"""Autonomous SWE-bench agent CLI (Section 4.4).

    uv run python -m agent_swebench --task-file task.json --output solution.json \\
        --model-name "model/name" --provider-url "https://provider.api/v1"

Exercised end to end against real SWE-bench Docker images across 5
models/3 providers/3 tasks - see BENCHMARK_REPORT.md for the full comparison,
including one independently-verified passing patch.
"""
# agent_mbpp.py の SWE-bench 版。骨格(タスク読み込み→SIGTERMハンドラ登録→
# サンドボックス構築→Orchestrator実行→finally後始末→solution.json書き出し)は
# agent_mbpp.py と完全に同じだが、以下の3点が異なる:
#   1. 実リポジトリを操作するため Docker コンテナ(SweBenchContainer)を
#      起動・後始末する必要がある
#   2. SandboxConfig の数値(allowed_directories/タイムアウト/メモリ)が
#      大きい - 実リポジトリの調査・修正には時間もリソースも余計にかかるため
#   3. 予算(イテレーション数・トークン数・時間)がMBPPより桁違いに大きい
from __future__ import annotations

import argparse
import json
import signal
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from docker_runner import SweBenchContainer
from llm.client import LLMClient
from models import SandboxConfig, SolutionOutput, SWEBenchTaskInput
from orchestrator import Orchestrator, OrchestratorConfig, ShutdownRequested
from prompts import build_system_prompt
from sandbox.executor import DEFAULT_AUTHORIZED_IMPORTS, Sandbox
from sandbox.mcp_client import MCPToolProxy

REPO_ROOT = Path(__file__).resolve().parent
# MCPツールサーバーのスクリプト自体は docker_runner.py が
# SweBenchContainer.start() の中でコンテナへ書き込む(TOOLS_FILE として渡す)。
TOOLS_FILE = REPO_ROOT / "mcp_tools_swebench.py"
SCRATCH_DIR = Path("/tmp/agent")

# SWE-bench用の予算。MBPP(agent_mbpp.py)と比べて桁違いに大きい:
# 30イテレーション、入力30万トークン、出力1万トークン、900秒(15分)。
# 実リポジトリの調査(ファイル読み込み・検索・テスト実行)にはそれだけ
# 手数とコンテキストが必要になる、という実測に基づいた設定。
MAX_ITERATIONS = 30
MAX_INPUT_TOKENS = 300_000
MAX_OUTPUT_TOKENS = 10_000
TIMEOUT_SECONDS = 900


def build_task_prompt(task: SWEBenchTaskInput) -> str:
    # SWEBenchTaskInput(models.py)を、LLMへの最初のユーザーメッセージに変換する。
    # hints_text が空文字列でなければ「ヒント」セクションを追記する
    # (SWE-benchデータセット側にissue解決の手がかりが含まれることがある)。
    hints = f"\n\nHints:\n{task.hints_text}" if task.hints_text else ""
    return (
        f"Repository: {task.repo}\n"
        # /testbed は docker_runner.py / mcp_tools_swebench.py が固定で
        # 使っているコンテナ内のマウントパス(TESTBED_PATH_IN_CONTAINER)。
        "Working directory: /testbed\n\n"
        f"Issue to fix:\n{task.problem_statement}{hints}\n\n"
        # 「最小限の変更で」「run_tests()で検証してから」「get_patch()の
        # 戻り値をそのままfinal_answerに渡せ」という3つの指示を1文で
        # 明示している - これがprompts.py の _SWEBENCH_FINAL_ANSWER と
        # 重複しているように見えるが、こちらはタスク固有のユーザー
        # メッセージ、あちらは全タスク共通のシステムプロンプトという
        # 別レイヤーの指示。
        "Explore the repository, make the minimal change that fixes the issue, "
        "verify it with run_tests(), and then submit with final_answer(get_patch())."
    )


