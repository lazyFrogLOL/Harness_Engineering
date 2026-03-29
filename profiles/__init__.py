# Profile registry

from profiles.base import BaseProfile, AgentConfig
from profiles.app_builder import AppBuilderProfile
from profiles.terminal import TerminalProfile
from profiles.swe_bench import SWEBenchProfile
from profiles.reasoning import ReasoningProfile

PROFILES: dict[str, type[BaseProfile]] = {
    "app-builder": AppBuilderProfile,
    "terminal": TerminalProfile,
    "swe-bench": SWEBenchProfile,
    "reasoning": ReasoningProfile,
}


def get_profile(name: str) -> BaseProfile:
    """Get a profile instance by name."""
    cls = PROFILES.get(name)
    if cls is None:
        available = ", ".join(PROFILES.keys())
        raise ValueError(f"Unknown profile: {name}. Available: {available}")
    return cls()


def list_profiles() -> list[dict[str, str]]:
    """List all available profiles."""
    return [
        {"name": cls().name(), "description": cls().description()}
        for cls in PROFILES.values()
    ]
