"""SkillRegistry for managing discovered skills, generating compact manifests, and tracking references."""

import re
from typing import Dict, List, Optional

from orchestrator.skills.types import SkillInfo


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
