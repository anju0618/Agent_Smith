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

import asyncio  # 非同期I/O(MCPクライアントの実体)を扱うため
import concurrent.futures  # スレッド間でのFuture結果取得・タイムアウト処理用
import shlex  # stdio起動コマンド文字列をシェル的に安全に分割するため
import threading  # イベントループを動かす専用バックグラウンドスレッド用
from contextlib import AsyncExitStack  # 複数の非同期コンテキストマネージャをまとめて管理するため
from typing import Any, Callable, Dict, List, Optional

from mcp import ClientSession  # MCPサーバーとの1セッションを表すクライアント
from mcp.client.stdio import StdioServerParameters, stdio_client  # stdioトランスポート用
from mcp.client.streamable_http import streamablehttp_client  # streamable HTTPトランスポート用
from mcp.types import Tool  # MCPツールのスキーマ情報を表す型

# 十分に長いが有限: 正当なツール呼び出しはテストスイート全体を実行することもあれば
# (mcp_tools_swebench.pyのrun_tests())、長時間かかるシェルコマンドのこともある。
# しかし、死んだり・デッドロックしたりしたMCPサーバーがサンドボックス
# (ひいてはagent_mbpp.py/agent_swebench.pyのクリーンアップ処理やsolution.json
# の書き込み)を永遠にハングさせてしまうことがあってはならない。接続確立と
# 切断についてはより短い上限を設けており、死んだサーバーが起動処理や
# コンテナのクリーンアップを無期限にブロックしないようにしている。
CONNECT_TIMEOUT_SECONDS = 30.0  # MCPサーバーへの接続確立を待つ最大秒数
CALL_TOOL_TIMEOUT_SECONDS = 300.0  # 1回のツール呼び出しを待つ最大秒数
CLOSE_TIMEOUT_SECONDS = 10.0  # 切断処理を待つ最大秒数


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
        # stdio_commandとhttp_urlはどちらか一方だけを指定する必要がある(XOR)
        if bool(stdio_command) == bool(http_url):
            raise ValueError("Provide exactly one of stdio_command or http_url")
        if connect_timeout <= 0:
            raise ValueError("connect_timeout must be positive")

        self._loop = asyncio.new_event_loop()  # このプロキシ専用の新しいイベントループを作成
        self._thread = threading.Thread(
            target=self._run_event_loop,  # このスレッドの実体はイベントループを回し続ける関数
            name="agent-smith-mcp-loop",
            daemon=True,  # メインプロセス終了時にこのスレッドが道連れで終了できるようにする
        )
        self._connection_ready = threading.Event()  # 接続確立(成功/失敗問わず)完了を知らせるイベント
        self._owner_stopped = threading.Event()  # 接続所有タスク(_connection_owner)が終了したことを知らせる
        self._close_requested = threading.Event()  # 正常な切断を要求されたことを示すフラグ
        self._cancel_requested = threading.Event()  # 強制キャンセルを要求されたことを示すフラグ
        self._connection_error: Optional[BaseException] = None  # 接続確立中に発生したエラーを保持
        self._owner_task: Optional[asyncio.Task[Any]] = None  # 接続を所有し続けるasyncioタスク
        self._connection_args = (stdio_command, http_url, env)  # 接続確立に必要な引数一式
        self._closed = False  # 既にclose()済みかどうか
        self.session: Optional[ClientSession] = None  # 接続済みのMCPクライアントセッション
        self.tools: List[Tool] = []  # 接続先サーバーから発見されたツール一覧
        self._thread.start()  # バックグラウンドでイベントループスレッドを起動

        try:
            ready = self._connection_ready.wait(timeout=connect_timeout)  # 接続完了(または失敗)を待つ
        except BaseException:
            # 待機中に(例えばKeyboardInterrupt等で)割り込まれた場合も、
            # バックグラウンドスレッドを確実に停止させてから再送出する
            self._stop_owner(graceful=False)
            raise

        if not ready:
            # タイムアウトした場合は接続所有タスクを止めてから例外化する
            self._stop_owner(graceful=False)
            raise TimeoutError(f"MCP connection timed out after {connect_timeout}s")
        if self._connection_error is not None:
            # 接続処理中に例外が起きていた場合、それをこの呼び出し元スレッドで再送出する
            error = self._connection_error
            self._stop_owner(graceful=False)
            raise error

    def _run_event_loop(self) -> None:
        # バックグラウンドスレッドのエントリーポイント: このスレッドにイベントループを紐付け、
        # 接続所有タスクを開始してから、run_forever()でループを回し続ける。
        asyncio.set_event_loop(self._loop)
        self._owner_task = self._loop.create_task(
            self._connection_owner(*self._connection_args)
        )
        self._owner_task.add_done_callback(self._owner_completed)  # タスク終了時にコールバックを呼ぶよう登録
        self._loop.run_forever()  # loop.stop()が呼ばれるまでイベントループを回し続ける

    def _owner_completed(self, task: asyncio.Task[Any]) -> None:
        # _connection_ownerタスクが終了した際に呼ばれるコールバック
        try:
            task.result()  # タスク内で例外が起きていればここで再送出される
        except BaseException as exc:
            if not self._connection_ready.is_set():
                # まだ接続完了イベントがセットされていなければ、これは起動時の
                # 失敗なのでエラーとして記録し、待機中のコンストラクタに知らせる
                self._connection_error = exc
                self._connection_ready.set()
        finally:
            self._owner_stopped.set()  # 所有タスクが終了したことを通知
            self._loop.stop()  # イベントループ自体も止める

    def _run(self, coro: Any, timeout: Optional[float] = None) -> Any:
        # 呼び出し元スレッド(通常はメインスレッド)から、バックグラウンドの
        # イベントループ上でコルーチンを実行し、その結果を同期的に待って返す。
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)  # スレッドセーフにコルーチンをスケジュール
        try:
            return future.result(timeout=timeout)  # 結果が出るまで(タイムアウト付きで)待つ
        except concurrent.futures.TimeoutError:
            future.cancel()  # タイムアウトしたら対応するタスクのキャンセルを試みる
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
        owner_task = asyncio.current_task()  # 自分自身(このコルーチンのタスク)への参照を保持

        async def cancel_when_requested() -> None:
            # _cancel_requestedフラグが立つのを監視し、立ったら所有タスクをキャンセルする補助コルーチン
            while not self._cancel_requested.is_set():
                await asyncio.sleep(0.05)  # ポーリング間隔(0.05秒)
            if owner_task is not None:
                owner_task.cancel()

        cancel_monitor = asyncio.create_task(cancel_when_requested())  # キャンセル監視タスクを起動
        try:
            try:
                async with AsyncExitStack() as exit_stack:  # 複数の非同期コンテキストをまとめて後始末する
                    if stdio_command:
                        # stdioトランスポート: コマンド文字列をシェル的に分割し、サブプロセスとして起動
                        parts = shlex.split(stdio_command)
                        params = StdioServerParameters(command=parts[0], args=parts[1:], env=env)
                        read, write = await exit_stack.enter_async_context(stdio_client(params))
                    else:
                        # streamable HTTPトランスポート: 既に起動済みのサーバーURLに接続
                        assert http_url is not None
                        read, write, _ = await exit_stack.enter_async_context(
                            streamablehttp_client(http_url)
                        )

                    # 読み書きストリームからMCPクライアントセッションを構築し、初期化ハンドシェイクを行う
                    self.session = await exit_stack.enter_async_context(ClientSession(read, write))
                    await self.session.initialize()
                    result = await self.session.list_tools()  # サーバーが提供するツール一覧を取得
                    self.tools = list(result.tools)
                    self._connection_ready.set()  # 接続完了(成功)をコンストラクタ側に通知
                    while not self._close_requested.is_set():
                        # close()が要求されるまで、このコルーチン(=接続の所有者)を
                        # 生かし続けることでコンテキストを維持する
                        await asyncio.sleep(0.05)
            except BaseException as exc:
                if not self._connection_ready.is_set():
                    # 接続確立中に例外が起きた場合は、それを記録してコンストラクタ側の待機を解除する
                    self._connection_error = exc
                    self._connection_ready.set()
        finally:
            cancel_monitor.cancel()  # キャンセル監視タスクはもう不要なので止める
            try:
                await cancel_monitor
            except asyncio.CancelledError:
                pass  # キャンセルによる例外は正常なので無視する
            self.session = None  # セッションはもう有効ではないのでクリア
            if not self._connection_ready.is_set():
                # ここまでにまだイベントがセットされていない異常系(想定外の早期終了等)でも
                # 呼び出し元をブロックしたままにしないよう、必ずセットしておく
                self._connection_ready.set()

    def _stop_owner(self, graceful: bool) -> None:
        # 接続所有タスクとバックグラウンドのイベントループを停止させる。
        # graceful=Trueなら「閉じてよい」という穏やかな要求、Falseなら強制キャンセル。
        if graceful:
            self._close_requested.set()
        else:
            self._cancel_requested.set()

        wait_timeout = CLOSE_TIMEOUT_SECONDS if graceful else 1.0
        if not self._owner_stopped.wait(timeout=wait_timeout):
            # 穏やかな要求で時間内に終わらなければ、強制キャンセルに切り替える
            self._cancel_requested.set()
            if self._owner_task is not None and self._loop.is_running():
                self._loop.call_soon_threadsafe(self._owner_task.cancel)
            self._owner_stopped.wait(timeout=1.0)  # 強制キャンセル後の終了を短時間だけ待つ

        if self._thread.is_alive() and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)  # イベントループ自体を止める
        self._thread.join(timeout=5.0)  # バックグラウンドスレッドの終了を待つ
        if not self._thread.is_alive() and not self._loop.is_closed():
            self._loop.close()  # スレッドが終わっていればイベントループのリソースを解放する

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
            return "[Error] MCP session is not connected"  # まだ接続されていない/既に切断済み
        try:
            # イベントループ上でcall_toolコルーチンを実行し、タイムアウト付きで結果を待つ
            result = self._run(session.call_tool(name, arguments), timeout=CALL_TOOL_TIMEOUT_SECONDS)
        except concurrent.futures.TimeoutError:
            return f"[Error] tool '{name}' timed out after {CALL_TOOL_TIMEOUT_SECONDS}s"
        parts = []
        for item in result.content:
            # 応答内容の各パートからテキストを抽出する(テキストでなければ文字列表現にフォールバック)
            text = getattr(item, "text", None)
            parts.append(text if text is not None else str(item))
        text_result = "\n".join(parts)  # 複数パートは改行で連結
        if getattr(result, "isError", False):
            # MCPサーバー自身がエラー応答を返した場合、それとわかる形で返す
            return f"[Error] tool '{name}' failed: {text_result}"
        return text_result

    def build_namespace(self) -> Dict[str, Callable[..., str]]:
        """発見した各ツールにつき1つずつ、同期的なラッパー関数を用意し、
        サンドボックスのexec()名前空間にそのまま組み込めるようにする(仕様書 4.2節)。"""
        namespace: Dict[str, Callable[..., str]] = {}
        for tool in self.tools:
            namespace[tool.name] = self._make_wrapper(tool)  # ツールごとにラッパー関数を生成して登録
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
        name = tool.name  # ツール名(生成する関数の名前にもなる)
        # 入力スキーマのproperties(引数定義)のキー順を、位置引数→パラメータ名の対応付けに使う
        param_names = list((tool.inputSchema or {}).get("properties", {}).keys())

        def wrapper(*args: Any, **kwargs: Any) -> str:
            if len(args) > len(param_names):
                # 定義されているパラメータ数より多い位置引数が渡された場合はエラー文字列を返す
                return (
                    f"[Error] {name}() takes at most {len(param_names)} positional "
                    f"arguments but {len(args)} were given"
                )
            arguments = dict(zip(param_names, args))  # 位置引数をパラメータ名にマッピング
            duplicates = arguments.keys() & kwargs.keys()  # 位置引数とキーワード引数で重複した名前を検出
            if duplicates:
                return f"[Error] {name}() got multiple values for {sorted(duplicates)}"
            arguments.update(kwargs)  # キーワード引数をマージ
            return self.call_tool(name, arguments)  # 実際のMCPツール呼び出しに委譲

        wrapper.__name__ = name  # デバッグ表示等のためラッパーの関数名をツール名に合わせる
        return wrapper

    def manual_text(self) -> str:
        """接続中のサーバーが提供するツール群を、システムプロンプト用の
        ドキュメントとして整形して返す(仕様書 4.2節 - サンドボックスの
        マニュアルは、接続されたMCPサーバーのツールスキーマから動的に
        生成されなければならない)。"""
        if not self.tools:
            return "(no MCP tools are currently connected)"  # ツールが1つもなければその旨を返す
        lines = []
        for tool in self.tools:
            schema = tool.inputSchema or {}
            properties = schema.get("properties", {})
            required = set(schema.get("required", []))  # 必須パラメータの集合
            params = []
            for pname, pschema in properties.items():
                ptype = pschema.get("type", "any")  # パラメータの型(スキーマになければ"any")
                marker = "" if pname in required else "?"  # 任意パラメータには"?"を付けて表示
                params.append(f"{pname}{marker}: {ptype}")
            signature = ", ".join(params)  # 関数シグネチャ風の文字列に整形
            description = (tool.description or "").strip()  # ツールの説明文(なければ空文字列)
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
            return  # 既にクローズ済みなら何もしない(冪等性の確保)
        self._closed = True
        self._stop_owner(graceful=True)  # 穏やかな切断を試みる
