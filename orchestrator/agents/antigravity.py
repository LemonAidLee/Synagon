"""Adapter for orchestrating Antigravity CLI via non-interactive print mode with JSON output."""

import json
import os
import shutil
import subprocess
from typing import Optional, List, Dict, Any, Tuple

from orchestrator.agents.exceptions import CLIExecutionError, CLITimeoutError, CLIParsingError
from orchestrator.launcher import run_agent_cli
from orchestrator.types import TokenUsage, create_token_usage, unavailable_token_usage


def parse_antigravity_token_usage(payload: Dict[str, Any]) -> TokenUsage:
    """Extract structured TokenUsage from Antigravity JSON payload.

    Args:
        payload: Parsed JSON payload returned by Antigravity CLI.

    Returns:
        Structured TokenUsage dictionary.
    """
    if not isinstance(payload, dict):
        return unavailable_token_usage()

    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return unavailable_token_usage()

    inp = usage.get("input_tokens")
    out = usage.get("output_tokens")
    tot = usage.get("total_tokens")
    cache_read = usage.get("cache_read_tokens")
    thinking = usage.get("thinking_tokens")

    inp_val = inp if isinstance(inp, int) else None
    out_val = out if isinstance(out, int) else None
    tot_val = tot if isinstance(tot, int) else None
    cache_read_val = cache_read if isinstance(cache_read, int) else None
    reasoning_val = thinking if isinstance(thinking, int) else None

    if tot_val is None and inp_val is not None and out_val is not None:
        tot_val = inp_val + out_val

    if inp_val is not None or out_val is not None or tot_val is not None:
        return create_token_usage(
            input_tokens=inp_val,
            output_tokens=out_val,
            total_tokens=tot_val,
            cache_read_tokens=cache_read_val,
            reasoning_tokens=reasoning_val,
            available=True,
            raw_usage=dict(usage),
        )

    return unavailable_token_usage()


def get_antigravity_executable_path() -> str:
    """Resolve path to the agy executable."""
    agy_path = shutil.which("agy")
    if agy_path:
        return agy_path

    # Fallback to known default Windows installation path
    default_win_path = os.path.expandvars(
        r"%LOCALAPPDATA%\agy\bin\agy.exe"
    )
    if os.path.isfile(default_win_path):
        return default_win_path

    raise FileNotFoundError(
        "Could not find 'agy' CLI executable in PATH or standard installation directory."
    )


def run_antigravity_raw(
    prompt: str,
    timeout: int = 180,
    working_dir: Optional[str] = None,
    extra_args: Optional[List[str]] = None,
    model: Optional[str] = None,
    visible: bool = False,
    title: Optional[str] = None,
    role: Optional[str] = "researcher",
    terminal_type: str = "auto",
    pause_on_completion: float = 1.5,
    tracer: Optional[Any] = None,
    agent_execution_mode: str = "auto",
) -> Dict[str, Any]:
    """Execute Antigravity CLI and return the parsed JSON payload."""
    exec_mode_str = (agent_execution_mode or "auto").strip().lower()
    if exec_mode_str == "native_tui":
        raise CLIExecutionError(
            "Antigravity CLI (AGY) does not expose an official programmatic TUI control bridge. "
            "Use agent_execution_mode: auto or headless."
        )

    executable = get_antigravity_executable_path()
    cmd = [executable, "-p", prompt, "--output-format", "json"]

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
        agent="antigravity",
        role=role or "researcher",
        model=model,
        terminal_type=terminal_type,
        pause_on_completion=pause_on_completion,
        tracer=tracer,
    )

    if exec_result.returncode != 0:
        raise CLIExecutionError(
            message=f"Antigravity CLI execution failed with code {exec_result.returncode}",
            returncode=exec_result.returncode,
            stdout=exec_result.stdout,
            stderr=exec_result.stderr,
            command=cmd,
        )

    raw_stdout = exec_result.stdout.strip()
    payload = None
    try:
        payload = json.loads(raw_stdout)
    except json.JSONDecodeError as exc:
        import re
        match = re.search(r'(\{.*\})', raw_stdout, re.DOTALL)
        if match:
            try:
                payload = json.loads(match.group(1))
            except json.JSONDecodeError:
                pass
                
        if payload is None:
            raise CLIParsingError(
                f"Failed to parse Antigravity JSON output: {exc}",
                raw_output=exec_result.stdout,
            ) from exc

    status = payload.get("status")
    if status != "SUCCESS":
        raise CLIExecutionError(
            message=f"Antigravity CLI returned non-success status: {status}",
            returncode=exec_result.returncode,
            stdout=exec_result.stdout,
            stderr=exec_result.stderr,
            command=cmd,
        )

    return payload


def run_antigravity_with_usage(
    prompt: str,
    timeout: int = 180,
    working_dir: Optional[str] = None,
    extra_args: Optional[List[str]] = None,
    model: Optional[str] = None,
    visible: bool = False,
    title: Optional[str] = None,
    role: Optional[str] = "researcher",
    terminal_type: str = "auto",
    pause_on_completion: float = 1.5,
    tracer: Optional[Any] = None,
    agent_execution_mode: str = "auto",
) -> Tuple[str, TokenUsage]:
    """Execute Antigravity CLI and return both response text and structured TokenUsage."""
    payload = run_antigravity_raw(
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

    response = payload.get("response")
    if response is None or not isinstance(response, str):
        raise CLIParsingError(
            "Antigravity output JSON is missing valid 'response' string field",
            raw_output=json.dumps(payload),
        )

    token_usage = parse_antigravity_token_usage(payload)
    return response.strip(), token_usage


def run_antigravity(
    prompt: str,
    timeout: int = 180,
    working_dir: Optional[str] = None,
    extra_args: Optional[List[str]] = None,
    model: Optional[str] = None,
    visible: bool = False,
    title: Optional[str] = None,
    role: Optional[str] = "researcher",
    terminal_type: str = "auto",
    pause_on_completion: float = 1.5,
    tracer: Optional[Any] = None,
    agent_execution_mode: str = "auto",
    return_usage: bool = False,
) -> Any:
    """Execute Antigravity CLI in non-interactive print mode and return response text.

    Args:
        return_usage: When True, return (response_text, token_usage) tuple.
                      When False (default), return response_text string for backward compatibility.
    """
    text, usage = run_antigravity_with_usage(
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

