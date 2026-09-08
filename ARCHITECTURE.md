# AI Orchestrator — Architecture

A multi-agent orchestration framework built on **LangGraph**, coordinating local command-line
AI agents (Antigravity, Claude Code, OpenCode) through non-interactive subprocess calls, with
no provider API keys.

This document describes the system **as it is**, not as it was staged and not as it is
intended to become. It is the input other agents read before planning changes to this
repository, so it is kept current with the code; when the two disagree, the code is right and
this file is a defect. Where the project is heading — parallel workers, goal decomposition, a
derived board, approval gates — lives in [ROADMAP.md](ROADMAP.md), and moves into this file
only once it is built.

**Reading order.** §1 is the contract — the invariants everything else exists to preserve.
§2–§4 are the model and the runtime of one **session**. §5–§15 are one feature each. §16–§17
are the layer above a session: decomposing a goal and delegating its tasks. §18–§19 are how a
person watches and steers that work, §20 is how work leaves the machine, and §21 is how the
team itself is composed. §22-§26 are the app that drives all of it: the daemon, the cockpit,
the live terminal, the desktop shell, and the Explorer. §27 is the map.

---

## 1. Invariants

Every one of these is load-bearing. Breaking one is a design change, not a refactor.

| # | Invariant | Where it lives |
| --- | --- | --- |
| 1 | **Facts are stored; status is derived.** Nodes record what happened. Exactly one node (`finalize_node`) decides what it means, by calling a pure function over those facts. | `status.py`, `graph.py` |
| 2 | **No blind trust.** The implementer's claim of success is a claim, and so is the verifier's. An ambiguous verdict is `UNKNOWN`, never `PASS`; and where a project supplies an acceptance command, the orchestrator runs it *itself* and a red result overrules a verifier's `PASS`. | `agents/verifier.py`, `acceptance.py` |
| 3 | **Never touch the user's checkout.** Agents execute in a git worktree on a run branch. Results are never merged back automatically, however many branches a goal produces. A task's *dependencies* are merged forward into its fresh worktree, which is the opposite direction and touches nothing the user owns. | `workspace.py`, `scheduler.py` |
| 4 | **Never discard uncommitted work.** A dirty worktree is never removed, pruned, or force-deleted. | `workspace.py`, `prune.py` |
| 5 | **Never guess a number.** Token counts come only from telemetry an agent reported. Unavailable is displayed as unavailable and contributes zero to every sum. | `types.py`, `metrics.py`, `budget.py` |
| 6 | **Degrade, never fail.** No git, no run store, no skills directory: the run continues with a warning. Isolation and observability are features, not preconditions. | `workspace.py`, `store.py`, `skills/` |
| 7 | **Bounded by construction.** Repair attempts, tokens, and wall time all have ceilings. An unbounded loop is not representable. | `graph.py`, `budget.py` |
| 8 | **A failure is not fatal until it is total.** One dead member of an ensemble is a warning; a phase fails only when every member of it fails. One failed task of a goal stops only the tasks that depended on it. | `graph.py` (sync nodes), `scheduler.py` |
| 9 | **Nothing is executed during inspection.** Preflight, skill discovery, and context collection read; they never launch an agent or run a discovered script. | `preflight.py`, `skills/`, `context.py` |
| 10 | **No secrets leave the machine.** `.env` and credential files are redacted from context; no API keys are read, stored, or transmitted. | `context.py` |
| 11 | **Ambiguity is escalated, never guessed.** A conflict between two agents' work — a dependency that will not merge, two tasks that changed the same file — stops and names a person. Picking a winner is the failure isolation exists to prevent. | `scheduler.py`, `workspace.py` |
| 12 | **Every decision is a deliberate human act.** Nothing starts a run, answers a gate, or publishes work except a person doing so on purpose — never a timer, a webhook, or an external signal. Views remain pure projections of recorded facts; what may *act* is enumerated, not open-ended: `--serve` writes nothing, `--design` writes only the team file, and the daemon's control API is a closed list of verbs, each one reached only from a page it served, behind a per-launch token and a same-origin check (§22.4). | `board.py`, `serve.py`, `teams.py`, `daemon.py` |
| 13 | **Never auto-push.** Work reaches a remote only when a person says so: `delivery.enabled` must be on *and* a human must act. No verdict, schedule, or wave completion crosses the network. This is invariant 3 in network form — and it is why a projection reads *recorded* CI facts rather than live ones. | `delivery.py` |

---

## 2. Conceptual hierarchy

```text
Agent  →  Model  →  Role  →  Responsibility  →  AgentResult
```

| Concept | Meaning | Examples |
| --- | --- | --- |
| **Agent** | The execution provider / binary. Owns subprocess mechanics, auth context, CLI flags. | `antigravity`, `claude`, `opencode` |
| **Model** | The engine selected from that provider, passed via the official `--model` flag and validated against the catalog. May be a **list**, forming an escalation ladder (§9.2). | `gemini-3.8-flash-high`, `sonnet`, `opencode/gpt-5.1-codex` |
| **Role** | The functional position in the pipeline. Determines which prompt is built and how the result is interpreted. | `researcher`, `planner`, `implementer`, `verifier` |
| **Responsibility** | The instructions for that role, injected into the prompt from `orchestrator.yaml`. | "Verify the implementation… return PASS or FAIL" |
| **AgentResult** | The standardized, serializable record of one execution. | see §4.2 |

> An **Agent** is *who* executes. A **Model** is *which engine*. A **Role** is *what position*.
> A **Responsibility** is *what instructions*. An **AgentResult** is *what they produced*.

Above that sits the delegation hierarchy (§16–§17), which the same rules extend into:

```text
Goal          what a person asked for                        (goals.py)
  Task        one focused, independently deliverable unit     (decompose.py)
    Session   one attempt at a Task by a team of agents       (the pipeline; `run` on disk)
      Phase   one role, one or more agents, in parallel       (graph.py)
```

A Task's state is derived from its Session; a Goal's status is derived from its Tasks. That is
invariant 1, applied at each level rather than restated — and once a person publishes a Task's
branch, a **Delivery** (§20) is one more level of the same thing: the record holds what the
forge said, and the state is derived from it.

---

## 3. Runtime topology

The graph is **built from configuration**, not hard-coded. `build_graph(config)` reads the
`agents:` list and assembles phases from it.

```text
START
  │
  ▼
context ──▶ preflight ──▶ ┌─ phase ─┐ ──▶ … ──▶ acceptance ──▶ ┌── verifier phase ──┐
                          │ member  │              (gate)         │ member … member    │
                          │ member  │                             └─────────┬──────────┘
                          └────┬────┘                                       │
                               ▼ sync                                     sync
                                                                            │
                                                          ┌─────────────────┴────────────┐
                                                     should_repair_or_end                │
                                                          │                              │
                                              repair_node ──▶ acceptance ──▶ …      finalize ──▶ END
```

A decomposition runs on its own graph, because it plans rather than changes anything:

```text
START ──▶ context ──▶ preflight ──▶ decomposer ──▶ finalize ──▶ END       (--plan-only)
```

### 3.1 Phases

Consecutive agents sharing a role become **one phase** whose members run in parallel. Every
phase — one member or five — ends at a **sync node**, because that is the only place able to
see every member's outcome. Phase-level decisions therefore have exactly one home:

* **Fatal vs. survivable failure.** All members failed → set `error` (the run's one fatal
  flag). Some failed → log a partial failure and continue on the survivors (invariant 8).
* **Verifier consensus.** Parallel verdicts are resolved into the single verdict that routes
  the workflow (§8.3). Resolving here also avoids concurrent writes to a single-value channel.

### 3.2 Nodes

| Node | Responsibility |
| --- | --- |
| `context` | Resolve project root, collect redacted context, load and validate config, discover skills, open the run store, prepare the workspace, seed the budget and the run clock. |
| `preflight` | Probe every configured agent before any of them launches (§6). A failed strict preflight halts here. |
| `<role>_<i>` | One agent execution: build the role's prompt, run the CLI, record an `AgentResult`. Replays instead of executing when resuming (§11.2). |
| `<role>` (sync) | Phase fan-in: fatal-vs-survivable, consensus, budget measurement. |
| `acceptance` | Runs the project's own check and records the result as a fact (§9.4). Present only when a command is configured; every path into verification, including each repair, passes through it. |
| `decomposer` | Turns a goal into a task graph (§16). Runs only on the `--plan-only` graph, never inside an ordinary run. |
| `repair_node` | An implementer executing repair attempt N, prompted with the verifier's findings *and the gate's own failure output*. Reuses the last implementer's config, including its escalation ladder. |
| `finalize` | Derive the status once, commit and clean up the worktree, close the run store. Every terminating path passes through here. |

### 3.3 The graph is built per run, not per process

`build_graph(config)` is called by the CLI with the configuration resolved for *that* run,
because the topology depends on it: an acceptance command adds a node, an ensemble adds
members. The module-level `graph` object still exists for LangGraph's own tooling
(`langgraph.json`) and for tests, but it is built at import time from whatever
`orchestrator.yaml` was on disk, so nothing that varies per run may depend on it.

### 3.4 Why the runner lookup is dynamic

`get_runner(agent_name)` resolves through the module object rather than a module-level dict.
A static dict would capture the original functions at import time, and `unittest.mock.patch`
patches the module attribute — the tests would silently exercise the real CLIs.

---

## 4. State and results

### 4.1 `OrchestratorState`

Everything here except `status` is a **durable fact**. `status` is the one derived field, and
it is written exactly once, by `finalize_node`.

```python
class OrchestratorState(TypedDict, total=False):
    # Inputs
    task: str
    project_root: Optional[str]
    project_context: Optional[str]
    config_path: Optional[str]
    config: Optional[OrchestratorConfig]

    # Discovery
    skills: Optional[List[SkillInfo]]
    skill_manifest: Optional[str]
    preflight: Optional[PreflightReport]
    skip_preflight: Optional[bool]

    # Facts produced by execution
    agent_results: Annotated[List[AgentResult], add]
    verification_history: Annotated[List[VerificationRecord], add]
    verification_verdict: Optional[str]      # resolved by the verifier's sync node
    repair_attempts: int
    max_repair_attempts: int
    blocked_reason: Optional[str]            # a BLOCKED verdict's human-action text

    # Workspace (Tier 0 #3)
    workspace: Optional[WorkspaceInfo]
    workspace_summary: Optional[dict]

    # Acceptance gate (Roadmap Phase 0)
    acceptance_checks: Annotated[List[dict], add]   # what the project's own command did
    acceptance_required: Optional[bool]             # may a red gate overrule a PASS?

    # Decomposition (Roadmap Phase 1)
    task_plan: Optional[dict]                # the validated task graph
    plan_only: Optional[bool]

    # Delegation (Roadmap Phases 2-3): this session is one task of a goal
    goal_id: Optional[str]
    task_id: Optional[str]
    workspace_base_ref: Optional[str]        # the branch or commit this session starts from
    workspace_merge_refs: Optional[list]     # dependency branches merged in before any agent runs
    workspace_merge: Optional[dict]          # what that merge did

    # Budget (Tier 2 #11)
    budget: Optional[dict]                   # the resolved ceiling
    budget_state: Optional[dict]             # what was spent, measured at the decision point
    budget_exhausted_reason: Optional[str]   # recorded fact: why the run stopped spending
    run_started_at: Optional[float]

    # Run store (Tier 1 #4)
    run_id: Optional[str]
    run_dir: Optional[str]
    run_store_enabled: Optional[bool]
    run_store_max_output_chars: Optional[int]
    resumed_from: Optional[str]              # set when this run continues a stored one

    # Execution environment
    visible_terminals: Optional[bool]
    terminal_type: Optional[str]
    agent_execution_mode: Optional[str]
    pause_on_completion: Optional[float]
    consensus_policy: Optional[str]

    # Derived, written only by finalize_node
    status: str
    summary: Optional[dict]
    error: Optional[str]
```

