"""The Agent/Orchestrator loop (TASKS.md section 1): Thought -> Code -> Observation.

CodeExtractor (section 2) and Sandbox (section 3) are defined here only as Protocols —
their real implementations land in later steps. This module wires the loop and the
system prompt against those interfaces so it can be tested independently with fakes.
"""
from dataclasses import dataclass, field
from typing import Optional, Protocol

from llm_client import LLMClient

END_CODE = "<end_code>"


class FinalAnswerReached(Exception):
    """Raised by a Sandbox when the agent's code called final_answer(...).

    Caught by run_agent_loop to end the loop — see memo.md 2.4 for why an exception
    (rather than a sentinel return value) is the natural way to unwind an in-progress
    sandbox execution to the orchestrator.
    """

    def __init__(self, answer: str):
        self.answer = answer


class CodeExtractor(Protocol):
    def extract(self, llm_output: str) -> Optional[str]:
        """Returns the Python code to run, or None if `llm_output` has no code block."""
        ...


class Sandbox(Protocol):
    def execute(self, code: str) -> str:
        """Runs `code` and returns the Observation text shown back to the LLM.

        Raises FinalAnswerReached if the code called final_answer(...).
        """
        ...


@dataclass
class AgentResult:
    success: bool
    answer: Optional[str]
    iterations: int
    transcript: list[dict[str, str]] = field(default_factory=list)
    error: Optional[str] = None


def build_system_prompt(tool_manual: str = "") -> str:
    """Builds the system prompt: role, available tools, and a worked Thought/Code/Observation
    example so the LLM learns the expected turn format and the `<end_code>` stop sequence.
    """
    return f"""You solve tasks by reasoning in a loop of Thought, Code, and Observation.

At each turn, write:
1. Thought: a short explanation of what you are about to do and why.
2. Code: a single Python code block, fenced as ```python ... ```, immediately followed
   by `{END_CODE}` on its own line. The code runs in a sandbox; anything it prints, or
   any error it raises, is returned to you as the next Observation.

When you know the final answer, call `final_answer(answer)` from inside a Code block
instead of printing it — this ends the task.

Available tools:
{tool_manual or "(none registered yet)"}

Example:
Thought: I need to check whether 17 is prime before using it.
Code:
```python
n = 17
is_prime = all(n % i != 0 for i in range(2, n))
print(is_prime)
```
{END_CODE}
Observation:
True

Thought: 17 is prime, so I can return it as the answer.
Code:
```python
final_answer(17)
```
{END_CODE}
"""


def run_agent_loop(
    task_description: str,
    llm: LLMClient,
    code_extractor: CodeExtractor,
    sandbox: Sandbox,
    max_iterations: int = 10,
    tool_manual: str = "",
) -> AgentResult:
    messages: list[dict[str, str]] = [
        {"role": "system", "content": build_system_prompt(tool_manual)},
        {"role": "user", "content": task_description},
    ]

    for iteration in range(1, max_iterations + 1):
        llm_output = llm.generate(messages, stop=[END_CODE])
        messages.append({"role": "assistant", "content": llm_output})

        code = code_extractor.extract(llm_output)
        if code is None:
            observation = "Error: no code block found in your response. " \
                "Reply with a ```python ... ``` block ending in " + END_CODE + "."
        else:
            try:
                observation = sandbox.execute(code)
            except FinalAnswerReached as final:
                return AgentResult(
                    success=True,
                    answer=final.answer,
                    iterations=iteration,
                    transcript=messages,
                )

        messages.append({"role": "user", "content": f"Observation:\n{observation}"})

    return AgentResult(
        success=False,
        answer=None,
        iterations=max_iterations,
        transcript=messages,
        error="max_iterations reached without a final_answer",
    )
