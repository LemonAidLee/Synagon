"""Tests for the five steps in Roadmap section 8.

Covers:
  8.1  The office watches a Goal, not the newest run - `serve.goal_sessions` and the
       `sessions` key `latest_activity` now carries
  8.2  The planning agent's memory - `orchestrator.memory`, its budget, and the one place
       it reaches the model
  8.3  The evidence beside the choice - `stats.evidence_for_choices` and `/api/stats`
  8.4  Retention that knows about delivery - a merged branch is *more* sweepable, and its
       record outlives it
  8.5  Review mode - the third mode of the local server, and the only one that can decide
       something about a run

The server tests start the real server and make real HTTP requests, for the reason the
Phase 7 tests do: a refusal that is only asserted against a mock is a refusal nobody proved.
"""

import json
import os
import shutil
import tempfile
import unittest
import urllib.error
import urllib.request

from orchestrator.config import ConfigValidationError, get_planning_config, validate_config
from orchestrator.delivery import (
    deliveries_by_branch,
    list_deliveries,
    record_branch_pruned,
    save_delivery,
)
from orchestrator.goals import GoalStore
from orchestrator.memory import (
    DEFAULT_BUDGET_CHARS,
    format_memory,
    memory_section,
    project_memory,
    summarise,
)
from orchestrator.prompts import build_decomposer_prompt
from orchestrator.prune import (
    DEFAULT_MERGED_MAX_AGE,
    format_prune_plan,
    parse_age,
    plan_prune,
)
from orchestrator.serve import (
    MAX_SESSIONS,
    goal_sessions,
    latest_activity,
    serve_board,
)
from orchestrator.stats import evidence_for_choices
from orchestrator.status import DELIVERY_MERGED, STATUS_COMPLETED, TASK_DONE
from orchestrator.store import RunStore

ROLES = {
    r: {"responsibility": r}
    for r in ("decomposer", "researcher", "planner", "implementer", "verifier")
}

PIPELINE = [
    {"agent": "opencode", "model": "opencode/gpt-5.1-codex", "role": "implementer"},
    {"agent": "claude", "model": "sonnet", "role": "verifier"},
]

MINIMAL_YAML = """\
agents:
  - agent: opencode
    model: opencode/gpt-5.1-codex
    role: implementer

  - agent: claude
    model: sonnet
    role: verifier

max_repair_attempts: 2

verification:
  consensus: unanimous

roles:
  implementer:
    responsibility: write the code
  verifier:
    responsibility: check the code
"""


def _cfg(**extra):
    config = {"agents": list(PIPELINE), "roles": dict(ROLES)}
    config.update(extra)
    return validate_config(config)


# ===========================================================================
# 8.1 - the office watches a Goal
# ===========================================================================