> **Reducer note.** `agent_results` and `verification_history` use `add` reducers. A node that
> returns their existing contents appends a *second copy*. Seed them only when genuinely empty.

### 4.2 `AgentResult` and `VerificationRecord`

```python
class AgentResult(TypedDict, total=False):
    agent: str; role: str; status: str        # "success" | "error"
    output: str; duration_seconds: float
    model: Optional[str]
    verdict: Optional[str]                    # verifier only
    repair_attempt: Optional[int]             # repair executions only
    token_usage: Optional[TokenUsage]
    execution_mode: Optional[str]             # "native_tui" | "headless"

class VerificationRecord(TypedDict, total=False):
    attempt: int                              # 1-based verification iteration
    repair_attempts: int                      # repairs completed before this evaluation
    verdict: str; output: str
    duration_seconds: float
    model: Optional[str]; agent: Optional[str]
    token_usage: Optional[TokenUsage]
    blocked_reason: Optional[str]             # carried as a fact so BLOCKED survives consensus
```

An errored execution is recorded as a **fact**, with the failure reason as its `output`.
Whether that failure is fatal is decided by the phase's sync node, never by the member.

---

## 5. Configuration

`orchestrator.yaml` is the single source of truth. `config.py` validates it and supplies a
`get_*_config` accessor per section, each of which returns every key populated so callers
never branch on a missing setting.

| Section | Purpose |
| --- | --- |
| `agents` | Who runs, in what order, as what role. Consecutive same-role entries form an ensemble. |
| `roles` | The responsibility text injected into each role's prompt. |
| `models` | The validation catalog. Every configured model — and every rung of a ladder — must appear here. |
| `max_repair_attempts` | How many repair passes a failure may buy. `0` disables repair. |
| `budget` | What those passes may cost, per session and per goal (§9.3, §17.4). |
| `execution` | Terminal visibility, terminal type, execution mode, pause. |
| `preflight` | `enabled`, `strict`, `deep`, `timeout_seconds` (§6). |
| `run_store` | `enabled`, `directory`, `max_output_chars` (§11). |
| `workspace` | `isolation`, `directory`, `commit_on_finish`, `keep_worktree` (§7). |
| `verification` | `consensus`: `unanimous` \| `majority` \| `any` (§8.3), and `acceptance`: the objective gate (§9.4). |
| `planning` | Which agent decomposes a goal, and the ceiling on tasks per plan (§16). |
| `delegation` | How a decomposed goal is carried out: `max_parallel`, `stop_on_failure` (§17). |
| `approval` | Which human gates are on, and `auto_approve` (§19). |
| `delivery` | Whether work may reach a remote at all, and how: `enabled`, `remote`, `base`, `draft`, `on_approve` (§20). |
| `skills` | `enabled`, `search_paths` (§12). |

The `agents:` block, `max_repair_attempts`, and `verification.consensus` are also what the
design surface edits (§21); every other section is hand-written only.

Validation is strict and fails loudly at load: an unknown model, an empty ladder, a negative
budget, or an unrecognized isolation mode raises `ConfigValidationError` with a message that
names the offending key and lists the valid values. Configuration errors are the cheapest
class of failure to catch, so they are caught before anything is launched.

The CLI can override selected settings for one run (`--isolate`, `--keep-worktree`,
`--max-total-tokens`, `--max-duration-seconds`, `--max-repair-attempts`, …). An override is
applied by rewriting the loaded config into `state["config"]`, so there is still exactly one
resolved configuration object per run.

---

## 6. Preflight

Every configured agent is probed **before the first one launches**, so an unusable environment
costs seconds instead of failing six minutes in, after two agents have already burned tokens.

| Check | Severity | Question |
| --- | --- | --- |
| `runner` | error | Does the orchestrator know how to run this agent? |
| `executable` | error | Does its CLI binary resolve on this machine? |
| `model_catalog` | error | Is the configured model in the catalog? |
| `role` | warning | Does the role have a responsibility? |
| `version` | warning | Does the binary actually respond? (deep mode only) |
| `acceptance` | warning | Does the configured acceptance command resolve on PATH? |

Shallow mode makes no subprocess calls. Deep mode adds one `--version` per distinct agent.
`check_verifier_independence` additionally warns when the verifier shares an agent *and*
model with the **planner** (grading a plan its own model wrote) or the **implementer**
(reviewing its own work). It is a warning, not an error: a single-vendor setup is a legitimate
choice, it just should not be an accidental one.

Probe without running a task: `--doctor`. Skip for one run: `--no-preflight`.

---

## 7. Workspace isolation, retention, and pruning

### 7.1 Isolation

One run gets one worktree:

```text
<project_root>/.orchestrator/worktrees/<run_id>     # working tree
orchestrator/run/<run_id>                            # branch
```

Every agent in the run executes with `cwd` set to that worktree, so the implementer writes
there and the verifier inspects the same tree it wrote. The run branch is created from the
current `HEAD` — or from `workspace_base_ref`, which is how a delegated task starts from its
dependency's branch (§17.2). The user's working tree and branch are never modified, checked
out, or reset.

`isolation: worktree` makes isolation a **precondition** — the run refuses to start
unisolated rather than silently writing the user's checkout. `auto` degrades with a warning
(no git, not a repository, no commits yet). `none` runs in the project directory.

### 7.2 Retention — the branch is the artifact

At the end of a run `finalize_node` commits the worktree onto its branch, and then applies the
retention policy in `finish_worktree`:

| Situation | Outcome |
| --- | --- |
| Work committed | Worktree **removed**, branch kept. The branch holds the work; the checkout was a second full copy of the project. |
| Worktree dirty (nothing committed) | Worktree **kept**, with the reason stated. The directory is the only copy — invariant 4. |
| Clean, and the branch has no commits of its own | Worktree removed **and the branch deleted**. The run changed nothing; the branch was an alias for the base commit. |
| `keep_worktree: true` | Kept regardless. |

Restoring a removed checkout is one command, printed in the run summary:
`git worktree add <path> <branch>`.

### 7.3 Pruning

Branches accumulate on purpose, so clearing them is an explicit, user-initiated sweep
(`prune.py`), never something a run does on its own:

```powershell
python -m orchestrator --prune-runs --older-than 30d --keep-failed --dry-run
```

The plan is always computed and shown first; `--dry-run` stops there, and an interactive
terminal is asked to confirm. A branch is **kept** when it is checked out in the main
repository, newer than the threshold, backed by a dirty worktree, or — under `--keep-failed` —
belongs to a run that did not succeed. A run with no recorded status cannot be shown to have
succeeded, so `--keep-failed` keeps it too. Every exclusion is reported with its reason.

The run store is never pruned: it is small append-only text, and it is the dataset `--stats`
reads.

#### Merged is safer than unreviewed

`--prune-runs` predated delivery (§20) and so treated a merged branch exactly like an
unreviewed one, which had it backwards: a branch whose pull request merged has had its work
taken into the base branch, so the branch is a **duplicate** rather than the only copy. It is
therefore the safest thing in the repository to sweep, and answers to a threshold of its own:

```powershell
python -m orchestrator --prune-runs --older-than 30d --merged-older-than 1d
```

A merged branch qualifies at whichever threshold is shorter, and `--keep-failed` does not hold
it back — that flag asks "could this still need inspecting?", and a person merging it already
answered. The two rules that protect *work* rather than evidence still apply unchanged: it
cannot be the branch you are standing on, and it cannot have a dirty worktree. The plan names
the delivery state of every candidate, so "why is this one sweepable" is on the report rather
than in this document.

#### The record outlives the branch

A delivery record must survive the branch it describes. Retention therefore **annotates** the
record — `local_branch_pruned_at`, plus a line in the record's own history — and never deletes
one. The board reads deliveries, not branches (§18.1), so work that landed goes on saying it
landed after the branch that carried it is gone. Making the board forget that work merged is
the one thing retention must not be able to do, so the delivery store is not pruned either,
for the reason the run store is not.

---

## 8. Verification

### 8.1 Independence

The verifier runs with `cwd` set to the same workspace the implementer wrote, and is
instructed to inspect the files on disk rather than trust the implementation report.

### 8.2 Verdicts

The prompt requires an unambiguous lead line; `parse_verdict` extracts it with
`(?:^|\n|\b)\*?\*?VERDICT:\*?\*?\s*(PASS|FAIL|BLOCKED)\b`, tolerating markdown emphasis and
headings.

| Verdict | Meaning | Consumes repair budget? |
| --- | --- | --- |
| `PASS` | The implementation satisfies the task. | — |
| `FAIL` | Wrong, and an agent could plausibly fix it. | yes |
| `BLOCKED` | Progress requires a human. No agent could resolve it. | **no** |
| `UNKNOWN` | No parseable verdict. Fail-safe; never a pass. | yes |

`BLOCKED` exists because spending three escalating repair attempts on a missing credential or
an ambiguous requirement is pure waste. `extract_human_action` lifts the "what a person must
do" text out of the verifier's output and records it on the `VerificationRecord`, so it
survives consensus resolution and is printed under a **NEEDS YOU** heading.

### 8.3 Verdict precedence

`derive_verdict` is the single place a run's verdict is decided, and the order is fixed:

1. **`BLOCKED` from any verifier.** A human is needed regardless of what any test says.
2. **A failed required acceptance gate** turns a `PASS` into `FAIL` (§9.4). A gate never turns
   a `FAIL` into a `PASS` — it can only withhold trust, never grant it.
3. **The consensus** of the verifiers, resolved by policy.

The verifier's sync node reads its routing verdict from this function rather than recomputing
consensus, so the router and the derived status cannot disagree. The CLI shows both numbers: an
agent's badge is what that agent said; the run's headline is what the run concluded.

### 8.4 Consensus

Several consecutive `verifier` agents run in parallel as a quorum. `resolve_consensus` folds
their verdicts into one, with fixed precedence:

1. **Any `BLOCKED` wins.** If one verifier says a human is needed, that is true regardless of
   what the others concluded.
2. Otherwise the policy decides: `unanimous` (default — every verdict must be `PASS`),
   `majority` (a strict majority), or `any` (a single `PASS`; weakest).
3. Not a pass? Prefer the concrete `FAIL` over the fail-safe `UNKNOWN`.

`unanimous` preserves the fail-safe contract: a dissenting or ambiguous verifier can never be
outvoted into a false `PASS`.

---

## 9. The repair loop and its ceilings

### 9.0 Retrying an execution, which is not repairing a result

Two different failures wear the same word, and conflating them left a real hole for a long
time:

| | Repair (§9.1) | Retry (this section) |
| --- | --- | --- |
| What failed | the verifier judged the work **wrong** | the agent **produced nothing** |
| What is known | an output exists and was assessed | there is no output to assess |
| The response | a new prompt carrying the findings | the same prompt, again |
| Configured by | `max_repair_attempts` | `execution.retry.attempts` |

Everything this project built assumed the second case did not happen. It did — and it was the
*only* thing that happened. Across every real end-to-end run this repository ever recorded,
**seven of seven failures were one agent returning empty output on the first step**, and the
pipeline halted there with no second attempt. A whole orchestrator was only ever as reliable as
its flakiest single call.

A local CLI agent is a subprocess over a network service and fails the way those fail:
intermittently, and by returning nothing rather than by raising. So an execution is attempted
up to `execution.retry.attempts` times, with a delay that doubles up to a ceiling, and both
failure modes — an empty reply and a raised exception — take the same path. When the agent's
`model:` is an escalation ladder (§9.2), each retry steps down it, which is the ladder finally
doing on execution failure what it already did on verification failure.

Three properties are load-bearing:

* **A retry is not an ensemble.** However many attempts one execution takes, the phase emits
  exactly **one** `AgentResult`. `agent_results` is what consensus is resolved from (§8) and
  what `--stats` computes pass rates over (§21.0), so three attempts appearing as three results
  would corrupt both. The attempts that failed become their own `agent_retry` events instead —
  recorded, and shown on the console, because a retry costs a whole agent invocation and a run
  that pauses for eight seconds with no explanation looks hung.
* **It is reversible by configuration.** `attempts: 1` restores the previous behaviour exactly,
  down to the wording of the error, so the change can be turned off rather than reverted.
* **It still fails.** An execution that never succeeds fails the phase as it always did, with
  every attempt's reason kept on the result. A retry policy that turned a broken agent into a
  silent success would be worse than no retry at all, and "it was flaky" must stay
  distinguishable from "it is broken" after the run is over.

The ceiling is deliberate and hard: `MAX_RETRY_ATTEMPTS` refuses a configuration above 10.
Retrying is insurance against a flaky call, not a way to hammer a service that is down.

**What retrying then revealed, which is the more useful half.** Turning three attempts on and
watching them all fail is a *diagnostic*: it distinguishes a flaky agent from a broken one. The
first live run under this policy produced six consecutive empty replies from the researcher,
which is not flakiness. Reproducing it took one command:

| Prompt to `gemini-3.8-flash-high` | output tokens | of which thinking | `response` |
| --- | --- | --- | --- |
| "Reply with the single word: OK" | 30 | 29 | `"OK"` |
| a reasoning-heavy arithmetic question | 1460 | 877 | 1148 chars |
| **"investigate this project and describe…"** | 1337 | 1039 | **empty string** |

Asked to *investigate a project* — the researcher's entire job — the CLI returns
`status: SUCCESS` and an empty `response`, having spent its output budget on thinking. Small
prompts answer correctly, which is why `--doctor` passes and why this survived undiagnosed
across every recorded failure. The role was reassigned on that evidence (§27 records the
`--stats` basis), and the general lesson is worth stating: **a retry policy is an instrument as
well as a remedy.** An execution that fails three times with a widening gap has told you
something a single failure could not.

There is a real gap left here. `escalate_model` steps down a *model* ladder, so it can only
fall back within one provider; when the researcher had a single model configured there was
nowhere to escalate to. Letting a role's `agent:` be a list — the way `model:` already can be —
is the natural next step, and is not built.

### 9.1 The loop

On a repairable verdict the run routes to `repair_node`, which is prompted with the original
task, the plan, the previous implementation, the verifier's findings, the attempt index
(`Attempt X of Y`), and an inspect-first mandate. It then re-enters **every** verifier member,
so a quorum re-evaluates the repaired workspace rather than one member speaking for it.

### 9.2 Escalation ladders

A `model` may be a list. Rung 0 runs the initial attempt; rung N runs repair attempt N
(clamped to the last rung). A repair therefore escalates instead of re-running the model that
just failed. Every rung is validated against the catalog at load time.

```yaml
  - agent: opencode
    model: [opencode/big-pickle, anthropic/claude-sonnet-4-5, opencode/gpt-5.1-codex]
    role: implementer
```

### 9.3 Budgets

`max_repair_attempts` bounds how many attempts a run makes; it says nothing about what they
cost. With a ladder configured, one task can put three agents through three attempts, each
more expensive than the last. `budget.max_total_tokens` and `budget.max_duration_seconds` are
that ceiling (`0` = unlimited, the default).

* **Measured where the decision is made.** The verifier's sync node evaluates the budget and
  records `budget_exhausted_reason` as a fact; `should_repair_or_end` and `derive_status` read
  that fact. Nothing is interrupted mid-execution: a ceiling cannot un-spend a call already in
  flight, and killing an agent halfway wastes everything it has already cost.
* **Recorded, not re-measured.** Deriving the overrun at read time would make status
  time-dependent — a stored run replayed days later would keep accruing wall-clock time
  against its old ceiling.
* **Never imputed.** Spend counts only reported token usage, so an agent that reports nothing
  can never fabricate an overrun (invariant 5).
* **A pass is never overridden.** `PASS` ends the run as `completed` whatever it cost.

A run that stops this way ends as `budget_exhausted` — distinct from `failed`, because the
implementation was not judged unfixable; the run simply declined to keep paying.

### 9.4 The objective acceptance gate

Everything above optimizes toward one sentence: `VERDICT: PASS`. Until the gate existed, that
sentence was a model's opinion about whether the tests pass — the exact kind of unverified claim
invariant 2 rejects when the *implementer* makes it. `verification.acceptance.command` is a
command the orchestrator runs itself:

```yaml
verification:
  acceptance:
    command: "pytest -q"     # or ["python", "-m", "pytest", "-q"]; empty disables the gate
    timeout_seconds: 600
    required: true           # a red gate overrules a verifier's PASS
    output_limit: 4000
```

* **It runs before the verifier**, so the verifier is handed evidence instead of being asked to
  produce it, and the repair after a failure receives the *actual* error output — the single
  most useful thing a repair attempt can be given.
* **The command comes from the user's checkout, never from the worktree.** Configuration is
  loaded at the project root before any agent runs, so an agent cannot edit the command that
  judges it. This is what makes the gate trustworthy rather than decorative.
* **No shell**, ever: a string is tokenized into argv and run with `shell=False`, like every
  other subprocess here.
* **A gate that cannot run is not a pass.** A missing binary, a timeout, or a crash is recorded
  as a failure with its reason. Failing open would be worse than having no gate, because it
  would look like evidence.
* **One result per repair generation.** The check that judges an attempt is the one taken after
  that many repairs, so a stale red result cannot condemn a fixed workspace.
* **It reports; `derive_verdict` decides.** `required: false` records and displays the result
  while changing nothing.

`--stats` then answers the question the gate exists to make askable: **how often does a verifier
return PASS while the project's own suite is red?** That is a property of a model, measured on
your tasks, and no consensus policy over several verifiers fixes a verifier that has it.

---

## 10. Derived status

Display status is **never stored** — it is computed from durable facts by `status.py`, whose
every function is pure. Storing a running status alongside the facts lets the two disagree;
deriving it makes that class of bug unrepresentable.

Precedence, highest first:

1. A failed strict preflight → `preflight_failed` (nothing was launched).
2. Any hard `error` fact, or a state whose every execution errored → `error`.
3. The latest verification verdict:
   * `PASS` → `completed`
   * `BLOCKED` → `blocked`
   * `FAIL` / `UNKNOWN` → `budget_exhausted` if the budget fact is set, else `failed` if the
     repair budget is spent, else `needs_repair`.
4. Otherwise, progress implied by the most recent successful result: `repaired`,
   `implemented`, `planned`, `analyzed`, `context_prepared`, `pending`.

`TERMINAL_STATUSES` says where the workflow can no longer progress; `UNSUCCESSFUL_STATUSES`
drives the CLI exit code. `derive_summary` renders the whole display-facing view — including
the budget snapshot and the workspace outcome — in one call, so no caller needs to trust a
stored string.

---

## 11. The run store

### 11.1 Layout

```text
.orchestrator/runs/<run_id>/
  run.json      # metadata + settings, rewritten on start, resume, and finish
  events.jsonl  # append-only log, one JSON object per line — the source of truth
```

Events: `run_started`, `preflight`, `context_collected`, `skills_discovered`, `workspace`,
`agent_result`, `verification`, `run_resumed`, `run_finished`.

`run_started` records the run's **settings** (consensus policy, isolation mode, budget,
execution mode). They cost nothing to store and they are what lets `--stats` compare
configurations rather than merely count runs.

The store never breaks a run: every filesystem operation is wrapped, a failure marks the store
degraded and is reported once, and the pipeline continues (invariant 6). `RunStore` holds no
open handles, so it is rebuilt from a directory path in each node — which keeps LangGraph
state free of non-serializable objects.

Browse with `--runs` and `--show-run <id|prefix|latest>`.

### 11.2 Resume

`--resume <id>` replays a stored run's event log back into state, marking every replayed
record `restored: True`, and continues from where it stopped. Each phase asks `should_replay`
whether the resumed run already produced its work:

| Phase | Replays when the restored run holds… |
| --- | --- |
| researcher / planner | a successful result for that role |
| implementer (initial) | a successful implementer result with no repair index |
| verifier | a verification record made after the *same* number of repairs |
| repair | a successful implementer result for the exact attempt about to be made |

Deciding from facts, per repair generation, keeps replay correct when the loop re-enters the
same node several times — a counter would replay the same recorded verification forever. A
replayed repair still advances `repair_attempts`, for the same reason. A phase whose members
all errored is re-run, because there is nothing to reuse.

Resuming also checks the run's branch back out (`reattach_run_worktree`), reusing the original
directory when it survives and recreating it from the branch when it does not. When neither is
possible the resumed run degrades to an un-isolated workspace and says so.

A resumed run appends to the **original** run's log — it is the same run continuing, and
splitting it would make its cost and history unreadable.

### 11.3 Aggregate analysis

`--stats [N] [--json]` reads every stored run and answers the questions that only appear once
you have a hundred of them, and that no external tooling can answer about *your* agents on
*your* tasks:

* Pass and error rates per **agent / model / role** pairing, with mean tokens and duration.
* What a **pass costs against a failure**, in tokens per run.
* Whether **repair attempt #2 ever succeeds when #1 did not** — is the ladder earning its
  escalation, or just spending?
* Whether **verifier ensembles** change outcomes enough to justify their price.
* Outcomes grouped by recorded **setting** (consensus, isolation, execution mode).

Two rules keep the report honest: runs whose token usage is incomplete are **excluded** from
cost statistics and that exclusion is printed; and every rate carries its denominator, with
samples below five flagged — a 100% pass rate over two runs is not a fact about a model.

### 11.4 The archive — correcting the store without destroying it

Those two rules keep the *report* honest and can do nothing about the *dataset*. For most of
this project's life the test suite wrote real run records into the developer's own
`.orchestrator/runs`. `tests/support.py` stopped that (§29) but could not un-write what was
already there: **141 synthetic runs against 23 real ones — 86% of the store.** Everything
downstream read them and reported them faithfully. The board drew 126 `Ready to Merge` cards
for pipeline runs that never ran; `--stats` scored one agent over 324 executions that were
mocks; §27's planner memory and §21.0's evidence-beside-the-choice were computed from the same
numbers. None of that was a bug. Each was a correct projection of a corrupt store, which is
the more dangerous shape of the problem: nothing anywhere reported an error.

`archive.py` is the correction, and its one design commitment is that **it does not delete**.
A run record is evidence. The reason to take one out of the store is never that its history is
worthless — it is that it is not evidence *about this project*, and every projection is being
told otherwise. So a run is **moved**, whole, into `.orchestrator/archive/runs/<run_id>/`, and
`--restore-runs` moves it back byte-for-byte. The only filesystem verb in the module, in both
directions, is a move.

```bash
python -m orchestrator --archive-runs --dry-run   # the plan, and nothing else
python -m orchestrator --archive-runs             # ask, then move
python -m orchestrator --archived                 # what is in the archive, and why
python -m orchestrator --restore-runs             # all of it back, or name ids
```

**What counts as synthetic is evidence, not a guess.** Matching the task text would have
worked exactly once, on this repository, for the string those particular tests happened to
use. A run is judged instead on two independent facts that one real agent invocation cannot
both produce:

| | Synthetic | Real |
| --- | --- | --- |
| Token usage reported by any execution | never | always, in all 23 |
| Slowest single execution | ≤ 0.88s | ≥ 8.26s |

Both must hold before a run is selected, and a run that recorded **no** executions is never
synthetic — that is an interrupted run, which is real. Requiring both is what keeps a genuine
run whose agents all failed to report usage (which invariant 5 explicitly permits) out of the
archive. `SYNTHETIC_MAX_EXECUTION_SECONDS` is 2.0s, sitting in a gap the two populations do
not come close to touching. `--matching TEXT` adds a second, explicit selector for the cases
evidence cannot reach; it combines with the synthetic test rather than replacing it.

