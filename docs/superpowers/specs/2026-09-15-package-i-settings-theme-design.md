# Package I — Settings and Theme Integration with Implemented Customization Controls (Design)

Status: approved 2026-09-15, implementation in progress
Date: 2026-09-15

## Problem

The desktop shell (`desktop/main.js`) has a File/View/Go/Window menu and, as of commit 064f9e1, a
Settings menu with one item, "Provider Accounts…", that opens `/settings` — a single-purpose page
(`orchestrator/web/settings.html`) showing only provider auth status. Theme (five named color
schemes) lives entirely inside `cockpit.html`, persisted to `localStorage` under `cockpit-theme`.
There is no general settings surface, no per-machine preference store, and no daemon write path
for anything except the team/role document (`POST /api/team`).

This package turns Settings into a real menu entry with a sectioned settings page, moves Theme
into the menu, and implements the settings that the existing architecture can actually support
safely — extending `orchestrator.yaml`'s validated schema and the daemon's existing read/write
discipline rather than inventing a parallel configuration system.

## Non-goals

- No System/Light/Dark theme model. The only theme concept that exists is five named dark
  color schemes (Srcery, Moonfly, Jellybeans, Tender, Miasma); this package exposes those, not a
  new light-mode design.
- No new entry in `daemon.control()`'s closed action-verb list. New write capability is added as
  narrow routes outside that dispatcher, each following `/api/team`'s validate → write → reload
  pattern.
- No provider enable/disable flag independent of role assignment — no such concept exists in the
  schema today, and role assignment (already editable via `/api/team`) already expresses it.
- No recent-projects list — no existing tracking mechanism to extend.
- No credential/token/keyring exposure of any kind, anywhere in the new UI.
- No change to worktree-per-run behavior (`workspace.finish_worktree`), to detached-login process
  ownership, to `RUNNABLE_AGENTS`, or to any existing provider auth/probe logic.

## Existing architecture (grounding)

- **Desktop shell**: Electron 33, `desktop/main.js`, one global `Menu.setApplicationMenu`. Menu
  items either use built-in `role:` entries (free OS accelerators) or custom `click` handlers that
  `window.loadURL()` a daemon route — there is no client-side router, no IPC bridge beyond the
  inert `orchestratorShell` flag in `preload.js`.
- **Serving**: `orchestrator/daemon.py` (`ThreadingHTTPServer`, loopback-only, per-launch token
  auth via `_authorised()`). Pages are static HTML/CSS/JS files in `orchestrator/web/`, no build
  step, no framework, no shared stylesheet between pages today.
- **Config**: `orchestrator.yaml` at the project root, schema and validation in `orchestrator/
  config.py` (`OrchestratorConfig`, `validate_config()`, per-field `DEFAULT_*_CONFIG` constants).
  The only existing write path is `POST /api/team` → `teams.write_team()` → `reload_config()`.
- **Per-machine state precedent**: `orchestrator/native_sessions.py` writes
  `~/.orchestrator/native_sessions.json` with an atomic tmp-file-then-`os.replace` pattern and a
  test-only path override (`ORCHESTRATOR_NATIVE_SESSIONS_FILE`). This is the pattern the new
  preferences store follows.
- **Diagnostics precedent**: `--doctor` (CLI-only, `orchestrator/preflight.py`) and
  `--check-providers` (CLI + `GET /api/providers`, `orchestrator/provider_auth.py`) are both
  already redaction-safe report generators; `--doctor` has no daemon route yet.
- **Branch/worktree retention**: `orchestrator/prune.py`'s `--prune-runs` is CLI-only, always
  plan-then-confirm, never auto-deletes, and never touches a dirty worktree or the checked-out
  branch. No daemon route exists.

## Design

### 1. Per-machine preferences store

New module `orchestrator/preferences.py`, same shape as `native_sessions.py`:

```python
def preferences_path() -> str:
    override = os.environ.get("ORCHESTRATOR_PREFERENCES_FILE")
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), ".orchestrator", "preferences.json")

def load_preferences() -> Dict[str, Any]: ...   # returns DEFAULT_PREFERENCES merged with the file
def save_preferences(patch: Dict[str, Any]) -> Dict[str, Any]: ...  # merge + atomic write, returns full doc
```

Fields and defaults (`DEFAULT_PREFERENCES`):