class TestTheOfficeWatchesAGoal(unittest.TestCase):
    """With `--parallel N` there are several sessions, and each one is a desk.

    Before 8.1 the office read the newest run *directory*, so three sessions shared one desk
    and it followed whichever had written most recently. These assert the projection that
    replaces that: one entry per session the goal started, each with its own events.
    """

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="orch_t10goal_")
        self.config = _cfg()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _session(self, run_id, task, task_id, goal_id, status=STATUS_COMPLETED, agents=None):
        store = RunStore.create(self.root, run_id=run_id)
        store.record_run_started(task=task, project_root=self.root, config=self.config)
        store.record_agent_started(agent="claude", role="implementer", model="sonnet")
        store.record_run_finished({"status": status, "verdict": "PASS"})
        # `run.json` is what pairs a run with its goal, so write the two ids onto it.
        meta_path = os.path.join(store.run_dir, "run.json")
        with open(meta_path, encoding="utf-8") as handle:
            meta = json.load(handle)
        meta["goal_id"] = goal_id
        meta["task_id"] = task_id
        meta["agents"] = agents or [
            {"agent": "opencode", "role": "implementer"},
            {"agent": "claude", "role": "verifier"},
        ]
        with open(meta_path, "w", encoding="utf-8") as handle:
            json.dump(meta, handle)
        return store

    def _goal_with(self, pairs):
        """Create a goal whose log names each (task_id, run_id) pair."""
        goal = GoalStore.create(self.root, goal_id="20260908T000000Z-goal01")
        goal.record_goal_started(goal="ship three things", project_root=self.root)
        for task_id, run_id in pairs:
            self._session(run_id, f"task {task_id}", task_id, goal.goal_id)
            goal.record_task_started(task_id, run_id, "abc123")
            goal.record_task_finished(
                task_id,
                {
                    "task_id": task_id,
                    "started": True,
                    "run_id": run_id,
                    "status": STATUS_COMPLETED,
                    "verdict": "PASS",
                    "branch": f"orchestrator/run/{run_id}",
                    "base_ref": "abc123",
                    "tokens": 1000,
                },
            )
        return goal

    def test_every_task_of_a_goal_becomes_its_own_session(self):
        goal = self._goal_with([
            ("alpha", "20260908T000001Z-aaaaaa"),
            ("beta", "20260908T000002Z-bbbbbb"),
            ("gamma", "20260908T000003Z-cccccc"),
        ])
        sessions = goal_sessions(self.root, self.config, goal.goal_id)
        self.assertEqual([s["task_id"] for s in sessions], ["alpha", "beta", "gamma"])
        self.assertEqual(len({s["run_id"] for s in sessions}), 3)

    def test_each_session_carries_its_own_events(self):
        goal = self._goal_with([
            ("alpha", "20260908T000001Z-aaaaaa"),
            ("beta", "20260908T000002Z-bbbbbb"),
        ])
        sessions = goal_sessions(self.root, self.config, goal.goal_id)
        for session in sessions:
            with self.subTest(task=session["task_id"]):
                self.assertTrue(session["events"], "a session with no events is a blank desk")
                self.assertTrue(session["agents"])

    def test_a_task_with_no_run_is_still_named(self):
        """A skipped or queued task has no session to draw, and an office that omitted it
        would be quietly claiming the goal is smaller than it is."""
        goal = self._goal_with([("alpha", "20260908T000001Z-aaaaaa")])
        goal.record_task_skipped("beta", "dependency", "alpha failed")

        sessions = goal_sessions(self.root, self.config, goal.goal_id)
        by_id = {s["task_id"]: s for s in sessions}
        self.assertIn("beta", by_id)
        self.assertIsNone(by_id["beta"]["run_id"])
        self.assertEqual(by_id["beta"]["events"], [])

    def test_the_number_of_desks_is_bounded(self):
        goal = self._goal_with([
            (f"task{i}", "20260908T0000%02dZ-aaaaa%d" % (i, i))
            for i in range(MAX_SESSIONS + 4)
        ])
        sessions = goal_sessions(self.root, self.config, goal.goal_id)
        self.assertEqual(len(sessions), MAX_SESSIONS)

    def test_a_run_without_a_goal_reports_itself_as_one_session(self):
        """Nothing that consumed this payload before 8.1 has to change: a lone run is a
        team of one, and lays out exactly where the single desk always was."""
        store = RunStore.create(self.root, run_id="20260908T000009Z-solo00")
        store.record_run_started(task="one thing", project_root=self.root, config=self.config)
        store.record_run_finished({"status": STATUS_COMPLETED, "verdict": "PASS"})

        activity = latest_activity(self.root, self.config)
        self.assertEqual(len(activity["sessions"]), 1)
        self.assertEqual(activity["sessions"][0]["run_id"], "20260908T000009Z-solo00")
        self.assertIsNone(activity["goal_id"])

    def test_activity_carries_the_whole_team_when_there_is_a_goal(self):
        self._goal_with([
            ("alpha", "20260908T000001Z-aaaaaa"),
            ("beta", "20260908T000002Z-bbbbbb"),
        ])
        activity = latest_activity(self.root, self.config)
        self.assertEqual(len(activity["sessions"]), 2)
        # And the keys the office already read are all still there.
        for key in ("run_id", "task", "status", "goal_id", "events", "goal_events"):
            self.assertIn(key, activity)

    def test_no_goal_at_all_is_an_empty_list_not_a_crash(self):
        self.assertEqual(goal_sessions(self.root, self.config, None), [])
        self.assertEqual(goal_sessions(self.root, self.config, "not-a-goal"), [])


class TestTheOfficePageDrawsATeam(unittest.TestCase):
    """Properties of `office.html` that are behaviour rather than taste."""

    @classmethod
    def setUpClass(cls):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "orchestrator", "web", "office.html"), encoding="utf-8") as h:
            cls.text = h.read()

    def test_it_lays_out_a_row_per_session(self):
        self.assertIn("ensureTeam", self.text)
        self.assertIn("sessionKey", self.text)

    def test_events_are_attributed_to_a_session(self):
        # One high-water mark per session: a single counter silently swallows the slower one.
        self.assertIn("seenBySession", self.text)
        self.assertNotIn("let seen = 0;", self.text)

    def test_it_still_depends_on_nothing_remote(self):
        for marker in ("https://", "<script src", "@import url("):
            with self.subTest(marker=marker):
                self.assertNotIn(marker, self.text)


# ===========================================================================
# 8.3 - the evidence beside the choice
# ===========================================================================


