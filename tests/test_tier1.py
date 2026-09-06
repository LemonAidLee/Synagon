"""Tests for the Tier 1 features.

Covers:
  #4 Durable run store        - orchestrator.store
  #5 Derived status           - orchestrator.status
  #6 Preflight probing        - orchestrator.preflight
  #7 BLOCKED verdict          - orchestrator.agents.verifier + routing
"""

import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

import orchestrator.graph as graph_module
from orchestrator.agents.verifier import (
    REPAIRABLE_VERDICTS,
    VERDICT_BLOCKED,
    
    extract_human_action,
    is_repairable,
    parse_verdict,
)
from orchestrator.prompts import build_verifier_prompt
from orchestrator.config import (
    ConfigValidationError,
    get_preflight_config,
    get_run_store_config,
    load_config,
    validate_config,
)
from orchestrator.graph import graph, should_repair_or_end
from orchestrator.preflight import (
    format_preflight_report,
    probe_agent,
    run_preflight,
    skipped_report,
)
from orchestrator.status import (
    STATUS_BLOCKED,
    STATUS_COMPLETED,
    STATUS_ERROR,
    STATUS_FAILED,
    STATUS_NEEDS_REPAIR,
    STATUS_PENDING,
    STATUS_PREFLIGHT_FAILED,
    derive_status,
    derive_summary,
    describe_status,
    has_error,
    is_successful,
    is_terminal,
)
from orchestrator.store import (
    RunStore,
    generate_run_id,
    list_runs,
    load_run,
    open_run,
    resolve_run_id,
)


BASE_CONFIG = {
    "agents": [
        {"agent": "antigravity", "model": "gemini-3.8-flash-high", "role": "researcher"},
        {"agent": "claude", "model": "sonnet", "role": "planner"},
        {"agent": "opencode", "model": "opencode/gpt-5.1-codex", "role": "implementer"},
        {"agent": "claude", "model": "sonnet", "role": "verifier"},
    ],
    "roles": {
        "researcher": {"responsibility": "Investigate."},
        "planner": {"responsibility": "Plan."},
        "implementer": {"responsibility": "Implement."},
        "verifier": {"responsibility": "Verify."},
    },
}


# ===========================================================================
# Tier 1 #5 - Derived status
# ===========================================================================


