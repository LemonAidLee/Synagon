"""Tests for the first two roadmap phases.

Covers:
  Phase 0  Objective acceptance gate  - orchestrator.acceptance, the gate node, verdict veto
  Phase 1  Goal decomposition         - orchestrator.decompose, the plan-only graph
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import orchestrator.graph as graph_module
from orchestrator.acceptance import (
    describe_check,
    format_for_prompt,
    gate_failed,
    latest_check,
    parse_command,
    run_acceptance,
    skipped_check,
)
from orchestrator.config import (
    ConfigValidationError,
    get_acceptance_config,
    get_planning_config,
    validate_config,
)
from orchestrator.decompose import (
    extract_json_block,
    format_plan,
    normalize_plan,
    parse_task_plan,
    plan_is_usable,
    topological_waves,
)
from orchestrator.graph import build_graph, build_plan_graph
from orchestrator.preflight import check_acceptance_command
from orchestrator.stats import compute_stats, format_stats
from orchestrator.status import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_NEEDS_REPAIR,
    STATUS_PLANNED,
    acceptance_overrides_pass,
    derive_status,
    derive_verdict,
)

ROLES = {
    r: {"responsibility": r}
    for r in ("decomposer", "researcher", "planner", "implementer", "verifier")
}

FULL_PIPELINE = [
    {"agent": "antigravity", "model": "gemini-3.8-flash-high", "role": "researcher"},
    {"agent": "claude", "model": "sonnet", "role": "planner"},
    {"agent": "opencode", "model": "opencode/gpt-5.1-codex", "role": "implementer"},
    {"agent": "claude", "model": "sonnet", "role": "verifier"},
]

#: A command that is guaranteed to exist and to control its own exit code.
PY = sys.executable


def _cfg(agents=None, **extra):
    config = {"agents": list(agents or FULL_PIPELINE), "roles": dict(ROLES)}
    config.update(extra)
    return validate_config(config)


def _gate(command, **extra):
    """A verification config carrying an acceptance command."""
    acceptance = {"command": command}
    acceptance.update(extra)
    return {"consensus": "unanimous", "acceptance": acceptance}


def _check(ok=True, repair_attempts=0, **extra):
    record = {
        "command": ["x"],
        "ok": ok,
        "exit_code": 0 if ok else 1,
        "duration_seconds": 0.1,
        "output": "",
        "skipped": False,
        "repair_attempts": repair_attempts,
    }
    record.update(extra)
    return record


# ===========================================================================
# Phase 0 - running the gate
# ===========================================================================


class TestParseCommand(unittest.TestCase):
    def test_list_is_taken_as_argv(self):
        self.assertEqual(parse_command(["pytest", "-q"]), ["pytest", "-q"])

    def test_string_is_tokenized(self):
        self.assertEqual(parse_command("pytest -q"), ["pytest", "-q"])

    def test_empty_means_no_gate(self):
        for empty in (None, "", "   ", []):
            self.assertEqual(parse_command(empty), [])

    def test_quoted_argument_survives(self):
        self.assertEqual(
            parse_command('python -c "import sys"'), ["python", "-c", "import sys"]
        )

    @unittest.skipUnless(os.name == "nt", "Windows path tokenization")
    def test_windows_paths_keep_their_backslashes(self):
        argv = parse_command(r"C:\Python\python.exe -m pytest")
        self.assertEqual(argv[0], r"C:\Python\python.exe")


class TestRunAcceptance(unittest.TestCase):
    """The gate reports what actually happened, and never fails open."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="orch_gate_")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_zero_exit_is_a_pass(self):
        check = run_acceptance([PY, "-c", "print('ok')"], self.dir)
        self.assertTrue(check["ok"])
        self.assertEqual(check["exit_code"], 0)
        self.assertIn("ok", check["output"])
        self.assertFalse(check["skipped"])

    def test_non_zero_exit_is_a_failure_and_keeps_the_output(self):
        check = run_acceptance(
            [PY, "-c", "import sys; print('boom'); sys.exit(3)"], self.dir
        )
        self.assertFalse(check["ok"])
        self.assertEqual(check["exit_code"], 3)
        self.assertIn("boom", check["output"])

    def test_stderr_is_captured(self):
        check = run_acceptance(
            [PY, "-c", "import sys; sys.stderr.write('bad news'); sys.exit(1)"], self.dir
        )
        self.assertIn("bad news", check["output"])

    def test_a_missing_binary_is_a_failure_not_a_pass(self):
        check = run_acceptance(["definitely-not-a-real-binary"], self.dir)
        self.assertFalse(check["ok"])
        self.assertIn("not found", check["error"])

    def test_a_timeout_is_a_failure_not_a_pass(self):
        check = run_acceptance(
            [PY, "-c", "import time; time.sleep(30)"], self.dir, timeout_seconds=1
        )
        self.assertFalse(check["ok"])
        self.assertIn("timeout", check["error"])

    def test_no_command_is_skipped_and_vetoes_nothing(self):
        check = run_acceptance("", self.dir)
        self.assertTrue(check["skipped"])
        self.assertFalse(gate_failed([check]))

    def test_it_runs_in_the_directory_it_is_given(self):
        with open(os.path.join(self.dir, "marker.txt"), "w", encoding="utf-8") as handle:
            handle.write("here")
        check = run_acceptance(
            [PY, "-c", "import os,sys; sys.exit(0 if os.path.isfile('marker.txt') else 9)"],
            self.dir,
        )
        self.assertTrue(check["ok"], check)

    def test_long_output_is_trimmed_from_the_front(self):
        check = run_acceptance(
            [PY, "-c", "print('x' * 50000); print('THE-TAIL')"], self.dir, output_limit=500
        )
        self.assertLess(len(check["output"]), 1200)
        self.assertIn("THE-TAIL", check["output"])
        self.assertIn("omitted", check["output"])


