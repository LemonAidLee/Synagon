"""What this repository has taught the planner (Roadmap 8.2).

`--plan-only` decomposed every Goal **cold**. The decomposer saw the project's context and
the goal text and nothing else: not the decompositions that came before, not which tasks
collided last time, not which areas of this repository are dense, not what a task of this
shape actually cost. This module is the missing half — the *memory* of a body that already
worked.

The decision this implements
----------------------------
A Project **does** accumulate knowledge across Goals (Roadmap 9.2). Its lifetime is the
repository, not a run, which makes it the first thing in this codebase to have that lifetime.

What it is not
--------------
**It is not a new store.** Every fact here is already recorded somewhere:

| Fact | Where it already lives |
| --- | --- |
| what a delivered task touched | the task's branch, via `workspace.changed_paths` |
| what a task *said* it would touch | the plan's `areas`, in the goal's `goal_plan` event |
| which siblings collided, over what | the goal's `collision` events |
| what a task cost | the task outcome's `tokens` and `duration_seconds` |
| what a person rejected, and why | the approval store's decided records |

So this is a **projection over the stores**, exactly as the board is, which keeps invariant 1
("nothing stores a status it could derive") intact one level higher: the memory cannot drift
from what happened, because it *is* what happened, read back.

Load-bearing rules
------------------
* **Facts, and only facts.** Nothing here is an opinion about what the planner should do. It
  reports what occurred and lets the prompt draw the conclusion. A memory that editorialised
  would be a second planner nobody could inspect.
* **Bounded, and it says so.** A memory that grows without a ceiling is a context window that
  eventually fails, exactly as the repair loop needed a ceiling. `MAX_GOALS` bounds how far
  back it reads, `MAX_GIT_QUERIES` bounds how much git it will do to build one, and
  `summarise` takes a character budget and reports what it dropped to fit.
* **Never raises, and degrades to nothing.** No git, no goals, an unreadable log: the answer
  is an empty memory, and a cold decomposition is what this project did before 8.2 anyway.
* **Inspectable.** `--memory` prints exactly what the decomposer will be told. A prompt
  addition a person cannot read is one they cannot argue with.
"""

import statistics
from typing import Any, Dict, List, Optional, Tuple

from orchestrator.approvals import (
    STATUS_REJECTED,
    list_approvals,
)
from orchestrator.config import (
    get_approval_config,
    get_delegation_config,
)
from orchestrator.goals import (
    collisions as goal_collisions,
    list_goals,
    load_goal,
    task_outcomes,
)
from orchestrator.status import TASK_DONE, derive_task_state
from orchestrator.workspace import changed_paths, is_git_repo

#: How many goals back the memory reads. Recent decompositions describe the repository as it
#: is; a decomposition from two hundred goals ago describes one that no longer exists.
MAX_GOALS = 12

#: How many `git diff` calls one memory may cost. A memory is built while a person waits for
#: a decomposition to start, so it may not turn into a hundred subprocesses.
MAX_GIT_QUERIES = 40

#: How many paths, collisions and rejections the memory carries at most, before any prompt
#: budget is applied. Ranked by how often they came up, so the ceiling drops the rare.
MAX_ROWS = 12

#: Default character budget for the prompt section. Roughly a few hundred tokens: enough to
#: change a decomposition, small enough that it can never be the reason a prompt does not fit.
DEFAULT_BUDGET_CHARS = 2400

#: Below this many observations a number is reported with a warning rather than as a finding,
#: the same rule `stats.py` holds itself to.
THIN_SAMPLE = 3


# ---------------------------------------------------------------------------
# Reading what happened
# ---------------------------------------------------------------------------


def _plan_of(goal: Dict[str, Any]) -> Dict[str, Any]:
    """Return the plan a goal executed, from its meta or its log. Never raises."""
    plan = goal.get("plan")
    if isinstance(plan, dict) and plan.get("tasks"):
        return plan
    for event in reversed(goal.get("events") or []):
        if event.get("event") == "goal_plan":
            candidate = event.get("plan")
            if isinstance(candidate, dict):
                return candidate
    return {}


def _areas_by_task(plan: Dict[str, Any]) -> Dict[str, List[str]]:
    """What each task in a plan *said* it would touch."""
    areas: Dict[str, List[str]] = {}
    for task in (plan or {}).get("tasks") or []:
        task_id = str(task.get("id") or "")
        if task_id:
            areas[task_id] = [str(a) for a in (task.get("areas") or []) if a]
    return areas


