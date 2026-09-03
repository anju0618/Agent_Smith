"""プロセス隔離されたサンドボックスワーカーを制御する、ホスト側のコントローラー。

ワーカーはネットワークを無効化したuser名前空間と、bubblewrapによる
ファイルシステム名前空間の内側で動作する。その境界を越えるのはMCPツールの
「名前」だけであり、実際のツール呼び出しは小さなJSONプロトコル経由で
信頼された親プロセスに送り返される。
"""
from __future__ import annotations

import json  # ワーカーとのプロトコルメッセージのシリアライズ/デシリアライズ用
import os  # プロセスグループへのシグナル送信(killpg)等に使用
import queue  # MCPツール呼び出しをタイムアウト付きで待つための結果受け渡し用キュー
import selectors  # ワーカーからの出力を待つ際のI/O多重化(タイムアウト付き読み取り)用
import shutil  # unshare/bwrapコマンドの実行パス探索用
import signal  # ワーカープロセスへのシグナル送信用
import subprocess  # ワーカープロセスの起動・管理用
import sys  # プラットフォーム判定・Pythonインタプリタパス取得用
import threading  # MCPツール呼び出しをタイムアウト付きで実行するための別スレッド起動用
import time  # デッドライン計算用
from pathlib import Path  # ファイルパス操作用
from typing import Any, Callable, Dict, Optional, Tuple, cast


STARTUP_TIMEOUT_SECONDS = 10.0  # ワーカーの初期化(ready応答)を待つ最大秒数
WORKER_TERMINATE_TIMEOUT_SECONDS = 0.5  # SIGTERM送信後、正常終了を待つ最大秒数
WORKER_KILL_TIMEOUT_SECONDS = 1.0  # SIGKILL送信後、終了を待つ最大秒数


