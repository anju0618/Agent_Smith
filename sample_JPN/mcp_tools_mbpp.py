"""MCP server exposing the MBPP tools (Section 4.3.2) over stdio or streamable HTTP.

    python mcp_tools_mbpp.py            # stdio transport (default)
    python mcp_tools_mbpp.py --http 8000  # streamable HTTP transport on port 8000

Kept at the repository root per Section 4.2's requirement for MCP tool files.
"""
# このファイルはエージェント本体(agent_mbpp.py)とは**別プロセス**として起動される
# MCPサーバー。役割はただ1つ、`run_tests(code, test_list)` というツールを1個だけ
# 公開すること。エージェント側のサンドボックス内からは、これがまるで普通の
# Python関数のように `run_tests(code=..., test_list=...)` と呼び出せる
# (sandbox/mcp_client.py の MCPToolProxy が橋渡しする)。
#
# 注意: @mcp.tool() が付いた関数のdocstringは、FastMCPによって自動的に
# MCPツールのスキーマ説明文として抽出され、それが sandbox/mcp_client.py の
# manual_text() 経由でシステムプロンプトに埋め込まれ、実際にLLMへ送信される。
# つまりこのdocstringは「ただのコード内コメント」ではなく「LLMへの説明書」
# そのものであり、意味を変えると挙動が変わってしまう。そのため元の英語の
# docstringは一切変更せず、日本語の解説はその外側(関数の前、あるいは
# docstringの外)にコメントとして追加している。
from __future__ import annotations

import argparse
import json
import os
import secrets
from typing import List

from mcp.server.fastmcp import FastMCP
from models import SandboxConfig
from sandbox.executor import DEFAULT_AUTHORIZED_IMPORTS, FinalAnswer, Sandbox

# FastMCPインスタンスを1つ作る。以降 @mcp.tool() でデコレートした関数が
# 自動的にこのサーバーの公開ツールとして登録されていく。
mcp = FastMCP("agent-smith-mbpp-tools")


def _test_imports() -> List[str]:
    """Imports the task's test_list needs but the candidate solution has no
    reason to include itself (e.g. `math` for `math.isclose(...)` assertions
    on a task whose own solution never touches `math`). agent_mbpp.py passes
    these through MBPPTaskInput.test_imports via this env var so run_tests()
    can guarantee they're present, rather than leaving it to chance whether
    the LLM's own code happens to need (and therefore import) the same
    module - which silently NameErrors on tasks where it doesn't."""
    # 環境変数 AGENT_SMITH_TEST_IMPORTS は agent_mbpp.py が
    # MCPToolProxy(..., env=tool_env) 経由でこのプロセス(子プロセス)にだけ
    # 渡した、JSON文字列化されたimport文のリスト。
    raw = os.environ.get("AGENT_SMITH_TEST_IMPORTS")
    if not raw:
        # 環境変数が無い/空文字列 = このタスクには追加importが不要。
        return []
    try:
        imports = json.loads(raw)
    except json.JSONDecodeError:
        # 万が一JSONとして壊れていても、ここで例外を投げてサーバー全体を
        # 落とすようなことはせず、単に「追加importなし」として静かに続行する。
        return []
    # 文字列以外の要素が紛れ込んでいた場合に備えたフィルタ(型の安全性確保)。
    return [line for line in imports if isinstance(line, str)]


