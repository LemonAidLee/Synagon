"""The daemon: a persistent local process the interface can drive (Roadmap Phase 8).

    python -m orchestrator --daemon        # persistent, on 127.0.0.1:8740

Everything ``--serve`` reads, plus three things a command that exits could never offer:

* **Control.** ``POST /api/control/start`` begins a delegated goal; ``cancel``, ``approve``,
  ``reject`` and ``deliver`` do from a click what ``--resume-goal``, ``--approve``,
  ``--reject`` and ``--deliver`` do from a terminal. Each one calls exactly the module the
  CLI calls.
* **A live stream.** ``GET /api/stream`` relays ``events.jsonl`` as server-sent events as they
  are appended, instead of making every watcher re-read the tail on a timer.
* **Supervision.** Several goals can be open at once, each a Job with a state a UI can show,
  and each cancellable.

Why this is allowed to write, when `serve.py` is not
----------------------------------------------------
Invariant 12 said watching is not steering, and for six phases the server could therefore
never write. Roadmap §10.6 revises *where* a deliberate action may be taken from, not whether
one is required. The rule the daemon holds itself to is narrower than "it's localhost":

* **Nothing acts on its own.** There is no scheduler, no webhook, no retry-until-it-passes.
  Every route below runs because a person pressed something this process itself served.
* **A per-launch token.** A loopback port that can start work is reachable by any page open
  in the same browser, so every ``/api/*`` route requires a token minted at launch and handed
  only to the UI this daemon opened. It is written to ``.orchestrator/daemon.json`` for local
  clients (that file is the handshake a script or the desktop shell reads), and never logged.
* **A same-origin check on every request.** A cross-origin ``fetch`` from a stray page carries
  an ``Origin`` header the browser sets and a page cannot forge; a request whose Origin is not
  this daemon's own is refused before it is read. Requiring the token in a *header* is what
  forces a browser to preflight, and the preflight is what it will fail.
* **Loopback only.** It binds ``127.0.0.1``, exactly as `serve.py` always has.

What it does not change
-----------------------
`graph.py`, `scheduler.py`, `workspace.py`, `delivery.py`, `store.py`, `goals.py`,
`approvals.py` and `status.py` are called, not modified. A daemon calling ``deliver()`` is
still a person's click reaching the only code that pushes; `budget.py`'s ceilings still bound
a run that a button started. The daemon is a new *caller* of the engine, never a new place
facts live.
"""

import json
import logging
import os
import secrets
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from orchestrator.approvals import (
    STATUS_APPROVED,
    STATUS_REJECTED,
)
from orchestrator.config import (
    get_approval_config,
    get_delegation_config,
    get_run_store_config,
)
from orchestrator.goals import goals_root, open_goal
from orchestrator.serve import _json_bytes
from orchestrator.store import runs_root
from orchestrator.terminals import TerminalRegistry

#: The daemon's own port. Deliberately not `serve.py`'s 8730: a read-only office and a daemon
#: are different things, and running both at once should not need an argument.
logger = logging.getLogger(__name__)

#: The daemon's own port. Deliberately not `serve.py`'s 8730: a read-only office and a daemon
#: are different things, and running both at once should not need an argument.
DEFAULT_DAEMON_PORT = 8740

#: The header the UI presents its token in. A *header* rather than only a query parameter on
#: purpose: a custom header is what makes a browser preflight a cross-origin request, and the
#: preflight is what a stray page fails.
TOKEN_HEADER = "X-Orchestrator-Token"

#: Where a local client (a script, the desktop shell) finds the running daemon.
HANDSHAKE_FILENAME = "daemon.json"

#: How many goals may be open at once. A ceiling, for the same reason `budget.py` has one:
#: a UI that can start work with a click can start work with a stuck click.
MAX_OPEN_JOBS = 8

#: How often the stream looks for new events, and how often it sends a keepalive comment.
STREAM_POLL_SECONDS = 0.4
STREAM_KEEPALIVE_SECONDS = 15.0

#: The largest control message the daemon will read. A goal is a sentence, not a payload.
MAX_CONTROL_BYTES = 64 * 1024

JOB_STARTING = "starting"
JOB_PLANNING = "planning"
JOB_RUNNING = "running"
JOB_FINISHED = "finished"
JOB_FAILED = "failed"
JOB_CANCELLED = "cancelled"

#: Job states that are over. A finished job stays in the list so the UI can show what happened.
JOB_TERMINAL = (JOB_FINISHED, JOB_FAILED, JOB_CANCELLED)


def utc_now_iso() -> str:
    """Current UTC time, as the stores write it."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------------------
# The token, and the handshake a local client reads it from
# ---------------------------------------------------------------------------


def mint_token() -> str:
    """A fresh token for one launch of the daemon.

    Per-launch, never reused across launches: restarting the daemon invalidates every page
    that was holding the old one, which is the behaviour you want from something that can
    start work.
    """
    return secrets.token_urlsafe(32)


def token_ok(provided: Optional[str], expected: Optional[str]) -> bool:
    """Compare a presented token with the daemon's, in constant time. Pure."""
    if not expected or not provided:
        return False
    return secrets.compare_digest(str(provided), str(expected))


