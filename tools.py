"""
Tool definitions and execution for agents.
Each tool is an OpenAI function-calling schema + a Python implementation.
Agents operate inside config.WORKSPACE to keep generated code isolated.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import config

# Playwright is optional — only needed for evaluator browser testing
try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve(path: str) -> Path:
    """Resolve a relative path inside the workspace. Prevent escaping."""
    p = Path(config.WORKSPACE, path).resolve()
    ws = Path(config.WORKSPACE).resolve()
    if not str(p).startswith(str(ws)):
        raise ValueError(f"Path escapes workspace: {path}")
    return p


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def read_file(path: str) -> str:
    p = _resolve(path)
    if not p.exists():
        return f"[error] File not found: {path}"
    content = p.read_text(encoding="utf-8", errors="replace")
    limit = 60_000
    if len(content) > limit:
        total = len(content)
        content = content[:limit] + (
            f"\n\n[TRUNCATED] You are seeing {limit} of {total} total characters. "
            f"The remaining {total - limit} characters are NOT shown above. "
            f"You MUST use run_bash with head/tail/sed to read the rest if needed."
        )
    return content


def read_skill_file(path: str) -> str:
    """Read a file from the skills directory (outside workspace). Path must be relative to project root."""
    project_root = Path(__file__).parent
    p = (project_root / path).resolve()
    # Must stay within the skills directory
    skills_dir = (project_root / "skills").resolve()
    if not str(p).startswith(str(skills_dir)):
        return f"[error] Path must be inside skills/ directory: {path}"
    if not p.exists():
        return f"[error] Skill file not found: {path}"
    return p.read_text(encoding="utf-8", errors="replace")[:60_000]


def write_file(path: str, content: str) -> str:
    if not path or not path.strip():
        return "[error] Empty file path"
    p = _resolve(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"Wrote {len(content)} chars to {path}"


def list_files(directory: str = ".") -> str:
    p = _resolve(directory)
    if not p.is_dir():
        return f"[error] Not a directory: {directory}"
    entries = []
    for item in sorted(p.rglob("*")):
        if item.is_file():
            rel = item.relative_to(Path(config.WORKSPACE).resolve())
            entries.append(str(rel))
    if not entries:
        return "(empty)"
    return "\n".join(entries[:200])


def run_bash(command: str, timeout: int = 300) -> str:
    """Run a shell command inside the workspace. Returns stdout+stderr."""
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=config.WORKSPACE,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = (result.stdout + "\n" + result.stderr).strip()
        if len(output) > 30_000:
            total = len(output)
            output = (
                output[:15_000]
                + f"\n\n[TRUNCATED] Showing first 15k + last 15k of {total} total characters. "
                f"The middle {total - 30_000} characters are NOT shown.\n\n"
                + output[-15_000:]
            )
        return output or "(no output)"
    except subprocess.TimeoutExpired:
        return (
            f"[error] Command timed out after {timeout}s. "
            f"If this command legitimately needs more time (e.g. compilation, training), "
            f"retry with a larger timeout parameter."
        )
    except Exception as e:
        return f"[error] {e}"


# ---------------------------------------------------------------------------
# Sub-agent delegation (context isolation)
# ---------------------------------------------------------------------------

def delegate_task(task: str, role: str = "assistant") -> str:
    """
    Spawn a sub-agent in a completely isolated context to handle a subtask.

    The sub-agent gets a clean context window — it does NOT inherit the parent's
    conversation history. It has access to the same workspace and tools.
    Only the structured result comes back to the parent.

    Use this for:
    - Exploring/reading many files without polluting your context
    - Running a series of bash commands and summarizing results
    - Any "dirty work" that would bloat your context window

    The sub-agent's internal reasoning is invisible to the caller.
    """
    # Lazy import to avoid circular dependency
    from agents import Agent

    sub = Agent(
        name=f"sub_{role}",
        system_prompt=(
            f"You are a sub-agent with the role: {role}. "
            f"Complete the assigned task and provide a concise, structured summary of your findings. "
            f"You have access to the workspace files and bash. "
            f"Focus only on the task — do not do extra work.\n"
            f"When done, respond with a clear summary of:\n"
            f"1. What you found or did\n"
            f"2. Key results or artifacts created\n"
            f"3. Any issues encountered"
        ),
        use_tools=True,
    )

    result = sub.run(task)

    if not result:
        return "[sub-agent returned no output]"

    # Truncate to avoid blowing up the parent's context
    if len(result) > 8000:
        result = result[:8000] + "\n...(truncated)"

    return result


# ---------------------------------------------------------------------------
# Playwright browser testing
# ---------------------------------------------------------------------------

# Holds a background dev server process so we can start it once and reuse
_dev_server_proc: subprocess.Popen | None = None


def _ensure_dev_server(start_command: str, port: int, startup_wait: int = 8) -> str:
    """Start a dev server in the background if not already running."""
    global _dev_server_proc
    if _dev_server_proc is not None and _dev_server_proc.poll() is None:
        return f"Dev server already running (pid={_dev_server_proc.pid})"
    _dev_server_proc = subprocess.Popen(
        start_command,
        shell=True,
        cwd=config.WORKSPACE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    time.sleep(startup_wait)
    if _dev_server_proc.poll() is not None:
        stderr = _dev_server_proc.stderr.read().decode(errors="replace")[:2000]
        return f"[error] Dev server exited immediately: {stderr}"
    return f"Dev server started (pid={_dev_server_proc.pid}, port={port})"


def stop_dev_server() -> str:
    """Stop the background dev server."""
    global _dev_server_proc
    if _dev_server_proc is None:
        return "No dev server running"
    _dev_server_proc.terminate()
    try:
        _dev_server_proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        _dev_server_proc.kill()
    _dev_server_proc = None
    return "Dev server stopped"


def browser_test(
    url: str,
    actions: list[dict] | None = None,
    screenshot: bool = True,
    start_command: str | None = None,
    port: int = 5173,
    startup_wait: int = 8,
) -> str:
    """
    Launch a headless browser, navigate to a URL, perform actions, and
    optionally take a screenshot. Returns a text report of what happened.

    actions is a list of dicts, each with:
      - type: "click" | "fill" | "wait" | "evaluate" | "scroll"
      - selector: CSS selector (for click/fill)
      - value: text to type (for fill), JS code (for evaluate)
      - delay: ms to wait (for wait)

    If start_command is provided, starts a dev server first.
    """
    if not HAS_PLAYWRIGHT:
        return (
            "[error] Playwright not installed. "
            "Install with: pip install playwright && python -m playwright install chromium"
        )

    report_lines = []

    # Optionally start dev server
    if start_command:
        srv_result = _ensure_dev_server(start_command, port, startup_wait)
        report_lines.append(f"Server: {srv_result}")

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 720})

            # Navigate
            try:
                page.goto(url, timeout=15000)
                report_lines.append(f"Navigated to {url} — title: {page.title()}")
            except Exception as e:
                report_lines.append(f"[error] Navigation failed: {e}")
                browser.close()
                return "\n".join(report_lines)

            # Check for console errors
            console_errors = []
            page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)

            # Execute actions
            for action in (actions or []):
                action_type = action.get("type", "")
                selector = action.get("selector", "")
                value = action.get("value", "")
                delay = action.get("delay", 1000)

                try:
                    if action_type == "click":
                        page.click(selector, timeout=5000)
                        report_lines.append(f"Clicked: {selector}")
                    elif action_type == "fill":
                        page.fill(selector, value, timeout=5000)
                        report_lines.append(f"Filled '{selector}' with '{value[:50]}'")
                    elif action_type == "wait":
                        page.wait_for_timeout(delay)
                        report_lines.append(f"Waited {delay}ms")
                    elif action_type == "evaluate":
                        result = page.evaluate(value)
                        report_lines.append(f"JS eval result: {str(result)[:500]}")
                    elif action_type == "scroll":
                        page.evaluate(f"window.scrollBy(0, {value or 500})")
                        report_lines.append(f"Scrolled by {value or 500}px")
                    else:
                        report_lines.append(f"[warn] Unknown action type: {action_type}")
                except Exception as e:
                    report_lines.append(f"[error] Action {action_type}('{selector}'): {e}")

                page.wait_for_timeout(300)  # brief pause between actions

            # Gather page info
            report_lines.append(f"Final URL: {page.url}")
            report_lines.append(f"Visible text (first 2000 chars): {page.inner_text('body')[:2000]}")

            if console_errors:
                report_lines.append(f"Console errors ({len(console_errors)}):")
                for err in console_errors[:10]:
                    report_lines.append(f"  - {err[:200]}")

            # Screenshot
            if screenshot:
                ss_path = Path(config.WORKSPACE) / "_screenshot.png"
                page.screenshot(path=str(ss_path), full_page=False)
                report_lines.append(f"Screenshot saved to _screenshot.png")

            browser.close()

    except Exception as e:
        report_lines.append(f"[error] Browser test failed: {e}")

    return "\n".join(report_lines)


# ---------------------------------------------------------------------------
# OpenAI function-calling schemas
# ---------------------------------------------------------------------------

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the contents of a file in the workspace.",
            "parameters": {
                "type": "object",
                "required": ["path"],
                "properties": {
                    "path": {"type": "string", "description": "Relative path inside workspace"}
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_skill_file",
            "description": "Read a skill file from the skills/ directory. Use this to load a skill's SKILL.md or any sub-files referenced within it. Path should be relative to project root (e.g. 'skills/frontend-design/SKILL.md').",
            "parameters": {
                "type": "object",
                "required": ["path"],
                "properties": {
                    "path": {"type": "string", "description": "Relative path to skill file (e.g. 'skills/frontend-design/SKILL.md')"}
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a file in the workspace.",
            "parameters": {
                "type": "object",
                "required": ["path", "content"],
                "properties": {
                    "path": {"type": "string", "description": "Relative path inside workspace"},
                    "content": {"type": "string", "description": "File content to write"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List all files in a directory recursively.",
            "parameters": {
                "type": "object",
                "properties": {
                    "directory": {
                        "type": "string",
                        "description": "Relative directory path (default: root)",
                        "default": ".",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_bash",
            "description": "Execute a shell command in the workspace directory. Use for installing deps, running builds, starting servers, running tests, etc. For long-running commands (compilation, training), increase the timeout parameter.",
            "parameters": {
                "type": "object",
                "required": ["command"],
                "properties": {
                    "command": {"type": "string", "description": "Shell command to run"},
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout in seconds (default 300). Increase for long builds/training.",
                        "default": 300,
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delegate_task",
            "description": (
                "Spawn a sub-agent in a completely isolated context to handle a subtask. "
                "The sub-agent gets a clean context window and does NOT see your conversation history. "
                "Only its structured result comes back. Use this for: "
                "(1) exploring/reading many files without bloating your context, "
                "(2) running a series of bash commands and getting a summary, "
                "(3) any 'dirty work' that would waste your context budget. "
                "The sub-agent has access to the same workspace and tools."
            ),
            "parameters": {
                "type": "object",
                "required": ["task"],
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "Detailed description of the subtask to delegate",
                    },
                    "role": {
                        "type": "string",
                        "description": "Role for the sub-agent (e.g. 'codebase_explorer', 'test_runner', 'dependency_installer')",
                        "default": "assistant",
                    },
                },
            },
        },
    },
]

# --- Evaluator-only tools (browser testing) ---

BROWSER_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "browser_test",
            "description": (
                "Launch a headless Chromium browser to test the running application. "
                "Navigates to a URL, performs UI actions (click, fill, scroll, evaluate JS), "
                "captures console errors, and takes a screenshot. "
                "Optionally starts a dev server first via start_command."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "URL to navigate to (e.g. http://localhost:5173)",
                    },
                    "actions": {
                        "type": "array",
                        "description": "List of browser actions to perform sequentially",
                        "items": {
                            "type": "object",
                            "properties": {
                                "type": {
                                    "type": "string",
                                    "enum": ["click", "fill", "wait", "evaluate", "scroll"],
                                    "description": "Action type",
                                },
                                "selector": {
                                    "type": "string",
                                    "description": "CSS selector (for click/fill)",
                                },
                                "value": {
                                    "type": "string",
                                    "description": "Text for fill, JS code for evaluate, pixels for scroll",
                                },
                                "delay": {
                                    "type": "integer",
                                    "description": "Milliseconds to wait (for wait action)",
                                },
                            },
                        },
                    },
                    "screenshot": {
                        "type": "boolean",
                        "description": "Take a screenshot after actions (default: true)",
                        "default": True,
                    },
                    "start_command": {
                        "type": "string",
                        "description": "Shell command to start the dev server (e.g. 'npm run dev'). Only needed on first call.",
                    },
                    "port": {
                        "type": "integer",
                        "description": "Port the dev server runs on (default: 5173)",
                        "default": 5173,
                    },
                    "startup_wait": {
                        "type": "integer",
                        "description": "Seconds to wait for dev server to start (default: 8)",
                        "default": 8,
                    },
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "stop_dev_server",
            "description": "Stop the background dev server started by browser_test.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

# ---------------------------------------------------------------------------
# Tool-call pre-validation & auto-correction
# ---------------------------------------------------------------------------

def _validate_and_fix(name: str, arguments: dict) -> tuple[dict, str | None]:
    """
    Pre-validate tool arguments and auto-correct common mistakes.
    Returns (fixed_arguments, warning_message_or_None).

    This is a lightweight heuristic layer — no LLM calls.
    Catches the most common tool-call errors from weaker models:
      - Empty/missing required arguments
      - Absolute paths that should be relative
      - Obvious typos in common patterns
    """
    warning = None

    if name == "write_file":
        path = arguments.get("path", "")
        content = arguments.get("content")

        # Empty path
        if not path or not path.strip():
            return arguments, "[auto-fix] Empty file path. You must specify a path."

        # Absolute path → make relative to workspace
        if path.startswith("/"):
            import re
            # Strip common workspace prefixes
            for prefix in ["/app/", "/home/user/", "/workspace/"]:
                if path.startswith(prefix):
                    arguments["path"] = path[len(prefix):]
                    warning = f"[auto-fix] Converted absolute path '{path}' to relative '{arguments['path']}'"
                    break

        # Missing content
        if content is None:
            arguments["content"] = ""
            warning = "[auto-fix] Missing 'content' argument — writing empty file."

    elif name == "read_file":
        path = arguments.get("path", "")

        # Absolute path → relative
        if path.startswith("/"):
            for prefix in ["/app/", "/home/user/", "/workspace/"]:
                if path.startswith(prefix):
                    arguments["path"] = path[len(prefix):]
                    warning = f"[auto-fix] Converted absolute path '{path}' to relative '{arguments['path']}'"
                    break

    elif name == "run_bash":
        command = arguments.get("command", "")

        # Empty command
        if not command or not command.strip():
            return arguments, "[auto-fix] Empty command. You must specify a command to run."

        # Detect interactive commands that will hang
        import re
        interactive_cmds = ["vim", "nano", "vi", "less", "more", "top", "htop"]
        first_word = command.strip().split()[0] if command.strip() else ""
        if first_word in interactive_cmds:
            return arguments, (
                f"[auto-fix] '{first_word}' is an interactive command that will hang. "
                f"Use non-interactive alternatives: "
                f"for editing use write_file, for viewing use cat/head/tail."
            )

    elif name == "list_files":
        directory = arguments.get("directory", ".")
        if directory.startswith("/"):
            for prefix in ["/app/", "/home/user/", "/workspace/"]:
                if directory.startswith(prefix):
                    arguments["directory"] = directory[len(prefix):] or "."
                    warning = f"[auto-fix] Converted absolute path '{directory}' to relative '{arguments['directory']}'"
                    break

    return arguments, warning


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

TOOL_DISPATCH = {
    "read_file": read_file,
    "read_skill_file": read_skill_file,
    "write_file": write_file,
    "list_files": list_files,
    "run_bash": run_bash,
    "delegate_task": delegate_task,
    "browser_test": browser_test,
    "stop_dev_server": stop_dev_server,
}


def execute_tool(name: str, arguments: dict) -> str:
    """Execute a tool by name with pre-validation and auto-correction."""
    fn = TOOL_DISPATCH.get(name)
    if fn is None:
        return f"[error] Unknown tool: {name}"

    # Pre-validate and auto-correct arguments
    arguments, fix_warning = _validate_and_fix(name, arguments)

    # If validation returned a blocking error (no fix possible), return it
    if fix_warning and fix_warning.startswith("[auto-fix] Empty"):
        return fix_warning
    if fix_warning and "interactive command" in fix_warning:
        return fix_warning

    try:
        result = fn(**arguments)
    except Exception as e:
        result = f"[error] {type(e).__name__}: {e}"

    # Prepend the auto-fix warning so the model knows what was corrected
    if fix_warning:
        result = f"{fix_warning}\n\n{result}"

    return result
