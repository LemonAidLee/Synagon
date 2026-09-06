"""Tests for the Tier 0 and Tier 2 fixes.

Covers:
  Tier 0 #3  Git worktree isolation      - orchestrator.workspace
  Tier 2 #8  Parallel phases             - ensembles, consensus, fault tolerance
  Tier 2 #9  Verifier independence       - orchestrator.preflight
  Tier 2 #11 Model escalation ladders    - config + role node
  plus the `add`-reducer duplication fix in context_node.
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import orchestrator.graph as graph_module
from orchestrator.config import (
    ConfigValidationError,
    get_consensus_policy,
    get_workspace_config,
    validate_config,
)
from orchestrator.graph import build_graph, graph
from orchestrator.preflight import check_verifier_independence, probe_agent, run_preflight
from orchestrator.status import (
    STATUS_BLOCKED,
    STATUS_COMPLETED,
    STATUS_ERROR,
    STATUS_FAILED,
    latest_verification_group,
    resolve_consensus,
)
from orchestrator import workspace as ws

ROLES = {
    r: {"responsibility": r}
    for r in ("researcher", "planner", "implementer", "verifier")
}

UNISOLATED = {"isolated": False, "path": os.getcwd(), "reason": "test"}


def _cfg(agents, **extra):
    config = {"agents": agents, "roles": dict(ROLES)}
    config.update(extra)
    return config


def _state(config, **extra):
    state = {
        "task": "t",
        "project_root": os.getcwd(),
        "run_store_enabled": False,
        "config": config,
        "agent_results": [],
        "workspace": dict(UNISOLATED),
    }
    state.update(extra)
    return state


# ===========================================================================
# Tier 0 #3 - Git worktree isolation
# ===========================================================================


class TestWorkspaceIsolation(unittest.TestCase):
    """Each run gets its own worktree; the user's checkout is never touched."""

    @classmethod
    def setUpClass(cls):
        cls.git = shutil.which("git") is not None

    def setUp(self):
        if not self.git:
            self.skipTest("git is not installed")
        self.repo = tempfile.mkdtemp(prefix="orch_ws_")
        self._git("init", "-q")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test")
        with open(os.path.join(self.repo, "seed.txt"), "w", encoding="utf-8") as f:
            f.write("seed\n")
        self._git("add", "-A")
        self._git("commit", "-q", "-m", "seed")

    def tearDown(self):
        if getattr(self, "repo", None):
            shutil.rmtree(self.repo, ignore_errors=True)

    def _git(self, *args):
        return subprocess.run(
            ["git"] + list(args), cwd=self.repo, capture_output=True, stdin=subprocess.DEVNULL
        )

    def test_detects_a_git_repository(self):
        self.assertTrue(ws.is_git_repo(self.repo))
        self.assertTrue(ws.has_commits(self.repo))

    def test_non_repo_is_not_isolated(self):
        plain = tempfile.mkdtemp(prefix="orch_plain_")
        try:
            info = ws.create_run_worktree(plain, "run1")
            self.assertFalse(info["isolated"])
            self.assertIn("not a git repository", info["reason"])
            self.assertEqual(info["path"], plain)
        finally:
            shutil.rmtree(plain, ignore_errors=True)

    def test_repo_without_commits_is_not_isolated(self):
        empty = tempfile.mkdtemp(prefix="orch_empty_")
        try:
            subprocess.run(["git", "init", "-q"], cwd=empty, capture_output=True)
            info = ws.create_run_worktree(empty, "run1")
            self.assertFalse(info["isolated"])
            self.assertIn("no commits", info["reason"])
        finally:
            shutil.rmtree(empty, ignore_errors=True)

    def test_creates_worktree_on_its_own_branch(self):
        info = ws.create_run_worktree(self.repo, "20260101T000000Z-abc123")
        self.assertTrue(info["isolated"], info.get("reason"))
        self.assertTrue(os.path.isdir(info["path"]))
        self.assertEqual(info["branch"], "orchestrator/run/20260101T000000Z-abc123")
        self.assertTrue(os.path.isfile(os.path.join(info["path"], "seed.txt")))

    def test_edits_in_the_worktree_do_not_touch_the_checkout(self):
        info = ws.create_run_worktree(self.repo, "run-edit")
        with open(os.path.join(info["path"], "agent_file.txt"), "w", encoding="utf-8") as f:
            f.write("written by an agent\n")

        self.assertFalse(os.path.exists(os.path.join(self.repo, "agent_file.txt")))
        summary = ws.summarize_worktree(info)
        self.assertEqual(summary["change_count"], 1)
        self.assertTrue(any("agent_file.txt" in line for line in summary["changed_files"]))

    def test_commit_puts_work_on_the_run_branch(self):
        info = ws.create_run_worktree(self.repo, "run-commit")
        with open(os.path.join(info["path"], "agent_file.txt"), "w", encoding="utf-8") as f:
            f.write("work\n")

        commit = ws.commit_worktree(info, "orchestrator run")
        self.assertIsNotNone(commit)
        self.assertFalse(ws.is_dirty(info["path"]))
        # The base branch is untouched by the run's commit.
        listed = subprocess.run(
            ["git", "ls-tree", "--name-only", "HEAD"],
            cwd=self.repo, capture_output=True, stdin=subprocess.DEVNULL,
        ).stdout.decode()
        self.assertNotIn("agent_file.txt", listed)

    def test_commit_with_no_changes_returns_none(self):
        info = ws.create_run_worktree(self.repo, "run-empty")
        self.assertIsNone(ws.commit_worktree(info, "nothing to do"))

    def test_dirty_worktree_is_never_force_removed(self):
        info = ws.create_run_worktree(self.repo, "run-dirty")
        with open(os.path.join(info["path"], "uncommitted.txt"), "w", encoding="utf-8") as f:
            f.write("precious\n")

        self.assertFalse(ws.remove_worktree(info))
        self.assertTrue(os.path.isdir(info["path"]))

        self.assertTrue(ws.remove_worktree(info, force=True))

    def test_clean_worktree_is_removable(self):
        info = ws.create_run_worktree(self.repo, "run-clean")
        self.assertTrue(ws.remove_worktree(info))

    def test_describe_reports_review_and_merge_commands(self):
        info = ws.create_run_worktree(self.repo, "run-describe")
        text = ws.describe_workspace(info)
        self.assertIn("isolated", text)
        self.assertIn("git diff", text)
        self.assertIn("git merge", text)

        text = ws.describe_workspace(dict(UNISOLATED))
        self.assertIn("NOT isolated", text)


