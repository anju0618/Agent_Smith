"""Host-side controller for the process-isolated sandbox worker.

The worker runs inside a network-disabled user namespace and a bubblewrap
filesystem namespace.  Only the names of MCP tools cross that boundary; tool
calls are sent back to the trusted parent process over a small JSON protocol.

# ============================================================================
# 【日本語解説】このファイルの立ち位置
# ============================================================================
# executor.py の Sandbox クラスが行うPythonレベルの制限（import制限、
# 危険builtinsの除去、ダンダー属性アクセス制限）は、あくまで「多層防御の
# 1枚」に過ぎません。executor.py 自身のdocstringにもある通り、Pythonの
# 内省機能は非常に強力で、名前ルックアップの差し替えだけでは防ぎきれない
# 脱出経路が理論上存在します。
#
# そこで本丸になるのが、このファイルが実装する**OSレベルの隔離**です。
# LLM生成コードは、このファイルが起動する「専用の子プロセス」の中で、
# 以下のように何重にも囲まれた状態で実行されます:
#
#   unshare --user --map-root-user --net --   ← 専用のuser namespace +
#                                                 network namespace（NIC無し）
#     bwrap --clearenv --die-with-parent ...  ← 最小限のファイルシステムしか
#                                                 見えない専用のmount namespace、
#                                                 非特権ユーザー(UID/GID 65534)
#       python isolated_worker.py             ← ここでようやくコードが動く
#
# つまり、たとえPythonレベルの防御(executor.py)を万が一突破されても、
# その先にあるのは「ネットワークに繋がらず」「ホストのファイルシステムが
# ほぼ見えず（allowed_directoriesで明示的に許可した場所だけ）」
# 「非特権ユーザーとして動いている」プロセスでしかない、という設計です。
#
# このファイル(IsolatedSandboxProcess)は**親プロセス側**（信頼された側）
# のコントローラです。対になる子プロセス側のエントリポイントは
# isolated_worker.py にあります。親子は標準入出力越しの改行区切りJSON
# メッセージで通信します（後述のプロトコル参照）。
# ============================================================================
"""
from __future__ import annotations

import json
import os
import queue
import selectors
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple, cast


STARTUP_TIMEOUT_SECONDS = 10.0
WORKER_TERMINATE_TIMEOUT_SECONDS = 0.5
WORKER_KILL_TIMEOUT_SECONDS = 1.0
# 【日本語解説】
# ワーカー起動時のタイムアウト(10秒)、SIGTERM後にワーカーの終了を待つ
# 時間(0.5秒)、SIGKILL後にさらに終了を待つ時間(1秒)。いずれも
# 「無期限に待ち続けてエージェント全体をハングさせない」ための
# 保険的な値。


