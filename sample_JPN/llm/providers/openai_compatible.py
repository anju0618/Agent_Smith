"""任意のOpenAI互換 /chat/completions エンドポイント向けのチャット補完クライアント。

OpenRouter、Groq、Together AI、Fireworks AI、その他Section 4.6.1に列挙された
ほとんどの無料枠プロバイダをカバーする - これらは全て同じワイヤーフォーマットを
話すため、まさにそれゆえにマルチプロバイダ抽象化(Section 4.6)をこれほど薄く
保てる。
"""
from __future__ import annotations  # 型注釈の評価を遅延させるためのfuture import

import time  # リクエストの所要時間を計測するためのtimeモジュール
from typing import List, Optional  # 型ヒント用のListとOptional

import requests  # HTTPリクエストを送信するためのrequestsライブラリ

from llm.provider import GenerationResult  # 統一された生成結果の型


class OpenAICompatibleProvider:
    def __init__(self, base_url: str, chat_path: str = "/chat/completions") -> None:
        self.base_url = base_url.rstrip("/")  # 末尾のスラッシュを除去したベースURLを保持
        self.chat_path = chat_path  # チャット補完エンドポイントのパス(デフォルトは"/chat/completions")

    def chat(
        self,
        messages: List[dict],  # 会話履歴(role/contentの辞書のリスト)
        model: str,  # 使用するモデル名
        api_key: str,  # 認証用のAPIキー
        stop: Optional[List[str]],  # 生成を止めるストップシーケンス(任意)
        max_output_tokens: int,  # 生成する最大トークン数
        timeout: float,  # リクエストのタイムアウト秒数
    ) -> GenerationResult:
        url = f"{self.base_url}{self.chat_path}"  # ベースURLとチャットパスを連結してリクエスト先URLを組み立てる
        # OpenAI互換APIへ送信するリクエストボディを構築する
        payload: dict = {
            "model": model,
            "messages": messages,
            "max_tokens": max_output_tokens,
        }
        if stop:  # ストップシーケンスが指定されていれば
            payload["stop"] = stop  # ペイロードに追加する

        # 認証ヘッダとコンテンツタイプを設定するHTTPヘッダ
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        start = time.monotonic()  # リクエスト開始時刻を記録(単調増加クロックを使用)
        response = requests.post(url, json=payload, headers=headers, timeout=timeout)  # POSTリクエストを送信する
        elapsed_ms = (time.monotonic() - start) * 1000  # 所要時間をミリ秒単位で計算する
        response.raise_for_status()  # HTTPエラーステータスなら例外を送出する
        data = response.json()  # レスポンスボディをJSONとしてパースする

        choice = data["choices"][0]  # 最初の(唯一の)候補を取り出す
        text = choice["message"].get("content") or ""  # 生成されたテキスト本文を取得(なければ空文字列)
        usage = data.get("usage", {})  # トークン使用量情報を取得(なければ空辞書)

        # 統一されたGenerationResult形式に変換して返す
        return GenerationResult(
            text=text,  # 生成されたテキスト
            input_tokens=usage.get("prompt_tokens", 0),  # 入力トークン数(なければ0)
            output_tokens=usage.get("completion_tokens", 0),  # 出力トークン数(なければ0)
            request_time_ms=elapsed_ms,  # リクエストにかかった時間(ミリ秒)
            api_url=self.base_url,  # 使用したAPIのベースURL
            model_name=model,  # 使用したモデル名
        )