class TestWorkspaceInGraph(unittest.TestCase):
    """The graph routes agents into the worktree when the run is isolated."""

    def test_agents_run_in_the_worktree(self):
        if not shutil.which("git"):
            self.skipTest("git is not installed")

        repo = tempfile.mkdtemp(prefix="orch_wsg_")
        try:
            for args in (
                ["init", "-q"],
                ["config", "user.email", "t@example.com"],
                ["config", "user.name", "T"],
            ):
                subprocess.run(["git"] + args, cwd=repo, capture_output=True)
            with open(os.path.join(repo, "seed.txt"), "w", encoding="utf-8") as f:
                f.write("seed\n")
            subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True)
            subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=repo, capture_output=True)

            seen = {}

            def impl(prompt, **kwargs):
                seen["impl"] = kwargs.get("working_dir")
                with open(os.path.join(kwargs["working_dir"], "made.txt"), "w", encoding="utf-8") as f:
                    f.write("x\n")
                return ("done", {"available": False}, "headless")

            def claude(prompt, **kwargs):
                if kwargs.get("role") == "verifier":
                    seen["verify"] = kwargs.get("working_dir")
                    return "VERDICT: PASS\nok"
                return "plan"

            with patch.object(graph_module, "run_antigravity", return_value="research"), \
                 patch.object(graph_module, "run_claude_code", side_effect=claude), \
                 patch.object(graph_module, "run_opencode", side_effect=impl):
                result = graph.invoke(
                    {"task": "make a file", "project_root": repo, "agent_results": []}
                )

            workspace = result.get("workspace") or {}
            self.assertTrue(workspace.get("isolated"), workspace.get("reason"))
            # Implementer and verifier share one workspace, and it is not the checkout.
            self.assertEqual(seen["impl"], seen["verify"])
            self.assertEqual(seen["impl"], workspace["path"])
            self.assertNotEqual(seen["impl"], repo)
            self.assertFalse(os.path.exists(os.path.join(repo, "made.txt")))
            self.assertEqual(result["workspace_summary"]["change_count"], 1)
            self.assertIsNotNone(result["workspace_summary"].get("commit"))
        finally:
            for wt in ("", ):  # release worktree handles before deleting the tree
                pass
            subprocess.run(["git", "worktree", "prune"], cwd=repo, capture_output=True)
            shutil.rmtree(repo, ignore_errors=True)

    def test_isolation_none_runs_in_the_project_directory(self):
        config = _cfg(
            [
                {"agent": "claude", "model": "sonnet", "role": "planner"},
            ],
            workspace={"isolation": "none"},
        )
        seen = {}

        def claude(prompt, **kwargs):
            seen["dir"] = kwargs.get("working_dir")
            return "plan"

        with patch.object(graph_module, "run_claude_code", side_effect=claude):
            result = build_graph(config).invoke(
                {
                    "task": "t",
                    "project_root": os.getcwd(),
                    "run_store_enabled": False,
                    "config": validate_config(config),
                    "agent_results": [],
                }
            )
        self.assertFalse(result["workspace"]["isolated"])
        self.assertEqual(seen["dir"], os.getcwd())

    def test_required_isolation_refuses_to_run_unisolated(self):
        config = validate_config(
            _cfg(
                [{"agent": "claude", "model": "sonnet", "role": "planner"}],
                workspace={"isolation": "worktree"},
            )
        )
        plain = tempfile.mkdtemp(prefix="orch_noniso_")
        try:
            with patch.object(graph_module, "run_claude_code") as claude:
                result = build_graph(config).invoke(
                    {
                        "task": "t",
                        "project_root": plain,
                        "run_store_enabled": False,
                        "config": config,
                        "agent_results": [],
                    }
                )
            claude.assert_not_called()
            self.assertEqual(result["status"], STATUS_ERROR)
            self.assertIn("could not be isolated", result["error"])
        finally:
            shutil.rmtree(plain, ignore_errors=True)


