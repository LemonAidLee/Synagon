"""Stage 7.5.2.1 - native TUI execution refinement and reliability.

Every test here pins a defect that was found by inspection or measurement, not a
hypothetical one:

* A failed retry/escalation attempt that *reported* usage (the Antigravity "SUCCESS with an
  empty reply" failure of ARCHITECTURE.md §9.0 bills its thinking tokens) was overwritten by
  the next attempt, so totals and budget decisions were quietly lower than what was paid.
* Headless and native OpenCode normalised the same step-finish numbers differently.
* Native completion detection treated OpenCode's `retry` status as the end of the turn, and a
  turn that produced nothing was reported with a fabricated success sentence.
* Terminating the npm `opencode.CMD` shim left the real OpenCode server running (measured).
* `native_tui` was only discovered to be impossible mid-run, and was retried three times.

The payloads in `RealOpenCodePayloads` were captured from OpenCode 1.18.29 on this machine
(`opencode run --format json` and `GET /session/<id>/message`), trimmed to the fields read.
"""

import itertools
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

import orchestrator.graph as graph_module
from orchestrator import native_sessions
from orchestrator.agents import NATIVE_TUI_AGENTS, supports_native_tui
from orchestrator.agents.antigravity import run_antigravity
from orchestrator.agents.claude_code import run_claude_code
from orchestrator.agents.exceptions import (
    CLIExecutionError,
    CLITimeoutError,
    NativeTUIUnavailableError,
)
from orchestrator.agents.opencode import parse_opencode_output, resolve_opencode_execution_mode
from orchestrator.agents.opencode_tui import run_opencode_native_tui
from orchestrator.agents.opencode_usage import step_tokens_from_messages, usage_from_step_tokens
from orchestrator.budget import tokens_spent
from orchestrator.config import ConfigValidationError, get_execution_config, validate_config
from orchestrator.graph import build_graph
from orchestrator.metrics import (
    aggregate_metrics,
    format_summary_table,
    summary_rows,
    verify_token_aggregation_invariant,
)
from orchestrator.preflight import check_execution_mode, run_preflight
from orchestrator.resume import load_resumable_run, reconstruct_state
from orchestrator.store import EVENT_AGENT_RETRY, load_run
from orchestrator.tracer import default_tracer
from orchestrator.types import create_agent_result, create_token_usage

from tests.support import isolated_graph_state, redirected_run_store, temporary_store_dir

ROLES = {r: {"responsibility": r} for r in ("researcher", "planner", "implementer", "verifier")}


def _usage(total, inp=None, out=None):
    inp = total - 10 if inp is None else inp
    out = total - inp if out is None else out
    return create_token_usage(input_tokens=inp, output_tokens=out, total_tokens=total)


# ---------------------------------------------------------------------------------------------
# P0 - the 528-token discrepancy, reproduced from the primary evidence
# ---------------------------------------------------------------------------------------------
class TestTheStage752Discrepancy(unittest.TestCase):
    """The two real runs, exactly as their task logs recorded them.

    task-2521 (failed at the implementer): researcher 9,911 + 1,905 = 11,816, planner 2 + 627.
    task-2553 (completed): researcher 12,232, planner 741, implementer 36,948, verifier 3,318,
    and the orchestrator's own table printed Total 53,239. The Stage 7.5.2 report quoted the
    first two figures from task-2521 and the total from task-2553.
    """

    def _run_b(self):
        return [
            create_agent_result("antigravity", "researcher", "success", token_usage=create_token_usage(9915, 2317, 12232)),
            create_agent_result("claude", "planner", "success", token_usage=create_token_usage(2, 739, 741)),
            create_agent_result("opencode", "implementer", "success", execution_mode="native_tui", token_usage=create_token_usage(36720, 228, 36948)),
            create_agent_result("claude", "verifier", "success", verdict="PASS", token_usage=create_token_usage(24, 3294, 3318)),
        ]

    def test_the_completed_run_adds_up_to_what_it_printed(self):
        results = self._run_b()
        self.assertEqual(aggregate_metrics(results)["known_total_tokens"], 53239)
        self.assertEqual(sum(r["tokens"] for r in summary_rows(results)), 53239)
        self.assertTrue(verify_token_aggregation_invariant(results)["is_valid"])

    def test_the_528_is_exactly_the_two_substituted_rows(self):
        run_a_researcher, run_a_planner = 11816, 629
        self.assertEqual((12232 - run_a_researcher) + (741 - run_a_planner), 528)
        self.assertEqual(53239 - (run_a_researcher + run_a_planner + 36948 + 3318), 528)


