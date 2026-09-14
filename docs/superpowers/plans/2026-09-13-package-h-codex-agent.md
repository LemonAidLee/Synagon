# Package H — Codex/ChatGPT Fourth Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add OpenAI's Codex CLI as a fourth first-class Synagon agent — discovery, auth status, login, configuration, and orchestration — using only the officially supported local CLI.

**Architecture:** A new adapter `orchestrator/agents/codex.py` mirrors the headless branch of the OpenCode adapter and reuses `run_agent_cli` for all process machinery. A new `check_codex_auth` probe in `provider_auth.py` reads `codex login status`, the one official non-interactive auth signal. Every other change is a one-line registry entry.

**Tech Stack:** Python 3.13, pytest, `unittest.mock`, `codex-cli 0.154.0` (`@openai/codex`).

**Spec:** `docs/superpowers/specs/2026-09-13-package-h-codex-agent-design.md`

## Global Constraints

- Agent/provider id is exactly `codex` (lowercase) everywhere.
- Display name is exactly `Codex`.
- Never read, open, stat, or reference `~/.codex/auth.json` as an auth signal.
- Never read, set, forward, or log `OPENAI_API_KEY`; never add it to a child environment.
- Never emit `--dangerously-bypass-approvals-and-sandbox`; never set `--sandbox` by default.
- Every subprocess: `shell=False`, explicit timeout, `stdin=DEVNULL`.
- `codex exec` argv always includes `--json --color never --skip-git-repo-check`.
- Subscription state is always `SUBSCRIPTION_UNAVAILABLE`; when authenticated, `subscription_detail` is exactly `SUBSCRIPTION_CONFIRMED_DETAIL`.
- `codex` is appended last to `RUNNABLE_AGENTS` and `PROVIDERS` — never inserted.
- `codex` is NOT added to `NATIVE_TUI_AGENTS`.
- Run tests with `.venv/Scripts/python.exe -m pytest`.

---

### Task 1: Codex executable resolver

**Files:**
- Create: `orchestrator/agents/codex.py`
- Test: `tests/test_codex.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `get_codex_executable_path() -> str`, raising `FileNotFoundError` when absent.

- [ ] **Step 1: Write the failing tests**

```python
import os
from unittest.mock import patch
import pytest
from orchestrator.agents.codex import get_codex_executable_path


class TestCodexExecutableResolution:
    def test_returns_which_result_when_plain_binary(self):
        with patch("shutil.which", return_value="/usr/local/bin/codex"):
            assert get_codex_executable_path() == "/usr/local/bin/codex"

    def test_prefers_native_exe_over_cmd_shim(self):
        """The .cmd shim routes through cmd.exe and inherits its 8192-char limit,
        which a long orchestration prompt exceeds. Same rationale as OpenCode."""
        shim = r"C:\Users\x\AppData\Roaming\npm\codex.cmd"
        native = os.path.join(
            r"C:\Users\x\AppData\Roaming\npm", "node_modules", "@openai", "codex",
            "node_modules", "@openai", "codex-win32-x64", "vendor",
            "x86_64-pc-windows-msvc", "bin", "codex.exe",
        )
        with patch("shutil.which", return_value=shim), \
             patch("os.path.isfile", side_effect=lambda p: p == native):
            assert get_codex_executable_path() == native

    def test_falls_back_to_cmd_shim_when_native_exe_missing(self):
        shim = r"C:\Users\x\AppData\Roaming\npm\codex.cmd"
        with patch("shutil.which", return_value=shim), \
             patch("os.path.isfile", return_value=False):
            assert get_codex_executable_path() == shim

    def test_raises_when_not_installed(self):
        with patch("shutil.which", return_value=None), \
             patch("os.path.isfile", return_value=False):
            with pytest.raises(FileNotFoundError) as exc:
                get_codex_executable_path()
            assert "codex" in str(exc.value).lower()

    def test_error_names_official_install_command(self):
        """A user with only the un-invokable MSIX desktop app must be told what to install."""
        with patch("shutil.which", return_value=None), \
             patch("os.path.isfile", return_value=False):
            with pytest.raises(FileNotFoundError) as exc:
                get_codex_executable_path()
            assert "@openai/codex" in str(exc.value)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_codex.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'orchestrator.agents.codex'`

- [ ] **Step 3: Write the module with the resolver**

```python
"""Adapter for orchestrating OpenAI's Codex CLI via `codex exec` non-interactive mode.

Every command and output shape here was verified against a live `codex-cli 0.154.0` install;
see docs/superpowers/specs/2026-09-13-package-h-codex-agent-design.md for the measurements.
"""

import json
import os
import shutil
from typing import Any, Dict, List, Optional, Tuple

from orchestrator.agents.exceptions import CLIExecutionError
from orchestrator.launcher import run_agent_cli
from orchestrator.types import TokenUsage, create_token_usage, unavailable_token_usage

#: Relative path from the npm shim's directory to the native binary the shim wraps.
_NPM_NATIVE_RELATIVE = os.path.join(
    "node_modules", "@openai", "codex", "node_modules", "@openai",
    "codex-win32-x64", "vendor", "x86_64-pc-windows-msvc", "bin", "codex.exe",
)

_INSTALL_HINT = (
    "Install the official CLI with 'npm install -g @openai/codex'. Note that the Codex/ChatGPT "
    "desktop app does not provide an invokable 'codex' command: its bundled binary lives under "
    "Program Files\\WindowsApps, which denies execution to other processes, and it publishes no "
    "app execution alias."
)


