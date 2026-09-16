"""サンドボックスCLI(仕様書 4.2節): 対話式REPLで、任意でMCPサーバーと接続できる。

    uv run sandbox                                          # 対話モード、デフォルト設定
    uv run sandbox sandbox_template.json                    # 独自の設定ファイルを指定
    uv run sandbox --mcp-stdio "python mcp_tools_mbpp.py" sandbox_template.json
    uv run sandbox --mcp-server http://localhost:8000/mcp
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Optional

from models import SandboxConfig

from sandbox.executor import DEFAULT_ALLOWED_DIRECTORIES, DEFAULT_AUTHORIZED_IMPORTS, FinalAnswer, Sandbox
from sandbox.mcp_client import MCPToolProxy


def load_config(path: Optional[str]) -> SandboxConfig:
    """JSONファイルからSandboxConfigを読み込む。pathがNoneならプロジェクトのデフォルト設定を使う。"""
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

    if mcp_stdio:
        print(f"Connecting to MCP server over stdio: {mcp_stdio}")

        return MCPToolProxy(stdio_command=mcp_stdio, env=dict(os.environ))

    if mcp_server:
        print(f"Connecting to MCP server over streamable HTTP: {mcp_server}")
        return MCPToolProxy(http_url=mcp_server)
    return None


_CONTINUATION_RE = re.compile(r"^(else|elif|except|finally)\b")


def _leading_whitespace(line: str) -> int:

    return len(line) - len(line.lstrip(" \t"))


def _run_block(sandbox: Sandbox, code: str) -> None:

    if not code.strip():
        return
    try:
        result = sandbox.run(code)
    except FinalAnswer as fa:

        print(f"[final_answer submitted] {fa.answer!r}")
    else:
        print(result)


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
    pending_line: Optional[str] = None
    while True:
        lines: list = []
        block_indent = 0
        while True:
            if pending_line is not None:
                line, pending_line = pending_line, None
            else:
                try:
                    line = input("... " if lines else ">>> ")
                except EOFError:
                    print()

                    _run_block(sandbox, "\n".join(lines))
                    return
            if not lines and line.strip() == "exit":
                return
            if line.strip() == "":
                if not lines:
                    continue

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
                    lines.extend(blank_run)
                    lines.append(peeked)
                    continue
                pending_line = peeked
                break
            if not lines:
                block_indent = _leading_whitespace(line)
            lines.append(line)
        _run_block(sandbox, "\n".join(lines))


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
        mcp_proxy = _connect_mcp(args.mcp_stdio, args.mcp_server)

        extra_namespace = mcp_proxy.build_namespace() if mcp_proxy else {}
        if mcp_proxy:

            print(f"Connected. {len(mcp_proxy.tools)} tool(s) available:\n{mcp_proxy.manual_text()}\n")

        sandbox = Sandbox(config, extra_namespace=extra_namespace)
        repl(sandbox)
    finally:

        if sandbox:
            sandbox.close()
        if mcp_proxy:
            mcp_proxy.close()


if __name__ == "__main__":
    main()
