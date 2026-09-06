"""CLI entry point for running the orchestrator workflow directly with `python -m orchestrator`."""

import argparse
import os
import sys
from colorama import Fore, Style, init

import json
from orchestrator.config import (
    load_config,
    get_preflight_config,
    get_run_store_config,
)
from orchestrator.context import get_project_root
from orchestrator.graph import graph
from orchestrator.metrics import format_summary_table, get_token_diagnostics, verify_token_aggregation_invariant
from orchestrator.preflight import format_preflight_report, run_preflight
from orchestrator.status import derive_status, describe_status, is_successful
from orchestrator.store import list_runs, load_run, resolve_run_id
from orchestrator.tracer import default_tracer
from orchestrator.types import get_agent_result


#: Console color for each verdict.
VERDICT_COLORS = {
    "PASS": Fore.GREEN,
    "FAIL": Fore.RED,
    "BLOCKED": Fore.MAGENTA,
    "UNKNOWN": Fore.YELLOW,
}


def verdict_color(verdict: str) -> str:
    """Return the console color associated with a verification verdict."""
    return VERDICT_COLORS.get(verdict, Fore.YELLOW)


# Ensure Windows consoles don't crash on model unicode outputs (arrows, emojis, dashes)
if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    if hasattr(sys.stderr, "reconfigure"):
        try:
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def safe_print(text: str = "") -> None:
    """Print text safely, replacing characters that cannot be encoded by the console."""
    try:
        print(text)
    except UnicodeEncodeError:
        encoding = sys.stdout.encoding or "utf-8"
        sanitized = text.encode(encoding, errors="replace").decode(encoding)
        print(sanitized)


