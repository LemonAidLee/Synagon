"""Tests for Stage 7.5.1 Antigravity Integrated Terminal Execution."""

import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from orchestrator.agents.exceptions import CLIExecutionError, CLITimeoutError
from orchestrator.config import ConfigValidationError, validate_config
from orchestrator.launcher import (
    DEFAULT_BRIDGE_URL,
    check_antigravity_bridge,
    close_antigravity_integrated_terminal,
    get_antigravity_bridge_url,
    launch_antigravity_integrated_terminal,
    run_agent_cli,
)


class TestIntegratedTerminalBridge(unittest.TestCase):
    """Tests for Antigravity IDE Integrated Terminal Bridge helpers."""

    def test_default_bridge_url(self):
        with patch("os.path.isfile", return_value=False):
            url = get_antigravity_bridge_url()
            self.assertEqual(url, DEFAULT_BRIDGE_URL)

    def test_bridge_url_from_port_file(self):
        fake_data = json.dumps({"port": 54321, "host": "127.0.0.1"})
        with patch("os.path.isfile", return_value=True), patch(
            "builtins.open", unittest.mock.mock_open(read_data=fake_data)
        ):
            url = get_antigravity_bridge_url()
            self.assertEqual(url, "http://127.0.0.1:54321")

    @patch("urllib.request.urlopen")
    def test_check_antigravity_bridge_healthy(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = json.dumps({"status": "ok"}).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        self.assertTrue(check_antigravity_bridge())

    @patch("urllib.request.urlopen")
    def test_check_antigravity_bridge_unhealthy_status(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = json.dumps({"status": "error"}).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        self.assertFalse(check_antigravity_bridge())

    @patch("urllib.request.urlopen", side_effect=Exception("Connection refused"))
    def test_check_antigravity_bridge_unreachable(self, mock_urlopen):
        self.assertFalse(check_antigravity_bridge())

    @patch("urllib.request.urlopen")
    def test_launch_integrated_terminal_success(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = json.dumps({"success": True}).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        ok = launch_antigravity_integrated_terminal(
            title="LangGraph - Claude Planner",
            cwd=r"D:\TestProject",
            command='python -m orchestrator.launcher --job-file "test.json"',
        )
        self.assertTrue(ok)

    @patch("urllib.request.urlopen", side_effect=Exception("Network error"))
    def test_launch_integrated_terminal_network_failure(self, mock_urlopen):
        with self.assertRaises(CLIExecutionError) as ctx:
            launch_antigravity_integrated_terminal(
                title="LangGraph - Claude Planner",
                cwd=r"D:\TestProject",
                command="echo test",
            )
        self.assertIn("Failed to communicate with Antigravity Terminal Bridge", str(ctx.exception))

    @patch("urllib.request.urlopen")
    def test_close_integrated_terminal_success(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = json.dumps({"success": True}).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        ok = close_antigravity_integrated_terminal(title="LangGraph - Claude Planner")
        self.assertTrue(ok)


class TestRunAgentCliIntegrated(unittest.TestCase):
    """Tests for run_agent_cli using antigravity_integrated terminal type."""

    @patch("orchestrator.launcher.check_antigravity_bridge", return_value=False)
    def test_integrated_mode_bridge_unavailable_raises_error(self, mock_bridge):
        with self.assertRaises(CLIExecutionError) as ctx:
            run_agent_cli(
                cmd=["echo", "test"],
                visible=True,
                terminal_type="antigravity_integrated",
            )
        self.assertIn("Antigravity IDE Integrated Terminal Bridge is not reachable", str(ctx.exception))

    @patch("orchestrator.launcher.launch_antigravity_integrated_terminal")
    @patch("orchestrator.launcher.check_antigravity_bridge", return_value=True)
    def test_integrated_mode_success(self, mock_bridge, mock_launch):
        def fake_launch(title, cwd, command, env=None, **kwargs):
            # Extract status_file from command or runner job
            # Find status.json from temp directories
            for root, dirs, files in os.walk(tempfile.gettempdir()):
                if "status.json" in files:
                    pass
            return True

        mock_launch.side_effect = fake_launch

        # We simulate status file creation by hooking tempfile
        original_mkdtemp = tempfile.mkdtemp

        created_dir = None

        def tracked_mkdtemp(*args, **kwargs):
            nonlocal created_dir
            created_dir = original_mkdtemp(*args, **kwargs)
            # Pre-write status.json simulating fast runner execution
            status_file = os.path.join(created_dir, "status.json")
            with open(status_file, "w", encoding="utf-8") as f:
                json.dump({
                    "returncode": 0,
                    "stdout": "Agent execution succeeded\nTotal tokens: 420\n",
                    "stderr": "",
                    "duration_seconds": 1.25,
                }, f)
            return created_dir

        with patch("tempfile.mkdtemp", side_effect=tracked_mkdtemp):
            res = run_agent_cli(
                cmd=["echo", "hello"],
                visible=True,
                terminal_type="antigravity_integrated",
                title="LangGraph - OpenCode Implementer",
                agent="opencode",
                role="implementer",
            )

        self.assertEqual(res.returncode, 0)
        self.assertIn("Agent execution succeeded", res.stdout)
        self.assertIn("Total tokens: 420", res.stdout)
        self.assertEqual(res.duration_seconds, 1.25)
        mock_launch.assert_called_once()

    @patch("orchestrator.launcher.launch_antigravity_integrated_terminal", return_value=True)
    @patch("orchestrator.launcher.check_antigravity_bridge", return_value=True)
    def test_integrated_mode_timeout(self, mock_bridge, mock_launch):
        # Fast-forward time to avoid sleeping 30s
        fake_time = [1000.0]

        def fast_time():
            fake_time[0] += 10.0
            return fake_time[0]

        with patch("time.time", side_effect=fast_time), patch("time.sleep", return_value=None):
            with self.assertRaises(CLITimeoutError):
                run_agent_cli(
                    cmd=["echo", "timeout"],
                    visible=True,
                    terminal_type="antigravity_integrated",
                    timeout=1,
                    pause_on_completion=0,
                )

    @patch("orchestrator.launcher.check_antigravity_bridge", return_value=True)
    @patch("orchestrator.launcher.launch_antigravity_integrated_terminal")
    def test_auto_prefers_integrated_when_bridge_healthy(self, mock_launch, mock_bridge):
        original_mkdtemp = tempfile.mkdtemp

        def tracked_mkdtemp(*args, **kwargs):
            d = original_mkdtemp(*args, **kwargs)
            with open(os.path.join(d, "status.json"), "w", encoding="utf-8") as f:
                json.dump({
                    "returncode": 0,
                    "stdout": "auto-integrated output",
                    "stderr": "",
                    "duration_seconds": 0.5,
                }, f)
            return d

        with patch("tempfile.mkdtemp", side_effect=tracked_mkdtemp):
            res = run_agent_cli(
                cmd=["echo", "auto"],
                visible=True,
                terminal_type="auto",
            )

        self.assertEqual(res.returncode, 0)
        self.assertEqual(res.stdout, "auto-integrated output")
        mock_launch.assert_called_once()


class TestConfigValidationIntegrated(unittest.TestCase):
    """Tests for terminal_type validation in orchestrator/config.py."""

    def _base_config(self):
        return {
            "agents": [
                {"agent": "antigravity", "role": "researcher"},
                {"agent": "claude", "role": "planner"},
                {"agent": "opencode", "role": "implementer"},
            ],
            "roles": {
                "researcher": {"responsibility": "Research code"},
                "planner": {"responsibility": "Plan architecture"},
                "implementer": {"responsibility": "Implement code"},
            },
        }

    def test_valid_antigravity_integrated_type(self):
        cfg = self._base_config()
        cfg["execution"] = {
            "visible_terminals": True,
            "terminal_type": "antigravity_integrated",
            "pause_on_completion": 2.0,
        }
        res = validate_config(cfg)
        self.assertEqual(res["execution"]["terminal_type"], "antigravity_integrated")

    def test_valid_integrated_alias(self):
        cfg = self._base_config()
        cfg["execution"] = {
            "visible_terminals": True,
            "terminal_type": "integrated",
        }
        res = validate_config(cfg)
        self.assertEqual(res["execution"]["terminal_type"], "integrated")

    def test_invalid_terminal_type_rejected(self):
        cfg = self._base_config()
        cfg["execution"] = {
            "visible_terminals": True,
            "terminal_type": "invalid_terminal",
        }
        with self.assertRaises(ConfigValidationError) as ctx:
            validate_config(cfg)
        self.assertIn("Invalid execution.terminal_type", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
