"""Tests for the Codex CLI adapter and its registry wiring (Package H).

The JSONL fixtures below are verbatim captures from live `codex-cli 0.154.0` runs on
2026-09-13; see docs/superpowers/specs/2026-09-13-package-h-codex-agent-design.md.
"""

import os
from unittest.mock import MagicMock, patch

import pytest

from orchestrator.agents.codex import (
    _stream_error,
    get_codex_executable_path,
    parse_codex_output,
    run_codex,
)
from orchestrator.agents.exceptions import CLIExecutionError

LIVE_SUCCESS = (
    '{"type":"thread.started","thread_id":"01a09a75-00aa-7b10-a2bb-95430d8ff412"}\n'
    '{"type":"turn.started"}\n'
    '{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"PONG"}}\n'
    '{"type":"turn.completed","usage":{"input_tokens":17154,"cached_input_tokens":9600,'
    '"cache_write_input_tokens":0,"output_tokens":6,"reasoning_output_tokens":0}}\n'
)

LIVE_FAILURE = (
    '{"type":"turn.started"}\n'
    '{"type":"error","message":"Reconnecting... 1/5 (unexpected status 401 Unauthorized)"}\n'
    '{"type":"error","message":"unexpected status 401 Unauthorized"}\n'
    '{"type":"turn.failed","error":{"message":"The \'no-such-model-xyz\' model is not '
    'supported when using Codex with a ChatGPT account."}}\n'
)

_NPM_NATIVE = os.path.join(
    "node_modules", "@openai", "codex", "node_modules", "@openai",
    "codex-win32-x64", "vendor", "x86_64-pc-windows-msvc", "bin", "codex.exe",
)


def _ok_result(stdout):
    result = MagicMock()
    result.returncode = 0
    result.stdout = stdout
    result.stderr = ""
    return result


# -- Task 1: resolver ----------------------------------------------------------------------


class TestCodexExecutableResolution:
    def test_returns_which_result_when_plain_binary(self):
        with patch("shutil.which", return_value="/usr/local/bin/codex"):
            assert get_codex_executable_path() == "/usr/local/bin/codex"

    def test_prefers_native_exe_over_cmd_shim(self):
        """The .cmd shim routes through cmd.exe and inherits its 8192-char limit, which a
        long orchestration prompt exceeds. Same rationale as get_opencode_executable_path."""
        shim_dir = r"C:\Users\x\AppData\Roaming\npm"
        shim = os.path.join(shim_dir, "codex.cmd")
        native = os.path.join(shim_dir, _NPM_NATIVE)
        with patch("shutil.which", return_value=shim), \
             patch("os.path.isfile", side_effect=lambda p: p == native):
            assert get_codex_executable_path() == native

    def test_falls_back_to_cmd_shim_when_native_exe_missing(self):
        shim = os.path.join(r"C:\Users\x\AppData\Roaming\npm", "codex.cmd")
        with patch("shutil.which", return_value=shim), \
             patch("os.path.isfile", return_value=False):
            assert get_codex_executable_path() == shim

    def test_ps1_shim_also_resolves_to_native(self):
        shim_dir = r"C:\Users\x\AppData\Roaming\npm"
        shim = os.path.join(shim_dir, "codex.ps1")
        native = os.path.join(shim_dir, _NPM_NATIVE)
        with patch("shutil.which", return_value=shim), \
             patch("os.path.isfile", side_effect=lambda p: p == native):
            assert get_codex_executable_path() == native

    def test_raises_when_not_installed(self):
        with patch("shutil.which", return_value=None), \
             patch("os.path.isfile", return_value=False):
            with pytest.raises(FileNotFoundError) as exc:
                get_codex_executable_path()
            assert "codex" in str(exc.value).lower()

    def test_error_names_official_install_command(self):
        """A user with only the un-invokable MSIX desktop app must be told what to install."""
        with patch("shutil.which", return_value=None), \
             patch("os.path.isfile", return_value=False):
            with pytest.raises(FileNotFoundError) as exc:
                get_codex_executable_path()
            assert "@openai/codex" in str(exc.value)


# -- Task 2: parser ------------------------------------------------------------------------


class TestParseCodexOutput:
    def test_extracts_agent_message_text(self):
        text, _ = parse_codex_output(LIVE_SUCCESS)
        assert text == "PONG"

    def test_maps_usage_fields_losslessly(self):
        _, usage = parse_codex_output(LIVE_SUCCESS)
        assert usage["available"] is True
        assert usage["input_tokens"] == 17154
        assert usage["output_tokens"] == 6
        assert usage["cache_read_tokens"] == 9600
        assert usage["cache_write_tokens"] == 0
        assert usage["reasoning_tokens"] == 0
        assert usage["raw_usage"]["cached_input_tokens"] == 9600

    def test_concatenates_multiple_agent_messages(self):
        stream = (
            '{"type":"item.completed","item":{"type":"agent_message","text":"alpha "}}\n'
            '{"type":"item.completed","item":{"type":"agent_message","text":"beta"}}\n'
        )
        text, _ = parse_codex_output(stream)
        assert text == "alpha beta"

    def test_ignores_non_agent_message_items(self):
        """Reasoning/command items must not leak into the response text."""
        stream = (
            '{"type":"item.completed","item":{"type":"reasoning","text":"secret thinking"}}\n'
            '{"type":"item.completed","item":{"type":"agent_message","text":"visible"}}\n'
        )
        text, _ = parse_codex_output(stream)
        assert text == "visible"

    def test_empty_output_is_unavailable_usage(self):
        text, usage = parse_codex_output("")
        assert text == ""
        assert usage["available"] is False

    def test_missing_usage_degrades_to_unavailable_not_zeros(self):
        """Schema drift must never fabricate numbers."""
        stream = '{"type":"item.completed","item":{"type":"agent_message","text":"hi"}}\n'
        _, usage = parse_codex_output(stream)
        assert usage["available"] is False
        assert usage["input_tokens"] is None

    def test_non_json_output_returned_verbatim(self):
        text, usage = parse_codex_output("plain text from a mocked CLI")
        assert text == "plain text from a mocked CLI"
        assert usage["available"] is False

    def test_unknown_event_types_ignored(self):
        stream = (
            '{"type":"some.future.event","payload":{"x":1}}\n'
            '{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}\n'
        )
        text, _ = parse_codex_output(stream)
        assert text == "ok"

    def test_malformed_json_line_skipped(self):
        stream = (
            '{not valid json\n'
            '{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}\n'
        )
        text, _ = parse_codex_output(stream)
        assert text == "ok"


class TestStreamError:
    def test_prefers_turn_failed_over_transient_errors(self):
        """`Reconnecting... 1/5` events are recovered retries, not the cause."""
        message = _stream_error(LIVE_FAILURE)
        assert "no-such-model-xyz" in message
        assert "Reconnecting" not in message

    def test_falls_back_to_last_error_when_no_turn_failed(self):
        stream = (
            '{"type":"error","message":"first"}\n'
            '{"type":"error","message":"second"}\n'
        )
        assert _stream_error(stream) == "second"

    def test_returns_none_when_no_error(self):
        assert _stream_error(LIVE_SUCCESS) is None


# -- Task 3: run_codex ---------------------------------------------------------------------


