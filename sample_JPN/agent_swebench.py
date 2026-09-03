"""自律型SWE-benchエージェントのCLI(セクション4.4)。

    uv run python -m agent_swebench --task-file task.json --output solution.json \\
        --model-name "model/name" --provider-url "https://provider.api/v1"

5つのモデル/3つのプロバイダ/3つのタスクにわたり、実際のSWE-bench Dockerイメージに対して
エンドツーエンドで検証済み - 比較結果の詳細はBENCHMARK_REPORT.mdを参照(独立に検証された
パッチが1件通過したケースを含む)。
"""
from __future__ import annotations  # 型注釈を文字列として遅延評価する(将来のアノテーション構文をサポート)

import argparse  # コマンドライン引数のパース用
import json  # タスクファイルの読み込みや結果の書き出しに使うJSON処理
import signal  # SIGTERMハンドラを登録するために使用
import sys  # 実行ファイルパスや終了コード制御に使用
from datetime import datetime  # 結果に付与するタイムスタンプ生成用
from pathlib import Path  # ファイルパス操作用
from typing import Optional  # Optional型注釈のため

from docker_runner import SweBenchContainer  # SWE-bench用Dockerコンテナの起動・管理クラス
from llm.client import LLMClient  # LLMプロバイダへの問い合わせを行うクライアント
from models import SandboxConfig, SolutionOutput, SWEBenchTaskInput  # サンドボックス設定・出力結果・タスク入力のデータモデル
from orchestrator import Orchestrator, OrchestratorConfig, ShutdownRequested  # 思考→コード→観測ループの本体と設定、中断例外
from prompts import build_system_prompt  # ベンチマークごとのシステムプロンプトを組み立てる関数
from sandbox.executor import DEFAULT_AUTHORIZED_IMPORTS, Sandbox  # サンドボックス実行環境と許可インポート一覧
from sandbox.mcp_client import MCPToolProxy  # MCPツールサーバへの接続プロキシ

REPO_ROOT = Path(__file__).resolve().parent  # このファイルが置かれているリポジトリのルートパス
TOOLS_FILE = REPO_ROOT / "mcp_tools_swebench.py"  # SWE-bench用MCPツールサーバのスクリプトパス
SCRATCH_DIR = Path("/tmp/agent")  # エージェントが使う一時作業ディレクトリ

MAX_ITERATIONS = 30  # デフォルトの最大反復回数(Thought->Code->Observationループの上限)
MAX_INPUT_TOKENS = 300_000  # 累積入力トークン数の上限
MAX_OUTPUT_TOKENS = 10_000  # 累積出力トークン数の上限
TIMEOUT_SECONDS = 900  # エージェント全体の実行時間の上限(秒)


def build_task_prompt(task: SWEBenchTaskInput) -> str:
    # SWE-benchタスクの内容からLLMに渡すユーザープロンプト文字列を組み立てる関数
    hints = f"\n\nHints:\n{task.hints_text}" if task.hints_text else ""  # ヒント情報がある場合はプロンプトに追加
    return (
        f"Repository: {task.repo}\n"
        "Working directory: /testbed\n\n"
        f"Issue to fix:\n{task.problem_statement}{hints}\n\n"
        "Explore the repository, make the minimal change that fixes the issue, "
        "verify it with run_tests(), and then submit with final_answer(get_patch())."
    )  # リポジトリ情報・作業ディレクトリ・修正すべきissue・手順の指示をまとめたプロンプトを返す