# ---------------------------------------------------------------------------------------------
# A failed attempt's usage is spent, attributed to its rung, and counted exactly once
# ---------------------------------------------------------------------------------------------
class TestFailedAttemptsAreAccounted(unittest.TestCase):
    def setUp(self):
        default_tracer.clear()
        self.store_dir = temporary_store_dir()
        self.addCleanup(shutil.rmtree, self.store_dir, ignore_errors=True)

    def _config(self, researcher, attempts=3):
        raw = {
            "agents": [
                dict(researcher, role="researcher"),
                {"agent": "claude", "model": "sonnet", "role": "planner"},
                {"agent": "opencode", "model": "opencode/gpt-5.1-codex", "role": "implementer"},
                {"agent": "claude", "model": "sonnet", "role": "verifier"},
            ],
            "roles": dict(ROLES),
            "preflight": {"enabled": False},
            "execution": {"retry": {"attempts": attempts, "backoff_seconds": 0}},
        }
        return redirected_run_store(validate_config(raw), self.store_dir)

    def _run(self, config, agy_replies, claude_research=None):
        agy = list(agy_replies)
        downstream = [("plan", _usage(741)), ("VERDICT: PASS\nfine", _usage(3318))]

        def antigravity(prompt, **kwargs):
            value = agy.pop(0)
            if isinstance(value, Exception):
                raise value
            return value

        def claude(prompt, **kwargs):
            if kwargs.get("role") == "researcher":
                return claude_research
            return downstream.pop(0)

        with patch.object(graph_module, "run_antigravity", side_effect=antigravity), patch.object(
            graph_module, "run_claude_code", side_effect=claude
        ), patch.object(
            graph_module, "run_opencode", return_value=("impl", _usage(36948), "headless")
        ):
            state = isolated_graph_state(config, "accounting under test", self.store_dir)
            return build_graph(config).invoke(state)

    def test_an_escalated_phase_counts_the_failed_rung_once_and_attributes_it(self):
        config = self._config(
            {"agent": ["antigravity", "claude"], "model": ["gemini-3.8-flash-high", "sonnet"]}
        )
        # The §9.0 failure: status SUCCESS, an empty response, and a real bill for the thinking.
        final = self._run(config, [("", _usage(12232))], claude_research=("research", _usage(629)))
        results = final["agent_results"]

        researcher = [r for r in results if r["role"] == "researcher"]
        self.assertEqual(len(researcher), 1, "a retry is not an ensemble")
        self.assertEqual(researcher[0]["agent"], "claude")
        self.assertEqual(researcher[0]["token_usage"]["total_tokens"], 629)
        attempt = researcher[0]["attempt_token_usage"][0]
        self.assertEqual((attempt["agent"], attempt["model"]), ("antigravity", "gemini-3.8-flash-high"))
        self.assertEqual(attempt["token_usage"]["total_tokens"], 12232)

        expected = 12232 + 629 + 741 + 36948 + 3318
        self.assertEqual(aggregate_metrics(results)["known_total_tokens"], expected)
        self.assertEqual(tokens_spent(results), expected)
        self.assertTrue(verify_token_aggregation_invariant(results)["is_valid"])
        labels = [row["label"] for row in summary_rows(results)]
        self.assertIn("Antigravity Researcher (try 1, failed)", labels)
        self.assertIn("Claude Researcher", labels)

        # The same figure is on the retry event, where it arrived.
        run = load_run(os.getcwd(), final["run_id"], directory=self.store_dir)
        retries = [e for e in run["events"] if e.get("event") == EVENT_AGENT_RETRY]
        self.assertEqual(retries[0]["token_usage"]["total_tokens"], 12232)

    def test_an_attempt_with_unknown_usage_makes_the_total_incomplete_not_larger(self):
        config = self._config({"agent": "antigravity", "model": "gemini-3.8-flash-high"})
        final = self._run(config, [RuntimeError("boom"), ("research", _usage(12232))])
        results = final["agent_results"]
        metrics = aggregate_metrics(results)
        self.assertEqual(metrics["known_total_tokens"], 12232 + 741 + 36948 + 3318)
        self.assertEqual(metrics["failed_attempts_with_unavailable_tokens"], 1)
        self.assertFalse(metrics["all_tokens_available"])
        self.assertIn("Known tokens:", format_summary_table(results))

    def test_a_failure_that_reported_usage_keeps_it(self):
        config = self._config({"agent": "antigravity", "model": "gemini-3.8-flash-high"}, attempts=1)
        paid = CLIExecutionError("turn errored after three steps", token_usage=_usage(900))
        final = self._run(config, [paid])
        self.assertTrue(final.get("error"))
        self.assertEqual(tokens_spent(final["agent_results"]), 900)

    def test_a_first_time_success_carries_no_attempt_usage(self):
        config = self._config({"agent": "antigravity", "model": "gemini-3.8-flash-high"})
        final = self._run(config, [("research", _usage(12232))])
        researcher = [r for r in final["agent_results"] if r["role"] == "researcher"][0]
        self.assertNotIn("attempt_token_usage", researcher)


class TestRepairAndResumeDoNotDoubleCount(unittest.TestCase):
    def setUp(self):
        default_tracer.clear()
        self.store_dir = temporary_store_dir()
        self.addCleanup(shutil.rmtree, self.store_dir, ignore_errors=True)
        raw = {
            "agents": [
                {"agent": "claude", "model": "sonnet", "role": "researcher"},
                {"agent": "claude", "model": "sonnet", "role": "planner"},
                {"agent": "opencode", "model": "opencode/gpt-5.1-codex", "role": "implementer"},
                {"agent": "claude", "model": "sonnet", "role": "verifier"},
            ],
            "roles": dict(ROLES),
            "preflight": {"enabled": False},
            "max_repair_attempts": 2,
            "execution": {"retry": {"attempts": 1, "backoff_seconds": 0}},
        }
        self.config = redirected_run_store(validate_config(raw), self.store_dir)

    def test_repair_attempts_each_count_once(self):
        claude = iter([
            ("research", _usage(100)), ("plan", _usage(200)),
            ("VERDICT: FAIL\nwrong", _usage(300)), ("VERDICT: FAIL\nstill wrong", _usage(310)),
            ("VERDICT: PASS\nok", _usage(320)),
        ])
        opencode = iter([("v1", _usage(1000), "native_tui"), ("v2", _usage(1100), "native_tui"), ("v3", _usage(1200), "native_tui")])
        with patch.object(graph_module, "run_claude_code", side_effect=lambda p, **k: next(claude)), patch.object(
            graph_module, "run_opencode", side_effect=lambda p, **k: next(opencode)
        ):
            final = build_graph(self.config).invoke(
                isolated_graph_state(self.config, "repair twice", self.store_dir)
            )
        self.assertEqual(final["status"], "completed")
        self.assertEqual(final["repair_attempts"], 2)
        self.assertEqual([v["verdict"] for v in final["verification_history"]], ["FAIL", "FAIL", "PASS"])
        self.assertEqual(aggregate_metrics(final["agent_results"])["known_total_tokens"], 100 + 200 + 300 + 310 + 320 + 1000 + 1100 + 1200)
        self.assertEqual(len(final["agent_results"]), 8)

    def test_a_resumed_run_counts_restored_work_once_and_only_runs_what_is_left(self):
        calls = {"claude": 0, "opencode": 0}
        first = iter([("research", _usage(100)), ("plan", _usage(200))])

        def claude_crashing(prompt, **kwargs):
            calls["claude"] += 1
            if kwargs.get("role") == "verifier":
                raise RuntimeError("process killed")
            return next(first)

        def opencode(prompt, **kwargs):
            calls["opencode"] += 1
            return ("impl", _usage(1000), "native_tui")

        with patch.object(graph_module, "run_claude_code", side_effect=claude_crashing), patch.object(
            graph_module, "run_opencode", side_effect=opencode
        ):
            crashed = build_graph(self.config).invoke(
                isolated_graph_state(self.config, "crash then resume", self.store_dir)
            )
        self.assertTrue(crashed.get("error"))
        self.assertEqual(calls, {"claude": 3, "opencode": 1})

        run = load_resumable_run(os.getcwd(), crashed["run_id"], directory=self.store_dir)
        state = reconstruct_state(run, project_root=os.getcwd())
        state["config"] = self.config
        state["workspace"] = {"isolated": False, "path": os.getcwd(), "reason": "test"}

        calls = {"claude": 0, "opencode": 0}

        def claude_verifying(prompt, **kwargs):
            calls["claude"] += 1
            return ("VERDICT: PASS\nresumed", _usage(300))

        with patch.object(graph_module, "run_claude_code", side_effect=claude_verifying), patch.object(
            graph_module, "run_opencode", side_effect=opencode
        ):
            resumed = build_graph(self.config).invoke(state)

        self.assertEqual(resumed["status"], "completed")
        self.assertEqual(calls, {"claude": 1, "opencode": 0}, "completed phases must replay, not re-run")
        # 100 + 200 + 1000 from the first process, the crashed verifier reported nothing, 300 now.
        self.assertEqual(aggregate_metrics(resumed["agent_results"])["known_total_tokens"], 1600)
        self.assertEqual(resumed["run_id"], crashed["run_id"])
        # Appended to the original run's log: four results from the first process, one now.
        events = load_run(os.getcwd(), crashed["run_id"], directory=self.store_dir)["events"]
        self.assertEqual(sum(1 for e in events if e.get("event") == "agent_result"), 5)


