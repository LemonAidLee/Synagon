"""Tests for the fourth round of fixes.

Covers:
  Worktree retention   - orchestrator.workspace.finish_worktree + finalize_node
  Branch pruning       - orchestrator.prune
  Run budgets          - orchestrator.budget + routing + derived status
  Resume               - orchestrator.resume + phase replay in the graph
  Aggregate analysis   - orchestrator.stats
"""

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

import orchestrator.graph as graph_module
from orchestrator import workspace as ws
from orchestrator.budget import (
    describe_budget,
    evaluate_budget,
    is_exhausted,
    seconds_elapsed,
    tokens_spent,
)
from orchestrator.config import (
    ConfigValidationError,
    get_budget_config,
    get_workspace_config,
    validate_config,
)
from orchestrator.graph import build_graph, should_repair_or_end
from orchestrator.prune import (
    execute_prune,
    format_prune_plan,
    parse_age,
    plan_prune,
)
from orchestrator.resume import (
    reconstruct_state,
    repairs_completed,
    replayable_roles,
    should_replay,
)
from orchestrator.stats import compute_stats, format_stats
from orchestrator.status import (
    STATUS_BUDGET_EXHAUSTED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_NEEDS_REPAIR,
    derive_status,
    derive_summary,
    is_successful,
)
from orchestrator.store import RunStore, load_run, runs_root

ROLES = {
    r: {"responsibility": r}
    for r in ("researcher", "planner", "implementer", "verifier")
}

FULL_PIPELINE = [
    {"agent": "antigravity", "model": "gemini-3.8-flash-high", "role": "researcher"},
    {"agent": "claude", "model": "sonnet", "role": "planner"},
    {"agent": "opencode", "model": "opencode/gpt-5.1-codex", "role": "implementer"},
    {"agent": "claude", "model": "sonnet", "role": "verifier"},
]


def _cfg(agents=None, **extra):
    config = {"agents": list(agents or FULL_PIPELINE), "roles": dict(ROLES)}
    config.update(extra)
    return validate_config(config)


def _usage(total):
    return {"available": True, "input_tokens": None, "output_tokens": None, "total_tokens": total}


def _result(role, agent="claude", status="success", tokens=None, **extra):
    res = {
        "agent": agent,
        "role": role,
        "status": status,
        "output": f"{role} output",
        "duration_seconds": 1.0,
        "model": "sonnet",
        "token_usage": _usage(tokens) if tokens is not None else {"available": False},
    }
    res.update(extra)
    return res


class _GitRepo:
    """A throwaway git repository with one commit."""

    def __init__(self, prefix="orch_t3_"):
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
            ["git"] + list(args),
            cwd=self.path,
            capture_output=True,
            stdin=subprocess.DEVNULL,
        )

    def cleanup(self):
        subprocess.run(["git", "worktree", "prune"], cwd=self.path, capture_output=True)
        shutil.rmtree(self.path, ignore_errors=True)


# ===========================================================================
# Worktree retention
# ===========================================================================


class TestWorktreeRetention(unittest.TestCase):
    """Committed work lives on the branch, so the checkout is not kept."""

    def setUp(self):
        if not shutil.which("git"):
            self.skipTest("git is not installed")
        self.repo = _GitRepo()

    def tearDown(self):
        self.repo.cleanup()

    def _write(self, info, name="agent_file.txt"):
        with open(os.path.join(info["path"], name), "w", encoding="utf-8") as handle:
            handle.write("work\n")

    def test_committed_worktree_is_removed_and_branch_kept(self):
        info = ws.create_run_worktree(self.repo.path, "run-a")
        self._write(info)
        commit = ws.commit_worktree(info, "run a")

        outcome = ws.finish_worktree(info, keep_worktree=False, commit=commit)

        self.assertTrue(outcome["removed"])
        self.assertFalse(outcome["branch_deleted"])
        self.assertFalse(os.path.isdir(info["path"]))
        branches = [b["branch"] for b in ws.list_run_branches(self.repo.path)]
        self.assertIn("orchestrator/run/run-a", branches)

    def test_dirty_worktree_is_kept_with_a_reason(self):
        info = ws.create_run_worktree(self.repo.path, "run-b")
        self._write(info)  # never committed

        outcome = ws.finish_worktree(info, keep_worktree=False)

        self.assertFalse(outcome["removed"])
        self.assertIn("uncommitted", outcome["retained_reason"])
        self.assertTrue(os.path.isdir(info["path"]))

    def test_keep_worktree_wins_over_removal(self):
        info = ws.create_run_worktree(self.repo.path, "run-c")
        outcome = ws.finish_worktree(info, keep_worktree=True)
        self.assertFalse(outcome["removed"])
        self.assertTrue(os.path.isdir(info["path"]))

    def test_run_that_changed_nothing_leaves_no_branch(self):
        info = ws.create_run_worktree(self.repo.path, "run-d")
        outcome = ws.finish_worktree(info, keep_worktree=False)

        self.assertTrue(outcome["removed"])
        self.assertTrue(outcome["branch_deleted"])
        self.assertEqual(ws.list_run_branches(self.repo.path), [])

    def test_unisolated_workspace_is_a_no_op(self):
        outcome = ws.finish_worktree({"isolated": False, "path": self.repo.path})
        self.assertFalse(outcome["removed"])
        self.assertFalse(outcome["branch_deleted"])

    def test_commits_ahead_counts_only_the_runs_own_work(self):
        info = ws.create_run_worktree(self.repo.path, "run-e")
        self.assertEqual(ws.commits_ahead(info), 0)
        self._write(info)
        ws.commit_worktree(info, "run e")
        self.assertEqual(ws.commits_ahead(info), 1)

    def test_default_config_no_longer_keeps_worktrees(self):
        self.assertFalse(get_workspace_config(None)["keep_worktree"])


