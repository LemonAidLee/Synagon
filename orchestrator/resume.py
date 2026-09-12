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

from datetime import datetime
from typing import Any, Dict, List, Optional

from orchestrator.store import (
    EVENT_ACCEPTANCE,
    EVENT_AGENT_RESULT,
    EVENT_AGENT_RETRY,
    EVENT_AGENT_STARTED,
    EVENT_RUN_RESUMED,
    EVENT_RUN_STARTED,
    EVENT_VERIFICATION,
    load_run,
    resolve_run_id,
)
from orchestrator.types import unavailable_token_usage, usage_total
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
    acceptance_checks: List[Dict[str, Any]] = []
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
        elif name == EVENT_ACCEPTANCE:
            check = dict(event.get("check") or {})
            if check:
                check[RESTORED] = True
                acceptance_checks.append(check)
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
        # The gate results are evidence the recorded verdicts were given against. A replayed
        # verification must be judged with the gate its verifier saw, not with a fresh run of
        # the command over a workspace that may have changed since (see `should_replay_gate`).
        "acceptance_checks": acceptance_checks,
        "interrupted_attempts": orphaned_attempts(events),
        # The wall-clock budget is a ceiling on the *run*, and a resumed run is the same run
        # continuing: the time its earlier sessions were working counts against it. The time it
        # spent stopped does not - a run resumed the next morning is not twelve hours over.
        "prior_elapsed_seconds": active_seconds(events),
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


def _timestamp(event: Dict[str, Any]) -> Optional[float]:
    raw = event.get("timestamp")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def active_seconds(events: List[Dict[str, Any]]) -> float:
    """How long a run's sessions were working, summed: the time a resumed run has already used.

    A session runs from its `run_started` (or `run_resumed`) event to the last event it wrote.
    The gap between one session's last event and the next session's start - the process was
    dead, or nobody had resumed it yet - is not counted: it spent nothing. What a killed session
    did after its last event is unknowable and is not guessed at, so this is a floor.
    """
    total = 0.0
    start: Optional[float] = None
    last: Optional[float] = None
    for event in events or []:
        stamp = _timestamp(event)
        if stamp is None:
            continue
        if event.get("event") in (EVENT_RUN_STARTED, EVENT_RUN_RESUMED):
            if start is not None and last is not None:
                total += max(0.0, last - start)
            start = last = stamp
        elif start is not None:
            last = stamp
    if start is not None and last is not None:
        total += max(0.0, last - start)
    return round(total, 2)


#: Why an execution that was running when its process stopped is an attempt with unknown spend.
IN_FLIGHT_REASON = "in flight when the orchestrator stopped; it never reported its usage"


