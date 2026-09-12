"""Unit and integration tests for the OpenCode CLI adapter."""

import itertools
import os
import subprocess
import unittest
from unittest.mock import patch, MagicMock

from tests.support import FakeProcess

from orchestrator.agents.opencode import (
    run_opencode,
    get_opencode_executable_path,
)
from orchestrator.agents.exceptions import (
    CLIExecutionError,
    CLITimeoutError,
)


class TestOpenCodeAdapter(unittest.TestCase):
    """Test suite for OpenCode adapter logic and error handling."""

    def test_executable_discovery(self):
        """Test that the opencode executable can be discovered on this system."""
        path = get_opencode_executable_path()
        self.assertTrue(bool(path), "opencode executable path should not be empty")
        self.assertTrue(os.path.isfile(path), f"Discovered path '{path}' must be a file")

    @patch("shutil.which", return_value=None)
    @patch("os.path.isfile", return_value=False)
    def test_executable_discovery_missing_raises_error(self, mock_isfile, mock_which):
        """Test that missing opencode executable raises FileNotFoundError."""
        with self.assertRaises(FileNotFoundError) as ctx:
            get_opencode_executable_path()
        self.assertIn("Could not find 'opencode' CLI executable", str(ctx.exception))

    @patch("subprocess.Popen")
    def test_run_opencode_success_mock(self, mock_run):
        """Test successful capture of OpenCode response text."""
        mock_run.return_value = FakeProcess(
            returncode=0,
            stdout="Implemented approved changes across 2 files.\n",
            stderr="",
        )

        response = run_opencode("Implement the planned architectural fixes")
        self.assertEqual(response, "Implemented approved changes across 2 files.")

    @patch("subprocess.Popen")
    def test_run_opencode_command_construction(self, mock_run):
        """Verify command arguments, working directory, and non-shell invocation."""
        mock_run.return_value = FakeProcess(returncode=0, stdout="Success", stderr="")

        test_prompt = "Implement architectural changes"
        test_dir = r"D:\Progmata\Project Beta"
        test_model = "opencode/gpt-5.1-codex"

        run_opencode(test_prompt, working_dir=test_dir, model=test_model)

        mock_run.assert_called_once()
        cmd_called = mock_run.call_args[0][0]
        kwargs_called = mock_run.call_args[1]

        # 1. Official CLI command structure: opencode run --model <model> <prompt>
        self.assertEqual(cmd_called[1], "run")
        self.assertIn("--model", cmd_called)
        model_idx = cmd_called.index("--model")
        self.assertEqual(cmd_called[model_idx + 1], test_model)
        self.assertEqual(cmd_called[-1], test_prompt)

        # 2. Process flags: no shell=True, explicit cwd, DEVNULL stdin
        self.assertEqual(kwargs_called.get("cwd"), test_dir)
        self.assertEqual(kwargs_called.get("stdin"), subprocess.DEVNULL)
        self.assertFalse(kwargs_called.get("shell", False))

    @patch("subprocess.Popen")
    def test_run_opencode_non_zero_exit_mock(self, mock_run):
        """Test handling of non-zero exit codes with stderr retention."""
        mock_run.return_value = FakeProcess(
            returncode=1,
            stdout="",
            stderr="Error: provider authentication required",
        )

        with self.assertRaises(CLIExecutionError) as ctx:
            run_opencode("Test prompt")
        self.assertEqual(ctx.exception.returncode, 1)
        self.assertIn("provider authentication required", ctx.exception.stderr)

    @patch("orchestrator.launcher.time.time", side_effect=itertools.count(0, 6))
    @patch("subprocess.Popen")
    def test_run_opencode_timeout_mock(self, mock_run, _clock):
        """Test handling of subprocess timeout."""
        mock_run.return_value = FakeProcess(times_out=True)

        with self.assertRaises(CLITimeoutError) as ctx:
            run_opencode("Test prompt", timeout=10)
        self.assertEqual(ctx.exception.timeout, 10)

    @unittest.skipUnless(os.environ.get("RUN_LIVE_TESTS") == "1", "Live test requires RUN_LIVE_TESTS=1")
    def test_run_opencode_live(self):
        """Live smoke test invoking the local opencode CLI (skipped by default)."""
        response = run_opencode("Reply with exactly: OPENCODE_TEST", timeout=60)
        self.assertIn("OPENCODE_TEST", response)


if __name__ == "__main__":
    unittest.main()
