"""Chat completion client for any OpenAI-compatible /chat/completions endpoint.

Covers OpenRouter, Groq, Together AI, Fireworks AI, and most other free-tier
providers listed in Section 4.6.1 - they all speak the same wire format, which
is exactly why the multi-provider abstraction (Section 4.6) can stay this thin.
"""
from __future__ import annotations

import time
from typing import List, Optional

import requests

from llm.provider import GenerationResult


class OpenAICompatibleProvider:
    def __init__(self, base_url: str, chat_path: str = "/chat/completions") -> None:
        self.base_url = base_url.rstrip("/")
        self.chat_path = chat_path

    def chat(
        self,
        messages: List[dict],
        model: str,
        api_key: str,
        stop: Optional[List[str]],
        max_output_tokens: int,
        timeout: float,
    ) -> GenerationResult:
        url = f"{self.base_url}{self.chat_path}"
        payload: dict = {
            "model": model,
            "messages": messages,
            "max_tokens": max_output_tokens,
        }
        if stop:
            payload["stop"] = stop

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        start = time.monotonic()
        response = requests.post(url, json=payload, headers=headers, timeout=timeout)
        elapsed_ms = (time.monotonic() - start) * 1000
        response.raise_for_status()
        data = response.json()

        choice = data["choices"][0]
        text = choice["message"].get("content") or ""
        usage = data.get("usage", {})

        return GenerationResult(
            text=text,
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
            request_time_ms=elapsed_ms,
            api_url=self.base_url,
            model_name=model,
        )