class IsolatedSandboxProcess:
    """Own one persistent, OS-isolated worker for a :class:`Sandbox`."""
    # 【日本語解説】
    # 1つの Sandbox インスタンス（isolated=True）につき、1つの常駐
    # ワーカープロセスを所有・管理するクラス。「常駐」である点が重要 ──
    # 1タスクの実行中、Thought→Code→Observationのループが何度回っても、
    # 同じワーカープロセスを使い続ける。そのおかげでLLMが定義した変数が
    # ターンをまたいで保持される（ワーカー内のnamespace辞書が生き続ける
    # ため）。

    def __init__(
        self,
        config: Any,
        extra_namespace: Dict[str, Callable[..., Any]],
        apply_process_memory_limit: bool,
    ) -> None:
        self._config = config
        self._extra_namespace = extra_namespace
        # 【日本語解説】
        # extra_namespace（MCPツールのラッパー関数群）はここでは
        # "呼び出し可能なPython関数"のまま親プロセス側に保持される。
        # ワーカー側には「関数の名前」だけが渡され(_worker_configの
        # 呼び出し元、_send()のinitメッセージのtool_names参照)、
        # 実際の呼び出しは親プロセスがこの辞書を使って代行する
        # （_invoke_tool参照）。ワーカー自身は本物のMCPクライアントも
        # asyncioイベントループも一切知らない。
        self._process: Optional[subprocess.Popen[str]] = None
        self._selector: Optional[selectors.BaseSelector] = None
        self._closed = False
        self._start(config, apply_process_memory_limit)

    def _start(self, config: Any, apply_process_memory_limit: bool) -> None:
        # 【日本語解説】
        # ワーカープロセスを実際に起動し、初期化ハンドシェイクまで行う。
        command = self._build_command(config)
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,  # 行バッファリング。1行(1メッセージ)ごとに読み書きするプロトコルに合わせている
                start_new_session=True,  # 独自のプロセスグループを作る。あとでkillpgでグループごと終了できるように
            )
        except OSError as exc:
            raise RuntimeError(f"could not start isolated sandbox worker: {exc}") from exc

        self._process = process
        if process.stdout is None:
            raise RuntimeError("isolated sandbox worker has no stdout pipe")
        # 【日本語解説】
        # selectors.DefaultSelector を使うのは、readline()で無期限に
        # ブロックするのではなく、「タイムアウト付きで」ワーカーからの
        # メッセージ到着を待てるようにするため（_read_message参照）。
        self._selector = selectors.DefaultSelector()
        self._selector.register(process.stdout, selectors.EVENT_READ)

        # 【日本語解説】
        # 起動直後、最初に送る "init" メッセージ。SandboxConfigの内容
        # (_worker_config)、注入するMCPツールの"名前だけ"のリスト、
        # メモリ制限を適用するかどうかのフラグをワーカーに伝える。
        self._send(
            {
                "type": "init",
                "config": self._worker_config(config),
                "tool_names": list(self._extra_namespace),
                "apply_process_memory_limit": apply_process_memory_limit,
            }
        )
        try:
            message = self._read_message(STARTUP_TIMEOUT_SECONDS)
        except (EOFError, TimeoutError, ValueError) as exc:
            # 【日本語解説】
            # 10秒以内に応答が無い、あるいはプロセスが即死した場合は、
            # 中途半端に生き残ったプロセスを確実に後始末してから
            # 例外を送出する。ここで確実に片付けないと、失敗した
            # ワーカーがゾンビとして残り続けるおそれがある。
            self._terminate_process()
            raise RuntimeError(f"isolated sandbox worker did not initialize: {exc}") from exc
        if message.get("type") != "ready":
            self._terminate_process()
            detail = message.get("error", "unknown worker initialization error")
            raise RuntimeError(f"isolated sandbox worker failed to initialize: {detail}")

    def _build_command(self, config: Any) -> list[str]:
        # ======================================================================
        # 【日本語解説】ここが「隔離」の核心 ── unshare + bwrap コマンドの組み立て
        # ======================================================================
        if sys.platform != "linux":
            # 【日本語解説】
            # user namespace（unshare --user）はLinux固有の機能。
            # 他OSでは動かないので、その場でfail-closed（代替手段への
            # フォールバックはせず、明示的にエラーで止まる）にする。
            raise RuntimeError("the isolated sandbox requires Linux user namespaces")

        unshare = shutil.which("unshare")
        bubblewrap = shutil.which("bwrap")
        if unshare is None or bubblewrap is None:
            # 【日本語解説】
            # 必須コマンドが1つでも見つからなければ、隔離無しの実行に
            # フォールバックすることは絶対にせず、ここで即座に諦める。
            # 「セキュリティ機構が使えないなら、無許可の代替実行はしない
            # （fail-closed）」という設計方針がここに表れている。
            raise RuntimeError("the isolated sandbox requires both 'unshare' and 'bwrap'")

        python_path = Path(sys.executable).resolve()
        project_root = Path(__file__).resolve().parents[1]
        sandbox_source = project_root / "sandbox"
        models_source = project_root / "models.py"
        if not python_path.is_file() or not sandbox_source.is_dir() or not models_source.is_file():
            raise RuntimeError("isolated sandbox runtime files are missing")

        site_packages = self._site_packages()
        if not site_packages:
            raise RuntimeError("isolated sandbox could not find the project's site-packages")
        if Path("/usr") not in python_path.parents:
            # 【日本語解説】
            # Pythonインタプリタ自体が /usr 配下（システム標準の場所）に
            # 無いと、後で --ro-bind /usr を使ってワーカー内に持ち込む
            # 前提が崩れてしまうため、事前にチェックしている。
            raise RuntimeError("isolated sandbox requires a Python interpreter under /usr")

        command = [
            unshare,
            "--user",          # 専用のuser namespaceを作る（内部では自分がroot扱いになる）
            "--map-root-user", # ↑のroot権限を、実際には非特権のホストユーザーにマッピングする
            "--net",           # 専用のnetwork namespaceを作る。ネットワークインターフェースは
                                # 一切割り当てられない ＝ 事実上ネットワークアクセス不可
            "--",
            bubblewrap,
            "--clearenv",       # 環境変数を全部消してから始める（.envの値などを継承しない）
            "--die-with-parent",# 親プロセス(IsolatedSandboxProcess)が死んだらワーカーも道連れで終了
            "--unshare-user",
            "--uid",
            "65534",  # 【日本語解説】65534は伝統的に「nobody」に割り当てられる非特権UID
            "--gid",
            "65534",
        ]
        for system_path in (Path("/usr"), Path("/lib"), Path("/lib64")):
            if system_path.exists():
                # --ro-bind: 読み取り専用でバインドマウントする。ワーカーが
                # システムライブラリを"読む"ことはできても"書き換える"ことは
                # できない。
                command.extend(["--ro-bind", str(system_path), str(system_path)])

        command.extend(
            [
                "--dev",
                "/dev",
                "--proc",
                "/proc",
                "--tmpfs",
                "/tmp",  # 一時的な、ワーカー専用のtmpfs。ホストの/tmpとは別物
                "--dir",
                "/agent",
                "--dir",
                "/agent/sandbox",
                "--ro-bind",
                str(sandbox_source),
                "/agent/sandbox",  # sandbox/パッケージ自体を読み取り専用で持ち込む
                "--dir",
                "/agent/site-packages",
                "--ro-bind",
                str(site_packages[0]),
                "/agent/site-packages",  # 依存ライブラリ(pydantic等)を読み取り専用で持ち込む
                "--ro-bind",
                str(models_source),
                "/agent/models.py",  # models.py（契約）も個別に持ち込む
            ]
        )

        # The worker needs only the project package and its dependencies.  In
        # particular, the repository root (which may contain .env files) is not
        # mounted wholesale into the untrusted process.
        # ----------------------------------------------------------------------
        # 【日本語解説】ここが特に重要な設計判断
        # ----------------------------------------------------------------------
        # プロジェクトのリポジトリルートを"丸ごと"マウントするのではなく、
        # ワーカーが本当に必要とするファイル（sandbox/パッケージ、
        # models.py、site-packages）**だけ**を個別に選んでマウントして
        # いる。これは、リポジトリルートには .env のようなAPIキーを
        # 含みうる機微なファイルが置かれている可能性があるため。
        # 「動かすために必要な最小限だけを見せる」という最小権限の原則が
        # ファイルシステムマウントのレベルでも徹底されている。
        # ----------------------------------------------------------------------
        python_path_entries = ["/agent", "/agent/site-packages"]
        for index, path in enumerate(site_packages[1:], start=1):
            target = f"/agent/site-packages-{index}"
            command.extend(["--dir", target, "--ro-bind", str(path), target])
            python_path_entries.append(target)

        mounted_targets = {
            Path("/agent"),
            Path("/agent/sandbox"),
            Path("/agent/site-packages"),
            Path("/tmp"),
            Path("/dev"),
            Path("/proc"),
            Path("/usr"),
            Path("/lib"),
            Path("/lib64"),
        }
        for directory in config.allowed_directories:
            # 【日本語解説】
            # SandboxConfig.allowed_directories に列挙されたディレクトリ
            # （例: "/testbed", スクラッチディレクトリ）だけを、
            # 読み書き可能な形で(--bind、--ro-bindではない)個別に
            # マウントする。ここがexecutor.pyの_make_restricted_open()の
            # 「ファイルパスチェック」の、OSレベルでの裏付けになっている
            # ── executor.py側のチェックが万一すり抜けても、そもそも
            # OS的にallowed_directories以外のパスはワーカーの中に
            # "存在すらしない"ので読み書きしようがない。
            mount_target = self._resolve_allowed_directory(directory)
            self._validate_allowed_target(mount_target)
            self._add_directory_mounts(command, mount_target, mounted_targets)
            if mount_target.is_dir():
                command.extend(["--bind", str(mount_target), str(mount_target)])

        command.extend(
            [
                "--setenv",
                "PYTHONPATH",
                os.pathsep.join(python_path_entries),
                "--setenv",
                "PYTHONUNBUFFERED",
                "1",
                "--setenv",
                "PYTHONDONTWRITEBYTECODE",
                "1",
                "--setenv",
                "HOME",
                "/tmp",
                "--chdir",
                "/agent",
                str(python_path),
                "/agent/sandbox/isolated_worker.py",  # ← 最終的にワーカー内で実行されるエントリポイント
            ]
        )
        return command

    @staticmethod
    def _resolve_allowed_directory(directory: str) -> Path:
        # 【日本語解説】
        # 相対パスをカレントディレクトリ基準の絶対パスに変換し、
        # resolve()でシンボリックリンクや".."を正規化する。
        target = Path(directory).expanduser()
        if not target.is_absolute():
            target = Path.cwd() / target
        return target.resolve()

    def _worker_config(self, config: Any) -> Dict[str, Any]:
        # 【日本語解説】
        # SandboxConfig(Pydanticモデル)をJSONとしてワーカーに送るために
        # 辞書化する。allowed_directoriesだけは、正規化済みの絶対パスに
        # 差し替えてから送る（ワーカー側は正規化されたパスをそのまま
        # 信用してよい状態になる）。
        config_data = cast(Dict[str, Any], config.model_dump(mode="json"))
        config_data["allowed_directories"] = [
            str(self._resolve_allowed_directory(directory))
            for directory in config.allowed_directories
        ]
        return config_data

    @staticmethod
    def _site_packages() -> list[Path]:
        # 【日本語解説】
        # sys.path から "site-packages" または "dist-packages" という
        # 名前のディレクトリを探し出す。これがワーカーに --ro-bind される
        # 依存ライブラリの実体になる。
        paths: list[Path] = []
        for entry in sys.path:
            if not entry:
                continue
            path = Path(entry)
            if path.name not in {"site-packages", "dist-packages"} or not path.is_dir():
                continue
            resolved = path.resolve()
            if resolved not in paths:
                paths.append(resolved)
        return paths

    @staticmethod
    def _validate_allowed_target(target: Path) -> None:
        # ----------------------------------------------------------------------
        # 【日本語解説】allowed_directoriesの設定ミス・悪用を防ぐガード
        # ----------------------------------------------------------------------
        # SandboxConfig.allowed_directories は呼び出し元（agent_mbpp.py等の
        # アプリケーションコード）が指定する値だが、もし誤って
        # "/"（ルート全体）や "/usr" のような保護対象そのものを
        # allowed_directoriesに指定してしまったら、隔離の意味が
        # 丸ごと無くなってしまう。ここでその「自傷」を防いでいる:
        #   1. targetそのものが保護対象ディレクトリと完全一致するなら拒否
        #   2. targetが保護対象ディレクトリの"内側"にあるなら拒否
        #      （ただし / と /tmp は例外 ── /tmp配下は元々tmpfsとして
        #        隔離済みで、その中にスクラッチ用サブディレクトリを
        #        許可するのは正当なユースケースのため）
        # ----------------------------------------------------------------------
        protected = {
            Path("/"),
            Path("/agent"),
            Path("/dev"),
            Path("/lib"),
            Path("/lib64"),
            Path("/proc"),
            Path("/tmp"),
            Path("/usr"),
        }
        if target in protected:
            raise ValueError(f"allowed directory '{target}' would weaken the isolated root")
        protected_roots = protected - {Path("/"), Path("/tmp")}
        if any(root in target.parents for root in protected_roots):
            raise ValueError(f"allowed directory '{target}' is inside a protected root")

    @staticmethod
    def _add_directory_mounts(
        command: list[str], target: Path, mounted_targets: set[Path]
    ) -> None:
        # 【日本語解説】
        # bubblewrapでは、あるパスを --bind するには、その途中の
        # 親ディレクトリも --dir で明示的に作っておく必要がある場合が
        # ある。target（例: /testbed/sub/dir）のパス階層を根から順に
        # たどりながら、まだ作っていない中間ディレクトリを --dir で
        # 追加していく。mounted_targetsで重複追加を避けている。
        current = Path("/")
        for part in target.parts[1:]:
            current /= part
            if current in mounted_targets:
                continue
            command.extend(["--dir", str(current)])
            mounted_targets.add(current)

    # ==========================================================================
    # 【日本語解説】ここから下は、起動済みワーカーとの通信プロトコル実装
    # ==========================================================================
    # 親↔子は「改行区切りのJSON」で会話する、シンプルなプロトコル:
    #   親→子: {"type": "init", ...}         → 子: {"type": "ready"} / {"type": "worker_error", ...}
    #   親→子: {"type": "run", "code": "..."}
    #   子→親: {"type": "tool_call", "name": ..., "args": [...], "kwargs": {...}}
    #   親→子: {"type": "tool_result", "ok": bool, "result": ...}
    #   子→親: {"type": "result", "output": "..."} /
    #          {"type": "final_answer", ...} /
    #          {"type": "keyboard_interrupt"} /
    #          {"type": "system_exit", "code": ...} /
    #          {"type": "worker_error", ...}
    # ==========================================================================

    def _send(self, message: Dict[str, Any]) -> None:
        # 【日本語解説】
        # 辞書をJSON文字列化し、改行を1つ付けてワーカーのstdinに書き込む。
        # default=str は、万一JSON化できない値（例外オブジェクトなど）が
        # 混ざっていても、str()に変換して何とかシリアライズを成功させる
        # ための保険。
        process = self._process
        if process is None or process.stdin is None:
            raise EOFError("isolated sandbox worker stdin is closed")
        process.stdin.write(json.dumps(message, separators=(",", ":"), default=str) + "\n")
        process.stdin.flush()

    def _read_message(self, timeout: float) -> Dict[str, Any]:
        # 【日本語解説】
        # ワーカーからの1メッセージ(1行のJSON)を、タイムアウト付きで
        # 読み取る。selectors.select()でまず「読める状態になるまで」
        # 待ち、タイムアウトすればTimeoutErrorを送出する。これが無いと
        # readline()は無期限にブロックしてしまい、ハングしたワーカーに
        # 親プロセス全体が引きずられてしまう。
        if self._selector is None or self._process is None or self._process.stdout is None:
            raise EOFError("isolated sandbox worker stdout is closed")
        events = self._selector.select(max(timeout, 0.0))
        if not events:
            raise TimeoutError(f"no worker response within {timeout:.3f}s")
        line = self._process.stdout.readline()
        if not line:
            # 【日本語解説】
            # selectorはデータありと言ったのに空行 ＝ パイプの向こうで
            # プロセスが終了した(EOF)ことを意味する。
            raise EOFError("isolated sandbox worker exited")
        message = json.loads(line)
        if not isinstance(message, dict):
            raise ValueError("isolated sandbox worker sent a non-object message")
        return message

    def run(self, code: str) -> str:
        # 【日本語解説】
        # executor.py の Sandbox.run() から isolated=True の場合に
        # 呼ばれる実処理。1ターン分のコード文字列を送り、ワーカーからの
        # 応答を待つ。応答の種類によって分岐する（下のif/elif連鎖）。
        if self._closed:
            return "[IsolatedSandboxError] sandbox worker is closed"

        timeout = float(self._config.max_execution_time_seconds)
        if timeout <= 0:
            return f"[Timeout] Execution exceeded {timeout:g}s and was not started."
        try:
            self._send({"type": "run", "code": code})
        except (BrokenPipeError, EOFError, OSError) as exc:
            self._closed = True
            return f"[IsolatedSandboxError] could not send code to worker: {exc}"

        # 【日本語解説】
        # ここからが「親側のタイムアウト管理」。deadline（絶対時刻）を
        # 計算し、ループの各反復で「残り時間」を計算し直しながら
        # _read_messageに渡す。これにより、tool_callのやり取りが
        # 複数回挟まっても、合計の実行時間がmax_execution_time_secondsを
        # 超えないように制御できる（executor.py側のsignal.alarm()による
        # ワーカー自身のタイムアウトとは別の、独立した第二のタイムアウト
        # 機構）。
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._terminate_process()
                self._closed = True
                return f"[Timeout] Execution exceeded {timeout:g}s and was interrupted."
            try:
                message = self._read_message(remaining)
            except TimeoutError:
                self._terminate_process()
                self._closed = True
                return f"[Timeout] Execution exceeded {timeout:g}s and was interrupted."
            except (EOFError, ValueError, json.JSONDecodeError) as exc:
                self._closed = True
                return f"[IsolatedSandboxError] worker protocol failed: {exc}"

            message_type = message.get("type")
            if message_type == "tool_call":
                # ----------------------------------------------------------------
                # 【日本語解説】ここがMCPツール呼び出しの橋渡しの核心
                # ----------------------------------------------------------------
                # ワーカー内のコードが `search_code("foo")` のような
                # MCPツール関数を呼ぶと、ワーカー側(_ToolBridge、
                # isolated_worker.py参照)はそれを実行するのではなく
                # "tool_call"メッセージとして親に転送する。親はここで
                # 実際に本物の関数(self._extra_namespaceの中身、実体は
                # MCPToolProxyが生成したラッパー)を呼び出し、結果を
                # "tool_result"としてワーカーに送り返す。
                # ワーカーはこの往復の間、次のメッセージを待つだけで、
                # 実際のMCP通信(asyncio等)を一切意識しない。
                ok, result, timed_out = self._invoke_tool(
                    message.get("name"), message.get("args", []), message.get("kwargs", {}), remaining
                )
                if timed_out:
                    self._terminate_process()
                    self._closed = True
                    return f"[Timeout] Execution exceeded {timeout:g}s while calling an MCP tool."
                try:
                    self._send({"type": "tool_result", "ok": ok, "result": result})
                except (BrokenPipeError, EOFError, OSError) as exc:
                    self._closed = True
                    return f"[IsolatedSandboxError] could not return MCP result: {exc}"
                continue  # 応答を送ったら、次のメッセージ（結果 or 別のtool_call）を待ち続ける
            if message_type == "result":
                # 正常終了。ワーカーの標準出力キャプチャ結果をそのまま返す。
                return str(message.get("output", ""))
            if message_type == "final_answer":
                # 【日本語解説】
                # ワーカー内でLLMコードがfinal_answer()を呼んだ場合。
                # ここでも「握りつぶさない」原則通り、executor.pyの
                # FinalAnswer例外をこの親プロセス側で**再構築して
                # 送出**し、呼び出し元（Sandbox.run→Orchestrator）まで
                # 確実に伝播させる。
                from sandbox.executor import FinalAnswer

                raise FinalAnswer(message.get("answer"))
            if message_type == "keyboard_interrupt":
                raise KeyboardInterrupt()
            if message_type == "system_exit":
                raise SystemExit(message.get("code"))
            if message_type == "worker_error":
                return f"[IsolatedSandboxError] {message.get('error', 'unknown worker error')}"
            return f"[IsolatedSandboxError] unknown worker message: {message_type!r}"

    def _invoke_tool(
        self, name: Any, args: Any, kwargs: Any, timeout: float
    ) -> Tuple[bool, Any, bool]:
        # 【日本語解説】
        # 親プロセス側で、実際にMCPツール関数(self._extra_namespace[name])
        # を呼び出す処理。ここが**別スレッドで**実行されている点が
        # 重要 ── MCPツール呼び出し自体がハングしても（例: 相手の
        # MCPサーバーが応答を返さない）、このメインスレッドは
        # thread.join(timeout)でタイムアウトを検知でき、親プロセス
        # 全体が無期限に固まることを防げる。これは
        # sandbox/mcp_client.pyが自前で持つタイムアウトとは別の、
        # もう一段のセーフティネット。
        if not isinstance(name, str):
            return False, "invalid MCP tool name", False
        function = self._extra_namespace.get(name)
        if function is None:
            return False, f"unknown MCP tool '{name}'", False
        if not isinstance(args, list) or not isinstance(kwargs, dict):
            return False, "invalid MCP tool arguments", False

        result_queue: "queue.Queue[Tuple[bool, Any]]" = queue.Queue(maxsize=1)

        def invoke() -> None:
            try:
                result_queue.put((True, function(*args, **kwargs)))
            except BaseException as exc:  # noqa: BLE001 - return tool failures to the worker
                # 【日本語解説】
                # ツール呼び出し中の例外は、親プロセスをクラッシュさせず
                # 「失敗した」という結果としてキューに積む。これが
                # ワーカー側に伝われば、LLMへのObservationとして
                # エラーメッセージが返る。
                result_queue.put((False, f"{type(exc).__name__}: {exc}"))

        thread = threading.Thread(target=invoke, name="agent-smith-mcp-call", daemon=True)
        thread.start()
        thread.join(max(timeout, 0.0))
        if thread.is_alive():
            # 【日本語解説】
            # join()がタイムアウトしても、スレッド自体はdaemon=Trueなので
            # プロセス終了時に強制的に片付けられる（明示的にkillする
            # 手段がPythonのthreadingには無いため、daemonフラグに頼る
            # 設計）。
            return False, "MCP tool call timed out", True
        succeeded, result = result_queue.get()
        if not succeeded:
            return False, result, False
        try:
            json.dumps(result)
        except (TypeError, ValueError):
            # 【日本語解説】
            # ツールの戻り値がJSONにシリアライズできない型だった場合、
            # 諦めずにstr()化してから送る（プロトコルがJSON前提なので、
            # ここで失敗すると通信そのものが壊れてしまうため）。
            result = str(result)
        return True, result, False

    def _terminate_process(self) -> None:
        # ----------------------------------------------------------------------
        # 【日本語解説】ワーカーの強制終了 ── SIGTERM→（猶予）→SIGKILLの二段階
        # ----------------------------------------------------------------------
        process = self._process
        if process is None or process.poll() is not None:
            return
        try:
            # os.killpgでプロセス"グループ"全体に送る。start_new_session=True
            # で専用グループを作っておいたのはこのため ── ワーカーが
            # さらに子プロセスを起動していても、グループごと一掃できる。
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=WORKER_TERMINATE_TIMEOUT_SECONDS)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            # 【日本語解説】
            # 0.5秒待ってもSIGTERMで終了しなければ、問答無用のSIGKILLに
            # 切り替える。前回説明したSIGTERM/SIGKILLの関係が、ここでは
            # 「親プロセスが子ワーカーに対して」同じパターンを適用して
            # いる。
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=WORKER_KILL_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            pass

    def close(self) -> None:
        # 【日本語解説】
        # 正常な後片付け。ワーカーに"close"メッセージを送って自発的な
        # 終了を試み(0.5秒待つ)、うまくいかなければ_terminate_process()の
        # 強制終了にフォールバックする。
        if self._closed:
            self._close_pipes()
            return
        self._closed = True
        process = self._process
        if process is not None and process.poll() is None:
            try:
                self._send({"type": "close"})
                process.wait(timeout=WORKER_TERMINATE_TIMEOUT_SECONDS)
            except (BrokenPipeError, EOFError, OSError, subprocess.TimeoutExpired):
                self._terminate_process()
        self._close_pipes()

    def _close_pipes(self) -> None:
        # 【日本語解説】
        # selectorとstdin/stdoutのパイプを明示的にクローズし、
        # ファイルディスクリプタのリークを防ぐ。例外は握りつぶす
        # （後片付け処理自体が失敗してもエージェント全体を落とさない
        # ため）。
        if self._selector is not None:
            try:
                self._selector.close()
            except Exception:
                pass
            self._selector = None
        if self._process is not None:
            for stream in (self._process.stdin, self._process.stdout):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass

    def __del__(self) -> None:
        # 【日本語解説】
        # executor.py の Sandbox.__del__ と同様、close()し忘れへの保険。
        try:
            self.close()
        except Exception:
            pass
