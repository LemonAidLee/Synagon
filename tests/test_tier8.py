"""Tests for the daemon, the cockpit, the live terminal, and the desktop shell.

Covers:
  Phase 8   The daemon shell - orchestrator.daemon: the token, the control API, the live
            stream, and the supervision of several goals at once
  Phase 9   The cockpit - orchestrator.cockpit's projection, and the one read the page makes
  Phase 10  The embedded live terminal - orchestrator.terminals, and the single hook in
            launcher.py that feeds it
  Phase 11  The desktop shell - what `desktop/` is allowed to be

Three things here deliberately use the real thing rather than a mock, because a mock could not
prove what they exist to prove: the daemon tests start a real server and make real HTTP
requests - including the ones that must be refused for want of a token and for a foreign
Origin; the capture tests run a real subprocess and read its output as it arrives; and the
shell tests read the real files in `desktop/`, because a claim about what a shell may do is
only worth making against what is actually on disk.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import re
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch

from orchestrator.agents.exceptions import CLIExecutionError
from orchestrator.approvals import (
    GATE_BEFORE_MERGE,
    STATUS_APPROVED,
    STATUS_PENDING,
    STATUS_REJECTED,
    list_approvals,
    request_approval,
)
from orchestrator.cockpit import (
    AGENT_DONE,
    AGENT_FAILED,
    AGENT_IDLE,
    AGENT_WORKING,
    agent_key,
    agent_states,
    column_state,
    columns,
    needs_you,
    ready_to_merge,
)
from orchestrator.config import validate_config
from orchestrator.daemon import (
    JOB_CANCELLED,
    JOB_FAILED,
    JOB_FINISHED,
    JOB_RUNNING,
    MAX_CONTROL_BYTES,
    TOKEN_HEADER,
    Daemon,
    EventTail,
    Job,
    clear_handshake,
    handshake_path,
    mint_token,
    newest_run_dir,
    origin_ok,
    read_handshake,
    read_run_meta,
    run_dir_for,
    serve_daemon,
    sse_frame,
    token_ok,
    write_handshake,
)
from orchestrator.status import STATUS_COMPLETED
from orchestrator.store import RunStore
from orchestrator.teams import team_from_config
from orchestrator.terminals import (
    TERMINAL_EXITED,
    TERMINAL_RUNNING,
    TerminalRegistry,
    TerminalStream,
    pty_available,
    run_captured,
)

ROLES = {
    "implementer": {"responsibility": "write the code"},
    "verifier": {"responsibility": "check the code"},
}

PIPELINE = [
    {"agent": "opencode", "model": "opencode/gpt-5.1-codex", "role": "implementer"},
    {"agent": "claude", "model": "sonnet", "role": "verifier"},
]

MINIMAL_YAML = """\
agents:
  - agent: opencode
    model: opencode/gpt-5.1-codex
    role: implementer

  - agent: claude
    model: sonnet
    role: verifier

max_repair_attempts: 2

verification:
  consensus: unanimous

roles:
  implementer:
    responsibility: write the code
  verifier:
    responsibility: check the code
