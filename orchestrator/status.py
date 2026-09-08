"""Derived status computation (Tier 1 #5).

Load-bearing rule: **display status is never stored — it is computed at read time
from durable facts.**

The orchestrator's durable facts are:

* ``agent_results``        — what each agent actually produced
* ``verification_history`` — what each verification evaluation concluded
* ``repair_attempts`` / ``max_repair_attempts`` — how much of the repair budget is spent
* ``acceptance_checks``    — what the project's own test command actually did
* ``preflight``            — whether the environment was viable before launch
* ``budget_exhausted_reason`` — recorded when a run hit its cost ceiling
* ``error``                — a hard failure message, if one occurred

Everything a human (or a UI) wants to *display* is a pure function of those facts.
Storing a running ``status`` string alongside them lets the two disagree; deriving
it makes that class of bug unrepresentable.

The same rule applies one level up (Roadmap Phases 2-3). A **Task**'s state is derived from
the Session that carried it out, and a **Goal**'s status is derived from its Tasks' states. A
scheduler that wrote those down would have to remember to update them; deriving them means a
goal killed halfway still reports exactly what happened.

Every function in this module is pure: no I/O, no mutation, no globals.
"""

from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Status vocabulary
# ---------------------------------------------------------------------------

STATUS_PENDING = "pending"
STATUS_CONTEXT_PREPARED = "context_prepared"
STATUS_ANALYZED = "analyzed"
STATUS_PLANNED = "planned"
STATUS_IMPLEMENTED = "implemented"
STATUS_REPAIRED = "repaired"
STATUS_NEEDS_REPAIR = "needs_repair"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_BLOCKED = "blocked"
STATUS_ERROR = "error"
STATUS_PREFLIGHT_FAILED = "preflight_failed"
STATUS_BUDGET_EXHAUSTED = "budget_exhausted"

#: Statuses from which the workflow cannot progress any further.
TERMINAL_STATUSES = frozenset(
    {
        STATUS_COMPLETED,
        STATUS_FAILED,
        STATUS_BLOCKED,
        STATUS_ERROR,
        STATUS_PREFLIGHT_FAILED,
        STATUS_BUDGET_EXHAUSTED,
    }
)

#: Statuses that represent an unsuccessful outcome (drives CLI exit codes).
UNSUCCESSFUL_STATUSES = frozenset(
    {
        STATUS_FAILED,
        STATUS_BLOCKED,
        STATUS_ERROR,
        STATUS_PREFLIGHT_FAILED,
        STATUS_BUDGET_EXHAUSTED,
    }
)

#: Mapping from a completed role to the progress status it implies.
_ROLE_PROGRESS: Dict[str, str] = {
    "decomposer": STATUS_PLANNED,
    "researcher": STATUS_ANALYZED,
    "planner": STATUS_PLANNED,
    "implementer": STATUS_IMPLEMENTED,
}

_STATUS_DESCRIPTIONS: Dict[str, str] = {
    STATUS_PENDING: "Not started.",
    STATUS_CONTEXT_PREPARED: "Project context collected; awaiting first agent.",
    STATUS_ANALYZED: "Research complete.",
    STATUS_PLANNED: "Planning complete.",
    STATUS_IMPLEMENTED: "Implementation complete; awaiting verification.",
    STATUS_REPAIRED: "Repair applied; awaiting re-verification.",
    STATUS_NEEDS_REPAIR: "Verification failed; repair budget remains.",
    STATUS_COMPLETED: "Verification passed.",
    STATUS_FAILED: "Verification failed and the repair budget is exhausted.",
    STATUS_BLOCKED: "Verification requires human input; no agent can resolve it.",
    STATUS_ERROR: "The pipeline halted on an execution error.",
    STATUS_PREFLIGHT_FAILED: "Preflight checks failed; no agent was launched.",
    STATUS_BUDGET_EXHAUSTED: (
        "Verification failed and the run reached its token or time budget, so no "
        "further repair was attempted."
    ),
}


# ---------------------------------------------------------------------------
# Fact readers
# ---------------------------------------------------------------------------


def has_error(state: Any) -> bool:
    """Return True when the run has hit a fatal failure.

    ``error`` is the fatal flag; an errored ``AgentResult`` is only a fact.
    The distinction matters for ensembles: when three researchers run in
    parallel and one dies, the run should continue on the surviving two. A
    phase sets ``error`` only when *every* member of it failed, so one dead
    ensemble member no longer aborts the pipeline.
    """
    if not state:
        return False
    return bool(state.get("error"))


