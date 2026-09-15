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
        # _SettingsDaemonCase's shared fixture config ({"agents": [], "roles": {}}) is
        # deliberately minimal for routes that don't validate it, but an empty 'agents'
        # list genuinely fails validate_config (see orchestrator/config.py). Swap in a
        # minimal but valid config here so this test exercises the true "valid" path
        # without touching the shared fixture other test classes rely on.
        self.daemon.config = {
            "agents": [{"agent": "claude", "role": "implementer"}],
            "roles": {},
        }
        status, body = self._get("/api/app_info")
        self.assertEqual(status, 200)
        self.assertIn("version", body)
        self.assertTrue(body["config_valid"])


if __name__ == "__main__":
    unittest.main()
