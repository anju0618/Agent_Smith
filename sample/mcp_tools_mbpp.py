"""MCP server exposing the MBPP tools (Section 4.3.2) over stdio or streamable HTTP.

    python mcp_tools_mbpp.py            # stdio transport (default)
    python mcp_tools_mbpp.py --http 8000  # streamable HTTP transport on port 8000

Kept at the repository root per Section 4.2's requirement for MCP tool files.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
from typing import List

from mcp.server.fastmcp import FastMCP
from models import SandboxConfig
from sandbox.executor import DEFAULT_AUTHORIZED_IMPORTS, FinalAnswer, Sandbox

mcp = FastMCP("agent-smith-mbpp-tools")


def _test_imports() -> List[str]:
    """Imports the task's test_list needs but the candidate solution has no
    reason to include itself (e.g. `math` for `math.isclose(...)` assertions
    on a task whose own solution never touches `math`). agent_mbpp.py passes
    these through MBPPTaskInput.test_imports via this env var so run_tests()
    can guarantee they're present, rather than leaving it to chance whether
    the LLM's own code happens to need (and therefore import) the same
    module - which silently NameErrors on tasks where it doesn't."""
    raw = os.environ.get("AGENT_SMITH_TEST_IMPORTS")
    if not raw:
        return []
    try:
        imports = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return [line for line in imports if isinstance(line, str)]


@mcp.tool()
def run_tests(code: str, test_list: List[str]) -> str:
    """Run a candidate MBPP solution against the given test assertions.

    Args:
        code: The candidate Python solution (a full function definition).
        test_list: Assertion strings to execute against `code`.

    Returns:
        A JSON string {"success": bool, "output": str} - success is True only
        if every assertion passed. Candidate code runs in the same hardened,
        OS-isolated sandbox used by the agents, so it cannot access the MCP
        server's host filesystem or network.
    """
    imports_prefix = "\n".join(_test_imports())
    marker = f"__AGENT_SMITH_MBPP_PASS_{secrets.token_hex(16)}__"
    full_code = (
        (imports_prefix + "\n" if imports_prefix else "")
        + code
        + "\n"
        + "\n".join(test_list)
        + f"\nprint({marker!r})"
    )

    sandbox = None
    try:
        sandbox = Sandbox(
            SandboxConfig(
                authorized_imports=DEFAULT_AUTHORIZED_IMPORTS,
                allowed_directories=[],
                max_execution_time_seconds=10,
                max_memory_mb=256,
            )
        )
        try:
            output = sandbox.run(full_code)
        except (FinalAnswer, KeyboardInterrupt, SystemExit) as exc:
            output = f"[Error] {type(exc).__name__}: {exc}"
    except Exception as exc:  # noqa: BLE001 - MCP tool must return JSON on setup errors
        output = f"[Error] {type(exc).__name__}: {exc}"
    finally:
        if sandbox is not None:
            sandbox.close()

    if output.startswith("[Timeout]") and "timed out" not in output:
        output = output.replace("Execution exceeded 10s", "Execution timed out after 10s", 1)
    success = marker in output
    if success:
        output = output.replace(marker, "").rstrip()
    return json.dumps({"success": success, "output": output})


def main() -> None:
    parser = argparse.ArgumentParser(description="MBPP MCP tool server")
    parser.add_argument(
        "--http", type=int, default=None, help="Serve over streamable HTTP on this port instead of stdio"
    )
    args = parser.parse_args()

    if args.http:
        mcp.settings.port = args.http
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