class TestEvidenceForChoices(unittest.TestCase):
    """`--stats` reports pairings; a dropdown offers an agent, a model, or a role."""

    def _report(self, *pairings):
        return {"runs_total": 9, "pairings": list(pairings)}

    def _pairing(self, agent, model, role, executions, verdicts, rate, tokens=None):
        return {
            "agent": agent, "model": model, "role": role,
            "executions": executions, "verdicts": verdicts,
            "pass_rate": rate, "mean_tokens": tokens,
        }

    def test_it_folds_one_pairing_three_ways(self):
        evidence = evidence_for_choices(
            self._report(self._pairing("claude", "sonnet", "verifier", 10, 10, 40.0, 12000))
        )
        self.assertEqual(evidence["by_agent"]["claude"]["pass_rate"], 40.0)
        self.assertEqual(evidence["by_model"]["claude/sonnet"]["pass_rate"], 40.0)
        self.assertEqual(evidence["by_role"]["verifier"]["pass_rate"], 40.0)

    def test_folding_two_pairings_sums_their_denominators(self):
        evidence = evidence_for_choices(self._report(
            self._pairing("claude", "sonnet", "verifier", 10, 10, 40.0, 12000),
            self._pairing("claude", "opus", "verifier", 10, 10, 80.0, 20000),
        ))
        role = evidence["by_role"]["verifier"]
        self.assertEqual(role["verdicts"], 20)
        self.assertEqual(role["passes"], 12)           # 4 + 8
        self.assertEqual(role["pass_rate"], 60.0)

    def test_never_judged_is_not_the_same_as_always_failed(self):
        """A role with no verdict must report None, not 0%. Reporting zero would be a
        finding this project never observed."""
        evidence = evidence_for_choices(
            self._report(self._pairing("claude", "sonnet", "planner", 4, 0, None, None))
        )
        row = evidence["by_role"]["planner"]
        self.assertIsNone(row["pass_rate"])
        self.assertIsNone(row["tokens_per_run"])
        self.assertEqual(row["runs"], 4)

    def test_a_thin_sample_says_so(self):
        evidence = evidence_for_choices(
            self._report(self._pairing("claude", "sonnet", "verifier", 1, 1, 100.0, 500))
        )
        self.assertTrue(evidence["by_role"]["verifier"]["thin"])

    def test_cost_is_weighted_by_how_often_a_pairing_ran(self):
        """Folding a rare pairing with a common one must not give them equal weight."""
        evidence = evidence_for_choices(self._report(
            self._pairing("claude", "sonnet", "verifier", 9, 9, 100.0, 1000),
            self._pairing("claude", "opus", "verifier", 1, 1, 100.0, 11000),
        ))
        self.assertEqual(evidence["by_role"]["verifier"]["tokens_per_run"], 2000.0)

    def test_an_empty_report_is_an_empty_memory_not_a_crash(self):
        for report in ({}, None, {"pairings": []}):
            with self.subTest(report=report):
                evidence = evidence_for_choices(report)
                self.assertEqual(evidence["by_agent"], {})
                self.assertEqual(evidence["by_model"], {})


class TestThePagesAskForTheEvidence(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.here = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "orchestrator", "web"
        )

    def _page(self, name):
        with open(os.path.join(self.here, name), encoding="utf-8") as handle:
            return handle.read()

    def test_the_design_surface_renders_it_beside_the_dropdowns(self):
        page = self._page("design.html")
        self.assertIn("/api/stats", page)
        for kind in ("by_agent", "by_model", "by_role"):
            with self.subTest(kind=kind):
                self.assertIn(kind, page)

    def test_the_cockpit_renders_it_too(self):
        self.assertIn("/api/stats", self._page("cockpit.html"))

    def test_the_evidence_route_is_only_ever_read(self):
        for name in ("design.html", "cockpit.html"):
            with self.subTest(page=name):
                page = self._page(name)
                self.assertNotIn('fetch("/api/stats", {\n      method', page)


# ===========================================================================
# 8.4 - retention that knows about delivery
# ===========================================================================


