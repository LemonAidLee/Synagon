"""Tests for an escalation ladder that can cross providers (`agent:` as a list).

`escalate_model` could only ever fall back *within one provider*. That was fine until the
failure it was built for turned out to be the provider itself: asked to investigate a
project, the researcher's CLI returned `status: SUCCESS` with an empty response on every
attempt, and a model ladder had nowhere to go. ARCHITECTURE.md §9.0 recorded the gap; this
is the code that closes it.

The property under test throughout is that **a rung is a pair**. An agent name and a model
id only mean something together - a model belongs to exactly one provider's catalog - so
the two lists are zipped, never clamped, and every consumer reads the pairs rather than
reasoning about the two lists separately.
"""

import shutil
import unittest
from unittest.mock import patch

import orchestrator.graph as graph_module
from orchestrator.cockpit import columns
from orchestrator.config import (
    ConfigValidationError,
    ladder_depth,
    ladder_rungs,
    rung_at,
    validate_config,
)
from orchestrator.graph import build_graph
from orchestrator.preflight import check_verifier_independence
from orchestrator.store import EVENT_AGENT_RETRY
from orchestrator.teams import normalize_team, validate_team
from orchestrator.tracer import default_tracer

from tests.support import (
    isolated_graph_state,
    redirected_run_store,
    temporary_store_dir,
)

ROLES = {
    r: {"responsibility": r}
    for r in ("researcher", "planner", "implementer", "verifier")
}


class RungsTest(unittest.TestCase):
    """`ladder_rungs` is the single place that knows how agent and model pair up."""

    def test_a_plain_entry_is_one_rung(self):
        self.assertEqual(
            ladder_rungs({"agent": "claude", "model": "sonnet"}), [("claude", "sonnet")]
        )

    def test_a_model_ladder_keeps_the_one_agent(self):
        self.assertEqual(
            ladder_rungs({"agent": "claude", "model": ["a", "b"]}),
            [("claude", "a"), ("claude", "b")],
        )

    def test_an_agent_ladder_with_no_models_uses_each_default(self):
        self.assertEqual(
            ladder_rungs({"agent": ["antigravity", "claude"]}),
            [("antigravity", None), ("claude", None)],
        )

    def test_two_ladders_are_zipped_rung_by_rung(self):
        self.assertEqual(
            ladder_rungs(
                {"agent": ["antigravity", "claude"], "model": ["gemini-x", "sonnet"]}
            ),
            [("antigravity", "gemini-x"), ("claude", "sonnet")],
        )

    def test_a_missing_agent_falls_back_to_claude_as_the_runner_always_did(self):
        self.assertEqual(ladder_rungs({"role": "verifier"}), [("claude", None)])

    def test_rung_at_clamps_to_the_last_rung(self):
        entry = {"agent": ["a", "b"], "model": ["x", "y"]}
        self.assertEqual(rung_at(entry, 0), ("a", "x"))
        self.assertEqual(rung_at(entry, 1), ("b", "y"))
        self.assertEqual(rung_at(entry, 99), ("b", "y"))
        self.assertEqual(rung_at(entry, -3), ("a", "x"))

    def test_depth_one_means_there_is_nowhere_to_escalate_to(self):
        self.assertEqual(ladder_depth({"agent": "claude", "model": "sonnet"}), 1)
        self.assertEqual(ladder_depth({"agent": ["antigravity", "claude"]}), 2)


