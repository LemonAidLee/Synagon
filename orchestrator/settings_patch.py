"""Project-level settings the Settings page may edit, written the same surgical way `teams.py`
writes the team: only the top-level blocks touched are rewritten, and everything else in
`orchestrator.yaml` — every other section, every comment — is left exactly as it was.

Each field maps to one of four top-level blocks (`execution`, `budget`, `run_store`, or the bare
scalar `max_repair_attempts`). Writing one field re-renders the *whole* block it lives in, using
the block's current fully-resolved value (`get_execution_config` etc.) with only the patched
field changed — so a save never resets a sibling field to its default, the same guarantee
`teams.py` gives a single role edit.
"""

import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from orchestrator.config import (
    MAX_RETRY_ATTEMPTS,
    ConfigValidationError,
    get_budget_config,
    get_execution_config,
    get_max_repair_attempts,
    get_run_store_config,
    validate_config,
)
from orchestrator.teams import config_file_path, replace_top_level_block

#: Every field the Settings page may write: which top-level block it lives in, its path within
#: that block's `get_*_config()` dict, and how to validate a candidate value.
SETTINGS_FIELDS: Dict[str, Dict[str, Any]] = {
    "execution.agent_execution_mode": {
        "block": "execution", "path": ("agent_execution_mode",),
        "validate": lambda v: v in ("auto", "native_tui", "headless"),
        "error": "must be one of: auto, native_tui, headless",
    },
    "execution.visible_terminals": {
        "block": "execution", "path": ("visible_terminals",),
        "validate": lambda v: isinstance(v, bool),
        "error": "must be true or false",
    },
    "execution.terminal_type": {
        "block": "execution", "path": ("terminal_type",),
        "validate": lambda v: v in (
            "auto", "antigravity_integrated", "integrated", "windows_terminal",
            "console", "wt", "cmd", "none",
        ),
        "error": (
            "must be one of: auto, antigravity_integrated, integrated, windows_terminal, "
            "console, wt, cmd, none"
        ),
    },
    "execution.retry.attempts": {
        "block": "execution", "path": ("retry", "attempts"),
        "validate": lambda v: (
            isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= MAX_RETRY_ATTEMPTS
        ),
        "error": f"must be an integer from 1 to {MAX_RETRY_ATTEMPTS}",
    },
    "execution.retry.escalate_model": {
        "block": "execution", "path": ("retry", "escalate_model"),
        "validate": lambda v: isinstance(v, bool),
        "error": "must be true or false",
    },
    "max_repair_attempts": {
        "block": "max_repair_attempts", "path": (),
        "validate": lambda v: isinstance(v, int) and not isinstance(v, bool) and v >= 0,
        "error": "must be a non-negative integer",
    },
    "budget.max_duration_seconds": {
        "block": "budget", "path": ("max_duration_seconds",),
        "validate": lambda v: isinstance(v, int) and not isinstance(v, bool) and v >= 0,
        "error": "must be a non-negative integer (0 = unlimited)",
    },
    "budget.goal_max_duration_seconds": {
        "block": "budget", "path": ("goal_max_duration_seconds",),
        "validate": lambda v: isinstance(v, int) and not isinstance(v, bool) and v >= 0,
        "error": "must be a non-negative integer (0 = unlimited)",
    },
    "run_store.max_output_chars": {
        "block": "run_store", "path": ("max_output_chars",),
        "validate": lambda v: isinstance(v, int) and not isinstance(v, bool) and v >= 0,
        "error": "must be a non-negative integer (0 = unlimited)",
    },
}


