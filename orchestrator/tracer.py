"""Execution tracing module providing observable progress tracking and formatted terminal output."""

import time
from dataclasses import dataclass, field
from typing import List, Optional, Any
from colorama import Fore, Style, init

from orchestrator.types import TokenUsage

init(autoreset=True)


@dataclass
class WorkflowEvent:
    """Structured record of an orchestration milestone."""
    name: str
    stage: str
    message: str
    agent: Optional[str] = None
    role: Optional[str] = None
    model: Optional[str] = None
    timestamp: float = field(default_factory=time.time)
    elapsed_seconds: Optional[float] = None
    status: str = "info"  # "info", "started", "completed", "failed"
    metadata: Optional[dict] = None


class ExecutionTracer:
    """Manages recording and rendering observable orchestration events."""

    def __init__(self, verbose: bool = True):
        self.verbose = verbose
        self.events: List[WorkflowEvent] = []
        self._start_time: float = time.time()
        self._step_timers: dict = {}

    def clear(self) -> None:
        """Clear recorded events and reset timers."""
        self.events.clear()
        self._start_time = time.time()
        self._step_timers.clear()

    def get_events(self) -> List[WorkflowEvent]:
        """Return a copy of all recorded events."""
        return list(self.events)

    def _emit(self, event: WorkflowEvent, console_msg: Optional[str] = None) -> None:
        self.events.append(event)
        if self.verbose and console_msg:
            try:
                print(console_msg)
            except UnicodeEncodeError:
                safe_msg = console_msg.encode("ascii", errors="replace").decode("ascii")
                print(safe_msg)

    # 1. Workflow started
    def log_workflow_start(self, task: str) -> None:
        self.clear()
        self._emit(
            WorkflowEvent(
                name="workflow_started",
                stage="workflow",
                message=f"Starting workflow for task: '{task}'",
                status="started",
                metadata={"task": task},
            ),
            console_msg=(
                f"\n{Fore.CYAN}{Style.BRIGHT}{'='*50}\n"
                f"AI ORCHESTRATOR\n"
                f"{'='*50}{Style.RESET_ALL}\n\n"
                f"{Fore.YELLOW}Task:{Style.RESET_ALL}\n{task}\n"
            ),
        )

    # 2. Context collection started
    def log_context_start(self) -> None:
        self._step_timers["context"] = time.time()
        self._emit(
            WorkflowEvent(
                name="context_started",
                stage="context",
                message="Preparing project context...",
                status="started",
            ),
            console_msg=f"{Fore.CYAN}[1/6] Preparing project context...{Style.RESET_ALL}",
        )

    # 3. Context collection completed
    def log_context_complete(self, root: str, summary_preview: str = "") -> None:
        start = self._step_timers.get("context", time.time())
        elapsed = round(time.time() - start, 2)
        self._emit(
            WorkflowEvent(
                name="context_completed",
                stage="context",
                message=f"Context prepared for root '{root}' in {elapsed}s",
                elapsed_seconds=elapsed,
                status="completed",
                metadata={"root": root},
            ),
            console_msg=(
                f"  {Fore.GREEN}[OK]{Style.RESET_ALL} Project detected: {Fore.WHITE}{root}{Style.RESET_ALL}\n"
                f"  {Fore.GREEN}[OK]{Style.RESET_ALL} Safe context prepared ({elapsed}s)\n"
            ),
        )

    def log_skills_discovered(self, count: int, skill_names: Optional[List[str]] = None) -> None:
        names_str = f": {', '.join(skill_names)}" if skill_names else ""
        if self.verbose:
            try:
                print(f"  {Fore.GREEN}[OK]{Style.RESET_ALL} Skills discovered: {Fore.WHITE}{count} available{names_str}{Style.RESET_ALL}")
            except UnicodeEncodeError:
                print(f"  [OK] Skills discovered: {count} available{names_str}")

    # Generic agent execution methods
    def log_agent_start(
        self,
        agent: str,
        role: str,
        model: Optional[str] = None,
        step_label: str = "[2/6]",
        title: str = "Antigravity",
        action: str = "Investigating project...",
    ) -> None:
        timer_key = f"{agent}_{role}"
        self._step_timers[timer_key] = time.time()
        metadata = {"agent": agent, "role": role}
        if model:
            metadata["model"] = model
        self._emit(
            WorkflowEvent(
                name=f"{agent}_started",
                stage=agent,
                agent=agent,
                role=role,
                model=model,
                message=f"{agent} ({role}, model: {model or 'default'}) started",
                status="started",
                metadata=metadata,
            ),
            console_msg=(
                f"{Fore.CYAN}{step_label} {title}{Style.RESET_ALL}\n"
                f"  {Fore.LIGHTBLACK_EX}Agent: {agent}{Style.RESET_ALL}\n"
                f"  {Fore.LIGHTBLACK_EX}Role: {role}{Style.RESET_ALL}\n"
                + (f"  {Fore.LIGHTBLACK_EX}Model: {model}{Style.RESET_ALL}\n" if model else "")
                + f"  {Fore.BLUE}->{Style.RESET_ALL} {action}"
            ),
        )

    def log_agent_complete(
        self,
        agent: str,
        role: str,
        response_length: int = 0,
        model: Optional[str] = None,
        token_usage: Optional[TokenUsage] = None,
    ) -> float:
        timer_key = f"{agent}_{role}"
        start = self._step_timers.get(timer_key, time.time())
        elapsed = round(time.time() - start, 2)
        metadata = {"agent": agent, "role": role, "response_length": response_length}
        if model:
            metadata["model"] = model

        # Format and record token telemetry
        if token_usage and token_usage.get("available") is True:
            inp = token_usage.get("input_tokens")
            out = token_usage.get("output_tokens")
            tot = token_usage.get("total_tokens")
            if tot is None and inp is not None and out is not None:
                tot = inp + out

            metadata["token_usage"] = {
                "input_tokens": inp,
                "output_tokens": out,
                "total_tokens": tot,
                "available": True,
            }
            if inp is not None:
                metadata["input_tokens"] = inp
            if out is not None:
                metadata["output_tokens"] = out
            if tot is not None:
                metadata["total_tokens"] = tot

            tok_lines = []
            if inp is not None:
                tok_lines.append(f"  {Fore.LIGHTBLACK_EX}Input tokens:{Style.RESET_ALL} {inp:,}")
            if out is not None:
                tok_lines.append(f"  {Fore.LIGHTBLACK_EX}Output tokens:{Style.RESET_ALL} {out:,}")
            if tot is not None:
                tok_lines.append(f"  {Fore.LIGHTBLACK_EX}Total tokens:{Style.RESET_ALL} {tot:,}")
            tokens_msg = ("\n" + "\n".join(tok_lines)) if tok_lines else ""
        else:
            metadata["token_usage"] = "unavailable"
            tokens_msg = f"\n  {Fore.LIGHTBLACK_EX}Token usage:{Style.RESET_ALL} unavailable"

        self._emit(
            WorkflowEvent(
                name=f"{agent}_completed",
                stage=agent,
                agent=agent,
                role=role,
                model=model,
                message=f"{agent} ({role}) completed in {elapsed}s",
                elapsed_seconds=elapsed,
                status="completed",
                metadata=metadata,
            ),
            console_msg=f"  {Fore.GREEN}[OK]{Style.RESET_ALL} Completed ({elapsed}s){tokens_msg}\n",
        )
        return elapsed

    def log_agent_error(
        self,
        agent: str,
        role: str,
        error_message: str,
        model: Optional[str] = None,
    ) -> float:
        timer_key = f"{agent}_{role}"
        start = self._step_timers.get(timer_key, time.time())
        elapsed = round(time.time() - start, 2)
        metadata = {"agent": agent, "role": role, "error": error_message}
        if model:
            metadata["model"] = model
        self._emit(
            WorkflowEvent(
                name="workflow_failed",
                stage=agent,
                agent=agent,
                role=role,
                model=model,
                message=f"Error in {agent} ({role}): {error_message}",
                elapsed_seconds=elapsed,
                status="failed",
                metadata=metadata,
            ),
            console_msg=(
                f"\n{Fore.RED}{Style.BRIGHT}[ERROR] {agent} ({role}) Failed:{Style.RESET_ALL} {error_message}\n"
            ),
        )
        return elapsed

    def log_terminal_launch(
        self,
        agent: str,
        role: str,
        title: str,
        mode: str = "visible",
    ) -> None:
        """Record and display terminal launch lifecycle event."""
        self._emit(
            WorkflowEvent(
                name="terminal_launch",
                stage=agent,
                agent=agent,
                role=role,
                message=f"Terminal launched for {agent} ({role}): '{title}' [{mode}]",
                status="started",
                metadata={"title": title, "mode": mode, "agent": agent, "role": role},
            ),
            console_msg=f"  {Fore.MAGENTA}[TERMINAL]{Style.RESET_ALL} Visible terminal opened: {Fore.WHITE}\"{title}\"{Style.RESET_ALL}",
        )

    def log_terminal_exit(
        self,
        agent: str,
        role: str,
        returncode: int,
        duration: float,
    ) -> None:
        """Record and display terminal process completion lifecycle event."""
        status_str = "completed" if returncode == 0 else "failed"
        self._emit(
            WorkflowEvent(
                name="terminal_process_exit",
                stage=agent,
                agent=agent,
                role=role,
                message=f"Terminal process for {agent} ({role}) exited with code {returncode} in {duration}s",
                elapsed_seconds=duration,
                status=status_str,
                metadata={"returncode": returncode, "agent": agent, "role": role},
            ),
            console_msg=f"  {Fore.MAGENTA}[TERMINAL]{Style.RESET_ALL} Process exited with code {returncode} ({duration}s)",
        )

    # Convenience agent wrappers preserving backward compatibility
    def log_antigravity_start(self, model: Optional[str] = None) -> None:
        self.log_agent_start(
            agent="antigravity",
            role="researcher",
            model=model,
            step_label="[2/6]",
            title="Antigravity",
            action="Investigating project...",
        )

    def log_antigravity_complete(
        self,
        response_length: int,
        model: Optional[str] = None,
        token_usage: Optional[TokenUsage] = None,
    ) -> float:
        return self.log_agent_complete(
            agent="antigravity",
            role="researcher",
            response_length=response_length,
            model=model,
            token_usage=token_usage,
        )

    def log_handoff_to_claude(self) -> None:
        self._emit(
            WorkflowEvent(
                name="handoff_to_claude",
                stage="handoff",
                message="State updated with researcher result; handing off to Claude Code planner",
                status="info",
            ),
            console_msg=None,
        )

    def log_claude_start(self, model: Optional[str] = None) -> None:
        self.log_agent_start(
            agent="claude",
            role="planner",
            model=model,
            step_label="[3/6]",
            title="Claude Code",
            action="Reviewing research...",
        )

    def log_claude_complete(
        self,
        response_length: int,
        model: Optional[str] = None,
        token_usage: Optional[TokenUsage] = None,
    ) -> float:
        return self.log_agent_complete(
            agent="claude",
            role="planner",
            response_length=response_length,
            model=model,
            token_usage=token_usage,
        )

    def log_handoff_to_opencode(self) -> None:
        self._emit(
            WorkflowEvent(
                name="handoff_to_opencode",
                stage="handoff",
                message="State updated with planner result; handing off to OpenCode implementer",
                status="info",
            ),
            console_msg=None,
        )

    def log_opencode_start(self, model: Optional[str] = None) -> None:
        self.log_agent_start(
            agent="opencode",
            role="implementer",
            model=model,
            step_label="[4/6]",
            title="OpenCode",
            action="Implementing changes in workspace...",
        )

    def log_opencode_complete(
        self,
        response_length: int = 0,
        model: Optional[str] = None,
        token_usage: Optional[TokenUsage] = None,
    ) -> float:
        return self.log_agent_complete(
            agent="opencode",
            role="implementer",
            response_length=response_length,
            model=model,
            token_usage=token_usage,
        )

    def log_handoff_to_verifier(self) -> None:
        self._emit(
            WorkflowEvent(
                name="handoff_to_verifier",
                stage="handoff",
                message="State updated with implementer result; handing off to Claude Code verifier",
                status="info",
            ),
            console_msg=None,
        )

    def log_verifier_start(self, agent: str = "claude", model: Optional[str] = None) -> None:
        title = "Claude Code" if agent == "claude" else agent.capitalize()
        self.log_agent_start(
            agent=agent,
            role="verifier",
            model=model,
            step_label="[5/6]",
            title=title,
            action="Verifying implementation against acceptance criteria...",
        )

    def log_verifier_complete(
        self,
        agent: str = "claude",
        verdict: str = "UNKNOWN",
        response_length: int = 0,
        model: Optional[str] = None,
        token_usage: Optional[TokenUsage] = None,
    ) -> float:
        timer_key = f"{agent}_verifier"
        start = self._step_timers.get(timer_key, time.time())
        elapsed = round(time.time() - start, 2)
        metadata = {
            "agent": agent,
            "role": "verifier",
            "verdict": verdict,
            "response_length": response_length,
        }
        if model:
            metadata["model"] = model

        # Format and record token telemetry
        if token_usage and token_usage.get("available") is True:
            inp = token_usage.get("input_tokens")
            out = token_usage.get("output_tokens")
            tot = token_usage.get("total_tokens")
            if tot is None and inp is not None and out is not None:
                tot = inp + out

            metadata["token_usage"] = {
                "input_tokens": inp,
                "output_tokens": out,
                "total_tokens": tot,
                "available": True,
            }
            if inp is not None:
                metadata["input_tokens"] = inp
            if out is not None:
                metadata["output_tokens"] = out
            if tot is not None:
                metadata["total_tokens"] = tot

            tok_lines = []
            if inp is not None:
                tok_lines.append(f"  {Fore.LIGHTBLACK_EX}Input tokens:{Style.RESET_ALL} {inp:,}")
            if out is not None:
                tok_lines.append(f"  {Fore.LIGHTBLACK_EX}Output tokens:{Style.RESET_ALL} {out:,}")
            if tot is not None:
                tok_lines.append(f"  {Fore.LIGHTBLACK_EX}Total tokens:{Style.RESET_ALL} {tot:,}")
            tokens_msg = ("\n" + "\n".join(tok_lines)) if tok_lines else ""
        else:
            metadata["token_usage"] = "unavailable"
            tokens_msg = f"\n  {Fore.LIGHTBLACK_EX}Token usage:{Style.RESET_ALL} unavailable"

        verdict_color = Fore.GREEN if verdict == "PASS" else (Fore.RED if verdict == "FAIL" else Fore.YELLOW)
        self._emit(
            WorkflowEvent(
                name="verifier_completed",
                stage="verifier",
                agent=agent,
                role="verifier",
                model=model,
                message=f"{agent} (verifier) completed in {elapsed}s with verdict: {verdict}",
                elapsed_seconds=elapsed,
                status="completed",
                metadata=metadata,
            ),
            console_msg=(
                f"  {Fore.GREEN}[OK]{Style.RESET_ALL} Completed ({elapsed}s) "
                f"| Verdict: {verdict_color}{Style.BRIGHT}{verdict}{Style.RESET_ALL}{tokens_msg}\n"
            ),
        )
        return elapsed

    def log_repair_start(
        self,
        attempt: int,
        max_attempts: int,
        agent: str = "opencode",
        model: Optional[str] = None,
    ) -> None:
        timer_key = f"{agent}_repair_{attempt}"
        self._step_timers[timer_key] = time.time()
        metadata = {"agent": agent, "role": "implementer", "attempt": attempt, "max_attempts": max_attempts}
        if model:
            metadata["model"] = model
        self._emit(
            WorkflowEvent(
                name="repair_started",
                stage="repair",
                agent=agent,
                role="implementer",
                model=model,
                message=f"Starting repair attempt {attempt}/{max_attempts} with {agent}",
                status="started",
                metadata=metadata,
            ),
            console_msg=(
                f"{Fore.YELLOW}{Style.BRIGHT}[REPAIR {attempt}/{max_attempts}] OpenCode (implementer){Style.RESET_ALL}\n"
                f"  {Fore.LIGHTBLACK_EX}Agent: {agent}{Style.RESET_ALL}\n"
                f"  {Fore.LIGHTBLACK_EX}Role: implementer{Style.RESET_ALL}\n"
                + (f"  {Fore.LIGHTBLACK_EX}Model: {model}{Style.RESET_ALL}\n" if model else "")
                + f"  {Fore.YELLOW}->{Style.RESET_ALL} Implementing repairs based on verifier feedback..."
            ),
        )

    def log_repair_complete(
        self,
        attempt: int,
        agent: str = "opencode",
        response_length: int = 0,
        model: Optional[str] = None,
        token_usage: Optional[TokenUsage] = None,
    ) -> float:
        timer_key = f"{agent}_repair_{attempt}"
        start = self._step_timers.get(timer_key, time.time())
        elapsed = round(time.time() - start, 2)
        metadata = {
            "agent": agent,
            "role": "implementer",
            "attempt": attempt,
            "response_length": response_length,
        }
        if model:
            metadata["model"] = model

        # Format and record token telemetry
        if token_usage and token_usage.get("available") is True:
            inp = token_usage.get("input_tokens")
            out = token_usage.get("output_tokens")
            tot = token_usage.get("total_tokens")
            if tot is None and inp is not None and out is not None:
                tot = inp + out

            metadata["token_usage"] = {
                "input_tokens": inp,
                "output_tokens": out,
                "total_tokens": tot,
                "available": True,
            }
            if inp is not None:
                metadata["input_tokens"] = inp
            if out is not None:
                metadata["output_tokens"] = out
            if tot is not None:
                metadata["total_tokens"] = tot

            tok_lines = []
            if inp is not None:
                tok_lines.append(f"  {Fore.LIGHTBLACK_EX}Input tokens:{Style.RESET_ALL} {inp:,}")
            if out is not None:
                tok_lines.append(f"  {Fore.LIGHTBLACK_EX}Output tokens:{Style.RESET_ALL} {out:,}")
            if tot is not None:
                tok_lines.append(f"  {Fore.LIGHTBLACK_EX}Total tokens:{Style.RESET_ALL} {tot:,}")
            tokens_msg = ("\n" + "\n".join(tok_lines)) if tok_lines else ""
        else:
            metadata["token_usage"] = "unavailable"
            tokens_msg = f"\n  {Fore.LIGHTBLACK_EX}Token usage:{Style.RESET_ALL} unavailable"

        self._emit(
            WorkflowEvent(
                name="repair_completed",
                stage="repair",
                agent=agent,
                role="implementer",
                model=model,
                message=f"Repair attempt {attempt} completed in {elapsed}s",
                elapsed_seconds=elapsed,
                status="completed",
                metadata=metadata,
            ),
            console_msg=(
                f"  {Fore.GREEN}[OK]{Style.RESET_ALL} Repair #{attempt} completed ({elapsed}s){tokens_msg}\n"
            ),
        )
        return elapsed

    def log_reverification_start(
        self,
        attempt: int,
        agent: str = "claude",
        model: Optional[str] = None,
    ) -> None:
        timer_key = f"{agent}_verifier_{attempt}"
        self._step_timers[timer_key] = time.time()
        title = "Claude Code" if agent == "claude" else agent.capitalize()
        metadata = {"agent": agent, "role": "verifier", "attempt": attempt}
        if model:
            metadata["model"] = model
        self._emit(
            WorkflowEvent(
                name="reverification_started",
                stage="verifier",
                agent=agent,
                role="verifier",
                model=model,
                message=f"Re-verification attempt {attempt} started with {agent}",
                status="started",
                metadata=metadata,
            ),
            console_msg=(
                f"{Fore.CYAN}{Style.BRIGHT}[RE-VERIFY {attempt}] {title} (verifier){Style.RESET_ALL}\n"
                f"  {Fore.LIGHTBLACK_EX}Agent: {agent}{Style.RESET_ALL}\n"
                f"  {Fore.LIGHTBLACK_EX}Role: verifier{Style.RESET_ALL}\n"
                + (f"  {Fore.LIGHTBLACK_EX}Model: {model}{Style.RESET_ALL}\n" if model else "")
                + f"  {Fore.BLUE}->{Style.RESET_ALL} Re-verifying repaired implementation..."
            ),
        )

    def log_repair_decision(
        self,
        verdict: str,
        repair_attempts: int,
        max_repair_attempts: int,
    ) -> None:
        if verdict != "PASS":
            if repair_attempts < max_repair_attempts:
                msg = f"Verification failed ({verdict}). Triggering repair attempt {repair_attempts + 1} of {max_repair_attempts}..."
                self._emit(
                    WorkflowEvent(
                        name="repair_decision",
                        stage="decision",
                        message=msg,
                        status="info",
                        metadata={
                            "verdict": verdict,
                            "repair_attempts": repair_attempts,
                            "max_repair_attempts": max_repair_attempts,
                            "action": "repair",
                        },
                    ),
                    console_msg=f"  {Fore.YELLOW}[INFO]{Style.RESET_ALL} {msg}\n",
                )
            else:
                msg = f"Verification failed ({verdict}) and max repair attempts ({max_repair_attempts}) reached. Ending workflow."
                self._emit(
                    WorkflowEvent(
                        name="repair_decision",
                        stage="decision",
                        message=msg,
                        status="info",
                        metadata={
                            "verdict": verdict,
                            "repair_attempts": repair_attempts,
                            "max_repair_attempts": max_repair_attempts,
                            "action": "end",
                        },
                    ),
                    console_msg=f"  {Fore.RED}[EXHAUSTED]{Style.RESET_ALL} {msg}\n",
                )

    # ------------------------------------------------------------------
    # Preflight (Tier 1 #6)
    # ------------------------------------------------------------------
    def log_preflight_start(self, deep: bool = False) -> None:
        mode = "deep" if deep else "shallow"
        self._emit(
            WorkflowEvent(
                name="preflight_started",
                stage="preflight",
                message=f"Running {mode} preflight checks on configured agents",
                status="started",
                metadata={"deep": deep},
            ),
            console_msg=(
                f"{Fore.CYAN}[1.5/6] Preflight{Style.RESET_ALL}\n"
                f"  {Fore.LIGHTBLACK_EX}Probing configured agents ({mode})...{Style.RESET_ALL}"
            ),
        )

    def log_preflight_skipped(self) -> None:
        self._emit(
            WorkflowEvent(
                name="preflight_skipped",
                stage="preflight",
                message="Preflight checks skipped",
                status="info",
                metadata={"skipped": True},
            ),
            console_msg=f"  {Fore.YELLOW}[SKIP]{Style.RESET_ALL} Preflight checks skipped\n",
        )

    def log_preflight_complete(
        self,
        ok: bool,
        errors: Optional[List[str]] = None,
        warnings: Optional[List[str]] = None,
        duration_seconds: float = 0.0,
    ) -> None:
        errors = errors or []
        warnings = warnings or []
        lines: List[str] = []
        if ok:
            lines.append(
                f"  {Fore.GREEN}[OK]{Style.RESET_ALL} All agents available "
                f"{Fore.LIGHTBLACK_EX}({duration_seconds}s){Style.RESET_ALL}"
            )
        else:
            lines.append(
                f"  {Fore.RED}[FAIL]{Style.RESET_ALL} Preflight failed "
                f"{Fore.LIGHTBLACK_EX}({duration_seconds}s){Style.RESET_ALL}"
            )
        for msg in errors:
            lines.append(f"    {Fore.RED}-{Style.RESET_ALL} {msg}")
        for msg in warnings:
            lines.append(f"    {Fore.YELLOW}-{Style.RESET_ALL} {msg}")

        self._emit(
            WorkflowEvent(
                name="preflight_completed",
                stage="preflight",
                message=("Preflight passed" if ok else "Preflight failed"),
                elapsed_seconds=duration_seconds,
                status="completed" if ok else "failed",
                metadata={"ok": ok, "errors": errors, "warnings": warnings},
            ),
            console_msg="\n".join(lines) + "\n",
        )

    # ------------------------------------------------------------------
    # Blocked verdict (Tier 1 #7)
    # ------------------------------------------------------------------
    def log_blocked(self, reason: str) -> None:
        self._emit(
            WorkflowEvent(
                name="verification_blocked",
                stage="decision",
                message=f"Verification blocked: {reason}",
                status="info",
                metadata={"verdict": "BLOCKED", "reason": reason, "action": "end"},
            ),
            console_msg=(
                f"  {Fore.MAGENTA}{Style.BRIGHT}[BLOCKED]{Style.RESET_ALL} "
                f"Human input required; repair attempts were not consumed.\n"
                f"    {Fore.MAGENTA}-{Style.RESET_ALL} {reason}\n"
            ),
        )

    # ------------------------------------------------------------------
    # Durable run store (Tier 1 #4)
    # ------------------------------------------------------------------
    def log_run_store_opened(self, run_id: str, run_dir: str) -> None:
        self._emit(
            WorkflowEvent(
                name="run_store_opened",
                stage="context",
                message=f"Recording run {run_id}",
                status="info",
                metadata={"run_id": run_id, "run_dir": run_dir},
            ),
            console_msg=(
                f"  {Fore.LIGHTBLACK_EX}Run ID:{Style.RESET_ALL} {run_id}\n"
                f"  {Fore.LIGHTBLACK_EX}Recording to:{Style.RESET_ALL} {run_dir}\n"
            ),
        )

    def log_run_store_closed(self, run_id: str, run_dir: str, status: str) -> None:
        self._emit(
            WorkflowEvent(
                name="run_store_closed",
                stage="workflow",
                message=f"Run {run_id} recorded with status '{status}'",
                status="info",
                metadata={"run_id": run_id, "run_dir": run_dir, "final_status": status},
            ),
            console_msg=(
                f"{Fore.LIGHTBLACK_EX}Run saved:{Style.RESET_ALL} {run_dir} "
                f"{Fore.LIGHTBLACK_EX}(status: {status}){Style.RESET_ALL}\n"
            ),
        )

    def log_run_store_degraded(self, reason: str) -> None:
        self._emit(
            WorkflowEvent(
                name="run_store_degraded",
                stage="workflow",
                message=f"Run store degraded: {reason}",
                status="info",
                metadata={"reason": reason},
            ),
            console_msg=(
                f"  {Fore.YELLOW}[WARN]{Style.RESET_ALL} Run was not fully recorded: {reason}\n"
            ),
        )

    # ------------------------------------------------------------------
    # Workspace isolation (Tier 0 #3)
    # ------------------------------------------------------------------
    def log_workspace_isolated(self, path: str, branch: str) -> None:
        self._emit(
            WorkflowEvent(
                name="workspace_isolated",
                stage="context",
                message=f"Run isolated in worktree {path} on branch {branch}",
                status="info",
                metadata={"path": path, "branch": branch, "isolated": True},
            ),
            console_msg=(
                f"  {Fore.LIGHTBLACK_EX}Workspace:{Style.RESET_ALL} {path}\n"
                f"  {Fore.LIGHTBLACK_EX}Branch:{Style.RESET_ALL}    {branch} "
                f"{Fore.GREEN}(isolated){Style.RESET_ALL}\n"
            ),
        )

    def log_workspace_not_isolated(self, reason: str) -> None:
        self._emit(
            WorkflowEvent(
                name="workspace_not_isolated",
                stage="context",
                message=f"Run is NOT isolated: {reason}",
                status="info",
                metadata={"isolated": False, "reason": reason},
            ),
            console_msg=(
                f"  {Fore.YELLOW}[WARN]{Style.RESET_ALL} Workspace not isolated - "
                f"agents will edit the project directly ({reason})\n"
            ),
        )

    def log_workspace_result(
        self,
        description: str,
        change_count: int = 0,
        commit: Optional[str] = None,
    ) -> None:
        commit_line = f"\n  {Fore.LIGHTBLACK_EX}Committed:{Style.RESET_ALL} {commit}" if commit else ""
        self._emit(
            WorkflowEvent(
                name="workspace_result",
                stage="workflow",
                message=f"Run changed {change_count} path(s)",
                status="info",
                metadata={"change_count": change_count, "commit": commit},
            ),
            console_msg=(
                f"\n{Fore.CYAN}{description}{Style.RESET_ALL}\n"
                f"  {Fore.LIGHTBLACK_EX}Changed paths:{Style.RESET_ALL} {change_count}"
                f"{commit_line}\n"
            ),
        )

    # ------------------------------------------------------------------
    # Ensemble phases and verifier consensus (Tier 2 #8)
    # ------------------------------------------------------------------
    def log_phase_partial_failure(
        self,
        role: str,
        failed: int,
        total: int,
        surviving_agents: Optional[List[str]] = None,
    ) -> None:
        survivors = ", ".join(surviving_agents or []) or "none"
        self._emit(
            WorkflowEvent(
                name="phase_partial_failure",
                stage=role,
                role=role,
                message=f"{failed} of {total} '{role}' agents failed; continuing on the survivors",
                status="info",
                metadata={
                    "role": role,
                    "failed": failed,
                    "total": total,
                    "surviving_agents": surviving_agents or [],
                },
            ),
            console_msg=(
                f"  {Fore.YELLOW}[WARN]{Style.RESET_ALL} {failed} of {total} "
                f"'{role}' agents failed; continuing with: {survivors}\n"
            ),
        )

    def log_consensus(
        self,
        verdicts: List[Any],
        policy: str,
        resolved: str,
    ) -> None:
        lines = [
            f"  {Fore.CYAN}Consensus{Style.RESET_ALL} "
            f"{Fore.LIGHTBLACK_EX}(policy: {policy}){Style.RESET_ALL}"
        ]
        for label, verdict in verdicts:
            color = (
                Fore.GREEN if verdict == "PASS"
                else Fore.RED if verdict == "FAIL"
                else Fore.MAGENTA if verdict == "BLOCKED"
                else Fore.YELLOW
            )
            lines.append(f"    {label:<28} {color}{verdict}{Style.RESET_ALL}")
        resolved_color = (
            Fore.GREEN if resolved == "PASS"
            else Fore.RED if resolved == "FAIL"
            else Fore.MAGENTA if resolved == "BLOCKED"
            else Fore.YELLOW
        )
        lines.append(
            f"    {'-> resolved':<28} {resolved_color}{Style.BRIGHT}{resolved}{Style.RESET_ALL}"
        )
        self._emit(
            WorkflowEvent(
                name="verifier_consensus",
                stage="verifier",
                role="verifier",
                message=f"Consensus ({policy}) resolved to {resolved}",
                status="info",
                metadata={
                    "policy": policy,
                    "verdicts": [{"label": l, "verdict": v} for l, v in verdicts],
                    "resolved": resolved,
                },
            ),
            console_msg="\n".join(lines) + "\n",
        )

    # Workflow completed
    def log_workflow_complete(self) -> None:
        total_elapsed = round(time.time() - self._start_time, 2)
        self._emit(
            WorkflowEvent(
                name="workflow_completed",
                stage="workflow",
                message=f"Workflow completed in {total_elapsed}s",
                elapsed_seconds=total_elapsed,
                status="completed",
            ),
            console_msg=(
                f"{Fore.CYAN}[6/6] Workflow complete{Style.RESET_ALL} "
                f"{Fore.LIGHTBLACK_EX}(Total time: {total_elapsed}s){Style.RESET_ALL}\n\n"
                f"{Fore.CYAN}{Style.BRIGHT}{'='*50}\n"
                f"RESULT\n"
                f"{'='*50}{Style.RESET_ALL}\n"
            ),
        )

    # 10. Failure event
    def log_error(
        self,
        stage: str,
        error_message: str,
        agent: Optional[str] = None,
        role: Optional[str] = None,
    ) -> None:
        total_elapsed = round(time.time() - self._start_time, 2)
        self._emit(
            WorkflowEvent(
                name="workflow_failed",
                stage=stage,
                agent=agent,
                role=role,
                message=f"Error in stage '{stage}': {error_message}",
                elapsed_seconds=total_elapsed,
                status="failed",
                metadata={"stage": stage, "error": error_message},
            ),
            console_msg=(
                f"\n{Fore.RED}{Style.BRIGHT}[ERROR] Workflow Failed at [{stage}]:{Style.RESET_ALL} {error_message}\n"
            ),
        )


# Global default tracer instance
default_tracer = ExecutionTracer(verbose=True)
