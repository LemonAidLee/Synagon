"""Preflight probing (Tier 1 #6).

Every configured agent is probed *before* the first one is launched, so that an
unusable environment fails in seconds rather than six minutes into a run — after
two agents have already burned tokens.

Checks performed per configured agent entry:

======================  ========  ==========================================
Check                   Severity  Question answered
======================  ========  ==========================================
``runner``              error     Does the orchestrator know how to run this agent?
``executable``          error     Does the agent's CLI binary resolve on this machine?
``model_catalog``       error     Is the configured model present in the catalog?
``role``                warning   Does the role have a responsibility defined?
``version``             warning   Does the binary actually respond? (deep mode only)
======================  ========  ==========================================

Shallow mode (the default) performs no subprocess calls, so it is fast and safe
to run unconditionally. Deep mode adds one short ``--version`` invocation per
distinct agent to confirm the binary really executes.

Probing is read-only: it never launches an agent, never sends a prompt, and
never writes to the workspace.
"""

import shutil
import subprocess
import time
from typing import Any, Callable, Dict, List, Optional, TypedDict

from orchestrator.agents.antigravity import get_antigravity_executable_path
from orchestrator.agents.claude_code import get_claude_executable_path
from orchestrator.agents.opencode import get_opencode_executable_path
from orchestrator.config import (
    OrchestratorConfig,
    get_acceptance_config,
    get_available_models,
    validate_model,
)

SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"

#: Agents the orchestrator knows how to execute, and how to locate their binary.
AGENT_EXECUTABLE_RESOLVERS: Dict[str, Callable[[], str]] = {
    "antigravity": get_antigravity_executable_path,
    "claude": get_claude_executable_path,
    "opencode": get_opencode_executable_path,
}

#: Arguments used to confirm a binary responds, in deep mode.
AGENT_VERSION_ARGS: Dict[str, List[str]] = {
    "antigravity": ["--version"],
    "claude": ["--version"],
    "opencode": ["--version"],
}


class CheckResult(TypedDict, total=False):
    """One individual preflight assertion."""
    name: str
    ok: bool
    severity: str
    detail: str


class ProbeResult(TypedDict, total=False):
    """The outcome of probing a single configured agent entry."""
    agent: str
    role: str
    model: Optional[str]
    ok: bool
    checks: List[CheckResult]
    executable: Optional[str]
    version: Optional[str]
    errors: List[str]
    warnings: List[str]


class PreflightReport(TypedDict, total=False):
    """The aggregate result of probing every configured agent."""
    ok: bool
    strict: bool
    deep: bool
    probes: List[ProbeResult]
    errors: List[str]
    warnings: List[str]
    duration_seconds: float
    checked_at: float
    skipped: bool


def _check(name: str, ok: bool, detail: str, severity: str = SEVERITY_ERROR) -> CheckResult:
    return {"name": name, "ok": ok, "severity": severity, "detail": detail}


def _probe_version(agent: str, executable: str, timeout: int) -> Optional[str]:
    """Run the agent's version command. Returns the reported version, or None.

    Uses ``shell=False`` and a detached stdin, matching the safety contract the
    rest of the orchestrator's subprocess handling follows.
    """
    args = AGENT_VERSION_ARGS.get(agent)
    if not args:
        return None
    try:
        completed = subprocess.run(
            [executable] + args,
            shell=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
        )
    except Exception:
        return None

    raw = (completed.stdout or b"") + (completed.stderr or b"")
    text = raw.decode("utf-8", errors="replace").strip()
    if completed.returncode != 0 and not text:
        return None
    first_line = text.splitlines()[0].strip() if text else ""
    return first_line or None