# ---------------------------------------------------------------------------------------------
# One OpenCode normalisation for both modes, checked against real 1.18.29 payloads
# ---------------------------------------------------------------------------------------------
class RealOpenCodePayloads:
    HEADLESS_ONE_STEP = (
        '{"type":"step_start","part":{"type":"step-start"}}\n'
        '{"type":"text","part":{"type":"text","text":"OK"}}\n'
        '{"type":"step_finish","sessionID":"ses_f70324ae9ffeeoLYh3OmgfwmF8","part":{"type":"step-finish",'
        '"tokens":{"total":12176,"input":10367,"output":17,"reasoning":0,"cache":{"write":0,"read":1792}},"cost":0}}\n'
    )
    SESSION_ONE_STEP = {"input": 10367, "output": 17, "reasoning": 0, "cache": {"read": 1792, "write": 0}}

    THREE_STEP_MESSAGES = [
        {"info": {"role": "user"}, "parts": [{"type": "text", "text": "Create a.txt ..."}]},
        {"info": {"role": "assistant", "time": {"created": 1, "completed": 2}, "finish": "tool-calls",
                  "tokens": {"total": 12674, "input": 10788, "output": 94, "reasoning": 0, "cache": {"read": 1792, "write": 0}}},
         "parts": [{"type": "tool", "tool": "write", "state": {"title": "a.txt"}},
                   {"type": "step-finish", "tokens": {"total": 12674, "input": 10788, "output": 94, "reasoning": 0, "cache": {"read": 1792, "write": 0}}}]},
        {"info": {"role": "assistant", "time": {"created": 3, "completed": 4}, "finish": "tool-calls",
                  "tokens": {"total": 12735, "input": 146, "output": 45, "reasoning": 0, "cache": {"read": 12544, "write": 0}}},
         "parts": [{"type": "tool", "tool": "read", "state": {"title": "a.txt"}},
                   {"type": "step-finish", "tokens": {"total": 12735, "input": 146, "output": 45, "reasoning": 0, "cache": {"read": 12544, "write": 0}}}]},
        {"info": {"role": "assistant", "time": {"created": 5, "completed": 6}, "finish": "stop",
                  "tokens": {"total": 12822, "input": 249, "output": 29, "reasoning": 0, "cache": {"read": 12544, "write": 0}}},
         "parts": [{"type": "text", "text": "DONE"},
                   {"type": "step-finish", "tokens": {"total": 12822, "input": 249, "output": 29, "reasoning": 0, "cache": {"read": 12544, "write": 0}}}]},
    ]
    THREE_STEP_SESSION = {"input": 11183, "output": 168, "reasoning": 0, "cache": {"read": 26880, "write": 0}}


class TestOneNormalisationForBothModes(unittest.TestCase):
    def test_headless_and_native_read_the_same_session_as_the_same_measurement(self):
        _text, headless = parse_opencode_output(RealOpenCodePayloads.HEADLESS_ONE_STEP)
        native = usage_from_step_tokens([RealOpenCodePayloads.SESSION_ONE_STEP])
        for key in ("input_tokens", "output_tokens", "total_tokens", "cache_read_tokens"):
            self.assertEqual(headless[key], native[key], key)
        self.assertEqual(headless["total_tokens"], 12176)
        self.assertEqual(headless["input_tokens"], 10367 + 1792)

    def test_a_multi_step_turn_sums_its_steps_and_matches_the_session_aggregate(self):
        steps = step_tokens_from_messages(RealOpenCodePayloads.THREE_STEP_MESSAGES)
        self.assertEqual(len(steps), 3)
        from_steps = usage_from_step_tokens(steps)
        from_aggregate = usage_from_step_tokens([RealOpenCodePayloads.THREE_STEP_SESSION])
        self.assertEqual(from_steps["total_tokens"], 38231)
        self.assertEqual(from_aggregate["total_tokens"], 38231)
        self.assertEqual(from_steps["input_tokens"], from_aggregate["input_tokens"])

    def test_a_missing_total_is_the_sum_of_every_reported_component(self):
        usage = usage_from_step_tokens([{"input": 100, "output": 20, "reasoning": 30, "cache": {"read": 400, "write": 50}}])
        self.assertEqual(usage["total_tokens"], 600)
        self.assertEqual(usage["input_tokens"], 550)
        self.assertEqual(usage["reasoning_tokens"], 30)
        self.assertEqual(usage["cache_write_tokens"], 50)

    def test_nothing_reported_stays_unavailable(self):
        self.assertFalse(usage_from_step_tokens([])["available"])
        self.assertFalse(usage_from_step_tokens([{"cost": 0}, "junk"])["available"])
        _t, usage = parse_opencode_output("plain text, no telemetry")
        self.assertFalse(usage["available"])


