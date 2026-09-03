"""サンドボックスCLI(仕様書 4.2節): 対話式REPLで、任意でMCPサーバーと接続できる。

    uv run sandbox                                          # 対話モード、デフォルト設定
    uv run sandbox sandbox_template.json                    # 独自の設定ファイルを指定
    uv run sandbox --mcp-stdio "python mcp_tools_mbpp.py" sandbox_template.json
    uv run sandbox --mcp-server http://localhost:8000/mcp
"""
from __future__ import annotations

import argparse  # コマンドライン引数のパース用
import json  # 設定ファイル(JSON)の読み込み用
import sys  # 標準エラー出力・終了コード制御用
from pathlib import Path  # 設定ファイルパスの操作用
from typing import Optional  # 省略可能な型ヒント用

from models import SandboxConfig  # サンドボックス設定を表すPydanticモデル
# デフォルトの許可import一覧・許可ディレクトリ一覧、FinalAnswer例外、Sandbox本体をexecutorから取得
from sandbox.executor import DEFAULT_ALLOWED_DIRECTORIES, DEFAULT_AUTHORIZED_IMPORTS, FinalAnswer, Sandbox
from sandbox.mcp_client import MCPToolProxy  # MCPサーバーへの同期的クライアントラッパー


def load_config(path: Optional[str]) -> SandboxConfig:
    """JSONファイルからSandboxConfigを読み込む。pathがNoneならプロジェクトのデフォルト設定を使う。"""
    if path is None:
        # 設定ファイル未指定時は、デフォルトの許可import・許可ディレクトリでSandboxConfigを構築
        return SandboxConfig(
            authorized_imports=DEFAULT_AUTHORIZED_IMPORTS,
            allowed_directories=DEFAULT_ALLOWED_DIRECTORIES,
        )
    config_path = Path(path)  # 文字列パスをPathオブジェクトに変換
    with config_path.open("r", encoding="utf-8") as file:  # 設定ファイルをUTF-8で開く
        data = json.load(file)  # JSONをPythonの辞書として読み込む
    return SandboxConfig.model_validate(data)  # 辞書をバリデーションしてモデル化


def _connect_mcp(mcp_stdio: Optional[str], mcp_server: Optional[str]) -> Optional[MCPToolProxy]:
    # stdio経由のMCPサーバー起動コマンドが指定されていれば、そちらを優先して接続
    if mcp_stdio:
        print(f"Connecting to MCP server over stdio: {mcp_stdio}")
        return MCPToolProxy(stdio_command=mcp_stdio)
    # HTTP経由のMCPサーバーURLが指定されていればそちらに接続
    if mcp_server:
        print(f"Connecting to MCP server over streamable HTTP: {mcp_server}")
        return MCPToolProxy(http_url=mcp_server)
    return None  # どちらも指定がなければMCP接続なし


def repl(sandbox: Sandbox) -> None:
    """REPL形式のCLIモード(仕様書 4.2節): コードを読み取り、空行で溜まったブロックを実行する。
    'exit'入力またはEOF(Ctrl+D)できれいに終了する。"""
    print("Agent Smith interactive sandbox. Blank line runs the block, 'exit' or Ctrl+D quits.")
    while True:
        lines: list = []  # 入力された行を溜めるバッファ
        try:
            first_line = input(">>> ")  # 最初の行を読み取る(プロンプトは">>> "を模倣)
        except EOFError:
            print()  # Ctrl+DでEOFになったら改行してから終了
            return
        if first_line.strip() == "exit":  # 'exit'と入力されたらREPLを終了
            return
        if first_line != "":
            lines.append(first_line)  # 空行でなければバッファに追加
            while True:
                try:
                    line = input("... ")  # 継続行を読み取る(継続プロンプト"... ")
                except EOFError:
                    print()  # 継続入力中にEOFなら改行して終了
                    return
                if line == "":
                    break  # 空行が来たらブロックの入力終了とみなす
                lines.append(line)  # 継続行をバッファに追加

        code = "\n".join(lines)  # バッファ内の行を改行で連結して1つのコード文字列にする
        if not code.strip():
            continue  # 空コードなら何もせず次のループへ
        try:
            result = sandbox.run(code)  # サンドボックス内でコードを実行
        except FinalAnswer as fa:
            # final_answer()が呼ばれたら、その回答を表示して次の入力へ(REPLは終了しない)
            print(f"[final_answer submitted] {fa.answer!r}")
            continue
        print(result)  # 実行結果(標準出力またはエラーメッセージ)を表示


def main() -> None:
    parser = argparse.ArgumentParser(description="Agent Smith sandbox")  # CLI引数パーサーを作成
    # 位置引数: サンドボックス設定JSONファイルへのパス(省略可、省略時はNone)
    parser.add_argument("config", nargs="?", default=None, help="Path to sandbox configuration JSON file")
    # オプション引数: stdio経由でMCPサーバーを起動するコマンド
    parser.add_argument("--mcp-stdio", default=None, help="Command used to launch an MCP server over stdio")
    # オプション引数: streamable HTTPのMCPサーバーのURL
    parser.add_argument("--mcp-server", default=None, help="URL of a streamable HTTP MCP server")
    args = parser.parse_args()  # コマンドライン引数を実際にパース

    try:
        config = load_config(args.config)  # 設定ファイルを読み込む(失敗時は例外)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        # ファイルI/Oエラー・JSON構文エラー・バリデーションエラーをまとめて捕捉
        print(f"Failed to load sandbox config: {exc}", file=sys.stderr)
        sys.exit(1)  # 設定読み込み失敗時は終了コード1で異常終了

    mcp_proxy = None  # MCPプロキシ(未接続なら None のまま)
    sandbox = None  # サンドボックスインスタンス(生成前は None)
    try:
        mcp_proxy = _connect_mcp(args.mcp_stdio, args.mcp_server)  # 指定があればMCPサーバーに接続
        # MCPツールをサンドボックスの名前空間に追加するための辞書を構築(未接続なら空辞書)
        extra_namespace = mcp_proxy.build_namespace() if mcp_proxy else {}
        if mcp_proxy:
            # 接続成功時は利用可能なツール数と説明文(マニュアル)を表示
            print(f"Connected. {len(mcp_proxy.tools)} tool(s) available:\n{mcp_proxy.manual_text()}\n")

        sandbox = Sandbox(config, extra_namespace=extra_namespace)  # サンドボックス本体を生成
        repl(sandbox)  # 対話ループを開始
    finally:
        # 正常終了・例外発生いずれの場合も、後始末としてサンドボックスとMCP接続を必ず閉じる
        if sandbox:
            sandbox.close()
        if mcp_proxy:
            mcp_proxy.close()


if __name__ == "__main__":
    main()  # スクリプトとして直接実行された場合のエントリーポイント
