# `sample/` 完全解説

このドキュメントは `sample/` ディレクトリ（Agent Smith 課題のリファレンス実装）に含まれる
全ファイルを読んだ上でまとめた解説です。目的は「動かし方」ではなく「なぜこう設計されているか」
を理解すること。チーム自身の実装（リポジトリルートの `sandbox/`, `models.py` など）と比較しながら
読むための資料として書いています。

対象読者: このリポジトリで作業しているチームメンバー。Python の基礎、Docker、非同期I/Oの概念は
既知として説明します。

---

## 目次

1. [ひとことで言うと何のプロジェクトか](#1-ひとことで言うと何のプロジェクトか)
2. [ディレクトリ構成](#2-ディレクトリ構成)
3. [全体アーキテクチャとデータフロー](#3-全体アーキテクチャとデータフロー)
4. [`models.py` — モデル契約](#4-modelspy--モデル契約)
5. [`orchestrator.py` — エージェントループの心臓部](#5-orchestratorpy--エージェントループの心臓部)
6. [`code_extraction.py` — フォーマット非依存化レイヤー](#6-code_extractionpy--フォーマット非依存化レイヤー)
7. [`prompts.py` — システムプロンプト構築](#7-promptspy--システムプロンプト構築)
8. [`sandbox/` — 実行境界（最重要パート）](#8-sandbox--実行境界最重要パート)
   - 8.1 [`sandbox/executor.py`](#81-sandboxexecutorpy)
   - 8.2 [`sandbox/isolated_process.py` と `sandbox/isolated_worker.py`](#82-sandboxisolated_processpy-と-sandboxisolated_workerpy)
   - 8.3 [`sandbox/mcp_client.py`](#83-sandboxmcp_clientpy)
   - 8.4 [`sandbox/cli.py`](#84-sandboxclipy)
9. [`llm/` — LLM プロバイダ抽象化層](#9-llm--llm-プロバイダ抽象化層)
   - 9.1 [`llm/provider.py`](#91-llmproviderpy)
   - 9.2 [`llm/client.py`](#92-llmclientpy)
   - 9.3 [`llm/providers/openai_compatible.py`](#93-llmprovidersopenai_compatiblepy)
   - 9.4 [`llm/providers/gemini.py`](#94-llmprovidersgeminipy)
10. [`config.py` — 環境変数とプロバイダ設定](#10-configpy--環境変数とプロバイダ設定)
11. [`agent_mbpp.py` / `agent_swebench.py` — エージェント CLI](#11-agent_mbpppy--agent_swebenchpy--エージェント-cli)
12. [`mcp_tools_mbpp.py` / `mcp_tools_swebench.py` — MCP ツールサーバー](#12-mcp_tools_mbpppy--mcp_tools_swebenchpy--mcp-ツールサーバー)
13. [`docker_runner.py` — Docker ブリッジ](#13-docker_runnerpy--docker-ブリッジ)
14. [設定ファイル・補助ファイル](#14-設定ファイル補助ファイル)
15. [テストスイート（`tests/`）](#15-テストスイートtests)
16. [セキュリティ設計まとめ](#16-セキュリティ設計まとめ)
17. [`BENCHMARK_REPORT.md` からの知見](#17-benchmark_reportmd-からの知見)
18. [実行方法チートシート](#18-実行方法チートシート)

---

## 1. ひとことで言うと何のプロジェクトか

自律的にプログラミング課題を解く **Code Agent** の実装。以下の2ベンチマークに対応する。

- **MBPP** — 短いアルゴリズム的な Python 問題。`run_tests` で公開テストに合格するか確認する。
- **SWE-bench** — 実在の GitHub リポジトリのバグを、実際に動く Docker コンテナの中で調査・修正し、
  `git diff` として提出する。

エージェントは **Thought → Code → Observation** ループ（[CodeAct](https://arxiv.org/abs/2402.01030)
方式）を繰り返す。LLM が自然言語で考え（Thought）、Python コードを1ブロック書き（Code）、
そのコードがセキュリティ制限付きサンドボックス内で実行され、結果（Observation）が次のターンの
入力に追加される。これを LLM が `final_answer(...)` を呼ぶまで、あるいはイテレーション数・
トークン予算・時間予算のいずれかが尽きるまで続ける。

README.md 末尾の「How AI was used」にある通り、`sample/` 全体は Claude が課題文書
(`en.subject.pdf`) とチームの初期実装を踏まえて生成したリファレンス実装であり、
**提出物ではなく学習・比較対象**として置かれている。

---

## 2. ディレクトリ構成

```
sample/
├── agent_mbpp.py            # MBPP エージェント CLI
├── agent_swebench.py        # SWE-bench エージェント CLI
├── orchestrator.py          # Thought→Code→Observation ループ本体（両ベンチマーク共通）
├── code_extraction.py       # LLM出力 → Python コードへの正規化
├── prompts.py                # システムプロンプト組み立て
├── models.py                 # moulinette との契約となる Pydantic モデル（コピー、編集禁止）
├── config.py                  # .env 読み込み・プロバイダレジストリ
├── docker_runner.py           # SWE-bench 用 Docker コンテナのライフサイクル管理
├── mcp_tools_mbpp.py          # MBPP 用 MCP ツールサーバー（run_tests のみ）
├── mcp_tools_swebench.py      # SWE-bench 用 MCP ツールサーバー（必須9ツール）
├── sandbox/
│   ├── executor.py            # サンドボックス本体（AST 静的解析・制限付き builtins）
│   ├── isolated_process.py    # 親プロセス側: unshare/bwrap でワーカーを起動・通信
│   ├── isolated_worker.py     # 子プロセス側: 実際に exec() するワーカーのエントリポイント
│   ├── mcp_client.py           # MCPToolProxy: 任意の MCP サーバーを動的に発見してPython関数化
│   └── cli.py                  # `uv run sandbox` の対話 REPL
├── llm/
│   ├── provider.py             # ChatProvider プロトコル / GenerationResult / UsageStats
│   ├── client.py                # LLMClient: キー・プロバイダのローテーションとフォールバック
│   └── providers/
│       ├── openai_compatible.py # OpenRouter/Groq/Together/Fireworks 共通実装
│       └── gemini.py             # Google AI Studio 専用実装
├── tests/                        # pytest スイート（ネットワーク・APIキー不要で完結）
├── solutions/                     # 実測ベンチマークの solution.json（証跡として保存）
├── cache/                          # タスク定義や生成物の一時置き場
├── sandbox_template.json          # SandboxConfig の設定例
├── .env.example                    # 必要な環境変数のテンプレート
├── pyproject.toml / uv.lock        # 依存関係（uv で管理）
├── Makefile                         # install/test/lint/sandbox などのショートカット
├── README.md                        # セットアップと使い方
└── BENCHMARK_REPORT.md              # 5モデル×3プロバイダ×3タスクの実測比較
```

---

## 3. 全体アーキテクチャとデータフロー

```
LLM API  <--Prompt/Response-->  Orchestrator (orchestrator.py)
                                      |
                               code_extraction.py   (```python フェンス / XML
                                      |               <invoke> / JSON
                                      v               <tool_call> / ReAct
                              +---------------+       を1つのPython呼び出しに正規化)
                              |    Sandbox    |
                              | (sandbox/     |
                              |  executor.py) |
                              +-------+-------+
                                      | ツール呼び出し(ただのPython関数呼び出しに見える)
                                      v
                              MCPToolProxy (sandbox/mcp_client.py)
                                      | stdio または streamable HTTP
                                      v
                       mcp_tools_mbpp.py / mcp_tools_swebench.py
                          （別プロセスとして起動される）
```

**キーポイントは「Orchestrator は MBPP でも SWE-bench でも一字一句同じ」ということ。**
違いは3つだけ:

1. システムプロンプト（`prompts.py` の `benchmark` 引数で切り替え）
2. サンドボックス設定（`allowed_directories` や実行時間上限）
3. 接続する MCP サーバー（`mcp_tools_mbpp.py` か `mcp_tools_swebench.py`、後者は
   Docker コンテナの中で動く）

この分離のおかげで、「エージェントのロジック」と「タスク固有の道具立て」が完全に疎結合になっている。

---

## 4. `models.py` — モデル契約

> ファイル冒頭のコメント: 「moulinette/models_public.py からのコピー。形は編集禁止」

採点システム（moulinette）と交わす契約となる Pydantic モデル群。

| モデル | 役割 |
|---|---|
| `StepMetrics` | 1イテレーション分の記録。`input_tokens`/`output_tokens`/`request_time_ms` に加え、**生の** `llm_output`/`sandbox_input`/`sandbox_output` を保持する。これにより後から「本当に何が起きたか」をトレースできる（`success` フィールドだけを信じない、という後述のベンチマークレポートの結論に直結）。 |
| `SolutionOutput` | 最終成果物。`task_id`, `benchmark`, `success`, `solution`（MBPPなら関数コード、SWE-benchならgit diff）, `steps[]`, `error` などを持つ。失敗時にも必ず書き出される。 |
| `SandboxConfig` | `authorized_imports`（`math.*` のようなglobパターン対応）, `allowed_directories`, タイムアウト・メモリ・出力文字数の上限。 |
| `MBPPTaskInput` / `SWEBenchTaskInput` | moulinette から渡されるタスク定義の型。 |

---

## 5. `orchestrator.py` — エージェントループの心臓部

`Orchestrator.run(task_id, benchmark, task_prompt)` が1タスクを最後まで走らせ、`SolutionOutput` を返す。

### ループの1イテレーションの流れ

```python
for step_number in range(1, max_iterations + 1):
    # 0. 停止条件チェック（SIGTERM要求／時間予算／入力トークン予算／出力トークン予算）
    # 1. 次のリクエストで超過しないか「送信前に」見積もる（後述）
    # 2. LLMClient.generate() を呼ぶ（stop=["<end_code>"]）
    # 3. code_extraction.extract_code() でコード抽出
    # 4. コードが無ければ note をそのまま Observation にする
    # 5. あれば Sandbox.run() で実行 → 結果 or FinalAnswer 例外
    # 6. StepMetrics を記録
    # 7. FinalAnswer が来ていればループを抜けて成功終了
    # 8. そうでなければ assistant/user メッセージを追記して次へ
```

### 設計上の工夫

- **stop sequence `<end_code>` の必然性**: これがないと、モデルが本物のツール実行結果を
  待たずに、次のObservationを幻覚して自分で書き続けてしまう危険がある（README.md Section 4.6 のtip）。
- **コードブロックが無い場合の扱い**: `[NoCodeBlock] ...` をそのまま次のObservationとして
  LLMに見せる。ループが「たぶんこうだろう」と推測することは絶対にしない。これは
  Section 4.1 の「明示的フィードバック必須」要件を体現している。
- **`FinalAnswer` は特別扱い**: `sandbox.run()` 内の汎用 `except Exception` では
  絶対に握りつぶさない。`final_answer()` はサンドボックスの制御フロー用シグナルであり、
  `KeyboardInterrupt`/`SystemExit` と同格に扱われる。
- **保守的なトークン予算の事前チェック** (`_conservative_input_token_bound`):
  実際にリクエストを送ってから「トークン超過でした」と分かるのではなく、送信予定メッセージの
  UTF-8バイト長から**送信前に**上限を見積もり、予算超過が予想されるなら未然にループを止める。
  初回はバイト数+32を保守的な上限とし、2回目以降は前回の実測トークン数に「増分バイト数+16」を
  足すことで、変化していないプロンプト部分に毎回バイト単位のワーストケースを適用せずに済ませている。
- **`ShutdownRequested` は `BaseException`**: `Exception` ではなく `BaseException` を継承。
  これにより、LLM呼び出し中やサンドボックス実行中にSIGTERMが届いても、
  `Sandbox.run()`側の汎用例外処理や `requests`/`urllib3` 内部のエラーハンドリングに
  握りつぶされず、即座にループを中断できる。moulinette の「SIGTERM→SIGKILLまでの猶予は
  約10秒」という制約の中で、SWE-bench側の `finally: container.cleanup()` を確実に
  実行させるための仕組み。

### 終了時に返す `SolutionOutput`

`success`/`solution`/`iterations`/`total_requests`/`total_input_tokens`/`total_output_tokens`/
`total_time_seconds`/`steps`/`system_prompt`/`error` をすべて埋めて返す。
失敗時（例外、タイムアウト、予算超過、上限イテレーション到達）でも空の `SolutionOutput` ではなく、
**そこまでの steps を含んだ** `SolutionOutput` を返す設計になっている。

---

## 6. `code_extraction.py` — フォーマット非依存化レイヤー

LLM ごとにツール呼び出しの書き方の癖が異なる。このモジュールは、あらゆる形式を
**サンドボックスが実行できる等価な Python コード文字列**に変換してから渡す。
これによりサンドボックス自体は完全にフォーマット非依存でいられる。

対応する形式（優先順位順）:

1. **正しく閉じた ```` ```python ... ``` ```` フェンス**（`<end_code>` または ``` ``` ``` で終端）
   — プライマリ形式。変換不要でそのまま抽出。
2. **閉じられていない ```` ```python ```` フェンス** — 救済策。残り全部をコードとして使い、
   `[MalformedCodeBlock]` の note を付ける。
3. **XML `<invoke name="...">...</invoke>`** 形式 — `<parameter>` を kwargs に変換し、
   `result = name(key=value, ...)\nprint(result)` の形にする。
4. **JSON/Hermes `<tool_call>{"name": ..., "arguments": {...}}</tool_call>`** 形式。
5. **ReAct の `Action: name\nAction Input: {...}`** 形式。
6. 上記いずれにも当たらなければ、最初の汎用フェンスブロックを最後の手段として使う。
7. それでも何も見つからなければ `code=None` を返し、`[NoCodeBlock]` という明示的なnoteを付ける。

`ExtractionResult(code, note)` の `note` は、Orchestrator が Observation の先頭に付加する
「何が起きたか」の説明文になる ── これも「暗黙に何かをしたら、必ずそれをLLMに伝える」という
一貫した設計方針の一部。

---

## 7. `prompts.py` — システムプロンプト構築

`build_system_prompt(benchmark, sandbox_manual, include_example=True)` が、
以下の3パーツを結合してシステムプロンプト全文を組み立てる:

1. **`FRAMEWORK_EXPLANATION`** — Thought/Code/Observation ループのルール説明。
   「コードは必ず1つの ```` ```python ... ``` ```` ブロックに全部入れて `<end_code>` で終える」
   「ツールは必ずキーワード引数で呼ぶ（ただしこれは*助言*であり保証ではない、後述の
   `mcp_client.py` の対策参照）」「Observationを絶対に自分で推測しない」など。
2. **`sandbox_manual`** — `MCPToolProxy.manual_text()` から動的に生成される、
   現在接続中のMCPサーバーが持つツール一覧とシグネチャ。**別のMCPサーバーに繋ぎ変えるだけで
   この部分も自動的に変わる**ため、「未知のMCPサーバーでテストされる」という課題要件に対応できる。
3. **`final_answer` の使い方の説明 + worked example**（`include_example=True` の場合）。

`include_example` フラグは本番では常に `True` だが、`BENCHMARK_REPORT.md` の
アブレーション実験（worked exampleを抜くと何が起きるか）のために存在する。

---

## 8. `sandbox/` — 実行境界（最重要パート）

LLMが生成した**信頼できないコード**を安全に実行するための多層防御。3つのファイルが
役割分担している。

### 8.1 `sandbox/executor.py`

`Sandbox` クラス本体。`isolated=True`（デフォルト）なら `IsolatedSandboxProcess` に処理を委譲し、
`isolated=False` は「OS境界が既に確立された後、ワーカー自身がコードを実行するための内部モード」。

#### インポート制限（多重チェック）

- **静的チェック** `check_imports()`: AST を walk して `ast.Import`/`ast.ImportFrom` ノードを
  `authorized_imports`（globパターン対応のアローリスト）と照合。
- **動的チェック** `_make_restricted_import()`: `__import__` 自体を差し替え、
  `__import__("os")` のように**関数として直接呼ぶ**バイパスも同じアローリストで弾く。
- **モジュールプロキシ** `_RestrictedModule`: 許可されたモジュールを生のまま返すと危険
  （`random._os` や `typing.sys` のように、許可モジュールが内部で非許可モジュールへの
  参照を持っていることがある）。そこで `ModuleType` を継承したラッパーで包み、
  非公開属性へのアクセスと非許可モジュールの露出を `__getattribute__` レベルで拒否する。

#### ファイルアクセス制限

`_make_restricted_open()` が `open` を差し替え、`os.path.realpath()` で解決した対象パスが
`allowed_directories` のいずれかの配下にあるかを検証する。

#### 危険な組み込みの除去

`_UNSAFE_BUILTINS = {eval, exec, compile, input, breakpoint, help, exit, quit,
__import__, open, vars}` を `builtins` から取り除いた辞書を `__builtins__` として渡す。

#### サンドボックスエスケープ対策（`check_dunder_attribute_access`）

このファイルで最も重要な部分。古典的な in-process Python サンドボックス脱出手法:

```python
().__class__.__bases__[0].__subclasses__()
```

これはロード済みの全クラスを辿り、`__init__.__globals__['__builtins__']` が
**本物の制限なし builtins モジュール**であるクラスを見つけ出す手口。
`__import__`/`open` の差し替えは**名前ルックアップ**しか守らないため、
このような「サンドボックスが一度も手渡していないオブジェクトへの任意の属性アクセス」には
無力 ── というのが `BENCHMARK_REPORT.md` に記録されている「独立レビューで見つかった重大な
脆弱性」の正体。

対策として、**デフォルト拒否のダンダー属性アローリスト**を導入:

- `_SAFE_DUNDER_ATTRS` に `__init__`, `__repr__`, 各種演算子オーバーロード
  (`__add__`, `__eq__`, ...), イテレーションプロトコル (`__iter__`, `__next__`) など、
  MBPP/SWE-bench の解答コードが正当に必要とする最小限のダンダー属性だけを列挙。
- `_is_forbidden_attribute()` が「`_` で始まり、かつこのアローリストに無い名前」を
  全て拒否する。これにより `__subclasses__`, `__globals__`, `__bases__`/`__base__`/`__mro__`,
  `__builtins__`, `__code__`/`__closure__`, `__getattribute__`, `__reduce__`/`__reduce_ex__`
  といった、危険な属性を個別に列挙する必要なく**まとめて**塞げる。
- `check_dunder_attribute_access()` は AST を walk して `ast.Attribute` ノードの
  ドット記法アクセス (`obj.__subclasses__`) を静的に拒否する。
- **動的バイパスも塞ぐ**: `getattr(obj, "__sub" + "classes__")` のように文字列を組み立てて
  `getattr` 経由でアクセスするケースは静的チェックをすり抜けるため、`getattr`/`setattr` 自体を
  `_make_restricted_getattr()`/`_make_restricted_setattr()` で差し替え、同じルールを
  ランタイムでも強制する。
- `str.format` も明示的に禁止 (`_FORBIDDEN_PUBLIC_ATTRIBUTES = {"format"}`)。理由は
  `"{0.__class__}".format(x)` のような属性ミニ言語が**文字列から実行時に属性名を解釈する**ため、
  AST上の `Attribute` ノードとしては現れず、上記の静的/動的チェックを両方すり抜けてしまうから。
- **既知の残存ギャップ**としてコード中に明記されているのは、それでも `str.format` を
  完全に無効化しない限り防ぎきれない経路がある可能性があること。ただし `str.format` を
  完全禁止すると MBPP/SWE-bench 用の正当な解答コードを壊す方が実害が大きいため、
  この項目は「OS境界が主たる防御線であり、これは多層防御の一枚」という前提のもとで
  受容されている。

#### タイムアウト・メモリ制限

- `signal.alarm()` + `SIGALRM` ハンドラでコード実行に壁時計タイムアウトをかける
  （`SandboxTimeoutError` を送出）。
- `resource.setrlimit(RLIMIT_AS, ...)` でプロセスのアドレス空間を制限し、
  暴走したメモリ確保を OS の OOM killer ではなく Python の `MemoryError` として捕捉できるようにする。

#### `Sandbox.run()` の返り値ポリシー

**通常のコードエラーで例外を投げることは絶対にない。** すべて `"[ErrorKind] ..."` という
文字列として返し、Orchestrator がそのままLLMへのObservationとして使えるようにする。
`FinalAnswer`, `KeyboardInterrupt`, `SystemExit` だけが呼び出し元へ伝播する。

### 8.2 `sandbox/isolated_process.py` と `sandbox/isolated_worker.py`

上記のPythonレベルの制限は「多層防御の一枚」に過ぎない。**本丸はOSレベルの隔離**。

#### 構成

- `IsolatedSandboxProcess`（親プロセス側）が `unshare` + `bubblewrap`(`bwrap`) で
  常駐ワーカープロセスを起動し、標準入出力越しのJSONプロトコルで通信する。
- `isolated_worker.py`（子プロセス側のエントリポイント）が、実際に `Sandbox(isolated=False)` を
  使ってコードを実行する。

#### 隔離の中身（`_build_command`）

```
unshare --user --map-root-user --net --   # 専用 user namespace + network namespace（NIC無し）
  bwrap --clearenv --die-with-parent --unshare-user
        --uid 65534 --gid 65534            # 非特権ユーザーとして実行
        --ro-bind /usr /usr, /lib, /lib64  # 最小限の読み取り専用ルート
        --dev /dev --proc /proc --tmpfs /tmp
        --dir /agent/sandbox --ro-bind <project>/sandbox /agent/sandbox
        --dir /agent/site-packages --ro-bind <venv>/site-packages /agent/site-packages
        --ro-bind <project>/models.py /agent/models.py
        --bind <allowed_directory> <allowed_directory>  # SandboxConfig.allowed_directories だけ
        python /agent/sandbox/isolated_worker.py
```

重要なのは **プロジェクトルートを丸ごとマウントしない**こと。`.env` ファイルなど
機微な情報を含みうるリポジトリルートではなく、`sandbox/` パッケージ、`models.py`、
必要な site-packages、そして `SandboxConfig.allowed_directories` に列挙されたディレクトリだけを
個別に ro-bind/bind する。`_validate_allowed_target()` は `/`, `/agent`, `/usr` などの
保護対象ルート自体やその内部を `allowed_directories` に指定することも拒否する。

#### 通信プロトコル

親↔子は改行区切りのJSONメッセージで通信する:

- 親→子: `{"type": "init", "config": ..., "tool_names": [...], "apply_process_memory_limit": ...}`
  → 子は `{"type": "ready"}` か `{"type": "worker_error", ...}` を返す。
- 親→子: `{"type": "run", "code": "..."}`
- 子→親: `{"type": "tool_call", "name": ..., "args": [...], "kwargs": {...}}`
  （ワーカー内部の `_ToolBridge` がツール呼び出しをそのまま親に転送する）
- 親→子: `{"type": "tool_result", "ok": bool, "result": ...}`
- 子→親: `{"type": "result", "output": "..."}` / `{"type": "final_answer", ...}` /
  `{"type": "keyboard_interrupt"}` / `{"type": "system_exit", "code": ...}` /
  `{"type": "worker_error", ...}`

**ワーカーは常駐する**ので、`namespace` に定義された変数は agent の各ステップ間で
保持される（1タスク＝1ワーカープロセスのライフサイクル）。MCPツール呼び出しは
ワーカー内では「ただの関数呼び出し」に見えるが、実体は `_ToolBridge` が親プロセスに
JSON-RPC的にリクエストを転送し、親側が実際の `MCPToolProxy` を叩いて結果を返す、という
仕組みになっている。ワーカー自身は生の `MCPToolProxy`（や asyncio イベントループ）を
一切知らない。

#### 二重のタイムアウト

- 親側 (`IsolatedSandboxProcess.run`) が壁時計デッドラインを持ち、超過したら
  `os.killpg(SIGTERM)` → 応答が無ければ `SIGKILL` でワーカーのプロセスグループごと殺す。
- ワーカー側 (`Sandbox` 内部) も `signal.alarm()` で自分自身の実行を打ち切る
  （クリーンな Python 例外としての `[Timeout]` を返せるようにするため）。
- MCPツール呼び出しは別スレッドで実行され、`thread.join(timeout)` でタイムアウトを検知する
  （親プロセス側のRPC待ちがハングしても、サンドボックス実行全体が無限に止まらないようにする
  ためのもう一段の防御）。

### 8.3 `sandbox/mcp_client.py`

`MCPToolProxy` — サンドボックスの外（信頼された親プロセス側）で動く、MCP公式SDKの
非同期クライアントに対する**同期ファサード**。

- **なぜ必要か**: サンドボックスの `exec()` 名前空間には `result = search_code("foo")` のような
  普通の同期Python関数が必要だが、`mcp` パッケージの `ClientSession` は asyncio ベース。
  このモジュールはバックグラウンドスレッドで専用イベントループを1つ回し、
  `asyncio.run_coroutine_threadsafe()` で全呼び出しを橋渡しすることで、
  他のコードは一切 asyncio を意識しなくて済むようにしている。
- **2つの必須トランスポートに両対応**: `stdio_client`（MCPサーバーをサブプロセスとして起動）と
  `streamablehttp_client`（既に起動しているHTTPサーバーに接続）。
- **ツール名を一切ハードコードしない**: コンストラクタが接続したら `list_tools()` を呼び、
  返ってきたツールスキーマから動的にラッパー関数を生成する (`build_namespace()`)。
  課題要件「未知のMCPサーバーでテストされる」への対応そのもの。
- **`manual_text()`**: 同じツールスキーマから、システムプロンプトに埋め込む
  人間可読なドキュメント文字列を生成する。ツール名・パラメータ名・型・必須/任意・説明文を
  自動整形するので、`prompts.py` は接続先のMCPサーバーが何であるかを一切知らなくてよい。
- **位置引数もキーワード引数も受け付ける** (`_make_wrapper`): システムプロンプトでは
  「必ずキーワード引数で呼べ」と指示しているが、これは*助言*であって保証ではない。
  課題自身のワークドイグザンプル (`result = search_code("validate_email")`) も位置引数を
  使っている。そこで、MCPツールのJSON Schemaの `properties` の宣言順を関数の実引数順とみなし、
  `*args` をその順序でパラメータ名にマッピングしてから `**kwargs` とマージする。
  （実際にこれが無かったために `wrapper() takes 0 positional arguments but 1 was given`
  という実行時エラーが起きたバグが `BENCHMARK_REPORT.md` に記録されている。）
- **すべての境界にタイムアウト**: 接続確立30秒、ツール呼び出し300秒、close 10秒。
  死んだ/ハングしたMCPサーバーが、サンドボックス（延いてはエージェント全体、
  延いては `container.cleanup()`）を無期限にブロックしないようにするため。
- **1つの「オーナーコルーチン」が全トランスポートコンテキストを所有**する設計
  (`_connection_owner`): AnyIOのトランスポートコンテキストは、それを`enter`したのと
  同じタスクが`exit`しなければならない制約があるため、接続確立からclose要求までを
  1つの長寿命コルーチンとして実装し、クロスタスクな `AsyncExitStack` の後始末失敗を防いでいる。

### 8.4 `sandbox/cli.py`

`uv run sandbox` で起動する対話的REPL。

```
uv run sandbox                                       # ツール無しのREPL
uv run sandbox sandbox_template.json                 # カスタム設定
uv run sandbox --mcp-stdio "python mcp_tools_mbpp.py" # MBPPツール付き
uv run sandbox --mcp-server http://localhost:8000/mcp # HTTP経由でMCP接続
```

コードを何行か入力し、空行でその塊を実行、`exit` か Ctrl+D で終了。デバッグ・動作確認用の
簡易フロントエンドで、`Sandbox` と `MCPToolProxy` の実際の使い方の最小サンプルにもなっている。

---

## 9. `llm/` — LLM プロバイダ抽象化層

### 9.1 `llm/provider.py`

`ChatProvider` プロトコル（`chat(messages, model, api_key, stop, max_output_tokens, timeout)
-> GenerationResult`）を定義。`requirements.md` が明示的に指摘する実装ギャップ
（「`generate()` はテキストだけでなく tokens/timing/api_url/model_name を返さねばならない」）
を埋めるのが `GenerationResult`。`UsageStats` は実行全体を通じた
requests/retries/tokens/latency の集計を担う。

### 9.2 `llm/client.py`

`LLMClient` — 1つの論理モデルを、複数プロバイダにわたるフォールバック付きで呼び出す。

- **キーのラウンドロビン**: `ProviderSpec.collect_api_keys()` が拾い上げた
  `<PREFIX>_API_KEY`, `_API_KEY_2`, ... を `next_key_index` で順番に回す。1つのキーが
  レート制限にかかっても全体が止まらないようにする（Section 4.6.1「複数トークン管理は必須」）。
- **プロバイダのフォールバック**: 与えられた `provider_specs` の順に試し、あるプロバイダの
  全キーが尽きたら次のプロバイダへ移る。
- **`AllProvidersExhaustedError`**: 全滅した場合に送出。**成功パスを一度も通っていない
  ので `StepMetrics` は作れないが、それでも実際に送られたHTTPリクエスト数
  (`attempted_requests`) は失われてはならない** ── Orchestrator側で
  `total_requests += exc.attempted_requests` として拾われる。
  （これが無かったために「全滅した試行の `total_requests` が0のまま記録される」
  というバグがあり、`BENCHMARK_REPORT.md` で修正記録が残っている。）
- **リトライのバックオフ**: 同一キーに対して `max_retries_per_key` 回まで、
  `backoff_seconds * (attempt + 1)` の線形バックオフでリトライする。

### 9.3 `llm/providers/openai_compatible.py`

OpenRouter・Groq・Together AI・Fireworks AI など、標準的な `/chat/completions` ワイヤ形式を
話すプロバイダ共通の実装。`{"model", "messages", "max_tokens", "stop"}` を送り、
`choices[0].message.content` と `usage.{prompt,completion}_tokens` を読む、素直な実装。

### 9.4 `llm/providers/gemini.py`

Google AI Studio 用。構造が根本的に異なる（エンドポイントが `/models/{model}:generateContent`、
認証はヘッダーではなくクエリパラメータ `?key=...`、リクエストは `contents`/`parts`、
レスポンスは `candidates` スキーマ）ため独立ファイルになっている ──
これにより「抽象化がOpenAI形式のラッパーに過ぎない」という状態を避け、
本当に異なる2実装で `ChatProvider` プロトコルが機能することを証明している。

**実際のインシデントから生まれた対策**がコメント付きで残っている: `requests` の
`HTTPError`/`ConnectionError`/`Timeout` は、デフォルトのエラーメッセージを
**リクエストURL全体から**組み立てる。Geminiはヘッダー認証が無く `?key=...` を
使うため、そのままだとAPIキーが例外メッセージに含まれてしまう。そのメッセージは
`AllProvidersExhaustedError` → `SolutionOutput.error` / `StepMetrics.sandbox_output`
経由で **`solution.json` に平文で書き込まれる**。実際にこの経路で3つの
`solution.json` に本物のキーが漏れ、GitHubのpush protectionに検知されて初めて
発覚したというインシデントが記録されている。対策として、`requests.RequestException` を
そのまま伝播させず、**キーを含まない `url`（クエリパラメータ抜き）とステータスコードだけ**から
メッセージを再構築してから送出する。

---

## 10. `config.py` — 環境変数とプロバイダ設定

- `load_env()`: プロジェクトルートの `.env` を（一度だけ）読み込む。シェルで既に設定済みの
  環境変数は上書きしない (`override=False`)。
- `ProviderSpec`: 1プロバイダの静的定義（名前、base_url、APIキーの環境変数プレフィックス、
  `kind`（`"openai_compatible"` か `"gemini"`））。`collect_api_keys()` が
  `<PREFIX>_API_KEY`, `_2`, `_3`, ... を集める。
- `KNOWN_PROVIDERS`: OpenRouter / Groq / Together / Fireworks / Google AI Studio の
  レジストリ。
- `resolve_provider(base_url)`: `--provider-url` を既知レジストリと前方一致で照合し、
  ヒットしなければ **URLから `<HOST>_API_KEY` という環境変数名を自動生成**した
  `ProviderSpec` を即席で作る。これにより「新しいプロバイダを追加するのにコード変更は不要、
  対応する環境変数を用意するだけでよい」という Section 5.6 の要求を満たす。

---

## 11. `agent_mbpp.py` / `agent_swebench.py` — エージェント CLI

両者とも骨格は完全に共通:

```
task.json 読み込み・バリデーション（Pydanticモデルへ）
  → SIGTERMハンドラ登録（Orchestrator未生成ならShutdownRequestedを直接送出）
  → MCPプロキシ起動（stdio経由でmcp_tools_*.pyを子プロセスとして起動）
  → SandboxConfig を組み立てて Sandbox 生成（MCPツールをextra_namespaceとして注入）
  → build_system_prompt() でシステムプロンプト生成
  → LLMClient.from_provider_url() でLLMクライアント生成
  → Orchestrator 生成・実行
  → 例外の種類に応じて error_solution() でフォールバック
  → finally で sandbox.close() / mcp_proxy.close() / (SWE-benchなら) container.cleanup()
  → 成功でも失敗でも必ず solution.json を書き出す
```

### `agent_mbpp.py` 固有の設定

- MCPサーバーはローカルサブプロセスとして `python mcp_tools_mbpp.py` を stdio 起動。
- `AGENT_SMITH_TEST_IMPORTS` という環境変数で `task.test_imports`
  （例: `math.isclose` を使うテストに必要な `math`）をMCPサーバーへ渡す。
  これは「候補コード自身がその import を必要としているとは限らないが、テストのassertion側は
  必要としている」というケースへの対応。渡さないと、LLMがたまたま同じimportを書かない限り
  `NameError` になる（実際に427タスク中13タスクでこれが問題になっていたと
  `BENCHMARK_REPORT.md` に記録がある）。
- `max_execution_time_seconds=20` は、`mcp_tools_mbpp.py` 内部の `run_tests()` が使う
  10秒のサブサンドボックスタイムアウトより**確実に長く**設定されている。これが無いと、
  正しいが少し遅いテスト実行に対して外側のサンドボックスアラームが先に発火し、
  正しい解答を誤ってタイムアウト扱いにしてしまう（実際のプロバイダに対するスモークテストで
  発見された不具合として記録あり）。
- 予算: 10イテレーション、入力6,000/出力1,500トークン、120秒（うち110秒がOrchestratorに渡る
  時間予算、残り10秒はクリーンアップ用の余白）。

### `agent_swebench.py` 固有の設定

- `SweBenchContainer`（`docker_runner.py`）でタスクのDockerイメージを起動し、
  `container.mcp_stdio_command()` が返す `docker exec -i ... python3 <tools>` を
  `MCPToolProxy(stdio_command=...)` に渡す。つまり **サンドボックス（Pythonインタプリタ）は
  ホスト側に残ったまま、MCPツールサーバーだけがコンテナの中で動く**（Section 4.4の
  アプローチ(b)）。
- `allowed_directories=["/testbed", str(SCRATCH_DIR)]`。
- 予算: 30イテレーション、入力300,000/出力10,000トークン、900秒（うち870秒がOrchestrator、
  30秒はコンテナクリーンアップ用の余白）。

---

## 12. `mcp_tools_mbpp.py` / `mcp_tools_swebench.py` — MCP ツールサーバー

どちらも `mcp.server.fastmcp.FastMCP` を使い、`@mcp.tool()` デコレータでツールを定義する
別プロセス。`--http <port>` を渡せば streamable HTTP、渡さなければ stdio で待ち受ける。

### `mcp_tools_mbpp.py`

ツールは `run_tests(code, test_list) -> str` の1つだけ（Section 4.3.2）。

- 候補コード + assertion群 + 秘密のマーカー文字列（`secrets.token_hex(16)` 由来、
  推測不可能にすることで候補コードが `print()` で偽装するのを防ぐ）を1つのPythonスクリプトに
  連結し、**エージェント本体が使うのとは別の、使い捨てのサンドボックス**
  （`allowed_directories=[]`、10秒タイムアウト）で実行する。
  これにより候補コードはMCPサーバー自身のホストファイルシステムやネットワークに一切触れない。
- マーカーが出力に含まれていれば成功と判定し、`{"success": bool, "output": str}` の
  JSON文字列を返す。
- `_test_imports()` が `AGENT_SMITH_TEST_IMPORTS` 環境変数から `test_imports` を読み、
  候補コードの前に自動的に前置する。

### `mcp_tools_swebench.py`

必須9ツールをすべて実装（Section 4.5）。**Docker固有のロジックは一切持たない** ──
`TESTBED_PATH` 環境変数さえ設定されていれば、ホスト上のベアなチェックアウトでも、
コンテナの中でも同じように動く。

| カテゴリ | ツール | 補足 |
|---|---|---|
| ファイルシステム | `read_file(filepath, start_line, end_line)` | `cat -n` 風に行番号付きで返す |
| | `edit_file(filepath, old_str, new_str)` | 完全一致文字列を1箇所だけ置換。`.py`なら編集後に `py_compile` で構文チェックし、壊れたら `[EditSyntaxError]` を明示的に返す（Section 4.1の必須フィードバック） |
| | `list_files(directory, pattern)` | 非再帰デフォルト、`**/` プレフィックスで再帰 |
| コード検索 | `search_code(pattern, file_pattern)` | grep風の正規表現検索、`path:line content` 形式で返す |
| | `search_function_or_class_definition_in_code(name)` | `search_code` の定義検索特化ラッパー |
| | `find_references(name, filepath, line)` | 定義位置自体を結果から除外できる |
| 実行系 | `run_command(command, workdir)` | シェルコマンド実行、120秒タイムアウト、SIGTERM→SIGKILLで確実に終了させる |
| | `run_tests()` | `AGENT_SMITH_EVAL_SCRIPT`（無ければ `<testbed>/eval.sh`）を `run_command` 経由で実行 |
| | `get_patch()` | `git -c core.fileMode=false diff` の出力をそのまま返す（Section 4.4） |

- **パスの脱出防止**: `_resolve_within_testbed()` が絶対パス化・`resolve()` した上で
  `TESTBED_PATH` の配下かどうかを検証し、外れていれば例外にする。`list_files`/`search_code` の
  glob展開結果も同様にチェックする（`_matching_files`）。
- **出力サイズの上限** `_cap_output()`（20,000文字、`SandboxConfig.max_output_chars` と
  同スケール）: これが無いと、巨大リポジトリでの検索や冗長なテスト出力が
  SWE-benchの累積300,000トークン予算を1ステップで大きく消費しかねない。
  ただし **`get_patch()` だけは例外**で切り詰めない ── その返り値は
  `final_answer(get_patch())` の直接の引数になりうるため、切り詰めると
  壊れた（適用不能な）diffをそのまま提出してしまうことになるから。

---

## 13. `docker_runner.py` — Docker ブリッジ

`SweBenchContainer` がタスクの Docker イメージのライフサイクル全体を管理する。
実装しているのは Section 4.4 の**アプローチ(b)**: サンドボックス（Pythonインタプリタ）自体は
ホストに置いたまま、**MCPツールサーバーのプロセスだけ**を `docker exec` でコンテナの中に
立てる。`mcp_tools_swebench.py` 自体は Docker を意識しないので、同じコードが
ベアなホストチェックアウトでもコンテナ内でも動く。

主な処理:

1. `images.pull()` → `containers.run(command="tail -f /dev/null", detach=True)` で
   長寿命コンテナを起動。
2. `eval.sh` と `mcp_tools_swebench.py` の中身をコンテナへ書き込む。
3. コンテナ内に `mcp`/`pydantic` パッケージがなければ `pip install` でブートストラップ
   （ネットワークが無いイメージでは失敗するが、それは「グレースフルなエージェントエラー」として
   `agent_swebench.py` 側で処理される）。
4. `mcp_stdio_command()` が `docker exec -i -e TESTBED_PATH=... -e AGENT_SMITH_EVAL_SCRIPT=...
   <container_id> python3 <tools_path>` を組み立てて返す。
5. `cleanup()` — `stop(timeout=5)` → `remove(force=True)`。両方とも例外を握りつぶす
   （後始末の失敗でエージェント全体をクラッシュさせないため）。

### 実際に見つかった `docker cp` バグの回避策 (`_write_into_container`)

`docker cp` はtarベースのコピーで、抽出したファイルをホストのUIDに`lchown`しようとする。
このホストのようにユーザーごとの subuid/subgid レンジ（`/etc/subuid`）でコンテナのUID
リマッピングを行っている環境では、ホストユーザーのUIDがそのレンジ外に落ちて
`Error response from daemon: failed to Lchown ...: invalid argument` で失敗する
（実際にこのホストで遭遇し、`BENCHMARK_REPORT.md` に記録されている。moulinette自身の
`validate swebench` も内部で同じエラーを踏むことが確認されている＝このプロジェクト固有の
バグではなくホスト環境依存の既知の問題）。

対策: `docker cp` を使わず、`docker exec -i <id> sh -c 'cat > <path>'` に標準入力で
内容を流し込む方式にする。これはコンテナのエントリポイントが動くユーザーとして書き込むため、
ホスト側のUIDマッピングの影響を受けない。この `docker exec` 呼び出しは
`docker-py` クライアントではなく `docker` CLIを直接呼んでいるため、
`docker-py` クライアントのデフォルトタイムアウトを継承しない ── そこで明示的に
`timeout=30` を指定し、コンテナが応答不能になった場合でも `container.start()`
（＝エージェント全体）を無期限にハングさせないようにしている。

---

## 14. 設定ファイル・補助ファイル

- **`pyproject.toml`**: `uv` で管理。ランタイム依存は `pydantic`, `python-dotenv`,
  `requests`, `mcp`, `docker` の5つのみ。開発依存として `flake8`, `mypy`, `pytest`。
  `[project.scripts] sandbox = "sandbox.cli:main"` により `uv run sandbox` が使えるようになる。
  `requires-python = "==3.10.*"` に固定。
- **`Makefile`**: `install`/`run`（`agent_mbpp --help`）/`debug`（`pdb`経由）/`sandbox`/
  `clean`/`fclean`/`lint`/`lint-strict`/`test` のショートカット。
- **`.env.example`**: 各プロバイダのAPIキー環境変数のテンプレート。`_2`, `_3` サフィックスで
  複数キーを登録できることがコメントで明記されている。実運用の `.env` は `.gitignore` されており
  「ソースにキーをハードコードすることは即セキュリティ違反」と明記。
- **`sandbox_template.json`**: `SandboxConfig` のJSON表現の実例。デフォルトの
  `DEFAULT_AUTHORIZED_IMPORTS`/`DEFAULT_ALLOWED_DIRECTORIES` と同内容。
- **`conftest.py`**: pytest がどこから呼ばれてもプロジェクトルートを `sys.path` に
  追加するだけの小さなブートストラップ。
- **`.flake8`**: flake8 の設定（詳細は個別ファイル参照）。
- **`cache/`**: `dump` してきたタスクJSONや生成した`solution.json`の一時置き場
  （`.gitkeep` のみコミット対象）。
- **`solutions/`**: `BENCHMARK_REPORT.md` の実測結果として得られた15本の `solution.json`
  （＋アブレーション実験の1本、＋タスク定義そのもの）が証跡として保存されている。
  命名規則は `<provider>_<model>_<task>.json`。

---

## 15. テストスイート（`tests/`）

`make test`（=`uv run pytest -v`）で、**ネットワークもAPIキーも一切不要**に完結するように
設計されている。各ファイルの狙い:

| ファイル | 検証対象 |
|---|---|
| `test_code_extraction.py` | Section 4.1 の全フォーマット（fence/XML/JSON/ReAct）が正しくPython呼び出しへ正規化され、未知の形式は明示的に失敗すること |
| `test_sandbox.py` | インポート制限・ファイルアクセス制限・タイムアウト・メモリ制限・**サンドボックスエスケープ対策**（dot記法、`getattr`、動的組み立て名、`__globals__`、`setattr`、通常の演算子オーバーロード/イテレーション/reprが壊れていないこと）を検証。メモリ上限のテストは（pytestプロセス自体のRLIMIT_ASを下げないよう）isolatedワーカー経由で行う |
| `test_llm_client.py` | キーローテーション・プロバイダフォールバック・リトライ・使用量集計。偽の`ChatProvider`でネットワークを完全に置き換える |
| `test_gemini_provider.py` | **APIキーが例外メッセージに絶対に漏れないこと**にフォーカスした専用テスト |
| `test_orchestrator.py` | 偽のLLMクライアント＋本物の`Sandbox`でThought→Code→Observationループをend-to-endで検証 |
| `test_mcp_client.py` | `mcp_tools_mbpp.py` を実サブプロセスとして起動した`MCPToolProxy`の結合テスト（ネットワーク不要） |
| `test_mcp_tools_mbpp.py` | `run_tests`ツール関数を直接呼び出し（`@mcp.tool()`デコレータは元の関数呼び出し可能性を保持するため、MCPトランスポート無しでテストできる） |
| `test_mcp_tools_swebench.py` | 必須9ツールを、`TESTBED_PATH`直下に作った小さな偽リポジトリに対して検証 |
| `test_agent_startup.py` | 両エージェントCLIの起動時エラー処理とシャットダウン処理 |
| `test_edge_cases.py` | 各コンポーネント共通の境界値・不正入力ケース |

---

## 16. セキュリティ設計まとめ

`README.md` の表を踏まえて要点だけ再掲する。

| 懸念 | 対策 | トレードオフ |
|---|---|---|
| インポート制限 | ASTウォーク + 実行時`__import__`パッチの二重チェック。許可モジュールも制限プロキシで包み、非公開属性・非許可の入れ子モジュールを隠す | — |
| OS隔離 | `unshare`+`bwrap`でネットワーク無効・専用PID/mount namespace・最小読み取り専用ルート・UID/GID 65534 | Linux user namespaceと`bwrap`が必須。セットアップ失敗時はfail-closed（無許可の代替実行にフォールバックしない） |
| ファイルシステム制限 | `open`を`realpath`解決＋`allowed_directories`チェック付きに差し替え | OS境界とこのラッパーの両方が必要（片方だけでは不十分という前提） |
| ネットワーク | bubblewrapの外側でnetwork namespaceを作成、NICなし | Linux namespaceサポートに依存 |
| 実行タイムアウト | 親がSIGTERM→SIGKILLでプロセスグループを終了、ワーカー自身もSIGALRM | 親側MCP呼び出しのハングは別途RPCタイムアウトで対応 |
| メモリ制限 | `RLIMIT_AS`をワーカー自身に適用 | ワーカーにのみ適用、エージェント本体やテストランナーには影響しない |
| 制限付きbuiltins | `eval`/`exec`/`compile`/`input`/`__import__`/`open`等を除去・差し替え | — |
| 非公開属性アクセス | デフォルト拒否のアローリスト。`getattr`/`setattr`も同一ルールで動的強制。`str.format`と`string.Formatter`、`operator.attrgetter`/`methodcaller`は使用不可 | OS境界が依然として主たる防御線 |
| `final_answer()` | どのMCPサーバーが繋がっていても常に注入される独立クロージャ。予約名として衝突を拒否し、`FinalAnswer`は汎用例外処理で握りつぶされない | Section 4.2の「例外伝播」要件に厳密準拠 |

---

## 17. `BENCHMARK_REPORT.md` からの知見

Section 4.7 要求（5モデル以上×2プロバイダ以上×3タスク以上）を満たす実測レポート。
要点:

- **実行環境**: Groq / OpenRouter / Google AI Studio の実際の無料枠キーで、
  実際のDockerイメージ（`sympy__sympy-14711`, `sympy__sympy-13480`,
  `pydata__xarray-4629`）に対して実行。Together AIは無料枠にチャット補完モデルが
  無かったため除外。
- **正しさの検証方法**: `success: true` はあくまで「エージェントが `final_answer()` を
  呼んだ」という意味でしかない。moulinette自身の `validate` コマンドがこのホストでは
  `docker cp` の`Lchown`バグに引っかかって使えなかったため、**すべての `success: true`**
  runについて、パッチを新しいコンテナに `git apply` した上で実際の `eval_script` を
  手動で走らせて独立検証している。
- **結果**: 15回中3回が独立検証済みの合格（`minimax/minimax-m3:free`が1回、
  `gemini-flash-lite-latest`が2回）。Groqの2モデルはTPM（分あたりトークン）上限
  （実測 8,000 tokens/min）が厳しすぎて1〜2リクエストで429に達し、
  `openai/gpt-oss-20b`（Groq）はネイティブfunction-calling機構との非互換で
  全リクエストが400エラー（クォータ問題ではなくアーキテクチャ上の非互換）。
- **アブレーション実験**（Section 4.7 point 5）: システムプロンプトから worked example
  を抜くと、同じモデル・同じタスクで、イテレーション数もトークン数も半分〜1/3に減り
  `success: true` を報告するにもかかわらず、実際には空文字列のパッチを提出していた。
  原因は、exampleが無いとモデルがツール呼び出し結果を `print()` せずに実行し続け、
  `sandbox_output` が空のまま処理を進めてしまうこと。
  **「`success: true` は正しさの証明にならない」という結論の直接的な根拠。**
- **もう一つの落とし穴**: `gemini-flash-lite`のsympy-13480合格runは、`final_answer()`
  以前に**合格したテスト実行の記録が一度もない**状態で提出されていた
  （モデルの推論自体は正しかったが、コードフェンスが繰り返し壊れていて
  実行できていなかった）。たまたま正解だっただけで、規律ある検証とは言えない、
  という指摘も記録されている。
- **見つかったバグ一覧**（詳細は本ファイルの各節に統合済み）: サンドボックスエスケープ、
  `docker cp`のUIDマッピング問題、MCPラッパーの位置引数拒否、MBPPの`test_imports`無視、
  ツール出力の無制限サイズ、MCP呼び出しの無限待ち、`total_requests`の数え漏れ。
  いずれも `make test` のオフラインテストでは検出されず、実際のDocker/実プロバイダ/
  独立レビューによって初めて見つかったもの。

---

## 18. 実行方法チートシート

```sh
cd sample
uv sync
cp .env.example .env   # 実際のAPIキーを埋める

# 対話サンドボックス
uv run sandbox
uv run sandbox sandbox_template.json
uv run sandbox --mcp-stdio "python mcp_tools_mbpp.py"

# MBPPエージェント
uv run python -m agent_mbpp --task-file cache/mbpp_task.json \
    --output cache/mbpp_solution.json \
    --model-name "qwen/qwen3.8-27b" --provider-url "https://api.groq.com/openai/v1"

# SWE-benchエージェント（要 Docker daemon）
uv run python -m agent_swebench --task-file cache/swebench_task.json \
    --output cache/swebench_solution.json \
    --model-name "minimax/minimax-m3:free" --provider-url "https://openrouter.ai/api/v1"

# テスト / Lint
make test    # pytest（ネットワーク・APIキー不要）
make lint    # flake8 + mypy
```

サンドボックスは Linux の `unshare` と `bubblewrap`（`bwrap`）を必要とする。
どちらかが無い環境では、未許可の実行にフォールバックすることなく **fail-closed**
（明示的に失敗する）ように作られている。