# ---------------------------------------------------------------------------------------------
# Execution modes: deterministic, strict, never faked
# ---------------------------------------------------------------------------------------------
class TestExecutionModeResolution(unittest.TestCase):
    @patch("orchestrator.agents.opencode.check_antigravity_bridge", return_value=True)
    @patch("orchestrator.agents.opencode.get_antigravity_bridge_url", return_value="http://127.0.0.1:49182")
    def test_the_decision_table(self, _url, _check):
        r = resolve_opencode_execution_mode
        self.assertEqual(r("native_tui"), "native_tui")
        self.assertEqual(r("native_tui", visible=False, terminal_type="console"), "native_tui")
        self.assertEqual(r("headless", visible=True, recorder_installed=True), "headless")
        self.assertEqual(r("auto", recorder_installed=True), "native_tui")
        self.assertEqual(r("auto", visible=True, terminal_type="antigravity_integrated"), "native_tui")
        self.assertEqual(r("auto", visible=True, terminal_type="auto"), "native_tui")
        self.assertEqual(r("auto", visible=False, terminal_type="antigravity_integrated"), "headless")
        self.assertEqual(r("auto", visible=True, terminal_type="windows_terminal"), "headless")
        self.assertEqual(r(None, visible=True), "native_tui")

    @patch("orchestrator.agents.opencode.check_antigravity_bridge", return_value=False)
    def test_auto_without_a_live_bridge_is_headless(self, _check):
        self.assertEqual(resolve_opencode_execution_mode("auto", visible=True), "headless")

    def test_an_unknown_mode_is_refused_rather_than_run_headless(self):
        with self.assertRaises(CLIExecutionError):
            resolve_opencode_execution_mode("native")

    def test_only_opencode_declares_native_tui(self):
        self.assertEqual(NATIVE_TUI_AGENTS, frozenset({"opencode"}))
        self.assertTrue(supports_native_tui("OpenCode"))
        self.assertFalse(supports_native_tui("claude"))
        self.assertFalse(supports_native_tui("antigravity"))


class TestNativeTuiNeverDowngrades(unittest.TestCase):
    @patch("orchestrator.agents.opencode_tui.subprocess.Popen")
    @patch("orchestrator.agents.opencode_tui.check_antigravity_bridge", return_value=False)
    def test_no_surface_refuses_before_spawning_anything(self, _check, popen):
        with self.assertRaises(NativeTUIUnavailableError):
            run_opencode_native_tui("task", project_root=tempfile.gettempdir())
        popen.assert_not_called()

    @patch("orchestrator.agents.claude_code.run_agent_cli")
    def test_claude_is_refused_not_run(self, cli):
        with self.assertRaises(NativeTUIUnavailableError):
            run_claude_code("task", agent_execution_mode="native_tui")
        cli.assert_not_called()

    @patch("orchestrator.agents.antigravity.run_agent_cli")
    def test_antigravity_is_refused_not_run(self, cli):
        with self.assertRaises(NativeTUIUnavailableError):
            run_antigravity("task", agent_execution_mode="native_tui")
        cli.assert_not_called()

    def test_the_graph_does_not_retry_a_refusal(self):
        default_tracer.clear()
        store_dir = temporary_store_dir()
        self.addCleanup(shutil.rmtree, store_dir, ignore_errors=True)
        raw = {
            "agents": [{"agent": "claude", "model": "sonnet", "role": "researcher"}],
            "roles": dict(ROLES),
            "preflight": {"enabled": False},
            "execution": {"retry": {"attempts": 3, "backoff_seconds": 0}},
        }
        config = redirected_run_store(validate_config(raw), store_dir)
        refusal = NativeTUIUnavailableError("no native TUI for claude")
        with patch.object(graph_module, "run_claude_code", side_effect=refusal) as runner:
            final = build_graph(config).invoke(
                isolated_graph_state(config, "strict", store_dir, agent_execution_mode="native_tui")
            )
        self.assertEqual(runner.call_count, 1)
        self.assertIn("no native TUI for claude", final["error"])
        self.assertNotIn("after 3 attempts", final["error"])
        self.assertNotIn("execution_mode", final["agent_results"][0])

    def test_an_invalid_mode_in_state_stops_the_run(self):
        default_tracer.clear()
        store_dir = temporary_store_dir()
        self.addCleanup(shutil.rmtree, store_dir, ignore_errors=True)
        config = redirected_run_store(
            validate_config({"agents": [{"agent": "claude", "role": "researcher"}], "roles": dict(ROLES)}),
            store_dir,
        )
        with patch.object(graph_module, "run_claude_code") as runner:
            final = build_graph(config).invoke(
                isolated_graph_state(config, "typo", store_dir, agent_execution_mode="native")
            )
        runner.assert_not_called()
        self.assertIn("Invalid agent_execution_mode 'native'", final["error"])


class TestPreflightRefusesImpossibleNativeTui(unittest.TestCase):
    def _config(self, agents, mode):
        return validate_config({
            "agents": agents,
            "roles": dict(ROLES),
            "execution": {"agent_execution_mode": mode},
        })

    PIPELINE = [
        {"agent": "claude", "model": "sonnet", "role": "planner"},
        {"agent": "opencode", "model": "opencode/gpt-5.1-codex", "role": "implementer"},
    ]

    def test_native_tui_with_a_claude_member_is_an_error(self):
        errors = check_execution_mode(self._config(self.PIPELINE, "native_tui"))
        self.assertEqual(len(errors), 1)
        self.assertIn("claude (planner)", errors[0])
        self.assertIn("never falls back", errors[0])
        report = run_preflight(self._config(self.PIPELINE, "native_tui"))
        self.assertFalse(report["ok"])

    def test_every_rung_of_a_ladder_is_checked(self):
        ladder = [{"agent": ["opencode", "claude"], "model": ["opencode/gpt-5.1-codex", "sonnet"], "role": "implementer"}]
        self.assertTrue(check_execution_mode(self._config(ladder, "native_tui")))

    def test_an_all_opencode_pipeline_and_auto_are_fine(self):
        only = [{"agent": "opencode", "model": "opencode/gpt-5.1-codex", "role": "implementer"}]
        self.assertEqual(check_execution_mode(self._config(only, "native_tui")), [])
        self.assertEqual(check_execution_mode(self._config(self.PIPELINE, "auto")), [])
        self.assertEqual(check_execution_mode(self._config(self.PIPELINE, "headless")), [])

    def test_the_run_override_wins_over_the_configuration(self):
        config = self._config(self.PIPELINE, "auto")
        self.assertTrue(check_execution_mode(config, execution_mode="native_tui"))


