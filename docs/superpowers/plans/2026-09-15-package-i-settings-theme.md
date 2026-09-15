# Package I: Settings and Theme Integration — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move Settings and Theme into the desktop app's top menu, and expand the provider-only `/settings` page into a real, sectioned settings surface backed by two new persistence paths — a per-machine preferences file and narrow, validated writes into `orchestrator.yaml` — using only what the existing config/daemon architecture already supports.

**Architecture:** A new per-machine JSON store (`orchestrator/preferences.py`, same atomic-write pattern as `native_sessions.py`) holds UI/workspace preferences; a new text-surgical patch module (`orchestrator/settings_patch.py`, same block-replace pattern as `teams.py`) writes project-level execution/budget/run-store fields into `orchestrator.yaml`. Both are exposed through new daemon routes added to `orchestrator/daemon.py`, outside `daemon.control()`'s closed verb list. The desktop menu (`desktop/main.js`) gains an expanded Settings menu and a Theme submenu. `orchestrator/web/settings.html` grows from one page into six tabs; theme CSS moves out of `cockpit.html` into a shared, daemon-served stylesheet so both pages theme identically.

**Tech Stack:** Python 3 (stdlib `http.server`, `pytest`/`unittest`), vanilla HTML/CSS/JS (no framework, no build step — matches every existing page), Electron 33 (`desktop/main.js`), PyYAML.

**Spec:** `docs/superpowers/specs/2026-09-15-package-i-settings-theme-design.md`

## Global Constraints

- No System/Light/Dark theme model — only the five existing named schemes (`theme-srcery`, `theme-moonfly`, `theme-jellybeans`, `theme-tender`, `theme-miasma`) are exposed.
- No new entry in `daemon.control()`'s closed action-verb list — every new write is a narrow route outside that dispatcher.
- No provider enable/disable flag independent of role assignment.
- No recent-projects list.
- No change to worktree-per-run cleanup, detached-login process ownership, `RUNNABLE_AGENTS`, or any existing provider-auth probe logic.
- Every write path validates before writing and never raises past its caller (mirrors `teams.write_team`).
- No credential, token, keyring, or private account data is ever added to any new route or page.
- No framework, no build step, no bundler — every page stays a single static HTML file with inline `<style>`/`<script>`, per this repo's existing convention.

---

## Task 1: Per-machine preferences store

**Files:**
- Create: `orchestrator/preferences.py`
- Test: `tests/test_preferences.py`

**Interfaces:**
- Produces: `preferences.PREFERENCES_ENV: str`, `preferences.DEFAULT_PREFERENCES: Dict[str, Any]`, `preferences.PreferencesValidationError(ValueError)`, `preferences.preferences_path() -> str`, `preferences.load_preferences() -> Dict[str, Any]`, `preferences.save_preferences(patch: Dict[str, Any]) -> Dict[str, Any]`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_preferences.py
"""Tests for orchestrator/preferences.py (Package I).

Shaped like `native_sessions.py`'s own test style: a temp-file override for the store's
location, defaults when nothing is saved, and an atomic write that never leaves a half-written
file behind.
"""

import json
import os
import tempfile
import unittest

from orchestrator import preferences


class PreferencesTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="orch_prefs_")
        self.path = os.path.join(self.tmpdir, "preferences.json")
        self._old_env = os.environ.get(preferences.PREFERENCES_ENV)
        os.environ[preferences.PREFERENCES_ENV] = self.path
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        if self._old_env is None:
            os.environ.pop(preferences.PREFERENCES_ENV, None)
        else:
            os.environ[preferences.PREFERENCES_ENV] = self._old_env

    def test_load_with_no_file_returns_defaults(self):
        self.assertEqual(preferences.load_preferences(), preferences.DEFAULT_PREFERENCES)

    def test_save_then_load_round_trips(self):
        preferences.save_preferences({"theme": "theme-moonfly", "density": "compact"})
        loaded = preferences.load_preferences()
        self.assertEqual(loaded["theme"], "theme-moonfly")
        self.assertEqual(loaded["density"], "compact")
        self.assertEqual(loaded["terminal_font_size"], 13)

    def test_save_merges_rather_than_replaces(self):
        preferences.save_preferences({"theme": "theme-tender"})
        preferences.save_preferences({"density": "compact"})
        loaded = preferences.load_preferences()
        self.assertEqual(loaded["theme"], "theme-tender")
        self.assertEqual(loaded["density"], "compact")

    def test_unknown_key_is_refused_and_nothing_is_written(self):
        with self.assertRaises(preferences.PreferencesValidationError):
            preferences.save_preferences({"not_a_real_preference": True})
        self.assertFalse(os.path.exists(self.path))

    def test_out_of_range_font_size_is_refused(self):
        with self.assertRaises(preferences.PreferencesValidationError):
            preferences.save_preferences({"terminal_font_size": 999})

    def test_bad_theme_name_is_refused(self):
        with self.assertRaises(preferences.PreferencesValidationError):
            preferences.save_preferences({"theme": "theme-nonexistent"})

    def test_bad_boolean_field_is_refused(self):
        with self.assertRaises(preferences.PreferencesValidationError):
            preferences.save_preferences({"reduced_motion": "yes"})

    def test_negative_prune_age_is_refused(self):
        with self.assertRaises(preferences.PreferencesValidationError):
            preferences.save_preferences({"prune_default_max_age_days": -1})

    def test_write_is_atomic_via_tmp_file(self):
        preferences.save_preferences({"theme": "theme-miasma"})
        self.assertFalse(os.path.exists(self.path + ".tmp"))
        with open(self.path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        self.assertEqual(data["theme"], "theme-miasma")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_preferences.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'orchestrator.preferences'`

- [ ] **Step 3: Write the implementation**

```python
# orchestrator/preferences.py
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_preferences.py -v`
Expected: PASS (10 tests)

- [ ] **Step 5: Commit**

```bash
git add orchestrator/preferences.py tests/test_preferences.py
git commit -m "Add the per-machine preferences store for Package I"
```

---

## Task 2: Daemon `/api/preferences` routes

**Files:**
- Modify: `orchestrator/daemon.py` (inside `make_daemon_handler`'s `DaemonHandler.do_GET`, after the existing `/api/providers` block around line 1010; and `DaemonHandler.do_POST`, after the existing `/api/team` block around line 1226)
- Create: `tests/test_package_i.py`

**Interfaces:**
- Consumes: `preferences.load_preferences()`, `preferences.save_preferences(patch)`, `preferences.PreferencesValidationError` (Task 1).
- Produces: `GET /api/preferences` → 200 + preferences JSON; `POST /api/preferences` → 200 `{"ok": true, "preferences": {...}}` or 400 `{"ok": false, "error": "..."}`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_package_i.py
"""Package I - the daemon's settings/theme/preferences/diagnostics routes.

Shaped like `tests/test_package_g.py`'s daemon fixture: a real `serve_daemon` on an OS-assigned
port, called over HTTP with the daemon's own token, torn down after each test.
"""

import json
import os
import shutil
import tempfile
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from orchestrator import preferences
from orchestrator.daemon import TOKEN_HEADER, Daemon, serve_daemon


class _SettingsDaemonCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="orch_pkgi_")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.config = {"agents": [], "roles": {}}
        self.daemon = Daemon(self.root, self.config, token="t")
        self.server = serve_daemon(
            self.root, self.config, port=0, open_browser=False, serve_forever=False,
            printer=lambda *_: None, daemon=self.daemon,
        )
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]

        self.prefs_path = os.path.join(self.root, "preferences.json")
        self._old_env = os.environ.get(preferences.PREFERENCES_ENV)
        os.environ[preferences.PREFERENCES_ENV] = self.prefs_path
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        if self._old_env is None:
            os.environ.pop(preferences.PREFERENCES_ENV, None)
        else:
            os.environ[preferences.PREFERENCES_ENV] = self._old_env

    def tearDown(self):
        self.daemon.stopping.set()
        self.daemon.capture_agent_output(False)
        self.server.shutdown()
        self.server.server_close()

    def _get(self, path):
        request = urllib.request.Request(self.base + path)
        request.add_header(TOKEN_HEADER, "t")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8") or "{}")

    def _post(self, path, payload=None):
        body = json.dumps(payload if payload is not None else {}).encode("utf-8")
        request = urllib.request.Request(self.base + path, data=body, method="POST")
        request.add_header(TOKEN_HEADER, "t")
        request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8") or "{}")


class TestPreferencesRoute(_SettingsDaemonCase):
    def test_get_returns_defaults_with_no_file(self):
        status, body = self._get("/api/preferences")
        self.assertEqual(status, 200)
        self.assertEqual(body["theme"], "theme-srcery")

    def test_post_saves_and_get_reflects_it(self):
        status, body = self._post("/api/preferences", {"theme": "theme-moonfly"})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["preferences"]["theme"], "theme-moonfly")
        status, body = self._get("/api/preferences")
        self.assertEqual(body["theme"], "theme-moonfly")

    def test_post_rejects_unknown_key(self):
        status, body = self._post("/api/preferences", {"nope": 1})
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])

    def test_requires_the_daemon_token(self):
        request = urllib.request.Request(self.base + "/api/preferences")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(request, timeout=10)
        self.assertEqual(ctx.exception.code, 401)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_package_i.py -v`
Expected: FAIL — `/api/preferences` returns 404 (falls through to `base.do_GET`/control-route 404)

- [ ] **Step 3: Add the routes**

In `orchestrator/daemon.py`, inside `DaemonHandler.do_GET`, immediately after the existing block:

```python
            if route == "/api/providers":
                try:
                    self._send(_json_bytes(daemon.provider_status()), "application/json")
                except Exception as exc:
                    self._send(_json_bytes({"error": str(exc)}), "application/json", status=500)
                return
```

insert:

```python
            if route == "/api/preferences":
                from orchestrator.preferences import load_preferences

                self._send(_json_bytes(load_preferences()), "application/json")
                return
```

Inside `DaemonHandler.do_POST`, immediately after the existing block:

```python
            if route == "/api/team":
                base.do_POST(self)  # the design surface's write, unchanged
                return
```

insert:

```python
            if route == "/api/preferences":
                payload = self._read_json()
                if payload is None:
                    return
                from orchestrator.preferences import PreferencesValidationError, save_preferences

                try:
                    updated = save_preferences(payload)
                except PreferencesValidationError as exc:
                    self._send(
                        _json_bytes({"ok": False, "error": str(exc)}), "application/json", status=400
                    )
                    return
                self._send(_json_bytes({"ok": True, "preferences": updated}), "application/json")
                return
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_package_i.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add orchestrator/daemon.py tests/test_package_i.py
git commit -m "Add GET/POST /api/preferences to the daemon"
```

---

## Task 3: Project-settings patch module

**Files:**
- Create: `orchestrator/settings_patch.py`
- Test: `tests/test_settings_patch.py`

**Interfaces:**
- Consumes: `orchestrator.config.{get_execution_config, get_budget_config, get_run_store_config, get_max_repair_attempts, validate_config, ConfigValidationError, MAX_RETRY_ATTEMPTS}`, `orchestrator.teams.{config_file_path, replace_top_level_block}`.
- Produces: `settings_patch.SETTINGS_FIELDS: Dict[str, Dict[str, Any]]`, `settings_patch.current_settings(config) -> Dict[str, Any]`, `settings_patch.validate_patch(patch) -> List[str]`, `settings_patch.write_settings(project_root, config, patch, config_path=None, backup=True) -> Dict[str, Any]` (return shape: `{"ok": bool, "path": str, "backup": str|None, "error": str|None, "problems": [...]}`, matching `teams.write_team`).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_settings_patch.py
"""Tests for orchestrator/settings_patch.py (Package I).

Mirrors how teams.py is tested: a real orchestrator.yaml on disk, a surgical write that must
leave every untouched byte alone, and a refusal that leaves the file exactly as it was.
"""

import shutil
import tempfile
import unittest
from pathlib import Path

from orchestrator.config import load_config
from orchestrator.settings_patch import current_settings, validate_patch, write_settings

MINIMAL_YAML = """\
agents:
  - agent: opencode
    model: opencode/gpt-5.1-codex
    role: implementer

  - agent: claude
    model: sonnet
    role: verifier

max_repair_attempts: 2

verification:
  consensus: unanimous

roles:
  implementer:
    responsibility: write the code
  verifier:
    responsibility: check the code
"""


class SettingsPatchTestCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="orch_settings_patch_")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.path = Path(self.root) / "orchestrator.yaml"
        self.path.write_text(MINIMAL_YAML, encoding="utf-8")
        self.config = load_config(str(self.path))

    def test_current_settings_reads_defaults_when_unset(self):
        values = current_settings(self.config)
        self.assertEqual(values["execution.agent_execution_mode"], "auto")
        self.assertEqual(values["max_repair_attempts"], 2)
        self.assertEqual(values["budget.max_duration_seconds"], 0)
        self.assertEqual(values["run_store.max_output_chars"], 0)

    def test_validate_patch_rejects_unknown_field(self):
        problems = validate_patch({"not_a_field": 1})
        self.assertEqual(len(problems), 1)

    def test_validate_patch_rejects_out_of_range_retry(self):
        problems = validate_patch({"execution.retry.attempts": 999})
        self.assertEqual(len(problems), 1)

    def test_validate_patch_accepts_a_good_value(self):
        self.assertEqual(validate_patch({"execution.agent_execution_mode": "headless"}), [])

    def test_write_settings_patches_one_field_and_preserves_others(self):
        result = write_settings(
            self.root, self.config, {"execution.agent_execution_mode": "headless"},
        )
        self.assertTrue(result["ok"], result.get("error"))
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("agent_execution_mode: headless", text)
        self.assertIn("write the code", text)

        reloaded = load_config(str(self.path))
        self.assertEqual(reloaded["execution"]["agent_execution_mode"], "headless")

    def test_write_settings_preserves_unrelated_retry_fields(self):
        write_settings(self.root, self.config, {"execution.retry.attempts": 5})
        reloaded = load_config(str(self.path))
        self.assertEqual(reloaded["execution"]["retry"]["attempts"], 5)
        # escalate_model was never touched, so it keeps the engine's default.
        self.assertTrue(reloaded["execution"]["retry"]["escalate_model"])

    def test_write_settings_refuses_invalid_patch_and_writes_nothing(self):
        original = self.path.read_text(encoding="utf-8")
        result = write_settings(self.root, self.config, {"max_repair_attempts": -1})
        self.assertFalse(result["ok"])
        self.assertEqual(self.path.read_text(encoding="utf-8"), original)

    def test_write_settings_leaves_a_backup(self):
        write_settings(self.root, self.config, {"max_repair_attempts": 5})
        backups = list(Path(self.root).glob("orchestrator.yaml.bak-*"))
        self.assertEqual(len(backups), 1)

    def test_write_settings_no_op_when_value_unchanged(self):
        write_settings(self.root, self.config, {"max_repair_attempts": 2})
        text_before = self.path.read_text(encoding="utf-8")
        result = write_settings(self.root, self.config, {"max_repair_attempts": 2})
        self.assertTrue(result.get("unchanged"))
        self.assertEqual(self.path.read_text(encoding="utf-8"), text_before)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_settings_patch.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'orchestrator.settings_patch'`

- [ ] **Step 3: Write the implementation**

```python
# orchestrator/settings_patch.py
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_settings_patch.py -v`
Expected: PASS (9 tests)

- [ ] **Step 5: Commit**

```bash
git add orchestrator/settings_patch.py tests/test_settings_patch.py
git commit -m "Add the project-settings patch module for Package I"
```

---

## Task 4: Daemon `/api/settings` routes

**Files:**
- Modify: `orchestrator/daemon.py` (`do_GET`, after the `/api/preferences` block from Task 2; `do_POST`, after the `/api/preferences` block from Task 2)
- Modify: `tests/test_package_i.py` (append)

**Interfaces:**
- Consumes: `settings_patch.current_settings(config)`, `settings_patch.write_settings(project_root, config, patch, config_path)` (Task 3); `Daemon.reload_config()` (existing).
- Produces: `GET /api/settings` → 200 + current values; `POST /api/settings` → 200/400 write result, reloads `daemon.config` on success.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_package_i.py`:

```python
from orchestrator.config import load_config

MINIMAL_YAML = """\
agents:
  - agent: opencode
    model: opencode/gpt-5.1-codex
    role: implementer

  - agent: claude
    model: sonnet
    role: verifier

max_repair_attempts: 2

verification:
  consensus: unanimous

roles:
  implementer:
    responsibility: write the code
  verifier:
    responsibility: check the code
"""


class _SettingsWritableDaemonCase(_SettingsDaemonCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="orch_pkgi_settings_")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        config_path = os.path.join(self.root, "orchestrator.yaml")
        with open(config_path, "w", encoding="utf-8") as handle:
            handle.write(MINIMAL_YAML)
        self.config = load_config(config_path)
        self.daemon = Daemon(self.root, self.config, token="t")
        self.server = serve_daemon(
            self.root, self.config, port=0, open_browser=False, serve_forever=False,
            printer=lambda *_: None, daemon=self.daemon,
        )
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]
        self.prefs_path = os.path.join(self.root, "preferences.json")
        self._old_env = os.environ.get(preferences.PREFERENCES_ENV)
        os.environ[preferences.PREFERENCES_ENV] = self.prefs_path
        self.addCleanup(self._restore_env)


class TestSettingsRoute(_SettingsWritableDaemonCase):
    def test_get_returns_current_values(self):
        status, body = self._get("/api/settings")
        self.assertEqual(status, 200)
        self.assertEqual(body["max_repair_attempts"], 2)

    def test_post_patches_and_reloads(self):
        status, body = self._post("/api/settings", {"execution.agent_execution_mode": "headless"})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        status, body = self._get("/api/settings")
        self.assertEqual(body["execution.agent_execution_mode"], "headless")

    def test_post_rejects_invalid_value(self):
        status, body = self._post("/api/settings", {"max_repair_attempts": -1})
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_package_i.py -v -k Settings`
Expected: FAIL — `/api/settings` returns 404

- [ ] **Step 3: Add the routes**

In `orchestrator/daemon.py`'s `do_GET`, after the `/api/preferences` block added in Task 2, insert:

```python
            if route == "/api/settings":
                from orchestrator.settings_patch import current_settings

                try:
                    self._send(_json_bytes(current_settings(daemon.config)), "application/json")
                except Exception as exc:
                    self._send(_json_bytes({"error": str(exc)}), "application/json", status=500)
                return
```

In `do_POST`, after the `/api/preferences` block added in Task 2, insert:

```python
            if route == "/api/settings":
                payload = self._read_json()
                if payload is None:
                    return
                from orchestrator.settings_patch import write_settings

                result = write_settings(
                    daemon.project_root, daemon.config, payload, config_path=daemon.config_path
                )
                if result.get("ok"):
                    daemon.reload_config()
                self._send(
                    _json_bytes(result), "application/json", status=200 if result.get("ok") else 400
                )
                return
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_package_i.py -v`
Expected: PASS (all tests so far)

- [ ] **Step 5: Commit**

```bash
git add orchestrator/daemon.py tests/test_package_i.py
git commit -m "Add GET/POST /api/settings to the daemon"
```

---

## Task 5: `/api/doctor`, `/api/diagnostics`, `/api/app_info` routes

**Files:**
- Modify: `orchestrator/daemon.py` (add `Daemon.doctor_report`, `Daemon.diagnostics_report`, module-level `app_version()`; add three `do_GET` routes)
- Modify: `tests/test_package_i.py` (append)

**Interfaces:**
- Consumes: `orchestrator.preflight.{run_preflight, format_preflight_report}`, `orchestrator.provider_auth.{check_all_providers, format_provider_report}`, `orchestrator.config.{get_preflight_config, validate_config, ConfigValidationError}`.
- Produces: `daemon.app_version() -> str`; `Daemon.doctor_report() -> {"report": ..., "text": str}`; `Daemon.diagnostics_report() -> {"text": str}`; `GET /api/doctor`, `GET /api/diagnostics`, `GET /api/app_info`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_package_i.py`:

```python
class TestDoctorRoute(_SettingsDaemonCase):
    def test_get_returns_a_report_and_text(self):
        fake_report = {"ok": True, "strict": True, "agents": []}
        with patch("orchestrator.preflight.run_preflight", return_value=fake_report):
            status, body = self._get("/api/doctor")
        self.assertEqual(status, 200)
        self.assertIn("text", body)
        self.assertIn("report", body)


class TestDiagnosticsRoute(_SettingsDaemonCase):
    def test_get_combines_preflight_and_provider_text(self):
        with patch(
            "orchestrator.preflight.run_preflight",
            return_value={"ok": True, "strict": True, "agents": []},
        ), patch("orchestrator.provider_auth.check_all_providers", return_value=[]):
            status, body = self._get("/api/diagnostics")
        self.assertEqual(status, 200)
        self.assertIn("text", body)


