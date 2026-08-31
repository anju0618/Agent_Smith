"""Autonomous SWE-bench agent CLI (Section 4.4).

    uv run python -m agent_swebench --task-file task.json --output solution.json \\
        --model-name "model/name" --provider-url "https://provider.api/v1"

Exercised end to end against real SWE-bench Docker images across 5
models/3 providers/3 tasks - see BENCHMARK_REPORT.md for the full comparison,
including one independently-verified passing patch.
"""
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
TOOLS_FILE = REPO_ROOT / "mcp_tools_swebench.py"
SCRATCH_DIR = Path("/tmp/agent")

MAX_ITERATIONS = 30
MAX_INPUT_TOKENS = 300_000
MAX_OUTPUT_TOKENS = 10_000
TIMEOUT_SECONDS = 900


def build_task_prompt(task: SWEBenchTaskInput) -> str:
    hints = f"\n\nHints:\n{task.hints_text}" if task.hints_text else ""
    return (
        f"Repository: {task.repo}\n"
        "Working directory: /testbed\n\n"
        f"Issue to fix:\n{task.problem_statement}{hints}\n\n"
        "Explore the repository, make the minimal change that fixes the issue, "
        "verify it with run_tests(), and then submit with final_answer(get_patch())."
    )


def error_solution(task_id: str, message: str) -> SolutionOutput:
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
        task_data = json.loads(Path(args.task_file).read_text())
        task = SWEBenchTaskInput.model_validate(task_data)
    except Exception as exc:
        solution = error_solution("unknown", f"Failed to load task file: {type(exc).__name__}: {exc}")
        Path(args.output).write_text(solution.model_dump_json(indent=2))
        sys.exit(1)

    container = SweBenchContainer(task.docker_image)
    mcp_proxy = None
    orchestrator: Optional[Orchestrator] = None

    def handle_sigterm(signum: int, frame: object) -> None:
        if orchestrator is not None:
            orchestrator.request_stop()

    signal.signal(signal.SIGTERM, handle_sigterm)

    try:
        SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
        container.start(eval_script=task.eval_script, tools_file=TOOLS_FILE)
        mcp_proxy = MCPToolProxy(stdio_command=container.mcp_stdio_command())

        sandbox_config = SandboxConfig(
            authorized_imports=DEFAULT_AUTHORIZED_IMPORTS,
            allowed_directories=["/testbed", str(SCRATCH_DIR)],
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
        solution = error_solution(task.instance_id, f"stopped: {exc}")
    except Exception as exc:
        solution = error_solution(task.instance_id, f"Agent crashed: {type(exc).__name__}: {exc}")
    finally:
        if mcp_proxy is not None:
            mcp_proxy.close()
        container.cleanup()

    Path(args.output).write_text(solution.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
