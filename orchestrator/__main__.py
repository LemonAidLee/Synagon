"""CLI entry point for running the orchestrator workflow directly with `python -m orchestrator`."""

import argparse
import os
import sys
from colorama import Fore, Style, init

import json
from orchestrator.acceptance import describe_check
from orchestrator.archive import (
    execute_archive,
    execute_restore,
    format_archive_plan,
    format_archive_result,
    format_archived_list,
    format_restore_result,
    list_archived,
    plan_archive,
    plan_restore,
)
from orchestrator.approvals import (
    GATE_BEFORE_MERGE,
    STATUS_APPROVED,
    STATUS_PENDING,
    STATUS_REJECTED,
    describe_approval,
    list_approvals,
    resolve_approval,
)
from orchestrator.board import COLUMN_READY, build_board, format_board
from orchestrator.budget import describe_budget
from orchestrator.config import (
    load_config,
    get_approval_config,
    get_delegation_config,
    get_delivery_config,
    get_planning_config,
    get_preflight_config,
    get_run_store_config,
    get_workspace_config,
)
from orchestrator.delivery import (
    deliveries_by_key,
    deliver,
    format_deliveries,
    format_delivery,
    list_deliveries,
    refresh_all,
)
from orchestrator.context import get_project_root
from orchestrator.decompose import format_plan, plan_is_usable
from orchestrator.goals import (
    GoalStore,
    list_goals,
    load_goal,
    open_goal,
    resolve_goal_id,
    task_outcomes,
)
from orchestrator.graph import build_graph, build_plan_graph
from orchestrator.scheduler import format_goal_summary, run_goal
from orchestrator.workspace import head_commit
from orchestrator.metrics import format_summary_table, get_token_diagnostics, verify_token_aggregation_invariant
from orchestrator.preflight import format_preflight_report, run_preflight
from orchestrator.prune import (
    DEFAULT_MAX_AGE,
    DEFAULT_MERGED_MAX_AGE,
    execute_prune,
    format_prune_plan,
    format_prune_result,
    parse_age,
    plan_prune,
)
from orchestrator.resume import (
    load_resumable_run,
    prepare_resumed_workspace,
    reconstruct_state,
    replayable_roles,
)
from orchestrator.stats import collect_runs, compute_stats, format_stats
from orchestrator.status import (
    derive_delivery_state,
    derive_goal_summary,
    derive_status,
    describe_goal_status,
    describe_status,
    goal_is_successful,
    is_successful,
)
from orchestrator.store import RunStore, list_runs, load_run, resolve_run_id
from orchestrator.teams import (
    format_team,
    get_template,
    list_templates,
    normalize_team,
    team_from_config,
    validate_team,
    write_team,
)
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


def read_board(project_root: str, config) -> dict:
    """Read every store and project them into one board (Roadmap Phase 4).

    The projection itself is pure and lives in `orchestrator.board`; this is the I/O half, so
    the CLI and the web view can share one definition of what a board is.
    """
    delegation_cfg = get_delegation_config(config)
    store_cfg = get_run_store_config(config)
    approval_cfg = get_approval_config(config)
    delivery_cfg = get_delivery_config(config)

    goals = list_goals(project_root, limit=0, directory=delegation_cfg.get("goals_directory"))
    full_goals = []
    outcomes_by_goal = {}
    for entry in goals:
        goal = load_goal(
            project_root, str(entry.get("goal_id")), directory=delegation_cfg.get("goals_directory")
        )
        if not goal:
            continue
        full_goals.append(goal)
        outcomes_by_goal[goal.get("goal_id")] = task_outcomes(goal)

    return build_board(
        goals=full_goals,
        outcomes_by_goal=outcomes_by_goal,
        runs=list_runs(project_root, limit=0, directory=store_cfg.get("directory")),
        approvals=list_approvals(project_root, directory=approval_cfg.get("directory")),
        deliveries=deliveries_by_key(project_root, directory=delivery_cfg.get("directory")),
    )


def resolve_card(board: dict, target: str) -> list:
    """Return every card matching what a person typed. Pure.

    A card id is either ``<goal id>/<task id>`` or a run id, both of which are long. Matching
    a prefix, a suffix, or the task id alone is what makes `--deliver` typable; returning
    *every* match rather than the first is what stops it from picking one for you.
    """
    wanted = str(target or "").strip()
    if not wanted:
        return []
    cards = board.get("cards") or []

    exact = [c for c in cards if str(c.get("id")) == wanted]
    if exact:
        return exact
    return [
        c for c in cards
        if str(c.get("id", "")).startswith(wanted)
        or str(c.get("id", "")).endswith(wanted)
        or str(c.get("task_id") or "") == wanted
        or str(c.get("run_id") or "").startswith(wanted)
    ]


