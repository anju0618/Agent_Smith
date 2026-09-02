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
# このファイルはSWE-bench用の必須9ツールをすべて実装するMCPサーバー。
# mcp_tools_mbpp.pyと同じく、agent_swebench.pyとは別プロセスとして動く。
# 決定的に重要な設計方針: このファイルは「TESTBED_PATH環境変数が指す
# ディレクトリ」を根っこにしたファイルシステム/subprocess操作しか行わず、
# Dockerを意識するコードは一行も無い。だからこそ、同じこのファイルが
# (1) docker_runner.pyによってコンテナの中で`docker exec`起動される場合と、
# (2) moulinette自身がホスト上のベアなチェックアウトに対して直接テストする
#     場合の、両方でまったく同じように動作できる。
#
# @mcp.tool() が付いた各関数のdocstringは、mcp_tools_mbpp.pyと同様に
# 実際にLLMへ送られるツール説明文の一部になるため、英語のまま変更していない。
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
    # 全ツール共通の「作業対象リポジトリのルート」を取得するヘルパー。
    # TESTBED_PATHは docker_runner.py の mcp_stdio_command() が
    # -e TESTBED_PATH=/testbed として渡す(コンテナ内で動く場合)か、
    # moulinetteが独立テスト時にホスト上のパスとして直接設定する。
    root = os.environ.get("TESTBED_PATH")
    if not root:
        # 環境変数が無ければ即座に例外 - fail-closed(サイレントに
        # 何もしないのではなく、はっきりエラーにする)方針がここにも表れている。
        raise RuntimeError(
            "TESTBED_PATH is not set. moulinette sets this to the repository root "
            "before starting this MCP server; set it yourself when testing standalone."
        )
    # .resolve() でシンボリックリンクなどを解決した正規化済み絶対パスにする。
    # これが後述の「パス脱出防止チェック」の基準点になる。
    return Path(root).resolve()


def _eval_script_path() -> Path:
    # 評価スクリプトの場所。AGENT_SMITH_EVAL_SCRIPT環境変数で上書きできるが
    # (docker_runner.pyが -e AGENT_SMITH_EVAL_SCRIPT=... で渡す)、
    # 指定が無ければ規約通り "<testbed>/eval.sh" を見に行く。
    override = os.environ.get("AGENT_SMITH_EVAL_SCRIPT")
    if override:
        return _resolve_within_testbed(override)
    return _testbed_root() / "eval.sh"


# ツール出力の上限文字数。SandboxConfig.max_output_charsのデフォルト値と
# 同じ桁数に揃えてある(意図的な整合)。
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
    # 探索系ツール(read_file/list_files/search_code/run_command/run_tests)の
    # 戻り値をここに必ず通すことで、「1回のツール呼び出しでトークン予算の
    # 大半を溶かしてしまう」事故を防ぐ。get_patch()だけは意図的にこの関数を
    # 通さない(下のget_patch()のdocstring参照) - パッチを切り詰めると
    # git applyできない壊れたdiffになってしまうため。
    if len(text) <= TOOL_OUTPUT_LIMIT_CHARS:
        return text
    omitted = len(text) - TOOL_OUTPUT_LIMIT_CHARS
    return (
        text[:TOOL_OUTPUT_LIMIT_CHARS]
        # 何文字省略したかを明示的にLLMへ伝える。「なぜか出力が
        # 途中で切れている」と黙って見せるのではなく、必ず理由を添える
        # という、このプロジェクト全体で一貫した「暗黙の動作を作らない」
        # 方針がここにも現れている。
        + f"\n[TruncatedToolOutput] {omitted} additional characters were cut off "
        f"(tool output limit: {TOOL_OUTPUT_LIMIT_CHARS} chars)."
    )


def _resolve_within_testbed(filepath: str) -> Path:
    """Resolve filepath against TESTBED_PATH and refuse to leave it."""
    # このファイル全体のセキュリティの要となる関数。read_file/edit_file/
    # list_files/run_commandなど、パスを受け取るツールはすべてこの関数を
    # 経由してから実際のファイルシステム操作を行う。
    root = _testbed_root()
    candidate = Path(filepath)
    # 絶対パスならそのまま、相対パスならリポジトリルートからの相対として解釈する。
    resolved = candidate if candidate.is_absolute() else root / candidate
    # .resolve()でシンボリックリンクや ".." を実際に展開した最終的な
    # 絶対パスにする。ここが重要: 文字列としては ".." を含んでいなくても、
    # シンボリックリンクを辿った先がTESTBED_PATHの外を指しているケースを
    # 見逃さないため。
    resolved = resolved.resolve()
    if not _is_within(root, resolved):
        # 展開した結果、リポジトリルートの外に出ていたら例外。
        # LLMが `filepath="../../etc/passwd"` のような値を渡してきても
        # ここで弾かれる。
        raise ValueError(f"'{filepath}' resolves outside the repository root {root}")
    return resolved


