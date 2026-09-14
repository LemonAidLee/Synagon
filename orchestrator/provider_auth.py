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
from orchestrator.agents.codex import get_codex_executable_path
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
#: Reserved for a provider name outside the three known ones, in the sense the design spec uses
#: it - current code instead refuses an unknown provider through a different path (see
#: `_resolve_executable` and `open_provider_login`'s `PROVIDERS` check), so no probe emits this
#: today. Kept so a future provider that needs it doesn't need a new state name.
AUTH_PROVIDER_UNAVAILABLE = "provider_unavailable"
AUTH_CLI_ERROR = "cli_error"
AUTH_TIMED_OUT = "timed_out"

SUBSCRIPTION_UNAVAILABLE = "unavailable"
#: Required verbatim by the design spec whenever auth_state == AUTH_AUTHENTICATED.
SUBSCRIPTION_CONFIRMED_DETAIL = "Authentication verified; subscription status cannot be confirmed."

#: The CLIs this module knows how to probe, in a fixed, stable order. `codex` is appended
#: last rather than inserted so no existing positional expectation - in a test, in the settings
#: page's card order, in a stored report - shifts underneath Package H.
PROVIDERS = ("claude", "opencode", "antigravity", "codex")

DEFAULT_TIMEOUT = 15

_VERSION_ARGS = {
    "claude": ["--version"],
    "opencode": ["--version"],
    "antigravity": ["--version"],
    "codex": ["--version"],
}