class ValidationTest(unittest.TestCase):
    """Every rung is checked, and an unpairable ladder is refused rather than guessed at."""

    def _raw(self, researcher):
        return {
            "agents": [
                researcher,
                {"agent": "opencode", "model": "opencode/gpt-5.1-codex", "role": "implementer"},
                {"agent": "claude", "model": "sonnet", "role": "verifier"},
            ],
            "roles": dict(ROLES),
        }

    def test_a_valid_cross_provider_ladder_loads(self):
        config = validate_config(
            self._raw(
                {
                    "agent": ["antigravity", "claude"],
                    "model": ["gemini-3.8-flash-high", "sonnet"],
                    "role": "researcher",
                }
            )
        )
        self.assertEqual(
            ladder_rungs(config["agents"][0]),
            [("antigravity", "gemini-3.8-flash-high"), ("claude", "sonnet")],
        )

    def test_mismatched_ladder_lengths_are_refused(self):
        with self.assertRaises(ConfigValidationError) as ctx:
            validate_config(
                self._raw(
                    {
                        "agent": ["antigravity", "claude"],
                        "model": ["gemini-3.8-flash-high"],
                        "role": "researcher",
                    }
                )
            )
        self.assertIn("2 agent(s) and 1 model(s)", str(ctx.exception))

    def test_an_empty_agent_list_is_refused(self):
        with self.assertRaises(ConfigValidationError) as ctx:
            validate_config(self._raw({"agent": [], "role": "researcher"}))
        self.assertIn("empty 'agent' list", str(ctx.exception))

    def test_a_model_is_validated_against_its_own_rungs_agent(self):
        """`sonnet` is a Claude model; on the Antigravity rung it is not a model at all."""
        with self.assertRaises(ConfigValidationError) as ctx:
            validate_config(
                self._raw(
                    {
                        "agent": ["antigravity", "claude"],
                        "model": ["sonnet", "sonnet"],
                        "role": "researcher",
                    }
                )
            )
        message = str(ctx.exception)
        self.assertIn("Agent: antigravity", message)
        self.assertIn("escalation step 1 of 2", message)

    def test_an_agent_this_orchestrator_cannot_run_is_refused(self):
        """It used to fall through `get_runner` to Claude in silence, which a ladder makes
        worse: a mistyped fallback would 'escalate' to an agent nobody chose."""
        with self.assertRaises(ConfigValidationError) as ctx:
            validate_config(
                self._raw({"agent": ["antigravity", "cluade"], "role": "researcher"})
            )
        message = str(ctx.exception)
        self.assertIn("'cluade'", message)
        self.assertIn("escalation step 2 of 2", message)

    def test_a_single_unknown_agent_is_refused_too(self):
        with self.assertRaises(ConfigValidationError) as ctx:
            validate_config(self._raw({"agent": "gpt-cli", "role": "researcher"}))
        self.assertIn("not a provider in the model catalog", str(ctx.exception))