class TestDerivedStatus(unittest.TestCase):
    """Status must be a pure function of durable facts."""

    def test_empty_state_is_pending(self):
        self.assertEqual(derive_status({}), STATUS_PENDING)
        self.assertEqual(derive_status(None), STATUS_PENDING)

    def test_pass_verdict_derives_completed(self):
        state = {
            "verification_history": [{"attempt": 1, "verdict": "PASS"}],
            "repair_attempts": 0,
            "max_repair_attempts": 2,
        }
        self.assertEqual(derive_status(state), STATUS_COMPLETED)

    def test_fail_with_budget_remaining_needs_repair(self):
        state = {
            "verification_history": [{"attempt": 1, "verdict": "FAIL"}],
            "repair_attempts": 0,
            "max_repair_attempts": 2,
        }
        self.assertEqual(derive_status(state), STATUS_NEEDS_REPAIR)

    def test_fail_with_budget_exhausted_is_failed(self):
        state = {
            "verification_history": [{"attempt": 3, "verdict": "FAIL"}],
            "repair_attempts": 2,
            "max_repair_attempts": 2,
        }
        self.assertEqual(derive_status(state), STATUS_FAILED)

    def test_unknown_verdict_never_derives_completed(self):
        state = {
            "verification_history": [{"attempt": 1, "verdict": "UNKNOWN"}],
            "repair_attempts": 2,
            "max_repair_attempts": 2,
        }
        self.assertEqual(derive_status(state), STATUS_FAILED)

    def test_blocked_verdict_derives_blocked(self):
        state = {
            "verification_history": [{"attempt": 1, "verdict": "BLOCKED"}],
            "repair_attempts": 0,
            "max_repair_attempts": 2,
        }
        self.assertEqual(derive_status(state), STATUS_BLOCKED)

    def test_error_fact_outranks_verdict(self):
        state = {
            "verification_history": [{"attempt": 1, "verdict": "PASS"}],
            "error": "something broke",
        }
        self.assertEqual(derive_status(state), STATUS_ERROR)

    def test_phase_where_every_agent_failed_derives_error(self):
        state = {"agent_results": [{"role": "planner", "status": "error"}]}
        self.assertEqual(derive_status(state), STATUS_ERROR)

    def test_has_error_reads_the_fatal_flag_not_individual_results(self):
        """One dead ensemble member is a fact; only `error` is fatal (Tier 2 #8)."""
        survivable = {
            "agent_results": [
                {"role": "researcher", "status": "error"},
                {"role": "researcher", "status": "success"},
            ]
        }
        self.assertFalse(has_error(survivable))
        self.assertNotEqual(derive_status(survivable), STATUS_ERROR)

        fatal = dict(survivable, error="every researcher failed")
        self.assertTrue(has_error(fatal))
        self.assertEqual(derive_status(fatal), STATUS_ERROR)

    def test_failed_strict_preflight_outranks_everything(self):
        state = {
            "preflight": {"ok": False, "strict": True},
            "error": "preflight failed",
            "verification_history": [{"verdict": "PASS"}],
        }
        self.assertEqual(derive_status(state), STATUS_PREFLIGHT_FAILED)

    def test_non_strict_preflight_failure_does_not_block(self):
        state = {"preflight": {"ok": False, "strict": False}}
        self.assertNotEqual(derive_status(state), STATUS_PREFLIGHT_FAILED)

    def test_progress_derives_from_last_successful_role(self):
        state = {
            "project_root": os.getcwd(),
            "agent_results": [
                {"role": "researcher", "status": "success"},
                {"role": "planner", "status": "success"},
            ],
        }
        self.assertEqual(derive_status(state), "planned")

    def test_derive_status_is_pure(self):
        """Deriving twice must not mutate the state or change the answer."""
        state = {
            "verification_history": [{"verdict": "FAIL"}],
            "repair_attempts": 1,
            "max_repair_attempts": 2,
        }
        snapshot = json.dumps(state, sort_keys=True)
        first = derive_status(state)
        second = derive_status(state)
        self.assertEqual(first, second)
        self.assertEqual(snapshot, json.dumps(state, sort_keys=True))

    def test_terminal_and_success_classification(self):
        self.assertTrue(is_terminal(STATUS_COMPLETED))
        self.assertTrue(is_terminal(STATUS_BLOCKED))
        self.assertFalse(is_terminal(STATUS_NEEDS_REPAIR))
        self.assertTrue(is_successful(STATUS_COMPLETED))
        self.assertFalse(is_successful(STATUS_BLOCKED))
        self.assertFalse(is_successful(STATUS_FAILED))

    def test_every_status_has_a_description(self):
        for status in (
            STATUS_PENDING, STATUS_COMPLETED, STATUS_FAILED,
            STATUS_BLOCKED, STATUS_ERROR, STATUS_PREFLIGHT_FAILED,
            STATUS_NEEDS_REPAIR,
        ):
            self.assertNotEqual(describe_status(status), "Unrecognized status.")

    def test_derive_summary_reports_counts(self):
        state = {
            "agent_results": [
                {"role": "researcher", "status": "success"},
                {"role": "planner", "status": "error"},
            ],
            "verification_history": [{"verdict": "FAIL"}],
            "repair_attempts": 1,
            "max_repair_attempts": 2,
        }
        summary = derive_summary(state)
        self.assertEqual(summary["agent_executions"], 2)
        self.assertEqual(summary["errored_executions"], 1)
        # One errored execution alongside a survivor is not fatal; the verdict
        # and repair budget decide the status.
        self.assertEqual(summary["status"], STATUS_NEEDS_REPAIR)


# ===========================================================================
# Tier 1 #4 - Durable run store
# ===========================================================================


