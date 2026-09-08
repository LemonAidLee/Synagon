"""Retention for the artifacts a run leaves behind.

Every isolated run creates a git worktree and a branch. The worktree is cleaned
up at the end of the run (see `orchestrator.workspace.finish_worktree`), but the
branch is deliberately kept: it *is* the run's work. Branches therefore
accumulate, and after a few weeks of real use `git branch` and `git worktree
list` stop being readable.

This module is the other half of that policy: a deliberate, explicit sweep the
user asks for, never something a run does on its own.

    python -m orchestrator --prune-runs --older-than 30d --keep-failed

Load-bearing rules
------------------
* **Deleting a branch destroys work.** Nothing here runs implicitly. The plan is
  always computed and shown first; `--dry-run` stops there, and an interactive
  terminal is asked to confirm before anything is deleted.
* **Never discard uncommitted work.** A branch whose worktree still exists and
  is dirty is always kept, whatever its age.
* **Never prune what is in use.** The branch checked out in the main repository
  is always kept.
* **Silence is not success.** A branch with no recorded run cannot be shown to
  have succeeded, so `--keep-failed` keeps it.
* **Merged is safer than unreviewed** (Roadmap 8.4). A branch whose pull request
  merged has had its work taken into the base branch, so the branch is now a
  duplicate rather than the only copy. It therefore qualifies on its own, shorter
  threshold, and `--keep-failed` does not hold it back — the run's own verdict
  stopped mattering the moment a person merged it.
* **The record outlives the branch.** Pruning a merged branch annotates its
  delivery record; it never deletes one. The board reads deliveries, not
  branches, so work that landed goes on saying so after the branch is gone.

The run store itself is never pruned: it is small, append-only text, and it is
the dataset `--stats` reads. Neither is the delivery store, for the same reason
one level up.
"""

import re
import time
from typing import Any, Dict, List, Optional

from orchestrator.delivery import (
    deliveries_by_branch,
    record_branch_pruned,
)
from orchestrator.status import DELIVERY_MERGED, derive_delivery_state, is_successful
from orchestrator.store import list_runs
from orchestrator.workspace import (
    current_branch,
    delete_branch,
    git_available,
    is_dirty,
    is_git_repo,
    list_run_branches,
    list_worktrees,
    prune_worktree_registry,
    remove_worktree,
)

#: Default age threshold, as accepted by `parse_age`.
DEFAULT_MAX_AGE = "30d"

#: Default age threshold for a branch whose pull request merged. Shorter than
#: `DEFAULT_MAX_AGE` on purpose: the work is in the base branch, so what is being
#: reclaimed is a duplicate. It is not zero because a merge and a `git log` on the
#: branch that carried it often happen the same afternoon.
DEFAULT_MERGED_MAX_AGE = "1d"

_AGE_PATTERN = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhdw]?)\s*$", re.IGNORECASE)

_AGE_UNITS = {
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
    "w": 604800,
    "": 86400,  # a bare number means days, which is what people mean here
}


def parse_age(text: str) -> int:
    """Parse an age like ``30d``, ``12h``, ``2w`` into seconds.

    A bare number is read as days, because that is the unit this feature is
    actually used in.

    Raises:
        ValueError: When the text is not a recognizable duration.
    """
    match = _AGE_PATTERN.match(str(text or ""))
    if not match:
        raise ValueError(
            f"Invalid age '{text}'. Use a number with an optional unit, "
            "e.g. 30d, 12h, 2w, 90m."
        )
    value, unit = match.groups()
    return int(float(value) * _AGE_UNITS[unit.lower()])


def format_age(seconds: float) -> str:
    """Render an age in the largest unit that keeps it readable."""
    seconds = max(0.0, float(seconds))
    if seconds < 3600:
        return f"{seconds / 60:.0f}m"
    if seconds < 86400:
        return f"{seconds / 3600:.0f}h"
    return f"{seconds / 86400:.1f}d"


def _run_statuses(project_root: str, runs_directory: Optional[str]) -> Dict[str, str]:
    """Map run id to the recorded status of that run, for runs the store kept."""
    statuses: Dict[str, str] = {}
    for entry in list_runs(project_root, limit=0, directory=runs_directory):
        run_id = entry.get("run_id")
        if run_id:
            statuses[str(run_id)] = str(entry.get("status") or "unknown")
    return statuses


def _worktrees_by_branch(project_root: str) -> Dict[str, str]:
    """Map branch name to the worktree path currently checking it out."""
    mapping: Dict[str, str] = {}
    for tree in list_worktrees(project_root):
        branch = tree.get("branch")
        path = tree.get("path")
        if branch and path:
            mapping[str(branch)] = str(path)
    return mapping


