"""Tests for surviving a flaky agent (per-execution retry).

Why this exists, specifically. Across this project's own recorded history, **every** real
end-to-end failure was the same event: one agent returned empty output on the first step of the
pipeline, and the run halted with no second attempt. Seven failures, one cause. The model
escalation ladder already existed but only fired after a verification FAIL, so an agent that
returned *nothing* got no retry, no fallback, and no second model.

These tests are therefore mostly about the failure path, and three properties matter more than
the happy one:

* a retry is **not an ensemble** - however many attempts an execution takes, the phase produces
  exactly one `AgentResult`, because that list is what consensus is resolved from and what
  `--stats` computes pass rates over;
* `attempts: 1` **restores the old behaviour exactly**, so the change is reversible by
  configuration rather than by a revert; and
* an execution that never succeeds still **fails**, with every attempt's reason kept - a retry
  policy that turned a broken agent into a silent success would be worse than no retry at all.
"""

import shutil
import tempfile
import unittest
from unittest.mock import patch

import orchestrator.graph as graph_module
from orchestrator.config import (
    ConfigValidationError,
    MAX_RETRY_ATTEMPTS,
    get_retry_config,
    validate_config,
)
from orchestrator.graph import build_graph
from orchestrator.store import EVENT_AGENT_RETRY
from orchestrator.tracer import default_tracer
from orchestrator.types import create_agent_result

from tests.support import isolated_graph_state, read_runs, redirected_run_store, temporary_store_dir

ROLES = {
    r: {"responsibility": r}
    for r in ("researcher", "planner", "implementer", "verifier")
}

PIPELINE = [
    {"agent": "antigravity", "model": "gemini-3.8-flash-high", "role": "researcher"},
    {"agent": "claude", "model": "sonnet", "role": "planner"},
    {"agent": "opencode", "model": "opencode/gpt-5.1-codex", "role": "implementer"},
    {"agent": "claude", "model": "sonnet", "role": "verifier"},
]

LADDER_PIPELINE = [
    {
        "agent": "antigravity",
        "model": [
            "gemini-3.8-flash-low",
            "gemini-3.8-flash-medium",
            "gemini-3.8-flash-high",
        ],
        "role": "researcher",
    },
] + PIPELINE[1:]


def _config(retry=None, pipeline=None):
    raw = {
        "agents": list(pipeline or PIPELINE),
        "roles": dict(ROLES),
        "preflight": {"enabled": False},
    }
    if retry is not None:
        # Passed through as given, so a malformed value reaches the validator rather than
        # being rejected by this helper - the point is what `validate_config` refuses.
        raw["execution"] = {"retry": dict(retry) if isinstance(retry, dict) else retry}
    return validate_config(raw)


class _PipelineCase(unittest.TestCase):
    """Drives the real graph with a scripted researcher, which is the agent that actually
    failed in production."""

    def setUp(self):
        default_tracer.clear()
        self.store_dir = temporary_store_dir()
        self.addCleanup(shutil.rmtree, self.store_dir, ignore_errors=True)

    def run_pipeline(self, researcher, retry=None, pipeline=None):
        """Run the graph with `researcher` scripting the first agent.

        `researcher` is a list of return values (the last repeats) or a callable.
        Returns ``(run_record, state, calls)`` where `calls` records every invocation.
        """
        config = redirected_run_store(_config(retry, pipeline), self.store_dir)
        calls = {"count": 0, "models": []}

        def antigravity(prompt, **kwargs):
            calls["count"] += 1
            calls["models"].append(kwargs.get("model"))
            if callable(researcher):
                return researcher(calls["count"])
            index = min(calls["count"] - 1, len(researcher) - 1)
            value = researcher[index]
            if isinstance(value, Exception):
                raise value
            return value

        with patch.object(graph_module, "run_antigravity", side_effect=antigravity), \
             patch.object(
                 graph_module, "run_claude_code",
                 side_effect=["plan", "VERDICT: PASS\nlooks right"],
             ), \
             patch.object(graph_module, "run_opencode", return_value="implementation"):
            state = isolated_graph_state(config, "retry under test", self.store_dir)
            state["config"] = config
            result = build_graph(config).invoke(state)

        records = read_runs(self.store_dir)
        return (records[-1] if records else {}), result, calls

    def researcher_result(self, state):
        for entry in state.get("agent_results") or []:
            if entry.get("role") == "researcher":
                return entry
        return {}


