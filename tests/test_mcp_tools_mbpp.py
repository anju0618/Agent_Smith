"""mcp_tools_mbpp.pyのrun_testsツールのテスト。直接呼び出す形式
(@mcp.tool()デコレータを付けても元の関数はそのまま呼び出し可能なため、
MCPの通信レイヤーは不要)。"""
import json

import pytest

from mcp_tools_mbpp import run_tests


def test_run_tests_uses_task_test_imports_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """MBPPタスクの中には、test_listの検証コードが候補解自体には含まれる理由のない
    importを必要とするものがある(例えば、解答自体は`math`に一切触れないのに
    テスト側だけが`math.isclose`を使うケース)。agent_mbpp.pyはこの環境変数経由で
    それらのimportをrun_tests()に渡すことで、LLMが偶然同じimportを自分の解答に
    追加してくれることに依存せずに済むようにし、NameErrorを防いでいる。"""

    monkeypatch.setenv("AGENT_SMITH_TEST_IMPORTS", json.dumps(["import math"]))
    code = "def volume_sphere(r):\n    return (4 / 3) * 3.141592653589793 * r ** 3\n"
    result = json.loads(run_tests(code, ["assert math.isclose(volume_sphere(1), 4.1887902047863905)"]))
    assert result["success"] is True


def test_run_tests_without_test_imports_env_var_is_unaffected(monkeypatch: pytest.MonkeyPatch) -> None:

    monkeypatch.delenv("AGENT_SMITH_TEST_IMPORTS", raising=False)
    code = "def add(a, b):\n    return a + b\n"
    result = json.loads(run_tests(code, ["assert add(2, 3) == 5"]))
    assert result["success"] is True


def test_run_tests_all_pass() -> None:

    code = "def add(a, b):\n    return a + b\n"
    result = json.loads(run_tests(code, ["assert add(2, 3) == 5", "assert add(-1, 1) == 0"]))
    assert result["success"] is True


def test_run_tests_failure_reports_output() -> None:


    code = "def add(a, b):\n    return a - b\n"
    result = json.loads(run_tests(code, ["assert add(2, 3) == 5"]))
    assert result["success"] is False
    assert "AssertionError" in result["output"] or "Error" in result["output"]


def test_run_tests_syntax_error_in_candidate() -> None:

    code = "def add(a, b)\n    return a + b\n"
    result = json.loads(run_tests(code, ["assert add(2, 3) == 5"]))
    assert result["success"] is False


def test_run_tests_infinite_loop_times_out() -> None:

    code = "def add(a, b):\n    while True:\n        pass\n"
    result = json.loads(run_tests(code, ["assert add(2, 3) == 5"]))
    assert result["success"] is False
    assert "timed out" in result["output"]


def test_run_tests_rejects_unauthorized_host_import() -> None:


    code = "import os\ndef cwd():\n    return os.getcwd()\n"
    result = json.loads(run_tests(code, ["assert cwd()"]))
    assert result["success"] is False
    assert "SandboxViolation" in result["output"]
