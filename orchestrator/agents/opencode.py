"""Adapter for orchestrating OpenCode CLI via non-interactive run mode with structured token usage."""

import json
import os
import shutil
import subprocess
from typing import Optional, List, Tuple, Any

from orchestrator.agents.exceptions import CLIExecutionError, CLITimeoutError
from orchestrator.agents.opencode_tui import run_opencode_native_tui
from orchestrator.launcher import check_antigravity_bridge, get_antigravity_bridge_url, run_agent_cli
from orchestrator.types import TokenUsage, create_token_usage, unavailable_token_usage


def parse_opencode_output(stdout: str) -> Tuple[str, TokenUsage]:
    """Parse OpenCode output, extracting response text and structured TokenUsage.

    Handles:
    1. JSON lines format from '--format json':
       - 'text' event parts concatenated to form the response text
       - 'step_finish' or 'step-finish' events containing tokens: { input, output, total, ... }
    2. Non-JSON fallback (e.g. plain text from mocked CLI or older versions):
       - text = stdout.strip()
       - usage = unavailable_token_usage()

    Args:
        stdout: Raw standard output from the OpenCode CLI process.

    Returns:
        Tuple of (response_text, token_usage).
    """
    if not stdout or not stdout.strip():
        return "", unavailable_token_usage()

    raw = stdout.strip()
    text_chunks: List[str] = []
    input_tokens = 0
    output_tokens = 0
    total_tokens = 0
    cache_read_tokens = 0
    reasoning_tokens = 0
    raw_usage_events: List[Dict[str, Any]] = []
    found_tokens = False
    is_json = False

    for line in raw.splitlines():
        line_str = line.strip()
        if not line_str.startswith("{"):
            continue
        try:
            event = json.loads(line_str)
            if not isinstance(event, dict):
                continue
            is_json = True
        except (json.JSONDecodeError, ValueError):
            continue

        part = event.get("part")
        part_dict = part if isinstance(part, dict) else {}

        # 1. Text chunks
        ev_type = event.get("type")
        part_type = part_dict.get("type")
        if ev_type == "text" or part_type == "text":
            txt = part_dict.get("text")
            if isinstance(txt, str):
                text_chunks.append(txt)

        # 2. Token metrics from step finish events
        if ev_type in ("step_finish", "step-finish") or part_type in ("step_finish", "step-finish"):
            tokens_dict = part_dict.get("tokens") or event.get("tokens")
            if isinstance(tokens_dict, dict):
                raw_usage_events.append(dict(tokens_dict))
                inp = tokens_dict.get("input")
                out = tokens_dict.get("output")
                tot = tokens_dict.get("total")
                cache_dict = tokens_dict.get("cache") or {}
                c_read = cache_dict.get("read") if isinstance(cache_dict, dict) else None
                reasoning = tokens_dict.get("reasoning")

                if isinstance(inp, int):
                    input_tokens += inp
                    found_tokens = True
                if isinstance(out, int):
                    output_tokens += out
                    found_tokens = True
                if isinstance(tot, int):
                    total_tokens += tot
                    found_tokens = True
                if isinstance(c_read, int):
                    cache_read_tokens += c_read
                if isinstance(reasoning, int):
                    reasoning_tokens += reasoning

    if is_json and text_chunks:
        response_text = "".join(text_chunks).strip()
    else:
        response_text = raw

    if found_tokens:
        if total_tokens == 0 and (input_tokens > 0 or output_tokens > 0):
            total_tokens = input_tokens + output_tokens + cache_read_tokens
        token_usage = create_token_usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            cache_read_tokens=cache_read_tokens if cache_read_tokens > 0 else None,
            reasoning_tokens=reasoning_tokens if reasoning_tokens > 0 else None,
            available=True,
            raw_usage={"steps": raw_usage_events} if raw_usage_events else None,
        )
    else:
        token_usage = unavailable_token_usage()

    return response_text, token_usage


def get_opencode_executable_path() -> str:
    """Resolve path to the opencode executable dynamically.

    Returns:
        Absolute path to the discovered opencode executable.

    Raises:
        FileNotFoundError: If opencode cannot be found in PATH or standard directories.
    """
    opencode_path = shutil.which("opencode")
    if opencode_path:
        # On Windows, npm creates a .cmd batch wrapper which enforces cmd.exe's 8192-char limit.
        # Prefer the underlying native opencode.exe binary if present in node_modules.
        if opencode_path.lower().endswith((".cmd", ".bat")):
            npm_dir = os.path.dirname(opencode_path)
            direct_exe = os.path.join(npm_dir, "node_modules", "opencode-ai", "bin", "opencode.exe")
            if os.path.isfile(direct_exe):
                return direct_exe
        return opencode_path

    # Fallback to known default Windows installation paths (preferring native .exe)
    candidate_paths = [
        os.path.expandvars(r"%APPDATA%\npm\node_modules\opencode-ai\bin\opencode.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\npm\node_modules\opencode-ai\bin\opencode.exe"),
        os.path.expandvars(r"%APPDATA%\npm\opencode.cmd"),
        os.path.expandvars(r"%LOCALAPPDATA%\npm\opencode.cmd"),
        r"C:\Program Files\nodejs\opencode.cmd",
    ]
    for candidate in candidate_paths:
        if os.path.isfile(candidate):
            return candidate

    raise FileNotFoundError(
        "Could not find 'opencode' CLI executable in PATH or standard installation directory."
    )