class TestCheckSelection(unittest.TestCase):
    """The check that judges an attempt is the one made for that attempt."""

    def test_latest_check_is_pinned_to_a_repair_generation(self):
        checks = [_check(ok=False, repair_attempts=0), _check(ok=True, repair_attempts=1)]
        self.assertFalse(latest_check(checks, 0)["ok"])
        self.assertTrue(latest_check(checks, 1)["ok"])
        self.assertTrue(latest_check(checks)["ok"])

    def test_gate_failed_ignores_other_generations(self):
        checks = [_check(ok=False, repair_attempts=0), _check(ok=True, repair_attempts=1)]
        self.assertTrue(gate_failed(checks, 0))
        self.assertFalse(gate_failed(checks, 1))

    def test_a_skipped_check_never_fails(self):
        self.assertFalse(gate_failed([skipped_check()]))

    def test_missing_checks_never_fail(self):
        self.assertFalse(gate_failed(None))
        self.assertFalse(gate_failed([]))


class TestGateReporting(unittest.TestCase):
    def test_describe_covers_every_shape(self):
        self.assertIn("not configured", describe_check(skipped_check()))
        self.assertIn("PASSED", describe_check(_check(ok=True)))
        self.assertIn("FAILED", describe_check(_check(ok=False)))
        self.assertIn("COULD NOT RUN", describe_check(_check(ok=False, error="gone")))
        self.assertIn("not run", describe_check(None))

    def test_prompt_section_is_empty_when_there_is_no_gate(self):
        self.assertEqual(format_for_prompt(None), "")
        self.assertEqual(format_for_prompt(skipped_check()), "")

    def test_a_red_gate_tells_the_agent_it_cannot_be_argued_with(self):
        text = format_for_prompt(_check(ok=False, output="AssertionError: nope"))
        self.assertIn("FAILED", text)
        self.assertIn("cannot be explained away", text)
        self.assertIn("AssertionError", text)

    def test_a_green_gate_is_not_reported_as_proof_of_completeness(self):
        text = format_for_prompt(_check(ok=True))
        self.assertIn("PASSED", text)
        self.assertIn("not proof of completeness", text)


# ===========================================================================
# Phase 0 - what the gate's result means
# ===========================================================================