| Field | Type | Default | Notes |
|---|---|---|---|
| `theme` | `"theme-srcery"` \| `"theme-moonfly"` \| `"theme-jellybeans"` \| `"theme-tender"` \| `"theme-miasma"` | `"theme-srcery"` | the 5 existing scheme classes |
| `density` | `"comfortable"` \| `"compact"` | `"comfortable"` | new body-level CSS class |
| `terminal_font_size` | int, 10–24 | 13 | validated range |
| `reduced_motion` | bool | `false` | adds a CSS override class |
| `default_workspace_dir` | str or `null` | `null` | feeds Electron's open-folder dialog |
| `startup_view` | `"cockpit"` \| `"office"` \| `"design"` | `"cockpit"` | only existing pages qualify |
| `output_verbosity` | `"expanded"` \| `"collapsed"` | `"expanded"` | default state for cockpit's existing `#collapse-all-btn` behavior |
| `budget_display` | `"detailed"` \| `"compact"` \| `"hidden"` | `"detailed"` | display-only, never touches `budget.*` enforcement |
| `prune_default_max_age_days` | int ≥ 0 | matches `prune.py`'s `DEFAULT_MAX_AGE` | default for the cleanup-preview form |
| `prune_default_keep_failed` | bool | `true` | default for the cleanup-preview form |

Both the Electron main process (`fs`, for menu radio state and the folder-picker default) and the
daemon (`GET`/`POST /api/preferences`) read/write this same file independently; each write is a
full atomic replace, so a lost race is last-writer-wins on a non-destructive preferences file, not
a correctness problem.

### 2. New daemon routes (outside `daemon.control()`, `/api/team`'s discipline)

| Route | Method | Behavior |
|---|---|---|
| `/api/preferences` | GET | returns `load_preferences()` |
| `/api/preferences` | POST | validates known keys/ranges, `save_preferences(patch)`, returns full doc |
| `/api/settings` | GET | returns the current values of the project-level fields below, read from `self.config` |
| `/api/settings` | POST | validates via `config.validate_config()`-compatible checks, patches only the named `orchestrator.yaml` block(s) with the same text-preserving rewrite `teams.py` already uses, `reload_config()` |
| `/api/doctor` | GET | runs `preflight.run_preflight()` against the current config, returns the structured report (same data `format_preflight_report()` renders) |
| `/api/prune/plan` | GET | runs `prune.plan_prune()` with query-param or preference-default age/keep-failed, returns the plan — never deletes |
| `/api/prune/execute` | POST | takes the *exact* previously-returned plan (or its id), calls `prune.execute_prune()` — refuses a plan that doesn't match current repo state rather than silently recomputing |
| `/api/diagnostics` | GET | returns `format_preflight_report()` + `format_provider_report()` concatenated — both already redaction-safe |

All writes require the existing per-launch token + Origin check (`_authorised()`); no new auth
mechanism.

### 3. `orchestrator.yaml` schema additions

Only fields that map to something the request asks for and that don't already exist are new; all
others reuse existing schema as-is:

- No brand-new top-level keys — `/api/settings` PATCHes existing sections: `execution.
  agent_execution_mode`, `execution.visible_terminals`, `execution.terminal_type`, `execution.
  retry.attempts`, `execution.retry.escalate_model`, `max_repair_attempts`, `budget.
  max_duration_seconds`, `budget.goal_max_duration_seconds`.
- Validation reuses `config.py`'s existing bounds (`MAX_RETRY_ATTEMPTS=10`, etc.) — no new ceiling
  invented.

### 4. Desktop menu (`desktop/main.js`)

- Settings > "Provider Accounts…" → renamed "Open Settings…", accelerator `CmdOrCtrl+,`, same
  `/settings` route (now the sectioned page).
- New Settings > Theme submenu: 5 radio items bound to `preferences.json`'s `theme` field, checked
  state read at menu-build time and after every change. Click handler: write preferences.json,
  then `window.webContents.executeJavaScript` a call to `window.__applyTheme(id)` on the focused
  window if the loaded page defines it (cockpit.html and the expanded settings.html both will).
- No changes to File/View/Go/Window.

### 5. Settings page (`orchestrator/web/settings.html`)

