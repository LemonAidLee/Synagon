"""Resuming a recorded run (Tier 1 #4, second half).

The run store already persists every fact a run produces, as it produces it.
That makes resume tractable rather than speculative: state is facts, status is
derived from them, and the event log holds the whole history. Without this, a
crash six minutes into a run - a dropped connection, a closed laptop, a killed
terminal - throws away every token already spent.

How it works
------------
`reconstruct_state` replays a stored run's ``events.jsonl`` back into an
`OrchestratorState`, marking every replayed record with ``restored: True``. The
graph then runs normally, and each phase asks `should_replay` whether the
resumed run already produced that phase's work. A replayed phase costs nothing:
no agent is launched, and the recorded result is reused verbatim.

Load-bearing rules
------------------
* **Replay is decided from facts, never from a counter.** "Has this phase's work
  already been done, for this repair attempt?" is answered by looking at the
  restored records, so it stays correct when the repair loop re-enters the same
  node several times.
* **Only successful work is replayed.** A phase whose every member errored is
  re-run, because there is nothing to reuse.
* **Never replay a phase the run had not reached.** Absence of a record means
  the phase runs, which is the safe direction: at worst the run repeats work it
  already did, and at best it continues exactly where it stopped.
* **A BLOCKED run is never replayed.** It stopped because it needed a person, so resuming it
  is that person saying they acted. Replaying the stored verdict would re-report a problem
  that has just been fixed, which is the one thing resume must not do.
* **The workspace has to come back too.** A finished run leaves only its branch,
  so resuming checks that branch back out (see
  `orchestrator.workspace.reattach_run_worktree`). When it cannot, the resumed
  run degrades to an un-isolated workspace and says so, exactly as a fresh run
  does.
"""

from typing import Any, Dict, List, Optional

from orchestrator.store import (
    EVENT_AGENT_RESULT,
    EVENT_RUN_STARTED,
    EVENT_VERIFICATION,
    load_run,
    resolve_run_id,
)
from orchestrator.workspace import WorkspaceInfo, reattach_run_worktree

#: Marks a record that came out of a stored run rather than this process.
RESTORED = "restored"

#: Roles whose work is a single artifact per run, replayable as a whole.
_SINGLE_PASS_ROLES = ("researcher", "planner")


