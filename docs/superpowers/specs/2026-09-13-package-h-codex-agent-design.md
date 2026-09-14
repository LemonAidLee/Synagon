# Package H — Codex/ChatGPT as a Fourth Synagon Agent (Design)

Status: implemented 2026-09-13
Date: 2026-09-13

## Problem

Synagon runs three local CLI providers — Claude Code (`claude`), Antigravity (`agy`), and
OpenCode (`opencode`). OpenAI's Codex CLI is a fourth local, subscription-backed coding CLI of
exactly the same shape: a locally installed binary, authenticated by the vendor's own account
system, drivable non-interactively. It is absent from the agent registry, the model catalog,
preflight, the provider-account settings page, and the orchestration pipeline.

This package adds `codex` as a first-class fourth agent using **only** the officially supported
local CLI, preserving every existing security invariant.

## Non-goals

- No OpenAI API keys, no `OPENAI_API_KEY` handling, no direct HTTP to `api.openai.com`.
- No reading, parsing, copying, or existence-testing of `~/.codex/auth.json` as an auth signal.
- No OAuth token extraction, browser scraping, or pty/ANSI screen-scraping.
- No subscription/entitlement claim — the CLI exposes no reliable official signal (see below).
- No change to the three existing providers' behavior or contracts.
- No native-TUI integration for Codex (it has no `serve`+`attach` equivalent; see Risks).

## Researched ground truth (2026-09-13)

Unlike Package G, whose table was compiled from vendor documentation, **every row below was
verified by executing the real binary on this machine** and capturing exact bytes, streams, and
exit codes. Version under test: `codex-cli 0.154.0` (`@openai/codex`, npm).

### Installation shape

| Fact | Verified value |
|---|---|
| npm package | `@openai/codex` (official OpenAI package), v0.154.0 |
| PATH shim (Windows) | `%APPDATA%\npm\codex.ps1` / `codex.cmd` |
| Native binary | `%APPDATA%\npm\node_modules\@openai\codex\node_modules\@openai\codex-win32-x64\vendor\x86_64-pc-windows-msvc\bin\codex.exe` |
| `codex --version` | stdout `codex-cli 0.154.0`, exit 0 |

The npm-shim-plus-native-`.exe` layout is **the same shape OpenCode already has**, and
`get_opencode_executable_path` already encodes the reason to prefer the native `.exe`: the
Windows `.cmd` wrapper routes through `cmd.exe` and inherits its 8192-character command-line
limit, which a long orchestration prompt will exceed. Codex's resolver mirrors that logic.

#### A note on the Codex desktop app (why it is not the integration target)

