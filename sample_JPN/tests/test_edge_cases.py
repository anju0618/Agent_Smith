"""サンプルとなる各コンポーネントに共通する、境界値・不正入力のテスト。"""
import json  # JSONのシリアライズ確認に使用

import pytest  # pytest.raisesや型ヒントに使用

from code_extraction import extract_code  # コード抽出関数
from config import ProviderSpec, _env_var_from_url, resolve_provider  # プロバイダ設定関連
from models import MBPPTaskInput, SandboxConfig, SWEBenchTaskInput  # データモデル
from sandbox.executor import Sandbox  # サンドボックス実行環境


def test_extraction_handles_empty_and_whitespace_only_output() -> None:
    # 空文字列や空白のみの文字列を渡した場合、コードなしとして扱われることを検証
    for value in ("", "   \n\t"):
        result = extract_code(value)
        assert result.code is None  # コードは抽出されない
        assert "[NoCodeBlock]" in result.note  # コードブロックなしという注記が付くこと


def test_extraction_rejects_malformed_alternate_formats() -> None:
    # 各種の代替フォーマット(tool_call/Action/invoke)が壊れている場合の挙動を検証
    # 壊れたJSONのtool_callはコード抽出に失敗する
    assert extract_code("<tool_call>{bad}</tool_call>").code is None
    # ReAct形式のAction Inputが不正なJSONでも、文字列としてそのまま扱われフォールバックする
    result = extract_code('Action: search_code\nAction Input: {"bad"}')
    assert result.code is not None  # コード自体は生成される(フォールバック)
    assert 'value=\'{"bad"}\'' in result.code  # 元の不正な文字列がそのまま値として渡される
    # 閉じタグのない不完全なinvoke/parameterはコード抽出に失敗する
    assert extract_code('<invoke name="x"><parameter name="a">').code is None


def test_extraction_preserves_json_null_boolean_and_numbers() -> None:
    # JSONのnull/true/数値がPythonのNone/True/intに正しく変換されることを検証
    result = extract_code(
        '<tool_call>{"name":"tool","arguments":{"a":null,"b":true,"c":1}}</tool_call>'
    )
    assert result.code == "result = tool(a=None, b=True, c=1)\nprint(result)"


def test_provider_url_normalization_and_environment_name() -> None:
    # プロバイダURLの末尾スラッシュ有無を吸収して正規化されること、
    # URLから環境変数名が正しく生成されることを検証
    assert resolve_provider("https://api.groq.com/openai/v1/").name == "groq"
    assert _env_var_from_url("https://example.com:8443/api") == "EXAMPLE_COM_8443_API_KEY"


def test_provider_key_collection_stops_at_first_missing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # APIキーは連番の環境変数(EDGE_KEY, EDGE_KEY_2, EDGE_KEY_3, ...)から集められるが、
    # 途中の番号(EDGE_KEY_2)が欠けていたらそこで収集を打ち切ることを検証
    monkeypatch.setenv("EDGE_KEY", "one")  # 1つ目のキーは設定
    monkeypatch.setenv("EDGE_KEY_3", "three")  # 2つ目を飛ばして3つ目だけ設定(無視されるはず)
    assert ProviderSpec("edge", "https://example.invalid", "EDGE_KEY").collect_api_keys() == ["one"]


def test_sandbox_empty_code_and_missing_name_are_explicit() -> None:
    # サンドボックスに空コードや未定義変数参照を渡した場合、
    # 明示的なエラーメッセージが返されることを検証
    sandbox = Sandbox(
        SandboxConfig(authorized_imports=[], allowed_directories=[]),
        apply_process_memory_limit=False,
    )
    assert "[NoCodeBlock]" in sandbox.run("   ")  # 空白のみのコードは「コードなし」エラー
    assert "NameError" in sandbox.run("print(missing_name)")  # 未定義変数はNameError


def test_sandbox_default_getattr_is_honored() -> None:
    # サンドボックス内でも組み込みgetattr()にデフォルト値引数が正しく機能することを検証
    sandbox = Sandbox(
        SandboxConfig(authorized_imports=[], allowed_directories=[]),
        apply_process_memory_limit=False,
    )
    assert sandbox.run("print(getattr(object(), 'missing', None))").strip() == "None"


def test_models_reject_missing_required_fields() -> None:
    # 必須フィールドが欠けたデータでモデルをバリデーションすると例外が発生することを検証
    with pytest.raises(Exception):
        MBPPTaskInput.model_validate({})
    with pytest.raises(Exception):
        SWEBenchTaskInput.model_validate({})


def test_models_round_trip_unicode_and_empty_optional_fields() -> None:
    # Unicode文字列(日本語相当の文字)や空のオプションフィールドを含むデータが、
    # モデルへの変換・JSONへのダンプを通じて問題なく往復できることを検証
    task = MBPPTaskInput.model_validate(
        {
            "task_id": 0,
            "task_definition": "文字列を処理する",  # Unicode文字列を含むタスク定義
            "function_definition": "def solve(x):",
            "test_list": [],  # 空のテストリスト(オプション相当)
        }
    )
    assert json.loads(task.model_dump_json())["task_id"] == 0  # JSON化しても値が保持されること
