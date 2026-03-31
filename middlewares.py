"""
Agent middlewares — hooks that run at specific points in the agent loop.

Middlewares are the harness engineer's primary tool for shaping agent behavior
without changing the core loop. They intercept execution at defined points:

  - post_tool:    After a tool call completes. Use for loop detection, tracking.
  - pre_exit:     When the agent wants to stop (no more tool calls). Use for
                  forced verification passes.
  - per_iteration: At the start of each iteration. Use for time budget warnings.

Middlewares return an optional message to inject into the conversation.
Returning None means "no intervention."

Design principle: middlewares are composable and profile-specific.
The base Agent loop knows nothing about terminal tasks or time budgets —
profiles wire in the middlewares they need.
"""
from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

log = logging.getLogger("harness")


class AgentMiddleware(ABC):
    """Base class for agent middlewares."""

    def post_tool(self, tool_name: str, tool_args: dict, result: str,
                  messages: list[dict]) -> str | None:
        """Called after each tool execution. Return a message to inject, or None."""
        return None

    def pre_exit(self, messages: list[dict]) -> str | None:
        """Called when the agent wants to stop. Return a message to force continuation, or None."""
        return None

    def per_iteration(self, iteration: int, messages: list[dict]) -> str | None:
        """Called at the start of each iteration. Return a message to inject, or None."""
        return None


# ---------------------------------------------------------------------------
# Loop Detection
# ---------------------------------------------------------------------------

class LoopDetectionMiddleware(AgentMiddleware):
    """
    Tracks per-file edit counts and detects repetitive command patterns.
    When the agent edits the same file or runs similar commands too many times,
    injects a nudge to reconsider the approach.

    Uses fuzzy matching for commands — catches variants like:
      python3 app.py  /  python3 app.py 2>&1  /  python3 ./app.py
    """

    def __init__(self, file_edit_threshold: int = 4, command_repeat_threshold: int = 3):
        self.file_edit_threshold = file_edit_threshold
        self.command_repeat_threshold = command_repeat_threshold
        self.file_edit_counts: dict[str, int] = {}
        self.recent_commands: list[str] = []
        self._file_warned: set[str] = set()  # avoid spamming same warning

    @staticmethod
    def _normalize_command(cmd: str) -> str:
        """Normalize a command for fuzzy comparison."""
        import re
        cmd = cmd.strip()
        # Remove common suffixes that don't change semantics
        cmd = re.sub(r'\s*2>&1\s*$', '', cmd)
        cmd = re.sub(r'\s*\|\s*head.*$', '', cmd)
        cmd = re.sub(r'\s*\|\s*tail.*$', '', cmd)
        # Normalize paths: ./foo → foo
        cmd = re.sub(r'\./(\S)', r'\1', cmd)
        # Collapse whitespace
        cmd = re.sub(r'\s+', ' ', cmd)
        return cmd.strip()

    def post_tool(self, tool_name: str, tool_args: dict, result: str,
                  messages: list[dict]) -> str | None:
        # Track file edits
        if tool_name == "write_file":
            path = tool_args.get("path", "")
            self.file_edit_counts[path] = self.file_edit_counts.get(path, 0) + 1
            count = self.file_edit_counts[path]
            if count >= self.file_edit_threshold and path not in self._file_warned:
                self._file_warned.add(path)
                log.warning(f"Loop detection: {path} edited {count} times")
                return (
                    f"[SYSTEM] You have edited '{path}' {count} times. "
                    "This pattern suggests your current approach may not be working. "
                    "STOP and reconsider:\n"
                    "1. Re-read the original task requirements.\n"
                    "2. Think about what's fundamentally wrong with your approach.\n"
                    "3. Try a completely different strategy."
                )

        # Track repeated commands (with fuzzy matching)
        if tool_name == "run_bash":
            cmd = tool_args.get("command", "").strip()
            self.recent_commands.append(cmd)
            if len(self.recent_commands) >= self.command_repeat_threshold:
                window = self.recent_commands[-self.command_repeat_threshold:]
                normalized = [self._normalize_command(c) for c in window]
                if len(set(normalized)) == 1:
                    log.warning(f"Loop detection: similar command repeated {self.command_repeat_threshold}x")
                    return (
                        f"[SYSTEM] You have run essentially the same command {self.command_repeat_threshold} "
                        f"times in a row with no progress.\n"
                        f"Command pattern: {normalized[0][:200]}\n"
                        "This is a doom loop. The same action will not produce a different result.\n"
                        "STOP. Re-read the error output carefully. Try a fundamentally different approach."
                    )

            # Also detect rapid-fire failed commands (different commands, same error)
            if "[error]" in result or "command not found" in result.lower():
                recent_errors = 0
                for msg in reversed(messages[-8:]):
                    content = msg.get("content", "")
                    if msg.get("role") == "tool" and (
                        "[error]" in content or "command not found" in content.lower()
                    ):
                        recent_errors += 1
                if recent_errors >= 3:
                    return (
                        "[SYSTEM] Multiple consecutive commands have failed. "
                        "Stop and diagnose the root cause before trying more commands. "
                        "Check: Is the required tool installed? Are you in the right directory? "
                        "Is there a dependency missing?"
                    )

        return None


# ---------------------------------------------------------------------------
# Pre-Exit Verification
# ---------------------------------------------------------------------------

class PreExitVerificationMiddleware(AgentMiddleware):
    """
    Forces the agent to run a verification pass before it's allowed to stop.

    On the first exit attempt, injects a verification prompt.
    On the second exit attempt, allows the agent to stop.

    This is the harness-level enforcement of self-verification —
    prompt-level instructions alone are not reliable enough.
    """

    def __init__(self, verification_prompt: str | None = None):
        self._exit_attempts = 0
        self._verification_prompt = verification_prompt or (
            "[SYSTEM] MANDATORY VERIFICATION — You are about to finish, but you MUST verify first.\n"
            "Do NOT just re-read your code. Run actual test/check commands:\n"
            "1. Re-read the original task specification.\n"
            "2. For each requirement, run a concrete verification command.\n"
            "3. Compare actual output against expected output.\n"
            "4. If anything fails, fix it before stopping.\n"
            "Only stop after ALL checks pass."
        )

    def pre_exit(self, messages: list[dict]) -> str | None:
        self._exit_attempts += 1
        if self._exit_attempts == 1:
            log.info("Pre-exit verification: forcing verification pass")
            return self._verification_prompt
        # Second exit — allow it
        log.info("Pre-exit verification: agent verified, allowing exit")
        return None


# ---------------------------------------------------------------------------
# Time Budget
# ---------------------------------------------------------------------------

class TimeBudgetMiddleware(AgentMiddleware):
    """
    Injects time awareness into the agent loop.

    At configurable thresholds (default: 60% and 85% of budget),
    warns the agent about remaining time and nudges it toward
    wrapping up and verifying.
    """

    def __init__(self, budget_seconds: float,
                 warn_threshold: float = 0.60,
                 critical_threshold: float = 0.85):
        self.budget_seconds = budget_seconds
        self.warn_threshold = warn_threshold
        self.critical_threshold = critical_threshold
        self.start_time = time.time()
        self._warned = False
        self._critical = False

    def per_iteration(self, iteration: int, messages: list[dict]) -> str | None:
        elapsed = time.time() - self.start_time
        fraction = elapsed / self.budget_seconds
        remaining = self.budget_seconds - elapsed

        if fraction >= self.critical_threshold and not self._critical:
            self._critical = True
            mins_left = remaining / 60
            log.warning(f"Time budget critical: {mins_left:.1f} min remaining")
            return (
                f"[SYSTEM] ⚠️ CRITICAL: Only {mins_left:.1f} minutes remaining out of "
                f"{self.budget_seconds / 60:.0f} min budget.\n"
                "STOP building new features. Immediately:\n"
                "1. Verify what you've done so far works correctly.\n"
                "2. Run final checks against the task requirements.\n"
                "3. Fix any broken items — do NOT start anything new."
            )

        if fraction >= self.warn_threshold and not self._warned:
            self._warned = True
            mins_left = remaining / 60
            log.info(f"Time budget warning: {mins_left:.1f} min remaining")
            return (
                f"[SYSTEM] Time check: {mins_left:.1f} minutes remaining out of "
                f"{self.budget_seconds / 60:.0f} min budget. "
                "Start wrapping up your current work and plan for verification."
            )

        return None


# ---------------------------------------------------------------------------
# Task Tracking (forced decomposition)
# ---------------------------------------------------------------------------

class TaskTrackingMiddleware(AgentMiddleware):
    """
    Encourages the agent to maintain explicit task tracking for multi-step work.

    After the agent has made several tool calls without writing any tracking
    artifact, injects a reminder to decompose and track progress.

    Inspired by ForgeCode's todo_write enforcement, which was their single
    biggest improvement (38% → 66% on TB2).

    This is a softer version — it nudges rather than hard-blocks, since
    not all tasks need decomposition. But for complex multi-step tasks,
    the nudge is enough to trigger the behavior.
    """

    def __init__(self, nudge_after_n_tools: int = 8):
        self.nudge_after_n_tools = nudge_after_n_tools
        self.tool_call_count = 0
        self._nudged = False

    def post_tool(self, tool_name: str, tool_args: dict, result: str,
                  messages: list[dict]) -> str | None:
        self.tool_call_count += 1

        if self._nudged or self.tool_call_count < self.nudge_after_n_tools:
            return None

        # Check if agent has already written any tracking/progress notes
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str) and "progress" in content.lower():
                # Agent seems to be tracking already
                return None
            # Check if agent wrote to a tracking file
            if msg.get("role") == "assistant":
                for tc in msg.get("tool_calls", []):
                    fn = tc.get("function", {})
                    if fn.get("name") == "write_file":
                        args_str = fn.get("arguments", "")
                        if any(kw in args_str.lower() for kw in ["todo", "progress", "checklist", "tracker"]):
                            return None

        self._nudged = True
        log.info("Task tracking: nudging agent to track progress")
        return (
            "[SYSTEM] You have made several tool calls. For complex tasks, "
            "tracking your progress helps avoid skipping steps or repeating work.\n"
            "Consider: What steps remain? What have you completed? What still needs verification?\n"
            "Keep a mental checklist and verify each requirement before finishing."
        )


# ---------------------------------------------------------------------------
# Error Guidance (structured recovery for weak models)
# ---------------------------------------------------------------------------

