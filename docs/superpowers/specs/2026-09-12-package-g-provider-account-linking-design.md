# Package G — Local AI Provider Account Linking (Design)

Status: approved for implementation
Date: 2026-09-12

## Problem

Synagon orchestrates three local CLI providers — Claude Code (`claude`), Antigravity CLI
(`agy`), and OpenCode (`opencode`) — but has no way for a user to check, from Synagon, whether
each CLI is installed and signed in to *their own* subscription/account before a run. Today the
only related check is `preflight.py`, which confirms a binary resolves and (in deep mode)
responds to `--version`; it says nothing about authentication.

This package adds a read-only-by-default status check plus a way to launch each provider's own
login flow, without Synagon ever touching a credential.

## Non-goals

- No new Synagon account/auth system.
- No credential storage, extraction, or display of any kind.
- No change to `preflight.py`'s existing contract (run-readiness), which stays scoped to
  runner/executable/model/role/version.
- No claim of subscription/entitlement verification where a CLI doesn't officially expose it.

## Researched ground truth (2026-09-12)

Verified against each vendor's own docs, not assumed. This matters because the whole feature's
credibility rests on requirement "use only documented, officially supported commands."

| Provider | Real binary | Official non-interactive install/version check | Official non-interactive auth check | Official login |
|---|---|---|---|---|
| Claude Code | `claude` | `shutil.which`, `claude --version` (both already used by `preflight.py`) | **None.** `claude doctor` is a real shell-level command (confirmed via multiple independent sources, e.g. code.claude.com and community write-ups) but is documented as an installation/config diagnostic, not an auth signal. `/login`, `/logout`, `/status` are real but are in-REPL slash commands only — not scriptable without driving a pty, which the spec explicitly forbids ("never scrape... ANSI terminal output"). Note: `claude auth status`/`ant auth status` belongs to the *separate* `ant` Claude Platform CLI (platform.claude.com/docs/en/cli-sdks-libraries/cli/authentication) — a different product for API/workspace administration, not Claude Code's subscription login. An earlier automated fetch conflated the two; this was caught on cross-check with the authoritative code.claude.com page and is called out here so it isn't rediscovered as a "bug" later. | Run bare `claude` — its own first-run/`/login` flow takes over. |
| Antigravity CLI | `agy` (a real Google product — `github.com/google-antigravity/antigravity-cli`, docs at `antigravity.google/docs/cli/`) | `shutil.which`, `agy --version` (already used by `preflight.py`) | **None.** Confirmed by fetching antigravity.google's own install, CLI reference, and troubleshooting pages directly: the only documented auth-related command is the in-session `/logout`; there is no shell-level `status`/`whoami`/`doctor` auth command. (A separate community wrapper, `agykit`, does add `agy status`/`whoami`/`doctor`-style commands, but that's third-party, not the official CLI — out of scope per "officially supported.") | Run bare `agy` — keyring/OAuth check happens on launch. |
| OpenCode | `opencode` | `shutil.which`, `opencode --version` (already used by `preflight.py`) | **Yes** — `opencode auth list` (alias `auth ls`) is documented (opencode.ai/docs/cli/) to list providers with stored credentials in `auth.json`, without printing the credentials themselves. | `opencode auth login [-p provider] [-m method]` (documented, interactive picker). |

