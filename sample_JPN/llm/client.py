# ============================================================================
# 【日本語解説】llm/client.py — 複数プロバイダ・複数APIキーをまたいで
# 「1つの論理モデル」を呼び出すための司令塔
#
# orchestrator.py はこのファイルの LLMClient.generate() を呼ぶだけで、
# 「どのプロバイダのどのキーを使うか」「失敗したらどう切り替えるか」は
# 一切気にしなくてよい。このファイルがその複雑さを丸ごと隠蔽している。
#
# 全体の考え方（2段階のフォールバック構造）:
#   1. プロバイダのフォールバック: 与えられたプロバイダ（例: Groq → OpenRouter）を
#      順番に試し、あるプロバイダが完全にダメなら次のプロバイダへ。
#   2. プロバイダ内でのキーのローテーション: 1つのプロバイダに複数APIキーが
#      登録されていれば（config.py の collect_api_keys() 参照）、ラウンドロビンで
#      順番に使う。1本のキーがレート制限に引っかかっても、別のキーで即座に続行できる。
# ============================================================================
"""Multi-provider LLM client: token rotation, provider fallback, retries, usage tracking.

This is the piece requirements.md's "current implementation gap" note (Section
4.1) calls out explicitly: generate() must return enough metadata to populate
StepMetrics (tokens, timing, api_url, model_name, retries), not just raw text.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import List, Optional

import requests

from config import ProviderSpec, resolve_provider
from llm.provider import ChatProvider, GenerationResult, UsageStats
from llm.providers.gemini import GeminiProvider
from llm.providers.openai_compatible import OpenAICompatibleProvider


class AllProvidersExhaustedError(RuntimeError):
    """Raised when every configured API key/provider failed for one generate() call.

    `attempted_requests` is set before raising: every failed attempt was a
    real HTTP request, and StepMetrics/SolutionOutput.total_requests must
    count it (Section 5.1's "Total number of LLM API requests made,
    including retries") even though this call never produced a StepMetrics
    entry (there's no successful generation to attach it to).
    """
    # 【日本語解説】
    # 全プロバイダ・全キー・全リトライを使い果たしても1回も成功しなかった場合に
    # 送出される例外。ここで重要なのは attempted_requests という属性を
    # 明示的に持たせていること。
    #
    # なぜこれが必要か: 通常、成功したLLM呼び出しは StepMetrics として記録され、
    # そこに含まれる retries フィールドが「何回失敗したあと成功したか」を教えてくれる。
    # しかし全滅した場合は StepMetrics 自体が1個も作られない（成功した呼び出しが
    # 無いので、記録すべき「ステップ」自体が存在しない）。
    # それでも「実際に何本のHTTPリクエストを送ったか」という事実は消してはいけない
    # （課題要件: total_requests は「リトライを含む、送った全リクエスト数」）。
    # そこでこの例外自体に attempted_requests を持たせ、orchestrator.py 側で
    # `total_requests += exc.attempted_requests` として拾えるようにしている。
    # ※ これが実装されていなかったために「全滅した試行の total_requests が
    #    0のまま記録されてしまう」というバグが実際にあった（BENCHMARK_REPORT.md参照）。

    def __init__(self, message: str, attempted_requests: int = 0) -> None:
        super().__init__(message)
        self.attempted_requests = attempted_requests


def _build_chat_provider(spec: ProviderSpec) -> ChatProvider:
    # 【日本語解説】
    # ProviderSpec.kind（"openai_compatible" か "gemini"）を見て、対応する
    # ChatProvider実装（Section 9.3 / 9.4）のインスタンスを組み立てるファクトリ関数。
    # ここが「新しいワイヤ形式のプロバイダを追加したければ、ここに分岐を1個足すだけでよい」
    # という拡張ポイントになっている。
    if spec.kind == "gemini":
        return GeminiProvider(spec.base_url)
    return OpenAICompatibleProvider(spec.base_url)


@dataclass
class _ProviderSlot:
    # 【日本語解説】
    # 「1つのプロバイダ」に関する実行時状態をまとめて持つ内部クラス。
    # spec: 静的な設定（config.pyのProviderSpec）
    # chat_provider: 実際にHTTPリクエストを組み立てる実装オブジェクト
    # api_keys: このプロバイダに登録されている全APIキーのリスト（1本以上）
    # next_key_index: 次に使うべきキーのインデックス（ラウンドロビンのカーソル）
    spec: ProviderSpec
    chat_provider: ChatProvider
    api_keys: List[str]
    next_key_index: int = field(default=0)


class LLMClient:
    """Calls a single logical model across one or more providers with fallback.

    Providers are tried in the order given. Within a provider, API keys rotate
    round-robin so one rate-limited key doesn't stall the whole run (Section
    4.6.1: "multi-token management is mandatory").
    """

    def __init__(
        self,
        model_name: str,
        provider_specs: List[ProviderSpec],
        max_retries_per_key: int = 2,       # 1本のキーにつき、失敗時に何回までリトライするか
        backoff_seconds: float = 1.5,        # リトライ間隔の基準秒数（線形バックオフの単位）
        request_timeout: float = 60.0,        # 1回のHTTPリクエストのタイムアウト秒数
    ) -> None:
        self.model_name = model_name
        self.max_retries_per_key = max_retries_per_key
        self.backoff_seconds = backoff_seconds
        self.request_timeout = request_timeout
        self.usage = UsageStats()  # このLLMClientインスタンスが生きている間ずっと累積する使用量統計

        # 【日本語解説】
        # 渡された各プロバイダについて、実際にAPIキーが（.env等に）設定されているものだけを
        # 「使えるスロット」として登録する。キーが1つも無いプロバイダはそもそも
        # 候補にすら入れない（無駄なリクエストを試みて即失敗する、を避けるため）。
        self._slots: List[_ProviderSlot] = []
        for spec in provider_specs:
            keys = spec.collect_api_keys()
            if not keys:
                continue
            self._slots.append(
                _ProviderSlot(spec=spec, chat_provider=_build_chat_provider(spec), api_keys=keys)
            )

        # 【日本語解説】
        # 1つも使えるプロバイダが無ければ、その場で即座にエラーにする。
        # 「キーが無いまま実行を始めて、初回のLLM呼び出しで初めて失敗に気づく」より、
        # 起動直後にはっきり失敗を教えたほうが親切、という判断。
        if not self._slots:
            names = ", ".join(spec.name for spec in provider_specs)
            raise ValueError(
                f"No API keys found for provider(s): {names}. "
                "Set them via .env or environment variables (see .env.example)."
            )

    @classmethod
    def from_provider_url(cls, model_name: str, provider_url: str, **kwargs: object) -> "LLMClient":
        # 【日本語解説】
        # agent_mbpp.py / agent_swebench.py の --provider-url 引数から直接
        # LLMClientを組み立てるための便利コンストラクタ。
        # config.py の resolve_provider() に丸投げしているだけ ── 既知のプロバイダなら
        # レジストリからそのまま拾い、未知のURLなら即席のProviderSpecを合成してくれる。
        return cls(model_name, [resolve_provider(provider_url)], **kwargs)  # type: ignore[arg-type]

    def generate(
        self,
        messages: List[dict],
        stop: Optional[List[str]] = None,
        max_output_tokens: int = 1024,
    ) -> GenerationResult:
        """Generate a completion, rotating keys/providers on failure.

        The returned result's `.retries` counts every failed attempt (across
        keys and providers) before the successful one, matching
        StepMetrics.retries semantics.
        """
        last_error: Optional[Exception] = None
        retries = 0  # プロバイダ・キーをまたいでも通算でカウントし続ける失敗回数

        # 【日本語解説】三重ループの全体構造:
        #   外側: プロバイダを順番に試す（例: 1番目のプロバイダがGroq、2番目がOpenRouter）
        #   中側: そのプロバイダの中で、APIキーをラウンドロビンで1本ずつ試す
        #   内側: 同じキーで、最大 max_retries_per_key 回までリトライする
        for slot in self._slots:
            for _ in range(len(slot.api_keys)):
                # 【日本語解説】
                # next_key_index が指すキーを取り出し、次回呼び出しのために
                # インデックスを1つ進めておく（% len(...) で末尾から先頭へ循環する）。
                # これにより、同じLLMClientインスタンスに対する複数回のgenerate()呼び出しが
                # 毎回違うキーを使うようになり、レート制限を分散できる。
                api_key = slot.api_keys[slot.next_key_index]
                slot.next_key_index = (slot.next_key_index + 1) % len(slot.api_keys)

                for attempt in range(self.max_retries_per_key):
                    try:
                        # 【日本語解説】ここが実際にHTTPリクエストを飛ばす箇所。
                        # slot.chat_provider は OpenAICompatibleProvider か
                        # GeminiProvider のどちらか（_build_chat_providerで決まっている）。
                        result = slot.chat_provider.chat(
                            messages=messages,
                            model=self.model_name,
                            api_key=api_key,
                            stop=stop,
                            max_output_tokens=max_output_tokens,
                            timeout=self.request_timeout,
                        )
                        # 【日本語解説】
                        # 成功した! ここまでに失敗した回数（retries）を結果に刻み込み、
                        # 累積使用量統計（usage）に加算してから返す。
                        result.retries = retries
                        self.usage.record(result)
                        return result
                    except (requests.RequestException, KeyError, IndexError) as exc:
                        # 【日本語解説】
                        # requests.RequestException: HTTPエラー・タイムアウト・接続エラーなど
                        #   ネットワーク/プロトコルレベルの失敗全般。
                        # KeyError / IndexError: レスポンスJSONの形が想定と違った場合
                        #   （例: choices配列が空、usageフィールドが無い等）に、
                        #   辞書アクセスやインデックスアクセスで自然に発生する例外を
                        #   ここでまとめて「失敗の1種」として扱っている。
                        last_error = exc
                        retries += 1
                        self.usage.errors.append(f"{slot.spec.name}: {exc}")
                        if attempt < self.max_retries_per_key - 1:
                            # 【日本語解説】
                            # 最後の試行でなければ、少し待ってからリトライする。
                            # 「backoff_seconds * (attempt + 1)」は線形バックオフ
                            # （1回目の待ちはbackoff_seconds×1、2回目はbackoff_seconds×2、
                            #  というように失敗するたびに待ち時間を延ばしていく）。
                            # 一時的な過負荷やレート制限が解消されるのを期待して待つ。
                            time.sleep(self.backoff_seconds * (attempt + 1))
                # 【日本語解説】
                # このキーでのリトライを使い果たした。例外は投げずに自然にループを抜け、
                # 外側のfor文へ戻って「次のキー」を試す（同じプロバイダ内で次のキーへ）。

        # 【日本語解説】
        # ここに到達するのは「全プロバイダ・全キー・全リトライがすべて失敗した」場合のみ。
        # 成功したStepMetricsは1つも無いが、実際に送ったHTTPリクエストの本数（retries）は
        # 使用量統計に反映し、AllProvidersExhaustedErrorにも刻んで呼び出し元に伝える。
        self.usage.total_requests += retries
        self.usage.total_retries += retries
        raise AllProvidersExhaustedError(
            f"All providers/keys exhausted for model '{self.model_name}'. Last error: {last_error}",
            attempted_requests=retries,
        )
