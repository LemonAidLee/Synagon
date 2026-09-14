"""Adapter for orchestrating OpenAI's Codex CLI via `codex exec` non-interactive mode.

Every command, stream, exit code, and output shape encoded here was measured against a live
`codex-cli 0.154.0` install rather than inferred from documentation; the captures are recorded
in docs/superpowers/specs/2026-09-13-package-h-codex-agent-design.md.

Codex is headless-only. It has no documented client/server equivalent to OpenCode's
`serve` + `attach`, so there is no supported way to drive a running interactive session and
detect its completion - `agent_execution_mode: native_tui` therefore refuses it, exactly as it
refuses `claude` and `antigravity`, and this module has no native-TUI branch at all.

Synagon never handles a Codex credential. This module reads no `auth.json`, and never reads,
sets, forwards, or logs `OPENAI_API_KEY`; authentication belongs entirely to the vendor's own
CLI and account system (see `orchestrator.provider_auth.check_codex_auth`).
"""

import json
import os
import shutil
from typing import Any, Dict, List, Optional, Tuple

from orchestrator.agents.exceptions import CLIExecutionError
from orchestrator.launcher import run_agent_cli
from orchestrator.types import TokenUsage, create_token_usage, unavailable_token_usage

#: Path from the npm shim's directory to the native binary that shim wraps.
_NPM_NATIVE_RELATIVE = os.path.join(
    "node_modules", "@openai", "codex", "node_modules", "@openai",
    "codex-win32-x64", "vendor", "x86_64-pc-windows-msvc", "bin", "codex.exe",
)

#: Named in the not-installed error because the most likely reason a user hits it is that they
#: have the Codex/ChatGPT *desktop app* and reasonably expect it to provide the command. It
#: does not: the app is an MSIX package whose bundled codex.exe lives under WindowsApps, which
#: denies execution to other processes, and it publishes no app execution alias. Only the
#: standalone CLI is a supported integration target.
_INSTALL_HINT = (
    "Install the official CLI with 'npm install -g @openai/codex'. Note that the Codex/ChatGPT "
    "desktop app does not provide an invokable 'codex' command: its bundled binary lives under "
    "Program Files\\WindowsApps, which denies execution to other processes, and it publishes "
    "no app execution alias."
)


def get_codex_executable_path() -> str:
    """Resolve the path to the Codex CLI executable dynamically.

    Returns:
        Absolute path to the discovered codex executable.

    Raises:
        FileNotFoundError: If codex cannot be found in PATH or standard npm directories.
    """
    codex_path = shutil.which("codex")
    if codex_path:
        # On Windows, npm creates .cmd/.ps1 shims which route through cmd.exe and inherit its
        # 8192-character command-line limit - a limit a long orchestration prompt exceeds.
        # Prefer the underlying native binary, exactly as get_opencode_executable_path does.
        if codex_path.lower().endswith((".cmd", ".bat", ".ps1")):
            native = os.path.join(os.path.dirname(codex_path), _NPM_NATIVE_RELATIVE)
            if os.path.isfile(native):
                return native
        return codex_path

    for base in (os.path.expandvars(r"%APPDATA%\npm"), os.path.expandvars(r"%LOCALAPPDATA%\npm")):
        native = os.path.join(base, _NPM_NATIVE_RELATIVE)
        if os.path.isfile(native):
            return native
        shim = os.path.join(base, "codex.cmd")
        if os.path.isfile(shim):
            return shim

    raise FileNotFoundError(
        f"Could not find the 'codex' CLI executable in PATH or standard npm directories. "
        f"{_INSTALL_HINT}"
    )


def _iter_events(stdout: str):
    """Yield each parseable JSON object from a JSONL stream, skipping anything else."""
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(event, dict):
            yield event


def _usage_from_turn_completed(usage: Dict[str, Any]) -> TokenUsage:
    """Map Codex's `turn.completed.usage` object onto Synagon's TokenUsage.

    `total_tokens` is deliberately left to `create_token_usage`'s input+output derivation,
    matching every other adapter: Codex reports no total of its own, and computing one here
    would be this module inventing a number the CLI never gave it.
    """
    return create_token_usage(
        input_tokens=usage.get("input_tokens"),
        output_tokens=usage.get("output_tokens"),
        cache_read_tokens=usage.get("cached_input_tokens"),
        cache_write_tokens=usage.get("cache_write_input_tokens"),
        reasoning_tokens=usage.get("reasoning_output_tokens"),
        available=True,
        raw_usage=dict(usage),
    )


