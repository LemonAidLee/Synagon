"""Archiving the run store: taking a run out of the dataset without destroying it.

`prune.py` is retention for *branches*, and it deletes. This is retention for the *run
store*, and it does not: an archived run is moved, whole, into
``.orchestrator/archive/runs/<run_id>/`` and can be moved back byte-for-byte. That
difference is the whole design. A branch is the only copy of some work, so deleting one
is a decision. A run record is evidence, and the reason to take a run out of the store is
never that its history is worthless — it is that it is not evidence *about this project*,
and every projection that reads the store is being told otherwise.

Why this exists
---------------
For most of this project's life the test suite wrote real run records into the developer's
own ``.orchestrator/runs``. `tests/support.py` stopped that, but it could not un-write what
was already there: **141 synthetic runs against 23 real ones**, 86% of the dataset. Everything
downstream read the poison and reported it faithfully — the board drew 126 "Ready to Merge"
cards for pipeline runs that never ran, ``--stats`` scored an agent over 324 executions that
were mocks, and the planner's memory (§27) and the Team pane's evidence (§8.3) were built on
the same numbers. None of those were bugs. They were correct projections of a corrupt store.

What counts as synthetic, and why it is evidence rather than a guess
-------------------------------------------------------------------
Matching on the task text would work exactly once, on this repository, for the string those
particular tests happened to use. Instead a run is judged by what it recorded, on two
independent facts that a real agent invocation cannot both produce:

* **No execution reported any token usage.** Invariant 5 says spend is never imputed, so an
  absent count is genuinely absent — but a whole run of agents that all reported nothing is
  a run in which no provider was ever called.
* **Every execution finished faster than a process launch plausibly could.** A mocked runner
  returns in milliseconds; a real CLI agent has to start, connect, and wait on a model.

Both must hold, and a run with no executions at all is never synthetic (it is an
interrupted run, which is real). On the store this was written for, the two groups do not
touch: the slowest synthetic execution took 0.88s and reported nothing, while the fastest
real one took 8.26s and every real run reported usage. `SYNTHETIC_MAX_EXECUTION_SECONDS`
sits in the gap with an order of magnitude of headroom on both sides.

Load-bearing rules
------------------
* **Nothing here deletes.** The only filesystem verb is a move, in both directions. A failed
  move leaves the run where it was; a half-copied directory is never left behind, because
  ``os.replace`` on a directory is atomic within a filesystem and the fallback copies before
  it removes.
* **The plan is always shown first.** Like `--prune-runs`: compute, print, confirm, then act.
* **An archived run says why it was archived.** ``archived.json`` inside the archived
  directory records the timestamp, the reason, and the evidence the judgement was made on,
  so the decision can be argued with a year later. Restoring removes that sidecar, so a
  restored run is exactly what it was before.
* **Archiving is not a projection.** No other module reads the archive. It is out of the
  store, and that is the entire point.
"""

import json
import os
import shutil
from typing import Any, Dict, List, Optional

from orchestrator.store import (
    RUN_EVENTS_FILENAME,
    list_runs,
    runs_root,
    utc_now_iso,
)

#: Where archived runs go, relative to the project root.
DEFAULT_ARCHIVE_DIR = ".orchestrator/archive/runs"

#: The sidecar an archived run carries. Written on archive, removed on restore.
ARCHIVE_META_FILENAME = "archived.json"

#: The longest one agent execution may take and still read as mocked. The store this was
#: written for separates at 0.88s (synthetic) against 8.26s (real); anywhere in that gap is
#: correct, and the middle of it is the least surprising place to stand.
SYNTHETIC_MAX_EXECUTION_SECONDS = 2.0

#: Why a run was taken out of the store. The archive records one of these verbatim.
REASON_SYNTHETIC = "synthetic: no execution reported token usage, and none took real time"
REASON_MATCHED = "matched --matching"


