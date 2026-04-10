# Harness — Multi-Agent Architecture for Long-Running Autonomous Development

English | [中文](README.md)

> An educational reproduction of Anthropic's [Harness design for long-running application development](https://www.anthropic.com/engineering/harness-design-long-running-apps).
>
> Pure Python + OpenAI-compatible API. No proprietary Agent SDK. Works with any model provider.

## What Is This

An educational project that reproduces every architectural concept from the Anthropic article in runnable code. Think of it as "executable annotations" for the article — every design decision maps to a concrete implementation.

Give it a one-sentence prompt, and it autonomously: plans the product → negotiates acceptance criteria → writes code → browser-tests it → scores it → iterates on feedback. Zero human intervention.

### Demo

The following app was autonomously generated using `python harness.py "Build an interactive periodic table of elements with search, category filters, and element detail popups"`:

> Model: Minimax M2.7 | Total time: 32.3 min | Tokens: 2.29M | Cost: $0.303

<p align="center">
  <img src="docs/demo.gif" alt="Harness Demo" width="720" />
</p>

## Quick Start

```bash
uv venv
source .venv/bin/activate
uv sync
# pip install -r requirements.txt
python -m playwright install chromium  # optional, for browser testing

cp .env.template .env
# Edit .env with your API key

python harness.py "Build a Pomodoro timer with start, pause, reset buttons. Single HTML file."
```

## Article Concepts → Code Mapping

This is the core value of the project. The table below maps each architectural concept from the article to its exact code location.

### 1. Three-Agent Architecture

The article describes a Planner → Builder → Evaluator system.

| Concept | Article Description | Code Location |
|---------|-------------------|---------------|
| Planner | Expands 1-4 sentences into a full product spec | `harness.py` L55, `prompts.py` PLANNER_SYSTEM |
| Builder | Builds code from spec, addresses QA feedback | `harness.py` L120-165, `prompts.py` BUILDER_SYSTEM |
| Evaluator | Tests the app with Playwright, scores on 4 criteria | `harness.py` L168-183, `prompts.py` EVALUATOR_SYSTEM |
| Inter-agent communication | State passed via files, no shared memory | `spec.md`, `contract.md`, `feedback.md` |

```
User: "Build a DAW"
        │
        ▼
   ┌─────────┐
   │ Planner │ → spec.md
   └────┬────┘
        ▼
   ┌─────────┐  contract.md  ┌───────────┐
   │ Builder │◄─────────────►│ Evaluator │
   └─────────┘  feedback.md  └───────────┘
        │                          │
        └──── loop until score ≥ 7.0 ──┘
```

### 2. Core Agent Loop (the while loop)

The heart of the article: `llm.call(prompt) → execute tools → trim context → repeat`.

```python
# agents.py — Agent.run()
for iteration in range(1, MAX_AGENT_ITERATIONS + 1):
    # 1. Context lifecycle check (compact or reset)
    # 2. llm.call(messages)
    # 3. Execute tool calls
    # 4. Append results to messages
    # 5. If no more tool calls → done
```

Located in `agents.py`, `Agent.run()` (~L78-170). As the article puts it: "all outer architecture answers one question — how to make this while loop run longer, more stably, with better output."

### 3. Context Anxiety & Reset

Key finding from the article: models "wrap up prematurely" (context anxiety). Compaction isn't enough — a full reset is required.

| Strategy | Trigger | Effect | Code Location |
|----------|---------|--------|---------------|
| Compaction | tokens > 80k | Summarize old messages, keep recent | `context.py` `compact_messages()` |
| Reset | tokens > 150k or anxiety detected | Write checkpoint, fresh slate | `context.py` `create_checkpoint()` + `restore_from_checkpoint()` |
| Anxiety detection | Model says "let me wrap up" etc. | Triggers reset | `context.py` `detect_anxiety()` |

```python
# context.py — anxiety signal detection
_ANXIETY_PATTERNS = [
    r"(?i)let me wrap up",
    r"(?i)running (low on|out of) (context|space|tokens)",
    r"(?i)that should be (enough|sufficient)",
    ...
]
```

```python
# agents.py — lifecycle check (start of each iteration)
if token_count > RESET_THRESHOLD or detect_anxiety(messages):
    checkpoint = create_checkpoint(messages, llm_call)      # write handoff doc
    messages = restore_from_checkpoint(checkpoint, prompt)   # fresh slate
elif token_count > COMPRESS_THRESHOLD:
    messages = compact_messages(messages, llm_call, role)    # summarize
```

### 4. Role-Specific Compaction Strategies

The article notes that different agents need different context management.

| Role | Retention | Summary Focus | Reason |
|------|-----------|---------------|--------|
| Evaluator | 50% | Preserve all scores and bug records | Needs cross-round quality trend comparison |
| Builder | 20% | Keep only architecture decisions and latest errors | Old debugging steps are useless |
| Default | 30% | Balanced retention | General purpose |

