# Package G — Local AI Provider Account Linking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a user check, from Synagon, whether `claude`, `opencode`, and `agy` (Antigravity CLI) are installed and — where a provider officially exposes it non-interactively — signed in, and launch each provider's own login flow, all without Synagon ever touching a credential.

**Architecture:** One new pure-function module (`orchestrator/provider_auth.py`, shaped like the existing `preflight.py`) does all classification and subprocess probing. It's wired into three surfaces that already exist: a new CLI flag (`--check-providers`, parallel to `--doctor`), two new daemon routes (`GET /api/providers`, `POST /api/control/provider_login`) plus a page route, and one new plain-HTML page (`orchestrator/web/settings.html`) matching the existing cockpit/office/design pages.

**Tech Stack:** Python 3.11+ stdlib (`subprocess`, `re`, `typing.TypedDict`), the project's existing `unittest` test suite, vanilla HTML/CSS/JS for the web page (no new frontend framework, no new dependency).

**Spec:** `docs/superpowers/specs/2026-09-12-package-g-provider-account-linking-design.md`

## Global Constraints

- Every subprocess call is `shell=False`, uses `stdin=subprocess.DEVNULL`, and passes an explicit `timeout` — no exceptions (matches `preflight.py::_probe_version`, and is a hard requirement of the spec).
- Never request, extract, store, display, or transmit a password, OAuth token, cookie, API key, or session file. No code in this plan reads any credential file, keyring entry, or `.credentials.json`/`auth.json` path directly — only a CLI's own stdout from a command it documents as safe to run.
- Only these three commands are used as auth signals, because they are the only ones confirmed against vendor docs (see spec's research table): `claude --version`, `agy --version`, `opencode auth list`. No other flag is invented for any of the three CLIs.
- `claude` and `agy` must never be reported as `authenticated` or `not_authenticated` — only `auth_unverifiable` — because no documented non-interactive check exists for either. Getting this wrong (guessing) is the one thing this feature must not do.
- The required exact subscription-unavailable copy is `"Authentication verified; subscription status cannot be confirmed."` — used verbatim, only when `auth_state == AUTH_AUTHENTICATED`.
- `open_provider_login` never blocks on or force-kills the launched process — it starts a detached terminal and returns. (`orchestrator.launcher.run_agent_cli` is NOT used for this, because it blocks synchronously and force-kills past its timeout — wrong for an indefinite interactive login session; see spec amendment.)
- All new stdout captured from a provider CLI is passed through `provider_auth.redact()` before it reaches a `detail` field, a log, or an HTTP response body.

---

### Task 1: `provider_auth.py` core — status model, redaction, and the version-only probe (claude, antigravity)

**Files:**
- Create: `orchestrator/provider_auth.py`
- Test: `tests/test_provider_auth.py`

**Interfaces:**
- Consumes: `orchestrator.agents.claude_code.get_claude_executable_path() -> str` (raises `FileNotFoundError`), `orchestrator.agents.antigravity.get_antigravity_executable_path() -> str` (raises `FileNotFoundError`) — both already exist, unchanged.
- Produces (used by Tasks 2–6): module-level constants `AUTH_NOT_INSTALLED`, `AUTH_NOT_AUTHENTICATED`, `AUTH_EXPIRED`, `AUTH_AUTHENTICATED`, `AUTH_UNVERIFIABLE`, `AUTH_PROVIDER_UNAVAILABLE`, `AUTH_CLI_ERROR`, `AUTH_TIMED_OUT` (all `str`); `SUBSCRIPTION_UNAVAILABLE = "unavailable"`; `SUBSCRIPTION_CONFIRMED_DETAIL` (the exact required copy); `PROVIDERS = ("claude", "opencode", "antigravity")`; `DEFAULT_TIMEOUT = 15`; `ProviderAuthStatus` (`TypedDict`, `total=False`, fields: `provider: str`, `installed: bool`, `executable: Optional[str]`, `auth_state: str`, `detail: str`, `subscription_state: str`, `subscription_detail: str`, `checked_at: float`, `duration_seconds: float`); `redact(text: str) -> str`; `check_claude_auth(timeout: int = DEFAULT_TIMEOUT) -> ProviderAuthStatus`; `check_antigravity_auth(timeout: int = DEFAULT_TIMEOUT) -> ProviderAuthStatus`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_provider_auth.py`:

```python
"""Package G - provider account-linking status checks.

Every probe here is read-only and safety-checked the same way `preflight.py`'s deep-mode
version probe is: `shell=False`, a detached stdin, an explicit timeout. None of it ever reads
a credential file or a keyring entry - see `provider_auth.redact` and the module docstring for
why, and the design spec (`docs/superpowers/specs/2026-09-12-package-g-provider-account-linking-design.md`)
for the vendor-doc research behind each provider's probe.
"""

import subprocess
import unittest
from unittest.mock import patch

from orchestrator.provider_auth import (
    AUTH_AUTHENTICATED,
    AUTH_CLI_ERROR,
    AUTH_EXPIRED,
    AUTH_NOT_AUTHENTICATED,
    AUTH_NOT_INSTALLED,
    AUTH_PROVIDER_UNAVAILABLE,
    AUTH_TIMED_OUT,
    AUTH_UNVERIFIABLE,
    check_antigravity_auth,
    check_claude_auth,
    redact,
)


class TestAuthStatesAreDistinct(unittest.TestCase):
    def test_every_state_is_its_own_string(self):
        # AUTH_EXPIRED is asserted here even though no provider probe reaches it today (see its
        # definition's comment): none of claude/opencode/antigravity documents a non-interactive
        # "credential present but expired" signal, distinct from "not authenticated" or
        # "unverifiable". The constant exists so a future probe that adds one doesn't need a new
        # name; this test is what keeps it a real, distinct value in the meantime.
        states = [
            AUTH_NOT_INSTALLED, AUTH_NOT_AUTHENTICATED, AUTH_EXPIRED, AUTH_AUTHENTICATED,
            AUTH_UNVERIFIABLE, AUTH_PROVIDER_UNAVAILABLE, AUTH_CLI_ERROR, AUTH_TIMED_OUT,
        ]
        self.assertEqual(len(states), len(set(states)))


class _Completed:
    def __init__(self, returncode=0, stdout=b"", stderr=b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class TestRedact(unittest.TestCase):
    def test_redacts_anthropic_style_keys(self):
        self.assertEqual(redact("key is sk-ant-api03-abcdefghijklmnop"), "key is [REDACTED]")

    def test_redacts_bearer_tokens(self):
        self.assertEqual(redact("Authorization: Bearer abcdefghij123456"), "Authorization: [REDACTED]")

    def test_leaves_ordinary_text_alone(self):
        self.assertEqual(redact("providers: anthropic, opencode"), "providers: anthropic, opencode")

    def test_handles_empty_string(self):
        self.assertEqual(redact(""), "")


class TestClaudeVersionOnlyProbe(unittest.TestCase):
    def test_not_installed(self):
        with patch(
            "orchestrator.provider_auth.get_claude_executable_path",
            side_effect=FileNotFoundError("no claude"),
        ):
            status = check_claude_auth()
        self.assertFalse(status["installed"])
        self.assertEqual(status["auth_state"], AUTH_NOT_INSTALLED)
        self.assertIsNone(status["executable"])

    def test_installed_and_responsive_is_unverifiable_not_a_guess(self):
        with patch(
            "orchestrator.provider_auth.get_claude_executable_path",
            return_value="C:/bin/claude.exe",
        ), patch(
            "orchestrator.provider_auth.subprocess.run",
            return_value=_Completed(returncode=0, stdout=b"2.1.267 (Claude Code)", stderr=b""),
        ):
            status = check_claude_auth()
        self.assertTrue(status["installed"])
        self.assertEqual(status["executable"], "C:/bin/claude.exe")
        self.assertEqual(status["auth_state"], AUTH_UNVERIFIABLE)
        self.assertIn("no documented non-interactive", status["detail"])

    def test_nonzero_exit_with_no_output_is_a_cli_error(self):
        with patch(
            "orchestrator.provider_auth.get_claude_executable_path",
            return_value="C:/bin/claude.exe",
        ), patch(
            "orchestrator.provider_auth.subprocess.run",
            return_value=_Completed(returncode=1, stdout=b"", stderr=b""),
        ):
            status = check_claude_auth()
        self.assertEqual(status["auth_state"], AUTH_CLI_ERROR)

    def test_timeout(self):
        with patch(
            "orchestrator.provider_auth.get_claude_executable_path",
            return_value="C:/bin/claude.exe",
        ), patch(
            "orchestrator.provider_auth.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=15),
        ):
            status = check_claude_auth(timeout=15)
        self.assertEqual(status["auth_state"], AUTH_TIMED_OUT)

    def test_probe_uses_safe_subprocess_flags(self):
        with patch(
            "orchestrator.provider_auth.get_claude_executable_path",
            return_value="C:/bin/claude.exe",
        ), patch(
            "orchestrator.provider_auth.subprocess.run",
            return_value=_Completed(returncode=0, stdout=b"1.0.0", stderr=b""),
        ) as mock_run:
            check_claude_auth(timeout=7)
        args, kwargs = mock_run.call_args
        self.assertEqual(args[0], ["C:/bin/claude.exe", "--version"])
        self.assertFalse(kwargs["shell"])
        self.assertIsNotNone(kwargs["stdin"])
        self.assertEqual(kwargs["timeout"], 7)


class TestAntigravityVersionOnlyProbe(unittest.TestCase):
    def test_not_installed(self):
        with patch(
            "orchestrator.provider_auth.get_antigravity_executable_path",
            side_effect=FileNotFoundError("no agy"),
        ):
            status = check_antigravity_auth()
        self.assertEqual(status["auth_state"], AUTH_NOT_INSTALLED)

    def test_installed_and_responsive_is_unverifiable(self):
        with patch(
            "orchestrator.provider_auth.get_antigravity_executable_path",
            return_value="C:/bin/agy.exe",
        ), patch(
            "orchestrator.provider_auth.subprocess.run",
            return_value=_Completed(returncode=0, stdout=b"agy 1.4.0", stderr=b""),
        ):
            status = check_antigravity_auth()
        self.assertEqual(status["auth_state"], AUTH_UNVERIFIABLE)
        self.assertEqual(status["provider"], "antigravity")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_provider_auth.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'orchestrator.provider_auth'`

- [ ] **Step 3: Implement `orchestrator/provider_auth.py`**

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_provider_auth.py -v`
Expected: PASS (9 tests)

- [ ] **Step 5: Commit**

```bash
git add orchestrator/provider_auth.py tests/test_provider_auth.py
git commit -m "$(cat <<'EOF'
Add provider_auth.py: status model and claude/antigravity account-linking probes

Neither CLI documents a non-interactive way to confirm sign-in (verified against
code.claude.com and antigravity.google's own docs), so both report AUTH_UNVERIFIABLE
rather than guessing - a distinct state from both authenticated and not_authenticated.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: `provider_auth.py` — OpenCode's real auth check

**Files:**
- Modify: `orchestrator/provider_auth.py`
- Test: `tests/test_provider_auth.py`

**Interfaces:**
- Consumes: `orchestrator.agents.opencode.get_opencode_executable_path() -> str` (existing, raises `FileNotFoundError`); `_run`, `_decode`, `_status`, `AUTH_*` constants from Task 1 (same module).
- Produces: `check_opencode_auth(timeout: int = DEFAULT_TIMEOUT) -> ProviderAuthStatus`; `_parse_opencode_auth_list(text: str) -> Optional[List[str]]` (module-private, but imported directly by its test).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_provider_auth.py` (add `AUTH_AUTHENTICATED`, `AUTH_NOT_AUTHENTICATED`, `SUBSCRIPTION_CONFIRMED_DETAIL`, `check_opencode_auth`, `_parse_opencode_auth_list` to the existing import from `orchestrator.provider_auth`):

```python
from orchestrator.provider_auth import (  # noqa: F811 - extends the Task 1 import block
    AUTH_AUTHENTICATED,
    AUTH_CLI_ERROR,
    AUTH_NOT_AUTHENTICATED,
    AUTH_NOT_INSTALLED,
    AUTH_TIMED_OUT,
    AUTH_UNVERIFIABLE,
    SUBSCRIPTION_CONFIRMED_DETAIL,
    _parse_opencode_auth_list,
    check_antigravity_auth,
    check_claude_auth,
    check_opencode_auth,
    redact,
)


class TestParseOpencodeAuthList(unittest.TestCase):
    def test_multiple_providers(self):
        self.assertEqual(
            _parse_opencode_auth_list("anthropic\nopencode\n"), ["anthropic", "opencode"]
        )

    def test_single_provider_with_extra_columns(self):
        self.assertEqual(_parse_opencode_auth_list("anthropic   oauth\n"), ["anthropic"])

    def test_empty_output_is_zero_providers_not_unparseable(self):
        self.assertEqual(_parse_opencode_auth_list(""), [])

    def test_explicit_no_providers_message_is_zero_providers(self):
        self.assertEqual(_parse_opencode_auth_list("No providers configured.\n"), [])

    def test_header_only_output_is_unparseable(self):
        self.assertIsNone(_parse_opencode_auth_list("PROVIDER   METHOD\n"))


class TestOpencodeAuthCheck(unittest.TestCase):
    def test_not_installed(self):
        with patch(
            "orchestrator.provider_auth.get_opencode_executable_path",
            side_effect=FileNotFoundError("no opencode"),
        ):
            status = check_opencode_auth()
        self.assertEqual(status["auth_state"], AUTH_NOT_INSTALLED)

    def test_authenticated_reports_provider_names_and_the_required_subscription_copy(self):
        with patch(
            "orchestrator.provider_auth.get_opencode_executable_path",
            return_value="C:/bin/opencode.exe",
        ), patch(
            "orchestrator.provider_auth.subprocess.run",
            return_value=_Completed(returncode=0, stdout=b"anthropic\nopencode\n", stderr=b""),
        ):
            status = check_opencode_auth()
        self.assertEqual(status["auth_state"], AUTH_AUTHENTICATED)
        self.assertIn("anthropic", status["detail"])
        self.assertIn("opencode", status["detail"])
        self.assertEqual(status["subscription_detail"], SUBSCRIPTION_CONFIRMED_DETAIL)

    def test_not_authenticated_when_no_providers_configured(self):
        with patch(
            "orchestrator.provider_auth.get_opencode_executable_path",
            return_value="C:/bin/opencode.exe",
        ), patch(
            "orchestrator.provider_auth.subprocess.run",
            return_value=_Completed(returncode=0, stdout=b"", stderr=b""),
        ):
            status = check_opencode_auth()
        self.assertEqual(status["auth_state"], AUTH_NOT_AUTHENTICATED)

    def test_nonzero_exit_is_a_cli_error(self):
        with patch(
            "orchestrator.provider_auth.get_opencode_executable_path",
            return_value="C:/bin/opencode.exe",
        ), patch(
            "orchestrator.provider_auth.subprocess.run",
            return_value=_Completed(returncode=1, stdout=b"", stderr=b"boom"),
        ):
            status = check_opencode_auth()
        self.assertEqual(status["auth_state"], AUTH_CLI_ERROR)

    def test_unparseable_output_is_a_cli_error_not_a_guess(self):
        with patch(
            "orchestrator.provider_auth.get_opencode_executable_path",
            return_value="C:/bin/opencode.exe",
        ), patch(
            "orchestrator.provider_auth.subprocess.run",
            return_value=_Completed(returncode=0, stdout=b"PROVIDER   METHOD\n", stderr=b""),
        ):
            status = check_opencode_auth()
        self.assertEqual(status["auth_state"], AUTH_CLI_ERROR)

    def test_timeout(self):
        with patch(
            "orchestrator.provider_auth.get_opencode_executable_path",
            return_value="C:/bin/opencode.exe",
        ), patch(
            "orchestrator.provider_auth.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="opencode", timeout=15),
        ):
            status = check_opencode_auth()
        self.assertEqual(status["auth_state"], AUTH_TIMED_OUT)

    def test_probe_command_and_safe_flags(self):
        with patch(
            "orchestrator.provider_auth.get_opencode_executable_path",
            return_value="C:/bin/opencode.exe",
        ), patch(
            "orchestrator.provider_auth.subprocess.run",
            return_value=_Completed(returncode=0, stdout=b"anthropic\n", stderr=b""),
        ) as mock_run:
            check_opencode_auth(timeout=9)
        args, kwargs = mock_run.call_args
        self.assertEqual(args[0], ["C:/bin/opencode.exe", "auth", "list"])
        self.assertFalse(kwargs["shell"])
        self.assertIsNotNone(kwargs["stdin"])
        self.assertEqual(kwargs["timeout"], 9)

    def test_no_secret_shaped_output_survives_into_detail(self):
        with patch(
            "orchestrator.provider_auth.get_opencode_executable_path",
            return_value="C:/bin/opencode.exe",
        ), patch(
            "orchestrator.provider_auth.subprocess.run",
            return_value=_Completed(
                returncode=1, stdout=b"", stderr=b"token=sk-ant-api03-verysecretvalue123"
            ),
        ):
            status = check_opencode_auth()
        self.assertNotIn("sk-ant-api03", status["detail"])
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_provider_auth.py -v`
Expected: FAIL — `ImportError: cannot import name 'check_opencode_auth'`

- [ ] **Step 3: Implement in `orchestrator/provider_auth.py`**

Append after `check_antigravity_auth`:

```python
#: Substrings (checked case-insensitively against the whole trimmed output) that OpenCode's own
#: `auth list` is expected to use for "nothing is configured" - kept short and literal on
#: purpose (see the design spec's "Risks" section: fail closed on anything unrecognised rather
#: than widen this list speculatively).
_NO_PROVIDERS_MARKERS = ("no providers", "no authenticated providers", "not logged in", "no credentials")

#: Line prefixes that mark a header/separator row rather than a provider entry.
_SKIP_LINE_PREFIXES = ("provider", "-", "=")


def _parse_opencode_auth_list(text: str) -> Optional[List[str]]:
    """Provider names out of `opencode auth list` output, or `None` if unparseable.

    Conservative on purpose: a recognisable "nothing configured" message is zero providers;
    each remaining non-header, non-blank line's first token is taken as a provider id; and if
    that leaves nothing at all (e.g. a header row with no data rows) this returns `None` so the
    caller reports a CLI error instead of silently claiming "not authenticated".
    """
    stripped = text.strip()
    if not stripped:
        return []
    lowered = stripped.lower()
    if any(marker in lowered for marker in _NO_PROVIDERS_MARKERS):
        return []

    providers: List[str] = []
    for line in stripped.splitlines():
        line = line.strip()
        if not line or line.lower().startswith(_SKIP_LINE_PREFIXES):
            continue
        token = line.split()[0].rstrip(":")
        if token:
            providers.append(token)
    return providers or None


def check_opencode_auth(timeout: int = DEFAULT_TIMEOUT) -> ProviderAuthStatus:
    """OpenCode: `opencode auth list` is documented to list providers with stored credentials.

    This is the one provider of the three with an officially documented, non-interactive,
    secret-free status command (opencode.ai/docs/cli/) - see the design spec's research table.
    """
    started = time.time()
    try:
        executable = get_opencode_executable_path()
    except Exception as exc:
        return _status("opencode", False, None, AUTH_NOT_INSTALLED, str(exc), started)

    try:
        completed = _run(executable, ["auth", "list"], timeout)
    except _ProbeTimeout:
        return _status(
            "opencode", True, executable, AUTH_TIMED_OUT,
            f"'opencode auth list' did not respond within {timeout}s.", started,
        )
    except _ProbeFailed as exc:
        return _status(
            "opencode", True, executable, AUTH_CLI_ERROR,
            f"Could not run 'opencode auth list': {exc}", started,
        )

    text = _decode(completed.stdout) + _decode(completed.stderr)
    if completed.returncode != 0:
        return _status(
            "opencode", True, executable, AUTH_CLI_ERROR,
            f"'opencode auth list' exited with code {completed.returncode}: {text.strip()[:300]}",
            started,
        )

    providers = _parse_opencode_auth_list(text)
    if providers is None:
        return _status(
            "opencode", True, executable, AUTH_CLI_ERROR,
            "Could not parse 'opencode auth list' output; run it yourself to check.", started,
        )
    if not providers:
        return _status(
            "opencode", True, executable, AUTH_NOT_AUTHENTICATED,
            "No providers are configured in OpenCode's credentials file.", started,
        )
    return _status(
        "opencode", True, executable, AUTH_AUTHENTICATED,
        f"Providers with stored credentials: {', '.join(providers)}", started,
        subscription_state=SUBSCRIPTION_UNAVAILABLE,
        subscription_detail=SUBSCRIPTION_CONFIRMED_DETAIL,
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_provider_auth.py -v`
Expected: PASS (all tests from Task 1 and Task 2)

- [ ] **Step 5: Commit**

```bash
git add orchestrator/provider_auth.py tests/test_provider_auth.py
git commit -m "$(cat <<'EOF'
Add OpenCode's real auth-list check to provider_auth.py

opencode auth list is officially documented and secret-free, so OpenCode gets a real
authenticated/not_authenticated answer instead of the unverifiable state claude/agy get.
The parser fails closed (cli_error) on anything it doesn't recognize rather than guessing.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Aggregate check, CLI report formatting, and the detached login launcher

**Files:**
- Modify: `orchestrator/provider_auth.py`
- Test: `tests/test_provider_auth.py`

**Interfaces:**
- Consumes: everything from Tasks 1–2 in the same module.
- Produces (used by Task 4 and Task 6): `check_all_providers(timeout: int = DEFAULT_TIMEOUT) -> List[ProviderAuthStatus]`; `providers_ok(providers: List[ProviderAuthStatus]) -> bool`; `format_provider_report(providers: List[ProviderAuthStatus], color: bool = True) -> str`; `open_provider_login(provider: str) -> Dict[str, Any]` (returns `{"ok": True, "provider": ...}` or `{"ok": False, "error": ...}`); `_spawn_detached_terminal(cmd: List[str], cwd: str, title: str) -> None` (module-private, imported directly by its test).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_provider_auth.py`:

```python
from orchestrator.provider_auth import (  # noqa: F811 - extends the running import block
    PROVIDERS,
    check_all_providers,
    format_provider_report,
    open_provider_login,
    providers_ok,
)


class TestCheckAllProviders(unittest.TestCase):
    def test_returns_all_three_in_a_fixed_order(self):
        with patch(
            "orchestrator.provider_auth.get_claude_executable_path",
            side_effect=FileNotFoundError("x"),
        ), patch(
            "orchestrator.provider_auth.get_opencode_executable_path",
            side_effect=FileNotFoundError("x"),
        ), patch(
            "orchestrator.provider_auth.get_antigravity_executable_path",
            side_effect=FileNotFoundError("x"),
        ):
            report = check_all_providers()
        self.assertEqual([p["provider"] for p in report], list(PROVIDERS))
        self.assertTrue(all(p["auth_state"] == AUTH_NOT_INSTALLED for p in report))


class TestProvidersOk(unittest.TestCase):
    def test_ok_when_nothing_is_a_cli_error_or_timeout(self):
        report = [
            {"provider": "claude", "auth_state": AUTH_UNVERIFIABLE},
            {"provider": "opencode", "auth_state": AUTH_NOT_AUTHENTICATED},
            {"provider": "antigravity", "auth_state": AUTH_NOT_INSTALLED},
        ]
        self.assertTrue(providers_ok(report))

    def test_not_ok_when_something_errored(self):
        report = [{"provider": "claude", "auth_state": AUTH_CLI_ERROR}]
        self.assertFalse(providers_ok(report))


class TestFormatProviderReport(unittest.TestCase):
    def test_names_every_provider_and_its_detail(self):
        report = [
            {
                "provider": "claude", "installed": True, "auth_state": AUTH_UNVERIFIABLE,
                "detail": "no documented check", "subscription_detail": "",
            },
            {
                "provider": "opencode", "installed": True, "auth_state": AUTH_AUTHENTICATED,
                "detail": "providers: anthropic", "subscription_detail": SUBSCRIPTION_CONFIRMED_DETAIL,
            },
            {
                "provider": "antigravity", "installed": False, "auth_state": AUTH_NOT_INSTALLED,
                "detail": "not found", "subscription_detail": "",
            },
        ]
        text = format_provider_report(report, color=False)
        self.assertIn("claude", text)
        self.assertIn("opencode", text)
        self.assertIn("antigravity", text)
        self.assertIn(SUBSCRIPTION_CONFIRMED_DETAIL, text)


class TestOpenProviderLogin(unittest.TestCase):
    def test_unknown_provider_is_refused_without_spawning_anything(self):
        with patch("orchestrator.provider_auth._spawn_detached_terminal") as spawn:
            result = open_provider_login("not-a-real-provider")
        self.assertFalse(result["ok"])
        spawn.assert_not_called()

    def test_not_installed_is_refused_without_spawning_anything(self):
        with patch(
            "orchestrator.provider_auth.get_claude_executable_path",
            side_effect=FileNotFoundError("no claude"),
        ), patch("orchestrator.provider_auth._spawn_detached_terminal") as spawn:
            result = open_provider_login("claude")
        self.assertFalse(result["ok"])
        spawn.assert_not_called()

    def test_claude_login_spawns_the_bare_binary(self):
        with patch(
            "orchestrator.provider_auth.get_claude_executable_path",
            return_value="C:/bin/claude.exe",
        ), patch("orchestrator.provider_auth._spawn_detached_terminal") as spawn:
            result = open_provider_login("claude")
        self.assertTrue(result["ok"])
        spawn.assert_called_once()
        cmd = spawn.call_args.args[0]
        self.assertEqual(cmd, ["C:/bin/claude.exe"])

    def test_opencode_login_spawns_auth_login(self):
        with patch(
            "orchestrator.provider_auth.get_opencode_executable_path",
            return_value="C:/bin/opencode.exe",
        ), patch("orchestrator.provider_auth._spawn_detached_terminal") as spawn:
            result = open_provider_login("opencode")
        self.assertTrue(result["ok"])
        cmd = spawn.call_args.args[0]
        self.assertEqual(cmd, ["C:/bin/opencode.exe", "auth", "login"])

    def test_spawn_failure_is_reported_not_raised(self):
        with patch(
            "orchestrator.provider_auth.get_claude_executable_path",
            return_value="C:/bin/claude.exe",
        ), patch(
            "orchestrator.provider_auth._spawn_detached_terminal",
            side_effect=OSError("no terminal available"),
        ):
            result = open_provider_login("claude")
        self.assertFalse(result["ok"])
        self.assertIn("no terminal available", result["error"])
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_provider_auth.py -v`
Expected: FAIL — `ImportError: cannot import name 'check_all_providers'`

- [ ] **Step 3: Implement in `orchestrator/provider_auth.py`**

Append at the end of the file:

```python
_CHECKERS = {
    "claude": check_claude_auth,
    "opencode": check_opencode_auth,
    "antigravity": check_antigravity_auth,
}


def check_all_providers(timeout: int = DEFAULT_TIMEOUT) -> List[ProviderAuthStatus]:
    """Every provider's status, in `PROVIDERS` order. One probe per provider, run in turn."""
    return [_CHECKERS[provider](timeout) for provider in PROVIDERS]


def providers_ok(providers: List[ProviderAuthStatus]) -> bool:
    """False only when a probe itself broke (`cli_error`/`timed_out`) - never on account state.

    "Not installed" and "not authenticated" are informative facts about the world, not failures
    of this check; a `--check-providers` run that reports them clearly has done its job.
    """
    return not any(p.get("auth_state") in (AUTH_CLI_ERROR, AUTH_TIMED_OUT) for p in providers)


def format_provider_report(providers: List[ProviderAuthStatus], color: bool = True) -> str:
    """Render a provider status list as human-readable text, styled like `--doctor`'s output."""
    if color:
        from colorama import Fore, Style

        green, red, yellow, cyan, reset, bright = (
            Fore.GREEN, Fore.RED, Fore.YELLOW, Fore.CYAN, Style.RESET_ALL, Style.BRIGHT,
        )
    else:
        green = red = yellow = cyan = reset = bright = ""

    markers = {
        AUTH_AUTHENTICATED: green + "[OK]" + reset,
        AUTH_UNVERIFIABLE: yellow + "[?]" + reset,
        AUTH_NOT_AUTHENTICATED: yellow + "[NOT SIGNED IN]" + reset,
        AUTH_EXPIRED: yellow + "[EXPIRED]" + reset,
        AUTH_NOT_INSTALLED: red + "[NOT INSTALLED]" + reset,
        AUTH_PROVIDER_UNAVAILABLE: red + "[UNAVAILABLE]" + reset,
        AUTH_CLI_ERROR: red + "[ERROR]" + reset,
        AUTH_TIMED_OUT: red + "[TIMED OUT]" + reset,
    }

    lines = [f"{cyan}{bright}PROVIDER ACCOUNTS{reset}", ""]
    for provider in providers:
        marker = markers.get(provider.get("auth_state"), "[?]")
        lines.append(f"  {marker} {bright}{provider.get('provider')}{reset}")
        lines.append(f"      {provider.get('detail', '')}")
        if provider.get("subscription_detail"):
            lines.append(f"      {provider['subscription_detail']}")
        lines.append("")
    return "\n".join(lines)


def _spawn_detached_terminal(cmd: List[str], cwd: str, title: str) -> None:
    """Open `cmd` in its own terminal window and return immediately.

    Deliberately not `orchestrator.launcher.run_agent_cli`: that helper waits for the launched
    process to exit and force-kills the whole tree past its timeout, which is right for an
    agent turn but wrong for an interactive login a person may leave open indefinitely (bare
    `claude` opens an indefinite REPL). This starts the window, is not owned by the
    orchestrator's process-job machinery, and is never waited on or killed.
    """
    if sys.platform == "win32":
        import shutil as _shutil

        wt_path = _shutil.which("wt.exe")
        if not wt_path:
            candidate = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WindowsApps\wt.exe")
            if os.path.isfile(candidate):
                wt_path = candidate
        if wt_path:
            subprocess.Popen([wt_path, "--title", title, "-d", cwd] + cmd)
            return
        create_new_console = 0x00000010
        subprocess.Popen(cmd, cwd=cwd, creationflags=create_new_console)
        return
    subprocess.Popen(cmd, cwd=cwd)


def open_provider_login(provider: str) -> Dict[str, Any]:
    """Open one provider's own login flow in a detached terminal window.

    Returns immediately; it does not wait for the human to finish signing in, and never touches
    whatever credential that flow ends up storing.
    """
    if provider not in PROVIDERS:
        return {"ok": False, "error": f"unknown provider '{provider}'"}

    try:
        executable = _resolve_executable(provider)
    except Exception as exc:
        return {"ok": False, "error": f"'{provider}' is not installed: {redact(str(exc))}"}

    cmd = [executable] + _LOGIN_ARGS[provider]
    title = f"{provider.capitalize()} Login"
    try:
        _spawn_detached_terminal(cmd, cwd=os.getcwd(), title=title)
    except Exception as exc:
        return {"ok": False, "error": redact(str(exc))}
    return {"ok": True, "provider": provider}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_provider_auth.py -v`
Expected: PASS (all tests in the file)

- [ ] **Step 5: Commit**

```bash
git add orchestrator/provider_auth.py tests/test_provider_auth.py
git commit -m "$(cat <<'EOF'
Add check_all_providers, CLI report formatting, and a detached login launcher

open_provider_login spawns each provider's own login flow in an untracked terminal window
rather than reusing run_agent_cli, which would force-kill an indefinite interactive login
session past its timeout.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: `--check-providers` CLI flag

**Files:**
- Modify: `orchestrator/__main__.py:64` (import), `orchestrator/__main__.py:337` (new argparse argument, right after the existing `--doctor` block), `orchestrator/__main__.py:814` (new dispatch block, right after the existing `--doctor` dispatch block returns)
- Test: `tests/test_provider_auth_cli.py`

**Interfaces:**
- Consumes: `orchestrator.provider_auth.check_all_providers`, `format_provider_report`, `providers_ok` (Task 3).
- Produces: nothing new consumed elsewhere — this is a leaf CLI entry point.

- [ ] **Step 1: Write the failing test**

Create `tests/test_provider_auth_cli.py`:

```python
"""Package G - the `--check-providers` CLI flag."""

import contextlib
import io
import sys
import unittest
from unittest.mock import patch

import orchestrator.__main__ as cli


class TestCheckProvidersFlag(unittest.TestCase):
    def test_prints_report_and_exits_zero_when_no_probe_errored(self):
        report = [
            {"provider": "claude", "installed": True, "auth_state": "auth_unverifiable",
             "detail": "claude responds", "subscription_detail": ""},
            {"provider": "opencode", "installed": True, "auth_state": "authenticated",
             "detail": "providers: anthropic", "subscription_detail":
                 "Authentication verified; subscription status cannot be confirmed."},
            {"provider": "antigravity", "installed": False, "auth_state": "not_installed",
             "detail": "not found", "subscription_detail": ""},
        ]
        out = io.StringIO()
        # Patched on `orchestrator.__main__`, not `orchestrator.provider_auth`: __main__.py does
        # `from orchestrator.provider_auth import check_all_providers`, a direct name import, so
        # the name a patch must replace is the one __main__'s dispatch code actually looks up.
        with patch.object(sys, "argv", ["orchestrator", "--check-providers"]), \
                patch("orchestrator.__main__.check_all_providers", return_value=report), \
                contextlib.redirect_stdout(out):
            code = cli.main()
        text = out.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("claude", text)
        self.assertIn("opencode", text)
        self.assertIn("antigravity", text)

    def test_exits_nonzero_when_a_probe_itself_errored(self):
        report = [
            {"provider": "claude", "installed": True, "auth_state": "cli_error",
             "detail": "boom", "subscription_detail": ""},
        ]
        out = io.StringIO()
        with patch.object(sys, "argv", ["orchestrator", "--check-providers"]), \
                patch("orchestrator.__main__.check_all_providers", return_value=report), \
                contextlib.redirect_stdout(out):
            code = cli.main()
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/test_provider_auth_cli.py -v`
Expected: FAIL — `AttributeError` or argparse error: `unrecognized arguments: --check-providers`

- [ ] **Step 3: Wire the flag into `orchestrator/__main__.py`**

Add the import on its own line directly after the existing preflight import (currently `orchestrator/__main__.py:64`, `from orchestrator.preflight import format_preflight_report, run_preflight`):

```python
from orchestrator.provider_auth import check_all_providers, format_provider_report, providers_ok
```

Add the new argument immediately after the existing `--doctor` `parser.add_argument(...)` block (currently ending at `orchestrator/__main__.py:337`, right before the `--deep-preflight` argument):

```python
    parser.add_argument(
        "--check-providers",
        action="store_true",
        dest="check_providers",
        default=False,
        help=(
            "Check whether claude, opencode, and agy are installed and, where officially "
            "supported non-interactively, signed in. Reads no credential of any provider."
        ),
    )
```

Add the dispatch block immediately after the existing `--doctor` block's `return 0 if report.get("ok") else 1` line (currently `orchestrator/__main__.py:814`), before the `# --- Run store browsing` comment:

```python
    # --- Provider account linking (Package G) ------------------------------
    if args.check_providers:
        report = check_all_providers()
        safe_print("")
        safe_print(format_provider_report(report))
        return 0 if providers_ok(report) else 1
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/test_provider_auth_cli.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add orchestrator/__main__.py tests/test_provider_auth_cli.py
git commit -m "$(cat <<'EOF'
Add --check-providers CLI flag

Parallel to --doctor rather than merged into it: --doctor's contract is run-readiness for
configured agents (binary/model/role); this is a different question, independent of any
configured team, and mixing them would change --doctor's existing output shape.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: The `/settings` web page

**Files:**
- Create: `orchestrator/web/settings.html`
- Modify: `orchestrator/web/cockpit.html:918-934` (add a Settings link to the header controls)
- Test: `tests/test_provider_settings_page.py`

**Interfaces:**
- Consumes: none (static HTML/JS; talks to the daemon routes Task 6 adds, at runtime in a browser — not exercised by this task's test).
- Produces: a file at `orchestrator/web/settings.html` that Task 6's `_page("settings.html")` calls into; a `href="/settings"` link in `cockpit.html`.

This page is plain HTML/CSS/JS, matching `cockpit.html`/`office.html`/`design.html` (no framework, no build step). Its test checks the page's static content only — the fetch/POST logic runs in a browser and isn't exercised by `unittest`; Task 6's daemon-level tests exercise the routes it calls.

- [ ] **Step 1: Write the failing test**

Create `tests/test_provider_settings_page.py`:

```python
"""Package G - the /settings page's static content.

Content-only checks, in the same spirit as `test_reliability.py`'s page-text assertions: this
confirms the page ships the elements its JS and Task 6's daemon routes depend on, not that the
JS executes (there is no browser in this suite).
"""

import unittest

from orchestrator.serve import WEB_DIR


class TestSettingsPageContent(unittest.TestCase):
    def setUp(self):
        self.page = (WEB_DIR / "settings.html").read_text(encoding="utf-8")

    def test_carries_the_token_placeholder(self):
        self.assertIn('name="orchestrator-token"', self.page)
        self.assertIn("__ORCHESTRATOR_TOKEN__", self.page)

    def test_fetches_the_providers_api(self):
        self.assertIn("/api/providers", self.page)

    def test_posts_to_the_login_control_action(self):
        self.assertIn("/api/control/provider_login", self.page)

    def test_has_a_container_for_provider_cards(self):
        self.assertIn('id="provider-cards"', self.page)

    def test_has_login_and_check_again_actions(self):
        self.assertIn("Login", self.page)
        self.assertIn("Check again", self.page)

    def test_names_all_three_providers_in_script(self):
        for provider in ("claude", "opencode", "antigravity"):
            self.assertIn(provider, self.page)


class TestCockpitLinksToSettings(unittest.TestCase):
    def test_cockpit_header_links_to_settings(self):
        page = (WEB_DIR / "cockpit.html").read_text(encoding="utf-8")
        self.assertIn('href="/settings"', page)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_provider_settings_page.py -v`
Expected: FAIL — `FileNotFoundError` (settings.html doesn't exist yet) and the cockpit link assertion fails

- [ ] **Step 3: Create `orchestrator/web/settings.html`**

```html
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta name="orchestrator-token" content="__ORCHESTRATOR_TOKEN__">
    <title>SYNAGON — Settings</title>
    <style>
:root {
    --bg: #1C1B19;
    --panel: #2D2C29;
    --green: #519F50;
    --amber: #FBB829;
    --red: #EF2F27;
    --cyan: #0AAEB3;
    --dim: #918175;
    --text: #FCE8C3;
    --text-dim: #BAA67F;
}
* { box-sizing: border-box; }
body {
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font-family: 'Cascadia Code', 'Consolas', monospace;
    padding: 24px;
}
h1 {
    font-size: 18px;
    letter-spacing: 2px;
    color: var(--cyan);
    margin: 0 0 4px 0;
}
p.subtitle {
    color: var(--text-dim);
    margin: 0 0 24px 0;
    font-size: 13px;
}
a.back-link {
    color: var(--cyan);
    text-decoration: none;
    font-size: 13px;
}
#provider-cards {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
    gap: 16px;
    margin-top: 20px;
}
.provider-card {
    background: var(--panel);
    border: 1px solid #3a3936;
    border-radius: 6px;
    padding: 16px;
}
.provider-card h2 {
    margin: 0 0 12px 0;
    font-size: 15px;
    color: var(--text);
}
.provider-row {
    display: flex;
    justify-content: space-between;
    font-size: 13px;
    margin: 6px 0;
    gap: 12px;
}
.provider-row .label { color: var(--text-dim); }
.state-authenticated { color: var(--green); }
.state-auth_unverifiable, .state-not_authenticated, .state-auth_expired { color: var(--amber); }
.state-not_installed, .state-cli_error, .state-timed_out, .state-provider_unavailable { color: var(--red); }
.detail {
    font-size: 12px;
    color: var(--text-dim);
    margin-top: 8px;
    line-height: 1.4;
}
.card-actions {
    margin-top: 14px;
    display: flex;
    gap: 8px;
}
button {
    background: var(--panel);
    border: 1px solid var(--dim);
    color: var(--text);
    padding: 6px 12px;
    border-radius: 4px;
    cursor: pointer;
    font-family: inherit;
    font-size: 12px;
}
button:hover { border-color: var(--cyan); color: var(--cyan); }
button:disabled { opacity: 0.5; cursor: default; }
#error-banner {
    display: none;
    background: var(--red);
    color: var(--bg);
    padding: 8px 12px;
    border-radius: 4px;
    margin-bottom: 16px;
    font-size: 13px;
}
    </style>