def archive_root(project_root: str, directory: Optional[str] = None) -> str:
    """Resolve the directory that holds archived runs for a project."""
    rel = directory or DEFAULT_ARCHIVE_DIR
    if os.path.isabs(rel):
        return os.path.normpath(rel)
    return os.path.normpath(os.path.join(project_root, rel))


# ---------------------------------------------------------------------------
# Judging one run
# ---------------------------------------------------------------------------


def run_evidence(run_dir: str) -> Dict[str, Any]:
    """What a run's event log says about whether anything actually ran. Never raises.

    Returns:
        ``{executions, token_usage_reported, max_execution_seconds, readable}``. A log that
        cannot be read reports ``readable: False`` and zero executions, which is never
        synthetic — an unreadable run is not a run that can be shown to be fake.
    """
    evidence: Dict[str, Any] = {
        "executions": 0,
        "token_usage_reported": False,
        "max_execution_seconds": 0.0,
        "readable": False,
    }

    path = os.path.join(run_dir, RUN_EVENTS_FILENAME)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            evidence["readable"] = True
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except (ValueError, TypeError):
                    continue  # a torn last line in an append-only log is not a failure
                if not isinstance(event, dict) or event.get("event") != "agent_result":
                    continue
                result = event.get("result")
                if not isinstance(result, dict):
                    continue
                evidence["executions"] += 1
                try:
                    duration = float(result.get("duration_seconds") or 0.0)
                except (TypeError, ValueError):
                    duration = 0.0
                if duration > evidence["max_execution_seconds"]:
                    evidence["max_execution_seconds"] = duration
                usage = result.get("token_usage")
                if isinstance(usage, dict) and usage.get("available"):
                    evidence["token_usage_reported"] = True
    except OSError:
        return evidence

    return evidence


def is_synthetic(
    evidence: Dict[str, Any],
    max_execution_seconds: float = SYNTHETIC_MAX_EXECUTION_SECONDS,
) -> bool:
    """Whether this evidence describes a run in which no agent was really invoked. Pure.

    Both conditions must hold, and there must be something to judge. Requiring both is what
    keeps a real run whose agents all failed to report usage — which invariant 5 permits —
    out of the archive.
    """
    if not evidence.get("readable"):
        return False
    if int(evidence.get("executions") or 0) <= 0:
        return False
    if evidence.get("token_usage_reported"):
        return False
    return float(evidence.get("max_execution_seconds") or 0.0) < float(max_execution_seconds)


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def plan_archive(
    project_root: str,
    synthetic: bool = True,
    matching: Optional[str] = None,
    runs_directory: Optional[str] = None,
    archive_directory: Optional[str] = None,
    max_execution_seconds: float = SYNTHETIC_MAX_EXECUTION_SECONDS,
) -> Dict[str, Any]:
    """Decide which runs leave the store, and say why for every one that stays.

    Args:
        project_root: The project whose store is being read.
        synthetic: Select runs that recorded no real agent invocation.
        matching: Also select runs whose task contains this text (case-insensitive).
        runs_directory: The run store, when it is not the default.
        archive_directory: Where archived runs go, when it is not the default.
        max_execution_seconds: The synthetic threshold, exposed for tests.

    Returns:
        ``{available, reason, total, selected, kept, archive_dir}``. ``selected`` and
        ``kept`` hold the same candidate shape, each carrying its own ``reason``.
    """
    store = runs_root(project_root, runs_directory)
    plan: Dict[str, Any] = {
        "available": True,
        "reason": "",
        "total": 0,
        "selected": [],
        "kept": [],
        "archive_dir": archive_root(project_root, archive_directory),
    }

    if not store.is_dir():
        plan["available"] = False
        plan["reason"] = f"no run store at {store}"
        return plan

    needle = (matching or "").strip().lower()
    if not synthetic and not needle:
        plan["available"] = False
        plan["reason"] = "nothing was selected: pass --synthetic-runs or --matching TEXT"
        return plan

    entries = list_runs(project_root, limit=0, directory=runs_directory)
    plan["total"] = len(entries)

    for meta in entries:
        run_dir = str(meta.get("run_dir") or "")
        task = str(meta.get("task") or "")
        evidence = run_evidence(run_dir)
        candidate: Dict[str, Any] = {
            "run_id": str(meta.get("run_id") or os.path.basename(run_dir)),
            "run_dir": run_dir,
            "task": task.replace("\n", " ")[:70],
            "status": str(meta.get("status") or "unknown"),
            "started_at": str(meta.get("started_at") or ""),
            "evidence": evidence,
        }

        reasons: List[str] = []
        if synthetic and is_synthetic(evidence, max_execution_seconds):
            reasons.append(REASON_SYNTHETIC)
        if needle and needle in task.lower():
            reasons.append(REASON_MATCHED)

        if reasons:
            candidate["reason"] = "; ".join(reasons)
            plan["selected"].append(candidate)
        else:
            candidate["reason"] = _keep_reason(evidence)
            plan["kept"].append(candidate)

    return plan


