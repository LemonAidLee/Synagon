"""Unit and offline tests for Stage 7.6 token usage parsing, aggregation, and display."""

import unittest
from typing import Dict, Any, List

from orchestrator.types import (
    TokenUsage,
    AgentResult,
    create_token_usage,
    unavailable_token_usage,
    create_agent_result,
)
from orchestrator.metrics import (
    aggregate_metrics,
    format_token_count,
    format_summary_table,
)
from orchestrator.agents.antigravity import parse_antigravity_token_usage
from orchestrator.agents.claude_code import parse_claude_output
from orchestrator.agents.opencode import parse_opencode_output
from orchestrator.tracer import ExecutionTracer


class TestTokenUsageDataModel(unittest.TestCase):
    """Test TokenUsage creation, validation, and backward-compatible AgentResult."""

    def test_create_token_usage_full(self):
        usage = create_token_usage(input_tokens=100, output_tokens=50, total_tokens=150, available=True)
        self.assertEqual(usage["input_tokens"], 100)
        self.assertEqual(usage["output_tokens"], 50)
        self.assertEqual(usage["total_tokens"], 150)
        self.assertTrue(usage["available"])

    def test_create_token_usage_calculates_total_if_omitted(self):
        usage = create_token_usage(input_tokens=100, output_tokens=50, available=True)
        self.assertEqual(usage["total_tokens"], 150)
        self.assertTrue(usage["available"])

    def test_unavailable_token_usage(self):
        usage = unavailable_token_usage()
        self.assertFalse(usage["available"])
        self.assertIsNone(usage["input_tokens"])
        self.assertIsNone(usage["output_tokens"])
        self.assertIsNone(usage["total_tokens"])

    def test_agent_result_with_token_usage(self):
        usage = create_token_usage(200, 300, 500, available=True)
        res = create_agent_result(
            agent="antigravity",
            role="researcher",
            status="success",
            output="Report",
            duration_seconds=5.2,
            token_usage=usage,
        )
        self.assertEqual(res["agent"], "antigravity")
        self.assertIn("token_usage", res)
        self.assertTrue(res["token_usage"]["available"])
        self.assertEqual(res["token_usage"]["total_tokens"], 500)

    def test_agent_result_defaults_to_unavailable_token_usage_without_crashing(self):
        # Backward compatibility test: calling create_agent_result without token_usage
        res = create_agent_result(
            agent="claude",
            role="planner",
            status="success",
            output="Plan",
            duration_seconds=3.1,
        )
        self.assertIn("token_usage", res)
        self.assertFalse(res["token_usage"]["available"])
        self.assertIsNone(res["token_usage"]["total_tokens"])


class TestAntigravityTokenParsing(unittest.TestCase):
    """Test adapter parsing for Antigravity CLI output metadata."""

    def test_valid_usage_metadata(self):
        payload = {
            "status": "SUCCESS",
            "response": "Analysis complete",
            "usage": {
                "input_tokens": 9584,
                "output_tokens": 132,
                "thinking_tokens": 131,
                "cache_read_tokens": 8144,
                "total_tokens": 9716,
            }
        }
        usage = parse_antigravity_token_usage(payload)
        self.assertTrue(usage["available"])
        self.assertEqual(usage["input_tokens"], 9584)
        self.assertEqual(usage["output_tokens"], 132)
        self.assertEqual(usage["total_tokens"], 9716)

    def test_missing_usage(self):
        payload = {"status": "SUCCESS", "response": "Analysis"}
        usage = parse_antigravity_token_usage(payload)
        self.assertFalse(usage["available"])
        self.assertIsNone(usage["total_tokens"])

    def test_partial_usage_missing_total(self):
        payload = {
            "usage": {
                "input_tokens": 500,
                "output_tokens": 250,
            }
        }
        usage = parse_antigravity_token_usage(payload)
        self.assertTrue(usage["available"])
        self.assertEqual(usage["input_tokens"], 500)
        self.assertEqual(usage["output_tokens"], 250)
        self.assertEqual(usage["total_tokens"], 750)

    def test_malformed_usage_not_a_dict(self):
        payload = {"usage": "invalid string"}
        usage = parse_antigravity_token_usage(payload)
        self.assertFalse(usage["available"])

    def test_zero_tokens(self):
        payload = {
            "usage": {
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
            }
        }
        usage = parse_antigravity_token_usage(payload)
        self.assertTrue(usage["available"])
        self.assertEqual(usage["total_tokens"], 0)

    def test_unexpected_fields_ignored(self):
        payload = {
            "usage": {
                "input_tokens": 100,
                "output_tokens": 50,
                "total_tokens": 150,
                "custom_experimental_field": 99999,
                "billing_uuid": "xyz",
            }
        }
        usage = parse_antigravity_token_usage(payload)
        self.assertTrue(usage["available"])
        self.assertEqual(usage["total_tokens"], 150)