class TestRunStore(unittest.TestCase):
    """The run store must persist facts and never break a run."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="orch_store_test_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_run_ids_are_unique_and_sortable(self):
        ids = [generate_run_id() for _ in range(5)]
        self.assertEqual(len(set(ids)), 5)
        self.assertEqual(ids, sorted(ids, key=lambda s: s.split("-")[0]) or ids)

    def test_open_run_creates_directory_and_metadata(self):
        store = open_run(self.tmp, task="Do a thing", config=BASE_CONFIG)
        self.assertIsNotNone(store)
        self.assertTrue(os.path.isdir(store.run_dir))
        self.assertTrue(store.meta_path.is_file())

        with open(store.meta_path, encoding="utf-8") as handle:
            meta = json.load(handle)
        self.assertEqual(meta["task"], "Do a thing")
        self.assertEqual(meta["status"], "running")
        self.assertEqual(len(meta["agents"]), 4)

    def test_disabled_store_returns_none(self):
        self.assertIsNone(open_run(self.tmp, task="x", config=BASE_CONFIG, enabled=False))

    def test_events_are_appended_in_order(self):
        store = open_run(self.tmp, task="t", config=BASE_CONFIG)
        store.record_agent_result({"agent": "claude", "role": "planner", "status": "success", "output": "plan"})
        store.record_verification({"attempt": 1, "verdict": "PASS", "output": "ok"})
        store.record_run_finished({"status": "completed", "verdict": "PASS"})

        run = load_run(self.tmp, store.run_id)
        names = [e["event"] for e in run["events"]]
        self.assertEqual(
            names,
            ["run_started", "agent_result", "verification", "run_finished"],
        )
        self.assertEqual([e["sequence"] for e in run["events"]], [1, 2, 3, 4])

    def test_finish_updates_metadata_status(self):
        store = open_run(self.tmp, task="t", config=BASE_CONFIG)
        store.record_run_finished({"status": "blocked", "verdict": "BLOCKED"})
        entries = list_runs(self.tmp)
        self.assertEqual(entries[0]["status"], "blocked")
        self.assertEqual(entries[0]["verdict"], "BLOCKED")
        self.assertIsNotNone(entries[0]["finished_at"])

    def test_store_rebuilt_from_dir_continues_sequence(self):
        store = open_run(self.tmp, task="t", config=BASE_CONFIG)
        store.record_agent_result({"agent": "claude", "role": "planner", "status": "success"})

        reopened = RunStore.from_dir(store.run_dir)
        reopened.record_agent_result({"agent": "opencode", "role": "implementer", "status": "success"})

        run = load_run(self.tmp, store.run_id)
        self.assertEqual([e["sequence"] for e in run["events"]], [1, 2, 3])

    def test_output_truncation_respects_limit(self):
        store = open_run(self.tmp, task="t", config=BASE_CONFIG, max_output_chars=50)
        store.record_agent_result({"agent": "claude", "role": "planner", "status": "success", "output": "x" * 500})
        run = load_run(self.tmp, store.run_id)
        stored = run["events"][1]["result"]["output"]
        self.assertLess(len(stored), 500)
        self.assertIn("truncated by run store", stored)

    def test_zero_limit_means_unlimited(self):
        store = open_run(self.tmp, task="t", config=BASE_CONFIG, max_output_chars=0)
        store.record_agent_result({"agent": "claude", "role": "planner", "status": "success", "output": "y" * 500})
        run = load_run(self.tmp, store.run_id)
        self.assertEqual(len(run["events"][1]["result"]["output"]), 500)

    def test_write_failure_degrades_without_raising(self):
        """A store that cannot write must never propagate an exception."""
        store = open_run(self.tmp, task="t", config=BASE_CONFIG)
        with patch("orchestrator.store.open", side_effect=OSError("disk full")):
            store.record_agent_result({"agent": "claude", "role": "planner", "status": "success"})
        self.assertTrue(store.degraded)
        self.assertIn("disk full", store.degraded_reason)

    def test_malformed_event_lines_are_skipped(self):
        store = open_run(self.tmp, task="t", config=BASE_CONFIG)
        with open(store.events_path, "a", encoding="utf-8") as handle:
            handle.write("this is not json\n")
        store.record_run_finished({"status": "completed"})
        run = load_run(self.tmp, store.run_id)
        self.assertEqual([e["event"] for e in run["events"]], ["run_started", "run_finished"])

    def test_list_runs_is_newest_first_and_limited(self):
        for _ in range(3):
            open_run(self.tmp, task="t", config=BASE_CONFIG)
        entries = list_runs(self.tmp, limit=2)
        self.assertEqual(len(entries), 2)
        self.assertGreaterEqual(entries[0]["run_id"], entries[1]["run_id"])

    def test_list_runs_on_missing_directory_is_empty(self):
        self.assertEqual(list_runs(os.path.join(self.tmp, "nope")), [])

    def test_resolve_run_id_supports_latest_and_prefix(self):
        store = open_run(self.tmp, task="t", config=BASE_CONFIG)
        self.assertEqual(resolve_run_id(self.tmp, "latest"), store.run_id)
        self.assertEqual(resolve_run_id(self.tmp, store.run_id[:12]), store.run_id)
        self.assertIsNone(resolve_run_id(self.tmp, "does-not-exist"))

    def test_load_missing_run_returns_none(self):
        self.assertIsNone(load_run(self.tmp, "20200101T000000Z-abcdef"))


# ===========================================================================
# Tier 1 #6 - Preflight probing
# ===========================================================================


class TestPreflight(unittest.TestCase):
    """Preflight must catch an unusable environment before any agent launches."""

    def test_probe_passes_when_binary_and_model_resolve(self):
        with patch.dict(
            "orchestrator.preflight.AGENT_EXECUTABLE_RESOLVERS",
            {"claude": lambda: "C:/bin/claude.exe"},
            clear=True,
        ):
            probe = probe_agent("claude", "planner", "sonnet", config=load_config(None, os.getcwd()))
        self.assertTrue(probe["ok"])
        self.assertEqual(probe["executable"], "C:/bin/claude.exe")
        self.assertEqual(probe["errors"], [])

    def test_missing_binary_is_an_error(self):
        def boom():
            raise FileNotFoundError("Could not find 'claude' CLI executable in PATH.")

        with patch.dict(
            "orchestrator.preflight.AGENT_EXECUTABLE_RESOLVERS",
            {"claude": boom},
            clear=True,
        ):
            probe = probe_agent("claude", "planner", "sonnet", config=load_config(None, os.getcwd()))
        self.assertFalse(probe["ok"])
        self.assertTrue(any("Could not find" in e for e in probe["errors"]))

    def test_unknown_agent_is_an_error(self):
        probe = probe_agent("notanagent", "planner", None, config=load_config(None, os.getcwd()))
        self.assertFalse(probe["ok"])
        self.assertTrue(any("Unknown agent" in e for e in probe["errors"]))

    def test_model_outside_catalog_is_an_error(self):
        config = load_config(None, os.getcwd())
        with patch.dict(
            "orchestrator.preflight.AGENT_EXECUTABLE_RESOLVERS",
            {"claude": lambda: "C:/bin/claude.exe"},
            clear=True,
        ):
            probe = probe_agent("claude", "planner", "sonnettt", config=config)
        self.assertFalse(probe["ok"])
        self.assertTrue(any("not in the claude catalog" in e for e in probe["errors"]))

    def test_missing_role_responsibility_is_a_warning_not_an_error(self):
        config = dict(BASE_CONFIG)
        config["roles"] = {"planner": {"responsibility": "Plan."}}
        with patch.dict(
            "orchestrator.preflight.AGENT_EXECUTABLE_RESOLVERS",
            {"claude": lambda: "C:/bin/claude.exe"},
            clear=True,
        ):
            probe = probe_agent("claude", "nonexistent_role", None, config=config)
        self.assertTrue(probe["ok"])
        self.assertTrue(probe["warnings"])

    def test_report_aggregates_every_configured_agent(self):
        resolvers = {
            "antigravity": lambda: "C:/bin/agy.exe",
            "claude": lambda: "C:/bin/claude.exe",
            "opencode": lambda: "C:/bin/opencode.exe",
        }
        config = load_config(None, os.getcwd())
        with patch.dict("orchestrator.preflight.AGENT_EXECUTABLE_RESOLVERS", resolvers, clear=True):
            report = run_preflight(config)
        self.assertTrue(report["ok"])
        self.assertEqual(len(report["probes"]), len(config["agents"]))

    def test_report_fails_when_any_agent_fails(self):
        def boom():
            raise FileNotFoundError("no opencode here")

        resolvers = {
            "antigravity": lambda: "C:/bin/agy.exe",
            "claude": lambda: "C:/bin/claude.exe",
            "opencode": boom,
        }
        config = load_config(None, os.getcwd())
        with patch.dict("orchestrator.preflight.AGENT_EXECUTABLE_RESOLVERS", resolvers, clear=True):
            report = run_preflight(config)
        self.assertFalse(report["ok"])
        self.assertTrue(any("opencode" in e for e in report["errors"]))

    def test_empty_agent_list_fails_preflight(self):
        report = run_preflight({"agents": [], "roles": {}})
        self.assertFalse(report["ok"])

    def test_shallow_mode_runs_no_subprocess(self):
        """The default probe must not spawn processes."""
        config = load_config(None, os.getcwd())
        with patch("orchestrator.preflight.subprocess.run") as mock_run:
            with patch.dict(
                "orchestrator.preflight.AGENT_EXECUTABLE_RESOLVERS",
                {"antigravity": lambda: "a", "claude": lambda: "c", "opencode": lambda: "o"},
                clear=True,
            ):
                run_preflight(config, deep=False)
        mock_run.assert_not_called()

    def test_deep_mode_probes_each_binary_once(self):
        """Two roles share the claude binary; it must be version-probed only once."""
        config = load_config(None, os.getcwd())

        class Completed:
            returncode = 0
            stdout = b"1.2.3\n"
            stderr = b""

        with patch("orchestrator.preflight.subprocess.run", return_value=Completed()) as mock_run:
            with patch.dict(
                "orchestrator.preflight.AGENT_EXECUTABLE_RESOLVERS",
                {"antigravity": lambda: "a", "claude": lambda: "c", "opencode": lambda: "o"},
                clear=True,
            ):
                report = run_preflight(config, deep=True)
        self.assertEqual(mock_run.call_count, 3)  # 3 distinct binaries, 4 agent entries
        self.assertTrue(report["ok"])

    def test_deep_probe_uses_safe_subprocess_flags(self):
        class Completed:
            returncode = 0
            stdout = b"9.9.9"
            stderr = b""

        with patch("orchestrator.preflight.subprocess.run", return_value=Completed()) as mock_run:
            with patch.dict(
                "orchestrator.preflight.AGENT_EXECUTABLE_RESOLVERS",
                {"claude": lambda: "C:/bin/claude.exe"},
                clear=True,
            ):
                probe_agent("claude", "planner", "sonnet", config=load_config(None, os.getcwd()), deep=True)
        kwargs = mock_run.call_args.kwargs
        self.assertFalse(kwargs["shell"])
        self.assertIsNotNone(kwargs["stdin"])
        self.assertIn("timeout", kwargs)

    def test_deep_probe_timeout_is_a_warning_not_an_error(self):
        with patch("orchestrator.preflight.subprocess.run", side_effect=Exception("timed out")):
            with patch.dict(
                "orchestrator.preflight.AGENT_EXECUTABLE_RESOLVERS",
                {"claude": lambda: "C:/bin/claude.exe"},
                clear=True,
            ):
                probe = probe_agent("claude", "planner", "sonnet", config=load_config(None, os.getcwd()), deep=True)
        self.assertTrue(probe["ok"])
        self.assertTrue(probe["warnings"])

    def test_skipped_report_is_ok(self):
        report = skipped_report()
        self.assertTrue(report["ok"])
        self.assertTrue(report["skipped"])

    def test_report_formats_without_raising(self):
        config = load_config(None, os.getcwd())
        with patch.dict(
            "orchestrator.preflight.AGENT_EXECUTABLE_RESOLVERS",
            {"antigravity": lambda: "a", "claude": lambda: "c", "opencode": lambda: "o"},
            clear=True,
        ):
            report = run_preflight(config)
        text = format_preflight_report(report, color=False)
        self.assertIn("PREFLIGHT", text)
        self.assertIn("passed", text)
        self.assertIn("PREFLIGHT", format_preflight_report(skipped_report(), color=False))


# ===========================================================================
# Tier 1 #7 - BLOCKED verdict
# ===========================================================================


class TestBlockedVerdict(unittest.TestCase):
    """BLOCKED must be parsed, routed, and must not consume the repair budget."""

    def test_parse_blocked_verdict(self):
        self.assertEqual(parse_verdict("VERDICT: BLOCKED\nNeeds a key."), VERDICT_BLOCKED)
        self.assertEqual(parse_verdict("**VERDICT:** BLOCKED"), VERDICT_BLOCKED)
        self.assertEqual(parse_verdict("### VERDICT:   blocked"), VERDICT_BLOCKED)

    def test_existing_verdicts_still_parse(self):
        self.assertEqual(parse_verdict("VERDICT: PASS"), "PASS")
        self.assertEqual(parse_verdict("VERDICT: FAIL"), "FAIL")
        self.assertEqual(parse_verdict("no verdict here"), "UNKNOWN")

    def test_blocked_is_not_repairable(self):
        self.assertFalse(is_repairable(VERDICT_BLOCKED))
        self.assertTrue(is_repairable("FAIL"))
        self.assertTrue(is_repairable("UNKNOWN"))
        self.assertNotIn(VERDICT_BLOCKED, REPAIRABLE_VERDICTS)

    def test_prompt_teaches_when_to_use_blocked(self):
        prompt = build_verifier_prompt(
            task="t", project_context="c", research_text="r",
            plan_text="p", implementation_text="i", responsibility="verify",
        )
        self.assertIn("VERDICT: BLOCKED", prompt)
        self.assertIn("Human Action Required", prompt)
        self.assertIn("credentials", prompt)
        self.assertIn("If a coding agent could make progress by editing files", prompt)

    def test_extract_human_action(self):
        text = (
            "VERDICT: BLOCKED\n\n"
            "Summary:\nCannot proceed.\n\n"
            "Human Action Required:\n"
            "Provide a valid STRIPE_API_KEY in the environment.\n"
        )
        self.assertEqual(
            extract_human_action(text),
            "Provide a valid STRIPE_API_KEY in the environment.",
        )

    def test_extract_human_action_ignores_none(self):
        self.assertIsNone(extract_human_action("Human Action Required:\nNone"))
        self.assertIsNone(extract_human_action("Human Action Required:\nN/A"))
        self.assertIsNone(extract_human_action("no such section"))
        self.assertIsNone(extract_human_action(""))
        self.assertIsNone(extract_human_action(None))

    def test_extract_human_action_ignores_echoed_template_placeholder(self):
        """A model that echoes the bracketed placeholder has stated no action."""
        echoed = "VERDICT: BLOCKED\n\nHuman Action Required:\n[If BLOCKED, state exactly what the user must decide.]"
        self.assertIsNone(extract_human_action(echoed))

    def test_routing_sends_blocked_straight_to_finalize(self):
        state = {
            "verification_verdict": "BLOCKED",
            "repair_attempts": 0,
            "max_repair_attempts": 2,
        }
        self.assertEqual(should_repair_or_end(state), "finalize")

    def test_routing_still_repairs_a_fail(self):
        state = {
            "verification_verdict": "FAIL",
            "repair_attempts": 0,
            "max_repair_attempts": 2,
        }
        self.assertEqual(should_repair_or_end(state), "repair_node")

    def test_routing_finalizes_on_error(self):
        self.assertEqual(should_repair_or_end({"error": "boom"}), "finalize")


# ===========================================================================
# Integration - all four features through the real graph
# ===========================================================================


class TestTier1Integration(unittest.TestCase):
    """End-to-end behavior of the assembled graph."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="orch_tier1_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _invoke(self, claude_outputs, extra_state=None):
        state = {
            "task": "Tier 1 integration",
            "project_root": os.getcwd(),
            "run_store_enabled": False,
            "agent_results": [],
        }
        state.update(extra_state or {})
        with patch.object(graph_module, "run_antigravity", return_value="analysis"), \
             patch.object(graph_module, "run_claude_code", side_effect=claude_outputs), \
             patch.object(graph_module, "run_opencode", return_value="implementation"):
            return graph.invoke(state)

    def test_graph_contains_preflight_and_finalize_nodes(self):
        nodes = set(graph.get_graph().nodes)
        self.assertIn("preflight", nodes)
        self.assertIn("finalize", nodes)

    def test_blocked_run_consumes_no_repair_attempts(self):
        result = self._invoke(
            [
                "plan",
                "VERDICT: BLOCKED\n\nHuman Action Required:\nProvide database credentials.",
            ]
        )
        self.assertEqual(result["verification_verdict"], "BLOCKED")
        self.assertEqual(result["status"], STATUS_BLOCKED)
        self.assertEqual(result.get("repair_attempts", 0), 0)
        self.assertEqual(result["blocked_reason"], "Provide database credentials.")

    def test_failing_run_still_consumes_the_repair_budget(self):
        """Control case: FAIL must behave exactly as before BLOCKED existed."""
        result = self._invoke(
            ["plan"] + ["VERDICT: FAIL\nBroken."] * 3,
            extra_state={"max_repair_attempts": 2},
        )
        self.assertEqual(result["status"], STATUS_FAILED)
        self.assertEqual(result["repair_attempts"], 2)

    def test_finalize_writes_a_derived_status_and_summary(self):
        result = self._invoke(["plan", "VERDICT: PASS\nGood."])
        self.assertEqual(result["status"], STATUS_COMPLETED)
        summary = result["summary"]
        self.assertEqual(summary["verdict"], "PASS")
        self.assertTrue(summary["terminal"])
        self.assertTrue(summary["successful"])
        self.assertEqual(summary["agent_executions"], 4)

    def test_status_matches_independent_derivation(self):
        """The stored status must equal a fresh derivation from the same facts."""
        result = self._invoke(["plan", "VERDICT: PASS\nGood."])
        self.assertEqual(result["status"], derive_status(result))

    def test_failed_preflight_halts_before_any_agent_runs(self):
        def boom():
            raise FileNotFoundError("no opencode binary")

        resolvers = {
            "antigravity": lambda: "a",
            "claude": lambda: "c",
            "opencode": boom,
        }
        with patch.dict("orchestrator.preflight.AGENT_EXECUTABLE_RESOLVERS", resolvers, clear=True), \
             patch.object(graph_module, "run_antigravity") as agy, \
             patch.object(graph_module, "run_claude_code") as claude, \
             patch.object(graph_module, "run_opencode") as oc:
            result = graph.invoke(
                {
                    "task": "should not run",
                    "project_root": os.getcwd(),
                    "run_store_enabled": False,
                    "agent_results": [],
                }
            )

        agy.assert_not_called()
        claude.assert_not_called()
        oc.assert_not_called()
        self.assertEqual(result["status"], STATUS_PREFLIGHT_FAILED)
        self.assertFalse(result["preflight"]["ok"])
        self.assertEqual(result.get("agent_results"), [])

    def test_skip_preflight_bypasses_probing(self):
        with patch("orchestrator.preflight.run_preflight") as probe:
            result = self._invoke(
                ["plan", "VERDICT: PASS\nok."],
                extra_state={"skip_preflight": True},
            )
        probe.assert_not_called()
        self.assertTrue(result["preflight"]["skipped"])
        self.assertEqual(result["status"], STATUS_COMPLETED)

    def test_run_store_records_a_complete_run(self):
        state = {
            "task": "record me",
            "project_root": self.tmp,
            "agent_results": [],
        }
        with patch.object(graph_module, "run_antigravity", return_value="analysis"), \
             patch.object(graph_module, "run_claude_code", side_effect=["plan", "VERDICT: PASS\nok"]), \
             patch.object(graph_module, "run_opencode", return_value="impl"), \
             patch.dict(
                 "orchestrator.preflight.AGENT_EXECUTABLE_RESOLVERS",
                 {"antigravity": lambda: "a", "claude": lambda: "c", "opencode": lambda: "o"},
                 clear=True,
             ):
            result = graph.invoke(state)

        self.assertIsNotNone(result.get("run_id"))
        run = load_run(self.tmp, result["run_id"])
        self.assertIsNotNone(run)

        names = [e["event"] for e in run["events"]]
        self.assertEqual(names[0], "run_started")
        self.assertIn("preflight", names)
        self.assertIn("verification", names)
        self.assertEqual(names[-1], "run_finished")
        self.assertEqual(names.count("agent_result"), 4)

        self.assertEqual(run["status"], STATUS_COMPLETED)
        self.assertEqual(run["verdict"], "PASS")

        entries = list_runs(self.tmp)
        self.assertEqual(entries[0]["run_id"], result["run_id"])

    def test_run_store_disabled_writes_nothing(self):
        self._invoke(["plan", "VERDICT: PASS\nok."], extra_state={"project_root": self.tmp})
        self.assertEqual(list_runs(self.tmp), [])


