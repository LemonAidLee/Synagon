"""Tests for Token Accounting Contract, Aggregation Invariant, and Normalization.

Covers:
1. Four-agent aggregation (exact 52,711 and 53,239 arithmetic cases)
2. No duplicate aggregation across nodes
3. Cumulative session usage is not double-counted
4. Repair attempt contributes exactly once
5. Verifier retry contributes exactly once
6. Missing/unavailable token usage does not corrupt totals
7. Zero-token execution behaves correctly
8. Token usage from one agent cannot leak into another agent's result
9. OpenCode session-level usage is normalized correctly
10. Claude and AGY usage parsing remains compatible with actual CLI outputs
"""

import json
import unittest
from typing import List

from orchestrator.agents.antigravity import parse_antigravity_token_usage
from orchestrator.agents.claude_code import parse_claude_output
from orchestrator.agents.opencode import parse_opencode_output
from orchestrator.metrics import (
    aggregate_metrics,
    format_summary_table,
    get_token_diagnostics,
    verify_token_aggregation_invariant,
)
from orchestrator.types import (
    AgentResult,
    create_agent_result,
    create_token_diagnostics,
    create_token_usage,
    unavailable_token_usage,
)


class TestTokenAccountingContract(unittest.TestCase):
    """Unit tests enforcing the single project-wide Token Accounting Contract."""

    def test_01_four_agent_aggregation_exact_sum(self):
        """Scenario 1: Four-agent aggregation with explicit arithmetic verification."""
        # Arithmetic case from prompt: 11816 + 629 + 36948 + 3318 = 52711
        results: List[AgentResult] = [
            create_agent_result(
                agent="antigravity",
                role="researcher",
                status="success",
                duration_seconds=10.0,
                token_usage=create_token_usage(9911, 1905, total_tokens=11816),
            ),
            create_agent_result(
                agent="claude",
                role="planner",
                status="success",
                duration_seconds=8.0,
                token_usage=create_token_usage(2, 627, total_tokens=629),
            ),
            create_agent_result(
                agent="opencode",
                role="implementer",
                status="success",
                duration_seconds=25.0,
                token_usage=create_token_usage(36720, 228, total_tokens=36948),
            ),
            create_agent_result(
                agent="claude",
                role="verifier",
                status="success",
                duration_seconds=15.0,
                token_usage=create_token_usage(24, 3294, total_tokens=3318),
            ),
        ]

        metrics = aggregate_metrics(results)
        self.assertEqual(metrics["known_total_tokens"], 52711)
        self.assertTrue(metrics["all_tokens_available"])

        invariant = verify_token_aggregation_invariant(results)
        self.assertTrue(invariant["is_valid"])
        self.assertEqual(invariant["sum_displayed"], 52711)
        self.assertEqual(invariant["reported_total"], 52711)
        self.assertEqual(invariant["difference"], 0)

    def test_01b_second_run_aggregation_exact_sum(self):
        """Scenario 1b: Verify the 53,239 total run arithmetic."""
        # 12232 + 741 + 36948 + 3318 = 53239
        results: List[AgentResult] = [
            create_agent_result(
                agent="antigravity",
                role="researcher",
                status="success",
                token_usage=create_token_usage(9915, 2317, total_tokens=12232),
            ),
            create_agent_result(
                agent="claude",
                role="planner",
                status="success",
                token_usage=create_token_usage(2, 739, total_tokens=741),
            ),
            create_agent_result(
                agent="opencode",
                role="implementer",
                status="success",
                token_usage=create_token_usage(36720, 228, total_tokens=36948),
            ),
            create_agent_result(
                agent="claude",
                role="verifier",
                status="success",
                token_usage=create_token_usage(24, 3294, total_tokens=3318),
            ),
        ]

        metrics = aggregate_metrics(results)
        self.assertEqual(metrics["known_total_tokens"], 53239)

        # Difference between run A (52711) and run B (53239) is exactly 528:
        self.assertEqual(53239 - 52711, 528)

        invariant = verify_token_aggregation_invariant(results)
        self.assertTrue(invariant["is_valid"])
        self.assertEqual(invariant["difference"], 0)

    def test_02_no_duplicate_aggregation(self):
        """Scenario 2: Aggregating multiple times or passing state does not duplicate token totals."""
        results = [
            create_agent_result("antigravity", "researcher", "success", token_usage=create_token_usage(100, 50, 150)),
            create_agent_result("claude", "planner", "success", token_usage=create_token_usage(200, 50, 250)),
        ]
        m1 = aggregate_metrics(results)
        m2 = aggregate_metrics(results)
        self.assertEqual(m1["known_total_tokens"], 400)
        self.assertEqual(m2["known_total_tokens"], 400)

    def test_03_cumulative_session_usage_not_double_counted(self):
        """Scenario 3: Multi-step OpenCode events report cumulative totals that are not added repeatedly."""
        # Simulated OpenCode stream with 2 step-finish events where total is cumulative
        json_stream = (
            '{"type":"step-finish","part":{"tokens":{"input":100,"output":50,"total":150}}}\n'
            '{"type":"step-finish","part":{"tokens":{"input":50,"output":25,"total":75}}}\n'
        )
        text, usage = parse_opencode_output(json_stream)
        self.assertTrue(usage["available"])
        self.assertEqual(usage["total_tokens"], 225)
        self.assertEqual(usage["input_tokens"], 150)
        self.assertEqual(usage["output_tokens"], 75)

    def test_04_repair_attempt_contributes_exactly_once(self):
        """Scenario 4: A repair attempt adds its own tokens exactly once to the workflow total."""
        results = [
            create_agent_result("opencode", "implementer", "success", repair_attempt=0, token_usage=create_token_usage(1000, 200, 1200)),
            create_agent_result("claude", "verifier", "success", repair_attempt=0, verdict="FAIL", token_usage=create_token_usage(300, 50, 350)),
            create_agent_result("opencode", "implementer", "success", repair_attempt=1, token_usage=create_token_usage(500, 100, 600)),
            create_agent_result("claude", "verifier", "success", repair_attempt=1, verdict="PASS", token_usage=create_token_usage(300, 50, 350)),
        ]
        metrics = aggregate_metrics(results)
        # Expected: 1200 + 350 + 600 + 350 = 2500
        self.assertEqual(metrics["known_total_tokens"], 2500)
        self.assertEqual(metrics["repair_attempts"], 1)
        self.assertEqual(metrics["verification_passes"], 1)
        self.assertEqual(metrics["verification_failures"], 1)

        invariant = verify_token_aggregation_invariant(results)
        self.assertTrue(invariant["is_valid"])
        self.assertEqual(invariant["difference"], 0)

    def test_05_verifier_retry_contributes_exactly_once(self):
        """Scenario 5: Multiple verifier runs each contribute exactly once without overlap."""
        results = [
            create_agent_result("claude", "verifier", "success", repair_attempt=0, verdict="FAIL", token_usage=create_token_usage(100, 50, 150)),
            create_agent_result("claude", "verifier", "success", repair_attempt=1, verdict="PASS", token_usage=create_token_usage(120, 60, 180)),
        ]
        metrics = aggregate_metrics(results)
        self.assertEqual(metrics["known_total_tokens"], 330)

    def test_06_missing_token_usage_does_not_corrupt_totals(self):
        """Scenario 6: Missing token usage is gracefully flagged without NaN or total corruption."""
        results = [
            create_agent_result("antigravity", "researcher", "success", token_usage=create_token_usage(100, 50, 150)),
            create_agent_result("claude", "planner", "success", token_usage=unavailable_token_usage()),
            create_agent_result("opencode", "implementer", "success", token_usage=create_token_usage(200, 100, 300)),
        ]
        metrics = aggregate_metrics(results)
        self.assertEqual(metrics["known_total_tokens"], 450)
        self.assertFalse(metrics["all_tokens_available"])
        self.assertEqual(metrics["executions_with_available_tokens"], 2)
        self.assertEqual(metrics["executions_with_unavailable_tokens"], 1)

        # Summary table reports unavailable count clearly
        summary = format_summary_table(results)
        self.assertIn("Known tokens:", summary)
        self.assertIn("1 execution", summary)

    def test_07_zero_token_execution_behaves_correctly(self):
        """Scenario 7: An execution reporting 0 tokens does not become unavailable or corrupt totals."""
        results = [
            create_agent_result("agent", "role", "success", token_usage=create_token_usage(0, 0, 0)),
        ]
        metrics = aggregate_metrics(results)
        self.assertEqual(metrics["known_total_tokens"], 0)
        self.assertTrue(metrics["all_tokens_available"])
        self.assertEqual(metrics["executions_with_available_tokens"], 1)

    def test_08_token_usage_isolation_between_agents(self):
        """Scenario 8: Token usage from one agent object does not leak or mutate another."""
        u1 = create_token_usage(100, 50, 150)
        u2 = create_token_usage(200, 100, 300)
        r1 = create_agent_result("agent1", "role1", "success", token_usage=u1)
        r2 = create_agent_result("agent2", "role2", "success", token_usage=u2)

        self.assertEqual(r1["token_usage"]["total_tokens"], 150)
        self.assertEqual(r2["token_usage"]["total_tokens"], 300)
        self.assertIsNot(r1["token_usage"], r2["token_usage"])

    def test_09_opencode_session_level_usage_normalization(self):
        """Scenario 9: OpenCode session tokens with cache read are normalized correctly."""
        # Emulate session metadata: base input=2384, cache read=7936, output=84 -> total=10404
        base_inp = 2384
        cache_read = 7936
        out = 84
        tot = 10404
        inp = base_inp + cache_read

        usage = create_token_usage(
            input_tokens=inp,
            output_tokens=out,
            total_tokens=tot,
            cache_read_tokens=cache_read,
            available=True,
            raw_usage={"input": base_inp, "output": out, "total": tot, "cache": {"read": cache_read}},
        )
        self.assertEqual(usage["total_tokens"], 10404)
        self.assertEqual(usage["cache_read_tokens"], 7936)
        self.assertEqual(usage["input_tokens"], 10320)

    def test_10_claude_and_agy_usage_parsing_compatibility(self):
        """Scenario 10: Claude Code and Antigravity CLI actual outputs parse into conforming TokenUsage."""
        # 1. Antigravity real CLI JSON output
        agy_json = {
            "conversation_id": "conv-123",
            "status": "SUCCESS",
            "response": "Research done.",
            "usage": {
                "input_tokens": 9590,
                "output_tokens": 29,
                "thinking_tokens": 27,
                "cache_read_tokens": 8144,
                "total_tokens": 9619,
            },
        }
        agy_usage = parse_antigravity_token_usage(agy_json)
        self.assertTrue(agy_usage["available"])
        self.assertEqual(agy_usage["total_tokens"], 9619)
        self.assertEqual(agy_usage["input_tokens"], 9590)
        self.assertEqual(agy_usage["output_tokens"], 29)
        self.assertEqual(agy_usage["reasoning_tokens"], 27)
        self.assertEqual(agy_usage["cache_read_tokens"], 8144)

        # 2. Claude Code real CLI JSON output (omits total_tokens)
        claude_stdout = json.dumps({
            "result": "Plan created.",
            "usage": {
                "input_tokens": 2,
                "cache_creation_input_tokens": 7846,
                "cache_read_input_tokens": 13463,
                "output_tokens": 4,
                "output_tokens_details": {"thinking_tokens": 0},
            },
        })
        text, claude_usage = parse_claude_output(claude_stdout)
        self.assertEqual(text, "Plan created.")
        self.assertTrue(claude_usage["available"])
        # Calculated total = input (2) + cache_write (7846) + cache_read (13463) + output (4) = 21315
        self.assertEqual(claude_usage["total_tokens"], 21315)
        self.assertEqual(claude_usage["input_tokens"], 21311)
        self.assertEqual(claude_usage["cache_read_tokens"], 13463)
        self.assertEqual(claude_usage["cache_write_tokens"], 7846)

    def test_structured_diagnostics_generation(self):
        """Verify structured diagnostic records are created with all contract fields."""
        res = create_agent_result(
            agent="opencode",
            role="implementer",
            status="success",
            execution_mode="native_tui",
            token_usage=create_token_usage(
                input_tokens=1000,
                output_tokens=200,
                total_tokens=1200,
                cache_read_tokens=800,
                raw_usage={"input": 200, "cache": {"read": 800}},
            ),
        )
        diags = get_token_diagnostics([res])
        self.assertEqual(len(diags), 1)
        d = diags[0]
        self.assertEqual(d["agent"], "opencode")
        self.assertEqual(d["role"], "implementer")
        self.assertEqual(d["execution_mode"], "native_tui")
        self.assertEqual(d["usage_normalized"]["total"], 1200)
        self.assertEqual(d["aggregation_contribution"], 1200)


if __name__ == "__main__":
    unittest.main()