This machine also has the Codex/ChatGPT desktop app installed as an MSIX package
(`OpenAI.Codex_26.908.4834.0_x64__2p2nqsd0c76g0`), which bundles its own `codex.exe` under
`C:\Program Files\WindowsApps\...\app\resources\`. That binary is **deliberately not used**:

- Executing it directly returns `Access is denied` — `WindowsApps` ACLs restrict MSIX payloads
  to the package identity, not arbitrary callers.
- It publishes **no app execution alias**, so there is no supported `codex` entry point from it.
- Its install path is version-stamped and changes on every app update.

Depending on it would mean either an ACL workaround or a hard-coded volatile path. Neither is a
supported integration. The resolver therefore looks only for the standalone CLI, and if only the
desktop app is present, Synagon honestly reports `not_installed` with a message naming the
official install command.

### Command contracts (measured)

| Capability | Command | Exit | stdout | stderr |
|---|---|---|---|---|
| Version | `codex --version` | 0 | `codex-cli 0.154.0` | — |
| Auth status (signed in) | `codex login status` | **0** | *(empty)* | `Logged in using ChatGPT` |
| Auth status (signed out) | `codex login status` | **1** | *(empty)* | `Not logged in` |
| Login | `codex login` | — | interactive browser/device flow | — |
| Non-interactive run | `codex exec --json --color never …` | 0 / 1 | JSONL event stream | human logs |

Three measured facts drive the implementation and are easy to get wrong:

1. **`codex login status` writes its answer to stderr, not stdout.** stdout is empty (0 bytes).
   The existing `provider_auth` probes already concatenate stdout+stderr, so this works — but the
   parser must not assume stdout.
2. **Signed-out is exit 1, not exit 0.** A non-zero exit here is a *legitimate answer*, not a CLI
   error. Treating non-zero as `cli_error` (the way `check_opencode_auth` correctly does for
   `auth list`) would misreport every signed-out user. Codex's probe classifies on the **message
   text first** and falls back to `cli_error` only when neither known phrase appears.
3. **Extra noise lines occur.** With a non-default `CODEX_HOME`, output was
   `WARNING: proceeding, even though we could not create PATH aliases: …\nNot logged in`. The
   parser therefore searches for a signal phrase across all lines rather than reading line 1.

Output contains **no ANSI escape sequences** for `login status` (verified by byte inspection).
The `exec` path is nevertheless invoked with `--color never`, because emitting ANSI into a parsed
stream is precisely the defect fixed in `ed45916` for OpenCode.

### `codex exec --json` event contract (measured, live run)

A live authenticated run of
`codex exec --json --color never -s read-only --skip-git-repo-check "<prompt>"` produced exactly:

```jsonl
{"type":"thread.started","thread_id":"01a09a75-00aa-7b10-a2bb-95430d8ff412"}
{"type":"turn.started"}
{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"PONG"}}
{"type":"turn.completed","usage":{"input_tokens":17154,"cached_input_tokens":9600,"cache_write_input_tokens":0,"output_tokens":6,"reasoning_output_tokens":0}}
```

- **Response text** = concatenation of `item.completed` items whose `item.type` is
  `agent_message`, taking `item.text`.
- **Token usage** = the `usage` object on `turn.completed`.

A failing run (measured twice — signed-out, and an unsupported `--model`) produced:

```jsonl
{"type":"error","message":"Reconnecting... 2/5 (unexpected status 401 …)"}
{"type":"error","message":"…"}
{"type":"turn.failed","error":{"message":"The 'no-such-model-xyz' model is not supported when using Codex with a ChatGPT account."}}
```

with exit 1. Note that `error` events are emitted for **transient, recovered** retries
(`Reconnecting... 1/5`), so the last `error` event is *not* a reliable failure cause. The
authoritative terminal error is `turn.failed.error.message`. Codex's parser reads that, and only
falls back to the last `error` event when no `turn.failed` is present.

### Token usage mapping

`turn.completed.usage` maps losslessly onto the existing `TokenUsage` TypedDict:

| Codex field | `TokenUsage` field |
|---|---|
| `input_tokens` | `input_tokens` |
| `output_tokens` | `output_tokens` |
| `cached_input_tokens` | `cache_read_tokens` |
| `cache_write_input_tokens` | `cache_write_tokens` |
| `reasoning_output_tokens` | `reasoning_tokens` |

The whole `usage` dict is preserved in `raw_usage`. `total_tokens` is left to
`create_token_usage`'s existing input+output derivation, matching every other adapter.

### Subscription status — unavailable, deliberately

`codex login status` reports the **auth mode** (`Logged in using ChatGPT` vs. an API-key mode),
which distinguishes *how* the user authenticated. It does **not** report plan tier, quota,
credits, or entitlement, and no other non-interactive Codex subcommand does either.

Per requirement, subscription is therefore always `SUBSCRIPTION_UNAVAILABLE`, carrying the
existing verbatim message `"Authentication verified; subscription status cannot be confirmed."`
whenever `auth_state == AUTH_AUTHENTICATED`.

Auth *mode* is reported, because it is officially and reliably emitted — but it is stated as a
mode, never as a subscription claim. `Logged in using ChatGPT` means a ChatGPT account was used;
it does not mean a paid plan is active, and the detail text must not imply that.

## Status model

No new states. Codex reuses `provider_auth.py`'s existing vocabulary, and — unlike `claude` and
`antigravity` — it can reach real `AUTH_AUTHENTICATED` / `AUTH_NOT_AUTHENTICATED` verdicts,
because it has an official non-interactive signal. It becomes the **second** provider (with
`opencode`) that never needs `AUTH_UNVERIFIABLE`.

| Measured condition | `auth_state` |
|---|---|
| resolver raises `FileNotFoundError` | `AUTH_NOT_INSTALLED` |
| stderr matches `Logged in …` | `AUTH_AUTHENTICATED` |
| stderr matches `Not logged in` | `AUTH_NOT_AUTHENTICATED` |
| neither phrase, any exit code | `AUTH_CLI_ERROR` |
| probe exceeds timeout | `AUTH_TIMED_OUT` |

`PROVIDERS` becomes `("claude", "opencode", "antigravity", "codex")`. Codex is appended rather
than inserted so existing positional expectations in tests and UI ordering do not shift.

## Registry and configuration surface

| Surface | File | Change |
|---|---|---|
| Runnable agents | `orchestrator/config.py:433` | `RUNNABLE_AGENTS += ("codex",)` |
| Model catalog | `orchestrator/config.py` `DEFAULT_CONFIG["models"]` | new `"codex"` list |
| Runner dispatch | `orchestrator/graph.py:485` `get_runner` | `if agent_name == "codex": return mod.run_codex` |
| Executable resolver | `orchestrator/preflight.py:47` | `"codex": get_codex_executable_path` |
| Version probe args | `orchestrator/preflight.py:55` | `"codex": ["--version"]` |
| Window title | `orchestrator/launcher.py:210` | `elif agent_clean == "codex": "Codex"` |
| Auth probe | `orchestrator/provider_auth.py` | `check_codex_auth`, `_CHECKERS`, `PROVIDERS`, `_LOGIN_ARGS`, `_VERSION_ARGS`, `_resolve_executable` |
| Adapter | `orchestrator/agents/codex.py` *(new)* | `run_codex`, `run_codex_with_usage`, `parse_codex_output`, `get_codex_executable_path`, `build_repair_prompt` |
| Package exports | `orchestrator/agents/__init__.py` | export `run_codex` |

Model catalog seed (ids are Codex `--model` values; the catalog is user-editable and validated
against, so it constrains configuration only, and an unknown id fails at config-validation time
rather than at run time):

```yaml
codex:
  - id: gpt-5.1-codex        # Codex CLI default family
  - id: gpt-5.1-codex-mini
