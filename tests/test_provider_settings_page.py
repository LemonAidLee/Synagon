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
        self.assertIn("/api/control/provider_login", self.page)

    def test_has_a_container_for_provider_cards(self):
        self.assertIn('id="provider-cards"', self.page)

    def test_has_login_and_check_again_actions(self):
        self.assertIn("Login", self.page)
        self.assertIn("Check again", self.page)

    def test_names_all_three_providers_in_script(self):
        for provider in ("claude", "opencode", "antigravity"):
            self.assertIn(provider, self.page)


class TestCockpitLinksToSettings(unittest.TestCase):
    def test_cockpit_header_links_to_settings(self):
        page = (WEB_DIR / "cockpit.html").read_text(encoding="utf-8")
        self.assertIn('href="/settings"', page)


if __name__ == "__main__":
    unittest.main()
