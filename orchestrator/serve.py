"""The local board server, the office, the design surface, and review (Roadmap 4, 7, 8.5).

    python -m orchestrator --serve     # read-only: the board and the office
    python -m orchestrator --design    # the same, plus the team editor, which can save
    python -m orchestrator --review    # the same, plus answering a gate and delivering a card

Serves on ``127.0.0.1``:

* the **board** — the same projection `--board` prints, as JSON,
* the **office** — a page where each role is a small character at a desk, walking, working, and
  handing off to the next role as a run proceeds, and
* the **design surface** (Phase 7) — a node editor for the team, which is a view over
  `orchestrator.yaml` because that file already *is* the team model.

Why this is safe to build now
-----------------------------
The office is a *reader*. It watches the event log and the stores; it never writes to them,
never starts a run, and never answers a gate. That is the whole reason it can be added without
touching the engine: every state it shows is already derived (`status.py`, `board.py`) and
already recorded (`store.py`, `goals.py`).

Load-bearing rules
------------------
* **Loopback only.** The server binds `127.0.0.1`. This is a window onto a developer's machine,
  not a service.
* **Read-only about runs, always.** Every route that touches a run, a goal, an approval or a
  delivery is a GET that projects stored facts. Nothing here can start work, answer a gate, or
  push a branch — those stay deliberate commands, not buttons a stray click can press.
* **Three modes, and a mode is never a relaxation of another.** `--serve` is read-only.
  `--design` adds `POST /api/team` and nothing else, so the office keeps the read-only
  guarantee it was built with; a write is validated before it lands and leaves a backup
  behind (see `teams.py`). `--review` (Roadmap 8.5) adds `POST /api/review/*` and nothing
  else — the only routes in this file that can answer a gate or deliver a card. Each flag
  turns on exactly its own routes: `--serve` has neither, `--design` cannot review, and
  `--review` cannot edit the team. A stray click on a server someone opened to *watch* a run
  must never be able to become a decision, so the capability has to be asked for by name.
* **A review acts through the code the CLI acts through.** `answer_gate` and `deliver_card_by_id`
  call `approvals.resolve_approval` and `__main__.deliver_card` — the same functions
  `--approve` and `--deliver` call, and the same ones the daemon's control verbs call. There
  is one implementation of "approve", not three.
* **No network dependencies.** The page is one self-contained HTML file with no CDN, no build
  step, and no framework, so the office works on a machine that is offline. The characters are
  drawn on a 2D canvas; swapping that renderer for a 3D one later changes nothing behind it,
  because the page consumes a projection rather than the engine.
* **It must not slow a run down.** Polling reads files that are already being appended to;
  nothing here holds a lock the orchestrator needs.
"""

import json
import os
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

from orchestrator.config import (
    get_delegation_config,
    get_delivery_config,
    get_run_store_config,
)
from orchestrator.delivery import list_deliveries
from orchestrator.goals import goals_root, list_goals, load_goal, task_outcomes
from orchestrator.stats import collect_runs, compute_stats, evidence_for_choices
from orchestrator.status import derive_task_state
from orchestrator.store import runs_root
from orchestrator.teams import (
    catalog_from_config,
    list_templates,
    normalize_team,
    team_from_config,
    validate_team,
    write_team,
)

WEB_DIR = Path(__file__).parent / "web"

#: How many recent events the office replays when a browser first connects.
INITIAL_EVENT_WINDOW = 200

#: How many sessions the office will draw at once. `--parallel N` is bounded by config; this
#: is the office's own ceiling, so a goal with forty tasks does not become forty desks.
MAX_SESSIONS = 8

#: The largest review message the server will read. A decision is an id and a note.
MAX_REVIEW_BYTES = 16 * 1024

#: How many of each session's events travel with it. Smaller than the single-run window
#: because a goal sends several at a time, and the office only replays them to catch up.
SESSION_EVENT_WINDOW = 60

#: How many runs the evidence route reads. `--stats` reads everything on purpose; a route a
#: page calls on load must be bounded, and the newest runs are the ones a choice is about.
EVIDENCE_RUN_WINDOW = 200

#: How long a computed evidence payload is reused before the runs are read again. Cheap
#: insurance against a page that re-renders on every event turning into a disk walk per frame.
EVIDENCE_TTL_SECONDS = 30.0


