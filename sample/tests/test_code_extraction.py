"""Tests for code_extraction.py: every format from Section 4.1 must normalize
into an equivalent Python call, and unrecognized output must fail explicitly."""
from code_extraction import extract_code


def test_closed_python_fence() -> None:
    text = "Thought: easy\nCode:\n```python\nprint(1 + 1)\n```\n<end_code>"
    result = extract_code(text)
    assert result.code == "print(1 + 1)"
    assert result.note == ""


def test_unclosed_python_fence_is_salvaged() -> None:
    text = "Code:\n```python\nprint('hello')\n"
    result = extract_code(text)
    assert result.code is not None
    assert "print('hello')" in result.code
    assert "[MalformedCodeBlock]" in result.note


def test_xml_invoke_is_converted() -> None:
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
    text = '<tool_call>{"name": "search_code", "arguments": {"pattern": "foo"}}</tool_call>'
    result = extract_code(text)
    assert result.code == "result = search_code(pattern='foo')\nprint(result)"
    assert "[FormatConverted]" in result.note


def test_react_format_is_converted() -> None:
    text = 'Action: list_files\nAction Input: {"directory": "/testbed"}'
    result = extract_code(text)
    assert result.code == "result = list_files(directory='/testbed')\nprint(result)"
    assert "[FormatConverted]" in result.note


def test_no_code_block_found() -> None:
    text = "I think the answer is 42, but let me think more."
    result = extract_code(text)
    assert result.code is None
    assert "[NoCodeBlock]" in result.note