class TestMergedIsMoreSweepable(unittest.TestCase):
    """A merged branch's work is in the base branch, so the branch is a duplicate."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="orch_t10prune_")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _delivered(self, branch, merged=True):
        record = {
            "id": "goal__task-" + branch.rsplit("/", 1)[-1],
            "branch": branch,
            "pushed": True,
            "created_at": "2026-09-01T00:00:00Z",
            "pr": {"number": 4, "url": "https://forge/pr/4",
                   "state": "MERGED" if merged else "OPEN",
                   "merged_at": "2026-09-02T00:00:00Z" if merged else None},
        }
        return save_delivery(self.root, record)

    def test_the_default_merged_age_is_shorter_than_the_ordinary_one(self):
        self.assertLess(parse_age(DEFAULT_MERGED_MAX_AGE), parse_age("30d"))

    def test_deliveries_can_be_looked_up_by_branch(self):
        self._delivered("orchestrator/run/aaa")
        self._delivered("orchestrator/run/bbb", merged=False)
        index = deliveries_by_branch(self.root)
        self.assertEqual(set(index), {"orchestrator/run/aaa", "orchestrator/run/bbb"})

    def test_pruning_annotates_the_record_and_never_deletes_it(self):
        """The one thing retention must not be able to do is make the board forget that
        work landed."""
        record = self._delivered("orchestrator/run/aaa")
        record_branch_pruned(self.root, record)

        records = list_deliveries(self.root)
        self.assertEqual(len(records), 1)
        kept = records[0]
        self.assertTrue(kept.get("local_branch_pruned_at"))
        self.assertEqual(kept["pr"]["state"], "MERGED")
        self.assertIn(
            "local_branch_pruned", [h.get("event") for h in kept.get("history") or []]
        )

    def test_a_plan_without_git_says_so_rather_than_guessing(self):
        plan = plan_prune(self.root, older_than_seconds=1, merged_older_than_seconds=0)
        self.assertFalse(plan["available"])
        self.assertIn("not a git repository", plan["reason"])

    def test_the_plan_carries_the_merged_threshold_it_used(self):
        plan = plan_prune(self.root, older_than_seconds=100, merged_older_than_seconds=7)
        self.assertEqual(plan["merged_older_than_seconds"], 7)

    def test_the_report_names_merged_branches(self):
        plan = {
            "available": True, "total": 2, "kept": [],
            "prunable": [
                {"branch": "orchestrator/run/aaa", "age": "3d", "status": "completed",
                 "delivery": DELIVERY_MERGED, "worktree": None},
                {"branch": "orchestrator/run/bbb", "age": "40d", "status": "completed",
                 "delivery": "local", "worktree": None},
            ],
        }
        report = format_prune_plan(plan)
        self.assertIn("1 of them merged", report)
        self.assertIn("DELIVERY", report)


class TestMergedInPlanPrune(unittest.TestCase):
    """The decision itself, with git and the delivery store both stubbed to known answers."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="orch_t10plan_")
        self.now = 1_000_000.0
        self.branches = [
            {"branch": "orchestrator/run/merged", "run_id": "merged",
             "commit": "aaa", "committed_at": self.now - 3600 * 5},      # 5h old
            {"branch": "orchestrator/run/local", "run_id": "local",
             "commit": "bbb", "committed_at": self.now - 3600 * 5},      # 5h old
        ]

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _plan(self, **kwargs):
        import orchestrator.prune as prune_module
        from unittest.mock import patch

        deliveries = {
            "orchestrator/run/merged": {
                "id": "d-merged", "branch": "orchestrator/run/merged",
                "pr": {"number": 1, "state": "MERGED", "merged_at": "2026-09-02T00:00:00Z"},
            }
        }
        with patch.object(prune_module, "git_available", return_value=True), \
             patch.object(prune_module, "is_git_repo", return_value=True), \
             patch.object(prune_module, "list_run_branches", return_value=self.branches), \
             patch.object(prune_module, "list_worktrees", return_value=[]), \
             patch.object(prune_module, "current_branch", return_value="master"), \
             patch.object(prune_module, "deliveries_by_branch", return_value=deliveries):
            return prune_module.plan_prune(self.root, now=self.now, **kwargs)

    def _names(self, entries):
        return {e["branch"] for e in entries}

    def test_a_merged_branch_qualifies_on_its_own_shorter_threshold(self):
        plan = self._plan(
            older_than_seconds=30 * 86400,          # nothing qualifies on this
            merged_older_than_seconds=3600,          # the merged one does
        )
        self.assertEqual(self._names(plan["prunable"]), {"orchestrator/run/merged"})
        self.assertEqual(self._names(plan["kept"]), {"orchestrator/run/local"})

    def test_the_plan_says_why_a_merged_branch_is_sweepable(self):
        plan = self._plan(older_than_seconds=30 * 86400, merged_older_than_seconds=3600)
        self.assertIn("merged", plan["prunable"][0]["prune_reason"])
        self.assertEqual(plan["prunable"][0]["delivery"], DELIVERY_MERGED)

    def test_keep_failed_does_not_hold_back_work_that_landed(self):
        """`keep_failed` asks "could this still need inspecting?"; a merge answered it."""
        plan = self._plan(
            older_than_seconds=3600, merged_older_than_seconds=3600, keep_failed=True
        )
        self.assertIn("orchestrator/run/merged", self._names(plan["prunable"]))
        # The undelivered one has no recorded run, so `keep_failed` still keeps it.
        self.assertIn("orchestrator/run/local", self._names(plan["kept"]))

    def test_a_merged_branch_that_is_too_new_is_still_kept(self):
        plan = self._plan(older_than_seconds=30 * 86400, merged_older_than_seconds=86400)
        self.assertEqual(plan["prunable"], [])

    def test_without_the_shorter_threshold_nothing_changes(self):
        """`merged_older_than_seconds=None` is exactly the pre-8.4 behaviour."""
        plan = self._plan(older_than_seconds=30 * 86400, merged_older_than_seconds=None)
        self.assertEqual(plan["prunable"], [])


# ===========================================================================
# 8.5 - review mode
# ===========================================================================