class TestTheCliRefusesATypo(unittest.TestCase):
    def test_argparse_rejects_an_unknown_mode(self):
        completed = subprocess.run(
            [sys.executable, "-m", "orchestrator", "--agent-execution-mode", "native", "--doctor"],
            capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("invalid choice", completed.stderr)


# ---------------------------------------------------------------------------------------------
# The native controller: completion, output, and lifecycle
# ---------------------------------------------------------------------------------------------
class _FakeOpenCodeServer:
    """Scripted responses for the REST calls the controller makes (requests is patched)."""

    def __init__(self, statuses, messages, final_messages=None, session_tokens=None):
        self.statuses = list(statuses)
        self.messages = messages
        self.final_messages = final_messages if final_messages is not None else messages
        self.session_tokens = session_tokens
        self.status_polls = 0
        self.message_fetches_while_active = 0
        self._active = False

    def get(self, url, *args, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        if url.endswith("/global/health"):
            resp.json.return_value = {"healthy": True}
        elif url.endswith("/session/status"):
            self.status_polls += 1
            kind = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
            self._active = kind in ("busy", "retry")
            resp.json.return_value = {"ses_TESTSESSION": {"type": kind}} if kind else {}
        elif url.endswith("/message"):
            if self._active:
                self.message_fetches_while_active += 1
            resp.json.return_value = self.final_messages if not self.statuses[1:] else self.messages
        else:
            resp.json.return_value = {"tokens": self.session_tokens} if self.session_tokens else {}
        return resp

    def post(self, url, *args, **kwargs):
        resp = MagicMock()
        if url.endswith("/session"):
            resp.status_code = 200
            resp.json.return_value = {"id": "ses_TESTSESSION"}
        else:
            resp.status_code = 204
        return resp


def _assistant(text="done", finish="stop", completed=True, error=None, tokens=None):
    info = {"role": "assistant", "time": {"created": 1, **({"completed": 2} if completed else {})}, "finish": finish}
    if error:
        info["error"] = error
    parts = [{"type": "text", "text": text}] if text else []
    parts.append({"type": "step-finish", "tokens": tokens or {"total": 120, "input": 100, "output": 20, "reasoning": 0, "cache": {"read": 0, "write": 0}}})
    return {"info": info, "parts": parts}


class _ControllerCase(unittest.TestCase):
    def setUp(self):
        self.registry = os.path.join(tempfile.mkdtemp(prefix="orch_sessions_"), "native_sessions.json")
        self.addCleanup(shutil.rmtree, os.path.dirname(self.registry), ignore_errors=True)
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {native_sessions.REGISTRY_ENV: self.registry}).start()
        patch("orchestrator.agents.opencode_tui.get_antigravity_bridge_url", return_value="http://127.0.0.1:49182").start()
        patch("orchestrator.agents.opencode_tui.check_antigravity_bridge", return_value=True).start()
        patch("orchestrator.agents.opencode_tui.launch_antigravity_integrated_terminal", return_value=True).start()
        patch("orchestrator.agents.opencode_tui.POLL_INTERVAL_SECONDS", 0.001).start()
        patch("orchestrator.agents.opencode_tui.time.sleep", lambda *_a, **_k: None).start()
        patch("orchestrator.native_sessions.server_alive", return_value=True).start()
        self.close = patch("orchestrator.agents.opencode_tui.close_antigravity_integrated_terminal").start()
        self.proc = MagicMock()
        self.proc.poll.return_value = None
        self.proc.pid = 4242
        self.stop_tree = patch("orchestrator.agents.opencode_tui.stop_process_tree").start()

    def run_with(self, server, **kwargs):
        with patch("orchestrator.agents.opencode_tui.subprocess.Popen", return_value=self.proc), patch(
            "orchestrator.agents.opencode_tui.requests.get", side_effect=server.get
        ), patch("orchestrator.agents.opencode_tui.requests.post", side_effect=server.post):
            return run_opencode_native_tui(
                "task", project_root=tempfile.gettempdir(), terminal_title="LangGraph - OpenCode Repair #1",
                pause_on_completion=0.0, **kwargs,
            )

    def assert_torn_down(self):
        self.assertTrue(self.close.called, "the terminal must be closed")
        self.proc.terminate.assert_called()
        self.assertEqual(native_sessions._load(), [])


class TestCompletionDetection(_ControllerCase):
    def test_a_retry_status_is_not_the_end_of_the_turn(self):
        server = _FakeOpenCodeServer(
            ["busy", "retry", "retry", "busy", "idle"],
            [_assistant(completed=False)],
            final_messages=[_assistant("real answer")],
        )
        text, usage = self.run_with(server)
        self.assertEqual(text, "real answer")
        self.assertGreaterEqual(server.status_polls, 5)
        self.assertEqual(server.message_fetches_while_active, 0)
        self.assertEqual(usage["total_tokens"], 120)

    def test_the_startup_gap_before_busy_does_not_end_the_turn(self):
        server = _FakeOpenCodeServer(
            [None, None, "busy", "idle"], [], final_messages=[_assistant("after the gap")]
        )
        text, _usage = self.run_with(server)
        self.assertEqual(text, "after the gap")

    def test_a_tool_calls_step_is_not_the_final_step(self):
        server = _FakeOpenCodeServer(
            [None, None, None, "busy", "idle"],
            [_assistant("", finish="tool-calls")],
            final_messages=[_assistant("", finish="tool-calls"), _assistant("final")],
        )
        text, _usage = self.run_with(server)
        self.assertIn("final", text)

    def test_a_turn_that_produced_nothing_is_empty_not_a_made_up_success(self):
        server = _FakeOpenCodeServer(["busy", "idle"], [_assistant("")])
        text, _usage = self.run_with(server)
        self.assertEqual(text, "")

    def test_an_error_opencode_recorded_is_raised_with_its_usage(self):
        error = {"name": "APIError", "data": {"message": "upstream 529"}}
        server = _FakeOpenCodeServer(["busy", "idle"], [_assistant("", error=error)])
        with self.assertRaises(CLIExecutionError) as ctx:
            self.run_with(server, close_on_completion=False)
        self.assertIn("APIError: upstream 529", str(ctx.exception))
        self.assertEqual(ctx.exception.token_usage["total_tokens"], 120)
        self.assert_torn_down()

    def test_a_turn_that_never_starts_fails_fast_instead_of_waiting_out_the_timeout(self):
        # Measured on 1.18.29: an unusable model is accepted with 204, then nothing - no busy, no
        # assistant message. The session's own accounting reads zero, and that is reported.
        zeros = {"input": 0, "output": 0, "reasoning": 0, "cache": {"read": 0, "write": 0}}
        server = _FakeOpenCodeServer([None], [{"info": {"role": "user"}, "parts": []}], session_tokens=zeros)
        ticks = itertools.count(0, 5)
        with patch("orchestrator.agents.opencode_tui.time.time", side_effect=lambda: float(next(ticks))):
            with self.assertRaises(CLIExecutionError) as ctx:
                self.run_with(server, timeout_seconds=180)
        self.assertIn("never started the turn", str(ctx.exception))
        self.assertEqual(ctx.exception.token_usage["total_tokens"], 0)
        self.assert_torn_down()

    def test_a_timeout_says_when_opencode_was_waiting_for_a_person(self):
        server = _FakeOpenCodeServer(["busy"], [])
        original = server.get

        def get(url, *a, **k):
            if url.endswith("/question"):
                return MagicMock(status_code=200, json=MagicMock(return_value=[
                    {"id": "que_1", "sessionID": "ses_TESTSESSION", "questions": []},
                    {"id": "que_2", "sessionID": "ses_SOMEONE_ELSE", "questions": []},
                ]))
            if url.endswith("/permission"):
                return MagicMock(status_code=200, json=MagicMock(return_value=[]))
            return original(url, *a, **k)

        server.get = get
        clock = itertools.chain([0.0, 0.0, 0.0], itertools.repeat(1e12))
        with patch("orchestrator.agents.opencode_tui.time.time", side_effect=lambda: next(clock)):
            with self.assertRaises(CLITimeoutError) as ctx:
                self.run_with(server, timeout_seconds=5)
        self.assertIn("waiting in its TUI for a person: 1 question pending", str(ctx.exception))
        self.assert_torn_down()

    def test_the_session_aggregate_is_the_fallback_when_no_step_reported(self):
        msg = {"info": {"role": "assistant", "time": {"created": 1, "completed": 2}, "finish": "stop"},
               "parts": [{"type": "text", "text": "ok"}]}
        server = _FakeOpenCodeServer(["busy", "idle"], [msg], session_tokens=RealOpenCodePayloads.SESSION_ONE_STEP)
        _text, usage = self.run_with(server)
        self.assertEqual(usage["total_tokens"], 12176)
        self.assertEqual(usage["raw_usage"]["source"], "opencode-session-aggregate")


class TestLifecycle(_ControllerCase):
    def test_close_true_tears_everything_down(self):
        self.run_with(_FakeOpenCodeServer(["busy", "idle"], [_assistant()]), close_on_completion=True)
        self.assert_torn_down()
        self.close.assert_called_once_with(
            title="LangGraph - OpenCode Repair #1 [ESSION]", bridge_url="http://127.0.0.1:49182"
        )

    def test_close_false_keeps_the_tui_and_server_and_records_them(self):
        text, _usage = self.run_with(_FakeOpenCodeServer(["busy", "idle"], [_assistant("kept")]), close_on_completion=False)
        self.assertEqual(text, "kept")
        self.close.assert_not_called()
        self.proc.terminate.assert_not_called()
        records = native_sessions._load()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["session_id"], "ses_TESTSESSION")
        self.assertEqual(records[0]["server_pid"], 4242)
        self.assertEqual(records[0]["terminal_title"], "LangGraph - OpenCode Repair #1 [ESSION]")

    def test_a_timeout_is_torn_down_even_when_closure_is_off(self):
        # Health wait (two reads), the deadline (one), then every later read is past it.
        clock = itertools.chain([0.0, 0.0, 0.0], itertools.repeat(1e12))
        with patch("orchestrator.agents.opencode_tui.time.time", side_effect=lambda: next(clock)):
            with self.assertRaises(CLITimeoutError):
                self.run_with(_FakeOpenCodeServer(["busy"], []), close_on_completion=False, timeout_seconds=5)
        self.assert_torn_down()

    def test_an_interruption_is_torn_down_even_when_closure_is_off(self):
        server = _FakeOpenCodeServer(["busy"], [])
        original = server.get

        def interrupted(url, *a, **k):
            if url.endswith("/session/status") and server.status_polls >= 2:
                raise KeyboardInterrupt()
            return original(url, *a, **k)

        server.get = interrupted
        with self.assertRaises(KeyboardInterrupt):
            self.run_with(server, close_on_completion=False)
        self.assert_torn_down()

    def test_a_failed_server_start_is_torn_down(self):
        self.proc.poll.return_value = 1
        with self.assertRaises(CLIExecutionError):
            self.run_with(_FakeOpenCodeServer(["busy"], []), close_on_completion=False)
        self.proc.terminate.assert_called()
        self.assertEqual(native_sessions._load(), [])

    @unittest.skipUnless(sys.platform == "win32", "process-tree stop is the Windows path")
    def test_on_windows_the_whole_server_tree_is_stopped(self):
        self.run_with(_FakeOpenCodeServer(["busy", "idle"], [_assistant()]))
        self.stop_tree.assert_called_once_with(4242)

    def test_two_sessions_never_share_a_tab_title(self):
        titles = []
        for sid in ("ses_AAAAAAAAAAAA", "ses_BBBBBBBBBBBB"):
            server = _FakeOpenCodeServer(["busy", "idle"], [_assistant()])
            server.post = lambda url, *a, _sid=sid, **k: MagicMock(
                status_code=200 if url.endswith("/session") else 204,
                json=MagicMock(return_value={"id": _sid}),
            )
            launch = patch("orchestrator.agents.opencode_tui.launch_antigravity_integrated_terminal", return_value=True).start()
            self.run_with(server, close_on_completion=False)
            titles.append(launch.call_args.kwargs["title"])
        self.assertEqual(len(set(titles)), 2)


