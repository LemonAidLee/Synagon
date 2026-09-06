"""Derived status computation (Tier 1 #5).

Load-bearing rule: **display status is never stored — it is computed at read time
from durable facts.**

The orchestrator's durable facts are:

* ``agent_results``        — what each agent actually produced
* ``verification_history`` — what each verification evaluation concluded
* ``repair_attempts`` / ``max_repair_attempts`` — how much of the repair budget is spent
* ``preflight``            — whether the environment was viable before launch
* ``error``                — a hard failure message, if one occurred

Everything a human (or a UI) wants to *display* is a pure function of those facts.
Storing a running ``status`` string alongside them lets the two disagree; deriving
it makes that class of bug unrepresentable.

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

#: Statuses from which the workflow cannot progress any further.
TERMINAL_STATUSES = frozenset(
    {
        STATUS_COMPLETED,
        STATUS_FAILED,
        STATUS_BLOCKED,
        STATUS_ERROR,
        STATUS_PREFLIGHT_FAILED,
    }
)

#: Statuses that represent an unsuccessful outcome (drives CLI exit codes).
UNSUCCESSFUL_STATUSES = frozenset(
    {
        STATUS_FAILED,
        STATUS_BLOCKED,
        STATUS_ERROR,
        STATUS_PREFLIGHT_FAILED,
    }
)

#: Mapping from a completed role to the progress status it implies.
_ROLE_PROGRESS: Dict[str, str] = {
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


def derive_verdict(state: Any, policy: str = "unanimous") -> Optional[str]:
    """Return the consensus verdict of the most recent verification attempt.

    A single verifier is simply the one-member case of consensus, so this is
    correct for both sequential and parallel verification.
    """
    if not state:
        return None
    group = latest_verification_group(state)
    if group:
        return resolve_consensus([rec.get("verdict") for rec in group], policy=policy)
    # Fall back to the flat alias for callers that only carry a partial state.
    return state.get("verification_verdict")


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
        # FAIL or UNKNOWN: the budget decides whether this is terminal.
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
    }