class _ModeCase(unittest.TestCase):
    design = False
    review = False

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="orch_t10mode_")
        self.path = os.path.join(self.root, "orchestrator.yaml")
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(MINIMAL_YAML)

        store = RunStore.create(self.root, run_id="20260908T000000Z-aaaaaa")
        store.record_run_started(task="do the thing", project_root=self.root, config=_cfg())
        store.record_run_finished({"status": STATUS_COMPLETED, "verdict": "PASS"})

        self.server = serve_board(
            self.root, _cfg(), port=0, open_browser=False, printer=lambda *_: None,
            serve_forever=False, design=self.design, review=self.review,
        )
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.root, ignore_errors=True)

    def _get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=5) as response:
            return response.status, json.loads(response.read())

    def _post(self, path, payload):
        request = urllib.request.Request(
            self.base + path, data=json.dumps(payload).encode("utf-8"),
            method="POST", headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())


class TestServeCannotDecideAnything(_ModeCase):
    """`--serve` is read-only, and 8.5 must not have quietly relaxed that."""

    def test_it_reports_itself_as_read_only(self):
        _, modes = self._get("/api/modes")
        self.assertTrue(modes["read_only"])
        self.assertFalse(modes["review"])
        self.assertFalse(modes["team_writable"])

    def test_every_review_route_is_refused(self):
        for action in ("approve", "reject", "deliver"):
            with self.subTest(action=action):
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    self._post("/api/review/" + action, {"id": "anything"})
                self.assertEqual(ctx.exception.code, 405)

    def test_approvals_are_readable_without_being_answerable(self):
        status, body = self._get("/api/approvals")
        self.assertEqual(status, 200)
        self.assertEqual(body["approvals"], [])


class TestDesignCannotReview(_ModeCase):
    """Modes are not a ladder: being allowed to edit the team is not being allowed to
    approve, and collapsing the two would make one of them accidental."""

    design = True

    def test_it_can_write_the_team(self):
        _, modes = self._get("/api/modes")
        self.assertTrue(modes["team_writable"])

    def test_but_it_cannot_answer_a_gate(self):
        self.assertFalse(self._get("/api/modes")[1]["review"])
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post("/api/review/approve", {"id": "anything"})
        self.assertEqual(ctx.exception.code, 405)


