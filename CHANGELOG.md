# Changelog

## Unreleased

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

`agents:` now assigns the researcher to `claude / sonnet`, which `--stats` shows filling that
role 20 times in this repository with a 0% error rate. The reasoning is recorded in
`orchestrator.yaml` beside the entry, including what to restore to put Antigravity back.

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
