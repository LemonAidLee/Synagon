"""Package G - the daemon's provider account-linking routes.

Shaped like `tests/test_package_c.py`'s daemon fixture: a real `serve_daemon` on an OS-assigned
port, called over HTTP with the daemon's own token, torn down after each test. No real provider
CLI is ever invoked here - `provider_auth`'s own subprocess-level behavior is covered by
`tests/test_provider_auth.py`; this file only proves the daemon wires it up correctly.
"""

import json
import shutil
import tempfile
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from orchestrator.daemon import TOKEN_HEADER, Daemon, serve_daemon


class _ProviderDaemonCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="orch_pkgg_")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.config = {"agents": [], "roles": {}}
        self.daemon = Daemon(self.root, self.config, token="t")
        self.server = serve_daemon(
            self.root, self.config, port=0, open_browser=False, serve_forever=False,
            printer=lambda *_: None, daemon=self.daemon,
        )
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]

    def tearDown(self):
        self.daemon.stopping.set()
        self.server.shutdown()
        self.server.server_close()

    def _get(self, path):
        request = urllib.request.Request(self.base + path)
        request.add_header(TOKEN_HEADER, "t")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def _call(self, path, payload=None):
        body = json.dumps(payload if payload is not None else {}).encode("utf-8")
        request = urllib.request.Request(self.base + path, data=body)
        request.add_header(TOKEN_HEADER, "t")
        request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8") or "{}")


_FAKE_REPORT = [
    {"provider": "claude", "installed": True, "auth_state": "auth_unverifiable",
     "detail": "d", "subscription_detail": "", "checked_at": 0.0, "duration_seconds": 0.0},
]


class TestProvidersRoute(_ProviderDaemonCase):
    def test_get_api_providers_returns_the_report(self):
        with patch("orchestrator.provider_auth.check_all_providers", return_value=_FAKE_REPORT):
            status, body = self._get("/api/providers")
        self.assertEqual(status, 200)
        data = json.loads(body.decode("utf-8"))
        self.assertEqual(data["providers"][0]["provider"], "claude")

    def test_requires_the_daemon_token(self):
        request = urllib.request.Request(self.base + "/api/providers")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(request, timeout=10)
        self.assertEqual(ctx.exception.code, 401)


class TestSettingsPageRoute(_ProviderDaemonCase):
    def test_settings_serves_the_page_with_token_substituted(self):
        status, body = self._get("/settings")
        self.assertEqual(status, 200)
        text = body.decode("utf-8")
        self.assertIn("PROVIDER ACCOUNTS", text)
        self.assertNotIn("__ORCHESTRATOR_TOKEN__", text)
        self.assertIn('content="t"', text)

    def test_settings_html_alias_works_too(self):
        status, _ = self._get("/settings.html")
        self.assertEqual(status, 200)


class TestProviderLoginControlAction(_ProviderDaemonCase):
    def test_login_delegates_to_open_provider_login(self):
        with patch(
            "orchestrator.provider_auth.open_provider_login",
            return_value={"ok": True, "provider": "claude"},
        ) as mock_login:
            status, body = self._call("/api/control/provider_login", {"provider": "claude"})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        mock_login.assert_called_once_with("claude")

    def test_missing_provider_is_a_400_not_a_crash(self):
        status, body = self._call("/api/control/provider_login", {})
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])

    def test_login_failure_from_provider_auth_is_reported_not_raised(self):
        with patch(
            "orchestrator.provider_auth.open_provider_login",
            return_value={"ok": False, "error": "not installed"},
        ):
            status, body = self._call("/api/control/provider_login", {"provider": "claude"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "not installed")


if __name__ == "__main__":
    unittest.main()
