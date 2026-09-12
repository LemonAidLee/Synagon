"""Metrics aggregation and formatting module for orchestration token accounting and execution stats."""

from typing import Any, Dict, List, Optional, Set, TypedDict
from orchestrator.types import (
    AgentResult,
    SkillInfo,
    TokenUsage,
    VerificationRecord,
    result_token_rows,
)


class OrchestrationMetrics(TypedDict, total=False):
    """Aggregated execution and token metrics across all orchestration steps."""
    total_duration_seconds: float
    known_input_tokens: int
    known_output_tokens: int
    known_total_tokens: int
    total_executions: int
    executions_with_available_tokens: int
    executions_with_unavailable_tokens: int
    repair_attempts: int
    verification_passes: int
    verification_failures: int
    all_tokens_available: bool
    failed_attempts: int                              # retried/escalated attempts behind the results
    failed_attempt_tokens: int                        # known tokens those attempts reported
    failed_attempts_with_unavailable_tokens: int


def format_token_count(tokens: Optional[int], available: bool = True) -> str:
    """Format a token count for display.

    Args:
        tokens: Number of tokens, or None.
        available: Whether token tracking was available for this execution.

    Returns:
        Formatted string (e.g. "8,421" or "unavailable").
    """
    if not available or tokens is None:
        return "unavailable"
    return f"{tokens:,}"


def aggregate_metrics(
    agent_results: List[AgentResult],
    verification_history: Optional[List[VerificationRecord]] = None,
) -> OrchestrationMetrics:
    """Aggregate duration, token usage, and repair/verification counts across agent executions.

    Strict rules:
    - Never guess, extrapolate, or estimate missing tokens.
    - Only sum tokens where available is True and value is an int.
    - Track executions with unavailable tokens explicitly.
    - A failed retry/escalation attempt that reported usage spent it: its tokens are part of
      the total, attributed to the attempt (never to the rung that later succeeded), and an
      attempt whose usage is unknown makes the total incomplete exactly as a result would.

    Args:
        agent_results: Complete list of AgentResult records from workflow state.
        verification_history: Optional list of verification records.

    Returns:
        Structured OrchestrationMetrics dictionary.
    """
    total_duration = 0.0
    known_input = 0
    known_output = 0
    known_total = 0
    executions_with_available = 0
    executions_with_unavailable = 0
    repair_count = 0
    ver_passes = 0
    ver_failures = 0
    failed_attempts = 0
    failed_attempt_tokens = 0
    failed_attempts_unavailable = 0

    for res in agent_results:
        for attempt, attempt_total in result_token_rows(res)[:-1]:
            failed_attempts += 1
            total_duration += float((attempt or {}).get("duration_seconds") or 0.0)
            if attempt_total is None:
                failed_attempts_unavailable += 1
            else:
                failed_attempt_tokens += attempt_total
                known_total += attempt_total
            usage = (attempt or {}).get("token_usage") or {}
            if usage.get("available") is True:
                if isinstance(usage.get("input_tokens"), int):
                    known_input += usage["input_tokens"]
                if isinstance(usage.get("output_tokens"), int):
                    known_output += usage["output_tokens"]

        # Sum duration
        duration = res.get("duration_seconds") or 0.0
        total_duration += duration

        # Count repair attempts
        rep_attempt = res.get("repair_attempt")
        role = res.get("role", "")
        if role == "implementer" and rep_attempt is not None and rep_attempt > 0:
            repair_count = max(repair_count, rep_attempt)

        # Count verification verdicts
        verdict = res.get("verdict")
        if verdict == "PASS":
            ver_passes += 1
        elif verdict in ("FAIL", "UNKNOWN"):
            ver_failures += 1

        # Token usage accounting
        usage: Optional[TokenUsage] = res.get("token_usage")
        if usage and usage.get("available") is True:
            executions_with_available += 1
            inp = usage.get("input_tokens")
            out = usage.get("output_tokens")
            tot = usage.get("total_tokens")

            if isinstance(inp, int):
                known_input += inp
            if isinstance(out, int):
                known_output += out
            if isinstance(tot, int):
                known_total += tot
            elif isinstance(inp, int) and isinstance(out, int):
                known_total += (inp + out)
        else:
            executions_with_unavailable += 1

    # Check verification history if provided for additional verification counts
    if verification_history:
        ver_passes = sum(1 for v in verification_history if v.get("verdict") == "PASS")
        ver_failures = sum(1 for v in verification_history if v.get("verdict") in ("FAIL", "UNKNOWN"))

    total_executions = len(agent_results)
    all_available = (
        total_executions > 0
        and executions_with_unavailable == 0
        and failed_attempts_unavailable == 0
    )

    return {
        "total_duration_seconds": round(total_duration, 2),
        "known_input_tokens": known_input,
        "known_output_tokens": known_output,
        "known_total_tokens": known_total,
        "total_executions": total_executions,
        "executions_with_available_tokens": executions_with_available,
        "executions_with_unavailable_tokens": executions_with_unavailable,
        "repair_attempts": repair_count,
        "verification_passes": ver_passes,
        "verification_failures": ver_failures,
        "all_tokens_available": all_available,
        "failed_attempts": failed_attempts,
        "failed_attempt_tokens": failed_attempt_tokens,
        "failed_attempts_with_unavailable_tokens": failed_attempts_unavailable,
    }


