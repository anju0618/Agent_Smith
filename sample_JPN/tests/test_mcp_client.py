"""Integration tests for MCPToolProxy (Section 4.2) against a real MCP server
subprocess (mcp_tools_mbpp.py over stdio) - no network or API keys needed.
"""
# ============================================================================
# 日本語解説: このファイルは sandbox/mcp_client.py の MCPToolProxy
# （サンドボックスの外側で動く、MCPサーバーへの同期的なアクセスを提供する
# クラス）を、実際に mcp_tools_mbpp.py を「本物のサブプロセス」として
# 起動した上で結合テストしています。ネットワークもAPIキーも不要ですが、
# 本物のstdio通信・本物のasyncioイベントループを使うので、
# 単体テストというより「結合テスト」に近い性質を持ちます。
#
# 特に重要なのは、MCPツールの呼び出しが「位置引数でもキーワード引数でも
# 動くこと」を確認しているテスト群です。システムプロンプトでは
# 「必ずキーワード引数で呼べ」と指示していますが、これはあくまで
# *助言*であって保証ではありません。実際に課題自身のワークドイグザンプル
# でも位置引数(search_code("validate_email"))が使われているため、
# MCPToolProxyが生成するラッパー関数は両方の呼び出し方に対応している
# 必要があります。
# ============================================================================
import asyncio
import json
import sys
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Iterator, Tuple

import pytest

from sandbox.mcp_client import MCPToolProxy

MCP_TOOLS_MBPP = Path(__file__).resolve().parent.parent / "mcp_tools_mbpp.py"


@pytest.fixture()
def proxy() -> Iterator[MCPToolProxy]:
    # pytestのfixture: 各テスト関数の実行前に、mcp_tools_mbpp.pyを
    # サブプロセスとして起動したMCPToolProxyを1つ用意し(yield)、
    # テストが終わったら(成功でも失敗でも)必ずproxy.close()で
    # 後片付けする。fixture名(proxy)を引数名として受け取るだけで、
    # 各テスト関数はこの準備・後片付けを自分で書かなくてよくなる。
    proxy = MCPToolProxy(stdio_command=f"{sys.executable} {MCP_TOOLS_MBPP}")
    try:
        yield proxy
    finally:
        proxy.close()


def test_tool_call_accepts_keyword_arguments(proxy: MCPToolProxy) -> None:
    # 最も素直な呼び方: run_tests(code=..., test_list=...) という
    # システムプロンプトが推奨する通りのキーワード引数呼び出しが
    # 正常に動作することを確認する。
    run_tests = proxy.build_namespace()["run_tests"]
    code = "def add(a, b):\n    return a + b"
    result = json.loads(run_tests(code=code, test_list=["assert add(1, 2) == 3"]))
    assert result["success"] is True


def test_tool_call_accepts_positional_arguments(proxy: MCPToolProxy) -> None:
    """The subject's own example (Section 3.1) calls tools positionally -
    e.g. ``result = search_code("validate_email")`` - so wrappers must too."""
    # 日本語解説: run_tests(code, test_list) のように、名前を書かず
    # 順番だけで渡す位置引数呼び出し。MCPツールのJSON Schemaのproperties
    # 宣言順を関数の実引数順とみなして*argsをマッピングする、という
    # MCPToolProxy側の実装(_make_wrapper)が機能していることを確認する。
    run_tests = proxy.build_namespace()["run_tests"]
    result = json.loads(run_tests("def add(a, b):\n    return a + b", ["assert add(1, 2) == 3"]))
    assert result["success"] is True


def test_tool_call_accepts_mixed_positional_and_keyword_arguments(proxy: MCPToolProxy) -> None:
    # 1つ目の引数は位置引数、2つ目はキーワード引数、という「混在」した
    # 呼び方も正しく処理できることを確認する。
    run_tests = proxy.build_namespace()["run_tests"]
    result = json.loads(run_tests("def add(a, b):\n    return a + b", test_list=["assert add(1, 2) == 3"]))
    assert result["success"] is True


def test_tool_call_rejects_too_many_positional_arguments(proxy: MCPToolProxy) -> None:
    # run_testsはcode/test_listの2引数しか受け取らないツールなので、
    # 3つ目の位置引数を余分に渡すと、サンドボックス内で暗黙に何かを
    # 推測して実行するのではなく、"positional"という単語を含む
    # 明示的な[Error]メッセージが返ることを確認する。
    run_tests = proxy.build_namespace()["run_tests"]
    result = run_tests("code", ["tests"], "unexpected extra arg")
    assert "[Error]" in result
    assert "positional" in result


def test_tool_call_rejects_duplicate_argument(proxy: MCPToolProxy) -> None:
    # 1つ目の引数(位置引数、実質code)と、キーワード引数code=...を
    # 同時に渡してしまう(=同じパラメータに2つの値を与えようとする)矛盾した
    # 呼び方をした場合、"multiple values"という明確なエラーになることを
    # 確認する（Pythonの通常の関数呼び出しと同じ挙動）。
    run_tests = proxy.build_namespace()["run_tests"]
    result = run_tests("def f(): pass", code="def f(): pass")
    assert "[Error]" in result
    assert "multiple values" in result


def test_manual_text_lists_discovered_tools(proxy: MCPToolProxy) -> None:
    # manual_text()は、接続中のMCPサーバーが公開しているツールの一覧を
    # 人間可読な説明文として自動生成する。ここではrun_testsという
    # ツール名や、その引数名(code, test_list)がちゃんと含まれていることを
    # 確認している。この説明文がそのままprompts.pyのシステムプロンプトに
    # 埋め込まれるので、「未知のMCPサーバーに繋いでも自動的に説明文が
    # 変わる」という仕組みの裏付けになっている。
    manual = proxy.manual_text()
    assert "run_tests" in manual
    assert "code" in manual
    assert "test_list" in manual


def test_connection_timeout_stops_background_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    # MCPサーバーへの接続が(意図的に)永遠にハングする状況を再現し、
    # connect_timeout=0.05(50ミリ秒)という短いタイムアウトを設定した
    # MCPToolProxyが、きちんとTimeoutErrorで諦めることを確認する。
    # さらに重要なのは、タイムアウトした後に「バックグラウンドで
    # 動かしていたasyncioイベントループ用のスレッドがちゃんと後片付け
    # されて残っていない(スレッドリークしていない)こと」まで確認して
    # いる点。接続に失敗したからといってゾンビスレッドが残り続けると、
    # 長時間動くエージェントプロセスでリソースリークにつながる。
    @asynccontextmanager
    async def hanging_client(params: object) -> AsyncIterator[Tuple[object, object]]:
        # 「接続処理が永遠に終わらない」状況を再現する偽のクライアント。
        # asyncio.Event().wait()は誰もset()しない限り永久に待ち続ける。
        await asyncio.Event().wait()
        yield object(), object()

    monkeypatch.setattr("sandbox.mcp_client.stdio_client", hanging_client)
    existing_threads = {thread.ident for thread in threading.enumerate()}

    with pytest.raises(TimeoutError, match="MCP connection timed out"):
        MCPToolProxy(stdio_command="unused", connect_timeout=0.05)

    leaked_threads = [
        thread
        for thread in threading.enumerate()
        if thread.ident not in existing_threads and thread.name == "agent-smith-mcp-loop"
    ]
    assert leaked_threads == []
