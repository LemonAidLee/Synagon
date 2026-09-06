"""Process launcher module for executing external agent CLIs in headless or visible terminal mode."""

import argparse
import ctypes
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

DEFAULT_BRIDGE_URL = "http://127.0.0.1:49182"
BRIDGE_PORT_FILE = os.path.expanduser(r"~/.antigravity-ide/terminal_bridge.json")


def get_antigravity_bridge_url() -> str:
    """Retrieve the URL of the running Antigravity IDE terminal bridge.

    Reads the port dynamically from the bridge port file if available,
    otherwise falls back to the default port (49182).
    """
    if os.path.isfile(BRIDGE_PORT_FILE):
        try:
            with open(BRIDGE_PORT_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                port = data.get("port")
                host = data.get("host", "127.0.0.1")
                if port:
                    return f"http://{host}:{port}"
        except Exception:
            pass
    return DEFAULT_BRIDGE_URL


def check_antigravity_bridge(
    bridge_url: Optional[str] = None, timeout: float = 1.0
) -> bool:
    """Check if the Antigravity Integrated Terminal Bridge is reachable and healthy.

    Args:
        bridge_url: Optional explicit bridge URL.
        timeout: Maximum seconds to wait for health check response.

    Returns:
        True if bridge responded with 200 OK and status 'ok', False otherwise.
    """
    url = (bridge_url or get_antigravity_bridge_url()).rstrip("/") + "/health"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                return data.get("status") == "ok"
    except Exception:
        return False
    return False


def launch_antigravity_integrated_terminal(
    title: str,
    cwd: str,
    command: str,
    env: Optional[Dict[str, str]] = None,
    bridge_url: Optional[str] = None,
    timeout: float = 3.0,
) -> bool:
    """Request the Antigravity IDE Integrated Terminal Bridge to create a terminal.

    Args:
        title: Window/tab title for the integrated terminal.
        cwd: Working directory (must be project root).
        command: Command string to execute in the terminal.
        env: Optional environment variables dictionary.
        bridge_url: Optional explicit bridge URL.
        timeout: Maximum seconds to wait for bridge response.

    Returns:
        True if terminal was successfully created, False otherwise.
    """
    from orchestrator.agents.exceptions import CLIExecutionError

    url = (bridge_url or get_antigravity_bridge_url()).rstrip("/") + "/create_terminal"
    payload: Dict[str, Any] = {
        "title": title,
        "cwd": cwd,
        "command": command,
    }
    if env:
        payload["env"] = env

    data_bytes = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data_bytes,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                resp_data = json.loads(resp.read().decode("utf-8"))
                return bool(resp_data.get("success", False))
    except Exception as exc:
        raise CLIExecutionError(
            message=f"Failed to communicate with Antigravity Terminal Bridge: {exc}",
            returncode=1,
            command=[command],
        ) from exc
    return False


def close_antigravity_integrated_terminal(
    title: str,
    bridge_url: Optional[str] = None,
    timeout: float = 2.0,
) -> bool:
    """Request the Antigravity IDE Integrated Terminal Bridge to close a terminal tab.

    Args:
        title: Title of the terminal tab to close.
        bridge_url: Optional explicit bridge URL.
        timeout: Timeout in seconds.

    Returns:
        True if close request was received, False otherwise.
    """
    url = (bridge_url or get_antigravity_bridge_url()).rstrip("/") + "/close_terminal"
    payload = {"title": title}
    data_bytes = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data_bytes,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                resp_data = json.loads(resp.read().decode("utf-8"))
                return bool(resp_data.get("success", False))
    except Exception:
        return False
    return False


@dataclass
class ExecutionResult:
    """Standardized record of external CLI command execution."""
    returncode: int
    stdout: str
    stderr: str
    duration_seconds: float
    command: List[str]


def get_terminal_title(
    agent: str,
    role: str,
    repair_attempt: Optional[int] = None,
    verification_attempt: Optional[int] = None,
) -> str:
    """Generate a meaningful, standardized window title for a visible agent terminal.

    Examples:
        - LangGraph - Antigravity Researcher
        - LangGraph - Claude Planner
        - LangGraph - OpenCode Implementer
        - LangGraph - Claude Verifier
        - LangGraph - OpenCode Repair #1
        - LangGraph - Claude Verification #2

    Args:
        agent: Name of the agent (e.g. 'antigravity', 'claude', 'opencode').
        role: Functional role (e.g. 'researcher', 'planner', 'implementer', 'verifier', 'repair').
        repair_attempt: Optional repair attempt count (1-indexed).
        verification_attempt: Optional verification evaluation count (1-indexed).

    Returns:
        Formatted window title string.
    """
    # Clean display names for agents
    agent_clean = agent.strip().lower()
    if agent_clean == "antigravity":
        agent_display = "Antigravity"
    elif agent_clean == "claude":
        agent_display = "Claude"
    elif agent_clean == "opencode":
        agent_display = "OpenCode"
    else:
        agent_display = agent.capitalize()

    role_clean = role.strip().lower()

    # Repair execution
    if role_clean in ("repair", "opencode_repair") or (
        role_clean == "implementer" and repair_attempt and repair_attempt > 0
    ):
        attempt_str = f" #{repair_attempt}" if repair_attempt else ""
        return f"LangGraph - {agent_display} Repair{attempt_str}"

    # Reverification execution
    if role_clean == "verifier" and verification_attempt and verification_attempt > 1:
        return f"LangGraph - {agent_display} Verification #{verification_attempt}"

    if role_clean == "verifier":
        return f"LangGraph - {agent_display} Verifier"

    if role_clean == "researcher":
        return f"LangGraph - {agent_display} Researcher"

    if role_clean == "planner":
        return f"LangGraph - {agent_display} Planner"

    if role_clean == "implementer":
        return f"LangGraph - {agent_display} Implementer"

    return f"LangGraph - {agent_display} {role.capitalize()}"


def run_agent_cli(
    cmd: List[str],
    cwd: Optional[str] = None,
    timeout: int = 180,
    visible: bool = False,
    title: Optional[str] = None,
    agent: Optional[str] = None,
    role: Optional[str] = None,
    model: Optional[str] = None,
    terminal_type: str = "auto",
    pause_on_completion: float = 1.5,
    tracer: Optional[Any] = None,
) -> ExecutionResult:
    """Execute an agent CLI command with optional visible terminal window.

    In headless mode (visible=False):
        Directly invokes subprocess.run with captured output.

    In visible mode (visible=True):
        Launches the agent in its own visible Windows terminal window, streams live
        stdout/stderr output to the terminal screen for user observation, captures
        the structured output and exit code, waits synchronously for completion,
        and cleanly closes after the specified pause.

    Args:
        cmd: List of command arguments.
        cwd: Working directory (must be project root).
        timeout: Maximum seconds before CLITimeoutError.
        visible: Whether to launch in a visible terminal window.
        title: Optional title for the terminal window.
        agent: Agent provider name (for title and logging).
        role: Agent role name (for title and logging).
        model: Model identifier (for display).
        terminal_type: Terminal host ('auto', 'windows_terminal', 'console').
        pause_on_completion: Seconds to keep visible terminal open after process finishes.
        tracer: Optional tracer instance for logging lifecycle events.

    Returns:
        ExecutionResult containing returncode, stdout, stderr, duration, command.

    Raises:
        CLITimeoutError: If execution exceeds timeout.
        CLIExecutionError: If child process fails or exits with non-zero code.
    """
    if not cmd:
        raise ValueError("Cannot execute empty command list")

    from orchestrator.agents.exceptions import CLIExecutionError, CLITimeoutError

    working_dir = cwd or os.getcwd()

    if not visible:
        # Headless execution
        t0 = time.time()
        try:
            res = subprocess.run(
                cmd,
                cwd=working_dir,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise CLITimeoutError(
                f"Agent CLI command timed out after {timeout} seconds",
                timeout=timeout,
                command=cmd,
            ) from exc

        duration = round(time.time() - t0, 2)
        return ExecutionResult(
            returncode=res.returncode,
            stdout=res.stdout,
            stderr=res.stderr,
            duration_seconds=duration,
            command=cmd,
        )

    # Visible terminal execution
    term_title = title or get_terminal_title(agent or "Agent", role or "Worker")

    if tracer and hasattr(tracer, "log_terminal_launch"):
        tracer.log_terminal_launch(agent or "agent", role or "role", term_title, mode="visible")

    tmp_dir = tempfile.mkdtemp(prefix="langgraph_terminal_")
    try:
        job_file = os.path.join(tmp_dir, "job.json")
        status_file = os.path.join(tmp_dir, "status.json")

        job_spec: Dict[str, Any] = {
            "title": term_title,
            "agent": agent or "",
            "role": role or "",
            "model": model or "",
            "cmd": cmd,
            "cwd": working_dir,
            "status_file": status_file,
            "timeout": timeout,
            "pause_seconds": max(0.0, float(pause_on_completion)),
        }

        with open(job_file, "w", encoding="utf-8") as f:
            json.dump(job_spec, f, indent=2)

        # Build runner command and environment
        runner_env = os.environ.copy()
        orchestrator_pkg_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        current_pythonpath = runner_env.get("PYTHONPATH", "")
        if orchestrator_pkg_parent not in current_pythonpath.split(os.pathsep):
            runner_env["PYTHONPATH"] = (
                f"{orchestrator_pkg_parent}{os.pathsep}{current_pythonpath}"
                if current_pythonpath
                else orchestrator_pkg_parent
            )

        runner_args = [
            sys.executable,
            "-m",
            "orchestrator.launcher",
            "--job-file",
            job_file,
        ]

        t0 = time.time()
        norm_type = terminal_type.lower()
        use_integrated = False

        if norm_type in ("antigravity_integrated", "integrated"):
            if not check_antigravity_bridge():
                raise CLIExecutionError(
                    message=(
                        "Antigravity IDE Integrated Terminal Bridge is not reachable at "
                        f"{get_antigravity_bridge_url()}.\n"
                        "Please ensure Antigravity IDE is running and the bridge extension is active.\n"
                        "Run 'python tools/install_terminal_bridge.py' to install or verify the bridge."
                    ),
                    returncode=1,
                    command=cmd,
                )
            use_integrated = True
        elif norm_type == "auto":
            # Auto-detect: prefer integrated Antigravity IDE terminal if bridge is available
            if check_antigravity_bridge():
                use_integrated = True
            else:
                use_integrated = False

        if use_integrated:
            # Launch via Antigravity Integrated Terminal Bridge
            pythonpath_val = runner_env.get("PYTHONPATH", "")
            runner_cmd_str = (
                f'$env:PYTHONPATH="{pythonpath_val}"; '
                f'& "{sys.executable}" -m orchestrator.launcher --job-file "{job_file}"'
            )
            bridge_env = {
                "PYTHONPATH": pythonpath_val,
                "PYTHONIOENCODING": "utf-8",
            }

            launch_antigravity_integrated_terminal(
                title=term_title,
                cwd=working_dir,
                command=runner_cmd_str,
                env=bridge_env,
            )

            # Wait for status file completion
            deadline = time.time() + timeout + pause_on_completion + 30
            status_data: Optional[Dict[str, Any]] = None

            while time.time() < deadline:
                if os.path.isfile(status_file):
                    try:
                        with open(status_file, "r", encoding="utf-8") as sf:
                            status_data = json.load(sf)
                        break
                    except (json.JSONDecodeError, PermissionError):
                        time.sleep(0.1)
                time.sleep(0.2)

            if not status_data:
                raise CLITimeoutError(
                    f"Integrated agent terminal timed out after {timeout} seconds (waiting on status file)",
                    timeout=timeout,
                    command=cmd,
                )
        else:
            wt_path = None
            if norm_type in ("windows_terminal", "wt") or norm_type == "auto":
                wt_path = shutil.which("wt.exe")
                if not wt_path:
                    candidate = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WindowsApps\wt.exe")
                    if os.path.isfile(candidate):
                        wt_path = candidate

            if wt_path:
                # Launch via Windows Terminal client
                launch_cmd = [
                    wt_path,
                    "--title",
                    term_title,
                    "-d",
                    working_dir,
                ] + runner_args

                # wt.exe handles window creation
                subprocess.Popen(launch_cmd, env=runner_env)

                # Wait for status file completion
                deadline = time.time() + timeout + pause_on_completion + 30
                status_data: Optional[Dict[str, Any]] = None

                while time.time() < deadline:
                    if os.path.isfile(status_file):
                        try:
                            with open(status_file, "r", encoding="utf-8") as sf:
                                status_data = json.load(sf)
                            break
                        except (json.JSONDecodeError, PermissionError):
                            # File being written
                            time.sleep(0.1)
                    time.sleep(0.2)

                if not status_data:
                    raise CLITimeoutError(
                        f"Visible agent terminal timed out after {timeout} seconds (waiting on status file)",
                        timeout=timeout,
                        command=cmd,
                    )
            else:
                # Native Windows console allocation with CREATE_NEW_CONSOLE (0x00000010)
                create_new_console_flag = 0x00000010
                proc = subprocess.Popen(
                    runner_args,
                    cwd=working_dir,
                    env=runner_env,
                    creationflags=create_new_console_flag,
                )

                try:
                    proc.wait(timeout=timeout + pause_on_completion + 30)
                except subprocess.TimeoutExpired as exc:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    raise CLITimeoutError(
                        f"Visible agent terminal timed out after {timeout} seconds",
                        timeout=timeout,
                        command=cmd,
                    ) from exc

                # Read status file
                status_data = None
                if os.path.isfile(status_file):
                    try:
                        with open(status_file, "r", encoding="utf-8") as sf:
                            status_data = json.load(sf)
                    except Exception:
                        status_data = None

                if not status_data:
                    # Fallback if runner terminated abnormally
                    status_data = {
                        "returncode": proc.returncode,
                        "stdout": "",
                        "stderr": f"Terminal runner exited with code {proc.returncode} without writing status",
                        "duration_seconds": round(time.time() - t0, 2),
                    }

        duration = status_data.get("duration_seconds", round(time.time() - t0, 2))
        returncode = status_data.get("returncode", 0)

        if tracer and hasattr(tracer, "log_terminal_exit"):
            tracer.log_terminal_exit(agent or "agent", role or "role", returncode, duration)

        return ExecutionResult(
            returncode=returncode,
            stdout=status_data.get("stdout", ""),
            stderr=status_data.get("stderr", ""),
            duration_seconds=duration,
            command=cmd,
        )

    finally:
        # Cleanup temporary files
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass


def _runner_entrypoint(job_file_path: str) -> None:
    """Internal runner entrypoint executed inside the visible terminal window."""
    with open(job_file_path, "r", encoding="utf-8") as f:
        job = json.load(f)

    title = job.get("title", "LangGraph Agent Execution")
    agent = job.get("agent", "")
    role = job.get("role", "")
    model = job.get("model", "")
    cmd = job.get("cmd", [])
    cwd = job.get("cwd", os.getcwd())
    status_file = job.get("status_file")
    timeout = job.get("timeout", 180)
    pause_seconds = job.get("pause_seconds", 1.5)

    # Set console window title via Win32 API and ANSI sequence
    try:
        ctypes.windll.kernel32.SetConsoleTitleW(title)
    except Exception:
        pass
    sys.stdout.write(f"\033]0;{title}\007")
    sys.stdout.flush()

    # Informative header banner
    print("=" * 72)
    print(f"  {title.upper()}")
    print("=" * 72)
    meta_parts = []
    if agent:
        meta_parts.append(f"Agent: {agent}")
    if role:
        meta_parts.append(f"Role: {role}")
    if model:
        meta_parts.append(f"Model: {model}")
    if meta_parts:
        print("  " + " | ".join(meta_parts))
    print(f"  Working Directory: {cwd}")
    print("-" * 72)
    sys.stdout.flush()

    t0 = time.time()
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except Exception as exc:
        err_msg = f"Failed to spawn process: {exc}\n"
        sys.stderr.write(err_msg)
        sys.stderr.flush()
        if status_file:
            with open(status_file, "w", encoding="utf-8") as sf:
                json.dump({
                    "returncode": 1,
                    "stdout": "",
                    "stderr": err_msg,
                    "duration_seconds": round(time.time() - t0, 2),
                }, sf)
        time.sleep(pause_seconds)
        sys.exit(1)

    stdout_lines: List[str] = []
    stderr_lines: List[str] = []

    def stream_stdout():
        try:
            for line in iter(proc.stdout.readline, ""):
                stdout_lines.append(line)
                sys.stdout.write(line)
                sys.stdout.flush()
            proc.stdout.close()
        except Exception:
            pass

    def stream_stderr():
        try:
            for line in iter(proc.stderr.readline, ""):
                stderr_lines.append(line)
                sys.stderr.write(line)
                sys.stderr.flush()
            proc.stderr.close()
        except Exception:
            pass

    t_out = threading.Thread(target=stream_stdout, daemon=True)
    t_err = threading.Thread(target=stream_stderr, daemon=True)
    t_out.start()
    t_err.start()

    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            proc.kill()
        except Exception:
            pass

    t_out.join(timeout=2.0)
    t_err.join(timeout=2.0)
    elapsed = round(time.time() - t0, 2)

    returncode = proc.returncode if not timed_out else -1
    full_stdout = "".join(stdout_lines)
    full_stderr = "".join(stderr_lines)

    if timed_out:
        full_stderr += f"\nProcess timed out after {timeout} seconds\n"

    # Write status file atomically
    if status_file:
        tmp_status = status_file + ".tmp"
        with open(tmp_status, "w", encoding="utf-8") as sf:
            json.dump({
                "returncode": returncode,
                "stdout": full_stdout,
                "stderr": full_stderr,
                "duration_seconds": elapsed,
            }, sf)
        try:
            os.replace(tmp_status, status_file)
        except Exception:
            with open(status_file, "w", encoding="utf-8") as sf:
                json.dump({
                    "returncode": returncode,
                    "stdout": full_stdout,
                    "stderr": full_stderr,
                    "duration_seconds": elapsed,
                }, sf)

    print("-" * 72)
    status_label = "TIMEOUT" if timed_out else ("SUCCESS" if returncode == 0 else f"FAILED (code {returncode})")
    print(f"  Execution finished: {status_label} in {elapsed}s")
    if pause_seconds > 0:
        print(f"  Closing terminal in {pause_seconds}s...")
    print("=" * 72)
    sys.stdout.flush()

    if pause_seconds > 0:
        time.sleep(pause_seconds)

    sys.exit(returncode if returncode >= 0 else 1)


def main() -> None:
    """CLI parser for runner execution."""
    parser = argparse.ArgumentParser(description="LangGraph Terminal Runner")
    parser.add_argument("--job-file", required=True, help="Path to job JSON file")
    args = parser.parse_args()
    _runner_entrypoint(args.job_file)


if __name__ == "__main__":
    main()
