import os
from pathlib import Path
from typing import Optional

PROMPTS_DIR = Path(__file__).parent

def _load_template(name: str) -> str:
    template_path = PROMPTS_DIR / f"{name}.md"
    if not template_path.exists():
        raise FileNotFoundError(f"Prompt template {name}.md not found at {template_path}")
    with open(template_path, "r", encoding="utf-8") as f:
        return f.read()

def build_researcher_prompt(
    role_name: str,
    responsibility: str,
    project_context: str,
    task: str,
    skill_manifest: str,
) -> str:
    skills_section = ""
    if skill_manifest and skill_manifest.strip() and not skill_manifest.startswith("(No skills"):
        skills_section = (
            "### AVAILABLE SKILLS:\n"
            f"{skill_manifest}\n\n"
            "Skill Instructions for Researcher:\n"
            "If any available skill is relevant to the task:\n"
            "- Identify it and consider its capabilities in your research.\n"
            "- Explicitly mention relevant skills in your findings.\n"
            "- Do not force the use of a skill if it is not relevant.\n\n"
        )
    
    template = _load_template("researcher")
    return template.format(
        role_name=role_name,
        responsibility=responsibility,
        project_context=project_context,
        skills_section=skills_section,
        task=task
    )

def build_planner_prompt(
    role_name: str,
    responsibility: str,
    project_context: str,
    task: str,
    research_text: str,
    skill_manifest: str,
) -> str:
    skills_section = ""
    if skill_manifest and skill_manifest.strip() and not skill_manifest.startswith("(No skills"):
        skills_section = (
            "### AVAILABLE SKILLS:\n"
            f"{skill_manifest}\n\n"
            "Skill Instructions for Planner:\n"
            "- Determine which available skills (if any) are relevant to the proposed implementation.\n"
            "- Recommend relevant skills and explain why they should be used.\n"
            "- Do not recommend skills that are not relevant to the task.\n\n"
        )

    template = _load_template("planner")
    return template.format(
        role_name=role_name,
        responsibility=responsibility,
        task=task,
        project_context=project_context,
        skills_section=skills_section,
        research_text=research_text
    )

def build_implementer_prompt(
    role_name: str,
    responsibility: str,
    project_context: str,
    task: str,
    research_text: str,
    plan_text: str,
    skill_manifest: str,
) -> str:
    skills_section = ""
    if skill_manifest and skill_manifest.strip() and not skill_manifest.startswith("(No skills"):
        skills_section = (
            "### AVAILABLE SKILLS:\n"
            f"{skill_manifest}\n\n"
            "Skill Instructions for Implementer:\n"
            "Before implementing functionality that may be supported by one of these skills:\n"
            "1. Identify whether the skill is relevant.\n"
            "2. Inspect its actual instructions at the specified path (e.g. SKILL.md) using your filesystem tools.\n"
            "3. Follow its documented workflow, conventions, and guidelines.\n"
            "4. Use its provided scripts, templates, or resources where appropriate.\n"
            "5. Do not unnecessarily reinvent functionality already provided by the skill.\n"
            "6. Do not modify a skill's own files unless the task explicitly requires it.\n\n"
        )

    template = _load_template("implementer")
    return template.format(
        role_name=role_name,
        responsibility=responsibility,
        task=task,
        project_context=project_context,
        skills_section=skills_section,
        research_text=research_text,
        plan_text=plan_text
    )

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

    template = _load_template("verifier")
    return template.format(
        role_name=role_name,
        responsibility=responsibility,
        iteration_header=iteration_header,
        task=task,
        project_context=project_context,
        skills_section=skills_section,
        research_text=research_text,
        plan_text=plan_text,
        previous_feedback_section=previous_feedback_section,
        implementation_text=implementation_text,
        reverification_note=reverification_note,
        skill_instruction=skill_instruction
    )

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

    template = _load_template("repair")
    return template.format(
        role_name=role_name,
        responsibility=responsibility,
        repair_attempt=repair_attempt,
        max_repair_attempts=max_repair_attempts,
        task=task,
        project_context=project_context,
        skills_section=skills_section,
        plan_text=plan_text,
        previous_implementation=previous_implementation,
        verifier_output=verifier_output,
        skill_repair_rule=skill_repair_rule
    )
