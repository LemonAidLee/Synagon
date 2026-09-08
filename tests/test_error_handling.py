"""Tests for error handling across the orchestrator.

These tests pin every graph to an explicit configuration built through
``build_graph`` rather than the module-level ``graph``, whose topology is baked
from whatever ``orchestrator.yaml`` happens to be on disk at first import. That
makes the error-handling contract here independent of the project's current team
(a test about "errors are caught and classified" must not silently become a test
about "which agents are in the pipeline today").

The contract being pinned down:

* A failing agent is recorded as an error *fact* (an ``agent_result`` with
  ``status="error"``), never an uncaught crash of the graph run.
* An exception with an empty ``str()`` (e.g. ``StopIteration``) still yields an
  informative error message - the type name is always recorded.
* A phase is fatal only when *every* member of it failed. One dead member of an
  ensemble is survivable (Tier 2 #8).
* CLI/provider failures arrive through the typed ``CLIError`` hierarchy and are
  absorbed into error results, not re-raised.
* ``status.py`` derives ``STATUS_ERROR`` from those facts; it never trusts a
  stored status string.
* ``config.load_config`` distinguishes a YAML syntax failure from an I/O failure.
* ``teams.write_team`` is atomic: a write that dies part-way leaves the original
  file exactly as it was, backed up, not truncated.
"""

import copy
import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

import orchestrator.config as config_module
import orchestrator.graph as graph_module
from orchestrator.config import DEFAULT_CONFIG, ConfigValidationError, load_config
from orchestrator.agents.exceptions import CLIError, CLIExecutionError
from orchestrator.graph import build_graph
from orchestrator.status import (
    STATUS_COMPLETED,
    STATUS_ERROR,
    derive_status,
    derive_summary,
    has_error,
    is_terminal,
)
from orchestrator.serve import serve_board
from orchestrator import teams as teams_module
from orchestrator.teams import (
    get_template,
    normalize_team,
    write_team,
)

#: A pipeline with one of each role - claude is invoked exactly twice.
BASE_CONFIG = copy.deepcopy(DEFAULT_CONFIG)
BASE_CONFIG["agents"] = [
    {"agent": "antigravity", "model": "gemini-3.8-flash-high", "role": "researcher"},
    {"agent": "claude", "model": "sonnet", "role": "planner"},
    {"agent": "opencode", "model": "opencode/gpt-5.1-codex", "role": "implementer"},
    {"agent": "claude", "model": "sonnet", "role": "verifier"},
]

#: A pipeline with an ensemble: two researchers run side by side.
ENSEMBLE_CONFIG = copy.deepcopy(DEFAULT_CONFIG)
ENSEMBLE_CONFIG["agents"] = [
    {"agent": "antigravity", "model": "gemini-3.8-flash-high", "role": "researcher"},
    {"agent": "claude", "model": "sonnet", "role": "researcher"},
    {"agent": "claude", "model": "sonnet", "role": "planner"},
    {"agent": "opencode", "model": "opencode/gpt-5.1-codex", "role": "implementer"},
    {"agent": "claude", "model": "sonnet", "role": "verifier"},
]

MINIMAL_YAML = """\
# The team
# ------------------------------------------------------------------
agents:
  - agent: opencode
    model: opencode/gpt-5.1-codex
    role: implementer

  - agent: claude
    model: sonnet
    role: verifier

# How hard it tries
max_repair_attempts: 2

# Who decides
verification:
  consensus: unanimous

roles:
  implementer:
    responsibility: write the code
  verifier:
    responsibility: check the code
"""


def _role_outputs(role_map):
    """Return a runner mock that answers by role instead of consuming a list."""
    def runner(prompt, **kwargs):
        return role_map.get(kwargs.get("role"), "output")
    return runner


def _happy_claude():
    """Return a claude runner that plans and then passes verification."""
    return _role_outputs({"planner": "plan", "verifier": "VERDICT: PASS\nok."})


