"""Durable run store (Tier 1 #4).

Every orchestration run is persisted to disk as it happens, so that a run's
history outlives the process that produced it.

Layout, rooted at ``<project_root>/.orchestrator/runs``::

    .orchestrator/runs/
      20260906T201500Z-a1b2c3/
        run.json      # run metadata, rewritten on start and on finish
        events.jsonl  # append-only event log, one JSON object per line

``events.jsonl`` is the source of truth; ``run.json`` is a convenience index
entry that ``list_runs`` can read without replaying the log.

Design rules
------------
* **The store never breaks a run.** Every filesystem operation is wrapped; a
  failure marks the store degraded and is reported once, but the pipeline
  continues. Persistence is observability, not correctness.
* **Append-only.** Events are only ever appended, so a crashed run still leaves
  a readable partial log.
* **No secrets are introduced.** The store records what the orchestrator already
  holds in memory; project context is redacted upstream by ``context.py``.
"""

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_RUNS_DIR = ".orchestrator/runs"

RUN_META_FILENAME = "run.json"
RUN_EVENTS_FILENAME = "events.jsonl"

# Event names written to events.jsonl
EVENT_RUN_STARTED = "run_started"
EVENT_PREFLIGHT = "preflight"
EVENT_CONTEXT = "context_collected"
EVENT_SKILLS = "skills_discovered"
EVENT_AGENT_STARTED = "agent_started"
EVENT_AGENT_RESULT = "agent_result"
EVENT_AGENT_RETRY = "agent_retry"
EVENT_VERIFICATION = "verification"
EVENT_RUN_FINISHED = "run_finished"
EVENT_RUN_RESUMED = "run_resumed"
EVENT_WORKSPACE = "workspace"
EVENT_ACCEPTANCE = "acceptance"
EVENT_TASK_PLAN = "task_plan"


def utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string with a trailing Z."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def generate_run_id() -> str:
    """Generate a sortable, collision-resistant run identifier.

    Format: ``<UTC compact timestamp>-<6 hex chars>`` (e.g. ``20260906T201500Z-a1b2c3``).
    Lexical sort order matches chronological order.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{uuid.uuid4().hex[:6]}"


def runs_root(project_root: str, directory: Optional[str] = None) -> Path:
    """Resolve the directory that holds all runs for a project."""
    rel = directory or DEFAULT_RUNS_DIR
    candidate = Path(rel)
    if candidate.is_absolute():
        return candidate
    return Path(project_root) / rel


def _truncate(text: str, max_chars: int) -> str:
    """Truncate `text` to `max_chars`, appending a marker. 0 or less means unlimited."""
    if max_chars and max_chars > 0 and text and len(text) > max_chars:
        omitted = len(text) - max_chars
        return f"{text[:max_chars]}\n\n[... truncated by run store: {omitted:,} characters omitted ...]"
    return text


class RunStore:
    """Append-only writer for a single orchestration run.

    The object is cheap to construct and holds no open file handles, so it can
    be rebuilt from a directory path on every node without coordination. That
    keeps it compatible with LangGraph state, which carries plain data.
    """

    def __init__(
        self,
        run_dir: str,
        run_id: str,
        max_output_chars: int = 0,
    ) -> None:
        self.run_dir = str(run_dir)
        self.run_id = run_id
        self.max_output_chars = max_output_chars
        self._degraded = False
        self._first_error: Optional[str] = None
        self._sequence = 0

    # -- construction --------------------------------------------------------

    @classmethod
    def create(
        cls,
        project_root: str,
        run_id: Optional[str] = None,
        directory: Optional[str] = None,
        max_output_chars: int = 0,
    ) -> "RunStore":
        """Create the run directory and return a store bound to it."""
        rid = run_id or generate_run_id()
        run_dir = runs_root(project_root, directory) / rid
        store = cls(str(run_dir), rid, max_output_chars=max_output_chars)
        try:
            run_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # pragma: no cover - filesystem dependent
            store._mark_degraded(f"Could not create run directory: {exc}")
        return store

    @classmethod
    def from_dir(cls, run_dir: str, max_output_chars: int = 0) -> "RunStore":
        """Rebuild a store for an existing run directory."""
        run_id = Path(run_dir).name
        store = cls(str(run_dir), run_id, max_output_chars=max_output_chars)
        store._sequence = store._count_events()
        return store

    # -- properties ----------------------------------------------------------

    @property
    def events_path(self) -> Path:
        return Path(self.run_dir) / RUN_EVENTS_FILENAME

    @property
    def meta_path(self) -> Path:
        return Path(self.run_dir) / RUN_META_FILENAME

    @property
    def degraded(self) -> bool:
        """True when at least one write failed; the run itself is unaffected."""
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
            "run_id": self.run_id,
            "event": event_name,
            "timestamp": utc_now_iso(),
        }
        record.update(payload)
        try:
            Path(self.run_dir).mkdir(parents=True, exist_ok=True)
            with open(self.events_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except Exception as exc:
            self._mark_degraded(f"Could not append '{event_name}' event: {exc}")

    def _write_meta(self, meta: Dict[str, Any]) -> None:
        """Rewrite run.json. Never raises."""
        try:
            Path(self.run_dir).mkdir(parents=True, exist_ok=True)
            with open(self.meta_path, "w", encoding="utf-8") as handle:
                json.dump(meta, handle, indent=2, ensure_ascii=False, default=str)
        except Exception as exc:
            self._mark_degraded(f"Could not write run metadata: {exc}")

    # -- recording -----------------------------------------------------------

    def record_run_started(
        self,
        task: str,
        project_root: str,
        config: Optional[Dict[str, Any]] = None,
        max_repair_attempts: Optional[int] = None,
        settings: Optional[Dict[str, Any]] = None,
        goal_id: Optional[str] = None,
        task_id: Optional[str] = None,
    ) -> None:
        """Record run start and seed run.json.

        `settings` records the knobs this run was configured with - consensus
        policy, isolation mode, budget. They cost nothing to store and they are
        what makes `--stats` able to answer whether a setting was worth its
        price; a run that does not record them can only be counted, not compared.
        """
        agents = []
        for entry in (config or {}).get("agents", []) or []:
            agents.append(
                {
                    "agent": entry.get("agent"),
                    "model": entry.get("model"),
                    "role": entry.get("role"),
                }
            )
        meta = {
            "run_id": self.run_id,
            "task": task,
            "project_root": project_root,
            "started_at": utc_now_iso(),
            "finished_at": None,
            "status": "running",
            "verdict": None,
            "agents": agents,
            "max_repair_attempts": max_repair_attempts,
            "settings": dict(settings or {}),
            # Set when this session is one task of a delegated goal (Roadmap Phase 2), so a
            # goal's tasks can be found from the sessions that carried them out.
            "goal_id": goal_id,
            "task_id": task_id,
        }
        self._write_meta(meta)
        self._append(
            EVENT_RUN_STARTED,
            {
                "task": task,
                "project_root": project_root,
                "agents": agents,
                "max_repair_attempts": max_repair_attempts,
                "settings": dict(settings or {}),
                "goal_id": goal_id,
                "task_id": task_id,
            },
        )

    def record_run_resumed(
        self,
        replayed_roles: Optional[List[str]] = None,
        repair_attempts: int = 0,
    ) -> None:
        """Record that this run was picked up again after stopping.

        Appended to the original run's log, because a resumed run is the same
        run continuing - splitting it into a second record would make its cost
        and its history unreadable.
        """
        self._append(
            EVENT_RUN_RESUMED,
            {
                "replayed_roles": list(replayed_roles or []),
                "repair_attempts": repair_attempts,
            },
        )
        try:
            meta: Dict[str, Any] = {}
            if self.meta_path.is_file():
                with open(self.meta_path, "r", encoding="utf-8") as handle:
                    meta = json.load(handle)
            meta["status"] = "running"
            meta["finished_at"] = None
            meta["resumed_count"] = int(meta.get("resumed_count") or 0) + 1
            self._write_meta(meta)
        except Exception as exc:
            self._mark_degraded(f"Could not record resume: {exc}")

    def record_preflight(self, report: Dict[str, Any]) -> None:
        """Record the preflight report."""
        self._append(EVENT_PREFLIGHT, {"report": report})

    def record_event(self, name: str, **payload: Any) -> None:
        """Record an arbitrary named event (e.g. workspace isolation)."""
        self._append(name, dict(payload))

    def record_context(self, project_root: str, context_chars: int) -> None:
        """Record that project context was collected."""
        self._append(
            EVENT_CONTEXT,
            {"project_root": project_root, "context_chars": context_chars},
        )

    def record_skills(self, skill_names: List[str]) -> None:
        """Record discovered skill names."""
        self._append(EVENT_SKILLS, {"count": len(skill_names), "skills": skill_names})

    def record_agent_started(
        self,
        agent: str,
        role: str,
        model: Optional[Any] = None,
        repair_attempt: Optional[int] = None,
    ) -> None:
        """Record that an agent was launched.

        Until this existed, a reader could only see agents that had already *finished*, so
        anything watching a run had to infer who was working from who was not. An execution
        beginning is a durable fact like any other; recording it is what lets the board and the
        office show work in progress rather than guess at it.
        """
        self._append(
            EVENT_AGENT_STARTED,
            {
                "agent": agent,
                "role": role,
                "model": model,
                "repair_attempt": repair_attempt,
            },
        )

    def record_agent_retry(
        self,
        agent: str,
        role: str,
        attempt: int,
        of: int,
        reason: str,
        model: Optional[Any] = None,
        next_model: Optional[Any] = None,
        next_agent: Optional[Any] = None,
        backoff_seconds: float = 0.0,
    ) -> None:
        """Record that one execution failed and is being attempted again.

        A retry is a fact, and an expensive one - it costs a whole agent invocation. It is
        recorded as its own event rather than as another `agent_result` because the phase
        produced *one* outcome, and a reader counting results to resolve consensus or to
        compute a pass rate must not see a retried execution as an ensemble of three.

        `next_agent` is recorded beside `next_model` because an escalation ladder may cross
        providers: which agent the run fell back *to* is the fact that makes a rescued run
        readable afterwards, and deriving it from the following `agent_result` would only
        work when the fallback succeeded.
        """
        self._append(
            EVENT_AGENT_RETRY,
            {
                "agent": agent,
                "role": role,
                "attempt": attempt,
                "of": of,
                "reason": reason,
                "model": model,
                "next_model": next_model,
                "next_agent": next_agent,
                "backoff_seconds": round(float(backoff_seconds), 2),
            },
        )

    def record_agent_result(self, result: Dict[str, Any]) -> None:
        """Record one AgentResult exactly as the pipeline produced it."""
        payload = dict(result)
        if "output" in payload and isinstance(payload["output"], str):
            payload["output"] = _truncate(payload["output"], self.max_output_chars)
        self._append(EVENT_AGENT_RESULT, {"result": payload})

    def record_acceptance(self, check: Dict[str, Any]) -> None:
        """Record one objective acceptance gate result (Roadmap Phase 0).

        Stored in full, including its output: this is the one piece of a run that is evidence
        rather than testimony, and `--stats` reads it back to ask how often a verifier's PASS
        disagreed with it.
        """
        payload = dict(check)
        if isinstance(payload.get("output"), str):
            payload["output"] = _truncate(payload["output"], self.max_output_chars)
        self._append(EVENT_ACCEPTANCE, {"check": payload})

    def record_task_plan(self, plan: Dict[str, Any]) -> None:
        """Record a decomposition (Roadmap Phase 1)."""
        self._append(EVENT_TASK_PLAN, {"plan": dict(plan)})

    def record_verification(self, record: Dict[str, Any]) -> None:
        """Record one VerificationRecord."""
        payload = dict(record)
        if "output" in payload and isinstance(payload["output"], str):
            payload["output"] = _truncate(payload["output"], self.max_output_chars)
        self._append(EVENT_VERIFICATION, {"record": payload})

    def record_run_finished(self, summary: Dict[str, Any]) -> None:
        """Record the terminal summary and finalize run.json."""
        self._append(EVENT_RUN_FINISHED, {"summary": summary})

        meta: Dict[str, Any] = {}
        try:
            if self.meta_path.is_file():
                with open(self.meta_path, "r", encoding="utf-8") as handle:
                    meta = json.load(handle)
        except Exception:
            meta = {}

        meta.setdefault("run_id", self.run_id)
        meta["finished_at"] = utc_now_iso()
        meta["status"] = summary.get("status")
        meta["verdict"] = summary.get("verdict")
        meta["summary"] = summary
        self._write_meta(meta)


# ---------------------------------------------------------------------------
# Reading runs back
# ---------------------------------------------------------------------------


def open_run(
    project_root: str,
    task: str,
    config: Optional[Dict[str, Any]] = None,
    enabled: bool = True,
    directory: Optional[str] = None,
    max_output_chars: int = 0,
    max_repair_attempts: Optional[int] = None,
    settings: Optional[Dict[str, Any]] = None,
    goal_id: Optional[str] = None,
    task_id: Optional[str] = None,
) -> Optional[RunStore]:
    """Create and seed a run store, or return None when persistence is disabled."""
    if not enabled:
        return None
    store = RunStore.create(
        project_root,
        directory=directory,
        max_output_chars=max_output_chars,
    )
    store.record_run_started(
        task=task,
        project_root=project_root,
        config=config,
        max_repair_attempts=max_repair_attempts,
        settings=settings,
        goal_id=goal_id,
        task_id=task_id,
    )
    return store


def list_runs(
    project_root: str,
    limit: int = 20,
    directory: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """List recorded runs, newest first.

    Reads each run's ``run.json``. Runs whose metadata is missing or unreadable
    are still listed, with whatever can be recovered from the directory name.
    """
    root = runs_root(project_root, directory)
    if not root.is_dir():
        return []

    entries: List[Dict[str, Any]] = []
    try:
        candidates = sorted(
            (d for d in root.iterdir() if d.is_dir()),
            key=lambda d: d.name,
            reverse=True,
        )
    except Exception:
        return []

    for run_dir in candidates:
        if limit and len(entries) >= limit:
            break
        meta: Dict[str, Any] = {"run_id": run_dir.name, "run_dir": str(run_dir)}
        meta_file = run_dir / RUN_META_FILENAME
        try:
            if meta_file.is_file():
                with open(meta_file, "r", encoding="utf-8") as handle:
                    meta.update(json.load(handle))
        except Exception:
            meta["status"] = meta.get("status") or "unreadable"
        meta["run_dir"] = str(run_dir)
        entries.append(meta)

    return entries


def load_run(
    project_root: str,
    run_id: str,
    directory: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Load one run's metadata and full event log.

    Returns None when the run does not exist. Malformed event lines are skipped
    rather than raising, so a partially-written log is still readable.
    """
    run_dir = runs_root(project_root, directory) / run_id
    if not run_dir.is_dir():
        return None

    meta: Dict[str, Any] = {"run_id": run_id, "run_dir": str(run_dir)}
    meta_file = run_dir / RUN_META_FILENAME
    try:
        if meta_file.is_file():
            with open(meta_file, "r", encoding="utf-8") as handle:
                meta.update(json.load(handle))
    except Exception:
        pass

    events: List[Dict[str, Any]] = []
    events_file = run_dir / RUN_EVENTS_FILENAME
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

    meta["run_dir"] = str(run_dir)
    meta["events"] = events
    return meta


def resolve_run_id(
    project_root: str,
    run_id: str,
    directory: Optional[str] = None,
) -> Optional[str]:
    """Resolve a full or partial run id (or ``latest``) to a concrete run id."""
    runs = list_runs(project_root, limit=0, directory=directory)
    if not runs:
        return None
    if run_id in ("latest", "last"):
        return runs[0].get("run_id")
    for entry in runs:
        if entry.get("run_id") == run_id:
            return run_id
    matches = [e.get("run_id") for e in runs if str(e.get("run_id", "")).startswith(run_id)]
    if len(matches) == 1:
        return matches[0]
    return None
