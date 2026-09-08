"""Carrying a decomposed goal out (Roadmap Phases 2 and 3).

`--plan-only` produces a task graph. This module runs it: one **Session** per **Task**, in
dependency order, each in its own git worktree on its own branch — which is what turns a
pipeline into a team.

Sequential and parallel are the same code
-----------------------------------------
`delegation.max_parallel` is the only difference between Phase 2 and Phase 3. One means one
task at a time; more means a wave's independent tasks run at once. There is no separate
sequential path, because two schedulers would be two sets of bugs.

How a task's workspace is built
-------------------------------
A task with no dependencies starts from the goal's base commit. A task *with* dependencies
starts from the first one's branch and has the rest merged in, because a dependency that its
dependent cannot see is not a dependency at all.

That forward merge is not the auto-merge the isolation rules forbid: that rule protects the
user's branches, and this writes only into a scratch worktree the orchestrator just made. When
the merge conflicts, nothing is guessed — the task is skipped and a person is asked.

What this deliberately does not do
----------------------------------
* **It never merges results back.** A goal produces N branches and a report. Integrating them
  is a human decision, exactly as it is for a single run.
* **It does not resolve collisions.** When two sibling tasks edit the same file, both are
  flagged `needs_attention` with the overlapping paths named. Guessing which one was right is
  precisely the failure isolation exists to prevent.
* **It does not retry a task.** A task's session already owns repair attempts, escalation, and
  its own budget. A second session would be a second, unbounded loop around one that is
  already bounded.
* **It does not decide an approval.** A gate (Roadmap Phase 5) records a pending approval and
  the goal stops at a clean boundary. `--resume-goal` picks it up once a person has answered.
"""

import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from orchestrator.approvals import (
    GATE_AFTER_DECOMPOSITION,
    GATE_BEFORE_MERGE,
    GATE_BEFORE_TASK,
    STATUS_APPROVED,
    STATUS_REJECTED,
    find_approval,
    gate_state,
    list_approvals,
    request_approval,
)
from orchestrator.budget import tokens_spent
from orchestrator.config import (
    OrchestratorConfig,
    get_approval_config,
    get_budget_config,
    get_delegation_config,
    get_workspace_config,
)
from orchestrator.decompose import TaskPlan
from orchestrator.goals import GoalStore, open_goal
from orchestrator.status import (
    TASK_DONE,
    derive_goal_summary,
    derive_task_state,
)
from orchestrator.tracer import default_tracer
from orchestrator.workspace import (
    changed_paths,
    head_commit,
    is_git_repo,
)

#: Signature of the callable that runs one task. Injectable so the scheduler can be tested
#: without launching a pipeline.
SessionRunner = Callable[[Dict[str, Any]], Dict[str, Any]]


