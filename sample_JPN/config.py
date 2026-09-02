"""Environment and provider configuration loading for Agent Smith.

Centralizes .env loading and the registry of known LLM providers so the rest
of the codebase never touches os.environ directly. This is where the General
Rules requirement ("no hardcoded API keys, everything from environment
variables / .env files") is actually enforced architecturally.
"""
# ============================================================================
# 【日本語解説】このファイルの存在理由
# ============================================================================
# 「os.environ に直接触るコードをこのファイル1つに集約し、他のどこにも
# APIキーをハードコードさせないための建て付け」そのものがこのファイルの
# 目的。load_env() / ProviderSpec / KNOWN_PROVIDERS / resolve_provider() の
# 4点だけで構成されている。
# ============================================================================
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List

from dotenv import load_dotenv

_ENV_LOADED = False
# モジュールレベルのフラグ。config モジュールを複数箇所からimportしても
# python-dotenv による .env 読み込みが何度も走らないようにするための冪等化フラグ。


def load_env() -> None:
    """Load .env once (idempotent). Never overrides variables already set in the shell."""
    # -------------------------------------------------------------------
    # 【日本語解説】load_env() — 冪等な .env 読み込み
    # -------------------------------------------------------------------
    # _ENV_LOADED が既に True ならすぐ return する（1プロセス内では1回だけ
    # 読み込む）。override=False が地味に重要で、CIやDocker実行時にシェル側で
    # 既に環境変数がセットされている場合、リポジトリに置かれた .env の値で
    # 上書きしてしまわないようにするための安全策。
    # -------------------------------------------------------------------
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    env_path = Path(__file__).resolve().parent / ".env"
    # .env は常にこのファイル（config.py）と同じディレクトリ、つまり sample/
    # 直下に置かれている前提。呼び出し元のカレントディレクトリに依存しない。
    load_dotenv(dotenv_path=env_path, override=False)
    _ENV_LOADED = True


load_env()
# モジュールが import された瞬間に1回だけ自動実行される。これにより、
# config を import した時点で以降のコードは os.environ から安全にAPIキーを
# 読める状態になっている。


@dataclass(frozen=True)
class ProviderSpec:
    """Static description of one LLM provider (Section 5.6 - multi-provider support)."""
    # -------------------------------------------------------------------
    # 【日本語解説】ProviderSpec = 1プロバイダの静的定義
    # -------------------------------------------------------------------
    # frozen=True なので生成後にフィールドを変更できないイミュータブルな
    # データクラス。1つのLLMプロバイダ（OpenRouter, Groq, ...）を表す。
    # -------------------------------------------------------------------

    name: str
    # プロバイダの識別名（例: "openrouter"）。
    base_url: str
    # そのプロバイダのAPIベースURL。
    api_key_env_prefix: str
    # APIキーを読み込む環境変数名のプレフィックス（例: "OPENROUTER_API_KEY"）。
    kind: str = "openai_compatible"  # or "gemini"
    # ワイヤ形式の種類。llm/client.py がこの値を見て、どちらの ChatProvider
    # 実装（openai_compatible.py か gemini.py）を使うかを決める。

    def collect_api_keys(self) -> List[str]:
        """Collect every API key configured for this provider.

        Supports multi-token management (Section 5.6.1): OPENROUTER_API_KEY,
        OPENROUTER_API_KEY_2, OPENROUTER_API_KEY_3, ... are all picked up so the
        LLM client can rotate between them when one hits a rate limit.
        """
        # ---------------------------------------------------------------
        # 【日本語解説】複数トークンのローテーション対応
        # ---------------------------------------------------------------
        # 「複数トークン管理が必須」という要件をここ1箇所で満たしている。
        # 例えば .env に
        #     GROQ_API_KEY=key_a
        #     GROQ_API_KEY_2=key_b
        #     GROQ_API_KEY_3=key_c
        # と書けば、この関数は ["key_a", "key_b", "key_c"] を返す。これが
        # llm/client.py の LLMClient に渡り、1つのキーがレート制限に当たっても
        # 別のキーへ自動的にローテーションする土台になる。
        #
        # 番号が飛んでいる場合（_2は無いが_3はある、など）は、_2が見つからな
        # かった時点で while ループが break するので、そこで収集は打ち切られる
        # ——「歯抜けの番号は末尾切り捨て」という単純な仕様。
        # ---------------------------------------------------------------
        keys = []
        primary = os.environ.get(self.api_key_env_prefix)
        # まずサフィックス無しの主キー（例: GROQ_API_KEY）を試す。
        if primary:
            keys.append(primary)
        index = 2
        while True:
            # index=2 から順に GROQ_API_KEY_2, GROQ_API_KEY_3, ... を探索。
            value = os.environ.get(f"{self.api_key_env_prefix}_{index}")
            if not value:
                break
            keys.append(value)
            index += 1
        return keys


