*This project has been created as part of the 42 curriculum by \<amakino\>, \<takawaka\>.*

# Agent Smith

## Description

LLM-driven **Code Agent**: reasons, writes Python, runs it in a sandboxed
MCP-tool environment, loops until `final_answer(...)`. Supports **MBPP**
(short problems, `run_tests`) and **SWE-bench** (Docker repo fixes,
submitted as a `git diff`).

## Instructions

```sh
uv sync && cp .env.example .env   # fill in free-tier API keys
uv run sandbox                    # interactive REPL
uv run python -m agent_mbpp     --task-file cache/mbpp_task.json --output cache/mbpp_solution.json --model-name "qwen/qwen3.8-27b" --provider-url "https://api.groq.com/openai/v1"
uv run python -m agent_swebench --task-file cache/swebench_task.json --output cache/swebench_solution.json --model-name "minimax/minimax-m3:free" --provider-url "https://openrouter.ai/api/v1"
make test   # pytest, no network/keys needed
```

Needs Linux `unshare`+`bubblewrap`; missing them fails the sandbox closed,
never falls back to an in-process runner. Task files come from moulinette
(`moulinette_eval dump mbpp/swebench`). `solution.json` is always written,
even on failure.

## System architecture

```
LLM API <-> Orchestrator -> code_extraction -> Sandbox -> MCPToolProxy -> mcp_tools_*.py
```

`orchestrator.py` runs the loop; `code_extraction.py` normalizes tool-call
formats (XML/JSON/ReAct → Python); `sandbox/` is the execution boundary;
`llm/` abstracts providers with key rotation/fallback; `agent_mbpp.py` /
`agent_swebench.py` are the CLIs that write `solution.json`.

## Agent loop explanation

1. Send the conversation to the LLM (`<end_code>` stop sequence).
2. Extract code; no block found → `[NoCodeBlock]` fed back honestly.
3. Run in `Sandbox.run()`; `final_answer()` ends the loop, anything else
   becomes the next Observation.
4. Log a `StepMetrics` entry, repeat until done or a limit is hit.

## Sandbox design

`sandbox/executor.py` runs code in a network-disabled `unshare`+`bubblewrap`
worker: AST + runtime import allowlist, path-checked `open`, wall-clock and
`RLIMIT_AS` limits, restricted builtins, and a default-deny dunder-attribute
allowlist blocking escapes like `().__class__.__bases__` walks. `final_answer()`
is an injected closure never caught by the generic handler. `SandboxConfig`
(`models.py`) loads from JSON (`sandbox_template.json`); `sandbox/cli.py` is
the interactive REPL.

## Tool implementation details

- **MCP** (`sandbox/mcp_client.py`): tools discovered via `list_tools()`,
  never hardcoded; stdio and streamable HTTP both supported.
- **MBPP**: `run_tests(code, test_list)` → `{"success": bool, "output": str}`.
- **SWE-bench**: all 9 mandatory tools, rooted at `TESTBED_PATH`; `get_patch`
  uses `git -c core.fileMode=false diff`.
- **Docker** (`docker_runner.py`): only the MCP tool server runs inside the
  container; the sandbox itself stays on the host.

## Benchmark results and analysis

See `BENCHMARK_REPORT.md`: 5 models × 3 providers on 3 SWE-bench tasks, 3
independently-verified passes, plus an ablation study.

## Resources

#### Classic references (Japanese-language sources)
- MCP overview — 「MCPとは？AIエージェント時代の標準規格を徹底解説」(NTT東日本) — https://business.ntt-east.co.jp/content/cloudsolution/ih_column-193.html
- MBPP / HumanEval overview — 「LLM評価指標、HumanEvalとMBPPとは？」(note) — https://note.com/fukudawataru/n/n745412f5659d
- SWE-bench, official Japanese translation — https://github.com/SWE-bench/SWE-bench/blob/main/docs/other_languages/README_JP.md
- ReAct — 「意思決定を行うためのprompt技術 ReAct」(Zenn) — https://zenn.dev/jow/articles/927395f5dbe694
- CodeAct — 「Microsoft Agent Framework CodeAct入門」(Qiita) — https://qiita.com/kai_kou/items/7c2a23e6aa05e860f99c
- Python `signal`/`resource`/`ast` 公式ドキュメント(日本語) — https://docs.python.org/ja/3/library/signal.html

#### How AI was used in this project
AI coding assistants helped with code generation, debugging, and
documentation drafting, under our direction and review. Claude Code was also
given this prompt to add line-by-line Japanese comments across the codebase:
「全ファイルのコードに一行一行わかりやすい解説コメントアウトをつけて.pythonを知らない人でもわかるように詳しく」
("Add an easy-to-understand explanatory comment to every line of code in
every file.")

---

## 日本語セクション（要約）

**Agent Smith** は、LLM自身が思考しPythonコードを書き、それをサンドボックス内で
実行し、結果を観察して次の一手を決める、というループ(Thought → Code →
Observation)を回して課題を解く自律エージェントです。MBPP(短いアルゴリズム問題)
とSWE-bench(実リポジトリのバグ修正、Dockerコンテナ内で実行)の両方に対応します。

- **使い方**: `uv sync` で依存関係を入れ、`.env` にAPIキーを設定。
  `uv run sandbox` で対話型サンドボックスを試せます。
- **アーキテクチャ**: Orchestrator(`orchestrator.py`)がLLM呼び出し→コード抽出
  →サンドボックス実行→観察結果のフィードバックを繰り返します。ツール呼び出しの
  フォーマット違い(XML/JSON/ReAct等)は`code_extraction.py`が吸収し、サンドボッ
  クスは常にPython関数呼び出しだけを見ます。
- **サンドボックス**: `unshare`+`bubblewrap`によるOS隔離、importの許可リスト、
  ファイルアクセス制限、タイムアウト/メモリ上限に加え、Pythonの内部オブジェクト
  を辿って制限を回避する典型的な「サンドボックス脱出」(`().__class__.__bases__`
  経由)を防ぐdunder属性の許可リストを実装しています。
- **ツール**: MCP(Model Context Protocol)経由でファイル読み書き・コード検索・
  テスト実行などのツールを動的に発見して呼び出します。SWE-bench用は9個の必須
  ツールを実装。
- **ベンチマーク結果**: 詳細は`BENCHMARK_REPORT.md`を参照(5モデル×3プロバイダ
  ×3タスクで比較し、3件を実際に検証済み)。
- **AIの利用について**: 開発中はAIコーディングアシスタントをコード生成・デバッ
  グ・ドキュメント作成の補助に使い、内容は自分たちで確認・レビューしました。
  また、Claude Codeに「全ファイルのコードに一行一行わかりやすい解説コメントアウト
  をつけて.pythonを知らない人でもわかるように詳しく」と指示し、コードベース全体に日本語コメントを追加しました。
