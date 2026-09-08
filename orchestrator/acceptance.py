"""The objective acceptance gate (Roadmap Phase 0).

Every verdict this orchestrator produced before this module was an LLM's opinion about whether
the tests pass. The verifier was told to run them, and its report was taken at its word — which
means the system's central claim, ``VERDICT: PASS``, rested on exactly the kind of unverified
assertion invariant 2 exists to reject.

This module closes that gap. The orchestrator runs a command **itself**, in the session's
worktree, and records the exit code as a fact. An agent cannot narrate its way past a non-zero
exit code.

Where it runs
-------------
Immediately **before** the verifier, so that:

* the verifier receives real failure output instead of its own paraphrase of a test run, and
* the repair prompt after a failure carries the actual error, which is the single most useful
  thing a repair attempt can be given.

Load-bearing rules
------------------
* **The command comes from the user's checkout, never from the worktree.** Configuration is
  loaded from the project root before any agent runs; an agent that edits `orchestrator.yaml`
  inside its worktree cannot change the command that judges it. This is the property that makes
  the gate trustworthy rather than decorative.
* **No shell.** The command is an argv list, run with ``shell=False`` and a detached stdin, like
  every other subprocess in this project. A string is tokenized, never handed to a shell.
* **A gate that cannot run is not a pass.** A missing binary, a timeout, or a crash records
  ``ok: False`` with the reason. Failing open would make the gate worse than useless, because it
  would look like evidence.
* **It reports; it does not decide.** This module returns a fact. Whether a red gate overrides a
  verifier's PASS is `verification.acceptance.required`, applied in `status.derive_verdict` —
  the one place verdicts are decided.
"""

import os
import shlex
import subprocess
import time
from typing import Any, Dict, List, Optional, Sequence, TypedDict, Union

#: Captured output is trimmed to this many characters from each stream before it is recorded
#: or shown to an agent. Test suites can emit megabytes; the tail is where the failure is.
DEFAULT_OUTPUT_LIMIT = 4000


class AcceptanceCheck(TypedDict, total=False):
    """The durable record of one gate execution."""
    command: List[str]      # argv actually executed
    ok: bool                # True only when the command ran and exited 0
    exit_code: Optional[int]
    duration_seconds: float
    output: str             # trimmed combined output, tail-weighted
    error: Optional[str]    # why the gate could not run, when it could not
    working_dir: Optional[str]
    repair_attempts: int    # the repair generation this check belongs to
    skipped: bool           # no command configured


def parse_command(command: Union[str, Sequence[str], None]) -> List[str]:
    """Normalize a configured command into an argv list.

    A list is taken as-is. A string is tokenized with `shlex`, using Windows rules on Windows so
    that ``C:\\path\\to\\python -m pytest`` survives; POSIX rules elsewhere. Returns an empty
    list when there is nothing to run.
    """
    if command is None:
        return []
    if isinstance(command, (list, tuple)):
        return [str(part) for part in command if str(part).strip()]

    text = str(command).strip()
    if not text:
        return []

    if os.name == "nt":
        # posix=False keeps backslashes intact, but leaves quote characters on the tokens.
        tokens = shlex.split(text, posix=False)
        cleaned: List[str] = []
        for token in tokens:
            if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
                token = token[1:-1]
            cleaned.append(token)
        return [t for t in cleaned if t]
    return shlex.split(text)


def _trim(text: str, limit: int) -> str:
    """Trim output to `limit` characters, keeping the tail where failures live."""
    if not text or limit <= 0 or len(text) <= limit:
        return text or ""
    omitted = len(text) - limit
    return f"[... {omitted:,} characters omitted ...]\n{text[-limit:]}"


def skipped_check(reason: str = "no acceptance command configured") -> AcceptanceCheck:
    """Return the record of a gate that was not configured."""
    return {
        "command": [],
        "ok": True,
        "exit_code": None,
        "duration_seconds": 0.0,
        "output": "",
        "error": reason,
        "skipped": True,
        "repair_attempts": 0,
    }


