"""MCP server exposing the MBPP tools (Section 4.3.2) over stdio or streamable HTTP.

    python mcp_tools_mbpp.py            # stdio transport (default)
    python mcp_tools_mbpp.py --http 8000  # streamable HTTP transport on port 8000

Kept at the repository root per Section 4.2's requirement for MCP tool files.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import multiprocessing
import os
import traceback
from typing import List, Tuple

from mcp.server.fastmcp import FastMCP

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


def _run_in_subprocess(code: str, queue: "multiprocessing.Queue") -> None:
    output = io.StringIO()
    try:
        with contextlib.redirect_stdout(output):
            exec(compile(code, "<mbpp-test>", "exec"), {"__name__": "__main__"})
        queue.put((True, output.getvalue()))
    except Exception:
        queue.put((False, output.getvalue() + "\n" + traceback.format_exc()))


def _execute_with_timeout(code: str, timeout: float) -> Tuple[bool, str]:
    # "spawn", not the platform default "fork": this server runs its own asyncio
    # event loop (anyio, for the stdio/HTTP transport), and forking a process with
    # live background threads risks the child deadlocking on a lock a thread held
    # at fork time that no longer exists to release it. spawn starts a clean
    # interpreter instead. Found via a live smoke test - see README.md.
    ctx = multiprocessing.get_context("spawn")
    queue: "multiprocessing.Queue" = ctx.Queue()
    process = ctx.Process(target=_run_in_subprocess, args=(code, queue))
    process.start()
    process.join(timeout=timeout)

    if process.is_alive():
        process.terminate()
        process.join(timeout=2.0)
        if process.is_alive():
            process.kill()
            process.join()
        return False, f"Execution timed out after {timeout}s"

    if queue.empty():
        return False, f"Process exited with code {process.exitcode} and produced no output"

    success, output = queue.get()
    return bool(success), str(output)


@mcp.tool()
def run_tests(code: str, test_list: List[str]) -> str:
    """Run a candidate MBPP solution against the given test assertions.

    Args:
        code: The candidate Python solution (a full function definition).
        test_list: Assertion strings to execute against `code`.

    Returns:
        A JSON string {"success": bool, "output": str} - success is True only
        if every assertion passed. Execution happens in a throwaway subprocess
        so a broken candidate solution can never crash this tool server.
    """
    imports_prefix = "\n".join(_test_imports())
    full_code = (imports_prefix + "\n" if imports_prefix else "") + code + "\n" + "\n".join(test_list)
    success, output = _execute_with_timeout(full_code, timeout=10.0)
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
