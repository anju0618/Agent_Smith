"""Secure, configurable Python execution sandbox (Section 4.2).

Design choices (the project explicitly calls out that isolation is a genuine
trade-off with no single right answer - these are documented here rather than
just implemented silently):

- In-process execution with a restricted globals/builtins dict, rather than a
  fresh OS process per snippet. This keeps MCP tool wrappers (built once,
  holding live connections) directly callable from sandboxed code with no
  cross-process RPC bridge, and keeps variables naturally persistent between
  agent steps in self.namespace (Section 3.1's "persistent variables" point).
- Execution timeout is enforced with SIGALRM (Unix only): CPython checks for
  pending signals between bytecode instructions, so this reliably interrupts
  both pure-Python infinite loops and blocking stdlib calls (sleep, I/O). The
  one case it cannot preempt is a C extension that blocks without releasing
  the GIL - a known limitation of in-process signal-based timeouts, accepted
  here for architectural simplicity. A fully hardened deployment could instead
  run each snippet in its own subprocess and SIGTERM/SIGKILL it - the approach
  Section 6.1 describes for the *outer* agent-process timeout, which
  moulinette itself enforces via its run-agent command.
- Memory is capped with resource.setrlimit(RLIMIT_AS) once per process: code
  that exceeds it gets a normal, catchable MemoryError instead of being
  OS-killed, so the sandbox can report a clean [MemoryLimitExceeded]
  observation instead of the whole agent process just vanishing.
"""
from __future__ import annotations

import ast
import builtins
import contextlib
import fnmatch
import io
import os
import signal
from collections.abc import Callable
from types import ModuleType
from typing import Any, Dict, Optional

from models import SandboxConfig

try:
    import resource
except ImportError:  # pragma: no cover - resource is Unix-only
    resource = None  # type: ignore[assignment]

DEFAULT_AUTHORIZED_IMPORTS = [
    "math", "math.*",
    "collections", "collections.*",
    "itertools", "re", "json",
    "typing", "typing.*",
    "functools", "operator",
    "heapq", "bisect", "copy",
    "string", "random",
    "datetime", "datetime.*",
    "array", "cmath",
]

DEFAULT_ALLOWED_DIRECTORIES = ["/testbed", "/tmp/agent"]

# Builtins that would let sandboxed code escape the restrictions below.
_UNSAFE_BUILTINS = {
    "eval", "exec", "compile", "input", "breakpoint",
    "help", "exit", "quit", "__import__", "open",
}


class SandboxViolation(Exception):
    """Raised when sandboxed code violates an import or filesystem restriction."""


class SandboxTimeoutError(Exception):
    """Raised internally when a sandbox.run() call exceeds max_execution_time_seconds."""


class FinalAnswer(Exception):
    """Raised by the injected final_answer() builtin to signal task completion.

    Carries the submitted answer in `.answer`. The orchestrator catches this to
    end the agent loop and build SolutionOutput - it must NOT be swallowed by
    Sandbox.run()'s generic error handling (Section 4.2's exception propagation
    requirement covers KeyboardInterrupt/SystemExit explicitly; FinalAnswer is
    the sandbox's own equivalent control-flow signal and gets the same treatment).
    """

    def __init__(self, answer: Any) -> None:
        super().__init__(answer)
        self.answer = answer


def _is_authorized(module_name: str, authorized: list) -> bool:
    return any(fnmatch.fnmatch(module_name, pattern) for pattern in authorized)