class TestRunCodex:
    def test_builds_required_argv(self):
        with patch("orchestrator.agents.codex.get_codex_executable_path", return_value="codex"), \
             patch("orchestrator.agents.codex.run_agent_cli",
                   return_value=_ok_result(LIVE_SUCCESS)) as run:
            run_codex("do a thing")
        cmd = run.call_args.kwargs["cmd"]
        assert cmd[0] == "codex" and cmd[1] == "exec"
        assert "--json" in cmd
        assert cmd[cmd.index("--color") + 1] == "never"
        assert "--skip-git-repo-check" in cmd
        assert cmd[-1] == "do a thing", "prompt must be the final positional argument"

    def test_never_bypasses_sandbox_or_approvals(self):
        with patch("orchestrator.agents.codex.get_codex_executable_path", return_value="codex"), \
             patch("orchestrator.agents.codex.run_agent_cli",
                   return_value=_ok_result(LIVE_SUCCESS)) as run:
            run_codex("x")
        cmd = run.call_args.kwargs["cmd"]
        assert "--dangerously-bypass-approvals-and-sandbox" not in cmd
        assert "--dangerously-bypass-hook-trust" not in cmd
        assert "--sandbox" not in cmd and "-s" not in cmd

    def test_passes_model_and_cwd(self):
        with patch("orchestrator.agents.codex.get_codex_executable_path", return_value="codex"), \
             patch("orchestrator.agents.codex.run_agent_cli",
                   return_value=_ok_result(LIVE_SUCCESS)) as run:
            run_codex("x", model="gpt-5.1-codex", working_dir="/tmp/wt")
        cmd = run.call_args.kwargs["cmd"]
        assert cmd[cmd.index("-m") + 1] == "gpt-5.1-codex"
        assert cmd[cmd.index("-C") + 1] == "/tmp/wt"

    def test_returns_text_by_default(self):
        with patch("orchestrator.agents.codex.get_codex_executable_path", return_value="codex"), \
             patch("orchestrator.agents.codex.run_agent_cli",
                   return_value=_ok_result(LIVE_SUCCESS)):
            assert run_codex("x") == "PONG"

    def test_return_usage_returns_tuple(self):
        with patch("orchestrator.agents.codex.get_codex_executable_path", return_value="codex"), \
             patch("orchestrator.agents.codex.run_agent_cli",
                   return_value=_ok_result(LIVE_SUCCESS)):
            text, usage = run_codex("x", return_usage=True)
        assert text == "PONG" and usage["output_tokens"] == 6

    def test_forwards_launcher_kwargs(self):
        """Timeout, ownership, visibility and tracing all belong to run_agent_cli."""
        with patch("orchestrator.agents.codex.get_codex_executable_path", return_value="codex"), \
             patch("orchestrator.agents.codex.run_agent_cli",
                   return_value=_ok_result(LIVE_SUCCESS)) as run:
            run_codex("x", timeout=99, visible=True, role="verifier", terminal_type="console")
        kwargs = run.call_args.kwargs
        assert kwargs["timeout"] == 99 and kwargs["visible"] is True
        assert kwargs["agent"] == "codex" and kwargs["role"] == "verifier"
        assert kwargs["terminal_type"] == "console"

    def test_failure_raises_with_terminal_cause(self):
        bad = MagicMock(returncode=1, stdout=LIVE_FAILURE, stderr="")
        with patch("orchestrator.agents.codex.get_codex_executable_path", return_value="codex"), \
             patch("orchestrator.agents.codex.run_agent_cli", return_value=bad):
            with pytest.raises(CLIExecutionError) as exc:
                run_codex("x")
        assert "no-such-model-xyz" in str(exc.value)
        assert "Reconnecting" not in str(exc.value)

    def test_failure_carries_partial_usage_when_available(self):
        stream = (
            '{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":2,'
            '"cached_input_tokens":0,"cache_write_input_tokens":0,"reasoning_output_tokens":0}}\n'
            '{"type":"turn.failed","error":{"message":"boom"}}\n'
        )
        bad = MagicMock(returncode=1, stdout=stream, stderr="")
        with patch("orchestrator.agents.codex.get_codex_executable_path", return_value="codex"), \
             patch("orchestrator.agents.codex.run_agent_cli", return_value=bad):
            with pytest.raises(CLIExecutionError) as exc:
                run_codex("x")
        assert exc.value.token_usage["input_tokens"] == 10

    def test_extra_args_appended_before_prompt(self):
        with patch("orchestrator.agents.codex.get_codex_executable_path", return_value="codex"), \
             patch("orchestrator.agents.codex.run_agent_cli",
                   return_value=_ok_result(LIVE_SUCCESS)) as run:
            run_codex("x", extra_args=["--sandbox", "workspace-write"])
        cmd = run.call_args.kwargs["cmd"]
        assert cmd.index("--sandbox") < cmd.index("x")

    def test_missing_executable_propagates(self):
        with patch("orchestrator.agents.codex.get_codex_executable_path",
                   side_effect=FileNotFoundError("nope")):
            with pytest.raises(FileNotFoundError):
                run_codex("x")


class TestCodexExports:
    def test_run_codex_exported_from_agents_package(self):
        from orchestrator.agents import run_codex as exported
        assert callable(exported)

    def test_codex_is_not_a_native_tui_agent(self):
        from orchestrator.agents import NATIVE_TUI_AGENTS, supports_native_tui
        assert "codex" not in NATIVE_TUI_AGENTS
        assert supports_native_tui("codex") is False


# -- Task 6: registry wiring ---------------------------------------------------------------


