"""Native TUI execution controller for OpenCode via official client/server architecture.

Decouples the presentation surface (interactive OpenCode TUI, in the Antigravity IDE's
integrated terminal or - when a daemon is watching (Roadmap Phase 10) - relayed through a
pseudo-terminal into the cockpit's own panel instead) from the orchestration control plane
(loopback HTTP server + REST API), which is identical either way.

Everything the orchestrator *decides* comes from OpenCode's documented server API - session
status, the session's messages, their step-finish token counts. The TUI is only ever shown to a
person; nothing here reads its screen, sends it keystrokes, or scrapes its output.
"""

import shutil
import socket
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

from orchestrator.agents.exceptions import (
    CLIExecutionError,
    CLITimeoutError,
    NativeTUIUnavailableError,
)
from orchestrator.agents.opencode_usage import step_tokens_from_messages, usage_from_step_tokens
from orchestrator.launcher import (
    check_antigravity_bridge,
    close_antigravity_integrated_terminal,
    get_antigravity_bridge_url,
    launch_antigravity_integrated_terminal,
)
from orchestrator.native_sessions import register_session, stop_process_tree
from orchestrator.types import TokenUsage, unavailable_token_usage

#: Session states in which OpenCode is still working on the turn. `retry` is OpenCode backing
#: off between provider attempts - the turn is not over, and treating it as idle ended turns early.
ACTIVE_STATUSES = ("busy", "retry")

#: Seconds between status polls.
POLL_INTERVAL_SECONDS = 0.5

#: Consecutive non-active polls, after the session was seen working, that end a turn whose last
#: message never reported a final finish reason. Guards against waiting out the whole timeout.
IDLE_CONFIRM_POLLS = 6

#: Seconds after `prompt_async` within which the session must show *some* sign of work (busy,
#: retry, or an assistant message). Measured on 1.18.29: a prompt naming an unusable model is
#: accepted with 204, the session never turns busy, and no assistant message is ever created -
#: the error exists only on the SSE event stream - so without this bound the controller waited
#: out the entire timeout, once per retry. A real turn shows work within a few seconds.
NO_START_GRACE_SECONDS = 30.0


def find_free_port() -> int:
    """Find an available ephemeral port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def parse_model_spec(model_name: Optional[str]) -> Tuple[str, str]:
    """Parse a model identifier into (providerID, modelID)."""
    if not model_name:
        return "opencode", "big-pickle"
    if "/" in model_name:
        parts = model_name.split("/", 1)
        return parts[0], parts[1]
    return "opencode", model_name


def _resolve_opencode_binary() -> str:
    """The real opencode executable rather than npm's ``opencode.CMD`` shim.

    Launching the server through the shim puts ``cmd.exe`` between this process and the server,
    and terminating the shim left the server running - measured: after ``terminate()`` on the
    shim, the server kept answering its health check. The headless path already preferred the
    native binary for the same reason.
    """
    try:
        from orchestrator.agents.opencode import get_opencode_executable_path

        return get_opencode_executable_path()
    except Exception:
        return shutil.which("opencode") or "opencode"


def _powershell_invocation(argv: List[str]) -> str:
    """One PowerShell command line running `argv`, with every argument quoted.

    The bridge types this into the IDE terminal's shell (PowerShell on Windows, as the
    launcher's own bridge path already assumes), so a path with a space must not split.
    """
    quoted = " ".join("'" + str(arg).replace("'", "''") + "'" for arg in argv)
    return f"& {quoted}"


def _latest_assistant(messages: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(messages, list):
        return None
    for message in reversed(messages):
        info = message.get("info") if isinstance(message, dict) else None
        if isinstance(info, dict) and info.get("role") == "assistant":
            return message
    return None


def _turn_complete(messages: Any) -> bool:
    """True when the session's latest assistant message is a finished final step.

    A step that ended in ``tool-calls`` is followed by another step, so it is not the end of the
    turn - unless it carries an error, which ends the turn where it stands.
    """
    last = _latest_assistant(messages)
    if last is None:
        return False
    info = last.get("info") or {}
    if not (info.get("time") or {}).get("completed"):
        return False
    return info.get("finish") != "tool-calls" or bool(info.get("error"))


def _turn_error(messages: Any) -> Optional[str]:
    """The error OpenCode recorded on the turn's last assistant message, if any."""
    last = _latest_assistant(messages)
    error = ((last or {}).get("info") or {}).get("error")
    if not error:
        return None
    if isinstance(error, dict):
        data = error.get("data") if isinstance(error.get("data"), dict) else {}
        detail = data.get("message") or ""
        name = error.get("name") or "error"
        return f"{name}: {detail}".rstrip(": ")
    return str(error)


