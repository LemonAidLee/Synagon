"""Tests for outward delivery and the design surface.

Covers:
  Phase 6  Outward integration, on the human's word - orchestrator.delivery, the delivery
           states in orchestrator.status, and how a delivered card moves on the board
  Phase 7  The design surface - orchestrator.teams, and the one route in the project that
           writes

Two things here deliberately use the real thing rather than a mock, because a mock could not
prove what they exist to prove: the push tests run real git against a real bare repository on
disk, and the design-surface tests start the real server and make real HTTP requests -
including the POST that must be refused on a read-only one.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

import orchestrator.delivery as delivery_module
from orchestrator.board import (
    COLUMN_IN_REVIEW,
    COLUMN_MERGED,
    COLUMN_NEEDS_YOU,
    COLUMN_READY,
    build_board,
    column_for,
    run_card,
)
from orchestrator.config import (
    ConfigValidationError,
    get_delivery_config,
    validate_config,
)
from orchestrator.delivery import (
    deliver,
    delivery_key,
    deliveries_by_key,
    format_deliveries,
    format_delivery,
    list_deliveries,
    load_delivery,
    preflight_delivery,
    read_pull_request,
    refresh_delivery,
    save_delivery,
)
from orchestrator.serve import serve_board
from orchestrator.status import (
    CHECKS_FAILING,
    CHECKS_NONE,
    CHECKS_PASSING,
    CHECKS_PENDING,
    DELIVERY_CHANGES_REQUESTED,
    DELIVERY_CHECKS_FAILED,
    DELIVERY_CHECKS_RUNNING,
    DELIVERY_CLOSED,
    DELIVERY_FAILED,
    DELIVERY_LOCAL,
    DELIVERY_MERGED,
    DELIVERY_PR_OPEN,
    DELIVERY_PUSHED,
    DELIVERY_READY,
    STATUS_COMPLETED,
    TASK_DONE,
    derive_delivery_state,
    describe_delivery_state,
    summarize_checks,
)
from orchestrator.store import RunStore
from orchestrator.teams import (
    TEAM_TEMPLATES,
    apply_team_to_text,
    catalog_from_config,
    check_team_text,
    format_team,
    get_template,
    list_templates,
    normalize_team,
    phases_of,
    render_agents_block,
    replace_nested_scalar,
    replace_top_level_block,
    team_from_config,
    validate_team,
    write_team,
)

ROLES = {
    r: {"responsibility": r}
    for r in ("decomposer", "researcher", "planner", "implementer", "verifier")
}

PIPELINE = [
    {"agent": "opencode", "model": "opencode/gpt-5.1-codex", "role": "implementer"},
    {"agent": "claude", "model": "sonnet", "role": "verifier"},
]

MINIMAL_YAML = """\
# The team
# ------------------------------------------------------------------
agents:
  - agent: opencode
    model: opencode/gpt-5.1-codex
    role: implementer

  - agent: claude
    model: sonnet
    role: verifier

# How hard it tries
max_repair_attempts: 2

# Who decides
verification:
  consensus: unanimous   # a trailing comment that must survive

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


def _pr(**fields):
    base = {"number": 7, "url": "https://forge/pr/7", "state": "OPEN", "checks": CHECKS_NONE}
    base.update(fields)
    return base


