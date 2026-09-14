"""Package H - Codex account-linking status checks and login flow.

Companion to `test_provider_auth.py` (Package G), kept in its own file so the three existing
providers' coverage stays untouched. Every expectation below is measured against a live
`codex-cli 0.154.0`, not inferred from documentation; see
docs/superpowers/specs/2026-09-13-package-h-codex-agent-design.md for the captures.
"""

import unittest
from unittest.mock import MagicMock, patch

from orchestrator.provider_auth import (
    AUTH_AUTHENTICATED,
    AUTH_CLI_ERROR,
    AUTH_NOT_AUTHENTICATED,
    AUTH_NOT_INSTALLED,
    AUTH_TIMED_OUT,
    PROVIDERS,
    SUBSCRIPTION_CONFIRMED_DETAIL,
    SUBSCRIPTION_UNAVAILABLE,
    check_all_providers,
    check_codex_auth,
    format_provider_report,
    open_provider_login,
)


#: Anything that would mean Synagon is touching a credential rather than asking the vendor's
#: CLI about it. Matched case-insensitively.
_CREDENTIAL_TOKENS = ("auth.json", "openai_api_key", "credentials.json", ".codex/auth")


def _credential_references(module):
    """Names and non-docstring literals in `module` that reference a credential source.

    Parsed with `ast` rather than grepped, because both modules *document* in prose that they
    never read `~/.codex/auth.json` - a plain substring search over the source would flag that
    promise as though it were the violation. Docstrings are excluded; every other string
    constant, identifier, and attribute is checked, so an actual `open(".../auth.json")` or an
    `os.environ["OPENAI_API_KEY"]` is still caught.
    """
    import ast

    with open(module.__file__, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())

    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                docstrings.add(id(body[0].value))

    found = []
    for node in ast.walk(tree):
        candidate = None
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docstrings:
                candidate = node.value
        elif isinstance(node, ast.Name):
            candidate = node.id
        elif isinstance(node, ast.Attribute):
            candidate = node.attr

        if candidate and any(t in candidate.lower() for t in _CREDENTIAL_TOKENS):
            found.append(candidate)
    return found


def _completed(returncode, stdout=b"", stderr=b""):
    completed = MagicMock()
    completed.returncode = returncode
    completed.stdout = stdout
    completed.stderr = stderr
    return completed


class TestCheckCodexAuth(unittest.TestCase):
    def test_signed_in_is_authenticated(self):
        """Measured: exit 0, stdout EMPTY, the answer on stderr."""
        with patch("orchestrator.provider_auth.get_codex_executable_path", return_value="codex"), \
             patch("orchestrator.provider_auth._run",
                   return_value=_completed(0, b"", b"Logged in using ChatGPT\n")):
            status = check_codex_auth()
        self.assertEqual(status["auth_state"], AUTH_AUTHENTICATED)
        self.assertTrue(status["installed"])

    def test_signed_out_is_not_authenticated_despite_exit_1(self):
        """Measured: signed-out exits 1. That is an answer, not a probe failure."""
        with patch("orchestrator.provider_auth.get_codex_executable_path", return_value="codex"), \
             patch("orchestrator.provider_auth._run",
                   return_value=_completed(1, b"", b"Not logged in\n")):
            status = check_codex_auth()
        self.assertEqual(status["auth_state"], AUTH_NOT_AUTHENTICATED)

    def test_tolerates_leading_warning_lines(self):
        """Measured with a non-default CODEX_HOME: a WARNING precedes the signal line."""
        noisy = (b"WARNING: proceeding, even though we could not create PATH aliases: ...\n"
                 b"Not logged in\n")
        with patch("orchestrator.provider_auth.get_codex_executable_path", return_value="codex"), \
             patch("orchestrator.provider_auth._run", return_value=_completed(1, b"", noisy)):
            status = check_codex_auth()
        self.assertEqual(status["auth_state"], AUTH_NOT_AUTHENTICATED)

    def test_negative_marker_wins_over_substring(self):
        """'Not logged in' contains 'logged in'; the negative must be tested first."""
        with patch("orchestrator.provider_auth.get_codex_executable_path", return_value="codex"), \
             patch("orchestrator.provider_auth._run",
                   return_value=_completed(1, b"", b"Not logged in\n")):
            self.assertEqual(check_codex_auth()["auth_state"], AUTH_NOT_AUTHENTICATED)

    def test_signed_in_detail_quotes_the_cli_not_a_warning(self):
        noisy = b"WARNING: something unrelated\nLogged in using ChatGPT\n"
        with patch("orchestrator.provider_auth.get_codex_executable_path", return_value="codex"), \
             patch("orchestrator.provider_auth._run", return_value=_completed(0, b"", noisy)):
            detail = check_codex_auth()["detail"]
        self.assertIn("Logged in using ChatGPT", detail)
        self.assertNotIn("WARNING", detail)

    def test_never_claims_subscription(self):
        with patch("orchestrator.provider_auth.get_codex_executable_path", return_value="codex"), \
             patch("orchestrator.provider_auth._run",
                   return_value=_completed(0, b"", b"Logged in using ChatGPT\n")):
            status = check_codex_auth()
        self.assertEqual(status["subscription_state"], SUBSCRIPTION_UNAVAILABLE)
        self.assertEqual(status["subscription_detail"], SUBSCRIPTION_CONFIRMED_DETAIL)

    def test_reports_auth_mode_without_claiming_a_plan(self):
        with patch("orchestrator.provider_auth.get_codex_executable_path", return_value="codex"), \
             patch("orchestrator.provider_auth._run",
                   return_value=_completed(0, b"", b"Logged in using ChatGPT\n")):
            detail = check_codex_auth()["detail"].lower()
        self.assertIn("chatgpt", detail)
        for claim in ("plus", "pro plan", "subscription is active", "entitled"):
            self.assertNotIn(claim, detail)

    def test_unrecognised_output_is_cli_error_not_a_guess(self):
        with patch("orchestrator.provider_auth.get_codex_executable_path", return_value="codex"), \
             patch("orchestrator.provider_auth._run",
                   return_value=_completed(0, b"", b"something entirely new\n")):
            self.assertEqual(check_codex_auth()["auth_state"], AUTH_CLI_ERROR)

    def test_not_installed(self):
        with patch("orchestrator.provider_auth.get_codex_executable_path",
                   side_effect=FileNotFoundError("no codex")):
            status = check_codex_auth()
        self.assertEqual(status["auth_state"], AUTH_NOT_INSTALLED)
        self.assertFalse(status["installed"])

    def test_timeout(self):
        from orchestrator.provider_auth import _ProbeTimeout
        with patch("orchestrator.provider_auth.get_codex_executable_path", return_value="codex"), \
             patch("orchestrator.provider_auth._run", side_effect=_ProbeTimeout("slow")):
            self.assertEqual(check_codex_auth()["auth_state"], AUTH_TIMED_OUT)

    def test_probe_failure_is_cli_error(self):
        from orchestrator.provider_auth import _ProbeFailed
        with patch("orchestrator.provider_auth.get_codex_executable_path", return_value="codex"), \
             patch("orchestrator.provider_auth._run", side_effect=_ProbeFailed("boom")):
            self.assertEqual(check_codex_auth()["auth_state"], AUTH_CLI_ERROR)

    def test_detail_is_redacted(self):
        leaky = b"Logged in using ChatGPT sk-abcdefghijklmnop\n"
        with patch("orchestrator.provider_auth.get_codex_executable_path", return_value="codex"), \
             patch("orchestrator.provider_auth._run", return_value=_completed(0, b"", leaky)):
            self.assertNotIn("sk-abcdefghijklmnop", check_codex_auth()["detail"])

    def test_module_never_references_the_credential_file_or_api_key(self):
        """The credential file must never become an auth signal, nor a key ever be read."""
        import orchestrator.provider_auth as module
        self.assertEqual(_credential_references(module), [])

    def test_adapter_never_references_the_credential_file_or_api_key(self):
        import orchestrator.agents.codex as module
        self.assertEqual(_credential_references(module), [])


