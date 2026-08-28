# Agent Smith — 課題説明とタスク

## この課題は何か
「自律的にコーディング課題を解くAIエージェント」を自分で実装する課題。
LLMに最終回答をそのまま出させるのではなく、**Thought → Code → Observation** のループを回させる。
LLMが書いたPythonコードを「サンドボックス」で安全に実行し、その実行結果(Observation)を
またLLMに読ませて次の一手を考えさせる、という自律ループを自作する。

対象ベンチマークは2つ:
- **MBPP**: 単発のアルゴリズム系Python問題
- **SWE-bench**: 実在リポジトリの本物のバグ修正(Dockerコンテナ内で完結)

キーワード: Code Agent / MCP (Model Context Protocol) / サンドボックス実行 / LLMベンチマーク比較

言語・ツール指定: **Python 3.10 必須、パッケージ管理は uv 必須**。
smolagents / llama-index / langgraph / crewai / autogen 等の「エージェント編成ロジックを肩代わりするライブラリ」は禁止。
エージェントループ自体は自作すること。

## 全体アーキテクチャ(公式の説明)
1. **Agent/Orchestrator**: 中心ループ。LLM呼び出し→コード抽出→サンドボックス実行→観測結果を読む、を繰り返す。
2. **Code Extraction**: LLM応答からコードを取り出す変換ステップ。
3. **Sandbox**: 実行境界。import/ファイルアクセス/タイムアウト/メモリ等を制限する。中にMCPクライアントを持つ。
4. **final_answer()**: サンドボックスが注入する組み込み関数。MCPツールではない。
5. **MCP Server**: 別プロセスで動くツール群(stdio or HTTP)。自作のMCPサーバーのみ使用可。

## タスクリスト

### 0. 環境準備
- [x] `uv` をインストールし、Python 3.10 プロジェクトを `uv init` で作成
- [] `moulinette.zip` を展開し、`moulinette/` の中身(README.md, models_public.py, __main__.py, tests/)を読む
  - `moulinette_eval dump/validate` の使い方、`sanitized_tasks.json`(MBPPデータ)を確認
- [] `.env` からAPIキーを読む設計にする(コードへのハードコード厳禁 = 即失格)

### 1. コア: Agent/Orchestratorループ
- [] Thought → Code → Observation ループの実装(`orchestrator.py`。CodeExtractor/SandboxはProtocolのみで、section 2/3の実装待ち)
- [] システムプロンプト設計(利用可能ツールの説明、Thought/Code/Observationの例、有効な推論ループの例)
- [] `stop_sequences`(`<end_code>` 等)をLLM API呼び出しに設定し、ツール出力を待たずに続きを幻覚生成しないようにする(`llm_client.py`)

### 2. コード抽出レイヤー(複数フォーマット対応)
- [ ] Pythonコードブロック(` ```python ... ``` <end_code>`)を主形式として対応
- [ ] Anthropic系XML tool call (`<invoke name="..."><parameter>...</parameter></invoke>`) を変換
- [ ] JSON/Hermes tool call (`<tool_call>{"name":...,"arguments":{...}}</tool_call>`) を変換
- [ ] ReAct形式(`Action: tool_name` / `Action Input: {...}`) を変換
- [ ] 非Python形式は `result = read_file(filepath="...")` のようなPython関数呼び出しに正規化してからサンドボックスへ渡す

