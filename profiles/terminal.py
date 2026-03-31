"""
Terminal task profile — optimized for Terminal-Bench-2.

Key constraints:
  - 30 min (1800s) hard timeout per task
  - Tasks are well-defined CLI problems, not open-ended
  - No UI, no browser testing needed
  - Correctness is binary: tests pass or fail

All tunable parameters are read via self.cfg.resolve(), so you can override
them without touching this file:

  # Via environment variables:
  PROFILE_TERMINAL_TASK_BUDGET=1800
  PROFILE_TERMINAL_PLANNER_BUDGET=120
  PROFILE_TERMINAL_PASS_THRESHOLD=8.0
  PROFILE_TERMINAL_LOOP_FILE_EDIT_THRESHOLD=4
  PROFILE_TERMINAL_TIME_WARN_THRESHOLD=0.65

  # Or via ProfileConfig in code:
  from profiles.base import ProfileConfig
  cfg = ProfileConfig(task_budget=1200, pass_threshold=9.0)
  profile = TerminalProfile(cfg=cfg)
"""
from __future__ import annotations

from profiles.base import BaseProfile, AgentConfig, ProfileConfig
from middlewares import (
    LoopDetectionMiddleware,
    PreExitVerificationMiddleware,
    TimeBudgetMiddleware,
    TaskTrackingMiddleware,
    ErrorGuidanceMiddleware,
)

# Commands to bootstrap environment awareness at the start of each build.
# Output is injected as context so the model doesn't waste time exploring.
ENV_BOOTSTRAP_COMMANDS = [
    "uname -a",
    "pwd",
    "ls -la /app/ 2>/dev/null || echo '/app not found'",
    "ls -la . 2>/dev/null",
    "python3 --version 2>/dev/null; python --version 2>/dev/null",
    "which gcc g++ make cmake 2>/dev/null || true",
    "pip3 list 2>/dev/null | head -30 || true",
    "cat /etc/os-release 2>/dev/null | head -5 || true",
    "df -h / 2>/dev/null | tail -1 || true",
    "free -h 2>/dev/null | head -2 || true",
    "env | grep -iE '^(PATH|HOME|USER|LANG|LC_)' 2>/dev/null || true",
]