class TestRetentionInGraph(unittest.TestCase):
    """finalize_node applies the retention policy at the end of a real run."""

    def setUp(self):
        if not shutil.which("git"):
            self.skipTest("git is not installed")
        self.repo = _GitRepo(prefix="orch_t3g_")

    def tearDown(self):
        self.repo.cleanup()

    def _run(self, **state):
        def impl(prompt, **kwargs):
            with open(os.path.join(kwargs["working_dir"], "made.txt"), "w", encoding="utf-8") as f:
                f.write("x\n")
            return ("done", {"available": False}, "headless")

        def claude(prompt, **kwargs):
            if kwargs.get("role") == "verifier":
                return "VERDICT: PASS\nok"
            return "plan"

        with patch.object(graph_module, "run_antigravity", return_value="research"), \
             patch.object(graph_module, "run_claude_code", side_effect=claude), \
             patch.object(graph_module, "run_opencode", side_effect=impl):
            base = {
                "task": "make a file",
                "project_root": self.repo.path,
                "run_store_enabled": False,
                "agent_results": [],
            }
            base.update(state)
            return build_graph(_cfg()).invoke(base)

    def test_finished_run_leaves_the_branch_but_not_the_checkout(self):
        result = self._run(config=_cfg())

        summary = result["workspace_summary"]
        self.assertIsNotNone(summary.get("commit"))
        self.assertTrue(summary["retention"]["removed"])
        self.assertFalse(os.path.isdir(result["workspace"]["path"]))
        branches = [b["branch"] for b in ws.list_run_branches(self.repo.path)]
        self.assertEqual(len(branches), 1)

    def test_keep_worktree_config_retains_the_checkout(self):
        config = _cfg(workspace={"keep_worktree": True})
        result = self._run(config=config)

        self.assertFalse(result["workspace_summary"]["retention"]["removed"])
        self.assertTrue(os.path.isdir(result["workspace"]["path"]))


# ===========================================================================
# Pruning run branches
# ===========================================================================


class TestParseAge(unittest.TestCase):
    def test_units(self):
        self.assertEqual(parse_age("30d"), 30 * 86400)
        self.assertEqual(parse_age("12h"), 12 * 3600)
        self.assertEqual(parse_age("2w"), 2 * 604800)
        self.assertEqual(parse_age("90m"), 90 * 60)
        self.assertEqual(parse_age("45s"), 45)

    def test_bare_number_means_days(self):
        self.assertEqual(parse_age("7"), 7 * 86400)

    def test_rejects_nonsense(self):
        for bad in ("", "soon", "30x", "-5d"):
            with self.assertRaises(ValueError):
                parse_age(bad)


