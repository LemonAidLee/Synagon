"""Tests for orchestrator/preferences.py (Package I).

Shaped like `native_sessions.py`'s own test style: a temp-file override for the store's
location, defaults when nothing is saved, and an atomic write that never leaves a half-written
file behind.
"""

import json
import os
import tempfile
import unittest

from orchestrator import preferences


class PreferencesTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="orch_prefs_")
        self.path = os.path.join(self.tmpdir, "preferences.json")
        self._old_env = os.environ.get(preferences.PREFERENCES_ENV)
        os.environ[preferences.PREFERENCES_ENV] = self.path
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        if self._old_env is None:
            os.environ.pop(preferences.PREFERENCES_ENV, None)
        else:
            os.environ[preferences.PREFERENCES_ENV] = self._old_env

    def test_load_with_no_file_returns_defaults(self):
        self.assertEqual(preferences.load_preferences(), preferences.DEFAULT_PREFERENCES)

    def test_save_then_load_round_trips(self):
        preferences.save_preferences({"theme": "theme-moonfly", "density": "compact"})
        loaded = preferences.load_preferences()
        self.assertEqual(loaded["theme"], "theme-moonfly")
        self.assertEqual(loaded["density"], "compact")
        self.assertEqual(loaded["terminal_font_size"], 13)

    def test_save_merges_rather_than_replaces(self):
        preferences.save_preferences({"theme": "theme-tender"})
        preferences.save_preferences({"density": "compact"})
        loaded = preferences.load_preferences()
        self.assertEqual(loaded["theme"], "theme-tender")
        self.assertEqual(loaded["density"], "compact")

    def test_unknown_key_is_refused_and_nothing_is_written(self):
        with self.assertRaises(preferences.PreferencesValidationError):
            preferences.save_preferences({"not_a_real_preference": True})
        self.assertFalse(os.path.exists(self.path))

    def test_out_of_range_font_size_is_refused(self):
        with self.assertRaises(preferences.PreferencesValidationError):
            preferences.save_preferences({"terminal_font_size": 999})

    def test_bad_theme_name_is_refused(self):
        with self.assertRaises(preferences.PreferencesValidationError):
            preferences.save_preferences({"theme": "theme-nonexistent"})

    def test_bad_boolean_field_is_refused(self):
        with self.assertRaises(preferences.PreferencesValidationError):
            preferences.save_preferences({"reduced_motion": "yes"})

    def test_negative_prune_age_is_refused(self):
        with self.assertRaises(preferences.PreferencesValidationError):
            preferences.save_preferences({"prune_default_max_age_days": -1})

    def test_write_is_atomic_via_tmp_file(self):
        preferences.save_preferences({"theme": "theme-miasma"})
        self.assertFalse(os.path.exists(self.path + ".tmp"))
        with open(self.path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        self.assertEqual(data["theme"], "theme-miasma")


if __name__ == "__main__":
    unittest.main()
