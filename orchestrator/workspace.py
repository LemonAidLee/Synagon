"""Git-backed workspace isolation (Tier 0 #3).

Without isolation, the implementer and up to `max_repair_attempts` repair passes
write directly into the live project directory with no diff, no rollback, and no
record of what changed. This module gives each run its own git worktree on its
own branch, so agent edits are contained, reviewable, and discardable.

Model
-----
One run gets one worktree::

    <project_root>/.orchestrator/worktrees/<run_id>     # working tree
    orchestrator/run/<run_id>                            # branch

Every agent in the run executes with ``cwd`` set to that worktree, so the
implementer writes there and the verifier inspects the same tree it wrote.

Load-bearing rules
------------------
* **Never touch the user's checkout.** The run's branch is created from the
  current HEAD; the original working tree and branch are never modified,
  checked out, or reset.
* **Never force-delete a dirty worktree.** Cleanup refuses to remove a worktree
  holding uncommitted work; the user's data outranks tidiness. This mirrors the
  same rule in Agent Orchestrator.
* **Never auto-merge.** Merging agent work back is a human decision. The run
  reports the branch and the command to review it.
* **Degrade, never fail.** If git is missing, the project is not a repository,
  or the worktree cannot be created, the run proceeds in the project directory
  with a warning. Isolation is a safety feature, not a precondition.
"""

import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, TypedDict

DEFAULT_WORKTREES_DIR = ".orchestrator/worktrees"
BRANCH_PREFIX = "orchestrator/run"

#: Seconds allowed for any single git invocation.
GIT_TIMEOUT = 60


class WorkspaceInfo(TypedDict, total=False):
    """The workspace an isolated run executes in."""
    isolated: bool           # True when a dedicated worktree was created
    path: str                # Directory agents should run in
    branch: Optional[str]    # Branch the worktree is checked out on
    base_branch: Optional[str]  # Branch or commit the run started from
    project_root: str        # The original project directory
    reason: Optional[str]    # Why isolation was skipped, when it was


def git_available() -> bool:
    """Return True when a git executable is on PATH."""
    return shutil.which("git") is not None


def _run_git(
    args: List[str],
    cwd: str,
    timeout: int = GIT_TIMEOUT,
) -> subprocess.CompletedProcess:
    """Run a git command safely and return the completed process.

    Uses ``shell=False`` and a detached stdin, matching the subprocess safety
    contract used throughout the orchestrator.
    """
    return subprocess.run(
        ["git"] + args,
        cwd=cwd,
        shell=False,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=timeout,
    )


def _git_text(args: List[str], cwd: str) -> Optional[str]:
    """Run git and return stripped stdout, or None when the command fails."""
    try:
        completed = _run_git(args, cwd)
    except Exception:
        return None
    if completed.returncode != 0:
        return None
    return (completed.stdout or b"").decode("utf-8", errors="replace").strip()


def is_git_repo(path: str) -> bool:
    """Return True when `path` is inside a git working tree."""
    if not git_available():
        return False
    return _git_text(["rev-parse", "--is-inside-work-tree"], path) == "true"


def has_commits(path: str) -> bool:
    """Return True when the repository has at least one commit.

    A worktree cannot be created from an empty repository, because there is no
    commit to branch from.
    """
    return _git_text(["rev-parse", "--verify", "HEAD"], path) is not None


def current_branch(path: str) -> Optional[str]:
    """Return the checked-out branch name, or None when detached or unavailable."""
    name = _git_text(["rev-parse", "--abbrev-ref", "HEAD"], path)
    if not name or name == "HEAD":
        return None
    return name


def is_dirty(path: str) -> bool:
    """Return True when the working tree has uncommitted changes.

    Errs on the side of caution: if the status cannot be determined, the tree is
    treated as dirty so that cleanup refuses to delete it.
    """
    status = _git_text(["status", "--porcelain"], path)
    if status is None:
        return True
    return bool(status.strip())


def changed_files(path: str) -> List[str]:
    """Return the porcelain status lines for the worktree, or an empty list."""
    status = _git_text(["status", "--porcelain"], path)
    if not status:
        return []
    return [line.strip() for line in status.splitlines() if line.strip()]


def branch_name_for_run(run_id: str) -> str:
    """Return the branch name a run's worktree is checked out on."""
    safe = "".join(ch if (ch.isalnum() or ch in "-_.") else "-" for ch in str(run_id))
    return f"{BRANCH_PREFIX}/{safe}"


def _skipped(project_root: str, reason: str) -> WorkspaceInfo:
    """Return a WorkspaceInfo describing an un-isolated run."""
    return {
        "isolated": False,
        "path": project_root,
        "branch": None,
        "base_branch": None,
        "project_root": project_root,
        "reason": reason,
    }


