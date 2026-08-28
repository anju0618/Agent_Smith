import pytest

from sandbox import FinalAnswer, Sandbox


def test_authorized_import_and_output() -> None:
    sb = Sandbox()
    assert sb.run("import math\nprint(math.sqrt(16))") == "4.0\n"


def test_state_persists_across_calls() -> None:
    sb = Sandbox()
    sb.run("x = 10")
    assert sb.run("print(x * 2)") == "20\n"


def test_unauthorized_import_via_statement_is_blocked() -> None:
    sb = Sandbox()
    result = sb.run("import os")
    assert "SandboxViolation" in result
    assert "os" in result


def test_unauthorized_import_via_dunder_import_is_blocked() -> None:
    sb = Sandbox()
    result = sb.run('__import__("os")')
    assert "SandboxViolation" in result


def test_final_answer_raises_with_the_given_value() -> None:
    sb = Sandbox()
    with pytest.raises(FinalAnswer) as exc_info:
        sb.run("final_answer(42)")
    assert exc_info.value.answer == 42


def test_runtime_error_is_reported_not_raised() -> None:
    sb = Sandbox()
    result = sb.run("1 / 0")
    assert "[Error] ZeroDivisionError" in result


def test_syntax_error_is_reported_not_raised() -> None:
    sb = Sandbox()
    result = sb.run("def broken(:")
    assert "[SyntaxError]" in result


def test_empty_code_is_reported() -> None:
    sb = Sandbox()
    assert "[Error]" in sb.run("")