def _tail_events(path: Path, limit: int) -> List[Dict[str, Any]]:
    """Read the last `limit` JSON lines of an event log. Never raises."""
    events: List[Dict[str, Any]] = []
    try:
        if not path.is_file():
            return []
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()
    except Exception:
        return []

    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def latest_activity(
    project_root: str,
    config: Any,
    limit: int = INITIAL_EVENT_WINDOW,
) -> Dict[str, Any]:
    """Return the most recent run's events, which goal it belongs to, and that goal's sessions.

    The office animates whatever is happening *now*, which is the newest run directory. When
    nothing is running that is simply the last thing that did — an office that goes blank the
    moment work finishes would hide the result you were waiting for.

    Since Roadmap 8.1 the newest run is only how the *goal* is found. When one is found, every
    session that goal started travels alongside it under ``sessions``, so a watcher can draw a
    team rather than whichever desk wrote last. A run that belongs to no goal reports itself as
    a single session, which is why nothing that consumed this payload before has to change.
    """
    store_cfg = get_run_store_config(config)
    delegation_cfg = get_delegation_config(config)

    root = runs_root(project_root, store_cfg.get("directory"))
    newest: Optional[Path] = None
    try:
        if root.is_dir():
            directories = [d for d in root.iterdir() if d.is_dir()]
            newest = max(directories, key=lambda d: d.name) if directories else None
    except Exception:
        newest = None

    events = _tail_events(newest / "events.jsonl", limit) if newest else []

    meta: Dict[str, Any] = {}
    if newest:
        try:
            meta_file = newest / "run.json"
            if meta_file.is_file():
                with open(meta_file, "r", encoding="utf-8") as handle:
                    meta = json.load(handle)
        except Exception:
            meta = {}

    goal_events: List[Dict[str, Any]] = []
    goal_id = meta.get("goal_id")
    if goal_id:
        goal_dir = goals_root(project_root, delegation_cfg.get("goals_directory")) / str(goal_id)
        goal_events = _tail_events(goal_dir / "events.jsonl", limit)

    sessions = goal_sessions(project_root, config, goal_id)
    if not sessions and newest:
        # No goal, or a goal whose log names no run yet: the run in hand is the whole team.
        sessions = [
            {
                "task_id": meta.get("task_id"),
                "run_id": meta.get("run_id") or newest.name,
                "task": meta.get("task"),
                "state": None,
                "status": meta.get("status"),
                "agents": meta.get("agents") or [],
                "events": events,
                "order": 0,
            }
        ]

    return {
        "run_id": meta.get("run_id") or (newest.name if newest else None),
        "task": meta.get("task"),
        "status": meta.get("status"),
        "goal_id": goal_id,
        "task_id": meta.get("task_id"),
        "sessions": sessions,
        "agents": meta.get("agents") or [],
        "events": events,
        "goal_events": goal_events,
    }


