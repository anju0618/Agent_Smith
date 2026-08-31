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


def test_list_files_is_non_recursive_by_default(fake_repo: Path) -> None:
    subdir = fake_repo / "sub"
    subdir.mkdir()
    (subdir / "nested.py").write_text("x = 1\n")

    output = tools.list_files(str(fake_repo), "*.py")

    assert str(fake_repo / "mail.py") in output
    assert "nested.py" not in output


def test_list_files_recurses_with_double_star_pattern(fake_repo: Path) -> None:
    subdir = fake_repo / "sub"
    subdir.mkdir()
    (subdir / "nested.py").write_text("x = 1\n")

    output = tools.list_files(str(fake_repo), "**/*.py")

    assert str(fake_repo / "mail.py") in output
    assert str(subdir / "nested.py") in output


def test_list_files_rejects_parent_and_absolute_patterns(fake_repo: Path) -> None:
    assert "[Error]" in tools.list_files(str(fake_repo), "../*.py")
    assert "[Error]" in tools.list_files(str(fake_repo), "/tmp/*.py")


def test_list_files_rejects_symlink_target_outside_testbed(
    fake_repo: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    (fake_repo / "outside-link.txt").symlink_to(outside)

    output = tools.list_files(str(fake_repo), "*.txt")

    assert "[Error]" in output
    assert "outside" in output


def test_search_code_finds_pattern(fake_repo: Path) -> None:
    output = tools.search_code("is_valid_email", "*.py")
    assert f"{fake_repo / 'mail.py'}:1" in output


def test_search_code_rejects_parent_pattern(fake_repo: Path) -> None:
    output = tools.search_code("anything", "../*.py")
    assert "[Error]" in output


def test_search_function_definition(fake_repo: Path) -> None:
    output = tools.search_function_or_class_definition_in_code("is_valid_email")
    assert f"{fake_repo / 'mail.py'}:1" in output


def test_find_references_includes_call_site(fake_repo: Path) -> None:
    output = tools.find_references("is_valid_email", "", 0)
    lines = output.splitlines()
    assert len(lines) == 2  # definition + one call site


def test_find_references_excludes_declaration_when_location_given(fake_repo: Path) -> None:
    output = tools.find_references("is_valid_email", str(fake_repo / "mail.py"), 1)
    lines = output.splitlines()
    assert len(lines) == 1  # only the call site, not the "def" line itself
    assert "def is_valid_email" not in output


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


def test_run_command_output_is_capped(fake_repo: Path) -> None:
    output = tools.run_command("python3 -c \"print('x' * 30000)\"")
    assert len(output) <= tools.TOOL_OUTPUT_LIMIT_CHARS + 200
    assert "[TruncatedToolOutput]" in output


def test_search_code_output_is_capped(fake_repo: Path) -> None:
    huge = fake_repo / "huge.py"
    huge.write_text("\n".join(f"x{i} = {i}  # marker" for i in range(5000)))
    output = tools.search_code("marker", "*.py")
    assert len(output) <= tools.TOOL_OUTPUT_LIMIT_CHARS + 200
    assert "[TruncatedToolOutput]" in output


def test_get_patch_is_never_truncated(fake_repo: Path) -> None:
    """get_patch()'s return value can be the literal final_answer() argument -
    truncating it would silently submit a corrupted, unappliable diff."""
    huge_value = "x" * (tools.TOOL_OUTPUT_LIMIT_CHARS * 2)
    tools.edit_file(str(fake_repo / "mail.py"), "return '@' in mail", f'return "{huge_value}" and True')
    patch = tools.get_patch()
    assert "[TruncatedToolOutput]" not in patch
    assert huge_value in patch
