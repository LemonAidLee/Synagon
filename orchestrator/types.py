"""Core data models and helper functions for structured agent orchestration.

Key Concepts:
- Agent: The execution provider/tool (e.g., "antigravity", "claude", "opencode").
- Role: The responsibility assigned to that agent (e.g., "researcher", "planner", "implementer").
- AgentResult: The standardized, structured output of a single agent execution.
- State: The shared LangGraph working memory during a workflow execution.
"""

from typing import Optional, List, Dict, Any, Tuple, TypedDict

from orchestrator.skills.types import SkillInfo


class TokenUsage(TypedDict, total=False):
    """Structured token usage information for an agent execution."""
    input_tokens: Optional[int]
    output_tokens: Optional[int]
    total_tokens: Optional[int]
    cache_read_tokens: Optional[int]
    cache_write_tokens: Optional[int]
    reasoning_tokens: Optional[int]
    available: bool
    raw_usage: Optional[Dict[str, Any]]


def create_token_usage(
    input_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
    total_tokens: Optional[int] = None,
    cache_read_tokens: Optional[int] = None,
    cache_write_tokens: Optional[int] = None,
    reasoning_tokens: Optional[int] = None,
    available: bool = True,
    raw_usage: Optional[Dict[str, Any]] = None,
) -> TokenUsage:
    """Helper to construct a TokenUsage dictionary.

    Args:
        input_tokens: Number of input/prompt tokens.
        output_tokens: Number of output/generation tokens.
        total_tokens: Total tokens consumed (calculated as input+output if omitted).
        cache_read_tokens: Optional cached prompt tokens read.
        cache_write_tokens: Optional cached prompt tokens written/created.
        reasoning_tokens: Optional thinking/reasoning tokens.
        available: True if usage was reliably reported by CLI metadata, False otherwise.
        raw_usage: Optional raw dictionary directly reported by provider CLI.

    Returns:
        Structured TokenUsage dictionary adhering to project token contract.
    """
    if available and total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens

    res: TokenUsage = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "available": available,
    }
    if cache_read_tokens is not None:
        res["cache_read_tokens"] = cache_read_tokens
    if cache_write_tokens is not None:
        res["cache_write_tokens"] = cache_write_tokens
    if reasoning_tokens is not None:
        res["reasoning_tokens"] = reasoning_tokens
    if raw_usage is not None:
        res["raw_usage"] = raw_usage

    return res


def unavailable_token_usage() -> TokenUsage:
    """Return a standardized TokenUsage indicating usage is unavailable."""
    return {
        "input_tokens": None,
        "output_tokens": None,
        "total_tokens": None,
        "available": False,
    }


