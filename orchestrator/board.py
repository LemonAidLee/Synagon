"""The board (Roadmap Phase 4).

Agent Orchestrator's central idea is a Kanban whose card positions are **derived** from session
state, review, and CI — never hand-maintained. That is invariant 1, and this project has been
built around it from the start, so the board is not new machinery: it is a projection over
facts that already exist.

    python -m orchestrator --board
    python -m orchestrator --board --json

Columns
-------
======================  ==================================================================
Queued                  a task whose session has not started
Working                 a session is running now
Needs You               blocked, a collision, a gate, a red check, or a change request
In Review               a `before_merge` gate is waiting, or a pull request is in flight
Ready to Merge          delivered, verified, and cleared (or no gate asked)
Merged                  its pull request was merged; the work has landed
Stalled                 failed, errored, or skipped because something else did
======================  ==================================================================

Once work has been delivered (Roadmap Phase 6) the forge owns facts this project does not —
CI and review — and they move the card too: a red check or a requested change is a *Needs
You*, a pull request still being chewed on is *In Review*, and a merged one has left the
queue entirely. Those facts are read from the **recorded** delivery, never from the network,
which is what keeps this projection pure and this board instant and offline.

Load-bearing rules
------------------
* **Pure.** Every function here takes facts and returns a projection. No I/O; the caller reads
  the stores. That is what lets the CLI, `--serve`, and any future UI show the *same* board
  without a second implementation drifting from the first.
* **Nothing is invented.** A card exists because a task or a session exists. A column is a
  function of recorded state, never of what a scheduler believed at the time.
* **Sessions without a goal are cards too.** Most work still starts as a single run, and a
  board that only showed delegated goals would be a board of the minority.
"""

from typing import Any, Dict, List, Optional

from orchestrator.approvals import (
    GATE_BEFORE_MERGE,
    STATUS_APPROVED,
    STATUS_PENDING,
    STATUS_REJECTED,
)
from orchestrator.delivery import delivery_key
from orchestrator.status import (
    DELIVERY_CHANGES_REQUESTED,
    DELIVERY_CHECKS_FAILED,
    DELIVERY_CHECKS_RUNNING,
    DELIVERY_CLOSED,
    DELIVERY_FAILED,
    DELIVERY_LOCAL,
    DELIVERY_MERGED,
    DELIVERY_PR_OPEN,
    STATUS_BLOCKED,
    STATUS_COMPLETED,
    TASK_AWAITING_APPROVAL,
    TASK_BLOCKED,
    TASK_DONE,
    TASK_FAILED,
    TASK_NEEDS_ATTENTION,
    TASK_QUEUED,
    TASK_RUNNING,
    TASK_SKIPPED_BUDGET,
    TASK_SKIPPED_CONFLICT,
    TASK_SKIPPED_DEPENDENCY,
    derive_delivery_state,
    derive_task_state,
    is_successful,
)

COLUMN_QUEUED = "Queued"
COLUMN_WORKING = "Working"
COLUMN_NEEDS_YOU = "Needs You"
COLUMN_IN_REVIEW = "In Review"
COLUMN_READY = "Ready to Merge"
COLUMN_MERGED = "Merged"
COLUMN_STALLED = "Stalled"

#: Left to right, as a person reads them.
COLUMNS = (
    COLUMN_QUEUED,
    COLUMN_WORKING,
    COLUMN_NEEDS_YOU,
    COLUMN_IN_REVIEW,
    COLUMN_READY,
    COLUMN_MERGED,
    COLUMN_STALLED,
)

#: Where a delivered card sits once the forge has an opinion (Roadmap Phase 6). States not
#: listed here leave the card where the merge gate put it: `local` and `pushed` are not the
#: forge disagreeing, they are the forge not having been asked.
_DELIVERY_COLUMNS: Dict[str, str] = {
    DELIVERY_MERGED: COLUMN_MERGED,
    DELIVERY_CHECKS_FAILED: COLUMN_NEEDS_YOU,
    DELIVERY_CHANGES_REQUESTED: COLUMN_NEEDS_YOU,
    DELIVERY_CLOSED: COLUMN_NEEDS_YOU,
    DELIVERY_FAILED: COLUMN_NEEDS_YOU,
    DELIVERY_PR_OPEN: COLUMN_IN_REVIEW,
    DELIVERY_CHECKS_RUNNING: COLUMN_IN_REVIEW,
}

_STATE_COLUMNS: Dict[str, str] = {
    TASK_QUEUED: COLUMN_QUEUED,
    TASK_RUNNING: COLUMN_WORKING,
    TASK_BLOCKED: COLUMN_NEEDS_YOU,
    TASK_NEEDS_ATTENTION: COLUMN_NEEDS_YOU,
    TASK_SKIPPED_CONFLICT: COLUMN_NEEDS_YOU,
    TASK_AWAITING_APPROVAL: COLUMN_NEEDS_YOU,
    TASK_FAILED: COLUMN_STALLED,
    TASK_SKIPPED_DEPENDENCY: COLUMN_STALLED,
    TASK_SKIPPED_BUDGET: COLUMN_STALLED,
}