Three rules, each the archive's answer to a way this could have gone wrong:

* **The plan is shown before anything moves**, and an interactive terminal is asked to
  confirm — the same shape as `--prune-runs` (§7.3), because what changes underneath is every
  number this project reports about itself.
* **An archived run says why it was archived.** `archived.json` inside it records the
  timestamp, the reason, and the evidence the judgement rested on, so the decision can be
  argued with a year later. Restoring deletes that sidecar, so a restored run carries no trace
  of having been archived.
* **A restore never overwrites.** A run whose id is already in the store is reported as a
  conflict and left in the archive. The store is append-only, and clobbering a live run is the
  one way this module could destroy something.

Nothing else reads the archive. It is out of the store, and that is the entire point.

**What the clean store then said**, which is the half worth keeping: with the mocks gone,
`--stats` puts the researcher role's Antigravity pairing at a **90% error rate over 10 runs** —
the failure §9.0 diagnosed by hand, sitting in the data all along and drowned out by 141 runs
that never called a provider. The same read corrects a claim made from the polluted store: the
Claude researcher has **3** recorded runs here, not 20, which is below the five-observation
line `--stats` flags. The reassignment was right; the evidence offered for it was inflated,
and that is exactly the class of error a corrupt dataset produces.

---

## 12. Token accounting

```text
   Antigravity            Claude Code             OpenCode
--output-format json   --output-format json    --format json
   payload["usage"]      payload["usage"]     step_finish tokens
        └────────────────────┼────────────────────┘
                             ▼
             TokenUsage {input, output, total, available}
                             ▼
                        AgentResult ──▶ metrics / budget / stats
```

**Real usage only.** No character counts, no words-per-token heuristics, no multipliers. A CLI
that returns unstructured text, malformed metadata, or runs in a mode without telemetry yields
`available: False`, displayed as `unavailable`. Missing telemetry never halts a run and never
counts as zero in a way that could be mistaken for a measurement: totals are reported as
`Known tokens: <sum>` alongside `Unavailable: <n> execution(s)`.

`verify_token_aggregation_invariant` asserts the displayed per-agent values sum exactly to the
reported total (`--token-diagnostics` prints the audit).

---

## 13. Skills

Skills are modular capabilities discovered passively from configured search paths (default
`./skills`, `./.agents/skills`). A skill is a directory containing `SKILL.md` with optional
frontmatter, plus optional `scripts/`, `templates/`, `assets/`, `examples/`, `resources/`.

Access has two levels:

1. **Awareness (every role).** A compact manifest — name, description, location, instructions
   path, resource directories — is injected into every prompt. Full contents are never
   injected; agents read them on demand from disk.
2. **Inspection and use (implementer, repair, verifier).** These roles receive exact
   filesystem paths and are instructed to read `SKILL.md` before writing or verifying code.

Discovery executes nothing, searches only within approved roots (never a drive root or system
directory), and never modifies an external skill directory.

---

## 14. Execution environment

Agents run headless by default. `execution.agent_execution_mode` selects `auto` (prefer a
native TUI where the provider supports one, fall back to headless), `native_tui`, or
`headless`. With `visible_terminals: true`, each execution is launched into its own titled
terminal window (`LangGraph - OpenCode Repair #1`), so a live run can be watched.

Live output is for the human; the launcher separately captures exact stdout, stderr, exit
code, and duration into a structured result, so console styling never pollutes state. The
launcher passes work through a `job.json` in a temporary directory rather than a command line,
which avoids the Windows 8191-character limit and removes command-string injection as a class.

All subprocesses use `shell=False`, `stdin=subprocess.DEVNULL`, explicit UTF-8 decoding with
replacement, and enforced timeouts.

---

## 15. Security guarantees

1. **Zero provider API keys.** Every agent runs as a local CLI subprocess relying on
   pre-existing local authentication. The orchestrator never requests, reads, stores, or
   transmits keys or tokens.
2. **Secret redaction.** Context collection skips `.env*`, SSH keys, and other credential
   files by name, and excludes `.git`, virtualenvs, caches, and `node_modules` wholesale.
3. **Official model selection only.** Models are passed via documented CLI flags and validated
   against the catalog before launch.
4. **Safe subprocess execution.** No shell, no inherited stdin, enforced timeouts.
5. **Directory confinement.** Modifying commands run inside the run's workspace, which is a
   worktree, not the user's checkout.
6. **No automatic merges, no forced deletions.** Integrating agent work is a human decision.
7. **No automatic pushes.** Work reaches a remote only when a person acts and the project has
   opted in (§20). The forge is driven through the user's own `gh`, so point 1 holds there
   too: no token is read, stored, or transmitted.
8. **Writes are enumerated, never ambient.** `--serve` writes nothing. `--design` adds exactly
   one writable route, `POST /api/team`, which edits the team file after validating the result
   (§21). The daemon adds a closed list of control verbs (§22) — start, cancel, approve,
   reject, deliver — and no other route can act.
9. **The daemon is not trusted for being local.** Every `/api/*` route requires a token minted
   at launch, and refuses any request carrying a foreign `Origin` before reading its body — a
   page open in the same browser can reach `127.0.0.1`, so being on loopback is not a
   permission (§22.1). The token is written owner-only and removed when the daemon stops.
10. **The desktop shell grants no extra power.** It runs the page sandboxed, with context
   isolation on and no Node integration, and exposes no privileged bridge: everything it can do
   is a daemon route (§25).

---

## 16. Goal decomposition (`--plan-only`)

One request drives one linear pipeline, which makes this an orchestrator of a *pipeline*. A
team needs work broken into pieces that can be handed out and sequenced, so `--plan-only` runs a
`decomposer` that turns a goal into a **task graph** and stops:

```powershell
python -m orchestrator "Add rate limiting to the API" --plan-only
python -m orchestrator "Add rate limiting to the API" --plan-only --json
```

Each task carries an id, a title, its intent, acceptance criteria, dependencies, and the areas
it expects to touch. `decompose.py` treats the agent's reply as untrusted text: it extracts the
JSON, normalizes it, drops unknown and self-dependencies with a warning, rejects cycles, and
groups the result into **execution waves** — sets of tasks with no ordering constraint between
them, which is the shape a scheduler would run in parallel.

Deliberate boundaries:

* **Planning executes nothing.** `--plan-only` runs on its own graph
  (`build_plan_graph`), so it cannot reach an implementer, and an ordinary run never pays for a
  decomposition it did not ask for.
* **The decomposer is not in `agents:`.** It runs *before* a pipeline, not inside one, so it is
  configured under `planning:` and inherits the planner's agent and model unless told otherwise.
* **A plan is a fact.** It is recorded to the run store like any other; the execution order is
  derived from the dependencies at read time, never stored as a sequence.
* **`planning.max_tasks` is a bound**, because a decomposer that emits work is a loop generator.
* **It is no longer cold.** The decomposer's prompt carries what previous goals recorded —
  which files siblings fought over, where work here lands, what a task cost, what a person
  rejected — bounded and summarised by `memory.py` (§27). Nothing else in the pipeline has a
  memory, and this one is a projection over stores that already exist rather than a new one.

---

## 17. Delegating a goal (`--delegate`)

`--plan-only` produces a task graph; `--delegate` runs it. One **Session** per **Task**, in
dependency order, each in its own worktree on its own branch:

```powershell
python -m orchestrator "Add rate limiting to the API" --delegate
python -m orchestrator "Add rate limiting to the API" --delegate --parallel 3
python -m orchestrator --goals            # what has been delegated
python -m orchestrator --show-goal latest # one goal, its tasks, and their branches
```

### 17.1 One scheduler, not two

`delegation.max_parallel` is the only difference between running tasks one at a time and running
them at once; one is the degenerate case of the other. A separate sequential path would have
been a second set of bugs in the part of the system that is hardest to reason about.

Concurrency is thread-based, because every session is subprocess-bound. The tracer takes a lock
around each emission and labels output with the task id per thread, so parallel work reads as
`[logger] …` / `[docs] …` rather than as interleaved noise.

### 17.2 How a task's workspace is built

| The task | Starts from |
| --- | --- |
| has no dependencies | the goal's base commit (the repo's HEAD when the goal began) |
| has one dependency | that dependency's branch |
| has several | the first dependency's branch, with the rest **merged forward** into the fresh worktree |

A dependency its dependent cannot see is not a dependency, so the merge is what makes
`depends_on` mean anything. It is not the auto-merge invariant 3 forbids: that rule protects the
user's branches, and this writes only into a scratch worktree the orchestrator just created.
When the merge conflicts, nothing is guessed — the task is skipped as `skipped_conflict` and a
person is named (invariant 11).

### 17.3 Collisions

Once every branch exists, the scheduler compares what each task changed
(`git diff base...branch`). Two **siblings** — neither depending on the other — that touched the
same path are a collision: both are flagged `needs_attention`, the overlapping paths are named,
and the goal reports `blocked`. Nothing is merged and nothing is reordered. A task built *on
top of* another sharing a file is cooperation, not conflict, so dependency pairs are excluded.

The brief each session receives lists its sibling tasks and says not to implement them. That is
the cheap half of collision control: most overlaps come from an agent helpfully doing its
neighbour's work.

### 17.4 Goal budgets

`budget.max_total_tokens` bounds one session. A decomposition multiplies that by however many
tasks it emitted, so `budget.goal_max_total_tokens` and `budget.goal_max_duration_seconds` bound
the goal. They are checked **between waves** — the moment the next spend is about to be
authorised — never mid-session, for the same reason a session's own budget is not checked
mid-agent (§9.3). Remaining tasks become `skipped_budget` and the goal reports
`budget_exhausted`.

### 17.5 What a goal leaves behind

```text
.orchestrator/goals/<goal_id>/
  goal.json     # metadata, the plan, and the derived summary
  events.jsonl  # goal_started, goal_plan, task_started, task_finished, collision, goal_finished
```

Plus one branch per delivered task, and one run-store record per session — each carrying
`goal_id` and `task_id`, so a goal's tasks can be traced to the sessions that ran them.

Task states (`queued`, `running`, `done`, `failed`, `blocked`, `needs_attention`,
`skipped_dependency`, `skipped_budget`, `skipped_conflict`) and goal statuses (`completed`,
`partial`, `failed`, `blocked`, `budget_exhausted`, `empty`) are **derived** from those facts by
`status.py`, never written down by the scheduler. `--show-goal` re-derives a goal's whole
summary by replaying its log, which is why a goal killed mid-flight still reports exactly what
each of its tasks got to.

### 17.6 What delegation deliberately does not do

* **It never merges results back, and never publishes them.** A goal produces N branches and a
  report; integrating them is a human decision, exactly as it is for a single run. Nothing is
  pushed either — a branch crosses the network only when a person says so (§20).
* **It does not retry a task.** A session already owns repair attempts, escalation, and its own
  budget. A second session would be an unbounded loop around a bounded one.
* **It does not reorder or split tasks.** The plan is the decomposer's; the scheduler only
  decides when each task runs and what its workspace starts from.

---

## 18. The board and the office (`--board`, `--serve`)

Agent Orchestrator's central idea is a Kanban whose card positions are **derived** — never
hand-maintained. That is invariant 1, so the board is not new machinery; it is a projection
over facts this system already records.

```powershell
python -m orchestrator --board            # columns of cards in the terminal
python -m orchestrator --board --json     # the same projection, for anything that reads it
python -m orchestrator --serve            # the board and the office at http://127.0.0.1:8730
```

### 18.1 Columns

