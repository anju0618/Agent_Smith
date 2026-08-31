"""Normalizes any LLM tool-call format into a Python code string for the sandbox.

Section 4.1: different LLMs are trained on different tool-calling conventions.
This layer converts formats (b) XML tool calls, (c) JSON/Hermes tool calls, and
(d) ReAct into equivalent Python function calls, so the sandbox itself stays
format-agnostic and only ever sees Python code. Format (a), a fenced
```python block, is the primary format and needs no conversion.
"""
from __future__ import annotations

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


@dataclass
class ExtractionResult:
    """`code` is the Python snippet to execute (None if nothing usable was found).

    `note` explains what the extraction layer did, so the orchestrator can
    prepend it to the Observation the LLM sees next - the mandatory "no code
    block found" / "malformed but interpreted anyway (explain how)" feedback
    from Section 4.1.
    """

    code: Optional[str]
    note: str


def _py_literal(value: str) -> str:
    """Render a raw string value as a Python literal, preferring its JSON/number reading."""
    stripped = value.strip()
    try:
        return repr(json.loads(stripped))
    except (json.JSONDecodeError, TypeError):
        return repr(value)


def _call_from_kwargs(name: str, kwargs: dict) -> str:
    parts = []
    for key, value in kwargs.items():
        if isinstance(value, str):
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
    return _call_from_kwargs(name, kwargs)


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
    """Best-effort extraction of a Python snippet from one LLM response.

    Tries, in order: a properly closed ```python fence, an unclosed-but-salvageable
    ```python fence, the three alternate tool-call formats from Section 4.1, then a
    bare generic fenced block as a last resort.
    """
    match = _PYTHON_FENCE_RE.search(llm_output)
    if match:
        return ExtractionResult(code=match.group(1).strip(), note="")

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

    return ExtractionResult(
        code=None,
        note=(
            "[NoCodeBlock] No valid Python code block or recognized tool-call format "
            "(```python fence, <invoke>, <tool_call>, or Action/Action Input) was found "
            "in the model's response. Reply with a ```python ... ``` block ending in <end_code>."
        ),
    )
