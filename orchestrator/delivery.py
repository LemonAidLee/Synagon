"""Outward integration, on the human's word (Roadmap Phase 6).

Everything a run produces stays on a local branch. This module is the only place in the
project that crosses the network, and it does so under one rule:

    **Never auto-push.** A branch reaches a remote when a person says so — never because a
    verifier said PASS, never because a scheduler finished a wave.

That is the never-auto-merge rule in network form. `workspace.py` refuses to merge agent work
into the user's branches; this refuses to publish it at all until someone presses *Ready to
Merge*.

What a delivery is
------------------
One act, recorded as one fact::

    push the branch  ->  open a pull request  ->  write down what the forge said

and then, whenever a person asks, read the forge's answer back:

    python -m orchestrator --board                    # what is ready
    python -m orchestrator --deliver <card>           # push it and open a PR
    python -m orchestrator --deliveries               # what has been delivered
    python -m orchestrator --refresh-deliveries       # re-read CI and review

Records live in ``<project_root>/.orchestrator/delivery/<key>.json``.

Load-bearing rules
------------------
* **Off until asked for.** `delivery.enabled` is false by default. A project that has not
  opted in cannot reach a remote by accident, whatever anyone approves.
* **The projection never reaches for the network.** `--board` reads *recorded* CI and review
  facts, so it is instant and works offline — and every card says how fresh its facts are. A
  refresh is a deliberate command that writes a new fact, never a side effect of looking.
* **Zero provider API keys stands.** The forge is driven through `gh`, the user's own
  locally-authenticated CLI. This project reads, stores, and transmits no token of any kind.
* **Degrade, never fail.** No `gh`, no remote, no network: the delivery is refused with a
  reason and nothing else breaks. A missing forge makes a card undeliverable, not a run
  broken.
* **A delivery is never invented.** Only work the board already places in *Ready to Merge*
  can be delivered — delivered, verified, and cleared at whatever gate was asked for.
"""

import json
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from orchestrator.status import (
    DELIVERY_MERGED,
    DELIVERY_CLOSED,
    derive_delivery_state,
    describe_delivery_state,
    summarize_checks,
)

DEFAULT_DELIVERY_DIR = ".orchestrator/delivery"

#: Seconds allowed for any single git or gh invocation. Pushing a large branch and asking a
#: forge for a review decision are both network calls; neither may hang a command forever.
NETWORK_TIMEOUT = 120

#: The fields `gh` is asked for. Everything the board derives comes from these.
PR_FIELDS = (
    "number,url,state,isDraft,mergeable,reviewDecision,statusCheckRollup,"
    "mergedAt,title,baseRefName,headRefName"
)


def utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string with a trailing Z."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------


def delivery_root(project_root: str, directory: Optional[str] = None) -> Path:
    """Resolve the directory that holds every delivery record for a project."""
    rel = directory or DEFAULT_DELIVERY_DIR
    candidate = Path(rel)
    if candidate.is_absolute():
        return candidate
    return Path(project_root) / rel


def delivery_key(
    goal_id: Optional[str] = None,
    task_id: Optional[str] = None,
    run_id: Optional[str] = None,
) -> str:
    """Return the filename-safe key one piece of deliverable work is recorded under. Pure.

    A goal's task is identified by both ids, because a task id is only unique inside its goal.
    A standalone session is identified by its run id.
    """
    if goal_id and task_id:
        raw = f"{goal_id}__{task_id}"
    else:
        raw = str(run_id or goal_id or task_id or "unknown")
    return re.sub(r"[^A-Za-z0-9._-]", "-", raw)[:120]


def _write(path: Path, payload: Dict[str, Any]) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False, default=str)
        return True
    except Exception:
        return False


def _read(path: Path) -> Optional[Dict[str, Any]]:
    try:
        if not path.is_file():
            return None
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return None


def save_delivery(
    project_root: str,
    record: Dict[str, Any],
    directory: Optional[str] = None,
) -> Dict[str, Any]:
    """Persist one delivery record. Never raises; a record that cannot be written says so."""
    path = delivery_root(project_root, directory) / f"{record.get('id')}.json"
    if not _write(path, record):
        return {**record, "degraded": "the delivery could not be recorded"}
    return record


