"""The Goal store (Roadmap Phases 2-3).

A **Session** is one attempt at one Task; the run store already persists those. A **Goal** is
what a person actually asked for, and it owns the decomposition plus the Sessions that carried
its Tasks out. This module persists that layer.

Layout, rooted at ``<project_root>/.orchestrator/goals``::

    .orchestrator/goals/
      20260907T051500Z-a1b2c3/
        goal.json     # metadata, rewritten on start and on finish
        events.jsonl  # append-only event log, one JSON object per line

The design rules are the run store's, one level up, and for the same reasons:

* **The store never breaks a goal.** Every filesystem operation is wrapped; a failure marks the
  store degraded and is reported once, but execution continues.
* **Append-only.** A goal killed halfway still leaves a readable log of what its tasks did.
* **Facts only.** Task states are *derived* from the sessions those tasks ran (see
  `orchestrator.status.derive_task_state`), never written down as a status a scheduler had to
  remember to update.
"""

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from orchestrator.store import generate_run_id, utc_now_iso

DEFAULT_GOALS_DIR = ".orchestrator/goals"

GOAL_META_FILENAME = "goal.json"
GOAL_EVENTS_FILENAME = "events.jsonl"

EVENT_GOAL_STARTED = "goal_started"
EVENT_GOAL_PLAN = "goal_plan"
EVENT_TASK_STARTED = "task_started"
EVENT_TASK_FINISHED = "task_finished"
EVENT_TASK_SKIPPED = "task_skipped"
EVENT_COLLISION = "collision"
EVENT_GOAL_FINISHED = "goal_finished"


def generate_goal_id() -> str:
    """Generate a sortable goal identifier, in the run store's format."""
    return generate_run_id()


def goals_root(project_root: str, directory: Optional[str] = None) -> Path:
    """Resolve the directory that holds every goal for a project."""
    rel = directory or DEFAULT_GOALS_DIR
    candidate = Path(rel)
    if candidate.is_absolute():
        return candidate
    return Path(project_root) / rel