def parse_codex_output(stdout: str) -> Tuple[str, TokenUsage]:
    """Parse a `codex exec --json` JSONL stream into response text and TokenUsage.

    Response text is every `item.completed` item whose `item.type` is `agent_message`,
    concatenated in order. Reasoning and command items are deliberately excluded: they are
    the model's working, not its answer, and folding them in would corrupt every downstream
    consumer that treats this string as the role's output.

    Usage comes from `turn.completed.usage`. When that event is absent - an interrupted run,
    or a future schema change - usage is reported *unavailable* rather than zeroed, so drift
    degrades honestly instead of fabricating token counts that would flow into budgets and
    metrics as though they were measured.

    Falls back to returning the raw text when the stream contains no JSON at all (a mocked
    CLI, or a plain-text mode), matching parse_opencode_output's contract.

    Args:
        stdout: Raw standard output from the codex CLI process.

    Returns:
        Tuple of (response_text, token_usage).
    """
    if not stdout or not stdout.strip():
        return "", unavailable_token_usage()

    raw = stdout.strip()
    chunks: List[str] = []
    usage: Optional[TokenUsage] = None
    saw_json = False

    for event in _iter_events(raw):
        saw_json = True
        event_type = event.get("type")

        if event_type == "item.completed":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message":
                text = item.get("text")
                if isinstance(text, str):
                    chunks.append(text)

        elif event_type == "turn.completed":
            reported = event.get("usage")
            if isinstance(reported, dict):
                usage = _usage_from_turn_completed(reported)

    if saw_json and chunks:
        response_text = "".join(chunks).strip()
    elif saw_json:
        response_text = ""
    else:
        response_text = raw

    return response_text, (usage if usage is not None else unavailable_token_usage())


def _stream_error(stdout: str) -> Optional[str]:
    """The authoritative failure cause from a `codex exec --json` stream, if any.

    `turn.failed.error.message` wins over any `error` event. This is not a stylistic
    preference: measured failing runs emit `error` events for *transient, recovered* retries
    ("Reconnecting... 1/5", "Falling back from WebSockets to HTTPS transport"), so reporting
    the last `error` event - the approach the OpenCode adapter can safely take, because its
    stream has no such retry chatter - would frequently name a recovered blip as the cause of
    a failure that actually happened for another reason. Only when no `turn.failed` event is
    present does the last `error` event stand in.
    """
    terminal = None
    last_error = None

    for event in _iter_events(stdout):
        event_type = event.get("type")
        if event_type == "turn.failed":
            error = event.get("error")
            if isinstance(error, dict) and error.get("message"):
                terminal = str(error["message"])
            elif error:
                terminal = str(error)
        elif event_type == "error" and event.get("message"):
            last_error = str(event["message"])

    message = terminal or last_error
    return message[:300] if message else None


