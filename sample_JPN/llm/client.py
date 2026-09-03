"""マルチプロバイダ対応LLMクライアント: トークンローテーション、プロバイダのフォールバック、
リトライ、使用量トラッキングを行う。

これはrequirements.mdの「現状の実装ギャップ」注記(Section 4.1)が明示的に指摘している部分:
generate()は生テキストだけでなく、StepMetricsを埋めるために十分なメタデータ
(トークン数、時間、api_url、model_name、retries)を返さなければならない。
"""
from __future__ import annotations  # 型注釈の評価を遅延させるためのfuture import

import time  # バックオフの待機時間計測に使うtimeモジュール
from dataclasses import dataclass, field  # データクラス定義と可変デフォルト値用のfield
from typing import List, Optional  # 型ヒント用のListとOptional

import requests  # HTTPリクエスト例外を捕捉するためのrequestsライブラリ

from config import ProviderSpec, resolve_provider  # プロバイダ設定の型と解決関数
from llm.provider import ChatProvider, GenerationResult, UsageStats  # プロバイダのプロトコルと結果・使用量の型
from llm.providers.gemini import GeminiProvider  # Gemini用の具体的プロバイダ実装
from llm.providers.openai_compatible import OpenAICompatibleProvider  # OpenAI互換API用の具体的プロバイダ実装


class AllProvidersExhaustedError(RuntimeError):
    """1回のgenerate()呼び出しで、設定された全てのAPIキー/プロバイダが失敗した場合に送出される。

    `attempted_requests` は例外を投げる前に設定される: 失敗した各試行は実際の
    HTTPリクエストであり、StepMetrics/SolutionOutput.total_requestsはそれを
    カウントしなければならない(Section 5.1の「リトライを含む、行われたLLM API
    リクエストの総数」)。この呼び出しは成功した生成が無いためStepMetricsの
    エントリを一切生成しないにもかかわらず、その回数は数えておく必要がある。
    """

    def __init__(self, message: str, attempted_requests: int = 0) -> None:
        super().__init__(message)  # RuntimeErrorの初期化にメッセージを渡す
        self.attempted_requests = attempted_requests  # 失敗も含めた試行済みリクエスト数を保持しておく


def _build_chat_provider(spec: ProviderSpec) -> ChatProvider:
    # プロバイダ仕様の種類(kind)に応じて、対応する具体的なChatProvider実装を生成するファクトリ関数
    if spec.kind == "gemini":  # Gemini形式のプロバイダなら
        return GeminiProvider(spec.base_url)  # GeminiProviderを構築して返す
    return OpenAICompatibleProvider(spec.base_url)  # それ以外はOpenAI互換プロバイダとして構築して返す


@dataclass
class _ProviderSlot:
    # 1つのプロバイダにつき、その仕様・実装・保有するAPIキー群・次に使うキーの位置をまとめて保持する内部データクラス
    spec: ProviderSpec  # プロバイダの静的な設定情報
    chat_provider: ChatProvider  # 実際にHTTPリクエストを行う具体的な実装
    api_keys: List[str]  # このプロバイダで利用可能な全APIキーのリスト
    next_key_index: int = field(default=0)  # 次にラウンドロビンで使用するキーのインデックス


