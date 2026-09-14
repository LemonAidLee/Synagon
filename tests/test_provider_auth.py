"""Package G - provider account-linking status checks.

Every probe here is read-only and safety-checked the same way `preflight.py`'s deep-mode
version probe is: `shell=False`, a detached stdin, an explicit timeout. None of it ever reads
a credential file or a keyring entry - see `provider_auth.redact` and the module docstring for
why, and the design spec (`docs/superpowers/specs/2026-09-12-package-g-provider-account-linking-design.md`)
for the vendor-doc research behind each provider's probe.
"""

import subprocess
import unittest
from unittest.mock import patch

from orchestrator.provider_auth import (
    AUTH_AUTHENTICATED,
    AUTH_CLI_ERROR,
    AUTH_EXPIRED,
    AUTH_NOT_AUTHENTICATED,
    AUTH_NOT_INSTALLED,
    AUTH_PROVIDER_UNAVAILABLE,
    AUTH_TIMED_OUT,
    AUTH_UNVERIFIABLE,
    PROVIDERS,
    SUBSCRIPTION_CONFIRMED_DETAIL,
    _parse_opencode_auth_list,
    _spawn_detached_terminal,
    check_all_providers,
    check_antigravity_auth,
    check_claude_auth,
    check_opencode_auth,
    format_provider_report,
    open_provider_login,
    providers_ok,
    redact,
)


class TestAuthStatesAreDistinct(unittest.TestCase):
    def test_every_state_is_its_own_string(self):
        # AUTH_EXPIRED is asserted here even though no provider probe reaches it today (see its
        # definition's comment): none of claude/opencode/antigravity documents a non-interactive
        # "credential present but expired" signal, distinct from "not authenticated" or
        # "unverifiable". The constant exists so a future probe that adds one doesn't need a new
        # name; this test is what keeps it a real, distinct value in the meantime.
        states = [
            AUTH_NOT_INSTALLED, AUTH_NOT_AUTHENTICATED, AUTH_EXPIRED, AUTH_AUTHENTICATED,
            AUTH_UNVERIFIABLE, AUTH_PROVIDER_UNAVAILABLE, AUTH_CLI_ERROR, AUTH_TIMED_OUT,
        ]
        self.assertEqual(len(states), len(set(states)))