def latest_verification_group(state: Any) -> List[Dict[str, Any]]:
    """Return the verification records belonging to the most recent attempt.

    With parallel verifiers, one attempt produces several records sharing the
    same ``attempt`` number. This returns that whole group so a verdict can be
    resolved across it.
    """
    if not state:
        return []
    history: List[Dict[str, Any]] = state.get("verification_history") or []
    if not history:
        return []
    latest_attempt = max((rec.get("attempt") or 0) for rec in history)
    group = [rec for rec in history if (rec.get("attempt") or 0) == latest_attempt]
    return group or [history[-1]]


def resolve_consensus(
    verdicts: List[Optional[str]],
    policy: str = "unanimous",
) -> str:
    """Resolve several verifier verdicts into one, conservatively.

    Precedence is fixed regardless of policy:

    1. Any ``BLOCKED`` wins — if one verifier says a human is needed, that is
       true no matter what the others concluded.
    2. Otherwise the policy decides whether the PASSes carry the group.

    Policies:
      ``unanimous`` (default) - every verdict must be PASS to pass. This
        preserves the fail-safe contract: an ambiguous or dissenting verifier
        can never be outvoted into a false PASS.
      ``majority`` - a strict majority of PASS verdicts passes.
      ``any`` - a single PASS passes. Weakest; offered for completeness.

    Args:
        verdicts: The verdicts from one verification attempt.
        policy: Consensus policy name.

    Returns:
        "PASS", "FAIL", "BLOCKED", or "UNKNOWN".
    """
    cleaned = [(v or "UNKNOWN").upper() for v in verdicts if v is not None]
    if not cleaned:
        return "UNKNOWN"

    if "BLOCKED" in cleaned:
        return "BLOCKED"

    passes = cleaned.count("PASS")
    total = len(cleaned)

    if policy == "any":
        if passes:
            return "PASS"
    elif policy == "majority":
        if passes * 2 > total:
            return "PASS"
    else:  # unanimous
        if passes == total:
            return "PASS"

    # Not a pass. Prefer the concrete FAIL over the fail-safe UNKNOWN.
    if "FAIL" in cleaned:
        return "FAIL"
    return "UNKNOWN"


def acceptance_overrides_pass(state: Any) -> bool:
    """Return True when an objective gate result must veto a verifier's PASS.

    The gate is a fact the orchestrator produced itself; a verifier's verdict is an opinion
    about the same workspace. When they disagree and the gate is `required`, the fact wins —
    that is the entire reason the gate exists (Roadmap Phase 0).

    A gate that was never configured, or was skipped, vetoes nothing.
    """
    if not state:
        return False
    if not state.get("acceptance_required", True):
        return False
    from orchestrator.acceptance import gate_failed

    return gate_failed(
        state.get("acceptance_checks"),
        int(state.get("repair_attempts") or 0),
    )


def derive_verdict(state: Any, policy: str = "unanimous") -> Optional[str]:
    """Return the verdict of the most recent verification attempt.

    A single verifier is simply the one-member case of consensus, so this is
    correct for both sequential and parallel verification.

    Precedence:

    1. ``BLOCKED`` from any verifier — a human is needed regardless of what any test says.
    2. A failed required acceptance gate — a red suite cannot be talked into a PASS.
    3. The consensus of the verifiers.
    """
    if not state:
        return None
    group = latest_verification_group(state)
    if not group:
        # Fall back to the flat alias for callers that only carry a partial state.
        return state.get("verification_verdict")

    verdict = resolve_consensus([rec.get("verdict") for rec in group], policy=policy)
    if verdict == "BLOCKED":
        return verdict
    if verdict == "PASS" and acceptance_overrides_pass(state):
        return "FAIL"
    return verdict


def derive_blocked_reason(state: Any) -> Optional[str]:
    """Return the human-action text from whichever verifier reported BLOCKED."""
    if not state:
        return None
    for rec in latest_verification_group(state):
        if (rec.get("verdict") or "").upper() == "BLOCKED":
            reason = rec.get("blocked_reason")
            if reason:
                return reason
    return state.get("blocked_reason")


