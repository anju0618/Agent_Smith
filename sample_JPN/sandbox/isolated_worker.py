"""`sandbox.isolated_process` から起動されるワーカーのエントリーポイント。

このファイルは意図的に小さく作られており、プロジェクトのsandboxパッケージと
標準ライブラリのみを使う。ホスト側からcallableオブジェクトを直接受け取ることは
決してなく、MCPツールはJSON-RPC風のブリッジ関数として表現される
(名前だけがプロセス境界を越え、実際の呼び出しは親プロセスに委譲される)。
"""
from __future__ import annotations

import json  # 親プロセスとのプロトコルメッセージのシリアライズ用
import sys  # 標準入出力(プロトコル通信路)へのアクセス用
from typing import Any, Callable, Dict, cast

from models import SandboxConfig  # サンドボックス設定モデル
from sandbox.executor import FinalAnswer, Sandbox  # 実際のコード実行を行うSandbox本体とFinalAnswer例外


def _send(output: Any, message: Dict[str, Any]) -> None:
    # メッセージ(辞書)を1行のJSONにシリアライズして出力ストリームに書き込み、即座にflushする
    # (親プロセス側がreadlineで1メッセージ=1行として読み取れるようにするため)
    output.write(json.dumps(message, separators=(",", ":"), default=str) + "\n")
    output.flush()


def _read(input_stream: Any) -> Dict[str, Any]:
    line = input_stream.readline()  # 入力ストリームから1行読み取る
    if not line:
        # 空文字列が返るのはストリームがEOFになった場合(親プロセスが終了・パイプを閉じた等)
        raise EOFError("parent process closed the worker protocol")
    message = json.loads(line)  # 受信した行をJSONとしてパース
    if not isinstance(message, dict):
        # プロトコル上はオブジェクト(辞書)以外を想定していないため異常とみなす
        raise ValueError("parent sent a non-object message")
    return message


class _ToolBridge:
    # MCPツール名1つにつき1つ生成される、呼び出し可能なブリッジオブジェクト。
    # サンドボックス内のコードから通常の関数のように呼び出すと、実体は
    # 親プロセスへの「ツール呼び出し要求」メッセージ送信+応答待ちに変換される。
    def __init__(self, name: str, input_stream: Any, output: Any) -> None:
        self._name = name  # ブリッジするMCPツールの名前
        self._input_stream = input_stream  # 親プロセスからの応答を読み取るストリーム
        self._output = output  # 親プロセスへリクエストを送るストリーム

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        # サンドボックス内コードがこのツール名を関数として呼んだときに実行される
        _send(
            self._output,
            # 親プロセスに「このツールをこの引数で呼んでほしい」と伝えるメッセージ
            {"type": "tool_call", "name": self._name, "args": list(args), "kwargs": kwargs},
        )
        response = _read(self._input_stream)  # 親プロセスからのツール実行結果を待って読み取る
        if response.get("type") != "tool_result":
            # 想定外の種類のメッセージが返ってきた場合はプロトコル違反として例外化
            raise RuntimeError(f"parent sent unexpected MCP response: {response.get('type')!r}")
        if not response.get("ok", False):
            # ツール呼び出し自体が失敗した場合、その理由をRuntimeErrorとしてサンドボックス側へ伝播
            raise RuntimeError(str(response.get("result", "MCP tool call failed")))
        return response.get("result")  # 成功時はツールの実行結果を返す


def main() -> int:
    # ワーカープロセスのエントリーポイント。stdin/stdoutを使って親プロセスとJSON行プロトコルで通信する。
    protocol_input = sys.stdin  # 親からのメッセージを読む入力ストリーム
    protocol_output = sys.__stdout__  # 親へメッセージを送る出力ストリーム(元のstdoutを直接使用)
    try:
        init = _read(protocol_input)  # 最初に届くはずの初期化メッセージを読み取る
        if init.get("type") != "init":
            raise ValueError("worker did not receive an init message")  # 初期化メッセージでなければ異常
        config = SandboxConfig.model_validate(init["config"])  # 受け取った設定データをバリデーションしてモデル化
        tool_names = init.get("tool_names", [])  # 利用可能なMCPツール名のリスト(親から渡される)
        if not isinstance(tool_names, list) or not all(isinstance(name, str) for name in tool_names):
            # ツール名リストの形式が不正なら初期化失敗として扱う
            raise ValueError("worker received invalid MCP tool names")
        # 各ツール名に対して、親プロセスへの橋渡しをする_ToolBridgeインスタンスを名前空間として構築
        namespace: Dict[str, Callable[..., Any]] = {
            name: cast(Callable[..., Any], _ToolBridge(name, protocol_input, protocol_output))
            for name in tool_names
        }
        # OS隔離(unshare/bwrap)は既に確立済みという前提でisolated=Falseにし、
        # このプロセス内で直接Sandboxを構築する(二重に隔離処理をしない)
        sandbox = Sandbox(
            config,
            extra_namespace=namespace,
            apply_process_memory_limit=bool(init.get("apply_process_memory_limit", True)),
            isolated=False,
        )
        _send(protocol_output, {"type": "ready"})  # 初期化完了を親プロセスに通知
    except BaseException as exc:  # noqa: BLE001 - 親は起動時の診断情報を必要とするためあらゆる例外を捕捉
        # 初期化に失敗した場合はエラー内容を親に伝えてから異常終了する
        _send(protocol_output, {"type": "worker_error", "error": f"{type(exc).__name__}: {exc}"})
        return 1

    while True:
        try:
            message = _read(protocol_input)  # 親からの次のコマンドメッセージを待つ
        except (EOFError, ValueError, json.JSONDecodeError):
            return 0  # 通信が切れた・壊れたメッセージが来た場合は静かに終了(異常終了扱いにしない)
        message_type = message.get("type")
        if message_type == "close":
            return 0  # 明示的な終了要求を受けたら正常終了
        if message_type != "run":
            # "run"以外の未知のコマンドはエラーとして通知し、ループは継続する
            _send(protocol_output, {"type": "worker_error", "error": "unknown worker command"})
            continue

        try:
            output = sandbox.run(str(message.get("code", "")))  # 受け取ったコードをサンドボックス内で実行
        except FinalAnswer as exc:
            # final_answer()が呼ばれた場合、その回答値を親プロセスに伝える
            _send(protocol_output, {"type": "final_answer", "answer": exc.answer})
        except KeyboardInterrupt:
            # 割り込み(Ctrl+C相当)が発生したことを親に伝える
            _send(protocol_output, {"type": "keyboard_interrupt"})
        except SystemExit as exc:
            # sys.exit()等が呼ばれた場合、その終了コードを親に伝える
            _send(protocol_output, {"type": "system_exit", "code": exc.code})
        except BaseException as exc:  # noqa: BLE001 - 次のステップのためワーカーを生かし続ける
            # 上記以外の予期しない例外はエラーとして親に通知しつつ、ワーカー自体は終了させない
            _send(protocol_output, {"type": "worker_error", "error": f"{type(exc).__name__}: {exc}"})
        else:
            # 例外なく実行できた場合は、標準出力キャプチャ結果を親プロセスに返す
            _send(protocol_output, {"type": "result", "output": output})


if __name__ == "__main__":
    raise SystemExit(main())  # スクリプトとして実行された場合、mainの返り値をプロセス終了コードにする