</head>
<body>
    <a class="back-link" href="/cockpit">&larr; Cockpit</a>
    <h1>PROVIDER ACCOUNTS</h1>
    <p class="subtitle">
        Whether each local AI provider CLI is installed and signed in under your own account.
        Synagon never reads, stores, or transmits a credential of any kind — it only launches
        each provider's own login flow and reads status from commands that provider documents
        as safe and non-interactive.
    </p>
    <div id="error-banner"></div>
    <div id="provider-cards">Loading…</div>

    <script>
    const TOKEN = (() => {
        const meta = document.querySelector('meta[name="orchestrator-token"]');
        const raw = meta ? meta.getAttribute('content') : '';
        return (raw && raw.indexOf('__') !== 0) ? raw : '';
    })();

    const PROVIDER_LABELS = {
        claude: 'Claude Code',
        opencode: 'OpenCode',
        antigravity: 'Antigravity (agy)',
    };

    const STATE_LABELS = {
        authenticated: 'Verified',
        auth_unverifiable: 'Cannot be confirmed automatically',
        not_authenticated: 'Not signed in',
        auth_expired: 'Login expired',
        not_installed: 'Not installed',
        provider_unavailable: 'Unavailable',
        cli_error: 'CLI error',
        timed_out: 'Timed out',
    };

    async function apiGet(path) {
        const res = await fetch(path, { headers: { 'X-Orchestrator-Token': TOKEN } });
        if (!res.ok) throw new Error(`API error: ${res.status}`);
        return res.json();
    }

    async function apiControl(verb, body) {
        const res = await fetch('/api/control/' + verb, {
            method: 'POST',
            headers: { 'X-Orchestrator-Token': TOKEN, 'Content-Type': 'application/json' },
            body: JSON.stringify(body || {}),
        });
        return res.json();
    }

    function showError(message) {
        const banner = document.getElementById('error-banner');
        banner.textContent = message;
        banner.style.display = message ? 'block' : 'none';
    }

    function renderCard(status) {
        const label = PROVIDER_LABELS[status.provider] || status.provider;
        const stateLabel = STATE_LABELS[status.auth_state] || status.auth_state;
        const installedText = status.installed ? 'Yes' : 'No';
        const subscriptionLine = status.subscription_detail
            ? `<div class="provider-row"><span class="label">Subscription</span><span>${status.subscription_detail}</span></div>`
            : '';
        return `
            <div class="provider-card" data-provider="${status.provider}">
                <h2>${label}</h2>
                <div class="provider-row"><span class="label">Installed</span><span>${installedText}</span></div>
                <div class="provider-row"><span class="label">Authentication</span>
                    <span class="state-${status.auth_state}">${stateLabel}</span></div>
                ${subscriptionLine}
                <div class="detail">${status.detail || ''}</div>
                <div class="card-actions">
                    <button data-action="login" data-provider="${status.provider}">Login</button>
                    <button data-action="check" data-provider="${status.provider}">Check again</button>
                </div>
            </div>
        `;
    }

    async function loadProviders() {
        try {
            const data = await apiGet('/api/providers');
            const container = document.getElementById('provider-cards');
            container.innerHTML = (data.providers || []).map(renderCard).join('');
            showError('');
        } catch (err) {
            showError('Could not load provider status: ' + err.message);
        }
    }

    document.getElementById('provider-cards').addEventListener('click', async (event) => {
        const button = event.target.closest('button[data-action]');
        if (!button) return;
        const provider = button.getAttribute('data-provider');
        if (button.getAttribute('data-action') === 'check') {
            await loadProviders();
            return;
        }
        button.disabled = true;
        try {
            const result = await apiControl('provider_login', { provider });
            if (!result.ok) showError(result.error || 'Could not start login.');
        } catch (err) {
            showError('Could not start login: ' + err.message);
        } finally {
            button.disabled = false;
        }
    });

    loadProviders();
    </script>