def column_for(
    state: str,
    merge_gate: str = STATUS_APPROVED,
    delivery: str = DELIVERY_LOCAL,
) -> str:
    """Return the column a task state belongs in. Pure.

    Args:
        state: A task state from `orchestrator.status`.
        merge_gate: The state of this card's `before_merge` approval — `approved` when no gate
            was configured, since nothing is waiting on a person.
        delivery: This card's delivery state (Roadmap Phase 6) — `local` when the work has
            never been pushed, which is every card until a person presses Ready to Merge.

    Returns:
        One of `COLUMNS`.

    A merged pull request outranks everything: it is the one fact that means the work has
    left the board. Otherwise the gate is asked first — a delivery cannot exist before
    someone cleared the gate that produced it — and then the forge is allowed to move a
    cleared card back to *Needs You*, because a red check is exactly that.
    """
    if state == TASK_DONE:
        if delivery == DELIVERY_MERGED:
            return COLUMN_MERGED
        if merge_gate == STATUS_PENDING:
            return COLUMN_IN_REVIEW
        if merge_gate == STATUS_REJECTED:
            return COLUMN_NEEDS_YOU
        return _DELIVERY_COLUMNS.get(delivery, COLUMN_READY)
    return _STATE_COLUMNS.get(state, COLUMN_QUEUED)


def _merge_gate_state(
    approvals: List[Dict[str, Any]],
    goal_id: Optional[str],
    task_id: Optional[str],
    run_id: Optional[str] = None,
) -> str:
    """Return the `before_merge` gate state for one card.

    No recorded gate means nobody asked for one, which is `approved`: an unasked question is
    not an unanswered one.
    """
    for record in approvals or []:
        if record.get("gate") != GATE_BEFORE_MERGE:
            continue
        if task_id and record.get("task_id") == task_id and record.get("goal_id") == goal_id:
            return str(record.get("status") or STATUS_PENDING)
        if run_id and record.get("run_id") == run_id:
            return str(record.get("status") or STATUS_PENDING)
    return STATUS_APPROVED


