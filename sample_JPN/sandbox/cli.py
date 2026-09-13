"""サンドボックスCLI(仕様書 4.2節): 対話式REPLで、任意でMCPサーバーと接続できる。

    uv run sandbox                                          # 対話モード、デフォルト設定
    uv run sandbox sandbox_template.json                    # 独自の設定ファイルを指定
    uv run sandbox --mcp-stdio "python mcp_tools_mbpp.py" sandbox_template.json
    uv run sandbox --mcp-server http://localhost:8000/mcp
"""
from __future__ import annotations

import argparse  # コマンドライン引数のパース用
import json  # 設定ファイル(JSON)の読み込み用
import os  # 起動時の環境変数をMCPサブプロセスへ引き継ぐため
import re  # 空行の後に続く行がelse/elif/except/finallyかどうかの判定用
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
        # mcp SDKはenv=Noneだと安全な最小限のデフォルト環境変数しか子プロセスに渡さない
        # (TESTBED_PATH等のカスタム変数は落ちる)。このCLIは任意のMCPサーバーに接続する
        # 汎用エントリポイント(仕様書4.2節)なので、起動元プロセスの環境変数をそのまま
        # 引き継ぐのが期待される動作(exam_sandbox.shのswebench_toolsテストが要求する挙動)。
        return MCPToolProxy(stdio_command=mcp_stdio, env=dict(os.environ))
    # HTTP経由のMCPサーバーURLが指定されていればそちらに接続
    if mcp_server:
        print(f"Connecting to MCP server over streamable HTTP: {mcp_server}")
        return MCPToolProxy(http_url=mcp_server)
    return None  # どちらも指定がなければMCP接続なし


_CONTINUATION_RE = re.compile(r"^(else|elif|except|finally)\b")  # else/elif/except/finallyで始まる行の検出用


def _leading_whitespace(line: str) -> int:
    # 行の先頭にある空白文字(スペース・タブ)の個数を返す(インデント幅の比較に使う)
    return len(line) - len(line.lstrip(" \t"))


def _run_block(sandbox: Sandbox, code: str) -> None:
    # 溜まったコードブロックをサンドボックスで実行し、結果(またはfinal_answer)を表示する共通処理
    if not code.strip():
        return  # 空・空白のみのコードは何もしない
    try:
        result = sandbox.run(code)  # サンドボックス内でコードを実行
    except FinalAnswer as fa:
        # final_answer()が呼ばれたら、その回答を表示して次の入力へ(REPLは終了しない)
        print(f"[final_answer submitted] {fa.answer!r}")
    else:
        print(result)  # 実行結果(標準出力またはエラーメッセージ)を表示


def repl(sandbox: Sandbox) -> None:
    """REPL形式のCLIモード(仕様書 4.2節): コードを1行ずつ読み取り、空行に達したら、
    その直後(空行が連続していれば、その先)の非空行を1行だけ先読みしてブロックの
    区切りを判定する。先読みした行が、いま溜めているブロックの先頭行より深く
    インデントされているか、同じ深さのelse/elif/except/finallyであれば、まだ
    同じブロックの続きとみなして取り込みを続ける。それ以外であれば、そこでブロックを
    確定させて実行し、先読みした行は次のブロックの先頭として持ち越す。
    こうすることで、if/try本体の途中に空行を挟んだインデント済み複数行ブロックも、
    独立した複数のtry/except等が空行で区切られている場合も、どちらも正しく扱える。
    'exit'入力またはEOF(Ctrl+D)できれいに終了する(その時点で溜まっている
    ブロックがあれば、破棄せず実行してから終了する)。"""
    print(
        "Agent Smith interactive sandbox. Blank line runs the block (unless it's still open), "
        "'exit' or Ctrl+D quits."
    )
    pending_line: Optional[str] = None  # 先読みの結果、次のブロックの先頭行として持ち越した行
    while True:
        lines: list = []  # 入力された行を溜めるバッファ
        block_indent = 0  # このブロックの先頭行のインデント幅
        while True:
            if pending_line is not None:
                line, pending_line = pending_line, None  # 持ち越された行があればそれを使う
            else:
                try:
                    line = input("... " if lines else ">>> ")
                except EOFError:
                    print()  # Ctrl+DでEOFになったら改行する
                    # それまでに溜まった(不完全かもしれない)ブロックを、破棄せず実行してから終了する
                    _run_block(sandbox, "\n".join(lines))
                    return
            if not lines and line.strip() == "exit":  # ブロックの先頭で'exit'と入力されたらREPLを終了
                return
            if line.strip() == "":
                if not lines:
                    continue  # ブロック開始前の空行は無視して次の入力を待つ
                # 空行(の連続)の先にある最初の非空行を先読みして、ブロックが続くか判定する
                blank_run = [line]
                while True:
                    try:
                        peeked = input("... ")
                    except EOFError:
                        print()
                        _run_block(sandbox, "\n".join(lines))
                        return
                    if peeked.strip() == "":
                        blank_run.append(peeked)
                        continue
                    break
                peeked_indent = _leading_whitespace(peeked)
                if peeked_indent > block_indent or (
                    peeked_indent == block_indent and _CONTINUATION_RE.match(peeked.strip())
                ):
                    lines.extend(blank_run)  # まだ同じブロックの続きなので空行ごと取り込む
                    lines.append(peeked)
                    continue
                pending_line = peeked  # 別ブロックの先頭行なので次のブロックへ持ち越す
                break  # 現在のブロックをここで確定させて実行へ
            if not lines:
                block_indent = _leading_whitespace(line)  # ブロック先頭行のインデント幅を記録
            lines.append(line)  # 読み取った行をバッファに追加
        _run_block(sandbox, "\n".join(lines))


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
