"""The team model behind the design surface (Roadmap Phase 7).

`orchestrator.yaml` is already the team model: who is in the pipeline, in what order, on which
model, playing which role. A node editor is therefore a **view over that file**, not a second
description of the same thing — which is why this module is small. It owns three jobs:

1. reading the current team out of a config,
2. the **templates** — a *Careful Team* with three verifiers and unanimous consensus, a *Fast
   Team* with one of each — because the fastest way to compose a team is to start from one,
3. writing a team back into `orchestrator.yaml` **without destroying it**.

Why the write is surgical
-------------------------
`orchestrator.yaml` is two hundred lines of commentary explaining every knob. Round-tripping it
through a YAML dumper would produce a valid file that had lost the reason for every value in
it. So a write replaces exactly the lines it owns — the `agents:` block, `max_repair_attempts`,
`verification.consensus` — and leaves every other byte, comment included, untouched.

Load-bearing rules
------------------
* **Validate before writing, never after.** The candidate text is parsed and run through
  `validate_config` first. A config file that fails to load is a project that cannot run, so
  an editor that could produce one would be a foot-gun with a GUI.
* **Never overwrite without a copy.** Every write leaves `orchestrator.yaml.bak-<stamp>`
  beside the file. The user's data outranks tidiness here exactly as it does in `workspace.py`.
* **A phase is consecutive same-role agents.** That is what `graph.py` already means by a
  phase; the editor shows the same grouping rather than inventing a second notion of one.
* **Two implementers in a phase is refused here too.** `config.py` rejects that configuration
  because concurrent writes to one working directory have no defined outcome. The editor says
  so *before* you save rather than after.
"""

import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from orchestrator.config import (
    ConfigValidationError,
    get_max_repair_attempts,
    ladder_rungs,
    validate_config,
)

DEFAULT_CONFIG_FILENAME = "orchestrator.yaml"


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------
#
# Each template is a *starting point*, not a preset the engine knows about: applying one
# writes ordinary `agents:` entries that a person can then edit by hand or in the editor. The
# models named here are the ones this project's catalog ships with.

TEAM_TEMPLATES: Dict[str, Dict[str, Any]] = {
    "solo": {
        "label": "Solo",
        "description": (
            "One implementer and one verifier. The cheapest team that still refuses to trust "
            "the implementer's own claim of success."
        ),
        "consensus": "any",
        "max_repair_attempts": 1,
        "agents": [
            {"agent": "opencode", "model": "opencode/gpt-5.1-codex", "role": "implementer"},
            {"agent": "claude", "model": "sonnet", "role": "verifier"},
        ],
    },
    "fast": {
        "label": "Fast Team",
        "description": (
            "One of each role. Research, a plan, an implementation, one verification - the "
            "shortest pipeline that still has someone thinking before someone writes."
        ),
        "consensus": "unanimous",
        "max_repair_attempts": 1,
        "agents": [
            {"agent": "antigravity", "model": "gemini-3.8-flash-high", "role": "researcher"},
            {"agent": "claude", "model": "sonnet", "role": "planner"},
            {"agent": "opencode", "model": "opencode/gpt-5.1-codex", "role": "implementer"},
            {"agent": "claude", "model": "sonnet", "role": "verifier"},
        ],
    },
    "balanced": {
        "label": "Balanced Team",
        "description": (
            "Two verifiers under majority consensus, and an implementer that escalates to a "
            "stronger model once its first attempt has been sent back."
        ),
        "consensus": "majority",
        "max_repair_attempts": 2,
        "agents": [
            {"agent": "antigravity", "model": "gemini-3.8-flash-high", "role": "researcher"},
            {"agent": "claude", "model": "sonnet", "role": "planner"},
            {
                "agent": "opencode",
                "model": ["opencode/gpt-5.1-codex", "anthropic/claude-sonnet-4-5"],
                "role": "implementer",
            },
            {"agent": "claude", "model": "sonnet", "role": "verifier"},
            {"agent": "opencode", "model": "opencode/gpt-5.1-codex", "role": "verifier"},
        ],
    },
    "careful": {
        "label": "Careful Team",
        "description": (
            "Two researchers, three verifiers, unanimous consensus, and three repair attempts. "
            "Expensive on purpose: nothing passes unless every verifier agrees it did."
        ),
        "consensus": "unanimous",
        "max_repair_attempts": 3,
        "agents": [
            {"agent": "antigravity", "model": "gemini-3.8-flash-high", "role": "researcher"},
            {"agent": "claude", "model": "sonnet", "role": "researcher"},
            {"agent": "claude", "model": "sonnet", "role": "planner"},
            {
                "agent": "opencode",
                "model": ["opencode/gpt-5.1-codex", "anthropic/claude-sonnet-4-5"],
                "role": "implementer",
            },
            {"agent": "claude", "model": "sonnet", "role": "verifier"},
            {"agent": "opencode", "model": "opencode/gpt-5.1-codex", "role": "verifier"},
            {"agent": "antigravity", "model": "gemini-3.8-flash-high", "role": "verifier"},
        ],
    },
}