class TestTheAttachProcessIsEnded(_ControllerCase):
    """Measured live: `opencode attach` does not exit when its server stops, so it is stopped."""

    def _stopped_quickly(self, runner):
        from threading import Timer, Event

        stop = Event()
        Timer(0.5, stop.set).start()
        started = time.time()
        runner([sys.executable, "-c", "import time; time.sleep(30)"], timeout=60, stop=stop)
        return time.time() - started

    def test_a_captured_pipe_ends_when_stopped(self):
        from orchestrator.terminals import run_captured

        self.assertLess(self._stopped_quickly(run_captured), 10)

    def test_a_pty_ends_when_stopped(self):
        from orchestrator.terminals import pty_available, run_captured_pty

        if not pty_available():
            self.skipTest("no pty implementation here")
        self.assertLess(self._stopped_quickly(run_captured_pty), 10)

    def test_the_controller_stops_the_attach_process_at_teardown(self):
        seen = {}

        def fake_attach(cmd, cwd=None, timeout=180, sink=None, env=None, stop=None):
            seen["stop"] = stop
            stop.wait(5)
            seen["stopped"] = stop.is_set()

        with patch("orchestrator.terminals.pty_available", return_value=False), patch(
            "orchestrator.terminals.run_captured", side_effect=fake_attach
        ):
            self.run_with(_FakeOpenCodeServer(["busy", "idle"], [_assistant("via pty")]), sink=lambda _t: None)
        self.assertTrue(seen.get("stopped"), "teardown must end the attach process")