class TestPrune(unittest.TestCase):
    """Pruning is explicit, conservative, and explains every exclusion."""

    def setUp(self):
        if not shutil.which("git"):
            self.skipTest("git is not installed")
        self.repo = _GitRepo(prefix="orch_t3p_")

    def tearDown(self):
        self.repo.cleanup()

    def _finished_run(self, run_id, status="completed"):
        """Create a run branch with a committed change, plus its store record."""
        info = ws.create_run_worktree(self.repo.path, run_id)
        with open(os.path.join(info["path"], f"{run_id}.txt"), "w", encoding="utf-8") as handle:
            handle.write("work\n")
        commit = ws.commit_worktree(info, f"run {run_id}")
        ws.finish_worktree(info, keep_worktree=False, commit=commit)

        store = RunStore.create(self.repo.path, run_id=run_id)
        store.record_run_started(task="t", project_root=self.repo.path)
        store.record_run_finished({"status": status, "verdict": "PASS" if status == "completed" else "FAIL"})
        return info

    def test_recent_branches_are_kept(self):
        self._finished_run("run-new")
        plan = plan_prune(self.repo.path, older_than_seconds=parse_age("30d"))

        self.assertEqual(plan["total"], 1)
        self.assertEqual(plan["prunable"], [])
        self.assertIn("newer than the threshold", plan["kept"][0]["keep_reason"])

    def test_old_branches_are_prunable_and_deleted(self):
        self._finished_run("run-old")
        plan = plan_prune(self.repo.path, older_than_seconds=0)

        self.assertEqual(len(plan["prunable"]), 1)
        self.assertEqual(plan["prunable"][0]["status"], "completed")

        result = execute_prune(self.repo.path, plan)
        self.assertEqual(len(result["deleted"]), 1)
        self.assertEqual(result["failed"], [])
        self.assertEqual(ws.list_run_branches(self.repo.path), [])

    def test_keep_failed_protects_unsuccessful_runs(self):
        self._finished_run("run-ok", status="completed")
        self._finished_run("run-bad", status="failed")

        plan = plan_prune(self.repo.path, older_than_seconds=0, keep_failed=True)

        self.assertEqual([c["run_id"] for c in plan["prunable"]], ["run-ok"])
        self.assertIn("did not succeed", plan["kept"][0]["keep_reason"])

    def test_keep_failed_protects_runs_with_no_record(self):
        info = ws.create_run_worktree(self.repo.path, "run-unrecorded")
        with open(os.path.join(info["path"], "f.txt"), "w", encoding="utf-8") as handle:
            handle.write("x\n")
        commit = ws.commit_worktree(info, "unrecorded")
        ws.finish_worktree(info, keep_worktree=False, commit=commit)

        plan = plan_prune(self.repo.path, older_than_seconds=0, keep_failed=True)
        self.assertEqual(plan["prunable"], [])
        self.assertIn("no recorded run", plan["kept"][0]["keep_reason"])

    def test_a_dirty_worktree_protects_its_branch(self):
        info = ws.create_run_worktree(self.repo.path, "run-dirty")
        with open(os.path.join(info["path"], "uncommitted.txt"), "w", encoding="utf-8") as handle:
            handle.write("precious\n")

        plan = plan_prune(self.repo.path, older_than_seconds=0)

        self.assertEqual(plan["prunable"], [])
        self.assertIn("uncommitted", plan["kept"][0]["keep_reason"])
        self.assertTrue(os.path.isdir(info["path"]))

    def test_checked_out_branch_is_never_pruned(self):
        self.repo.git("checkout", "-q", "-b", "orchestrator/run/checked-out")
        plan = plan_prune(self.repo.path, older_than_seconds=0)
        self.assertEqual(plan["prunable"], [])
        self.assertIn("checked out", plan["kept"][0]["keep_reason"])

    def test_plan_is_readable(self):
        self._finished_run("run-report")
        text = format_prune_plan(plan_prune(self.repo.path, older_than_seconds=0))
        self.assertIn("WILL DELETE", text)
        self.assertIn("orchestrator/run/run-report", text)

    def test_non_repo_reports_why_it_cannot_prune(self):
        plain = tempfile.mkdtemp(prefix="orch_t3np_")
        try:
            plan = plan_prune(plain, older_than_seconds=0)
            self.assertFalse(plan["available"])
            self.assertIn("not a git repository", plan["reason"])
            self.assertIn("Cannot prune", format_prune_plan(plan))
        finally:
            shutil.rmtree(plain, ignore_errors=True)


# ===========================================================================
# Run budgets
# ===========================================================================


class TestBudgetAccounting(unittest.TestCase):
    """Spend is measured from reported usage only; nothing is imputed."""

    def test_sums_only_reported_usage(self):
        results = [
            _result("researcher", tokens=100),
            _result("planner"),  # unavailable
            _result("implementer", tokens=250),
        ]
        self.assertEqual(tokens_spent(results), 350)

    def test_falls_back_to_input_plus_output(self):
        results = [
            {
                "token_usage": {
                    "available": True,
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "total_tokens": None,
                }
            }
        ]
        self.assertEqual(tokens_spent(results), 15)

    def test_unavailable_usage_can_never_trigger_an_overrun(self):
        state = {
            "agent_results": [_result("implementer"), _result("verifier")],
            "budget": {"max_total_tokens": 1},
        }
        self.assertFalse(is_exhausted(state))

    def test_elapsed_prefers_the_wall_clock(self):
        state = {"run_started_at": time.time() - 30, "agent_results": [_result("planner")]}
        self.assertGreaterEqual(seconds_elapsed(state), 29)

    def test_elapsed_falls_back_to_execution_durations(self):
        state = {"agent_results": [_result("planner"), _result("implementer")]}
        self.assertEqual(seconds_elapsed(state), 2.0)

    def test_zero_means_unlimited(self):
        state = {"agent_results": [_result("planner", tokens=10**9)], "budget": {}}
        evaluated = evaluate_budget(state)
        self.assertFalse(evaluated["exhausted"])
        self.assertIn("no limit", describe_budget(evaluated))

    def test_token_ceiling_names_the_setting_that_stopped_the_run(self):
        state = {
            "agent_results": [_result("implementer", tokens=500)],
            "budget": {"max_total_tokens": 400},
        }
        evaluated = evaluate_budget(state)
        self.assertTrue(evaluated["exhausted"])
        self.assertIn("budget.max_total_tokens", evaluated["reason"])
        self.assertIn("500", evaluated["reason"])

    def test_time_ceiling(self):
        state = {
            "run_started_at": time.time() - 120,
            "agent_results": [],
            "budget": {"max_duration_seconds": 60},
        }
        evaluated = evaluate_budget(state)
        self.assertTrue(evaluated["exhausted"])
        self.assertIn("budget.max_duration_seconds", evaluated["reason"])


