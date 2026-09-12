"""Execution tracing module providing observable progress tracking and formatted terminal output."""

import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any
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


def _attempt_tokens_note(token_usage: Optional[TokenUsage]) -> str:
    """"12,232 tokens reported; " for a failed attempt that reported usage, else nothing."""
    if not token_usage or token_usage.get("available") is not True:
        return ""
    total = token_usage.get("total_tokens")
    if not isinstance(total, int):
        return ""
    return f"{total:,} tokens reported; "


class ExecutionTracer:
    """Manages recording and rendering observable orchestration events."""

    def __init__(self, verbose: bool = True):
        self.verbose = verbose
        self.events: List[WorkflowEvent] = []
        self._start_time: float = time.time()
        self._step_timers: dict = {}
        # Parallel tasks (Roadmap Phase 3) emit through this one tracer from several threads.
        # The lock keeps a line from being interleaved with another line; the thread-local
        # label says which task a line belongs to, which is the difference between watching a
        # delegated goal and watching noise.
        self._lock = threading.RLock()
        self._local = threading.local()

    def clear(self) -> None:
        """Clear recorded events and reset timers."""
        with self._lock:
            self.events.clear()
            self._start_time = time.time()
            self._step_timers.clear()

    def set_label(self, label: Optional[str]) -> None:
        """Label this thread's output with the task it is running."""
        self._local.label = label

    def clear_label(self) -> None:
        """Stop labelling this thread's output."""
        self._local.label = None

    @property
    def label(self) -> Optional[str]:
        return getattr(self._local, "label", None)

    def get_events(self) -> List[WorkflowEvent]:
        """Return a copy of all recorded events."""
        return list(self.events)

    def _emit(self, event: WorkflowEvent, console_msg: Optional[str] = None) -> None:
        label = self.label
        if label:
            event.metadata = dict(event.metadata or {})
            event.metadata["task_id"] = label
        with self._lock:
            self.events.append(event)
            if self.verbose and console_msg:
                if label:
                    prefix = f"{Fore.LIGHTBLACK_EX}[{label}]{Style.RESET_ALL} "
                    console_msg = "\n".join(
                        (prefix + line if line.strip() else line)
                        for line in console_msg.splitlines()
                    )
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

    def log_agent_retry(
        self,
        agent: str,
        role: str,
        attempt: int,
        of: int,
        reason: str,
        model: Optional[str] = None,
        next_model: Optional[str] = None,
        next_agent: Optional[str] = None,
        backoff_seconds: float = 0.0,
        token_usage: Optional[TokenUsage] = None,
    ) -> None:
        """Report that an execution failed and is about to be attempted again.

        Said on the console rather than swallowed: a run that pauses for eight seconds with
        no explanation looks hung, and a retry that nobody can see is a cost nobody can audit.

        When the ladder crosses providers the *agent* changes too, and the line says so:
        "-> claude/sonnet" is a materially different event from "-> sonnet", and a reader who
        cannot tell them apart cannot tell which fallback actually rescued the run.
        """
        changed_agent = bool(next_agent and next_agent != agent)
        changed_model = bool(next_model and next_model != model)
        if changed_agent:
            switching = f" -> {next_agent}/{next_model}" if next_model else f" -> {next_agent}"
        elif changed_model:
            switching = f" -> {next_model}"
        else:
            switching = ""
        self._emit(
            WorkflowEvent(
                name="agent_retry",
                stage=agent,
                agent=agent,
                role=role,
                model=model,
                message=(
                    f"{agent} ({role}) attempt {attempt}/{of} failed: {reason}. "
                    f"Retrying in {backoff_seconds:.1f}s{switching}"
                ),
                status="retrying",
                metadata={
                    "agent": agent,
                    "role": role,
                    "attempt": attempt,
                    "of": of,
                    "reason": reason,
                    "model": model,
                    "next_model": next_model,
                    "next_agent": next_agent,
                    "backoff_seconds": backoff_seconds,
                    "token_usage": dict(token_usage) if token_usage else None,
                },
            ),
            console_msg=(
                f"  {Fore.YELLOW}[RETRY]{Style.RESET_ALL} {agent} ({role}) "
                f"attempt {attempt}/{of} failed: {reason}\n"
                f"          {_attempt_tokens_note(token_usage)}"
                f"retrying in {backoff_seconds:.1f}s{switching}"
            ),
        )

    def log_native_session_retained(self, title: str, session_id: str, port: int) -> None:
        """Report that a native TUI session was kept open for inspection."""
        self._emit(
            WorkflowEvent(
                name="native_session_retained",
                stage="opencode",
                agent="opencode",
                message=f"Native TUI session {session_id} kept open in '{title}' (port {port})",
                status="info",
                metadata={"title": title, "session_id": session_id, "port": port},
            ),
            console_msg=(
                f"  {Fore.MAGENTA}[TERMINAL]{Style.RESET_ALL} Native TUI kept open for inspection: "
                f"\"{title}\"\n"
                f"          close it with: python -m orchestrator --close-sessions"
            ),
        )

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
        retention: Optional[str] = None,
    ) -> None:
        commit_line = f"\n  {Fore.LIGHTBLACK_EX}Committed:{Style.RESET_ALL} {commit}" if commit else ""
        retention_line = (
            f"\n  {Fore.LIGHTBLACK_EX}Cleanup:{Style.RESET_ALL} {retention}" if retention else ""
        )
        self._emit(
            WorkflowEvent(
                name="workspace_result",
                stage="workflow",
                message=f"Run changed {change_count} path(s)",
                status="info",
                metadata={
                    "change_count": change_count,
                    "commit": commit,
                    "retention": retention,
                },
            ),
            console_msg=(
                f"\n{Fore.CYAN}{description}{Style.RESET_ALL}\n"
                f"  {Fore.LIGHTBLACK_EX}Changed paths:{Style.RESET_ALL} {change_count}"
                f"{commit_line}{retention_line}\n"
            ),
        )

    # ------------------------------------------------------------------
    # Delegated goals (Roadmap Phases 2-3)
    # ------------------------------------------------------------------
    def log_goal_start(
        self,
        goal: str,
        goal_id: Optional[str] = None,
        task_count: int = 0,
        waves: int = 0,
        max_parallel: int = 1,
    ) -> None:
        mode = "one at a time" if max_parallel <= 1 else f"up to {max_parallel} at a time"
        self._emit(
            WorkflowEvent(
                name="goal_started",
                stage="goal",
                message=f"Delegating {task_count} task(s) for: {goal}",
                status="started",
                metadata={
                    "goal_id": goal_id,
                    "task_count": task_count,
                    "waves": waves,
                    "max_parallel": max_parallel,
                },
            ),
            console_msg=(
                f"\n{Fore.CYAN}{Style.BRIGHT}{'=' * 50}\n"
                f"DELEGATING {task_count} TASK(S)\n"
                f"{'=' * 50}{Style.RESET_ALL}\n"
                f"  {Fore.LIGHTBLACK_EX}Goal:{Style.RESET_ALL}  {goal}\n"
                f"  {Fore.LIGHTBLACK_EX}Waves:{Style.RESET_ALL} {waves} ({mode})\n"
                + (f"  {Fore.LIGHTBLACK_EX}Goal:{Style.RESET_ALL}  {goal_id}\n" if goal_id else "")
            ),
        )

    @staticmethod
    def _short_ref(ref: str) -> str:
        """Abbreviate a commit id; leave a branch name whole, since half a name is useless."""
        text = str(ref or "")
        if len(text) >= 32 and all(c in "0123456789abcdef" for c in text.lower()):
            return text[:12]
        return text

    def log_task_start(self, task_id: str, title: str, base_ref: str = "") -> None:
        self._emit(
            WorkflowEvent(
                name="task_started",
                stage="goal",
                message=f"Task '{task_id}' started: {title}",
                status="started",
                metadata={"task_id": task_id, "base_ref": base_ref},
            ),
            console_msg=(
                f"\n{Fore.CYAN}{Style.BRIGHT}--> TASK {task_id}{Style.RESET_ALL} {title}\n"
                f"  {Fore.LIGHTBLACK_EX}Starting from:{Style.RESET_ALL} {self._short_ref(base_ref)}\n"
            ),
        )

    def log_task_finished(
        self,
        task_id: str,
        state: str,
        branch: Optional[str] = None,
        tokens: int = 0,
        detail: Optional[str] = None,
    ) -> None:
        color = Fore.GREEN if state == "done" else (
            Fore.MAGENTA if state in ("blocked", "needs_attention") else Fore.RED
        )
        extra = f"\n  {Fore.LIGHTBLACK_EX}{detail}{Style.RESET_ALL}" if detail else ""
        self._emit(
            WorkflowEvent(
                name="task_finished",
                stage="goal",
                message=f"Task '{task_id}' finished: {state}",
                status="completed" if state == "done" else "failed",
                metadata={"task_id": task_id, "state": state, "branch": branch, "tokens": tokens},
            ),
            console_msg=(
                f"{color}<-- TASK {task_id}: {state}{Style.RESET_ALL} "
                f"{Fore.LIGHTBLACK_EX}({tokens:,} tokens"
                + (f", {branch}" if branch else "")
                + f"){Style.RESET_ALL}{extra}\n"
            ),
        )

    def log_task_skipped(self, task_id: str, reason: str, detail: str = "") -> None:
        self._emit(
            WorkflowEvent(
                name="task_skipped",
                stage="goal",
                message=f"Task '{task_id}' skipped ({reason}): {detail}",
                status="failed",
                metadata={"task_id": task_id, "reason": reason},
            ),
            console_msg=(
                f"{Fore.YELLOW}--- TASK {task_id}: skipped{Style.RESET_ALL} "
                f"{Fore.LIGHTBLACK_EX}{detail}{Style.RESET_ALL}\n"
            ),
        )

    def log_collision(self, task_ids: List[str], paths: List[str]) -> None:
        """Report that sibling tasks changed the same files - never resolve it."""
        shown = ", ".join(paths[:4]) + (" ..." if len(paths) > 4 else "")
        self._emit(
            WorkflowEvent(
                name="collision",
                stage="goal",
                message=f"Tasks {' + '.join(task_ids)} both changed: {shown}",
                status="failed",
                metadata={"task_ids": list(task_ids), "paths": list(paths)},
            ),
            console_msg=(
                f"{Fore.MAGENTA}{Style.BRIGHT}[COLLISION]{Style.RESET_ALL} "
                f"{' + '.join(task_ids)} both changed {shown}\n"
                f"  {Fore.LIGHTBLACK_EX}Nothing was merged; review the branches "
                f"together.{Style.RESET_ALL}\n"
            ),
        )

    def log_dependency_merge(
        self,
        merged: Optional[List[str]] = None,
        conflicted: Optional[List[str]] = None,
        error: Optional[str] = None,
    ) -> None:
        """Report bringing a task's dependencies into its workspace."""
        if conflicted:
            console = (
                f"  {Fore.MAGENTA}[DEPENDENCIES] Conflict merging "
                f"{', '.join(conflicted)}{Style.RESET_ALL}\n"
                f"  {Fore.LIGHTBLACK_EX}Nothing was guessed at; this task needs a "
                f"person.{Style.RESET_ALL}\n"
            )
        elif merged:
            console = (
                f"  {Fore.LIGHTBLACK_EX}Dependencies merged in:{Style.RESET_ALL} "
                f"{', '.join(merged)}\n"
            )
        else:
            console = ""
        self._emit(
            WorkflowEvent(
                name="dependency_merge",
                stage="context",
                message=f"Merged {len(merged or [])} dependency branch(es)",
                status="failed" if conflicted else "info",
                metadata={
                    "merged": list(merged or []),
                    "conflicted": list(conflicted or []),
                    "error": error,
                },
            ),
            console_msg=console,
        )

    def log_approval_gate(
        self,
        gate: str,
        subject: str,
        state: str,
        approval_id: str = "",
    ) -> None:
        """Report that an approval gate was reached, and what it said.

        A pending gate is the loudest thing this tracer prints, because it is the one state
        that will not resolve itself.
        """
        if state == "approved":
            console = (
                f"  {Fore.GREEN}[GATE OK]{Style.RESET_ALL} {gate} "
                f"{Fore.LIGHTBLACK_EX}({subject}){Style.RESET_ALL}\n"
            )
        elif state == "rejected":
            console = f"  {Fore.RED}[GATE REJECTED]{Style.RESET_ALL} {gate} - {subject}\n"
        else:
            console = (
                f"\n{Fore.MAGENTA}{Style.BRIGHT}[NEEDS YOU]{Style.RESET_ALL} "
                f"{gate}: {subject}\n"
                f"  {Fore.LIGHTBLACK_EX}Approve with:{Style.RESET_ALL} "
                f"python -m orchestrator --approve {approval_id}\n"
            )
        self._emit(
            WorkflowEvent(
                name="approval_gate",
                stage="goal",
                message=f"Approval gate '{gate}' is {state}: {subject}",
                status="failed" if state == "pending" else "info",
                metadata={
                    "gate": gate,
                    "state": state,
                    "approval_id": approval_id,
                    "subject": subject,
                },
            ),
            console_msg=console,
        )

    def log_delivery(
        self,
        subject: str,
        state: str,
        url: str = "",
        detail: str = "",
    ) -> None:
        """Report that work crossed the network, or that it was refused (Roadmap Phase 6).

        A push is the loudest thing this project does to the outside world, so it is said
        plainly and only ever after a person asked for it.
        """
        if state in ("refused", "failed"):
            console = (
                f"  {Fore.YELLOW}[NOT DELIVERED]{Style.RESET_ALL} {subject}\n"
                f"    {Fore.LIGHTBLACK_EX}{detail}{Style.RESET_ALL}\n"
            )
        else:
            console = (
                f"  {Fore.GREEN}[DELIVERED]{Style.RESET_ALL} {subject} "
                f"{Fore.LIGHTBLACK_EX}({state}){Style.RESET_ALL}\n"
                + (f"    {url}\n" if url else "")
            )
        self._emit(
            WorkflowEvent(
                name="delivery",
                stage="goal",
                message=f"Delivery of {subject} is {state}",
                status="failed" if state in ("refused", "failed") else "info",
                metadata={"subject": subject, "state": state, "url": url, "detail": detail},
            ),
            console_msg=console,
        )

    def log_goal_resumed(self, goal_id: str, replayed: Optional[List[str]] = None) -> None:
        """Report which tasks a resumed goal is replaying rather than re-running."""
        names = ", ".join(replayed or []) or "nothing"
        self._emit(
            WorkflowEvent(
                name="goal_resumed",
                stage="goal",
                message=f"Resumed goal {goal_id}; replaying {names}",
                status="info",
                metadata={"goal_id": goal_id, "replayed": list(replayed or [])},
            ),
            console_msg=(
                f"{Fore.CYAN}{Style.BRIGHT}[RESUME]{Style.RESET_ALL} Continuing goal "
                f"{goal_id}\n"
                f"  {Fore.LIGHTBLACK_EX}Already delivered (not re-run):{Style.RESET_ALL} "
                f"{names}\n"
            ),
        )

    def log_goal_budget_exhausted(self, reason: str) -> None:
        self._emit(
            WorkflowEvent(
                name="goal_budget_exhausted",
                stage="goal",
                message=f"Goal budget exhausted: {reason}",
                status="failed",
                metadata={"reason": reason},
            ),
            console_msg=(
                f"\n{Fore.YELLOW}{Style.BRIGHT}[BUDGET] No further tasks will be started: "
                f"{reason}{Style.RESET_ALL}\n"
            ),
        )

    def log_goal_complete(
        self,
        status: str,
        done: int = 0,
        task_count: int = 0,
        collisions: int = 0,
    ) -> None:
        color = Fore.GREEN if status == "completed" else (
            Fore.MAGENTA if status == "blocked" else Fore.YELLOW
        )
        self._emit(
            WorkflowEvent(
                name="goal_finished",
                stage="goal",
                message=f"Goal finished: {status} ({done}/{task_count} delivered)",
                status="completed" if status == "completed" else "failed",
                metadata={"status": status, "done": done, "task_count": task_count},
            ),
            console_msg=(
                f"\n{color}{Style.BRIGHT}{'=' * 50}\n"
                f"GOAL {status.upper()}: {done}/{task_count} task(s) delivered"
                + (f", {collisions} collision(s)" if collisions else "")
                + f"\n{'=' * 50}{Style.RESET_ALL}\n"
            ),
        )

    # ------------------------------------------------------------------
    # Objective acceptance gate (Roadmap Phase 0)
    # ------------------------------------------------------------------
    def log_acceptance_start(self, command: Optional[List[str]] = None) -> None:
        rendered = " ".join(command or []) or "(none)"
        self._emit(
            WorkflowEvent(
                name="acceptance_started",
                stage="acceptance",
                message=f"Running the acceptance command: {rendered}",
                status="started",
                metadata={"command": list(command or [])},
            ),
            console_msg=(
                f"\n{Fore.CYAN}[GATE]{Style.RESET_ALL} Running the project's own check\n"
                f"  {Fore.LIGHTBLACK_EX}Command:{Style.RESET_ALL} {rendered}"
            ),
        )

    def log_acceptance_result(
        self,
        ok: bool,
        exit_code: Optional[int] = None,
        duration_seconds: float = 0.0,
        error: Optional[str] = None,
        required: bool = True,
    ) -> None:
        """Report what the orchestrator's own check found.

        Phrased as evidence rather than as a verdict: the gate reports, and
        `status.derive_verdict` decides what its result means.
        """
        if error:
            headline = f"{Fore.RED}[GATE] COULD NOT RUN{Style.RESET_ALL} {error}"
            status = "failed"
        elif ok:
            headline = (
                f"{Fore.GREEN}[GATE] PASSED{Style.RESET_ALL} "
                f"{Fore.LIGHTBLACK_EX}(exit 0, {duration_seconds}s){Style.RESET_ALL}"
            )
            status = "completed"
        else:
            consequence = (
                " - a verifier PASS cannot override this"
                if required
                else " - advisory only (acceptance.required is false)"
            )
            headline = (
                f"{Fore.RED}[GATE] FAILED{Style.RESET_ALL} "
                f"{Fore.LIGHTBLACK_EX}(exit {exit_code}, {duration_seconds}s){consequence}"
                f"{Style.RESET_ALL}"
            )
            status = "failed"

        self._emit(
            WorkflowEvent(
                name="acceptance_result",
                stage="acceptance",
                message=f"Acceptance gate {'passed' if ok else 'failed'}",
                status=status,
                elapsed_seconds=duration_seconds,
                metadata={
                    "ok": ok,
                    "exit_code": exit_code,
                    "error": error,
                    "required": required,
                },
            ),
            console_msg=f"  {headline}\n",
        )

    def log_acceptance_override(self, consensus: str, verdict: str) -> None:
        """Report that the objective check overruled what the verifiers concluded."""
        self._emit(
            WorkflowEvent(
                name="acceptance_override",
                stage="verifier",
                role="verifier",
                message=f"Acceptance gate changed the verdict from {consensus} to {verdict}",
                status="failed",
                metadata={"consensus": consensus, "verdict": verdict},
            ),
            console_msg=(
                f"  {Fore.RED}[GATE] Verdict changed {consensus} -> {verdict}:{Style.RESET_ALL} "
                f"the acceptance command disagrees with the verifier, and it was run by the "
                f"orchestrator.\n"
            ),
        )

    # ------------------------------------------------------------------
    # Goal decomposition (Roadmap Phase 1)
    # ------------------------------------------------------------------
    def log_task_plan(
        self,
        task_count: int,
        waves: int = 0,
        error: Optional[str] = None,
        warnings: Optional[List[str]] = None,
    ) -> None:
        """Report the shape of a decomposition, without reprinting the plan itself."""
        if error:
            console = (
                f"  {Fore.RED}[PLAN] Could not read a task plan:{Style.RESET_ALL} {error}\n"
            )
        else:
            parallel = f", {waves} wave(s)" if waves else ""
            console = (
                f"  {Fore.GREEN}[PLAN]{Style.RESET_ALL} {task_count} task(s){parallel}\n"
            )
        for warning in warnings or []:
            console += f"  {Fore.YELLOW}note:{Style.RESET_ALL} {warning}\n"

        self._emit(
            WorkflowEvent(
                name="task_plan",
                stage="decomposer",
                role="decomposer",
                message=f"Decomposed the goal into {task_count} task(s)",
                status="failed" if error else "completed",
                metadata={
                    "task_count": task_count,
                    "waves": waves,
                    "error": error,
                    "warnings": list(warnings or []),
                },
            ),
            console_msg=console,
        )

    # ------------------------------------------------------------------
    # Run budget (Tier 2 #11)
    # ------------------------------------------------------------------
    def log_budget_exhausted(
        self,
        reason: str,
        tokens_spent: int = 0,
        max_total_tokens: int = 0,
        seconds_elapsed: float = 0.0,
        max_duration_seconds: int = 0,
        repair_attempts: int = 0,
        max_repair_attempts: int = 0,
    ) -> None:
        """Report that the run stopped escalating because it hit its ceiling.

        This is not a failure of the agents; it is the safety valve doing its
        job, so it says plainly what was spent and what the remaining repair
        budget would have been.
        """
        remaining = max(0, max_repair_attempts - repair_attempts)
        token_line = (
            f"{tokens_spent:,} / {max_total_tokens:,}" if max_total_tokens else f"{tokens_spent:,} (no limit)"
        )
        time_line = (
            f"{seconds_elapsed:.0f}s / {max_duration_seconds}s"
            if max_duration_seconds
            else f"{seconds_elapsed:.0f}s (no limit)"
        )
        self._emit(
            WorkflowEvent(
                name="budget_exhausted",
                stage="verifier",
                role="verifier",
                message=f"Run budget exhausted: {reason}",
                status="failed",
                metadata={
                    "reason": reason,
                    "tokens_spent": tokens_spent,
                    "max_total_tokens": max_total_tokens,
                    "seconds_elapsed": seconds_elapsed,
                    "max_duration_seconds": max_duration_seconds,
                    "repair_attempts_remaining": remaining,
                },
            ),
            console_msg=(
                f"\n{Fore.YELLOW}{Style.BRIGHT}[BUDGET] Stopping: {reason}{Style.RESET_ALL}\n"
                f"  {Fore.LIGHTBLACK_EX}Tokens:{Style.RESET_ALL} {token_line}\n"
                f"  {Fore.LIGHTBLACK_EX}Time:{Style.RESET_ALL}   {time_line}\n"
                f"  {Fore.LIGHTBLACK_EX}Unused repair attempts:{Style.RESET_ALL} {remaining}\n"
            ),
        )

    # ------------------------------------------------------------------
    # Resuming a recorded run (Tier 1 #4)
    # ------------------------------------------------------------------
    def log_run_resumed(
        self,
        run_id: str,
        completed_roles: Optional[List[str]] = None,
        repair_attempts: int = 0,
    ) -> None:
        """Report which phases a resumed run is replaying rather than re-running."""
        replayed = ", ".join(completed_roles or []) or "nothing"
        self._emit(
            WorkflowEvent(
                name="run_resumed",
                stage="context",
                message=f"Resumed run {run_id}; replaying {replayed}",
                status="info",
                metadata={
                    "run_id": run_id,
                    "completed_roles": list(completed_roles or []),
                    "repair_attempts": repair_attempts,
                },
            ),
            console_msg=(
                f"{Fore.CYAN}{Style.BRIGHT}[RESUME]{Style.RESET_ALL} Continuing run "
                f"{run_id}\n"
                f"  {Fore.LIGHTBLACK_EX}Replaying (not re-running):{Style.RESET_ALL} {replayed}\n"
                f"  {Fore.LIGHTBLACK_EX}Repairs already spent:{Style.RESET_ALL} {repair_attempts}\n"
            ),
        )

    def log_phase_replayed(self, role: str, agent: Optional[str] = None) -> None:
        """Report that a phase was satisfied from the resumed run's history."""
        self._emit(
            WorkflowEvent(
                name="phase_replayed",
                stage=role,
                role=role,
                agent=agent,
                message=f"Phase '{role}' replayed from the resumed run",
                status="info",
                metadata={"role": role, "replayed": True},
            ),
            console_msg=(
                f"  {Fore.LIGHTBLACK_EX}[replay]{Style.RESET_ALL} {role} - "
                f"reusing the recorded result, no agent launched\n"
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