def run_acceptance(
    command: Union[str, Sequence[str], None],
    working_dir: str,
    timeout_seconds: int = 600,
    output_limit: int = DEFAULT_OUTPUT_LIMIT,
    repair_attempts: int = 0,
    env: Optional[Dict[str, str]] = None,
) -> AcceptanceCheck:
    """Run the acceptance command and record what happened. Never raises.

    Args:
        command: The configured command, as a string or argv list.
        working_dir: Directory to run in — the session's worktree.
        timeout_seconds: Seconds allowed before the command is killed.
        output_limit: Characters of combined output to keep.
        repair_attempts: The repair generation this check belongs to.
        env: Optional environment overrides.

    Returns:
        An AcceptanceCheck. ``ok`` is True only when the command ran to completion and exited 0.
    """
    argv = parse_command(command)
    if not argv:
        record = skipped_check()
        record["repair_attempts"] = int(repair_attempts)
        record["working_dir"] = working_dir
        return record

    started = time.time()
    record: AcceptanceCheck = {
        "command": argv,
        "ok": False,
        "exit_code": None,
        "duration_seconds": 0.0,
        "output": "",
        "error": None,
        "working_dir": working_dir,
        "repair_attempts": int(repair_attempts),
        "skipped": False,
    }

    merged_env = None
    if env:
        merged_env = dict(os.environ)
        merged_env.update(env)

    try:
        completed = subprocess.run(
            argv,
            cwd=working_dir,
            shell=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=max(1, int(timeout_seconds)),
            env=merged_env,
        )
    except FileNotFoundError:
        record["error"] = (
            f"the acceptance command '{argv[0]}' was not found on PATH. "
            "A gate that cannot run is not a pass."
        )
        record["duration_seconds"] = round(time.time() - started, 2)
        return record
    except subprocess.TimeoutExpired:
        record["error"] = f"the acceptance command exceeded its {timeout_seconds}s timeout"
        record["duration_seconds"] = round(time.time() - started, 2)
        return record
    except Exception as exc:  # pragma: no cover - platform dependent
        record["error"] = f"the acceptance command could not be run: {exc}"
        record["duration_seconds"] = round(time.time() - started, 2)
        return record

    stdout = (completed.stdout or b"").decode("utf-8", errors="replace")
    stderr = (completed.stderr or b"").decode("utf-8", errors="replace")
    combined = stdout if not stderr else f"{stdout}\n{stderr}" if stdout else stderr

    record["exit_code"] = completed.returncode
    record["ok"] = completed.returncode == 0
    record["output"] = _trim(combined.strip(), output_limit)
    record["duration_seconds"] = round(time.time() - started, 2)
    return record


def latest_check(
    checks: Optional[List[Dict[str, Any]]],
    repair_attempts: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """Return the most recent gate result, optionally for one repair generation.

    Pinning to a generation matters in the repair loop: the check that judges this attempt is
    the one made after this many repairs, not the one that failed two attempts ago.
    """
    if not checks:
        return None
    if repair_attempts is None:
        return checks[-1]
    matching = [c for c in checks if int(c.get("repair_attempts") or 0) == int(repair_attempts)]
    return matching[-1] if matching else None


def gate_failed(
    checks: Optional[List[Dict[str, Any]]],
    repair_attempts: Optional[int] = None,
) -> bool:
    """Return True when the relevant gate result exists, was not skipped, and is red."""
    check = latest_check(checks, repair_attempts)
    if not check or check.get("skipped"):
        return False
    return not check.get("ok")


def describe_check(check: Optional[Dict[str, Any]]) -> str:
    """Render a one-line summary of a gate result."""
    if not check:
        return "Acceptance gate: not run."
    if check.get("skipped"):
        return "Acceptance gate: not configured."
    command = " ".join(check.get("command") or []) or "(none)"
    if check.get("error"):
        return f"Acceptance gate: COULD NOT RUN - {check['error']} ({command})"
    verdict = "PASSED" if check.get("ok") else "FAILED"
    return (
        f"Acceptance gate: {verdict} - `{command}` exited "
        f"{check.get('exit_code')} in {check.get('duration_seconds', 0)}s"
    )


def format_for_prompt(check: Optional[Dict[str, Any]]) -> str:
    """Render a gate result as evidence for a verifier or repair prompt.

    Deliberately blunt about what the result means, because the whole point is that this is the
    one piece of the prompt an agent must not talk itself out of.
    """
    if not check or check.get("skipped"):
        return ""

    command = " ".join(check.get("command") or [])
    lines = ["### OBJECTIVE ACCEPTANCE CHECK (run by the orchestrator, not by an agent):"]
    lines.append(f"Command: {command}")

    if check.get("error"):
        lines.append(f"Result:  COULD NOT RUN - {check['error']}")
        lines.append(
            "Treat this as a failure of the check, not as evidence that the implementation works."
        )
    elif check.get("ok"):
        lines.append(f"Result:  PASSED (exit code 0, {check.get('duration_seconds', 0)}s)")
        lines.append(
            "This is evidence, not proof of completeness: a green suite does not mean the task "
            "was done. Judge the task on its own terms."
        )
    else:
        lines.append(f"Result:  FAILED (exit code {check.get('exit_code')})")
        lines.append(
            "This command was run by the orchestrator in the same workspace you are inspecting. "
            "A non-zero exit code cannot be explained away: if it is still failing, the verdict "
            "is FAIL."
        )

    output = (check.get("output") or "").strip()
    if output:
        lines.append("")
        lines.append("Output:")
        lines.append(output)

    return "\n".join(lines) + "\n\n"
