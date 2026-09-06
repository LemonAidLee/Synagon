# AI Orchestrator Architecture

A modular, extensible multi-agent orchestration framework built on **LangGraph**, coordinating local command-line AI agents via non-interactive subprocess calls without requiring provider API keys.

---

## 1. Architectural Hierarchy

The orchestration architecture is structured around a strict 5-level conceptual hierarchy:

```text
Agent
  ↓
Model
  ↓
Role
  ↓
Responsibility
  ↓
AgentResult
```

### Detailed Concept Definitions

#### 1. Agent
The underlying execution provider or tool binary.
* **Examples**: `antigravity`, `claude`, `opencode`
* **Purpose**: Manages execution mechanisms, subprocess environments, authentication context, and CLI flags.

#### 2. Model
The specific engine selected from that execution provider.
* **Examples**:
  * Antigravity: `gemini-3.8-flash-high`, `gemini-3.7-flash-high`, `claude-sonnet-4-6`
  * Claude Code: `sonnet`, `opus`, `haiku`
  * OpenCode: `opencode/gpt-5.1-codex`, `anthropic/claude-sonnet-4-5`, `custom/my-model`
* **Purpose**: Passed via official CLI flags (e.g. `--model`). Verified against the model catalog prior to execution.

#### 3. Role
The functional responsibility category in the workflow pipeline.
* **Examples**: `researcher`, `planner`, `implementer`, `verifier`
* **Purpose**: Dictates what stage of the engineering loop an agent is fulfilling.

#### 4. Responsibility
The explicit instructions, expectations, and guidelines associated with that role.
* **Examples**: Context analysis, technical gap assessment, architectural planning, code modification, test validation.
* **Purpose**: Injected dynamically into agent prompts from `orchestrator.yaml`.

#### 5. AgentResult
The standardized output record produced by one agent execution.
* **Fields**: `agent`, `model`, `role`, `status`, `duration_seconds`, `output`.
* **Purpose**: Provides uniform, serializable records accumulated in shared state.

> **Key Rule**: An **Agent** is *who* executes. A **Model** is *which engine* is selected. A **Role** is *the title/position* assigned. A **Responsibility** is *what instructions* are given. An **AgentResult** is *what they produced*.

---

## 2. Configuration & Model Catalog Architecture (`orchestrator.yaml`)

`orchestrator.yaml` serves as the single source of truth for:
1. **Agent Assignments**: Which provider, model, and role are active.
2. **Model Catalog**: The known, user-configurable models available for each provider.
3. **Role Responsibilities**: Natural-language instructions for each workflow role.

```yaml
agents:
  - agent: antigravity
    model: gemini-3.8-flash-high
    role: researcher

  - agent: claude
    model: sonnet
    role: planner

  - agent: opencode
    model: opencode/gpt-5.1-codex
    role: implementer

models:
  antigravity:
    - id: gemini-3.8-flash-high
      name: Gemini 3.8 Flash (High)
    - id: gemini-3.7-flash-high
      name: Gemini 3.7 Flash (High)
    ...
  claude:
    - id: sonnet
      name: Claude Sonnet (CLI alias)
    - id: opus
      name: Claude Opus (CLI alias)
    ...
  opencode:
    - id: opencode/gpt-5.1-codex
      name: GPT-5.1 Codex via OpenCode
    - id: anthropic/claude-sonnet-4-5
      name: Claude Sonnet 4.5 via Anthropic
    - id: custom/my-model
      name: Custom / user-defined model

roles:
  researcher:
    responsibility: "Investigate and analyze the supplied project context..."
  planner:
    responsibility: "Review the user task, project context, and researcher findings..."
  implementer:
    responsibility: "Implement the approved changes directly in the workspace..."
  verifier:
    responsibility: "Verify the implementation against acceptance criteria..."
```

---

## 3. Model Distinction & Validation Principles

The architecture explicitly distinguishes between three model states:

1. **Configured Model**: The model ID actively assigned to an agent under `agents` in `orchestrator.yaml`.
2. **Known Model**: A model ID documented in the `models` catalog in `orchestrator.yaml`.
3. **Currently Available Model**: A model that the user's active subscription, local CLI environment, and provider authentication actually permit at runtime.

### Pre-Execution Validation
Before any AI CLI is spawned, the configuration loader validates that the configured model exists in that agent's catalog. If an invalid model is requested (e.g. `sonnettt`), the pipeline halts immediately with a clear, actionable error:

```text
Configuration Error

Agent: claude
Requested model: sonnettt

Model not found in the configured Claude model catalog.

Available models:
  - sonnet
  - opus
  - haiku

Edit orchestrator.yaml and select one of the available model IDs.
```

### OpenCode Extensibility & Escape Hatch
OpenCode supports arbitrary providers and models using `provider/model` syntax. The configuration system avoids hardcoding a fixed provider list:
- Users can add any new or custom model (e.g., `openrouter/deepseek-v3`, `ollama/qwen2.5-coder`, `custom/my-model`) directly into `orchestrator.yaml` under `models.opencode`.
- The validator immediately accepts the new model without any changes to Python code.

---

## 4. Standardized Result Format (`AgentResult`)

All agent executions record their output using a uniform, structured schema:

```python
class AgentResult(TypedDict):
    agent: str               # Execution provider: "antigravity", "claude", "opencode"
    role: str                # Role assigned: "researcher", "planner", etc.
    status: str              # "success" or "error"
    output: str              # Output text produced by the agent
    duration_seconds: float  # Elapsed execution time in seconds
    model: Optional[str]     # Selected model name passed to CLI
```

### Shared Working Memory (`OrchestratorState`)

```python
class OrchestratorState(TypedDict, total=False):
    task: str
    message: Optional[str]
    project_root: Optional[str]
    project_context: Optional[str]
    config_path: Optional[str]
    config: Optional[OrchestratorConfig]
    agent_results: List[AgentResult]
    analysis: Optional[str]  # Backward-compatible alias for researcher output
    review: Optional[str]    # Backward-compatible alias for planner output
    status: str
    error: Optional[str]
```## 5. Current Implemented Workflow (Stage 7: Controlled Self-Repair Loop)

```text
                         USER TASK
                             │
                             ▼
                      PROJECT CONTEXT
                             │
                             ▼
                      ANTIGRAVITY
                       Researcher
                             │
                             ▼
                        CLAUDE
                         Planner
                             │
                             ▼
                       OPENCODE
                      Implementer
                             │
                             ▼
                        CLAUDE
                        Verifier
                             │
                       ┌─────┴─────┐
                       │           │
                     PASS         FAIL
                       │           │
                       ▼           ▼
                      END      Attempts?
                                  │
                           ┌──────┴──────┐
                           │             │
                        REMAIN        EXHAUSTED
                           │             │
                           ▼             ▼
                       OPENCODE         END
                        Repair       (FAILED)
                           │
                           ▼
                        CLAUDE
                       Verifier
                           │
                     ┌─────┴─────┐
                     │           │
                   PASS         FAIL
                     │           │
                     ▼           ▼
                    END      More attempts?
                                  │
                                ...
```

The workflow executes as an adaptive, bounded orchestration graph:

1. **`[1/6]` Project Context & Config Validation (`context_node`)**:
   - Reads and validates `orchestrator.yaml`.
   - Ingests `max_repair_attempts` (default: 2, or caller override).
   - Initializes `repair_attempts = 0` and `verification_history = []`.
   - Collects safe workspace context (redacting secrets).
2. **`[2/6]` Antigravity Node (`antigravity_node`)**:
   - Researcher role: Analyzes codebase, dependencies, and architectural structure.
3. **`[3/6]` Claude Code Node (`claude_node`)**:
   - Planner role: Formulates concrete, prioritized implementation recommendations.
4. **`[4/6]` OpenCode Node (`opencode_node`)**:
   - Implementer role: Executes the planned changes directly in the workspace (`cwd=project_root`).