# ===========================================================================
# Tier 2 #8 - Parallel phases
# ===========================================================================


class TestConsensus(unittest.TestCase):
    """Consensus resolution is conservative and BLOCKED always wins."""

    def test_single_verdict_passes_through(self):
        self.assertEqual(resolve_consensus(["PASS"]), "PASS")
        self.assertEqual(resolve_consensus(["FAIL"]), "FAIL")

    def test_unanimous_requires_every_pass(self):
        self.assertEqual(resolve_consensus(["PASS", "PASS"]), "PASS")
        self.assertEqual(resolve_consensus(["PASS", "FAIL"]), "FAIL")
        self.assertEqual(resolve_consensus(["PASS", "UNKNOWN"]), "UNKNOWN")

    def test_majority_policy(self):
        self.assertEqual(resolve_consensus(["PASS", "PASS", "FAIL"], "majority"), "PASS")
        self.assertEqual(resolve_consensus(["PASS", "FAIL", "FAIL"], "majority"), "FAIL")
        self.assertEqual(resolve_consensus(["PASS", "FAIL"], "majority"), "FAIL")

    def test_any_policy(self):
        self.assertEqual(resolve_consensus(["FAIL", "PASS"], "any"), "PASS")
        self.assertEqual(resolve_consensus(["FAIL", "FAIL"], "any"), "FAIL")

    def test_blocked_wins_under_every_policy(self):
        for policy in ("unanimous", "majority", "any"):
            self.assertEqual(resolve_consensus(["PASS", "BLOCKED"], policy), "BLOCKED")
            self.assertEqual(resolve_consensus(["PASS", "PASS", "BLOCKED"], policy), "BLOCKED")

    def test_empty_is_unknown(self):
        self.assertEqual(resolve_consensus([]), "UNKNOWN")
        self.assertEqual(resolve_consensus([None]), "UNKNOWN")

    def test_latest_group_selects_one_attempt(self):
        state = {
            "verification_history": [
                {"attempt": 1, "verdict": "FAIL"},
                {"attempt": 1, "verdict": "PASS"},
                {"attempt": 2, "verdict": "PASS"},
                {"attempt": 2, "verdict": "PASS"},
            ]
        }
        group = latest_verification_group(state)
        self.assertEqual(len(group), 2)
        self.assertTrue(all(r["attempt"] == 2 for r in group))


