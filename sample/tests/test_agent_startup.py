"""Startup error and shutdown handling for both agent CLIs."""
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
