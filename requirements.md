# requirements.md — Agent Smith 課題要件 詳細まとめ


## 目次
1. [Foreword / AI Instructions(Ch. I-II)](#1-foreword--ai-instructions)
2. [Overview — Code Agentとは(Ch. III)](#2-overview--code-agentとは)
3. [Common Instructions — 全体ルール(Ch. IV)](#3-common-instructions--全体ルール)
4. [Mandatory Part(Ch. V)](#4-mandatory-part)
   - [5.1 Agentic Framework](#41-agentic-framework)
   - [5.1.1 Common Output Models](#411-common-output-models)
   - [5.1.2 Development Approach](#412-development-approach)
   - [5.2 The Sandbox](#42-the-sandbox)
   - [5.3 MBPP Agent](#43-mbpp-agent)
   - [5.4 SWE-bench Agent](#44-swe-bench-agent)
   - [5.5 Mandatory Tools](#45-mandatory-tools)
   - [5.6 LLM API Providers](#46-llm-api-providers)
   - [5.7 Model Benchmark Report](#47-model-benchmark-report)
5. [Evaluation(Ch. VI)](#5-evaluation)
6. [Readme Requirements(Ch. VII)](#6-readme-requirements)
7. [Submission(Ch. VIII)](#7-submission)
8. [TASKS.mdで見つけた誤りの訂正記録](#8-tasksmdで見つけた誤りの訂正記録)

---

## 1. Foreword / AI Instructions

- ソフトウェアエンジニアリングは「正しいコードを書く」だけでなく、**システム理解・大規模コードベースのナビゲーション・デバッグ・効率的な反復**が本質、という前置き。
- Agent Smith = 「モデルがコードを書くだけ」ではなく、**タスクについて推論し、ツールと対話し、コードを安全に実行し、結果を観測し、戦略を適応させる**システム。静的プロンプト・JSON tool callingを超えた、実行可能Pythonコード駆動の**完全なエージェントループ**を作る。

### AI Instructions(重要・行動指針として)
- AIは強力な相棒だが、**技術的・非技術的判断の責任はAIに持たせない**。どの部分も自分で深掘りできる状態を保つこと。
- Main message: 成熟し責任あるAI利用を目指す。AIに意思決定させない(特にAIがゴール・制約・チームダイナミクスを把握していない場合)。ピアとの協働で創造性・独自性・人間の監督を維持する。
- Learner rules: プロジェクトの知的主導権を自分で持つ、チーム/ピアの集合知を優先する、AI技術の進化に能動的にキャッチアップする。
- **Good practice例**: 「AIにAPIのユニットテスト生成を手伝わせ、チームメイトとレビューしてエッジケースを調整した。時間が節約でき、両方が新しいことを学んだ」
- **Bad practice例**: 「AIにプロジェクトのアーキテクチャ全体を生成させた。動くが、ピアレビューや客先で設計判断を説明できと言われてもできない。信用を失い、失格する」

→ この課題は評価中に**設計判断を自分の言葉で説明・防御できること**を前提にしている(§5参照)。

---

## 2. Overview — Code Agentとは

- 古典的LLM出力(最終回答を1回で出す)ではなく、モデルに以下を許可する新パラダイム = **agentic code generation**:
  - 実行可能なPythonコードを書く
  - コードから直接ツールを呼ぶ
  - 実行結果を観測する
  - タスクが解けるまで反復する
- 構造化ループ: **Thought → Code → Observation**(下図、繰り返し。最終的に`FINAL ANSWER`で解を提出)。
- 対象ベンチマーク2つ: **MBPP**(アルゴリズム系Python問題)、**SWE-bench**(実プロダクションリポジトリのバグ修正)。
- 本課題の核心的難しさ: エージェントを賢くするだけでなく、**安全・制御可能・再現可能・測定可能**にすること。**複数のLLMをベンチマークして比較評価する**ことも要求される(成功率だけでなく反復効率まで)。

### III.1 What is a Code Agent?
Code Agent = 以下ができるAIシステム:
- プログラミングタスクについて推論する
- 実行可能コードを生成する
- 制御された環境でそのコードを実行する
- ファイル・テスト・リポジトリと対話するツールを使う
- 結果を観測し、アプローチを洗練する

この課題では**code-based tool calling**を実装する。例:
```python
result = search_code("validate_email")
print(result)
content = read_file("models.py", 1, 50)
print(content)
```
JSON tool callingより表現力が高い理由: ステップ間で変数が永続する、条件分岐・ループが書ける、複数ツール呼び出しを1ステップで合成できる。

### アーキテクチャ図(p.8)
```
LLM API ⇄ Orchestrator ── Response containing code block ──▶ Code extraction
                                                                    │
                                                          Extracted code
                                                                    ▼
                                              ┌───────────── Sandbox ───────────────────────────┐
                                              │  Python Interpreter ── Tool Call ──▶ MCP Client │
                                              └─────────────────────────────────────────────────┘
                                                                    │ STDIO / HTTP
                                                                    ▼
                                                              MCP Server
```
1. **Agent/Orchestrator**: 中心ループ。LLM呼び出し→コード抽出→サンドボックスに渡す→観測結果を読む→繰り返し。
2. **Code Extraction**: LLM応答とサンドボックスの間の変換ステップ。
3. **Sandbox**: 実行境界。LLM生成コードにセキュリティ制約を課す。内部にMCPクライアントを持つ。
4. **final_answer()**: サンドボックスが注入する組み込み関数。**MCPツールではない**。
5. **MCP Server(s)**: 別プロセス(stdio or HTTP)で動く。**自分のMCPサーバーのみ**許可。

---

## 3. Common Instructions — 全体ルール

### IV.1 General Rules
- **Python 3.10** 必須
- **uv** をパッケージマネージャとして必須使用
- クリーンなソフトウェアアーキテクチャに従うこと
- **全エラーを優雅にハンドルすること** — 評価中のクラッシュは即失敗
- コードは読みやすく・構造化され・文書化されていること
- **すべての実行はサンドボックス環境内で行うこと**

### IV.2 Technical Constraints
- **複数のLLMプロバイダ・複数モデルをサポートすること**
- **使用量トラッキングを実装すること**(トークン、リトライ、レイテンシ、リクエスト数)
- **設定可能なサンドボックスを実装すること**(import・ファイルシステムアクセス、§5.2で詳細規定)
- **ツールはエージェントループと独立して動作すること**
- **エージェント編成ロジックを再実装するライブラリの使用禁止**(例: llama-index, smolagents, langgraph, crewai, autogen)
- **エージェントループは自分自身の実装であること**
- マルチエージェント構成も許可されるが(なくても完走可能)、**オーケストレーションは自作コードであること**

---

## 4. Mandatory Part

### 4.1 Agentic Framework

自律的にコーディング課題を解ける**agentic framework**を構築する。LLMプロバイダ選定は§4.6参照。

システムが満たすべき要件:
1. **Thought → Code → Observation ループ**を実装する
2. モデル応答からLLM生成Pythonコードを抽出する
3. 生成コードをサンドボックス環境内で実行する
4. サンドボックスの実行結果をLLMにフィードバックする
5. エージェントループを使ってベンチマークタスクを自律的に解く
6. **system promptを設計する**、以下を含めて:
   - 利用可能ツールの明確なドキュメント
   - 構造化された応答スロットの例(`Thought`, `Code`, `Observation`)
   - 効果的なエージェント推論ループの例

#### コード抽出フォーマット(複数プロバイダをベンチマークする際に必要)
LLMによって学習時のツール呼び出し形式が異なるため、Pythonコードブロック以外にも対応が必要(非網羅的リスト):

| 形式 | 例 |
|---|---|
| (a) Pythonコードブロック(主形式) | ` ```python ... ``` ` に続けて `<end_code>` |
| (b) XML tool call(Anthropic系) | `<invoke name="..."><parameter>...</parameter></invoke>` |
| (c) JSON/Hermes tool call | `<tool_call>{"name": "...", "arguments": {...}}</tool_call>` |
| (d) ReAct形式 | `Action: tool_name` / `Action Input: {...}` |

**非Python形式は、サンドボックス実行前に等価なPython関数呼び出しに変換する**こと(例: `result = read_file(filepath="/testbed/file.py")`)。これによりサンドボックス自体はフォーマット非依存のまま、どのモデルでも対応できる。

#### サンドボックスが明示フィードバックを返すべき5状況(必須・赤枠強調)
1. モデル応答に有効なコードブロックが見つからなかった
2. コードブロックは壊れていたが解釈できた(**その方法を説明すること**)
3. 実行がタイムアウトに達し、部分的な出力のみ
4. ツール出力がサイズ制限で切り詰められた
5. 編集がシンタックスエラー/lint違反を起こした

> LLMに「何が起きたか」を推測させてはいけない。サイレントな失敗は幻覚した観測結果と反復の無駄につながる。

#### 4.1.1 Common Output Models

MBPP/SWE-bench両エージェントは**同じ**`StepMetrics`/`SolutionOutput`を出力する(`benchmark`と`solution`フィールドの意味だけが変わる: MBPPはPythonコード、SWE-benchはgit patch)。

```python
class StepMetrics(BaseModel):
    """1エージェントステップのメトリクス。
    各ステップ = 1回の LLM generate → sandbox execute サイクル。
    評価には全フィールドが必須。適用外の場合は空文字列でよい(例: sandbox実行が無いステップ)。
    """
    step: int                  # 1始まりの反復番号
    input_tokens: int          # このステップでLLMに送ったトークン数
    output_tokens: int         # このステップでLLMが生成したトークン数
    request_time_ms: float     # LLM API呼び出しのウォールクロック時間(ミリ秒)
    timestamp: str = <ISO8601, now>
    api_url: str = ""          # LLM APIエンドポイントのベースURL (例: 'https://openrouter.ai/api/v1')
    model_name: str = ""       # このステップで使ったモデル識別子
    llm_output: str = ""       # コード抽出前の、LLMが生成した生テキスト
    sandbox_input: str = ""    # サンドボックスに送ったPythonコード
    sandbox_output: str = ""   # サンドボックス実行結果(stdout/stderr/エラーメッセージ)
    retries: int = 0           # 成功するまでのLLM API再試行回数(0=初回成功)

class SolutionOutput(BaseModel):
    """学生の解答の出力 — 評価に必須のフォーマット。solution.json に書き出す構造。"""
    task_id: str                       # MBPPはtask_id文字列、SWE-benchはinstance_id
    benchmark: str                     # 'mbpp' or 'swebench'
    success: bool                      # エージェント自身が解けたと判断したか
    solution: str                      # MBPP: Python関数コード / SWE-bench: git patch(diff)
    iterations: int                    # 使用したエージェントループ反復数
    total_requests: int                # LLM APIリクエスト総数(リトライ含む)
    total_input_tokens: int            # 全ステップのinput_tokens合計
    total_output_tokens: int           # 全ステップのoutput_tokens合計
    total_time_seconds: float          # エージェント開始〜終了のウォールクロック時間
    steps: List[StepMetrics] = []      # ステップごとのメトリクス(1反復1エントリ)
    system_prompt: str = ""            # LLMに送った完全なsystem prompt(証跡確認用)
    error: Optional[str] = None        # 失敗時のエラーメッセージ(成功時はNone)
    timestamp: str = <ISO8601, now>
```

> `SolutionOutput`は`system_prompt`フィールドを**必ず**含み、`steps`内の各要素は`api_url`・`model_name`(そのステップで使ったLLMエンドポイント特定用)、`llm_output`・`sandbox_input`・`sandbox_output`(生LLM応答・サンドボックスに送ったコード・実行結果)、`retries`を**必ず**含むこと。このメタデータは、エージェントが正規の探索・推論を通してタスクを解いたことを評価時に検証するために精査される(カンニング防止、§5.4.1参照)。

> **現状の実装ギャップ**: `orchestrator.py`/`llm_client.py`(section 1実装時点)はまだ`StepMetrics`を一切生成していない。`LLMClient.generate()`もテキストのみ返し、トークン数・時間・api_url・model_nameを返していない。section 4着手時に`LLMClient`のインターフェースを拡張する必要がある。

#### 4.1.2 Development Approach(進め方のガイド質問)
- **Starting out**: どちらのベンチマークがシンプルで先に着手すべきか? 最も能力の高いモデルでテストしているか、それとも早すぎる段階で制約により自分の首を絞めていないか? トークン・反復制約を一時的に外した状態で1問解けるか? 解けないなら制約を戻しても悪化するだけ。
- **Debugging your agent**: 最初の3〜5イテレーションで何が起きているか(ツール呼び出しの迷走、幻覚した観測結果、予期しない経路)? 自分でタスクを手動で解いてみて、その手法をプロンプトに反映したか? SWE-benchでは評価スクリプトを小さなゴールに分解し、フルスクリプトの成功を待つより個々のテスト通過を追跡する。
- **Scaling up**: 1タスクで動く解法は一般化するか、それとも過学習か? 早すぎる最適化(トークン・モデル選択)をしていないか、それともまずアプローチが機能することを証明したか? モデルによってツール呼び出しスタイルの好みが違う — モデルの自然な傾向と戦っていないか、それに合わせているか?

---

### 4.2 The Sandbox

安全で設定可能な実行サンドボックスの設計・実装がこの章の焦点。

#### 1. サンドボックスCLI
```bash
# インタラクティブサンドボックス起動
uv run sandbox

# カスタム設定で
uv run sandbox sandbox_template.json

# MBPPツール付き(stdio)
uv run sandbox --mcp-stdio "python mcp_tools_mbpp.py" sandbox_template.json

# MBPPツール付き(HTTP)
uv run sandbox --mcp-server <URL>

# SWE-benchツール付き
uv run sandbox --mcp-stdio "python mcp_tools_swebench.py" sandbox_template.json
```

**インタラクティブサンドボックス**(タスク引数なしの`uv run sandbox`)はREPLスタイルのCLIモード:
- プロンプトを開き、ユーザー入力コードをループで読み、§5.2のimport/ファイルシステム/タイムアウト/メモリ制限下でサンドボックス名前空間内に実行する
- 接続されたMCPツールラッパーと`final_answer`が利用可能
- 各エントリ後に結果または例外を表示し、プロンプトに戻る
- `exit`コマンドまたはEOF(**Ctrl+D**)でクリーンに終了する

#### 2. `final_answer`ツール
- **`final_answer` IS**: サンドボックスが実行名前空間に注入するcallable関数。エージェントのコードが`final_answer(answer_string)`を呼ぶと、サンドボックスは引数を捕捉し、エージェントループにタスク完了を通知する。エージェントループはそこで終了し`SolutionOutput`を生成する。
- **`final_answer` is NOT**: MCPツールではない。どのMCPサーバーからも提供されない。**接続中のMCPサーバーに関わらず、常にサンドボックス名前空間に存在する**。MCPツールはサンドボックスの外側で動作する(例: Docker内のファイル読み取り、テスト実行)一方、`final_answer`はサンドボックスの内側でエージェントループを制御する。
- 使い方:
  - **MBPP**: `final_answer(your_solution_code)` — Pythonコードを引数として渡す
  - **SWE-bench**: `final_answer(get_patch())` — `get_patch()`で取得したgit patchを渡す
- **アーキテクチャ境界**: サンドボックスは実行名前空間に2種類のcallableを提供する:
  1. **MCPツールラッパー**: 接続中のMCPサーバーから動的に発見される
  2. **`final_answer`**: 常に存在、サンドボックス自身が提供
  - 別のMCPサーバーに接続すると、MCPツールラッパーは変わるが`final_answer`は変わらない
- **例外の伝播**: サンドボックスはプログラムフローを制御する例外を正しく伝播しなければならない。特に**`KeyboardInterrupt`と`SystemExit`はサイレントに握りつぶさず**、正しいシャットダウンのためにエージェントループまで届かせること。

#### 3. サンドボックスのセキュリティ制約
LLM生成コードの実行は本質的にリスクがある。サンドボックスは単なる技術コンポーネントではなく、**自律システムと現実世界の間の安全境界**。

- **Import制限**: 設定済みのallowlistにあるモジュールのみimport可能
- **ファイルシステム制限**: サンドボックス化されたコードによるファイルアクセスは、allowlistされたディレクトリ(`SandboxConfig`の`allowed_directories`フィールド)に限定される。**これらはサンドボックスプロセス自身が見るパスで評価される。ホストのみのパスではない**。コードが到達する必要のあるどのディレクトリも、allowlistに含まれている必要がある。タスク作業領域(例: `/testbed`)を書き込み可能なscratch/ランタイム領域(例: `/tmp/agent`)から分離できるよう、複数エントリが許可されている。allowlist外は拒否される
- **ネットワークアクセス禁止**: あらゆる送受信のネットワーク接続を防止
- **実行タイムアウト**: 設定済みタイムアウトを超えるコードを終了させる(サンドボックス化されたコードにのみ適用)
- **メモリ制限**: 許可RAM使用量を超えるコードを終了させる
- **制限された組み込み関数**: 特権昇格を防ぐため、危険なbuiltinsを削除/上書きする

> **実装はPython標準ライブラリとbuiltinsのみを使うこと。RestrictedPythonのような外部パッケージは禁止。**

```python
class SandboxConfig(BaseModel):
    """学生解答用のサンドボックス設定。
    Allowlist方式: authorized_importsにあるimportのみ許可。それ以外はデフォルトでブロック。
    """
    authorized_imports: List[str] = [
        "math", "math.*", "collections", "collections.*", "itertools", "re", "json",
        "typing", "typing.*", "functools", "operator", "heapq", "bisect", "copy",
        "string", "random", "datetime", "datetime.*", "array", "cmath",
    ]
    allowed_directories: List[str] = ["/testbed", "/tmp/agent"]
    max_execution_time_seconds: int = 30
    max_memory_mb: int = 512
```
- `.*`で終わるエントリはそのモジュールのサブモジュールのimportも許可する(例: `collections.*`は`collections.abc`を許可)
- 別のMCPサーバーに接続すると、サンドボックスはそのサーバーのツールを動的に発見・公開すること。**必須ツールは自分のMCPサーバーが接続されている時のみ存在する**

> サンドボックスは中心的な実行レイヤー。MCPサーバーに接続し、そのツールをサンドボックス名前空間内でcallable Python関数として公開する。**サンドボックスがMCPクライアントをラップするのであって、その逆ではない**。サンドボックスとMCPツールは独立したセキュリティ領域: サンドボックスはLLM生成Pythonコードが何をできるか(import、パス、タイムアウト、メモリ)を制限する一方、MCPツールのアクションはサンドボックスの外側で発生し、サンドボックスのタイムアウトの対象外(例: 外部プロセスを起動するツール)。

#### 4. MCPサーバー統合
- 必須ツールをエージェントに統合すること
- MCPツール・リソース・プロンプトを公開すること
- MCPツールはサンドボックスからPython関数として呼び出し可能であること
- システムは**未知のMCPサーバー**でテストされる(=自分のツール実装をハードコードせず、動的発見に対応すること)
- MCPツールファイル(`mcp_tools_mbpp.py`, `mcp_tools_swebench.py`)は**リポジトリのルート**に配置すること
- MCPサーバー接続は**stdioとstreamable HTTPの両方**をサポートすること

#### 5. サンドボックスマニュアルの生成
- LLMプロンプトに供給する**サンドボックスマニュアル**を生成すること。MCPツールのドキュメント(またはそれへのアクセス方法)を含めること
- マニュアルは接続中のMCPサーバーのツールスキーマ(ツール名・説明・パラメータ型)から**動的に**生成されること。別のMCPサーバーに接続したら、マニュアルは自動的にそのサーバーのツールを反映すること。マニュアルはLLMが「どんなツールが使えて、どう呼ぶか」を理解するために読むもの

サンドボックスの上限・挙動は**PydanticモデルとJSON設定ファイル**で設定可能にすること。

**サンドボックス分離アプローチ**: 信頼できないコードを自プロセス内で実行するか、別プロセスで実行するか? それぞれセキュリティ境界・タイムアウトハンドリング(暴走コードをどう殺すか)・通信(結果をどう取得するか)にトレードオフがある。複数の正解が存在するので、自分のアーキテクチャに合うものを選ぶこと。

---

### 4.3 MBPP Agent

MBPP(Mostly Basic Python Problems)タスクを解く自律エージェントを実装する。

#### 1. エージェントCLIインターフェース
```bash
# 1. タスクをdump
cd moulinette
uv run moulinette_eval dump mbpp --output ../cache/mbpp_task.json

# 2. 自分のエージェントを実行
cd ../student
uv run python -m agent_mbpp --task-file ../cache/mbpp_task.json \
  --output ../cache/mbpp_solution.json \
  --model-name "model/name" --provider-url "https://provider.api/v1"

# 3. 解答を検証
cd ../moulinette
uv run moulinette_eval validate mbpp ../cache/mbpp_task.json \
  ../cache/mbpp_solution.json
```
- Task loading / Agent execution

#### 2. MBPP MCPツール
- **`run_tests(code, test_list)`**: 候補解を与えられたテストアサーションに対して実行する。`success`(bool: 全アサーションが通ったか)と`output`フィールドを含むJSON文字列を返す
- 追加で有用と考えるツールを実装してよい

#### 3. Pydanticモデル
```python
class MBPPTaskInput(BaseModel):
    """MBPPタスク評価用の入力。"""
    task_id: int
    task_definition: str
    function_definition: str
    test_imports: List[str] = []
    test_list: List[str] = []
```
- エージェント出力: §4.1.1と同じ`StepMetrics`/`SolutionOutput`(`benchmark="mbpp"`, `solution`=自分の関数コード)

#### 4. 制限内での動作設計
§5.1(評価章の6.1)で定義された上限(反復・トークン・時間)内で動作するよう設計すること。評価はエージェント出力がこれらの上限内に収まっているか検証する。`max_iterations`はエージェントループの設定可能パラメータであること。

外部LLMプロバイダは、インターフェースがプロジェクト制約に準拠している限り許可される。

---

### 4.4 SWE-bench Agent

Dockerized環境内でSWE-benchタスクを解く自律エージェントを実装する。

- SWE-benchでは両方のアプローチが有効:
  - (a) サンドボックスをDockerコンテナ内にデプロイする、または
  - (b) サンドボックスをホスト上で動かし、MCPツールがDockerへブリッジする
- どちらを選んでもサンドボックスのセキュリティ制約は遵守すること
- 実リポジトリの実バグ修正・機能実装を行う
- Dockerコンテナ内でコードベースを探索し、**プログラム実行後は自分でクリーンアップする責任がある**
- `git -c core.fileMode=false diff` を使って有効なパッチを生成・提出する

> **重要**: SWE-benchツールを単体でテストする際、moulinetteはMCPサーバー起動前に環境変数`TESTBED_PATH`をリポジトリルートに設定する。**ツールはこの正確な変数名を読んでリポジトリを特定すること**。

Dockerコンテナ内に追加の依存関係(例: ruff, jedi, tree)をインストールしてツールを強化してもよい。

- 初期テストは最もシンプルなタスクから: `sympy__sympy-14711`, `sympy__sympy-13480`, `pydata__xarray-4629`。未使用ツールをセットアップから外すとどうなるか試してみる、というヒントあり

1. エージェントCLIインターフェース(MBPPと同型、`dump swebench` / `agent_swebench` / `validate swebench`)
2. SWE-bench MCPツール(§4.5「Mandatory Tools」の仕様に従う。追加ツールも可)
3. Pydanticモデル:
```python
class SWEBenchTaskInput(BaseModel):
    """SWE-benchタスクの入力。moulinetteが提供する。エージェントはこれを受け取り、
    issueを修正するgit patchを生成しなければならない。"""
    instance_id: str        # 例: 'sympy__sympy-23534'
    problem_statement: str  # GitHub issueの説明(何を直すべきか)
    docker_image: str       # pullするDockerイメージ名(例: 'swebench/sweb.eval.x86_64.sympy_1776_sympy-23534:latest')
    eval_script: str        # パッチ評価のためコンテナ内で実行するbashスクリプト
    hints_text: str = ""    # issueに関するオプションのヒント(空の場合あり)
    repo: str = ""          # リポジトリ名(例: 'sympy/sympy')
```
4. エージェント出力(patch): §4.1.1と同じ`StepMetrics`/`SolutionOutput`(`benchmark="swebench"`, `solution`=自分のgit patch)
5. §5(評価章)で定義される**hard limits**を遵守する

`SolutionOutput`は`system_prompt`フィールド(LLMに送った完全なsystem prompt)を含むこと。`steps`内の各ステップは`llm_output`・`sandbox_input`・`sandbox_output`(生LLM応答、サンドボックスに送ったPythonコード、実行結果)を含むこと。`retries`フィールドはそのステップに何回LLM API再試行が必要だったかを追跡する。このメタデータは、エージェントが正規のコード探索・推論を通じてタスクを解いたこと(PR/issue/外部ソースからの答えの取得ではないこと)を検証するために評価時に精査される。

---

### 4.5 Mandatory Tools

以下のツールを**すべて**実装すること。各ツールはMCP(Model Context Protocol)サーバーで公開され、**SWE-benchベンチマークの文脈で独立してテストされる**。

#### 4.5.1 File System Tools
- **`read_file(filepath, start_line, end_line)`**: 行番号付きでファイル内容を読む。出力フォーマットは`cat -n`風であること:
  ```
  <line_number>: <line_content>
  <line_number>: <line_content>
  ...
  ```
- **`edit_file(filepath, old_str, new_str)`**: ファイル内の厳密な文字列を新しい文字列に置換する
- **`list_files(directory, pattern)`**: 与えられたパターンにマッチするディレクトリ内のファイルを列挙する

#### 4.5.2 Code Search Tools
- **`search_code(pattern, file_pattern)`**: コードベースに対するgrep風検索を行う。出力フォーマット:
  ```
  /absolute/path_to_file.py:<line_number> <line_content>
  /absolute/path_to_other_file.py:<line_number> <line_content>
  ...
  ```
- **`search_function_or_class_definition_in_code(name)`**: 関数またはクラスの定義を検索する。出力フォーマットは`search_code`と同様
- **`find_references(name, filepath, line)`**: シンボル(関数・クラス)の全使用箇所を検索する。出力フォーマットは`search_code`と同様

#### 4.5.3 Execution Tools
- **`run_tests()`**: 評価スクリプトを実行する
- **`get_patch()`**: リポジトリに対する全変更の統合`git diff`を取得する(実装次第)
- **`run_command(command, workdir)`**: 指定した作業ディレクトリでシェルコマンドを実行する。コマンドのstdout・stderr・exit codeを返す

---

### 4.6 LLM API Providers

エージェント実装にはLLM APIへの呼び出しが必要。多くのプロバイダが開発・実験に十分な無料枠/使用量枠を提供している。

- 下記リストは**例示目的のみ**。公開情報に基づき、時間とともに変化しうる。**プロジェクト要件(無料枠、プロバイダごとの複数APIトークン)を満たす限り他のプロバイダも自由に使える**
- 評価対象データセットは**SWE-bench Verified**。これを知った上で以下を調査できる: モデルリーダーボード(どのLLMが実世界コーディングタスクで最良か)、エージェントシステムの説明(トップシステムがループ・ツール・プロンプトをどう設計しているか)、タスクごとのトレース・評価(特定モデルが特定タスクでどう振る舞うか、最初のデバッグサイクルのモデル選定に有用)
- **Tip**: モデルがツール呼び出し後も生成を続けてしまい実際の実行出力を待たない場合、`stop_sequences`(または`stop`)APIパラメータ(例: `<end_code>`, `</tool_call>`)でコードブロックを終える token で生成を止めること。さもないと本物の観測結果の代わりに架空のツール出力を幻覚する可能性がある

#### 4.6.1 Examples of Free Providers
例: OpenRouter, Together AI, Groq, Google AI Studio (Gemini), Mistral AI, Cohere, Fireworks AI, Perplexity AI, Anyscale(非網羅的)。

**実装は複数プロバイダごとに複数APIキーをサポートすること**。プロバイダのフォールバックの実装も検討すること。レート制限・クォータは評価時の負荷下では特に変動しうる。

**重要**:
- このリストは**非網羅的かつ非契約的**
- アクセス条件・クォータ・利用可能モデルは時間とともに変化しうる
- 学習者は追加のプロバイダを探索したり、必要ならセルフホストモデルを使うことが推奨される
- **ソリューション全体は無料枠のみに依存すること。** 有料プラン・購入クレジット・課金有効化アカウントは一切禁止。プロジェクトは評価時に無料クォータのみで完全実行可能でなければならない
- **マルチトークン管理は必須。** プロバイダごとに複数APIトークンをサポートすること。レート制限・クォータ枯渇に対処するためトークンローテーションを実装すること
- 実装は、大規模なリファクタなしにプロバイダを切り替えられる程度に**十分抽象化**されていること

> プロバイダの選択自体は採点対象ではない。**抽象化の質、エラーハンドリング、全体アーキテクチャの質**が採点対象。

---

### 4.7 Model Benchmark Report

生の正答率だけでは「このエージェントに最適なモデルはどれか」の全体像は分からない — 探索効率・トークンコスト・プロバイダ信頼性・反復規律も重要。

**リポジトリルートに`BENCHMARK_REPORT.md`を作成し、少なくとも5モデル×少なくとも2プロバイダを、同一の少なくとも3つのSWE-benchタスクで比較すること。**

> Tip: 無料枠プロバイダは日次トークン/リクエストクォータを課すため、5モデルを走らせると終わる前に枯渇しうる。プロバイダが多いほど独立したクォータが増えるので、最低限の2プロバイダより多めに準備しておくことが推奨される。

レポートに含めるべき内容:
1. **Setup**: 比較したモデル/プロバイダ、使用したタスク、それらのタスクを選んだ理由
2. **Results table**: モデル×タスクの組み合わせごとに: Pass/Fail、使用反復数、入力トークン総数、出力トークン総数、ウォールクロック時間
3. **Provider reliability**: モデル/プロバイダごとに: リクエストあたりの平均応答時間、必要だった再試行回数(レート制限・タイムアウト・エラー)、ベンチマーク実行中の全体的な可用性
4. **Intermediary metrics**(以下の少なくとも2つ):
   - エージェントが最終パッチに現れるファイルを最初に読む/編集したステップ(探索効率)
   - テスト失敗数が最初に減少したステップ(部分的進捗)
   - 「テスト初通過」から`final_answer`までの反復数(提出規律 — 0が理想)
   - これらの指標は`solution.json`ファイルを目視で調べて手動測定してよい。**自動化は不要**。重要なのはツールではなく分析
5. **Ablation study**: 同一タスク・同一モデルで、エージェントへの変更(プロンプト・ツール・パラメータ)の少なくとも1つのbefore/after比較
6. **Conclusions**: 最終パイプラインにどのモデルを選んだか、なぜか。どのモデルを除外できるか、なぜか。データに基づくこと

**裏付けとなる`solution.json`ファイル群はリポジトリに残しておくこと。**

---

## 5. Evaluation

評価中、APIキーと設定は評価スクリプトへの必須引数として渡される`.env`ファイル経由で提供される:
```bash
./exam_TYPE.sh --student-path ./student --moulinette-path ./moulinette --env-file /path/to/.env
```
**CLIは環境変数からのAPIキー読み込みをサポートすること**。標準的な環境変数名(例: `OPENROUTER_API_KEY`)を使うこと。→ 現在の`config.py`の命名(`OPENROUTER_API_KEY`等)はこれに準拠済み。

### 評価スクリプト一覧

| | `exam_mbpp.sh` | `exam_swebench.sh` | `exam_sandbox.sh` |
|---|---|---|---|
| タスク | 5 random tasks | 3 random tasks | セキュリティテスト |
| Pass基準 | 4/5 | 2/3 | **ALL**合格 |
| フロー | 1. dump task<br>2. run agent<br>3. validate | 1. dump task<br>2. run agent<br>3. validate<br>4. container cleanup | テスト内容:<br>- import block<br>- builtin block<br>- network block<br>- path restrict<br>- timeout<br>- memory limit<br>- MCP protocol |

DUMP→RUN→VALIDATEの流れ: `moulinette dump TYPE → task.json` → `student_agent --task-file task.json --output solution.json`(制限内で解く) → `moulinette validate TYPE task.json solution.json`(テスト照合+メトリクス上限照合) → PASS or FAIL。

### 5.1 Hard Requirements and Limits(VI.1)

実装は以下の実行制限を厳守すること。いずれかの上限超過は該当タスクの失敗となる。

| Metric | MBPP | SWE-bench |
|---|---|---|
| Maximum iterations | 10 | 30 |
| Maximum input tokens | **6,000** | **300,000** |
| Maximum output tokens | **1,500** | **10,000** |
| Timeout | 120 seconds | 900 seconds |

> ⚠️ TASKS.mdの初版はこの表の入力/出力を取り違えていた(§8参照、訂正済み)。

- トークン上限は**単一タスクの全反復にわたる累積**(全Thought→Code→Observationサイクルの合計)
- 推論モデル使用時のthinking/reasoningトークンも、他のトークン同様に上限にカウントされる
- 選択したモデルのreasoningトークンが上限をタイトにする場合、non-reasoningモデルの使用を検討すること
- タイムアウトは「事後チェック」ではなく、**時間内に戻らなければエージェントのプロセス(および子プロセス)をSIGTERM→SIGKILLで強制終了**することで強制される。SWE-benchでは、この場合でもDockerコンテナのクリーンアップが走るようにすること(例: シグナルハンドラ)。あるいは十分速く完了してほぼ到達しないようにする

### 5.2 Pass Criteria(VI.2)

| Benchmark | Tasks | Pass Threshold |
|---|---|---|
| MBPP | 5 random tasks | 4 out of 5 |
| SWE-bench | 3 random tasks | 2 out of 3 |

**試験中の再試行は許可されない**: 失敗したタスクは再実行されない(1発勝負)。これは自分のエージェント内部のLLM呼び出しリトライロジック(`StepMetrics.retries`で追跡)を制限するものではない。

### 5.3 Grading Criteria(VI.3)

課題に合格するには**以下すべて**を満たすこと:
- MBPP・SWE-bench両方のpass基準を満たす
- 全ての反復・トークン・タイムアウト上限を遵守する
- 全ての必須ツールが独立したテストに合格する
- サンドボックスが分離・セキュリティテストに合格する

> ⚠️ **コード内のハードコードされたAPIキーはセキュリティ失格**。全APIキーは環境変数または設定ファイル(`.env`)から読み込むこと。ソースコード中に見つかったAPIキーは評価時にフラグが立てられる。

### 5.4 What We Will Test in the Review(VI.4)
- 必須ツールの正しさ
- エージェント推論ループの正しい実装
- サンドボックスのセキュリティと分離保証
- モデルベンチマーク結果とトークン統計
- コード品質・堅牢性・全体アーキテクチャ

> 評価中、エージェントに**小さなライブ改修**を求められ、MBPPタスクで再実行させられる。これは`solution.json`のデータが実際の実行由来であり、捏造値でないことを確認するテスト。各修正は2〜5分で終わる粒度が想定される。**どこを直せばいいか分からない場合、自分のコードベースを理解していないことを意味する**。演習後は全変更を元に戻すよう求められる(例: `git checkout`)。

#### 5.4.1 AI Safety in Evaluation(VI.4.1)
自律コードエージェントの構築は、サンドボックスセキュリティを超えた重要な安全上の考慮事項を提起する。評価中、エージェントが**正規の推論とコード探索**を通じてタスクを解いたこと(近道を悪用したのではないこと)を検証する。

エージェントが**してはいけないこと**:
- Pull request・issue・外部ソースから解答を取得すること
- 訓練データからの記憶済みパッチを、正規の探索なしに使用すること
- サンドボックスのセキュリティ制約を迂回すること
- 提供されたタスクコンテキスト外のリソースにアクセスすること

`SolutionOutput`は`system_prompt`・`llm_output`・`sandbox_input`・`sandbox_output`フィールドを、まさに評価者がエージェントの推論プロセスをトレースできるように含む。この透明性は責任あるAI開発の中核部分。

**違反はグレード0点となる。**

### 5.5 Evaluation logging structure(VI.5)
評価結果は以下に保存される:
```
./evaluations/EVAL_TYPE/YYYY-MM-DD_HH-MM-SS/task_id/task.json, solution.json, stdout.log, stderr.log
```

---

## 6. Readme Requirements

`README.md`はリポジトリのルートに必須。プロジェクトに不慣れな人(ピア、スタッフ、採用担当者等)がプロジェクトの概要・実行方法・詳細情報の在処を素早く理解できることが目的。

**最低限含めるべき内容:**
- **1行目は必ずイタリックで、正確に次の文言であること**:
  > *This project has been created as part of the 42 curriculum by \<login1\>[, \<login2\>[, \<login3\>[...]]].*
- **"Description"セクション**: プロジェクトの目標と概要を明確に提示
- **"Instructions"セクション**: コンパイル・インストール・実行に関する情報
- **"Resources"セクション**: トピックに関する古典的な参考文献(ドキュメント・記事・チュートリアル等)、および**AIがどう使われたか**(どのタスク・どの部分に使ったか特定して)の説明
- ➡ **プロジェクトによっては追加セクションが必要な場合がある**(例: 使用例・機能一覧・技術選定等)。必須の追加事項があれば明示的に列挙される(このsubjectでは以下が明示列挙)

**README.mdに含めるべき必須内容(明示列挙)**:
- System architecture
- Agent loop explanation
- Sandbox design
- Tool implementation details
- Benchmark results and analysis

> **READMEは英語で書くこと。**

---

## 7. Submission

Gitリポジトリでプロジェクトを提出する。リポジトリには以下を含めること:
- 提出物の内部アーキテクチャ・ディレクトリ構造は自由
- サンドボックス・モデルの設定ファイル
- `README.md`

> ⚠️ **Dockerイメージ、大きなモデル重み、生成物(生成出力)を含めないこと。**

→ 今後 `cache/`・`evaluations/` のような生成物ディレクトリが出てきたら、`.gitignore`に追加が必要。

---

## 8. TASKS.mdで見つけた誤りの訂正記録

TASKS.mdの初版(section 5, 6)は、MBPP/SWE-benchの入力・出力トークン上限を取り違えて記載していた:

| | 旧記述(誤り) | 正しい値(subject §6.1.1/6.1.2) |
|---|---|---|
| MBPP | 反復10 / **出力**トークン6,000 / 120秒 | 反復10 / **入力**トークン6,000 / **出力**トークン1,500 / 120秒 |
| SWE-bench | 反復30 / **出力**トークン300,000 / 900秒 | 反復30 / **入力**トークン300,000 / **出力**トークン10,000 / 900秒 |

TASKS.mdは既に修正済み(該当行にsubject参照付きのコメントを追加)。出力トークンの実際の上限はかなり小さい(MBPP 1,500、SWE-bench 10,000)ため、`max_output_tokens`のようなAPIパラメータ設定やレスポンス長の見積もりを誤ると、上限超過でタスク失敗になりうる点に注意。
