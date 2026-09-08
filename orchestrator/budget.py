"""Run budgets: the ceiling the escalation ladder assumes exists.

`max_repair_attempts` bounds how many *attempts* a run may make. It says nothing
about what those attempts cost. With a model escalation ladder configured, one
task can put three agents through three attempts, each one more expensive than
the last, with no upper bound on tokens or wall time.

This module supplies that bound. It is deliberately thin, because the
orchestrator already records exact per-execution token usage and durations: the
budget is read off those same durable facts rather than tracked separately.

Load-bearing rules
------------------
* **Pure.** No I/O, no mutation, no globals - like `orchestrator.status`, which
  this module feeds.
* **Never guess.** Only token counts an agent actually reported are counted.
  An execution whose usage is unavailable contributes zero, so an unreported
  agent can never fabricate an overrun (see the token accounting contract).
* **Never interrupt work in flight.** The budget is checked at decision points -
  before spending another repair attempt - not mid-execution. A ceiling cannot
  un-spend the tokens of a call already made, and killing an agent halfway
  wastes everything it has already cost.
* **Unlimited is the default.** 0 means no ceiling, so a user who never
  configures a budget sees exactly the behavior they had before.
"""

import time
from typing import Any, Dict, List, Optional, TypedDict


class BudgetState(TypedDict, total=False):
    """What a run has spent, against what it is allowed to spend."""
    tokens_spent: int
    max_total_tokens: int
    seconds_elapsed: float
    max_duration_seconds: int
    exhausted: bool
    reason: Optional[str]


def tokens_spent(agent_results: Optional[List[Dict[str, Any]]]) -> int:
    """Sum the tokens every execution actually reported.

    Mirrors `orchestrator.metrics.aggregate_metrics`: an execution whose usage
    is unavailable contributes nothing rather than an estimate.
    """
    total = 0
    for result in agent_results or []:
        usage = result.get("token_usage") or {}
        if usage.get("available") is not True:
            continue
        value = usage.get("total_tokens")
        if isinstance(value, int):
            total += value
            continue
        inp = usage.get("input_tokens")
        out = usage.get("output_tokens")
        if isinstance(inp, int) and isinstance(out, int):
            total += inp + out
    return total


def seconds_elapsed(state: Any, now: Optional[float] = None) -> float:
    """Return how long the run has been going.

    Prefers the wall clock from the run's start, because that is what a
    `max_duration_seconds` ceiling means to a person watching it. Falls back to
    the sum of execution durations when the start time is absent - for a
    reconstructed or partial state, that is the only honest number available.
    """
    if not state:
        return 0.0
    started = state.get("run_started_at")
    if isinstance(started, (int, float)) and started > 0:
        return max(0.0, (time.time() if now is None else float(now)) - float(started))
    return round(
        sum(float(r.get("duration_seconds") or 0.0) for r in state.get("agent_results") or []),
        2,
    )


def evaluate_budget(
    state: Any,
    budget: Optional[Dict[str, Any]] = None,
    now: Optional[float] = None,
) -> BudgetState:
    """Measure a run against its ceiling.

    Args:
        state: The orchestrator state, or any mapping carrying the same facts.
        budget: The resolved BudgetConfig; read from ``state['budget']`` when
            omitted.
        now: Current epoch seconds; injectable so the decision is testable.

    Returns:
        A BudgetState. ``exhausted`` is True only when a configured, non-zero
        limit has been reached, and ``reason`` then says which one, in the
        concrete numbers a person can act on.
    """
    state = state or {}
    if budget is None:
        budget = state.get("budget") or {}

    try:
        max_tokens = max(0, int(budget.get("max_total_tokens") or 0))
    except (TypeError, ValueError):
        max_tokens = 0
    try:
        max_seconds = max(0, int(budget.get("max_duration_seconds") or 0))
    except (TypeError, ValueError):
        max_seconds = 0

    spent = tokens_spent(state.get("agent_results"))
    elapsed = seconds_elapsed(state, now=now)

    reason: Optional[str] = None
    if max_tokens and spent >= max_tokens:
        reason = (
            f"token budget exhausted: {spent:,} of {max_tokens:,} tokens spent "
            f"(budget.max_total_tokens)"
        )
    elif max_seconds and elapsed >= max_seconds:
        reason = (
            f"time budget exhausted: {elapsed:.0f}s of {max_seconds}s elapsed "
            f"(budget.max_duration_seconds)"
        )

    return {
        "tokens_spent": spent,
        "max_total_tokens": max_tokens,
        "seconds_elapsed": round(elapsed, 2),
        "max_duration_seconds": max_seconds,
        "exhausted": reason is not None,
        "reason": reason,
    }


def is_exhausted(state: Any, budget: Optional[Dict[str, Any]] = None) -> bool:
    """Return True when the run has reached a configured ceiling."""
    return bool(evaluate_budget(state, budget).get("exhausted"))


def describe_budget(budget_state: BudgetState) -> str:
    """Render a one-line account of what a run spent against its ceiling."""
    if not budget_state:
        return "Budget: not configured."
    spent = budget_state.get("tokens_spent") or 0
    elapsed = budget_state.get("seconds_elapsed") or 0.0
    max_tokens = budget_state.get("max_total_tokens") or 0
    max_seconds = budget_state.get("max_duration_seconds") or 0

    token_part = f"{spent:,} tokens" + (f" / {max_tokens:,}" if max_tokens else " (no limit)")
    time_part = f"{elapsed:.0f}s" + (f" / {max_seconds}s" if max_seconds else " (no limit)")
    line = f"Budget: {token_part}, {time_part}."
    if budget_state.get("reason"):
        line += f" {budget_state['reason']}."
    return line