def budget_exhausted_reason(state: Any) -> Optional[str]:
    """Return why the run stopped spending, when it did.

    The reason is a *fact*, recorded by the verifier's sync node at the moment
    it decided not to fund another repair attempt. Deriving it here instead
    would make status time-dependent: a stored run replayed days later would
    keep accruing wall-clock time against its old ceiling.
    """
    if not state:
        return None
    reason = state.get("budget_exhausted_reason")
    return str(reason) if reason else None


def preflight_blocked(state: Any) -> bool:
    """Return True when a strict preflight report failed."""
    if not state:
        return False
    report = state.get("preflight")
    if not isinstance(report, dict):
        return False
    return bool(report.get("strict")) and not report.get("ok", True)


# ---------------------------------------------------------------------------
# The derivation
# ---------------------------------------------------------------------------


def derive_status(state: Any) -> str:
    """Compute the workflow status purely from durable facts.

    Precedence, highest first:

    1. A failed strict preflight — nothing was launched.
    2. Any hard error fact.
    3. The latest verification verdict, interpreted against the repair budget.
    4. Otherwise, progress implied by the most recent successful agent result.

    Args:
        state: The orchestrator state (or any mapping carrying the same facts).

    Returns:
        One of the ``STATUS_*`` constants in this module.
    """
    if not state:
        return STATUS_PENDING

    if preflight_blocked(state):
        return STATUS_PREFLIGHT_FAILED

    if has_error(state):
        return STATUS_ERROR

    # A partial state carrying only errored executions is an error even without
    # the fatal flag (a phase in which every member failed).
    results: List[Dict[str, Any]] = state.get("agent_results") or []
    if results and all(r.get("status") == "error" for r in results):
        return STATUS_ERROR

    verdict = derive_verdict(state)
    if verdict:
        if verdict == "PASS":
            return STATUS_COMPLETED
        if verdict == "BLOCKED":
            return STATUS_BLOCKED
        # FAIL or UNKNOWN: the budgets decide whether this is terminal. The
        # cost ceiling is checked first, because when both are spent the run
        # stopped for the more specific reason.
        if budget_exhausted_reason(state):
            return STATUS_BUDGET_EXHAUSTED
        repair_attempts = int(state.get("repair_attempts") or 0)
        max_repair_attempts = state.get("max_repair_attempts")
        max_repair_attempts = 2 if max_repair_attempts is None else int(max_repair_attempts)
        if repair_attempts >= max_repair_attempts:
            return STATUS_FAILED
        return STATUS_NEEDS_REPAIR

    for res in reversed(results):
        if res.get("status") != "success":
            continue
        if res.get("repair_attempt"):
            return STATUS_REPAIRED
        progress = _ROLE_PROGRESS.get(res.get("role") or "")
        if progress:
            return progress

    if state.get("project_root"):
        return STATUS_CONTEXT_PREPARED

    return STATUS_PENDING


def is_terminal(status: str) -> bool:
    """Return True when no further workflow progress is possible from `status`."""
    return status in TERMINAL_STATUSES


def is_successful(status: str) -> bool:
    """Return True when `status` represents a successful outcome."""
    return status not in UNSUCCESSFUL_STATUSES


def describe_status(status: str) -> str:
    """Return a one-line human-readable explanation of a status value."""
    return _STATUS_DESCRIPTIONS.get(status, "Unrecognized status.")


def derive_summary(state: Any) -> Dict[str, Any]:
    """Derive the full display-facing view of a run from its durable facts.

    Useful for CLIs, run-store finalization, and any future UI: a single call
    that never needs the caller to trust a stored status string.
    """
    status = derive_status(state)
    history = (state or {}).get("verification_history") or []
    results = (state or {}).get("agent_results") or []
    max_repairs = (state or {}).get("max_repair_attempts")
    return {
        "status": status,
        "description": describe_status(status),
        "verdict": derive_verdict(state) or "UNKNOWN",
        "terminal": is_terminal(status),
        "successful": is_successful(status),
        "repair_attempts": int((state or {}).get("repair_attempts") or 0),
        "max_repair_attempts": 2 if max_repairs is None else int(max_repairs),
        "verification_evaluations": len(history),
        "agent_executions": len(results),
        "errored_executions": sum(1 for r in results if r.get("status") == "error"),
        "budget": (state or {}).get("budget_state") or {},
        "budget_exhausted_reason": budget_exhausted_reason(state),
        "acceptance_overrode_pass": acceptance_overrides_pass(state),
    }


# ---------------------------------------------------------------------------
# Tasks and goals (Roadmap Phases 2-3)
# ---------------------------------------------------------------------------