class TestTheRegistry(unittest.TestCase):
    def setUp(self):
        self.registry = os.path.join(tempfile.mkdtemp(prefix="orch_sessions_"), "native_sessions.json")
        self.addCleanup(shutil.rmtree, os.path.dirname(self.registry), ignore_errors=True)
        env = patch.dict(os.environ, {native_sessions.REGISTRY_ENV: self.registry})
        env.start()
        self.addCleanup(env.stop)

    def _record(self, n):
        return {"session_id": f"ses_{n}", "port": 50000 + n, "server_pid": 7000 + n, "terminal_title": f"t{n}"}

    def test_it_is_bounded_and_closes_the_oldest(self):
        with patch.object(native_sessions, "server_alive", return_value=True), patch.object(
            native_sessions, "close_session", side_effect=lambda r: {"session_id": r["session_id"]}
        ) as close:
            for n in range(native_sessions.MAX_RETAINED_SESSIONS + 2):
                native_sessions.register_session(self._record(n))
        self.assertEqual(len(native_sessions._load()), native_sessions.MAX_RETAINED_SESSIONS)
        self.assertEqual([c.args[0]["session_id"] for c in close.call_args_list], ["ses_0", "ses_1"])

    def test_a_pid_that_no_longer_owns_the_port_is_never_killed(self):
        with patch.object(native_sessions, "server_alive", return_value=True), patch.object(
            native_sessions, "listening_pid", return_value=9999
        ), patch.object(native_sessions, "stop_process_tree") as stop, patch(
            "orchestrator.launcher.close_antigravity_integrated_terminal", return_value=True
        ):
            outcome = native_sessions.close_session(self._record(1))
        stop.assert_not_called()
        self.assertFalse(outcome["server_stopped"])
        self.assertIn("left running", outcome["note"])

    def test_the_owning_pid_is_stopped(self):
        alive = iter([True] + [False] * 5)
        with patch.object(native_sessions, "server_alive", side_effect=lambda *a, **k: next(alive)), patch.object(
            native_sessions, "listening_pid", return_value=7001
        ), patch.object(native_sessions, "stop_process_tree") as stop, patch(
            "orchestrator.launcher.close_antigravity_integrated_terminal", return_value=True
        ):
            outcome = native_sessions.close_session(self._record(1))
        stop.assert_called_once_with(7001)
        self.assertTrue(outcome["server_stopped"])
        self.assertTrue(outcome["terminal_closed"])

    def test_close_sessions_forgets_what_it_closed(self):
        native_sessions._save([self._record(1), self._record(2)])
        with patch.object(native_sessions, "close_session", side_effect=lambda r: {"session_id": r["session_id"]}):
            closed = native_sessions.close_sessions()
        self.assertEqual([c["session_id"] for c in closed], ["ses_1", "ses_2"])
        self.assertEqual(native_sessions._load(), [])

    def test_dead_records_are_dropped_without_touching_anything(self):
        native_sessions._save([self._record(1)])
        with patch.object(native_sessions, "server_alive", return_value=False):
            self.assertEqual(native_sessions.list_sessions(), [])
        self.assertEqual(native_sessions._load(), [])


@unittest.skipUnless(sys.platform == "win32", "Windows-specific: a child outlives a terminated parent")
class TestProcessTreeStopForReal(unittest.TestCase):
    """The orphan was real, so the fix is tested against real processes, not a mock."""

    def _alive(self, pid):
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return str(pid) in out

    def test_a_grandchild_is_stopped_with_its_parent(self):
        marker = os.path.join(tempfile.mkdtemp(prefix="orch_tree_"), "child.pid")
        self.addCleanup(shutil.rmtree, os.path.dirname(marker), ignore_errors=True)
        script = (
            "import subprocess,sys,time;"
            "c=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']);"
            f"open(r'{marker}','w').write(str(c.pid));time.sleep(60)"
        )
        parent = subprocess.Popen([sys.executable, "-c", script])
        deadline = time.time() + 20
        while not os.path.exists(marker) and time.time() < deadline:
            time.sleep(0.1)
        time.sleep(0.2)
        child = int(open(marker).read())
        self.assertTrue(self._alive(child))
        native_sessions.stop_process_tree(parent.pid)
        parent.wait(timeout=10)
        deadline = time.time() + 10
        while self._alive(child) and time.time() < deadline:
            time.sleep(0.2)
        self.assertFalse(self._alive(child), "the child must not survive its parent")


