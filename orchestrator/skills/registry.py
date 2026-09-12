"""SkillRegistry for managing discovered skills, generating compact manifests, and tracking references."""

import os
import re
from typing import Dict, List, Optional

from orchestrator.skills.types import SkillInfo


def _within(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([os.path.normcase(path), os.path.normcase(root)]) == os.path.normcase(root)
    except ValueError:  # different drives
        return False


def rebase_skill_paths(
    skills: Optional[List[SkillInfo]],
    project_root: str,
    working_directory: str,
) -> List[SkillInfo]:
    """Point the paths of project skills at the run's worktree instead of the user's checkout.

    Skills are discovered from the project root, so their paths name the checkout - and those
    paths go into every agent's prompt with an instruction to read them. In an isolated run
    that sent agents out of their worktree, the way "Project Root" did before Stage 7.5.2.1
    (OpenCode stops on its own external-directory permission prompt, and the checkout is what
    invariant 3 protects). A skill committed to the project exists at the same relative path in
    the worktree, so that path is used. One that does not (an untracked skill) keeps its path
    and is marked ``outside_workspace`` so the manifest can say so. Skills from a search path
    outside the project are untouched. Returns new records; the input is not modified.
    """
    rebased: List[SkillInfo] = []
    root = os.path.abspath(project_root or "")
    work = os.path.abspath(working_directory or "")
    for skill in skills or []:
        entry: SkillInfo = dict(skill)  # type: ignore[assignment]
        location = str(entry.get("location") or "")
        if root and work and os.path.normcase(root) != os.path.normcase(work) and location and _within(location, root) and not _within(location, work):
            candidate = os.path.join(work, os.path.relpath(location, root))
            if os.path.isdir(candidate):
                entry["location"] = candidate
                instructions = str(entry.get("instructions_path") or "")
                if instructions and _within(instructions, root):
                    entry["instructions_path"] = os.path.join(work, os.path.relpath(instructions, root))
            else:
                entry["outside_workspace"] = True  # type: ignore[typeddict-unknown-key]
        rebased.append(entry)
    return rebased


def format_skill_manifest(skills: List[SkillInfo]) -> str:
    """Format a compact, token-efficient manifest of available skills for injection into prompts.

    Args:
        skills: List of SkillInfo dictionaries.

    Returns:
        Compact text manifest string.
    """
    if not skills:
        return "(No skills available in the current project or environment)"

    lines: List[str] = ["AVAILABLE SKILLS:"]
    for idx, skill in enumerate(skills, 1):
        name = skill.get("name", "unknown")
        desc = skill.get("description", "").strip() or "No description provided."
        location = skill.get("location", "")
        instructions = skill.get("instructions_path", "")
        resources = skill.get("resources", [])

        entry = [
            f"{idx}. {name}",
            f"   Description: {desc}",
        ]
        if location:
            entry.append(f"   Location: {location}")
        if instructions:
            entry.append(f"   Instructions: {instructions}")
        if resources:
            entry.append(f"   Resources: {', '.join(resources)}")
        if skill.get("outside_workspace"):
            entry.append(
                "   Note: this skill is not committed to the project, so it is not in your "
                "worktree. Read it only; never write to its location."
            )

        lines.append("\n".join(entry))

    return "\n\n".join(lines)


class SkillRegistry:
    """Registry maintaining normalized skill records for an orchestration run."""

    def __init__(self, skills: Optional[List[SkillInfo]] = None) -> None:
        self._skills: Dict[str, SkillInfo] = {}
        if skills:
            for skill in skills:
                self.register(skill)

    def register(self, skill: SkillInfo) -> None:
        """Register or update a skill in the registry."""
        name = skill.get("name", "").strip().lower()
        if name:
            self._skills[name] = skill

    def get(self, name: str) -> Optional[SkillInfo]:
        """Retrieve a skill by normalized name."""
        return self._skills.get(name.strip().lower())

    def list_skills(self) -> List[SkillInfo]:
        """Return all registered skills ordered by name."""
        return sorted(self._skills.values(), key=lambda s: s.get("name", ""))

    def format_manifest(self) -> str:
        """Generate the compact text manifest for this registry."""
        return format_skill_manifest(self.list_skills())

    def detect_referenced_skills(self, text: str) -> List[str]:
        """Identify which registered skills are explicitly referenced in a text string.

        Args:
            text: Arbitrary agent output or prompt text.

        Returns:
            List of referenced skill names.
        """
        if not text or not self._skills:
            return []

        referenced: List[str] = []
        for name in sorted(self._skills.keys()):
            # Use word-boundary regex match for accurate detection
            pattern = rf"\b{re.escape(name)}\b"
            if re.search(pattern, text, re.IGNORECASE):
                referenced.append(name)

        return referenced