| Column | Derived from |
| --- | --- |
| Queued | a task whose session has not started |
| Working | a session running now |
| Needs You | `blocked`, a collision, a dependency that would not merge, a pending gate, a red check, or a requested change |
| In Review | a `before_merge` gate still waiting on a person, or a pull request in flight |
| Ready to Merge | delivered, and either cleared at that gate or never gated |
| Merged | its pull request was merged; the work has landed |
| Stalled | failed, errored, or skipped because something else did |

A card is a **task** of a goal, or a **standalone session** — most work still starts as a single
run, and a board showing only delegated goals would be a board of the minority. A session that
belongs to a goal never appears twice: one piece of work, one card.

Once work has been delivered (§20) the forge owns facts this project does not, and they move
the card too. The precedence is deliberate: a **merged** pull request outranks everything,
because it is the one fact that means the work has left the board; otherwise the gate is asked
first — a delivery cannot exist before someone cleared the gate that produced it — and only
then may CI send a cleared card back to *Needs You*. Work in a project that never enabled
delivery is `local`, which moves nothing: Phase 6 does not shift a single card until it is
turned on.

`board.py` is pure. The CLI and the web view call the same functions, so there is no second
definition of "In Review" to drift from the first.

### 18.2 The office

`--serve` opens a local page where each configured role is a character at a desk. They walk in,
work (with a thought bubble), mark their result, hand off to the next role, and sit back down.
The board's counts sit beside them, and anything in *Needs You* raises a banner.

It is driven by the run's own event log — `agent_started`, `agent_result`, `verification`,
`acceptance`, `run_finished` — read from two read-only endpoints (`/api/board`,
`/api/activity`). The store gained `agent_started` for exactly this: without it a reader could
only see agents that had already *finished*, and would have to infer who was working.

#### It watches a Goal, not the newest run

The office was built against `latest_activity`, which reads the **newest run directory**. With
one session that is right. With `--parallel 3` it was a lie: three sessions shared one row of
desks and the office followed whichever had written most recently. The board never had this
problem, because it is a projection across every goal and session; the office simply had no
such projection to consume.

`serve.goal_sessions` is that projection. The newest run is now only how the *goal* is found;
from there the goal's own log says which run each task was given (`goals.task_outcomes`), each
of those runs has a log of its own, and the two are paired. `latest_activity` carries the
result under `sessions`, so:

* every in-flight task gets **its own row of desks**, laid out in the order the goal started
  them, so a desk does not move when a sibling finishes;
* a task that was skipped or is still queued is named with no session, because an office that
  omitted it would be quietly claiming the goal is smaller than it is;
* a run that belongs to no goal reports itself as a single session, and lays out exactly where
  the single desk always was — which is why nothing that consumed this payload before had to
  change;
* the number of desks is bounded by `MAX_SESSIONS`, and when the ceiling has to drop something
  it drops finished sessions before live ones.

The page keeps **one high-water mark per session** rather than one for the office, because two
sessions number their events independently and a single counter silently swallows the slower
one. The daemon's stream still follows one run, so the poll runs alongside it and every event
is deduped per session: what the stream delivers first is not replayed, and a sibling's desk is
never stale.

Served by the daemon (§22) the page consumes the live stream instead of polling for events, and
falls back to its original poll when there is none — which is what keeps one file working
against both servers.

Deliberate constraints:

* **Loopback only, and read-only unless a mode was asked for by name.** The server binds
  `127.0.0.1`, and under `--serve` every route is a GET. `--design` (§21) adds exactly one
  route that writes, and it writes the team file rather than anything belonging to a run.
  `--review` (§18.3) adds the three routes that can decide something about a run, and nothing
  else. `--daemon` (§22) is the surface that can also *start* work, and §22.4 is the account of
  what makes that a deliberate act rather than an ambient one.
* **No network dependencies.** One self-contained HTML file: no CDN, no build step, no
  framework, so the office works offline. The characters are canvas 2D; swapping in a 3D
  renderer later changes nothing behind it, because the page consumes a projection.
* **It cannot slow a run down.** It reads files that are already being appended to and holds no
  lock the orchestrator wants.

### 18.3 Review mode (`--review`)

The friction the board left was the gap between *seeing* and *acting*: it showed a card, and
answering it meant typing a command with an id somewhere else. Closing that gap is only safe if
it is a **third mode** rather than a relaxation of the first two.

| Mode | May read | May write the team file | May decide about a run |
| --- | --- | --- | --- |
| `--serve` | yes | no | no |
| `--design` | yes | yes | no |
| `--review` | yes | no | yes |

Modes are not a ladder. Being allowed to compose a team is not being allowed to approve, and
collapsing the two would make one of them accidental; `--design` and `--review` are refused
together for the same reason. `/api/modes` reports which capability this server was given, so
a page renders the buttons it actually has — a button that exists but is always refused is
worse than no button, and a button on a read-only server is exactly the stray click the
read-only guarantee exists to prevent.

Review is three verbs and no more: `POST /api/review/approve`, `/reject`, `/deliver`. Anything
else under that prefix is a 404, not a 501, because it is not a thing this server can be asked
to do at all. Each one calls what the CLI calls — `serve.answer_gate` wraps
`approvals.resolve_approval`, `serve.deliver_card_by_id` wraps `__main__.deliver_card` — and
the daemon's `approve`, `reject` and `deliver` verbs (§22.4) now call the *same two functions*.
There is one implementation of "approve" in this project, not three, which is what stops two
surfaces drifting into two meanings of the word.

`deliver_card_by_id` takes the **board** as an argument rather than reading one for itself.
That is the point: the answer to "may this card be delivered" comes from the projection, so a
caller cannot smuggle in a card the board never put in *Ready to Merge*.

---

## 19. Approval gates (`approval.gates`)

`BLOCKED` is the verifier *discovering* that a human is needed. A **gate** is the configuration
saying so in advance:

```yaml
approval:
  gates:
    after_decomposition: false   # read the plan before any task runs
    before_task: false           # authorise each task individually
    before_merge: false          # delivered work waits to be cleared
  auto_approve: false            # the guardrail, made explicit
```

### 19.1 A gate records; it does not block

A gate never blocks a process waiting for a keystroke. A run that hangs on stdin cannot be
scheduled, watched, or resumed — and the whole point of a gate is that the person may not be
there. Instead it records a **pending approval** and the work stops at a clean boundary:

```powershell
python -m orchestrator --approvals                  # what is waiting
python -m orchestrator --approve <id> --note "..."  # clear it
python -m orchestrator --reject <id> --note "..."   # refuse it
python -m orchestrator --resume-goal <goal id>      # carry on
```

### 19.2 What each gate does

| Gate | Effect |
| --- | --- |
| `after_decomposition` | The plan is produced and recorded, then every task waits. The goal reports `awaiting_approval`. |
| `before_task` | Each task is authorised individually. A held task does not block its siblings — only what depends on it waits. |
| `before_merge` | Never blocks anything: the work is already done and on its branch. It decides whether the card reads *In Review* or *Ready to Merge*. |

### 19.3 Rules

* **A decision is a fact.** Approved, rejected, and auto-approved are all recorded, with who and
  when. `auto_approve` does not skip a gate — it *answers* it, and the record says so. A
  guardrail you cannot tell was on is not a guardrail.
* **Silence is not consent.** No decision means pending, and pending stops the work.
* **A decision is never silently overwritten.** Answering an already-answered gate reports that
  rather than pretending to act.
* **A gate that cannot be recorded is pending.** Failing open would quietly remove the check a
  person asked for.

### 19.4 Resuming

`--resume-goal` replays the tasks that already delivered and re-runs everything else — a failed
dependency, a spent budget, a task held at a gate. Delivered branches are reused as the base for
dependents, so a resumed goal builds on its own earlier work.

The session-level `--resume` gained a related fix here: a `blocked` session used to replay its
stored BLOCKED verdict and re-verify nothing, so a human's fix was never checked. A BLOCKED
verification is now never replayed — resuming *is* the person saying they acted.

---

## 20. Outward delivery (`--deliver`)

Everything a run produces stays on a local branch. `delivery.py` is the only module in the
project that crosses the network, and it does so under invariant 13: **never auto-push.**

```yaml
delivery:
  enabled: false       # nothing is pushed while this is false, whatever anyone approves
  remote: origin
  base: ""             # empty means the remote's default branch
  draft: true
  on_approve: true     # answering a `before_merge` gate delivers the work
```

```powershell
python -m orchestrator --board                # what is Ready to Merge
python -m orchestrator --deliver <card>       # push it, open a pull request
python -m orchestrator --deliveries           # what has been published
python -m orchestrator --refresh-deliveries   # re-read CI and review state
```

### 20.1 Two triggers, both a person

There are exactly two callers of `deliver()`, and both are someone acting:

* `--deliver <card>`, which refuses anything the board does not already place in *Ready to
  Merge* — delivered, verified, and cleared at whatever gate was asked for; and
* `--approve` on a `before_merge` gate, when `delivery.on_approve` is set. Pressing *Ready to
  Merge* is what publishes the work, which is what that gate was always for.

Both are gated on `delivery.enabled`, so a project that has not opted in cannot reach a remote
by accident. Turning `on_approve` off keeps the gate and requires the explicit command as well.

### 20.2 What a delivery records

One act — push the branch, open a pull request, write down what the forge said — stored as one
JSON record under `.orchestrator/delivery/<key>.json`, keyed by `<goal id>__<task id>` or by
run id. The record holds the push, the pull request's number and URL, its state, a one-word
check summary, the review decision, and its own append-only `history`.

The **state** is not in the record. It is derived from it by `status.derive_delivery_state`,
exactly as a session's status is derived from its facts (invariant 1):

| State | Means |
| --- | --- |
| `local` | nothing has crossed the network |
| `pushed` | the branch is on the remote; no pull request |
| `pr_open` | a pull request is open and nothing has come back |
| `checks_running` | the forge is still working |
| `checks_failed` | a check failed |
| `changes_requested` | a reviewer objected |
| `ready` | checks green, review not objecting |
| `merged` / `closed` | the forge has finished with it |
| `failed` | the delivery attempt itself did not work |

`local` and `failed` are deliberately different states: work nobody pushed is not work whose
push failed. So are `pushed` and `failed`: when `gh` is missing the branch still reaches the
remote and only the pull request does not, and saying so is the difference between "try again"
and "go and look for the branch you already published".

### 20.3 The projection never reaches for the network

`--board` reads *recorded* CI facts. A refresh is a deliberate command that writes a new fact,
never a side effect of looking — otherwise the board would be slow, would fail offline, and
reading it would become an act with consequences. Every card therefore carries
`delivered_as_of`, and `--deliveries` prints it, because a card showing a stale check as
current would be the projection lying.

`refresh_delivery` also declines to ask about a merged or closed pull request: the forge has
finished with it and asking again cannot change the answer.

### 20.4 No keys, ever

The forge is driven through `gh`, the user's own locally-authenticated CLI, under the same
subprocess contract as every agent: `shell=False`, stdin detached, a timeout. Invariant 10
stands unchanged — this project reads, stores, and transmits no credential. No `gh` means a
refusal with a reason, not a broken run.

---

### 20.5 What happens to a delivered branch afterwards

Delivery and retention meet in §7.3: a merged branch becomes *more* sweepable than an
unreviewed one, and pruning it annotates its delivery record rather than deleting it. A card
that reads *Merged* on the board goes on reading *Merged* after its local branch is gone.

---

## 21. The design surface (`--design`)

`orchestrator.yaml` is already the team model — who is in the pipeline, in what order, on which
model, playing which role. So the node editor is a **view over that file**, not a second
description of the same thing, and `teams.py` is correspondingly small.

```powershell
python -m orchestrator --teams                # the current team, and the templates
python -m orchestrator --apply-team careful   # start from a template
python -m orchestrator --design               # compose one visually at 127.0.0.1:8730/design
```

### 21.0 The evidence beside the choice