class TestBudgetConfig(unittest.TestCase):
    def test_defaults_to_unlimited(self):
        # 0 everywhere: per-session ceilings and the goal-level ones added for delegation.
        resolved = get_budget_config(None)
        self.assertEqual(set(resolved.values()), {0})
        self.assertEqual(resolved["max_total_tokens"], 0)
        self.assertEqual(resolved["max_duration_seconds"], 0)

    def test_validates_and_reads_back(self):
        config = validate_config(
            {
                "agents": FULL_PIPELINE,
                "roles": dict(ROLES),
                "budget": {"max_total_tokens": 50000, "max_duration_seconds": 900},
            }
        )
        self.assertEqual(get_budget_config(config)["max_total_tokens"], 50000)
        self.assertEqual(get_budget_config(config)["max_duration_seconds"], 900)

    def test_rejects_negative_and_non_integer_budgets(self):
        for bad in ({"max_total_tokens": -1}, {"max_duration_seconds": "soon"}, {"max_total_tokens": True}):
            with self.assertRaises(ConfigValidationError):
                validate_config({"agents": FULL_PIPELINE, "roles": dict(ROLES), "budget": bad})

    def test_rejects_non_mapping(self):
        with self.assertRaises(ConfigValidationError):
            validate_config({"agents": FULL_PIPELINE, "roles": dict(ROLES), "budget": []})


class TestBudgetRouting(unittest.TestCase):
    """The ladder stops climbing when the ceiling is reached."""

    def _failing_state(self, **extra):
        state = {
            "verification_history": [{"attempt": 1, "verdict": "FAIL", "repair_attempts": 0}],
            "verification_verdict": "FAIL",
            "repair_attempts": 0,
            "max_repair_attempts": 2,
            "agent_results": [_result("implementer", tokens=1000)],
        }
        state.update(extra)
        return state

    def test_repairs_when_budget_remains(self):
        state = self._failing_state(budget={"max_total_tokens": 10000})
        self.assertEqual(should_repair_or_end(state), "repair_node")

    def test_stops_when_the_token_budget_is_spent(self):
        state = self._failing_state(budget={"max_total_tokens": 500})
        self.assertEqual(should_repair_or_end(state), "finalize")

    def test_stops_on_the_recorded_fact_without_re_measuring(self):
        state = self._failing_state(budget_exhausted_reason="time budget exhausted")
        self.assertEqual(should_repair_or_end(state), "finalize")

    def test_a_pass_is_never_overridden_by_the_budget(self):
        state = self._failing_state(
            budget={"max_total_tokens": 1},
            verification_history=[{"attempt": 1, "verdict": "PASS", "repair_attempts": 0}],
            verification_verdict="PASS",
        )
        self.assertEqual(should_repair_or_end(state), "finalize")
        self.assertEqual(derive_status(state), STATUS_COMPLETED)


class TestBudgetStatus(unittest.TestCase):
    """budget_exhausted is derived from the recorded fact, not from a clock."""

    def _state(self, **extra):
        state = {
            "verification_history": [{"attempt": 1, "verdict": "FAIL", "repair_attempts": 0}],
            "repair_attempts": 0,
            "max_repair_attempts": 2,
        }
        state.update(extra)
        return state

    def test_without_the_fact_the_run_merely_needs_repair(self):
        self.assertEqual(derive_status(self._state()), STATUS_NEEDS_REPAIR)

    def test_with_the_fact_the_run_is_budget_exhausted(self):
        state = self._state(budget_exhausted_reason="token budget exhausted: 9 of 5")
        self.assertEqual(derive_status(state), STATUS_BUDGET_EXHAUSTED)
        self.assertFalse(is_successful(STATUS_BUDGET_EXHAUSTED))

    def test_budget_outranks_a_spent_repair_budget(self):
        state = self._state(
            repair_attempts=2,
            budget_exhausted_reason="token budget exhausted",
        )
        self.assertEqual(derive_status(state), STATUS_BUDGET_EXHAUSTED)

    def test_spent_repairs_alone_still_fail(self):
        self.assertEqual(derive_status(self._state(repair_attempts=2)), STATUS_FAILED)

    def test_summary_carries_the_budget(self):
        state = self._state(
            budget_state={"tokens_spent": 90, "max_total_tokens": 80},
            budget_exhausted_reason="token budget exhausted",
        )
        summary = derive_summary(state)
        self.assertEqual(summary["status"], STATUS_BUDGET_EXHAUSTED)
        self.assertEqual(summary["budget"]["tokens_spent"], 90)
        self.assertIn("token budget", summary["budget_exhausted_reason"])

    def test_status_of_a_stored_run_does_not_drift_with_time(self):
        # An old run replayed today must not accrue wall-clock time against the
        # ceiling it ran under: the fact, not the clock, decides.
        state = self._state(
            run_started_at=time.time() - 100000,
            budget={"max_duration_seconds": 60},
        )
        self.assertEqual(derive_status(state), STATUS_NEEDS_REPAIR)


