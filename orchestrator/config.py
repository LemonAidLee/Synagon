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
    """Configuration definition for an agent assigned to a role."""
    agent: str
    model: Optional[str]
    role: str


class RoleConfig(TypedDict):
    """Configuration definition for a role's responsibility."""
    responsibility: str


class ExecutionConfig(TypedDict, total=False):
    """Configuration definition for execution environment and terminal visibility."""
    visible_terminals: bool
    terminal_type: str
    pause_on_completion: float
    agent_execution_mode: str  # "auto", "native_tui", "headless"


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
    keep_worktree: bool     # Leave the worktree on disk after the run


class VerificationConfig(TypedDict, total=False):
    """Configuration for how several verifiers reach one verdict (Tier 2 #8)."""
    consensus: str  # "unanimous", "majority", or "any"


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
    "keep_worktree": True,
}

DEFAULT_VERIFICATION_CONFIG: VerificationConfig = {
    "consensus": "unanimous",
}

VALID_ISOLATION_MODES = ("auto", "worktree", "none")
VALID_CONSENSUS_POLICIES = ("unanimous", "majority", "any")


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

        # A model may be a single id, or a list of ids forming an escalation
        # ladder: index 0 is used for the initial attempt, index N for repair
        # attempt N (clamped to the last entry). See make_role_node.
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
            model_value = ladder
        elif raw_model:
            model_value = str(raw_model).strip()
        else:
            model_value = None

        agent_entry: AgentConfig = {
            "agent": str(item["agent"]).strip(),
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

    # 4. Validate agent model selection against catalog.
    #    Every rung of an escalation ladder is validated independently.
    for agent_entry in validated_agents:
        agent_name = agent_entry["agent"]
        configured = agent_entry.get("model")
        if not configured or agent_name not in validated_models:
            continue

        ladder = configured if isinstance(configured, list) else [configured]
        allowed = [m["id"] for m in validated_models[agent_name]]
        if not allowed:
            continue

        for rung, model_id in enumerate(ladder):
            if model_id in allowed:
                continue
            formatted_available = "\n".join(f"  - {m_id}" for m_id in allowed)
            if agent_name == "opencode":
                extra_hint = (
                    "If this is a custom or newly available OpenCode model,\n"
                    "add its exact provider/model identifier to orchestrator.yaml."
                )
            else:
                extra_hint = "Edit orchestrator.yaml and select one of the available model IDs."

            position = f" (escalation step {rung + 1} of {len(ladder)})" if len(ladder) > 1 else ""
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
        # Check in project_root or current working directory
        search_dirs = []
        if project_root:
            search_dirs.append(Path(project_root).resolve())
        search_dirs.append(Path(os.getcwd()).resolve())

        for d in search_dirs:
            candidate = d / "orchestrator.yaml"
            if candidate.is_file():
                target_file = candidate
                break

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
        agent_match = (agent is None) or (entry.get("agent") == agent)
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
            "agent_execution_mode": "auto",
        }
    exec_cfg = config["execution"]
    return {
        "visible_terminals": bool(exec_cfg.get("visible_terminals", False)),
        "terminal_type": str(exec_cfg.get("terminal_type", "auto")),
        "pause_on_completion": float(exec_cfg.get("pause_on_completion", 1.5)),
        "agent_execution_mode": str(exec_cfg.get("agent_execution_mode", "auto")),
    }


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