`--stats` has always known which agent, model and role pairings actually pass verification and
what each costs. Until Roadmap 8.3 the only place that knowledge lived was a report you had to
go and read, while the dropdown that acts on it sat here with nothing beside it — which made
composing a team an act of preference rather than of evidence.

`stats.evidence_for_choices` folds the same `pairings` rows three ways, because a dropdown
does not offer a pairing: it offers an **agent**, or a **model**, or fills a **role**. Each map
is keyed by exactly what the chooser has in hand (`by_agent`, `by_model` keyed
`<agent>/<model>`, `by_role`), and each row carries its denominator.

Two properties are load-bearing:

* **Never judged is not always failed.** A role with no recorded verdict reports `pass_rate:
  null`, never `0`. Reporting zero would be publishing a finding this project never observed.
* **Cost is weighted by how often a pairing ran.** Folding a rare pairing with a common one
  must not give them equal weight, or a single expensive outlier becomes the headline number.

`/api/stats` serves it — read-only, in every mode, bounded to the newest `EVIDENCE_RUN_WINDOW`
runs and cached for `EVIDENCE_TTL_SECONDS`, so a page that re-renders on every event does not
turn into a disk walk per frame. `design.html` renders it under each provider dropdown, each
model in the escalation ladder, each role, and on the agent chip itself; the cockpit's Team
pane renders the same rows. Nothing new is measured: it is a projection over a projection,
which is why it cannot disagree with the table `--stats` prints.

### 21.1 Templates

| Template | Shape |
| --- | --- |
| `solo` | implementer + verifier, `any` consensus, 1 repair attempt |
| `fast` | one of each role, `unanimous`, 1 repair attempt |
| `balanced` | two verifiers under `majority`, an implementer with an escalation ladder, 2 attempts |
| `careful` | two researchers, three verifiers, `unanimous`, 3 attempts |

A template is a *starting point*, not a preset the engine knows about: applying one writes
ordinary `agents:` entries a person can then edit. Every template names only models the default
catalog ships with, because a template that produced a config which would not load is not a
template, it is a trap — and a test asserts exactly that for all four.

### 21.2 The editor draws phases, not rows

A **phase** is consecutive same-role agents, which is what `graph.py` already means by one. The
editor stores phases directly and flattens them on save, so the drawing and the file cannot
disagree about what a phase is. Within a phase, several agents are an ensemble; one agent with
several models is an escalation ladder (§9.2). Two implementers in one phase is refused *before*
you save, mirroring the same refusal in `validate_config`.

The page is one self-contained HTML file — no CDN, no build step, no framework — for the same
reason the office is: it has to work on a machine that is offline.

### 21.3 The write is surgical, validated, and backed up

`orchestrator.yaml` is mostly commentary explaining every knob. Round-tripping it through a
YAML dumper would produce a valid file that had lost the reason for every value in it. So a
write replaces exactly the lines it owns — the `agents:` block, `max_repair_attempts`, and
`verification.consensus` — and leaves every other byte, comment included, alone.
`replace_top_level_block` stops at the next line starting in column zero, which is the *comment
banner* introducing the next section, and leaves the blank line before it in the tail so that
rewriting is idempotent rather than growing the file each time.

Two rules make this safe enough to expose to a browser:

* **Validate before writing, never after.** The candidate text is parsed and run through
  `validate_config` first, and a failure leaves the file untouched. A config that cannot load
  is a project that cannot run, so an editor that could produce one would be a foot-gun with a
  GUI.
* **Never overwrite without a copy.** Every write leaves `orchestrator.yaml.bak-<stamp>` beside
  the file, and a backup that cannot be made cancels the write.

### 21.4 Why this does not break invariant 12

`--serve` is unchanged: read-only, every route a GET. `--design` enables exactly one route,
`POST /api/team`, which edits the team file and nothing else. No route on either server can
start a run, answer a gate, or push a branch — the distinction invariant 12 draws is between
*observing a run* and *steering* it, and composing a team is neither. The page is data, so the
posted document is normalized and revalidated server-side before it is believed.

---

## 22. The daemon (`--daemon`)

Everything up to §21 is a command that exits. A dashboard you can *drive* has nothing to be
clicked into, so Phase 8 wraps the engine in a persistent local process.

```powershell
python -m orchestrator --daemon          # 127.0.0.1:8740, and it opens the cockpit
python -m orchestrator --daemon 9001     # a port of your choosing
python -m orchestrator --daemon --no-browser    # what the desktop shell passes
```

It serves everything `--serve` serves, and adds three things a command that exits could not:

| | Route | What it calls |
| --- | --- | --- |
| Start a goal | `POST /api/control/start` | the same planner and `run_goal` that `--delegate` calls |
| Cancel one | `POST /api/control/cancel` | nothing — it sets a flag the scheduler's next authorisation reads |
| Answer a gate | `POST /api/control/approve` / `reject` | `approvals.resolve_approval`, exactly as `--approve` does |
| Publish | `POST /api/control/deliver` | `deliver_card`, exactly as `--deliver` does |
| Watch | `GET /api/stream` | `events.jsonl`, relayed as server-sent events |

`daemon.py` subclasses `serve.py`'s handler rather than copying it, so every read route keeps
one definition and what is new here is only authentication, control, and the stream.

### 22.1 The token, and why localhost was not enough

A loopback port that can *start work* is a bigger target than one that can only read, because
any page open in the same browser can already reach `127.0.0.1` with a `fetch`. Three things
answer that, and all three have to hold:

* **A per-launch token.** Minted at startup, never reused across launches, required on every
  `/api/*` route. It is substituted into each page the daemon serves — a same-origin script can
  read it back out of the document, a cross-origin one cannot — so it never travels in a URL,
  a title bar, or a referrer. `EventSource` cannot set a header, so the stream also accepts
  `?token=`; that is the one exception and it is still same-origin only.
* **An Origin check on every request.** A browser sets `Origin` and a page cannot forge it. A
  request whose Origin exists and is not this daemon's own is refused *before its body is
  read*. A missing Origin is allowed, because that is a script or a `curl` — which browser
  ambient authority does not reach, and which had to know the token anyway.
* **The token in a header.** Requiring a custom header is what forces a browser to preflight a
  cross-origin request, and the preflight is what it fails.

The token is also written to `.orchestrator/daemon.json` with owner-only permissions, which is
the handshake a local client reads: `tools/daemon_client.py`, and the desktop shell (§25).
That file is how a script can drive the daemon at all, and it is removed when it stops.

### 22.2 Jobs, and what cancelling honestly means

A **Job** is one piece of work the daemon was told to start. It is not a new place facts live:
the goal it starts is recorded by `goals.py` and each session by `store.py`, exactly as
`--delegate` records them. A Job holds only what is true of the *supervision* — what was asked,
when, and whether it is still going.

* **Several at once.** `--parallel N` already runs several sessions within one goal; the daemon
  holds several *goals* open. `MAX_OPEN_JOBS` bounds that, for the same reason `budget.py`
  bounds a run: a UI that can start work with a click can start work with a stuck click.
* **It refuses rather than queues.** At the ceiling, a start is refused with a reason. A UI
  that silently banked clicks would be a scheduler, and a scheduler is precisely the thing
  invariant 12 exists to prevent.
* **Cancelling is cooperative, and says so.** An agent CLI already running is *not* killed —
  half-killing a subprocess mid-write is how a workspace gets corrupted. What stops is the
  authorisation of further work: no task starts after the flag is set, which is the same thing
  `stop_on_failure` already does between waves.
* **A crashed goal does not take the daemon with it.** The job fails, the error is recorded on
  it, and the next start still works.

### 22.3 The stream

`GET /api/stream` follows the newest run (or a named one) and relays each event as it is
appended. `EventTail` tracks a byte offset rather than re-reading the tail — the office's old
poll re-parsed 200 lines every 900 ms, which is fine for one page and wrong as a foundation.
A log that was replaced is noticed by size and re-read from the start; a half-written final
line is skipped rather than fatal.

The replay a new connection gets is read from offset zero and *then* filtered by `since`,
rather than tailed and then sought — anything appended between those two steps would otherwise
be lost exactly when a run is busiest.

The stream is another *reader* of `events.jsonl`, never a second source of truth for it.
`office.html` now consumes it when there is one and falls back to its original poll when there
is not, which is what keeps that page working against both servers.

### 22.4 How invariant 12 is revised, precisely

Invariant 12 said watching is not steering, and for six phases the server could therefore never
write. Its *purpose* — a decision must always be a deliberate human action, never something a
schedule or an external signal triggered — is unchanged. What changes is **where** that
deliberate action may be taken from: a click in a single-user, locally-running app is exactly
as deliberate as typing a command.

What holds that line in code rather than in prose:

* `Daemon.control` is a **closed list** of verbs. Anything not named there is not something the
  UI can do, and an unknown verb is a 404 rather than a dispatch.
* There is no timer, no webhook, no retry-until-it-passes, and no route that acts on anything
  the daemon did not itself present a button for.
* Every enforcement point is where it always was. `workspace.py` and `delivery.py` are still
  the only code that touches a branch or a remote; a daemon calling `deliver()` is a person's
  click reaching the same gate `--deliver` reaches. `budget.py`'s ceilings do not become
  optional because a click started the run.

---

## 23. The cockpit

Three views now exist, and they answer three different questions:

| View | Organised by | Answers | Can act |
| --- | --- | --- | --- |
| The board (`--board`, §18) | work item | "what is the state of everything I asked for" | no |
| The office (`--serve`, §18) | one run's roles | "who is working right now" | no |
| The cockpit (`--daemon`) | **role** | "what is my team, and what is each member doing" | **yes** |

The cockpit does not replace either. The office in particular stays as the dependency-free,
offline fallback it was valued for.

### 23.1 Role columns, not task columns

One column per configured role, in `teams.phases_of()`'s order — which is `graph.py`'s
execution order, which is why "left to right" is a true statement about the page rather than a
metaphor. A project with five roles gets five columns; the default four gets four. One card per
agent in that role, so an ensemble or a quorum of verifiers is several cards stacked in one
column.

Each column's "+" opens the add-agent flow and posts a whole team to `POST /api/team` — the
same route, the same `normalize_team` / `validate_team` / `write_team`, the same backup. The
cockpit **relocates** the team editor into the live dashboard; it does not reimplement it.

### 23.2 Where a card's state comes from

`cockpit.py` is pure, for the same reason `board.py` is. It derives each card's state from the
two events Phase 4 added and from nothing else:

* `agent_started` → `working`
* `agent_result` with a failing verdict or an error → `failed`
* `agent_result` otherwise → `done` (a researcher returns no verdict; finishing is all it
  claims, and calling that a failure would be the projection lying)
* nothing recorded → `idle`

A repair attempt re-starts an agent, so a second `agent_started` correctly puts the card back
to `working`; tokens accumulate across attempts. A column is `working` while any of its agents
is; once every member has finished, it is `failed` only when *all* of them did — matching
`make_sync_node`'s own fatal condition (invariant 8) exactly, not a looser "any failure"
reading. Verified against a real run: an ensemble of two researchers where one errored and one
succeeded continued straight through to a PASS, and the column reads `done` throughout, with
the failed member's own card still carrying the warning rather than a column-wide red that
would have misreported a run that was genuinely succeeding.

States are keyed by `agent/role`, because that is all an event carries. A role with several
*identical* agents therefore shows the same state on each of its cards. That is honest: the
event log genuinely does not distinguish them.

### 23.3 One read, not five

`GET /api/cockpit` returns the team, the catalog, the templates, the columns, the board counts,
the pending approvals, the deliverable cards, and the jobs — in one response. A dashboard
assembled from five independently-timed polls shows five different moments at once.

The page then listens on the stream and re-reads when an agent starts or finishes, with a slow
poll behind it. The poll is not redundant: the board is a projection over every store, so it
can change for a reason no run event announced — a delivery's CI state, say.

