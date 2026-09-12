"""Package B - end-to-end reliability and failure recovery.

Every test here pins a defect that was reproduced by execution before it was fixed:

* A process killed after an attempt failed, and before its phase produced its one result, left
  that attempt's spend only on the `agent_retry` event. Resume read results alone: measured, a
  700-token failed attempt vanished from the resumed run's total.
* A process killed partway through a repair was resumed to `completed` with the repair neither
  recorded nor verified: the acceptance gate was re-run over the half-repaired workspace and its
  green result was paired with the *replayed* PASS of a verifier that had judged a red gate.
* The headless adapters dropped the usage a failed CLI run reported. Measured on this machine:
  Claude Code 2.1.267, agy 1.2.1 and OpenCode 1.18.29 all exit 1 on failure and still print it.
* A headless timeout killed only the agent's own process: a grandchild holding its stdout made
  a 2-second timeout return after 25 seconds, and the captured path left the grandchild running.
* Skill paths in every prompt named the user's checkout, not the run's worktree.
* A visible run on the Antigravity integrated terminal learned the bridge was down only after
  launching (and retrying) its first agent; a bridge that refused to create a tab cost a timeout.
* The cockpit counted neither failed attempts nor a ladder's other rungs, and its run total was a
  sum of cards - which counts identical ensemble members once per card.
* The CLI's error exit showed no spend at all.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import orchestrator.graph as graph_module
from orchestrator.agents.antigravity import run_antigravity
from orchestrator.agents.claude_code import run_claude_code
from orchestrator.agents.exceptions import CLIExecutionError, CLITimeoutError
from orchestrator.agents.opencode import run_opencode
from orchestrator.budget import tokens_spent
from orchestrator.cockpit import agent_states, columns, run_tokens
from orchestrator.config import validate_config
from orchestrator.graph import build_graph, context_node
from orchestrator.launcher import ExecutionResult, run_agent_cli
from orchestrator.metrics import aggregate_metrics, summary_rows, verify_token_aggregation_invariant
from orchestrator.preflight import check_terminal_surface, run_preflight
from orchestrator.resume import (
    load_resumable_run,
    orphaned_attempts,
    reconstruct_state,
    should_replay_gate,
)
from orchestrator.skills.registry import format_skill_manifest, rebase_skill_paths
from orchestrator.store import load_run
from orchestrator.terminals import run_captured
from orchestrator.tracer import default_tracer
from orchestrator.types import create_agent_result, create_token_usage, usage_total

from tests.support import isolated_graph_state, temporary_store_dir

ROLES = {r: {"responsibility": r} for r in ("researcher", "planner", "implementer", "verifier")}


def _usage(total):
    return create_token_usage(input_tokens=total - 1, output_tokens=1, total_tokens=total)


def _config(store_dir, **extra):
    raw = {
        "agents": [
            {"agent": "claude", "model": "sonnet", "role": "researcher"},
            {"agent": "claude", "model": "sonnet", "role": "planner"},
            {"agent": "opencode", "model": "opencode/gpt-5.1-codex", "role": "implementer"},
            {"agent": "claude", "model": "sonnet", "role": "verifier"},
        ],
        "roles": dict(ROLES),
        "preflight": {"enabled": False},
        "max_repair_attempts": 2,
        "execution": {"retry": {"attempts": 3, "backoff_seconds": 0}},
    }
    raw.update(extra)
    from tests.support import redirected_run_store

    return redirected_run_store(validate_config(raw), store_dir)


def _resume(config, run_id, store_dir, workspace):
    run = load_resumable_run(os.getcwd(), run_id, directory=store_dir)
    state = reconstruct_state(run, project_root=os.getcwd())
    state["config"] = config
    state["workspace"] = workspace
    return state


# ---------------------------------------------------------------------------------------------
# A killed process's failed attempts are recovered on resume, and counted exactly once
# ---------------------------------------------------------------------------------------------
class TestResumeRecoversInterruptedAttempts(unittest.TestCase):
    def setUp(self):
        default_tracer.clear()
        self.store_dir = temporary_store_dir()
        self.addCleanup(shutil.rmtree, self.store_dir, ignore_errors=True)
        self.config = _config(self.store_dir)
        self.workspace = {"isolated": False, "path": os.getcwd(), "reason": "test"}

    def _crash_mid_retry(self):
        """Implementer attempt 1 returns nothing but bills 700; attempt 2 dies with the process."""
        claude = iter([("research", _usage(100)), ("plan", _usage(200))])
        impl = iter([("", _usage(700), "headless")])

        def opencode(prompt, **kwargs):
            try:
                return next(impl)
            except StopIteration:
                raise KeyboardInterrupt()  # not an Exception: nothing in the graph catches it

        with patch.object(graph_module, "run_claude_code", side_effect=lambda p, **k: next(claude)), \
                patch.object(graph_module, "run_opencode", side_effect=opencode):
            state = isolated_graph_state(self.config, "crash mid retry", self.store_dir)
            with self.assertRaises(KeyboardInterrupt):
                build_graph(self.config).invoke(state)
        run = [d for d in os.listdir(self.store_dir)][0]
        return run

    def _finish(self, run_id):
        claude = iter([("VERDICT: PASS\nok", _usage(300))])
        with patch.object(graph_module, "run_claude_code", side_effect=lambda p, **k: next(claude)), \
                patch.object(graph_module, "run_opencode", return_value=("impl", _usage(1000), "headless")):
            return build_graph(self.config).invoke(_resume(self.config, run_id, self.store_dir, self.workspace))

    def test_the_spend_of_an_attempt_killed_mid_retry_survives_the_resume(self):
        run_id = self._crash_mid_retry()
        before = load_run(os.getcwd(), run_id, directory=self.store_dir)
        self.assertEqual([e["event"] for e in before["events"]].count("agent_retry"), 1)
        orphans = orphaned_attempts(before["events"])
        # Attempt 1 failed and reported 700; attempt 2 was running when the process died and
        # reported nothing (Package C: it is an attempt with unknown spend, not an absence).
        self.assertEqual([(o["attempt"], usage_total(o["token_usage"])) for o in orphans],
                         [(1, 700), (2, None)])
        self.assertTrue(orphans[1]["in_flight"])

        final = self._finish(run_id)
        self.assertEqual(final["status"], "completed")
        paid = 100 + 200 + 700 + 1000 + 300
        metrics = aggregate_metrics(final["agent_results"])
        self.assertEqual(metrics["known_total_tokens"], paid)
        self.assertFalse(metrics["all_tokens_available"], "attempt 2's spend is unknown, not zero")
        self.assertEqual(tokens_spent(final["agent_results"]), paid)
        self.assertTrue(verify_token_aggregation_invariant(final["agent_results"])["is_valid"])

        impl = [r for r in final["agent_results"] if r["role"] == "implementer"]
        self.assertEqual(len(impl), 1, "a retry is not an ensemble, across a resume too")
        recovered = impl[0]["attempt_token_usage"]
        self.assertEqual([(a["attempt"], a["interrupted"]) for a in recovered], [(1, True), (2, True)])
        self.assertIn("(try 1, before resume)", " ".join(r["label"] for r in summary_rows(final["agent_results"])))

        # The durable record now accounts for the attempt, so a further resume finds no orphan.
        after = load_run(os.getcwd(), run_id, directory=self.store_dir)
        self.assertEqual(orphaned_attempts(after["events"]), [])

    def test_resuming_a_finished_run_again_counts_nothing_twice(self):
        run_id = self._crash_mid_retry()
        first = self._finish(run_id)
        total = aggregate_metrics(first["agent_results"])["known_total_tokens"]
        with patch.object(graph_module, "run_claude_code", side_effect=AssertionError("must replay")), \
                patch.object(graph_module, "run_opencode", side_effect=AssertionError("must replay")):
            again = build_graph(self.config).invoke(_resume(self.config, run_id, self.store_dir, self.workspace))
        self.assertEqual(again["status"], "completed")
        self.assertEqual(aggregate_metrics(again["agent_results"])["known_total_tokens"], total)

    def test_an_accounted_retry_is_not_an_orphan_and_ensembles_do_not_claim_each_other(self):
        retry = lambda agent, total, attempt=1: {  # noqa: E731
            "event": "agent_retry", "role": "researcher", "agent": agent, "model": "m",
            "attempt": attempt, "reason": "empty", "token_usage": _usage(total),
        }
        result = lambda agent, total: {  # noqa: E731
            "event": "agent_result",
            "result": create_agent_result(
                agent, "researcher", "success", token_usage=_usage(5),
                attempt_token_usage=[{"attempt": 1, "agent": agent, "model": "m", "token_usage": _usage(total)}],
            ),
        }
        # Two members of one phase fail attempt 1 on the same agent and model, interleaved; only
        # the second finishes. The orphan is the first member's attempt, by its reported spend.
        events = [retry("claude", 40), retry("claude", 90), result("claude", 90)]
        orphans = orphaned_attempts(events)
        self.assertEqual([o["token_usage"]["total_tokens"] for o in orphans], [40])

    def test_an_attempt_with_no_recorded_usage_is_recovered_as_unknown_not_zero(self):
        events = [{"event": "agent_retry", "role": "planner", "agent": "claude", "model": "sonnet",
                   "attempt": 1, "reason": "boom", "token_usage": {"available": False}}]
        orphan = orphaned_attempts(events)[0]
        self.assertFalse(orphan["token_usage"]["available"])
        result = create_agent_result("claude", "planner", "success", token_usage=_usage(10),
                                     attempt_token_usage=[orphan])
        self.assertFalse(aggregate_metrics([result])["all_tokens_available"])
        self.assertEqual(aggregate_metrics([result])["known_total_tokens"], 10)


# ---------------------------------------------------------------------------------------------
# A replayed verdict is judged with the gate result it was given against
# ---------------------------------------------------------------------------------------------
class TestResumeKeepsTheGateWithItsVerdict(unittest.TestCase):
    """The gate here is a real subprocess that passes only once a repair has written its file."""

    def setUp(self):
        default_tracer.clear()
        self.store_dir = temporary_store_dir()
        self.work = tempfile.mkdtemp(prefix="orch_gate_")
        self.addCleanup(shutil.rmtree, self.store_dir, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.work, ignore_errors=True)
        gate = [sys.executable, "-c", "import os,sys; sys.exit(0 if os.path.exists('fixed') else 1)"]
        self.config = _config(
            self.store_dir,
            verification={"acceptance": {"command": gate, "required": True, "timeout_seconds": 60}},
            execution={"retry": {"attempts": 1, "backoff_seconds": 0}},
        )
        self.workspace = {"isolated": False, "path": self.work, "reason": "test"}

    def test_a_repair_killed_midway_is_redone_and_reverified_not_passed_on_a_stale_verdict(self):
        claude = iter([("research", _usage(100)), ("plan", _usage(200)), ("VERDICT: PASS\nlgtm", _usage(300))])
        impl = iter([("attempt", _usage(1000), "headless")])

        def repair_then_die(prompt, **kwargs):
            try:
                return next(impl)
            except StopIteration:
                open(os.path.join(kwargs["working_dir"], "fixed"), "w").close()  # the edit lands
                raise KeyboardInterrupt()  # ...and the process dies before reporting

        with patch.object(graph_module, "run_claude_code", side_effect=lambda p, **k: next(claude)), \
                patch.object(graph_module, "run_opencode", side_effect=repair_then_die):
            state = isolated_graph_state(self.config, "gate then crash", self.store_dir, workspace=self.workspace)
            with self.assertRaises(KeyboardInterrupt):
                build_graph(self.config).invoke(state)
        run_id = os.listdir(self.store_dir)[0]

        calls = []
        claude2 = iter([("VERDICT: PASS\nrepaired", _usage(310))])

        def verifier(prompt, **kwargs):
            calls.append(kwargs.get("role"))
            return next(claude2)

        def repair(prompt, **kwargs):
            calls.append("repair")
            return ("repaired", _usage(1100), "headless")

        with patch.object(graph_module, "run_claude_code", side_effect=verifier), \
                patch.object(graph_module, "run_opencode", side_effect=repair):
            final = build_graph(self.config).invoke(_resume(self.config, run_id, self.store_dir, self.workspace))

        self.assertEqual(final["status"], "completed")
        self.assertEqual(final["repair_attempts"], 1, "the interrupted repair must be redone")
        self.assertEqual(calls, ["repair", "verifier"], "and the repaired workspace re-verified")
        checks = [e["check"] for e in load_run(os.getcwd(), run_id, directory=self.store_dir)["events"]
                  if e.get("event") == "acceptance"]
        self.assertEqual([(c["repair_attempts"], c["ok"]) for c in checks], [(0, False), (1, True)],
                         "one gate result per generation: the gen-0 result is replayed, not re-run")

    def test_the_gate_replays_only_when_its_verification_replays(self):
        restored_gate = {"repair_attempts": 0, "ok": False, "restored": True}
        passed = {"repair_attempts": 0, "verdict": "PASS", "restored": True}
        blocked = {"repair_attempts": 0, "verdict": "BLOCKED", "restored": True}
        base = {"resumed_from": "r", "acceptance_checks": [restored_gate]}
        self.assertTrue(should_replay_gate({**base, "verification_history": [passed]}, 0))
        # A BLOCKED run is resumed because a person acted: the gate looks afresh too.
        self.assertFalse(should_replay_gate({**base, "verification_history": [blocked]}, 0))
        # Recorded gate, no recorded verdict (killed between the two): a fresh verifier, a fresh gate.
        self.assertFalse(should_replay_gate({**base, "verification_history": []}, 0))
        self.assertFalse(should_replay_gate({"acceptance_checks": [restored_gate], "verification_history": [passed]}, 0))


# ---------------------------------------------------------------------------------------------
# A failed CLI run's reported usage travels with the failure (shapes measured on this machine)
# ---------------------------------------------------------------------------------------------
class TestAFailedExecutionKeepsWhatItReported(unittest.TestCase):
    # Claude Code 2.1.267, `claude -p ... --output-format json --model claude-does-not-exist-9`:
    # exit 1, and this object on stdout (trimmed to the fields read; usage values are the
    # measured shape with non-zero counts, as a turn that failed after working reports them).
    CLAUDE_FAILED = json.dumps({
        "type": "result", "subtype": "success", "is_error": True, "api_error_status": 404,
        "result": "There's an issue with the selected model (claude-does-not-exist-9).",
        "usage": {"input_tokens": 12, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 30,
                  "output_tokens": 8, "output_tokens_details": {"thinking_tokens": 0}},
    })
    # agy 1.2.1, `agy -p ... --output-format json --model does-not-exist-9`: exit 1.
    AGY_FAILED = json.dumps({
        "conversation_id": "", "status": "ERROR", "response": "",
        "error": "invalid model selection (--model \"does-not-exist-9\"): model does-not-exist-9 is not recognized\nAvailable models: ...",
        "usage": {"input_tokens": 40, "output_tokens": 2, "thinking_tokens": 0, "cache_read_tokens": 0, "total_tokens": 42},
    })
    # OpenCode 1.18.29 `run --format json`: a step that finished, then the session error; exit 1.
    OPENCODE_FAILED = "\n".join([
        json.dumps({"type": "step_finish", "part": {"type": "step-finish", "tokens": {
            "input": 900, "output": 50, "reasoning": 0, "cache": {"read": 100, "write": 0}, "total": 1050}}}),
        json.dumps({"type": "error", "error": {"name": "UnknownError",
                                               "data": {"message": "Unexpected server error."}}}),
    ])

    def _failed(self, stdout):
        return ExecutionResult(returncode=1, stdout=stdout, stderr="", duration_seconds=0.1, command=["x"])

    def test_claude(self):
        with patch("orchestrator.agents.claude_code.run_agent_cli", return_value=self._failed(self.CLAUDE_FAILED)), \
                patch("orchestrator.agents.claude_code.get_claude_executable_path", return_value="claude"):
            with self.assertRaises(CLIExecutionError) as caught:
                run_claude_code("p", return_usage=True)
        self.assertEqual(caught.exception.token_usage["total_tokens"], 12 + 30 + 8)
        self.assertIn("issue with the selected model", str(caught.exception))

    def test_antigravity_on_a_non_zero_exit_and_on_a_non_success_status(self):
        with patch("orchestrator.agents.antigravity.run_agent_cli", return_value=self._failed(self.AGY_FAILED)), \
                patch("orchestrator.agents.antigravity.get_antigravity_executable_path", return_value="agy"):
            with self.assertRaises(CLIExecutionError) as caught:
                run_antigravity("p", return_usage=True)
        self.assertEqual(caught.exception.token_usage["total_tokens"], 42)
        self.assertIn("invalid model selection", str(caught.exception))
        self.assertNotIn("Available models", str(caught.exception), "only the first line of the error")

        ok_exit = ExecutionResult(returncode=0, stdout=self.AGY_FAILED, stderr="", duration_seconds=0.1, command=["x"])
        with patch("orchestrator.agents.antigravity.run_agent_cli", return_value=ok_exit), \
                patch("orchestrator.agents.antigravity.get_antigravity_executable_path", return_value="agy"):
            with self.assertRaises(CLIExecutionError) as caught:
                run_antigravity("p", return_usage=True)
        self.assertEqual(caught.exception.token_usage["total_tokens"], 42)

    def test_opencode_headless(self):
        with patch("orchestrator.agents.opencode.run_agent_cli", return_value=self._failed(self.OPENCODE_FAILED)), \
                patch("orchestrator.agents.opencode.get_opencode_executable_path", return_value="opencode"):
            with self.assertRaises(CLIExecutionError) as caught:
                run_opencode("p", agent_execution_mode="headless", return_usage=True)
        self.assertEqual(caught.exception.token_usage["total_tokens"], 1050)
        self.assertIn("UnknownError: Unexpected server error.", str(caught.exception))

    def test_a_failure_that_reported_nothing_still_reports_nothing(self):
        with patch("orchestrator.agents.claude_code.run_agent_cli", return_value=self._failed("")), \
                patch("orchestrator.agents.claude_code.get_claude_executable_path", return_value="claude"):
            with self.assertRaises(CLIExecutionError) as caught:
                run_claude_code("p", return_usage=True)
        self.assertIsNone(caught.exception.token_usage)


# ---------------------------------------------------------------------------------------------
# Headless timeouts and interruptions stop the agent's whole process tree, on time
# ---------------------------------------------------------------------------------------------
@unittest.skipUnless(sys.platform == "win32", "process-tree stop is taskkill /T, Windows only")
class TestHeadlessTimeoutStopsTheWholeTree(unittest.TestCase):
    """Real processes: an agent that leaves a grandchild holding its stdout, then hangs."""

    def _agent(self):
        pidfile = tempfile.mktemp(suffix=".pid")
        self.addCleanup(lambda: os.path.exists(pidfile) and os.remove(pidfile))
        code = (
            "import subprocess, sys, time\n"
            "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
            f"open(r'{pidfile}', 'w').write(str(g.pid))\n"
            "print('working', flush=True)\n"
            "time.sleep(60)\n"
        )
        return [sys.executable, "-c", code], pidfile

    def _alive(self, pid):
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return str(pid) in out

    def _assert_stopped_on_time(self, call):
        cmd, pidfile = self._agent()
        started = time.time()
        call(cmd)
        elapsed = time.time() - started
        time.sleep(0.5)
        with open(pidfile) as fh:
            grandchild = int(fh.read())
        alive = self._alive(grandchild)
        if alive:
            subprocess.run(["taskkill", "/PID", str(grandchild), "/F"], capture_output=True)
        self.assertLess(elapsed, 12, "a 2-second timeout must not wait for the agent's children")
        self.assertFalse(alive, "the agent's child must not outlive the timeout")

    def test_the_headless_path(self):
        def call(cmd):
            with self.assertRaises(CLITimeoutError):
                run_agent_cli(cmd, timeout=2)
        self._assert_stopped_on_time(call)

    def test_the_captured_daemon_path(self):
        def call(cmd):
            with self.assertRaises(subprocess.TimeoutExpired):
                run_captured(cmd, timeout=2)
        self._assert_stopped_on_time(call)

    def test_the_offline_suite_cannot_start_a_real_agent(self):
        """The guard in tests/__init__.py - added after this suite, through mocks aimed at the
        wrong seam, started the real CLIs. It refuses before any process exists."""
        if os.environ.get("RUN_LIVE_TESTS") == "1":
            self.skipTest("live tests deliberately allow real agent CLIs")
        for binary in ("claude", r"C:\npm\node_modules\opencode-ai\bin\opencode.exe", "AGY.EXE"):
            with self.assertRaises(RuntimeError):
                subprocess.Popen([binary, "--version"])
        with self.assertRaises(RuntimeError):
            run_claude_code("must not run")

    def test_an_ordinary_run_is_unchanged(self):
        # The child writes UTF-8 (-X utf8) and the launcher reads UTF-8, so a non-ASCII character
        # must come back exactly. The literal is escaped: an earlier copy of this test carried it
        # double-encoded ("âœ“"), which only round-tripped where PYTHONIOENCODING happened to be
        # utf-8 and failed everywhere else.
        result = run_agent_cli([sys.executable, "-X", "utf8", "-c", "print('ok \\u2713')"], timeout=30)
        self.assertEqual((result.returncode, result.stdout.strip()), (0, "ok ✓"))


# ---------------------------------------------------------------------------------------------
# Agents are shown skills inside their worktree
# ---------------------------------------------------------------------------------------------
class TestSkillPathsFollowTheWorktree(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="orch_checkout_")
        self.work = os.path.join(self.root, ".orchestrator", "worktrees", "run1")
        self.external = tempfile.mkdtemp(prefix="orch_external_")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.external, ignore_errors=True)
        for base in (self.root, self.work):
            os.makedirs(os.path.join(base, "skills", "tracked"))
        os.makedirs(os.path.join(self.root, "skills", "untracked"))

    def _skill(self, base, name):
        location = os.path.join(base, "skills", name) if base != self.external else self.external
        return {"name": name, "description": "d", "location": location,
                "instructions_path": os.path.join(location, "SKILL.md"), "resources": []}

    def test_a_committed_skill_is_read_from_the_worktree(self):
        [skill] = rebase_skill_paths([self._skill(self.root, "tracked")], self.root, self.work)
        self.assertEqual(skill["location"], os.path.join(self.work, "skills", "tracked"))
        self.assertEqual(skill["instructions_path"], os.path.join(self.work, "skills", "tracked", "SKILL.md"))

    def test_an_untracked_skill_is_flagged_and_external_ones_are_untouched(self):
        original = [self._skill(self.root, "untracked"), self._skill(self.external, "ext")]
        untracked, external = rebase_skill_paths(original, self.root, self.work)
        self.assertTrue(untracked.get("outside_workspace"))
        self.assertIn("not in your worktree", format_skill_manifest([untracked]))
        self.assertEqual(external, original[1])
        self.assertNotIn("outside_workspace", original[0], "the input is not modified")

    def test_an_unisolated_run_is_unchanged(self):
        skills = [self._skill(self.root, "tracked")]
        self.assertEqual(rebase_skill_paths(skills, self.root, self.root), skills)

    @unittest.skipIf(shutil.which("git") is None, "git is required")
    def test_the_prompt_manifest_of_a_real_isolated_run_names_no_checkout_path(self):
        repo = tempfile.mkdtemp(prefix="orch_skillrepo_")
        self.addCleanup(shutil.rmtree, repo, ignore_errors=True)
        os.makedirs(os.path.join(repo, "skills", "tidy"))
        with open(os.path.join(repo, "skills", "tidy", "SKILL.md"), "w") as fh:
            fh.write("---\nname: tidy\ndescription: Keep it tidy.\n---\n")
        with open(os.path.join(repo, ".gitignore"), "w") as fh:
            fh.write(".orchestrator/\n")
        for args in (["init", "-q"], ["config", "user.email", "t@example.invalid"], ["config", "user.name", "t"],
                     ["add", "-A"], ["commit", "-q", "-m", "init"]):
            subprocess.run(["git", *args], cwd=repo, capture_output=True, check=True)
        store = temporary_store_dir()
        self.addCleanup(shutil.rmtree, store, ignore_errors=True)
        config = _config(store, workspace={"isolation": "worktree"})
        state = context_node({"task": "t", "project_root": repo, "config": config})
        worktree = state["workspace"]["path"]
        self.addCleanup(subprocess.run, ["git", "worktree", "remove", "--force", worktree], cwd=repo, capture_output=True)
        manifest = state["skill_manifest"]
        self.assertIn(os.path.join(worktree, "skills", "tidy", "SKILL.md"), manifest)
        self.assertNotIn(os.path.join(os.path.realpath(repo), "skills"), manifest)


# ---------------------------------------------------------------------------------------------
# A terminal that cannot exist is refused before anything runs
# ---------------------------------------------------------------------------------------------
class TestPreflightRefusesAnUnreachableBridge(unittest.TestCase):
    def _config(self, visible=True, terminal_type="antigravity_integrated"):
        return {"execution": {"visible_terminals": visible, "terminal_type": terminal_type}}

    @patch("orchestrator.launcher.check_antigravity_bridge", return_value=False)
    def test_an_explicit_integrated_terminal_with_no_bridge_is_an_error(self, _check):
        errors = check_terminal_surface(self._config())
        self.assertEqual(len(errors), 1)
        self.assertIn("no fallback", errors[0])

    @patch("orchestrator.launcher.check_antigravity_bridge", return_value=False)
    def test_everything_that_does_not_need_the_bridge_is_fine(self, _check):
        self.assertEqual(check_terminal_surface(self._config(terminal_type="auto")), [])
        self.assertEqual(check_terminal_surface(self._config(visible=False)), [])
        # The run's own overrides are what count (e.g. --no-visible-terminals).
        self.assertEqual(check_terminal_surface(self._config(), visible_terminals=False), [])
        self.assertEqual(check_terminal_surface(self._config(), terminal_type="console"), [])

    @patch("orchestrator.launcher.check_antigravity_bridge", return_value=False)
    def test_under_the_daemon_no_window_is_opened_so_no_bridge_is_needed(self, _check):
        from orchestrator.launcher import set_output_recorder

        previous = set_output_recorder(lambda **kwargs: None)
        self.addCleanup(set_output_recorder, previous)
        self.assertEqual(check_terminal_surface(self._config()), [])

    @patch("orchestrator.launcher.check_antigravity_bridge", return_value=True)
    def test_a_live_bridge_passes(self, _check):
        self.assertEqual(check_terminal_surface(self._config()), [])

    @patch("orchestrator.launcher.check_antigravity_bridge", return_value=False)
    def test_it_fails_the_preflight_report(self, _check):
        config = validate_config({
            "agents": [{"agent": "claude", "model": "sonnet", "role": "planner"}],
            "roles": {"planner": {"responsibility": "p"}},
            "execution": {"visible_terminals": True, "terminal_type": "antigravity_integrated"},
        })
        with patch("orchestrator.preflight.shutil.which", return_value="claude"):
            report = run_preflight(config)
        self.assertFalse(report["ok"])
        self.assertTrue(any(e.startswith("terminal:") for e in report["errors"]))

    @patch("orchestrator.launcher.check_antigravity_bridge", return_value=True)
    @patch("orchestrator.launcher.launch_antigravity_integrated_terminal", return_value=False)
    def test_a_bridge_that_does_not_create_the_tab_fails_now_not_after_the_timeout(self, _launch, _check):
        started = time.time()
        with self.assertRaises(CLIExecutionError):
            run_agent_cli(["x"], visible=True, terminal_type="antigravity_integrated", timeout=120)
        self.assertLess(time.time() - started, 5)


# ---------------------------------------------------------------------------------------------
# The cockpit shows every spend, once
# ---------------------------------------------------------------------------------------------
class TestTheCockpitCountsEverySpendOnce(unittest.TestCase):
    def _result_event(self, seq, result):
        return {"event": "agent_result", "sequence": seq, "result": result}

    def test_a_card_includes_failed_attempts_and_every_rung_of_its_ladder(self):
        escalated = create_agent_result(
            "claude", "researcher", "success", token_usage=_usage(629),
            attempt_token_usage=[{"attempt": 1, "agent": "antigravity", "model": "g", "token_usage": _usage(12232)}],
        )
        events = [{"event": "agent_started", "sequence": 1, "agent": "antigravity", "role": "researcher"},
                  {"event": "agent_started", "sequence": 2, "agent": "claude", "role": "researcher"},
                  self._result_event(3, escalated)]
        team = {"phases": [{"index": 0, "role": "researcher", "agents": [
            {"agent": ["antigravity", "claude"], "model": ["g", "sonnet"], "role": "researcher"}]}]}
        [card] = columns(team, events)[0]["agents"]
        self.assertEqual(card["tokens"], 12232 + 629)
        self.assertEqual(agent_states(events)["claude/researcher"]["tokens"], 12232 + 629)

    def test_the_run_total_is_read_from_results_not_summed_from_cards(self):
        a = create_agent_result("claude", "verifier", "success", verdict="PASS", token_usage=_usage(300))
        b = create_agent_result("claude", "verifier", "success", verdict="PASS", token_usage=_usage(310))
        events = [self._result_event(1, a), self._result_event(2, b)]
        team = {"phases": [{"index": 0, "role": "verifier", "parallel": True, "agents": [
            {"agent": "claude", "model": "sonnet", "role": "verifier"},
            {"agent": "claude", "model": "sonnet", "role": "verifier"}]}]}
        cards = columns(team, events)[0]["agents"]
        self.assertEqual(sum(c["tokens"] for c in cards), 2 * 610, "the old page total")
        self.assertEqual(run_tokens(events)["known"], 610)
        self.assertTrue(run_tokens(events)["complete"])

    def test_unreported_spend_makes_the_total_a_floor(self):
        failed = create_agent_result("claude", "planner", "error", token_usage=None,
                                     attempt_token_usage=[{"attempt": 1, "agent": "claude", "model": "s", "token_usage": _usage(50)}])
        totals = run_tokens([self._result_event(1, failed)])
        self.assertEqual((totals["known"], totals["complete"], totals["failed_attempt_tokens"]), (50, False, 50))

    def test_the_page_shows_the_server_total(self):
        with open(os.path.join(os.path.dirname(graph_module.__file__), "web", "cockpit.html"), encoding="utf-8") as fh:
            page = fh.read()
        self.assertIn("state.cockpit.run_tokens", page)
        self.assertEqual(page.count("runTokenLabel("), 3, "defined once, used by both token displays")


# ---------------------------------------------------------------------------------------------
# The CLI's error exit shows what the run spent
# ---------------------------------------------------------------------------------------------
class TestTheErrorExitShowsTheSpend(unittest.TestCase):
    def test_retry_exhaustion_prints_the_summary_table(self):
        import contextlib
        import io

        import orchestrator.__main__ as cli

        project = tempfile.mkdtemp(prefix="orch_cli_")
        self.addCleanup(shutil.rmtree, project, ignore_errors=True)
        config = {
            "agents": [{"agent": "claude", "model": "sonnet", "role": "researcher"},
                       {"agent": "claude", "model": "sonnet", "role": "planner"}],
            "roles": {"researcher": {"responsibility": "r"}, "planner": {"responsibility": "p"}},
            "models": {"claude": [{"id": "sonnet", "name": "s"}]},
            "preflight": {"enabled": False},
            "workspace": {"isolation": "none"},
            "execution": {"retry": {"attempts": 2, "backoff_seconds": 0}},
        }
        import yaml

        with open(os.path.join(project, "orchestrator.yaml"), "w", encoding="utf-8") as fh:
            yaml.safe_dump(config, fh)
        replies = iter([("research", _usage(100))])

        def claude(prompt, **kwargs):
            if kwargs.get("role") == "planner":
                raise CLIExecutionError("down", token_usage=_usage(40))
            return next(replies)

        out = io.StringIO()
        with patch.object(graph_module, "run_claude_code", side_effect=claude), \
                patch.object(sys, "argv", ["orchestrator", "task", "--project-root", project]), \
                contextlib.redirect_stdout(out):
            code = cli.main()
        text = out.getvalue()
        self.assertEqual(code, 1)
        self.assertIn("Pipeline Error", text)
        self.assertIn("Claude Planner (try 1, failed)", text)
        self.assertRegex(text, r"Total\s+[\d.]+s\s+180")


if __name__ == "__main__":
    unittest.main()