class TestItSurvivesTheFailureThatActuallyHappened(_PipelineCase):
    """Empty output from the researcher, which was 7 of 7 real failures."""

    def test_an_execution_that_recovers_on_the_third_attempt_completes_the_run(self):
        record, state, calls = self.run_pipeline(
            ["", "", "real research"], retry={"attempts": 3, "backoff_seconds": 0}
        )
        self.assertEqual(record["status"], "completed")
        self.assertEqual(calls["count"], 3)
        self.assertEqual(self.researcher_result(state)["status"], "success")

    def test_an_exception_retries_the_same_way_an_empty_reply_does(self):
        """A subprocess over a network fails both ways, and a policy that only handled one
        would leave half the failure modes fatal."""
        record, state, calls = self.run_pipeline(
            [RuntimeError("connection reset"), RuntimeError("connection reset"), "research"],
            retry={"attempts": 3, "backoff_seconds": 0},
        )
        self.assertEqual(record["status"], "completed")
        self.assertEqual(calls["count"], 3)

    def test_the_phase_still_produces_exactly_one_result(self):
        """A retry is not an ensemble. `agent_results` is what consensus is resolved from and
        what `--stats` counts, so three attempts must not look like three agents."""
        _, state, _ = self.run_pipeline(
            ["", "", "research"], retry={"attempts": 3, "backoff_seconds": 0}
        )
        researchers = [
            r for r in (state.get("agent_results") or []) if r.get("role") == "researcher"
        ]
        self.assertEqual(len(researchers), 1)
        self.assertEqual(len(state.get("agent_results") or []), len(PIPELINE))

    def test_the_result_says_how_many_attempts_it_took_and_why_they_failed(self):
        _, state, _ = self.run_pipeline(
            ["", "", "research"], retry={"attempts": 3, "backoff_seconds": 0}
        )
        result = self.researcher_result(state)
        self.assertEqual(result["attempts"], 3)
        self.assertEqual(len(result["attempt_failures"]), 2)
        self.assertIn("empty output", result["attempt_failures"][0])

    def test_a_first_time_success_carries_no_retry_noise(self):
        """Every result would otherwise gain `attempts: 1`, which is noise in a stored event."""
        _, state, calls = self.run_pipeline(
            ["research"], retry={"attempts": 3, "backoff_seconds": 0}
        )
        result = self.researcher_result(state)
        self.assertEqual(calls["count"], 1)
        self.assertNotIn("attempts", result)
        self.assertNotIn("attempt_failures", result)