TASK_QUEUED = "queued"
TASK_RUNNING = "running"
TASK_DONE = "done"
TASK_FAILED = "failed"
TASK_BLOCKED = "blocked"
TASK_NEEDS_ATTENTION = "needs_attention"
TASK_SKIPPED_DEPENDENCY = "skipped_dependency"
TASK_SKIPPED_BUDGET = "skipped_budget"
TASK_SKIPPED_CONFLICT = "skipped_conflict"
TASK_AWAITING_APPROVAL = "awaiting_approval"

#: Task states from which no further progress happens without a person.
TASK_NEEDS_PERSON = frozenset(
    {TASK_BLOCKED, TASK_NEEDS_ATTENTION, TASK_SKIPPED_CONFLICT, TASK_AWAITING_APPROVAL}
)

#: Task states that did not deliver the task.
TASK_UNSUCCESSFUL = frozenset(
    {
        TASK_FAILED,
        TASK_BLOCKED,
        TASK_NEEDS_ATTENTION,
        TASK_SKIPPED_DEPENDENCY,
        TASK_SKIPPED_BUDGET,
        TASK_SKIPPED_CONFLICT,
        TASK_AWAITING_APPROVAL,
    }
)

_TASK_DESCRIPTIONS: Dict[str, str] = {
    TASK_QUEUED: "Waiting to start.",
    TASK_RUNNING: "A session is working on it.",
    TASK_DONE: "Its session verified successfully; the work is on its branch.",
    TASK_FAILED: "Its session finished without passing verification.",
    TASK_BLOCKED: "Its session needs a person before any agent can continue.",
    TASK_NEEDS_ATTENTION: "It edited the same files as a sibling task; a person must reconcile them.",
    TASK_SKIPPED_DEPENDENCY: "A task it depends on did not succeed, so it never started.",
    TASK_SKIPPED_BUDGET: "The goal reached its budget before this task started.",
    TASK_SKIPPED_CONFLICT: "Its dependencies could not be merged into one workspace.",
    TASK_AWAITING_APPROVAL: "An approval gate is waiting for a person before it can start.",
}

GOAL_COMPLETED = "completed"
GOAL_PARTIAL = "partial"
GOAL_FAILED = "failed"
GOAL_BLOCKED = "blocked"
GOAL_BUDGET_EXHAUSTED = "budget_exhausted"
GOAL_EMPTY = "empty"
GOAL_AWAITING_APPROVAL = "awaiting_approval"

_GOAL_DESCRIPTIONS: Dict[str, str] = {
    GOAL_COMPLETED: "Every task delivered.",
    GOAL_PARTIAL: "Some tasks delivered and some did not; the successful branches still stand.",
    GOAL_FAILED: "No task delivered.",
    GOAL_BLOCKED: "Progress needs a person: a task is blocked or two tasks collided.",
    GOAL_BUDGET_EXHAUSTED: "The goal reached its token or time ceiling before finishing.",
    GOAL_EMPTY: "There was nothing to run.",
    GOAL_AWAITING_APPROVAL: "An approval gate is waiting for a person.",
}


def derive_task_state(outcome: Optional[Dict[str, Any]]) -> str:
    """Derive a Task's state from what its Session recorded.

    A Task has no status of its own: it is exactly what happened to the Session that carried
    it out, plus the two things only the scheduler can see — that a dependency failed, or that
    a sibling touched the same files.
    """
    if not outcome:
        return TASK_QUEUED

    if outcome.get("skipped"):
        reason = str(outcome.get("skip_reason") or "")
        if reason == "budget":
            return TASK_SKIPPED_BUDGET
        if reason == "conflict":
            return TASK_SKIPPED_CONFLICT
        if reason == "approval":
            return TASK_AWAITING_APPROVAL
        return TASK_SKIPPED_DEPENDENCY

    if outcome.get("collided"):
        return TASK_NEEDS_ATTENTION

    session_status = str(outcome.get("status") or "")
    if not session_status:
        # A session that recorded an error never reached a status of its own; the error is
        # the fact, and it means the task did not deliver.
        if outcome.get("error"):
            return TASK_FAILED
        return TASK_RUNNING if outcome.get("started") else TASK_QUEUED

    if session_status == STATUS_BLOCKED:
        return TASK_BLOCKED
    if session_status == STATUS_COMPLETED:
        return TASK_DONE
    return TASK_FAILED