def plan_prune(
    project_root: str,
    older_than_seconds: int,
    keep_failed: bool = False,
    runs_directory: Optional[str] = None,
    now: Optional[float] = None,
    merged_older_than_seconds: Optional[int] = None,
    deliveries_directory: Optional[str] = None,
) -> Dict[str, Any]:
    """Decide which run branches may be deleted, and explain every exclusion.

    Args:
        project_root: The project directory.
        older_than_seconds: Only branches whose tip is at least this old qualify.
        keep_failed: Keep branches whose run did not finish successfully, so a
            failure can still be inspected. Has no effect on a merged branch: its
            work landed, which settles the question `keep_failed` is asking.
        runs_directory: Run store directory, when it is not the default.
        now: Current epoch seconds; injectable so the decision is testable.
        merged_older_than_seconds: The shorter threshold a branch whose pull
            request merged qualifies on. None means merged branches are treated
            like every other branch, which is what this did before Roadmap 8.4.
        deliveries_directory: Delivery store directory, when it is not the default.

    Returns:
        ``{"prunable": [...], "kept": [...], "total": n, "available": bool,
        "reason": str|None}``. Each candidate carries its `delivery` state and,
        when it is prunable for a reason other than age, a `prune_reason`.
        Never raises.
    """
    plan: Dict[str, Any] = {
        "prunable": [],
        "kept": [],
        "total": 0,
        "available": True,
        "reason": None,
        "older_than_seconds": int(older_than_seconds),
        "keep_failed": bool(keep_failed),
        "merged_older_than_seconds": (
            None if merged_older_than_seconds is None else int(merged_older_than_seconds)
        ),
    }

    if not git_available():
        plan["available"] = False
        plan["reason"] = "git is not installed or not on PATH"
        return plan
    if not is_git_repo(project_root):
        plan["available"] = False
        plan["reason"] = "the project is not a git repository"
        return plan

    now = time.time() if now is None else float(now)
    statuses = _run_statuses(project_root, runs_directory)
    worktrees = _worktrees_by_branch(project_root)
    checked_out = current_branch(project_root)
    try:
        deliveries = deliveries_by_branch(project_root, directory=deliveries_directory)
    except Exception:
        # Retention must survive a delivery store it cannot read: the worst that happens is
        # that every branch is judged on the ordinary threshold, which is the old behaviour.
        deliveries = {}

    branches = list_run_branches(project_root)
    plan["total"] = len(branches)

    for record in branches:
        branch = str(record.get("branch") or "")
        run_id = record.get("run_id") or ""
        age = max(0.0, now - float(record.get("committed_at") or now))
        worktree = worktrees.get(branch)
        status = statuses.get(str(run_id), "unrecorded")

        delivered = deliveries.get(branch)
        delivery_state = derive_delivery_state(delivered)
        merged = delivery_state == DELIVERY_MERGED

        candidate: Dict[str, Any] = {
            "branch": branch,
            "run_id": run_id,
            "commit": record.get("commit"),
            "age_seconds": age,
            "age": format_age(age),
            "status": status,
            "worktree": worktree,
            "delivery": delivery_state,
            "delivery_id": (delivered or {}).get("id"),
        }

        # A merged branch answers to a threshold of its own, and only to the two rules that
        # protect work rather than evidence: it cannot be the branch you are standing on, and
        # it cannot have uncommitted changes in a worktree. `keep_failed` asks "could this
        # still need inspecting?" and a merge has already answered it.
        threshold = older_than_seconds
        if merged and merged_older_than_seconds is not None:
            threshold = min(older_than_seconds, int(merged_older_than_seconds))

        keep_reason = None
        if branch == checked_out:
            keep_reason = "checked out in the main repository"
        elif age < threshold:
            keep_reason = f"newer than the threshold ({format_age(age)} old)"
        elif worktree and is_dirty(worktree):
            keep_reason = "its worktree still holds uncommitted changes"
        elif merged:
            keep_reason = None  # its work landed; nothing below applies
        elif keep_failed and status == "unrecorded":
            keep_reason = "no recorded run, so success cannot be established"
        elif keep_failed and not is_successful(status):
            keep_reason = f"run did not succeed (status: {status})"

        if keep_reason:
            candidate["keep_reason"] = keep_reason
            plan["kept"].append(candidate)
        else:
            if merged:
                candidate["prune_reason"] = "merged, so the work is in the base branch"
            plan["prunable"].append(candidate)

    plan["prunable"].sort(key=lambda c: c["age_seconds"], reverse=True)
    plan["kept"].sort(key=lambda c: c["age_seconds"], reverse=True)
    return plan


