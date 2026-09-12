# Demo: Research → Planning → Implementation → Verification → Acceptance

A throwaway, two-file Python project with one deliberately failing test, used to exercise the
whole Synagon pipeline against something real rather than a mocked one. Nothing here is
required to use Synagon — it exists to give a new user (or reviewer) something they can run
themselves in under two minutes, with a project small enough to read the whole diff of.

## Set up and run

```powershell
python demo/setup_demo_project.py          # creates demo/scratch (git-ignored)
python -m orchestrator --project-root demo/scratch "Add a multiply(a, b) function to calc.py so the existing tests in test_calc.py pass. Do not modify test_calc.py."
```

`demo/scratch` is a real, isolated git repository — it is not part of Synagon's own history,
and deleting it and re-running the setup script is always safe. **Before your first run against
it**, see the README's Troubleshooting section and establish workspace trust with whichever CLI
you configured as implementer — OpenCode and Claude Code both deny every file operation on a
directory neither has touched before, and a fully headless run has no terminal to answer that
first-run trust prompt. This is not a Synagon defect; it is worth knowing because it looks
exactly like a broken pipeline otherwise.

## What one run demonstrates

A single goal against this project exercises the acceptance gate honestly, because the test
suite fails until `multiply` exists:

- **Research → Planning → Implementation → Verification.** The researcher reads both files and
  reports the `ImportError`; the planner turns that into one specific instruction; the
  implementer edits `calc.py`; the verifier runs the tests and returns PASS or FAIL.
- **Acceptance overrules a verifier's PASS.** `verification.acceptance.command: pytest -q` is run
  by the orchestrator itself, in the run's own worktree, before the verifier's verdict is
  trusted. A verifier that says PASS over a still-red suite is overruled — this is invariant 2 in
  ARCHITECTURE.md, not a special case for this demo.
- **Repair, on a FAIL.** If the acceptance gate or the verifier fails, the implementer gets a
  second and third attempt (`max_repair_attempts`), each fed the verifier's own findings.
- **Retry, on a flaky or empty response.** Any single execution — not just the implementer's —
  is retried up to three times with a widening backoff if the agent returns nothing or raises.
- **Honest token accounting.** Every agent call's usage is summed in the final table; a failed
  attempt's tokens are never lost, and an attempt with no reported usage is shown as
  `unavailable` rather than guessed at.
- **Isolation.** The implementer never touches this checkout — it writes to its own git
  worktree, on its own `orchestrator/run/<id>` branch, reviewable with `git diff` and mergeable
  (or not) entirely at your own discretion.

## Demonstrating the rest

These are one flag or one extra invocation away from the same project, and are not run by
default because they either cost more (an escalation, a full delegated goal) or need a
deliberate interruption (resume):

| To see | Run |
| --- | --- |
| **Escalation across providers** | Add `agent: [antigravity, claude]` / `model: [gemini-3.8-flash-high, sonnet]` to the researcher role in `demo/scratch/orchestrator.yaml`, then rerun. |
| **Budget limits** | Add `--max-total-tokens 5000` — the run stops instead of continuing past the ceiling. |
| **Resume after interruption** | Start the run, kill the process (Ctrl+C or a hard kill) mid-implementation, then `python -m orchestrator --project-root demo/scratch --resume <run_id>` (the id is printed at the start of every run). Completed phases are replayed from the event log, not re-paid for. |
| **Delegation into several tasks** | `--delegate` instead of a bare task string turns the same goal into a task graph first (`--plan-only` to inspect it without running anything). |
| **Process cleanup** | `python -m orchestrator --project-root demo/scratch --prune-runs --dry-run` (branches and worktrees) and `--close-sessions` (any kept native-TUI sessions). |
| **The cockpit, live** | `python -m orchestrator --project-root demo/scratch --daemon`, then type the same goal into the dashboard instead of a terminal. |

Several real, measured runs of this exact demo — including the acceptance gate correctly
holding the line over a still-red suite, two repair attempts, retries with backoff, and an
honest OpenCode failure that cost real tokens and produced nothing — are recorded in
`CHANGELOG.md` under Package F, with the real token and timing numbers.
