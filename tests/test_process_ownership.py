"""Package C - process ownership: a hard-killed orchestrator leaves no agent process behind.

Package B measured the defect: with the orchestrator killed outright during the implementer,
`opencode.exe` kept running, able to write into the worktree a resumed run would use. A killed
process runs no cleanup of its own, so the fix is a Windows job object with kill-on-close
(`orchestrator.process_jobs`), which the *kernel* closes when the orchestrator dies.

These tests use real processes - Python stand-ins for an agent CLI that start a child of their
own, never a real agent - and a real hard kill (`TerminateProcess`, which is what
`Popen.kill()` is on Windows). Each ownership test has a control beside it that runs the same
scenario without ownership and shows the survivor, so a test that passes is one that could
have failed.
"""

import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from orchestrator.process_jobs import (
    IS_WINDOWS,
    finish_owned,
    job_of,
    process_alive,
    release_owned,
    spawn_owned,
    stop_owned,
    watch_owner,
)

PY = sys.executable
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: A stand-in agent CLI: it starts a child of its own (a tool, a test run, a language server),
#: writes both PIDs to the file it is given, and works for two minutes.
AGENT = textwrap.dedent(
    """
    import os, subprocess, sys, time
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    with open(sys.argv[1] + ".tmp", "w") as fh:
        fh.write("%d %d" % (os.getpid(), child.pid))
    os.replace(sys.argv[1] + ".tmp", sys.argv[1])
    time.sleep(120)
    """
)

#: An agent that starts a background child and exits at once. The child's parent is gone, so a
#: parent-PID walk (`taskkill /T` from the agent's PID) can no longer find it.
DETACHING_AGENT = textwrap.dedent(
    """
    import os, subprocess, sys
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    with open(sys.argv[1] + ".tmp", "w") as fh:
        fh.write("%d %d" % (os.getpid(), child.pid))
    os.replace(sys.argv[1] + ".tmp", sys.argv[1])
    """
)


def _read_pids(path, seconds=20.0):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if os.path.isfile(path):
            with open(path) as fh:
                return [int(p) for p in fh.read().split()]
        time.sleep(0.05)
    raise AssertionError("the stand-in agent never started")


def _gone(pids, seconds=10.0):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if not any(process_alive(p) for p in pids):
            return True
        time.sleep(0.1)
    return False


def _cleanup(pids):
    """End processes this test itself started, by exact PID - never by name."""
    for pid in pids:
        if process_alive(pid):
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, shell=False)


def _orchestrator(body):
    """A stand-in orchestrator process running `body` against the real modules."""
    code = "import sys\nsys.path.insert(0, %r)\n" % ROOT + textwrap.dedent(body)
    return subprocess.Popen(
        [PY, "-c", code], cwd=ROOT, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )


@unittest.skipUnless(IS_WINDOWS, "job objects are the Windows mechanism")
class TestAHardKilledOrchestratorLeavesNoAgentBehind(unittest.TestCase):
    """The P0: kill the orchestrator outright while an agent (and its child) is running."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="orch_owner_")
        self.pidfile = os.path.join(self.dir, "pids")
        self.survivors = []
        self.addCleanup(lambda: _cleanup(self.survivors))

    def _kill_during(self, body):
        orch = _orchestrator(body % {"agent": AGENT, "pidfile": self.pidfile, "cwd": self.dir})
        self.addCleanup(lambda: orch.poll() is None and orch.kill())
        try:
            pids = _read_pids(self.pidfile)
        except AssertionError:
            orch.kill()
            self.fail("stand-in orchestrator failed: %s" % orch.communicate()[1])
        self.survivors.extend(pids)
        self.assertTrue(all(process_alive(p) for p in pids), "the agent should be running")
        orch.kill()  # TerminateProcess: no finally, no atexit, no signal handler runs
        orch.wait(timeout=10)
        return pids

    def test_the_headless_path(self):
        pids = self._kill_during(
            """
            from orchestrator.launcher import run_bounded
            run_bounded([sys.executable, "-c", %(agent)r, %(pidfile)r], cwd=%(cwd)r, timeout=300)
            """
        )
        self.assertTrue(_gone(pids), "an agent or its child outlived the killed orchestrator")

    def test_the_daemon_captured_path(self):
        pids = self._kill_during(
            """
            from orchestrator.terminals import run_captured
            run_captured([sys.executable, "-c", %(agent)r, %(pidfile)r], cwd=%(cwd)r, timeout=300)
            """
        )
        self.assertTrue(_gone(pids))

    def test_the_acceptance_gate(self):
        pids = self._kill_during(
            """
            from orchestrator.acceptance import run_acceptance
            run_acceptance([sys.executable, "-c", %(agent)r, %(pidfile)r], %(cwd)r, timeout_seconds=300)
            """
        )
        self.assertTrue(_gone(pids))

    def test_control_without_ownership_the_agent_survives(self):
        """The defect Package B measured, reproduced with a plain Popen: this is what the fix
        changes, so a passing ownership test above is one that could have failed."""
        pids = self._kill_during(
            """
            import subprocess
            subprocess.Popen([sys.executable, "-c", %(agent)r, %(pidfile)r], cwd=%(cwd)r).wait()
            """
        )
        self.assertFalse(_gone(pids, seconds=2.0), "without a job the child should survive")


@unittest.skipUnless(IS_WINDOWS, "job objects are the Windows mechanism")
class TestOwnershipIsExactlyWhatWasStarted(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="orch_owner_")
        self.pidfile = os.path.join(self.dir, "pids")
        self.started = []
        self.addCleanup(lambda: _cleanup(self.started))

    def test_a_suspended_start_runs_normally(self):
        process = spawn_owned(
            [PY, "-c", "print('ran')"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        out, _ = process.communicate(timeout=20)
        self.assertEqual(out.strip(), "ran")
        self.assertEqual(process.returncode, 0)
        finish_owned(process)

    def test_the_process_and_its_children_are_in_its_job(self):
        process = spawn_owned([PY, "-c", AGENT, self.pidfile])
        agent, child = _read_pids(self.pidfile)
        self.started.extend([agent, child])
        # `sys.executable` may be a launcher that starts the real interpreter as its own child
        # (a venv's python.exe does), so the process started here and the agent that wrote its
        # PID can differ - which is the point: all of them are in the job.
        members = set(job_of(process).pids())
        self.assertLessEqual({process.pid, agent, child}, members)
        self.assertTrue(stop_owned(process))
        self.assertTrue(_gone([agent, child]))
        finish_owned(process)

    def test_a_child_whose_parent_already_exited_is_still_stopped(self):
        """What `taskkill /T` could not reach: the agent is gone, its child is not."""
        from orchestrator.launcher import stop_tree

        process = spawn_owned([PY, "-c", DETACHING_AGENT, self.pidfile])
        agent, child = _read_pids(self.pidfile)
        self.started.append(child)
        process.wait(timeout=20)
        self.assertTrue(process_alive(child))
        stop_tree(process)
        self.assertTrue(_gone([child]))

    def test_an_unrelated_process_is_never_touched(self):
        bystander = subprocess.Popen([PY, "-c", "import time; time.sleep(120)"])
        self.started.append(bystander.pid)
        process = spawn_owned([PY, "-c", AGENT, self.pidfile])
        agent, child = _read_pids(self.pidfile)
        self.started.extend([agent, child])
        stop_owned(process)
        finish_owned(process)
        self.assertTrue(_gone([agent, child]))
        self.assertTrue(process_alive(bystander.pid), "a process nobody owned was stopped")
        bystander.kill()

    def test_a_released_process_outlives_its_owner(self):
        """A retained native TUI session is kept on purpose; releasing it is what allows that."""
        orch = _orchestrator(
            """
            import time
            from orchestrator.process_jobs import release_owned, spawn_owned
            p = spawn_owned([sys.executable, "-c", %r, %r])
            while not __import__("os").path.isfile(%r):
                time.sleep(0.05)
            release_owned(p)
            time.sleep(120)
            """ % (AGENT, self.pidfile, self.pidfile)
        )
        self.addCleanup(lambda: orch.poll() is None and orch.kill())
        pids = _read_pids(self.pidfile)
        self.started.extend(pids)
        time.sleep(0.5)  # let the release land before the owner dies
        orch.kill()
        orch.wait(timeout=10)
        self.assertFalse(_gone(pids, seconds=2.0), "a released process should survive")

    def test_a_test_fake_is_not_mistaken_for_a_real_process(self):
        fake = MagicMock()
        with patch("subprocess.Popen", return_value=fake):
            returned = spawn_owned(["whatever"])
        self.assertIs(returned, fake)
        self.assertIsNone(job_of(returned))
        self.assertFalse(release_owned(returned))


@unittest.skipUnless(IS_WINDOWS, "the owner watch is the Windows mechanism")
class TestARunnerTheOrchestratorCannotOwnWatchesIt(unittest.TestCase):
    """A visible runner hosted by Windows Terminal or the IDE is not our child, so no job of
    ours can hold it; it watches the orchestrator's PID and stops its agent when that is gone."""

    def test_the_watch_fires_when_the_owner_exits(self):
        owner = subprocess.Popen([PY, "-c", "import time; time.sleep(120)"])
        fired = threading.Event()
        self.assertIsNotNone(watch_owner(owner.pid, fired.set))
        self.assertFalse(fired.wait(0.3))
        owner.kill()
        self.assertTrue(fired.wait(10))

    def test_an_owner_that_cannot_be_opened_is_not_watched(self):
        self.assertIsNone(watch_owner(None, lambda: None))
        self.assertIsNone(watch_owner(0, lambda: None))

    def test_the_runner_stops_its_agent_when_the_orchestrator_dies(self):
        import json

        workdir = tempfile.mkdtemp(prefix="orch_runner_")
        pidfile = os.path.join(workdir, "pids")
        owner = subprocess.Popen([PY, "-c", "import time; time.sleep(120)"])
        job = {
            "title": "t", "cmd": [PY, "-c", AGENT, pidfile], "cwd": workdir,
            "status_file": os.path.join(workdir, "status.json"), "timeout": 300,
            "pause_seconds": 0, "owner_pid": owner.pid,
        }
        job_file = os.path.join(workdir, "job.json")
        with open(job_file, "w") as fh:
            json.dump(job, fh)
        env = dict(os.environ, PYTHONPATH=ROOT)
        runner = subprocess.Popen(
            [PY, "-m", "orchestrator.launcher", "--job-file", job_file],
            cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.addCleanup(lambda: runner.poll() is None and runner.kill())
        pids = _read_pids(pidfile)
        self.addCleanup(lambda: _cleanup(pids))
        owner.kill()
        self.assertTrue(_gone(pids, seconds=15), "the runner's agent outlived the orchestrator")
        runner.wait(timeout=15)
        with open(job["status_file"]) as fh:
            self.assertIn("orchestrator that started this agent exited", json.load(fh)["stderr"])


if __name__ == "__main__":
    unittest.main()