def observe(
    project_root: str,
    config: Any = None,
    max_goals: int = MAX_GOALS,
    max_git_queries: int = MAX_GIT_QUERIES,
) -> List[Dict[str, Any]]:
    """Read the last `max_goals` goals into one shape per goal. Never raises.

    Returns:
        Newest first, each ``{"goal_id", "goal", "started_at", "status", "task_count",
        "tasks": [{"task_id", "state", "tokens", "duration_seconds", "touched", "areas",
        "touched_source"}], "collisions": [...]}``.

    `touched` is what the branch actually changed when git can still answer — a branch that
    retention has swept (Roadmap 8.4) cannot be diffed, and then the task's declared `areas`
    stand in, with `touched_source` saying which it is. Guessing silently would turn "what
    this task touched" into "what someone predicted it would", and those are different facts.
    """
    delegation = get_delegation_config(config or {})
    try:
        entries = list_goals(
            project_root, limit=max_goals, directory=delegation.get("goals_directory")
        )
    except Exception:
        return []

    git_ok = False
    try:
        git_ok = is_git_repo(project_root)
    except Exception:
        git_ok = False
    budget = max_git_queries if git_ok else 0

    observed: List[Dict[str, Any]] = []
    for entry in entries:
        goal_id = entry.get("goal_id")
        if not goal_id:
            continue
        try:
            goal = load_goal(
                project_root, str(goal_id), directory=delegation.get("goals_directory")
            )
        except Exception:
            continue
        if not goal:
            continue

        plan = _plan_of(goal)
        areas = _areas_by_task(plan)
        outcomes = task_outcomes(goal)

        tasks: List[Dict[str, Any]] = []
        for task_id, outcome in outcomes.items():
            state = derive_task_state(outcome)
            declared = areas.get(task_id, [])
            touched: List[str] = []
            source = "none"

            branch, base = outcome.get("branch"), outcome.get("base_ref")
            if state == TASK_DONE and branch and base and budget > 0:
                budget -= 1
                try:
                    touched = changed_paths(project_root, str(base), str(branch))
                except Exception:
                    touched = []
                if touched:
                    source = "diff"
            if not touched and declared:
                touched, source = list(declared), "declared"

            tasks.append(
                {
                    "task_id": task_id,
                    "state": state,
                    "tokens": int(outcome.get("tokens") or 0),
                    "duration_seconds": float(outcome.get("duration_seconds") or 0.0),
                    "touched": touched,
                    "areas": declared,
                    "touched_source": source,
                }
            )

        observed.append(
            {
                "goal_id": str(goal_id),
                "goal": str(goal.get("goal") or entry.get("goal") or ""),
                "started_at": goal.get("started_at") or entry.get("started_at"),
                "status": goal.get("status") or entry.get("status"),
                "task_count": len((plan or {}).get("tasks") or []) or len(tasks),
                "tasks": tasks,
                "collisions": goal_collisions(goal),
            }
        )
    return observed


# ---------------------------------------------------------------------------
# The projection
# ---------------------------------------------------------------------------


def _directory_of(path: str) -> str:
    """The path a hot-spot is really about: its directory, or the file when it is top-level."""
    text = str(path or "").replace("\\", "/").strip("/")
    if "/" not in text:
        return text
    return text.rsplit("/", 1)[0] + "/"


def _rejections(
    project_root: str,
    config: Any,
    limit: int = MAX_ROWS,
) -> List[Dict[str, Any]]:
    """Every gate a person answered `rejected`, newest first. Never raises."""
    try:
        records = list_approvals(
            project_root,
            status=STATUS_REJECTED,
            directory=get_approval_config(config or {}).get("directory"),
        )
    except Exception:
        return []
    return [
        {
            "gate": r.get("gate"),
            "subject": r.get("subject"),
            "note": r.get("note"),
            "decided_at": r.get("decided_at"),
        }
        for r in records[:limit]
    ]