class TestReviewCanDecideAndNothingElse(_ModeCase):
    review = True

    def test_it_reports_itself_as_a_reviewer(self):
        _, modes = self._get("/api/modes")
        self.assertTrue(modes["review"])
        self.assertFalse(modes["team_writable"])
        self.assertFalse(modes["read_only"])

    def test_it_cannot_edit_the_team(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post("/api/team", {"agents": []})
        self.assertEqual(ctx.exception.code, 405)
        with open(self.path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), MINIMAL_YAML)

    def test_a_gate_that_does_not_exist_is_refused_with_a_reason(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post("/api/review/approve", {"id": "no-such-gate"})
        self.assertEqual(ctx.exception.code, 400)
        body = json.loads(ctx.exception.read())
        self.assertIn("no approval matching", body["error"])

    def test_it_answers_a_real_gate(self):
        from orchestrator.approvals import list_approvals, request_approval

        gate = request_approval(self.root, "before_merge", subject="task-a: a branch")
        status, result = self._post(
            "/api/review/approve", {"id": gate["id"], "note": "looks right"}
        )
        self.assertEqual(status, 200)
        self.assertTrue(result["ok"])
        decided = list_approvals(self.root)[0]
        self.assertEqual(decided["status"], "approved")
        self.assertEqual(decided["note"], "looks right")

    def test_a_gate_cannot_be_answered_twice(self):
        from orchestrator.approvals import request_approval

        gate = request_approval(self.root, "before_merge", subject="task-a: a branch")
        self._post("/api/review/approve", {"id": gate["id"]})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post("/api/review/reject", {"id": gate["id"]})
        self.assertEqual(ctx.exception.code, 400)
        self.assertIn("already", json.loads(ctx.exception.read())["error"])

    def test_a_card_that_does_not_exist_is_refused_by_name(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post("/api/review/deliver", {"card": "no-such-card"})
        self.assertEqual(ctx.exception.code, 400)
        self.assertIn("no card matching", json.loads(ctx.exception.read())["error"])

    def test_a_card_the_board_did_not_call_ready_is_refused(self):
        """The board's judgement gates delivery, not the server's - a caller cannot smuggle
        in a card the projection never put in that column."""
        failed = RunStore.create(self.root, run_id="20260908T000001Z-bbbbbb")
        failed.record_run_started(task="a broken thing", project_root=self.root, config=_cfg())
        failed.record_run_finished({"status": "failed", "verdict": "FAIL"})

        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post("/api/review/deliver", {"card": "20260908T000001Z-bbbbbb"})
        self.assertEqual(ctx.exception.code, 400)
        self.assertIn("Ready to Merge", json.loads(ctx.exception.read())["error"])

    def test_a_ready_card_goes_through_the_delivery_path_refusals_and_all(self):
        """Review does not reimplement delivery: a Ready card reaches `deliver_card`, and
        this project ships with `delivery.enabled` false, so that is what refuses it."""
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post("/api/review/deliver", {"card": "20260908T000000Z-aaaaaa"})
        self.assertEqual(ctx.exception.code, 400)
        body = json.loads(ctx.exception.read())
        self.assertIn("delivery.enabled is false", body["delivery"]["refused"])

    def test_an_action_that_is_not_one_of_the_three_is_not_a_route(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post("/api/review/merge", {})
        self.assertEqual(ctx.exception.code, 404)

    def test_it_still_cannot_start_work(self):
        for route in ("/api/board", "/api/activity", "/api/goals", "/api/stats"):
            with self.subTest(route=route):
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    self._post(route, {})
                self.assertEqual(ctx.exception.code, 405)


class TestThereIsOneImplementationOfApprove(unittest.TestCase):
    """`--review`, the daemon and the CLI must not each mean something different by it."""

    def test_the_daemon_delegates_to_the_shared_act(self):
        import inspect

        from orchestrator.daemon import Daemon

        self.assertIn("answer_gate", inspect.getsource(Daemon.decide))
        self.assertIn("deliver_card_by_id", inspect.getsource(Daemon.deliver))


# ===========================================================================
# 8.2 - the planning agent's memory
# ===========================================================================


def _observed(**overrides):
    """One goal's worth of observations, in the shape `observe` produces."""
    base = {
        "goal_id": "g1",
        "goal": "add a health endpoint",
        "started_at": "2026-09-01T00:00:00Z",
        "status": "completed",
        "task_count": 2,
        "tasks": [
            {"task_id": "a", "state": TASK_DONE, "tokens": 10_000,
             "duration_seconds": 60.0, "touched": ["orchestrator/config.py"],
             "areas": ["orchestrator/config.py"], "touched_source": "diff"},
            {"task_id": "b", "state": TASK_DONE, "tokens": 20_000,
             "duration_seconds": 90.0, "touched": ["orchestrator/config.py", "tests/x.py"],
             "areas": [], "touched_source": "diff"},
        ],
        "collisions": [{"task_ids": ["a", "b"], "paths": ["orchestrator/config.py"]}],
    }
    base.update(overrides)
    return base


class TestTheProjection(unittest.TestCase):
    """The memory is a projection over stores that already exist, not new bookkeeping."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="orch_t10mem_")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _memory(self, observed):
        return project_memory(self.root, None, observed=observed)

    def test_a_collision_is_keyed_by_the_path_not_the_task_ids(self):
        """A task id is unique inside its goal and means nothing in the next one; the path
        is the thing the next decomposition can avoid."""
        memory = self._memory([_observed(), _observed(goal_id="g2")])
        self.assertEqual(len(memory["collisions"]), 1)
        row = memory["collisions"][0]
        self.assertEqual(row["path"], "orchestrator/config.py")
        self.assertEqual(row["times"], 2)
        self.assertEqual(row["goals"], 2)
        self.assertEqual(row["pairs"], [["a", "b"]])

    def test_collisions_rank_by_how_often_they_happened(self):
        quiet = _observed(
            goal_id="g2",
            collisions=[{"task_ids": ["c", "d"], "paths": ["README.md"]}],
        )
        memory = self._memory([_observed(), _observed(goal_id="g3"), quiet])
        self.assertEqual([r["path"] for r in memory["collisions"]],
                         ["orchestrator/config.py", "README.md"])

    def test_hot_paths_count_tasks_not_file_changes(self):
        """A task that rewrote forty files in one directory counts once for that directory:
        the question is whether tasks keep landing there, not how much churn there was."""
        memory = self._memory([_observed()])
        hot = {row["path"]: row for row in memory["hot_paths"]}
        self.assertEqual(hot["orchestrator/"]["tasks"], 2)
        self.assertEqual(hot["orchestrator/"]["goals"], 1)
        self.assertEqual(hot["tests/"]["tasks"], 1)

    def test_a_declared_area_is_marked_as_such(self):
        """When retention has swept a branch git cannot diff it, and the task's declared
        `areas` stand in. Presenting a prediction as an observation would be a lie."""
        observed = _observed(tasks=[
            {"task_id": "a", "state": TASK_DONE, "tokens": 5,
             "duration_seconds": 1.0, "touched": ["docs/guide.md"],
             "areas": ["docs/guide.md"], "touched_source": "declared"},
        ])
        memory = self._memory([observed])
        row = next(r for r in memory["hot_paths"] if r["path"] == "docs/")
        self.assertEqual(row["declared_only"], 1)

    def test_a_top_level_file_is_its_own_hot_spot(self):
        observed = _observed(tasks=[
            {"task_id": "a", "state": TASK_DONE, "tokens": 5, "duration_seconds": 1.0,
             "touched": ["CHANGELOG.md"], "areas": [], "touched_source": "diff"},
        ])
        memory = self._memory([observed])
        self.assertEqual(memory["hot_paths"][0]["path"], "CHANGELOG.md")

    def test_cost_separates_what_delivered_from_what_did_not(self):
        observed = _observed(tasks=[
            {"task_id": "a", "state": TASK_DONE, "tokens": 10_000,
             "duration_seconds": 1.0, "touched": [], "areas": [], "touched_source": "none"},
            {"task_id": "b", "state": "failed", "tokens": 40_000,
             "duration_seconds": 1.0, "touched": [], "areas": [], "touched_source": "none"},
        ])
        memory = self._memory([observed])
        self.assertEqual(memory["cost"]["median_tokens_delivered"], 10_000)
        self.assertEqual(memory["cost"]["median_tokens_not_delivered"], 40_000)
        self.assertEqual(memory["cost"]["tasks_delivered"], 1)

    def test_a_cost_nobody_reported_is_none_rather_than_zero(self):
        observed = _observed(tasks=[
            {"task_id": "a", "state": TASK_DONE, "tokens": 0,
             "duration_seconds": 1.0, "touched": [], "areas": [], "touched_source": "none"},
        ])
        memory = self._memory([observed])
        self.assertIsNone(memory["cost"]["median_tokens_delivered"])
        self.assertEqual(memory["cost"]["measured_for_cost"], 0)

    def test_a_thin_memory_says_it_is_thin(self):
        self.assertTrue(self._memory([_observed()])["thin"])
        self.assertFalse(
            self._memory([_observed(goal_id=f"g{i}") for i in range(5)])["thin"]
        )

    def test_no_goals_is_an_empty_memory_not_a_crash(self):
        memory = self._memory([])
        self.assertEqual(memory["goals_read"], 0)
        self.assertEqual(memory["collisions"], [])
        self.assertEqual(memory["hot_paths"], [])

    def test_a_rejection_is_remembered_with_its_reason(self):
        from orchestrator.approvals import request_approval, resolve_approval

        gate = request_approval(self.root, "before_merge", subject="task-a: a branch")
        resolve_approval(self.root, gate["id"], "rejected", note="wrong approach entirely")

        memory = project_memory(self.root, None, observed=[_observed()])
        self.assertEqual(len(memory["rejections"]), 1)
        self.assertEqual(memory["rejections"][0]["note"], "wrong approach entirely")

    def test_an_approved_gate_is_not_a_rejection(self):
        from orchestrator.approvals import request_approval, resolve_approval

        gate = request_approval(self.root, "before_merge", subject="fine")
        resolve_approval(self.root, gate["id"], "approved", note="ship it")
        memory = project_memory(self.root, None, observed=[_observed()])
        self.assertEqual(memory["rejections"], [])


class TestTheSummariserAndItsBudget(unittest.TestCase):
    """A memory that grows without a ceiling is a context window that eventually fails."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="orch_t10sum_")
        self.memory = project_memory(
            self.root, None,
            observed=[_observed(goal_id=f"g{i}") for i in range(6)],
        )

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_it_stays_inside_its_budget(self):
        for budget in (200, 600, 1200, DEFAULT_BUDGET_CHARS):
            with self.subTest(budget=budget):
                block = summarise(self.memory, budget_chars=budget)
                # The header alone may exceed a tiny budget; nothing beyond it may.
                self.assertLessEqual(len(block), max(budget, 400) + 120)

    def test_the_most_actionable_section_survives_the_smallest_budget(self):
        """A collision is a pair of tasks that should not have been emitted together, which
        is the one thing here that can change a decomposition."""
        block = summarise(self.memory, budget_chars=600)
        self.assertIn("fought over", block)

    def test_a_truncated_memory_says_it_was_truncated(self):
        block = summarise(self.memory, budget_chars=600)
        self.assertIn("omitted to stay within the memory budget", block)

    def test_an_empty_memory_produces_no_block_at_all(self):
        empty = project_memory(self.root, None, observed=[])
        self.assertEqual(summarise(empty), "")

    def test_it_never_gives_an_instruction(self):
        """Facts, and only facts: a memory that editorialised would be a second planner
        nobody could inspect."""
        block = summarise(self.memory)
        self.assertIn("recorded facts, not", block)

    def test_a_thin_memory_warns_the_reader_in_the_block(self):
        thin = project_memory(self.root, None, observed=[_observed()])
        self.assertIn("anecdote", summarise(thin))

    def test_the_terminal_report_says_when_there_is_nothing(self):
        empty = project_memory(self.root, None, observed=[])
        self.assertIn("decomposed cold", format_memory(empty))

    def test_the_terminal_report_shows_what_the_prompt_will(self):
        report = format_memory(self.memory)
        self.assertIn("orchestrator/config.py", report)
        self.assertIn("Read 6 goal(s)", report)

    def test_building_a_section_never_raises(self):
        # A project root that is not a directory is the harshest realistic case.
        self.assertEqual(memory_section(os.path.join(self.root, "nope")), "")


class TestTheMemoryReachesTheModel(unittest.TestCase):
    """One place, and only one: the decomposer's prompt."""

    def test_the_block_is_carried_under_its_own_heading(self):
        prompt = build_decomposer_prompt(
            "decomposer", "break goals down", "context", "a goal",
            memory="`config.py` — 3 collision(s)",
        )
        self.assertIn("### WHAT THIS REPOSITORY HAS TAUGHT US:", prompt)
        self.assertIn("3 collision(s)", prompt)

    def test_without_a_memory_the_prompt_is_the_cold_prompt(self):
        cold = build_decomposer_prompt("decomposer", "r", "context", "a goal")
        self.assertNotIn("### WHAT THIS REPOSITORY HAS TAUGHT US:", cold)
        self.assertNotIn("{memory_section}", cold)

    def test_whitespace_is_not_a_memory(self):
        blank = build_decomposer_prompt("decomposer", "r", "c", "g", memory="   \n  ")
        self.assertNotIn("### WHAT THIS REPOSITORY HAS TAUGHT US:", blank)

    def test_the_instructions_tell_the_planner_what_to_do_with_it(self):
        prompt = build_decomposer_prompt("decomposer", "r", "c", "g", memory="x")
        self.assertIn("that collision has happened", prompt)

    def test_the_decomposer_node_builds_it_from_the_configured_bounds(self):
        import inspect

        import orchestrator.graph as graph_module

        source = inspect.getsource(graph_module)
        self.assertIn("memory_section(", source)
        self.assertIn('memory_cfg.get("enabled"', source)


class TestTheMemoryIsConfigurable(unittest.TestCase):
    def test_it_is_on_by_default(self):
        memory = get_planning_config(validate_config(
            {"agents": list(PIPELINE), "roles": dict(ROLES)}
        )).get("memory")
        self.assertTrue(memory["enabled"])
        self.assertGreater(memory["max_goals"], 0)
        self.assertGreater(memory["budget_chars"], 0)

    def test_it_can_be_turned_off(self):
        config = validate_config({
            "agents": list(PIPELINE), "roles": dict(ROLES),
            "planning": {"memory": {"enabled": False}},
        })
        self.assertFalse(get_planning_config(config)["memory"]["enabled"])

    def test_its_bounds_can_be_set(self):
        config = validate_config({
            "agents": list(PIPELINE), "roles": dict(ROLES),
            "planning": {"memory": {"max_goals": 3, "budget_chars": 500}},
        })
        memory = get_planning_config(config)["memory"]
        self.assertEqual(memory["max_goals"], 3)
        self.assertEqual(memory["budget_chars"], 500)

    def test_nonsense_is_refused_at_load_rather_than_at_run(self):
        for bad in (
            {"memory": "yes"},
            {"memory": {"enabled": "true"}},
            {"memory": {"max_goals": -1}},
            {"memory": {"budget_chars": "lots"}},
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(ConfigValidationError):
                    validate_config({
                        "agents": list(PIPELINE), "roles": dict(ROLES), "planning": bad,
                    })


class TestObservingRealStores(unittest.TestCase):
    """`observe` reads the goal store; nothing writes a memory anywhere."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="orch_t10obs_")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_it_reads_a_goal_back_into_observations(self):
        from orchestrator.memory import observe

        goal = GoalStore.create(self.root, goal_id="20260908T000000Z-goal01")
        goal.record_goal_started(goal="do two things", project_root=self.root)
        goal.record_plan({"goal": "do two things", "tasks": [
            {"id": "a", "title": "A", "areas": ["orchestrator/config.py"]},
            {"id": "b", "title": "B", "areas": ["tests/"]},
        ]})
        goal.record_task_started("a", "run-a", "base")
        goal.record_task_finished("a", {
            "task_id": "a", "started": True, "run_id": "run-a", "status": STATUS_COMPLETED,
            "verdict": "PASS", "tokens": 1234,
        })
        goal.record_collision(["a", "b"], ["orchestrator/config.py"])

        observed = observe(self.root, None)
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0]["task_count"], 2)
        self.assertEqual(observed[0]["collisions"][0]["paths"], ["orchestrator/config.py"])
        task = observed[0]["tasks"][0]
        self.assertEqual(task["tokens"], 1234)
        # No branch to diff, so the declared areas stand in and say so.
        self.assertEqual(task["touched"], ["orchestrator/config.py"])
        self.assertEqual(task["touched_source"], "declared")

    def test_it_writes_nothing(self):
        from orchestrator.memory import observe

        goal = GoalStore.create(self.root, goal_id="20260908T000000Z-goal01")
        goal.record_goal_started(goal="do a thing", project_root=self.root)
        before = sorted(os.listdir(self.root))
        observe(self.root, None)
        project_memory(self.root, None)
        self.assertEqual(sorted(os.listdir(self.root)), before)

    def test_an_empty_project_observes_nothing(self):
        from orchestrator.memory import observe

        self.assertEqual(observe(self.root, None), [])


if __name__ == "__main__":
    unittest.main()
