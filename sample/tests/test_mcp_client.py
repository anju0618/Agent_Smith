"""Integration tests for MCPToolProxy (Section 4.2) against a real MCP server
subprocess (mcp_tools_mbpp.py over stdio) - no network or API keys needed.
"""
import json
import sys
from pathlib import Path
from typing import Iterator

import pytest

from sandbox.mcp_client import MCPToolProxy

MCP_TOOLS_MBPP = Path(__file__).resolve().parent.parent / "mcp_tools_mbpp.py"


@pytest.fixture()
def proxy() -> Iterator[MCPToolProxy]:
    proxy = MCPToolProxy(stdio_command=f"{sys.executable} {MCP_TOOLS_MBPP}")
    try:
        yield proxy
    finally:
        proxy.close()


def test_tool_call_accepts_keyword_arguments(proxy: MCPToolProxy) -> None:
    run_tests = proxy.build_namespace()["run_tests"]
    code = "def add(a, b):\n    return a + b"
    result = json.loads(run_tests(code=code, test_list=["assert add(1, 2) == 3"]))
    assert result["success"] is True


def test_tool_call_accepts_positional_arguments(proxy: MCPToolProxy) -> None:
    """The subject's own example (Section 3.1) calls tools positionally -
    e.g. ``result = search_code("validate_email")`` - so wrappers must too."""
    run_tests = proxy.build_namespace()["run_tests"]
    result = json.loads(run_tests("def add(a, b):\n    return a + b", ["assert add(1, 2) == 3"]))
    assert result["success"] is True


def test_tool_call_accepts_mixed_positional_and_keyword_arguments(proxy: MCPToolProxy) -> None:
    run_tests = proxy.build_namespace()["run_tests"]
    result = json.loads(run_tests("def add(a, b):\n    return a + b", test_list=["assert add(1, 2) == 3"]))
    assert result["success"] is True


def test_tool_call_rejects_too_many_positional_arguments(proxy: MCPToolProxy) -> None:
    run_tests = proxy.build_namespace()["run_tests"]
    result = run_tests("code", ["tests"], "unexpected extra arg")
    assert "[Error]" in result
    assert "positional" in result


def test_tool_call_rejects_duplicate_argument(proxy: MCPToolProxy) -> None:
    run_tests = proxy.build_namespace()["run_tests"]
    result = run_tests("def f(): pass", code="def f(): pass")
    assert "[Error]" in result
    assert "multiple values" in result


def test_manual_text_lists_discovered_tools(proxy: MCPToolProxy) -> None:
    manual = proxy.manual_text()
    assert "run_tests" in manual
    assert "code" in manual
    assert "test_list" in manual
