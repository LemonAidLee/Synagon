"""LangGraph orchestration graph connecting Antigravity CLI and Claude Code CLI with safe project context and structured agent roles."""

import time
from operator import add
from typing import Annotated, Any, Callable, Dict, List, Optional, Tuple, TypedDict

from langgraph.graph import StateGraph, START, END

from orchestrator.agents.antigravity import run_antigravity
from orchestrator.agents.claude_code import run_claude_code
from orchestrator.agents.opencode import run_opencode
from orchestrator.agents.verifier import (
    VERDICT_BLOCKED,
    VERDICT_PASS,
    extract_human_action,
    is_repairable,
    parse_verdict,
)
from orchestrator.prompts import (
    build_implementer_prompt,
    build_planner_prompt,
    build_researcher_prompt,
    build_verifier_prompt,
    build_repair_prompt,
)
from orchestrator.config import (
    OrchestratorConfig,
    load_config,
    get_agent_config,
    get_role_responsibility,
    get_consensus_policy,
    get_max_repair_attempts,
    get_visible_terminals,
    get_execution_config,
    get_preflight_config,
    get_run_store_config,
    get_skill_config,
    get_workspace_config,
)
from orchestrator.context import collect_project_context, get_project_root
from orchestrator.launcher import get_terminal_title
from orchestrator.preflight import (
    PreflightReport,
    format_preflight_report,
    run_preflight,
    skipped_report,
)
from orchestrator.skills.types import SkillInfo
from orchestrator.skills.discovery import discover_skills
from orchestrator.skills.registry import format_skill_manifest
from orchestrator.status import (
    derive_blocked_reason,
    derive_status,
    derive_summary,
    derive_verdict,
    has_error,
    latest_verification_group,
    resolve_consensus,
)
from orchestrator.store import RunStore, generate_run_id, open_run
from orchestrator.workspace import (
    WorkspaceInfo,
    commit_worktree,
    create_run_worktree,
    describe_workspace,
    summarize_worktree,
)
from orchestrator.tracer import default_tracer
from orchestrator.types import (
    AgentResult,
    VerificationRecord,
    create_agent_result,
    get_agent_result,
    get_latest_role_outputs,
    unavailable_token_usage,
)


class OrchestratorState(TypedDict, total=False):
    """Shared state schema for the orchestrator workflow.

    Everything here except `status` is a **durable fact** — something an agent or
    the environment actually produced. `status` is the one derived field, and it
    is written exactly once, by `finalize_node`, from those facts. No other node
    stores a status; see `orchestrator.status.derive_status`.
    """
    task: str
    message: Optional[str]
    project_root: Optional[str]
    project_context: Optional[str]
    config_path: Optional[str]
    config: Optional[OrchestratorConfig]
    skills: Optional[List[SkillInfo]]
    skill_manifest: Optional[str]
    agent_results: Annotated[List[AgentResult], add]
    verification_verdict: Optional[str]  # "PASS", "FAIL", "BLOCKED", or "UNKNOWN"
    verification_history: Annotated[List[VerificationRecord], add]
    repair_attempts: int
    max_repair_attempts: int
    visible_terminals: Optional[bool]
    terminal_type: Optional[str]
    pause_on_completion: Optional[float]
    agent_execution_mode: Optional[str]
    # Tier 1 #6 - preflight
    preflight: Optional[PreflightReport]
    skip_preflight: Optional[bool]
    # Tier 1 #4 - durable run store
    run_id: Optional[str]
    run_dir: Optional[str]
    run_store_enabled: Optional[bool]
    run_store_max_output_chars: Optional[int]
    # Tier 1 #7 - blocked verdict
    blocked_reason: Optional[str]
    # Tier 0 #3 - git worktree isolation
    workspace: Optional[WorkspaceInfo]
    workspace_summary: Optional[dict]
    # Tier 2 #8 - verifier consensus policy
    consensus_policy: Optional[str]
    # Derived at read time; written only by finalize_node (Tier 1 #5)
    status: str
    summary: Optional[dict]
    error: Optional[str]


def _working_dir(state: OrchestratorState) -> Optional[str]:
    """Return the directory agents should execute in.

    When the run is isolated this is the git worktree, so agent edits never
    touch the user's checkout (Tier 0 #3). Otherwise it is the project root.
    """
    workspace = state.get("workspace")
    if isinstance(workspace, dict) and workspace.get("path"):
        return workspace["path"]
    return state.get("project_root")