"""

REPO_ROOT = Path(__file__).resolve().parent.parent


def _cfg(**extra):
    config = {"agents": list(PIPELINE), "roles": dict(ROLES)}
    config.update(extra)
    return validate_config(config)


def _started(agent, role, sequence, model="sonnet"):
    return {
        "event": "agent_started",
        "sequence": sequence,
        "agent": agent,
        "role": role,
        "model": model,
    }


def _result(agent, role, sequence, verdict=None, status=None, output="", tokens=0):
    # Mirrors types.create_agent_result: "status" ("success"/"error") is the only failure
    # signal most roles ever carry, "verdict" is set only for a verifier, and there is no
    # "error" key on the dict at all - a fixture that invented one would hide exactly the
    # schema mismatch this file exists to catch.
    result = {
        "agent": agent,
        "role": role,
        "status": status or ("error" if verdict in ("FAIL", "ERROR", "BLOCKED", "TIMEOUT")
                              else "success"),
        "output": output,
        "duration_seconds": 1.5,
        "token_usage": {"available": bool(tokens), "total_tokens": tokens},
    }
    if verdict is not None:
        result["verdict"] = verdict
    return {"event": "agent_result", "sequence": sequence, "result": result}


# ===========================================================================
# Phase 8 - the token, and the handshake
# ===========================================================================


class TestTheToken(unittest.TestCase):
    """A loopback port that can start work needs more than being on loopback."""

    def test_a_token_is_per_launch(self):
        self.assertNotEqual(mint_token(), mint_token())
        self.assertGreater(len(mint_token()), 20)

    def test_only_the_right_token_is_accepted(self):
        token = mint_token()
        self.assertTrue(token_ok(token, token))
        self.assertFalse(token_ok(token + "x", token))
        self.assertFalse(token_ok("", token))
        self.assertFalse(token_ok(None, token))

    def test_no_token_configured_accepts_nothing(self):
        # A daemon that somehow lost its token must refuse everything, not everyone.
        self.assertFalse(token_ok("anything", ""))
        self.assertFalse(token_ok("anything", None))

    def test_a_missing_origin_is_a_script_and_is_allowed(self):
        self.assertTrue(origin_ok(None, 8740))
        self.assertTrue(origin_ok("", 8740))

    def test_our_own_origins_are_allowed(self):
        self.assertTrue(origin_ok("http://127.0.0.1:8740", 8740))
        self.assertTrue(origin_ok("http://localhost:8740", 8740))
        self.assertTrue(origin_ok("http://127.0.0.1:8740/", 8740))

    def test_a_foreign_origin_is_refused(self):
        # The case this exists for: a page open in the same browser reaching for 127.0.0.1.
        self.assertFalse(origin_ok("http://evil.example", 8740))
        self.assertFalse(origin_ok("https://127.0.0.1:8740", 8740))
        self.assertFalse(origin_ok("http://127.0.0.1:8731", 8740))


class TestTheHandshake(unittest.TestCase):
    """How a local client finds a running daemon."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="orch_t8shake_")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_it_round_trips(self):
        path = write_handshake(self.root, 8741, "abc123")
        self.assertIsNotNone(path)
        self.assertEqual(path, handshake_path(self.root))
        payload = read_handshake(self.root)
        self.assertEqual(payload["port"], 8741)
        self.assertEqual(payload["token"], "abc123")
        self.assertEqual(payload["project_root"], self.root)
        self.assertEqual(payload["pid"], os.getpid())

    def test_nothing_running_reads_as_nothing(self):
        self.assertIsNone(read_handshake(self.root))

    def test_stopping_removes_it(self):
        write_handshake(self.root, 8741, "abc123")
        self.assertTrue(clear_handshake(self.root))
        self.assertIsNone(read_handshake(self.root))
        # Clearing what is not there is not an error - a crashed daemon leaves no file.
        self.assertFalse(clear_handshake(self.root))

    def test_a_corrupt_handshake_reads_as_nothing(self):
        path = handshake_path(self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        self.assertIsNone(read_handshake(self.root))


# ===========================================================================
# Phase 8 - supervision
# ===========================================================================


class TestJobs(unittest.TestCase):
    """The daemon holds jobs; it does not hold facts about runs."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="orch_t8jobs_")
        self.ran = threading.Event()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _daemon(self, runner=None, max_jobs=8):
        return Daemon(self.root, _cfg(), goal_runner=runner or self._ok, max_jobs=max_jobs)

    def _ok(self, daemon, job):
        daemon.set_state(job, JOB_RUNNING, goal_id="goal-1")
        self.ran.set()
        return {"summary": {"status": "delivered"}}

    def _await(self, daemon, job_id, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            job = daemon.find_job(job_id)
            if job and job.done:
                return job
            time.sleep(0.02)
        self.fail("the job did not finish")

    def test_a_goal_runs_and_finishes(self):
        daemon = self._daemon()
        result = daemon.start_goal("do the thing")
        self.assertTrue(result["ok"])
        job = self._await(daemon, result["job"]["job_id"])
        self.assertEqual(job.state, JOB_FINISHED)
        self.assertEqual(job.goal_id, "goal-1")
        self.assertTrue(self.ran.is_set())

    def test_an_empty_goal_is_refused(self):
        daemon = self._daemon()
        self.assertFalse(daemon.start_goal("   ")["ok"])
        self.assertEqual(daemon.jobs(), [])

    def test_a_crashed_goal_does_not_take_the_daemon_with_it(self):
        def explode(daemon, job):
            raise RuntimeError("the planner fell over")

        daemon = self._daemon(explode)
        result = daemon.start_goal("do the thing")
        job = self._await(daemon, result["job"]["job_id"])
        self.assertEqual(job.state, JOB_FAILED)
        self.assertIn("fell over", job.error)
        # And the daemon still takes work afterwards.
        self.assertTrue(daemon.start_goal("something else")["ok"])

    def test_a_returned_error_fails_the_job(self):
        daemon = self._daemon(lambda d, j: {"error": "could not decompose"})
        result = daemon.start_goal("vague")
        job = self._await(daemon, result["job"]["job_id"])
        self.assertEqual(job.state, JOB_FAILED)
        self.assertEqual(job.error, "could not decompose")

    def test_the_ceiling_refuses_rather_than_queues(self):
        # A UI that banked clicks would be a scheduler, which is the thing invariant 12
        # exists to prevent.
        release = threading.Event()

        def wait(daemon, job):
            release.wait(5)
            return {}

        daemon = self._daemon(wait, max_jobs=2)
        self.assertTrue(daemon.start_goal("one")["ok"])
        self.assertTrue(daemon.start_goal("two")["ok"])
        refused = daemon.start_goal("three")
        self.assertFalse(refused["ok"])
        self.assertIn("already open", refused["error"])
        release.set()

    def test_cancelling_is_cooperative_and_says_so(self):
        seen = []
        release = threading.Event()

        def runner(daemon, job):
            daemon.set_state(job, JOB_RUNNING)
            release.wait(5)
            seen.append(job.cancel.is_set())
            return {"summary": None}

        daemon = self._daemon(runner)
        result = daemon.start_goal("long one")
        job_id = result["job"]["job_id"]
        cancelled = daemon.cancel_job(job_id)
        self.assertTrue(cancelled["ok"])
        release.set()
        job = self._await(daemon, job_id)
        self.assertEqual(job.state, JOB_CANCELLED)
        self.assertEqual(seen, [True])

    def test_cancelling_a_finished_job_is_refused(self):
        daemon = self._daemon()
        result = daemon.start_goal("quick")
        self._await(daemon, result["job"]["job_id"])
        again = daemon.cancel_job(result["job"]["job_id"])
        self.assertFalse(again["ok"])
        self.assertIn("already", again["error"])

    def test_a_job_id_prefix_resolves(self):
        daemon = self._daemon()
        result = daemon.start_goal("do the thing")
        job_id = result["job"]["job_id"]
        self.assertIsNotNone(daemon.find_job(job_id[:6]))
        self.assertIsNone(daemon.find_job("zzzzzz"))
        self.assertIsNone(daemon.find_job(""))

    def test_the_version_moves_when_a_job_does(self):
        daemon = self._daemon()
        before = daemon.jobs_version()
        daemon.start_goal("do the thing")
        self.assertGreater(daemon.jobs_version(), before)

    def test_a_snapshot_shows_only_supervision(self):
        job = Job("abc", "goal", "do the thing", 2)
        snapshot = job.snapshot()
        self.assertEqual(snapshot["job_id"], "abc")
        self.assertEqual(snapshot["goal"], "do the thing")
        self.assertEqual(snapshot["parallel"], 2)
        self.assertFalse(snapshot["cancel_requested"])


class TestControlIsAClosedList(unittest.TestCase):
    """What a click can ask for is enumerated, not open-ended."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="orch_t8ctl_")
        self.daemon = Daemon(self.root, _cfg(), goal_runner=lambda d, j: {})

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_an_unknown_verb_is_not_dispatched(self):
        result = self.daemon.control("merge_everything", {})
        self.assertFalse(result["ok"])
        self.assertTrue(result["unknown"])

    def test_start_accepts_goal_or_task(self):
        self.assertTrue(self.daemon.control("start", {"goal": "a"})["ok"])
        self.assertTrue(self.daemon.control("start", {"task": "b"})["ok"])

    def test_a_bad_parallel_value_does_not_crash_the_start(self):
        result = self.daemon.control("start", {"goal": "a", "parallel": "lots"})
        self.assertTrue(result["ok"])
        self.assertIsNone(result["job"]["parallel"])

    def test_an_unknown_decision_is_refused(self):
        self.assertFalse(self.daemon.decide("anything", "maybe")["ok"])


class TestAnsweringGates(unittest.TestCase):
    """Approving from a click calls exactly what `--approve` calls."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="orch_t8gate_")
        self.daemon = Daemon(self.root, _cfg(), goal_runner=lambda d, j: {})
        self.approval = request_approval(
            self.root, GATE_BEFORE_MERGE, subject="the work", branch="orch/run-1"
        )

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_approving_records_the_decision(self):
        result = self.daemon.control("approve", {"id": self.approval["id"], "note": "ok"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["approval"]["status"], STATUS_APPROVED)
        self.assertEqual(result["approval"]["note"], "ok")
        stored = list_approvals(self.root, status=STATUS_APPROVED)
        self.assertEqual(len(stored), 1)

    def test_rejecting_records_the_decision(self):
        result = self.daemon.control("reject", {"id": self.approval["id"]})
        self.assertTrue(result["ok"])
        self.assertEqual(result["approval"]["status"], STATUS_REJECTED)

    def test_an_answered_gate_is_not_answered_twice(self):
        self.daemon.control("approve", {"id": self.approval["id"]})
        again = self.daemon.control("reject", {"id": self.approval["id"]})
        self.assertFalse(again["ok"])
        self.assertIn("already", again["error"])

    def test_an_unknown_gate_is_refused(self):
        self.assertFalse(self.daemon.control("approve", {"id": "nope"})["ok"])


class TestDeliveringFromAClick(unittest.TestCase):
    """The daemon does not get its own opinion about what may be pushed."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="orch_t8deliver_")
        self.daemon = Daemon(self.root, _cfg(), goal_runner=lambda d, j: {})

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_an_unknown_card_is_refused(self):
        result = self.daemon.deliver("nothing-like-this")
        self.assertFalse(result["ok"])
        self.assertIn("no card matching", result["error"])

    def test_a_card_that_is_not_ready_is_refused_before_anything_is_pushed(self):
        store = RunStore.create(self.root, run_id="20260907T000000Z-aaaaaa")
        store.record_run_started(task="do the thing", project_root=self.root, config=_cfg())
        store.record_run_finished({"status": "failed", "verdict": "FAIL"})

        result = self.daemon.deliver("20260907T000000Z-aaaaaa")
        self.assertFalse(result["ok"])
        self.assertIn("Ready to Merge", result["error"])
        self.assertIn("not", result["error"])


# ===========================================================================
# Phase 8 - the live stream's reader
# ===========================================================================


class TestEventTail(unittest.TestCase):
    """Following an append-only log by offset, not by re-reading its tail."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="orch_t8tail_")
        self.path = Path(self.root) / "events.jsonl"

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _append(self, *events):
        with open(self.path, "a", encoding="utf-8") as handle:
            for event in events:
                handle.write(json.dumps(event) + "\n")

    def test_a_missing_log_is_empty_not_an_error(self):
        self.assertEqual(EventTail(self.path).read_new(), [])

    def test_only_what_is_new_comes_back(self):
        self._append({"sequence": 1}, {"sequence": 2})
        tail = EventTail(self.path)
        self.assertEqual([e["sequence"] for e in tail.read_new()], [1, 2])
        self.assertEqual(tail.read_new(), [])
        self._append({"sequence": 3})
        self.assertEqual([e["sequence"] for e in tail.read_new()], [3])

    def test_a_replaced_log_is_read_from_the_start_again(self):
        self._append({"sequence": 1}, {"sequence": 2}, {"sequence": 3})
        tail = EventTail(self.path)
        tail.read_new()
        self.path.write_text(json.dumps({"sequence": 1}) + "\n", encoding="utf-8")
        self.assertEqual([e["sequence"] for e in tail.read_new()], [1])

    def test_a_half_written_line_is_skipped_not_fatal(self):
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"sequence": 1}) + "\n")
            handle.write('{"sequence": 2, "eve')
        tail = EventTail(self.path)
        self.assertEqual([e["sequence"] for e in tail.read_new()], [1])

    def test_a_log_that_cannot_be_read_degrades_and_records_the_reason(self):
        # Invariant 6: a reader that cannot read is a reader that returns nothing, not
        # one that raises - but the reason must be observable, not a silent swallow that
        # makes a permissions error look identical to an empty log.
        tail = EventTail(self.path)
        with patch.object(type(self.path), "stat", side_effect=PermissionError("denied")), \
                self.assertLogs("orchestrator.daemon", level="WARNING") as logs:
            self.assertEqual(tail.read_new(), [])
        self.assertTrue(
            any("could not read event tail" in line for line in logs.output), logs.output
        )

    def test_seek_end_starts_from_now(self):
        self._append({"sequence": 1})
        tail = EventTail(self.path)
        tail.seek_end()
        self.assertEqual(tail.read_new(), [])

    def test_a_frame_is_well_formed(self):
        frame = sse_frame("orchestrator", {"sequence": 4}).decode("utf-8")
        self.assertTrue(frame.startswith("event: orchestrator\ndata: "))
        self.assertTrue(frame.endswith("\n\n"))
        self.assertEqual(json.loads(frame.split("data: ", 1)[1].strip())["sequence"], 4)


class TestResolvingARun(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="orch_t8run_")
        self.config = _cfg()
        store = RunStore.create(self.root, run_id="20260907T000000Z-aaaaaa")
        store.record_run_started(task="first", project_root=self.root, config=self.config)
        store.record_run_finished({"status": STATUS_COMPLETED, "verdict": "PASS"})
        self.later = RunStore.create(self.root, run_id="20260907T010000Z-bbbbbb")
        self.later.record_run_started(task="second", project_root=self.root, config=self.config)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_no_run_named_means_the_newest(self):
        found = run_dir_for(self.root, self.config, None)
        self.assertEqual(found.name, "20260907T010000Z-bbbbbb")
        self.assertEqual(read_run_meta(found)["task"], "second")

    def test_a_prefix_resolves(self):
        found = run_dir_for(self.root, self.config, "20260907T000000Z")
        self.assertEqual(found.name, "20260907T000000Z-aaaaaa")

    def test_an_unknown_run_is_none(self):
        self.assertIsNone(run_dir_for(self.root, self.config, "nope"))

    def test_meta_of_nothing_is_empty(self):
        self.assertEqual(read_run_meta(None), {})

    def test_an_unreadable_runs_root_degrades_and_records_the_reason(self):
        # Invariant 6 again: no run store is a missing run dir, and a run dir that cannot
        # be read is the same thing - never an exception up the daemon's control stack.
        with patch("orchestrator.daemon.Path.iterdir",
                   side_effect=PermissionError("denied")), \
                self.assertLogs("orchestrator.daemon", level="WARNING") as logs:
            self.assertIsNone(newest_run_dir(self.root, self.config))
        self.assertTrue(
            any("could not list run directories" in line for line in logs.output),
            logs.output,
        )

    def test_resolving_a_prefix_that_cannot_be_listed_degrades_and_records_the_reason(self):
        with patch("orchestrator.daemon.Path.iterdir",
                   side_effect=PermissionError("denied")), \
                self.assertLogs("orchestrator.daemon", level="WARNING") as logs:
            self.assertIsNone(run_dir_for(self.root, self.config, "20260907"))
        self.assertTrue(
            any("could not resolve run" in line for line in logs.output), logs.output
        )

    def test_metadata_that_cannot_be_read_degrades_and_records_the_reason(self):
        # A corrupt or unreadable run.json is an empty dict plus a recorded reason; it is
        # never a crash, and never indistinguishable from a run that simply has no metadata.
        found = run_dir_for(self.root, self.config, None)
        meta_file = found / "run.json"
        meta_file.write_text("{not json", encoding="utf-8")
        with self.assertLogs("orchestrator.daemon", level="WARNING") as logs:
            self.assertEqual(read_run_meta(found), {})
        self.assertTrue(
            any("could not read run metadata" in line for line in logs.output), logs.output
        )


# ===========================================================================
# Phase 8 - the real server
# ===========================================================================


class _DaemonCase(unittest.TestCase):
    """A real daemon on a real port, with a goal runner that runs no pipeline."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="orch_t8serve_")
        self.config_file = os.path.join(self.root, "orchestrator.yaml")
        with open(self.config_file, "w", encoding="utf-8") as handle:
            handle.write(MINIMAL_YAML)

        self.store = RunStore.create(self.root, run_id="20260907T000000Z-aaaaaa")
        self.store.record_run_started(
            task="do the thing", project_root=self.root, config=_cfg()
        )

        self.started = []

        def runner(daemon, job):
            self.started.append(job.goal)
            daemon.set_state(job, JOB_RUNNING, goal_id="goal-1")
            return {"summary": {"status": "delivered"}}

        self.daemon = Daemon(
            self.root, _cfg(), config_path=self.config_file,
            token="test-token", goal_runner=runner,
        )
        self.server = serve_daemon(
            self.root, _cfg(), port=0, open_browser=False, serve_forever=False,
            printer=lambda *_: None, daemon=self.daemon,
        )
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]

    def tearDown(self):
        self.daemon.stopping.set()
        self.daemon.capture_agent_output(False)
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.root, ignore_errors=True)

    def _call(self, path, payload=None, token="test-token", origin=None, raw=False):
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(self.base + path, data=body)
        if token:
            request.add_header(TOKEN_HEADER, token)
        if origin:
            request.add_header("Origin", origin)
        if body:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                text = response.read().decode("utf-8", "replace")
                return response.status, (text if raw else json.loads(text or "{}"))
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", "replace")
            try:
                return exc.code, json.loads(text or "{}")
            except ValueError:
                return exc.code, text


