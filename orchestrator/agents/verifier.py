"""Verifier agent module for evaluating implementations against user tasks and requirements.

Reuses the existing Claude Code CLI adapter without introducing new authentication systems.
"""

import re
from typing import Optional, List

from orchestrator.agents.claude_code import run_claude_code


#: Verdicts the verifier may return.
#:
#: PASS    - the implementation satisfies the task.
#: FAIL    - the implementation is wrong, and another agent could plausibly fix it.
#: BLOCKED - progress requires a human; no repair attempt could resolve it.
#: UNKNOWN - the verifier produced no parseable verdict (fail-safe, never a pass).
VERDICT_PASS = "PASS"
VERDICT_FAIL = "FAIL"
VERDICT_BLOCKED = "BLOCKED"
VERDICT_UNKNOWN = "UNKNOWN"

VALID_VERDICTS = (VERDICT_PASS, VERDICT_FAIL, VERDICT_BLOCKED)

#: Verdicts that consume the repair budget. BLOCKED deliberately does not:
#: spending repair attempts on something no agent can fix is pure waste.
REPAIRABLE_VERDICTS = (VERDICT_FAIL, VERDICT_UNKNOWN)


def parse_verdict(text: str) -> str:
    """Extract the verification verdict from verifier output.

    Args:
        text: Raw output string returned by the verifier agent.

    Returns:
        "PASS", "FAIL", or "BLOCKED" when an unambiguous verdict line is found,
        otherwise "UNKNOWN". A missing or malformed verdict never yields a pass.
    """
    if not text or not isinstance(text, str):
        return VERDICT_UNKNOWN

    # Search for "VERDICT: PASS|FAIL|BLOCKED" (case-insensitive).
    # Handles variations like "**VERDICT:** PASS", "VERDICT:  FAIL", "### VERDICT: BLOCKED"
    match = re.search(
        r"(?:^|\n|\b)\*?\*?VERDICT:\*?\*?\s*(PASS|FAIL|BLOCKED)\b",
        text,
        re.IGNORECASE,
    )
    if match:
        return match.group(1).upper()

    return VERDICT_UNKNOWN


def is_repairable(verdict: Optional[str]) -> bool:
    """Return True when `verdict` is one an automated repair attempt should address."""
    return verdict in REPAIRABLE_VERDICTS