def _is_within(root: Path, candidate: Path) -> bool:
    # candidateがroot自身か、rootの子孫であるかを判定する小さなヘルパー。
    # Path.parentsはcandidateの祖先パスすべてを列挙するイテレータなので、
    # その中にrootが含まれていれば「rootの内側にある」と言える。
    return candidate == root or root in candidate.parents


def _validate_glob_pattern(pattern: str) -> None:
    # list_files/search_codeが受け取るglobパターン自体を検証する。
    # パターン文字列そのものが絶対パスだったり ".." を含んでいたりすると、
    # globの展開結果を待たずにその時点で明らかに不正なので早期に拒否する。
    pattern_path = Path(pattern)
    if not pattern or pattern_path.is_absolute() or ".." in pattern_path.parts:
        raise ValueError("glob pattern must be relative and must not contain '..'")


def _matching_files(directory: Path, pattern: str, recursive: bool) -> list:
    """Return only regular files whose resolved targets remain in TESTBED_PATH."""
    # list_files/search_codeの共通処理: globパターンでファイルを探し、
    # それぞれの解決結果が本当にTESTBED_PATHの中に収まっているかを
    # 再チェックしてから返す(パターン検証だけでなく、展開結果も二重に確認)。
    _validate_glob_pattern(pattern)
    root = _testbed_root()
    try:
        # recursiveならrglob(**相当)、そうでなければglob(直下のみ)。
        candidates = list(directory.rglob(pattern) if recursive else directory.glob(pattern))
    except (NotImplementedError, ValueError) as exc:
        raise ValueError(f"invalid glob pattern '{pattern}': {exc}") from exc

    matches = []
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            # 壊れたシンボリックリンクなど、resolve()自体が失敗するケースは
            # 静かにスキップする(致命的なエラーにはしない)。
            continue
        if not _is_within(root, resolved):
            # globの展開結果がシンボリックリンク経由でリポジトリの外を
            # 指していた場合は、ここで初めて検出されて例外になる。
            raise ValueError(f"glob pattern '{pattern}' matched a path outside {root}")
        if resolved.is_file() and ".git" not in resolved.parts:
            # ディレクトリは除外(ファイルのみ対象)。.git配下も除外 -
            # バージョン管理の内部データはコード検索・編集の対象として
            # 意味が無いだけでなく、巨大なオブジェクトファイルなどで
            # 無駄にノイズになるため。
            matches.append(resolved)
    return matches


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
    # 行番号付きでファイルを読む、最も基本的な探索ツール。
    try:
        path = _resolve_within_testbed(filepath)
    except ValueError as exc:
        # パス脱出などの検証エラーは、例外を投げっぱなしにせず
        # "[Error] ..." という文字列としてLLMに返す。MCPツールは常に
        # 文字列を返す契約であり、かつLLMに「何が悪かったか」を
        # 明示的に伝えるのがこのプロジェクトの一貫した方針。
        return f"[Error] {exc}"
    if not path.is_file():
        return f"[Error] file not found: {filepath}"

    # errors="replace": ファイルがUTF-8として不正なバイト列を含んでいても
    # 例外で落ちずに、置換文字(U+FFFD)で読み進める - 壊れたバイナリに
    # 近いファイルを読ませてもツールがクラッシュしないための保険。
    lines = path.read_text(errors="replace").splitlines()
    last = end_line if end_line is not None else len(lines)
    first = max(start_line, 1)
    # Pythonのリストスライスは0始まりなので、1始まりのstart_line/end_lineから
    # スライス用のインデックスへ変換している(first-1がスライス開始位置)。
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
        # old_strが1回も見つからない = LLMが記憶している内容と実ファイルが
        # ズレている(既に編集済み、typoなど)可能性が高い。何もせずエラーで返す。
        return f"[Error] old_str not found in {filepath}"
    if occurrences > 1:
        # 2回以上ヒットする場合、「どちらを置換すべきか」が一意に決まらない。
        # あいまいなまま片方を勝手に選んで書き換えるのではなく、
        # LLMにもっと具体的な(前後の文脈を含む)old_strを渡すよう
        # 差し戻す設計 - 「意図しない箇所を書き換えてしまう」事故を防ぐ。
        return (
            f"[Error] old_str is not unique in {filepath} "
            f"({occurrences} occurrences) - include more context"
        )

    # ここまで来れば厳密に1箇所だけの置換だと保証されている。
    new_content = content.replace(old_str, new_str, 1)
    path.write_text(new_content)

    if path.suffix == ".py":
        # .pyファイルを編集した場合は、追加のセーフティネットとして
        # py_compileで構文チェックする。編集そのものはすでに書き込み
        # 済みなので、構文エラーがあっても元に戻すわけではなく、
        # 「編集は適用されたが、その結果は構文エラーになっている」と
        # 明示的にLLMへ伝える([EditSyntaxError])。これによりLLMは
        # 次のターンで問題を認識し、自分で修正できる。
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

    try:
        # recursive=False固定 - list_filesは「このディレクトリの中身を
        # ざっと見る」用途で、深い再帰探索がしたいなら次のsearch_codeや
        # "**/"プレフィックス付きpatternを使えばよい、という役割分担。
        matches = sorted(str(p) for p in _matching_files(path, pattern, recursive=False))
    except ValueError as exc:
        return f"[Error] {exc}"
    return _cap_output("\n".join(matches)) if matches else "(no files matched)"