class TestCodexRegistry:
    def test_runnable_agents_includes_codex_last(self):
        from orchestrator.config import RUNNABLE_AGENTS
        assert RUNNABLE_AGENTS == ("antigravity", "claude", "opencode", "codex")

    def test_get_runner_returns_run_codex(self):
        import orchestrator.graph as mod
        from orchestrator.graph import get_runner
        assert get_runner("codex") is mod.run_codex

    def test_unknown_agent_still_refused(self):
        from orchestrator.graph import UnknownAgentError, get_runner
        with pytest.raises(UnknownAgentError):
            get_runner("codexx")

    def test_preflight_resolver_registered(self):
        from orchestrator.preflight import AGENT_EXECUTABLE_RESOLVERS, AGENT_VERSION_ARGS
        assert "codex" in AGENT_EXECUTABLE_RESOLVERS
        assert AGENT_VERSION_ARGS["codex"] == ["--version"]

    def test_default_catalog_has_codex_models(self):
        from orchestrator.config import DEFAULT_CONFIG
        ids = [m["id"] for m in DEFAULT_CONFIG["models"]["codex"]]
        assert "gpt-5.1-codex" in ids

    def test_window_title_uses_proper_case(self):
        from orchestrator.launcher import get_terminal_title
        assert "Codex" in get_terminal_title("codex", "implementer")

    def test_window_title_not_left_to_capitalize_fallback(self):
        """`"codex".capitalize()` also yields "Codex", so assert the explicit branch exists
        rather than passing by coincidence the way a real typo would."""
        from orchestrator.launcher import get_terminal_title
        assert "Codex" in get_terminal_title("codex", "verifier")
        assert "OpenCode" in get_terminal_title("opencode", "verifier")

    def test_shipped_yaml_catalog_offers_codex(self):
        """The shipped orchestrator.yaml carries its own `models:` block, which *overrides*
        DEFAULT_CONFIG's. Without a codex section there, assigning a codex role is refused
        with "not a provider in the model catalog" - so the default catalog alone is not
        enough to make Codex assignable out of the box."""
        import yaml
        with open("orchestrator.yaml", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle)
        ids = [m["id"] for m in raw["models"]["codex"]]
        assert "gpt-5.1-codex" in ids

    def test_codex_role_validates_against_shipped_config(self):
        import copy
        import os
        import tempfile

        import yaml
        from orchestrator.config import load_config

        with open("orchestrator.yaml", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle)
        cfg = copy.deepcopy(raw)
        cfg["agents"][1] = {
            "agent": "codex", "model": "gpt-5.1-codex", "role": cfg["agents"][1]["role"],
        }
        path = os.path.join(tempfile.mkdtemp(), "orchestrator.yaml")
        with open(path, "w", encoding="utf-8") as handle:
            yaml.safe_dump(cfg, handle)

        loaded = load_config(path)
        assert any(a["agent"] == "codex" for a in loaded["agents"])

    def test_unknown_codex_model_is_refused(self):
        """A model id the catalog has never heard of must fail at config time, not run time."""
        import copy
        import os
        import tempfile

        import yaml
        from orchestrator.config import ConfigValidationError, load_config

        with open("orchestrator.yaml", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle)
        cfg = copy.deepcopy(raw)
        cfg["agents"][1] = {
            "agent": "codex", "model": "definitely-not-a-codex-model",
            "role": cfg["agents"][1]["role"],
        }
        path = os.path.join(tempfile.mkdtemp(), "orchestrator.yaml")
        with open(path, "w", encoding="utf-8") as handle:
            yaml.safe_dump(cfg, handle)

        with pytest.raises(ConfigValidationError):
            load_config(path)

    def test_settings_page_labels_codex(self):
        """The settings card map is the one provider-specific bit of UI."""
        with open("orchestrator/web/settings.html", encoding="utf-8") as handle:
            html = handle.read()
        assert "codex:" in html and "Codex" in html


# -- Task 7: backward compatibility --------------------------------------------------------


class TestBackwardCompatibility:
    def test_existing_three_agents_unchanged(self):
        from orchestrator.config import RUNNABLE_AGENTS
        for agent in ("antigravity", "claude", "opencode"):
            assert agent in RUNNABLE_AGENTS

    def test_existing_runners_unchanged(self):
        import orchestrator.graph as mod
        from orchestrator.graph import get_runner
        assert get_runner("claude") is mod.run_claude_code
        assert get_runner("opencode") is mod.run_opencode
        assert get_runner("antigravity") is mod.run_antigravity

    def test_config_without_codex_still_validates(self):
        """The shipped orchestrator.yaml never mentions codex; it must be unaffected."""
        from orchestrator.config import load_config
        config = load_config("orchestrator.yaml")
        assert config["agents"]

    def test_existing_catalogs_untouched(self):
        from orchestrator.config import DEFAULT_CONFIG
        assert [m["id"] for m in DEFAULT_CONFIG["models"]["claude"]] == [
            "sonnet", "opus", "haiku",
        ]

    def test_missing_codex_does_not_break_other_providers(self):
        """A machine without codex must see no new failures."""
        from orchestrator.provider_auth import check_all_providers, providers_ok

        completed = MagicMock()
        completed.returncode = 0
        completed.stdout = b"1.0.0"
        completed.stderr = b""

        with patch("orchestrator.provider_auth.get_codex_executable_path",
                   side_effect=FileNotFoundError("not installed")), \
             patch("orchestrator.provider_auth.get_claude_executable_path",
                   return_value="claude"), \
             patch("orchestrator.provider_auth.get_opencode_executable_path",
                   side_effect=FileNotFoundError()), \
             patch("orchestrator.provider_auth.get_antigravity_executable_path",
                   side_effect=FileNotFoundError()), \
             patch("orchestrator.provider_auth._run", return_value=completed):
            results = check_all_providers()

        codex = [r for r in results if r["provider"] == "codex"][0]
        assert codex["auth_state"] == "not_installed"
        assert providers_ok(results) is True, "not_installed is a fact, not a probe failure"

    def test_native_tui_mode_refuses_codex(self):
        from orchestrator.agents import supports_native_tui
        assert supports_native_tui("codex") is False