class TestTheDaemonRefusesWhatItShould(_DaemonCase):
    def test_no_token_is_refused(self):
        status, body = self._call("/api/daemon", token=None)
        self.assertEqual(status, 401)
        self.assertIn("token", body["error"])

    def test_a_wrong_token_is_refused(self):
        self.assertEqual(self._call("/api/daemon", token="guess")[0], 401)

    def test_a_foreign_origin_is_refused_before_the_body_is_read(self):
        status, body = self._call(
            "/api/control/start", {"goal": "mine now"}, origin="http://evil.example"
        )
        self.assertEqual(status, 403)
        self.assertEqual(self.started, [])

    def test_a_foreign_origin_is_refused_even_with_the_right_token(self):
        status, _ = self._call("/api/daemon", origin="http://evil.example")
        self.assertEqual(status, 403)

    def test_our_own_origin_is_accepted(self):
        origin = self.base
        self.assertEqual(self._call("/api/daemon", origin=origin)[0], 200)

    def test_an_unknown_control_verb_is_not_found(self):
        self.assertEqual(self._call("/api/control/rm_rf", {})[0], 404)

    def test_an_unknown_route_is_not_found(self):
        self.assertEqual(self._call("/api/nothing")[0], 404)
        self.assertEqual(self._call("/nothing", raw=True)[0], 404)

    def test_an_oversized_control_message_is_refused(self):
        status, body = self._call("/api/control/start", {"goal": "x" * (MAX_CONTROL_BYTES + 10)})
        self.assertEqual(status, 400)
        self.assertEqual(self.started, [])

    def test_a_garbled_control_body_is_refused_not_crashed(self):
        # A malformed JSON body must earn a 400 with a reason, never a stack trace up the
        # daemon's control stack.
        request = urllib.request.Request(
            self.base + "/api/control/start", data=b"{ definitely not json"
        )
        request.add_header(TOKEN_HEADER, "test-token")
        request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                self.fail("expected a 400, got %d" % response.status)
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)
            body = json.loads(exc.read().decode("utf-8", "replace"))
            self.assertIn("could not read the request", body["error"])
        self.assertEqual(self.started, [])


