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
* **The branch is the artifact; the worktree is scaffolding.** Once the run's
  work is committed it lives on the branch, so the checkout is redundant and is
  removed. A dirty worktree is never removed, because then the directory *is*
  the only copy. See `finish_worktree`.
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
    base_ref: Optional[str] = None,
) -> WorkspaceInfo:
    """Create an isolated worktree for a run, or explain why it was skipped.

    Args:
        project_root: The project directory.
        run_id: Identifier used for the worktree directory and branch name.
        directory: Worktrees directory, relative to the project root or absolute.
        base_ref: Commit or branch to start from. Defaults to the current HEAD.
            A delegated task starts from its dependency's branch instead, which is
            what makes ``depends_on`` mean anything.

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
    start_point = base_ref or "HEAD"
    base_branch = base_ref or current_branch(project_root) or "HEAD"

    if base_ref and _git_text(["rev-parse", "--verify", f"{base_ref}^{{commit}}"], project_root) is None:
        return _skipped(project_root, f"base ref '{base_ref}' does not resolve to a commit")

    try:
        base.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        return _skipped(project_root, f"could not create worktrees directory: {exc}")

    try:
        completed = _run_git(
            ["worktree", "add", "-b", branch, str(worktree_path), start_point],
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


def head_commit(project_root: str, ref: str = "HEAD") -> Optional[str]:
    """Return the full commit id a ref points at, or None."""
    return _git_text(["rev-parse", f"{ref}^{{commit}}"], project_root)


def changed_paths(project_root: str, base: str, branch: str) -> List[str]:
    """Return the paths a branch changed relative to a base, sorted.

    Uses ``base...branch`` so the comparison is against the merge base: it lists what the
    branch did, not what happened elsewhere in the meantime.
    """
    text = _git_text(["diff", "--name-only", f"{base}...{branch}"], project_root)
    if not text:
        return []
    return sorted({line.strip() for line in text.splitlines() if line.strip()})


def merge_refs_into_worktree(
    workspace: WorkspaceInfo,
    refs: List[str],
) -> Dict[str, Any]:
    """Merge other branches into a session's fresh worktree, before any agent runs.

    A task that depends on another has to be able to *see* that work, so its workspace is
    built from its dependencies. This is not the auto-merge the isolation rules forbid: that
    rule protects the user's branches, and this writes only into a scratch worktree the
    orchestrator just created.

    A conflict is left unresolved and reported. Merging someone else's half-finished work by
    guessing is exactly the failure mode isolation exists to prevent, so the task stops and a
    person is asked.

    Returns:
        ``{"merged": [...], "conflicted": [...], "error": str|None}``. Never raises.
    """
    outcome: Dict[str, Any] = {"merged": [], "conflicted": [], "error": None}
    if not workspace or not workspace.get("isolated") or not refs:
        return outcome

    path = workspace.get("path") or ""
    for ref in refs:
        try:
            completed = _run_git(
                ["merge", "--no-edit", "--no-ff", ref],
                cwd=path,
            )
        except Exception as exc:
            outcome["error"] = f"could not merge {ref}: {exc}"
            return outcome

        if completed.returncode == 0:
            outcome["merged"].append(ref)
            continue

        outcome["conflicted"].append(ref)
        detail = (completed.stderr or b"").decode("utf-8", errors="replace").strip()
        outcome["error"] = detail.splitlines()[0] if detail else f"merge of {ref} conflicted"
        try:
            _run_git(["merge", "--abort"], cwd=path)
        except Exception:
            pass
        return outcome

    return outcome


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


def commits_ahead(workspace: WorkspaceInfo) -> Optional[int]:
    """Return how many commits the run's branch has that its base does not.

    Zero means the branch is an exact copy of the base commit: the run produced
    nothing, and the branch is pure noise. None means the count could not be
    determined, which callers must treat as "assume there is work".
    """
    if not workspace or not workspace.get("isolated"):
        return None
    branch = workspace.get("branch")
    base = workspace.get("base_branch") or "HEAD"
    root = workspace.get("project_root") or ""
    if not branch or not root:
        return None
    text = _git_text(["rev-list", "--count", f"{base}..{branch}"], root)
    if text is None:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def delete_branch(project_root: str, branch: str) -> bool:
    """Delete a local branch, including one that was never merged. Never raises.

    Run branches are never merged by the orchestrator, so an unmerged-branch
    guard would refuse every deletion. The caller is responsible for having
    established that the branch is safe to lose.
    """
    if not branch:
        return False
    try:
        return _run_git(["branch", "-D", branch], cwd=project_root).returncode == 0
    except Exception:
        return False


def prune_worktree_registry(project_root: str) -> bool:
    """Drop git records of worktrees whose directories are gone. Never raises."""
    try:
        return _run_git(["worktree", "prune"], cwd=project_root).returncode == 0
    except Exception:
        return False


def finish_worktree(
    workspace: WorkspaceInfo,
    keep_worktree: bool = False,
    commit: Optional[str] = None,
) -> Dict[str, Any]:
    """Apply the retention policy at the end of a run.

    Left alone, every run leaves a full second checkout and a permanent branch
    behind; a few weeks of use makes ``git worktree list`` unusable. The policy
    is the one the rest of this module already implies:

    * The work is committed, so the **branch** holds it - remove the worktree.
    * The worktree is dirty, so the **directory** holds it - keep the worktree
      and say why. Nothing is ever force-removed.
    * The branch has no commits of its own and the tree was clean - the run
      changed nothing, so delete the branch too rather than accumulating
      identical pointers at the base commit.

    Args:
        workspace: The workspace created by `create_run_worktree`.
        keep_worktree: Leave the checkout on disk regardless.
        commit: The commit `commit_worktree` produced, when it made one.

    Returns:
        A dict recording what happened: ``removed``, ``branch_deleted``, and
        ``retained_reason`` when the worktree was kept. Never raises.
    """
    outcome: Dict[str, Any] = {
        "removed": False,
        "branch_deleted": False,
        "retained_reason": None,
    }
    if not workspace or not workspace.get("isolated"):
        return outcome

    if keep_worktree:
        outcome["retained_reason"] = "workspace.keep_worktree is true"
        return outcome

    path = workspace.get("path") or ""
    if is_dirty(path):
        # Nothing was committed, so this directory is the only copy of the work.
        outcome["retained_reason"] = (
            "the worktree has uncommitted changes; it is the only copy of the "
            "run's work"
        )
        return outcome

    ahead = commits_ahead(workspace)
    if not remove_worktree(workspace):
        outcome["retained_reason"] = "git refused to remove the worktree"
        return outcome
    outcome["removed"] = True

    # A branch with no commits of its own is an alias for the base commit.
    # Deleting it loses nothing and keeps `git branch` readable.
    if not commit and ahead == 0:
        project_root = workspace.get("project_root") or ""
        branch = workspace.get("branch") or ""
        if delete_branch(project_root, branch):
            outcome["branch_deleted"] = True

    prune_worktree_registry(workspace.get("project_root") or "")
    return outcome


def reattach_run_worktree(
    project_root: str,
    run_id: str,
    branch: Optional[str] = None,
    path: Optional[str] = None,
    directory: Optional[str] = None,
) -> WorkspaceInfo:
    """Re-open the workspace of an earlier run, for ``--resume``.

    A finished run normally leaves only its branch, so resuming means checking
    that branch back out into a worktree. Reuses the original directory when it
    still exists, recreates it from the branch when it does not, and degrades to
    an un-isolated workspace when neither is possible.
    """
    if not git_available():
        return _skipped(project_root, "git is not installed or not on PATH")
    if not is_git_repo(project_root):
        return _skipped(project_root, "the project is not a git repository")

    branch = branch or branch_name_for_run(run_id)
    rel = directory or DEFAULT_WORKTREES_DIR
    base = Path(rel) if Path(rel).is_absolute() else Path(project_root) / rel
    worktree_path = Path(path) if path else base / str(run_id)

    def _info(base_branch: Optional[str]) -> WorkspaceInfo:
        return {
            "isolated": True,
            "path": str(worktree_path),
            "branch": branch,
            "base_branch": base_branch,
            "project_root": project_root,
            "reason": None,
        }

    # Still on disk and still a worktree: reuse it exactly as it stands.
    if worktree_path.is_dir() and (worktree_path / ".git").exists():
        return _info(current_branch(project_root) or "HEAD")

    if _git_text(["rev-parse", "--verify", f"refs/heads/{branch}"], project_root) is None:
        return _skipped(
            project_root,
            f"branch '{branch}' no longer exists, so the run's workspace cannot be restored",
        )

    try:
        base.mkdir(parents=True, exist_ok=True)
        prune_worktree_registry(project_root)
        completed = _run_git(
            ["worktree", "add", str(worktree_path), branch],
            cwd=project_root,
        )
    except Exception as exc:
        return _skipped(project_root, f"could not restore the run's worktree: {exc}")

    if completed.returncode != 0:
        detail = (completed.stderr or b"").decode("utf-8", errors="replace").strip()
        return _skipped(project_root, f"could not restore the run's worktree: {detail}")

    return _info(current_branch(project_root) or "HEAD")


def run_id_from_branch(branch: str) -> Optional[str]:
    """Return the run id encoded in an orchestrator run branch name, or None."""
    prefix = f"{BRANCH_PREFIX}/"
    if not branch or not branch.startswith(prefix):
        return None
    return branch[len(prefix):] or None


def list_run_branches(project_root: str) -> List[Dict[str, Any]]:
    """List every ``orchestrator/run/*`` branch with its age and tip commit.

    Returns an empty list when git is unavailable or the pattern matches
    nothing. Never raises.
    """
    text = _git_text(
        [
            "for-each-ref",
            "--format=%(refname:short)%09%(objectname:short)%09%(committerdate:unix)",
            f"refs/heads/{BRANCH_PREFIX}/",
        ],
        project_root,
    )
    if not text:
        return []

    branches: List[Dict[str, Any]] = []
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        name, commit, stamp = parts
        try:
            committed_at = int(stamp)
        except ValueError:
            continue
        branches.append(
            {
                "branch": name,
                "run_id": run_id_from_branch(name),
                "commit": commit,
                "committed_at": committed_at,
            }
        )
    return branches


def list_worktrees(project_root: str) -> List[Dict[str, Any]]:
    """List the repository worktrees as ``{path, branch}`` records.

    Parses ``git worktree list --porcelain``. Never raises.
    """
    text = _git_text(["worktree", "list", "--porcelain"], project_root)
    if not text:
        return []

    trees: List[Dict[str, Any]] = []
    current: Dict[str, Any] = {}
    for line in text.splitlines() + [""]:
        if not line.strip():
            if current.get("path"):
                trees.append(current)
            current = {}
            continue
        if line.startswith("worktree "):
            current["path"] = line[len("worktree "):].strip()
        elif line.startswith("branch "):
            ref = line[len("branch "):].strip()
            current["branch"] = ref.replace("refs/heads/", "", 1)
    return trees


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


def describe_retention(workspace: WorkspaceInfo, outcome: Dict[str, Any]) -> str:
    """Render what happened to the worktree at the end of a run."""
    if not workspace or not workspace.get("isolated"):
        return ""
    if outcome.get("branch_deleted"):
        return "Worktree removed and its branch deleted: the run changed nothing."
    if outcome.get("removed"):
        return (
            "Worktree removed; the work is on the branch. Restore it with:\n"
            f"  git worktree add {workspace.get('path')} {workspace.get('branch')}"
        )
    reason = outcome.get("retained_reason") or "kept"
    return f"Worktree kept at {workspace.get('path')} ({reason})."