def _get_store(state: OrchestratorState) -> Optional[RunStore]:
    """Rebuild the run store for the active run, or None when disabled.

    The store holds no open handles, so reconstructing it per node is cheap and
    keeps LangGraph state free of non-serializable objects.
    """
    run_dir = state.get("run_dir")
    if not run_dir:
        return None
    try:
        return RunStore.from_dir(
            run_dir,
            max_output_chars=int(state.get("run_store_max_output_chars") or 0),
        )
    except Exception:
        return None


def _prepare_workspace(
    config: OrchestratorConfig,
    project_root: str,
    run_id: Optional[str],
) -> WorkspaceInfo:
    """Resolve the workspace this run executes in (Tier 0 #3).

    Returns an un-isolated workspace pointing at the project root when isolation
    is switched off, or when git cannot provide it. This never raises.
    """
    ws_cfg = get_workspace_config(config)
    mode = ws_cfg.get("isolation", "auto")

    if mode == "none":
        return {
            "isolated": False,
            "path": project_root,
            "branch": None,
            "base_branch": None,
            "project_root": project_root,
            "reason": "workspace.isolation is 'none'",
        }

    # Isolation must not depend on the run store being enabled, so mint an id
    # for the worktree when there isn't one.
    identifier = run_id or generate_run_id()
    return create_run_worktree(project_root, identifier, directory=ws_cfg.get("directory"))


def context_node(state: OrchestratorState) -> OrchestratorState:
    """Node that collects safe project context and loads configuration before agent analysis."""
    default_tracer.log_context_start()
    try:
        root = get_project_root(state.get("project_root"))
        context = collect_project_context(root)
        config = state.get("config") or load_config(state.get("config_path"), root)
        max_repairs = state.get("max_repair_attempts")
        if max_repairs is None:
            max_repairs = get_max_repair_attempts(config)

        exec_cfg = get_execution_config(config)
        visible_terms = state.get("visible_terminals")
        if visible_terms is None:
            visible_terms = exec_cfg.get("visible_terminals", False)
        term_type = state.get("terminal_type") or exec_cfg.get("terminal_type", "auto")
        pause_secs = state.get("pause_on_completion")
        if pause_secs is None:
            pause_secs = exec_cfg.get("pause_on_completion", 1.5)
        agent_exec_mode = state.get("agent_execution_mode") or exec_cfg.get("agent_execution_mode", "auto")

        default_tracer.log_context_complete(root)

        skill_cfg = get_skill_config(config)
        discovered_skills = state.get("skills")
        manifest = state.get("skill_manifest")
        if discovered_skills is None:
            discovered_skills = discover_skills(
                project_root=root,
                search_paths=skill_cfg.get("search_paths"),
                enabled=skill_cfg.get("enabled", True),
            )
            manifest = format_skill_manifest(discovered_skills)
            skill_names = [s.get("name", "") for s in discovered_skills]
            default_tracer.log_skills_discovered(len(discovered_skills), skill_names)

        # Tier 1 #4 - open the durable run store and record the run's opening facts.
        store_cfg = get_run_store_config(config)
        store_enabled = state.get("run_store_enabled")
        if store_enabled is None:
            store_enabled = store_cfg.get("enabled", True)
        max_output_chars = int(store_cfg.get("max_output_chars", 0) or 0)

        run_id = state.get("run_id")
        run_dir = state.get("run_dir")
        if store_enabled and not run_dir:
            store = open_run(
                project_root=root,
                task=state.get("task") or state.get("message", ""),
                config=config,
                enabled=True,
                directory=store_cfg.get("directory"),
                max_output_chars=max_output_chars,
                max_repair_attempts=max_repairs,
            )
            if store is not None:
                run_id = store.run_id
                run_dir = store.run_dir
                store.record_context(root, len(context or ""))
                if discovered_skills is not None:
                    store.record_skills([s.get("name", "") for s in discovered_skills])
                default_tracer.log_run_store_opened(store.run_id, store.run_dir)
                if store.degraded:
                    default_tracer.log_run_store_degraded(store.degraded_reason or "unknown error")

        # Tier 0 #3 - isolate the run in its own git worktree when possible, so
        # the implementer and its repairs never write the user's checkout.
        workspace = state.get("workspace")
        if workspace is None:
            workspace = _prepare_workspace(config, root, run_id)
            if workspace.get("isolated"):
                default_tracer.log_workspace_isolated(
                    workspace.get("path", ""), workspace.get("branch", "")
                )
            else:
                default_tracer.log_workspace_not_isolated(workspace.get("reason") or "unknown")
            store = _get_store({"run_dir": run_dir, "run_store_max_output_chars": max_output_chars})
            if store is not None:
                store.record_event("workspace", workspace=workspace)

        ws_cfg = get_workspace_config(config)
        if ws_cfg.get("isolation") == "worktree" and not workspace.get("isolated"):
            # Isolation was demanded, not merely preferred. Refuse to run
            # unisolated rather than silently writing the user's checkout.
            return {
                "workspace": workspace,
                "error": (
                    "workspace.isolation is 'worktree' but the run could not be isolated: "
                    f"{workspace.get('reason')}"
                ),
            }

        # NOTE: `agent_results` and `verification_history` use `add` reducers, so
        # returning their existing contents would append a second copy. Seed them
        # only when they are genuinely empty.
        return {
            "project_root": root,
            "project_context": context,
            "config": config,
            "skills": discovered_skills,
            "skill_manifest": manifest,
            "repair_attempts": state.get("repair_attempts", 0),
            "max_repair_attempts": max_repairs,
            "visible_terminals": visible_terms,
            "terminal_type": term_type,
            "pause_on_completion": float(pause_secs),
            "agent_execution_mode": str(agent_exec_mode),
            "consensus_policy": state.get("consensus_policy") or get_consensus_policy(config),
            "workspace": workspace,
            "run_id": run_id,
            "run_dir": run_dir,
            "run_store_enabled": bool(store_enabled),
            "run_store_max_output_chars": max_output_chars,
        }
    except Exception as exc:
        err_msg = f"Project context collection error: {exc}"
        default_tracer.log_error("context", err_msg)
        return {
            "error": err_msg,
        }