### 3. サンドボックス
- [ ] `uv run sandbox` でインタラクティブREPLが起動すること(exit / EOF(Ctrl+D)で終了)
- [ ] `uv run sandbox sandbox_template.json` でカスタム設定読み込み
- [ ] `uv run sandbox --mcp-stdio "..."` / `--mcp-server <URL>` でMCP接続切り替え
- [ ] `final_answer(answer)` 関数を常にサンドボックス名前空間に注入(MCPツールとは独立)
- [ ] セキュリティ制約を実装(**外部ライブラリ禁止、標準ライブラリのみ**):
  - [ ] importホワイトリスト(`SandboxConfig.authorized_imports`、`.*`でサブモジュールも許可)
  - [ ] ファイルシステム制限(`allowed_directories`、サンドボックスプロセスから見えるパスで判定)
  - [ ] ネットワーク遮断(送受信とも)
  - [ ] 実行タイムアウト(`max_execution_time_seconds`、SIGTERM→SIGKILLで強制終了)
  - [ ] メモリ上限(`max_memory_mb`)
  - [ ] 危険な組み込み関数の除去/上書き
  - [ ] `KeyboardInterrupt`/`SystemExit` は握りつぶさずエージェントループまで伝播させる
- [ ] MCPサーバー接続(stdio・streamable HTTP両対応)、ツールをPython関数として動的公開
- [ ] 「サンドボックスマニュアル」をMCPツールのスキーマから動的生成し、システムプロンプトに含める
- [ ] Pydanticモデル `SandboxConfig` をJSON設定ファイルから読み込めるようにする
  - subject記載の `SandboxConfig` デフォルト値(`Agent_Smith.pdf` V.2, p.16):
    - `authorized_imports`: `"math"`, `"math.*"`, `"collections"`, `"collections.*"`, `"itertools"`, `"re"`, `"json"`, `"typing"`, `"typing.*"`, `"functools"`, `"operator"`, `"heapq"`, `"bisect"`, `"copy"`, `"string"`, `"random"`, `"datetime"`, `"datetime.*"`, `"array"`, `"cmath"`
    - `allowed_directories`: `"/testbed"`, `"/tmp/agent"`
    - `max_execution_time_seconds`: `30` / `max_memory_mb`: `512`
    - `models.py` の `SandboxConfig.authorized_imports` は `default_factory=list`(空)なので、上記の具体的なデフォルトリストは自分で埋める
- [ ] サンドボックスは以下の状況でLLMに明示的なフィードバックを返す:
  - [ ] コードブロックが見つからない
  - [ ] コードブロックは壊れていたが解釈できた(その方法を説明)
  - [ ] タイムアウトで部分出力
  - [ ] ツール出力がサイズ制限で切り詰められた
  - [ ] 編集がシンタックスエラー/lint違反を起こした

### 4. 共通出力モデル
- [ ] `StepMetrics` Pydanticモデル実装(1ステップごとの入出力トークン、時間、raw応答、sandbox_input/output、retries等)
- [ ] `SolutionOutput` Pydanticモデル実装(task_id, benchmark, success, solution, iterations, total_*, steps, system_prompt, error, timestamp)

### 5. MBPPエージェント
- [ ] `agent_mbpp` CLI (`--task-file` / `--output` / `--model-name` / `--provider-url`)
- [ ] `mcp_tools_mbpp.py` をリポジトリルートに配置
- [ ] `run_tests(code, test_list)` ツール実装(JSON `{success, output}` を返す)
- [ ] `MBPPTaskInput` Pydanticモデル
- [ ] `max_iterations` 等を設定可能に(上限: 反復10 / 入力トークン6,000 / 出力トークン1,500 / タイムアウト120秒 — subject 6.1.1で確認、旧記述は入出力を取り違えていたので修正)

### 6. SWE-benchエージェント
- [ ] `agent_swebench` CLI (同様のインターフェース)
- [ ] `mcp_tools_swebench.py` をリポジトリルートに配置
- [ ] サンドボックスをDocker内に置くか、ホスト上でMCPツールがDockerへブリッジするか設計判断
- [ ] `TESTBED_PATH` 環境変数からリポジトリルートを取得(moulinetteが設定する)
- [ ] `git -c core.fileMode=false diff` でパッチ生成
- [ ] Dockerコンテナのクリーンアップ処理(タイムアウトでSIGKILLされても走るようシグナルハンドラ検討)
- [ ] `SWEBenchTaskInput` Pydanticモデル
- [ ] 上限: 反復30 / 入力トークン300,000 / 出力トークン10,000 / タイムアウト900秒(subject 6.1.2で確認、旧記述は入出力を取り違えていたので修正)