def derive_goal_status(task_states: List[str]) -> str:
    """Derive a Goal's status from the states of its Tasks.

    Precedence is chosen so the headline names the thing a person would act on first:
    a budget that stopped the work, then anything needing a person, then the delivery record.
    """
    states = [s for s in task_states if s]
    if not states:
        return GOAL_EMPTY

    if any(s == TASK_SKIPPED_BUDGET for s in states):
        return GOAL_BUDGET_EXHAUSTED
    if any(s == TASK_AWAITING_APPROVAL for s in states):
        return GOAL_AWAITING_APPROVAL
    if any(s in TASK_NEEDS_PERSON for s in states):
        return GOAL_BLOCKED

    done = sum(1 for s in states if s == TASK_DONE)
    if done == len(states):
        return GOAL_COMPLETED
    if done == 0:
        return GOAL_FAILED
    return GOAL_PARTIAL


def describe_task_state(state: str) -> str:
    """Return a one-line explanation of a Task state."""
    return _TASK_DESCRIPTIONS.get(state, "Unrecognized task state.")


def describe_goal_status(status: str) -> str:
    """Return a one-line explanation of a Goal status."""
    return _GOAL_DESCRIPTIONS.get(status, "Unrecognized goal status.")


def goal_is_successful(status: str) -> bool:
    """Return True when a Goal delivered everything it set out to."""
    return status == GOAL_COMPLETED


