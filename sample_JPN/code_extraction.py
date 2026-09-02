"""Normalizes any LLM tool-call format into a Python code string for the sandbox.

Section 4.1: different LLMs are trained on different tool-calling conventions.
This layer converts formats (b) XML tool calls, (c) JSON/Hermes tool calls, and
(d) ReAct into equivalent Python function calls, so the sandbox itself stays
format-agnostic and only ever sees Python code. Format (a), a fenced
```python block, is the primary format and needs no conversion.
"""
# ============================================================================
# 【日本語解説】このファイルの役割
# ============================================================================
# LLMごとにツール呼び出しの書き方の癖が異なる（```python フェンス派、
# XML <invoke> 派、JSON <tool_call> 派、ReAct Action/Action Input 派、など）。
# このモジュールは、あらゆる形式を「サンドボックスが実行できる等価な Python
# コード文字列」に変換してから渡す。これによりサンドボックス自体は完全に
# フォーマット非依存でいられる。
#
# extract_code() が上から順に7段階のフォールバックを試し、最初にヒットした
# 形式を使う（詳細は各関数のコメントを参照）。
# ============================================================================
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Optional

# ----------------------------------------------------------------------------
# 【日本語解説】正規表現パターン一覧（優先順位そのままの定義順）
# ----------------------------------------------------------------------------
# _PYTHON_FENCE_RE が (?:```|<end_code>) の「どちらでも」閉じられるのが
# ポイント: モデルが <end_code> を書き忘れて ``` だけで閉じても、逆に ```
# を忘れて <end_code> だけ書いても、どちらも正常系として拾える。
# ----------------------------------------------------------------------------
_PYTHON_FENCE_RE = re.compile(r"```python\s*\n(.*?)(?:```|<end_code>)", re.DOTALL)
# 正しく閉じられた ```python ... ``` (または <end_code> 終端) ブロックにマッチ。
# これがプライマリ形式で、変換不要でそのまま抽出できる。
_GENERIC_FENCE_RE = re.compile(r"```(?:\w+)?\s*\n(.*?)```", re.DOTALL)
# 言語指定の無い、または python 以外の言語タグが付いたフェンスブロックにマッチ。
# すべての形式を試して失敗したときの、最後の保険として使われる。
_UNCLOSED_FENCE_RE = re.compile(r"```python\s*\n(.*)$", re.DOTALL)
# ```python は開いているが閉じフェンス（``` や <end_code>）が無いまま応答が
# 終わってしまった場合の救済用。残りテキスト全部をコードとみなす。

_XML_INVOKE_RE = re.compile(r'<invoke\s+name="([^"]+)">(.*?)</invoke>', re.DOTALL)
# <invoke name="ツール名">...</invoke> 形式（Claude系でよく見る形式）にマッチ。
_XML_PARAM_RE = re.compile(r'<parameter(?:\s+name="([^"]+)")?>(.*?)</parameter>', re.DOTALL)
# <invoke> の中身から <parameter name="キー">値</parameter> を1つずつ拾う。
# name属性が省略されている場合は後段で arg0, arg1, ... という仮名を振る。

_JSON_TOOLCALL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)
# <tool_call>{"name": ..., "arguments": {...}}</tool_call> 形式（Hermes系モデル
# でよく見る形式）にマッチ。

_REACT_RE = re.compile(r"Action:\s*(\S+)\s*\nAction Input:\s*(\{.*?\}|\S.*)", re.DOTALL)
# 古典的な ReAct プロンプティング形式 "Action: ツール名\nAction Input: {...}"
# にマッチ。


