"""Custom exception classes for CLI agent orchestration."""

from typing import Any, Dict, Optional, List


class CLIError(Exception):
    """Base exception for all CLI agent invocation errors."""
    pass


class CLIExecutionError(CLIError):
    """Raised when a CLI command returns a non-zero exit code or explicit failure status.

    `token_usage` carries what the provider reported for a failed execution that nevertheless
    spent tokens (a turn that errored after several steps), so the failure is not also an
    accounting hole. None means nothing was reported, which is not the same as zero.
    """

    def __init__(
        self,
        message: str,
        returncode: Optional[int] = None,
        stdout: str = "",
        stderr: str = "",
        command: Optional[List[str]] = None,
        token_usage: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(message)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.command = command
        self.token_usage = token_usage

    def __str__(self) -> str:
        base = super().__str__()
        parts = [base]
        if self.returncode is not None:
            parts.append(f"Exit code: {self.returncode}")
        if self.stderr:
            parts.append(f"Stderr: {self.stderr.strip()}")
        return " | ".join(parts)


class CLITimeoutError(CLIError):
    """Raised when a CLI command exceeds its allocated timeout."""

    def __init__(
        self,
        message: str,
        timeout: int,
        command: Optional[List[str]] = None,
        token_usage: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(message)
        self.timeout = timeout
        self.command = command
        self.token_usage = token_usage


class NativeTUIUnavailableError(CLIExecutionError):
    """Raised when `agent_execution_mode: native_tui` is required but cannot be provided.

    Either the agent has no supported programmatic native-TUI integration at all (Claude Code,
    Antigravity), or the one surface that can show it is not there (no Antigravity terminal
    bridge and no daemon relay). Retrying cannot change either answer, so this is never
    retried, and `native_tui` never silently becomes headless.
    """


class CLIParsingError(CLIError):
    """Raised when CLI output cannot be parsed or validated according to expected schema."""

    def __init__(self, message: str, raw_output: str = ""):
        super().__init__(message)
        self.raw_output = raw_output
