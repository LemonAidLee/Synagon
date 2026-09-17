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

    def _get_raw(self, path):
        request = urllib.request.Request(self.base + path)
        request.add_header(TOKEN_HEADER, "t")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

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
    def test_get_reports_the_version_even_with_no_config_file(self):
        # The temp project root has no orchestrator.yaml. The version is a fact about the
        # application, so it is still reported; the configuration is called invalid, with a
        # reason that names the file rather than a bare traceback.
        status, body = self._get("/api/app_info")
        self.assertEqual(status, 200)
        self.assertTrue(body["version"])
        self.assertFalse(body["config_valid"])
        self.assertIn("orchestrator.yaml", body["config_error"])

    def test_a_valid_config_file_is_not_reported_invalid(self):
        """The config the daemon holds is `load_config`'s output, not the file as written.

        `load_config` materializes optional keys it did not find - `planning.agent: None`
        among them - and `validate_config` reads a literal None there as an agent named
        "None" and rejects it. Validating that dict told everyone with a perfectly good
        orchestrator.yaml that their configuration was invalid. This asserts on the real
        production path: a file on disk, and the config the daemon was handed from it.
        """
        from orchestrator.config import load_config

        path = os.path.join(self.root, "orchestrator.yaml")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(
                "agents:\n"
                "  - agent: claude\n"
                "    role: implementer\n"
                "roles:\n"
                "  implementer:\n"
                "    responsibility: Write the code.\n"
                "planning:\n"
                "  max_tasks: 12\n"
            )
        self.daemon.config = load_config(path)
        self.daemon.config_path = path

        status, body = self._get("/api/app_info")
        self.assertEqual(status, 200)
        self.assertTrue(
            body["config_valid"],
            "a valid orchestrator.yaml was reported invalid: %s" % body.get("config_error"),
        )
        self.assertIsNone(body["config_error"])

    def test_an_invalid_config_file_is_reported_with_its_reason(self):
        path = os.path.join(self.root, "orchestrator.yaml")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(
                "agents:\n"
                "  - agent: not_a_provider\n"
                "    role: implementer\n"
                "roles:\n"
                "  implementer:\n"
                "    responsibility: Write the code.\n"
            )
        self.daemon.config_path = path

        status, body = self._get("/api/app_info")
        self.assertEqual(status, 200)
        self.assertFalse(body["config_valid"])
        self.assertIn("not_a_provider", body["config_error"])


class TestPrunePlanRoute(_SettingsDaemonCase):
    def test_get_uses_preference_defaults_when_no_query_given(self):
        fake_plan = {"available": True, "total": 0, "prunable": [], "kept": []}
        with patch("orchestrator.prune.plan_prune", return_value=fake_plan) as mock_plan:
            status, body = self._get("/api/prune/plan")
        self.assertEqual(status, 200)
        self.assertTrue(body["available"])
        mock_plan.assert_called_once()
        self.assertEqual(mock_plan.call_args.args[1], 30 * 86400)

    def test_get_honors_query_overrides(self):
        with patch("orchestrator.prune.plan_prune", return_value={"available": True}) as mock_plan:
            status, _ = self._get("/api/prune/plan?older_than_days=7&keep_failed=0")
        self.assertEqual(status, 200)
        self.assertEqual(mock_plan.call_args.args[1], 7 * 86400)
        self.assertFalse(mock_plan.call_args.kwargs["keep_failed"])


