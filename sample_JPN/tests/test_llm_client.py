"""Tests for llm/client.py: token rotation, provider fallback, retries, usage tracking.

Network calls are replaced with a fake ChatProvider so these tests never touch
a real API - see README.md for how to smoke-test against a real provider.
"""
# ============================================================================
# 日本語解説: このファイルは llm/client.py の LLMClient
# （1つの論理モデルを、複数プロバイダ・複数APIキーにわたるフォールバック
# 付きで呼び出すクラス）をテストしています。
#
# 実際のネットワーク通信は一切行いません。_FakeProvider という「偽の
# ChatProvider」を使い、本物のHTTPリクエストの代わりに、指定した回数だけ
# 意図的に失敗させたり、決まったレスポンスを返したりできるようにして
# います。これによりネットワークやAPIキーが無くても、
# 「1つのキーがレート制限に当たったら別のキーに自動的に切り替わるか」
# 「全キー・全プロバイダが尽きたときに正しくエラーになるか」
# 「失敗した試行の回数もちゃんとカウントされるか」といった、
# 本物のAPIを叩かないと再現しにくい状況を確実に再現してテストできます。
# ============================================================================
from typing import List, Optional

import pytest
import requests

from config import ProviderSpec
from llm import client as client_module
from llm.provider import GenerationResult


class _FakeProvider:
    # 本物のChatProviderの代わりに使う偽物。fail_timesで指定した回数分だけ
    # 意図的にConnectionErrorを発生させ、それ以降は固定のGenerationResultを
    # 返す。seen_api_keysに呼び出しごとのapi_keyを記録しておくことで、
    # 「本当にキーがローテーションされたか」を後からassertで確認できる。
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
    # llm/client.py内部で「実際のChatProviderを組み立てる関数」
    # (_build_chat_provider)を、常に上のfakeを返すものに差し替える。
    # これによりLLMClientの内部ロジック(ローテーション/リトライ/フォールバック)
    # だけを、本物のHTTP層を経由せずにテストできる。
    monkeypatch.setattr(client_module, "_build_chat_provider", lambda spec: fake)


def test_generate_succeeds_on_first_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    # 最も基本的な正常系: 1回目の呼び出しでいきなり成功するケース。
    # retries(リトライ回数)が0であること、usage統計(total_requests/
    # total_input_tokens)が正しく積み上がることを確認する。
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
    # 1回目は失敗し、2回目で成功するケース。fail_times=1なので
    # 最初の呼び出しだけがConnectionErrorになり、LLMClientが自動的に
    # リトライして2回目で成功結果を得られることを確認する。
    # retries==1、total_requests==2（1回失敗+1回成功=2回のHTTPリクエスト）、
    # total_retries==1という、それぞれ意味の異なる集計値がすべて
    # 正しく反映されることを確認している。
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
    # 複数トークン管理のテスト。.envに FAKE_API_KEY と FAKE_API_KEY_2 の
    # 2つのキーが設定されている状況を再現し、generate()を2回呼んだときに
    # 実際に異なるキー（"key-one"→"key-two"）が順番に使われる
    # （ラウンドロビン方式でローテーションされる）ことを確認する。
    # 1つのキーがレート制限に達しても全体が止まらないようにするための機能。
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
    # fail_times=999という、事実上「絶対に成功しない」設定にして、
    # リトライを尽くしてもなお失敗し続けた場合に
    # AllProvidersExhaustedError が最終的に送出されることを確認する。
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
    # 日本語解説: このテストがとても重要な理由。全滅した(一度も成功しなかった)
    # 場合、StepMetricsは1つも作られない（成功パスを一度も通っていないので
    # 作りようがない）。しかし、失敗したとはいえ実際にHTTPリクエストは
    # 送信されているので、その回数(attempted_requests)は失われてはならない。
    # AllProvidersExhaustedErrorの例外オブジェクト自身がその回数を
    # attempted_requestsとして保持していて、Orchestrator側が
    # total_requests += exc.attempted_requests として拾い上げる、
    # という設計になっている。これが無いと「全滅した試行のtotal_requestsが
    # 0のまま記録される」というバグになる(実際にBENCHMARK_REPORT.mdに
    # 記録されている過去の不具合)。
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
    # 環境変数にAPIキーが1つも設定されていない状態でLLMClientを組み立てようと
    # すると、実際にリクエストを送る前の初期化段階でValueErrorになることを
    # 確認する。「キーが無いまま実行を進めて後から分かりにくいエラーになる」
    # のではなく、最初にはっきり失敗させるという設計判断。
    monkeypatch.delenv("MISSING_API_KEY", raising=False)
    spec = ProviderSpec("fake", "https://fake.example/v1", "MISSING_API_KEY")

    with pytest.raises(ValueError):
        client_module.LLMClient("fake-model", [spec])