@dataclass
class ExtractionResult:
    """`code` is the Python snippet to execute (None if nothing usable was found).

    `note` explains what the extraction layer did, so the orchestrator can
    prepend it to the Observation the LLM sees next - the mandatory "no code
    block found" / "malformed but interpreted anyway (explain how)" feedback
    from Section 4.1.
    """
    # -------------------------------------------------------------------
    # 【日本語解説】ExtractionResult = extract_code() の戻り値
    # -------------------------------------------------------------------
    # code: 実行すべきPythonコード文字列。何も見つからなければ None。
    # note: 抽出処理が「何をしたか」の説明文。正常系（プライマリ形式が
    #       そのまま見つかった場合）は空文字列。それ以外（救済策やフォー
    #       マット変換、抽出失敗）の場合は、次のObservationの先頭に付加
    #       される「何が起きたか」の明示的な説明になる。
    #       ── これは「暗黙に何かをしたら、必ずそれをLLMに伝える」という
    #       このプロジェクト全体の一貫した設計方針の一部。
    # -------------------------------------------------------------------

    code: Optional[str]
    note: str


def _py_literal(value: str) -> str:
    """Render a raw string value as a Python literal, preferring its JSON/number reading."""
    # ---------------------------------------------------------------
    # 【日本語解説】XMLの<parameter>の中身をPythonリテラルに変換する
    # ---------------------------------------------------------------
    # XML/ReAct形式では値は常に「文字列」として渡ってくる（例: "true", "3",
    # "hello"）。しかしそれを json.loads() で読めるなら、Pythonの True/3
    # のような本来の型として埋め込みたい（"true" のまま渡すと Python コード
    # 上では文字列"true"になってしまい、ツール側が bool を期待していると
    # 壊れる）。json.loads に失敗する（普通の自然言語文字列など）場合は、
    # ただの文字列として repr() する。
    # ---------------------------------------------------------------
    stripped = value.strip()
    try:
        return repr(json.loads(stripped))
    except (json.JSONDecodeError, TypeError):
        return repr(value)


def _call_from_kwargs(name: str, kwargs: dict, parse_string_literals: bool = False) -> str:
    # ---------------------------------------------------------------
    # 【日本語解説】ツール名+引数辞書 → 等価なPythonコード文字列への変換
    # ---------------------------------------------------------------
    # XML/JSON/ReAct のどの形式から来た呼び出しも、最終的にはこの関数を通って
    #     result = ツール名(key1=value1, key2=value2, ...)
    #     print(result)
    # という、サンドボックスがそのまま exec() できる2行のPythonコードに
    # 変換される。print(result) を必ず付けるのは、ツールの戻り値をLLMに
    # 見せる（Observationとして反映させる）ため——print しなければ、結果は
    # サンドボックスの標準出力に現れず、LLMには何も見えないまま次のターンに
    # 進んでしまう（BENCHMARK_REPORT.md のアブレーション実験で、worked
    # exampleが無いとこの print 忘れパターンが実際に起きたと記録されている）。
    #
    # parse_string_literals=True の場合だけ _py_literal() で型を推測する
    # （XML由来の呼び出しのみ。JSON/ReActは既にJSONとしてパース済みで、
    # 値の型が最初から分かっているので不要）。
    # ---------------------------------------------------------------
    parts = []
    for key, value in kwargs.items():
        if parse_string_literals and isinstance(value, str):
            parts.append(f"{key}={_py_literal(value)}")
        else:
            parts.append(f"{key}={value!r}")
    return f"result = {name}({', '.join(parts)})\nprint(result)"