def preflight_node(state: OrchestratorState) -> OrchestratorState:
    """Probe every configured agent before any of them is launched (Tier 1 #6).

    A failed strict preflight halts the run here, so an unusable environment
    costs seconds instead of failing partway through after agents have already
    consumed tokens.
    """
    if has_error(state):
        return {}

    config = state.get("config") or load_config(state.get("config_path"), state.get("project_root"))
    cfg = get_preflight_config(config)

    if state.get("skip_preflight") or not cfg.get("enabled", True):
        report = skipped_report()
        default_tracer.log_preflight_skipped()
        return {"preflight": report}

    default_tracer.log_preflight_start(deep=bool(cfg.get("deep", False)))
    report = run_preflight(
        config,
        deep=bool(cfg.get("deep", False)),
        strict=bool(cfg.get("strict", True)),
        timeout=int(cfg.get("timeout_seconds", 15)),
    )

    store = _get_store(state)
    if store is not None:
        store.record_preflight(report)

    default_tracer.log_preflight_complete(
        ok=bool(report.get("ok")),
        errors=list(report.get("errors") or []),
        warnings=list(report.get("warnings") or []),
        duration_seconds=float(report.get("duration_seconds") or 0.0),
    )

    updates: OrchestratorState = {"preflight": report}
    if not report.get("ok") and report.get("strict"):
        detail = "; ".join(report.get("errors") or []) or "preflight failed"
        updates["error"] = f"Preflight failed, no agent was launched: {detail}"
        default_tracer.log_error("preflight", updates["error"])
    return updates


# We must not use a static RUNNERS dictionary, because unittest.mock.patch
# patches the module-level names, and a static dict captures the original 
# functions at import time.
def get_runner(agent_name: str) -> Callable:
    import orchestrator.graph as mod
    if agent_name == "antigravity": return mod.run_antigravity
    if agent_name == "claude": return mod.run_claude_code
    if agent_name == "opencode": return mod.run_opencode
    return mod.run_claude_code

def _join_role_outputs(existing_results: List[AgentResult], role: str) -> str:
    outputs = get_latest_role_outputs(existing_results, role)
    if not outputs:
        return ""
    if len(outputs) == 1:
        return outputs[0].get("output", "")
    
    joined = []
    for i, res in enumerate(outputs, 1):
        agent = res.get("agent", "unknown")
        joined.append(f"--- {role.capitalize()} ({agent} #{i}) ---\n{res.get('output', '')}")
    return "\n\n".join(joined)


