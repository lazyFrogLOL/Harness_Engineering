# Harness — 多 Agent 长时间自主开发架构

[English](README_EN.md) | 中文

> 基于 Anthropic 文章 [Harness design for long-running application development](https://www.anthropic.com/engineering/harness-design-long-running-apps) 的教学复现项目。
>
> 纯 Python + OpenAI 兼容 API 实现，不依赖任何 Agent SDK。适配任何模型提供商。

## 这个项目是什么

这是一个教学项目，目标是用可运行的代码复现 Anthropic 文章中描述的每一个架构概念。你可以把它当作文章的"可执行注释"——每个设计决策都能在代码里找到对应实现。

给它一句话需求，它会自主完成：规划产品 → 协商验收标准 → 写代码 → 浏览器测试 → 打分 → 根据反馈迭代。全程零人工干预。

### 演示

以下是使用 `python harness.py "Build an interactive periodic table of elements with search, category filters, and element detail popups"` 自主生成的应用：

> 模型：Minimax M2.7 | 总耗时：32.3 分钟 | Token 用量：2.29M | 成本：$0.303

<p align="center">
  <img src="docs/demo.gif" alt="Harness Demo" width="720" />
</p>

## 快速开始

```bash
uv venv
source .venv/bin/activate
uv sync
# pip install -r requirements.txt
python -m playwright install chromium  # 可选，用于浏览器测试

cp .env.template .env
# 编辑 .env 填入你的 API key

python harness.py "Build a Pomodoro timer with start, pause, reset buttons. Single HTML file."
```

## 文章概念 → 代码对照表

这是本项目的核心价值。下表将文章中的每个架构概念映射到具体的代码位置。

### 1. 三 Agent 架构

文章描述了 Planner → Builder → Evaluator 的三 Agent 系统。

| 概念 | 文章描述 | 代码位置 |
|------|---------|---------|
| Planner | 将 1-4 句话扩展为完整产品规格 | `harness.py` L55, `prompts.py` PLANNER_SYSTEM |
| Builder | 按 spec 写代码，处理 QA 反馈 | `harness.py` L120-165, `prompts.py` BUILDER_SYSTEM |
| Evaluator | 用 Playwright 实际操作页面并打分 | `harness.py` L168-183, `prompts.py` EVALUATOR_SYSTEM |
| Agent 间通信 | 通过文件传递状态，不共享内存 | `spec.md`, `contract.md`, `feedback.md` |

```
用户: "Build a DAW"
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
        └──── 循环直到分数 ≥ 7.0 ───┘
```

### 2. 核心 Agent 循环（while loop）

文章的核心：`llm.call(prompt) → 执行工具 → 裁剪上下文 → 重复`。

```python
# agents.py — Agent.run() 方法
for iteration in range(1, MAX_AGENT_ITERATIONS + 1):
    # 1. 上下文生命周期检查（压缩或重置）
    # 2. llm.call(messages)
    # 3. 执行 tool calls
    # 4. 将结果追加到 messages
    # 5. 如果没有更多 tool calls → 结束
```

对应 `agents.py` 的 `Agent.run()` 方法（约 L78-170）。这就是文章说的"所有外层架构都是在回答：怎么让这个 while 循环跑得更久、更稳、产出更好"。

### 3. 上下文焦虑与重置

文章的关键发现：模型会"提前收工"（context anxiety），compaction 不够，必须 reset。

| 策略 | 触发条件 | 效果 | 代码位置 |
|------|---------|------|---------|
| Compaction | tokens > 80k | 摘要旧消息，保留近期 | `context.py` `compact_messages()` |
| Reset | tokens > 150k 或检测到焦虑信号 | 写 checkpoint，全新白板 | `context.py` `create_checkpoint()` + `restore_from_checkpoint()` |
| 焦虑检测 | 模型说"let me wrap up"等 | 触发 reset | `context.py` `detect_anxiety()` |

```python
# context.py — 焦虑信号检测
_ANXIETY_PATTERNS = [
    r"(?i)let me wrap up",
    r"(?i)running (low on|out of) (context|space|tokens)",
    r"(?i)that should be (enough|sufficient)",
    ...
]
```

```python
# agents.py — 生命周期检查（每轮迭代开头）
if token_count > RESET_THRESHOLD or detect_anxiety(messages):
    checkpoint = create_checkpoint(messages, llm_call)      # 写交接文档
    messages = restore_from_checkpoint(checkpoint, prompt)   # 全新白板
elif token_count > COMPRESS_THRESHOLD:
    messages = compact_messages(messages, llm_call, role)    # 摘要压缩
```

### 4. 角色差异化的压缩策略

文章提到不同 Agent 需要不同的上下文管理策略。

| 角色 | 保留比例 | 摘要重点 | 原因 |
|------|---------|---------|------|
| Evaluator | 50% | 保留所有评分和 bug 记录 | 需要跨轮对比质量趋势 |
| Builder | 20% | 只留架构决策和最新错误 | 旧的调试过程没用 |
| Default | 30% | 平衡保留 | 通用策略 |

代码在 `context.py` 的 `compact_messages()` 函数，通过 `role` 参数区分。

### 5. Sprint Contract 协商

文章描述：Builder 和 Evaluator 在每轮开始前协商"done 长什么样"。

```
Builder 提出 contract → Evaluator 审核 → 不通过则修改 → 最多 3 轮 → 写入 contract.md
```

代码在 `harness.py` 的 `_negotiate_contract()` 方法。使用两个轻量 Agent：
- `contract_proposer`（`prompts.py` CONTRACT_BUILDER_SYSTEM）
- `contract_reviewer`（`prompts.py` CONTRACT_REVIEWER_SYSTEM）

### 6. REFINE vs PIVOT 策略决策

文章描述：Builder 根据分数趋势决定是打磨还是推翻重来。

```python
# harness.py — 构建分数趋势上下文
if len(score_history) >= 2:
    delta = score_history[-1] - score_history[-2]
    # IMPROVING → REFINE
    # STAGNANT or DECLINING → PIVOT
```

Builder 在每轮开始前收到完整的分数历史和趋势分析，被要求在写代码之前先声明 REFINE 还是 PIVOT。

### 7. Sub-Agent 上下文隔离

文章描述：父 Agent 可以 spawn sub-agent 做脏活，只拿回结构化结果。

```python
# tools.py — delegate_task()
def delegate_task(task, role="assistant"):
    sub = Agent(name=f"sub_{role}", ...)  # 全新的上下文窗口
    result = sub.run(task)                 # 独立的 while 循环
    return result[:8000]                   # 只返回摘要，内部推理不可见
```

Builder 可以调用 `delegate_task` 把探索代码库、跑测试等任务委派出去，自己的上下文保持干净。

### 8. Skill 渐进式披露（Progressive Disclosure）

基于 Anthropic 的 [Agent Skills](https://claude.com/blog/equipping-agents-for-the-real-world-with-agent-skills) 设计，三层结构：

| 层级 | 内容 | 谁决定加载 | 代码位置 |
|------|------|-----------|---------|
| Level 1 | name + description 注入 system prompt | 自动（启动时） | `skills.py` `build_catalog_prompt()` |
| Level 2 | Agent 读取 SKILL.md 全文 | Agent 自主决定 | Agent 调用 `read_skill_file()` |
| Level 3 | SKILL.md 引用的子文件 | Agent 按需读取 | Agent 再次调用 `read_skill_file()` |

```
启动时 system prompt 里只有:
  "frontend-design: Create distinctive, production-grade frontend interfaces..."
  Path: skills/frontend-design/SKILL.md

Agent 判断需要 → 自己调用 read_skill_file("skills/frontend-design/SKILL.md")
SKILL.md 里提到 reference.md → Agent 按需再读
```

关键：是 Agent 自己决定何时加载 skill，不是外部代码替它决定。

### 9. Evaluator 的 Playwright 浏览器测试

文章描述：Evaluator 用 Playwright 实际操作页面，而不是只看代码。

```python
# tools.py — browser_test()
# 启动 dev server → 打开 headless Chromium → 导航 → 点击/填表/执行 JS → 截图
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

### 10. 评估标准（来自文章）

| 维度 | 权重 | 含义 |
|------|------|------|
| Design Quality | HIGH | 视觉是否有统一身份感，还是拼凑的模板 |
| Originality | HIGH | 有没有自定义设计决策，还是 AI 默认审美（紫色渐变+白卡片） |
| Craft | MEDIUM | 技术执行：排版层次、间距一致性、色彩和谐 |
| Functionality | HIGH | 功能是否真的能用，每个按钮都要点 |

定义在 `prompts.py` 的 EVALUATOR_SYSTEM 中。

## 项目结构

```
├── harness.py        # 入口 + 外层编排循环（Plan → Contract → Build → Evaluate）
├── agents.py         # 核心 Agent while 循环（llm.call → tool → context check）
├── context.py        # 上下文生命周期（压缩 / 焦虑检测 / 重置 / checkpoint）
├── tools.py          # 工具实现 + OpenAI function schemas
│                       read_file, write_file, list_files, run_bash,
│                       delegate_task, browser_test, read_skill_file
├── prompts.py        # 所有 Agent 的 system prompt 和评估标准
├── skills.py         # Skill 注册表（渐进式披露 Level 1）
├── skills/           # Skill 目录
│   └── frontend-design/
│       └── SKILL.md  # Anthropic 官方 frontend design skill
├── config.py         # 配置（自动加载 .env）
├── .env.template     # 环境变量模板
├── requirements.txt  # Python 依赖
└── workspace/        # 生成的项目输出（每次运行创建子目录）
```

## 配置

```bash
cp .env.template .env
```

```bash
# API — 任何 OpenAI 兼容端点
OPENAI_API_KEY=sk-your-key-here
OPENAI_BASE_URL=https://api.openai.com/v1
HARNESS_MODEL=gpt-4o

# 工作目录
HARNESS_WORKSPACE=./workspace

# 调参（可选）
MAX_HARNESS_ROUNDS=5        # 最大 build→evaluate 轮数
PASS_THRESHOLD=7.0          # QA 通过分数（满分 10）
COMPRESS_THRESHOLD=80000    # 触发上下文压缩的 token 数
RESET_THRESHOLD=150000      # 触发上下文重置的 token 数
MAX_AGENT_ITERATIONS=60     # 每个 Agent 最大工具调用轮数
```

### 多 Provider 示例

```bash
# OpenAI
OPENAI_BASE_URL=https://api.openai.com/v1
HARNESS_MODEL=gpt-4o

# OpenRouter
OPENAI_BASE_URL=https://openrouter.ai/api/v1
HARNESS_MODEL=anthropic/claude-sonnet-4

# Ollama（本地）
OPENAI_BASE_URL=http://localhost:11434/v1
HARNESS_MODEL=qwen2.5-coder:32b
```

## 示例需求

```bash
# 文章原文的两个案例
python harness.py "Build a fully featured DAW in the browser using the Web Audio API"
python harness.py "Create a 2D retro game maker with level editor, sprite editor, and playable test mode"

# 轻量测试
python harness.py "Build a Pomodoro timer with start, pause, reset. Single HTML file."

# 视觉密集型（测试 frontend-design skill）
python harness.py "Build an interactive periodic table with search, filters, and element detail popups"
```

## 文章中尚未实现的部分

以下概念在文章中有描述，本项目有意未实现。列出原因和实现思路，供学习者作为练习方向。

### 1. Evaluator few-shot 校准

文章描述：作者用 few-shot examples + detailed score breakdowns 来校准 Evaluator 的打分标准，确保评分一致性，防止分数漂移和过度宽容。

为什么没实现：需要人工标注一组"这个设计值 3 分，那个值 8 分"的参考样例，属于调优工作而非架构设计。

实现思路：
```python
# 在 prompts.py 的 EVALUATOR_SYSTEM 中追加 few-shot 样例
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

### 2. 每轮 token 成本追踪

文章描述：文章中有详细的 per-agent duration + cost 表格（Planner 4.7min/$0.46, Build 2hr/$71.08）。

为什么没实现：不同 provider（OpenAI、OpenRouter、Ollama）的计费方式和 API 返回的 usage 字段格式不同，难以通用。

实现思路：
```python
# 在 agents.py 的 Agent.run() 中，每次 API 调用后累计 usage
response = client.chat.completions.create(**kwargs)
if response.usage:
    self.total_prompt_tokens += response.usage.prompt_tokens
    self.total_completion_tokens += response.usage.completion_tokens

# 在 harness.py 中，每个阶段结束后打印成本
# 需要一个 price_per_token 配置项，按模型不同设置
cost = (prompt_tokens * input_price + completion_tokens * output_price) / 1_000_000
log.info(f"Build round {round_num}: {duration:.0f}s, ${cost:.2f}")
```

### 3. Git 回滚保护

文章描述：Builder 有 git 做版本控制，可以在评估失败或 PIVOT 时回滚到上一个好的状态。

为什么没实现：当前 Builder 已经在用 git commit，但 Harness 层面没有利用它做自动回滚。这是锦上添花，不影响核心流程。

实现思路：
```python
# 在 harness.py 的 build 阶段前后加 git tag
def run(self, ...):
    for round_num in range(...):
        # Build 前打 tag
        os.system(f"cd {config.WORKSPACE} && git tag round-{round_num}-start")

        self.builder.run(build_task)

        # Build 后打 tag
        os.system(f"cd {config.WORKSPACE} && git tag round-{round_num}-end")

        # 如果 PIVOT，回滚到上一轮的 end tag
        if score_declining and strategy == "PIVOT":
            os.system(f"cd {config.WORKSPACE} && git reset --hard round-{round_num-1}-end")
```

### 4. 组件开关（Harness 简化）

文章描述：每个 harness 组件都编码了"模型做不到什么"的假设。新模型出来后应该逐个移除不再需要的组件，只保留仍然 load-bearing 的部分。

为什么没实现：这是运维/迭代层面的实践，不是一次性架构设计。

实现思路：
```bash
# 通过环境变量控制各组件的启用/禁用
ENABLE_CONTRACT=true        # 跳过 sprint contract 协商
ENABLE_PLAYWRIGHT=true      # 跳过浏览器测试，只做代码审查
ENABLE_PLANNER=true         # 跳过 planner，直接用用户 prompt 作为 spec
ENABLE_ANXIETY_DETECTION=true  # 关闭焦虑检测（如果模型不再需要）
```

```python
# harness.py
if config.ENABLE_CONTRACT:
    self._negotiate_contract(round_num)

if config.ENABLE_PLANNER:
    self.planner.run(...)
else:
    # 直接把用户 prompt 写入 spec.md
    write_file("spec.md", user_prompt)
```

### 5. Evaluator 自校准循环

文章描述：作者花了多轮手动读 Evaluator 的 log，找到判断偏差的地方，更新 prompt。这是一个 meta 层面的调优过程。

为什么没实现：这本质上是"人在循环里"的迭代过程，不是可以自动化的架构组件。

实现思路（半自动化）：
```python
# 在 harness.py 结束后，生成一份 Evaluator 审计报告
def _audit_evaluator(self):
    """检查 Evaluator 是否过于宽容"""
    scores = self.score_history
    if all(s >= 8.0 for s in scores) and len(scores) >= 3:
        log.warning(
            "Evaluator may be too lenient — all scores >= 8.0. "
            "Consider adding few-shot calibration examples."
        )
    if scores and scores[0] >= 7.0:
        log.warning(
            "First round already passed — Evaluator may not be pushing hard enough."
        )
```

## 扩展 Skill

在 `skills/` 下创建新目录，放入 `SKILL.md`：

```
skills/
  my-new-skill/
    SKILL.md          ← 必须有 YAML frontmatter (name, description)
    reference.md      ← 可选，SKILL.md 里引用
    helper.py         ← 可选，Agent 可以执行的脚本
```

SKILL.md 格式：

```markdown
---
name: my-new-skill
description: 一句话描述，Agent 根据这个决定是否加载
---

详细指导内容...
```

Agent 会在 system prompt 里看到 name + description，自己决定是否读取完整内容。

## 依赖

- Python 3.10+
- `openai` — API 客户端
- `tiktoken` — token 计数
- `playwright`（可选）— 浏览器测试

## License

MIT