class _GitRepo:
    """A real repository, with a real bare remote to push into."""

    def __init__(self, prefix="orch_t7_", with_remote=True):
        self.path = tempfile.mkdtemp(prefix=prefix)
        self.remote = None
        for args in (["init", "-q"], ["config", "user.email", "t@e.com"],
                     ["config", "user.name", "T"]):
            self.git(*args)
        with open(os.path.join(self.path, "seed.txt"), "w", encoding="utf-8") as handle:
            handle.write("seed\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "seed")

        if with_remote:
            self.remote = tempfile.mkdtemp(prefix=prefix + "remote_")
            subprocess.run(["git", "init", "-q", "--bare", self.remote],
                           capture_output=True, stdin=subprocess.DEVNULL)
            self.git("remote", "add", "origin", self.remote)

    def git(self, *args):
        return subprocess.run(
            ["git"] + list(args), cwd=self.path, capture_output=True, stdin=subprocess.DEVNULL
        )

    def cleanup(self):
        shutil.rmtree(self.path, ignore_errors=True)
        if self.remote:
            shutil.rmtree(self.remote, ignore_errors=True)


def _git_available():
    return shutil.which("git") is not None


# ===========================================================================
# Phase 6 - what a delivery record means
# ===========================================================================


class TestDeliveryStates(unittest.TestCase):
    """A delivery state is derived from recorded facts, never written down."""

    def test_no_record_is_local_not_failed(self):
        # The distinction matters: work nobody pushed is not work whose push failed.
        self.assertEqual(derive_delivery_state(None), DELIVERY_LOCAL)
        self.assertEqual(derive_delivery_state({}), DELIVERY_LOCAL)

    def test_a_pushed_branch_without_a_pull_request(self):
        self.assertEqual(derive_delivery_state({"pushed": True}), DELIVERY_PUSHED)

    def test_an_attempt_that_did_not_work(self):
        self.assertEqual(derive_delivery_state({"error": "no remote"}), DELIVERY_FAILED)

    def test_a_failed_check_outranks_an_open_pull_request(self):
        record = {"pushed": True, "pr": _pr(checks=CHECKS_FAILING)}
        self.assertEqual(derive_delivery_state(record), DELIVERY_CHECKS_FAILED)

    def test_a_change_request_outranks_a_green_build(self):
        record = {"pushed": True, "pr": _pr(checks=CHECKS_PASSING,
                                            review_decision="CHANGES_REQUESTED")}
        self.assertEqual(derive_delivery_state(record), DELIVERY_CHANGES_REQUESTED)

    def test_checks_still_running(self):
        record = {"pushed": True, "pr": _pr(checks=CHECKS_PENDING)}
        self.assertEqual(derive_delivery_state(record), DELIVERY_CHECKS_RUNNING)

    def test_green_and_unopposed_is_ready(self):
        record = {"pushed": True, "pr": _pr(checks=CHECKS_PASSING)}
        self.assertEqual(derive_delivery_state(record), DELIVERY_READY)

    def test_a_review_that_asked_for_nothing_yet_is_just_open(self):
        record = {"pushed": True, "pr": _pr(checks=CHECKS_NONE,
                                            review_decision="REVIEW_REQUIRED")}
        self.assertEqual(derive_delivery_state(record), DELIVERY_PR_OPEN)

    def test_an_approval_with_no_checks_is_ready(self):
        record = {"pushed": True, "pr": _pr(checks=CHECKS_NONE, review_decision="APPROVED")}
        self.assertEqual(derive_delivery_state(record), DELIVERY_READY)

    def test_merged_and_closed_are_terminal(self):
        self.assertEqual(
            derive_delivery_state({"pr": _pr(state="MERGED", checks=CHECKS_FAILING)}),
            DELIVERY_MERGED,
        )
        self.assertEqual(derive_delivery_state({"pr": _pr(state="CLOSED")}), DELIVERY_CLOSED)

    def test_every_state_explains_itself(self):
        for state in (DELIVERY_LOCAL, DELIVERY_PUSHED, DELIVERY_PR_OPEN, DELIVERY_MERGED,
                      DELIVERY_CHECKS_FAILED, DELIVERY_CHANGES_REQUESTED, DELIVERY_READY,
                      DELIVERY_CLOSED, DELIVERY_FAILED, DELIVERY_CHECKS_RUNNING):
            self.assertNotIn("Unrecognized", describe_delivery_state(state))


class TestCheckSummary(unittest.TestCase):
    """One word for a whole CI run, and an unread check is never a green one."""

    def test_nothing_configured(self):
        self.assertEqual(summarize_checks(None), CHECKS_NONE)
        self.assertEqual(summarize_checks([]), CHECKS_NONE)

    def test_all_green(self):
        rollup = [{"conclusion": "SUCCESS"}, {"state": "SUCCESS"}, {"conclusion": "SKIPPED"}]
        self.assertEqual(summarize_checks(rollup), CHECKS_PASSING)

    def test_one_failure_fails_the_lot(self):
        rollup = [{"conclusion": "SUCCESS"}, {"conclusion": "FAILURE"}]
        self.assertEqual(summarize_checks(rollup), CHECKS_FAILING)

    def test_a_failure_outranks_something_still_running(self):
        rollup = [{"status": "IN_PROGRESS"}, {"conclusion": "TIMED_OUT"}]
        self.assertEqual(summarize_checks(rollup), CHECKS_FAILING)

    def test_still_running(self):
        rollup = [{"conclusion": "SUCCESS"}, {"status": "QUEUED"}]
        self.assertEqual(summarize_checks(rollup), CHECKS_PENDING)

    def test_a_word_this_project_has_never_seen_is_pending(self):
        self.assertEqual(summarize_checks([{"conclusion": "MYSTERY"}]), CHECKS_PENDING)


class TestDeliveryStore(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="orch_t7store_")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_a_key_identifies_a_task_inside_its_goal(self):
        # A task id is only unique within its goal, so both are part of the key.
        self.assertEqual(delivery_key(goal_id="g1", task_id="t1"), "g1__t1")
        self.assertEqual(delivery_key(run_id="run-1"), "run-1")

    def test_a_key_can_never_escape_the_delivery_directory(self):
        # Task ids come from a decomposer's output, which is a model's text. A key is a
        # filename, so it may not contain a separator or resolve outside its own directory.
        key = delivery_key(goal_id="g/1", task_id="../../etc")
        self.assertNotIn("/", key)
        self.assertNotIn("\\", key)

        root = delivery_module.delivery_root(self.root)
        written = (root / f"{key}.json").resolve()
        self.assertEqual(written.parent, root.resolve())

    def test_records_round_trip(self):
        save_delivery(self.root, {"id": "g__t", "branch": "b", "created_at": "2026-01-01"})
        self.assertEqual(load_delivery(self.root, "g__t")["branch"], "b")
        self.assertEqual(len(list_deliveries(self.root)), 1)

    def test_records_resolve_by_prefix(self):
        save_delivery(self.root, {"id": "20260907-abc", "created_at": "1"})
        self.assertIsNotNone(load_delivery(self.root, "20260907"))
        self.assertIsNone(load_delivery(self.root, "nothing"))

    def test_an_empty_project_has_no_deliveries(self):
        self.assertEqual(list_deliveries(self.root), [])
        self.assertEqual(deliveries_by_key(self.root), {})

    def test_they_read_back_as_a_table_that_says_how_stale_it_is(self):
        save_delivery(self.root, {"id": "g__t", "branch": "b", "created_at": "1",
                                  "pushed": True, "refreshed_at": "2026-09-07T00:00:00Z",
                                  "pr": _pr(checks=CHECKS_PASSING)})
        rendered = format_deliveries(list_deliveries(self.root))
        self.assertIn("2026-09-07T00:00:00Z", rendered)
        self.assertIn("--refresh-deliveries", rendered)
        self.assertIn("PULL REQUEST", rendered)

    def test_an_empty_table_says_what_to_do(self):
        self.assertIn("--deliver", format_deliveries([]))
        self.assertIn("No delivery", format_delivery({}))


# ===========================================================================
# Phase 6 - never auto-push
# ===========================================================================


class TestNeverAutoPush(unittest.TestCase):
    """Invariant 13. Nothing reaches a remote unless the project opted in and a person acted."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="orch_t7push_")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_delivery_is_off_by_default(self):
        self.assertFalse(get_delivery_config(_cfg())["enabled"])
        self.assertFalse(get_delivery_config(None)["enabled"])

    def test_a_disabled_project_refuses_before_touching_the_network(self):
        cfg = get_delivery_config(_cfg())
        with patch.object(delivery_module, "_run") as never:
            reason = preflight_delivery(self.root, "orchestrator/run/x", cfg)
        never.assert_not_called()
        self.assertIn("delivery.enabled", reason)

    def test_deliver_on_a_disabled_project_records_the_refusal_and_pushes_nothing(self):
        cfg = get_delivery_config(_cfg())
        with patch.object(delivery_module, "push_branch") as push:
            record = deliver(self.root, cfg, branch="b", title="t", run_id="r1")
        push.assert_not_called()
        self.assertIn("delivery.enabled", record["refused"])
        self.assertFalse(record["pushed"])
        # The refusal is durable, so the board can say why nothing happened.
        self.assertIn("delivery.enabled", load_delivery(self.root, "r1")["refused"])

    def test_work_with_no_branch_cannot_be_delivered(self):
        cfg = dict(get_delivery_config(_cfg()), enabled=True)
        self.assertIn("no branch", preflight_delivery(self.root, "", cfg))


@unittest.skipUnless(_git_available(), "git is not installed")
class TestDeliveringForReal(unittest.TestCase):
    """Real git, a real bare remote, and a real push - a mock could not prove this works."""

    def setUp(self):
        self.repo = _GitRepo()
        self.cfg = dict(get_delivery_config(_cfg()), enabled=True,
                        directory=os.path.join(self.repo.path, ".orchestrator/delivery"))
        self.repo.git("branch", "orchestrator/run/x")

    def tearDown(self):
        self.repo.cleanup()

    def test_a_branch_that_does_not_exist_is_refused(self):
        self.assertIn("does not exist", preflight_delivery(self.repo.path, "nope", self.cfg))

    def test_a_repository_with_no_remote_is_refused(self):
        lonely = _GitRepo(with_remote=False)
        try:
            self.assertIn("no 'origin' remote",
                          preflight_delivery(lonely.path, "master", self.cfg))
        finally:
            lonely.cleanup()

    def test_the_branch_really_reaches_the_remote(self):
        with patch.object(delivery_module, "gh_available", return_value=False):
            record = deliver(self.repo.path, self.cfg, branch="orchestrator/run/x",
                             title="A change", run_id="run-1")

        self.assertTrue(record["pushed"], record.get("error"))
        listed = subprocess.run(["git", "branch", "--list"], cwd=self.repo.remote,
                                capture_output=True, stdin=subprocess.DEVNULL)
        self.assertIn("orchestrator/run/x", listed.stdout.decode())

    def test_without_gh_the_branch_is_published_and_the_pull_request_is_not(self):
        # Saying "pushed, but no PR" is the difference between "try again" and "go look for
        # the branch you already published".
        with patch.object(delivery_module, "gh_available", return_value=False):
            record = deliver(self.repo.path, self.cfg, branch="orchestrator/run/x",
                             title="A change", run_id="run-1")
        self.assertEqual(derive_delivery_state(record), DELIVERY_PUSHED)
        self.assertIn("gh", record["error"])

    def test_a_pull_request_is_opened_and_read_back(self):
        pr = _pr(checks=CHECKS_PENDING)
        with patch.object(delivery_module, "gh_available", return_value=True), \
             patch.object(delivery_module, "create_pull_request",
                          return_value={"ok": True, "pr": pr, "error": None, "existed": False}):
            record = deliver(self.repo.path, self.cfg, branch="orchestrator/run/x",
                             title="A change", goal_id="g1", task_id="t1")

        self.assertEqual(derive_delivery_state(record), DELIVERY_CHECKS_RUNNING)
        self.assertEqual(record["pr"]["url"], "https://forge/pr/7")
        self.assertEqual([h["event"] for h in record["history"]], ["pushed", "pr_opened"])

    def test_delivering_twice_reuses_the_pull_request(self):
        pr = _pr()
        with patch.object(delivery_module, "gh_available", return_value=True), \
             patch.object(delivery_module, "create_pull_request",
                          return_value={"ok": True, "pr": pr, "error": None, "existed": True}):
            deliver(self.repo.path, self.cfg, branch="orchestrator/run/x", title="A", run_id="r")
            record = deliver(self.repo.path, self.cfg, branch="orchestrator/run/x",
                             title="A", run_id="r")
        self.assertEqual([h["event"] for h in record["history"]].count("pr_opened"), 0)
        self.assertIn("pr_reused", [h["event"] for h in record["history"]])


class TestRefreshing(unittest.TestCase):
    """Reading CI back is a command that writes a fact, not a side effect of looking."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="orch_t7refresh_")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_a_refresh_records_what_the_forge_said(self):
        record = {"id": "r", "branch": "b", "pushed": True, "created_at": "1",
                  "pr": _pr(checks=CHECKS_PENDING)}
        save_delivery(self.root, record)
        answer = {"found": True, "error": None, "pr": _pr(checks=CHECKS_PASSING)}
        with patch.object(delivery_module, "read_pull_request", return_value=answer):
            refreshed = refresh_delivery(self.root, record)

        self.assertEqual(derive_delivery_state(refreshed), DELIVERY_READY)
        self.assertTrue(refreshed["refreshed_at"])
        self.assertEqual(derive_delivery_state(load_delivery(self.root, "r")), DELIVERY_READY)

    def test_a_merged_pull_request_is_never_asked_about_again(self):
        record = {"id": "r", "branch": "b", "pr": _pr(state="MERGED"), "created_at": "1"}
        with patch.object(delivery_module, "read_pull_request") as never:
            refresh_delivery(self.root, record)
        never.assert_not_called()

    def test_a_forge_that_cannot_be_reached_is_recorded_not_raised(self):
        record = {"id": "r", "branch": "b", "pushed": True, "created_at": "1"}
        save_delivery(self.root, record)
        with patch.object(delivery_module, "read_pull_request",
                          return_value={"found": False, "pr": {}, "error": "network is down"}):
            refreshed = refresh_delivery(self.root, record)
        self.assertEqual(refreshed["error"], "network is down")

    def test_reading_a_pull_request_without_gh_is_an_answer_not_a_crash(self):
        with patch.object(delivery_module, "gh_available", return_value=False):
            answer = read_pull_request(self.root, "b")
        self.assertFalse(answer["found"])
        self.assertIn("gh", answer["error"])


# ===========================================================================
# Phase 6 - how a delivered card moves
# ===========================================================================


class TestDeliveredCards(unittest.TestCase):
    """The forge's facts move a card, and the projection never reaches for them itself."""

    def test_a_merged_pull_request_leaves_the_queue(self):
        self.assertEqual(column_for(TASK_DONE, "approved", DELIVERY_MERGED), COLUMN_MERGED)

    def test_a_red_check_sends_a_cleared_card_back_to_a_person(self):
        self.assertEqual(column_for(TASK_DONE, "approved", DELIVERY_CHECKS_FAILED),
                         COLUMN_NEEDS_YOU)
        self.assertEqual(column_for(TASK_DONE, "approved", DELIVERY_CHANGES_REQUESTED),
                         COLUMN_NEEDS_YOU)

    def test_a_pull_request_in_flight_is_in_review(self):
        self.assertEqual(column_for(TASK_DONE, "approved", DELIVERY_PR_OPEN), COLUMN_IN_REVIEW)
        self.assertEqual(column_for(TASK_DONE, "approved", DELIVERY_CHECKS_RUNNING),
                         COLUMN_IN_REVIEW)

    def test_undelivered_work_stays_exactly_where_it_was(self):
        # Phase 6 must not move a single card in a project that never turned delivery on.
        self.assertEqual(column_for(TASK_DONE, "approved", DELIVERY_LOCAL), COLUMN_READY)
        self.assertEqual(column_for(TASK_DONE, "approved", DELIVERY_PUSHED), COLUMN_READY)
        self.assertEqual(column_for(TASK_DONE, "pending", DELIVERY_LOCAL), COLUMN_IN_REVIEW)

    def test_a_merged_card_outranks_a_gate_nobody_answered(self):
        self.assertEqual(column_for(TASK_DONE, "pending", DELIVERY_MERGED), COLUMN_MERGED)

    def test_a_session_card_carries_its_pull_request(self):
        run = {"run_id": "run-1", "task": "do it", "status": STATUS_COMPLETED,
               "summary": {"workspace": {"branch": "orchestrator/run/run-1"}}}
        record = {"id": "run-1", "pushed": True, "refreshed_at": "2026-09-07T00:00:00Z",
                  "pr": _pr(checks=CHECKS_PASSING)}
        card = run_card(run, deliveries={"run-1": record})

        self.assertEqual(card["column"], COLUMN_READY)
        self.assertEqual(card["pr_number"], 7)
        self.assertEqual(card["checks"], CHECKS_PASSING)
        self.assertEqual(card["delivered_as_of"], "2026-09-07T00:00:00Z")

    def test_a_goals_task_finds_its_delivery_by_the_same_key(self):
        goal = {"goal_id": "g1", "goal": "a goal",
                "plan": {"tasks": [{"id": "t1", "title": "One"}]}}
        outcomes = {"t1": {"status": STATUS_COMPLETED, "verdict": "PASS",
                           "branch": "orchestrator/run/r"}}
        deliveries = {delivery_key(goal_id="g1", task_id="t1"):
                      {"id": "g1__t1", "pr": _pr(state="MERGED")}}
        board = build_board(goals=[goal], outcomes_by_goal={"g1": outcomes},
                            deliveries=deliveries)
        self.assertEqual(board["counts"][COLUMN_MERGED], 1)

    def test_the_board_never_reaches_for_the_network(self):
        # A projection that phoned a forge would be slow, would fail offline, and would make
        # reading a board an act with consequences.
        run = {"run_id": "run-1", "task": "do it", "status": STATUS_COMPLETED, "summary": {}}
        with patch.object(delivery_module, "_run") as never:
            build_board(runs=[run], deliveries={"run-1": {"id": "run-1", "pushed": True}})
        never.assert_not_called()


class TestDeliveryConfig(unittest.TestCase):
    def test_defaults(self):
        resolved = get_delivery_config(_cfg())
        self.assertFalse(resolved["enabled"])
        self.assertEqual(resolved["remote"], "origin")
        self.assertTrue(resolved["draft"])
        self.assertTrue(resolved["on_approve"])

    def test_settings_are_read_back(self):
        config = _cfg(delivery={"enabled": True, "remote": "upstream", "base": "main",
                                "draft": False, "on_approve": False})
        resolved = get_delivery_config(config)
        self.assertTrue(resolved["enabled"])
        self.assertEqual(resolved["remote"], "upstream")
        self.assertEqual(resolved["base"], "main")
        self.assertFalse(resolved["draft"])
        self.assertFalse(resolved["on_approve"])

    def test_flags_must_be_boolean(self):
        with self.assertRaises(ConfigValidationError):
            _cfg(delivery={"enabled": "yes"})

    def test_the_remote_must_be_a_real_name(self):
        with self.assertRaises(ConfigValidationError):
            _cfg(delivery={"remote": ""})

    def test_an_empty_base_means_the_remotes_default_branch(self):
        self.assertEqual(get_delivery_config(_cfg(delivery={"base": ""}))["base"], "")


# ===========================================================================
# Phase 7 - the team model
# ===========================================================================


class TestPhases(unittest.TestCase):
    """A phase is consecutive same-role agents - graph.py's definition, not a second one."""

    def test_consecutive_same_role_agents_are_one_phase(self):
        phases = phases_of([
            {"agent": "a", "role": "verifier"},
            {"agent": "b", "role": "verifier"},
            {"agent": "c", "role": "implementer"},
        ])
        self.assertEqual([p["role"] for p in phases], ["verifier", "implementer"])
        self.assertTrue(phases[0]["parallel"])
        self.assertFalse(phases[1]["parallel"])

    def test_the_same_role_twice_apart_is_two_phases(self):
        phases = phases_of([
            {"agent": "a", "role": "verifier"},
            {"agent": "b", "role": "implementer"},
            {"agent": "c", "role": "verifier"},
        ])
        self.assertEqual(len(phases), 3)

    def test_an_empty_team_has_no_phases(self):
        self.assertEqual(phases_of([]), [])


class TestTeamFromConfig(unittest.TestCase):
    def test_it_reads_the_pipeline_the_project_is_configured_with(self):
        team = team_from_config(_cfg(verification={"consensus": "majority"},
                                     max_repair_attempts=3))
        self.assertEqual([a["role"] for a in team["agents"]], ["implementer", "verifier"])
        self.assertEqual(team["consensus"], "majority")
        self.assertEqual(team["max_repair_attempts"], 3)

    def test_the_catalog_is_what_the_editor_may_offer(self):
        catalog = catalog_from_config(_cfg())
        self.assertIn("claude", catalog["models"])
        self.assertIn("implementer", catalog["roles"])
        self.assertEqual(catalog["consensus_policies"], ["unanimous", "majority", "any"])

    def test_a_team_reads_back_as_a_pipeline(self):
        rendered = format_team(team_from_config(_cfg()))
        self.assertIn("implementer", rendered)
        self.assertIn("consensus:", rendered)


class TestTemplates(unittest.TestCase):
    """A template that would not load is not a template, it is a trap."""

    def test_every_template_produces_a_team_this_project_can_run(self):
        config = _cfg()
        for name in TEAM_TEMPLATES:
            with self.subTest(template=name):
                team = normalize_team(get_template(name))
                self.assertEqual(validate_team(team, config), [])

    def test_the_careful_team_is_a_quorum_of_verifiers(self):
        team = normalize_team(get_template("careful"))
        verifiers = [a for a in team["agents"] if a["role"] == "verifier"]
        self.assertGreaterEqual(len(verifiers), 3)
        self.assertEqual(team["consensus"], "unanimous")

    def test_the_fast_team_is_one_of_each(self):
        team = normalize_team(get_template("fast"))
        roles = [a["role"] for a in team["agents"]]
        self.assertEqual(sorted(roles), ["implementer", "planner", "researcher", "verifier"])

    def test_an_unknown_template_is_none_not_a_guess(self):
        self.assertIsNone(get_template("whatever"))

    def test_they_are_listed_with_the_shape_they_would_produce(self):
        listed = {t["name"]: t for t in list_templates()}
        self.assertIn("careful", listed)
        self.assertTrue(listed["careful"]["phases"])
        self.assertTrue(listed["careful"]["description"])


class TestNormalizingWhatTheEditorPosts(unittest.TestCase):
    """The design surface is a web page, and a page is data. Nothing here trusts it."""

    def test_junk_entries_are_dropped(self):
        team = normalize_team({"agents": ["not a dict", {"agent": "", "role": "verifier"},
                                          {"agent": "claude", "role": "verifier"}]})
        self.assertEqual(len(team["agents"]), 1)

    def test_a_ladder_of_one_is_just_a_model(self):
        team = normalize_team({"agents": [{"agent": "claude", "model": ["sonnet"],
                                           "role": "verifier"}]})
        self.assertEqual(team["agents"][0]["model"], "sonnet")

    def test_a_real_ladder_survives(self):
        team = normalize_team({"agents": [{"agent": "opencode", "role": "implementer",
                                           "model": ["a", "b"]}]})
        self.assertEqual(team["agents"][0]["model"], ["a", "b"])

    def test_an_unknown_consensus_policy_falls_back(self):
        self.assertEqual(normalize_team({"consensus": "whatever"})["consensus"], "unanimous")

    def test_a_nonsense_repair_count_falls_back(self):
        self.assertEqual(normalize_team({"max_repair_attempts": "lots"})["max_repair_attempts"], 2)
        self.assertEqual(normalize_team({"max_repair_attempts": -4})["max_repair_attempts"], 0)


class TestValidatingATeam(unittest.TestCase):
    def setUp(self):
        self.config = _cfg()

    def test_a_workable_team_has_nothing_wrong_with_it(self):
        self.assertEqual(validate_team(team_from_config(self.config), self.config), [])

    def test_a_team_with_nobody_to_write_the_code(self):
        team = normalize_team({"agents": [{"agent": "claude", "model": "sonnet",
                                           "role": "verifier"}]})
        self.assertTrue(any("implementer" in p for p in validate_team(team, self.config)))

    def test_a_team_with_nobody_to_check_the_work(self):
        team = normalize_team({"agents": [{"agent": "opencode", "role": "implementer",
                                           "model": "opencode/gpt-5.1-codex"}]})
        self.assertTrue(any("verifier" in p for p in validate_team(team, self.config)))

    def test_two_implementers_in_one_phase_is_refused_here_too(self):
        team = normalize_team({"agents": [
            {"agent": "opencode", "model": "opencode/gpt-5.1-codex", "role": "implementer"},
            {"agent": "opencode", "model": "opencode/gpt-5.1-codex", "role": "implementer"},
            {"agent": "claude", "model": "sonnet", "role": "verifier"},
        ]})
        self.assertTrue(any("parallel" in p for p in validate_team(team, self.config)))

    def test_a_model_the_catalog_never_heard_of(self):
        team = normalize_team({"agents": [
            {"agent": "claude", "model": "gpt-9-ultra", "role": "implementer"},
            {"agent": "claude", "model": "sonnet", "role": "verifier"},
        ]})
        self.assertTrue(any("gpt-9-ultra" in p for p in validate_team(team, self.config)))

    def test_a_provider_that_does_not_exist(self):
        team = normalize_team({"agents": [
            {"agent": "mystery", "role": "implementer"},
            {"agent": "claude", "model": "sonnet", "role": "verifier"},
        ]})
        self.assertTrue(any("mystery" in p for p in validate_team(team, self.config)))

    def test_an_empty_team_is_not_a_team(self):
        self.assertTrue(validate_team(normalize_team({}), self.config))


# ===========================================================================
# Phase 7 - writing the file without destroying it
# ===========================================================================


class TestSurgicalYamlWriting(unittest.TestCase):
    """orchestrator.yaml is mostly documentation. A writer that ate it would be a defect."""

    def test_a_block_is_replaced_and_the_next_banner_survives(self):
        result = replace_top_level_block(MINIMAL_YAML, "agents", "agents:\n  - agent: claude")
        self.assertIn("# How hard it tries", result)
        self.assertIn("# The team", result)
        self.assertNotIn("opencode", result)

    def test_a_missing_key_is_appended(self):
        result = replace_top_level_block("roles: {}\n", "agents", "agents:\n  - agent: claude")
        self.assertIn("agents:", result)
        self.assertIn("roles:", result)

    def test_rewriting_twice_changes_nothing_the_second_time(self):
        once = replace_top_level_block(MINIMAL_YAML, "agents", "agents:\n  - agent: claude")
        twice = replace_top_level_block(once, "agents", "agents:\n  - agent: claude")
        self.assertEqual(once, twice)

    def test_a_nested_scalar_keeps_its_indentation_and_its_comment(self):
        result = replace_nested_scalar(MINIMAL_YAML, "verification", "consensus", "majority")
        self.assertIn("  consensus: majority", result)
        self.assertIn("a trailing comment that must survive", result)

    def test_a_missing_nested_section_is_added(self):
        result = replace_nested_scalar("agents: []\n", "verification", "consensus", "any")
        self.assertIn("verification:", result)
        self.assertIn("consensus: any", result)

    def test_an_escalation_ladder_renders_as_a_list(self):
        block = render_agents_block([
            {"agent": "opencode", "model": ["a", "b"], "role": "implementer"}
        ])
        self.assertIn("    model:", block)
        self.assertIn("      - a", block)
        self.assertIn("first repair", block)

    def test_an_agent_with_no_model_omits_the_key(self):
        block = render_agents_block([{"agent": "claude", "model": None, "role": "verifier"}])
        self.assertNotIn("model:", block)

    def test_applying_a_team_produces_a_config_that_loads(self):
        team = normalize_team(get_template("careful"))
        result = apply_team_to_text(MINIMAL_YAML, team)
        ok, problem = check_team_text(result)
        self.assertTrue(ok, problem)

    def test_applying_a_team_is_idempotent(self):
        team = normalize_team(get_template("balanced"))
        once = apply_team_to_text(MINIMAL_YAML, team)
        self.assertEqual(once, apply_team_to_text(once, team))

    def test_a_team_survives_the_round_trip_through_the_file(self):
        team = normalize_team(get_template("balanced"))
        text = apply_team_to_text(MINIMAL_YAML, team)
        import yaml
        back = team_from_config(validate_config(yaml.safe_load(text)))
        self.assertEqual(back["agents"], team["agents"])
        self.assertEqual(back["consensus"], team["consensus"])
        self.assertEqual(back["max_repair_attempts"], team["max_repair_attempts"])

    def test_broken_yaml_is_caught_before_it_is_written(self):
        ok, problem = check_team_text("agents: [\n")
        self.assertFalse(ok)
        self.assertIn("YAML", problem)


class TestWritingTheTeam(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="orch_t7write_")
        self.path = os.path.join(self.root, "orchestrator.yaml")
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(MINIMAL_YAML)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _read(self):
        with open(self.path, encoding="utf-8") as handle:
            return handle.read()

    def test_a_write_leaves_a_backup_behind(self):
        result = write_team(self.root, normalize_team(get_template("careful")))
        self.assertTrue(result["ok"], result.get("error"))
        self.assertTrue(os.path.isfile(result["backup"]))
        with open(result["backup"], encoding="utf-8") as handle:
            self.assertEqual(handle.read(), MINIMAL_YAML)

    def test_the_documentation_in_the_file_survives(self):
        write_team(self.root, normalize_team(get_template("careful")))
        written = self._read()
        self.assertIn("# The team", written)
        self.assertIn("# How hard it tries", written)
        self.assertIn("a trailing comment that must survive", written)

    def test_the_team_is_actually_changed(self):
        write_team(self.root, normalize_team(get_template("careful")))
        self.assertEqual(self._read().count("role: verifier"), 3)
        self.assertIn("max_repair_attempts: 3", self._read())

    def test_writing_the_same_team_twice_is_a_no_op(self):
        team = normalize_team(get_template("careful"))
        write_team(self.root, team)
        again = write_team(self.root, team)
        self.assertTrue(again["ok"])
        self.assertTrue(again["unchanged"])
        self.assertIsNone(again["backup"])

    def test_a_configuration_that_would_not_load_is_never_written(self):
        # Two implementers is refused by validate_config, so the candidate text is rejected
        # before it can replace a working file.
        broken = {"agents": [
            {"agent": "opencode", "model": "opencode/gpt-5.1-codex", "role": "implementer"},
            {"agent": "opencode", "model": "opencode/gpt-5.1-codex", "role": "implementer"},
        ], "consensus": "unanimous", "max_repair_attempts": 1}
        result = write_team(self.root, broken)
        self.assertFalse(result["ok"])
        self.assertIn("refusing to write", result["error"])
        self.assertEqual(self._read(), MINIMAL_YAML)

    def test_a_missing_file_is_reported_not_created(self):
        empty = tempfile.mkdtemp(prefix="orch_t7empty_")
        try:
            result = write_team(empty, normalize_team(get_template("fast")))
            self.assertFalse(result["ok"])
            self.assertIn("does not exist", result["error"])
            self.assertFalse(os.path.isfile(os.path.join(empty, "orchestrator.yaml")))
        finally:
            shutil.rmtree(empty, ignore_errors=True)


# ===========================================================================
# Phase 7 - the one route that writes
# ===========================================================================


class _ServerCase(unittest.TestCase):
    design = False

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="orch_t7serve_")
        self.path = os.path.join(self.root, "orchestrator.yaml")
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(MINIMAL_YAML)

        store = RunStore.create(self.root, run_id="20260907T000000Z-aaaaaa")
        store.record_run_started(task="do the thing", project_root=self.root, config=_cfg())
        store.record_run_finished({"status": STATUS_COMPLETED, "verdict": "PASS"})

        self.server = serve_board(
            self.root, _cfg(), port=0, open_browser=False, printer=lambda *_: None,
            serve_forever=False, design=self.design,
        )
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.root, ignore_errors=True)

    def _get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=5) as response:
            return response.status, response.read()

    def _post(self, path, payload):
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())


