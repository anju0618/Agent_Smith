"""Google AI StudioのGemini REST API向けのチャット補完クライアント。

Geminiのワイヤーフォーマットは構造的に異なる(/chat/completionsパスがない、
APIキーはクエリパラメータとして渡す、"contents"/"parts"というリクエストスキーマ、
"candidates"というレスポンススキーマ)ため、OpenAICompatibleProviderとは分離して
実装している - これはSection 4.6で要求されているマルチプロバイダ抽象化を
裏付ける、構造的に異なる2つ目のプロバイダであり、この抽象化が単なる
OpenAI形式のインターフェースの偽装ではないことを証明している。
"""
from __future__ import annotations

import time
from typing import List, Optional, Tuple

import requests

from llm.provider import GenerationResult


class GeminiProvider:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    @staticmethod
    def _to_gemini_contents(messages: List[dict]) -> Tuple[Optional[str], List[dict]]:

        system_instruction = None
        contents = []
        for msg in messages:
            role = msg["role"]
            if role == "system":
                system_instruction = msg["content"]
                continue

            gemini_role = "model" if role == "assistant" else "user"

            contents.append({"role": gemini_role, "parts": [{"text": msg["content"]}]})
        return system_instruction, contents

    def chat(
        self,
        messages: List[dict],
        model: str,
        api_key: str,
        stop: Optional[List[str]],
        max_output_tokens: int,
        timeout: float,
    ) -> GenerationResult:
        system_instruction, contents = self._to_gemini_contents(messages)
        url = f"{self.base_url}/models/{model}:generateContent"

        generation_config: dict = {"maxOutputTokens": max_output_tokens}
        if stop:
            generation_config["stopSequences"] = stop

        payload: dict = {"contents": contents, "generationConfig": generation_config}
        if system_instruction:

            payload["systemInstruction"] = {"parts": [{"text": system_instruction}]}

        start = time.monotonic()
        try:

            response = requests.post(url, params={"key": api_key}, json=payload, timeout=timeout)
            response.raise_for_status()
        except requests.RequestException as exc:

            status = getattr(getattr(exc, "response", None), "status_code", None)

            status_part = f"status={status}" if status is not None else type(exc).__name__
            raise requests.RequestException(
                f"Gemini request failed ({status_part}) for url: {url} "
                "(query parameters, including the API key, redacted)"
            ) from None
        elapsed_ms = (time.monotonic() - start) * 1000
        data = response.json()

        candidate = data["candidates"][0]
        parts = candidate.get("content", {}).get("parts", [])
        text = "".join(part.get("text", "") for part in parts)

        usage = data.get("usageMetadata", {})

        return GenerationResult(
            text=text,
            input_tokens=usage.get("promptTokenCount", 0),
            output_tokens=usage.get("candidatesTokenCount", 0),
            request_time_ms=elapsed_ms,
            api_url=self.base_url,
            model_name=model,
        )
