"""LLMのツール呼び出し形式が何であれ、それをサンドボックス用のPythonコード文字列に正規化する。

Section 4.1: LLMごとに学習されているツール呼び出しの慣習が異なる。
このレイヤーは (b) XMLツール呼び出し、(c) JSON/Hermes形式のツール呼び出し、
(d) ReAct形式を、それぞれ同等のPython関数呼び出しに変換する。これにより、
サンドボックス自体は形式に依存せず、常にPythonコードだけを見ればよくなる。
形式(a)である ```python フェンスブロックが標準形式であり、変換は不要。
"""
from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass
from typing import Optional


_PYTHON_FENCE_RE = re.compile(r"```python\s*\n(.*?)(?:```|<end_code>)", re.DOTALL)

_GENERIC_FENCE_RE = re.compile(r"```(?:\w+)?\s*\n(.*?)```", re.DOTALL)

_UNCLOSED_FENCE_RE = re.compile(r"```python\s*\n(.*)$", re.DOTALL)


_XML_INVOKE_RE = re.compile(r'<invoke\s+name="([^"]+)">(.*?)</invoke>', re.DOTALL)

_XML_PARAM_RE = re.compile(r'<parameter(?:\s+name="([^"]+)")?>(.*?)</parameter>', re.DOTALL)


_JSON_TOOLCALL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)


_REACT_RE = re.compile(r"Action:\s*(\S+)\s*\nAction Input:\s*(\{.*?\}|\S.*)", re.DOTALL)


_MISSING_PARENS_CALL_RE = re.compile(
    r"^(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s+"
    r"(?P<arg>'''(?:.|\n)*?'''|\"\"\"(?:.|\n)*?\"\"\"|'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\")\s*$",
    re.DOTALL,
)


def _repair_missing_call_parens(code: str) -> Optional[str]:
    """`final_answer '''...'''`のように、関数呼び出しの丸括弧を丸ごと忘れた
    コードを`final_answer('''...''')`へ書き直す。

    `<identifier> <文字列リテラル>`という並びは、それ単体の文としてはPythonの
    文法上どうやっても有効になり得ない(丸括弧なしにこの形で構文的に妥当な
    Python文は存在しない)ため、これを検出したら常に修復して構わない -
    既に妥当なコードを誤って壊す余地がない。実例: `run_tests(...)`で既に
    正解だと確認済みの解答が、`final_answer(code)`ではなく`final_answer code`
    という書き方のせいでSyntaxErrorになり、そのままMBPPの小さいトークン予算を
    使い切って不合格になったケースがあった。
    """
    match = _MISSING_PARENS_CALL_RE.match(code.strip())
    if not match:
        return None
    repaired = f"{match.group('name')}({match.group('arg')})"
    try:
        ast.parse(repaired)
    except SyntaxError:
        return None
    return repaired


@dataclass
class ExtractionResult:
    """抽出結果を保持するデータクラス。

    `code` は実行すべきPythonスニペット(何も見つからなかった場合はNone)。

    `note` は抽出レイヤーが何を行ったかを説明する文字列で、オーケストレーターが
    次にLLMへ見せるObservationの先頭に付加できるようにする - Section 4.1で
    義務付けられている「コードブロックが見つからなかった」/「不正な形式だが
    解釈して実行した(その方法を説明する)」というフィードバックのこと。
    """

    code: Optional[str]
    note: str


def _py_literal(value: str) -> str:
    """生の文字列値を、可能ならJSON/数値としての解釈を優先してPythonリテラルとして表現する。"""
    stripped = value.strip()
    try:
        return repr(json.loads(stripped))
    except (json.JSONDecodeError, TypeError):
        return repr(value)


def _call_from_kwargs(name: str, kwargs: dict, parse_string_literals: bool = False) -> str:

    parts = []
    for key, value in kwargs.items():
        if parse_string_literals and isinstance(value, str):
            parts.append(f"{key}={_py_literal(value)}")
        else:
            parts.append(f"{key}={value!r}")

    return f"result = {name}({', '.join(parts)})\nprint(result)"


