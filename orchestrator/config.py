"""Configuration module for loading and validating agent, model, and role definitions."""

import os
from pathlib import Path
from typing import Dict, List, Optional, TypedDict, Any
import yaml

from orchestrator.skills.types import SkillConfig


class ModelConfig(TypedDict):
    """Configuration definition for a model in the catalog."""
    id: str
    name: str


class AgentConfig(TypedDict, total=False):
    """Configuration definition for an agent assigned to a role.

    Both `agent` and `model` may be a **list**, forming an escalation ladder: rung 0 runs
    the initial attempt and rung N runs escalation step N, clamped to the last rung. When
    both are lists they are zipped, so rung i is the pair ``(agent[i], model[i])`` — a model
    id only ever means something next to the agent whose catalog defines it. See
    `ladder_rungs`.
    """
    agent: Any               # str, or a list of them - a ladder of agents
    model: Optional[Any]     # str, a list of them, or None
    role: str


class RoleConfig(TypedDict):
    """Configuration definition for a role's responsibility."""
    responsibility: str


class RetryConfig(TypedDict, total=False):
    """How many times one agent execution may be re-attempted before the phase fails.

    A local CLI agent is a subprocess over a network service, and it fails the way those fail:
    intermittently, and by returning nothing rather than by raising. Until this existed the
    pipeline halted on the first such failure, which made the whole run only as reliable as its
    flakiest single call.
    """
    attempts: int            # total attempts per execution; 1 restores the old behaviour
    backoff_seconds: float   # base delay, doubled per retry
    max_backoff_seconds: float  # ceiling on that doubling
    escalate_model: bool     # step down the model ladder on each retry, when one is configured


class ExecutionConfig(TypedDict, total=False):
    """Configuration definition for execution environment and terminal visibility."""
    visible_terminals: bool
    terminal_type: str
    pause_on_completion: float
    close_terminal_on_completion: bool  # False keeps visible terminals / native TUI sessions open
    agent_execution_mode: str  # "auto", "native_tui", "headless"
    retry: RetryConfig         # what happens when one execution fails


class PreflightConfig(TypedDict, total=False):
    """Configuration definition for pre-execution environment probing (Tier 1 #6)."""
    enabled: bool           # Probe configured agents before launching any of them
    strict: bool            # Halt the run when a probe reports an error
    deep: bool              # Additionally invoke each binary's --version command
    timeout_seconds: int    # Seconds allowed per deep version probe


class RunStoreConfig(TypedDict, total=False):
    """Configuration definition for durable run persistence (Tier 1 #4)."""
    enabled: bool             # Persist each run to disk as it executes
    directory: str            # Runs directory, relative to project root or absolute
    max_output_chars: int     # Per-output truncation limit; 0 means unlimited


class WorkspaceConfig(TypedDict, total=False):
    """Configuration for git-backed workspace isolation (Tier 0 #3)."""
    isolation: str          # "auto" (isolate when possible), "worktree" (require), "none"
    directory: str          # Worktrees directory, relative to project root or absolute
    commit_on_finish: bool  # Commit the run's work onto its branch when it ends
    keep_worktree: bool     # Leave the checkout on disk after the run (default: remove it)


class BudgetConfig(TypedDict, total=False):
    """Configuration for spending ceilings (Tier 2 #11, and Roadmap Phase 2).

    The escalation ladder can spend several agents across several repair
    attempts on one task. `max_repair_attempts` bounds the number of attempts;
    these bound their cost. 0 means unlimited.

    The first pair bound one **session**. The `goal_` pair bound a whole
    **goal** — every session its tasks run, added up — because a decomposition
    multiplies a per-session ceiling by however many tasks it happened to emit.
    """
    max_total_tokens: int           # Stop escalating once this many tokens are spent
    max_duration_seconds: int       # Stop escalating once the session has run this long
    goal_max_total_tokens: int      # Stop starting tasks once the goal has spent this much
    goal_max_duration_seconds: int  # Stop starting tasks once the goal has run this long


class AcceptanceConfig(TypedDict, total=False):
    """Configuration for the objective acceptance gate (Roadmap Phase 0).

    A command the orchestrator runs itself, in the session's worktree, before the verifier.
    Read from the user's checkout, so an agent cannot edit the command that judges it.
    """
    command: Any            # str or list of str; empty disables the gate
    timeout_seconds: int    # seconds allowed before the command is killed
    required: bool          # when the gate is red, a verifier's PASS cannot stand
    output_limit: int       # characters of output kept and shown to agents


class VerificationConfig(TypedDict, total=False):
    """Configuration for how several verifiers reach one verdict (Tier 2 #8)."""
    consensus: str  # "unanimous", "majority", or "any"
    acceptance: Optional[AcceptanceConfig]


class ApprovalConfig(TypedDict, total=False):
    """Configuration for human approval gates (Roadmap Phase 5).

    A gate stops the work at a clean boundary and records a pending approval, rather than
    blocking a process on stdin: the person it is waiting for may not be there.
    """
    gates: Dict[str, bool]   # which gates are on: after_decomposition, before_task, before_merge
    auto_approve: bool       # answer every gate automatically - the guardrail, made explicit
    directory: str           # approvals directory, relative to project root or absolute


class DeliveryConfig(TypedDict, total=False):
    """Configuration for outward integration (Roadmap Phase 6).

    Off by default. A project that has not opted in cannot reach a remote by accident,
    whatever anyone approves - `never auto-push` is invariant 13.
    """
    enabled: bool           # nothing crosses the network until this is true
    remote: str             # git remote to push to
    base: str               # pull request base branch; "" means the remote's default
    draft: bool             # open pull requests as drafts
    on_approve: bool        # answering a `before_merge` gate delivers the work
    directory: str          # delivery records directory, relative to project root or absolute


class DelegationConfig(TypedDict, total=False):
    """Configuration for carrying a decomposed goal out (Roadmap Phases 2-3)."""
    max_parallel: int       # how many tasks may have a session in flight at once
    stop_on_failure: bool   # abandon the remaining waves once any task fails
    goals_directory: str    # goal store directory, relative to project root or absolute


class PlanningMemoryConfig(TypedDict, total=False):
    """What the decomposer is allowed to remember across goals (Roadmap 8.2)."""
    enabled: bool           # off returns the cold decomposition this project did before 8.2
    max_goals: int          # how far back the memory reads
    budget_chars: int       # the ceiling on what it may add to the prompt


class PlanningConfig(TypedDict, total=False):
    """Configuration for goal decomposition (Roadmap Phase 1)."""
    agent: Optional[str]    # which provider decomposes the goal; None inherits the planner's
    model: Optional[str]    # which model it uses; None inherits the planner's
    max_tasks: int          # ceiling on tasks in one plan
    memory: PlanningMemoryConfig   # what it remembers of previous goals


class OrchestratorConfig(TypedDict, total=False):
    """Root configuration structure for the orchestrator."""
    models: Dict[str, List[ModelConfig]]
    agents: List[AgentConfig]
    roles: Dict[str, RoleConfig]
    max_repair_attempts: Optional[int]
    execution: Optional[ExecutionConfig]
    skills: Optional[SkillConfig]
    preflight: Optional[PreflightConfig]
    run_store: Optional[RunStoreConfig]
    workspace: Optional[WorkspaceConfig]
    verification: Optional[VerificationConfig]
    budget: Optional[BudgetConfig]
    planning: Optional[PlanningConfig]
    delegation: Optional[DelegationConfig]
    approval: Optional[ApprovalConfig]
    delivery: Optional[DeliveryConfig]


#: A hard ceiling on `execution.retry.attempts`. A retry policy is insurance against a flaky
#: call, not a way to turn one execution into an unbounded loop against a service that is down.
MAX_RETRY_ATTEMPTS = 10


class ConfigValidationError(ValueError):
    """Raised when orchestrator configuration fails schema validation."""
    pass