Code in `context.py` `compact_messages()`, differentiated by the `role` parameter.

### 5. Sprint Contract Negotiation

Article: Builder and Evaluator agree on what "done" looks like before each round.

```
Builder proposes contract → Evaluator reviews → revise if needed → max 3 rounds → saved to contract.md
```

Code in `harness.py` `_negotiate_contract()`. Uses two lightweight agents:
- `contract_proposer` (`prompts.py` CONTRACT_BUILDER_SYSTEM)
- `contract_reviewer` (`prompts.py` CONTRACT_REVIEWER_SYSTEM)

### 6. REFINE vs PIVOT Strategy

Article: Builder decides whether to polish or start over based on score trends.

```python
# harness.py — build score trend context
if len(score_history) >= 2:
    delta = score_history[-1] - score_history[-2]
    # IMPROVING → REFINE
    # STAGNANT or DECLINING → PIVOT
```

Builder receives the full score history and trend analysis before each round, and must declare REFINE or PIVOT before writing code.

### 7. Sub-Agent Context Isolation

Article: parent agent can spawn sub-agents for dirty work, only getting back structured results.

```python
# tools.py — delegate_task()
def delegate_task(task, role="assistant"):
    sub = Agent(name=f"sub_{role}", ...)  # clean context window
    result = sub.run(task)                 # independent while loop
    return result[:8000]                   # only summary returned, internals invisible
```

Builder can call `delegate_task` to offload codebase exploration, test runs, etc. Its own context stays clean.

### 8. Skill Progressive Disclosure

Based on Anthropic's [Agent Skills](https://claude.com/blog/equipping-agents-for-the-real-world-with-agent-skills) design, three-level structure:

| Level | Content | Who decides to load | Code Location |
|-------|---------|-------------------|---------------|
| Level 1 | name + description injected into system prompt | Automatic (at startup) | `skills.py` `build_catalog_prompt()` |
| Level 2 | Agent reads full SKILL.md | Agent decides autonomously | Agent calls `read_skill_file()` |
| Level 3 | Sub-files referenced by SKILL.md | Agent reads on demand | Agent calls `read_skill_file()` again |

Key: the agent decides when to load a skill, not external code.

### 9. Evaluator Playwright Browser Testing

Article: Evaluator interacts with the live page via Playwright, not just reading code.

```python
# tools.py — browser_test()
browser_test(
    url="http://localhost:5173",
    start_command="npm run dev",
    actions=[
        {"type": "click", "selector": "#start-btn"},
        {"type": "fill", "selector": "#search", "value": "test"},
        {"type": "evaluate", "value": "document.querySelectorAll('.item').length"},
    ]
)
```

### 10. Evaluation Criteria (from the article)

| Criterion | Weight | Meaning |
|-----------|--------|---------|
| Design Quality | HIGH | Coherent visual identity vs. patchwork template |
| Originality | HIGH | Custom design decisions vs. AI defaults (purple gradients, white cards) |
| Craft | MEDIUM | Technical execution: typography hierarchy, spacing, color harmony |
| Functionality | HIGH | Does it actually work? Every button must be clicked |

Defined in `prompts.py` EVALUATOR_SYSTEM.

## Project Structure

```
├── harness.py        # Entry point + outer orchestration loop (Plan → Contract → Build → Evaluate)
├── agents.py         # Core agent while loop (llm.call → tool → context check)
├── context.py        # Context lifecycle (compaction / anxiety detection / reset / checkpoint)
├── tools.py          # Tool implementations + OpenAI function schemas
│                       read_file, write_file, list_files, run_bash,
│                       delegate_task, browser_test, read_skill_file
├── prompts.py        # All agent system prompts and evaluation criteria
├── skills.py         # Skill registry (progressive disclosure Level 1)
├── skills/           # Skill directory
│   └── frontend-design/
│       └── SKILL.md  # Anthropic's official frontend design skill
├── config.py         # Configuration (auto-loads .env)
├── .env.template     # Environment variable template
├── requirements.txt  # Python dependencies
└── workspace/        # Generated project output (subdirectory per run)
```

## Configuration

```bash
cp .env.template .env
```

```bash
# API — any OpenAI-compatible endpoint
OPENAI_API_KEY=sk-your-key-here
OPENAI_BASE_URL=https://api.openai.com/v1
HARNESS_MODEL=gpt-4o

# Workspace
HARNESS_WORKSPACE=./workspace

# Tuning (optional)
MAX_HARNESS_ROUNDS=5        # max build→evaluate rounds
PASS_THRESHOLD=7.0          # QA pass score (out of 10)
COMPRESS_THRESHOLD=80000    # token count to trigger compaction
RESET_THRESHOLD=150000      # token count to trigger full reset
MAX_AGENT_ITERATIONS=60     # max tool calls per agent run
```

### Provider Examples