def error_solution(task_id: str, message: str) -> SolutionOutput:
    # エラー発生時に返す空のSolutionOutputを組み立てるヘルパー関数
    return SolutionOutput(
        task_id=task_id,  # 対象タスクのID(instance_id)
        benchmark="swebench",  # ベンチマーク種別を固定でswebenchとする
        success=False,  # 失敗として記録
        solution="",  # 解答(パッチ)は空文字列
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
    # エントリーポイント: 引数解析、タスク読み込み、Dockerコンテナ起動、オーケストレータ実行、結果出力・後片付けまでを行う
    parser = argparse.ArgumentParser(description="Agent Smith - SWE-bench agent")  # 引数パーサを作成
    parser.add_argument("--task-file", required=True)  # 入力タスクファイルのパス(必須)
    parser.add_argument("--output", required=True)  # 結果出力先ファイルのパス(必須)
    parser.add_argument("--model-name", required=True)  # 使用するモデル名(必須)
    parser.add_argument("--provider-url", required=True)  # LLMプロバイダのURL(必須)
    parser.add_argument("--max-iterations", type=int, default=MAX_ITERATIONS)  # 最大反復回数(省略時はデフォルト値)
    args = parser.parse_args()  # 実際にコマンドライン引数を解析

    try:
        task_data = json.loads(Path(args.task_file).read_text())  # タスクファイルを読み込みJSONとしてパース
        task = SWEBenchTaskInput.model_validate(task_data)  # pydanticモデルとしてバリデーション・変換
    except Exception as exc:
        # タスクファイルの読み込み・検証に失敗した場合はエラー用の解答を書き出して異常終了する
        solution = error_solution("unknown", f"Failed to load task file: {type(exc).__name__}: {exc}")  # タスクID不明としてエラー結果を作成
        Path(args.output).write_text(solution.model_dump_json(indent=2))  # 結果をJSONとして出力ファイルに書き込む
        sys.exit(1)  # 異常終了コードでプロセスを終了

    container: Optional[SweBenchContainer] = None  # Dockerコンテナの参照(finally節でクリーンアップするため先に宣言)
    mcp_proxy = None  # MCPツールプロキシの参照(finally節でクローズするため先に宣言)
    sandbox: Optional[Sandbox] = None  # サンドボックスの参照(finally節でクローズするため先に宣言)
    orchestrator: Optional[Orchestrator] = None  # オーケストレータのインスタンス(SIGTERMハンドラから参照するため先に宣言)

    def handle_sigterm(signum: int, frame: object) -> None:
        # SIGTERM受信時のハンドラ: オーケストレータが未生成なら即座に例外を送出し、生成済みなら停止要求を伝える
        if orchestrator is None:
            raise ShutdownRequested("shutdown requested (e.g. SIGTERM)")  # まだループ開始前なのでここで直接中断させる
        orchestrator.request_stop()  # 実行中のオーケストレータに停止を要求する

    signal.signal(signal.SIGTERM, handle_sigterm)  # SIGTERMシグナルに上記ハンドラを登録

    try:
        container = SweBenchContainer(task.docker_image)  # タスクに紐づくDockerイメージからコンテナ管理オブジェクトを生成
        SCRATCH_DIR.mkdir(parents=True, exist_ok=True)  # 一時作業ディレクトリを作成(既に存在してもエラーにしない)
        container.start(eval_script=task.eval_script, tools_file=TOOLS_FILE)  # コンテナを起動し、評価スクリプトとMCPツールファイルを配置する
        mcp_proxy = MCPToolProxy(stdio_command=container.mcp_stdio_command())  # コンテナ内で動くMCPツールサーバにstdio経由で接続

        sandbox_config = SandboxConfig(
            authorized_imports=DEFAULT_AUTHORIZED_IMPORTS,  # サンドボックス内で許可するimportの一覧
            allowed_directories=["/testbed", str(SCRATCH_DIR)],  # サンドボックスからアクセスを許可するディレクトリ(コンテナ内のリポジトリと一時領域)
            max_execution_time_seconds=60,  # サンドボックス全体の実行時間上限(秒)
            max_memory_mb=512,  # サンドボックスに割り当てるメモリ上限(MB)
        )
        sandbox = Sandbox(sandbox_config, extra_namespace=mcp_proxy.build_namespace())  # サンドボックスを生成し、MCPツールを名前空間に追加する
        system_prompt = build_system_prompt("swebench", mcp_proxy.manual_text())  # SWE-bench用のシステムプロンプトをツールの説明文付きで生成

        llm_client = LLMClient.from_provider_url(args.model_name, args.provider_url)  # 指定されたモデル・プロバイダURLからLLMクライアントを生成
        orchestrator = Orchestrator(
            llm_client,  # 使用するLLMクライアント
            sandbox,  # 使用するサンドボックス
            system_prompt,  # システムプロンプト
            OrchestratorConfig(
                max_iterations=args.max_iterations,  # 最大反復回数(コマンドライン引数から)
                max_input_tokens=MAX_INPUT_TOKENS,  # 入力トークン数上限
                max_output_tokens=MAX_OUTPUT_TOKENS,  # 出力トークン数上限
                max_time_seconds=TIMEOUT_SECONDS - 30,  # ハードキル前の後片付け余裕分として30秒引いた値
                max_tokens_per_request=1500,  # 1リクエストあたりの最大出力トークン数
            ),
        )  # オーケストレータを生成しループ実行の準備をする

        task_prompt = build_task_prompt(task)  # タスク情報からユーザープロンプトを構築
        solution = orchestrator.run(task.instance_id, "swebench", task_prompt)  # Thought->Code->Observationループを実行し結果を取得

    except ShutdownRequested as exc:
        # SIGTERMがOrchestrator.run()自身がガードしている区間の外(例えばサンドボックス実行の途中)で
        # 割り込んできた場合でも、ここに正常にたどり着くことでfinally節のcontainer.cleanup()が
        # moulinetteのSIGTERM->SIGKILL猶予期間内に確実に実行されるようにする(先にSIGKILLされてしまうのを防ぐ)。
        solution = error_solution(task.instance_id, f"stopped: {exc}")  # 停止理由を含むエラー結果を生成
    except Exception as exc:
        # 想定外の例外が発生した場合、クラッシュとして記録する
        solution = error_solution(task.instance_id, f"Agent crashed: {type(exc).__name__}: {exc}")  # 例外の型とメッセージを含むエラー結果を生成
    finally:
        if sandbox is not None:
            sandbox.close()  # サンドボックスのリソースを確実に解放する
        if mcp_proxy is not None:
            mcp_proxy.close()  # MCPツールプロキシ(サブプロセス)を確実に終了させる
        if container is not None:
            container.cleanup()  # Dockerコンテナを確実に停止・削除する

    Path(args.output).write_text(solution.model_dump_json(indent=2))  # 最終的な解答結果をJSON形式で出力ファイルに書き込む


if __name__ == "__main__":
    main()  # スクリプトとして直接実行された場合にmain()を呼び出す
