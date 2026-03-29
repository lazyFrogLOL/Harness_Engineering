"""
App Builder profile — the original scenario from the Anthropic article.
Plan a product → write code → browser-test → iterate.
"""
from __future__ import annotations

import tools
import prompts
from profiles.base import BaseProfile, AgentConfig


class AppBuilderProfile(BaseProfile):

    def name(self) -> str:
        return "app-builder"

    def description(self) -> str:
        return "Build complete web applications from a one-sentence prompt (Anthropic article scenario)"

    def planner(self) -> AgentConfig:
        return AgentConfig(system_prompt=prompts.PLANNER_SYSTEM)

    def builder(self) -> AgentConfig:
        return AgentConfig(system_prompt=prompts.BUILDER_SYSTEM)

    def evaluator(self) -> AgentConfig:
        return AgentConfig(
            system_prompt=prompts.EVALUATOR_SYSTEM,
            extra_tool_schemas=tools.BROWSER_TOOL_SCHEMAS,
        )

    def contract_proposer(self) -> AgentConfig:
        return AgentConfig(system_prompt=prompts.CONTRACT_BUILDER_SYSTEM)

    def contract_reviewer(self) -> AgentConfig:
        return AgentConfig(system_prompt=prompts.CONTRACT_REVIEWER_SYSTEM)