class TestGateVeto(unittest.TestCase):
    """A red required gate overrides a verifier's PASS. Nothing else does."""

    def _state(self, verdict="PASS", checks=(), **extra):
        state = {
            "verification_history": [
                {"attempt": 1, "verdict": verdict, "repair_attempts": 0}
            ],
            "repair_attempts": 0,
            "max_repair_attempts": 2,
            "acceptance_checks": list(checks),
            "acceptance_required": True,
        }
        state.update(extra)
        return state

    def test_pass_stands_when_the_gate_is_green(self):
        state = self._state(checks=[_check(ok=True)])
        self.assertEqual(derive_verdict(state), "PASS")
        self.assertEqual(derive_status(state), STATUS_COMPLETED)

    def test_pass_becomes_fail_when_the_gate_is_red(self):
        state = self._state(checks=[_check(ok=False)])
        self.assertTrue(acceptance_overrides_pass(state))
        self.assertEqual(derive_verdict(state), "FAIL")
        self.assertEqual(derive_status(state), STATUS_NEEDS_REPAIR)

    def test_an_advisory_gate_decides_nothing(self):
        state = self._state(checks=[_check(ok=False)], acceptance_required=False)
        self.assertFalse(acceptance_overrides_pass(state))
        self.assertEqual(derive_verdict(state), "PASS")

    def test_blocked_outranks_the_gate(self):
        # A human is needed regardless of what any test says.
        state = self._state(verdict="BLOCKED", checks=[_check(ok=False)])
        self.assertEqual(derive_verdict(state), "BLOCKED")

    def test_the_gate_cannot_turn_a_fail_into_a_pass(self):
        state = self._state(verdict="FAIL", checks=[_check(ok=True)])
        self.assertEqual(derive_verdict(state), "FAIL")

    def test_no_gate_means_no_change_in_behaviour(self):
        state = self._state(checks=[])
        self.assertEqual(derive_verdict(state), "PASS")
        self.assertEqual(derive_status(state), STATUS_COMPLETED)

    def test_the_veto_is_pinned_to_the_current_repair_generation(self):
        # The gate failed before the repair; the one after it passed.
        state = self._state(
            checks=[_check(ok=False, repair_attempts=0), _check(ok=True, repair_attempts=1)],
            repair_attempts=1,
            verification_history=[
                {"attempt": 1, "verdict": "FAIL", "repair_attempts": 0},
                {"attempt": 2, "verdict": "PASS", "repair_attempts": 1},
            ],
        )
        self.assertEqual(derive_verdict(state), "PASS")


class TestAcceptanceConfig(unittest.TestCase):
    def test_disabled_by_default(self):
        resolved = get_acceptance_config(None)
        self.assertEqual(resolved["command"], "")
        self.assertTrue(resolved["required"])

    def test_string_and_list_commands_both_validate(self):
        for command in ("pytest -q", ["pytest", "-q"]):
            config = _cfg(verification=_gate(command))
            self.assertEqual(get_acceptance_config(config)["command"], command)

    def test_rejects_a_command_that_is_neither(self):
        with self.assertRaises(ConfigValidationError):
            _cfg(verification=_gate(42))

    def test_rejects_a_nonsense_timeout(self):
        with self.assertRaises(ConfigValidationError):
            _cfg(verification=_gate("pytest", timeout_seconds=0))

    def test_rejects_a_non_boolean_required(self):
        with self.assertRaises(ConfigValidationError):
            _cfg(verification=_gate("pytest", required="yes"))

    def test_preflight_warns_about_a_command_it_cannot_find(self):
        config = _cfg(verification=_gate("definitely-not-a-real-binary check"))
        warnings = check_acceptance_command(config)
        self.assertEqual(len(warnings), 1)
        self.assertIn("not found on PATH", warnings[0])

    def test_preflight_is_silent_without_a_gate(self):
        self.assertEqual(check_acceptance_command(_cfg()), [])


