"""Package C - configuration that takes effect, budgets that survive a resume, and the cockpit rail.

Each class pins a defect found by reading the code and then reproducing it:

* **A role could not be reassigned from the daemon.** `POST /api/team` wrote `orchestrator.yaml`
  correctly, but the daemon (and the handler under it) kept the configuration it started with.
  The cockpit re-read the old team, so the save looked as if it had not happened, and the next
  goal ran the old assignment - both until a restart.
* **An agent could not be added.** The pipeline's `+ ADD AGENT` button had no listener, and the
  Team pane had no way to add or remove an entry.
* **An agent with no runner ran as Claude.** `graph.get_runner` returned Claude for any name it
  did not know, so a role assigned to a catalog provider with no runner ran someone else.
* **The wall-clock budget restarted on resume**, and **an execution killed with the process
  vanished** from the record instead of making the total visibly incomplete.
* **The sidebar rail was bracketed letters**, and its badge code rewrote the button's contents.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import MagicMock, patch

import orchestrator.graph as graph_module
from orchestrator.budget import describe_budget, evaluate_budget
from orchestrator.config import RUNNABLE_AGENTS, load_config, validate_config
from orchestrator.daemon import TOKEN_HEADER, Daemon, serve_daemon
from orchestrator.graph import UnknownAgentError, build_graph, get_runner, make_role_node
from orchestrator.metrics import aggregate_metrics, summary_rows, verify_token_aggregation_invariant
from orchestrator.preflight import AGENT_EXECUTABLE_RESOLVERS
from orchestrator.resume import active_seconds, load_resumable_run, orphaned_attempts, reconstruct_state
from orchestrator.serve import WEB_DIR
from orchestrator.store import RunStore, load_run
from orchestrator.teams import normalize_team, validate_team
from orchestrator.tracer import default_tracer
from orchestrator.types import create_token_usage

from tests.support import isolated_graph_state, redirected_run_store, temporary_store_dir

TEAM_YAML = """\
# The team, edited through the daemon. This comment must survive every save.
agents:
  - agent: claude
    model: sonnet
    role: researcher

  - agent: claude
    model: sonnet
    role: planner

  - agent: opencode
    model: opencode/gpt-5.1-codex
    role: implementer

  - agent: claude
    model: sonnet
    role: verifier

max_repair_attempts: 2

verification:
  consensus: unanimous

models:
  antigravity:
    - id: gemini-3.8-flash-high
  claude:
    - id: sonnet
    - id: opus
  opencode:
    - id: opencode/gpt-5.1-codex
  # A provider listed in the catalog that this orchestrator has no runner for.
  gemini-cli:
    - id: gemini-2.5-pro

roles:
  researcher:
    responsibility: investigate
  planner:
    responsibility: plan
  implementer:
    responsibility: write the code
  verifier:
    responsibility: check the code
"""

ROLES = {r: {"responsibility": r} for r in ("researcher", "planner", "implementer", "verifier")}


def _usage(total):
    return create_token_usage(input_tokens=total - 1, output_tokens=1, total_tokens=total)


def _team(config, **changes):
    """The config's team as the editor would post it, with entries replaced by index."""
    agents = [dict(a) for a in config["agents"]]
    for index, entry in changes.get("replace", {}).items():
        agents[index] = entry
    for index, entry in changes.get("insert", []):
        agents.insert(index, entry)
    return {"agents": agents, "consensus": "unanimous", "max_repair_attempts": 2}


