"""Live agent output, relayed to a panel instead of only a window (Roadmap Phase 10).

`launcher.py` has always been able to run an agent where a person can watch it: with
``visible=True`` it opens a real terminal, streams the CLI's stdout and stderr to it live, and
waits. What it could never do is let anything *else* see that stream - the terminal is a
separate OS window, and nothing relays its contents anywhere.

This module is that one missing capability, and nothing more:

* a **registry** of live terminals, one per agent execution, each a bounded ring buffer of
  output chunks with a sequence number, so a panel can ask "what has happened since 41?" and
  a page that reconnects does not have to start from nothing;
* a **captured runner** that executes the same command `launcher.py` would, feeding every byte
  into that buffer as it arrives *and* returning exactly what `subprocess.run` returned, so the
  pipeline above it cannot tell the difference.

What this is not
----------------
* **Not a thought-process parser.** Per Roadmap §10.2 this shows the raw terminal an agent's
  CLI is already writing - verdict lines, progress bars, warnings and all. It extracts no
  reasoning and depends on no provider's output format, so no provider changing theirs can
  break it.
* **Not a second source of truth.** A terminal buffer is transcript, not fact. What a run
  *means* is still `store.py`'s `agent_result` and `status.py`'s derivation; the buffer is
  dropped when the daemon stops and nothing reads it back.
* **Not unbounded.** An agent can print a great deal. Each terminal keeps a fixed number of
  characters and drops the oldest, and the registry keeps a fixed number of terminals, for the
  same reason `budget.py` exists: a local process must not grow without a ceiling.

On pseudo-terminals
-------------------
A true pty makes a CLI believe it is talking to a terminal, which is what makes colour, cursor
control, and an actual interactive TUI appear - `run_captured_pty` uses one where it can:
POSIX's standard-library `pty`, or `pywinpty` (ConPTY) on Windows. `pty_available()` says which
is true right now rather than assuming either way, since the Windows dependency is optional.

A pty is not a fix for every agent, and it is worth being precise about which: it changes
*where* output goes, not *what a headless invocation produces*. A CLI run with an explicit
"print machine-readable JSON" flag still prints exactly that JSON on a pty - the flag chose the
format, not the terminal. `run_captured` (a plain pipe) is therefore the right tool for those,
and `daemon.py` renders their buffered output as a finished result rather than a live scroll of
JSON. `run_captured_pty` earns its keep for the one case that is genuinely different: an agent
whose own CLI offers a real interactive program, run instead of asked to print structured
output - `opencode_tui.py`'s `attach` process is exactly that case (§10.5's AO-inspired "watch
the native terminal" idea, not "make json pretty").
"""

import os
import subprocess
import threading
import time
import uuid
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

#: The most characters one terminal keeps. Older output is dropped, oldest first.
MAX_TERMINAL_CHARS = 256 * 1024

#: The most terminals a registry holds. Closed ones are dropped first, oldest first.
MAX_TERMINALS = 32

#: How much output is coalesced into one chunk before it is published.
READ_CHUNK = 4096

TERMINAL_RUNNING = "running"
TERMINAL_EXITED = "exited"


def _now() -> float:
    return time.time()


