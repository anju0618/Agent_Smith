"""Tests for mcp_tools_swebench.py's mandatory tools (Section 4.5), called
directly against a small fake repository rooted at TESTBED_PATH."""
# ============================================================================
# 日本語解説: このファイルは mcp_tools_swebench.py が提供する必須9ツール
# （read_file, edit_file, list_files, search_code,
# search_function_or_class_definition_in_code, find_references, run_command,
# run_tests, get_patch）を、本物のDockerコンテナやSWE-benchの実タスクを
# 使わずにテストしています。fake_repoというpytest fixtureが、tmp_path
# （pytestが用意する一時ディレクトリ）の中に小さなGitリポジトリを
# 実際に作り、そこをTESTBED_PATH環境変数として指すことで、
# 「本物のリポジトリのミニチュア版」を用意しています。
#
# 特に重要な観点は2つです:
#   1. パスの脱出防止 — TESTBED_PATHの外にあるファイルを読み書きしようと
#      する経路(絶対パス指定、../での相対パス、シンボリックリンク経由)が
#      すべてきちんと拒否されること。
#   2. 出力サイズの制御 — 巨大な出力を返すツールが正しく切り詰められる
#      一方で、get_patch()だけは切り詰められないこと(壊れたdiffを
#      提出してしまわないため)。
# ============================================================================
import subprocess
from pathlib import Path

import pytest

import mcp_tools_swebench as tools


@pytest.fixture()
def fake_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # 各テストの前に、mail.pyという1ファイルだけを持つ小さなGitリポジトリを
    # tmp_path配下に実際に作成するfixture。git init/add/commitまで
    # 本物のgitコマンドで行うことで、get_patch()（git diffのラッパー）の
    # ようなツールも本物のgitの挙動でテストできる。最後に
    # monkeypatch.setenvでTESTBED_PATH環境変数をこのリポジトリのパスに
    # 設定し、mcp_tools_swebench.py側の各ツールがこのfake_repoを
    # 「作業対象のリポジトリルート」として認識するようにしている。
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "mail.py").write_text(
        "def is_valid_email(mail):\n"
        "    return '@' in mail\n"
        "\n"
        "def send_email(mail):\n"
        "    if is_valid_email(mail):\n"
        "        return True\n"
        "    return False\n"
    )
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "a@a.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "a"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    monkeypatch.setenv("TESTBED_PATH", str(repo))
    return repo


def test_read_file_returns_cat_n_style(fake_repo: Path) -> None:
    # read_fileが "行番号: 内容" という、cat -n コマンドと同じ形式で
    # 指定した範囲(start_line=1, end_line=2)だけを返すことを確認する。
    output = tools.read_file(str(fake_repo / "mail.py"), start_line=1, end_line=2)
    assert output == "1: def is_valid_email(mail):\n2:     return '@' in mail"


def test_list_files_matches_pattern(fake_repo: Path) -> None:
    # list_filesがglobパターン("*.py")にマッチするファイルを一覧できることを確認する。
    output = tools.list_files(str(fake_repo), "*.py")
    assert output.strip() == str(fake_repo / "mail.py")


def test_list_files_is_non_recursive_by_default(fake_repo: Path) -> None:
    # list_filesはデフォルトでは非再帰(サブディレクトリの中は見ない)。
    # subディレクトリの中に置いたnested.pyが結果に含まれないことを確認する。
    subdir = fake_repo / "sub"
    subdir.mkdir()
    (subdir / "nested.py").write_text("x = 1\n")

    output = tools.list_files(str(fake_repo), "*.py")

    assert str(fake_repo / "mail.py") in output
    assert "nested.py" not in output


def test_list_files_recurses_with_double_star_pattern(fake_repo: Path) -> None:
    # パターンの先頭に "**/" を付けると再帰検索になり、サブディレクトリの
    # 中のファイルも見つかることを確認する。
    subdir = fake_repo / "sub"
    subdir.mkdir()
    (subdir / "nested.py").write_text("x = 1\n")

    output = tools.list_files(str(fake_repo), "**/*.py")

    assert str(fake_repo / "mail.py") in output
    assert str(subdir / "nested.py") in output


def test_list_files_rejects_parent_and_absolute_patterns(fake_repo: Path) -> None:
    # パスの脱出防止テストその1: globパターン自体が "../*.py"
    # (親ディレクトリへ辿ろうとする)や "/tmp/*.py"（絶対パス）である
    # 場合、そもそもパターンの検証段階(_validate_glob_pattern)で
    # 拒否されることを確認する。
    assert "[Error]" in tools.list_files(str(fake_repo), "../*.py")
    assert "[Error]" in tools.list_files(str(fake_repo), "/tmp/*.py")