class TestParallelPhases(unittest.TestCase):
    """Ensembles run concurrently, survive partial failure, and reach consensus."""

    def _ensemble_cfg(self, verifiers=1, researchers=1, **extra):
        agents = []
        for i in range(researchers):
            agents.append(
                {"agent": "antigravity" if i == 0 else "claude",
                 "model": "gemini-3.8-flash-high" if i == 0 else "sonnet",
                 "role": "researcher"}
            )
        agents.append({"agent": "claude", "model": "sonnet", "role": "planner"})
        agents.append({"agent": "opencode", "model": "opencode/gpt-5.1-codex", "role": "implementer"})
        for i in range(verifiers):
            agents.append(
                {"agent": "claude" if i == 0 else "antigravity",
                 "model": "sonnet" if i == 0 else "gemini-3.8-flash-high",
                 "role": "verifier"}
            )
        return _cfg(agents, **extra)

    def test_parallel_verifiers_do_not_crash(self):
        """Regression: concurrent writes used to raise InvalidUpdateError."""
        config = self._ensemble_cfg(verifiers=2)
        with patch.object(graph_module, "run_antigravity", side_effect=["research", "VERDICT: PASS\nok"]), \
             patch.object(graph_module, "run_claude_code", side_effect=["plan", "VERDICT: PASS\nok"]), \
             patch.object(graph_module, "run_opencode", return_value="impl"):
            result = build_graph(config).invoke(_state(config, max_repair_attempts=0))
        self.assertEqual(result["verification_verdict"], "PASS")
        self.assertEqual(result["status"], STATUS_COMPLETED)

    def test_dissenting_verifier_blocks_a_unanimous_pass(self):
        config = self._ensemble_cfg(verifiers=2)
        with patch.object(graph_module, "run_antigravity", side_effect=["research", "VERDICT: FAIL\nbroken"]), \
             patch.object(graph_module, "run_claude_code", side_effect=["plan", "VERDICT: PASS\nok"]), \
             patch.object(graph_module, "run_opencode", return_value="impl"):
            result = build_graph(config).invoke(
                _state(config, max_repair_attempts=0, consensus_policy="unanimous")
            )
        self.assertEqual(result["verification_verdict"], "FAIL")
        self.assertEqual(result["status"], STATUS_FAILED)
        self.assertEqual(len(result["verification_history"]), 2)

    def test_one_blocked_verifier_blocks_the_quorum(self):
        config = self._ensemble_cfg(verifiers=2)
        blocked = "VERDICT: BLOCKED\n\nHuman Action Required:\nProvide an API key."
        with patch.object(graph_module, "run_antigravity", side_effect=["research", blocked]), \
             patch.object(graph_module, "run_claude_code", side_effect=["plan", "VERDICT: PASS\nok"]), \
             patch.object(graph_module, "run_opencode", return_value="impl"):
            result = build_graph(config).invoke(_state(config, max_repair_attempts=2))
        self.assertEqual(result["verification_verdict"], "BLOCKED")
        self.assertEqual(result["status"], STATUS_BLOCKED)
        self.assertEqual(result.get("repair_attempts", 0), 0)
        self.assertIn("API key", result["blocked_reason"])

    def test_ensemble_survives_one_dead_member(self):
        config = self._ensemble_cfg(researchers=2, verifiers=1)
        with patch.object(graph_module, "run_antigravity", side_effect=RuntimeError("agy down")), \
             patch.object(graph_module, "run_claude_code",
                          side_effect=["SURVIVING research", "plan", "VERDICT: PASS\nok"]), \
             patch.object(graph_module, "run_opencode", return_value="impl"):
            result = build_graph(config).invoke(_state(config, max_repair_attempts=0))

        self.assertEqual(result["status"], STATUS_COMPLETED)
        roles = [r["role"] for r in result["agent_results"]]
        self.assertIn("planner", roles)
        self.assertEqual(sum(1 for r in result["agent_results"] if r["status"] == "error"), 1)

    def test_phase_fails_only_when_every_member_dies(self):
        config = self._ensemble_cfg(researchers=2, verifiers=1)
        with patch.object(graph_module, "run_antigravity", side_effect=RuntimeError("agy down")), \
             patch.object(graph_module, "run_claude_code", side_effect=RuntimeError("claude down")), \
             patch.object(graph_module, "run_opencode", return_value="impl"):
            result = build_graph(config).invoke(_state(config))

        self.assertEqual(result["status"], STATUS_ERROR)
        self.assertIn("researcher", result["error"])
        self.assertNotIn("planner", [r["role"] for r in result["agent_results"]])

    def test_ensemble_outputs_reach_the_next_phase(self):
        config = self._ensemble_cfg(researchers=2, verifiers=1)
        captured = {}

        def claude(prompt, **kwargs):
            role = kwargs.get("role")
            if role == "researcher":
                return "CLAUDE-FINDINGS"
            if role == "planner":
                captured["planner"] = prompt
                return "plan"
            return "VERDICT: PASS\nok"

        with patch.object(graph_module, "run_antigravity", return_value="AGY-FINDINGS"), \
             patch.object(graph_module, "run_claude_code", side_effect=claude), \
             patch.object(graph_module, "run_opencode", return_value="impl"):
            build_graph(config).invoke(_state(config, max_repair_attempts=0))

        self.assertIn("AGY-FINDINGS", captured["planner"])
        self.assertIn("CLAUDE-FINDINGS", captured["planner"])

    def test_repair_re_enters_every_verifier(self):
        config = self._ensemble_cfg(verifiers=2)
        agy = ["research"] + ["VERDICT: FAIL\nbad"] * 3
        claude = ["plan"] + ["VERDICT: FAIL\nbad"] * 3
        with patch.object(graph_module, "run_antigravity", side_effect=agy), \
             patch.object(graph_module, "run_claude_code", side_effect=claude), \
             patch.object(graph_module, "run_opencode", return_value="impl"):
            result = build_graph(config).invoke(_state(config, max_repair_attempts=1))

        # 2 verifiers x 2 verification rounds
        self.assertEqual(len(result["verification_history"]), 4)
        self.assertEqual(result["repair_attempts"], 1)
        self.assertEqual(result["status"], STATUS_FAILED)


