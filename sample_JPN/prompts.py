"""System prompt construction shared by both agents (Section 4.1, point 6):
clear tool documentation, structured Thought/Code/Observation slots, and an
example of an effective reasoning loop.
"""
# ============================================================================
# 【日本語解説】このファイルの役割
# ============================================================================
# build_system_prompt() が、以下の4パーツを文字列結合してシステムプロンプト
# 全文を組み立てる。ファイル全体で120行弱、うち大半は定数として定義された
# 素のテキストブロックで、ロジックは末尾の build_system_prompt() 関数1つだけ。
#
#   パーツ1: FRAMEWORK_EXPLANATION       … 両ベンチマーク共通のルール説明
#   パーツ2: sandbox_manual（引数で注入） … 接続中のMCPツール一覧（動的パート）
#   パーツ3: final_answer の使い方        … MBPP/SWE-benchで別文面
#   パーツ4: worked example               … 実例（アブレーション実験用にON/OFF可能）
#
# 【重要】以下の英語の複数行文字列（FRAMEWORK_EXPLANATION,
# _MBPP_FINAL_ANSWER, _SWEBENCH_FINAL_ANSWER, _MBPP_EXAMPLE,
# _SWEBENCH_EXAMPLE）は、実際にLLMへ送信されるプロンプトそのものです。
# これらは翻訳・変更すると実際のエージェントの挙動が変わってしまうため、
# 意図的に英語のまま残し、日本語コメントは各ブロックの外側にのみ
# 付けています。
# ============================================================================
from __future__ import annotations

# ----------------------------------------------------------------------------
# 【日本語解説】パーツ1: FRAMEWORK_EXPLANATION
# ----------------------------------------------------------------------------
# Thought → Code → Observation ループのルールをLLMに説明する、両ベンチマーク
# 共通の固定テキスト。要点:
#   - Thought欄に理由付けを、Code欄に「1つだけ」の```python ... ```ブロックを
#     書かせ、必ず <end_code> で終わらせる（stop sequenceとして機能する
#     トークン。orchestrator.py の OrchestratorConfig.stop_sequences 参照）。
#   - 「ツールは必ずキーワード引数で呼べ」という指示があるが、これは
#     あくまで助言であり保証ではない。実際の強制は
#     sandbox/mcp_client.py の _make_wrapper() 側（位置引数もキーワード
#     引数も両方受け付けるように作られている）で保証されている。
#   - 「Observationを絶対に自分で推測するな」という指示は、LLMが実行結果を
#     幻覚で先読みしてしまう事故を防ぐための重要なルール。
# ----------------------------------------------------------------------------
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

# ----------------------------------------------------------------------------
# 【日本語解説】パーツ3: final_answer の使い方（ベンチマークごとに別文面）
# ----------------------------------------------------------------------------
# _MBPP_FINAL_ANSWER: 「テストが通ったら次のターンで即 final_answer を呼べ、
# 再検証するな」という指示は、トークン予算・イテレーション予算をエージェント
# 自身に節約させるためのプロンプトレベルのガードレール。
# ----------------------------------------------------------------------------
_MBPP_FINAL_ANSWER = """\
Call final_answer(code) exactly once, where `code` is a string containing the
complete Python function that solves the task (matching the given function
signature). Example: final_answer("def add(a, b):\\n    return a + b")

As soon as run_tests(...) reports {"success": true}, call final_answer with
that exact code immediately in your NEXT turn - do not re-verify a solution
that already passed, and do not keep exploring alternatives. Every extra turn
spends part of your limited token and iteration budget.
"""