class TestAppInfoRoute(_SettingsDaemonCase):
    def test_get_reports_version_and_validity(self):
        status, body = self._get("/api/app_info")
        self.assertEqual(status, 200)
        self.assertIn("version", body)
        self.assertTrue(body["config_valid"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_package_i.py -v -k "Doctor or Diagnostics or AppInfo"`
Expected: FAIL — all three routes 404

- [ ] **Step 3: Implement**

Add near `Daemon.provider_status` in `orchestrator/daemon.py`:

```python
    def doctor_report(self) -> Dict[str, Any]:
        """The same probe `--doctor` runs, for the settings page's diagnostics panel."""
        from orchestrator.config import get_preflight_config
        from orchestrator.preflight import format_preflight_report, run_preflight

        cfg = get_preflight_config(self.config)
        report = run_preflight(
            self.config,
            deep=False,
            strict=bool(cfg.get("strict", True)),
            timeout=int(cfg.get("timeout_seconds", 15)),
        )
        return {"report": report, "text": format_preflight_report(report, color=False)}

    def diagnostics_report(self) -> Dict[str, Any]:
        """A combined, already-redacted text report: preflight plus provider account status."""
        from orchestrator.preflight import format_preflight_report, run_preflight
        from orchestrator.provider_auth import check_all_providers, format_provider_report

        preflight = run_preflight(self.config, deep=False)
        providers = check_all_providers()
        text = (
            format_preflight_report(preflight, color=False)
            + "\n\n"
            + format_provider_report(providers, color=False)
        )
        return {"text": text}
```

Add near `VENDORED_FILES` (module level, before `make_daemon_handler`):

```python
def app_version() -> str:
    """The desktop shell's own version, read from its package.json. "unknown" if absent."""
    try:
        path = Path(__file__).resolve().parent.parent / "desktop" / "package.json"
        return json.loads(path.read_text(encoding="utf-8")).get("version", "unknown")
    except Exception:
        return "unknown"
```

(If `json` or `Path` are not already imported at the top of `orchestrator/daemon.py`, add `import json` and `from pathlib import Path` — check first with `grep -n "^import\|^from" orchestrator/daemon.py`.)

In `do_GET`, after the `/api/settings` block from Task 4, insert:

```python
            if route == "/api/doctor":
                try:
                    self._send(_json_bytes(daemon.doctor_report()), "application/json")
                except Exception as exc:
                    self._send(_json_bytes({"error": str(exc)}), "application/json", status=500)
                return

            if route == "/api/diagnostics":
                try:
                    self._send(_json_bytes(daemon.diagnostics_report()), "application/json")
                except Exception as exc:
                    self._send(_json_bytes({"error": str(exc)}), "application/json", status=500)
                return

            if route == "/api/app_info":
                from orchestrator.config import ConfigValidationError, validate_config

                valid, error = True, None
                try:
                    validate_config(daemon.config)
                except ConfigValidationError as exc:
                    valid, error = False, str(exc)
                self._send(
                    _json_bytes(
                        {"version": app_version(), "config_valid": valid, "config_error": error}
                    ),
                    "application/json",
                )
                return
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_package_i.py -v`
Expected: PASS (all tests so far)

- [ ] **Step 5: Commit**

```bash
git add orchestrator/daemon.py tests/test_package_i.py
git commit -m "Add GET /api/doctor, /api/diagnostics, /api/app_info to the daemon"
```

---

## Task 6: `/api/prune/plan` and `/api/prune/execute` routes

**Files:**
- Modify: `orchestrator/daemon.py` (add `Daemon.prune_plan`, `Daemon.prune_execute`; add `do_GET`/`do_POST` routes)
- Modify: `tests/test_package_i.py` (append)

**Interfaces:**
- Consumes: `orchestrator.prune.{plan_prune, execute_prune}`, `orchestrator.config.get_delivery_config`, `preferences.load_preferences()`.
- Produces: `Daemon.prune_plan(older_than_seconds: int, keep_failed: bool) -> Dict`, `Daemon.prune_execute(plan: Dict) -> Dict`; `GET /api/prune/plan[?older_than_days=N&keep_failed=0|1]`, `POST /api/prune/execute` (body `{"plan": {...}}`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_package_i.py`:

```python
class TestPrunePlanRoute(_SettingsDaemonCase):
    def test_get_uses_preference_defaults_when_no_query_given(self):
        fake_plan = {"available": True, "total": 0, "prunable": [], "kept": []}
        with patch("orchestrator.prune.plan_prune", return_value=fake_plan) as mock_plan:
            status, body = self._get("/api/prune/plan")
        self.assertEqual(status, 200)
        self.assertTrue(body["available"])
        mock_plan.assert_called_once()
        self.assertEqual(mock_plan.call_args.args[1], 30 * 86400)

    def test_get_honors_query_overrides(self):
        with patch("orchestrator.prune.plan_prune", return_value={"available": True}) as mock_plan:
            status, _ = self._get("/api/prune/plan?older_than_days=7&keep_failed=0")
        self.assertEqual(status, 200)
        self.assertEqual(mock_plan.call_args.args[1], 7 * 86400)
        self.assertFalse(mock_plan.call_args.kwargs["keep_failed"])


class TestPruneExecuteRoute(_SettingsDaemonCase):
    def test_post_passes_the_plan_through_unchanged(self):
        plan = {"available": True, "prunable": [{"branch": "run/x"}]}
        with patch(
            "orchestrator.prune.execute_prune", return_value={"deleted": [], "failed": []}
        ) as mock_exec:
            status, body = self._post("/api/prune/execute", {"plan": plan})
        self.assertEqual(status, 200)
        mock_exec.assert_called_once()
        self.assertEqual(mock_exec.call_args.args[1], plan)

    def test_post_without_a_plan_is_a_400(self):
        status, body = self._post("/api/prune/execute", {})
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_package_i.py -v -k Prune`
Expected: FAIL — both routes 404

- [ ] **Step 3: Implement**

Add near `Daemon.provider_status` in `orchestrator/daemon.py`:

```python
    def prune_plan(self, older_than_seconds: int, keep_failed: bool) -> Dict[str, Any]:
        """What `--prune-runs --dry-run` would show. Never deletes anything."""
        from orchestrator.config import get_delivery_config
        from orchestrator.prune import plan_prune

        return plan_prune(
            self.project_root,
            older_than_seconds,
            keep_failed=keep_failed,
            deliveries_directory=get_delivery_config(self.config).get("directory"),
        )

    def prune_execute(self, plan: Dict[str, Any]) -> Dict[str, Any]:
        """Delete exactly the branches `plan` marked prunable. Never computes its own plan."""
        from orchestrator.config import get_delivery_config
        from orchestrator.prune import execute_prune

        return execute_prune(
            self.project_root, plan,
            deliveries_directory=get_delivery_config(self.config).get("directory"),
        )
```

In `do_GET`, after the `/api/app_info` block from Task 5, insert:

```python
            if route == "/api/prune/plan":
                from orchestrator.preferences import load_preferences

                query = parse_qs(urlparse(self.path).query)
                prefs = load_preferences()
                try:
                    older_days = int(
                        (query.get("older_than_days") or [str(prefs["prune_default_max_age_days"])])[0]
                    )
                except ValueError:
                    older_days = prefs["prune_default_max_age_days"]
                keep_failed_param = (query.get("keep_failed") or [None])[0]
                keep_failed = (
                    prefs["prune_default_keep_failed"]
                    if keep_failed_param is None
                    else keep_failed_param in ("1", "true")
                )
                try:
                    self._send(
                        _json_bytes(daemon.prune_plan(older_days * 86400, keep_failed)),
                        "application/json",
                    )
                except Exception as exc:
                    self._send(_json_bytes({"error": str(exc)}), "application/json", status=500)
                return
```

In `do_POST`, after the `/api/settings` block from Task 4, insert:

```python
            if route == "/api/prune/execute":
                payload = self._read_json()
                if payload is None:
                    return
                plan = payload.get("plan")
                if not isinstance(plan, dict):
                    self._send(
                        _json_bytes({"ok": False, "error": "a prune plan was expected"}),
                        "application/json",
                        status=400,
                    )
                    return
                try:
                    result = daemon.prune_execute(plan)
                    self._send(_json_bytes(result), "application/json")
                except Exception as exc:
                    self._send(_json_bytes({"ok": False, "error": str(exc)}), "application/json", 500)
                return
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_package_i.py -v`
Expected: PASS (all tests so far)

- [ ] **Step 5: Commit**

```bash
git add orchestrator/daemon.py tests/test_package_i.py
git commit -m "Add GET /api/prune/plan and POST /api/prune/execute to the daemon"
```

---

## Task 7: Shared theme stylesheet, served by the daemon, adopted by the cockpit

**Files:**
- Create: `orchestrator/web/shared/theme.css`
- Modify: `orchestrator/daemon.py` (add `SHARED_FILES`, `_shared()`, `/shared/` route)
- Modify: `orchestrator/web/cockpit.html` (remove inline theme CSS block, add `<link>`, rework theme boot JS into `window.__applyTheme`, backed by `/api/preferences`)
- Modify: `tests/test_package_i.py` (append)

**Interfaces:**
- Produces: `GET /shared/theme.css`; `window.__applyTheme(themeId, opts)` global function defined by `cockpit.html` (consumed later by `desktop/main.js` in Task 8, and by `settings.html` in Task 9).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_package_i.py`:

```python
class TestSharedThemeRoute(_SettingsDaemonCase):
    def test_get_serves_theme_css(self):
        status, _ = self._get_raw("/shared/theme.css")
        self.assertEqual(status, 200)

    def test_unknown_shared_file_is_404(self):
        status, _ = self._get_raw("/shared/does-not-exist.css")
        self.assertEqual(status, 404)
```

Also add a small raw-bytes GET helper to `_SettingsDaemonCase` (theme.css is not JSON, so `_get` as written would fail to `json.loads` it):

```python
    def _get_raw(self, path):
        request = urllib.request.Request(self.base + path)
        request.add_header(TOKEN_HEADER, "t")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()
```

(Add this method to `_SettingsDaemonCase`, right after the existing `_get` method.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_package_i.py -v -k SharedTheme`
Expected: FAIL — `/shared/theme.css` is not routed (falls through to 404 via the generic `/api/` guard, or a plain 404 from `base.do_GET`)

- [ ] **Step 3a: Extract the CSS**

Read `orchestrator/web/cockpit.html` lines 9–162 (the five `body.theme-*` blocks, including the base `:root, body.theme-srcery` block). Move that exact text — unchanged — into a new file:

```css
/* orchestrator/web/shared/theme.css
   The five color schemes, shared by every page that themes itself (cockpit.html, settings.html).
   Moved out of cockpit.html so a second page does not have to duplicate five palettes to stay
   in sync with it. */
:root, body.theme-srcery {
    --bg: #1C1B19;
    --panel: #2D2C29;
    --green: #519F50;
    --amber: #FBB829;
    --red: #EF2F27;
    --cyan: #0AAEB3;
    --dim: #918175;
    --text: #FCE8C3;
    --text-dim: #BAA67F;

    --scrollbar-track: var(--panel);
    --scrollbar-thumb: #3a3936;
    --scrollbar-thumb-hover: #4a4946;
    --scrollbar-thumb-active: var(--dim);

    --hl-keyword: #E02C6D;
    --hl-string: #519F50;
    --hl-comment: #918175;
    --hl-number: #68A8E4;
    --hl-type: #0AAEB3;
    --hl-builtin: #2C78BF;
    --hl-function: #FBB829;

    /* ANSI Colors (Srcery) */
    --ansi-30: #1C1B19; --ansi-31: #EF2F27; --ansi-32: #519F50; --ansi-33: #FBB829;
    --ansi-34: #2C78BF; --ansi-35: #E02C6D; --ansi-36: #0AAEB3; --ansi-37: #BAA67F;
    --ansi-90: #918175; --ansi-91: #F75341; --ansi-92: #98BC37; --ansi-93: #FED06E;
    --ansi-94: #68A8E4; --ansi-95: #FF5C8F; --ansi-96: #2BE4D0; --ansi-97: #FCE8C3;
}

body.theme-moonfly {
    --bg: #080808;
    --panel: #141414;
    --green: #8cc85f;
    --amber: #e3c78a;
    --red: #ff5454;
    --cyan: #79dac8;
    --dim: #323437;
    --text: #d0d0d0;
    --text-dim: #888888;

    --scrollbar-track: var(--panel);
    --scrollbar-thumb: #323437;
    --scrollbar-thumb-hover: #505050;
    --scrollbar-thumb-active: var(--dim);

    --hl-keyword: #cf87e8;
    --hl-string: #8cc85f;
    --hl-comment: #505050;
    --hl-number: #ff5454;
    --hl-type: #79dac8;
    --hl-builtin: #80a0ff;
    --hl-function: #e3c78a;

    /* ANSI Colors (Moonfly) */
    --ansi-30: #323437; --ansi-31: #ff5454; --ansi-32: #8cc85f; --ansi-33: #e3c78a;
    --ansi-34: #80a0ff; --ansi-35: #cf87e8; --ansi-36: #79dac8; --ansi-37: #d0d0d0;
    --ansi-90: #505050; --ansi-91: #ff5454; --ansi-92: #8cc85f; --ansi-93: #e3c78a;
    --ansi-94: #80a0ff; --ansi-95: #cf87e8; --ansi-96: #79dac8; --ansi-97: #ffffff;
}

body.theme-jellybeans {
    --bg: #151515;
    --panel: #1c1c1c;
    --green: #99ad6a;
    --amber: #d8ad4c;
    --red: #cf6a4c;
    --cyan: #71b9f8;
    --dim: #404040;
    --text: #e8e8d3;
    --text-dim: #888888;

    --scrollbar-track: var(--panel);
    --scrollbar-thumb: #3b3b3b;
    --scrollbar-thumb-hover: #555555;
    --scrollbar-thumb-active: var(--dim);

    --hl-keyword: #8fbfdc;
    --hl-string: #99ad6a;
    --hl-comment: #888888;
    --hl-number: #cf6a4c;
    --hl-type: #71b9f8;
    --hl-builtin: #cf6a4c;
    --hl-function: #d8ad4c;

    /* ANSI Colors (Jellybeans) */
    --ansi-30: #3b3b3b; --ansi-31: #cf6a4c; --ansi-32: #99ad6a; --ansi-33: #d8ad4c;
    --ansi-34: #597bc5; --ansi-35: #a037b0; --ansi-36: #71b9f8; --ansi-37: #adadad;
    --ansi-90: #404040; --ansi-91: #cf6a4c; --ansi-92: #99ad6a; --ansi-93: #d8ad4c;
    --ansi-94: #597bc5; --ansi-95: #a037b0; --ansi-96: #71b9f8; --ansi-97: #e8e8d3;
}

body.theme-tender {
    --bg: #282828;
    --panel: #202020;
    --green: #c9d05c;
    --amber: #ffc24b;
    --red: #f43753;
    --cyan: #73cef4;
    --dim: #4c4c4c;
    --text: #eeeeee;
    --text-dim: #b3b3b3;

    --scrollbar-track: var(--panel);
    --scrollbar-thumb: #3a3a3a;
    --scrollbar-thumb-hover: #4d4d4d;
    --scrollbar-thumb-active: var(--dim);

    --hl-keyword: #d3b987;
    --hl-string: #c9d05c;
    --hl-comment: #4c4c4c;
    --hl-number: #ffc24b;
    --hl-type: #b3deef;
    --hl-builtin: #73cef4;
    --hl-function: #d3b987;

    /* ANSI Colors (Tender) */
    --ansi-30: #282828; --ansi-31: #f43753; --ansi-32: #c9d05c; --ansi-33: #ffc24b;
    --ansi-34: #b3deef; --ansi-35: #d3b987; --ansi-36: #73cef4; --ansi-37: #eeeeee;
    --ansi-90: #4c4c4c; --ansi-91: #f43753; --ansi-92: #c9d05c; --ansi-93: #ffc24b;
    --ansi-94: #b3deef; --ansi-95: #d3b987; --ansi-96: #73cef4; --ansi-97: #ffffff;
}

body.theme-miasma {
    --bg: #222222;
    --panel: #1a1a1a;
    --green: #5f875f;
    --amber: #b36d43;
    --red: #bb7744;
    --cyan: #c9a554;
    --dim: #666666;
    --text: #c2c2b0;
    --text-dim: #78824b;

    --scrollbar-track: var(--panel);
    --scrollbar-thumb: #333333;
    --scrollbar-thumb-hover: #4a4a4a;
    --scrollbar-thumb-active: var(--text-dim);

    --hl-keyword: #78824b;
    --hl-string: #c9a554;
    --hl-comment: #666666;
    --hl-number: #d7c483;
    --hl-type: #5f875f;
    --hl-builtin: #b36d43;
    --hl-function: #c9a554;

    /* ANSI Colors (Miasma) */
    --ansi-30: #222222; --ansi-31: #685742; --ansi-32: #5f875f; --ansi-33: #b36d43;
    --ansi-34: #78824b; --ansi-35: #bb7744; --ansi-36: #c9a554; --ansi-37: #d7c483;
    --ansi-90: #666666; --ansi-91: #685742; --ansi-92: #5f875f; --ansi-93: #b36d43;
    --ansi-94: #78824b; --ansi-95: #bb7744; --ansi-96: #c9a554; --ansi-97: #d7c483;
}
```

In `orchestrator/web/cockpit.html`, delete lines 9–162 (the block above, now moved) from the inline `<style>`, so the `<style>` block's first rule becomes the existing `:root { --font: ...; }` block that was at line 164.

- [ ] **Step 3b: Serve it**

In `orchestrator/daemon.py`, near `VENDORED_FILES`, add:

```python
#: Static assets shared by more than one page (currently just the theme palettes), served the
#: same allow-listed way `VENDORED_FILES` is - an explicit list, not a directory walk.
SHARED_FILES = {
    "theme.css": "text/css; charset=utf-8",
}
```

In `DaemonHandler`, add a method mirroring `_vendor` (place it right after `_vendor`):

```python
        def _shared(self, name: str) -> None:
            """Serve one shared static asset, from a fixed list - same shape as `_vendor`."""
            if name not in SHARED_FILES:
                self._send(b"Not found", "text/plain; charset=utf-8", status=404)
                return
            try:
                body = (WEB_DIR / "shared" / name).read_bytes()
            except Exception:
                self._send(b"Not found", "text/plain; charset=utf-8", status=404)
                return
            self._send(body, SHARED_FILES[name])
```

In `do_GET`, right after the existing `/vendor/` block:

```python
            if route.startswith("/vendor/"):
                self._vendor(route[len("/vendor/"):])
                return
```

insert:

```python
            if route.startswith("/shared/"):
                self._shared(route[len("/shared/"):])
                return
```

- [ ] **Step 3c: Point cockpit.html at it, and rework the theme JS**

In `<head>`, add before the existing `<style>` tag:

```html
    <link rel="stylesheet" href="/shared/theme.css">
```

Near the existing `apiSaveTeam` function (~line 1201 in the original file), add:

```javascript
    async function apiSavePreferences(patch) {
        const res = await fetch("/api/preferences", {
            method: "POST",
            headers: { 'X-Orchestrator-Token': TOKEN, 'Content-Type': 'application/json' },
            body: JSON.stringify(patch)
        });
        return res.json();
    }
```

Replace the boot()-time theme block:

```javascript
        // Theme initialization
        const savedTheme = localStorage.getItem('cockpit-theme') || 'theme-srcery';
        document.body.classList.add(savedTheme);
        if (DOM.themeSelect) {
            DOM.themeSelect.value = savedTheme;
            DOM.themeSelect.addEventListener('change', (e) => {
                document.body.classList.remove('theme-srcery', 'theme-miasma', 'theme-tender', 'theme-jellybeans', 'theme-moonfly');
                document.body.classList.add(e.target.value);
                localStorage.setItem('cockpit-theme', e.target.value);
            });
        }
```

with:

```javascript
        // Theme initialization - read from the daemon's per-machine preferences (Package I), so
        // a choice made from the desktop Theme menu or the Settings page is honored here too,
        // and survives a relaunch (a random port each launch broke localStorage for this).
        let initialTheme = 'theme-srcery';
        try {
            const prefs = await apiGet('/api/preferences');
            if (prefs && prefs.theme) initialTheme = prefs.theme;
        } catch (err) { /* keep the default */ }
        window.__applyTheme(initialTheme, { persist: false });
        if (DOM.themeSelect) {
            DOM.themeSelect.addEventListener('change', (e) => window.__applyTheme(e.target.value));
        }
```

Add `window.__applyTheme`, defined once near the top of the main `<script>` block (after `DOM` is assigned, since it reads `DOM.themeSelect`):

```javascript
    window.__applyTheme = function(themeId, opts) {
        const persist = !opts || opts.persist !== false;
        document.body.classList.remove('theme-srcery', 'theme-miasma', 'theme-tender', 'theme-jellybeans', 'theme-moonfly');
        document.body.classList.add(themeId);
        if (DOM.themeSelect) DOM.themeSelect.value = themeId;
        if (persist) apiSavePreferences({ theme: themeId }).catch(() => {});
    };
```

(`window.__applyTheme` is what Electron's menu calls via `executeJavaScript` in Task 8, and what `settings.html`'s own theme picker calls in Task 9 — one apply/persist path, two entry points, per the design's "do not create duplicate theme logic" constraint.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_package_i.py -v`
Expected: PASS (all tests so far)

Run: `python -m pytest tests/test_provider_settings_page.py -v`
Expected: PASS — unaffected by this task (cockpit.html's `href="/settings"` link and its de-emphasized class are untouched)

- [ ] **Step 5: Commit**

```bash
git add orchestrator/web/shared/theme.css orchestrator/web/cockpit.html orchestrator/daemon.py tests/test_package_i.py
git commit -m "Extract theme CSS into a shared, daemon-served stylesheet; back cockpit theme with /api/preferences"
```

---

## Task 8: Desktop menu — Settings rename, Theme submenu, default workspace directory

**Files:**
- Modify: `desktop/main.js`
- Modify: `tests/test_provider_settings_page.py` (append `TestThemeMenu`, update the existing `TestDesktopSettingsMenu` assertions for the renamed item)

**Interfaces:**
- Consumes: `window.__applyTheme` (Task 7, called via `executeJavaScript`).
- Produces: menu items "Settings > Open Settings…" (`CmdOrCtrl+,`) and "Settings > Theme > {Srcery, Moonfly, Jellybeans, Tender, Miasma}" (radio); reads/writes `~/.orchestrator/preferences.json` directly via Node `fs`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_provider_settings_page.py`, update `TestDesktopSettingsMenu` (the old label no longer exists) and add a new class:

```python
class TestDesktopSettingsMenu(unittest.TestCase):
    def setUp(self):
        desktop_main = WEB_DIR.parent.parent / "desktop" / "main.js"
        self.source = desktop_main.read_text(encoding="utf-8")

    def test_settings_menu_item_exists(self):
        self.assertIn('label: "Settings"', self.source)

    def test_settings_menu_opens_the_settings_route(self):
        self.assertIn('urlFor(window, "/settings")', self.source)

    def test_settings_menu_follows_window_menu(self):
        window_menu_pos = self.source.index('{ role: "windowMenu" }')
        settings_pos = self.source.index('label: "Settings"')
        self.assertLess(window_menu_pos, settings_pos)

    def test_settings_submenu_item_is_generic_now(self):
        """Package I: the page behind /settings is no longer provider-accounts-only."""
        self.assertIn('label: "Open Settings…"', self.source)
        self.assertNotIn('label: "Provider Accounts…"', self.source)

    def test_settings_has_an_accelerator(self):
        self.assertIn('accelerator: "CmdOrCtrl+,"', self.source)


class TestThemeMenu(unittest.TestCase):
    def setUp(self):
        desktop_main = WEB_DIR.parent.parent / "desktop" / "main.js"
        self.source = desktop_main.read_text(encoding="utf-8")

    def test_theme_submenu_exists_under_settings(self):
        settings_pos = self.source.index('label: "Settings"')
        theme_pos = self.source.index('label: "Theme"')
        self.assertLess(settings_pos, theme_pos)

    def test_theme_items_are_radios(self):
        self.assertIn('type: "radio"', self.source)

    def test_theme_click_applies_and_persists(self):
        self.assertIn("applyTheme", self.source)
        self.assertIn("__applyTheme", self.source)

    def test_reads_and_writes_preferences_json(self):
        self.assertIn('".orchestrator"', self.source)
        self.assertIn('"preferences.json"', self.source)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_provider_settings_page.py -v`
Expected: FAIL — new assertions not yet true of `desktop/main.js`

- [ ] **Step 3: Implement**

Replace the full contents of `desktop/main.js` with:

```javascript
// The desktop shell (Roadmap Phase 11).
//
// Deliberately last, and deliberately thin. Everything this window shows was already proven
// in a browser tab against a running daemon (Phases 8-10); what is added here is the chrome a
// browser cannot give: open a project folder, a window per project, native menus, and a
// daemon whose lifetime is the window's rather than a terminal's.
//
// What this file is allowed to do
// -------------------------------
// * spawn `python -m orchestrator --daemon <port> --project-root <folder> --no-browser`,
// * wait for that daemon's own handshake file to appear, and
// * load the cockpit it serves.
//
// What it must never do: reach into the project, call the engine, or hold any state of its
// own about a run. Every fact still comes from the daemon, which still gets it from the
// stores. If this shell were deleted, `--daemon` in a terminal would lose nothing but a
// window frame - and that is the test of whether the split in Roadmap §10.7 was kept.
//
// Package I adds one more small trusted read/write: `~/.orchestrator/preferences.json`, the
// same per-machine JSON file the daemon's `/api/preferences` route reads and writes. Electron's
// main process reads it directly (plain `fs`, like the handshake file already is) rather than
// through HTTP, because the menu needs a theme's checked state and a default folder before any
// project window - and its daemon - exists.

// Electron degrades to a plain Node interpreter - `require("electron")` returns a path
// string instead of the {app, BrowserWindow, ...} API - whenever ELECTRON_RUN_AS_NODE is set
// in its environment. Some dev tooling sets that globally so it can reuse Electron's bundled
// Node as a script runner, and it is inherited by anything launched from the same shell,
// including this app. Detected here rather than assumed, and fixed by re-executing this same
// binary with the variable cleared, instead of failing later with `app` mysteriously
// undefined (exactly what happened during development, from a shell that had it set).
if (process.env.ELECTRON_RUN_AS_NODE) {
  const { spawnSync } = require("child_process");
  const cleanEnv = { ...process.env };
  delete cleanEnv.ELECTRON_RUN_AS_NODE;
  const result = spawnSync(process.execPath, process.argv.slice(1), {
    env: cleanEnv,
    stdio: "inherit",
  });
  process.exit(result.status === null ? 1 : result.status);
}

const { app, BrowserWindow, Menu, dialog, shell } = require("electron");
const { spawn } = require("child_process");
const fs = require("fs");
const net = require("net");
const os = require("os");
const path = require("path");

// The repository this shell ships inside. The daemon is a module in it, not a bundled binary:
// the engine stays Python, exactly as it was.
const REPO_ROOT = path.resolve(__dirname, "..");

/** One open project: its window, its daemon process, and the folder it was opened on. */
const projects = new Map(); // window id -> { window, child, projectRoot, port }

const PREFERENCES_PATH = path.join(os.homedir(), ".orchestrator", "preferences.json");
const THEME_IDS = ["theme-srcery", "theme-moonfly", "theme-jellybeans", "theme-tender", "theme-miasma"];
const THEME_LABELS = {
  "theme-srcery": "Srcery",
  "theme-moonfly": "Moonfly",
  "theme-jellybeans": "Jellybeans",
  "theme-tender": "Tender",
  "theme-miasma": "Miasma",
};

/** Every saved preference, or `{}` if the file does not exist yet or cannot be read. */
function readPreferences() {
  try {
    return JSON.parse(fs.readFileSync(PREFERENCES_PATH, "utf-8"));
  } catch (err) {
    return {};
  }
}

/** Merge `patch` into the saved preferences and write it back, atomically. Never throws. */
function writePreferences(patch) {
  const next = { ...readPreferences(), ...patch };
  try {
    fs.mkdirSync(path.dirname(PREFERENCES_PATH), { recursive: true });
    const tmp = PREFERENCES_PATH + ".tmp";
    fs.writeFileSync(tmp, JSON.stringify(next, null, 2), "utf-8");
    fs.renameSync(tmp, PREFERENCES_PATH);
  } catch (err) {
    // The daemon's own /api/preferences route is the source of truth for any open page; a
    // failed write here only means the menu's *next* rebuild still shows the old theme.
  }
  return next;
}

function pythonCommand() {
  // The project's own virtualenv first, because that is where its dependencies are. An
  // explicit override wins, so a machine with an unusual layout is not stuck.
  if (process.env.ORCHESTRATOR_PYTHON) return process.env.ORCHESTRATOR_PYTHON;
  const candidates =
    process.platform === "win32"
      ? [path.join(REPO_ROOT, ".venv", "Scripts", "python.exe")]
      : [path.join(REPO_ROOT, ".venv", "bin", "python")];
  for (const candidate of candidates) {
    if (fs.existsSync(candidate)) return candidate;
  }
  return process.platform === "win32" ? "python" : "python3";
}

/** Ask the OS for a port nothing is using, so two open projects never collide. */
function freePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.unref();
    server.on("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const { port } = server.address();
      server.close(() => resolve(port));
    });
  });
}

