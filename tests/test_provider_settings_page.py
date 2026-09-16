"""Package G - the /settings page's static content.

Content-only checks, in the same spirit as `test_reliability.py`'s page-text assertions: this
confirms the page ships the elements its JS and Task 6's daemon routes depend on, not that the
JS executes (there is no browser in this suite).
"""

import unittest

from orchestrator.serve import WEB_DIR


class TestSettingsPageContent(unittest.TestCase):
    def setUp(self):
        self.page = (WEB_DIR / "settings.html").read_text(encoding="utf-8")

    def test_carries_the_token_placeholder(self):
        self.assertIn('name="orchestrator-token"', self.page)
        self.assertIn("__ORCHESTRATOR_TOKEN__", self.page)

    def test_fetches_the_providers_api(self):
        self.assertIn("/api/providers", self.page)

    def test_posts_to_the_login_control_action(self):
        self.assertIn("'/api/control/' + action", self.page)
        self.assertIn("apiControl('provider_login'", self.page)

    def test_has_a_container_for_provider_cards(self):
        self.assertIn('id="provider-cards"', self.page)

    def test_has_login_and_check_again_actions(self):
        self.assertIn("Login", self.page)
        self.assertIn("Check again", self.page)

    def test_names_all_three_providers_in_script(self):
        for provider in ("claude", "opencode", "antigravity"):
            self.assertIn(provider, self.page)

    def test_names_codex_in_script(self):
        self.assertIn("codex", self.page)

    def test_uses_a_two_column_grid_on_wide_screens(self):
        """Four cards must form a 2x2 grid, not a 3-then-1 layout that strands Codex alone."""
        self.assertIn("repeat(2, minmax(0, 1fr))", self.page)

    def test_stacks_to_one_column_on_narrow_screens(self):
        self.assertIn("@media (max-width: 640px)", self.page)
        self.assertIn("grid-template-columns: 1fr", self.page)

    def test_cards_pin_actions_to_a_shared_bottom_edge(self):
        """So Login/Check again line up even when one card's text is much longer."""
        self.assertIn(".card-actions {", self.page)
        self.assertIn("margin-top: auto", self.page)

    def test_subscription_text_is_not_squeezed_into_a_label_value_row(self):
        """Codex's longer subscription sentence must wrap as its own paragraph, not fight a
        flex row for space the way a short Installed/Authentication value can."""
        self.assertIn("subscription-block", self.page)

    def test_focus_visible_styling_present(self):
        self.assertIn(":focus-visible", self.page)


class TestCockpitLinksToSettings(unittest.TestCase):
    def test_cockpit_header_links_to_settings(self):
        page = (WEB_DIR / "cockpit.html").read_text(encoding="utf-8")
        self.assertIn('href="/settings"', page)

    def test_header_settings_link_is_de_emphasized(self):
        """The desktop shell's Settings menu (desktop/main.js) is the primary way in now; the
        header link stays only for a plain-browser-tab cockpit, so it must no longer use the
        same heavy primary-btn treatment as START."""
        page = (WEB_DIR / "cockpit.html").read_text(encoding="utf-8")
        self.assertIn('class="settings-link"', page)
        self.assertNotIn('href="/settings" class="primary-btn"', page)


class TestDesktopSettingsMenu(unittest.TestCase):
    def setUp(self):
        desktop_main = WEB_DIR.parent.parent / "desktop" / "main.js"
        self.source = desktop_main.read_text(encoding="utf-8")

    def test_settings_menu_item_exists(self):
        self.assertIn('label: "Settings"', self.source)

    def test_settings_menu_opens_the_settings_route(self):
        self.assertIn('urlFor(window, "/settings")', self.source)

    def test_settings_menu_follows_window_menu(self):
        window_menu_pos = self.source.index('{ role: "windowMenu" }')
        settings_pos = self.source.index('label: "Settings"')
        self.assertLess(window_menu_pos, settings_pos)

    def test_settings_submenu_item_is_generic_now(self):
        """Package I: the page behind /settings is no longer provider-accounts-only."""
        self.assertIn('label: "Open Settings…"', self.source)
        self.assertNotIn('label: "Provider Accounts…"', self.source)

    def test_settings_has_an_accelerator(self):
        self.assertIn('accelerator: "CmdOrCtrl+,"', self.source)


class TestThemeMenu(unittest.TestCase):
    def setUp(self):
        desktop_main = WEB_DIR.parent.parent / "desktop" / "main.js"
        self.source = desktop_main.read_text(encoding="utf-8")

    def test_theme_submenu_exists_under_settings(self):
        settings_pos = self.source.index('label: "Settings"')
        theme_pos = self.source.index('label: "Theme"')
        self.assertLess(settings_pos, theme_pos)

    def test_theme_items_are_radios(self):
        self.assertIn('type: "radio"', self.source)

    def test_theme_click_applies_and_persists(self):
        self.assertIn("applyTheme", self.source)
        self.assertIn("__applyTheme", self.source)

    def test_reads_and_writes_preferences_json(self):
        self.assertIn('".orchestrator"', self.source)
        self.assertIn('"preferences.json"', self.source)

    def test_shell_and_daemon_read_the_same_preferences_file(self):
        """The daemon is this process's child and inherits its environment.

        If the shell ignored the override the daemon honors, the two would read different
        files - the Theme menu showing one choice and the page it themes another.
        """
        from orchestrator import preferences

        self.assertIn(preferences.PREFERENCES_ENV, self.source)