# ---------------------------------------------------------------------------
# Code Search Tools (Section 4.5.2)
# ---------------------------------------------------------------------------


def _iter_matching_files(root: Path, file_pattern: str) -> list:
    # search_code系ツールが使う内部ヘルパー。list_filesとは異なり常に
    # recursive=Trueでリポジトリ全体を対象にする。
    return _matching_files(root, file_pattern, recursive=True)


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
        # LLMが渡してくる正規表現は不正な場合もあるので、コンパイル失敗を
        # ここで捕まえて分かりやすいエラーメッセージにする。
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
                    # grepと同じ "パス:行番号 内容" というよく見慣れた
                    # フォーマットで結果を積む。これは
                    # search_function_or_class_definition_in_code()や
                    # find_references()でも共通して使われる形式。
                    results.append(f"{file}:{lineno} {line}")
        except OSError:
            # 読み取り中に権限エラーなどが起きても、そのファイルだけ
            # スキップして検索全体は継続する。
            continue
    return _cap_output("\n".join(results)) if results else "(no matches)"


# 関数/クラス定義行にマッチする正規表現のテンプレート。{name}の部分に
# エスケープ済みのシンボル名を埋め込んで使う。
_DEF_RE_TEMPLATE = r"^\s*(?:async\s+def|def|class)\s+{name}\b"


