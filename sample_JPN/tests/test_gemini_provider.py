"""llm/providers/gemini.py のテスト。最も重要な1点、すなわち
「APIキーが例外メッセージに絶対に漏れないこと」に焦点を当てる。

Geminiは`?key=...`というURLクエリパラメータで認証を行う(ヘッダーによる方式は
用意されていない)。requestsのHTTPErrorも、接続/タイムアウト系の例外も、
そのデフォルトメッセージをリクエストURL全体(このクエリ文字列を含む)から
組み立ててしまう。実際にこの経路(AllProvidersExhaustedErrorのメッセージ経由)で
本物のキーがsolution.jsonに漏れかけた事例があり、GitHubのpush protectionに
よってその場で検知された。詳細はBENCHMARK_REPORT.mdを参照。
"""
import pytest  # pytest.raisesやmonkeypatch型ヒントに使用
import requests  # HTTPError/ConnectionErrorなどの例外クラスに使用

from llm.providers.gemini import GeminiProvider  # テスト対象のGeminiプロバイダ実装

FAKE_KEY = "AIzaSyFAKE_KEY_FOR_TESTING_0000000000000"  # テスト用のダミーAPIキー


class _FakeResponse:
    # requests.postの戻り値を模倣するフェイクレスポンスクラス
    def __init__(self, status_code: int, url: str) -> None:
        self.status_code = status_code  # HTTPステータスコード
        self.reason = "Too Many Requests"  # ステータス文言(429を想定)
        self.url = url  # リクエストURL(APIキーのクエリ文字列を含む)

    def raise_for_status(self) -> None:
        # requestsの実挙動同様、エラーステータスの場合にHTTPErrorを送出する
        # (このメッセージにURL全体、つまりAPIキーも含まれてしまう点が検証対象)
        error = requests.HTTPError(f"{self.status_code} Client Error: {self.reason} for url: {self.url}")
        error.response = self  # type: ignore[assignment]
        raise error

    def json(self) -> dict:
        return {}


def test_http_error_message_never_contains_the_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # HTTPErrorが発生しても、その例外メッセージ文字列にAPIキーが含まれないことを検証
    leaking_url = (
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-lite-latest:"
        f"generateContent?key={FAKE_KEY}"
    )

    def fake_post(url: str, params: dict, json: dict, timeout: float) -> _FakeResponse:
        # requests.postの代わりに429エラー(APIキーを含むURL付き)を返すフェイク関数
        return _FakeResponse(status_code=429, url=leaking_url)

    monkeypatch.setattr("llm.providers.gemini.requests.post", fake_post)  # requests.postを差し替え
    provider = GeminiProvider("https://generativelanguage.googleapis.com/v1beta")

    # chat()呼び出しで例外が発生することを期待しつつ捕捉する
    with pytest.raises(requests.RequestException) as exc_info:
        provider.chat(
            messages=[{"role": "user", "content": "hi"}],
            model="gemini-flash-lite-latest",
            api_key=FAKE_KEY,
            stop=None,
            max_output_tokens=100,
            timeout=10.0,
        )

    assert FAKE_KEY not in str(exc_info.value)  # 例外メッセージにAPIキーが含まれていないこと


def test_connection_error_message_never_contains_the_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """DNS解決失敗・タイムアウト・接続拒否といった低レベルの失敗はraise_for_status()に
    到達する前に発生するが、urllib3/requestsはそれらのデフォルトメッセージにも
    やはりURL全体(クエリ文字列込み)を埋め込んでしまう。"""

    def fake_post(url: str, params: dict, json: dict, timeout: float) -> _FakeResponse:
        # requests.postの代わりに、APIキーを含むURLを埋め込んだConnectionErrorを送出する
        raise requests.ConnectionError(
            f"HTTPSConnectionPool: Max retries exceeded with url: /v1beta/models/x:generateContent"
            f"?key={FAKE_KEY} (Caused by ...)"
        )

    monkeypatch.setattr("llm.providers.gemini.requests.post", fake_post)  # requests.postを差し替え
    provider = GeminiProvider("https://generativelanguage.googleapis.com/v1beta")

    # chat()呼び出しで例外が発生することを期待しつつ捕捉する
    with pytest.raises(requests.RequestException) as exc_info:
        provider.chat(
            messages=[{"role": "user", "content": "hi"}],
            model="gemini-flash-lite-latest",
            api_key=FAKE_KEY,
            stop=None,
            max_output_tokens=100,
            timeout=10.0,
        )

    assert FAKE_KEY not in str(exc_info.value)  # 例外メッセージにAPIキーが含まれていないこと


def test_successful_call_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    # APIキー漏洩対策のガードが、正常系の動作(成功時のレスポンス処理)を
    # 壊していないことを検証する
    class _OkResponse(_FakeResponse):
        def raise_for_status(self) -> None:
            return None  # 成功時は何も起きない

        def json(self) -> dict:
            # Gemini APIの正常なレスポンス形式を模倣したダミーデータ
            return {
                "candidates": [{"content": {"parts": [{"text": "hello"}]}}],
                "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 1},
            }

    def fake_post(url: str, params: dict, json: dict, timeout: float) -> _OkResponse:
        # クエリパラメータとしてAPIキーが正しく渡されていることも併せて確認
        assert params == {"key": FAKE_KEY}
        return _OkResponse(status_code=200, url=url)

    monkeypatch.setattr("llm.providers.gemini.requests.post", fake_post)  # requests.postを差し替え
    provider = GeminiProvider("https://generativelanguage.googleapis.com/v1beta")

    result = provider.chat(
        messages=[{"role": "user", "content": "hi"}],
        model="gemini-flash-lite-latest",
        api_key=FAKE_KEY,
        stop=None,
        max_output_tokens=100,
        timeout=10.0,
    )

    assert result.text == "hello"  # レスポンスからテキストが正しく取り出されること
    assert result.input_tokens == 3  # 入力トークン数がusageMetadataから反映されること
    assert result.output_tokens == 1  # 出力トークン数がusageMetadataから反映されること