def _run_meta(run_dir: Path) -> Dict[str, Any]:
    """Read one run's `run.json`, or an empty dict. Never raises."""
    try:
        meta_file = run_dir / "run.json"
        if meta_file.is_file():
            with open(meta_file, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
                return loaded if isinstance(loaded, dict) else {}
    except Exception:
        pass
    return {}


def goal_sessions(
    project_root: str,
    config: Any,
    goal_id: Optional[str],
    limit: int = SESSION_EVENT_WINDOW,
) -> List[Dict[str, Any]]:
    """One entry per session a goal started, each with its own events (Roadmap 8.1).

    The office was built against `latest_activity`, which reads the *newest run directory* -
    so under `--parallel 3` it showed one desk and switched it to whichever session had
    written most recently. The board never had this problem because it is a projection across
    every goal and session; the office simply had no such projection to consume. This is it.

    Nothing new is recorded to build it. A goal's log already says which run each task was
    given (`task_outcomes`), and each of those runs already has a log of its own; this reads
    both and pairs them, which is invariant 1 one level up.

    Sessions come back in the order the goal started them, so a desk does not move when a
    sibling finishes. Live sessions sort ahead of finished ones only when there are more than
    `MAX_SESSIONS` of them, because then something has to be dropped and it should not be the
    work still running.
    """
    if not goal_id:
        return []

    delegation_cfg = get_delegation_config(config)
    store_cfg = get_run_store_config(config)
    goal = load_goal(project_root, str(goal_id), directory=delegation_cfg.get("goals_directory"))
    if not goal:
        return []

    outcomes = task_outcomes(goal)
    root = runs_root(project_root, store_cfg.get("directory"))

    sessions: List[Dict[str, Any]] = []
    for order, (task_id, outcome) in enumerate(outcomes.items()):
        run_id = outcome.get("run_id")
        state = derive_task_state(outcome)
        if not run_id:
            # A task that was skipped or is still queued has no session to draw. It is still
            # worth naming, so the office can show an empty desk rather than pretend the task
            # does not exist.
            sessions.append(
                {
                    "task_id": task_id,
                    "run_id": None,
                    "task": outcome.get("title") or task_id,
                    "state": state,
                    "status": None,
                    "agents": [],
                    "events": [],
                    "order": order,
                }
            )
            continue

        run_dir = root / str(run_id)
        meta = _run_meta(run_dir)
        sessions.append(
            {
                "task_id": task_id,
                "run_id": str(run_id),
                "task": meta.get("task") or outcome.get("title") or task_id,
                "state": state,
                "status": meta.get("status"),
                "agents": meta.get("agents") or [],
                "events": _tail_events(run_dir / "events.jsonl", limit),
                "order": order,
            }
        )

    if len(sessions) > MAX_SESSIONS:
        sessions.sort(key=lambda s: (s.get("status") is not None, s["order"]))
        sessions = sessions[:MAX_SESSIONS]
        sessions.sort(key=lambda s: s["order"])
    return sessions


def evidence(
    project_root: str,
    config: Optional[Dict[str, Any]],
    _cache: Dict[str, Any] = {},
) -> Dict[str, Any]:
    """The `--stats` projection, keyed for a dropdown and cached briefly (Roadmap 8.3).

    `--stats` exists to answer "is this verifier worth what it costs" and, until now, could
    only answer it in a report nobody had open while choosing. This is the same computation
    behind a route, so the number can sit next to the choice it is about.
    """
    import time

    now = time.time()
    if _cache.get("root") == project_root and (now - _cache.get("at", 0.0)) < EVIDENCE_TTL_SECONDS:
        return _cache["payload"]

    directory = get_run_store_config(config or {}).get("directory")
    runs, unreadable = collect_runs(project_root, limit=EVIDENCE_RUN_WINDOW, directory=directory)
    payload = evidence_for_choices(compute_stats(runs, unreadable))
    payload["window"] = EVIDENCE_RUN_WINDOW
    _cache.update({"root": project_root, "at": now, "payload": payload})
    return payload


def _json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, default=str).encode("utf-8")


#: The largest team document the design surface will accept. A node editor posts a few
#: kilobytes; anything larger is not a team.
MAX_TEAM_BYTES = 256 * 1024


# ---------------------------------------------------------------------------
# The two acts review mode allows, each calling what the CLI already calls
# ---------------------------------------------------------------------------


def answer_gate(
    project_root: str,
    config: Any,
    target: str,
    decision: str,
    note: str = "",
) -> Dict[str, Any]:
    """Answer one approval gate. Calls `approvals.resolve_approval` and nothing else.

    Shared by `--review` and the daemon's `approve`/`reject` verbs, so the two surfaces
    cannot drift into two different meanings of the same word.
    """
    from orchestrator.approvals import STATUS_APPROVED, STATUS_REJECTED, resolve_approval
    from orchestrator.config import get_approval_config

    if decision not in (STATUS_APPROVED, STATUS_REJECTED):
        return {"ok": False, "error": "unknown decision '%s'" % decision}

    record = resolve_approval(
        project_root,
        str(target or ""),
        decision,
        note=str(note or ""),
        directory=get_approval_config(config).get("directory"),
    )
    if record is None:
        return {"ok": False, "error": "no approval matching '%s'" % target}
    if record.get("already_decided"):
        return {
            "ok": False,
            "error": "that gate was already %s" % record.get("status"),
            "approval": record,
        }
    return {"ok": True, "approval": record}