# ===========================================================================
# Configuration
# ===========================================================================


class TestTier1Config(unittest.TestCase):
    """New config sections must validate and default sensibly."""

    def test_defaults_when_sections_absent(self):
        config = validate_config(BASE_CONFIG)
        pf = get_preflight_config(config)
        self.assertTrue(pf["enabled"])
        self.assertTrue(pf["strict"])
        self.assertFalse(pf["deep"])

        store = get_run_store_config(config)
        self.assertTrue(store["enabled"])
        self.assertEqual(store["directory"], ".orchestrator/runs")
        self.assertEqual(store["max_output_chars"], 0)

    def test_values_round_trip(self):
        raw = dict(BASE_CONFIG)
        raw["preflight"] = {"enabled": False, "strict": False, "deep": True, "timeout_seconds": 30}
        raw["run_store"] = {"enabled": False, "directory": "custom/runs", "max_output_chars": 1000}
        config = validate_config(raw)

        pf = get_preflight_config(config)
        self.assertFalse(pf["enabled"])
        self.assertTrue(pf["deep"])
        self.assertEqual(pf["timeout_seconds"], 30)

        store = get_run_store_config(config)
        self.assertFalse(store["enabled"])
        self.assertEqual(store["directory"], "custom/runs")
        self.assertEqual(store["max_output_chars"], 1000)

    def test_invalid_preflight_values_rejected(self):
        for bad in (
            {"enabled": "yes"},
            {"strict": 1},
            {"timeout_seconds": 0},
            {"timeout_seconds": -5},
        ):
            raw = dict(BASE_CONFIG)
            raw["preflight"] = bad
            with self.assertRaises(ConfigValidationError):
                validate_config(raw)

    def test_invalid_run_store_values_rejected(self):
        for bad in (
            {"enabled": "true"},
            {"directory": ""},
            {"max_output_chars": -1},
        ):
            raw = dict(BASE_CONFIG)
            raw["run_store"] = bad
            with self.assertRaises(ConfigValidationError):
                validate_config(raw)

    def test_non_mapping_sections_rejected(self):
        raw = dict(BASE_CONFIG)
        raw["preflight"] = ["nope"]
        with self.assertRaises(ConfigValidationError):
            validate_config(raw)

    def test_project_yaml_declares_both_sections(self):
        config = load_config(None, os.getcwd())
        self.assertIn("preflight", config)
        self.assertIn("run_store", config)


if __name__ == "__main__":
    unittest.main()
