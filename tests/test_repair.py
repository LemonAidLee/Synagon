"""Unit and integration tests for Stage 7: Controlled Self-Repair Loop."""

import os
import unittest
from unittest.mock import patch, MagicMock

from orchestrator.prompts import build_repair_prompt
from orchestrator.agents.verifier import parse_verdict
from orchestrator.prompts import build_verifier_prompt
from orchestrator.config import (
    ConfigValidationError,
    load_config,
    validate_config,
    get_max_repair_attempts,
)
from orchestrator.graph import graph, should_repair_or_end
from orchestrator.tracer import default_tracer


class TestRepairPrompt(unittest.TestCase):
    """Unit tests for repair prompt generation and content contract."""

    def test_repair_prompt_contains_all_required_elements(self):
        """Test 4: Verify repair prompt contains task, verifier findings, required fixes, and attempt count."""
        prompt = build_repair_prompt(
            task="Build a calculator module with add() and multiply()",
            project_context="Files: calc.py, test_calc.py",
            plan_text="Implement add() and multiply() in calc.py",
            previous_implementation="def add(a, b): return a + b\n# multiply missing",
            verifier_output=(
                "VERDICT: FAIL\n\n"
                "Summary:\nMultiply function is missing.\n\n"
                "Findings:\n- multiply() is not implemented.\n\n"
                "Required Fixes:\n- Implement multiply(a, b) in calc.py"
            ),
            repair_attempt=1,
            max_repair_attempts=2,
            responsibility="Implement approved changes directly in the workspace.",
            role_name="implementer",
        )

        # 1. Original task
        self.assertIn("Build a calculator module with add() and multiply()", prompt)
        # 2. Project context
        self.assertIn("Files: calc.py, test_calc.py", prompt)
        # 3. Previous implementation
        self.assertIn("def add(a, b): return a + b", prompt)
        # 4. Verifier output & findings
        self.assertIn("VERDICT: FAIL", prompt)
        self.assertIn("Multiply function is missing.", prompt)
        self.assertIn("Implement multiply(a, b) in calc.py", prompt)
        # 5. Repair attempt numbers
        self.assertIn("Repair Attempt: 1 of 2", prompt)
        # 6. Inspection instruction
        self.assertIn("Inspect the current workspace and implementation files before modifying anything", prompt)
        # 7. Responsibility
        self.assertIn("Implement approved changes directly in the workspace", prompt)


class TestRepairConfigValidation(unittest.TestCase):
    """Unit tests for max_repair_attempts configuration and validation."""

    def test_valid_max_repair_attempts(self):
        """Test 12a: Valid integer max_repair_attempts (0, 1, 5) accepted."""
        base_data = {
            "agents": [{"agent": "opencode", "role": "implementer"}],
            "roles": {"implementer": {"responsibility": "Implement changes"}},
        }

        cfg0 = validate_config({**base_data, "max_repair_attempts": 0})
        self.assertEqual(cfg0["max_repair_attempts"], 0)

        cfg2 = validate_config({**base_data, "max_repair_attempts": 2})
        self.assertEqual(cfg2["max_repair_attempts"], 2)

        cfg5 = validate_config({**base_data, "max_repair_attempts": 5})
        self.assertEqual(cfg5["max_repair_attempts"], 5)

    def test_default_max_repair_attempts(self):
        """Test default max_repair_attempts is 2 when omitted."""
        base_data = {
            "agents": [{"agent": "opencode", "role": "implementer"}],
            "roles": {"implementer": {"responsibility": "Implement changes"}},
        }
        cfg = validate_config(base_data)
        self.assertEqual(cfg["max_repair_attempts"], 2)
        self.assertEqual(get_max_repair_attempts(cfg), 2)

    def test_invalid_max_repair_attempts_negative(self):
        """Test 12b: Negative max_repair_attempts raises ConfigValidationError."""
        data = {
            "agents": [{"agent": "opencode", "role": "implementer"}],
            "roles": {"implementer": {"responsibility": "Implement changes"}},
            "max_repair_attempts": -1,
        }
        with self.assertRaises(ConfigValidationError):
            validate_config(data)

    def test_invalid_max_repair_attempts_non_int(self):
        """Test 12c: Non-integer max_repair_attempts raises ConfigValidationError."""
        data = {
            "agents": [{"agent": "opencode", "role": "implementer"}],
            "roles": {"implementer": {"responsibility": "Implement changes"}},
            "max_repair_attempts": "two",
        }
        with self.assertRaises(ConfigValidationError):
            validate_config(data)