</body>
</html>
```

- [ ] **Step 4: Add the cockpit header link**

In `orchestrator/web/cockpit.html`, find this exact block (around line 918):

```html
            <div class="goal-controls">
                <select id="theme-select" style="font-size: 15px">
```

Replace it with:

```html
            <div class="goal-controls">
                <a href="/settings" class="primary-btn" style="text-decoration:none; display:inline-flex; align-items:center;">SETTINGS</a>
                <select id="theme-select" style="font-size: 15px">
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest tests/test_provider_settings_page.py -v`
Expected: PASS (7 tests)

- [ ] **Step 6: Commit**

```bash
git add orchestrator/web/settings.html orchestrator/web/cockpit.html tests/test_provider_settings_page.py
git commit -m "$(cat <<'EOF'
Add the /settings page for provider account linking

Plain HTML/CSS/JS matching cockpit.html/office.html/design.html - no new frontend
dependency. Linked from the cockpit header for discoverability.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: Daemon wiring — `GET /api/providers`, `POST /api/control/provider_login`, and the `/settings` route

**Files:**
- Modify: `orchestrator/daemon.py` (four insertions, detailed below)
- Test: `tests/test_package_g.py`

**Interfaces:**
- Consumes: `orchestrator.provider_auth.check_all_providers`, `open_provider_login` (Task 3); `orchestrator.web.settings.html` existing on disk (Task 5).
- Produces: `Daemon.provider_status(self) -> Dict[str, Any]` (`{"providers": [...]}`); `Daemon.provider_login(self, provider: str) -> Dict[str, Any]`; the `"provider_login"` verb in `Daemon.control()`; `GET /api/providers`; `GET /settings` and `/settings.html`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_package_g.py`:

```python
"""Package G - the daemon's provider account-linking routes.

Shaped like `tests/test_package_c.py`'s daemon fixture: a real `serve_daemon` on an OS-assigned
port, called over HTTP with the daemon's own token, torn down after each test. No real provider
CLI is ever invoked here - `provider_auth`'s own subprocess-level behavior is covered by
`tests/test_provider_auth.py`; this file only proves the daemon wires it up correctly.
"""