def orphaned_attempts(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Attempts whose spend no recorded result accounts for.

    Two kinds, both marked ``interrupted`` and shaped like `attempt_token_usage` entries:

    * **A failed attempt the process died after.** A failed attempt's usage reaches the durable
      record twice: on its `agent_retry` event, at the moment it failed, and on the phase's
      eventual `AgentResult` (`attempt_token_usage`), which is what every total reads. A process
      killed between the two leaves the spend only on the event, and a resume that reads results
      alone would never count it: measured, a hard kill after a failed attempt that reported
      700 tokens resumed to a total 700 lower than what was paid.
    * **The attempt the process died during** (Package C). An `agent_started` with no result
      after it was running when the process stopped. Whatever it cost was never reported, so it
      is recorded as an attempt with *unavailable* usage (`in_flight`): the run's total becomes
      a floor that says so, instead of a complete-looking number that silently omits it
      (invariant 5). Its tokens are never estimated. An execution that was between attempts (in
      a retry's backoff) when it stopped is indistinguishable here and is marked the same way:
      the error is in the direction of saying "incomplete", never of inventing a number.

    An event is matched to the result that later carried it by role, attempt number, agent,
    model and reported total, so interleaved ensemble members cannot claim each other's
    attempts. A `run_resumed` event closes every execution still open before it: a new session
    means the previous process is gone. What is left over is returned, oldest first.
    """
    pending: List[Dict[str, Any]] = []
    running: List[Dict[str, Any]] = []  # executions started and not yet ended, oldest first

    def end_running(role: Any, agent: Any, model: Any) -> None:
        mine = [e for e in running if e["role"] == role]
        if not mine:
            return
        exact = [e for e in mine if (e["agent"], e["model"]) == (agent, model)]
        running.remove((exact or mine)[0])

    def abandon_running(executions: Optional[List[Dict[str, Any]]] = None) -> None:
        chosen = list(running) if executions is None else list(executions)
        for execution in chosen:
            running.remove(execution)
            pending.append(
                {
                    "role": execution["role"],
                    "attempt": execution["attempts"] + 1,
                    "agent": execution["agent"],
                    "model": execution["model"],
                    "reason": IN_FLIGHT_REASON,
                    "token_usage": unavailable_token_usage(),
                    "interrupted": True,
                    "in_flight": True,
                }
            )

    for event in events or []:
        name = event.get("event")
        if name == EVENT_AGENT_STARTED:
            running.append(
                {
                    "role": event.get("role"),
                    "agent": event.get("agent"),
                    "model": event.get("model"),
                    "attempts": 0,
                }
            )
        elif name == EVENT_RUN_RESUMED:
            abandon_running()
        elif name == EVENT_AGENT_RETRY:
            for execution in running:
                if execution["role"] == event.get("role") and (
                    execution["agent"], execution["model"]
                ) == (event.get("agent"), event.get("model")):
                    # The next attempt runs on the rung the retry stepped to.
                    execution["attempts"] = int(event.get("attempt") or execution["attempts"] + 1)
                    execution["agent"] = event.get("next_agent") or execution["agent"]
                    execution["model"] = event.get("next_model", execution["model"])
                    break
            pending.append(
                {
                    "role": event.get("role"),
                    "attempt": event.get("attempt"),
                    "agent": event.get("agent"),
                    "model": event.get("model"),
                    "reason": event.get("reason"),
                    # An attempt with no recorded usage is still an attempt: it makes the total
                    # incomplete rather than disappearing (invariant 5).
                    "token_usage": dict(event.get("token_usage") or unavailable_token_usage()),
                    "interrupted": True,
                }
            )
        elif name == EVENT_AGENT_RESULT:
            result = event.get("result") or {}
            carried = [a for a in result.get("attempt_token_usage") or [] if a.get("interrupted")]
            mine = [e for e in running if e["role"] == result.get("role")]
            if carried and mine:
                # A result carrying interrupted attempts is, by construction, the execution that
                # replaced a killed one (graph._inherited_attempts). Its own start is the newest
                # open execution of its role; any older one still open is the execution the
                # process died during - closed here even when no `run_resumed` marks the session
                # boundary, and then matched below against what this result carried.
                running.remove(mine[-1])
                abandon_running(mine[:-1])
            else:
                end_running(result.get("role"), result.get("agent"), result.get("model"))
            for spent in result.get("attempt_token_usage") or []:
                for index, candidate in enumerate(pending):
                    if (
                        candidate["role"] == result.get("role")
                        and candidate["attempt"] == spent.get("attempt")
                        and candidate["agent"] == spent.get("agent")
                        and candidate["model"] == spent.get("model")
                        and usage_total(candidate["token_usage"]) == usage_total(spent.get("token_usage"))
                    ):
                        del pending[index]
                        break
    abandon_running()
    return pending


def should_replay_gate(state: Any, repair_attempts: int) -> bool:
    """Return True when the acceptance gate for this repair generation must not be re-run.

    The gate is replayed exactly when the verification it fed is replayed. Re-running it then
    would pair the recorded verdict with evidence gathered over a different workspace - a
    process killed partway through a repair leaves that repair's edits on disk, and a fresh
    gate over them combined with the replayed PASS of a verifier that never saw them was
    measured to end a run `completed` with the repair neither recorded nor verified. When the
    verification is *not* replayed (none was recorded, or it was BLOCKED and a person has
    acted), the gate runs fresh, because a fresh verifier will be judging a fresh workspace.
    """
    if not state or not state.get("resumed_from"):
        return False
    generation = int(repair_attempts or 0)
    recorded = [
        check
        for check in state.get("acceptance_checks") or []
        if check.get(RESTORED) and int(check.get("repair_attempts") or 0) == generation
    ]
    return bool(recorded) and should_replay(state, "verifier", repair_attempts=generation)


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