def _collect_output(messages: Any) -> str:
    """The turn's assistant text, with one line per tool call. Empty when there was none."""
    text_parts: List[str] = []
    for message in messages if isinstance(messages, list) else []:
        if not isinstance(message, dict) or (message.get("info") or {}).get("role") != "assistant":
            continue
        for part in message.get("parts") or []:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text" and "text" in part:
                text_parts.append(part["text"])
            elif part.get("type") == "tool":
                tool_state = part.get("state") or {}
                tool_title = tool_state.get("title") or tool_state.get("output") or part.get("tool", "tool")
                text_parts.append(f"Tool executed: {tool_title}")
    return "\n".join(text_parts).strip()


def _get_messages(base: str, session_id: str) -> Any:
    resp = requests.get(f"{base}/session/{session_id}/message", timeout=5)
    return resp.json() if resp.status_code == 200 else None


def _collect_usage(base: str, session_id: str, messages: Any) -> TokenUsage:
    """Token usage for the session, from the same step-finish parts headless mode reads.

    Falls back to the session's own aggregate (the same provider numbers, summed by OpenCode)
    only when no step-finish part is present. Never raises: unknown usage stays unavailable.
    """
    try:
        steps = step_tokens_from_messages(messages)
        if steps:
            return usage_from_step_tokens(steps, source="opencode-session-messages")
        detail = requests.get(f"{base}/session/{session_id}", timeout=5)
        if detail.status_code == 200:
            tokens = detail.json().get("tokens")
            if isinstance(tokens, dict):
                return usage_from_step_tokens([tokens], source="opencode-session-aggregate")
    except Exception:
        pass
    return unavailable_token_usage()


def _pending_human_requests(base: str, session_id: str) -> str:
    """What this session is waiting on a person for, via OpenCode's documented listings.

    In server mode OpenCode can stop and ask - a permission prompt, or its question tool - and
    that shows in the TUI and waits. The orchestrator never answers on anyone's behalf (that
    would be a permission bypass); it only says so, so a timeout explains itself. Never raises.
    """
    found = []
    for path, noun in (("/permission", "permission request"), ("/question", "question")):
        try:
            resp = requests.get(base + path, timeout=3)
            items = resp.json() if resp.status_code == 200 else []
            count = sum(
                1 for item in items if isinstance(item, dict) and item.get("sessionID") == session_id
            ) if isinstance(items, list) else 0
        except Exception:
            count = 0
        if count:
            found.append(f"{count} {noun}{'s' if count > 1 else ''}")
    return " and ".join(found)


def _stop_server(server_process: Optional[subprocess.Popen]) -> None:
    """Stop the server and every process it started. Never raises."""
    if server_process is None:
        return
    from orchestrator.process_jobs import finish_owned, stop_owned

    pid = getattr(server_process, "pid", None)
    if server_process.poll() is None and not stop_owned(server_process):
        # Not owned by a job (it could not be established): the PID tree is the fallback.
        if sys.platform == "win32" and isinstance(pid, int):
            stop_process_tree(pid)
    try:
        server_process.terminate()
        server_process.wait(timeout=5)
    except Exception:
        try:
            server_process.kill()
        except Exception:
            pass
    finish_owned(server_process)