@mcp.tool()
def search_function_or_class_definition_in_code(name: str) -> str:
    """Find where a function or class is defined.

    Args:
        name: Function or class name to look for.

    Returns:
        Same format as search_code: "/absolute/path.py:<line_number> <line_content>".
    """
    # search_codeの薄いラッパー。re.escape(name)でnameに正規表現の特殊文字が
    # 含まれていても文字通りの文字列としてマッチさせる(例えば
    # "foo.bar"のような名前が来ても、"."が「任意の1文字」として
    # 解釈されないようにする)。
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
    # \bname\b - 単語境界付きでnameそのものを検索する(部分一致の
    # 誤検出、例えば "foo" を検索して "foobar" までヒットするのを防ぐ)。
    results = str(search_code(rf"\b{re.escape(name)}\b", "*.py"))
    if not filepath or not line or results.startswith("[Error]") or results == "(no matches)":
        # filepath/lineが指定されていない、あるいはそもそも検索自体が
        # エラーだったり0件だったりする場合は、除外処理をせずそのまま返す。
        return results

    try:
        declaration_path = str(_resolve_within_testbed(filepath))
    except ValueError:
        # filepathの解決に失敗しても致命的エラーにはせず、
        # 「除外なしの全結果」を返す方にフォールバックする。
        return results

    # 定義行そのものを示す行頭文字列("<パス>:<行番号> ")を作り、
    # それで始まる行だけを結果からフィルタして除外する。
    # 「定義」自体は「使用」ではない、という要求(docstring参照)を
    # 満たすための処理。
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
        # shell=True: commandを文字列のままシェルに渡す(パイプやリダイレクトを
        # LLMが自然に書けるようにするため)。start_new_session=True で
        # このプロセスを新しいプロセスグループのリーダーにする - これにより、
        # commandがさらに子プロセスを産んでいても(例: `sleep 999 &`)、
        # プロセスグループ全体に対してシグナルを送れば取りこぼしなく
        # 巻き込んで終了させられる。
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
            # 通常経路: 120秒以内に終わればここで完了。
            stdout, stderr = process.communicate(timeout=120)
        except subprocess.TimeoutExpired:
            # 120秒を超えたら、まずプロセスグループ全体にSIGTERM
            # (穏やかな終了要求)を送る。os.killpgはプロセスグループID
            # (ここではプロセスグループリーダーのpidと同じ値)を対象にする。
            os.killpg(process.pid, signal.SIGTERM)
            try:
                # SIGTERMを送ったあと2秒だけ待ち、自主的に終了するかを見る。
                stdout, stderr = process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                # それでも終わらなければSIGKILLで問答無用に強制終了させる。
                os.killpg(process.pid, signal.SIGKILL)
                stdout, stderr = process.communicate()
            # タイムアウト経路では、たとえ強制終了後に多少のstdout/stderrを
            # 回収できていても、それは使わずに固定のエラーメッセージだけを返す
            # (中途半端な出力をLLMに見せて誤解させないため)。
            return "[Error] command timed out after 120s"
        returncode = process.returncode
    except subprocess.TimeoutExpired:
        # communicate()呼び出し自体がまれに例外として飛んでくる経路への保険。
        return "[Error] command timed out after 120s"

    return _cap_output(
        f"exit_code: {returncode}\n--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}"
    )


@mcp.tool()
def run_tests() -> str:
    """Run the task's evaluation script.

    Returns:
        The evaluation script's combined output, or an explanatory error if no
        evaluation script is available in this context.
    """
    # このrun_tests()はmcp_tools_mbpp.pyのrun_tests(code, test_list)とは
    # 完全に別物(引数無し)。SWE-benchでは「候補コードを直接assertで
    # チェックする」のではなく、「タスクごとに用意された評価スクリプト
    # (eval.sh)をリポジトリに対して丸ごと実行する」という方式を取る。
    try:
        eval_script = _eval_script_path()
    except ValueError as exc:
        return f"[Error] {exc}"
    if not eval_script.is_file():
        # 評価スクリプトが存在しない(=moulinette経由ではなく単独で
        # このMCPサーバーをテストしている場合など)は、代わりに
        # run_command()を直接使うようLLM(または人間の開発者)に案内する。
        return (
            f"[Error] no evaluation script found at {eval_script}. "
            "Use run_command(...) to invoke the project's own test runner instead."
        )
    # 実体はrun_command()への薄い委譲。shlex.quote()でスクリプトパスを
    # シェルエスケープしてから "bash <path>" として実行する。
    return str(run_command(f"bash {shlex.quote(str(eval_script))}"))


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
    # -c core.fileMode=false: ファイルのパーミッションビット(実行権限など)の
    # 変更をdiffの対象から除外するgit設定。コンテナ内でファイルを
    # 書き換える際にパーミッションが本来の意図と無関係に変わってしまう
    # ことがあり、それが無意味な差分としてパッチに混入するのを防ぐ。
    result = subprocess.run(
        ["git", "-c", "core.fileMode=false", "diff"], cwd=root, capture_output=True, text=True
    )
    if result.returncode != 0:
        return f"[Error] git diff failed: {result.stderr}"
    # ここで意図的に_cap_output()を通していない点に注意
    # (このファイル冒頭の_cap_output()のdocstringで理由を説明済み):
    # この戻り値は final_answer(get_patch()) として直接提出されうるため、
    # 切り詰めるとgit applyできない壊れたdiffになってしまう。
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
        # docker_runner.pyのmcp_stdio_command()が組み立てる
        # `docker exec -i ... python3 <path>` コマンドは、
        # 引数無しでこのスクリプトを起動する - つまり実運用では常に
        # このstdio分岐が使われる。
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