def prune_summary(plan: Dict[str, Any]) -> Dict[str, int]:
    """Summarize why branches are being kept, grouped by reason category.

    Takes the output of :func:`plan_prune` and returns counts of kept branches
    grouped by their keep-reason category.  Dynamic details embedded in two of
    the five reason strings (age, status) are collapsed into their parent
    category so callers get a stable, readable summary.

    Args:
        plan: A plan dict as returned by :func:`plan_prune`.

    Returns:
        A dict with exactly these keys, each an ``int`` count::

            {
                "checked_out": 0,
                "too_new": 0,
                "dirty_worktree": 0,
                "no_recorded_run": 0,
                "run_failed": 0,
                "other": 0,
            }

        Never raises — an unavailable plan or an empty ``kept`` list yields
        all-zero counts.
    """
    result: Dict[str, int] = {
        "checked_out": 0,
        "too_new": 0,
        "dirty_worktree": 0,
        "no_recorded_run": 0,
        "run_failed": 0,
        "other": 0,
    }

    for candidate in plan.get("kept") or []:
        reason = str(candidate.get("keep_reason") or "")
        if reason == "checked out in the main repository":
            result["checked_out"] += 1
        elif reason.startswith("newer than the threshold"):
            result["too_new"] += 1
        elif reason == "its worktree still holds uncommitted changes":
            result["dirty_worktree"] += 1
        elif reason == "no recorded run, so success cannot be established":
            result["no_recorded_run"] += 1
        elif reason.startswith("run did not succeed"):
            result["run_failed"] += 1
        else:
            result["other"] += 1

    return result


def execute_prune(
    project_root: str,
    plan: Dict[str, Any],
    deliveries_directory: Optional[str] = None,
) -> Dict[str, Any]:
    """Delete the branches a plan marked prunable, worktree first.

    A worktree that refuses to be removed (because it turned dirty between
    planning and now) blocks its branch: the directory is then the only copy.

    A branch that had a delivery record leaves that record behind, annotated with
    the fact that its local branch is gone (Roadmap 8.4). Deleting the record here
    would make the board forget that the work ever landed, which is the one thing
    retention must not be able to do.

    Returns:
        ``{"deleted": [...], "failed": [...]}`` where each entry carries the
        branch name and, on failure, the reason. Never raises.
    """
    deleted: List[Dict[str, Any]] = []
    failed: List[Dict[str, Any]] = []

    for candidate in plan.get("prunable") or []:
        branch = candidate.get("branch") or ""
        worktree = candidate.get("worktree")

        if worktree:
            removed = remove_worktree(
                {
                    "isolated": True,
                    "path": worktree,
                    "project_root": project_root,
                    "branch": branch,
                },
                force=False,
            )
            if not removed:
                failed.append(
                    {
                        **candidate,
                        "error": "its worktree could not be removed (uncommitted changes?)",
                    }
                )
                continue

        if delete_branch(project_root, branch):
            _note_pruned_delivery(project_root, candidate, deliveries_directory)
            deleted.append(candidate)
        else:
            failed.append({**candidate, "error": "git refused to delete the branch"})

    prune_worktree_registry(project_root)
    return {"deleted": deleted, "failed": failed}


def _note_pruned_delivery(
    project_root: str,
    candidate: Dict[str, Any],
    deliveries_directory: Optional[str],
) -> None:
    """Annotate the delivery record for a branch just deleted. Never raises."""
    if not candidate.get("delivery_id"):
        return
    try:
        from orchestrator.delivery import load_delivery

        record = load_delivery(
            project_root, str(candidate["delivery_id"]), directory=deliveries_directory
        )
        if record:
            record_branch_pruned(project_root, record, directory=deliveries_directory)
    except Exception:
        pass  # retention succeeded; the annotation is a courtesy, not the act


def format_prune_plan(plan: Dict[str, Any], show_kept: bool = True) -> str:
    """Render a prune plan as a readable report."""
    if not plan.get("available"):
        return f"Cannot prune: {plan.get('reason')}"

    lines: List[str] = []
    prunable = plan.get("prunable") or []
    kept = plan.get("kept") or []

    lines.append(
        f"{plan.get('total', 0)} orchestrator run branch(es); "
        f"{len(prunable)} prunable, {len(kept)} kept."
    )

    merged = [c for c in prunable if c.get("delivery") == DELIVERY_MERGED]
    if merged:
        lines.append(
            f"  {len(merged)} of them merged, so their work is already in the base branch."
        )

    if prunable:
        lines.append("")
        lines.append("  WILL DELETE")
        lines.append(
            f"    {'BRANCH':<44} {'AGE':>7}  {'STATUS':<16} {'DELIVERY':<10} WORKTREE"
        )
        for c in prunable:
            worktree = c.get("worktree") or "-"
            lines.append(
                f"    {c['branch']:<44} {c['age']:>7}  {c['status']:<16} "
                f"{str(c.get('delivery') or '-'):<10} {worktree}"
            )

    if kept and show_kept:
        lines.append("")
        lines.append("  KEEPING")
        for c in kept:
            lines.append(f"    {c['branch']:<44} {c.get('keep_reason', '')}")

    return "\n".join(lines)


def format_prune_result(result: Dict[str, Any]) -> str:
    """Render what a prune actually did."""
    deleted = result.get("deleted") or []
    failed = result.get("failed") or []
    lines = [f"Deleted {len(deleted)} branch(es)."]
    for entry in failed:
        lines.append(f"  Could not delete {entry.get('branch')}: {entry.get('error')}")
    return "\n".join(lines)
