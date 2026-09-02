"""Tests for code_extraction.py: every format from Section 4.1 must normalize
into an equivalent Python call, and unrecognized output must fail explicitly."""
# ============================================================================
# 日本語解説: このファイルは code_extraction.py（LLM の出力から実行可能な Python
# コードを取り出す変換レイヤー）をテストするファイルです。
#
# LLM ごとにツール呼び出しの書き方の癖が異なるため、code_extraction.py は
#   (a) ```python ... ``` の正規フェンス
#   (b) XML の <invoke name="...">...</invoke> 形式
#   (c) JSON/Hermes 形式の <tool_call>{"name": ..., "arguments": {...}}</tool_call>
#   (d) ReAct 形式の "Action: name\nAction Input: {...}"
# のいずれで来ても、すべて同じ形の Python 呼び出しコード文字列に変換します。
# ここでは各形式が正しく変換されること、そして「どの形式にも当てはまらない
# 出力」は黙って何かを推測するのではなく明示的に失敗すること（[NoCodeBlock]）
# を確認しています。
# ============================================================================
from code_extraction import extract_code


def test_closed_python_fence() -> None:
    # 最もオーソドックスなケース: ```python ... ``` で正しく閉じられ、
    # さらに <end_code> も付いている「お手本通り」の出力。
    # 変換は不要なので、note（何か特別なことをしたという説明文）は空文字列になる。
    text = "Thought: easy\nCode:\n```python\nprint(1 + 1)\n```\n<end_code>"
    result = extract_code(text)
    assert result.code == "print(1 + 1)"
    assert result.note == ""


def test_unclosed_python_fence_is_salvaged() -> None:
    # LLM が ``` や <end_code> で閉じ忘れた場合の救済策を確認するテスト。
    # 閉じフェンスが無くても、そこから先の全部をコードとみなして拾い上げる。
    # ただしこれは「本来のフォーマット違反」なので、[MalformedCodeBlock] という
    # 注記がユーザー（次のObservationを見るLLM自身）に返される。
    text = "Code:\n```python\nprint('hello')\n"
    result = extract_code(text)
    assert result.code is not None
    assert "print('hello')" in result.code
    assert "[MalformedCodeBlock]" in result.note


def test_xml_invoke_is_converted() -> None:
    # XML形式（<invoke name="ツール名"><parameter name="引数名">値</parameter>...</invoke>）
    # を、キーワード引数付きのPython関数呼び出しに変換できることを確認する。
    # 変換後は result = 関数名(引数=値, ...) \n print(result) という形になる
    # （printを付けるのは、実行結果が次のObservationとしてLLMに見えるようにするため）。
    text = (
        'Code:\n<invoke name="read_file">'
        '<parameter name="filepath">/testbed/a.py</parameter>'
        '<parameter name="start_line">1</parameter>'
        "</invoke>"
    )
    result = extract_code(text)
    assert result.code is not None
    assert "read_file(" in result.code
    assert "filepath='/testbed/a.py'" in result.code
    assert "start_line=1" in result.code
    assert "[FormatConverted]" in result.note


def test_json_tool_call_is_converted() -> None:
    # JSON/Hermes形式（<tool_call>{"name": ..., "arguments": {...}}</tool_call>）の変換確認。
    # モデル(Hermes系など)によってはこの形式でツールを呼びたがるため、
    # これも同じPython呼び出しコードに正規化される。
    text = '<tool_call>{"name": "search_code", "arguments": {"pattern": "foo"}}</tool_call>'
    result = extract_code(text)
    assert result.code == "result = search_code(pattern='foo')\nprint(result)"
    assert "[FormatConverted]" in result.note


def test_json_tool_call_preserves_string_argument_types() -> None:
    # JSON側の値が "123" や "false" のように「数値/真偽値に見える文字列」でも、
    # JSON上ではあくまで文字列型として書かれているなら、Python変換後も
    # 文字列のまま（数値やbool型に化けない）ことを確認する型保持のテスト。
    text = (
        '<tool_call>{"name": "search_code", '
        '"arguments": {"pattern": "123", "file_pattern": "false"}}</tool_call>'
    )
    result = extract_code(text)
    assert result.code == "result = search_code(pattern='123', file_pattern='false')\nprint(result)"


def test_react_format_is_converted() -> None:
    # ReAct形式（"Action: ツール名\nAction Input: {JSON}"）からの変換確認。
    # 古典的なReAct系プロンプトで訓練されたモデルが出しがちな形式。
    text = 'Action: list_files\nAction Input: {"directory": "/testbed"}'
    result = extract_code(text)
    assert result.code == "result = list_files(directory='/testbed')\nprint(result)"
    assert "[FormatConverted]" in result.note


def test_react_format_preserves_json_string_argument_types() -> None:
    # ReAct形式でも、JSON側の値が "null" という文字列そのものなら、
    # Python の None には化けず、文字列 'null' のまま変換されることを確認する。
    text = 'Action: search_code\nAction Input: {"pattern": "null"}'
    result = extract_code(text)
    assert result.code == "result = search_code(pattern='null')\nprint(result)"


def test_no_code_block_found() -> None:
    # どの形式にも当てはまらない、ただの自然文だけの応答が来たケース。
    # ここで大事なのは「何かを推測して実行しようとしない」こと。
    # code は None になり、note に [NoCodeBlock] という明示的な説明が入る。
    # このnoteはOrchestratorがそのまま次のObservationとしてLLMに見せるので、
    # LLMは「コードブロックが無かった」と正しく認識して書き直せる。
    text = "I think the answer is 42, but let me think more."
    result = extract_code(text)
    assert result.code is None
    assert "[NoCodeBlock]" in result.note