def make_role_node(role_name: str, agent_index: Optional[int] = None, is_repair: bool = False):
    """Factory to create a generalized agent execution node for a given role."""
    def role_node(state: OrchestratorState) -> OrchestratorState:
        # Guard on the error *fact*, not on a stored status string (Tier 1 #5).
        if has_error(state):
            return {}

        config = state.get("config") or load_config(state.get("config_path"), state.get("project_root"))
        
        # In repair mode, we still assume the implementer role is doing the repair
        cfg_role = "implementer" if is_repair else role_name
        
        if agent_index is not None:
            agents = config.get("agents", [])
            agent_cfg = agents[agent_index] if agent_index < len(agents) else {}
        else:
            agent_cfg = get_agent_config(config, role=cfg_role)
            
        if not agent_cfg:
            agent_cfg = {"agent": "claude"} # Fallback

        agent_name = agent_cfg.get("agent", "claude")
        model_name = agent_cfg.get("model")
        responsibility = get_role_responsibility(config, cfg_role)

        existing_results = list(state.get("agent_results") or [])
        repair_attempts = state.get("repair_attempts", 0)
        
        if isinstance(model_name, list):
            model_index = min(repair_attempts, len(model_name) - 1)
            model_name = model_name[model_index]
        task = state.get("task") or state.get("message", "")
        project_context = state.get("project_context", "(No project context supplied)")
        # Agents execute in the run's isolated worktree when there is one.
        project_root = _working_dir(state)
        skill_manifest = state.get("skill_manifest", "")
        
        visible = bool(state.get("visible_terminals", False))
        term_type = str(state.get("terminal_type", "auto"))
        pause_secs = float(state.get("pause_on_completion", 1.5))
        agent_exec_mode = str(state.get("agent_execution_mode", "auto"))

        max_repair_attempts = state.get("max_repair_attempts", 2)

        # Build prompts based on role
        if is_repair:
            repair_attempts += 1
            analysis = _join_role_outputs(existing_results, "researcher")
            plan_text = _join_role_outputs(existing_results, "planner")
            previous_impl = _join_role_outputs(existing_results, "implementer")
            
            verification_history = state.get("verification_history", [])
            verifier_output = verification_history[-1]["output"] if verification_history else ""

            prompt = build_repair_prompt(
                task=task,
                project_context=project_context,
                plan_text=plan_text,
                previous_implementation=previous_impl,
                verifier_output=verifier_output,
                repair_attempt=repair_attempts,
                max_repair_attempts=max_repair_attempts,
                responsibility=responsibility,
                role_name=cfg_role,
                skill_manifest=skill_manifest,
            )
            default_tracer.log_repair_start(
                attempt=repair_attempts,
                max_attempts=max_repair_attempts,
                agent=agent_name,
                model=model_name,
            )
            term_title = get_terminal_title(agent_name, "repair", repair_attempt=repair_attempts)
        elif role_name == "researcher":
            prompt = build_researcher_prompt(
                role_name, responsibility, project_context, task, skill_manifest
            )
            default_tracer.log_agent_start(
                agent=agent_name, role=role_name, model=model_name, step_label="[2/6]",
                title=agent_name.capitalize(), action="Investigating project..."
            )
            term_title = get_terminal_title(agent_name, role_name)
        elif role_name == "planner":
            analysis = _join_role_outputs(existing_results, "researcher")
            prompt = build_planner_prompt(
                role_name, responsibility, project_context, task, analysis, skill_manifest
            )
            default_tracer.log_agent_start(
                agent=agent_name, role=role_name, model=model_name, step_label="[3/6]",
                title=agent_name.capitalize(), action="Reviewing research..."
            )
            term_title = get_terminal_title(agent_name, role_name)
        elif role_name == "implementer":
            analysis = _join_role_outputs(existing_results, "researcher")
            plan_text = _join_role_outputs(existing_results, "planner")
            prompt = build_implementer_prompt(
                role_name, responsibility, project_context, task, analysis, plan_text, skill_manifest
            )
            default_tracer.log_agent_start(
                agent=agent_name, role=role_name, model=model_name, step_label="[4/6]",
                title=agent_name.capitalize(), action="Implementing planned changes in workspace..."
            )
            term_title = get_terminal_title(agent_name, role_name)
        elif role_name == "verifier":
            analysis = _join_role_outputs(existing_results, "researcher")
            plan_text = _join_role_outputs(existing_results, "planner")
            implementation = _join_role_outputs(existing_results, "implementer")
            
            verification_history = state.get("verification_history", [])
            verification_attempt = len(verification_history) + 1
            previous_verifier_output = verification_history[-1]["output"] if verification_attempt > 1 and verification_history else None
            
            if verification_attempt == 1:
                default_tracer.log_agent_start(
                    agent=agent_name, role=role_name, model=model_name, step_label="[5/6]",
                    title=agent_name.capitalize(), action="Verifying implementation against acceptance criteria..."
                )
            else:
                default_tracer.log_reverification_start(
                    attempt=verification_attempt, agent=agent_name, model=model_name,
                )

            prompt = build_verifier_prompt(
                task=task, project_context=project_context, research_text=analysis,
                plan_text=plan_text, implementation_text=implementation,
                responsibility=responsibility, role_name=role_name,
                verification_attempt=verification_attempt, repair_attempts_completed=repair_attempts,
                previous_verifier_output=previous_verifier_output, skill_manifest=skill_manifest,
            )
            term_title = get_terminal_title(agent_name, role_name, verification_attempt=verification_attempt)
        else:
            # Fallback for dynamic roles
            prompt = f"Role: {role_name}\nResponsibility: {responsibility}\nTask: {task}\nContext:\n{project_context}"
            default_tracer.log_agent_start(
                agent=agent_name, role=role_name, model=model_name, step_label="[?]",
                title=agent_name.capitalize(), action=f"Executing {role_name} tasks..."
            )
            term_title = get_terminal_title(agent_name, role_name)

        start_time = time.time()
        runner = get_runner(agent_name)

        try:
            kwargs = {
                "working_dir": project_root,
                "model": model_name,
                "visible": visible,
                "title": term_title,
                "role": role_name,
                "terminal_type": term_type,
                "pause_on_completion": pause_secs,
                "tracer": default_tracer,
                "agent_execution_mode": agent_exec_mode,
                "return_usage": True,
            }
            if runner == run_opencode:
                kwargs["return_execution_mode"] = True

            raw_res = runner(prompt, **kwargs)
            duration = round(time.time() - start_time, 2)

            exec_mode = "headless"
            if isinstance(raw_res, tuple):
                if len(raw_res) == 3:
                    output_text, token_usage, exec_mode = raw_res
                elif len(raw_res) == 2:
                    output_text, token_usage = raw_res
                else:
                    output_text, token_usage = "", unavailable_token_usage()
            else:
                output_text = str(raw_res) if raw_res is not None else ""
                token_usage = unavailable_token_usage()

            if not output_text.strip():
                err_msg = f"{agent_name} returned empty output for {role_name}"
                default_tracer.log_agent_error(agent_name, role_name, err_msg, model=model_name)
                error_result = create_agent_result(
                    # The failure reason is recorded as the result's output so it
                    # survives into the run store and the phase's error message.
                    agent=agent_name, role=role_name, status="error", output=err_msg,
                    duration_seconds=duration, model=model_name, token_usage=token_usage,
                    execution_mode=exec_mode,
                )
                store = _get_store(state)
                if store is not None:
                    store.record_agent_result(error_result)
                # Record the failure as a fact. Whether it is fatal is decided by
                # this phase's sync node, which can see every sibling's outcome.
                return {"agent_results": [error_result]}

            verdict = None
            if role_name == "verifier":
                verdict = parse_verdict(output_text)
                default_tracer.log_verifier_complete(
                    agent=agent_name, verdict=verdict, response_length=len(output_text),
                    model=model_name, token_usage=token_usage
                )
            elif is_repair:
                default_tracer.log_repair_complete(
                    attempt=repair_attempts, agent=agent_name, response_length=len(output_text),
                    model=model_name, token_usage=token_usage
                )
            else:
                default_tracer.log_agent_complete(
                    agent=agent_name, role=role_name, response_length=len(output_text),
                    model=model_name, token_usage=token_usage
                )
                # Simple handoff logs
                if role_name == "researcher": default_tracer.log_handoff_to_claude()
                if role_name == "planner": default_tracer.log_handoff_to_opencode()
                if role_name == "implementer": default_tracer.log_handoff_to_verifier()

            res = create_agent_result(
                agent=agent_name, role=role_name, status="success", output=output_text,
                duration_seconds=duration, model=model_name, token_usage=token_usage,
                execution_mode=exec_mode, verdict=verdict, repair_attempt=repair_attempts if is_repair else None
            )

            store = _get_store(state)
            if store is not None:
                store.record_agent_result(res)

            state_updates: OrchestratorState = {
                "agent_results": [res],
            }

            # Record durable facts only. The phase verdict is resolved by the
            # sync node; `status` is derived by finalize_node.
            if is_repair:
                state_updates["repair_attempts"] = repair_attempts
            elif role_name == "verifier":
                v_record: VerificationRecord = {
                    "attempt": len(state.get("verification_history", [])) + 1,
                    "repair_attempts": repair_attempts,
                    "verdict": verdict,
                    "output": output_text,
                    "duration_seconds": duration,
                    "model": model_name,
                    "agent": agent_name,
                    "token_usage": token_usage,
                }
                # Tier 1 #7 - carry the human-action text as a fact on the record,
                # so a BLOCKED verdict survives consensus resolution.
                if verdict == VERDICT_BLOCKED:
                    v_record["blocked_reason"] = extract_human_action(output_text) or (
                        "The verifier reported that human input is required, but did not "
                        "specify what. See the verifier output above."
                    )
                state_updates["verification_history"] = [v_record]
                if store is not None:
                    store.record_verification(v_record)

            return state_updates

        except Exception as exc:
            duration = round(time.time() - start_time, 2)
            err_msg = f"{agent_name} ({role_name}) node error: {exc}"
            default_tracer.log_agent_error(agent_name, role_name, err_msg, model=model_name)
            error_result = create_agent_result(
                agent=agent_name, role=role_name, status="error", output=err_msg,
                duration_seconds=duration, model=model_name,
            )
            store = _get_store(state)
            if store is not None:
                store.record_agent_result(error_result)
            # As above: a fact, not a verdict on the run. The sync node decides.
            return {"agent_results": [error_result]}

    return role_node