```

## Login flow

`codex login` opens the vendor's own browser/device authorization flow. It is long-lived and
interactive, so it reuses `_spawn_detached_terminal` exactly as the three existing providers do —
launched detached, never waited on, never killed, never owned by the orchestrator's process-job
machinery. `_LOGIN_ARGS["codex"] = ["login"]`.

Synagon never sees, stores, or transmits whatever credential that flow writes.

## Orchestration pipeline

`run_codex` follows the headless branch of `run_opencode_with_usage` and reuses `run_agent_cli`
verbatim, inheriting timeout enforcement, process-tree ownership/kill, visible-terminal hosting,
terminal-type selection, and tracer integration with no new process machinery.

Fixed argv:

```
codex exec --json --color never --skip-git-repo-check [-m MODEL] [-C CWD] <extra_args> <prompt>
```

- `--json` — the parsed JSONL stream.
- `--color never` — no ANSI into a parsed stream (the `ed45916` lesson).
- `--skip-git-repo-check` — worktrees and scratch dirs are not always git repos; without this
  Codex refuses to start there.
- `--sandbox` is **not** set by Synagon. Codex's own default sandbox applies, and
  `--dangerously-bypass-approvals-and-sandbox` is never emitted. A user who wants a different
  policy passes it through `extra_args`, an explicit and auditable choice.

`stdin` is `DEVNULL`. This is not incidental: a measured run with an inherited stdin **hung until
timeout**, printing `Reading additional input from stdin...`, because `codex exec` reads a prompt
from stdin when one is piped. `run_agent_cli`'s existing stdin handling is verified against this
in Task 4.

Codex is **not** added to `NATIVE_TUI_AGENTS`. It has no documented client/server
`serve`+`attach` equivalent to OpenCode's, so `agent_execution_mode: native_tui` must keep
refusing it, exactly as it refuses `claude` and `antigravity`.

## Security invariants (unchanged, re-verified)

1. **Zero credential handling.** Codex's probe runs one documented command and reads its
   human-readable status sentence. `~/.codex/auth.json` is never opened. No `OPENAI_API_KEY` is
   read, set, forwarded, or logged; Synagon never adds it to a child environment.
2. **Redaction.** Every `detail` string passes through the existing `redact()`. Codex error
   messages carry `request id` and `cf-ray` correlators (measured) — not secrets, but the
   existing `sk-…`/bearer patterns still apply as defense-in-depth, and `redact()` is applied to
   adapter error details too.
3. **`shell=False`, explicit timeout, `stdin=DEVNULL`** on every probe, via the existing `_run`.
4. **No new network calls from Synagon.** Only the vendor CLI talks to the vendor.
5. **Detached login is unowned** — not registered with the process-job machinery, never killed.
6. **Truthful reporting.** No subscription claim; `not_installed` when only the un-invokable
   desktop app is present.

## Risks

| Risk | Mitigation |
|---|---|
| Status phrasing changes in a future CLI version | Match on stable lowercase substrings (`"logged in"`, `"not logged in"`) checked most-specific-first; anything unrecognised is `cli_error`, never a guessed verdict. Fail closed, as Package G established. |
| `--json` event schema drift | Parser ignores unknown event types and unknown item types; absent usage yields `unavailable_token_usage()` rather than zeros, so drift degrades to "usage unavailable", never to fabricated numbers. |
| Transient `error` events misread as failure | Terminal cause is taken from `turn.failed`, with last-`error` only as fallback. |
| Windows `.cmd` 8192-char prompt truncation | Resolver prefers the native `.exe`, mirroring OpenCode's documented rationale. |
| Codex not installed | Every surface degrades to `not_installed`; the three existing providers are untouched, and a config naming `codex` fails preflight with a clear message rather than silently running another agent. |

## Backward compatibility

Codex is **additive only**. `RUNNABLE_AGENTS` and `PROVIDERS` gain a trailing element; no
existing tuple position, function signature, config file, or stored run record changes. An
`orchestrator.yaml` that never mentions `codex` behaves identically, and a machine without the
Codex CLI sees no new failures — verified explicitly in Task 7.
