# ============================================================================
# 【日本語解説】llm/providers/openai_compatible.py
# — OpenAI互換の /chat/completions を話すプロバイダ共通実装
#
# OpenRouter・Groq・Together AI・Fireworks AI などは、それぞれ別会社・別サービスだが
# 「APIの喋り方」がほぼ同じ（OpenAIが最初に定めた/chat/completionsという
# エンドポイント形式をそのまま踏襲している）。だからこのファイル1つで
# それら全部をカバーできる。逆に言うと、Google Gemini（gemini.py）のように
# 喋り方が根本的に違うプロバイダは、このファイルでは対応できず別実装が必要になる。
# ============================================================================
"""Chat completion client for any OpenAI-compatible /chat/completions endpoint.

Covers OpenRouter, Groq, Together AI, Fireworks AI, and most other free-tier
providers listed in Section 4.6.1 - they all speak the same wire format, which
is exactly why the multi-provider abstraction (Section 4.6) can stay this thin.
"""
from __future__ import annotations

import time
from typing import List, Optional

import requests

from llm.provider import GenerationResult


class OpenAICompatibleProvider:
    # 【日本語解説】
    # llm/provider.py の ChatProvider プロトコルを満たす実装その1。
    # base_url（例: "https://openrouter.ai/api/v1"）と、叩くパス
    # （デフォルトは"/chat/completions"）を保持するだけの薄いクラス。
    def __init__(self, base_url: str, chat_path: str = "/chat/completions") -> None:
        self.base_url = base_url.rstrip("/")  # 末尾スラッシュを除去しておき、urlの二重スラッシュを防ぐ
        self.chat_path = chat_path

    def chat(
        self,
        messages: List[dict],
        model: str,
        api_key: str,
        stop: Optional[List[str]],
        max_output_tokens: int,
        timeout: float,
    ) -> GenerationResult:
        # 【日本語解説】実際にAPIを1回叩く本体。流れは素直な3ステップ:
        # 1. リクエストペイロードを組み立てる
        # 2. requests.post()で送信する
        # 3. レスポンスJSONから必要な値を取り出し、GenerationResultに詰め替える

        url = f"{self.base_url}{self.chat_path}"
        payload: dict = {
            "model": model,
            "messages": messages,           # {"role": ..., "content": ...} のリストをそのまま渡す
            "max_tokens": max_output_tokens,
        }
        if stop:
            # 【日本語解説】
            # stop（例: ["<end_code>"]）が指定されていれば、そのままpayloadに含める。
            # 未指定（None）や空リストなら、payloadに"stop"キー自体を含めない
            # （一部のプロバイダは空リストを渡すとエラーになることがあるための配慮）。
            payload["stop"] = stop

        headers = {
            "Authorization": f"Bearer {api_key}",  # OpenAI互換APIの標準的な認証方式（Bearerトークン）
            "Content-Type": "application/json",
        }

        # 【日本語解説】
        # time.monotonic()はシステム時計の変更（NTP補正など）の影響を受けない
        # 「単調増加する時計」。壁時計時間の計測にtime.time()ではなくこちらを使うのは、
        # リクエスト中に時刻同期が走っても計測値が狂わないようにするため。
        start = time.monotonic()
        response = requests.post(url, json=payload, headers=headers, timeout=timeout)
        elapsed_ms = (time.monotonic() - start) * 1000
        response.raise_for_status()  # HTTPステータスが4xx/5xxならここで例外(HTTPError)を投げる
        data = response.json()

        # 【日本語解説】
        # OpenAI互換APIのレスポンス形式: {"choices": [{"message": {"content": "..."}}],
        # "usage": {"prompt_tokens": N, "completion_tokens": M}}
        # choices[0]だけを見ているのは、agentは1回のリクエストにつき1つの応答候補
        # （n=1相当）しか要求していないため。
        choice = data["choices"][0]
        text = choice["message"].get("content") or ""  # contentがNoneの場合に備えて空文字にフォールバック
        usage = data.get("usage", {})  # usageフィールド自体が無いプロバイダ実装があっても壊れないように.get()で防御

        return GenerationResult(
            text=text,
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
            request_time_ms=elapsed_ms,
            api_url=self.base_url,
            model_name=model,
        )
