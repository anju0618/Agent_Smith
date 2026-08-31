"""Tests for mcp_tools_mbpp.py's run_tests tool, called directly (the @mcp.tool()
decorator leaves the underlying function callable - no MCP transport needed)."""
import json

import pytest

from mcp_tools_mbpp import run_tests


def test_run_tests_uses_task_test_imports_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """Some MBPP tasks' test_list needs an import the candidate solution has
    no reason to include itself (e.g. `math.isclose` on a task whose own
    solution never touches `math`) - agent_mbpp.py passes those through this
    env var so run_tests() doesn't NameError instead of silently depending on
    the LLM happening to add the same import for its own unrelated reasons."""
    monkeypatch.setenv("AGENT_SMITH_TEST_IMPORTS", json.dumps(["import math"]))
    code = "def volume_sphere(r):\n    return (4 / 3) * 3.141592653589793 * r ** 3\n"
    result = json.loads(run_tests(code, ["assert math.isclose(volume_sphere(1), 4.1887902047863905)"]))
    assert result["success"] is True


def test_run_tests_without_test_imports_env_var_is_unaffected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENT_SMITH_TEST_IMPORTS", raising=False)
    code = "def add(a, b):\n    return a + b\n"
    result = json.loads(run_tests(code, ["assert add(2, 3) == 5"]))
    assert result["success"] is True


def test_run_tests_all_pass() -> None:
    code = "def add(a, b):\n    return a + b\n"
    result = json.loads(run_tests(code, ["assert add(2, 3) == 5", "assert add(-1, 1) == 0"]))
    assert result["success"] is True


def test_run_tests_failure_reports_output() -> None:
    code = "def add(a, b):\n    return a - b\n"
    result = json.loads(run_tests(code, ["assert add(2, 3) == 5"]))
    assert result["success"] is False
    assert "AssertionError" in result["output"] or "Error" in result["output"]


def test_run_tests_syntax_error_in_candidate() -> None:
    code = "def add(a, b)\n    return a + b\n"
    result = json.loads(run_tests(code, ["assert add(2, 3) == 5"]))
    assert result["success"] is False


def test_run_tests_infinite_loop_times_out() -> None:
    code = "def add(a, b):\n    while True:\n        pass\n"
    result = json.loads(run_tests(code, ["assert add(2, 3) == 5"]))
    assert result["success"] is False
    assert "timed out" in result["output"]
