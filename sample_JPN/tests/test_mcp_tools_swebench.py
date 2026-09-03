"""mcp_tools_swebench.pyの必須ツール群(セクション4.5)のテスト。
TESTBED_PATHを起点とした小さな偽リポジトリに対して、各ツールを直接呼び出して検証する。"""
import subprocess  # gitコマンドやシェルコマンドの実行に使用
from pathlib import Path  # ファイルパス操作に使用

import pytest  # fixture定義・monkeypatchに使用

import mcp_tools_swebench as tools  # テスト対象のツール群を含むモジュール


@pytest.fixture()
def fake_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # テスト用の小さなgitリポジトリを一時ディレクトリに作成するfixture
    repo = tmp_path / "repo"
    repo.mkdir()
    # メール検証・送信を行うだけの単純なPythonファイルを配置
    (repo / "mail.py").write_text(
        "def is_valid_email(mail):\n"
        "    return '@' in mail\n"
        "\n"
        "def send_email(mail):\n"
        "    if is_valid_email(mail):\n"
        "        return True\n"
        "    return False\n"
    )
    # gitリポジトリとして初期化し、最初のコミットを作成する
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "a@a.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "a"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    # ツール群がこのリポジトリをテストベッドとして認識するよう環境変数を設定
    monkeypatch.setenv("TESTBED_PATH", str(repo))
    return repo


def test_read_file_returns_cat_n_style(fake_repo: Path) -> None:
    # read_fileが「行番号: 内容」という cat -n 形式で出力することを検証
    output = tools.read_file(str(fake_repo / "mail.py"), start_line=1, end_line=2)
    assert output == "1: def is_valid_email(mail):\n2:     return '@' in mail"


def test_list_files_matches_pattern(fake_repo: Path) -> None:
    # list_filesがglobパターンに一致するファイルを返すことを検証
    output = tools.list_files(str(fake_repo), "*.py")
    assert output.strip() == str(fake_repo / "mail.py")


def test_list_files_is_non_recursive_by_default(fake_repo: Path) -> None:
    # デフォルトではlist_filesがサブディレクトリを再帰的に探索しないことを検証
    subdir = fake_repo / "sub"
    subdir.mkdir()
    (subdir / "nested.py").write_text("x = 1\n")  # サブディレクトリ内のファイル

    output = tools.list_files(str(fake_repo), "*.py")

    assert str(fake_repo / "mail.py") in output  # 直下のファイルは含まれる
    assert "nested.py" not in output  # サブディレクトリ内のファイルは含まれない


def test_list_files_recurses_with_double_star_pattern(fake_repo: Path) -> None:
    # "**/*.py"のような二重アスタリスクパターンを使うと再帰的に探索されることを検証
    subdir = fake_repo / "sub"
    subdir.mkdir()
    (subdir / "nested.py").write_text("x = 1\n")

    output = tools.list_files(str(fake_repo), "**/*.py")

    assert str(fake_repo / "mail.py") in output  # 直下のファイルも含まれる
    assert str(subdir / "nested.py") in output  # サブディレクトリ内のファイルも含まれる


def test_list_files_rejects_parent_and_absolute_patterns(fake_repo: Path) -> None:
    # 親ディレクトリへの脱出("../")や絶対パスを使ったパターンが拒否されることを検証
    assert "[Error]" in tools.list_files(str(fake_repo), "../*.py")
    assert "[Error]" in tools.list_files(str(fake_repo), "/tmp/*.py")


def test_list_files_rejects_symlink_target_outside_testbed(
    fake_repo: Path, tmp_path: Path
) -> None:
    # テストベッドの外を指すシンボリックリンクが拒否されることを検証(サンドボックス脱出防止)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    (fake_repo / "outside-link.txt").symlink_to(outside)  # テストベッド外へのシンボリックリンク

    output = tools.list_files(str(fake_repo), "*.txt")

    assert "[Error]" in output  # エラーとして扱われること
    assert "outside" in output  # テストベッド外を指している旨のメッセージが含まれること


def test_search_code_finds_pattern(fake_repo: Path) -> None:
    # search_codeが指定パターンにマッチする行をファイル名:行番号の形式で返すことを検証
    output = tools.search_code("is_valid_email", "*.py")
    assert f"{fake_repo / 'mail.py'}:1" in output


def test_search_code_rejects_parent_pattern(fake_repo: Path) -> None:
    # search_codeでも親ディレクトリへの脱出パターンが拒否されることを検証
    output = tools.search_code("anything", "../*.py")
    assert "[Error]" in output