5. **`[5/6]` Claude Verifier Node (`verifier_node`)**:
   - Verifier role: Independently evaluates the implementation, runs/inspects tests, and parses verdict: `PASS`, `FAIL`, or `UNKNOWN`.
   - Records full report into `AgentResult` and appends `VerificationRecord` into `verification_history`.
6. **Decision & Routing (`should_repair_or_end`)**:
   - **`VERDICT: PASS`**: Workflow terminates immediately at `END` with status `completed`.
   - **`VERDICT: FAIL` / `UNKNOWN`**:
     - If `repair_attempts < max_repair_attempts`: Routes to `opencode_repair`.
     - Else: Terminates at `END` with status `failed`.
7. **OpenCode Repair Node (`opencode_repair_node`)**:
   - Increments `repair_attempts` (1, 2, ...).
   - Injects cumulative verifier findings, failure details, and required fixes into repair prompt.
   - Instructs OpenCode to **inspect workspace first**, identify root cause, apply targeted fixes, and run tests.
   - Unconditionally loops back to `verifier_node` for re-verification.

---

## 6. Self-Repair Prompt Contract & Context Architecture

The self-repair loop implements a targeted feedback chain:

1. **Repair Prompt to OpenCode**:
   - **Original Task**: The immutable user objective.
   - **Project Context**: Safe directory inventory and environment specs.
   - **Planner Plan**: The architectural plan and expected design.
   - **Previous Implementation**: The code changes attempted in the prior run.
   - **Verifier Findings & Feedback**: The explicit failure findings, test errors, and required fixes from the verifier.
   - **Attempt Numbers**: Current attempt index and maximum allowed (`Attempt X of Y`).
   - **Inspect-First Mandate**: OpenCode is required to inspect actual files on disk before modifying anything.

2. **Re-Verification Prompt to Claude Code**:
   - **Iteration Context**: Explicit indication of verification iteration (`Attempt #N`) and completed repairs.
   - **Previous Findings**: Prior failure findings to verify resolution.
   - **Re-Verification Mandate**: Instructs the verifier to inspect the new actual workspace state on disk rather than trusting claims.

---

## 7. Verifier Architecture & Structured Verdict Parsing

The Verifier agent (`orchestrator/agents/verifier.py`) provides an independent, objective evaluation of the workspace changes.

### Independent Verification Principles
1. **No Blind Trust**: The implementer's self-reported success is treated as a claim, not proof. The verifier independently checks the code and tests.
2. **Workspace Inspection**: The verifier runs with `cwd=project_root`, allowing Claude Code to inspect the actual files modified on disk.
3. **Structured Verdict**: The verifier prompt instructs the model to lead with an unambiguous verdict line:
   * `VERDICT: PASS`
   * `VERDICT: FAIL`
4. **Strict Verdict Extraction**: The parsing regex `r"VERDICT:\s*(PASS|FAIL)"` extracts the verdict.
5. **No False Positives (Fail-Safe)**: If the verifier output is ambiguous, truncated, or lacks a clear verdict line, the system records `verdict = "UNKNOWN"`. Missing verdicts **never** default to `PASS`.

---

## 8. Bounded Retry & State History

1. **Bounded Execution**: The repair loop is strictly governed by `max_repair_attempts` (default: 2). Infinite looping is structurally impossible.
2. **Zero Repairs Mode (`max_repair_attempts: 0`)**: When set to 0, any failure terminates immediately at `END` without repair, matching Stage 6 behavior.
3. **History Preservation (`verification_history`)**: Each verification attempt appends a `VerificationRecord`:
   - `attempt`: 1-based verification iteration count
   - `repair_attempts`: number of repairs performed before this verification
   - `verdict`: "PASS", "FAIL", or "UNKNOWN"
   - `output`: complete textual report
   - `duration_seconds`: evaluation runtime
   - `model`: verifier model ID
