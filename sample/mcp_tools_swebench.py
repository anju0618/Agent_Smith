"""MCP server exposing the mandatory SWE-bench tools (Section 4.5).

Reads the repository root from the TESTBED_PATH environment variable, exactly
as moulinette sets it before starting this server for independent tool
testing. Every tool is a plain filesystem/subprocess operation rooted there -
this file has no Docker-specific logic, so the same code works whether
TESTBED_PATH points at a bare host checkout or at a path inside a container
this process happens to be running in (see docker_runner.py for how our own
agent_swebench.py pipeline wires that up, per approach (b) of Section 4.4).

    python mcp_tools_swebench.py            # stdio transport (default)
    python mcp_tools_swebench.py --http 8000  # streamable HTTP transport
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("agent-smith-swebench-tools")


def _testbed_root() -> Path:
    root = os.environ.get("TESTBED_PATH")
    if not root:
        raise RuntimeError(
            "TESTBED_PATH is not set. moulinette sets this to the repository root "
            "before starting this MCP server; set it yourself when testing standalone."
        )
    return Path(root).resolve()


def _eval_script_path() -> Path:
    override = os.environ.get("AGENT_SMITH_EVAL_SCRIPT")
    if override:
        return Path(override)
    return _testbed_root() / "eval.sh"


TOOL_OUTPUT_LIMIT_CHARS = 20_000  # same scale as SandboxConfig.max_output_chars's default


def _cap_output(text: str) -> str:
    """Cap exploratory tool output before it becomes an Observation.

    Without this, a single search_code() over a huge codebase, read_file()
    on a huge file, or run_command()/run_tests() invoking a verbose test
    suite could return a response large enough to eat most of the 300,000
    cumulative input-token budget (Section 6.1.2) in one step, or balloon
    memory relaying it through the MCP transport. Not applied to
    get_patch(): that return value can be the literal argument to
    final_answer(get_patch()), and truncating it would silently corrupt a
    real patch into an invalid diff - a genuine "minimal fix" patch is
    realistically small anyway, and printing it for inspection along the way
    is still bounded by the sandbox's own stdout truncation
    (sandbox/executor.py's [TruncatedOutput]).
    """
    if len(text) <= TOOL_OUTPUT_LIMIT_CHARS:
        return text
    omitted = len(text) - TOOL_OUTPUT_LIMIT_CHARS
    return (
        text[:TOOL_OUTPUT_LIMIT_CHARS]
        + f"\n[TruncatedToolOutput] {omitted} additional characters were cut off "
        f"(tool output limit: {TOOL_OUTPUT_LIMIT_CHARS} chars)."
    )


def _resolve_within_testbed(filepath: str) -> Path:
    """Resolve filepath against TESTBED_PATH and refuse to leave it."""
    root = _testbed_root()
    candidate = Path(filepath)
    resolved = candidate if candidate.is_absolute() else root / candidate
    resolved = resolved.resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError(f"'{filepath}' resolves outside the repository root {root}")
    return resolved


# ---------------------------------------------------------------------------
# File System Tools (Section 4.5.1)
# ---------------------------------------------------------------------------


@mcp.tool()
def read_file(filepath: str, start_line: int = 1, end_line: Optional[int] = None) -> str:
    """Read a file's content with line numbers, cat -n style.

    Args:
        filepath: Path to the file (absolute, or relative to the repo root).
        start_line: First line to include (1-indexed, inclusive).
        end_line: Last line to include (1-indexed, inclusive). Reads to EOF if omitted.

    Returns:
        "<line_number>: <line_content>" lines, one per line of the file.
    """
    try:
        path = _resolve_within_testbed(filepath)
    except ValueError as exc:
        return f"[Error] {exc}"
    if not path.is_file():
        return f"[Error] file not found: {filepath}"

    lines = path.read_text(errors="replace").splitlines()
    last = end_line if end_line is not None else len(lines)
    first = max(start_line, 1)
    selected = lines[first - 1: last]
    return _cap_output("\n".join(f"{i}: {line}" for i, line in enumerate(selected, start=first)))


@mcp.tool()
def edit_file(filepath: str, old_str: str, new_str: str) -> str:
    """Replace an exact string occurrence in a file with a new string.

    Args:
        filepath: Path to the file to edit.
        old_str: Exact text to find (must appear exactly once).
        new_str: Replacement text.

    Returns:
        A status message. If the edit introduces a Python syntax error, that is
        reported explicitly instead of being silently applied (Section 4.1's
        mandatory "edit introduced a syntax error" feedback).
    """
    try:
        path = _resolve_within_testbed(filepath)
    except ValueError as exc:
        return f"[Error] {exc}"
    if not path.is_file():
        return f"[Error] file not found: {filepath}"

    content = path.read_text(errors="replace")
    occurrences = content.count(old_str)
    if occurrences == 0:
        return f"[Error] old_str not found in {filepath}"
    if occurrences > 1:
        return (
            f"[Error] old_str is not unique in {filepath} "
            f"({occurrences} occurrences) - include more context"
        )

    new_content = content.replace(old_str, new_str, 1)
    path.write_text(new_content)

    if path.suffix == ".py":
        result = subprocess.run(
            ["python3", "-m", "py_compile", str(path)], capture_output=True, text=True
        )
        if result.returncode != 0:
            return f"[EditSyntaxError] Edit applied, but introduced a syntax error:\n{result.stderr}"

    return f"Edit applied to {filepath}"


@mcp.tool()
def list_files(directory: str, pattern: str = "*") -> str:
    """List files in a directory matching a glob pattern.

    Non-recursive by default (e.g. "*.py" lists only direct children); use a
    "**/" prefix in pattern for a recursive search (e.g. "**/*.py").

    Args:
        directory: Directory to list (absolute, or relative to the repo root).
        pattern: Glob pattern to filter file names (e.g. "*.py", "**/*.py").
    """
    try:
        path = _resolve_within_testbed(directory)
    except ValueError as exc:
        return f"[Error] {exc}"
    if not path.is_dir():
        return f"[Error] directory not found: {directory}"

    matches = sorted(str(p) for p in path.glob(pattern) if p.is_file() and ".git" not in p.parts)
    return _cap_output("\n".join(matches)) if matches else "(no files matched)"


# ---------------------------------------------------------------------------
# Code Search Tools (Section 4.5.2)
# ---------------------------------------------------------------------------


def _iter_matching_files(root: Path, file_pattern: str) -> list:
    return [p for p in root.rglob(file_pattern) if p.is_file() and ".git" not in p.parts]


@mcp.tool()
def search_code(pattern: str, file_pattern: str = "*.py") -> str:
    """Grep-like regex search across the codebase.

    Args:
        pattern: Regular expression to search for.
        file_pattern: Glob for which files to search (default "*.py").

    Returns:
        "/absolute/path.py:<line_number> <line_content>" lines.
    """
    root = _testbed_root()
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        return f"[Error] invalid regex: {exc}"

    results = []
    for file in _iter_matching_files(root, file_pattern):
        try:
            for lineno, line in enumerate(file.read_text(errors="replace").splitlines(), start=1):
                if regex.search(line):
                    results.append(f"{file}:{lineno} {line}")
        except OSError:
            continue
    return _cap_output("\n".join(results)) if results else "(no matches)"


_DEF_RE_TEMPLATE = r"^\s*(?:async\s+def|def|class)\s+{name}\b"


@mcp.tool()
def search_function_or_class_definition_in_code(name: str) -> str:
    """Find where a function or class is defined.

    Args:
        name: Function or class name to look for.

    Returns:
        Same format as search_code: "/absolute/path.py:<line_number> <line_content>".
    """
    return str(search_code(_DEF_RE_TEMPLATE.format(name=re.escape(name)), "*.py"))


@mcp.tool()
def find_references(name: str, filepath: str = "", line: int = 0) -> str:
    """Find all usages of a function or class name across the codebase.

    Args:
        name: Symbol name to search for.
        filepath: Optional path to the file where `name` is defined. When given
            together with `line`, that exact location is excluded from the
            results, since a declaration is not itself a "usage".
        line: Optional 1-indexed line of the definition (see filepath).

    Returns:
        Same format as search_code: one usage per line, declaration excluded
        when filepath/line identify it.
    """
    results = str(search_code(rf"\b{re.escape(name)}\b", "*.py"))
    if not filepath or not line or results.startswith("[Error]") or results == "(no matches)":
        return results

    try:
        declaration_path = str(_resolve_within_testbed(filepath))
    except ValueError:
        return results

    declaration_marker = f"{declaration_path}:{line} "
    filtered = [ln for ln in results.splitlines() if not ln.startswith(declaration_marker)]
    return "\n".join(filtered) if filtered else "(no matches other than the declaration)"


# ---------------------------------------------------------------------------
# Execution Tools (Section 4.5.3)
# ---------------------------------------------------------------------------


@mcp.tool()
def run_command(command: str, workdir: str = "") -> str:
    """Run a shell command in the given working directory.

    Args:
        command: The shell command to execute.
        workdir: Working directory (absolute, or relative to the repo root;
            defaults to the repo root).

    Returns:
        A formatted block with stdout, stderr, and exit code.
    """
    try:
        cwd = _resolve_within_testbed(workdir) if workdir else _testbed_root()
    except ValueError as exc:
        return f"[Error] {exc}"

    try:
        result = subprocess.run(command, shell=True, cwd=cwd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return "[Error] command timed out after 120s"

    return _cap_output(
        f"exit_code: {result.returncode}\n--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )


@mcp.tool()
def run_tests() -> str:
    """Run the task's evaluation script.

    Returns:
        The evaluation script's combined output, or an explanatory error if no
        evaluation script is available in this context.
    """
    eval_script = _eval_script_path()
    if not eval_script.is_file():
        return (
            f"[Error] no evaluation script found at {eval_script}. "
            "Use run_command(...) to invoke the project's own test runner instead."
        )
    return str(run_command(f"bash {eval_script}"))


@mcp.tool()
def get_patch() -> str:
    """Get the unified git diff of every change made to the repository so far.

    Returns:
        The output of `git -c core.fileMode=false diff` (Section 4.4).
        Deliberately not passed through _cap_output - see that function's
        docstring for why truncating a real patch would be worse than
        leaving it whole.
    """
    root = _testbed_root()
    result = subprocess.run(
        ["git", "-c", "core.fileMode=false", "diff"], cwd=root, capture_output=True, text=True
    )
    if result.returncode != 0:
        return f"[Error] git diff failed: {result.stderr}"
    return result.stdout


def main() -> None:
    parser = argparse.ArgumentParser(description="SWE-bench MCP tool server")
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
