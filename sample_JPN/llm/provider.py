# ============================================================================
# 【日本語解説】llm/provider.py — LLMプロバイダの「共通インターフェース」を定義するファイル
#
# このプロジェクトは OpenRouter / Groq / Together / Fireworks / Google AI Studio
# など複数のLLMプロバイダに対応する必要がある。しかし各プロバイダはAPIのワイヤ形式
# （リクエストの組み立て方・レスポンスの読み方）がバラバラ。
# そこで「プロバイダが何であっても、呼び出し側（llm/client.py）から見れば
# 同じインターフェースで叩ける」ようにするための"契約"をこのファイルで定義している。
#
# ポイントは、単に「生成されたテキスト」だけを返せば良いわけではないということ。
# 課題要件（requirements.md）が明示的に指摘しているのは、「generate()はテキストだけでなく
# トークン数・応答時間・APIのURL・モデル名・リトライ回数まで返さなければならない」という点。
# なぜなら、これらの値がすべて models.py の StepMetrics（1ステップごとの計測記録）を
# 埋めるために必須だから。もしテキストしか返さなければ、後段の Orchestrator が
# 「このステップで何トークン使ったか」を一切知りようがなくなってしまう。
# ============================================================================
"""Abstract LLM provider interface and usage-tracking types.

Requirements.md flags an implementation gap for this exact spot: `generate()`
must return enough metadata to populate StepMetrics (tokens, timing, api_url,
model_name, retries), not just raw text. GenerationResult below is that
contract; every concrete provider (openai_compatible.py, gemini.py) returns one.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Protocol


@dataclass
class GenerationResult:
    """Everything one agent step needs to populate StepMetrics (models.py)."""
    # 【日本語解説】
    # LLMに1回問い合わせた結果をまるごと表すデータクラス。
    # openai_compatible.py / gemini.py という「中身が全く違う2つの実装」が、
    # どちらも最終的にこの同じ型を組み立てて返すことで、呼び出し側（llm/client.py の
    # LLMClient や orchestrator.py）はプロバイダの違いを一切気にしなくてよくなる。
    # フィールドはそのまま models.py の StepMetrics の対応フィールドに流し込まれる
    # （orchestrator.py で gen.text → llm_output、gen.input_tokens → input_tokens、
    #   というように1対1で対応している）。

    text: str              # LLMが生成した生のテキスト（Thought+Codeの全文、加工前）
    input_tokens: int      # このリクエストで消費した入力トークン数
    output_tokens: int     # このリクエストで生成された出力トークン数
    request_time_ms: float  # このAPIコール1回にかかった時間（ミリ秒、壁時計時間）
    api_url: str            # 実際に叩いたAPIのベースURL（どのプロバイダを使ったか後から追跡できるように）
    model_name: str          # 実際に使われたモデル識別子
    retries: int = 0          # このリクエストが成功するまでに何回リトライしたか（デフォルトは0=一発成功）


class ChatProvider(Protocol):
    """A provider knows how to turn (messages, model, api_key, ...) into a GenerationResult."""
    # 【日本語解説】
    # Python の typing.Protocol を使った「構造的部分型（ダックタイピングの静的版）」。
    # 継承を強制せず、「chat()という同じシグネチャのメソッドさえ持っていれば、
    # どんなクラスでもChatProviderとして扱ってよい」というインターフェース定義。
    # 実際にこのプロトコルを満たすクラスは openai_compatible.py の
    # OpenAICompatibleProvider と gemini.py の GeminiProvider の2つ。

    def chat(
        self,
        messages: List[dict],           # 会話履歴（{"role": "system"/"user"/"assistant", "content": "..."}のリスト）
        model: str,                      # 呼び出すモデル名
        api_key: str,                     # 認証に使うAPIキー（1回の呼び出しにつき1本、呼び出し側でローテーション済み）
        stop: Optional[List[str]],         # 生成を打ち切るstop sequence（例: ["<end_code>"]）
        max_output_tokens: int,             # 出力トークン数の上限
        timeout: float,                      # HTTPリクエストのタイムアウト秒数
    ) -> GenerationResult:
        ...  # pragma: no cover - Protocol
        # 【日本語解説】本体を持たない（Protocolなので実装は各サブクラス側にある）


@dataclass
class UsageStats:
    """Aggregate usage tracking across a whole agent run (Section 4.2 - Technical
    Constraints: "you must implement usage tracking: tokens, retries, latency,
    requests")."""
    # 【日本語解説】
    # 1回のLLM呼び出し分ではなく、「エージェントが1タスクを解く間ずっと」の
    # 累積使用量を集計するためのクラス。LLMClient（llm/client.py）が1個だけ保持し、
    # generate()が呼ばれるたびに record() で加算していく。
    # 課題要件「トークン数・リトライ回数・レイテンシ・リクエスト数の使用量トラッキングを
    # 実装しなければならない」に対応する実体がこれ。

    total_requests: int = 0      # これまでの累積HTTPリクエスト数（成功・失敗問わず、リトライも1件として数える）
    total_retries: int = 0        # 累積リトライ回数
    total_input_tokens: int = 0    # 累積入力トークン数
    total_output_tokens: int = 0    # 累積出力トークン数
    total_latency_ms: float = 0.0    # 累積レイテンシ（ミリ秒）
    errors: List[str] = field(default_factory=list)  # 発生したエラーメッセージの履歴（プロバイダ名付き）

    def record(self, gen: GenerationResult) -> None:
        # 【日本語解説】
        # 1回の成功したLLM呼び出し結果（GenerationResult）を受け取り、
        # 各累積カウンタに加算する。「+= 1 + gen.retries」がポイント:
        # 例えばリトライが2回あって3回目で成功した場合、実際に送られたHTTPリクエストは
        # 3本（失敗2回＋成功1回）なので、「1（成功分）+ retries（失敗分）」で
        # 正しくリクエスト総数をカウントできる。
        self.total_requests += 1 + gen.retries
        self.total_retries += gen.retries
        self.total_input_tokens += gen.input_tokens
        self.total_output_tokens += gen.output_tokens
        self.total_latency_ms += gen.request_time_ms
