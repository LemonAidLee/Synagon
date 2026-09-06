"""Passive skill discovery module for scanning configured locations and normalizing skill metadata."""

import os
from pathlib import Path
import re
from typing import List, Optional, Set
import yaml

from orchestrator.skills.types import SkillInfo


KNOWN_RESOURCE_DIRS = {
    "scripts",
    "templates",
    "assets",
    "examples",
    "resources",
    "tools",
    "docs",
    "references",
}


def _extract_frontmatter(content: str) -> tuple[Optional[dict], str]:
    """Extract YAML frontmatter from markdown content if present.

    Returns:
        Tuple of (parsed_frontmatter_dict or None, remaining_content).
    """
    stripped = content.strip()
    if not stripped.startswith("---"):
        return None, content

    parts = stripped.split("---", 2)
    if len(parts) >= 3:
        frontmatter_text = parts[1]
        body = parts[2]
        try:
            parsed = yaml.safe_load(frontmatter_text)
            if isinstance(parsed, dict):
                return parsed, body
        except Exception:
            return None, body

    return None, content


def _extract_fallback_metadata(body: str, default_name: str) -> tuple[str, str]:
    """Extract skill name and description from markdown body when frontmatter is absent.

    Returns:
        Tuple of (name, description).
    """
    name = default_name
    description = ""

    lines = [line.strip() for line in body.splitlines()]
    non_empty = [
        line for line in lines
        if line and not line.startswith("---") and not line.startswith("===") and not line.startswith("***")
    ]

    for line in non_empty:
        if line.startswith("#") and name == default_name:
            extracted = line.lstrip("#").strip()
            if extracted:
                name = extracted.lower().replace(" ", "-")
        elif not line.startswith("#") and not description:
            description = line
        if name != default_name and description:
            break

    if not description:
        description = f"Skill capability provided by {name}."

    return name, description


def parse_skill_metadata(skill_dir: Path) -> Optional[SkillInfo]:
    """Passively parse a skill directory into a standardized SkillInfo record.

    Strict rules:
    - Never executes any code, binaries, or scripts.
    - Requires an instructions document (SKILL.md or skill.md).
    - Gracefully handles malformed YAML or unreadable files.

    Args:
        skill_dir: Path object pointing to the skill directory candidate.

    Returns:
        SkillInfo dictionary if valid, None if directory is not a valid skill.
    """
    if not skill_dir.is_dir():
        return None

    # Locate instructions file (SKILL.md preferred, fallback to skill.md)
    instructions_file: Optional[Path] = None
    for candidate in ["SKILL.md", "skill.md", "Skill.md"]:
        target = skill_dir / candidate
        if target.is_file():
            instructions_file = target
            break

    if instructions_file is None:
        return None

    try:
        content = instructions_file.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None

    default_name = skill_dir.name.lower().replace(" ", "-")
    frontmatter, body = _extract_frontmatter(content)

    version: Optional[str] = None
    if frontmatter and isinstance(frontmatter, dict):
        raw_name = frontmatter.get("name")
        name = str(raw_name).strip().lower().replace(" ", "-") if raw_name else default_name
        description = str(frontmatter.get("description") or "").strip()
        raw_version = frontmatter.get("version")
        version = str(raw_version).strip() if raw_version is not None else None

        if not description:
            _, fallback_desc = _extract_fallback_metadata(body, default_name)
            description = fallback_desc
    else:
        name, description = _extract_fallback_metadata(body, default_name)

    # Inspect immediate child directories for resources without deep recursion
    resources: List[str] = []
    try:
        for child in sorted(skill_dir.iterdir()):
            if child.is_dir() and not child.name.startswith(".") and child.name != "__pycache__":
                resources.append(child.name)
    except Exception:
        resources = []

    return {
        "name": name,
        "description": description,
        "location": str(skill_dir.resolve()),
        "instructions_path": str(instructions_file.resolve()),
        "version": version,
        "resources": resources,
    }


def is_drive_root(path: Path) -> bool:
    r"""Check if a path represents an entire drive root (e.g. C:\ or D:\)."""
    resolved = path.resolve()
    # Check if parent is same as path (e.g. C:\) or root path
    return resolved.parent == resolved or str(resolved).rstrip("\\/") in ("/", "") or bool(re.match(r"^[a-zA-Z]:\\?$", str(resolved)))


def discover_skills(
    project_root: str,
    search_paths: Optional[List[str]] = None,
    enabled: bool = True,
) -> List[SkillInfo]:
    r"""Discover all available skills from configured search paths relative to project root.

    Security & Safety Rules:
    - Never scans arbitrary drives (e.g. C:\ or D:\ directly).
    - Strictly passive: no code execution during discovery.
    - Deduplicates by normalized skill name (earlier search path takes precedence).
    - Sorts output deterministically by skill name.

    Args:
        project_root: Authoritative root of the project being orchestrated.
        search_paths: List of relative or absolute directory paths to search for skills.
        enabled: Toggle to disable skill discovery entirely.

    Returns:
        List of unique SkillInfo dictionaries ordered alphabetically by name.
    """
    if not enabled:
        return []

    root_path = Path(project_root).resolve()
    if not root_path.is_dir():
        return []

    if search_paths is None:
        search_paths = ["./skills", "./.agents/skills"]

    discovered: List[SkillInfo] = []
    seen_names: Set[str] = set()

    for rel_or_abs in search_paths:
        if not rel_or_abs or not isinstance(rel_or_abs, str):
            continue

        candidate_path = Path(rel_or_abs.strip())
        if candidate_path.is_absolute():
            resolved_dir = candidate_path.resolve()
        else:
            resolved_dir = (root_path / candidate_path).resolve()

        # Security barrier: forbid drive roots and non-existent folders
        if is_drive_root(resolved_dir) or not resolved_dir.is_dir():
            continue

        # Iterate over immediate child directories
        try:
            children = sorted(resolved_dir.iterdir())
        except Exception:
            continue

        for child in children:
            if not child.is_dir() or child.name.startswith("."):
                continue

            skill = parse_skill_metadata(child)
            if skill and skill["name"] not in seen_names:
                seen_names.add(skill["name"])
                discovered.append(skill)

    # Return deterministically sorted by name
    return sorted(discovered, key=lambda s: s["name"])