class TestParallelImplementerRejected(unittest.TestCase):
    """Concurrent writes by two coding agents are refused, not silently allowed."""

    def test_consecutive_implementers_rejected(self):
        with self.assertRaises(ConfigValidationError) as ctx:
            validate_config(
                _cfg([
                    {"agent": "opencode", "model": "opencode/gpt-5.1-codex", "role": "implementer"},
                    {"agent": "claude", "model": "sonnet", "role": "implementer"},
                ])
            )
        message = str(ctx.exception)
        self.assertIn("parallel", message)
        self.assertIn("escalation ladder", message)

    def test_non_consecutive_implementers_allowed(self):
        config = validate_config(
            _cfg([
                {"agent": "opencode", "model": "opencode/gpt-5.1-codex", "role": "implementer"},
                {"agent": "claude", "model": "sonnet", "role": "verifier"},
                {"agent": "claude", "model": "sonnet", "role": "implementer"},
            ])
        )
        self.assertEqual(len(config["agents"]), 3)

    def test_parallel_researchers_and_verifiers_still_allowed(self):
        config = validate_config(
            _cfg([
                {"agent": "antigravity", "model": "gemini-3.8-flash-high", "role": "researcher"},
                {"agent": "claude", "model": "sonnet", "role": "researcher"},
                {"agent": "claude", "model": "sonnet", "role": "verifier"},
                {"agent": "antigravity", "model": "gemini-3.8-flash-high", "role": "verifier"},
            ])
        )
        self.assertEqual(len(config["agents"]), 4)


# ===========================================================================
# Tier 2 #11 - Model escalation ladders
# ===========================================================================