/**
 * Reserve a port, or explain why there is no daemon to start.
 *
 * `openProject` must not be able to reject before it has a window to report into. A `freePort`
 * failure - the machine has no loopback port left to give - is the same class of problem as a
 * daemon that never appears, so it surfaces the same dialog and lets the caller carry on,
 * instead of an unhandled promise rejection in the Electron main process.
 */
async function reservePort(projectRoot) {
  try {
    return await freePort();
  } catch (err) {
    await dialog.showMessageBox(null, {
      type: "error",
      title: "The daemon did not start",
      message: `Could not reserve a port for ${projectRoot}.`,
      detail: String(err && err.message ? err.message : err),
    });
    return null;
  }
}

const handshakeFile = (projectRoot) =>
  path.join(projectRoot, ".orchestrator", "daemon.json");

/**
 * Wait until the daemon has written its handshake for the port we asked for.
 *
 * The handshake is the daemon's own signal that it is serving - not a guess from a log line,
 * and not a fixed sleep. A stale file from a previous run is ignored because the port has to
 * match the one this launch was given.
 */
function awaitDaemon(projectRoot, port, timeoutMs = 30000) {
  const started = Date.now();
  const file = handshakeFile(projectRoot);
  return new Promise((resolve, reject) => {
    const tick = () => {
      try {
        const payload = JSON.parse(fs.readFileSync(file, "utf-8"));
        if (Number(payload.port) === Number(port)) return resolve(payload);
      } catch (err) {
        /* not written yet */
      }
      if (Date.now() - started > timeoutMs) {
        return reject(new Error("the daemon did not start within 30 seconds"));
      }
      setTimeout(tick, 200);
    };
    tick();
  });
}

async function openProject(projectRoot) {
  // A daemon was briefly seen spawned twice for one launch, of unconfirmed origin. This is
  // not decorative: if it recurs, the timestamp and call count pin down whether it is one
  // call whose child process the OS lists twice, two genuinely separate calls (from where?),
  // or a double invocation of `whenReady`/`activate` this file did not anticipate.
  openProject._calls = (openProject._calls || 0) + 1;
  console.log(
    `[main] openProject #${openProject._calls} for ${projectRoot} at ${new Date().toISOString()}`
  );

  const port = await reservePort(projectRoot);
  if (port === null) return null;

  // A stale handshake would otherwise be mistaken for this launch's.
  try {
    fs.unlinkSync(handshakeFile(projectRoot));
  } catch (err) {
    /* there was none */
  }

  const child = spawn(
    pythonCommand(),
    [
      "-m",
      "orchestrator",
      "--daemon",
      String(port),
      "--project-root",
      projectRoot,
      "--no-browser",
    ],
    { cwd: REPO_ROOT, stdio: ["ignore", "pipe", "pipe"] }
  );

  let startupLog = "";
  const remember = (chunk) => {
    const text = chunk.toString();
    startupLog = (startupLog + text).slice(-4000);
    // Also surfaced live in this process's own console (visible to whoever ran `npm start`
    // from a terminal) - the daemon's own stdout/stderr previously only reached a person via
    // the error dialog on an unexpected exit, which meant a crash between here and there
    // left no visible trace at all.
    process.stdout.write(`[daemon:${port}] ${text}`);
  };
  child.stdout.on("data", remember);
  child.stderr.on("data", remember);

  const window = new BrowserWindow({
    width: 1360,
    height: 880,
    minWidth: 900,
    minHeight: 600,
    backgroundColor: "#10131a",
    title: `Synagon — ${path.basename(projectRoot)}`,
    show: false,
    webPreferences: {
      // The cockpit is a plain page served over loopback; it needs no Node, and giving it
      // none is what keeps the shell from becoming an extra thing that can act.
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });

  projects.set(window.id, { window, child, projectRoot, port });

  // A link to a pull request belongs in the person's own browser, not in this window.
  window.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: "deny" };
  });

  try {
    await awaitDaemon(projectRoot, port);
    await window.loadURL(`http://127.0.0.1:${port}/`);
    window.show();
  } catch (err) {
    window.show();
    dialog.showMessageBox(window, {
      type: "error",
      title: "The daemon did not start",
      message: `Could not start the orchestrator daemon for ${projectRoot}.`,
      detail: `${err.message}\n\n${startupLog}`,
    });
  }

  child.on("exit", (code, signal) => {
    console.log(
      `[main] daemon on port ${port} exited: code=${code} signal=${signal} at ${new Date().toISOString()}`
    );
    if (!window.isDestroyed() && code !== 0) {
      dialog.showMessageBox(window, {
        type: "warning",
        title: "The daemon stopped",
        message: "The orchestrator daemon for this project exited.",
        detail: `Exit code ${code}.\n\n${startupLog}`,
      });
    }
  });

  window.on("closed", () => {
    const entry = projects.get(window.id);
    projects.delete(window.id);
    if (entry && entry.child && !entry.child.killed) entry.child.kill();
  });

  return window;
}

async function chooseProject(parent) {
  const prefs = readPreferences();
  const options = {
    title: "Open a project",
    properties: ["openDirectory"],
    buttonLabel: "Open",
  };
  if (prefs.default_workspace_dir && fs.existsSync(prefs.default_workspace_dir)) {
    options.defaultPath = prefs.default_workspace_dir;
  }
  const result = await dialog.showOpenDialog(parent || null, options);
  if (result.canceled || !result.filePaths.length) return null;
  return openProject(result.filePaths[0]);
}

/** Apply a theme to one open window's page immediately, and remember the choice. */
function applyTheme(themeId, window) {
  writePreferences({ theme: themeId });
  if (window && !window.isDestroyed()) {
    window.webContents
      .executeJavaScript(`window.__applyTheme && window.__applyTheme(${JSON.stringify(themeId)});`)
      .catch(() => {});
  }
  buildMenu(); // rebuild so the radio state - here, and on every other open window - reflects it
}

