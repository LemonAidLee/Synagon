# Synagon

Synagon is a multi-agent orchestrator built on [LangGraph](https://github.com/langchain-ai/langgraph).
It drives **local command-line AI agents** — Antigravity, Claude Code, OpenCode — through a
research → plan → implement → verify loop, with automatic repair when verification fails.

It uses no provider API keys. Every agent runs as a local subprocess against the CLI you have
already authenticated.

> **There is an app now.** `python -m orchestrator --daemon` opens a **cockpit**: one column
> per role, one card per agent, live as it runs. Type a goal into it, watch it flow left to
> right through the team, click an agent to read its terminal, approve a gate and deliver the
> branch without leaving the page. An **Explorer** browses the project *and every run's
> worktree*, so "what did the agent actually write?" is one click rather than a `cd`; `Ctrl K`
> opens a command palette and `Ctrl P` jumps to a file. `desktop/` wraps the same thing as a
> real desktop window, opened per project like an IDE. See
> [ARCHITECTURE.md](ARCHITECTURE.md) §22–§26, which describes only what exists today.

```
  GOAL ──▶ decomposer ──▶ TASK ──▶ session ──▶ branch          one session per task,
                       ├▶ TASK ──▶ session ──▶ branch          in dependency order,
                       └▶ TASK ──▶ session ──▶ branch          optionally in parallel

  session:  context ▶ preflight ▶ researcher ▶ planner ▶ implementer ▶ gate ▶ verifier ▶ finalize
                                                             ▲                    │
                                                             └─────── repair ◀────┘
```

---

## Quick start

```powershell
# 1. Install
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt

# 2. Check the environment before spending anything on it
python -m orchestrator --doctor

# 3. Run a task
python -m orchestrator "Add a --verbose flag to the CLI and cover it with tests"

# ...or drive the whole thing from the cockpit
python -m orchestrator --daemon
```

The daemon prints a URL and opens it. Type a goal into the bar at the top and the columns light
up left to right as the pipeline runs. It binds `127.0.0.1` and mints a token per launch, which
it hands only to the page it served — nothing else on your machine can reach it.

For a real window instead of a browser tab:

```powershell
cd desktop
npm install
npm start
```

The agents do **not** edit your checkout. Each run gets its own git worktree on its own
branch; when the run finishes, the work is committed to that branch and the worktree is
removed. What you review at the end is a diff:

```powershell
git diff master...orchestrator/run/<run_id>
git merge orchestrator/run/<run_id>      # merging is always your decision
```

---

## The command line

| Command | What it does |
| --- | --- |
| `python -m orchestrator "<task>"` | Run the pipeline on a task. |
| `--doctor` | Probe every configured agent (binary, model, role) and exit. |
| `--list-models` | Print the configured model catalog. |
| `--runs [N]` | List recorded runs, newest first. |
| `--show-run <id>` | Print one run: status, events, verdicts, tokens. Accepts a unique prefix or `latest`. |
| `--resume <id>` | Continue a run that stopped. Completed phases are replayed from its event log, not paid for again. |
| `--stats [N] [--json]` | Analyze every recorded run: pass rates by agent/model/role, what a pass costs against a failure, whether repairs and ensembles earn their price. |
| `--prune-runs [--older-than 30d] [--merged-older-than 1d] [--keep-failed] [--dry-run]` | Delete old run branches and leftover worktrees. A branch whose pull request merged answers to the shorter threshold — its work is already in the base branch. Always shows the plan and asks first. |
| `--archive-runs [--matching TEXT] [--dry-run]` | Move runs in which no agent was really invoked out of the run store, so `--stats`, the board and the planner's memory stop counting them. Nothing is deleted. |
| `--archived` / `--restore-runs [id…]` | List what is archived and why, or move it back into the store. |
| `--memory [N] [--json]` | What this repository has taught the planner: which files sibling tasks fought over, where work lands, what a task cost, what you rejected and why. Add `--plan-only` to print the exact block the decomposer's prompt will carry. |
| `"<goal>" --plan-only [--json]` | Break a goal into a task graph — dependencies, acceptance criteria, and the order the tasks could run in — and stop. Implements nothing. |
| `"<goal>" --delegate [--parallel N]` | Decompose the goal, then run each task as its own session on its own branch, in dependency order. |
| `--goals [N]` | List delegated goals, newest first. |
| `--show-goal <id> [--json]` | One goal: its tasks, their states, branches, and collisions. |
| `--resume-goal <id>` | Continue a goal that stopped. Delivered tasks are replayed, not re-run. |
| `--board [--json]` | Every piece of work as a card in a column, derived from what happened. |
| `--serve [PORT]` | Open the board and the live office view at `http://127.0.0.1:8730`. Read-only. |
| `--review [PORT]` | The same pages, in the one mode that can answer a gate and deliver a card. It cannot start work or edit the team. |
| `--daemon [PORT]` | Run the persistent daemon and open the **cockpit** at `http://127.0.0.1:8740`: role columns, live agent cards, embedded terminals, a file Explorer over the project and every run's worktree — and the one surface that can start, cancel, approve and deliver work. |
| `--approvals [all]` | What is waiting for you at an approval gate. |
| `--approve <id>` / `--reject <id>` | Answer a gate, with an optional `--note`. |
| `--deliver <card>` | Push one card's branch and open a pull request for it. Only work the board calls *Ready to Merge*. |
| `--deliveries [--json]` | What has been published, and the CI and review state recorded for it. |
| `--refresh-deliveries [N]` | Re-read CI and review state from the forge and record what it says. |
| `--teams` | The team this project is configured with, and the templates available. |
| `--apply-team <name>` | Replace the team with a template — validated first, previous version backed up. |
| `--design [PORT]` | Compose the team visually at `http://127.0.0.1:8730/design`. |
| `--no-browser` | With `--serve`, `--design`, `--review` or `--daemon`: do not open a browser. |

Per-run overrides: `--acceptance-command "pytest -q"` / `--no-acceptance`,
`--max-repair-attempts`, `--max-total-tokens`, `--max-duration-seconds`,
`--isolate {auto,worktree,none}` / `--no-isolate`, `--keep-worktree`, `--no-preflight`,
`--no-run-store`, `--visible-terminals`, `--token-diagnostics`.

---

## Configuration

Everything lives in [`orchestrator.yaml`](orchestrator.yaml), which is heavily commented.
The parts you are most likely to touch:

```yaml
agents:                          # who runs, in what order, as what
  - {agent: claude,      model: sonnet,                role: researcher}
  - {agent: claude,      model: sonnet,                role: planner}
  - {agent: opencode,    model: opencode/big-pickle,   role: implementer}
  - {agent: claude,      model: sonnet,                role: verifier}
                                 # either field may be a list - see Escalation ladders

max_repair_attempts: 2           # how many repair passes a failure may buy

budget:                          # what work may cost (0 = unlimited)
  max_total_tokens: 0            # per session
  max_duration_seconds: 0
  goal_max_total_tokens: 0       # across every task of one delegated goal
  goal_max_duration_seconds: 0

delegation:
  max_parallel: 1                # how many tasks may run at once
  stop_on_failure: false

approval:                        # ask a person before the team acts
  gates:
    after_decomposition: false
    before_task: false
    before_merge: false
  auto_approve: false

delivery:                        # nothing reaches a remote until this is on
  enabled: false
  remote: origin
  draft: true                    # open pull requests as drafts
  on_approve: true               # approving a before_merge gate publishes the work

workspace:
  isolation: auto                # auto | worktree (required) | none
  commit_on_finish: true
  keep_worktree: false           # the branch is the artifact; the checkout is scaffolding

verification:
  consensus: unanimous           # unanimous | majority | any
  acceptance:
    command: "pytest -q"         # run by the orchestrator itself, before the verifier
    required: true               # a red gate overrules a verifier's PASS
```

**The board and the office.** `--board` shows every task and session as a card in a column —
Queued, Working, Needs You, In Review, Ready to Merge, Merged, Stalled — derived from recorded
facts, never hand-maintained. `--serve` opens the same board in a browser alongside *the office*: a
page where each role is a small character at a desk, working and handing off as the run
proceeds. Under `--parallel N` it draws one row of desks per task, so what you watch is the
team rather than whichever session wrote last. It is a reader — loopback-only, read-only, no
CDN, works offline. `--review` opens the same pages in the one mode that can *answer* what it
shows: approve a gate, deliver a Ready to Merge card, and nothing else.

**Approval gates.** Ask to be consulted before the team acts: after a decomposition, before each
task, or before delivered work counts as ready. A gate records a pending approval and stops at a
clean boundary rather than blocking on stdin; `--approvals`, `--approve`, then `--resume-goal`
carries on. `auto_approve` answers gates automatically and records that it did — a guardrail you
cannot tell was on is not a guardrail.

**Delegation.** `--delegate` turns one goal into a team: the decomposer breaks it into focused
tasks, and each task runs the whole pipeline in its own worktree on its own branch. Dependent
tasks start from their dependency's branch, so they can see the work they build on. `--parallel N`
runs independent tasks at once. Sibling tasks that changed the same file are reported as a
collision and nothing is merged — reconciling them is yours.

**Delivery, on your word.** Everything a run produces stays on a local branch. Turn
`delivery.enabled` on and pressing *Ready to Merge* — `--deliver <card>`, or approving a
`before_merge` gate — pushes the branch and opens a draft pull request through your own `gh`.
Nothing is ever pushed automatically: no verdict, no schedule, no finished wave crosses the
network. Afterwards `--refresh-deliveries` reads CI and review state back, and the board moves
the card: a red check or a change request is *Needs You*, a merged pull request is *Merged*.
Those facts are recorded rather than live, so the board stays instant and works offline, and
every card says how fresh its facts are.

**Designing the team.** `orchestrator.yaml` already *is* the team model, so `--design` opens a
node editor over it: phases left to right, agents inside a phase running in parallel, a model
ladder per agent, consensus and repair attempts beside them. Start from a template — `solo`,
`fast`, `balanced`, `careful` — in the browser or with `--apply-team careful`. A save rewrites
only the three things it owns and leaves every comment in the file alone, validates the result
before it lands, and keeps the previous version as a `.bak-` copy. Each dropdown carries the
evidence for the choice beside it — *this verifier has passed 41% of the time over 17 runs and
costs 12k tokens each* — read from the same `--stats` projection, so composing a team is an
evidence-based act rather than a preference.

The three modes of that server are separate, never a ladder: `--serve` reads, `--design` writes
only the team file, `--review` only decides. `--design` cannot approve, `--review` cannot edit
the team, and asking for both at once is refused. A stray click on a page you opened to watch a
run can never become a decision.

**The planner remembers.** Every goal used to be decomposed *cold*. Now the decomposer's prompt
also carries what previous goals recorded here: which files sibling tasks fought over, where
work in this repository actually lands, what a task cost, and what you rejected at a gate and
why — so it stops proposing the pair that collided last week. It holds no facts of its own: it
is a projection over the goal, run and approval stores, bounded by how far back it reads and by
a character budget on what reaches the prompt. `--memory` prints exactly what the planner will
be told, and `planning.memory.enabled: false` returns the cold decomposition.

**It survives a flaky agent.** A local CLI agent is a subprocess over a network service, and
it fails the way those fail: intermittently, and by returning *nothing* rather than by raising.
An execution is therefore attempted up to three times with a widening gap, stepping down the
model ladder as it goes, and an exception is retried the same way an empty reply is. However
many attempts it takes, the phase still produces one result — a retry is not an ensemble, and
consensus and `--stats` both count results — with the failed attempts recorded as their own
events so the cost is auditable. Set `execution.retry.attempts: 1` to turn it off entirely.

**The acceptance gate.** Set `verification.acceptance.command` and the orchestrator runs your
tests *itself*, in the run's worktree, before the verifier sees anything. The verifier is then
handed the real output instead of being asked to produce it, a failing repair gets the actual
error, and a verifier that claims PASS over a red suite is overruled. The command is read from
your checkout, never from the worktree the agents can edit — so an agent cannot rewrite the
command that judges it.

Two more capabilities worth knowing about:

**Ensembles.** List several agents with the same role consecutively and they run in parallel
as one phase. Several verifiers form a quorum resolved by `verification.consensus`; a single
dead ensemble member is a warning, not the end of the run.

**Escalation ladders.** Give a role a *list* instead of one value. Rung 0 runs the first
attempt, rung N runs escalation step N — so a failure escalates rather than re-running what
just failed. `model` may be a list, **and so may `agent`**, which is what lets a role fall
back across providers when the provider itself is what broke:

```yaml
  - agent: opencode                                    # one agent, three models
    model: [opencode/big-pickle, anthropic/claude-sonnet-4-5, opencode/gpt-5.1-codex]
    role: implementer

  - agent: [antigravity, claude]                       # across providers
    model: [gemini-3.8-flash-high, sonnet]
    role: researcher
```

When both are lists they are paired rung by rung, so they must be the same length — a model
id only means something next to the agent whose catalog defines it, and clamping the shorter
list would quietly hand one to the wrong provider. Preflight probes every rung, not just the
first: a fallback is only ever reached on a bad day, which is a bad day to find out its
binary is missing.

---

## Prerequisites

- **Python 3.11+** and **git** on `PATH`.
- At least one locally-authenticated agent CLI: [Claude Code](https://claude.com/product/claude-code),
  [OpenCode](https://opencode.ai), or Antigravity's `agy`. `--doctor` probes whichever ones your
  `orchestrator.yaml` names and tells you which are missing — install and `<cli> auth login` (or
  equivalent) before your first run, since Synagon reads no provider API key of its own.
- **Node.js** only if you want the desktop shell (`desktop/`) or are rebuilding the terminal
  bridge extension; the CLI and the browser cockpit need neither.
- **`gh`** (GitHub CLI), already authenticated, only if you turn on `delivery.enabled` — nothing
  pushes or opens a pull request without it.

## Terminal bridge (Antigravity IDE)

Only needed for `terminal_type: antigravity_integrated` — a visible or native-TUI run that opens
its terminal as a tab inside the Antigravity IDE instead of a separate console window. Every
other terminal type (`windows_terminal`, `console`, or headless with no visible terminal at all)
needs none of this.

```powershell
python tools/install_terminal_bridge.py
```

This packages `tools/antigravity_terminal_bridge/` into a VSIX, installs it into the running IDE
(`antigravity-ide --install-extension … --force`), and also copies it straight into
`~/.antigravity-ide/extensions/` so it survives IDE restarts. **Reinstalling the extension does
not update a window that already loaded it** — reload the IDE window (or restart it) afterwards
for the change to take effect. The bridge listens on `127.0.0.1:49182`; only one IDE window can
hold that port at a time, and if the window that owns it closes, another open window picks it up
within a few seconds rather than leaving the bridge unreachable.

## Execution modes

Every agent can run **headless** (no visible window; this is the default and what the offline
test suite always uses). Two things can be layered on top of that, and they are not the same
capability:

| Mode | What you see | Which agents |
| --- | --- | --- |
| Headless | Nothing — output is captured, not shown. | All. |
| Visible headless (`visible_terminals: true`) | The agent's own headless CLI output, streamed live into its own titled terminal window (or IDE tab, with the bridge). | All. |
| Native TUI (`agent_execution_mode: native_tui`, or `auto` when a surface exists) | The agent's actual interactive TUI, driven programmatically. | **OpenCode only** — it is the only CLI with a documented `serve`/`attach` API. Requesting this for Claude Code or Antigravity is refused at preflight, on purpose: there is no supported way to drive their interactive sessions, and this project does not screen-scrape or send keystrokes to fake it. |

`--visible-terminals` sets the second on the command line for one run; `execution.close_terminal_on_completion: false` (or `--keep-terminals`) leaves a finished terminal or native session open afterwards for inspection, bounded to 8 kept sessions, closable with `--close-sessions`.

## Troubleshooting

- **`--doctor` fails for an agent.** It probes the exact binary, model and role your config
  names; the failure message says which one. Fix the CLI's own auth/installation first — Synagon
  never retries past a preflight failure.
- **A run using `terminal_type: antigravity_integrated` fails immediately, before any agent
  runs.** Preflight checks the bridge's health before starting anything. Install or reinstall the
  bridge (above), and reload the IDE window — a stale, already-loaded copy is the usual cause.
- **A native-TUI run for Claude Code or Antigravity is refused at preflight.** This is not a bug;
  see the Execution modes table above. Switch that role to `headless` or `auto`, or move it to
  OpenCode.
- **A run seems to hang, then a retry message appears in the console.** That is
  `execution.retry` stepping down a model ladder after an empty or failed attempt — expected
  behavior for a flaky provider call, not a stuck process. Every attempt's cost is still recorded.
- **The daemon or `--serve` opens a blank/unreachable page.** Both bind `127.0.0.1` only; check
  nothing else already owns the port, and that you opened the URL the process itself printed (it
  carries a per-launch token the page needs).
- **Windows only:** OpenCode's native TUI uses `pywinpty`/ConPTY; if it is not installed, that one
  capability degrades to headless rather than failing the run (`terminals.pty_available()`).
- **An agent's every file operation is denied on a brand-new project directory** — OpenCode
  reports "The user rejected permission to use this specific tool call"; Claude Code's verifier
  correctly reports `BLOCKED` with "Human Action Required." Both CLIs trust a working directory
  only after a person (or an explicitly permission-bypassing invocation) has used it at least
  once; a fully headless first run has no terminal to answer that trust prompt, so every
  read/edit is silently denied — and Synagon does not, and should not, silently pass a
  permission-bypass flag on your behalf (that decision belongs to you, and to that CLI's own
  security model, not to this orchestrator). Establish trust once, per directory, before your
  first Synagon run against it:
  - **OpenCode:** `opencode run --auto "<anything>"` once by hand (or open the directory once in
    OpenCode's own TUI). Confirmed to persist across later headless runs.
  - **Claude Code:** open the directory once in Claude Code's interactive CLI and accept its
    workspace-trust prompt (or answer it yourself if you already know the risk, using its own
    `--permission-mode` / `--dangerously-skip-permissions` flags — outside Synagon, at your own
    judgment, never something this project sets for you).

  This is genuinely worth knowing before your first run against a new project: it looks
  identical to a broken pipeline (an implementer that changes nothing, over and over) and the
  honest failure it produces — a `BLOCKED` verdict asking for human action, or a retried-then-
  failed execution — is the system behaving exactly as designed (invariants 2 and 7), not a bug
  to chase.

## Known limitations

- **The built-in default (no `orchestrator.yaml` of your own) assigns the researcher role to
  `antigravity / gemini-3.8-flash-high`.** This project's own config moved that role to
  `claude / sonnet` after measuring that pairing return an empty response — `status: SUCCESS`,
  output budget spent entirely on thinking tokens — when asked to investigate a project, which is
  precisely the researcher's job (see CHANGELOG, "the researcher moved off Antigravity"). The
  fallback default was deliberately left as-is rather than changed to match, because the offline
  suite pins the fallback's exact shape across 15 tests in four files; changing it is a larger,
  riskier edit than this pass should make on its own judgment. If you have no `orchestrator.yaml`
  yet, either write one (see Configuration above) and give the researcher role to `claude` or
  `opencode`, or add an escalation ladder: `agent: [antigravity, claude]`, `model:
  [gemini-3.8-flash-high, sonnet]`.
- **A project's own `orchestrator.yaml` does not merge with the built-in default — it fully
  replaces it.** Supplying one with only `agents:` overridden will fail validation (missing
  `roles`), and supplying a `models:` block with only one provider drops the others' catalogs.
  Copy the full file (or start from `--apply-team <template>`) rather than writing a partial
  override.
- **Native TUI is OpenCode-only**, by design — see Execution modes above. Claude Code and
  Antigravity run headless or visible-headless; revisit only if either ships a supported way to
  drive an interactive session programmatically.
- **Process-tree kill-on-close is proven on Windows**, via job objects (`process_jobs.py`);
  POSIX ownership of the same guarantee is not yet built.
- **The desktop shell runs beside a Python checkout** — it spawns `python -m orchestrator
  --daemon` from the project you open, and does not (yet) bundle its own Python interpreter for
  someone without this repository.
- **Collisions between parallel tasks are detected and reported, never auto-resolved.**
  Reconciling two tasks that touched the same file is always a person's decision.
- **A run's git history predates the project's current name.** Some historical commits and one
  mocked test path reference this project's former working name; see `CHANGELOG.md` (Package D)
  for why that is a recorded fact rather than something to scrub.

---

## What a run leaves behind

```
.orchestrator/
  runs/<run_id>/run.json        # metadata: task, status, verdict, settings
  runs/<run_id>/events.jsonl    # append-only log: every agent result, every verdict
  goals/<goal_id>/goal.json     # a delegated goal: its plan and what each task did
  goals/<goal_id>/events.jsonl  # append-only log: task started, finished, collisions
  approvals/<id>.json           # one approval gate: what was asked, and who answered it
  delivery/<key>.json           # one delivered branch: its pull request, checks, and review
  worktrees/<run_id>/           # the run's checkout, removed once its work is committed
```

Plus one branch per run, `orchestrator/run/<run_id>`, holding the work as a commit. That is
deliberate — the branch *is* the run's output. Sweep old ones when you no longer need them:

```powershell
python -m orchestrator --prune-runs --older-than 30d --keep-failed --dry-run
```

The run store is the dataset `--stats`, the board and the planner's memory all read, so a run
recorded in it that never actually happened is not clutter — it is a wrong answer everywhere
downstream, reported confidently. `--archive-runs` moves those out (a run counts as one when
**no** execution reported token usage *and* none took real time, which no live agent
invocation can both do), into `.orchestrator/archive/runs/` with a note saying why:

```powershell
python -m orchestrator --archive-runs --dry-run   # the plan, and nothing else
python -m orchestrator --archived                 # what is archived, and why
python -m orchestrator --restore-runs             # all of it back
```

`.orchestrator/` is git-ignored. Nothing in it is required for the orchestrator to run.

---

## Design rules

These are the invariants the code is built around; [ARCHITECTURE.md](ARCHITECTURE.md)
explains each one in full.

1. **Facts are stored; status is derived.** Nodes record what happened. Exactly one place
   (`finalize_node`) decides what it means, from those facts.
2. **No blind trust.** The implementer's claim of success is a claim, and so is the verifier's.
   An ambiguous verdict is `UNKNOWN` — never `PASS` — and a configured acceptance command is
   run by the orchestrator itself, overruling a `PASS` when it fails.
3. **Never touch the user's checkout, and never auto-push.** Agents write a worktree on a run
   branch, and nothing is ever merged back automatically — however many branches a goal
   produces. Nothing is published either: a branch reaches a remote only when the project
   opted in *and* a person acted. Ambiguity is escalated, never guessed: a dependency that
   will not merge, or two tasks that changed the same file, stops and names a person.
4. **Never discard uncommitted work.** A dirty worktree is never removed, pruned, or forced.
5. **Never guess a number.** Token counts come only from what an agent actually reported;
   unavailable is displayed as unavailable and contributes zero.
6. **Degrade, never fail.** No git, no run store, no skills directory — the run continues
   with a warning. Observability and isolation are features, not preconditions.
7. **Every decision is a deliberate human act.** Nothing starts a run, answers a gate, or
   publishes work except a person doing so on purpose — never a timer, a webhook, or an
   external signal. The board and the office remain pure projections that change nothing. The
   cockpit *can* act, and what it may do is a closed list of verbs, each reachable only from a
   page the daemon itself served, behind a per-launch token and a same-origin check.
8. **Bounded by construction.** Repair attempts, tokens, and wall time all have ceilings.
   An infinite loop is not representable — and a click cannot start more work than the daemon's
   own ceiling allows.

---

## Testing

```powershell
.venv\Scripts\python -m unittest discover -s tests -t . -q
```

Agent subprocesses are mocked throughout, so the suite runs offline in seconds and never
launches a real CLI. Git is not mocked where the point is that git works: the delegation tests
merge real branches, and the delivery tests push into a real bare repository on disk. No test
ever contacts a forge.

Nor is the network mocked where the point is that a refusal is real: the daemon tests start a
server on a real port and make real requests, including the ones that must be refused for want
of a token and for a foreign `Origin`. "This cannot be driven by a page you did not open" is a
claim worth testing rather than asserting.

---

## Repository layout

```
orchestrator/
  __main__.py     CLI entry point
  graph.py        LangGraph topology: phases, ensembles, repair loop, finalize
  config.py       orchestrator.yaml schema, validation, defaults
  context.py      safe project context collection (redacts secrets)
  preflight.py    probes every agent before any of them launches
  workspace.py    git worktree isolation, commit, and retention
  prune.py        the retention sweep behind --prune-runs
  archive.py      the run store's own retention: moving a run out of the dataset, reversibly
  budget.py       token and time ceilings
  acceptance.py   the objective gate: the project's own check, run by the orchestrator
  decompose.py    turning a goal into a validated task graph
  scheduler.py    running a goal's tasks: waves, parallelism, dependencies, collisions
  goals.py        the goal store: what was delegated, and what each task did
  board.py        the board projection: cards and columns, derived from facts
  approvals.py    approval gates: asking a person, and recording the answer
  delivery.py     the only module that crosses the network: push, pull request, CI facts
  teams.py        team templates, and the surgical rewrite of orchestrator.yaml
  memory.py       what this repository has taught the planner, projected from the stores
  serve.py        the local server: --serve reads, --design writes the team, --review decides
  daemon.py       the persistent process behind --daemon: control verbs, jobs, the live stream
  cockpit.py      the role-column projection: what each agent is doing, derived from events
  terminals.py    live agent output: bounded ring buffers, and the captured runner
  explorer.py     the read-only file tree: confinement, bounded reads, git overlay, name search
  web/            the office, the team editor, the cockpit: self-contained pages, no dependencies
  status.py       derived status: the one place a run's meaning is computed
  store.py        durable run store (run.json + events.jsonl)
  resume.py       replay a stored run and continue it
  stats.py        aggregate analysis across stored runs
  metrics.py      token accounting and the final summary table
  tracer.py       structured, observable progress output
  types.py        AgentResult, TokenUsage, VerificationRecord
  launcher.py     process/terminal launching (headless, native TUI, visible windows)
  agents/         one module per CLI provider, plus verdict parsing
  prompts/        role prompt templates
  skills/         passive discovery of modular agent skills
desktop/          the Electron shell: spawns a daemon per project window, loads its cockpit
tools/            the Antigravity terminal-bridge extension, and daemon_client.py
tests/            offline unit and integration tests
```
