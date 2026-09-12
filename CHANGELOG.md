# Changelog

## Unreleased

### Package F — release readiness

A cleanup and documentation pass over everything accumulated in Packages B–E, plus a real
demonstration project. No architecture changed; every fix below is something a release
checklist should have caught.

- **A hardcoded personal path in shipped source.** `tools/install_terminal_bridge.py` fell back
  to a literal `C:\Users\Asus\...` if `%LOCALAPPDATA%` didn't resolve. Removed; the fallback is
  now `shutil.which`, same as every other optional-tool lookup in this project.
- **A stale API-key template that contradicted this project's own architecture.** `.env.example`
  listed `ANTHROPIC_API_KEY` / `GOOGLE_API_KEY` / `OPENAI_API_KEY` — nothing in `orchestrator/`
  reads any of them; every agent is a local, already-authenticated CLI, by design (README,
  invariant "Zero provider API keys"). Removed, along with `langchain-anthropic`,
  `langchain-google-genai`, `langchain-openai` and `python-dotenv` from `requirements.txt`,
  which existed only to support that unused path. `langgraph` itself stays — `graph.py` builds
  its `StateGraph` from it.
- **A local design-linter's cache directory (`.impeccable/`) was untracked but not ignored.**
  Added to `.gitignore`; it holds this session's own tool state, not project source.
- **Considered, then deliberately left alone: the built-in default's researcher pairing.** With
  no `orchestrator.yaml` of your own, `DEFAULT_CONFIG` assigns the researcher role to
  `antigravity / gemini-3.8-flash-high` — the exact pairing this project's own config moved away
  from after measuring it return an empty response to an investigate-the-project prompt (see
  "the researcher moved off Antigravity", below). Changing the fallback to match looked like a
  one-line fix; it broke 15 tests across four files that pin the fallback's exact shape. Fixing
  it well is a real (if small) design change — deciding what the fallback *should* demonstrate,
  and updating every test that encodes it — not a release-readiness cleanup, so it was reverted
  and documented instead: README, Known limitations, names the risk and the two ways around it
  (write your own config, or ladder the researcher across providers).
- **README gained the sections a release needs and did not have:** Prerequisites, a terminal
  bridge install/reload walkthrough, an Execution modes table (headless / visible-headless /
  native TUI, and exactly which agents support which), Troubleshooting, and Known limitations —
  including a config-loading behavior worth knowing before you write your own `orchestrator.yaml`
  (it fully replaces the built-in default rather than merging with it).
- **`demo/`** — a reproducible, two-file throwaway project (`setup_demo_project.py` creates it;
  never committed, `demo/scratch/` is git-ignored) with one deliberately failing test, small
  enough to read the whole diff of, built to exercise Research → Planning → Implementation →
  Verification → Acceptance end to end. `demo/README.md` covers reproducing it and the one flag
  or extra invocation needed to see escalation, budget limits, resume, delegation, and process
  cleanup on the same project.
- **Live validation, and what it actually found.** Eight real runs against the demo project (real
  Claude Code, OpenCode, and Antigravity CLIs; no mocks) surfaced a genuine first-run gotcha
  neither README nor ARCHITECTURE.md had named: **OpenCode and Claude Code each deny every file
  operation on a project directory neither has been used against before**, and a fully headless
  run has no terminal to answer that first-run trust prompt — indistinguishable from a broken
  pipeline until you know to look for it. Now documented in README (Troubleshooting) and in
  `demo/README.md`. Working within that constraint, the same eight runs are honest evidence for
  exactly the behaviors this release needed to demonstrate, with real token and timing numbers:
  - **Retry with widening backoff** on an empty OpenCode reply, more than once (2.0s, then 4.0s).
  - **Repair, on a genuine FAIL.** The acceptance gate (`pytest -q`, run by the orchestrator
    itself) failed, the verifier agreed, and two repair attempts followed — 33.3s and 8.0s of
    real implementer work — before the run correctly reported `Repair attempts: 2/2` and gave up
    rather than loop forever.
  - **Honest token accounting under failure.** A run whose implementer failed three attempts in a
    row still reported every attempt's real cost (up to 162,861 tokens on one failed attempt
    alone) rather than showing zero or guessing.
  - **`BLOCKED`, not a false `PASS`, when no agent can proceed.** Once Claude Code's own
    workspace-trust denial made every implementer attempt a no-op, the verifier correctly
    reported `BLOCKED` with the specific human action needed, and spent zero of its repair budget
    retrying something no amount of retrying could fix — invariants 2 and 7 holding under a real
    failure, not a scripted one.
  - **Process cleanup.** A run that changed nothing removed its own worktree and deleted the
    branch it had created (`Cleanup: Worktree removed and its branch deleted: the run changed
    nothing`); a run with real (if incomplete) changes committed them to the run's own branch and
    reported exactly what to do to inspect or discard them.
  - **Not reached live this pass:** a full green PASS end-to-end (blocked by the trust gotcha
    above, compounded by an unrelated live outage on one OpenCode model, `opencode/gpt-5.1-codex`,
    and by output flakiness on another, `opencode/big-pickle`, neither of which is a Synagon
    defect), a cross-provider escalation ladder, and `--resume` — each is one documented flag or
    invocation away on the same demo project (`demo/README.md`), just not spent here after eight
    real runs had already answered the question this pass needed answered: does the reliability
    machinery work under a real failure. It does.