class TestBudgetInGraph(unittest.TestCase):
    """A run that blows its ceiling stops after verification, not mid-agent."""

    def test_ladder_stops_and_records_why(self):
        calls = {"impl": 0, "verify": 0}

        def impl(prompt, **kwargs):
            calls["impl"] += 1
            return ("implemented", {"available": True, "total_tokens": 5000}, "headless")

        def claude(prompt, **kwargs):
            if kwargs.get("role") == "verifier":
                calls["verify"] += 1
                return ("VERDICT: FAIL\nnot yet", {"available": True, "total_tokens": 100})
            return ("plan", {"available": True, "total_tokens": 100})

        config = _cfg(budget={"max_total_tokens": 1000})
        with patch.object(graph_module, "run_antigravity", return_value=("research", {"available": True, "total_tokens": 100})), \
             patch.object(graph_module, "run_claude_code", side_effect=claude), \
             patch.object(graph_module, "run_opencode", side_effect=impl):
            result = build_graph(config).invoke(
                {
                    "task": "t",
                    "project_root": os.getcwd(),
                    "run_store_enabled": False,
                    "config": config,
                    "agent_results": [],
                    "workspace": {"isolated": False, "path": os.getcwd(), "reason": "test"},
                }
            )

        self.assertEqual(result["status"], STATUS_BUDGET_EXHAUSTED)
        self.assertIn("max_total_tokens", result["budget_exhausted_reason"])
        # No repair was funded, even though two attempts were still allowed.
        self.assertEqual(calls["impl"], 1)
        self.assertEqual(calls["verify"], 1)
        self.assertEqual(result.get("repair_attempts", 0), 0)

    def test_generous_budget_leaves_the_repair_loop_alone(self):
        attempts = {"impl": 0}

        def impl(prompt, **kwargs):
            attempts["impl"] += 1
            return ("implemented", {"available": True, "total_tokens": 10}, "headless")

        def claude(prompt, **kwargs):
            if kwargs.get("role") == "verifier":
                verdict = "PASS" if attempts["impl"] > 1 else "FAIL"
                return (f"VERDICT: {verdict}", {"available": True, "total_tokens": 10})
            return ("plan", {"available": True, "total_tokens": 10})

        config = _cfg(budget={"max_total_tokens": 1000000})
        with patch.object(graph_module, "run_antigravity", return_value=("research", {"available": True, "total_tokens": 10})), \
             patch.object(graph_module, "run_claude_code", side_effect=claude), \
             patch.object(graph_module, "run_opencode", side_effect=impl):
            result = build_graph(config).invoke(
                {
                    "task": "t",
                    "project_root": os.getcwd(),
                    "run_store_enabled": False,
                    "config": config,
                    "agent_results": [],
                    "workspace": {"isolated": False, "path": os.getcwd(), "reason": "test"},
                }
            )

        self.assertEqual(result["status"], STATUS_COMPLETED)
        self.assertEqual(result["repair_attempts"], 1)
        self.assertIsNone(result.get("budget_exhausted_reason"))


# ===========================================================================
# Resume
# ===========================================================================


def _stored_run(tmp_root, events, run_id="20260101T000000Z-abcdef", status="failed"):
    """Write a run to a real store and read it back the way --resume does."""
    store = RunStore.create(tmp_root, run_id=run_id)
    store.record_run_started(task="rebuild the widget", project_root=tmp_root)
    for kind, payload in events:
        if kind == "result":
            store.record_agent_result(payload)
        elif kind == "verification":
            store.record_verification(payload)
        elif kind == "workspace":
            store.record_event("workspace", workspace=payload)
    store.record_run_finished({"status": status, "verdict": "FAIL"})
    return load_run(tmp_root, run_id)


