"""The Explorer: reading the tree the agents are actually working in (Roadmap Phase 12).

Every other view in this project answers a question about *the work* - what is the state of
each card (`board.py`), who is running right now (`cockpit.py`), what did this process print
(`terminals.py`). None of them can answer the question a person asks immediately after
watching an agent finish, which is simply: **what did it write?**

This module is that, and deliberately nothing more: a read-only projection of a directory
tree and the text in it, over the roots this project already creates.

Three kinds of root, not one
----------------------------
An orchestrator that gives every run its own git worktree (`workspace.py`) has more than one
tree worth looking at, and conflating them would be the same mistake as storing a card's
column:

* **the project** - the user's own checkout, which invariant 4 says nothing here may touch;
* **each run's worktree** - ``.orchestrator/worktrees/<run_id>``, which is where an agent's
  edits actually land, and therefore the tree a person most wants after a run;
* **the run store** - ``.orchestrator/runs/<run_id>``, the artefacts and event log themselves.

`roots()` returns them as a list, each with an id the UI passes back. A caller that asks for
a path names the root it means, so "which tree is this file in" is answered by the request
rather than guessed from the path.

Read-only, and structurally so
------------------------------
Invariant 12 said watching is not steering, and §10.6 revised *where* a deliberate action may
be taken from, not whether one is required. Browsing is watching. There is no write in this
module - no ``open(..., "w")``, no ``mkdir``, no ``unlink`` - so an Explorer cannot become an
editor by accident, and `daemon.py` exposes it on GET only.

Confinement is the one security property here, and it is enforced by construction:
`resolve_within` realpaths both the root and the candidate and refuses anything that is not
underneath, so a `..` climb, an absolute path, and a symlink pointing out of the tree are all
the same answer - ``None``. Every function that takes a relative path goes through it.

Bounded, like everything else local
-----------------------------------
A repository can hold a million files and a single file can hold a gigabyte. A directory
listing is capped and *says* it was capped; a file read is capped and says how much of it you
are seeing; a file that is not text is reported as binary rather than decoded into noise.
Nothing here recurses on its own except `find_files`, which has a visit ceiling - the UI asks
for one directory at a time - so an Explorer opened on a huge monorepo costs one ``scandir``,
not a walk.
"""

import os
import subprocess
import time
from typing import Any, Dict, List, Optional, Tuple

#: Directories that are noise in every project this will ever be opened on. Hidden rather
#: than removed: ``include_hidden=True`` shows them, because "I need to look inside .git" is
#: a real thing a person does and refusing it would be the tool having an opinion.
NOISE_DIRECTORIES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        "node_modules",
        ".venv",
        "venv",
        ".idea",
        ".vs",
        ".gradle",
        ".next",
        ".turbo",
    }
)

#: The most entries one directory listing returns. A directory with more says so.
MAX_ENTRIES = 2000

#: The most bytes one file read returns. A file longer than this says so and is cut, rather
#: than being refused - the first half megabyte of a log is usually the point of opening it.
MAX_FILE_BYTES = 512 * 1024

#: How many bytes are sniffed to decide whether a file is text.
SNIFF_BYTES = 4096

#: How long ``git status`` is allowed to take before its overlay is simply not shown. A tree
#: that renders without decoration is fine; a tree that blocks on git is not.
GIT_TIMEOUT_SECONDS = 5.0

#: Extension -> the language name a highlighter is asked for. Absent means "plain text",
#: which is a correct answer rather than a missing one.
LANGUAGES: Dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".json": "json",
    ".jsonc": "json",
    ".jsonl": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".ini": "ini",
    ".cfg": "ini",
    ".md": "markdown",
    ".markdown": "markdown",
    ".rst": "rst",
    ".html": "html",
    ".htm": "html",
    ".xml": "xml",
    ".svg": "xml",
    ".css": "css",
    ".scss": "css",
    ".sass": "css",
    ".less": "css",
    ".sh": "shell",
    ".bash": "shell",
    ".zsh": "shell",
    ".ps1": "powershell",
    ".bat": "batch",
    ".cmd": "batch",
    ".sql": "sql",
    ".go": "go",
    ".rs": "rust",
    ".rb": "ruby",
    ".java": "java",
    ".kt": "kotlin",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".hpp": "cpp",
    ".cs": "csharp",
    ".php": "php",
    ".swift": "swift",
    ".lua": "lua",
    ".txt": "text",
    ".log": "text",
}