def _extract_xml_invoke(text: str) -> Optional[str]:
    # ---------------------------------------------------------------
    # 【日本語解説】XML <invoke> 形式の変換
    # ---------------------------------------------------------------
    # 例:
    #   <invoke name="search_code">
    #     <parameter name="pattern">is_valid_email</parameter>
    #   </invoke>
    # は
    #   result = search_code(pattern='is_valid_email')
    #   print(result)
    # に変換される。<parameter> に name 属性が無い場合は "arg0", "arg1", ...
    # という仮の引数名を振る（enumerate の index を使用）。
    # ---------------------------------------------------------------
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
    # ---------------------------------------------------------------
    # 【日本語解説】JSON/Hermes <tool_call> 形式の変換
    # ---------------------------------------------------------------
    # 例: <tool_call>{"name": "search_code", "arguments": {"pattern": "foo"}}</tool_call>
    # payload.get("name") が無い、または arguments が dict でない、または
    # JSONとして壊れている場合は None を返し、呼び出し元 extract_code() が
    # 次の形式（ReAct）へフォールバックする。
    # ---------------------------------------------------------------
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
    # ---------------------------------------------------------------
    # 【日本語解説】ReAct "Action: name\nAction Input: {...}" 形式の変換
    # ---------------------------------------------------------------
    # Action Input が正当なJSON辞書ならそのまま kwargs として使う。
    # JSON辞書でない場合（例えば Action Input: "some raw string"）は
    # {"value": raw_input} という1引数の呼び出しとして扱うフォールバックも
    # 用意されている——形式が曖昧でも、可能な限り何かのPython呼び出しに
    # 変換しようとする「best-effort」の姿勢がここにも表れている。
    # ---------------------------------------------------------------
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
    # =====================================================================
    # 【日本語解説】extract_code() 全体のフロー — 7段階のフォールバック
    # =====================================================================
    # orchestrator.py の Orchestrator.run() が、LLMの生応答 (gen.text) を
    # 毎ターンこの関数に渡す。上から順に試し、最初にヒットした形式の結果を
    # 即座に返す（後段は一切試さない）。
    # =====================================================================

    # --- 1. 正しく閉じた ```python ... ``` フェンス（プライマリ形式） -----
    match = _PYTHON_FENCE_RE.search(llm_output)
    if match:
        # note="" — 何も特別なことは起きていない、変換なしの正常系。
        return ExtractionResult(code=match.group(1).strip(), note="")

    # --- 2. 閉じられていない ```python フェンス（救済策） -----------------
    unclosed = _UNCLOSED_FENCE_RE.search(llm_output)
    if unclosed:
        # 閉じフェンスを忘れていても、残りのテキスト全部をコードとして
        # 救済する。ただし「本当は正しくない形式だった」ことを note で
        # LLMに伝え、次のターンで直させる。
        return ExtractionResult(
            code=unclosed.group(1).strip(),
            note=(
                "[MalformedCodeBlock] The ```python fence was never closed with ``` or "
                "<end_code>; the rest of the response was used as the code anyway."
            ),
        )

    # --- 3〜5. 代替のツール呼び出し形式（XML / JSON / ReAct）を順に試す ----
    for extractor, format_name in (
        (_extract_xml_invoke, "XML <invoke> tool call"),
        (_extract_json_tool_call, "JSON/Hermes <tool_call>"),
        (_extract_react, "ReAct Action / Action Input"),
    ):
        code = extractor(llm_output)
        if code:
            # どの形式が使われたかを note に明示することで、LLMに「あなたの
            # 応答は変換された」ことを気づかせる（本来期待している
            # ```python フェンス形式への誘導にもなる）。
            return ExtractionResult(
                code=code,
                note=(
                    f"[FormatConverted] Response used a {format_name} format; "
                    "converted to an equivalent Python call before execution."
                ),
            )

    # --- 6. 最後の保険: 言語タグの無い/違う汎用フェンスブロック -----------
    generic = _GENERIC_FENCE_RE.search(llm_output)
    if generic:
        return ExtractionResult(
            code=generic.group(1).strip(),
            note=(
                "[MalformedCodeBlock] No ```python fence found; "
                "used the first generic fenced block instead."
            ),
        )

    # --- 7. それでも何も見つからなければ、諦めて明示的なフィードバックを返す ---
    # code=None が返ると、orchestrator.py はサンドボックス実行そのものを
    # スキップし、この note をそのまま次のObservationとしてLLMに渡す。
    # ループが「たぶんこう直せばいいだろう」と勝手に推測して何かを実行する
    # ことは絶対にない——常に「何が起きたか／何を直すべきか」を明示的に
    # LLMへ返す、という一貫した設計方針がここにも表れている。
    return ExtractionResult(
        code=None,
        note=(
            "[NoCodeBlock] No valid Python code block or recognized tool-call format "
            "(```python fence, <invoke>, <tool_call>, or Action/Action Input) was found "
            "in the model's response. Reply with a ```python ... ``` block ending in <end_code>."
        ),
    )
