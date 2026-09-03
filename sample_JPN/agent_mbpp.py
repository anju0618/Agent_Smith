"""自律型MBPPエージェントのCLI(セクション4.3.1)。

    uv run python -m agent_mbpp --task-file task.json --output solution.json \\
        --model-name "model/name" --provider-url "https://provider.api/v1"
"""
from __future__ import annotations  # 型注釈を文字列として遅延評価する(将来のアノテーション構文をサポート)

import argparse  # コマンドライン引数のパース用
import json  # タスクファイルの読み込みや結果の書き出しに使うJSON処理
import signal  # SIGTERMハンドラを登録するために使用
import sys  # 実行ファイルパスや終了コード制御に使用
from datetime import datetime  # 結果に付与するタイムスタンプ生成用
from pathlib import Path  # ファイルパス操作用
from typing import Optional  # Optional型注釈のため

from llm.client import LLMClient  # LLMプロバイダへの問い合わせを行うクライアント
from models import MBPPTaskInput, SandboxConfig, SolutionOutput  # タスク入力・サンドボックス設定・出力結果のデータモデル
from orchestrator import Orchestrator, OrchestratorConfig, ShutdownRequested  # 思考→コード→観測ループの本体と設定、中断例外
from prompts import build_system_prompt  # ベンチマークごとのシステムプロンプトを組み立てる関数
from sandbox.executor import DEFAULT_AUTHORIZED_IMPORTS, Sandbox  # サンドボックス実行環境と許可インポート一覧
from sandbox.mcp_client import MCPToolProxy  # MCPツールサーバへの接続プロキシ

REPO_ROOT = Path(__file__).resolve().parent  # このファイルが置かれているリポジトリのルートパス
MCP_TOOLS_SCRIPT = REPO_ROOT / "mcp_tools_mbpp.py"  # MBPP用MCPツールサーバのスクリプトパス
SCRATCH_DIR = Path("/tmp/agent")  # エージェントが使う一時作業ディレクトリ

MAX_ITERATIONS = 10  # デフォルトの最大反復回数(Thought->Code->Observationループの上限)
MAX_INPUT_TOKENS = 6_000  # 累積入力トークン数の上限
MAX_OUTPUT_TOKENS = 1_500  # 累積出力トークン数の上限
TIMEOUT_SECONDS = 120  # エージェント全体の実行時間の上限(秒)


def build_task_prompt(task: MBPPTaskInput) -> str:
    # MBPPタスクの内容からLLMに渡すユーザープロンプト文字列を組み立てる関数
    tests_preview = "\n".join(task.test_list) if task.test_list else "(no public tests provided)"  # 公開テスト一覧を改行区切りで表示用に整形(なければその旨を表示)
    imports_note = (
        f"\n\nrun_tests() automatically makes these imports available to the assertions "
        f"above, so you don't need to add them yourself just for the tests to run: "
        f"{'; '.join(task.test_imports)}"
        if task.test_imports
        else ""
    )  # テストに必要な追加importがある場合、その旨をLLMに伝える補足文を作成
    return (
        f"Task: {task.task_definition}\n\n"
        f"Function signature: {task.function_definition}\n\n"
        f"Public tests your solution must pass (there may also be hidden tests):\n{tests_preview}"
        f"{imports_note}\n\n"
        "Use run_tests(code, test_list) to check your solution against these assertions "
        "before submitting. Submit with final_answer(your_function_code) once confident."
    )  # タスク定義・関数シグネチャ・公開テスト・提出方法の指示をまとめたプロンプトを返す


def error_solution(task_id: str, message: str) -> SolutionOutput:
    # エラー発生時に返す空のSolutionOutputを組み立てるヘルパー関数
    return SolutionOutput(
        task_id=task_id,  # 対象タスクのID
        benchmark="mbpp",  # ベンチマーク種別を固定でmbppとする
        success=False,  # 失敗として記録
        solution="",  # 解答は空文字列
        iterations=0,  # 反復回数は0
        total_requests=0,  # LLMへのリクエスト回数は0
        total_input_tokens=0,  # 入力トークン数は0
        total_output_tokens=0,  # 出力トークン数は0
        total_time_seconds=0.0,  # 実行時間は0秒
        steps=[],  # ステップの記録は空リスト
        system_prompt="",  # システムプロンプトは空
        error=message,  # エラーメッセージを格納
        timestamp=datetime.now().isoformat(),  # 現在時刻をISO形式で記録
    )


