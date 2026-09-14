"""マルチプロバイダ対応LLMクライアント: トークンローテーション、プロバイダのフォールバック、
リトライ、使用量トラッキングを行う。

これはrequirements.mdの「現状の実装ギャップ」注記(Section 4.1)が明示的に指摘している部分:
generate()は生テキストだけでなく、StepMetricsを埋めるために十分なメタデータ
(トークン数、時間、api_url、model_name、retries)を返さなければならない。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import List, Optional

import requests

from config import KNOWN_PROVIDERS, ProviderSpec, resolve_provider
from llm.provider import ChatProvider, GenerationResult, UsageStats
from llm.providers.gemini import GeminiProvider
from llm.providers.openai_compatible import OpenAICompatibleProvider


class AllProvidersExhaustedError(RuntimeError):
    """1回のgenerate()呼び出しで、設定された全てのAPIキー/プロバイダが失敗した場合に送出される。

    `attempted_requests` は例外を投げる前に設定される: 失敗した各試行は実際の
    HTTPリクエストであり、StepMetrics/SolutionOutput.total_requestsはそれを
    カウントしなければならない(Section 5.1の「リトライを含む、行われたLLM API
    リクエストの総数」)。この呼び出しは成功した生成が無いためStepMetricsの
    エントリを一切生成しないにもかかわらず、その回数は数えておく必要がある。
    """

    def __init__(self, message: str, attempted_requests: int = 0) -> None:
        super().__init__(message)
        self.attempted_requests = attempted_requests


def _build_chat_provider(spec: ProviderSpec) -> ChatProvider:

    if spec.kind == "gemini":
        return GeminiProvider(spec.base_url)
    return OpenAICompatibleProvider(spec.base_url)


@dataclass
class _ProviderSlot:

    spec: ProviderSpec
    chat_provider: ChatProvider
    api_keys: List[str]
    model_name: str
    next_key_index: int = field(default=0)


class LLMClient:
    """1つ以上のプロバイダにまたがって、単一の論理モデルをフォールバック付きで呼び出すクライアント。

    プロバイダは与えられた順序で試行される。1つのプロバイダ内では、APIキーが
    ラウンドロビン方式でローテーションされるので、レート制限に達した1つのキーが
    実行全体を止めてしまうことがない(Section 4.6.1: 「複数トークン管理は必須」)。
    """

    def __init__(
        self,
        model_name: str,
        provider_specs: List[ProviderSpec],
        max_retries_per_key: int = 2,
        backoff_seconds: float = 1.5,
        request_timeout: float = 60.0,
    ) -> None:
        self.model_name = model_name
        self.max_retries_per_key = max_retries_per_key
        self.backoff_seconds = backoff_seconds
        self.request_timeout = request_timeout
        self.usage = UsageStats()

        self._slots: List[_ProviderSlot] = []
        for spec in provider_specs:
            keys = spec.collect_api_keys()
            if not keys:
                continue
            self._slots.append(
                _ProviderSlot(
                    spec=spec, chat_provider=_build_chat_provider(spec), api_keys=keys, model_name=model_name
                )
            )

        if not self._slots:
            names = ", ".join(spec.name for spec in provider_specs)
            raise ValueError(
                f"No API keys found for provider(s): {names}. "
                "Set them via .env or environment variables (see .env.example)."
            )

    @classmethod
    def from_provider_url(cls, model_name: str, provider_url: str, **kwargs: object) -> "LLMClient":
        """指定された(model_name, provider_url)を最優先で使いつつ、.envに鍵が設定されている
        他の既知プロバイダ(KNOWN_PROVIDERS)を自動で予備として追加するショートカット。

        Section 4.6.1「複数プロバイダごとに複数APIトークンをサポート」「プロバイダの
        フォールバックの実装も検討すること」への対応。以前はここが単一プロバイダのspecしか
        作らず、そのプロバイダ/アカウントの無料枠が尽きると`AllProvidersExhaustedError`で
        即座に全滅していた(config.pyの複数プロバイダ登録・複数キーローテーションが実際の
        起動経路からは一切使われていなかった)。予備プロバイダは呼び出し元が指定した
        model_nameではなく、そのプロバイダ用にBENCHMARK_REPORT.mdで動作確認済みの
        `fallback_model`を使う(モデル名はプロバイダをまたいで共通ではないため)。
        """
        primary = resolve_provider(provider_url)
        client = cls(model_name, [primary], **kwargs)  # type: ignore[arg-type]
        for spec in KNOWN_PROVIDERS:
            if spec.name == primary.name or spec.fallback_model is None:
                continue
            keys = spec.collect_api_keys()
            if not keys:
                continue

            client._slots.append(
                _ProviderSlot(
                    spec=spec,
                    chat_provider=_build_chat_provider(spec),
                    api_keys=keys,
                    model_name=spec.fallback_model,
                )
            )
        return client

    def generate(
        self,
        messages: List[dict],
        stop: Optional[List[str]] = None,
        max_output_tokens: int = 1024,
    ) -> GenerationResult:
        """補完を生成する。失敗時はキー/プロバイダをローテーションしながら再試行する。

        返り値の `.retries` は、成功した試行の前に行われた(キーとプロバイダを
        またいだ)全ての失敗試行の回数をカウントしており、StepMetrics.retriesの
        意味論と一致する。
        """
        last_error: Optional[Exception] = None
        retries = 0

        for slot in self._slots:
            for _ in range(len(slot.api_keys)):
                api_key = slot.api_keys[slot.next_key_index]

                slot.next_key_index = (slot.next_key_index + 1) % len(slot.api_keys)

                for attempt in range(self.max_retries_per_key):
                    try:
                        result = slot.chat_provider.chat(
                            messages=messages,
                            model=slot.model_name,
                            api_key=api_key,
                            stop=stop,
                            max_output_tokens=max_output_tokens,
                            timeout=self.request_timeout,
                        )
                        result.retries = retries
                        self.usage.record(result)
                        return result
                    except (requests.RequestException, KeyError, IndexError) as exc:

                        last_error = exc
                        retries += 1
                        self.usage.errors.append(f"{slot.spec.name}: {exc}")
                        if attempt < self.max_retries_per_key - 1:
                            time.sleep(self.backoff_seconds * (attempt + 1))


        self.usage.total_requests += retries
        self.usage.total_retries += retries
        raise AllProvidersExhaustedError(
            f"All providers/keys exhausted for model '{self.model_name}'. Last error: {last_error}",
            attempted_requests=retries,
        )