def get_codex_executable_path() -> str:
    """Resolve the path to the Codex CLI executable.

    Returns:
        Absolute path to the discovered codex executable.

    Raises:
        FileNotFoundError: If codex cannot be found in PATH or standard directories.
    """
    codex_path = shutil.which("codex")
    if codex_path:
        # On Windows, npm creates a .cmd/.ps1 shim which enforces cmd.exe's 8192-char limit.
        # Prefer the underlying native codex.exe, exactly as get_opencode_executable_path does.
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_codex.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add orchestrator/agents/codex.py tests/test_codex.py
git commit -m "Add Codex CLI executable resolver, preferring the native exe over the npm shim"
```

---

### Task 2: `codex exec --json` output parser

**Files:**
- Modify: `orchestrator/agents/codex.py`
- Test: `tests/test_codex.py`

**Interfaces:**
- Consumes: `get_codex_executable_path` from Task 1.
- Produces: `parse_codex_output(stdout: str) -> Tuple[str, TokenUsage]` and `_stream_error(stdout: str) -> Optional[str]`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_codex.py`. The fixtures below are **verbatim captures** from live runs.

```python
from orchestrator.agents.codex import parse_codex_output, _stream_error

LIVE_SUCCESS = (
    '{"type":"thread.started","thread_id":"01a09a75-00aa-7b10-a2bb-95430d8ff412"}\n'
    '{"type":"turn.started"}\n'
    '{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"PONG"}}\n'
    '{"type":"turn.completed","usage":{"input_tokens":17154,"cached_input_tokens":9600,'
    '"cache_write_input_tokens":0,"output_tokens":6,"reasoning_output_tokens":0}}\n'
)

LIVE_FAILURE = (
    '{"type":"turn.started"}\n'
    '{"type":"error","message":"Reconnecting... 1/5 (unexpected status 401 Unauthorized)"}\n'
    '{"type":"error","message":"unexpected status 401 Unauthorized"}\n'
    '{"type":"turn.failed","error":{"message":"The \'no-such-model-xyz\' model is not '
    'supported when using Codex with a ChatGPT account."}}\n'
)


class TestParseCodexOutput:
    def test_extracts_agent_message_text(self):
        text, _ = parse_codex_output(LIVE_SUCCESS)
        assert text == "PONG"

    def test_maps_usage_fields_losslessly(self):
        _, usage = parse_codex_output(LIVE_SUCCESS)
        assert usage["available"] is True
        assert usage["input_tokens"] == 17154
        assert usage["output_tokens"] == 6
        assert usage["cache_read_tokens"] == 9600
        assert usage["cache_write_tokens"] == 0
        assert usage["reasoning_tokens"] == 0
        assert usage["raw_usage"]["cached_input_tokens"] == 9600

    def test_concatenates_multiple_agent_messages(self):
        stream = (
            '{"type":"item.completed","item":{"type":"agent_message","text":"alpha "}}\n'
            '{"type":"item.completed","item":{"type":"agent_message","text":"beta"}}\n'
        )
        text, _ = parse_codex_output(stream)
        assert text == "alpha beta"

    def test_ignores_non_agent_message_items(self):
        """Reasoning/command items must not leak into the response text."""
        stream = (
            '{"type":"item.completed","item":{"type":"reasoning","text":"secret thinking"}}\n'
            '{"type":"item.completed","item":{"type":"agent_message","text":"visible"}}\n'
        )
        text, _ = parse_codex_output(stream)
        assert text == "visible"

    def test_empty_output_is_unavailable_usage(self):
        text, usage = parse_codex_output("")
        assert text == ""
        assert usage["available"] is False

    def test_missing_usage_degrades_to_unavailable_not_zeros(self):
        """Schema drift must never fabricate numbers."""
        stream = '{"type":"item.completed","item":{"type":"agent_message","text":"hi"}}\n'
        _, usage = parse_codex_output(stream)
        assert usage["available"] is False
        assert usage["input_tokens"] is None

    def test_non_json_output_returned_verbatim(self):
        text, usage = parse_codex_output("plain text from a mocked CLI")
        assert text == "plain text from a mocked CLI"
        assert usage["available"] is False

    def test_unknown_event_types_ignored(self):
        stream = (
            '{"type":"some.future.event","payload":{"x":1}}\n'
            '{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}\n'
        )
        text, _ = parse_codex_output(stream)
        assert text == "ok"

    def test_malformed_json_line_skipped(self):
        stream = (
            '{not valid json\n'
            '{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}\n'
        )
        text, _ = parse_codex_output(stream)
        assert text == "ok"


class TestStreamError:
    def test_prefers_turn_failed_over_transient_errors(self):
        """`Reconnecting... 1/5` events are recovered retries, not the cause."""
        assert "no-such-model-xyz" in _stream_error(LIVE_FAILURE)
        assert "Reconnecting" not in _stream_error(LIVE_FAILURE)

    def test_falls_back_to_last_error_when_no_turn_failed(self):
        stream = (
            '{"type":"error","message":"first"}\n'
            '{"type":"error","message":"second"}\n'
        )
        assert _stream_error(stream) == "second"

    def test_returns_none_when_no_error(self):
        assert _stream_error(LIVE_SUCCESS) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_codex.py -k "Parse or StreamError" -v`
Expected: FAIL — `ImportError: cannot import name 'parse_codex_output'`

- [ ] **Step 3: Implement the parser**

Append to `orchestrator/agents/codex.py`:

```python
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
    """Map Codex's `turn.completed.usage` onto Synagon's TokenUsage.

    `total_tokens` is deliberately left to create_token_usage's input+output derivation,
    matching every other adapter, since Codex reports no total of its own.
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
    concatenated. Reasoning and command items are deliberately excluded. Usage comes from
    `turn.completed.usage`; when absent, usage is *unavailable* rather than zeroed, so a
    future schema change degrades honestly instead of fabricating numbers.

    Falls back to returning the raw text when the stream contains no JSON at all (a mocked
    CLI or a plain-text mode), matching parse_opencode_output's contract.
    """
    if not stdout or not stdout.strip():
        return "", unavailable_token_usage()

    raw = stdout.strip()
    chunks: List[str] = []
    usage: Optional[TokenUsage] = None
    saw_json = False

    for event in _iter_events(raw):
        saw_json = True
        etype = event.get("type")
        if etype == "item.completed":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message":
                text = item.get("text")
                if isinstance(text, str):
                    chunks.append(text)
        elif etype == "turn.completed":
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

    `turn.failed.error.message` wins: measured runs emit `error` events for *transient,
    recovered* retries ("Reconnecting... 1/5"), so the last `error` event is not a reliable
    cause. Only when no `turn.failed` is present does the last `error` event stand in.
    """
    terminal = None
    last_error = None
    for event in _iter_events(stdout):
        etype = event.get("type")
        if etype == "turn.failed":
            error = event.get("error")
            if isinstance(error, dict) and error.get("message"):
                terminal = str(error["message"])
            elif error:
                terminal = str(error)
        elif etype == "error" and event.get("message"):
            last_error = str(event["message"])
    message = terminal or last_error
    return message[:300] if message else None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_codex.py -v`
Expected: 17 passed

- [ ] **Step 5: Commit**

```bash
git add orchestrator/agents/codex.py tests/test_codex.py
git commit -m "Parse codex exec --json events into response text and TokenUsage"
```

---

### Task 3: `run_codex` orchestration entry point

**Files:**
- Modify: `orchestrator/agents/codex.py`
- Modify: `orchestrator/agents/__init__.py`
- Test: `tests/test_codex.py`

**Interfaces:**
- Consumes: `parse_codex_output`, `_stream_error`, `get_codex_executable_path`.
- Produces: `run_codex_with_usage(...) -> Tuple[str, TokenUsage]` and `run_codex(..., return_usage: bool = False) -> Any`, signature-compatible with `run_opencode`.

- [ ] **Step 1: Write the failing tests**

```python
from unittest.mock import MagicMock
from orchestrator.agents.codex import run_codex, run_codex_with_usage
from orchestrator.agents.exceptions import CLIExecutionError


def _ok_result(stdout):
    r = MagicMock()
    r.returncode = 0
    r.stdout = stdout
    r.stderr = ""
    return r


class TestRunCodex:
    def test_builds_required_argv(self):
        with patch("orchestrator.agents.codex.get_codex_executable_path", return_value="codex"), \
             patch("orchestrator.agents.codex.run_agent_cli", return_value=_ok_result(LIVE_SUCCESS)) as run:
            run_codex("do a thing")
        cmd = run.call_args.kwargs["cmd"]
        assert cmd[0] == "codex" and cmd[1] == "exec"
        assert "--json" in cmd
        assert cmd[cmd.index("--color") + 1] == "never"
        assert "--skip-git-repo-check" in cmd
        assert cmd[-1] == "do a thing", "prompt must be the final positional argument"

    def test_never_bypasses_sandbox_or_approvals(self):
        with patch("orchestrator.agents.codex.get_codex_executable_path", return_value="codex"), \
             patch("orchestrator.agents.codex.run_agent_cli", return_value=_ok_result(LIVE_SUCCESS)) as run:
            run_codex("x")
        cmd = run.call_args.kwargs["cmd"]
        assert "--dangerously-bypass-approvals-and-sandbox" not in cmd
        assert "--dangerously-bypass-hook-trust" not in cmd
        assert "--sandbox" not in cmd and "-s" not in cmd

    def test_passes_model_and_cwd(self):
        with patch("orchestrator.agents.codex.get_codex_executable_path", return_value="codex"), \
             patch("orchestrator.agents.codex.run_agent_cli", return_value=_ok_result(LIVE_SUCCESS)) as run:
            run_codex("x", model="gpt-5.1-codex", working_dir="/tmp/wt")
        cmd = run.call_args.kwargs["cmd"]
        assert cmd[cmd.index("-m") + 1] == "gpt-5.1-codex"
        assert cmd[cmd.index("-C") + 1] == "/tmp/wt"

    def test_returns_text_by_default(self):
        with patch("orchestrator.agents.codex.get_codex_executable_path", return_value="codex"), \
             patch("orchestrator.agents.codex.run_agent_cli", return_value=_ok_result(LIVE_SUCCESS)):
            assert run_codex("x") == "PONG"

    def test_return_usage_returns_tuple(self):
        with patch("orchestrator.agents.codex.get_codex_executable_path", return_value="codex"), \
             patch("orchestrator.agents.codex.run_agent_cli", return_value=_ok_result(LIVE_SUCCESS)):
            text, usage = run_codex("x", return_usage=True)
        assert text == "PONG" and usage["output_tokens"] == 6

    def test_forwards_launcher_kwargs(self):
        """Timeout, ownership, visibility and tracing all belong to run_agent_cli."""
        with patch("orchestrator.agents.codex.get_codex_executable_path", return_value="codex"), \
             patch("orchestrator.agents.codex.run_agent_cli", return_value=_ok_result(LIVE_SUCCESS)) as run:
            run_codex("x", timeout=99, visible=True, role="verifier", terminal_type="console")
        kw = run.call_args.kwargs
        assert kw["timeout"] == 99 and kw["visible"] is True
        assert kw["agent"] == "codex" and kw["role"] == "verifier"
        assert kw["terminal_type"] == "console"

    def test_failure_raises_with_terminal_cause(self):
        bad = MagicMock(returncode=1, stdout=LIVE_FAILURE, stderr="")
        with patch("orchestrator.agents.codex.get_codex_executable_path", return_value="codex"), \
             patch("orchestrator.agents.codex.run_agent_cli", return_value=bad):
            with pytest.raises(CLIExecutionError) as exc:
                run_codex("x")
        assert "no-such-model-xyz" in str(exc.value)
        assert "Reconnecting" not in str(exc.value)

    def test_failure_carries_partial_usage_when_available(self):
        stream = (
            '{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":2,'
            '"cached_input_tokens":0,"cache_write_input_tokens":0,"reasoning_output_tokens":0}}\n'
            '{"type":"turn.failed","error":{"message":"boom"}}\n'
        )
        bad = MagicMock(returncode=1, stdout=stream, stderr="")
        with patch("orchestrator.agents.codex.get_codex_executable_path", return_value="codex"), \
             patch("orchestrator.agents.codex.run_agent_cli", return_value=bad):
            with pytest.raises(CLIExecutionError) as exc:
                run_codex("x")
        assert exc.value.token_usage["input_tokens"] == 10

    def test_extra_args_appended_before_prompt(self):
        with patch("orchestrator.agents.codex.get_codex_executable_path", return_value="codex"), \
             patch("orchestrator.agents.codex.run_agent_cli", return_value=_ok_result(LIVE_SUCCESS)) as run:
            run_codex("x", extra_args=["--sandbox", "workspace-write"])
        cmd = run.call_args.kwargs["cmd"]
        assert cmd.index("--sandbox") < cmd.index("x")


class TestCodexExports:
    def test_run_codex_exported_from_agents_package(self):
        from orchestrator.agents import run_codex as exported
        assert callable(exported)

    def test_codex_is_not_a_native_tui_agent(self):
        from orchestrator.agents import NATIVE_TUI_AGENTS, supports_native_tui
        assert "codex" not in NATIVE_TUI_AGENTS
        assert supports_native_tui("codex") is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_codex.py -k "RunCodex or Exports" -v`