def list_templates() -> List[Dict[str, Any]]:
    """Return every team template, with the shape it would produce. Pure."""
    return [
        {
            "name": name,
            "label": template["label"],
            "description": template["description"],
            "consensus": template["consensus"],
            "max_repair_attempts": template["max_repair_attempts"],
            "agents": [dict(a) for a in template["agents"]],
            "phases": phases_of(template["agents"]),
        }
        for name, template in TEAM_TEMPLATES.items()
    ]


def get_template(name: str) -> Optional[Dict[str, Any]]:
    """Return one template as a team document, or None when the name is unknown. Pure."""
    template = TEAM_TEMPLATES.get(str(name or "").strip().lower())
    if not template:
        return None
    return {
        "agents": [dict(a) for a in template["agents"]],
        "consensus": template["consensus"],
        "max_repair_attempts": template["max_repair_attempts"],
        "template": name,
    }


# ---------------------------------------------------------------------------
# Reading a team out of a config
# ---------------------------------------------------------------------------


def phases_of(agents: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Group consecutive same-role agents into phases. Pure.

    This is `graph.py`'s definition, not a new one: consecutive entries sharing a role run as
    one parallel phase - an ensemble of researchers, a quorum of verifiers - and the editor
    draws exactly that.
    """
    phases: List[Dict[str, Any]] = []
    for entry in agents or []:
        role = str(entry.get("role") or "")
        if phases and phases[-1]["role"] == role:
            phases[-1]["agents"].append(dict(entry))
        else:
            phases.append({"role": role, "agents": [dict(entry)]})
    for index, phase in enumerate(phases):
        phase["index"] = index
        phase["parallel"] = len(phase["agents"]) > 1
    return phases


def team_from_config(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Read the current team out of a loaded config. Pure."""
    config = config or {}
    verification = config.get("verification") or {}
    agents = [dict(a) for a in (config.get("agents") or [])]
    return {
        "agents": agents,
        "phases": phases_of(agents),
        "consensus": str(verification.get("consensus") or "unanimous"),
        "max_repair_attempts": get_max_repair_attempts(config),
    }


def catalog_from_config(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Return what the editor is allowed to offer: providers, models, and roles. Pure.

    The editor never invents a choice. Everything it can put in a dropdown comes from the
    same catalog `preflight.py` validates against, so a team composed in the browser cannot
    name a model this project does not know.
    """
    config = config or {}
    return {
        "models": {
            provider: [
                {"id": m.get("id"), "name": m.get("name") or m.get("id")}
                for m in entries or []
            ]
            for provider, entries in (config.get("models") or {}).items()
        },
        "roles": {
            name: (role or {}).get("responsibility", "")
            for name, role in (config.get("roles") or {}).items()
        },
        "consensus_policies": ["unanimous", "majority", "any"],
    }


# ---------------------------------------------------------------------------
# Validating a team before it is written
# ---------------------------------------------------------------------------


def normalize_team(team: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Coerce whatever the editor posted into the shape this module writes. Pure.

    Nothing here trusts its input: the design surface is a web page, and a page is data.
    """
    team = team if isinstance(team, dict) else {}
    agents: List[Dict[str, Any]] = []

    for raw in team.get("agents") or []:
        if not isinstance(raw, dict):
            continue
        raw_agent = raw.get("agent")
        if isinstance(raw_agent, list):
            rungs = [str(a).strip() for a in raw_agent if str(a).strip()]
            agent: Any = rungs if len(rungs) > 1 else (rungs[0] if rungs else "")
        else:
            agent = str(raw_agent or "").strip()
        role = str(raw.get("role") or "").strip()
        if not agent or not role:
            continue

        model = raw.get("model")
        if isinstance(model, list):
            ladder = [str(m).strip() for m in model if str(m).strip()]
            # A one-entry list normally means "a ladder the editor has not filled in yet",
            # and collapsing it keeps the written YAML plain. Beside an *agent* ladder it
            # means something else - one model for several providers - so the shape is kept
            # and `validate_team` gets to say the lengths do not pair up.
            collapse = len(ladder) <= 1 and not isinstance(agent, list)
            model_value: Any = (
                (ladder[0] if ladder else None) if collapse else (ladder or None)
            )
        elif isinstance(model, str) and model.strip():
            model_value = model.strip()
        else:
            model_value = None

        agents.append({"agent": agent, "model": model_value, "role": role})

    consensus = str(team.get("consensus") or "unanimous").strip().lower()
    if consensus not in ("unanimous", "majority", "any"):
        consensus = "unanimous"

    try:
        attempts = max(0, int(team.get("max_repair_attempts", 2)))
    except (TypeError, ValueError):
        attempts = 2

    return {
        "agents": agents,
        "phases": phases_of(agents),
        "consensus": consensus,
        "max_repair_attempts": attempts,
    }


def validate_team(team: Dict[str, Any], config: Optional[Dict[str, Any]] = None) -> List[str]:
    """Return everything wrong with a team, in the words the editor should show. Pure.

    An empty list means it would produce a config this project can run. This duplicates none
    of `validate_config`'s logic - it is the *early* half, so the editor can grey a Save
    button out instead of letting a person discover the problem in a stack trace.
    """
    problems: List[str] = []
    agents = team.get("agents") or []
    config = config or {}
    catalog = config.get("models") or {}
    roles = config.get("roles") or {}

    if not agents:
        problems.append("A team needs at least one agent.")

    roles_used = {str(a.get("role")) for a in agents}
    if agents and "implementer" not in roles_used:
        problems.append("No agent has the 'implementer' role, so nothing would be written.")
    if agents and "verifier" not in roles_used:
        problems.append(
            "No agent has the 'verifier' role. Without one, the implementer's claim of "
            "success is the only evidence there is."
        )

    for index, entry in enumerate(agents, start=1):
        role = str(entry.get("role") or "")
        if roles and role not in roles:
            problems.append(
                f"Agent {index}: role '{role}' has no responsibility defined under roles:."
            )

        # Both fields may be ladders, and `ladder_rungs` is the one place that knows how the
        # two pair up. Mismatched lengths are refused before the rungs are read, because a
        # zip that silently truncates would validate a pairing the run will never make.
        raw_agent = entry.get("agent")
        raw_model = entry.get("model")
        if (
            isinstance(raw_agent, list)
            and isinstance(raw_model, list)
            and len(raw_agent) != len(raw_model)
        ):
            problems.append(
                f"Agent {index}: {len(raw_agent)} agents and {len(raw_model)} models. A "
                f"ladder pairs them rung by rung, so the two lists must be the same length."
            )
            continue

        for rung, (agent, model) in enumerate(ladder_rungs(entry), start=1):
            where = f" (step {rung})" if isinstance(raw_agent, list) or isinstance(raw_model, list) else ""
            if catalog and agent not in catalog:
                problems.append(
                    f"Agent {index}{where}: '{agent}' is not a provider in the model catalog."
                )
                continue
            known = {str(m.get("id")) for m in catalog.get(agent, [])}
            if model and known and str(model) not in known:
                problems.append(
                    f"Agent {index}{where}: '{agent}' has no model '{model}' in the catalog."
                )

    for phase in phases_of(agents):
        if phase["role"] == "implementer" and len(phase["agents"]) > 1:
            problems.append(
                "Two implementers are configured consecutively, which would run them in "
                "parallel against the same working directory. Use a model ladder on one "
                "implementer instead."
            )

    return problems


# ---------------------------------------------------------------------------
# Writing a team back, surgically
# ---------------------------------------------------------------------------


def render_agents_block(agents: List[Dict[str, Any]]) -> str:
    """Render a team's agents as the `agents:` block of a YAML file. Pure."""
    lines = ["agents:"]
    for entry in agents or []:
        lines.append(f"  - agent: {entry.get('agent')}")
        model = entry.get("model")
        if isinstance(model, list):
            lines.append("    model:")
            for step, value in enumerate(model):
                comment = "  # first attempt" if step == 0 else (
                    "  # first repair, and beyond" if step == 1 else ""
                )
                lines.append(f"      - {value}{comment}")
        elif model:
            lines.append(f"    model: {model}")
        lines.append(f"    role: {entry.get('role')}")
        lines.append("")
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def replace_top_level_block(text: str, key: str, block: str) -> str:
    """Replace one top-level YAML block, leaving every other byte alone. Pure.

    The block runs from `key:` to the next line that starts at column zero - which is the next
    top-level key *or* the comment banner introducing it. Stopping at the comment is the whole
    point: those banners are the documentation, and a writer that ate them would make the file
    worse every time it ran.
    """
    lines = text.splitlines()
    start = next(
        (i for i, line in enumerate(lines) if re.match(rf"^{re.escape(key)}\s*:", line)),
        None,
    )
    if start is None:
        separator = "" if text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
        return text + separator + block + "\n"

    end = len(lines)
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if line.strip() and not line[0].isspace():
            end = index
            break

    # Blank lines before the next section belong to the *gap*, not to this block: leaving
    # them in the tail is what stops a rewrite from growing a blank line every time it runs.
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1

    return "\n".join(lines[:start] + block.splitlines() + lines[end:]) + (
        "\n" if text.endswith("\n") else ""
    )


def replace_nested_scalar(text: str, parent: str, key: str, value: Any) -> str:
    """Replace `parent: / key: value`, preserving indentation and comments. Pure."""
    lines = text.splitlines()
    start = next(
        (i for i, line in enumerate(lines) if re.match(rf"^{re.escape(parent)}\s*:", line)),
        None,
    )
    if start is None:
        separator = "\n" if text.endswith("\n") else "\n\n"
        return text + separator + f"{parent}:\n  {key}: {value}\n"

    for index in range(start + 1, len(lines)):
        line = lines[index]
        if line.strip() and not line[0].isspace():
            break
        match = re.match(rf"^(\s+){re.escape(key)}\s*:(.*)$", line)
        if match:
            indent = match.group(1)
            comment = ""
            tail = match.group(2)
            if "#" in tail:
                comment = "  " + tail[tail.index("#"):].strip()
            lines[index] = f"{indent}{key}: {value}{comment}"
            return "\n".join(lines) + ("\n" if text.endswith("\n") else "")

    lines.insert(start + 1, f"  {key}: {value}")
    return "\n".join(lines) + ("\n" if text.endswith("\n") else "")


def apply_team_to_text(text: str, team: Dict[str, Any]) -> str:
    """Return the config file text with this team written into it. Pure."""
    updated = replace_top_level_block(text, "agents", render_agents_block(team.get("agents") or []))
    updated = replace_top_level_block(
        updated, "max_repair_attempts", f"max_repair_attempts: {team.get('max_repair_attempts', 2)}"
    )
    return replace_nested_scalar(updated, "verification", "consensus", team.get("consensus", "unanimous"))


def config_file_path(project_root: str, config_path: Optional[str] = None) -> Path:
    """Resolve which file the design surface edits."""
    if config_path:
        candidate = Path(config_path)
        return candidate if candidate.is_absolute() else Path(project_root) / candidate
    return Path(project_root) / DEFAULT_CONFIG_FILENAME


def check_team_text(text: str) -> Tuple[bool, Optional[str]]:
    """Return whether a candidate config file would load and validate. Pure enough (no I/O)."""
    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return False, f"the result is not valid YAML: {exc}"
    if not isinstance(parsed, dict):
        return False, "the result is not a YAML mapping"
    try:
        validate_config(parsed)
    except ConfigValidationError as exc:
        return False, str(exc)
    except Exception as exc:  # pragma: no cover - defensive
        return False, str(exc)
    return True, None


def write_team(
    project_root: str,
    team: Dict[str, Any],
    config_path: Optional[str] = None,
    backup: bool = True,
) -> Dict[str, Any]:
    """Write a team into `orchestrator.yaml`, or refuse and say why. Never raises.

    Returns:
        ``{"ok": bool, "path": str, "backup": str|None, "error": str|None,
        "problems": [...]}``. A refusal leaves the file exactly as it was: the candidate text
        is validated *before* anything is written, because a config that cannot load is a
        project that cannot run.
    """
    path = config_file_path(project_root, config_path)
    result: Dict[str, Any] = {"ok": False, "path": str(path), "backup": None,
                              "error": None, "problems": []}

    normalized = normalize_team(team)
    try:
        original = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        result["error"] = f"{path} does not exist, so there is nothing to edit"
        return result
    except Exception as exc:
        result["error"] = f"could not read {path}: {exc}"
        return result

    candidate = apply_team_to_text(original, normalized)
    ok, problem = check_team_text(candidate)
    if not ok:
        result["error"] = f"refusing to write a configuration that would not load: {problem}"
        result["problems"] = [problem or "unknown"]
        return result

    if candidate == original:
        result.update({"ok": True, "unchanged": True, "team": normalized})
        return result

    if backup:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_path = path.with_name(f"{path.name}.bak-{stamp}")
        try:
            shutil.copy2(path, backup_path)
            result["backup"] = str(backup_path)
        except Exception as exc:
            result["error"] = f"refusing to write without a backup: {exc}"
            return result

    # Write through a temp file and swap, so a failure part-way through the write
    # cannot truncate or corrupt the live `orchestrator.yaml` - it either lands
    # whole or not at all.
    tmp_path = path.with_name(f"{path.name}.tmp")
    try:
        tmp_path.write_text(candidate, encoding="utf-8")
        os.replace(tmp_path, path)
    except Exception as exc:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        result["error"] = f"could not write {path}: {exc}"
        return result

    result.update({"ok": True, "team": normalized})
    return result


def format_team(team: Dict[str, Any]) -> str:
    """Render a team as a readable pipeline."""
    phases = team.get("phases") or phases_of(team.get("agents") or [])
    if not phases:
        return "  (no agents configured)"

    lines: List[str] = []
    for phase in phases:
        marker = "||" if phase.get("parallel") else "->"
        lines.append(f"  {marker} {phase['role']}")
        for entry in phase["agents"]:
            model = entry.get("model")
            if isinstance(model, list):
                shown = " -> ".join(str(m) for m in model) + "   (escalation ladder)"
            else:
                shown = str(model or "(provider default)")
            lines.append(f"       {entry.get('agent'):<14} {shown}")
    lines.append("")
    lines.append(
        f"  consensus: {team.get('consensus')}    "
        f"repair attempts: {team.get('max_repair_attempts')}"
    )
    return "\n".join(lines)