class LLMClient:
    """1つ以上のプロバイダにまたがって、単一の論理モデルをフォールバック付きで呼び出すクライアント。

    プロバイダは与えられた順序で試行される。1つのプロバイダ内では、APIキーが
    ラウンドロビン方式でローテーションされるので、レート制限に達した1つのキーが
    実行全体を止めてしまうことがない(Section 4.6.1: 「複数トークン管理は必須」)。
    """

    def __init__(
        self,
        model_name: str,  # 呼び出し対象のモデル名(論理名、全プロバイダ共通で使われる)
        provider_specs: List[ProviderSpec],  # 試行するプロバイダ仕様のリスト(優先順)
        max_retries_per_key: int = 2,  # 1つのAPIキーあたりの最大リトライ回数
        backoff_seconds: float = 1.5,  # リトライ間のバックオフ待機時間の基準値(秒)
        request_timeout: float = 60.0,  # 1リクエストあたりのタイムアウト秒数
    ) -> None:
        self.model_name = model_name  # モデル名を保持
        self.max_retries_per_key = max_retries_per_key  # キーごとの最大リトライ回数を保持
        self.backoff_seconds = backoff_seconds  # バックオフの基準秒数を保持
        self.request_timeout = request_timeout  # リクエストタイムアウトを保持
        self.usage = UsageStats()  # 使用量集計オブジェクトを初期化

        self._slots: List[_ProviderSlot] = []  # 実際に使用可能な(APIキーが見つかった)プロバイダスロットのリスト
        for spec in provider_specs:  # 与えられた各プロバイダ仕様について
            keys = spec.collect_api_keys()  # そのプロバイダのAPIキーを環境変数から収集する
            if not keys:  # 1つもキーが見つからなければ
                continue  # このプロバイダはスキップする(使えないため)
            self._slots.append(
                _ProviderSlot(spec=spec, chat_provider=_build_chat_provider(spec), api_keys=keys)
            )  # 対応する具体的プロバイダ実装を構築し、スロットとして登録する

        if not self._slots:  # 使用可能なプロバイダが1つもなければ
            names = ", ".join(spec.name for spec in provider_specs)  # エラーメッセージ用にプロバイダ名を列挙
            raise ValueError(
                f"No API keys found for provider(s): {names}. "
                "Set them via .env or environment variables (see .env.example)."
            )  # APIキーが見つからなかった旨のエラーを送出する

    @classmethod
    def from_provider_url(cls, model_name: str, provider_url: str, **kwargs: object) -> "LLMClient":
        # 単一のプロバイダURLからプロバイダ仕様を解決し、それだけを使うLLMClientを生成するショートカット
        return cls(model_name, [resolve_provider(provider_url)], **kwargs)  # type: ignore[arg-type]

    def generate(
        self,
        messages: List[dict],  # LLMに送る会話履歴
        stop: Optional[List[str]] = None,  # 生成を止めるストップシーケンス(任意)
        max_output_tokens: int = 1024,  # 生成する最大トークン数
    ) -> GenerationResult:
        """補完を生成する。失敗時はキー/プロバイダをローテーションしながら再試行する。

        返り値の `.retries` は、成功した試行の前に行われた(キーとプロバイダを
        またいだ)全ての失敗試行の回数をカウントしており、StepMetrics.retriesの
        意味論と一致する。
        """
        last_error: Optional[Exception] = None  # 直近に発生した例外を保持する変数(全滅時のエラーメッセージ用)
        retries = 0  # ここまでの失敗試行回数のカウンタ

        for slot in self._slots:  # 登録されている各プロバイダを優先順に試す
            for _ in range(len(slot.api_keys)):  # そのプロバイダが持つ全キーの数だけループする(全キーを一巡させる)
                api_key = slot.api_keys[slot.next_key_index]  # 次に使うべきキーを取得
                slot.next_key_index = (slot.next_key_index + 1) % len(slot.api_keys)  # 次回のためにラウンドロビンでインデックスを進める

                for attempt in range(self.max_retries_per_key):  # このキーについて、最大リトライ回数まで試行する
                    try:
                        result = slot.chat_provider.chat(
                            messages=messages,
                            model=self.model_name,
                            api_key=api_key,
                            stop=stop,
                            max_output_tokens=max_output_tokens,
                            timeout=self.request_timeout,
                        )  # 実際にプロバイダへリクエストを送信して結果を取得する
                        result.retries = retries  # ここまでの失敗回数を結果オブジェクトに記録する
                        self.usage.record(result)  # 使用量集計に今回の成功結果を反映する
                        return result  # 成功したので即座に結果を返す(これ以降のループは実行しない)
                    except (requests.RequestException, KeyError, IndexError) as exc:  # HTTPエラーやレスポンス形式異常が発生した場合
                        last_error = exc  # 直近のエラーとして記録しておく(最終的な例外メッセージ用)
                        retries += 1  # 失敗試行回数をインクリメント
                        self.usage.errors.append(f"{slot.spec.name}: {exc}")  # どのプロバイダで何のエラーが起きたかを記録する
                        if attempt < self.max_retries_per_key - 1:  # まだこのキーでリトライ余地が残っていれば
                            time.sleep(self.backoff_seconds * (attempt + 1))  # 試行回数に応じて増加するバックオフ時間だけ待機する
                # このキーのリトライを使い果たした - 次のキー/プロバイダへフォールスルーする

        self.usage.total_requests += retries  # 全滅した場合でも、行った試行回数をリクエスト総数に加算しておく
        self.usage.total_retries += retries  # 同様にリトライ総数にも加算しておく
        raise AllProvidersExhaustedError(
            f"All providers/keys exhausted for model '{self.model_name}'. Last error: {last_error}",
            attempted_requests=retries,
        )  # 全てのプロバイダ/キーが尽きたことを示す例外を、試行回数と最後のエラーとともに送出する