def make_sync_node(role_name: str, member_count: int):
    """Factory for a phase's fan-in node.

    Every phase — whether it holds one agent or an ensemble — ends here. The
    sync node is where phase-level decisions live, because it is the only place
    that can see every member's outcome:

    * **Fatal-vs-survivable failure.** A phase is fatal only when *all* of its
      members failed. One dead member of a three-way ensemble is a warning, not
      the end of the run (Tier 2 #8).
    * **Verifier consensus.** Parallel verifiers each record their own verdict;
      this node resolves them into the one verdict that routes the workflow,
      which also avoids concurrent writes to a single-value channel.
    """
    def sync_node(state: OrchestratorState) -> OrchestratorState:
        if has_error(state):
            return {}

        results = list(state.get("agent_results") or [])
        group = get_latest_role_outputs(results, role_name)
        errored = [r for r in group if r.get("status") == "error"]
        succeeded = [r for r in group if r.get("status") == "success"]

        # Every member of the phase failed: the run cannot continue. Carry the
        # underlying reasons forward rather than replacing them with a count.
        if group and not succeeded:
            reasons = [r.get("output") or f"{r.get('agent')} failed" for r in errored]
            if len(group) == 1:
                err_msg = reasons[0]
            else:
                joined = "; ".join(reasons)
                err_msg = (
                    f"All {len(group)} agents in the '{role_name}' phase failed: {joined}"
                )
            last_failed = errored[-1] if errored else {}
            default_tracer.log_error(
                role_name,
                err_msg,
                agent=last_failed.get("agent"),
                role=role_name,
            )
            return {"error": err_msg}

        updates: OrchestratorState = {}

        # Some members failed but others succeeded: continue on the survivors.
        if errored and succeeded:
            default_tracer.log_phase_partial_failure(
                role=role_name,
                failed=len(errored),
                total=len(group),
                surviving_agents=[r.get("agent", "?") for r in succeeded],
            )

        if role_name == "verifier":
            policy = state.get("consensus_policy") or "unanimous"
            group_records = latest_verification_group(state)
            verdict = resolve_consensus(
                [rec.get("verdict") for rec in group_records], policy=policy
            )
            updates["verification_verdict"] = verdict

            if member_count > 1 or len(group_records) > 1:
                default_tracer.log_consensus(
                    verdicts=[(rec.get("model") or rec.get("agent") or "?", rec.get("verdict") or "UNKNOWN")
                              for rec in group_records],
                    policy=policy,
                    resolved=verdict,
                )

            repair_attempts = state.get("repair_attempts", 0)
            max_repair_attempts = state.get("max_repair_attempts", 2)

            if verdict == VERDICT_BLOCKED:
                reason = derive_blocked_reason(state) or (
                    "A verifier reported that human input is required."
                )
                updates["blocked_reason"] = reason
                default_tracer.log_blocked(reason)
                default_tracer.log_workflow_complete()
            else:
                default_tracer.log_repair_decision(
                    verdict=verdict,
                    repair_attempts=repair_attempts,
                    max_repair_attempts=max_repair_attempts,
                )
                if verdict == VERDICT_PASS or repair_attempts >= max_repair_attempts:
                    default_tracer.log_workflow_complete()

        return updates

    return sync_node


