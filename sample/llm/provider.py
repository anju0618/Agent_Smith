"""Abstract LLM provider interface and usage-tracking types.

Requirements.md flags an implementation gap for this exact spot: `generate()`
must return enough metadata to populate StepMetrics (tokens, timing, api_url,
model_name, retries), not just raw text. GenerationResult below is that
contract; every concrete provider (openai_compatible.py, gemini.py) returns one.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Protocol


@dataclass
class GenerationResult:
    """Everything one agent step needs to populate StepMetrics (models.py)."""

    text: str
    input_tokens: int
    output_tokens: int
    request_time_ms: float
    api_url: str
    model_name: str
    retries: int = 0


class ChatProvider(Protocol):
    """A provider knows how to turn (messages, model, api_key, ...) into a GenerationResult."""

    def chat(
        self,
        messages: List[dict],
        model: str,
        api_key: str,
        stop: Optional[List[str]],
        max_output_tokens: int,
        timeout: float,
    ) -> GenerationResult:
        ...  # pragma: no cover - Protocol


@dataclass
class UsageStats:
    """Aggregate usage tracking across a whole agent run (Section 4.2 - Technical
    Constraints: "you must implement usage tracking: tokens, retries, latency,
    requests")."""

    total_requests: int = 0
    total_retries: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_latency_ms: float = 0.0
    errors: List[str] = field(default_factory=list)

    def record(self, gen: GenerationResult) -> None:
        self.total_requests += 1 + gen.retries
        self.total_retries += gen.retries
        self.total_input_tokens += gen.input_tokens
        self.total_output_tokens += gen.output_tokens
        self.total_latency_ms += gen.request_time_ms
