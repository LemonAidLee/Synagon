"""Tests for orchestrator/settings_patch.py (Package I).

Mirrors how teams.py is tested: a real orchestrator.yaml on disk, a surgical write that must
leave every untouched byte alone, and a refusal that leaves the file exactly as it was.
"""

import shutil
import tempfile
import unittest
from pathlib import Path

from orchestrator.config import load_config
from orchestrator.settings_patch import current_settings, validate_patch, write_settings

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


class SettingsPatchTestCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="orch_settings_patch_")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.path = Path(self.root) / "orchestrator.yaml"
        self.path.write_text(MINIMAL_YAML, encoding="utf-8")
        self.config = load_config(str(self.path))

    def test_current_settings_reads_defaults_when_unset(self):
        values = current_settings(self.config)
        self.assertEqual(values["execution.agent_execution_mode"], "auto")
        self.assertEqual(values["max_repair_attempts"], 2)
        self.assertEqual(values["budget.max_duration_seconds"], 0)
        self.assertEqual(values["run_store.max_output_chars"], 0)

    def test_validate_patch_rejects_unknown_field(self):
        problems = validate_patch({"not_a_field": 1})
        self.assertEqual(len(problems), 1)

    def test_validate_patch_rejects_out_of_range_retry(self):
        problems = validate_patch({"execution.retry.attempts": 999})
        self.assertEqual(len(problems), 1)

    def test_validate_patch_accepts_a_good_value(self):
        self.assertEqual(validate_patch({"execution.agent_execution_mode": "headless"}), [])

    def test_write_settings_patches_one_field_and_preserves_others(self):
        result = write_settings(
            self.root, self.config, {"execution.agent_execution_mode": "headless"},
        )
        self.assertTrue(result["ok"], result.get("error"))
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("agent_execution_mode: headless", text)
        self.assertIn("write the code", text)

        reloaded = load_config(str(self.path))
        self.assertEqual(reloaded["execution"]["agent_execution_mode"], "headless")

    def test_write_settings_preserves_unrelated_retry_fields(self):
        write_settings(self.root, self.config, {"execution.retry.attempts": 5})
        reloaded = load_config(str(self.path))
        self.assertEqual(reloaded["execution"]["retry"]["attempts"], 5)
        # escalate_model was never touched, so it keeps the engine's default.
        self.assertTrue(reloaded["execution"]["retry"]["escalate_model"])

    def test_write_settings_refuses_invalid_patch_and_writes_nothing(self):
        original = self.path.read_text(encoding="utf-8")
        result = write_settings(self.root, self.config, {"max_repair_attempts": -1})
        self.assertFalse(result["ok"])
        self.assertEqual(self.path.read_text(encoding="utf-8"), original)

    def test_write_settings_leaves_a_backup(self):
        write_settings(self.root, self.config, {"max_repair_attempts": 5})
        backups = list(Path(self.root).glob("orchestrator.yaml.bak-*"))
        self.assertEqual(len(backups), 1)

    def test_write_settings_no_op_when_value_unchanged(self):
        write_settings(self.root, self.config, {"max_repair_attempts": 2})
        text_before = self.path.read_text(encoding="utf-8")
        result = write_settings(self.root, self.config, {"max_repair_attempts": 2})
        self.assertTrue(result.get("unchanged"))
        self.assertEqual(self.path.read_text(encoding="utf-8"), text_before)


if __name__ == "__main__":
    unittest.main()
