"""Tests for the delegation phases.

Covers:
  Phase 2  Running a decomposed goal   - orchestrator.scheduler, goal store, goal budgets
  Phase 3  Running tasks in parallel   - concurrency, collision detection
"""

import os
import shutil
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

import orchestrator.graph as graph_module
from orchestrator import workspace as ws
from orchestrator.config import ConfigValidationError, get_delegation_config, validate_config
from orchestrator.goals import (
    GoalStore,
    collisions,
    list_goals,
    load_goal,
    resolve_goal_id,
    task_outcomes,
)
from orchestrator.graph import build_graph
from orchestrator.scheduler import (
    _goal_budget_state,
    _ready_tasks,
    detect_collisions,
    format_goal_summary,
    render_task_brief,
    run_goal,
)
from orchestrator.status import (
    GOAL_BLOCKED,
    GOAL_BUDGET_EXHAUSTED,
    GOAL_COMPLETED,
    GOAL_FAILED,
    GOAL_PARTIAL,
    TASK_BLOCKED,
    TASK_DONE,
    TASK_FAILED,
    TASK_NEEDS_ATTENTION,
    TASK_QUEUED,
    TASK_SKIPPED_BUDGET,
    TASK_SKIPPED_CONFLICT,
    TASK_SKIPPED_DEPENDENCY,
    derive_goal_status,
    derive_goal_summary,
    derive_task_state,
)

ROLES = {
    r: {"responsibility": r}
    for r in ("decomposer", "researcher", "planner", "implementer", "verifier")
}

PIPELINE = [
    {"agent": "opencode", "model": "opencode/gpt-5.1-codex", "role": "implementer"},
    {"agent": "claude", "model": "sonnet", "role": "verifier"},
]


def _cfg(**extra):
    config = {"agents": list(PIPELINE), "roles": dict(ROLES)}
    config.update(extra)
    return validate_config(config)


def _plan(*tasks):
    from orchestrator.decompose import normalize_plan

    return normalize_plan({"tasks": list(tasks)}, goal="a goal")


def _task(tid, title=None, depends_on=None, areas=None):
    return {
        "id": tid,
        "title": title or tid.replace("-", " ").title(),
        "depends_on": list(depends_on or []),
        "areas": list(areas or []),
    }


def _session(status="completed", tokens=0, **extra):
    """A fake session result, shaped like the graph's final state."""
    result = {
        "status": status,
        "verification_verdict": "PASS" if status == "completed" else "FAIL",
        "run_id": f"run-{status}",
        "agent_results": [
            {"token_usage": {"available": True, "total_tokens": tokens}}
        ] if tokens else [],
        "summary": {"duration_seconds": 0.5},
        "workspace": {"isolated": False},
    }
    result.update(extra)
    return result