def test_list_files_rejects_symlink_target_outside_testbed(
    fake_repo: Path, tmp_path: Path
) -> None:
    # パスの脱出防止テストその2、より巧妙なケース: パターン自体は普通の
    # "*.txt" でも、リポジトリ内に「リポジトリの外を指すシンボリック
    # リンク」が置かれていた場合、そのリンク先(outside.txt)まで
    # 辿って読めてしまってはいけない。_matching_files()がglobの
    # マッチ結果それぞれについてresolve()した実体のパスを検証している
    # ことを確認するテスト。
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    (fake_repo / "outside-link.txt").symlink_to(outside)

    output = tools.list_files(str(fake_repo), "*.txt")

    assert "[Error]" in output
    assert "outside" in output


def test_search_code_finds_pattern(fake_repo: Path) -> None:
    # search_codeが正規表現でファイル内を検索し、
    # "パス:行番号" 形式で結果を返すことを確認する基本のテスト。
    output = tools.search_code("is_valid_email", "*.py")
    assert f"{fake_repo / 'mail.py'}:1" in output


def test_search_code_rejects_parent_pattern(fake_repo: Path) -> None:
    # search_code側でも同じパス脱出防止(../を含むパターンの拒否)が
    # 効いていることを確認する。
    output = tools.search_code("anything", "../*.py")
    assert "[Error]" in output