@mcp.tool()
def run_tests(code: str, test_list: List[str]) -> str:
    """Run a candidate MBPP solution against the given test assertions.

    Args:
        code: The candidate Python solution (a full function definition).
        test_list: Assertion strings to execute against `code`.

    Returns:
        A JSON string {"success": bool, "output": str} - success is True only
        if every assertion passed. Candidate code runs in the same hardened,
        OS-isolated sandbox used by the agents, so it cannot access the MCP
        server's host filesystem or network.
    """
    # ここから下が実装本体。日本語での要点解説:
    #
    # このツールがやっていることは「LLMが書いた候補コードを、テストの
    # assert文と一緒に1本のPythonスクリプトに連結し、それを**もう一段
    # 別のサンドボックス**で実行して、全部のassertが通ったかどうかを
    # 判定する」こと。エージェント本体が使っているサンドボックスとは
    # 別の、使い捨てのSandboxインスタンスをここで新規に作る点に注意。
    imports_prefix = "\n".join(_test_imports())
    # secrets.token_hex(16) は暗号論的に安全な乱数から16バイト(32桁の16進数)
    # の文字列を生成する。この値を候補コードの実行のたびに新しく作り、
    # 実行スクリプトの一番最後に print(marker) として仕込む。これにより
    # 「候補コードが自力で正解を装って print("成功しました") のような
    # 固定文字列を出力してごまかす」ことが事実上不可能になる - マーカーは
    # 毎回変わり、かつ候補コード自身はその値を知りようがないため。
    marker = f"__AGENT_SMITH_MBPP_PASS_{secrets.token_hex(16)}__"
    full_code = (
        # (1) test_importsで指定された前置import(あれば)
        (imports_prefix + "\n" if imports_prefix else "")
        # (2) 候補コード本体(関数定義)
        + code
        + "\n"
        # (3) 公開テストのassert文を全部連結
        + "\n".join(test_list)
        # (4) 最後にマーカーをprint。ここまで例外なく実行が到達すれば
        #     「全assertが通過した」ことの証拠になる。
        + f"\nprint({marker!r})"
    )

    sandbox = None
    try:
        sandbox = Sandbox(
            SandboxConfig(
                authorized_imports=DEFAULT_AUTHORIZED_IMPORTS,
                # allowed_directories=[] - ファイルアクセスを一切許可しない。
                # 候補コードはただの計算をするだけで、ファイルを読み書きする
                # 正当な理由が無いため、最も厳しい設定にしている。
                allowed_directories=[],
                # このMCPサーバー内部でのタイムアウトは10秒。
                # agent_mbpp.py側のSandboxConfig(外側)のタイムアウトは
                # 20秒に設定されており、常にこちらより長い - 内側が先に
                # 確実にタイムアウトするようにするための意図的な余裕。
                max_execution_time_seconds=10,
                max_memory_mb=256,
            )
        )
        try:
            output = sandbox.run(full_code)
        except (FinalAnswer, KeyboardInterrupt, SystemExit) as exc:
            # 万が一候補コードの中に final_answer(...) が紛れ込んでいたり
            # (通常起きないはずだが、LLMの出力は信頼できない前提)、
            # KeyboardInterrupt/SystemExitが飛んできても、ここで捕まえて
            # ただのエラーメッセージ文字列に変換する。run_tests()は
            # MCPツールとして常にJSON文字列を返す契約なので、例外を
            # そのまま外へ伝播させるわけにはいかない。
            output = f"[Error] {type(exc).__name__}: {exc}"
    except Exception as exc:  # noqa: BLE001 - MCP tool must return JSON on setup errors
        # Sandbox自体の生成に失敗した場合(通常はまず起きないが)も同様に
        # 文字列化してJSON応答の一部にする。
        output = f"[Error] {type(exc).__name__}: {exc}"
    finally:
        # 使い捨てサンドボックスなので、成功・失敗にかかわらず必ず閉じる。
        if sandbox is not None:
            sandbox.close()

    if output.startswith("[Timeout]") and "timed out" not in output:
        # Sandbox.run()が返すタイムアウトメッセージの文言を、LLMにとって
        # より分かりやすい表現に軽く言い換えている(表示上の調整であり、
        # ロジックには影響しない)。
        output = output.replace("Execution exceeded 10s", "Execution timed out after 10s", 1)
    # マーカー文字列が出力に含まれているかどうかだけで成否を判定する -
    # これが「全assertを最後まで通過できたか」の唯一かつ確実な判定方法。
    success = marker in output
    if success:
        # 成功時は出力からマーカー自体を取り除いてから返す。LLMに
        # 「このランダムな文字列は何だろう」と余計な混乱を与えないため。
        output = output.replace(marker, "").rstrip()
    return json.dumps({"success": success, "output": output})


def main() -> None:
    parser = argparse.ArgumentParser(description="MBPP MCP tool server")
    parser.add_argument(
        "--http", type=int, default=None, help="Serve over streamable HTTP on this port instead of stdio"
    )
    args = parser.parse_args()

    if args.http:
        # --http 8000 のように指定されればHTTPサーバーとして待ち受ける
        # (このプロジェクトの主経路では使われないが、未知のMCPクライアントが
        # HTTP経由で接続してくるケースへの対応)。
        mcp.settings.port = args.http
        mcp.run(transport="streamable-http")
    else:
        # デフォルトはstdio - 親プロセス(agent_mbpp.py)から
        # サブプロセスとして起動され、標準入出力パイプでMCPプロトコルの
        # やり取りをする、このプロジェクトでの主要な起動形態。
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