def deliver_card_by_id(
    project_root: str,
    config: Any,
    board: Dict[str, Any],
    card_id: str,
) -> Dict[str, Any]:
    """Push one card and open a pull request, through the only code that pushes.

    The board's own judgement is what gates this, not the server's: a card that is not
    *Ready to Merge* is refused here exactly as ``--deliver`` refuses it. Which is the point
    of taking the board as an argument — the answer comes from the projection, and a caller
    cannot smuggle in a card the board never put in that column.
    """
    from orchestrator.__main__ import deliver_card, resolve_card
    from orchestrator.board import COLUMN_READY

    matches = resolve_card(board, str(card_id or ""))
    if not matches:
        return {"ok": False, "error": "no card matching '%s'" % card_id}
    if len(matches) > 1:
        return {
            "ok": False,
            "error": "'%s' matches %d cards" % (card_id, len(matches)),
            "cards": [{"id": c.get("id"), "title": c.get("title")} for c in matches],
        }

    card = matches[0]
    if card.get("column") != COLUMN_READY:
        return {
            "ok": False,
            "error": "that card is in '%s', not '%s'" % (card.get("column"), COLUMN_READY),
            "card": {"id": card.get("id"), "column": card.get("column")},
        }

    lines: List[str] = []
    record = deliver_card(project_root, config, card, printer=lines.append)
    refused = record.get("refused") or record.get("error")
    return {"ok": not refused, "delivery": record, "report": "\n".join(lines).strip()}


