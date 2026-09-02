"""Sandbox CLI (Section 4.2): interactive REPL, optionally wired to an MCP server.

    uv run sandbox                                          # interactive, defaults
    uv run sandbox sandbox_template.json                    # custom config
    uv run sandbox --mcp-stdio "python mcp_tools_mbpp.py" sandbox_template.json
    uv run sandbox --mcp-server http://localhost:8000/mcp

# ============================================================================
# 【日本語解説】このファイルの立ち位置
# ============================================================================
# `uv run sandbox` というコマンドの実体がこのファイルの main() です
# （pyproject.tomlの[project.scripts]でsandbox.cli:mainとして登録されて
# いる）。役割は2つ:
#   1. Sandbox と MCPToolProxy を実際に動かして手元で試すための、
#      人間向けの対話的REPL（Read-Eval-Print Loop）を提供する。
#   2. このプロジェクトの中で「SandboxとMCPToolProxyをどう組み合わせて
#      使うか」の**最小のリファレンス実装**として機能する ──
#      agent_mbpp.py/agent_swebench.pyを読む前に、まずこの小さな
#      main()を読むと全体の使い方がイメージしやすい。
# ============================================================================
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from models import SandboxConfig
from sandbox.executor import DEFAULT_ALLOWED_DIRECTORIES, DEFAULT_AUTHORIZED_IMPORTS, FinalAnswer, Sandbox
from sandbox.mcp_client import MCPToolProxy


def load_config(path: Optional[str]) -> SandboxConfig:
    """Load a SandboxConfig from a JSON file, or fall back to the project defaults."""
    # 【日本語解説】
    # パス未指定ならプロジェクトのデフォルト設定
    # （DEFAULT_AUTHORIZED_IMPORTS / DEFAULT_ALLOWED_DIRECTORIES、
    # executor.py参照）を使う。パスが指定されていれば、そのJSONファイル
    # （例: sandbox_template.json、Section 14参照）を読み込み、
    # SandboxConfig.model_validate()でPydanticのバリデーションを
    # 通してから使う。
    if path is None:
        return SandboxConfig(
            authorized_imports=DEFAULT_AUTHORIZED_IMPORTS,
            allowed_directories=DEFAULT_ALLOWED_DIRECTORIES,
        )
    config_path = Path(path)
    with config_path.open("r", encoding="utf-8") as file:
        data = json.load(file)
    return SandboxConfig.model_validate(data)


def _connect_mcp(mcp_stdio: Optional[str], mcp_server: Optional[str]) -> Optional[MCPToolProxy]:
    # 【日本語解説】
    # --mcp-stdio と --mcp-server はどちらも省略可能（両方省略すれば
    # ツール無しの素のサンドボックスとして動く）。指定があれば
    # MCPToolProxy（sandbox/mcp_client.py、Section 8.3）をその
    # トランスポート方式で接続する。
    if mcp_stdio:
        print(f"Connecting to MCP server over stdio: {mcp_stdio}")
        return MCPToolProxy(stdio_command=mcp_stdio)
    if mcp_server:
        print(f"Connecting to MCP server over streamable HTTP: {mcp_server}")
        return MCPToolProxy(http_url=mcp_server)
    return None


def repl(sandbox: Sandbox) -> None:
    """REPL-style CLI mode (Section 4.2): reads code, a blank line runs the
    accumulated block, 'exit' or EOF (Ctrl+D) quits cleanly."""
    # ------------------------------------------------------------------
    # 【日本語解説】対話ループの中身
    # ------------------------------------------------------------------
    # 1行ずつinput()で読み込み、空行が来るまで1つのコードブロックとして
    # 蓄積する。空行が来たらそのブロックをsandbox.run()に渡して実行し、
    # 結果を表示してまた最初から繰り返す。'exit'とCtrl+D(EOFError)の
    # どちらでも正常に抜けられるようにしている。
    print("Agent Smith interactive sandbox. Blank line runs the block, 'exit' or Ctrl+D quits.")
    while True:
        lines: list = []
        try:
            first_line = input(">>> ")
        except EOFError:
            print()
            return
        if first_line.strip() == "exit":
            return
        if first_line != "":
            lines.append(first_line)
            while True:
                try:
                    line = input("... ")
                except EOFError:
                    print()
                    return
                if line == "":
                    break
                lines.append(line)

        code = "\n".join(lines)
        if not code.strip():
            continue
        try:
            result = sandbox.run(code)
        except FinalAnswer as fa:
            # 【日本語解説】
            # REPLでも final_answer(...) を呼べば、通常のエージェント
            # ループと同じようにFinalAnswer例外として飛んでくる。
            # ここではエージェントを終了させるのではなく、申告内容を
            # 表示してREPLを続けるだけの、動作確認用の扱いにしている。
            print(f"[final_answer submitted] {fa.answer!r}")
            continue
        print(result)


def main() -> None:
    parser = argparse.ArgumentParser(description="Agent Smith sandbox")
    parser.add_argument("config", nargs="?", default=None, help="Path to sandbox configuration JSON file")
    parser.add_argument("--mcp-stdio", default=None, help="Command used to launch an MCP server over stdio")
    parser.add_argument("--mcp-server", default=None, help="URL of a streamable HTTP MCP server")
    args = parser.parse_args()

    try:
        config = load_config(args.config)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"Failed to load sandbox config: {exc}", file=sys.stderr)
        sys.exit(1)

    mcp_proxy = None
    sandbox = None
    try:
        # 【日本語解説】
        # ここが「Sandbox + MCPToolProxy」の最小の組み合わせ方の見本。
        # 1. MCPサーバーに接続する（指定されていれば）
        # 2. mcp_proxy.build_namespace() でツールをPython関数の辞書に変換
        # 3. その辞書を Sandbox の extra_namespace としてそのまま渡す
        # これと全く同じパターンが agent_mbpp.py / agent_swebench.py でも
        # 使われている（Section 11参照）。
        mcp_proxy = _connect_mcp(args.mcp_stdio, args.mcp_server)
        extra_namespace = mcp_proxy.build_namespace() if mcp_proxy else {}
        if mcp_proxy:
            print(f"Connected. {len(mcp_proxy.tools)} tool(s) available:\n{mcp_proxy.manual_text()}\n")

        sandbox = Sandbox(config, extra_namespace=extra_namespace)
        repl(sandbox)
    finally:
        # 【日本語解説】
        # どんな終わり方をしても（正常終了、例外、Ctrl+C）、
        # Sandboxの隔離ワーカープロセスとMCP接続を必ず後片付けする。
        # agent_mbpp.py/agent_swebench.pyのfinallyブロックと同じ
        # パターン。
        if sandbox:
            sandbox.close()
        if mcp_proxy:
            mcp_proxy.close()


if __name__ == "__main__":
    main()
