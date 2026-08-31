"""Chat completion client for Google AI Studio's Gemini REST API.

Kept separate from OpenAICompatibleProvider because Gemini's wire format
differs structurally (no /chat/completions path, API key as a query parameter,
"contents"/"parts" request schema, "candidates" response schema) - this is the
second, structurally different provider backing the multi-provider abstraction
required by Section 4.6, proving the abstraction isn't just an OpenAI-shaped
interface in disguise.
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
            # Never let a requests exception propagate as-is here: requests and
            # urllib3 build their default messages (HTTPError, ConnectionError,
            # Timeout alike) from the *full* request URL, which for this
            # provider includes the API key as a `?key=...` query parameter -
            # Gemini has no header-based auth option. That string ends up in
            # AllProvidersExhaustedError's message and from there straight into
            # SolutionOutput.error / StepMetrics.sandbox_output - i.e.
            # committed to disk in solution.json - unless it's rebuilt here,
            # from `url` (never the key-bearing request/response url), before
            # it can leak. (Found live: a real key reached three solution.json
            # files this way and was only caught by GitHub's push protection
            # before being pushed - see BENCHMARK_REPORT.md.)
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