Neither `claude` nor `agy` exposes subscription/quota/entitlement data non-interactively (Claude's
`/status` and Antigravity's `/credits` are both in-REPL only). OpenCode has no subscription
concept of its own — it's bring-your-own-provider-keys. So subscription status is always
`"unavailable"` in this package; the honest message from the requirements
(`"Authentication verified; subscription status cannot be confirmed."`) is shown whenever
`auth_state == AUTHENTICATED`.

## Status model

New module `orchestrator/provider_auth.py`, shaped like `preflight.py` (TypedDicts, pure
functions, no classes where a function suffices):

```python
AUTH_NOT_INSTALLED = "not_installed"
AUTH_NOT_AUTHENTICATED = "not_authenticated"
AUTH_EXPIRED = "auth_expired"                # defined for completeness; unreachable today —
                                              # no provider here documents a distinct "expired"
                                              # signal outside an interactive session
AUTH_AUTHENTICATED = "authenticated"
AUTH_UNVERIFIABLE = "auth_unverifiable"      # claude, agy: CLI is installed/runs, but no
                                              # documented non-interactive way exists to confirm
                                              # sign-in state
AUTH_PROVIDER_UNAVAILABLE = "provider_unavailable"  # a provider name outside {claude, opencode,
                                              # antigravity}
AUTH_CLI_ERROR = "cli_error"
AUTH_TIMED_OUT = "timed_out"

class ProviderAuthStatus(TypedDict, total=False):
    provider: str
    installed: bool
    executable: Optional[str]
    auth_state: str
    detail: str
    subscription_state: str       # always "unavailable" in this package
    subscription_detail: str
    checked_at: float
    duration_seconds: float
```

`AUTH_UNVERIFIABLE` is a deliberately distinct state from both `AUTH_AUTHENTICATED` and
`AUTH_NOT_AUTHENTICATED` — the UI must never present a guess as a fact. Its `detail` explains why
(no documented command) and points at the Login button to check by hand.

### Per-provider probe behavior

- **`check_claude_auth(timeout=15)`**: resolve `get_claude_executable_path()`; not found →
  `AUTH_NOT_INSTALLED`. Found → run `claude --version` via `launcher.run_bounded` (`shell=False`,
  `stdin=DEVNULL`, bounded timeout — the same call shape `preflight._probe_version` already uses).
  Success → `AUTH_UNVERIFIABLE` with an explanatory detail. `CLITimeoutError` → `AUTH_TIMED_OUT`.
  Any other failure to execute → `AUTH_CLI_ERROR`.
- **`check_antigravity_auth(timeout=15)`**: identical shape against `agy --version`.
- **`check_opencode_auth(timeout=15)`**: resolve `get_opencode_executable_path()`; not found →
  `AUTH_NOT_INSTALLED`. Found → run `opencode auth list`. No documented `--json` flag for this
  subcommand, so parsing is conservative and line-based: non-empty output naming at least one
  provider → `AUTH_AUTHENTICATED`, with the provider names (only) placed in `detail`; output that
  parses as "no providers configured" → `AUTH_NOT_AUTHENTICATED`; anything that doesn't match
  either recognizable shape → `AUTH_CLI_ERROR` (never guessed as authenticated). Timeout →
  `AUTH_TIMED_OUT`.
- **`redact(text)`**: applied to any stdout that reaches `detail`/logs — strips substrings
  matching common secret shapes (`sk-...`, `Bearer <token>`, `AKIA...`, long hex/base64 runs
  adjacent to "token"/"key"/"secret"). Defense-in-depth: none of the three commands above are
  documented to print a secret, but a status feature must not become the exception that proves
  the zero-credential-handling invariant wrong.
- **`open_provider_login(provider, timeout=300)`**: resolves the executable, then calls
  `orchestrator.launcher.run_agent_cli(cmd=[...], visible=True, timeout=300,
  close_on_completion=False, agent=provider, role="login", title="<Provider> Login")` — reusing
  the existing visible-terminal launch path instead of new spawn code. Commands:
  `[claude_exe]` bare, `[opencode_exe, "auth", "login"]`, `[agy_exe]` bare. Returns as soon as the
  terminal is launched; it does not wait for the human to finish signing in.
- **`check_all_providers(timeout=15)`**: runs all three and returns a list of
  `ProviderAuthStatus`.

## CLI surface

New flag `--check-providers` in `orchestrator/__main__.py`, parallel to `--doctor` (not merged
into it — `--doctor`'s existing contract is run-readiness for *configured* agents; provider
account-linking is a different question and mixing them would change `--doctor`'s existing
output shape and tests). Prints one block per provider using the same colorama-based renderer
style as `format_preflight_report`.

## Web UI

- New page `orchestrator/web/settings.html` (plain HTML + vanilla JS, no framework — matching
  `cockpit.html`/`office.html`/`design.html`), served at `/settings` and `/settings.html` from
  `daemon.py::do_GET` alongside the existing three page routes.
- New read route `GET /api/providers` → `Daemon.provider_status()` → `check_all_providers()` as
  JSON, gated by the existing `_authorised()` check like `/api/cockpit`.
- New control action `"provider_login"` added to `Daemon.control()`'s closed dispatch list →
  calls `open_provider_login(payload["provider"])`.
- One card per provider:

  ```text
  Claude Code
  Installed: Yes
  Authentication: Cannot be confirmed automatically
    (Claude Code exposes no documented non-interactive status check;
     click Login to verify in its own login flow.)
  Subscription: Not available
  [Login] [Check again]
  ```

  For OpenCode when authenticated:

  ```text
  OpenCode
  Installed: Yes
  Authentication: Verified (providers: anthropic, opencode)
  Subscription: Authentication verified; subscription status cannot be confirmed.
  [Login] [Check again]
  ```

- A small link from `cockpit.html`'s header to `/settings` for discoverability. There's no
  existing nav convention between `cockpit`/`office`/`design` today (each is opened by direct
  URL), so this is a minimal addition, not a rework of navigation.

## Testing

`tests/test_provider_auth.py`, `unittest.TestCase` shaped like `tests/test_package_c.py`, mocking
at the same seam `tests/support.py::FakeProcess` already patches. Cases per provider: not
installed, installed + responds, non-zero exit, `CLITimeoutError`, malformed/empty stdout; plus
OpenCode-specific authenticated-with-providers vs. no-providers-configured cases. A
daemon-level test hits `GET /api/providers` and the `provider_login` control action with the
subprocess layer mocked, confirming no real process is spawned in tests and no secret-shaped
string ever reaches a response body (the `redact` behavior, asserted directly).

## Docs

README.md gets a new subsection near "Prerequisites"/"Troubleshooting" covering: how to run
`--check-providers` and open `/settings`, what each status state means, why `claude` and `agy`
can't be verified non-interactively (with a one-line citation of the vendor docs), the restated
zero-credential-handling invariant for this specific feature, and basic troubleshooting (CLI not
found, timeout, "cannot be confirmed" is not an error).

## Risks / open items carried into implementation

- `opencode auth list`'s exact output format is not pinned down by a public sample; the parser
  must fail closed (`AUTH_CLI_ERROR`) on anything it doesn't recognize rather than guess
  `AUTHENTICATED`. Implementation should keep the parser small and the "recognized empty" and
  "recognized non-empty" cases narrow, adding to them only from an actually observed real output,
  not further speculation.
- If a future Claude Code or Antigravity CLI release adds an official non-interactive auth-status
  command, `AUTH_UNVERIFIABLE` for that provider should be revisited — this design deliberately
  does not fake one now.
