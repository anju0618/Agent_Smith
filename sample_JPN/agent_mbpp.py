"""Autonomous MBPP agent CLI (Section 4.3.1).

    uv run python -m agent_mbpp --task-file task.json --output solution.json \\
        --model-name "model/name" --provider-url "https://provider.api/v1"
"""
# このファイルは「MBPP（短いアルゴリズム問題）を解くエージェント」のコマンドライン
# エントリポイント。中身の役割はただ1つ: タスクを読み込み、必要な部品
# （サンドボックス・MCPツール・LLMクライアント・Orchestrator）を組み立てて
# Orchestrator に丸投げし、結果を solution.json として書き出すこと。
# エージェントの「考える」ロジックそのもの（Thought→Code→Observationループ）は
# 一切ここには無く、すべて orchestrator.py 側にある。ここは "配線" だけを担当する。
from __future__ import annotations

import argparse
import json
import signal
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from llm.client import LLMClient
from models import MBPPTaskInput, SandboxConfig, SolutionOutput
from orchestrator import Orchestrator, OrchestratorConfig, ShutdownRequested
from prompts import build_system_prompt
from sandbox.executor import DEFAULT_AUTHORIZED_IMPORTS, Sandbox
from sandbox.mcp_client import MCPToolProxy

# このファイル自身の場所（sample/ ディレクトリ）を基準に、MCPツールサーバーの
# スクリプトパスを組み立てる。相対パスに頼らないことで、どのディレクトリから
# 起動されても正しく `mcp_tools_mbpp.py` を見つけられるようにしている。
REPO_ROOT = Path(__file__).resolve().parent
MCP_TOOLS_SCRIPT = REPO_ROOT / "mcp_tools_mbpp.py"
# エージェントが実行中に書き込める唯一のディレクトリ。SandboxConfig.allowed_directories
# としてそのまま使われる（後述）。/tmp 配下なので、プロセス終了後も残るがOS再起動で消える。
SCRATCH_DIR = Path("/tmp/agent")

# MBPP用の予算。SWE-bench（agent_swebench.py）と比べて全体的に小さい値になっているのは、
# MBPPが「短い関数を1つ書いて公開テストに通す」だけの軽量なタスクだから。
MAX_ITERATIONS = 10
MAX_INPUT_TOKENS = 6_000
MAX_OUTPUT_TOKENS = 1_500
TIMEOUT_SECONDS = 120


def build_task_prompt(task: MBPPTaskInput) -> str:
    # moulinette から渡された MBPPTaskInput（Section 4/models.py参照）を、
    # LLMへの最初のユーザーメッセージ（自然言語のタスク説明）に変換する関数。
    # ここで作られる文字列が orchestrator.py の messages[1]["content"] になる。
    tests_preview = "\n".join(task.test_list) if task.test_list else "(no public tests provided)"
    imports_note = (
        # test_imports が指定されているタスクでは、「run_tests() がこのimportを
        # 自動的に前置してくれるので、あなたの解答コード自身に書く必要はない」と
        # LLMに明示的に伝える。これが無いと、LLMは「なぜかテストでNameErrorになる」
        # ことに戸惑い、無駄なイテレーションを消費しかねない。
        f"\n\nrun_tests() automatically makes these imports available to the assertions "
        f"above, so you don't need to add them yourself just for the tests to run: "
        f"{'; '.join(task.test_imports)}"
        if task.test_imports
        else ""
    )
    return (
        f"Task: {task.task_definition}\n\n"
        f"Function signature: {task.function_definition}\n\n"
        f"Public tests your solution must pass (there may also be hidden tests):\n{tests_preview}"
        f"{imports_note}\n\n"
        # 「隠しテストもあるかもしれない」と明記することで、LLMが公開テストだけに
        # 過適合した安易な解法（ハードコードなど）に逃げるのを牽制している。
        "Use run_tests(code, test_list) to check your solution against these assertions "
        "before submitting. Submit with final_answer(your_function_code) once confident."
    )


