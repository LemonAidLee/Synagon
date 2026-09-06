"""Project context collection module for orchestrating agents with safe workspace awareness."""

import json
import os
from pathlib import Path
from typing import Optional, Set, List, Dict, Any


EXCLUDED_DIRS: Set[str] = {
    ".git",
    ".venv",
    "venv",
    "ENV",
    "env",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "build",
    "dist",
    ".langgraph_api",
    ".langgraph",
    ".idea",
    ".vscode",
    "node_modules",
    "target",
}

# Patterns of files whose contents must NEVER be read
SENSITIVE_FILE_NAMES: Set[str] = {
    ".env",
    ".env.local",
    ".env.production",
    ".env.development",
    ".env.test",
    "id_rsa",
    "id_ed25519",
}


def get_project_root(start_dir: Optional[str] = None) -> str:
    """Determine and validate the project root directory.

    Args:
        start_dir: Optional directory path. Defaults to the current working directory.

    Returns:
        Absolute normalized path to the project root directory.

    Raises:
        FileNotFoundError: If the specified directory does not exist.
        NotADirectoryError: If the path exists but is not a directory.
    """
    path_str = start_dir if start_dir else os.getcwd()
    resolved = Path(path_str).resolve()

    if not resolved.exists():
        raise FileNotFoundError(f"Project root directory does not exist: {resolved}")
    if not resolved.is_dir():
        raise NotADirectoryError(f"Project root path is not a directory: {resolved}")

    return str(resolved)


