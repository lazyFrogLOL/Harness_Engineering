#!/usr/bin/env python3
"""
Harness — profile-driven multi-agent architecture for autonomous task execution.

Reproduces the design from Anthropic's "Harness design for long-running
application development" using a pure Python + OpenAI-compatible API approach.

The core loop (Plan → Build → Evaluate → Iterate) is generic.
Profiles define the scenario-specific behavior (prompts, tools, scoring).

Built-in profiles:
  app-builder  — Build web apps from a prompt (original Anthropic article scenario)
  terminal     — Solve terminal/CLI tasks (Terminal-Bench-2 style)
  swe-bench    — Fix GitHub issues in real repos
  reasoning    — Knowledge-intensive QA (MMMU-Pro style)

Usage:
  python harness.py "Build a DAW in the browser"                    # default: app-builder
  python harness.py --profile terminal "Fix the broken git merge"
  python harness.py --profile swe-bench "Fix issue #123"
  python harness.py --profile reasoning "Calculate the orbital period of..."
  python harness.py --list-profiles
"""
from __future__ import annotations

import logging
import os
import re
import sys
import time
from pathlib import Path

import config
import tools
from agents import Agent
from skills import SkillRegistry
from profiles import get_profile, list_profiles
from profiles.base import BaseProfile

log = logging.getLogger("harness")