def run_opencode_native_tui(
    prompt: str,
    project_root: str,
    model_name: Optional[str] = None,
    timeout_seconds: int = 180,
    pause_on_completion: float = 1.5,
    terminal_title: Optional[str] = None,
    tracer: Optional[object] = None,
    sink: Optional[Callable[[str], Any]] = None,
    close_on_completion: bool = True,
) -> Tuple[str, TokenUsage]:
    """Execute OpenCode in its native interactive TUI while retaining deterministic orchestration.

    Architecture:
    1. Starts an ephemeral loopback OpenCode server (`opencode serve --port <P>`) in `project_root`.
    2. Creates a session on the server via `POST /session`.
    3. Shows the live native interactive OpenCode TUI (`opencode attach http://127.0.0.1:<P>
       -s <sessionID>`) one of two ways:
       - `sink` given (the daemon is watching, Roadmap Phase 10): the attach process runs as
         a local subprocess attached to a pseudo-terminal, and its raw output is relayed to
         `sink` - the same mechanism `launcher.py` uses for every other agent, so the cockpit's
         terminal panel shows OpenCode's actual interactive program rather than a captured
         pipe's worth of machine-readable text. No Antigravity IDE bridge is needed for this.
       - no `sink` (direct CLI usage, no daemon): spawns an Antigravity integrated terminal
         running the same attach command.
    4. Dispatches the prompt to the session via `POST /session/<sessionID>/prompt_async`.
    5. Polls `GET /session/status` and the session's messages until the turn is over: the
       session is no longer `busy`/`retry` and its last assistant message is a completed
       final step (or it went quiet after having been seen working).
    6. Collects the assistant response and exact token usage via server REST APIs.
    7. Tears down the presentation and the server - or, with `close_on_completion=False` on
       the bridge path after a completed turn, leaves both running and registers them
       (`orchestrator.native_sessions`) so the TUI and its history stay inspectable.

    A failed, timed-out or interrupted execution is *always* torn down, whatever
    `close_on_completion` says: a session that may still be working is never left behind.

    Args:
        prompt: Implementation task or repair instructions.
        project_root: Target workspace root directory.
        model_name: Configured model identifier (e.g. 'opencode/big-pickle').
        timeout_seconds: Maximum seconds before timing out.
        pause_on_completion: Pause duration before closing the terminal / attach process.
        terminal_title: Title for the Antigravity integrated terminal tab (bridge path only).
            The session id is appended, so closing one tab can never close another run's.
        tracer: Optional tracer instance.
        sink: Where to relay the attach process's live output (daemon path). When given, the
            Antigravity IDE bridge is not used at all - this parameter is what lets the
            cockpit's panel show OpenCode's native TUI without that IDE being open.
        close_on_completion: False retains a completed bridge session for inspection. Ignored
            with `sink`: the cockpit panel already keeps the transcript after the process ends.

    Returns:
        Tuple of (response_text, token_usage). `response_text` is empty when the turn produced
        no text and no tool call, so the caller's empty-output handling applies.

    Raises:
        NativeTUIUnavailableError: If no surface can show the TUI (bridge offline, no daemon).
        CLIExecutionError: If the server fails to start, the prompt fails, or OpenCode recorded
            an error on the turn (with whatever usage was reported attached).
        CLITimeoutError: If execution exceeds timeout_seconds (with reported usage attached).
    """
    # Fail before anything is spawned - a bridge-dependent request should not cost a server
    # process before it is refused. When `sink` is given there is nothing bridge-shaped to
    # check: the daemon shows the attach process itself.
    bridge_url: Optional[str] = None
    if sink is None:
        bridge_url = get_antigravity_bridge_url()
        if not bridge_url or not check_antigravity_bridge(bridge_url):
            raise NativeTUIUnavailableError(
                "Native TUI execution requires the Antigravity integrated terminal "
                "bridge to be online and active (or a daemon to relay it instead)."
            )

    opencode_bin = _resolve_opencode_binary()
    port = find_free_port()
    base = f"http://127.0.0.1:{port}"
    title = terminal_title or "LangGraph - OpenCode Implementer"

    server_process: Optional[subprocess.Popen] = None
    tab_title: Optional[str] = None
    attach_thread: Optional[threading.Thread] = None
    # Ends the daemon-path `attach` process at teardown. Measured on 1.18.29 under ConPTY: the
    # attach TUI does NOT exit when its server is stopped, so without this it lingered until the
    # pty runner's own timeout - or outlived the orchestrator entirely.
    attach_stop = threading.Event()
    retained = False

    try:
        # 1. Start ephemeral opencode server in project_root
        server_cmd = [opencode_bin, "serve", "--port", str(port), "--hostname", "127.0.0.1"]
        from orchestrator.process_jobs import release_owned, spawn_owned

        # Owned by its own kill-on-close job: a hard-killed orchestrator no longer leaves this
        # server running (Package B measured exactly that). Its own job, not a shared one, so a
        # session retained below can be released on its own.
        server_process = spawn_owned(
            server_cmd,
            cwd=project_root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        # 2. Wait for server health
        server_ready = False
        start_wait = time.time()
        while time.time() - start_wait < 10.0:
            if server_process.poll() is not None:
                raise CLIExecutionError(
                    f"OpenCode server terminated unexpectedly with code {server_process.returncode}"
                )
            try:
                h_resp = requests.get(f"{base}/global/health", timeout=1)
                if h_resp.status_code == 200:
                    server_ready = True
                    break
            except Exception:
                time.sleep(0.2)

        if not server_ready:
            raise CLIExecutionError(f"OpenCode server failed to start on port {port} within 10s")

        # 3. Create session
        sess_resp = requests.post(f"{base}/session", json={"directory": project_root}, timeout=5)
        if sess_resp.status_code != 200:
            raise CLIExecutionError(f"Failed to create OpenCode session: {sess_resp.text}")
        session_id = sess_resp.json().get("id")
        if not session_id:
            raise CLIExecutionError(f"OpenCode server returned invalid session payload: {sess_resp.text}")

        # 4. Show the native TUI - through the daemon's captured pty when it is watching
        #    (`sink` given), through the Antigravity IDE bridge otherwise.
        attach_argv = [opencode_bin, "attach", base, "-s", session_id]

        if sink is not None:
            from orchestrator.terminals import PtyUnavailableError, pty_available
            from orchestrator.terminals import run_captured as _run_captured
            from orchestrator.terminals import run_captured_pty as _run_captured_pty

            def _attach() -> None:
                # A failure here is a failure to *show* the work, not to do it - the real
                # task's outcome comes from the REST polling below regardless.
                runner = _run_captured_pty if pty_available() else _run_captured
                try:
                    runner(
                        attach_argv, cwd=project_root, timeout=timeout_seconds + 30,
                        sink=sink, stop=attach_stop,
                    )
                except PtyUnavailableError:
                    pass  # a race with pty_available() itself
                except Exception:
                    pass

            attach_thread = threading.Thread(target=_attach, daemon=True)
            attach_thread.start()
        else:
            candidate_title = f"{title} [{session_id[-6:]}]"
            if not launch_antigravity_integrated_terminal(
                title=candidate_title,
                cwd=project_root,
                command=_powershell_invocation(attach_argv),
                bridge_url=bridge_url,
            ):
                raise CLIExecutionError(f"Failed to launch Antigravity integrated terminal for {title}")
            tab_title = candidate_title

        # Give the attach process a moment to connect before the prompt is dispatched.
        time.sleep(1.5)

        # 5. Send task asynchronously to the session
        provider_id, model_id = parse_model_spec(model_name)
        prompt_resp = requests.post(
            f"{base}/session/{session_id}/prompt_async",
            json={
                "parts": [{"type": "text", "text": prompt}],
                "model": {"providerID": provider_id, "modelID": model_id},
            },
            timeout=10,
        )
        if prompt_resp.status_code not in (200, 204):
            raise CLIExecutionError(f"Failed to submit task to OpenCode session: {prompt_resp.text}")

        # 6. Poll until the turn is over. Right after prompt_async returns the session can be
        #    briefly absent from /session/status before it turns busy (observed on 1.18.29), so
        #    "not busy" alone never ends a turn: the messages have to say it is finished too.
        prompt_sent_at = time.time()
        deadline = prompt_sent_at + timeout_seconds
        seen_active = False
        idle_polls = 0
        messages: Any = None
        while True:
            if time.time() >= deadline:
                usage = _collect_usage(base, session_id, _safe_messages(base, session_id))
                waiting = _pending_human_requests(base, session_id)
                raise CLITimeoutError(
                    f"OpenCode native TUI execution timed out after {timeout_seconds}s"
                    + (f"; it was waiting in its TUI for a person: {waiting} pending" if waiting else ""),
                    timeout=timeout_seconds,
                    token_usage=usage if usage.get("available") else None,
                )
            time.sleep(POLL_INTERVAL_SECONDS)
            if server_process.poll() is not None:
                raise CLIExecutionError("OpenCode server exited prematurely during execution")
            try:
                st_resp = requests.get(f"{base}/session/status", timeout=3)
                if st_resp.status_code != 200:
                    continue
                state = (st_resp.json() or {}).get(session_id)
                kind = state.get("type") if isinstance(state, dict) else None
                if kind in ACTIVE_STATUSES:
                    seen_active = True
                    idle_polls = 0
                    continue
                idle_polls += 1
                messages = _get_messages(base, session_id)
                # Quiet for IDLE_CONFIRM_POLLS in a row ends the turn once there is evidence
                # it ran at all - seen working, or an assistant message exists. Before either,
                # quiet is only the gap between prompt_async and the session turning busy.
                worked = seen_active or _latest_assistant(messages) is not None
                if _turn_complete(messages) or (worked and idle_polls >= IDLE_CONFIRM_POLLS):
                    break
            except Exception:
                continue
            if (
                not seen_active
                and _latest_assistant(messages) is None
                and time.time() - prompt_sent_at >= NO_START_GRACE_SECONDS
            ):
                usage = _collect_usage(base, session_id, messages)
                raise CLIExecutionError(
                    f"OpenCode never started the turn: {int(NO_START_GRACE_SECONDS)}s after the "
                    "prompt was accepted the session had not become busy and had produced no "
                    f"reply. This is how an unusable model or provider presents "
                    f"(model: {model_name or 'default'}).",
                    token_usage=usage if usage.get("available") else None,
                )

        # 7. Collect the turn: its final messages, its usage, and whether OpenCode says it failed.
        final_messages = _safe_messages(base, session_id)
        if final_messages is not None:
            messages = final_messages
        token_usage = _collect_usage(base, session_id, messages)
        error = _turn_error(messages)
        if error:
            raise CLIExecutionError(
                f"OpenCode reported an error for this turn: {error}",
                token_usage=token_usage if token_usage.get("available") else None,
            )
        output_text = _collect_output(messages)

        # 8. Retain the session for inspection, or close it.
        if not close_on_completion and sink is None and tab_title is not None:
            register_session(
                {
                    "session_id": session_id,
                    "port": port,
                    "server_pid": server_process.pid,
                    "terminal_title": tab_title,
                    "bridge_url": bridge_url,
                    "project_root": project_root,
                    "model": model_name,
                }
            )
            # Kept on purpose and now accounted for by the retention registry, which stops it
            # only after proving it still owns its port - so it must outlive this process.
            release_owned(server_process)
            retained = True
            log_retained = getattr(tracer, "log_native_session_retained", None)
            if callable(log_retained):
                log_retained(tab_title, session_id, port)
        elif pause_on_completion > 0:
            time.sleep(pause_on_completion)

        return output_text, token_usage

    finally:
        if not retained:
            if tab_title and bridge_url:
                try:
                    close_antigravity_integrated_terminal(title=tab_title, bridge_url=bridge_url)
                except Exception:
                    pass
            # The attach process is ended explicitly - it does not exit on its own when the
            # server goes away - and then waited for, so attach processes cannot pile up in a
            # long-lived daemon or outlive the orchestrator. The cockpit panel keeps its
            # transcript either way.
            attach_stop.set()
            _stop_server(server_process)
            if attach_thread is not None:
                attach_thread.join(timeout=10.0)


def _safe_messages(base: str, session_id: str) -> Any:
    try:
        return _get_messages(base, session_id)
    except Exception:
        return None