class TestItStillFailsWhenItShould(_PipelineCase):
    """A retry policy that turned a broken agent into a silent success would be worse than
    none at all."""

    def test_an_execution_that_never_succeeds_still_fails_the_run(self):
        record, state, calls = self.run_pipeline(
            [""], retry={"attempts": 3, "backoff_seconds": 0}
        )
        self.assertEqual(record["status"], "error")
        self.assertEqual(calls["count"], 3)
        self.assertEqual(self.researcher_result(state)["status"], "error")

    def test_the_failure_says_it_was_attempted_more_than_once(self):
        _, state, _ = self.run_pipeline([""], retry={"attempts": 3, "backoff_seconds": 0})
        self.assertIn("after 3 attempts", self.researcher_result(state)["output"])

    def test_every_attempt_s_reason_survives_into_the_result(self):
        """"It was flaky" and "it is broken" must stay distinguishable after the run."""
        _, state, _ = self.run_pipeline([""], retry={"attempts": 3, "backoff_seconds": 0})
        self.assertEqual(len(self.researcher_result(state)["attempt_failures"]), 3)

    def test_the_last_exception_is_named_rather_than_flattened_to_empty_output(self):
        _, state, _ = self.run_pipeline(
            [RuntimeError("gateway timeout")], retry={"attempts": 2, "backoff_seconds": 0}
        )
        output = self.researcher_result(state)["output"]
        self.assertIn("RuntimeError", output)
        self.assertIn("gateway timeout", output)

    def test_retries_are_bounded_by_the_configured_attempts(self):
        for attempts in (1, 2, 4):
            with self.subTest(attempts=attempts):
                self.setUp()
                _, _, calls = self.run_pipeline(
                    [""], retry={"attempts": attempts, "backoff_seconds": 0}
                )
                self.assertEqual(calls["count"], attempts)


class TestTurningItOffRestoresTheOldBehaviour(_PipelineCase):
    """The change must be reversible by configuration, not by a revert."""

    def test_one_attempt_invokes_the_agent_once_and_halts(self):
        record, state, calls = self.run_pipeline(
            ["", "", "research"], retry={"attempts": 1}
        )
        self.assertEqual(record["status"], "error")
        self.assertEqual(calls["count"], 1)

    def test_one_attempt_reports_the_bare_reason_with_no_attempt_count(self):
        _, state, _ = self.run_pipeline([""], retry={"attempts": 1})
        result = self.researcher_result(state)
        self.assertEqual(result["output"], "antigravity returned empty output for researcher")
        self.assertNotIn("attempts", result)


class TestTheLadderIsUsedAsAFallback(_PipelineCase):
    """The escalation ladder existed but only fired after a verification FAIL."""

    def test_each_retry_steps_down_the_ladder(self):
        _, _, calls = self.run_pipeline(
            ["", "", "research"],
            retry={"attempts": 3, "backoff_seconds": 0},
            pipeline=LADDER_PIPELINE,
        )
        self.assertEqual(
            calls["models"],
            ["gemini-3.8-flash-low", "gemini-3.8-flash-medium", "gemini-3.8-flash-high"],
        )

    def test_the_last_rung_is_reused_rather_than_running_off_the_end(self):
        _, _, calls = self.run_pipeline(
            [""], retry={"attempts": 5, "backoff_seconds": 0}, pipeline=LADDER_PIPELINE
        )
        self.assertEqual(len(calls["models"]), 5)
        self.assertEqual(calls["models"][-1], "gemini-3.8-flash-high")
        self.assertEqual(calls["models"][-2], "gemini-3.8-flash-high")

    def test_escalation_can_be_turned_off_without_turning_off_retrying(self):
        _, _, calls = self.run_pipeline(
            ["", "", "research"],
            retry={"attempts": 3, "backoff_seconds": 0, "escalate_model": False},
            pipeline=LADDER_PIPELINE,
        )
        self.assertEqual(calls["count"], 3)
        self.assertEqual(set(calls["models"]), {"gemini-3.8-flash-low"})

    def test_an_agent_with_no_ladder_simply_retries_the_same_model(self):
        _, _, calls = self.run_pipeline(
            ["", "", "research"], retry={"attempts": 3, "backoff_seconds": 0}
        )
        self.assertEqual(set(calls["models"]), {"gemini-3.8-flash-high"})