def load_resumable_run(
    project_root: str,
    run_ref: str,
    directory: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Load a stored run by id, unique prefix, or ``latest``. None when missing."""
    resolved = resolve_run_id(project_root, run_ref, directory=directory)
    if not resolved:
        return None
    return load_run(project_root, resolved, directory=directory)


def reconstruct_state(
    run: Dict[str, Any],
    project_root: Optional[str] = None,
) -> Dict[str, Any]:
    """Rebuild an orchestrator state from a stored run's event log.

    Only durable facts are restored. Derived values - status, the resolved
    verdict, the summary - are deliberately left out, so the resumed run
    re-derives them from the facts exactly as a fresh run would.

    Args:
        run: A run as returned by `orchestrator.store.load_run`.
        project_root: Override for the project directory, when the run is being
            resumed from somewhere other than where it started.

    Returns:
        A partial `OrchestratorState` ready to be passed to ``graph.invoke``.
    """
    run = run or {}
    events: List[Dict[str, Any]] = run.get("events") or []

    agent_results: List[Dict[str, Any]] = []
    verification_history: List[Dict[str, Any]] = []
    workspace: Optional[WorkspaceInfo] = None
    task = run.get("task") or ""
    root = project_root or run.get("project_root")
    max_repair_attempts: Optional[int] = run.get("max_repair_attempts")

    for event in events:
        name = event.get("event")
        if name == EVENT_RUN_STARTED:
            task = event.get("task") or task
            root = project_root or event.get("project_root") or root
            if event.get("max_repair_attempts") is not None:
                max_repair_attempts = event.get("max_repair_attempts")
        elif name == EVENT_AGENT_RESULT:
            result = dict(event.get("result") or {})
            if result:
                result[RESTORED] = True
                agent_results.append(result)
        elif name == EVENT_VERIFICATION:
            record = dict(event.get("record") or {})
            if record:
                record[RESTORED] = True
                verification_history.append(record)
        elif name == "workspace":
            candidate = event.get("workspace")
            if isinstance(candidate, dict):
                workspace = dict(candidate)  # type: ignore[assignment]

    state: Dict[str, Any] = {
        "task": task,
        "project_root": root,
        "agent_results": agent_results,
        "verification_history": verification_history,
        "repair_attempts": repairs_completed(agent_results, verification_history),
        "resumed_from": run.get("run_id"),
        "run_id": run.get("run_id"),
        "run_dir": run.get("run_dir"),
    }
    if max_repair_attempts is not None:
        state["max_repair_attempts"] = int(max_repair_attempts)
    if workspace is not None:
        state["workspace"] = workspace
    return state


def repairs_completed(
    agent_results: List[Dict[str, Any]],
    verification_history: Optional[List[Dict[str, Any]]] = None,
) -> int:
    """Return how many repair attempts the stored run actually spent.

    Read from the results themselves rather than from a stored counter, so a run
    that crashed between incrementing and recording still resumes with the right
    number.
    """
    spent = 0
    for result in agent_results or []:
        attempt = result.get("repair_attempt")
        if isinstance(attempt, int) and result.get("status") == "success":
            spent = max(spent, attempt)
    for record in verification_history or []:
        attempts = record.get("repair_attempts")
        if isinstance(attempts, int):
            spent = max(spent, attempts)
    return spent


def _restored(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [r for r in records or [] if r.get(RESTORED)]


def should_replay(
    state: Any,
    role: str,
    is_repair: bool = False,
    repair_attempts: int = 0,
) -> bool:
    """Return True when a phase's work is already present in the resumed run.

    Args:
        state: The orchestrator state.
        role: The phase's role.
        is_repair: True for the repair node, which is an implementer executing
            attempt ``repair_attempts + 1``.
        repair_attempts: Repairs completed *before* this node runs.

    Returns:
        True to reuse the recorded work and launch no agent.
    """
    if not state or not state.get("resumed_from"):
        return False

    results = _restored(state.get("agent_results") or [])

    if is_repair:
        # The repair node is about to execute attempt N+1; replay only if that
        # exact attempt is already recorded.
        target = int(repair_attempts) + 1
        return any(
            r.get("role") == "implementer"
            and r.get("status") == "success"
            and r.get("repair_attempt") == target
            for r in results
        )

    if role == "verifier":
        # One verification per repair generation. A record made after the same
        # number of repairs is this phase's work; anything older is not.
        generation = [
            record
            for record in _restored(state.get("verification_history") or [])
            if record.get("repair_attempts", 0) == int(repair_attempts)
        ]
        if any((r.get("verdict") or "").upper() == "BLOCKED" for r in generation):
            # A BLOCKED run stopped because it needed a person. Resuming it *is* the person
            # saying they have acted, so replaying the old verdict would answer a question
            # nobody asked again - the run must look at the workspace afresh.
            return False
        return bool(generation)

    if role == "implementer":
        # The initial implementation carries no repair index.
        return any(
            r.get("role") == "implementer"
            and r.get("status") == "success"
            and not r.get("repair_attempt")
            for r in results
        )

    if role in _SINGLE_PASS_ROLES:
        return any(r.get("role") == role and r.get("status") == "success" for r in results)

    return False


def replayable_roles(state: Any) -> List[str]:
    """List the roles a resumed run will replay on its first pass, for reporting."""
    if not state or not state.get("resumed_from"):
        return []
    repair_attempts = int(state.get("repair_attempts") or 0)
    roles: List[str] = []
    for role in ("researcher", "planner", "implementer", "verifier"):
        attempts = 0 if role != "verifier" else repair_attempts
        if should_replay(state, role, is_repair=False, repair_attempts=attempts):
            roles.append(role)
    return roles


def prepare_resumed_workspace(
    state: Dict[str, Any],
    directory: Optional[str] = None,
) -> Dict[str, Any]:
    """Check the resumed run's branch back out, so agents write where it left off.

    Mutates and returns `state`. When the workspace cannot be restored, the
    state carries an un-isolated workspace with the reason, which the graph
    reports exactly as it reports a fresh run that could not be isolated.
    """
    workspace = state.get("workspace")
    if not isinstance(workspace, dict) or not workspace.get("isolated"):
        return state

    project_root = state.get("project_root") or workspace.get("project_root") or ""
    state["workspace"] = reattach_run_worktree(
        project_root,
        str(state.get("run_id") or ""),
        branch=workspace.get("branch"),
        path=workspace.get("path"),
        directory=directory,
    )
    return state