def _delivery_fields(record: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Project one delivery record onto the fields a card carries. Pure.

    `delivered_as_of` is not decoration: these facts came from the forge when someone last
    asked, and a card that showed a stale check as current would be the projection lying.
    """
    state = derive_delivery_state(record)
    pull = (record or {}).get("pr") or {}
    return {
        "delivery": state,
        "pr_number": pull.get("number"),
        "pr_url": pull.get("url"),
        "pr_state": pull.get("state"),
        "checks": pull.get("checks"),
        "review_decision": pull.get("review_decision") or None,
        "delivered_as_of": (record or {}).get("refreshed_at") or (record or {}).get("pushed_at"),
        "delivery_note": (record or {}).get("refused") or (record or {}).get("error"),
    }


def goal_cards(
    goal: Dict[str, Any],
    outcomes: Dict[str, Dict[str, Any]],
    approvals: Optional[List[Dict[str, Any]]] = None,
    deliveries: Optional[Dict[str, Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Project one goal's tasks into cards. Pure."""
    plan = goal.get("plan") or {}
    goal_id = goal.get("goal_id")
    cards: List[Dict[str, Any]] = []

    for task in plan.get("tasks") or []:
        task_id = str(task.get("id"))
        outcome = (outcomes or {}).get(task_id) or {}
        state = derive_task_state(outcome)
        gate = _merge_gate_state(approvals or [], goal_id, task_id)
        shipped = (deliveries or {}).get(delivery_key(goal_id=goal_id, task_id=task_id))
        fields = _delivery_fields(shipped)
        card = {
            "id": f"{goal_id}/{task_id}",
            "title": task.get("title") or task_id,
            "column": column_for(state, gate, fields["delivery"]),
            "state": state,
            "goal_id": goal_id,
            "goal": goal.get("goal"),
            "task_id": task_id,
            "run_id": outcome.get("run_id"),
            "branch": outcome.get("branch"),
            "tokens": int(outcome.get("tokens") or 0),
            "merge_gate": gate,
            "detail": (
                outcome.get("detail")
                or outcome.get("blocked_reason")
                or outcome.get("error")
            ),
            "updated_at": goal.get("finished_at") or goal.get("started_at"),
        }
        card.update(fields)
        cards.append(card)
    return cards


def run_card(
    run: Dict[str, Any],
    approvals: Optional[List[Dict[str, Any]]] = None,
    deliveries: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Project one standalone session into a card. Pure.

    A session's status vocabulary is the one `status.py` already derives, so it maps onto task
    states rather than getting a second set of rules.
    """
    status = str(run.get("status") or "")
    if status == STATUS_COMPLETED:
        state = TASK_DONE
    elif status == STATUS_BLOCKED:
        state = TASK_BLOCKED
    elif status == "running":
        state = TASK_RUNNING
    elif not status:
        state = TASK_QUEUED
    elif is_successful(status):
        state = TASK_RUNNING          # a progress status: the session is mid-flight
    else:
        state = TASK_FAILED

    run_id = run.get("run_id")
    gate = _merge_gate_state(approvals or [], None, None, run_id=run_id)
    summary = run.get("summary") or {}
    workspace = summary.get("workspace") or {}

    shipped = (deliveries or {}).get(delivery_key(run_id=str(run_id)))
    fields = _delivery_fields(shipped)

    card = {
        "id": str(run_id),
        "title": str(run.get("task") or "(no task recorded)"),
        "column": column_for(state, gate, fields["delivery"]),
        "state": state,
        "goal_id": run.get("goal_id"),
        "goal": None,
        "task_id": run.get("task_id"),
        "run_id": run_id,
        "branch": workspace.get("branch"),
        "tokens": int((summary.get("budget") or {}).get("tokens_spent") or 0),
        "merge_gate": gate,
        "detail": summary.get("blocked_reason") or summary.get("error"),
        "updated_at": run.get("finished_at") or run.get("started_at"),
    }
    card.update(fields)
    return card


def build_board(
    goals: Optional[List[Dict[str, Any]]] = None,
    outcomes_by_goal: Optional[Dict[str, Dict[str, Dict[str, Any]]]] = None,
    runs: Optional[List[Dict[str, Any]]] = None,
    approvals: Optional[List[Dict[str, Any]]] = None,
    deliveries: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Project goals, standalone sessions, and approvals into a board. Pure.

    Args:
        goals: Goal records (metadata plus `plan`), newest first.
        outcomes_by_goal: Each goal's task outcomes, replayed from its log.
        runs: Run store records. Those belonging to a goal are dropped, because that goal
            already contributes a card for them — one piece of work, one card.
        approvals: Every approval record, used to place delivered work.
        deliveries: Delivery records indexed by key (Roadmap Phase 6) — the recorded CI and
            review facts for work that has been pushed.

    Returns:
        ``{"columns": {name: [card, ...]}, "cards": [...], "counts": {...}, "needs_you": n}``
    """
    cards: List[Dict[str, Any]] = []

    for goal in goals or []:
        goal_id = goal.get("goal_id")
        cards.extend(
            goal_cards(
                goal,
                (outcomes_by_goal or {}).get(goal_id, {}),
                approvals=approvals,
                deliveries=deliveries,
            )
        )

    for run in runs or []:
        if run.get("goal_id"):
            continue
        cards.append(run_card(run, approvals=approvals, deliveries=deliveries))

    columns: Dict[str, List[Dict[str, Any]]] = {name: [] for name in COLUMNS}
    for card in cards:
        columns.setdefault(card["column"], []).append(card)

    return {
        "columns": columns,
        "cards": cards,
        "counts": {name: len(columns.get(name, [])) for name in COLUMNS},
        "needs_you": len(columns.get(COLUMN_NEEDS_YOU, [])),
        "ready": len(columns.get(COLUMN_READY, [])),
        "total": len(cards),
    }


def format_board(board: Dict[str, Any], width: int = 96) -> str:
    """Render a board as columns of cards for a terminal."""
    if not board or not board.get("total"):
        return (
            "The board is empty. Run a task, or delegate a goal:\n"
            '  python -m orchestrator "<goal>" --delegate'
        )

    lines: List[str] = []
    counts = board.get("counts") or {}
    header = "  ".join(f"{name} ({counts.get(name, 0)})" for name in COLUMNS)
    lines.append(header)
    lines.append("-" * min(width, len(header)))

    for name in COLUMNS:
        cards = (board.get("columns") or {}).get(name) or []
        if not cards:
            continue
        lines.append("")
        lines.append(f"{name.upper()} ({len(cards)})")
        for card in cards:
            title = str(card.get("title") or "")
            if len(title) > 58:
                title = title[:57] + "…"
            lines.append(f"  {title}")
            bits = []
            if card.get("goal"):
                bits.append(f"goal: {str(card['goal'])[:40]}")
            if card.get("branch"):
                bits.append(str(card["branch"]))
            if card.get("tokens"):
                bits.append(f"{card['tokens']:,} tokens")
            if card.get("pr_number"):
                checks = card.get("checks") or "no checks"
                bits.append(f"PR #{card['pr_number']} ({checks})")
            if bits:
                lines.append(f"      {'  |  '.join(bits)}")
            if card.get("pr_url"):
                lines.append(f"      {card['pr_url']}")
            if card.get("detail"):
                lines.append(f"      {str(card['detail'])[:76]}")
            if card.get("delivery_note"):
                lines.append(f"      {str(card['delivery_note'])[:76]}")

    if board.get("needs_you"):
        lines.append("")
        lines.append(f"{board['needs_you']} card(s) need you.")
    if board.get("ready"):
        lines.append(
            f"{board['ready']} card(s) are ready to merge. "
            "Publish one with: python -m orchestrator --deliver <card id>"
        )

    return "\n".join(lines)
