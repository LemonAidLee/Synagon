# Synagon Roadmap — from a pipeline to a delegation system

[ARCHITECTURE.md](ARCHITECTURE.md) describes the system **as it is**. This document describes
where it is **going**, and why each step is the next one. It is deliberately separate so that
neither file has to hedge: nothing here is implemented until it moves into ARCHITECTURE.md.

---

## 1. The destination

Two existing projects describe the two halves of the target:

**[Untrivial-ai/agent-orchestrator](https://github.com/Untrivial-ai/agent-orchestrator)** (AO) —
several coding agents working **in parallel on one repository**, each in its own git branch and
worktree, coordinated by a persistent **project orchestrator** that decomposes goals into
focused tasks and sequences them. Progress is legible on a **Kanban board whose card positions
are derived** from session state, pull requests, CI, and review — never hand-maintained. Its
columns are *Working*, *Needs You*, *In Review*, *Ready to Merge*.

**[arturitu/the-delegation](https://github.com/arturitu/the-delegation)** — a **no-code
environment for designing agent teams**: a node editor for composing who does what, predefined
team templates, per-agent model choice, live cost and token tracking, streamed agent
"thoughts", and **PR-style human approval gates** with an explicit auto-approve guardrail.

Stated as one sentence:

> **A team of agents you compose visually, that works in parallel on a real repository under
> git isolation, decomposed and sequenced by a planning agent, with progress and cost visible
> on a board derived from facts, and human approval where you choose to require it.**

AO contributes the **execution and coordination substrate**. The Delegation contributes the
**design surface and the human-in-the-loop contract**. This project already owns a
surprising amount of the first and none of the second.

---

## 2. What already serves the destination

This is not a rewrite. The current system is a single-worker version of AO's substrate, and
several of its load-bearing rules are the *same rules* the target needs.

| Target requirement | What exists today | Status |
| --- | --- | --- |
| Worker isolation: own branch + worktree | `workspace.py` — one worktree and branch per run, never touching the checkout, never auto-merging | **Done.** `workspace.py:25` already cites AO for the same rule. |
| Board positions derived, never stored | Invariant 1 and `status.py` — status is a pure function of durable facts | **Done, and it is the key asset.** A board is a *projection*, not new state. |
| A "Needs You" state | `BLOCKED` verdict + `blocked_reason`, which deliberately spends no repair budget | **Done in substance**, missing only the column. |
| Live activity to observe | `events.jsonl` — append-only, one JSON object per event | **Done.** A daemon or UI can tail this. |
| Per-agent model choice | `agents:` entries, validated against the catalog | **Done.** |
| Cost and token tracking | `types.TokenUsage`, `metrics.py`, `budget.py`, `--stats` | **Done, and ahead of both references** (ceilings, not just display). |
| Team composition as data | `orchestrator.yaml` — agents, roles, responsibilities, ensembles, ladders | **Done.** A node editor is a *front-end for this file*, not a new model. |
| Recovery from interruption | `--resume` replaying the event log | **Done.** |
| Retention of parallel worktrees | `finish_worktree`, `--prune-runs` | **Done, and required sooner** once N workers run per goal. |
| PR-style human approval before anything leaves | `approval.gates`, then `delivery` | **Done.** A gate decides, and only a person's answer pushes. |

| Target requirement | Gap |
| --- | --- |
| **Decomposition** — one goal → many focused tasks | **Built** (Phase 1): `--plan-only`. |
| **Parallel workers** — many tasks in flight at once | **Built** (Phases 2-3): `--delegate --parallel N`. |
| **Collision handling** between concurrent workers | **Built** (Phase 3): detected and reported; never resolved. |
| **A persistent planning agent** with memory across runs | Still open. Every goal is decomposed cold (§7.2). |
| **A live view of who is working** | **Built** (Phase 4): the office, driven by `agent_started`. |
| **A board** | **Built** (Phase 4): `--board`, and `--serve` for the live office. |
| **Approval gates** mid-run | **Built** (Phase 5): three gates, with `auto_approve` as the guardrail. |
| **PR / CI facts** feeding state | **Built** (Phase 6): `--deliver`, `--refresh-deliveries`, and two new board columns' worth of forge facts. |
| **A design surface** (node editor, templates) | **Built** (Phase 7): `--design`, `--teams`, `--apply-team`. |

---

## 3. Concept model

Today's vocabulary conflates several things under **run**: the user's request, one pipeline
execution, one git branch, and one store record. The target needs them separated. Proposed
names, chosen to extend rather than replace what exists:

```text
Project        the repository, and its configuration          (exists: project_root + orchestrator.yaml)
  Goal         what a person asked for, in their words        (new — today this is `task`)
    Task       one focused, independently deliverable unit    (new — the decomposer's output)
      Session  one attempt at a Task by a team of agents      (exists: today's `run`)
        Phase  one role, one or more agents, in parallel      (exists)
```

* A **Session** keeps everything a run has now: worktree, branch, event log, derived status,
  budget, resume. It is still called a `run` on disk; renaming is mechanical and has not been
  worth the churn yet.
* A **Task** owns a queue position, dependencies on other Tasks, and the Session that carried
  it out. Its state is derived from that Session, exactly as a Session's status is derived from
  its facts — the same rule, one level up. **Built** (`status.derive_task_state`).
* A **Goal** owns the decomposition and the Sessions its Tasks ran. **Built**
  (`goals.py`, `status.derive_goal_status`). The planning *conversation* — a decomposer with
  memory across Goals — is still open (§7).
* The **Board** is a pure projection over Tasks. No new state: `board.py` should look like
  `status.py` — a pure function from facts to columns.

Column mapping — **built** in `board.py`, derived from facts, never stored:

| Column | Derived from |
| --- | --- |
| Queued | Task has no Session yet, or is waiting on a dependency |
| Working | latest Session status is a progress status (`analyzed` … `repaired`) |
| Needs You | Session ended `blocked`, a collision, a dependency that would not merge, a pending gate, a failed check, or a requested change |
| In Review | a `before_merge` gate is waiting on a human, or a pull request is in flight |
| Ready to Merge | delivered, and either cleared at the `before_merge` gate or never gated |
| Merged | its pull request was merged; the work has landed |
| Stalled | Session ended `failed`, `error`, or `budget_exhausted` |

The last three rows now have two sources. Before a person presses *Ready to Merge* they are
derived from Sessions and gates; afterwards the forge's recorded CI and review facts move the
card too (Phase 6). A card in a project that never enabled delivery is unaffected.

---

## 4. The path

Each phase is a step the user can direct, review, and stop at. Every phase must leave the
system working and the test suite green.

### Phase 0 — Make PASS mean something (prerequisite) — **built**

An **objective acceptance gate**: a configured command (`pytest -q`, `npm test`) run by the
orchestrator itself in the Session's worktree, with its exit code and output recorded as a
fact. See ARCHITECTURE.md §1 invariant 2 for why: today every verdict is an LLM's opinion, and
`Ready to Merge` cannot honestly exist without a check the orchestrator performed itself.

This is listed first because decomposition multiplies the cost of a wrong verdict: with ten
parallel Tasks, ten unverified PASSes is not a board, it is a rumour.

### Phase 1 — Decompose a Goal into Tasks — **built (`--plan-only`)**

A `decomposer` (or `orchestrator`) role that reads the Goal and the project context and emits a
**structured task list** — id, title, intent, acceptance criteria, dependencies, touched areas —
recorded as a durable fact. Executes nothing at first: `--plan-only` prints the task graph and
stores it. Cheap, offline-testable, and it is the single feature that separates a pipeline
from a delegation system.

### Phase 2 — Run Tasks, one at a time, from the graph — **built (`--delegate`)**

A scheduler that walks the graph in dependency order and runs one Session per Task, reusing the
entire existing pipeline unchanged. **Goal-level budgets** appear here — `budget.goal_max_*` — a
ceiling across Tasks, because a decomposition otherwise multiplies a per-session ceiling by
however many tasks it happened to emit.

Built as `orchestrator/scheduler.py` plus a Goal store (`orchestrator/goals.py`). A Task with
dependencies starts from its dependency's branch, with any further dependencies merged forward,
so `depends_on` actually means something. See ARCHITECTURE.md §16.

### Phase 3 — Run Tasks in parallel — **built (`--parallel N`)**

`delegation.max_parallel` (or `--parallel N`) is the only difference from Phase 2: there is one
scheduler, and one task at a time is its degenerate case. Two schedulers would have been two
sets of bugs.

**Collisions are detected and reported, never resolved.** After every branch exists, sibling
tasks that changed the same file are flagged `needs_attention` with the overlapping paths
named, and the Goal reports `blocked` — the Needs You column, arriving before the board does.
Guessing which sibling was right is exactly the failure isolation exists to prevent.

### Phase 4 — The board, and then the office — **built (`--board`, `--serve`)**

`board.py` as a pure projection over the Goal store, surfaced first as `--board` (text and
`--json`), then over a tail of `events.jsonl` for live updates. Most of the work is already
done: `status.derive_task_state` and `derive_goal_status` produce every state a column needs,
and `--show-goal` already renders them per goal. What remains is the column mapping in §3, a
projection across goals, and the reader. Because the projection is pure and the event log is
already append-only, the web view is a *reader*, not a rewrite — and the CLI board is the
contract that keeps it honest.

Then the **embodied view** (decision 2): `python -m orchestrator --serve` summons a local web
page where each role is a small character in a shared office, walking, working, and handing
off to the next role as the run proceeds. This is not a separate system — the office reads the
run's event log and never writes to it.

Built as `board.py` (pure projection), `serve.py` (a loopback, read-only server) and
`web/office.html` (one self-contained page: no CDN, no build step, so it works offline). The
characters are drawn on a 2D canvas; replacing that renderer with a 3D one changes nothing
behind it, because the page consumes a projection rather than the engine. The store gained one
event to make this honest — `agent_started` — so the office shows who is working rather than
inferring it from who has finished. See ARCHITECTURE.md §18.

### Phase 5 — Approval gates — **built (`approval.gates`)**

`BLOCKED` is the verifier discovering a human is needed; a **gate** is the configuration saying
so in advance. Three are available — `after_decomposition`, `before_task`, `before_merge` — with
`auto_approve` as the explicit guardrail. A gate records a pending approval and stops at a clean
boundary rather than blocking a process on stdin, because the person it waits for may not be
there. `--approvals`, `--approve`, `--reject`, then `--resume-goal` to carry on. See
ARCHITECTURE.md §19.

Two things this phase also closed:

* **`--resume-goal`** (previously open item 6). A goal picks up where it stopped; tasks that
  already delivered are replayed, never re-run.
* **The BLOCKED resume defect.** `--resume` on a blocked Session used to replay the stored
  verdict and re-verify nothing, so the human's fix was never checked. A BLOCKED verification is
  now never replayed: resuming *is* the person saying they acted.

### Phase 6 — Outward integration, on the human's word — **built (`--deliver`)**

A branch is pushed and a pull request opened **when a person presses Ready to Merge** (decision
4), never automatically. There are exactly two triggers, and both are someone acting:
`--deliver <card>`, which refuses anything the board does not already place in *Ready to
Merge*; and `--approve` on a `before_merge` gate, when `delivery.on_approve` is set. Both are
gated on `delivery.enabled`, which is false by default — a project that has not opted in cannot
reach a remote whatever anyone approves. That is now **invariant 13, never auto-push**: the
never-auto-merge rule in network form.

Afterwards the forge owns facts this project does not. `--refresh-deliveries` reads CI and
review state back and records it, and `board.py` moves the card accordingly. The important
design choice is that the *projection never reaches for the network*: `--board` reads recorded
facts, so it is instant and works offline, and every card carries `delivered_as_of` because a
card showing a stale check as current would be the projection lying.

The forge is driven through `gh`, the user's own locally-authenticated CLI, so decision 3
holds unchanged. Without `gh` the branch is still pushed and only the pull request is not —
`pushed` and `failed` are different states, because "go and look for the branch you already
published" is not the same advice as "try again". See ARCHITECTURE.md §20.

### Phase 7 — The design surface — **built (`--design`)**

A node editor that reads and writes `orchestrator.yaml` — the file is already the team model,
so the editor is a view over it — plus team templates: `careful` (three verifiers, unanimous
consensus), `fast` (one of each), `balanced`, and `solo`. Composing a team here is what
populates the office in Phase 4 with characters, because both read the same `agents:` block.

The editor draws **phases**, not rows: consecutive same-role agents, which is what `graph.py`
already means by a phase. Several agents in one phase is an ensemble; one agent with several
models is an escalation ladder. Two implementers in a phase is refused before you save rather
than after.

Two decisions made this safe to expose to a browser. The write is **surgical** — it replaces
the `agents:` block, `max_repair_attempts`, and `verification.consensus`, and leaves every
other byte of that heavily-commented file alone, because round-tripping it through a YAML
dumper would produce a valid file that had lost the reason for every value in it. And it is
**validated before it lands**, with a timestamped backup, because a config that cannot load is
a project that cannot run.

Invariant 12 was refined rather than broken: `--serve` is unchanged and read-only, `--design`
enables exactly one route that writes, and no route on either server can start a run, answer a
gate, or push a branch. See ARCHITECTURE.md §21.

### Phase 8 — The daemon shell — **built (`--daemon`)**

A persistent local process wrapping the engine: a control API (start, cancel, approve, reject,
deliver) alongside everything `serve.py` already reads, and `GET /api/stream` relaying
`events.jsonl` as server-sent events as they are appended rather than being polled for.
`office.html` now consumes that stream and falls back to its original poll when there is none,
which is what proves the same page works against both servers. `tools/daemon_client.py` starts
a run through the new endpoint instead of a terminal invocation, which is what proves the
engine no longer needs its own command line to be driven.

The local-token safeguard was designed here rather than deferred, and it is three things that
must all hold: a per-launch token substituted into each page the daemon serves (so it never
travels in a URL), an `Origin` check that refuses a foreign origin before reading a body, and
requiring the token in a *header*, which is what forces a browser to preflight a cross-origin
request and therefore fail it. See ARCHITECTURE.md §22.

Invariant 12 was **revised, not broken**: its purpose — a decision is always a deliberate human
act, never a timer or an external signal — is unchanged; what changed is where that act may be
taken from. `Daemon.control` is a closed list of verbs, each reached only from a page the daemon
itself served, and each calling the same module the CLI calls. Cancelling is honestly
cooperative: no running agent is killed, but no further task is authorised.

---

### Phase 9 — The cockpit — **built (served at `--daemon`'s root)**

One column per configured role in `teams.phases_of()`'s order — which is `graph.py`'s execution
order, so "left to right" is a fact about the page rather than a metaphor — and one card per
agent in it. An ensemble is several cards in one column. Each card's state is derived by the
pure `cockpit.py` from `agent_started` and `agent_result` and from nothing else, which is
exactly the pair Phase 4 added so a reader could see work in progress rather than guess at it.

A goal is typed into the dashboard; gates are answered where a person is already looking; a
Ready to Merge card can be delivered from it. Each column's "+" posts a whole team to the same
`POST /api/team` the design surface uses — the cockpit **relocates** that editor into the live
dashboard rather than reimplementing it. The whole page is one read (`/api/cockpit`), because a
dashboard assembled from five independently-timed polls shows five different moments at once.
See ARCHITECTURE.md §23.

---

### Phase 10 — The embedded live terminal — **built (the card's terminal panel)**

`terminals.py` captures each agent subprocess's output into a bounded, sequenced ring buffer and
relays it to a panel in the cockpit. The entire integration is **one optional hook** in
`launcher.py`: no agent adapter and nothing in `graph.py` changed, and with no recorder
installed the headless path is byte-for-byte the `subprocess.run` it always was.

Per §10.2 this is not a thought-process parser — it shows the raw output the CLI is already
producing, so no provider changing its format can break it. Two honesty notes rather than
claims: the transcript is bounded and *says* when it truncated, and the capture is a pipe
rather than a pseudo-terminal on Windows, because ConPTY needs a package this project does not
depend on. A CLI that checks `isatty` will therefore choose its plain rendering. A real pty
belongs with the shell in Phase 11, and is the reason that shell is Electron.
See ARCHITECTURE.md §24.

---

### Phase 11 — The desktop shell — **built (`desktop/`)**

An Electron window: open a project folder, get a window; the window starts that project's
daemon on a free port, waits for its own handshake file rather than sleeping or reading a log
line, loads the cockpit, and stops the daemon when it closes. Native "Open Project Folder…" and
"New Window", and an installable package.

It is thin on purpose, and the thinness is the property worth protecting: it spawns
`python -m orchestrator --daemon`, and that is the whole of its relationship with the engine.
The page runs sandboxed with no Node integration and `preload.js` exposes one boolean, so the
shell adds no way to act that a daemon route does not govern. Delete `desktop/` and `--daemon`
loses a window frame and nothing else — which is what §10.7's split was for, and what the tests
assert against the files themselves. See ARCHITECTURE.md §25.

---

### Phase 12 — The Explorer, and the console shell — **built (`explorer.py`, `cockpit.html`)**

Every view before this answered a question about the *work*. None could answer the one a person
asks the moment an agent finishes: **what did it write?** `explorer.py` is a read-only
projection of a directory tree and the text in it, over the three kinds of root this project
already creates — the user's checkout, each run's worktree, and the run store. It reaches the
UI on three GET routes (`/api/explorer`, `/api/file`, `/api/find`); a POST to any of them is a
404, and `control()` is still the closed list it was.

Browsing is watching, so invariant 12 is not bent here. Confinement is by construction rather
than by convention: `resolve_within` realpaths both ends and requires a separator, so a `..`
climb, an absolute path and a symlink out of the tree are one answer. Everything is bounded and
says when it was — a capped listing, a cut file, a binary named rather than decoded, a name
search with a visit ceiling.

The cockpit became the shell around it: an activity rail, four sidebar panes, a tabbed stage,
an inspector, a terminal panel with one tab per agent, a status bar, and a command palette. It
still loads nothing from the network, still writes only through the control routes and the team
editor, and its five animations each communicate one thing and all stop under
`prefers-reduced-motion`. See ARCHITECTURE.md §26.

---

## 5. What must not break

The invariants in ARCHITECTURE.md §1 are the reason this codebase is a good foundation, and
scaling to many workers makes most of them *more* important, not less:

* **Facts stored, status derived** — this is exactly AO's Kanban principle. A board that stores
  card positions drifts from reality; a board that derives them cannot.
* **Never touch the user's checkout / never auto-merge** — with N workers, an auto-merge is N
  times as likely to be wrong.
* **Never discard uncommitted work** — with N worktrees, retention bugs are N times as costly.
* **Degrade, never fail** — a board, a daemon, and a UI are all observability. None of them may
  become a precondition for running a task.
* **Bounded by construction** — a decomposer that can emit tasks is a loop generator; Goal-level
  ceilings must exist before parallel execution does.

Two additions the decisions in §6 made binding, both now in the code:

* **Never auto-push** — invariant 13. Branches reach a remote only when a person presses Ready
  to Merge, and only in a project that turned `delivery.enabled` on. The never-auto-merge rule
  now has a network-shaped sibling.
* **Zero provider API keys stands.** New agents must be locally authenticated CLIs — including
  free-tier provider subscriptions driven through their own CLI — never keys this project
  reads, stores, or transmits. Phase 6 held to this: the forge is reached through `gh`, not
  through a token.

Phase 7 added a third, because it introduced the first write in the web surface:

* **One writable route, one file.** The design surface edits the team file and nothing else,
  only under `--design`, only after the result validates, and always leaving a backup.

Phase 8 added a fourth, because it introduced the first surface that can touch a *run* — and
this is the one to watch, because it is the only place where a slip would not look like a bug:

* **What may act is enumerated, and only reachable from a page this process served.** The
  daemon's control API is a closed list of verbs, each calling the module the CLI already
  calls; every one of them requires a per-launch token and refuses a foreign `Origin`. There is
  no timer, no webhook, and no route that acts on anything the daemon did not itself present a
  button for. Invariant 12 was rewritten to say *that* rather than "the server never writes" —
  its purpose (a decision is a deliberate human act) is what was always load-bearing, and it
  still holds.

---

## 6. Decisions taken

*Recorded 2026-09-07. These are settled; the phases above reflect them.*

1. ~~**Delivery form — CLI engine, UI as a reader.**~~ **Superseded — see §10.** The
   orchestrator's engine still stays a command that can be run headless, and every projection
   still gains `--json`. What is revised is "UI as a reader": the product goal grew into an app
   that starts and steers work, not only watches it, which needed a persistent daemon rather
   than a process that exits. The reasoning that produced this decision — keep the engine's
   output a clean, stable contract rather than a rendering — is exactly why §10's daemon is
   designed to call the *same* engine rather than replace it.
2. **The embodied office is in scope.** Opening a project with orchestration should be able to
   summon a web page where each role is a character, visibly working and handing off. Making
   the work fun to watch is a goal, not a decoration. It is a *reader* of the event stream
   (Phase 4), so it cannot destabilize the engine.
3. **Zero provider API keys stands.** External providers are used through their own
   locally-authenticated CLIs, including free-tier subscriptions. No key is ever read, stored,
   or transmitted by this project. The multimodal generation The Delegation gets from the
   Gemini API is therefore out of scope unless a CLI offers it.
4. **Nothing is pushed until a person says so.** Work accumulates on local branches. Pressing
   *Ready to Merge* is what pushes the branch and opens the pull request.

## 7. Where Phase 7 leaves the destination

The sentence in §1 was:

> A team of agents you compose visually, that works in parallel on a real repository under git
> isolation, decomposed and sequenced by a planning agent, with progress and cost visible on a
> board derived from facts, and human approval where you choose to require it.

Every clause of it now exists. *Compose visually* is `--design`; *in parallel under git
isolation* is `--delegate --parallel N` over `workspace.py`; *decomposed and sequenced* is
`decompose.py` and `scheduler.py`; *progress and cost on a derived board* is `board.py`,
`--serve`, and `--stats`; *human approval where you choose* is `approval.gates`, and Phase 6
carried that all the way out to a pull request.

That is worth saying plainly, because it changes what the next steps are *for*. The remaining
work is no longer "reach the destination". It is: **make the parts that exist good enough to
live in every day, and give the planning agent the one thing it still lacks — a memory.**

*Note (2026-09-07): the destination itself was reopened the same day this was written. §10
describes a bigger interface than the one this section declared complete — read it before
treating "reached" as the last word.*

*Note (2026-09-08): the second half of that sentence is now also done. §8 is built end to end,
and the planning agent has the memory this section named as the one thing it still lacked. What
is left is in §9 — questions about scope and policy, not clauses of the destination.*

---

## 8. The next steps — **all built**

*Ranked as they were written. All five are now implemented; ARCHITECTURE.md describes each one
as it is. What follows is the argument that produced them, kept because the reasoning is the
part worth remembering, with a note under each saying what it became.*

Each was a step the user could direct, review, and stop at, and each left the test suite green —
the same rule the phases above were built under.

### 8.1 The office should watch a Goal, not the newest run *(smallest, and it is a defect)* — **built**

Phase 3 made several sessions run at once. Phase 4 built the office against `latest_activity`,
which reads **the newest run directory** — so with `--parallel 3`, the office shows one desk
and switches to whichever session wrote most recently. The board already gets this right,
because it is a projection across every goal and session; only the office does not.

The fix is small and needs no new machinery: read the goal's event log alongside the run's (the
server already loads `goal_events`), give each in-flight task its own desk, and let the office
show a *team* rather than a session. Nothing behind it changes — it consumes a projection, so
this is a front-end correction to a claim the office already makes.

**Do this first because it is cheap, visible, and currently untrue.**

> **Built as `serve.goal_sessions`, carried on `latest_activity` under `sessions`.** The newest
> run is now only how the goal is *found*; from there the goal's log names each task's run and
> the two logs are paired. The office lays out one row of desks per session, keeps one
> high-water mark per session rather than one for the office, and names a queued or skipped
> task with no session rather than omitting it. A run with no goal reports itself as one
> session, so nothing that consumed the payload before had to change. See ARCHITECTURE.md
> §18.2.

### 8.2 The planning agent's memory *(the largest, and the last hollow clause)* — **built**

`--plan-only` decomposes every Goal **cold**. The decomposer sees the project's context and the
goal text, and nothing else: not the decompositions that came before, not which tasks collided
last time, not which areas of this repository are dense, not what a task of this shape actually
cost. AO's "persistent project orchestrator" is the half of the destination this project has
built the *body* of and not the *memory*.

What that needs, in order:

1. **A decision** (§9.2): does a Project accumulate knowledge across Goals, or is each Goal
   decomposed fresh? Persistence implies a store above the Goal store — the first thing in this
   codebase whose lifetime is the *repository*, not a run.
2. **Facts worth remembering**, and only facts: which files each delivered Task actually
   touched (`changed_paths` already computes this), which sibling pairs collided, what each
   task cost, what a person rejected at a gate and why. Every one of those is already recorded
   somewhere; the memory is a *projection over the stores*, not new bookkeeping — which keeps
   invariant 1 intact one level higher.
3. **Feeding it into the decomposer's prompt**, bounded. A memory that grows without a ceiling
   is a context window that eventually fails, so it needs a summariser and a budget, exactly as
   the repair loop needed a ceiling.

The prize is concrete: a decomposer that knows two tasks touching `config.py` collided last
week will stop emitting that pair, which is the collision policy question (§9.3) answered by
prevention rather than by resolution.

> **Built as `orchestrator/memory.py`, and the decision in step 1 was answered "yes".** A
> Project accumulates knowledge across Goals; its lifetime is the repository. It is *not* a new
> store — every fact is read back from the goal, run and approval stores, so it is a projection
> exactly as the board is, and a test asserts that building one writes nothing. It carries
> collisions keyed by path, hot paths counted by task, cost split by what delivered, gate
> rejections with their reasons, and past decomposition shapes. It is bounded three ways
> (`max_goals`, a git-query ceiling, `budget_chars`), adds sections most-actionable-first and
> says what it dropped, and reaches the model in exactly one place: the decomposer's prompt,
> framed as facts rather than instructions. `--memory` prints what the planner will be told,
> because a prompt addition a person cannot read is one they cannot argue with. See
> ARCHITECTURE.md §27.

### 8.3 Put the evidence next to the choice, in the design surface — **built**

`--stats` already computes pass rates and cost per agent, per model, and per role, and whether
repair attempts and ensembles earn their price. The design surface lets you pick an agent and a
model from a dropdown **with none of that visible**. Both halves exist; nothing connects them.

Serving the stats projection alongside `/api/team` and rendering it beside each dropdown — *this
verifier has passed 41% of the time and costs 12k tokens; this ensemble has never changed a
verdict* — is what makes composing a team an evidence-based act rather than a preference. It is
also the cheapest way to make `--stats` matter, since a report nobody opens changes no
decisions.

> **Built as `stats.evidence_for_choices` behind `/api/stats`.** `--stats` reports *pairings*;
> a dropdown offers an agent, a model, or a role, so the same rows are folded three ways and
> keyed by what the chooser has in hand. Rendered in the design surface beside every provider,
> model and role dropdown, and in the cockpit's Team pane. Two rules hold it honest: a role
> nobody verified reports no rate rather than 0%, and cost is weighted by how often a pairing
> ran. See ARCHITECTURE.md §21.0.

### 8.4 Retention across the whole lifecycle, now that work can land — **built**

`--prune-runs` predates delivery and knows nothing about it. Two consequences worth closing:

* A **merged** card's local branch is the safest thing in the repository to sweep — its work
  landed — and today it is treated exactly like an unreviewed one. Merged should be *more*
  sweepable, not the same.
* A delivery record should **outlive** the branch it describes. Pruning a branch whose pull
  request merged must not erase the record that it merged, or the board will forget that the
  work ever landed.

Small, unglamorous, and the kind of thing that decides whether this is still pleasant to use in
six months.

> **Built.** A merged branch answers to `--merged-older-than` (default `1d`) rather than
> `--older-than` (default `30d`), and `--keep-failed` no longer holds it back — that flag asks
> whether something might still need inspecting, and a merge answered it. The two rules that
> protect work rather than evidence are unchanged: not the branch you are standing on, and not
> one with a dirty worktree. Pruning a delivered branch **annotates** its record
> (`local_branch_pruned_at`, plus a history line) and never deletes one, so a card that reads
> *Merged* goes on reading *Merged*. See ARCHITECTURE.md §7.3.

### 8.5 Close the *Needs You* loop where a person is already looking — **built**

The friction that remains is the gap between *seeing* and *acting*: the board shows a card, and
answering it means typing a command with an id somewhere else. This is §9.6, and Phase 7 has
now established the pattern for doing it safely — one narrow route, validated server-side,
enabled only by the command that asks for it.

If it is done, it should be a **third mode**, not a relaxation of the first two: `--serve` stays
read-only, `--design` writes only the team file, and a review mode would be the only thing that
can answer a gate or deliver a card. A stray click must never be able to become a decision on a
server someone opened to watch a run.

> **Built as `--review`, and it is a third mode exactly as argued.** `--serve` reads,
> `--design` writes the team file, `--review` decides — and no mode is a superset of another:
> `--design` cannot approve, `--review` cannot edit the team, and the two are refused together.
> Three verbs (`approve`, `reject`, `deliver`) and no more; anything else under the prefix is a
> 404. `/api/modes` reports which capability the server was given so a page renders only the
> buttons it has. The acts call what the CLI calls, and the daemon's control verbs now call the
> *same two functions* — there is one implementation of "approve" in this project rather than
> three. See ARCHITECTURE.md §18.3.

---

## 9. Still open

1. **Scope of a Project.** One repository at a time (AO's model), or many concurrently?
   Everything is keyed by `project_root` today, so many-at-once is a shell of the current
   design rather than a change to it — but a board across repositories is a different product.
2. ~~**The planning agent's memory.**~~ **Answered: it persists, and it remembers only what
   the stores already recorded.** A Project accumulates knowledge across Goals; its lifetime is
   the repository. What it is allowed to remember is bounded twice over — by being a projection
   (it can hold no fact no store holds) and by an explicit budget on what reaches the prompt.
   `--memory` makes it readable, which is the other half of the answer: a memory a person
   cannot inspect is one they cannot correct.
3. **Collision policy.** When two workers touch the same files: detect and escalate (what it
   does now), prevent, or attempt automatic rebase/merge resolution? *Partly answered:* §8.2
   now gives the decomposer the collisions this repository has actually had, keyed by the path,
   so prevention is available to it. Whether that is *enough* — and whether automatic
   resolution is ever wanted — needs goals run against the memory to say. Phase 6 raised the
   stakes: two delivered branches touching one file are two pull requests.
4. **Who chooses the team.** Phase 7 answered "the human, visually", and §8.3 has now put the
   evidence beside that choice. The alternative is still open: should the planning agent select
   roles and models per Task from the catalog, using that same projection rather than a
   person's judgement? Everything it would need is now served on one route.
5. **How far the office should go.** It draws 2D characters and animates events. Whether that
   becomes a 3D office (Three.js, models, pathfinding) is a front-end question the projection
   does not care about — but it decides whether the page can stay dependency-free and offline,
   which both pages currently are.
6. ~~**Whether a served page should be able to answer a gate.**~~ **Answered: yes, and only
   when the mode was asked for by name.** `--review` (§8.5) is the third mode; `--serve` keeps
   the read-only guarantee it was built with.
7. **Scope of a Project, revisited.** §9.1 asked whether one orchestrator could span several
   repositories. The memory makes the answer sharper rather than easier: everything it knows is
   *about one repository*, so a cross-repository board would need a memory per repository and a
   rule for which one a goal is decomposed against. Nothing yet needs it.


---

## 10. Revision (2026-09-07): from a reader to a cockpit

*This section revises Decision 1 in §6 and reopens the destination §7 declared reached. It does
not undo Phases 0–7 — every module they built is exactly what §10.8 builds **on**, not what it
replaces. Nothing here is implemented; per this document's own rule, none of it moves into
ARCHITECTURE.md until it is.*

### 10.1 What changed, and why this is a revision and not an addition

Decision 1 committed to a specific shape: *"the orchestrator stays a command that exits; the
interface is built against projections, as a reader."* That was right for what it governed — a
board and an office you watch. The product goal has since grown into something that decision
explicitly ruled out: an app you **drive**. Opened per project, like an IDE. Showing a live
dashboard of every role and every agent working. Started by typing into it, not by remembering
a command. Answerable by clicking, not only by `--approve <id>`.

That is a different relationship between the person and the tool, and it is worth being honest
that it is a *reversal* of §6.1, not a refinement of it — the whole reason "watching is not
steering" (invariant 12) held for six phases was that starting and steering work always went
through a terminal. This section is where that stops being true, deliberately, and says what
has to change to do it safely.

Restated destination:

> A desktop app, opened per project like an IDE, showing every configured role as a column and
> every agent assigned to it as a card. A goal is started from the dashboard, and work flows
> through the columns left to right exactly as the pipeline executes, each card's status
> updating live as it happens. Clicking a card opens that agent's own terminal, live, exactly
> as if you had run it yourself.

### 10.2 What this takes from AO, and what it does not

AO's own README was checked rather than assumed, because guessing about another project's
features and writing the guess into this one's roadmap would be a bad way to start. Three things
transfer directly, and one does not:

* **The Kanban vocabulary already matches.** AO's columns include *Working* and *Needs You* —
  the same names this project's own `--board` already uses (Phase 4), independently arrived at
  from the same idea (AO's central Kanban principle, cited since §1). Nothing to change here.
* **Context attached to a unit of work.** AO keeps "the task, conversation, terminal, changed
  files, browser preview, pull request, CI, and review state" attached to each session. This
  project already has most of that scattered across stores — `store.py` (task/conversation),
  `workspace.py` (changed files), `delivery.py` (pull request, CI, review, Phase 6) — so the
  work here is presentation, not new data.
* **The live agent view is a terminal, not a transcript.** This is the one that reframes §10.5
  below. AO's own words: *"Use structured Chat or the agent's native terminal UI."* It does not
  parse or stream a structured thought process — it gives you the raw terminal the agent's CLI
  is already running in. That is a simpler, more proven pattern than reverse-engineering each
  provider's internal reasoning format, and it is what §10.5 builds toward.

  *(§1 attributes "streamed agent thoughts" to the **other** reference project — The Delegation,
  not AO — and that specific claim has not been checked the way AO's was here. Do not read this
  section as "neither reference does this"; only AO has actually been verified. If The
  Delegation's version turns out to be a structured stream rather than a raw terminal, it is
  worth re-reading before committing to the terminal-only approach in §10.5.)*
* **What is not being taken:** AO's Kanban is organized by *task*. The dashboard this section
  describes is organized by *role* — that is this project's own idea, not AO's, and it does not
  replace `--board` (§10.3 explains why both exist).

### 10.3 The cockpit: role columns, not task columns

Call this new view **the cockpit**, to keep it distinct from two things that already exist and
are not being replaced:

* **The board** (`--board`, Phase 4) — a Kanban of *work items* (tasks and goals) across their
  whole lifecycle: Queued through Merged or Stalled, spanning history. It answers "what is the
  state of everything I've asked for."
* **The office** (`--serve`, Phase 4) — a live, read-only view of *one run's* roles as 2D
  characters. It answers "who is working right now," but only by watching.

The cockpit answers a third, different question — "what is my team, and what is each member of
it doing right now" — and unlike the other two, it can be **acted on**:

* **One column per configured role**, in pipeline order. This is not a fixed "4 columns" — it
  is however many roles the project's `agents:` list actually defines, in the same order
  `teams.py`'s `phases_of()` already groups them (ARCHITECTURE.md §21.2). A project with five roles gets five
  columns; the default pipeline's four roles get four.
* **One card per agent assigned to that role.** A role with several agents (an ensemble, or a
  quorum of verifiers) shows several cards stacked in that column — the same model
  `design.html` already draws as a phase's agent list, just rendered as a vertical column
  instead of a horizontal box.
* **A "+" button per column** opens the exact same add-agent flow Phase 7 already built:
  pick a provider and a model (or a ladder) from the catalog, and it is validated and written
  into `orchestrator.yaml` through `teams.py`'s `normalize_team` / `validate_team` /
  `write_team` — unchanged. The cockpit does not reimplement team editing; it relocates the
  existing editor from a standalone page into the live dashboard.

### 10.4 Starting and watching a goal from the cockpit

A goal is typed into the dashboard rather than a terminal, and the pipeline's already-fixed
execution order (§2's conceptual hierarchy: Phase = one role, in sequence) is what makes
"left to right" a true statement about the columns rather than just a metaphor — the leftmost
configured role genuinely runs first.

As execution proceeds, each agent's card moves through the same status vocabulary
`status.py` already derives for a whole session (`pending` → `analyzed` → … → `completed` /
`blocked` / `failed`), applied **per agent within the running session** instead of only to the
session as a whole. The signal already exists to drive this: `agent_started` (added in Phase 4
specifically so the office could show who is working, not just who had finished) and
`agent_result`, tailed from `events.jsonl` exactly as `/api/activity` already tails them. The
cockpit's per-column status is the office's data source, redrawn.

**Left open, deliberately, rather than guessed:** does the prompt attach specifically to the
first column (so a person could, in principle, type into the *implementer* column instead and
skip research), or is it a single "start a goal" action that happens to animate column one
first because that is where the pipeline begins? These read almost the same to a user and very
differently to whoever builds it, and §10.10 carries it as an open question rather than an
assumption.

### 10.5 The live agent view: an embedded terminal, not a parsed transcript

`launcher.py` already does most of the hard part. `run_agent_cli(..., visible=True)` already
spawns the agent's CLI in its own terminal (Windows Terminal, console, or the Antigravity IDE
bridge), streams its live stdout/stderr to that window, and waits synchronously while capturing
a status file — that is documented behavior today, not aspiration. What it does not do is let
anything *else* see that stream: the terminal is a separate OS window, and nothing relays its
contents anywhere.

The gap between what exists and what §10.1's destination needs is exactly one capability:
**capture that same stream and relay it live to a UI panel instead of (or in addition to)
popping a separate window.** That is a pseudo-terminal (pty) capture problem, and it is a well
worn one — it is the same mechanism behind VS Code's integrated terminal and every web-based
SSH client: run the child process attached to a pty instead of a plain pipe, and stream its raw
bytes to a terminal-emulator widget (`xterm.js` is the standard choice) over a socket.

Two things worth being precise about before this is built:

* **It is not a thought-process parser.** Per §10.2, this deliberately does not attempt to
  extract or structure an agent's reasoning — it shows the same raw terminal a person would see
  running the CLI themselves, verdict parsing and all. That is simpler, more honest about what
  it actually shows, and does not need updating every time a provider changes its output format.
* **The platform matters.** This project develops on Windows, and Windows' pty API (ConPTY) is
  a different thing from a Unix pty. Whichever desktop shell §10.7 picks already has a mature
  library for this (`node-pty` for Electron, `portable-pty` for Tauri) — this is an integration
  task, not a from-scratch one, but it is not zero-cost either.

### 10.6 The daemon: what has to become persistent, and what must not change

A dashboard that can *start* work needs something that is always running to start it — the
CLI's whole model, "runs to completion and exits," has nothing to be clicked into. Call this
new, persistent, local process **the daemon**. It needs to:

* launch a session or a delegated goal on command, instead of only when invoked from a
  terminal;
* supervise several runs at once — not new in principle (`--parallel N` already runs several
  Sessions concurrently within one goal; the daemon needs to hold several *goals*, and possibly
  several *projects*, open at once);
* relay `events.jsonl` and pty output live over a socket, instead of only being polled;
* accept control commands over that same connection — start, cancel, approve, reject, deliver —
  where today those are separate CLI invocations;
* keep serving everything `serve.py` already serves as plain reads (`/api/board`,
  `/api/goals`, `/api/deliveries`, `/api/team`).

**What must not change, because nothing about wanting a nicer UI changes why these rules
exist:**

| Invariant | Still holds because |
| --- | --- |
| Never touch the checkout / never auto-merge / never auto-push | Enforced in `workspace.py` and `delivery.py` themselves, regardless of who calls them — a daemon calling `deliver()` is still a person's click, not a scheduler's decision |
| Facts stored, status derived | `store.py`, `goals.py`, `approvals.py`, `status.py` do not change; the daemon is a new *caller* of them, not a new place facts live |
| Bounded by construction | `budget.py`'s ceilings do not become optional because a click started the run instead of a terminal |
| Zero provider API keys | Unaffected — agents are still local CLI subprocesses |
| Loopback only | The daemon binds `127.0.0.1`, same as `serve.py` today; nothing here proposes a remote-control surface |

**What is revised, precisely:** invariant 12 said watching is not steering, and the server could
therefore never write. Its *purpose* — a decision must always be a deliberate human action, never
something a schedule or an external signal triggered — is not being given up. What changes is
*where* that deliberate action can be taken from: a click in a single-user, locally-running app
is exactly as deliberate as typing a command, provided the daemon only ever acts on something a
human triggered through its own UI, in front of it, and never on a timer, a webhook, or a
message from anything it did not itself present a button for.

That distinction has a concrete consequence worth flagging now rather than after the fact: a
loopback-bound HTTP server that can *start work* is a bigger target than one that can only read,
because any web page open in the same browser can already reach `127.0.0.1` with a fetch call.
Phase 8 (§10.9) has to design against that specifically — at minimum, a per-launch local token
the UI must present, not just "it's on localhost so it's fine."

### 10.7 The desktop shell

"Open a project folder," "new window," native menus — none of this exists in any form today,
and it is genuinely a different kind of thing from everything built so far: an installable
desktop application, not a page a browser happens to render.

The natural split is to keep the daemon exactly what it already mostly is — Python, calling
into `graph.py` / `scheduler.py` / `workspace.py` unchanged — and let only the *shell* be new,
in whatever language that shell's toolkit requires. Two real candidates, both able to embed a
terminal (§10.5) and manage native windows:

| | Electron | Tauri |
| --- | --- | --- |
| Language | JavaScript/TypeScript throughout | Rust for the shell, JS for the UI |
| Ships | Its own Chromium + Node (bigger install, more memory) | The OS's own webview (smaller install) |
| Terminal embedding | `node-pty` — mature, widely used | `portable-pty` — solid, younger ecosystem |
| New skill required of this project | None beyond web frontend | Rust, which nothing here currently uses |

Neither choice touches the engine. This is deliberately left open (§10.10) rather than decided
here — it is exactly the kind of decision that should follow, not precede, proving the
interaction model in a browser tab first (§10.9's phasing is built around that).

### 10.8 What this reuses untouched — read this before writing any code

The point of a roadmap section like this one is that a future conversation should not have to
re-derive what already exists. Nothing below needs to be rebuilt for the cockpit or the daemon:

| Already built | Stays exactly as it is | Becomes |
| --- | --- | --- |
| `orchestrator.yaml`, `config.py` | The team model: roles, agents, ensembles, ladders | Still the single source of truth |
| `teams.py` | Validation, templates, the surgical write-back | What the cockpit's "+" button calls |
| `graph.py`, `scheduler.py` | The actual execution engine | What the daemon calls to start work, instead of only `__main__.py` |
| `workspace.py`, `delivery.py` | Isolation, retention, and the only code that ever pushes | Unchanged; still the sole enforcement point for invariants 3 and 13 |
| `store.py`, `goals.py`, `approvals.py` | Durable, append-only facts | Unchanged; still what the daemon reads from and writes to |
| `status.py`, `board.py` | Derived status and the task/goal Kanban projection | Unchanged; `--board` keeps working exactly as today, alongside the new cockpit view |
| `serve.py` | The read-only HTTP layer | The seed the daemon's HTTP+socket layer grows from |
| `office.html` | The 2D live view, and the `agent_started` event stream it proved was needed | Its data source is what feeds the cockpit's column status (§10.4); the page itself may stay as the dependency-free fallback (§10.10) |
| `launcher.py` | Visible-terminal execution, already streaming live output to a window | Its pty-adjacent groundwork is what §10.5's embedded terminal extends |

### 10.9 A phased path, continuing the numbering in §4

*All four are now built; §4 carries what each one actually became, and ARCHITECTURE.md §22-§25
describes them as they are. What follows is the plan as it was written, kept because the order
it argues for is the part worth remembering.*

Same rule as Phases 0–7: each step must leave the system working and the suite green, and each
is small enough to direct, review, and stop at. The order is deliberate — it defers the most
expensive, least reversible decision (§10.7) to last, and proves the riskiest new capability
(a daemon that can be told to start work) while it can still be tested from a browser tab,
exactly as Phases 4 and 7 were.

**Phase 8 — The daemon shell, no new UI yet.** Wrap the existing engine in a persistent local
process exposing a start/cancel/approve/deliver control API alongside everything `serve.py`
already reads, and a socket that relays `events.jsonl` live instead of only being polled.
Design its local-token safeguard (§10.6) here, not later. Prove it with `office.html` pointed
at the socket instead of its current poll loop, and a script that starts a run through the new
endpoint instead of a terminal invocation.

**Phase 9 — The cockpit, still in a browser tab.** A new page — role columns instead of the
office's canvas — served by the daemon from Phase 8. "+" calls `teams.py` exactly as `design.html`
already does. Still opened the same way `--serve` is opened today; the only thing that's new is
that it can start and steer, because Phase 8 gave it something to call.

**Phase 10 — The embedded live terminal.** Pty capture of each agent subprocess (§10.5),
relayed over the daemon's socket, rendered with a terminal-emulator widget inside a card's
detail view.

**Phase 11 — The desktop shell.** Wrap the by-then-proven browser app in Electron or Tauri
(§10.7), add real "Open Folder" / "New Window" chrome, and package it as an installable app.
Last on purpose: it is the part with the least engineering risk and the most glue, and doing it
last means it wraps something already proven rather than something imagined.

### 10.10 Open questions this revision added

*Building Phases 8-11 answered four of these. The answers are recorded here rather than
quietly absorbed, because an open question that was settled by an implementation decision is
worth being able to find later.*

1. ~~**Where a started goal actually attaches**~~ (§10.4) — **answered: goal-level.** The
   startbar starts a *goal*, and column one animates first because that is where the pipeline
   begins. Attaching a prompt to a specific column would mean letting a person skip a
   configured role from the UI, which no other surface allows and which `graph.py` has no way
   to express.
2. ~~**Concurrency scope**~~ — **answered: one daemon per project.** `project_root` is fixed
   for the life of a daemon, and the desktop shell starts one per opened folder on its own free
   port. Two windows on the same project get two daemons, and nothing shared between them; the
   stores on disk remain the only coordination, exactly as they are between two terminals.
   *Still open beneath it:* whether a single daemon supervising several projects would be worth
   the coupling. Nothing yet needs it.
3. ~~**Electron vs. Tauri**~~ (§10.7) — **answered: Electron.** The deciding factor was the one
   §10.5 predicted: the shell has to embed a terminal, and `node-pty` is mature on Windows,
   where this project is developed. Tauri would ship smaller but adds Rust to a project that
   uses none, to save download size for a tool that runs beside a checkout. `desktop/` is the
   entire commitment, so this is cheap to revisit.
4. ~~**Does the cockpit retire the office?**~~ — **answered: both exist.** The office kept the
   property Decision 2 valued it for, and gained the stream: it is still one self-contained
   file with no dependencies, still works offline, and now consumes `/api/stream` when a daemon
   serves it and falls back to polling when `--serve` does. One page, both servers.
5. ~~**The daemon's local-token safeguard**~~ (§10.6) — **answered**, and it is three mechanisms
   rather than one, because any single one of them fails alone: a per-launch token substituted
   into the served page (never a URL), an `Origin` check that refuses a foreign origin before
   reading a body, and requiring the token in a header so a browser must preflight. See
   ARCHITECTURE.md §22.1.
6. **How much of `launcher.py`'s visible-terminal path survives.** *Still open.* Nothing was
   removed: `--visible-terminals` opens a real OS window exactly as before, and the embedded
   panel is a separate capture on the headless path. Whether both are worth keeping once the
   shell owns a real pty is a question for after Phase 11's pty work, not before it.

### 10.11 What is still open after Phase 11

* ~~**A real pseudo-terminal.**~~ **Built**, on the Python side: `run_captured_pty`
  (`pywinpty`/ConPTY on Windows, the standard library on POSIX) relays OpenCode's actual
  `attach` TUI into the cockpit's panel with no Antigravity IDE bridge needed, verified with a
  real session. `node-pty` in the Electron shell itself remains open (§10.7's original
  reasoning) as a way to give a *native window* the same treatment, not as a gap in the
  browser-tab cockpit.
* **Native TUI for Claude Code and Antigravity.** *Not supported, deliberately.* OpenCode is
  the only agent with a supported programmatic native-TUI integration (its `serve` + `attach`
  API); Claude Code and Antigravity have interactive CLIs but no supported way to deliver a
  prompt to a running interactive session and detect that its turn finished, and driving one
  by keystrokes or screen-scraping is ruled out. They run headless, visibly when configured,
  and `agent_execution_mode: native_tui` refuses them at preflight (ARCHITECTURE.md §14). Revisit
  only if either ships a supported control mechanism.
* ~~**Surviving the orchestrator's own death (Package B).**~~ **Closed by Package C** on
  Windows (ARCHITECTURE.md §14.5, §30.2c): every agent process is owned by a kill-on-close job
  object, so a hard-killed orchestrator no longer orphans its agents (measured live, twice); the
  execution in flight at a kill is recorded as an attempt with unknown spend rather than
  vanishing; the wall-clock budget continues across resume. Still open: POSIX ownership, and
  the IDE-owned tab (and, in native mode, its `attach` TUI) that a hard kill leaves behind.
* **A live bridge run — done (Package C).** A full workflow ran through real integrated
  terminals, OpenCode in its native TUI in an IDE tab, and left nothing running. The bridge's
  port-takeover fix is installed; activating it needs an IDE window reload.
* **Configuring the team from the cockpit — done (Package C).** Reassigning a role's agent and
  adding or removing an agent now take effect in the running daemon, persist, and survive a
  restart (ARCHITECTURE.md §22.5).
* **A packaged app that carries its own Python.** Today `desktop/` runs the interpreter beside
  a checkout. Shipping an installer to someone who does not have the repository is a different
  problem, and not one this phase claimed to solve.
* ~~**The planning agent's memory** (§8.2)~~ — **built**, and it was the largest hollow clause
  in the system. `memory.py` is a projection over the goal, run and approval stores, bounded
  and summarised into the decomposer's prompt, and printable with `--memory`. The revision left
  it untouched on purpose: a cockpit is a way of *watching* a body work, and this was the body
  learning. See ARCHITECTURE.md §27.