- **Offline suite: 1205 tests, unchanged pass rate.** Run twice around the changes above (before
  and after), both `OK`, confirming none of this pass's edits touched tested behavior.

### Package D — Synagon identity, and final polish

The product identity moves from the working name "Project Beta" to **Synagon**. This is a
branding and packaging pass, not an architecture change — every invariant and every module from
Packages A–C is unchanged.

- **The project folder rename is prepared but not yet applied.** The plan is
  `D:\Progmata\Project Beta` → `D:\Progmata\Synagon`, and the whole tracked tree was searched
  first for the old name and for hardcoded absolute paths: every entry point resolves
  `project_root` dynamically (`os.getcwd()` / `--project-root` in `__main__.py`, `workspace.py`),
  so nothing in the engine depends on the old path. The only hit was the terminal bridge's own
  publisher id (below); a mocked path string in a test was left as `D:\Progmata\Project Beta`,
  matching the folder as it exists today. The rename itself is deferred: attempting it live found
  the folder held open by the running Antigravity IDE session (a file-watch handle Windows treats
  as in-use), and forcing that closed was out of scope for this change. Whenever the folder *is*
  renamed, pre-existing run records under `.orchestrator/runs/` will keep the old absolute path as
  a historical fact (invariant 1: facts are stored, never rewritten) — resuming one of those runs
  will degrade exactly the way `reattach_run_worktree` already handles a worktree that no longer
  exists at its recorded path: it recreates the worktree from the branch instead of failing.
- **The daemon's homepage (`cockpit.html`) carries the new identity.** `<title>`, the header
  wordmark, and the boot-splash sequence now read SYNAGON, including a hand-built ASCII wordmark
  (7-row dot-matrix, verified line-length-equal before embedding) in the same full-screen boot
  overlay that used to just type "ORCHESTRATOR". The overlay's font-size is now
  `clamp(7px, 2.3vw, 19px)` for the logo specifically, so it scales down instead of overflowing
  at narrow window widths, and the logo is `aria-hidden` with the overlay itself carrying
  `role="status" aria-label="Synagon daemon starting"` for anyone not seeing the art.
- **The desktop shell and the terminal bridge extension follow.** `desktop/package.json`'s
  `productName` and `appId`, and the window title in `main.js`, now say Synagon; the Antigravity
  terminal bridge's VSIX publisher id moves from `project-beta` to `synagon` (its own extension
  namespace, not a third-party identifier), and the committed `.vsix` was rebuilt from the
  updated manifest.
- **What did not change, on purpose.** The Python package (`orchestrator/`), the CLI invocation
  (`python -m orchestrator`), `orchestrator.yaml`, and every internal module name stay exactly as
  they are — renaming them would ripple through every import and test for no user-facing benefit.
  Provider names (Antigravity, Claude Code, OpenCode) are untouched, per their own identities.

### Package C — process ownership, honest spend across resume, configuration that takes effect

Production hardening on top of Package B. Every fix was reproduced first - by execution, or by
following the code path end to end - and each has a regression test in
`tests/test_process_ownership.py` or `tests/test_package_c.py`. Evidence levels are those of
ARCHITECTURE.md §30.2c; nothing below is called live that was mocked.

- **A hard-killed orchestrator no longer leaves its agents running.** Every agent process -
  headless, the daemon's captured and pty paths, the visible console runner, the OpenCode
  native-TUI server, and the acceptance gate's command - is created suspended, placed in its own
  Windows job object with kill-on-close, then resumed (`orchestrator/process_jobs.py`). The
  kernel ends the job's processes when the orchestrator dies, however it dies. A job holds only
  what was started into it; nothing is found by name. A retained native session is released so
  it outlives the run on purpose; a runner hosted by Windows Terminal or the IDE bridge watches
  the orchestrator's PID and stops its agent when it is gone. LIVE: the orchestrator killed with
  `TerminateProcess` during the OpenCode implementer, twice - every process of the run gone
  within 8 s (Package B measured `opencode.exe` surviving).