DEFAULT_CONFIG: OrchestratorConfig = {
    "models": {
        "antigravity": [
            {"id": "gemini-3.8-flash-high", "name": "Gemini 3.8 Flash (High)"},
            {"id": "gemini-3.8-flash-medium", "name": "Gemini 3.8 Flash (Medium)"},
            {"id": "gemini-3.8-flash-low", "name": "Gemini 3.8 Flash (Low)"},
            {"id": "gemini-3.7-flash-high", "name": "Gemini 3.7 Flash (High)"},
            {"id": "gemini-3.7-flash-medium", "name": "Gemini 3.7 Flash (Medium)"},
            {"id": "gemini-3.7-flash-low", "name": "Gemini 3.7 Flash (Low)"},
            {"id": "gemini-3.6-flash-high", "name": "Gemini 3.6 Flash (High)"},
            {"id": "gemini-3.6-flash-medium", "name": "Gemini 3.6 Flash (Medium)"},
            {"id": "gemini-3.1-pro-high", "name": "Gemini 3.1 Pro (High)"},
            {"id": "claude-sonnet-4-6", "name": "Claude Sonnet 4.6 (Thinking)"},
            {"id": "claude-opus-4-6-thinking", "name": "Claude Opus 4.6 (Thinking)"},
            {"id": "gpt-oss-120b-medium", "name": "GPT-OSS 120B (Medium)"},
        ],
        "claude": [
            {"id": "sonnet", "name": "Claude Sonnet (CLI alias)"},
            {"id": "opus", "name": "Claude Opus (CLI alias)"},
            {"id": "haiku", "name": "Claude Haiku (CLI alias)"},
        ],
        "opencode": [
            {"id": "opencode/gpt-5.1-codex", "name": "GPT-5.1 Codex via OpenCode"},
            {"id": "anthropic/claude-sonnet-4-5", "name": "Claude Sonnet 4.5 via Anthropic"},
            {"id": "google/gemini-3-pro", "name": "Gemini 3 Pro via Google"},
            {"id": "custom/my-model", "name": "Custom / user-defined model"},
        ],
        # Which model ids Codex accepts depends on the *auth mode*: measured on a ChatGPT
        # account, `gpt-5.1-codex` and `gpt-5.1-codex-mini` are both refused with "not
        # supported when using Codex with a ChatGPT account". Only ids confirmed to work
        # under the ChatGPT login this project targets are listed. `codex doctor` reports
        # the account's current model; omitting `model:` entirely uses that default and is
        # the most portable choice.
        "codex": [
            {"id": "gpt-5.5", "name": "GPT-5.5 (Codex, ChatGPT account)"},
        ],
    },
    "agents": [
        {
            "agent": "antigravity",
            "model": "gemini-3.8-flash-high",
            "role": "researcher",
        },
        {
            "agent": "claude",
            "model": "sonnet",
            "role": "planner",
        },
        {
            "agent": "opencode",
            "model": "opencode/gpt-5.1-codex",
            "role": "implementer",
        },
        {
            "agent": "claude",
            "model": "sonnet",
            "role": "verifier",
        },
    ],
    "roles": {
        "decomposer": {
            "responsibility": (
                "Break the user's goal into the smallest set of focused, independently "
                "deliverable tasks, with explicit dependencies, the areas each task is "
                "expected to touch, and how each one would be judged done. Plan only: "
                "never implement."
            )
        },
        "researcher": {
            "responsibility": (
                "Investigate and analyze the supplied project context and user task. "
                "Provide project-specific observations, identify structural gaps, and "
                "outline areas for deeper technical inspection."
            )
        },
        "planner": {
            "responsibility": (
                "Review the user task, project context, and researcher findings. "
                "Formulate concrete, prioritized planning recommendations, architectural "
                "decisions, and actionable next steps."
            )
        },
        "implementer": {
            "responsibility": (
                "Implement the approved architectural and code changes directly in the "
                "workspace with clean, maintainable modifications."
            )
        },
        "verifier": {
            "responsibility": (
                "Verify the implementation against the original user task, "
                "project context, research findings, planning recommendations, "
                "and implementation result. Execute relevant tests when appropriate, "
                "identify regressions or errors, and return a clear PASS or FAIL "
                "verdict with supporting findings."
            )
        },
    },
    "max_repair_attempts": 2,
    "execution": {
        "visible_terminals": False,
        "terminal_type": "auto",
        "pause_on_completion": 1.5,
        "close_terminal_on_completion": True,
        "agent_execution_mode": "auto",
    },
    "skills": {
        "enabled": True,
        "search_paths": ["./skills", "./.agents/skills"],
    },
    "preflight": {
        "enabled": True,
        "strict": True,
        "deep": False,
        "timeout_seconds": 15,
    },
    "run_store": {
        "enabled": True,
        "directory": ".orchestrator/runs",
        "max_output_chars": 0,
    },
    "workspace": {
        "isolation": "auto",
        "directory": ".orchestrator/worktrees",
        "commit_on_finish": True,
        "keep_worktree": True,
    },
    "verification": {
        "consensus": "unanimous",
    },
}


DEFAULT_PREFLIGHT_CONFIG: PreflightConfig = {
    "enabled": True,
    "strict": True,
    "deep": False,
    "timeout_seconds": 15,
}

DEFAULT_RUN_STORE_CONFIG: RunStoreConfig = {
    "enabled": True,
    "directory": ".orchestrator/runs",
    "max_output_chars": 0,
}

DEFAULT_WORKSPACE_CONFIG: WorkspaceConfig = {
    "isolation": "auto",
    "directory": ".orchestrator/worktrees",
    "commit_on_finish": True,
    # The branch is the artifact; the checkout is scaffolding. Keeping every
    # worktree leaves a full second copy of the project per run, so the default
    # is to remove it once the work is committed. A dirty worktree is kept
    # regardless - see orchestrator.workspace.finish_worktree.
    "keep_worktree": False,
}

#: The gate is off until a project names its own command: there is no command this
#: orchestrator could guess that would be right for an arbitrary repository.
DEFAULT_ACCEPTANCE_CONFIG: AcceptanceConfig = {
    "command": "",
    "timeout_seconds": 600,
    "required": True,
    "output_limit": 4000,
}

DEFAULT_VERIFICATION_CONFIG: VerificationConfig = {
    "consensus": "unanimous",
    "acceptance": dict(DEFAULT_ACCEPTANCE_CONFIG),
}

#: Decomposition is not part of the `agents:` pipeline - it runs before one, and only when
#: asked. It therefore carries its own agent selection rather than a pipeline entry.
#: The planner's memory is on by default (Roadmap 9.2, answered: a Project accumulates
#: knowledge across Goals). It is a projection over stores that already exist, it is bounded,
#: and `--memory` prints exactly what it will say - so the cost of it being on is a few
#: hundred tokens per decomposition, and the cost of it being off is deciding cold every time.
#: Retries are on by default. The alternative was measured rather than assumed: across this
#: project's own recorded history every real end-to-end failure was one agent returning empty
#: output on the first step, with no second attempt. Three attempts with a widening gap is the
#: cheapest thing that turns most of those into a completed run.
DEFAULT_RETRY_CONFIG: RetryConfig = {
    "attempts": 3,
    "backoff_seconds": 2.0,
    "max_backoff_seconds": 30.0,
    "escalate_model": True,
}

DEFAULT_PLANNING_MEMORY_CONFIG: PlanningMemoryConfig = {
    "enabled": True,
    "max_goals": 12,
    "budget_chars": 2400,
}

DEFAULT_PLANNING_CONFIG: PlanningConfig = {
    "agent": None,   # None means "use whoever plans" - resolved by get_planning_config
    "model": None,
    "max_tasks": 12,
    "memory": dict(DEFAULT_PLANNING_MEMORY_CONFIG),
}

#: 0 means unlimited. Off by default: a ceiling the user did not choose is a
#: surprise mid-run halt, and the repair-attempt bound already prevents runaway
#: looping. Set these to bound cost as well as attempts.
DEFAULT_BUDGET_CONFIG: BudgetConfig = {
    "max_total_tokens": 0,
    "max_duration_seconds": 0,
    "goal_max_total_tokens": 0,
    "goal_max_duration_seconds": 0,
}

#: One task at a time by default. Parallelism is a real change in behaviour - several agents
#: writing several worktrees at once - so it is something a user turns on, not something they
#: discover after the fact.
DEFAULT_DELEGATION_CONFIG: DelegationConfig = {
    "max_parallel": 1,
    "stop_on_failure": False,
    "goals_directory": ".orchestrator/goals",
}

#: Every gate off by default. A gate the user did not ask for is a run that mysteriously
#: stopped; `before_merge` is the one most projects will want first, because it costs nothing
#: (the work is already done) and turns a pile of branches into a review queue.
DEFAULT_APPROVAL_CONFIG: ApprovalConfig = {
    "gates": {
        "after_decomposition": False,
        "before_task": False,
        "before_merge": False,
    },
    "auto_approve": False,
    "directory": ".orchestrator/approvals",
}

#: Nothing crosses the network until a project turns this on, and even then only a person
#: can trigger it. Draft pull requests by default: the first thing a reviewer should see is
#: that a machine wrote it and a human has not finished with it.
DEFAULT_DELIVERY_CONFIG: DeliveryConfig = {
    "enabled": False,
    "remote": "origin",
    "base": "",
    "draft": True,
    "on_approve": True,
    "directory": ".orchestrator/delivery",
}

