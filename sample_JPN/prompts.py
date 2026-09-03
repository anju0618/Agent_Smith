"""両方のエージェントで共有されるシステムプロンプトの構築(Section 4.1, point 6):
明確なツールのドキュメント、構造化されたThought/Code/Observationの各枠、
そして効果的な推論ループの例を含む。
"""
from __future__ import annotations  # 型注釈の評価を遅延させるためのfuture import

# エージェントに提示する基本的な振る舞いの説明文(文字列リテラルの中身は英語のまま、モデルへの指示なので変更しない)
FRAMEWORK_EXPLANATION = """\
You are an autonomous coding agent. You solve tasks by repeating a strict
Thought -> Code -> Observation loop:

  Thought: briefly reason about what to try next.
  Code: a single ```python ... ``` block ending with the literal token <end_code>.
  (the sandbox executes your code and returns its result as an Observation)
  Observation: you will be shown the sandbox's output/error for your code.

Rules:
- Put ALL of your reasoning in the Thought section, in plain text.
- Put ALL executable code inside exactly one ```python ... ``` block per turn,
  and end that block with <end_code> on its own line. Do not put code anywhere else.
- Variables you define persist between turns - you do not need to redefine them.
- Only the modules explicitly listed in the sandbox manual below may be imported.
- Always call tools with keyword arguments matching their listed parameter names
  exactly (e.g. run_tests(code=..., test_list=...)), never positional arguments.
- You never get to see the result of your code until the next Observation -
  never guess or invent an Observation yourself.
- When you are confident you solved the task, call final_answer(...) with your
  solution as described below. Calling it ends the loop immediately.
- If the sandbox reports [NoCodeBlock], [SyntaxError], [SandboxViolation],
  [Timeout], [MemoryLimitExceeded], or [TruncatedOutput], read the message
  carefully and adjust your next Code block accordingly - never repeat the
  exact same code after an error.
"""

# MBPPベンチマーク向けの「最終解答の出し方」を説明する文字列
_MBPP_FINAL_ANSWER = """\
Call final_answer(code) exactly once, where `code` is a string containing the
complete Python function that solves the task (matching the given function
signature). Example: final_answer("def add(a, b):\\n    return a + b")

As soon as run_tests(...) reports {"success": true}, call final_answer with
that exact code immediately in your NEXT turn - do not re-verify a solution
that already passed, and do not keep exploring alternatives. Every extra turn
spends part of your limited token and iteration budget.
"""

# SWE-benchベンチマーク向けの「最終解答の出し方」を説明する文字列
_SWEBENCH_FINAL_ANSWER = """\
Call final_answer(get_patch()) exactly once, once you have verified your fix
with run_tests(). get_patch() returns the unified git diff of every change you
made to the repository - do not hand-write the patch yourself.
"""

# MBPP向けの、Thought/Code/Observationループの具体例を示す文字列
_MBPP_EXAMPLE = """\
Example turn:

Thought: I'll write the function and check it against the public tests before submitting.
Code:
```python
code = "def add(a, b):\\n    return a + b"
print(run_tests(code=code, test_list=["assert add(2, 3) == 5", "assert add(-1, 1) == 0"]))
```
<end_code>

Observation: {"success": true, "output": ""}

Thought: All public tests passed. I'm confident in this solution.
Code:
```python
final_answer("def add(a, b):\\n    return a + b")
```
<end_code>
"""

# SWE-bench向けの、Thought/Code/Observationループの具体例を示す文字列
_SWEBENCH_EXAMPLE = """\
Example turn:

Thought: I need to find where `is_valid_email` is defined before changing it.
Code:
```python
result = search_function_or_class_definition_in_code("is_valid_email")
print(result)
```
<end_code>

Observation: /testbed/src/mail.py:65 def is_valid_email(mail: str) -> bool:

Thought: Let me read that file around the definition.
Code:
```python
content = read_file(filepath="/testbed/src/mail.py", start_line=60, end_line=75)
print(content)
```
<end_code>
"""


def build_system_prompt(benchmark: str, sandbox_manual: str, include_example: bool = True) -> str:
    """LLMに送信する完全なシステムプロンプトを組み立てる(Section 4.1, point 6)。

    引数:
        benchmark: ベンチマーク種別。"mbpp" または "swebench" のいずれか。
        sandbox_manual: MCPToolProxy.manual_text()から取得すべき値。こうすることで
            プロンプトが常に実際に接続されているMCPサーバーの内容を反映するようになる(Section 4.2)。
        include_example: 両方のエージェントCLIでデフォルトはTrue。ベンチマークレポートの
            アブレーション実験(Section 4.7 point 5)がこの関数を複製せずに
            「Before」プロンプト(例なし)を組み立てられるように存在する引数。

    返り値:
        LLMに渡す最終的なシステムプロンプト文字列。
    """
    if benchmark == "mbpp":  # MBPPベンチマークの場合
        final_answer_doc, example = _MBPP_FINAL_ANSWER, _MBPP_EXAMPLE  # MBPP用の最終解答説明と例を選択
    elif benchmark == "swebench":  # SWE-benchベンチマークの場合
        final_answer_doc, example = _SWEBENCH_FINAL_ANSWER, _SWEBENCH_EXAMPLE  # SWE-bench用の最終解答説明と例を選択
    else:  # どちらにも一致しない未知のベンチマーク名の場合
        raise ValueError(f"Unknown benchmark: {benchmark}")  # エラーを送出する

    # 基本の枠組み説明・ツール一覧・最終解答の出し方を連結してプロンプト本体を作る
    prompt = (
        f"{FRAMEWORK_EXPLANATION}\n"
        f"## Available tools\n{sandbox_manual}\n\n"
        f"## Submitting your solution\n{final_answer_doc}\n"
    )
    if include_example:  # 例を含める設定なら
        prompt += f"## {example}"  # 具体例セクションを末尾に追加する
    return prompt  # 完成したシステムプロンプトを返す