class TerminalStream:
    """One agent execution's output, as a bounded, sequenced ring buffer.

    Sequence numbers are per-terminal and monotonic. A reader holds the last sequence it saw
    and asks for what came after; if that is older than what the buffer still holds, it is
    told the transcript was truncated rather than being quietly handed a gap.
    """

    def __init__(
        self,
        terminal_id: str,
        agent: str = "",
        role: str = "",
        model: Any = None,
        command: Optional[List[str]] = None,
        cwd: str = "",
        max_chars: int = MAX_TERMINAL_CHARS,
    ) -> None:
        self.id = terminal_id
        self.agent = agent
        self.role = role
        self.model = model
        self.command = list(command or [])
        self.cwd = cwd
        self.max_chars = max(1024, int(max_chars))
        self.started_at = _now()
        self.finished_at: Optional[float] = None
        self.exit_code: Optional[int] = None
        self.state = TERMINAL_RUNNING

        self._chunks: Deque[Tuple[int, str]] = deque()
        self._chars = 0
        self._sequence = 0
        self._dropped_through = 0
        self._lock = threading.Lock()

    # -- writing -----------------------------------------------------------

    def append(self, text: str) -> int:
        """Add output. Returns the sequence number it was given. Never raises."""
        if not text:
            with self._lock:
                return self._sequence
        with self._lock:
            self._sequence += 1
            self._chunks.append((self._sequence, str(text)))
            self._chars += len(text)
            while self._chars > self.max_chars and len(self._chunks) > 1:
                sequence, dropped = self._chunks.popleft()
                self._chars -= len(dropped)
                self._dropped_through = sequence
            return self._sequence

    def close(self, exit_code: Optional[int] = None) -> None:
        """Mark the execution finished. Idempotent."""
        with self._lock:
            if self.state == TERMINAL_EXITED:
                return
            self.state = TERMINAL_EXITED
            self.exit_code = exit_code
            self.finished_at = _now()

    # -- reading -----------------------------------------------------------

    def read_since(self, since: int = 0) -> Dict[str, Any]:
        """Everything after `since`, plus enough context to render it. Pure of I/O."""
        with self._lock:
            chunks = [(sequence, text) for sequence, text in self._chunks if sequence > int(since)]
            truncated = int(since) < self._dropped_through
            return {
                "id": self.id,
                "agent": self.agent,
                "role": self.role,
                "model": self.model,
                "command": self.command,
                "cwd": self.cwd,
                "state": self.state,
                "exit_code": self.exit_code,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "truncated": truncated,
                "since": int(since),
                "next": self._sequence,
                "text": "".join(text for _, text in chunks),
            }

    def snapshot(self) -> Dict[str, Any]:
        """What the terminal is, without its contents. Pure of I/O."""
        with self._lock:
            return {
                "id": self.id,
                "agent": self.agent,
                "role": self.role,
                "model": self.model,
                "state": self.state,
                "exit_code": self.exit_code,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "chars": self._chars,
                "next": self._sequence,
            }


class TerminalRegistry:
    """Every terminal the daemon is relaying, keyed by id and by the agent that owns it."""

    def __init__(self, max_terminals: int = MAX_TERMINALS) -> None:
        self.max_terminals = max(1, int(max_terminals))
        self._terminals: Dict[str, TerminalStream] = {}
        self._order: List[str] = []
        self._lock = threading.Lock()

    def open(
        self,
        agent: str = "",
        role: str = "",
        model: Any = None,
        command: Optional[List[str]] = None,
        cwd: str = "",
    ) -> TerminalStream:
        """Start relaying one agent execution."""
        stream = TerminalStream(
            uuid.uuid4().hex[:12], agent=agent, role=role, model=model,
            command=command, cwd=cwd,
        )
        with self._lock:
            self._terminals[stream.id] = stream
            self._order.append(stream.id)
            self._prune_locked()
        return stream

    def _prune_locked(self) -> None:
        while len(self._order) > self.max_terminals:
            for index, terminal_id in enumerate(self._order):
                if self._terminals[terminal_id].state == TERMINAL_EXITED:
                    self._order.pop(index)
                    self._terminals.pop(terminal_id, None)
                    break
            else:
                terminal_id = self._order.pop(0)  # everything is live; drop the oldest
                self._terminals.pop(terminal_id, None)

    def get(self, terminal_id: str) -> Optional[TerminalStream]:
        with self._lock:
            return self._terminals.get(str(terminal_id or ""))

    def read_since(self, terminal_id: str, since: int = 0) -> Optional[Dict[str, Any]]:
        stream = self.get(terminal_id)
        return stream.read_since(since) if stream is not None else None

    def listing(self) -> List[Dict[str, Any]]:
        """Every terminal, newest first."""
        with self._lock:
            order = list(reversed(self._order))
            terminals = dict(self._terminals)
        return [terminals[key].snapshot() for key in order if key in terminals]

    def live_by_agent(self) -> Dict[str, str]:
        """A map of ``agent/role`` to the id of its most recent terminal.

        This is what lets a cockpit card offer "open the terminal": the card knows an agent
        and a role, and the registry knows which stream that pair is writing to.
        """
        out: Dict[str, str] = {}
        with self._lock:
            order = list(self._order)
            terminals = dict(self._terminals)
        for key in order:  # oldest first, so the newest wins
            stream = terminals.get(key)
            if stream is None:
                continue
            out["%s/%s" % (stream.agent, stream.role)] = stream.id
        return out

    def close_all(self) -> None:
        """Mark every live terminal finished. Called when the daemon stops."""
        with self._lock:
            streams = list(self._terminals.values())
        for stream in streams:
            stream.close(stream.exit_code)


