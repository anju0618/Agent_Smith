"""Google AI StudioのGemini REST API向けのチャット補完クライアント。

Geminiのワイヤーフォーマットは構造的に異なる(/chat/completionsパスがない、
APIキーはクエリパラメータとして渡す、"contents"/"parts"というリクエストスキーマ、
"candidates"というレスポンススキーマ)ため、OpenAICompatibleProviderとは分離して
実装している - これはSection 4.6で要求されているマルチプロバイダ抽象化を
裏付ける、構造的に異なる2つ目のプロバイダであり、この抽象化が単なる
OpenAI形式のインターフェースの偽装ではないことを証明している。
"""
from __future__ import annotations  # 型注釈の評価を遅延させるためのfuture import

import time  # リクエストの所要時間を計測するためのtimeモジュール
from typing import List, Optional, Tuple  # 型ヒント用のList、Optional、Tuple

import requests  # HTTPリクエストを送信するためのrequestsライブラリ

from llm.provider import GenerationResult  # 統一された生成結果の型


class GeminiProvider:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")  # 末尾のスラッシュを除去したベースURLを保持

    @staticmethod
    def _to_gemini_contents(messages: List[dict]) -> Tuple[Optional[str], List[dict]]:
        # OpenAI形式のmessagesリストを、Gemini API用の(systemInstruction, contents)形式に変換するヘルパー
        system_instruction = None  # システムメッセージの内容を保持する変数(見つからなければNoneのまま)
        contents = []  # Gemini形式の会話内容(contents)を蓄積するリスト
        for msg in messages:  # 各メッセージについて
            role = msg["role"]  # メッセージのロール(system/user/assistantなど)を取得
            if role == "system":  # システムロールのメッセージなら
                system_instruction = msg["content"]  # systemInstructionとして別扱いで保持する
                continue  # contentsには追加せず次のメッセージへ
            gemini_role = "model" if role == "assistant" else "user"  # assistantはGeminiでは"model"、それ以外は全て"user"にマッピング
            contents.append({"role": gemini_role, "parts": [{"text": msg["content"]}]})  # Gemini形式のcontentsエントリとして追加
        return system_instruction, contents  # システム指示文と会話内容のタプルを返す

    def chat(
        self,
        messages: List[dict],  # 会話履歴(role/contentの辞書のリスト、OpenAI形式)
        model: str,  # 使用するモデル名
        api_key: str,  # 認証用のAPIキー
        stop: Optional[List[str]],  # 生成を止めるストップシーケンス(任意)
        max_output_tokens: int,  # 生成する最大トークン数
        timeout: float,  # リクエストのタイムアウト秒数
    ) -> GenerationResult:
        system_instruction, contents = self._to_gemini_contents(messages)  # OpenAI形式のメッセージをGemini形式に変換する
        url = f"{self.base_url}/models/{model}:generateContent"  # Geminiのgenerate用エンドポイントURLを組み立てる

        generation_config: dict = {"maxOutputTokens": max_output_tokens}  # 生成設定(最大出力トークン数)を初期化
        if stop:  # ストップシーケンスが指定されていれば
            generation_config["stopSequences"] = stop  # Gemini形式のキー名で生成設定に追加する

        payload: dict = {"contents": contents, "generationConfig": generation_config}  # リクエストボディの基本部分を構築
        if system_instruction:  # システム指示があれば
            payload["systemInstruction"] = {"parts": [{"text": system_instruction}]}  # Gemini形式のsystemInstructionとして追加する

        start = time.monotonic()  # リクエスト開始時刻を記録(単調増加クロックを使用)
        try:
            response = requests.post(url, params={"key": api_key}, json=payload, timeout=timeout)  # APIキーをクエリパラメータとして付与しPOSTする
            response.raise_for_status()  # HTTPエラーステータスなら例外を送出する
        except requests.RequestException as exc:
            # ここで発生したrequests例外をそのまま伝播させてはならない: requestsとurllib3は
            # (HTTPError、ConnectionError、Timeoutいずれも)デフォルトのメッセージを
            # リクエストの *完全な* URLから組み立てるが、このプロバイダの場合そのURLには
            # `?key=...` というクエリパラメータとしてAPIキーが含まれている -
            # Geminiにはヘッダーベースの認証方法が存在しないためである。その文字列は
            # 最終的にAllProvidersExhaustedErrorのメッセージに入り、そこから
            # SolutionOutput.error / StepMetrics.sandbox_outputへとそのまま渡る -
            # つまりsolution.jsonとしてディスクに書き込まれてしまう - ので、それが
            # 漏洩する前にここで(キーを含むリクエスト/レスポンスのurlからではなく、
            # 常に`url`変数から)メッセージを再構築する。(実際に発生した事例: 本物の
            # APIキーがこの経路で3つのsolution.jsonファイルに残ってしまい、GitHubの
            # push protectionによってpushされる直前に検知された - 詳細は
            # BENCHMARK_REPORT.md参照)
            status = getattr(getattr(exc, "response", None), "status_code", None)  # 例外にレスポンスがあればそのステータスコードを取得
            status_part = f"status={status}" if status is not None else type(exc).__name__  # ステータスコードがあればそれを、なければ例外の型名を使う
            raise requests.RequestException(
                f"Gemini request failed ({status_part}) for url: {url} "
                "(query parameters, including the API key, redacted)"
            ) from None  # APIキーを含まない安全なメッセージで例外を再送出する(元の例外はチェーンしない)
        elapsed_ms = (time.monotonic() - start) * 1000  # 所要時間をミリ秒単位で計算する
        data = response.json()  # レスポンスボディをJSONとしてパースする

        candidate = data["candidates"][0]  # 最初の(唯一の)生成候補を取り出す
        parts = candidate.get("content", {}).get("parts", [])  # 候補の中のテキストパーツ一覧を取得(なければ空リスト)
        text = "".join(part.get("text", "") for part in parts)  # 全パーツのテキストを連結して1つの文字列にする

        usage = data.get("usageMetadata", {})  # トークン使用量メタデータを取得(なければ空辞書)

        # 統一されたGenerationResult形式に変換して返す
        return GenerationResult(
            text=text,  # 生成されたテキスト
            input_tokens=usage.get("promptTokenCount", 0),  # 入力トークン数(なければ0)
            output_tokens=usage.get("candidatesTokenCount", 0),  # 出力トークン数(なければ0)
            request_time_ms=elapsed_ms,  # リクエストにかかった時間(ミリ秒)
            api_url=self.base_url,  # 使用したAPIのベースURL
            model_name=model,  # 使用したモデル名
        )
