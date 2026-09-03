"""Tests for llm/providers/gemini.py, focused on the one thing that matters
most: an API key must never leak into an exception message.

Gemini authenticates via a `?key=...` URL query parameter (no header option),
and both requests' HTTPError and its connection/timeout exceptions build their
default message from the full request URL - including that query string. A
real key reaching solution.json this way (via AllProvidersExhaustedError's
message) was caught live by GitHub's push protection; see BENCHMARK_REPORT.md.
"""
# ============================================================================
# 日本語解説: このファイルは llm/providers/gemini.py（Google AI Studio用の
# LLMプロバイダ実装）の中でも、「APIキーが例外メッセージに絶対に漏れない
# こと」だけにフォーカスした専用のテストファイルです。
#
# なぜこれが特別に重要かというと、実際にこのプロジェクトで起きた
# インシデントに由来するからです。ほとんどのAPI（OpenRouterやGroqなど）は
# APIキーをHTTPヘッダーで送りますが、Geminiは違っていて、URLの
# クエリパラメータとして "?key=AIzaSy..." のように直接埋め込みます。
# 一方、Pythonの requests ライブラリが投げる HTTPError や
# ConnectionError は、デフォルトのエラーメッセージを「リクエストURL全体
# から」組み立てます。つまり何も対策しないと、そのURL丸ごと（クエリ
# パラメータのAPIキーも含めて）がそのまま例外メッセージに現れてしまいます。
#
# そのメッセージは AllProvidersExhaustedError → SolutionOutput.error /
# StepMetrics.sandbox_output という経路を通って、最終的に solution.json
# というファイルに平文でそのまま書き込まれます。実際にこの経路で
# 3つのsolution.jsonに本物のAPIキーが漏れてしまい、GitHubの
# push protection（秘密情報の混入をpush時に検知する機能）に
# 引っかかって初めて発覚した、という実話がBENCHMARK_REPORT.mdに
# 記録されています。
#
# 対策として、gemini.py は requests.RequestException をそのまま
# 伝播させるのではなく、「キーを含まない url（クエリパラメータを除いた
# 部分）とステータスコードだけ」から新しいメッセージを組み立て直して
# から送出するようにしています。このファイルの3つのテストは、
# その対策が実際に機能していること（そして正常系まで壊していないこと）
# を確認しています。
# ============================================================================
import pytest
import requests

from llm.providers.gemini import GeminiProvider

FAKE_KEY = "AIzaSyFAKE_KEY_FOR_TESTING_0000000000000"


class _FakeResponse:
    # requests.Responseの代わりに使う偽のレスポンスオブジェクト。
    # 本物のGemini APIを叩かずに「エラーになった状況」を再現するための道具。
    def __init__(self, status_code: int, url: str) -> None:
        self.status_code = status_code
        self.reason = "Too Many Requests"
        self.url = url

    def raise_for_status(self) -> None:
        # requestsライブラリの実際の挙動を模倣: HTTPErrorのデフォルト
        # メッセージにはself.url（クエリパラメータ込みのURL全体）が
        # そのまま埋め込まれる。これが「対策しないと何が起きるか」の再現。
        error = requests.HTTPError(f"{self.status_code} Client Error: {self.reason} for url: {self.url}")
        error.response = self  # type: ignore[assignment]
        raise error

    def json(self) -> dict:
        return {}


def test_http_error_message_never_contains_the_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # わざと「APIキーを含んだURL」でHTTPError(429 Too Many Requests)が
    # 発生する状況を再現し、GeminiProvider.chat()が投げ直す例外の
    # メッセージ文字列(str(exc_info.value))の中に、本物のAPIキー(FAKE_KEY)
    # が一切含まれていないことを確認する。これがこのファイルの核心のテスト。
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
    # 日本語解説: 上のテストがHTTPステータスエラー(429など、サーバーから
    # 応答が返ってきた後のエラー)だったのに対し、こちらはDNS解決失敗や
    # タイムアウト、接続拒否のような、そもそもサーバーに届く前の
    # より低レベルな失敗のケース。この場合raise_for_status()にすら
    # 到達しないが、それでもurllib3/requests側が組み立てるデフォルトの
    # ConnectionErrorメッセージにはURL(クエリパラメータ込み)が
    # 埋め込まれてしまう。つまり「対策すべき経路は1つではなく、
    # 複数の異なる例外クラスそれぞれで同じ問題が起きうる」ことを示す
    # テストであり、gemini.py側もそれぞれの経路をカバーしている必要がある。
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
    # キー漏洩対策に集中しすぎて、正常系のレスポンス処理まで壊していないか
    # を確認する回帰テスト。「防御を追加したら、正常系を壊していないかの
    # テストもセットで書く」という、このテストスイート全体で繰り返される
    # パターンがここにも表れている。paramsに正しくキーが渡っていること
    # (実際のリクエストを送る側では当然キーは必要)、レスポンスの
    # candidates/usageMetadataが正しくGenerationResultに変換されることを
    # 確認している。
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
