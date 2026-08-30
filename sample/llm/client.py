"""Multi-provider LLM client: token rotation, provider fallback, retries, usage tracking.

This is the piece requirements.md's "current implementation gap" note (Section
4.1) calls out explicitly: generate() must return enough metadata to populate
StepMetrics (tokens, timing, api_url, model_name, retries), not just raw text.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import List, Optional

import requests

from config import ProviderSpec, resolve_provider
from llm.provider import ChatProvider, GenerationResult, UsageStats
from llm.providers.gemini import GeminiProvider
from llm.providers.openai_compatible import OpenAICompatibleProvider


class AllProvidersExhaustedError(RuntimeError):
    """Raised when every configured API key/provider failed for one generate() call."""


def _build_chat_provider(spec: ProviderSpec) -> ChatProvider:
    if spec.kind == "gemini":
        return GeminiProvider(spec.base_url)
    return OpenAICompatibleProvider(spec.base_url)


@dataclass
class _ProviderSlot:
    spec: ProviderSpec
    chat_provider: ChatProvider
    api_keys: List[str]
    next_key_index: int = field(default=0)


class LLMClient:
    """Calls a single logical model across one or more providers with fallback.

    Providers are tried in the order given. Within a provider, API keys rotate
    round-robin so one rate-limited key doesn't stall the whole run (Section
    4.6.1: "multi-token management is mandatory").
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
                _ProviderSlot(spec=spec, chat_provider=_build_chat_provider(spec), api_keys=keys)
            )

        if not self._slots:
            names = ", ".join(spec.name for spec in provider_specs)
            raise ValueError(
                f"No API keys found for provider(s): {names}. "
                "Set them via .env or environment variables (see .env.example)."
            )

    @classmethod
    def from_provider_url(cls, model_name: str, provider_url: str, **kwargs: object) -> "LLMClient":
        return cls(model_name, [resolve_provider(provider_url)], **kwargs)  # type: ignore[arg-type]

    def generate(
        self,
        messages: List[dict],
        stop: Optional[List[str]] = None,
        max_output_tokens: int = 1024,
    ) -> GenerationResult:
        """Generate a completion, rotating keys/providers on failure.

        The returned result's `.retries` counts every failed attempt (across
        keys and providers) before the successful one, matching
        StepMetrics.retries semantics.
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
                            model=self.model_name,
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
                # exhausted retries for this key - fall through to the next key/provider

        self.usage.total_retries += retries
        raise AllProvidersExhaustedError(
            f"All providers/keys exhausted for model '{self.model_name}'. Last error: {last_error}"
        )