class TestClaudeCodeTokenParsing(unittest.TestCase):
    """Test adapter parsing for Claude Code CLI output metadata."""

    def test_valid_usage_json(self):
        stdout = (
            '{"result": "Plan verified.", "usage": {"input_tokens": 4010, "output_tokens": 1822}}'
        )
        text, usage = parse_claude_output(stdout)
        self.assertEqual(text, "Plan verified.")
        self.assertTrue(usage["available"])
        self.assertEqual(usage["input_tokens"], 4010)
        self.assertEqual(usage["output_tokens"], 1822)
        self.assertEqual(usage["total_tokens"], 5832)

    def test_fallback_plain_text(self):
        stdout = "Plan verified from plain text output."
        text, usage = parse_claude_output(stdout)
        self.assertEqual(text, "Plan verified from plain text output.")
        self.assertFalse(usage["available"])
        self.assertIsNone(usage["total_tokens"])

    def test_json_without_usage_field(self):
        stdout = '{"result": "No usage here", "duration_ms": 1200}'
        text, usage = parse_claude_output(stdout)
        self.assertEqual(text, "No usage here")
        self.assertFalse(usage["available"])

    def test_zero_tokens(self):
        stdout = '{"result": "Zero", "usage": {"input_tokens": 0, "output_tokens": 0}}'
        text, usage = parse_claude_output(stdout)
        self.assertEqual(text, "Zero")
        self.assertTrue(usage["available"])
        self.assertEqual(usage["total_tokens"], 0)

    def test_malformed_json_fallback(self):
        stdout = '{"result": "Broken JSON...'
        text, usage = parse_claude_output(stdout)
        self.assertEqual(text, '{"result": "Broken JSON...')
        self.assertFalse(usage["available"])


class TestOpenCodeTokenParsing(unittest.TestCase):
    """Test adapter parsing for OpenCode CLI output metadata."""

    def test_valid_single_step_json_events(self):
        stdout = (
            '{"type":"step_start","timestamp":100}\n'
            '{"type":"text","part":{"type":"text","text":"Changes applied."}}\n'
            '{"type":"step_finish","part":{"tokens":{"total":10079,"input":10063,"output":16}}}\n'
        )
        text, usage = parse_opencode_output(stdout)
        self.assertEqual(text, "Changes applied.")
        self.assertTrue(usage["available"])
        self.assertEqual(usage["input_tokens"], 10063)
        self.assertEqual(usage["output_tokens"], 16)
        self.assertEqual(usage["total_tokens"], 10079)

    def test_multi_step_json_events_accumulates_tokens(self):
        stdout = (
            '{"type":"text","part":{"type":"text","text":"Step 1 done. "}}\n'
            '{"type":"step_finish","part":{"tokens":{"total":500,"input":400,"output":100}}}\n'
            '{"type":"text","part":{"type":"text","text":"Step 2 done."}}\n'
            '{"type":"step_finish","part":{"tokens":{"total":600,"input":450,"output":150}}}\n'
        )
        text, usage = parse_opencode_output(stdout)
        self.assertEqual(text, "Step 1 done. Step 2 done.")
        self.assertTrue(usage["available"])
        self.assertEqual(usage["input_tokens"], 850)
        self.assertEqual(usage["output_tokens"], 250)
        self.assertEqual(usage["total_tokens"], 1100)

    def test_fallback_plain_text(self):
        stdout = "Implemented approved changes cleanly in workspace."
        text, usage = parse_opencode_output(stdout)
        self.assertEqual(text, "Implemented approved changes cleanly in workspace.")
        self.assertFalse(usage["available"])
        self.assertIsNone(usage["total_tokens"])

    def test_zero_tokens(self):
        stdout = (
            '{"type":"text","part":{"type":"text","text":"Zero tokens"}}\n'
            '{"type":"step_finish","part":{"tokens":{"total":0,"input":0,"output":0}}}\n'
        )
        text, usage = parse_opencode_output(stdout)
        self.assertEqual(text, "Zero tokens")
        self.assertTrue(usage["available"])
        self.assertEqual(usage["total_tokens"], 0)