def create_token_diagnostics(
    agent: str,
    role: str,
    execution_mode: Optional[str],
    token_usage: TokenUsage,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Construct structured diagnostic record for an agent execution's token usage.

    Enforces the single Token Accounting Contract across providers.
    """
    available = token_usage.get("available", False)
    tot = token_usage.get("total_tokens") if available else None
    return {
        "agent": agent,
        "role": role,
        "execution_mode": execution_mode or "unknown",
        "session_id": session_id,
        "usage_raw": token_usage.get("raw_usage") or {},
        "usage_normalized": {
            "input": token_usage.get("input_tokens"),
            "output": token_usage.get("output_tokens"),
            "cached_read": token_usage.get("cache_read_tokens"),
            "cached_write": token_usage.get("cache_write_tokens"),
            "reasoning": token_usage.get("reasoning_tokens"),
            "total": tot,
        },
        "aggregation_contribution": tot if (available and isinstance(tot, int)) else 0,
    }


class AgentResult(TypedDict, total=False):
    """Standardized representation of an agent's execution result."""
    agent: str
    role: str
    status: str  # "success" or "error"
    output: str
    duration_seconds: float
    model: Optional[str]
    verdict: Optional[str]  # e.g., "PASS", "FAIL", or "UNKNOWN" for verifier role
    repair_attempt: Optional[int]  # Repair iteration index (e.g. 1, 2) if applicable
    token_usage: Optional[TokenUsage]  # Structured token usage metadata (Stage 7.6)
    execution_mode: Optional[str]  # e.g. "native_tui" or "headless" (Stage 7.5.2)
    attempts: Optional[int]  # How many executions this one result took; 1 when it worked first
    attempt_failures: Optional[List[str]]  # Why each earlier attempt failed, oldest first
    # What each earlier, failed attempt reported spending, attributed to the rung that spent it:
    # [{"attempt", "agent", "model", "duration_seconds", "token_usage"}], oldest first.
    attempt_token_usage: Optional[List[Dict[str, Any]]]


def usage_total(usage: Optional[Dict[str, Any]]) -> Optional[int]:
    """The total one TokenUsage reported, or None when it reported nothing usable.

    The one reading of a TokenUsage every sum in the project goes through, so a display, a
    budget and a stats report cannot disagree about what one execution cost.
    """
    if not isinstance(usage, dict) or usage.get("available") is not True:
        return None
    total = usage.get("total_tokens")
    if isinstance(total, int):
        return total
    inp, out = usage.get("input_tokens"), usage.get("output_tokens")
    if isinstance(inp, int) and isinstance(out, int):
        return inp + out
    return None


def result_token_rows(result: Dict[str, Any]) -> List[Tuple[Optional[Dict[str, Any]], Optional[int]]]:
    """Every separately-reported spend behind one AgentResult: failed attempts, then the result.

    Returns ``(attempt_record_or_None, total_or_None)`` pairs. The result's own usage is the last
    row, with ``None`` in the first slot. A retry is not an ensemble - there is still one result
    per phase - but a failed attempt that reported usage *spent* it, and dropping it would make
    every total and every budget decision quietly lower than what was actually paid.
    """
    rows: List[Tuple[Optional[Dict[str, Any]], Optional[int]]] = []
    for attempt in result.get("attempt_token_usage") or []:
        if isinstance(attempt, dict):
            rows.append((attempt, usage_total(attempt.get("token_usage"))))
    rows.append((None, usage_total(result.get("token_usage"))))
    return rows


def result_tokens_spent(result: Dict[str, Any]) -> Tuple[int, bool]:
    """Known tokens behind one AgentResult, including its failed attempts, and whether all
    of it was reported. Unreported spend contributes zero and makes the answer incomplete."""
    known = 0
    complete = True
    for _attempt, total in result_token_rows(result):
        if total is None:
            complete = False
        else:
            known += total
    return known, complete


class VerificationRecord(TypedDict, total=False):
    """Structured record of an individual verification evaluation in workflow history."""
    attempt: int
    repair_attempts: int
    verdict: str
    output: str
    duration_seconds: float
    model: Optional[str]
    token_usage: Optional[TokenUsage]


def create_agent_result(
    agent: str,
    role: str,
    status: str,
    output: str = "",
    duration_seconds: float = 0.0,
    model: Optional[str] = None,
    verdict: Optional[str] = None,
    repair_attempt: Optional[int] = None,
    token_usage: Optional[TokenUsage] = None,
    execution_mode: Optional[str] = None,
    attempts: Optional[int] = None,
    attempt_failures: Optional[List[str]] = None,
    attempt_token_usage: Optional[List[Dict[str, Any]]] = None,
) -> AgentResult:
    """Helper to construct a validated AgentResult dictionary.

    Args:
        agent: Name of the execution provider (e.g., 'antigravity', 'claude').
        role: Functional role assigned to the agent (e.g., 'researcher', 'planner', 'verifier').
        status: Execution status ('success' or 'error').
        output: Text output produced by the agent.
        duration_seconds: Execution time in seconds.
        model: Optional model name selected for the agent.
        verdict: Optional verification verdict ('PASS', 'FAIL', 'UNKNOWN') for verifier role.
        repair_attempt: Optional repair iteration index if this execution is a repair or post-repair verification.
        token_usage: Optional TokenUsage dictionary containing reliable CLI usage metadata.
        execution_mode: Optional execution mode ("native_tui" or "headless").
        attempts: How many executions produced this one result. One result per phase however
            many attempts it took - a retry is not an ensemble, and consensus and `--stats`
            both count results.
        attempt_failures: Why each earlier attempt failed, oldest first, so "it was flaky" and
            "it is broken" stay distinguishable after the run.
        attempt_token_usage: What each earlier, failed attempt reported spending, attributed to
            the agent and model that spent it. Counted by every total and budget, never lost.

    Returns:
        A structured AgentResult TypedDict instance.
    """
    res: AgentResult = {
        "agent": agent,
        "role": role,
        "status": status,
        "output": output,
        "duration_seconds": round(max(0.0, duration_seconds), 2),
        "model": model,
        "token_usage": token_usage if token_usage is not None else unavailable_token_usage(),
    }
    if verdict is not None:
        res["verdict"] = verdict
    if repair_attempt is not None:
        res["repair_attempt"] = repair_attempt
    if execution_mode is not None:
        res["execution_mode"] = execution_mode
    # Only carried when it says something: every result would otherwise gain `attempts: 1`,
    # which is noise in a stored event and in every diff of one.
    if attempts is not None and attempts > 1:
        res["attempts"] = attempts
    if attempt_failures:
        res["attempt_failures"] = list(attempt_failures)
    if attempt_token_usage:
        res["attempt_token_usage"] = [dict(a) for a in attempt_token_usage]
    return res


def get_agent_result(
    agent_results: List[AgentResult],
    role: Optional[str] = None,
    agent: Optional[str] = None,
) -> Optional[AgentResult]:
    """Retrieve the most recent AgentResult matching the specified role and/or agent.

    Args:
        agent_results: List of AgentResult objects from workflow state.
        role: Optional role name to filter by (e.g., 'researcher').
        agent: Optional agent name to filter by (e.g., 'antigravity').

    Returns:
        The matching AgentResult if found, or None.
    """
    if not agent_results:
        return None

    # Search backwards for the most recent match
    for res in reversed(agent_results):
        role_match = (role is None) or (res.get("role") == role)
        agent_match = (agent is None) or (res.get("agent") == agent)
        if role_match and agent_match:
            return res

    return None


def get_latest_role_outputs(
    agent_results: List[AgentResult],
    role: str,
) -> List[AgentResult]:
    """Retrieve the most recent contiguous block of AgentResults for a specific role.
    
    This is used to fetch all parallel executions of a role from the latest phase,
    ignoring older executions from previous repair loops.
    """
    if not agent_results:
        return []

    results = []
    found_role = False

    # Search backwards
    for res in reversed(agent_results):
        if res.get("role") == role:
            found_role = True
            results.insert(0, res)  # maintain original order
        elif found_role:
            # We found the block of our role, and now hit a different role. Stop.
            break

    return results