# =============================================================================================
# The daemon: reassigning a role and adding an agent take effect, persist, and resolve
# =============================================================================================
class _TeamDaemonCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="orch_pkgc_")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.config_file = os.path.join(self.root, "orchestrator.yaml")
        with open(self.config_file, "w", encoding="utf-8") as handle:
            handle.write(TEAM_YAML)
        self.config = load_config(self.config_file)
        store = RunStore.create(self.root, run_id="20260912T000000Z-aaaaaa")
        store.record_run_started(task="t", project_root=self.root, config=self.config)

        self.goal_configs = []

        def runner(daemon, job):
            # What `run_delegated_goal` hands the graph is `daemon.config`.
            self.goal_configs.append(json.loads(json.dumps(daemon.config)))
            return {"summary": {"status": "delivered"}}

        self.daemon = Daemon(
            self.root, self.config, config_path=self.config_file, token="t", goal_runner=runner
        )
        self.server = serve_daemon(
            self.root, self.config, port=0, open_browser=False, serve_forever=False,
            printer=lambda *_: None, daemon=self.daemon,
        )
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]

    def tearDown(self):
        self.daemon.stopping.set()
        self.daemon.capture_agent_output(False)
        self.server.shutdown()
        self.server.server_close()

    def _call(self, path, payload=None):
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(self.base + path, data=body)
        request.add_header(TOKEN_HEADER, "t")
        if body:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8") or "{}")

    def _file(self):
        with open(self.config_file, encoding="utf-8") as handle:
            return handle.read()

    def _start_goal(self):
        self.assertEqual(self._call("/api/control/start", {"goal": "g"})[0], 200)
        deadline = time.time() + 5
        while time.time() < deadline and not self.goal_configs:
            time.sleep(0.02)
        return self.goal_configs[-1]


class TestReassigningARoleTakesEffect(_TeamDaemonCase):
    RESEARCHER_ON_AGY = {"agent": "antigravity", "model": "gemini-3.8-flash-high", "role": "researcher"}

    def test_the_cockpit_shows_the_new_assignment_without_a_restart(self):
        status, body = self._call("/api/team", _team(self.config, replace={0: self.RESEARCHER_ON_AGY}))
        self.assertEqual(status, 200, body)
        self.assertTrue(body["ok"])
        self.assertTrue(body["reloaded"])

        _, cockpit = self._call("/api/cockpit")
        self.assertEqual(cockpit["team"]["agents"][0]["agent"], "antigravity",
                         "the cockpit re-read the team the daemon started with")
        _, team = self._call("/api/team")
        self.assertEqual(team["team"]["agents"][0]["model"], "gemini-3.8-flash-high")

    def test_the_next_goal_runs_the_new_assignment(self):
        self._call("/api/team", _team(self.config, replace={0: self.RESEARCHER_ON_AGY}))
        config = self._start_goal()
        self.assertEqual(config["agents"][0]["agent"], "antigravity")

    def test_the_graph_resolves_the_new_assignment(self):
        self._call("/api/team", _team(self.config, replace={0: self.RESEARCHER_ON_AGY}))
        claude = MagicMock(return_value=("never", _usage(1)))
        with patch.object(graph_module, "run_antigravity", return_value=("research", _usage(5))) as agy, \
                patch.object(graph_module, "run_claude_code", claude):
            update = make_role_node("researcher", agent_index=0)(
                {"config": self.daemon.config, "task": "t", "agent_results": [],
                 "workspace": {"isolated": False, "path": self.root}}
            )
        agy.assert_called_once()
        claude.assert_not_called()
        self.assertEqual(update["agent_results"][0]["agent"], "antigravity")

    def test_a_restarted_daemon_keeps_it(self):
        self._call("/api/team", _team(self.config, replace={0: self.RESEARCHER_ON_AGY}))
        restarted = Daemon(self.root, load_config(self.config_file), config_path=self.config_file)
        self.assertEqual(restarted.cockpit()["team"]["agents"][0]["agent"], "antigravity")

    def test_the_other_assignments_and_the_file_s_comments_are_untouched(self):
        self._call("/api/team", _team(self.config, replace={0: self.RESEARCHER_ON_AGY}))
        written = self._file()
        self.assertIn("This comment must survive every save", written)
        self.assertIn("A provider listed in the catalog", written)
        after = load_config(self.config_file)["agents"]
        self.assertEqual(after[1:], self.config["agents"][1:])
        self.assertTrue(any(name.startswith("orchestrator.yaml.bak-") for name in os.listdir(self.root)))


