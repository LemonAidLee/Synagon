"""Unit and integration tests for the LangGraph orchestrator workflow."""

import os
import shutil
import unittest
from unittest.mock import patch, MagicMock

from orchestrator.config import DEFAULT_CONFIG
from orchestrator.graph import build_graph
from orchestrator.tracer import default_tracer

from tests.support import (
    RunStoreGuard,
    isolated_graph_state,
    redirected_run_store,
    temporary_store_dir,
)


class TestWorkflow(unittest.TestCase):
    """Test suite for the LangGraph Context -> Antigravity -> Claude Code pipeline."""

    def setUp(self):
        default_tracer.clear()

    @patch("orchestrator.graph.run_opencode")
    @patch("orchestrator.graph.run_claude_code")
    @patch("orchestrator.graph.run_antigravity")
    def test_workflow_mock_execution(self, mock_antigravity, mock_claude, mock_opencode):
        """Test complete workflow state propagation and prompt enrichment across all 4 agents."""
        mock_antigravity.return_value = "Antigravity analysis report grounded in project context."
        planner_text = "Claude Code review: analysis verified against supplied context."
        verifier_text = "VERDICT: PASS\n\nSummary:\nImplementation satisfies all requirements.\n\nTests:\nPASS"
        mock_claude.side_effect = [planner_text, verifier_text]
        mock_opencode.return_value = "OpenCode implementation: applied planned changes cleanly in workspace."

        task = "Review architecture and dependency structure"
        initial_state = {
            "task": task,
            "project_root": os.getcwd(), "config": DEFAULT_CONFIG,
            "run_store_enabled": False,
            "workspace": {"isolated": False, "path": os.getcwd()},
        }

        result = build_graph(DEFAULT_CONFIG).invoke(initial_state)

        # 1. Verify LangGraph state fields
        self.assertEqual(result["status"], "completed")
        self.assertIsNone(result.get("error"))
        self.assertTrue(bool(result.get("project_root")), "project_root must be set in state")
        self.assertTrue(bool(result.get("project_context")), "project_context must be set in state")
        self.assertIn("orchestrator", result["project_context"])

        self.assertEqual(result["verification_verdict"], "PASS")

        # 2. Verify structured agent_results list contains all 4 results
        agent_results = result.get("agent_results")
        self.assertIsInstance(agent_results, list)
        self.assertEqual(len(agent_results), 4)

        # First result: Antigravity as researcher
        agy_res = agent_results[0]
        self.assertEqual(agy_res["agent"], "antigravity")
        self.assertEqual(agy_res["role"], "researcher")
        self.assertEqual(agy_res["status"], "success")
        self.assertEqual(agy_res["output"], "Antigravity analysis report grounded in project context.")
        self.assertEqual(agy_res["model"], "gemini-3.8-flash-high")
        self.assertGreaterEqual(agy_res["duration_seconds"], 0.0)

        # Second result: Claude Code as planner
        claude_res = agent_results[1]
        self.assertEqual(claude_res["agent"], "claude")
        self.assertEqual(claude_res["role"], "planner")
        self.assertEqual(claude_res["status"], "success")
        self.assertEqual(claude_res["output"], planner_text)
        self.assertEqual(claude_res["model"], "sonnet")
        self.assertGreaterEqual(claude_res["duration_seconds"], 0.0)

        # Third result: OpenCode as implementer
        opencode_res = agent_results[2]
        self.assertEqual(opencode_res["agent"], "opencode")
        self.assertEqual(opencode_res["role"], "implementer")
        self.assertEqual(opencode_res["status"], "success")
        self.assertEqual(opencode_res["output"], "OpenCode implementation: applied planned changes cleanly in workspace.")
        self.assertEqual(opencode_res["model"], "opencode/gpt-5.1-codex")
        self.assertGreaterEqual(opencode_res["duration_seconds"], 0.0)

        # Fourth result: Claude Code as verifier
        verifier_res = agent_results[3]
        self.assertEqual(verifier_res["agent"], "claude")
        self.assertEqual(verifier_res["role"], "verifier")
        self.assertEqual(verifier_res["status"], "success")
        self.assertEqual(verifier_res["output"], verifier_text)
        self.assertEqual(verifier_res["model"], "sonnet")
        self.assertEqual(verifier_res["verdict"], "PASS")
        self.assertGreaterEqual(verifier_res["duration_seconds"], 0.0)

        # 3. Verify Antigravity received task + project context + role instructions + model
        mock_antigravity.assert_called_once()
        antigravity_call_args = mock_antigravity.call_args
        antigravity_prompt = antigravity_call_args[0][0]
        self.assertEqual(antigravity_call_args.kwargs.get("model"), "gemini-3.8-flash-high")
        self.assertIn(task, antigravity_prompt)
        self.assertIn("researcher", antigravity_prompt)
        self.assertIn("SUPPLIED PROJECT CONTEXT", antigravity_prompt)
        self.assertIn(result["project_context"], antigravity_prompt)

        # 4. Verify Claude Code planner received task + analysis + project context + role instructions + model
        self.assertEqual(mock_claude.call_count, 2)
        claude_call_args = mock_claude.call_args_list[0]
        claude_prompt = claude_call_args[0][0]
        self.assertEqual(claude_call_args.kwargs.get("model"), "sonnet")
        self.assertIn(task, claude_prompt)
        self.assertIn("planner", claude_prompt)
        self.assertIn("RESEARCHER FINDINGS", claude_prompt)
        self.assertIn("Antigravity analysis report grounded in project context.", claude_prompt)
        self.assertIn("PROJECT CONTEXT", claude_prompt)
        self.assertIn(result["project_context"], claude_prompt)

        # 5. Verify OpenCode received task + context + researcher findings + planner recommendations + responsibility + model
        mock_opencode.assert_called_once()
        opencode_call_args = mock_opencode.call_args
        opencode_prompt = opencode_call_args[0][0]
        self.assertEqual(opencode_call_args.kwargs.get("model"), "opencode/gpt-5.1-codex")
        self.assertIn(task, opencode_prompt)
        self.assertIn("implementer", opencode_prompt)
        self.assertIn("ORIGINAL USER TASK", opencode_prompt)
        self.assertIn("PROJECT CONTEXT", opencode_prompt)
        self.assertIn("RESEARCHER FINDINGS", opencode_prompt)
        self.assertIn(agy_res["output"], opencode_prompt)
        self.assertIn("PLANNER RECOMMENDATIONS", opencode_prompt)
        self.assertIn(claude_res["output"], opencode_prompt)

        # 6. Verify Claude Code verifier received cumulative context + implementation + responsibility
        verifier_call_args = mock_claude.call_args_list[1]
        verifier_prompt = verifier_call_args[0][0]
        self.assertEqual(verifier_call_args.kwargs.get("model"), "sonnet")
        self.assertIn(task, verifier_prompt)
        self.assertIn("verifier", verifier_prompt)
        self.assertIn("ORIGINAL USER TASK", verifier_prompt)
        self.assertIn("PROJECT CONTEXT", verifier_prompt)
        self.assertIn("RESEARCHER FINDINGS", verifier_prompt)
        self.assertIn(agy_res["output"], verifier_prompt)
        self.assertIn("PLANNER RECOMMENDATIONS", verifier_prompt)
        self.assertIn(claude_res["output"], verifier_prompt)
        self.assertIn("IMPLEMENTER RESULT", verifier_prompt)
        self.assertIn(opencode_res["output"], verifier_prompt)

    @patch("orchestrator.graph.run_opencode")
    @patch("orchestrator.graph.run_claude_code")
    @patch("orchestrator.graph.run_antigravity")
    def test_execution_trace_event_sequence(self, mock_antigravity, mock_claude, mock_opencode):
        """Verify that observable execution trace events occur in the exact expected sequence with role metadata."""
        mock_antigravity.return_value = "Mock analysis"
        mock_claude.side_effect = ["Mock review", "VERDICT: PASS\nAll verified."]
        mock_opencode.return_value = "Mock implementation"

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

        # Verify agent, role, and model metadata on events
        agy_start = next(e for e in events if e.name == "antigravity_started")
        self.assertEqual(agy_start.agent, "antigravity")
        self.assertEqual(agy_start.role, "researcher")
        self.assertEqual(agy_start.model, "gemini-3.8-flash-high")

        claude_start = next(e for e in events if e.name == "claude_started" and e.role == "planner")
        self.assertEqual(claude_start.agent, "claude")
        self.assertEqual(claude_start.role, "planner")
        self.assertEqual(claude_start.model, "sonnet")

        opencode_start = next(e for e in events if e.name == "opencode_started")
        self.assertEqual(opencode_start.agent, "opencode")
        self.assertEqual(opencode_start.role, "implementer")
        self.assertEqual(opencode_start.model, "opencode/gpt-5.1-codex")

        verifier_start = next(e for e in events if e.name == "claude_started" and e.role == "verifier")
        self.assertEqual(verifier_start.agent, "claude")
        self.assertEqual(verifier_start.role, "verifier")
        self.assertEqual(verifier_start.model, "sonnet")

    @patch("orchestrator.graph.run_antigravity")
    def test_workflow_failure_produces_safe_error(self, mock_antigravity):
        """Test safe error handling when an agent node fails."""
        mock_antigravity.side_effect = RuntimeError("Simulated CLI failure")

        initial_state = {"task": "Test error handling", "project_root": os.getcwd(), "config": DEFAULT_CONFIG, "run_store_enabled": False, "workspace": {"isolated": False, "path": os.getcwd()}}
        result = build_graph(DEFAULT_CONFIG).invoke(initial_state)

        self.assertEqual(result["status"], "error")
        self.assertIn("antigravity (researcher) node error", result["error"])

        # Check structured agent_results contains the error result
        results = result.get("agent_results", [])
        self.assertTrue(len(results) >= 1)
        error_res = results[-1]
        self.assertEqual(error_res["agent"], "antigravity")
        self.assertEqual(error_res["role"], "researcher")
        self.assertEqual(error_res["status"], "error")

        events = default_tracer.get_events()
        failed_events = [e for e in events if e.status == "failed"]
        self.assertTrue(len(failed_events) >= 1)
        self.assertEqual(failed_events[-1].agent, "antigravity")
        self.assertEqual(failed_events[-1].role, "researcher")

    def test_invalid_project_root_handled_cleanly(self):
        """Test that invalid project root path is handled cleanly without crashing."""
        initial_state = {
            "task": "Test invalid root",
            "project_root": "Z:\\non\\existent\\directory\\path\\12345",
        }
        result = build_graph(DEFAULT_CONFIG).invoke(initial_state)

        self.assertEqual(result["status"], "error")
        self.assertIn("Project context collection error", result["error"])


    @patch("orchestrator.graph.run_opencode")
    @patch("orchestrator.graph.run_claude_code")
    @patch("orchestrator.graph.run_antigravity")
    def test_workflow_opencode_failure_produces_safe_error(self, mock_antigravity, mock_claude, mock_opencode):
        """Test safe error handling when OpenCode implementer node fails."""
        mock_antigravity.return_value = "Mock analysis"
        mock_claude.return_value = "Mock review"
        mock_opencode.side_effect = RuntimeError("Simulated OpenCode failure")

        initial_state = {"task": "Test OpenCode error handling", "project_root": os.getcwd(), "config": DEFAULT_CONFIG, "run_store_enabled": False, "workspace": {"isolated": False, "path": os.getcwd()}}
        result = build_graph(DEFAULT_CONFIG).invoke(initial_state)

        self.assertEqual(result["status"], "error")
        self.assertIn("opencode (implementer) node error", result["error"])

        results = result.get("agent_results", [])
        self.assertEqual(len(results), 3)
        self.assertEqual(results[0]["agent"], "antigravity")
        self.assertEqual(results[0]["status"], "success")
        self.assertEqual(results[1]["agent"], "claude")
        self.assertEqual(results[1]["status"], "success")
        self.assertEqual(results[2]["agent"], "opencode")
        self.assertEqual(results[2]["status"], "error")

        events = default_tracer.get_events()
        failed_events = [e for e in events if e.status == "failed"]
        self.assertTrue(len(failed_events) >= 1)
        self.assertEqual(failed_events[-1].agent, "opencode")
        self.assertEqual(failed_events[-1].role, "implementer")

    @patch("orchestrator.graph.run_opencode")
    @patch("orchestrator.graph.run_claude_code")
    @patch("orchestrator.graph.run_antigravity")
    def test_workflow_visible_terminals_parameter_propagation(self, mock_antigravity, mock_claude, mock_opencode):
        """Verify visible=True and correct window titles are passed to all agent functions."""
        mock_antigravity.return_value = "Mock analysis"
        mock_claude.side_effect = ["Mock review", "VERDICT: PASS\nAll verified."]
        mock_opencode.return_value = "Mock implementation"

        initial_state = {
            "task": "Test visible terminal parameters",
            "project_root": os.getcwd(), "config": DEFAULT_CONFIG,
            "run_store_enabled": False,
            "workspace": {"isolated": False, "path": os.getcwd()},
            "visible_terminals": True,
            "pause_on_completion": 0.0,
        }
        result = build_graph(DEFAULT_CONFIG).invoke(initial_state)

        self.assertEqual(result["status"], "completed")

        # Verify Antigravity call kwargs
        agy_call = mock_antigravity.call_args
        self.assertTrue(agy_call.kwargs.get("visible"))
        self.assertEqual(agy_call.kwargs.get("title"), "LangGraph - Antigravity Researcher")

        # Verify Claude Planner call kwargs
        planner_call = mock_claude.call_args_list[0]
        self.assertTrue(planner_call.kwargs.get("visible"))
        self.assertEqual(planner_call.kwargs.get("title"), "LangGraph - Claude Planner")

        # Verify OpenCode Implementer call kwargs
        opencode_call = mock_opencode.call_args
        self.assertTrue(opencode_call.kwargs.get("visible"))
        self.assertEqual(opencode_call.kwargs.get("title"), "LangGraph - OpenCode Implementer")

        # Verify Claude Verifier call kwargs
        verifier_call = mock_claude.call_args_list[1]
        self.assertTrue(verifier_call.kwargs.get("visible"))
        self.assertEqual(verifier_call.kwargs.get("title"), "LangGraph - Claude Verifier")



class TestLiveWorkflow(unittest.TestCase):
    """Integration test suite for live CLI execution (skipped by default in unit runs)."""

    @unittest.skipUnless(os.environ.get("RUN_LIVE_TESTS") == "1", "Live test requires RUN_LIVE_TESTS=1")
    def test_workflow_live_execution(self):
        """Live end-to-end test invoking Antigravity CLI -> LangGraph -> Claude Code CLI -> OpenCode CLI -> Claude Verifier."""
        initial_state = {
            "task": "Reply in 1 sentence: What is the primary benefit of type hints in Python?",
            "project_root": os.getcwd(), "config": DEFAULT_CONFIG,
            "run_store_enabled": False,
            "workspace": {"isolated": False, "path": os.getcwd()},
        }
        result = build_graph(DEFAULT_CONFIG).invoke(initial_state)

        self.assertEqual(result["status"], "completed", f"Workflow failed: {result.get('error')}")
        self.assertTrue(bool(result.get("agent_results")), "agent_results should not be empty")
        self.assertEqual(len(result["agent_results"]), 4)
        self.assertTrue(bool(result.get("analysis")), "Antigravity analysis should not be empty")
        self.assertTrue(bool(result.get("review")), "Claude review should not be empty")
        self.assertTrue(bool(result.get("implementation")), "OpenCode implementation should not be empty")
        self.assertTrue(bool(result.get("verification")), "Claude verification should not be empty")
        self.assertIn(result.get("verification_verdict"), ["PASS", "FAIL"])

    @unittest.skipUnless(os.environ.get("RUN_LIVE_TESTS") == "1", "Live test requires RUN_LIVE_TESTS=1")
    def test_workflow_live_execution_visible_terminals(self):
        """Live end-to-end test with visible terminals enabled."""
        initial_state = {
            "task": "Reply in 1 sentence: What is the primary benefit of type hints in Python?",
            "project_root": os.getcwd(), "config": DEFAULT_CONFIG,
            "run_store_enabled": False,
            "workspace": {"isolated": False, "path": os.getcwd()},
            "visible_terminals": True,
            "pause_on_completion": 0.5,
        }
        result = build_graph(DEFAULT_CONFIG).invoke(initial_state)

        self.assertEqual(result["status"], "completed", f"Workflow failed: {result.get('error')}")
        self.assertTrue(bool(result.get("agent_results")), "agent_results should not be empty")
        self.assertEqual(len(result["agent_results"]), 4)
        self.assertIn(result.get("verification_verdict"), ["PASS", "FAIL"])



if __name__ == "__main__":
    unittest.main()


class TestTheSuiteStaysOutOfTheProject(unittest.TestCase):
    """The suite must not write into the run store of the project it is testing.

    This is a regression guard rather than a feature test. The run store is what `--stats`
    analyses, what the board projects, and what the planner's memory reads, so a test that
    appends to it does not merely leave litter: it changes what the orchestrator believes
    about this repository. One checkout reached 141 suite artifacts against 16 real runs
    before anyone noticed, because nothing was watching.
    """

    def setUp(self):
        default_tracer.clear()

    @patch("orchestrator.graph.run_opencode")
    @patch("orchestrator.graph.run_claude_code")
    @patch("orchestrator.graph.run_antigravity")
    def test_running_the_graph_writes_no_run_into_the_real_store(
        self, mock_antigravity, mock_claude, mock_opencode
    ):
        mock_antigravity.return_value = "Mock analysis"
        mock_claude.side_effect = ["Mock review", "VERDICT: PASS\nAll verified."]
        mock_opencode.return_value = "Mock implementation"

        store_dir = temporary_store_dir()
        self.addCleanup(shutil.rmtree, store_dir, ignore_errors=True)
        config = redirected_run_store(DEFAULT_CONFIG, store_dir)

        with RunStoreGuard(self):
            build_graph(config).invoke(
                isolated_graph_state(DEFAULT_CONFIG, "Guarded run", store_dir)
            )

    @patch("orchestrator.graph.run_opencode")
    @patch("orchestrator.graph.run_claude_code")
    @patch("orchestrator.graph.run_antigravity")
    def test_the_redirected_store_really_did_record_the_run(
        self, mock_antigravity, mock_claude, mock_opencode
    ):
        """The guard above would also pass if the store had simply been switched off, so
        prove the run was recorded — somewhere else."""
        from tests.support import read_runs

        mock_antigravity.return_value = "Mock analysis"
        mock_claude.side_effect = ["Mock review", "VERDICT: PASS\nAll verified."]
        mock_opencode.return_value = "Mock implementation"

        store_dir = temporary_store_dir()
        self.addCleanup(shutil.rmtree, store_dir, ignore_errors=True)
        config = redirected_run_store(DEFAULT_CONFIG, store_dir)
        build_graph(config).invoke(
            isolated_graph_state(DEFAULT_CONFIG, "Redirected run", store_dir)
        )

        records = read_runs(store_dir)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["task"], "Redirected run")