```bash
# OpenAI
OPENAI_BASE_URL=https://api.openai.com/v1
HARNESS_MODEL=gpt-4o

# OpenRouter
OPENAI_BASE_URL=https://openrouter.ai/api/v1
HARNESS_MODEL=anthropic/claude-sonnet-4

# Ollama (local)
OPENAI_BASE_URL=http://localhost:11434/v1
HARNESS_MODEL=qwen2.5-coder:32b
```

## Example Prompts

```bash
# From the original article
python harness.py "Build a fully featured DAW in the browser using the Web Audio API"
python harness.py "Create a 2D retro game maker with level editor, sprite editor, and playable test mode"

# Lightweight test
python harness.py "Build a Pomodoro timer with start, pause, reset. Single HTML file."

# Visually dense (tests frontend-design skill)
python harness.py "Build an interactive periodic table with search, filters, and element detail popups"
```

## Not Yet Implemented

The following concepts are described in the article but intentionally left unimplemented. Reasons and implementation sketches are provided as exercise directions for learners.

### 1. Evaluator Few-Shot Calibration

Article: The author used few-shot examples with detailed score breakdowns to calibrate the evaluator's grading, ensuring consistency and preventing score drift.

Why not implemented: Requires manually annotated reference examples ("this design is a 3, that one is an 8"). This is tuning work, not architecture.

Implementation sketch:
```python
# Append to EVALUATOR_SYSTEM in prompts.py
"""
### Scoring Reference Examples

**Design Quality 3/10 example:**
A white page with a centered card, purple gradient header, Inter font,
default shadcn components. No visual identity. This is AI slop.

**Design Quality 8/10 example:**
A dark theme with custom grain texture overlay, asymmetric layout,
Playfair Display headings paired with DM Sans body text, deliberate
use of negative space, accent color derived from content context.
"""
```

### 2. Per-Round Token Cost Tracking

Article: Detailed per-agent duration + cost tables (Planner 4.7min/$0.46, Build 2hr/$71.08).

Why not implemented: Different providers (OpenAI, OpenRouter, Ollama) have different billing and usage field formats.

Implementation sketch:
```python
# In agents.py Agent.run(), accumulate usage after each API call
response = client.chat.completions.create(**kwargs)
if response.usage:
    self.total_prompt_tokens += response.usage.prompt_tokens
    self.total_completion_tokens += response.usage.completion_tokens

cost = (prompt_tokens * input_price + completion_tokens * output_price) / 1_000_000
log.info(f"Build round {round_num}: {duration:.0f}s, ${cost:.2f}")
```

### 3. Git Rollback Protection

Article: Builder uses git for version control, enabling rollback on evaluation failure or PIVOT.

Why not implemented: Builder already uses git commit, but the Harness doesn't leverage it for automatic rollback. Nice-to-have, not core.

Implementation sketch:
```python
# In harness.py, tag before/after each build
os.system(f"cd {config.WORKSPACE} && git tag round-{round_num}-start")
self.builder.run(build_task)
os.system(f"cd {config.WORKSPACE} && git tag round-{round_num}-end")

# On PIVOT, rollback
if strategy == "PIVOT":
    os.system(f"cd {config.WORKSPACE} && git reset --hard round-{round_num-1}-end")
```

### 4. Component Toggles (Harness Simplification)

Article: Every harness component encodes an assumption about what the model can't do. When a new model ships, strip away components that are no longer load-bearing.

Why not implemented: This is an operational/iteration practice, not a one-time architecture decision.

Implementation sketch:
```bash
ENABLE_CONTRACT=true
ENABLE_PLAYWRIGHT=true
ENABLE_PLANNER=true
ENABLE_ANXIETY_DETECTION=true
```

### 5. Evaluator Self-Calibration Loop

Article: The author spent multiple rounds reading evaluator logs, finding judgment gaps, and updating prompts.

Why not implemented: This is fundamentally a human-in-the-loop process, not an automatable component.

Implementation sketch (semi-automated):
```python
def _audit_evaluator(self):
    scores = self.score_history
    if all(s >= 8.0 for s in scores) and len(scores) >= 3:
        log.warning("Evaluator may be too lenient — all scores >= 8.0.")
    if scores and scores[0] >= 7.0:
        log.warning("First round already passed — Evaluator may not be pushing hard enough.")
```

## Extending Skills

Create a new directory under `skills/` with a `SKILL.md`:

```
skills/
  my-new-skill/
    SKILL.md          ← must have YAML frontmatter (name, description)
    reference.md      ← optional, referenced from SKILL.md
    helper.py         ← optional, executable script for the agent
```

SKILL.md format:

```markdown
---
name: my-new-skill
description: One-line description. The agent uses this to decide whether to load it.
---

Detailed guidance content...
```

The agent sees name + description in its system prompt and decides whether to read the full content.

## Dependencies

- Python 3.10+
- `openai` — API client
- `tiktoken` — token counting
- `playwright` (optional) — browser testing

## License

MIT
