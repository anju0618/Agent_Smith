"""Agent/Orchestrator: the Thought -> Code -> Observation loop (Section 4.1).

Shared verbatim between agent_mbpp.py and agent_swebench.py - only the system
prompt, sandbox configuration, connected MCP server, and how final_answer's
argument becomes SolutionOutput.solution differ between the two benchmarks.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

from code_extraction import extract_code
from llm.client import AllProvidersExhaustedError, LLMClient
from models import SolutionOutput, StepMetrics
from sandbox.executor import FinalAnswer, Sandbox


class ShutdownRequested(BaseException):
    """Raised by request_stop() at the exact point a SIGTERM was delivered.

    Deliberately a BaseException, not an Exception: Sandbox.run()'s generic
    ``except Exception`` (and requests/urllib3's own internal error handling)
    must never swallow it, or a SIGTERM arriving mid-LLM-call/mid-sandbox-exec
    would silently do nothing until that call finishes on its own - which can
    take longer than the ~10s grace period external harnesses (e.g.
    moulinette's run-agent) give between SIGTERM and SIGKILL, causing SIGKILL
    to hit first and skip agent_swebench.py's `finally: container.cleanup()`.
    Raising immediately, in the signal handler itself, interrupts a blocked
    call right away (the same technique - and the same reason - as
    sandbox/executor.py's own SIGALRM handler raising SandboxTimeoutError).
    """


@dataclass
class OrchestratorConfig:
    max_iterations: int
    max_input_tokens: int
    max_output_tokens: int
    max_time_seconds: float
    stop_sequences: List[str] = field(default_factory=lambda: ["<end_code>"])
    max_tokens_per_request: int = 1024


class Orchestrator:
    """Runs one task to completion (or to a limit) and returns a SolutionOutput."""

    def __init__(
        self,
        llm_client: LLMClient,
        sandbox: Sandbox,
        system_prompt: str,
        config: OrchestratorConfig,
    ) -> None:
        self.llm_client = llm_client
        self.sandbox = sandbox
        self.system_prompt = system_prompt
        self.config = config
        self._stop_requested = False

    def request_stop(self) -> None:
        """Called from a SIGTERM handler so a killed agent still returns partial
        metrics (and, for SWE-bench, still reaches its container cleanup)
        instead of losing them. Raises immediately (see ShutdownRequested) so a
        SIGTERM landing mid-LLM-call interrupts it right away rather than
        waiting for it to finish on its own; the outer hard timeout is still
        enforced by moulinette's run-agent command per Section 6.1."""
        self._stop_requested = True
        raise ShutdownRequested("shutdown requested (e.g. SIGTERM)")

    def run(self, task_id: str, benchmark: str, task_prompt: str) -> SolutionOutput:
        start = time.monotonic()
        messages: List[dict] = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": task_prompt},
        ]
        steps: List[StepMetrics] = []
        total_input_tokens = 0
        total_output_tokens = 0
        total_requests = 0
        error: Optional[str] = None
        solution_text = ""
        success = False

        for step_number in range(1, self.config.max_iterations + 1):
            if self._stop_requested:
                error = "stopped: shutdown requested (e.g. SIGTERM)"
                break
            elapsed = time.monotonic() - start
            if elapsed >= self.config.max_time_seconds:
                error = f"time budget exhausted ({elapsed:.1f}s >= {self.config.max_time_seconds}s)"
                break
            if total_input_tokens >= self.config.max_input_tokens:
                error = (
                    f"input token budget exhausted "
                    f"({total_input_tokens} >= {self.config.max_input_tokens})"
                )
                break
            if total_output_tokens >= self.config.max_output_tokens:
                error = (
                    f"output token budget exhausted "
                    f"({total_output_tokens} >= {self.config.max_output_tokens})"
                )
                break

            try:
                gen = self.llm_client.generate(
                    messages,
                    stop=self.config.stop_sequences,
                    max_output_tokens=self.config.max_tokens_per_request,
                )
            except AllProvidersExhaustedError as exc:
                total_requests += exc.attempted_requests
                error = f"LLM request failed: {exc}"
                break
            except ShutdownRequested as exc:
                error = str(exc)
                break

            total_requests += 1 + gen.retries
            total_input_tokens += gen.input_tokens
            total_output_tokens += gen.output_tokens

            extraction = extract_code(gen.text)
            sandbox_input = extraction.code or ""
            final_answer_raised: Optional[FinalAnswer] = None

            if extraction.code is None:
                observation = extraction.note
            else:
                try:
                    sandbox_output = self.sandbox.run(extraction.code)
                    observation = (
                        f"{extraction.note}\n{sandbox_output}" if extraction.note else sandbox_output
                    )
                except FinalAnswer as fa:
                    final_answer_raised = fa
                    observation = f"[FinalAnswer submitted] {fa.answer!r}"

            steps.append(
                StepMetrics(
                    step=step_number,
                    input_tokens=gen.input_tokens,
                    output_tokens=gen.output_tokens,
                    request_time_ms=gen.request_time_ms,
                    api_url=gen.api_url,
                    model_name=gen.model_name,
                    llm_output=gen.text,
                    sandbox_input=sandbox_input,
                    sandbox_output=observation,
                    retries=gen.retries,
                )
            )

            if final_answer_raised is not None:
                success = True
                solution_text = str(final_answer_raised.answer)
                break

            messages.append({"role": "assistant", "content": gen.text})
            messages.append({"role": "user", "content": f"Observation:\n{observation}"})
        else:
            error = f"max iterations reached ({self.config.max_iterations})"

        if not success and error is None:
            error = "loop ended without a final_answer() call"

        return SolutionOutput(
            task_id=task_id,
            benchmark=benchmark,
            success=success,
            solution=solution_text,
            iterations=len(steps),
            total_requests=total_requests,
            total_input_tokens=total_input_tokens,
            total_output_tokens=total_output_tokens,
            total_time_seconds=time.monotonic() - start,
            steps=steps,
            system_prompt=self.system_prompt,
            error=None if success else error,
            timestamp=datetime.now().isoformat(),
        )
