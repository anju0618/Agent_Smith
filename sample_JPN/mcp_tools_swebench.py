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
from __future__ import annotations  # 型注釈を文字列として遅延評価する(将来のアノテーション構文をサポート)

import argparse  # コマンドライン引数のパース用
import os  # 環境変数の読み取りに使用
import re  # 正規表現による検索用
import shlex  # シェルコマンド用の文字列を安全にクォートするために使用
import signal  # サブプロセスへのシグナル送信に使用
import subprocess  # 外部コマンド・スクリプトの実行に使用
from pathlib import Path  # ファイルパス操作用
from typing import Optional  # Optional型注釈のため

from mcp.server.fastmcp import FastMCP  # MCPサーバを構築するためのフレームワーク

mcp = FastMCP("agent-smith-swebench-tools")  # SWE-bench用MCPサーバのインスタンスを作成


def _testbed_root() -> Path:
    # TESTBED_PATH環境変数からリポジトリのルートパスを取得するヘルパー関数
    root = os.environ.get("TESTBED_PATH")  # 環境変数の値を取得(未設定ならNone)
    if not root:
        raise RuntimeError(
            "TESTBED_PATH is not set. moulinette sets this to the repository root "
            "before starting this MCP server; set it yourself when testing standalone."
        )  # 未設定の場合は分かりやすいメッセージ付きで例外を送出する
    return Path(root).resolve()  # 絶対パスに正規化して返す


def _eval_script_path() -> Path:
    # 評価スクリプトのパスを決定するヘルパー関数(環境変数での上書きに対応)
    override = os.environ.get("AGENT_SMITH_EVAL_SCRIPT")  # 評価スクリプトパスの上書き指定を取得
    if override:
        return _resolve_within_testbed(override)  # 上書き指定があればそれをTESTBED_PATH配下として解決する
    return _testbed_root() / "eval.sh"  # 上書きがなければリポジトリルート直下のeval.shをデフォルトとする


