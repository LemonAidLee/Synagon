"""Package G - the `--check-providers` CLI flag."""

import contextlib
import io
import sys
import unittest
from unittest.mock import patch

import orchestrator.__main__ as cli


class TestCheckProvidersFlag(unittest.TestCase):
    def test_prints_report_and_exits_zero_when_no_probe_errored(self):
        report = [
            {"provider": "claude", "installed": True, "auth_state": "auth_unverifiable",
             "detail": "claude responds", "subscription_detail": ""},
            {"provider": "opencode", "installed": True, "auth_state": "authenticated",
             "detail": "providers: anthropic", "subscription_detail":
                 "Authentication verified; subscription status cannot be confirmed."},
            {"provider": "antigravity", "installed": False, "auth_state": "not_installed",
             "detail": "not found", "subscription_detail": ""},
        ]
        out = io.StringIO()
        # Patched on `orchestrator.__main__`, not `orchestrator.provider_auth`: __main__.py does
        # `from orchestrator.provider_auth import check_all_providers`, a direct name import, so
        # the name a patch must replace is the one __main__'s dispatch code actually looks up.
        with patch.object(sys, "argv", ["orchestrator", "--check-providers"]), \
                patch("orchestrator.__main__.check_all_providers", return_value=report), \
                contextlib.redirect_stdout(out):
            code = cli.main()
        text = out.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("claude", text)
        self.assertIn("opencode", text)
        self.assertIn("antigravity", text)

    def test_exits_nonzero_when_a_probe_itself_errored(self):
        report = [
            {"provider": "claude", "installed": True, "auth_state": "cli_error",
             "detail": "boom", "subscription_detail": ""},
        ]
        out = io.StringIO()
        with patch.object(sys, "argv", ["orchestrator", "--check-providers"]), \
                patch("orchestrator.__main__.check_all_providers", return_value=report), \
                contextlib.redirect_stdout(out):
            code = cli.main()
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