# ----------------------------------------------------------------------------
# 【日本語解説】_SWEBENCH_FINAL_ANSWER
# ----------------------------------------------------------------------------
# 「get_patch()を呼べ、自分でパッチを手書きするな」という一文が重要。
# LLMが差分を手で組み立てると、行番号やコンテキスト行のズレで git apply
# 不能な壊れたdiffになりがちなので、必ずツール（git diff の薄いラッパー、
# mcp_tools_swebench.py の get_patch()）を経由させて機械的に正しいものだけを
# 提出させている。
# ----------------------------------------------------------------------------
_SWEBENCH_FINAL_ANSWER = """\
Call final_answer(get_patch()) exactly once, once you have verified your fix
with run_tests(). get_patch() returns the unified git diff of every change you
made to the repository - do not hand-write the patch yourself.
"""

# ----------------------------------------------------------------------------
# 【日本語解説】パーツ4: worked example（MBPP用）
# ----------------------------------------------------------------------------
# Thought→Code→Observationを実際にどう書けばいいかの具体例。
# BENCHMARK_REPORT.md のアブレーション実験によると、この worked example を
# システムプロンプトから抜くと、モデルはツール呼び出し結果を print() せずに
# コードを実行し続け、sandbox_output が空のまま final_answer() を呼んで
# しまう（success: true を報告するのに実際には空文字列のパッチだった、
# という事故が記録されている）。つまりこの例は単なる補足ではなく、
# 「正しく動くための実質的な必須パーツ」に近い。
# ----------------------------------------------------------------------------
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

# ----------------------------------------------------------------------------
# 【日本語解説】パーツ4: worked example（SWE-bench用）
# ----------------------------------------------------------------------------
# 「まず定義を探す → 該当箇所を読む」という調査の型を、実際のツール呼び出し
# 込みで示している。MBPP版と同様、これが無いと結果を print() せずに進んで
# しまう事故につながる。
# ----------------------------------------------------------------------------
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
    """Assemble the full system prompt sent to the LLM (Section 4.1, point 6).

    `sandbox_manual` should come from MCPToolProxy.manual_text() so the prompt
    always reflects whichever MCP server is actually connected (Section 4.2).

    `include_example` defaults to True for both agent CLIs; it exists so the
    benchmark report's ablation study (Section 4.7 point 5) can build the
    "before" prompt (no worked example) without duplicating this function.
    """
    # =====================================================================
    # 【日本語解説】組み立て本体
    # =====================================================================
    # この関数自身はツール名を1つも知らない。sandbox_manual 引数として
    # MCPToolProxy.manual_text()（sandbox/mcp_client.py）の出力をそのまま
    # 受け取り、"## Available tools\n{sandbox_manual}\n\n" として埋め込む
    # だけ。別のMCPサーバーに繋ぎ変えるだけでこの部分も自動的に変わる
    # ため、「未知のMCPサーバーでテストされる」という課題要件に対応できる。
    # =====================================================================
    if benchmark == "mbpp":
        final_answer_doc, example = _MBPP_FINAL_ANSWER, _MBPP_EXAMPLE
    elif benchmark == "swebench":
        final_answer_doc, example = _SWEBENCH_FINAL_ANSWER, _SWEBENCH_EXAMPLE
    else:
        # 未知の benchmark 文字列が来たら黙ってどちらかにフォールバック
        # するのではなく即座に ValueError。システムプロンプトの中身を
        # 静かに間違えるくらいなら、起動直後にはっきり落ちたほうがいい、
        # という判断。
        raise ValueError(f"Unknown benchmark: {benchmark}")

    prompt = (
        f"{FRAMEWORK_EXPLANATION}\n"
        f"## Available tools\n{sandbox_manual}\n\n"
        f"## Submitting your solution\n{final_answer_doc}\n"
    )
    if include_example:
        # include_example は本番の2つのCLI（agent_mbpp.py / agent_swebench.py）
        # では常に True で呼ばれる。False にできるのは
        # BENCHMARK_REPORT.md のアブレーション実験専用——「worked example
        # だけを抜いた場合に何が起きるか」を検証するために、この関数を
        # 複製せず1引数だけで切り替えられるようにしてある。
        prompt += f"## {example}"
    return prompt