class TestReconstruction(unittest.TestCase):
    """A stored run replays into state as facts, and only facts."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="orch_t3r_")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_replays_results_and_verifications(self):
        run = _stored_run(
            self.root,
            [
                ("result", _result("researcher", agent="antigravity")),
                ("result", _result("planner")),
                ("result", _result("implementer", agent="opencode")),
                ("result", _result("verifier", verdict="FAIL")),
                ("verification", {"attempt": 1, "verdict": "FAIL", "repair_attempts": 0, "output": "no"}),
            ],
        )
        state = reconstruct_state(run)

        self.assertEqual(state["task"], "rebuild the widget")
        self.assertEqual(len(state["agent_results"]), 4)
        self.assertEqual(len(state["verification_history"]), 1)
        self.assertTrue(all(r["restored"] for r in state["agent_results"]))
        self.assertEqual(state["resumed_from"], run["run_id"])

    def test_does_not_carry_derived_values(self):
        run = _stored_run(self.root, [("result", _result("planner"))])
        state = reconstruct_state(run)
        for derived in ("status", "summary", "verification_verdict"):
            self.assertNotIn(derived, state)

    def test_restores_the_recorded_workspace(self):
        info = {"isolated": True, "path": "/tmp/wt", "branch": "orchestrator/run/x"}
        run = _stored_run(self.root, [("workspace", info)])
        self.assertEqual(reconstruct_state(run)["workspace"]["branch"], "orchestrator/run/x")

    def test_repairs_are_counted_from_the_records(self):
        results = [
            _result("implementer", agent="opencode"),
            _result("implementer", agent="opencode", repair_attempt=1),
        ]
        self.assertEqual(repairs_completed(results), 1)
        self.assertEqual(
            repairs_completed([], [{"attempt": 2, "repair_attempts": 3}]), 3
        )

    def test_a_failed_repair_is_not_counted_as_spent_work(self):
        results = [_result("implementer", status="error", repair_attempt=1)]
        self.assertEqual(repairs_completed(results), 0)

    def test_a_truncated_event_log_resumes_from_what_is_readable(self):
        # A process killed mid-write leaves a half-written final line. `load_run` skips what
        # cannot be parsed and the resume continues from the rest - never a JSON error.
        store = RunStore.create(self.root, run_id="20260101T000000Z-truncate")
        store.record_run_started(task="rebuild the widget", project_root=self.root)
        store.record_agent_result(_result("planner"))
        with open(store.events_path, "a", encoding="utf-8") as handle:
            handle.write('{"event": "agent_result", "result": ')

        run = load_run(self.root, "20260101T000000Z-truncate")
        self.assertIsNotNone(run)
        self.assertEqual(len(run["events"]), 2)  # the broken tail line was skipped
        state = reconstruct_state(run)
        self.assertEqual(state["task"], "rebuild the widget")
        self.assertEqual(len(state["agent_results"]), 1)

    def test_a_garbage_store_reconstructs_as_nothing_rather_than_raising(self):
        # A store whose files were clobbered reads back as an empty run: no event is invented,
        # and no JSONDecodeError escapes into the graph.
        run_dir = runs_root(self.root) / "20260101T000000Z-garbage"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "run.json").write_text("{ definitely not json", encoding="utf-8")
        (run_dir / "events.jsonl").write_text(
            "{not json\n{\"event\": ", encoding="utf-8", errors="replace"
        )

        run = load_run(self.root, "20260101T000000Z-garbage")
        self.assertIsNotNone(run)
        self.assertEqual(list(run.get("events") or []), [])
        state = reconstruct_state(run)  # must not raise
        self.assertEqual(state["agent_results"], [])
        self.assertEqual(state["resumed_from"], "20260101T000000Z-garbage")


class TestReplayDecision(unittest.TestCase):
    """Replay is decided from the restored records, per repair generation."""

    def _state(self, results=(), history=(), **extra):
        state = {
            "resumed_from": "run-1",
            "agent_results": [dict(r, restored=True) for r in results],
            "verification_history": [dict(h, restored=True) for h in history],
        }
        state.update(extra)
        return state

    def test_nothing_replays_outside_a_resume(self):
        state = {"agent_results": [dict(_result("planner"), restored=True)]}
        self.assertFalse(should_replay(state, "planner"))

    def test_completed_phases_replay(self):
        state = self._state([_result("researcher"), _result("planner")])
        self.assertTrue(should_replay(state, "researcher"))
        self.assertTrue(should_replay(state, "planner"))
        self.assertFalse(should_replay(state, "implementer"))

    def test_a_wholly_failed_phase_is_re_run(self):
        state = self._state([_result("planner", status="error")])
        self.assertFalse(should_replay(state, "planner"))

    def test_verification_replays_only_for_its_own_repair_generation(self):
        state = self._state(history=[{"attempt": 1, "verdict": "FAIL", "repair_attempts": 0}])
        self.assertTrue(should_replay(state, "verifier", repair_attempts=0))
        self.assertFalse(should_replay(state, "verifier", repair_attempts=1))

    def test_repair_replays_only_the_attempt_already_made(self):
        state = self._state([_result("implementer", repair_attempt=1)])
        self.assertTrue(should_replay(state, "implementer", is_repair=True, repair_attempts=0))
        self.assertFalse(should_replay(state, "implementer", is_repair=True, repair_attempts=1))

    def test_initial_implementation_is_not_satisfied_by_a_repair(self):
        state = self._state([_result("implementer", repair_attempt=1)])
        self.assertFalse(should_replay(state, "implementer"))

    def test_replayable_roles_reports_what_will_be_reused(self):
        state = self._state(
            [_result("researcher"), _result("planner"), _result("implementer")],
        )
        self.assertEqual(replayable_roles(state), ["researcher", "planner", "implementer"])


class TestResumeInGraph(unittest.TestCase):
    """A resumed run pays for the phases the original never finished, and no others."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="orch_t3rg_")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _invoke(self, state, verifier_output="VERDICT: PASS\nok"):
        calls = {"research": 0, "plan": 0, "impl": 0, "verify": 0}

        def antigravity(prompt, **kwargs):
            calls["research"] += 1
            return "research"

        def claude(prompt, **kwargs):
            if kwargs.get("role") == "verifier":
                calls["verify"] += 1
                return verifier_output
            calls["plan"] += 1
            return "plan"

        def opencode(prompt, **kwargs):
            calls["impl"] += 1
            return ("implemented", {"available": False}, "headless")

        config = _cfg()
        base = {
            "project_root": self.root,
            "run_store_enabled": False,
            "config": config,
            "workspace": {"isolated": False, "path": self.root, "reason": "test"},
        }
        base.update(state)
        with patch.object(graph_module, "run_antigravity", side_effect=antigravity), \
             patch.object(graph_module, "run_claude_code", side_effect=claude), \
             patch.object(graph_module, "run_opencode", side_effect=opencode):
            result = build_graph(config).invoke(base)
        return result, calls

    def test_a_crash_before_verification_only_pays_for_the_verifier(self):
        run = _stored_run(
            self.root,
            [
                ("result", _result("researcher", agent="antigravity")),
                ("result", _result("planner")),
                ("result", _result("implementer", agent="opencode")),
            ],
        )
        state = reconstruct_state(run)
        state["run_dir"] = None  # exercise the graph, not the store
        state["run_store_enabled"] = False

        result, calls = self._invoke(state)

        self.assertEqual(calls, {"research": 0, "plan": 0, "impl": 0, "verify": 1})
        self.assertEqual(result["status"], STATUS_COMPLETED)

    def test_a_crash_after_a_failed_verification_resumes_at_the_repair(self):
        run = _stored_run(
            self.root,
            [
                ("result", _result("researcher", agent="antigravity")),
                ("result", _result("planner")),
                ("result", _result("implementer", agent="opencode")),
                ("result", _result("verifier", verdict="FAIL")),
                ("verification", {"attempt": 1, "verdict": "FAIL", "repair_attempts": 0, "output": "no"}),
            ],
        )
        state = reconstruct_state(run)
        state["run_dir"] = None
        state["run_store_enabled"] = False

        result, calls = self._invoke(state)

        # The recorded verification is replayed, so the run routes straight into
        # a repair and one fresh verification.
        self.assertEqual(calls["research"], 0)
        self.assertEqual(calls["plan"], 0)
        self.assertEqual(calls["impl"], 1)
        self.assertEqual(calls["verify"], 1)
        self.assertEqual(result["repair_attempts"], 1)
        self.assertEqual(result["status"], STATUS_COMPLETED)

    def test_a_fully_replayed_run_launches_nothing(self):
        run = _stored_run(
            self.root,
            [
                ("result", _result("researcher", agent="antigravity")),
                ("result", _result("planner")),
                ("result", _result("implementer", agent="opencode")),
                ("result", _result("verifier", verdict="PASS")),
                ("verification", {"attempt": 1, "verdict": "PASS", "repair_attempts": 0, "output": "ok"}),
            ],
            status="completed",
        )
        state = reconstruct_state(run)
        state["run_dir"] = None
        state["run_store_enabled"] = False

        result, calls = self._invoke(state)

        self.assertEqual(calls, {"research": 0, "plan": 0, "impl": 0, "verify": 0})
        self.assertEqual(result["status"], STATUS_COMPLETED)

    def test_a_member_that_died_last_session_is_not_this_phases_failure(self):
        # The crashed verifier is history. Re-running the phase must be judged on
        # what it produces now, not blamed for the previous session's death.
        run = _stored_run(
            self.root,
            [
                ("result", _result("researcher", agent="antigravity")),
                ("result", _result("planner")),
                ("result", _result("implementer", agent="opencode")),
                ("result", _result("verifier", status="error")),
            ],
            status="error",
        )
        state = reconstruct_state(run)
        state["run_dir"] = None
        state["run_store_enabled"] = False

        with patch.object(graph_module.default_tracer, "log_phase_partial_failure") as warned:
            result, calls = self._invoke(state)

        warned.assert_not_called()
        self.assertEqual(calls["verify"], 1)
        self.assertEqual(result["status"], STATUS_COMPLETED)

    def test_resume_appends_to_the_original_run_record(self):
        run = _stored_run(self.root, [("result", _result("planner"))])
        store = RunStore.from_dir(run["run_dir"])
        store.record_run_resumed(replayed_roles=["planner"], repair_attempts=0)

        reloaded = load_run(self.root, run["run_id"])
        names = [e.get("event") for e in reloaded["events"]]
        self.assertIn("run_resumed", names)
        self.assertEqual(reloaded["status"], "running")
        self.assertEqual(reloaded["resumed_count"], 1)


