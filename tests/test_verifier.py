"""Unit and integration tests for the Claude Code Verifier integration."""

import os
import shutil
import unittest
from unittest.mock import patch, MagicMock

from orchestrator.agents.verifier import (
    parse_verdict,
    run_verifier,
)
from orchestrator.prompts import build_verifier_prompt
from orchestrator.config import DEFAULT_CONFIG
from orchestrator.graph import build_graph
from orchestrator.tracer import default_tracer

from tests.support import (
    isolated_graph_state,
    redirected_run_store,
    temporary_store_dir,
)


class TestVerifierUnit(unittest.TestCase):
    """Unit test suite for verifier parsing, prompt construction, and adapter execution."""

    def test_pass_parsing(self):
        """Test 1: Verify unambiguous PASS parsing across formatting variations."""
        self.assertEqual(parse_verdict("VERDICT: PASS"), "PASS")
        self.assertEqual(parse_verdict("VERDICT: pass"), "PASS")
        self.assertEqual(parse_verdict("**VERDICT:** PASS\nSummary: All tests pass."), "PASS")
        self.assertEqual(parse_verdict("### VERDICT: PASS"), "PASS")
        self.assertEqual(parse_verdict("Verdict:   PASS"), "PASS")

    def test_fail_parsing(self):
        """Test 2: Verify unambiguous FAIL parsing across formatting variations."""
        self.assertEqual(parse_verdict("VERDICT: FAIL"), "FAIL")
        self.assertEqual(parse_verdict("VERDICT: fail"), "FAIL")
        self.assertEqual(parse_verdict("**VERDICT:** FAIL\nSummary: 2 tests failed."), "FAIL")
        self.assertEqual(parse_verdict("### VERDICT: FAIL"), "FAIL")
        self.assertEqual(parse_verdict("Verdict:   FAIL"), "FAIL")

    def test_missing_or_ambiguous_verdict_returns_unknown(self):
        """Test 3: Missing, empty, or ambiguous verdicts MUST return UNKNOWN and never default to PASS."""
        self.assertEqual(parse_verdict("The implementation seems fine overall."), "UNKNOWN")
        self.assertEqual(parse_verdict(""), "UNKNOWN")
        self.assertEqual(parse_verdict(None), "UNKNOWN")
        self.assertEqual(parse_verdict("I think this is a pass maybe."), "UNKNOWN")
        self.assertEqual(parse_verdict("FAILED test"), "UNKNOWN")

    def test_verifier_receives_cumulative_context(self):
        """Test 4: Verify that the verifier prompt builder includes all required context."""
        task = "Add a greeting unit test"
        project_context = "Files: hello.py, README.md"
        research = "Observed standalone script hello.py without tests."
        plan = "Create tests/test_hello.py with unittest.TestCase."
        implementation = "Created tests/test_hello.py and ran tests successfully."
        responsibility = "Verify the implementation against criteria and return PASS or FAIL."

        prompt = build_verifier_prompt(
            task=task,
            project_context=project_context,
            research_text=research,
            plan_text=plan,
            implementation_text=implementation,
            responsibility=responsibility,
            role_name="verifier",
        )

        self.assertIn("ORIGINAL USER TASK:\n" + task, prompt)
        self.assertIn("PROJECT CONTEXT:\n" + project_context, prompt)
        self.assertIn("RESEARCHER FINDINGS:\n" + research, prompt)
        self.assertIn("PLANNER RECOMMENDATIONS:\n" + plan, prompt)
        self.assertIn("IMPLEMENTER RESULT:\n" + implementation, prompt)
        self.assertIn("VERIFICATION INSTRUCTIONS:", prompt)
        self.assertIn("VERDICT: PASS", prompt)
        self.assertIn("VERDICT: FAIL", prompt)
        self.assertIn(responsibility, prompt)

    @patch("orchestrator.agents.verifier.run_claude_code")
    def test_run_verifier_invokes_claude_adapter(self, mock_claude):
        """Verify run_verifier passes arguments to run_claude_code."""
        mock_claude.return_value = "VERDICT: PASS\nSummary: Verified."
        res = run_verifier("Verify this", working_dir="/test/dir", model="sonnet")

        mock_claude.assert_called_once_with(
            prompt="Verify this",
            timeout=180,
            working_dir="/test/dir",
            extra_args=None,
            model="sonnet",
        )
        self.assertEqual(res, "VERDICT: PASS\nSummary: Verified.")


class TestVerifierWorkflow(unittest.TestCase):
    """Workflow integration tests verifying the 4-agent graph execution with Claude Verifier."""

    def setUp(self):
        default_tracer.clear()

    @patch("orchestrator.graph.run_claude_code")
    @patch("orchestrator.graph.run_opencode")
    @patch("orchestrator.graph.run_antigravity")
    def test_verifier_agent_result_and_pass_termination(
        self, mock_antigravity, mock_opencode, mock_claude
    ):
        """Test 5 & 7: Verify verifier AgentResult, verdict=PASS, and termination at END."""
        mock_antigravity.return_value = "Antigravity research findings."
        mock_opencode.return_value = "OpenCode implementation output."

        # Claude is called twice: 1st for planner, 2nd for verifier
        planner_output = "Claude Code planning recommendations."
        verifier_output = "VERDICT: PASS\n\nSummary:\nImplementation satisfies all requirements.\n\nTests:\nPASS"
        mock_claude.side_effect = [planner_output, verifier_output]

        initial_state = {
            "task": "Implement feature X and verify",
            "project_root": os.getcwd(), "config": DEFAULT_CONFIG,
            "run_store_enabled": False,
            "workspace": {"isolated": False, "path": os.getcwd()},
        }
        result = build_graph(DEFAULT_CONFIG).invoke(initial_state)

        # Graph completes
        self.assertEqual(result["status"], "completed")
        self.assertIsNone(result.get("error"))

        self.assertEqual(result["verification_verdict"], "PASS")

        # Verify 4 accumulated AgentResults
        agent_results = result.get("agent_results", [])
        self.assertEqual(len(agent_results), 4)

        # Result 1: researcher
        self.assertEqual(agent_results[0]["agent"], "antigravity")
        self.assertEqual(agent_results[0]["role"], "researcher")

        # Result 2: planner
        self.assertEqual(agent_results[1]["agent"], "claude")
        self.assertEqual(agent_results[1]["role"], "planner")

        # Result 3: implementer
        self.assertEqual(agent_results[2]["agent"], "opencode")
        self.assertEqual(agent_results[2]["role"], "implementer")

        # Result 4: verifier (Test 5)
        verifier_res = agent_results[3]
        self.assertEqual(verifier_res["agent"], "claude")
        self.assertEqual(verifier_res["role"], "verifier")
        self.assertEqual(verifier_res["model"], "sonnet")
        self.assertEqual(verifier_res["status"], "success")
        self.assertEqual(verifier_res["output"], verifier_output)
        self.assertEqual(verifier_res["verdict"], "PASS")
        self.assertGreaterEqual(verifier_res["duration_seconds"], 0.0)

    @patch("orchestrator.graph.run_claude_code")
    @patch("orchestrator.graph.run_opencode")
    @patch("orchestrator.graph.run_antigravity")
    def test_verifier_fail_terminates_at_end(
        self, mock_antigravity, mock_opencode, mock_claude
    ):
        """Test 8: Verify verifier FAIL verdict terminates at END without looping."""
        mock_antigravity.return_value = "Antigravity research findings."
        mock_opencode.return_value = "OpenCode implementation output."

        planner_output = "Claude Code planning recommendations."
        verifier_output = "VERDICT: FAIL\n\nSummary:\nTests failed.\n\nRequired Fixes:\nFix syntax error."
        mock_claude.side_effect = [planner_output, verifier_output]

        initial_state = {
            "task": "Implement feature Y and verify",
            "project_root": os.getcwd(), "config": DEFAULT_CONFIG,
            "run_store_enabled": False,
            "workspace": {"isolated": False, "path": os.getcwd()},
            "max_repair_attempts": 0,
        }
        result = build_graph(DEFAULT_CONFIG).invoke(initial_state)

        # Graph completes with failed status when max_repair_attempts=0
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["verification_verdict"], "FAIL")

        agent_results = result.get("agent_results", [])
        self.assertEqual(len(agent_results), 4)
        self.assertEqual(agent_results[3]["verdict"], "FAIL")

        # Verify Claude was called exactly 2 times (no retry loop)
        self.assertEqual(mock_claude.call_count, 2)
        self.assertEqual(mock_opencode.call_count, 1)

    @patch("orchestrator.graph.run_claude_code")
    @patch("orchestrator.graph.run_opencode")
    @patch("orchestrator.graph.run_antigravity")
    def test_graph_ordering_and_trace_events(
        self, mock_antigravity, mock_opencode, mock_claude
    ):
        """Test 6: Verify context -> antigravity -> claude -> opencode -> verifier event order."""
        mock_antigravity.return_value = "Mock research"
        mock_opencode.return_value = "Mock implementation"
        mock_claude.side_effect = ["Mock plan", "VERDICT: PASS\nAll good."]

        # The run store stays enabled - the sequence asserted below names its events -
        # but it is redirected, so the suite does not append a run to the developer's own
        # project every time it runs. See tests/support.py.
        store_dir = temporary_store_dir()
        self.addCleanup(shutil.rmtree, store_dir, ignore_errors=True)
        config = redirected_run_store(DEFAULT_CONFIG, store_dir)
        initial_state = isolated_graph_state(DEFAULT_CONFIG, "Verify trace sequence", store_dir)
        build_graph(config).invoke(initial_state)

        events = default_tracer.get_events()
        event_names = [e.name for e in events]

        expected_sequence = [
            "context_started",
            "context_completed",
            "run_store_opened",
            "preflight_started",
            "preflight_completed",
            "antigravity_started",
            "antigravity_completed",
            "handoff_to_claude",
            "claude_started",
            "claude_completed",
            "handoff_to_opencode",
            "opencode_started",
            "opencode_completed",
            "handoff_to_verifier",
            "claude_started",
            "verifier_completed",
            "workflow_completed",
            "run_store_closed",
        ]
        self.assertEqual(event_names, expected_sequence)


class TestLiveVerifier(unittest.TestCase):
    """Integration test suite for live Claude verifier execution (skipped by default in unit runs)."""

    @unittest.skipUnless(os.environ.get("RUN_LIVE_TESTS") == "1", "Live test requires RUN_LIVE_TESTS=1")
    def test_live_verifier_execution(self):
        """Live smoke test verifying Claude CLI invocation, verifier prompt handling, and verdict parsing."""
        prompt = (
            "You are acting as the verifier in an AI orchestrator.\n"
            "Task: Add a hello world function.\n"
            "Implementation: def hello(): return 'hello world'\n"
            "Evaluate this implementation and respond with either:\n"
            "VERDICT: PASS\n"
            "or\n"
            "VERDICT: FAIL\n"
            "Summary: Brief summary."
        )
        response = run_verifier(prompt, timeout=60, model="sonnet")
        self.assertTrue(bool(response), "Verifier response should not be empty")

        verdict = parse_verdict(response)
        self.assertIn(verdict, ["PASS", "FAIL"], f"Expected PASS or FAIL, got '{verdict}'")


if __name__ == "__main__":
    unittest.main()
