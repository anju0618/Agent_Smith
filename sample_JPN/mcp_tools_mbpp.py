"""MBPP用ツール(セクション4.3.2)をstdioまたはstreamable HTTP経由で公開するMCPサーバ。

    python mcp_tools_mbpp.py            # stdioトランスポート(デフォルト)
    python mcp_tools_mbpp.py --http 8000  # ポート8000でstreamable HTTPトランスポート

セクション4.2の「MCPツールファイルはリポジトリのルートに置くこと」という要件に従い
ルート直下に配置している。
"""
from __future__ import annotations  # 型注釈を文字列として遅延評価する(将来のアノテーション構文をサポート)

import argparse  # コマンドライン引数のパース用
import json  # テスト結果や環境変数のJSONエンコード/デコードに使用
import os  # 環境変数の読み取りに使用
import secrets  # 実行成功を検出するためのランダムなマーカー文字列生成に使用
from typing import List  # 型注釈のため

from mcp.server.fastmcp import FastMCP  # MCPサーバを構築するためのフレームワーク
from models import SandboxConfig  # サンドボックスの設定を表すデータモデル
from sandbox.executor import DEFAULT_AUTHORIZED_IMPORTS, FinalAnswer, Sandbox  # サンドボックス実行環境・許可インポート一覧・final_answer例外

mcp = FastMCP("agent-smith-mbpp-tools")  # MBPP用MCPサーバのインスタンスを作成


def _test_imports() -> List[str]:
    """タスクのtest_listが必要とするが、候補解答自体には含める理由がないimportの一覧を返す
    (例: 候補解答が`math`を一切使わないタスクで、`math.isclose(...)`というassertionが
    必要とする`math`)。agent_mbpp.pyがMBPPTaskInput.test_importsをこの環境変数経由で
    渡すことで、LLM自身のコードがたまたま同じモジュールを必要としてimportしているかどうかに
    運任せにするのではなく、run_tests()側でこれらのimportが確実に存在するようにできる
    - importに任せると、必要としないタスクでは静かにNameErrorになってしまう。"""
    raw = os.environ.get("AGENT_SMITH_TEST_IMPORTS")  # 環境変数からJSON文字列を取得(未設定ならNone)
    if not raw:
        return []  # 環境変数が未設定または空文字列なら空リストを返す
    try:
        imports = json.loads(raw)  # JSON文字列をパースしてPythonのリストに変換
    except json.JSONDecodeError:
        return []  # パースに失敗した場合は安全側に倒して空リストを返す
    return [line for line in imports if isinstance(line, str)]  # 文字列型の要素のみを残してフィルタする


@mcp.tool()
def run_tests(code: str, test_list: List[str]) -> str:
    """MBPPの候補解答を、与えられたテストのassertionに対して実行する。

    引数:
        code: 候補となるPythonの解答(完全な関数定義)。
        test_list: `code`に対して実行するassertion文字列のリスト。

    戻り値:
        JSON文字列 {"success": bool, "output": str} - successは全てのassertionが
        通った場合のみTrueになる。候補コードはエージェント自身が使うものと同じ、
        OSレベルで隔離されたハード化済みサンドボックス内で実行されるため、
        MCPサーバが動くホストのファイルシステムやネットワークにはアクセスできない。
    """
    imports_prefix = "\n".join(_test_imports())  # テストに必要な追加importを改行区切りの文字列にまとめる
    marker = f"__AGENT_SMITH_MBPP_PASS_{secrets.token_hex(16)}__"  # 全assertionが通過したことを検出するためのランダムなマーカー文字列を生成
    full_code = (
        (imports_prefix + "\n" if imports_prefix else "")  # 追加importがあれば先頭に付加
        + code  # 候補解答のコード本体
        + "\n"
        + "\n".join(test_list)  # 各テストassertionを改行区切りで追加
        + f"\nprint({marker!r})"  # 最後にマーカーを出力する行を追加(ここまで到達すれば全assertionが例外を出さずに通った証拠)
    )  # 候補解答+追加import+テスト+マーカー出力を1つのスクリプトに連結する

    sandbox = None  # サンドボックスの参照(finally節でクローズするため先に宣言)
    try:
        sandbox = Sandbox(
            SandboxConfig(
                authorized_imports=DEFAULT_AUTHORIZED_IMPORTS,  # サンドボックス内で許可するimportの一覧
                allowed_directories=[],  # ファイルシステムへのアクセスは許可しない
                max_execution_time_seconds=10,  # 実行時間の上限(秒)
                max_memory_mb=256,  # メモリ使用量の上限(MB)
            )
        )  # テスト実行専用の使い捨てサンドボックスを生成
        try:
            output = sandbox.run(full_code)  # 連結したスクリプトをサンドボックス内で実行
        except (FinalAnswer, KeyboardInterrupt, SystemExit) as exc:
            output = f"[Error] {type(exc).__name__}: {exc}"  # 候補コードが誤ってfinal_answer()等を呼んだ場合はエラーとして記録
    except Exception as exc:  # noqa: BLE001 - MCPツールはセットアップ時のエラーでもJSONを返す必要がある
        output = f"[Error] {type(exc).__name__}: {exc}"  # サンドボックス生成自体に失敗した場合もエラーメッセージとして記録
    finally:
        if sandbox is not None:
            sandbox.close()  # サンドボックスのリソースを確実に解放する

    if output.startswith("[Timeout]") and "timed out" not in output:
        output = output.replace("Execution exceeded 10s", "Execution timed out after 10s", 1)  # タイムアウトメッセージの文言をより分かりやすい表現に置き換える
    success = marker in output  # 出力にマーカーが含まれていれば全assertionが通過したとみなす
    if success:
        output = output.replace(marker, "").rstrip()  # 成功時は出力からマーカー文字列を除去し、末尾の空白を整える
    return json.dumps({"success": success, "output": output})  # 成否と出力内容をJSON文字列として返す


def main() -> None:
    # エントリーポイント: コマンドライン引数を解析し、stdioまたはHTTPでMCPサーバを起動する
    parser = argparse.ArgumentParser(description="MBPP MCP tool server")  # 引数パーサを作成
    parser.add_argument(
        "--http", type=int, default=None, help="Serve over streamable HTTP on this port instead of stdio"
    )  # HTTPで待ち受けるポート番号(指定しなければstdioモード)
    args = parser.parse_args()  # 実際にコマンドライン引数を解析

    if args.http:
        mcp.settings.port = args.http  # 指定されたポート番号をサーバ設定に反映
        mcp.run(transport="streamable-http")  # streamable HTTPトランスポートでサーバを起動
    else:
        mcp.run(transport="stdio")  # デフォルトのstdioトランスポートでサーバを起動


if __name__ == "__main__":
    main()  # スクリプトとして直接実行された場合にmain()を呼び出す