def finalize_node(state: OrchestratorState) -> OrchestratorState:
    """Terminal node: derive the status once, from facts, and close the run.

    This is the only place in the pipeline that writes `status`. Every upstream
    node records what happened; this node decides what that means (Tier 1 #5).
    """
    summary = derive_summary(state)

    blocked_reason = state.get("blocked_reason")
    if blocked_reason:
        summary["blocked_reason"] = blocked_reason

    report = state.get("preflight")
    if isinstance(report, dict):
        summary["preflight_ok"] = bool(report.get("ok"))

    if state.get("error"):
        summary["error"] = state.get("error")

    # Tier 0 #3 - record what the run changed, and commit it onto the run's own
    # branch so the work is reviewable as a diff and survives worktree cleanup.
    workspace = state.get("workspace")
    workspace_summary = summarize_worktree(workspace) if workspace else {"isolated": False}
    if workspace and workspace.get("isolated"):
        config = state.get("config")
        ws_cfg = get_workspace_config(config)
        if ws_cfg.get("commit_on_finish") and workspace_summary.get("change_count"):
            task_line = (state.get("task") or "orchestrator run").strip().splitlines()[0][:72]
            commit = commit_worktree(
                workspace,
                f"{task_line}\n\n"
                f"Orchestrator run {state.get('run_id') or ''} "
                f"(status: {summary['status']}, verdict: {summary['verdict']}).",
            )
            if commit:
                workspace_summary["commit"] = commit
        default_tracer.log_workspace_result(
            describe_workspace(workspace),
            change_count=int(workspace_summary.get("change_count") or 0),
            commit=workspace_summary.get("commit"),
        )
    summary["workspace"] = workspace_summary

    store = _get_store(state)
    if store is not None:
        store.record_run_finished(summary)
        default_tracer.log_run_store_closed(
            store.run_id, store.run_dir, summary.get("status", "unknown")
        )
        if store.degraded:
            default_tracer.log_run_store_degraded(store.degraded_reason or "unknown error")

    return {
        "status": summary["status"],
        "summary": summary,
        "workspace_summary": workspace_summary,
    }