class TerminalProfile(BaseProfile):

    # --- Default values (overridable via ProfileConfig or env vars) ---
    _DEFAULTS = {
        "task_budget": 1800,
        "planner_budget": 120,
        "evaluator_budget": 180,
        "pass_threshold": 8.0,
        "max_rounds": 2,
        "loop_file_edit_threshold": 4,
        "loop_command_repeat_threshold": 3,
        "task_tracking_nudge_after": 8,
        "time_warn_threshold": 0.65,
        "time_critical_threshold": 0.85,
    }

    def _get(self, key: str):
        """Resolve a config value: env var > ProfileConfig > default."""
        return self.cfg.resolve(key, self.name(), self._DEFAULTS[key])

    @property
    def _builder_budget(self) -> float:
        return self._get("task_budget") - self._get("planner_budget") - self._get("evaluator_budget")

    def name(self) -> str:
        return "terminal"

    def description(self) -> str:
        return "Solve terminal/CLI tasks (Terminal-Bench-2 style)"

    def planner(self) -> AgentConfig:
        return AgentConfig(
            system_prompt="""\
You are a quick task planner for a terminal/CLI task.

Workflow:
1. DISCOVER: Use list_files and run_bash to understand the environment:
   - What files exist in the workspace?
   - Are there existing tests, scripts, or Makefiles?
   - What does the task actually require?
2. PLAN: Based on what you found, write a brief step-by-step plan.

Plan rules:
- Keep it SHORT — 5-10 steps max.
- Be specific: list exact commands, file paths, tools needed.
- Note how to VERIFY each step (what command proves it worked).
- Note any existing test scripts or verification tools you found.

Use write_file to save the plan to spec.md, then stop.
""",
            time_budget=self._get("planner_budget"),
        )

    def builder(self) -> AgentConfig:
        builder_budget = self._builder_budget
        return AgentConfig(
            system_prompt="""\
You are an expert Linux system administrator and developer. \
Complete the given task by executing shell commands.

CRITICAL RULES:
- Your PRIMARY action is run_bash. Execute commands, don't just describe them.
- If you finish without running any commands, you have FAILED.
- Work FAST. You have limited time. Don't overthink — execute.
- Read spec.md first for the plan, then execute step by step.
- If feedback.md exists, read it and fix the issues.
- Do NOT write long explanations. Just execute and verify.

TESTABILITY — your work will be verified by automated test scripts:
- Follow task specifications LITERALLY — exact file names, exact output \
formats, exact paths. Do not improvise or rename things.
- If the task says "write output to result.txt", it means exactly result.txt, \
not results.txt or output.txt.
- If the task specifies a particular format, match it character-for-character.
- Think: "If a test script checks for this, would it pass?"

PROBLEM-SOLVING STRATEGY:
1. Plan & Discover: Read spec.md, scan the codebase, understand the task.
2. Build: Implement step by step.
3. Verify: Run tests, read FULL output, compare against task spec (not your code).
4. Fix: If anything fails, re-read the original spec and fix.

WHEN THINGS GO WRONG:
- If a command is not found: install it (apt-get install, pip install, etc.) \
before retrying. Check which package provides it.
- If a command times out: retry with a larger timeout parameter.
- If your approach isn't working after 3-4 attempts: STOP and try a \
fundamentally different strategy. Do not keep tweaking the same broken approach.
- Read error messages carefully — they usually tell you exactly what's wrong.

Tools: read_file, write_file, list_files, run_bash, delegate_task.
""",
            middlewares=[
                LoopDetectionMiddleware(
                    file_edit_threshold=self._get("loop_file_edit_threshold"),
                    command_repeat_threshold=self._get("loop_command_repeat_threshold"),
                ),
                ErrorGuidanceMiddleware(),
                TaskTrackingMiddleware(
                    nudge_after_n_tools=self._get("task_tracking_nudge_after"),
                ),
                PreExitVerificationMiddleware(
                    verification_prompt=(
                        "[SYSTEM] MANDATORY VERIFICATION — You are about to finish, "
                        "but you MUST verify your work first.\n"
                        "Switch to REVIEWER mode. Forget what you think you did — check what actually exists:\n"
                        "1. Re-read the original task requirements (the user prompt, not just spec.md).\n"
                        "2. For EACH requirement, run a concrete check command "
                        "(ls -la, cat, test -f, diff, grep, python3 -c, etc.)\n"
                        "3. Compare ACTUAL output against what the task asked for.\n"
                        "4. Check exact file paths, exact output formats, exact behavior.\n"
                        "5. If ANY check fails, fix it before stopping.\n"
                        "Think like an automated test script — would your solution pass?"
                    ),
                ),
                TimeBudgetMiddleware(
                    budget_seconds=builder_budget,
                    warn_threshold=self._get("time_warn_threshold"),
                    critical_threshold=self._get("time_critical_threshold"),
                ),
            ],
            time_budget=builder_budget,
        )

    def evaluator(self) -> AgentConfig:
        return AgentConfig(
            system_prompt="""\
You are a quick verifier. Check if the task was done correctly.

Rules:
- Read spec.md for what should have been done.
- Run 2-3 verification commands with run_bash (ls, cat, test, diff, etc.)
- Check EXACT file paths, output formats, and behavior against the task spec.
- Score Correctness 0-10. Be honest but fast.
- Write a SHORT evaluation to feedback.md. No essays.

Format for feedback.md:
```
## Verification
- Correctness: X/10 — [one sentence]
- **Average: X/10**
### Issues: [list if any, with exact details of what's wrong]
```

Use write_file to save to feedback.md, then stop.
""",
            time_budget=self._get("evaluator_budget"),
        )

    # No contract negotiation — TB2 tasks are already well-specified
    def contract_proposer(self) -> AgentConfig:
        return AgentConfig(system_prompt="", enabled=False)

    def contract_reviewer(self) -> AgentConfig:
        return AgentConfig(system_prompt="", enabled=False)

    def pass_threshold(self) -> float:
        return self._get("pass_threshold")

    def max_rounds(self) -> int:
        return self._get("max_rounds")

    def format_build_task(self, user_prompt: str, round_num: int,
                          prev_feedback: str, score_history: list[float]) -> str:
        """Streamlined task prompt with environment bootstrapping."""
        env_section = ""
        if round_num == 1:
            import subprocess, config as _cfg
            env_lines = []
            for cmd in ENV_BOOTSTRAP_COMMANDS:
                try:
                    r = subprocess.run(
                        cmd, shell=True, cwd=_cfg.WORKSPACE,
                        capture_output=True, text=True, timeout=10,
                    )
                    out = (r.stdout + r.stderr).strip()
                    if out:
                        env_lines.append(f"$ {cmd}\n{out}")
                except Exception:
                    pass
            if env_lines:
                env_section = (
                    "\n\n--- ENVIRONMENT INFO (pre-collected, do NOT re-run these) ---\n"
                    + "\n\n".join(env_lines)
                    + "\n--- END ENVIRONMENT INFO ---\n"
                )

        task = (
            f"Complete this task:\n\n{user_prompt}\n\n"
            f"Read spec.md for the plan. Execute commands with run_bash. "
            f"Verify your work when done."
            f"{env_section}"
        )
        if prev_feedback:
            task += (
                f"\n\nYour previous attempt had issues. "
                f"Read feedback.md and fix them. Be precise."
            )
        return task