4. **All AgentResults Preserved**: Every execution (initial research, planning, initial implementation, verification 1, repair 1, verification 2, etc.) is recorded as a distinct, observable `AgentResult`.

---

## 9. Model Inspection CLI

Users can view configured models and display names across all providers directly from the command line:

```powershell
python -m orchestrator --list-models
```

---

## 10. Security & Isolation Guarantees

1. **Zero Provider API Keys**: All agents (`antigravity`, `claude`, `opencode`) execute as local command-line subprocesses relying exclusively on pre-existing local CLI authentication. The orchestrator never requests, reads, stores, or transmits API keys or tokens.
2. **Official Model Selection Only**: Model selection uses official CLI arguments (`--model`) and is validated against the model catalog prior to execution.
3. **Strict Secret Protection**: The project context collector explicitly redacts `.env` and credential files, ensuring secret tokens are never exposed in agent prompts.
4. **Safe Subprocess Execution**: All subprocesses use `shell=False`, `stdin=subprocess.DEVNULL`, explicit UTF-8 decoding, and enforced timeouts to prevent hanging or shell injection.
5. **Target Directory Confinement**: All modifying commands run strictly within `project_root`.

---

## 11. Visible Agent Execution Terminals (Stage 7.5)

During live orchestration runs, each external agent's work can be visibly observed in real-time within its own dedicated terminal window.

```text
User Task
    │
    ▼
LangGraph Orchestrator
    │
    ├── [Terminal Window] "LangGraph - Antigravity Researcher"
    │       └── Antigravity CLI streams live output → closes upon completion
    │
    ├── [Terminal Window] "LangGraph - Claude Planner"
    │       └── Claude Code CLI streams live output → closes upon completion
    │
    ├── [Terminal Window] "LangGraph - OpenCode Implementer"
    │       └── OpenCode CLI streams live output → closes upon completion
    │
    ├── [Terminal Window] "LangGraph - Claude Verifier"
    │       └── Claude Code CLI streams live output → evaluates verdict
    │
    └── (If FAIL and repairs remain)
            ├── [Terminal Window] "LangGraph - OpenCode Repair #1"
            │       └── OpenCode implements fixes based on verifier feedback
            └── [Terminal Window] "LangGraph - Claude Verification #2"
                    └── Claude re-evaluates repaired workspace state
```

### Key Architectural Characteristics

1. **Windows Terminal vs. IDE Terminal Distinction**:
   Terminals are spawned as distinct, visible operating system console windows (via Windows Console Host or Windows Terminal) rather than manipulating the internal private pane layout of the Antigravity IDE. This ensures reliable visibility across any IDE, avoids fragile UI pane hooks, and guarantees proper process lifecycle isolation.

2. **Sequential Execution & Native Synchronization**:
   Visible terminal execution remains strictly sequential. The parent LangGraph orchestrator waits synchronously for each child process to complete before routing to the next node. Parallel execution is not introduced.

3. **Output Visibility vs. Authoritative Structured Result**:
   * **Live Terminal Output**: The user sees the external CLI's live streaming stdout and stderr in the console window in real-time.
   * **Orchestrator Result**: The launcher captures exact stdout, stderr, exit code, and duration into a structured `ExecutionResult`, preserving JSON output fidelity (for Antigravity) and preventing console styling from polluting LangGraph state.

4. **Configurable Execution Visibility**:
   In `orchestrator.yaml`:
   ```yaml
   execution:
     visible_terminals: false  # Default false for CI/automated testing
     terminal_type: auto       # "auto", "windows_terminal", or "console"
     pause_on_completion: 1.5  # Seconds to observe completed process before closing
   ```
   Can be enabled or disabled per execution via CLI flags:
   * `--visible-terminals`
   * `--no-visible-terminals`

5. **Standardized Window Titles**:
   Every window features an informative title identifying the pipeline stage:
   * Researcher: `LangGraph - Antigravity Researcher`
   * Planner: `LangGraph - Claude Planner`
   * Implementer: `LangGraph - OpenCode Implementer`
   * Verifier (Initial): `LangGraph - Claude Verifier`
   * Repair Attempt #N: `LangGraph - OpenCode Repair #N`
   * Reverification Attempt #N: `LangGraph - Claude Verification #N`