class TestEverySaveKeepsItsOwnBackup(_TeamDaemonCase):
    """Found live: three saves in one second left one backup, overwritten twice, so the file as
    it was before the first save existed nowhere."""

    def test_saves_in_the_same_second_do_not_overwrite_each_other_s_backup(self):
        original = self._file()
        agy = {"agent": "antigravity", "model": "gemini-3.8-flash-high", "role": "researcher"}
        for team in (_team(self.config, replace={0: agy}), _team(self.config),
                     _team(self.config, replace={0: agy})):
            self.assertEqual(self._call("/api/team", team)[0], 200)
        backups = sorted(n for n in os.listdir(self.root) if n.startswith("orchestrator.yaml.bak-"))
        self.assertEqual(len(backups), 3)
        contents = [open(os.path.join(self.root, n), encoding="utf-8").read() for n in backups]
        self.assertIn(original, contents, "the pre-edit file must survive in some backup")


class TestAddingAnAgent(_TeamDaemonCase):
    SECOND_VERIFIER = {"agent": "claude", "model": "opus", "role": "verifier"}

    def test_an_added_agent_is_saved_shown_run_and_kept(self):
        status, body = self._call("/api/team", _team(self.config, insert=[(4, self.SECOND_VERIFIER)]))
        self.assertEqual(status, 200, body)

        _, cockpit = self._call("/api/cockpit")
        phases = cockpit["team"]["phases"]
        self.assertEqual(phases[-1]["role"], "verifier")
        self.assertEqual(len(phases[-1]["agents"]), 2, "the new verifier joins the verifier phase")
        verifier_cards = [c for c in cockpit["columns"] if c["role"] == "verifier"][0]["agents"]
        self.assertEqual(len(verifier_cards), 2)

        self.assertEqual(len(self._start_goal()["agents"]), 5)
        for entry in load_config(self.config_file)["agents"]:
            get_runner(entry["agent"])  # every entry resolves to a runner

    def test_removing_it_again_works_the_same_way(self):
        self._call("/api/team", _team(self.config, insert=[(4, self.SECOND_VERIFIER)]))
        status, _ = self._call("/api/team", _team(self.config))
        self.assertEqual(status, 200)
        self.assertEqual(len(self.daemon.config["agents"]), 4)


class TestAnInvalidAgentFailsClearly(_TeamDaemonCase):
    def _refused(self, team, needle):
        before = self._file()
        status, body = self._call("/api/team", team)
        self.assertEqual(status, 400)
        self.assertTrue(any(needle in p for p in body["problems"]), body["problems"])
        self.assertEqual(self._file(), before, "a refused team must not touch the file")
        self.assertEqual(self.daemon.config["agents"], self.config["agents"])

    def test_a_provider_with_no_runner(self):
        self._refused(
            _team(self.config, replace={0: {"agent": "gemini-cli", "model": "gemini-2.5-pro", "role": "researcher"}}),
            "no runner",
        )

    def test_a_provider_that_is_not_in_the_catalog(self):
        self._refused(_team(self.config, replace={0: {"agent": "cluade", "role": "researcher"}}),
                      "not a provider in the model catalog")

    def test_a_model_the_provider_does_not_have(self):
        self._refused(_team(self.config, replace={0: {"agent": "claude", "model": "gpt-9", "role": "researcher"}}),
                      "has no model 'gpt-9'")

    def test_two_implementers_side_by_side(self):
        self._refused(
            _team(self.config, insert=[(3, {"agent": "claude", "model": "sonnet", "role": "implementer"})]),
            "Two implementers",
        )


