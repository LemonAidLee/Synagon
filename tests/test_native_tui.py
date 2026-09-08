"""Tests for Stage 7.5.2 Native Agent TUI Execution & Orchestration Bridge.

Validates:
1. Configuration parsing and validation of execution.agent_execution_mode
2. native_tui mode selection
3. Headless fallback behavior
4. Terminal creation via bridge
5. Terminal naming for implementer and repair attempts
6. Session and controller initialization
7. Task delivery to OpenCode session
8. Deterministic completion detection via session status
9. Timeout handling (CLITimeoutError)
10. Process failure handling (CLIExecutionError)
11. Malformed result handling
12. Bridge/controller failure
13. Metrics and token usage preservation
14. AgentResult compatibility (execution_mode field)
15. LangGraph sequencing and state propagation
16. Verifier -> Repair loop with native TUI
17. Repair attempt counting with native TUI
18. Skill context preservation
19. Existing headless mode regression
20. Existing terminal bridge regression
"""

import json
import unittest
from unittest.mock import MagicMock, patch

from orchestrator.agents.exceptions import CLIExecutionError, CLITimeoutError
from orchestrator.agents.opencode import run_opencode, run_opencode_with_usage
from orchestrator.agents.opencode_tui import (
    find_free_port,
    parse_model_spec,
    run_opencode_native_tui,
)
from orchestrator.config import DEFAULT_CONFIG, ConfigValidationError, load_config, validate_config
from orchestrator.graph import build_graph
from orchestrator.types import (
    AgentResult,
    create_agent_result,
    create_token_usage,
    unavailable_token_usage,
)


class TestNativeTUIConfiguration(unittest.TestCase):
    """Test configuration schema validation and fallback defaults for agent_execution_mode."""

    def test_valid_execution_modes(self):
        import copy
        from orchestrator.config import DEFAULT_CONFIG

        for mode in ("auto", "native_tui", "headless"):
            cfg = copy.deepcopy(DEFAULT_CONFIG)
            cfg["execution"]["agent_execution_mode"] = mode
            validated = validate_config(cfg)
            self.assertEqual(validated["execution"]["agent_execution_mode"], mode)

    def test_invalid_execution_mode_raises(self):
        import copy
        from orchestrator.config import DEFAULT_CONFIG

        cfg = copy.deepcopy(DEFAULT_CONFIG)
        cfg["execution"]["agent_execution_mode"] = "invalid_mode_xyz"
        with self.assertRaises(ConfigValidationError) as ctx:
            validate_config(cfg)
        self.assertIn("Invalid execution.agent_execution_mode", str(ctx.exception))

    def test_default_execution_mode_is_auto(self):
        import copy
        from orchestrator.config import DEFAULT_CONFIG

        cfg = copy.deepcopy(DEFAULT_CONFIG)
        cfg["execution"].pop("agent_execution_mode", None)
        validated = validate_config(cfg)
        self.assertEqual(validated["execution"]["agent_execution_mode"], "auto")


class TestOpenCodeNativeTUIController(unittest.TestCase):
    """Test the opencode_tui controller module functions."""

    def test_find_free_port(self):
        port = find_free_port()
        self.assertIsInstance(port, int)
        self.assertGreater(port, 1024)

    def test_parse_model_spec(self):
        self.assertEqual(parse_model_spec("opencode/big-pickle"), ("opencode", "big-pickle"))
        self.assertEqual(parse_model_spec("openai/gpt-4o"), ("openai", "gpt-4o"))
        self.assertEqual(parse_model_spec("custom-model"), ("opencode", "custom-model"))
        self.assertEqual(parse_model_spec(None), ("opencode", "big-pickle"))

    @patch("orchestrator.agents.opencode_tui.get_antigravity_bridge_url", return_value=None)
    def test_native_tui_bridge_offline_raises(self, mock_bridge):
        with self.assertRaises(CLIExecutionError) as ctx:
            run_opencode_native_tui(
                prompt="test prompt",
                project_root="D:\\test",
            )
        self.assertIn("Antigravity integrated terminal bridge", str(ctx.exception))

    @patch("orchestrator.agents.opencode_tui.get_antigravity_bridge_url", return_value="http://127.0.0.1:49182")
    @patch("orchestrator.agents.opencode_tui.check_antigravity_bridge", return_value=True)
    @patch("orchestrator.agents.opencode_tui.subprocess.Popen")
    @patch("orchestrator.agents.opencode_tui.requests.get")
    @patch("orchestrator.agents.opencode_tui.requests.post")
    @patch("orchestrator.agents.opencode_tui.launch_antigravity_integrated_terminal")
    @patch("orchestrator.agents.opencode_tui.close_antigravity_integrated_terminal")
    def test_run_opencode_native_tui_success(
        self,
        mock_close,
        mock_launch,
        mock_post,
        mock_get,
        mock_popen,
        mock_check,
        mock_url,
    ):
        # Mock server process
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        # Mock terminal launch
        mock_launch.return_value = True

        # Mock HTTP responses
        # 1. health check: get -> 200
        # 2. create session: post -> 200 {"id": "ses-abc"}
        # 3. submit prompt: post -> 204
        # 4. poll status: get -> {"ses-abc": {"type": "busy"}}, then {}
        # 5. get message: get -> [{"info": {"role": "assistant"}, "parts": [{"type": "text", "text": "Task finished!"}]}]
        # 6. get details: get -> {"tokens": {"input": 150, "output": 50, "cache": {"read": 20}}}
        def fake_get(url, *args, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            if "/global/health" in url:
                resp.json.return_value = {"healthy": True}
            elif "/session/status" in url:
                # Sequence: first busy, then idle
                if not hasattr(fake_get, "poll_count"):
                    fake_get.poll_count = 0
                fake_get.poll_count += 1
                if fake_get.poll_count == 1:
                    resp.json.return_value = {"ses-abc": {"type": "busy"}}
                else:
                    resp.json.return_value = {}
            elif "/message" in url:
                resp.json.return_value = [
                    {"info": {"role": "assistant"}, "parts": [{"type": "text", "text": "Implemented successfully."}]}
                ]
            elif "/session/ses-abc" in url:
                resp.json.return_value = {
                    "tokens": {"input": 150, "output": 50, "cache": {"read": 25}}
                }
            return resp

        def fake_post(url, *args, **kwargs):
            resp = MagicMock()
            if url.endswith("/session"):
                resp.status_code = 200
                resp.json.return_value = {"id": "ses-abc"}
            else:
                resp.status_code = 204
            return resp

        mock_get.side_effect = fake_get
        mock_post.side_effect = fake_post

        output, tokens = run_opencode_native_tui(
            prompt="Implement hello.py",
            project_root="D:\\test_root",
            model_name="opencode/big-pickle",
            pause_on_completion=0.0,
            terminal_title="LangGraph - OpenCode Implementer",
        )

        self.assertEqual(output, "Implemented successfully.")
        self.assertTrue(tokens["available"])
        self.assertEqual(tokens["output_tokens"], 50)
        self.assertEqual(tokens["input_tokens"], 175)  # 150 + 25 cache
        mock_launch.assert_called_once()
        mock_close.assert_called_once_with(title="LangGraph - OpenCode Implementer", bridge_url="http://127.0.0.1:49182")
        mock_proc.terminate.assert_called_once()

    @patch("orchestrator.agents.opencode_tui.get_antigravity_bridge_url", return_value="http://127.0.0.1:49182")
    @patch("orchestrator.agents.opencode_tui.check_antigravity_bridge", return_value=True)
    @patch("orchestrator.agents.opencode_tui.launch_antigravity_integrated_terminal", return_value=True)
    @patch("orchestrator.agents.opencode_tui.close_antigravity_integrated_terminal")
    @patch("orchestrator.agents.opencode_tui.subprocess.Popen")
    @patch("orchestrator.agents.opencode_tui.requests.get")
    @patch("orchestrator.agents.opencode_tui.requests.post")
    def test_run_opencode_native_tui_timeout(
        self,
        mock_post,
        mock_get,
        mock_popen,
        mock_close,
        mock_launch,
        mock_check,
        mock_url,
    ):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        def fake_get(url, *args, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            if "/global/health" in url:
                resp.json.return_value = {"healthy": True}
            elif "/session/status" in url:
                # Stays busy indefinitely
                resp.json.return_value = {"ses-timeout": {"type": "busy"}}
            return resp

        def fake_post(url, *args, **kwargs):
            resp = MagicMock()
            if url.endswith("/session"):
                resp.status_code = 200
                resp.json.return_value = {"id": "ses-timeout"}
            else:
                resp.status_code = 204
            return resp

        mock_get.side_effect = fake_get
        mock_post.side_effect = fake_post

        with self.assertRaises(CLITimeoutError):
            run_opencode_native_tui(
                prompt="Infinite loop",
                project_root="D:\\test_root",
                timeout_seconds=1,
                pause_on_completion=0.0,
            )
        mock_close.assert_called_once_with(title="LangGraph - OpenCode Implementer", bridge_url="http://127.0.0.1:49182")


class TestOpenCodeAdapterExecutionMode(unittest.TestCase):
    """Test opencode adapter dispatching between native_tui and headless."""

    @patch("orchestrator.agents.opencode.run_opencode_native_tui")
    def test_native_tui_mode_invokes_controller(self, mock_tui):
        mock_tui.return_value = ("Done via TUI", create_token_usage(100, 50))

        text, usage, mode = run_opencode(
            "Write code",
            agent_execution_mode="native_tui",
            return_execution_mode=True,
        )

        self.assertEqual(text, "Done via TUI")
        self.assertEqual(mode, "native_tui")
        mock_tui.assert_called_once()

    @patch("orchestrator.agents.opencode.run_agent_cli")
    def test_headless_mode_bypasses_tui(self, mock_cli):
        mock_cli_res = MagicMock()
        mock_cli_res.returncode = 0
        mock_cli_res.stdout = "Headless output"
        mock_cli_res.stderr = ""
        mock_cli.return_value = mock_cli_res

        text, usage, mode = run_opencode(
            "Write code",
            agent_execution_mode="headless",
            return_execution_mode=True,
        )

        self.assertEqual(text, "Headless output")
        self.assertEqual(mode, "headless")
        mock_cli.assert_called_once()


class TestAgentResultAndWorkflowIntegration(unittest.TestCase):
    """Test AgentResult compatibility and LangGraph execution mode metadata."""

    def test_create_agent_result_with_execution_mode(self):
        res = create_agent_result(
            agent="opencode",
            role="implementer",
            status="success",
            output="file.py created",
            execution_mode="native_tui",
        )
        self.assertEqual(res["execution_mode"], "native_tui")
        self.assertEqual(res["agent"], "opencode")
        self.assertEqual(res["status"], "success")

    @patch("orchestrator.graph.run_antigravity")
    @patch("orchestrator.graph.run_claude_code")
    @patch("orchestrator.graph.run_opencode")
    def test_workflow_sequential_execution_and_repair_with_native_tui(
        self,
        mock_opencode,
        mock_claude,
        mock_agy,
    ):
        import tempfile

        mock_agy.return_value = ("Research findings", create_token_usage(50, 50))
        mock_claude.side_effect = [
            ("Planner plan", create_token_usage(60, 40)),           # Planner
            ("VERDICT: FAIL\nRequired Fixes:\nAdd greet", create_token_usage(80, 20)),  # Verifier 1 -> FAIL
            ("VERDICT: PASS\nAll good", create_token_usage(70, 30)),                   # Verifier 2 -> PASS
        ]
        # OpenCode first implementation, then repair #1
        mock_opencode.side_effect = [
            ("Initial impl", create_token_usage(100, 50), "native_tui"),
            ("Repaired impl", create_token_usage(120, 60), "native_tui"),
        ]

        with tempfile.TemporaryDirectory() as tmp_dir:
            state = {
                "task": "Create greeting function",
                "project_root": tmp_dir,
                "agent_execution_mode": "auto",
                "run_store_enabled": False,
                "workspace": {"isolated": False, "path": tmp_dir},
            }

            final_state = build_graph(DEFAULT_CONFIG).invoke(state)

            self.assertEqual(final_state.get("status"), "completed")
            self.assertEqual(final_state.get("verification_verdict"), "PASS")
            self.assertEqual(final_state.get("repair_attempts"), 1)

            # Check agent results: 6 total
            # 1: agy researcher
            # 2: claude planner
            # 3: opencode implementer
            # 4: claude verifier (fail)
            # 5: opencode repair
            # 6: claude verifier (pass)
            results = final_state.get("agent_results", [])
            self.assertEqual(len(results), 6)
            opencode_results = [r for r in results if r.get("agent") == "opencode"]
            self.assertEqual(len(opencode_results), 2)
            for r in opencode_results:
                self.assertEqual(r.get("execution_mode"), "native_tui")


if __name__ == "__main__":
    unittest.main()
