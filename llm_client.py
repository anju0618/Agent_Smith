"""LLM client abstraction. Only OpenRouter is wired up for now — the full multi-provider
rotation layer (TASKS.md section 8) comes later; this exists so the orchestrator loop
(section 1) has something real to call.
"""
from typing import Protocol

import requests

from config import get_api_key


class LLMClient(Protocol):
    def generate(self, messages: list[dict[str, str]], stop: list[str]) -> str:
        """Sends `messages` (OpenAI chat format) and returns the assistant's raw text."""
        ...


class OpenRouterClient:
    """Minimal OpenAI-compatible chat completions client for OpenRouter."""

    API_URL = "https://openrouter.ai/api/v1/chat/completions"

    def __init__(self, model_name: str, timeout_seconds: int = 60):
        self.model_name = model_name
        self.timeout_seconds = timeout_seconds
        self._api_key = get_api_key("openrouter")

    def generate(self, messages: list[dict[str, str]], stop: list[str]) -> str:
        response = requests.post(
            self.API_URL,
            headers={"Authorization": f"Bearer {self._api_key}"},
            json={"model": self.model_name, "messages": messages, "stop": stop},
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        content: str = response.json()["choices"][0]["message"]["content"]
        return content