def _keep_reason(evidence: Dict[str, Any]) -> str:
    """The one-line answer to "why is this run staying?". Pure."""
    if not evidence.get("readable"):
        return "its event log could not be read, so nothing about it is established"
    if int(evidence.get("executions") or 0) <= 0:
        return "recorded no agent execution, so there is nothing to judge"
    if evidence.get("token_usage_reported"):
        return "reported real token usage"
    seconds = float(evidence.get("max_execution_seconds") or 0.0)
    return f"an execution took {seconds:.2f}s, which is real time"


def plan_restore(
    project_root: str,
    run_ids: Optional[List[str]] = None,
    runs_directory: Optional[str] = None,
    archive_directory: Optional[str] = None,
) -> Dict[str, Any]:
    """Decide which archived runs go back into the store.

    Args:
        run_ids: Ids or unique prefixes to restore. Empty means every archived run.

    Returns:
        ``{available, reason, selected, conflicts, store_dir}``. A run whose id already
        exists in the store is a conflict rather than an overwrite: the store is
        append-only, and a restore that clobbered a live run would be the one way this
        module could destroy something.
    """
    archive_dir = archive_root(project_root, archive_directory)
    store = runs_root(project_root, runs_directory)
    plan: Dict[str, Any] = {
        "available": True,
        "reason": "",
        "selected": [],
        "conflicts": [],
        "store_dir": str(store),
        "archive_dir": archive_dir,
    }

    if not os.path.isdir(archive_dir):
        plan["available"] = False
        plan["reason"] = f"no archive at {archive_dir}"
        return plan

    wanted = [str(r).strip() for r in (run_ids or []) if str(r).strip()]
    for entry in sorted(os.listdir(archive_dir)):
        source = os.path.join(archive_dir, entry)
        if not os.path.isdir(source):
            continue
        if wanted and not any(entry == w or entry.startswith(w) for w in wanted):
            continue
        candidate = {
            "run_id": entry,
            "source": source,
            "target": str(store / entry),
            **_archived_meta(source),
        }
        if os.path.exists(candidate["target"]):
            candidate["error"] = "a run with that id is already in the store"
            plan["conflicts"].append(candidate)
        else:
            plan["selected"].append(candidate)

    if wanted and not plan["selected"] and not plan["conflicts"]:
        plan["available"] = False
        plan["reason"] = "no archived run matched " + ", ".join(wanted)
    return plan