#: Files whose *name* fixes the language, extension or not.
FILENAME_LANGUAGES: Dict[str, str] = {
    "Dockerfile": "docker",
    "Makefile": "makefile",
    "LICENSE": "text",
    "CODEOWNERS": "text",
    ".gitignore": "ignore",
    ".gitattributes": "ignore",
    ".dockerignore": "ignore",
    "requirements.txt": "ini",
}


# ---------------------------------------------------------------------------
# Confinement
# ---------------------------------------------------------------------------


def _normalise(path: str) -> str:
    """A path in the one form comparisons here are made on.

    ``normcase`` matters on Windows and is a no-op elsewhere, which is exactly the behaviour
    wanted: a case-insensitive filesystem must not let two spellings of the same directory
    differ when the question being asked is "is this inside".
    """
    return os.path.normcase(os.path.realpath(path))


def resolve_within(root: str, relative: str = "") -> Optional[str]:
    """The absolute path `relative` names inside `root`, or None if it escapes.

    This is the whole of the Explorer's safety, so it is written to be boring: resolve both
    ends (which follows symlinks, junctions and ``..``), then require that the candidate is
    the root or is under it *with a separator between*. Prefix matching alone would accept a
    sibling directory whose name merely starts with the root's; the separator is what closes
    that.

    Args:
        root: The root directory, which must itself exist.
        relative: A path relative to it. Absolute paths, drive letters and ``..`` are all
            rejected rather than interpreted, because a UI has no reason to send them.

    Returns:
        The real absolute path, or None.
    """
    raw = str(relative or "").strip().replace("\\", "/")
    # Judged *before* the separators are trimmed: stripping first would quietly reinterpret
    # "/etc/passwd" as the root-relative "etc/passwd" rather than refusing it, and a check
    # that cannot see what it is checking is not a check.
    if os.path.isabs(raw) or raw.startswith("/") or (len(raw) > 1 and raw[1] == ":"):
        return None

    text = raw.strip("/")
    if text in ("", "."):
        candidate = root
    else:
        candidate = os.path.join(root, *[part for part in text.split("/") if part != "."])

    try:
        real_root = _normalise(root)
        real_candidate = _normalise(candidate)
    except (OSError, ValueError):
        return None

    if real_candidate == real_root:
        return os.path.realpath(root)
    if real_candidate.startswith(real_root + os.sep):
        return os.path.realpath(candidate)
    return None


def relative_to(root: str, absolute: str) -> str:
    """`absolute` as a forward-slash path relative to `root`. Never raises."""
    try:
        rel = os.path.relpath(absolute, root)
    except ValueError:
        return ""
    if rel == ".":
        return ""
    return rel.replace("\\", "/")


# ---------------------------------------------------------------------------
# The roots
# ---------------------------------------------------------------------------