class _GitRepo:
    """A throwaway git repository with one commit."""

    def __init__(self, prefix="orch_t5_"):
        import tempfile

        self.path = tempfile.mkdtemp(prefix=prefix)
        for args in (
            ["init", "-q"],
            ["config", "user.email", "t@example.com"],
            ["config", "user.name", "T"],
        ):
            self.git(*args)
        with open(os.path.join(self.path, "seed.txt"), "w", encoding="utf-8") as handle:
            handle.write("seed\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "seed")

    def git(self, *args):
        return subprocess.run(
            ["git"] + list(args), cwd=self.path, capture_output=True, stdin=subprocess.DEVNULL
        )

    def cleanup(self):
        subprocess.run(["git", "worktree", "prune"], cwd=self.path, capture_output=True)
        shutil.rmtree(self.path, ignore_errors=True)


# ===========================================================================
# Deriving what a goal did
# ===========================================================================


class TestTaskAndGoalDerivation(unittest.TestCase):
    """Task and goal state are derived, exactly like a session's status."""

    def test_a_task_with_no_session_is_queued(self):
        self.assertEqual(derive_task_state(None), TASK_QUEUED)
        self.assertEqual(derive_task_state({}), TASK_QUEUED)

    def test_task_state_follows_its_session(self):
        self.assertEqual(derive_task_state({"status": "completed"}), TASK_DONE)
        self.assertEqual(derive_task_state({"status": "failed"}), TASK_FAILED)
        self.assertEqual(derive_task_state({"status": "error"}), TASK_FAILED)
        self.assertEqual(derive_task_state({"status": "budget_exhausted"}), TASK_FAILED)
        self.assertEqual(derive_task_state({"status": "blocked"}), TASK_BLOCKED)

    def test_the_scheduler_only_owns_what_a_session_cannot_see(self):
        self.assertEqual(
            derive_task_state({"skipped": True, "skip_reason": "dependency"}),
            TASK_SKIPPED_DEPENDENCY,
        )
        self.assertEqual(
            derive_task_state({"skipped": True, "skip_reason": "budget"}), TASK_SKIPPED_BUDGET
        )
        self.assertEqual(
            derive_task_state({"skipped": True, "skip_reason": "conflict"}), TASK_SKIPPED_CONFLICT
        )
        self.assertEqual(
            derive_task_state({"status": "completed", "collided": True}), TASK_NEEDS_ATTENTION
        )

    def test_goal_status_summarises_its_tasks(self):
        self.assertEqual(derive_goal_status([TASK_DONE, TASK_DONE]), GOAL_COMPLETED)
        self.assertEqual(derive_goal_status([TASK_DONE, TASK_FAILED]), GOAL_PARTIAL)
        self.assertEqual(derive_goal_status([TASK_FAILED]), GOAL_FAILED)
        self.assertEqual(derive_goal_status([]), "empty")

    def test_anything_needing_a_person_outranks_delivery(self):
        self.assertEqual(derive_goal_status([TASK_DONE, TASK_BLOCKED]), GOAL_BLOCKED)
        self.assertEqual(derive_goal_status([TASK_DONE, TASK_NEEDS_ATTENTION]), GOAL_BLOCKED)

    def test_a_budget_that_stopped_the_work_is_the_headline(self):
        self.assertEqual(
            derive_goal_status([TASK_DONE, TASK_SKIPPED_BUDGET, TASK_BLOCKED]),
            GOAL_BUDGET_EXHAUSTED,
        )

    def test_summary_totals_the_sessions(self):
        summary = derive_goal_summary(
            [_task("a"), _task("b")],
            {
                "a": {"status": "completed", "tokens": 100, "duration_seconds": 1.0},
                "b": {"status": "failed", "tokens": 50, "duration_seconds": 2.0},
            },
        )
        self.assertEqual(summary["status"], GOAL_PARTIAL)
        self.assertEqual(summary["done"], 1)
        self.assertEqual(summary["tokens"], 150)
        self.assertEqual(summary["duration_seconds"], 3.0)


# ===========================================================================
# Ordering and briefs
# ===========================================================================


class TestReadiness(unittest.TestCase):
    def test_independent_tasks_are_all_ready(self):
        ready, unrunnable = _ready_tasks([_task("a"), _task("b")], {})
        self.assertEqual([t["id"] for t in ready], ["a", "b"])
        self.assertEqual(unrunnable, [])

    def test_a_dependent_task_waits(self):
        tasks = [_task("a"), _task("b", depends_on=["a"])]
        ready, _ = _ready_tasks(tasks, {})
        self.assertEqual([t["id"] for t in ready], ["a"])

        ready, _ = _ready_tasks(tasks, {"a": TASK_DONE})
        self.assertEqual([t["id"] for t in ready], ["b"])

    def test_a_failed_dependency_makes_its_dependents_unrunnable(self):
        tasks = [_task("a"), _task("b", depends_on=["a"])]
        ready, unrunnable = _ready_tasks(tasks, {"a": TASK_FAILED})
        self.assertEqual(ready, [])
        self.assertEqual(unrunnable[0][0]["id"], "b")
        self.assertEqual(unrunnable[0][1], ["a"])


class TestTaskBrief(unittest.TestCase):
    """A session sees only its own task, so the brief has to place it."""

    def test_it_carries_the_task_and_its_criteria(self):
        task = {
            "id": "limiter",
            "title": "Add a token bucket",
            "intent": "The shared primitive.",
            "acceptance": ["unit tests cover refill"],
            "areas": ["api/limits.py"],
        }
        brief = render_task_brief(task, "Add rate limiting", siblings=[task])
        self.assertIn("Add a token bucket", brief)
        self.assertIn("The shared primitive.", brief)
        self.assertIn("unit tests cover refill", brief)
        self.assertIn("api/limits.py", brief)
        self.assertIn("Add rate limiting", brief)

    def test_it_names_the_siblings_it_must_not_implement(self):
        tasks = [_task("a", "Build the thing"), _task("b", "Document the thing")]
        brief = render_task_brief(tasks[0], "goal", siblings=tasks)
        self.assertIn("Do NOT implement them", brief)
        self.assertIn("Document the thing", brief)
        self.assertNotIn("  - Build the thing", brief)


# ===========================================================================
# Phase 2 - running a goal
# ===========================================================================


class TestRunGoal(unittest.TestCase):
    """The scheduler runs every task, in order, and reports what happened."""

    def setUp(self):
        self.repo = _GitRepo(prefix="orch_t5g_")
        self.config = _cfg()

    def tearDown(self):
        self.repo.cleanup()

    def _run(self, plan, results=None, **kwargs):
        """Run a goal with a scripted session per task id."""
        results = results or {}
        seen = []

        def runner(state):
            seen.append(state)
            return results.get(state["task_id"], _session())

        summary = run_goal(
            goal="a goal",
            plan=plan,
            project_root=self.repo.path,
            config=kwargs.pop("config", self.config),
            session_runner=runner,
            **kwargs,
        )
        return summary, seen

    def test_it_runs_every_task_and_completes(self):
        summary, seen = self._run(_plan(_task("a"), _task("b")))

        self.assertEqual(summary["status"], GOAL_COMPLETED)
        self.assertEqual(summary["done"], 2)
        self.assertEqual(sorted(s["task_id"] for s in seen), ["a", "b"])

    def test_each_task_gets_its_own_brief_not_the_goal(self):
        _, seen = self._run(_plan(_task("a", "First thing"), _task("b", "Second thing")))
        briefs = {s["task_id"]: s["task"] for s in seen}
        self.assertIn("First thing", briefs["a"])
        self.assertIn("Second thing", briefs["b"])

    def test_dependencies_run_in_order_and_build_on_each_other(self):
        plan = _plan(_task("a"), _task("b", depends_on=["a"]))
        results = {
            "a": _session(workspace={"branch": "orchestrator/run/a-branch"}),
            "b": _session(),
        }
        _, seen = self._run(plan, results)

        order = [s["task_id"] for s in seen]
        self.assertEqual(order, ["a", "b"])
        # The dependent task starts from its dependency's branch, not from the goal's base.
        self.assertEqual(seen[1]["workspace_base_ref"], "orchestrator/run/a-branch")
        self.assertNotEqual(seen[0]["workspace_base_ref"], "orchestrator/run/a-branch")

    def test_extra_dependencies_are_merged_forward(self):
        plan = _plan(_task("a"), _task("b"), _task("c", depends_on=["a", "b"]))
        results = {
            "a": _session(workspace={"branch": "branch-a"}),
            "b": _session(workspace={"branch": "branch-b"}),
            "c": _session(),
        }
        _, seen = self._run(plan, results)

        third = [s for s in seen if s["task_id"] == "c"][0]
        self.assertEqual(third["workspace_base_ref"], "branch-a")
        self.assertEqual(third["workspace_merge_refs"], ["branch-b"])

    def test_a_failed_task_skips_its_dependents(self):
        plan = _plan(_task("a"), _task("b", depends_on=["a"]))
        summary, seen = self._run(plan, {"a": _session(status="failed")})

        self.assertEqual([s["task_id"] for s in seen], ["a"])
        states = {r["task_id"]: r["state"] for r in summary["tasks"]}
        self.assertEqual(states["a"], TASK_FAILED)
        self.assertEqual(states["b"], TASK_SKIPPED_DEPENDENCY)
        self.assertEqual(summary["status"], GOAL_FAILED)

    def test_an_unrelated_task_still_runs_after_a_failure(self):
        plan = _plan(_task("a"), _task("b", depends_on=["a"]), _task("c"))
        summary, seen = self._run(plan, {"a": _session(status="failed")})

        self.assertIn("c", [s["task_id"] for s in seen])
        self.assertEqual(summary["status"], GOAL_PARTIAL)

    def test_stop_on_failure_abandons_the_rest(self):
        plan = _plan(_task("a"), _task("b"), _task("c"))
        config = _cfg(delegation={"stop_on_failure": True})
        summary, seen = self._run(plan, {"a": _session(status="failed")}, config=config)

        self.assertLess(len(seen), 3)
        self.assertNotEqual(summary["status"], GOAL_COMPLETED)

    def test_a_blocked_task_makes_the_goal_need_a_person(self):
        summary, _ = self._run(
            _plan(_task("a"), _task("b")),
            {"a": _session(status="blocked", blocked_reason="needs a token")},
        )
        self.assertEqual(summary["status"], GOAL_BLOCKED)
        row = [r for r in summary["tasks"] if r["task_id"] == "a"][0]
        self.assertEqual(row["state"], TASK_BLOCKED)
        self.assertIn("needs a token", row["detail"])

    def test_a_crashing_session_does_not_kill_the_goal(self):
        def runner(state):
            if state["task_id"] == "a":
                raise RuntimeError("the pipeline exploded")
            return _session()

        summary = run_goal(
            goal="g",
            plan=_plan(_task("a"), _task("b")),
            project_root=self.repo.path,
            config=self.config,
            session_runner=runner,
        )
        states = {r["task_id"]: r["state"] for r in summary["tasks"]}
        self.assertEqual(states["a"], TASK_FAILED)
        self.assertEqual(states["b"], TASK_DONE)
        self.assertEqual(summary["status"], GOAL_PARTIAL)

    def test_a_dependency_merge_conflict_stops_only_that_task(self):
        plan = _plan(_task("a"), _task("b"), _task("c", depends_on=["a", "b"]))
        results = {
            "a": _session(workspace={"branch": "branch-a"}),
            "b": _session(workspace={"branch": "branch-b"}),
            "c": _session(
                status="error",
                workspace_merge={"merged": [], "conflicted": ["branch-b"], "error": "CONFLICT"},
            ),
        }
        summary, _ = self._run(plan, results)

        states = {r["task_id"]: r["state"] for r in summary["tasks"]}
        self.assertEqual(states["c"], TASK_SKIPPED_CONFLICT)
        self.assertEqual(summary["status"], GOAL_BLOCKED)

    def test_the_report_is_readable(self):
        summary, _ = self._run(
            _plan(_task("a")), {"a": _session(workspace={"branch": "orchestrator/run/a"})}
        )
        text = format_goal_summary(summary)
        self.assertIn("GOAL:", text)
        self.assertIn("a", text)
        self.assertIn("git diff", text)


class TestGoalBudget(unittest.TestCase):
    """A decomposition multiplies a per-session ceiling, so the goal has its own."""

    def setUp(self):
        self.repo = _GitRepo(prefix="orch_t5b_")

    def tearDown(self):
        self.repo.cleanup()

    def test_zero_means_unlimited(self):
        state = _goal_budget_state({}, spent_tokens=10 ** 9, started_at=0.0)
        self.assertFalse(state["exhausted"])

    def test_the_token_ceiling_names_the_setting(self):
        state = _goal_budget_state(
            {"goal_max_total_tokens": 100}, spent_tokens=150, started_at=time.time()
        )
        self.assertTrue(state["exhausted"])
        self.assertIn("budget.goal_max_total_tokens", state["reason"])

    def test_the_time_ceiling_names_the_setting(self):
        state = _goal_budget_state(
            {"goal_max_duration_seconds": 1}, spent_tokens=0, started_at=time.time() - 30
        )
        self.assertTrue(state["exhausted"])
        self.assertIn("budget.goal_max_duration_seconds", state["reason"])

    def test_a_spent_goal_budget_stops_the_remaining_tasks(self):
        config = _cfg(budget={"goal_max_total_tokens": 100})
        ran = []

        def runner(state):
            ran.append(state["task_id"])
            return _session(tokens=500)

        summary = run_goal(
            goal="g",
            plan=_plan(_task("a"), _task("b", depends_on=["a"]), _task("c", depends_on=["b"])),
            project_root=self.repo.path,
            config=config,
            session_runner=runner,
        )

        # The first task is authorised; after it overspends, nothing else starts.
        self.assertEqual(ran, ["a"])
        self.assertEqual(summary["status"], GOAL_BUDGET_EXHAUSTED)
        states = {r["task_id"]: r["state"] for r in summary["tasks"]}
        self.assertEqual(states["b"], TASK_SKIPPED_BUDGET)
        self.assertIn("goal_max_total_tokens", summary["budget"]["reason"])

    def test_config_validation(self):
        config = _cfg(budget={"goal_max_total_tokens": 5, "goal_max_duration_seconds": 6})
        self.assertEqual(config["budget"]["goal_max_total_tokens"], 5)
        with self.assertRaises(ConfigValidationError):
            _cfg(budget={"goal_max_total_tokens": -1})


# ===========================================================================
# Phase 3 - parallelism and collisions
# ===========================================================================


class TestParallelExecution(unittest.TestCase):
    def setUp(self):
        self.repo = _GitRepo(prefix="orch_t5p_")

    def tearDown(self):
        self.repo.cleanup()

    def test_independent_tasks_really_do_overlap(self):
        active = {"now": 0, "peak": 0}
        lock = threading.Lock()

        def runner(state):
            with lock:
                active["now"] += 1
                active["peak"] = max(active["peak"], active["now"])
            time.sleep(0.15)
            with lock:
                active["now"] -= 1
            return _session()

        summary = run_goal(
            goal="g",
            plan=_plan(_task("a"), _task("b"), _task("c")),
            project_root=self.repo.path,
            config=_cfg(),
            session_runner=runner,
            max_parallel=3,
        )

        self.assertEqual(summary["status"], GOAL_COMPLETED)
        self.assertGreater(active["peak"], 1)

    def test_one_at_a_time_is_the_default(self):
        active = {"now": 0, "peak": 0}
        lock = threading.Lock()

        def runner(state):
            with lock:
                active["now"] += 1
                active["peak"] = max(active["peak"], active["now"])
            time.sleep(0.05)
            with lock:
                active["now"] -= 1
            return _session()

        run_goal(
            goal="g",
            plan=_plan(_task("a"), _task("b"), _task("c")),
            project_root=self.repo.path,
            config=_cfg(),
            session_runner=runner,
        )
        self.assertEqual(active["peak"], 1)

    def test_dependencies_are_still_respected_under_parallelism(self):
        order = []
        lock = threading.Lock()

        def runner(state):
            with lock:
                order.append(state["task_id"])
            return _session(workspace={"branch": f"branch-{state['task_id']}"})

        run_goal(
            goal="g",
            plan=_plan(_task("a"), _task("b"), _task("c", depends_on=["a", "b"])),
            project_root=self.repo.path,
            config=_cfg(),
            session_runner=runner,
            max_parallel=4,
        )
        self.assertEqual(order[-1], "c")

    def test_max_parallel_is_validated(self):
        self.assertEqual(get_delegation_config(_cfg(delegation={"max_parallel": 4}))["max_parallel"], 4)
        with self.assertRaises(ConfigValidationError):
            _cfg(delegation={"max_parallel": 0})
        with self.assertRaises(ConfigValidationError):
            _cfg(delegation={"stop_on_failure": "yes"})


class TestCollisionDetection(unittest.TestCase):
    """Siblings that edited the same file are reported, never reconciled."""

    def setUp(self):
        if not shutil.which("git"):
            self.skipTest("git is not installed")
        self.repo = _GitRepo(prefix="orch_t5c_")

    def tearDown(self):
        self.repo.cleanup()

    def _branch_touching(self, name, filename, content="x\n"):
        """Create a run branch whose single commit changes `filename`."""
        info = ws.create_run_worktree(self.repo.path, name)
        with open(os.path.join(info["path"], filename), "w", encoding="utf-8") as handle:
            handle.write(content)
        ws.commit_worktree(info, f"work for {name}")
        ws.finish_worktree(info, keep_worktree=False, commit="x")
        return info["branch"]

    def test_two_siblings_touching_one_file_collide(self):
        base = ws.head_commit(self.repo.path)
        outcomes = {
            "a": {"status": "completed", "branch": self._branch_touching("a", "shared.py"),
                  "base_ref": base},
            "b": {"status": "completed", "branch": self._branch_touching("b", "shared.py"),
                  "base_ref": base},
        }
        found = detect_collisions(self.repo.path, outcomes, [_task("a"), _task("b")])

        self.assertEqual(len(found), 1)
        self.assertEqual(sorted(found[0]["task_ids"]), ["a", "b"])
        self.assertEqual(found[0]["paths"], ["shared.py"])

    def test_tasks_touching_different_files_do_not_collide(self):
        base = ws.head_commit(self.repo.path)
        outcomes = {
            "a": {"status": "completed", "branch": self._branch_touching("a", "one.py"),
                  "base_ref": base},
            "b": {"status": "completed", "branch": self._branch_touching("b", "two.py"),
                  "base_ref": base},
        }
        self.assertEqual(detect_collisions(self.repo.path, outcomes, [_task("a"), _task("b")]), [])

    def test_a_dependent_task_is_not_colliding_with_its_dependency(self):
        # Building on top of someone's file is cooperation, not conflict.
        base = ws.head_commit(self.repo.path)
        outcomes = {
            "a": {"status": "completed", "branch": self._branch_touching("a", "shared.py"),
                  "base_ref": base},
            "b": {"status": "completed", "branch": self._branch_touching("b", "shared.py"),
                  "base_ref": base},
        }
        tasks = [_task("a"), _task("b", depends_on=["a"])]
        self.assertEqual(detect_collisions(self.repo.path, outcomes, tasks), [])

    def test_a_failed_task_is_not_counted_as_a_collision(self):
        base = ws.head_commit(self.repo.path)
        outcomes = {
            "a": {"status": "completed", "branch": self._branch_touching("a", "shared.py"),
                  "base_ref": base},
            "b": {"status": "failed", "branch": self._branch_touching("b", "shared.py"),
                  "base_ref": base},
        }
        self.assertEqual(detect_collisions(self.repo.path, outcomes, [_task("a"), _task("b")]), [])

    def test_a_collision_marks_both_tasks_and_blocks_the_goal(self):
        def runner(state):
            info = ws.create_run_worktree(self.repo.path, f"sess-{state['task_id']}")
            with open(os.path.join(info["path"], "shared.py"), "w", encoding="utf-8") as handle:
                handle.write(f"by {state['task_id']}\n")
            ws.commit_worktree(info, "work")
            ws.finish_worktree(info, keep_worktree=False, commit="x")
            return _session(workspace=dict(info))

        summary = run_goal(
            goal="g",
            plan=_plan(_task("a"), _task("b")),
            project_root=self.repo.path,
            config=_cfg(),
            session_runner=runner,
            max_parallel=2,
        )

        self.assertEqual(len(summary["collisions"]), 1)
        self.assertEqual(summary["status"], GOAL_BLOCKED)
        for row in summary["tasks"]:
            self.assertEqual(row["state"], TASK_NEEDS_ATTENTION)
        self.assertIn("COLLISIONS", format_goal_summary(summary))

    def test_a_non_repository_reports_no_collisions(self):
        import tempfile

        plain = tempfile.mkdtemp(prefix="orch_t5np_")
        try:
            self.assertEqual(detect_collisions(plain, {"a": {"branch": "x"}}, [_task("a")]), [])
        finally:
            shutil.rmtree(plain, ignore_errors=True)


# ===========================================================================
# The goal store
# ===========================================================================


class TestGoalStore(unittest.TestCase):
    def setUp(self):
        self.repo = _GitRepo(prefix="orch_t5s_")

    def tearDown(self):
        self.repo.cleanup()

    def test_a_goal_is_recorded_and_reads_back(self):
        summary = run_goal(
            goal="build the thing",
            plan=_plan(_task("a"), _task("b", depends_on=["a"])),
            project_root=self.repo.path,
            config=_cfg(),
            session_runner=lambda state: _session(tokens=10),
        )

        goal_id = summary["goal_id"]
        self.assertTrue(goal_id)

        listed = list_goals(self.repo.path)
        self.assertEqual(listed[0]["goal_id"], goal_id)
        self.assertEqual(listed[0]["status"], GOAL_COMPLETED)

        goal = load_goal(self.repo.path, goal_id)
        self.assertEqual(goal["goal"], "build the thing")
        self.assertEqual(len(goal["plan"]["tasks"]), 2)

        names = [e["event"] for e in goal["events"]]
        self.assertIn("goal_started", names)
        self.assertIn("goal_plan", names)
        self.assertIn("task_finished", names)
        self.assertIn("goal_finished", names)

    def test_outcomes_replay_from_the_log(self):
        summary = run_goal(
            goal="g",
            plan=_plan(_task("a"), _task("b", depends_on=["a"])),
            project_root=self.repo.path,
            config=_cfg(),
            session_runner=lambda state: (
                _session(status="failed") if state["task_id"] == "a" else _session()
            ),
        )
        goal = load_goal(self.repo.path, summary["goal_id"])
        outcomes = task_outcomes(goal)

        self.assertEqual(derive_task_state(outcomes["a"]), TASK_FAILED)
        self.assertEqual(derive_task_state(outcomes["b"]), TASK_SKIPPED_DEPENDENCY)

        # The summary derived from the log matches the one the scheduler returned.
        replayed = derive_goal_summary(goal["plan"]["tasks"], outcomes)
        self.assertEqual(replayed["status"], summary["status"])

    def test_ids_resolve_by_prefix_and_latest(self):
        summary = run_goal(
            goal="g",
            plan=_plan(_task("a")),
            project_root=self.repo.path,
            config=_cfg(),
            session_runner=lambda state: _session(),
        )
        goal_id = summary["goal_id"]
        self.assertEqual(resolve_goal_id(self.repo.path, "latest"), goal_id)
        self.assertEqual(resolve_goal_id(self.repo.path, goal_id[:10]), goal_id)
        self.assertIsNone(resolve_goal_id(self.repo.path, "nope"))

    def test_store_failures_never_break_a_goal(self):
        store = GoalStore("/nonexistent\x00path", "g1")
        store.record_goal_started("g", self.repo.path)
        store.record_task_finished("a", {"status": "completed"})
        store.record_goal_finished({"status": GOAL_COMPLETED})
        self.assertTrue(store.degraded)

    def test_persistence_can_be_turned_off(self):
        summary = run_goal(
            goal="g",
            plan=_plan(_task("a")),
            project_root=self.repo.path,
            config=_cfg(),
            session_runner=lambda state: _session(),
            store_enabled=False,
        )
        self.assertIsNone(summary["goal_id"])
        self.assertEqual(list_goals(self.repo.path), [])


# ===========================================================================
# End to end through the real pipeline
# ===========================================================================


class TestDelegationThroughThePipeline(unittest.TestCase):
    """A goal really does produce one branch per task, with agents mocked."""

    def setUp(self):
        if not shutil.which("git"):
            self.skipTest("git is not installed")
        self.repo = _GitRepo(prefix="orch_t5e2e_")

    def tearDown(self):
        self.repo.cleanup()

    def test_each_task_lands_on_its_own_branch(self):
        config = _cfg()
        session_graph = build_graph(config)

        def opencode(prompt, **kwargs):
            # The brief names the sibling tasks too, so identify this task by its intent.
            name = "alpha" if "write the alpha part" in prompt else "beta"
            with open(os.path.join(kwargs["working_dir"], f"{name}.txt"), "w", encoding="utf-8") as f:
                f.write(name)
            return ("implemented", {"available": True, "total_tokens": 10}, "headless")

        def claude(prompt, **kwargs):
            return ("VERDICT: PASS", {"available": True, "total_tokens": 5})

        with patch.object(graph_module, "run_claude_code", side_effect=claude), \
             patch.object(graph_module, "run_opencode", side_effect=opencode):
            summary = run_goal(
                goal="do both",
                plan=_plan(
                    dict(_task("alpha", "Alpha work"), intent="write the alpha part"),
                    dict(_task("beta", "Beta work"), intent="write the beta part"),
                ),
                project_root=self.repo.path,
                config=config,
                session_runner=lambda state: session_graph.invoke(state),
            )

        self.assertEqual(summary["status"], GOAL_COMPLETED, summary)
        self.assertEqual(summary["done"], 2)
        self.assertEqual(summary["tokens"], 30)

        branches = {b["branch"] for b in ws.list_run_branches(self.repo.path)}
        self.assertEqual(len(branches), 2)

        # Each branch carries only its own task's file, and the checkout is untouched.
        for row in summary["tasks"]:
            paths = ws.changed_paths(self.repo.path, summary["base_ref"], row["branch"])
            self.assertEqual(len(paths), 1, paths)
            self.assertIn(row["task_id"], paths[0])
        self.assertFalse(os.path.exists(os.path.join(self.repo.path, "alpha.txt")))
        self.assertEqual(summary["collisions"], [])

    def test_a_dependent_task_sees_its_dependencys_work(self):
        config = _cfg()
        session_graph = build_graph(config)
        saw = {}

        def opencode(prompt, **kwargs):
            work_dir = kwargs["working_dir"]
            if "write the first file" in prompt:
                with open(os.path.join(work_dir, "first.txt"), "w", encoding="utf-8") as f:
                    f.write("done")
            else:
                # The second task must be able to see what the first produced.
                saw["first_visible"] = os.path.isfile(os.path.join(work_dir, "first.txt"))
                with open(os.path.join(work_dir, "second.txt"), "w", encoding="utf-8") as f:
                    f.write("done")
            return ("implemented", {"available": False}, "headless")

        with patch.object(graph_module, "run_claude_code", return_value="VERDICT: PASS"), \
             patch.object(graph_module, "run_opencode", side_effect=opencode):
            summary = run_goal(
                goal="chain",
                plan=_plan(
                    dict(_task("one", "First step"), intent="write the first file"),
                    dict(
                        _task("two", "Second step", depends_on=["one"]),
                        intent="write the second file",
                    ),
                ),
                project_root=self.repo.path,
                config=config,
                session_runner=lambda state: session_graph.invoke(state),
            )

        self.assertEqual(summary["status"], GOAL_COMPLETED, summary)
        self.assertTrue(saw.get("first_visible"), "the dependent task could not see its dependency")


if __name__ == "__main__":
    unittest.main()
