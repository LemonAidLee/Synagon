"""Type definitions for the Skill Registry and discovery layer."""

from typing import List, Optional, TypedDict


class SkillInfo(TypedDict, total=False):
    """Normalized metadata for a discovered skill."""
    name: str
    description: str
    location: str
    instructions_path: str
    version: Optional[str]
    resources: List[str]


class SkillConfig(TypedDict, total=False):
    """Configuration definition for skill discovery in orchestrator.yaml."""
    enabled: bool
    search_paths: List[str]