### 23.4 What it can act on, and what it will not guess

Two different things reach the *Needs you* panel, and conflating them would be a lie: a **gate**
is a question with an id that can be answered, and a **card** in the Needs You column may simply
be work that failed. Only the first gets buttons. Answering one prompts for a note and can be
cancelled — a gate is never answered by an accidental click — and delivering asks for
confirmation naming the branch it would push.

---

## 24. The live agent terminal

`launcher.py` could always run an agent where a person could watch it: `visible=True` opens a
real terminal and streams the CLI's output to it. What it could not do was let anything *else*
see that stream — the terminal is a separate OS window, and nothing relayed its contents
anywhere. `terminals.py` is exactly that one missing capability.

### 24.1 One hook, in one place

`launcher.set_output_recorder()` installs an optional callback that receives the agent, role,
model and command about to run, and returns a sink — or `None` to leave the execution exactly
as it was. The daemon installs one; nothing else does.

This is deliberately the entire integration surface. **No agent adapter and nothing in
`graph.py` changes to gain a live terminal**, and with no recorder installed the headless path
is the `subprocess.run` it has always been. When a sink is present, `run_captured` runs the
same command through pipes with reader threads and returns an identical result — same stdout,
same stderr, same return code, same timeout behaviour — so the pipeline above it cannot tell
the difference. A sink that throws is swallowed: a panel that went away must not stop an agent.

### 24.2 Transcript, not a source of truth

A terminal buffer is a **bounded ring** of sequenced chunks (`MAX_TERMINAL_CHARS`, and
`MAX_TERMINALS` of them). A reader holds the last sequence it saw and asks for what came after;
if that is older than what the buffer still holds it is told the transcript was **truncated**,
rather than being quietly handed a gap.

What a run *means* is still `store.py`'s `agent_result` and `status.py`'s derivation. The buffer
is dropped when the daemon stops and nothing reads it back — it is evidence you watch, not
evidence the system reasons from.

Per Roadmap §10.2, this is deliberately **not a thought-process parser**. It shows the raw
output an agent's CLI is already producing, so no provider changing its format can break it.

### 24.3 A pipe for most agents, a real pty for the one that needs it

A true pseudo-terminal makes a CLI believe it is talking to a terminal, which is what makes
colour, cursor control, and an actual interactive program appear. `run_captured_pty` uses one
via the standard library's `pty` on POSIX, or `pywinpty` (ConPTY) on Windows - an optional
dependency `pty_available()` checks for rather than assumes, since its absence should degrade
a live view, not break the agent it would have shown.