def probe_agent(
    agent: str,
    role: str,
    model: Optional[str],
    config: Optional[OrchestratorConfig] = None,
    deep: bool = False,
    timeout: int = 15,
) -> ProbeResult:
    """Probe one configured agent entry without launching it.

    Args:
        agent: Execution provider name (e.g. ``"claude"``).
        role: Role the agent is assigned (e.g. ``"verifier"``).
        model: Configured model id, if any.
        config: Loaded configuration, used for catalog and role validation.
        deep: When True, additionally invoke the binary's version command.
        timeout: Seconds allowed for the deep version probe.

    Returns:
        A ProbeResult. ``ok`` is False when any error-severity check failed.
    """
    checks: List[CheckResult] = []
    executable: Optional[str] = None
    version: Optional[str] = None

    # 1. Is this an agent we can actually run?
    resolver = AGENT_EXECUTABLE_RESOLVERS.get(agent)
    if resolver is None:
        known = ", ".join(sorted(AGENT_EXECUTABLE_RESOLVERS))
        checks.append(
            _check(
                "runner",
                False,
                f"Unknown agent '{agent}'. The orchestrator can run: {known}.",
            )
        )
    else:
        checks.append(_check("runner", True, f"Agent '{agent}' has a registered runner."))

        # 2. Does its binary resolve on this machine?
        try:
            executable = resolver()
            checks.append(_check("executable", True, f"Resolved to {executable}"))
        except Exception as exc:
            checks.append(
                _check(
                    "executable",
                    False,
                    f"{exc} Install the '{agent}' CLI, or assign this role to a different agent "
                    f"in orchestrator.yaml.",
                )
            )

    # 3. Is the configured model in the catalog?
    #    A model may be an escalation ladder; every rung is checked.
    if model:
        ladder = list(model) if isinstance(model, (list, tuple)) else [model]
        if config:
            missing = [m for m in ladder if not validate_model(config, agent, m)]
            if not missing:
                if len(ladder) > 1:
                    detail = (
                        f"Escalation ladder of {len(ladder)} models is in the {agent} catalog: "
                        f"{' -> '.join(ladder)}"
                    )
                else:
                    detail = f"Model '{ladder[0]}' is in the {agent} catalog."
                checks.append(_check("model_catalog", True, detail))
            else:
                available = [m.get("id", "") for m in get_available_models(config, agent)]
                hint = ", ".join(available) if available else "(catalog is empty)"
                checks.append(
                    _check(
                        "model_catalog",
                        False,
                        f"Model(s) {', '.join(repr(m) for m in missing)} not in the {agent} "
                        f"catalog. Available: {hint}",
                    )
                )
        else:
            checks.append(
                _check(
                    "model_catalog",
                    True,
                    "No configuration supplied; catalog check skipped.",
                    severity=SEVERITY_WARNING,
                )
            )
    else:
        checks.append(
            _check(
                "model_catalog",
                True,
                f"No model configured for {agent}; the CLI default will be used.",
                severity=SEVERITY_WARNING,
            )
        )

    # 4. Does the role carry a responsibility?
    roles = (config or {}).get("roles") or {}
    if role and role in roles and (roles[role] or {}).get("responsibility"):
        checks.append(_check("role", True, f"Role '{role}' has a responsibility defined."))
    else:
        checks.append(
            _check(
                "role",
                False,
                f"Role '{role}' has no responsibility defined in orchestrator.yaml; "
                f"the agent will receive a generic prompt.",
                severity=SEVERITY_WARNING,
            )
        )

    # 5. Deep mode only: does the binary actually respond?
    if deep and executable:
        version = _probe_version(agent, executable, timeout)
        if version:
            checks.append(_check("version", True, version))
        else:
            checks.append(
                _check(
                    "version",
                    False,
                    f"'{agent}' did not report a version within {timeout}s. "
                    f"It may be broken, unauthenticated, or slow to start.",
                    severity=SEVERITY_WARNING,
                )
            )

    errors = [
        c["detail"] for c in checks if not c["ok"] and c.get("severity") == SEVERITY_ERROR
    ]
    warnings = [
        c["detail"] for c in checks if not c["ok"] and c.get("severity") == SEVERITY_WARNING
    ]

    return {
        "agent": agent,
        "role": role,
        "model": model,
        "ok": not errors,
        "checks": checks,
        "executable": executable,
        "version": version,
        "errors": errors,
        "warnings": warnings,
    }