#: The agents this orchestrator has a runner for (`graph.get_runner`, `preflight`'s resolvers).
#: The model catalog may list more providers; an entry naming one of those cannot run and is
#: refused by the team editor and by preflight, and never silently run as a different agent.
RUNNABLE_AGENTS = ("antigravity", "claude", "opencode", "codex")

VALID_ISOLATION_MODES = ("auto", "worktree", "none")
VALID_CONSENSUS_POLICIES = ("unanimous", "majority", "any")


def ladder_rungs(entry: Any) -> List[Any]:
    """Every ``(agent, model)`` pair one configured entry can run as, rung by rung. Pure.

    An escalation ladder used to be a property of the model alone, which meant a role could
    only ever fall back *within one provider*. When the researcher's single model returned
    empty output on every attempt (§9.0) there was nowhere to escalate to, and the run died
    at step one. Making `agent` a list too is the same idea one level up, and expressing
    both as one list of rungs is what keeps every consumer - the graph, preflight, the
    cockpit, the design surface - from growing its own opinion about how the two pair up.

    The pairing rule, in full:

    ============================  ==================================================
    ``agent: claude``             one rung: ``[("claude", None)]``
    ``model: [a, b]``             two rungs, same agent
    ``agent: [x, y]``             two rungs, each with its own default model
    ``agent: [x, y]``,            two rungs, zipped: ``[("x", "a"), ("y", "b")]``
    ``model: [a, b]``
    ``agent: [x, y]``,            two rungs, both on model ``a`` (usually a mistake,
    ``model: a``                  and one the catalog check will catch)
    ============================  ==================================================

    Mismatched list lengths are refused at validation rather than clamped here, so this
    never has to invent a pairing.

    Returns:
        A non-empty list of ``(agent_name, model_id_or_None)`` tuples.
    """
    if not isinstance(entry, dict):
        return [("claude", None)]

    raw_agent = entry.get("agent")
    agents = (
        [str(a).strip() for a in raw_agent if str(a).strip()]
        if isinstance(raw_agent, (list, tuple))
        else [str(raw_agent or "claude").strip() or "claude"]
    ) or ["claude"]

    raw_model = entry.get("model")
    if isinstance(raw_model, (list, tuple)):
        models: List[Optional[str]] = [
            (str(m).strip() or None) for m in raw_model
        ] or [None]
    else:
        models = [str(raw_model).strip() or None if raw_model else None]

    depth = max(len(agents), len(models))
    return [
        (agents[min(i, len(agents) - 1)], models[min(i, len(models) - 1)])
        for i in range(depth)
    ]


def rung_at(entry: Any, index: int) -> Any:
    """The ``(agent, model)`` pair for escalation step `index`, clamped to the last rung. Pure."""
    rungs = ladder_rungs(entry)
    return rungs[max(0, min(int(index), len(rungs) - 1))]


def ladder_depth(entry: Any) -> int:
    """How many rungs an entry has. 1 means there is nowhere to escalate to. Pure."""
    return len(ladder_rungs(entry))


