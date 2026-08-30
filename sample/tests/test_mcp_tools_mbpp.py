"""Tests for mcp_tools_mbpp.py's run_tests tool, called directly (the @mcp.tool()
decorator leaves the underlying function callable - no MCP transport needed)."""
import json

from mcp_tools_mbpp import run_tests


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