It is deliberately not used everywhere. A pty changes *where* output goes, not *what a
headless invocation produces*: an agent run with an explicit "print machine-readable JSON"
flag prints exactly that JSON on a pty too - verified directly, by piping one straight through
a real pseudo-terminal and getting back the identical blob. Claude Code and antigravity are
always run that way, so `run_captured` (a plain pipe) is the right tool for them; the daemon
renders their buffered result as a finished answer once the process exits rather than a live
scroll of JSON syntax (§23.2's cockpit panel does the same, client-side, for the same reason).

`run_captured_pty` earns its keep for the one agent that is genuinely different: OpenCode
ships a real interactive TUI (`opencode attach ...`) behind its own client/server API. When a
recorder is installed, `opencode_tui.py` spawns that `attach` process locally, attached to a
pty, instead of asking the Antigravity IDE's own bridge extension to display it - the
mechanism this project always used, and still falls back to when no recorder is present. The
result is OpenCode's actual interactive program, colour and all, live in the cockpit's panel,
with no IDE or bridge required. The panel renders SGR colour, bold and dim, drops cursor
movement, and collapses a carriage return that is rewriting a progress bar; it is not a full
terminal emulator and does not claim to be, but it is the real program's real output.

---

## 25. The desktop shell

`desktop/` is an Electron window around the daemon: open a project folder, get a window; the
window starts that project's daemon on a free port, waits for its **own handshake file** to
appear with that port (not a fixed sleep, not a guess from a log line), loads the cockpit it
serves, and kills the daemon when it closes.

It is deliberately thin, and thinness is the property worth protecting:

* It spawns `python -m orchestrator --daemon <port> --project-root <folder> --no-browser`, and
  that is the whole of its relationship with the engine.
* `preload.js` exposes one boolean. The page runs sandboxed with `contextIsolation` on and no
  Node integration, so the shell adds **no way to act that a daemon route does not govern**.
* If `desktop/` were deleted, `python -m orchestrator --daemon` would lose a window frame and
  nothing else. Tests assert each of these against the files on disk, because a claim about
  what a shell may do is only worth making against what is actually there.

**Why Electron, not Tauri.** The shell has to embed a terminal (§24), and `node-pty` is the
mature option for that on Windows, where this project is developed. Tauri would ship a smaller
app but would add Rust — a language nothing here uses — to buy a smaller download for a tool
that runs beside a checkout of the repository it drives. The decision is also cheap to revisit:
`desktop/` is the entire commitment.

---

## 26. The Explorer, and the shell around it

Every view before this one answers a question about *the work*: what is the state of each card
(§22), who is running right now (§23), what did this process print (§24). None of them can
answer the question a person asks the moment an agent finishes, which is simply **what did it
write?** `explorer.py` is that question and nothing else - a read-only projection of a
directory tree and the text in it.

### 26.1 Three kinds of root, not one

An orchestrator that gives every run its own worktree (§13) has more than one tree worth
looking at, and conflating them would be the same mistake as storing a card's column:

| Root id | Tree | Why it is offered |
| --- | --- | --- |
| `project` | the user's own checkout | invariant 4 says nothing here may touch it; reading is not touching |
| `worktree:<run_id>` | `.orchestrator/worktrees/<run_id>` | where an agent's edits actually land, and so the tree most wanted after a run |
| `runs` | `.orchestrator/runs` | the artefacts and the event log themselves |

`roots()` builds the list from what is on disk, so a worktree that `finish_worktree` removed
stops being offered rather than becoming a broken entry. A request names a **root id**, never a
directory, and `root_path()` resolves it by identity - an id that is not in `roots()` is not a
place a read can begin.

### 26.2 Confinement, by construction

`resolve_within(root, relative)` is the whole of the security property, and it is deliberately
boring: realpath both ends - which follows symlinks, junctions and `..` - then require that the
candidate *is* the root or is under it **with a separator between**. Every function that takes
a relative path goes through it, so a `..` climb, an absolute path, a drive letter and a
symlink pointing out of the tree are all the same answer: `None`.

Two details are worth naming because they are the two ways this is usually got wrong:

* **Prefix matching is not containment.** Without the separator check, a root of `/project`
  accepts `/project-secrets`. There is a test for exactly that sibling.
* **The absolute check happens before separators are trimmed.** Stripping first turns
  `/etc/passwd` into the root-relative `etc/passwd`, so the check never sees what it is
  checking. There is a test for that too.

### 26.3 Read-only, and structurally so

Invariant 12 said watching is not steering, and §10.6 revised *where* a deliberate action may
be taken from, not whether one is required. **Browsing is watching.** There is no write in
`explorer.py` - no `open(..., "w")`, no `mkdir`, no `unlink` - so an Explorer cannot become an
editor by accident, and `daemon.py` exposes it on three GET routes only: `/api/explorer` (one
directory, decorated), `/api/file` (one file's text, bounded) and `/api/find` (a file-*name*
search, for the palette). A POST to any of them is a 404, and `control()` remains the closed
list it was - there is no `explore` verb, and a test asserts there is not.

### 26.4 Bounded, like everything else local

A repository can hold a million files and one file can hold a gigabyte, so: a listing is capped
at `MAX_ENTRIES` and *says* it was capped; a read is capped at `MAX_FILE_BYTES` and says how
much of the file it is showing; a file that is not text is reported as binary rather than
decoded into replacement characters; and `find_files` has a visit ceiling so it is safe to call
from a keystroke. Nothing recurses on its own - the UI asks for one directory at a time, which
is one `scandir` per expansion rather than a walk.

The git overlay is **decoration, not fact**. `git_status()` returns `{}` for every failure -
no git, not a repository, a slow status - because a tree that renders without decoration is
correct, just less informative, and a tree that blocks on git is not.

### 26.5 The shell the cockpit became

`cockpit.html` is now a console rather than a dashboard: an activity rail, a sidebar with four
panes (Explorer, Team, Board, Terminals), a tabbed stage where the pipeline and any opened
files live, an inspector, a terminal panel with one tab per agent, a status bar, and a command
palette on `Ctrl K`. Three properties are load-bearing rather than cosmetic, and each is tested
against the file on disk:

* **Nothing is loaded from the network.** No framework, no CDN, no web font. The daemon is a
  loopback process that must render with the network unplugged, and a build step between
  `daemon.py` and what a person sees would be a second thing to keep true.
* **The only writes are the control routes and the team editor.** A test scans the page for
  every `method: "POST"` and asserts the set of routes reached that way is exactly
  `/api/control/` and `/api/team`.
* **Motion is motivated, and optional.** Five animations exist, each communicating one thing:
  the titlebar hairline (a job is running), the liveness dot (the stream is connected), the
  card sweep (this agent is working, and how far through is not knowable), the card flash (this
  changed while you were looking elsewhere), and the view/palette entrance (this came forward).
  All of them are disabled under `prefers-reduced-motion`.

The syntax highlighting is a **scanner, not a parser**, and is honest about that: comments,
strings, numbers and keywords, with three pieces of state carried between lines - inside a
block comment, inside a triple-quoted string, inside a fenced code block - because those are
the three things that genuinely span lines. It cannot tell a type from a variable and does not
pretend to.

---

## 27. The planner's memory (`planning.memory`)

Every phase up to here built the **body** of a persistent project orchestrator: a decomposer, a
scheduler, isolation, a board, gates, delivery. None of it built the **memory**. `--plan-only`
decomposed every goal cold — the decomposer saw the project's context and the goal text and
nothing else: not the decompositions that came before, not which tasks collided last time, not
which areas of this repository are dense, not what a task of this shape actually cost.

### 27.1 The decision

A Project **does** accumulate knowledge across Goals. Its lifetime is the repository, not a
run, which makes it the first thing in this codebase with that lifetime.

### 27.2 It is not a new store

Every fact the memory holds is already recorded somewhere:

| Fact | Where it already lives |
| --- | --- |
| what a delivered task touched | the task's branch, via `workspace.changed_paths` |
| what a task *said* it would touch | the plan's `areas`, in the goal's `goal_plan` event |
| which siblings collided, over what | the goal's `collision` events |
| what a task cost | the task outcome's `tokens` and `duration_seconds` |
| what a person rejected, and why | the approval store's decided records |

So `memory.py` is a **projection over the stores**, exactly as `board.py` is. That keeps
invariant 1 intact one level higher: the memory cannot drift from what happened, because it
*is* what happened, read back. Nothing writes a memory, and a test asserts that building one
leaves the project directory byte-identical.

### 27.3 What it says, and how it says it

`project_memory` folds the observations into five things, and every one carries the count it
was computed from:

* **Collisions, keyed by path.** A task id is unique inside its goal and means nothing in the
  next one; the *path* is what the next decomposition can avoid. The task pairs are kept as
  evidence, not as the key.
* **Hot paths**, counted by **task**, not by file change — a task that rewrote forty files in
  one directory counts once for that directory, because the question is whether tasks keep
  landing there, not how much churn there was.
* **Cost**, separated into what delivered and what did not, as medians over the executions that
  actually reported usage. A cost nobody reported is `None`, never zero.
* **Rejections**, with the note the person wrote.
* **Shapes** — how goals here have been decomposed before.

Two honesty rules run through it. When retention (§7.3) has swept a branch, git cannot diff it
and the task's declared `areas` stand in — marked `declared`, because presenting a prediction
as an observation would be a lie. And a memory built from fewer than `THIN_SAMPLE` goals says
so in the block itself, so a planner reads it as an anecdote rather than a pattern.

### 27.4 The budget

A memory that grows without a ceiling is a context window that eventually fails, exactly as the
repair loop needed a ceiling (§9). Three bounds hold it:

| Bound | What it stops |
| --- | --- |
| `max_goals` | reading a history that describes a repository which no longer exists |
| `MAX_GIT_QUERIES` | one memory turning into a hundred subprocesses while a person waits |
| `budget_chars` | the block becoming the reason a prompt does not fit |

`summarise` adds sections **whole**, most actionable first, until the next one would not fit,
and says how many it dropped. The order is the priority: a collision is a pair of tasks that
should not have been emitted together, which is the one thing here that can change a
decomposition, so it is the last thing dropped; a list of past goal shapes is the first.

### 27.5 Where it reaches the model, and how to read it

One place: the decomposer's prompt, under its own `### WHAT THIS REPOSITORY HAS TAUGHT US:`
heading, framed as *recorded facts, not instructions*. Building it can never fail a run —
`memory_section` returns `""` on any problem, and `""` is exactly the cold prompt this project
used before. Turning `planning.memory.enabled` off returns that prompt permanently.

A prompt addition a person cannot read is one they cannot argue with, so it is inspectable:

```powershell
python -m orchestrator --memory                # the report
python -m orchestrator --memory --plan-only    # the exact block the prompt will carry
python -m orchestrator --memory --json         # the projection
python -m orchestrator --memory 5              # over the last 5 goals
```

The prize is concrete, and it is the collision policy question answered by **prevention** rather
than by resolution: a decomposer that knows two tasks touching `config.py` collided last week
stops emitting that pair.

---

## 28. Module map and extension points

| Module | Owns |
| --- | --- |
| `__main__.py` | CLI surface: run, `--doctor`, `--runs`, `--show-run`, `--resume`, `--stats`, `--prune-runs`, `--list-models`, `--board`, `--serve`, `--design`, `--daemon`, `--teams`, `--deliver`. |
| `graph.py` | Topology, node factories, sync nodes, routing, `finalize_node`. |
| `config.py` | Schema, validation, defaults, `get_*_config` accessors. |
| `context.py` | Project root resolution and redacted context collection. |
| `preflight.py` | Agent probing and the readiness report. |
| `workspace.py` | Worktree creation, commit, retention, reattachment, branch listing. |
| `prune.py` | The retention sweep and its plan/execute split, including the shorter threshold a merged branch answers to. |
| `archive.py` | Retention for the run *store*: judging which runs never really ran, and moving them out of the dataset reversibly. Deletes nothing. |
| `budget.py` | Spend measurement and ceiling evaluation (pure). |
| `acceptance.py` | Running the project's own check, and rendering its result as evidence. |
| `decompose.py` | Parsing, validating, and ordering a task graph (pure). |
| `scheduler.py` | Running a goal's tasks: waves, concurrency, dependency workspaces, collisions. |
| `goals.py` | Goal persistence and read-back, and replaying a goal's log into task outcomes. |
| `board.py` | The board projection: cards and columns from facts (pure). |
| `approvals.py` | Approval gates: requesting, listing, and deciding them. |
| `delivery.py` | The only module that crosses the network: push, pull request, refresh, and the delivery store. |
| `teams.py` | The team model, the templates, and the surgical rewrite of `orchestrator.yaml`. |
| `serve.py` | The loopback server behind `--serve` (read-only), `--design` (one writable route) and `--review` (three deciding routes); the goal-session projection the office draws; and the two acts review and the daemon share. |
| `daemon.py` | The persistent process behind `--daemon`: the token, the control verbs, jobs, and the live stream. |
| `cockpit.py` | The role-column projection: agent states from events, columns from the team (pure). |
| `terminals.py` | Live agent output: the bounded ring buffers, the registry, and the captured runner. |
| `explorer.py` | The read-only file tree: root resolution, confinement, listings, bounded reads, the git overlay, name search (pure but for the filesystem). |
| `web/office.html` | The office view: one self-contained page, no dependencies. |
| `web/design.html` | The team editor: one self-contained page, no dependencies. |
| `web/cockpit.html` | The cockpit: role columns, the goal bar, the terminal panel. No dependencies. |
| `desktop/` | The Electron shell (§25). Spawns a daemon and loads its cockpit; knows nothing else. |
| `status.py` | Status vocabulary, consensus resolution, `derive_*` (pure). |
| `store.py` | Run persistence and read-back. |
| `resume.py` | Event-log replay and the phase-replay decision. |
| `stats.py` | Aggregate analysis across stored runs, and the evidence projection the choosers render (read-only). |
| `memory.py` | What this repository has taught the planner (§27): the projection over the goal, run and approval stores, and its budget. |
| `metrics.py` | Token aggregation, invariant audit, summary table. |
| `tracer.py` | Structured events and console rendering. |
| `types.py` | `AgentResult`, `TokenUsage`, `VerificationRecord` and their constructors, including how many attempts one result took. |
| `launcher.py` | Process and terminal launching. |
| `agents/` | One adapter per provider; `verifier.py` owns verdict parsing. |
| `prompts/` | Role prompt builders. |
| `skills/` | Discovery, registry, manifest formatting. |

**Adding an agent provider.** Write `agents/<name>.py` exposing a runner and an executable
resolver, register it in `get_runner`, `AGENT_EXECUTABLE_RESOLVERS`, and
`AGENT_VERSION_ARGS`, and add its models to the catalog.

**Adding a role.** Add it to `roles:` with a responsibility and to `agents:` in pipeline
order. `make_role_node` falls back to a generic prompt for unknown roles; add a branch and a
prompt builder to give it a real one. A role that should participate in resume needs a rule in
`should_replay`.

**Adding a status.** Add the constant, its description, and its membership in
`TERMINAL_STATUSES` / `UNSUCCESSFUL_STATUSES`, then derive it in `derive_status` from a
recorded fact — never from a value a node remembered to write. The same applies one level up:
task states and goal statuses live in `status.py` and are derived from what sessions recorded.

**Adding a reason a task cannot run.** Record it as a skip with its own `skip_reason` in the
scheduler, map that reason to a task state in `derive_task_state`, and decide where it sits in
`derive_goal_status`'s precedence. Nothing else needs to change: the report, `--show-goal`, and
the board all read the derivation.

**Adding a forge.** `delivery.py` separates the act from the derivation on purpose: everything
provider-shaped lives in `push_branch`, `create_pull_request`, and `read_pull_request`, which
return this project's own record shape rather than the forge's. A second forge is those three
functions plus a way to choose between them; `status.derive_delivery_state` and `board.py`
never learn that it exists.

**Changing how a flaky agent is handled.** The retry loop lives in `make_role_node` around
the single `runner(...)` call, and its whole contract is that the phase still emits one
`AgentResult` (§9.0). A new failure mode belongs *inside* that loop as another `reason`, never
as a second result appended to `agent_results` — the moment a retry becomes a row in that list,
consensus and `--stats` are both silently wrong.

**Adding a fact to the planner's memory.** Observe it in `memory.observe` from a store that
already records it — never by writing a new one — fold it in `project_memory` with the count it
was computed from, and give it a section in `_sections` positioned by how much it could change
a decomposition. `summarise` and `--memory` pick it up with no further change, and the budget
applies to it automatically because sections are added whole.

**Adding a mode to the local server.** Give `make_handler` its own flag, turn on exactly its
own routes, report it from `/api/modes`, and refuse it in combination with the others in
`__main__`. A mode that is a superset of another is not a mode; it is a relaxation, and §18.3
is the reason this project does not have one.

**Adding a control verb.** Add it to `Daemon.control`'s closed list and give it a method that
calls the module the CLI already calls — never a new implementation of the same act. If it has
no button on a page the daemon serves, it does not belong there at all (§22.4).

**Adding a team template.** Add an entry to `TEAM_TEMPLATES` using models the *default* catalog
ships with, and it appears in `--teams`, `--apply-team`, and the design surface at once. The
test that validates every template against a real config will tell you if it would not load.

---

## 29. Testing

```powershell
.venv\Scripts\python -m unittest discover -s tests -t . -q
```

Every agent subprocess is mocked, so the suite runs offline in seconds and never launches a
real CLI. Tests that need git create a throwaway repository in a temp directory and remove it,
and skip themselves when git is absent. `tests/test_tier0_tier2.py`, `test_tier1.py`,
`test_tier3.py`, `test_tier4.py`, `test_tier5.py`, `test_tier6.py`, `test_tier7.py`, and
`test_tier8.py` are organized by the feature area they cover rather than by module, so the
invariants in §1 each have an obvious home.

Two kinds of test deliberately use the real thing rather than a mock, because a mock could not
prove what they exist to prove: the acceptance-gate tests run real subprocesses
(`sys.executable` with a chosen exit code), and the delegation tests run real git — creating
branches, merging dependencies forward, and detecting collisions in a throwaway repository. The
office tests start the real server and make real HTTP requests, including a POST that must be
refused: a read-only server is a claim worth testing rather than asserting.

The delivery tests extend that. `TestDeliveringForReal` pushes a real branch into a real bare
repository on disk and then reads it back out of the remote, because "the branch reaches the
remote" is not something a mocked `git push` can demonstrate. Only the forge itself is mocked,
since `gh` may not be installed and a test must never open a pull request. Two tests exist
purely to hold invariants that would otherwise erode quietly: one asserts that building a board
makes no subprocess call at all, and one asserts that `--serve` still refuses `POST /api/team`
now that `--design` accepts it.

`test_tier8.py` extends that pattern to the app. The daemon tests start a real server on a real
port and make real HTTP requests — including the ones that must be refused for want of a token
and for a foreign `Origin`, because "this cannot be driven by a page you did not open" is a
claim worth testing rather than asserting. The capture tests run real subprocesses and read
their output as it arrives, since liveness is the entire point of §24 and a mock could not show
it. And the desktop-shell tests read the real files in `desktop/`: they assert that the shell
starts a *daemon* rather than the engine, that it waits for the handshake rather than sleeping,
and that `preload.js` exposes no privileged bridge — because a claim about what a shell may do
is only worth making against what is actually on disk.

`test_tier10.py` covers Roadmap section 8, and its shape follows what each step actually
claims. The office tests build a real goal store beside real run stores and assert the pairing
between them, because "one desk per task" is a statement about two logs agreeing. The evidence
tests are mostly about what must *not* be said: that a role nobody verified reports no rate
rather than zero, and that folding a rare pairing with a common one does not give them equal
weight. The retention tests stub git and the delivery store to known answers so the *decision*
is what is under test, and one of them asserts that pruning leaves the delivery record on disk
with its merge intact. The review tests start three real servers, one per mode, and the useful
half of them assert refusals: that `--serve` cannot decide, that `--design` cannot approve, and
that `--review` cannot edit the team. The memory tests drive the projection from synthetic
observations so it is testable without a filesystem, and then check the budget the way a
ceiling has to be checked — that the most actionable section is the last one dropped, and that
a truncated memory says it was truncated.

`test_tier9.py` does the same for the Explorer, and its centre of gravity is the refusals. The
confinement tests are written the way a security property has to be — not "does the happy path
work" but "is every way out of the tree the same answer" — and they include a symlink pointing
out of the root, because that is the escape a string comparison on the *requested* path cannot
see. The route tests start a real daemon and assert that each Explorer route needs the token,
refuses a foreign `Origin`, refuses a traversal over the wire, and is not reachable by POST.
The page tests read the real `cockpit.html`: that it loads nothing from the network, that the
only routes it POSTs to are the control verbs and the team editor, and that it honours
`prefers-reduced-motion` — claims about behaviour, not about taste.