def extract_human_action(text: str) -> Optional[str]:
    """Extract the 'Human Action Required' section from verifier output.

    Populated by the verifier when it returns BLOCKED, this is what the user
    must decide, provide, or authorize before the run can continue.

    Args:
        text: Raw verifier output.

    Returns:
        The section body, or None when absent or explicitly 'None'.
    """
    if not text or not isinstance(text, str):
        return None

    match = re.search(
        r"\*?\*?Human Action Required:?\*?\*?\s*\n?(.*?)(?=\n\s*\*?\*?[A-Z][A-Za-z ]{2,30}:\*?\*?\s*\n|\Z)",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return None

    body = match.group(1).strip()
    # A bracketed body is the prompt's own placeholder echoed back, not a real
    # instruction to the user. Treat it as absent.
    if body.startswith("[") and body.endswith("]"):
        return None
    if not body or body.lower() in {"none", "n/a", "none.", "not applicable"}:
        return None
    return body


def build_verifier_prompt(
    task: str,
    project_context: str,
    research_text: str,
    plan_text: str,
    implementation_text: str,
    responsibility: str,
    role_name: str = "verifier",
    verification_attempt: int = 1,
    repair_attempts_completed: int = 0,
    previous_verifier_output: Optional[str] = None,
    skill_manifest: Optional[str] = None,
) -> str:
    """Construct the comprehensive cumulative prompt for the verifier agent.

    Args:
        task: Original user task.
        project_context: Collected safe project context.
        research_text: Researcher (Antigravity) findings.
        plan_text: Planner (Claude Code) recommendations.
        implementation_text: Implementer (OpenCode) execution result.
        responsibility: Role responsibility defined in configuration.
        role_name: Role name (defaults to 'verifier').
        verification_attempt: Verification attempt index (1-based).
        repair_attempts_completed: Number of repair attempts completed prior to this verification.
        previous_verifier_output: Optional output from previous verification attempt to check against.
        skill_manifest: Optional compact manifest of available skills.

    Returns:
        Full prompt string for the verifier agent.
    """
    iteration_header = ""
    if verification_attempt > 1 or repair_attempts_completed > 0:
        iteration_header = (
            "### VERIFICATION ITERATION CONTEXT:\n"
            f"- Verification Evaluation Attempt: #{verification_attempt}\n"
            f"- Repair Attempts Completed: {repair_attempts_completed}\n\n"
        )

    previous_feedback_section = ""
    if previous_verifier_output:
        previous_feedback_section = (
            "### PREVIOUS VERIFIER FINDINGS (REPAIR TARGET):\n"
            f"{previous_verifier_output}\n\n"
        )

    skills_section = ""
    skill_instruction = ""
    if skill_manifest and skill_manifest.strip():
        skills_section = (
            "### AVAILABLE SKILLS:\n"
            f"{skill_manifest}\n\n"
        )
        skill_instruction = (
            "   - Skill Compliance: Consider whether relevant available skills were used appropriately. "
            "Inspect skill instructions at the provided paths when necessary, and verify that the "
            "implementation follows relevant skill requirements if a skill was recommended or required.\n"
        )

    reverification_note = ""
    if verification_attempt > 1:
        reverification_note = (
            "NOTE: This is a RE-VERIFICATION after a self-repair attempt. "
            "Examine the latest files in the workspace to confirm whether previously identified "
            "issues have been corrected without introducing new regressions.\n"
        )

    return (
        f"You are acting as the {role_name} in an AI development orchestrator.\n\n"
        f"Your role: {role_name}\n"
        f"Your responsibility: {responsibility}\n\n"
        f"{iteration_header}"
        "### ORIGINAL USER TASK:\n"
        f"{task}\n\n"
        "### PROJECT CONTEXT:\n"
        f"{project_context}\n\n"
        f"{skills_section}"
        "### RESEARCHER FINDINGS:\n"
        f"{research_text}\n\n"
        "### PLANNER RECOMMENDATIONS:\n"
        f"{plan_text}\n\n"
        f"{previous_feedback_section}"
        "### IMPLEMENTER RESULT:\n"
        f"{implementation_text}\n\n"
        "### VERIFICATION INSTRUCTIONS:\n"
        f"{reverification_note}"
        "1. Independently evaluate whether the implementation satisfies the original user task.\n"
        "   Do NOT assume OpenCode's claims are correct without verifying the actual files.\n"
        "2. Inspect the project workspace and examine actual code modifications and tests.\n"
        "3. Evaluate:\n"
        "   - Task Completion: Did the implementation address everything requested?\n"
        "   - Correctness: Does the code behave properly and meet specifications?\n"
        f"{skill_instruction}"
        "   - Tests: Run or inspect relevant tests. Do they pass or fail?\n"
        "   - Scope: Did the implementation touch files it was explicitly forbidden to modify?\n"
        "   - Regressions: Were existing functions or modules broken?\n"
        "4. Your response MUST include an explicit, unambiguous verdict line formatted exactly as:\n"
        "   VERDICT: PASS\n"
        "   or\n"
        "   VERDICT: FAIL\n"
        "   or\n"
        "   VERDICT: BLOCKED\n\n"
        "### CHOOSING THE VERDICT:\n"
        "- PASS: The implementation satisfies the original task. Tests relevant to the change pass.\n"
        "- FAIL: The implementation is incorrect or incomplete, AND a coding agent could\n"
        "  plausibly fix it by editing files in this workspace. Use FAIL whenever an automated\n"
        "  repair attempt has a realistic chance of success.\n"
        "- BLOCKED: Progress is impossible without a human decision or human action. Use BLOCKED\n"
        "  ONLY when repeating the implementation attempt cannot help, for example:\n"
        "    * Missing or invalid credentials, API keys, tokens, or account permissions.\n"
        "    * A required external service, network resource, or licence is unavailable.\n"
        "    * The task is genuinely ambiguous and needs the user to choose between valid readings.\n"
        "    * The task requires a product, legal, security, or architectural decision the user owns.\n"
        "    * A required tool, runtime, or dependency is absent and cannot be installed from here.\n"
        "  Do NOT use BLOCKED merely because the task is difficult, large, or partially complete.\n"
        "  If a coding agent could make progress by editing files, the verdict is FAIL, not BLOCKED.\n\n"
        "Provide your evaluation in the following structure:\n"
        "VERDICT: [PASS, FAIL, or BLOCKED]\n\n"
        "Summary:\n"
        "[Brief summary of verification results]\n\n"
        "Tests:\n"
        "[Test results and observations]\n\n"
        "Findings:\n"
        "[Specific findings regarding task completion, correctness, and scope]\n\n"
        "Required Fixes:\n"
        "[List required fixes if FAIL, or 'None' if PASS]\n\n"
        "Human Action Required:\n"
        "[If BLOCKED, state exactly what the user must decide, provide, or authorize. "
        "Otherwise write 'None'.]"
    )


def run_verifier(
    prompt: str,
    timeout: int = 180,
    working_dir: Optional[str] = None,
    extra_args: Optional[List[str]] = None,
    model: Optional[str] = None,
) -> str:
    """Execute the Claude verifier agent using the existing Claude Code adapter.

    Args:
        prompt: The verification prompt.
        timeout: Maximum seconds to wait.
        working_dir: Project root directory for file and test inspection.
        extra_args: Optional extra CLI flags.
        model: Optional model name to pass to Claude Code CLI.

    Returns:
        The response text produced by Claude Code.
    """
    return run_claude_code(
        prompt=prompt,
        timeout=timeout,
        working_dir=working_dir,
        extra_args=extra_args,
        model=model,
    )
