"""Unit tests for Orchestrator configuration loading, validation, and role/model resolution."""

import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from orchestrator.config import (
    AgentConfig,
    ModelConfig,
    RoleConfig,
    OrchestratorConfig,
    ConfigValidationError,
    DEFAULT_CONFIG,
    load_config,
    validate_config,
    get_agent_config,
    get_role_responsibility,
    get_available_models,
    validate_model,
    get_model_display_name,
)
from orchestrator.graph import graph
from orchestrator.tracer import default_tracer


class TestConfig(unittest.TestCase):
    """Test suite covering configuration loading, validation, and resolution."""

    def test_load_default_project_config(self):
        """Test loading the default orchestrator.yaml from the project root."""
        config = load_config(project_root=os.getcwd())
        self.assertIsInstance(config, dict)
        self.assertGreaterEqual(len(config["agents"]), 2)
        self.assertIn("researcher", config["roles"])
        self.assertIn("planner", config["roles"])

        # Check default agent mappings
        agy_cfg = get_agent_config(config, agent="antigravity")
        self.assertIsNotNone(agy_cfg)
        self.assertEqual(agy_cfg["role"], "researcher")
        self.assertEqual(agy_cfg["model"], "gemini-3.8-flash-high")

        claude_cfg = get_agent_config(config, agent="claude")
        self.assertIsNotNone(claude_cfg)
        self.assertEqual(claude_cfg["role"], "planner")
        self.assertEqual(claude_cfg["model"], "sonnet")

    def test_load_explicit_missing_file_raises(self):
        """Test that an explicitly specified missing config file raises FileNotFoundError."""
        with self.assertRaises(FileNotFoundError):
            load_config("nonexistent_path_to_config.yaml")

    def test_load_missing_file_fallback_to_default(self):
        """Test that when orchestrator.yaml is absent in search dir, it falls back to DEFAULT_CONFIG."""
        with tempfile.TemporaryDirectory() as empty_dir:
            config = load_config(project_root=empty_dir)
            self.assertEqual(len(config["agents"]), len(DEFAULT_CONFIG["agents"]))
            self.assertEqual(config["roles"].keys(), DEFAULT_CONFIG["roles"].keys())
            self.assertIn("antigravity", config["models"])
            self.assertIn("claude", config["models"])
            self.assertIn("opencode", config["models"])

    def test_load_custom_yaml_file(self):
        """Test loading a user-edited custom YAML configuration."""
        yaml_content = (
            "agents:\n"
            "  - agent: claude\n"
            "    model: opus\n"
            "    role: planner\n"
            "  - agent: antigravity\n"
            "    model: gemini-3.7-flash-high\n"
            "    role: researcher\n"
            "roles:\n"
            "  planner:\n"
            "    responsibility: Custom planning instructions\n"
            "  researcher:\n"
            "    responsibility: Custom research instructions\n"
        )
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as tf:
            tf.write(yaml_content)
            temp_path = tf.name

        try:
            config = load_config(temp_path)
            self.assertEqual(len(config["agents"]), 2)
            self.assertEqual(get_role_responsibility(config, "planner"), "Custom planning instructions")
            self.assertEqual(get_role_responsibility(config, "researcher"), "Custom research instructions")

            claude_cfg = get_agent_config(config, agent="claude")
            self.assertIsNotNone(claude_cfg)
            self.assertEqual(claude_cfg["model"], "opus")

            agy_cfg = get_agent_config(config, agent="antigravity")
            self.assertIsNotNone(agy_cfg)
            self.assertEqual(agy_cfg["model"], "gemini-3.7-flash-high")
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    def test_invalid_config_not_a_mapping(self):
        """Test validation failure when YAML root is not a dictionary."""
        with self.assertRaises(ValueError) as ctx:
            validate_config(["not", "a", "dictionary"])
        self.assertIn("must be a dictionary/mapping", str(ctx.exception))

    def test_invalid_config_missing_agents_section(self):
        """Test validation failure when 'agents' section is missing."""
        with self.assertRaises(ValueError) as ctx:
            validate_config({"roles": {"planner": {"responsibility": "Do plan"}}})
        self.assertIn("must define a non-empty 'agents' list", str(ctx.exception))

    def test_invalid_config_missing_roles_section(self):
        """Test validation failure when 'roles' section is missing."""
        with self.assertRaises(ValueError) as ctx:
            validate_config({"agents": [{"agent": "claude", "role": "planner"}]})
        self.assertIn("must define a 'roles' dictionary", str(ctx.exception))

    def test_invalid_config_missing_agent_fields(self):
        """Test validation failure when agent item is missing required keys."""
        with self.assertRaises(ValueError) as ctx:
            validate_config({
                "agents": [{"model": "sonnet"}],
                "roles": {"planner": {"responsibility": "Plan"}},
            })
        self.assertIn("missing required non-empty 'agent' field", str(ctx.exception))

    def test_invalid_config_missing_role_responsibility(self):
        """Test validation failure when role is missing 'responsibility'."""
        with self.assertRaises(ValueError) as ctx:
            validate_config({
                "agents": [{"agent": "claude", "role": "planner"}],
                "roles": {"planner": {"other_field": "test"}},
            })
        self.assertIn("must be a dictionary containing a 'responsibility' field", str(ctx.exception))

    def test_role_responsibility_resolution(self):
        """Test get_role_responsibility helper."""
        cfg = OrchestratorConfig(
            agents=[AgentConfig(agent="claude", role="planner", model="sonnet")],
            roles={"planner": RoleConfig(responsibility="Evaluate architecture")},
        )
        resp = get_role_responsibility(cfg, "planner")
        self.assertEqual(resp, "Evaluate architecture")

        # Unknown role falls back to generic string
        unknown_resp = get_role_responsibility(cfg, "unknown_role")
        self.assertIn("unknown_role", unknown_resp)

    def test_agent_model_selection_helpers(self):
        """Test get_agent_config querying by role and agent."""
        cfg = OrchestratorConfig(
            agents=[
                AgentConfig(agent="antigravity", role="researcher", model="gemini-3.8-flash-high"),
                AgentConfig(agent="claude", role="planner", model="sonnet"),
                AgentConfig(agent="opencode", role="implementer", model="deepseek-coder"),
            ],
            roles={},
        )
        # By role
        impl = get_agent_config(cfg, role="implementer")
        self.assertIsNotNone(impl)
        self.assertEqual(impl["agent"], "opencode")
        self.assertEqual(impl["model"], "deepseek-coder")

        # By agent
        agy = get_agent_config(cfg, agent="antigravity")
        self.assertIsNotNone(agy)
        self.assertEqual(agy["model"], "gemini-3.8-flash-high")
        self.assertEqual(agy["role"], "researcher")


