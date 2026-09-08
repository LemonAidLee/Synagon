"""Human approval gates (Roadmap Phase 5).

`BLOCKED` is the orchestrator discovering that a human is needed. An **approval gate** is the
configuration saying so in advance: *stop here and ask, whatever the agents think*.

    approval:
      gates:
        after_decomposition: true    # look at the plan before any task runs
        before_task: false           # authorise each task individually
        before_merge: true           # delivered work waits for a person to clear it
      auto_approve: false            # the guardrail, made explicit

How a gate behaves
------------------
A gate does not block a process waiting for a keystroke. A run that hangs on stdin cannot be
scheduled, watched, or resumed, and the whole point of a gate is that the person may not be
there. Instead a gate records a **pending approval** — a durable fact — and the work stops at a
clean boundary. The person decides later::

    python -m orchestrator --approvals              # what is waiting
    python -m orchestrator --approve <id>           # clear it
    python -m orchestrator --reject <id> --note ... # refuse it, with a reason
    python -m orchestrator --resume-goal <goal id>  # carry on

Load-bearing rules
------------------
* **A decision is a fact, not a status.** Approved, rejected, and auto-approved are all
  recorded, with who and when. `auto_approve` does not skip the gate; it answers it, and the
  record says so — a guardrail you cannot tell was on is not a guardrail.
* **Silence is not consent.** A gate with no decision is pending, and pending stops the work.
* **The store never breaks a run.** Every filesystem operation is wrapped. A gate that cannot
  be recorded is reported and treated as *pending*, because failing open would quietly remove
  the check a person asked for.
"""

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_APPROVALS_DIR = ".orchestrator/approvals"

#: The points a gate can be configured at.
GATE_AFTER_DECOMPOSITION = "after_decomposition"
GATE_BEFORE_TASK = "before_task"
GATE_BEFORE_MERGE = "before_merge"

VALID_GATES = (GATE_AFTER_DECOMPOSITION, GATE_BEFORE_TASK, GATE_BEFORE_MERGE)

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"

#: Human-readable prompts, so the CLI and a UI ask the same question.
GATE_QUESTIONS: Dict[str, str] = {
    GATE_AFTER_DECOMPOSITION: "Run these tasks?",
    GATE_BEFORE_TASK: "Start this task?",
    GATE_BEFORE_MERGE: "Is this work ready to merge?",
}


def utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string with a trailing Z."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def approvals_root(project_root: str, directory: Optional[str] = None) -> Path:
    """Resolve the directory that holds every approval for a project."""
    rel = directory or DEFAULT_APPROVALS_DIR
    candidate = Path(rel)
    if candidate.is_absolute():
        return candidate
    return Path(project_root) / rel