class TestAnAgentWithNoRunnerIsNeverRunAsAnother(unittest.TestCase):
    def test_get_runner_refuses_instead_of_returning_claude(self):
        with self.assertRaises(UnknownAgentError) as ctx:
            get_runner("gemini-cli")
        self.assertIn("does not substitute", str(ctx.exception))
        self.assertIs(get_runner("claude"), graph_module.run_claude_code)

    def test_the_runnable_list_is_the_one_preflight_probes(self):
        self.assertEqual(set(RUNNABLE_AGENTS), set(AGENT_EXECUTABLE_RESOLVERS))

    def test_the_team_editor_names_the_missing_runner(self):
        config = validate_config(json.loads(json.dumps({
            "agents": [{"agent": "claude", "role": "implementer"}, {"agent": "claude", "role": "verifier"}],
            "roles": ROLES, "models": {"claude": [{"id": "sonnet"}], "gemini-cli": [{"id": "g"}]},
        })))
        team = normalize_team({"agents": [
            {"agent": "gemini-cli", "model": "g", "role": "implementer"},
            {"agent": "claude", "model": "sonnet", "role": "verifier"},
        ]})
        self.assertTrue(any("no runner" in p for p in validate_team(team, config)))

    def test_a_phase_assigned_to_it_fails_once_and_runs_nobody_else(self):
        """With preflight skipped - the one path that reached `get_runner` with such a name."""
        store_dir = temporary_store_dir()
        self.addCleanup(shutil.rmtree, store_dir, ignore_errors=True)
        config = redirected_run_store(validate_config({
            "agents": [
                {"agent": "gemini-cli", "role": "researcher"},
                {"agent": "opencode", "role": "implementer"},
                {"agent": "claude", "role": "verifier"},
            ],
            "roles": ROLES,
            "models": {"claude": [{"id": "sonnet"}], "opencode": [{"id": "x/y"}], "gemini-cli": [{"id": "g"}]},
            "preflight": {"enabled": False},
            "execution": {"retry": {"attempts": 3, "backoff_seconds": 0}},
        }), store_dir)
        claude, opencode = MagicMock(), MagicMock()
        with patch.object(graph_module, "run_claude_code", claude), \
                patch.object(graph_module, "run_opencode", opencode):
            final = build_graph(config).invoke(isolated_graph_state(config, "t", store_dir))
        claude.assert_not_called()
        opencode.assert_not_called()
        researcher = [r for r in final["agent_results"] if r["role"] == "researcher"][0]
        self.assertEqual(researcher["status"], "error")
        self.assertIn("no runner for agent 'gemini-cli'", researcher["output"])
        self.assertNotIn("attempts", researcher, "a missing runner is not a flake to retry")


# =============================================================================================
# The time budget, and unknown spend, across a resume
# =============================================================================================
def _stamp(seconds):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(1_800_000_000 + seconds)) + "+00:00"


class TestTheTimeBudgetSpansTheRun(unittest.TestCase):
    def test_active_time_counts_each_session_and_not_the_downtime_between(self):
        events = [
            {"event": "run_started", "timestamp": _stamp(0)},
            {"event": "agent_result", "timestamp": _stamp(30)},
            # killed; resumed an hour later
            {"event": "run_resumed", "timestamp": _stamp(3630)},
            {"event": "agent_result", "timestamp": _stamp(3650)},
        ]
        self.assertEqual(active_seconds(events), 50.0)

    def test_the_ceiling_is_measured_against_the_whole_run(self):
        now = time.time()
        fresh = {"run_started_at": now - 10, "budget": {"max_duration_seconds": 60}}
        self.assertFalse(evaluate_budget(fresh, now=now)["exhausted"])
        resumed = dict(fresh, prior_elapsed_seconds=55)
        state = evaluate_budget(resumed, now=now)
        self.assertTrue(state["exhausted"])
        self.assertIn("time budget exhausted: 65s of 60s", state["reason"])