def validate_config(raw_data: Any) -> OrchestratorConfig:
    """Validate raw parsed dictionary against OrchestratorConfig schema.

    Validates:
    - Root mapping structure
    - 'agents' list and required agent/role attributes
    - 'roles' dictionary and required responsibility descriptions
    - 'models' catalog (if present) and verifies configured agent models against it

    Raises:
        ConfigValidationError: If data does not adhere to required structure or specifies an invalid model.
    """
    if not isinstance(raw_data, dict):
        raise ConfigValidationError("Configuration root must be a dictionary/mapping.")

    # 1. Validate agents list
    if "agents" not in raw_data or not isinstance(raw_data["agents"], list):
        raise ConfigValidationError("Configuration must define a non-empty 'agents' list.")

    if len(raw_data["agents"]) == 0:
        raise ConfigValidationError("'agents' list cannot be empty.")

    validated_agents: List[AgentConfig] = []
    for idx, item in enumerate(raw_data["agents"]):
        if not isinstance(item, dict):
            raise ConfigValidationError(f"agents[{idx}] must be a dictionary.")
        if "agent" not in item or not str(item["agent"]).strip():
            raise ConfigValidationError(f"agents[{idx}] is missing required non-empty 'agent' field.")
        if "role" not in item or not str(item["role"]).strip():
            raise ConfigValidationError(f"agents[{idx}] is missing required non-empty 'role' field.")

        # Either field may be a single value or a list forming an escalation ladder:
        # index 0 for the initial attempt, index N for escalation step N (clamped to the
        # last entry). See `ladder_rungs` and make_role_node.
        raw_agent = item.get("agent")
        if isinstance(raw_agent, (list, tuple)):
            if not raw_agent:
                raise ConfigValidationError(
                    f"agents[{idx}] has an empty 'agent' list. Provide at least one agent "
                    f"name."
                )
            agent_ladder = []
            for a_idx, entry in enumerate(raw_agent):
                if not isinstance(entry, (str, int, float)) or not str(entry).strip():
                    raise ConfigValidationError(
                        f"agents[{idx}].agent[{a_idx}] must be a non-empty agent name."
                    )
                agent_ladder.append(str(entry).strip())
            # A one-entry list is just a scalar spelled as a list - ladder_rungs treats
            # them identically - so collapsing here keeps the length check below from
            # refusing a config that would run exactly like `agent: <name>` would.
            agent_value: Any = agent_ladder[0] if len(agent_ladder) == 1 else agent_ladder
        else:
            agent_value = str(raw_agent).strip()

        raw_model = item.get("model")
        model_value: Optional[Any]
        if isinstance(raw_model, (list, tuple)):
            if not raw_model:
                raise ConfigValidationError(
                    f"agents[{idx}] has an empty 'model' list. Provide at least one model id, "
                    f"or omit 'model' to use the CLI default."
                )
            ladder = []
            for m_idx, entry in enumerate(raw_model):
                if not isinstance(entry, (str, int, float)) or not str(entry).strip():
                    raise ConfigValidationError(
                        f"agents[{idx}].model[{m_idx}] must be a non-empty model id string."
                    )
                ladder.append(str(entry).strip())
            # Same collapse as above - but only when the agent side is not itself a
            # ladder, where a single typed-so-far model instead means "one model for
            # several providers" and the length check two lines down is what should
            # catch it (mirrors teams.py's normalize_team).
            collapse = len(ladder) == 1 and not isinstance(agent_value, list)
            model_value = ladder[0] if collapse else ladder
        elif raw_model:
            model_value = str(raw_model).strip()
        else:
            model_value = None

        # Two ladders of different lengths have no correct pairing. Clamping the shorter
        # one would silently hand a model id to an agent whose catalog has never heard of
        # it, which is the one mistake this pairing exists to make impossible.
        if isinstance(agent_value, list) and isinstance(model_value, list):
            if len(agent_value) != len(model_value):
                raise ConfigValidationError(
                    f"agents[{idx}] lists {len(agent_value)} agent(s) and "
                    f"{len(model_value)} model(s). When both are ladders they are paired "
                    f"rung by rung, so they must be the same length - a model id only means "
                    f"something next to the agent whose catalog defines it.\n\n"
                    f"  - agent: [antigravity, claude]\n"
                    f"    model: [gemini-3.8-flash-high, sonnet]\n"
                    f"    role: researcher"
                )

        agent_entry: AgentConfig = {
            "agent": agent_value,
            "role": str(item["role"]).strip(),
            "model": model_value,
        }
        validated_agents.append(agent_entry)

    # 1b. Reject parallel implementers.
    #
    # Consecutive same-role agents form a parallel phase. That is well-defined
    # for read-only roles (an ensemble of researchers, a quorum of verifiers),
    # but two implementers running concurrently would write the same working
    # directory with no defined reconciliation. Rather than silently corrupt a
    # workspace, refuse the configuration.
    for idx in range(1, len(validated_agents)):
        prev_role = validated_agents[idx - 1]["role"]
        role = validated_agents[idx]["role"]
        if role == prev_role and role == "implementer":
            raise ConfigValidationError(
                "Configuration Error\n\n"
                "Two 'implementer' agents are configured consecutively, which would run them\n"
                "in parallel against the same working directory. Concurrent writes by two\n"
                "coding agents have no defined outcome, so this is not allowed.\n\n"
                "Parallel execution is supported for read-only roles - run several\n"
                "'researcher' agents as an ensemble, or several 'verifier' agents as a\n"
                "quorum. For implementation, configure exactly one agent per phase, or use\n"
                "a model escalation ladder:\n\n"
                "  - agent: opencode\n"
                "    model:\n"
                "      - opencode/gpt-5.1-codex      # first attempt\n"
                "      - anthropic/claude-sonnet-4-5 # first repair, and beyond\n"
                "    role: implementer"
            )

    # 2. Validate roles dictionary
    if "roles" not in raw_data or not isinstance(raw_data["roles"], dict):
        raise ConfigValidationError("Configuration must define a 'roles' dictionary.")

    validated_roles: Dict[str, RoleConfig] = {}
    for role_name, role_data in raw_data["roles"].items():
        if not isinstance(role_data, dict) or "responsibility" not in role_data:
            raise ConfigValidationError(
                f"Role '{role_name}' must be a dictionary containing a 'responsibility' field."
            )
        validated_roles[str(role_name).strip()] = {
            "responsibility": str(role_data["responsibility"]).strip()
        }

    # 3. Validate models catalog (if present, or fallback to default)
    validated_models: Dict[str, List[ModelConfig]] = {}
    has_explicit_models = "models" in raw_data

    if has_explicit_models:
        if not isinstance(raw_data["models"], dict):
            raise ConfigValidationError("Configuration 'models' must be a dictionary/mapping.")

        for provider, m_list in raw_data["models"].items():
            if not isinstance(m_list, list):
                raise ConfigValidationError(f"models['{provider}'] must be a list of model entries.")
            provider_models: List[ModelConfig] = []
            for idx, item in enumerate(m_list):
                if not isinstance(item, dict):
                    raise ConfigValidationError(f"models['{provider}'][{idx}] must be a dictionary.")
                if "id" not in item or not str(item["id"]).strip():
                    raise ConfigValidationError(
                        f"models['{provider}'][{idx}] is missing required non-empty 'id' field."
                    )
                entry: ModelConfig = {
                    "id": str(item["id"]).strip(),
                    "name": str(item.get("name") or item["id"]).strip(),
                }
                provider_models.append(entry)
            validated_models[str(provider).strip()] = provider_models
    else:
        # Provide default catalog copy
        validated_models = {k: list(v) for k, v in DEFAULT_CONFIG.get("models", {}).items()}

    # 4. Validate every rung against the catalog.
    #
    #    A rung is an (agent, model) pair, and both halves are checked: an agent name this
    #    orchestrator cannot run used to fall through `graph.get_runner` to Claude in
    #    silence, which a ladder makes materially worse - a mistyped fallback rung would
    #    "escalate" to an agent nobody chose and the run would look fine.
    for idx, agent_entry in enumerate(validated_agents):
        rungs = ladder_rungs(agent_entry)
        for rung, (agent_name, model_id) in enumerate(rungs):
            position = (
                f" (escalation step {rung + 1} of {len(rungs)})" if len(rungs) > 1 else ""
            )

            if agent_name not in validated_models:
                known = "\n".join(f"  - {name}" for name in sorted(validated_models))
                raise ConfigValidationError(
                    f"\nConfiguration Error\n\n"
                    f"agents[{idx}] names the agent '{agent_name}'{position}, which is not a "
                    f"provider in the model catalog.\n\n"
                    f"Known agents:\n{known}\n\n"
                    f"Check the spelling, or add '{agent_name}' to the 'models' catalog in "
                    f"orchestrator.yaml."
                )

            allowed = [m["id"] for m in validated_models[agent_name]]
            if not model_id or not allowed or model_id in allowed:
                continue

            formatted_available = "\n".join(f"  - {m_id}" for m_id in allowed)
            if agent_name == "opencode":
                extra_hint = (
                    "If this is a custom or newly available OpenCode model,\n"
                    "add its exact provider/model identifier to orchestrator.yaml."
                )
            else:
                extra_hint = "Edit orchestrator.yaml and select one of the available model IDs."

            raise ConfigValidationError(
                f"\nConfiguration Error\n\n"
                f"Agent: {agent_name}\n"
                f"Requested model: {model_id}{position}\n\n"
                f"Model not found in the configured {agent_name.capitalize()} model catalog.\n\n"
                f"Available models:\n{formatted_available}\n\n"
                f"{extra_hint}"
            )

    # 5. Validate max_repair_attempts
    raw_max_repairs = raw_data.get("max_repair_attempts", 2)
    if raw_max_repairs is not None:
        if not isinstance(raw_max_repairs, int) or isinstance(raw_max_repairs, bool) or raw_max_repairs < 0:
            raise ConfigValidationError(
                "Configuration 'max_repair_attempts' must be an integer greater than or equal to 0."
            )
        max_repair_attempts = raw_max_repairs
    else:
        max_repair_attempts = 2

    # 6. Validate execution settings
    raw_exec = raw_data.get("execution")
    validated_execution: ExecutionConfig = {
        "visible_terminals": False,
        "terminal_type": "auto",
        "pause_on_completion": 1.5,
        "close_terminal_on_completion": True,
        "retry": dict(DEFAULT_RETRY_CONFIG),
    }
    if raw_exec is not None:
        if not isinstance(raw_exec, dict):
            raise ConfigValidationError("Configuration 'execution' must be a dictionary/mapping.")

        if "visible_terminals" in raw_exec:
            vis = raw_exec["visible_terminals"]
            if not isinstance(vis, bool):
                raise ConfigValidationError("Configuration 'execution.visible_terminals' must be a boolean.")
            validated_execution["visible_terminals"] = vis

        if "terminal_type" in raw_exec:
            t_type = str(raw_exec["terminal_type"]).strip().lower()
            valid_types = (
                "auto",
                "antigravity_integrated",
                "integrated",
                "windows_terminal",
                "console",
                "wt",
                "cmd",
                "none",
            )
            if t_type not in valid_types:
                raise ConfigValidationError(
                    f"Invalid execution.terminal_type '{t_type}'. Must be one of: {', '.join(valid_types)}"
                )
            validated_execution["terminal_type"] = t_type

        if "pause_on_completion" in raw_exec:
            pause = raw_exec["pause_on_completion"]
            if not isinstance(pause, (int, float)) or isinstance(pause, bool) or pause < 0:
                raise ConfigValidationError(
                    "Configuration 'execution.pause_on_completion' must be a non-negative number."
                )
            validated_execution["pause_on_completion"] = float(pause)

        if "close_terminal_on_completion" in raw_exec:
            close_value = raw_exec["close_terminal_on_completion"]
            if not isinstance(close_value, bool):
                raise ConfigValidationError(
                    "Configuration 'execution.close_terminal_on_completion' must be true or false."
                )
            validated_execution["close_terminal_on_completion"] = close_value

        if "agent_execution_mode" in raw_exec:
            mode = str(raw_exec["agent_execution_mode"]).strip().lower()
            valid_modes = ("auto", "native_tui", "headless")
            if mode not in valid_modes:
                raise ConfigValidationError(
                    f"Invalid execution.agent_execution_mode '{mode}'. Must be one of: {', '.join(valid_modes)}"
                )
            validated_execution["agent_execution_mode"] = mode
        else:
            validated_execution["agent_execution_mode"] = "auto"

        if raw_exec.get("retry") is not None:
            raw_retry = raw_exec["retry"]
            if not isinstance(raw_retry, dict):
                raise ConfigValidationError(
                    "Configuration 'execution.retry' must be a dictionary/mapping."
                )
            retry: RetryConfig = dict(DEFAULT_RETRY_CONFIG)  # type: ignore[assignment]

            if "attempts" in raw_retry:
                attempts = raw_retry["attempts"]
                if (
                    not isinstance(attempts, int)
                    or isinstance(attempts, bool)
                    or attempts < 1
                    or attempts > MAX_RETRY_ATTEMPTS
                ):
                    raise ConfigValidationError(
                        "Configuration 'execution.retry.attempts' must be an integer between "
                        f"1 and {MAX_RETRY_ATTEMPTS} (1 disables retrying)."
                    )
                retry["attempts"] = attempts

            for key in ("backoff_seconds", "max_backoff_seconds"):
                if key in raw_retry:
                    value = raw_retry[key]
                    if (
                        isinstance(value, bool)
                        or not isinstance(value, (int, float))
                        or value < 0
                    ):
                        raise ConfigValidationError(
                            f"Configuration 'execution.retry.{key}' must be a "
                            "non-negative number."
                        )
                    retry[key] = float(value)  # type: ignore[literal-required]

            if "escalate_model" in raw_retry:
                if not isinstance(raw_retry["escalate_model"], bool):
                    raise ConfigValidationError(
                        "Configuration 'execution.retry.escalate_model' must be true or false."
                    )
                retry["escalate_model"] = raw_retry["escalate_model"]

            validated_execution["retry"] = retry

    # 7. Validate skills settings
    raw_skills = raw_data.get("skills")
    validated_skills: SkillConfig = {
        "enabled": True,
        "search_paths": ["./skills", "./.agents/skills"],
    }
    if raw_skills is not None:
        if not isinstance(raw_skills, dict):
            raise ConfigValidationError("Configuration 'skills' must be a dictionary/mapping.")

        if "enabled" in raw_skills:
            en = raw_skills["enabled"]
            if not isinstance(en, bool):
                raise ConfigValidationError("Configuration 'skills.enabled' must be a boolean.")
            validated_skills["enabled"] = en

        if "search_paths" in raw_skills:
            sp = raw_skills["search_paths"]
            if not isinstance(sp, list):
                raise ConfigValidationError("Configuration 'skills.search_paths' must be a list of strings.")
            cleaned_paths: List[str] = []
            for idx, p in enumerate(sp):
                if not isinstance(p, str) or not p.strip():
                    raise ConfigValidationError(
                        f"Configuration 'skills.search_paths[{idx}]' must be a non-empty string."
                    )
                cleaned_paths.append(str(p).strip())
            validated_skills["search_paths"] = cleaned_paths

    # 8. Validate preflight settings (Tier 1 #6)
    raw_preflight = raw_data.get("preflight")
    validated_preflight: PreflightConfig = dict(DEFAULT_PREFLIGHT_CONFIG)  # type: ignore[assignment]
    if raw_preflight is not None:
        if not isinstance(raw_preflight, dict):
            raise ConfigValidationError("Configuration 'preflight' must be a dictionary/mapping.")

        for flag in ("enabled", "strict", "deep"):
            if flag in raw_preflight:
                val = raw_preflight[flag]
                if not isinstance(val, bool):
                    raise ConfigValidationError(
                        f"Configuration 'preflight.{flag}' must be a boolean."
                    )
                validated_preflight[flag] = val

        if "timeout_seconds" in raw_preflight:
            timeout = raw_preflight["timeout_seconds"]
            if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
                raise ConfigValidationError(
                    "Configuration 'preflight.timeout_seconds' must be a positive integer."
                )
            validated_preflight["timeout_seconds"] = timeout

    # 9. Validate run store settings (Tier 1 #4)
    raw_store = raw_data.get("run_store")
    validated_store: RunStoreConfig = dict(DEFAULT_RUN_STORE_CONFIG)  # type: ignore[assignment]
    if raw_store is not None:
        if not isinstance(raw_store, dict):
            raise ConfigValidationError("Configuration 'run_store' must be a dictionary/mapping.")

        if "enabled" in raw_store:
            en = raw_store["enabled"]
            if not isinstance(en, bool):
                raise ConfigValidationError("Configuration 'run_store.enabled' must be a boolean.")
            validated_store["enabled"] = en

        if "directory" in raw_store:
            directory = raw_store["directory"]
            if not isinstance(directory, str) or not directory.strip():
                raise ConfigValidationError(
                    "Configuration 'run_store.directory' must be a non-empty string."
                )
            validated_store["directory"] = directory.strip()

        if "max_output_chars" in raw_store:
            cap = raw_store["max_output_chars"]
            if not isinstance(cap, int) or isinstance(cap, bool) or cap < 0:
                raise ConfigValidationError(
                    "Configuration 'run_store.max_output_chars' must be a non-negative integer "
                    "(0 means unlimited)."
                )
            validated_store["max_output_chars"] = cap

    # 10. Validate workspace isolation settings (Tier 0 #3)
    raw_workspace = raw_data.get("workspace")
    validated_workspace: WorkspaceConfig = dict(DEFAULT_WORKSPACE_CONFIG)  # type: ignore[assignment]
    if raw_workspace is not None:
        if not isinstance(raw_workspace, dict):
            raise ConfigValidationError("Configuration 'workspace' must be a dictionary/mapping.")

        if "isolation" in raw_workspace:
            mode = str(raw_workspace["isolation"]).strip().lower()
            if mode not in VALID_ISOLATION_MODES:
                raise ConfigValidationError(
                    f"Invalid workspace.isolation '{mode}'. "
                    f"Must be one of: {', '.join(VALID_ISOLATION_MODES)}"
                )
            validated_workspace["isolation"] = mode

        if "directory" in raw_workspace:
            directory = raw_workspace["directory"]
            if not isinstance(directory, str) or not directory.strip():
                raise ConfigValidationError(
                    "Configuration 'workspace.directory' must be a non-empty string."
                )
            validated_workspace["directory"] = directory.strip()

        for flag in ("commit_on_finish", "keep_worktree"):
            if flag in raw_workspace:
                val = raw_workspace[flag]
                if not isinstance(val, bool):
                    raise ConfigValidationError(
                        f"Configuration 'workspace.{flag}' must be a boolean."
                    )
                validated_workspace[flag] = val

    # 11. Validate verification consensus settings (Tier 2 #8)
    raw_verification = raw_data.get("verification")
    validated_verification: VerificationConfig = dict(DEFAULT_VERIFICATION_CONFIG)  # type: ignore[assignment]
    if raw_verification is not None:
        if not isinstance(raw_verification, dict):
            raise ConfigValidationError("Configuration 'verification' must be a dictionary/mapping.")

        if "consensus" in raw_verification:
            policy = str(raw_verification["consensus"]).strip().lower()
            if policy not in VALID_CONSENSUS_POLICIES:
                raise ConfigValidationError(
                    f"Invalid verification.consensus '{policy}'. "
                    f"Must be one of: {', '.join(VALID_CONSENSUS_POLICIES)}"
                )
            validated_verification["consensus"] = policy

        raw_acceptance = raw_verification.get("acceptance")
        validated_acceptance: AcceptanceConfig = dict(DEFAULT_ACCEPTANCE_CONFIG)  # type: ignore[assignment]
        if raw_acceptance is not None:
            if not isinstance(raw_acceptance, dict):
                raise ConfigValidationError(
                    "Configuration 'verification.acceptance' must be a dictionary/mapping."
                )

            if "command" in raw_acceptance:
                command = raw_acceptance["command"]
                if command is None:
                    command = ""
                if isinstance(command, (list, tuple)):
                    if not all(isinstance(part, str) for part in command):
                        raise ConfigValidationError(
                            "Configuration 'verification.acceptance.command' must be a string or "
                            "a list of strings."
                        )
                    command = [str(part) for part in command]
                elif not isinstance(command, str):
                    raise ConfigValidationError(
                        "Configuration 'verification.acceptance.command' must be a string or a "
                        "list of strings (e.g. 'pytest -q')."
                    )
                validated_acceptance["command"] = command

            for key in ("timeout_seconds", "output_limit"):
                if key in raw_acceptance:
                    value = raw_acceptance[key]
                    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                        raise ConfigValidationError(
                            f"Configuration 'verification.acceptance.{key}' must be a positive integer."
                        )
                    validated_acceptance[key] = value

            if "required" in raw_acceptance:
                required = raw_acceptance["required"]
                if not isinstance(required, bool):
                    raise ConfigValidationError(
                        "Configuration 'verification.acceptance.required' must be a boolean."
                    )
                validated_acceptance["required"] = required
        validated_verification["acceptance"] = validated_acceptance

    # 12. Validate the run budget (Tier 2 #11)
    raw_budget = raw_data.get("budget")
    validated_budget: BudgetConfig = dict(DEFAULT_BUDGET_CONFIG)  # type: ignore[assignment]
    if raw_budget is not None:
        if not isinstance(raw_budget, dict):
            raise ConfigValidationError("Configuration 'budget' must be a dictionary/mapping.")
        for key in (
            "max_total_tokens",
            "max_duration_seconds",
            "goal_max_total_tokens",
            "goal_max_duration_seconds",
        ):
            if key in raw_budget:
                value = raw_budget[key]
                if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                    raise ConfigValidationError(
                        f"Configuration 'budget.{key}' must be a non-negative integer "
                        "(0 means unlimited)."
                    )
                validated_budget[key] = value

    # 13. Validate goal decomposition settings (Roadmap Phase 1)
    raw_planning = raw_data.get("planning")
    validated_planning: PlanningConfig = dict(DEFAULT_PLANNING_CONFIG)  # type: ignore[assignment]
    if raw_planning is not None:
        if not isinstance(raw_planning, dict):
            raise ConfigValidationError("Configuration 'planning' must be a dictionary/mapping.")

        if "agent" in raw_planning:
            agent_name = str(raw_planning["agent"]).strip()
            if not agent_name:
                raise ConfigValidationError("Configuration 'planning.agent' must be a non-empty string.")
            if agent_name not in validated_models:
                known = "\n".join(f"  - {name}" for name in sorted(validated_models))
                raise ConfigValidationError(
                    f"\nConfiguration Error\n\n"
                    f"planning.agent names the agent '{agent_name}', which is not a "
                    f"provider in the model catalog.\n\n"
                    f"Known agents:\n{known}\n\n"
                    f"Check the spelling, or add '{agent_name}' to the 'models' catalog in "
                    f"orchestrator.yaml."
                )
            validated_planning["agent"] = agent_name

        if "model" in raw_planning and raw_planning["model"] is not None:
            model_name = str(raw_planning["model"]).strip()
            agent_name = validated_planning.get("agent")
            if not agent_name:
                planner_entry = next(
                    (a for a in validated_agents if a.get("role") == "planner"), {}
                )
                agent_name = planner_entry.get("agent") or "claude"
            catalog = [m.get("id") for m in validated_models.get(agent_name, [])]
            if catalog and model_name not in catalog:
                raise ConfigValidationError(
                    f"Invalid planning.model '{model_name}' for agent '{agent_name}'.\n"
                    f"Available models: {', '.join(str(c) for c in catalog)}"
                )
            validated_planning["model"] = model_name

        if "max_tasks" in raw_planning:
            max_tasks = raw_planning["max_tasks"]
            if not isinstance(max_tasks, int) or isinstance(max_tasks, bool) or max_tasks < 1:
                raise ConfigValidationError(
                    "Configuration 'planning.max_tasks' must be a positive integer."
                )
            validated_planning["max_tasks"] = max_tasks

        if "memory" in raw_planning and raw_planning["memory"] is not None:
            raw_memory = raw_planning["memory"]
            if not isinstance(raw_memory, dict):
                raise ConfigValidationError(
                    "Configuration 'planning.memory' must be a dictionary/mapping."
                )
            memory_cfg: PlanningMemoryConfig = dict(DEFAULT_PLANNING_MEMORY_CONFIG)  # type: ignore[assignment]
            if "enabled" in raw_memory:
                if not isinstance(raw_memory["enabled"], bool):
                    raise ConfigValidationError(
                        "Configuration 'planning.memory.enabled' must be true or false."
                    )
                memory_cfg["enabled"] = raw_memory["enabled"]
            for key in ("max_goals", "budget_chars"):
                if key in raw_memory:
                    value = raw_memory[key]
                    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                        raise ConfigValidationError(
                            f"Configuration 'planning.memory.{key}' must be a "
                            "non-negative integer."
                        )
                    memory_cfg[key] = value  # type: ignore[literal-required]
            validated_planning["memory"] = memory_cfg

    # 14. Validate delegation settings (Roadmap Phases 2-3)
    raw_delegation = raw_data.get("delegation")
    validated_delegation: DelegationConfig = dict(DEFAULT_DELEGATION_CONFIG)  # type: ignore[assignment]
    if raw_delegation is not None:
        if not isinstance(raw_delegation, dict):
            raise ConfigValidationError("Configuration 'delegation' must be a dictionary/mapping.")

        if "max_parallel" in raw_delegation:
            parallel = raw_delegation["max_parallel"]
            if not isinstance(parallel, int) or isinstance(parallel, bool) or parallel < 1:
                raise ConfigValidationError(
                    "Configuration 'delegation.max_parallel' must be a positive integer "
                    "(1 runs one task at a time)."
                )
            validated_delegation["max_parallel"] = parallel

        if "stop_on_failure" in raw_delegation:
            stop = raw_delegation["stop_on_failure"]
            if not isinstance(stop, bool):
                raise ConfigValidationError(
                    "Configuration 'delegation.stop_on_failure' must be a boolean."
                )
            validated_delegation["stop_on_failure"] = stop

        if "goals_directory" in raw_delegation:
            directory = raw_delegation["goals_directory"]
            if not isinstance(directory, str) or not directory.strip():
                raise ConfigValidationError(
                    "Configuration 'delegation.goals_directory' must be a non-empty string."
                )
            validated_delegation["goals_directory"] = directory.strip()

    # 15. Validate approval gates (Roadmap Phase 5)
    from orchestrator.approvals import VALID_GATES

    raw_approval = raw_data.get("approval")
    validated_approval: ApprovalConfig = {
        "gates": dict(DEFAULT_APPROVAL_CONFIG["gates"]),
        "auto_approve": DEFAULT_APPROVAL_CONFIG["auto_approve"],
        "directory": DEFAULT_APPROVAL_CONFIG["directory"],
    }
    if raw_approval is not None:
        if not isinstance(raw_approval, dict):
            raise ConfigValidationError("Configuration 'approval' must be a dictionary/mapping.")

        raw_gates = raw_approval.get("gates")
        if raw_gates is not None:
            if not isinstance(raw_gates, dict):
                raise ConfigValidationError(
                    "Configuration 'approval.gates' must be a dictionary/mapping."
                )
            for name, value in raw_gates.items():
                if name not in VALID_GATES:
                    raise ConfigValidationError(
                        f"Unknown approval gate '{name}'. "
                        f"Must be one of: {', '.join(VALID_GATES)}"
                    )
                if not isinstance(value, bool):
                    raise ConfigValidationError(
                        f"Configuration 'approval.gates.{name}' must be a boolean."
                    )
                validated_approval["gates"][name] = value

        if "auto_approve" in raw_approval:
            auto = raw_approval["auto_approve"]
            if not isinstance(auto, bool):
                raise ConfigValidationError(
                    "Configuration 'approval.auto_approve' must be a boolean."
                )
            validated_approval["auto_approve"] = auto

        if "directory" in raw_approval:
            directory = raw_approval["directory"]
            if not isinstance(directory, str) or not directory.strip():
                raise ConfigValidationError(
                    "Configuration 'approval.directory' must be a non-empty string."
                )
            validated_approval["directory"] = directory.strip()

    # 16. Validate outward delivery (Roadmap Phase 6)
    raw_delivery = raw_data.get("delivery")
    validated_delivery: DeliveryConfig = dict(DEFAULT_DELIVERY_CONFIG)  # type: ignore[assignment]
    if raw_delivery is not None:
        if not isinstance(raw_delivery, dict):
            raise ConfigValidationError("Configuration 'delivery' must be a dictionary/mapping.")

        for flag in ("enabled", "draft", "on_approve"):
            if flag in raw_delivery:
                value = raw_delivery[flag]
                if not isinstance(value, bool):
                    raise ConfigValidationError(
                        f"Configuration 'delivery.{flag}' must be a boolean."
                    )
                validated_delivery[flag] = value

        for text_key in ("remote", "directory"):
            if text_key in raw_delivery:
                value = raw_delivery[text_key]
                if not isinstance(value, str) or not value.strip():
                    raise ConfigValidationError(
                        f"Configuration 'delivery.{text_key}' must be a non-empty string."
                    )
                validated_delivery[text_key] = value.strip()

        if "base" in raw_delivery:
            base = raw_delivery["base"]
            if base is None:
                base = ""
            if not isinstance(base, str):
                raise ConfigValidationError(
                    "Configuration 'delivery.base' must be a string "
                    "(empty means the remote's default branch)."
                )
            validated_delivery["base"] = base.strip()

    return {
        "models": validated_models,
        "agents": validated_agents,
        "roles": validated_roles,
        "max_repair_attempts": max_repair_attempts,
        "execution": validated_execution,
        "skills": validated_skills,
        "preflight": validated_preflight,
        "run_store": validated_store,
        "workspace": validated_workspace,
        "verification": validated_verification,
        "budget": validated_budget,
        "planning": validated_planning,
        "delegation": validated_delegation,
        "approval": validated_approval,
        "delivery": validated_delivery,
    }