- **An execution killed with the process is an attempt with unknown spend, not nothing.**
  `resume.orphaned_attempts` also finds executions that started and never ended; the execution
  that replaces one carries it as an `in_flight` attempt with unavailable usage, so the total is
  shown as a floor (`Known tokens`, `N earlier tries`, `Budget: at least …`) - never estimated,
  counted once across any number of resumes.
- **The wall-clock budget spans the whole run.** A resumed run carries `prior_elapsed_seconds`
  (its earlier sessions' working time, downtime excluded) and its time budget and reported
  duration continue from it. Budgets report `tokens_complete` and `unreported_executions`.
- **Reassigning a role in the daemon takes effect.** `POST /api/team` wrote the file, but the
  daemon and its handler kept their startup configuration: the cockpit re-read the old team and
  the next goal ran the old assignment until a restart. Both now re-read the file after a
  successful save. LIVE on the real daemon, including a kill and restart.
- **Agents can be added and removed.** The pipeline's `+ ADD AGENT` had no listener and the
  Team pane had no add/remove; both now edit the one team draft, saved through the one route.
- **An agent with no runner is never run as Claude.** `graph.get_runner` returned Claude for any
  unknown name; it now raises `UnknownAgentError`, a phase failure that is not retried, and the
  team editor names the missing runner (`config.RUNNABLE_AGENTS`).
- **Every team save keeps its own backup.** Found live: saves within one second shared a backup
  name, and the pre-edit file was overwritten.
- **Icon sidebar.** Explorer, Team, Board and Terminals are inline-SVG icon buttons with
  tooltips, `aria-label` and `aria-pressed`; the Board badge no longer rewrites its button.
- **The offline suite cannot start an agent on a pty either.** `tests/__init__.py` now also
  guards pywinpty's `PtyProcess.spawn`, the one process start that bypasses `Popen`.
- **Known-issue cleanup.** Ghost worktree directories are removed (empty and unregistered
  only); the file viewer's tokenizer compiles all its patterns once and all four are
  compile-tested; `archive.py` uses `RUN_META_FILENAME`; verifier independence computes each
  entry's rungs once.
- **Live bridge run.** A full workflow on the Antigravity integrated-terminal bridge: three
  runner tabs, OpenCode's native `attach` TUI in an IDE tab, `completed`/PASS, rows summing to
  335,859, nothing left running. The bridge's port-takeover fix is installed (files identical to
  source) and activates on the next IDE window reload - not done here, because it would have
  ended this session.

### Package B — reliability when things go wrong

A validation pass over failure and recovery: success, retry, escalation, verifier FAIL, repair,
budget, acceptance override, interruption, hard crash and resume, each driven through the real
CLI entry point against throwaway git repositories. Every fix below was reproduced by execution
first, and each has a regression test in `tests/test_reliability.py` that fails when the fix is
switched off.

- **A hard crash no longer loses a failed attempt's spend.** A failed attempt's usage lived only
  on its `agent_retry` event until the phase produced its one result, and resume read results
  alone: killed after a 700-token failed attempt, a run resumed to a total 700 lower than what
  was paid. `resume.orphaned_attempts` recovers such attempts, and the execution that replaces
  them carries them on its result (`interrupted: true`, shown as `(try N, before resume)`) —
  counted once, and never again on a second resume.
- **A crash mid-repair can no longer resume to a false `completed`.** Resume re-ran the
  acceptance gate over the half-repaired workspace and paired its green result with the
  *replayed* PASS of a verifier that had judged a red gate: status `completed`, repair neither
  recorded nor verified. Gate results are now restored from the log, and a gate is replayed
  exactly when the verification it fed is replayed (`resume.should_replay_gate`).
- **Failed CLI runs keep the usage they reported.** Measured on this machine, Claude Code
  2.1.267, agy 1.2.1 and OpenCode 1.18.29 all exit 1 on failure and still print their usage; the
  headless adapters discarded it. It now travels on `CLIExecutionError.token_usage`, with the
  CLI's own error message.
- **Headless timeouts are enforced and leave no orphans.** Only the agent's own process was
  killed: a grandchild holding its stdout made a 2-second timeout return after 25 s, and the
  daemon's captured path returned on time but left the grandchild running. Both paths (and the
  visible runner) now stop the whole process tree (`launcher.stop_tree`, `run_bounded`) on a
  timeout or an interruption: measured after the fix, 2.3 s and 2.2 s, no survivors.