class _ControlledRun(unittest.TestCase):
    """The real graph, store, resume and budget; scripted agents with an independent ledger."""

    def setUp(self):
        default_tracer.clear()
        self.store_dir = temporary_store_dir()
        self.addCleanup(shutil.rmtree, self.store_dir, ignore_errors=True)
        self.workspace = {"isolated": False, "path": os.getcwd(), "reason": "test"}

    def _config(self, **extra):
        raw = {
            "agents": [
                {"agent": "claude", "model": "sonnet", "role": "researcher"},
                {"agent": "opencode", "model": "opencode/gpt-5.1-codex", "role": "implementer"},
                {"agent": "claude", "model": "sonnet", "role": "verifier"},
            ],
            "roles": dict(ROLES),
            "preflight": {"enabled": False},
            "max_repair_attempts": 2,
            "execution": {"retry": {"attempts": 1, "backoff_seconds": 0}},
        }
        raw.update(extra)
        return redirected_run_store(validate_config(raw), self.store_dir)

    def _crash_in_the_implementer(self, config, research_seconds=0.0):
        def research(prompt, **kwargs):
            time.sleep(research_seconds)
            return ("research", _usage(100))

        with patch.object(graph_module, "run_claude_code", side_effect=research), \
                patch.object(graph_module, "run_opencode", side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                build_graph(config).invoke(isolated_graph_state(config, "task", self.store_dir))
        return os.listdir(self.store_dir)[0]

    def _resume_like_the_cli(self, config, run_id):
        """What `python -m orchestrator --resume` does: reconstruct, then record the resume."""
        run = load_resumable_run(os.getcwd(), run_id, directory=self.store_dir)
        state = reconstruct_state(run, project_root=os.getcwd())
        RunStore.from_dir(str(run["run_dir"])).record_run_resumed(replayed_roles=["researcher"])
        state["config"] = config
        state["workspace"] = self.workspace
        return state


class TestTheBudgetContinuesAfterAResume(_ControlledRun):
    def test_a_resumed_run_does_not_get_a_fresh_clock(self):
        """The first session spends 2.3s; the ceiling is 2s. Before Package C the resumed
        session measured only itself, found the budget untouched, and paid for a repair.

        Event timestamps have one-second resolution, so a session's measured time is a floor
        within a second of the truth: 2.3s of work reads as at least 2."""
        config = self._config(budget={"max_duration_seconds": 2})
        run_id = self._crash_in_the_implementer(config, research_seconds=2.3)
        state = self._resume_like_the_cli(config, run_id)
        self.assertGreaterEqual(state["prior_elapsed_seconds"], 2.0)

        opencode = MagicMock(return_value=("impl", _usage(50), "headless"))
        with patch.object(graph_module, "run_claude_code", return_value=("VERDICT: FAIL\nbroken", _usage(10))), \
                patch.object(graph_module, "run_opencode", opencode):
            final = build_graph(config).invoke(state)
        self.assertEqual(final["status"], "budget_exhausted")
        self.assertEqual(opencode.call_count, 1, "no repair may be funded past the run's ceiling")
        self.assertGreaterEqual(final["summary"]["duration_seconds"], 2.0)


class TestAnExecutionKilledWithTheProcess(_ControlledRun):
    def test_its_spend_is_unknown_not_zero_and_counted_once_across_resumes(self):
        config = self._config()
        run_id = self._crash_in_the_implementer(config)
        events = load_run(os.getcwd(), run_id, directory=self.store_dir)["events"]
        orphans = orphaned_attempts(events)
        self.assertEqual([(o["role"], o["attempt"], o["in_flight"]) for o in orphans],
                         [("implementer", 1, True)])
        self.assertFalse(orphans[0]["token_usage"]["available"])

        with patch.object(graph_module, "run_claude_code", return_value=("VERDICT: PASS\nok", _usage(10))), \
                patch.object(graph_module, "run_opencode", return_value=("impl", _usage(50), "headless")):
            final = build_graph(config).invoke(self._resume_like_the_cli(config, run_id))
        self.assertEqual(final["status"], "completed")

        metrics = aggregate_metrics(final["agent_results"])
        self.assertEqual(metrics["known_total_tokens"], 100 + 50 + 10)
        self.assertFalse(metrics["all_tokens_available"])
        self.assertTrue(verify_token_aggregation_invariant(final["agent_results"])["is_valid"])
        labels = " ".join(r["label"] for r in summary_rows(final["agent_results"]))
        self.assertIn("stopped mid-run, before resume", labels)

        budget = evaluate_budget({"agent_results": final["agent_results"]})
        self.assertFalse(budget["tokens_complete"])
        self.assertTrue(describe_budget(budget).startswith("Budget: at least 160 tokens"))

        # Accounted for now: a second resume finds nothing, replays everything, adds nothing.
        after = load_run(os.getcwd(), run_id, directory=self.store_dir)["events"]
        self.assertEqual(orphaned_attempts(after), [])
        with patch.object(graph_module, "run_claude_code", side_effect=AssertionError("replay")), \
                patch.object(graph_module, "run_opencode", side_effect=AssertionError("replay")):
            again = build_graph(config).invoke(self._resume_like_the_cli(config, run_id))
        self.assertEqual(again["status"], "completed")
        self.assertEqual(aggregate_metrics(again["agent_results"])["known_total_tokens"], 160)
        self.assertEqual(len([r for r in again["agent_results"] if r["role"] == "implementer"]), 1)


# =============================================================================================
# The cockpit: the icon rail, and the Team pane's add/remove
# =============================================================================================
class TestTheSidebarRail(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = (WEB_DIR / "cockpit.html").read_text(encoding="utf-8")
        cls.rail = re.search(r'<nav class="activity-rail".*?</nav>', cls.text, re.S).group(0)
        cls.buttons = re.findall(r"<button class=\"rail-btn[^\"]*\"(.*?)>(.*?)</button>", cls.rail, re.S)

    def test_four_labelled_icon_buttons_and_no_letters(self):
        self.assertEqual(len(self.buttons), 4)
        for attrs, inner in self.buttons:
            pane = re.search(r'data-pane="(\w+)"', attrs).group(1)
            with self.subTest(pane=pane):
                self.assertRegex(attrs, r'title="\w+"')
                self.assertRegex(attrs, r'aria-label="\w+"')
                self.assertRegex(attrs, r'aria-pressed="(true|false)"')
                self.assertIn('<svg class="rail-icon"', inner)
                self.assertIn('aria-hidden="true"', inner)
                self.assertNotRegex(inner, r"\[[A-Z$]\]")
        self.assertEqual([re.search(r'data-pane="(\w+)"', a).group(1) for a, _ in self.buttons],
                         ["explorer", "team", "board", "terminals"])

    def test_the_icons_are_distinct(self):
        shapes = [re.sub(r"\s+", "", inner) for _, inner in self.buttons]
        self.assertEqual(len(set(shapes)), 4)

    def test_the_active_pane_is_announced_and_the_badge_keeps_the_icon(self):
        self.assertIn("b.setAttribute('aria-pressed', on ? 'true' : 'false')", self.text)
        badge = re.search(r"function updateRailBadges\(\) \{(.*?)\n    \}", self.text, re.S).group(1)
        self.assertNotIn("innerHTML", badge, "rewriting the button would erase its icon")
        self.assertNotIn("[B]", self.text)

    def test_the_icons_are_drawn_in_the_theme_colour(self):
        self.assertRegex(self.text, r"\.rail-icon \{[^}]*stroke: currentColor")


class TestTheTeamPaneCanAddAndRemove(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = (WEB_DIR / "cockpit.html").read_text(encoding="utf-8")

    def test_the_pipeline_s_add_agent_button_is_wired(self):
        self.assertIn("footer.querySelector('.add-agent-btn').addEventListener('click'", self.text)
        self.assertIn("addAgentToDraft(col.role)", self.text)

    def test_the_team_pane_adds_removes_and_rereads_what_was_saved(self):
        self.assertIn('id="team-add-btn"', self.text)
        self.assertIn("data-team-remove=", self.text)
        save = re.search(r"async function saveTeam\(\) \{(.*?)\n    \}\n", self.text, re.S).group(1)
        self.assertIn("state.teamDraft = null", save)
        self.assertIn("await fetchCockpit()", save)

    def test_the_role_order_comes_from_the_project_not_the_page(self):
        body = re.search(r"function addAgentToDraft\(role\) \{(.*?)\n    \}\n", self.text, re.S).group(1)
        self.assertIn("Object.keys(catalog.roles", body)
        for role in ("researcher", "planner", "implementer", "verifier"):
            self.assertNotIn("'%s'" % role, body)

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_the_page_s_script_parses(self):
        scripts = re.findall(r"<script>(.*?)</script>", self.text, re.S)
        self.assertTrue(scripts)
        path = os.path.join(tempfile.mkdtemp(prefix="orch_js_"), "cockpit.js")
        self.addCleanup(shutil.rmtree, os.path.dirname(path), ignore_errors=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("\n;\n".join(scripts))
        checked = subprocess.run(["node", "--check", path], capture_output=True, text=True)
        self.assertEqual(checked.returncode, 0, checked.stderr)


# =============================================================================================
# Test safety: the pty is a second way to start a process, and it is guarded too
# =============================================================================================
class TestTheOfflineSuiteCannotStartAnAgentOnAPty(unittest.TestCase):
    def setUp(self):
        if os.environ.get("RUN_LIVE_TESTS") == "1":
            self.skipTest("live tests deliberately allow real agent CLIs")
        try:
            import winpty  # noqa: F401
        except Exception:
            self.skipTest("pywinpty is not installed")

    def test_pywinpty_refuses_an_agent_binary(self):
        import winpty

        for argv in (["opencode", "attach", "http://127.0.0.1:1"],
                     [r"C:\npm\node_modules\opencode-ai\bin\opencode.exe", "attach"],
                     ["CLAUDE.CMD"], ["agy", "-p", "x"]):
            with self.subTest(argv=argv[0]), self.assertRaises(RuntimeError):
                winpty.PtyProcess.spawn(argv)

    def test_the_daemon_s_pty_runner_is_refused_before_anything_starts(self):
        from orchestrator.terminals import run_captured_pty

        with self.assertRaises(RuntimeError):
            run_captured_pty(["opencode", "attach", "http://127.0.0.1:1"], timeout=5)


# =============================================================================================
# Known cleanup: ghost worktree directories are removed - and only those
# =============================================================================================
class TestGhostWorktreeDirectories(unittest.TestCase):
    """Real git. ARCHITECTURE.md §30.3: a worktree git deregistered but Windows would not let it
    delete left an empty directory nothing ever removed (ten on this machine)."""

    def setUp(self):
        from orchestrator.workspace import remove_ghost_worktree_dirs

        self.sweep = remove_ghost_worktree_dirs
        self.root = tempfile.mkdtemp(prefix="orch_ghost_")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

        def git(*args):
            subprocess.run(["git", *args], cwd=self.root, capture_output=True, check=True)

        git("init", "-q")
        git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "--allow-empty", "-q", "-m", "init")
        base = os.path.join(self.root, ".orchestrator", "worktrees")
        os.makedirs(base)
        self.live = os.path.join(base, "live")
        git("worktree", "add", "-q", self.live)
        self.ghost = os.path.join(base, "ghost")
        self.fresh = os.path.join(base, "fresh")
        self.held = os.path.join(base, "held")
        for path in (self.ghost, self.fresh, self.held):
            os.makedirs(path)
        with open(os.path.join(self.held, "notes.txt"), "w") as handle:
            handle.write("somebody's")
        old = time.time() - 3600
        for path in (self.ghost, self.held, self.live):
            os.utime(path, (old, old))

    def _norm(self, paths):
        return sorted(os.path.normcase(os.path.abspath(p)) for p in paths)

    def test_only_an_old_empty_untracked_directory_is_removed(self):
        outcome = self.sweep(self.root)
        self.assertEqual(self._norm(outcome["removed"]), self._norm([self.ghost]))
        self.assertEqual(self._norm(outcome["kept"]), self._norm([self.held]))
        self.assertFalse(os.path.exists(self.ghost))
        self.assertTrue(os.path.isfile(os.path.join(self.held, "notes.txt")), "content is never removed")
        self.assertTrue(os.path.isdir(self.live), "a registered worktree is never touched")
        self.assertTrue(os.path.isdir(self.fresh), "a directory being created right now is never swept")

    def test_a_dry_run_touches_nothing(self):
        outcome = self.sweep(self.root, dry_run=True)
        self.assertEqual(self._norm(outcome["removed"]), self._norm([self.ghost]))
        self.assertTrue(os.path.isdir(self.ghost))


if __name__ == "__main__":
    unittest.main()
