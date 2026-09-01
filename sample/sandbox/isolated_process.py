"""Host-side controller for the process-isolated sandbox worker.

The worker runs inside a network-disabled user namespace and a bubblewrap
filesystem namespace.  Only the names of MCP tools cross that boundary; tool
calls are sent back to the trusted parent process over a small JSON protocol.
"""
from __future__ import annotations

import json
import os
import queue
import selectors
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple, cast


STARTUP_TIMEOUT_SECONDS = 10.0
WORKER_TERMINATE_TIMEOUT_SECONDS = 0.5
WORKER_KILL_TIMEOUT_SECONDS = 1.0


class IsolatedSandboxProcess:
    """Own one persistent, OS-isolated worker for a :class:`Sandbox`."""

    def __init__(
        self,
        config: Any,
        extra_namespace: Dict[str, Callable[..., Any]],
        apply_process_memory_limit: bool,
    ) -> None:
        self._config = config
        self._extra_namespace = extra_namespace
        self._process: Optional[subprocess.Popen[str]] = None
        self._selector: Optional[selectors.BaseSelector] = None
        self._closed = False
        self._start(config, apply_process_memory_limit)

    def _start(self, config: Any, apply_process_memory_limit: bool) -> None:
        command = self._build_command(config)
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
        except OSError as exc:
            raise RuntimeError(f"could not start isolated sandbox worker: {exc}") from exc

        self._process = process
        if process.stdout is None:
            raise RuntimeError("isolated sandbox worker has no stdout pipe")
        self._selector = selectors.DefaultSelector()
        self._selector.register(process.stdout, selectors.EVENT_READ)

        self._send(
            {
                "type": "init",
                "config": self._worker_config(config),
                "tool_names": list(self._extra_namespace),
                "apply_process_memory_limit": apply_process_memory_limit,
            }
        )
        try:
            message = self._read_message(STARTUP_TIMEOUT_SECONDS)
        except (EOFError, TimeoutError, ValueError) as exc:
            self._terminate_process()
            raise RuntimeError(f"isolated sandbox worker did not initialize: {exc}") from exc
        if message.get("type") != "ready":
            self._terminate_process()
            detail = message.get("error", "unknown worker initialization error")
            raise RuntimeError(f"isolated sandbox worker failed to initialize: {detail}")

    def _build_command(self, config: Any) -> list[str]:
        if sys.platform != "linux":
            raise RuntimeError("the isolated sandbox requires Linux user namespaces")

        unshare = shutil.which("unshare")
        bubblewrap = shutil.which("bwrap")
        if unshare is None or bubblewrap is None:
            raise RuntimeError("the isolated sandbox requires both 'unshare' and 'bwrap'")

        python_path = Path(sys.executable).resolve()
        project_root = Path(__file__).resolve().parents[1]
        sandbox_source = project_root / "sandbox"
        models_source = project_root / "models.py"
        if not python_path.is_file() or not sandbox_source.is_dir() or not models_source.is_file():
            raise RuntimeError("isolated sandbox runtime files are missing")

        site_packages = self._site_packages()
        if not site_packages:
            raise RuntimeError("isolated sandbox could not find the project's site-packages")
        if Path("/usr") not in python_path.parents:
            raise RuntimeError("isolated sandbox requires a Python interpreter under /usr")

        command = [
            unshare,
            "--user",
            "--map-root-user",
            "--net",
            "--",
            bubblewrap,
            "--clearenv",
            "--die-with-parent",
            "--unshare-user",
            "--uid",
            "65534",
            "--gid",
            "65534",
        ]
        for system_path in (Path("/usr"), Path("/lib"), Path("/lib64")):
            if system_path.exists():
                command.extend(["--ro-bind", str(system_path), str(system_path)])

        command.extend(
            [
                "--dev",
                "/dev",
                "--proc",
                "/proc",
                "--tmpfs",
                "/tmp",
                "--dir",
                "/agent",
                "--dir",
                "/agent/sandbox",
                "--ro-bind",
                str(sandbox_source),
                "/agent/sandbox",
                "--dir",
                "/agent/site-packages",
                "--ro-bind",
                str(site_packages[0]),
                "/agent/site-packages",
                "--ro-bind",
                str(models_source),
                "/agent/models.py",
            ]
        )

        # The worker needs only the project package and its dependencies.  In
        # particular, the repository root (which may contain .env files) is not
        # mounted wholesale into the untrusted process.
        python_path_entries = ["/agent", "/agent/site-packages"]
        for index, path in enumerate(site_packages[1:], start=1):
            target = f"/agent/site-packages-{index}"
            command.extend(["--dir", target, "--ro-bind", str(path), target])
            python_path_entries.append(target)

        mounted_targets = {
            Path("/agent"),
            Path("/agent/sandbox"),
            Path("/agent/site-packages"),
            Path("/tmp"),
            Path("/dev"),
            Path("/proc"),
            Path("/usr"),
            Path("/lib"),
            Path("/lib64"),
        }
        for directory in config.allowed_directories:
            mount_target = self._resolve_allowed_directory(directory)
            self._validate_allowed_target(mount_target)
            self._add_directory_mounts(command, mount_target, mounted_targets)
            if mount_target.is_dir():
                command.extend(["--bind", str(mount_target), str(mount_target)])

        command.extend(
            [
                "--setenv",
                "PYTHONPATH",
                os.pathsep.join(python_path_entries),
                "--setenv",
                "PYTHONUNBUFFERED",
                "1",
                "--setenv",
                "PYTHONDONTWRITEBYTECODE",
                "1",
                "--setenv",
                "HOME",
                "/tmp",
                "--chdir",
                "/agent",
                str(python_path),
                "/agent/sandbox/isolated_worker.py",
            ]
        )
        return command

    @staticmethod
    def _resolve_allowed_directory(directory: str) -> Path:
        target = Path(directory).expanduser()
        if not target.is_absolute():
            target = Path.cwd() / target
        return target.resolve()

    def _worker_config(self, config: Any) -> Dict[str, Any]:
        config_data = cast(Dict[str, Any], config.model_dump(mode="json"))
        config_data["allowed_directories"] = [
            str(self._resolve_allowed_directory(directory))
            for directory in config.allowed_directories
        ]
        return config_data

    @staticmethod
    def _site_packages() -> list[Path]:
        paths: list[Path] = []
        for entry in sys.path:
            if not entry:
                continue
            path = Path(entry)
            if path.name not in {"site-packages", "dist-packages"} or not path.is_dir():
                continue
            resolved = path.resolve()
            if resolved not in paths:
                paths.append(resolved)
        return paths

    @staticmethod
    def _validate_allowed_target(target: Path) -> None:
        protected = {
            Path("/"),
            Path("/agent"),
            Path("/dev"),
            Path("/lib"),
            Path("/lib64"),
            Path("/proc"),
            Path("/tmp"),
            Path("/usr"),
        }
        if target in protected:
            raise ValueError(f"allowed directory '{target}' would weaken the isolated root")
        protected_roots = protected - {Path("/"), Path("/tmp")}
        if any(root in target.parents for root in protected_roots):
            raise ValueError(f"allowed directory '{target}' is inside a protected root")

    @staticmethod
    def _add_directory_mounts(
        command: list[str], target: Path, mounted_targets: set[Path]
    ) -> None:
        current = Path("/")
        for part in target.parts[1:]:
            current /= part
            if current in mounted_targets:
                continue
            command.extend(["--dir", str(current)])
            mounted_targets.add(current)

    def _send(self, message: Dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise EOFError("isolated sandbox worker stdin is closed")
        process.stdin.write(json.dumps(message, separators=(",", ":"), default=str) + "\n")
        process.stdin.flush()

    def _read_message(self, timeout: float) -> Dict[str, Any]:
        if self._selector is None or self._process is None or self._process.stdout is None:
            raise EOFError("isolated sandbox worker stdout is closed")
        events = self._selector.select(max(timeout, 0.0))
        if not events:
            raise TimeoutError(f"no worker response within {timeout:.3f}s")
        line = self._process.stdout.readline()
        if not line:
            raise EOFError("isolated sandbox worker exited")
        message = json.loads(line)
        if not isinstance(message, dict):
            raise ValueError("isolated sandbox worker sent a non-object message")
        return message

    def run(self, code: str) -> str:
        if self._closed:
            return "[IsolatedSandboxError] sandbox worker is closed"

        timeout = float(self._config.max_execution_time_seconds)
        if timeout <= 0:
            return f"[Timeout] Execution exceeded {timeout:g}s and was not started."
        try:
            self._send({"type": "run", "code": code})
        except (BrokenPipeError, EOFError, OSError) as exc:
            self._closed = True
            return f"[IsolatedSandboxError] could not send code to worker: {exc}"

        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._terminate_process()
                self._closed = True
                return f"[Timeout] Execution exceeded {timeout:g}s and was interrupted."
            try:
                message = self._read_message(remaining)
            except TimeoutError:
                self._terminate_process()
                self._closed = True
                return f"[Timeout] Execution exceeded {timeout:g}s and was interrupted."
            except (EOFError, ValueError, json.JSONDecodeError) as exc:
                self._closed = True
                return f"[IsolatedSandboxError] worker protocol failed: {exc}"

            message_type = message.get("type")
            if message_type == "tool_call":
                ok, result, timed_out = self._invoke_tool(
                    message.get("name"), message.get("args", []), message.get("kwargs", {}), remaining
                )
                if timed_out:
                    self._terminate_process()
                    self._closed = True
                    return f"[Timeout] Execution exceeded {timeout:g}s while calling an MCP tool."
                try:
                    self._send({"type": "tool_result", "ok": ok, "result": result})
                except (BrokenPipeError, EOFError, OSError) as exc:
                    self._closed = True
                    return f"[IsolatedSandboxError] could not return MCP result: {exc}"
                continue
            if message_type == "result":
                return str(message.get("output", ""))
            if message_type == "final_answer":
                from sandbox.executor import FinalAnswer

                raise FinalAnswer(message.get("answer"))
            if message_type == "keyboard_interrupt":
                raise KeyboardInterrupt()
            if message_type == "system_exit":
                raise SystemExit(message.get("code"))
            if message_type == "worker_error":
                return f"[IsolatedSandboxError] {message.get('error', 'unknown worker error')}"
            return f"[IsolatedSandboxError] unknown worker message: {message_type!r}"

    def _invoke_tool(
        self, name: Any, args: Any, kwargs: Any, timeout: float
    ) -> Tuple[bool, Any, bool]:
        if not isinstance(name, str):
            return False, "invalid MCP tool name", False
        function = self._extra_namespace.get(name)
        if function is None:
            return False, f"unknown MCP tool '{name}'", False
        if not isinstance(args, list) or not isinstance(kwargs, dict):
            return False, "invalid MCP tool arguments", False

        result_queue: "queue.Queue[Tuple[bool, Any]]" = queue.Queue(maxsize=1)

        def invoke() -> None:
            try:
                result_queue.put((True, function(*args, **kwargs)))
            except BaseException as exc:  # noqa: BLE001 - return tool failures to the worker
                result_queue.put((False, f"{type(exc).__name__}: {exc}"))

        thread = threading.Thread(target=invoke, name="agent-smith-mcp-call", daemon=True)
        thread.start()
        thread.join(max(timeout, 0.0))
        if thread.is_alive():
            return False, "MCP tool call timed out", True
        succeeded, result = result_queue.get()
        if not succeeded:
            return False, result, False
        try:
            json.dumps(result)
        except (TypeError, ValueError):
            result = str(result)
        return True, result, False

    def _terminate_process(self) -> None:
        process = self._process
        if process is None or process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=WORKER_TERMINATE_TIMEOUT_SECONDS)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=WORKER_KILL_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            pass

    def close(self) -> None:
        if self._closed:
            self._close_pipes()
            return
        self._closed = True
        process = self._process
        if process is not None and process.poll() is None:
            try:
                self._send({"type": "close"})
                process.wait(timeout=WORKER_TERMINATE_TIMEOUT_SECONDS)
            except (BrokenPipeError, EOFError, OSError, subprocess.TimeoutExpired):
                self._terminate_process()
        self._close_pipes()

    def _close_pipes(self) -> None:
        if self._selector is not None:
            try:
                self._selector.close()
            except Exception:
                pass
            self._selector = None
        if self._process is not None:
            for stream in (self._process.stdin, self._process.stdout):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass
