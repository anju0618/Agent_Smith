"""Tests for llm/client.py: token rotation, provider fallback, retries, usage tracking.

Network calls are replaced with a fake ChatProvider so these tests never touch
a real API - see README.md for how to smoke-test against a real provider.
"""
from typing import List, Optional

import pytest
import requests

from config import ProviderSpec
from llm import client as client_module
from llm.provider import GenerationResult


class _FakeProvider:
    def __init__(self, fail_times: int = 0) -> None:
        self.fail_times = fail_times
        self.calls = 0
        self.seen_api_keys: List[str] = []

    def chat(
        self,
        messages: List[dict],
        model: str,
        api_key: str,
        stop: Optional[List[str]],
        max_output_tokens: int,
        timeout: float,
    ) -> GenerationResult:
        self.calls += 1
        self.seen_api_keys.append(api_key)
        if self.calls <= self.fail_times:
            raise requests.ConnectionError("simulated network failure")
        return GenerationResult(
            text="Thought: ok",
            input_tokens=10,
            output_tokens=5,
            request_time_ms=1.0,
            api_url="https://fake.example/v1",
            model_name=model,
        )


def _install_fake_provider(monkeypatch: pytest.MonkeyPatch, fake: _FakeProvider) -> None:
    monkeypatch.setattr(client_module, "_build_chat_provider", lambda spec: fake)


def test_generate_succeeds_on_first_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeProvider(fail_times=0)
    _install_fake_provider(monkeypatch, fake)
    monkeypatch.setenv("FAKE_API_KEY", "secret")

    spec = ProviderSpec("fake", "https://fake.example/v1", "FAKE_API_KEY")
    llm = client_module.LLMClient("fake-model", [spec], backoff_seconds=0)
    result = llm.generate([{"role": "user", "content": "hi"}])

    assert result.retries == 0
    assert result.text == "Thought: ok"
    assert llm.usage.total_requests == 1
    assert llm.usage.total_input_tokens == 10


def test_generate_retries_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeProvider(fail_times=1)
    _install_fake_provider(monkeypatch, fake)
    monkeypatch.setenv("FAKE_API_KEY", "secret")

    spec = ProviderSpec("fake", "https://fake.example/v1", "FAKE_API_KEY")
    llm = client_module.LLMClient("fake-model", [spec], backoff_seconds=0, max_retries_per_key=3)
    result = llm.generate([{"role": "user", "content": "hi"}])

    assert fake.calls == 2
    assert result.retries == 1
    assert llm.usage.total_requests == 2
    assert llm.usage.total_retries == 1


def test_multiple_keys_rotate(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeProvider(fail_times=0)
    _install_fake_provider(monkeypatch, fake)
    monkeypatch.setenv("FAKE_API_KEY", "key-one")
    monkeypatch.setenv("FAKE_API_KEY_2", "key-two")

    spec = ProviderSpec("fake", "https://fake.example/v1", "FAKE_API_KEY")
    llm = client_module.LLMClient("fake-model", [spec], backoff_seconds=0)
    llm.generate([{"role": "user", "content": "hi"}])
    llm.generate([{"role": "user", "content": "hi"}])

    assert fake.seen_api_keys == ["key-one", "key-two"]


def test_all_providers_exhausted_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeProvider(fail_times=999)
    _install_fake_provider(monkeypatch, fake)
    monkeypatch.setenv("FAKE_API_KEY", "secret")

    spec = ProviderSpec("fake", "https://fake.example/v1", "FAKE_API_KEY")
    llm = client_module.LLMClient("fake-model", [spec], backoff_seconds=0, max_retries_per_key=1)

    with pytest.raises(client_module.AllProvidersExhaustedError):
        llm.generate([{"role": "user", "content": "hi"}])


def test_all_providers_exhausted_reports_attempted_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every failed attempt was a real HTTP request; SolutionOutput.total_requests
    (Section 5.1) must count them even though this call never succeeds."""
    fake = _FakeProvider(fail_times=999)
    _install_fake_provider(monkeypatch, fake)
    monkeypatch.setenv("FAKE_API_KEY", "secret")

    spec = ProviderSpec("fake", "https://fake.example/v1", "FAKE_API_KEY")
    llm = client_module.LLMClient("fake-model", [spec], backoff_seconds=0, max_retries_per_key=3)

    with pytest.raises(client_module.AllProvidersExhaustedError) as exc_info:
        llm.generate([{"role": "user", "content": "hi"}])

    assert exc_info.value.attempted_requests == 3
    assert llm.usage.total_requests == 3
    assert llm.usage.total_retries == 3


def test_missing_api_key_raises_value_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MISSING_API_KEY", raising=False)
    spec = ProviderSpec("fake", "https://fake.example/v1", "MISSING_API_KEY")

    with pytest.raises(ValueError):
        client_module.LLMClient("fake-model", [spec])