def error_solution(task_id: str, message: str) -> SolutionOutput:
    # agent_mbpp.py の同名関数と同じ役割。benchmark="swebench" である点だけが違う。
    return SolutionOutput(
        task_id=task_id,
        benchmark="swebench",
        success=False,
        solution="",
        iterations=0,
        total_requests=0,
        total_input_tokens=0,
        total_output_tokens=0,
        total_time_seconds=0.0,
        steps=[],
        system_prompt="",
        error=message,
        timestamp=datetime.now().isoformat(),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Agent Smith - SWE-bench agent")
    parser.add_argument("--task-file", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--model-name", required=True)
    parser.add_argument("--provider-url", required=True)
    parser.add_argument("--max-iterations", type=int, default=MAX_ITERATIONS)
    args = parser.parse_args()

    try:
        # SWEBenchTaskInput でバリデーション。ここで失敗する場合、
        # まだDockerコンテナも何も起動していないので後始末は不要。
        task_data = json.loads(Path(args.task_file).read_text())
        task = SWEBenchTaskInput.model_validate(task_data)
    except Exception as exc:
        solution = error_solution("unknown", f"Failed to load task file: {type(exc).__name__}: {exc}")
        Path(args.output).write_text(solution.model_dump_json(indent=2))
        sys.exit(1)

    # agent_mbpp.py には無かった container 変数。Dockerコンテナの生存期間を
    # このtry/finallyブロックで管理する。
    container: Optional[SweBenchContainer] = None
    mcp_proxy = None
    sandbox: Optional[Sandbox] = None
    orchestrator: Optional[Orchestrator] = None

    def handle_sigterm(signum: int, frame: object) -> None:
        # agent_mbpp.py と全く同じ構造。Orchestratorがまだ無ければ自分で
        # ShutdownRequestedを投げ、あれば request_stop() に委譲する。
        if orchestrator is None:
            raise ShutdownRequested("shutdown requested (e.g. SIGTERM)")
        orchestrator.request_stop()

    signal.signal(signal.SIGTERM, handle_sigterm)

    try:
        # task.docker_image (例: "swebench/sweb.eval.x86_64.sympy_..._latest")
        # をもとにコンテナのライフサイクル管理オブジェクトを作る。この時点では
        # まだ何も起動していない(__init__はdocker.from_env()でクライアントを
        # 作るだけ)。
        container = SweBenchContainer(task.docker_image)
        SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
        # ここで実際に: (1) イメージをpull、(2) 長寿命コンテナを起動、
        # (3) eval.shとmcp_tools_swebench.pyの中身をコンテナへ書き込み、
        # (4) MCPサーバーの依存関係(mcp/pydantic)をコンテナ内にブートストラップ
        # ……という一連の処理が走る(docker_runner.py参照)。ネットワークの
        # 無いイメージだと(4)が失敗し、RuntimeErrorとしてここまで伝播してくる
        # ("グレースフルなエージェントエラー"として下のexcept節で処理される)。
        container.start(eval_script=task.eval_script, tools_file=TOOLS_FILE)
        # agent_mbpp.pyとの決定的な違い: stdio_commandに直接
        # "python mcp_tools_swebench.py"を渡すのではなく、
        # container.mcp_stdio_command()が返す
        # "docker exec -i -e TESTBED_PATH=... <container_id> python3 <path>"
        # というコマンドを渡す。つまりMCPサーバーは**コンテナの中で**動く一方、
        # このPythonプロセス(サンドボックス本体)自体はホスト側に残ったまま
        # ── Section 4.4のアプローチ(b)そのもの。
        mcp_proxy = MCPToolProxy(stdio_command=container.mcp_stdio_command())

        sandbox_config = SandboxConfig(
            authorized_imports=DEFAULT_AUTHORIZED_IMPORTS,
            # agent_mbpp.pyの[SCRATCH_DIRのみ]と違い、/testbed(コンテナ内に
            # マウントされた実リポジトリのパス)も許可している。ただしこれは
            # 「サンドボックス(Pythonインタプリタ)側のファイルアクセス許可」の
            # 話であり、実際のファイル操作はMCPツール(read_file/edit_fileなど)
            # 経由でコンテナ内で行われる。
            allowed_directories=["/testbed", str(SCRATCH_DIR)],
            # MBPP版の20秒/256MBと比べて、60秒/512MBとかなり緩め。
            # 実リポジトリのテストスイートはMBPPの単純なassert文よりずっと
            # 重く、時間もメモリも必要になるため。
            max_execution_time_seconds=60,
            max_memory_mb=512,
        )
        sandbox = Sandbox(sandbox_config, extra_namespace=mcp_proxy.build_namespace())
        system_prompt = build_system_prompt("swebench", mcp_proxy.manual_text())

        llm_client = LLMClient.from_provider_url(args.model_name, args.provider_url)
        orchestrator = Orchestrator(
            llm_client,
            sandbox,
            system_prompt,
            OrchestratorConfig(
                max_iterations=args.max_iterations,
                max_input_tokens=MAX_INPUT_TOKENS,
                max_output_tokens=MAX_OUTPUT_TOKENS,
                max_time_seconds=TIMEOUT_SECONDS - 30,  # leave cleanup margin before the hard kill
                # ↑ 900秒のうち870秒だけをOrchestratorに渡し、残り30秒は
                # container.cleanup()(コンテナの停止・削除)のための余白として
                # 確保する。MBPP版の余白(10秒)より広いのは、Dockerコンテナの
                # 停止・削除がプロセス終了より時間がかかりうるため。
                max_tokens_per_request=1500,
            ),
        )

        task_prompt = build_task_prompt(task)
        solution = orchestrator.run(task.instance_id, "swebench", task_prompt)

    except ShutdownRequested as exc:
        # A SIGTERM interrupted us outside the LLM-call window Orchestrator.run()
        # already guards (e.g. mid sandbox exec) - still land here gracefully so
        # the finally below reaches container.cleanup() well within moulinette's
        # SIGTERM->SIGKILL grace period, instead of getting SIGKILLed first.
        # ↑ 原文コメントの補足: たとえOrchestrator.run()の外側(例えば
        # container.start()の途中)でSIGTERMが来たとしても、ここでキャッチして
        # gracefulにerror_solutionを作り、下のfinallyでcontainer.cleanup()まで
        # 確実に到達させる。これができないと、moulinetteがSIGTERM送信後
        # 約10秒待ってSIGKILLするその猶予の中で後片付けが終わらず、
        # コンテナが起動しっぱなしのゴミとして残ってしまう。
        solution = error_solution(task.instance_id, f"stopped: {exc}")
    except Exception as exc:
        solution = error_solution(task.instance_id, f"Agent crashed: {type(exc).__name__}: {exc}")
    finally:
        # ここがagent_mbpp.pyとのもう1つの違い: container.cleanup()が
        # 追加されている。sandbox→mcp_proxy→containerの順で後始末する。
        # 各cleanupメソッド自体も内部で例外を握りつぶす設計になっている
        # (docker_runner.pyのcleanup()参照) - 後始末の失敗でエージェント
        # 全体がcrashしてsolution.jsonすら書けなくなる、という最悪の事態を
        # 避けるため。
        if sandbox is not None:
            sandbox.close()
        if mcp_proxy is not None:
            mcp_proxy.close()
        if container is not None:
            container.cleanup()

    Path(args.output).write_text(solution.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