# ---------------------------------------------------------------------------
# Running a command with its output relayed
# ---------------------------------------------------------------------------


class CapturedResult:
    """What a captured execution produced - the same fields `subprocess.run` returns."""

    def __init__(
        self,
        returncode: int,
        stdout: str,
        stderr: str,
        duration_seconds: float,
    ) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.duration_seconds = duration_seconds


def pty_available() -> bool:
    """Whether a real pseudo-terminal can be used on this platform right now.

    POSIX ships one in the standard library. Windows needs `pywinpty` (ConPTY under the
    hood) - an optional dependency, so this checks for it rather than assuming either way.
    Callers that need a pty should still fall back to `run_captured` when this is False,
    since the dependency may simply not be installed.
    """
    if os.name == "nt":
        try:
            import winpty  # noqa: F401
            return True
        except Exception:
            return False
    try:
        import pty  # noqa: F401
        return True
    except Exception:
        return False


def _wait_or_stop(process: Any, timeout: float, stop: threading.Event) -> int:
    """`process.wait(timeout)`, except that setting `stop` kills the process and returns."""
    deadline = _now() + timeout
    while True:
        try:
            return process.wait(timeout=0.2)
        except subprocess.TimeoutExpired:
            if stop.is_set():
                from orchestrator.launcher import stop_tree

                stop_tree(process)
                return process.returncode if process.returncode is not None else -1
            if _now() >= deadline:
                raise subprocess.TimeoutExpired(getattr(process, "args", ""), timeout)