class ErrorHandlingGraphTest(unittest.TestCase):
    """The graph's catch-all: errors become facts, never crashes."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="orch_err_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _state(self, config=None, **extra):
        state = {
            "task": "error handling",
            "project_root": os.getcwd(),
            "config": config or BASE_CONFIG,
            "run_store_enabled": False,
            "agent_results": [],
            "workspace": {"isolated": False, "path": os.getcwd()},
        }
        state.update(extra)
        return state

    def test_a_failing_agent_produces_an_error_result_not_a_crash(self):
        """A RuntimeError in one node is recorded as a fact; the run still terminates."""
        with patch.object(graph_module, "run_antigravity", return_value="analysis"), \
             patch.object(graph_module, "run_claude_code",
                          side_effect=_failing_then_passing()), \
             patch.object(graph_module, "run_opencode", return_value="implementation"):
            result = build_graph(BASE_CONFIG).invoke(self._state())

        planner_res = next(
            r for r in result["agent_results"] if r.get("role") == "planner"
        )
        self.assertEqual(planner_res["status"], "error")
        self.assertIn("RuntimeError", planner_res["output"])
        self.assertIn("node error", planner_res["output"])
        self.assertIn("CLI setup", planner_res["output"])

        # The fatal flag is a fact; the sync node decided this phase could not continue.
        self.assertEqual(result["status"], STATUS_ERROR)
        self.assertTrue(has_error(result))

    def test_an_exception_with_empty_str_still_yields_an_informative_error(self):
        """Regression: `str(SomeException()) == ''` used to log a bare 'node error: '."""
        with patch.object(graph_module, "run_antigravity", return_value="analysis"), \
             patch.object(graph_module, "run_claude_code", side_effect=_empty_str_exception()), \
             patch.object(graph_module, "run_opencode", return_value="implementation"):
            result = build_graph(BASE_CONFIG).invoke(self._state())

        verifier_res = next(
            r for r in result["agent_results"] if r.get("role") == "verifier"
        )
        self.assertEqual(verifier_res["status"], "error")
        self.assertIn("StopIteration", verifier_res["output"])
        self.assertNotEqual(verifier_res["output"], "claude (verifier) node error: ")
        self.assertIn("node error (StopIteration)", verifier_res["output"])

    def test_an_ensemble_survives_one_dead_member(self):
        """Tier 2 #8: one failed researcher must not abort the run."""
        def agy_raises(prompt, **kwargs):
            raise RuntimeError("researcher went down")

        with patch.object(graph_module, "run_antigravity", side_effect=agy_raises), \
             patch.object(graph_module, "run_claude_code",
                          side_effect=_happy_claude()), \
             patch.object(graph_module, "run_opencode", return_value="implementation"):
            result = build_graph(ENSEMBLE_CONFIG).invoke(
                self._state(config=ENSEMBLE_CONFIG)
            )

        self.assertEqual(result["status"], STATUS_COMPLETED)
        # The dead member's fact survived; the run simply did not need it.
        errored = [r for r in result["agent_results"] if r.get("status") == "error"]
        self.assertEqual(len(errored), 1)
        self.assertEqual(errored[0]["agent"], "antigravity")
        self.assertFalse(has_error(result))

    def test_an_ensemble_where_everyone_dies_is_fatal(self):
        """When all members of a phase fail, the run stops with STATUS_ERROR."""
        def agy_raises(prompt, **kwargs):
            raise RuntimeError("researcher went down")

        def claude_half(prompt, **kwargs):
            if kwargs.get("role") == "researcher":
                raise RuntimeError("also down")
            return "plan" if kwargs.get("role") == "planner" else "VERDICT: PASS\nok."

        with patch.object(graph_module, "run_antigravity", side_effect=agy_raises), \
             patch.object(graph_module, "run_claude_code", side_effect=claude_half), \
             patch.object(graph_module, "run_opencode", return_value="implementation"):
            result = build_graph(ENSEMBLE_CONFIG).invoke(
                self._state(config=ENSEMBLE_CONFIG)
            )

        self.assertEqual(result["status"], STATUS_ERROR)
        self.assertTrue(has_error(result))
        # The implementer never ran: the researcher phase was already fatal.
        self.assertNotIn("implementation",
                         [r.get("output") for r in result.get("agent_results", [])])

    def test_typed_cli_errors_are_absorbed_as_error_results(self):
        """Provider/CLI failures are typed CLIErrors and never crash the run."""
        with patch.object(graph_module, "run_antigravity", return_value="analysis"), \
             patch.object(graph_module, "run_claude_code",
                          side_effect=CLIExecutionError("boom", returncode=1)), \
             patch.object(graph_module, "run_opencode", return_value="implementation"):
            result = build_graph(BASE_CONFIG).invoke(self._state())

        self.assertEqual(result["status"], STATUS_ERROR)
        self.assertIsNone(result.get("verification_verdict"))
        planner_res = next(
            r for r in result["agent_results"] if r.get("role") == "planner"
        )
        self.assertEqual(planner_res["status"], "error")
        self.assertIn("boom", planner_res["output"])

    def test_status_is_derived_from_the_error_fact(self):
        """An errored run derives STATUS_ERROR - the invariant behind 'Simulated CLI failure'."""
        state = {
            "agent_results": [
                {"agent": "antigravity", "role": "researcher", "status": "error",
                 "output": "antigravity (researcher) node error (RuntimeError): Simulated CLI failure"},
            ],
            "error": "antigravity (researcher) node error (RuntimeError): Simulated CLI failure",
        }
        self.assertEqual(derive_status(state), STATUS_ERROR)
        self.assertTrue(is_terminal(STATUS_ERROR))
        summary = derive_summary(state)
        self.assertEqual(summary["status"], STATUS_ERROR)
        self.assertEqual(summary["errored_executions"], 1)
        self.assertFalse(summary["successful"])


