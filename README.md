*This project has been created as part of the 42 curriculum by \<amakino\>, \<takawaka\>.*

<div align="center">

# Agent Smith

```
⠀⠀⠀⠀⠀⠀⣀⣤⣴⣶⣶⣦⣤⡀⠀⠀⠀⠀⠀⠀⠀
⠀⠀⠀⢀⣴⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣦⣄⠀⠀⠀
⠀⠀⢀⣾⣿⣿⣿⠿⣿⣿⣿⣿⣿⣿⠿⣿⣿⣿⣷⡀⠀
⠀⠀⢸⣿⣿⠋⠀⠀⠸⠿⠿⠿⠿⠇⠀⠀⠙⢿⣿⡇⠀
⠀⠀⢸⣿⠇⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠘⣿⡇⠀
⠀⠀⢸⣿⠠⣤⣄⣀⠀⠀⠀⠀⠀⠀⣀⣠⣤⠀⣿⡇⠀
⠀⠀⣸⣿⣠⣴⣿⣿⣿⣷⣄⣠⣾⣿⣿⣿⣦⣄⣿⣇⠀
⣠⣼⣿⣿⢹⣿⣿⣿⣿⡿⠉⠉⢿⣿⣿⣿⣿⡇⣿⣿⡇
⣿⣿⣿⣿⠀⠈⠉⠁⠀⠀⠀⠀⠀⠀⠉⠉⠁⠀⣿⣿⠇
⢸⡇⢹⣿⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣿⡏⠀
⢸⡇⢸⣿⠀⠀⠀⠀⢠⣤⣶⣶⣦⡄⠀⠀⠀⠀⣿⡇⠀
⢸⡇⠘⢿⣷⣄⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣠⣾⡿⠃⠀
⢸⣇⠀⠈⢻⣿⣷⣤⡀⠀⠀⠀⠀⢀⣴⣾⣿⡏⠀⠀⠀
⠀⠻⢷⣦⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡄⠀⠀
⠀⠀⠀⠸⠿⠿⠿⠿⠿⠏⠀⠀⠙⠿⠿⠿⠿⠿⠇⠀⠀
```

> *"Human beings are a disease, a cancer of this planet. You're a plague and we are the cure."*
>
> — Agent Smith, *The Matrix*

</div>

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

## Running the exam scripts & moulinette

`exams.zip`/`moulinette.zip` (already sitting next to this repo, one
directory up) unzip into the layout the exam scripts expect: this repo is
the "student" project, and `exams/`/`moulinette/` are **sibling**
directories, not subfolders of this repo. Keep it that way — the
anti-cheat check greps everything under `--student-path` recursively, so
extracting `moulinette/` *inside* this repo makes it flag moulinette's own
code as if it were yours.

```sh
# one-time setup, run from the parent directory of this repo
cd ..
unzip -n moulinette.zip && unzip -n exams.zip
cd moulinette && uv sync && cd ../Agent_Smith
cp .env.example .env   # fill in real API keys
```

Layout after that:

```
42cadet/
├── Agent_Smith/   (this repo == "student")
├── exams/         (unzipped exams.zip)
└── moulinette/    (unzipped moulinette.zip)
```

### Exam scripts (run from inside this repo)

```sh
../exams/exam_sandbox.sh   --student-path . --moulinette-path ../moulinette --env-file .env   # ~2 min
../exams/exam_anticheat.sh --student-path .                                                    # ~10 sec
../exams/exam_mbpp.sh      --student-path . --moulinette-path ../moulinette --env-file .env   # ~5 min
../exams/exam_swebench.sh  --student-path . --moulinette-path ../moulinette --env-file .env   # ~45 min, needs Docker running
```

Results land in `../evaluations/<sandbox|mbpp|swebench>/<timestamp>/`.
See `../exams/exam_scripts_instructions.md` for what each script checks and
how to interpret a failure.

### Connecting to the sandbox's MCP URL

`exam_sandbox.sh` spins up an MCP server over HTTP on a port and points the
sandbox at it; do the same manually with:

