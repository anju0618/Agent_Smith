"""Tests for sandbox/executor.py (Section 4.2's security constraints).

Memory-limit enforcement is tested through the isolated worker (see
test_memory_limit_is_enforced), so lowering RLIMIT_AS cannot affect the pytest
process or later tests in this session.
"""
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import List, Optional

import pytest

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


def test_authorized_nested_and_from_imports_are_allowed() -> None:
    sandbox = _sandbox()
    output = sandbox.run(
        "import collections.abc\nfrom math import sqrt\n"
        "print(isinstance([], collections.abc.Iterable), sqrt(9))"
    )
    assert output.strip() == "True 3.0"


def test_unauthorized_import_is_blocked() -> None:
    sandbox = _sandbox()
    output = sandbox.run("import os\nprint(os.getcwd())")
    assert "[SandboxViolation]" in output


def test_dynamic_import_bypass_is_blocked() -> None:
    sandbox = _sandbox()
    output = sandbox.run("m = __import__('os')\nprint(m)")
    assert "[SandboxViolation]" in output


def test_private_module_reference_escape_is_blocked() -> None:
    sandbox = _sandbox(authorized_imports=DEFAULT_AUTHORIZED_IMPORTS)
    output = sandbox.run("import random\nprint(random._os.listdir('/'))")
    assert "[SandboxViolation]" in output
    assert "_os" in output


def test_public_unauthorized_nested_module_is_blocked() -> None:
    sandbox = _sandbox(authorized_imports=DEFAULT_AUTHORIZED_IMPORTS)
    output = sandbox.run("import typing\nprint(typing.sys.modules)")
    assert "[SandboxViolation]" in output
    assert "unauthorized module 'sys'" in output


def test_operator_attrgetter_private_attribute_bypass_is_blocked() -> None:
    sandbox = _sandbox(authorized_imports=DEFAULT_AUTHORIZED_IMPORTS)
    output = sandbox.run(
        "import operator\nimport random\nprint(operator.attrgetter('_os')(random))"
    )
    assert "[SandboxViolation]" in output
    assert "operator.attrgetter" in output


def test_vars_builtin_and_star_import_are_blocked() -> None:
    sandbox = _sandbox(authorized_imports=DEFAULT_AUTHORIZED_IMPORTS)
    assert "NameError" in sandbox.run("print(vars(object))")
    assert "[SandboxViolation]" in sandbox.run("from random import *")


def test_subclasses_escape_via_dot_attribute_is_blocked() -> None:
    """Classic in-process sandbox escape: walk already-loaded classes via
    __subclasses__ to find one whose __init__.__globals__ holds the real,
    unrestricted builtins - completely bypassing the restricted __import__."""
    sandbox = _sandbox(authorized_imports=["math"])
    code = textwrap.dedent(
        """
        for cls in ().__class__.__bases__[0].__subclasses__():
            try:
                g = cls.__init__.__globals__
            except Exception:
                continue
            if "__builtins__" in g:
                b = g["__builtins__"]
                real_import = b["__import__"] if isinstance(b, dict) else b.__import__
                real_import("os")
                print("ESCAPED")
                break
        """
    )
    output = sandbox.run(code)
    assert "[SandboxViolation]" in output
    assert "__subclasses__" in output
    assert "ESCAPED" not in output


def test_format_attribute_escape_is_blocked() -> None:
    sandbox = _sandbox(authorized_imports=["math"])
    output = sandbox.run('print("{0.__class__}".format(1))')
    assert "[SandboxViolation]" in output


def test_formatter_field_escape_is_blocked() -> None:
    sandbox = _sandbox(authorized_imports=["string"])
    output = sandbox.run(
        "import string\n"
        "formatter = string.Formatter()\n"
        "formatter.get_field('0.__class__', ((),), {})"
    )
    assert "[SandboxViolation]" in output


def test_isolated_worker_cannot_see_host_root_files() -> None:
    sandbox = _sandbox(authorized_imports=["os", "posixpath"])
    try:
        output = sandbox.run("import os\nprint(os.path.exists('/etc/passwd'))")
    finally:
        sandbox.close()
    assert output.strip() == "False"


def test_subclasses_escape_via_getattr_is_blocked() -> None:
    sandbox = _sandbox(authorized_imports=["math"])
    output = sandbox.run("getattr(object, '__subclasses__')()")
    assert "[SandboxViolation]" in output


def test_subclasses_escape_via_dynamically_built_name_is_blocked() -> None:
    """getattr's guard must check the resolved name, not the literal source
    text, since the name can be built at runtime to dodge a naive string scan."""
    sandbox = _sandbox(authorized_imports=["math"])
    output = sandbox.run("name = '__sub' + 'classes__'\ngetattr(object, name)()")
    assert "[SandboxViolation]" in output


def test_globals_attribute_access_is_blocked() -> None:
    sandbox = _sandbox(authorized_imports=["math"])
    output = sandbox.run("def f(): pass\nprint(f.__globals__)")
    assert "[SandboxViolation]" in output


def test_setattr_on_dangerous_dunder_is_blocked() -> None:
    sandbox = _sandbox(authorized_imports=["math"])
    output = sandbox.run("class Foo: pass\nclass Bar: pass\nsetattr(Foo, '__bases__', (Bar,))")
    assert "[SandboxViolation]" in output


def test_common_dunders_still_work_for_legitimate_code() -> None:
    """The dunder blocklist must not break ordinary operator overloading,
    iteration, or repr - only the introspection/escape-relevant ones."""
    sandbox = _sandbox(authorized_imports=["math"])
    code = textwrap.dedent(
        """
        class Point:
            def __init__(self, x, y):
                self.x, self.y = x, y
            def __repr__(self):
                return f"Point({self.x}, {self.y})"
            def __add__(self, other):
                return Point(self.x + other.x, self.y + other.y)
            def __eq__(self, other):
                return (self.x, self.y) == (other.x, other.y)

        p = Point(1, 2) + Point(3, 4)
        print(repr(p))
        print(p == Point(4, 6))
        print(len([1, 2, 3]))
        print(getattr(p, "x"))
        """
    )
    output = sandbox.run(code)
    assert "[SandboxViolation]" not in output
    assert "Point(4, 6)" in output
    assert "True" in output


def test_reserved_namespace_names_cannot_override_sandbox_controls() -> None:
    config = SandboxConfig(authorized_imports=[], allowed_directories=[])
    with pytest.raises(ValueError, match="final_answer"):
        Sandbox(
            config,
            extra_namespace={"final_answer": lambda answer: answer},
            apply_process_memory_limit=False,
        )


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


def test_relative_allowed_directory_is_mounted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path.parent)
    sandbox = _sandbox(allowed_directories=[tmp_path.name])
    target = str(tmp_path / "relative.txt")
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
