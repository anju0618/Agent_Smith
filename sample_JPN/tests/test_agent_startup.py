"""Startup error and shutdown handling for both agent CLIs."""
# ============================================================================
# 日本語解説: このファイルは agent_mbpp.py / agent_swebench.py という
# 2つのエージェントCLIの「起動直後のエラー処理」と「SIGTERM相当の
# シャットダウン処理」をテストしています。本物のDockerや本物のMCP
# サブプロセスは使わず、SweBenchContainerやMCPToolProxyといった
# 依存コンポーネントをmonkeypatchで偽物に差し替えることで、
# 「起動処理の途中でエラーが起きた場合」「Orchestratorがまだ
# 生成される前にSIGTERMが届いた場合」といった、通常のテストでは
# 再現しづらいタイミングの問題を確実に再現しています。
# ============================================================================
import json
import sys
from pathlib import Path
from typing import Callable, Dict

import pytest

import agent_mbpp
import agent_swebench


def _run_cli(
    monkeypatch: pytest.MonkeyPatch,
    module_name: str,
    task_file: Path,
    output_file: Path,
) -> None:
    # sys.argvを差し替えて、あたかもコマンドラインから
    # `python -m agent_mbpp --task-file ... --output ...` のように
    # 起動されたかのように見せかけるヘルパー関数。
    monkeypatch.setattr(
        sys,
        "argv",
        [
            module_name,
            "--task-file",
            str(task_file),
            "--output",
            str(output_file),
            "--model-name",
            "fake-model",
            "--provider-url",
            "https://fake.invalid/v1",
        ],
    )


def test_swebench_docker_initialization_failure_writes_error_solution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # SweBenchContainerの初期化(Dockerイメージのpull/起動)が失敗する
    # 状況をFailingContainerという偽クラスで再現する。エージェントが
    # クラッシュして何も出力しないのではなく、たとえ起動処理の
    # 最初期段階で失敗しても、success: falseと具体的なエラー内容
    # ("docker unavailable")を含んだsolution.jsonが必ず書き出される
    # ことを確認する。これは「成功でも失敗でも必ずsolution.jsonを
    # 書き出す」という設計方針(EXPLAINED.md 11節)の裏付け。
    task_file = tmp_path / "task.json"
    output_file = tmp_path / "solution.json"
    task_file.write_text(
        json.dumps(
            {
                "instance_id": "project__repo-1",
                "problem_statement": "fix it",
                "docker_image": "missing:image",
                "eval_script": "true",
            }
        )
    )

    class FailingContainer:
        def __init__(self, docker_image: str) -> None:
            raise RuntimeError("docker unavailable")

    monkeypatch.setattr(agent_swebench, "SweBenchContainer", FailingContainer)
    monkeypatch.setattr(agent_swebench.signal, "signal", lambda signum, handler: None)
    _run_cli(monkeypatch, "agent_swebench", task_file, output_file)

    agent_swebench.main()

    result = json.loads(output_file.read_text())
    assert result["success"] is False
    assert "docker unavailable" in result["error"]


def test_swebench_sigterm_before_orchestrator_still_stops_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # このテストが再現しているのは「まだOrchestratorインスタンスが
    # 作られる前(=agent_swebench.pyのorchestrator変数がNoneのまま)の
    # タイミングでSIGTERMが届いたらどうなるか」という、前の会話で
    # 話したhandle_sigterm()の分岐（if orchestrator is None:
    # raise ShutdownRequested(...)）を狙い撃ちしたテストです。
    #
    # signal.signalを直接呼ぶ代わりに、渡されたハンドラ関数を
    # handlers["sigterm"]という辞書に保存しておくcapture_signalで
    # 差し替えています。そしてInterruptingContainerのコンストラクタの
    # 中で、あたかも「まさにこのタイミングでSIGTERMが届いた」かのように
    # そのハンドラを手動で呼び出すことで、実際のOSシグナルを送らなくても
    # 「Docker起動処理の最中にSIGTERMが来る」という状況を確実に
    # 再現しています。それでもクラッシュせず、"shutdown requested"という
    # エラーを含んだsolution.jsonがきちんと書き出されることを確認する。
    task_file = tmp_path / "task.json"
    output_file = tmp_path / "solution.json"
    task_file.write_text(
        json.dumps(
            {
                "instance_id": "project__repo-1",
                "problem_statement": "fix it",
                "docker_image": "fake:image",
                "eval_script": "true",
            }
        )
    )
    handlers: Dict[str, Callable[[int, object], None]] = {}

    def capture_signal(signum: int, handler: Callable[[int, object], None]) -> None:
        handlers["sigterm"] = handler

    class InterruptingContainer:
        def __init__(self, docker_image: str) -> None:
            handlers["sigterm"](agent_swebench.signal.SIGTERM, None)

    monkeypatch.setattr(agent_swebench.signal, "signal", capture_signal)
    monkeypatch.setattr(agent_swebench, "SweBenchContainer", InterruptingContainer)
    _run_cli(monkeypatch, "agent_swebench", task_file, output_file)

    agent_swebench.main()

    result = json.loads(output_file.read_text())
    assert result["success"] is False
    assert "shutdown requested" in result["error"]


def test_mbpp_sigterm_before_orchestrator_still_stops_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 上のSWE-bench版と全く同じ考え方のテストを、agent_mbpp.py側でも
    # 行っている。今度はMCPToolProxyの初期化中(MCPサブプロセス起動中)
    # にSIGTERMが届く状況を再現し、同じく正しくエラー終了することを確認する。
    task_file = tmp_path / "task.json"
    output_file = tmp_path / "solution.json"
    task_file.write_text(
        json.dumps(
            {
                "task_id": 1,
                "task_definition": "add numbers",
                "function_definition": "def add(a, b):",
                "test_list": ["assert add(1, 2) == 3"],
            }
        )
    )
    handlers: Dict[str, Callable[[int, object], None]] = {}

    def capture_signal(signum: int, handler: Callable[[int, object], None]) -> None:
        handlers["sigterm"] = handler

    class InterruptingProxy:
        def __init__(self, stdio_command: str, env: Dict[str, str]) -> None:
            handlers["sigterm"](agent_mbpp.signal.SIGTERM, None)

    monkeypatch.setattr(agent_mbpp.signal, "signal", capture_signal)
    monkeypatch.setattr(agent_mbpp, "MCPToolProxy", InterruptingProxy)
    monkeypatch.setattr(agent_mbpp, "SCRATCH_DIR", tmp_path / "scratch")
    _run_cli(monkeypatch, "agent_mbpp", task_file, output_file)

    agent_mbpp.main()

    result = json.loads(output_file.read_text())
    assert result["success"] is False
    assert "shutdown requested" in result["error"]
