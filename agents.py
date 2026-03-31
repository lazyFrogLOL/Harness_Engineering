"""
Agent implementation — the core while loop with tool use.
Uses OpenAI-compatible chat completions API with function calling.
"""
from __future__ import annotations

import json
import time
import logging
from openai import OpenAI

import config
import tools
import context

log = logging.getLogger("harness")

# ---------------------------------------------------------------------------
# LLM client (singleton)
# ---------------------------------------------------------------------------

_client: OpenAI | None = None


def get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(
            api_key=config.API_KEY,
            base_url=config.BASE_URL,
            timeout=300.0,        # 5 min per request
            max_retries=2,
        )
    return _client


def llm_call_simple(messages: list[dict]) -> str:
    """Simple LLM call without tools — used for summarization."""
    resp = get_client().chat.completions.create(
        model=config.MODEL,
        messages=messages,
        max_tokens=10000,
    )
    return resp.choices[0].message.content or ""


# ---------------------------------------------------------------------------
# Core agent loop
# ---------------------------------------------------------------------------

class Agent:
    """
    A single agent with a system prompt and tool access.

    This is the 'managed agent loop' from the architecture:
    - while loop with llm.call(prompt)
    - tool execution
    - context lifecycle (compaction / reset)

    Skills are handled via progressive disclosure:
    - Level 1: skill catalog (name + description) is baked into system_prompt
    - Level 2: agent decides to read_skill_file("skills/.../SKILL.md") on its own
    - Level 3: SKILL.md references sub-files, agent reads those too
    No external code decides which skills to load — the agent does.
    """

    def __init__(self, name: str, system_prompt: str, use_tools: bool = True,
                 extra_tool_schemas: list[dict] | None = None,
                 middlewares: list | None = None,
                 time_budget: float | None = None):
        self.name = name
        self.system_prompt = system_prompt
        self.use_tools = use_tools
        self.extra_tool_schemas = extra_tool_schemas or []
        self.middlewares = middlewares or []  # list[AgentMiddleware]
        self.time_budget = time_budget

    def run(self, task: str) -> str:
        """
        Execute the agent loop until the model stops calling tools
        or we hit the iteration limit.

        Returns the final assistant text response.
        """
        messages: list[dict] = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": task},
        ]

        client = get_client()
        consecutive_errors = 0
        last_text = ""

        for iteration in range(1, config.MAX_AGENT_ITERATIONS + 1):
            # --- Middleware: per-iteration hooks ---
            for mw in self.middlewares:
                inject = mw.per_iteration(iteration, messages)
                if inject:
                    messages.append({"role": "user", "content": inject})

            # --- Context lifecycle check ---
            token_count = context.count_tokens(messages)
            log.info(f"[{self.name}] iteration={iteration}  tokens≈{token_count}")

            if token_count > config.RESET_THRESHOLD or context.detect_anxiety(messages):
                reason = "anxiety detected" if token_count <= config.RESET_THRESHOLD else f"tokens {token_count} > threshold"
                log.warning(f"[{self.name}] Context reset triggered ({reason}). Writing checkpoint...")
                checkpoint = context.create_checkpoint(messages, llm_call_simple)
                messages = context.restore_from_checkpoint(checkpoint, self.system_prompt)
            elif token_count > config.COMPRESS_THRESHOLD:
                log.info(f"[{self.name}] Compacting context (role={self.name})...")
                messages = context.compact_messages(messages, llm_call_simple, role=self.name)

            # --- LLM call ---
            kwargs = dict(
                model=config.MODEL,
                messages=messages,
                max_tokens=32768,
            )
            if self.use_tools:
                kwargs["tools"] = tools.TOOL_SCHEMAS + self.extra_tool_schemas
                kwargs["tool_choice"] = "auto"

            try:
                response = client.chat.completions.create(**kwargs)
            except Exception as e:
                log.error(f"[{self.name}] API error: {e}")
                consecutive_errors += 1
                if consecutive_errors >= config.MAX_TOOL_ERRORS:
                    log.error(f"[{self.name}] Too many API errors, aborting.")
                    break
                time.sleep(2 ** consecutive_errors)
                continue

            consecutive_errors = 0
            choice = response.choices[0]
            msg = choice.message

            # --- Append assistant message to history ---
            assistant_msg = {"role": "assistant", "content": msg.content}
            if msg.tool_calls:
                assistant_msg["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in msg.tool_calls
                ]
            messages.append(assistant_msg)

            # --- If model produced text, capture it ---
            if msg.content:
                last_text = msg.content
                log.info(f"[{self.name}] assistant: {msg.content[:200]}...")

            # --- If no tool calls, check pre-exit middlewares ---
            if not msg.tool_calls:
                forced_continue = False
                for mw in self.middlewares:
                    inject = mw.pre_exit(messages)
                    if inject:
                        messages.append({"role": "user", "content": inject})
                        forced_continue = True
                        break  # one pre-exit nudge at a time
                if forced_continue:
                    continue  # re-enter the loop
                log.info(f"[{self.name}] Finished (no more tool calls).")
                break

            # --- Execute tool calls ---
            for tc in msg.tool_calls:
                fn_name = tc.function.name
                try:
                    fn_args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    log.warning(f"[{self.name}] Bad JSON in tool call {fn_name}: {tc.function.arguments[:200]}")
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": f"[error] Invalid JSON arguments: {tc.function.arguments[:200]}",
                    })
                    continue

                log.info(f"[{self.name}] tool: {fn_name}({_truncate(str(fn_args), 120)})")
                result = tools.execute_tool(fn_name, fn_args)
                log.debug(f"[{self.name}] tool result: {_truncate(result, 200)}")

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result,
                })

                # --- Middleware: post-tool hooks ---
                for mw in self.middlewares:
                    inject = mw.post_tool(fn_name, fn_args, result, messages)
                    if inject:
                        messages.append({"role": "user", "content": inject})
                        break  # one nudge per tool call is enough

            # --- Check finish reason ---
            if choice.finish_reason == "stop":
                log.info(f"[{self.name}] Finished (stop).")
                break

            if choice.finish_reason == "length":
                log.warning(f"[{self.name}] Output truncated (max_tokens hit). Asking model to retry with smaller chunks.")
                messages.append({
                    "role": "user",
                    "content": (
                        "[SYSTEM] Your last response was cut off because it exceeded the token limit. "
                        "The tool call was NOT executed. "
                        "Please retry, but split large files into smaller parts:\n"
                        "1. Write the first half of the file with write_file\n"
                        "2. Then write the second half as a separate file or append\n"
                        "Or simplify the implementation to fit in one response."
                    ),
                })

        else:
            log.warning(f"[{self.name}] Hit max iterations ({config.MAX_AGENT_ITERATIONS}).")

        return last_text


def _truncate(s: str, n: int) -> str:
    return s[:n] + "..." if len(s) > n else s