class TestDesktopStartupView(unittest.TestCase):
    """`startup_view` is offered on the Settings page, so something has to act on it.

    It was saved and validated but never read by anything, which made the control on the
    Workspace tab a switch wired to nothing.
    """

    def setUp(self):
        desktop_main = WEB_DIR.parent.parent / "desktop" / "main.js"
        self.source = desktop_main.read_text(encoding="utf-8")

    def test_the_preference_is_read_when_a_project_opens(self):
        self.assertIn("readPreferences().startup_view", self.source)

    def test_every_offered_view_has_a_route(self):
        from orchestrator import preferences

        self.assertIn("STARTUP_ROUTES", self.source)
        for view in preferences.STARTUP_VIEWS:
            self.assertIn("%s:" % view, self.source)

    def test_the_window_no_longer_hardcodes_the_cockpit(self):
        self.assertNotIn("await window.loadURL(`http://127.0.0.1:${port}/`)", self.source)
        self.assertIn("${port}${startup}", self.source)

    def test_an_unknown_view_falls_back_to_the_cockpit(self):
        self.assertIn("|| STARTUP_ROUTES.cockpit", self.source)


class TestSettingsPageTabs(unittest.TestCase):
    def setUp(self):
        self.page = (WEB_DIR / "settings.html").read_text(encoding="utf-8")

    def test_has_six_tabs(self):
        for tab in ("appearance", "workspace", "agents", "execution", "terminal", "safety"):
            self.assertIn(f'data-tab="{tab}"', self.page)

    def test_tabs_use_aria_tablist_roles(self):
        self.assertIn('role="tablist"', self.page)
        self.assertIn('role="tab"', self.page)
        self.assertIn('role="tabpanel"', self.page)

    def test_links_shared_theme_css(self):
        self.assertIn('href="/shared/theme.css"', self.page)

    def test_still_carries_the_token_placeholder(self):
        self.assertIn('name="orchestrator-token"', self.page)
        self.assertIn("__ORCHESTRATOR_TOKEN__", self.page)


class TestAppearanceSection(unittest.TestCase):
    def setUp(self):
        self.page = (WEB_DIR / "settings.html").read_text(encoding="utf-8")

    def test_has_theme_density_font_and_motion_controls(self):
        self.assertIn('id="tab-appearance"', self.page)
        self.assertIn('id="appearance-theme"', self.page)
        self.assertIn('id="appearance-density"', self.page)
        self.assertIn('id="appearance-font-size"', self.page)
        self.assertIn('id="appearance-reduced-motion"', self.page)

    def test_saves_through_preferences_api(self):
        self.assertIn("/api/preferences", self.page)

    def test_defines_apply_theme_hook(self):
        self.assertIn("window.__applyTheme", self.page)


class TestWorkspaceSection(unittest.TestCase):
    def setUp(self):
        self.page = (WEB_DIR / "settings.html").read_text(encoding="utf-8")

    def test_has_default_dir_and_startup_view_controls(self):
        self.assertIn('id="workspace-default-dir"', self.page)
        self.assertIn('id="workspace-startup-view"', self.page)

    def test_recent_projects_is_documented_as_unavailable(self):
        self.assertIn('Recent projects', self.page)
        self.assertIn('unavailable-note', self.page)

    def test_has_a_preview_before_a_delete_button(self):
        preview_pos = self.page.index('id="prune-preview-btn"')
        delete_pos = self.page.index('id="prune-delete-btn"')
        self.assertLess(preview_pos, delete_pos)
        # The delete control must not be usable before a plan exists.
        self.assertIn('id="prune-delete-btn" disabled', self.page)

    def test_prune_execute_sends_the_previewed_plan(self):
        self.assertIn('/api/prune/plan', self.page)
        self.assertIn('/api/prune/execute', self.page)

    def test_prune_reports_branches_the_daemon_spared(self):
        # The daemon re-checks the plan and may refuse part of it; a branch that survived
        # because the repository moved is the outcome worth showing, not a silent count.
        self.assertIn('result.refused', self.page)
        self.assertIn('changed since the preview', self.page)


class TestAgentsProvidersSection(unittest.TestCase):
    def setUp(self):
        self.page = (WEB_DIR / "settings.html").read_text(encoding="utf-8")

    def test_reads_the_team_api(self):
        self.assertIn("/api/team", self.page)

    def test_shows_account_default_when_model_is_unset(self):
        self.assertIn("Account default", self.page)

    def test_links_to_design_for_editing_assignments(self):
        self.assertIn('href="/design"', self.page)

    def test_documents_provider_enable_disable_as_unavailable(self):
        self.assertIn("enable", self.page.lower())
        self.assertIn("Design", self.page)


