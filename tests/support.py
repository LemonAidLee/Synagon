"""Shared helpers that keep the suite out of the project it is testing.

A test that invokes the real graph with `project_root=os.getcwd()` and the run store enabled
writes a real run record into the developer's own `.orchestrator/runs` — every time the suite
runs. That is not a cosmetic problem. The run store is the dataset `--stats` analyses, the
board projects, and (since Roadmap 8.2) the planner's memory reads, so a suite that appends to
it is a suite that quietly rewrites what the orchestrator believes about this repository. It is
also unbounded: 141 of one checkout's 157 records turned out to be suite artifacts.

`redirected_run_store` is the fix, and `assert_run_store_untouched` is the guard that stops it
coming back.
"""

import json
import os
import tempfile
from typing import Any, Dict, Optional

from orchestrator.store import runs_root


def redirected_run_store(config: Dict[str, Any], directory: str) -> Dict[str, Any]:
    """Return a copy of `config` whose run store writes to `directory`.

    The store stays *enabled* on purpose. Several tests assert the `run_store_opened` and
    `run_store_closed` events in an exact sequence, so disabling it would trade one wrong
    behaviour for a weaker test. `runs_root` honours an absolute directory, so pointing it at
    a temporary one keeps every event and writes none of it into the project.
    """
    redirected = dict(config)
    store = dict(redirected.get("run_store") or {})
    store["enabled"] = True
    store["directory"] = str(directory)
    redirected["run_store"] = store
    return redirected


def isolated_graph_state(
    config: Dict[str, Any],
    task: str,
    directory: str,
    **overrides: Any,
) -> Dict[str, Any]:
    """Build an initial graph state that cannot write into the project under test.

    `project_root` stays the real checkout, because the context collection these tests
    exercise is *about* a real project; only the store is redirected.
    """
    state: Dict[str, Any] = {
        "task": task,
        "project_root": os.getcwd(),
        "config": redirected_run_store(config, directory),
        "workspace": {"isolated": False, "path": os.getcwd(), "reason": "test"},
    }
    state.update(overrides)
    return state


def run_store_fingerprint(project_root: Optional[str] = None) -> int:
    """How many runs the *real* project's store holds right now."""
    root = runs_root(project_root or os.getcwd())
    if not root.is_dir():
        return 0
    return sum(1 for entry in root.iterdir() if entry.is_dir())


class RunStoreGuard:
    """Assert that a block of test code wrote nothing into the real run store.

    Used as a context manager by the regression test that exists solely to catch this class
    of mistake being reintroduced:

        with RunStoreGuard(self):
            build_graph(config).invoke(state)
    """

    def __init__(self, case: Any, project_root: Optional[str] = None) -> None:
        self.case = case
        self.project_root = project_root or os.getcwd()
        self.before = 0

    def __enter__(self) -> "RunStoreGuard":
        self.before = run_store_fingerprint(self.project_root)
        return self

    def __exit__(self, *exc_info: Any) -> None:
        if exc_info[0] is not None:
            return
        after = run_store_fingerprint(self.project_root)
        self.case.assertEqual(
            after,
            self.before,
            "this test wrote %d run(s) into the real project's run store. Redirect it with "
            "tests.support.redirected_run_store: the store is what --stats, the board and the "
            "planner's memory all read." % (after - self.before),
        )


class FakeProcess:
    """What `subprocess.Popen` returns, for a test that patches it to stand in for an agent CLI.

    The launcher starts headless agents with `Popen` (so a timeout can stop the whole process
    tree), so that is the seam an adapter test mocks. `times_out=True` makes every wait time
    out, which the launcher turns into `CLITimeoutError` once its deadline passes.
    """

    pid = None  # never a real PID: a tree stop on a fake process must not reach taskkill

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "", times_out: bool = False):
        self.returncode = None if times_out else returncode
        self._stdout, self._stderr, self._times_out = stdout, stderr, times_out

    def communicate(self, timeout: Optional[float] = None):
        import subprocess

        if self._times_out:
            raise subprocess.TimeoutExpired(cmd="agent", timeout=timeout)
        return self._stdout, self._stderr

    def poll(self):
        return self.returncode

    def kill(self):
        self.returncode = -9

    def wait(self, timeout: Optional[float] = None):
        return self.returncode


def temporary_store_dir() -> str:
    """A throwaway directory for a redirected run store."""
    return tempfile.mkdtemp(prefix="orch_teststore_")


def read_runs(directory: str) -> list:
    """Every run record written into a redirected store, for a test that wants to inspect it."""
    root = runs_root(os.getcwd(), directory)
    if not root.is_dir():
        return []
    records = []
    for entry in sorted(root.iterdir()):
        meta = entry / "run.json"
        if meta.is_file():
            try:
                records.append(json.loads(meta.read_text(encoding="utf-8")))
            except Exception:
                pass
    return records