def check_imports(tree: ast.AST, authorized: list) -> None:
    """Static check: reject any Import/ImportFrom node outside the allowlist.

    This alone can be bypassed by calling ``__import__("os")`` as a plain
    function rather than writing an import statement, which is why
    _make_restricted_import below re-checks at call time regardless of how
    the sandboxed code reached it.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if not _is_authorized(alias.name, authorized):
                    raise SandboxViolation(f"import of '{alias.name}' is not permitted")
        elif isinstance(node, ast.ImportFrom):
            if node.module is None or not _is_authorized(node.module, authorized):
                raise SandboxViolation(f"import of '{node.module}' is not permitted")


def _make_restricted_import(authorized: list) -> Callable[..., ModuleType]:
    real_import = builtins.__import__

    def restricted_import(
        name: str,
        globals: Optional[dict] = None,
        locals: Optional[dict] = None,
        fromlist: tuple = (),
        level: int = 0,
    ) -> ModuleType:
        if not _is_authorized(name, authorized):
            raise SandboxViolation(f"import of '{name}' is not permitted")
        return real_import(name, globals, locals, fromlist, level)

    return restricted_import


def _make_restricted_open(allowed_directories: list) -> Callable[..., Any]:
    real_open = builtins.open
    resolved_allowed = [os.path.realpath(d) for d in allowed_directories]

    def restricted_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if isinstance(file, (str, bytes, os.PathLike)):
            target = os.path.realpath(os.fspath(file))
            allowed = any(target == base or target.startswith(base + os.sep) for base in resolved_allowed)
            if not allowed:
                raise SandboxViolation(
                    f"path '{file!r}' is outside the allowed directories {allowed_directories}"
                )
        return real_open(file, mode, *args, **kwargs)

    return restricted_open


def final_answer(answer: Any) -> None:
    """Injected into every sandbox namespace - NOT an MCP tool (Section 4.2)."""
    raise FinalAnswer(answer)


def _alarm_handler(signum: int, frame: Any) -> None:
    raise SandboxTimeoutError()


class Sandbox:
    """Executes untrusted, LLM-generated Python under the configured restrictions."""

    def __init__(
        self,
        config: Optional[SandboxConfig] = None,
        extra_namespace: Optional[Dict[str, Callable]] = None,
        apply_process_memory_limit: bool = True,
    ) -> None:
        if config is None:
            config = SandboxConfig(
                authorized_imports=DEFAULT_AUTHORIZED_IMPORTS,
                allowed_directories=DEFAULT_ALLOWED_DIRECTORIES,
            )
        self.config = config
        if apply_process_memory_limit:
            self._apply_memory_limit()
        self.namespace = self._build_namespace(extra_namespace or {})

    def _apply_memory_limit(self) -> None:
        """Cap this process's address space so runaway allocations raise
        MemoryError instead of triggering the OS OOM killer.

        This is applied once, for the whole process's lifetime - a documented
        trade-off of the in-process isolation approach (see module docstring).
        Callers that construct many Sandboxes in one process (e.g. tests) should
        pass apply_process_memory_limit=False and exercise this behavior in an
        isolated subprocess instead, since RLIMIT_AS can only be lowered, never
        raised back up, for the lifetime of a process.
        """
        if resource is None or not hasattr(resource, "RLIMIT_AS"):
            return
        limit_bytes = self.config.max_memory_mb * 1024 * 1024
        try:
            soft, hard = resource.getrlimit(resource.RLIMIT_AS)
            new_hard = hard if hard != resource.RLIM_INFINITY and hard < limit_bytes else limit_bytes
            resource.setrlimit(resource.RLIMIT_AS, (limit_bytes, new_hard))
        except (ValueError, OSError):
            pass  # best-effort: some sandboxed CI environments forbid lowering limits further

    def _build_namespace(self, extra_namespace: Dict[str, Callable]) -> dict:
        restricted_builtins = {
            name: value for name, value in vars(builtins).items() if name not in _UNSAFE_BUILTINS
        }
        restricted_builtins["__import__"] = _make_restricted_import(self.config.authorized_imports)
        restricted_builtins["open"] = _make_restricted_open(self.config.allowed_directories)

        namespace: dict = {"__builtins__": restricted_builtins, "final_answer": final_answer}
        namespace.update(extra_namespace)  # MCP tool wrappers, if any (Section 4.2)
        return namespace

    def run(self, code: str) -> str:
        """Execute one snippet and return its captured stdout, or an explicit error string.

        Never raises for ordinary code errors - those come back as
        "[ErrorKind] ..." text so the agent loop can feed them to the LLM as an
        Observation (Section 4.1's mandatory explicit-feedback requirement).
        FinalAnswer, KeyboardInterrupt, and SystemExit are the only things
        propagated to the caller.
        """
        if not code.strip():
            return "[NoCodeBlock] The submitted code was empty."

        try:
            tree = ast.parse(code)
        except SyntaxError as exc:
            return f"[SyntaxError] {exc}"

        try:
            check_imports(tree, self.config.authorized_imports)
        except SandboxViolation as exc:
            return f"[SandboxViolation] {exc}"

        compiled = compile(tree, "<agent>", "exec")
        output = io.StringIO()
        has_alarm = hasattr(signal, "SIGALRM")
        previous_handler = None
        if has_alarm:
            previous_handler = signal.signal(signal.SIGALRM, _alarm_handler)
            signal.alarm(self.config.max_execution_time_seconds)

        try:
            with contextlib.redirect_stdout(output):
                exec(compiled, self.namespace)
            return self._truncate(output.getvalue())
        except SandboxTimeoutError:
            partial = self._truncate(output.getvalue())
            return (
                f"[Timeout] Execution exceeded {self.config.max_execution_time_seconds}s "
                f"and was interrupted. Partial output before timeout:\n{partial}"
            )
        except MemoryError:
            return (
                f"[MemoryLimitExceeded] Execution exceeded "
                f"{self.config.max_memory_mb}MB and was interrupted."
            )
        except SandboxViolation as exc:
            return f"[SandboxViolation] {exc}"
        except FinalAnswer:
            raise
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:  # noqa: BLE001 - intentional: all other errors become Observations
            return f"[Error] {type(exc).__name__}: {exc}"
        finally:
            if has_alarm:
                signal.alarm(0)
                signal.signal(signal.SIGALRM, previous_handler)

    def _truncate(self, text: str) -> str:
        limit = self.config.max_output_chars
        if len(text) <= limit:
            return text
        omitted = len(text) - limit
        return (
            text[:limit]
            + f"\n[TruncatedOutput] {omitted} additional characters were cut off "
            f"(output limit: {limit} chars)."
        )


if __name__ == "__main__":
    sandbox = Sandbox()
    demo_code = "import math\ndef test():\n    return sum([1, 2, 3, 4]) / 4\nprint(test())"
    print(sandbox.run(demo_code))
