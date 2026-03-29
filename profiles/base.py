"""
Base profile — defines the interface that all task profiles must implement.

A Profile encapsulates everything that's scenario-specific:
  - System prompts for each agent role
  - Which tools each agent gets
  - Evaluation criteria and scoring
  - How to extract pass/fail from evaluator output

The Harness framework handles the loop: Plan → Build → Evaluate → Iterate.
The Profile handles the content: what each agent knows and can do.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import tools


@dataclass
class AgentConfig:
    """Configuration for a single agent role."""
    system_prompt: str
    extra_tool_schemas: list[dict] = field(default_factory=list)
    enabled: bool = True


class BaseProfile(ABC):
    """
    Abstract base for task profiles.

    Subclass this to create a new scenario (app building, SWE-Bench,
    terminal tasks, etc.). The harness calls these methods to get
    scenario-specific configuration.
    """

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
        return 7.0

    def max_rounds(self) -> int | None:
        """Override max harness rounds. None = use config default."""
        return None

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