def project_memory(
    project_root: str,
    config: Any = None,
    max_goals: int = MAX_GOALS,
    observed: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """What this repository has taught the planner, as facts. Never raises.

    Args:
        project_root: The project directory.
        config: The resolved configuration, for the store locations.
        max_goals: How far back to read.
        observed: Pre-read observations, so the projection is testable without a filesystem.

    Returns:
        ``{"goals_read", "hot_paths", "collisions", "cost", "shapes", "rejections",
        "thin"}``. Every rate and every number carries the count it was computed from,
        because "two tasks touched config.py" and "forty did" are different advice.
    """
    if observed is None:
        observed = observe(project_root, config, max_goals=max_goals)

    memory: Dict[str, Any] = {
        "goals_read": len(observed),
        "hot_paths": [],
        "collisions": [],
        "cost": {},
        "shapes": [],
        "rejections": _rejections(project_root, config),
        "thin": len(observed) < THIN_SAMPLE,
    }
    if not observed:
        return memory

    # -- which areas of this repository are dense ---------------------------
    # Counted by *task*, not by file change, so a task that rewrote forty files in one
    # directory counts once for that directory. The question is "do tasks keep landing here",
    # not "how much churn is there".
    by_dir: Dict[str, Dict[str, Any]] = {}
    for goal in observed:
        for task in goal["tasks"]:
            for directory in {_directory_of(p) for p in task["touched"] if p}:
                row = by_dir.setdefault(
                    directory, {"path": directory, "tasks": 0, "goals": set(), "declared_only": 0}
                )
                row["tasks"] += 1
                row["goals"].add(goal["goal_id"])
                if task["touched_source"] == "declared":
                    row["declared_only"] += 1

    memory["hot_paths"] = sorted(
        (
            {
                "path": row["path"],
                "tasks": row["tasks"],
                "goals": len(row["goals"]),
                "declared_only": row["declared_only"],
            }
            for row in by_dir.values()
        ),
        key=lambda r: (-r["tasks"], r["path"]),
    )[:MAX_ROWS]

    # -- which sibling pairs actually collided, over what --------------------
    # Keyed by the *path*, because that is what the next decomposition can avoid. The task
    # ids that collided are kept as evidence, not as the key: a task id is unique inside its
    # goal and means nothing in the next one.
    by_path: Dict[str, Dict[str, Any]] = {}
    for goal in observed:
        for collision in goal["collisions"]:
            for path in collision.get("paths") or []:
                row = by_path.setdefault(
                    str(path), {"path": str(path), "times": 0, "goals": set(), "pairs": []}
                )
                row["times"] += 1
                row["goals"].add(goal["goal_id"])
                pair = tuple(sorted(str(t) for t in (collision.get("task_ids") or [])))
                if pair and pair not in row["pairs"]:
                    row["pairs"].append(pair)

    memory["collisions"] = sorted(
        (
            {
                "path": row["path"],
                "times": row["times"],
                "goals": len(row["goals"]),
                "pairs": [list(pair) for pair in row["pairs"][:3]],
            }
            for row in by_path.values()
        ),
        key=lambda r: (-r["times"], r["path"]),
    )[:MAX_ROWS]

    # -- what a task of this shape actually costs ----------------------------
    delivered = [t for g in observed for t in g["tasks"] if t["state"] == TASK_DONE]
    failed = [t for g in observed for t in g["tasks"] if t["state"] != TASK_DONE]
    delivered_tokens = [t["tokens"] for t in delivered if t["tokens"]]
    failed_tokens = [t["tokens"] for t in failed if t["tokens"]]
    memory["cost"] = {
        "tasks_delivered": len(delivered),
        "tasks_not_delivered": len(failed),
        "median_tokens_delivered": (
            int(statistics.median(delivered_tokens)) if delivered_tokens else None
        ),
        "median_tokens_not_delivered": (
            int(statistics.median(failed_tokens)) if failed_tokens else None
        ),
        "measured_for_cost": len(delivered_tokens) + len(failed_tokens),
    }

    # -- what a decomposition of this repository has looked like -------------
    memory["shapes"] = [
        {
            "goal": goal["goal"][:120],
            "tasks": goal["task_count"],
            "delivered": sum(1 for t in goal["tasks"] if t["state"] == TASK_DONE),
            "collisions": len(goal["collisions"]),
            "at": goal["started_at"],
        }
        for goal in observed[:MAX_ROWS]
    ]
    return memory


# ---------------------------------------------------------------------------
# The summariser, and its budget
# ---------------------------------------------------------------------------


def _sections(memory: Dict[str, Any]) -> List[Tuple[str, List[str]]]:
    """Build the prompt's sections, most actionable first. Pure.

    The order *is* the priority: when the budget cannot fit everything, what survives is what
    would change a decomposition. A collision is the most actionable thing this memory knows —
    it is a pair of tasks that should not have been emitted together — and a list of past goal
    shapes is the least, so that is the order they are dropped in.
    """
    sections: List[Tuple[str, List[str]]] = []

    if memory.get("collisions"):
        lines = []
        for row in memory["collisions"]:
            pairs = "; ".join(" + ".join(pair) for pair in row.get("pairs") or [])
            lines.append(
                f"- `{row['path']}` — {row['times']} collision(s) across {row['goals']} goal(s)"
                + (f" (e.g. {pairs})" if pairs else "")
            )
        sections.append(
            (
                "Files that sibling tasks have fought over. Two tasks that will both touch one "
                "of these belong in one task, or in a dependency chain.",
                lines,
            )
        )

    if memory.get("hot_paths"):
        lines = []
        for row in memory["hot_paths"]:
            note = " (declared, not diffed)" if row["declared_only"] >= row["tasks"] else ""
            lines.append(
                f"- `{row['path']}` — {row['tasks']} task(s) across {row['goals']} goal(s){note}"
            )
        sections.append(
            ("Where work in this repository has actually landed.", lines)
        )

    if memory.get("rejections"):
        lines = [
            f"- {r.get('subject') or '(no subject)'}"
            + (f" — \"{str(r.get('note'))[:120]}\"" if r.get("note") else "")
            for r in memory["rejections"]
        ]
        sections.append(
            ("Work a person rejected at a gate, and why. Do not propose it again unchanged.",
             lines)
        )

    cost = memory.get("cost") or {}
    if cost.get("measured_for_cost"):
        lines = [
            f"- a delivered task: median {cost['median_tokens_delivered']:,} tokens "
            f"over {cost['tasks_delivered']} task(s)"
            if cost.get("median_tokens_delivered")
            else f"- {cost.get('tasks_delivered', 0)} delivered task(s), cost unreported",
        ]
        if cost.get("median_tokens_not_delivered"):
            lines.append(
                f"- a task that did not deliver: median "
                f"{cost['median_tokens_not_delivered']:,} tokens over "
                f"{cost['tasks_not_delivered']} task(s)"
            )
        sections.append(("What a task of this repository has cost.", lines))

    if memory.get("shapes"):
        lines = [
            f"- \"{s['goal']}\" — {s['tasks']} task(s), {s['delivered']} delivered"
            + (f", {s['collisions']} collision(s)" if s["collisions"] else "")
            for s in memory["shapes"]
        ]
        sections.append(("How goals here have been decomposed before.", lines))

    return sections


def summarise(memory: Dict[str, Any], budget_chars: int = DEFAULT_BUDGET_CHARS) -> str:
    """Render a memory as the bounded block the decomposer's prompt carries. Pure.

    Sections are added whole, most actionable first, until the next one would not fit. A
    memory that was cut says so, because a planner told "here is what happened" about a
    truncated list would over-trust it.

    Returns:
        The block, or "" when there is nothing worth saying — in which case the prompt is
        exactly the cold prompt this project used before Roadmap 8.2.
    """
    sections = _sections(memory)
    if not sections:
        return ""

    header = (
        f"What this repository has taught the planner, from its last "
        f"{memory.get('goals_read', 0)} goal(s). These are recorded facts, not "
        f"instructions — weigh them against what you read in the code."
    )
    if memory.get("thin"):
        header += (
            " There have been few goals so far, so treat every count below as an anecdote "
            "rather than a pattern."
        )

    parts: List[str] = [header]
    used = len(header)
    dropped = 0

    for title, lines in sections:
        block = "\n\n" + title + "\n" + "\n".join(lines)
        if used + len(block) > budget_chars:
            dropped += 1
            continue
        parts.append(block)
        used += len(block)

    if dropped:
        parts.append(
            f"\n\n({dropped} further section(s) omitted to stay within the memory budget.)"
        )
    return "".join(parts)


def memory_section(
    project_root: str,
    config: Any = None,
    max_goals: int = MAX_GOALS,
    budget_chars: int = DEFAULT_BUDGET_CHARS,
) -> str:
    """Build the decomposer's memory block for this project. Never raises.

    A failure here must not stop a decomposition: the fallback is "" — the cold prompt, which
    is exactly what this project did before this module existed.
    """
    try:
        return summarise(
            project_memory(project_root, config, max_goals=max_goals),
            budget_chars=budget_chars,
        )
    except Exception:
        return ""


def format_memory(memory: Dict[str, Any]) -> str:
    """Render a memory for a terminal, so a person can read what the planner will be told."""
    lines: List[str] = [
        f"Read {memory.get('goals_read', 0)} goal(s)."
        + (" Thin evidence." if memory.get("thin") else "")
    ]
    sections = _sections(memory)
    if not sections:
        lines.append("")
        lines.append("  Nothing recorded yet — every goal is decomposed cold.")
        return "\n".join(lines)

    for title, rows in sections:
        lines.append("")
        lines.append("  " + title)
        lines.extend("  " + row for row in rows)
    return "\n".join(lines)
