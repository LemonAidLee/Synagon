"""Goal decomposition (Roadmap Phase 1).

One request currently drives one linear pipeline, which is what makes this an orchestrator of a
*pipeline* rather than of a *team*. A team needs work broken into pieces that can be handed out,
sequenced, and eventually run in parallel — so the first step toward that is a planning pass
that turns a Goal into a **task graph**, and records it as a durable fact.

This module is deliberately execution-free. `--plan-only` produces and stores the graph and
stops; nothing schedules it yet (that is Phase 2). Splitting the two means the decomposition can
be inspected, argued with, and improved against real goals before anything depends on it.

Load-bearing rules
------------------
* **A model's output is text until it parses.** The agent is asked for JSON, but its answer is
  treated as untrusted text: extracted, parsed, and normalized here, with every failure mode
  producing a readable error rather than a half-built graph.
* **The graph must be a graph.** Unknown dependency ids and cycles are detected and rejected, so
  a scheduler built on top of this can assume a valid DAG.
* **Never invent work.** Normalization fills in ids and orders tasks; it never adds, merges, or
  rewrites what the planner asked for.
* **A plan is a fact, not a status.** It is recorded to the run store like any other fact, and
  the execution order is *derived* from the dependencies on read.
"""

import json
import re
from typing import Any, Dict, List, Optional, Tuple, TypedDict

#: Hard ceiling on tasks in one plan, so a runaway decomposition cannot become a work queue that
#: takes a day to drain. Configurable via `planning.max_tasks`.
DEFAULT_MAX_TASKS = 12

_ID_SAFE = re.compile(r"[^a-z0-9_-]+")


class TaskSpec(TypedDict, total=False):
    """One focused, independently deliverable unit of work."""
    id: str
    title: str
    intent: str               # what this task is for, in the planner's words
    acceptance: List[str]     # how a person or a verifier would know it is done
    depends_on: List[str]     # ids of tasks that must land first
    areas: List[str]          # files or directories this task expects to touch
    risk: Optional[str]       # what could go wrong, when the planner flagged something


class TaskPlan(TypedDict, total=False):
    """The decomposer's output: a validated task graph."""
    goal: str
    tasks: List[TaskSpec]
    order: List[List[str]]    # execution waves; tasks within a wave have no ordering constraint
    warnings: List[str]
    error: Optional[str]      # set when the output could not be turned into a plan


