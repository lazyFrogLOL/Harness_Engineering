"""
Harbor adapter — runs our harness agent on Terminal-Bench 2.0 via Harbor framework.

Harbor has two agent types:
  - External (BaseAgent): agent runs outside container, sends commands via environment.exec()
  - Installed (BaseInstalledAgent): agent is installed inside the container

We use Installed agent — our harness.py runs natively inside the container,
so run_bash just works as subprocess without any bridging.

Usage:
  # Install harbor
  pip install harbor

  # Test on hello-world task
  harbor run -d "terminal-bench@2.0" \
    --agent-import-path benchmarks.harbor_agent:HarnessAgent \
    --task-names hello-world

  # Full benchmark
  harbor run -d "terminal-bench@2.0" \
    --agent-import-path benchmarks.harbor_agent:HarnessAgent

  # With Daytona (no Docker needed locally)
  harbor run -d "terminal-bench@2.0" \
    --agent-import-path benchmarks.harbor_agent:HarnessAgent \
    --env daytona
"""
from __future__ import annotations

import os
import shlex
from pathlib import Path

from harbor.agents.installed.base import BaseInstalledAgent, with_prompt_template
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext


class HarnessAgent(BaseInstalledAgent):
    """
    Installs our harness inside the Harbor container and runs it
    with --profile terminal for each task.
    """

    @staticmethod
    def name() -> str:
        return "harness-agent"

    def __init__(self, model_name: str | None = None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._model_name = model_name

    async def install(self, environment: BaseEnvironment) -> None:
        """Install dependencies and clone our repo into the container.

        Strategy: never use apt-get for python (too slow/unreliable on Daytona).
        1. Ensure git exists (apt-get only for git, which is tiny and fast)
        2. Clone repo (includes vendor_wheels/)
        3. If no python3 → download standalone python from GitHub (~30MB)
        4. Install openai from vendored wheels (fully offline)
        """
        # Step 1: Ensure git is available (only apt-get we ever do)
        await self.exec_as_root(
            environment,
            command=(
                "command -v git >/dev/null 2>&1 || "
                "( for i in $(seq 1 15); do "
                "    fuser /var/lib/dpkg/lock >/dev/null 2>&1 || break; sleep 2; "
                "  done && "
                "  apt-get update -qq 2>/dev/null && "
                "  apt-get install -y -qq git 2>/dev/null ) || true"
            ),
        )

        # Step 2: Clone repo (includes vendor_wheels/)
        await self.exec_as_agent(
            environment,
            command=(
                "git clone --depth 1 "
                "https://github.com/lazyFrogLOL/Harness_Engineering.git "
                "/home/user/harness-agent"
            ),
        )

        # Step 3: If no python3, install standalone from GitHub
        await self.exec_as_root(
            environment,
            command=(
                "if command -v python3 >/dev/null 2>&1; then "
                "  echo \"python3 found: $(python3 --version)\"; "
                "else "
                "  echo 'No python3, installing standalone from GitHub...' && "
                "  curl -sL -o /tmp/python.tar.gz "
                "    'https://github.com/astral-sh/python-build-standalone/releases/"
                "download/20250604/cpython-3.12.11+20250604-x86_64-unknown-linux-gnu-install_only.tar.gz' && "
                "  mkdir -p /opt/python && "
                "  tar -xzf /tmp/python.tar.gz -C /opt/python --strip-components=1 && "
                "  ln -sf /opt/python/bin/python3 /usr/local/bin/python3 && "
                "  ln -sf /opt/python/bin/pip3 /usr/local/bin/pip3 && "
                "  rm -f /tmp/python.tar.gz && "
                "  echo \"standalone python installed: $(python3 --version)\"; "
                "fi"
            ),
        )

        # Step 4: Install openai from vendored wheels (fully offline)
        await self.exec_as_root(
            environment,
            command=(
                "python3 -c 'import openai' 2>/dev/null || "
                # Try pip with vendored wheels
                "( pip3 install --break-system-packages --no-index "
                "  --find-links=/home/user/harness-agent/vendor_wheels "
                "  openai 2>/dev/null && "
                "  python3 -c 'import openai' 2>/dev/null ) || "
                "( python3 -m pip install --break-system-packages --no-index "
                "  --find-links=/home/user/harness-agent/vendor_wheels "
                "  openai 2>/dev/null && "
                "  python3 -c 'import openai' 2>/dev/null ) || "
                # No pip at all — unzip wheels directly
                "( SITE=$(python3 -c 'import site; print(site.getsitepackages()[0])') && "
                "  for whl in /home/user/harness-agent/vendor_wheels/*.whl; do "
                "    python3 -m zipfile -e \"$whl\" \"$SITE\" 2>/dev/null; "
                "  done && "
                "  python3 -c 'import openai; print(\"openai installed via wheel unzip\")' ) || "
                "echo 'FATAL: failed to install openai'"
            ),
        )

    @with_prompt_template
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        """Run our harness with --profile terminal on the given task."""
        escaped = shlex.quote(instruction)

        # Build env vars string for the command
        env_vars = []
        for key in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "HARNESS_MODEL"):
            val = os.environ.get(key)
            if val:
                env_vars.append(f"{key}={shlex.quote(val)}")

        env_vars.append("HARNESS_WORKSPACE=/app")
        env_vars.append("HARNESS_FLAT_WORKSPACE=1")
        env_prefix = " ".join(env_vars)

        # Run harness with system python3
        await self.exec_as_agent(
            environment,
            command=(
                f"cd /home/user/harness-agent && "
                f"{env_prefix} "
                f"python3 harness.py --profile terminal {escaped}"
            ),
        )

    def populate_context_post_run(self, context: AgentContext) -> None:
        """Called after run() completes. Could parse logs if needed."""
        pass