def _model_label(model: Any) -> str:
    """Render a model id or escalation ladder as a single comparable label."""
    if isinstance(model, (list, tuple)):
        return " -> ".join(str(m) for m in model)
    return str(model) if model else "(CLI default)"


def check_verifier_independence(config: Optional[OrchestratorConfig]) -> List[str]:
    """Warn when the verifier shares a model with the planner or implementer.

    Independence is the verifier's entire premise. A verifier running the same
    model as the planner grades an implementation against a plan its own model
    wrote, inheriting that model's blind spots and its disposition to consider
    the plan sound. The same applies to a verifier sharing the implementer's
    model: it is then reviewing its own work.

    This is a warning, not an error — a single-vendor setup is a legitimate
    choice, it just should not be an accidental one.

    Args:
        config: The loaded configuration.

    Returns:
        Warning strings, empty when the verifier is independent.
    """
    agents = (config or {}).get("agents") or []
    verifiers = [a for a in agents if a.get("role") == "verifier"]
    if not verifiers:
        return []

    warnings: List[str] = []
    for other_role in ("planner", "implementer"):
        others = [a for a in agents if a.get("role") == other_role]
        if not others:
            continue
        for verifier in verifiers:
            for other in others:
                same_agent = verifier.get("agent") == other.get("agent")
                same_model = _model_label(verifier.get("model")) == _model_label(other.get("model"))
                if same_agent and same_model:
                    detail = (
                        "the verifier is grading a plan its own model wrote"
                        if other_role == "planner"
                        else "the verifier is reviewing its own implementation"
                    )
                    warnings.append(
                        f"verifier independence: the verifier and the {other_role} both use "
                        f"{verifier.get('agent')}/{_model_label(verifier.get('model'))}, so "
                        f"{detail}. Assign a different agent or model to one of them for an "
                        f"independent check."
                    )
    return warnings


def check_acceptance_command(config: Optional[OrchestratorConfig]) -> List[str]:
    """Warn when the configured acceptance command cannot be found on PATH.

    A gate whose binary does not resolve fails every run for a reason that has nothing to do
    with the code being written, and the failure appears minutes in, after two agents have
    been paid for. Catching it here costs nothing.

    Only a warning: the command may be provided by an environment that exists at run time but
    not at probe time, and preflight must not become a reason a working project cannot run.
    """
    from orchestrator.acceptance import parse_command

    argv = parse_command(get_acceptance_config(config).get("command"))
    if not argv:
        return []

    executable = argv[0]
    if shutil.which(executable) is None:
        return [
            f"acceptance gate: '{executable}' was not found on PATH, so the check "
            f"'{' '.join(argv)}' will fail before it runs. Fix the command or set "
            f"verification.acceptance.command to '' to disable the gate."
        ]
    return []


def run_preflight(
    config: Optional[OrchestratorConfig],
    deep: bool = False,
    strict: bool = True,
    timeout: int = 15,
) -> PreflightReport:
    """Probe every agent in the configuration.

    Args:
        config: Loaded orchestrator configuration.
        deep: When True, invoke each distinct binary's version command.
        strict: Recorded on the report; the caller decides whether to halt.
        timeout: Seconds allowed per deep version probe.

    Returns:
        A PreflightReport. ``ok`` is False when any agent probe reported an error.
    """
    started = time.time()
    probes: List[ProbeResult] = []

    agents = (config or {}).get("agents") or []
    if not agents:
        return {
            "ok": False,
            "strict": strict,
            "deep": deep,
            "probes": [],
            "errors": ["No agents are configured in orchestrator.yaml."],
            "warnings": [],
            "duration_seconds": round(time.time() - started, 3),
            "checked_at": started,
            "skipped": False,
        }

    # Deep-probe each distinct binary only once, however many roles use it.
    version_cache: Dict[str, Optional[str]] = {}

    for entry in agents:
        agent = entry.get("agent") or ""
        role = entry.get("role") or ""
        model = entry.get("model")

        use_deep = deep and agent not in version_cache
        probe = probe_agent(
            agent=agent,
            role=role,
            model=model,
            config=config,
            deep=use_deep,
            timeout=timeout,
        )
        if use_deep:
            version_cache[agent] = probe.get("version")
        elif deep and probe.get("executable"):
            # Reuse the cached version rather than re-invoking the same binary.
            cached = version_cache.get(agent)
            probe["version"] = cached
            if cached:
                probe.setdefault("checks", []).append(
                    _check("version", True, f"{cached} (cached)")
                )
        probes.append(probe)

    errors: List[str] = []
    warnings: List[str] = []
    for probe in probes:
        label = f"{probe['agent']} ({probe['role']})"
        errors.extend(f"{label}: {msg}" for msg in probe.get("errors") or [])
        warnings.extend(f"{label}: {msg}" for msg in probe.get("warnings") or [])

    warnings.extend(check_verifier_independence(config))
    warnings.extend(check_acceptance_command(config))

    return {
        "ok": not errors,
        "strict": strict,
        "deep": deep,
        "probes": probes,
        "errors": errors,
        "warnings": warnings,
        "duration_seconds": round(time.time() - started, 3),
        "checked_at": started,
        "skipped": False,
    }