function buildMenu() {
  const prefs = readPreferences();
  const currentTheme = THEME_IDS.includes(prefs.theme) ? prefs.theme : "theme-srcery";

  const template = [
    {
      label: "File",
      submenu: [
        {
          label: "Open Project Folder…",
          accelerator: "CmdOrCtrl+O",
          click: (_item, window) => chooseProject(window),
        },
        {
          label: "New Window on This Project",
          accelerator: "CmdOrCtrl+Shift+N",
          click: (_item, window) => {
            const entry = window ? projects.get(window.id) : null;
            if (entry) openProject(entry.projectRoot);
            else chooseProject(window);
          },
        },
        { type: "separator" },
        { role: "close" },
        { role: "quit" },
      ],
    },
    {
      label: "View",
      submenu: [
        { role: "reload" },
        { role: "forceReload" },
        { role: "toggleDevTools" },
        { type: "separator" },
        { role: "resetZoom" },
        { role: "zoomIn" },
        { role: "zoomOut" },
        { type: "separator" },
        { role: "togglefullscreen" },
      ],
    },
    {
      label: "Go",
      submenu: [
        {
          label: "Cockpit",
          click: (_item, window) => window && window.loadURL(urlFor(window, "/")),
        },
        {
          label: "Office",
          click: (_item, window) => window && window.loadURL(urlFor(window, "/office")),
        },
        {
          label: "Design the team",
          click: (_item, window) => window && window.loadURL(urlFor(window, "/design")),
        },
      ],
    },
    { role: "windowMenu" },
    {
      label: "Settings",
      submenu: [
        {
          label: "Open Settings…",
          accelerator: "CmdOrCtrl+,",
          click: (_item, window) => window && window.loadURL(urlFor(window, "/settings")),
        },
        { type: "separator" },
        {
          label: "Theme",
          submenu: THEME_IDS.map((id) => ({
            label: THEME_LABELS[id],
            type: "radio",
            checked: id === currentTheme,
            click: (_item, window) => applyTheme(id, window),
          })),
        },
      ],
    },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

function urlFor(window, route) {
  const entry = projects.get(window.id);
  return entry ? `http://127.0.0.1:${entry.port}${route}` : "about:blank";
}

app.whenReady().then(async () => {
  console.log(`[main] whenReady fired at ${new Date().toISOString()}`);
  buildMenu();
  // Opened on a folder given on the command line, on this repository, or on whatever the
  // person picks - in that order, so `npm start` in a checkout just works.
  const argument = process.argv.slice(2).find((a) => !a.startsWith("-"));
  const initial = argument ? path.resolve(argument) : REPO_ROOT;
  if (fs.existsSync(path.join(initial, "orchestrator.yaml"))) await openProject(initial);
  else await chooseProject(null);
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});

app.on("before-quit", () => {
  for (const entry of projects.values()) {
    if (entry.child && !entry.child.killed) entry.child.kill();
  }
});

app.on("activate", () => {
  console.log(`[main] activate fired at ${new Date().toISOString()}, windows=${BrowserWindow.getAllWindows().length}`);
  if (BrowserWindow.getAllWindows().length === 0) chooseProject(null);
});
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_provider_settings_page.py -v`
Expected: PASS (all tests, including the new `TestThemeMenu` class)

- [ ] **Step 5: Commit**

```bash
git add desktop/main.js tests/test_provider_settings_page.py
git commit -m "Rename the desktop Settings item and add a Theme submenu, backed by preferences.json"
```

---

## Task 9: `settings.html` — page shell, tab navigation, Appearance section

**Files:**
- Modify: `orchestrator/web/settings.html` (full restructure — see below)
- Modify: `tests/test_provider_settings_page.py` (append)

**Interfaces:**
- Consumes: `GET /api/preferences`, `POST /api/preferences` (Tasks 1–2); `window.__applyTheme` convention from Task 7 (settings.html defines its own copy, since it is a separate page load — same function name and behavior, not a shared JS file, matching this codebase's "each page duplicates its own script" convention).
- Produces: `#tab-appearance` section; tab-switching JS reused by Tasks 10–13, which each add one more `<section class="tab-panel">` and one more `<button class="tab-btn">`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_provider_settings_page.py`:

```python
class TestSettingsPageTabs(unittest.TestCase):
    def setUp(self):
        self.page = (WEB_DIR / "settings.html").read_text(encoding="utf-8")

    def test_has_six_tabs(self):
        for tab in ("appearance", "workspace", "agents", "execution", "terminal", "safety"):
            self.assertIn(f'data-tab="{tab}"', self.page)

    def test_tabs_use_aria_tablist_roles(self):
        self.assertIn('role="tablist"', self.page)
        self.assertIn('role="tab"', self.page)
        self.assertIn('role="tabpanel"', self.page)

    def test_links_shared_theme_css(self):
        self.assertIn('href="/shared/theme.css"', self.page)

    def test_still_carries_the_token_placeholder(self):
        self.assertIn('name="orchestrator-token"', self.page)
        self.assertIn("__ORCHESTRATOR_TOKEN__", self.page)


class TestAppearanceSection(unittest.TestCase):
    def setUp(self):
        self.page = (WEB_DIR / "settings.html").read_text(encoding="utf-8")

    def test_has_theme_density_font_and_motion_controls(self):
        self.assertIn('id="tab-appearance"', self.page)
        self.assertIn('id="appearance-theme"', self.page)
        self.assertIn('id="appearance-density"', self.page)
        self.assertIn('id="appearance-font-size"', self.page)
        self.assertIn('id="appearance-reduced-motion"', self.page)

    def test_saves_through_preferences_api(self):
        self.assertIn("/api/preferences", self.page)

    def test_defines_apply_theme_hook(self):
        self.assertIn("window.__applyTheme", self.page)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_provider_settings_page.py -v -k "Tabs or Appearance"`
Expected: FAIL — none of this markup exists yet

- [ ] **Step 3: Rewrite `settings.html`**

Replace the full contents of `orchestrator/web/settings.html` with:

```html
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta name="orchestrator-token" content="__ORCHESTRATOR_TOKEN__">
    <link rel="stylesheet" href="/shared/theme.css">
    <title>SYNAGON — Settings</title>
    <style>
:root {
    --font: 'Cascadia Code', 'Consolas', monospace;
}
* { box-sizing: border-box; }
body {
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font-family: var(--font);
    padding: 24px;
}
body.density-compact .settings-group { padding: 10px 14px; margin-bottom: 10px; }
body.density-compact .field-row { margin: 6px 0; }
body.reduced-motion * { transition: none !important; animation: none !important; }

.page { max-width: 900px; margin: 0 auto; }
h1 { font-size: 18px; letter-spacing: 2px; color: var(--cyan); margin: 0 0 4px 0; }
p.subtitle { color: var(--text-dim); margin: 0 0 20px 0; font-size: 13px; line-height: 1.5; }
a.back-link { color: var(--cyan); text-decoration: none; font-size: 13px; }
a.back-link:hover, a.back-link:focus-visible { text-decoration: underline; }

.tabs {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
    border-bottom: 1px solid var(--dim);
    margin: 16px 0 24px 0;
}
.tab-btn {
    background: transparent;
    border: none;
    border-bottom: 2px solid transparent;
    color: var(--text-dim);
    padding: 8px 12px;
    font-family: inherit;
    font-size: 12px;
    letter-spacing: 0.5px;
    text-transform: uppercase;
    cursor: pointer;
}
.tab-btn:hover { color: var(--text); }
.tab-btn.active { color: var(--cyan); border-bottom-color: var(--cyan); }
.tab-btn:focus-visible { outline: 2px solid var(--cyan); outline-offset: 2px; }
.tab-panel { display: none; }
.tab-panel.active { display: block; }

.settings-group {
    background: var(--panel);
    border: 1px solid #3a3936;
    border-radius: 6px;
    padding: 16px 20px;
    margin-bottom: 16px;
}
.settings-group h2 {
    margin: 0 0 14px 0;
    font-size: 13px;
    text-transform: uppercase;
    letter-spacing: 1px;
    color: var(--text-dim);
}
.field-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 16px;
    margin: 10px 0;
    font-size: 13px;
}
.field-row .field-label { flex: 1; }
.field-row .field-label .hint {
    display: block;
    color: var(--text-dim);
    font-size: 11px;
    margin-top: 2px;
}
.field-row select, .field-row input[type="text"], .field-row input[type="number"] {
    background: var(--bg);
    border: 1px solid var(--dim);
    color: var(--text);
    padding: 5px 8px;
    border-radius: 4px;
    font-family: inherit;
    font-size: 12px;
    min-width: 160px;
}
.field-row input[type="checkbox"] { width: 16px; height: 16px; }
button {
    background: var(--panel);
    border: 1px solid var(--dim);
    color: var(--text);
    padding: 6px 14px;
    border-radius: 4px;
    cursor: pointer;
    font-family: inherit;
    font-size: 12px;
}
button:hover { border-color: var(--cyan); color: var(--cyan); }
button:disabled { opacity: 0.5; cursor: default; }
button.primary { border-color: var(--green); color: var(--green); }
button.primary:hover { background: var(--green); color: var(--bg); }
button.danger { border-color: var(--red); color: var(--red); }
button.danger:hover { background: var(--red); color: var(--bg); }
button:focus-visible, a:focus-visible, input:focus-visible, select:focus-visible {
    outline: 2px solid var(--cyan);
    outline-offset: 2px;
}
.save-status { font-size: 12px; color: var(--text-dim); margin-left: 8px; }
.save-status.ok { color: var(--green); }
.save-status.err { color: var(--red); }
.unavailable-note {
    font-size: 12px;
    color: var(--text-dim);
    font-style: italic;
    margin: 10px 0;
}
pre.report {
    background: var(--bg);
    border: 1px solid var(--dim);
    border-radius: 4px;
    padding: 12px;
    font-size: 12px;
    line-height: 1.5;
    white-space: pre-wrap;
    word-break: break-word;
    max-height: 400px;
    overflow-y: auto;
}
#error-banner {
    display: none;
    background: var(--red);
    color: var(--bg);
    padding: 8px 12px;
    border-radius: 4px;
    margin-bottom: 16px;
    font-size: 13px;
}

/* Carried over from the provider-accounts page this tab used to be the whole of. */
#provider-cards {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 18px;
    margin-top: 20px;
}
@media (max-width: 640px) {
    #provider-cards { grid-template-columns: 1fr; }
}
.provider-card {
    background: var(--panel);
    border: 1px solid #3a3936;
    border-left: 3px solid var(--dim);
    border-radius: 6px;
    padding: 18px 20px;
    display: flex;
    flex-direction: column;
    min-height: 220px;
}
.provider-card.accent-authenticated { border-left-color: var(--green); }
.provider-card.accent-auth_unverifiable,
.provider-card.accent-not_authenticated,
.provider-card.accent-auth_expired { border-left-color: var(--amber); }
.provider-card.accent-not_installed,
.provider-card.accent-provider_unavailable,
.provider-card.accent-cli_error,
.provider-card.accent-timed_out { border-left-color: var(--red); }
.provider-card h2 {
    margin: 0 0 14px 0;
    font-size: 16px;
    font-weight: 700;
    letter-spacing: 0.5px;
    color: var(--text);
    text-transform: none;
}
.provider-row {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    font-size: 13px;
    margin: 7px 0;
    gap: 12px;
}
.provider-row .label { color: var(--text-dim); }
.provider-row .value { font-weight: 600; }
.provider-row .value.yes { color: var(--green); }
.provider-row .value.no { color: var(--text-dim); font-weight: 400; }
.state-authenticated { color: var(--green); }
.state-auth_unverifiable, .state-not_authenticated, .state-auth_expired { color: var(--amber); }
.state-not_installed, .state-cli_error, .state-timed_out, .state-provider_unavailable { color: var(--red); }
.subscription-block {
    margin-top: 10px;
    padding-top: 10px;
    border-top: 1px dashed var(--dim);
}
.subscription-block .caption {
    display: block;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    color: var(--text-dim);
    margin-bottom: 4px;
}
.subscription-block .text {
    font-size: 12px;
    color: var(--text-dim);
    line-height: 1.5;
    font-style: italic;
}
.detail {
    font-size: 12px;
    color: var(--text-dim);
    margin-top: 12px;
    line-height: 1.5;
    word-break: break-word;
}
.card-actions {
    margin-top: auto;
    padding-top: 14px;
    display: flex;
    gap: 8px;
}
    </style>
</head>
<body>
    <div class="page">
        <a class="back-link" href="/cockpit">&larr; Cockpit</a>
        <h1>SETTINGS</h1>
        <p class="subtitle">
            Appearance, workspace, agents, execution, and diagnostics — for this machine and
            this project.
        </p>
        <div id="error-banner"></div>

        <nav class="tabs" role="tablist" aria-label="Settings sections">
            <button class="tab-btn active" data-tab="appearance" role="tab" aria-selected="true" id="tabbtn-appearance">Appearance</button>
            <button class="tab-btn" data-tab="workspace" role="tab" aria-selected="false" id="tabbtn-workspace">Workspace</button>
            <button class="tab-btn" data-tab="agents" role="tab" aria-selected="false" id="tabbtn-agents">Agents &amp; Providers</button>
            <button class="tab-btn" data-tab="execution" role="tab" aria-selected="false" id="tabbtn-execution">Execution</button>
            <button class="tab-btn" data-tab="terminal" role="tab" aria-selected="false" id="tabbtn-terminal">Terminal &amp; Desktop</button>
            <button class="tab-btn" data-tab="safety" role="tab" aria-selected="false" id="tabbtn-safety">Safety &amp; Diagnostics</button>
        </nav>

        <section class="tab-panel active" id="tab-appearance" role="tabpanel" aria-labelledby="tabbtn-appearance">
            <div class="settings-group">
                <h2>Appearance</h2>
                <p class="unavailable-note" style="margin-top:-6px;">Applies immediately, and is saved to this machine (not this project).</p>
                <div class="field-row">
                    <span class="field-label">Theme</span>
                    <select id="appearance-theme">
                        <option value="theme-srcery">Srcery</option>
                        <option value="theme-moonfly">Moonfly</option>
                        <option value="theme-jellybeans">Jellybeans</option>
                        <option value="theme-tender">Tender</option>
                        <option value="theme-miasma">Miasma</option>
                    </select>
                </div>
                <div class="field-row">
                    <span class="field-label">Density</span>
                    <select id="appearance-density">
                        <option value="comfortable">Comfortable</option>
                        <option value="compact">Compact</option>
                    </select>
                </div>
                <div class="field-row">
                    <span class="field-label">Terminal / output font size
                        <span class="hint">10–24px</span>
                    </span>
                    <input type="number" id="appearance-font-size" min="10" max="24" step="1">
                </div>
                <div class="field-row">
                    <span class="field-label">Reduced motion</span>
                    <input type="checkbox" id="appearance-reduced-motion">
                </div>
                <span class="save-status" id="appearance-status"></span>
            </div>
        </section>

        <section class="tab-panel" id="tab-workspace" role="tabpanel" aria-labelledby="tabbtn-workspace">
            <!-- Task 10 -->
        </section>

        <section class="tab-panel" id="tab-agents" role="tabpanel" aria-labelledby="tabbtn-agents">
            <div class="settings-group">
                <h2>Provider Accounts</h2>
                <p class="subtitle" style="margin-bottom:8px;">
                    Whether each local AI provider CLI is installed and signed in under your own
                    account. Synagon never reads, stores, or transmits a credential of any kind —
                    it only launches each provider's own login flow and reads status from
                    commands that provider documents as safe and non-interactive.
                </p>
                <div id="provider-cards">Loading…</div>
            </div>
            <!-- role/agent table and provider enable/disable note added in Task 11 -->
        </section>

        <section class="tab-panel" id="tab-execution" role="tabpanel" aria-labelledby="tabbtn-execution">
            <!-- Task 12 -->
        </section>

        <section class="tab-panel" id="tab-terminal" role="tabpanel" aria-labelledby="tabbtn-terminal">
            <!-- Task 12 -->
        </section>

        <section class="tab-panel" id="tab-safety" role="tabpanel" aria-labelledby="tabbtn-safety">
            <!-- Task 13 -->
        </section>
    </div>

    <script>
    const TOKEN = (() => {
        const meta = document.querySelector('meta[name="orchestrator-token"]');
        const raw = meta ? meta.getAttribute('content') : '';
        return (raw && raw.indexOf('__') !== 0) ? raw : '';
    })();

    async function apiGet(path) {
        const res = await fetch(path, { headers: { 'X-Orchestrator-Token': TOKEN } });
        if (!res.ok) throw new Error(`API error: ${res.status}`);
        return res.json();
    }

    async function apiControl(action, body) {
        const res = await fetch('/api/control/' + action, {
            method: 'POST',
            headers: { 'X-Orchestrator-Token': TOKEN, 'Content-Type': 'application/json' },
            body: JSON.stringify(body || {}),
        });
        return res.json();
    }

    async function apiSavePreferences(patch) {
        const res = await fetch('/api/preferences', {
            method: 'POST',
            headers: { 'X-Orchestrator-Token': TOKEN, 'Content-Type': 'application/json' },
            body: JSON.stringify(patch),
        });
        return res.json();
    }

    async function apiSaveSettings(patch) {
        const res = await fetch('/api/settings', {
            method: 'POST',
            headers: { 'X-Orchestrator-Token': TOKEN, 'Content-Type': 'application/json' },
            body: JSON.stringify(patch),
        });
        return res.json();
    }

    function showError(message) {
        const banner = document.getElementById('error-banner');
        banner.textContent = message;
        banner.style.display = message ? 'block' : 'none';
    }

    function escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text == null ? '' : String(text);
        return div.innerHTML;
    }

    function flashStatus(elementId, ok, message) {
        const el = document.getElementById(elementId);
        if (!el) return;
        el.textContent = message;
        el.className = 'save-status ' + (ok ? 'ok' : 'err');
        setTimeout(() => { el.textContent = ''; el.className = 'save-status'; }, 2500);
    }

    // -- Tabs: click and left/right-arrow, per the ARIA tablist pattern -----------------------
    const TAB_IDS = ['appearance', 'workspace', 'agents', 'execution', 'terminal', 'safety'];

    function activateTab(name) {
        for (const id of TAB_IDS) {
            const btn = document.getElementById('tabbtn-' + id);
            const panel = document.getElementById('tab-' + id);
            const active = id === name;
            btn.classList.toggle('active', active);
            btn.setAttribute('aria-selected', String(active));
            btn.tabIndex = active ? 0 : -1;
            panel.classList.toggle('active', active);
        }
    }

    document.querySelector('.tabs').addEventListener('click', (event) => {
        const btn = event.target.closest('.tab-btn');
        if (btn) activateTab(btn.getAttribute('data-tab'));
    });

    document.querySelector('.tabs').addEventListener('keydown', (event) => {
        if (event.key !== 'ArrowRight' && event.key !== 'ArrowLeft') return;
        const current = TAB_IDS.indexOf(document.activeElement.getAttribute('data-tab'));
        if (current === -1) return;
        const delta = event.key === 'ArrowRight' ? 1 : -1;
        const next = (current + delta + TAB_IDS.length) % TAB_IDS.length;
        const nextBtn = document.getElementById('tabbtn-' + TAB_IDS[next]);
        nextBtn.focus();
        activateTab(TAB_IDS[next]);
        event.preventDefault();
    });

    // -- Theme, shared with the menu and the cockpit (Package I) -------------------------------
    window.__applyTheme = function(themeId, opts) {
        const persist = !opts || opts.persist !== false;
        document.body.classList.remove('theme-srcery', 'theme-miasma', 'theme-tender', 'theme-jellybeans', 'theme-moonfly');
        document.body.classList.add(themeId);
        const select = document.getElementById('appearance-theme');
        if (select) select.value = themeId;
        if (persist) apiSavePreferences({ theme: themeId }).catch(() => {});
    };

    // -- Appearance section ---------------------------------------------------------------------
    async function initAppearance() {
        let prefs;
        try {
            prefs = await apiGet('/api/preferences');
        } catch (err) {
            showError('Could not load preferences: ' + err.message);
            return;
        }
        window.__applyTheme(prefs.theme, { persist: false });
        document.body.classList.toggle('density-compact', prefs.density === 'compact');
        document.body.classList.toggle('reduced-motion', !!prefs.reduced_motion);
        document.getElementById('appearance-density').value = prefs.density;
        document.getElementById('appearance-font-size').value = prefs.terminal_font_size;
        document.getElementById('appearance-reduced-motion').checked = !!prefs.reduced_motion;

        document.getElementById('appearance-theme').addEventListener('change', (e) => {
            window.__applyTheme(e.target.value);
            flashStatus('appearance-status', true, 'Saved');
        });
        document.getElementById('appearance-density').addEventListener('change', async (e) => {
            document.body.classList.toggle('density-compact', e.target.value === 'compact');
            try {
                await apiSavePreferences({ density: e.target.value });
                flashStatus('appearance-status', true, 'Saved');
            } catch (err) {
                flashStatus('appearance-status', false, 'Could not save');
            }
        });
        document.getElementById('appearance-font-size').addEventListener('change', async (e) => {
            const value = parseInt(e.target.value, 10);
            try {
                const result = await apiSavePreferences({ terminal_font_size: value });
                if (result.ok === false) throw new Error(result.error || 'invalid value');
                flashStatus('appearance-status', true, 'Saved');
            } catch (err) {
                flashStatus('appearance-status', false, err.message);
            }
        });
        document.getElementById('appearance-reduced-motion').addEventListener('change', async (e) => {
            document.body.classList.toggle('reduced-motion', e.target.checked);
            try {
                await apiSavePreferences({ reduced_motion: e.target.checked });
                flashStatus('appearance-status', true, 'Saved');
            } catch (err) {
                flashStatus('appearance-status', false, 'Could not save');
            }
        });
    }

    // -- Provider accounts (Package G, unchanged behavior) --------------------------------------
    const PROVIDER_LABELS = {
        claude: 'Claude Code',
        opencode: 'OpenCode',
        antigravity: 'Antigravity (agy)',
        codex: 'Codex (ChatGPT)',
    };
    const STATE_LABELS = {
        authenticated: 'Verified',
        auth_unverifiable: 'Cannot be confirmed automatically',
        not_authenticated: 'Not signed in',
        auth_expired: 'Login expired',
        not_installed: 'Not installed',
        provider_unavailable: 'Unavailable',
        cli_error: 'CLI error',
        timed_out: 'Timed out',
    };

    function renderProviderCard(status) {
        const label = PROVIDER_LABELS[status.provider] || status.provider;
        const stateLabel = STATE_LABELS[status.auth_state] || status.auth_state;
        const installedText = status.installed ? 'Yes' : 'No';
        const installedClass = status.installed ? 'yes' : 'no';
        const subscriptionBlock = status.subscription_detail
            ? `<div class="subscription-block">
                   <span class="caption">Subscription</span>
                   <div class="text">${escapeHtml(status.subscription_detail)}</div>
               </div>`
            : '';
        return `
            <div class="provider-card accent-${status.auth_state}" data-provider="${status.provider}">
                <h2>${label}</h2>
                <div class="provider-row"><span class="label">Installed</span><span class="value ${installedClass}">${installedText}</span></div>
                <div class="provider-row"><span class="label">Authentication</span>
                    <span class="value state-${status.auth_state}">${stateLabel}</span></div>
                ${subscriptionBlock}
                <div class="detail">${escapeHtml(status.detail || '')}</div>
                <div class="card-actions">
                    <button data-action="login" data-provider="${status.provider}">Login</button>
                    <button data-action="check" data-provider="${status.provider}">Check again</button>
                </div>
            </div>
        `;
    }

    async function loadProviders() {
        try {
            const data = await apiGet('/api/providers');
            document.getElementById('provider-cards').innerHTML =
                (data.providers || []).map(renderProviderCard).join('');
            showError('');
        } catch (err) {
            showError('Could not load provider status: ' + err.message);
        }
    }

    document.getElementById('provider-cards').addEventListener('click', async (event) => {
        const button = event.target.closest('button[data-action]');
        if (!button) return;
        const provider = button.getAttribute('data-provider');
        if (button.getAttribute('data-action') === 'check') {
            await loadProviders();
            return;
        }
        button.disabled = true;
        try {
            const result = await apiControl('provider_login', { provider });
            if (!result.ok) showError(result.error || 'Could not start login.');
        } catch (err) {
            showError('Could not start login: ' + err.message);
        } finally {
            button.disabled = false;
        }
    });

    // -- Boot -------------------------------------------------------------------------------
    initAppearance();
    loadProviders();
    </script>
</body>
</html>
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_provider_settings_page.py -v`
Expected: PASS — every prior assertion still holds (token placeholder, `/api/providers`, `apiControl('provider_login'`, `provider-cards`, Login/Check again, all four provider names, the 2×2 grid rule, the 640px stack rule, `.card-actions { margin-top: auto`, `subscription-block`, `:focus-visible`), plus the new tab/appearance assertions.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/web/settings.html tests/test_provider_settings_page.py
git commit -m "Rebuild settings.html as a tabbed page with an Appearance section"
```

---

## Task 10: `settings.html` — Workspace section

**Files:**
- Modify: `orchestrator/web/settings.html` (fill in `#tab-workspace`, add JS)
- Modify: `tests/test_provider_settings_page.py` (append)

**Interfaces:**
- Consumes: `GET/POST /api/preferences` (default dir, startup view, prune defaults); `GET /api/prune/plan`, `POST /api/prune/execute` (Task 6).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_provider_settings_page.py`:

```python
class TestWorkspaceSection(unittest.TestCase):
    def setUp(self):
        self.page = (WEB_DIR / "settings.html").read_text(encoding="utf-8")

    def test_has_default_dir_and_startup_view_controls(self):
        self.assertIn('id="workspace-default-dir"', self.page)
        self.assertIn('id="workspace-startup-view"', self.page)

    def test_recent_projects_is_documented_as_unavailable(self):
        self.assertIn('Recent projects', self.page)
        self.assertIn('unavailable-note', self.page)

    def test_has_a_preview_before_a_delete_button(self):
        preview_pos = self.page.index('id="prune-preview-btn"')
        delete_pos = self.page.index('id="prune-delete-btn"')
        self.assertLess(preview_pos, delete_pos)
        # The delete control must not be usable before a plan exists.
        self.assertIn('id="prune-delete-btn" disabled', self.page)

    def test_prune_execute_sends_the_previewed_plan(self):
        self.assertIn('/api/prune/plan', self.page)
        self.assertIn('/api/prune/execute', self.page)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_provider_settings_page.py -v -k Workspace`
Expected: FAIL

- [ ] **Step 3: Implement**

Replace the `<!-- Task 10 -->` placeholder inside `#tab-workspace` with:

```html
            <div class="settings-group">
                <h2>Workspace</h2>
                <div class="field-row">
                    <span class="field-label">Default project directory
                        <span class="hint">Used as the starting folder for File &gt; Open Project Folder…</span>
                    </span>
                    <input type="text" id="workspace-default-dir" placeholder="(none set)">
                </div>
                <div class="field-row">
                    <span class="field-label">Startup view</span>
                    <select id="workspace-startup-view">
                        <option value="cockpit">Cockpit</option>
                        <option value="office">Office</option>
                        <option value="design">Design</option>
                    </select>
                </div>
                <p class="unavailable-note">
                    Recent projects: unavailable — Synagon does not currently track which
                    projects were opened previously, so there is nothing for this list to read.
                </p>
                <span class="save-status" id="workspace-status"></span>
            </div>

            <div class="settings-group">
                <h2>Branch &amp; worktree cleanup</h2>
                <p class="unavailable-note" style="margin-top:-6px;">
                    A run's worktree is always removed when the run ends. This only concerns the
                    branches those runs leave behind — nothing is ever deleted without you
                    previewing the exact list first and confirming.
                </p>
                <div class="field-row">
                    <span class="field-label">Only consider branches older than
                        <span class="hint">days</span>
                    </span>
                    <input type="number" id="prune-age-days" min="0" step="1">
                </div>
                <div class="field-row">
                    <span class="field-label">Keep branches whose run did not succeed</span>
                    <input type="checkbox" id="prune-keep-failed">
                </div>
                <div class="field-row">
                    <button id="prune-preview-btn">Preview cleanup</button>
                </div>
                <div id="prune-plan-output"></div>
                <div class="field-row">
                    <button id="prune-delete-btn" class="danger" disabled>Delete 0 branches</button>
                </div>
                <span class="save-status" id="prune-status"></span>
            </div>
```

Add to the `<script>` block, before the "Boot" section:

```javascript
    // -- Workspace section ------------------------------------------------------------------
    let lastPrunePlan = null;

    async function initWorkspace() {
        let prefs;
        try {
            prefs = await apiGet('/api/preferences');
        } catch (err) {
            showError('Could not load preferences: ' + err.message);
            return;
        }
        document.getElementById('workspace-default-dir').value = prefs.default_workspace_dir || '';
        document.getElementById('workspace-startup-view').value = prefs.startup_view;
        document.getElementById('prune-age-days').value = prefs.prune_default_max_age_days;
        document.getElementById('prune-keep-failed').checked = !!prefs.prune_default_keep_failed;

        document.getElementById('workspace-default-dir').addEventListener('change', async (e) => {
            try {
                await apiSavePreferences({ default_workspace_dir: e.target.value || null });
                flashStatus('workspace-status', true, 'Saved');
            } catch (err) {
                flashStatus('workspace-status', false, 'Could not save');
            }
        });
        document.getElementById('workspace-startup-view').addEventListener('change', async (e) => {
            try {
                await apiSavePreferences({ startup_view: e.target.value });
                flashStatus('workspace-status', true, 'Saved');
            } catch (err) {
                flashStatus('workspace-status', false, 'Could not save');
            }
        });
        document.getElementById('prune-age-days').addEventListener('change', (e) => {
            apiSavePreferences({ prune_default_max_age_days: parseInt(e.target.value, 10) }).catch(() => {});
        });
        document.getElementById('prune-keep-failed').addEventListener('change', (e) => {
            apiSavePreferences({ prune_default_keep_failed: e.target.checked }).catch(() => {});
        });

        document.getElementById('prune-preview-btn').addEventListener('click', previewPrune);
        document.getElementById('prune-delete-btn').addEventListener('click', executePrune);
    }

    async function previewPrune() {
        const days = document.getElementById('prune-age-days').value;
        const keepFailed = document.getElementById('prune-keep-failed').checked ? '1' : '0';
        const output = document.getElementById('prune-plan-output');
        const deleteBtn = document.getElementById('prune-delete-btn');
        output.textContent = 'Loading…';
        deleteBtn.disabled = true;
        deleteBtn.textContent = 'Delete 0 branches';
        try {
            lastPrunePlan = await apiGet(`/api/prune/plan?older_than_days=${encodeURIComponent(days)}&keep_failed=${keepFailed}`);
            if (!lastPrunePlan.available) {
                output.textContent = 'Cannot preview: ' + (lastPrunePlan.reason || 'unknown reason');
                return;
            }
            const prunable = lastPrunePlan.prunable || [];
            output.innerHTML = prunable.length
                ? '<pre class="report">' + escapeHtml(prunable.map((c) => c.branch).join('\n')) + '</pre>'
                : '<p class="unavailable-note">Nothing to prune.</p>';
            deleteBtn.disabled = prunable.length === 0;
            deleteBtn.textContent = `Delete ${prunable.length} branch${prunable.length === 1 ? '' : 'es'}`;
        } catch (err) {
            output.textContent = 'Could not load a plan: ' + err.message;
        }
    }

    async function executePrune() {
        if (!lastPrunePlan) return;
        const deleteBtn = document.getElementById('prune-delete-btn');
        deleteBtn.disabled = true;
        try {
            const res = await fetch('/api/prune/execute', {
                method: 'POST',
                headers: { 'X-Orchestrator-Token': TOKEN, 'Content-Type': 'application/json' },
                body: JSON.stringify({ plan: lastPrunePlan }),
            });
            const result = await res.json();
            const deleted = (result.deleted || []).length;
            flashStatus('prune-status', true, `Deleted ${deleted} branch${deleted === 1 ? '' : 'es'}`);
            lastPrunePlan = null;
            document.getElementById('prune-plan-output').textContent = '';
            deleteBtn.textContent = 'Delete 0 branches';
        } catch (err) {
            flashStatus('prune-status', false, 'Could not delete: ' + err.message);
            deleteBtn.disabled = false;
        }
    }
```

Add `initWorkspace();` to the Boot section, next to `initAppearance();`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_provider_settings_page.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add orchestrator/web/settings.html tests/test_provider_settings_page.py
git commit -m "Add the Workspace section to settings.html: default dir, startup view, branch cleanup"
```

---

## Task 11: `settings.html` — Agents & Providers section (role/model summary)

**Files:**
- Modify: `orchestrator/web/settings.html` (extend `#tab-agents`)
- Modify: `tests/test_provider_settings_page.py` (append)

**Interfaces:**
- Consumes: `GET /api/team` (existing route, unchanged — `{team, catalog, templates, problems, writable}`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_provider_settings_page.py`:

```python
class TestAgentsProvidersSection(unittest.TestCase):
    def setUp(self):
        self.page = (WEB_DIR / "settings.html").read_text(encoding="utf-8")

    def test_reads_the_team_api(self):
        self.assertIn("/api/team", self.page)

    def test_shows_account_default_when_model_is_unset(self):
        self.assertIn("Account default", self.page)

    def test_links_to_design_for_editing_assignments(self):
        self.assertIn('href="/design"', self.page)

    def test_documents_provider_enable_disable_as_unavailable(self):
        self.assertIn("enable", self.page.lower())
        self.assertIn("Design", self.page)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_provider_settings_page.py -v -k AgentsProviders`
Expected: FAIL

- [ ] **Step 3: Implement**

Inside `#tab-agents`, after the existing provider-cards `<div class="settings-group">`, add:

```html
            <div class="settings-group">
                <h2>Role Assignments</h2>
                <p class="subtitle" style="margin-bottom:8px;">
                    Read-only here. Editing which agent and model plays which role — enabling or
                    retiring a provider for future runs is the same action as removing it from
                    every role below — is done in <a href="/design">Design the team</a>, which
                    already validates a change before it can be saved.
                </p>
                <div id="team-summary">Loading…</div>
            </div>
```

Add to the `<script>` block:

```javascript
    // -- Agents & Providers: role/model summary (read-only) ------------------------------------
    async function loadTeamSummary() {
        const el = document.getElementById('team-summary');
        try {
            const data = await apiGet('/api/team');
            const rows = (data.team.agents || []).map((entry) => {
                const model = entry.model;
                const shown = model == null
                    ? 'Account default'
                    : (Array.isArray(model) ? model.join(' → ') : escapeHtml(model));
                return `<div class="provider-row"><span class="label">${escapeHtml(entry.role)} — ${escapeHtml(entry.agent)}</span><span class="value">${shown}</span></div>`;
            });
            el.innerHTML = rows.length ? rows.join('') : '<p class="unavailable-note">No agents configured.</p>';
        } catch (err) {
            el.textContent = 'Could not load the team: ' + err.message;
        }
    }
```

Add `loadTeamSummary();` to the Boot section.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_provider_settings_page.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add orchestrator/web/settings.html tests/test_provider_settings_page.py
git commit -m "Add a read-only role/model summary to the Agents & Providers section"
```

---

## Task 12: `settings.html` — Execution and Terminal & Desktop sections, plus cockpit budget-display

**Files:**
- Modify: `orchestrator/web/settings.html` (fill `#tab-execution`, `#tab-terminal`)
- Modify: `orchestrator/web/cockpit.html` (`#status-tokens` honors `budget_display`)
- Modify: `tests/test_provider_settings_page.py` (append)

**Interfaces:**
- Consumes: `GET/POST /api/settings` (Task 4) for all execution/terminal fields; `GET/POST /api/preferences` for `budget_display` (per-machine, display-only).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_provider_settings_page.py`:

```python
class TestExecutionSection(unittest.TestCase):
    def setUp(self):
        self.page = (WEB_DIR / "settings.html").read_text(encoding="utf-8")

    def test_has_the_execution_fields(self):
        for field_id in (
            "execution-mode", "execution-retry-attempts", "execution-repair-attempts",
            "execution-escalate", "execution-session-duration", "execution-goal-duration",
        ):
            self.assertIn(f'id="{field_id}"', self.page)

    def test_saves_through_settings_api(self):
        self.assertIn("apiSaveSettings", self.page)

    def test_budget_display_is_a_preference_not_a_setting(self):
        self.assertIn('id="execution-budget-display"', self.page)
        self.assertIn("does not change enforcement", self.page.lower())


class TestTerminalDesktopSection(unittest.TestCase):
    def setUp(self):
        self.page = (WEB_DIR / "settings.html").read_text(encoding="utf-8")

    def test_has_visibility_and_terminal_type(self):
        self.assertIn('id="terminal-visible"', self.page)
        self.assertIn('id="terminal-type"', self.page)

    def test_notes_native_tui_is_the_execution_mode_field(self):
        self.assertIn("same field", self.page.lower())

    def test_has_output_verbosity_mapped_to_run_store(self):
        self.assertIn('id="terminal-output-verbosity"', self.page)

    def test_documents_detached_login_as_read_only(self):
        self.assertIn("detached", self.page.lower())


class TestCockpitBudgetDisplay(unittest.TestCase):
    def test_status_tokens_honors_budget_display_preference(self):
        page = (WEB_DIR / "cockpit.html").read_text(encoding="utf-8")
        self.assertIn("budget_display", page)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_provider_settings_page.py -v -k "Execution or TerminalDesktop or CockpitBudget"`
Expected: FAIL

- [ ] **Step 3a: Execution section**

Replace the `<!-- Task 12 -->` placeholder inside `#tab-execution` with:

```html
            <div class="settings-group">
                <h2>Execution</h2>
                <p class="unavailable-note" style="margin-top:-6px;">Saved immediately; used by the next goal you start — no restart needed.</p>
                <div class="field-row">
                    <span class="field-label">Default execution mode</span>
                    <select id="execution-mode">
                        <option value="auto">Auto</option>
                        <option value="native_tui">Native TUI</option>
                        <option value="headless">Headless</option>
                    </select>
                </div>
                <div class="field-row">
                    <span class="field-label">Retry attempts <span class="hint">1–10</span></span>
                    <input type="number" id="execution-retry-attempts" min="1" max="10" step="1">
                </div>
                <div class="field-row">
                    <span class="field-label">Repair-attempt limit</span>
                    <input type="number" id="execution-repair-attempts" min="0" step="1">
                </div>
                <div class="field-row">
                    <span class="field-label">Escalate to a different model on retry</span>
                    <input type="checkbox" id="execution-escalate">
                </div>
                <div class="field-row">
                    <span class="field-label">Session duration ceiling <span class="hint">seconds, 0 = unlimited</span></span>
                    <input type="number" id="execution-session-duration" min="0" step="1">
                </div>
                <div class="field-row">
                    <span class="field-label">Goal duration ceiling <span class="hint">seconds, 0 = unlimited</span></span>
                    <input type="number" id="execution-goal-duration" min="0" step="1">
                </div>
                <span class="save-status" id="execution-status"></span>
            </div>

            <div class="settings-group">
                <h2>Budget display</h2>
                <p class="unavailable-note" style="margin-top:-6px;">
                    This machine only — controls how the cockpit's token counter is shown. It
                    does not change enforcement; the ceilings above are what actually stop a run.
                </p>
                <div class="field-row">
                    <span class="field-label">Cockpit token counter</span>
                    <select id="execution-budget-display">
                        <option value="detailed">Detailed</option>
                        <option value="compact">Compact</option>
                        <option value="hidden">Hidden</option>
                    </select>
                </div>
            </div>
```

- [ ] **Step 3b: Terminal & Desktop section**

Replace the `<!-- Task 12 -->` placeholder inside `#tab-terminal` with:

```html
            <div class="settings-group">
                <h2>Terminal &amp; Desktop</h2>
                <p class="unavailable-note" style="margin-top:-6px;">Saved immediately; used by the next goal you start — no restart needed.</p>
                <div class="field-row">
                    <span class="field-label">Show agent terminals</span>
                    <input type="checkbox" id="terminal-visible">
                </div>
                <div class="field-row">
                    <span class="field-label">Terminal type</span>
                    <select id="terminal-type">
                        <option value="auto">Auto</option>
                        <option value="antigravity_integrated">Antigravity integrated</option>
                        <option value="integrated">Integrated</option>
                        <option value="windows_terminal">Windows Terminal</option>
                        <option value="console">Console</option>
                        <option value="wt">wt</option>
                        <option value="cmd">cmd</option>
                        <option value="none">None</option>
                    </select>
                </div>
                <p class="unavailable-note">
                    Native-TUI preference is the same field as Execution's "Default execution
                    mode" above (native_tui) — it is not a second, separate switch.
                </p>
                <div class="field-row">
                    <span class="field-label">Output detail kept per run
                        <span class="hint">how much of each agent's output is retained</span>
                    </span>
                    <select id="terminal-output-verbosity">
                        <option value="0">Full detail</option>
                        <option value="20000">Standard (20,000 characters)</option>
                        <option value="4000">Compact (4,000 characters)</option>
                    </select>
                </div>
                <p class="unavailable-note">
                    Detached-login behavior (used by Login on the Agents &amp; Providers tab) is
                    fixed: each provider's own login runs in its own detached terminal that
                    Synagon never waits on, kills, or otherwise owns. There is no setting for it.
                </p>
                <span class="save-status" id="terminal-status"></span>
            </div>
```

- [ ] **Step 3c: Wire both sections' JS**

Add to the `<script>` block:

```javascript
    // -- Execution & Terminal/Desktop: project-level, via /api/settings ------------------------
    const EXECUTION_FIELD_IDS = {
        'execution-mode': 'execution.agent_execution_mode',
        'execution-retry-attempts': 'execution.retry.attempts',
        'execution-repair-attempts': 'max_repair_attempts',
        'execution-escalate': 'execution.retry.escalate_model',
        'execution-session-duration': 'budget.max_duration_seconds',
        'execution-goal-duration': 'budget.goal_max_duration_seconds',
        'terminal-visible': 'execution.visible_terminals',
        'terminal-type': 'execution.terminal_type',
        'terminal-output-verbosity': 'run_store.max_output_chars',
    };

    function readFieldValue(elementId) {
        const el = document.getElementById(elementId);
        if (el.type === 'checkbox') return el.checked;
        if (el.tagName === 'SELECT' && ['execution-retry-attempts', 'execution-repair-attempts',
            'execution-session-duration', 'execution-goal-duration'].indexOf(elementId) === -1) {
            return elementId === 'terminal-output-verbosity' ? parseInt(el.value, 10) : el.value;
        }
        if (el.type === 'number') return parseInt(el.value, 10);
        return el.value;
    }

    async function initExecutionAndTerminal() {
        let settings;
        try {
            settings = await apiGet('/api/settings');
        } catch (err) {
            showError('Could not load settings: ' + err.message);
            return;
        }
        document.getElementById('execution-mode').value = settings['execution.agent_execution_mode'];
        document.getElementById('execution-retry-attempts').value = settings['execution.retry.attempts'];
        document.getElementById('execution-repair-attempts').value = settings['max_repair_attempts'];
        document.getElementById('execution-escalate').checked = !!settings['execution.retry.escalate_model'];
        document.getElementById('execution-session-duration').value = settings['budget.max_duration_seconds'];
        document.getElementById('execution-goal-duration').value = settings['budget.goal_max_duration_seconds'];
        document.getElementById('terminal-visible').checked = !!settings['execution.visible_terminals'];
        document.getElementById('terminal-type').value = settings['execution.terminal_type'];
        document.getElementById('terminal-output-verbosity').value = String(settings['run_store.max_output_chars']);

        let prefs;
        try {
            prefs = await apiGet('/api/preferences');
            document.getElementById('execution-budget-display').value = prefs.budget_display;
        } catch (err) { /* leave the default selected */ }

        for (const [elementId, settingName] of Object.entries(EXECUTION_FIELD_IDS)) {
            const statusId = elementId.startsWith('terminal') ? 'terminal-status' : 'execution-status';
            document.getElementById(elementId).addEventListener('change', async (e) => {
                try {
                    const result = await apiSaveSettings({ [settingName]: readFieldValue(elementId) });
                    if (result.ok === false) throw new Error((result.problems || [result.error]).join(' '));
                    flashStatus(statusId, true, 'Saved');
                } catch (err) {
                    flashStatus(statusId, false, err.message);
                }
            });
        }

        document.getElementById('execution-budget-display').addEventListener('change', async (e) => {
            try {
                await apiSavePreferences({ budget_display: e.target.value });
                flashStatus('execution-status', true, 'Saved');
            } catch (err) {
                flashStatus('execution-status', false, 'Could not save');
            }
        });
    }
```

Add `initExecutionAndTerminal();` to the Boot section.

- [ ] **Step 3d: Cockpit budget-display wiring**

In `orchestrator/web/cockpit.html`, near where `DOM.statusTokens.innerText` is set (inside the function that updates the status bar from `state.cockpit`), read the preference once at boot and store it, then use it when rendering. Add, alongside the existing `apiSavePreferences` helper introduced in Task 7:

```javascript
    let budgetDisplayMode = 'compact';
```

In `boot()`, alongside the theme-loading code from Task 7:

```javascript
        try {
            const prefs2 = await apiGet('/api/preferences');
            budgetDisplayMode = prefs2.budget_display || 'compact';
        } catch (err) { /* keep 'compact' */ }
```

Replace:

```javascript
        DOM.statusTokens.innerText = `TOKENS ${runTokenLabel(totalTokens)}`;
```

with:

```javascript
        if (budgetDisplayMode === 'hidden') {
            DOM.statusTokens.style.display = 'none';
        } else {
            DOM.statusTokens.style.display = '';
            DOM.statusTokens.innerText = budgetDisplayMode === 'detailed'
                ? `TOKENS ${runTokenLabel(totalTokens)} (budget_display: detailed)`
                : `TOKENS ${runTokenLabel(totalTokens)}`;
        }
```

(The "detailed" branch above is intentionally the simplest correct increment over "compact" — it does not fetch a budget ceiling from `/api/settings` on every poll tick, which would add a request to a function that already runs on a fast refresh loop. A future package can extend "detailed" to show `spent / ceiling` once there is a cheap way to include the ceiling in the existing `/api/cockpit` payload; for now the preference's three states are all real and distinct — shown, shown with a explicit detailed marker, and hidden — and enforcement is untouched either way.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_provider_settings_page.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add orchestrator/web/settings.html orchestrator/web/cockpit.html tests/test_provider_settings_page.py
git commit -m "Add Execution and Terminal & Desktop settings sections; wire budget-display preference into the cockpit"
```

---

## Task 13: `settings.html` — Safety, Privacy, Diagnostics section

**Files:**
- Modify: `orchestrator/web/settings.html` (fill `#tab-safety`)
- Modify: `tests/test_provider_settings_page.py` (append)

**Interfaces:**
- Consumes: `GET /api/doctor`, `GET /api/diagnostics`, `GET /api/app_info` (Task 5); `GET /api/providers` (existing, reused).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_provider_settings_page.py`:

```python
class TestSafetyDiagnosticsSection(unittest.TestCase):
    def setUp(self):
        self.page = (WEB_DIR / "settings.html").read_text(encoding="utf-8")

    def test_has_credential_handling_explanation(self):
        self.assertIn("never reads, stores, or transmits a credential", self.page.lower().replace("Synagon ", ""))

    def test_has_doctor_and_diagnostics_buttons(self):
        self.assertIn('id="run-doctor-btn"', self.page)
        self.assertIn('id="generate-diagnostics-btn"', self.page)

    def test_has_a_check_providers_control(self):
        self.assertIn('id="check-providers-btn"', self.page)

    def test_fetches_app_info(self):
        self.assertIn("/api/app_info", self.page)

    def test_never_renders_a_credential_field(self):
        lowered = self.page.lower()
        for banned in ("api_key", "auth.json", "token=", "password"):
            self.assertNotIn(banned, lowered)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_provider_settings_page.py -v -k SafetyDiagnostics`
Expected: FAIL

- [ ] **Step 3: Implement**

Replace the `<!-- Task 13 -->` placeholder inside `#tab-safety` with:

```html
            <div class="settings-group">
                <h2>Credential Handling</h2>
                <p class="subtitle" style="margin-bottom:0;">
                    Synagon never reads, stores, or transmits a credential, API key, or token of
                    any kind for any provider. Provider status is read only from commands each
                    provider documents as safe and non-interactive; logging in always opens that
                    provider's own flow in its own detached terminal. Anything a diagnostic
                    report below shows has already been passed through the same redaction filter
                    that scrubs API-key-, bearer-token-, and secret-shaped text before it is ever
                    rendered.
                </p>
            </div>

            <div class="settings-group">
                <h2>Doctor &amp; Provider Checks</h2>
                <div class="field-row">
                    <button id="run-doctor-btn">Run Doctor</button>
                    <button id="check-providers-btn">Check Providers</button>
                </div>
                <div id="doctor-output"></div>
            </div>

            <div class="settings-group">
                <h2>Diagnostic Report</h2>
                <p class="unavailable-note" style="margin-top:-6px;">
                    Combines the Doctor probe and provider status into one report, already
                    redacted, for you to copy if you need to share it.
                </p>
                <div class="field-row">
                    <button id="generate-diagnostics-btn">Generate diagnostic report</button>
                </div>
                <div id="diagnostics-output"></div>
            </div>

            <div class="settings-group">
                <h2>Application</h2>
                <div id="app-info">Loading…</div>
            </div>
```

Add to the `<script>` block:

```javascript
    // -- Safety, Privacy, Diagnostics -----------------------------------------------------------
    async function initSafety() {
        document.getElementById('run-doctor-btn').addEventListener('click', async () => {
            const out = document.getElementById('doctor-output');
            out.textContent = 'Running…';
            try {
                const data = await apiGet('/api/doctor');
                out.innerHTML = '<pre class="report">' + escapeHtml(data.text) + '</pre>';
            } catch (err) {
                out.textContent = 'Could not run Doctor: ' + err.message;
            }
        });

        document.getElementById('check-providers-btn').addEventListener('click', () => {
            document.getElementById('tabbtn-agents').click();
            loadProviders();
        });

        document.getElementById('generate-diagnostics-btn').addEventListener('click', async () => {
            const out = document.getElementById('diagnostics-output');
            out.textContent = 'Generating…';
            try {
                const data = await apiGet('/api/diagnostics');
                out.innerHTML = '<pre class="report">' + escapeHtml(data.text) + '</pre>';
            } catch (err) {
                out.textContent = 'Could not generate a report: ' + err.message;
            }
        });

        const infoEl = document.getElementById('app-info');
        try {
            const info = await apiGet('/api/app_info');
            const validity = info.config_valid
                ? '<span class="value yes">Valid</span>'
                : `<span class="value state-not_authenticated">Invalid — ${escapeHtml(info.config_error || '')}</span>`;
            infoEl.innerHTML =
                `<div class="provider-row"><span class="label">Version</span><span class="value">${escapeHtml(info.version)}</span></div>` +
                `<div class="provider-row"><span class="label">Configuration</span>${validity}</div>`;
        } catch (err) {
            infoEl.textContent = 'Could not load application info: ' + err.message;
        }
    }
```

Add `initSafety();` to the Boot section.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_provider_settings_page.py -v`
Expected: PASS (every test in the file)

- [ ] **Step 5: Full-file review, then commit**

Run: `python -m pytest tests/test_preferences.py tests/test_settings_patch.py tests/test_package_i.py tests/test_provider_settings_page.py -v`
Expected: PASS (every test from Tasks 1–13)

```bash
git add orchestrator/web/settings.html tests/test_provider_settings_page.py
git commit -m "Add the Safety, Privacy, Diagnostics section to settings.html"
```

---

## Task 14: Manual desktop verification (no automated JS/Electron test runner exists)

**Files:** none modified — this task is a verification pass, and its outcome is reported, not committed as code.

There is no JS/Electron test runner in this repository (`desktop/package.json` has no `test` script, no Jest/Playwright/Vitest dependency), and adding one is out of scope for this package (stated as a limitation in the design spec). This task hand-verifies the pieces automated tests cannot reach.

- [ ] **Step 1: Run the full offline Python suite**

Run: `python -m pytest tests/ -v`
Expected: PASS, no regressions in any pre-existing test file.

- [ ] **Step 2: Launch the desktop app**

Run: `cd desktop && npm start` (or the project's existing documented launch command — check `desktop/README.md` first if `npm start` does not apply)

- [ ] **Step 3: Verify the menu**

- [ ] Settings menu shows "Open Settings…" with `Ctrl+,` (or `Cmd+,` on macOS) next to it, and a separator, then "Theme" with 5 radio items, one checked.
- [ ] "Open Settings…" opens `/settings` in the current window and shows all six tabs.
- [ ] Clicking a different Theme radio item changes the open window's colors immediately, and the radio state updates.
- [ ] Quit and relaunch the app on the same project: the theme chosen persists (this is the fix for the pre-existing localStorage/random-port limitation — confirm it actually survives relaunch, not just navigation).

- [ ] **Step 4: Verify Settings page interactively**

- [ ] Appearance: change theme, density, font size, reduced motion — each applies immediately and a "Saved" status appears.
- [ ] Workspace: set a default directory, change startup view, run "Preview cleanup" (verify it never deletes without the second explicit click), confirm "Delete N branches" is disabled until a preview has run.
- [ ] Agents & Providers: existing provider cards still show correct status; Login/Check again still work; the role/model summary shows "Account default" for any role with no `model:` set; the Design link opens `/design`.
- [ ] Execution: change retry attempts, repair limit, escalate toggle, duration ceilings — confirm `/api/settings` round-trips (reload the page, values persist) and `orchestrator.yaml` shows a `.bak-<timestamp>` backup file after the first save.
- [ ] Terminal & Desktop: change visible-terminals, terminal type, output-verbosity — same persistence check.
- [ ] Safety & Diagnostics: Run Doctor and Generate diagnostic report both return text with no raw credential-shaped strings in it; Check Providers switches to the Agents tab and refreshes.

- [ ] **Step 5: Narrow-width and accessibility check**

- [ ] Resize the window to ~400px wide (or open `/settings` in a narrow browser tab): tabs wrap, no horizontal scroll on the page body, provider cards stack to one column.
- [ ] Tab through the whole Settings page with keyboard only: every control shows a visible focus ring (`:focus-visible`), and Left/Right arrow keys move between tabs per the ARIA tablist pattern implemented in Task 9.

- [ ] **Step 6: Confirm nothing pre-existing broke**

- [ ] `/cockpit` still loads, still themes correctly, and its own header "SETTINGS" link (still de-emphasized, still pointing at `/settings`) still works from a plain browser tab.
- [ ] `/api/providers`, `/api/team`, `/design` all still behave exactly as before this package.

**Report at the end of this task:** which manual checks passed, any that did not (with what was found), and the final PASS/FAIL verdict for the whole package.

---

## Self-Review Notes (completed during planning)

- **Spec coverage:** Appearance (Task 9), Workspace incl. worktree cleanup (Task 10), Agents & Providers incl. "Account default" and provider enable/disable documented as unavailable (Task 11), Execution incl. budget-display as a non-enforcement preference (Task 12), Terminal & Desktop incl. native-TUI cross-reference and detached-login preserved (Task 12), Safety/Privacy/Diagnostics incl. Doctor, Check Providers, version/validity, diagnostic report (Task 13), menu integration and Theme submenu (Task 8), shared theme CSS (Task 7), narrow write API outside `daemon.control()` (Tasks 2, 4, 5, 6), validation/backup/never-silent-overwrite on every write path (Tasks 3, 6), manual + automated testing (Task 14, plus every task's own test step) are all covered by a task above. Recent-projects is explicitly implemented as an "unavailable" note (Task 10), not a fake control.
- **Placeholder scan:** every step above contains complete, runnable code; no "TBD"/"add validation"/"similar to Task N" remains except the two explicit `<!-- Task N -->` HTML comments in Task 9's shell, which Tasks 10–13 each replace with real markup in their own step.
- **Type/name consistency checked:** `preferences.load_preferences`/`save_preferences` (Task 1) are the exact names Tasks 2, 6, 7, 8, 9–13 call; `settings_patch.current_settings`/`write_settings`/`SETTINGS_FIELDS` (Task 3) are the exact names Task 4 calls and Task 12's `EXECUTION_FIELD_IDS` dotted keys match `SETTINGS_FIELDS` exactly; `Daemon.doctor_report`/`diagnostics_report`/`prune_plan`/`prune_execute` (Tasks 5–6) are the exact names their routes call; `window.__applyTheme` (Task 7) is the exact name Task 8's `executeJavaScript` call and Task 9's settings.html both use.