def load_config(
    config_path: Optional[str] = None,
    project_root: Optional[str] = None,
) -> OrchestratorConfig:
    """Load and validate orchestrator configuration from YAML file or default fallback.

    Args:
        config_path: Optional explicit path to configuration YAML file.
        project_root: Optional directory to search for orchestrator.yaml.

    Returns:
        Validated OrchestratorConfig instance.

    Raises:
        FileNotFoundError: If explicit config_path does not exist.
        ConfigValidationError: If file content is invalid YAML or fails validation.
    """
    target_file: Optional[Path] = None

    if config_path:
        target_file = Path(config_path).resolve()
        if not target_file.is_file():
            raise FileNotFoundError(f"Specified configuration file not found: {target_file}")
    else:
        # An explicit project_root is a claim about where to look, not a hint alongside cwd -
        # a caller who names an empty directory means "there is no config here", and silently
        # falling through to cwd anyway would read whatever the real project's config
        # happens to contain instead of the DEFAULT_CONFIG that absence is supposed to mean.
        # cwd is used only when the caller did not say where to look at all.
        search_dir = Path(project_root).resolve() if project_root else Path(os.getcwd()).resolve()
        candidate = search_dir / "orchestrator.yaml"
        if candidate.is_file():
            target_file = candidate

    if not target_file:
        return DEFAULT_CONFIG

    try:
        with open(target_file, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except Exception as exc:
        raise ConfigValidationError(f"Failed to read YAML from '{target_file}': {exc}") from exc

    return validate_config(raw)


def get_agent_config(
    config: OrchestratorConfig,
    role: Optional[str] = None,
    agent: Optional[str] = None,
) -> Optional[AgentConfig]:
    """Retrieve the configured agent entry assigned to a specific role and/or agent.

    Args:
        config: The loaded OrchestratorConfig.
        role: Optional role name to filter by (e.g. 'researcher', 'planner').
        agent: Optional agent name to filter by (e.g. 'antigravity', 'claude').

    Returns:
        Matching AgentConfig or None.
    """
    for entry in config.get("agents", []):
        role_match = (role is None) or (entry.get("role") == role)
        agent_match = (agent is None) or any(
            rung_agent == agent for rung_agent, _model in ladder_rungs(entry)
        )
        if role_match and agent_match:
            return entry
    return None


def get_role_responsibility(config: OrchestratorConfig, role: str) -> str:
    """Retrieve the responsibility description for a specific role.

    Args:
        config: The loaded OrchestratorConfig.
        role: The role name to look up.

    Returns:
        Responsibility text string, or a sensible default.
    """
    role_entry = config.get("roles", {}).get(role)
    if role_entry and role_entry.get("responsibility"):
        return role_entry["responsibility"]
    return f"Execute tasks associated with the '{role}' role."


def get_available_models(config: OrchestratorConfig, agent: str) -> List[ModelConfig]:
    """Retrieve list of configured models for a specific agent/provider.

    Args:
        config: The loaded OrchestratorConfig.
        agent: The agent provider name (e.g., 'antigravity', 'claude', 'opencode').

    Returns:
        List of ModelConfig dictionaries (each with 'id' and 'name').
    """
    models_catalog = config.get("models", {})
    return list(models_catalog.get(agent, []))


def validate_model(config: OrchestratorConfig, agent: str, model: str) -> bool:
    """Check whether a model ID is configured for the given agent provider.

    Args:
        config: The loaded OrchestratorConfig.
        agent: The agent provider name.
        model: The model ID to validate.

    Returns:
        True if model is defined in the agent's catalog, False otherwise.
    """
    available = get_available_models(config, agent)
    if not available:
        return True
    return any(m.get("id") == model for m in available)


def get_model_display_name(config: OrchestratorConfig, agent: str, model: str) -> str:
    """Retrieve the human-readable display name for a given agent and model ID.

    Args:
        config: The loaded OrchestratorConfig.
        agent: The agent provider name.
        model: The model ID.

    Returns:
        Human-readable display name if found, or the model ID as fallback.
    """
    for m in get_available_models(config, agent):
        if m.get("id") == model:
            return m.get("name", model)
    return model


def get_max_repair_attempts(config: Optional[OrchestratorConfig]) -> int:
    """Retrieve maximum repair attempts allowed, defaulting to 2.

    Args:
        config: The loaded OrchestratorConfig or None.

    Returns:
        Integer maximum repair attempts (>= 0).
    """
    if not config:
        return 2
    val = config.get("max_repair_attempts", 2)
    return val if val is not None else 2


def get_visible_terminals(config: Optional[OrchestratorConfig]) -> bool:
    """Retrieve visible terminals preference, defaulting to False.

    Args:
        config: The loaded OrchestratorConfig or None.

    Returns:
        True if visible terminals are enabled, False otherwise.
    """
    if not config:
        return False
    exec_cfg = config.get("execution")
    if exec_cfg and isinstance(exec_cfg, dict):
        return bool(exec_cfg.get("visible_terminals", False))
    return False


def get_execution_config(config: Optional[OrchestratorConfig]) -> ExecutionConfig:
    """Retrieve execution configuration with safe fallbacks.

    Args:
        config: The loaded OrchestratorConfig or None.

    Returns:
        ExecutionConfig dictionary.
    """
    if not config or not config.get("execution"):
        return {
            "visible_terminals": False,
            "terminal_type": "auto",
            "pause_on_completion": 1.5,
            "close_terminal_on_completion": True,
            "agent_execution_mode": "auto",
            "retry": dict(DEFAULT_RETRY_CONFIG),
        }
    exec_cfg = config["execution"]
    return {
        "visible_terminals": bool(exec_cfg.get("visible_terminals", False)),
        "terminal_type": str(exec_cfg.get("terminal_type", "auto")),
        "pause_on_completion": float(exec_cfg.get("pause_on_completion", 1.5)),
        "close_terminal_on_completion": exec_cfg.get("close_terminal_on_completion", True) is not False,
        "agent_execution_mode": str(exec_cfg.get("agent_execution_mode", "auto")),
        "retry": get_retry_config(config),
    }


def get_retry_config(config: Optional[OrchestratorConfig]) -> RetryConfig:
    """Retrieve the per-execution retry policy with safe fallbacks.

    Args:
        config: The loaded OrchestratorConfig or None.

    Returns:
        RetryConfig with every key populated. `attempts` is never below 1, so the worst a
        malformed value can do is turn retries off - never turn one execution into a loop.
    """
    resolved: RetryConfig = dict(DEFAULT_RETRY_CONFIG)  # type: ignore[assignment]
    if not config:
        return resolved
    exec_cfg = config.get("execution")
    raw = exec_cfg.get("retry") if isinstance(exec_cfg, dict) else None
    if not isinstance(raw, dict):
        return resolved

    try:
        if "attempts" in raw:
            resolved["attempts"] = max(1, min(int(raw["attempts"]), MAX_RETRY_ATTEMPTS))
    except (TypeError, ValueError):
        pass
    for key in ("backoff_seconds", "max_backoff_seconds"):
        try:
            if key in raw:
                resolved[key] = max(0.0, float(raw[key]))  # type: ignore[literal-required]
        except (TypeError, ValueError):
            pass
    if isinstance(raw.get("escalate_model"), bool):
        resolved["escalate_model"] = raw["escalate_model"]
    return resolved


def get_skill_config(config: Optional[OrchestratorConfig]) -> SkillConfig:
    """Retrieve skill configuration with safe fallbacks.

    Args:
        config: The loaded OrchestratorConfig or None.

    Returns:
        SkillConfig dictionary.
    """
    if not config or not config.get("skills"):
        return {
            "enabled": True,
            "search_paths": ["./skills", "./.agents/skills"],
        }
    return config["skills"]


def get_preflight_config(config: Optional[OrchestratorConfig]) -> PreflightConfig:
    """Retrieve preflight configuration with safe fallbacks.

    Args:
        config: The loaded OrchestratorConfig or None.

    Returns:
        PreflightConfig dictionary with every key populated.
    """
    resolved: PreflightConfig = dict(DEFAULT_PREFLIGHT_CONFIG)  # type: ignore[assignment]
    if not config:
        return resolved
    raw = config.get("preflight")
    if isinstance(raw, dict):
        for key in ("enabled", "strict", "deep"):
            if key in raw:
                resolved[key] = bool(raw[key])
        if "timeout_seconds" in raw:
            try:
                resolved["timeout_seconds"] = max(1, int(raw["timeout_seconds"]))
            except (TypeError, ValueError):
                pass
    return resolved


def get_run_store_config(config: Optional[OrchestratorConfig]) -> RunStoreConfig:
    """Retrieve run store configuration with safe fallbacks.

    Args:
        config: The loaded OrchestratorConfig or None.

    Returns:
        RunStoreConfig dictionary with every key populated.
    """
    resolved: RunStoreConfig = dict(DEFAULT_RUN_STORE_CONFIG)  # type: ignore[assignment]
    if not config:
        return resolved
    raw = config.get("run_store")
    if isinstance(raw, dict):
        if "enabled" in raw:
            resolved["enabled"] = bool(raw["enabled"])
        if raw.get("directory"):
            resolved["directory"] = str(raw["directory"])
        if "max_output_chars" in raw:
            try:
                resolved["max_output_chars"] = max(0, int(raw["max_output_chars"]))
            except (TypeError, ValueError):
                pass
    return resolved


def get_workspace_config(config: Optional[OrchestratorConfig]) -> WorkspaceConfig:
    """Retrieve workspace isolation configuration with safe fallbacks.

    Args:
        config: The loaded OrchestratorConfig or None.

    Returns:
        WorkspaceConfig dictionary with every key populated.
    """
    resolved: WorkspaceConfig = dict(DEFAULT_WORKSPACE_CONFIG)  # type: ignore[assignment]
    if not config:
        return resolved
    raw = config.get("workspace")
    if isinstance(raw, dict):
        mode = str(raw.get("isolation", resolved["isolation"])).strip().lower()
        if mode in VALID_ISOLATION_MODES:
            resolved["isolation"] = mode
        if raw.get("directory"):
            resolved["directory"] = str(raw["directory"])
        for flag in ("commit_on_finish", "keep_worktree"):
            if flag in raw:
                resolved[flag] = bool(raw[flag])
    return resolved


def get_consensus_policy(config: Optional[OrchestratorConfig]) -> str:
    """Retrieve the verifier consensus policy, defaulting to 'unanimous'.

    Args:
        config: The loaded OrchestratorConfig or None.

    Returns:
        One of VALID_CONSENSUS_POLICIES.
    """
    if not config:
        return DEFAULT_VERIFICATION_CONFIG["consensus"]
    raw = config.get("verification")
    if isinstance(raw, dict):
        policy = str(raw.get("consensus", "")).strip().lower()
        if policy in VALID_CONSENSUS_POLICIES:
            return policy
    return DEFAULT_VERIFICATION_CONFIG["consensus"]


def get_budget_config(config: Optional[OrchestratorConfig]) -> BudgetConfig:
    """Retrieve the run budget with safe fallbacks.

    Args:
        config: The loaded OrchestratorConfig or None.

    Returns:
        BudgetConfig with every key populated; 0 means unlimited.
    """
    resolved: BudgetConfig = dict(DEFAULT_BUDGET_CONFIG)  # type: ignore[assignment]
    if not config:
        return resolved
    raw = config.get("budget")
    if isinstance(raw, dict):
        for key in (
            "max_total_tokens",
            "max_duration_seconds",
            "goal_max_total_tokens",
            "goal_max_duration_seconds",
        ):
            if key in raw:
                try:
                    resolved[key] = max(0, int(raw[key]))
                except (TypeError, ValueError):
                    pass
    return resolved


def get_acceptance_config(config: Optional[OrchestratorConfig]) -> AcceptanceConfig:
    """Retrieve the acceptance gate configuration with safe fallbacks.

    Args:
        config: The loaded OrchestratorConfig or None.

    Returns:
        AcceptanceConfig with every key populated. An empty `command` means no gate.
    """
    resolved: AcceptanceConfig = dict(DEFAULT_ACCEPTANCE_CONFIG)  # type: ignore[assignment]
    if not config:
        return resolved
    raw = config.get("verification")
    if not isinstance(raw, dict):
        return resolved
    raw_acceptance = raw.get("acceptance")
    if isinstance(raw_acceptance, dict):
        if "command" in raw_acceptance and raw_acceptance["command"] is not None:
            resolved["command"] = raw_acceptance["command"]
        if "required" in raw_acceptance:
            resolved["required"] = bool(raw_acceptance["required"])
        for key in ("timeout_seconds", "output_limit"):
            if key in raw_acceptance:
                try:
                    resolved[key] = max(1, int(raw_acceptance[key]))
                except (TypeError, ValueError):
                    pass
    return resolved


def get_planning_config(config: Optional[OrchestratorConfig]) -> PlanningConfig:
    """Retrieve the decomposition configuration with safe fallbacks.

    Falls back to the configured planner's agent and model when `planning` names none, so a
    project that never configures decomposition still gets a sensible one.

    Args:
        config: The loaded OrchestratorConfig or None.

    Returns:
        PlanningConfig with every key populated.
    """
    resolved: PlanningConfig = dict(DEFAULT_PLANNING_CONFIG)  # type: ignore[assignment]
    resolved["agent"] = "claude"
    if not config:
        return resolved

    raw = config.get("planning") if isinstance(config.get("planning"), dict) else {}
    planner = get_agent_config(config, role="planner") or {}
    planner_agent = str(planner.get("agent") or "") or None
    planner_model = planner.get("model")
    if isinstance(planner_model, (list, tuple)):
        # An escalation ladder's first rung is the planner's ordinary choice.
        planner_model = planner_model[0] if planner_model else None

    chosen_agent = str(raw.get("agent") or "").strip() or planner_agent or "claude"
    resolved["agent"] = chosen_agent

    if raw.get("model"):
        resolved["model"] = str(raw["model"])
    elif chosen_agent == planner_agent and planner_model:
        # Inherit only when the decomposer runs on the same provider as the planner;
        # a model id is meaningless across providers.
        resolved["model"] = str(planner_model)
    else:
        resolved["model"] = None

    if "max_tasks" in raw:
        try:
            resolved["max_tasks"] = max(1, int(raw["max_tasks"]))
        except (TypeError, ValueError):
            pass

    memory: PlanningMemoryConfig = dict(DEFAULT_PLANNING_MEMORY_CONFIG)  # type: ignore[assignment]
    raw_memory = raw.get("memory")
    if isinstance(raw_memory, dict):
        if isinstance(raw_memory.get("enabled"), bool):
            memory["enabled"] = raw_memory["enabled"]
        for key in ("max_goals", "budget_chars"):
            try:
                if key in raw_memory:
                    memory[key] = max(0, int(raw_memory[key]))  # type: ignore[literal-required]
            except (TypeError, ValueError):
                pass
    resolved["memory"] = memory
    return resolved


def get_delegation_config(config: Optional[OrchestratorConfig]) -> DelegationConfig:
    """Retrieve the delegation configuration with safe fallbacks.

    Args:
        config: The loaded OrchestratorConfig or None.

    Returns:
        DelegationConfig with every key populated. `max_parallel` of 1 is the sequential case.
    """
    resolved: DelegationConfig = dict(DEFAULT_DELEGATION_CONFIG)  # type: ignore[assignment]
    if not config:
        return resolved
    raw = config.get("delegation")
    if isinstance(raw, dict):
        if "max_parallel" in raw:
            try:
                resolved["max_parallel"] = max(1, int(raw["max_parallel"]))
            except (TypeError, ValueError):
                pass
        if "stop_on_failure" in raw:
            resolved["stop_on_failure"] = bool(raw["stop_on_failure"])
        if raw.get("goals_directory"):
            resolved["goals_directory"] = str(raw["goals_directory"])
    return resolved


def get_approval_config(config: Optional[OrchestratorConfig]) -> ApprovalConfig:
    """Retrieve the approval gate configuration with safe fallbacks.

    Args:
        config: The loaded OrchestratorConfig or None.

    Returns:
        ApprovalConfig with every gate present, so callers never branch on a missing key.
    """
    resolved: ApprovalConfig = {
        "gates": dict(DEFAULT_APPROVAL_CONFIG["gates"]),
        "auto_approve": DEFAULT_APPROVAL_CONFIG["auto_approve"],
        "directory": DEFAULT_APPROVAL_CONFIG["directory"],
    }
    if not config:
        return resolved
    raw = config.get("approval")
    if isinstance(raw, dict):
        raw_gates = raw.get("gates")
        if isinstance(raw_gates, dict):
            for name, value in raw_gates.items():
                if name in resolved["gates"]:
                    resolved["gates"][name] = bool(value)
        if "auto_approve" in raw:
            resolved["auto_approve"] = bool(raw["auto_approve"])
        if raw.get("directory"):
            resolved["directory"] = str(raw["directory"])
    return resolved


def gate_enabled(config: Optional[OrchestratorConfig], gate: str) -> bool:
    """Return True when a named approval gate is switched on."""
    return bool(get_approval_config(config)["gates"].get(gate, False))


def get_delivery_config(config: Optional[OrchestratorConfig]) -> DeliveryConfig:
    """Retrieve the outward delivery configuration with safe fallbacks.

    Args:
        config: The loaded OrchestratorConfig or None.

    Returns:
        DeliveryConfig with every key populated. A missing or unreadable section resolves to
        `enabled: False`, so the failure mode of this accessor is "nothing is published".
    """
    resolved: DeliveryConfig = dict(DEFAULT_DELIVERY_CONFIG)  # type: ignore[assignment]
    if not config:
        return resolved
    raw = config.get("delivery")
    if isinstance(raw, dict):
        for flag in ("enabled", "draft", "on_approve"):
            if flag in raw:
                resolved[flag] = bool(raw[flag])
        for text_key in ("remote", "directory"):
            if raw.get(text_key):
                resolved[text_key] = str(raw[text_key])
        if "base" in raw and raw["base"] is not None:
            resolved["base"] = str(raw["base"]).strip()
    return resolved