TOOL_OUTPUT_LIMIT_CHARS = 20_000  # SandboxConfig.max_output_charsのデフォルトと同程度のスケール


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
    """
    if len(text) <= TOOL_OUTPUT_LIMIT_CHARS:
        return text  # 上限以内であればそのまま返す
    omitted = len(text) - TOOL_OUTPUT_LIMIT_CHARS  # 切り詰めによって省略される文字数を計算
    return (
        text[:TOOL_OUTPUT_LIMIT_CHARS]  # 上限文字数までの内容を残す
        + f"\n[TruncatedToolOutput] {omitted} additional characters were cut off "
        f"(tool output limit: {TOOL_OUTPUT_LIMIT_CHARS} chars)."  # 何文字省略されたかを示す注記を追加
    )


def _resolve_within_testbed(filepath: str) -> Path:
    """filepathをTESTBED_PATHを基準に解決し、その外に出ることを拒否する。"""
    root = _testbed_root()  # リポジトリのルートパスを取得
    candidate = Path(filepath)  # 与えられたパス文字列をPathオブジェクト化
    resolved = candidate if candidate.is_absolute() else root / candidate  # 絶対パスならそのまま、相対パスならルートと結合
    resolved = resolved.resolve()  # シンボリックリンクや".."などを解決した実パスに正規化
    if not _is_within(root, resolved):
        raise ValueError(f"'{filepath}' resolves outside the repository root {root}")  # 解決結果がルート外であれば拒否する
    return resolved  # 検証済みの絶対パスを返す


def _is_within(root: Path, candidate: Path) -> bool:
    # candidateがrootそのもの、またはrootの配下にあるかどうかを判定するヘルパー関数
    return candidate == root or root in candidate.parents  # 一致するか、祖先ディレクトリにrootが含まれていればTrue


def _validate_glob_pattern(pattern: str) -> None:
    # globパターンが相対パスであり、".."を含まないことを検証するヘルパー関数
    pattern_path = Path(pattern)  # パターン文字列をPathオブジェクト化
    if not pattern or pattern_path.is_absolute() or ".." in pattern_path.parts:
        raise ValueError("glob pattern must be relative and must not contain '..'")  # 空文字・絶対パス・親ディレクトリ参照を拒否する


def _matching_files(directory: Path, pattern: str, recursive: bool) -> list:
    """解決後のパスがTESTBED_PATH配下に留まる通常ファイルのみを返す。"""
    _validate_glob_pattern(pattern)  # まずパターン自体の安全性を検証する
    root = _testbed_root()  # リポジトリのルートパスを取得
    try:
        candidates = list(directory.rglob(pattern) if recursive else directory.glob(pattern))  # recursiveフラグに応じて再帰的/非再帰的にglob検索
    except (NotImplementedError, ValueError) as exc:
        raise ValueError(f"invalid glob pattern '{pattern}': {exc}") from exc  # globパターン自体が不正な場合はエラーとして送出

    matches = []  # 条件を満たすファイルのリスト(結果格納用)
    for candidate in candidates:
        try:
            resolved = candidate.resolve()  # 各候補パスをシンボリックリンク解決済みの絶対パスにする
        except OSError:
            continue  # 解決に失敗した場合(壊れたリンク等)はスキップする
        if not _is_within(root, resolved):
            raise ValueError(f"glob pattern '{pattern}' matched a path outside {root}")  # ルート外にマッチした場合は安全のため例外を送出
        if resolved.is_file() and ".git" not in resolved.parts:
            matches.append(resolved)  # 通常ファイルであり、かつ.gitディレクトリ配下でないもののみ結果に追加
    return matches  # 条件を満たすファイルパスのリストを返す


# ---------------------------------------------------------------------------
# ファイルシステム系ツール(セクション4.5.1)
# ---------------------------------------------------------------------------


@mcp.tool()
def read_file(filepath: str, start_line: int = 1, end_line: Optional[int] = None) -> str:
    """ファイルの内容を、cat -nのように行番号付きで読み取る。

    引数:
        filepath: ファイルへのパス(絶対パス、またはリポジトリルートからの相対パス)。
        start_line: 読み取りを開始する行(1始まり、この行を含む)。
        end_line: 読み取りを終了する行(1始まり、この行を含む)。省略時はファイル末尾まで読む。

    戻り値:
        ファイルの各行に対応する"<行番号>: <行の内容>"という形式の文字列。
    """
    try:
        path = _resolve_within_testbed(filepath)  # 指定パスをリポジトリルート配下として解決・検証
    except ValueError as exc:
        return f"[Error] {exc}"  # パスが不正・範囲外であればエラーメッセージを返す
    if not path.is_file():
        return f"[Error] file not found: {filepath}"  # ファイルが存在しない場合のエラー

    lines = path.read_text(errors="replace").splitlines()  # ファイル全体を読み込み、デコードエラーは置換文字にしつつ行のリストに分割
    last = end_line if end_line is not None else len(lines)  # end_line未指定なら最終行までを対象とする
    first = max(start_line, 1)  # start_lineが1未満にならないよう補正する
    selected = lines[first - 1: last]  # 1始まりの行番号指定を0始まりのスライスに変換して該当範囲を抽出
    return _cap_output("\n".join(f"{i}: {line}" for i, line in enumerate(selected, start=first)))  # 各行に行番号を付けて結合し、出力サイズ上限を適用して返す


@mcp.tool()
def edit_file(filepath: str, old_str: str, new_str: str) -> str:
    """ファイル内の完全一致する文字列を新しい文字列に置き換える。

    引数:
        filepath: 編集対象ファイルへのパス。
        old_str: 検索対象の正確なテキスト(ファイル内にちょうど1回だけ出現する必要がある)。
        new_str: 置き換え後のテキスト。

    戻り値:
        処理結果を示すメッセージ。編集によってPythonの構文エラーが発生した場合は、
        黙って適用するのではなく明示的にその旨を報告する(セクション4.1が求める
        「edit introduced a syntax error」フィードバックの必須要件)。
    """
    try:
        path = _resolve_within_testbed(filepath)  # 指定パスをリポジトリルート配下として解決・検証
    except ValueError as exc:
        return f"[Error] {exc}"  # パスが不正・範囲外であればエラーメッセージを返す
    if not path.is_file():
        return f"[Error] file not found: {filepath}"  # ファイルが存在しない場合のエラー

    content = path.read_text(errors="replace")  # 編集対象ファイルの内容全体を読み込む
    occurrences = content.count(old_str)  # old_strがファイル内に何回出現するかを数える
    if occurrences == 0:
        return f"[Error] old_str not found in {filepath}"  # 1回も見つからなければエラー
    if occurrences > 1:
        return (
            f"[Error] old_str is not unique in {filepath} "
            f"({occurrences} occurrences) - include more context"
        )  # 複数回出現する場合は一意に特定できないためエラー(より多くの文脈を含めるよう促す)

    new_content = content.replace(old_str, new_str, 1)  # 一意に特定できたので、最初の1件だけを置換する
    path.write_text(new_content)  # 置換後の内容をファイルに書き戻す

    if path.suffix == ".py":
        result = subprocess.run(
            ["python3", "-m", "py_compile", str(path)], capture_output=True, text=True
        )  # Pythonファイルであれば、コンパイルチェックで構文エラーが発生していないか検証する
        if result.returncode != 0:
            return f"[EditSyntaxError] Edit applied, but introduced a syntax error:\n{result.stderr}"  # 構文エラーが検出された場合は編集済みだがエラーがある旨を報告する

    return f"Edit applied to {filepath}"  # 正常に編集が完了したことを示すメッセージを返す


@mcp.tool()
def list_files(directory: str, pattern: str = "*") -> str:
    """指定ディレクトリ内で、globパターンに一致するファイルを一覧表示する。

    デフォルトでは非再帰的(例えば"*.py"は直下の子ファイルのみを対象とする)。
    再帰的に検索する場合はpatternの先頭に"**/"を付ける(例: "**/*.py")。

    引数:
        directory: 一覧表示するディレクトリ(絶対パス、またはリポジトリルートからの相対パス)。
        pattern: ファイル名を絞り込むためのglobパターン(例: "*.py", "**/*.py")。
    """
    try:
        path = _resolve_within_testbed(directory)  # 指定ディレクトリをリポジトリルート配下として解決・検証
    except ValueError as exc:
        return f"[Error] {exc}"  # パスが不正・範囲外であればエラーメッセージを返す
    if not path.is_dir():
        return f"[Error] directory not found: {directory}"  # ディレクトリが存在しない場合のエラー

    try:
        matches = sorted(str(p) for p in _matching_files(path, pattern, recursive=False))  # 非再帰的にマッチするファイルを検索し、パス文字列としてソートする
    except ValueError as exc:
        return f"[Error] {exc}"  # globパターンが不正な場合などはエラーメッセージを返す
    return _cap_output("\n".join(matches)) if matches else "(no files matched)"  # マッチがあれば一覧を返し、なければその旨のメッセージを返す


# ---------------------------------------------------------------------------
# コード検索系ツール(セクション4.5.2)
# ---------------------------------------------------------------------------


def _iter_matching_files(root: Path, file_pattern: str) -> list:
    # 指定ルート配下を再帰的に検索し、file_patternに一致するファイルの一覧を返すヘルパー関数
    return _matching_files(root, file_pattern, recursive=True)


@mcp.tool()
def search_code(pattern: str, file_pattern: str = "*.py") -> str:
    """コードベース全体に対するgrepライクな正規表現検索。

    引数:
        pattern: 検索対象の正規表現。
        file_pattern: 検索対象とするファイルを絞り込むglobパターン(デフォルトは"*.py")。

    戻り値:
        "/absolute/path.py:<行番号> <行の内容>"という形式の行の並び。
    """
    root = _testbed_root()  # リポジトリのルートパスを取得
    try:
        regex = re.compile(pattern)  # 与えられたパターン文字列を正規表現としてコンパイル
    except re.error as exc:
        return f"[Error] invalid regex: {exc}"  # 正規表現として不正な場合はエラーメッセージを返す

    try:
        matching_files = _iter_matching_files(root, file_pattern)  # file_patternに一致する検索対象ファイル一覧を取得
    except ValueError as exc:
        return f"[Error] {exc}"  # globパターンが不正な場合などはエラーメッセージを返す

    results = []  # マッチした行を集めるリスト
    for file in matching_files:
        try:
            for lineno, line in enumerate(file.read_text(errors="replace").splitlines(), start=1):
                if regex.search(line):
                    results.append(f"{file}:{lineno} {line}")  # 正規表現にマッチした行を「ファイルパス:行番号 内容」の形式で記録
        except OSError:
            continue  # ファイル読み込みに失敗した場合はそのファイルをスキップする
    return _cap_output("\n".join(results)) if results else "(no matches)"  # マッチがあれば結果を返し、なければその旨のメッセージを返す


_DEF_RE_TEMPLATE = r"^\s*(?:async\s+def|def|class)\s+{name}\b"  # 関数・非同期関数・クラスの定義行にマッチする正規表現テンプレート


@mcp.tool()
def search_function_or_class_definition_in_code(name: str) -> str:
    """関数またはクラスがどこで定義されているかを探す。

    引数:
        name: 探したい関数またはクラスの名前。

    戻り値:
        search_codeと同じ形式: "/absolute/path.py:<行番号> <行の内容>"。
    """
    return str(search_code(_DEF_RE_TEMPLATE.format(name=re.escape(name)), "*.py"))  # 定義行にマッチする正規表現を組み立ててsearch_codeに委譲する


@mcp.tool()
def find_references(name: str, filepath: str = "", line: int = 0) -> str:
    """コードベース全体から、ある関数・クラス名のすべての使用箇所を探す。

    引数:
        name: 検索対象のシンボル名。
        filepath: `name`が定義されているファイルへの任意指定パス。lineと合わせて
            指定した場合、宣言そのものは「使用」ではないため結果から除外される。
        line: 定義箇所の1始まりの行番号(任意指定、filepathとセットで使用)。

    戻り値:
        search_codeと同じ形式: 1行1使用箇所。filepath/lineで宣言箇所が
        特定できた場合はそれを除外する。
    """
    results = str(search_code(rf"\b{re.escape(name)}\b", "*.py"))  # 単語境界付きでシンボル名を検索し、全出現箇所を取得する
    if not filepath or not line or results.startswith("[Error]") or results == "(no matches)":
        return results  # 宣言位置の情報がない、あるいは検索自体がエラー/該当なしの場合はそのまま返す

    try:
        declaration_path = str(_resolve_within_testbed(filepath))  # 宣言ファイルのパスをリポジトリルート配下として解決する
    except ValueError:
        return results  # 宣言ファイルのパスが不正な場合は除外処理をせずそのまま返す

    declaration_marker = f"{declaration_path}:{line} "  # 宣言箇所の行を特定するための先頭一致文字列を組み立てる
    filtered = [ln for ln in results.splitlines() if not ln.startswith(declaration_marker)]  # 宣言箇所に一致する行を結果から除外する
    return "\n".join(filtered) if filtered else "(no matches other than the declaration)"  # 除外後の結果を返す(宣言以外に使用箇所がなければその旨を返す)


# ---------------------------------------------------------------------------
# 実行系ツール(セクション4.5.3)
# ---------------------------------------------------------------------------


@mcp.tool()
def run_command(command: str, workdir: str = "") -> str:
    """指定した作業ディレクトリでシェルコマンドを実行する。

    引数:
        command: 実行するシェルコマンド。
        workdir: 作業ディレクトリ(絶対パス、またはリポジトリルートからの相対パス。
            省略時はリポジトリルート)。

    戻り値:
        stdout・stderr・終了コードをまとめた整形済みブロック。
    """
    try:
        cwd = _resolve_within_testbed(workdir) if workdir else _testbed_root()  # workdirが指定されていればそれを解決し、なければリポジトリルートを使う
    except ValueError as exc:
        return f"[Error] {exc}"  # 作業ディレクトリの指定が不正な場合はエラーメッセージを返す

    try:
        process = subprocess.Popen(
            command,
            shell=True,  # シェル経由でコマンド文字列を実行する
            cwd=cwd,  # 指定した作業ディレクトリで実行する
            stdout=subprocess.PIPE,  # 標準出力をパイプで受け取る
            stderr=subprocess.PIPE,  # 標準エラー出力をパイプで受け取る
            text=True,  # バイト列ではなく文字列として入出力を扱う
            start_new_session=True,  # 新しいプロセスグループで起動し、後でグループ単位でシグナルを送れるようにする
        )  # サブプロセスとしてコマンドを起動する
        try:
            stdout, stderr = process.communicate(timeout=120)  # 最大120秒待ってプロセスの完了と出力の取得を試みる
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)  # タイムアウトした場合はプロセスグループ全体にSIGTERMを送って穏やかに終了を試みる
            try:
                stdout, stderr = process.communicate(timeout=2)  # SIGTERM後さらに2秒だけ終了を待つ
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)  # それでも終了しなければSIGKILLで強制終了する
                stdout, stderr = process.communicate()  # 強制終了後の出力を回収する
            return "[Error] command timed out after 120s"  # タイムアウトが発生したことを示すエラーを返す
        returncode = process.returncode  # 正常終了した場合の終了コードを取得
    except subprocess.TimeoutExpired:
        return "[Error] command timed out after 120s"  # communicate呼び出し自体がタイムアウト例外を送出した場合のフォールバック

    return _cap_output(
        f"exit_code: {returncode}\n--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}"
    )  # 終了コード・標準出力・標準エラー出力をまとめて整形し、出力サイズ上限を適用して返す


@mcp.tool()
def run_tests() -> str:
    """タスクの評価スクリプトを実行する。

    戻り値:
        評価スクリプトの標準出力・標準エラーを結合した出力。このコンテキストで
        評価スクリプトが利用できない場合は、その旨を説明するエラーを返す。
    """
    try:
        eval_script = _eval_script_path()  # 評価スクリプトのパスを決定する
    except ValueError as exc:
        return f"[Error] {exc}"  # パス決定に失敗した場合はエラーメッセージを返す
    if not eval_script.is_file():
        return (
            f"[Error] no evaluation script found at {eval_script}. "
            "Use run_command(...) to invoke the project's own test runner instead."
        )  # 評価スクリプトが存在しない場合、代わりにrun_command()を使うよう案内する
    return str(run_command(f"bash {shlex.quote(str(eval_script))}"))  # 評価スクリプトをbashで実行するコマンドをrun_command()に委譲する(パスは安全にクォートする)


@mcp.tool()
def get_patch() -> str:
    """これまでにリポジトリに加えられた全ての変更をまとめたunified git diffを取得する。

    戻り値:
        `git -c core.fileMode=false diff`の出力(セクション4.4)。意図的に
        _cap_outputを通していない - 本物のパッチを切り詰めることがなぜより悪いのかは
        その関数のdocstringを参照。
    """
    root = _testbed_root()  # リポジトリのルートパスを取得
    result = subprocess.run(
        ["git", "-c", "core.fileMode=false", "diff"], cwd=root, capture_output=True, text=True
    )  # ファイルモード変更を無視した設定でgit diffを実行し、リポジトリ全体の変更差分を取得する
    if result.returncode != 0:
        return f"[Error] git diff failed: {result.stderr}"  # git diffの実行自体が失敗した場合はエラーメッセージを返す
    return result.stdout  # 成功した場合はdiffの標準出力(パッチ本体)をそのまま返す


def main() -> None:
    # エントリーポイント: コマンドライン引数を解析し、stdioまたはHTTPでMCPサーバを起動する
    parser = argparse.ArgumentParser(description="SWE-bench MCP tool server")  # 引数パーサを作成
    parser.add_argument(
        "--http", type=int, default=None, help="Serve over streamable HTTP on this port instead of stdio"
    )  # HTTPで待ち受けるポート番号(指定しなければstdioモード)
    args = parser.parse_args()  # 実際にコマンドライン引数を解析

    if args.http:
        mcp.settings.port = args.http  # 指定されたポート番号をサーバ設定に反映
        mcp.run(transport="streamable-http")  # streamable HTTPトランスポートでサーバを起動
    else:
        mcp.run(transport="stdio")  # デフォルトのstdioトランスポートでサーバを起動


if __name__ == "__main__":
    main()  # スクリプトとして直接実行された場合にmain()を呼び出す