def deliver_card(project_root: str, config, card: dict, printer=None) -> dict:
    """Push one card's branch and open a pull request for it (Roadmap Phase 6).

    Called only from a person's own command: `--deliver`, or answering a `before_merge` gate
    when `delivery.on_approve` is set. There is no third caller, and that is invariant 13.
    """
    say = printer or safe_print
    delivery_cfg = get_delivery_config(config)

    goal_line = f"Goal: {card['goal']}\n\n" if card.get("goal") else ""
    body = (
        f"{goal_line}"
        f"Produced by the orchestrator and verified before delivery.\n\n"
        f"- session: `{card.get('run_id') or 'n/a'}`\n"
        f"- branch: `{card.get('branch')}`\n"
        f"- tokens: {card.get('tokens', 0):,}\n"
    )

    record = deliver(
        project_root,
        delivery_cfg,
        branch=str(card.get("branch") or ""),
        title=str(card.get("title") or card.get("id")),
        body=body,
        goal_id=card.get("goal_id"),
        task_id=card.get("task_id"),
        run_id=card.get("run_id") if not card.get("task_id") else None,
    )

    state = derive_delivery_state(record)
    default_tracer.log_delivery(
        subject=str(card.get("id")),
        state="refused" if record.get("refused") else state,
        url=str((record.get("pr") or {}).get("url") or ""),
        detail=str(record.get("refused") or record.get("error") or ""),
    )
    say("")
    say(format_delivery(record))
    say("")
    return record


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
        choices=("auto", "native_tui", "headless"),
        help=(
            "Agent execution mode: 'auto' (native TUI where a supported integration and a "
            "surface exist - OpenCode only - headless elsewhere), 'native_tui' (require it; "
            "refuses agents without one, never falls back), or 'headless'."
        ),
    )
    parser.add_argument(
        "--keep-terminals",
        action="store_false",
        dest="close_terminal_on_completion",
        default=None,
        help=(
            "Leave visible terminals and native TUI sessions open after they finish "
            "(execution.close_terminal_on_completion: false for this run)."
        ),
    )
    parser.add_argument(
        "--close-sessions",
        action="store_true",
        dest="close_sessions",
        default=False,
        help="Close every native TUI session kept open for inspection, and stop its server.",
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
        "--plan-only",
        action="store_true",
        dest="plan_only",
        default=False,
        help=(
            "Break the goal into a task graph and stop. Prints the tasks, their dependencies, "
            "and the order they could run in. Implements nothing."
        ),
    )
    parser.add_argument(
        "--delegate",
        action="store_true",
        dest="delegate",
        default=False,
        help=(
            "Treat the input as a goal: decompose it into tasks and run each one as its own "
            "session, on its own branch, in dependency order."
        ),
    )
    parser.add_argument(
        "--parallel",
        type=int,
        dest="parallel",
        default=None,
        help=(
            "With --delegate, how many tasks may run at once (default 1, one at a time). "
            "Overrides delegation.max_parallel."
        ),
    )
    parser.add_argument(
        "--board",
        action="store_true",
        dest="board",
        default=False,
        help=(
            "Show every piece of work as a card in a column - Queued, Working, Needs You, "
            "In Review, Ready to Merge, Stalled - derived from what actually happened."
        ),
    )
    parser.add_argument(
        "--serve",
        nargs="?",
        type=int,
        const=8730,
        dest="serve",
        default=None,
        help=(
            "Serve the board and the live office view on http://127.0.0.1:PORT "
            "(default 8730). Read-only: it watches the event log and changes nothing."
        ),
    )
    parser.add_argument(
        "--design",
        nargs="?",
        type=int,
        const=8730,
        dest="design",
        default=None,
        help=(
            "Open the team design surface on http://127.0.0.1:PORT (default 8730): a node "
            "editor over orchestrator.yaml, with team templates. The only page that can save."
        ),
    )
    parser.add_argument(
        "--memory",
        nargs="?",
        type=int,
        const=0,
        dest="memory",
        default=None,
        help=(
            "Print what this repository has taught the planner - the facts previous goals "
            "recorded that the decomposer is now told about - and exit. Optionally over the "
            "last N goals. Add --json for the projection, or --plan-only for the exact "
            "prompt block."
        ),
    )
    parser.add_argument(
        "--review",
        nargs="?",
        type=int,
        const=8730,
        dest="review",
        default=None,
        help=(
            "Open the board and office on http://127.0.0.1:PORT (default 8730) in review "
            "mode: the only mode of this server that can answer an approval gate and deliver "
            "a Ready to Merge card. It cannot start work or edit the team."
        ),
    )
    parser.add_argument(
        "--daemon",
        nargs="?",
        type=int,
        const=None,
        dest="daemon",
        default=-1,
        help=(
            "Run the persistent local daemon on http://127.0.0.1:PORT (default 8740) and "
            "open the cockpit: the one surface that can start, cancel, approve and deliver "
            "work. Every route needs the token it mints at launch."
        ),
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        dest="no_browser",
        default=False,
        help=(
            "With --serve, --design or --daemon, do not open a browser. What the desktop "
            "shell passes, because it opens the page in its own window."
        ),
    )
    parser.add_argument(
        "--teams",
        action="store_true",
        dest="teams",
        default=False,
        help="Show the team this project is configured with, and the templates available.",
    )
    parser.add_argument(
        "--apply-team",
        dest="apply_team",
        default=None,
        help=(
            "Replace the configured team with a template (see --teams). Validates the result "
            "before writing and keeps a backup of the previous orchestrator.yaml."
        ),
    )
    parser.add_argument(
        "--deliver",
        dest="deliver",
        default=None,
        help=(
            "Push one card's branch and open a pull request for it (accepts a card id, or a "
            "unique part of one). Only work the board calls Ready to Merge can be delivered."
        ),
    )
    parser.add_argument(
        "--deliveries",
        action="store_true",
        dest="deliveries",
        default=False,
        help="List what has been pushed, and the CI and review facts recorded for it.",
    )
    parser.add_argument(
        "--refresh-deliveries",
        nargs="?",
        type=int,
        const=0,
        dest="refresh_deliveries",
        default=None,
        help=(
            "Re-read CI and review state from the forge for every open pull request "
            "(optionally only the most recent N) and record what it says."
        ),
    )
    parser.add_argument(
        "--approvals",
        nargs="?",
        type=str,
        const="pending",
        dest="approvals",
        default=None,
        help="List approval gates waiting for you (or 'all', 'approved', 'rejected') and exit.",
    )
    parser.add_argument(
        "--approve",
        dest="approve",
        default=None,
        help="Approve a pending gate by id (accepts a unique prefix).",
    )
    parser.add_argument(
        "--reject",
        dest="reject",
        default=None,
        help="Reject a pending gate by id (accepts a unique prefix).",
    )
    parser.add_argument(
        "--note",
        dest="note",
        default="",
        help="With --approve or --reject, the reason to record alongside the decision.",
    )
    parser.add_argument(
        "--resume-goal",
        dest="resume_goal",
        default=None,
        help=(
            "Continue a delegated goal by id (accepts a unique prefix, or 'latest'). "
            "Tasks that already delivered are replayed, not re-run."
        ),
    )
    parser.add_argument(
        "--goals",
        nargs="?",
        type=int,
        const=20,
        dest="goals",
        default=None,
        help="List the most recent delegated goals (default 20) and exit.",
    )
    parser.add_argument(
        "--show-goal",
        dest="show_goal",
        default=None,
        help="Print a delegated goal by id (accepts a unique prefix, or 'latest') and exit.",
    )
    parser.add_argument(
        "--acceptance-command",
        dest="acceptance_command",
        default=None,
        help=(
            "Command the orchestrator runs itself in the workspace before verifying "
            "(e.g. 'pytest -q'). Overrides verification.acceptance.command."
        ),
    )
    parser.add_argument(
        "--no-acceptance",
        action="store_true",
        dest="no_acceptance",
        default=False,
        help="Skip the objective acceptance gate for this run.",
    )
    parser.add_argument(
        "--resume",
        dest="resume",
        default=None,
        help=(
            "Continue a recorded run by id (accepts a unique prefix, or 'latest'). "
            "Phases the run already completed are replayed from its event log "
            "instead of being re-run."
        ),
    )
    parser.add_argument(
        "--stats",
        nargs="?",
        type=int,
        const=0,
        dest="stats",
        default=None,
        help=(
            "Analyze the recorded runs (optionally only the most recent N) and exit: "
            "pass rates by agent/model/role, what a pass costs against a failure, "
            "whether repair attempts and ensembles earn their price."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        default=False,
        help="With --stats, emit the report as JSON instead of a table.",
    )
    parser.add_argument(
        "--prune-runs",
        action="store_true",
        dest="prune_runs",
        default=False,
        help=(
            "Delete old orchestrator run branches (and their leftover worktrees) "
            "and exit. Shows the plan and asks before deleting anything."
        ),
    )
    parser.add_argument(
        "--archive-runs",
        action="store_true",
        dest="archive_runs",
        default=False,
        help=(
            "Move runs in which no agent was really invoked out of the run store and into "
            ".orchestrator/archive/runs, so that --stats, the board and the planner's "
            "memory stop reading them. Shows the plan and asks first. Nothing is deleted; "
            "--restore-runs puts them back."
        ),
    )
    parser.add_argument(
        "--matching",
        dest="matching",
        default=None,
        help=(
            "With --archive-runs, also archive runs whose task contains this text. "
            "Combines with the synthetic test rather than replacing it."
        ),
    )
    parser.add_argument(
        "--restore-runs",
        nargs="*",
        dest="restore_runs",
        default=None,
        help=(
            "Move archived runs back into the store (all of them, or the ids/prefixes "
            "named). A run whose id is already in the store is reported, never overwritten."
        ),
    )
    parser.add_argument(
        "--archived",
        action="store_true",
        dest="archived",
        default=False,
        help="List the runs currently held in the archive, and when each was archived.",
    )
    parser.add_argument(
        "--older-than",
        dest="older_than",
        default=DEFAULT_MAX_AGE,
        help=f"With --prune-runs, the minimum branch age to delete (default: {DEFAULT_MAX_AGE}).",
    )
    parser.add_argument(
        "--merged-older-than",
        dest="merged_older_than",
        default=DEFAULT_MERGED_MAX_AGE,
        help=(
            "With --prune-runs, the shorter age at which a branch whose pull request merged "
            f"qualifies (default: {DEFAULT_MERGED_MAX_AGE}). Its work is already in the base "
            "branch, so the branch is a duplicate rather than the only copy."
        ),
    )
    parser.add_argument(
        "--keep-failed",
        action="store_true",
        dest="keep_failed",
        default=False,
        help="With --prune-runs, keep branches whose run did not succeed, so failures stay inspectable.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        default=False,
        help="With --prune-runs, print the plan and delete nothing.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        dest="assume_yes",
        default=False,
        help="With --prune-runs, skip the confirmation prompt.",
    )
    parser.add_argument(
        "--max-total-tokens",
        type=int,
        dest="max_total_tokens",
        default=None,
        help="Stop escalating once the run has spent this many tokens (0 = unlimited).",
    )
    parser.add_argument(
        "--max-duration-seconds",
        type=int,
        dest="max_duration_seconds",
        default=None,
        help="Stop escalating once the run has been going this long (0 = unlimited).",
    )
    parser.add_argument(
        "--keep-worktree",
        action="store_true",
        dest="keep_worktree",
        default=None,
        help="Leave the run's worktree on disk instead of removing it once the work is committed.",
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

    # --- Native TUI sessions kept open for inspection ----------------------
    if args.close_sessions:
        from orchestrator.native_sessions import close_sessions

        outcomes = close_sessions()
        if not outcomes:
            safe_print("No native TUI sessions are being kept open.")
            return 0
        for outcome in outcomes:
            safe_print(
                f"  {outcome.get('session_id')}: terminal "
                f"{'closed' if outcome.get('terminal_closed') else 'not found'}, server "
                f"{'stopped' if outcome.get('server_stopped') else 'not stopped'}"
                f"{' - ' + outcome['note'] if outcome.get('note') else ''}"
            )
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
            execution_mode=args.agent_execution_mode,
            visible_terminals=args.visible_terminals,
            terminal_type=args.terminal_type,
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
        if summary.get("acceptance"):
            safe_print(f"  {Fore.LIGHTBLACK_EX}Gate:{Style.RESET_ALL}      {describe_check(summary['acceptance'])}")
        if summary.get("task_plan"):
            plan_meta = summary["task_plan"]
            safe_print(
                f"  {Fore.LIGHTBLACK_EX}Plan:{Style.RESET_ALL}      "
                f"{plan_meta.get('tasks', 0)} task(s), {plan_meta.get('waves', 0)} wave(s)"
            )
        if summary.get("budget_exhausted_reason"):
            safe_print(f"  {Fore.YELLOW}Budget:{Style.RESET_ALL}    {summary['budget_exhausted_reason']}")
        elif summary.get("budget"):
            safe_print(f"  {Fore.LIGHTBLACK_EX}Budget:{Style.RESET_ALL}    {describe_budget(summary['budget'])}")
        ws_recorded = (summary.get("workspace") or {}) if isinstance(summary.get("workspace"), dict) else {}
        if ws_recorded.get("branch"):
            safe_print(f"  {Fore.LIGHTBLACK_EX}Branch:{Style.RESET_ALL}    {ws_recorded['branch']}")
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

    # --- The daemon and the cockpit (Roadmap Phases 8-10) ------------------
    if args.daemon != -1:
        try:
            root = get_project_root(args.project_root or os.getcwd())
            daemon_config = load_config(args.config_path, root)
        except Exception as exc:
            safe_print(f"{Fore.RED}Error: {exc}{Style.RESET_ALL}")
            return 1

        from orchestrator.daemon import DEFAULT_DAEMON_PORT, serve_daemon

        try:
            serve_daemon(
                root,
                daemon_config,
                port=int(args.daemon if args.daemon is not None else DEFAULT_DAEMON_PORT),
                open_browser=not args.no_browser,
                printer=safe_print,
                config_path=args.config_path,
            )
        except KeyboardInterrupt:
            safe_print("\nStopped.")
        return 0

    # --- The board (Roadmap Phase 4), and the design surface (Phase 7) -----
    if (
        args.board
        or args.serve is not None
        or args.design is not None
        or args.review is not None
    ):
        try:
            root = get_project_root(args.project_root or os.getcwd())
            board_config = load_config(args.config_path, root)
        except Exception as exc:
            safe_print(f"{Fore.RED}Error: {exc}{Style.RESET_ALL}")
            return 1

        if args.serve is not None or args.design is not None or args.review is not None:
            from orchestrator.serve import serve_board

            # Three modes, never two at once: each flag turns on exactly its own routes, and
            # asking for two would make it ambiguous which capability the page was given.
            designing = args.design is not None
            reviewing = args.review is not None
            if designing and reviewing:
                safe_print(
                    f"{Fore.RED}--design and --review are separate modes; "
                    f"start one server for each.{Style.RESET_ALL}"
                )
                return 1

            port = args.design if designing else (args.review if reviewing else args.serve)
            try:
                serve_board(
                    root,
                    board_config,
                    port=int(port),
                    open_browser=not args.no_browser,
                    printer=safe_print,
                    design=designing,
                    config_path=args.config_path,
                    review=reviewing,
                )
            except KeyboardInterrupt:
                safe_print("\nStopped.")
            return 0

        board = read_board(root, board_config)
        if args.as_json:
            safe_print(json.dumps(board, indent=2, default=str))
            return 0
        safe_print(f"\n{Fore.CYAN}{Style.BRIGHT}BOARD{Style.RESET_ALL}\n")
        safe_print(format_board(board))
        safe_print("")
        return 0

    # --- The design surface, from the terminal (Roadmap Phase 7) -----------
    if args.teams or args.apply_team:
        try:
            root = get_project_root(args.project_root or os.getcwd())
            team_config = load_config(args.config_path, root)
        except Exception as exc:
            safe_print(f"{Fore.RED}Error: {exc}{Style.RESET_ALL}")
            return 1

        if args.apply_team:
            template = get_template(args.apply_team)
            if not template:
                safe_print(f"{Fore.RED}No team template called '{args.apply_team}'.{Style.RESET_ALL}")
                safe_print("Use --teams to see the templates available.")
                return 1

            team = normalize_team(template)
            problems = validate_team(team, team_config)
            if problems:
                safe_print(f"{Fore.RED}That template does not fit this project:{Style.RESET_ALL}")
                for problem in problems:
                    safe_print(f"  - {problem}")
                return 1

            result = write_team(root, team, config_path=args.config_path)
            if not result.get("ok"):
                safe_print(f"{Fore.RED}Not written: {result.get('error')}{Style.RESET_ALL}")
                return 1

            safe_print("")
            safe_print(f"{Fore.GREEN}Applied the {args.apply_team} team{Style.RESET_ALL} "
                       f"to {result.get('path')}")
            if result.get("backup"):
                safe_print(f"{Fore.LIGHTBLACK_EX}Previous version kept at "
                           f"{result['backup']}{Style.RESET_ALL}")
            safe_print("")
            safe_print(format_team(team))
            safe_print("")
            return 0

        current = team_from_config(team_config)
        safe_print(f"\n{Fore.CYAN}{Style.BRIGHT}THIS PROJECT'S TEAM{Style.RESET_ALL}\n")
        safe_print(format_team(current))
        problems = validate_team(current, team_config)
        if problems:
            safe_print("")
            for problem in problems:
                safe_print(f"  {Fore.YELLOW}{problem}{Style.RESET_ALL}")

        safe_print(f"\n{Fore.CYAN}{Style.BRIGHT}TEMPLATES{Style.RESET_ALL}\n")
        for template in list_templates():
            phases = template["phases"]
            shape = " -> ".join(
                ph["role"] + (f" x{len(ph['agents'])}" if len(ph["agents"]) > 1 else "")
                for ph in phases
            )
            safe_print(f"  {Fore.WHITE}{Style.BRIGHT}{template['name']:<10}{Style.RESET_ALL} "
                       f"{template['label']}")
            safe_print(f"             {Fore.LIGHTBLACK_EX}{template['description']}{Style.RESET_ALL}")
            safe_print(f"             {shape}   "
                       f"[{template['consensus']} consensus, "
                       f"{template['max_repair_attempts']} repair attempts]")
            safe_print("")
        safe_print(f"{Fore.LIGHTBLACK_EX}Apply one with: python -m orchestrator "
                   f"--apply-team careful{Style.RESET_ALL}")
        safe_print(f"{Fore.LIGHTBLACK_EX}Compose one visually: python -m orchestrator "
                   f"--design{Style.RESET_ALL}\n")
        return 0

    # --- Outward delivery (Roadmap Phase 6) --------------------------------
    if args.deliveries or args.refresh_deliveries is not None or args.deliver:
        try:
            root = get_project_root(args.project_root or os.getcwd())
            delivery_config = load_config(args.config_path, root)
        except Exception as exc:
            safe_print(f"{Fore.RED}Error: {exc}{Style.RESET_ALL}")
            return 1

        directory = get_delivery_config(delivery_config).get("directory")

        if args.deliver:
            board = read_board(root, delivery_config)
            matches = resolve_card(board, args.deliver)
            if not matches:
                safe_print(f"{Fore.RED}No card matching '{args.deliver}'.{Style.RESET_ALL}")
                safe_print("Use --board to see what there is.")
                return 1
            if len(matches) > 1:
                safe_print(f"{Fore.YELLOW}'{args.deliver}' matches "
                           f"{len(matches)} cards:{Style.RESET_ALL}")
                for card in matches:
                    safe_print(f"  {card['id']}  [{card['column']}]  {card['title']}")
                return 1

            card = matches[0]
            if card.get("column") != COLUMN_READY:
                safe_print(
                    f"{Fore.YELLOW}That card is in '{card.get('column')}', not "
                    f"'{COLUMN_READY}'.{Style.RESET_ALL}"
                )
                safe_print(
                    "  Only work that is delivered, verified, and cleared at whatever gate "
                    "was asked for can be published."
                )
                if card.get("merge_gate") == STATUS_PENDING:
                    safe_print("  A before_merge gate is waiting: python -m orchestrator --approvals")
                return 1

            record = deliver_card(root, delivery_config, card)
            return 0 if not (record.get("refused") or record.get("error")) else 1

        if args.refresh_deliveries is not None:
            records = list_deliveries(root, directory=directory)
            if not records:
                safe_print(f"{Fore.YELLOW}Nothing has been delivered yet.{Style.RESET_ALL}")
                return 0
            safe_print(f"{Fore.LIGHTBLACK_EX}Asking the forge about "
                       f"{len(records)} delivered branch(es)...{Style.RESET_ALL}")
            refreshed = refresh_all(root, directory=directory, limit=args.refresh_deliveries)
            if args.as_json:
                safe_print(json.dumps(refreshed, indent=2, default=str))
                return 0
            safe_print(f"\n{Fore.CYAN}{Style.BRIGHT}DELIVERIES{Style.RESET_ALL}\n")
            safe_print(format_deliveries(refreshed))
            safe_print("")
            return 0

        records = list_deliveries(root, directory=directory)
        if args.as_json:
            safe_print(json.dumps(records, indent=2, default=str))
            return 0
        safe_print(f"\n{Fore.CYAN}{Style.BRIGHT}DELIVERIES{Style.RESET_ALL}\n")
        safe_print(format_deliveries(records))
        safe_print("")
        return 0

    # --- Approval gates (Roadmap Phase 5) ----------------------------------
    if args.approvals is not None or args.approve or args.reject:
        try:
            root = get_project_root(args.project_root or os.getcwd())
            approval_config = load_config(args.config_path, root)
            approval_cfg = get_approval_config(approval_config)
        except Exception as exc:
            safe_print(f"{Fore.RED}Error: {exc}{Style.RESET_ALL}")
            return 1

        directory = approval_cfg.get("directory")

        for target, decision in ((args.approve, STATUS_APPROVED), (args.reject, STATUS_REJECTED)):
            if not target:
                continue
            record = resolve_approval(
                root, target, decision, note=args.note, directory=directory
            )
            if record is None:
                safe_print(f"{Fore.RED}No approval matching '{target}'.{Style.RESET_ALL}")
                safe_print("Use --approvals to list what is waiting.")
                return 1
            if record.get("already_decided"):
                safe_print(
                    f"{Fore.YELLOW}That gate was already {record.get('status')} "
                    f"by {record.get('decided_by')}.{Style.RESET_ALL}"
                )
                return 1
            color = Fore.GREEN if decision == STATUS_APPROVED else Fore.RED
            safe_print(f"{color}{decision}{Style.RESET_ALL}: {record.get('subject')}")
            if record.get("goal_id"):
                safe_print(
                    f"{Fore.LIGHTBLACK_EX}Continue with: python -m orchestrator "
                    f"--resume-goal {record['goal_id']}{Style.RESET_ALL}"
                )

            # Pressing Ready to Merge is what crosses the network (Roadmap Phase 6). Only
            # here, only on approval, and only when the project asked for it: `delivery` is
            # off by default and `on_approve` can turn just this trigger off while leaving
            # `--deliver` available.
            delivery_cfg = get_delivery_config(approval_config)
            if (
                decision == STATUS_APPROVED
                and record.get("gate") == GATE_BEFORE_MERGE
                and delivery_cfg.get("enabled")
                and delivery_cfg.get("on_approve")
            ):
                board = read_board(root, approval_config)
                card_id = (
                    f"{record.get('goal_id')}/{record.get('task_id')}"
                    if record.get("task_id")
                    else str(record.get("run_id") or "")
                )
                matches = resolve_card(board, card_id)
                if matches:
                    deliver_card(root, approval_config, matches[0])
                else:
                    safe_print(
                        f"{Fore.YELLOW}Approved, but that work is no longer on the board, "
                        f"so nothing was pushed.{Style.RESET_ALL}"
                    )
            elif decision == STATUS_APPROVED and record.get("gate") == GATE_BEFORE_MERGE:
                if not delivery_cfg.get("enabled"):
                    safe_print(
                        f"{Fore.LIGHTBLACK_EX}Nothing was pushed: delivery.enabled is false. "
                        f"The work is on {record.get('branch') or 'its branch'}."
                        f"{Style.RESET_ALL}"
                    )
                else:
                    safe_print(
                        f"{Fore.LIGHTBLACK_EX}Publish it with: python -m orchestrator "
                        f"--deliver {record.get('task_id') or record.get('run_id')}"
                        f"{Style.RESET_ALL}"
                    )
            return 0

        wanted = str(args.approvals or "pending").lower()
        status = None if wanted == "all" else wanted
        records = list_approvals(root, status=status, directory=directory)
        if not records:
            safe_print(
                f"{Fore.GREEN}Nothing is waiting for you.{Style.RESET_ALL}"
                if wanted == "pending"
                else f"{Fore.YELLOW}No {wanted} approvals recorded.{Style.RESET_ALL}"
            )
            return 0

        if args.as_json:
            safe_print(json.dumps(records, indent=2, default=str))
            return 0

        safe_print(f"\n{Fore.CYAN}{Style.BRIGHT}APPROVALS{Style.RESET_ALL} ({len(records)})\n")
        for record in records:
            color = {
                STATUS_PENDING: Fore.MAGENTA,
                STATUS_APPROVED: Fore.GREEN,
                STATUS_REJECTED: Fore.RED,
            }.get(str(record.get("status")), Fore.WHITE)
            safe_print(f"  {color}{describe_approval(record)}{Style.RESET_ALL}")
            if record.get("detail"):
                safe_print(f"      {Fore.LIGHTBLACK_EX}{record['detail']}{Style.RESET_ALL}")
        safe_print("")
        if status == STATUS_PENDING:
            safe_print(
                f"{Fore.LIGHTBLACK_EX}Answer one with: python -m orchestrator "
                f"--approve <id>  |  --reject <id> --note \"why\"{Style.RESET_ALL}\n"
            )
        return 0

    # --- Goal browsing (Roadmap Phases 2-3) --------------------------------
    if args.goals is not None:
        try:
            root = get_project_root(args.project_root or os.getcwd())
            delegation_cfg = get_delegation_config(load_config(args.config_path, root))
        except Exception as exc:
            safe_print(f"{Fore.RED}Error: {exc}{Style.RESET_ALL}")
            return 1

        entries = list_goals(root, limit=args.goals, directory=delegation_cfg.get("goals_directory"))
        if not entries:
            safe_print(f"{Fore.YELLOW}No delegated goals recorded.{Style.RESET_ALL}")
            return 0

        safe_print(f"\n{Fore.CYAN}{Style.BRIGHT}DELEGATED GOALS{Style.RESET_ALL} ({len(entries)})\n")
        safe_print(f"  {'GOAL ID':<26} {'STATUS':<18} {'TASKS':>6}  GOAL")
        safe_print(f"  {'-'*26} {'-'*18} {'-'*6}  {'-'*30}")
        for entry in entries:
            status = str(entry.get("status") or "unknown")
            summary = entry.get("summary") or {}
            tasks = f"{summary.get('done', 0)}/{summary.get('task_count', 0)}"
            text = str(entry.get("goal") or "")
            if len(text) > 40:
                text = text[:39] + "…"
            color = Fore.GREEN if goal_is_successful(status) else (
                Fore.YELLOW if status == "running" else Fore.RED
            )
            safe_print(
                f"  {entry.get('goal_id',''):<26} {color}{status:<18}{Style.RESET_ALL} "
                f"{tasks:>6}  {text}"
            )
        safe_print("")
        return 0

    if args.show_goal:
        try:
            root = get_project_root(args.project_root or os.getcwd())
            delegation_cfg = get_delegation_config(load_config(args.config_path, root))
        except Exception as exc:
            safe_print(f"{Fore.RED}Error: {exc}{Style.RESET_ALL}")
            return 1

        directory = delegation_cfg.get("goals_directory")
        resolved = resolve_goal_id(root, args.show_goal, directory=directory)
        goal = load_goal(root, resolved, directory=directory) if resolved else None
        if not goal:
            safe_print(f"{Fore.RED}No goal matching '{args.show_goal}'.{Style.RESET_ALL}")
            safe_print("Use --goals to list delegated goals.")
            return 1

        plan = goal.get("plan") or {}
        summary = derive_goal_summary(plan.get("tasks") or [], task_outcomes(goal))
        summary["goal"] = goal.get("goal")
        summary["base_ref"] = goal.get("base_ref") or "HEAD"
        summary["collisions"] = [
            {"task_ids": e.get("task_ids") or [], "paths": e.get("paths") or []}
            for e in goal.get("events") or []
            if e.get("event") == "collision"
        ]

        if args.as_json:
            safe_print(json.dumps(summary, indent=2, default=str))
            return 0

        safe_print(f"\n{Fore.CYAN}{Style.BRIGHT}GOAL {goal.get('goal_id')}{Style.RESET_ALL}")
        safe_print(f"  {Fore.LIGHTBLACK_EX}Directory:{Style.RESET_ALL} {goal.get('goal_dir')}")
        safe_print(f"  {Fore.LIGHTBLACK_EX}Started:{Style.RESET_ALL}   {goal.get('started_at')}")
        safe_print(f"  {Fore.LIGHTBLACK_EX}Finished:{Style.RESET_ALL}  {goal.get('finished_at') or '(incomplete)'}")
        safe_print(f"  {Fore.LIGHTBLACK_EX}Status:{Style.RESET_ALL}    {summary['status']} - {describe_goal_status(summary['status'])}")
        safe_print("")
        safe_print(format_goal_summary(summary))
        safe_print("")
        safe_print(f"{Fore.LIGHTBLACK_EX}Inspect one task's session with --show-run <run id>.{Style.RESET_ALL}\n")
        return 0

    # --- Aggregate analysis over the run store (Tier 1 #4) -----------------
    if args.stats is not None:
        try:
            root = get_project_root(args.project_root or os.getcwd())
            store_cfg = get_run_store_config(load_config(args.config_path, root))
        except Exception as exc:
            safe_print(f"{Fore.RED}Error: {exc}{Style.RESET_ALL}")
            return 1

        runs, unreadable = collect_runs(
            root, limit=max(0, int(args.stats or 0)), directory=store_cfg.get("directory")
        )
        report = compute_stats(runs, unreadable=unreadable)
        if args.as_json:
            safe_print(json.dumps(report, indent=2, default=str))
        else:
            safe_print(f"\n{Fore.CYAN}{Style.BRIGHT}RUN STATISTICS{Style.RESET_ALL}\n")
            safe_print(format_stats(report))
            safe_print("")
        return 0

    # --- What the planner remembers (Roadmap 8.2) --------------------------
    if args.memory is not None:
        try:
            root = get_project_root(args.project_root or os.getcwd())
            memory_config = load_config(args.config_path, root)
        except Exception as exc:
            safe_print(f"{Fore.RED}Error: {exc}{Style.RESET_ALL}")
            return 1

        from orchestrator.memory import (
            MAX_GOALS,
            format_memory,
            project_memory,
            summarise,
        )

        planning_cfg = get_planning_config(memory_config)
        memory_cfg = planning_cfg.get("memory") or {}
        window = int(args.memory) if args.memory else int(memory_cfg.get("max_goals", MAX_GOALS))
        memory = project_memory(root, memory_config, max_goals=window)

        if args.as_json:
            safe_print(json.dumps(memory, indent=2, default=str))
            return 0

        # `--plan-only` here means "show me the block, not the report": the exact text the
        # decomposer's prompt will carry, so a person can read what the planner is told.
        if args.plan_only:
            block = summarise(memory, budget_chars=int(memory_cfg.get("budget_chars", 2400)))
            safe_print(block or "(nothing recorded yet: every goal is decomposed cold)")
            return 0

        safe_print(f"\n{Fore.CYAN}{Style.BRIGHT}WHAT THE PLANNER REMEMBERS{Style.RESET_ALL}\n")
        if not memory_cfg.get("enabled", True):
            safe_print(
                f"{Fore.YELLOW}planning.memory.enabled is false, so none of this reaches "
                f"the decomposer.{Style.RESET_ALL}"
            )
        safe_print(format_memory(memory))
        safe_print("")
        return 0

    # --- The archive: retention over the run store itself -------------------
    #
    # `--prune-runs` below deletes branches. This does not delete anything: it moves runs
    # that were never really run out of the dataset every projection reads, and
    # `--restore-runs` moves them back. The store is evidence, and evidence is corrected
    # by being set aside with a reason attached, not by being destroyed.
    if args.archived:
        try:
            root = get_project_root(args.project_root or os.getcwd())
        except Exception as exc:
            safe_print(f"{Fore.RED}Error: {exc}{Style.RESET_ALL}")
            return 1
        safe_print(f"\n{Fore.CYAN}{Style.BRIGHT}ARCHIVED RUNS{Style.RESET_ALL}\n")
        safe_print(format_archived_list(list_archived(root)))
        safe_print("")
        return 0

    if args.restore_runs is not None:
        try:
            root = get_project_root(args.project_root or os.getcwd())
            config = load_config(args.config_path, root)
        except Exception as exc:
            safe_print(f"{Fore.RED}Error: {exc}{Style.RESET_ALL}")
            return 1

        runs_dir = get_run_store_config(config).get("directory")
        plan = plan_restore(root, run_ids=args.restore_runs, runs_directory=runs_dir)
        if not plan.get("available"):
            safe_print(f"{Fore.YELLOW}Cannot restore: {plan.get('reason')}{Style.RESET_ALL}\n")
            return 1

        selected = plan.get("selected") or []
        conflicts = plan.get("conflicts") or []
        safe_print(f"\n{Fore.CYAN}{Style.BRIGHT}RESTORE ARCHIVED RUNS{Style.RESET_ALL}\n")
        safe_print(f"  {len(selected)} to restore into {plan.get('store_dir')}")
        for c in conflicts:
            safe_print(f"  {Fore.YELLOW}{c['run_id']}: {c['error']}{Style.RESET_ALL}")
        if not selected:
            safe_print(f"\n{Fore.GREEN}Nothing to restore.{Style.RESET_ALL}\n")
            return 0 if not conflicts else 1
        if args.dry_run:
            safe_print(f"\n{Fore.LIGHTBLACK_EX}--dry-run: nothing was moved.{Style.RESET_ALL}\n")
            return 0

        # Restoring is reversible, so this asks rather than refuses - but it still asks,
        # because what changes underneath is every number this project reports about itself,
        # the same reason --archive-runs asks before it moves anything.
        if not args.assume_yes:
            if not sys.stdin or not sys.stdin.isatty():
                safe_print(
                    f"{Fore.YELLOW}Refusing to move runs without confirmation. "
                    f"Re-run with --yes (or --dry-run to preview).{Style.RESET_ALL}\n"
                )
                return 1
            answer = input(f"Restore {len(selected)} run(s)? [y/N] ").strip().lower()
            if answer not in ("y", "yes"):
                safe_print("Aborted; nothing was moved.\n")
                return 0

        result = execute_restore(plan)
        safe_print("")
        safe_print(format_restore_result(result))
        safe_print("")
        return 0 if not result.get("failed") and not conflicts else 1

    if args.archive_runs:
        try:
            root = get_project_root(args.project_root or os.getcwd())
            config = load_config(args.config_path, root)
        except Exception as exc:
            safe_print(f"{Fore.RED}Error: {exc}{Style.RESET_ALL}")
            return 1

        runs_dir = get_run_store_config(config).get("directory")
        plan = plan_archive(root, matching=args.matching, runs_directory=runs_dir)
        safe_print(f"\n{Fore.CYAN}{Style.BRIGHT}ARCHIVE RUN RECORDS{Style.RESET_ALL}\n")
        safe_print(format_archive_plan(plan, show_kept=False))
        safe_print("")

        if not plan.get("available"):
            return 1
        if not plan.get("selected"):
            safe_print(f"{Fore.GREEN}Nothing to archive.{Style.RESET_ALL}\n")
            return 0
        if args.dry_run:
            safe_print(f"{Fore.LIGHTBLACK_EX}--dry-run: nothing was moved.{Style.RESET_ALL}\n")
            return 0

        # Archiving is reversible, so this asks rather than refuses - but it still asks,
        # because what changes underneath is every number this project reports about itself.
        if not args.assume_yes:
            if not sys.stdin or not sys.stdin.isatty():
                safe_print(
                    f"{Fore.YELLOW}Refusing to move runs without confirmation. "
                    f"Re-run with --yes (or --dry-run to preview).{Style.RESET_ALL}\n"
                )
                return 1
            answer = (
                input(f"Archive {len(plan['selected'])} run(s)? [y/N] ").strip().lower()
            )
            if answer not in ("y", "yes"):
                safe_print("Aborted; nothing was moved.\n")
                return 0

        result = execute_archive(root, plan)
        safe_print(format_archive_result(result))
        safe_print("")
        return 0 if not result.get("failed") else 1

    # --- Retention sweep over run branches (Tier 0 #3) ---------------------
    if args.prune_runs:
        try:
            root = get_project_root(args.project_root or os.getcwd())
            config = load_config(args.config_path, root)
        except Exception as exc:
            safe_print(f"{Fore.RED}Error: {exc}{Style.RESET_ALL}")
            return 1

        try:
            older_than = parse_age(args.older_than)
            merged_older_than = parse_age(args.merged_older_than)
        except ValueError as exc:
            safe_print(f"{Fore.RED}{exc}{Style.RESET_ALL}")
            return 1

        deliveries_dir = get_delivery_config(config).get("directory")
        plan = plan_prune(
            root,
            older_than_seconds=older_than,
            keep_failed=bool(args.keep_failed),
            runs_directory=get_run_store_config(config).get("directory"),
            merged_older_than_seconds=merged_older_than,
            deliveries_directory=deliveries_dir,
        )
        safe_print(f"\n{Fore.CYAN}{Style.BRIGHT}PRUNE RUN BRANCHES{Style.RESET_ALL}\n")
        safe_print(format_prune_plan(plan))
        safe_print("")

        # Empty directories git no longer tracks (a worktree Windows would not let git delete).
        # Removing an empty directory loses nothing, so this needs no confirmation - only
        # `--dry-run` holds it back.
        from orchestrator.workspace import remove_ghost_worktree_dirs

        ghosts = remove_ghost_worktree_dirs(
            root, get_workspace_config(config).get("directory"), dry_run=bool(args.dry_run)
        )
        if ghosts["removed"]:
            verb = "Would remove" if args.dry_run else "Removed"
            safe_print(
                f"{verb} {len(ghosts['removed'])} empty worktree director"
                f"{'y' if len(ghosts['removed']) == 1 else 'ies'} git no longer tracks."
            )
        for kept in ghosts["kept"]:
            safe_print(f"{Fore.YELLOW}Left alone (not empty, not a worktree):{Style.RESET_ALL} {kept}")
        if ghosts["removed"] or ghosts["kept"]:
            safe_print("")

        if not plan.get("available"):
            return 1
        if not plan.get("prunable"):
            safe_print(f"{Fore.GREEN}Nothing to prune.{Style.RESET_ALL}\n")
            return 0
        if args.dry_run:
            safe_print(f"{Fore.LIGHTBLACK_EX}--dry-run: nothing was deleted.{Style.RESET_ALL}\n")
            return 0

        # Deleting a run branch destroys the only copy of that run's work, so
        # confirm unless the user has already said yes on the command line.
        if not args.assume_yes:
            if not sys.stdin or not sys.stdin.isatty():
                safe_print(
                    f"{Fore.YELLOW}Refusing to delete without confirmation. "
                    f"Re-run with --yes (or --dry-run to preview).{Style.RESET_ALL}\n"
                )
                return 1
            answer = input(f"Delete {len(plan['prunable'])} branch(es)? [y/N] ").strip().lower()
            if answer not in ("y", "yes"):
                safe_print("Aborted; nothing was deleted.\n")
                return 0

        result = execute_prune(root, plan, deliveries_directory=deliveries_dir)
        safe_print(format_prune_result(result))
        safe_print("")
        return 0 if not result.get("failed") else 1

    # --- Resume a delegated goal (Roadmap Phases 2-5) ----------------------
    if args.resume_goal:
        try:
            root = get_project_root(args.project_root or os.getcwd())
            goal_config = load_config(args.config_path, root)
        except Exception as exc:
            safe_print(f"{Fore.RED}Error loading configuration: {exc}{Style.RESET_ALL}")
            return 1

        directory = get_delegation_config(goal_config).get("goals_directory")
        resolved = resolve_goal_id(root, args.resume_goal, directory=directory)
        goal = load_goal(root, resolved, directory=directory) if resolved else None
        if not goal:
            safe_print(f"{Fore.RED}No goal matching '{args.resume_goal}'.{Style.RESET_ALL}")
            safe_print("Use --goals to list delegated goals.")
            return 1

        plan = goal.get("plan") or {}
        if not plan.get("tasks"):
            safe_print(
                f"{Fore.RED}Goal {goal.get('goal_id')} recorded no plan, so there is nothing "
                f"to resume.{Style.RESET_ALL}"
            )
            return 1

        session_graph = build_graph(goal_config)
        store = GoalStore.from_dir(str(goal.get("goal_dir")))
        summary = run_goal(
            goal=str(goal.get("goal") or ""),
            plan=plan,
            project_root=root,
            config=goal_config,
            session_runner=lambda state: session_graph.invoke(state),
            max_parallel=args.parallel,
            goal_store=store,
            resume_outcomes=task_outcomes(goal),
        )

        if args.as_json:
            safe_print(json.dumps(summary, indent=2, default=str))
        else:
            safe_print("")
            safe_print(format_goal_summary(summary))
            safe_print("")
        return 0 if goal_is_successful(summary.get("status", "")) else 1

    # --- Resume a recorded run (Tier 1 #4) ---------------------------------
    resumed_state = None
    if args.resume:
        try:
            root = get_project_root(args.project_root or os.getcwd())
            config = load_config(args.config_path, root)
        except Exception as exc:
            safe_print(f"{Fore.RED}Error loading configuration: {exc}{Style.RESET_ALL}")
            return 1

        store_cfg = get_run_store_config(config)
        run = load_resumable_run(root, args.resume, directory=store_cfg.get("directory"))
        if not run:
            safe_print(f"{Fore.RED}No run matching '{args.resume}'.{Style.RESET_ALL}")
            safe_print("Use --runs to list recorded runs.")
            return 1

        resumed_state = reconstruct_state(run, project_root=root)
        if not resumed_state.get("task"):
            safe_print(
                f"{Fore.RED}Run {run.get('run_id')} recorded no task, so there is "
                f"nothing to resume.{Style.RESET_ALL}"
            )
            return 1

        prepare_resumed_workspace(
            resumed_state, directory=get_workspace_config(config).get("directory")
        )
        replayed = replayable_roles(resumed_state)
        default_tracer.log_run_resumed(
            str(run.get("run_id")),
            completed_roles=replayed,
            repair_attempts=int(resumed_state.get("repair_attempts") or 0),
        )
        workspace = resumed_state.get("workspace")
        if isinstance(workspace, dict) and not workspace.get("isolated") and workspace.get("reason"):
            safe_print(
                f"{Fore.YELLOW}Workspace could not be restored:{Style.RESET_ALL} "
                f"{workspace['reason']}\n"
            )
        try:
            store = RunStore.from_dir(
                str(run.get("run_dir")),
                max_output_chars=int(store_cfg.get("max_output_chars") or 0),
            )
            store.record_run_resumed(
                replayed_roles=replayed,
                repair_attempts=int(resumed_state.get("repair_attempts") or 0),
            )
        except Exception:
            pass

    if not args.task and resumed_state is None:
        safe_print(f"{Fore.RED}Error: No task provided.{Style.RESET_ALL}")
        safe_print(
            "Usage: python -m orchestrator \"<task>\"\n"
            "       python -m orchestrator \"<goal>\" --plan-only\n"
            "       python -m orchestrator \"<goal>\" --delegate [--parallel 3]\n"
            "       python -m orchestrator --board | --serve | --approvals\n"
            "       python -m orchestrator --resume <id>\n"
            "       python -m orchestrator --list-models | --doctor | --runs | --show-run <id>\n"
            "       python -m orchestrator --stats | --prune-runs [--older-than 30d]"
        )
        return 1

    task_str = " ".join(args.task) if args.task else str(resumed_state.get("task") or "")

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
    if resumed_state is not None:
        # The resumed run's facts, with the CLI's task/root taking precedence.
        initial_state = {**resumed_state, **{k: v for k, v in initial_state.items() if v}}
        initial_state["task"] = task_str
        initial_state["agent_results"] = resumed_state.get("agent_results") or []
    if args.max_repair_attempts is not None:
        initial_state["max_repair_attempts"] = args.max_repair_attempts
    if args.visible_terminals is not None:
        initial_state["visible_terminals"] = args.visible_terminals
    if args.terminal_type is not None:
        initial_state["terminal_type"] = args.terminal_type
    if args.agent_execution_mode is not None:
        initial_state["agent_execution_mode"] = args.agent_execution_mode
    if args.close_terminal_on_completion is not None:
        initial_state["close_terminal_on_completion"] = args.close_terminal_on_completion
    if args.no_preflight:
        initial_state["skip_preflight"] = True
    if args.no_run_store:
        initial_state["run_store_enabled"] = False
    overrides = (
        args.isolation is not None
        or args.keep_worktree is not None
        or args.max_total_tokens is not None
        or args.max_duration_seconds is not None
        or args.acceptance_command is not None
        or args.no_acceptance
    )
    if overrides:
        # A CLI flag wins over the matching orchestrator.yaml setting.
        try:
            override = load_config(args.config_path, project_root)
            override["workspace"] = dict(override.get("workspace") or {})
            if args.isolation is not None:
                override["workspace"]["isolation"] = args.isolation
            if args.keep_worktree is not None:
                override["workspace"]["keep_worktree"] = bool(args.keep_worktree)
            override["budget"] = dict(override.get("budget") or {})
            if args.max_total_tokens is not None:
                override["budget"]["max_total_tokens"] = max(0, args.max_total_tokens)
            if args.max_duration_seconds is not None:
                override["budget"]["max_duration_seconds"] = max(0, args.max_duration_seconds)
            if args.acceptance_command is not None or args.no_acceptance:
                override["verification"] = dict(override.get("verification") or {})
                acceptance = dict(override["verification"].get("acceptance") or {})
                if args.no_acceptance:
                    acceptance["command"] = ""
                elif args.acceptance_command is not None:
                    acceptance["command"] = args.acceptance_command
                override["verification"]["acceptance"] = acceptance
            initial_state["config"] = override
        except Exception as exc:
            safe_print(f"{Fore.RED}Error loading configuration: {exc}{Style.RESET_ALL}")
            return 1

    # --- Delegate: decompose, then run every task (Roadmap Phases 2-3) -----
    if args.delegate and not args.plan_only:
        try:
            run_config = initial_state.get("config") or load_config(args.config_path, project_root)
        except Exception as exc:
            safe_print(f"{Fore.RED}Error loading configuration: {exc}{Style.RESET_ALL}")
            return 1

        # The goal is opened before planning, so the decomposition is recorded as part of the
        # goal it produced rather than as a stray session of its own.
        goal_store = None
        if not args.no_run_store:
            goal_store = open_goal(
                project_root,
                task_str,
                base_ref=head_commit(project_root),
                directory=get_delegation_config(run_config).get("goals_directory"),
            )

        # Planning only reads, so it does not take a branch of its own.
        plan_config = dict(run_config)
        plan_config["workspace"] = dict(plan_config.get("workspace") or {})
        plan_config["workspace"]["isolation"] = "none"
        plan_state = dict(initial_state)
        plan_state["config"] = plan_config
        plan_state["plan_only"] = True
        if goal_store is not None:
            plan_state["goal_id"] = goal_store.goal_id

        plan_result = build_plan_graph(plan_config).invoke(plan_state)
        if plan_result.get("error"):
            safe_print(f"{Fore.RED}{Style.BRIGHT}Planning error:{Style.RESET_ALL} {plan_result['error']}")
            return 1

        plan = plan_result.get("task_plan") or {}
        if not plan_is_usable(plan):
            safe_print(f"\n{Fore.RED}Could not delegate: {plan.get('error')}{Style.RESET_ALL}")
            safe_print("Run with --plan-only to inspect the decomposer's output.\n")
            return 1

        safe_print(f"\n{Fore.CYAN}{Style.BRIGHT}TASK PLAN{Style.RESET_ALL}\n")
        safe_print(format_plan(plan))
        safe_print("")

        if goal_store is not None:
            goal_store.record_plan(plan)

        # Each task runs the ordinary pipeline, unchanged. The scheduler only decides which
        # task runs when, and what its workspace starts from.
        session_graph = build_graph(run_config)

        def session_runner(state):
            return session_graph.invoke(state)

        # Whatever the user asked for on the command line applies to every session the goal
        # spawns, not only to the planning pass.
        session_defaults = {
            key: initial_state[key]
            for key in (
                "config_path",
                "skip_preflight",
                "max_repair_attempts",
                "visible_terminals",
                "terminal_type",
                "agent_execution_mode",
                "close_terminal_on_completion",
                "run_store_enabled",
            )
            if key in initial_state
        }

        summary = run_goal(
            goal=task_str,
            plan=plan,
            project_root=project_root,
            config=run_config,
            session_runner=session_runner,
            max_parallel=args.parallel,
            goal_store=goal_store,
            store_enabled=not args.no_run_store,
            session_defaults=session_defaults,
        )

        if args.as_json:
            safe_print(json.dumps(summary, indent=2, default=str))
        else:
            safe_print("")
            safe_print(format_goal_summary(summary))
            safe_print("")
            if summary.get("goal_dir"):
                safe_print(
                    f"{Fore.LIGHTBLACK_EX}Goal recorded at: {summary['goal_dir']}{Style.RESET_ALL}"
                )
            safe_print(
                f"{Fore.LIGHTBLACK_EX}{describe_goal_status(summary['status'])} "
                f"Nothing was merged - each task's work is on its own branch."
                f"{Style.RESET_ALL}\n"
            )
        return 0 if goal_is_successful(summary.get("status", "")) else 1

    # --- Decomposition only (Roadmap Phase 1) ------------------------------
    if args.plan_only:
        try:
            plan_config = initial_state.get("config") or load_config(args.config_path, project_root)
        except Exception as exc:
            safe_print(f"{Fore.RED}Error loading configuration: {exc}{Style.RESET_ALL}")
            return 1

        # Planning only reads. Creating a branch and a worktree for a pass that changes
        # nothing would leave litter for every plan the user ever asks for.
        plan_config = dict(plan_config)
        plan_config["workspace"] = dict(plan_config.get("workspace") or {})
        plan_config["workspace"]["isolation"] = "none"
        initial_state["config"] = plan_config
        initial_state["plan_only"] = True

        plan_result = build_plan_graph(plan_config).invoke(initial_state)

        if plan_result.get("error"):
            safe_print(f"{Fore.RED}{Style.BRIGHT}Planning error:{Style.RESET_ALL} {plan_result['error']}")
            return 1

        plan = plan_result.get("task_plan") or {}
        if args.as_json:
            safe_print(json.dumps(plan, indent=2, default=str))
        else:
            safe_print(f"\n{Fore.CYAN}{Style.BRIGHT}TASK PLAN{Style.RESET_ALL}\n")
            safe_print(format_plan(plan))
            safe_print("")
            decomposer_res = get_agent_result(plan_result.get("agent_results") or [], role="decomposer")
            if decomposer_res and decomposer_res.get("output"):
                safe_print(f"{Fore.LIGHTBLACK_EX}--- decomposer reasoning ---{Style.RESET_ALL}")
                safe_print(decomposer_res["output"])
                safe_print("")
            if plan_result.get("run_dir"):
                safe_print(
                    f"{Fore.LIGHTBLACK_EX}Plan recorded at: {plan_result['run_dir']}"
                    f"{Style.RESET_ALL}"
                )
            safe_print(
                f"{Fore.LIGHTBLACK_EX}Nothing was implemented. Run each task with "
                f"python -m orchestrator \"<task>\".{Style.RESET_ALL}\n"
            )

        return 0 if plan.get("tasks") and not plan.get("error") else 1

    # The topology depends on the configuration - an acceptance gate adds a node, an ensemble
    # adds members - so it is built from the config resolved for THIS run, not from the one
    # that happened to be on disk when the module was imported.
    try:
        run_config = initial_state.get("config") or load_config(args.config_path, project_root)
    except Exception as exc:
        safe_print(f"{Fore.RED}Error loading configuration: {exc}{Style.RESET_ALL}")
        return 1
    initial_state["config"] = run_config

    result = build_graph(run_config).invoke(initial_state)

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
        # A run that stopped on an error has usually still paid for something - every attempt
        # of the execution that exhausted its retries, at least - so the spend is shown here as
        # it is for any other ending, not left to be found in the run store.
        if result.get("agent_results"):
            safe_print("")
            safe_print(
                format_summary_table(
                    agent_results=result.get("agent_results") or [],
                    verification_verdict=None,
                    repair_attempts=int(result.get("repair_attempts") or 0),
                )
            )
            safe_print("")
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
    # What that verifier said, and what the run concluded, are two different things: an
    # objective gate can overrule a PASS (Phase 0), and a quorum resolves several verdicts
    # into one. The agent's badge shows its own answer; everything summarising the run shows
    # the resolved one.
    agent_verdict = (verifier_res.get("verdict") if verifier_res else None) or "UNKNOWN"
    verdict = result.get("verification_verdict") or agent_verdict
    v_color = verdict_color(verdict)
    agent_v_color = verdict_color(agent_verdict)
    verdict_badge = f" [Verdict: {agent_v_color}{agent_verdict}{Style.RESET_ALL}{Fore.YELLOW}{Style.BRIGHT}]"

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

    # Phase 0 - the objective check, and whether it overruled a verifier.
    gate = (result.get("summary") or {}).get("acceptance")
    if gate and not gate.get("skipped"):
        gate_color = Fore.GREEN if gate.get("ok") else Fore.RED
        safe_print(f"{gate_color}{describe_check(gate)}{Style.RESET_ALL}")
        if (result.get("summary") or {}).get("acceptance_overrode_pass"):
            safe_print(
                f"{Fore.RED}The verifier returned PASS, but the acceptance command failed, "
                f"so the verdict is FAIL.{Style.RESET_ALL}"
            )
        safe_print("")

    # Tier 2 #11 - what the run spent, and whether that is why it stopped.
    budget_state = result.get("budget_state") or {}
    budget_reason = result.get("budget_exhausted_reason")
    if budget_reason:
        safe_print(f"{Fore.YELLOW}{Style.BRIGHT}{'='*40}{Style.RESET_ALL}")
        safe_print(f"{Fore.YELLOW}{Style.BRIGHT}   BUDGET REACHED{Style.RESET_ALL}")
        safe_print(f"{Fore.YELLOW}{Style.BRIGHT}{'='*40}{Style.RESET_ALL}\n")
        safe_print(budget_reason)
        safe_print(
            f"{Fore.LIGHTBLACK_EX}The run stopped escalating rather than failing: "
            f"repair attempts remained, but the ceiling did not.{Style.RESET_ALL}\n"
        )
    elif budget_state.get("max_total_tokens") or budget_state.get("max_duration_seconds"):
        safe_print(f"{Fore.LIGHTBLACK_EX}{describe_budget(budget_state)}{Style.RESET_ALL}\n")

    # Status is derived from facts, never read from a value a node remembered
    # to write (Tier 1 #5). `result["status"]` is finalize_node's derivation;
    # recomputing here keeps the CLI correct even for a partial state.
    status_val = result.get("status") or derive_status(result)
    if status_val == "blocked":
        status_color = Fore.MAGENTA
    elif status_val == "budget_exhausted":
        status_color = Fore.YELLOW
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
            retention = ws_summary.get("retention") or {}
            if retention.get("branch_deleted"):
                safe_print(
                    f"  {Fore.LIGHTBLACK_EX}Cleanup:{Style.RESET_ALL}  worktree removed and "
                    f"branch deleted (the run changed nothing)"
                )
            elif retention.get("removed"):
                safe_print(
                    f"  {Fore.LIGHTBLACK_EX}Cleanup:{Style.RESET_ALL}  worktree removed; "
                    f"restore it with git worktree add {workspace.get('path')} {workspace.get('branch')}"
                )
            elif retention.get("retained_reason"):
                safe_print(
                    f"  {Fore.LIGHTBLACK_EX}Cleanup:{Style.RESET_ALL}  worktree kept - "
                    f"{retention['retained_reason']}"
                )
            base = workspace.get("base_branch") or "HEAD"
            if not retention.get("branch_deleted"):
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