class _Completed:
    def __init__(self, returncode=0, stdout=b"", stderr=b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class TestRedact(unittest.TestCase):
    def test_redacts_anthropic_style_keys(self):
        self.assertEqual(redact("key is sk-ant-api03-abcdefghijklmnop"), "key is [REDACTED]")

    def test_redacts_bearer_tokens(self):
        self.assertEqual(redact("Authorization: Bearer abcdefghij123456"), "Authorization: [REDACTED]")

    def test_leaves_ordinary_text_alone(self):
        self.assertEqual(redact("providers: anthropic, opencode"), "providers: anthropic, opencode")

    def test_handles_empty_string(self):
        self.assertEqual(redact(""), "")


class TestClaudeVersionOnlyProbe(unittest.TestCase):
    def test_not_installed(self):
        with patch(
            "orchestrator.provider_auth.get_claude_executable_path",
            side_effect=FileNotFoundError("no claude"),
        ):
            status = check_claude_auth()
        self.assertFalse(status["installed"])
        self.assertEqual(status["auth_state"], AUTH_NOT_INSTALLED)
        self.assertIsNone(status["executable"])

    def test_installed_and_responsive_is_unverifiable_not_a_guess(self):
        with patch(
            "orchestrator.provider_auth.get_claude_executable_path",
            return_value="C:/bin/claude.exe",
        ), patch(
            "orchestrator.provider_auth.subprocess.run",
            return_value=_Completed(returncode=0, stdout=b"2.1.267 (Claude Code)", stderr=b""),
        ):
            status = check_claude_auth()
        self.assertTrue(status["installed"])
        self.assertEqual(status["executable"], "C:/bin/claude.exe")
        self.assertEqual(status["auth_state"], AUTH_UNVERIFIABLE)
        self.assertIn("no documented non-interactive", status["detail"])

    def test_nonzero_exit_with_no_output_is_a_cli_error(self):
        with patch(
            "orchestrator.provider_auth.get_claude_executable_path",
            return_value="C:/bin/claude.exe",
        ), patch(
            "orchestrator.provider_auth.subprocess.run",
            return_value=_Completed(returncode=1, stdout=b"", stderr=b""),
        ):
            status = check_claude_auth()
        self.assertEqual(status["auth_state"], AUTH_CLI_ERROR)

    def test_timeout(self):
        with patch(
            "orchestrator.provider_auth.get_claude_executable_path",
            return_value="C:/bin/claude.exe",
        ), patch(
            "orchestrator.provider_auth.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=15),
        ):
            status = check_claude_auth(timeout=15)
        self.assertEqual(status["auth_state"], AUTH_TIMED_OUT)

    def test_probe_uses_safe_subprocess_flags(self):
        with patch(
            "orchestrator.provider_auth.get_claude_executable_path",
            return_value="C:/bin/claude.exe",
        ), patch(
            "orchestrator.provider_auth.subprocess.run",
            return_value=_Completed(returncode=0, stdout=b"1.0.0", stderr=b""),
        ) as mock_run:
            check_claude_auth(timeout=7)
        args, kwargs = mock_run.call_args
        self.assertEqual(args[0], ["C:/bin/claude.exe", "--version"])
        self.assertFalse(kwargs["shell"])
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(kwargs["timeout"], 7)


class TestAntigravityVersionOnlyProbe(unittest.TestCase):
    def test_not_installed(self):
        with patch(
            "orchestrator.provider_auth.get_antigravity_executable_path",
            side_effect=FileNotFoundError("no agy"),
        ):
            status = check_antigravity_auth()
        self.assertEqual(status["auth_state"], AUTH_NOT_INSTALLED)

    def test_installed_and_responsive_is_unverifiable(self):
        with patch(
            "orchestrator.provider_auth.get_antigravity_executable_path",
            return_value="C:/bin/agy.exe",
        ), patch(
            "orchestrator.provider_auth.subprocess.run",
            return_value=_Completed(returncode=0, stdout=b"agy 1.4.0", stderr=b""),
        ):
            status = check_antigravity_auth()
        self.assertEqual(status["auth_state"], AUTH_UNVERIFIABLE)
        self.assertEqual(status["provider"], "antigravity")


class TestParseOpencodeAuthList(unittest.TestCase):
    def test_multiple_providers(self):
        self.assertEqual(
            _parse_opencode_auth_list("anthropic\nopencode\n"), ["anthropic", "opencode"]
        )

    def test_single_provider_with_extra_columns(self):
        self.assertEqual(_parse_opencode_auth_list("anthropic   oauth\n"), ["anthropic"])

    def test_empty_output_is_zero_providers_not_unparseable(self):
        self.assertEqual(_parse_opencode_auth_list(""), [])

    def test_explicit_no_providers_message_is_zero_providers(self):
        self.assertEqual(_parse_opencode_auth_list("No providers configured.\n"), [])

    def test_header_only_output_is_unparseable(self):
        self.assertIsNone(_parse_opencode_auth_list("PROVIDER   METHOD\n"))

    def test_real_opencode_1_18_29_box_drawn_zero_credentials_output_is_zero_providers(self):
        # Exact bytes captured from a real `opencode auth list` run (opencode 1.18.29, Windows)
        # against a machine with zero stored credentials. Before the fix this was misparsed as
        # three "providers" named after the ANSI-wrapped box-drawing characters themselves
        # (`\x1b[90m┌\x1b[39m`, `\x1b[90m│\x1b[39m`, `\x1b[90m└\x1b[39m`), fabricating
        # AUTH_AUTHENTICATED on a machine that is not authenticated at all.
        stdout = (
            b"\x1b[90m\xe2\x94\x8c\x1b[39m  Credentials \x1b[90m~\\.local\\share\\opencode\\auth.json\n"
            b"\x1b[90m\xe2\x94\x82\x1b[39m\n"
            b"\x1b[90m\xe2\x94\x94\x1b[39m  0 credentials\n\n"
        )
        stderr = b"\x1b[0m\r\n"
        text = stdout.decode("utf-8") + stderr.decode("utf-8")
        self.assertEqual(_parse_opencode_auth_list(text), [])

    def test_ansi_escape_codes_are_stripped_before_parsing(self):
        self.assertEqual(
            _parse_opencode_auth_list("\x1b[32manthropic\x1b[0m\n\x1b[32mopencode\x1b[0m\n"),
            ["anthropic", "opencode"],
        )

    def test_box_drawing_decoration_lines_are_skipped_even_with_real_providers_present(self):
        # Future-proofing: a box-drawn UI that reports a NON-zero count must not let the
        # decoration characters themselves be parsed as provider names alongside real ones.
        self.assertEqual(
            _parse_opencode_auth_list("┌ Credentials\n│ anthropic\n└ 2 credentials\n"),
            ["anthropic"],
        )


class TestOpencodeAuthCheck(unittest.TestCase):
    def test_not_installed(self):
        with patch(
            "orchestrator.provider_auth.get_opencode_executable_path",
            side_effect=FileNotFoundError("no opencode"),
        ):
            status = check_opencode_auth()
        self.assertEqual(status["auth_state"], AUTH_NOT_INSTALLED)

    def test_authenticated_reports_provider_names_and_the_required_subscription_copy(self):
        with patch(
            "orchestrator.provider_auth.get_opencode_executable_path",
            return_value="C:/bin/opencode.exe",
        ), patch(
            "orchestrator.provider_auth.subprocess.run",
            return_value=_Completed(returncode=0, stdout=b"anthropic\nopencode\n", stderr=b""),
        ):
            status = check_opencode_auth()
        self.assertEqual(status["auth_state"], AUTH_AUTHENTICATED)
        self.assertIn("anthropic", status["detail"])
        self.assertIn("opencode", status["detail"])
        self.assertEqual(status["subscription_detail"], SUBSCRIPTION_CONFIRMED_DETAIL)

    def test_real_opencode_1_18_29_box_drawn_output_is_not_authenticated_not_fabricated(self):
        # End-to-end reproduction of the live-validation bug via check_opencode_auth(), using the
        # exact real subprocess bytes captured from opencode 1.18.29 with zero stored credentials.
        stdout = (
            b"\x1b[90m\xe2\x94\x8c\x1b[39m  Credentials \x1b[90m~\\.local\\share\\opencode\\auth.json\n"
            b"\x1b[90m\xe2\x94\x82\x1b[39m\n"
            b"\x1b[90m\xe2\x94\x94\x1b[39m  0 credentials\n\n"
        )
        stderr = b"\x1b[0m\r\n"
        with patch(
            "orchestrator.provider_auth.get_opencode_executable_path",
            return_value="C:/bin/opencode.exe",
        ), patch(
            "orchestrator.provider_auth.subprocess.run",
            return_value=_Completed(returncode=0, stdout=stdout, stderr=stderr),
        ):
            status = check_opencode_auth()
        self.assertEqual(status["auth_state"], AUTH_NOT_AUTHENTICATED)
        self.assertNotEqual(status["auth_state"], AUTH_AUTHENTICATED)
        self.assertNotIn("\u250c", status["detail"])
        self.assertNotIn("\u2502", status["detail"])
        self.assertNotIn("\u2514", status["detail"])

    def test_not_authenticated_when_no_providers_configured(self):
        with patch(
            "orchestrator.provider_auth.get_opencode_executable_path",
            return_value="C:/bin/opencode.exe",
        ), patch(
            "orchestrator.provider_auth.subprocess.run",
            return_value=_Completed(returncode=0, stdout=b"", stderr=b""),
        ):
            status = check_opencode_auth()
        self.assertEqual(status["auth_state"], AUTH_NOT_AUTHENTICATED)

    def test_nonzero_exit_is_a_cli_error(self):
        with patch(
            "orchestrator.provider_auth.get_opencode_executable_path",
            return_value="C:/bin/opencode.exe",
        ), patch(
            "orchestrator.provider_auth.subprocess.run",
            return_value=_Completed(returncode=1, stdout=b"", stderr=b"boom"),
        ):
            status = check_opencode_auth()
        self.assertEqual(status["auth_state"], AUTH_CLI_ERROR)

    def test_nonzero_exit_with_ansi_colored_output_does_not_leak_escape_codes_into_detail(self):
        # A real opencode install colorizes basically everything, error output included (see the
        # stray "\x1b[0m\r\n" this module's own bug report captured on stderr in the *success*
        # case). `_ANSI_ESCAPE_RE` must be applied to the text used for this branch's detail
        # message too, not just inside `_parse_opencode_auth_list`.
        with patch(
            "orchestrator.provider_auth.get_opencode_executable_path",
            return_value="C:/bin/opencode.exe",
        ), patch(
            "orchestrator.provider_auth.subprocess.run",
            return_value=_Completed(
                returncode=1, stdout=b"", stderr=b"\x1b[31merror: something broke\x1b[0m"
            ),
        ):
            status = check_opencode_auth()
        self.assertEqual(status["auth_state"], AUTH_CLI_ERROR)
        self.assertNotIn("\x1b", status["detail"])
        self.assertIn("error: something broke", status["detail"])

    def test_unparseable_output_is_a_cli_error_not_a_guess(self):
        with patch(
            "orchestrator.provider_auth.get_opencode_executable_path",
            return_value="C:/bin/opencode.exe",
        ), patch(
            "orchestrator.provider_auth.subprocess.run",
            return_value=_Completed(returncode=0, stdout=b"PROVIDER   METHOD\n", stderr=b""),
        ):
            status = check_opencode_auth()
        self.assertEqual(status["auth_state"], AUTH_CLI_ERROR)

    def test_timeout(self):
        with patch(
            "orchestrator.provider_auth.get_opencode_executable_path",
            return_value="C:/bin/opencode.exe",
        ), patch(
            "orchestrator.provider_auth.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="opencode", timeout=15),
        ):
            status = check_opencode_auth()
        self.assertEqual(status["auth_state"], AUTH_TIMED_OUT)

    def test_probe_command_and_safe_flags(self):
        with patch(
            "orchestrator.provider_auth.get_opencode_executable_path",
            return_value="C:/bin/opencode.exe",
        ), patch(
            "orchestrator.provider_auth.subprocess.run",
            return_value=_Completed(returncode=0, stdout=b"anthropic\n", stderr=b""),
        ) as mock_run:
            check_opencode_auth(timeout=9)
        args, kwargs = mock_run.call_args
        self.assertEqual(args[0], ["C:/bin/opencode.exe", "auth", "list"])
        self.assertFalse(kwargs["shell"])
        self.assertIsNotNone(kwargs["stdin"])
        self.assertEqual(kwargs["timeout"], 9)

    def test_no_secret_shaped_output_survives_into_detail(self):
        with patch(
            "orchestrator.provider_auth.get_opencode_executable_path",
            return_value="C:/bin/opencode.exe",
        ), patch(
            "orchestrator.provider_auth.subprocess.run",
            return_value=_Completed(
                returncode=1, stdout=b"", stderr=b"token=sk-ant-api03-verysecretvalue123"
            ),
        ):
            status = check_opencode_auth()
        self.assertNotIn("sk-ant-api03", status["detail"])


class TestCheckAllProviders(unittest.TestCase):
    def test_returns_every_provider_in_a_fixed_order(self):
        # Every resolver in PROVIDERS must be patched, including codex (Package H): an
        # unpatched one falls through to the real binary on the developer's machine, and the
        # probe then reports that machine's true state instead of the fixture's.
        with patch(
            "orchestrator.provider_auth.get_claude_executable_path",
            side_effect=FileNotFoundError("x"),
        ), patch(
            "orchestrator.provider_auth.get_opencode_executable_path",
            side_effect=FileNotFoundError("x"),
        ), patch(
            "orchestrator.provider_auth.get_antigravity_executable_path",
            side_effect=FileNotFoundError("x"),
        ), patch(
            "orchestrator.provider_auth.get_codex_executable_path",
            side_effect=FileNotFoundError("x"),
        ):
            report = check_all_providers()
        self.assertEqual([p["provider"] for p in report], list(PROVIDERS))
        self.assertTrue(all(p["auth_state"] == AUTH_NOT_INSTALLED for p in report))


class TestProvidersOk(unittest.TestCase):
    def test_ok_when_nothing_is_a_cli_error_or_timeout(self):
        report = [
            {"provider": "claude", "auth_state": AUTH_UNVERIFIABLE},
            {"provider": "opencode", "auth_state": AUTH_NOT_AUTHENTICATED},
            {"provider": "antigravity", "auth_state": AUTH_NOT_INSTALLED},
        ]
        self.assertTrue(providers_ok(report))

    def test_not_ok_when_something_errored(self):
        report = [{"provider": "claude", "auth_state": AUTH_CLI_ERROR}]
        self.assertFalse(providers_ok(report))


class TestFormatProviderReport(unittest.TestCase):
    def test_names_every_provider_and_its_detail(self):
        report = [
            {
                "provider": "claude", "installed": True, "auth_state": AUTH_UNVERIFIABLE,
                "detail": "no documented check", "subscription_detail": "",
            },
            {
                "provider": "opencode", "installed": True, "auth_state": AUTH_AUTHENTICATED,
                "detail": "providers: anthropic", "subscription_detail": SUBSCRIPTION_CONFIRMED_DETAIL,
            },
            {
                "provider": "antigravity", "installed": False, "auth_state": AUTH_NOT_INSTALLED,
                "detail": "not found", "subscription_detail": "",
            },
        ]
        text = format_provider_report(report, color=False)
        self.assertIn("claude", text)
        self.assertIn("opencode", text)
        self.assertIn("antigravity", text)
        self.assertIn(SUBSCRIPTION_CONFIRMED_DETAIL, text)


class TestOpenProviderLogin(unittest.TestCase):
    def test_unknown_provider_is_refused_without_spawning_anything(self):
        with patch("orchestrator.provider_auth._spawn_detached_terminal") as spawn:
            result = open_provider_login("not-a-real-provider")
        self.assertFalse(result["ok"])
        spawn.assert_not_called()

    def test_not_installed_is_refused_without_spawning_anything(self):
        with patch(
            "orchestrator.provider_auth.get_claude_executable_path",
            side_effect=FileNotFoundError("no claude"),
        ), patch("orchestrator.provider_auth._spawn_detached_terminal") as spawn:
            result = open_provider_login("claude")
        self.assertFalse(result["ok"])
        spawn.assert_not_called()

    def test_claude_login_spawns_the_bare_binary(self):
        with patch(
            "orchestrator.provider_auth.get_claude_executable_path",
            return_value="C:/bin/claude.exe",
        ), patch("orchestrator.provider_auth._spawn_detached_terminal") as spawn:
            result = open_provider_login("claude")
        self.assertTrue(result["ok"])
        spawn.assert_called_once()
        cmd = spawn.call_args.args[0]
        self.assertEqual(cmd, ["C:/bin/claude.exe"])

    def test_opencode_login_spawns_auth_login(self):
        with patch(
            "orchestrator.provider_auth.get_opencode_executable_path",
            return_value="C:/bin/opencode.exe",
        ), patch("orchestrator.provider_auth._spawn_detached_terminal") as spawn:
            result = open_provider_login("opencode")
        self.assertTrue(result["ok"])
        cmd = spawn.call_args.args[0]
        self.assertEqual(cmd, ["C:/bin/opencode.exe", "auth", "login"])

    def test_spawn_failure_is_reported_not_raised(self):
        with patch(
            "orchestrator.provider_auth.get_claude_executable_path",
            return_value="C:/bin/claude.exe",
        ), patch(
            "orchestrator.provider_auth._spawn_detached_terminal",
            side_effect=OSError("no terminal available"),
        ):
            result = open_provider_login("claude")
        self.assertFalse(result["ok"])
        self.assertIn("no terminal available", result["error"])


class TestSpawnDetachedTerminal(unittest.TestCase):
    """Direct coverage of `_spawn_detached_terminal`'s own branch logic.

    Every other test in this file mocks `_spawn_detached_terminal` away entirely, which is how
    a missing `start_new_session=True` on the POSIX branch went unnoticed through six reviews -
    see the design spec's amendment. This exercises the function itself instead.
    """

    def test_posix_branch_detaches_into_its_own_session(self):
        with patch("orchestrator.provider_auth.sys.platform", "linux"), patch(
            "orchestrator.provider_auth.subprocess.Popen"
        ) as mock_popen:
            _spawn_detached_terminal(["claude"], cwd="/tmp/project", title="Claude Login")
        mock_popen.assert_called_once_with(
            ["claude"], cwd="/tmp/project", start_new_session=True
        )


if __name__ == "__main__":
    unittest.main()