class TestMetricsAggregation(unittest.TestCase):
    """Test aggregation of metrics across agent executions, repair attempts, and missing tokens."""

    def test_all_usage_available(self):
        results = [
            create_agent_result("antigravity", "researcher", "success", duration_seconds=10.0,
                                token_usage=create_token_usage(1000, 500, 1500, available=True)),
            create_agent_result("claude", "planner", "success", duration_seconds=8.0,
                                token_usage=create_token_usage(2000, 800, 2800, available=True)),
            create_agent_result("opencode", "implementer", "success", duration_seconds=30.0,
                                token_usage=create_token_usage(5000, 1200, 6200, available=True)),
            create_agent_result("claude", "verifier", "success", duration_seconds=5.0, verdict="PASS",
                                token_usage=create_token_usage(1500, 500, 2000, available=True)),
        ]
        metrics = aggregate_metrics(results)
        self.assertEqual(metrics["total_duration_seconds"], 53.0)
        self.assertEqual(metrics["known_input_tokens"], 9500)
        self.assertEqual(metrics["known_output_tokens"], 3000)
        self.assertEqual(metrics["known_total_tokens"], 12500)
        self.assertEqual(metrics["total_executions"], 4)
        self.assertEqual(metrics["executions_with_available_tokens"], 4)
        self.assertEqual(metrics["executions_with_unavailable_tokens"], 0)
        self.assertTrue(metrics["all_tokens_available"])
        self.assertEqual(metrics["verification_passes"], 1)
        self.assertEqual(metrics["verification_failures"], 0)

    def test_mixed_usage_some_unavailable(self):
        results = [
            create_agent_result("antigravity", "researcher", "success", duration_seconds=10.0,
                                token_usage=create_token_usage(1000, 500, 1500, available=True)),
            create_agent_result("claude", "planner", "success", duration_seconds=8.0,
                                token_usage=create_token_usage(2000, 800, 2800, available=True)),
            create_agent_result("opencode", "implementer", "success", duration_seconds=30.0,
                                token_usage=unavailable_token_usage()),
            create_agent_result("claude", "verifier", "success", duration_seconds=5.0, verdict="PASS",
                                token_usage=create_token_usage(1500, 500, 2000, available=True)),
        ]
        metrics = aggregate_metrics(results)
        self.assertEqual(metrics["known_total_tokens"], 6300)
        self.assertEqual(metrics["executions_with_available_tokens"], 3)
        self.assertEqual(metrics["executions_with_unavailable_tokens"], 1)
        self.assertFalse(metrics["all_tokens_available"])

    def test_all_usage_unavailable(self):
        results = [
            create_agent_result("antigravity", "researcher", "success", duration_seconds=10.0,
                                token_usage=unavailable_token_usage()),
            create_agent_result("claude", "planner", "success", duration_seconds=8.0,
                                token_usage=unavailable_token_usage()),
        ]
        metrics = aggregate_metrics(results)
        self.assertEqual(metrics["known_total_tokens"], 0)
        self.assertEqual(metrics["executions_with_available_tokens"], 0)
        self.assertEqual(metrics["executions_with_unavailable_tokens"], 2)
        self.assertFalse(metrics["all_tokens_available"])

    def test_repair_attempts_counted_correctly(self):
        results = [
            create_agent_result("antigravity", "researcher", "success", duration_seconds=10.0,
                                token_usage=create_token_usage(1000, 200, 1200, available=True)),
            create_agent_result("claude", "planner", "success", duration_seconds=5.0,
                                token_usage=create_token_usage(1500, 300, 1800, available=True)),
            create_agent_result("opencode", "implementer", "success", duration_seconds=20.0,
                                token_usage=create_token_usage(4000, 500, 4500, available=True)),
            create_agent_result("claude", "verifier", "success", duration_seconds=4.0, verdict="FAIL",
                                token_usage=create_token_usage(1000, 200, 1200, available=True)),
            # Repair attempt 1
            create_agent_result("opencode", "implementer", "success", duration_seconds=15.0, repair_attempt=1,
                                token_usage=create_token_usage(3000, 400, 3400, available=True)),
            create_agent_result("claude", "verifier", "success", duration_seconds=4.0, verdict="PASS", repair_attempt=1,
                                token_usage=create_token_usage(1200, 200, 1400, available=True)),
        ]
        metrics = aggregate_metrics(results)
        self.assertEqual(metrics["total_executions"], 6)
        self.assertEqual(metrics["repair_attempts"], 1)
        self.assertEqual(metrics["verification_passes"], 1)
        self.assertEqual(metrics["verification_failures"], 1)
        self.assertEqual(metrics["known_total_tokens"], 13500)