def create_run_worktree(
    project_root: str,
    run_id: str,
    directory: Optional[str] = None,
) -> WorkspaceInfo:
    """Create an isolated worktree for a run, or explain why it was skipped.

    Args:
        project_root: The project directory.
        run_id: Identifier used for the worktree directory and branch name.
        directory: Worktrees directory, relative to the project root or absolute.

    Returns:
        A WorkspaceInfo. ``isolated`` is False when the run must proceed in the
        project directory, with ``reason`` explaining why. This function never
        raises.
    """
    if not git_available():
        return _skipped(project_root, "git is not installed or not on PATH")

    if not is_git_repo(project_root):
        return _skipped(
            project_root,
            "the project is not a git repository (run 'git init' to enable isolation)",
        )

    if not has_commits(project_root):
        return _skipped(
            project_root,
            "the repository has no commits yet; make an initial commit to enable isolation",
        )

    rel = directory or DEFAULT_WORKTREES_DIR
    base = Path(rel) if Path(rel).is_absolute() else Path(project_root) / rel
    worktree_path = base / str(run_id)
    branch = branch_name_for_run(run_id)
    base_branch = current_branch(project_root) or "HEAD"

    try:
        base.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        return _skipped(project_root, f"could not create worktrees directory: {exc}")

    try:
        completed = _run_git(
            ["worktree", "add", "-b", branch, str(worktree_path), "HEAD"],
            cwd=project_root,
        )
    except Exception as exc:
        return _skipped(project_root, f"git worktree add failed: {exc}")

    if completed.returncode != 0:
        detail = (completed.stderr or b"").decode("utf-8", errors="replace").strip()
        return _skipped(project_root, f"git worktree add failed: {detail or 'unknown error'}")

    return {
        "isolated": True,
        "path": str(worktree_path),
        "branch": branch,
        "base_branch": base_branch,
        "project_root": project_root,
        "reason": None,
    }


def summarize_worktree(workspace: WorkspaceInfo) -> Dict[str, Any]:
    """Summarize what a run changed inside its worktree.

    Returns counts and the porcelain lines, so the run's outcome can record what
    the agents actually touched without diffing file contents into memory.
    """
    if not workspace or not workspace.get("isolated"):
        return {"isolated": False, "changed_files": [], "change_count": 0}

    path = workspace.get("path") or ""
    lines = changed_files(path)
    return {
        "isolated": True,
        "path": path,
        "branch": workspace.get("branch"),
        "base_branch": workspace.get("base_branch"),
        "changed_files": lines,
        "change_count": len(lines),
        "dirty": bool(lines),
    }


def commit_worktree(
    workspace: WorkspaceInfo,
    message: str,
) -> Optional[str]:
    """Commit everything in the run's worktree onto its own branch.

    Committing makes the run's work reviewable as a diff and lets the worktree
    be removed without losing anything — the branch keeps the work.

    Args:
        workspace: The workspace created by `create_run_worktree`.
        message: Commit message.

    Returns:
        The new commit's short hash, or None when nothing was committed or the
        commit failed. Never raises.
    """
    if not workspace or not workspace.get("isolated"):
        return None
    path = workspace.get("path") or ""
    if not changed_files(path):
        return None

    try:
        if _run_git(["add", "-A"], cwd=path).returncode != 0:
            return None
        completed = _run_git(["commit", "-m", message, "--no-verify"], cwd=path)
        if completed.returncode != 0:
            return None
    except Exception:
        return None

    return _git_text(["rev-parse", "--short", "HEAD"], path)


def remove_worktree(workspace: WorkspaceInfo, force: bool = False) -> bool:
    """Remove a run's worktree, refusing to discard uncommitted work.

    Args:
        workspace: The workspace created by `create_run_worktree`.
        force: Remove even when the worktree is dirty. Off by default and never
            set by the orchestrator itself — losing an agent's work silently is
            worse than leaving a directory behind.

    Returns:
        True when the worktree was removed. Never raises.
    """
    if not workspace or not workspace.get("isolated"):
        return False

    path = workspace.get("path") or ""
    project_root = workspace.get("project_root") or ""

    if not force and is_dirty(path):
        return False

    args = ["worktree", "remove", path]
    if force:
        args.append("--force")
    try:
        return _run_git(args, cwd=project_root).returncode == 0
    except Exception:
        return False


def describe_workspace(workspace: WorkspaceInfo) -> str:
    """Render a short, actionable description of where a run's work lives."""
    if not workspace:
        return "Workspace: (unknown)"
    if not workspace.get("isolated"):
        reason = workspace.get("reason") or "isolation disabled"
        return (
            f"Workspace: {workspace.get('path')} (NOT isolated - {reason})\n"
            f"Agent edits were written directly into the project directory."
        )
    return (
        f"Workspace: {workspace.get('path')} (isolated)\n"
        f"Branch:    {workspace.get('branch')} (from {workspace.get('base_branch')})\n"
        f"Review:    git diff {workspace.get('base_branch')}...{workspace.get('branch')}\n"
        f"Merge:     git merge {workspace.get('branch')}"
    )
