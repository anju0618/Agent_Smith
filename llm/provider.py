"""抽象LLMプロバイダインターフェースと、使用量トラッキング用の各種型。

Requirements.mdはまさにこの箇所を実装ギャップとして指摘している: `generate()`は
単なる生テキストだけではなく、StepMetricsを埋めるために十分なメタデータ
(トークン数、時間、api_url、model_name、retries)を返さなければならない。
以下のGenerationResultがその契約であり、全ての具体的プロバイダ
(openai_compatible.py、gemini.py)はこれを返す。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Protocol


@dataclass
class GenerationResult:
    """1回のエージェントステップがStepMetrics(models.py)を埋めるために必要な全情報。"""

    text: str
    input_tokens: int
    output_tokens: int
    request_time_ms: float
    api_url: str
    model_name: str
    retries: int = 0


class ChatProvider(Protocol):
    """プロバイダは (messages, model, api_key, ...) をGenerationResultに変換する方法を知っている。"""

    def chat(
        self,
        messages: List[dict],
        model: str,
        api_key: str,
        stop: Optional[List[str]],
        max_output_tokens: int,
        timeout: float,
    ) -> GenerationResult:
        ...


@dataclass
class UsageStats:
    """エージェント実行全体を通じた使用量の集計(Section 4.2 - 技術的制約:
    「トークン数、リトライ回数、レイテンシ、リクエスト数の使用量トラッキングを
    実装しなければならない」への対応)。"""

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