import json
import shutil
import tempfile
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from orchestrator.daemon import TOKEN_HEADER, Daemon, serve_daemon


class _ProviderDaemonCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="orch_pkgg_")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.config = {"agents": [], "roles": {}}
        self.daemon = Daemon(self.root, self.config, token="t")
        self.server = serve_daemon(
            self.root, self.config, port=0, open_browser=False, serve_forever=False,
            printer=lambda *_: None, daemon=self.daemon,
        )
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]

    def tearDown(self):
        self.daemon.stopping.set()
        self.server.shutdown()
        self.server.server_close()

    def _get(self, path):
        request = urllib.request.Request(self.base + path)
        request.add_header(TOKEN_HEADER, "t")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def _call(self, path, payload=None):
        body = json.dumps(payload if payload is not None else {}).encode("utf-8")
        request = urllib.request.Request(self.base + path, data=body)
        request.add_header(TOKEN_HEADER, "t")
        request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8") or "{}")


_FAKE_REPORT = [
    {"provider": "claude", "installed": True, "auth_state": "auth_unverifiable",
     "detail": "d", "subscription_detail": "", "checked_at": 0.0, "duration_seconds": 0.0},
]


class TestProvidersRoute(_ProviderDaemonCase):
    def test_get_api_providers_returns_the_report(self):
        with patch("orchestrator.provider_auth.check_all_providers", return_value=_FAKE_REPORT):
            status, body = self._get("/api/providers")
        self.assertEqual(status, 200)
        data = json.loads(body.decode("utf-8"))
        self.assertEqual(data["providers"][0]["provider"], "claude")

    def test_requires_the_daemon_token(self):
        request = urllib.request.Request(self.base + "/api/providers")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(request, timeout=10)
        self.assertEqual(ctx.exception.code, 401)


