"""両方のエージェントCLI(MBPP用・SWE-bench用)における起動時エラーとシャットダウン処理のテスト。"""
import json  # タスクファイル/出力ファイルのJSON読み書きに使用
import sys  # sys.argvを差し替えてCLI引数をシミュレートするために使用
from pathlib import Path  # ファイルパス操作のために使用
from typing import Callable, Dict  # 型ヒント(コールバック関数・辞書)のために使用

import pytest  # テストフレームワーク本体、monkeypatch型ヒントにも使用

import agent_mbpp  # MBPP用エージェントCLIのエントリーポイントモジュール
import agent_swebench  # SWE-bench用エージェントCLIのエントリーポイントモジュール


def _run_cli(
    monkeypatch: pytest.MonkeyPatch,
    module_name: str,
    task_file: Path,
    output_file: Path,
) -> None:
    # sys.argvを書き換えて、指定したタスクファイル・出力ファイル・モデル名・
    # プロバイダURLでCLIが起動されたかのように振る舞わせる
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
    # SWE-bench: Dockerコンテナの初期化に失敗した場合、
    # エラー内容を含むsolution.jsonが書き出されることを検証するテスト
    task_file = tmp_path / "task.json"  # 一時ディレクトリに置くタスク定義ファイル
    output_file = tmp_path / "solution.json"  # 一時ディレクトリに置く出力先ファイル
    task_file.write_text(
        json.dumps(
            {
                "instance_id": "project__repo-1",  # SWE-benchタスクの識別子
                "problem_statement": "fix it",  # 問題文(ダミー)
                "docker_image": "missing:image",  # 存在しないDockerイメージ名
                "eval_script": "true",  # 評価スクリプト(ダミー)
            }
        )
    )

    class FailingContainer:
        # コンストラクタで必ず例外を投げる、Docker初期化失敗をシミュレートするフェイククラス
        def __init__(self, docker_image: str) -> None:
            raise RuntimeError("docker unavailable")

    # SweBenchContainerを上のフェイククラスに差し替える
    monkeypatch.setattr(agent_swebench, "SweBenchContainer", FailingContainer)
    # シグナルハンドラ登録を無害化(実際にシグナルハンドラを設定しない)
    monkeypatch.setattr(agent_swebench.signal, "signal", lambda signum, handler: None)
    # CLI引数を差し替える
    _run_cli(monkeypatch, "agent_swebench", task_file, output_file)

    # 実際にCLIのmain関数を実行
    agent_swebench.main()

    # 出力されたsolution.jsonの内容を検証
    result = json.loads(output_file.read_text())
    assert result["success"] is False  # 失敗として記録されているはず
    assert "docker unavailable" in result["error"]  # エラーメッセージに原因が含まれるはず


def test_swebench_sigterm_before_orchestrator_still_stops_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # SWE-bench: orchestrator起動前(コンテナ初期化中)にSIGTERMを受け取っても、
    # 正常にシャットダウン処理されエラーとして記録されることを検証するテスト
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
    handlers: Dict[str, Callable[[int, object], None]] = {}  # 登録されたシグナルハンドラを保持する辞書

    def capture_signal(signum: int, handler: Callable[[int, object], None]) -> None:
        # signal.signal()の代わりに呼ばれ、実際に登録せずハンドラを保存するだけにする
        handlers["sigterm"] = handler

    class InterruptingContainer:
        # コンストラクタの中でSIGTERMハンドラを呼び出し、初期化中の割り込みを再現するフェイククラス
        def __init__(self, docker_image: str) -> None:
            handlers["sigterm"](agent_swebench.signal.SIGTERM, None)

    monkeypatch.setattr(agent_swebench.signal, "signal", capture_signal)  # シグナル登録を差し替え
    monkeypatch.setattr(agent_swebench, "SweBenchContainer", InterruptingContainer)  # コンテナ初期化を差し替え
    _run_cli(monkeypatch, "agent_swebench", task_file, output_file)

    agent_swebench.main()

    result = json.loads(output_file.read_text())
    assert result["success"] is False  # 失敗として記録されているはず
    assert "shutdown requested" in result["error"]  # シャットダウン要求が原因と記録されているはず


def test_mbpp_sigterm_before_orchestrator_still_stops_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # MBPP: orchestrator起動前(MCPプロキシ初期化中)にSIGTERMを受け取っても、
    # 正常にシャットダウン処理されエラーとして記録されることを検証するテスト
    task_file = tmp_path / "task.json"
    output_file = tmp_path / "solution.json"
    task_file.write_text(
        json.dumps(
            {
                "task_id": 1,  # MBPPタスクのID
                "task_definition": "add numbers",  # タスクの説明文
                "function_definition": "def add(a, b):",  # 関数シグネチャの雛形
                "test_list": ["assert add(1, 2) == 3"],  # 検証用のassert文リスト
            }
        )
    )
    handlers: Dict[str, Callable[[int, object], None]] = {}  # 登録されたシグナルハンドラを保持する辞書

    def capture_signal(signum: int, handler: Callable[[int, object], None]) -> None:
        # signal.signal()の代わりに呼ばれ、実際に登録せずハンドラを保存するだけにする
        handlers["sigterm"] = handler

    class InterruptingProxy:
        # コンストラクタの中でSIGTERMハンドラを呼び出し、初期化中の割り込みを再現するフェイククラス
        def __init__(self, stdio_command: str, env: Dict[str, str]) -> None:
            handlers["sigterm"](agent_mbpp.signal.SIGTERM, None)

    monkeypatch.setattr(agent_mbpp.signal, "signal", capture_signal)  # シグナル登録を差し替え
    monkeypatch.setattr(agent_mbpp, "MCPToolProxy", InterruptingProxy)  # MCPプロキシ初期化を差し替え
    monkeypatch.setattr(agent_mbpp, "SCRATCH_DIR", tmp_path / "scratch")  # 作業用スクラッチディレクトリを一時パスに変更
    _run_cli(monkeypatch, "agent_mbpp", task_file, output_file)

    agent_mbpp.main()

    result = json.loads(output_file.read_text())
    assert result["success"] is False  # 失敗として記録されているはず
    assert "shutdown requested" in result["error"]  # シャットダウン要求が原因と記録されているはず