def _slug(text: str, fallback: str) -> str:
    """Turn a title into a stable, readable id."""
    slug = _ID_SAFE.sub("-", str(text or "").strip().lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    return slug[:48] or fallback


def extract_json_block(text: str) -> Optional[str]:
    """Find the JSON document in an agent's reply.

    Agents wrap JSON in prose and fences with great enthusiasm. Tries, in order: a ```json
    fence, any fence, then the outermost brace-balanced object in the text.
    """
    if not text or not isinstance(text, str):
        return None

    fenced = re.search(r"```(?:json|JSON)\s*\n(.+?)```", text, re.DOTALL)
    if fenced:
        return fenced.group(1).strip()

    fenced_any = re.search(r"```\s*\n(\{.+?\})\s*```", text, re.DOTALL)
    if fenced_any:
        return fenced_any.group(1).strip()

    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return text[start:index + 1]
        start = text.find("{", start + 1)
    return None


def _as_list(value: Any) -> List[str]:
    """Coerce a field that should be a list of strings, accepting a bare string."""
    if value is None:
        return []
    if isinstance(value, str):
        stripped = value.strip()
        return [stripped] if stripped else []
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return []


def topological_waves(tasks: List[TaskSpec]) -> Tuple[List[List[str]], Optional[str]]:
    """Group task ids into execution waves, or explain why they cannot be ordered.

    A wave is a set of tasks whose dependencies are all satisfied by earlier waves, so its
    members could run at the same time. That is the shape Phase 3 will schedule against, and
    seeing it now is the point of printing a plan.
    """
    remaining = {t["id"]: set(t.get("depends_on") or []) for t in tasks}
    waves: List[List[str]] = []
    done: set = set()

    while remaining:
        ready = sorted([tid for tid, deps in remaining.items() if deps <= done])
        if not ready:
            stuck = ", ".join(sorted(remaining))
            return waves, f"dependency cycle or unsatisfiable dependency among: {stuck}"
        waves.append(ready)
        done.update(ready)
        for tid in ready:
            remaining.pop(tid, None)

    return waves, None


def normalize_plan(
    payload: Any,
    goal: str = "",
    max_tasks: int = DEFAULT_MAX_TASKS,
) -> TaskPlan:
    """Validate and normalize a parsed decomposition into a TaskPlan.

    Repairs what is safely repairable (missing ids, string-instead-of-list fields, duplicate
    ids) and reports what is not (no tasks, unknown dependencies, cycles).
    """
    plan: TaskPlan = {"goal": goal, "tasks": [], "order": [], "warnings": [], "error": None}

    if not isinstance(payload, dict):
        plan["error"] = "the decomposer's output was not a JSON object"
        return plan

    raw_tasks = payload.get("tasks")
    if not isinstance(raw_tasks, list) or not raw_tasks:
        plan["error"] = "the decomposer returned no tasks"
        return plan

    plan["goal"] = str(payload.get("goal") or goal or "")

    if len(raw_tasks) > max_tasks:
        plan["warnings"].append(
            f"the decomposer proposed {len(raw_tasks)} tasks; keeping the first {max_tasks} "
            f"(planning.max_tasks)"
        )
        raw_tasks = raw_tasks[:max_tasks]

    seen: Dict[str, int] = {}
    tasks: List[TaskSpec] = []
    for index, raw in enumerate(raw_tasks, 1):
        if not isinstance(raw, dict):
            plan["warnings"].append(f"task #{index} was not an object and was dropped")
            continue

        title = str(raw.get("title") or raw.get("name") or "").strip()
        if not title:
            title = f"Task {index}"
            plan["warnings"].append(f"task #{index} had no title")

        tid = _slug(raw.get("id") or title, fallback=f"task-{index}")
        if tid in seen:
            seen[tid] += 1
            tid = f"{tid}-{seen[tid]}"
        else:
            seen[tid] = 1

        tasks.append(
            {
                "id": tid,
                "title": title,
                "intent": str(raw.get("intent") or raw.get("description") or "").strip(),
                "acceptance": _as_list(raw.get("acceptance") or raw.get("acceptance_criteria")),
                "depends_on": [_slug(d, d) for d in _as_list(raw.get("depends_on") or raw.get("dependencies"))],
                "areas": _as_list(raw.get("areas") or raw.get("files")),
                "risk": (str(raw.get("risk")).strip() or None) if raw.get("risk") else None,
            }
        )

    known = {t["id"] for t in tasks}
    for task in tasks:
        unknown = [d for d in task["depends_on"] if d not in known]
        if unknown:
            plan["warnings"].append(
                f"task '{task['id']}' depends on unknown task(s) {', '.join(unknown)}; "
                f"the dependency was dropped"
            )
            task["depends_on"] = [d for d in task["depends_on"] if d in known]
        if task["id"] in task["depends_on"]:
            plan["warnings"].append(f"task '{task['id']}' depended on itself; the dependency was dropped")
            task["depends_on"] = [d for d in task["depends_on"] if d != task["id"]]

    plan["tasks"] = tasks
    waves, cycle_error = topological_waves(tasks)
    plan["order"] = waves
    if cycle_error:
        plan["error"] = cycle_error
    return plan


def parse_task_plan(
    text: str,
    goal: str = "",
    max_tasks: int = DEFAULT_MAX_TASKS,
) -> TaskPlan:
    """Turn a decomposer agent's raw output into a validated TaskPlan. Never raises."""
    block = extract_json_block(text or "")
    if not block:
        return {
            "goal": goal,
            "tasks": [],
            "order": [],
            "warnings": [],
            "error": "the decomposer's output contained no JSON object",
        }
    try:
        payload = json.loads(block)
    except json.JSONDecodeError as exc:
        return {
            "goal": goal,
            "tasks": [],
            "order": [],
            "warnings": [],
            "error": f"the decomposer's JSON could not be parsed: {exc}",
        }
    return normalize_plan(payload, goal=goal, max_tasks=max_tasks)


def plan_is_usable(plan: Optional[TaskPlan]) -> bool:
    """Return True when a plan has tasks and no structural error."""
    return bool(plan and plan.get("tasks") and not plan.get("error"))


def format_plan(plan: Optional[TaskPlan], width: int = 78) -> str:
    """Render a task plan as a readable brief, including the execution waves."""
    if not plan:
        return "No plan."
    if plan.get("error") and not plan.get("tasks"):
        return f"Decomposition failed: {plan['error']}"

    tasks = {t["id"]: t for t in plan.get("tasks") or []}
    lines: List[str] = []

    if plan.get("goal"):
        lines.append(f"GOAL: {plan['goal']}")
        lines.append("")

    lines.append(f"{len(tasks)} task(s):")
    lines.append("")
    for task in plan.get("tasks") or []:
        lines.append(f"  [{task['id']}] {task['title']}")
        if task.get("intent"):
            lines.append(f"      {task['intent']}")
        if task.get("depends_on"):
            lines.append(f"      after:      {', '.join(task['depends_on'])}")
        if task.get("areas"):
            lines.append(f"      touches:    {', '.join(task['areas'][:6])}")
        for criterion in task.get("acceptance") or []:
            lines.append(f"      done when:  {criterion}")
        if task.get("risk"):
            lines.append(f"      risk:       {task['risk']}")
        lines.append("")

    order = plan.get("order") or []
    if order:
        lines.append("EXECUTION ORDER")
        for index, wave in enumerate(order, 1):
            titles = ", ".join(tasks[t]["title"] if t in tasks else t for t in wave)
            parallel = " (can run in parallel)" if len(wave) > 1 else ""
            lines.append(f"  Wave {index}{parallel}: {titles}")
        lines.append("")

    if plan.get("error"):
        lines.append(f"PROBLEM: {plan['error']}")
    for warning in plan.get("warnings") or []:
        lines.append(f"  note: {warning}")

    return "\n".join(lines).rstrip()
