"""MBPP用ツール(セクション4.3.2)をstdioまたはstreamable HTTP経由で公開するMCPサーバ。

    python mcp_tools_mbpp.py            # stdioトランスポート(デフォルト)
    python mcp_tools_mbpp.py --http 8000  # ポート8000でstreamable HTTPトランスポート

セクション4.2の「MCPツールファイルはリポジトリのルートに置くこと」という要件に従い
ルート直下に配置している。
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
    """タスクのtest_listが必要とするが、候補解答自体には含める理由がないimportの一覧を返す
    (例: 候補解答が`math`を一切使わないタスクで、`math.isclose(...)`というassertionが
    必要とする`math`)。agent_mbpp.pyがMBPPTaskInput.test_importsをこの環境変数経由で
    渡すことで、LLM自身のコードがたまたま同じモジュールを必要としてimportしているかどうかに
    運任せにするのではなく、run_tests()側でこれらのimportが確実に存在するようにできる
    - importに任せると、必要としないタスクでは静かにNameErrorになってしまう。"""
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
        code: Candidate Python solution (a complete function definition).
        test_list: Assertion strings to run against `code`.

    Returns:
        JSON string {"success": bool, "output": str} - success is True only
        if every assertion passed. Runs in the same OS-isolated hardened
        sandbox the agent itself uses, with no access to this host's
        filesystem or network.
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
    except Exception as exc:  # noqa: BLE001
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
