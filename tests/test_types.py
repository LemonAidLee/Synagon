"""Unit tests for AgentResult data model and helper functions."""

import unittest

from orchestrator.types import (
    AgentResult,
    create_agent_result,
    get_agent_result,
)


class TestAgentResultTypes(unittest.TestCase):
    """Test suite for AgentResult structure and helper operations."""

    def test_create_agent_result(self):
        """Test constructing an AgentResult with required fields and model."""
        res = create_agent_result(
            agent="antigravity",
            role="researcher",
            status="success",
            output="Project analysis report.",
            duration_seconds=7.846,
            model="gemini-3.8-flash-high",
        )

        self.assertEqual(res["agent"], "antigravity")
        self.assertEqual(res["role"], "researcher")
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["output"], "Project analysis report.")
        self.assertEqual(res["duration_seconds"], 7.85)
        self.assertEqual(res["model"], "gemini-3.8-flash-high")

    def test_create_agent_result_default_model(self):
        """Test that model defaults to None when omitted."""
        res = create_agent_result(
            agent="claude",
            role="planner",
            status="success",
            output="Plan output",
        )
        self.assertIsNone(res["model"])

    def test_create_agent_result_negative_duration(self):
        """Test that negative durations are clamped to zero."""
        res = create_agent_result(
            agent="claude",
            role="planner",
            status="error",
            output="",
            duration_seconds=-3.5,
        )
        self.assertEqual(res["duration_seconds"], 0.0)

    def test_get_agent_result_empty(self):
        """Test retrieving from an empty list returns None."""
        self.assertIsNone(get_agent_result([], role="researcher"))

    def test_get_agent_result_by_role(self):
        """Test filtering results by role."""
        results: list[AgentResult] = [
            create_agent_result("antigravity", "researcher", "success", "research"),
            create_agent_result("claude", "planner", "success", "plan"),
        ]

        researcher = get_agent_result(results, role="researcher")
        self.assertIsNotNone(researcher)
        self.assertEqual(researcher["agent"], "antigravity")
        self.assertEqual(researcher["output"], "research")

        planner = get_agent_result(results, role="planner")
        self.assertIsNotNone(planner)
        self.assertEqual(planner["agent"], "claude")
        self.assertEqual(planner["output"], "plan")

        verifier = get_agent_result(results, role="verifier")
        self.assertIsNone(verifier)

    def test_get_agent_result_by_agent(self):
        """Test filtering results by agent."""
        results: list[AgentResult] = [
            create_agent_result("antigravity", "researcher", "success", "research"),
            create_agent_result("claude", "planner", "success", "plan"),
        ]

        agy = get_agent_result(results, agent="antigravity")
        self.assertIsNotNone(agy)
        self.assertEqual(agy["role"], "researcher")

        claude = get_agent_result(results, agent="claude")
        self.assertIsNotNone(claude)
        self.assertEqual(claude["role"], "planner")

    def test_get_agent_result_returns_most_recent(self):
        """Test that get_agent_result returns the latest result when multiple match."""
        results: list[AgentResult] = [
            create_agent_result("claude", "planner", "success", "first plan"),
            create_agent_result("claude", "planner", "success", "refined plan"),
        ]

        latest_planner = get_agent_result(results, role="planner")
        self.assertIsNotNone(latest_planner)
        self.assertEqual(latest_planner["output"], "refined plan")


if __name__ == "__main__":
    unittest.main()