def skipped_report(reason: str = "Preflight disabled.") -> PreflightReport:
    """Return a report representing a deliberately skipped preflight."""
    return {
        "ok": True,
        "strict": False,
        "deep": False,
        "probes": [],
        "errors": [],
        "warnings": [reason],
        "duration_seconds": 0.0,
        "checked_at": time.time(),
        "skipped": True,
    }


def format_preflight_report(report: PreflightReport, color: bool = True) -> str:
    """Render a preflight report as human-readable text.

    Args:
        report: The report to render.
        color: When True, wrap status markers in ANSI colors via colorama.

    Returns:
        A multi-line string suitable for terminal output.
    """
    if color:
        from colorama import Fore, Style

        green, red, yellow, cyan, dim, reset, bright = (
            Fore.GREEN,
            Fore.RED,
            Fore.YELLOW,
            Fore.CYAN,
            Fore.LIGHTBLACK_EX,
            Style.RESET_ALL,
            Style.BRIGHT,
        )
    else:
        green = red = yellow = cyan = dim = reset = bright = ""

    lines: List[str] = []
    lines.append(f"{cyan}{bright}PREFLIGHT{reset}")

    if report.get("skipped"):
        lines.append(f"  {yellow}[SKIP]{reset} Preflight was skipped.")
        return "\n".join(lines)

    mode = "deep" if report.get("deep") else "shallow"
    lines.append(
        f"  {dim}Mode: {mode} | Duration: {report.get('duration_seconds', 0.0)}s"
        f" | Strict: {report.get('strict')}{reset}"
    )
    lines.append("")

    for probe in report.get("probes") or []:
        marker = f"{green}[OK]{reset}" if probe.get("ok") else f"{red}[FAIL]{reset}"
        model = probe.get("model") or "(CLI default)"
        lines.append(
            f"  {marker} {bright}{probe.get('agent')}{reset} "
            f"{dim}role={probe.get('role')} model={model}{reset}"
        )
        for check in probe.get("checks") or []:
            if check.get("ok"):
                sub = f"{green}ok{reset}"
            elif check.get("severity") == SEVERITY_WARNING:
                sub = f"{yellow}warn{reset}"
            else:
                sub = f"{red}fail{reset}"
            lines.append(f"      {sub:<20} {check.get('name'):<14} {check.get('detail')}")
        lines.append("")

    if report.get("errors"):
        lines.append(f"  {red}{bright}Errors ({len(report['errors'])}):{reset}")
        for msg in report["errors"]:
            lines.append(f"    {red}-{reset} {msg}")
        lines.append("")

    if report.get("warnings"):
        lines.append(f"  {yellow}{bright}Warnings ({len(report['warnings'])}):{reset}")
        for msg in report["warnings"]:
            lines.append(f"    {yellow}-{reset} {msg}")
        lines.append("")

    if report.get("ok"):
        lines.append(f"  {green}{bright}All preflight checks passed.{reset}")
    else:
        lines.append(f"  {red}{bright}Preflight failed. No agent was launched.{reset}")

    return "\n".join(lines)