class TestModelEscalation(unittest.TestCase):
    """A model list escalates on retry instead of repeating what just failed."""

    LADDER = ["opencode/gpt-5.1-codex", "anthropic/claude-sonnet-4-5", "google/gemini-3-pro"]

    def _ladder_cfg(self):
        return _cfg([
            {"agent": "opencode", "model": list(self.LADDER), "role": "implementer"},
            {"agent": "claude", "model": "sonnet", "role": "verifier"},
        ])

    def test_ladder_is_accepted_by_validation(self):
        config = validate_config(self._ladder_cfg())
        self.assertEqual(config["agents"][0]["model"], self.LADDER)

    def test_every_rung_is_validated(self):
        with self.assertRaises(ConfigValidationError) as ctx:
            validate_config(
                _cfg([
                    {"agent": "opencode",
                     "model": ["opencode/gpt-5.1-codex", "totally-bogus"],
                     "role": "implementer"},
                ])
            )
        self.assertIn("totally-bogus", str(ctx.exception))
        self.assertIn("escalation step 2 of 2", str(ctx.exception))

    def test_empty_ladder_rejected(self):
        with self.assertRaises(ConfigValidationError):
            validate_config(
                _cfg([{"agent": "opencode", "model": [], "role": "implementer"}])
            )

    def test_escalates_one_rung_per_repair(self):
        config = validate_config(self._ladder_cfg())
        used = []

        def track(prompt, **kwargs):
            used.append(kwargs.get("model"))
            return ("impl", {"available": False}, "headless")

        with patch.object(graph_module, "run_opencode", side_effect=track), \
             patch.object(graph_module, "run_claude_code", side_effect=["VERDICT: FAIL\nx"] * 4):
            build_graph(config).invoke(_state(config, max_repair_attempts=2))

        # Initial attempt, then a different model for each repair.
        self.assertEqual(used, self.LADDER)

    def test_ladder_clamps_at_the_last_rung(self):
        config = validate_config(
            _cfg([
                {"agent": "opencode", "model": self.LADDER[:2], "role": "implementer"},
                {"agent": "claude", "model": "sonnet", "role": "verifier"},
            ])
        )
        used = []

        def track(prompt, **kwargs):
            used.append(kwargs.get("model"))
            return ("impl", {"available": False}, "headless")

        with patch.object(graph_module, "run_opencode", side_effect=track), \
             patch.object(graph_module, "run_claude_code", side_effect=["VERDICT: FAIL\nx"] * 4):
            build_graph(config).invoke(_state(config, max_repair_attempts=2))

        self.assertEqual(used, [self.LADDER[0], self.LADDER[1], self.LADDER[1]])

    def test_preflight_accepts_a_ladder(self):
        config = validate_config(self._ladder_cfg())
        with patch.dict(
            "orchestrator.preflight.AGENT_EXECUTABLE_RESOLVERS",
            {"opencode": lambda: "oc", "claude": lambda: "cl"},
            clear=True,
        ):
            probe = probe_agent("opencode", "implementer", self.LADDER, config=config)
        self.assertTrue(probe["ok"], probe.get("errors"))

    def test_preflight_flags_a_bad_rung(self):
        config = validate_config(self._ladder_cfg())
        with patch.dict(
            "orchestrator.preflight.AGENT_EXECUTABLE_RESOLVERS",
            {"opencode": lambda: "oc"},
            clear=True,
        ):
            probe = probe_agent(
                "opencode", "implementer",
                ["opencode/gpt-5.1-codex", "nope"], config=config,
            )
        self.assertFalse(probe["ok"])


# ===========================================================================
# Tier 2 #9 - Verifier independence
# ===========================================================================