def origin_ok(origin: Optional[str], port: int) -> bool:
    """Whether a request's `Origin` is this daemon's own page. Pure.

    A missing Origin is allowed: that is a script or a curl, which a browser's ambient
    authority does not reach and which had to know the token anyway. What is refused is an
    Origin that exists and is *someone else's* - the one case a browser can be tricked into.
    """
    if origin is None or not str(origin).strip():
        return True
    value = str(origin).strip().rstrip("/").lower()
    return value in (
        "http://127.0.0.1:%d" % int(port),
        "http://localhost:%d" % int(port),
        "http://[::1]:%d" % int(port),
    )


def handshake_path(project_root: str, directory: Optional[str] = None) -> Path:
    """Where the running daemon advertises itself to local clients."""
    base = Path(directory) if directory else Path(project_root) / ".orchestrator"
    if not base.is_absolute():
        base = Path(project_root) / base
    return base / HANDSHAKE_FILENAME


def write_handshake(
    project_root: str,
    port: int,
    token: str,
    directory: Optional[str] = None,
) -> Optional[Path]:
    """Record the port and token for local clients. Returns the path, or None if it failed.

    The file is the token's only durable home, so it is created with owner-only permissions
    where the platform honours them. A daemon that cannot write it still runs - the UI it
    opened has the token already - so this never raises.
    """
    path = handshake_path(project_root, directory)
    payload = {
        "pid": os.getpid(),
        "port": int(port),
        "token": token,
        "url": "http://127.0.0.1:%d/" % int(port),
        "project_root": str(project_root),
        "started_at": utc_now_iso(),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
        try:
            os.chmod(path, 0o600)
        except Exception:
            pass  # Windows honours this only partially; the file is still under the project
        return path
    except Exception:
        return None


def read_handshake(project_root: str, directory: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Read a running daemon's port and token, or None if there is no handshake."""
    path = handshake_path(project_root, directory)
    try:
        if not path.is_file():
            return None
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def clear_handshake(project_root: str, directory: Optional[str] = None) -> bool:
    """Remove the handshake when the daemon stops. Never raises."""
    try:
        handshake_path(project_root, directory).unlink()
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


class Job:
    """One piece of work the daemon was told to start, and what became of it.

    A Job is *not* a new place facts live: the goal it starts is recorded by `goals.py` and
    each session by `store.py`, exactly as ``--delegate`` records them. This holds only what
    is true of the supervision itself - what was asked, when, and whether it is still going.
    """

    def __init__(self, job_id: str, kind: str, goal: str, parallel: Optional[int] = None) -> None:
        self.id = job_id
        self.kind = kind
        self.goal = goal
        self.parallel = parallel
        self.state = JOB_STARTING
        self.goal_id: Optional[str] = None
        self.started_at = utc_now_iso()
        self.finished_at: Optional[str] = None
        self.error: Optional[str] = None
        self.summary: Optional[Dict[str, Any]] = None
        self.cancel = threading.Event()
        self.thread: Optional[threading.Thread] = None

    @property
    def done(self) -> bool:
        return self.state in JOB_TERMINAL

    def snapshot(self) -> Dict[str, Any]:
        """What a UI is allowed to see. Pure."""
        return {
            "job_id": self.id,
            "kind": self.kind,
            "goal": self.goal,
            "parallel": self.parallel,
            "state": self.state,
            "goal_id": self.goal_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "cancel_requested": self.cancel.is_set(),
            "summary": self.summary,
        }


class Daemon:
    """The supervisor: it holds jobs, and it is the only thing here that calls the engine.

    `goal_runner` is injected for the same reason `run_goal` takes a `session_runner`: the
    supervision has to be testable without running a pipeline. The default runs the real one.
    """

    def __init__(
        self,
        project_root: str,
        config: Any,
        config_path: Optional[str] = None,
        token: Optional[str] = None,
        goal_runner: Optional[Callable[["Daemon", Job], Dict[str, Any]]] = None,
        max_jobs: int = MAX_OPEN_JOBS,
        printer: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.project_root = str(project_root)
        self.config = config
        self.config_path = config_path
        self.token = token or mint_token()
        self.goal_runner = goal_runner or run_delegated_goal
        self.max_jobs = max(1, int(max_jobs))
        self.printer = printer or (lambda _line: None)
        self.started_at = utc_now_iso()
        self._jobs: Dict[str, Job] = {}
        self._order: List[str] = []
        self._lock = threading.Lock()
        self._jobs_version = 0
        #: Set when the daemon is shutting down, so live streams end instead of being cut.
        self.stopping = threading.Event()
        #: Live agent output (Phase 10). Empty until `capture_agent_output` is installed.
        self.terminals = TerminalRegistry()

    # -- reading -----------------------------------------------------------

    def board(self) -> Dict[str, Any]:
        """The board projection, read exactly as ``--board`` reads it."""
        from orchestrator.__main__ import read_board

        return read_board(self.project_root, self.config)

    def jobs(self) -> List[Dict[str, Any]]:
        """Every job this daemon has supervised, newest first."""
        with self._lock:
            return [self._jobs[job_id].snapshot() for job_id in reversed(self._order)]

    def jobs_version(self) -> int:
        """Bumped whenever a job's state changes, so a stream can notice cheaply."""
        with self._lock:
            return self._jobs_version

    def open_jobs(self) -> int:
        """How many jobs have not finished."""
        with self._lock:
            return sum(1 for job in self._jobs.values() if not job.done)

    def _touch(self) -> None:
        with self._lock:
            self._jobs_version += 1

    def set_state(self, job: Job, state: str, **fields: Any) -> None:
        """Move a job to a new state and let anything watching know."""
        job.state = state
        for key, value in fields.items():
            setattr(job, key, value)
        if state in JOB_TERMINAL and not job.finished_at:
            job.finished_at = utc_now_iso()
        self._touch()

    def cockpit(self) -> Dict[str, Any]:
        """Everything the cockpit draws, in one read (Roadmap Phase 9).

        One request rather than five: the page is a dashboard, and a dashboard assembled from
        five independently-timed polls shows five different moments at once.
        """
        from orchestrator.cockpit import columns, needs_you, ready_to_merge
        from orchestrator.serve import latest_activity
        from orchestrator.teams import (
            catalog_from_config,
            list_templates,
            team_from_config,
            validate_team,
        )

        from orchestrator.approvals import STATUS_PENDING, list_approvals

        team = team_from_config(self.config)
        activity = latest_activity(self.project_root, self.config)
        board = self.board()
        waiting = list_approvals(
            self.project_root,
            status=STATUS_PENDING,
            directory=get_approval_config(self.config).get("directory"),
        )

        return {
            "project_root": self.project_root,
            "approvals": waiting,
            "team": team,
            "catalog": catalog_from_config(self.config),
            "templates": list_templates(),
            "problems": validate_team(team, self.config),
            "columns": columns(
                team, activity.get("events") or [], self.terminals.live_by_agent()
            ),
            "activity": {
                key: value
                for key, value in activity.items()
                if key not in ("events", "goal_events")
            },
            "jobs": self.jobs(),
            "needs_you": needs_you(board),
            "ready": ready_to_merge(board),
            "counts": board.get("counts") or {},
            "max_jobs": self.max_jobs,
            "writable": True,
        }

    # -- the Explorer (Roadmap Phase 12) -----------------------------------
    #
    # Three thin passes through to `explorer.py`, which is where the confinement lives. They
    # are methods on the Daemon rather than free functions in the handler for one reason: the
    # project root and the config are what decide which roots exist, and those are the
    # daemon's, not the request's. A route may name a root id; it may never name a directory.

    def explore(
        self, root_id: str = "project", path: str = "", include_hidden: bool = False
    ) -> Dict[str, Any]:
        """One directory of one root, decorated with git state."""
        from orchestrator.explorer import snapshot

        return snapshot(
            self.project_root,
            self.config,
            root_id=root_id,
            path=path,
            include_hidden=include_hidden,
        )

    def open_file(self, root_id: str = "project", path: str = "") -> Dict[str, Any]:
        """One file's text, bounded, from one root."""
        from orchestrator.explorer import read_file, root_path

        base = root_path(self.project_root, self.config, root_id)
        if base is None:
            return {"error": "no root '%s'" % root_id}
        return read_file(base, path)

    def find_files(
        self, root_id: str = "project", query: str = "", include_hidden: bool = False
    ) -> Dict[str, Any]:
        """Files under one root whose name matches, for the palette's "go to file"."""
        from orchestrator.explorer import find_files, root_path

        base = root_path(self.project_root, self.config, root_id)
        if base is None:
            return {"error": "no root '%s'" % root_id}
        return find_files(base, query, include_hidden=include_hidden)

    def control(self, action: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Dispatch one control message. The whole of what a click can ask for.

        Deliberately a closed list. Anything not named here is not something the UI can do,
        which is what keeps "the daemon only acts on what it presented a button for" a
        property of the code rather than a promise in a docstring.
        """
        payload = payload if isinstance(payload, dict) else {}
        verb = str(action or "").strip()

        if verb == "start":
            return self.start_goal(
                str(payload.get("goal") or payload.get("task") or ""),
                payload.get("parallel"),
            )
        if verb == "cancel":
            return self.cancel_job(str(payload.get("job_id") or payload.get("id") or ""))
        if verb == "approve":
            return self.decide(
                str(payload.get("id") or payload.get("approval") or ""),
                STATUS_APPROVED,
                str(payload.get("note") or ""),
            )
        if verb == "reject":
            return self.decide(
                str(payload.get("id") or payload.get("approval") or ""),
                STATUS_REJECTED,
                str(payload.get("note") or ""),
            )
        if verb == "deliver":
            return self.deliver(str(payload.get("card") or payload.get("id") or ""))

        return {"ok": False, "unknown": True, "error": "no control action '%s'" % verb}

    # -- live agent output (Roadmap Phase 10) ------------------------------

    def capture_agent_output(self, enabled: bool = True) -> None:
        """Relay every agent's output into this daemon's terminal registry, or stop doing so.

        Installed on the one hook `launcher.py` exposes, so no adapter and nothing in the
        graph knows this happened. Turning it off restores exactly the execution path the CLI
        has always used.
        """
        from orchestrator import launcher

        if not enabled:
            launcher.set_output_recorder(None)
            return

        def recorder(agent=None, role=None, model=None, cmd=None, cwd=None):
            stream = self.terminals.open(
                agent=str(agent or ""),
                role=str(role or ""),
                model=model,
                command=list(cmd or []),
                cwd=str(cwd or ""),
            )
            self._touch()

            def sink(text: str) -> None:
                stream.append(text)

            sink.stream = stream  # type: ignore[attr-defined]
            return sink

        launcher.set_output_recorder(recorder)

    # -- control -----------------------------------------------------------

    def start_goal(self, goal: str, parallel: Optional[int] = None) -> Dict[str, Any]:
        """Begin a delegated goal in the background. The one route that starts work.

        Refuses rather than queues when the ceiling is reached: a UI that silently banks
        clicks would be a scheduler, and a scheduler is the thing invariant 12 exists to
        prevent.
        """
        text = str(goal or "").strip()
        if not text:
            return {"ok": False, "error": "a goal was expected"}
        if self.open_jobs() >= self.max_jobs:
            return {
                "ok": False,
                "error": "%d goals are already open; cancel one before starting another"
                % self.max_jobs,
            }

        try:
            lanes = int(parallel) if parallel is not None else None
        except (TypeError, ValueError):
            lanes = None

        job = Job(uuid.uuid4().hex[:12], "goal", text, lanes)
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
            self._jobs_version += 1

        def body() -> None:
            try:
                result = self.goal_runner(self, job) or {}
            except Exception as exc:  # a crashed goal must not take the daemon with it
                self.set_state(job, JOB_FAILED, error=str(exc))
                return
            if job.cancel.is_set():
                self.set_state(job, JOB_CANCELLED, summary=result.get("summary"))
            elif result.get("error"):
                self.set_state(
                    job, JOB_FAILED, error=str(result["error"]), summary=result.get("summary")
                )
            else:
                self.set_state(job, JOB_FINISHED, summary=result.get("summary"))

        job.thread = threading.Thread(target=body, name="goal-%s" % job.id, daemon=True)
        job.thread.start()
        return {"ok": True, "job": job.snapshot()}

    def cancel_job(self, job_id: str) -> Dict[str, Any]:
        """Ask a job to stop.

        Cooperative, and honestly so: an agent CLI already running is not killed, because
        half-killing a subprocess mid-write is how a workspace gets corrupted. What stops is
        the *authorisation of further work* - no task starts after this, which is the same
        thing `stop_on_failure` already does between waves.
        """
        job = self.find_job(job_id)
        if job is None:
            return {"ok": False, "error": "no job matching '%s'" % job_id}
        if job.done:
            return {"ok": False, "error": "that job already %s" % job.state, "job": job.snapshot()}
        job.cancel.set()
        self._touch()
        return {"ok": True, "job": job.snapshot()}

    def find_job(self, job_id: str) -> Optional[Job]:
        """Resolve a job id, or a unique prefix of one."""
        wanted = str(job_id or "").strip()
        if not wanted:
            return None
        with self._lock:
            if wanted in self._jobs:
                return self._jobs[wanted]
            matches = [job for key, job in self._jobs.items() if key.startswith(wanted)]
        return matches[0] if len(matches) == 1 else None

    def decide(self, target: str, decision: str, note: str = "") -> Dict[str, Any]:
        """Answer an approval gate, through the one implementation of that act.

        Since Roadmap 8.5 `--review` can answer a gate too, and two surfaces that each
        implemented "approve" would eventually implement it differently. `serve.answer_gate`
        is that implementation; this is the daemon's name for it.
        """
        from orchestrator.serve import answer_gate

        return answer_gate(self.project_root, self.config, target, decision, note)

    def deliver(self, card_id: str) -> Dict[str, Any]:
        """Push one card and open a pull request, through the only code that pushes.

        The board's own judgement is what gates this, not the daemon's: a card that is not
        Ready to Merge is refused here exactly as ``--deliver`` refuses it. Shared with
        `--review` for the reason `decide` is (Roadmap 8.5).
        """
        from orchestrator.serve import deliver_card_by_id

        return deliver_card_by_id(self.project_root, self.config, self.board(), card_id)


# ---------------------------------------------------------------------------
# Running a goal, the way `--delegate` runs one
# ---------------------------------------------------------------------------


def run_delegated_goal(daemon: Daemon, job: Job) -> Dict[str, Any]:
    """Plan a goal and run it, calling exactly what the CLI's ``--delegate`` path calls.

    Nothing about the pipeline is different because a click started it: the same planner, the
    same scheduler, the same budget, the same isolation. The only addition is the cancel
    check, which refuses to authorise a session rather than interrupting one.
    """
    from orchestrator.decompose import plan_is_usable
    from orchestrator.graph import build_graph, build_plan_graph
    from orchestrator.scheduler import run_goal
    from orchestrator.workspace import head_commit

    project_root = daemon.project_root
    config = daemon.config
    delegation = get_delegation_config(config)

    store = open_goal(
        project_root,
        job.goal,
        base_ref=head_commit(project_root),
        directory=delegation.get("goals_directory"),
    )
    if store is not None:
        job.goal_id = store.goal_id
    daemon.set_state(job, JOB_PLANNING)

    # Planning only reads, so it takes no branch of its own - the same override ``--delegate``
    # applies before it plans.
    plan_config = dict(config)
    plan_config["workspace"] = dict(plan_config.get("workspace") or {})
    plan_config["workspace"]["isolation"] = "none"

    plan_state: Dict[str, Any] = {
        "task": job.goal,
        "project_root": project_root,
        "config": plan_config,
        "config_path": daemon.config_path,
        "agent_results": [],
        "plan_only": True,
    }
    if job.goal_id:
        plan_state["goal_id"] = job.goal_id

    plan_result = build_plan_graph(plan_config).invoke(plan_state)
    if plan_result.get("error"):
        return {"error": str(plan_result["error"])}

    plan = plan_result.get("task_plan") or {}
    if not plan_is_usable(plan):
        return {"error": str(plan.get("error") or "the goal could not be decomposed")}
    if store is not None:
        store.record_plan(plan)

    if job.cancel.is_set():
        return {"summary": None}

    session_graph = build_graph(config)

    def session_runner(state: Dict[str, Any]) -> Dict[str, Any]:
        if job.cancel.is_set():
            return {"error": "cancelled before this task started"}
        return session_graph.invoke(state)

    daemon.set_state(job, JOB_RUNNING)
    summary = run_goal(
        goal=job.goal,
        plan=plan,
        project_root=project_root,
        config=config,
        session_runner=session_runner,
        max_parallel=job.parallel,
        goal_store=store,
        session_defaults={"config_path": daemon.config_path},
    )
    return {"summary": summary}


# ---------------------------------------------------------------------------
# The live stream
# ---------------------------------------------------------------------------


class EventTail:
    """Follow one append-only event log, returning only what is new.

    A byte offset rather than a re-read: the office's poll loop re-parsed the last 200 lines
    every 900ms, which is fine for one page and wrong as a foundation. This reads what was
    appended since last time, and resets if the file it was following was replaced.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.offset = 0

    def read_new(self) -> List[Dict[str, Any]]:
        """Every complete event appended since the last call. Never raises."""
        try:
            if not self.path.is_file():
                return []
            size = self.path.stat().st_size
            if size < self.offset:
                self.offset = 0  # the log was rotated or replaced
            if size == self.offset:
                return []
            with open(self.path, "r", encoding="utf-8", errors="replace") as handle:
                handle.seek(self.offset)
                chunk = handle.read()
                self.offset = handle.tell()
        except Exception as exc:
            logger.warning("could not read event tail %s: %s", self.path, exc)
            return []

        events: List[Dict[str, Any]] = []
        for line in chunk.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return events

    def seek_end(self) -> None:
        """Start from now, ignoring what is already in the log."""
        try:
            self.offset = self.path.stat().st_size if self.path.is_file() else 0
        except Exception:
            self.offset = 0


def newest_run_dir(project_root: str, config: Any) -> Optional[Path]:
    """The run directory the stream follows when it was not told which one. Never raises."""
    root = runs_root(project_root, get_run_store_config(config).get("directory"))
    try:
        if not root.is_dir():
            return None
        directories = [d for d in root.iterdir() if d.is_dir()]
        return max(directories, key=lambda d: d.name) if directories else None
    except Exception as exc:
        logger.warning("could not list run directories under %s: %s", root, exc)
        return None


def run_dir_for(project_root: str, config: Any, run_id: Optional[str]) -> Optional[Path]:
    """Resolve a run id, or a unique prefix, to its directory; newest when none was named."""
    if not run_id or str(run_id) == "latest":
        return newest_run_dir(project_root, config)
    root = runs_root(project_root, get_run_store_config(config).get("directory"))
    candidate = root / str(run_id)
    if candidate.is_dir():
        return candidate
    try:
        matches = [d for d in root.iterdir() if d.is_dir() and d.name.startswith(str(run_id))]
    except Exception as exc:
        logger.warning("could not resolve run '%s' under %s: %s", run_id, root, exc)
        return None
    return matches[0] if len(matches) == 1 else None


def read_run_meta(run_dir: Optional[Path]) -> Dict[str, Any]:
    """A run's `run.json`, or an empty dict. Never raises."""
    if run_dir is None:
        return {}
    try:
        meta_file = run_dir / "run.json"
        if not meta_file.is_file():
            return {}
        with open(meta_file, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        return payload if isinstance(payload, dict) else {}
    except Exception as exc:
        logger.warning("could not read run metadata at %s: %s", meta_file, exc)
        return {}


def goal_events_path(project_root: str, config: Any, goal_id: Optional[str]) -> Optional[Path]:
    """Where a goal's own event log lives, if the run belongs to one."""
    if not goal_id:
        return None
    directory = get_delegation_config(config).get("goals_directory")
    return goals_root(project_root, directory) / str(goal_id) / "events.jsonl"


def sse_frame(event: str, payload: Any) -> bytes:
    """One server-sent-events message. Pure."""
    body = json.dumps(payload, default=str)
    return ("event: %s\ndata: %s\n\n" % (event, body)).encode("utf-8")


# ---------------------------------------------------------------------------
# The HTTP surface
# ---------------------------------------------------------------------------


#: Substituted into every page the daemon serves, so the UI never has to be handed its token
#: through a URL (which would put it in history, in the title bar, and in any referrer).
#: A page served by `serve.py` keeps the placeholder, and reads that as "no daemon here".
TOKEN_PLACEHOLDER = "__ORCHESTRATOR_TOKEN__"

#: Third-party files served from `web/vendor/`, name -> content type. An explicit list, so
#: adding a library is a deliberate edit here rather than a consequence of dropping a file in
#: a directory. Vendored rather than fetched from a CDN because the daemon is a loopback
#: process: a page that needs the internet to render is a page that breaks on a train.
VENDORED_FILES = {
    "gsap.min.js": "text/javascript; charset=utf-8",
}


def make_daemon_handler(daemon: "Daemon", port: int):
    """Build the daemon's request handler on top of the read-only one.

    `serve.py`'s handler is subclassed rather than copied: every read route the office and the
    design surface already use keeps one definition, and what is added here is only the part
    that is new - authentication, control, and the stream.
    """
    from orchestrator.serve import INITIAL_EVENT_WINDOW, WEB_DIR, make_handler

    base = make_handler(
        daemon.project_root,
        daemon.config,
        daemon.board,
        writable=True,
        config_path=daemon.config_path,
        on_team_saved=None,
    )

    class DaemonHandler(base):  # type: ignore[misc,valid-type]
        server_version = "orchestrator-daemon"
        protocol_version = "HTTP/1.0"  # every response closes; the stream depends on it

        # -- authentication ------------------------------------------------

        def _authorised(self) -> bool:
            """Every `/api/*` request needs the token and a same-origin (or absent) Origin.

            Refusing here, before a body is read, is deliberate: a request that cannot be
            trusted should not get as far as being parsed.
            """
            if not origin_ok(self.headers.get("Origin"), port):
                self._send(
                    _json_bytes({"error": "cross-origin requests are refused"}),
                    "application/json",
                    status=403,
                )
                return False

            presented = self.headers.get(TOKEN_HEADER)
            if not presented:
                query = parse_qs(urlparse(self.path).query)
                presented = (query.get("token") or [None])[0]
            if not token_ok(presented, daemon.token):
                self._send(
                    _json_bytes(
                        {
                            "error": "this daemon needs its launch token. "
                            "Open the page the daemon printed, or read "
                            ".orchestrator/daemon.json."
                        }
                    ),
                    "application/json",
                    status=401,
                )
                return False
            return True

        def _page(self, filename: str) -> None:
            """Serve a page with this launch's token substituted in.

            The token reaches the UI in the document the daemon itself served, which only a
            same-origin script can read back. Nothing has to put it in a URL.
            """
            try:
                text = (WEB_DIR / filename).read_text(encoding="utf-8")
            except Exception as exc:
                self._send(
                    ("Could not read %s: %s" % (filename, exc)).encode("utf-8"),
                    "text/plain; charset=utf-8",
                    status=500,
                )
                return
            body = text.replace(TOKEN_PLACEHOLDER, daemon.token).encode("utf-8")
            self._send(body, "text/html; charset=utf-8")

        def _vendor(self, name: str) -> None:
            """Serve one vendored library by name, from a fixed list.

            An allow-list rather than a path join, because a path join under a directory is
            how a static-file server becomes a file-disclosure bug. `explorer.py` is the one
            place in this project allowed to turn a request into a path, and it is confined;
            this is not that, and does not try to be.
            """
            if name not in VENDORED_FILES:
                self._send(b"Not found", "text/plain; charset=utf-8", status=404)
                return
            try:
                body = (WEB_DIR / "vendor" / name).read_bytes()
            except Exception:
                self._send(b"Not found", "text/plain; charset=utf-8", status=404)
                return
            self._send(body, VENDORED_FILES[name])

        # -- reading -------------------------------------------------------

        def do_GET(self) -> None:  # noqa: N802
            route = urlparse(self.path).path

            if route in ("/", "/index.html", "/cockpit", "/cockpit.html"):
                self._page("cockpit.html")
                return
            if route in ("/office", "/office.html"):
                self._page("office.html")
                return
            if route in ("/design", "/design.html"):
                self._page("design.html")
                return

            # Vendored libraries, served from disk rather than from a CDN. This is the whole
            # of what makes a third-party animation engine allowable here: the daemon is a
            # loopback process, and a page that fetches a script over the internet renders
            # broken with the network unplugged. The route is a fixed allow-list of file
            # names, not a static-file server - `web/vendor/` is not browsable, and nothing
            # outside it is reachable by naming it.
            if route.startswith("/vendor/"):
                self._vendor(route[len("/vendor/"):])
                return

            if not route.startswith("/api/"):
                self._send(b"Not found", "text/plain; charset=utf-8", status=404)
                return

            if not self._authorised():
                return

            if route == "/api/daemon":
                self._send(
                    _json_bytes(
                        {
                            "ok": True,
                            "project_root": daemon.project_root,
                            "started_at": daemon.started_at,
                            "port": port,
                            "max_jobs": daemon.max_jobs,
                            "open_jobs": daemon.open_jobs(),
                            "jobs": daemon.jobs(),
                        }
                    ),
                    "application/json",
                )
                return

            if route == "/api/jobs":
                self._send(_json_bytes({"jobs": daemon.jobs()}), "application/json")
                return

            if route == "/api/cockpit":
                try:
                    self._send(_json_bytes(daemon.cockpit()), "application/json")
                except Exception as exc:
                    self._send(_json_bytes({"error": str(exc)}), "application/json", status=500)
                return

            if route == "/api/terminals":
                self._send(
                    _json_bytes({"terminals": daemon.terminals.listing()}), "application/json"
                )
                return

            if route == "/api/terminal":
                query = parse_qs(urlparse(self.path).query)
                term_id = (query.get("id") or [""])[0]
                try:
                    since = int((query.get("since") or ["0"])[0])
                except ValueError:
                    since = 0
                payload = daemon.terminals.read_since(term_id, since)
                if payload is None:
                    self._send(
                        _json_bytes({"error": "no terminal '%s'" % term_id}),
                        "application/json",
                        status=404,
                    )
                    return
                self._send(_json_bytes(payload), "application/json")
                return

            # -- the Explorer (Phase 12). GET only, and that is the point: browsing is
            # watching, and `explorer.py` holds no write for a POST to reach even if one
            # were added here by mistake.
            if route == "/api/explorer":
                query = parse_qs(urlparse(self.path).query)
                self._send(
                    _json_bytes(
                        daemon.explore(
                            root_id=(query.get("root") or ["project"])[0],
                            path=(query.get("path") or [""])[0],
                            include_hidden=(query.get("hidden") or [""])[0] in ("1", "true"),
                        )
                    ),
                    "application/json",
                )
                return

            if route == "/api/file":
                query = parse_qs(urlparse(self.path).query)
                payload = daemon.open_file(
                    root_id=(query.get("root") or ["project"])[0],
                    path=(query.get("path") or [""])[0],
                )
                self._send(
                    _json_bytes(payload),
                    "application/json",
                    status=404 if payload.get("error") else 200,
                )
                return

            if route == "/api/find":
                query = parse_qs(urlparse(self.path).query)
                self._send(
                    _json_bytes(
                        daemon.find_files(
                            root_id=(query.get("root") or ["project"])[0],
                            query=(query.get("q") or [""])[0],
                            include_hidden=(query.get("hidden") or [""])[0] in ("1", "true"),
                        )
                    ),
                    "application/json",
                )
                return

            if route == "/api/stream":
                self._stream()
                return

            base.do_GET(self)  # the read routes serve.py already defines

        # -- the live stream -----------------------------------------------

        def _stream(self) -> None:
            """Relay the event log, and job state, as server-sent events until disconnected.

            This is the capability §10.6 asked for: a watcher is *told* what happened instead
            of asking for the tail on a timer. What it relays is exactly what is already on
            disk - the stream is another reader of `events.jsonl`, not a second source of
            truth for it.
            """
            query = parse_qs(urlparse(self.path).query)
            wanted_run = (query.get("run") or [""])[0] or None
            since_raw = (query.get("since") or [""])[0]
            try:
                since = int(since_raw) if since_raw else -1
            except ValueError:
                since = -1
            follow_latest = not wanted_run or wanted_run == "latest"

            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
            except Exception:
                return

            def emit(event: str, payload: Any) -> bool:
                try:
                    self.wfile.write(sse_frame(event, payload))
                    self.wfile.flush()
                    return True
                except Exception:
                    return False  # the page navigated away

            run_dir = run_dir_for(daemon.project_root, daemon.config, wanted_run)
            meta = read_run_meta(run_dir)
            run_tail = EventTail((run_dir / "events.jsonl") if run_dir else Path("."))
            goal_tail: Optional[EventTail] = None
            goal_path = goal_events_path(daemon.project_root, daemon.config, meta.get("goal_id"))
            if goal_path:
                goal_tail = EventTail(goal_path)

            if not emit("hello", self._activity(run_dir, meta)):
                return

            # The replay is read from offset zero and then filtered, rather than tailed and
            # then sought: anything appended between the two would otherwise be lost exactly
            # when a run is busiest.
            backlog = [
                event
                for event in run_tail.read_new()
                if int(event.get("sequence") or 0) > since
            ]
            for event in backlog[-INITIAL_EVENT_WINDOW:]:
                if not emit("orchestrator", event):
                    return
            if goal_tail is not None:
                for event in goal_tail.read_new()[-INITIAL_EVENT_WINDOW:]:
                    if not emit("goal", event):
                        return

            jobs_version = -1
            last_keepalive = time.time()

            while not daemon.stopping.is_set():
                if follow_latest:
                    newest = newest_run_dir(daemon.project_root, daemon.config)
                    if newest is not None and (run_dir is None or newest != run_dir):
                        run_dir = newest
                        meta = read_run_meta(run_dir)
                        run_tail = EventTail(run_dir / "events.jsonl")
                        goal_path = goal_events_path(
                            daemon.project_root, daemon.config, meta.get("goal_id")
                        )
                        goal_tail = EventTail(goal_path) if goal_path else None
                        if not emit("activity", self._activity(run_dir, meta)):
                            return

                sent_anything = False
                for event in run_tail.read_new():
                    if not emit("orchestrator", event):
                        return
                    sent_anything = True
                if goal_tail is not None:
                    for event in goal_tail.read_new():
                        if not emit("goal", event):
                            return
                        sent_anything = True

                version = daemon.jobs_version()
                if version != jobs_version:
                    jobs_version = version
                    if not emit("jobs", {"jobs": daemon.jobs()}):
                        return
                    sent_anything = True

                now = time.time()
                if sent_anything:
                    last_keepalive = now
                elif now - last_keepalive >= STREAM_KEEPALIVE_SECONDS:
                    last_keepalive = now
                    try:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                    except Exception:
                        return

                time.sleep(STREAM_POLL_SECONDS)

        def _activity(self, run_dir: Optional[Path], meta: Dict[str, Any]) -> Dict[str, Any]:
            """What the office's `/api/activity` says, without the events it re-reads."""
            return {
                "run_id": meta.get("run_id") or (run_dir.name if run_dir else None),
                "task": meta.get("task"),
                "status": meta.get("status"),
                "goal_id": meta.get("goal_id"),
                "task_id": meta.get("task_id"),
                "agents": meta.get("agents") or [],
                "jobs": daemon.jobs(),
            }

        # -- control -------------------------------------------------------

        def do_POST(self) -> None:  # noqa: N802
            """The routes that act. Each one is a person's click, and calls the CLI's code."""
            route = urlparse(self.path).path

            if not route.startswith("/api/"):
                self._send(
                    _json_bytes({"error": "no such route"}), "application/json", status=404
                )
                return
            if not self._authorised():
                return

            if route == "/api/team":
                base.do_POST(self)  # the design surface's write, unchanged
                return

            if not route.startswith("/api/control/"):
                self._send(
                    _json_bytes({"error": "no such route"}), "application/json", status=404
                )
                return

            payload = self._read_json()
            if payload is None:
                return

            action = route[len("/api/control/"):]
            try:
                result = daemon.control(action, payload)
            except Exception as exc:
                self._send(_json_bytes({"ok": False, "error": str(exc)}), "application/json", 500)
                return

            status = 200 if result.get("ok") else 400
            if result.get("unknown"):
                status = 404
            self._send(_json_bytes(result), "application/json", status=status)

        def _read_json(self) -> Optional[Dict[str, Any]]:
            """Read a control message, or answer the client and return None."""
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length < 0 or length > MAX_CONTROL_BYTES:
                self._send(
                    _json_bytes({"ok": False, "error": "that message is too large"}),
                    "application/json",
                    status=400,
                )
                return None
            if length == 0:
                return {}
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8"))
            except Exception as exc:
                self._send(
                    _json_bytes({"ok": False, "error": "could not read the request: %s" % exc}),
                    "application/json",
                    status=400,
                )
                return None
            return body if isinstance(body, dict) else {}

    return DaemonHandler


def serve_daemon(
    project_root: str,
    config: Any,
    port: int = DEFAULT_DAEMON_PORT,
    open_browser: bool = True,
    printer: Optional[Callable[[str], None]] = None,
    serve_forever: bool = True,
    config_path: Optional[str] = None,
    token: Optional[str] = None,
    daemon: Optional[Daemon] = None,
) -> "ThreadingHTTPServer":
    """Run the daemon until interrupted.

    Args:
        project_root: The project directory this daemon is opened on.
        config: The resolved configuration.
        port: Port to bind on 127.0.0.1. 0 asks the OS for a free one, which is what tests use.
        open_browser: Open the cockpit automatically.
        printer: Where to print the banner; defaults to `print`.
        serve_forever: False returns the server without blocking, for tests.
        config_path: The config file the team editor writes.
        token: A token to use instead of minting one. For tests.
        daemon: A pre-built Daemon, so a caller can inject a goal runner.

    Returns:
        The running server, with `.daemon` attached.
    """
    import webbrowser
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    say = printer or print
    supervisor = daemon or Daemon(
        project_root, config, config_path=config_path, token=token, printer=say
    )

    # Bound before the handler is built, and only then activated: the handler needs the
    # port to judge an Origin, and no request may arrive before it knows one.
    httpd = ThreadingHTTPServer(
        ("127.0.0.1", int(port)), BaseHTTPRequestHandler, bind_and_activate=False
    )
    httpd.server_bind()
    bound_port = httpd.server_address[1]
    httpd.RequestHandlerClass = make_daemon_handler(supervisor, bound_port)
    httpd.supervisor = supervisor  # type: ignore[attr-defined]
    httpd.server_activate()

    # Agent output is relayed from the moment the daemon is up, not from the moment a panel
    # is opened: a terminal you can only see if you were already watching is not much of one.
    supervisor.capture_agent_output(True)

    url = "http://127.0.0.1:%d/" % bound_port
    handshake = write_handshake(project_root, bound_port, supervisor.token)

    say("")
    say("  The cockpit is open at %s" % url)
    say("  This daemon can start, cancel, approve and deliver work - and only when you")
    say("  press something on the page it served. Nothing here runs on a timer.")
    if handshake:
        say("  A local client reads the port and token from %s" % handshake)
    say("  Ctrl-C to stop.")
    say("")

    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()

    if not serve_forever:
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        return httpd

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        raise
    finally:
        supervisor.stopping.set()
        supervisor.capture_agent_output(False)
        supervisor.terminals.close_all()
        clear_handshake(project_root)
        httpd.server_close()
    return httpd