class TestARetryIsARecordedFact(_PipelineCase):
    """A retry costs a whole agent invocation, so it is not something to swallow."""

    def test_each_retry_is_written_to_the_run_store(self):
        self.run_pipeline(["", "", "research"], retry={"attempts": 3, "backoff_seconds": 0})
        import json
        import os

        from orchestrator.store import runs_root

        root = runs_root(os.getcwd(), self.store_dir)
        run_dir = sorted(p for p in root.iterdir() if p.is_dir())[-1]
        events = [
            json.loads(line)
            for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        retries = [e for e in events if e.get("event") == EVENT_AGENT_RETRY]
        self.assertEqual(len(retries), 2)
        self.assertEqual(retries[0]["attempt"], 1)
        self.assertEqual(retries[0]["of"], 3)
        self.assertIn("empty output", retries[0]["reason"])

    def test_a_retry_is_announced_on_the_console(self):
        """A run that pauses with no explanation looks hung."""
        self.run_pipeline(["", "research"], retry={"attempts": 2, "backoff_seconds": 0})
        names = [e.name for e in default_tracer.get_events()]
        self.assertIn("agent_retry", names)

    def test_a_run_that_never_retried_records_no_retry_events(self):
        self.run_pipeline(["research"], retry={"attempts": 3, "backoff_seconds": 0})
        names = [e.name for e in default_tracer.get_events()]
        self.assertNotIn("agent_retry", names)


class TestTheBackoffWidens(_PipelineCase):
    def test_the_delay_doubles_and_is_capped(self):
        slept = []
        with patch.object(graph_module.time, "sleep", side_effect=slept.append):
            self.run_pipeline(
                [""],
                retry={"attempts": 5, "backoff_seconds": 1, "max_backoff_seconds": 4},
            )
        # 1, 2, 4, then capped at 4. The final attempt does not sleep - nothing follows it.
        self.assertEqual(slept, [1.0, 2.0, 4.0, 4.0])

    def test_a_zero_backoff_never_sleeps(self):
        slept = []
        with patch.object(graph_module.time, "sleep", side_effect=slept.append):
            self.run_pipeline([""], retry={"attempts": 3, "backoff_seconds": 0})
        self.assertEqual(slept, [])


class TestTheRetryPolicyIsConfiguration(unittest.TestCase):
    def test_it_is_on_by_default(self):
        retry = get_retry_config(_config())
        self.assertGreater(retry["attempts"], 1)
        self.assertTrue(retry["escalate_model"])

    def test_its_values_can_be_set(self):
        retry = get_retry_config(
            _config({"attempts": 5, "backoff_seconds": 0.5, "escalate_model": False})
        )
        self.assertEqual(retry["attempts"], 5)
        self.assertEqual(retry["backoff_seconds"], 0.5)
        self.assertFalse(retry["escalate_model"])

    def test_nonsense_is_refused_at_load_rather_than_at_run(self):
        for bad in (
            "not a mapping",
            {"attempts": 0},
            {"attempts": MAX_RETRY_ATTEMPTS + 1},
            {"attempts": "three"},
            {"attempts": True},
            {"backoff_seconds": -1},
            {"max_backoff_seconds": "long"},
            {"escalate_model": "yes"},
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(ConfigValidationError):
                    _config(bad)

    def test_a_retry_policy_can_never_become_an_unbounded_loop(self):
        """Insurance against a flaky call, not a way to hammer a service that is down."""
        with self.assertRaises(ConfigValidationError):
            _config({"attempts": 1000})


class TestTheResultShape(unittest.TestCase):
    def test_attempts_is_only_carried_when_it_says_something(self):
        first_try = create_agent_result("claude", "verifier", "success", "ok", attempts=1)
        self.assertNotIn("attempts", first_try)

        retried = create_agent_result(
            "claude", "verifier", "success", "ok",
            attempts=2, attempt_failures=["attempt 1: empty"],
        )
        self.assertEqual(retried["attempts"], 2)
        self.assertEqual(retried["attempt_failures"], ["attempt 1: empty"])

    def test_an_empty_failure_list_is_not_carried(self):
        result = create_agent_result(
            "claude", "verifier", "success", "ok", attempts=1, attempt_failures=[]
        )
        self.assertNotIn("attempt_failures", result)


if __name__ == "__main__":
    unittest.main()