def _approval_id(gate: str) -> str:
    """Generate a short, sortable, readable approval id."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{gate.replace('_', '-')}-{uuid.uuid4().hex[:4]}"


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


def request_approval(
    project_root: str,
    gate: str,
    subject: str,
    detail: str = "",
    goal_id: Optional[str] = None,
    task_id: Optional[str] = None,
    run_id: Optional[str] = None,
    branch: Optional[str] = None,
    auto_approve: bool = False,
    directory: Optional[str] = None,
) -> Dict[str, Any]:
    """Record that a gate was reached, and return the resulting approval.

    When `auto_approve` is set the approval is created already answered, and says so. The gate
    still happened; a guardrail whose effect is invisible is not one.

    Returns:
        The approval record. Its ``status`` is what the caller must act on. A record that could
        not be written comes back pending with ``degraded`` set, because failing open would
        silently drop a check the user asked for.
    """
    approval: Dict[str, Any] = {
        "id": _approval_id(gate),
        "gate": gate,
        "question": GATE_QUESTIONS.get(gate, "Approve?"),
        "subject": subject,
        "detail": detail,
        "goal_id": goal_id,
        "task_id": task_id,
        "run_id": run_id,
        "branch": branch,
        "project_root": project_root,
        "created_at": utc_now_iso(),
        "status": STATUS_APPROVED if auto_approve else STATUS_PENDING,
        "decided_at": utc_now_iso() if auto_approve else None,
        "decided_by": "auto_approve" if auto_approve else None,
        "note": "approval.auto_approve is set" if auto_approve else None,
    }

    path = approvals_root(project_root, directory) / f"{approval['id']}.json"
    if not _write(path, approval):
        approval["degraded"] = "the approval could not be recorded; treating it as pending"
        approval["status"] = STATUS_PENDING
    return approval


def list_approvals(
    project_root: str,
    status: Optional[str] = None,
    goal_id: Optional[str] = None,
    directory: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """List approvals, newest first, optionally filtered. Never raises."""
    root = approvals_root(project_root, directory)
    if not root.is_dir():
        return []

    found: List[Dict[str, Any]] = []
    try:
        files = sorted(root.glob("*.json"), key=lambda p: p.name, reverse=True)
    except Exception:
        return []

    for path in files:
        record = _read(path)
        if not record:
            continue
        if status and record.get("status") != status:
            continue
        if goal_id and record.get("goal_id") != goal_id:
            continue
        found.append(record)
    return found


def load_approval(
    project_root: str,
    approval_id: str,
    directory: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Load one approval by id, or by unique prefix. None when it does not exist."""
    root = approvals_root(project_root, directory)
    exact = _read(root / f"{approval_id}.json")
    if exact:
        return exact

    matches = [r for r in list_approvals(project_root, directory=directory)
               if str(r.get("id", "")).startswith(approval_id)]
    return matches[0] if len(matches) == 1 else None


def resolve_approval(
    project_root: str,
    approval_id: str,
    decision: str,
    note: str = "",
    decided_by: str = "human",
    directory: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Approve or reject a pending approval, recording who decided and when.

    Returns the updated record, or None when the approval does not exist. A decision is never
    silently overwritten: an approval that was already answered comes back unchanged, with
    ``already_decided`` set, so a caller can say so rather than pretend it acted.
    """
    record = load_approval(project_root, approval_id, directory=directory)
    if not record:
        return None

    if record.get("status") != STATUS_PENDING:
        return {**record, "already_decided": True}

    record["status"] = STATUS_APPROVED if decision == STATUS_APPROVED else STATUS_REJECTED
    record["decided_at"] = utc_now_iso()
    record["decided_by"] = decided_by
    record["note"] = note or None

    path = approvals_root(project_root, directory) / f"{record['id']}.json"
    if not _write(path, record):
        return {**record, "degraded": "the decision could not be recorded"}
    return record


def find_approval(
    approvals: List[Dict[str, Any]],
    gate: str,
    goal_id: Optional[str] = None,
    task_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Return the most recent approval matching a gate and its subject. Pure."""
    for record in approvals or []:
        if record.get("gate") != gate:
            continue
        if goal_id is not None and record.get("goal_id") != goal_id:
            continue
        if task_id is not None and record.get("task_id") != task_id:
            continue
        return record
    return None


def gate_state(approval: Optional[Dict[str, Any]]) -> str:
    """Return what a gate's record means for the work: pending, approved, or rejected. Pure."""
    if not approval:
        return STATUS_PENDING
    status = str(approval.get("status") or STATUS_PENDING)
    return status if status in (STATUS_PENDING, STATUS_APPROVED, STATUS_REJECTED) else STATUS_PENDING


def describe_approval(approval: Dict[str, Any]) -> str:
    """Render one approval as a single readable line."""
    if not approval:
        return "(no approval)"
    where = approval.get("task_id") or approval.get("goal_id") or approval.get("run_id") or "-"
    decided = ""
    if approval.get("status") != STATUS_PENDING:
        decided = f" by {approval.get('decided_by')} at {approval.get('decided_at')}"
    return (
        f"{approval.get('id')}  {approval.get('gate')}  {approval.get('status')}{decided}  "
        f"[{where}] {approval.get('subject')}"
    )
