"""Unit and integration tests for the Claude Code CLI adapter."""

import os
import subprocess
import unittest
from unittest.mock import patch, MagicMock

from orchestrator.agents.claude_code import (
    run_claude_code,
    get_claude_executable_path,
)
from orchestrator.agents.exceptions import (
    CLIExecutionError,
    CLITimeoutError,
)


class TestClaudeCodeAdapter(unittest.TestCase):
    """Test suite for Claude Code adapter logic and error handling."""

    def test_executable_discovery(self):
        """Test that the claude executable can be discovered on this system."""
        path = get_claude_executable_path()
        self.assertTrue(bool(path), "claude executable path should not be empty")

    @patch("subprocess.run")
    def test_run_claude_code_success_mock(self, mock_run):
        """Test successful capture of Claude Code response text."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="Here is the review from Claude.\n",
            stderr="",
        )

        response = run_claude_code("Test prompt")
        self.assertEqual(response, "Here is the review from Claude.")

    @patch("subprocess.run")
    def test_run_claude_code_model_flag(self, mock_run):
        """Test that passing model appends --model <name> to claude command."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="Claude model test response",
            stderr="",
        )

        run_claude_code("Test prompt", model="sonnet")
        called_cmd = mock_run.call_args[0][0]
        self.assertIn("--model", called_cmd)
        model_idx = called_cmd.index("--model")
        self.assertEqual(called_cmd[model_idx + 1], "sonnet")

    @patch("subprocess.run")
    def test_run_claude_code_non_zero_exit_mock(self, mock_run):
        """Test handling of non-zero exit codes from claude."""
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="Error: auth required",
        )

        with self.assertRaises(CLIExecutionError) as ctx:
            run_claude_code("Test prompt")
        self.assertEqual(ctx.exception.returncode, 1)

    @patch("subprocess.run")
    def test_run_claude_code_timeout_mock(self, mock_run):
        """Test handling of subprocess timeout."""
        mock_run.side_effect = subprocess.TimeoutExpired(cmd=["claude"], timeout=10)

        with self.assertRaises(CLITimeoutError) as ctx:
            run_claude_code("Test prompt", timeout=10)
        self.assertEqual(ctx.exception.timeout, 10)

    @unittest.skipUnless(os.environ.get("RUN_LIVE_TESTS") == "1", "Live test requires RUN_LIVE_TESTS=1")
    def test_run_claude_code_live(self):
        """Live smoke test invoking the authenticated claude CLI with a strict test response."""
        response = run_claude_code("Reply with exactly: CLAUDE_TEST", timeout=60)
        self.assertIn("CLAUDE_TEST", response)


if __name__ == "__main__":
    unittest.main()