class IsolatedSandboxProcess:
    """:class:`Sandbox`のために、永続的なOS隔離ワーカーを1つ所有・管理するクラス。"""

    def __init__(
        self,
        config: Any,
        extra_namespace: Dict[str, Callable[..., Any]],
        apply_process_memory_limit: bool,
    ) -> None:
        self._config = config  # サンドボックス設定を保存
        self._extra_namespace = extra_namespace  # MCPツール名→実関数の対応(ツール呼び出し時に参照)
        self._process: Optional[subprocess.Popen[str]] = None  # ワーカーのサブプロセスハンドル
        self._selector: Optional[selectors.BaseSelector] = None  # ワーカー出力を監視するセレクタ
        self._closed = False  # 既にクローズ済みかどうかのフラグ
        self._start(config, apply_process_memory_limit)  # ワーカープロセスを起動して初期化する

    def _start(self, config: Any, apply_process_memory_limit: bool) -> None:
        command = self._build_command(config)  # unshare/bwrapを含む起動コマンドを組み立てる
        try:
            # ワーカープロセスを起動。stdin/stdoutをパイプにしてJSON行プロトコルで通信する
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,  # 標準エラーは捨てる(プロトコル通信を汚さないため)
                text=True,  # バイト列ではなく文字列として読み書きする
                bufsize=1,  # 行バッファリング(1行ごとにflushされやすくする)
                start_new_session=True,  # 新しいセッション(プロセスグループ)を作り、後でまとめてkillできるようにする
            )
        except OSError as exc:
            raise RuntimeError(f"could not start isolated sandbox worker: {exc}") from exc

        self._process = process
        if process.stdout is None:
            raise RuntimeError("isolated sandbox worker has no stdout pipe")
        self._selector = selectors.DefaultSelector()  # タイムアウト付き読み取りのためのセレクタを用意
        self._selector.register(process.stdout, selectors.EVENT_READ)  # ワーカーのstdoutを監視対象に登録

        # 初期化メッセージを送信: 設定・利用可能なMCPツール名・メモリ上限適用有無を渡す
        self._send(
            {
                "type": "init",
                "config": self._worker_config(config),
                "tool_names": list(self._extra_namespace),
                "apply_process_memory_limit": apply_process_memory_limit,
            }
        )
        try:
            message = self._read_message(STARTUP_TIMEOUT_SECONDS)  # ワーカーからの応答を一定時間待つ
        except (EOFError, TimeoutError, ValueError) as exc:
            # 応答が来ない・不正な場合はワーカーを強制終了して起動失敗として例外化
            self._terminate_process()
            raise RuntimeError(f"isolated sandbox worker did not initialize: {exc}") from exc
        if message.get("type") != "ready":
            # "ready"以外の応答(エラー通知等)が返ってきた場合も起動失敗として扱う
            self._terminate_process()
            detail = message.get("error", "unknown worker initialization error")
            raise RuntimeError(f"isolated sandbox worker failed to initialize: {detail}")

    def _build_command(self, config: Any) -> list[str]:
        # ワーカーを起動するための unshare + bubblewrap コマンドライン全体を組み立てる。
        # ここで積み重ねる各種フラグがOS隔離の実体そのものであるため、1つ1つの意味を説明する。
        if sys.platform != "linux":
            # unshare/bwrapはLinux固有のユーザー名前空間機構に依存するため他OSでは使えない
            raise RuntimeError("the isolated sandbox requires Linux user namespaces")

        unshare = shutil.which("unshare")  # unshareコマンドの実行パスを探す
        bubblewrap = shutil.which("bwrap")  # bubblewrap(bwrap)コマンドの実行パスを探す
        if unshare is None or bubblewrap is None:
            # どちらかが見つからなければ隔離サンドボックス自体を動かせないので起動を諦める
            raise RuntimeError("the isolated sandbox requires both 'unshare' and 'bwrap'")

        python_path = Path(sys.executable).resolve()  # 現在使っているPythonインタプリタの実パス
        project_root = Path(__file__).resolve().parents[1]  # プロジェクトルート(このファイルの2階層上)
        sandbox_source = project_root / "sandbox"  # sandboxパッケージのソースディレクトリ
        models_source = project_root / "models.py"  # models.py(SandboxConfig等)のパス
        if not python_path.is_file() or not sandbox_source.is_dir() or not models_source.is_file():
            # ワーカー実行に必要なファイル群が見つからなければ起動できない
            raise RuntimeError("isolated sandbox runtime files are missing")

        site_packages = self._site_packages()  # 依存パッケージが入っているsite-packagesディレクトリ一覧
        if not site_packages:
            raise RuntimeError("isolated sandbox could not find the project's site-packages")
        if Path("/usr") not in python_path.parents:
            # Pythonインタプリタが/usr配下にないと、後述の/usrの読み取り専用マウントだけでは
            # インタプリタ自体をワーカー側に見せられないため、この構成を前提としてチェックする
            raise RuntimeError("isolated sandbox requires a Python interpreter under /usr")

        command = [
            unshare,
            "--user",  # 新しいuser名前空間を作成(権限を隔離する土台)
            "--map-root-user",  # 名前空間内ではrootに見えるようにマッピング(bwrap内部でuid切り替えするため)
            "--net",  # 新しいnetwork名前空間を作成(外部ネットワークアクセスを遮断)
            "--",
            bubblewrap,
            "--clearenv",  # 親プロセスの環境変数を全て消去してから起動(情報漏洩防止)
            "--die-with-parent",  # 親プロセスが死んだらワーカーも道連れに終了させる(プロセスの取り残し防止)
            "--unshare-user",  # bwrap内でさらにuser名前空間を分離
            "--uid",
            "65534",  # 実質的な"nobody"ユーザーIDとして動作させる(非特権)
            "--gid",
            "65534",  # 同様に"nogroup"グループIDとして動作させる
        ]
        # /usr, /lib, /lib64はPython本体や共有ライブラリの実行に必要なため読み取り専用でバインド
        for system_path in (Path("/usr"), Path("/lib"), Path("/lib64")):
            if system_path.exists():
                command.extend(["--ro-bind", str(system_path), str(system_path)])

        command.extend(
            [
                "--dev",
                "/dev",  # 最小限のデバイスファイル群を用意
                "--proc",
                "/proc",  # /procを新規にマウント(隔離されたPID名前空間用)
                "--tmpfs",
                "/tmp",  # 一時ファイルシステムとして空の/tmpを用意(ホストの/tmpは見えない)
                "--dir",
                "/agent",  # ワーカー用の作業ディレクトリを作成
                "--dir",
                "/agent/sandbox",
                "--ro-bind",
                str(sandbox_source),
                "/agent/sandbox",  # sandboxパッケージのソースを読み取り専用でマウント
                "--dir",
                "/agent/site-packages",
                "--ro-bind",
                str(site_packages[0]),
                "/agent/site-packages",  # 依存パッケージ(1つ目)を読み取り専用でマウント
                "--ro-bind",
                str(models_source),
                "/agent/models.py",  # models.pyを読み取り専用でマウント
            ]
        )

        # ワーカーが必要とするのはプロジェクトのパッケージとその依存関係のみである。
        # 特に、.envファイル等を含みうるリポジトリのルートディレクトリ全体は、この
        # 信頼できないプロセスに丸ごとマウントされることはない。
        python_path_entries = ["/agent", "/agent/site-packages"]  # ワーカー側のPYTHONPATHに積む項目
        for index, path in enumerate(site_packages[1:], start=1):
            # 2つ目以降のsite-packagesディレクトリがあれば、それぞれ別の番号付きパスにマウント
            target = f"/agent/site-packages-{index}"
            command.extend(["--dir", target, "--ro-bind", str(path), target])
            python_path_entries.append(target)

        # 既にマウント済みのパスを記録しておき、後続の許可ディレクトリマウント時の重複を避ける
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
            # 設定で許可された各ディレクトリを解決し、隔離ルートを弱めないか検証してからマウント
            mount_target = self._resolve_allowed_directory(directory)
            self._validate_allowed_target(mount_target)
            self._add_directory_mounts(command, mount_target, mounted_targets)
            if mount_target.is_dir():
                # 許可ディレクトリは読み取り専用ではなく書き込み可能(--bind)でマウントする
                # (サンドボックス内コードがそこにファイルを書けるようにするため)
                command.extend(["--bind", str(mount_target), str(mount_target)])

        command.extend(
            [
                "--setenv",
                "PYTHONPATH",
                os.pathsep.join(python_path_entries),  # ワーカー内Pythonがimportできるパスを設定
                "--setenv",
                "PYTHONUNBUFFERED",
                "1",  # 標準出力をバッファリングせず即座にflushさせる(プロトコル通信の遅延防止)
                "--setenv",
                "PYTHONDONTWRITEBYTECODE",
                "1",  # .pycファイルを書き出させない(読み取り専用マウントなのでどのみち書けないが明示)
                "--setenv",
                "HOME",
                "/tmp",  # HOME環境変数を隔離された/tmpに向ける
                "--chdir",
                "/agent",  # ワーカープロセスの作業ディレクトリを/agentに設定
                str(python_path),  # 実行するインタプリタ本体
                "/agent/sandbox/isolated_worker.py",  # ワーカーのエントリーポイントスクリプト
            ]
        )
        return command

    @staticmethod
    def _resolve_allowed_directory(directory: str) -> Path:
        # 設定文字列で渡された許可ディレクトリを絶対パス・シンボリックリンク解決済みのPathに変換する
        target = Path(directory).expanduser()  # "~"等をホームディレクトリに展開
        if not target.is_absolute():
            target = Path.cwd() / target  # 相対パスなら現在の作業ディレクトリ基準の絶対パスにする
        return target.resolve()  # シンボリックリンクを解決した実パスを返す

    def _worker_config(self, config: Any) -> Dict[str, Any]:
        # ワーカーに送る設定データを構築する。allowed_directoriesは相対パスの
        # ままだとワーカー側(chdirが異なる)で解釈が変わってしまうため、
        # ここで絶対パスに解決してから渡す。
        config_data = cast(Dict[str, Any], config.model_dump(mode="json"))
        config_data["allowed_directories"] = [
            str(self._resolve_allowed_directory(directory))
            for directory in config.allowed_directories
        ]
        return config_data

    @staticmethod
    def _site_packages() -> list[Path]:
        # sys.pathの中から"site-packages"または"dist-packages"という名前のディレクトリを収集する
        # (依存パッケージがインストールされている場所をワーカーにも見せるため)
        paths: list[Path] = []
        for entry in sys.path:
            if not entry:
                continue  # 空文字列のエントリ(カレントディレクトリを表す)は無視
            path = Path(entry)
            if path.name not in {"site-packages", "dist-packages"} or not path.is_dir():
                continue  # 該当しない名前、または実在しないディレクトリはスキップ
            resolved = path.resolve()
            if resolved not in paths:
                paths.append(resolved)  # 重複を避けつつ収集
        return paths

    @staticmethod
    def _validate_allowed_target(target: Path) -> None:
        # ユーザー設定の許可ディレクトリが、隔離環境の根幹となる保護対象パスと
        # 一致・またはその内部にないことを検証する。これを怠ると、たとえば
        # allowed_directoriesに"/usr"を指定することで読み取り専用のはずの
        # マウントを書き込み可能な--bindで上書きしてしまうなど、隔離を
        # 弱める設定が可能になってしまう。
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
            # 保護対象パスそのものを許可ディレクトリに指定するのは禁止
            raise ValueError(f"allowed directory '{target}' would weaken the isolated root")
        # "/"と"/tmp"はサブディレクトリを許可ディレクトリにしてもよい(/tmp内は元々書き込み可能なtmpfsのため)
        protected_roots = protected - {Path("/"), Path("/tmp")}
        if any(root in target.parents for root in protected_roots):
            # 上記以外の保護対象ルート(/agent, /usr等)の内部を指定するのも禁止
            raise ValueError(f"allowed directory '{target}' is inside a protected root")

    @staticmethod
    def _add_directory_mounts(
        command: list[str], target: Path, mounted_targets: set[Path]
    ) -> None:
        # bwrapでは、あるパスをマウントする前に、その親ディレクトリ階層が
        # 存在している必要がある。ここではtargetまでのパス階層を1段ずつたどり、
        # まだ作られていない各段に"--dir"(空ディレクトリ作成)を積んでいく。
        current = Path("/")
        for part in target.parts[1:]:
            current /= part
            if current in mounted_targets:
                continue  # 既にマウント/作成済みの階層はスキップ
            command.extend(["--dir", str(current)])
            mounted_targets.add(current)  # 作成済みとして記録

    def _send(self, message: Dict[str, Any]) -> None:
        # ワーカーのstdinに1行のJSONメッセージを書き込む(プロトコル送信の共通処理)
        process = self._process
        if process is None or process.stdin is None:
            raise EOFError("isolated sandbox worker stdin is closed")
        process.stdin.write(json.dumps(message, separators=(",", ":"), default=str) + "\n")
        process.stdin.flush()  # 即座に相手に届くようflush(バッファ滞留によるデッドロック防止)

    def _read_message(self, timeout: float) -> Dict[str, Any]:
        # ワーカーのstdoutから1行分のJSONメッセージを、タイムアウト付きで読み取る
        if self._selector is None or self._process is None or self._process.stdout is None:
            raise EOFError("isolated sandbox worker stdout is closed")
        events = self._selector.select(max(timeout, 0.0))  # timeout秒以内に読み取り可能になるか待つ
        if not events:
            raise TimeoutError(f"no worker response within {timeout:.3f}s")  # 時間内に応答がなければタイムアウト
        line = self._process.stdout.readline()  # 実際に1行読み取る
        if not line:
            raise EOFError("isolated sandbox worker exited")  # 空文字列はワーカー側の終了(EOF)を意味する
        message = json.loads(line)  # JSONとしてパース
        if not isinstance(message, dict):
            raise ValueError("isolated sandbox worker sent a non-object message")  # オブジェクト以外は不正
        return message

    def run(self, code: str) -> str:
        # コードスニペットをワーカーに送って実行させ、結果(または各種エラー文字列)を返す。
        # ワーカーとのやり取り中にMCPツール呼び出しが挟まる場合は、それをこのループ内で処理する。
        if self._closed:
            return "[IsolatedSandboxError] sandbox worker is closed"  # 既に閉じたワーカーには実行できない

        timeout = float(self._config.max_execution_time_seconds)  # このコード実行に許される最大秒数
        if timeout <= 0:
            return f"[Timeout] Execution exceeded {timeout:g}s and was not started."  # 0以下なら即タイムアウト扱い
        try:
            self._send({"type": "run", "code": code})  # ワーカーに実行コマンドを送信
        except (BrokenPipeError, EOFError, OSError) as exc:
            # ワーカーが既に死んでいる等でパイプが壊れている場合
            self._closed = True
            return f"[IsolatedSandboxError] could not send code to worker: {exc}"

        deadline = time.monotonic() + timeout  # このコード実行全体のデッドライン(絶対時刻)
        while True:
            remaining = deadline - time.monotonic()  # 残り許容時間を計算
            if remaining <= 0:
                # 残り時間が尽きたらワーカーを強制終了してタイムアウトを返す
                self._terminate_process()
                self._closed = True
                return f"[Timeout] Execution exceeded {timeout:g}s and was interrupted."
            try:
                message = self._read_message(remaining)  # 残り時間内にワーカーからの次のメッセージを待つ
            except TimeoutError:
                # 時間内に応答が来なければタイムアウトとしてワーカーを強制終了
                self._terminate_process()
                self._closed = True
                return f"[Timeout] Execution exceeded {timeout:g}s and was interrupted."
            except (EOFError, ValueError, json.JSONDecodeError) as exc:
                # 通信プロトコル自体が壊れた場合はこのSandboxを閉じたものとして扱う
                self._closed = True
                return f"[IsolatedSandboxError] worker protocol failed: {exc}"

            message_type = message.get("type")
            if message_type == "tool_call":
                # ワーカーがMCPツール呼び出しを要求してきた場合、親プロセス側で実際に呼び出す
                ok, result, timed_out = self._invoke_tool(
                    message.get("name"), message.get("args", []), message.get("kwargs", {}), remaining
                )
                if timed_out:
                    # ツール呼び出し自体がタイムアウトした場合もワーカーごと終了させる
                    self._terminate_process()
                    self._closed = True
                    return f"[Timeout] Execution exceeded {timeout:g}s while calling an MCP tool."
                try:
                    self._send({"type": "tool_result", "ok": ok, "result": result})  # 結果をワーカーに返送
                except (BrokenPipeError, EOFError, OSError) as exc:
                    self._closed = True
                    return f"[IsolatedSandboxError] could not return MCP result: {exc}"
                continue  # ツール呼び出しへの応答を返したら、次のワーカーメッセージ待ちに戻る
            if message_type == "result":
                return str(message.get("output", ""))  # 通常の実行結果(標準出力)が返ってきた
            if message_type == "final_answer":
                # ワーカー側でfinal_answer()が呼ばれた場合、この親プロセス側でも
                # 同じFinalAnswer例外を再構築して送出し、呼び出し元に伝播させる
                from sandbox.executor import FinalAnswer

                raise FinalAnswer(message.get("answer"))
            if message_type == "keyboard_interrupt":
                raise KeyboardInterrupt()  # ワーカー側での割り込みをこちらでも再現する
            if message_type == "system_exit":
                raise SystemExit(message.get("code"))  # ワーカー側でのsys.exit()をこちらでも再現する
            if message_type == "worker_error":
                return f"[IsolatedSandboxError] {message.get('error', 'unknown worker error')}"  # ワーカー内部エラー
            return f"[IsolatedSandboxError] unknown worker message: {message_type!r}"  # 想定外の種類のメッセージ

    def _invoke_tool(
        self, name: Any, args: Any, kwargs: Any, timeout: float
    ) -> Tuple[bool, Any, bool]:
        # ワーカーから要求されたMCPツール呼び出しを、親プロセス(このプロセス)側で
        # 実際に実行する。戻り値は (成功したか, 結果またはエラーメッセージ, タイムアウトしたか)。
        if not isinstance(name, str):
            return False, "invalid MCP tool name", False  # ツール名が文字列でなければ不正
        function = self._extra_namespace.get(name)  # 登録済みのMCPツール関数を名前で検索
        if function is None:
            return False, f"unknown MCP tool '{name}'", False  # 未登録のツール名は拒否
        if not isinstance(args, list) or not isinstance(kwargs, dict):
            return False, "invalid MCP tool arguments", False  # 引数の型が想定外なら不正

        result_queue: "queue.Queue[Tuple[bool, Any]]" = queue.Queue(maxsize=1)  # 別スレッドから結果を受け取るキュー

        def invoke() -> None:
            # 別スレッドで実際にツール関数を呼び出す(メインスレッドをブロックせずタイムアウト監視するため)
            try:
                result_queue.put((True, function(*args, **kwargs)))
            except BaseException as exc:  # noqa: BLE001 - ツール失敗もワーカーに返すため広く捕捉
                result_queue.put((False, f"{type(exc).__name__}: {exc}"))

        thread = threading.Thread(target=invoke, name="agent-smith-mcp-call", daemon=True)
        thread.start()  # ツール呼び出しを別スレッドで開始
        thread.join(max(timeout, 0.0))  # 残り時間内に完了するのを待つ
        if thread.is_alive():
            # 時間内に終わらなければタイムアウト扱い(スレッド自体はdaemonなので放置される)
            return False, "MCP tool call timed out", True
        succeeded, result = result_queue.get()  # スレッドが置いた結果を取り出す
        if not succeeded:
            return False, result, False  # ツール実行が例外で失敗した場合
        try:
            json.dumps(result)  # 結果がそのままJSONシリアライズ可能か試す(ワーカーに送り返せるか確認)
        except (TypeError, ValueError):
            result = str(result)  # シリアライズできない値は文字列化してフォールバック
        return True, result, False

    def _terminate_process(self) -> None:
        # ワーカーのプロセスグループ全体を段階的に終了させる(まずSIGTERM、それでも
        # 終わらなければSIGKILL)。start_new_sessionで新しいプロセスグループを
        # 作っているため、killpgでunshare/bwrap配下の子孫プロセスもまとめて倒せる。
        process = self._process
        if process is None or process.poll() is not None:
            return  # プロセスが存在しない、または既に終了していれば何もしない
        try:
            os.killpg(process.pid, signal.SIGTERM)  # まずは穏やかな終了シグナルを送る
        except ProcessLookupError:
            return  # 既にプロセスが消えていた場合
        try:
            process.wait(timeout=WORKER_TERMINATE_TIMEOUT_SECONDS)  # 短時間だけ正常終了を待つ
            return
        except subprocess.TimeoutExpired:
            pass  # 時間内に終わらなければ次のSIGKILLに進む
        try:
            os.killpg(process.pid, signal.SIGKILL)  # 強制終了シグナルを送る
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=WORKER_KILL_TIMEOUT_SECONDS)  # 強制終了の完了を待つ
        except subprocess.TimeoutExpired:
            pass  # ここでタイムアウトしてもこれ以上できることはない(ベストエフォート)

    def close(self) -> None:
        # ワーカーを穏やかに閉じる(可能であれば"close"メッセージを送って自発的終了を促す)。
        if self._closed:
            self._close_pipes()  # 既にクローズ済みでもパイプの後始末だけは念のため行う
            return
        self._closed = True
        process = self._process
        if process is not None and process.poll() is None:
            try:
                self._send({"type": "close"})  # ワーカーに「終了してよい」と伝える
                process.wait(timeout=WORKER_TERMINATE_TIMEOUT_SECONDS)  # 自発的な終了を少し待つ
            except (BrokenPipeError, EOFError, OSError, subprocess.TimeoutExpired):
                # 通信できない、または時間内に終わらなければ強制終了に切り替える
                self._terminate_process()
        self._close_pipes()

    def _close_pipes(self) -> None:
        # セレクタとパイプ(stdin/stdout)をクローズしてリソースリークを防ぐ
        if self._selector is not None:
            try:
                self._selector.close()
            except Exception:
                pass  # クローズ時のエラーは無視してよい(ベストエフォート)
            self._selector = None
        if self._process is not None:
            for stream in (self._process.stdin, self._process.stdout):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass  # 既に閉じている等のエラーは無視

    def __del__(self) -> None:
        # ガベージコレクション時の後始末(ベストエフォート)。__del__内の例外は無視する。
        try:
            self.close()
        except Exception:
            pass