class TestModelCatalog(unittest.TestCase):
    """Test suite covering model catalog structure, helpers, and validation."""

    def test_model_catalog_loaded_from_project_config(self):
        """Verify models catalog contains antigravity, claude, and opencode sections with official IDs."""
        config = load_config(project_root=os.getcwd())
        models = config.get("models", {})

        self.assertIn("antigravity", models)
        self.assertIn("claude", models)
        self.assertIn("opencode", models)

        agy_ids = [m["id"] for m in models["antigravity"]]
        self.assertIn("gemini-3.8-flash-high", agy_ids)
        self.assertIn("gemini-3.7-flash-high", agy_ids)
        self.assertIn("claude-sonnet-4-6", agy_ids)

        claude_ids = [m["id"] for m in models["claude"]]
        self.assertIn("sonnet", claude_ids)
        self.assertIn("opus", claude_ids)
        self.assertIn("haiku", claude_ids)

        opencode_ids = [m["id"] for m in models["opencode"]]
        self.assertIn("opencode/gpt-5.1-codex", opencode_ids)
        self.assertIn("anthropic/claude-sonnet-4-5", opencode_ids)
        self.assertIn("google/gemini-3-pro", opencode_ids)

    def test_get_available_models_helper(self):
        """Test get_available_models retrieves configured models for an agent."""
        config = load_config(project_root=os.getcwd())
        claude_models = get_available_models(config, "claude")
        self.assertGreaterEqual(len(claude_models), 3)
        self.assertTrue(any(m["id"] == "sonnet" for m in claude_models))

    def test_validate_model_helper(self):
        """Test validate_model correctly identifies valid and invalid models."""
        config = load_config(project_root=os.getcwd())
        self.assertTrue(validate_model(config, "claude", "sonnet"))
        self.assertTrue(validate_model(config, "antigravity", "gemini-3.8-flash-high"))
        self.assertTrue(validate_model(config, "opencode", "opencode/gpt-5.1-codex"))

        self.assertFalse(validate_model(config, "claude", "sonnettt"))
        self.assertFalse(validate_model(config, "antigravity", "gpt-fake-model"))
        self.assertFalse(validate_model(config, "opencode", "opencode/gpt-999"))

    def test_get_model_display_name_helper(self):
        """Test get_model_display_name returns human-friendly name or falls back to ID."""
        config = load_config(project_root=os.getcwd())
        self.assertEqual(
            get_model_display_name(config, "antigravity", "gemini-3.8-flash-high"),
            "Gemini 3.8 Flash (High)",
        )
        self.assertEqual(
            get_model_display_name(config, "claude", "sonnet"),
            "Claude Sonnet (CLI alias)",
        )
        # Fallback for unknown
        self.assertEqual(
            get_model_display_name(config, "claude", "unknown-model"),
            "unknown-model",
        )

    def test_invalid_model_selection_claude_error_message(self):
        """Test that configuring an invalid Claude model fails before execution with a helpful error."""
        invalid_data = {
            "models": {
                "claude": [
                    {"id": "sonnet", "name": "Claude Sonnet"},
                    {"id": "opus", "name": "Claude Opus"},
                    {"id": "haiku", "name": "Claude Haiku"},
                ]
            },
            "agents": [
                {"agent": "claude", "model": "sonnettt", "role": "planner"}
            ],
            "roles": {
                "planner": {"responsibility": "Plan tasks"}
            }
        }
        with self.assertRaises(ConfigValidationError) as ctx:
            validate_config(invalid_data)

        error_msg = str(ctx.exception)
        self.assertIn("Agent: claude", error_msg)
        self.assertIn("Requested model: sonnettt", error_msg)
        self.assertIn("Model not found in the configured Claude model catalog", error_msg)
        self.assertIn("- sonnet", error_msg)
        self.assertIn("- opus", error_msg)
        self.assertIn("- haiku", error_msg)
        self.assertIn("Edit orchestrator.yaml and select one of the available model IDs.", error_msg)

    def test_invalid_model_selection_opencode_escape_hatch_message(self):
        """Test that invalid OpenCode model gives instructions on adding custom provider/model."""
        invalid_data = {
            "models": {
                "opencode": [
                    {"id": "opencode/gpt-5.1-codex", "name": "GPT-5.1 Codex via OpenCode"},
                    {"id": "anthropic/claude-sonnet-4-5", "name": "Claude Sonnet 4.5 via Anthropic"},
                ]
            },
            "agents": [
                {"agent": "opencode", "model": "opencode/gpt-999", "role": "implementer"}
            ],
            "roles": {
                "implementer": {"responsibility": "Implement changes"}
            }
        }
        with self.assertRaises(ConfigValidationError) as ctx:
            validate_config(invalid_data)

        error_msg = str(ctx.exception)
        self.assertIn("Agent: opencode", error_msg)
        self.assertIn("Requested model: opencode/gpt-999", error_msg)
        self.assertIn("If this is a custom or newly available OpenCode model", error_msg)
        self.assertIn("add its exact provider/model identifier to orchestrator.yaml.", error_msg)

    def test_custom_opencode_model_addition(self):
        """Verify user can add a custom provider/model in YAML without modifying Python code."""
        custom_yaml = (
            "models:\n"
            "  opencode:\n"
            "    - id: custom-provider/llama-4-super\n"
            "      name: Custom Llama 4\n"
            "agents:\n"
            "  - agent: opencode\n"
            "    model: custom-provider/llama-4-super\n"
            "    role: implementer\n"
            "roles:\n"
            "  implementer:\n"
            "    responsibility: Execute changes\n"
        )
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as tf:
            tf.write(custom_yaml)
            temp_path = tf.name

        try:
            config = load_config(temp_path)
            self.assertTrue(validate_model(config, "opencode", "custom-provider/llama-4-super"))
            agent_cfg = get_agent_config(config, agent="opencode")
            self.assertIsNotNone(agent_cfg)
            self.assertEqual(agent_cfg["model"], "custom-provider/llama-4-super")
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    def test_list_models_cli_flag(self):
        """Test executing python -m orchestrator --list-models exits 0 and prints catalog."""
        python_exe = sys.executable
        res = subprocess.run(
            [python_exe, "-m", "orchestrator", "--list-models"],
            cwd=os.getcwd(),
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(res.returncode, 0)
        self.assertIn("AVAILABLE CONFIGURED MODELS", res.stdout)
        self.assertIn("ANTIGRAVITY", res.stdout)
        self.assertIn("CLAUDE", res.stdout)
        self.assertIn("OPENCODE", res.stdout)
        self.assertIn("gemini-3.8-flash-high", res.stdout)
        self.assertIn("sonnet", res.stdout)
        self.assertIn("opencode/gpt-5.1-codex", res.stdout)


class TestConfigWorkflowPropagation(unittest.TestCase):
    """Test suite verifying config propagation through LangGraph workflow."""

    def setUp(self):
        default_tracer.clear()

    @patch("orchestrator.graph.run_opencode")
    @patch("orchestrator.graph.run_claude_code")
    @patch("orchestrator.graph.run_antigravity")
    def test_custom_config_model_propagation_to_adapters(self, mock_antigravity, mock_claude, mock_opencode):
        """Verify custom model selection propagates to CLI adapters, AgentResult, and Tracer."""
        mock_antigravity.return_value = "Mock custom research output"
        mock_claude.side_effect = [
            "Mock custom planning output",
            "VERDICT: PASS\nSummary: Verified successfully.",
        ]
        mock_opencode.return_value = "Mock custom implementation output"

        custom_yaml = (
            "agents:\n"
            "  - agent: antigravity\n"
            "    model: gemini-3.7-flash-high\n"
            "    role: researcher\n"
            "  - agent: claude\n"
            "    model: opus\n"
            "    role: planner\n"
            "  - agent: opencode\n"
            "    model: opencode/gpt-5.1-codex\n"
            "    role: implementer\n"
            "  - agent: claude\n"
            "    model: haiku\n"
            "    role: verifier\n"
            "roles:\n"
            "  researcher:\n"
            "    responsibility: Custom research instructions\n"
            "  planner:\n"
            "    responsibility: Custom planning instructions\n"
            "  implementer:\n"
            "    responsibility: Custom implementation instructions\n"
            "  verifier:\n"
            "    responsibility: Custom verification instructions\n"
        )
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as tf:
            tf.write(custom_yaml)
            temp_path = tf.name

        try:
            initial_state = {
                "task": "Test custom model propagation",
                "project_root": os.getcwd(),
                "run_store_enabled": False,
                "config_path": temp_path,
            }
            result = graph.invoke(initial_state)

            self.assertEqual(result["status"], "completed")

            # 1. Verify model passed to CLI adapter calls
            mock_antigravity.assert_called_once()
            agy_kwargs = mock_antigravity.call_args.kwargs
            self.assertEqual(agy_kwargs.get("model"), "gemini-3.7-flash-high")

            self.assertEqual(mock_claude.call_count, 2)
            planner_claude_kwargs = mock_claude.call_args_list[0].kwargs
            self.assertEqual(planner_claude_kwargs.get("model"), "opus")
            verifier_claude_kwargs = mock_claude.call_args_list[1].kwargs
            self.assertEqual(verifier_claude_kwargs.get("model"), "haiku")

            mock_opencode.assert_called_once()
            opencode_kwargs = mock_opencode.call_args.kwargs
            self.assertEqual(opencode_kwargs.get("model"), "opencode/gpt-5.1-codex")

            # 2. Verify model recorded in AgentResult objects
            results = result.get("agent_results") or []
            self.assertEqual(len(results), 4)
            self.assertEqual(results[0]["model"], "gemini-3.7-flash-high")
            self.assertEqual(results[1]["model"], "opus")
            self.assertEqual(results[2]["model"], "opencode/gpt-5.1-codex")
            self.assertEqual(results[3]["model"], "haiku")
            self.assertEqual(results[3]["verdict"], "PASS")

            # 3. Verify custom responsibility injected into prompts
            agy_prompt = mock_antigravity.call_args[0][0]
            self.assertIn("Custom research instructions", agy_prompt)

            planner_prompt = mock_claude.call_args_list[0][0][0]
            self.assertIn("Custom planning instructions", planner_prompt)

            opencode_prompt = mock_opencode.call_args[0][0]
            self.assertIn("Custom implementation instructions", opencode_prompt)

            verifier_prompt = mock_claude.call_args_list[1][0][0]
            self.assertIn("Custom verification instructions", verifier_prompt)

            # 4. Verify tracer events recorded model
            events = default_tracer.get_events()
            agy_events = [e for e in events if e.agent == "antigravity"]
            self.assertTrue(all(e.model == "gemini-3.7-flash-high" for e in agy_events if e.status in ("started", "completed")))

            claude_events = [e for e in events if e.agent == "claude" and e.role == "planner"]
            self.assertTrue(all(e.model == "opus" for e in claude_events if e.status in ("started", "completed")))

            opencode_events = [e for e in events if e.agent == "opencode"]
            self.assertTrue(all(e.model == "opencode/gpt-5.1-codex" for e in opencode_events if e.status in ("started", "completed")))

            verifier_events = [e for e in events if e.agent == "claude" and e.role == "verifier"]
            self.assertTrue(all(e.model == "haiku" for e in verifier_events if e.status in ("started", "completed")))

        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)


if __name__ == "__main__":
    unittest.main()