class TestGateInGraph(unittest.TestCase):
    """The gate runs before the verifier, on every pass through it."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="orch_gate_graph_")
        self.calls = []

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _invoke(self, config, verifier_outputs, impl=None):
        outputs = list(verifier_outputs)

        def claude(prompt, **kwargs):
            if kwargs.get("role") == "verifier":
                self.calls.append(("verify", prompt))
                return outputs.pop(0) if outputs else "VERDICT: PASS"
            return "plan"

        def opencode(prompt, **kwargs):
            self.calls.append(("impl", prompt))
            if impl:
                impl()
            return ("implemented", {"available": False}, "headless")

        with patch.object(graph_module, "run_antigravity", return_value="research"), \
             patch.object(graph_module, "run_claude_code", side_effect=claude), \
             patch.object(graph_module, "run_opencode", side_effect=opencode):
            return build_graph(config).invoke(
                {
                    "task": "t",
                    "project_root": self.dir,
                    "run_store_enabled": False,
                    "config": config,
                    "agent_results": [],
                    "workspace": {"isolated": False, "path": self.dir, "reason": "test"},
                }
            )

    def test_a_red_gate_turns_a_verifier_pass_into_a_failure(self):
        config = _cfg(
            verification=_gate([PY, "-c", "import sys; print('tests failed'); sys.exit(1)"]),
            max_repair_attempts=0,
        )
        result = self._invoke(config, ["VERDICT: PASS\nlooks great to me"])

        self.assertEqual(result["verification_verdict"], "FAIL")
        self.assertEqual(result["status"], STATUS_FAILED)
        self.assertFalse(result["acceptance_checks"][0]["ok"])
        self.assertTrue(result["summary"]["acceptance_overrode_pass"])

    def test_a_green_gate_leaves_a_pass_alone(self):
        config = _cfg(verification=_gate([PY, "-c", "print('all good')"]))
        result = self._invoke(config, ["VERDICT: PASS"])

        self.assertEqual(result["status"], STATUS_COMPLETED)
        self.assertTrue(result["acceptance_checks"][0]["ok"])
        self.assertFalse(result["summary"]["acceptance_overrode_pass"])

    def test_the_verifier_is_shown_the_result_it_did_not_produce(self):
        config = _cfg(verification=_gate([PY, "-c", "import sys; print('E: broken'); sys.exit(2)"]))
        self._invoke(config, ["VERDICT: FAIL"])

        verifier_prompts = [p for kind, p in self.calls if kind == "verify"]
        self.assertTrue(verifier_prompts)
        self.assertIn("OBJECTIVE ACCEPTANCE CHECK", verifier_prompts[0])
        self.assertIn("E: broken", verifier_prompts[0])

    def test_the_repair_is_given_the_real_failure_output(self):
        config = _cfg(
            verification=_gate([PY, "-c", "import sys; print('AssertionError: 1 != 2'); sys.exit(1)"]),
            max_repair_attempts=1,
        )
        self._invoke(config, ["VERDICT: FAIL\nnot yet", "VERDICT: FAIL\nstill no"])

        repair_prompts = [p for kind, p in self.calls if kind == "impl"][1:]
        self.assertTrue(repair_prompts, "expected a repair attempt")
        self.assertIn("AssertionError: 1 != 2", repair_prompts[0])

    def test_the_gate_runs_again_after_every_repair(self):
        config = _cfg(
            verification=_gate([PY, "-c", "print('ok')"]),
            max_repair_attempts=1,
        )
        result = self._invoke(config, ["VERDICT: FAIL", "VERDICT: PASS"])

        generations = sorted(c["repair_attempts"] for c in result["acceptance_checks"])
        self.assertEqual(generations, [0, 1])

    def test_a_repair_that_fixes_the_code_turns_the_gate_green(self):
        # The whole loop, end to end: red gate vetoes a PASS, the repair is handed the real
        # failure, the fix lands, the gate goes green, and the PASS is allowed to stand.
        marker = os.path.join(self.dir, "fixed.txt")
        command = [
            PY,
            "-c",
            "import os, sys; sys.exit(0 if os.path.isfile('fixed.txt') else 1)",
        ]

        def fix():
            # Only repairs that were shown the gate output write the fix.
            repair_prompts = [p for kind, p in self.calls if kind == "impl"]
            if len(repair_prompts) > 1 and "OBJECTIVE ACCEPTANCE CHECK" in repair_prompts[-1]:
                with open(marker, "w", encoding="utf-8") as handle:
                    handle.write("fixed")

        config = _cfg(verification=_gate(command), max_repair_attempts=2)
        result = self._invoke(config, ["VERDICT: PASS", "VERDICT: PASS"], impl=fix)

        self.assertEqual(result["repair_attempts"], 1)
        self.assertEqual(result["status"], STATUS_COMPLETED)
        self.assertEqual(result["verification_verdict"], "PASS")
        gates = sorted((c["repair_attempts"], c["ok"]) for c in result["acceptance_checks"])
        self.assertEqual(gates, [(0, False), (1, True)])

    def test_no_command_leaves_the_pipeline_exactly_as_it_was(self):
        result = self._invoke(_cfg(), ["VERDICT: PASS"])
        self.assertEqual(result["status"], STATUS_COMPLETED)
        self.assertEqual(result.get("acceptance_checks", []), [])
        verifier_prompts = [p for kind, p in self.calls if kind == "verify"]
        self.assertNotIn("OBJECTIVE ACCEPTANCE CHECK", verifier_prompts[0])

    def test_a_gate_that_cannot_run_does_not_become_a_pass(self):
        config = _cfg(
            verification=_gate("definitely-not-a-real-binary"), max_repair_attempts=0
        )
        result = self._invoke(config, ["VERDICT: PASS"])

        self.assertEqual(result["verification_verdict"], "FAIL")
        self.assertIn("not found", result["acceptance_checks"][0]["error"])


class TestGateInStats(unittest.TestCase):
    """The gate makes the one comparison that judges a verifier."""

    def _run(self, run_id, verdict, gate_ok):
        return {
            "run_id": run_id,
            "status": "completed" if verdict == "PASS" else "failed",
            "verdict": verdict,
            "events": [
                {"event": "acceptance", "check": _check(ok=gate_ok)},
                {
                    "event": "verification",
                    "record": {"attempt": 1, "verdict": verdict, "repair_attempts": 0, "model": "sonnet"},
                },
            ],
        }

    def test_it_counts_a_pass_over_a_red_suite(self):
        runs = [
            self._run("a", "PASS", gate_ok=False),
            self._run("b", "PASS", gate_ok=True),
            self._run("c", "FAIL", gate_ok=False),
        ]
        report = compute_stats(runs)
        row = report["gate"]["by_verifier"][0]

        self.assertEqual(report["gate"]["runs_with_a_gate"], 3)
        self.assertEqual(row["judged"], 3)
        self.assertEqual(row["pass_over_red"], 1)
        self.assertAlmostEqual(row["pass_over_red_rate"], 33.3, places=1)
        self.assertIn("VERIFIER AGAINST THE OBJECTIVE CHECK", format_stats(report))

    def test_runs_without_a_gate_are_not_counted(self):
        report = compute_stats([
            {
                "run_id": "a",
                "status": "completed",
                "verdict": "PASS",
                "events": [{"event": "verification", "record": {"attempt": 1, "verdict": "PASS"}}],
            }
        ])
        self.assertEqual(report["gate"]["runs_with_a_gate"], 0)
        self.assertEqual(report["gate"]["by_verifier"], [])


# ===========================================================================
# Phase 1 - decomposition
# ===========================================================================

GOOD_PLAN = """
Here is how I would split this up.

```json
{
  "goal": "Add rate limiting to the API",
  "tasks": [
    {
      "id": "token-bucket",
      "title": "Add a token bucket implementation",
      "intent": "The shared primitive both middleware and tests need.",
      "acceptance": ["unit tests cover refill and burst"],
      "depends_on": [],
      "areas": ["api/limits.py"]
    },
    {
      "id": "middleware",
      "title": "Apply the limiter in middleware",
      "intent": "Enforce the limit per API key.",
      "acceptance": ["429 returned past the limit"],
      "depends_on": ["token-bucket"],
      "areas": ["api/middleware.py"],
      "risk": "needs a decision on the per-key quota"
    }
  ]
}
```
"""


class TestJsonExtraction(unittest.TestCase):
    def test_finds_a_fenced_json_block(self):
        self.assertIn('"tasks"', extract_json_block(GOOD_PLAN))

    def test_finds_bare_json_amid_prose(self):
        text = 'Sure. {"tasks": [{"title": "a"}]} Hope that helps.'
        self.assertEqual(extract_json_block(text), '{"tasks": [{"title": "a"}]}')

    def test_handles_braces_inside_strings(self):
        text = '{"tasks": [{"title": "use {braces} here"}]}'
        self.assertEqual(extract_json_block(text), text)

    def test_returns_none_when_there_is_no_json(self):
        self.assertIsNone(extract_json_block("I would not split this up at all."))
        self.assertIsNone(extract_json_block(""))


class TestPlanParsing(unittest.TestCase):
    def test_a_good_plan_parses_into_a_graph(self):
        plan = parse_task_plan(GOOD_PLAN, goal="Add rate limiting")

        self.assertTrue(plan_is_usable(plan))
        self.assertEqual([t["id"] for t in plan["tasks"]], ["token-bucket", "middleware"])
        self.assertEqual(plan["tasks"][1]["depends_on"], ["token-bucket"])
        self.assertEqual(plan["order"], [["token-bucket"], ["middleware"]])
        self.assertEqual(plan["tasks"][1]["risk"], "needs a decision on the per-key quota")

    def test_independent_tasks_share_a_wave(self):
        plan = parse_task_plan(
            '{"tasks": [{"title": "A"}, {"title": "B"}, '
            '{"title": "C", "depends_on": ["a"]}]}'
        )
        self.assertEqual(plan["order"][0], ["a", "b"])
        self.assertEqual(plan["order"][1], ["c"])

    def test_missing_json_is_an_error_not_a_crash(self):
        plan = parse_task_plan("I have decided not to answer in JSON.")
        self.assertFalse(plan_is_usable(plan))
        self.assertIn("no JSON object", plan["error"])

    def test_malformed_json_is_an_error_not_a_crash(self):
        plan = parse_task_plan('```json\n{"tasks": [ {"title": }]}\n```')
        self.assertFalse(plan_is_usable(plan))
        self.assertIn("could not be parsed", plan["error"])

    def test_an_empty_task_list_is_an_error(self):
        self.assertIn("no tasks", parse_task_plan('{"tasks": []}')["error"])

    def test_ids_are_generated_and_deduplicated(self):
        plan = parse_task_plan('{"tasks": [{"title": "Same Thing"}, {"title": "Same Thing"}]}')
        self.assertEqual([t["id"] for t in plan["tasks"]], ["same-thing", "same-thing-2"])

    def test_string_fields_are_accepted_where_lists_are_expected(self):
        plan = parse_task_plan('{"tasks": [{"title": "A", "acceptance": "it works"}]}')
        self.assertEqual(plan["tasks"][0]["acceptance"], ["it works"])

    def test_an_unknown_dependency_is_dropped_with_a_warning(self):
        plan = parse_task_plan('{"tasks": [{"title": "A", "depends_on": ["ghost"]}]}')
        self.assertEqual(plan["tasks"][0]["depends_on"], [])
        self.assertTrue(any("unknown task" in w for w in plan["warnings"]))
        self.assertTrue(plan_is_usable(plan))

    def test_a_self_dependency_is_dropped(self):
        plan = parse_task_plan('{"tasks": [{"id": "a", "title": "A", "depends_on": ["a"]}]}')
        self.assertEqual(plan["tasks"][0]["depends_on"], [])
        self.assertTrue(any("itself" in w for w in plan["warnings"]))

    def test_a_cycle_is_reported_rather_than_scheduled(self):
        plan = parse_task_plan(
            '{"tasks": [{"id": "a", "title": "A", "depends_on": ["b"]}, '
            '{"id": "b", "title": "B", "depends_on": ["a"]}]}'
        )
        self.assertFalse(plan_is_usable(plan))
        self.assertIn("cycle", plan["error"])

    def test_max_tasks_bounds_the_plan(self):
        many = ", ".join(f'{{"title": "T{i}"}}' for i in range(30))
        plan = parse_task_plan("{" + f'"tasks": [{many}]' + "}", max_tasks=5)
        self.assertEqual(len(plan["tasks"]), 5)
        self.assertTrue(any("keeping the first 5" in w for w in plan["warnings"]))

    def test_a_non_object_payload_is_an_error(self):
        self.assertIn("not a JSON object", normalize_plan(["a", "b"])["error"])

    def test_waves_of_an_empty_plan_are_empty(self):
        waves, error = topological_waves([])
        self.assertEqual(waves, [])
        self.assertIsNone(error)

    def test_the_rendered_plan_shows_the_execution_order(self):
        text = format_plan(parse_task_plan(GOOD_PLAN))
        self.assertIn("[token-bucket]", text)
        self.assertIn("after:      token-bucket", text)
        self.assertIn("EXECUTION ORDER", text)
        self.assertIn("Wave 1", text)

    def test_parallel_waves_are_called_out(self):
        text = format_plan(parse_task_plan('{"tasks": [{"title": "A"}, {"title": "B"}]}'))
        self.assertIn("can run in parallel", text)


class TestPlanningConfig(unittest.TestCase):
    def test_it_inherits_the_planner(self):
        config = _cfg()
        resolved = get_planning_config(config)
        self.assertEqual(resolved["agent"], "claude")
        self.assertEqual(resolved["model"], "sonnet")

    def test_an_explicit_choice_wins(self):
        config = _cfg(planning={"agent": "claude", "model": "opus", "max_tasks": 4})
        resolved = get_planning_config(config)
        self.assertEqual(resolved["model"], "opus")
        self.assertEqual(resolved["max_tasks"], 4)

    def test_a_model_from_another_provider_is_not_inherited(self):
        config = _cfg(
            [{"agent": "opencode", "model": "opencode/gpt-5.1-codex", "role": "planner"}],
            planning={"agent": "claude"},
        )
        self.assertIsNone(get_planning_config(config)["model"])

    def test_an_unknown_planning_model_is_rejected(self):
        with self.assertRaises(ConfigValidationError):
            _cfg(planning={"agent": "claude", "model": "not-a-model"})

    def test_max_tasks_must_be_positive(self):
        with self.assertRaises(ConfigValidationError):
            _cfg(planning={"max_tasks": 0})


class TestPlanGraph(unittest.TestCase):
    """--plan-only produces a plan and launches nothing else."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="orch_plan_")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _invoke(self, output=GOOD_PLAN, config=None):
        seen = {}

        def claude(prompt, **kwargs):
            seen["role"] = kwargs.get("role")
            seen["prompt"] = prompt
            seen["model"] = kwargs.get("model")
            return output

        config = config or _cfg()
        with patch.object(graph_module, "run_antigravity") as antigravity, \
             patch.object(graph_module, "run_claude_code", side_effect=claude), \
             patch.object(graph_module, "run_opencode") as opencode:
            result = build_plan_graph(config).invoke(
                {
                    "task": "Add rate limiting to the API",
                    "project_root": self.dir,
                    "run_store_enabled": False,
                    "config": config,
                    "agent_results": [],
                    "workspace": {"isolated": False, "path": self.dir, "reason": "test"},
                }
            )
        return result, seen, antigravity, opencode

    def test_it_decomposes_without_implementing(self):
        result, seen, antigravity, opencode = self._invoke()

        antigravity.assert_not_called()
        opencode.assert_not_called()
        self.assertEqual(seen["role"], "decomposer")
        self.assertEqual(len(result["task_plan"]["tasks"]), 2)
        self.assertEqual(result["status"], STATUS_PLANNED)

    def test_the_decomposer_is_told_the_task_ceiling(self):
        _, seen, _, _ = self._invoke(config=_cfg(planning={"max_tasks": 3}))
        self.assertIn("between 1 and 3 tasks", seen["prompt"])

    def test_it_uses_the_configured_planning_model(self):
        _, seen, _, _ = self._invoke(config=_cfg(planning={"agent": "claude", "model": "opus"}))
        self.assertEqual(seen["model"], "opus")

    def test_an_unparseable_reply_is_reported_not_crashed(self):
        result, _, _, _ = self._invoke(output="I would rather not.")
        self.assertFalse(plan_is_usable(result["task_plan"]))
        self.assertIn("no JSON object", result["task_plan"]["error"])

    def test_the_plan_reaches_the_run_summary(self):
        result, _, _, _ = self._invoke()
        self.assertEqual(result["summary"]["task_plan"]["tasks"], 2)
        self.assertEqual(result["summary"]["task_plan"]["waves"], 2)


class TestPlanStorage(unittest.TestCase):
    """A plan is a durable fact, recorded like any other."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="orch_planstore_")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_the_plan_is_written_to_the_run_store(self):
        from orchestrator.store import load_run

        config = _cfg()
        with patch.object(graph_module, "run_claude_code", return_value=GOOD_PLAN):
            result = build_plan_graph(config).invoke(
                {
                    "task": "Add rate limiting",
                    "project_root": self.dir,
                    "config": config,
                    "agent_results": [],
                    "workspace": {"isolated": False, "path": self.dir, "reason": "test"},
                }
            )

        run = load_run(self.dir, result["run_id"])
        plans = [e for e in run["events"] if e.get("event") == "task_plan"]
        self.assertEqual(len(plans), 1)
        self.assertEqual(len(plans[0]["plan"]["tasks"]), 2)


if __name__ == "__main__":
    unittest.main()