Expected: FAIL — `ImportError: cannot import name 'run_codex'`

- [ ] **Step 3: Implement `run_codex`**

Append to `orchestrator/agents/codex.py`:

```python
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

    Codex has no supported client/server native-TUI integration (no `serve`+`attach`
    equivalent), so this is always a headless `codex exec` run, optionally hosted in a
    visible terminal. All process machinery - timeout enforcement, process-tree ownership
    and kill, terminal hosting, tracing - is delegated to `run_agent_cli` unchanged.

    Raises:
        CLITimeoutError: If execution exceeds `timeout` (raised by run_agent_cli).
        CLIExecutionError: If codex exits non-zero.
        FileNotFoundError: If the codex executable cannot be found.
    """
    executable = get_codex_executable_path()

    # --json          parsed JSONL event stream (measured contract).
    # --color never   no ANSI into a parsed stream; the ed45916 lesson, applied up front.
    # --skip-git-repo-check  worktrees and scratch dirs are not always git repos, and codex
    #                 otherwise refuses to start in them.
    # Sandbox policy is deliberately NOT set: codex's own default applies, and a user who
    # wants another policy passes it through extra_args as an explicit, auditable choice.
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
        # A failed turn's stream still carries whatever usage was reported before the
        # failure, and a `turn.failed` saying why. Both travel with the exception.
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
    """Execute the Codex CLI and return response text, or (text, usage) when requested."""
    text, usage = run_codex_with_usage(
        prompt=prompt, timeout=timeout, working_dir=working_dir, extra_args=extra_args,
        model=model, visible=visible, title=title, role=role, terminal_type=terminal_type,
        pause_on_completion=pause_on_completion, tracer=tracer,
        close_on_completion=close_on_completion,
    )
    if return_usage:
        return text, usage
    return text
```

- [ ] **Step 4: Export it from the agents package**

In `orchestrator/agents/__init__.py`, update the docstring and add the import and export:

```python
"""CLI Agent Adapters for Antigravity, Claude Code, OpenCode, and Codex."""

from orchestrator.agents.antigravity import run_antigravity, run_antigravity_raw
from orchestrator.agents.claude_code import run_claude_code
from orchestrator.agents.codex import run_codex
```

and add `"run_codex",` to `__all__`. Leave `NATIVE_TUI_AGENTS` untouched — Codex has no
supported native-TUI integration, so `agent_execution_mode: native_tui` must keep refusing it.

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_codex.py -v`
Expected: 28 passed

- [ ] **Step 6: Commit**

```bash
git add orchestrator/agents/codex.py orchestrator/agents/__init__.py tests/test_codex.py
git commit -m "Add run_codex, a headless codex exec adapter reusing run_agent_cli"
```

---

### Task 4: Auth status probe (`codex login status`)

**Files:**
- Modify: `orchestrator/provider_auth.py`
- Test: `tests/test_provider_auth.py`

**Interfaces:**
- Consumes: `get_codex_executable_path` from Task 1.
- Produces: `check_codex_auth(timeout: int = DEFAULT_TIMEOUT) -> ProviderAuthStatus`; `PROVIDERS` gains a trailing `"codex"`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_provider_auth.py`:

```python
from orchestrator.provider_auth import (
    AUTH_AUTHENTICATED, AUTH_CLI_ERROR, AUTH_NOT_AUTHENTICATED, AUTH_NOT_INSTALLED,
    AUTH_TIMED_OUT, PROVIDERS, SUBSCRIPTION_CONFIRMED_DETAIL, SUBSCRIPTION_UNAVAILABLE,
    check_codex_auth,
)


def _completed(returncode, stdout=b"", stderr=b""):
    c = MagicMock()
    c.returncode = returncode
    c.stdout = stdout
    c.stderr = stderr
    return c


class TestCheckCodexAuth:
    def test_signed_in_is_authenticated(self):
        """Measured: exit 0, stdout EMPTY, stderr 'Logged in using ChatGPT'."""
        with patch("orchestrator.provider_auth.get_codex_executable_path", return_value="codex"), \
             patch("orchestrator.provider_auth._run",
                   return_value=_completed(0, b"", b"Logged in using ChatGPT\n")):
            status = check_codex_auth()
        assert status["auth_state"] == AUTH_AUTHENTICATED
        assert status["installed"] is True

    def test_signed_out_is_not_authenticated_despite_exit_1(self):
        """Measured: signed-out is exit 1. That is an answer, not a CLI error."""
        with patch("orchestrator.provider_auth.get_codex_executable_path", return_value="codex"), \
             patch("orchestrator.provider_auth._run",
                   return_value=_completed(1, b"", b"Not logged in\n")):
            status = check_codex_auth()
        assert status["auth_state"] == AUTH_NOT_AUTHENTICATED

    def test_tolerates_leading_warning_lines(self):
        """Measured with a non-default CODEX_HOME: a WARNING precedes the signal line."""
        noisy = (b"WARNING: proceeding, even though we could not create PATH aliases: ...\n"
                 b"Not logged in\n")
        with patch("orchestrator.provider_auth.get_codex_executable_path", return_value="codex"), \
             patch("orchestrator.provider_auth._run", return_value=_completed(1, b"", noisy)):
            status = check_codex_auth()
        assert status["auth_state"] == AUTH_NOT_AUTHENTICATED

    def test_not_logged_in_checked_before_logged_in(self):
        """'Not logged in' contains 'logged in'; the negative must win."""
        with patch("orchestrator.provider_auth.get_codex_executable_path", return_value="codex"), \
             patch("orchestrator.provider_auth._run",
                   return_value=_completed(1, b"", b"Not logged in\n")):
            assert check_codex_auth()["auth_state"] == AUTH_NOT_AUTHENTICATED

    def test_never_claims_subscription(self):
        with patch("orchestrator.provider_auth.get_codex_executable_path", return_value="codex"), \
             patch("orchestrator.provider_auth._run",
                   return_value=_completed(0, b"", b"Logged in using ChatGPT\n")):
            status = check_codex_auth()
        assert status["subscription_state"] == SUBSCRIPTION_UNAVAILABLE
        assert status["subscription_detail"] == SUBSCRIPTION_CONFIRMED_DETAIL

    def test_reports_auth_mode_without_claiming_a_plan(self):
        with patch("orchestrator.provider_auth.get_codex_executable_path", return_value="codex"), \
             patch("orchestrator.provider_auth._run",
                   return_value=_completed(0, b"", b"Logged in using ChatGPT\n")):
            detail = check_codex_auth()["detail"].lower()
        assert "chatgpt" in detail
        for claim in ("plus", "pro", "subscription is active", "entitled"):
            assert claim not in detail

    def test_unrecognised_output_is_cli_error_not_a_guess(self):
        with patch("orchestrator.provider_auth.get_codex_executable_path", return_value="codex"), \
             patch("orchestrator.provider_auth._run",
                   return_value=_completed(0, b"", b"something entirely new\n")):
            assert check_codex_auth()["auth_state"] == AUTH_CLI_ERROR

    def test_not_installed(self):
        with patch("orchestrator.provider_auth.get_codex_executable_path",
                   side_effect=FileNotFoundError("no codex")):
            status = check_codex_auth()
        assert status["auth_state"] == AUTH_NOT_INSTALLED
        assert status["installed"] is False

    def test_timeout(self):
        from orchestrator.provider_auth import _ProbeTimeout
        with patch("orchestrator.provider_auth.get_codex_executable_path", return_value="codex"), \
             patch("orchestrator.provider_auth._run", side_effect=_ProbeTimeout("slow")):
            assert check_codex_auth()["auth_state"] == AUTH_TIMED_OUT

    def test_detail_is_redacted(self):
        leaky = b"Logged in using ChatGPT sk-abcdefghijklmnop\n"
        with patch("orchestrator.provider_auth.get_codex_executable_path", return_value="codex"), \
             patch("orchestrator.provider_auth._run", return_value=_completed(0, b"", leaky)):
            assert "sk-abcdefghijklmnop" not in check_codex_auth()["detail"]

    def test_probe_never_reads_auth_json(self):
        """The credential file must never be an auth signal."""
        import orchestrator.provider_auth as pa
        source = open(pa.__file__, encoding="utf-8").read()
        assert "auth.json" not in source
        assert "OPENAI_API_KEY" not in source


class TestProvidersRegistry:
    def test_codex_appended_last(self):
        assert PROVIDERS == ("claude", "opencode", "antigravity", "codex")

    def test_check_all_includes_codex(self):
        from orchestrator.provider_auth import check_all_providers
        with patch("orchestrator.provider_auth.get_claude_executable_path", side_effect=FileNotFoundError()), \
             patch("orchestrator.provider_auth.get_opencode_executable_path", side_effect=FileNotFoundError()), \
             patch("orchestrator.provider_auth.get_antigravity_executable_path", side_effect=FileNotFoundError()), \
             patch("orchestrator.provider_auth.get_codex_executable_path", side_effect=FileNotFoundError()):
            results = check_all_providers()
        assert [r["provider"] for r in results] == ["claude", "opencode", "antigravity", "codex"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_provider_auth.py -k "Codex or Registry" -v`