- **Skill paths point into the worktree.** The skill manifest in every prompt named the user's
  checkout — the same out-of-worktree trip Stage 7.5.2.1 fixed for "Project Root". Committed
  skills are now shown at their worktree path; an untracked one is marked read-only.
- **An unreachable terminal bridge is a preflight error.** With `terminal_type:
  antigravity_integrated` and the bridge down, the first agent was launched and retried before
  the run failed. Preflight now refuses it (not under the daemon, and not for `auto`), and a
  bridge that answers but does not create the tab fails at once instead of after the timeout.
- **The bridge extension survives its owning window closing.** Found live: the window holding
  port 49182 closed, the other had given up on its only `EADDRINUSE`, and no window served the
  bridge again. It now retries, and a window that never owned the port no longer deletes the
  owner's port file. Needs a reinstall of the extension and an IDE reload to take effect.
- **The cockpit counts every spend, once.** Cards now include failed attempts and every rung of
  a ladder; the run total comes from the results (`cockpit.run_tokens`) instead of a sum of
  cards, which counted identical ensemble members once per card.
- **A run that ends in an error shows what it spent.** The CLI printed only "Pipeline Error".
- **The offline suite cannot start a real agent.** Moving the launcher to `Popen` made adapter
  tests that mocked `subprocess.run` start the real CLIs during one suite run (four `claude -p
  "Test prompt"` sessions, no tools, no files changed; an OpenCode run killed by hand). Those
  tests now mock `Popen`, and `tests/__init__.py` refuses to start `claude`, `agy` or `opencode`
  unless `RUN_LIVE_TESTS=1`.
- **Evidence.** Controlled (real CLI entry point, worktrees, gate, store, resume; scripted
  agents with an independent token ledger): 17 of 17 scenarios. Live (real CLIs, scratch repos):
  a normal run, a cross-model escalation from an unusable model, a gate-driven FAIL → repair →
  PASS, a hard kill during the implementer followed by `--resume`, and OpenCode in native TUI on
  a pty — each with summary rows summing exactly to the printed total, the checkout untouched,
  and no agent process left behind by the orchestrator. Not validated live: the Antigravity
  integrated-terminal bridge (down in this environment; cause identified above). See
  ARCHITECTURE.md §30.2b.

### Stage 7.5.2.1 — native TUI execution made correct, not just visible

A reliability pass over the Stage 7.5.2 native-TUI path. Every change below fixes a defect that
was reproduced by execution or measured on this machine (OpenCode 1.18.29), not one that was
merely plausible.

- **The "528-token discrepancy" was a report error, and is reconstructed from the logs.** The
  Stage 7.5.2 walkthrough quoted Researcher 11,816 and Planner 629 beside a total of 53,239. The
  run logs show two live runs: `task-2521` (11,816 / 629, then failed at the implementer) and
  `task-2553` (12,232 / 741 / 36,948 / 3,318, whose own table printed Total 53,239). The report
  mixed them: (12,232 − 11,816) + (741 − 629) = 528. The orchestrator summed correctly both times.
- **A failed attempt's tokens are no longer lost.** The retry/escalation loop overwrote each
  failed attempt's reported usage with the next attempt's, so after an escalation every total and
  every budget decision was lower than what was paid — and the Antigravity failure mode §9.0
  documents (`SUCCESS`, empty reply, a real bill for the thinking) is exactly that case. Failed
  attempts now travel on the one `AgentResult` as `attempt_token_usage`, attributed to the rung
  that spent them, shown as their own summary row, recorded on the `agent_retry` event, and
  counted exactly once by metrics, budget, `--stats` and diagnostics (`types.result_token_rows`).
- **One OpenCode token normalisation.** Headless and native mode read the same step-finish
  numbers but normalised them differently (cached input in or out of `input_tokens`; reasoning
  and cache-write tokens missing from native totals). `agents/opencode_usage.py` is now the only
  normaliser, and native mode reads the session's step-finish parts — the same events headless
  parses.
- **Native completion detection fixed.** OpenCode's `retry` status (provider backoff) ended the
  turn early; a turn that produced nothing was reported as a made-up success sentence, which
  bypassed the empty-output retry; an error OpenCode recorded on the turn was ignored; and a
  prompt naming an unusable model waited out the whole timeout. All four are fixed.
- **No more orphaned OpenCode servers.** The native controller started the server through npm's
  `opencode.CMD` shim, and terminating the shim left `opencode.exe` listening — one leaked server
  per native execution. It now starts the real binary and stops the whole process tree.
