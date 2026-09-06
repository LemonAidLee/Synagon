"""Custom exception classes for CLI agent orchestration."""

from typing import Optional, List


class CLIError(Exception):
    """Base exception for all CLI agent invocation errors."""
    pass


class CLIExecutionError(CLIError):
    """Raised when a CLI command returns a non-zero exit code or explicit failure status."""

    def __init__(
        self,
        message: str,
        returncode: Optional[int] = None,
        stdout: str = "",
        stderr: str = "",
        command: Optional[List[str]] = None,
    ):
        super().__init__(message)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.command = command

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
    ):
        super().__init__(message)
        self.timeout = timeout
        self.command = command


class CLIParsingError(CLIError):
    """Raised when CLI output cannot be parsed or validated according to expected schema."""

    def __init__(self, message: str, raw_output: str = ""):
        super().__init__(message)
        self.raw_output = raw_output