def test_run_tests_rejects_evaluation_script_outside_testbed(
    fake_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 評価スクリプトのパスがテストベッドの外を指している場合、拒否されることを検証
    monkeypatch.setenv("AGENT_SMITH_EVAL_SCRIPT", "/etc/passwd")

    output = tools.run_tests()

    assert "[Error]" in output  # エラーとして扱われること
    assert "outside the repository root" in output  # リポジトリ外である旨のメッセージが含まれること


def test_run_tests_uses_default_evaluation_script(
    fake_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 環境変数が未設定の場合、テストベッド直下のデフォルトのeval.shが使われることを検証
    monkeypatch.delenv("AGENT_SMITH_EVAL_SCRIPT", raising=False)
    eval_script = fake_repo / "eval.sh"
    eval_script.write_text("#!/bin/sh\nprintf 'evaluation-ok\\n'\n")  # 実行結果を出力するだけの簡単なスクリプト
    eval_script.chmod(0o755)  # 実行権限を付与

    output = tools.run_tests()

    assert "evaluation-ok" in output  # デフォルトの評価スクリプトが実行されたことの確認


def test_docker_runner_places_evaluation_script_under_testbed() -> None:
    # docker_runner側の定数が、評価スクリプトをコンテナ内テストベッド配下に
    # 配置する設定になっていることを検証
    from docker_runner import EVAL_SCRIPT_PATH_IN_CONTAINER, TESTBED_PATH_IN_CONTAINER

    assert EVAL_SCRIPT_PATH_IN_CONTAINER == f"{TESTBED_PATH_IN_CONTAINER}/eval.sh"


def test_search_function_definition(fake_repo: Path) -> None:
    # search_function_or_class_definition_in_codeが関数定義の位置を正しく見つけることを検証
    output = tools.search_function_or_class_definition_in_code("is_valid_email")
    assert f"{fake_repo / 'mail.py'}:1" in output


def test_find_references_includes_call_site(fake_repo: Path) -> None:
    # find_referencesが定義箇所と呼び出し箇所の両方を含めて返すことを検証
    output = tools.find_references("is_valid_email", "", 0)
    lines = output.splitlines()
    assert len(lines) == 2  # 定義行 + 呼び出し箇所1件、合計2行になるはず


def test_find_references_excludes_declaration_when_location_given(fake_repo: Path) -> None:
    # 定義箇所自身の位置(ファイル名・行番号)を指定した場合、その定義行自体は
    # 結果から除外され、呼び出し箇所だけが返ることを検証
    output = tools.find_references("is_valid_email", str(fake_repo / "mail.py"), 1)
    lines = output.splitlines()
    assert len(lines) == 1  # 呼び出し箇所のみ、"def"行自体は含まれないはず
    assert "def is_valid_email" not in output


def test_edit_file_applies_unique_replacement(fake_repo: Path) -> None:
    # edit_fileが一意にマッチする文字列を正しく置換し、ファイルに反映することを検証
    result = tools.edit_file(str(fake_repo / "mail.py"), "return '@' in mail", "return mail.count('@') == 1")
    assert result.startswith("Edit applied")  # 適用成功のメッセージで始まること
    assert "mail.count" in (fake_repo / "mail.py").read_text()  # ファイル内容が実際に置換されていること


def test_edit_file_rejects_ambiguous_match(fake_repo: Path) -> None:
    # 置換対象の文字列が複数箇所にマッチして一意に定まらない場合、拒否されることを検証
    result = tools.edit_file(str(fake_repo / "mail.py"), "mail", "email")
    assert "[Error]" in result  # エラーとして扱われること
    assert "not unique" in result  # 一意でない旨のメッセージが含まれること


def test_edit_file_reports_introduced_syntax_error(fake_repo: Path) -> None:
    # 置換によって構文エラーが発生してしまう場合、それが検出され報告されることを検証
    result = tools.edit_file(str(fake_repo / "mail.py"), "return '@' in mail", "return '@' in mail(")
    assert "[EditSyntaxError]" in result  # 構文エラーとして報告されること


def test_get_patch_reflects_uncommitted_changes(fake_repo: Path) -> None:
    # edit_fileによる未コミットの変更が、get_patch()の差分に反映されることを検証
    tools.edit_file(str(fake_repo / "mail.py"), "return '@' in mail", "return mail.count('@') == 1")
    patch = tools.get_patch()
    assert "diff --git" in patch  # git diff形式のパッチであること
    assert "mail.count" in patch  # 変更内容がパッチに含まれること


def test_run_command_returns_exit_code_and_streams(fake_repo: Path) -> None:
    # run_commandが標準出力・標準エラー出力・終了コードをまとめて返すことを検証
    output = tools.run_command("echo hello && echo failed 1>&2 && exit 3")
    assert "exit_code: 3" in output  # 終了コードが記録されていること
    assert "hello" in output  # 標準出力の内容が含まれること
    assert "failed" in output  # 標準エラー出力の内容も含まれること


def test_path_traversal_outside_testbed_is_rejected(fake_repo: Path) -> None:
    # テストベッド外の絶対パス(/etc/passwd)へのアクセスが拒否されることを検証
    output = tools.read_file("/etc/passwd")
    assert "[Error]" in output


def test_run_command_output_is_capped(fake_repo: Path) -> None:
    # コマンド出力が上限文字数で切り詰められ、その旨が明記されることを検証
    output = tools.run_command("python3 -c \"print('x' * 30000)\"")
    assert len(output) <= tools.TOOL_OUTPUT_LIMIT_CHARS + 200  # 上限+マージン程度に収まっていること
    assert "[TruncatedToolOutput]" in output  # 切り詰められたことを示す注記があること


def test_search_code_output_is_capped(fake_repo: Path) -> None:
    # search_codeの出力も同様に上限文字数で切り詰められることを検証
    huge = fake_repo / "huge.py"
    huge.write_text("\n".join(f"x{i} = {i}  # marker" for i in range(5000)))  # 大量のマッチ行を作る
    output = tools.search_code("marker", "*.py")
    assert len(output) <= tools.TOOL_OUTPUT_LIMIT_CHARS + 200  # 上限+マージン程度に収まっていること
    assert "[TruncatedToolOutput]" in output  # 切り詰められたことを示す注記があること


def test_get_patch_is_never_truncated(fake_repo: Path) -> None:
    """get_patch()の戻り値は、そのままfinal_answer()の引数になり得る -
    もし切り詰めてしまうと、壊れて適用不可能なdiffを黙って提出することになってしまう。"""
    huge_value = "x" * (tools.TOOL_OUTPUT_LIMIT_CHARS * 2)  # 出力上限の2倍の長さの巨大な値
    tools.edit_file(str(fake_repo / "mail.py"), "return '@' in mail", f'return "{huge_value}" and True')
    patch = tools.get_patch()
    assert "[TruncatedToolOutput]" not in patch  # get_patchの結果は切り詰められないこと
    assert huge_value in patch  # 巨大な値が完全にそのままパッチに含まれていること