def render_task_brief(
    task: Dict[str, Any],
    goal: str,
    siblings: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Render one task as the instruction its session receives.

    The sibling list is not decoration. A session sees only its own task, so without knowing
    what else is being built it will helpfully implement its neighbours' work too — and two
    branches that both changed the same file is precisely the collision this design is trying
    to avoid.
    """
    lines: List[str] = [str(task.get("title") or task.get("id") or "Untitled task")]

    intent = str(task.get("intent") or "").strip()
    if intent:
        lines.append("")
        lines.append(intent)

    acceptance = [str(a) for a in (task.get("acceptance") or []) if str(a).strip()]
    if acceptance:
        lines.append("")
        lines.append("This task is done when:")
        for criterion in acceptance:
            lines.append(f"  - {criterion}")

    areas = [str(a) for a in (task.get("areas") or []) if str(a).strip()]
    if areas:
        lines.append("")
        lines.append("Expected to touch: " + ", ".join(areas))

    if goal:
        lines.append("")
        lines.append(f"This is one task of a larger goal: {goal}")

    others = [
        f"  - {s.get('title') or s.get('id')}"
        for s in (siblings or [])
        if s.get("id") != task.get("id")
    ]
    if others:
        lines.append("")
        lines.append(
            "Other tasks of this goal are being carried out separately, in their own "
            "workspaces. Do NOT implement them; if your work needs something they own, "
            "assume it will exist and keep your change to your own task:"
        )
        lines.extend(others)

    return "\n".join(lines)


def _ready_tasks(
    tasks: List[Dict[str, Any]],
    finished: Dict[str, str],
) -> Tuple[List[Dict[str, Any]], List[Tuple[Dict[str, Any], List[str]]]]:
    """Split the not-yet-run tasks into those that can start and those that never will.

    Returns:
        ``(ready, unrunnable)`` where `unrunnable` pairs each task with the dependencies that
        did not succeed.
    """
    ready: List[Dict[str, Any]] = []
    unrunnable: List[Tuple[Dict[str, Any], List[str]]] = []

    for task in tasks:
        if task["id"] in finished:
            continue
        deps = list(task.get("depends_on") or [])
        failed = [d for d in deps if finished.get(d) not in (None, TASK_DONE)]
        if failed:
            unrunnable.append((task, failed))
            continue
        if all(finished.get(d) == TASK_DONE for d in deps):
            ready.append(task)

    return ready, unrunnable


def _base_ref_for(
    task: Dict[str, Any],
    branches: Dict[str, Optional[str]],
    goal_base: str,
) -> Tuple[str, List[str]]:
    """Return the ref a task's worktree starts from, and the refs to merge into it."""
    deps = [d for d in (task.get("depends_on") or []) if branches.get(d)]
    if not deps:
        return goal_base, []
    return str(branches[deps[0]]), [str(branches[d]) for d in deps[1:]]


def _goal_budget_state(
    budget: Dict[str, Any],
    spent_tokens: int,
    started_at: float,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """Measure a goal against its ceiling. Pure; mirrors `budget.evaluate_budget`."""
    try:
        max_tokens = max(0, int(budget.get("goal_max_total_tokens") or 0))
    except (TypeError, ValueError):
        max_tokens = 0
    try:
        max_seconds = max(0, int(budget.get("goal_max_duration_seconds") or 0))
    except (TypeError, ValueError):
        max_seconds = 0

    elapsed = max(0.0, (time.time() if now is None else now) - started_at)

    reason: Optional[str] = None
    if max_tokens and spent_tokens >= max_tokens:
        reason = (
            f"goal token budget exhausted: {spent_tokens:,} of {max_tokens:,} tokens spent "
            f"(budget.goal_max_total_tokens)"
        )
    elif max_seconds and elapsed >= max_seconds:
        reason = (
            f"goal time budget exhausted: {elapsed:.0f}s of {max_seconds}s elapsed "
            f"(budget.goal_max_duration_seconds)"
        )

    return {
        "tokens_spent": spent_tokens,
        "max_total_tokens": max_tokens,
        "seconds_elapsed": round(elapsed, 2),
        "max_duration_seconds": max_seconds,
        "exhausted": reason is not None,
        "reason": reason,
    }


def _check_gate(
    project_root: str,
    config: OrchestratorConfig,
    gate: str,
    subject: str,
    detail: str = "",
    goal_id: Optional[str] = None,
    task_id: Optional[str] = None,
    run_id: Optional[str] = None,
    branch: Optional[str] = None,
) -> Tuple[str, Optional[Dict[str, Any]]]:
    """Resolve one approval gate for one subject (Roadmap Phase 5).

    An existing decision is never re-asked; an unconfigured gate is not a question at all.

    Returns:
        ``(state, approval)`` where state is ``approved`` (proceed), ``pending`` (stop and
        wait), or ``rejected``. An unconfigured gate is always ``approved``.
    """
    approval_cfg = get_approval_config(config)
    if not approval_cfg["gates"].get(gate, False):
        return STATUS_APPROVED, None

    directory = approval_cfg.get("directory")
    existing = find_approval(
        list_approvals(project_root, directory=directory),
        gate,
        goal_id=goal_id,
        task_id=task_id,
    )
    if existing:
        return gate_state(existing), existing

    approval = request_approval(
        project_root,
        gate=gate,
        subject=subject,
        detail=detail,
        goal_id=goal_id,
        task_id=task_id,
        run_id=run_id,
        branch=branch,
        auto_approve=bool(approval_cfg.get("auto_approve")),
        directory=directory,
    )
    default_tracer.log_approval_gate(
        gate=gate,
        subject=subject,
        state=str(approval.get("status")),
        approval_id=str(approval.get("id")),
    )
    return gate_state(approval), approval


def detect_collisions(
    project_root: str,
    outcomes: Dict[str, Dict[str, Any]],
    tasks: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Find pairs of tasks that changed the same file from the same base.

    Only siblings can collide: if one task depends on the other, the second was built *on top
    of* the first, so touching the same file is cooperation rather than conflict.

    Returns a list of ``{"task_ids": [a, b], "paths": [...]}``. Never raises.
    """
    if not is_git_repo(project_root):
        return []

    depends: Dict[str, Set[str]] = {
        str(t.get("id")): set(t.get("depends_on") or []) for t in tasks
    }

    touched: Dict[str, Set[str]] = {}
    for task_id, outcome in (outcomes or {}).items():
        branch = outcome.get("branch")
        base = outcome.get("base_ref")
        if not branch or not base:
            continue
        if derive_task_state(outcome) != TASK_DONE:
            continue
        paths = changed_paths(project_root, str(base), str(branch))
        if paths:
            touched[task_id] = set(paths)

    found: List[Dict[str, Any]] = []
    ids = sorted(touched)
    for i, left in enumerate(ids):
        for right in ids[i + 1:]:
            if right in depends.get(left, set()) or left in depends.get(right, set()):
                continue
            overlap = touched[left] & touched[right]
            if overlap:
                found.append({"task_ids": [left, right], "paths": sorted(overlap)})
    return found


def _record_skip_pending_plan(
    task: Dict[str, Any],
    gate: str,
    approval: Optional[Dict[str, Any]],
    outcomes: Dict[str, Dict[str, Any]],
    finished: Dict[str, str],
    store: Optional[GoalStore],
) -> None:
    """Mark every task of a plan that is still waiting to be approved."""
    detail = (
        "the plan was rejected"
        if gate == STATUS_REJECTED
        else f"the plan is waiting for approval {approval.get('id') if approval else ''}"
    )
    outcomes[task["id"]] = {
        "task_id": task["id"],
        "started": False,
        "skipped": True,
        "skip_reason": "approval",
        "detail": detail,
    }
    finished[task["id"]] = derive_task_state(outcomes[task["id"]])
    if store is not None:
        store.record_task_skipped(task["id"], "approval", detail)


def run_goal(
    goal: str,
    plan: TaskPlan,
    project_root: str,
    config: OrchestratorConfig,
    session_runner: SessionRunner,
    max_parallel: Optional[int] = None,
    goal_store: Optional[GoalStore] = None,
    store_enabled: bool = True,
    session_defaults: Optional[Dict[str, Any]] = None,
    resume_outcomes: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Run every task in a plan, in dependency order, and report what happened.

    Args:
        goal: The goal, in the user's words.
        plan: A validated TaskPlan from `orchestrator.decompose`.
        project_root: The project directory.
        config: The resolved configuration.
        session_runner: Called with the state for one task's session; returns the session's
            final state. Injected so the scheduler is testable without a pipeline.
        max_parallel: Overrides `delegation.max_parallel` for this goal.
        goal_store: An open GoalStore, or None to open one.
        store_enabled: Persist the goal. False for tests and dry runs.
        session_defaults: State every session starts from - the per-run options a user set on
            the command line, which must apply to each delegated session exactly as they would
            to a single run.
        resume_outcomes: Task outcomes replayed from a stored goal. Tasks that already
            delivered are not run again; everything else is picked up where it stopped.

    Returns:
        The goal's derived summary, plus `goal_id`, `collisions`, and `budget`.
    """
    tasks: List[Dict[str, Any]] = list(plan.get("tasks") or [])
    delegation = get_delegation_config(config)
    budget = dict(get_budget_config(config))
    workspace_cfg = get_workspace_config(config)
    concurrency = max(1, int(max_parallel or delegation.get("max_parallel", 1)))

    goal_base = head_commit(project_root) or "HEAD"
    started_at = time.time()

    store = goal_store
    if store is None and store_enabled:
        store = open_goal(
            project_root,
            goal,
            base_ref=goal_base,
            directory=delegation.get("goals_directory"),
            settings={
                "max_parallel": concurrency,
                "stop_on_failure": bool(delegation.get("stop_on_failure")),
                "isolation": workspace_cfg.get("isolation"),
                "budget": budget,
            },
        )
        if store is not None:
            store.record_plan(dict(plan))

    goal_id = store.goal_id if store is not None else None
    default_tracer.log_goal_start(
        goal=goal,
        goal_id=goal_id,
        task_count=len(tasks),
        waves=len(plan.get("order") or []),
        max_parallel=concurrency,
    )

    outcomes: Dict[str, Dict[str, Any]] = {}
    branches: Dict[str, Optional[str]] = {}
    finished: Dict[str, str] = {}
    spent_tokens = 0
    stop_reason: Optional[str] = None

    # Resuming: work that already delivered is replayed, not re-run. Anything else - failed,
    # skipped, held at a gate - is picked up again, because the reason it stopped may have
    # been answered since.
    replayed: List[str] = []
    for task_id, outcome in (resume_outcomes or {}).items():
        if derive_task_state(outcome) != TASK_DONE:
            continue
        outcomes[task_id] = dict(outcome)
        branches[task_id] = outcome.get("branch")
        finished[task_id] = TASK_DONE
        spent_tokens += int(outcome.get("tokens") or 0)
        replayed.append(task_id)
    if replayed:
        default_tracer.log_goal_resumed(goal_id or "", replayed)

    # The plan itself can be gated: a decomposition is worth reading before a team acts on it.
    plan_gate, plan_approval = _check_gate(
        project_root,
        config,
        GATE_AFTER_DECOMPOSITION,
        subject=goal,
        detail=f"{len(tasks)} task(s): " + ", ".join(str(t.get("title") or t.get("id")) for t in tasks),
        goal_id=goal_id,
    )
    if plan_gate != STATUS_APPROVED:
        for task in tasks:
            if task["id"] in finished:
                continue
            _record_skip_pending_plan(
                task, plan_gate, plan_approval, outcomes, finished, store
            )
        summary = derive_goal_summary(tasks, outcomes)
        summary.update(
            {
                "goal_id": goal_id,
                "goal": goal,
                "base_ref": goal_base,
                "max_parallel": concurrency,
                "collisions": [],
                "budget": _goal_budget_state(budget, spent_tokens, started_at),
                "wall_seconds": round(time.time() - started_at, 2),
                "awaiting_approval": plan_approval,
            }
        )
        if store is not None:
            store.record_goal_finished(summary)
            summary["goal_dir"] = store.goal_dir
        default_tracer.log_goal_complete(
            status=summary["status"], done=summary["done"], task_count=summary["task_count"]
        )
        return summary

    def _record_skip(task: Dict[str, Any], reason: str, detail: str) -> None:
        outcomes[task["id"]] = {
            "task_id": task["id"],
            "started": False,
            "skipped": True,
            "skip_reason": reason,
            "detail": detail,
        }
        finished[task["id"]] = derive_task_state(outcomes[task["id"]])
        if store is not None:
            store.record_task_skipped(task["id"], reason, detail)
        default_tracer.log_task_skipped(task["id"], reason, detail)

    while True:
        ready, unrunnable = _ready_tasks(tasks, finished)

        for task, failed_deps in unrunnable:
            _record_skip(
                task,
                "dependency",
                f"depends on {', '.join(failed_deps)}, which did not succeed",
            )

        if not ready:
            break

        if stop_reason:
            for task in ready:
                _record_skip(task, "dependency", stop_reason)
            continue

        # The goal's ceiling is checked between waves - the point where the next spend is
        # about to be authorised - never mid-session, for the same reason a session's own
        # budget is not checked mid-agent.
        budget_state = _goal_budget_state(budget, spent_tokens, started_at)
        if budget_state["exhausted"]:
            default_tracer.log_goal_budget_exhausted(budget_state["reason"] or "")
            for task in ready:
                _record_skip(task, "budget", budget_state["reason"] or "goal budget exhausted")
            continue

        # Each task can be authorised individually. A task held at its gate does not block
        # its siblings; only the tasks that depend on it wait.
        authorised: List[Dict[str, Any]] = []
        for task in ready:
            task_gate, task_approval = _check_gate(
                project_root,
                config,
                GATE_BEFORE_TASK,
                subject=str(task.get("title") or task["id"]),
                detail=str(task.get("intent") or ""),
                goal_id=goal_id,
                task_id=str(task["id"]),
            )
            if task_gate == STATUS_APPROVED:
                authorised.append(task)
            else:
                _record_skip(
                    task,
                    "approval",
                    (
                        f"rejected at the {GATE_BEFORE_TASK} gate"
                        if task_gate == STATUS_REJECTED
                        else f"waiting for approval {task_approval.get('id') if task_approval else ''}"
                    ),
                )
        if not authorised:
            continue

        wave = authorised[:concurrency] if concurrency < len(authorised) else authorised

        def _run_one(task: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
            task_id = str(task["id"])
            base_ref, merge_refs = _base_ref_for(task, branches, goal_base)
            brief = render_task_brief(task, goal, siblings=tasks)

            default_tracer.set_label(task_id)
            try:
                default_tracer.log_task_start(task_id, task.get("title") or "", base_ref)
                state = {
                    **(session_defaults or {}),
                    "task": brief,
                    "project_root": project_root,
                    "config": config,
                    "agent_results": [],
                    "goal_id": goal_id,
                    "task_id": task_id,
                    "workspace_base_ref": base_ref,
                    "workspace_merge_refs": merge_refs,
                }
                result = session_runner(state) or {}
            except Exception as exc:  # a crashed session must not kill the goal
                result = {"error": f"the session for task '{task_id}' crashed: {exc}"}
            finally:
                default_tracer.clear_label()

            workspace = result.get("workspace") or {}
            summary = result.get("summary") or {}
            merge = result.get("workspace_merge") or {}

            outcome: Dict[str, Any] = {
                "task_id": task_id,
                "started": True,
                "run_id": result.get("run_id"),
                "base_ref": base_ref,
                "branch": workspace.get("branch"),
                "status": result.get("status"),
                "verdict": result.get("verification_verdict") or summary.get("verdict"),
                "tokens": tokens_spent(result.get("agent_results")),
                "duration_seconds": float(summary.get("duration_seconds") or 0.0),
                "blocked_reason": result.get("blocked_reason"),
                "error": result.get("error"),
            }
            if merge.get("conflicted"):
                # The dependencies could not be brought together, so the task never had a
                # workspace worth running in.
                outcome.update(
                    {
                        "started": False,
                        "skipped": True,
                        "skip_reason": "conflict",
                        "detail": merge.get("error") or "dependency branches conflicted",
                    }
                )
            return task_id, outcome

        if len(wave) == 1:
            results = [_run_one(wave[0])]
        else:
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                results = list(pool.map(_run_one, wave))

        for task_id, outcome in results:
            outcomes[task_id] = outcome
            branches[task_id] = outcome.get("branch")
            state = derive_task_state(outcome)
            finished[task_id] = state
            spent_tokens += int(outcome.get("tokens") or 0)
            if store is not None:
                store.record_task_started(task_id, outcome.get("run_id"), outcome.get("base_ref"))
                store.record_task_finished(task_id, outcome)
            default_tracer.log_task_finished(
                task_id,
                state,
                branch=outcome.get("branch"),
                tokens=int(outcome.get("tokens") or 0),
                detail=outcome.get("detail") or outcome.get("blocked_reason") or outcome.get("error"),
            )

            if state == TASK_DONE:
                # Delivered work waits for a person to clear it, when asked to. This gate
                # never blocks anything: the work is already done and on its branch, and the
                # answer only decides whether the card reads "In Review" or "Ready to Merge".
                _check_gate(
                    project_root,
                    config,
                    GATE_BEFORE_MERGE,
                    subject=f"{task_id}: {outcome.get('branch') or 'no branch'}",
                    detail=f"delivered by session {outcome.get('run_id')}",
                    goal_id=goal_id,
                    task_id=task_id,
                    run_id=outcome.get("run_id"),
                    branch=outcome.get("branch"),
                )

            if state != TASK_DONE and delegation.get("stop_on_failure"):
                stop_reason = (
                    f"task '{task_id}' did not succeed and delegation.stop_on_failure is set"
                )

    # Collisions are found once every branch exists: the question is which tasks touched the
    # same files, and that cannot be known until they have.
    found = detect_collisions(project_root, outcomes, tasks)
    for collision in found:
        for task_id in collision["task_ids"]:
            if task_id in outcomes:
                outcomes[task_id]["collided"] = True
                outcomes[task_id]["collision_paths"] = collision["paths"]
        if store is not None:
            store.record_collision(collision["task_ids"], collision["paths"])
        default_tracer.log_collision(collision["task_ids"], collision["paths"])

    summary = derive_goal_summary(tasks, outcomes)
    summary["goal_id"] = goal_id
    summary["goal"] = goal
    summary["base_ref"] = goal_base
    summary["max_parallel"] = concurrency
    summary["collisions"] = found
    summary["budget"] = _goal_budget_state(budget, spent_tokens, started_at)
    summary["wall_seconds"] = round(time.time() - started_at, 2)
    summary["replayed"] = replayed

    if store is not None:
        store.record_goal_finished(summary)
        summary["goal_dir"] = store.goal_dir

    default_tracer.log_goal_complete(
        status=summary["status"],
        done=summary["done"],
        task_count=summary["task_count"],
        collisions=len(found),
    )
    return summary


def format_goal_summary(summary: Dict[str, Any]) -> str:
    """Render a goal's outcome as a readable report."""
    if not summary:
        return "No goal summary."

    lines: List[str] = []
    lines.append(f"GOAL: {summary.get('goal', '')}")
    lines.append(
        f"  {summary.get('done', 0)}/{summary.get('task_count', 0)} task(s) delivered "
        f"- {summary.get('status')}: {summary.get('description')}"
    )
    lines.append("")
    lines.append(f"  {'TASK':<22} {'STATE':<20} {'TOKENS':>9}  BRANCH")
    lines.append(f"  {'-' * 22} {'-' * 20} {'-' * 9}  {'-' * 30}")
    for row in summary.get("tasks") or []:
        branch = row.get("branch") or "-"
        lines.append(
            f"  {str(row.get('task_id'))[:22]:<22} {row.get('state', ''):<20} "
            f"{row.get('tokens', 0):>9,}  {branch}"
        )
        if row.get("detail"):
            lines.append(f"      {row['detail']}")

    collisions = summary.get("collisions") or []
    if collisions:
        lines.append("")
        lines.append("  COLLISIONS (sibling tasks that changed the same files)")
        for collision in collisions:
            lines.append(f"    {' + '.join(collision['task_ids'])}")
            for path in collision["paths"][:8]:
                lines.append(f"      {path}")
        lines.append("    Nothing was merged. Review these branches together before merging.")

    waiting = summary.get("awaiting_approval")
    if waiting:
        lines.append("")
        lines.append(f"  WAITING FOR YOU: {waiting.get('question')}")
        lines.append(f"    {waiting.get('detail')}")
        lines.append(f"    python -m orchestrator --approve {waiting.get('id')}")
        lines.append(f"    python -m orchestrator --resume-goal {summary.get('goal_id')}")

    budget = summary.get("budget") or {}
    if budget.get("reason"):
        lines.append("")
        lines.append(f"  BUDGET: {budget['reason']}")

    delivered = [
        r for r in (summary.get("tasks") or [])
        if r.get("state") == TASK_DONE and r.get("branch")
    ]
    if delivered:
        lines.append("")
        lines.append("  Review the delivered work:")
        for row in delivered:
            if row.get("branch"):
                lines.append(f"    git diff {summary.get('base_ref', 'HEAD')[:12]}...{row['branch']}")

    return "\n".join(lines)