def should_repair_or_end(state: OrchestratorState) -> str:
    """Route from verification: repair, or finalize.

    Repair is attempted only for verdicts an agent could plausibly fix. A
    BLOCKED verdict needs a human, so it finalizes immediately without spending
    any of the repair budget (Tier 1 #7).
    """
    if has_error(state):
        return "finalize"

    # The sync node writes the resolved verdict; derive it as a fallback so this
    # router is correct even for a partial state (Tier 1 #5).
    verdict = state.get("verification_verdict") or derive_verdict(
        state, policy=state.get("consensus_policy") or "unanimous"
    )

    if verdict == VERDICT_PASS:
        return "finalize"

    if verdict == VERDICT_BLOCKED:
        return "finalize"

    if not is_repairable(verdict):
        return "finalize"

    repair_attempts = state.get("repair_attempts", 0)
    max_repair_attempts = state.get("max_repair_attempts", 2)

    if repair_attempts < max_repair_attempts:
        return "repair_node"

    return "finalize"

# =================================================================================
# Dynamic Graph Construction
# =================================================================================
def build_graph(config: OrchestratorConfig):
    """Assemble the orchestration graph.

    Topology::

        START -> context -> preflight -> <roles in configured order> -> finalize -> END
                                              verifier -+-> repair_node -> verifier
                                                        `-> finalize

    `preflight` gates every agent launch; `finalize` derives the run's status
    exactly once. Both are always present so that every terminating path — pass,
    fail, blocked, or error — ends with a derived status and a closed run store.
    """
    builder = StateGraph(OrchestratorState)
    builder.add_node("context", context_node)
    builder.add_node("preflight", preflight_node)
    builder.add_node("finalize", finalize_node)

    builder.add_edge(START, "context")
    builder.add_edge("context", "preflight")
    builder.add_edge("finalize", END)

    agents = config.get("agents", [])
    if not agents:
        # Fallback if config is malformed
        builder.add_edge("preflight", "finalize")
        return builder.compile()

    # Group consecutive same-role agents into one parallel phase.
    phases: List[List[Tuple[int, Dict[str, Any]]]] = []
    current_phase: List[Tuple[int, Dict[str, Any]]] = []
    for i, agent in enumerate(agents):
        if current_phase and current_phase[-1][1].get("role") == agent.get("role"):
            current_phase.append((i, agent))
        else:
            if current_phase:
                phases.append(current_phase)
            current_phase = [(i, agent)]
    if current_phase:
        phases.append(current_phase)

    all_roles = {agent.get("role") for agent in agents}
    has_repair_loop = "implementer" in all_roles and "verifier" in all_roles

    if has_repair_loop:
        # The repair node reuses the last implementer's configuration, which is
        # also where a model escalation ladder lives.
        impl_indices = [i for i, a in enumerate(agents) if a.get("role") == "implementer"]
        repair_agent_idx = impl_indices[-1] if impl_indices else None
        builder.add_node(
            "repair_node",
            make_role_node("implementer", agent_index=repair_agent_idx, is_repair=True),
        )

    # Every phase gets member nodes plus one sync node, whether it holds one
    # agent or an ensemble. Uniformity means phase-level decisions (fatal vs.
    # survivable failure, verifier consensus) have exactly one home.
    last_phase_nodes = ["preflight"]
    phase_members: Dict[str, List[str]] = {}
    used_sync_names: Dict[str, int] = {}

    for phase in phases:
        role = phase[0][1].get("role")

        # A role appearing in two non-consecutive phases must not collide.
        seen = used_sync_names.get(role, 0)
        used_sync_names[role] = seen + 1
        sync_name = role if seen == 0 else f"{role}__{seen + 1}"

        member_nodes: List[str] = []
        for idx, (agent_idx, _) in enumerate(phase):
            node_name = f"{sync_name}_{idx}"
            builder.add_node(node_name, make_role_node(role, agent_index=agent_idx))
            member_nodes.append(node_name)

        builder.add_node(sync_name, make_sync_node(role, len(phase)))

        for prev in last_phase_nodes:
            for member in member_nodes:
                builder.add_edge(prev, member)
        for member in member_nodes:
            builder.add_edge(member, sync_name)

        phase_members[sync_name] = member_nodes
        last_phase_nodes = [sync_name]

    if has_repair_loop:
        # Routing hangs off the verifier's sync node, which holds the resolved
        # consensus verdict. Repair re-enters at the verifier's member nodes so
        # that every verifier in a quorum re-evaluates the repaired workspace.
        builder.add_conditional_edges(
            "verifier",
            should_repair_or_end,
            {
                "repair_node": "repair_node",
                "finalize": "finalize",
            },
        )
        for member in phase_members.get("verifier", ["verifier"]):
            builder.add_edge("repair_node", member)
    else:
        for node in last_phase_nodes:
            builder.add_edge(node, "finalize")

    return builder.compile()

# To maintain backward compatibility with `from orchestrator.graph import graph`, 
# we need to build a static graph using the default config initially.
# When actually running, the state's `config` can override node behavior, 
# but the topology is fixed at import time by `orchestrator.yaml`.
try:
    _initial_config = load_config(None, get_project_root())
except Exception:
    _initial_config = {"agents": [{"role": "researcher"}, {"role": "planner"}, {"role": "implementer"}, {"role": "verifier"}]}

graph = build_graph(_initial_config)
