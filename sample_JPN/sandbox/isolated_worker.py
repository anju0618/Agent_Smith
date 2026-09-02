"""Worker entry point for :mod:`sandbox.isolated_process`.

This file is intentionally small and uses only the project sandbox plus the
standard library.  It never receives callable objects from the host; MCP
tools are represented by JSON-RPC-style bridge functions.

# ============================================================================
# 【日本語解説】このファイルの立ち位置
# ============================================================================
# isolated_process.py（親プロセス側）が unshare + bwrap で起動する、
# **子プロセス側**のエントリポイントがこのファイルです。
# `python /agent/sandbox/isolated_worker.py` として、隔離された
# namespace（ネットワーク無し・非特権ユーザー・限定されたファイルシステム
# ビュー）の中で実行されます。
#
# 設計上のポイントは「このファイルは意図的に小さく保たれている」こと。
# importしているのはこのプロジェクト自身のsandboxパッケージと
# 標準ライブラリだけで、外部依存が少ないほど攻撃対象面（コード自体の
# バグの入り込む余地）も小さくなります。
#
# もう1つの重要な設計判断は「ホスト（親プロセス）からcallableな
# オブジェクトを直接受け取ることは絶対にない」という点です。MCPツール
# （search_code, run_tests など）は、親プロセス側では本物のPython関数
# ですが、ワーカー側にはその**名前の文字列だけ**が渡され、実体は
# 下の _ToolBridge という「呼ばれたら親にJSON経由で転送するだけの
# ダミー関数」に置き換えられます。つまりワーカー自身は、本物の
# MCPClientSessionもasyncioイベントループも、そもそも存在すら知り
# ません。
# ============================================================================
"""
from __future__ import annotations

import json
import sys
from typing import Any, Callable, Dict, cast

from models import SandboxConfig
from sandbox.executor import FinalAnswer, Sandbox


def _send(output: Any, message: Dict[str, Any]) -> None:
    # 【日本語解説】
    # isolated_process.py の _send() と対になる、ワーカー側の送信関数。
    # 同じ「改行区切りJSON」プロトコルで親に書き込む。
    output.write(json.dumps(message, separators=(",", ":"), default=str) + "\n")
    output.flush()


def _read(input_stream: Any) -> Dict[str, Any]:
    # 【日本語解説】
    # 親からの1メッセージを読む。空行（EOF）が来たら、親プロセスが
    # パイプを閉じた＝終了要求とみなしてEOFErrorを送出する。
    line = input_stream.readline()
    if not line:
        raise EOFError("parent process closed the worker protocol")
    message = json.loads(line)
    if not isinstance(message, dict):
        raise ValueError("parent sent a non-object message")
    return message


class _ToolBridge:
    """MCPツール呼び出しを親プロセスへ転送するだけの、"見せかけの関数"。

    【日本語解説】
    Sandboxのnamespaceに `search_code` のような名前でこのインスタンスが
    注入されると、LLM生成コードから見れば「search_code(...)というただの
    関数」にしか見えません。しかし実際に呼ばれると、中身は一切の
    ツールロジックを持たず、ただ「name・args・kwargsをJSONにして親へ送り、
    親からの結果を待って返すだけ」の薄いプロキシです。この仕組みのおかげで、
    ワーカー側は「どんなMCPツールが繋がっているか」を一切知らなくても
    動作できます（tool_namesという名前のリストさえ受け取れば十分）。
    """
    def __init__(self, name: str, input_stream: Any, output: Any) -> None:
        self._name = name
        self._input_stream = input_stream
        self._output = output

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        # 【日本語解説】
        # 1. "tool_call"メッセージとして、関数名と受け取った引数
        #    （位置引数・キーワード引数の両方）をそのまま親に送る。
        _send(
            self._output,
            {"type": "tool_call", "name": self._name, "args": list(args), "kwargs": kwargs},
        )
        # 2. 親からの応答を待つ。ここでブロックしている間、Sandboxの
        #    exec()自体は「関数呼び出しの返りを待っている」状態になる
        #    ── これがisolated_process.py側の説明にあった「MCPツール
        #    呼び出しの間もタイムアウト管理が効いている」仕組みと対応する。
        response = _read(self._input_stream)
        if response.get("type") != "tool_result":
            raise RuntimeError(f"parent sent unexpected MCP response: {response.get('type')!r}")
        if not response.get("ok", False):
            # 【日本語解説】
            # 親側でツール呼び出しが失敗した場合（例外・タイムアウトなど）、
            # ここで通常のPython例外(RuntimeError)として再送出する。
            # LLM生成コードから見れば「普通の関数呼び出しがエラーを
            # 投げた」だけに見え、executor.pyのSandbox.run()の
            # 汎用except Exception節が拾って"[Error] ..."という
            # Observationに変換してくれる。
            raise RuntimeError(str(response.get("result", "MCP tool call failed")))
        return response.get("result")