# ===========================================================================
# Aggregate analysis
# ===========================================================================


def _run_record(run_id, status, verdict, results, verifications=(), settings=None):
    events = [{"event": "agent_result", "result": r} for r in results]
    events += [{"event": "verification", "record": v} for v in verifications]
    return {
        "run_id": run_id,
        "status": status,
        "verdict": verdict,
        "settings": settings or {},
        "events": events,
    }


class TestStats(unittest.TestCase):
    """The stats report answers questions, and never invents an answer."""

    def test_empty_store_says_so(self):
        report = compute_stats([])
        self.assertEqual(report["runs_total"], 0)
        self.assertIn("No recorded runs", format_stats(report))

    def test_counts_outcomes_with_their_denominator(self):
        runs = [
            _run_record("a", "completed", "PASS", [_result("verifier", verdict="PASS")]),
            _run_record("b", "failed", "FAIL", [_result("verifier", verdict="FAIL")]),
            _run_record("c", "blocked", "BLOCKED", [_result("verifier", verdict="BLOCKED")]),
            _run_record("d", "budget_exhausted", "FAIL", [_result("verifier", verdict="FAIL")]),
        ]
        report = compute_stats(runs)
        self.assertEqual(report["outcomes"]["finished"], 4)
        self.assertEqual(report["outcomes"]["pass_rate"], 25.0)
        self.assertEqual(report["outcomes"]["blocked"], 1)
        self.assertEqual(report["outcomes"]["budget_exhausted"], 1)

    def test_cost_excludes_runs_with_unreported_usage(self):
        runs = [
            _run_record("a", "completed", "PASS", [_result("implementer", tokens=1000)]),
            _run_record("b", "failed", "FAIL", [_result("implementer", tokens=3000)]),
            _run_record("c", "failed", "FAIL", [_result("implementer")]),  # no usage
        ]
        report = compute_stats(runs)
        self.assertEqual(report["cost"]["mean_tokens_pass"], 1000)
        self.assertEqual(report["cost"]["mean_tokens_fail"], 3000)
        self.assertEqual(report["cost"]["runs_with_incomplete_usage"], 1)

    def test_pairings_carry_pass_rate_and_sample_size(self):
        runs = [
            _run_record(
                "a", "completed", "PASS",
                [
                    _result("verifier", agent="claude", verdict="PASS", tokens=10),
                    _result("verifier", agent="claude", verdict="FAIL", tokens=20),
                ],
            )
        ]
        report = compute_stats(runs)
        row = report["pairings"][0]
        self.assertEqual(row["executions"], 2)
        self.assertEqual(row["pass_rate"], 50.0)
        self.assertEqual(row["mean_tokens"], 15.0)
        self.assertTrue(row["thin"])

    def test_repair_effectiveness_is_reported_per_attempt(self):
        runs = [
            _run_record(
                "a", "completed", "PASS", [],
                verifications=[
                    {"attempt": 1, "verdict": "FAIL", "repair_attempts": 0},
                    {"attempt": 2, "verdict": "FAIL", "repair_attempts": 1},
                    {"attempt": 3, "verdict": "PASS", "repair_attempts": 2},
                ],
            ),
            _run_record(
                "b", "failed", "FAIL", [],
                verifications=[
                    {"attempt": 1, "verdict": "FAIL", "repair_attempts": 0},
                    {"attempt": 2, "verdict": "PASS", "repair_attempts": 1},
                ],
            ),
        ]
        report = compute_stats(runs)
        by_attempt = {row["attempt"]: row for row in report["repair"]["by_attempt"]}
        self.assertEqual(report["repair"]["runs_with_at_least_one_repair"], 2)
        self.assertEqual(by_attempt[1]["attempted"], 2)
        self.assertEqual(by_attempt[1]["passed"], 1)
        self.assertEqual(by_attempt[2]["pass_rate"], 100.0)

    def test_ensembles_are_compared_on_outcome_and_cost(self):
        single = _run_record(
            "a", "failed", "FAIL",
            [_result("verifier", agent="claude", verdict="FAIL", tokens=100)],
        )
        pair = _run_record(
            "b", "completed", "PASS",
            [
                dict(_result("verifier", agent="claude", verdict="PASS", tokens=100), model="opus"),
                dict(_result("verifier", agent="claude", verdict="PASS", tokens=150), model="sonnet"),
            ],
        )
        report = compute_stats([single, pair])
        self.assertEqual(report["ensembles"]["single verifier"]["pass_rate"], 0.0)
        self.assertEqual(report["ensembles"]["2 verifiers"]["pass_rate"], 100.0)
        self.assertEqual(report["ensembles"]["2 verifiers"]["mean_tokens"], 250.0)

    def test_outcomes_are_grouped_by_recorded_setting(self):
        runs = [
            _run_record("a", "completed", "PASS", [], settings={"consensus": "unanimous"}),
            _run_record("b", "failed", "FAIL", [], settings={"consensus": "unanimous"}),
            _run_record("c", "completed", "PASS", [], settings={"consensus": "majority"}),
        ]
        report = compute_stats(runs)
        self.assertEqual(report["settings"]["consensus"]["unanimous"]["runs"], 2)
        self.assertEqual(report["settings"]["consensus"]["unanimous"]["pass_rate"], 50.0)
        self.assertEqual(report["settings"]["consensus"]["majority"]["pass_rate"], 100.0)

    def test_report_renders_and_flags_thin_samples(self):
        runs = [_run_record("a", "completed", "PASS", [_result("verifier", verdict="PASS", tokens=5)])]
        text = format_stats(compute_stats(runs))
        self.assertIn("OUTCOMES", text)
        self.assertIn("too thin", text)

    def test_report_is_json_serializable(self):
        runs = [_run_record("a", "completed", "PASS", [_result("verifier", verdict="PASS")])]
        json.dumps(compute_stats(runs))


if __name__ == "__main__":
    unittest.main()
