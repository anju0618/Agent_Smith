"""Tests for mcp_tools_mbpp.py's run_tests tool, called directly (the @mcp.tool()
decorator leaves the underlying function callable - no MCP transport needed)."""
# ============================================================================
# 日本語解説: このファイルは mcp_tools_mbpp.py の run_tests ツール
# （候補コードを公開テストに対して実行し、合否を判定する唯一のツール）を
# テストしています。ファイル冒頭のコメントにある通り、@mcp.tool()という
# デコレータは元のPython関数をそのまま呼び出し可能な状態に保つため、
# MCPサーバーを実際に起動してstdio/HTTP経由で通信しなくても、
# from mcp_tools_mbpp import run_tests として直接関数を呼ぶだけで
# テストできます（test_mcp_client.pyとは違い、こちらは「MCPプロトコルの
# 皮を被せる前の、中身のロジックそのもの」をテストしていると考えると
# 分かりやすいです）。
# ============================================================================
import json

import pytest

from mcp_tools_mbpp import run_tests


def test_run_tests_uses_task_test_imports_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """Some MBPP tasks' test_list needs an import the candidate solution has
    no reason to include itself (e.g. `math.isclose` on a task whose own
    solution never touches `math`) - agent_mbpp.py passes those through this
    env var so run_tests() doesn't NameError instead of silently depending on
    the LLM happening to add the same import for its own unrelated reasons."""
    # 日本語解説: MBPPのタスクによっては、公開テストのassert文側だけが
    # math.iscloseのようなimportを必要としていて、候補コード自身は
    # そのモジュールに一切触れない、というケースがあります。もし候補コードが
    # たまたま同じimportを書いていなければ、テスト実行時にNameErrorに
    # なってしまいます。これを防ぐため、agent_mbpp.pyはタスク定義の
    # test_importsフィールドを環境変数AGENT_SMITH_TEST_IMPORTSとして
    # run_tests()に渡し、run_tests()側が候補コードの前に自動的に
    # importを前置します。このテストはその仕組みが実際に機能していることを
    # 確認しています（"import math"を環境変数経由で渡し、候補コード自体は
    # mathに一切触れていないのにmath.iscloseを使うテストが通ることを確認）。
    monkeypatch.setenv("AGENT_SMITH_TEST_IMPORTS", json.dumps(["import math"]))
    code = "def volume_sphere(r):\n    return (4 / 3) * 3.141592653589793 * r ** 3\n"
    result = json.loads(run_tests(code, ["assert math.isclose(volume_sphere(1), 4.1887902047863905)"]))
    assert result["success"] is True


def test_run_tests_without_test_imports_env_var_is_unaffected(monkeypatch: pytest.MonkeyPatch) -> None:
    # 環境変数AGENT_SMITH_TEST_IMPORTSが設定されていない(通常のケース)場合
    # にも、普通に動作すること(過剰な前提を持ち込んでいないこと)を確認する。
    monkeypatch.delenv("AGENT_SMITH_TEST_IMPORTS", raising=False)
    code = "def add(a, b):\n    return a + b\n"
    result = json.loads(run_tests(code, ["assert add(2, 3) == 5"]))
    assert result["success"] is True


def test_run_tests_all_pass() -> None:
    # 正常系の基本形: 複数のassert文がすべて通れば success=True になる。
    code = "def add(a, b):\n    return a + b\n"
    result = json.loads(run_tests(code, ["assert add(2, 3) == 5", "assert add(-1, 1) == 0"]))
    assert result["success"] is True


def test_run_tests_failure_reports_output() -> None:
    # 候補コードが間違っている(引き算をしている)場合、success=Falseに
    # なり、outputフィールドにAssertionErrorの内容が含まれる。
    # 「失敗した」という結果だけでなく「なぜ失敗したか」までLLMに
    # 見える形で返すことを確認している。
    code = "def add(a, b):\n    return a - b\n"
    result = json.loads(run_tests(code, ["assert add(2, 3) == 5"]))
    assert result["success"] is False
    assert "AssertionError" in result["output"] or "Error" in result["output"]


def test_run_tests_syntax_error_in_candidate() -> None:
    # 候補コード自体に構文エラー(defの後にコロン忘れ)がある場合も、
    # run_tests()がクラッシュせず success=False という形で正しく処理する。
    code = "def add(a, b)\n    return a + b\n"
    result = json.loads(run_tests(code, ["assert add(2, 3) == 5"]))
    assert result["success"] is False


def test_run_tests_infinite_loop_times_out() -> None:
    # 候補コードが無限ループを含んでいる場合でも、run_tests()内部の
    # サンドボックスに設定された10秒タイムアウトによって処理が
    # 打ち切られ、success=False、"timed out"という文言がoutputに
    # 含まれることを確認する。これによりMCPサーバー自体がハングして
    # エージェント全体を無期限に止めてしまうことを防いでいる。
    code = "def add(a, b):\n    while True:\n        pass\n"
    result = json.loads(run_tests(code, ["assert add(2, 3) == 5"]))
    assert result["success"] is False
    assert "timed out" in result["output"]


def test_run_tests_rejects_unauthorized_host_import() -> None:
    # 候補コードが許可されていないモジュール(os)をimportしようとしても、
    # run_tests()自身が内部で別立てしているサンドボックス
    # (allowed_directories=[]の使い捨てサンドボックス)が正しく
    # [SandboxViolation]としてブロックすることを確認する。つまり
    # run_tests()というMCPツール自体も、エージェント本体と同じ
    # 多層防御(Section 8)の中で候補コードを実行しているという証拠。
    code = "import os\ndef cwd():\n    return os.getcwd()\n"
    result = json.loads(run_tests(code, ["assert cwd()"]))
    assert result["success"] is False
    assert "SandboxViolation" in result["output"]