class TestExecutionSection(unittest.TestCase):
    def setUp(self):
        self.page = (WEB_DIR / "settings.html").read_text(encoding="utf-8")

    def test_has_the_execution_fields(self):
        for field_id in (
            "execution-mode", "execution-retry-attempts", "execution-repair-attempts",
            "execution-escalate", "execution-session-duration", "execution-goal-duration",
        ):
            self.assertIn(f'id="{field_id}"', self.page)

    def test_saves_through_settings_api(self):
        self.assertIn("apiSaveSettings", self.page)

    def test_offers_only_the_execution_modes_the_engine_supports(self):
        for mode in ("auto", "native_tui", "headless"):
            self.assertIn(f'value="{mode}"', self.page)

    def test_labels_zero_duration_as_unlimited(self):
        self.assertIn("0 = unlimited", self.page)

    def test_budget_display_is_a_preference_not_a_setting(self):
        self.assertIn('id="execution-budget-display"', self.page)
        self.assertIn("does not change enforcement", self.page.lower())


class TestTerminalDesktopSection(unittest.TestCase):
    def setUp(self):
        self.page = (WEB_DIR / "settings.html").read_text(encoding="utf-8")

    def test_has_visibility_and_terminal_type(self):
        self.assertIn('id="terminal-visible"', self.page)
        self.assertIn('id="terminal-type"', self.page)

    def test_notes_native_tui_is_the_execution_mode_field(self):
        self.assertIn("same field", self.page.lower())

    def test_has_output_verbosity_mapped_to_run_store(self):
        self.assertIn('id="terminal-output-verbosity"', self.page)
        self.assertIn("run_store.max_output_chars", self.page)

    def test_documents_detached_login_as_read_only(self):
        self.assertIn("detached", self.page.lower())

    def test_offers_every_terminal_type_the_config_accepts(self):
        from orchestrator.settings_patch import SETTINGS_FIELDS

        spec = SETTINGS_FIELDS["execution.terminal_type"]
        for candidate in (
            "auto", "antigravity_integrated", "integrated", "windows_terminal",
            "console", "wt", "cmd", "none",
        ):
            self.assertTrue(spec["validate"](candidate))
            self.assertIn(f'value="{candidate}"', self.page)


class TestCockpitBudgetDisplay(unittest.TestCase):
    def setUp(self):
        self.page = (WEB_DIR / "cockpit.html").read_text(encoding="utf-8")

    def test_status_tokens_honors_budget_display_preference(self):
        self.assertIn("budget_display", self.page)

    def test_hidden_mode_hides_the_counter(self):
        self.assertIn("budgetDisplayMode", self.page)
        self.assertIn("'hidden'", self.page)


class TestSafetyDiagnosticsSection(unittest.TestCase):
    def setUp(self):
        self.page = (WEB_DIR / "settings.html").read_text(encoding="utf-8")

    def test_has_credential_handling_explanation(self):
        self.assertIn("never reads, stores, or transmits a credential", self.page)

    def test_has_doctor_and_diagnostics_buttons(self):
        self.assertIn('id="run-doctor-btn"', self.page)
        self.assertIn('id="generate-diagnostics-btn"', self.page)

    def test_has_a_check_providers_control(self):
        self.assertIn('id="check-providers-btn"', self.page)

    def test_fetches_app_info(self):
        self.assertIn("/api/app_info", self.page)

    def test_reads_doctor_and_diagnostics_from_their_own_routes(self):
        self.assertIn("/api/doctor", self.page)
        self.assertIn("/api/diagnostics", self.page)

    def test_renders_report_text_escaped(self):
        # Doctor and diagnostics text comes from provider CLIs by way of preflight; it reaches
        # innerHTML, so it must go through escapeHtml first, exactly as provider cards do.
        self.assertIn("escapeHtml(data.text)", self.page)

    def test_never_names_a_credential_store_or_field(self):
        # The words "secret" and "bearer" do appear, in the sentence explaining what the
        # redaction filter scrubs - that is the guarantee, not a leak. What must never appear
        # is an identifier that names a credential, a credential file, or a keyring: those
        # would mean the page had started handling one.
        lowered = self.page.lower()
        for banned in ("api_key", "apikey", "api-key", "auth.json", "credentials.json",
                       "keyring", "keychain", "password", "access_token", "refresh_token",
                       "client_secret", "private_key", ".netrc", "authorization:"):
            self.assertNotIn(banned, lowered, f"{banned!r} must never appear on this page")

    def test_status_messages_cancel_their_previous_timer(self):
        # Without this, an earlier "Saved" clears a later error message early - the reason a
        # refused edit briefly lost its only explanation when driven from a browser.
        page = (WEB_DIR / "settings.html").read_text(encoding="utf-8")
        self.assertIn("clearTimeout(statusTimers.get(elementId))", page)

    def test_has_no_input_that_could_collect_a_secret(self):
        self.assertNotIn('type="password"', self.page)

    def test_sends_the_daemon_token_only_as_a_header(self):
        # The launch token reaches the page in the document and goes back in a header. A
        # query-string token would put it in history and in any referrer.
        self.assertIn("'X-Orchestrator-Token': TOKEN", self.page)
        self.assertNotIn("token=", self.page)


if __name__ == "__main__":
    unittest.main()
