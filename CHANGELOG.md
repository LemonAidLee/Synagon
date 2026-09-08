# Changelog

## Unreleased

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