class TestDisplayFormatting(unittest.TestCase):
    """Test format_token_count and format_summary_table."""

    def test_format_token_count(self):
        self.assertEqual(format_token_count(8421, available=True), "8,421")
        self.assertEqual(format_token_count(0, available=True), "0")
        self.assertEqual(format_token_count(None, available=True), "unavailable")
        self.assertEqual(format_token_count(8421, available=False), "unavailable")

    def test_format_summary_table_all_available(self):
        results = [
            create_agent_result("antigravity", "researcher", "success", duration_seconds=15.8,
                                token_usage=create_token_usage(3201, 5220, 8421, available=True)),
            create_agent_result("claude", "planner", "success", duration_seconds=14.2,
                                token_usage=create_token_usage(4000, 1832, 5832, available=True)),
            create_agent_result("opencode", "implementer", "success", duration_seconds=72.1,
                                token_usage=create_token_usage(10000, 2947, 12947, available=True)),
            create_agent_result("claude", "verifier", "success", duration_seconds=11.2, verdict="PASS",
                                token_usage=create_token_usage(2000, 1106, 3106, available=True)),
        ]
        table = format_summary_table(results, verification_verdict="PASS", repair_attempts=0)
        self.assertIn("ORCHESTRATION COMPLETE", table)
        self.assertIn("Antigravity Researcher", table)
        self.assertIn("8,421", table)
        self.assertIn("Claude Planner", table)
        self.assertIn("5,832", table)
        self.assertIn("OpenCode Implementer", table)
        self.assertIn("12,947", table)
        self.assertIn("Claude Verifier", table)
        self.assertIn("3,106", table)
        self.assertIn("Total", table)
        self.assertIn("30,306", table)
        self.assertIn("Verification: PASS", table)
        self.assertIn("Repair attempts: 0", table)

    def test_format_summary_table_partial_unavailable(self):
        results = [
            create_agent_result("antigravity", "researcher", "success", duration_seconds=15.8,
                                token_usage=create_token_usage(3201, 5220, 8421, available=True)),
            create_agent_result("claude", "planner", "success", duration_seconds=14.2,
                                token_usage=create_token_usage(4000, 1832, 5832, available=True)),
            create_agent_result("opencode", "implementer", "success", duration_seconds=72.1,
                                token_usage=unavailable_token_usage()),
            create_agent_result("claude", "verifier", "success", duration_seconds=11.2, verdict="PASS",
                                token_usage=create_token_usage(2000, 1106, 3106, available=True)),
        ]
        table = format_summary_table(results, verification_verdict="PASS", repair_attempts=0)
        self.assertIn("OpenCode Implementer", table)
        self.assertIn("unavailable", table)
        self.assertIn("Known tokens:", table)
        self.assertIn("17,359", table)
        self.assertIn("Unavailable:", table)
        self.assertIn("1 execution", table)

    def test_tracer_records_token_telemetry(self):
        tracer = ExecutionTracer(verbose=False)
        tracer.log_agent_start("claude", "planner")
        tracer.log_agent_complete(
            agent="claude",
            role="planner",
            response_length=50,
            token_usage=create_token_usage(100, 50, 150, available=True),
        )
        events = tracer.get_events()
        self.assertEqual(len(events), 2)
        start_event = events[0]
        complete_event = events[1]
        self.assertEqual(complete_event.metadata["token_usage"]["total_tokens"], 150)
        self.assertEqual(complete_event.metadata["token_usage"]["input_tokens"], 100)
        self.assertEqual(complete_event.metadata["token_usage"]["output_tokens"], 50)


if __name__ == "__main__":
    unittest.main()