class TestPruneExecuteRoute(_SettingsDaemonCase):
    """`delete_branch` is `git branch -D`, so what reaches it has to be re-checked here.

    The plan comes from the page, which may have drawn it minutes earlier. These tests pin
    the contract the design spec asked for: a branch is deleted only if the planner still
    says it may be, and a plan that drifted - or was never the planner's - deletes nothing.
    """

    FRESH = {
        "available": True,
        "older_than_seconds": 86400,
        "keep_failed": False,
        "merged_older_than_seconds": None,
        "prunable": [{"branch": "run/still-ok", "worktree": None, "age_seconds": 99999}],
        "kept": [{"branch": "run/now-busy", "keep_reason": "checked out in the main repository"}],
    }

    def test_post_deletes_a_branch_the_planner_still_lists(self):
        submitted = {
            "available": True, "older_than_seconds": 86400, "keep_failed": False,
            "prunable": [{"branch": "run/still-ok"}],
        }
        with patch("orchestrator.prune.plan_prune", return_value=self.FRESH), \
             patch("orchestrator.prune.execute_prune",
                   return_value={"deleted": [{"branch": "run/still-ok"}], "failed": []}) as ex:
            status, body = self._post("/api/prune/execute", {"plan": submitted})
        self.assertEqual(status, 200)
        self.assertEqual([d["branch"] for d in body["deleted"]], ["run/still-ok"])
        self.assertEqual(body["refused"], [])
        # The server's own candidate is what was executed, not the submitted stub.
        self.assertEqual(ex.call_args.args[1]["prunable"], self.FRESH["prunable"])

    def test_post_refuses_a_branch_that_stopped_being_prunable(self):
        """Previewed, then checked out before the click. It must survive, with the reason."""
        submitted = {
            "available": True, "older_than_seconds": 86400, "keep_failed": False,
            "prunable": [{"branch": "run/now-busy"}],
        }
        with patch("orchestrator.prune.plan_prune", return_value=self.FRESH), \
             patch("orchestrator.prune.execute_prune") as ex:
            status, body = self._post("/api/prune/execute", {"plan": submitted})
        self.assertEqual(status, 200)
        ex.assert_not_called()
        self.assertEqual(body["deleted"], [])
        self.assertEqual(
            body["refused"], [{"branch": "run/now-busy",
                               "error": "checked out in the main repository"}]
        )

    def test_post_refuses_a_branch_the_planner_never_named(self):
        """A fabricated plan naming any other branch - main included - deletes nothing."""
        submitted = {
            "available": True, "older_than_seconds": 86400, "keep_failed": False,
            "prunable": [{"branch": "main"}, {"branch": "run/still-ok"}],
        }
        with patch("orchestrator.prune.plan_prune", return_value=self.FRESH), \
             patch("orchestrator.prune.execute_prune",
                   return_value={"deleted": [{"branch": "run/still-ok"}], "failed": []}) as ex:
            status, body = self._post("/api/prune/execute", {"plan": submitted})
        self.assertEqual(status, 200)
        executed = [c["branch"] for c in ex.call_args.args[1]["prunable"]]
        self.assertEqual(executed, ["run/still-ok"])
        self.assertNotIn("main", executed)
        self.assertEqual([r["branch"] for r in body["refused"]], ["main"])

    def test_post_deletes_nothing_when_the_repository_cannot_be_planned(self):
        unavailable = {"available": False, "reason": "git is not installed or not on PATH"}
        submitted = {"available": True, "prunable": [{"branch": "run/x"}]}
        with patch("orchestrator.prune.plan_prune", return_value=unavailable), \
             patch("orchestrator.prune.execute_prune") as ex:
            status, body = self._post("/api/prune/execute", {"plan": submitted})
        self.assertEqual(status, 200)
        ex.assert_not_called()
        self.assertEqual(body["deleted"], [])
        self.assertIn("git is not installed", body["error"])

    def test_post_with_an_empty_plan_deletes_nothing(self):
        with patch("orchestrator.prune.execute_prune") as ex:
            status, body = self._post("/api/prune/execute", {"plan": {"prunable": []}})
        self.assertEqual(status, 200)
        ex.assert_not_called()
        self.assertEqual(body["deleted"], [])

    def test_post_without_a_plan_is_a_400(self):
        status, body = self._post("/api/prune/execute", {})
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])