def _archived_meta(source: str) -> Dict[str, Any]:
    """The sidecar an archived run carries, or empty. Never raises."""
    try:
        with open(os.path.join(source, ARCHIVE_META_FILENAME), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return {
            "archived_at": str(data.get("archived_at") or ""),
            "archive_reason": str(data.get("reason") or ""),
        }
    except (OSError, ValueError, TypeError):
        return {"archived_at": "", "archive_reason": ""}


def list_archived(
    project_root: str, archive_directory: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Every archived run, newest first. Never raises."""
    archive_dir = archive_root(project_root, archive_directory)
    rows: List[Dict[str, Any]] = []
    try:
        entries = sorted(os.listdir(archive_dir), reverse=True)
    except OSError:
        return rows
    for entry in entries:
        source = os.path.join(archive_dir, entry)
        if not os.path.isdir(source):
            continue
        row: Dict[str, Any] = {"run_id": entry, "path": source, "task": "", "status": ""}
        row.update(_archived_meta(source))
        try:
            with open(os.path.join(source, "run.json"), "r", encoding="utf-8") as fh:
                meta = json.load(fh)
            row["task"] = str(meta.get("task") or "").replace("\n", " ")[:70]
            row["status"] = str(meta.get("status") or "")
        except (OSError, ValueError, TypeError):
            pass
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Doing it
# ---------------------------------------------------------------------------


def _move(source: str, target: str) -> None:
    """Move a directory, atomically where the filesystem allows it.

    ``os.replace`` cannot cross a filesystem and refuses a non-empty target, both of which
    are correct here. The fallback copies first and only then removes the source, so an
    interrupted move leaves the run readable in its original place.
    """
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    try:
        os.replace(source, target)
    except OSError:
        shutil.copytree(source, target, dirs_exist_ok=False)
        shutil.rmtree(source)


def execute_archive(
    project_root: str,
    plan: Dict[str, Any],
    archive_directory: Optional[str] = None,
) -> Dict[str, Any]:
    """Move the runs a plan selected out of the store. Never raises.

    Returns:
        ``{"archived": [...], "failed": [...]}``; a failed entry carries its ``error`` and
        its run is still in the store.
    """
    archive_dir = str(plan.get("archive_dir") or archive_root(project_root, archive_directory))
    archived: List[Dict[str, Any]] = []
    failed: List[Dict[str, Any]] = []

    for candidate in plan.get("selected") or []:
        source = str(candidate.get("run_dir") or "")
        run_id = str(candidate.get("run_id") or "")
        target = os.path.join(archive_dir, run_id)
        if not source or not os.path.isdir(source):
            failed.append({**candidate, "error": "the run is no longer in the store"})
            continue
        if os.path.exists(target):
            failed.append({**candidate, "error": "already archived under that id"})
            continue
        try:
            _move(source, target)
        except (OSError, shutil.Error) as exc:
            failed.append({**candidate, "error": f"could not move it: {exc}"})
            continue

        # Written after the move, so a run only ever claims to be archived once it is.
        # A sidecar that fails to write is not a failed archive - the run has moved, and
        # the explanation is a courtesy the same way `prune.py` treats its annotation.
        try:
            with open(os.path.join(target, ARCHIVE_META_FILENAME), "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "run_id": run_id,
                        "archived_at": utc_now_iso(),
                        "reason": str(candidate.get("reason") or ""),
                        "evidence": candidate.get("evidence") or {},
                        "origin": source,
                    },
                    fh,
                    indent=2,
                )
        except (OSError, TypeError, ValueError):
            pass
        archived.append({**candidate, "archived_to": target})

    return {"archived": archived, "failed": failed}


def execute_restore(plan: Dict[str, Any]) -> Dict[str, Any]:
    """Move archived runs back into the store, exactly as they were. Never raises.

    Takes only the plan: `plan_restore` already resolved every source and target against the
    configured store, and re-deriving them here would be a second place for the two to
    disagree.
    """
    restored: List[Dict[str, Any]] = []
    failed: List[Dict[str, Any]] = []

    for candidate in plan.get("selected") or []:
        source = str(candidate.get("source") or "")
        target = str(candidate.get("target") or "")
        if not os.path.isdir(source):
            failed.append({**candidate, "error": "it is no longer in the archive"})
            continue
        if os.path.exists(target):
            failed.append({**candidate, "error": "a run with that id is already in the store"})
            continue
        try:
            _move(source, target)
        except (OSError, shutil.Error) as exc:
            failed.append({**candidate, "error": f"could not move it: {exc}"})
            continue
        # The sidecar described the archiving, not the run. A restored run carries no
        # trace of having been archived, which is what "reversible" has to mean.
        try:
            os.remove(os.path.join(target, ARCHIVE_META_FILENAME))
        except OSError:
            pass
        restored.append({**candidate, "restored_to": target})

    return {"restored": restored, "failed": failed}


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def format_archive_plan(plan: Dict[str, Any], show_kept: bool = True) -> str:
    """Render an archive plan as a readable report."""
    if not plan.get("available"):
        return f"Cannot archive: {plan.get('reason')}"

    selected = plan.get("selected") or []
    kept = plan.get("kept") or []
    total = int(plan.get("total") or 0)
    share = (100.0 * len(selected) / total) if total else 0.0

    lines: List[str] = [
        f"{total} recorded run(s); {len(selected)} to archive ({share:.0f}% of the store), "
        f"{len(kept)} kept.",
        f"  Archive: {plan.get('archive_dir')}",
    ]

    if selected:
        lines.append("")
        lines.append("  WILL ARCHIVE")
        lines.append(f"    {'RUN':<26} {'STATUS':<10} {'EXECS':>5} {'SLOWEST':>8}  TASK")
        for c in selected[:40]:
            ev = c.get("evidence") or {}
            lines.append(
                f"    {c['run_id']:<26} {c['status']:<10} "
                f"{int(ev.get('executions') or 0):>5} "
                f"{float(ev.get('max_execution_seconds') or 0.0):>7.2f}s  {c['task']}"
            )
        if len(selected) > 40:
            lines.append(f"    ... and {len(selected) - 40} more")

    if kept and show_kept:
        lines.append("")
        lines.append("  KEEPING")
        for c in kept:
            lines.append(f"    {c['run_id']:<26} {c.get('reason', '')}")

    return "\n".join(lines)


def format_archive_result(result: Dict[str, Any]) -> str:
    """Render what archiving actually did."""
    archived = result.get("archived") or []
    failed = result.get("failed") or []
    lines = [f"Archived {len(archived)} run(s)."]
    if failed:
        lines.append(f"{len(failed)} could not be archived:")
        for c in failed:
            lines.append(f"  {c.get('run_id', '?'):<26} {c.get('error', '')}")
    if archived:
        lines.append("Restore them with: python -m orchestrator --restore-runs")
    return "\n".join(lines)


def format_restore_result(result: Dict[str, Any]) -> str:
    """Render what restoring actually did."""
    restored = result.get("restored") or []
    failed = result.get("failed") or []
    lines = [f"Restored {len(restored)} run(s) to the store."]
    if failed:
        lines.append(f"{len(failed)} could not be restored:")
        for c in failed:
            lines.append(f"  {c.get('run_id', '?'):<26} {c.get('error', '')}")
    return "\n".join(lines)


def format_archived_list(rows: List[Dict[str, Any]]) -> str:
    """Render the archive itself."""
    if not rows:
        return "Nothing is archived."
    lines = [f"{len(rows)} archived run(s).", ""]
    lines.append(f"  {'RUN':<26} {'ARCHIVED':<22} {'STATUS':<10} TASK")
    for row in rows:
        lines.append(
            f"  {row['run_id']:<26} {str(row.get('archived_at') or '-'):<22} "
            f"{str(row.get('status') or '-'):<10} {row.get('task') or ''}"
        )
    return "\n".join(lines)


__all__ = [
    "ARCHIVE_META_FILENAME",
    "DEFAULT_ARCHIVE_DIR",
    "REASON_MATCHED",
    "REASON_SYNTHETIC",
    "SYNTHETIC_MAX_EXECUTION_SECONDS",
    "archive_root",
    "execute_archive",
    "execute_restore",
    "format_archive_plan",
    "format_archive_result",
    "format_archived_list",
    "format_restore_result",
    "is_synthetic",
    "list_archived",
    "plan_archive",
    "plan_restore",
    "run_evidence",
]
