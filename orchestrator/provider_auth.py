"""Provider account-linking status checks (Package G).

Answers one question Synagon has never asked before: is the CLI a role is assigned to actually
usable right now, under the *user's own* account? It never asks the corollary question of what
that account is or holds - Synagon reads no credential, of any provider, ever. Every probe here
runs a command the provider's own documentation names as safe and non-interactive; where no such
command exists (claude, antigravity - see the design spec's research table), this module says so
honestly instead of guessing from a version check or a credential file's mere existence.

Deliberately separate from `preflight.py`: that module answers "can a *configured* agent run
right now" (binary/model/role), a run-readiness gate exercised before every task. This module
answers "is this account linked at all", asked on demand from a settings page or
`--check-providers`, independent of any configured team.
"""

import os
import re
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, TypedDict

from orchestrator.agents.antigravity import get_antigravity_executable_path
from orchestrator.agents.claude_code import get_claude_executable_path
from orchestrator.agents.opencode import get_opencode_executable_path

# -- status states -------------------------------------------------------------------------

AUTH_NOT_INSTALLED = "not_installed"
AUTH_NOT_AUTHENTICATED = "not_authenticated"
#: Reserved for a provider that documents a distinct "credential present but expired" signal.
#: None of claude/opencode/antigravity does today (see spec) - kept so a future probe that adds
#: one doesn't need a new state name.
AUTH_EXPIRED = "auth_expired"
AUTH_AUTHENTICATED = "authenticated"
#: The CLI is installed and responds, but no officially documented non-interactive command
#: exists to confirm sign-in state. Never conflate this with AUTH_AUTHENTICATED or
#: AUTH_NOT_AUTHENTICATED - it is not a guess in either direction.
AUTH_UNVERIFIABLE = "auth_unverifiable"
AUTH_PROVIDER_UNAVAILABLE = "provider_unavailable"
AUTH_CLI_ERROR = "cli_error"
AUTH_TIMED_OUT = "timed_out"

SUBSCRIPTION_UNAVAILABLE = "unavailable"
#: Required verbatim by the design spec whenever auth_state == AUTH_AUTHENTICATED.
SUBSCRIPTION_CONFIRMED_DETAIL = "Authentication verified; subscription status cannot be confirmed."

#: The three CLIs this module knows how to probe, in a fixed, stable order.
PROVIDERS = ("claude", "opencode", "antigravity")

DEFAULT_TIMEOUT = 15
DEFAULT_LOGIN_TIMEOUT = 15  # only used to bound resolving the executable before a login spawn

_VERSION_ARGS = {
    "claude": ["--version"],
    "opencode": ["--version"],
    "antigravity": ["--version"],
}

#: Argv appended to the resolved executable to open each provider's own login flow.
_LOGIN_ARGS: Dict[str, List[str]] = {
    "claude": [],
    "opencode": ["auth", "login"],
    "antigravity": [],
}


class ProviderAuthStatus(TypedDict, total=False):
    provider: str
    installed: bool
    executable: Optional[str]
    auth_state: str
    detail: str
    subscription_state: str
    subscription_detail: str
    checked_at: float
    duration_seconds: float


# -- redaction -------------------------------------------------------------------------------

_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9\-_]{10,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9\-_.]{10,}"),
    re.compile(r"AKIA[0-9A-Z]{12,}"),
    re.compile(r"(?i)(api[_-]?key|token|secret)\s*[:=]\s*\S+"),
]


def redact(text: str) -> str:
    """Scrub anything shaped like a credential out of `text`. Defense-in-depth.

    None of this module's probe commands are documented to print a secret - `opencode auth
    list` is explicitly documented to list provider names, not credentials, and the two
    `--version` probes print only a version string. This exists so a status-checking feature
    can never become the one place Synagon's zero-credential-handling invariant quietly breaks,
    even if a future CLI version changes what one of these commands prints.
    """
    if not text:
        return text
    result = text
    for pattern in _SECRET_PATTERNS:
        result = pattern.sub("[REDACTED]", result)
    return result


# -- probing -----------------------------------------------------------------------------------


class _ProbeTimeout(Exception):
    pass


class _ProbeFailed(Exception):
    pass