def run_codex_with_usage(
    prompt: str,
    timeout: int = 180,
    working_dir: Optional[str] = None,
    extra_args: Optional[List[str]] = None,
    model: Optional[str] = None,
    visible: bool = False,
    title: Optional[str] = None,
    role: Optional[str] = "implementer",
    terminal_type: str = "auto",
    pause_on_completion: float = 1.5,
    tracer: Optional[object] = None,
    close_on_completion: bool = True,
) -> Tuple[str, TokenUsage]:
    """Execute the Codex CLI non-interactively and return response text and TokenUsage.

    Always a headless `codex exec` run, optionally hosted in a visible terminal window. All
    process machinery - timeout enforcement, process-tree ownership and kill, terminal
    hosting, tracing, stdin detachment - is delegated to `run_agent_cli` unchanged, so Codex
    inherits every reliability guarantee the other adapters already have rather than growing
    its own.

    Args:
        prompt: The instructions to send to Codex.
        timeout: Maximum seconds to wait before raising CLITimeoutError. Default is 180s.
        working_dir: Working directory for the CLI process (defaults to cwd).
        extra_args: Optional list of additional flags to pass to codex.
        model: Optional model name passed via the official '-m' flag.
        visible: Whether to execute in a visible terminal window.
        title: Optional title for the terminal window.
        role: Assigned role name (for logging and title).
        terminal_type: Terminal host ('auto', 'antigravity_integrated', 'windows_terminal', 'console').
        pause_on_completion: Seconds to pause upon completion before closing a visible window.
        tracer: Optional tracer instance.
        close_on_completion: Whether the hosting terminal closes after the turn.

    Returns:
        Tuple of (response_text, token_usage).

    Raises:
        CLITimeoutError: If execution exceeds the specified timeout.
        CLIExecutionError: If codex exits with a non-zero code.
        FileNotFoundError: If the codex executable cannot be found.
    """
    executable = get_codex_executable_path()

    # --json                 the parsed JSONL event stream (measured contract).
    # --color never          keeps ANSI escapes out of a stream we parse. Applied up front
    #                        rather than stripped afterwards: fabricating provider names out
    #                        of colour codes and box-drawing glyphs is exactly the defect
    #                        ed45916 fixed for OpenCode, and not emitting them is stronger
    #                        than cleaning them up.
    # --skip-git-repo-check  worktrees and scratch directories are not always git repos, and
    #                        codex otherwise refuses to start in them.
    #
    # Sandbox policy is deliberately NOT set here. Codex's own default applies, and Synagon
    # never emits --dangerously-bypass-approvals-and-sandbox. A user who wants a different
    # policy passes it through extra_args, which makes the loosening an explicit, auditable
    # choice in their own config rather than a silent default of ours.
    cmd = [executable, "exec", "--json", "--color", "never", "--skip-git-repo-check"]

    if model:
        cmd.extend(["-m", model])
    if working_dir:
        cmd.extend(["-C", working_dir])
    if extra_args:
        cmd.extend(extra_args)
    cmd.append(prompt)

    exec_result = run_agent_cli(
        cmd=cmd,
        cwd=working_dir,
        timeout=timeout,
        visible=visible,
        title=title,
        agent="codex",
        role=role or "implementer",
        model=model,
        terminal_type=terminal_type,
        pause_on_completion=pause_on_completion,
        tracer=tracer,
        close_on_completion=close_on_completion,
    )

    if exec_result.returncode != 0:
        # A failed turn's stream still carries whatever usage was reported before the failure
        # and a `turn.failed` event saying why. Both travel with the exception so a failed
        # attempt still bills its tokens and explains itself.
        _text, failed_usage = parse_codex_output(exec_result.stdout or "")
        message = f"Codex CLI execution failed with code {exec_result.returncode}"
        error = _stream_error(exec_result.stdout or "")
        if error:
            message += f": {error}"
        raise CLIExecutionError(
            message=message,
            returncode=exec_result.returncode,
            stdout=exec_result.stdout,
            stderr=exec_result.stderr,
            command=cmd,
            token_usage=failed_usage if failed_usage.get("available") else None,
        )

    output = exec_result.stdout.strip() if exec_result.stdout else (exec_result.stderr or "").strip()
    return parse_codex_output(output)


def run_codex(
    prompt: str,
    timeout: int = 180,
    working_dir: Optional[str] = None,
    extra_args: Optional[List[str]] = None,
    model: Optional[str] = None,
    visible: bool = False,
    title: Optional[str] = None,
    role: Optional[str] = "implementer",
    terminal_type: str = "auto",
    pause_on_completion: float = 1.5,
    tracer: Optional[object] = None,
    return_usage: bool = False,
    close_on_completion: bool = True,
) -> Any:
    """Execute the Codex CLI and return response text, or (text, usage) when requested.

    Args:
        return_usage: When True, return a (response_text, token_usage) tuple. When False
            (default), return the response text alone, matching the other adapters' shape.
    """
    text, usage = run_codex_with_usage(
        prompt=prompt,
        timeout=timeout,
        working_dir=working_dir,
        extra_args=extra_args,
        model=model,
        visible=visible,
        title=title,
        role=role,
        terminal_type=terminal_type,
        pause_on_completion=pause_on_completion,
        tracer=tracer,
        close_on_completion=close_on_completion,
    )
    if return_usage:
        return text, usage
    return text