def derive_goal_summary(
    tasks: List[Dict[str, Any]],
    outcomes: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    """Derive the whole display-facing view of a Goal from its Tasks and their Sessions."""
    rows: List[Dict[str, Any]] = []
    for task in tasks or []:
        task_id = task.get("id")
        outcome = (outcomes or {}).get(task_id) or {}
        state = derive_task_state(outcome)
        rows.append(
            {
                "task_id": task_id,
                "title": task.get("title"),
                "state": state,
                "description": describe_task_state(state),
                "run_id": outcome.get("run_id"),
                "branch": outcome.get("branch"),
                "verdict": outcome.get("verdict"),
                "tokens": int(outcome.get("tokens") or 0),
                "duration_seconds": float(outcome.get("duration_seconds") or 0.0),
                "detail": outcome.get("detail") or outcome.get("blocked_reason"),
            }
        )

    status = derive_goal_status([r["state"] for r in rows])
    return {
        "status": status,
        "description": describe_goal_status(status),
        "successful": goal_is_successful(status),
        "tasks": rows,
        "task_count": len(rows),
        "done": sum(1 for r in rows if r["state"] == TASK_DONE),
        "needs_person": sum(1 for r in rows if r["state"] in TASK_NEEDS_PERSON),
        "unsuccessful": sum(1 for r in rows if r["state"] in TASK_UNSUCCESSFUL),
        "tokens": sum(r["tokens"] for r in rows),
        "duration_seconds": round(sum(r["duration_seconds"] for r in rows), 2),
    }


# ---------------------------------------------------------------------------
# Delivery (Roadmap Phase 6)
# ---------------------------------------------------------------------------
#
# Once a person presses *Ready to Merge*, the branch crosses the network: it is pushed and a
# pull request is opened. From that moment the forge owns facts this project does not — CI
# results and review decisions — and a **delivery record** is where those facts are written
# down when someone asks for them.
#
# The rule from §1 does not change shape here, only scale: the record holds what the forge
# said and when it said it; the *state* below is derived from that record at read time. That
# is what keeps the board honest and offline — a projection never reaches for the network, so
# a card can only ever be as current as the last recorded refresh, and it says so.

DELIVERY_LOCAL = "local"
DELIVERY_PUSHED = "pushed"
DELIVERY_PR_OPEN = "pr_open"
DELIVERY_CHECKS_RUNNING = "checks_running"
DELIVERY_CHECKS_FAILED = "checks_failed"
DELIVERY_CHANGES_REQUESTED = "changes_requested"
DELIVERY_READY = "ready"
DELIVERY_MERGED = "merged"
DELIVERY_CLOSED = "closed"
DELIVERY_FAILED = "failed"

#: Delivery states from which nothing moves without a person.
DELIVERY_NEEDS_PERSON = frozenset(
    {DELIVERY_CHECKS_FAILED, DELIVERY_CHANGES_REQUESTED, DELIVERY_CLOSED, DELIVERY_FAILED}
)

#: Delivery states that are still in flight on the forge.
DELIVERY_IN_FLIGHT = frozenset(
    {DELIVERY_PR_OPEN, DELIVERY_CHECKS_RUNNING}
)

_DELIVERY_DESCRIPTIONS: Dict[str, str] = {
    DELIVERY_LOCAL: "Nothing has crossed the network; the work is on a local branch.",
    DELIVERY_PUSHED: "The branch is on the remote. No pull request was opened.",
    DELIVERY_PR_OPEN: "A pull request is open and nothing has come back yet.",
    DELIVERY_CHECKS_RUNNING: "The forge is still running checks on the pull request.",
    DELIVERY_CHECKS_FAILED: "A check on the pull request failed.",
    DELIVERY_CHANGES_REQUESTED: "A reviewer asked for changes.",
    DELIVERY_READY: "Checks are green and review does not object.",
    DELIVERY_MERGED: "The pull request was merged.",
    DELIVERY_CLOSED: "The pull request was closed without merging.",
    DELIVERY_FAILED: "The delivery attempt itself did not succeed.",
}

#: What `summarize_checks` may report, in order of severity.
CHECKS_FAILING = "failing"
CHECKS_PENDING = "pending"
CHECKS_PASSING = "passing"
CHECKS_NONE = "none"


def derive_delivery_state(record: Optional[Dict[str, Any]]) -> str:
    """Derive what a delivery record means, from the facts it holds. Pure.

    Args:
        record: A delivery record, or None when the work was never delivered.

    Returns:
        One of the ``DELIVERY_*`` states. No record at all is `local`, which is the honest
        answer: a card whose work has not been pushed is not a card whose push failed.
    """
    if not record:
        return DELIVERY_LOCAL

    pull = record.get("pr") or {}
    forge_state = str(pull.get("state") or "").upper()
    if forge_state == "MERGED" or pull.get("merged_at"):
        return DELIVERY_MERGED
    if forge_state == "CLOSED":
        return DELIVERY_CLOSED

    if pull.get("number") or pull.get("url"):
        if str(pull.get("review_decision") or "").upper() == "CHANGES_REQUESTED":
            return DELIVERY_CHANGES_REQUESTED
        checks = str(pull.get("checks") or CHECKS_NONE).lower()
        if checks == CHECKS_FAILING:
            return DELIVERY_CHECKS_FAILED
        if checks == CHECKS_PENDING:
            return DELIVERY_CHECKS_RUNNING
        review = str(pull.get("review_decision") or "").upper()
        if checks == CHECKS_PASSING and review in ("", "APPROVED"):
            return DELIVERY_READY
        if checks == CHECKS_NONE and review == "APPROVED":
            return DELIVERY_READY
        return DELIVERY_PR_OPEN

    if record.get("pushed"):
        return DELIVERY_PUSHED
    if record.get("error"):
        return DELIVERY_FAILED
    return DELIVERY_LOCAL


def describe_delivery_state(state: str) -> str:
    """Return a one-line explanation of a delivery state."""
    return _DELIVERY_DESCRIPTIONS.get(state, "Unrecognized delivery state.")


def summarize_checks(rollup: Optional[List[Dict[str, Any]]]) -> str:
    """Reduce a forge's per-check detail to one word. Pure.

    Accepts GitHub's ``statusCheckRollup`` shape, whose entries are either check runs
    (``status`` plus ``conclusion``) or commit statuses (``state``). Anything unrecognized
    counts as pending rather than passing: an unread check is not a green one.
    """
    if not rollup:
        return CHECKS_NONE

    failing = False
    pending = False
    counted = 0

    for entry in rollup:
        if not isinstance(entry, dict):
            continue
        counted += 1
        conclusion = str(entry.get("conclusion") or "").upper()
        status = str(entry.get("status") or "").upper()
        state = str(entry.get("state") or "").upper()

        if conclusion in ("FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED",
                          "STARTUP_FAILURE") or state in ("FAILURE", "ERROR"):
            failing = True
        elif conclusion in ("SUCCESS", "NEUTRAL", "SKIPPED") or state == "SUCCESS":
            continue
        else:
            # Queued, in progress, expected, or a word this project has not seen before.
            pending = True

    if not counted:
        return CHECKS_NONE
    if failing:
        return CHECKS_FAILING
    if pending:
        return CHECKS_PENDING
    return CHECKS_PASSING
