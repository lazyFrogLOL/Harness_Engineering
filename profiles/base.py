"""
Base profile — defines the interface that all task profiles must implement.

A Profile encapsulates everything that's scenario-specific:
  - System prompts for each agent role
  - Which tools each agent gets
  - Evaluation criteria and scoring
  - How to extract pass/fail from evaluator output
  - Middleware hooks for agent behavior (loop detection, time budget, etc.)

The Harness framework handles the loop: Plan → Build → Evaluate → Iterate.
The Profile handles the content: what each agent knows and can do.

Configuration hierarchy (highest priority wins):
  1. Environment variables: PROFILE_<PROFILE_NAME>_<KEY> (e.g. PROFILE_TERMINAL_PASS_THRESHOLD=9.0)
  2. ProfileConfig passed to constructor
  3. Profile subclass defaults
  4. BaseProfile defaults
"""
from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import tools


@dataclass
class AgentConfig:
    """Configuration for a single agent role."""
    system_prompt: str
    extra_tool_schemas: list[dict] = field(default_factory=list)
    enabled: bool = True
    middlewares: list = field(default_factory=list)  # list[AgentMiddleware]
    time_budget: float | None = None  # seconds; None = no limit


@dataclass
class ProfileConfig:
    """
    Tunable parameters for a profile, separated from code.

    This lets you adjust thresholds, time budgets, and middleware settings
    without touching the profile's Python code. Useful for:
      - Rapid iteration on benchmark tuning
      - Per-model adjustments (different models need different settings)
      - A/B testing different configurations
    """
    # --- Harness loop ---
    pass_threshold: float | None = None       # score to pass evaluation
    max_rounds: int | None = None             # max build→evaluate rounds

    # --- Time budgets (seconds) ---
    task_budget: float | None = None          # total time for the task
    planner_budget: float | None = None       # time for planner agent
    builder_budget: float | None = None       # time for builder agent
    evaluator_budget: float | None = None     # time for evaluator agent

    # --- Middleware thresholds ---
    loop_file_edit_threshold: int | None = None      # edits before loop warning
    loop_command_repeat_threshold: int | None = None  # repeats before loop warning
    task_tracking_nudge_after: int | None = None      # tool calls before tracking nudge
    time_warn_threshold: float | None = None          # fraction of budget for warning
    time_critical_threshold: float | None = None      # fraction of budget for critical

    def _env_key(self, profile_name: str, field_name: str) -> str:
        """Build environment variable name: PROFILE_TERMINAL_PASS_THRESHOLD."""
        return f"PROFILE_{profile_name.upper().replace('-', '_')}_{field_name.upper()}"

    def resolve(self, field_name: str, profile_name: str, default):
        """
        Resolve a config value with priority: env var > explicit config > default.
        """
        # Check environment variable
        env_key = self._env_key(profile_name, field_name)
        env_val = os.environ.get(env_key)
        if env_val is not None:
            # Coerce to the type of default
            if isinstance(default, float):
                return float(env_val)
            elif isinstance(default, int):
                return int(env_val)
            return env_val

        # Check explicit config value
        config_val = getattr(self, field_name, None)
        if config_val is not None:
            return config_val

        return default


class BaseProfile(ABC):
    """
    Abstract base for task profiles.

    Subclass this to create a new scenario (app building, SWE-Bench,
    terminal tasks, etc.). The harness calls these methods to get
    scenario-specific configuration.

    Accepts an optional ProfileConfig for tunable parameters.
    Subclasses read config via self.cfg.resolve(field, profile_name, default).
    """

    def __init__(self, cfg: ProfileConfig | None = None):
        self.cfg = cfg or ProfileConfig()

    @abstractmethod
    def name(self) -> str:
        """Short identifier for this profile (e.g. 'app-builder', 'swe-bench')."""
        ...

    @abstractmethod
    def description(self) -> str:
        """One-line description shown in --help."""
        ...

    @abstractmethod
    def planner(self) -> AgentConfig:
        """Config for the planning agent. Return enabled=False to skip planning."""
        ...

    @abstractmethod
    def builder(self) -> AgentConfig:
        """Config for the execution agent."""
        ...

    @abstractmethod
    def evaluator(self) -> AgentConfig:
        """Config for the evaluation agent."""
        ...

    def contract_proposer(self) -> AgentConfig:
        """Config for contract proposer. Override to customize or disable."""
        return AgentConfig(system_prompt="", enabled=False)

    def contract_reviewer(self) -> AgentConfig:
        """Config for contract reviewer. Override to customize or disable."""
        return AgentConfig(system_prompt="", enabled=False)

    def pass_threshold(self) -> float:
        """Score threshold to pass evaluation. Default 7.0."""
        return self.cfg.resolve("pass_threshold", self.name(), 7.0)

    def max_rounds(self) -> int | None:
        """Override max harness rounds. None = use config default."""
        return self.cfg.resolve("max_rounds", self.name(), None)

    def format_build_task(self, user_prompt: str, round_num: int,
                          prev_feedback: str, score_history: list[float]) -> str:
        """
        Build the task string sent to the builder each round.
        Override for custom task formatting.
        """
        task = f"Task: {user_prompt}\n"
        if prev_feedback:
            task += f"\nPrevious evaluation feedback:\n{prev_feedback}\n"
        if score_history:
            task += f"\nScore history: {score_history}\n"
        return task

    def extract_score(self, feedback_text: str) -> float:
        """
        Parse the score from evaluator output.
        Override for custom scoring formats.
        Default: looks for 'Average: X/10' pattern.
        """
        import re
        match = re.search(r"[Aa]verage[:\s]*(\d+\.?\d*)\s*/\s*10", feedback_text)
        if match:
            return float(match.group(1))
        scores = re.findall(r"(\d+\.?\d*)\s*/\s*10", feedback_text)
        if scores:
            vals = [float(s) for s in scores]
            return sum(vals) / len(vals)
        return 0.0

    def resolve_task_timeout(self, user_prompt: str) -> float | None:
        """
        Resolve the actual timeout for a task based on the user prompt.

        Override in subclasses that have task-specific timeout metadata
        (e.g. terminal profile uses TB2 task.toml data).

        Returns timeout in seconds, or None to use the default budget.
        """
        return None

    def resolve_time_allocation(self, user_prompt: str) -> dict:
        """
        Return time allocation for the three phases as fractions of total budget.

        Returns dict with keys: planner, builder, evaluator (floats summing to ~1.0),
        and optionally planner_enabled, evaluator_enabled (bools).

        Override in subclasses for dynamic allocation based on task properties.
        Default: fixed split.
        """
        return {
            "planner": 0.07,    # ~7% for planning
            "builder": 0.83,    # ~83% for building
            "evaluator": 0.10,  # ~10% for evaluation
            "planner_enabled": True,
            "evaluator_enabled": True,
        }
