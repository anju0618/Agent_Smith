"""LLMのツール呼び出し形式が何であれ、それをサンドボックス用のPythonコード文字列に正規化する。

Section 4.1: LLMごとに学習されているツール呼び出しの慣習が異なる。
このレイヤーは (b) XMLツール呼び出し、(c) JSON/Hermes形式のツール呼び出し、
(d) ReAct形式を、それぞれ同等のPython関数呼び出しに変換する。これにより、
サンドボックス自体は形式に依存せず、常にPythonコードだけを見ればよくなる。
形式(a)である ```python フェンスブロックが標準形式であり、変換は不要。
"""
from __future__ import annotations  # 型注釈の評価を遅延させるためのfuture import

import json  # JSON形式のツール呼び出しをパースするためのjsonモジュール
import re  # 各種テキストパターンを検出するための正規表現モジュール
from dataclasses import dataclass  # 結果を保持するデータクラスを定義するため
from typing import Optional  # 型ヒント用のOptional

# 正しく閉じられた ```python ... ``` または <end_code> で終わるコードフェンスにマッチする正規表現
_PYTHON_FENCE_RE = re.compile(r"```python\s*\n(.*?)(?:```|<end_code>)", re.DOTALL)
# 言語指定の有無を問わない、任意の閉じたコードフェンスにマッチする正規表現(最後の手段用)
_GENERIC_FENCE_RE = re.compile(r"```(?:\w+)?\s*\n(.*?)```", re.DOTALL)
# 閉じタグがないまま文末まで続く ```python フェンスにマッチする正規表現(救済用)
_UNCLOSED_FENCE_RE = re.compile(r"```python\s*\n(.*)$", re.DOTALL)

# XML形式の <invoke name="...">...</invoke> ツール呼び出しにマッチする正規表現
_XML_INVOKE_RE = re.compile(r'<invoke\s+name="([^"]+)">(.*?)</invoke>', re.DOTALL)
# XML形式の <parameter name="...">...</parameter> パラメータにマッチする正規表現
_XML_PARAM_RE = re.compile(r'<parameter(?:\s+name="([^"]+)")?>(.*?)</parameter>', re.DOTALL)

# JSON/Hermes形式の <tool_call>{...}</tool_call> にマッチする正規表現
_JSON_TOOLCALL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)

# ReAct形式の "Action: ...\nAction Input: ..." にマッチする正規表現
_REACT_RE = re.compile(r"Action:\s*(\S+)\s*\nAction Input:\s*(\{.*?\}|\S.*)", re.DOTALL)


@dataclass
class ExtractionResult:
    """抽出結果を保持するデータクラス。

    `code` は実行すべきPythonスニペット(何も見つからなかった場合はNone)。

    `note` は抽出レイヤーが何を行ったかを説明する文字列で、オーケストレーターが
    次にLLMへ見せるObservationの先頭に付加できるようにする - Section 4.1で
    義務付けられている「コードブロックが見つからなかった」/「不正な形式だが
    解釈して実行した(その方法を説明する)」というフィードバックのこと。
    """

    code: Optional[str]  # 実行すべきPythonコード文字列。抽出失敗時はNone
    note: str  # 抽出処理の説明・注記(空文字列の場合もある)


def _py_literal(value: str) -> str:
    """生の文字列値を、可能ならJSON/数値としての解釈を優先してPythonリテラルとして表現する。"""
    stripped = value.strip()  # 前後の空白を除去
    try:
        return repr(json.loads(stripped))  # JSONとしてパースできればその値のPython表現(repr)を返す
    except (json.JSONDecodeError, TypeError):  # JSONとして解釈できない場合
        return repr(value)  # 元の文字列そのもののPython表現(repr)を返す


def _call_from_kwargs(name: str, kwargs: dict, parse_string_literals: bool = False) -> str:
    # 関数呼び出しの引数部分の文字列表現を溜めるリスト
    parts = []
    for key, value in kwargs.items():  # 各キーワード引数について
        if parse_string_literals and isinstance(value, str):  # 文字列値をJSON解釈で変換するモードかつ実際に文字列の場合
            parts.append(f"{key}={_py_literal(value)}")  # JSON/数値解釈を試みたPythonリテラルとして追加
        else:  # それ以外(すでにPythonの値、または変換不要)の場合
            parts.append(f"{key}={value!r}")  # 値をそのままreprで文字列化して追加
    # "result = 関数名(引数...)" とその結果をprintするコードを組み立てて返す
    return f"result = {name}({', '.join(parts)})\nprint(result)"


def _extract_xml_invoke(text: str) -> Optional[str]:
    # テキスト中からXML形式のinvokeタグを検索する
    match = _XML_INVOKE_RE.search(text)
    if not match:  # 見つからなければ
        return None  # 抽出失敗としてNoneを返す
    name, body = match.group(1), match.group(2)  # 関数名とinvokeタグの中身(パラメータ部分)を取り出す
    kwargs: dict = {}  # パラメータ名と値を格納する辞書
    for index, param_match in enumerate(_XML_PARAM_RE.finditer(body)):  # invoke内の各parameterタグについて
        key = param_match.group(1) or f"arg{index}"  # name属性がなければ位置に基づく仮の名前(arg0, arg1, ...)を使う
        kwargs[key] = param_match.group(2).strip()  # パラメータの値(前後の空白を除去)を辞書に格納
    # 抽出した関数名と引数からPython呼び出しコードを生成して返す(文字列値はJSON解釈を試みる)
    return _call_from_kwargs(name, kwargs, parse_string_literals=True)


