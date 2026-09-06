"""Native TUI execution controller for OpenCode via official client/server architecture.

Decouples the presentation surface (interactive OpenCode TUI in Antigravity terminal)
from the orchestration control plane (loopback HTTP server + REST API).
"""

import json
import os
import shutil
import socket
import subprocess
import time
from typing import Optional, Tuple

import requests

from orchestrator.agents.exceptions import CLIExecutionError, CLITimeoutError
from orchestrator.launcher import (
    check_antigravity_bridge,
    close_antigravity_integrated_terminal,
    get_antigravity_bridge_url,
    launch_antigravity_integrated_terminal,
)
from orchestrator.types import TokenUsage, create_token_usage, unavailable_token_usage


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


def run_opencode_native_tui(
    prompt: str,
    project_root: str,
    model_name: Optional[str] = None,
    timeout_seconds: int = 180,
    pause_on_completion: float = 1.5,
    terminal_title: Optional[str] = None,
    tracer: Optional[object] = None,
) -> Tuple[str, TokenUsage]:
    """Execute OpenCode in its native interactive TUI while retaining deterministic orchestration.

    Architecture:
    1. Starts an ephemeral loopback OpenCode server (`opencode serve --port <P>`) in `project_root`.
    2. Creates a session on the server via `POST /session`.
    3. Spawns an Antigravity integrated terminal running `opencode attach http://127.0.0.1:<P> -s <sessionID>`.
       The user sees the live native interactive OpenCode TUI with streaming output and tools.
    4. Dispatches the prompt to the session via `POST /session/<sessionID>/prompt_async`.
    5. Polls `GET /session/status` until the turn transitions to idle.
    6. Collects the assistant response and exact token usage via server REST APIs.
    7. Cleanly cleans up the terminal and terminates the server.

    Args:
        prompt: Implementation task or repair instructions.
        project_root: Target workspace root directory.
        model_name: Configured model identifier (e.g. 'opencode/big-pickle').
        timeout_seconds: Maximum seconds before timing out.
        pause_on_completion: Pause duration before closing terminal tab.
        terminal_title: Title for the Antigravity integrated terminal tab.
        tracer: Optional tracer instance.

    Returns:
        Tuple of (response_text, token_usage).

    Raises:
        CLIExecutionError: If bridge is unavailable, server fails to start, or prompt fails.
        CLITimeoutError: If execution exceeds timeout_seconds.
    """
    bridge_url = get_antigravity_bridge_url()
    if not bridge_url or not check_antigravity_bridge(bridge_url):
        raise CLIExecutionError(
            "Native TUI execution requires Antigravity integrated terminal bridge to be online and active."
        )

    # Resolve opencode binary
    opencode_bin = shutil.which("opencode") or "opencode.cmd"

    port = find_free_port()
    title = terminal_title or "LangGraph - OpenCode Implementer"

    server_process: Optional[subprocess.Popen] = None
    terminal_opened: bool = False

    try:
        # 1. Start ephemeral opencode server in project_root
        server_cmd = [opencode_bin, "serve", "--port", str(port), "--hostname", "127.0.0.1"]
        server_process = subprocess.Popen(
            server_cmd,
            cwd=project_root,
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
                h_resp = requests.get(f"http://127.0.0.1:{port}/global/health", timeout=1)
                if h_resp.status_code == 200:
                    server_ready = True
                    break
            except Exception:
                time.sleep(0.2)

        if not server_ready:
            raise CLIExecutionError(f"OpenCode server failed to start on port {port} within 10s")

        # 3. Create session
        sess_resp = requests.post(
            f"http://127.0.0.1:{port}/session",
            json={"directory": project_root},
            timeout=5,
        )
        if sess_resp.status_code != 200:
            raise CLIExecutionError(f"Failed to create OpenCode session: {sess_resp.text}")
        session_id = sess_resp.json().get("id")
        if not session_id:
            raise CLIExecutionError(f"OpenCode server returned invalid session payload: {sess_resp.text}")

        # 4. Launch native TUI in Antigravity integrated terminal
        attach_cmd = f'opencode attach http://127.0.0.1:{port} -s {session_id}'
        terminal_opened = launch_antigravity_integrated_terminal(
            title=title,
            cwd=project_root,
            command=attach_cmd,
            bridge_url=bridge_url,
        )
        if not terminal_opened:
            raise CLIExecutionError(f"Failed to launch Antigravity integrated terminal for {title}")

        # Give TUI process a brief moment to connect
        time.sleep(1.5)

        # 5. Send task asynchronously to the session
        provider_id, model_id = parse_model_spec(model_name)
        payload = {
            "parts": [{"type": "text", "text": prompt}],
            "model": {"providerID": provider_id, "modelID": model_id},
        }
        prompt_resp = requests.post(
            f"http://127.0.0.1:{port}/session/{session_id}/prompt_async",
            json=payload,
            timeout=10,
        )
        if prompt_resp.status_code not in (200, 204):
            raise CLIExecutionError(f"Failed to submit task to OpenCode session: {prompt_resp.text}")

        # 6. Poll status until completion
        poll_start = time.time()
        was_busy = False
        while time.time() - poll_start < timeout_seconds:
            time.sleep(0.5)
            if server_process.poll() is not None:
                raise CLIExecutionError("OpenCode server exited prematurely during execution")

            try:
                st_resp = requests.get(f"http://127.0.0.1:{port}/session/status", timeout=3)
                if st_resp.status_code == 200:
                    status_dict = st_resp.json()
                    sess_info = status_dict.get(session_id)
                    if sess_info and isinstance(sess_info, dict) and sess_info.get("type") == "busy":
                        was_busy = True
                        continue

                    # If it was busy and is now not busy (or missing), turn is complete
                    if was_busy and (sess_info is None or sess_info.get("type") != "busy"):
                        break

                    # If not was_busy yet, check if completion happened rapidly
                    if not was_busy and time.time() - poll_start > 2.0:
                        msg_check = requests.get(f"http://127.0.0.1:{port}/session/{session_id}/message", timeout=3)
                        if msg_check.status_code == 200:
                            msgs = msg_check.json()
                            if any(m.get("info", {}).get("role") == "assistant" for m in msgs):
                                break
            except Exception:
                pass
        else:
            raise CLITimeoutError(
                f"OpenCode native TUI execution timed out after {timeout_seconds}s",
                timeout=timeout_seconds,
            )

        # 7. Collect output text from assistant message parts
        output_text = ""
        msg_resp = requests.get(f"http://127.0.0.1:{port}/session/{session_id}/message", timeout=5)
        if msg_resp.status_code == 200:
            msgs = msg_resp.json()
            text_parts = []
            for m in msgs:
                if m.get("info", {}).get("role") == "assistant":
                    for p in m.get("parts", []):
                        if p.get("type") == "text" and "text" in p:
                            text_parts.append(p["text"])
                        elif p.get("type") == "tool":
                            tool_name = p.get("tool", "tool")
                            tool_state = p.get("state", {})
                            tool_title = tool_state.get("title") or tool_state.get("output") or tool_name
                            text_parts.append(f"Tool executed: {tool_title}")
            output_text = "\n".join(text_parts).strip()
        if not output_text:
            output_text = f"OpenCode completed session {session_id} successfully."

        # 8. Collect token usage
        token_usage = unavailable_token_usage()
        detail_resp = requests.get(f"http://127.0.0.1:{port}/session/{session_id}", timeout=5)
        if detail_resp.status_code == 200:
            tokens_data = detail_resp.json().get("tokens") or {}
            base_inp = tokens_data.get("input", 0)
            cache_dict = tokens_data.get("cache") or {}
            cache_read = cache_dict.get("read", 0) if isinstance(cache_dict, dict) else 0
            reasoning = tokens_data.get("reasoning", 0)
            out = tokens_data.get("output", 0)
            tot = tokens_data.get("total")
            if tot is None:
                tot = base_inp + cache_read + out
            inp = base_inp + cache_read
            if tot > 0 or inp > 0 or out > 0:
                token_usage = create_token_usage(
                    input_tokens=inp,
                    output_tokens=out,
                    total_tokens=tot,
                    cache_read_tokens=cache_read if cache_read > 0 else None,
                    reasoning_tokens=reasoning if reasoning > 0 else None,
                    available=True,
                    raw_usage=dict(tokens_data),
                )

        # 9. Optional pause before closing terminal
        if pause_on_completion > 0:
            time.sleep(pause_on_completion)

        return output_text, token_usage

    finally:
        if terminal_opened and bridge_url:
            try:
                close_antigravity_integrated_terminal(title=title, bridge_url=bridge_url)
            except Exception:
                pass
            terminal_opened = False

        # Guarantee server process is terminated
        if server_process is not None:
            try:
                server_process.terminate()
                server_process.wait(timeout=3)
            except Exception:
                try:
                    server_process.kill()
                except Exception:
                    pass
                    pass