def roots(project_root: str, config: Any = None) -> List[Dict[str, Any]]:
    """The trees worth browsing, most useful first.

    The project always comes first and always exists. A worktree appears only if it is on
    disk, so a run whose worktree was cleaned up (`finish_worktree`) simply stops being
    offered rather than becoming a broken entry.

    Args:
        project_root: The project this daemon was opened on.
        config: The resolved configuration, used only to find the configured directories.

    Returns:
        A list of ``{id, label, path, kind}``. ``id`` is what a request names.
    """
    found: List[Dict[str, Any]] = [
        {
            "id": "project",
            "label": os.path.basename(os.path.abspath(project_root)) or str(project_root),
            "path": os.path.abspath(project_root),
            "kind": "project",
        }
    ]

    worktrees_dir = ".orchestrator/worktrees"
    runs_dir = ".orchestrator/runs"
    try:
        from orchestrator.config import get_run_store_config, get_workspace_config

        worktrees_dir = get_workspace_config(config).get("directory") or worktrees_dir
        runs_dir = get_run_store_config(config).get("directory") or runs_dir
    except Exception:
        pass  # degrade, never fail: the defaults are what those modules default to

    base = worktrees_dir
    if not os.path.isabs(base):
        base = os.path.join(project_root, worktrees_dir)
    try:
        entries = sorted(os.scandir(base), key=lambda item: item.name)
    except (OSError, ValueError):
        entries = []
    for entry in entries:
        try:
            if not entry.is_dir():
                continue
        except OSError:
            continue
        found.append(
            {
                "id": "worktree:%s" % entry.name,
                "label": entry.name,
                "path": os.path.abspath(entry.path),
                "kind": "worktree",
            }
        )

    store = runs_dir if os.path.isabs(runs_dir) else os.path.join(project_root, runs_dir)
    if os.path.isdir(store):
        found.append(
            {
                "id": "runs",
                "label": "run store",
                "path": os.path.abspath(store),
                "kind": "runs",
            }
        )
    return found


def root_path(project_root: str, config: Any, root_id: str) -> Optional[str]:
    """The directory a root id names, or None if no root has that id.

    Lookup by identity rather than by joining a caller-supplied path onto a base: an id that
    is not in `roots()` is not a root, which is one fewer place a traversal could begin.
    """
    wanted = str(root_id or "project")
    for root in roots(project_root, config):
        if root["id"] == wanted:
            return root["path"]
    return None


# ---------------------------------------------------------------------------
# Listing a directory
# ---------------------------------------------------------------------------


def language_of(name: str) -> str:
    """The language a file's name implies, or "" for plain text. Pure."""
    if name in FILENAME_LANGUAGES:
        return FILENAME_LANGUAGES[name]
    _, ext = os.path.splitext(name)
    return LANGUAGES.get(ext.lower(), "")


def _entry_sort_key(entry: Dict[str, Any]) -> Tuple[int, str]:
    """Directories first, then case-insensitively by name - the order every file tree uses."""
    return (0 if entry["kind"] == "dir" else 1, str(entry["name"]).lower())


def list_directory(
    root: str,
    relative: str = "",
    include_hidden: bool = False,
    max_entries: int = MAX_ENTRIES,
) -> Dict[str, Any]:
    """One directory's children. Never recurses, never raises.

    Args:
        root: The confining root.
        relative: The directory inside it, "" for the root itself.
        include_hidden: Show dotfiles and `NOISE_DIRECTORIES`.
        max_entries: Cap on how many children come back.

    Returns:
        ``{path, entries, total, truncated}``, or ``{error, path}``.
    """
    target = resolve_within(root, relative)
    if target is None:
        return {"error": "that path is outside this root", "path": str(relative or "")}
    if not os.path.isdir(target):
        return {"error": "not a directory", "path": str(relative or "")}

    entries: List[Dict[str, Any]] = []
    total = 0
    try:
        scanned = list(os.scandir(target))
    except (OSError, ValueError) as exc:
        return {"error": "could not read that directory: %s" % exc, "path": str(relative or "")}

    for item in scanned:
        name = item.name
        hidden = name.startswith(".") or name in NOISE_DIRECTORIES
        if hidden and not include_hidden:
            continue
        total += 1
        if len(entries) >= max(1, int(max_entries)):
            continue
        try:
            is_dir = item.is_dir()
            stat = item.stat()
            size = 0 if is_dir else int(stat.st_size)
            mtime = float(stat.st_mtime)
        except OSError:
            # A file that vanished between scandir and stat is not an error worth failing a
            # whole listing over - it is simply not there any more.
            is_dir = False
            size = 0
            mtime = 0.0
        entries.append(
            {
                "name": name,
                "path": relative_to(root, item.path),
                "kind": "dir" if is_dir else "file",
                "size": size,
                "modified": mtime,
                "language": "" if is_dir else language_of(name),
                "hidden": hidden,
            }
        )

    entries.sort(key=_entry_sort_key)
    return {
        "path": relative_to(root, target),
        "entries": entries,
        "total": total,
        "truncated": total > len(entries),
    }