# ---------------------------------------------------------------------------------------------
# Visible (headless) terminals honour the same setting
# ---------------------------------------------------------------------------------------------
class TestVisibleTerminalLifecycle(unittest.TestCase):
    def _run_integrated(self, close, write_status=True):
        from orchestrator.launcher import run_agent_cli

        original = tempfile.mkdtemp

        def mkdtemp(*args, **kwargs):
            d = original(*args, **kwargs)
            if write_status:
                with open(os.path.join(d, "status.json"), "w", encoding="utf-8") as fh:
                    json.dump({"returncode": 0, "stdout": "ok", "stderr": "", "duration_seconds": 0.1}, fh)
            return d

        with patch("orchestrator.launcher.check_antigravity_bridge", return_value=True), patch(
            "orchestrator.launcher.launch_antigravity_integrated_terminal", return_value=True
        ) as launch, patch(
            "orchestrator.launcher.close_antigravity_integrated_terminal", return_value=True
        ) as closer, patch("tempfile.mkdtemp", side_effect=mkdtemp):
            try:
                run_agent_cli(["x"], visible=True, terminal_type="antigravity_integrated",
                              title="LangGraph - Claude Planner", pause_on_completion=0,
                              close_on_completion=close, timeout=1)
            except CLITimeoutError:
                pass
        return launch, closer

    def test_close_true_closes_the_integrated_tab_it_opened(self):
        launch, closer = self._run_integrated(close=True)
        title = launch.call_args.kwargs["title"]
        self.assertTrue(title.startswith("LangGraph - Claude Planner ["))
        closer.assert_called_once_with(title=title)

    def test_close_false_leaves_the_tab(self):
        _launch, closer = self._run_integrated(close=False)
        closer.assert_not_called()

    def test_a_timed_out_tab_is_closed_whatever_the_setting(self):
        with patch("time.time", side_effect=(1000.0 + 10 * i for i in range(10_000))), patch("time.sleep"):
            _launch, closer = self._run_integrated(close=False, write_status=False)
        closer.assert_called_once()

    def test_a_kept_os_window_holds_itself_open_and_does_not_block(self):
        from orchestrator.launcher import run_agent_cli

        captured = {}

        def popen(args, **kwargs):
            job = json.load(open(args[args.index("--job-file") + 1], encoding="utf-8"))
            captured["job"] = job
            with open(job["status_file"], "w", encoding="utf-8") as fh:
                json.dump({"returncode": 0, "stdout": "held", "stderr": "", "duration_seconds": 0.1}, fh)
            proc = MagicMock()
            proc.poll.return_value = None  # still open, waiting for Enter
            return proc

        with patch("orchestrator.launcher.check_antigravity_bridge", return_value=False), patch(
            "subprocess.Popen", side_effect=popen
        ):
            res = run_agent_cli(["x"], visible=True, terminal_type="console", pause_on_completion=0,
                                close_on_completion=False, timeout=30)
        self.assertTrue(captured["job"]["hold_open"])
        self.assertEqual(res.stdout, "held")

    def test_the_runner_waits_for_enter_only_when_holding_open(self):
        from orchestrator.launcher import _runner_entrypoint

        for hold, expect_input in ((True, True), (False, False)):
            d = tempfile.mkdtemp(prefix="orch_runner_")
            self.addCleanup(shutil.rmtree, d, ignore_errors=True)
            job = os.path.join(d, "job.json")
            with open(job, "w", encoding="utf-8") as fh:
                json.dump({"cmd": [sys.executable, "-c", "print('hi')"], "cwd": d,
                           "status_file": os.path.join(d, "status.json"), "timeout": 30,
                           "pause_seconds": 0, "hold_open": hold}, fh)
            with patch("builtins.input", return_value="") as waited, self.assertRaises(SystemExit):
                _runner_entrypoint(job)
            self.assertEqual(waited.called, expect_input)
            self.assertTrue(os.path.exists(os.path.join(d, "status.json")))


class TestAgentsAreShownTheirWorktree(unittest.TestCase):
    """Found live: shown the user's checkout as 'Project Root', OpenCode read test_calc.py from
    there, left its worktree, and stopped on its own external-directory permission prompt."""

    def test_an_isolated_run_names_the_worktree_and_not_the_checkout(self):
        from orchestrator.context import collect_project_context

        root = tempfile.mkdtemp(prefix="orch_root_")
        worktree = tempfile.mkdtemp(prefix="orch_wt_")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        self.addCleanup(shutil.rmtree, worktree, ignore_errors=True)
        text = collect_project_context(root, working_directory=worktree)
        self.assertIn(os.path.abspath(worktree), text)
        self.assertNotIn(os.path.abspath(root), text)
        self.assertIn("must not be read or modified", text)

    def test_an_unisolated_run_is_unchanged(self):
        from orchestrator.context import collect_project_context

        root = tempfile.mkdtemp(prefix="orch_root_")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        self.assertEqual(collect_project_context(root), collect_project_context(root, working_directory=root))
        self.assertIn(f"- **Project Root**: `{os.path.realpath(root)}`", collect_project_context(root))


class TestTheSettingIsConfiguration(unittest.TestCase):
    def test_default_is_true_and_it_round_trips(self):
        self.assertTrue(get_execution_config(validate_config({"agents": [{"agent": "claude", "role": "researcher"}], "roles": dict(ROLES)}))["close_terminal_on_completion"])
        cfg = validate_config({"agents": [{"agent": "claude", "role": "researcher"}], "roles": dict(ROLES),
                               "execution": {"close_terminal_on_completion": False}})
        self.assertFalse(get_execution_config(cfg)["close_terminal_on_completion"])

    def test_a_non_boolean_is_refused(self):
        with self.assertRaises(ConfigValidationError):
            validate_config({"agents": [{"agent": "claude", "role": "researcher"}], "roles": dict(ROLES),
                             "execution": {"close_terminal_on_completion": "no"}})

    def test_the_graph_passes_it_to_every_runner(self):
        default_tracer.clear()
        store_dir = temporary_store_dir()
        self.addCleanup(shutil.rmtree, store_dir, ignore_errors=True)
        config = redirected_run_store(validate_config({
            "agents": [{"agent": "claude", "model": "sonnet", "role": "researcher"}],
            "roles": dict(ROLES), "preflight": {"enabled": False},
            "execution": {"close_terminal_on_completion": False},
        }), store_dir)
        with patch.object(graph_module, "run_claude_code", return_value=("r", _usage(10))) as runner:
            build_graph(config).invoke(isolated_graph_state(config, "keep", store_dir))
        self.assertIs(runner.call_args.kwargs["close_on_completion"], False)


if __name__ == "__main__":
    unittest.main()