def _safe_summarize_requirements(req_path: Path) -> List[str]:
    """Safely extract package names from requirements.txt without executing anything."""
    if not req_path.is_file():
        return []

    packages: List[str] = []
    try:
        with open(req_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                # Strip inline comments
                line = line.split("#")[0].strip()
                # Extract package name (strip version specifiers)
                pkg_name = line.split("==")[0].split(">=")[0].split("<=")[0].split("~=")[0].strip()
                if pkg_name:
                    packages.append(pkg_name)
    except Exception:
        return ["(Unable to read requirements.txt)"]

    return packages

def _safe_summarize_pyproject(toml_path: Path) -> List[str]:
    """Safely extract basic info from pyproject.toml without external libraries."""
    if not toml_path.is_file():
        return []
    deps = []
    try:
        in_deps = False
        with open(toml_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line.startswith("[project]") or line.startswith("[tool.poetry.dependencies]"):
                    pass # Just marker
                if line.startswith("dependencies = ["):
                    in_deps = True
                    continue
                if in_deps:
                    if line.startswith("]"):
                        in_deps = False
                        continue
                    # Clean up strings like "flask>=2.0",
                    cleaned = line.replace('"', '').replace("'", "").replace(",", "").strip()
                    pkg = cleaned.split(">=")[0].split("==")[0].split("<=")[0].split("~=")[0]
                    if pkg and not pkg.startswith("#"):
                        deps.append(pkg)
    except Exception:
        return ["(Unable to read pyproject.toml)"]
    return deps

def _safe_summarize_package_json(json_path: Path) -> List[str]:
    """Safely extract dependencies from package.json."""
    if not json_path.is_file():
        return []
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            deps = list(data.get("dependencies", {}).keys())
            dev_deps = list(data.get("devDependencies", {}).keys())
            return deps + [d + " (dev)" for d in dev_deps]
    except Exception:
        return ["(Unable to read package.json)"]

def _safe_summarize_cargo_toml(toml_path: Path) -> List[str]:
    """Safely extract dependencies from Cargo.toml."""
    if not toml_path.is_file():
        return []
    deps = []
    try:
        in_deps = False
        with open(toml_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line.startswith("[dependencies]") or line.startswith("[dev-dependencies]"):
                    in_deps = True
                    continue
                elif line.startswith("["):
                    in_deps = False
                
                if in_deps and "=" in line and not line.startswith("#"):
                    pkg = line.split("=")[0].strip()
                    deps.append(pkg)
    except Exception:
        return ["(Unable to read Cargo.toml)"]
    return deps

def _safe_summarize_go_mod(mod_path: Path) -> List[str]:
    """Safely extract module name and dependencies from go.mod."""
    if not mod_path.is_file():
        return []
    deps = []
    try:
        with open(mod_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line.startswith("module "):
                    deps.append(f"Module: {line.split(' ')[1]}")
                elif line and not line.startswith("require") and not line.startswith(")") and not line.startswith("//"):
                    # heuristic for require block lines
                    parts = line.split(" ")
                    if len(parts) >= 2 and "." in parts[0]:
                        deps.append(parts[0])
    except Exception:
        return ["(Unable to read go.mod)"]
    return deps

def _safe_summarize_langgraph_config(config_path: Path) -> Dict[str, Any]:
    """Safely read high-level graph definitions from langgraph.json."""
    if not config_path.is_file():
        return {}

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return {
                "graphs": data.get("graphs", {}),
                "dependencies": data.get("dependencies", []),
            }
    except Exception:
        return {}


def collect_project_context(project_root: Optional[str] = None) -> str:
    """Inspect the project root and produce a concise, safe markdown context block.

    Security guarantees:
    - Never reads .env file contents.
    - Excludes virtual environments, git internals, and cache directories.
    - Never leaks secret tokens or credentials.

    Args:
        project_root: The project directory path (defaults to current working directory).

    Returns:
        A concise, deterministic markdown string summarizing project structure.
    """
    root_str = get_project_root(project_root)
    root_path = Path(root_str)

    # Inspect top-level items
    top_level_files: List[str] = []
    top_level_dirs: List[str] = []

    try:
        entries = sorted(os.listdir(root_path))
    except Exception as exc:
        return f"### Project Context\n- Root: `{root_str}`\n- Error inspecting directory: {exc}\n"

    for entry in entries:
        full_entry = root_path / entry
        if full_entry.is_dir():
            if entry not in EXCLUDED_DIRS and not entry.endswith(".egg-info"):
                top_level_dirs.append(entry)
        else:
            # Check for sensitive files
            if entry in SENSITIVE_FILE_NAMES or (entry.startswith(".env") and entry != ".env.example"):
                top_level_files.append(f"{entry} (present - contents redacted for security)")
            else:
                top_level_files.append(entry)

    lines: List[str] = [
        "### Project Context (Collected by Orchestrator)",
        f"- **Project Root**: `{root_str}`",
        f"- **Top-Level Directories**: {', '.join(top_level_dirs) if top_level_dirs else 'None'}",
        f"- **Top-Level Files**: {', '.join(top_level_files) if top_level_files else 'None'}",
    ]

    # Inspect key packages & structure ecosystem-agnostic
    ecosystem_detected = False
    
    # Python
    py_deps = _safe_summarize_requirements(root_path / "requirements.txt")
    if not py_deps:
        py_deps = _safe_summarize_pyproject(root_path / "pyproject.toml")
    if py_deps:
        lines.append(f"- **Python Dependencies**: {', '.join(py_deps)}")
        ecosystem_detected = True
        
    # Node.js
    node_deps = _safe_summarize_package_json(root_path / "package.json")
    if node_deps:
        lines.append(f"- **Node.js Dependencies**: {', '.join(node_deps)}")
        ecosystem_detected = True
        
    # Rust
    rust_deps = _safe_summarize_cargo_toml(root_path / "Cargo.toml")
    if rust_deps:
        lines.append(f"- **Rust Dependencies (Cargo.toml)**: {', '.join(rust_deps)}")
        ecosystem_detected = True
        
    # Go
    go_deps = _safe_summarize_go_mod(root_path / "go.mod")
    if go_deps:
        lines.append(f"- **Go Modules (go.mod)**: {', '.join(go_deps)}")
        ecosystem_detected = True

    if not ecosystem_detected:
        lines.append("- **Ecosystem**: Unknown or unstructured (no standard manifests found at root).")

    langgraph_info = _safe_summarize_langgraph_config(root_path / "langgraph.json")
    if langgraph_info.get("graphs"):
        graphs_summary = ", ".join(f"{k}: {v}" for k, v in langgraph_info["graphs"].items())
        lines.append(f"- **LangGraph Configuration**: {graphs_summary}")

    lines.append("- **Excluded from Context**: Virtual environments, version control (.git), node_modules, target, and cache directories.")
    return "\n".join(lines)