def main() -> int:
    init(autoreset=True)
    parser = argparse.ArgumentParser(
        prog="python -m orchestrator",
        description="Run the LangGraph multi-agent orchestrator (Antigravity -> Claude Planner -> OpenCode -> Claude Verifier).",
    )
    parser.add_argument(
        "task",
        nargs="*",
        help="The task/instruction for the orchestrator to execute.",
    )
    parser.add_argument(
        "--project-root",
        dest="project_root",
        default=None,
        help="Optional project root directory path (defaults to current working directory).",
    )
    parser.add_argument(
        "--config",
        dest="config_path",
        default=None,
        help="Optional path to custom YAML configuration file (defaults to orchestrator.yaml).",
    )
    parser.add_argument(
        "--list-models",
        action="store_true",
        dest="list_models",
        help="List available configured models from the catalog and exit.",
    )
    parser.add_argument(
        "--max-repair-attempts",
        type=int,
        dest="max_repair_attempts",
        default=None,
        help="Maximum number of self-repair attempts if verification fails (defaults to config value).",
    )
    parser.add_argument(
        "--visible-terminals",
        action="store_true",
        dest="visible_terminals",
        default=None,
        help="Launch each agent visibly in its own terminal window.",
    )
    parser.add_argument(
        "--no-visible-terminals",
        action="store_false",
        dest="visible_terminals",
        default=None,
        help="Run in headless mode without opening terminal windows.",
    )
    parser.add_argument(
        "--terminal-type",
        dest="terminal_type",
        default=None,
        help="Terminal host type: 'antigravity_integrated', 'windows_terminal', 'console', 'auto', or 'none'.",
    )
    parser.add_argument(
        "--agent-execution-mode",
        dest="agent_execution_mode",
        default=None,
        help="Agent execution mode: 'auto' (prefer native TUI where supported), 'native_tui', or 'headless'.",
    )
    parser.add_argument(
        "--token-diagnostics",
        action="store_true",
        dest="token_diagnostics",
        default=False,
        help="Print structured token diagnostic records and invariant verification.",
    )
    parser.add_argument(
        "--doctor",
        action="store_true",
        dest="doctor",
        default=False,
        help="Probe every configured agent (binary, model, role) and exit without running a task.",
    )
    parser.add_argument(
        "--deep-preflight",
        action="store_true",
        dest="deep_preflight",
        default=None,
        help="Additionally invoke each agent binary's version command during preflight.",
    )
    parser.add_argument(
        "--no-preflight",
        action="store_true",
        dest="no_preflight",
        default=False,
        help="Skip preflight probing and launch agents immediately.",
    )
    parser.add_argument(
        "--no-run-store",
        action="store_true",
        dest="no_run_store",
        default=False,
        help="Do not persist this run to .orchestrator/runs.",
    )
    parser.add_argument(
        "--runs",
        nargs="?",
        type=int,
        const=20,
        dest="runs",
        default=None,
        help="List the most recent recorded runs (default 20) and exit.",
    )
    parser.add_argument(
        "--show-run",
        dest="show_run",
        default=None,
        help="Print a recorded run by id (accepts a unique prefix, or 'latest') and exit.",
    )
    parser.add_argument(
        "--isolate",
        dest="isolation",
        choices=("auto", "worktree", "none"),
        default=None,
        help=(
            "Workspace isolation for this run: 'auto' (isolate in a git worktree when "
            "possible), 'worktree' (require isolation, refuse to run without it), "
            "or 'none' (edit the project directly)."
        ),
    )
    parser.add_argument(
        "--no-isolate",
        action="store_const",
        const="none",
        dest="isolation",
        help="Shorthand for --isolate none: let agents edit the project directory.",
    )
    args = parser.parse_args()

    if args.list_models:
        try:
            root = get_project_root(args.project_root or os.getcwd())
            config = load_config(args.config_path, root)
        except Exception as exc:
            safe_print(f"{Fore.RED}Error loading configuration: {exc}{Style.RESET_ALL}")
            return 1

        models_catalog = config.get("models", {})
        safe_print(f"\n{Fore.CYAN}{Style.BRIGHT}AVAILABLE CONFIGURED MODELS{Style.RESET_ALL}\n")
        for provider, model_list in models_catalog.items():
            safe_print(f"{Fore.YELLOW}{Style.BRIGHT}{provider.upper()}{Style.RESET_ALL}")
            for m in model_list:
                m_id = m.get("id", "")
                m_name = m.get("name", "")
                safe_print(f"  {Fore.WHITE}{m_id:<32}{Style.RESET_ALL} {Fore.LIGHTBLACK_EX}{m_name}{Style.RESET_ALL}")
            safe_print()
        return 0

    # --- Preflight doctor (Tier 1 #6) --------------------------------------
    if args.doctor:
        try:
            root = get_project_root(args.project_root or os.getcwd())
            config = load_config(args.config_path, root)
        except Exception as exc:
            safe_print(f"{Fore.RED}Error loading configuration: {exc}{Style.RESET_ALL}")
            return 1

        cfg = get_preflight_config(config)
        deep = args.deep_preflight if args.deep_preflight is not None else cfg.get("deep", False)
        report = run_preflight(
            config,
            deep=bool(deep),
            strict=bool(cfg.get("strict", True)),
            timeout=int(cfg.get("timeout_seconds", 15)),
        )
        safe_print("")
        safe_print(format_preflight_report(report))
        safe_print("")
        return 0 if report.get("ok") else 1

    # --- Run store browsing (Tier 1 #4) ------------------------------------
    if args.runs is not None:
        try:
            root = get_project_root(args.project_root or os.getcwd())
            store_cfg = get_run_store_config(load_config(args.config_path, root))
        except Exception as exc:
            safe_print(f"{Fore.RED}Error: {exc}{Style.RESET_ALL}")
            return 1

        entries = list_runs(root, limit=args.runs, directory=store_cfg.get("directory"))
        if not entries:
            safe_print(f"{Fore.YELLOW}No recorded runs found.{Style.RESET_ALL}")
            return 0

        safe_print(f"\n{Fore.CYAN}{Style.BRIGHT}RECORDED RUNS{Style.RESET_ALL} ({len(entries)})\n")
        safe_print(f"  {'RUN ID':<26} {'STATUS':<16} {'VERDICT':<9} TASK")
        safe_print(f"  {'-'*26} {'-'*16} {'-'*9} {'-'*30}")
        for entry in entries:
            status = str(entry.get("status") or "unknown")
            verdict = str(entry.get("verdict") or "-")
            task = str(entry.get("task") or "")
            if len(task) > 46:
                task = task[:45] + "…"
            color = Fore.GREEN if is_successful(status) and status != "running" else Fore.RED
            if status == "running":
                color = Fore.YELLOW
            safe_print(
                f"  {entry.get('run_id',''):<26} {color}{status:<16}{Style.RESET_ALL} "
                f"{verdict_color(verdict)}{verdict:<9}{Style.RESET_ALL} {task}"
            )
        safe_print("")
        return 0

    if args.show_run:
        try:
            root = get_project_root(args.project_root or os.getcwd())
            store_cfg = get_run_store_config(load_config(args.config_path, root))
        except Exception as exc:
            safe_print(f"{Fore.RED}Error: {exc}{Style.RESET_ALL}")
            return 1

        directory = store_cfg.get("directory")
        resolved = resolve_run_id(root, args.show_run, directory=directory)
        if not resolved:
            safe_print(f"{Fore.RED}No run matching '{args.show_run}'.{Style.RESET_ALL}")
            safe_print("Use --runs to list recorded runs.")
            return 1

        run = load_run(root, resolved, directory=directory)
        if not run:
            safe_print(f"{Fore.RED}Could not read run '{resolved}'.{Style.RESET_ALL}")
            return 1

        safe_print(f"\n{Fore.CYAN}{Style.BRIGHT}RUN {run.get('run_id')}{Style.RESET_ALL}")
        safe_print(f"  {Fore.LIGHTBLACK_EX}Directory:{Style.RESET_ALL} {run.get('run_dir')}")
        safe_print(f"  {Fore.LIGHTBLACK_EX}Task:{Style.RESET_ALL}      {run.get('task')}")
        safe_print(f"  {Fore.LIGHTBLACK_EX}Started:{Style.RESET_ALL}   {run.get('started_at')}")
        safe_print(f"  {Fore.LIGHTBLACK_EX}Finished:{Style.RESET_ALL}  {run.get('finished_at') or '(incomplete)'}")
        run_status = str(run.get("status") or "unknown")
        safe_print(f"  {Fore.LIGHTBLACK_EX}Status:{Style.RESET_ALL}    {run_status} - {describe_status(run_status)}")
        summary = run.get("summary") or {}
        if summary.get("blocked_reason"):
            safe_print(f"  {Fore.MAGENTA}Human action required:{Style.RESET_ALL} {summary['blocked_reason']}")
        if summary.get("error"):
            safe_print(f"  {Fore.RED}Error:{Style.RESET_ALL} {summary['error']}")

        safe_print(f"\n{Fore.CYAN}{Style.BRIGHT}  EVENTS{Style.RESET_ALL}")
        for event in run.get("events") or []:
            name = event.get("event", "")
            line = f"    {event.get('timestamp','')}  {name}"
            if name == "agent_result":
                res = event.get("result") or {}
                extra = f"{res.get('agent')}/{res.get('role')} status={res.get('status')}"
                if res.get("verdict"):
                    extra += f" verdict={res.get('verdict')}"
                usage = res.get("token_usage") or {}
                if usage.get("available"):
                    extra += f" tokens={usage.get('total_tokens')}"
                line += f"  {Fore.LIGHTBLACK_EX}{extra}{Style.RESET_ALL}"
            elif name == "run_finished":
                s = event.get("summary") or {}
                line += f"  {Fore.LIGHTBLACK_EX}status={s.get('status')} verdict={s.get('verdict')}{Style.RESET_ALL}"
            safe_print(line)
        safe_print("")
        return 0

    if not args.task:
        safe_print(f"{Fore.RED}Error: No task provided.{Style.RESET_ALL}")
        safe_print(
            "Usage: python -m orchestrator \"<task>\"\n"
            "       python -m orchestrator --list-models | --doctor | --runs | --show-run <id>"
        )
        return 1

    task_str = " ".join(args.task)

    try:
        project_root = get_project_root(args.project_root or os.getcwd())
    except Exception as exc:
        safe_print(f"{Fore.RED}Error determining project root: {exc}{Style.RESET_ALL}")
        return 1

    # Log workflow start in tracer (prints header and task)
    default_tracer.log_workflow_start(task_str)

    initial_state = {
        "task": task_str,
        "project_root": project_root,
        "config_path": args.config_path,
        "agent_results": [],
    }
    if args.max_repair_attempts is not None:
        initial_state["max_repair_attempts"] = args.max_repair_attempts
    if args.visible_terminals is not None:
        initial_state["visible_terminals"] = args.visible_terminals
    if args.terminal_type is not None:
        initial_state["terminal_type"] = args.terminal_type
    if args.agent_execution_mode is not None:
        initial_state["agent_execution_mode"] = args.agent_execution_mode
    if args.no_preflight:
        initial_state["skip_preflight"] = True
    if args.no_run_store:
        initial_state["run_store_enabled"] = False
    if args.isolation is not None:
        # A CLI override wins over the config's workspace.isolation setting.
        try:
            override = load_config(args.config_path, project_root)
            override.setdefault("workspace", {})
            override["workspace"] = dict(override.get("workspace") or {})
            override["workspace"]["isolation"] = args.isolation
            initial_state["config"] = override
        except Exception as exc:
            safe_print(f"{Fore.RED}Error loading configuration: {exc}{Style.RESET_ALL}")
            return 1

    result = graph.invoke(initial_state)

    # A failed preflight halted the run before any agent launched. Show the
    # full report so the user can see exactly which check failed.
    report = result.get("preflight")
    if isinstance(report, dict) and not report.get("ok") and report.get("strict"):
        safe_print("")
        safe_print(format_preflight_report(report))
        safe_print("")
        return 1

    if result.get("error"):
        safe_print(f"{Fore.RED}{Style.BRIGHT}Pipeline Error:{Style.RESET_ALL} {result['error']}")
        return 1


    # Extract structured results from state
    agent_results = result.get("agent_results") or []
    researcher_res = get_agent_result(agent_results, role="researcher")
    planner_res = get_agent_result(agent_results, role="planner")
    implementer_res = get_agent_result(agent_results, role="implementer")
    verifier_res = get_agent_result(agent_results, role="verifier")

    # Outputs come from the structured results. The old flat aliases
    # (analysis/review/implementation/verification) were removed from state.
    researcher_output = researcher_res["output"] if researcher_res else "No analysis output returned."
    planner_output = planner_res["output"] if planner_res else "No review output returned."
    implementer_output = implementer_res["output"] if implementer_res else "No implementation output returned."
    verifier_output = verifier_res["output"] if verifier_res else "No verification output returned."

    res_agent = (researcher_res.get("agent") or "antigravity").upper() if researcher_res else "ANTIGRAVITY"
    res_role = (researcher_res.get("role") or "researcher").upper() if researcher_res else "RESEARCHER"
    res_model_str = f" [Model: {researcher_res.get('model')}]" if (researcher_res and researcher_res.get("model")) else ""

    plan_agent = (planner_res.get("agent") or "claude").upper() if planner_res else "CLAUDE CODE"
    plan_role = (planner_res.get("role") or "planner").upper() if planner_res else "PLANNER"
    plan_model_str = f" [Model: {planner_res.get('model')}]" if (planner_res and planner_res.get("model")) else ""

    impl_agent = (implementer_res.get("agent") or "opencode").upper() if implementer_res else "OPENCODE"
    impl_role = (implementer_res.get("role") or "implementer").upper() if implementer_res else "IMPLEMENTER"
    impl_model_str = f" [Model: {implementer_res.get('model')}]" if (implementer_res and implementer_res.get("model")) else ""

    ver_agent = (verifier_res.get("agent") or "claude").upper() if verifier_res else "CLAUDE CODE"
    ver_role = (verifier_res.get("role") or "verifier").upper() if verifier_res else "VERIFIER"
    ver_model_str = f" [Model: {verifier_res.get('model')}]" if (verifier_res and verifier_res.get("model")) else ""
    verdict = (verifier_res.get("verdict") if verifier_res else None) or result.get("verification_verdict") or "UNKNOWN"
    v_color = verdict_color(verdict)
    verdict_badge = f" [Verdict: {v_color}{verdict}{Style.RESET_ALL}{Fore.YELLOW}{Style.BRIGHT}]"

    # Print results attributed to Agent, Role & Model
    safe_print(f"{Fore.GREEN}{Style.BRIGHT}========================================{Style.RESET_ALL}")
    safe_print(f"{Fore.GREEN}{Style.BRIGHT}   1. {res_agent} (ROLE: {res_role}){res_model_str}{Style.RESET_ALL}")
    safe_print(f"{Fore.GREEN}{Style.BRIGHT}========================================{Style.RESET_ALL}\n")
    safe_print(researcher_output)
    safe_print()

    safe_print(f"{Fore.MAGENTA}{Style.BRIGHT}========================================{Style.RESET_ALL}")
    safe_print(f"{Fore.MAGENTA}{Style.BRIGHT}   2. {plan_agent} (ROLE: {plan_role}){plan_model_str}{Style.RESET_ALL}")
    safe_print(f"{Fore.MAGENTA}{Style.BRIGHT}========================================{Style.RESET_ALL}\n")
    safe_print(planner_output)
    safe_print()

    if implementer_res:
        safe_print(f"{Fore.BLUE}{Style.BRIGHT}========================================{Style.RESET_ALL}")
        safe_print(f"{Fore.BLUE}{Style.BRIGHT}   3. {impl_agent} (ROLE: {impl_role}){impl_model_str}{Style.RESET_ALL}")
        safe_print(f"{Fore.BLUE}{Style.BRIGHT}========================================{Style.RESET_ALL}\n")
        safe_print(implementer_output)
        safe_print()

    if verifier_res:
        safe_print(f"{Fore.YELLOW}{Style.BRIGHT}========================================{Style.RESET_ALL}")
        safe_print(f"{Fore.YELLOW}{Style.BRIGHT}   4. {ver_agent} (ROLE: {ver_role}){ver_model_str}{verdict_badge}{Style.RESET_ALL}")
        safe_print(f"{Fore.YELLOW}{Style.BRIGHT}========================================{Style.RESET_ALL}\n")
        safe_print(verifier_output)
        safe_print()

    repair_attempts = result.get("repair_attempts", 0)
    max_repairs = result.get("max_repair_attempts", 2)
    history = result.get("verification_history") or []

    # Tier 1 #7 - a BLOCKED run needs a person, so say plainly what they must do.
    blocked_reason = result.get("blocked_reason")
    if blocked_reason:
        safe_print(f"{Fore.MAGENTA}{Style.BRIGHT}{'='*40}{Style.RESET_ALL}")
        safe_print(f"{Fore.MAGENTA}{Style.BRIGHT}   NEEDS YOU{Style.RESET_ALL}")
        safe_print(f"{Fore.MAGENTA}{Style.BRIGHT}{'='*40}{Style.RESET_ALL}\n")
        safe_print(blocked_reason)
        safe_print(
            f"\n{Fore.LIGHTBLACK_EX}No repair attempts were consumed: the verifier judged "
            f"that no coding agent could resolve this.{Style.RESET_ALL}\n"
        )

    if repair_attempts > 0 or len(history) > 1:
        safe_print(f"{Fore.CYAN}Self-Repair Tracking:{Style.RESET_ALL}")
        safe_print(f"  Repair attempts executed: {repair_attempts} / {max_repairs}")
        safe_print(f"  Total verification evaluations: {len(history)}")
        safe_print()

    # Print final orchestration summary table
    summary_table = format_summary_table(
        agent_results=agent_results,
        verification_verdict=verdict,
        repair_attempts=repair_attempts,
        skills=result.get("skills"),
    )
    safe_print(summary_table)
    safe_print()

    if args.token_diagnostics:
        safe_print(f"{Fore.CYAN}{Style.BRIGHT}=== TOKEN DIAGNOSTICS & INVARIANT AUDIT ==={Style.RESET_ALL}")
        diagnostics = get_token_diagnostics(agent_results)
        inv = verify_token_aggregation_invariant(agent_results)
        safe_print(json.dumps(diagnostics, indent=2))
        safe_print(f"\n{Fore.YELLOW}Aggregation Invariant Check:{Style.RESET_ALL}")
        safe_print(f"  Sum of displayed tokens: {inv['sum_displayed']:,}")
        safe_print(f"  Reported total tokens:   {inv['reported_total']:,}")
        diff_color = Fore.GREEN if inv["is_valid"] else Fore.RED
        safe_print(f"  Difference:              {diff_color}{inv['difference']}{Style.RESET_ALL}")
        safe_print(f"  Status:                  {diff_color}{'VALID (EXACT MATCH)' if inv['is_valid'] else 'INVALID DISCREPANCY'}{Style.RESET_ALL}\n")

    # Status is derived from facts, never read from a value a node remembered
    # to write (Tier 1 #5). `result["status"]` is finalize_node's derivation;
    # recomputing here keeps the CLI correct even for a partial state.
    status_val = result.get("status") or derive_status(result)
    if status_val == "blocked":
        status_color = Fore.MAGENTA
    elif is_successful(status_val):
        status_color = Fore.GREEN
    else:
        status_color = Fore.RED

    # Tier 0 #3 - say where the agents' edits actually landed.
    workspace = result.get("workspace")
    ws_summary = result.get("workspace_summary") or {}
    if isinstance(workspace, dict):
        if workspace.get("isolated"):
            changed = int(ws_summary.get("change_count") or 0)
            safe_print(f"{Fore.CYAN}{Style.BRIGHT}Workspace (isolated){Style.RESET_ALL}")
            safe_print(f"  {Fore.LIGHTBLACK_EX}Worktree:{Style.RESET_ALL} {workspace.get('path')}")
            safe_print(f"  {Fore.LIGHTBLACK_EX}Branch:{Style.RESET_ALL}   {workspace.get('branch')}")
            if ws_summary.get("commit"):
                safe_print(f"  {Fore.LIGHTBLACK_EX}Commit:{Style.RESET_ALL}   {ws_summary['commit']}")
            safe_print(f"  {Fore.LIGHTBLACK_EX}Changed:{Style.RESET_ALL}  {changed} path(s)")
            base = workspace.get("base_branch") or "HEAD"
            safe_print(
                f"  {Fore.LIGHTBLACK_EX}Review:{Style.RESET_ALL}   "
                f"git diff {base}...{workspace.get('branch')}"
            )
            safe_print(
                f"  {Fore.LIGHTBLACK_EX}Merge:{Style.RESET_ALL}    "
                f"git merge {workspace.get('branch')}"
            )
            safe_print()
        else:
            safe_print(
                f"{Fore.YELLOW}Workspace not isolated{Style.RESET_ALL} "
                f"{Fore.LIGHTBLACK_EX}({workspace.get('reason')}){Style.RESET_ALL} - "
                f"agents edited {workspace.get('path')} directly.\n"
            )

    if result.get("run_dir"):
        safe_print(f"{Fore.LIGHTBLACK_EX}Run recorded at: {result['run_dir']}{Style.RESET_ALL}")

    safe_print(
        f"{Fore.CYAN}{Style.BRIGHT}=== Pipeline Completed "
        f"(Status: {status_color}{status_val}{Style.RESET_ALL}{Fore.CYAN}{Style.BRIGHT}, "
        f"Final Verdict: {v_color}{verdict}{Style.RESET_ALL}{Fore.CYAN}{Style.BRIGHT}, "
        f"Repairs: {repair_attempts}/{max_repairs}) ==={Style.RESET_ALL}"
    )
    safe_print(f"{Fore.LIGHTBLACK_EX}{describe_status(status_val)}{Style.RESET_ALL}")

    return 0 if is_successful(status_val) else 1


if __name__ == "__main__":
    sys.exit(main())

