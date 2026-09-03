"""MCPToolProxy(セクション4.2)の統合テスト。実際のMCPサーバーサブプロセス
(mcp_tools_mbpp.pyをstdio経由で起動)に対して行う - ネットワークもAPIキーも不要。
"""
import asyncio  # 非同期処理(タイムアウトテストのイベント待機)に使用
import json  # ツール呼び出し結果のJSONデコードに使用
import sys  # 現在のPython実行ファイルパス取得に使用
import threading  # バックグラウンドスレッドのリーク検知に使用
from contextlib import asynccontextmanager  # 非同期コンテキストマネージャ定義に使用
from pathlib import Path  # ファイルパス操作に使用
from typing import AsyncIterator, Iterator, Tuple  # 型ヒントに使用

import pytest  # fixture定義・monkeypatch・pytest.raisesに使用

from sandbox.mcp_client import MCPToolProxy  # テスト対象のMCPクライアントプロキシ

# mcp_tools_mbpp.pyへのパス(このテストファイルの2階層上、プロジェクトルート直下)
MCP_TOOLS_MBPP = Path(__file__).resolve().parent.parent / "mcp_tools_mbpp.py"


@pytest.fixture()
def proxy() -> Iterator[MCPToolProxy]:
    # mcp_tools_mbpp.pyをサブプロセスとして起動するMCPToolProxyを用意するfixture
    proxy = MCPToolProxy(stdio_command=f"{sys.executable} {MCP_TOOLS_MBPP}")
    try:
        yield proxy  # テスト本体にproxyを渡す
    finally:
        proxy.close()  # テスト終了後は必ずプロキシ(サブプロセス)を閉じる


def test_tool_call_accepts_keyword_arguments(proxy: MCPToolProxy) -> None:
    # run_testsツールをキーワード引数で呼び出せることを検証
    run_tests = proxy.build_namespace()["run_tests"]
    code = "def add(a, b):\n    return a + b"
    result = json.loads(run_tests(code=code, test_list=["assert add(1, 2) == 3"]))
    assert result["success"] is True  # テストが成功として報告されること


def test_tool_call_accepts_positional_arguments(proxy: MCPToolProxy) -> None:
    """課題本文の例(セクション3.1)ではツールを位置引数で呼び出している -
    例えば ``result = search_code("validate_email")`` のように - ため、ラッパー側もそれに対応する必要がある。"""
    run_tests = proxy.build_namespace()["run_tests"]
    result = json.loads(run_tests("def add(a, b):\n    return a + b", ["assert add(1, 2) == 3"]))
    assert result["success"] is True  # テストが成功として報告されること


def test_tool_call_accepts_mixed_positional_and_keyword_arguments(proxy: MCPToolProxy) -> None:
    # 位置引数とキーワード引数を混在させて呼び出しても正しく動作することを検証
    run_tests = proxy.build_namespace()["run_tests"]
    result = json.loads(run_tests("def add(a, b):\n    return a + b", test_list=["assert add(1, 2) == 3"]))
    assert result["success"] is True  # テストが成功として報告されること


def test_tool_call_rejects_too_many_positional_arguments(proxy: MCPToolProxy) -> None:
    # 余分な位置引数を渡した場合、明示的なエラーが返ることを検証
    run_tests = proxy.build_namespace()["run_tests"]
    result = run_tests("code", ["tests"], "unexpected extra arg")
    assert "[Error]" in result  # エラーであることを示す接頭辞が含まれること
    assert "positional" in result  # 位置引数に関するエラーメッセージであること


def test_tool_call_rejects_duplicate_argument(proxy: MCPToolProxy) -> None:
    # 同じ引数を位置引数とキーワード引数の両方で重複指定した場合、明示的なエラーが返ることを検証
    run_tests = proxy.build_namespace()["run_tests"]
    result = run_tests("def f(): pass", code="def f(): pass")
    assert "[Error]" in result  # エラーであることを示す接頭辞が含まれること
    assert "multiple values" in result  # 引数が重複して渡されたことを示すメッセージであること


def test_manual_text_lists_discovered_tools(proxy: MCPToolProxy) -> None:
    # マニュアルテキストに、発見されたツール名やパラメータ名が含まれることを検証
    manual = proxy.manual_text()
    assert "run_tests" in manual  # ツール名が含まれること
    assert "code" in manual  # パラメータ名codeが含まれること
    assert "test_list" in manual  # パラメータ名test_listが含まれること


def test_connection_timeout_stops_background_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    # 接続がハングした場合にタイムアウト例外が発生し、
    # かつバックグラウンドのイベントループスレッドがリークしないことを検証
    @asynccontextmanager
    async def hanging_client(params: object) -> AsyncIterator[Tuple[object, object]]:
        # 決して完了しない(接続がハングしたことを模倣する)非同期コンテキストマネージャ
        await asyncio.Event().wait()
        yield object(), object()

    monkeypatch.setattr("sandbox.mcp_client.stdio_client", hanging_client)  # stdio_clientをハング版に差し替え
    existing_threads = {thread.ident for thread in threading.enumerate()}  # 既存スレッドのIDを記録

    # 接続タイムアウトによりTimeoutErrorが送出されることを確認
    with pytest.raises(TimeoutError, match="MCP connection timed out"):
        MCPToolProxy(stdio_command="unused", connect_timeout=0.05)

    # 新たに作られたスレッドの中に、MCP用バックグラウンドループのスレッドが
    # 残っていないか(リークしていないか)を確認する
    leaked_threads = [
        thread
        for thread in threading.enumerate()
        if thread.ident not in existing_threads and thread.name == "agent-smith-mcp-loop"
    ]
    assert leaked_threads == []  # リークしたスレッドが存在しないこと
