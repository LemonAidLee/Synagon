"""Unit and integration tests for the Antigravity CLI adapter."""

import itertools
import json
import os
import subprocess
import unittest
from unittest.mock import patch, MagicMock

from tests.support import FakeProcess

from orchestrator.agents.antigravity import (
    run_antigravity,
    run_antigravity_raw,
    get_antigravity_executable_path,
)
from orchestrator.agents.exceptions import (
    CLIExecutionError,
    CLITimeoutError,
    CLIParsingError,
)


class TestAntigravityAdapter(unittest.TestCase):
    """Test suite for Antigravity adapter logic and error handling."""

    def test_executable_discovery(self):
        """Test that the agy executable can be discovered on this system."""
        path = get_antigravity_executable_path()
        self.assertTrue(bool(path), "agy executable path should not be empty")

    @patch("subprocess.Popen")
    def test_run_antigravity_success_mock(self, mock_run):
        """Test successful parsing of Antigravity JSON output."""
        mock_run.return_value = FakeProcess(
            returncode=0,
            stdout=json.dumps({
                "status": "SUCCESS",
                "response": "Here is the research findings.\n",
                "conversation_id": "test-conv-123",
            }),
            stderr="",
        )

        response = run_antigravity("Test prompt")
        self.assertEqual(response, "Here is the research findings.")

    @patch("subprocess.Popen")
    def test_run_antigravity_model_flag(self, mock_run):
        """Test that passing model appends --model <name> to agy command."""
        mock_run.return_value = FakeProcess(
            returncode=0,
            stdout=json.dumps({"status": "SUCCESS", "response": "Model test response"}),
            stderr="",
        )

        run_antigravity("Test prompt", model="gemini-3.8-flash-high")
        called_cmd = mock_run.call_args[0][0]
        self.assertIn("--model", called_cmd)
        model_idx = called_cmd.index("--model")
        self.assertEqual(called_cmd[model_idx + 1], "gemini-3.8-flash-high")

    @patch("subprocess.Popen")
    def test_run_antigravity_non_zero_exit_mock(self, mock_run):
        """Test handling of non-zero exit codes from agy."""
        mock_run.return_value = FakeProcess(
            returncode=1,
            stdout="",
            stderr="Error: connection failed",
        )

        with self.assertRaises(CLIExecutionError) as ctx:
            run_antigravity("Test prompt")
        self.assertEqual(ctx.exception.returncode, 1)

    @patch("orchestrator.launcher.time.time", side_effect=itertools.count(0, 6))
    @patch("subprocess.Popen")
    def test_run_antigravity_timeout_mock(self, mock_run, _clock):
        """Test handling of subprocess timeout."""
        mock_run.return_value = FakeProcess(times_out=True)

        with self.assertRaises(CLITimeoutError) as ctx:
            run_antigravity("Test prompt", timeout=10)
        self.assertEqual(ctx.exception.timeout, 10)

    @patch("subprocess.Popen")
    def test_run_antigravity_malformed_json_mock(self, mock_run):
        """Test handling of malformed JSON from agy."""
        mock_run.return_value = FakeProcess(
            returncode=0,
            stdout="Not valid JSON at all",
            stderr="",
        )

        with self.assertRaises(CLIParsingError):
            run_antigravity("Test prompt")

    @patch("subprocess.Popen")
    def test_run_antigravity_non_success_status_mock(self, mock_run):
        """Test handling of JSON response with status != SUCCESS."""
        mock_run.return_value = FakeProcess(
            returncode=0,
            stdout=json.dumps({
                "status": "FAILED",
                "response": "Something went wrong",
            }),
            stderr="",
        )

        with self.assertRaises(CLIExecutionError):
            run_antigravity("Test prompt")

    @unittest.skipUnless(os.environ.get("RUN_LIVE_TESTS") == "1", "Live test requires RUN_LIVE_TESTS=1")
    def test_run_antigravity_live(self):
        """Live smoke test invoking the authenticated agy CLI with a strict test response."""
        response = run_antigravity("Reply with exactly: AGY_TEST", timeout=60)
        self.assertIn("AGY_TEST", response)


if __name__ == "__main__":
    unittest.main()
