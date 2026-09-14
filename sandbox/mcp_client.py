"""公式`mcp` SDKの非同期クライアントに対する、同期的なファサード(仕様書 4.2節)。

サンドボックスのexec()名前空間には`result = search_code("foo")`のような
普通の同期的なPython関数が必要だが、`mcp`パッケージのClientSessionは
asyncioベースで作られている。このモジュールはバックグラウンドスレッドで
1つの永続的なイベントループを動かし、run_coroutine_threadsafeを使って
すべての呼び出しをそのループへ橋渡しすることで、コードベースの他の部分が
asyncioを意識しなくて済むようにしている。

仕様書4.2節が要求する両方のトランスポートに対応する: stdio(MCPサーバーを
サブプロセスとして起動する方式)と streamable HTTP(既に起動済みのサーバーに
接続する方式)。
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import shlex
import threading
from contextlib import AsyncExitStack
from typing import Any, Callable, Dict, List, Optional

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import Tool


CONNECT_TIMEOUT_SECONDS = 30.0
CALL_TOOL_TIMEOUT_SECONDS = 300.0
CLOSE_TIMEOUT_SECONDS = 10.0


class MCPToolProxy:
    """1つのMCPサーバーに接続し、そのツール群を同期的に呼び出せる関数として公開するクラス。

    仕様書4.2節: 「システムは未知のMCPサーバーでテストされる」 - このプロキシは
    ツール名を一切ハードコードせず、list_tools()から動的に発見しラッパーを
    構築するため、仕様に準拠したどのMCPサーバーとでも動作する。
    """

    def __init__(
        self,
        stdio_command: Optional[str] = None,
        http_url: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        connect_timeout: float = CONNECT_TIMEOUT_SECONDS,
    ) -> None:

        if bool(stdio_command) == bool(http_url):
            raise ValueError("Provide exactly one of stdio_command or http_url")
        if connect_timeout <= 0:
            raise ValueError("connect_timeout must be positive")

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_event_loop,
            name="agent-smith-mcp-loop",
            daemon=True,
        )
        self._connection_ready = threading.Event()
        self._owner_stopped = threading.Event()
        self._close_requested = threading.Event()
        self._cancel_requested = threading.Event()
        self._connection_error: Optional[BaseException] = None
        self._owner_task: Optional[asyncio.Task[Any]] = None
        self._connection_args = (stdio_command, http_url, env)
        self._closed = False
        self.session: Optional[ClientSession] = None
        self.tools: List[Tool] = []
        self._thread.start()

        try:
            ready = self._connection_ready.wait(timeout=connect_timeout)
        except BaseException:


            self._stop_owner(graceful=False)
            raise

        if not ready:

            self._stop_owner(graceful=False)
            raise TimeoutError(f"MCP connection timed out after {connect_timeout}s")
        if self._connection_error is not None:

            error = self._connection_error
            self._stop_owner(graceful=False)
            raise error

    def _run_event_loop(self) -> None:


        asyncio.set_event_loop(self._loop)
        self._owner_task = self._loop.create_task(
            self._connection_owner(*self._connection_args)
        )
        self._owner_task.add_done_callback(self._owner_completed)
        self._loop.run_forever()

    def _owner_completed(self, task: asyncio.Task[Any]) -> None:

        try:
            task.result()
        except BaseException as exc:
            if not self._connection_ready.is_set():


                self._connection_error = exc
                self._connection_ready.set()
        finally:
            self._owner_stopped.set()
            self._loop.stop()

    def _run(self, coro: Any, timeout: Optional[float] = None) -> Any:


        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise

    async def _connection_owner(
        self, stdio_command: Optional[str], http_url: Optional[str], env: Optional[Dict[str, str]]
    ) -> None:
        """close()されるまで、全てのトランスポートコンテキストを1つのタスクの中で所有し続ける。

        AnyIOのトランスポートコンテキストは、それに入ったのと同じタスクが
        exitしなければならないタスクグループのキャンセルスコープを内部に持つ。
        この所有者コルーチンを生かし続けることで、タスクをまたいだ
        AsyncExitStackの後始末が失敗する問題を避けている。
        """
        owner_task = asyncio.current_task()

        async def cancel_when_requested() -> None:

            while not self._cancel_requested.is_set():
                await asyncio.sleep(0.05)
            if owner_task is not None:
                owner_task.cancel()

        cancel_monitor = asyncio.create_task(cancel_when_requested())
        try:
            try:
                async with AsyncExitStack() as exit_stack:
                    if stdio_command:

                        parts = shlex.split(stdio_command)
                        params = StdioServerParameters(command=parts[0], args=parts[1:], env=env)
                        read, write = await exit_stack.enter_async_context(stdio_client(params))
                    else:

                        assert http_url is not None
                        read, write, _ = await exit_stack.enter_async_context(
                            streamablehttp_client(http_url)
                        )


                    self.session = await exit_stack.enter_async_context(ClientSession(read, write))
                    await self.session.initialize()
                    result = await self.session.list_tools()
                    self.tools = list(result.tools)
                    self._connection_ready.set()
                    while not self._close_requested.is_set():


                        await asyncio.sleep(0.05)
            except BaseException as exc:
                if not self._connection_ready.is_set():

                    self._connection_error = exc
                    self._connection_ready.set()
        finally:
            cancel_monitor.cancel()
            try:
                await cancel_monitor
            except asyncio.CancelledError:
                pass
            self.session = None
            if not self._connection_ready.is_set():


                self._connection_ready.set()

    def _stop_owner(self, graceful: bool) -> None:


        if graceful:
            self._close_requested.set()
        else:
            self._cancel_requested.set()

        wait_timeout = CLOSE_TIMEOUT_SECONDS if graceful else 1.0
        if not self._owner_stopped.wait(timeout=wait_timeout):

            self._cancel_requested.set()
            if self._owner_task is not None and self._loop.is_running():
                self._loop.call_soon_threadsafe(self._owner_task.cancel)
            self._owner_stopped.wait(timeout=1.0)

        if self._thread.is_alive() and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5.0)
        if not self._thread.is_alive() and not self._loop.is_closed():
            self._loop.close()

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> str:
        """1つのMCPツールを同期的に呼び出し、そのテキスト内容を返す。

        CALL_TOOL_TIMEOUT_SECONDSで上限を設けており、死んだ・ハングした
        MCPサーバーがあっても、サンドボックス(ひいてはエージェントループ)を
        無期限にブロックするのではなく、明示的なObservationとして返せる
        ようにしている - これはサンドボックス自身のSIGALRMベースの実行
        タイムアウトと並ぶ多層防御である。この呼び出しは通常そのSIGALRM
        タイムアウトの管理下でも実行されるが、このモジュールは、それが
        純粋なPythonコードを中断させるのと全く同じようにスレッドロックの
        待機を必ず中断させてくれるとは想定できないため、独自の上限も設けている。
        """
        session = self.session
        if session is None:
            return "[Error] MCP session is not connected"
        try:

            result = self._run(session.call_tool(name, arguments), timeout=CALL_TOOL_TIMEOUT_SECONDS)
        except concurrent.futures.TimeoutError:
            return f"[Error] tool '{name}' timed out after {CALL_TOOL_TIMEOUT_SECONDS}s"
        parts = []
        for item in result.content:

            text = getattr(item, "text", None)
            parts.append(text if text is not None else str(item))
        text_result = "\n".join(parts)
        if getattr(result, "isError", False):

            return f"[Error] tool '{name}' failed: {text_result}"
        return text_result

    def build_namespace(self) -> Dict[str, Callable[..., str]]:
        """発見した各ツールにつき1つずつ、同期的なラッパー関数を用意し、
        サンドボックスのexec()名前空間にそのまま組み込めるようにする(仕様書 4.2節)。"""
        namespace: Dict[str, Callable[..., str]] = {}
        for tool in self.tools:
            namespace[tool.name] = self._make_wrapper(tool)
        return namespace

    def _make_wrapper(self, tool: Tool) -> Callable[..., str]:
        """通常のPython関数のように、位置引数でもキーワード引数でも受け取れる
        ラッパーを構築する - 課題自体のサンプル(仕様書3.1節)ではツールを
        位置引数で呼んでおり(``result = search_code("validate_email")``)、
        こちらのシステムプロンプトの「常にキーワード引数を使うこと」という
        指示はLLMへの助言に過ぎず、LLMが必ず従うという保証にはならない。
        位置引数は、MCPツールスキーマで宣言されたプロパティの並び順を使って
        パラメータ名にマッピングされる。この並び順は、FastMCPベースの
        サーバー(このプロジェクトのもの、および実質的に他の準拠実装)では、
        元の関数の実際の引数順と一致する。"""
        name = tool.name

        param_names = list((tool.inputSchema or {}).get("properties", {}).keys())

        def wrapper(*args: Any, **kwargs: Any) -> str:
            if len(args) > len(param_names):

                return (
                    f"[Error] {name}() takes at most {len(param_names)} positional "
                    f"arguments but {len(args)} were given"
                )
            arguments = dict(zip(param_names, args))
            duplicates = arguments.keys() & kwargs.keys()
            if duplicates:
                return f"[Error] {name}() got multiple values for {sorted(duplicates)}"
            arguments.update(kwargs)
            return self.call_tool(name, arguments)

        wrapper.__name__ = name
        return wrapper

    def manual_text(self) -> str:
        """接続中のサーバーが提供するツール群を、システムプロンプト用の
        ドキュメントとして整形して返す(仕様書 4.2節 - サンドボックスの
        マニュアルは、接続されたMCPサーバーのツールスキーマから動的に
        生成されなければならない)。"""
        if not self.tools:
            return "(no MCP tools are currently connected)"
        lines = []
        for tool in self.tools:
            schema = tool.inputSchema or {}
            properties = schema.get("properties", {})
            required = set(schema.get("required", []))
            params = []
            for pname, pschema in properties.items():
                ptype = pschema.get("type", "any")
                marker = "" if pname in required else "?"
                params.append(f"{pname}{marker}: {ptype}")
            signature = ", ".join(params)
            description = (tool.description or "").strip()
            lines.append(f"- {tool.name}({signature})\n    {description}")
        return "\n".join(lines)

    def close(self) -> None:
        """CLOSE_TIMEOUT_SECONDSで上限を設けた、ベストエフォートなシャットダウン処理
        - 呼び出し元(agent_mbpp.py/agent_swebench.pyの`finally`ブロック)は、
        たとえMCPサーバーが既に死んでいたりスタックしていたりしても、この
        呼び出しが速やかに返ることに依存している。というのも、
        agent_swebench.pyでは、この呼び出しの直後の行が
        `container.cleanup()`であり、それがこの呼び出しによって
        飢餓状態(スタベーション)にされてはならないからである。"""
        if self._closed:
            return
        self._closed = True
        self._stop_owner(graceful=True)