def run_opencode_with_usage(
    prompt: str,
    timeout: int = 180,
    working_dir: Optional[str] = None,
    extra_args: Optional[List[str]] = None,
    model: Optional[str] = None,
    visible: bool = False,
    title: Optional[str] = None,
    role: Optional[str] = "implementer",
    terminal_type: str = "auto",
    pause_on_completion: float = 1.5,
    tracer: Optional[object] = None,
    agent_execution_mode: str = "auto",
    return_execution_mode: bool = False,
) -> Any:
    """Execute OpenCode CLI in native TUI or headless mode and return response text and TokenUsage.

    Args:
        prompt: The implementation instructions/task to send to OpenCode.
        timeout: Maximum seconds to wait before raising CLITimeoutError. Default is 180s.
        working_dir: Working directory for the CLI process (defaults to cwd).
        extra_args: Optional list of additional flags to pass to opencode.
        model: Optional model name to pass via the official '--model' flag.
        visible: Whether to execute in a visible terminal window.
        title: Optional title for the terminal window.
        role: Assigned role name (for logging and title).
        terminal_type: Terminal host ('auto', 'antigravity_integrated', 'windows_terminal', 'console').
        pause_on_completion: Seconds to pause upon completion before closing visible window.
        tracer: Optional tracer instance.
        agent_execution_mode: Execution mode: 'auto' (prefer native TUI where supported),
                              'native_tui' (require native TUI), or 'headless'.
        return_execution_mode: When True, return (response_text, token_usage, execution_mode).

    Returns:
        Tuple of (response_text, token_usage) or (response_text, token_usage, execution_mode).

    Raises:
        CLITimeoutError: If execution exceeds the specified timeout.
        CLIExecutionError: If opencode exits with a non-zero code or bridge is offline.
        FileNotFoundError: If the opencode executable cannot be found.
    """
    exec_mode_str = (agent_execution_mode or "auto").strip().lower()

    # A recorder (installed by the daemon, Roadmap Phase 10) means there is somewhere for a
    # live view to go that is not the Antigravity IDE's own bridge - the cockpit's terminal
    # panel. When one is present, native TUI no longer needs that bridge to be worth using:
    # the real interactive `opencode attach` process is captured through a pty instead and
    # relayed the same way any other agent's output would be.
    from orchestrator.launcher import get_output_recorder

    recorder = get_output_recorder()

    # Determine whether to use native TUI execution
    use_native_tui = False
    if exec_mode_str == "native_tui":
        use_native_tui = True
    elif exec_mode_str == "auto":
        if recorder is not None:
            use_native_tui = True
        else:
            is_integrated = terminal_type in ("antigravity_integrated", "integrated", "auto")
            bridge_url = get_antigravity_bridge_url()
            bridge_active = bool(bridge_url and check_antigravity_bridge(bridge_url))
            if visible and is_integrated and bridge_active:
                use_native_tui = True

    if use_native_tui:
        sink = None
        if recorder is not None:
            try:
                sink = recorder(
                    agent="opencode", role=role, model=model,
                    cmd=["opencode", "attach", "<session>"], cwd=working_dir or os.getcwd(),
                )
            except Exception:
                sink = None

        response_text, token_usage = run_opencode_native_tui(
            prompt=prompt,
            project_root=working_dir or os.getcwd(),
            model_name=model,
            timeout_seconds=timeout,
            pause_on_completion=pause_on_completion,
            terminal_title=title,
            tracer=tracer,
            sink=sink,
        )
        stream = getattr(sink, "stream", None)
        if stream is not None:
            stream.close(0)
        if return_execution_mode:
            return response_text, token_usage, "native_tui"
        return response_text, token_usage

    # Fallback to headless execution
    executable = get_opencode_executable_path()
    cmd = [executable, "run"]

    # Request JSON format events unless explicitly overridden in extra_args
    has_format_flag = extra_args and any(
        arg == "--format" or arg.startswith("--format=") for arg in extra_args
    )
    if not has_format_flag:
        cmd.extend(["--format", "json"])

    if model:
        cmd.extend(["--model", model])

    if extra_args:
        cmd.extend(extra_args)

    cmd.append(prompt)

    exec_result = run_agent_cli(
        cmd=cmd,
        cwd=working_dir,
        timeout=timeout,
        visible=visible,
        title=title,
        agent="opencode",
        role=role or "implementer",
        model=model,
        terminal_type=terminal_type,
        pause_on_completion=pause_on_completion,
        tracer=tracer,
    )

    if exec_result.returncode != 0:
        raise CLIExecutionError(
            message=f"OpenCode CLI execution failed with code {exec_result.returncode}",
            returncode=exec_result.returncode,
            stdout=exec_result.stdout,
            stderr=exec_result.stderr,
            command=cmd,
        )

    output = exec_result.stdout.strip() if exec_result.stdout else exec_result.stderr.strip()
    resp_text, t_usage = parse_opencode_output(output)
    if return_execution_mode:
        return resp_text, t_usage, "headless"
    return resp_text, t_usage


