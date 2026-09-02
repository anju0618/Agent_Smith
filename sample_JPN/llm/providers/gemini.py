# ============================================================================
# 【日本語解説】llm/providers/gemini.py — Google AI Studio (Gemini) 専用実装
#
# なぜopenai_compatible.pyと別ファイルなのか: GeminiのREST APIは構造そのものが違う。
#   - エンドポイントが "/chat/completions" ではなく "/models/{model}:generateContent"
#   - 認証がAuthorizationヘッダーではなく、URLのクエリパラメータ "?key=..."
#   - リクエストボディが「messages」ではなく「contents」＋「parts」という入れ子構造
#   - レスポンスが「choices」ではなく「candidates」という入れ子構造
# この「本当に構造が違う2つ目のプロバイダ」がちゃんとChatProviderプロトコル
# （llm/provider.py）を満たせることで、「抽象化がOpenAI形式のラッパーに過ぎない」
# という状態を避けられている。
#
# 【最重要】このファイルには実際に起きたセキュリティインシデントへの対策が
# 埋め込まれている。詳しくは chat() 内の except ブロックのコメントを参照。
# ============================================================================
"""Chat completion client for Google AI Studio's Gemini REST API.

Kept separate from OpenAICompatibleProvider because Gemini's wire format
differs structurally (no /chat/completions path, API key as a query parameter,
"contents"/"parts" request schema, "candidates" response schema) - this is the
second, structurally different provider backing the multi-provider abstraction
required by Section 4.6, proving the abstraction isn't just an OpenAI-shaped
interface in disguise.
"""
from __future__ import annotations

import time
from typing import List, Optional, Tuple

import requests

from llm.provider import GenerationResult


class GeminiProvider:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    @staticmethod
    def _to_gemini_contents(messages: List[dict]) -> Tuple[Optional[str], List[dict]]:
        # 【日本語解説】
        # このプロジェクト内部では「OpenAI形式」のメッセージリスト
        # （{"role": "system"/"user"/"assistant", "content": "..."}）を共通形式として
        # 使っている（orchestrator.pyのmessagesがまさにこれ）。しかしGemini APIは
        # 独自の形式を要求するので、ここで変換する。
        #
        # Geminiの作法:
        #   - "system"ロールという概念がAPI上は無く、代わりに別フィールド
        #     "systemInstruction"としてリクエストのトップレベルに渡す必要がある。
        #     そのため、system役割のメッセージはcontentsリストには入れず、
        #     system_instruction変数として個別に取り出しておく。
        #   - assistant（アシスタント自身の過去発言）は Gemini では "model" という
        #     ロール名になる。それ以外（user）はそのまま "user"。
        #   - 各メッセージのテキストは {"parts": [{"text": "..."}]} という
        #     入れ子構造で包む必要がある。
        system_instruction = None
        contents = []
        for msg in messages:
            role = msg["role"]
            if role == "system":
                system_instruction = msg["content"]
                continue
            gemini_role = "model" if role == "assistant" else "user"
            contents.append({"role": gemini_role, "parts": [{"text": msg["content"]}]})
        return system_instruction, contents

    def chat(
        self,
        messages: List[dict],
        model: str,
        api_key: str,
        stop: Optional[List[str]],
        max_output_tokens: int,
        timeout: float,
    ) -> GenerationResult:
        system_instruction, contents = self._to_gemini_contents(messages)
        url = f"{self.base_url}/models/{model}:generateContent"

        # 【日本語解説】
        # stop sequenceや出力トークン上限は、Geminiでは"generationConfig"という
        # 専用オブジェクトの中にまとめて入れる（OpenAI互換APIのようにpayload直下ではない）。
        generation_config: dict = {"maxOutputTokens": max_output_tokens}
        if stop:
            generation_config["stopSequences"] = stop

        payload: dict = {"contents": contents, "generationConfig": generation_config}
        if system_instruction:
            payload["systemInstruction"] = {"parts": [{"text": system_instruction}]}

        start = time.monotonic()
        try:
            # 【日本語解説】
            # ここが構造上の最大の違い: 認証がHTTPヘッダーではなく、URLの
            # クエリパラメータ "?key=<APIキー>" として送られる
            # （params={"key": api_key} → requestsがURLに自動的に付加する）。
            # OpenAI互換プロバイダのように Authorization: Bearer ヘッダーを使う
            # 選択肢がGoogle AI Studioには無いため、この方式にせざるを得ない。
            # ただしこれが、下のexceptブロックで説明する漏洩リスクの直接の原因になる。
            response = requests.post(url, params={"key": api_key}, json=payload, timeout=timeout)
            response.raise_for_status()
        except requests.RequestException as exc:
            # ================================================================
            # 【日本語解説：最重要・実際のセキュリティインシデントへの対策】
            #
            # なぜこのtry/exceptが存在するのか、そしてなぜ絶対にこの部分の
            # ロジックを変更してはいけないのかを説明する。
            #
            # requestsライブラリ（およびその内部で使われるurllib3）は、
            # HTTPError・ConnectionError・Timeoutのデフォルトのエラーメッセージを
            # 「実際にリクエストした完全なURL」から自動的に組み立てる。
            # このGeminiプロバイダでは、そのURLには
            #   ?key=AIzaSy...(本物のAPIキー)...
            # というクエリパラメータが含まれている（上のrequests.post()参照）。
            #
            # もしここで例外を素通しにしてしまうと、そのエラーメッセージ文字列は
            # 次のような経路でファイルに書き出されてしまう:
            #
            #   requests.RequestException（キー入りURLを含む）
            #     → llm/client.py の AllProvidersExhaustedError のメッセージに連結される
            #     → orchestrator.py で SolutionOutput.error / StepMetrics.sandbox_output
            #       に代入される
            #     → agent_*.py が solution.json としてディスクに平文で書き出す
            #
            # 実際にこの経路で本物のAPIキーが3つのsolution.jsonファイルに
            # 漏洩する事故が起きた。GitHubへのpushを試みた際、GitHubの
            # push protection機能がAPIキーのパターンを検知してブロックしたことで
            # 初めて発覚した（詳細はBENCHMARK_REPORT.md参照）。
            #
            # 【対策の中身】
            # requests.RequestExceptionをそのまま再送出（re-raise）せず、
            # 必ずここで「キーを含まない情報」だけから新しいメッセージを
            # 組み立て直してから送出する:
            #   - `url`変数（=クエリパラメータを含まない、f-stringで組み立てた
            #     ベースURL+パスのみの文字列。requests.post()に渡した
            #     `params={"key": api_key}`はurlという変数そのものには
            #     含まれていないことに注意）
            #   - HTTPステータスコード（あれば）だけを添える
            # `from None`を付けているのは、元の例外（キー入りURLの情報を
            # 内部に保持している可能性がある）を例外チェーンからも切り離し、
            # トレースバック経由での漏洩リスクも断つため。
            #
            # ★★★ このtry/exceptブロックの中身は、キーを一切露出させない
            # という目的のために存在する。修正・簡略化する際は、
            # 「urlに実際のクエリパラメータ（?key=...)が含まれていないこと」を
            # 必ず再確認すること。★★★
            # ================================================================
            status = getattr(getattr(exc, "response", None), "status_code", None)
            status_part = f"status={status}" if status is not None else type(exc).__name__
            raise requests.RequestException(
                f"Gemini request failed ({status_part}) for url: {url} "
                "(query parameters, including the API key, redacted)"
            ) from None
        elapsed_ms = (time.monotonic() - start) * 1000
        data = response.json()

        # 【日本語解説】
        # Geminiのレスポンス形式: {"candidates": [{"content": {"parts": [{"text": "..."}]}}],
        # "usageMetadata": {"promptTokenCount": N, "candidatesTokenCount": M}}
        # partsが複数に分かれて返ってくることがあるため、joinで全部連結してから
        # 1つのテキストにまとめている。
        candidate = data["candidates"][0]
        parts = candidate.get("content", {}).get("parts", [])
        text = "".join(part.get("text", "") for part in parts)

        usage = data.get("usageMetadata", {})

        return GenerationResult(
            text=text,
            input_tokens=usage.get("promptTokenCount", 0),
            output_tokens=usage.get("candidatesTokenCount", 0),
            request_time_ms=elapsed_ms,
            api_url=self.base_url,
            model_name=model,
        )
