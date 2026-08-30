"""Tests for sandbox/executor.py (Section 4.2's security constraints).

Memory-limit enforcement is tested in a subprocess (see
test_memory_limit_is_enforced) because Sandbox._apply_memory_limit() lowers
RLIMIT_AS for the whole calling process and, once lowered, a process can never
raise it back up - applying it directly to the pytest process itself would
risk breaking every later test in this session.
"""
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import List, Optional

from models import SandboxConfig
from sandbox.executor import DEFAULT_AUTHORIZED_IMPORTS, FinalAnswer, Sandbox


def _sandbox(
    allowed_directories: Optional[List[str]] = None, authorized_imports: Optional[List[str]] = None
) -> Sandbox:
    imports = authorized_imports if authorized_imports is not None else DEFAULT_AUTHORIZED_IMPORTS
    directories = allowed_directories if allowed_directories is not None else []
    config = SandboxConfig(authorized_imports=imports, allowed_directories=directories)
    return Sandbox(config, apply_process_memory_limit=False)


def test_authorized_import_is_allowed() -> None:
    sandbox = _sandbox()
    output = sandbox.run("import math\nprint(math.sqrt(4))")
    assert output.strip() == "2.0"


def test_unauthorized_import_is_blocked() -> None:
    sandbox = _sandbox()
    output = sandbox.run("import os\nprint(os.getcwd())")
    assert "[SandboxViolation]" in output


def test_dynamic_import_bypass_is_blocked() -> None:
    sandbox = _sandbox()
    output = sandbox.run("m = __import__('os')\nprint(m)")
    assert "[SandboxViolation]" in output


def test_variables_persist_between_calls() -> None:
    sandbox = _sandbox()
    sandbox.run("x = 41")
    output = sandbox.run("print(x + 1)")
    assert output.strip() == "42"


def test_final_answer_raises_and_carries_value() -> None:
    sandbox = _sandbox()
    try:
        sandbox.run("final_answer('done')")
    except FinalAnswer as fa:
        assert fa.answer == "done"
    else:
        raise AssertionError("expected FinalAnswer to be raised")


def test_syntax_error_is_explicit() -> None:
    sandbox = _sandbox()
    output = sandbox.run("def broken(:\n    pass")
    assert output.startswith("[SyntaxError]")


def test_empty_code_is_explicit() -> None:
    sandbox = _sandbox()
    assert sandbox.run("   ") == "[NoCodeBlock] The submitted code was empty."


def test_filesystem_restriction_blocks_outside_paths(tmp_path: Path) -> None:
    outside_file = str(tmp_path / "secret.txt")
    sandbox = _sandbox(allowed_directories=["/tmp/agent-smith-allowed"])
    output = sandbox.run(f"open({outside_file!r}, 'w')")
    assert "[SandboxViolation]" in output


def test_filesystem_restriction_allows_configured_directory(tmp_path: Path) -> None:
    sandbox = _sandbox(allowed_directories=[str(tmp_path)])
    target = str(tmp_path / "allowed.txt")
    output = sandbox.run(f"open({target!r}, 'w').write('hi')\nprint('ok')")
    assert output.strip() == "ok"


def test_output_is_truncated() -> None:
    config = SandboxConfig(authorized_imports=[], allowed_directories=[], max_output_chars=20)
    sandbox = Sandbox(config, apply_process_memory_limit=False)
    output = sandbox.run("print('x' * 100)")
    assert "[TruncatedOutput]" in output


def test_timeout_interrupts_infinite_loop() -> None:
    config = SandboxConfig(
        authorized_imports=[], allowed_directories=[], max_execution_time_seconds=1
    )
    sandbox = Sandbox(config, apply_process_memory_limit=False)
    output = sandbox.run("while True:\n    pass")
    assert output.startswith("[Timeout]")


def test_memory_limit_is_enforced() -> None:
    """Runs in a subprocess so lowering RLIMIT_AS cannot affect the test runner."""
    script = textwrap.dedent(
        """
        from models import SandboxConfig
        from sandbox.executor import Sandbox

        config = SandboxConfig(authorized_imports=[], allowed_directories=[], max_memory_mb=32)
        sandbox = Sandbox(config)
        print(sandbox.run("data = bytearray(500 * 1024 * 1024)"))
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert "[MemoryLimitExceeded]" in result.stdout
