"""Unit tests for Stage 7.5 Visible Agent Execution Terminals and Process Launcher."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from orchestrator.config import (
    validate_config,
    load_config,
    get_visible_terminals,
    get_execution_config,
    ConfigValidationError,
    DEFAULT_CONFIG,
)
from orchestrator.launcher import (
    ExecutionResult,
    get_terminal_title,
    run_agent_cli,
    _runner_entrypoint,
)
from orchestrator.agents.exceptions import CLIExecutionError, CLITimeoutError
from orchestrator.tracer import ExecutionTracer


class TestLauncherConfig(unittest.TestCase):
    """Test configuration validation and retrieval for execution settings."""

    def test_default_config_has_visible_terminals_false(self):
        """Verify default execution config sets visible_terminals to False."""
        self.assertIn("execution", DEFAULT_CONFIG)
        self.assertFalse(DEFAULT_CONFIG["execution"]["visible_terminals"])
        self.assertEqual(DEFAULT_CONFIG["execution"]["terminal_type"], "auto")
        self.assertEqual(DEFAULT_CONFIG["execution"]["pause_on_completion"], 1.5)

    def test_get_visible_terminals_defaults(self):
        """Verify get_visible_terminals helper with None and empty config."""
        self.assertFalse(get_visible_terminals(None))
        self.assertFalse(get_visible_terminals({}))
        self.assertFalse(get_visible_terminals(DEFAULT_CONFIG))

    def test_get_visible_terminals_enabled(self):
        """Verify get_visible_terminals returns True when configured."""
        cfg = dict(DEFAULT_CONFIG)
        cfg["execution"] = {"visible_terminals": True, "terminal_type": "auto", "pause_on_completion": 1.0}
        self.assertTrue(get_visible_terminals(cfg))

    def test_get_execution_config_safe_defaults(self):
        """Verify get_execution_config returns safe defaults if missing."""
        res = get_execution_config(None)
        self.assertFalse(res["visible_terminals"])
        self.assertEqual(res["terminal_type"], "auto")

    def test_validate_config_visible_terminals_boolean(self):
        """Verify validation accepts valid boolean visible_terminals."""
        raw = {
            "agents": [{"agent": "antigravity", "role": "researcher"}],
            "roles": {"researcher": {"responsibility": "Analyze"}},
            "execution": {
                "visible_terminals": True,
                "terminal_type": "windows_terminal",
                "pause_on_completion": 2.0,
            },
        }
        validated = validate_config(raw)
        self.assertTrue(validated["execution"]["visible_terminals"])
        self.assertEqual(validated["execution"]["terminal_type"], "windows_terminal")
        self.assertEqual(validated["execution"]["pause_on_completion"], 2.0)

    def test_validate_config_rejects_non_boolean_visible_terminals(self):
        """Verify validation rejects non-boolean visible_terminals."""
        raw = {
            "agents": [{"agent": "antigravity", "role": "researcher"}],
            "roles": {"researcher": {"responsibility": "Analyze"}},
            "execution": {"visible_terminals": "yes"},
        }
        with self.assertRaises(ConfigValidationError) as ctx:
            validate_config(raw)
        self.assertIn("execution.visible_terminals", str(ctx.exception))

    def test_validate_config_rejects_invalid_terminal_type(self):
        """Verify validation rejects unsupported terminal types."""
        raw = {
            "agents": [{"agent": "antigravity", "role": "researcher"}],
            "roles": {"researcher": {"responsibility": "Analyze"}},
            "execution": {"terminal_type": "xterm_256"},
        }
        with self.assertRaises(ConfigValidationError) as ctx:
            validate_config(raw)
        self.assertIn("Invalid execution.terminal_type", str(ctx.exception))

    def test_validate_config_rejects_negative_pause(self):
        """Verify validation rejects negative pause_on_completion."""
        raw = {
            "agents": [{"agent": "antigravity", "role": "researcher"}],
            "roles": {"researcher": {"responsibility": "Analyze"}},
            "execution": {"pause_on_completion": -1.0},
        }
        with self.assertRaises(ConfigValidationError) as ctx:
            validate_config(raw)
        self.assertIn("execution.pause_on_completion", str(ctx.exception))


class TestTerminalTitleConstruction(unittest.TestCase):
    """Test standardized terminal title formatting for each agent and role."""

    def test_antigravity_researcher_title(self):
        """Test initial Antigravity Researcher title."""
        title = get_terminal_title("antigravity", "researcher")
        self.assertEqual(title, "LangGraph - Antigravity Researcher")

    def test_claude_planner_title(self):
        """Test initial Claude Planner title."""
        title = get_terminal_title("claude", "planner")
        self.assertEqual(title, "LangGraph - Claude Planner")

    def test_opencode_implementer_title(self):
        """Test initial OpenCode Implementer title."""
        title = get_terminal_title("opencode", "implementer")
        self.assertEqual(title, "LangGraph - OpenCode Implementer")

    def test_claude_verifier_initial_title(self):
        """Test initial Claude Verifier title."""
        title = get_terminal_title("claude", "verifier")
        self.assertEqual(title, "LangGraph - Claude Verifier")

    def test_claude_verifier_first_attempt_title(self):
        """Test Claude Verifier attempt 1 title."""
        title = get_terminal_title("claude", "verifier", verification_attempt=1)
        self.assertEqual(title, "LangGraph - Claude Verifier")

    def test_claude_verifier_subsequent_attempt_title(self):
        """Test Claude Verifier reverification attempt title."""
        title = get_terminal_title("claude", "verifier", verification_attempt=2)
        self.assertEqual(title, "LangGraph - Claude Verification #2")

        title3 = get_terminal_title("claude", "verifier", verification_attempt=3)
        self.assertEqual(title3, "LangGraph - Claude Verification #3")

    def test_opencode_repair_title(self):
        """Test OpenCode Repair title with attempt counter."""
        title1 = get_terminal_title("opencode", "repair", repair_attempt=1)
        self.assertEqual(title1, "LangGraph - OpenCode Repair #1")

        title2 = get_terminal_title("opencode", "implementer", repair_attempt=2)
        self.assertEqual(title2, "LangGraph - OpenCode Repair #2")


class TestProcessLauncherUnit(unittest.TestCase):
    """Test process launcher execution in headless and visible modes."""

    def test_empty_command_raises_value_error(self):
        """Test that empty command list raises ValueError."""
        with self.assertRaises(ValueError):
            run_agent_cli([])

    def test_headless_execution_success(self):
        """Test standard headless execution using Python one-liner."""
        cmd = [sys.executable, "-c", "import sys; sys.stdout.write('Hello Headless')"]
        res = run_agent_cli(cmd, visible=False)
        self.assertIsInstance(res, ExecutionResult)
        self.assertEqual(res.returncode, 0)
        self.assertEqual(res.stdout, "Hello Headless")
        self.assertEqual(res.stderr, "")
        self.assertGreaterEqual(res.duration_seconds, 0.0)

    def test_headless_execution_stdout_and_stderr_separation(self):
        """Test that stdout and stderr remain distinct in headless mode."""
        cmd = [
            sys.executable,
            "-c",
            "import sys; sys.stdout.write('OutText\\n'); sys.stderr.write('ErrText\\n')",
        ]
        res = run_agent_cli(cmd, visible=False)
        self.assertEqual(res.returncode, 0)
        self.assertEqual(res.stdout, "OutText\n")
        self.assertEqual(res.stderr, "ErrText\n")

    def test_headless_execution_timeout(self):
        """Test that timeout raises CLITimeoutError in headless mode."""
        cmd = [sys.executable, "-c", "import time; time.sleep(5)"]
        with self.assertRaises(CLITimeoutError) as ctx:
            run_agent_cli(cmd, timeout=1, visible=False)
        self.assertEqual(ctx.exception.timeout, 1)

    @patch("subprocess.Popen")
    def test_visible_execution_job_spec_and_process_creation(self, mock_popen):
        """Verify visible mode creates job specification and invokes runner with CREATE_NEW_CONSOLE."""
        # Create a mock process that simulates writing status.json
        captured_job_file = []

        def side_effect(args, **kwargs):
            # args: [sys.executable, '-m', 'orchestrator.launcher', '--job-file', job_file]
            job_path = args[args.index("--job-file") + 1]
            captured_job_file.append(job_path)
            with open(job_path, "r", encoding="utf-8") as jf:
                job_data = json.load(jf)
            # Write status file to simulate runner completion
            with open(job_data["status_file"], "w", encoding="utf-8") as sf:
                json.dump({
                    "returncode": 0,
                    "stdout": "Visible Output",
                    "stderr": "",
                    "duration_seconds": 0.5,
                }, sf)

            proc_mock = MagicMock()
            proc_mock.returncode = 0
            proc_mock.wait.return_value = 0
            return proc_mock

        mock_popen.side_effect = side_effect

        tracer = ExecutionTracer(verbose=False)
        cmd = ["dummy_exe", "arg1", "arg2"]
        cwd = os.getcwd()

        res = run_agent_cli(
            cmd=cmd,
            cwd=cwd,
            timeout=30,
            visible=True,
            terminal_type="console",
            agent="antigravity",
            role="researcher",
            model="gemini-3.8-flash-high",
            pause_on_completion=0.0,
            tracer=tracer,
        )

        mock_popen.assert_called_once()
        called_args = mock_popen.call_args[0][0]
        called_kwargs = mock_popen.call_args[1]

        # Verify CREATE_NEW_CONSOLE (0x00000010) was passed
        self.assertEqual(called_kwargs.get("creationflags"), 0x00000010)
        self.assertIn("-m", called_args)
        self.assertIn("orchestrator.launcher", called_args)

        # Verify ExecutionResult
        self.assertEqual(res.returncode, 0)
        self.assertEqual(res.stdout, "Visible Output")

        # Verify tracer recorded terminal_launch and terminal_process_exit
        event_names = [e.name for e in tracer.get_events()]
        self.assertIn("terminal_launch", event_names)
        self.assertIn("terminal_process_exit", event_names)

    def test_runner_entrypoint_executes_child_and_writes_status(self):
        """Test internal runner entrypoint logic with temporary job specification."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            job_file = os.path.join(tmp_dir, "job.json")
            status_file = os.path.join(tmp_dir, "status.json")

            job_spec = {
                "title": "LangGraph - Test Child",
                "agent": "test",
                "role": "tester",
                "model": "model-1",
                "cmd": [sys.executable, "-c", "import sys; sys.stdout.write('Child Output\\n')"],
                "cwd": os.getcwd(),
                "status_file": status_file,
                "timeout": 10,
                "pause_seconds": 0.0,
            }

            with open(job_file, "w", encoding="utf-8") as f:
                json.dump(job_spec, f)

            with self.assertRaises(SystemExit) as ctx:
                _runner_entrypoint(job_file)

            self.assertEqual(ctx.exception.code, 0)
            self.assertTrue(os.path.isfile(status_file))

            with open(status_file, "r", encoding="utf-8") as sf:
                status = json.load(sf)

            self.assertEqual(status["returncode"], 0)
            self.assertEqual(status["stdout"], "Child Output\n")


if __name__ == "__main__":
    unittest.main()