def _extract_json_tool_call(text: str) -> Optional[str]:
    # テキスト中からJSON/Hermes形式のtool_callタグを検索する
    match = _JSON_TOOLCALL_RE.search(text)
    if not match:  # 見つからなければ
        return None  # 抽出失敗としてNoneを返す
    try:
        payload = json.loads(match.group(1))  # tool_callタグの中身をJSONとしてパース
    except json.JSONDecodeError:  # JSONとして不正な場合
        return None  # 抽出失敗としてNoneを返す
    name = payload.get("name")  # 呼び出す関数名を取得
    arguments = payload.get("arguments", {})  # 引数辞書を取得(なければ空辞書)
    if not name or not isinstance(arguments, dict):  # 関数名がない、または引数がdict型でない場合
        return None  # 不正な形式として抽出失敗を返す
    return _call_from_kwargs(name, arguments)  # Python呼び出しコードを生成して返す


def _extract_react(text: str) -> Optional[str]:
    # テキスト中からReAct形式の Action/Action Input を検索する
    match = _REACT_RE.search(text)
    if not match:  # 見つからなければ
        return None  # 抽出失敗としてNoneを返す
    name, raw_input = match.group(1).strip(), match.group(2).strip()  # 関数名(Action)と入力(Action Input)を取り出す
    try:
        arguments = json.loads(raw_input)  # Action Inputの内容をJSONとしてパースを試みる
        if not isinstance(arguments, dict):  # JSONとしては解釈できたがdict型でない場合(数値や文字列など)
            arguments = {"value": arguments}  # "value"という単一キーの辞書に包む
    except json.JSONDecodeError:  # JSONとして解釈できない場合
        arguments = {"value": raw_input}  # 生の文字列をそのまま"value"キーに入れる
    return _call_from_kwargs(name, arguments)  # Python呼び出しコードを生成して返す


def extract_code(llm_output: str) -> ExtractionResult:
    """1回分のLLM応答からPythonスニペットをベストエフォートで抽出する。

    以下の順で試みる: 正しく閉じられた ```python フェンス、閉じタグがないが
    救済可能な ```python フェンス、Section 4.1にある3種類の代替ツール呼び出し形式、
    そして最後の手段として言語指定なしの一般的なフェンスブロック。
    """
    # まず最優先の形式: 正しく閉じられた ```python フェンスを探す
    match = _PYTHON_FENCE_RE.search(llm_output)
    if match:  # 見つかった場合
        return ExtractionResult(code=match.group(1).strip(), note="")  # 前後の空白を除いたコードを返す(注記なし)

    # 次に、閉じタグのない```pythonフェンス(応答が途中で切れた場合など)を探す
    unclosed = _UNCLOSED_FENCE_RE.search(llm_output)
    if unclosed:  # 見つかった場合
        return ExtractionResult(
            code=unclosed.group(1).strip(),  # フェンス以降の残り全部をコードとして採用する
            note=(
                "[MalformedCodeBlock] The ```python fence was never closed with ``` or "
                "<end_code>; the rest of the response was used as the code anyway."
            ),  # 閉じられていなかったことをLLMへのフィードバックとして注記する
        )

    # 続いて、XML/JSON/ReActの3種類の代替ツール呼び出し形式を順に試す
    for extractor, format_name in (
        (_extract_xml_invoke, "XML <invoke> tool call"),  # XML形式の抽出関数と表示名
        (_extract_json_tool_call, "JSON/Hermes <tool_call>"),  # JSON形式の抽出関数と表示名
        (_extract_react, "ReAct Action / Action Input"),  # ReAct形式の抽出関数と表示名
    ):
        code = extractor(llm_output)  # 各形式の抽出を試みる
        if code:  # 抽出に成功したら
            return ExtractionResult(
                code=code,  # 変換済みのPythonコードを返す
                note=(
                    f"[FormatConverted] Response used a {format_name} format; "
                    "converted to an equivalent Python call before execution."
                ),  # どの形式から変換したかをLLMへのフィードバックとして注記する
            )

    # どの専用形式にも一致しなければ、最後の手段として言語指定なしの一般的なフェンスを探す
    generic = _GENERIC_FENCE_RE.search(llm_output)
    if generic:  # 見つかった場合
        return ExtractionResult(
            code=generic.group(1).strip(),  # フェンス内の中身をコードとして採用する
            note=(
                "[MalformedCodeBlock] No ```python fence found; "
                "used the first generic fenced block instead."
            ),  # ```pythonフェンスがなかったことを注記する
        )

    # ここまでの全ての方式で何も見つからなかった場合は、抽出失敗として扱う
    return ExtractionResult(
        code=None,  # 実行可能なコードはなし
        note=(
            "[NoCodeBlock] No valid Python code block or recognized tool-call format "
            "(```python fence, <invoke>, <tool_call>, or Action/Action Input) was found "
            "in the model's response. Reply with a ```python ... ``` block ending in <end_code>."
        ),  # 有効なコードブロックが全く見つからなかったことをLLMへのフィードバックとして注記する
    )
