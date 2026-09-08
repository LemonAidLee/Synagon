"""Tests for the board and the approval gates.

Covers:
  Phase 4  The board and the office  - orchestrator.board, orchestrator.serve
  Phase 5  Approval gates            - orchestrator.approvals, gates in the scheduler,
                                       resuming a goal, and re-verifying a BLOCKED session
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
import urllib.request
from unittest.mock import patch

import orchestrator.graph as graph_module
from orchestrator.approvals import (
    GATE_AFTER_DECOMPOSITION,
    GATE_BEFORE_MERGE,
    GATE_BEFORE_TASK,
    STATUS_APPROVED,
    STATUS_PENDING,
    STATUS_REJECTED,
    describe_approval,
    find_approval,
    gate_state,
    list_approvals,
    load_approval,
    request_approval,
    resolve_approval,
)
from orchestrator.board import (
    COLUMN_IN_REVIEW,
    COLUMN_NEEDS_YOU,
    COLUMN_QUEUED,
    COLUMN_READY,
    COLUMN_STALLED,
    COLUMN_WORKING,
    build_board,
    column_for,
    format_board,
    goal_cards,
    run_card,
)
from orchestrator.config import ConfigValidationError, gate_enabled, get_approval_config, validate_config
from orchestrator.decompose import normalize_plan
from orchestrator.goals import GoalStore, load_goal, task_outcomes
from orchestrator.graph import build_graph
from orchestrator.resume import reconstruct_state, should_replay
from orchestrator.scheduler import format_goal_summary, run_goal
from orchestrator.serve import latest_activity, serve_board
from orchestrator.status import (
    GOAL_AWAITING_APPROVAL,
    GOAL_COMPLETED,
    STATUS_COMPLETED,
    TASK_AWAITING_APPROVAL,
    TASK_BLOCKED,
    TASK_DONE,
    TASK_FAILED,
    TASK_NEEDS_ATTENTION,
    TASK_QUEUED,
    TASK_RUNNING,
    TASK_SKIPPED_BUDGET,
)
from orchestrator.store import RunStore

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
    return normalize_plan({"tasks": list(tasks)}, goal="a goal")


def _task(tid, title=None, depends_on=None):
    return {"id": tid, "title": title or tid, "depends_on": list(depends_on or [])}


def _session(status="completed", tokens=0, **extra):
    result = {
        "status": status,
        "verification_verdict": "PASS" if status == "completed" else "FAIL",
        "run_id": f"run-{status}",
        "agent_results": [{"token_usage": {"available": True, "total_tokens": tokens}}] if tokens else [],
        "summary": {"duration_seconds": 0.1},
        "workspace": {"isolated": False},
    }
    result.update(extra)
    return result


class _GitRepo:
    def __init__(self, prefix="orch_t6_"):
        self.path = tempfile.mkdtemp(prefix=prefix)
        for args in (["init", "-q"], ["config", "user.email", "t@e.com"], ["config", "user.name", "T"]):
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
# Phase 4 - the board
# ===========================================================================


class TestColumns(unittest.TestCase):
    """A column is a function of state; nothing stores one."""

    def test_work_in_progress(self):
        self.assertEqual(column_for(TASK_QUEUED), COLUMN_QUEUED)
        self.assertEqual(column_for(TASK_RUNNING), COLUMN_WORKING)

    def test_anything_needing_a_person_lands_in_one_column(self):
        for state in (TASK_BLOCKED, TASK_NEEDS_ATTENTION, TASK_AWAITING_APPROVAL):
            self.assertEqual(column_for(state), COLUMN_NEEDS_YOU)

    def test_delivered_work_waits_only_when_a_gate_asked(self):
        self.assertEqual(column_for(TASK_DONE), COLUMN_READY)
        self.assertEqual(column_for(TASK_DONE, STATUS_APPROVED), COLUMN_READY)
        self.assertEqual(column_for(TASK_DONE, STATUS_PENDING), COLUMN_IN_REVIEW)

    def test_rejected_work_needs_a_person_again(self):
        self.assertEqual(column_for(TASK_DONE, STATUS_REJECTED), COLUMN_NEEDS_YOU)

    def test_failures_stall(self):
        self.assertEqual(column_for(TASK_FAILED), COLUMN_STALLED)
        self.assertEqual(column_for(TASK_SKIPPED_BUDGET), COLUMN_STALLED)


class TestBoardProjection(unittest.TestCase):
    def _goal(self, **extra):
        goal = {
            "goal_id": "g1",
            "goal": "build the thing",
            "plan": {"tasks": [_task("a", "Task A"), _task("b", "Task B")]},
            "started_at": "2026-09-07T00:00:00Z",
        }
        goal.update(extra)
        return goal

    def test_a_goals_tasks_become_cards(self):
        board = build_board(
            goals=[self._goal()],
            outcomes_by_goal={"g1": {"a": {"status": "completed", "branch": "br-a", "tokens": 10}}},
        )
        self.assertEqual(board["total"], 2)
        by_title = {c["title"]: c for c in board["cards"]}
        self.assertEqual(by_title["Task A"]["column"], COLUMN_READY)
        self.assertEqual(by_title["Task A"]["branch"], "br-a")
        self.assertEqual(by_title["Task B"]["column"], COLUMN_QUEUED)

    def test_a_pending_merge_gate_puts_delivered_work_in_review(self):
        approvals = [
            {
                "gate": GATE_BEFORE_MERGE,
                "goal_id": "g1",
                "task_id": "a",
                "status": STATUS_PENDING,
            }
        ]
        cards = goal_cards(
            self._goal(), {"a": {"status": "completed", "branch": "br-a"}}, approvals=approvals
        )
        card = [c for c in cards if c["task_id"] == "a"][0]
        self.assertEqual(card["column"], COLUMN_IN_REVIEW)
        self.assertEqual(card["merge_gate"], STATUS_PENDING)

    def test_a_standalone_session_is_a_card(self):
        card = run_card({"run_id": "r1", "task": "do a thing", "status": "completed"})
        self.assertEqual(card["column"], COLUMN_READY)
        self.assertEqual(card["title"], "do a thing")

    def test_a_running_session_is_working(self):
        self.assertEqual(run_card({"run_id": "r", "status": "running"})["column"], COLUMN_WORKING)
        self.assertEqual(run_card({"run_id": "r", "status": "implemented"})["column"], COLUMN_WORKING)

    def test_a_blocked_session_needs_a_person(self):
        card = run_card({"run_id": "r", "status": "blocked", "summary": {"blocked_reason": "a token"}})
        self.assertEqual(card["column"], COLUMN_NEEDS_YOU)
        self.assertEqual(card["detail"], "a token")

    def test_a_goals_sessions_do_not_appear_twice(self):
        board = build_board(
            goals=[self._goal()],
            outcomes_by_goal={"g1": {}},
            runs=[
                {"run_id": "r1", "task": "Task A", "status": "completed", "goal_id": "g1", "task_id": "a"},
                {"run_id": "r2", "task": "a lone run", "status": "completed"},
            ],
        )
        titles = sorted(c["title"] for c in board["cards"])
        self.assertEqual(titles, ["Task A", "Task B", "a lone run"])

    def test_counts_and_needs_you_are_reported(self):
        board = build_board(
            goals=[self._goal()],
            outcomes_by_goal={"g1": {"a": {"status": "blocked"}}},
        )
        self.assertEqual(board["counts"][COLUMN_NEEDS_YOU], 1)
        self.assertEqual(board["needs_you"], 1)

    def test_an_empty_board_says_what_to_do(self):
        self.assertIn("empty", format_board(build_board()))

    def test_the_board_renders(self):
        board = build_board(
            goals=[self._goal()], outcomes_by_goal={"g1": {"a": {"status": "completed", "branch": "br"}}}
        )
        text = format_board(board)
        self.assertIn("READY TO MERGE", text)
        self.assertIn("Task A", text)


class TestOfficeServer(unittest.TestCase):
    """The office is a reader: every route is a GET that projects stored facts."""

    def setUp(self):
        self.repo = _GitRepo(prefix="orch_t6s_")
        self.config = _cfg()

        # One recorded session, so there is something to serve.
        store = RunStore.create(self.repo.path, run_id="20260907T000000Z-aaaaaa")
        store.record_run_started(
            task="do the thing", project_root=self.repo.path, config=self.config
        )
        store.record_agent_started("opencode", "implementer", "m")
        store.record_agent_result(
            {"agent": "opencode", "role": "implementer", "status": "success",
             "token_usage": {"available": True, "total_tokens": 42}}
        )
        store.record_run_finished({"status": STATUS_COMPLETED, "verdict": "PASS"})

        self.server = serve_board(
            self.repo.path, self.config, port=0, open_browser=False,
            printer=lambda *_: None, serve_forever=False,
        )
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.repo.cleanup()

    def _get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=5) as response:
            return response.status, response.read()

    def test_it_serves_the_office_page(self):
        status, body = self._get("/")
        self.assertEqual(status, 200)
        text = body.decode("utf-8")
        self.assertIn("<canvas", text)
        # No CDN: the office has to work on a machine that is offline.
        self.assertNotIn("http://cdn", text)
        self.assertNotIn("https://cdn", text)
        self.assertNotIn("<script src", text)

    def test_it_serves_the_board_as_json(self):
        status, body = self._get("/api/board")
        self.assertEqual(status, 200)
        board = json.loads(body)
        self.assertEqual(board["total"], 1)
        self.assertIn(COLUMN_READY, board["counts"])

    def test_activity_carries_the_events_and_the_team(self):
        status, body = self._get("/api/activity")
        self.assertEqual(status, 200)
        activity = json.loads(body)
        self.assertEqual(activity["task"], "do the thing")
        names = [e["event"] for e in activity["events"]]
        self.assertIn("agent_started", names)
        self.assertIn("agent_result", names)
        self.assertEqual([a["role"] for a in activity["agents"]], ["implementer", "verifier"])

    def test_unknown_routes_are_not_found(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get("/api/secrets")
        self.assertEqual(ctx.exception.code, 404)

    def test_it_refuses_to_be_written_to(self):
        request = urllib.request.Request(self.base + "/api/board", data=b"{}", method="POST")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(request, timeout=5)
        self.assertIn(ctx.exception.code, (405, 501))

    def test_it_binds_loopback_only(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")

    def test_activity_of_an_empty_project(self):
        empty = tempfile.mkdtemp(prefix="orch_t6e_")
        try:
            activity = latest_activity(empty, self.config)
            self.assertEqual(activity["events"], [])
            self.assertIsNone(activity["run_id"])
        finally:
            shutil.rmtree(empty, ignore_errors=True)


# ===========================================================================
# Phase 5 - approvals
# ===========================================================================


class TestApprovalStore(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="orch_t6a_")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_a_gate_starts_pending(self):
        record = request_approval(self.root, GATE_BEFORE_MERGE, "task a")
        self.assertEqual(record["status"], STATUS_PENDING)
        self.assertIsNone(record["decided_at"])
        self.assertEqual(len(list_approvals(self.root, status=STATUS_PENDING)), 1)

    def test_auto_approve_answers_the_gate_and_says_so(self):
        record = request_approval(self.root, GATE_BEFORE_TASK, "task a", auto_approve=True)
        self.assertEqual(record["status"], STATUS_APPROVED)
        self.assertEqual(record["decided_by"], "auto_approve")
        self.assertIn("auto_approve", record["note"])

    def test_approving_records_who_and_why(self):
        created = request_approval(self.root, GATE_BEFORE_MERGE, "task a")
        decided = resolve_approval(self.root, created["id"], STATUS_APPROVED, note="looks good")
        self.assertEqual(decided["status"], STATUS_APPROVED)
        self.assertEqual(decided["note"], "looks good")
        self.assertTrue(decided["decided_at"])
        self.assertEqual(load_approval(self.root, created["id"])["status"], STATUS_APPROVED)

    def test_rejecting_is_recorded_too(self):
        created = request_approval(self.root, GATE_AFTER_DECOMPOSITION, "the plan")
        decided = resolve_approval(self.root, created["id"], STATUS_REJECTED, note="too broad")
        self.assertEqual(decided["status"], STATUS_REJECTED)

    def test_a_decision_is_never_silently_overwritten(self):
        created = request_approval(self.root, GATE_BEFORE_MERGE, "task a")
        resolve_approval(self.root, created["id"], STATUS_APPROVED)
        again = resolve_approval(self.root, created["id"], STATUS_REJECTED)
        self.assertTrue(again["already_decided"])
        self.assertEqual(load_approval(self.root, created["id"])["status"], STATUS_APPROVED)

    def test_ids_resolve_by_prefix(self):
        created = request_approval(self.root, GATE_BEFORE_MERGE, "task a")
        self.assertEqual(load_approval(self.root, created["id"][:12])["id"], created["id"])
        self.assertIsNone(load_approval(self.root, "nope"))

    def test_listing_filters_by_status_and_goal(self):
        request_approval(self.root, GATE_BEFORE_TASK, "a", goal_id="g1")
        second = request_approval(self.root, GATE_BEFORE_TASK, "b", goal_id="g2")
        resolve_approval(self.root, second["id"], STATUS_APPROVED)

        self.assertEqual(len(list_approvals(self.root, status=STATUS_PENDING)), 1)
        self.assertEqual(len(list_approvals(self.root, goal_id="g2")), 1)
        self.assertEqual(len(list_approvals(self.root)), 2)

    def test_silence_is_not_consent(self):
        self.assertEqual(gate_state(None), STATUS_PENDING)
        self.assertEqual(gate_state({}), STATUS_PENDING)
        self.assertEqual(gate_state({"status": "nonsense"}), STATUS_PENDING)

    def test_find_matches_the_gate_and_its_subject(self):
        records = [
            {"gate": GATE_BEFORE_TASK, "goal_id": "g1", "task_id": "a", "status": STATUS_PENDING},
            {"gate": GATE_BEFORE_TASK, "goal_id": "g1", "task_id": "b", "status": STATUS_APPROVED},
        ]
        self.assertEqual(find_approval(records, GATE_BEFORE_TASK, "g1", "b")["status"], STATUS_APPROVED)
        self.assertIsNone(find_approval(records, GATE_BEFORE_MERGE, "g1", "a"))

    def test_it_reads_back_as_one_line(self):
        record = request_approval(self.root, GATE_BEFORE_MERGE, "task a", goal_id="g1", task_id="a")
        self.assertIn("before_merge", describe_approval(record))
        self.assertIn("pending", describe_approval(record))


class TestApprovalConfig(unittest.TestCase):
    def test_every_gate_is_off_by_default(self):
        resolved = get_approval_config(None)
        self.assertEqual(set(resolved["gates"].values()), {False})
        self.assertFalse(resolved["auto_approve"])

    def test_gates_are_read_back(self):
        config = _cfg(approval={"gates": {"before_merge": True}, "auto_approve": True})
        self.assertTrue(gate_enabled(config, GATE_BEFORE_MERGE))
        self.assertFalse(gate_enabled(config, GATE_BEFORE_TASK))
        self.assertTrue(get_approval_config(config)["auto_approve"])

    def test_an_unknown_gate_is_rejected(self):
        with self.assertRaises(ConfigValidationError):
            _cfg(approval={"gates": {"before_lunch": True}})

    def test_gate_values_must_be_boolean(self):
        with self.assertRaises(ConfigValidationError):
            _cfg(approval={"gates": {"before_merge": "yes"}})
        with self.assertRaises(ConfigValidationError):
            _cfg(approval={"auto_approve": "yes"})


class TestGatesInTheScheduler(unittest.TestCase):
    def setUp(self):
        self.repo = _GitRepo(prefix="orch_t6g_")

    def tearDown(self):
        self.repo.cleanup()

    def _run(self, config, plan=None, results=None):
        ran = []

        def runner(state):
            ran.append(state["task_id"])
            return (results or {}).get(state["task_id"], _session())

        summary = run_goal(
            goal="a goal",
            plan=plan or _plan(_task("a"), _task("b")),
            project_root=self.repo.path,
            config=config,
            session_runner=runner,
        )
        return summary, ran

    def test_no_gates_means_no_change_in_behaviour(self):
        summary, ran = self._run(_cfg())
        self.assertEqual(summary["status"], GOAL_COMPLETED)
        self.assertEqual(sorted(ran), ["a", "b"])
        self.assertEqual(list_approvals(self.repo.path), [])

    def test_the_plan_gate_stops_everything_and_asks(self):
        config = _cfg(approval={"gates": {GATE_AFTER_DECOMPOSITION: True}})
        summary, ran = self._run(config)

        self.assertEqual(ran, [])
        self.assertEqual(summary["status"], GOAL_AWAITING_APPROVAL)
        for row in summary["tasks"]:
            self.assertEqual(row["state"], TASK_AWAITING_APPROVAL)

        pending = list_approvals(self.repo.path, status=STATUS_PENDING)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["gate"], GATE_AFTER_DECOMPOSITION)
        # The summary carries the gate, and the report says how to answer it.
        self.assertEqual(summary["awaiting_approval"]["id"], pending[0]["id"])
        report = format_goal_summary(summary)
        self.assertIn("WAITING FOR YOU", report)
        self.assertIn(f"--approve {pending[0]['id']}", report)

    def test_auto_approve_answers_the_plan_gate(self):
        config = _cfg(approval={"gates": {GATE_AFTER_DECOMPOSITION: True}, "auto_approve": True})
        summary, ran = self._run(config)

        self.assertEqual(sorted(ran), ["a", "b"])
        self.assertEqual(summary["status"], GOAL_COMPLETED)
        # The gate still happened, and the record says how it was answered.
        recorded = list_approvals(self.repo.path)
        self.assertEqual(recorded[0]["decided_by"], "auto_approve")

    def test_a_task_gate_holds_only_that_task(self):
        config = _cfg(approval={"gates": {GATE_BEFORE_TASK: True}})
        summary, ran = self._run(config)

        self.assertEqual(ran, [])
        states = {r["task_id"]: r["state"] for r in summary["tasks"]}
        self.assertEqual(set(states.values()), {TASK_AWAITING_APPROVAL})
        self.assertEqual(len(list_approvals(self.repo.path, status=STATUS_PENDING)), 2)

    def test_the_merge_gate_never_blocks_delivered_work(self):
        config = _cfg(approval={"gates": {GATE_BEFORE_MERGE: True}})
        summary, ran = self._run(config)

        self.assertEqual(sorted(ran), ["a", "b"])
        self.assertEqual(summary["status"], GOAL_COMPLETED)
        pending = list_approvals(self.repo.path, status=STATUS_PENDING)
        self.assertEqual(len(pending), 2)
        self.assertEqual(pending[0]["gate"], GATE_BEFORE_MERGE)

    def test_delivered_work_with_a_pending_gate_shows_as_in_review(self):
        config = _cfg(approval={"gates": {GATE_BEFORE_MERGE: True}})
        summary, _ = self._run(config)
        goal = load_goal(self.repo.path, summary["goal_id"])
        goal["plan"] = {"tasks": [_task("a"), _task("b")]}

        board = build_board(
            goals=[goal],
            outcomes_by_goal={goal["goal_id"]: task_outcomes(goal)},
            approvals=list_approvals(self.repo.path),
        )
        self.assertEqual(board["counts"][COLUMN_IN_REVIEW], 2)
        self.assertEqual(board["counts"][COLUMN_READY], 0)

    def test_a_rejected_plan_does_not_run(self):
        config = _cfg(approval={"gates": {GATE_AFTER_DECOMPOSITION: True}})
        self._run(config)
        pending = list_approvals(self.repo.path, status=STATUS_PENDING)[0]
        resolve_approval(self.repo.path, pending["id"], STATUS_REJECTED, note="no")

        summary, ran = self._run(config)
        self.assertEqual(ran, [])
        self.assertEqual(summary["status"], GOAL_AWAITING_APPROVAL)


class TestResumingAGoal(unittest.TestCase):
    """A goal that stopped is picked up; delivered work is never redone."""

    def setUp(self):
        self.repo = _GitRepo(prefix="orch_t6r_")

    def tearDown(self):
        self.repo.cleanup()

    def test_an_approved_plan_runs_on_resume(self):
        config = _cfg(approval={"gates": {GATE_AFTER_DECOMPOSITION: True}})
        plan = _plan(_task("a"), _task("b"))

        first = run_goal(
            goal="g", plan=plan, project_root=self.repo.path, config=config,
            session_runner=lambda s: _session(),
        )
        self.assertEqual(first["status"], GOAL_AWAITING_APPROVAL)

        pending = list_approvals(self.repo.path, status=STATUS_PENDING)[0]
        resolve_approval(self.repo.path, pending["id"], STATUS_APPROVED)

        goal = load_goal(self.repo.path, first["goal_id"])
        ran = []

        def runner(state):
            ran.append(state["task_id"])
            return _session()

        second = run_goal(
            goal="g", plan=plan, project_root=self.repo.path, config=config,
            session_runner=runner,
            goal_store=GoalStore.from_dir(goal["goal_dir"]),
            resume_outcomes=task_outcomes(goal),
        )
        self.assertEqual(sorted(ran), ["a", "b"])
        self.assertEqual(second["status"], GOAL_COMPLETED)

    def test_delivered_tasks_are_replayed_not_re_run(self):
        plan = _plan(_task("a"), _task("b"))
        first = run_goal(
            goal="g", plan=plan, project_root=self.repo.path, config=_cfg(),
            session_runner=lambda s: (
                _session() if s["task_id"] == "a" else _session(status="failed")
            ),
        )
        goal = load_goal(self.repo.path, first["goal_id"])

        ran = []

        def runner(state):
            ran.append(state["task_id"])
            return _session()

        second = run_goal(
            goal="g", plan=plan, project_root=self.repo.path, config=_cfg(),
            session_runner=runner,
            goal_store=GoalStore.from_dir(goal["goal_dir"]),
            resume_outcomes=task_outcomes(goal),
        )
        self.assertEqual(ran, ["b"])
        self.assertEqual(second["replayed"], ["a"])
        self.assertEqual(second["status"], GOAL_COMPLETED)

    def test_a_resumed_dependent_task_still_sees_its_dependency(self):
        plan = _plan(_task("a"), _task("b", depends_on=["a"]))
        first = run_goal(
            goal="g", plan=plan, project_root=self.repo.path, config=_cfg(),
            session_runner=lambda s: (
                _session(workspace={"branch": "branch-a"})
                if s["task_id"] == "a"
                else _session(status="failed")
            ),
        )
        goal = load_goal(self.repo.path, first["goal_id"])
        seen = []

        def runner(state):
            seen.append(state)
            return _session()

        run_goal(
            goal="g", plan=plan, project_root=self.repo.path, config=_cfg(),
            session_runner=runner,
            goal_store=GoalStore.from_dir(goal["goal_dir"]),
            resume_outcomes=task_outcomes(goal),
        )
        self.assertEqual(seen[0]["task_id"], "b")
        self.assertEqual(seen[0]["workspace_base_ref"], "branch-a")


class TestBlockedSessionResume(unittest.TestCase):
    """Resuming a BLOCKED run re-verifies; it never replays the old verdict."""

    def test_the_verifier_is_not_replayed_after_blocked(self):
        state = {
            "resumed_from": "r1",
            "agent_results": [],
            "verification_history": [
                {"restored": True, "attempt": 1, "repair_attempts": 0, "verdict": "BLOCKED"}
            ],
        }
        self.assertFalse(should_replay(state, "verifier", repair_attempts=0))

    def test_a_failed_verification_is_still_replayed(self):
        state = {
            "resumed_from": "r1",
            "agent_results": [],
            "verification_history": [
                {"restored": True, "attempt": 1, "repair_attempts": 0, "verdict": "FAIL"}
            ],
        }
        self.assertTrue(should_replay(state, "verifier", repair_attempts=0))

    def test_resuming_a_blocked_run_asks_the_verifier_again(self):
        root = tempfile.mkdtemp(prefix="orch_t6b_")
        try:
            store = RunStore.create(root, run_id="20260907T000000Z-blocked")
            store.record_run_started(task="needs a token", project_root=root)
            for role, agent in (("researcher", "antigravity"), ("planner", "claude"),
                                ("implementer", "opencode")):
                store.record_agent_result(
                    {"agent": agent, "role": role, "status": "success", "output": role}
                )
            store.record_agent_result(
                {"agent": "claude", "role": "verifier", "status": "success",
                 "output": "VERDICT: BLOCKED", "verdict": "BLOCKED"}
            )
            store.record_verification(
                {"attempt": 1, "repair_attempts": 0, "verdict": "BLOCKED",
                 "output": "HUMAN ACTION REQUIRED: set a token"}
            )
            store.record_run_finished({"status": "blocked", "verdict": "BLOCKED"})

            from orchestrator.store import load_run

            state = reconstruct_state(load_run(root, "20260907T000000Z-blocked"), project_root=root)
            state["run_dir"] = None
            state["run_store_enabled"] = False
            state["config"] = _cfg()
            state["workspace"] = {"isolated": False, "path": root, "reason": "test"}

            calls = []

            def claude(prompt, **kwargs):
                calls.append(kwargs.get("role"))
                return "VERDICT: PASS\nthe token is there now"

            with patch.object(graph_module, "run_claude_code", side_effect=claude), \
                 patch.object(graph_module, "run_opencode") as opencode:
                result = build_graph(_cfg()).invoke(state)

            opencode.assert_not_called()          # the implementation was replayed
            self.assertEqual(calls, ["verifier"])  # the verifier was asked again
            self.assertEqual(result["status"], STATUS_COMPLETED)
        finally:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