# ---------------------------------------------------------------------------
# Reading a file
# ---------------------------------------------------------------------------


def looks_binary(sample: bytes) -> bool:
    """Whether a sample of a file's first bytes reads as binary. Pure.

    The NUL byte is the one reliable signal across formats, and it is what ``git`` itself
    uses. A file that decodes as UTF-8 and holds no NUL is shown; anything else is named as
    binary rather than mangled into replacement characters.
    """
    if b"\x00" in sample:
        return True
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError:
        # A truncated multi-byte character at the sniff boundary is not binary; a genuinely
        # undecodable byte earlier in the sample is.
        try:
            sample[: max(0, len(sample) - 4)].decode("utf-8")
        except UnicodeDecodeError:
            return True
    return False


def read_file(root: str, relative: str, max_bytes: int = MAX_FILE_BYTES) -> Dict[str, Any]:
    """One file's text, bounded. Never raises.

    Returns:
        ``{path, name, language, size, binary, text, truncated, lines}``, or ``{error, path}``.
    """
    target = resolve_within(root, relative)
    if target is None:
        return {"error": "that path is outside this root", "path": str(relative or "")}
    if not os.path.isfile(target):
        return {"error": "not a file", "path": str(relative or "")}

    try:
        size = os.path.getsize(target)
    except OSError as exc:
        return {"error": "could not read that file: %s" % exc, "path": str(relative or "")}

    name = os.path.basename(target)
    cap = max(0, int(max_bytes))
    common: Dict[str, Any] = {
        "path": relative_to(root, target),
        "name": name,
        "language": language_of(name),
        "size": int(size),
    }

    try:
        with open(target, "rb") as handle:
            head = handle.read(SNIFF_BYTES)
            if looks_binary(head):
                common.update({"binary": True, "text": "", "truncated": False, "lines": 0})
                return common
            # Sliced after the sniff rather than read short: the binary check wants its full
            # sample even when the caller asked for fewer bytes than that, and the cap is a
            # promise about what comes back, not about how much was looked at.
            body = (head + handle.read(max(0, cap - len(head))))[:cap]
    except OSError as exc:
        return {"error": "could not read that file: %s" % exc, "path": str(relative or "")}

    text = body.decode("utf-8", errors="replace")
    common.update(
        {
            "binary": False,
            "text": text,
            "truncated": size > len(body),
            "lines": text.count("\n") + (0 if not text or text.endswith("\n") else 1),
        }
    )
    return common


# ---------------------------------------------------------------------------
# The git overlay
# ---------------------------------------------------------------------------

#: Porcelain's status letter -> the one word a tree decorates with.
_GIT_CODES = {
    "?": "untracked",
    "!": "ignored",
    "A": "added",
    "M": "modified",
    "D": "deleted",
    "R": "renamed",
    "C": "copied",
    "U": "conflicted",
}


def git_status(root: str, timeout: float = GIT_TIMEOUT_SECONDS) -> Dict[str, str]:
    """Which paths under `root` git considers changed. Never raises; {} when it cannot tell.

    This is decoration, not fact: a tree that renders without it is correct, just less
    informative. That is why every failure here - no git, not a repository, a slow status on
    a huge tree - returns an empty overlay instead of an error the UI has to handle.
    """
    if not os.path.isdir(root):
        return {}
    try:
        completed = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=normal"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=max(0.1, float(timeout)),
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if completed.returncode != 0:
        return {}

    overlay: Dict[str, str] = {}
    for line in (completed.stdout or "").splitlines():
        if len(line) < 4:
            continue
        index_code, worktree_code, path = line[0], line[1], line[3:]
        if " -> " in path:  # a rename reports "old -> new"; the new name is the one shown
            path = path.split(" -> ", 1)[1]
        path = path.strip().strip('"').replace("\\", "/").rstrip("/")
        state = _GIT_CODES.get(index_code.strip() or worktree_code.strip() or "")
        if state and path:
            overlay[path] = state
    return overlay


