from typing import Optional

from orchestrator import (
    END_CODE,
    FinalAnswerReached,
    build_system_prompt,
    run_agent_loop,
)


class FakeLLM:
    """Returns canned outputs in order; records the `stop` sequences it was called with."""

    def __init__(self, outputs: list[str]):
        self._outputs = outputs
        self.calls: list[list[str]] = []

    def generate(self, messages: list[dict[str, str]], stop: list[str]) -> str:
        self.calls.append(stop)
        return self._outputs[len(self.calls) - 1]


class FakeCodeExtractor:
    """Extracts everything after 'CODE:' in the fake LLM output, or None if absent."""

    def extract(self, llm_output: str) -> Optional[str]:
        if "CODE:" not in llm_output:
            return None
        return llm_output.split("CODE:", 1)[1].strip()


class FakeSandbox:
    """Echoes the code back as the observation, unless it starts with 'final_answer('."""

    def execute(self, code: str) -> str:
        if code.startswith("final_answer("):
            answer = code[len("final_answer("):-1]
            raise FinalAnswerReached(answer)
        return f"ran: {code}"


def test_system_prompt_documents_the_end_code_marker() -> None:
    prompt = build_system_prompt()
    assert END_CODE in prompt
    assert "Thought" in prompt and "Observation" in prompt


def test_loop_passes_end_code_as_stop_sequence() -> None:
    llm = FakeLLM(["CODE: final_answer(1)"])
    result = run_agent_loop("task", llm, FakeCodeExtractor(), FakeSandbox())
    assert llm.calls == [[END_CODE]]
    assert result.success is True


def test_loop_runs_several_iterations_then_returns_final_answer() -> None:
    llm = FakeLLM(
        [
            "CODE: x = 1",
            "CODE: x = 2",
            "CODE: final_answer(42)",
        ]
    )
    result = run_agent_loop("task", llm, FakeCodeExtractor(), FakeSandbox())

    assert result.success is True
    assert result.answer == "42"
    assert result.iterations == 3
    # system + user + 2 full (assistant + observation) rounds + the final assistant
    # turn that reached final_answer (no observation follows it, the loop returns).
    assert len(result.transcript) == 2 + 2 * 2 + 1


def test_loop_gives_feedback_when_no_code_block_found() -> None:
    llm = FakeLLM(["no code here", "CODE: final_answer(1)"])
    result = run_agent_loop("task", llm, FakeCodeExtractor(), FakeSandbox())

    assert result.success is True
    first_observation = result.transcript[3]
    assert first_observation["role"] == "user"
    assert "no code block found" in first_observation["content"]


def test_loop_stops_at_max_iterations_without_final_answer() -> None:
    llm = FakeLLM(["CODE: x = 1"] * 3)
    result = run_agent_loop(
        "task", llm, FakeCodeExtractor(), FakeSandbox(), max_iterations=3
    )

    assert result.success is False
    assert result.iterations == 3
    assert result.error is not None