Expected: FAIL — `ImportError: cannot import name 'check_codex_auth'`

- [ ] **Step 3: Implement the probe**

In `orchestrator/provider_auth.py`, add the import beside the other three resolvers:

```python
from orchestrator.agents.codex import get_codex_executable_path
```

Update the three registries:

```python
PROVIDERS = ("claude", "opencode", "antigravity", "codex")

_VERSION_ARGS = {
    "claude": ["--version"],
    "opencode": ["--version"],
    "antigravity": ["--version"],
    "codex": ["--version"],
}

_LOGIN_ARGS: Dict[str, List[str]] = {
    "claude": [],
    "opencode": ["auth", "login"],
    "antigravity": [],
    "codex": ["login"],
}
```

Add a branch to `_resolve_executable`, keeping the deliberate bare-name-call style so tests can
patch it:

```python
    if provider == "codex":
        return get_codex_executable_path()
```

Add the probe and register it:

```python
#: Measured against codex-cli 0.154.0. `codex login status` prints its answer to *stderr*
#: (stdout is empty) and exits 1 when signed out - so exit code alone is not the signal, and a
#: non-zero exit here is a legitimate answer rather than a CLI error. Order matters: "not logged
#: in" contains "logged in", so the negative is tested first.
_CODEX_SIGNED_OUT_MARKERS = ("not logged in", "not signed in")
_CODEX_SIGNED_IN_MARKERS = ("logged in", "signed in")


def check_codex_auth(timeout: int = DEFAULT_TIMEOUT) -> ProviderAuthStatus:
    """Codex: `codex login status` is an officially documented non-interactive status command.

    With `opencode`, this is the second of the four providers with a real auth signal. It
    reports the auth *mode* it is given ("Logged in using ChatGPT"), which says how the user
    authenticated - never that a paid plan is active. Subscription stays `unavailable`: no
    non-interactive Codex command reports plan, quota, or entitlement.

    `~/.codex/auth.json` is never opened. Its mere existence is not an auth signal, exactly as
    this module refuses for every other provider.
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

    # Measured: the answer arrives on stderr, and stdout is empty. Both are read so a future
    # version that moves the line to stdout keeps working.
    text = _ANSI_ESCAPE_RE.sub("", _decode(completed.stdout) + _decode(completed.stderr)).strip()
    lowered = text.lower()

    if any(marker in lowered for marker in _CODEX_SIGNED_OUT_MARKERS):
        return _status(
            "codex", True, executable, AUTH_NOT_AUTHENTICATED,
            "Codex is installed but no account is signed in. Click Login to sign in with your "
            "own ChatGPT account.", started,
        )

    if any(marker in lowered for marker in _CODEX_SIGNED_IN_MARKERS):
        signal = next(
            (line.strip() for line in text.splitlines()
             if any(m in line.lower() for m in _CODEX_SIGNED_IN_MARKERS)),
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
```

and extend `_CHECKERS`:

```python
_CHECKERS = {
    "claude": check_claude_auth,
    "opencode": check_opencode_auth,
    "antigravity": check_antigravity_auth,
    "codex": check_codex_auth,
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_provider_auth.py -v`
Expected: all pass, including the pre-existing Package G tests

- [ ] **Step 5: Commit**

```bash
git add orchestrator/provider_auth.py tests/test_provider_auth.py
git commit -m "Add check_codex_auth using the official 'codex login status' signal"
```

---

### Task 5: Login flow and CLI report

