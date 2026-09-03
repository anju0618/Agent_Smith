"""code_extraction.py のテスト: セクション4.1にある全ての出力フォーマットが
等価なPython呼び出しに正規化されること、認識できない出力は明示的に失敗と
扱われることを検証する。"""
from code_extraction import extract_code  # LLM出力からコードを抽出する対象の関数


def test_closed_python_fence() -> None:
    # 正しく閉じられたPythonのコードフェンス(```python ... ```)が
    # そのままコードとして抽出されることを検証
    text = "Thought: easy\nCode:\n```python\nprint(1 + 1)\n```\n<end_code>"
    result = extract_code(text)
    assert result.code == "print(1 + 1)"  # フェンス内のコードのみが取り出される
    assert result.note == ""  # 特に注記(変換・エラー等)は付かない


def test_unclosed_python_fence_is_salvaged() -> None:
    # 閉じフェンスがない不完全なコードブロックでも、内容が救済的に取り出されることを検証
    text = "Code:\n```python\nprint('hello')\n"
    result = extract_code(text)
    assert result.code is not None  # コードが取得できていること
    assert "print('hello')" in result.code  # 元のコード内容が含まれること
    assert "[MalformedCodeBlock]" in result.note  # 不正な形式だったという注記が付くこと


def test_xml_invoke_is_converted() -> None:
    # XML形式の<invoke>/<parameter>タグによるツール呼び出しが、
    # 通常のPython関数呼び出し構文に変換されることを検証
    text = (
        'Code:\n<invoke name="read_file">'
        '<parameter name="filepath">/testbed/a.py</parameter>'
        '<parameter name="start_line">1</parameter>'
        "</invoke>"
    )
    result = extract_code(text)
    assert result.code is not None  # コードへの変換に成功していること
    assert "read_file(" in result.code  # 関数呼び出しの形になっていること
    assert "filepath='/testbed/a.py'" in result.code  # 文字列引数が正しく渡されること
    assert "start_line=1" in result.code  # 数値引数が正しく渡されること
    assert "[FormatConverted]" in result.note  # フォーマット変換が行われたという注記が付くこと


def test_json_tool_call_is_converted() -> None:
    # <tool_call>タグ内のJSON形式のツール呼び出しがPythonコードに変換されることを検証
    text = '<tool_call>{"name": "search_code", "arguments": {"pattern": "foo"}}</tool_call>'
    result = extract_code(text)
    # 結果を変数に代入してprintするコードに変換されることを確認
    assert result.code == "result = search_code(pattern='foo')\nprint(result)"
    assert "[FormatConverted]" in result.note  # フォーマット変換が行われたという注記が付くこと


def test_json_tool_call_preserves_string_argument_types() -> None:
    # JSON引数の値が数値・真偽値に見える文字列("123"や"false")であっても、
    # 元の型(文字列)がそのまま保持されて変換されることを検証
    text = (
        '<tool_call>{"name": "search_code", '
        '"arguments": {"pattern": "123", "file_pattern": "false"}}</tool_call>'
    )
    result = extract_code(text)
    # "123"や"false"は文字列のままPythonの文字列リテラルとして渡される
    assert result.code == "result = search_code(pattern='123', file_pattern='false')\nprint(result)"


def test_react_format_is_converted() -> None:
    # ReAct形式(Action: / Action Input:)の呼び出しがPythonコードに変換されることを検証
    text = 'Action: list_files\nAction Input: {"directory": "/testbed"}'
    result = extract_code(text)
    assert result.code == "result = list_files(directory='/testbed')\nprint(result)"
    assert "[FormatConverted]" in result.note  # フォーマット変換が行われたという注記が付くこと


def test_react_format_preserves_json_string_argument_types() -> None:
    # ReAct形式でも、JSON文字列値("null"など)がPythonのNone等に誤変換されず
    # 文字列のまま保持されることを検証
    text = 'Action: search_code\nAction Input: {"pattern": "null"}'
    result = extract_code(text)
    assert result.code == "result = search_code(pattern='null')\nprint(result)"


def test_no_code_block_found() -> None:
    # コードブロックが全く含まれないテキストの場合、コードはNoneとなり
    # 「コードブロックなし」の注記が付くことを検証
    text = "I think the answer is 42, but let me think more."
    result = extract_code(text)
    assert result.code is None  # コードは抽出されない
    assert "[NoCodeBlock]" in result.note  # コードブロックが見つからなかったという注記が付くこと
