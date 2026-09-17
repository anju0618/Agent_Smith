"""SWE-bench向けの必須ツール(セクション4.5)を公開するMCPサーバ。

moulinetteが独立したツールテストのためにこのサーバを起動する際に設定するのと全く同じ方法で、
TESTBED_PATH環境変数からリポジトリのルートを読み取る。各ツールは単純なファイルシステム/
サブプロセス操作であり、このファイル自体にはDocker固有のロジックは一切ない - そのため、
TESTBED_PATHがベアなホスト上のチェックアウトを指していても、このプロセス自身が動いている
コンテナ内のパスを指していても同じコードで動作する(私たち自身のagent_swebench.pyパイプラインが
セクション4.4のアプローチ(b)に従ってこれをどう配線しているかはdocker_runner.py参照)。

    python mcp_tools_swebench.py            # stdioトランスポート(デフォルト)
    python mcp_tools_swebench.py --http 8000  # streamable HTTPトランスポート
"""
from __future__ import annotations

import argparse
import os
import re
import shlex
import signal
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
        return _resolve_within_testbed(override)
    return _testbed_root() / "eval.sh"


TOOL_OUTPUT_LIMIT_CHARS = 20_000


def _cap_output(text: str) -> str:
    """探索系ツールの出力がObservationになる前に長さの上限を設ける。

    これがないと、巨大なコードベースに対するsearch_code()単発呼び出し、巨大ファイルへの
    read_file()、あるいは冗長なテストスイートを呼び出すrun_command()/run_tests()が、
    1ステップで累積300,000トークンの入力予算(セクション6.1.2)の大部分を食いつぶすほど
    大きな応答を返したり、MCPトランスポート経由で中継する際にメモリを膨張させたりする
    恐れがある。get_patch()には適用していない: その戻り値はfinal_answer(get_patch())の
    引数そのものになりうるため、これを切り詰めると本物のパッチが不正なdiffへと静かに
    壊されてしまう - 本当に「最小限の修正」であるパッチは現実的には十分小さいはずであり、
    途中経過を確認するために出力する場合でも、サンドボックス自体のstdout切り詰め
    (sandbox/executor.pyの[TruncatedOutput])によって既に制限されている。

    先頭だけを残す単純な切り詰めは行わない: run_tests()が呼び出すeval.shは
    `git status`/`git show`/`git diff <base_commit>`のような大量のノイズを最初に
    出力してから、末尾でようやくpytestのPASSED/FAILEDや例外トレースバックを出す
    構成になっているため、先頭だけ残すと肝心のテスト結果が毎回消えてしまう
    (実例: scikit-learn__scikit-learn-13439ではdockerイメージ内のファイル権限が
    git記録と食い違い、`git diff`だけで上限を使い切ってpytestの出力が完全に
    見えなくなった)。末尾を手厚く残しつつ先頭にも少し余地を残すことで、冒頭の
    文脈と末尾の結果の両方が見えるようにする。
    """
    if len(text) <= TOOL_OUTPUT_LIMIT_CHARS:
        return text
    omitted = len(text) - TOOL_OUTPUT_LIMIT_CHARS
    head_chars = TOOL_OUTPUT_LIMIT_CHARS // 4
    tail_chars = TOOL_OUTPUT_LIMIT_CHARS - head_chars
    return (
        text[:head_chars]
        + f"\n[TruncatedToolOutput] {omitted} chars omitted - kept head+tail; the "
        "tail usually holds the test result/traceback.\n"
        + text[-tail_chars:]
    )


_TESTBED_PATH_ALIAS = Path("/testbed")


def _resolve_within_testbed(filepath: str) -> Path:
    """filepathをTESTBED_PATHを基準に解決し、その外に出ることを拒否する。"""
    root = _testbed_root()
    candidate = Path(filepath)
    if candidate.is_absolute():
        if candidate == _TESTBED_PATH_ALIAS:
            resolved = root
        elif _TESTBED_PATH_ALIAS in candidate.parents:

            resolved = root / candidate.relative_to(_TESTBED_PATH_ALIAS)
        else:
            resolved = candidate
    else:
        resolved = root / candidate
    resolved = resolved.resolve()
    if not _is_within(root, resolved):
        raise ValueError(f"'{filepath}' resolves outside the repository root {root}")
    return resolved


def _is_within(root: Path, candidate: Path) -> bool:

    return candidate == root or root in candidate.parents


def _validate_glob_pattern(pattern: str) -> None:

    pattern_path = Path(pattern)
    if not pattern or pattern_path.is_absolute() or ".." in pattern_path.parts:
        raise ValueError("glob pattern must be relative and must not contain '..'")


def _matching_files(directory: Path, pattern: str, recursive: bool) -> list:
    """解決後のパスがTESTBED_PATH配下に留まる通常ファイルのみを返す。"""
    _validate_glob_pattern(pattern)
    root = _testbed_root()
    try:

        candidates = list(directory.rglob(pattern) if recursive else directory.glob(pattern))
    except (NotImplementedError, ValueError) as exc:
        raise ValueError(f"invalid glob pattern '{pattern}': {exc}") from exc

    matches = []
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if not _is_within(root, resolved):

            raise ValueError(f"glob pattern '{pattern}' matched a path outside {root}")
        if resolved.is_file() and ".git" not in resolved.parts:
            matches.append(resolved)
    return matches