def get_token_diagnostics(agent_results: List[AgentResult]) -> List[Dict[str, Any]]:
    """Produce structured diagnostic records for every execution's token accounting.

    Enforces the single Token Accounting Contract across all agents in the pipeline.
    """
    from orchestrator.types import create_token_diagnostics

    diagnostics: List[Dict[str, Any]] = []
    for res in agent_results:
        role = res.get("role", "unknown")
        mode = res.get("execution_mode", "unknown")
        for attempt, _total in result_token_rows(res):
            source = attempt if attempt is not None else res
            diag = create_token_diagnostics(
                agent=source.get("agent", "unknown"),
                role=role,
                execution_mode=mode,
                token_usage=source.get("token_usage") or {},
            )
            if attempt is not None:
                diag["failed_attempt"] = attempt.get("attempt")
                diag["model"] = attempt.get("model")
            diagnostics.append(diag)
    return diagnostics


def verify_token_aggregation_invariant(agent_results: List[AgentResult]) -> Dict[str, Any]:
    """Verify that the sum of displayed agent token values strictly matches the aggregated total.

    Enforces: sum(agent_tokens) == reported_total (Difference == 0).

    Returns:
        Dict with 'is_valid', 'sum_displayed', 'reported_total', and 'difference'.
    """
    metrics = aggregate_metrics(agent_results)
    # Summed from the rows `format_summary_table` draws - failed attempts included, each on its
    # own row - so "the rows add up to the total" is checked against what a person reads.
    displayed_sum = 0
    for row in summary_rows(agent_results):
        if row["tokens"] is not None:
            displayed_sum += row["tokens"]

    reported_total = metrics["known_total_tokens"]
    diff = reported_total - displayed_sum
    return {
        "is_valid": (diff == 0),
        "sum_displayed": displayed_sum,
        "reported_total": reported_total,
        "difference": diff,
    }


def format_skills_summary(
    skills: Optional[List[SkillInfo]],
    agent_results: Optional[List[AgentResult]] = None,
) -> str:
    """Format skills section for the final summary reporting available and referenced skills.

    Args:
        skills: List of discovered SkillInfo dictionaries.
        agent_results: List of AgentResult records from workflow state.

    Returns:
        Formatted multi-line skills summary string.
    """
    if not skills:
        return ""

    from orchestrator.skills.registry import SkillRegistry
    registry = SkillRegistry(skills)

    available_names = [s.get("name", "") for s in skills if s.get("name")]
    if not available_names:
        return ""

    all_referenced: Set[str] = set()
    if agent_results:
        for res in agent_results:
            output = res.get("output", "")
            refs = registry.detect_referenced_skills(output)
            for r in refs:
                all_referenced.add(r)

    lines = [
        "SKILLS",
        "-" * 54,
        f"Available:   {', '.join(available_names)}",
    ]
    if all_referenced:
        lines.append(f"Referenced:  {', '.join(sorted(all_referenced))}")
    else:
        lines.append("Referenced:  None")

    return "\n".join(lines)


def _agent_display(agent_raw: str) -> str:
    names = {"antigravity": "Antigravity", "claude": "Claude", "opencode": "OpenCode"}
    return names.get(str(agent_raw).lower(), str(agent_raw).capitalize())


def _execution_label(agent_raw: str, role_raw: str, rep_att: Optional[int]) -> str:
    agent_disp = _agent_display(agent_raw)
    if role_raw == "researcher":
        return f"{agent_disp} Researcher"
    if role_raw == "planner":
        return f"{agent_disp} Planner"
    if role_raw == "implementer":
        if rep_att is not None and rep_att > 0:
            return f"{agent_disp} Repair #{rep_att}"
        return f"{agent_disp} Implementer"
    if role_raw == "verifier":
        if rep_att is not None and rep_att > 0:
            return f"{agent_disp} Reverifier #{rep_att + 1}"
        return f"{agent_disp} Verifier"
    return f"{agent_disp} ({role_raw})"