6. **Process Confinement & Safe IPC**:
   The process launcher writes a structured job specification (`job.json`) in a secure temporary directory, avoiding Windows command-line character length limits (8191 characters) and preventing command string injection. Child processes inherit explicit `PYTHONPATH` and execute confined to `project_root`.


---

## 10. Stage 7.6: Token Usage & Execution Metrics

### 10.1 Architecture Overview

Stage 7.6 introduces structured token accounting and execution metrics across all pipeline stages, including initial runs and self-repair iterations.

```text
               CLI Execution Output
                         │
             ┌───────────┼───────────┐
             ▼           ▼           ▼
        Antigravity   Claude Code  OpenCode
         (JSON raw)   (--output-   (--format
                       format json)   json)
             │           │           │
             └───────────┼───────────┘
                         ▼
                 Standardized TokenUsage
            {input, output, total, available}
                         ▼
                    AgentResult
                         ▼
                  LangGraph State
                         ▼
             Tracing & Summary Table
```

### 10.2 Core Principle: Real Usage Only

* **Zero Estimation**: Token counts are strictly extracted from official, reliable telemetry returned by CLI processes.
* **No Approximations**: Character counts, word counts, character-per-token heuristics, and arbitrary multipliers are strictly forbidden.
* **Explicit Availability**: If a CLI returns unstructured text, malformed metadata, or is run in a mode without token telemetry, token usage is explicitly marked as `available: False` (`tokens: unavailable`). Execution succeeds without failure; missing telemetry never halts the pipeline.

### 10.3 CLI Metadata Extraction

| Agent | Output Format Flag | Token Telemetry Location | Fields Extracted |
|---|---|---|---|
| **Antigravity CLI** (`agy`) | `--output-format json` | `payload["usage"]` | `input_tokens`, `output_tokens`, `total_tokens` |
| **Claude Code CLI** (`claude`) | `--output-format json` | `payload["usage"]` | `input_tokens`, `output_tokens`, `total_tokens = input + output` |
| **OpenCode CLI** (`opencode`) | `--format json` | `event["part"]["tokens"]` in `step_finish` | `input`, `output`, `total` (accumulated across steps) |

### 10.4 Standardized Data Structures

```python
class TokenUsage(TypedDict, total=False):
    input_tokens: Optional[int]
    output_tokens: Optional[int]
    total_tokens: Optional[int]
    available: bool

class AgentResult(TypedDict, total=False):
    agent: str
    role: str
    status: str
    output: str
    duration_seconds: float
    model: Optional[str]
    verdict: Optional[str]
    repair_attempt: Optional[int]
    token_usage: Optional[TokenUsage]
```

### 10.5 Metrics Aggregation & Reporting

* **Individual Step Reporting**: As each agent execution finishes, the tracer outputs execution time along with input, output, and total tokens (or `Token usage: unavailable`).
* **Repair Attempt Attribution**: Initial implementation and subsequent repair attempts (`OpenCode Repair #1`, `Claude Reverifier #2`) are individually recorded in `agent_results` with distinct iteration counters and token metrics.
* **Grand Totals vs Partial Totals**:
  - If all executions have available token metrics, a single grand total is reported.
  - If one or more executions report `available: False`, the orchestrator explicitly reports `Known tokens: <sum>` and `Unavailable: <count> execution(s)`. It never silently treats missing usage as zero.

---

## 11. Global Skill Architecture (Stage 7.7)

### 11.1 Conceptual Model

Skills represent modular capabilities, instructions, workflows, scripts, or templates located within the project or user environment.
The orchestration system integrates a **global skill discovery and access layer** that maintains **role-independent skill awareness**:

```text
                         USER TASK
                            │
                            ▼
                     ┌──────────────┐
                     │  LangGraph   │
                     │ Orchestrator │
                     └──────┬───────┘
                            │
              ┌─────────────┼─────────────┐
              │             │             │
              ▼             ▼             ▼
         Project        Skill Registry   Config
         Context
              │             │
              └─────────────┼─────────────┘
                            │
                            ▼
                     Shared Agent Context
                            │
           ┌────────────────┼────────────────┐
           ▼                ▼                ▼
      Antigravity        Claude           OpenCode
       Researcher        Planner        Implementer
           │                │                │
           └────────────────┼────────────────┘
                            ▼
                      Claude Verifier
                            │
                    [FAIL]  │  [PASS]
                 ┌──────────┴──────────┐
                 ▼                     ▼
          OpenCode Repair             END
                 │
                 ▼
          Claude Reverifier
```

Skills are **never tied to a specific role**. Every agent receives global awareness of all available skills.

### 11.2 What is a Skill?

Minimally, a skill is a directory containing:
* `SKILL.md` (or `skill.md`): Markdown instructions detailing conventions, workflows, and rules, with optional YAML frontmatter (`name`, `description`, `version`).
* Optional resources: immediate subdirectories such as `scripts/`, `templates/`, `assets/`, `examples/`, `resources/`, or `tools/`.

### 11.3 Standardized Skill Representation

```python
class SkillInfo(TypedDict, total=False):
    name: str                 # Normalized lowercase/hyphenated identifier
    description: str          # Concise summary from frontmatter or lead paragraph
    location: str             # Absolute path to skill directory
    instructions_path: str    # Absolute path to SKILL.md
    version: Optional[str]    # Optional version string
    resources: List[str]      # Names of immediate subdirectories present
```

### 11.4 Two Levels of Skill Access

1. **Level 1 — Skill Awareness (All Roles)**:
   Every agent prompt receives a compact, token-efficient manifest containing skill names, descriptions, locations, instructions paths, and available resource directories. Full file contents are **never** injected into prompts.
2. **Level 2 — Skill Inspection & Use (Implementer, Repair, Verifier)**:
   Implementers, repair agents, and verifiers receive exact filesystem paths to `SKILL.md` and resources. When a skill is relevant, they inspect the files directly on disk using their standard file tools before writing or verifying code.

### 11.5 Role Behaviors

* **Antigravity (Researcher)**: Receives the skill manifest. Considers available capabilities in its research and explicitly notes relevant skills in its findings.
* **Claude Code (Planner)**: Evaluates available skills against the task and research. Explicitly recommends which skills should be applied and why.
* **OpenCode (Implementer)**: Receives instructions to inspect `SKILL.md` at the specified path before writing code, adhere to documented workflows, use provided scripts/templates, and avoid reinventing functionality.
* **Claude Code (Verifier & Reverifier)**: Verifies whether relevant skills were followed appropriately by inspecting the workspace files against the skill requirements.
* **OpenCode (Repair Implementer)**: If verification failed due to missing or improper skill usage, inspects the skill instructions directly to resolve the issue.

### 11.6 Security Boundaries & Controlled Roots

* **Passive Discovery Only**: No binaries, scripts, or hooks are executed during discovery.
* **Controlled Roots**: Searches strictly within approved search paths configured in `orchestrator.yaml` (defaulting to `./skills` and `./.agents/skills` relative to the active project root).
* **Drive Root Protection**: Blind scans of root drives (`C:\`, `D:\`) or system directories are strictly forbidden.
* **Untouched External Skills**: Real external/global skill directories are read-only and never modified during orchestration runs.

### 11.7 Token-Efficiency Design

* Full skill documentation, scripts, and templates are **never** dumped into agent prompts.
* Prompts receive only a compact manifest (< 50 tokens per skill on average).
* Agents read the full skill contents on-demand via filesystem operations only when relevant.

### 11.8 Reporting & Usage Attribution

The final orchestration summary records:
* **Available Skills**: List of discovered skill names.
* **Referenced Skills**: Skills explicitly mentioned by name across agent execution outputs.
