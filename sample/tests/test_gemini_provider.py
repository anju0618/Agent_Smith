"""Tests for llm/providers/gemini.py, focused on the one thing that matters
most: an API key must never leak into an exception message.

Gemini authenticates via a `?key=...` URL query parameter (no header option),
and both requests' HTTPError and its connection/timeout exceptions build their
default message from the full request URL - including that query string. A
real key reaching solution.json this way (via AllProvidersExhaustedError's
message) was caught live by GitHub's push protection; see BENCHMARK_REPORT.md.
"""
import pytest
import requests

from llm.providers.gemini import GeminiProvider

FAKE_KEY = "AIzaSyFAKE_KEY_FOR_TESTING_0000000000000"


class _FakeResponse:
    def __init__(self, status_code: int, url: str) -> None:
        self.status_code = status_code
        self.reason = "Too Many Requests"
        self.url = url

    def raise_for_status(self) -> None:
        error = requests.HTTPError(f"{self.status_code} Client Error: {self.reason} for url: {self.url}")
        error.response = self  # type: ignore[assignment]
        raise error

    def json(self) -> dict:
        return {}


def test_http_error_message_never_contains_the_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    leaking_url = (
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-lite-latest:"
        f"generateContent?key={FAKE_KEY}"
    )

    def fake_post(url: str, params: dict, json: dict, timeout: float) -> _FakeResponse:
        return _FakeResponse(status_code=429, url=leaking_url)

    monkeypatch.setattr("llm.providers.gemini.requests.post", fake_post)
    provider = GeminiProvider("https://generativelanguage.googleapis.com/v1beta")

    with pytest.raises(requests.RequestException) as exc_info:
        provider.chat(
            messages=[{"role": "user", "content": "hi"}],
            model="gemini-flash-lite-latest",
            api_key=FAKE_KEY,
            stop=None,
            max_output_tokens=100,
            timeout=10.0,
        )

    assert FAKE_KEY not in str(exc_info.value)


def test_connection_error_message_never_contains_the_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lower-level failures (DNS, timeout, connection refused) raise before
    raise_for_status() is ever reached, but urllib3/requests still embed the
    full URL - including the query string - in *their* default messages too."""

    def fake_post(url: str, params: dict, json: dict, timeout: float) -> _FakeResponse:
        raise requests.ConnectionError(
            f"HTTPSConnectionPool: Max retries exceeded with url: /v1beta/models/x:generateContent"
            f"?key={FAKE_KEY} (Caused by ...)"
        )

    monkeypatch.setattr("llm.providers.gemini.requests.post", fake_post)
    provider = GeminiProvider("https://generativelanguage.googleapis.com/v1beta")

    with pytest.raises(requests.RequestException) as exc_info:
        provider.chat(
            messages=[{"role": "user", "content": "hi"}],
            model="gemini-flash-lite-latest",
            api_key=FAKE_KEY,
            stop=None,
            max_output_tokens=100,
            timeout=10.0,
        )

    assert FAKE_KEY not in str(exc_info.value)


def test_successful_call_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    class _OkResponse(_FakeResponse):
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "candidates": [{"content": {"parts": [{"text": "hello"}]}}],
                "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 1},
            }

    def fake_post(url: str, params: dict, json: dict, timeout: float) -> _OkResponse:
        assert params == {"key": FAKE_KEY}
        return _OkResponse(status_code=200, url=url)

    monkeypatch.setattr("llm.providers.gemini.requests.post", fake_post)
    provider = GeminiProvider("https://generativelanguage.googleapis.com/v1beta")

    result = provider.chat(
        messages=[{"role": "user", "content": "hi"}],
        model="gemini-flash-lite-latest",
        api_key=FAKE_KEY,
        stop=None,
        max_output_tokens=100,
        timeout=10.0,
    )

    assert result.text == "hello"
    assert result.input_tokens == 3
    assert result.output_tokens == 1