class TestVerifierIndependence(unittest.TestCase):
    """Sharing a model between planner and verifier is surfaced as a warning."""

    def test_shared_planner_model_warns(self):
        warnings = check_verifier_independence(
            _cfg([
                {"agent": "claude", "model": "sonnet", "role": "planner"},
                {"agent": "claude", "model": "sonnet", "role": "verifier"},
            ])
        )
        self.assertEqual(len(warnings), 1)
        self.assertIn("its own model wrote", warnings[0])

    def test_shared_implementer_model_warns(self):
        warnings = check_verifier_independence(
            _cfg([
                {"agent": "claude", "model": "sonnet", "role": "implementer"},
                {"agent": "claude", "model": "sonnet", "role": "verifier"},
            ])
        )
        self.assertEqual(len(warnings), 1)
        self.assertIn("its own implementation", warnings[0])

    def test_independent_verifier_produces_no_warning(self):
        self.assertEqual(
            check_verifier_independence(
                _cfg([
                    {"agent": "claude", "model": "sonnet", "role": "planner"},
                    {"agent": "claude", "model": "opus", "role": "verifier"},
                ])
            ),
            [],
        )

    def test_different_agent_is_independent(self):
        self.assertEqual(
            check_verifier_independence(
                _cfg([
                    {"agent": "claude", "model": "sonnet", "role": "planner"},
                    {"agent": "antigravity", "model": "sonnet", "role": "verifier"},
                ])
            ),
            [],
        )

    def test_no_verifier_no_warning(self):
        self.assertEqual(
            check_verifier_independence(
                _cfg([{"agent": "claude", "model": "sonnet", "role": "planner"}])
            ),
            [],
        )

    def test_warning_surfaces_in_the_preflight_report(self):
        config = validate_config(
            _cfg([
                {"agent": "claude", "model": "sonnet", "role": "planner"},
                {"agent": "claude", "model": "sonnet", "role": "verifier"},
            ])
        )
        with patch.dict(
            "orchestrator.preflight.AGENT_EXECUTABLE_RESOLVERS",
            {"claude": lambda: "cl"},
            clear=True,
        ):
            report = run_preflight(config)
        self.assertTrue(report["ok"])  # a warning, never a hard failure
        self.assertTrue(any("verifier independence" in w for w in report["warnings"]))


# ===========================================================================
# Reducer duplication regression
# ===========================================================================


class TestReducerSeeding(unittest.TestCase):
    """context_node must not re-append list channels that use `add` reducers."""

    def test_seeded_results_are_not_duplicated(self):
        seed = [{
            "agent": "x", "role": "researcher", "status": "success",
            "output": "SEEDED", "duration_seconds": 0.0,
        }]
        with patch.object(graph_module, "run_antigravity", return_value="research"), \
             patch.object(graph_module, "run_claude_code", side_effect=["plan", "VERDICT: PASS\nok"]), \
             patch.object(graph_module, "run_opencode", return_value="impl"):
            result = graph.invoke({
                "task": "t",
                "project_root": os.getcwd(),
                "run_store_enabled": False,
                "agent_results": list(seed),
                "workspace": dict(UNISOLATED),
            })
        copies = [r for r in result["agent_results"] if r.get("output") == "SEEDED"]
        self.assertEqual(len(copies), 1)


# ===========================================================================
# Configuration for the new sections
# ===========================================================================


class TestNewConfigSections(unittest.TestCase):

    def test_workspace_defaults(self):
        cfg = get_workspace_config(validate_config(_cfg(
            [{"agent": "claude", "model": "sonnet", "role": "planner"}]
        )))
        self.assertEqual(cfg["isolation"], "auto")
        self.assertTrue(cfg["commit_on_finish"])

    def test_workspace_values_round_trip(self):
        cfg = get_workspace_config(validate_config(_cfg(
            [{"agent": "claude", "model": "sonnet", "role": "planner"}],
            workspace={"isolation": "worktree", "directory": "wt", "commit_on_finish": False},
        )))
        self.assertEqual(cfg["isolation"], "worktree")
        self.assertEqual(cfg["directory"], "wt")
        self.assertFalse(cfg["commit_on_finish"])

    def test_invalid_isolation_rejected(self):
        with self.assertRaises(ConfigValidationError):
            validate_config(_cfg(
                [{"agent": "claude", "model": "sonnet", "role": "planner"}],
                workspace={"isolation": "sandbox"},
            ))

    def test_consensus_policy_round_trip(self):
        cfg = validate_config(_cfg(
            [{"agent": "claude", "model": "sonnet", "role": "verifier"}],
            verification={"consensus": "majority"},
        ))
        self.assertEqual(get_consensus_policy(cfg), "majority")

    def test_invalid_consensus_rejected(self):
        with self.assertRaises(ConfigValidationError):
            validate_config(_cfg(
                [{"agent": "claude", "model": "sonnet", "role": "verifier"}],
                verification={"consensus": "vibes"},
            ))

    def test_project_yaml_declares_both_sections(self):
        from orchestrator.config import load_config
        config = load_config(None, os.getcwd())
        self.assertIn("workspace", config)
        self.assertIn("verification", config)


if __name__ == "__main__":
    unittest.main()