def _resolved_blocks(config: Optional[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {
        "execution": dict(get_execution_config(config)),
        "budget": dict(get_budget_config(config)),
        "run_store": dict(get_run_store_config(config)),
    }


def current_settings(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Every field's current value, read the same way the engine reads it."""
    blocks = _resolved_blocks(config)
    values: Dict[str, Any] = {}
    for name, spec in SETTINGS_FIELDS.items():
        if spec["block"] == "max_repair_attempts":
            values[name] = get_max_repair_attempts(config)
            continue
        node: Any = blocks[spec["block"]]
        for key in spec["path"]:
            node = node.get(key) if isinstance(node, dict) else None
        values[name] = node
    return values


def validate_patch(patch: Dict[str, Any]) -> List[str]:
    """Every field in `patch` that is unknown or out of range, in the words the page shows."""
    problems: List[str] = []
    for name, value in patch.items():
        spec = SETTINGS_FIELDS.get(name)
        if spec is None:
            problems.append(f"'{name}' is not a setting this page can write.")
            continue
        if not spec["validate"](value):
            problems.append(f"'{name}' {spec['error']}.")
    return problems


def _set_path(node: Dict[str, Any], path: Tuple[str, ...], value: Any) -> None:
    for key in path[:-1]:
        node = node.setdefault(key, {})
    if path:
        node[path[-1]] = value


def _render_mapping_block(key: str, mapping: Dict[str, Any]) -> str:
    lines = [f"{key}:"]
    for k, v in mapping.items():
        if isinstance(v, dict):
            lines.append(f"  {k}:")
            for k2, v2 in v.items():
                lines.append(f"    {k2}: {v2}")
        else:
            lines.append(f"  {k}: {v}")
    return "\n".join(lines)


def apply_settings_to_text(text: str, config: Optional[Dict[str, Any]], patch: Dict[str, Any]) -> str:
    """Return the config file text with `patch` applied. Pure. Assumes `patch` already validated."""
    blocks = _resolved_blocks(config)
    blocks["execution"]["retry"] = dict(blocks["execution"].get("retry") or {})
    touched = set()

    for name, value in patch.items():
        spec = SETTINGS_FIELDS[name]
        block_name = spec["block"]
        if block_name == "max_repair_attempts":
            touched.add(block_name)
            continue
        _set_path(blocks[block_name], spec["path"], value)
        touched.add(block_name)

    updated = text
    for block_name in ("execution", "budget", "run_store"):
        if block_name in touched:
            updated = replace_top_level_block(
                updated, block_name, _render_mapping_block(block_name, blocks[block_name])
            )
    if "max_repair_attempts" in touched:
        updated = replace_top_level_block(
            updated, "max_repair_attempts",
            f"max_repair_attempts: {patch['max_repair_attempts']}",
        )
    return updated


def check_settings_text(text: str) -> Tuple[bool, Optional[str]]:
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
    return True, None


def write_settings(
    project_root: str,
    config: Optional[Dict[str, Any]],
    patch: Dict[str, Any],
    config_path: Optional[str] = None,
    backup: bool = True,
) -> Dict[str, Any]:
    """Write validated settings into `orchestrator.yaml`, surgically. Never raises.

    Mirrors `teams.write_team` exactly: validate before writing, back up before overwriting,
    write through a temp file. Returns
    ``{"ok": bool, "path": str, "backup": str|None, "error": str|None, "problems": [...]}``.
    """
    path = config_file_path(project_root, config_path)
    result: Dict[str, Any] = {
        "ok": False, "path": str(path), "backup": None, "error": None, "problems": [],
    }

    problems = validate_patch(patch)
    if problems:
        result["problems"] = problems
        result["error"] = "refusing to write invalid settings"
        return result

    try:
        original = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        result["error"] = f"{path} does not exist, so there is nothing to edit"
        return result
    except Exception as exc:
        result["error"] = f"could not read {path}: {exc}"
        return result

    candidate = apply_settings_to_text(original, config, patch)
    ok, problem = check_settings_text(candidate)
    if not ok:
        result["error"] = f"refusing to write a configuration that would not load: {problem}"
        result["problems"] = [problem or "unknown"]
        return result

    if candidate == original:
        result.update({"ok": True, "unchanged": True})
        return result

    if backup:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_path = path.with_name(f"{path.name}.bak-{stamp}")
        suffix = 1
        while backup_path.exists():
            backup_path = path.with_name(f"{path.name}.bak-{stamp}-{suffix}")
            suffix += 1
        try:
            shutil.copy2(path, backup_path)
            result["backup"] = str(backup_path)
        except Exception as exc:
            result["error"] = f"refusing to write without a backup: {exc}"
            return result

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

    result["ok"] = True
    return result