def test_run_tests_rejects_evaluation_script_outside_testbed(
    fake_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # run_tests()が使う評価スクリプトのパスをAGENT_SMITH_EVAL_SCRIPT
    # 環境変数で指定できるが、それがTESTBED_PATHの外(/etc/passwd)を
    # 指していた場合は実行せず、"outside the repository root"という
    # 明示的なエラーを返すことを確認する。
    monkeypatch.setenv("AGENT_SMITH_EVAL_SCRIPT", "/etc/passwd")

    output = tools.run_tests()

    assert "[Error]" in output
    assert "outside the repository root" in output


def test_run_tests_uses_default_evaluation_script(
    fake_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # AGENT_SMITH_EVAL_SCRIPTが未設定の場合、デフォルトで
    # "<testbed>/eval.sh" を評価スクリプトとして使うことを確認する。
    # ここでは実際にfake_repo直下にeval.shを置き、その出力
    # ("evaluation-ok")がrun_tests()の戻り値に含まれることを確認している。
    monkeypatch.delenv("AGENT_SMITH_EVAL_SCRIPT", raising=False)
    eval_script = fake_repo / "eval.sh"
    eval_script.write_text("#!/bin/sh\nprintf 'evaluation-ok\\n'\n")
    eval_script.chmod(0o755)

    output = tools.run_tests()

    assert "evaluation-ok" in output


def test_docker_runner_places_evaluation_script_under_testbed() -> None:
    # docker_runner.py(Dockerブリッジ)がコンテナ内にeval.shを配置する
    # パスが、mcp_tools_swebench.pyがデフォルトで探しにいくパス
    # ("<testbed>/eval.sh")とちゃんと一致していることを確認する、
    # いわば「2つの独立したモジュール間の暗黙の契約」を検証するテスト。
    # この2つがズレると、コンテナ側は正しい場所にeval.shを置いたのに
    # ツール側が見つけられない、という食い違いバグになる。
    from docker_runner import EVAL_SCRIPT_PATH_IN_CONTAINER, TESTBED_PATH_IN_CONTAINER

    assert EVAL_SCRIPT_PATH_IN_CONTAINER == f"{TESTBED_PATH_IN_CONTAINER}/eval.sh"


def test_search_function_definition(fake_repo: Path) -> None:
    # search_function_or_class_definition_in_codeが、is_valid_emailという
    # 関数の"def"行そのものを正しく見つけられることを確認する。
    output = tools.search_function_or_class_definition_in_code("is_valid_email")
    assert f"{fake_repo / 'mail.py'}:1" in output


def test_find_references_includes_call_site(fake_repo: Path) -> None:
    # find_referencesが、is_valid_emailの定義行(1行目)と、それを呼んでいる
    # 箇所(send_email内)の両方、合計2箇所をヒットとして返すことを確認する。
    output = tools.find_references("is_valid_email", "", 0)
    lines = output.splitlines()
    assert len(lines) == 2  # definition + one call site


def test_find_references_excludes_declaration_when_location_given(fake_repo: Path) -> None:
    # filepathとlineを指定すると、その定義位置自体は結果から除外され、
    # 「本当の使用箇所」だけが返ることを確認する。定義そのものは
    # "使用"ではない、という区別をツールが正しく行っている証拠。
    output = tools.find_references("is_valid_email", str(fake_repo / "mail.py"), 1)
    lines = output.splitlines()
    assert len(lines) == 1  # only the call site, not the "def" line itself
    assert "def is_valid_email" not in output


def test_edit_file_applies_unique_replacement(fake_repo: Path) -> None:
    # edit_fileがold_str(完全一致する文字列)を1箇所だけ正しく
    # new_strに置き換え、実際にファイルの中身が変わっていることを確認する。
    result = tools.edit_file(str(fake_repo / "mail.py"), "return '@' in mail", "return mail.count('@') == 1")
    assert result.startswith("Edit applied")
    assert "mail.count" in (fake_repo / "mail.py").read_text()


def test_edit_file_rejects_ambiguous_match(fake_repo: Path) -> None:
    # old_strとして "mail" のような、ファイル中に複数回登場する曖昧な
    # 文字列を指定すると、どこを置換すべきか一意に決まらないため、
    # 実行せずに"not unique"というエラーを返すことを確認する。
    # あいまいな置換を許すと意図しない箇所を書き換えてしまう事故に
    # つながるため、あえて失敗させてLLMに前後関係を増やして
    # 書き直させる設計。
    result = tools.edit_file(str(fake_repo / "mail.py"), "mail", "email")
    assert "[Error]" in result
    assert "not unique" in result


def test_edit_file_reports_introduced_syntax_error(fake_repo: Path) -> None:
    # 置換自体は一意に決まって適用されたが、その結果としてPythonの
    # 構文が壊れてしまった場合(閉じ括弧忘れなど)、edit_fileは
    # 黙って壊れたファイルを残すのではなく、py_compileで検証した上で
    # [EditSyntaxError]という明示的なメッセージを返すことを確認する。
    result = tools.edit_file(str(fake_repo / "mail.py"), "return '@' in mail", "return '@' in mail(")
    assert "[EditSyntaxError]" in result


def test_get_patch_reflects_uncommitted_changes(fake_repo: Path) -> None:
    # edit_fileでファイルを変更した後、get_patch()を呼ぶと、
    # その変更内容が "diff --git" から始まる標準的なunified diff形式で
    # 正しく返ってくることを確認する。
    tools.edit_file(str(fake_repo / "mail.py"), "return '@' in mail", "return mail.count('@') == 1")
    patch = tools.get_patch()
    assert "diff --git" in patch
    assert "mail.count" in patch


def test_run_command_returns_exit_code_and_streams(fake_repo: Path) -> None:
    # run_commandが、標準出力・標準エラー出力・終了コードのすべてを
    # まとめて返すことを確認する。exit_codeが期待通り(3)になっている
    # ことも合わせて確認している。
    output = tools.run_command("echo hello && echo failed 1>&2 && exit 3")
    assert "exit_code: 3" in output
    assert "hello" in output
    assert "failed" in output


def test_path_traversal_outside_testbed_is_rejected(fake_repo: Path) -> None:
    # read_fileに絶対パスで/etc/passwdを渡しても、TESTBED_PATHの外なので
    # 拒否されることを確認する。パス脱出防止の最も基本的なケース。
    output = tools.read_file("/etc/passwd")
    assert "[Error]" in output


def test_run_command_output_is_capped(fake_repo: Path) -> None:
    # run_commandが30000文字もの巨大な出力を生成するコマンドを実行しても、
    # _cap_output()により出力サイズがTOOL_OUTPUT_LIMIT_CHARS
    # (20,000文字)程度に切り詰められ、[TruncatedToolOutput]という
    # 印が付くことを確認する。これが無いと、冗長なコマンド出力だけで
    # SWE-benchの累積トークン予算を大きく消費してしまう。
    output = tools.run_command("python3 -c \"print('x' * 30000)\"")
    assert len(output) <= tools.TOOL_OUTPUT_LIMIT_CHARS + 200
    assert "[TruncatedToolOutput]" in output


def test_search_code_output_is_capped(fake_repo: Path) -> None:
    # search_code側でも同様に、巨大なファイル(5000行)を検索して
    # 大量にヒットした場合、出力が切り詰められることを確認する。
    huge = fake_repo / "huge.py"
    huge.write_text("\n".join(f"x{i} = {i}  # marker" for i in range(5000)))
    output = tools.search_code("marker", "*.py")
    assert len(output) <= tools.TOOL_OUTPUT_LIMIT_CHARS + 200
    assert "[TruncatedToolOutput]" in output


def test_get_patch_is_never_truncated(fake_repo: Path) -> None:
    """get_patch()'s return value can be the literal final_answer() argument -
    truncating it would silently submit a corrupted, unappliable diff."""
    # 日本語解説: このテストは他の「出力は切り詰められる」テスト群とは
    # あえて逆のことを確認している。get_patch()の戻り値は
    # final_answer(get_patch())のように、LLMがそのまま最終提出物として
    # 使うことがある。もしここで機械的に切り詰めてしまったら、
    # 提出されるgitパッチが壊れて(git applyできない状態に)なって
    # しまう。そのため、TOOL_OUTPUT_LIMIT_CHARSの2倍もの巨大な変更を
    # あえて作り、それでもget_patch()の出力に[TruncatedToolOutput]という
    # 印が付かず、変更内容(huge_value)がそのまま丸ごと含まれていることを
    # 確認している。
    huge_value = "x" * (tools.TOOL_OUTPUT_LIMIT_CHARS * 2)
    tools.edit_file(str(fake_repo / "mail.py"), "return '@' in mail", f'return "{huge_value}" and True')
    patch = tools.get_patch()
    assert "[TruncatedToolOutput]" not in patch
    assert huge_value in patch
