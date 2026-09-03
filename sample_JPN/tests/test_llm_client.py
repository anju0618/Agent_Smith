"""llm/client.py のテスト: トークンローテーション、プロバイダのフォールバック、
リトライ、使用量トラッキングを検証する。

ネットワーク呼び出しはフェイクのChatProviderに置き換えているため、これらの
テストは実際のAPIには一切触れない。実プロバイダに対するスモークテストの
方法はREADME.mdを参照。
"""
from typing import List, Optional  # 型ヒントに使用

import pytest  # monkeypatch型ヒント・pytest.raisesに使用
import requests  # ConnectionError(擬似的なネットワーク障害)に使用

from config import ProviderSpec  # プロバイダ設定(URL・APIキー環境変数名など)
from llm import client as client_module  # テスト対象のLLMClient本体を含むモジュール
from llm.provider import GenerationResult  # chat()の戻り値型


class _FakeProvider:
    """本物のChatProviderの代わりに使う、指定回数だけ失敗してから成功するフェイク実装。"""

    def __init__(self, fail_times: int = 0) -> None:
        self.fail_times = fail_times  # 最初に失敗させる回数
        self.calls = 0  # 実際の呼び出し回数カウンタ
        self.seen_api_keys: List[str] = []  # 呼び出しごとに渡されたAPIキーの記録

    def chat(
        self,
        messages: List[dict],
        model: str,
        api_key: str,
        stop: Optional[List[str]],
        max_output_tokens: int,
        timeout: float,
    ) -> GenerationResult:
        self.calls += 1  # 呼び出し回数をインクリメント
        self.seen_api_keys.append(api_key)  # 使われたAPIキーを記録
        if self.calls <= self.fail_times:
            # 指定回数までは擬似的なネットワーク障害を発生させる
            raise requests.ConnectionError("simulated network failure")
        # それ以降は固定の成功レスポンスを返す
        return GenerationResult(
            text="Thought: ok",
            input_tokens=10,
            output_tokens=5,
            request_time_ms=1.0,
            api_url="https://fake.example/v1",
            model_name=model,
        )


def _install_fake_provider(monkeypatch: pytest.MonkeyPatch, fake: _FakeProvider) -> None:
    # LLMClient内部でプロバイダを構築する関数を、常にフェイクを返すよう差し替える
    monkeypatch.setattr(client_module, "_build_chat_provider", lambda spec: fake)


def test_generate_succeeds_on_first_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    # 1回目の呼び出しで成功する通常ケースを検証
    fake = _FakeProvider(fail_times=0)  # 失敗しないフェイクプロバイダ
    _install_fake_provider(monkeypatch, fake)
    monkeypatch.setenv("FAKE_API_KEY", "secret")  # APIキー用の環境変数を設定

    spec = ProviderSpec("fake", "https://fake.example/v1", "FAKE_API_KEY")
    llm = client_module.LLMClient("fake-model", [spec], backoff_seconds=0)
    result = llm.generate([{"role": "user", "content": "hi"}])

    assert result.retries == 0  # リトライは発生していないこと
    assert result.text == "Thought: ok"  # フェイクの応答テキストがそのまま返ること
    assert llm.usage.total_requests == 1  # リクエスト数が1回とカウントされること
    assert llm.usage.total_input_tokens == 10  # 入力トークン数が積算されること


def test_generate_retries_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    # 1回失敗した後にリトライして成功するケースを検証
    fake = _FakeProvider(fail_times=1)  # 最初の1回だけ失敗するフェイクプロバイダ
    _install_fake_provider(monkeypatch, fake)
    monkeypatch.setenv("FAKE_API_KEY", "secret")

    spec = ProviderSpec("fake", "https://fake.example/v1", "FAKE_API_KEY")
    llm = client_module.LLMClient("fake-model", [spec], backoff_seconds=0, max_retries_per_key=3)
    result = llm.generate([{"role": "user", "content": "hi"}])

    assert fake.calls == 2  # 失敗1回+成功1回で計2回呼ばれること
    assert result.retries == 1  # リトライ回数が1と記録されること
    assert llm.usage.total_requests == 2  # 総リクエスト数が2とカウントされること
    assert llm.usage.total_retries == 1  # 総リトライ数が1とカウントされること


def test_multiple_keys_rotate(monkeypatch: pytest.MonkeyPatch) -> None:
    # 複数のAPIキーが登録されている場合、呼び出しごとに順番にローテーションされることを検証
    fake = _FakeProvider(fail_times=0)
    _install_fake_provider(monkeypatch, fake)
    monkeypatch.setenv("FAKE_API_KEY", "key-one")  # 1つ目のキー
    monkeypatch.setenv("FAKE_API_KEY_2", "key-two")  # 2つ目のキー(連番の環境変数)

    spec = ProviderSpec("fake", "https://fake.example/v1", "FAKE_API_KEY")
    llm = client_module.LLMClient("fake-model", [spec], backoff_seconds=0)
    llm.generate([{"role": "user", "content": "hi"}])  # 1回目の呼び出し
    llm.generate([{"role": "user", "content": "hi"}])  # 2回目の呼び出し

    assert fake.seen_api_keys == ["key-one", "key-two"]  # 1回目・2回目で異なるキーが使われること


def test_all_providers_exhausted_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    # 全てのプロバイダ/キーで失敗し続けた場合、AllProvidersExhaustedErrorが送出されることを検証
    fake = _FakeProvider(fail_times=999)  # 常に失敗するフェイクプロバイダ
    _install_fake_provider(monkeypatch, fake)
    monkeypatch.setenv("FAKE_API_KEY", "secret")

    spec = ProviderSpec("fake", "https://fake.example/v1", "FAKE_API_KEY")
    llm = client_module.LLMClient("fake-model", [spec], backoff_seconds=0, max_retries_per_key=1)

    with pytest.raises(client_module.AllProvidersExhaustedError):
        llm.generate([{"role": "user", "content": "hi"}])


def test_all_providers_exhausted_reports_attempted_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    """失敗した試行のそれぞれが実際のHTTPリクエストだった。SolutionOutput.total_requests
    (セクション5.1)は、この呼び出しが最終的に成功しなくても、それらを全てカウント
    しなければならない。"""
    fake = _FakeProvider(fail_times=999)  # 常に失敗するフェイクプロバイダ
    _install_fake_provider(monkeypatch, fake)
    monkeypatch.setenv("FAKE_API_KEY", "secret")

    spec = ProviderSpec("fake", "https://fake.example/v1", "FAKE_API_KEY")
    llm = client_module.LLMClient("fake-model", [spec], backoff_seconds=0, max_retries_per_key=3)

    with pytest.raises(client_module.AllProvidersExhaustedError) as exc_info:
        llm.generate([{"role": "user", "content": "hi"}])

    assert exc_info.value.attempted_requests == 3  # 例外に試行回数3が記録されていること
    assert llm.usage.total_requests == 3  # 使用量トラッキング側でも3回とカウントされること
    assert llm.usage.total_retries == 3  # リトライ数も3とカウントされること


def test_missing_api_key_raises_value_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # 指定された環境変数にAPIキーが存在しない場合、LLMClientの構築時点でValueErrorになることを検証
    monkeypatch.delenv("MISSING_API_KEY", raising=False)  # 該当の環境変数が存在しないことを保証
    spec = ProviderSpec("fake", "https://fake.example/v1", "MISSING_API_KEY")

    with pytest.raises(ValueError):
        client_module.LLMClient("fake-model", [spec])