def reload_config(project_root: str, config_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Re-read the configuration file the team editor writes, or None if it will not load.

    The file is the single source of truth (ARCHITECTURE.md §5); a long-lived server only ever
    holds a copy of it, and after a write that copy is stale.
    """
    from orchestrator.config import load_config
    from orchestrator.teams import config_file_path

    try:
        return load_config(str(config_file_path(project_root, config_path)))
    except Exception:
        return None


def make_handler(
    project_root: str,
    config: Any,
    board_reader: Callable[[], Dict[str, Any]],
    writable: bool = False,
    config_path: Optional[str] = None,
    on_team_saved: Optional[Callable[[Dict[str, Any]], None]] = None,
    review: bool = False,
    on_reviewed: Optional[Callable[[str, Dict[str, Any]], None]] = None,
):
    """Build the request handler.

    Args:
        writable: Enables `POST /api/team` — the one route in this project that writes to the
            configuration. It is off unless the server was started with `--design`, so the
            office cannot acquire a write by accident.
        config_path: The config file the design surface edits; None means `orchestrator.yaml`.
        on_team_saved: Called with the write result, so the command that started the server
            can report a save on the terminal the person is actually watching.
        review: Enables `POST /api/review/approve`, `/reject` and `/deliver` (Roadmap 8.5) —
            the only routes here that can decide something about a run. Off unless the server
            was started with `--review`, and independent of `writable`: neither flag implies
            the other, because "may edit the team" and "may answer a gate" are different
            permissions and collapsing them would make one of them accidental.
        on_reviewed: Called with `(action, result)` after each review act, so the command
            that started the server can report a decision on the terminal.
    """

    class Handler(BaseHTTPRequestHandler):
        server_version = "orchestrator-office"

        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            # The office polls constantly; a request log would bury the run's own output.
            pass

        def _send(self, body: bytes, content_type: str, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionAbortedError):
                pass  # the page navigated away mid-poll

        def _page(self, filename: str) -> None:
            try:
                self._send((WEB_DIR / filename).read_bytes(), "text/html; charset=utf-8")
            except Exception as exc:
                self._send(
                    f"Could not read {filename}: {exc}".encode("utf-8"),
                    "text/plain; charset=utf-8",
                    status=500,
                )

        def do_GET(self) -> None:  # noqa: N802
            route = urlparse(self.path).path

            if route in ("/", "/index.html", "/office"):
                self._page("office.html")
                return

            if route in ("/design", "/design.html"):
                self._page("design.html")
                return

            if route == "/api/board":
                try:
                    self._send(_json_bytes(board_reader()), "application/json")
                except Exception as exc:
                    self._send(_json_bytes({"error": str(exc)}), "application/json", status=500)
                return

            if route == "/api/activity":
                try:
                    self._send(_json_bytes(latest_activity(project_root, config)), "application/json")
                except Exception as exc:
                    self._send(_json_bytes({"error": str(exc)}), "application/json", status=500)
                return

            if route == "/api/goals":
                delegation_cfg = get_delegation_config(config)
                try:
                    goals = list_goals(
                        project_root, limit=20, directory=delegation_cfg.get("goals_directory")
                    )
                    self._send(_json_bytes(goals), "application/json")
                except Exception as exc:
                    self._send(_json_bytes({"error": str(exc)}), "application/json", status=500)
                return

            if route == "/api/deliveries":
                delivery_cfg = get_delivery_config(config)
                try:
                    records = list_deliveries(
                        project_root, directory=delivery_cfg.get("directory")
                    )
                    self._send(_json_bytes(records), "application/json")
                except Exception as exc:
                    self._send(_json_bytes({"error": str(exc)}), "application/json", status=500)
                return

            if route == "/api/modes":
                # What this server is allowed to do, so a page can render the buttons it
                # actually has rather than offering one that will be refused.
                self._send(
                    _json_bytes(
                        {
                            "team_writable": bool(writable),
                            "review": bool(review),
                            "read_only": not (writable or review),
                        }
                    ),
                    "application/json",
                )
                return

            if route == "/api/approvals":
                try:
                    from orchestrator.approvals import STATUS_PENDING, list_approvals
                    from orchestrator.config import get_approval_config

                    self._send(
                        _json_bytes(
                            {
                                "approvals": list_approvals(
                                    project_root,
                                    status=STATUS_PENDING,
                                    directory=get_approval_config(config).get("directory"),
                                )
                            }
                        ),
                        "application/json",
                    )
                except Exception as exc:
                    self._send(_json_bytes({"error": str(exc)}), "application/json", status=500)
                return

            if route == "/api/stats":
                try:
                    self._send(_json_bytes(evidence(project_root, config)), "application/json")
                except Exception as exc:
                    self._send(_json_bytes({"error": str(exc)}), "application/json", status=500)
                return

            if route == "/api/team":
                try:
                    team = team_from_config(config)
                    self._send(
                        _json_bytes(
                            {
                                "team": team,
                                "catalog": catalog_from_config(config),
                                "templates": list_templates(),
                                "problems": validate_team(team, config),
                                "writable": bool(writable),
                            }
                        ),
                        "application/json",
                    )
                except Exception as exc:
                    self._send(_json_bytes({"error": str(exc)}), "application/json", status=500)
                return

            self._send(b"Not found", "text/plain; charset=utf-8", status=404)

        def do_POST(self) -> None:  # noqa: N802
            """The only write in the project's web surface: saving the team.

            It refuses on a read-only server, refuses anything that is not the team route, and
            refuses a team that would not produce a loadable configuration. What it never
            touches is a run: no route here can start work or answer a gate.
            """
            nonlocal config
            route = urlparse(self.path).path

            if route.startswith("/api/review/"):
                self._review(route[len("/api/review/"):])
                return

            if route != "/api/team":
                self._send(
                    _json_bytes({"error": "this server does not accept writes on that route"}),
                    "application/json",
                    status=405,
                )
                return
            if not writable:
                self._send(
                    _json_bytes(
                        {
                            "error": "this server is read-only. "
                            "Start it with --design to edit the team."
                        }
                    ),
                    "application/json",
                    status=405,
                )
                return

            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length <= 0 or length > MAX_TEAM_BYTES:
                self._send(
                    _json_bytes({"error": "a team document was expected"}),
                    "application/json",
                    status=400,
                )
                return

            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except Exception as exc:
                self._send(
                    _json_bytes({"error": f"could not read the team: {exc}"}),
                    "application/json",
                    status=400,
                )
                return

            team = normalize_team(payload if isinstance(payload, dict) else {})
            problems = validate_team(team, config)
            if problems:
                self._send(
                    _json_bytes({"ok": False, "problems": problems}),
                    "application/json",
                    status=400,
                )
                return

            result = write_team(project_root, team, config_path=config_path)
            if result.get("ok"):
                # The file just changed underneath this server. Every later read (the team, the
                # catalog, the board) and every later validation must see what was written, not
                # the configuration the server started with. Without this a saved assignment was
                # on disk, but the editor re-read the old one and looked as if the save had not
                # happened - until a restart (Package C).
                reloaded = reload_config(project_root, config_path)
                if reloaded is not None:
                    config = reloaded
                result["reloaded"] = reloaded is not None
            if on_team_saved:
                try:
                    on_team_saved(result)
                except Exception:
                    pass
            self._send(
                _json_bytes(result), "application/json", status=200 if result.get("ok") else 400
            )

        def _review(self, action: str) -> None:
            """Answer a gate, or deliver a card, when this server was started with --review.

            A closed list of three, exactly as the daemon's `control` is a closed list: an
            action not named here is a 404, not a 501, because it is not a thing this server
            can be asked to do at all.
            """
            from orchestrator.approvals import STATUS_APPROVED, STATUS_REJECTED

            if action not in ("approve", "reject", "deliver"):
                self._send(_json_bytes({"error": "no such review action"}),
                           "application/json", status=404)
                return

            if not review:
                self._send(
                    _json_bytes(
                        {
                            "ok": False,
                            "error": "this server cannot answer a gate or deliver a card. "
                            "Start it with --review to make decisions from the page.",
                        }
                    ),
                    "application/json",
                    status=405,
                )
                return

            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length < 0 or length > MAX_REVIEW_BYTES:
                self._send(_json_bytes({"ok": False, "error": "that message is too large"}),
                           "application/json", status=400)
                return
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
            except Exception as exc:
                self._send(_json_bytes({"ok": False, "error": "could not read: %s" % exc}),
                           "application/json", status=400)
                return
            if not isinstance(payload, dict):
                payload = {}

            try:
                if action == "deliver":
                    result = deliver_card_by_id(
                        project_root, config, board_reader(),
                        str(payload.get("card") or payload.get("id") or ""),
                    )
                else:
                    result = answer_gate(
                        project_root, config,
                        str(payload.get("id") or payload.get("approval") or ""),
                        STATUS_APPROVED if action == "approve" else STATUS_REJECTED,
                        str(payload.get("note") or ""),
                    )
            except Exception as exc:
                self._send(_json_bytes({"ok": False, "error": str(exc)}),
                           "application/json", status=500)
                return

            if on_reviewed:
                try:
                    on_reviewed(action, result)
                except Exception:
                    pass
            self._send(_json_bytes(result), "application/json",
                       status=200 if result.get("ok") else 400)

    return Handler


def serve_board(
    project_root: str,
    config: Any,
    port: int = 8730,
    open_browser: bool = True,
    printer: Optional[Callable[[str], None]] = None,
    serve_forever: bool = True,
    design: bool = False,
    config_path: Optional[str] = None,
    review: bool = False,
) -> ThreadingHTTPServer:
    """Serve the board, the office, and optionally the design or review surface.

    Args:
        project_root: The project directory.
        config: The resolved configuration.
        port: Port to bind on 127.0.0.1.
        open_browser: Open the page automatically.
        printer: Where to print the banner; defaults to `print`.
        serve_forever: False returns the server without blocking, for tests.
        design: Open on the design surface and allow it to save the team. Off by default, so
            `--serve` stays the read-only window it has always been.
        config_path: The config file the design surface edits.
        review: Allow the page to answer an approval gate and deliver a Ready to Merge card
            (Roadmap 8.5). Off by default and independent of `design`.

    Returns:
        The running server.
    """
    say = printer or print

    # Imported lazily: the CLI half of the board projection lives with the CLI, and the server
    # must not drag the whole command-line module into every import of this one.
    from orchestrator.__main__ import read_board

    def announce(result: Dict[str, Any]) -> None:
        if result.get("ok") and not result.get("unchanged"):
            say(f"  Saved the team to {result.get('path')}")
            if result.get("backup"):
                say(f"  Previous version kept at {result['backup']}")
        elif result.get("error"):
            say(f"  Not saved: {result['error']}")

    def announce_review(action: str, result: Dict[str, Any]) -> None:
        if result.get("ok"):
            if action == "deliver":
                say(f"  Delivered {(result.get('delivery') or {}).get('id')}")
            else:
                say(f"  Gate {(result.get('approval') or {}).get('id')} {action}d")
        else:
            say(f"  Not {action}d: {result.get('error')}")

    handler = make_handler(
        project_root,
        config,
        lambda: read_board(project_root, config),
        writable=bool(design),
        config_path=config_path,
        on_team_saved=announce if design else None,
        review=bool(review),
        on_reviewed=announce_review if review else None,
    )
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    root_url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    url = root_url + ("design" if design else "")

    say("")
    if design:
        say(f"  The design surface is open at {url}")
        say("  Composing a team here writes orchestrator.yaml, and nothing else.")
        say("  Every save is validated first and leaves a backup. Ctrl-C to stop.")
    elif review:
        say(f"  The review surface is open at {url}")
        say("  It can answer an approval gate and deliver a Ready to Merge card.")
        say("  It cannot start work or edit the team. Ctrl-C to stop.")
    else:
        say(f"  The office is open at {url}")
        say("  It watches the event log and changes nothing. Ctrl-C to stop.")
    say("")

    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()

    if not serve_forever:
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        return httpd

    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
    return httpd