### 7. 必須ツール(SWE-bench向け、MCPサーバーとして実装)
- [ ] File System: `read_file(filepath,start_line,end_line)`(`cat -n`風出力)/ `edit_file(filepath,old_str,new_str)` / `list_files(directory,pattern)`
- [ ] Code Search: `search_code(pattern,file_pattern)` / `search_function_or_class_definition_in_code(name)` / `find_references(name,filepath,line)` (いずれも `path:line content` 形式)
- [ ] Execution: `run_tests()` / `get_patch()` / `run_command(command,workdir)`

### 8. LLMプロバイダ層
- [ ] 複数プロバイダ・複数モデル対応(OpenRouter, Groq, Together AI, Google AI Studio等の無料枠)
- [ ] プロバイダごとに複数APIトークン対応 + トークンローテーション(レート制限対策)
- [ ] プロバイダ切り替えが容易な抽象化レイヤー
- [ ] 使用量トラッキング(トークン、リトライ、レイテンシ、リクエスト数)
- [ ] 課金プラン一切禁止、無料枠のみで完結すること

### 9. ベンチマークレポート(`BENCHMARK_REPORT.md`をリポジトリルートに)
- [ ] 最低5モデル × 最低2プロバイダ × 同一3タスク以上で比較
- [ ] Setup章(モデル/プロバイダ/タスク選定理由)
- [ ] 結果表(モデル×タスクごとにPass/Fail、反復数、入出力トークン、所要時間)
- [ ] プロバイダ信頼性(平均応答時間、リトライ回数、可用性)
- [ ] 中間指標を最低2種(最終パッチファイルへの初回アクセスステップ、テスト失敗数が初めて減るステップ、テスト通過からfinal_answerまでの反復数、等)
- [ ] アブレーション study(プロンプト/ツール/パラメータの変更前後比較を最低1件)
- [ ] 結論(最終パイプラインに採用したモデルとその理由、データに基づく)
- [ ] 裏付けとなる `solution.json` 群をリポジトリに残す

### 10. README.md
- [ ] 1行目イタリック: `_This project has been created as part of the 42 curriculum by <login>._`
- [ ] Description / Instructions / Resources(AI利用の説明含む)セクション
- [ ] システムアーキテクチャ、エージェントループ説明、サンドボックス設計、ツール実装詳細、ベンチマーク結果と分析
- [ ] 英語で書くこと

### 11. 評価対策
- [ ] `exam_mbpp.sh`(5タスク中4/5合格)/ `exam_swebench.sh`(3タスク中2/3合格)/ `exam_sandbox.sh`(セキュリティテスト全通過)をローカルで通しておく
- [ ] 評価中にライブで小改修を求められる想定をしておく(自分のコードを理解している証明、2-5分で対応できる粒度)
- [ ] エージェントが「正規の探索・推論」で解いたことをトレース(system_prompt/llm_output/sandbox_input/sandbox_output)から示せるようにする(PRやissueから答えを取ってくる等はカンニング扱いで即0点)

## 進め方のヒント(公式Development Approach章より)
- 先にどちらのベンチマークが簡単か見極める。制約(トークン/反復上限)を外した状態でまず1問解けるか確認してから制約を戻す。
- デバッグ時は最初の3〜5イテレーションで何が起きているか(ツール呼び出しの迷走、幻覚した観測結果など)を見る。自分で手動でタスクを解いてみて、それをプロンプトに反映する。
- SWE-benchは `sympy__sympy-14711`, `sympy__sympy-13480`, `pydata__xarray-4629` あたりから着手すると良い(公式推奨の易しめタスク)。
