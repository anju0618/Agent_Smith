"""Tests for mcp_tools_swebench.py's mandatory tools (Section 4.5), called
directly against a small fake repository rooted at TESTBED_PATH."""
import subprocess
from pathlib import Path

import pytest

import mcp_tools_swebench as tools


@pytest.fixture()
def fake_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
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
    output = tools.read_file(str(fake_repo / "mail.py"), start_line=1, end_line=2)
    assert output == "1: def is_valid_email(mail):\n2:     return '@' in mail"


def test_list_files_matches_pattern(fake_repo: Path) -> None:
    output = tools.list_files(str(fake_repo), "*.py")
    assert output.strip() == str(fake_repo / "mail.py")


def test_search_code_finds_pattern(fake_repo: Path) -> None:
    output = tools.search_code("is_valid_email", "*.py")
    assert f"{fake_repo / 'mail.py'}:1" in output


def test_search_function_definition(fake_repo: Path) -> None:
    output = tools.search_function_or_class_definition_in_code("is_valid_email")
    assert f"{fake_repo / 'mail.py'}:1" in output


def test_find_references_includes_call_site(fake_repo: Path) -> None:
    output = tools.find_references("is_valid_email", "", 0)
    lines = output.splitlines()
    assert len(lines) == 2  # definition + one call site


def test_edit_file_applies_unique_replacement(fake_repo: Path) -> None:
    result = tools.edit_file(str(fake_repo / "mail.py"), "return '@' in mail", "return mail.count('@') == 1")
    assert result.startswith("Edit applied")
    assert "mail.count" in (fake_repo / "mail.py").read_text()


def test_edit_file_rejects_ambiguous_match(fake_repo: Path) -> None:
    result = tools.edit_file(str(fake_repo / "mail.py"), "mail", "email")
    assert "[Error]" in result
    assert "not unique" in result


def test_edit_file_reports_introduced_syntax_error(fake_repo: Path) -> None:
    result = tools.edit_file(str(fake_repo / "mail.py"), "return '@' in mail", "return '@' in mail(")
    assert "[EditSyntaxError]" in result


def test_get_patch_reflects_uncommitted_changes(fake_repo: Path) -> None:
    tools.edit_file(str(fake_repo / "mail.py"), "return '@' in mail", "return mail.count('@') == 1")
    patch = tools.get_patch()
    assert "diff --git" in patch
    assert "mail.count" in patch


def test_run_command_returns_exit_code_and_streams(fake_repo: Path) -> None:
    output = tools.run_command("echo hello && echo failed 1>&2 && exit 3")
    assert "exit_code: 3" in output
    assert "hello" in output
    assert "failed" in output


def test_path_traversal_outside_testbed_is_rejected(fake_repo: Path) -> None:
    output = tools.read_file("/etc/passwd")
    assert "[Error]" in output
