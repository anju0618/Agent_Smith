"""Agent Smithのための環境変数・プロバイダ設定読み込みモジュール。

.envの読み込みと既知のLLMプロバイダのレジストリをここに集約することで、
コードベースの他の部分がos.environに直接触れないようにする。これは
「General Rules」の要件("APIキーのハードコード禁止、全て環境変数/.envファイルから")を
アーキテクチャ的に実際に強制している箇所である。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv

_ENV_LOADED = False


def load_env() -> None:
    """.envを一度だけ読み込む(冪等)。シェルで既に設定されている変数は決して上書きしない。"""
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    env_path = Path(__file__).resolve().parent / ".env"
    load_dotenv(dotenv_path=env_path, override=False)
    _ENV_LOADED = True


load_env()


@dataclass(frozen=True)
class ProviderSpec:
    """1つのLLMプロバイダに関する静的な記述(Section 5.6 - マルチプロバイダ対応)。"""

    name: str
    base_url: str
    api_key_env_prefix: str
    kind: str = "openai_compatible"
    fallback_model: Optional[str] = None


    def collect_api_keys(self) -> List[str]:
        """このプロバイダに設定されている全てのAPIキーを収集する。

        複数トークン管理(Section 5.6.1)に対応: OPENROUTER_API_KEY、
        OPENROUTER_API_KEY_2、OPENROUTER_API_KEY_3、...が全て拾われるので、
        LLMクライアントは1つのキーがレート制限に達した際に他のキーへ
        ローテーションできる。
        """
        keys = []
        primary = os.environ.get(self.api_key_env_prefix)
        if primary:
            keys.append(primary)
        index = 2
        while True:
            value = os.environ.get(f"{self.api_key_env_prefix}_{index}")
            if not value:
                break
            keys.append(value)
            index += 1
        return keys


KNOWN_PROVIDERS: List[ProviderSpec] = [
    ProviderSpec(
        "openrouter", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY", "openai_compatible",


        fallback_model="nvidia/nemotron-3-super-120b-a12b:free",
    ),
    ProviderSpec(
        "groq", "https://api.groq.com/openai/v1", "GROQ_API_KEY", "openai_compatible",
        fallback_model="openai/gpt-oss-120b",
    ),

    ProviderSpec("together", "https://api.together.xyz/v1", "TOGETHER_API_KEY", "openai_compatible"),
    ProviderSpec(
        "fireworks", "https://api.fireworks.ai/inference/v1", "FIREWORKS_API_KEY", "openai_compatible"
    ),
    ProviderSpec(
        "google_ai_studio",
        "https://generativelanguage.googleapis.com/v1beta",
        "GOOGLE_AI_STUDIO_API_KEY",
        "gemini",
        fallback_model="gemini-flash-lite-latest",
    ),
]


DEFAULT_MODEL_NAME = "gemini-flash-lite-latest"
DEFAULT_PROVIDER_URL = "https://generativelanguage.googleapis.com/v1beta"


def _env_var_from_url(base_url: str) -> str:
    """一覧にないプロバイダに対して、<HOST>_API_KEY形式の環境変数名をベストエフォートで推測する。"""
    host = re.sub(r"^https?://", "", base_url).split("/")[0]
    host = re.sub(r"[^a-zA-Z0-9]+", "_", host).strip("_").upper()
    return f"{host}_API_KEY"


def resolve_provider(base_url: str) -> ProviderSpec:
    """--provider-urlを既知のレジストリと照合し、なければ汎用のProviderSpecを合成する。

    "他のプロバイダも...プロジェクト要件を満たす限り使用可能"(Section 5.6)という
    要求を満たすため、OpenAI互換のベースURLであれば、慣例に従った<HOST>_API_KEY
    環境変数さえ設定されていれば、そのまま動作するようにする。
    """
    normalized = base_url.rstrip("/")
    for spec in KNOWN_PROVIDERS:
        spec_url = spec.base_url.rstrip("/")
        if normalized == spec_url or normalized.startswith(spec_url):
            return spec
    return ProviderSpec(
        name=normalized,
        base_url=base_url,
        api_key_env_prefix=_env_var_from_url(base_url),
        kind="openai_compatible",
    )
