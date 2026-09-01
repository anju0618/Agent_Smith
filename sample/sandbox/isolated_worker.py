"""Worker entry point for :mod:`sandbox.isolated_process`.

This file is intentionally small and uses only the project sandbox plus the
standard library.  It never receives callable objects from the host; MCP
tools are represented by JSON-RPC-style bridge functions.
"""
from __future__ import annotations

import json
import sys
from typing import Any, Callable, Dict, cast

from models import SandboxConfig
from sandbox.executor import FinalAnswer, Sandbox


def _send(output: Any, message: Dict[str, Any]) -> None:
    output.write(json.dumps(message, separators=(",", ":"), default=str) + "\n")
    output.flush()


def _read(input_stream: Any) -> Dict[str, Any]:
    line = input_stream.readline()
    if not line:
        raise EOFError("parent process closed the worker protocol")
    message = json.loads(line)
    if not isinstance(message, dict):
        raise ValueError("parent sent a non-object message")
    return message


class _ToolBridge:
    def __init__(self, name: str, input_stream: Any, output: Any) -> None:
        self._name = name
        self._input_stream = input_stream
        self._output = output

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        _send(
            self._output,
            {"type": "tool_call", "name": self._name, "args": list(args), "kwargs": kwargs},
        )
        response = _read(self._input_stream)
        if response.get("type") != "tool_result":
            raise RuntimeError(f"parent sent unexpected MCP response: {response.get('type')!r}")
        if not response.get("ok", False):
            raise RuntimeError(str(response.get("result", "MCP tool call failed")))
        return response.get("result")


def main() -> int:
    protocol_input = sys.stdin
    protocol_output = sys.__stdout__
    try:
        init = _read(protocol_input)
        if init.get("type") != "init":
            raise ValueError("worker did not receive an init message")
        config = SandboxConfig.model_validate(init["config"])
        tool_names = init.get("tool_names", [])
        if not isinstance(tool_names, list) or not all(isinstance(name, str) for name in tool_names):
            raise ValueError("worker received invalid MCP tool names")
        namespace: Dict[str, Callable[..., Any]] = {
            name: cast(Callable[..., Any], _ToolBridge(name, protocol_input, protocol_output))
            for name in tool_names
        }
        sandbox = Sandbox(
            config,
            extra_namespace=namespace,
            apply_process_memory_limit=bool(init.get("apply_process_memory_limit", True)),
            isolated=False,
        )
        _send(protocol_output, {"type": "ready"})
    except BaseException as exc:  # noqa: BLE001 - parent needs startup diagnostics
        _send(protocol_output, {"type": "worker_error", "error": f"{type(exc).__name__}: {exc}"})
        return 1

    while True:
        try:
            message = _read(protocol_input)
        except (EOFError, ValueError, json.JSONDecodeError):
            return 0
        message_type = message.get("type")
        if message_type == "close":
            return 0
        if message_type != "run":
            _send(protocol_output, {"type": "worker_error", "error": "unknown worker command"})
            continue

        try:
            output = sandbox.run(str(message.get("code", "")))
        except FinalAnswer as exc:
            _send(protocol_output, {"type": "final_answer", "answer": exc.answer})
        except KeyboardInterrupt:
            _send(protocol_output, {"type": "keyboard_interrupt"})
        except SystemExit as exc:
            _send(protocol_output, {"type": "system_exit", "code": exc.code})
        except BaseException as exc:  # noqa: BLE001 - keep the worker alive for the next step
            _send(protocol_output, {"type": "worker_error", "error": f"{type(exc).__name__}: {exc}"})
        else:
            _send(protocol_output, {"type": "result", "output": output})


if __name__ == "__main__":
    raise SystemExit(main())