def _run(executable: str, args: List[str], timeout: int) -> Any:
    """Run one short probe command safely. Raises `_ProbeTimeout` or `_ProbeFailed`.

    `shell=False`, a detached stdin, and an explicit timeout - the same contract
    `preflight.py::_probe_version` already uses for the same kind of subprocess call.
    """
    try:
        return subprocess.run(
            [executable] + args,
            shell=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise _ProbeTimeout(str(exc)) from exc
    except Exception as exc:
        raise _ProbeFailed(str(exc)) from exc


def _decode(raw: Optional[bytes]) -> str:
    return (raw or b"").decode("utf-8", errors="replace")


def _resolve_executable(provider: str) -> str:
    """The provider's executable path, or raise `FileNotFoundError`.

    Deliberately an explicit branch calling each resolver by its bare name, not a
    dict-of-function-references built at module load time: a dict literal like
    `{"claude": get_claude_executable_path}` captures the function *object* at definition
    time, so `unittest.mock.patch("orchestrator.provider_auth.get_claude_executable_path", ...)`
    in a test would silently miss it - the dict would go on calling the original, unpatched
    function. A bare-name call inside a function body, by contrast, is resolved from the
    module's namespace fresh on every call, which is exactly what a test's patch needs to land.
    """
    if provider == "claude":
        return get_claude_executable_path()
    if provider == "opencode":
        return get_opencode_executable_path()
    if provider == "antigravity":
        return get_antigravity_executable_path()
    raise FileNotFoundError(f"unknown provider '{provider}'")


def _status(
    provider: str,
    installed: bool,
    executable: Optional[str],
    auth_state: str,
    detail: str,
    started: float,
    subscription_state: str = SUBSCRIPTION_UNAVAILABLE,
    subscription_detail: str = "",
) -> ProviderAuthStatus:
    now = time.time()
    return {
        "provider": provider,
        "installed": installed,
        "executable": executable,
        "auth_state": auth_state,
        "detail": redact(detail),
        "subscription_state": subscription_state,
        "subscription_detail": subscription_detail,
        "checked_at": now,
        "duration_seconds": round(now - started, 3),
    }


def _check_version_only(provider: str, timeout: int) -> ProviderAuthStatus:
    """claude / antigravity: confirm the binary runs; auth state is honestly unverifiable.

    Neither CLI documents a non-interactive way to confirm sign-in (verified against
    code.claude.com and antigravity.google's own docs - see the design spec). Reporting
    anything other than AUTH_UNVERIFIABLE here would be a guess Synagon has no basis for.
    """
    started = time.time()
    try:
        executable = _resolve_executable(provider)
    except Exception as exc:
        return _status(provider, False, None, AUTH_NOT_INSTALLED, str(exc), started)

    try:
        completed = _run(executable, _VERSION_ARGS[provider], timeout)
    except _ProbeTimeout:
        return _status(
            provider, True, executable, AUTH_TIMED_OUT,
            f"'{provider} --version' did not respond within {timeout}s.", started,
        )
    except _ProbeFailed as exc:
        return _status(
            provider, True, executable, AUTH_CLI_ERROR,
            f"Could not run '{provider} --version': {exc}", started,
        )

    text = (_decode(completed.stdout) + _decode(completed.stderr)).strip()
    if completed.returncode != 0 and not text:
        return _status(
            provider, True, executable, AUTH_CLI_ERROR,
            f"'{provider} --version' exited with code {completed.returncode} and no output.",
            started,
        )
    version_line = text.splitlines()[0] if text else "(no version reported)"
    return _status(
        provider, True, executable, AUTH_UNVERIFIABLE,
        f"{provider} responds ({version_line}), but exposes no documented non-interactive "
        "command to confirm sign-in status. Click Login to verify interactively.",
        started,
    )


def check_claude_auth(timeout: int = DEFAULT_TIMEOUT) -> ProviderAuthStatus:
    """Claude Code: installed + runnable, but authentication cannot be confirmed via CLI."""
    return _check_version_only("claude", timeout)


def check_antigravity_auth(timeout: int = DEFAULT_TIMEOUT) -> ProviderAuthStatus:
    """Antigravity CLI (agy): installed + runnable, but authentication cannot be confirmed via CLI."""
    return _check_version_only("antigravity", timeout)
