"""Autonomous MBPP agent CLI (Section 4.3.1).

    uv run python -m agent_mbpp --task-file task.json --output solution.json \\
        --model-name "model/name" --provider-url "https://provider.api/v1"
"""
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
from orchestrator import Orchestrator, OrchestratorConfig
from prompts import build_system_prompt
from sandbox.executor import DEFAULT_AUTHORIZED_IMPORTS, Sandbox
from sandbox.mcp_client import MCPToolProxy

REPO_ROOT = Path(__file__).resolve().parent
MCP_TOOLS_SCRIPT = REPO_ROOT / "mcp_tools_mbpp.py"
SCRATCH_DIR = Path("/tmp/agent")

MAX_ITERATIONS = 10
MAX_INPUT_TOKENS = 6_000
MAX_OUTPUT_TOKENS = 1_500
TIMEOUT_SECONDS = 120


def build_task_prompt(task: MBPPTaskInput) -> str:
    tests_preview = "\n".join(task.test_list) if task.test_list else "(no public tests provided)"
    return (
        f"Task: {task.task_definition}\n\n"
        f"Function signature: {task.function_definition}\n\n"
        f"Public tests your solution must pass (there may also be hidden tests):\n{tests_preview}\n\n"
        "Use run_tests(code, test_list) to check your solution against these assertions "
        "before submitting. Submit with final_answer(your_function_code) once confident."
    )


def error_solution(task_id: str, message: str) -> SolutionOutput:
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
        task_data = json.loads(Path(args.task_file).read_text())
        task = MBPPTaskInput.model_validate(task_data)
    except Exception as exc:
        solution = error_solution("unknown", f"Failed to load task file: {type(exc).__name__}: {exc}")
        Path(args.output).write_text(solution.model_dump_json(indent=2))
        sys.exit(1)

    orchestrator: Optional[Orchestrator] = None

    def handle_sigterm(signum: int, frame: object) -> None:
        if orchestrator is not None:
            orchestrator.request_stop()

    signal.signal(signal.SIGTERM, handle_sigterm)

    mcp_proxy = None
    try:
        SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
        mcp_proxy = MCPToolProxy(stdio_command=f"{sys.executable} {MCP_TOOLS_SCRIPT}")

        sandbox_config = SandboxConfig(
            authorized_imports=DEFAULT_AUTHORIZED_IMPORTS,
            allowed_directories=[str(SCRATCH_DIR)],
            # Must stay comfortably above mcp_tools_mbpp.py's own internal 10s
            # run_tests() subprocess timeout, or the outer sandbox alarm can fire
            # first on a legitimate (slow but correct) test run - found via a live
            # smoke test against a real provider, see README.md.
            max_execution_time_seconds=20,
            max_memory_mb=256,
        )
        sandbox = Sandbox(sandbox_config, extra_namespace=mcp_proxy.build_namespace())
        system_prompt = build_system_prompt("mbpp", mcp_proxy.manual_text())

        llm_client = LLMClient.from_provider_url(args.model_name, args.provider_url)
        orchestrator = Orchestrator(
            llm_client,
            sandbox,
            system_prompt,
            OrchestratorConfig(
                max_iterations=args.max_iterations,
                max_input_tokens=MAX_INPUT_TOKENS,
                max_output_tokens=MAX_OUTPUT_TOKENS,
                max_time_seconds=TIMEOUT_SECONDS - 10,
                max_tokens_per_request=400,
            ),
        )

        task_prompt = build_task_prompt(task)
        solution = orchestrator.run(str(task.task_id), "mbpp", task_prompt)

    except Exception as exc:
        solution = error_solution(str(task.task_id), f"Agent crashed: {type(exc).__name__}: {exc}")
    finally:
        if mcp_proxy is not None:
            mcp_proxy.close()

    Path(args.output).write_text(solution.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
