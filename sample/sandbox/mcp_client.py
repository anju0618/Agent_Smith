"""Synchronous facade over the official `mcp` SDK's async client (Section 4.2).

The sandbox needs plain, synchronous Python functions in its exec() namespace
- `result = search_code("foo")` - but the `mcp` package's ClientSession is
asyncio-based. This runs one persistent event loop in a background thread and
bridges every call across it with run_coroutine_threadsafe, so the rest of the
codebase never has to think about asyncio.

Supports both required transports (Section 4.2): stdio (spawns the MCP server
as a subprocess) and streamable HTTP (connects to an already-running server).
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import shlex
import threading
from contextlib import AsyncExitStack
from typing import Any, Callable, Dict, List, Optional

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import Tool

# Generous but finite: a legitimate tool call can run a whole test suite
# (mcp_tools_swebench.py's run_tests()) or a long shell command, but an MCP
# server that has died or deadlocked must not be able to hang the sandbox
# (and, in turn, agent_mbpp.py/agent_swebench.py's cleanup/solution.json
# write) forever. Connection establishment and shutdown have shorter bounds so
# a dead server cannot block startup or container cleanup indefinitely.
CONNECT_TIMEOUT_SECONDS = 30.0
CALL_TOOL_TIMEOUT_SECONDS = 300.0
CLOSE_TIMEOUT_SECONDS = 10.0


class MCPToolProxy:
    """Connects to one MCP server and exposes its tools as synchronous callables.

    Section 4.2: "the system will be tested with an unknown MCP server" - this
    proxy never hardcodes tool names, it discovers them from list_tools() and
    builds wrappers dynamically, so it works with any compliant MCP server.
    """

    def __init__(
        self,
        stdio_command: Optional[str] = None,
        http_url: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        connect_timeout: float = CONNECT_TIMEOUT_SECONDS,
    ) -> None:
        if bool(stdio_command) == bool(http_url):
            raise ValueError("Provide exactly one of stdio_command or http_url")
        if connect_timeout <= 0:
            raise ValueError("connect_timeout must be positive")

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_event_loop,
            name="agent-smith-mcp-loop",
            daemon=True,
        )
        self._connection_ready = threading.Event()
        self._owner_stopped = threading.Event()
        self._close_requested = threading.Event()
        self._cancel_requested = threading.Event()
        self._connection_error: Optional[BaseException] = None
        self._owner_task: Optional[asyncio.Task[Any]] = None
        self._connection_args = (stdio_command, http_url, env)
        self._closed = False
        self.session: Optional[ClientSession] = None
        self.tools: List[Tool] = []
        self._thread.start()

        try:
            ready = self._connection_ready.wait(timeout=connect_timeout)
        except BaseException:
            self._stop_owner(graceful=False)
            raise

        if not ready:
            self._stop_owner(graceful=False)
            raise TimeoutError(f"MCP connection timed out after {connect_timeout}s")
        if self._connection_error is not None:
            error = self._connection_error
            self._stop_owner(graceful=False)
            raise error

    def _run_event_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._owner_task = self._loop.create_task(
            self._connection_owner(*self._connection_args)
        )
        self._owner_task.add_done_callback(self._owner_completed)
        self._loop.run_forever()

    def _owner_completed(self, task: asyncio.Task[Any]) -> None:
        try:
            task.result()
        except BaseException as exc:
            if not self._connection_ready.is_set():
                self._connection_error = exc
                self._connection_ready.set()
        finally:
            self._owner_stopped.set()
            self._loop.stop()

    def _run(self, coro: Any, timeout: Optional[float] = None) -> Any:
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise

    async def _connection_owner(
        self, stdio_command: Optional[str], http_url: Optional[str], env: Optional[Dict[str, str]]
    ) -> None:
        """Own all transport contexts in one task until close().

        AnyIO transport contexts contain task-group cancel scopes which must be
        exited by the same task that entered them. Keeping this owner coroutine
        alive avoids cross-task AsyncExitStack teardown failures.
        """
        owner_task = asyncio.current_task()

        async def cancel_when_requested() -> None:
            while not self._cancel_requested.is_set():
                await asyncio.sleep(0.05)
            if owner_task is not None:
                owner_task.cancel()

        cancel_monitor = asyncio.create_task(cancel_when_requested())
        try:
            try:
                async with AsyncExitStack() as exit_stack:
                    if stdio_command:
                        parts = shlex.split(stdio_command)
                        params = StdioServerParameters(command=parts[0], args=parts[1:], env=env)
                        read, write = await exit_stack.enter_async_context(stdio_client(params))
                    else:
                        assert http_url is not None
                        read, write, _ = await exit_stack.enter_async_context(
                            streamablehttp_client(http_url)
                        )

                    self.session = await exit_stack.enter_async_context(ClientSession(read, write))
                    await self.session.initialize()
                    result = await self.session.list_tools()
                    self.tools = list(result.tools)
                    self._connection_ready.set()
                    while not self._close_requested.is_set():
                        await asyncio.sleep(0.05)
            except BaseException as exc:
                if not self._connection_ready.is_set():
                    self._connection_error = exc
                    self._connection_ready.set()
        finally:
            cancel_monitor.cancel()
            try:
                await cancel_monitor
            except asyncio.CancelledError:
                pass
            self.session = None
            if not self._connection_ready.is_set():
                self._connection_ready.set()

    def _stop_owner(self, graceful: bool) -> None:
        if graceful:
            self._close_requested.set()
        else:
            self._cancel_requested.set()

        wait_timeout = CLOSE_TIMEOUT_SECONDS if graceful else 1.0
        if not self._owner_stopped.wait(timeout=wait_timeout):
            self._cancel_requested.set()
            if self._owner_task is not None and self._loop.is_running():
                self._loop.call_soon_threadsafe(self._owner_task.cancel)
            self._owner_stopped.wait(timeout=1.0)

        if self._thread.is_alive() and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5.0)
        if not self._thread.is_alive() and not self._loop.is_closed():
            self._loop.close()

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> str:
        """Synchronously invoke one MCP tool and return its text content.

        Bounded by CALL_TOOL_TIMEOUT_SECONDS so a dead/hung MCP server becomes
        an explicit Observation instead of blocking the sandbox (and the
        agent loop) indefinitely - defense in depth alongside the sandbox's
        own SIGALRM-based execution timeout, which this call normally runs
        under too, but which this module cannot assume will always interrupt
        a thread-lock wait the same way it interrupts pure-Python code.
        """
        session = self.session
        if session is None:
            return "[Error] MCP session is not connected"
        try:
            result = self._run(session.call_tool(name, arguments), timeout=CALL_TOOL_TIMEOUT_SECONDS)
        except concurrent.futures.TimeoutError:
            return f"[Error] tool '{name}' timed out after {CALL_TOOL_TIMEOUT_SECONDS}s"
        parts = []
        for item in result.content:
            text = getattr(item, "text", None)
            parts.append(text if text is not None else str(item))
        text_result = "\n".join(parts)
        if getattr(result, "isError", False):
            return f"[Error] tool '{name}' failed: {text_result}"
        return text_result

    def build_namespace(self) -> Dict[str, Callable[..., str]]:
        """One synchronous wrapper function per discovered tool, ready to drop
        straight into the sandbox's exec() namespace (Section 4.2)."""
        namespace: Dict[str, Callable[..., str]] = {}
        for tool in self.tools:
            namespace[tool.name] = self._make_wrapper(tool)
        return namespace

    def _make_wrapper(self, tool: Tool) -> Callable[..., str]:
        """Build a wrapper that accepts a tool's arguments either positionally
        or by keyword, like a normal Python function - the subject's own
        example (Section 3.1) calls tools positionally
        (``result = search_code("validate_email")``), and our system prompt's
        "always use keyword arguments" guidance is advice to the LLM, not a
        constraint an LLM is guaranteed to follow. Positional arguments are
        mapped to parameter names using the MCP tool schema's declared
        property order, which matches the underlying function's real
        parameter order for a FastMCP-based server (ours and, in practice,
        any other compliant one)."""
        name = tool.name
        param_names = list((tool.inputSchema or {}).get("properties", {}).keys())

        def wrapper(*args: Any, **kwargs: Any) -> str:
            if len(args) > len(param_names):
                return (
                    f"[Error] {name}() takes at most {len(param_names)} positional "
                    f"arguments but {len(args)} were given"
                )
            arguments = dict(zip(param_names, args))
            duplicates = arguments.keys() & kwargs.keys()
            if duplicates:
                return f"[Error] {name}() got multiple values for {sorted(duplicates)}"
            arguments.update(kwargs)
            return self.call_tool(name, arguments)

        wrapper.__name__ = name
        return wrapper

    def manual_text(self) -> str:
        """Render the connected server's tools as documentation for the system
        prompt (Section 4.2 - the sandbox manual must be generated dynamically
        from the connected MCP server's tool schemas)."""
        if not self.tools:
            return "(no MCP tools are currently connected)"
        lines = []
        for tool in self.tools:
            schema = tool.inputSchema or {}
            properties = schema.get("properties", {})
            required = set(schema.get("required", []))
            params = []
            for pname, pschema in properties.items():
                ptype = pschema.get("type", "any")
                marker = "" if pname in required else "?"
                params.append(f"{pname}{marker}: {ptype}")
            signature = ", ".join(params)
            description = (tool.description or "").strip()
            lines.append(f"- {tool.name}({signature})\n    {description}")
        return "\n".join(lines)

    def close(self) -> None:
        """Best-effort shutdown, bounded by CLOSE_TIMEOUT_SECONDS - callers
        (agent_mbpp.py/agent_swebench.py's `finally` blocks) rely on this
        returning promptly even if the MCP server already died or is stuck,
        since agent_swebench.py's `container.cleanup()` is the very next
        line after this call and must not be starved by it."""
        if self._closed:
            return
        self._closed = True
        self._stop_owner(graceful=True)