```sh
uv run --directory ../moulinette python ../exams/sandbox_tests/simple_mcp_server.py --http --port 18080 &
uv run sandbox --mcp-server http://localhost:18080/mcp
```

### Moulinette directly (dump / run-agent / validate / display)

```sh
cd ../moulinette
uv run moulinette_eval dump mbpp --task-id 42 --output ../Agent_Smith/cache/mbpp_task.json
cd ../Agent_Smith
uv run python -m agent_mbpp --task-file cache/mbpp_task.json --output cache/mbpp_solution.json
cd ../moulinette
uv run moulinette_eval validate mbpp ../Agent_Smith/cache/mbpp_task.json ../Agent_Smith/cache/mbpp_solution.json
uv run moulinette_eval display ../Agent_Smith/cache/mbpp_solution.json
```

Or the one-shot wrapper (dump → run-agent → validate for a single task):

```sh
cd ../moulinette
./quickstart.sh mbpp     --student-path ../Agent_Smith --model-name "qwen/qwen3.8-27b" --provider-url "https://api.groq.com/openai/v1"
./quickstart.sh swebench --student-path ../Agent_Smith --model-name "minimax/minimax-m3:free" --provider-url "https://openrouter.ai/api/v1"
```

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
- **exam / moulinette の動かし方**: `moulinette.zip`/`exams.zip` はこのリポジトリの
  一つ上の階層に展開し(`exams/`・`moulinette/`をこのリポジトリの**兄弟**ディレクト
  リにする。中に入れるとanti-cheatチェックがmoulinette自身のコードを誤検知する)、
  `cd ../moulinette && uv sync` を実行。採点スクリプトは
  `../exams/exam_sandbox.sh --student-path . --moulinette-path ../moulinette --env-file .env`
  のように呼び出す(詳細コマンドは上の英語セクション "Running the exam scripts &
  moulinette" を参照)。サンドボックスをMCPのURL経由で叩きたいときは、
  MCPサーバーをポートを開いて起動してから `uv run sandbox --mcp-server
  http://localhost:PORT/mcp` で接続する("Connecting to the sandbox's MCP URL"
  参照)。
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

### コマンド集

#### セットアップ

```sh
# Agent Smith(このリポジトリ)
uv sync && cp .env.example .env

# exams.zip / moulinette.zip を一つ上の階層に展開(兄弟ディレクトリにする)
cd ..
unzip -n moulinette.zip && unzip -n exams.zip
cd moulinette && uv sync && cd ../Agent_Smith
```

#### Agent Smith を直接動かす

```sh
uv run sandbox                    # 対話型サンドボックスREPL
uv run python -m agent_mbpp     --task-file cache/mbpp_task.json --output cache/mbpp_solution.json --model-name "qwen/qwen3.8-27b" --provider-url "https://api.groq.com/openai/v1"
uv run python -m agent_swebench --task-file cache/swebench_task.json --output cache/swebench_solution.json --model-name "minimax/minimax-m3:free" --provider-url "https://openrouter.ai/api/v1"
make test                         # pytest、ネットワーク/APIキー不要
```

#### サンドボックスのMCP URL接続

```sh
uv run --directory ../moulinette python ../exams/sandbox_tests/simple_mcp_server.py --http --port 18080 &
uv run sandbox --mcp-server http://localhost:18080/mcp
```

#### exam スクリプト(このリポジトリ直下から実行)

```sh
../exams/exam_sandbox.sh   --student-path . --moulinette-path ../moulinette --env-file .env   # ~2分
../exams/exam_anticheat.sh --student-path .                                                    # ~10秒
../exams/exam_mbpp.sh      --student-path . --moulinette-path ../moulinette --env-file .env   # ~5分
../exams/exam_swebench.sh  --student-path . --moulinette-path ../moulinette --env-file .env   # ~45分、Docker起動が必要
```

結果は `../evaluations/<sandbox|mbpp|swebench>/<日時>/` に保存されます。

#### moulinette を直接操作(dump / run-agent / validate / display / select)

```sh
cd ../moulinette

# タスクをダンプ(ランダム or 指定ID)
uv run moulinette_eval dump mbpp --output ../Agent_Smith/cache/mbpp_task.json
uv run moulinette_eval dump mbpp --task-id 42 --output ../Agent_Smith/cache/mbpp_task.json
uv run moulinette_eval dump swebench --output ../Agent_Smith/cache/swebench_task.json
uv run moulinette_eval dump swebench --task-id sympy__sympy-23534 --output ../Agent_Smith/cache/swebench_task.json

# エージェントをタイムアウト付きで実行
uv run moulinette_eval run-agent 120 "python -m agent_mbpp --task-file task.json --output solution.json"

# 採点(正解性+メトリクス)
uv run moulinette_eval validate mbpp ../Agent_Smith/cache/mbpp_task.json ../Agent_Smith/cache/mbpp_solution.json
uv run moulinette_eval validate swebench ../Agent_Smith/cache/swebench_task.json ../Agent_Smith/cache/swebench_solution.json
uv run moulinette_eval validate mbpp ../Agent_Smith/cache/mbpp_task.json ../Agent_Smith/cache/mbpp_solution.json --skip-metrics

# メトリクスのみ
uv run moulinette_eval validate_metrics mbpp ../Agent_Smith/cache/mbpp_solution.json
uv run moulinette_eval validate_metrics swebench ../Agent_Smith/cache/swebench_solution.json

# 結果表示
uv run moulinette_eval display ../Agent_Smith/cache/mbpp_solution.json
uv run moulinette_eval display ../Agent_Smith/cache/mbpp_solution.json --full

# 本番と同じランダム抽選(SWE-bench: 6問から3問)
uv run moulinette_eval select swebench --count 3
uv run moulinette_eval select swebench --count 3 --seed 42 --output selection.json

# 引数なしで全コマンド一覧(Fire CLIなので --help だけだとモジュールのdocstringしか出ない)
uv run moulinette_eval
```

#### moulinette の一発ラッパー(dump → run-agent → validate)

```sh
cd ../moulinette
./quickstart.sh mbpp     --student-path ../Agent_Smith --model-name "qwen/qwen3.8-27b" --provider-url "https://api.groq.com/openai/v1"
./quickstart.sh swebench --student-path ../Agent_Smith --model-name "minimax/minimax-m3:free" --provider-url "https://openrouter.ai/api/v1"
./quickstart.sh mbpp     --student-path ../Agent_Smith --task-id 42
./quickstart.sh swebench --student-path ../Agent_Smith --seed 42
```

#### moulinette のタスク探索用 Fire CLI

```sh
cd ../moulinette

# MBPP
uv run moulinette_mbpp list_tasks
uv run moulinette_mbpp list_tasks --split test
uv run moulinette_mbpp get_task 42
uv run moulinette_mbpp evaluate_task_solution 42 "def similar_elements(a, b): return tuple(set(a) & set(b))"

# SWE-bench
uv run moulinette_swebench list_instances
uv run moulinette_swebench list_instances --repo_pattern "sympy"
uv run moulinette_swebench get_instance_info sympy__sympy-23534
uv run moulinette_swebench eval sympy__sympy-23534 --patch patch.diff
```

#### 個別タスクの手動再実行(exam_mbpp.sh が何をしているかを手元で再現)

```sh
cd ../moulinette
uv run moulinette_eval dump mbpp --task-id 42 --output ../cache/task.json
cd ../Agent_Smith
uv run python -m agent_mbpp --task-file ../cache/task.json --output ../cache/solution.json
cd ../moulinette
uv run moulinette_eval validate mbpp ../cache/task.json ../cache/solution.json
```

詳細(各exam scriptの合否判定・失敗の読み方)は `../exams/exam_scripts_instructions.md`
と `../exams/moulinette_helper.md` を参照。