class TestDesignSurface(_ServerCase):
    """`--design` is the only server that can write, and only to the team file."""

    design = True

    def test_the_page_is_self_contained(self):
        status, body = self._get("/design")
        text = body.decode("utf-8")
        self.assertEqual(status, 200)
        # No CDN: the editor has to work on a machine that is offline.
        self.assertNotIn("<script src", text)
        self.assertNotIn("https://cdn", text)
        self.assertNotIn("http://cdn", text)

    def test_the_office_is_still_there(self):
        status, body = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn("<canvas", body.decode("utf-8"))

    def test_the_team_route_serves_everything_the_editor_may_offer(self):
        _, body = self._get("/api/team")
        data = json.loads(body)
        self.assertTrue(data["writable"])
        self.assertIn("claude", data["catalog"]["models"])
        self.assertIn("implementer", data["catalog"]["roles"])
        self.assertTrue(data["templates"])

    def test_saving_a_team_writes_the_file(self):
        careful = normalize_team(get_template("careful"))
        status, result = self._post("/api/team", {
            "agents": careful["agents"],
            "consensus": careful["consensus"],
            "max_repair_attempts": careful["max_repair_attempts"],
        })
        self.assertEqual(status, 200)
        self.assertTrue(result["ok"], result.get("error"))
        with open(self.path, encoding="utf-8") as handle:
            written = handle.read()
        self.assertEqual(written.count("role: verifier"), 3)
        self.assertIn("# The team", written)

    def test_a_team_that_would_not_run_is_refused_before_it_lands(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post("/api/team", {"agents": [
                {"agent": "opencode", "model": "opencode/gpt-5.1-codex", "role": "implementer"},
                {"agent": "opencode", "model": "opencode/gpt-5.1-codex", "role": "implementer"},
                {"agent": "claude", "model": "sonnet", "role": "verifier"},
            ]})
        self.assertEqual(ctx.exception.code, 400)
        with open(self.path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), MINIMAL_YAML)

    def test_it_writes_the_team_file_and_nothing_else(self):
        # There is no route that can start a run, answer a gate, or push a branch.
        for route in ("/api/board", "/api/activity", "/api/goals", "/api/deliveries"):
            with self.subTest(route=route):
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    self._post(route, {})
                self.assertEqual(ctx.exception.code, 405)

    def test_a_body_that_is_not_a_team_is_refused(self):
        request = urllib.request.Request(
            self.base + "/api/team", data=b"not json", method="POST",
            headers={"Content-Type": "application/json"},
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(ctx.exception.code, 400)

    def test_an_empty_body_is_refused(self):
        request = urllib.request.Request(self.base + "/api/team", data=b"", method="POST")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(ctx.exception.code, 400)


class TestServeStaysReadOnly(_ServerCase):
    """`--serve` never acquires a write, however much Phase 7 added to the same server."""

    design = False

    def test_the_team_route_reads(self):
        _, body = self._get("/api/team")
        self.assertFalse(json.loads(body)["writable"])

    def test_but_it_refuses_to_be_written_to(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post("/api/team", {"agents": []})
        self.assertEqual(ctx.exception.code, 405)
        with open(self.path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), MINIMAL_YAML)

    def test_deliveries_are_readable(self):
        status, body = self._get("/api/deliveries")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), [])


if __name__ == "__main__":
    unittest.main()