class TestCodexInProvidersRegistry(unittest.TestCase):
    def test_codex_appended_last(self):
        self.assertEqual(PROVIDERS, ("claude", "opencode", "antigravity", "codex"))

    def test_check_all_includes_codex(self):
        with patch("orchestrator.provider_auth.get_claude_executable_path",
                   side_effect=FileNotFoundError()), \
             patch("orchestrator.provider_auth.get_opencode_executable_path",
                   side_effect=FileNotFoundError()), \
             patch("orchestrator.provider_auth.get_antigravity_executable_path",
                   side_effect=FileNotFoundError()), \
             patch("orchestrator.provider_auth.get_codex_executable_path",
                   side_effect=FileNotFoundError()):
            results = check_all_providers()
        self.assertEqual([r["provider"] for r in results],
                         ["claude", "opencode", "antigravity", "codex"])


class TestCodexLogin(unittest.TestCase):
    def test_spawns_detached_codex_login(self):
        with patch("orchestrator.provider_auth.get_codex_executable_path", return_value="codex"), \
             patch("orchestrator.provider_auth._spawn_detached_terminal") as spawn:
            result = open_provider_login("codex")
        self.assertTrue(result["ok"])
        self.assertEqual(spawn.call_args[0][0], ["codex", "login"])

    def test_login_never_passes_a_credential_flag(self):
        """--with-api-key / --with-access-token read secrets from stdin. Never ours to send."""
        with patch("orchestrator.provider_auth.get_codex_executable_path", return_value="codex"), \
             patch("orchestrator.provider_auth._spawn_detached_terminal") as spawn:
            open_provider_login("codex")
        cmd = spawn.call_args[0][0]
        self.assertNotIn("--with-api-key", cmd)
        self.assertNotIn("--with-access-token", cmd)

    def test_login_when_not_installed_reports_cleanly(self):
        with patch("orchestrator.provider_auth.get_codex_executable_path",
                   side_effect=FileNotFoundError("nope")):
            result = open_provider_login("codex")
        self.assertFalse(result["ok"])
        self.assertIn("not installed", result["error"])

    def test_unknown_provider_still_refused(self):
        self.assertFalse(open_provider_login("gpt4all")["ok"])


class TestCodexInReport(unittest.TestCase):
    def test_report_renders_codex_row(self):
        rows = [{
            "provider": "codex",
            "installed": True,
            "executable": "codex",
            "auth_state": AUTH_AUTHENTICATED,
            "detail": "Logged in using ChatGPT",
            "subscription_state": SUBSCRIPTION_UNAVAILABLE,
            "subscription_detail": SUBSCRIPTION_CONFIRMED_DETAIL,
        }]
        out = format_provider_report(rows, color=False)
        self.assertIn("codex", out)
        self.assertIn("[OK]", out)
        self.assertIn("subscription status cannot be confirmed", out)


if __name__ == "__main__":
    unittest.main()
