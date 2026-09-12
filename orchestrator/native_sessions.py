"""Native TUI sessions kept open after their turn, and the one way they are closed again.

With ``execution.close_terminal_on_completion: false`` an OpenCode native TUI session is not torn
down when its turn completes: its server keeps running and the ``opencode attach`` TUI stays
connected in its terminal tab, so a person can scroll the agent's real interface and history
afterwards. Keeping a process alive on purpose is only acceptable if it is *accounted for*, so
every retained session is written here, and:

* **Bounded.** At most `MAX_RETAINED_SESSIONS` are kept; registering one more closes the oldest.
* **Closed deliberately.** ``python -m orchestrator --close-sessions`` closes every one of them.
* **Never a guess about which process to kill.** A PID recorded yesterday may belong to something
  else today. A server is only stopped when its recorded port still answers as an OpenCode
  server *and* that port is still owned by the recorded PID; otherwise the record is dropped and
  nothing is killed.

Sessions that failed, timed out, or were interrupted are never retained - they are always torn
down where they ran (`orchestrator.agents.opencode_tui`).
"""

import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from typing import Any, Dict, Iterable, List, Optional

#: Override for the registry location (tests point it at a temporary file).
REGISTRY_ENV = "ORCHESTRATOR_NATIVE_SESSIONS_FILE"

#: How many retained sessions may exist at once. Each is a live OpenCode server process.
MAX_RETAINED_SESSIONS = 8

_lock = threading.Lock()


def registry_path() -> str:
    """Where retained sessions are recorded."""
    override = os.environ.get(REGISTRY_ENV)
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), ".orchestrator", "native_sessions.json")


def _load() -> List[Dict[str, Any]]:
    try:
        with open(registry_path(), "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return []
    return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []


def _save(records: List[Dict[str, Any]]) -> None:
    path = registry_path()
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(records, handle, indent=2)
        os.replace(tmp, path)
    except OSError:
        pass


def server_alive(port: Any, timeout: float = 1.0) -> bool:
    """True when an OpenCode server answers its health check on `port`."""
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{int(port)}/global/health", timeout=timeout
        ) as resp:
            if resp.status != 200:
                return False
            return bool(json.loads(resp.read().decode("utf-8")).get("healthy"))
    except Exception:
        return False


def listening_pid(port: Any) -> Optional[int]:
    """The PID that owns a listening TCP socket on `port`, or None when it cannot be told."""
    try:
        port = int(port)
    except (TypeError, ValueError):
        return None
    if sys.platform == "win32":
        try:
            out = subprocess.run(
                ["netstat", "-ano", "-p", "TCP"],
                shell=False,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=10,
            ).stdout
        except Exception:
            return None
        for line in out.splitlines():
            fields = line.split()
            if len(fields) >= 5 and fields[0].upper() == "TCP" and fields[3].upper() == "LISTENING":
                if fields[1].endswith(f":{port}") and fields[4].isdigit():
                    return int(fields[4])
        return None
    try:
        out = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            shell=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
    except Exception:
        return None
    first = out.splitlines()[0] if out else ""
    return int(first) if first.isdigit() else None


def stop_process_tree(pid: Any) -> None:
    """Stop a process and everything it started. Never raises.

    On Windows a process's children are not taken down with it, so a plain terminate leaves
    them running - which is exactly how `opencode.CMD` used to orphan every native TUI server.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return
    if pid <= 0:
        return
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                shell=False,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=15,
            )
        else:
            os.kill(pid, signal.SIGTERM)
    except Exception:
        pass


def close_session(record: Dict[str, Any]) -> Dict[str, Any]:
    """Close one retained session's terminal and stop its server, when that is provably safe."""
    outcome: Dict[str, Any] = {
        "session_id": record.get("session_id"),
        "terminal_title": record.get("terminal_title"),
        "terminal_closed": False,
        "server_stopped": False,
        "note": "",
    }

    title = record.get("terminal_title")
    if title:
        try:
            from orchestrator.launcher import close_antigravity_integrated_terminal

            outcome["terminal_closed"] = bool(
                close_antigravity_integrated_terminal(
                    title=title, bridge_url=record.get("bridge_url")
                )
            )
        except Exception:
            outcome["terminal_closed"] = False

    port = record.get("port")
    pid = record.get("server_pid")
    if not server_alive(port):
        outcome["note"] = "server already stopped"
        return outcome
    owner = listening_pid(port)
    if owner is None or owner != pid:
        # Something answers on that port, but it cannot be shown to be the process this record
        # started. Killing it on a guess is the one thing this module must never do.
        outcome["note"] = (
            f"port {port} is no longer owned by recorded pid {pid}; left running"
        )
        return outcome
    stop_process_tree(pid)
    deadline = time.time() + 5.0
    while time.time() < deadline and server_alive(port, timeout=0.5):
        time.sleep(0.2)
    outcome["server_stopped"] = not server_alive(port, timeout=0.5)
    return outcome


def list_sessions() -> List[Dict[str, Any]]:
    """Every retained session whose server is still running. Dead records are forgotten."""
    with _lock:
        records = _load()
        alive = [r for r in records if server_alive(r.get("port"))]
        if len(alive) != len(records):
            _save(alive)
        return alive


def register_session(record: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Record a retained session. Returns what had to be closed to stay within the bound."""
    with _lock:
        records = [r for r in _load() if server_alive(r.get("port"))]
        entry = dict(record)
        entry.setdefault("created_at", time.time())
        records.append(entry)
        overflow = records[:-MAX_RETAINED_SESSIONS] if len(records) > MAX_RETAINED_SESSIONS else []
        kept = records[len(overflow):]
        _save(kept)
    return [close_session(r) for r in overflow]


def close_sessions(session_ids: Optional[Iterable[str]] = None) -> List[Dict[str, Any]]:
    """Close retained sessions - all of them, or only the ids given."""
    wanted = set(session_ids) if session_ids else None
    with _lock:
        records = _load()
        chosen = [r for r in records if wanted is None or r.get("session_id") in wanted]
        remaining = [r for r in records if r not in chosen]
        _save(remaining)
    return [close_session(r) for r in chosen]