def main() -> None:
    # エントリーポイント: 引数解析、タスク読み込み、オーケストレータ実行、結果出力までを行う
    parser = argparse.ArgumentParser(description="Agent Smith - MBPP agent")  # 引数パーサを作成
    parser.add_argument("--task-file", required=True)  # 入力タスクファイルのパス(必須)
    parser.add_argument("--output", required=True)  # 結果出力先ファイルのパス(必須)
    parser.add_argument("--model-name", required=True)  # 使用するモデル名(必須)
    parser.add_argument("--provider-url", required=True)  # LLMプロバイダのURL(必須)
    parser.add_argument("--max-iterations", type=int, default=MAX_ITERATIONS)  # 最大反復回数(省略時はデフォルト値)
    args = parser.parse_args()  # 実際にコマンドライン引数を解析

    try:
        task_data = json.loads(Path(args.task_file).read_text())  # タスクファイルを読み込みJSONとしてパース
        task = MBPPTaskInput.model_validate(task_data)  # pydanticモデルとしてバリデーション・変換
    except Exception as exc:
        # タスクファイルの読み込み・検証に失敗した場合はエラー用の解答を書き出して異常終了する
        solution = error_solution("unknown", f"Failed to load task file: {type(exc).__name__}: {exc}")  # タスクID不明としてエラー結果を作成
        Path(args.output).write_text(solution.model_dump_json(indent=2))  # 結果をJSONとして出力ファイルに書き込む
        sys.exit(1)  # 異常終了コードでプロセスを終了

    orchestrator: Optional[Orchestrator] = None  # オーケストレータのインスタンス(SIGTERMハンドラから参照するため先に宣言)

    def handle_sigterm(signum: int, frame: object) -> None:
        # SIGTERM受信時のハンドラ: オーケストレータが未生成なら即座に例外を送出し、生成済みなら停止要求を伝える
        if orchestrator is None:
            raise ShutdownRequested("shutdown requested (e.g. SIGTERM)")  # まだループ開始前なのでここで直接中断させる
        orchestrator.request_stop()  # 実行中のオーケストレータに停止を要求する

    signal.signal(signal.SIGTERM, handle_sigterm)  # SIGTERMシグナルに上記ハンドラを登録

    mcp_proxy = None  # MCPツールプロキシの参照(finally節でクローズするため先に宣言)
    sandbox: Optional[Sandbox] = None  # サンドボックスの参照(finally節でクローズするため先に宣言)
    try:
        SCRATCH_DIR.mkdir(parents=True, exist_ok=True)  # 一時作業ディレクトリを作成(既に存在してもエラーにしない)
        # MBPPタスクの中には、候補解答自体には不要だがテストのassertion側で
        # 必要となるimport(例: math.isclose)があるため、それをmcp_tools_mbpp.pyに
        # 渡してrun_tests()側で自動的に先頭に追加させる(LLMが自力で気づくことに
        # 依存しないようにする、詳細はmcp_tools_mbpp.py参照)。
        tool_env = {"AGENT_SMITH_TEST_IMPORTS": json.dumps(task.test_imports)}  # テストに必要な追加importを環境変数として渡す準備
        mcp_proxy = MCPToolProxy(stdio_command=f"{sys.executable} {MCP_TOOLS_SCRIPT}", env=tool_env)  # MCPツールサーバをサブプロセスとして起動しstdio経由で接続

        sandbox_config = SandboxConfig(
            authorized_imports=DEFAULT_AUTHORIZED_IMPORTS,  # サンドボックス内で許可するimportの一覧
            allowed_directories=[str(SCRATCH_DIR)],  # サンドボックスからアクセスを許可するディレクトリ
            # mcp_tools_mbpp.py内部のrun_tests()サブプロセスタイムアウト(10秒)よりも
            # 十分大きくしておく必要がある。そうしないと、正常だが遅いテスト実行に対して
            # 外側のサンドボックスのアラームが先に発火してしまう
            # (実際のプロバイダに対するライブスモークテストで判明した既知の問題、README.md参照)。
            max_execution_time_seconds=20,  # サンドボックス全体の実行時間上限(秒)
            max_memory_mb=256,  # サンドボックスに割り当てるメモリ上限(MB)
        )
        sandbox = Sandbox(sandbox_config, extra_namespace=mcp_proxy.build_namespace())  # サンドボックスを生成し、MCPツールを名前空間に追加する
        system_prompt = build_system_prompt("mbpp", mcp_proxy.manual_text())  # MBPP用のシステムプロンプトをツールの説明文付きで生成

        llm_client = LLMClient.from_provider_url(args.model_name, args.provider_url)  # 指定されたモデル・プロバイダURLからLLMクライアントを生成
        orchestrator = Orchestrator(
            llm_client,  # 使用するLLMクライアント
            sandbox,  # 使用するサンドボックス
            system_prompt,  # システムプロンプト
            OrchestratorConfig(
                max_iterations=args.max_iterations,  # 最大反復回数(コマンドライン引数から)
                max_input_tokens=MAX_INPUT_TOKENS,  # 入力トークン数上限
                max_output_tokens=MAX_OUTPUT_TOKENS,  # 出力トークン数上限
                max_time_seconds=TIMEOUT_SECONDS - 10,  # 全体タイムアウトから後片付け余裕分の10秒を引いた値
                max_tokens_per_request=400,  # 1リクエストあたりの最大出力トークン数
            ),
        )  # オーケストレータを生成しループ実行の準備をする

        task_prompt = build_task_prompt(task)  # タスク情報からユーザープロンプトを構築
        solution = orchestrator.run(str(task.task_id), "mbpp", task_prompt)  # Thought->Code->Observationループを実行し結果を取得

    except ShutdownRequested as exc:
        # SIGTERMなどによる中断要求を受けた場合、その旨を記録したエラー解答を作る
        solution = error_solution(str(task.task_id), f"stopped: {exc}")  # 停止理由を含むエラー結果を生成
    except Exception as exc:
        # 想定外の例外が発生した場合、クラッシュとして記録する
        solution = error_solution(str(task.task_id), f"Agent crashed: {type(exc).__name__}: {exc}")  # 例外の型とメッセージを含むエラー結果を生成
    finally:
        if sandbox is not None:
            sandbox.close()  # サンドボックスのリソースを確実に解放する
        if mcp_proxy is not None:
            mcp_proxy.close()  # MCPツールプロキシ(サブプロセス)を確実に終了させる

    Path(args.output).write_text(solution.model_dump_json(indent=2))  # 最終的な解答結果をJSON形式で出力ファイルに書き込む


if __name__ == "__main__":
    main()  # スクリプトとして直接実行された場合にmain()を呼び出す