class TestOfficeAndDesignAreThemed(unittest.TestCase):
    """The office and the design surface carried their own palette until Package I.

    A theme chosen from the desktop Settings menu persisted but restyled nothing while one of
    them was open - and `startup_view: office` makes that the first thing a project shows.
    """

    def setUp(self):
        from orchestrator.serve import WEB_DIR

        self.web = WEB_DIR
        self.pages = {
            name: (WEB_DIR / ("%s.html" % name)).read_text(encoding="utf-8")
            for name in ("office", "design")
        }

    def test_both_link_the_shared_palette_and_the_bridge(self):
        for name, page in self.pages.items():
            self.assertIn('href="/shared/theme.css"', page, name)
            self.assertIn('href="/shared/surface.css"', page, name)

    def test_neither_defines_a_palette_of_its_own(self):
        import re

        for name, page in self.pages.items():
            css = page[page.find("<style>"):page.find("</style>")]
            leftover = re.findall(r"#[0-9a-fA-F]{3,8}\b", css)
            self.assertEqual(leftover, [], "%s.html CSS still hardcodes %s" % (name, leftover))

    def test_both_expose_the_apply_theme_contract_the_menu_calls(self):
        # desktop/main.js applies a menu choice with
        # `window.__applyTheme && window.__applyTheme(id)`, so a page without it is a page
        # the Theme menu silently does nothing to.
        for name, page in self.pages.items():
            self.assertIn("window.__applyTheme", page, name)
            for theme in ("theme-srcery", "theme-moonfly", "theme-jellybeans",
                          "theme-tender", "theme-miasma"):
                self.assertIn(theme, page, "%s.html does not know %s" % (name, theme))

    def test_both_read_the_saved_theme_but_tolerate_no_daemon(self):
        # Under `--serve` / `--design` there is no /api/preferences. The read has to fail
        # soft and leave the stylesheet's default in place.
        for name, page in self.pages.items():
            self.assertIn("/api/preferences", page, name)
            self.assertIn("catch", page, name)

    def test_the_bridge_only_defines_names_the_palette_does_not(self):
        """Redefining one of theme.css's own names would follow it into the cockpit."""
        import re

        theme = (self.web / "shared" / "theme.css").read_text(encoding="utf-8")
        surface = (self.web / "shared" / "surface.css").read_text(encoding="utf-8")
        declared = lambda css: set(re.findall(r"^\s*(--[a-z0-9-]+)\s*:", css, re.M))
        clash = declared(theme) & declared(surface)
        self.assertEqual(clash, set(), "surface.css redefines palette names: %s" % clash)

    def test_the_office_canvas_paints_from_the_palette(self):
        page = self.pages["office"]
        self.assertIn("readPalette", page)
        self.assertIn("PALETTE.panel", page)
        # Repainting on a theme change is what makes the scene follow the menu.
        self.assertIn("readPalette();", page)

    def test_role_and_figure_colors_stay_fixed_on_purpose(self):
        """A role's color identifies it; it is not chrome, and Miasma has no pink to offer."""
        page = self.pages["office"]
        self.assertIn("ROLE_COLORS", page)
        self.assertIn("#f0abfc", page)  # decomposer
        surface = (self.web / "shared" / "surface.css").read_text(encoding="utf-8")
        self.assertIn("--person: #c084fc", surface)

    def test_the_office_reads_the_token_before_it_uses_it(self):
        """`const TOKEN` is in the temporal dead zone until its own line runs."""
        page = self.pages["office"]
        self.assertLess(page.index("const TOKEN"), page.index("window.__applyTheme"))


class TestSharedAssetsServedWithoutADaemon(unittest.TestCase):
    """`serve.py` serves the office and the design surface too.

    The stylesheet they link has to resolve under that handler as well, or both pages render
    with no colors at all outside the desktop shell.
    """

    def setUp(self):
        import shutil
        import tempfile

        from orchestrator.serve import serve_board

        self.root = tempfile.mkdtemp(prefix="orch_pkgi_serve_")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        config = {
            "agents": [{"agent": "claude", "role": "implementer"}],
            "roles": {"implementer": {"responsibility": "write the code"}},
        }
        self.server = serve_board(
            self.root, config, port=0, open_browser=False, printer=lambda *_: None,
            serve_forever=False,
        )
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def _get(self, path):
        try:
            with urllib.request.urlopen(self.base + path, timeout=5) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def test_theme_css_is_served(self):
        status, body = self._get("/shared/theme.css")
        self.assertEqual(status, 200)
        self.assertIn(b"theme-moonfly", body)

    def test_surface_css_is_served(self):
        status, body = self._get("/shared/surface.css")
        self.assertEqual(status, 200)
        self.assertIn(b"--ink", body)

    def test_the_office_it_serves_links_something_that_resolves(self):
        status, body = self._get("/office")
        self.assertEqual(status, 200)
        page = body.decode("utf-8")
        for href in ("/shared/theme.css", "/shared/surface.css"):
            self.assertIn(href, page)
            self.assertEqual(self._get(href)[0], 200, "%s 404s under serve.py" % href)

    def test_it_is_an_allow_list_not_a_file_server(self):
        for probe in ("/shared/../orchestrator.yaml", "/shared/nope.css", "/shared/"):
            self.assertEqual(self._get(probe)[0], 404, probe)


class TestSharedThemeRoute(_SettingsDaemonCase):
    def test_get_serves_theme_css(self):
        status, _ = self._get_raw("/shared/theme.css")
        self.assertEqual(status, 200)

    def test_unknown_shared_file_is_404(self):
        status, _ = self._get_raw("/shared/does-not-exist.css")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
