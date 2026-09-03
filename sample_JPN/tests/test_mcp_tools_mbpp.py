"""mcp_tools_mbpp.pyのrun_testsツールのテスト。直接呼び出す形式
(@mcp.tool()デコレータを付けても元の関数はそのまま呼び出し可能なため、
MCPの通信レイヤーは不要)。"""
import json  # ツール実行結果のJSONデコードに使用

import pytest  # monkeypatch型ヒントに使用

from mcp_tools_mbpp import run_tests  # テスト対象のツール関数


def test_run_tests_uses_task_test_imports_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """MBPPタスクの中には、test_listの検証コードが候補解自体には含まれる理由のない
    importを必要とするものがある(例えば、解答自体は`math`に一切触れないのに
    テスト側だけが`math.isclose`を使うケース)。agent_mbpp.pyはこの環境変数経由で
    それらのimportをrun_tests()に渡すことで、LLMが偶然同じimportを自分の解答に
    追加してくれることに依存せずに済むようにし、NameErrorを防いでいる。"""
    # テスト実行時に必要な追加importをJSON配列として環境変数に設定
    monkeypatch.setenv("AGENT_SMITH_TEST_IMPORTS", json.dumps(["import math"]))
    code = "def volume_sphere(r):\n    return (4 / 3) * 3.141592653589793 * r ** 3\n"
    result = json.loads(run_tests(code, ["assert math.isclose(volume_sphere(1), 4.1887902047863905)"]))
    assert result["success"] is True  # mathがimportされているためテストが成功すること


def test_run_tests_without_test_imports_env_var_is_unaffected(monkeypatch: pytest.MonkeyPatch) -> None:
    # 環境変数AGENT_SMITH_TEST_IMPORTSが未設定でも、通常のテストは問題なく動作することを検証
    monkeypatch.delenv("AGENT_SMITH_TEST_IMPORTS", raising=False)  # 環境変数が存在しないことを保証
    code = "def add(a, b):\n    return a + b\n"
    result = json.loads(run_tests(code, ["assert add(2, 3) == 5"]))
    assert result["success"] is True  # 通常通りテストが成功すること


def test_run_tests_all_pass() -> None:
    # 複数のassertが全て通る正しい実装の場合、成功と報告されることを検証
    code = "def add(a, b):\n    return a + b\n"
    result = json.loads(run_tests(code, ["assert add(2, 3) == 5", "assert add(-1, 1) == 0"]))
    assert result["success"] is True  # 全てのassertが成功すること


def test_run_tests_failure_reports_output() -> None:
    # 誤った実装(引き算になっている)の場合、失敗と報告され、
    # 出力にエラー内容が含まれることを検証
    code = "def add(a, b):\n    return a - b\n"
    result = json.loads(run_tests(code, ["assert add(2, 3) == 5"]))
    assert result["success"] is False  # assertが失敗すること
    assert "AssertionError" in result["output"] or "Error" in result["output"]  # エラー内容が出力に含まれること


def test_run_tests_syntax_error_in_candidate() -> None:
    # 候補コードに構文エラーがある場合、失敗と報告されることを検証
    code = "def add(a, b)\n    return a + b\n"  # コロンが抜けている構文エラー
    result = json.loads(run_tests(code, ["assert add(2, 3) == 5"]))
    assert result["success"] is False  # 構文エラーのため失敗すること


def test_run_tests_infinite_loop_times_out() -> None:
    # 無限ループを含むコードがタイムアウトによって停止し、失敗と報告されることを検証
    code = "def add(a, b):\n    while True:\n        pass\n"
    result = json.loads(run_tests(code, ["assert add(2, 3) == 5"]))
    assert result["success"] is False  # タイムアウトのため失敗すること
    assert "timed out" in result["output"]  # 出力にタイムアウトを示す文言が含まれること


def test_run_tests_rejects_unauthorized_host_import() -> None:
    # サンドボックス外(ホスト環境)へのアクセスを可能にするosモジュールのimportが
    # 拒否され、SandboxViolationとして報告されることを検証
    code = "import os\ndef cwd():\n    return os.getcwd()\n"
    result = json.loads(run_tests(code, ["assert cwd()"]))
    assert result["success"] is False  # 許可されていないimportのため失敗すること
    assert "SandboxViolation" in result["output"]  # サンドボックス違反として報告されること