def run_captured(
    cmd: List[str],
    cwd: Optional[str] = None,
    timeout: int = 180,
    sink: Optional[Callable[[str], Any]] = None,
    env: Optional[Dict[str, str]] = None,
    stop: Optional[threading.Event] = None,
) -> CapturedResult:
    """Run a command, publishing its output as it arrives, and return what it produced.

    The contract is deliberately identical to the headless path in `launcher.run_agent_cli`:
    same arguments, same captured stdout and stderr, same timeout behaviour. Only the
    *liveness* is new - which is exactly the difference Roadmap §10.5 asked for, and nothing
    above this has to change to get it.

    `stop`, when given and set, ends the command early: it is killed and what it produced so far
    is returned. It exists for a display process whose owner decides when it is done (the native
    TUI's `attach`, which does not exit by itself when its server goes away).

    Raises:
        subprocess.TimeoutExpired: If the command outlives `timeout`. The caller translates
            this into the project's own CLITimeoutError, as it always has.
    """
    from orchestrator.process_jobs import finish_owned, spawn_owned

    started = _now()
    out_parts: List[str] = []
    err_parts: List[str] = []

    # Owned by a kill-on-close job: the agent's tree ends with the daemon, however it ends.
    process = spawn_owned(
        cmd,
        cwd=cwd or os.getcwd(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=env,
    )

    def pump(handle: Any, parts: List[str], prefix: str = "") -> None:
        try:
            while True:
                chunk = handle.read(READ_CHUNK)
                if not chunk:
                    break
                parts.append(chunk)
                if sink is not None:
                    try:
                        sink(prefix + chunk if prefix else chunk)
                    except Exception:
                        pass  # a panel that went away must not stop the agent
        except Exception:
            pass
        finally:
            try:
                handle.close()
            except Exception:
                pass

    threads = [
        threading.Thread(target=pump, args=(process.stdout, out_parts), daemon=True),
        threading.Thread(target=pump, args=(process.stderr, err_parts), daemon=True),
    ]
    for thread in threads:
        thread.start()

    try:
        # `_wait_or_stop` waits in short slices, so an interruption is acted on promptly; with
        # no `stop` event it is simply never set.
        returncode = _wait_or_stop(process, timeout, stop or threading.Event())
    except BaseException:
        # A timeout or an interruption: the agent's whole tree goes, and is reaped. Killing only
        # the direct child left its children running (measured - see `launcher.stop_tree`).
        from orchestrator.launcher import stop_tree

        stop_tree(process)
        for thread in threads:
            thread.join(timeout=1.0)
        raise
    for thread in threads:
        thread.join(timeout=5.0)
    # Anything the agent left running in the background ends with its execution.
    finish_owned(process)

    return CapturedResult(
        returncode=returncode,
        stdout="".join(out_parts),
        stderr="".join(err_parts),
        duration_seconds=round(_now() - started, 2),
    )


# ---------------------------------------------------------------------------
# Running a command attached to a real pseudo-terminal
# ---------------------------------------------------------------------------


class PtyUnavailableError(RuntimeError):
    """Raised by `run_captured_pty` when no pty implementation exists here.

    Callers should catch this and fall back to `run_captured` - a missing optional
    dependency must degrade a live view, never break the agent it would have shown.
    """


def run_captured_pty(
    cmd: List[str],
    cwd: Optional[str] = None,
    timeout: int = 180,
    sink: Optional[Callable[[str], Any]] = None,
    env: Optional[Dict[str, str]] = None,
    stop: Optional[threading.Event] = None,
) -> CapturedResult:
    """Run a command attached to a real pseudo-terminal, publishing output as it arrives.

    `stop` behaves as in `run_captured`: setting it ends the command and returns normally.

    Unlike `run_captured`, the child believes it is talking to an interactive terminal -
    which is what lets a program that only colours or formats its output for a human (an
    actual TUI, not a `--output-format json` print mode) do that here too. This is the
    mechanism Roadmap §10.5 asked for and AO's own design uses: attach the process to a
    pty instead of a plain pipe or a separate OS window, and relay its raw bytes.

    stdout and stderr are not distinguishable on a pty - the child sees one terminal, not
    two pipes - so `CapturedResult.stderr` is always empty here; everything is in `stdout`.

    Raises:
        PtyUnavailableError: No pty implementation is available on this platform (check
            `pty_available()` first to avoid this rather than catching it every time).
        subprocess.TimeoutExpired: The command outlived `timeout`.
    """
    if not pty_available():
        raise PtyUnavailableError(
            "no pseudo-terminal is available here - "
            "install pywinpty on Windows, or use run_captured instead"
        )
    if os.name == "nt":
        return _run_captured_pty_windows(cmd, cwd, timeout, sink, env, stop)
    return _run_captured_pty_posix(cmd, cwd, timeout, sink, env, stop)


def _run_captured_pty_windows(
    cmd: List[str],
    cwd: Optional[str],
    timeout: int,
    sink: Optional[Callable[[str], Any]],
    env: Optional[Dict[str, str]],
    stop: Optional[threading.Event] = None,
) -> CapturedResult:
    """The Windows half of `run_captured_pty`, via `pywinpty` (ConPTY).

    `PtyProcess.read()` has no timeout of its own and can block indefinitely waiting for
    the next chunk, so - exactly as `run_captured`'s plain pipes do - reading happens on a
    background thread and the timeout is enforced by how long the *main* thread waits, not
    by how long any one read call takes. An earlier version checked the deadline only
    between reads and could overshoot by an entire silent period (e.g. a child that pauses
    for several seconds produces nothing to read, so nothing gave the deadline a chance to
    fire until it returned).
    """
    import winpty

    started = _now()
    out_parts: List[str] = []
    done = threading.Event()

    from orchestrator.process_jobs import own_pid

    process = winpty.PtyProcess.spawn(cmd, cwd=cwd or os.getcwd(), env=env)
    # pywinpty creates the process itself, so it cannot be created suspended the way
    # `spawn_owned` does: it is owned from here on, and anything it starts from here on is too.
    # Closing the job (the `finally` below, or the kernel when this process dies) ends them all.
    job = own_pid(getattr(process, "pid", None))

    def _end() -> None:
        # pywinpty's own terminate first: it is what closes the pseudo console and so ends the
        # reader's blocking read. Ending the child through the job first left pywinpty looking at
        # an already-dead process, skipping that close, and the read blocked until its join
        # timeout (measured: 3.1s instead of 1.1s for a 1s timeout). The job then ends anything
        # the child started.
        try:
            process.terminate(force=True)
        except Exception:
            pass
        if job is not None:
            job.terminate()

    def pump() -> None:
        try:
            while True:
                try:
                    chunk = process.read(READ_CHUNK)
                except EOFError:
                    break
                except Exception:
                    break
                if not chunk:
                    break
                text = chunk if isinstance(chunk, str) else chunk.decode("utf-8", "replace")
                out_parts.append(text)
                if sink is not None:
                    try:
                        sink(text)
                    except Exception:
                        pass  # a panel that went away must not stop the agent
        finally:
            done.set()

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()

    try:
        if stop is None:
            timed_out = not done.wait(timeout=timeout)
        else:
            deadline = started + timeout
            while not done.wait(timeout=0.2) and not stop.is_set() and _now() < deadline:
                pass
            timed_out = not done.is_set() and not stop.is_set()
            if stop.is_set() and not done.is_set():
                _end()
                reader.join(timeout=2.0)
        if timed_out:
            _end()
            reader.join(timeout=2.0)
            raise subprocess.TimeoutExpired(cmd, timeout)

        try:
            process.wait()
        finally:
            try:
                if process.isalive():
                    process.terminate(force=True)
            except Exception:
                pass
        reader.join(timeout=5.0)

        returncode = process.exitstatus
        return CapturedResult(
            returncode=returncode if returncode is not None else -1,
            stdout="".join(out_parts),
            stderr="",
            duration_seconds=round(_now() - started, 2),
        )
    finally:
        # pywinpty holds a socket bridging its ConPTY pipes; closing it explicitly is what
        # avoids the interpreter warning about it (and the handle leak) that garbage
        # collection alone left behind.
        try:
            process.close()
        except Exception:
            pass
        if job is not None:
            job.close()


def _run_captured_pty_posix(
    cmd: List[str],
    cwd: Optional[str],
    timeout: int,
    sink: Optional[Callable[[str], Any]],
    env: Optional[Dict[str, str]],
    stop: Optional[threading.Event] = None,
) -> CapturedResult:
    """The POSIX half of `run_captured_pty`, via the standard library's `pty` module."""
    import errno
    import fcntl
    import pty as pty_module
    import select
    import struct
    import termios

    started = _now()
    out_parts: List[str] = []

    master_fd, slave_fd = pty_module.openpty()
    try:
        # 24x80 is `run_captured_pty`'s only opinion about the terminal it gives a child;
        # a program that queries size gets an answer instead of an error.
        fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
    except Exception:
        pass

    process = subprocess.Popen(
        cmd,
        cwd=cwd or os.getcwd(),
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        env=env,
        close_fds=True,
        start_new_session=True,
    )
    os.close(slave_fd)  # only the child needs the slave end

    deadline = started + timeout
    try:
        while True:
            remaining = deadline - _now()
            if stop is not None and stop.is_set():
                process.kill()
                break
            if remaining <= 0:
                process.kill()
                raise subprocess.TimeoutExpired(cmd, timeout)
            ready, _, _ = select.select([master_fd], [], [], min(remaining, 0.5))
            if master_fd in ready:
                try:
                    chunk = os.read(master_fd, READ_CHUNK)
                except OSError as exc:
                    if exc.errno == errno.EIO:
                        break  # the child closed its end of the pty - normal exit
                    raise
                if not chunk:
                    break
                text = chunk.decode("utf-8", "replace")
                out_parts.append(text)
                if sink is not None:
                    try:
                        sink(text)
                    except Exception:
                        pass  # a panel that went away must not stop the agent
            if process.poll() is not None and not ready:
                break
        returncode = process.wait(timeout=max(0.0, deadline - _now()) or None)
    finally:
        try:
            os.close(master_fd)
        except Exception:
            pass

    return CapturedResult(
        returncode=returncode,
        stdout="".join(out_parts),
        stderr="",
        duration_seconds=round(_now() - started, 2),
    )