def summary_rows(agent_results: List[AgentResult]) -> List[Dict[str, Any]]:
    """The rows of the final summary table: ``{label, duration_seconds, tokens}``.

    One row per AgentResult, preceded by one row per failed attempt behind it, each labelled
    with the agent that actually made that attempt. `tokens` is None when unreported.
    """
    rows: List[Dict[str, Any]] = []
    for res in agent_results:
        role_raw = res.get("role", "role")
        rep_att = res.get("repair_attempt")
        for attempt, total in result_token_rows(res):
            if attempt is None:
                label = _execution_label(res.get("agent", "agent"), role_raw, rep_att)
                duration = res.get("duration_seconds", 0.0) or 0.0
            else:
                base = _execution_label(attempt.get("agent") or "agent", role_raw, rep_att)
                # An attempt the process was killed during, recovered on resume from its
                # `agent_retry` event, is labelled as such rather than as this session's try.
                # One the process died *during* never reported anything: its row reads
                # "unavailable", and the label says why rather than implying it failed.
                if attempt.get("in_flight"):
                    when = "stopped mid-run, before resume"
                else:
                    when = "before resume" if attempt.get("interrupted") else "failed"
                label = f"{base} (try {attempt.get('attempt')}, {when})"
                duration = attempt.get("duration_seconds", 0.0) or 0.0
            rows.append({"label": label, "duration_seconds": float(duration), "tokens": total})
    return rows


def format_summary_table(
    agent_results: List[AgentResult],
    verification_verdict: Optional[str] = None,
    repair_attempts: int = 0,
    skills: Optional[List[SkillInfo]] = None,
) -> str:
    """Generate the standardized text summary table for the final orchestration report.

    Args:
        agent_results: List of AgentResult records.
        verification_verdict: Final verification verdict ("PASS", "FAIL", etc.).
        repair_attempts: Final repair attempt count.
        skills: Optional list of discovered SkillInfo records.

    Returns:
        Formatted multi-line summary string.
    """
    metrics = aggregate_metrics(agent_results)

    lines = [
        "=" * 50,
        "ORCHESTRATION COMPLETE",
        "=" * 50,
        "",
        f"{'Agent / Execution':<30} {'Duration':>10} {'Tokens':>12}",
        "-" * 54,
    ]

    for row in summary_rows(agent_results):
        duration_str = f"{row['duration_seconds']:.1f}s"
        tokens_str = f"{row['tokens']:,}" if row["tokens"] is not None else "unavailable"
        lines.append(f"{row['label']:<30} {duration_str:>10} {tokens_str:>12}")

    lines.append("-" * 54)

    total_duration_str = f"{metrics['total_duration_seconds']:.1f}s"

    if metrics["all_tokens_available"]:
        total_tokens_str = f"{metrics['known_total_tokens']:,}"
        lines.append(f"{'Total':<30} {total_duration_str:>10} {total_tokens_str:>12}")
    else:
        # Some or all tokens are unavailable: explicitly report known totals and unavailable counts
        lines.append(f"{'Total Duration':<30} {total_duration_str:>10}")
        known_tokens_str = f"{metrics['known_total_tokens']:,}"
        lines.append(f"{'Known tokens:':<30} {'':>10} {known_tokens_str:>12}")
        unavail_count = metrics["executions_with_unavailable_tokens"]
        unavail_str = f"{unavail_count} execution" if unavail_count == 1 else f"{unavail_count} executions"
        lines.append(f"{'Unavailable:':<30} {'':>10} {unavail_str:>12}")
        failed_unavail = metrics.get("failed_attempts_with_unavailable_tokens") or 0
        if failed_unavail:
            # Earlier tries of an execution: ones that failed, and ones the process was stopped
            # during (resume.orphaned_attempts) - neither reported what it spent.
            noun = "earlier try" if failed_unavail == 1 else "earlier tries"
            lines.append(f"{'':<30} {'':>10} {f'{failed_unavail} {noun}':>12}")

    lines.append("")
    if verification_verdict:
        lines.append(f"Verification: {verification_verdict}")
    lines.append(f"Repair attempts: {repair_attempts}")

    if skills:
        skills_summary = format_skills_summary(skills, agent_results)
        if skills_summary:
            lines.append("")
            lines.append(skills_summary)

    return "\n".join(lines)
