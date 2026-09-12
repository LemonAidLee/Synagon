"""CLI Agent Adapters for Antigravity and Claude Code."""

from orchestrator.agents.antigravity import run_antigravity, run_antigravity_raw
from orchestrator.agents.claude_code import run_claude_code
from orchestrator.agents.exceptions import (
    CLIError,
    CLIExecutionError,
    CLITimeoutError,
    CLIParsingError,
    NativeTUIUnavailableError,
)

#: Agents with a *supported, programmatic* native TUI integration: OpenCode, whose official
#: `serve` + `attach` client/server architecture lets the orchestrator drive a session over
#: REST while the real interactive TUI shows it. Claude Code and Antigravity have interactive
#: CLIs a person can launch by hand, but neither exposes a supported way to deliver a prompt to,
#: and detect completion of, a running interactive session - so they run headless (optionally
#: in a visible terminal), and `agent_execution_mode: native_tui` refuses them.
NATIVE_TUI_AGENTS = frozenset({"opencode"})


def supports_native_tui(agent: str) -> bool:
    """True when `agent` has a supported programmatic native-TUI integration."""
    return str(agent or "").strip().lower() in NATIVE_TUI_AGENTS


__all__ = [
    "run_antigravity",
    "run_antigravity_raw",
    "run_claude_code",
    "CLIError",
    "CLIExecutionError",
    "CLITimeoutError",
    "CLIParsingError",
    "NativeTUIUnavailableError",
    "NATIVE_TUI_AGENTS",
    "supports_native_tui",
]
