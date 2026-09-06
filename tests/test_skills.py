"""Unit and integration test suite for Stage 7.7 Global Skill Awareness & Access."""

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from orchestrator.config import (
    ConfigValidationError,
    validate_config,
    get_skill_config,
    DEFAULT_CONFIG,
)
from orchestrator.graph import (
    OrchestratorState,
    context_node,
    make_role_node,
)
from orchestrator.metrics import format_skills_summary, format_summary_table
from orchestrator.skills.discovery import (
    discover_skills,
    parse_skill_metadata,
    is_drive_root,
)
from orchestrator.skills.registry import SkillRegistry, format_skill_manifest
from orchestrator.skills.types import SkillInfo
from orchestrator.types import create_agent_result


class TestSkillDiscovery(unittest.TestCase):
    """Tests for passive skill discovery, parsing, and normalization."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name).resolve()
        self.skills_dir = self.workspace / "skills"
        self.skills_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_skill_discovery_valid_frontmatter(self):
        """Discovers a skill with valid YAML frontmatter and resource directories."""
        renderer_dir = self.skills_dir / "3d-renderer"
        renderer_dir.mkdir()
        (renderer_dir / "scripts").mkdir()
        (renderer_dir / "templates").mkdir()
        (renderer_dir / "SKILL.md").write_text(
            "---\n"
            "name: 3d-renderer\n"
            "description: Create and manipulate 3D scenes.\n"
            "version: 1.2.0\n"
            "---\n\n"
            "# 3D Renderer Skill\n"
            "Use this skill for threejs workflows.",
            encoding="utf-8",
        )

        skills = discover_skills(str(self.workspace), search_paths=["./skills"])
        self.assertEqual(len(skills), 1)
        skill = skills[0]
        self.assertEqual(skill["name"], "3d-renderer")
        self.assertEqual(skill["description"], "Create and manipulate 3D scenes.")
        self.assertEqual(skill["version"], "1.2.0")
        self.assertEqual(Path(skill["location"]).resolve(), renderer_dir.resolve())
        self.assertEqual(Path(skill["instructions_path"]).resolve(), (renderer_dir / "SKILL.md").resolve())
        self.assertIn("scripts", skill["resources"])
        self.assertIn("templates", skill["resources"])

    def test_skill_discovery_markdown_fallback(self):
        """Discovers a skill without YAML frontmatter, extracting name and description from markdown body."""
        data_dir = self.skills_dir / "data-viz"
        data_dir.mkdir()
        (data_dir / "skill.md").write_text(
            "# Data Visualization\n\n"
            "Create charting and data exploration visualizers with matplotlib.\n\n"
            "Detailed usage instructions...",
            encoding="utf-8",
        )

        skills = discover_skills(str(self.workspace), search_paths=["./skills"])
        self.assertEqual(len(skills), 1)
        skill = skills[0]
        self.assertEqual(skill["name"], "data-visualization")
        self.assertEqual(
            skill["description"],
            "Create charting and data exploration visualizers with matplotlib.",
        )
        self.assertIsNone(skill.get("version"))

    def test_skill_discovery_multiple_and_deterministic_order(self):
        """Discovers multiple skills ordered deterministically by name."""
        for name in ["zeta-tool", "alpha-builder", "gamma-analyzer"]:
            s_dir = self.skills_dir / name
            s_dir.mkdir()
            (s_dir / "SKILL.md").write_text(
                f"---\nname: {name}\ndescription: {name} capability.\n---\n# {name}",
                encoding="utf-8",
            )

        skills = discover_skills(str(self.workspace), search_paths=["./skills"])
        self.assertEqual(len(skills), 3)
        self.assertEqual([s["name"] for s in skills], ["alpha-builder", "gamma-analyzer", "zeta-tool"])

    def test_skill_discovery_duplicate_handling(self):
        """Deduplicates skills found across multiple search paths in priority order."""
        extra_dir = self.workspace / "extra_skills"
        extra_dir.mkdir()

        # Skill in first search path
        s1 = self.skills_dir / "shared-skill"
        s1.mkdir()
        (s1 / "SKILL.md").write_text(
            "---\nname: shared-skill\ndescription: Primary version.\n---\n",
            encoding="utf-8",
        )

        # Duplicate in second search path
        s2 = extra_dir / "shared-skill"
        s2.mkdir()
        (s2 / "SKILL.md").write_text(
            "---\nname: shared-skill\ndescription: Duplicate secondary version.\n---\n",
            encoding="utf-8",
        )

        skills = discover_skills(
            str(self.workspace),
            search_paths=["./skills", "./extra_skills"],
        )
        self.assertEqual(len(skills), 1)
        self.assertEqual(skills[0]["name"], "shared-skill")
        self.assertEqual(skills[0]["description"], "Primary version.")

    def test_skill_discovery_malformed_skill_does_not_crash(self):
        """Handles corrupted/malformed skills gracefully without crashing."""
        # Broken YAML frontmatter
        broken_dir = self.skills_dir / "broken-skill"
        broken_dir.mkdir()
        (broken_dir / "SKILL.md").write_text(
            "---\n[this is invalid: yaml: {bad: [unclosed\n---\n# Broken Skill\nFallback desc.",
            encoding="utf-8",
        )

        # Directory without SKILL.md
        empty_skill_dir = self.skills_dir / "not-a-skill"
        empty_skill_dir.mkdir()

        skills = discover_skills(str(self.workspace), search_paths=["./skills"])
        # Should gracefully fall back to markdown parsing for broken-skill
        self.assertEqual(len(skills), 1)
        self.assertEqual(skills[0]["name"], "broken-skill")
        self.assertIn("Fallback desc", skills[0]["description"])

    def test_skill_discovery_empty_or_missing_directory(self):
        """Returns empty list when skills directory does not exist or has no skills."""
        empty_workspace = self.workspace / "empty_proj"
        empty_workspace.mkdir()
        skills = discover_skills(str(empty_workspace), search_paths=["./skills"])
        self.assertEqual(skills, [])

    def test_skill_discovery_disabled(self):
        """Returns empty list when discovery is disabled."""
        s_dir = self.skills_dir / "valid-skill"
        s_dir.mkdir()
        (s_dir / "SKILL.md").write_text("---\nname: valid-skill\n---\n", encoding="utf-8")

        skills = discover_skills(str(self.workspace), search_paths=["./skills"], enabled=False)
        self.assertEqual(skills, [])

    def test_skill_security_drive_root_protection(self):
        """Verifies drive root paths like C:\\ or D:\\ are rejected."""
        self.assertTrue(is_drive_root(Path("C:/")))
        self.assertTrue(is_drive_root(Path("C:\\")))
        self.assertTrue(is_drive_root(Path("/")))
        self.assertFalse(is_drive_root(self.skills_dir))


class TestSkillConfig(unittest.TestCase):
    """Tests for validating skill configuration in orchestrator.yaml."""

    def test_default_skill_config(self):
        """Verifies default skill configuration settings."""
        skill_cfg = get_skill_config(DEFAULT_CONFIG)
        self.assertTrue(skill_cfg.get("enabled"))
        self.assertEqual(skill_cfg.get("search_paths"), ["./skills", "./.agents/skills"])

    def test_valid_custom_skill_config(self):
        """Validates custom skills section in configuration."""
        raw = {
            "agents": [{"agent": "antigravity", "role": "researcher"}],
            "roles": {"researcher": {"responsibility": "research"}},
            "skills": {
                "enabled": True,
                "search_paths": ["./my_skills", "./custom_tools/skills"],
            },
        }
        validated = validate_config(raw)
        self.assertIn("skills", validated)
        self.assertTrue(validated["skills"]["enabled"])
        self.assertEqual(validated["skills"]["search_paths"], ["./my_skills", "./custom_tools/skills"])

    def test_invalid_skills_type(self):
        """Rejects non-dictionary skills configuration."""
        raw = {
            "agents": [{"agent": "antigravity", "role": "researcher"}],
            "roles": {"researcher": {"responsibility": "research"}},
            "skills": "enabled",
        }
        with self.assertRaises(ConfigValidationError):
            validate_config(raw)

    def test_invalid_skills_enabled_type(self):
        """Rejects non-boolean skills.enabled."""
        raw = {
            "agents": [{"agent": "antigravity", "role": "researcher"}],
            "roles": {"researcher": {"responsibility": "research"}},
            "skills": {"enabled": "yes"},
        }
        with self.assertRaises(ConfigValidationError):
            validate_config(raw)

    def test_invalid_skills_search_paths_type(self):
        """Rejects non-list skills.search_paths."""
        raw = {
            "agents": [{"agent": "antigravity", "role": "researcher"}],
            "roles": {"researcher": {"responsibility": "research"}},
            "skills": {"search_paths": "./skills"},
        }
        with self.assertRaises(ConfigValidationError):
            validate_config(raw)

    def test_invalid_skills_search_paths_entries(self):
        """Rejects empty string entries in skills.search_paths."""
        raw = {
            "agents": [{"agent": "antigravity", "role": "researcher"}],
            "roles": {"researcher": {"responsibility": "research"}},
            "skills": {"search_paths": ["./skills", "   "]},
        }
        with self.assertRaises(ConfigValidationError):
            validate_config(raw)


class TestSkillManifestAndRegistry(unittest.TestCase):
    """Tests for SkillRegistry, token-efficient manifest generation, and reference detection."""

    def setUp(self):
        self.skills: list[SkillInfo] = [
            {
                "name": "3d-renderer",
                "description": "Create and manipulate 3D scenes.",
                "location": "D:/proj/skills/3d-renderer",
                "instructions_path": "D:/proj/skills/3d-renderer/SKILL.md",
                "version": "1.0.0",
                "resources": ["scripts", "templates"],
            },
            {
                "name": "database-migration",
                "description": "Manage database migrations with alembic.",
                "location": "D:/proj/skills/database-migration",
                "instructions_path": "D:/proj/skills/database-migration/SKILL.md",
                "version": None,
                "resources": [],
            },
        ]
        self.registry = SkillRegistry(self.skills)

    def test_manifest_formatting_token_efficiency(self):
        """Verifies manifest includes metadata, path references, but NOT full file contents."""
        manifest = self.registry.format_manifest()
        self.assertIn("AVAILABLE SKILLS:", manifest)
        self.assertIn("1. 3d-renderer", manifest)
        self.assertIn("Description: Create and manipulate 3D scenes.", manifest)
        self.assertIn("Location: D:/proj/skills/3d-renderer", manifest)
        self.assertIn("Instructions: D:/proj/skills/3d-renderer/SKILL.md", manifest)
        self.assertIn("Resources: scripts, templates", manifest)
        self.assertIn("2. database-migration", manifest)

    def test_manifest_empty_when_no_skills(self):
        """Generates clear empty notice when no skills exist."""
        empty_reg = SkillRegistry([])
        manifest = empty_reg.format_manifest()
        self.assertIn("No skills available", manifest)

    def test_registry_lookup(self):
        """Verifies retrieving skill by normalized name."""
        skill = self.registry.get("3D-RENDERER")
        self.assertIsNotNone(skill)
        self.assertEqual(skill["name"], "3d-renderer")

        missing = self.registry.get("non-existent")
        self.assertIsNone(missing)

    def test_detect_referenced_skills(self):
        """Detects skills mentioned by name with word-boundary accuracy."""
        text = "We should use the 3d-renderer skill to build the viewport."
        refs = self.registry.detect_referenced_skills(text)
        self.assertEqual(refs, ["3d-renderer"])

        # Should not falsely match partial substrings
        partial_text = "Let's 3d-renderers something else."
        refs_partial = self.registry.detect_referenced_skills(partial_text)
        self.assertEqual(refs_partial, [])

    def test_skills_summary_reporting(self):
        """Verifies summary reporting of available and referenced skills."""
        results = [
            create_agent_result(
                agent="claude",
                role="planner",
                status="success",
                output="I recommend using database-migration for this task.",
            )
        ]
        summary = format_skills_summary(self.skills, results)
        self.assertIn("SKILLS", summary)
        self.assertIn("Available:   3d-renderer, database-migration", summary)
        self.assertIn("Referenced:  database-migration", summary)

    def test_format_summary_table_includes_skills(self):
        """Verifies format_summary_table incorporates skills section when provided."""
        results = [
            create_agent_result(
                agent="claude",
                role="planner",
                status="success",
                output="No skills needed.",
            )
        ]
        table = format_summary_table(
            agent_results=results,
            verification_verdict="PASS",
            skills=self.skills,
        )
        self.assertIn("Available:   3d-renderer, database-migration", table)
        self.assertIn("Referenced:  None", table)


class TestRoleIndependentSkillAwareness(unittest.TestCase):
    """Tests ensuring ALL agent roles receive skill awareness without role-based gating."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name).resolve()
        self.skills_dir = self.workspace / "skills"
        self.skills_dir.mkdir(parents=True, exist_ok=True)

        mock_skill = self.skills_dir / "test-skill"
        mock_skill.mkdir()
        (mock_skill / "SKILL.md").write_text(
            "---\nname: test-skill\ndescription: Test capability.\n---\n# Test",
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_context_node_discovers_skills_and_populates_state(self):
        """Verifies context_node discovers skills relative to project_root and freezes manifest."""
        state: OrchestratorState = {
            "task": "Test task",
            "project_root": str(self.workspace),
        }
        res = context_node(state)
        self.assertIn("skills", res)
        self.assertIn("skill_manifest", res)
        self.assertEqual(len(res["skills"]), 1)
        self.assertEqual(res["skills"][0]["name"], "test-skill")
        self.assertIn("test-skill", res["skill_manifest"])

    @patch("orchestrator.graph.run_antigravity")
    def test_researcher_receives_skill_manifest(self, mock_antigravity):
        """Verifies Antigravity researcher receives the skill manifest in its prompt."""
        mock_antigravity.return_value = ("Research findings mentioning test-skill", None)

        state: OrchestratorState = {
            "task": "Research something",
            "project_root": str(self.workspace),
            "project_context": "Sample project context",
            "skill_manifest": "AVAILABLE SKILLS:\n1. test-skill\n   Description: Test capability.",
            "config": {"agents": [{"role": "researcher", "agent": "antigravity"}]},
        }
        antigravity_node = make_role_node("researcher")
        antigravity_node(state)

        prompt_sent = mock_antigravity.call_args[0][0]
        self.assertIn("AVAILABLE SKILLS:", prompt_sent)
        self.assertIn("test-skill", prompt_sent)
        self.assertIn("Skill Instructions for Researcher:", prompt_sent)

    @patch("orchestrator.graph.run_claude_code")
    def test_planner_receives_skill_manifest(self, mock_claude):
        """Verifies Claude planner receives the skill manifest in its prompt."""
        mock_claude.return_value = ("Planning recommendations", None)

        state: OrchestratorState = {
            "task": "Plan something",
            "project_root": str(self.workspace),
            "project_context": "Sample project context",
            "analysis": "Initial analysis",
            "skill_manifest": "AVAILABLE SKILLS:\n1. test-skill\n   Description: Test capability.",
            "config": {"agents": [{"role": "planner", "agent": "claude"}]},
        }
        claude_node = make_role_node("planner")
        claude_node(state)

        prompt_sent = mock_claude.call_args[0][0]
        self.assertIn("AVAILABLE SKILLS:", prompt_sent)
        self.assertIn("test-skill", prompt_sent)
        self.assertIn("Skill Instructions for Planner:", prompt_sent)

    @patch("orchestrator.graph.run_opencode")
    def test_implementer_receives_skill_manifest_with_inspection_rules(self, mock_opencode):
        """Verifies OpenCode implementer receives the skill manifest and inspection guidelines."""
        mock_opencode.return_value = ("Implemented changes", None)

        state: OrchestratorState = {
            "task": "Implement something",
            "project_root": str(self.workspace),
            "project_context": "Sample project context",
            "analysis": "Analysis",
            "review": "Plan",
            "skill_manifest": "AVAILABLE SKILLS:\n1. test-skill\n   Instructions: ./skills/test-skill/SKILL.md",
            "config": {"agents": [{"role": "implementer", "agent": "opencode"}]},
        }
        opencode_node = make_role_node("implementer")
        opencode_node(state)

        prompt_sent = mock_opencode.call_args[0][0]
        self.assertIn("AVAILABLE SKILLS:", prompt_sent)
        self.assertIn("test-skill", prompt_sent)
        self.assertIn("Skill Instructions for Implementer:", prompt_sent)
        self.assertIn("Inspect its actual instructions at the specified path", prompt_sent)

    @patch("orchestrator.graph.run_claude_code")
    def test_verifier_receives_skill_manifest(self, mock_claude):
        """Verifies Claude verifier receives the skill manifest in its verification prompt."""
        mock_claude.return_value = ("VERDICT: PASS\nSummary: Verified with skill compliance", None)

        state: OrchestratorState = {
            "task": "Verify something",
            "project_root": str(self.workspace),
            "project_context": "Sample project context",
            "analysis": "Analysis",
            "review": "Plan",
            "implementation": "Code implemented",
            "skill_manifest": "AVAILABLE SKILLS:\n1. test-skill\n   Description: Test capability.",
            "config": {"agents": [{"role": "verifier", "agent": "claude"}]},
        }
        verifier_node = make_role_node("verifier")
        verifier_node(state)

        prompt_sent = mock_claude.call_args[0][0]
        self.assertIn("AVAILABLE SKILLS:", prompt_sent)
        self.assertIn("test-skill", prompt_sent)
        self.assertIn("Skill Compliance: Consider whether relevant available skills were used", prompt_sent)

    @patch("orchestrator.graph.run_opencode")
    def test_repair_implementer_receives_skill_manifest(self, mock_opencode):
        """Verifies OpenCode repair agent receives the skill manifest in its repair prompt."""
        mock_opencode.return_value = ("Repaired code", None)

        state: OrchestratorState = {
            "task": "Fix something",
            "project_root": str(self.workspace),
            "project_context": "Sample project context",
            "review": "Plan",
            "implementation": "Broken code",
            "verification": "VERDICT: FAIL\nDid not use test-skill.",
            "repair_attempts": 0,
            "max_repair_attempts": 2,
            "skill_manifest": "AVAILABLE SKILLS:\n1. test-skill\n   Description: Test capability.",
            "config": {"agents": [{"role": "implementer", "agent": "opencode"}]},
        }
        opencode_repair_node = make_role_node("implementer", is_repair=True)
        opencode_repair_node(state)

        prompt_sent = mock_opencode.call_args[0][0]
        self.assertIn("AVAILABLE SKILLS:", prompt_sent)
        self.assertIn("test-skill", prompt_sent)
        self.assertIn("If verification failed due to missing or improper skill usage", prompt_sent)


if __name__ == "__main__":
    unittest.main()