def _extract_xml_invoke(text: str) -> Optional[str]:

    match = _XML_INVOKE_RE.search(text)
    if not match:
        return None
    name, body = match.group(1), match.group(2)
    kwargs: dict = {}
    for index, param_match in enumerate(_XML_PARAM_RE.finditer(body)):
        key = param_match.group(1) or f"arg{index}"
        kwargs[key] = param_match.group(2).strip()

    return _call_from_kwargs(name, kwargs, parse_string_literals=True)


def _extract_json_tool_call(text: str) -> Optional[str]:

    match = _JSON_TOOLCALL_RE.search(text)
    if not match:
        return None
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    name = payload.get("name")
    arguments = payload.get("arguments", {})
    if not name or not isinstance(arguments, dict):
        return None
    return _call_from_kwargs(name, arguments)


def _extract_react(text: str) -> Optional[str]:

    match = _REACT_RE.search(text)
    if not match:
        return None
    name, raw_input = match.group(1).strip(), match.group(2).strip()
    try:
        arguments = json.loads(raw_input)
        if not isinstance(arguments, dict):
            arguments = {"value": arguments}
    except json.JSONDecodeError:
        arguments = {"value": raw_input}
    return _call_from_kwargs(name, arguments)


def extract_code(llm_output: str) -> ExtractionResult:
    """1回分のLLM応答からPythonスニペットをベストエフォートで抽出する。

    以下の順で試みる: 正しく閉じられた ```python フェンス、閉じタグがないが
    救済可能な ```python フェンス、Section 4.1にある3種類の代替ツール呼び出し形式、
    そして最後の手段として言語指定なしの一般的なフェンスブロック。
    """

    match = _PYTHON_FENCE_RE.search(llm_output)
    if match:
        code = match.group(1).strip()
        repaired = _repair_missing_call_parens(code)
        if repaired is not None:
            return ExtractionResult(
                code=repaired,
                note=(
                    "[FormatConverted] The call was missing its parentheses "
                    f"(`{code.splitlines()[0]}` has no `(...)`); added them before execution."
                ),
            )
        return ExtractionResult(code=code, note="")


    unclosed = _UNCLOSED_FENCE_RE.search(llm_output)
    if unclosed:
        return ExtractionResult(
            code=unclosed.group(1).strip(),
            note=(
                "[MalformedCodeBlock] The ```python fence was never closed with ``` or "
                "<end_code>; the rest of the response was used as the code anyway."
            ),
        )


    for extractor, format_name in (
        (_extract_xml_invoke, "XML <invoke> tool call"),
        (_extract_json_tool_call, "JSON/Hermes <tool_call>"),
        (_extract_react, "ReAct Action / Action Input"),
    ):
        code = extractor(llm_output)
        if code:
            return ExtractionResult(
                code=code,
                note=(
                    f"[FormatConverted] Response used a {format_name} format; "
                    "converted to an equivalent Python call before execution."
                ),
            )


    generic = _GENERIC_FENCE_RE.search(llm_output)
    if generic:
        return ExtractionResult(
            code=generic.group(1).strip(),
            note=(
                "[MalformedCodeBlock] No ```python fence found; "
                "used the first generic fenced block instead."
            ),
        )


    stripped = llm_output.strip()
    if stripped:
        try:
            ast.parse(stripped)
        except SyntaxError:
            pass
        else:
            return ExtractionResult(
                code=stripped,
                note=(
                    "[FormatConverted] No code fence at all was found, but the entire "
                    "response is valid Python on its own; ran it directly."
                ),
            )


    return ExtractionResult(
        code=None,
        note=(
            "[NoCodeBlock] No valid Python code block or recognized tool-call format "
            "(```python fence, <invoke>, <tool_call>, or Action/Action Input) was found "
            "in the model's response. Reply with a ```python ... ``` block ending in <end_code>."
        ),
    )