# Known free-tier providers (Section 5.6.1 - illustrative and non-exhaustive).
# Add more ProviderSpec entries here to support additional providers.
# ---------------------------------------------------------------------------
# 【日本語解説】KNOWN_PROVIDERS — 既知の無料枠プロバイダのレジストリ
# ---------------------------------------------------------------------------
# 5行のリテラルなリストで、新しいプロバイダを「公式サポート」扱いにしたければ
# ここに1行足すだけでよい。kind フィールドが "openai_compatible" か "gemini" かで
# llm/client.py がどちらの ChatProvider 実装を使うかが決まる。
# ---------------------------------------------------------------------------
KNOWN_PROVIDERS: List[ProviderSpec] = [
    ProviderSpec("openrouter", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY", "openai_compatible"),
    ProviderSpec("groq", "https://api.groq.com/openai/v1", "GROQ_API_KEY", "openai_compatible"),
    ProviderSpec("together", "https://api.together.xyz/v1", "TOGETHER_API_KEY", "openai_compatible"),
    ProviderSpec(
        "fireworks", "https://api.fireworks.ai/inference/v1", "FIREWORKS_API_KEY", "openai_compatible"
    ),
    ProviderSpec(
        "google_ai_studio",
        "https://generativelanguage.googleapis.com/v1beta",
        "GOOGLE_AI_STUDIO_API_KEY",
        "gemini",
        # Gemini だけ kind="gemini" — エンドポイント形式・認証方式・
        # リクエスト/レスポンスのJSONスキーマが根本的に異なるため、
        # llm/providers/gemini.py という専用実装で扱われる。
    ),
]


def _env_var_from_url(base_url: str) -> str:
    """Best-effort guess of a <HOST>_API_KEY env var name for an unlisted provider."""
    # ---------------------------------------------------------------
    # 【日本語解説】URLから環境変数名を機械的に組み立てる
    # ---------------------------------------------------------------
    # 例: "https://api.novita.ai/v3/openai" というURLが渡されたら
    #   1. "https://" を取り除く → "api.novita.ai/v3/openai"
    #   2. "/" で分割して先頭だけ残す → "api.novita.ai"
    #   3. 英数字以外をすべて "_" に置換し、大文字化 → "API_NOVITA_AI"
    #   4. 末尾に "_API_KEY" を付ける → "API_NOVITA_AI_API_KEY"
    # という手順で、未登録のプロバイダでも「それらしい」環境変数名を
    # 自動生成する。
    # ---------------------------------------------------------------
    host = re.sub(r"^https?://", "", base_url).split("/")[0]
    host = re.sub(r"[^a-zA-Z0-9]+", "_", host).strip("_").upper()
    return f"{host}_API_KEY"


def resolve_provider(base_url: str) -> ProviderSpec:
    """Match a --provider-url against the known registry, or synthesize a generic one.

    Keeps the system usable with "other providers ... as long as your system
    complies with the project requirements" (Section 5.6): any OpenAI-compatible
    base URL works out of the box, as long as its key is exported under the
    conventional <HOST>_API_KEY environment variable.
    """
    # ---------------------------------------------------------------
    # 【日本語解説】未知のプロバイダも自動サポートする仕組み
    # ---------------------------------------------------------------
    # --provider-url が KNOWN_PROVIDERS のどれとも前方一致しなければ、
    # _env_var_from_url() でホスト名から機械的に環境変数名を組み立てて
    # 即席の ProviderSpec を作る。
    #
    # つまり「新しいプロバイダを追加するのにコード変更は一切不要、.env に
    # 対応する環境変数を1つ用意するだけでよい」という要件を、コードを
    # 1行も書かずに満たしている。
    #
    # ただし kind は常に "openai_compatible" に固定されるため、Geminiの
    # ような非互換API構造のプロバイダを未知のURLとして渡した場合は動かない。
    # これは仕様として妥当なトレードオフで、「本当に異なるワイヤ形式」は
    # KNOWN_PROVIDERS に明示的に登録するしかない設計になっている。
    # ---------------------------------------------------------------
    normalized = base_url.rstrip("/")
    for spec in KNOWN_PROVIDERS:
        spec_url = spec.base_url.rstrip("/")
        if normalized == spec_url or normalized.startswith(spec_url):
            # 完全一致だけでなく前方一致も許す（例: base_url にパスの続きが
            # 付いている場合でも既知プロバイダとして認識できるようにする）。
            return spec
    # どの既知プロバイダにも一致しなければ、URLから推測した環境変数名を持つ
    # 汎用（openai_compatible）の ProviderSpec をその場で合成して返す。
    return ProviderSpec(
        name=normalized,
        base_url=base_url,
        api_key_env_prefix=_env_var_from_url(base_url),
        kind="openai_compatible",
    )
