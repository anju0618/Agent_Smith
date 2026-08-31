"""End-to-end tests of the Thought -> Code -> Observation loop (Section 4.1)
using a fake LLM client and a real Sandbox - no network or API keys required.
"""
from typing import List, Optional

import pytest

from llm.client import AllProvidersExhaustedError
from llm.provider import GenerationResult
from models import SandboxConfig
from orchestrator import Orchestrator, OrchestratorConfig, ShutdownRequested
from sandbox.executor import Sandbox


class _ScriptedLLMClient:
    """Replays a fixed sequence of LLM responses, one per generate() call."""

    def __init__(self, texts: List[str]) -> None:
        self.texts = texts
        self.calls = 0

    def generate(
        self,
        messages: List[dict],
        stop: Optional[List[str]] = None,
        max_output_tokens: int = 1024,
    ) -> GenerationResult:
        text = self.texts[self.calls]
        self.calls += 1
        return GenerationResult(
            text=text,
            input_tokens=20,
            output_tokens=10,
            request_time_ms=5.0,
            api_url="https://fake.example/v1",
            model_name="fake-model",
        )


def _build_orchestrator(
    llm_client: _ScriptedLLMClient,
    max_iterations: int = 5,
    max_input_tokens: int = 10_000,
    max_output_tokens: int = 10_000,
    max_time_seconds: float = 30.0,
) -> Orchestrator:
    sandbox = Sandbox(
        SandboxConfig(authorized_imports=[], allowed_directories=[]),
        apply_process_memory_limit=False,
    )
    config = OrchestratorConfig(
        max_iterations=max_iterations,
        max_input_tokens=max_input_tokens,
        max_output_tokens=max_output_tokens,
        max_time_seconds=max_time_seconds,
    )
    return Orchestrator(llm_client, sandbox, "system prompt", config)  # type: ignore[arg-type]


def test_final_answer_ends_the_loop_successfully() -> None:
    llm_client = _ScriptedLLMClient(
        [
            "Thought: try\nCode:\n```python\nprint(1 + 1)\n```\n<end_code>",
            'Thought: done\nCode:\n```python\nfinal_answer("def f():\\n    return 1")\n```\n<end_code>',
        ]
    )
    orchestrator = _build_orchestrator(llm_client)
    result = orchestrator.run("1", "mbpp", "solve this task")

    assert result.success is True
    assert result.error is None
    assert result.solution == "def f():\n    return 1"
    assert result.iterations == 2
    assert result.steps[0].sandbox_output.strip() == "2"
    assert result.system_prompt == "system prompt"
    assert result.total_input_tokens == 40
    assert result.total_output_tokens == 20


def test_max_iterations_reached_without_final_answer() -> None:
    llm_client = _ScriptedLLMClient(
        ["Thought: loop\nCode:\n```python\nprint('again')\n```\n<end_code>" for _ in range(3)]
    )
    orchestrator = _build_orchestrator(llm_client, max_iterations=3)
    result = orchestrator.run("1", "mbpp", "solve this task")

    assert result.success is False
    assert result.iterations == 3
    assert result.error is not None
    assert "max iterations reached" in result.error


def test_missing_code_block_is_reported_and_loop_continues() -> None:
    llm_client = _ScriptedLLMClient(
        [
            "I am just thinking out loud with no code.",
            'Thought: ok now\nCode:\n```python\nfinal_answer("done")\n```\n<end_code>',
        ]
    )
    orchestrator = _build_orchestrator(llm_client)
    result = orchestrator.run("1", "mbpp", "solve this task")

    assert "[NoCodeBlock]" in result.steps[0].sandbox_output
    assert result.steps[0].sandbox_input == ""
    assert result.success is True
    assert result.solution == "done"


class _ShutdownOnSecondCallLLMClient:
    """Simulates a SIGTERM (ShutdownRequested) interrupting the second generate() call."""

    def __init__(self, first_response: str) -> None:
        self.first_response = first_response
        self.calls = 0

    def generate(
        self,
        messages: List[dict],
        stop: Optional[List[str]] = None,
        max_output_tokens: int = 1024,
    ) -> GenerationResult:
        self.calls += 1
        if self.calls == 1:
            return GenerationResult(
                text=self.first_response,
                input_tokens=20,
                output_tokens=10,
                request_time_ms=5.0,
                api_url="https://fake.example/v1",
                model_name="fake-model",
            )
        raise ShutdownRequested("shutdown requested (e.g. SIGTERM)")


def test_shutdown_requested_during_llm_call_preserves_partial_steps() -> None:
    llm_client = _ShutdownOnSecondCallLLMClient(
        "Thought: x\nCode:\n```python\nprint(1)\n```\n<end_code>"
    )
    orchestrator = _build_orchestrator(llm_client, max_iterations=5)  # type: ignore[arg-type]

    result = orchestrator.run("1", "mbpp", "solve this task")

    assert result.success is False
    assert result.iterations == 1  # the first step's metrics were kept, not discarded
    assert result.error is not None
    assert "shutdown requested" in result.error


def test_request_stop_raises_shutdown_requested_immediately() -> None:
    orchestrator = _build_orchestrator(_ScriptedLLMClient([]))

    with pytest.raises(ShutdownRequested):
        orchestrator.request_stop()
    assert orchestrator._stop_requested is True


class _AlwaysExhaustedLLMClient:
    def generate(
        self,
        messages: List[dict],
        stop: Optional[List[str]] = None,
        max_output_tokens: int = 1024,
    ) -> GenerationResult:
        raise AllProvidersExhaustedError("all keys exhausted", attempted_requests=4)


def test_total_requests_counts_failed_attempts_when_all_providers_exhausted() -> None:
    """Every failed HTTP attempt still counts toward SolutionOutput.total_requests
    (Section 5.1's "including retries"), even though the call never succeeds
    and so never produces a StepMetrics entry to attach it to."""
    orchestrator = _build_orchestrator(_AlwaysExhaustedLLMClient())  # type: ignore[arg-type]

    result = orchestrator.run("1", "mbpp", "solve this task")

    assert result.success is False
    assert result.total_requests == 4
    assert result.iterations == 0
    assert "LLM request failed" in (result.error or "")


def test_token_budget_stops_the_loop_before_exceeding_it() -> None:
    llm_client = _ScriptedLLMClient(
        ["Thought: x\nCode:\n```python\nprint(1)\n```\n<end_code>" for _ in range(5)]
    )
    orchestrator = _build_orchestrator(llm_client, max_input_tokens=15, max_iterations=5)
    result = orchestrator.run("1", "mbpp", "solve this task")

    assert result.success is False
    assert result.iterations == 1
    assert result.error is not None
    assert "token budget exhausted" in result.error