def run_opencode(
    prompt: str,
    timeout: int = 180,
    working_dir: Optional[str] = None,
    extra_args: Optional[List[str]] = None,
    model: Optional[str] = None,
    visible: bool = False,
    title: Optional[str] = None,
    role: Optional[str] = "implementer",
    terminal_type: str = "auto",
    pause_on_completion: float = 1.5,
    tracer: Optional[object] = None,
    agent_execution_mode: str = "auto",
    return_usage: bool = False,
    return_execution_mode: bool = False,
) -> Any:
    """Execute OpenCode CLI in native TUI or headless mode and return response text.

    Args:
        return_usage: When True, return (response_text, token_usage) tuple.
        return_execution_mode: When True, return (response_text, token_usage, execution_mode) tuple.
        When both are False (default), return response_text string for backward compatibility.
    """
    res = run_opencode_with_usage(
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
        return_execution_mode=return_execution_mode,
    )
    if return_execution_mode:
        return res
    if return_usage:
        return res[0], res[1]
    return res[0]



def build_repair_prompt(
    task: str,
    project_context: str,
    plan_text: str,
    previous_implementation: str,
    verifier_output: str,
    repair_attempt: int,
    max_repair_attempts: int,
    responsibility: str,
    role_name: str = "implementer",
    skill_manifest: Optional[str] = None,
) -> str:
    """Construct the targeted prompt for OpenCode to perform a self-repair attempt.

    Args:
        task: Original user task.
        project_context: Safe project context.
        plan_text: Original planner recommendations.
        previous_implementation: Text output from the previous implementation attempt.
        verifier_output: Verifier evaluation report containing verdict, findings, and required fixes.
        repair_attempt: Current repair attempt index (1-based).
        max_repair_attempts: Configured maximum repair attempts allowed.
        responsibility: Role responsibility defined in configuration.
        role_name: Role name (defaults to 'implementer').
        skill_manifest: Optional compact manifest of available skills.

    Returns:
        Structured repair prompt string.
    """
    skills_section = ""
    skill_repair_rule = ""
    if skill_manifest and skill_manifest.strip():
        skills_section = (
            "### AVAILABLE SKILLS:\n"
            f"{skill_manifest}\n\n"
        )
        skill_repair_rule = (
            "4. If verification failed due to missing or improper skill usage, inspect the actual "
            "skill instructions at the specified path and apply the required skill workflow.\n"
        )

    return (
        f"You are acting as the {role_name} in an AI development orchestrator performing a SELF-REPAIR ATTEMPT.\n\n"
        f"Your role: {role_name}\n"
        f"Your responsibility: {responsibility}\n\n"
        f"Repair Attempt: {repair_attempt} of {max_repair_attempts}\n\n"
        "### ORIGINAL USER TASK:\n"
        f"{task}\n\n"
        "### PROJECT CONTEXT:\n"
        f"{project_context}\n\n"
        f"{skills_section}"
        "### PLANNER RECOMMENDATIONS:\n"
        f"{plan_text}\n\n"
        "### PREVIOUS IMPLEMENTATION RESULT:\n"
        f"{previous_implementation}\n\n"
        "### VERIFIER FEEDBACK & FINDINGS:\n"
        f"{verifier_output}\n\n"
        "### REPAIR INSTRUCTIONS (INSPECT FIRST):\n"
        "1. Inspect the current workspace and implementation files before modifying anything.\n"
        "2. Review the verifier findings and required fixes above carefully to understand what failed.\n"
        "3. Determine the root cause of the verification failure.\n"
        f"{skill_repair_rule}"
        "5. Make the necessary modifications directly in the workspace to fix the identified problems.\n"
        "6. Avoid making unrelated, out-of-scope, or disruptive changes.\n"
        "7. Run relevant tests or build checks in the workspace to ensure the repair works.\n"
        "8. Leave the workspace in a clean, working state ready for re-verification.\n"
        "9. Summarize all modifications made during this repair attempt and how they resolve the verifier's findings."
    )

