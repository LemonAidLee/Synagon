"""Per-machine UI/workspace preferences (Package I).

Theme, density, and similar choices are personal, not project state: they do not belong in
`orchestrator.yaml`, which is a project file that is often shared or committed. This module is
the other place such a choice can live, one per machine, following the exact pattern
`native_sessions.py` already uses for per-machine state: a JSON file under `~/.orchestrator/`,
written atomically (temp file then `os.replace`), with a test-only path override.

Every field has a default, so `load_preferences()` never fails and a caller never has to
special-case a missing key.
"""

import json
import os
import threading
from typing import Any, Dict

#: Override for the file location (tests point it at a temporary file).
PREFERENCES_ENV = "ORCHESTRATOR_PREFERENCES_FILE"

_lock = threading.Lock()

THEMES = ("theme-srcery", "theme-moonfly", "theme-jellybeans", "theme-tender", "theme-miasma")
DENSITIES = ("comfortable", "compact")
STARTUP_VIEWS = ("cockpit", "office", "design")
BUDGET_DISPLAYS = ("detailed", "compact", "hidden")

DEFAULT_PREFERENCES: Dict[str, Any] = {
    "theme": "theme-srcery",
    "density": "comfortable",
    "terminal_font_size": 13,
    "reduced_motion": False,
    "default_workspace_dir": None,
    "startup_view": "cockpit",
    "budget_display": "compact",
    "prune_default_max_age_days": 30,
    "prune_default_keep_failed": True,
}


class PreferencesValidationError(ValueError):
    """A preferences patch named an unknown key, or gave one an out-of-range value."""


def preferences_path() -> str:
    """Where preferences are recorded."""
    override = os.environ.get(PREFERENCES_ENV)
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), ".orchestrator", "preferences.json")


def _validate(key: str, value: Any) -> None:
    if key not in DEFAULT_PREFERENCES:
        raise PreferencesValidationError(f"'{key}' is not a known preference.")
    if key == "theme" and value not in THEMES:
        raise PreferencesValidationError(f"'theme' must be one of: {', '.join(THEMES)}.")
    if key == "density" and value not in DENSITIES:
        raise PreferencesValidationError(f"'density' must be one of: {', '.join(DENSITIES)}.")
    if key == "startup_view" and value not in STARTUP_VIEWS:
        raise PreferencesValidationError(f"'startup_view' must be one of: {', '.join(STARTUP_VIEWS)}.")
    if key == "budget_display" and value not in BUDGET_DISPLAYS:
        raise PreferencesValidationError(f"'budget_display' must be one of: {', '.join(BUDGET_DISPLAYS)}.")
    if key == "terminal_font_size":
        if not isinstance(value, int) or isinstance(value, bool) or not (10 <= value <= 24):
            raise PreferencesValidationError("'terminal_font_size' must be an integer from 10 to 24.")
    if key in ("reduced_motion", "prune_default_keep_failed") and not isinstance(value, bool):
        raise PreferencesValidationError(f"'{key}' must be true or false.")
    if key == "default_workspace_dir" and value is not None and not isinstance(value, str):
        raise PreferencesValidationError("'default_workspace_dir' must be a string or null.")
    if key == "prune_default_max_age_days":
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise PreferencesValidationError("'prune_default_max_age_days' must be a non-negative integer.")


def load_preferences() -> Dict[str, Any]:
    """Every preference, defaults merged with whatever is saved on disk. Never raises."""
    resolved = dict(DEFAULT_PREFERENCES)
    try:
        with open(preferences_path(), "r", encoding="utf-8") as handle:
            saved = json.load(handle)
    except (OSError, ValueError):
        return resolved
    if isinstance(saved, dict):
        for key, value in saved.items():
            if key in DEFAULT_PREFERENCES:
                resolved[key] = value
    return resolved


def save_preferences(patch: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and merge `patch` into the saved preferences, and return the full result.

    Raises:
        PreferencesValidationError: `patch` has an unknown key or an out-of-range value.
            Nothing is written in that case.
    """
    for key, value in patch.items():
        _validate(key, value)

    with _lock:
        current = load_preferences()
        current.update(patch)
        path = preferences_path()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(current, handle, indent=2)
        os.replace(tmp, path)
    return current
