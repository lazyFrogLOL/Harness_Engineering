"""
Reasoning profile — for knowledge-intensive QA tasks (MMMU-Pro style).
Analyze question → research/compute → draft answer → verify → iterate.

Note: MMMU-Pro involves images which require vision-capable models.
This profile handles the text reasoning flow; image support depends
on the model's multimodal capabilities.
"""
from __future__ import annotations

from profiles.base import BaseProfile, AgentConfig


class ReasoningProfile(BaseProfile):

    def name(self) -> str:
        return "reasoning"

    def description(self) -> str:
        return "Solve knowledge-intensive reasoning tasks (MMMU-Pro style)"

    def planner(self) -> AgentConfig:
        return AgentConfig(
            system_prompt="""\
You are an expert analyst. Given a question or problem, create a \
structured approach to solve it.

Workflow:
1. Identify the domain (math, science, engineering, etc.).
2. Break the problem into sub-problems.
3. For each sub-problem, note what knowledge or computation is needed.
4. Write the approach to spec.md.

If computation is needed, note the specific formulas or algorithms.
Do NOT solve the problem yet. Only write the approach to spec.md.
""",
        )

    def builder(self) -> AgentConfig:
        return AgentConfig(
            system_prompt="""\
You are an expert problem solver. Your job is to work through a \
problem step by step and produce a final answer.

Workflow:
1. Read spec.md for the approach.
2. If feedback.md exists, read it — your previous answer may have errors.
3. Work through each step:
   - For calculations, use run_bash with Python: python3 -c "..."
   - For research, reason from your knowledge.
   - Show all work explicitly.
4. Write your complete solution and final answer to answer.md.

Use write_file to save your solution to answer.md.
The final answer must be clearly marked: **ANSWER: [your answer]**
""",
        )

    def evaluator(self) -> AgentConfig:
        return AgentConfig(
            system_prompt="""\
You are a strict grader verifying a solution to an academic problem.

Process:
1. Read spec.md for the problem breakdown.
2. Read answer.md for the submitted solution.
3. Verify each step independently:
   - Re-do key calculations with run_bash: python3 -c "..."
   - Check logical reasoning for gaps.
   - Verify the final answer.
4. Score:
   - Reasoning: Is the approach sound? (1-10)
   - Accuracy: Is the final answer correct? (1-10)
   - Completeness: Are all parts addressed? (1-10)

Output format — write to feedback.md:
```
## Grading

### Scores
- Reasoning: X/10 — [justification]
- Accuracy: X/10 — [justification]
- Completeness: X/10 — [justification]
- **Average: X/10**

### Errors Found
1. [Error description]

### Correct Steps
- [Observations]
```

Use write_file to save to feedback.md.
""",
        )

    def pass_threshold(self) -> float:
        return 9.0  # Academic tasks need high accuracy

    def max_rounds(self) -> int:
        return 3

    def extract_score(self, feedback_text: str) -> float:
        """Same as base — looks for Average: X/10."""
        return super().extract_score(feedback_text)