def decorate(entries: List[Dict[str, Any]], overlay: Dict[str, str]) -> List[Dict[str, Any]]:
    """Attach git state to a listing. Pure.

    A directory takes a state if anything under it has one - "something in here changed" is
    what a collapsed folder needs to be able to say, and is why this is not a plain lookup.
    """
    decorated: List[Dict[str, Any]] = []
    for entry in entries:
        item = dict(entry)
        path = str(item.get("path") or "")
        if item.get("kind") == "dir":
            prefix = path + "/"
            item["git"] = (
                "modified"
                if any(key == path or key.startswith(prefix) for key in overlay)
                else ""
            )
        else:
            item["git"] = overlay.get(path, "")
        decorated.append(item)
    return decorated


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


def find_files(
    root: str,
    query: str,
    limit: int = 200,
    include_hidden: bool = False,
    max_visited: int = 20000,
) -> Dict[str, Any]:
    """Paths under `root` whose name contains `query`, case-insensitively.

    A file *name* search, not a content search: it is what a command palette's "go to file"
    needs, it costs one walk, and it cannot be turned into a way to read a file the
    confinement above would otherwise refuse.

    `max_visited` is the ceiling that makes this safe to call on a monorepo from a keystroke.
    """
    needle = str(query or "").strip().lower()
    if not needle:
        return {"matches": [], "truncated": False, "visited": 0}

    matches: List[Dict[str, Any]] = []
    visited = 0
    cap = max(1, int(limit))
    ceiling = max(1, int(max_visited))
    stopped = False

    for current, dirnames, filenames in os.walk(root):
        if not include_hidden:
            dirnames[:] = [
                name
                for name in dirnames
                if not name.startswith(".") and name not in NOISE_DIRECTORIES
            ]
        for name in filenames:
            visited += 1
            if visited > ceiling or len(matches) >= cap:
                stopped = True
                break
            if not include_hidden and name.startswith("."):
                continue
            if needle not in name.lower():
                continue
            matches.append(
                {
                    "name": name,
                    "path": relative_to(root, os.path.join(current, name)),
                    "kind": "file",
                    "language": language_of(name),
                }
            )
        if stopped:
            break

    # An exact name match first, then shallowest: in a "go to file" list, the shallower match
    # is nearly always the one meant, and an exact name match always is.
    matches.sort(key=lambda m: (m["name"].lower() != needle, m["path"].count("/"), m["path"]))
    return {"matches": matches, "truncated": stopped, "visited": visited}


# ---------------------------------------------------------------------------
# The one read the panel makes
# ---------------------------------------------------------------------------


def snapshot(
    project_root: str,
    config: Any,
    root_id: str = "project",
    path: str = "",
    include_hidden: bool = False,
) -> Dict[str, Any]:
    """One directory, decorated, with the root list beside it - the Explorer's single read.

    Assembled here rather than in `daemon.py` for the same reason `cockpit()` is: a panel
    built from three independently-timed requests shows three different moments at once.
    """
    available = roots(project_root, config)
    base = root_path(project_root, config, root_id)
    if base is None:
        return {"error": "no root '%s'" % root_id, "roots": available, "root": root_id}

    listing = list_directory(base, path, include_hidden=include_hidden)
    listing["roots"] = available
    listing["root"] = root_id
    if listing.get("error"):
        return listing

    listing["entries"] = decorate(listing.get("entries") or [], git_status(base))
    listing["root_path"] = base
    listing["read_at"] = time.time()
    return listing
