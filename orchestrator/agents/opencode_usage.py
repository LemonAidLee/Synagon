"""One normalisation of OpenCode token telemetry, shared by headless and native TUI mode.

Both modes read the same provider-reported numbers: the ``tokens`` object OpenCode attaches to
every ``step-finish`` part. Headless mode reads those parts from ``opencode run --format json``;
native TUI mode fetches the very same parts from the session over the server's REST API. They
used to be normalised in two places, which let the two modes disagree about what
``input_tokens`` meant (headless excluded cached input, native included it) and let native mode
drop reasoning and cache-write tokens from its total. There is now one function.

The contract, identical to the Claude Code adapter's:

* ``input_tokens``  = input + cache.read + cache.write  (the full processed context)
* ``output_tokens`` = output
* ``total_tokens``  = the step's own reported ``total`` when present, otherwise the sum of the
  components it did report (input + output + reasoning + cache.read + cache.write) - arithmetic
  over reported numbers, never an estimate. Measured on OpenCode 1.18.29, every reported step
  total equals exactly that sum.
"""

from typing import Any, Dict, Iterable, List, Optional

from orchestrator.types import TokenUsage, create_token_usage, unavailable_token_usage


def _count(value: Any) -> Optional[int]:
    """A reported token count as an int, or None when it is missing or not a count."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def usage_from_step_tokens(
    steps: Iterable[Any],
    source: str = "step-finish",
) -> TokenUsage:
    """Sum OpenCode per-step ``tokens`` objects into one TokenUsage.

    Args:
        steps: The ``tokens`` dict of each step, oldest first. Anything that is not a dict, or
            reports none of input/output/total, is ignored rather than counted as zero.
        source: Where the numbers came from, kept on ``raw_usage`` so a figure can be traced.

    Returns:
        A TokenUsage, or unavailable usage when no step reported anything.
    """
    input_tokens = output_tokens = reasoning_tokens = 0
    cache_read_tokens = cache_write_tokens = total_tokens = 0
    raw_steps: List[Dict[str, Any]] = []

    for tokens in steps:
        if not isinstance(tokens, dict):
            continue
        inp = _count(tokens.get("input"))
        out = _count(tokens.get("output"))
        reported_total = _count(tokens.get("total"))
        if inp is None and out is None and reported_total is None:
            continue
        cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
        reasoning = _count(tokens.get("reasoning")) or 0
        cache_read = _count(cache.get("read")) or 0
        cache_write = _count(cache.get("write")) or 0

        raw_steps.append(dict(tokens))
        input_tokens += (inp or 0) + cache_read + cache_write
        output_tokens += out or 0
        reasoning_tokens += reasoning
        cache_read_tokens += cache_read
        cache_write_tokens += cache_write
        if reported_total is None:
            reported_total = (inp or 0) + (out or 0) + reasoning + cache_read + cache_write
        total_tokens += reported_total

    if not raw_steps:
        return unavailable_token_usage()

    return create_token_usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        cache_read_tokens=cache_read_tokens or None,
        cache_write_tokens=cache_write_tokens or None,
        reasoning_tokens=reasoning_tokens or None,
        available=True,
        raw_usage={"source": source, "steps": raw_steps},
    )


def step_tokens_from_messages(messages: Any) -> List[Dict[str, Any]]:
    """Every ``step-finish`` part's ``tokens`` across a session's assistant messages.

    `messages` is the body of ``GET /session/<id>/message``. These are the same parts, carrying
    the same numbers, that ``opencode run --format json`` streams - which is what makes native
    and headless usage the same measurement rather than two that merely agree today.
    """
    steps: List[Dict[str, Any]] = []
    if not isinstance(messages, list):
        return steps
    for message in messages:
        if not isinstance(message, dict):
            continue
        info = message.get("info") or {}
        if not isinstance(info, dict) or info.get("role") != "assistant":
            continue
        for part in message.get("parts") or []:
            if isinstance(part, dict) and part.get("type") == "step-finish":
                tokens = part.get("tokens")
                if isinstance(tokens, dict):
                    steps.append(tokens)
    return steps