def error_solution(task_id: str, message: str) -> SolutionOutput:
    # 起動時のタスク読み込み失敗や、実行中の予期しないクラッシュなど、
    # 「正常にOrchestratorが回らなかった」ケース全般で使う、空っぽの失敗用
    # SolutionOutput を組み立てるヘルパー。moulinette 側の契約
    # （models.py の SolutionOutput）は success/solution/steps などの
    # フィールドがすべて必須なので、失敗時でも「型として正しい」JSONを
    # 必ず書き出せるようにするための存在。
    return SolutionOutput(
        task_id=task_id,
        benchmark="mbpp",
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
    parser = argparse.ArgumentParser(description="Agent Smith - MBPP agent")
    parser.add_argument("--task-file", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--model-name", required=True)
    parser.add_argument("--provider-url", required=True)
    parser.add_argument("--max-iterations", type=int, default=MAX_ITERATIONS)
    args = parser.parse_args()

    try:
        # --task-file に指定されたJSONファイルを読み、Pydanticモデル
        # MBPPTaskInput（models.py）でバリデーションする。ここで失敗するのは
        # 「JSONとして壊れている」「必須フィールドが無い」といったケースで、
        # まだ Orchestrator も Sandbox も一切生成していない段階なので、
        # 後始末すべきリソースは何も無い。即座にエラー用の solution.json を
        # 書いて sys.exit(1) する。
        task_data = json.loads(Path(args.task_file).read_text())
        task = MBPPTaskInput.model_validate(task_data)
    except Exception as exc:
        solution = error_solution("unknown", f"Failed to load task file: {type(exc).__name__}: {exc}")
        Path(args.output).write_text(solution.model_dump_json(indent=2))
        sys.exit(1)

    # orchestrator 変数はこの時点では None。SIGTERMハンドラのクロージャが
    # この変数を後から参照する（Pythonのクロージャは変数を「name」で捕捉するので、
    # 後で再代入されればハンドラ側からもその新しい値が見える）。
    orchestrator: Optional[Orchestrator] = None

    def handle_sigterm(signum: int, frame: object) -> None:
        # OSからSIGTERMが届いたときに呼ばれるハンドラ。
        # まだ Orchestrator が存在しない段階（Sandbox構築中など）でSIGTERMが
        # 来た場合は、自分で直接 ShutdownRequested を投げて、外側の
        # try/except/finally に後始末をさせる。
        # Orchestrator が既にあれば、request_stop() に委譲する
        # （orchestrator.py側で _stop_requested フラグを立てつつ、同じく
        #  ShutdownRequested を即座に送出する）。
        if orchestrator is None:
            raise ShutdownRequested("shutdown requested (e.g. SIGTERM)")
        orchestrator.request_stop()

    # ここでOS側にハンドラを登録する。以降このプロセスがSIGTERMを受け取ると、
    # デフォルトの「即終了」ではなく上のhandle_sigtermが呼ばれるようになる。
    signal.signal(signal.SIGTERM, handle_sigterm)

    mcp_proxy = None
    sandbox: Optional[Sandbox] = None
    try:
        # /tmp/agent をスクラッチ領域として用意。exist_ok=True なので
        # 既に存在していてもエラーにならない（複数回実行されるケースへの配慮）。
        SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
        # Some MBPP tasks' test assertions need an import (e.g. math.isclose)
        # that the candidate solution itself has no reason to include - passed
        # to mcp_tools_mbpp.py so run_tests() can prepend it itself rather
        # than relying on the LLM to guess it's needed (see mcp_tools_mbpp.py).
        # ↑ 上のコメントは元のコードにあったもの。日本語で補足すると:
        # task.test_imports（例: ["import math"]）をJSON文字列にして環境変数
        # AGENT_SMITH_TEST_IMPORTS に詰め、これから起動するMCPツールサーバー
        # （別プロセス）の環境として渡す。mcp_tools_mbpp.py 側の
        # _test_imports() がこの環境変数を読み取り、run_tests() の中で
        # 候補コードの前に自動的に前置する。
        tool_env = {"AGENT_SMITH_TEST_IMPORTS": json.dumps(task.test_imports)}
        # MCPToolProxy を stdio モードで起動: 実体は
        # `python mcp_tools_mbpp.py` を子プロセスとしてサブプロセス起動し、
        # 標準入出力パイプ越しにMCPプロトコルで通信する。env=tool_env で
        # 上で作った環境変数を子プロセスにだけ渡す（親プロセスの環境は汚さない）。
        mcp_proxy = MCPToolProxy(stdio_command=f"{sys.executable} {MCP_TOOLS_SCRIPT}", env=tool_env)

        sandbox_config = SandboxConfig(
            authorized_imports=DEFAULT_AUTHORIZED_IMPORTS,
            # MBPPでは、エージェントが読み書きしていいディレクトリはスクラッチ
            # 領域だけ。SWE-benchのように実リポジトリ(/testbed)を触る必要が
            # 無いため、アクセス範囲を最小限に絞っている。
            allowed_directories=[str(SCRATCH_DIR)],
            # Must stay comfortably above mcp_tools_mbpp.py's own internal 10s
            # run_tests() subprocess timeout, or the outer sandbox alarm can fire
            # first on a legitimate (slow but correct) test run - found via a live
            # smoke test against a real provider, see README.md.
            # ↑ このタイムアウト値(20秒)は、mcp_tools_mbpp.py 内部で
            # run_tests() が使っている「使い捨てサンドボックスの10秒タイムアウト」
            # よりも確実に長く設定しなければならない。もし外側(このSandbox)の
            # タイムアウトの方が短い、あるいは同じだと、正しいが少し遅い
            # テスト実行を外側のアラームが先に殺してしまい、本来なら合格するはずの
            # 解答を誤ってタイムアウト扱いにしてしまう。実際に本番相当のLLM
            # プロバイダに対するスモークテストで発見された不具合。
            max_execution_time_seconds=20,
            max_memory_mb=256,
        )
        # extra_namespace=mcp_proxy.build_namespace() で、MCPサーバーが
        # 公開しているツール(run_tests)を、サンドボックス内のPython名前空間に
        # 「ただの関数」として注入する。LLMが書くコードからは
        # `run_tests(code=..., test_list=...)` という普通の関数呼び出しに
        # 見えるが、実体は裏でMCPプロトコル越しに別プロセスへ委譲されている。
        sandbox = Sandbox(sandbox_config, extra_namespace=mcp_proxy.build_namespace())
        # システムプロンプトを組み立てる。"mbpp" を渡すことでMBPP用の
        # final_answer説明文・worked exampleが選ばれる(prompts.py参照)。
        # mcp_proxy.manual_text() が「今つながっているMCPサーバーのツール一覧」を
        # 動的に生成するので、ここでどんなツールがあるかをこのファイルは
        # 一切ハードコードしていない。
        system_prompt = build_system_prompt("mbpp", mcp_proxy.manual_text())

        # --model-name と --provider-url から、キーのローテーション・
        # プロバイダのフォールバックまで面倒を見てくれる LLMClient を作る
        # (config.py の resolve_provider() 経由で未知のプロバイダURLにも対応)。
        llm_client = LLMClient.from_provider_url(args.model_name, args.provider_url)
        # ここでようやく Orchestrator を生成する。この代入が終わった瞬間から、
        # 上で定義した handle_sigterm はこの orchestrator インスタンスの
        # request_stop() を呼べるようになる。
        orchestrator = Orchestrator(
            llm_client,
            sandbox,
            system_prompt,
            OrchestratorConfig(
                max_iterations=args.max_iterations,
                max_input_tokens=MAX_INPUT_TOKENS,
                max_output_tokens=MAX_OUTPUT_TOKENS,
                # 全体のタイムアウト(120秒)から10秒を引いた110秒だけを
                # Orchestratorに与える。残りの10秒は、ループを抜けたあとの
                # sandbox.close()/mcp_proxy.close() などの後始末に使う余白。
                max_time_seconds=TIMEOUT_SECONDS - 10,
                max_tokens_per_request=400,
            ),
        )

        task_prompt = build_task_prompt(task)
        # ここが実際にエージェントループが走る箇所。Thought→Code→Observationを
        # 繰り返し、成功・失敗いずれの場合も SolutionOutput を返してくる
        # (Section5/orchestrator.py参照)。この1行の中に、このプロジェクトの
        # 本体ロジックがすべて詰まっている。
        solution = orchestrator.run(str(task.task_id), "mbpp", task_prompt)

    except ShutdownRequested as exc:
        # SIGTERMによってループが中断された場合。ここまでに積まれた steps は
        # 失われる(Orchestrator.run内で例外により関数を抜けているため)が、
        # 「なぜ止まったか」を説明する solution.json だけは必ず書き出す。
        solution = error_solution(str(task.task_id), f"stopped: {exc}")
    except Exception as exc:
        # 予期しないあらゆる例外(バグ、ネットワーク障害など)をここで拾い、
        # プロセスをクラッシュさせるのではなく、必ず solution.json を
        # 書き出してから終了する。"General Rules" の「エラーは必ず
        # グレースフルに処理する」という要求を満たすための最後の砦。
        solution = error_solution(str(task.task_id), f"Agent crashed: {type(exc).__name__}: {exc}")
    finally:
        # try節のどこで抜けても(正常終了・SIGTERM・クラッシュいずれでも)
        # 必ずここを通る。生成済みのリソースだけを安全に閉じる
        # (Noneチェックしているのは、途中で例外が起きて一部しか
        #  生成されていない場合に備えるため)。
        if sandbox is not None:
            sandbox.close()
        if mcp_proxy is not None:
            mcp_proxy.close()

    # 成功でも失敗でも、最後に必ず1回だけ solution.json を書き出す。
    # これが moulinette が読みにいく最終成果物。
    Path(args.output).write_text(solution.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
