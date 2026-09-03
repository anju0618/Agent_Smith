"""Agent Smithのための環境変数・プロバイダ設定読み込みモジュール。

.envの読み込みと既知のLLMプロバイダのレジストリをここに集約することで、
コードベースの他の部分がos.environに直接触れないようにする。これは
「General Rules」の要件("APIキーのハードコード禁止、全て環境変数/.envファイルから")を
アーキテクチャ的に実際に強制している箇所である。
"""
from __future__ import annotations  # 型注釈の評価を遅延させる(前方参照を許可する)ためのfuture import

import os  # 環境変数を読むためのosモジュール
import re  # 正規表現を使うためのreモジュール
from dataclasses import dataclass  # データクラスを定義するためのdataclassデコレータ
from pathlib import Path  # ファイルパスをオブジェクトとして扱うためのPath
from typing import List  # 型ヒント用のList

from dotenv import load_dotenv  # .envファイルを読み込むためのdotenvライブラリ

_ENV_LOADED = False  # .envの読み込みが完了したかどうかを示すモジュールレベルのフラグ(多重読み込み防止用)


def load_env() -> None:
    """.envを一度だけ読み込む(冪等)。シェルで既に設定されている変数は決して上書きしない。"""
    global _ENV_LOADED  # モジュールレベルのフラグを更新するためglobal宣言
    if _ENV_LOADED:  # 既に読み込み済みなら
        return  # 何もせず即座に戻る(冪等性の確保)
    env_path = Path(__file__).resolve().parent / ".env"  # このファイルと同じディレクトリにある.envファイルのパスを組み立てる
    load_dotenv(dotenv_path=env_path, override=False)  # .envを読み込む(既存の環境変数は上書きしない)
    _ENV_LOADED = True  # 読み込み完了フラグを立てる


load_env()  # モジュールインポート時に一度.envの読み込みを実行しておく


@dataclass(frozen=True)
class ProviderSpec:
    """1つのLLMプロバイダに関する静的な記述(Section 5.6 - マルチプロバイダ対応)。"""

    name: str  # プロバイダ名(例: "openrouter")
    base_url: str  # プロバイダのAPIベースURL
    api_key_env_prefix: str  # APIキーを保持する環境変数名のプレフィックス
    kind: str = "openai_compatible"  # プロバイダの種類。"openai_compatible" または "gemini"

    def collect_api_keys(self) -> List[str]:
        """このプロバイダに設定されている全てのAPIキーを収集する。

        複数トークン管理(Section 5.6.1)に対応: OPENROUTER_API_KEY、
        OPENROUTER_API_KEY_2、OPENROUTER_API_KEY_3、...が全て拾われるので、
        LLMクライアントは1つのキーがレート制限に達した際に他のキーへ
        ローテーションできる。
        """
        keys = []  # 収集したAPIキーを格納するリスト
        primary = os.environ.get(self.api_key_env_prefix)  # まず接尾辞なしの主キーを取得
        if primary:  # 主キーが存在すれば
            keys.append(primary)  # リストに追加
        index = 2  # 2番目以降の連番キーを探すためのインデックス
        while True:  # 連番のキーが見つからなくなるまでループ
            value = os.environ.get(f"{self.api_key_env_prefix}_{index}")  # 例: OPENROUTER_API_KEY_2 を取得
            if not value:  # 値が見つからなければ
                break  # ループを終了(これ以上の連番キーはないとみなす)
            keys.append(value)  # 見つかったキーをリストに追加
            index += 1  # 次の連番へ進む
        return keys  # 収集した全キーのリストを返す


# 既知の無料枠プロバイダ一覧(Section 5.6.1 - あくまで例示であり網羅的ではない)。
# 追加のプロバイダをサポートするには、ここにProviderSpecのエントリを追加する。
KNOWN_PROVIDERS: List[ProviderSpec] = [
    ProviderSpec("openrouter", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY", "openai_compatible"),  # OpenRouter
    ProviderSpec("groq", "https://api.groq.com/openai/v1", "GROQ_API_KEY", "openai_compatible"),  # Groq
    ProviderSpec("together", "https://api.together.xyz/v1", "TOGETHER_API_KEY", "openai_compatible"),  # Together AI
    ProviderSpec(
        "fireworks", "https://api.fireworks.ai/inference/v1", "FIREWORKS_API_KEY", "openai_compatible"
    ),  # Fireworks AI
    ProviderSpec(
        "google_ai_studio",
        "https://generativelanguage.googleapis.com/v1beta",
        "GOOGLE_AI_STUDIO_API_KEY",
        "gemini",
    ),  # Google AI Studio(Geminiは他と形式が異なるためkind="gemini")
]


def _env_var_from_url(base_url: str) -> str:
    """一覧にないプロバイダに対して、<HOST>_API_KEY形式の環境変数名をベストエフォートで推測する。"""
    host = re.sub(r"^https?://", "", base_url).split("/")[0]  # スキーム(http://など)を取り除き、パス部分より前のホスト名だけを取り出す
    host = re.sub(r"[^a-zA-Z0-9]+", "_", host).strip("_").upper()  # 英数字以外をアンダースコアに置換し、前後の_を除去して大文字化
    return f"{host}_API_KEY"  # "<HOST>_API_KEY"という形式の環境変数名を返す


def resolve_provider(base_url: str) -> ProviderSpec:
    """--provider-urlを既知のレジストリと照合し、なければ汎用のProviderSpecを合成する。

    "他のプロバイダも...プロジェクト要件を満たす限り使用可能"(Section 5.6)という
    要求を満たすため、OpenAI互換のベースURLであれば、慣例に従った<HOST>_API_KEY
    環境変数さえ設定されていれば、そのまま動作するようにする。
    """
    normalized = base_url.rstrip("/")  # 末尾のスラッシュを除去して比較しやすくする
    for spec in KNOWN_PROVIDERS:  # 既知のプロバイダを順番にチェック
        spec_url = spec.base_url.rstrip("/")  # 比較対象側も末尾スラッシュを除去
        if normalized == spec_url or normalized.startswith(spec_url):  # 完全一致、またはプレフィックス一致なら
            return spec  # 一致した既知のProviderSpecを返す
    return ProviderSpec(  # 既知のプロバイダに一致しなければ、汎用のProviderSpecをその場で作成する
        name=normalized,  # 名前は正規化したURLをそのまま使う
        base_url=base_url,  # ベースURLは元の値をそのまま使う
        api_key_env_prefix=_env_var_from_url(base_url),  # APIキー環境変数名はURLから推測する
        kind="openai_compatible",  # 未知のプロバイダはOpenAI互換とみなす
    )
