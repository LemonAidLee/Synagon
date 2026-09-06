"""Unit tests for the project context collection module."""

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from orchestrator.context import (
    get_project_root,
    collect_project_context,
    EXCLUDED_DIRS,
)


class TestProjectContext(unittest.TestCase):
    """Test suite for safe workspace context collection and secret protection."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="test_context_")
        self.root_path = Path(self.temp_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_project_root_detection(self):
        """Test detection and validation of the project root directory."""
        detected = get_project_root(self.temp_dir)
        self.assertEqual(Path(detected).resolve(), self.root_path.resolve())

        # Test non-existent path
        with self.assertRaises(FileNotFoundError):
            get_project_root(os.path.join(self.temp_dir, "non_existent_subdir"))

    def test_env_contents_never_exposed(self):
        """CRITICAL: Ensure secret values in .env and related files are NEVER included in context."""
        secret_value = "SECRET_SUPER_CONFIDENTIAL_KEY_987654321"
        another_secret = "OAUTH_REFRESH_TOKEN_ABCD_EFGH"

        # Create sensitive files
        env_file = self.root_path / ".env"
        env_file.write_text(f"API_KEY={secret_value}\nPASSWORD=topsecret\n", encoding="utf-8")

        env_local = self.root_path / ".env.local"
        env_local.write_text(f"AUTH_TOKEN={another_secret}\n", encoding="utf-8")

        context = collect_project_context(self.temp_dir)

        # Assert secrets are strictly absent
        self.assertNotIn(secret_value, context, "Security violation: .env secret leaked into context!")
        self.assertNotIn(another_secret, context, "Security violation: .env.local secret leaked into context!")
        self.assertNotIn("topsecret", context, "Security violation: .env password leaked into context!")

        # Assert files are identified as redacted
        self.assertIn(".env (present - contents redacted for security)", context)
        self.assertIn(".env.local (present - contents redacted for security)", context)

    def test_excluded_directories(self):
        """Test that .venv, .git, and __pycache__ are excluded from project context."""
        # Create directories that should be excluded
        (self.root_path / ".git").mkdir()
        (self.root_path / ".venv").mkdir()
        (self.root_path / "__pycache__").mkdir()
        (self.root_path / "build").mkdir()

        # Create a regular directory that should be included
        (self.root_path / "orchestrator").mkdir()
        (self.root_path / "tests").mkdir()

        context = collect_project_context(self.temp_dir)

        self.assertIn("orchestrator", context)
        self.assertIn("tests", context)

        # Check top-level directories line does not contain excluded directories
        for line in context.splitlines():
            if line.startswith("- **Top-Level Directories**"):
                self.assertNotIn(".git", line)
                self.assertNotIn(".venv", line)
                self.assertNotIn("__pycache__", line)
                self.assertNotIn("build", line)

    def test_relevant_project_files_included(self):
        """Test that relevant configuration and dependency information appears in context."""
        # Create requirements.txt
        req_file = self.root_path / "requirements.txt"
        req_file.write_text("langgraph>=1.2.0\ncolorama==0.4.6\n# A comment\npython-dotenv\n", encoding="utf-8")

        # Create langgraph.json
        lg_json = self.root_path / "langgraph.json"
        lg_json.write_text('{"graphs": {"orchestrator": "./orchestrator/graph.py:graph"}, "dependencies": ["."]}', encoding="utf-8")

        context = collect_project_context(self.temp_dir)

        self.assertIn("requirements.txt", context)
        self.assertIn("langgraph.json", context)
        self.assertIn("langgraph", context)
        self.assertIn("colorama", context)
        self.assertIn("python-dotenv", context)
        self.assertIn("./orchestrator/graph.py:graph", context)

    def test_live_workspace_context_collection(self):
        """Test context collection against the real project root directory."""
        cwd = os.getcwd()
        context = collect_project_context(cwd)

        self.assertIn("Project Context", context)
        self.assertIn("orchestrator", context)
        self.assertIn("requirements.txt", context)
        self.assertNotIn(".venv (present", context)


if __name__ == "__main__":
    unittest.main()
