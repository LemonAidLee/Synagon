"""Tests for format_budget_status() in orchestrator/budget.py.

Covers:
1. No budget configured - both ceilings unlimited, no reason sentence
2. Under both ceilings - spend and elapsed within limits, no reason
3. Token ceiling exhausted - reason names the token overrun
4. Time ceiling exhausted - reason names the time overrun
5. Unavailable token_usage contributes zero to the rendered spend

All now/run_started_at values are injected as literals (never wall-clock) so
the tests stay deterministic, consistent with evaluate_budget's testability.
"""

import unittest

from orchestrator.budget import format_budget_status
from orchestrator.types import (
    create_agent_result,
    create_token_usage,
    unavailable_token_usage,
)


class TestFormatBudgetStatus(unittest.TestCase):
    """Unit tests for the single-call budget status helper."""

    def test_no_budget_configured(self):
        line = format_budget_status({})
        self.assertIn("Budget:", line)
        # Both ceilings default to 0, i.e. no limit. The "not configured"
        # sentinel in describe_budget is unreachable through this composition.
        self.assertEqual(line.count("(no limit)"), 2)
        self.assertNotIn("exhausted", line)

    def test_under_both_ceilings(self):
        result = create_agent_result(
            agent="test",
            role="researcher",
            status="success",
            token_usage=create_token_usage(total_tokens=100),
        )
        state = {
            "agent_results": [result],
            "run_started_at": 1000.0,
        }
        budget = {"max_total_tokens": 1000, "max_duration_seconds": 60}
        line = format_budget_status(state, budget=budget, now=1010.0)
        self.assertIn("100 tokens / 1,000", line)
        self.assertIn("10s / 60s", line)
        self.assertNotIn("exhausted", line)

    def test_token_ceiling_exhausted(self):
        result = create_agent_result(
            agent="test",
            role="researcher",
            status="success",
            token_usage=create_token_usage(total_tokens=120),
        )
        state = {
            "agent_results": [result],
            "run_started_at": 1000.0,
        }
        budget = {"max_total_tokens": 100}
        line = format_budget_status(state, budget=budget, now=1010.0)
        self.assertIn("token budget exhausted", line)

    def test_time_ceiling_exhausted(self):
        state = {
            "agent_results": [],
            "run_started_at": 1000.0,
        }
        # No max_total_tokens so the token check (checked first via `elif`) does
        # not fire and mask the time reason we are asserting on.
        budget = {"max_duration_seconds": 60}
        line = format_budget_status(state, budget=budget, now=1070.0)
        self.assertIn("time budget exhausted", line)

    def test_unavailable_token_usage_contributes_zero(self):
        results = [
            create_agent_result(
                agent="unavailable",
                role="researcher",
                status="success",
                token_usage=unavailable_token_usage(),
            ),
            create_agent_result(
                agent="known",
                role="researcher",
                status="success",
                token_usage=create_token_usage(total_tokens=50),
            ),
        ]
        state = {"agent_results": results, "run_started_at": 1000.0}
        budget = {"max_total_tokens": 1000, "max_duration_seconds": 60}
        line = format_budget_status(state, budget=budget, now=1010.0)
        self.assertIn("50 tokens", line)
        self.assertNotIn("(no limit)", line)


if __name__ == "__main__":
    unittest.main()