class ConfigErrorTest(unittest.TestCase):
    """`load_config` distinguishes a YAML syntax failure from an I/O failure."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="orch_cfgerr_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, text):
        path = os.path.join(self.tmp, "orchestrator.yaml")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def test_yaml_syntax_error_is_distinguishable(self):
        path = self._write("agents:\n  - agent: opencode\n   role: implementer\n\t nope")
        with self.assertRaises(ConfigValidationError) as ctx:
            load_config(path)
        message = str(ctx.exception)
        self.assertIn(path, message)
        self.assertIn("Failed to read YAML", message)
        self.assertNotIn("access denied", message)

    def test_an_io_error_is_distinguishable(self):
        path = self._write("agents: []")
        with patch.object(config_module, "open",
                          side_effect=PermissionError("permission denied")):
            with self.assertRaises(ConfigValidationError) as ctx:
                load_config(path)
        message = str(ctx.exception)
        self.assertIn("permission denied", message)
        self.assertIn("Failed to read YAML", message)
        # The original failure is chained, never swallowed silently.
        self.assertIsInstance(ctx.exception.__cause__, PermissionError)


class WriteTeamAtomicityTest(unittest.TestCase):
    """The one web write must never corrupt `orchestrator.yaml` mid-failure."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="orch_wtatomic_")
        self.path = os.path.join(self.root, "orchestrator.yaml")
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(MINIMAL_YAML)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_a_failed_write_leaves_the_original_intact_and_backed_up(self):
        team = normalize_team(get_template("careful"))
        with patch.object(teams_module.os, "replace",
                          side_effect=OSError("disk full")):
            result = write_team(self.root, team)

        self.assertFalse(result["ok"])
        self.assertIn("could not write", result["error"])

        with open(self.path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), MINIMAL_YAML)

        # A backup was still taken before the failed swap, so nothing is lost.
        self.assertTrue(result.get("backup") and os.path.isfile(result["backup"]))

        # No temp file pollutes the directory.
        self.assertFalse(
            os.path.isfile(os.path.join(self.root, "orchestrator.yaml.tmp"))
        )

    def test_a_team_that_cannot_parse_is_never_written_at_all(self):
        broken = {"agents": [
            {"agent": "opencode", "model": "opencode/gpt-5.1-codex", "role": "implementer"},
            {"agent": "opencode", "model": "opencode/gpt-5.1-codex", "role": "implementer"},
        ], "consensus": "unanimous", "max_repair_attempts": 1}
        result = write_team(self.root, broken)
        self.assertFalse(result["ok"])
        self.assertIn("refusing to write", result["error"])
        self.assertIsNone(result["backup"])
        with open(self.path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), MINIMAL_YAML)


class ServeWriteErrorTest(unittest.TestCase):
    """POST /api/team surfaces write failures as 400s and never corrupts the file."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="orch_srvwriteerr_")
        with open(os.path.join(self.root, "orchestrator.yaml"),
                  "w", encoding="utf-8") as handle:
            handle.write(MINIMAL_YAML)
        self.server = serve_board(
            self.root, config_module.validate_config({
                "agents": list(BASE_CONFIG["agents"]),
                "roles": {r: {"responsibility": r}
                          for r in ("researcher", "planner", "implementer", "verifier")},
            }),
            port=0, open_browser=False, printer=lambda *_: None,
            serve_forever=False, design=True,
        )
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.root, ignore_errors=True)

    def test_a_write_failure_is_reported_and_the_file_survives(self):
        import urllib.request
        import urllib.error

        team = normalize_team(get_template("careful"))
        payload = {
            "agents": team["agents"],
            "consensus": team["consensus"],
            "max_repair_attempts": team["max_repair_attempts"],
        }
        with patch.object(teams_module.os, "replace",
                          side_effect=OSError("disk full")):
            request = urllib.request.Request(
                self.base + "/api/team",
                data=json.dumps(payload).encode("utf-8"),
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(request, timeout=5)
            result = json.loads(
                ctx.exception.read().decode("utf-8")
            )

        self.assertEqual(ctx.exception.code, 400)
        self.assertFalse(result["ok"])
        self.assertIn("could not write", result["error"])
        with open(os.path.join(self.root, "orchestrator.yaml"),
                  encoding="utf-8") as handle:
            self.assertEqual(handle.read(), MINIMAL_YAML)


def _failing_then_passing():
    """Claude plans by raising, then re-verifies on the repair path... never reached."""
    calls = {"n": 0}

    def runner(prompt, **kwargs):
        if kwargs.get("role") == "planner":
            raise RuntimeError("CLI setup")
        return "VERDICT: PASS\nok."
    return runner


def _empty_str_exception():
    """Return a runner whose verifier fails with an exception whose str() is empty."""
    def runner(prompt, **kwargs):
        if kwargs.get("role") == "verifier":
            raise StopIteration()
        return "plan" if kwargs.get("role") == "planner" else "output"
    return runner


if __name__ == "__main__":
    unittest.main()