@mcp.tool()
def read_file(filepath: str, start_line: int = 1, end_line: Optional[int] = None) -> str:
    """Read a file's contents with line numbers, like `cat -n`.

    Args:
        filepath: Path to the file (absolute, or relative to the repo root).
        start_line: First line to read (1-indexed, inclusive).
        end_line: Last line to read (1-indexed, inclusive). Defaults to EOF.

    Returns:
        One "<line number>: <line content>" string per line.
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
    """Replace an exact-match string in a file with a new string.

    Args:
        filepath: Path to the file to edit.
        old_str: Exact text to find (must occur exactly once in the file).
        new_str: Replacement text.

    Returns:
        A status message. If the edit introduces a Python syntax error, that
        is reported explicitly instead of being applied silently.
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

    Non-recursive by default (e.g. "*.py" only matches direct children).
    Prefix pattern with "**/" to search recursively (e.g. "**/*.py").

    Args:
        directory: Directory to list (absolute, or relative to the repo root).
        pattern: Glob pattern to filter filenames (e.g. "*.py", "**/*.py").
    """
    try:
        path = _resolve_within_testbed(directory)
    except ValueError as exc:
        return f"[Error] {exc}"
    if not path.is_dir():
        return f"[Error] directory not found: {directory}"

    try:

        matches = sorted(str(p) for p in _matching_files(path, pattern, recursive=False))
    except ValueError as exc:
        return f"[Error] {exc}"

    return _cap_output("\n".join(matches)) if matches else "(no files matched)"


def _iter_matching_files(root: Path, file_pattern: str) -> list:

    return _matching_files(root, file_pattern, recursive=True)


@mcp.tool()
def search_code(pattern: str, file_pattern: str = "*.py") -> str:
    """grep-like regex search across the whole codebase.

    Args:
        pattern: Regular expression to search for.
        file_pattern: Glob pattern restricting which files to search (default "*.py").

    Returns:
        Lines of the form "/absolute/path.py:<line number> <line content>".
    """
    root = _testbed_root()
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        return f"[Error] invalid regex: {exc}"

    try:
        matching_files = _iter_matching_files(root, file_pattern)
    except ValueError as exc:
        return f"[Error] {exc}"

    results = []
    for file in matching_files:
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
        name: Name of the function or class to find.

    Returns:
        Same format as search_code: "/absolute/path.py:<line number> <line content>".
    """

    return str(search_code(_DEF_RE_TEMPLATE.format(name=re.escape(name)), "*.py"))


@mcp.tool()
def find_references(name: str, filepath: str = "", line: int = 0) -> str:
    """Find every usage of a function/class name across the codebase.

    Args:
        name: Symbol name to search for.
        filepath: Optional path to the file where `name` is defined. Combined
            with line, the declaration itself is excluded from the results
            since it is not a "usage".
        line: Optional 1-indexed line of the definition (used with filepath).

    Returns:
        Same format as search_code: one usage per line. Excludes the
        declaration line when filepath/line identify it.
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


@mcp.tool()
def run_command(command: str, workdir: str = "") -> str:
    """Run a shell command in the given working directory.

    Args:
        command: Shell command to run.
        workdir: Working directory (absolute, or relative to the repo root;
            defaults to the repo root).

    Returns:
        A formatted block combining stdout, stderr, and exit code.
    """
    try:

        cwd = _resolve_within_testbed(workdir) if workdir else _testbed_root()
    except ValueError as exc:
        return f"[Error] {exc}"

    try:
        process = subprocess.Popen(
            command,
            shell=True,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=120)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                stdout, stderr = process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                stdout, stderr = process.communicate()
            return "[Error] command timed out after 120s"
        returncode = process.returncode
    except subprocess.TimeoutExpired:
        return "[Error] command timed out after 120s"

    return _cap_output(
        f"exit_code: {returncode}\n--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}"
    )


@mcp.tool()
def run_tests() -> str:
    """Run the task's evaluation script.

    Returns:
        The evaluation script's combined stdout/stderr. Returns an
        explanatory error if no evaluation script is available here.
    """
    try:
        eval_script = _eval_script_path()
    except ValueError as exc:
        return f"[Error] {exc}"
    if not eval_script.is_file():
        return (
            f"[Error] no evaluation script found at {eval_script}. "
            "Use run_command(...) to invoke the project's own test runner instead."
        )

    return str(run_command(f"bash {shlex.quote(str(eval_script))}"))


@mcp.tool()
def get_patch() -> str:
    """Get a unified git diff of every change made to the repository so far.

    Returns:
        The output of `git -c core.fileMode=false diff`. Intentionally not
        passed through _cap_output - see that function's docstring for why
        truncating a real patch would be worse.
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