Grows into six sections (Appearance, Workspace, Agents & Providers, Execution, Terminal &
Desktop, Safety/Privacy/Diagnostics) in one static file, following existing page conventions (no
framework, no build step). The current provider-account card grid becomes the Agents & Providers
section body, unchanged in markup/behavior. Each section:

- **Appearance**: theme picker (mirrors the menu, same `__applyTheme`/`/api/preferences` path),
  density, terminal font size, reduced motion. Applies immediately client-side; Save persists.
- **Workspace**: default project directory (text field + note that it takes effect on next Open
  Folder dialog), startup view, worktree/branch cleanup — a "Preview cleanup" button calling
  `GET /api/prune/plan` renders the plan (branches, ages, why each is kept/pruned), and only then
  a clearly labeled "Delete N branches" button becomes available, posting that exact plan to
  `POST /api/prune/execute`. Recent-projects is shown as "not available" with a one-line reason.
- **Agents & Providers**: existing provider cards (unchanged) plus a read-only role→agent/model
  table from `GET /api/team`, "Account default" shown when a role's `model` is unset. A link to
  the Design surface for editing role assignments (reuses `/api/team`, no new endpoint). Provider
  enable/disable is shown as "not available — controlled by role assignment in Design" rather than
  a fake toggle.
- **Execution**: execution mode, retry attempts, repair-attempt limit, escalate-on-retry toggle,
  session/goal duration ceilings (labeled "0 = unlimited"), budget-display preference (client-only
  toggle on cockpit's existing budget UI).
- **Terminal & Desktop**: visible-terminals toggle, terminal type, and a note that native-TUI
  preference is the same `execution.agent_execution_mode` field shown under Execution (not
  duplicated); output-verbosity default; detached-login behavior described read-only, not
  editable.
- **Safety, Privacy, Diagnostics**: static credential-handling text (sourced from `provider_auth.
  py`'s docstring and README's existing linking section), "Run Doctor" (`GET /api/doctor`),
  "Check Providers" (existing `/api/providers`, reused), app version + `validate_config()` result,
  "Generate diagnostic report" (`GET /api/diagnostics`) shown as read-only text, no download (the
  sandboxed desktop `<a download>` restriction doesn't apply here since this is the daemon's own
  page, but keeping it copy/viewable text avoids adding new file-save plumbing for one button).

Every field that requires a daemon round-trip carries: Apply/Save (writes now), a note when a
change needs a daemon restart to take effect (none currently do — every field is either read on
next use or re-read via `reload_config()`), and a Reset-to-defaults per section. Validation errors
surface inline with the existing error-message style `config.py` already uses.

### 6. Theme CSS extraction

The five `body.theme-*` blocks move out of `cockpit.html`'s inline `<style>` into one shared
`/shared/theme.css`, served from the daemon's existing vendored-asset allow-list (same mechanism
`gsap.min.js` uses). `cockpit.html` and the expanded `settings.html` both `<link>` it and drop
their duplicated color literals in favor of the existing CSS custom properties. `office.html` and
`design.html` are left unchanged unless adopting the shared file turns out to be a trivial
mechanical swap during implementation — if not, they stay as they are today (noted as a
follow-up, not a regression).

## Testing

- `tests/test_preferences.py` — new module, mirrors `native_sessions.py`'s test style: defaults,
  merge-on-save, atomic write, path override.
- `tests/test_config.py` — extended with validation tests for the fields exposed via
  `/api/settings` (already-existing bounds, just newly reachable).
- New route tests in the `daemon`/`serve` test files for `/api/preferences`, `/api/settings`,
  `/api/doctor`, `/api/prune/plan`, `/api/prune/execute`, `/api/diagnostics` — auth-required,
  validation-rejects-bad-input, happy-path.
- `tests/test_settings_page.py` (rename/extend `test_provider_settings_page.py`) — content-only
  assertions against the expanded `settings.html`, following the existing no-browser pattern.
- Desktop/menu: hand-verified only (Electron BrowserWindow launch, menu clicks, relaunch
  persistence check) — there is no JS/Electron test runner in this repo today, and adding one is
  out of scope for this package. Stated as a limitation, not silently skipped.

## Explicitly unavailable (documented, not faked)

- Real System/Light/Dark theming.
- Recent-projects list.
- A standalone provider enable/disable flag.
- Output "verbosity levels" beyond the existing collapse/expand default.
- Any change to detached-login behavior (preserved as-is, by requirement).
