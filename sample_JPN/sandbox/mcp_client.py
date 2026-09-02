"""Synchronous facade over the official `mcp` SDK's async client (Section 4.2).

The sandbox needs plain, synchronous Python functions in its exec() namespace
- `result = search_code("foo")` - but the `mcp` package's ClientSession is
asyncio-based. This runs one persistent event loop in a background thread and
bridges every call across it with run_coroutine_threadsafe, so the rest of the
codebase never has to think about asyncio.

Supports both required transports (Section 4.2): stdio (spawns the MCP server
as a subprocess) and streamable HTTP (connects to an already-running server).

# ============================================================================
# 【日本語解説】このファイルの立ち位置 ── なぜこんなに複雑なのか
# ============================================================================
# MCPプロトコル（ツール発見・呼び出し）の公式SDK(`mcp`パッケージ)は
# asyncioベースで作られています。しかしサンドボックス側
# (sandbox/executor.py)のexec()名前空間には、`result = search_code("foo")`
# のような**ただの同期関数呼び出し**が必要です。LLMが生成するコードに
# `await`や`async def`を書かせるわけにはいきません。
#
# そのギャップを埋めるのがこの MCPToolProxy クラスです。やっていることを
# 一言でいうと「非同期のMCPクライアントを、専用のバックグラウンドスレッド
# の中に閉じ込め、そのスレッドとメインスレッドの間を
# asyncio.run_coroutine_threadsafe() で橋渡しすることで、外から見ると
# 完全に同期的なAPIに見せかける」というテクニックです。
#
# これにより、このファイルより外側のコード（executor.py、
# isolated_worker.py、agent_mbpp.py等）は asyncio を一切意識せずに
# 済みます。
# ============================================================================
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

# Generous but finite: a legitimate tool call can run a whole test suite
# (mcp_tools_swebench.py's run_tests()) or a long shell command, but an MCP
# server that has died or deadlocked must not be able to hang the sandbox
# (and, in turn, agent_mbpp.py/agent_swebench.py's cleanup/solution.json
# write) forever. Connection establishment and shutdown have shorter bounds so
# a dead server cannot block startup or container cleanup indefinitely.
# ----------------------------------------------------------------------------
# 【日本語解説】3種類のタイムアウトが用意されている理由
# ----------------------------------------------------------------------------
# - CONNECT_TIMEOUT_SECONDS (30秒): MCPサーバーへの接続確立に許す時間。
#   サブプロセス起動やHTTP接続開始が異常に遅い場合、ここで見切りをつける。
# - CALL_TOOL_TIMEOUT_SECONDS (300秒=5分): 1回のツール呼び出しに許す時間。
#   SWE-benchのrun_tests()がテストスイート全体を回すこともあるため長め。
#   ただし無限ではない ── サーバーが死んでいたりデッドロックしていたら、
#   ここで打ち切ってエラーメッセージに変換する。
# - CLOSE_TIMEOUT_SECONDS (10秒): 後片付け(close)に許す時間。
#   agent_swebench.pyのfinallyブロックでは、この直後に
#   container.cleanup()が控えているため、closeがここで無期限に
#   ハングするとDockerコンテナの後始末まで巻き添えで止まってしまう。
# どの境界にも必ず「有限の」タイムアウトが設定されている、という
# 一貫した設計方針が見える。
# ----------------------------------------------------------------------------
CONNECT_TIMEOUT_SECONDS = 30.0
CALL_TOOL_TIMEOUT_SECONDS = 300.0
CLOSE_TIMEOUT_SECONDS = 10.0


class MCPToolProxy:
    """Connects to one MCP server and exposes its tools as synchronous callables.

    Section 4.2: "the system will be tested with an unknown MCP server" - this
    proxy never hardcodes tool names, it discovers them from list_tools() and
    builds wrappers dynamically, so it works with any compliant MCP server.
    """
    # 【日本語解説】
    # クラスdocstringにある通り、このクラスはツール名を一切
    # ハードコードしません。接続後にlist_tools()を呼んで初めて
    # 「このMCPサーバーにはどんなツールがあるか」を知り、そこから
    # 動的にラッパー関数を生成します（build_namespace参照）。これが
    # 「未知のMCPサーバーに繋いでテストされる」という課題要件への
    # 直接的な対応です。

    def __init__(
        self,
        stdio_command: Optional[str] = None,
        http_url: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        connect_timeout: float = CONNECT_TIMEOUT_SECONDS,
    ) -> None:
        # 【日本語解説】
        # stdio_command（サブプロセスとして起動するコマンド文字列）か
        # http_url（既に起動済みのHTTPサーバーのURL）のどちらか
        # "ちょうど1つ"だけを受け取る。bool(a) == bool(b) は
        # 「両方Noneでない」または「両方None」のどちらかならTrueになる
        # ため、XOR的に「ちょうど片方だけ指定」を強制するイディオム。
        if bool(stdio_command) == bool(http_url):
            raise ValueError("Provide exactly one of stdio_command or http_url")
        if connect_timeout <= 0:
            raise ValueError("connect_timeout must be positive")

        # ------------------------------------------------------------------
        # 【日本語解説】専用イベントループとバックグラウンドスレッドの準備
        # ------------------------------------------------------------------
        # asyncio.new_event_loop() で、メインスレッドのイベントループとは
        # 完全に独立した専用のイベントループを作る。これを別スレッド
        # (self._thread)の中で self._run_event_loop() として動かす。
        # 以降、MCP関連の非同期処理はすべてこのループの上で実行される。
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_event_loop,
            name="agent-smith-mcp-loop",
            daemon=True,
        )
        # 【日本語解説】
        # スレッド間の同期に使う各種Event。
        #   _connection_ready: 接続確立(成功/失敗どちらか確定)を知らせる
        #   _owner_stopped: _connection_owner()コルーチンが終了したことを知らせる
        #   _close_requested: 「正常にcloseしてほしい」という要求
        #   _cancel_requested: 「強制的にキャンセルしてほしい」という要求
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

        # 【日本語解説】
        # __init__自体は同期関数として呼ばれるので、ここでバックグラウンド
        # スレッドが接続を確立する（またはタイムアウト/失敗する）まで、
        # メインスレッド側は_connection_ready.wait()でブロックして待つ。
        try:
            ready = self._connection_ready.wait(timeout=connect_timeout)
        except BaseException:
            # 待機中にKeyboardInterruptなどが飛んできても、バックグラウンド
            # スレッドを放置せず後片付けしてから再送出する。
            self._stop_owner(graceful=False)
            raise

        if not ready:
            self._stop_owner(graceful=False)
            raise TimeoutError(f"MCP connection timed out after {connect_timeout}s")
        if self._connection_error is not None:
            # 接続中にエラーが起きていた場合、そのままそのエラーを
            # 呼び出し元に伝播させる。
            error = self._connection_error
            self._stop_owner(graceful=False)
            raise error

    def _run_event_loop(self) -> None:
        # 【日本語解説】
        # バックグラウンドスレッドのエントリポイント。このスレッドに
        # 専用のイベントループを紐付け(asyncio.set_event_loop)、
        # _connection_owner()コルーチンをタスクとしてスケジュールしてから
        # run_forever()で無期限にイベントループを回し続ける。
        # （run_forever自体は_owner_completedがloop.stop()するまで
        #  ブロックし続ける ＝ このスレッドの生存期間そのもの）
        asyncio.set_event_loop(self._loop)
        self._owner_task = self._loop.create_task(
            self._connection_owner(*self._connection_args)
        )
        self._owner_task.add_done_callback(self._owner_completed)
        self._loop.run_forever()

    def _owner_completed(self, task: asyncio.Task[Any]) -> None:
        # 【日本語解説】
        # _connection_owner()タスクが完了（正常終了・例外・キャンセル
        # いずれか）した際に呼ばれるコールバック。
        try:
            task.result()  # 例外があればここで再送出される
        except BaseException as exc:
            # 【日本語解説】
            # まだ接続完了(ready)を通知していない段階で owner タスクが
            # 死んだ場合（＝接続確立に失敗した場合）、そのエラーを
            # _connection_errorに記録してから_connection_readyを立てる。
            # これにより__init__側の待機が「エラーとともに」解除される。
            if not self._connection_ready.is_set():
                self._connection_error = exc
                self._connection_ready.set()
        finally:
            self._owner_stopped.set()
            self._loop.stop()  # run_forever()を終了させる ＝ スレッドが終わる

    def _run(self, coro: Any, timeout: Optional[float] = None) -> Any:
        # ------------------------------------------------------------------
        # 【日本語解説】このクラスの心臓部 ── 同期↔非同期の橋渡し1行
        # ------------------------------------------------------------------
        # asyncio.run_coroutine_threadsafe(coro, self._loop) が、
        # 「別スレッドで動いているイベントループに、コルーチンの実行を
        # スレッドセーフに投げ込む」標準ライブラリの仕組み。戻り値は
        # concurrent.futures.Future（asyncioのFutureではなく、通常の
        # スレッド間で使えるFuture）で、future.result(timeout=...)は
        # 普通の同期呼び出しとしてブロックして結果を待てる。
        # これが「呼び出し元(メインスレッド)からは同期関数に見える」
        # 仕組みの正体そのもの。
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            # タイムアウトした場合、投げっぱなしにせずfuture.cancel()で
            # 実行中のコルーチンにもキャンセルを伝える。
            future.cancel()
            raise

    async def _connection_owner(
        self, stdio_command: Optional[str], http_url: Optional[str], env: Optional[Dict[str, str]]
    ) -> None:
        """Own all transport contexts in one task until close().

        AnyIO transport contexts contain task-group cancel scopes which must be
        exited by the same task that entered them. Keeping this owner coroutine
        alive avoids cross-task AsyncExitStack teardown failures.
        """
        # ----------------------------------------------------------------------
        # 【日本語解説】「1つのオーナーコルーチンが全トランスポートを所有する」設計
        # ----------------------------------------------------------------------
        # docstringにある通り、AnyIO（stdio_client/streamablehttp_clientの
        # 内部実装が使うライブラリ）のトランスポートコンテキストは、
        # 「enter したのと同じタスク（コルーチン）が exit しなければ
        # ならない」という制約を持っています。もし接続処理と切断処理を
        # 別々のタスク・別々の呼び出しから行おうとすると、内部の
        # キャンセルスコープが壊れてエラーになります。
        #
        # そこでこのプロジェクトは、接続確立(enter_async_context)から
        # close要求を待つ間(while ... sleep)、そして最終的な後始末
        # (AsyncExitStackのexit)までを**1つの長寿命コルーチン**として
        # 実装しています。呼び出し元（他のスレッド）は、このコルーチンに
        # 対して「閉じてほしい」という"要求"（Eventのセット）を送るだけで、
        # 実際にexitを呼ぶのは常にこのコルーチン自身、という設計です。
        # ----------------------------------------------------------------------
        owner_task = asyncio.current_task()

        async def cancel_when_requested() -> None:
            # 【日本語解説】
            # _cancel_requested Eventがセットされるのを0.05秒間隔で
            # ポーリングし、セットされたらowner_task自身をキャンセルする
            # 監視用の別タスク。「強制終了」経路(graceful=False)で使われる。
            while not self._cancel_requested.is_set():
                await asyncio.sleep(0.05)
            if owner_task is not None:
                owner_task.cancel()

        cancel_monitor = asyncio.create_task(cancel_when_requested())
        try:
            try:
                async with AsyncExitStack() as exit_stack:
                    # 【日本語解説】
                    # stdio_commandが指定されていれば、そのコマンドを
                    # shlex.splitで分割してサブプロセスとしてMCPサーバーを
                    # 起動する(stdio_client)。http_urlならストリーミング
                    # HTTP接続(streamablehttp_client)を張る。どちらの経路も
                    # 最終的に read/write の非同期ストリームペアを得る点は
                    # 共通で、この後のClientSessionは輸送方式の違いを
                    # 意識しない。
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
                    # 【日本語解説】
                    # ここがツール名ハードコード無しの核心 ──
                    # list_tools()を呼んで、接続先のMCPサーバーが実際に
                    # 公開しているツールの一覧(スキーマ付き)を取得する。
                    result = await self.session.list_tools()
                    self.tools = list(result.tools)
                    # 接続とツール発見が完了したので、__init__側の
                    # 待機を解除する。
                    self._connection_ready.set()
                    # 【日本語解説】
                    # ここが「接続を維持し続ける」部分。close要求
                    # (_close_requested)が来るまで、ただ0.05秒間隔で
                    # スリープし続けるだけのループ。この間、
                    # ClientSessionは生きたままなので、他のスレッドから
                    # call_tool()経由で何度でもツールを呼べる。
                    while not self._close_requested.is_set():
                        await asyncio.sleep(0.05)
            except BaseException as exc:
                if not self._connection_ready.is_set():
                    # まだ準備完了を通知していない段階で例外が起きた
                    # （＝接続確立自体に失敗した）場合は、エラーとして
                    # 記録して待機を解除する。
                    self._connection_error = exc
                    self._connection_ready.set()
        finally:
            # 【日本語解説】
            # async with AsyncExitStack() ブロックを抜けると
            # （正常終了・close要求・例外いずれの経路でも）、
            # 自動的にenter_async_contextで入ったコンテキスト
            # （ClientSession、stdio_client/streamablehttp_client）が
            # 逆順にexitされる。これが「同じタスクの中でenter/exitが
            # 完結する」ことの実現方法。
            cancel_monitor.cancel()
            try:
                await cancel_monitor
            except asyncio.CancelledError:
                pass
            self.session = None
            if not self._connection_ready.is_set():
                self._connection_ready.set()

    def _stop_owner(self, graceful: bool) -> None:
        # ------------------------------------------------------------------
        # 【日本語解説】オーナーコルーチンを止める2つの経路
        # ------------------------------------------------------------------
        # graceful=True（通常のclose()経路）: _close_requestedを立てて、
        #   _connection_owner()内のwhileループが自然に抜けるのを待つ
        #   （＝ClientSessionを正しい手順で閉じる、丁寧な終了）。
        # graceful=False（接続失敗時やタイムアウト時の緊急停止）:
        #   _cancel_requestedを立てて、cancel_when_requested()経由で
        #   owner_taskを強制キャンセルする（＝多少手荒でも即座に止める）。
        # ------------------------------------------------------------------
        if graceful:
            self._close_requested.set()
        else:
            self._cancel_requested.set()

        wait_timeout = CLOSE_TIMEOUT_SECONDS if graceful else 1.0
        if not self._owner_stopped.wait(timeout=wait_timeout):
            # 【日本語解説】
            # graceful要求のはずが時間内に終わらなかった場合の保険。
            # 待っていても終わらないなら、強制キャンセルに切り替える
            # （loop.call_soon_threadsafeで、別スレッドのイベントループに
            #  安全にキャンセル要求を注入する）。
            self._cancel_requested.set()
            if self._owner_task is not None and self._loop.is_running():
                self._loop.call_soon_threadsafe(self._owner_task.cancel)
            self._owner_stopped.wait(timeout=1.0)

        if self._thread.is_alive() and self._loop.is_running():
            # イベントループ自体にもrun_forever()を止めるよう指示する。
            self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5.0)
        if not self._thread.is_alive() and not self._loop.is_closed():
            self._loop.close()

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> str:
        """Synchronously invoke one MCP tool and return its text content.

        Bounded by CALL_TOOL_TIMEOUT_SECONDS so a dead/hung MCP server becomes
        an explicit Observation instead of blocking the sandbox (and the
        agent loop) indefinitely - defense in depth alongside the sandbox's
        own SIGALRM-based execution timeout, which this call normally runs
        under too, but which this module cannot assume will always interrupt
        a thread-lock wait the same way it interrupts pure-Python code.
        """
        # 【日本語解説】
        # このメソッドが、生成されたラッパー関数(_make_wrapper参照)から
        # 実際に呼ばれる「本体」。self._run()で非同期のsession.call_tool()
        # をバックグラウンドループに投げ、CALL_TOOL_TIMEOUT_SECONDS
        # （5分）でタイムアウトさせる。docstringの但し書きが重要:
        # executor.py側のsignal.alarm()によるタイムアウトは「純粋な
        # Pythonコードの実行」には効くが、スレッド間のロック待ちのような
        # 状況では必ずしも同じように割り込めるとは限らないため、
        # この関数自身も独立したタイムアウトを持っている（多層防御）。
        session = self.session
        if session is None:
            return "[Error] MCP session is not connected"
        try:
            result = self._run(session.call_tool(name, arguments), timeout=CALL_TOOL_TIMEOUT_SECONDS)
        except concurrent.futures.TimeoutError:
            return f"[Error] tool '{name}' timed out after {CALL_TOOL_TIMEOUT_SECONDS}s"
        # 【日本語解説】
        # MCPツールの戻り値(result.content)は、テキスト以外の型
        # （画像など）も含みうる汎用的な構造。ここではtext属性がある
        # ものはそのテキストを、無ければstr()化したものを使い、
        # 複数パートを改行で連結して1つの文字列に正規化する
        # （サンドボックスの名前空間に置く関数はすべて「文字列を返す」
        # という単純な契約に統一している）。
        parts = []
        for item in result.content:
            text = getattr(item, "text", None)
            parts.append(text if text is not None else str(item))
        text_result = "\n".join(parts)
        if getattr(result, "isError", False):
            return f"[Error] tool '{name}' failed: {text_result}"
        return text_result

    def build_namespace(self) -> Dict[str, Callable[..., str]]:
        """One synchronous wrapper function per discovered tool, ready to drop
        straight into the sandbox's exec() namespace (Section 4.2)."""
        # 【日本語解説】
        # list_tools()で発見した各ツールについて、_make_wrapper()で
        # 同期ラッパー関数を1つずつ作り、{ツール名: ラッパー関数}の辞書に
        # まとめる。これがそのまま Sandbox(config, extra_namespace=...)
        # の extra_namespace 引数として渡される（agent_mbpp.py /
        # agent_swebench.py参照）。
        namespace: Dict[str, Callable[..., str]] = {}
        for tool in self.tools:
            namespace[tool.name] = self._make_wrapper(tool)
        return namespace

    def _make_wrapper(self, tool: Tool) -> Callable[..., str]:
        """Build a wrapper that accepts a tool's arguments either positionally
        or by keyword, like a normal Python function - the subject's own
        example (Section 3.1) calls tools positionally
        (``result = search_code("validate_email")``), and our system prompt's
        "always use keyword arguments" guidance is advice to the LLM, not a
        constraint an LLM is guaranteed to follow. Positional arguments are
        mapped to parameter names using the MCP tool schema's declared
        property order, which matches the underlying function's real
        parameter order for a FastMCP-based server (ours and, in practice,
        any other compliant one)."""
        # ----------------------------------------------------------------------
        # 【日本語解説】位置引数もキーワード引数も受け付ける理由（実際のバグ経緯）
        # ----------------------------------------------------------------------
        # prompts.py（Section 7参照）のシステムプロンプトは「必ず
        # キーワード引数で呼べ」とLLMに指示しています。しかしこれは
        # あくまで**助言**であって、LLMが必ず守る保証はありません。
        # 実際、課題自身のワークドイグザンプルにすら
        # `result = search_code("validate_email")` という**位置引数**の
        # 呼び出しが登場します。
        #
        # もしラッパー関数がキーワード引数しか受け付けない実装だったら、
        # LLMがこのお手本通りに書いただけで
        # `wrapper() takes 0 positional arguments but 1 was given`
        # のような実行時エラーになってしまいます（実際にこのバグが
        # 起きたことがBENCHMARK_REPORT.mdに記録されています）。
        #
        # 対策として、MCPツールのJSON Schemaが持つ`properties`の
        # **宣言順**を「実引数の並び順」とみなし、位置引数をその順番で
        # パラメータ名にマッピングしてから、キーワード引数とマージする、
        # という実装になっています。
        # ----------------------------------------------------------------------
        name = tool.name
        param_names = list((tool.inputSchema or {}).get("properties", {}).keys())

        def wrapper(*args: Any, **kwargs: Any) -> str:
            if len(args) > len(param_names):
                return (
                    f"[Error] {name}() takes at most {len(param_names)} positional "
                    f"arguments but {len(args)} were given"
                )
            # 【日本語解説】
            # zip(param_names, args) で「パラメータ名の順番」と
            # 「渡された位置引数」を対応付ける。例えば
            # search_code(pattern, file_pattern) というツールに対して
            # search_code("foo") と呼ばれたら {"pattern": "foo"} になる。
            arguments = dict(zip(param_names, args))
            duplicates = arguments.keys() & kwargs.keys()
            if duplicates:
                # 位置引数とキーワード引数の両方で同じパラメータを
                # 指定してしまった場合（普通のPython関数と同じエラー
                # 挙動）を検出する。
                return f"[Error] {name}() got multiple values for {sorted(duplicates)}"
            arguments.update(kwargs)
            return self.call_tool(name, arguments)

        wrapper.__name__ = name  # デバッグ時にwrapper.__name__ではなくツール名が見えるように
        return wrapper

    def manual_text(self) -> str:
        """Render the connected server's tools as documentation for the system
        prompt (Section 4.2 - the sandbox manual must be generated dynamically
        from the connected MCP server's tool schemas)."""
        # 【日本語解説】
        # prompts.py の build_system_prompt() に埋め込まれる、
        # 「使えるツール一覧」のドキュメント文字列をここで動的に生成する。
        # ツール名・パラメータ名・型・必須かどうか(?マーク)・説明文を
        # JSON Schemaから自動整形するので、prompts.py側は接続先の
        # MCPサーバーが何であるかを一切知らなくてよい。
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
                marker = "" if pname in required else "?"  # 必須でないパラメータには"?"を付けて可読に示す
                params.append(f"{pname}{marker}: {ptype}")
            signature = ", ".join(params)
            description = (tool.description or "").strip()
            lines.append(f"- {tool.name}({signature})\n    {description}")
        return "\n".join(lines)

    def close(self) -> None:
        """Best-effort shutdown, bounded by CLOSE_TIMEOUT_SECONDS - callers
        (agent_mbpp.py/agent_swebench.py's `finally` blocks) rely on this
        returning promptly even if the MCP server already died or is stuck,
        since agent_swebench.py's `container.cleanup()` is the very next
        line after this call and must not be starved by it."""
        # 【日本語解説】
        # 二重closeを防ぐガードのあと、_stop_owner(graceful=True)で
        # 「丁寧な」終了を試みる。docstringにある通り、
        # agent_swebench.pyではこの直後にcontainer.cleanup()が続くため、
        # ここが無期限にハングするとDockerコンテナの後片付けまで
        # 巻き添えで止まってしまう ── だからこそCLOSE_TIMEOUT_SECONDSで
        # 必ず有限時間内に処理が戻ることが保証されている。
        if self._closed:
            return
        self._closed = True
        self._stop_owner(graceful=True)