class TestRepairWorkflow(unittest.TestCase):
    """Integration tests verifying the LangGraph self-repair loop behavior."""

    def setUp(self):
        default_tracer.clear()

    @patch("orchestrator.graph.run_claude_code")
    @patch("orchestrator.graph.run_opencode")
    @patch("orchestrator.graph.run_antigravity")
    def test_pass_does_not_repair(self, mock_antigravity, mock_opencode, mock_claude):
        """Test 1: When initial verification returns PASS, workflow terminates immediately (repair_attempts == 0)."""
        mock_antigravity.return_value = "Research output."
        mock_opencode.return_value = "Implementation output."
        mock_claude.side_effect = [
            "Planner recommendations.",
            "VERDICT: PASS\n\nSummary: Implementation verified.",
        ]

        result = graph.invoke({
            "task": "Build feature A",
            "project_root": os.getcwd(),
            "run_store_enabled": False,
            "workspace": {"isolated": False, "path": os.getcwd()},
        })

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["verification_verdict"], "PASS")
        self.assertEqual(result["repair_attempts"], 0)
        # OpenCode called exactly once (initial implementation)
        self.assertEqual(mock_opencode.call_count, 1)
        # Claude called twice (planner, verifier)
        self.assertEqual(mock_claude.call_count, 2)

    @patch("orchestrator.graph.run_claude_code")
    @patch("orchestrator.graph.run_opencode")
    @patch("orchestrator.graph.run_antigravity")
    def test_fail_triggers_repair_and_increments_counter(
        self, mock_antigravity, mock_opencode, mock_claude
    ):
        """Test 2, 3, 5, 6: FAIL triggers repair, increments repair_attempts, re-verifies, and succeeds on PASS."""
        mock_antigravity.return_value = "Research findings."
        mock_opencode.side_effect = [
            "Initial implementation (flawed).",
            "Repaired implementation (fixed).",
        ]
        mock_claude.side_effect = [
            "Planner plan.",
            "VERDICT: FAIL\n\nSummary: Bug in calculation.\n\nRequired Fixes:\nFix calc formula.",
            "VERDICT: PASS\n\nSummary: Bug resolved. Tests pass.",
        ]

        result = graph.invoke({
            "task": "Fix the formula in calc.py",
            "project_root": os.getcwd(),
            "run_store_enabled": False,
            "workspace": {"isolated": False, "path": os.getcwd()},
            "max_repair_attempts": 2,
        })

        # Final state
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["verification_verdict"], "PASS")
        self.assertEqual(result["repair_attempts"], 1)

        # OpenCode called twice: initial implementation + repair #1
        self.assertEqual(mock_opencode.call_count, 2)
        # Claude called 3 times: planner + initial verification + reverification
        self.assertEqual(mock_claude.call_count, 3)

        # Verify second OpenCode call received repair prompt with failure findings
        repair_call_prompt = mock_opencode.call_args_list[1][0][0]
        self.assertIn("SELF-REPAIR ATTEMPT", repair_call_prompt)
        self.assertIn("Repair Attempt: 1 of 2", repair_call_prompt)
        self.assertIn("Fix calc formula", repair_call_prompt)

    @patch("orchestrator.graph.run_claude_code")
    @patch("orchestrator.graph.run_opencode")
    @patch("orchestrator.graph.run_antigravity")
    def test_repair_fails_repeatedly_until_exhaustion(
        self, mock_antigravity, mock_opencode, mock_claude
    ):
        """Test 7 & 8: When repair fails repeatedly, stops at max_repair_attempts with status failed."""
        mock_antigravity.return_value = "Research."
        # Initial implementation + Repair 1 + Repair 2
        mock_opencode.side_effect = [
            "Initial impl.",
            "Repair attempt 1.",
            "Repair attempt 2.",
        ]
        # Planner + Verifier 1 (FAIL) + Verifier 2 (FAIL) + Verifier 3 (FAIL)
        mock_claude.side_effect = [
            "Plan.",
            "VERDICT: FAIL\n\nFindings: error 1",
            "VERDICT: FAIL\n\nFindings: error 2",
            "VERDICT: FAIL\n\nFindings: error 3",
        ]

        result = graph.invoke({
            "task": "Fix difficult bug",
            "project_root": os.getcwd(),
            "run_store_enabled": False,
            "workspace": {"isolated": False, "path": os.getcwd()},
            "max_repair_attempts": 2,
        })

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["verification_verdict"], "FAIL")
        self.assertEqual(result["repair_attempts"], 2)

        # OpenCode called 3 times total (1 initial + 2 repairs), NOT a 4th time
        self.assertEqual(mock_opencode.call_count, 3)
        # Claude called 4 times total (1 plan + 3 verifications)
        self.assertEqual(mock_claude.call_count, 4)

    @patch("orchestrator.graph.run_claude_code")
    @patch("orchestrator.graph.run_opencode")
    @patch("orchestrator.graph.run_antigravity")
    def test_zero_repair_attempts_terminates_immediately(
        self, mock_antigravity, mock_opencode, mock_claude
    ):
        """Test 9: With max_repair_attempts=0, FAIL leads to immediate termination without repair."""
        mock_antigravity.return_value = "Research."
        mock_opencode.return_value = "Initial impl."
        mock_claude.side_effect = [
            "Plan.",
            "VERDICT: FAIL\n\nFindings: test failed",
        ]

        result = graph.invoke({
            "task": "Test zero repairs mode",
            "project_root": os.getcwd(),
            "run_store_enabled": False,
            "workspace": {"isolated": False, "path": os.getcwd()},
            "max_repair_attempts": 0,
        })

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["verification_verdict"], "FAIL")
        self.assertEqual(result["repair_attempts"], 0)

        # OpenCode called once only
        self.assertEqual(mock_opencode.call_count, 1)
        # Claude called twice only (planner, verifier)
        self.assertEqual(mock_claude.call_count, 2)

    @patch("orchestrator.graph.run_claude_code")
    @patch("orchestrator.graph.run_opencode")
    @patch("orchestrator.graph.run_antigravity")
    def test_unknown_verdict_never_becomes_pass(
        self, mock_antigravity, mock_opencode, mock_claude
    ):
        """Test 10: UNKNOWN verdict is treated as non-PASS (triggers repair, never reports PASS)."""
        mock_antigravity.return_value = "Research."
        mock_opencode.side_effect = [
            "Initial impl.",
            "Repair impl.",
        ]
        mock_claude.side_effect = [
            "Plan.",
            "Ambiguous output without verdict line.",
            "Another ambiguous output.",
        ]

        result = graph.invoke({
            "task": "Test unknown verdict safety",
            "project_root": os.getcwd(),
            "run_store_enabled": False,
            "workspace": {"isolated": False, "path": os.getcwd()},
            "max_repair_attempts": 1,
        })

        self.assertNotEqual(result["verification_verdict"], "PASS")
        self.assertEqual(result["verification_verdict"], "UNKNOWN")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["repair_attempts"], 1)

    @patch("orchestrator.graph.run_claude_code")
    @patch("orchestrator.graph.run_opencode")
    @patch("orchestrator.graph.run_antigravity")
    def test_multiple_agent_results_and_verification_history_preserved(
        self, mock_antigravity, mock_opencode, mock_claude
    ):
        """Test 11: All executions preserved in agent_results and verification_history."""
        mock_antigravity.return_value = "Research."
        mock_opencode.side_effect = ["Initial impl", "Repair impl"]
        mock_claude.side_effect = [
            "Plan",
            "VERDICT: FAIL\n\nSummary: Fail 1",
            "VERDICT: PASS\n\nSummary: Pass 2",
        ]

        result = graph.invoke({
            "task": "Test history preservation",
            "project_root": os.getcwd(),
            "run_store_enabled": False,
            "workspace": {"isolated": False, "path": os.getcwd()},
            "max_repair_attempts": 2,
        })

        agent_results = result.get("agent_results", [])
        # 1 antigravity + 1 claude planner + 1 opencode initial + 1 verifier 1 + 1 opencode repair + 1 verifier 2 = 6
        self.assertEqual(len(agent_results), 6)
        self.assertEqual(agent_results[0]["agent"], "antigravity")
        self.assertEqual(agent_results[1]["agent"], "claude")
        self.assertEqual(agent_results[2]["agent"], "opencode")
        self.assertEqual(agent_results[3]["agent"], "claude")
        self.assertEqual(agent_results[3]["verdict"], "FAIL")
        self.assertEqual(agent_results[4]["agent"], "opencode")
        self.assertEqual(agent_results[4]["repair_attempt"], 1)
        self.assertEqual(agent_results[5]["agent"], "claude")
        self.assertEqual(agent_results[5]["verdict"], "PASS")

        # Verification history
        history = result.get("verification_history", [])
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["attempt"], 1)
        self.assertEqual(history[0]["verdict"], "FAIL")
        self.assertEqual(history[1]["attempt"], 2)
        self.assertEqual(history[1]["verdict"], "PASS")


if __name__ == "__main__":
    unittest.main()
