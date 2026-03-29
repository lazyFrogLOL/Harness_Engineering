"""
Terminal-Bench 2.0 adapter — bridges our Agent loop to the TB2 evaluation harness.

Two integration options:

  Option A (Recommended): BaseAgent — our code runs outside the container,
  sends commands via tmux session.

  Option B: AbstractInstalledAgent — our code is installed inside the container.

Usage:
  # Install terminal-bench
  uv tool install terminal-bench

  # Option A: BaseAgent (runs outside container)
  tb run --agent-import-path harbor_agent:HarnessBaseAgent --task-id hello-world

  # Option B: InstalledAgent (installed inside container)
  tb run --agent-import-path harbor_agent:HarnessInstalledAgent --task-id hello-world

  # Full benchmark (5 trials per task)
  tb run --agent-import-path harbor_agent:HarnessBaseAgent -k 5
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

log = logging.getLogger("harness")

# Ensure parent directory is on sys.path so we can import project modules
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))


# ============================================================
# Option A: BaseAgent — recommended, simpler
# ============================================================

from terminal_bench.agents.base_agent import BaseAgent, AgentResult
from terminal_bench.tmux_session import TmuxSession


class HarnessBaseAgent(BaseAgent):
    """
    Adapts our Agent loop to TB2's BaseAgent interface.

    Instead of using our tools.py run_bash (which runs locally),
    we route all commands through the TB2 tmux session (which runs
    inside the Docker container).
    """

    @staticmethod
    def name() -> str:
        return "harness-agent"

    def perform_task(
        self,
        task_description: str,
        session: TmuxSession,
        logging_dir: Path | None = None,
    ) -> AgentResult:
        import config
        import tools as tools_module
        from agents import Agent

        # --- Patch run_bash to route through tmux session ---
        original_run_bash = tools_module.run_bash

        def tmux_run_bash(command: str, timeout: int = 120) -> str:
            """Execute command in TB2's Docker container via tmux."""
            try:
                session.send_keys(command)
                time.sleep(min(timeout, 30))  # wait for command to complete
                output = session.get_output()
                if len(output) > 30_000:
                    output = output[:15_000] + "\n...(truncated)...\n" + output[-15_000:]
                return output or "(no output)"
            except Exception as e:
                return f"[error] {e}"

        # --- Patch read_file/write_file to work via tmux ---
        original_read_file = tools_module.read_file
        original_write_file = tools_module.write_file
        original_list_files = tools_module.list_files

        def tmux_read_file(path: str) -> str:
            return tmux_run_bash(f"cat {path} 2>&1 | head -c 60000")

        def tmux_write_file(path: str, content: str) -> str:
            # Use heredoc for writing files in the container
            escaped = content.replace("'", "'\\''")
            return tmux_run_bash(f"mkdir -p $(dirname {path}) && cat > {path} << 'HARNESS_EOF'\n{content}\nHARNESS_EOF")

        def tmux_list_files(directory: str = ".") -> str:
            return tmux_run_bash(f"find {directory} -type f 2>/dev/null | head -200")

        # Apply patches
        tools_module.run_bash = tmux_run_bash
        tools_module.read_file = tmux_read_file
        tools_module.write_file = tmux_write_file
        tools_module.list_files = tmux_list_files
        tools_module.TOOL_DISPATCH.update({
            "run_bash": tmux_run_bash,
            "read_file": tmux_read_file,
            "write_file": tmux_write_file,
            "list_files": tmux_list_files,
        })

        # Don't use workspace sandboxing — we're in a container
        original_workspace = config.WORKSPACE
        config.WORKSPACE = "/home/user"

        prompt_tokens = 0
        completion_tokens = 0

        try:
            # Use the terminal profile's builder prompt
            from profiles.terminal import TerminalProfile
            profile = TerminalProfile()
            builder_cfg = profile.builder()

            agent = Agent(
                name="terminal",
                system_prompt=builder_cfg.system_prompt,
                use_tools=True,
            )

            agent.run(task_description)

        finally:
            # Restore everything
            tools_module.run_bash = original_run_bash
            tools_module.read_file = original_read_file
            tools_module.write_file = original_write_file
            tools_module.list_files = original_list_files
            tools_module.TOOL_DISPATCH.update({
                "run_bash": original_run_bash,
                "read_file": original_read_file,
                "write_file": original_write_file,
                "list_files": original_list_files,
            })
            config.WORKSPACE = original_workspace

        return AgentResult(
            input_tokens=prompt_tokens,
            output_tokens=completion_tokens,
        )


# ============================================================
# Option B: InstalledAgent — runs inside the container
# ============================================================

from terminal_bench.agents.installed_agents.abstract_installed_agent import (
    AbstractInstalledAgent,
)
from terminal_bench.harness_models import TerminalCommand


class HarnessInstalledAgent(AbstractInstalledAgent):
    """
    Installs our harness inside the TB2 Docker container and runs it.
    The agent code is copied into the container and executed directly.
    """

    @staticmethod
    def name() -> str:
        return "harness-agent-installed"

    def __init__(self, model_name: str | None = None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._model_name = model_name

    @property
    def _env(self) -> dict[str, str]:
        """Pass API credentials from host to container."""
        env = {}
        for key in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "HARNESS_MODEL"):
            if key in os.environ:
                env[key] = os.environ[key]
        # Also pass through common provider keys
        for key in ("ANTHROPIC_API_KEY",):
            if key in os.environ:
                env[key] = os.environ[key]
        return env

    @property
    def _install_agent_script_path(self) -> os.PathLike:
        """Path to the setup script that installs our agent in the container."""
        return Path(__file__).parent / "tb_setup.sh"

    def _run_agent_commands(self, task_description: str) -> list[TerminalCommand]:
        """Commands to run our agent with the given task."""
        import shlex
        escaped = shlex.quote(task_description)

        return [
            TerminalCommand(
                command=(
                    f"cd /opt/harness-agent && "
                    f"python3 harness.py --profile terminal {escaped}"
                ),
                max_timeout_sec=300,
                block=True,
            )
        ]