- **`agent_execution_mode` is deterministic and strict.** `native_tui` is refused at preflight for
  any agent without a supported native TUI (Claude Code, Antigravity), is never retried, and
  never falls back to headless; an unknown mode is an error instead of silently running headless.
- **Terminal lifecycle is configurable:** `execution.close_terminal_on_completion` (default
  `true`; this project's config sets `false`). When off, a finished native TUI session keeps its
  server and TUI so its history can be inspected, bounded to 8 and closed with
  `--close-sessions`; failures, timeouts and interruptions are always torn down; a kept server is
  only ever stopped after proving it still owns its port. `--keep-terminals` sets it for one run.
- **The daemon-path `attach` TUI is ended at teardown.** It does not exit when its server stops
  (measured); `run_captured`/`run_captured_pty` take a `stop` event the controller sets.
- **Agents are shown their worktree, not the user's checkout.** Found live: shown the checkout
  as "Project Root", OpenCode read a file from there, left its worktree, and waited on its own
  external-directory permission prompt until the timeout. An isolated run's context now names the
  worktree and says the checkout is off-limits; a native timeout names any pending permission or
  question it was waiting on.
- **`tests/test_native_tui_reliability.py`** covers all of the above, including real captured
  OpenCode payloads, a real process-tree stop and real `stop` events.
- **Live validation** (isolated scratch repos, real CLI, OpenCode's real TUI on a pty): normal
  run, genuine verifier FAIL → native repair → PASS, cross-rung escalation from an unusable model,
  and hard-kill → resume all passed, with exact token invariants and no surviving OpenCode
  process. The Antigravity bridge path was not exercised (bridge offline).

### Fixed — the run store held 141 runs that never happened

For most of this project's life the test suite wrote real run records into the developer's own
`.orchestrator/runs`. `tests/support.py` stopped that; it could not un-write what was already
there. **141 synthetic runs against 23 real ones — 86% of the dataset.** Everything downstream
read them and reported them faithfully: the board drew 126 `Ready to Merge` cards for pipeline
runs that never ran, `--stats` scored one agent over 324 executions that were mocks, and the
planner's memory and the design surface's evidence were computed from the same numbers. None of
it was a bug. Each was a correct projection of a corrupt store, which is the harder shape of
the problem — nothing anywhere reported an error.

- **`orchestrator/archive.py` and `--archive-runs`.** A run is *moved*, whole, into
  `.orchestrator/archive/runs/<run_id>/`, and `--restore-runs` moves it back byte-for-byte.
  The only filesystem verb in the module, in both directions, is a move: a run record is
  evidence, and evidence is corrected by being set aside with a reason attached, not destroyed.
  `--archived` lists what is held and why.
- **What counts as synthetic is evidence, not a guess.** Matching the task text would have
  worked exactly once, for the string those particular tests happened to use. A run is judged
  on two facts a real invocation cannot both produce: no execution reported any token usage,
  *and* every execution finished faster than a process launch plausibly could. Both must hold,
  and a run that recorded no executions at all is never synthetic — that is an interrupted run,
  which is real. On this store the populations do not come close to touching: the slowest
  synthetic execution took 0.88s, the fastest real one 8.26s.
- **It asks first**, shows the plan, supports `--dry-run`, writes an `archived.json` beside
  each archived run recording the timestamp, the reason and the evidence, and refuses to
  overwrite a live run on restore.
- **`tests/test_archive.py`** — 28 tests, including that a restored run is byte-for-byte what
  it was, that a conflicting id is reported rather than clobbered, and that a run with no
  executions is kept.

**What the clean store then said.** `--stats` now puts the researcher role's Antigravity
pairing at a **90% error rate over 10 runs** — the failure diagnosed by hand below, sitting in
the data the whole time and drowned out by 141 runs that never called a provider. It also
corrects a claim made from the polluted store: the Claude researcher has **3** recorded runs
here, not 20, which is under the five-observation line `--stats` flags. The reassignment was
right; the evidence quoted for it was inflated, and that is exactly the class of error a
corrupt dataset produces.

### Added — an escalation ladder can cross providers (`agent:` as a list)

`escalate_model` could only ever fall back *within one provider*, which was fine until the
thing that failed was the provider. ARCHITECTURE.md §9.0 recorded the gap; this closes it.

- **A rung is a pair, not a model.** Either `agent` or `model` may be a list, and
  `config.ladder_rungs` is the single place that knows how they combine: rung *i* is
  `(agent[i], model[i])`. Rung 0 runs the first attempt, rung N runs escalation step N, so both
  a repair and a retry escalate rather than repeating what just failed.
- **Mismatched list lengths are refused, not clamped.** Pairing a two-agent ladder with one
  model would hand a model id to a provider whose catalog has never heard of it, silently, at
  the moment a run is already failing.
- **An agent name this orchestrator cannot run is now a configuration error.** It used to fall
  through `get_runner` to Claude in silence — tolerable for a single agent, materially worse
  for a ladder, where a mistyped fallback would "escalate" to an agent nobody chose.
- **Preflight probes every rung**, the runner and terminal title are resolved per attempt, the
  `agent_retry` event records `next_agent` beside `next_model`, the cockpit card follows
  whichever rung the run actually reached, and verifier independence is checked across all
  rungs — a ladder that escalates the verifier onto the implementer's pairing loses
  independence exactly when it matters most.
- **`tests/test_agent_ladder.py`** — 29 tests, including a full pipeline run in which the first
  provider returns nothing on every attempt and the phase is rescued by the second, still
  producing exactly one `AgentResult`.

### Fixed — the cockpit's file viewer could not open any file

Clicking any file in the Explorer showed "Failed to read file". The server was fine throughout:
`/api/file` returned 200 with correct content for every path tried. The fault was one line of
the page. `tokenize()` built its pattern in a template literal with every backslash doubled
twice, so `(?=\\\\()` reached the regex engine as "a literal backslash, then an unclosed
group". `new RegExp` threw on the first line of every file, `openFile` caught it, and the only
symptom was the toast — for every file, of every type, in every root.

- The pattern is correctly escaped, and `/^\\d+$/` (which matched a literal backslash, so no
  number was ever highlighted) is now `/^\d+$/`.
- `tests/test_tier9.py` extracts that pattern from the served page, resolves its
  interpolations, and **compiles it**. An unbalanced group is unbalanced in any engine, and a
  page-level string assertion would not have caught this.
- Paths and root ids are `encodeURIComponent`-encoded — a file named `a b&c.py` is legal, and
  an unencoded one truncated the request at the ampersand.
- The viewer reports a binary or truncated file instead of drawing an empty pane, the failure
  toast names the file and the reason, and the tab id no longer goes through `btoa`, which
  throws on any path outside Latin-1.
- **The root picker now switches trees.** It listed every root and browsed only the first,
  which hid the tree a person most wants after a run: the worktree the agent actually wrote in.
- **A worktree root is offered only if it still holds a checkout.** `finish_worktree` asks
  git to remove one and git sometimes takes the contents while leaving the directory behind
  - which is what happened to every run in this checkout. The Explorer was listing ten roots
  that each opened onto nothing. `roots()` documented this case and did not handle it.

### Fixed — the pipeline now survives a flaky agent

This is the change that matters most in this release, and it came from reading the project's
own run store rather than from the roadmap. Across every real end-to-end run ever recorded
here, **seven of seven failures were the same event**: one agent returned empty output on the
first step of the pipeline, and the run halted with no second attempt. The model escalation
ladder existed but only fired after a verification *FAIL*, so an agent that produced *nothing*
got no retry, no fallback and no second model. A whole orchestrator was only ever as reliable
as its flakiest single call.

- **`execution.retry`** — an execution is now attempted up to `attempts` times (default 3) with
  a delay that doubles up to `max_backoff_seconds`, and an exception is retried the same way an
  empty reply is. With `escalate_model` on, each retry steps down the agent's model ladder,
  which is that ladder finally doing on execution failure what it already did on verification
  failure. `attempts: 1` restores the previous behaviour exactly, down to the wording of the
  error, so this is reversible by configuration rather than by a revert. `MAX_RETRY_ATTEMPTS`
  hard-refuses anything above 10: this is insurance against a flaky call, not a way to hammer a
  service that is down.
- **A retry is not an ensemble.** However many attempts one execution takes, the phase emits
  exactly one `AgentResult` — that list is what consensus is resolved from and what `--stats`
  computes pass rates over, so three attempts appearing as three results would corrupt both.
  The failed attempts become their own `agent_retry` events in the run store, and are announced
  on the console, because a retry costs a whole agent invocation and a run that pauses silently
  for eight seconds looks hung.
- **It still fails.** An execution that never succeeds fails the phase as it always did, with
  every attempt's reason kept on the result, so "it was flaky" and "it is broken" stay
  distinguishable after the fact.
- **`tests/test_retry.py`** — 27 tests, weighted toward the failure path: that the phase still
  produces one result, that `attempts: 1` reproduces the old behaviour, that an execution which
  never succeeds still fails with its history intact, that the ladder is stepped and reused at
  its last rung, and that the backoff doubles and is capped.

### Changed — the researcher moved off Antigravity, on measured evidence

Retrying revealed the failure was not flaky but **deterministic**, which is a more useful
finding than a fix. Reproduced on demand with three prompts to the same model:

| Prompt | output tokens | of which thinking | `response` |
| --- | --- | --- | --- |
| "Reply with the single word: OK" | 30 | 29 | `"OK"` |
| a reasoning-heavy arithmetic question | 1460 | 877 | 1148 chars |
| **"investigate this project and describe…"** | 1337 | 1039 | **empty string** |

Asked to *investigate a project* — precisely the researcher's job — the Antigravity CLI returns
`status: SUCCESS` with an empty `response`, having spent its output budget on thinking tokens.
It answers small prompts correctly, so `--doctor` passes and nothing looks wrong until a run
dies. Six consecutive live attempts produced six empty replies.

`agents:` now assigns the researcher to `claude / sonnet`. The reasoning is recorded in
`orchestrator.yaml` beside the entry, including what to restore to put Antigravity back —
now as the first rung of a ladder rather than as the only choice, so a repeat of this failure
escalates instead of ending the run.

> The pass-rate figures originally quoted here came from the polluted run store; see
> *the run store held 141 runs that never happened*, above, for what the cleaned dataset says.

**The result:** the same two-task parallel goal that delivered **0/2** before these changes now
delivers **2/2**, with both agents' work committed to their own branches and their own new test
files passing (5 tests and 12 tests). Exactly one retry fired during that run — a verifier that
timed out at 180 seconds — and it is the difference between 2/2 and 1/2.

### Fixed — a preflight test was coupled to the live team

`test_deep_mode_probes_each_binary_once` read `load_config(os.getcwd())`, which made it a test
about whatever team this project happens to be configured with; it broke the moment the
researcher was reassigned to a provider already in the pipeline. It now states its own case as
a fixture: four agent entries over three distinct binaries.

### Fixed — the test suite no longer writes into the project's run store

`tests/test_workflow.py` and `tests/test_verifier.py` invoked the real graph with
`project_root=os.getcwd()` and the run store enabled, so **every suite run appended a real run
record to the developer's own `.orchestrator/runs`**. One checkout had reached 141 suite
artifacts against 16 real runs.

This was not cosmetic. The run store is the dataset `--stats` analyses, the board projects and
the planner's memory reads, so a suite that appended to it quietly rewrote what the
orchestrator believed about its own repository — which is why `--stats` reported a meaningless
79% pass rate over mocked runs.

- **`tests/support.py`** — `redirected_run_store` points a test's store at a temporary
  directory while leaving it *enabled*, because several tests assert the `run_store_opened` and
  `run_store_closed` events in an exact sequence and disabling it would trade one wrong
  behaviour for a weaker test.
- **`RunStoreGuard`** and two regression tests: one asserting that running the graph writes
  nothing into the real store, and one asserting the redirected store really did record the run
  — because the first would also pass if the store had simply been switched off.


### Added — Roadmap section 8: the five steps after the cockpit

- **`orchestrator/memory.py` — the planning agent's memory (8.2).** Every goal used to be
  decomposed cold. The decomposer's prompt now carries what previous goals recorded here:
  collisions keyed by the *path* siblings fought over, hot paths counted by task rather than by
  file change, cost split by what delivered and what did not, gate rejections with their
  reasons, and past decomposition shapes. It is a **projection over the goal, run and approval
  stores** — not a new store, and a test asserts that building one writes nothing. Bounded three
  ways (`max_goals`, a git-query ceiling, `budget_chars`), it adds sections
  most-actionable-first and says how many it dropped. It reaches the model in exactly one
  place, framed as facts rather than instructions, and returns `""` on any failure — which is
  the cold prompt this project used before.
- **`--memory [N] [--json] [--plan-only]`** — print what the planner will be told, as a report,
  as the projection, or as the exact prompt block. A prompt addition a person cannot read is
  one they cannot argue with.
- **`planning.memory`** in `orchestrator.yaml`: `enabled`, `max_goals`, `budget_chars`, all
  validated at load.
- **`serve.goal_sessions` — the office watches a Goal (8.1).** The newest run directory is now
  only how the *goal* is found; from there the goal's log names each task's run and the two are
  paired. `latest_activity` carries the result under `sessions`, and `office.html` lays out one
  row of desks per session with one high-water mark per session rather than one for the whole
  page. A queued or skipped task is named with no session; a run with no goal reports itself as
  one session, so nothing that consumed the payload before had to change.
- **`stats.evidence_for_choices` and `GET /api/stats` — evidence beside the choice (8.3).**
  `--stats` reports *pairings*; a dropdown offers an agent, a model, or a role, so the same rows
  are folded three ways and keyed by what the chooser has in hand. Rendered in the design
  surface beside every provider, model and role dropdown, on the agent chip, and in the
  cockpit's Team pane. A role nobody verified reports no rate rather than 0%, and cost is
  weighted by how often a pairing ran.
- **Delivery-aware retention (8.4).** `--merged-older-than` (default `1d`) is the shorter
  threshold a branch whose pull request merged answers to, and `--keep-failed` no longer holds
  one back — a merge already answered the question that flag asks. Pruning a delivered branch
  **annotates** its record (`local_branch_pruned_at`, plus a history line) and never deletes
  one, so a card that reads *Merged* goes on reading *Merged* after the branch is gone. The
  plan names each candidate's delivery state.
- **`--review` — the third mode (8.5).** The only mode of the local server that can answer an
  approval gate or deliver a *Ready to Merge* card, over three routes and no more. It is not a
  relaxation of the other two: `--design` cannot approve, `--review` cannot edit the team, and
  the two are refused together. `GET /api/modes` reports which capability the server was given,
  so a page renders only the buttons it has; the office grows an approve/reject/deliver panel
  when — and only when — review is on.
- **`tests/test_tier10.py`** — 78 tests over the five steps: the goal-session pairing, the
  evidence folding and what it must *not* say, the retention decision with git and the delivery
  store stubbed, three real servers asserting each mode's refusals, and the memory projection
  and its budget.

### Changed

- **There is one implementation of "approve" and one of "deliver".** `serve.answer_gate` and
  `serve.deliver_card_by_id` are those implementations; the daemon's `approve`, `reject` and
  `deliver` control verbs now call them rather than carrying copies. `deliver_card_by_id` takes
  the board as an argument, so a caller cannot smuggle in a card the projection never put in
  *Ready to Merge*.
- **The cockpit is a console shell again**, matching what ARCHITECTURE.md §26.5 has described
  and `test_tier9.py` has asserted: an activity rail, four sidebar panes (Explorer, Team, Board,
  Terminals), `Ctrl P` go-to-file over `/api/find`, the team editor's write, remembered panel
  geometry, and the keyboard map (`Ctrl K`, `Ctrl P`, `Ctrl B`, ``Ctrl ` ``, `Ctrl I`) shown in
  the status bar. The page's two writes each name their own route at the call site rather than
  going through a generic poster, so "what can this page change?" is answered by reading two
  functions.

### Fixed

- The cockpit's status bar referenced a `#status-file` element that did not exist, so opening a
  file threw on the last line of `openFile`.
- `test_tier8.py`'s offline assertion for the cockpit forbade every `<script src`, which became
  wrong when the animation library was vendored and served by the daemon from `/vendor/`. It now
  asserts the property it was a proxy for: every script the page loads is a same-origin
  `/vendor/` path.

---

## Earlier

### Added — Phase 12: the Explorer, and the console shell

- **`orchestrator/explorer.py`** — a read-only projection of a directory tree and the text in
  it, over three kinds of root: the project checkout, each run's git worktree, and the run
  store. Confinement is by construction (`resolve_within` realpaths both ends and requires a
  separator, so `..`, absolute paths and symlinks out of the tree are one answer: refused).
  Everything is bounded and says when it was — capped listings, cut files, binaries named
  rather than decoded, a name search with a visit ceiling. A git-status overlay decorates the
  tree and degrades to nothing when git cannot answer.
- **Three GET routes on the daemon** — `/api/explorer`, `/api/file`, `/api/find`. Browsing is
  watching, so there is no write: a POST to any of them is a 404, and `control()` remains the
  closed list it was.
- **The cockpit is now a console shell** — activity rail; sidebar panes for Explorer, Team,
  Board and Terminals; a tabbed stage holding the pipeline and any opened files, with
  viewer-grade syntax highlighting; an inspector; a terminal panel with one tab per agent
  (replacing the modal drawer); a status bar; a command palette on `Ctrl K` with go-to-file on
  `Ctrl P`; draggable, remembered panel geometry; and a full keyboard map.
- **`tests/test_tier9.py`** — 65 tests covering confinement, listing, bounded reads, the git
  overlay, name search, root resolution, the three routes and their refusals, and the
  properties of the served page.

### Changed

- The cockpit no longer loads anything from the network, still writes only through the control
  routes and the team editor, and every one of its five animations stops under
  `prefers-reduced-motion`.

### Fixed

- `explorer.read_file` now honours a `max_bytes` smaller than the binary-sniff window, which
  previously returned up to 4 KB regardless of the cap.