class ErrorGuidanceMiddleware(AgentMiddleware):
    """
    Detects common error patterns in tool output and injects specific,
    actionable recovery suggestions.

    Weak models struggle to recover from errors on their own — they often
    retry the same failing command or give up. This middleware matches
    error patterns and provides concrete next steps.

    Based on TB2 command-level error analysis:
      - 24.1% of failures: command not found / not on PATH
      -  9.6% of failures: runtime errors in executables
      -  High rate: permission denied, missing dependencies
    """

    # Pattern → (description, recovery suggestion)
    # Patterns are checked in order; first match wins.
    ERROR_PATTERNS: list[tuple[str, str, str]] = [
        # --- Command not found ---
        (
            "command not found",
            "command_not_found",
            "The command is not installed. Try:\n"
            "  apt-get update && apt-get install -y <package>  (for system tools)\n"
            "  pip install <package>  (for Python tools)\n"
            "  which <command> || apt-cache search <keyword>  (to find the right package)\n"
            "If apt-get fails with permission denied, prefix with sudo.",
        ),
        (
            "no such file or directory",
            "file_not_found",
            "A file or directory doesn't exist. Check:\n"
            "  ls -la <parent_directory>  (does the path exist?)\n"
            "  pwd  (are you in the right directory?)\n"
            "  find . -name '<filename>'  (search for the file)",
        ),
        # --- Permission errors ---
        (
            "permission denied",
            "permission_denied",
            "Permission denied. Try:\n"
            "  chmod +x <file>  (if it needs to be executable)\n"
            "  sudo <command>  (if it needs root)\n"
            "  ls -la <file>  (check current permissions)",
        ),
        # --- Python/pip errors ---
        (
            "externally-managed-environment",
            "pip_managed_env",
            "This Python environment is externally managed (PEP 668). Use:\n"
            "  pip install --break-system-packages <package>\n"
            "  or: pip install --user <package>\n"
            "  or: python3 -m venv /tmp/venv && source /tmp/venv/bin/activate",
        ),
        (
            "modulenotfounderror",
            "python_import",
            "A Python module is missing. Install it:\n"
            "  pip install <module_name>\n"
            "  pip install --break-system-packages <module_name>  (if managed env)\n"
            "Check the exact package name — it may differ from the import name.",
        ),
        (
            "no module named",
            "python_import",
            "A Python module is missing. Install it:\n"
            "  pip install <module_name>\n"
            "Check: the pip package name may differ from the import name "
            "(e.g. 'import cv2' → 'pip install opencv-python').",
        ),
        # --- Compilation errors ---
        (
            "fatal error:",
            "compilation",
            "Compilation failed. Check:\n"
            "  1. Read the error — it shows the file and line number.\n"
            "  2. Missing header? Install dev packages: apt-get install -y lib<name>-dev\n"
            "  3. Use: apt-cache search <header_name> to find the right package.",
        ),
        (
            "undefined reference to",
            "linker",
            "Linker error — a symbol is missing. Check:\n"
            "  1. Are you linking all required libraries? (-l<lib> flag)\n"
            "  2. Is the library installed? apt-get install -y lib<name>-dev\n"
            "  3. Check library search path: ldconfig -p | grep <lib>",
        ),
        # --- Git errors ---
        (
            "not a git repository",
            "git",
            "Not in a git repository. Try:\n"
            "  git init  (to create one)\n"
            "  cd <correct_directory>  (you may be in the wrong dir)\n"
            "  find / -name '.git' -type d 2>/dev/null  (find existing repos)",
        ),
        # --- Disk/resource errors ---
        (
            "no space left on device",
            "disk_full",
            "Disk is full. Free space:\n"
            "  df -h  (check disk usage)\n"
            "  du -sh /* 2>/dev/null | sort -rh | head  (find large dirs)\n"
            "  apt-get clean  (clear package cache)\n"
            "  rm -rf /tmp/*  (clear temp files)",
        ),
        (
            "killed",
            "oom",
            "Process was killed (likely out of memory). Try:\n"
            "  free -h  (check available memory)\n"
            "  Reduce memory usage: smaller batch size, fewer workers, etc.\n"
            "  Use swap: fallocate -l 2G /swapfile && mkswap /swapfile && swapon /swapfile",
        ),
    ]

    def __init__(self):
        self._last_guidance_type: str | None = None

    def post_tool(self, tool_name: str, tool_args: dict, result: str,
                  messages: list[dict]) -> str | None:
        if tool_name != "run_bash":
            return None

        result_lower = result.lower()

        # Skip if no error indicators
        if "[error]" not in result_lower and "error" not in result_lower and "not found" not in result_lower:
            self._last_guidance_type = None
            return None

        for pattern, guidance_type, suggestion in self.ERROR_PATTERNS:
            if pattern in result_lower:
                # Don't repeat the same guidance type consecutively
                if guidance_type == self._last_guidance_type:
                    return None
                self._last_guidance_type = guidance_type
                log.info(f"Error guidance: matched '{guidance_type}'")
                return f"[SYSTEM] Error detected — here's how to fix it:\n{suggestion}"

        self._last_guidance_type = None
        return None
