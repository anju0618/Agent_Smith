"""抽象LLMプロバイダインターフェースと、使用量トラッキング用の各種型。

Requirements.mdはまさにこの箇所を実装ギャップとして指摘している: `generate()`は
単なる生テキストだけではなく、StepMetricsを埋めるために十分なメタデータ
(トークン数、時間、api_url、model_name、retries)を返さなければならない。
以下のGenerationResultがその契約であり、全ての具体的プロバイダ
(openai_compatible.py、gemini.py)はこれを返す。
"""
from __future__ import annotations  # 型注釈の評価を遅延させるためのfuture import

from dataclasses import dataclass, field  # データクラス定義と可変デフォルト値用のfield
from typing import List, Optional, Protocol  # 型ヒント用のList、Optional、構造的部分型のProtocol


@dataclass
class GenerationResult:
    """1回のエージェントステップがStepMetrics(models.py)を埋めるために必要な全情報。"""

    text: str  # LLMが生成したテキスト本文
    input_tokens: int  # 入力(プロンプト)側のトークン数
    output_tokens: int  # 出力(生成結果)側のトークン数
    request_time_ms: float  # このリクエストにかかった実時間(ミリ秒)
    api_url: str  # リクエストを送信したAPIのベースURL
    model_name: str  # 使用したモデル名
    retries: int = 0  # 成功するまでにかかったリトライ回数(デフォルトは0)


class ChatProvider(Protocol):
    """プロバイダは (messages, model, api_key, ...) をGenerationResultに変換する方法を知っている。"""

    def chat(
        self,
        messages: List[dict],  # 会話履歴(role/contentの辞書のリスト)
        model: str,  # 使用するモデル名
        api_key: str,  # 認証に使うAPIキー
        stop: Optional[List[str]],  # 生成を止めるストップシーケンス(任意)
        max_output_tokens: int,  # 生成する最大トークン数
        timeout: float,  # リクエストのタイムアウト秒数
    ) -> GenerationResult:
        ...  # pragma: no cover - Protocolのため実装本体はここには存在しない


@dataclass
class UsageStats:
    """エージェント実行全体を通じた使用量の集計(Section 4.2 - 技術的制約:
    「トークン数、リトライ回数、レイテンシ、リクエスト数の使用量トラッキングを
    実装しなければならない」への対応)。"""

    total_requests: int = 0  # 送信したリクエストの総数(リトライ含む)
    total_retries: int = 0  # リトライの総回数
    total_input_tokens: int = 0  # 入力トークンの総数
    total_output_tokens: int = 0  # 出力トークンの総数
    total_latency_ms: float = 0.0  # 累積のレイテンシ(ミリ秒)
    errors: List[str] = field(default_factory=list)  # 発生したエラーメッセージの一覧(可変なのでdefault_factoryで初期化)

    def record(self, gen: GenerationResult) -> None:
        # 1回の成功した生成結果を受け取り、その分の集計を各カウンタに加算するメソッド
        self.total_requests += 1 + gen.retries  # 今回の成功リクエスト1回 + それまでの失敗(リトライ)回数を加算
        self.total_retries += gen.retries  # リトライ回数を累積に加算
        self.total_input_tokens += gen.input_tokens  # 入力トークン数を累積に加算
        self.total_output_tokens += gen.output_tokens  # 出力トークン数を累積に加算
        self.total_latency_ms += gen.request_time_ms  # レイテンシを累積に加算
