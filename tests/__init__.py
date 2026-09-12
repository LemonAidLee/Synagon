"""Tests package for CLI agent orchestration.

The offline suite must never start a real agent CLI. Until Package B that held only because
each adapter test happened to mock the exact function the launcher used to start a process:
when the launcher moved from `subprocess.run` to `subprocess.Popen` (so a timeout could stop
the agent's whole process tree), those mocks stopped intercepting and a suite run started the
real `claude`, `agy` and `opencode` binaries with the tests' prompts. Nothing was modified, but
real tokens were spent and one OpenCode run had to be killed by hand.

So the guarantee is now structural rather than incidental: while this package is imported, any
attempt to start a process whose executable is one of the agent CLIs is refused, loudly. A test
that means to exercise a real CLI opts in the way the live tests already do (RUN_LIVE_TESTS=1).
"""

import os
import subprocess

AGENT_BINARIES = frozenset({"claude", "agy", "opencode"})


def _executable_name(args) -> str:
    first = args[0] if isinstance(args, (list, tuple)) and args else args
    try:
        return os.path.splitext(os.path.basename(os.fspath(first)))[0].lower()
    except TypeError:
        return ""


if os.environ.get("RUN_LIVE_TESTS") != "1" and not getattr(subprocess.Popen, "_agent_guard", False):
    _real_init = subprocess.Popen.__init__

    def _guarded_init(self, args, *rest, **kwargs):
        if _executable_name(args) in AGENT_BINARIES:
            raise RuntimeError(
                f"the offline test suite tried to start a real agent CLI ({args[0] if isinstance(args, (list, tuple)) else args}). "
                "Mock the launcher seam (tests.support.FakeProcess), or set RUN_LIVE_TESTS=1 for a deliberate live test."
            )
        _real_init(self, args, *rest, **kwargs)

    subprocess.Popen.__init__ = _guarded_init
    subprocess.Popen._agent_guard = True

    # pywinpty creates its ConPTY child natively, not through Popen, so the guard above never
    # sees it - and `run_captured_pty` is exactly how the daemon starts OpenCode's `attach` TUI.
    # Package C closes that second door the same way (tests/test_package_c.py pins it).
    try:
        import winpty as _winpty
    except Exception:  # pragma: no cover - optional dependency
        _winpty = None
    if _winpty is not None and not getattr(_winpty.PtyProcess, "_agent_guard", False):
        _real_spawn = _winpty.PtyProcess.spawn.__func__

        def _guarded_spawn(cls, argv, *rest, **kwargs):
            if _executable_name(argv if isinstance(argv, (list, tuple)) else str(argv).split()) in AGENT_BINARIES:
                raise RuntimeError(
                    f"the offline test suite tried to start a real agent CLI on a pty ({argv}). "
                    "Set RUN_LIVE_TESTS=1 for a deliberate live test."
                )
            return _real_spawn(cls, argv, *rest, **kwargs)

        _winpty.PtyProcess.spawn = classmethod(_guarded_spawn)
        _winpty.PtyProcess._agent_guard = True
