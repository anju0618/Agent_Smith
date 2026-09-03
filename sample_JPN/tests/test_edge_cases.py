"""Boundary and malformed-input tests shared across the sample components."""
# ============================================================================
# 日本語解説: このファイルは特定の1モジュールに専念するのではなく、
# code_extraction.py / config.py / models.py / sandbox/executor.py という
# 複数のコンポーネントにまたがる「境界値・エッジケース」を集めた
# テストファイルです。空文字列、壊れたJSON、必須フィールドの欠落、
# 歯抜けの環境変数番号など、「普通の正常系テストでは通らないような
# 半端な入力」に対して、各コンポーネントが暗黙に何かを推測して
# 動くのではなく、常に一貫した明示的な挙動を返すことを確認しています。
# ============================================================================
import json

import pytest

from code_extraction import extract_code
from config import ProviderSpec, _env_var_from_url, resolve_provider
from models import MBPPTaskInput, SandboxConfig, SWEBenchTaskInput
from sandbox.executor import Sandbox


def test_extraction_handles_empty_and_whitespace_only_output() -> None:
    # LLMが空文字列や空白だけを返してきた場合、extract_code()が
    # 例外を投げたりcrashしたりせず、code=Noneと[NoCodeBlock]という
    # 明示的な結果を一貫して返すことを確認する。
    for value in ("", "   \n\t"):
        result = extract_code(value)
        assert result.code is None
        assert "[NoCodeBlock]" in result.note


def test_extraction_rejects_malformed_alternate_formats() -> None:
    # 3種類の壊れた入力を確認する:
    #   1. JSON自体が壊れている<tool_call>{bad}</tool_call> → code=None
    #   2. ReAct形式のAction Inputが壊れたJSON({"bad"}) → JSONとして
    #      パースできないので、文字列全体をvalue引数として扱う
    #      フォールバックに落ちる（'{"bad"}'という文字列そのものが
    #      value=として渡される）
    #   3. XML <invoke>タグが閉じられていない場合 → code=None
    assert extract_code("<tool_call>{bad}</tool_call>").code is None
    result = extract_code('Action: search_code\nAction Input: {"bad"}')
    assert result.code is not None
    assert 'value=\'{"bad"}\'' in result.code
    assert extract_code('<invoke name="x"><parameter name="a">').code is None


def test_extraction_preserves_json_null_boolean_and_numbers() -> None:
    # JSON/Hermes形式で渡された値の型(null, true, 数値)が、
    # Pythonの等価な値(None, True, 1)に正しく変換されることを確認する。
    result = extract_code(
        '<tool_call>{"name":"tool","arguments":{"a":null,"b":true,"c":1}}</tool_call>'
    )
    assert result.code == "result = tool(a=None, b=True, c=1)\nprint(result)"


def test_provider_url_normalization_and_environment_name() -> None:
    # resolve_provider()が末尾のスラッシュを正規化して既知プロバイダ
    # (groq)を正しく認識できること、そして_env_var_from_url()が
    # ポート番号込みのURL(example.com:8443)からでも、大文字・アンダー
    # スコア区切りの妥当な環境変数名(EXAMPLE_COM_8443_API_KEY)を
    # 機械的に生成できることを確認する。
    assert resolve_provider("https://api.groq.com/openai/v1/").name == "groq"
    assert _env_var_from_url("https://example.com:8443/api") == "EXAMPLE_COM_8443_API_KEY"


def test_provider_key_collection_stops_at_first_missing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # EDGE_KEY(1個目)とEDGE_KEY_3(3個目)だけが設定されていて、
    # EDGE_KEY_2(2個目)が歯抜けになっている状況を確認する。
    # collect_api_keys()は連番を順にチェックしていくため、2番目が
    # 見つからなかった時点でそこで収集を打ち切り、EDGE_KEY_3は
    # 拾われない（["one"]だけが返る）ことを確認する。これは
    # 「歯抜けの番号は末尾切り捨て」という単純な仕様の裏付け。
    monkeypatch.setenv("EDGE_KEY", "one")
    monkeypatch.setenv("EDGE_KEY_3", "three")
    assert ProviderSpec("edge", "https://example.invalid", "EDGE_KEY").collect_api_keys() == ["one"]


def test_sandbox_empty_code_and_missing_name_are_explicit() -> None:
    # サンドボックスに空白だけのコードを渡すと[NoCodeBlock]、
    # 定義されていない変数(missing_name)を参照するコードを渡すと
    # 通常のPythonと同じくNameErrorになる(サンドボックスが変な
    # エラーメッセージにすり替えたりせず、素直にPythonの例外情報を
    # 伝えている)ことを確認する。
    sandbox = Sandbox(
        SandboxConfig(authorized_imports=[], allowed_directories=[]),
        apply_process_memory_limit=False,
    )
    assert "[NoCodeBlock]" in sandbox.run("   ")
    assert "NameError" in sandbox.run("print(missing_name)")


def test_sandbox_default_getattr_is_honored() -> None:
    # getattr(obj, 'missing', None)のように「見つからなかった場合の
    # デフォルト値」を指定するgetattrの3引数形式が、制限付きgetattrに
    # 差し替えられた後でも正しく機能する(Noneが返る)ことを確認する。
    # セキュリティ対策としてgetattrを差し替えていても、Pythonの
    # 標準的な使い方まで壊していないかという回帰確認。
    sandbox = Sandbox(
        SandboxConfig(authorized_imports=[], allowed_directories=[]),
        apply_process_memory_limit=False,
    )
    assert sandbox.run("print(getattr(object(), 'missing', None))").strip() == "None"


def test_models_reject_missing_required_fields() -> None:
    # MBPPTaskInput/SWEBenchTaskInputはPydanticモデルなので、必須
    # フィールド(...で指定されたフィールド)が1つも無い空の辞書を
    # 渡すと、Pydanticがバリデーションエラーを送出することを確認する。
    # moulinetteから壊れたタスク定義が来た場合に、後段の処理まで
    # 進んでから訳の分からないエラーになるのではなく、入力の時点で
    # はっきり失敗させるための仕組み。
    with pytest.raises(Exception):
        MBPPTaskInput.model_validate({})
    with pytest.raises(Exception):
        SWEBenchTaskInput.model_validate({})


def test_models_round_trip_unicode_and_empty_optional_fields() -> None:
    # task_id=0（0という値はfalsyだが、Noneや未設定ではなく正当な値
    # として扱われるべき）や、日本語のようなUnicode文字列、空の
    # test_listリストといった「境界値」を持つデータでも、モデルへの
    # 読み込みとJSONへの書き出し(model_dump_json)が正しく往復できる
    # ことを確認する。
    task = MBPPTaskInput.model_validate(
        {
            "task_id": 0,
            "task_definition": "文字列を処理する",
            "function_definition": "def solve(x):",
            "test_list": [],
        }
    )
    assert json.loads(task.model_dump_json())["task_id"] == 0