class TestTheDaemonServesWhatItShould(_DaemonCase):
    def test_the_reads_serve_py_defines_still_work(self):
        for route in ("/api/board", "/api/activity", "/api/goals", "/api/deliveries",
                      "/api/team"):
            self.assertEqual(self._call(route)[0], 200, route)

    def test_it_reports_itself(self):
        status, body = self._call("/api/daemon")
        self.assertEqual(status, 200)
        self.assertEqual(body["project_root"], self.root)
        self.assertEqual(body["open_jobs"], 0)

    def test_the_pages_carry_this_launch_s_token(self):
        for route in ("/", "/office", "/design"):
            status, page = self._call(route, raw=True)
            self.assertEqual(status, 200, route)
            self.assertIn('content="test-token"', page)
            self.assertNotIn("__ORCHESTRATOR_TOKEN__", page)

    def test_the_cockpit_page_depends_on_nothing_remote(self):
        """Same rule the office and the design surface hold: it must work offline.

        This once asserted the page carried no `<script src` at all, which was the right
        rule read through the wrong proxy: what must be true is that nothing is fetched
        across the network, and a library served from `/vendor/` by the daemon itself is
        not. So the assertion is now the property rather than its former shorthand -
        every script the page loads must be a same-origin path under `/vendor/`.
        """
        _, page = self._call("/", raw=True)
        for source in re.findall(r'<script[^>]+src="([^"]+)"', page):
            self.assertTrue(
                source.startswith("/vendor/"),
                "the cockpit loaded %r, which is not a vendored same-origin path" % source,
            )
        self.assertNotIn("https://cdn", page)
        self.assertNotIn("http://cdn", page)

    def test_starting_a_goal_starts_it(self):
        status, body = self._call("/api/control/start", {"goal": "add a health endpoint"})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        deadline = time.time() + 5
        while time.time() < deadline and not self.started:
            time.sleep(0.02)
        self.assertEqual(self.started, ["add a health endpoint"])

    def test_an_empty_goal_is_refused_with_a_reason(self):
        status, body = self._call("/api/control/start", {"goal": "  "})
        self.assertEqual(status, 400)
        self.assertIn("goal was expected", body["error"])

    def test_jobs_are_listed(self):
        self._call("/api/control/start", {"goal": "one"})
        status, body = self._call("/api/jobs")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["jobs"]), 1)
        self.assertEqual(body["jobs"][0]["goal"], "one")

    def test_the_team_editor_still_writes_through_the_daemon(self):
        status, body = self._call(
            "/api/team",
            {
                "agents": [
                    {"agent": "opencode", "model": "opencode/gpt-5.1-codex",
                     "role": "implementer"},
                    {"agent": "claude", "model": "sonnet", "role": "verifier"},
                    {"agent": "claude", "model": "opus", "role": "verifier"},
                ],
                "consensus": "majority",
                "max_repair_attempts": 3,
            },
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        with open(self.config_file, encoding="utf-8") as handle:
            written = handle.read()
        self.assertIn("opus", written)
        self.assertIn("consensus: majority", written)

    def test_an_invalid_team_is_refused(self):
        status, body = self._call("/api/team", {"agents": [], "consensus": "unanimous"})
        self.assertEqual(status, 400)
        self.assertTrue(body["problems"])
        with open(self.config_file, encoding="utf-8") as handle:
            self.assertIn("agents:", handle.read())

    def test_the_stream_replays_and_then_follows(self):
        request = urllib.request.Request(self.base + "/api/stream?since=-1")
        request.add_header(TOKEN_HEADER, "test-token")
        with urllib.request.urlopen(request, timeout=10) as response:
            frames = []
            deadline = time.time() + 3
            self.store.record_agent_started("claude", "verifier", "sonnet")
            while time.time() < deadline and len(frames) < 2:
                line = response.readline().decode("utf-8", "replace").strip()
                if line.startswith("event:"):
                    frames.append(line.split(":", 1)[1].strip())
        self.assertEqual(frames[0], "hello")
        self.assertIn("orchestrator", frames)

    def test_the_stream_needs_the_token_too(self):
        request = urllib.request.Request(self.base + "/api/stream")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(caught.exception.code, 401)

    def test_the_stream_accepts_the_token_in_the_query(self):
        # EventSource cannot set a header, which is the whole reason this fallback exists.
        request = urllib.request.Request(self.base + "/api/stream?token=test-token")
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertEqual(response.status, 200)

    def test_the_handshake_is_written_while_it_runs(self):
        payload = read_handshake(self.root)
        self.assertEqual(payload["token"], "test-token")
        self.assertEqual(payload["port"], self.server.server_address[1])


# ===========================================================================
# Phase 9 - the cockpit projection
# ===========================================================================


class TestAgentStates(unittest.TestCase):
    """Derived from the two events Phase 4 added, and from nothing else."""

    def test_nothing_started_is_nothing_derived(self):
        self.assertEqual(agent_states([]), {})

    def test_started_is_working(self):
        states = agent_states([_started("claude", "verifier", 1)])
        self.assertEqual(states["claude/verifier"]["state"], AGENT_WORKING)

    def test_a_pass_is_done(self):
        states = agent_states([
            _started("claude", "verifier", 1),
            _result("claude", "verifier", 2, verdict="PASS", tokens=1200),
        ])
        entry = states["claude/verifier"]
        self.assertEqual(entry["state"], AGENT_DONE)
        self.assertEqual(entry["verdict"], "PASS")
        self.assertEqual(entry["tokens"], 1200)

    def test_a_fail_is_failed(self):
        states = agent_states([
            _started("claude", "verifier", 1),
            _result("claude", "verifier", 2, verdict="FAIL"),
        ])
        self.assertEqual(states["claude/verifier"]["state"], AGENT_FAILED)

    def test_an_error_is_failed_even_without_a_verdict(self):
        # Only a verifier ever carries a verdict; a researcher, planner or implementer that
        # fails does so with `status: "error"` and nothing else - this is the exact shape a
        # live run produced when antigravity returned empty output, and the case an earlier
        # version of this projection (checking a nonexistent "error" key) drew as done.
        states = agent_states([
            _result("antigravity", "researcher", 1, status="error", output="empty output")
        ])
        self.assertEqual(states["antigravity/researcher"]["state"], AGENT_FAILED)
        self.assertEqual(states["antigravity/researcher"]["error"], "empty output")

    def test_a_researcher_error_is_never_mistaken_for_done(self):
        # Regression: agent_states used to check result.get("error"), a key AgentResult never
        # has (types.py only ever sets "status"). A hard failure with no verdict field at all
        # was therefore indistinguishable from success.
        states = agent_states([
            _started("antigravity", "researcher", 1),
            _result("antigravity", "researcher", 2, status="error",
                    output="antigravity returned empty output for researcher"),
        ])
        entry = states["antigravity/researcher"]
        self.assertEqual(entry["state"], AGENT_FAILED)
        self.assertNotEqual(entry["state"], AGENT_DONE)

    def test_a_researcher_that_returns_no_verdict_is_done_not_failed(self):
        # Only a verifier returns a verdict; finishing is all a researcher claims.
        states = agent_states([
            _started("antigravity", "researcher", 1),
            _result("antigravity", "researcher", 2),
        ])
        self.assertEqual(states["antigravity/researcher"]["state"], AGENT_DONE)

    def test_a_repair_attempt_restarts_the_agent(self):
        states = agent_states([
            _started("claude", "verifier", 1),
            _result("claude", "verifier", 2, verdict="FAIL"),
            _started("claude", "verifier", 3),
        ])
        self.assertEqual(states["claude/verifier"]["state"], AGENT_WORKING)

    def test_tokens_accumulate_across_attempts(self):
        states = agent_states([
            _started("claude", "verifier", 1),
            _result("claude", "verifier", 2, verdict="FAIL", tokens=100),
            _started("claude", "verifier", 3),
            _result("claude", "verifier", 4, verdict="PASS", tokens=250),
        ])
        self.assertEqual(states["claude/verifier"]["tokens"], 350)


class TestColumns(unittest.TestCase):
    """One column per configured role, in the order the pipeline runs them."""

    def setUp(self):
        self.team = team_from_config(_cfg())

    def test_a_column_per_role_in_pipeline_order(self):
        result = columns(self.team)
        self.assertEqual([c["role"] for c in result], ["implementer", "verifier"])
        self.assertEqual([c["index"] for c in result], [0, 1])

    def test_a_team_at_rest_is_all_idle(self):
        result = columns(self.team)
        self.assertTrue(all(c["state"] == AGENT_IDLE for c in result))
        self.assertTrue(all(a["state"] == AGENT_IDLE for c in result for a in c["agents"]))

    def test_an_ensemble_is_several_cards_in_one_column(self):
        config = _cfg(agents=list(PIPELINE) + [
            {"agent": "claude", "model": "opus", "role": "verifier"},
        ])
        result = columns(team_from_config(config))
        self.assertEqual(len(result), 2)
        self.assertEqual(len(result[1]["agents"]), 2)
        self.assertTrue(result[1]["parallel"])

    def test_a_ladder_is_labelled_as_one(self):
        config = _cfg(agents=[
            {"agent": "claude", "model": ["sonnet", "opus"], "role": "implementer"},
            {"agent": "claude", "model": "sonnet", "role": "verifier"},
        ])
        card = columns(team_from_config(config))[0]["agents"][0]
        self.assertTrue(card["ladder"])
        self.assertEqual(card["model_label"], "sonnet -> opus")

    def test_live_states_land_on_the_right_cards(self):
        events = [
            _started("opencode", "implementer", 1),
            _result("opencode", "implementer", 2),
            _started("claude", "verifier", 3),
        ]
        result = columns(self.team, events)
        self.assertEqual(result[0]["state"], AGENT_DONE)
        self.assertEqual(result[1]["state"], AGENT_WORKING)

    def test_a_terminal_id_reaches_the_card_it_belongs_to(self):
        result = columns(self.team, [], {"claude/verifier": "term-1"})
        self.assertIsNone(result[0]["agents"][0]["terminal_id"])
        self.assertEqual(result[1]["agents"][0]["terminal_id"], "term-1")

    def test_a_card_key_distinguishes_an_ensemble(self):
        self.assertNotEqual(
            agent_key("claude", "verifier", 0), agent_key("claude", "verifier", 1)
        )


class TestColumnState(unittest.TestCase):
    """A phase's state is `graph.py`'s idea of one, not a new one."""

    def test_anything_working_makes_the_column_working(self):
        self.assertEqual(
            column_state([{"state": AGENT_DONE}, {"state": AGENT_WORKING}]), AGENT_WORKING
        )

    def test_a_survived_partial_failure_reads_as_done_not_failed(self):
        # Regression: verified against a real run. An ensemble of two researchers where one
        # errored and one succeeded is exactly `make_sync_node`'s "errored and succeeded"
        # branch - it logs a partial failure and continues on the survivor. The pipeline
        # genuinely went on to planner, implementer and verifier and finished PASS; a column
        # reading FAILED here would have told a person watching that the run had stopped
        # while it was still correctly running.
        self.assertEqual(
            column_state([{"state": AGENT_DONE}, {"state": AGENT_FAILED}]), AGENT_DONE
        )

    def test_every_member_failing_is_the_columns_failure(self):
        # The actual fatal condition in `make_sync_node`: "group and not succeeded".
        self.assertEqual(
            column_state([{"state": AGENT_FAILED}, {"state": AGENT_FAILED}]), AGENT_FAILED
        )

    def test_all_done_is_done(self):
        self.assertEqual(column_state([{"state": AGENT_DONE}] * 2), AGENT_DONE)

    def test_partly_done_is_still_working(self):
        self.assertEqual(
            column_state([{"state": AGENT_DONE}, {"state": AGENT_IDLE}]), AGENT_WORKING
        )

    def test_partly_failed_with_one_not_yet_started_is_still_working(self):
        # Whether the phase survives cannot be judged - the way make_sync_node judges it, on
        # every member at once - until every member has actually finished.
        self.assertEqual(
            column_state([{"state": AGENT_FAILED}, {"state": AGENT_IDLE}]), AGENT_WORKING
        )

    def test_an_empty_column_is_idle(self):
        self.assertEqual(column_state([]), AGENT_IDLE)


class TestWhatTheBoardLendsTheCockpit(unittest.TestCase):
    def test_needs_you_and_ready_come_from_the_board_untouched(self):
        board = {"columns": {"Needs You": [{"id": "a"}], "Ready to Merge": [{"id": "b"}]}}
        self.assertEqual(needs_you(board), [{"id": "a"}])
        self.assertEqual(ready_to_merge(board), [{"id": "b"}])

    def test_no_board_is_no_cards(self):
        self.assertEqual(needs_you(None), [])
        self.assertEqual(ready_to_merge({}), [])


class TestTheCockpitRead(_DaemonCase):
    """The page makes one read; a dashboard assembled from five polls shows five moments."""

    def test_one_read_carries_everything_the_page_draws(self):
        status, body = self._call("/api/cockpit")
        self.assertEqual(status, 200)
        for key in ("team", "catalog", "templates", "problems", "columns", "activity",
                    "jobs", "needs_you", "ready", "counts", "approvals", "project_root"):
            self.assertIn(key, body)

    def test_the_columns_are_the_configured_roles(self):
        _, body = self._call("/api/cockpit")
        self.assertEqual([c["role"] for c in body["columns"]], ["implementer", "verifier"])

    def test_a_pending_gate_is_offered_with_an_id_to_answer_it_by(self):
        approval = request_approval(self.root, GATE_BEFORE_MERGE, subject="the work")
        _, body = self._call("/api/cockpit")
        self.assertEqual([a["id"] for a in body["approvals"]], [approval["id"]])
        self.assertEqual(body["approvals"][0]["status"], STATUS_PENDING)

    def test_a_live_agent_shows_as_working(self):
        self.store.record_agent_started("claude", "verifier", "sonnet")
        _, body = self._call("/api/cockpit")
        verifier = [c for c in body["columns"] if c["role"] == "verifier"][0]
        self.assertEqual(verifier["state"], AGENT_WORKING)


# ===========================================================================
# Phase 10 - the live terminal
# ===========================================================================


class TestTerminalStream(unittest.TestCase):
    """Transcript, bounded, sequenced - and honest when it drops something."""

    def test_output_comes_back_in_order(self):
        stream = TerminalStream("t1", agent="claude", role="verifier")
        stream.append("one\n")
        stream.append("two\n")
        payload = stream.read_since(0)
        self.assertEqual(payload["text"], "one\ntwo\n")
        self.assertEqual(payload["next"], 2)
        self.assertFalse(payload["truncated"])

    def test_a_reader_can_ask_for_only_what_is_new(self):
        stream = TerminalStream("t1")
        stream.append("one\n")
        first = stream.read_since(0)
        stream.append("two\n")
        self.assertEqual(stream.read_since(first["next"])["text"], "two\n")

    def test_asking_again_from_the_end_returns_nothing(self):
        stream = TerminalStream("t1")
        stream.append("one\n")
        payload = stream.read_since(0)
        self.assertEqual(stream.read_since(payload["next"])["text"], "")

    def test_an_empty_append_changes_nothing(self):
        stream = TerminalStream("t1")
        self.assertEqual(stream.append(""), 0)

    def test_it_is_bounded_and_says_when_it_dropped_something(self):
        stream = TerminalStream("t1", max_chars=1024)
        for index in range(400):
            stream.append("x" * 40 + "\n")
        payload = stream.read_since(0)
        self.assertLessEqual(len(payload["text"]), 1024 + 41)
        self.assertTrue(payload["truncated"])

    def test_closing_records_how_it_ended(self):
        stream = TerminalStream("t1")
        self.assertEqual(stream.state, TERMINAL_RUNNING)
        stream.close(0)
        self.assertEqual(stream.state, TERMINAL_EXITED)
        self.assertEqual(stream.read_since(0)["exit_code"], 0)
        stream.close(9)  # idempotent: a second close does not rewrite history
        self.assertEqual(stream.exit_code, 0)


class TestTerminalRegistry(unittest.TestCase):
    def test_a_terminal_can_be_found_by_id(self):
        registry = TerminalRegistry()
        stream = registry.open(agent="claude", role="verifier")
        self.assertIs(registry.get(stream.id), stream)
        self.assertIsNone(registry.get("nope"))
        self.assertIsNone(registry.read_since("nope", 0))

    def test_the_newest_terminal_for_an_agent_wins(self):
        registry = TerminalRegistry()
        registry.open(agent="claude", role="verifier")
        second = registry.open(agent="claude", role="verifier")
        self.assertEqual(registry.live_by_agent()["claude/verifier"], second.id)

    def test_it_is_bounded_and_drops_finished_terminals_first(self):
        registry = TerminalRegistry(max_terminals=2)
        first = registry.open(agent="a", role="r")
        first.close(0)
        second = registry.open(agent="b", role="r")
        third = registry.open(agent="c", role="r")
        self.assertIsNone(registry.get(first.id))
        self.assertIsNotNone(registry.get(second.id))
        self.assertIsNotNone(registry.get(third.id))

    def test_listing_is_newest_first(self):
        registry = TerminalRegistry()
        registry.open(agent="a", role="r")
        registry.open(agent="b", role="r")
        self.assertEqual([t["agent"] for t in registry.listing()], ["b", "a"])

    def test_closing_all_ends_every_live_terminal(self):
        registry = TerminalRegistry()
        stream = registry.open(agent="a", role="r")
        registry.close_all()
        self.assertEqual(stream.state, TERMINAL_EXITED)


class TestRunCaptured(unittest.TestCase):
    """A real subprocess: the same result as before, and live output as well."""

    def test_it_returns_what_subprocess_run_would(self):
        result = run_captured(
            [sys.executable, "-c", "print('hello'); import sys; sys.stderr.write('warned')"],
            timeout=30,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("hello", result.stdout)
        self.assertIn("warned", result.stderr)
        self.assertGreaterEqual(result.duration_seconds, 0)

    def test_a_failure_is_reported_not_raised(self):
        result = run_captured([sys.executable, "-c", "raise SystemExit(3)"], timeout=30)
        self.assertEqual(result.returncode, 3)

    def test_output_reaches_the_sink_as_it_arrives(self):
        seen = []
        result = run_captured(
            [sys.executable, "-c",
             "import time\nfor i in range(3):\n    print(i, flush=True)\n    time.sleep(.02)"],
            timeout=30,
            sink=seen.append,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("0", "".join(seen))
        self.assertIn("2", "".join(seen))

    def test_a_sink_that_throws_does_not_stop_the_agent(self):
        def hostile(_text):
            raise RuntimeError("the panel went away")

        result = run_captured([sys.executable, "-c", "print('still ran')"],
                              timeout=30, sink=hostile)
        self.assertEqual(result.returncode, 0)
        self.assertIn("still ran", result.stdout)

    def test_a_timeout_raises_as_it_always_did(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            run_captured([sys.executable, "-c", "import time; time.sleep(10)"], timeout=1)

    def test_whether_a_pty_is_available_is_stated_not_assumed(self):
        # Windows needs the optional `pywinpty` dependency (ConPTY); POSIX ships one in the
        # standard library. Either way this asks, rather than assumes either answer.
        self.assertIsInstance(pty_available(), bool)
        if os.name == "nt":
            try:
                import winpty  # noqa: F401
                expected = True
            except Exception:
                expected = False
            self.assertEqual(pty_available(), expected)


class TestTheLauncherHook(unittest.TestCase):
    """One hook, in one place: no agent adapter and nothing in the graph changes."""

    def setUp(self):
        from orchestrator import launcher

        self.launcher = launcher
        self.previous = launcher.set_output_recorder(None)

    def tearDown(self):
        self.launcher.set_output_recorder(self.previous)

    def _run(self, code="print('from the agent')"):
        return self.launcher.run_agent_cli(
            cmd=[sys.executable, "-c", code],
            timeout=30, visible=False, agent="claude", role="verifier", model="sonnet",
        )

    def test_with_nothing_installed_the_headless_path_is_unchanged(self):
        self.assertIsNone(self.launcher.get_output_recorder())
        result = self._run()
        self.assertEqual(result.returncode, 0)
        self.assertIn("from the agent", result.stdout)

    def test_an_installed_recorder_sees_the_output_and_who_produced_it(self):
        registry = TerminalRegistry()
        seen = {}

        def recorder(agent=None, role=None, model=None, cmd=None, cwd=None):
            seen.update({"agent": agent, "role": role, "model": model})
            stream = registry.open(agent=agent, role=role, model=model, command=cmd, cwd=cwd)
            sink = lambda text: stream.append(text)
            sink.stream = stream
            return sink

        self.launcher.set_output_recorder(recorder)
        result = self._run()

        self.assertEqual(result.returncode, 0)
        self.assertIn("from the agent", result.stdout)   # the pipeline sees no difference
        self.assertEqual(seen, {"agent": "claude", "role": "verifier", "model": "sonnet"})
        terminal = registry.listing()[0]
        self.assertEqual(terminal["state"], TERMINAL_EXITED)
        self.assertEqual(terminal["exit_code"], 0)
        self.assertIn("from the agent", registry.read_since(terminal["id"], 0)["text"])

    def test_a_broken_recorder_does_not_break_the_run(self):
        def hostile(**_kwargs):
            raise RuntimeError("no")

        self.launcher.set_output_recorder(hostile)
        result = self._run()
        self.assertEqual(result.returncode, 0)
        self.assertIn("from the agent", result.stdout)

    def test_a_captured_failure_still_reports_its_code(self):
        registry = TerminalRegistry()

        def recorder(agent=None, role=None, model=None, cmd=None, cwd=None):
            stream = registry.open(agent=agent, role=role, command=cmd)
            sink = lambda text: stream.append(text)
            sink.stream = stream
            return sink

        self.launcher.set_output_recorder(recorder)
        result = self._run("raise SystemExit(2)")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(registry.listing()[0]["exit_code"], 2)


class TestRunCapturedPty(unittest.TestCase):
    """A real pseudo-terminal, exercised for real - a mock could not prove liveness or that
    a child process genuinely believes it is talking to a terminal rather than a pipe.
    """

    def setUp(self):
        from orchestrator.terminals import pty_available

        if not pty_available():
            self.skipTest("no pty implementation available on this machine")

    def test_it_returns_what_run_captured_would(self):
        from orchestrator.terminals import run_captured_pty

        result = run_captured_pty(["cmd", "/c", "echo hello"] if os.name == "nt"
                                  else [sys.executable, "-c", "print('hello')"], timeout=15)
        self.assertEqual(result.returncode, 0)
        self.assertIn("hello", result.stdout)
        self.assertEqual(result.stderr, "")  # a pty has one stream, not two

    def test_a_nonzero_exit_is_reported(self):
        from orchestrator.terminals import run_captured_pty

        result = run_captured_pty(["cmd", "/c", "exit 3"] if os.name == "nt"
                                  else [sys.executable, "-c", "raise SystemExit(3)"], timeout=15)
        self.assertEqual(result.returncode, 3)

    def test_output_reaches_the_sink_live(self):
        from orchestrator.terminals import run_captured_pty

        seen = []
        run_captured_pty(
            ["cmd", "/c", "echo one && echo two"] if os.name == "nt"
            else [sys.executable, "-c", "print('one'); print('two')"],
            timeout=15, sink=seen.append,
        )
        joined = "".join(seen)
        self.assertIn("one", joined)
        self.assertIn("two", joined)

    def test_the_timeout_is_bounded_by_the_deadline_not_by_read_granularity(self):
        # Regression: an earlier version only checked the deadline between blocking reads,
        # so a child that goes silent for longer than the timeout (produces nothing to
        # read) could overshoot it by an entire silent period before the check ever ran
        # again. Reading now happens on a background thread; the deadline is enforced by
        # how long the *main* thread waits, not by how long one read call takes.
        from orchestrator.terminals import run_captured_pty

        slow_cmd = (
            ["cmd", "/c", "timeout /t 5 >nul & echo done"] if os.name == "nt"
            else [sys.executable, "-c", "import time; time.sleep(5); print('done')"]
        )
        started = time.time()
        with self.assertRaises(subprocess.TimeoutExpired):
            run_captured_pty(slow_cmd, timeout=1)
        elapsed = time.time() - started
        self.assertLess(elapsed, 2.5, "timeout overshot by more than one read cycle")


class TestVisibleRoutesThroughCaptureWhenRecorded(unittest.TestCase):
    """The daemon's captured/relayed output replaces a separate OS window, not adds to it."""

    def setUp(self):
        from orchestrator import launcher

        self.launcher = launcher
        self.previous = launcher.set_output_recorder(None)

    def tearDown(self):
        self.launcher.set_output_recorder(self.previous)

    def test_visible_with_no_recorder_is_unaffected(self):
        # Nothing about a plain CLI invocation (no daemon, no recorder) should change: this
        # only asserts the *routing decision* is untouched, not the window itself, since a
        # real window is not something a unit test should pop open.
        self.assertIsNone(self.launcher.get_output_recorder())

    def test_a_recorder_makes_visible_use_the_captured_path(self):
        seen = []

        def recorder(agent=None, role=None, model=None, cmd=None, cwd=None):
            return seen.append

        self.launcher.set_output_recorder(recorder)
        result = self.launcher.run_agent_cli(
            cmd=[sys.executable, "-c", "print('captured, not windowed')"],
            timeout=15, visible=True, agent="claude", role="verifier", model="sonnet",
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("captured, not windowed", result.stdout)
        # The sink actually received output - proof the captured branch ran, not the
        # separate-window branch (which never calls a recorder at all).
        self.assertTrue(seen)

    def test_a_recorder_still_reports_failures_correctly(self):
        from orchestrator.agents.exceptions import CLITimeoutError

        def recorder(agent=None, role=None, model=None, cmd=None, cwd=None):
            return lambda text: None

        self.launcher.set_output_recorder(recorder)
        with self.assertRaises(CLITimeoutError):
            self.launcher.run_agent_cli(
                cmd=[sys.executable, "-c", "import time; time.sleep(5)"],
                timeout=1, visible=True, agent="claude", role="verifier",
            )


class TestOpencodeNativeTuiWithoutBridge(unittest.TestCase):
    """When a recorder is watching, opencode's native TUI needs no Antigravity IDE bridge -
    it shows its own attach process through the daemon's captured pty instead.
    """

    @patch("orchestrator.agents.opencode_tui.subprocess.Popen")
    @patch("orchestrator.agents.opencode_tui.requests.get")
    @patch("orchestrator.agents.opencode_tui.requests.post")
    @patch("orchestrator.terminals.run_captured")
    @patch("orchestrator.terminals.pty_available", return_value=False)
    def test_no_bridge_functions_are_even_consulted(
        self, _mock_pty, mock_run_captured, mock_post, mock_get, mock_popen
    ):
        # The strongest version of "doesn't need the bridge": the bridge check functions are
        # never patched at all here, so if the code path still reached them (unmocked, real
        # network calls against a bridge that is not running) this test would hang or fail -
        # it passing at all is the proof they were never called.
        #
        # `run_captured`/`pty_available` are patched on `orchestrator.terminals`, not on
        # `opencode_tui` - the attach thread imports them locally at call time, straight from
        # their real module, so a patch on the wrong module would silently miss and this
        # test would try to spawn a real (nonexistent) `opencode.cmd attach` process.
        from orchestrator.agents.opencode_tui import run_opencode_native_tui
        from orchestrator.terminals import CapturedResult

        def fake_run_captured(cmd, cwd=None, timeout=180, sink=None, env=None):
            if sink is not None:
                sink("(a real interactive opencode TUI would render here)\n")
            return CapturedResult(returncode=0, stdout="", stderr="", duration_seconds=0.1)

        mock_run_captured.side_effect = fake_run_captured

        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        def fake_get(url, *args, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            if "/global/health" in url:
                resp.json.return_value = {"healthy": True}
            elif "/session/status" in url:
                resp.json.return_value = {}
            elif "/message" in url:
                resp.json.return_value = [
                    {"info": {"role": "assistant"},
                     "parts": [{"type": "text", "text": "done via pty"}]}
                ]
            elif "/session/ses-1" in url:
                resp.json.return_value = {"tokens": {"input": 10, "output": 5}}
            return resp

        def fake_post(url, *args, **kwargs):
            resp = MagicMock()
            resp.status_code = 200 if url.endswith("/session") else 204
            resp.json.return_value = {"id": "ses-1"}
            return resp

        mock_get.side_effect = fake_get
        mock_post.side_effect = fake_post

        seen = []
        output, tokens = run_opencode_native_tui(
            prompt="do the thing",
            project_root=".",
            pause_on_completion=0.0,
            sink=seen.append,
        )
        self.assertEqual(output, "done via pty")
        self.assertTrue(tokens["available"])

    def test_with_no_sink_and_no_bridge_it_still_fails_fast(self):
        # Regression: a refactor briefly moved this check past the point where the server
        # process is spawned, so a bridge-offline failure started paying for a subprocess
        # it was never going to use. Asserting no Popen call proves it fails *before* that.
        from orchestrator.agents.opencode_tui import run_opencode_native_tui

        with patch("orchestrator.agents.opencode_tui.subprocess.Popen") as mock_popen, \
             patch("orchestrator.agents.opencode_tui.check_antigravity_bridge",
                   return_value=False):
            with self.assertRaises(CLIExecutionError) as ctx:
                run_opencode_native_tui(prompt="x", project_root=".")
            self.assertIn("Antigravity integrated terminal bridge", str(ctx.exception))
            mock_popen.assert_not_called()


class TestTerminalRoutes(_DaemonCase):
    def test_terminals_are_listed_and_readable(self):
        self.daemon.capture_agent_output(True)
        self.daemon.terminals.open(agent="claude", role="verifier").append("hello\n")

        status, body = self._call("/api/terminals")
        self.assertEqual(status, 200)
        self.assertEqual(body["terminals"][0]["agent"], "claude")

        terminal_id = body["terminals"][0]["id"]
        status, payload = self._call("/api/terminal?id=%s&since=0" % terminal_id)
        self.assertEqual(status, 200)
        self.assertEqual(payload["text"], "hello\n")

    def test_an_unknown_terminal_is_not_found(self):
        self.assertEqual(self._call("/api/terminal?id=nope")[0], 404)

    def test_a_terminal_needs_the_token_like_everything_else(self):
        self.assertEqual(self._call("/api/terminals", token=None)[0], 401)


# ===========================================================================
# Phase 11 - the desktop shell
# ===========================================================================


class TestTheDesktopShell(unittest.TestCase):
    """What the shell is allowed to be. Read from the files, not from intent."""

    def setUp(self):
        self.desktop = REPO_ROOT / "desktop"
        self.main = (self.desktop / "main.js").read_text(encoding="utf-8")
        self.preload = (self.desktop / "preload.js").read_text(encoding="utf-8")

    def test_it_exists_and_is_declared(self):
        manifest = json.loads((self.desktop / "package.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["main"], "main.js")
        self.assertIn("electron", manifest["devDependencies"])

    def test_it_starts_the_daemon_rather_than_the_engine(self):
        self.assertIn('"--daemon"', self.main)
        self.assertIn('"--no-browser"', self.main)
        self.assertIn('"--project-root"', self.main)

    def test_it_waits_for_the_daemons_own_handshake(self):
        # Not a fixed sleep, and not a guess from a log line.
        self.assertIn("daemon.json", self.main)
        self.assertIn("Number(payload.port) === Number(port)", self.main)

    def test_it_gives_the_page_no_privileged_bridge(self):
        self.assertIn("contextIsolation: true", self.main)
        self.assertIn("nodeIntegration: false", self.main)
        self.assertIn("sandbox: true", self.main)
        for forbidden in ("ipcRenderer", 'require("fs")', 'require("child_process")'):
            self.assertNotIn(forbidden, self.preload)

    def test_it_stops_the_daemon_it_started(self):
        self.assertIn("before-quit", self.main)
        self.assertIn("child.kill()", self.main)

    def test_the_shell_never_reaches_into_the_engine(self):
        # If this directory were deleted, `--daemon` would lose a window frame and nothing else.
        self.assertNotIn("orchestrator/graph", self.main)
        self.assertNotIn("orchestrator/scheduler", self.main)
        self.assertNotIn("events.jsonl", self.main)

    def test_a_port_reservation_failure_is_a_dialog_not_a_crash(self):
        # Regression: `freePort()` can reject (no loopback port available), and that happened
        # before openProject's own try block - so a harmless condition crashed the Electron
        # main process with an unhandled promise rejection. The port has to be reserved
        # through a helper that turns the failure into the same dialog the daemon-start
        # failure already shows.
        self.assertIn("async function reservePort", self.main)
        self.assertIn("return await freePort();", self.main)
        self.assertIn("dialog.showMessageBox(null", self.main)
        self.assertIn("Could not reserve a port for", self.main)
        self.assertNotIn("const port = await freePort();", self.main)


if __name__ == "__main__":
    unittest.main()