class GoalStore:
    """Append-only writer for a single delegated goal.

    Holds no open handles, so it can be reconstructed from a directory path at any point —
    the same property that lets `RunStore` live alongside LangGraph state.
    """

    def __init__(self, goal_dir: str, goal_id: str) -> None:
        self.goal_dir = str(goal_dir)
        self.goal_id = goal_id
        self._degraded = False
        self._first_error: Optional[str] = None
        self._sequence = 0

    # -- construction --------------------------------------------------------

    @classmethod
    def create(
        cls,
        project_root: str,
        goal_id: Optional[str] = None,
        directory: Optional[str] = None,
    ) -> "GoalStore":
        """Create the goal directory and return a store bound to it."""
        gid = goal_id or generate_goal_id()
        goal_dir = goals_root(project_root, directory) / gid
        store = cls(str(goal_dir), gid)
        try:
            goal_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # pragma: no cover - filesystem dependent
            store._mark_degraded(f"Could not create goal directory: {exc}")
        return store

    @classmethod
    def from_dir(cls, goal_dir: str) -> "GoalStore":
        """Rebuild a store for an existing goal directory."""
        store = cls(str(goal_dir), Path(goal_dir).name)
        store._sequence = store._count_events()
        return store

    # -- properties ----------------------------------------------------------

    @property
    def events_path(self) -> Path:
        return Path(self.goal_dir) / GOAL_EVENTS_FILENAME

    @property
    def meta_path(self) -> Path:
        return Path(self.goal_dir) / GOAL_META_FILENAME

    @property
    def degraded(self) -> bool:
        return self._degraded

    @property
    def degraded_reason(self) -> Optional[str]:
        return self._first_error

    # -- internals -----------------------------------------------------------

    def _mark_degraded(self, reason: str) -> None:
        if not self._degraded:
            self._degraded = True
            self._first_error = reason

    def _count_events(self) -> int:
        try:
            if not self.events_path.is_file():
                return 0
            with open(self.events_path, "r", encoding="utf-8", errors="replace") as handle:
                return sum(1 for line in handle if line.strip())
        except Exception:
            return 0

    def _append(self, event_name: str, payload: Dict[str, Any]) -> None:
        """Append one event. Never raises."""
        self._sequence += 1
        record = {
            "sequence": self._sequence,
            "goal_id": self.goal_id,
            "event": event_name,
            "timestamp": utc_now_iso(),
        }
        record.update(payload)
        try:
            Path(self.goal_dir).mkdir(parents=True, exist_ok=True)
            with open(self.events_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except Exception as exc:
            self._mark_degraded(f"Could not append '{event_name}' event: {exc}")

    def _write_meta(self, meta: Dict[str, Any]) -> None:
        try:
            Path(self.goal_dir).mkdir(parents=True, exist_ok=True)
            with open(self.meta_path, "w", encoding="utf-8") as handle:
                json.dump(meta, handle, indent=2, ensure_ascii=False, default=str)
        except Exception as exc:
            self._mark_degraded(f"Could not write goal metadata: {exc}")

    def _read_meta(self) -> Dict[str, Any]:
        try:
            if self.meta_path.is_file():
                with open(self.meta_path, "r", encoding="utf-8") as handle:
                    return json.load(handle)
        except Exception:
            pass
        return {}

    # -- recording -----------------------------------------------------------

    def record_goal_started(
        self,
        goal: str,
        project_root: str,
        base_ref: Optional[str] = None,
        settings: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Record the goal's opening facts and seed goal.json."""
        meta = {
            "goal_id": self.goal_id,
            "goal": goal,
            "project_root": project_root,
            "base_ref": base_ref,
            "started_at": utc_now_iso(),
            "finished_at": None,
            "status": "running",
            "settings": dict(settings or {}),
        }
        self._write_meta(meta)
        self._append(
            EVENT_GOAL_STARTED,
            {"goal": goal, "project_root": project_root, "base_ref": base_ref,
             "settings": dict(settings or {})},
        )

    def record_plan(self, plan: Dict[str, Any]) -> None:
        """Record the decomposition this goal is executing."""
        self._append(EVENT_GOAL_PLAN, {"plan": dict(plan)})
        meta = self._read_meta()
        meta["plan"] = dict(plan)
        meta.setdefault("goal_id", self.goal_id)
        self._write_meta(meta)

    def record_task_started(self, task_id: str, run_id: Optional[str], base_ref: Optional[str]) -> None:
        self._append(
            EVENT_TASK_STARTED,
            {"task_id": task_id, "run_id": run_id, "base_ref": base_ref},
        )

    def record_task_finished(self, task_id: str, outcome: Dict[str, Any]) -> None:
        """Record what a task's session produced."""
        self._append(EVENT_TASK_FINISHED, {"task_id": task_id, "outcome": dict(outcome)})

    def record_task_skipped(self, task_id: str, reason: str, detail: str = "") -> None:
        """Record that a task never ran, and why."""
        self._append(
            EVENT_TASK_SKIPPED, {"task_id": task_id, "reason": reason, "detail": detail}
        )

    def record_collision(self, task_ids: List[str], paths: List[str]) -> None:
        """Record that sibling tasks edited the same files."""
        self._append(EVENT_COLLISION, {"task_ids": list(task_ids), "paths": list(paths)})

    def record_goal_finished(self, summary: Dict[str, Any]) -> None:
        """Record the terminal summary and finalize goal.json."""
        self._append(EVENT_GOAL_FINISHED, {"summary": summary})
        meta = self._read_meta()
        meta.setdefault("goal_id", self.goal_id)
        meta["finished_at"] = utc_now_iso()
        meta["status"] = summary.get("status")
        meta["summary"] = summary
        self._write_meta(meta)


# ---------------------------------------------------------------------------
# Reading goals back
# ---------------------------------------------------------------------------


def open_goal(
    project_root: str,
    goal: str,
    base_ref: Optional[str] = None,
    enabled: bool = True,
    directory: Optional[str] = None,
    settings: Optional[Dict[str, Any]] = None,
) -> Optional[GoalStore]:
    """Create and seed a goal store, or return None when persistence is disabled."""
    if not enabled:
        return None
    store = GoalStore.create(project_root, directory=directory)
    store.record_goal_started(
        goal=goal, project_root=project_root, base_ref=base_ref, settings=settings
    )
    return store


def list_goals(
    project_root: str,
    limit: int = 20,
    directory: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """List recorded goals, newest first. Never raises."""
    root = goals_root(project_root, directory)
    if not root.is_dir():
        return []

    entries: List[Dict[str, Any]] = []
    try:
        candidates = sorted(
            (d for d in root.iterdir() if d.is_dir()), key=lambda d: d.name, reverse=True
        )
    except Exception:
        return []

    for goal_dir in candidates:
        if limit and len(entries) >= limit:
            break
        meta: Dict[str, Any] = {"goal_id": goal_dir.name, "goal_dir": str(goal_dir)}
        meta_file = goal_dir / GOAL_META_FILENAME
        try:
            if meta_file.is_file():
                with open(meta_file, "r", encoding="utf-8") as handle:
                    meta.update(json.load(handle))
        except Exception:
            meta["status"] = meta.get("status") or "unreadable"
        meta["goal_dir"] = str(goal_dir)
        entries.append(meta)
    return entries


def load_goal(
    project_root: str,
    goal_id: str,
    directory: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Load one goal's metadata and full event log. None when it does not exist."""
    goal_dir = goals_root(project_root, directory) / goal_id
    if not goal_dir.is_dir():
        return None

    meta: Dict[str, Any] = {"goal_id": goal_id, "goal_dir": str(goal_dir)}
    meta_file = goal_dir / GOAL_META_FILENAME
    try:
        if meta_file.is_file():
            with open(meta_file, "r", encoding="utf-8") as handle:
                meta.update(json.load(handle))
    except Exception:
        pass

    events: List[Dict[str, Any]] = []
    events_file = goal_dir / GOAL_EVENTS_FILENAME
    try:
        if events_file.is_file():
            with open(events_file, "r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    except Exception:
        pass

    meta["goal_dir"] = str(goal_dir)
    meta["events"] = events
    return meta


def resolve_goal_id(
    project_root: str,
    goal_id: str,
    directory: Optional[str] = None,
) -> Optional[str]:
    """Resolve a full or partial goal id (or ``latest``) to a concrete goal id."""
    goals = list_goals(project_root, limit=0, directory=directory)
    if not goals:
        return None
    if goal_id in ("latest", "last"):
        return goals[0].get("goal_id")
    for entry in goals:
        if entry.get("goal_id") == goal_id:
            return goal_id
    matches = [g.get("goal_id") for g in goals if str(g.get("goal_id", "")).startswith(goal_id)]
    if len(matches) == 1:
        return matches[0]
    return None


def task_outcomes(goal: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Replay a goal's event log into the latest outcome per task.

    The log is the source of truth; this is the read-time projection of it, so a goal that was
    killed mid-flight still reports exactly what each of its tasks got to.
    """
    outcomes: Dict[str, Dict[str, Any]] = {}
    for event in (goal or {}).get("events") or []:
        name = event.get("event")
        task_id = event.get("task_id")
        if not task_id:
            continue
        if name == EVENT_TASK_STARTED:
            outcomes[task_id] = {
                "task_id": task_id,
                "run_id": event.get("run_id"),
                "base_ref": event.get("base_ref"),
                "started": True,
            }
        elif name == EVENT_TASK_FINISHED:
            row = outcomes.setdefault(task_id, {"task_id": task_id, "started": True})
            row.update(dict(event.get("outcome") or {}))
        elif name == EVENT_TASK_SKIPPED:
            outcomes[task_id] = {
                "task_id": task_id,
                "started": False,
                "skipped": True,
                "skip_reason": event.get("reason"),
                "detail": event.get("detail"),
            }
    return outcomes


def collisions(goal: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return every recorded collision between sibling tasks."""
    return [
        {"task_ids": e.get("task_ids") or [], "paths": e.get("paths") or []}
        for e in (goal or {}).get("events") or []
        if e.get("event") == EVENT_COLLISION
    ]