class Harness:
    """
    Generic orchestration loop driven by a Profile.

    The Profile defines:
      - System prompts for each agent role
      - Which tools each agent gets
      - Evaluation criteria and pass threshold
      - Whether contract negotiation is enabled

    The Harness handles:
      - The Plan → Build → Evaluate → Iterate loop
      - Context lifecycle (compaction / reset)
      - Score tracking and REFINE/PIVOT decisions
      - Workspace and git management
    """

    def __init__(self, profile: BaseProfile):
        self.profile = profile
        self.skill_registry = SkillRegistry()
        skill_catalog = self.skill_registry.build_catalog_prompt()

        # Build agents from profile config
        planner_cfg = profile.planner()
        builder_cfg = profile.builder()
        evaluator_cfg = profile.evaluator()
        proposer_cfg = profile.contract_proposer()
        reviewer_cfg = profile.contract_reviewer()

        self.planner = Agent(
            "planner", planner_cfg.system_prompt + skill_catalog,
            use_tools=True, extra_tool_schemas=planner_cfg.extra_tool_schemas,
            middlewares=planner_cfg.middlewares, time_budget=planner_cfg.time_budget,
        ) if planner_cfg.enabled else None

        self.builder = Agent(
            "builder", builder_cfg.system_prompt + skill_catalog,
            use_tools=True, extra_tool_schemas=builder_cfg.extra_tool_schemas,
            middlewares=builder_cfg.middlewares, time_budget=builder_cfg.time_budget,
        )

        self.evaluator = Agent(
            "evaluator", evaluator_cfg.system_prompt,
            use_tools=True, extra_tool_schemas=evaluator_cfg.extra_tool_schemas,
            middlewares=evaluator_cfg.middlewares, time_budget=evaluator_cfg.time_budget,
        ) if evaluator_cfg.enabled else None

        self.contract_proposer = Agent(
            "contract_proposer", proposer_cfg.system_prompt, use_tools=True,
            middlewares=proposer_cfg.middlewares,
        ) if proposer_cfg.enabled else None

        self.contract_reviewer = Agent(
            "contract_reviewer", reviewer_cfg.system_prompt, use_tools=True,
            middlewares=reviewer_cfg.middlewares,
        ) if reviewer_cfg.enabled else None

    def run(self, user_prompt: str) -> None:
        # Create a unique project subdirectory under workspace
        # (skip if HARNESS_FLAT_WORKSPACE is set — used for benchmarks
        #  where outputs must land directly in the workspace root)
        if os.environ.get("HARNESS_FLAT_WORKSPACE"):
            Path(config.WORKSPACE).mkdir(parents=True, exist_ok=True)
        else:
            from datetime import datetime
            slug = re.sub(r'[^a-z0-9]+', '-', user_prompt.lower().strip())[:40].strip('-')
            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            project_name = f"{timestamp}_{slug}"
            project_dir = os.path.join(config.WORKSPACE, project_name)

            config.WORKSPACE = os.path.abspath(project_dir)
            Path(config.WORKSPACE).mkdir(parents=True, exist_ok=True)

        log.info(f"Profile: {self.profile.name()}")
        log.info(f"Project directory: {config.WORKSPACE}")

        # Initialize git
        git_dir = Path(config.WORKSPACE) / ".git"
        if not git_dir.exists():
            os.system(f"cd {config.WORKSPACE} && git init && git add -A 2>/dev/null; git commit -m 'init' --allow-empty 2>/dev/null")

        total_start = time.time()
        max_rounds = self.profile.max_rounds() or config.MAX_HARNESS_ROUNDS
        threshold = self.profile.pass_threshold()

        # ---- Phase 1: Planning ----
        if self.planner:
            log.info("=" * 60)
            log.info("PHASE 1: PLANNING")
            log.info("=" * 60)
            phase_start = time.time()

            self.planner.run(
                f"Create a plan for the following task:\n\n"
                f"{user_prompt}\n\n"
                f"Save the plan to spec.md."
            )

            log.info(f"Planning completed in {time.time() - phase_start:.0f}s")
        else:
            # No planner — write prompt directly as spec
            spec_path = Path(config.WORKSPACE) / config.SPEC_FILE
            spec_path.write_text(f"# Task\n\n{user_prompt}\n", encoding="utf-8")
            log.info("No planner — wrote prompt directly to spec.md")

        # ---- Phase 2: Build → Evaluate loop ----
        score_history: list[float] = []

        for round_num in range(1, max_rounds + 1):

            # ---- Contract negotiation (if enabled) ----
            if self.contract_proposer and self.contract_reviewer:
                log.info("=" * 60)
                log.info(f"ROUND {round_num}/{max_rounds}: CONTRACT NEGOTIATION")
                log.info("=" * 60)
                contract_start = time.time()
                self._negotiate_contract(round_num)
                log.info(f"Contract negotiation completed in {time.time() - contract_start:.0f}s")

            # ---- Build ----
            log.info("=" * 60)
            log.info(f"ROUND {round_num}/{max_rounds}: BUILD")
            log.info("=" * 60)
            build_start = time.time()

            feedback_path = Path(config.WORKSPACE) / config.FEEDBACK_FILE
            prev_feedback = feedback_path.read_text(encoding="utf-8") if feedback_path.exists() else ""

            build_task = self.profile.format_build_task(
                user_prompt, round_num, prev_feedback, score_history,
            )

            self.builder.run(build_task)
            log.info(f"Build round {round_num} completed in {time.time() - build_start:.0f}s")

            # ---- Evaluate (if enabled) ----
            if self.evaluator:
                log.info("=" * 60)
                log.info(f"ROUND {round_num}/{max_rounds}: EVALUATE")
                log.info("=" * 60)
                eval_start = time.time()

                self.evaluator.run(
                    f"This is evaluation round {round_num}.\n"
                    f"Read spec.md to understand the task.\n"
                    f"Examine the work done and test it thoroughly.\n"
                    f"Score each criterion honestly. Write your evaluation to feedback.md."
                )

                log.info(f"Evaluation round {round_num} completed in {time.time() - eval_start:.0f}s")
                tools.stop_dev_server()

                # Check score
                feedback_text = ""
                if feedback_path.exists():
                    feedback_text = feedback_path.read_text(encoding="utf-8")
                score = self.profile.extract_score(feedback_text)
                score_history.append(score)
                log.info(f"Round {round_num} average score: {score:.1f} / 10  (threshold: {threshold})")
                log.info(f"Score history: {score_history}")

                if score >= threshold:
                    log.info(f"PASSED at round {round_num}.")
                    break
            else:
                log.info("No evaluator — single-pass execution.")
                break

        else:
            log.warning(f"Did not pass after {max_rounds} rounds.")

        total_duration = time.time() - total_start
        log.info("=" * 60)
        log.info(f"HARNESS COMPLETE — total time: {total_duration / 60:.1f} minutes")
        log.info(f"Output in: {config.WORKSPACE}")
        log.info("=" * 60)

    def _negotiate_contract(self, round_num: int, max_iterations: int = 3) -> None:
        self.contract_proposer.run(
            f"This is round {round_num}.\n"
            f"Read spec.md. If feedback.md exists, read it too.\n"
            f"Propose a sprint contract for this round. Write it to contract.md."
        )

        for i in range(max_iterations):
            log.info(f"[contract] Review iteration {i + 1}/{max_iterations}")

            self.contract_reviewer.run(
                f"Review the sprint contract in contract.md for round {round_num}.\n"
                f"Read spec.md for context. Read feedback.md if it exists.\n"
                f"If acceptable, write APPROVED at the top and save to contract.md.\n"
                f"If changes needed, write revision requests and save updated contract."
            )

            contract_path = Path(config.WORKSPACE) / "contract.md"
            if contract_path.exists():
                contract_text = contract_path.read_text(encoding="utf-8")
                if "APPROVED" in contract_text.upper()[:200]:
                    log.info("[contract] Contract approved.")
                    return

            if i < max_iterations - 1:
                log.info("[contract] Contract needs revision...")
                self.contract_proposer.run(
                    f"The reviewer requested changes. Read contract.md and revise."
                )

        log.warning("[contract] Max iterations reached, proceeding with current contract.")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    from logger import setup_logging
    setup_logging(verbose="--verbose" in sys.argv or "-v" in sys.argv)

    # Parse flags
    args = [a for a in sys.argv[1:] if a not in ("--verbose", "-v")]

    # --list-profiles
    if "--list-profiles" in args:
        print("Available profiles:\n")
        for p in list_profiles():
            print(f"  {p['name']:15s} {p['description']}")
        sys.exit(0)

    # --profile <name>
    profile_name = "app-builder"
    if "--profile" in args:
        idx = args.index("--profile")
        if idx + 1 < len(args):
            profile_name = args[idx + 1]
            args = args[:idx] + args[idx + 2:]
        else:
            print("Error: --profile requires a name")
            sys.exit(1)

    if not config.API_KEY:
        print("Error: Set OPENAI_API_KEY in .env or environment.")
        sys.exit(1)

    if len(args) < 1:
        print("Usage: python harness.py [--profile <name>] \"<task>\" [--verbose]")
        print()
        print("Profiles:")
        for p in list_profiles():
            print(f"  {p['name']:15s} {p['description']}")
        print()
        print("Examples:")
        print('  python harness.py "Build a DAW in the browser"')
        print('  python harness.py --profile terminal "Fix the broken symlinks in /tmp"')
        print('  python harness.py --profile swe-bench "Fix the TypeError in parse_config()"')
        print('  python harness.py --profile reasoning "What is the escape velocity of Mars?"')
        sys.exit(1)

    user_prompt = " ".join(args)

    try:
        profile = get_profile(profile_name)
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)

    log.info(f"Prompt: {user_prompt}")
    log.info(f"Profile: {profile_name}")
    log.info(f"Model: {config.MODEL}")
    log.info(f"Base URL: {config.BASE_URL}")
    log.info(f"Workspace: {config.WORKSPACE}")

    # Preflight
    log.info("Verifying API connection...")
    try:
        from agents import get_client
        resp = get_client().chat.completions.create(
            model=config.MODEL,
            messages=[{"role": "user", "content": "Say OK"}],
            max_tokens=5,
        )
        log.info(f"API OK — model responded: {resp.choices[0].message.content}")
    except Exception as e:
        log.error(f"API preflight failed: {e}")
        print(f"\nCannot connect to API. Check your .env:\n"
              f"  OPENAI_API_KEY  — is it valid?\n"
              f"  OPENAI_BASE_URL — is {config.BASE_URL} correct?\n"
              f"  HARNESS_MODEL   — does {config.MODEL} exist on this provider?")
        sys.exit(1)

    harness = Harness(profile)
    harness.run(user_prompt)


if __name__ == "__main__":
    main()