#: Argv appended to the resolved executable to open each provider's own login flow.
_LOGIN_ARGS: Dict[str, List[str]] = {
    "claude": [],
    "opencode": ["auth", "login"],
    "antigravity": [],
    "codex": ["login"],
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
    if provider == "codex":
        return get_codex_executable_path()
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


#: Substrings (checked case-insensitively against the whole trimmed output) that OpenCode's own
#: `auth list` is expected to use for "nothing is configured" - kept short and literal on
#: purpose (see the design spec's "Risks" section: fail closed on anything unrecognised rather
#: than widen this list speculatively).
_NO_PROVIDERS_MARKERS = ("no providers", "no authenticated providers", "not logged in", "no credentials")

#: A real opencode install (verified against 1.18.29) renders its box-drawn UI summary as
#: "0 credentials" rather than any of the string markers above - the digit zero, not the word
#: "no". Checked as its own regex (case-insensitive, tolerant of surrounding whitespace) rather
#: than folded into `_NO_PROVIDERS_MARKERS` since it is a pattern, not a literal substring.
_ZERO_CREDENTIALS_RE = re.compile(r"\b0\s+credentials?\b", re.IGNORECASE)

#: Standard ANSI CSI escape sequence (e.g. `\x1b[90m`, `\x1b[39m`, `\x1b[0m`) - colour/style
#: codes a real terminal-facing CLI (opencode 1.18.29 included) wraps its box-drawn UI output in.
#: Stripped before any parsing logic runs so these bytes can never end up mistaken for part of a
#: provider name, nor leak into the `detail` field shown to users.
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

#: Line prefixes that mark a header/separator row rather than a provider entry. "credentials"
#: covers the box-drawn UI's own header row ("Credentials ~/path/to/auth.json", decoration
#: stripped) - the same section title `_NO_PROVIDERS_MARKERS`/`_ZERO_CREDENTIALS_RE` recognise
#: when it appears with a "0" summary, but here skipped on its own merits as a header, not data.
_SKIP_LINE_PREFIXES = ("provider", "-", "=", "credentials")

#: Non-word characters (box-drawing glyphs, bullets, stray leading punctuation) a UI-styled CLI
#: prefixes each output line with. Stripped from the front of every line before the header check
#: and before a token is extracted, so a decorated row like "│ anthropic" recovers "anthropic"
#: rather than the leading glyph itself becoming the (rejected) token.
_LEADING_DECORATION_RE = re.compile(r"^[^\w]+")

#: A bare "N credentials"/"N credential" line (opencode's box UI ends its output with one, e.g.
#: "└  2 credentials") is a summary/count row, not a provider entry - skipped regardless of N,
#: the same "fail closed" way `_ZERO_CREDENTIALS_RE` treats the N=0 case at the whole-output level.
_CREDENTIALS_COUNT_LINE_RE = re.compile(r"^\d+\s+credentials?$", re.IGNORECASE)


def _parse_opencode_auth_list(text: str) -> Optional[List[str]]:
    """Provider names out of `opencode auth list` output, or `None` if unparseable.

    Conservative on purpose: a recognisable "nothing configured" message is zero providers;
    each remaining non-header, non-blank line's first token is taken as a provider id; and if
    that leaves nothing at all (e.g. a header row with no data rows) this returns `None` so the
    caller reports a CLI error instead of silently claiming "not authenticated".

    ANSI escape sequences are stripped first: a real opencode install renders `auth list` as a
    colorized, box-drawn UI ("┌ Credentials ...", "│", "└  0 credentials") rather than plain
    text, and without stripping, the escape codes and box-drawing characters would otherwise be
    parsed as "provider names" - exactly the guessing this parser exists to refuse. Leading
    decoration characters (the box-drawing glyphs themselves) are then stripped per-line, and any
    resulting token with no alphanumeric content is rejected rather than accepted as a provider
    id - defense in depth for box-drawn styles this exact parser hasn't seen yet.
    """
    text = _ANSI_ESCAPE_RE.sub("", text)
    stripped = text.strip()
    if not stripped:
        return []
    lowered = stripped.lower()
    if any(marker in lowered for marker in _NO_PROVIDERS_MARKERS) or _ZERO_CREDENTIALS_RE.search(lowered):
        return []

    providers: List[str] = []
    for line in stripped.splitlines():
        line = _LEADING_DECORATION_RE.sub("", line.strip()).strip()
        if not line or line.lower().startswith(_SKIP_LINE_PREFIXES):
            continue
        if _CREDENTIALS_COUNT_LINE_RE.match(line):
            continue
        token = line.split()[0].rstrip(":")
        if token and re.search(r"[A-Za-z0-9]", token):
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

    # Stripped once, here, so every use of `text` below - this branch's error detail and the
    # `_parse_opencode_auth_list` call - sees ANSI-free text. `_parse_opencode_auth_list` also
    # strips it internally (idempotent, so harmless when called directly or with plain text).
    text = _ANSI_ESCAPE_RE.sub("", _decode(completed.stdout) + _decode(completed.stderr))
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


#: Measured against codex-cli 0.154.0. `codex login status` prints its answer to *stderr*
#: (stdout is empty) and exits 1 when signed out - so the exit code alone is not the signal,
#: and a non-zero exit here is a legitimate answer rather than a probe failure. Treating it the
#: way `check_opencode_auth` correctly treats a non-zero `auth list` would misreport every
#: signed-out user as a CLI error.
#:
#: Order matters when these are tested: "not logged in" contains "logged in", so the negative
#: markers are always checked first.
_CODEX_SIGNED_OUT_MARKERS = ("not logged in", "not signed in")
_CODEX_SIGNED_IN_MARKERS = ("logged in", "signed in")


def check_codex_auth(timeout: int = DEFAULT_TIMEOUT) -> ProviderAuthStatus:
    """Codex: `codex login status` is an officially documented non-interactive status command.

    With `opencode`, this is the second of the four providers with a real auth signal, so it
    never needs AUTH_UNVERIFIABLE. It reports the auth *mode* the CLI gives it ("Logged in
    using ChatGPT"), which says how the user authenticated - never that a paid plan is active.
    Subscription stays `unavailable`: no non-interactive Codex command reports plan, quota, or
    entitlement, so claiming one would be exactly the guess this module exists to refuse.

    `~/.codex/auth.json` is never opened, and its mere existence is never treated as an auth
    signal - the same refusal this module already applies to every other provider.
    """
    started = time.time()
    try:
        executable = get_codex_executable_path()
    except Exception as exc:
        return _status("codex", False, None, AUTH_NOT_INSTALLED, str(exc), started)

    try:
        completed = _run(executable, ["login", "status"], timeout)
    except _ProbeTimeout:
        return _status(
            "codex", True, executable, AUTH_TIMED_OUT,
            f"'codex login status' did not respond within {timeout}s.", started,
        )
    except _ProbeFailed as exc:
        return _status(
            "codex", True, executable, AUTH_CLI_ERROR,
            f"Could not run 'codex login status': {exc}", started,
        )

    # Measured: the answer arrives on stderr and stdout is empty. Both are read so a future
    # version that moves the line to stdout keeps working without a code change. ANSI is
    # stripped defensively - this command emitted none when measured, but a parsed stream
    # should never depend on that staying true.
    text = _ANSI_ESCAPE_RE.sub("", _decode(completed.stdout) + _decode(completed.stderr)).strip()
    lowered = text.lower()

    if any(marker in lowered for marker in _CODEX_SIGNED_OUT_MARKERS):
        return _status(
            "codex", True, executable, AUTH_NOT_AUTHENTICATED,
            "Codex is installed but no account is signed in. Click Login to sign in with your "
            "own ChatGPT account.", started,
        )

    if any(marker in lowered for marker in _CODEX_SIGNED_IN_MARKERS):
        # Report the CLI's own sentence rather than a rephrasing of it, picked out of any
        # surrounding warning lines (a non-default CODEX_HOME emits one, measured).
        signal = next(
            (line.strip() for line in text.splitlines()
             if any(marker in line.lower() for marker in _CODEX_SIGNED_IN_MARKERS)),
            text.splitlines()[0] if text else "",
        )
        return _status(
            "codex", True, executable, AUTH_AUTHENTICATED,
            f"{signal} (reported by 'codex login status'). This confirms how you signed in, "
            "not which plan you hold.", started,
            subscription_state=SUBSCRIPTION_UNAVAILABLE,
            subscription_detail=SUBSCRIPTION_CONFIRMED_DETAIL,
        )

    return _status(
        "codex", True, executable, AUTH_CLI_ERROR,
        f"Could not interpret 'codex login status' output: {text[:300]}", started,
    )


_CHECKERS = {
    "claude": check_claude_auth,
    "opencode": check_opencode_auth,
    "antigravity": check_antigravity_auth,
    "codex": check_codex_auth,
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
    """Launch `cmd` detached from this process and return immediately.

    On Windows this opens a real terminal window (`wt.exe` when available, else
    `CREATE_NEW_CONSOLE`) so the person can see and use the login prompt. On POSIX there is no
    portable "open a terminal window" primitive, so this instead starts `cmd` as a detached
    background process, in its own session (`start_new_session=True`) and sharing no window of
    its own - deliberately, so a Ctrl-C at the daemon's own terminal cannot SIGINT a login the
    user left running.

    Deliberately not `orchestrator.launcher.run_agent_cli`: that helper waits for the launched
    process to exit and force-kills the whole tree past its timeout, which is right for an
    agent turn but wrong for an interactive login a person may leave open indefinitely (bare
    `claude` opens an indefinite REPL). This starts the process, is not owned by the
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
    subprocess.Popen(cmd, cwd=cwd, start_new_session=True)


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