class PipelineTest(unittest.TestCase):
    """The graph escalates across providers, not just across models."""

    def setUp(self):
        default_tracer.clear()
        self.store_dir = temporary_store_dir()
        self.addCleanup(shutil.rmtree, self.store_dir, ignore_errors=True)

    def run_with(self, researcher_ladder, replies, retry=None):
        """Run the pipeline with a researcher ladder and a scripted sequence of replies.

        Returns the list of ``(agent, model)`` pairs the researcher phase actually invoked.
        """
        raw = {
            "agents": [
                dict(researcher_ladder, role="researcher"),
                {"agent": "claude", "model": "sonnet", "role": "planner"},
                {"agent": "opencode", "model": "opencode/gpt-5.1-codex", "role": "implementer"},
                {"agent": "claude", "model": "sonnet", "role": "verifier"},
            ],
            "roles": dict(ROLES),
            "preflight": {"enabled": False},
            "execution": {"retry": dict(retry or {"attempts": 3, "backoff_seconds": 0})},
        }
        config = redirected_run_store(validate_config(raw), self.store_dir)

        invoked = []
        script = list(replies)
        downstream = ["plan", "VERDICT: PASS\nlooks right"]

        def researcher(agent_name):
            def runner(prompt, **kwargs):
                invoked.append((agent_name, kwargs.get("model")))
                value = script[min(len(invoked) - 1, len(script) - 1)]
                if isinstance(value, Exception):
                    raise value
                return value

            return runner

        def claude(prompt, **kwargs):
            # Claude is both a ladder rung for the researcher and the real planner and
            # verifier, so which it is depends on the role it was handed.
            if kwargs.get("role") == "researcher":
                return researcher("claude")(prompt, **kwargs)
            return downstream.pop(0) if downstream else "VERDICT: PASS"

        with patch.object(
            graph_module, "run_antigravity", side_effect=researcher("antigravity")
        ), patch.object(graph_module, "run_claude_code", side_effect=claude), patch.object(
            graph_module, "run_opencode", return_value="implementation"
        ):
            state = isolated_graph_state(config, "ladder under test", self.store_dir)
            state["config"] = config
            result = build_graph(config).invoke(state)
        return invoked, result

    def test_a_dead_provider_is_escalated_past(self):
        """The failure §9.0 diagnosed: every attempt on one provider returns nothing."""
        invoked, state = self.run_with(
            {"agent": ["antigravity", "claude"], "model": ["gemini-3.8-flash-high", "sonnet"]},
            ["", "", "real research"],
        )
        self.assertEqual(
            invoked,
            [
                ("antigravity", "gemini-3.8-flash-high"),
                ("claude", "sonnet"),
                ("claude", "sonnet"),
            ],
        )
        researcher = [
            r for r in (state.get("agent_results") or []) if r.get("role") == "researcher"
        ]
        self.assertEqual(len(researcher), 1)
        self.assertEqual(researcher[0]["status"], "success")
        self.assertEqual(researcher[0]["agent"], "claude")

    def test_the_phase_still_produces_exactly_one_result(self):
        """A cross-provider retry is no more an ensemble than a same-provider one."""
        _, state = self.run_with(
            {"agent": ["antigravity", "claude"], "model": ["gemini-3.8-flash-high", "sonnet"]},
            ["", "research"],
        )
        self.assertEqual(len(state.get("agent_results") or []), 4)

    def test_the_last_rung_is_reused_rather_than_running_off_the_end(self):
        invoked, _ = self.run_with(
            {"agent": ["antigravity", "claude"], "model": ["gemini-3.8-flash-high", "sonnet"]},
            [""],
            retry={"attempts": 4, "backoff_seconds": 0},
        )
        self.assertEqual([a for a, _m in invoked], ["antigravity", "claude", "claude", "claude"])

    def test_escalation_off_keeps_retrying_the_same_agent(self):
        invoked, _ = self.run_with(
            {"agent": ["antigravity", "claude"], "model": ["gemini-3.8-flash-high", "sonnet"]},
            ["", "", "research"],
            retry={"attempts": 3, "backoff_seconds": 0, "escalate_model": False},
        )
        self.assertEqual({a for a, _m in invoked}, {"antigravity"})

    def test_the_run_records_which_agent_it_fell_back_to(self):
        """Deriving it from the following result would only work when the fallback worked."""
        import json
        import os

        from orchestrator.store import runs_root

        self.run_with(
            {"agent": ["antigravity", "claude"], "model": ["gemini-3.8-flash-high", "sonnet"]},
            ["", "research"],
        )
        root = runs_root(os.getcwd(), self.store_dir)
        run_dir = sorted(p for p in root.iterdir() if p.is_dir())[-1]
        events = [
            json.loads(line)
            for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        retries = [e for e in events if e.get("event") == EVENT_AGENT_RETRY]
        self.assertEqual(len(retries), 1)
        self.assertEqual(retries[0]["agent"], "antigravity")
        self.assertEqual(retries[0]["next_agent"], "claude")
        self.assertEqual(retries[0]["next_model"], "sonnet")

    def test_the_console_names_the_provider_it_is_switching_to(self):
        self.run_with(
            {"agent": ["antigravity", "claude"], "model": ["gemini-3.8-flash-high", "sonnet"]},
            ["", "research"],
        )
        retries = [e for e in default_tracer.get_events() if e.name == "agent_retry"]
        self.assertEqual(len(retries), 1)
        self.assertIn("-> claude/sonnet", retries[0].message)


class PreflightTest(unittest.TestCase):
    """A fallback is only ever reached on a bad day, which is a bad day to find it missing."""

    def _config(self, agents):
        return validate_config(
            {"agents": agents, "roles": dict(ROLES), "preflight": {"enabled": True}}
        )

    def test_every_rung_is_probed(self):
        from orchestrator.preflight import run_preflight

        config = self._config(
            [
                {
                    "agent": ["antigravity", "claude"],
                    "model": ["gemini-3.8-flash-high", "sonnet"],
                    "role": "researcher",
                },
                {"agent": "opencode", "model": "opencode/gpt-5.1-codex", "role": "implementer"},
                {"agent": "claude", "model": "sonnet", "role": "verifier"},
            ]
        )
        report = run_preflight(config, deep=False)
        researcher_probes = [p for p in report["probes"] if p["role"] == "researcher"]
        self.assertEqual(len(researcher_probes), 2)
        self.assertEqual([p["agent"] for p in researcher_probes], ["antigravity", "claude"])
        self.assertEqual([p["ladder_rung"] for p in researcher_probes], [0, 1])

    def test_independence_is_lost_when_a_ladder_escalates_onto_the_implementer(self):
        config = self._config(
            [
                {"agent": "opencode", "model": "opencode/gpt-5.1-codex", "role": "implementer"},
                {
                    "agent": ["claude", "opencode"],
                    "model": ["sonnet", "opencode/gpt-5.1-codex"],
                    "role": "verifier",
                },
            ]
        )
        warnings = check_verifier_independence(config)
        self.assertEqual(len(warnings), 1)
        self.assertIn("on escalation", warnings[0])
        self.assertIn("opencode/opencode/gpt-5.1-codex", warnings[0])

    def test_an_independent_ladder_warns_about_nothing(self):
        config = self._config(
            [
                {"agent": "opencode", "model": "opencode/gpt-5.1-codex", "role": "implementer"},
                {
                    "agent": ["claude", "antigravity"],
                    "model": ["sonnet", "gemini-3.8-flash-high"],
                    "role": "verifier",
                },
            ]
        )
        self.assertEqual(check_verifier_independence(config), [])


class CockpitTest(unittest.TestCase):
    """One card, possibly worked by more than one agent."""

    TEAM = {
        "agents": [
            {
                "agent": ["antigravity", "claude"],
                "model": ["gemini-3.8-flash-high", "sonnet"],
                "role": "researcher",
            }
        ]
    }

    def test_a_ladder_card_names_both_agents(self):
        card = columns(self.TEAM)[0]["agents"][0]
        self.assertEqual(card["agent_label"], "antigravity -> claude")
        self.assertTrue(card["ladder"])
        self.assertEqual(card["ladder_depth"], 2)
        self.assertEqual(card["state"], "idle")

    def test_the_card_follows_the_rung_the_run_reached(self):
        events = [
            {
                "sequence": 1,
                "event": "agent_started",
                "agent": "antigravity",
                "role": "researcher",
            },
            {"sequence": 2, "event": "agent_started", "agent": "claude", "role": "researcher"},
            {
                "sequence": 3,
                "event": "agent_result",
                "result": {
                    "agent": "claude",
                    "role": "researcher",
                    "status": "success",
                    "output": "research",
                },
            },
        ]
        card = columns(self.TEAM, events)[0]["agents"][0]
        self.assertEqual(card["state"], "done")
        self.assertEqual(card["agent"], "claude")

    def test_a_card_whose_first_rung_is_still_working_reads_as_working(self):
        events = [
            {
                "sequence": 1,
                "event": "agent_started",
                "agent": "antigravity",
                "role": "researcher",
            }
        ]
        card = columns(self.TEAM, events)[0]["agents"][0]
        self.assertEqual(card["state"], "working")
        self.assertEqual(card["agent"], "antigravity")


class DesignSurfaceTest(unittest.TestCase):
    """The editor validates a ladder before the config loader ever sees it."""

    CONFIG = {
        "models": {
            "claude": [{"id": "sonnet", "name": "Sonnet"}],
            "antigravity": [{"id": "gemini-3.8-flash-high", "name": "Gemini"}],
            "opencode": [{"id": "opencode/gpt-5.1-codex", "name": "Big Pickle"}],
        },
        "roles": dict(ROLES),
    }

    def test_an_agent_ladder_survives_normalization(self):
        team = normalize_team(
            {
                "agents": [
                    {
                        "agent": ["antigravity", "claude"],
                        "model": ["gemini-3.8-flash-high", "sonnet"],
                        "role": "researcher",
                    }
                ]
            }
        )
        self.assertEqual(team["agents"][0]["agent"], ["antigravity", "claude"])

    def test_a_one_entry_agent_list_collapses_to_a_plain_string(self):
        team = normalize_team(
            {"agents": [{"agent": ["claude"], "model": "sonnet", "role": "verifier"}]}
        )
        self.assertEqual(team["agents"][0]["agent"], "claude")

    def test_the_editor_reports_a_model_on_the_wrong_rung(self):
        team = normalize_team(
            {
                "agents": [
                    {
                        "agent": ["antigravity", "claude"],
                        "model": ["sonnet", "sonnet"],
                        "role": "implementer",
                    },
                    {"agent": "claude", "model": "sonnet", "role": "verifier"},
                ]
            }
        )
        problems = validate_team(team, self.CONFIG)
        self.assertTrue(any("step 1" in p and "antigravity" in p for p in problems))

    def test_the_editor_reports_mismatched_ladder_lengths(self):
        team = normalize_team(
            {
                "agents": [
                    {
                        "agent": ["antigravity", "claude"],
                        "model": ["gemini-3.8-flash-high"],
                        "role": "implementer",
                    },
                    {"agent": "claude", "model": "sonnet", "role": "verifier"},
                ]
            }
        )
        problems = validate_team(team, self.CONFIG)
        self.assertTrue(any("same length" in p for p in problems))


if __name__ == "__main__":
    unittest.main()