def main() -> int:
    # 【日本語解説】
    # このワーカープロセスのメインループ。stdin/stdoutをそのまま
    # プロトコル通信のパイプとして使う（print()デバッグ出力などが
    # 混ざるとプロトコルが壊れるため、Sandbox.run()内部で
    # contextlib.redirect_stdoutされたLLMコードのstdoutは、この
    # protocol_outputとは別のio.StringIOに向いている点に注意）。
    protocol_input = sys.stdin
    protocol_output = sys.__stdout__
    try:
        # ------------------------------------------------------------------
        # 【日本語解説】起動シーケンス（initメッセージの処理）
        # ------------------------------------------------------------------
        init = _read(protocol_input)
        if init.get("type") != "init":
            raise ValueError("worker did not receive an init message")
        # SandboxConfigをJSONから復元(Pydanticのバリデーション付き)。
        config = SandboxConfig.model_validate(init["config"])
        tool_names = init.get("tool_names", [])
        if not isinstance(tool_names, list) or not all(isinstance(name, str) for name in tool_names):
            raise ValueError("worker received invalid MCP tool names")
        # 【日本語解説】
        # ツール名のリストから、それぞれに対応する_ToolBridgeインスタンス
        # を作り、Sandboxのextra_namespaceとして渡す準備をする。
        namespace: Dict[str, Callable[..., Any]] = {
            name: cast(Callable[..., Any], _ToolBridge(name, protocol_input, protocol_output))
            for name in tool_names
        }
        # 【日本語解説】
        # isolated=False で Sandbox を作る ── ここが「OS隔離が既に
        # 確立された後、実際にコードを実行するSandboxクラス自身の
        # 内部モード」の呼び出し元そのもの。もうこの時点で
        # unshare/bwrapの中にいるので、これ以上プロセスを分ける必要は
        # なく、このプロセス自身がexecutor.pyのSandbox.run()ロジックを
        # 直接実行する。
        sandbox = Sandbox(
            config,
            extra_namespace=namespace,
            apply_process_memory_limit=bool(init.get("apply_process_memory_limit", True)),
            isolated=False,
        )
        # 準備完了を親に通知。
        _send(protocol_output, {"type": "ready"})
    except BaseException as exc:  # noqa: BLE001 - parent needs startup diagnostics
        # 【日本語解説】
        # 起動処理中に何が起きても（設定が不正、Sandbox初期化失敗など）、
        # プロセスをそのままクラッシュさせるのではなく、"worker_error"
        # メッセージとして親に理由を伝えてから終了する。親側の
        # _start()（isolated_process.py）がこれを受け取ってRuntimeError
        # に変換し、原因が分かる形でエラーを返せるようにするため。
        _send(protocol_output, {"type": "worker_error", "error": f"{type(exc).__name__}: {exc}"})
        return 1

    # ------------------------------------------------------------------
    # 【日本語解説】メインループ ── 1タスクの全ターンにわたって動き続ける
    # ------------------------------------------------------------------
    # このwhileループが「ワーカーが常駐する」ことの実装そのもの。
    # 1回のrunメッセージ処理が終わっても、ここでまた次のメッセージを
    # 待つので、sandbox.namespace（LLMが定義した変数群）はプロセスの
    # メモリ上に生き続ける。
    while True:
        try:
            message = _read(protocol_input)
        except (EOFError, ValueError, json.JSONDecodeError):
            # 親がパイプを閉じた、あるいは壊れたメッセージが来た場合は、
            # クラッシュではなく正常終了(0)としてループを抜ける。
            return 0
        message_type = message.get("type")
        if message_type == "close":
            # 親からの明示的な終了要求（isolated_process.pyのclose()参照）。
            return 0
        if message_type != "run":
            _send(protocol_output, {"type": "worker_error", "error": "unknown worker command"})
            continue

        # 【日本語解説】
        # ここがLLM生成コードを実際に実行する箇所。sandbox.run()の
        # 戻り値・例外の種類に応じて、対応するメッセージタイプで
        # 親に結果を伝え返す ── executor.pyのSandbox.run()の
        # 「返り値ポリシー」（FinalAnswer/KeyboardInterrupt/SystemExitは
        # 例外のまま、それ以外は文字列で返す）が、ここでプロトコルの
        # メッセージタイプに1対1で変換されている。
        try:
            output = sandbox.run(str(message.get("code", "")))
        except FinalAnswer as exc:
            _send(protocol_output, {"type": "final_answer", "answer": exc.answer})
        except KeyboardInterrupt:
            _send(protocol_output, {"type": "keyboard_interrupt"})
        except SystemExit as exc:
            _send(protocol_output, {"type": "system_exit", "code": exc.code})
        except BaseException as exc:  # noqa: BLE001 - keep the worker alive for the next step
            # 【日本語解説】
            # 想定外の例外が起きても、ワーカープロセス自体はクラッシュ
            # させず"worker_error"として伝え、whileループを継続する
            # （＝ワーカーは生き続け、次のターンのrunメッセージも
            # 引き続き受け付けられる）。1ターンの異常でタスク全体の
            # 状態（変数の永続性など）を失わせないための配慮。
            _send(protocol_output, {"type": "worker_error", "error": f"{type(exc).__name__}: {exc}"})
        else:
            # 正常終了時のみ"result"メッセージで出力を返す。
            _send(protocol_output, {"type": "result", "output": output})


if __name__ == "__main__":
    raise SystemExit(main())
