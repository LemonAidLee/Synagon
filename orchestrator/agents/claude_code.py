"""Adapter for orchestrating Claude Code CLI via non-interactive print mode with structured token usage."""

import json
import os
import shutil
import subprocess
from typing import Optional, List, Tuple, Any

from orchestrator.agents.exceptions import CLIExecutionError, CLITimeoutError
from orchestrator.launcher import run_agent_cli
from orchestrator.types import TokenUsage, create_token_usage, unavailable_token_usage


def parse_claude_output(stdout: str) -> Tuple[str, TokenUsage]:
    """Parse Claude Code output, extracting text and structured TokenUsage.

    Handles:
    1. JSON output from '--output-format json':
       - 'result': response text string
       - 'usage': { 'input_tokens': int, 'output_tokens': int, ... }
    2. Non-JSON fallback (e.g. plain text from mocked CLI, stderr warnings, or older versions):
       - text = stdout.strip()
       - usage = unavailable_token_usage()

    Args:
        stdout: Raw standard output from the Claude CLI process.

    Returns:
        Tuple of (response_text, token_usage).
    """
    if not stdout or not stdout.strip():
        return "", unavailable_token_usage()

    raw = stdout.strip()
    data = None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        # Plain text output (e.g. mocked CLI in tests or older CLI without JSON flag)
        # Attempt to extract JSON from the last line or block if stdout has CLI warnings (e.g. "Warning: no stdin data...")
        import re
        match = re.search(r'(\{.*\})', raw, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(1))
            except (json.JSONDecodeError, ValueError):
                pass
                
        if data is None:
            return raw, unavailable_token_usage()

    if not isinstance(data, dict):
        return raw, unavailable_token_usage()

    # Extract response text
    text = data.get("result")
    if text is None or not isinstance(text, str):
        text = raw

    # Extract usage metadata
    usage_dict = data.get("usage")
    if isinstance(usage_dict, dict):
        inp = usage_dict.get("input_tokens")
        out = usage_dict.get("output_tokens")
        tot = usage_dict.get("total_tokens")
        cache_read = usage_dict.get("cache_read_input_tokens")
        cache_write = usage_dict.get("cache_creation_input_tokens")
        thinking_details = usage_dict.get("output_tokens_details") or {}
        thinking = thinking_details.get("thinking_tokens") if isinstance(thinking_details, dict) else None

        inp_val = inp if isinstance(inp, int) else None
        out_val = out if isinstance(out, int) else None
        tot_val = tot if isinstance(tot, int) else None
        cache_read_val = cache_read if isinstance(cache_read, int) else None
        cache_write_val = cache_write if isinstance(cache_write, int) else None
        reasoning_val = thinking if isinstance(thinking, int) else None

        # In Claude Code / Anthropic, input_tokens does NOT include cached tokens.
        # We must sum them to get the true processed context size.
        true_inp = (inp_val or 0) + (cache_read_val or 0) + (cache_write_val or 0)

        if tot_val is None and (inp_val is not None or cache_read_val is not None or cache_write_val is not None) and out_val is not None:
            tot_val = true_inp + out_val

        if inp_val is not None or out_val is not None or tot_val is not None:
            return text.strip(), create_token_usage(
                input_tokens=true_inp if (inp_val is not None) else None,
                output_tokens=out_val,
                total_tokens=tot_val,
                cache_read_tokens=cache_read_val,
                cache_write_tokens=cache_write_val,
                reasoning_tokens=reasoning_val,
                available=True,
                raw_usage=dict(usage_dict),
            )

    return text.strip(), unavailable_token_usage()


def get_claude_executable_path() -> str:
    """Resolve path to the claude executable."""
    claude_path = shutil.which("claude")
    if claude_path:
        return claude_path

    # Fallback to known default Windows installation path
    default_win_path = os.path.expandvars(
        r"%APPDATA%\SPB_Data\.local\bin\claude.exe"
    )
    if os.path.isfile(default_win_path):
        return default_win_path

    raise FileNotFoundError(
        "Could not find 'claude' CLI executable in PATH or standard installation directory."
    )


def run_claude_code_with_usage(
    prompt: str,
    timeout: int = 180,
    working_dir: Optional[str] = None,
    extra_args: Optional[List[str]] = None,
    model: Optional[str] = None,
    visible: bool = False,
    title: Optional[str] = None,
    role: Optional[str] = "planner",
    terminal_type: str = "auto",
    pause_on_completion: float = 1.5,
    tracer: Optional[object] = None,
    agent_execution_mode: str = "auto",
) -> Tuple[str, TokenUsage]:
    """Execute Claude Code CLI and return both response text and structured TokenUsage."""
    exec_mode_str = (agent_execution_mode or "auto").strip().lower()
    if exec_mode_str == "native_tui":
        raise CLIExecutionError(
            "Claude Code does not expose an official programmatic TUI control bridge. "
            "Use agent_execution_mode: auto or headless."
        )

    executable = get_claude_executable_path()
    cmd = [executable, "-p", prompt]

    # Request JSON output format if not already specified in extra_args
    has_format_flag = extra_args and any(
        arg == "--output-format" or arg.startswith("--output-format=") for arg in extra_args
    )
    if not has_format_flag:
        cmd.extend(["--output-format", "json"])

    if model:
        cmd.extend(["--model", model])

    if extra_args:
        cmd.extend(extra_args)

    exec_result = run_agent_cli(
        cmd=cmd,
        cwd=working_dir,
        timeout=timeout,
        visible=visible,
        title=title,
        agent="claude",
        role=role or "planner",
        model=model,
        terminal_type=terminal_type,
        pause_on_completion=pause_on_completion,
        tracer=tracer,
    )

    if exec_result.returncode != 0:
        raise CLIExecutionError(
            message=f"Claude Code CLI execution failed with code {exec_result.returncode}",
            returncode=exec_result.returncode,
            stdout=exec_result.stdout,
            stderr=exec_result.stderr,
            command=cmd,
        )

    output_text, token_usage = parse_claude_output(exec_result.stdout)
    return output_text, token_usage


def run_claude_code(
    prompt: str,
    timeout: int = 180,
    working_dir: Optional[str] = None,
    extra_args: Optional[List[str]] = None,
    model: Optional[str] = None,
    visible: bool = False,
    title: Optional[str] = None,
    role: Optional[str] = "planner",
    terminal_type: str = "auto",
    pause_on_completion: float = 1.5,
    tracer: Optional[object] = None,
    agent_execution_mode: str = "auto",
    return_usage: bool = False,
) -> Any:
    """Execute Claude Code CLI in non-interactive print mode and return response text."""
    text, usage = run_claude_code_with_usage(
        prompt=prompt,
        timeout=timeout,
        working_dir=working_dir,
        extra_args=extra_args,
        model=model,
        visible=visible,
        title=title,
        role=role,
        terminal_type=terminal_type,
        pause_on_completion=pause_on_completion,
        tracer=tracer,
        agent_execution_mode=agent_execution_mode,
    )
    if return_usage:
        return text, usage
    return text
    return text