**Files:**
- Modify: `orchestrator/provider_auth.py` (already covered by Task 4's `_LOGIN_ARGS`)
- Test: `tests/test_provider_auth_cli.py`

**Interfaces:**
- Consumes: `open_provider_login`, `format_provider_report` (both existing, unchanged).
- Produces: no new symbols — this task proves Codex rides the existing infrastructure.

- [ ] **Step 1: Write the failing tests**

```python
from orchestrator.provider_auth import format_provider_report, open_provider_login


class TestCodexLogin:
    def test_spawns_detached_codex_login(self):
        with patch("orchestrator.provider_auth.get_codex_executable_path", return_value="codex"), \
             patch("orchestrator.provider_auth._spawn_detached_terminal") as spawn:
            result = open_provider_login("codex")
        assert result["ok"] is True
        cmd = spawn.call_args[0][0]
        assert cmd == ["codex", "login"]

    def test_login_never_passes_a_credential_flag(self):
        """--with-api-key / --with-access-token read secrets from stdin. Never ours to send."""
        with patch("orchestrator.provider_auth.get_codex_executable_path", return_value="codex"), \
             patch("orchestrator.provider_auth._spawn_detached_terminal") as spawn:
            open_provider_login("codex")
        cmd = spawn.call_args[0][0]
        assert "--with-api-key" not in cmd
        assert "--with-access-token" not in cmd

    def test_login_when_not_installed_reports_cleanly(self):
        with patch("orchestrator.provider_auth.get_codex_executable_path",
                   side_effect=FileNotFoundError("nope")):
            result = open_provider_login("codex")
        assert result["ok"] is False and "not installed" in result["error"]

    def test_unknown_provider_still_refused(self):
        assert open_provider_login("gpt4all")["ok"] is False


class TestCodexInReport:
    def test_report_renders_codex_row(self):
        rows = [{
            "provider": "codex", "installed": True, "executable": "codex",
            "auth_state": "authenticated", "detail": "Logged in using ChatGPT",
            "subscription_state": "unavailable",
            "subscription_detail": "Authentication verified; subscription status cannot be confirmed.",
        }]
        out = format_provider_report(rows, color=False)
        assert "codex" in out and "[OK]" in out
        assert "subscription status cannot be confirmed" in out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_provider_auth_cli.py -k Codex -v`
Expected: FAIL — `open_provider_login("codex")` returns `unknown provider 'codex'` (if Task 4 is not yet applied) or passes immediately (if it is).

- [ ] **Step 3: Confirm no implementation change is needed**

Task 4 added `"codex"` to `PROVIDERS` and `_LOGIN_ARGS`, and `_resolve_executable` handles it.
`open_provider_login` and `format_provider_report` are provider-agnostic. If any test above
fails, fix `provider_auth.py` rather than the test.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_provider_auth_cli.py -v`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add tests/test_provider_auth_cli.py
git commit -m "Cover Codex login and report rendering on the existing provider infrastructure"
```

---

### Task 6: Registry, config, preflight, and launcher wiring

**Files:**
- Modify: `orchestrator/config.py:433` and `DEFAULT_CONFIG["models"]`
- Modify: `orchestrator/graph.py:485`
- Modify: `orchestrator/preflight.py:47,55`
- Modify: `orchestrator/launcher.py:210`
- Modify: `orchestrator/web/settings.html:126-128`
- Test: `tests/test_codex.py`, `tests/test_provider_settings_page.py`

**Daemon note:** `daemon.py`'s `/api/providers` and `provider_login` call `check_all_providers()`
and `open_provider_login()` directly and iterate `PROVIDERS`, so they pick Codex up with **no
change**. Only the settings page's display-name map is provider-specific.

**Interfaces:**
- Consumes: `run_codex` (Task 3), `get_codex_executable_path` (Task 1).
- Produces: `RUNNABLE_AGENTS == ("antigravity", "claude", "opencode", "codex")`.

- [ ] **Step 1: Write the failing tests**

```python
class TestCodexRegistry:
    def test_runnable_agents_includes_codex_last(self):
        from orchestrator.config import RUNNABLE_AGENTS
        assert RUNNABLE_AGENTS == ("antigravity", "claude", "opencode", "codex")

    def test_get_runner_returns_run_codex(self):
        from orchestrator.graph import get_runner
        import orchestrator.graph as mod
        assert get_runner("codex") is mod.run_codex

    def test_unknown_agent_still_refused(self):
        from orchestrator.graph import get_runner, UnknownAgentError
        with pytest.raises(UnknownAgentError):
            get_runner("codexx")

    def test_preflight_resolver_registered(self):
        from orchestrator.preflight import AGENT_EXECUTABLE_RESOLVERS, AGENT_VERSION_ARGS
        assert "codex" in AGENT_EXECUTABLE_RESOLVERS
        assert AGENT_VERSION_ARGS["codex"] == ["--version"]

    def test_default_catalog_has_codex_models(self):
        from orchestrator.config import DEFAULT_CONFIG
        ids = [m["id"] for m in DEFAULT_CONFIG["models"]["codex"]]
        assert "gpt-5.1-codex" in ids

    def test_window_title_uses_proper_case(self):
        from orchestrator.launcher import build_window_title
        assert "Codex" in build_window_title("codex", "implementer")

    def test_team_editor_accepts_codex(self):
        from orchestrator.config import RUNNABLE_AGENTS
        assert "codex" in RUNNABLE_AGENTS

    def test_settings_page_labels_codex(self):
        """The settings card map is the one provider-specific bit of UI."""
        html = open("orchestrator/web/settings.html", encoding="utf-8").read()
        assert "codex:" in html and "Codex" in html
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_codex.py -k Registry -v`
Expected: FAIL — `RUNNABLE_AGENTS` is the 3-tuple

- [ ] **Step 3: Apply the wiring**

`orchestrator/config.py` — extend the tuple and the catalog:

```python
RUNNABLE_AGENTS = ("antigravity", "claude", "opencode", "codex")
```

```python
        "codex": [
            {"id": "gpt-5.1-codex", "name": "GPT-5.1 Codex (Codex CLI default)"},
            {"id": "gpt-5.1-codex-mini", "name": "GPT-5.1 Codex Mini"},
        ],
```

`orchestrator/graph.py` — add the import beside the other runners and one dispatch line in
`get_runner`, before the `UnknownAgentError` raise:

```python
    if agent_name == "codex": return mod.run_codex
```

`orchestrator/preflight.py`:

```python
from orchestrator.agents.codex import get_codex_executable_path
```
```python
    "codex": get_codex_executable_path,
```
```python
    "codex": ["--version"],
```

`orchestrator/launcher.py` — add the display-name branch:

```python
    elif agent_clean == "codex":
        agent_display = "Codex"
```

`orchestrator/web/settings.html` — add the card label beside the other three:

```javascript
        codex: 'Codex (ChatGPT)',
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_codex.py -v`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add orchestrator/config.py orchestrator/graph.py orchestrator/preflight.py orchestrator/launcher.py tests/test_codex.py
git commit -m "Register codex in the agent registry, catalog, preflight, and launcher"
```

---

### Task 7: Backward-compatibility and failure-handling guarantees

**Files:**
- Test: `tests/test_codex.py`

**Interfaces:**
- Consumes: everything from Tasks 1-6.
- Produces: no new symbols.

- [ ] **Step 1: Write the tests**

```python
class TestBackwardCompatibility:
    def test_existing_three_agents_unchanged(self):
        from orchestrator.config import RUNNABLE_AGENTS
        for agent in ("antigravity", "claude", "opencode"):
            assert agent in RUNNABLE_AGENTS

    def test_existing_runners_unchanged(self):
        from orchestrator.graph import get_runner
        import orchestrator.graph as mod
        assert get_runner("claude") is mod.run_claude_code
        assert get_runner("opencode") is mod.run_opencode
        assert get_runner("antigravity") is mod.run_antigravity

    def test_config_without_codex_still_validates(self):
        """The shipped orchestrator.yaml never mentions codex; it must be unaffected."""
        from orchestrator.config import load_config
        config = load_config("orchestrator.yaml")
        assert config["agents"]

    def test_existing_catalogs_untouched(self):
        from orchestrator.config import DEFAULT_CONFIG
        assert [m["id"] for m in DEFAULT_CONFIG["models"]["claude"]] == ["sonnet", "opus", "haiku"]

    def test_missing_codex_does_not_break_other_providers(self):
        """A machine without codex must see no new failures."""
        from orchestrator.provider_auth import check_all_providers, providers_ok
        with patch("orchestrator.provider_auth.get_codex_executable_path",
                   side_effect=FileNotFoundError("not installed")), \
             patch("orchestrator.provider_auth.get_claude_executable_path", return_value="claude"), \
             patch("orchestrator.provider_auth.get_opencode_executable_path", side_effect=FileNotFoundError()), \
             patch("orchestrator.provider_auth.get_antigravity_executable_path", side_effect=FileNotFoundError()), \
             patch("orchestrator.provider_auth._run", return_value=_completed(0, b"1.0.0", b"")):
            results = check_all_providers()
        codex = [r for r in results if r["provider"] == "codex"][0]
        assert codex["auth_state"] == "not_installed"
        assert providers_ok(results) is True, "not_installed is a fact, not a probe failure"

    def test_native_tui_mode_refuses_codex(self):
        from orchestrator.agents import supports_native_tui
        assert supports_native_tui("codex") is False
```

- [ ] **Step 2: Run them**

Run: `.venv/Scripts/python.exe -m pytest tests/test_codex.py -k Backward -v`
Expected: all pass. If `test_config_without_codex_still_validates` fails, the catalog change
broke validation — fix `config.py`, not the test.

- [ ] **Step 3: Run the FULL offline suite**

Run: `.venv/Scripts/python.exe -m pytest tests/ -q`
Expected: no regressions against the pre-Package-H baseline.

- [ ] **Step 4: Commit**

```bash
git add tests/test_codex.py
git commit -m "Guarantee Codex is additive: existing agents, configs, and catalogs unchanged"
```

---

### Task 8: Documentation

**Files:**
- Modify: `README.md`, `ARCHITECTURE.md`, `CHANGELOG.md`
- Modify: `docs/superpowers/specs/2026-09-13-package-h-codex-agent-design.md` (status line only)

- [ ] **Step 1: README** — add `codex` to the provider/agent list and the account-linking
  section, with the install line `npm install -g @openai/codex`, and state plainly that Codex
  reports authentication but never subscription.

- [ ] **Step 2: ARCHITECTURE.md** — add `codex` to the agent-registry and provider-auth
  sections; record that it is headless-only (no native TUI) and that its auth signal is
  `codex login status`.

- [ ] **Step 3: CHANGELOG.md** — add a Package H entry naming the new agent, the verified CLI
  version, the auth signal, and the explicit non-goals (no API keys, no subscription claim).

- [ ] **Step 4: Mark the spec implemented** — change its status line to
  `Status: implemented 2026-09-13`.

- [ ] **Step 5: Commit**

```bash
git add README.md ARCHITECTURE.md CHANGELOG.md docs/superpowers/specs/2026-09-13-package-h-codex-agent-design.md
git commit -m "Document the Codex agent in README, ARCHITECTURE, CHANGELOG, and the design spec"
```

---

## Live validation (after Task 8)

Not a unit test — run by hand against the real CLI, with results recorded in the final report.

- [ ] `codex --version` → `codex-cli 0.154.0`
- [ ] `.venv/Scripts/python.exe -c "from orchestrator.agents.codex import get_codex_executable_path as g; print(g())"` → the native `codex.exe`, not the `.ps1`/`.cmd` shim
- [ ] `.venv/Scripts/python.exe -m orchestrator --check-providers` → four rows; codex `[OK]`
- [ ] A real `run_codex` round trip in a scratch dir → returns text and non-zero token counts
- [ ] A real signed-out probe via an isolated `CODEX_HOME` → `not_authenticated`, **without**
      logging the user out