class TestSettingsPageRoute(_ProviderDaemonCase):
    def test_settings_serves_the_page_with_token_substituted(self):
        status, body = self._get("/settings")
        self.assertEqual(status, 200)
        text = body.decode("utf-8")
        self.assertIn("PROVIDER ACCOUNTS", text)
        self.assertNotIn("__ORCHESTRATOR_TOKEN__", text)
        self.assertIn('content="t"', text)

    def test_settings_html_alias_works_too(self):
        status, _ = self._get("/settings.html")
        self.assertEqual(status, 200)


class TestProviderLoginControlAction(_ProviderDaemonCase):
    def test_login_delegates_to_open_provider_login(self):
        with patch(
            "orchestrator.provider_auth.open_provider_login",
            return_value={"ok": True, "provider": "claude"},
        ) as mock_login:
            status, body = self._call("/api/control/provider_login", {"provider": "claude"})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        mock_login.assert_called_once_with("claude")

    def test_missing_provider_is_a_400_not_a_crash(self):
        status, body = self._call("/api/control/provider_login", {})
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])

    def test_login_failure_from_provider_auth_is_reported_not_raised(self):
        with patch(
            "orchestrator.provider_auth.open_provider_login",
            return_value={"ok": False, "error": "not installed"},
        ):
            status, body = self._call("/api/control/provider_login", {"provider": "claude"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "not installed")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_package_g.py -v`
Expected: FAIL — `/api/providers` and `/settings` 404, `provider_login` control action returns `{"unknown": True}` at 404

- [ ] **Step 3: Wire the routes into `orchestrator/daemon.py`**

Add a page route. Find this exact block in `do_GET` (around line 930):

```python
            if route in ("/design", "/design.html"):
                self._page("design.html")
                return
```

Replace it with:

```python
            if route in ("/design", "/design.html"):
                self._page("design.html")
                return
            if route in ("/settings", "/settings.html"):
                self._page("settings.html")
                return
```

Add the read route. Find this exact block in `do_GET` (around line 972):

```python
            if route == "/api/cockpit":
                try:
                    self._send(_json_bytes(daemon.cockpit()), "application/json")
                except Exception as exc:
                    self._send(_json_bytes({"error": str(exc)}), "application/json", status=500)
                return
```

Add immediately after it:

```python
            if route == "/api/providers":
                self._send(_json_bytes(daemon.provider_status()), "application/json")
                return
```

Add the control verb. Find this exact block in `Daemon.control()` (around line 472):

```python
        if verb == "deliver":
            return self.deliver(str(payload.get("card") or payload.get("id") or ""))

        return {"ok": False, "unknown": True, "error": "no control action '%s'" % verb}
```

Replace it with:

```python
        if verb == "deliver":
            return self.deliver(str(payload.get("card") or payload.get("id") or ""))
        if verb == "provider_login":
            return self.provider_login(str(payload.get("provider") or ""))

        return {"ok": False, "unknown": True, "error": "no control action '%s'" % verb}
```

Add the two `Daemon` methods. Find this exact block (the `board` method, around line 317):

```python
    def board(self) -> Dict[str, Any]:
        """The board projection, read exactly as ``--board`` reads it."""
        from orchestrator.__main__ import read_board

        return read_board(self.project_root, self.config)
```

Add immediately after it:

```python
    def provider_status(self) -> Dict[str, Any]:
        """Every local AI provider's account-linking status, for the settings page (Package G).

        Reads no credential of any kind - see `provider_auth`'s module docstring. This is a
        read, so it belongs with `board`/`cockpit`, not with the control actions below.
        """
        from orchestrator.provider_auth import check_all_providers

        return {"providers": check_all_providers()}

    def provider_login(self, provider: str) -> Dict[str, Any]:
        """Open one provider's own login flow in a detached terminal (Package G).

        Never waits on it and never touches whatever credential that flow ends up storing.
        """
        from orchestrator.provider_auth import open_provider_login

        if not provider:
            return {"ok": False, "error": "a provider was expected"}
        return open_provider_login(provider)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_package_g.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Run the full test suite to confirm nothing else broke**

Run: `python -m pytest tests/ -x -q`
Expected: PASS (every test, including the pre-existing `test_package_c.py`, `test_tier1.py`, `test_reliability.py`)

- [ ] **Step 6: Commit**

```bash
git add orchestrator/daemon.py tests/test_package_g.py
git commit -m "$(cat <<'EOF'
Wire provider account linking into the daemon: /api/providers, /settings, provider_login

GET /api/providers and the /settings page route follow the existing read-route pattern
(auth-gated like /api/cockpit); provider_login joins Daemon.control()'s closed dispatch
list alongside start/cancel/approve/reject/deliver.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: Documentation — README and CHANGELOG

**Files:**
- Modify: `README.md:259-269` (new section after Prerequisites), `README.md:304-308` (new Troubleshooting bullet)
- Modify: `CHANGELOG.md:1-5` (new Package G section, above the existing Package F section)

**Interfaces:**
- Consumes: nothing (documentation only).
- Produces: nothing consumed by other tasks — this is the last task.

- [ ] **Step 1: Add the new README section**

In `README.md`, find this exact block (the end of Prerequisites and the start of Terminal bridge, around line 269):

```markdown
- **`gh`** (GitHub CLI), already authenticated, only if you turn on `delivery.enabled` — nothing
  pushes or opens a pull request without it.

## Terminal bridge (Antigravity IDE)
```

Replace it with:

```markdown
- **`gh`** (GitHub CLI), already authenticated, only if you turn on `delivery.enabled` — nothing
  pushes or opens a pull request without it.

## Provider account linking

`--check-providers` (or the cockpit's **Settings** page, `/settings` on the daemon) checks
whether `claude`, `opencode`, and `agy` are installed and, where officially supported
non-interactively, signed in — without Synagon ever reading, storing, or transmitting a
credential of any kind. It only runs commands each provider documents as safe: `claude
--version`, `agy --version`, and `opencode auth list` (the one of the three with a real,
documented, secret-free "who's signed in" command).

```powershell
python -m orchestrator --check-providers
```

Possible states per provider:

| State | Meaning |
| --- | --- |
| Not installed | The binary isn't on `PATH`. |
| Cannot be confirmed automatically | **Claude Code and `agy` always report this.** Neither CLI documents a non-interactive way to check sign-in status — Anthropic's and Google's own docs confirm it (see `docs/superpowers/specs/2026-09-12-package-g-provider-account-linking-design.md` for the citations). This is not an error; click **Login** (or run the CLI yourself) to verify. |
| Not signed in / Verified | **OpenCode only.** `opencode auth list` genuinely reports which providers have stored credentials. |
| Subscription: cannot be confirmed | Shown whenever authentication is confirmed — none of the three CLIs exposes quota/entitlement data through a documented non-interactive command, so this is never guessed at. |
| CLI error / Timed out | The probe itself failed to run cleanly — worth a look, independent of your account. |

**Login** opens the provider's own login flow in a normal terminal window — `claude` and `agy`
plainly, `opencode auth login` for OpenCode's interactive provider picker — and Synagon does not
wait for it or touch what it stores. This is the same zero-provider-API-key invariant the rest
of this README describes, restated for account status specifically: Synagon links to your
already-authenticated CLI, never to a credential it holds itself.

## Terminal bridge (Antigravity IDE)
```

- [ ] **Step 2: Add the Troubleshooting bullet**

In `README.md`, find this exact block (around line 306):

```markdown
## Troubleshooting

- **`--doctor` fails for an agent.** It probes the exact binary, model and role your config
  names; the failure message says which one. Fix the CLI's own auth/installation first — Synagon
  never retries past a preflight failure.
```

Replace it with:

```markdown
## Troubleshooting

- **`--doctor` fails for an agent.** It probes the exact binary, model and role your config
  names; the failure message says which one. Fix the CLI's own auth/installation first — Synagon
  never retries past a preflight failure.
- **`--check-providers` (or the Settings page) says "cannot be confirmed automatically" for
  claude or agy.** That is the honest answer, not a bug — see "Provider account linking" above.
  Click Login, or run `claude`/`agy` yourself, to check.
```

- [ ] **Step 3: Add the CHANGELOG entry**

In `CHANGELOG.md`, find this exact block (the very top, around line 1-6):

```markdown
# Changelog

## Unreleased

### Package F — release readiness
```

Replace it with:

```markdown
# Changelog

## Unreleased

### Package G — local AI provider account linking

A read-only status check for whether `claude`, `opencode`, and `agy` are installed and (where
officially documented non-interactively — OpenCode only) signed in, plus a way to launch each
provider's own login flow. Synagon reads no credential of any kind; `claude` and `agy` honestly
report "cannot be confirmed automatically" rather than guessing, since neither CLI documents a
non-interactive auth-status command (verified against Anthropic's and Google's own docs — see
`docs/superpowers/specs/2026-09-12-package-g-provider-account-linking-design.md`).

- New `orchestrator/provider_auth.py`, `--check-providers`, and a cockpit **Settings** page
  (`/settings`) with one card per provider and Login / Check again actions.

### Package F — release readiness
```

- [ ] **Step 4: Verify the docs render sensibly**

Run: `python -c "import pathlib; text = pathlib.Path('README.md').read_text(encoding='utf-8'); assert '## Provider account linking' in text; assert text.count('## ') >= 15; print('README OK')"`
Expected: prints `README OK`

- [ ] **Step 5: Commit**

```bash
git add README.md CHANGELOG.md
git commit -m "$(cat <<'EOF'
Document provider account linking in README and CHANGELOG

Explains the --check-providers flag, the /settings page, what each status state means,
and why claude/agy can't be verified non-interactively (with citations).

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

## Final verification (after all seven tasks)

- [ ] Run the complete suite once more: `python -m pytest tests/ -q`
- [ ] Manually smoke-test the CLI: `python -m orchestrator --check-providers` in a real shell and confirm it prints one block per provider without crashing (results will reflect whatever is actually installed on the machine running this).
- [ ] Manually smoke-test the UI: start the daemon (`python -m orchestrator --daemon`), open the URL it prints, click through to **Settings**, confirm the three provider cards render, and that **Check again** re-fetches without a page reload. Confirm **Login** opens a real terminal window for at least one installed CLI. This UI smoke test cannot be automated in this suite (no browser) and must be done by hand before calling the feature done for real use.
