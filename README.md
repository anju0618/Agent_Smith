*This project has been created as part of the 42 curriculum by amakino, takawaka*

<div align="center">

# Agent_Smith

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

> *"Human beings are a disease, a cancer of this planet. You’re a plague and we are the cure."*
>
> — Agent Smith, *The Matrix*

</div>

## Table of Contents

- [English](#english)
  - [Description](#description)
  - [Instructions](#instructions)
  - [Resources](#resources)
  - [Terms](#terms)
- [日本語](#日本語)
  - [説明](#説明)
  - [手順](#手順)
  - [参考資料](#参考資料)
  - [専門用語](#専門用語)

---

## English

### Description

### Instructions

1. make venv
```sh
make install
```

### Resources

#### Search terms in Subject PDF
- [MCP (Model Context Protocol) の仕組みを知りたい！](https://qiita.com/megmogmog1965/items/79ec6a47d9c223e8cffc)
- [What is agentic coding?](https://www.ibm.com/think/topics/agentic-coding)
- [Pythonのexecで詰まった話と、それに関する一考察](http://den3.net/activity_diary/2024/09/16/6890/) - execの勉強
- [[python] 文を実行するexec, eval](https://qiita.com/Kodaira_/items/30c84806b61792b613f2)
- [言語Sandbox環境の脆弱性とその真因の考察 - RestrictedPythonを題材に](https://laysakura.github.io/2024/12/17/RestrictedPython-CVE/) - RestrictedPythonの勉強

### Terms

#### Core Agent Concepts
- **Agentic framework**: A system where an LLM autonomously reasons, writes code, executes it, and iterates until a task is solved.
- **Code Agent**: An AI system that reasons about a programming task, generates executable code, runs it in a controlled environment, uses tools, and refines its approach based on results.
- **Agentic code generation**: A paradigm where the LLM writes and executes code as part of its reasoning, rather than only returning a final answer.
- **Agent Loop (Thought → Code → Observation)**: The repeating cycle where the LLM thinks, writes code, and observes the sandboxed execution result until the task is solved.
- **Agent/Orchestrator**: The central control loop that calls the LLM, extracts code, sends it to the sandbox, reads the observation, and repeats.
- **Code Extraction**: The transformation step that pulls executable code out of the LLM's raw text response.
- **Code-based tool calling**: An approach where the LLM calls tools by writing Python code (e.g., `result = search_code(...)`) instead of a single JSON tool call, enabling persistent variables, loops, and multi-step logic.
- **System prompt**: The instructions given to the LLM describing available tools, the response format (Thought/Code/Observation), and examples of effective reasoning loops.
- **Sandbox manual**: Documentation of available MCP tools (names, descriptions, parameter types), dynamically generated from the connected MCP server and included in the system prompt.
- **final_answer()**: A built-in function always injected into the sandbox namespace (it is NOT an MCP tool) that the agent calls to signal task completion and submit its solution.

#### Sandbox & Security
- **Sandbox**: The execution boundary that safely runs LLM-generated Python code, enforcing import, filesystem, timeout, and memory restrictions; it also hosts an MCP client.
- **Interactive sandbox**: A REPL-style mode (`uv run sandbox` with no task argument) that reads and executes user-typed code line by line under the same restrictions, until `exit` or EOF.
- **Import restrictions / authorized_imports**: An allowlist of Python modules the sandboxed code may import; anything not listed is blocked by default.
- **Filesystem restrictions / allowed_directories**: An allowlist of directories (as seen from inside the sandbox) that sandboxed code may access.
- **No network access**: The sandbox must block all outbound and inbound network connections.
- **Execution timeout**: A configured time limit after which running code is force-killed (SIGTERM then SIGKILL).
- **Memory limits**: A configured RAM ceiling after which the sandboxed process is terminated.
- **Restricted builtins**: Dangerous built-in functions (e.g., `open`, `eval`, `exec`, `__import__`) that are removed or overridden to prevent privilege escalation.
#### MCP & Tooling
- **MCP (Model Context Protocol)**: An open protocol (by Anthropic) connecting an LLM/agent to external tools and data sources, exposed via a separate MCP server.
- **MCP Server**: A separate process exposing tools, resources, and prompts over stdio or streamable HTTP; only the student's own MCP server(s) are allowed.
- **MCP Client**: The component inside the sandbox that connects to an MCP server and exposes its tools as callable Python functions.
- **stdio / streamable HTTP**: The two required transport mechanisms for connecting to an MCP server.
- **Mandatory Tools**: The fixed set of MCP tools every submission must implement (file system, code search, and execution tools).
- **File System Tools**: `read_file`, `edit_file`, `list_files` — reading, editing, and listing files in the sandboxed workspace.
- **Code Search Tools**: `search_code`, `search_function_or_class_definition_in_code`, `find_references` — grep-like search, definition lookup, and usage lookup.
- **Execution Tools**: `run_tests`, `get_patch`, `run_command` — running the evaluation script, retrieving a git diff, and running arbitrary shell commands.

#### Benchmarks
- **MBPP (Mostly Basic Python Problems)**: A benchmark of short, self-contained algorithmic Python problems used to evaluate the agent.
- **SWE-bench**: A benchmark of real-world bug fixes in production repositories, evaluated inside Docker containers.
- **SWE-bench Verified**: The specific SWE-bench dataset variant used for evaluation in this project.

#### LLM & Providers
- **LLM API Providers**: Third-party services offering LLM inference (e.g., OpenRouter, Together AI, Groq, Google AI Studio, Mistral AI, Cohere, Fireworks AI, Perplexity AI, Anyscale), used within their free tiers only.
- **stop_sequences**: A generation parameter (e.g., `<end_code>`) that stops the LLM from continuing past the end of a code block, preventing it from hallucinating tool results.
- **Multi-token management / token rotation**: Supporting multiple API keys per provider and rotating between them to handle rate limits and quota exhaustion.
- **Non-Python tool-call formats**: Alternative formats the code-extraction layer must normalize into Python calls — XML tool calls (Anthropic-style `<invoke>`), JSON/Hermes tool calls (`<tool_call>`), and ReAct format (`Action:` / `Action Input:`).

#### Evaluation & Reporting
- **Model Benchmark Report (BENCHMARK_REPORT.md)**: A required report comparing at least 5 models across at least 2 providers on the same ≥3 SWE-bench tasks (setup, results table, provider reliability, intermediary metrics, ablation study, conclusions).
- **Ablation study**: A before/after comparison isolating the effect of one change (prompt, tools, or parameters) on the same tasks with the same model.
- **Intermediary metrics**: Progress indicators beyond pass/fail, such as the iteration at which the final patch's file is first touched, or the gap between tests passing and calling `final_answer`.

---

## 日本語

<div align="center">

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

> *「人間は伝染病だ。この惑星における癌細胞のようなものだ。お前たちは疫病であり、我々こそが治療薬だ。」*
>
> — エージェント・スミス、『マトリックス』

</div>

### 説明

### 手順

1. venvを作成する
```sh
make install
```

### 参考資料

#### 課題PDF内の検索キーワード
- [MCP (Model Context Protocol) の仕組みを知りたい！](https://qiita.com/megmogmog1965/items/79ec6a47d9c223e8cffc)
- [What is agentic coding?](https://www.ibm.com/think/topics/agentic-coding)
- [Loop Engineering入門：AIコーディングエージェントを動かすシステムを設計する](https://zenn.dev/suwash/articles/loop-engineering_20260610)

### 専門用語

#### エージェントの基本概念
- **agentic framework（エージェント型フレームワーク）**: LLMが自律的に推論し、コードを書き、それを実行し、タスクが解決するまで繰り返すシステム。
- **Code Agent（コードエージェント）**: プログラミングタスクについて推論し、実行可能なコードを生成し、制御された環境でそれを実行し、ツールを使い、結果をもとにアプローチを改善するAIシステム。
- **agentic code generation（エージェント型コード生成）**: LLMが最終回答だけを返すのではなく、推論の一部としてコードを書いて実行するパラダイム。
- **Agent Loop（Thought → Code → Observation ループ）**: LLMが考え、コードを書き、サンドボックスでの実行結果を観測する、という一連の流れをタスクが解決するまで繰り返すループ。
- **Agent/Orchestrator（オーケストレーター）**: LLM呼び出し、コード抽出、サンドボックスへの送信、観測結果の読み取りを繰り返す中心制御ループ。
- **Code Extraction（コード抽出）**: LLMの生テキスト応答から実行可能なコードを取り出す変換ステップ。
- **code-based tool calling（コードベースのツール呼び出し）**: LLMが単一のJSONツール呼び出しではなく、Pythonコード（例: `result = search_code(...)`）を書いてツールを呼び出す方式。変数の保持・ループ・複数ステップの合成が可能になる。
- **system prompt（システムプロンプト）**: 利用可能なツールの説明、応答フォーマット（Thought/Code/Observation）、効果的な推論ループの例をLLMに与える指示文。
- **sandbox manual（サンドボックスマニュアル）**: 接続中のMCPサーバーから動的に生成される、利用可能なMCPツール（名前・説明・パラメータ型）のドキュメント。システムプロンプトに含める。
- **final_answer()**: サンドボックスの実行名前空間に常に注入される組み込み関数（MCPツールではない）。エージェントがこれを呼ぶとタスク完了を示し、解答が確定する。

#### サンドボックスとセキュリティ
- **Sandbox（サンドボックス）**: LLMが生成したPythonコードを安全に実行する実行境界。import・ファイルシステム・タイムアウト・メモリ制限を課し、内部にMCPクライアントを持つ。
- **interactive sandbox（インタラクティブサンドボックス）**: タスク引数なしで `uv run sandbox` を実行するREPL形式のモード。同じ制限下でユーザー入力のコードを1行ずつ実行し、`exit` またはEOFで終了する。
- **import restrictions / authorized_imports（importの許可リスト）**: サンドボックス内コードがimportできるPythonモジュールの許可リスト。リストにないものはデフォルトで拒否される。
- **filesystem restrictions / allowed_directories（ファイルシステム制限）**: サンドボックス内コードがアクセスできるディレクトリの許可リスト（サンドボックス視点のパス）。
- **no network access（ネットワーク遮断）**: サンドボックスは送受信両方向のネットワーク接続をすべて遮断しなければならない。
- **execution timeout（実行タイムアウト）**: 設定された時間を超えて実行されたコードを強制終了（SIGTERM→SIGKILL）する仕組み。
- **memory limits（メモリ上限）**: 設定されたRAM使用量を超えたプロセスを終了させる仕組み。
- **restricted builtins（組み込み関数の制限）**: 権限昇格を防ぐため、危険な組み込み関数（`open`, `eval`, `exec`, `__import__` など）を削除・上書きすること。
#### MCPとツール
- **MCP (Model Context Protocol)**: Anthropicが策定した、LLM/エージェントと外部ツール・データソースを繋ぐオープンプロトコル。別プロセスのMCPサーバーとして公開される。
- **MCP Server（MCPサーバー）**: stdioまたはstreamable HTTP経由でツール・リソース・プロンプトを公開する別プロセス。自作のMCPサーバーのみ使用可能。
- **MCP Client（MCPクライアント）**: サンドボックス内でMCPサーバーに接続し、そのツールをPython関数として公開するコンポーネント。
- **stdio / streamable HTTP**: MCPサーバー接続時に必須対応となる2種類のトランスポート方式。
- **Mandatory Tools（必須ツール）**: すべての提出物が実装しなければならない、固定のMCPツール群（ファイルシステム系・コード検索系・実行系）。
- **File System Tools（ファイルシステム系ツール）**: `read_file` / `edit_file` / `list_files` — サンドボックス化された作業領域内のファイルの読み込み・編集・一覧取得。
- **Code Search Tools（コード検索系ツール）**: `search_code` / `search_function_or_class_definition_in_code` / `find_references` — grep風検索、定義箇所の検索、参照箇所の検索。
- **Execution Tools（実行系ツール）**: `run_tests` / `get_patch` / `run_command` — 評価スクリプトの実行、git diffの取得、任意のシェルコマンド実行。

#### ベンチマーク
- **MBPP (Mostly Basic Python Problems)**: エージェントを評価するための、短く自己完結したアルゴリズム系Python問題のベンチマーク。
- **SWE-bench**: 実在のプロダクションリポジトリにおける実際のバグ修正を、Dockerコンテナ内で評価するベンチマーク。
- **SWE-bench Verified**: 本課題の評価で使用される、SWE-benchの特定のデータセットバリアント。

#### LLMとプロバイダ
- **LLM API Providers（LLM APIプロバイダ）**: LLM推論を提供するサードパーティサービス（OpenRouter, Together AI, Groq, Google AI Studio, Mistral AI, Cohere, Fireworks AI, Perplexity AI, Anyscale など）。無料枠のみで利用すること。
- **stop_sequences**: LLMがコードブロックの終端を超えて生成を続け、ツールの実行結果を「幻覚」で生成してしまうのを防ぐための生成パラメータ（例: `<end_code>`）。
- **Multi-token management / token rotation（複数トークン管理・トークンローテーション）**: プロバイダごとに複数のAPIキーを持ち、レート制限やクォータ枯渇に対応するためにローテーションさせること。
- **Non-Python tool-call formats（Python以外のツール呼び出し形式）**: コード抽出層がPython関数呼び出しに正規化すべき代替形式。Anthropic系XMLツール呼び出し（`<invoke>`）、JSON/Hermes系ツール呼び出し（`<tool_call>`）、ReAct形式（`Action:` / `Action Input:`）。

#### 評価とレポート
- **Model Benchmark Report (BENCHMARK_REPORT.md)**: 最低5モデル×最低2プロバイダを、同一の3タスク以上のSWE-benchタスクで比較する必須レポート（Setup・結果表・プロバイダ信頼性・中間指標・アブレーションスタディ・結論を含む）。
- **Ablation study（アブレーションスタディ）**: 同一タスク・同一モデルで、プロンプト/ツール/パラメータなど1つの変更前後を比較する検証。
- **Intermediary metrics（中間指標）**: Pass/Fail以外の進捗指標。例えば最終パッチに含まれるファイルに最初にアクセスしたステップ数、テスト通過から`final_answer`呼び出しまでの反復数など。
