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
    check_antigravity_auth,
    check_claude_auth,
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
        self.assertIsNotNone(kwargs["stdin"])
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


if __name__ == "__main__":
    unittest.main()