def load_delivery(
    project_root: str,
    key: str,
    directory: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Load one delivery record by key, or by unique prefix. None when it does not exist."""
    exact = _read(delivery_root(project_root, directory) / f"{key}.json")
    if exact:
        return exact
    matches = [
        r for r in list_deliveries(project_root, directory=directory)
        if str(r.get("id", "")).startswith(key)
    ]
    return matches[0] if len(matches) == 1 else None


def list_deliveries(
    project_root: str,
    directory: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """List every delivery record, newest first. Never raises."""
    root = delivery_root(project_root, directory)
    if not root.is_dir():
        return []
    try:
        files = sorted(root.glob("*.json"), key=lambda p: p.name)
    except Exception:
        return []

    records = [r for r in (_read(path) for path in files) if r]
    records.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
    return records


def deliveries_by_key(
    project_root: str,
    directory: Optional[str] = None,
) -> Dict[str, Dict[str, Any]]:
    """Return every delivery record indexed by key, for the board to project."""
    return {str(r.get("id")): r for r in list_deliveries(project_root, directory=directory)}


# ---------------------------------------------------------------------------
# The network, behind two CLIs the user already authenticated
# ---------------------------------------------------------------------------


def _run(args: List[str], cwd: str, timeout: int = NETWORK_TIMEOUT) -> Dict[str, Any]:
    """Run a command safely and return its outcome. Never raises.

    Uses ``shell=False`` and a detached stdin, matching the subprocess contract used
    throughout the orchestrator: a command that decides to ask a question finds no one there
    and fails, rather than hanging a run forever.
    """
    try:
        completed = subprocess.run(
            args,
            cwd=cwd,
            shell=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return {"ok": False, "code": None, "out": "", "err": f"{args[0]} is not on PATH"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "code": None, "out": "", "err": f"{args[0]} timed out"}
    except Exception as exc:  # pragma: no cover - platform dependent
        return {"ok": False, "code": None, "out": "", "err": str(exc)}

    return {
        "ok": completed.returncode == 0,
        "code": completed.returncode,
        "out": (completed.stdout or b"").decode("utf-8", errors="replace").strip(),
        "err": (completed.stderr or b"").decode("utf-8", errors="replace").strip(),
    }


def gh_available() -> bool:
    """Return True when the GitHub CLI is on PATH."""
    return shutil.which("gh") is not None


def remote_exists(project_root: str, remote: str = "origin") -> bool:
    """Return True when the repository has the named remote configured."""
    result = _run(["git", "remote"], project_root, timeout=15)
    if not result["ok"]:
        return False
    return remote in [line.strip() for line in result["out"].splitlines()]


def default_base_branch(project_root: str, remote: str = "origin") -> Optional[str]:
    """Return the remote's default branch, or None when it cannot be determined."""
    result = _run(
        ["git", "symbolic-ref", "--short", f"refs/remotes/{remote}/HEAD"],
        project_root,
        timeout=15,
    )
    if result["ok"] and result["out"]:
        return result["out"].split("/", 1)[-1]
    return None


def branch_exists(project_root: str, branch: str) -> bool:
    """Return True when the branch exists locally."""
    return _run(
        ["git", "rev-parse", "--verify", f"refs/heads/{branch}"], project_root, timeout=15
    )["ok"]


def push_branch(project_root: str, branch: str, remote: str = "origin") -> Dict[str, Any]:
    """Push one branch to a remote, setting upstream. Never raises."""
    result = _run(["git", "push", "--set-upstream", remote, branch], project_root)
    return {
        "ok": bool(result["ok"]),
        "error": None if result["ok"] else (result["err"] or result["out"] or "push failed"),
    }


def read_pull_request(project_root: str, branch: str) -> Dict[str, Any]:
    """Read the forge's view of the pull request for a branch. Never raises.

    Returns:
        ``{"found": bool, "pr": {...}, "error": str|None}``. The `pr` mapping is this
        project's own shape, not the forge's, so `status.derive_delivery_state` never has to
        know which forge answered.
    """
    if not gh_available():
        return {"found": False, "pr": {}, "error": "gh is not on PATH"}

    result = _run(
        ["gh", "pr", "view", branch, "--json", PR_FIELDS], project_root
    )
    if not result["ok"]:
        message = result["err"] or result["out"] or "gh pr view failed"
        # "no pull requests found" is an answer, not a failure.
        no_pr = "no pull requests found" in message.lower() or "not found" in message.lower()
        return {"found": False, "pr": {}, "error": None if no_pr else message}

    try:
        raw = json.loads(result["out"] or "{}")
    except json.JSONDecodeError as exc:
        return {"found": False, "pr": {}, "error": f"could not read gh output: {exc}"}

    rollup = raw.get("statusCheckRollup") or []
    return {
        "found": True,
        "error": None,
        "pr": {
            "number": raw.get("number"),
            "url": raw.get("url"),
            "state": raw.get("state"),
            "draft": bool(raw.get("isDraft")),
            "mergeable": raw.get("mergeable"),
            "review_decision": raw.get("reviewDecision") or "",
            "checks": summarize_checks(rollup),
            "checks_detail": [
                {
                    "name": entry.get("name") or entry.get("context"),
                    "conclusion": entry.get("conclusion") or entry.get("state"),
                    "status": entry.get("status"),
                }
                for entry in rollup
                if isinstance(entry, dict)
            ][:20],
            "merged_at": raw.get("mergedAt"),
            "title": raw.get("title"),
            "base": raw.get("baseRefName"),
        },
    }


def create_pull_request(
    project_root: str,
    branch: str,
    title: str,
    body: str = "",
    base: str = "",
    draft: bool = True,
) -> Dict[str, Any]:
    """Open a pull request for a branch. Never raises.

    An existing pull request is not an error: it is read back instead. Opening a second one
    for the same branch would be the network equivalent of a duplicate card.
    """
    if not gh_available():
        return {"ok": False, "pr": {}, "error": "gh is not on PATH"}

    existing = read_pull_request(project_root, branch)
    if existing["found"]:
        return {"ok": True, "pr": existing["pr"], "error": None, "existed": True}

    args = ["gh", "pr", "create", "--head", branch, "--title", title, "--body", body or title]
    if base:
        args += ["--base", base]
    if draft:
        args.append("--draft")

    result = _run(args, project_root)
    if not result["ok"]:
        return {
            "ok": False,
            "pr": {},
            "error": result["err"] or result["out"] or "gh pr create failed",
        }

    # `gh pr create` prints the URL; the authoritative fields come from reading it back.
    read = read_pull_request(project_root, branch)
    if read["found"]:
        return {"ok": True, "pr": read["pr"], "error": None, "existed": False}
    return {
        "ok": True,
        "pr": {"url": result["out"].splitlines()[-1] if result["out"] else None},
        "error": None,
        "existed": False,
    }


# ---------------------------------------------------------------------------
# The act
# ---------------------------------------------------------------------------


def _history(record: Dict[str, Any], event: str, detail: str = "") -> None:
    """Append one line to a record's own history. A record keeps its whole story."""
    record.setdefault("history", []).append(
        {"at": utc_now_iso(), "event": event, "detail": detail}
    )


def deliveries_by_branch(
    project_root: str,
    directory: Optional[str] = None,
) -> Dict[str, Dict[str, Any]]:
    """Return every delivery record indexed by the local branch it describes.

    Retention asks a different question of this store than the board does: not "what happened
    to this task" but "what happened to this branch". Both are answered from the same records.
    """
    index: Dict[str, Dict[str, Any]] = {}
    for record in list_deliveries(project_root, directory=directory):
        branch = str(record.get("branch") or "")
        if branch:
            index[branch] = record
    return index


def record_branch_pruned(
    project_root: str,
    record: Dict[str, Any],
    directory: Optional[str] = None,
) -> Dict[str, Any]:
    """Note in a delivery record that its local branch has been swept (Roadmap 8.4).

    A delivery record must **outlive** the branch it describes: the fact that work merged is
    not undone by reclaiming the branch that carried it, and a board that forgot would be
    telling a person their landed work never landed. So retention does not delete the record -
    it annotates it, and the record keeps its whole story as it always has.
    """
    record = dict(record or {})
    record["local_branch_pruned_at"] = utc_now_iso()
    _history(record, "local_branch_pruned", str(record.get("branch") or ""))
    return save_delivery(project_root, record, directory=directory)


def preflight_delivery(
    project_root: str,
    branch: str,
    delivery_cfg: Dict[str, Any],
) -> Optional[str]:
    """Return why this branch cannot be delivered, or None when it can. Never raises.

    Checked before anything crosses the network, so a refusal costs nothing and reads as one
    sentence a person can act on.
    """
    if not delivery_cfg.get("enabled"):
        return (
            "delivery.enabled is false, so nothing may be pushed. "
            "Turn it on in orchestrator.yaml when you want work to reach a remote."
        )
    if not branch:
        return "there is no branch to deliver: this work was never isolated on one"
    if not shutil.which("git"):
        return "git is not on PATH"
    if not branch_exists(project_root, branch):
        return f"branch '{branch}' does not exist locally"
    remote = str(delivery_cfg.get("remote") or "origin")
    if not remote_exists(project_root, remote):
        return f"this repository has no '{remote}' remote configured"
    return None


def deliver(
    project_root: str,
    delivery_cfg: Dict[str, Any],
    branch: str,
    title: str,
    body: str = "",
    goal_id: Optional[str] = None,
    task_id: Optional[str] = None,
    run_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Push a branch and open a pull request for it, then record what happened.

    This is the only function in the project that pushes. It is called from exactly two
    places, both of which are a person acting: `--deliver`, and answering a `before_merge`
    gate when `delivery.on_approve` is set.

    Returns:
        The delivery record. `refused` is set when nothing crossed the network and says why;
        `error` is set when something was attempted and did not work. Never raises.
    """
    key = delivery_key(goal_id=goal_id, task_id=task_id, run_id=run_id)
    directory = delivery_cfg.get("directory")
    remote = str(delivery_cfg.get("remote") or "origin")

    record = load_delivery(project_root, key, directory=directory) or {
        "id": key,
        "goal_id": goal_id,
        "task_id": task_id,
        "run_id": run_id,
        "branch": branch,
        "remote": remote,
        "created_at": utc_now_iso(),
        "pushed": False,
        "pr": {},
        "history": [],
    }
    record["branch"] = branch or record.get("branch")
    record["remote"] = remote

    refusal = preflight_delivery(project_root, record.get("branch") or "", delivery_cfg)
    if refusal:
        record["refused"] = refusal
        _history(record, "refused", refusal)
        return save_delivery(project_root, record, directory=directory)
    record.pop("refused", None)

    pushed = push_branch(project_root, str(record["branch"]), remote)
    if not pushed["ok"]:
        record["error"] = pushed["error"]
        _history(record, "push_failed", str(pushed["error"]))
        return save_delivery(project_root, record, directory=directory)

    record["pushed"] = True
    record["pushed_at"] = utc_now_iso()
    record["error"] = None
    _history(record, "pushed", f"{remote}/{record['branch']}")

    base = str(delivery_cfg.get("base") or "") or (default_base_branch(project_root, remote) or "")
    record["base"] = base or None

    opened = create_pull_request(
        project_root,
        str(record["branch"]),
        title=title,
        body=body,
        base=base,
        draft=bool(delivery_cfg.get("draft", True)),
    )
    if not opened["ok"]:
        # The branch is on the remote; only the pull request failed. That is a `pushed`
        # delivery with an error attached, not a failed one — and saying so is the difference
        # between "try again" and "look for the branch you already published".
        record["error"] = opened["error"]
        _history(record, "pr_failed", str(opened["error"]))
        return save_delivery(project_root, record, directory=directory)

    record["pr"] = opened["pr"]
    record["refreshed_at"] = utc_now_iso()
    record["error"] = None
    _history(
        record,
        "pr_reused" if opened.get("existed") else "pr_opened",
        str(opened["pr"].get("url") or ""),
    )
    return save_delivery(project_root, record, directory=directory)


def refresh_delivery(
    project_root: str,
    record: Dict[str, Any],
    directory: Optional[str] = None,
) -> Dict[str, Any]:
    """Re-read one delivery's CI and review facts from the forge and record them.

    A refresh is a command, never a side effect of looking at the board: a projection that
    reached for the network would be slow, would fail offline, and would make reading a board
    an act with consequences.
    """
    branch = str(record.get("branch") or "")
    if not branch:
        return record

    state = derive_delivery_state(record)
    if state in (DELIVERY_MERGED, DELIVERY_CLOSED):
        # The forge has finished with it. Asking again cannot change the answer.
        return record

    read = read_pull_request(project_root, branch)
    if read.get("error"):
        record["error"] = read["error"]
        _history(record, "refresh_failed", str(read["error"]))
        return save_delivery(project_root, record, directory=directory)

    if read["found"]:
        record["pr"] = read["pr"]
        record["error"] = None
        _history(record, "refreshed", str(read["pr"].get("checks") or ""))
    record["refreshed_at"] = utc_now_iso()
    return save_delivery(project_root, record, directory=directory)


def refresh_all(
    project_root: str,
    directory: Optional[str] = None,
    limit: int = 0,
) -> List[Dict[str, Any]]:
    """Refresh every delivery the forge might still have news about. Never raises."""
    records = list_deliveries(project_root, directory=directory)
    if limit:
        records = records[:limit]
    return [refresh_delivery(project_root, record, directory=directory) for record in records]


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def describe_delivery(record: Dict[str, Any]) -> str:
    """Render one delivery as a single readable line."""
    if not record:
        return "(no delivery)"
    state = derive_delivery_state(record)
    pull = record.get("pr") or {}
    where = f"#{pull['number']}" if pull.get("number") else (record.get("branch") or "-")
    return f"{record.get('id')}  {state}  {where}  {pull.get('url') or record.get('refused') or ''}".strip()


def format_deliveries(records: List[Dict[str, Any]]) -> str:
    """Render every delivery as a table, with how fresh each one's facts are."""
    if not records:
        return (
            "Nothing has been delivered.\n"
            "  python -m orchestrator --board              # what is ready to merge\n"
            "  python -m orchestrator --deliver <card>     # push it and open a pull request"
        )

    lines = [f"  {'WORK':<34} {'STATE':<18} {'CHECKS':<9} {'AS OF':<21} PULL REQUEST",
             f"  {'-' * 34} {'-' * 18} {'-' * 9} {'-' * 21} {'-' * 30}"]
    for record in records:
        state = derive_delivery_state(record)
        pull = record.get("pr") or {}
        target = str(record.get("id") or "")[:34]
        checks = str(pull.get("checks") or "-")[:9]
        seen = str(record.get("refreshed_at") or record.get("pushed_at") or "-")[:21]
        url = str(pull.get("url") or record.get("refused") or record.get("error") or "")
        lines.append(f"  {target:<34} {state:<18} {checks:<9} {seen:<21} {url[:60]}")

    lines.append("")
    lines.append(
        "  These are recorded facts, not live ones. "
        "Re-read them with: python -m orchestrator --refresh-deliveries"
    )
    return "\n".join(lines)


def format_delivery(record: Dict[str, Any]) -> str:
    """Render one delivery as a short report."""
    if not record:
        return "No delivery."
    state = derive_delivery_state(record)
    pull = record.get("pr") or {}
    lines = [
        f"  {record.get('id')}",
        f"  State:    {state} - {describe_delivery_state(state)}",
        f"  Branch:   {record.get('branch')} -> {record.get('remote')}",
    ]
    if record.get("refused"):
        lines.append(f"  Refused:  {record['refused']}")
    if record.get("error"):
        lines.append(f"  Error:    {record['error']}")
    if pull.get("url"):
        lines.append(f"  PR:       {pull.get('url')}  ({pull.get('state')}"
                     f"{', draft' if pull.get('draft') else ''})")
        lines.append(f"  Checks:   {pull.get('checks')}   Review: {pull.get('review_decision') or 'none'}")
    if record.get("refreshed_at"):
        lines.append(f"  As of:    {record['refreshed_at']}")
    return "\n".join(lines)
