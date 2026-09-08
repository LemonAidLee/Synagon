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
from orchestrator.decompose import parse_task_plan
from orchestrator.prompts import (
    build_decomposer_prompt,
    build_implementer_prompt,
    build_planner_prompt,
    build_researcher_prompt,
    build_verifier_prompt,
    build_repair_prompt,
)
from orchestrator.acceptance import (
    format_for_prompt as format_acceptance_for_prompt,
    describe_check,
    latest_check,
    parse_command,
    run_acceptance,
)
from orchestrator.budget import evaluate_budget
from orchestrator.config import (
    OrchestratorConfig,
    load_config,
    get_acceptance_config,
    get_agent_config,
    get_role_responsibility,
    get_budget_config,
    get_consensus_policy,
    get_planning_config,
    get_retry_config,
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
from orchestrator.resume import should_replay
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
    describe_retention,
    describe_workspace,
    finish_worktree,
    merge_refs_into_worktree,
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
    # Tier 2 #11 - run budget: the ceiling the escalation ladder spends against
    budget: Optional[dict]
    budget_state: Optional[dict]
    budget_exhausted_reason: Optional[str]
    run_started_at: Optional[float]
    # Tier 1 #4 - resume: facts replayed from a previous run's event log
    resumed_from: Optional[str]
    # Phase 0 - objective acceptance gate: a command the orchestrator ran itself
    acceptance_checks: Annotated[List[Dict[str, Any]], add]
    acceptance_required: Optional[bool]
    # Phase 1 - goal decomposition
    task_plan: Optional[dict]
    plan_only: Optional[bool]
    # Phases 2-3 - this session is one task of a delegated goal
    goal_id: Optional[str]
    task_id: Optional[str]
    workspace_base_ref: Optional[str]      # branch or commit this session starts from
    workspace_merge_refs: Optional[list]   # dependency branches merged in before any agent runs
    workspace_merge: Optional[dict]        # what that merge did
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
    base_ref: Optional[str] = None,
) -> WorkspaceInfo:
    """Resolve the workspace this run executes in (Tier 0 #3).

    `base_ref` is what a delegated task uses to start from its dependency's branch instead of
    the current HEAD (Roadmap Phase 2).

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
    return create_run_worktree(
        project_root,
        identifier,
        directory=ws_cfg.get("directory"),
        base_ref=base_ref,
    )


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
                # Recorded so `--stats` can compare outcomes across settings,
                # not merely count runs.
                settings={
                    "consensus": get_consensus_policy(config),
                    "isolation": get_workspace_config(config).get("isolation"),
                    "budget": dict(get_budget_config(config)),
                    "agent_execution_mode": str(agent_exec_mode),
                },
                goal_id=state.get("goal_id"),
                task_id=state.get("task_id"),
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
        merge_outcome = state.get("workspace_merge")
        if workspace is None:
            workspace = _prepare_workspace(
                config, root, run_id, base_ref=state.get("workspace_base_ref")
            )
            if workspace.get("isolated"):
                default_tracer.log_workspace_isolated(
                    workspace.get("path", ""), workspace.get("branch", "")
                )
                # A delegated task is built on its dependencies' work, so their branches are
                # merged into this fresh worktree before any agent sees it (Phase 2). A
                # conflict is never guessed at: the run stops and a person is asked.
                merge_refs = [str(r) for r in (state.get("workspace_merge_refs") or []) if r]
                if merge_refs:
                    merge_outcome = merge_refs_into_worktree(workspace, merge_refs)
                    default_tracer.log_dependency_merge(
                        merged=list(merge_outcome.get("merged") or []),
                        conflicted=list(merge_outcome.get("conflicted") or []),
                        error=merge_outcome.get("error"),
                    )
            else:
                default_tracer.log_workspace_not_isolated(workspace.get("reason") or "unknown")
            store = _get_store({"run_dir": run_dir, "run_store_max_output_chars": max_output_chars})
            if store is not None:
                store.record_event("workspace", workspace=workspace)
                if merge_outcome:
                    store.record_event("dependency_merge", merge=merge_outcome)

        if isinstance(merge_outcome, dict) and merge_outcome.get("conflicted"):
            return {
                "workspace": workspace,
                "workspace_merge": merge_outcome,
                "error": (
                    "the task's dependencies could not be merged into one workspace: "
                    f"{merge_outcome.get('error')}. Nothing was guessed at; reconcile "
                    f"{', '.join(merge_outcome.get('conflicted') or [])} by hand."
                ),
            }

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
            # A run's cost ceiling and the clock it is measured against. Seeded
            # once here so every later decision reads the same numbers.
            "budget": state.get("budget") or dict(get_budget_config(config)),
            "run_started_at": state.get("run_started_at") or time.time(),
            # Whether a red gate can override a verifier's PASS is a policy, and it travels
            # with the run so `derive_verdict` can apply it without loading config.
            "acceptance_required": bool(get_acceptance_config(config).get("required", True)),
            "workspace": workspace,
            "workspace_merge": merge_outcome,
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


def make_role_node(
    role_name: str,
    agent_index: Optional[int] = None,
    is_repair: bool = False,
    agent_override: Optional[Dict[str, Any]] = None,
):
    """Factory to create a generalized agent execution node for a given role.

    `agent_override` supplies an agent entry that is not part of the `agents:` pipeline - the
    decomposer, which runs before a pipeline rather than inside one.
    """
    def role_node(state: OrchestratorState) -> OrchestratorState:
        # Guard on the error *fact*, not on a stored status string (Tier 1 #5).
        if has_error(state):
            return {}

        config = state.get("config") or load_config(state.get("config_path"), state.get("project_root"))
        
        # In repair mode, we still assume the implementer role is doing the repair
        cfg_role = "implementer" if is_repair else role_name
        
        if agent_override is not None:
            agent_cfg = dict(agent_override)
        elif agent_index is not None:
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

        # Tier 1 #4 - a resumed run replays the work the original already
        # produced rather than paying for it twice. The repair node still has to
        # advance the attempt counter it is replaying, or the loop would route
        # straight back into the same replay.
        if should_replay(state, role_name, is_repair=is_repair, repair_attempts=repair_attempts):
            default_tracer.log_phase_replayed(role_name, agent=agent_name)
            return {"repair_attempts": repair_attempts + 1} if is_repair else {}

        # A model may be an escalation ladder: rung 0 for the initial attempt,
        # rung N for repair attempt N. A repair node is executing attempt
        # `repair_attempts + 1`, so it must escalate now rather than repeat the
        # model that just failed.
        model_ladder = list(model_name) if isinstance(model_name, list) else None
        ladder_rung = 0
        if model_ladder:
            ladder_rung = min(
                (repair_attempts + 1) if is_repair else 0, len(model_ladder) - 1
            )
            model_name = model_ladder[ladder_rung]
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

            # The gate's own failure output is the most useful thing a repair can be given:
            # a real stack trace beats the verifier's summary of one.
            acceptance_section = format_acceptance_for_prompt(
                latest_check(state.get("acceptance_checks"), repair_attempts - 1)
            )

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
                acceptance_section=acceptance_section,
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
                acceptance_section=format_acceptance_for_prompt(
                    latest_check(state.get("acceptance_checks"), repair_attempts)
                ),
            )
            term_title = get_terminal_title(agent_name, role_name, verification_attempt=verification_attempt)
        elif role_name == "decomposer":
            # Roadmap 8.2: the decomposer is the one role whose prompt carries what previous
            # goals recorded. Building it never fails a run - `memory_section` returns "" on
            # any problem, and "" is the cold prompt this had before it had a memory.
            planning_cfg = get_planning_config(config)
            memory_cfg = planning_cfg.get("memory") or {}
            memory_text = ""
            if memory_cfg.get("enabled", True):
                from orchestrator.memory import memory_section

                memory_text = memory_section(
                    str(state.get("project_root") or ""),
                    config,
                    max_goals=int(memory_cfg.get("max_goals", 12)),
                    budget_chars=int(memory_cfg.get("budget_chars", 2400)),
                )

            prompt = build_decomposer_prompt(
                role_name=role_name,
                responsibility=responsibility,
                project_context=project_context,
                task=task,
                skill_manifest=skill_manifest,
                max_tasks=int(planning_cfg.get("max_tasks", 12)),
                memory=memory_text,
            )
            default_tracer.log_agent_start(
                agent=agent_name, role=role_name, model=model_name, step_label="[2/3]",
                title=agent_name.capitalize(), action="Breaking the goal into tasks...",
            )
            term_title = get_terminal_title(agent_name, role_name)
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

        # An execution beginning is a fact too, and the only one that tells a reader who is
        # working right now rather than who has already finished.
        store = _get_store(state)
        if store is not None:
            store.record_agent_started(
                agent=agent_name,
                role=role_name,
                model=model_name,
                repair_attempt=repair_attempts if is_repair else None,
            )

        # A local CLI agent is a subprocess over a network service, and it fails the way those
        # fail: intermittently, and by returning *nothing* rather than by raising. Every real
        # end-to-end failure this project ever recorded was exactly that - one empty researcher
        # reply on the first step, and the run halted with no second attempt. So an execution
        # is now attempted up to `execution.retry.attempts` times, with a widening gap, and
        # steps down the model ladder as it goes when one is configured.
        #
        # A retry is not an ensemble. However many attempts it takes, the phase produces ONE
        # AgentResult, because `agent_results` is what consensus is resolved from and what
        # `--stats` computes pass rates over; three rows for one execution would corrupt both.
        # The attempts that failed are recorded as their own events instead.
        retry_cfg = get_retry_config(config)
        max_attempts = max(1, int(retry_cfg.get("attempts", 1)))
        base_backoff = float(retry_cfg.get("backoff_seconds", 0.0))
        max_backoff = float(retry_cfg.get("max_backoff_seconds", 30.0))
        escalate = bool(retry_cfg.get("escalate_model", True)) and bool(model_ladder)

        attempt_failures: List[str] = []
        output_text = ""
        token_usage = unavailable_token_usage()
        exec_mode = "headless"
        duration = 0.0
        fatal_exception: Optional[BaseException] = None

        for attempt in range(1, max_attempts + 1):
            fatal_exception = None
            start_time = time.time()
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

                if (output_text or "").strip():
                    break  # the execution produced something; carry on below

                reason = f"{agent_name} returned empty output for {role_name}"
            except Exception as exc:  # noqa: BLE001 - every failure mode retries the same way
                duration = round(time.time() - start_time, 2)
                fatal_exception = exc
                detail = str(exc) if str(exc).strip() else repr(exc)
                reason = f"node error ({type(exc).__name__}): {detail}"
                output_text = ""
                token_usage = unavailable_token_usage()

            attempt_failures.append(f"attempt {attempt}: {reason}")

            if attempt >= max_attempts:
                break

            # Widen the gap, and step down the ladder if one is configured. The next rung is
            # a *stronger* model by convention, which is the same bet the repair ladder makes.
            backoff = min(base_backoff * (2 ** (attempt - 1)), max_backoff)
            next_model = model_name
            if escalate and model_ladder and ladder_rung < len(model_ladder) - 1:
                ladder_rung += 1
                next_model = model_ladder[ladder_rung]

            default_tracer.log_agent_retry(
                agent=agent_name, role=role_name, attempt=attempt, of=max_attempts,
                reason=reason, model=model_name, next_model=next_model,
                backoff_seconds=backoff,
            )
            if store is not None:
                store.record_agent_retry(
                    agent=agent_name, role=role_name, attempt=attempt, of=max_attempts,
                    reason=reason, model=model_name, next_model=next_model,
                    backoff_seconds=backoff,
                )

            model_name = next_model
            if backoff > 0:
                time.sleep(backoff)

        try:
            if not (output_text or "").strip():
                # Every attempt failed. Report the last reason, and carry the whole history
                # so "it was flaky" and "it is broken" are distinguishable after the fact.
                if fatal_exception is not None:
                    detail = (
                        str(fatal_exception)
                        if str(fatal_exception).strip()
                        else repr(fatal_exception)
                    )
                    err_msg = (
                        f"{agent_name} ({role_name}) node error "
                        f"({type(fatal_exception).__name__}): {detail}"
                    )
                else:
                    err_msg = f"{agent_name} returned empty output for {role_name}"
                if max_attempts > 1:
                    err_msg += f" (after {max_attempts} attempts)"

                default_tracer.log_agent_error(agent_name, role_name, err_msg, model=model_name)
                error_result = create_agent_result(
                    # The failure reason is recorded as the result's output so it
                    # survives into the run store and the phase's error message.
                    agent=agent_name, role=role_name, status="error", output=err_msg,
                    duration_seconds=duration, model=model_name, token_usage=token_usage,
                    execution_mode=exec_mode,
                    attempts=len(attempt_failures) or 1,
                    attempt_failures=list(attempt_failures),
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
                execution_mode=exec_mode, verdict=verdict,
                repair_attempt=repair_attempts if is_repair else None,
                # A success on the second attempt is still a success, but a run whose agents
                # needed retrying is a different fact from one whose agents did not, and only
                # the result can carry it to `--stats`.
                attempts=len(attempt_failures) + 1,
                attempt_failures=list(attempt_failures) or None,
            )

            store = _get_store(state)
            if store is not None:
                store.record_agent_result(res)

            state_updates: OrchestratorState = {
                "agent_results": [res],
            }

            if role_name == "decomposer":
                # The agent's reply is text until it parses. The plan - not the prose - is
                # the durable fact.
                plan = parse_task_plan(
                    output_text,
                    goal=task,
                    max_tasks=int(get_planning_config(config).get("max_tasks", 12)),
                )
                state_updates["task_plan"] = plan
                default_tracer.log_task_plan(
                    task_count=len(plan.get("tasks") or []),
                    waves=len(plan.get("order") or []),
                    error=plan.get("error"),
                    warnings=list(plan.get("warnings") or []),
                )
                if store is not None:
                    store.record_task_plan(plan)

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
            # Name the exception type and fall back to `repr` when `str(exc)` is
            # empty (e.g. StopIteration), so a swallowed failure is never logged
            # as a bare "node error: ".
            detail = str(exc) if str(exc).strip() else repr(exc)
            err_msg = f"{agent_name} ({role_name}) node error ({type(exc).__name__}): {detail}"
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


def acceptance_node(state: OrchestratorState) -> OrchestratorState:
    """Run the project's own acceptance command and record the result (Phase 0).

    Runs between the implementer and the verifier, so the verifier is handed evidence rather
    than asked to produce it. The command comes from the configuration loaded at the project
    root, never from the worktree the agents can edit.
    """
    if has_error(state):
        return {}

    config = state.get("config") or load_config(state.get("config_path"), state.get("project_root"))
    cfg = get_acceptance_config(config)
    command = cfg.get("command")
    repair_attempts = int(state.get("repair_attempts") or 0)

    if not parse_command(command):
        return {}

    default_tracer.log_acceptance_start(parse_command(command))
    check = run_acceptance(
        command,
        working_dir=_working_dir(state) or "",
        timeout_seconds=int(cfg.get("timeout_seconds", 600)),
        output_limit=int(cfg.get("output_limit", 4000)),
        repair_attempts=repair_attempts,
    )
    default_tracer.log_acceptance_result(
        ok=bool(check.get("ok")),
        exit_code=check.get("exit_code"),
        duration_seconds=float(check.get("duration_seconds") or 0.0),
        error=check.get("error"),
        required=bool(cfg.get("required", True)),
    )

    store = _get_store(state)
    if store is not None:
        store.record_acceptance(check)

    return {"acceptance_checks": [dict(check)]}


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

        # A resumed run carries the previous session's records for this role in
        # the same trailing block. When this phase actually executed, judge it on
        # what it produced now: a member that died in the session before is part
        # of the run's history, not of this phase's outcome (Tier 1 #4).
        fresh = [r for r in group if not r.get("restored")]
        if fresh:
            group = fresh

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
            consensus = resolve_consensus(
                [rec.get("verdict") for rec in group_records], policy=policy
            )
            # `derive_verdict` owns the precedence between a consensus and the objective
            # acceptance gate (Phase 0), so the routed verdict is read from it rather than
            # recomputed here - otherwise the router and the derived status could disagree.
            verdict = derive_verdict(state, policy=policy) or consensus
            updates["verification_verdict"] = verdict
            if verdict != consensus:
                default_tracer.log_acceptance_override(consensus, verdict)

            if member_count > 1 or len(group_records) > 1:
                default_tracer.log_consensus(
                    verdicts=[(rec.get("model") or rec.get("agent") or "?", rec.get("verdict") or "UNKNOWN")
                              for rec in group_records],
                    policy=policy,
                    resolved=verdict,
                )

            repair_attempts = state.get("repair_attempts", 0)
            max_repair_attempts = state.get("max_repair_attempts", 2)

            # Tier 2 #11 - measure the run against its ceiling here, where the
            # decision to fund another attempt is about to be made, and record
            # the answer as a fact. `should_repair_or_end` and `derive_status`
            # both read that fact rather than re-measuring a moving clock.
            budget_state = evaluate_budget(state)
            updates["budget_state"] = dict(budget_state)
            if budget_state.get("exhausted") and verdict != VERDICT_PASS:
                reason = budget_state.get("reason") or "run budget exhausted"
                updates["budget_exhausted_reason"] = reason
                if verdict != VERDICT_BLOCKED:
                    default_tracer.log_budget_exhausted(
                        reason=reason,
                        tokens_spent=int(budget_state.get("tokens_spent") or 0),
                        max_total_tokens=int(budget_state.get("max_total_tokens") or 0),
                        seconds_elapsed=float(budget_state.get("seconds_elapsed") or 0.0),
                        max_duration_seconds=int(budget_state.get("max_duration_seconds") or 0),
                        repair_attempts=repair_attempts,
                        max_repair_attempts=max_repair_attempts,
                    )

            if verdict == VERDICT_BLOCKED:
                reason = derive_blocked_reason(state) or (
                    "A verifier reported that human input is required."
                )
                updates["blocked_reason"] = reason
                default_tracer.log_blocked(reason)
                default_tracer.log_workflow_complete()
            elif updates.get("budget_exhausted_reason"):
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

    gate = latest_check(state.get("acceptance_checks"), int(state.get("repair_attempts") or 0))
    if gate is not None:
        summary["acceptance"] = dict(gate)
        summary["acceptance_summary"] = describe_check(gate)

    plan = state.get("task_plan")
    if isinstance(plan, dict):
        summary["task_plan"] = {
            "tasks": len(plan.get("tasks") or []),
            "waves": len(plan.get("order") or []),
            "error": plan.get("error"),
        }

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

        # The commit above put the work on the branch, which makes the checkout
        # redundant. Leaving it costs a full copy of the project per run, so it
        # goes - unless it is dirty, in which case it is the only copy.
        retention = finish_worktree(
            workspace,
            keep_worktree=bool(ws_cfg.get("keep_worktree", False)),
            commit=workspace_summary.get("commit"),
        )
        workspace_summary["retention"] = retention
        default_tracer.log_workspace_result(
            describe_workspace(workspace),
            change_count=int(workspace_summary.get("change_count") or 0),
            commit=workspace_summary.get("commit"),
            retention=describe_retention(workspace, retention),
        )
    summary["workspace"] = workspace_summary
    started = state.get("run_started_at")
    if isinstance(started, (int, float)) and started > 0:
        summary["duration_seconds"] = round(time.time() - float(started), 2)

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

    Three ceilings govern a repair, and all of them are checked here: the number
    of attempts (`max_repair_attempts`), the tokens spent, and the wall time
    elapsed (Tier 2 #11). An escalation ladder makes each attempt more expensive
    than the last, so an attempt bound alone does not bound cost.
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

    # The sync node records the budget verdict as a fact; re-measure only when
    # routing a state that never passed through it.
    if state.get("budget_exhausted_reason") or evaluate_budget(state).get("exhausted"):
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

    # The acceptance gate is a node in its own right, placed immediately before the verifier,
    # so that every path into verification - the first pass and every repair - runs it.
    gate_enabled = bool(parse_command(get_acceptance_config(config).get("command")))

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

        # Objective evidence is gathered before the verifier is asked for an opinion.
        if role == "verifier" and gate_enabled and "acceptance" not in phase_members:
            builder.add_node("acceptance", acceptance_node)
            for prev in last_phase_nodes:
                builder.add_edge(prev, "acceptance")
            last_phase_nodes = ["acceptance"]
            phase_members["acceptance"] = ["acceptance"]

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
        if gate_enabled and "acceptance" in phase_members:
            # A repair changes the workspace, so the gate is re-run before it is re-judged.
            builder.add_edge("repair_node", "acceptance")
        else:
            for member in phase_members.get("verifier", ["verifier"]):
                builder.add_edge("repair_node", member)
    else:
        for node in last_phase_nodes:
            builder.add_edge(node, "finalize")

    return builder.compile()

def build_plan_graph(config: OrchestratorConfig):
    """Assemble the decomposition-only graph (Roadmap Phase 1).

    Topology::

        START -> context -> preflight -> decomposer -> finalize -> END

    Deliberately not part of `build_graph`: decomposition runs *before* a pipeline and produces
    a plan, not a change. Keeping it a separate graph means `--plan-only` cannot accidentally
    launch an implementer, and an ordinary run never pays for a decomposition it did not ask
    for.
    """
    planning = get_planning_config(config)
    agent_entry = {
        "agent": planning.get("agent") or "claude",
        "model": planning.get("model"),
        "role": "decomposer",
    }

    builder = StateGraph(OrchestratorState)
    builder.add_node("context", context_node)
    builder.add_node("preflight", preflight_node)
    builder.add_node(
        "decomposer_0",
        make_role_node("decomposer", agent_override=agent_entry),
    )
    builder.add_node("decomposer", make_sync_node("decomposer", 1))
    builder.add_node("finalize", finalize_node)

    builder.add_edge(START, "context")
    builder.add_edge("context", "preflight")
    builder.add_edge("preflight", "decomposer_0")
    builder.add_edge("decomposer_0", "decomposer")
    builder.add_edge("decomposer", "finalize")
    builder.add_edge("finalize", END)
    return builder.compile()


# `from orchestrator.graph import graph` stays available for LangGraph's own tooling
# (langgraph.json) and for tests. Its topology is fixed at import time by the project's
# orchestrator.yaml, so the CLI does not use it: `__main__` calls `build_graph` with the
# configuration resolved for the run, because the topology depends on that configuration.
try:
    _initial_config = load_config(None, get_project_root())
except Exception:
    _initial_config = {"agents": [{"role": "researcher"}, {"role": "planner"}, {"role": "implementer"}, {"role": "verifier"}]}

graph = build_graph(_initial_config)
