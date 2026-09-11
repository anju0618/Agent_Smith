# 実装解説

このドキュメントは、このリポジトリ（Agent Smith 課題の提出物）に含まれる全ファイルを読んだ上で
まとめた解説です。目的は「動かし方」ではなく「なぜこう設計されているか」を理解すること。

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

「エージェント」という言葉が何を指すかを先に定義しておく。ここでの**エージェント**とは、LLM（大規模言語モデル、いわゆるChatGPTのようなもの）を「1回質問して1回答えをもらって終わり」ではなく、**自分で考え・コードを書き・その結果を見て、また次の一手を考える**というサイクルを何度も繰り返させる仕組みのこと。人間がその都度指示を出す必要はなく、LLM自身が「次に何を試すべきか」を判断しながらループを回す——だから「自律的」と呼ばれる。

エージェントは **Thought → Code → Observation** ループ（[CodeAct](https://arxiv.org/abs/2402.01030)
方式）を繰り返す。この3つの言葉の意味はこうなる:

- **Thought（思考）**: LLMが「次に何を試すか」を自然言語で書く部分。人間で言えば「よし、まずこの関数を書いて、公開テストに通るか確認してみよう」という独り言に相当する。
- **Code（コード）**: その思考を実行可能なPythonコードとして1ブロック書く部分。
- **Observation（観測）**: そのコードを実際に実行した結果（出力やエラーメッセージ）をLLMに見せる部分。LLMはこれを見て、次のThoughtを考える。

つまり「LLM が自然言語で考え（Thought）、Python コードを1ブロック書き（Code）、そのコードがセキュリティ制限付きサンドボックス内で実行され、結果（Observation）が次のターンの入力に追加される」という1周が、そのままThought→Code→Observationの3語に対応する。**サンドボックス**（Section 8で詳しく扱う）とは、「LLMが書いた、信用できるかどうか分からないコード」を安全に動かすための隔離環境のこと——通常のPythonの`exec()`をそのまま使うと、悪意のあるコード（あるいは単にバグのあるコード）がファイルを消したりネットワークに接続したりできてしまうため、実行できる範囲を厳しく制限した箱の中でだけコードを走らせる。

このループは、LLM が `final_answer(...)` という特別な関数を呼ぶまで、あるいはイテレーション数・
トークン予算・時間予算のいずれかが尽きるまで続く。**トークン**とはLLMがテキストを処理する際の最小単位（英単語1つが複数トークンに分かれることもある）で、多くのLLM APIは「入力に使えるトークン数」「1回のリクエストで生成できるトークン数」に応じて課金される。ループを無制限に回すとAPI費用も実行時間も青天井になってしまうため、「最大何回ループを回してよいか（イテレーション数）」「入力・出力それぞれ合計何トークンまで使ってよいか（トークン予算）」「合計何秒まで許すか（時間予算）」という3種類の上限を設け、そのどれか1つでも超えたら強制的にループを打ち切る（詳細はSection 5）。

README.md 末尾の「How AI was used」に記載の通り、開発中は AI コーディングアシスタントを
補助的に活用している。

---

## 2. ディレクトリ構成

各ファイルの役割を一覧できるよう、実際のディレクトリ構成をコメント付きで示す。太字で示した3つ（`orchestrator.py`, `sandbox/`, `llm/`）が特に中核で、他の多くのファイルはこの3つを「どのベンチマーク向けに、どう組み合わせるか」を決める配線係だと考えると読みやすい。

```
.
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

`sandbox/`と`llm/`がそれぞれ独立したPythonパッケージ（サブディレクトリ＋複数ファイル）として切り出されているのは偶然ではない——「信用できないコードを実行する」という関心事と「LLM APIの違いを吸収する」という関心事は、それぞれ内部に複数の役割分担があるほど複雑なので、1ファイルに詰め込まず専用のディレクトリに分けている。逆に`agent_mbpp.py`/`agent_swebench.py`/`code_extraction.py`/`prompts.py`/`models.py`/`config.py`/`docker_runner.py`のようなトップレベルの単一ファイル群は、それぞれが「1つの役割」で完結するくらいの大きさに収まっている、という目安で読み分けるとよい。

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

この図を上から下へ、実際に1回分のループがどう流れるかという時系列で読み解くと分かりやすい:

1. **Orchestrator**（Section 5）がLLM APIにプロンプトを送り、テキスト応答（Thought＋Code）を受け取る。
2. 応答テキストは、LLMごとにツール呼び出しの書き方の癖が異なる（```python フェンスで書くモデルもあれば、XMLタグで書くモデルもある）ため、**code_extraction.py**（Section 6）がその違いを吸収し、どんな形式で来ても「実行すべきPythonコード文字列」に正規化する。
3. 正規化されたコードは**Sandbox**（Section 8）に渡され、そこで初めて実際に実行される。
4. サンドボックス内のコードが（LLMに渡されたツール一覧の中から）ツールを呼び出すと、それは見た目こそ普通のPython関数呼び出しだが、実体は**MCPToolProxy**（Section 8.3）を経由して外部のツールサーバーへ転送される呼び出しである。
5. 転送先は**mcp_tools_mbpp.py**または**mcp_tools_swebench.py**（Section 12）——これらはOrchestratorやSandboxとは**別プロセス**として起動されており、実際のテスト実行やファイル操作を担当する。
6. その実行結果がObservationとしてOrchestratorに戻り、1に戻って次のターンが始まる。

**キーポイントは「Orchestrator は MBPP でも SWE-bench でも一字一句同じ」ということ。**
違いは3つだけ:

1. システムプロンプト（`prompts.py` の `benchmark` 引数で切り替え）
2. サンドボックス設定（`allowed_directories` や実行時間上限）
3. 接続する MCP サーバー（`mcp_tools_mbpp.py` か `mcp_tools_swebench.py`、後者は
   Docker コンテナの中で動く）

この分離のおかげで、「エージェントのロジック」と「タスク固有の道具立て」が完全に疎結合になっている。**疎結合**とは、2つの部品が互いの内部実装をほとんど知らずに済むように設計されている状態を指す——ここでは「Orchestratorはどんなツールが繋がっているかを一切知らなくても動く」し、逆に「ツールサーバー側もOrchestratorの内部ループの都合を一切知らなくてよい」。この結果、新しいベンチマーク（例えば3つ目の未知のタスク種別）を追加したくなっても、Orchestrator自体には一切手を入れずに、システムプロンプト・サンドボックス設定・MCPサーバーの3点だけを新しく用意すればよい、という拡張性が生まれている。

---

## 4. `models.py` — モデル契約

冒頭2行がこのファイルの立ち位置を明言している:

```python
# ABOUTME: Student-facing Pydantic models for the moulinette evaluation contract.
# ABOUTME: This file is copied verbatim from moulinette/models_public.py — do not edit its shape.
```

つまりこれは自作の型ではなく**採点システム側のスキーマのコピー**。フィールドを1つ変えるだけで
採点が壊れるので、「形は編集禁止」。

ここで使われている**Pydantic**というライブラリについて、まず基本から説明する。素のPythonクラスは、フィールドに何を代入しても文句を言わない——`step: int`と書いてあっても、実際には`step = "abc"`のような文字列を入れてもエラーにはならない（型注釈はあくまで人間やエディタへのヒントで、実行時には強制されない）。Pydanticの`BaseModel`を継承したクラスは違う: インスタンスを作る瞬間に、宣言された型と実際に渡された値を**実行時に照合**し、合っていなければ即座に例外を投げる。つまり「このフィールドは必ず整数でなければならない」「このフィールドは必須で、省略はできない」といったルールを、コメントで説明する代わりに**コードそのものとして強制**できる——これが「型契約」と呼ばれる所以で、`models.py`が「moulinette(採点システム)とエージェントの間の約束事」を表現する手段として選ばれている理由でもある。

全86行、5つの `BaseModel`（`StepMetrics`: 9-26行目、
`SolutionOutput`: 29-47行目、`SandboxConfig`: 50-60行目、`MBPPTaskInput`: 63-72行目、
`SWEBenchTaskInput`: 75-85行目）に役割は3種類しかない: moulinette→エージェントの
**入力契約**（`MBPPTaskInput`/`SWEBenchTaskInput`）、エージェント内部だけで完結する**設定**
（`SandboxConfig`）、エージェント→moulinetteの**出力契約**（`SolutionOutput`、その内訳としての
`StepMetrics`）。

地味だが徹底されている点として、**5クラス86行のほぼ全フィールドが `Field(..., description=...)`
または `Field(default=..., description=...)` の形で書かれている**（必須フィールドは
`Field(...)`、省略可能なら`default`/`default_factory`付き）。この`Field(...)`という書き方についても補足しておく: Pydanticでは単に`step: int`と書くだけでも動くが、そのフィールドに説明文やデフォルト値、生成ルールを追加で持たせたい場合に`Field(...)`という関数を使う。`Field(...)`の第一引数が三点リーダー`...`（Pythonの`Ellipsis`という特殊な値）になっているものは「デフォルト値なし＝必須フィールド」を意味し、`Field(default="")`や`Field(default_factory=lambda: ...)`になっているものは「省略された場合はこの値・この関数の戻り値を使う」という意味になる（`default_factory`は、リストや辞書のように「複数のインスタンスで使い回すと危険な可変オブジェクト」をデフォルト値にする際、呼び出すたびに新しく作り直すために使われる）。単なる型注釈ではなくフィールドごとに
自然文の説明が付与されているのは、このファイルがmoulinette側で人間が読むドキュメントとしても
JSON Schema（後述のSection 8.3で説明するMCPツールの引数記述にも同じ考え方が出てくる、「型と説明文の組み合わせを機械可読な形式で表現したもの」）としても機能することを想定した設計であることの表れ——このファイル自体が
「エージェント開発者向けの唯一の正式な契約書」になっている。`SandboxConfig.max_output_chars`
の説明文には `"project-internal, not required by the moulinette"` という一文が埋め込まれており
（60行目）、モデル定義の中に「このフィールドだけは採点契約の一部ではなく実装都合のものだ」
という注記がそのまま残っている——ドキュメントを別に書かなくても、スキーマを読むだけで
契約の境界線が分かるようになっている。

### `StepMetrics`（1イテレーション分の記録、9-26行目）

```python
class StepMetrics(BaseModel):
    step: int
    input_tokens: int
    output_tokens: int
    request_time_ms: float
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat())
    api_url: str = ""
    model_name: str = ""
    llm_output: str = ""      # コード抽出前のLLM生テキスト全文
    sandbox_input: str = ""   # 抽出後、実際にサンドボックスへ送ったコード
    sandbox_output: str = ""  # サンドボックスの実行結果（stdout/stderr/エラー文字列）
    retries: int = 0
```

`orchestrator.py:191-204` が1イテレーションごとにこれを1個組み立てて `steps` に積む:

```python
steps.append(
    StepMetrics(
        step=step_number,
        input_tokens=gen.input_tokens,
        output_tokens=gen.output_tokens,
        request_time_ms=gen.request_time_ms,
        api_url=gen.api_url,
        model_name=gen.model_name,
        llm_output=gen.text,          # ← 生テキストそのもの、無加工
        sandbox_input=sandbox_input,  # ← extract_code(gen.text) で抜き出したコード部分だけ
        sandbox_output=observation,
        retries=gen.retries,
    )
)
```

`llm_output` と `sandbox_input` は同じLLM応答から来ているが中身は別物: `llm_output` は
`Thought: ...\nCode:\n```python\n...\n```\n<end_code>` という **Thought込みの全文**、
`sandbox_input` はそこから ```` ```python ... ``` ```` ブロックだけを`code_extraction.py`が
抜き出した**コード部分のみ**。両方残すのは、あとから「LLMがちゃんとThoughtを書いていたか」
「コード抽出は正しく機能したか」を個別に検証できるようにするため。

### `SolutionOutput`（タスク全体の最終成果物）

`StepMetrics` が「1ステップ」なのに対し、こちらは「タスク1つ分のサマリ + 全ステップ履歴」。
`agent_mbpp.py` / `agent_swebench.py` がこれを組み立てて `solution.json` に書き出し、
moulinette がそれを読んで正誤判定する。

```python
class SolutionOutput(BaseModel):
    task_id: str
    benchmark: str          # "mbpp" or "swebench"
    success: bool           # エージェントが final_answer() を呼んだかどうか（正解の証明ではない）
    solution: str            # MBPP: 関数コード / SWE-bench: git diff
    iterations: int
    total_requests: int      # リトライ込みのLLM APIコール総数
    total_input_tokens: int
    total_output_tokens: int
    total_time_seconds: float
    steps: List[StepMetrics] = []
    system_prompt: str = ""  # 実際に送った完全なsystem prompt（provenance検証用）
    error: Optional[str] = None
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat())
```

`total_input_tokens` などは理屈上 `steps` を合計すれば導出できる値だが、あえてトップレベルにも
持たせているのは、moulinette が集計処理をせずに一発で予算超過チェックできるようにするため
（詳細な監査は `steps` 側、合計値チェックはトップレベル側、という役割分担）。`success: true` の
意味が「エージェントが `final_answer()` を呼んだ」でしかなく「本当に正しい」ではないことは、
Section 17 のアブレーション実験（空文字列パッチで `success: true` を報告した事故）で
実証されている。

### `SandboxConfig`（サンドボックスの門番設定）

```python
class SandboxConfig(BaseModel):
    authorized_imports: List[str] = []      # importのホワイトリスト（"math.*" のようなglob可）
    allowed_directories: List[str] = []      # ファイルアクセスを許可するパス
    max_execution_time_seconds: int = 30
    max_memory_mb: int = 512
    max_output_chars: int = 20_000           # プロジェクト内部の都合。moulinette契約外
```

`authorized_imports` はデフォルト**拒否**方式 — ここに無い名前は `import` した瞬間ブロックされる。「デフォルト拒否（allowlist、許可リスト方式）」とは、「明示的に許可したものだけが通り、それ以外は自動的にすべて禁止」という考え方で、「明示的に禁止したものだけがダメで、それ以外はすべて許可」という逆方向の「デフォルト許可（blocklist、拒否リスト方式）」より安全側に倒れている——新しい危険なモジュールが将来追加・発見されても、ホワイトリストに載せない限り自動的にブロックされ続けるため。
実際の値は `agent_mbpp.py` と `agent_swebench.py` で明確に差がある:

| | `agent_mbpp.py:107-116` | `agent_swebench.py:102-107` |
|---|---|---|
| `allowed_directories` | `[SCRATCH_DIR]` のみ | `["/testbed", SCRATCH_DIR]` |
| `max_execution_time_seconds` | `20` | `60` |
| `max_memory_mb` | `256` | `512` |

MBPPは短いアルゴリズム問題しか解かないのでスクラッチディレクトリしか触らせずリソースも絞る。
SWE-benchはDockerでマウントされた実リポジトリ（`/testbed`）を調査・修正する必要があるため、
アクセス範囲もリソースも倍増させている。`agent_mbpp.py` 側のタイムアウト設定には
実運用で踏んだ地雷の跡がコメントで残っている:

```python
# Must stay comfortably above mcp_tools_mbpp.py's own internal 10s
# run_tests() subprocess timeout, or the outer sandbox alarm can fire
# first on a legitimate (slow but correct) test run - found via a live
# smoke test against a real provider, see README.md.
max_execution_time_seconds=20,
```

`mcp_tools_mbpp.py` 内部の `run_tests()` サブプロセスタイムアウトが10秒なので、外側の
サンドボックスタイムアウトを20秒にして、内側が確実に先にタイムアウトするよう余裕を持たせている
——これが無いと、正しいが少し遅いテスト実行を外側のアラームが先に殺してしまい、正解を誤って
タイムアウト扱いにする。

### `MBPPTaskInput` / `SWEBenchTaskInput`（moulinetteからの入力契約）

`SolutionOutput` の逆方向。moulinetteがエージェントに投げてくるタスク定義そのもの。

```python
class MBPPTaskInput(BaseModel):
    task_id: int
    task_definition: str        # 自然言語の問題文
    function_definition: str    # 例: "def solve(nums):"
    test_imports: List[str] = []
    test_list: List[str] = []   # 公開テストのassert文そのもの

class SWEBenchTaskInput(BaseModel):
    instance_id: str
    problem_statement: str      # 実際のGitHub issue文面
    docker_image: str           # 例: "swebench/sweb.eval.x86_64.sympy_1776_sympy-23534:latest"
    eval_script: str            # パッチ判定用bashスクリプト
    hints_text: str = ""
    repo: str = ""
```

`SWEBenchTaskInput` の各フィールドは単なるデータではなく、後続コードの**入力パラメータそのもの**
になる。`agent_swebench.py:97-99` を見ると分かりやすい:

```python
container = SweBenchContainer(task.docker_image)
container.start(eval_script=task.eval_script, tools_file=TOOLS_FILE)
```

`docker_image` はそのまま `docker pull` されるイメージ名、`eval_script` はそのままコンテナ内で
実行されるbashスクリプトになる——モデル定義を読んだだけで、それが後段のDockerライフサイクル
（Section 13）に直結していることまで追える。

---

## 5. `orchestrator.py` — エージェントループの心臓部

`Orchestrator.run(task_id, benchmark, task_prompt)` が1タスクを最後まで走らせ、`SolutionOutput` を返す。

このセクションを読む前に、繰り返し出てくる3つの前提知識を押さえておく。

- **トークン(token)**: LLM(大規模言語モデル)はテキストをそのまま1文字ずつ処理するのではなく、「トークン」という単位に分割してから処理する。1トークンはおおよそ数文字〜1単語程度に相当することが多いが、厳密な対応はモデルごとのトークナイザ次第で、バイト数や文字数とは別物である。LLM APIの課金や利用上限(このプロジェクトでは`max_input_tokens`/`max_output_tokens`)は、このトークン数を単位に数えられる。以降「入力トークン予算」「出力トークン予算」という言葉が繰り返し出てくるが、これは「LLMに送るテキスト(入力)」「LLMが生成するテキスト(出力)」それぞれについて、使ってよいトークン数の上限を指す。
- **`time.monotonic()`**: OSの「壁時計」(いわゆる現在時刻、`datetime.now()`など)は、NTPによる時刻同期やユーザーの手動変更で不連続に飛ぶことがある。`time.monotonic()`はそれとは別の、システム起動からの経過を単調に増加し続けるだけの時計で、時刻の巻き戻り・飛びが原則として起こらない。「経過時間を測る」という用途に限れば、こちらの方が安全——`start = time.monotonic()`を記録し、あとで`time.monotonic() - start`を計算すれば、途中でシステム時計が調整されても影響を受けない経過時間が得られる。
- **UNIXシグナル(SIGTERM/SIGKILL)**: OSがプロセスに送る、非同期の「割り込み通知」。`SIGTERM`は「お願いベース」の終了要求で、プロセス側は`signal.signal(...)`でハンドラ関数を登録し、通知が来たときに任意の後始末コードを挟んでから終了できる。一方`SIGKILL`はOSレベルで即座にプロセスを強制終了させる命令で、プロセス側は一切関与できず、`finally`節すら実行されない。このセクションで説明する`ShutdownRequested`の設計は、この2つのシグナルの性質の違いに直接由来している。

### ループの1イテレーションの流れ

```python
for step_number in range(1, max_iterations + 1):
    # 0. 停止条件チェック(SIGTERM要求／時間予算／入力トークン予算／出力トークン予算)
    # 1. 次のリクエストで超過しないか「送信前に」見積もる(後述)
    # 2. LLMClient.generate() を呼ぶ(stop=["<end_code>"])
    # 3. code_extraction.extract_code() でコード抽出
    # 4. コードが無ければ note をそのまま Observation にする
    # 5. あれば Sandbox.run() で実行 → 結果 or FinalAnswer 例外
    # 6. StepMetrics を記録
    # 7. FinalAnswer が来ていればループを抜けて成功終了
    # 8. そうでなければ assistant/user メッセージを追記して次へ
```

### `run()` の実装を行単位で追う(`orchestrator.py:101-235`)

```python
for step_number in range(1, self.config.max_iterations + 1):        # 118
    if self._stop_requested:                                        # 120  停止要求済みなら即break
        error = "stopped: shutdown requested (e.g. SIGTERM)"; break
    elapsed = time.monotonic() - start                               # 123  経過時間を計測
    if elapsed >= self.config.max_time_seconds: ...; break            # 124  時間予算チェック
    if total_input_tokens >= self.config.max_input_tokens: ...; break # 127  累積入力トークンチェック
    if total_output_tokens >= self.config.max_output_tokens: ...; break # 133 累積出力トークンチェック

    current_message_bytes = _serialized_message_bytes(messages)       # 140  送信予定メッセージのバイト数
    input_token_bound = _conservative_input_token_bound(...)          # 141  次リクエストの安全側トークン見積もり
    if input_token_bound > remaining_input_tokens: ...; break         # 147  「送信したら超過する」を事前に検知

    request_output_limit = min(config.max_tokens_per_request,
                                remaining_output_tokens)               # 156  1リクエストの出力上限を動的に絞る

    try:
        gen = self.llm_client.generate(messages,
                  stop=config.stop_sequences,
                  max_output_tokens=request_output_limit)             # 162  実際のLLM呼び出し
    except AllProvidersExhaustedError as exc:
        total_requests += exc.attempted_requests; error = ...; break  # 167  失敗しても試行回数は必ず加算
    except ShutdownRequested as exc:
        error = str(exc); break                                      # 171  LLM呼び出し中のSIGTERMをここで捕捉

    total_requests += 1 + gen.retries                                 # 175  本試行+リトライぶんを加算
    ...
    extraction = extract_code(gen.text)                               # 181  コード抽出
    if extraction.code is None:
        observation = extraction.note                                # 186  抽出失敗 → note をそのままObservationに
    else:
        try:
            sandbox_output = self.sandbox.run(extraction.code)        # 189  サンドボックス実行
            observation = f"{note}\n{sandbox_output}" if note else sandbox_output
        except FinalAnswer as fa:
            final_answer_raised = fa                                  # 194  final_answer() はここでだけ捕捉
            observation = f"[FinalAnswer submitted] {fa.answer!r}"

    steps.append(StepMetrics(...))                                    # 197  ここまでの全情報を1ステップとして記録

    if final_answer_raised is not None:
        success = True; solution_text = str(final_answer_raised.answer); break  # 212
    messages.append({"role": "assistant", "content": gen.text})       # 217  会話履歴を更新
    messages.append({"role": "user", "content": f"Observation:\n{observation}"})  # 218
else:
    error = f"max iterations reached ({self.config.max_iterations})" # 219-221  forループがbreakされず完走した場合
```

ポイントは次の3つ:

1. **8つの停止条件チェック(0〜7)が毎イテレーションの先頭で毎回すべて評価される**こと。
   `_stop_requested` チェック(118-122行目)はポーリング的な保険であり、実際にSIGTERMが
   ループの外(LLM呼び出し中やサンドボックス実行中)で届いた場合の主経路は後述の
   `ShutdownRequested` 例外の方である。
2. **`for...else`構文の利用**(219-221行目): まずこの構文自体があまり馴染みがないかもしれないので説明しておく——Pythonの`for`ループには`else`節を付けられ、これは「ループが`break`で**途中終了しなかった**場合(＝イテレータを最後まで自然に使い切った場合)にだけ実行される」という、他の言語にはあまり見ない機能。裏を返せば、ループのどこかで`break`が実行されればその時点で`else`節は**スキップ**される。ここでの`break`されるケース(成功・各種予算超過・SIGTERM)はすべて`else`をスキップするので、`else`節に「その他の終了理由」＝「上限イテレーション到達」だけが自然に残る。専用のフラグ変数(例えば`reached_max_iterations = True`のような変数を用意して最後にチェックする、という書き方)を使わずに済ませる、Pythonらしいイディオム。
3. **223-224行目の最終セーフティネット**: `success`も`error`もどちらもセットされずにループを
   抜けるケースは理論上ほぼ起こらないはずだが(8つの`break`経路と`for...else`ですべて
   カバーされている)、万一のロジック漏れがあっても`SolutionOutput.error`が空文字列のまま
   moulinetteに渡ってしまう事故を防ぐための、意図的な多重防御。

### 設計上の工夫

- **stop sequence `<end_code>` の必然性**: まず「stop sequence」というLLM API共通の概念を説明しておく——多くのLLM APIには、生成中のテキストが指定した特定の文字列と一致した瞬間に、そこで生成を打ち切ってレスポンスを返す、という機能がある。この文字列のことをstop sequenceと呼ぶ。`config.stop_sequences`にデフォルトで入っている`<end_code>`がまさにこれで、LLMが1つのCodeブロックを書き終えた合図として使われている。これがないと、モデルが本物のツール実行結果を
  待たずに、次のObservationを幻覚して自分で書き続けてしまう危険がある(README.md Section 4.6 のtip)。
- **コードブロックが無い場合の扱い**: `[NoCodeBlock] ...` をそのまま次のObservationとして
  LLMに見せる。ループが「たぶんこうだろう」と推測することは絶対にしない。これは
  Section 4.1 の「明示的フィードバック必須」要件を体現している。
- **`FinalAnswer` は特別扱い**: `sandbox.run()` 内の汎用 `except Exception` では
  絶対に握りつぶさない。`final_answer()` はサンドボックスの制御フロー用シグナルであり、
  `KeyboardInterrupt`/`SystemExit` と同格に扱われる。
- **保守的なトークン予算の事前チェック** (`_conservative_input_token_bound`,
  `orchestrator.py:54-71`):
  実際にリクエストを送ってから「トークン超過でした」と分かるのではなく、送信予定メッセージの
  UTF-8バイト長から**送信前に**上限を見積もり、予算超過が予想されるなら未然にループを止める。

  ```python
  if previous_message_bytes is None or previous_input_tokens is None:
      return current_message_bytes + 32          # 初回: バイト数 + 固定の余裕32
  added_bytes = max(0, current_message_bytes - previous_message_bytes)
  return previous_input_tokens + added_bytes + 16  # 2回目以降: 前回実測 + 増分バイト + 余裕16
  ```

  なぜ「初回はバイト数、2回目以降は実測トークン数ベース」で式が変わるのか: 1トークンは
  平均して1バイトより長い(英語で概ね3〜4バイト/トークン)ため、バイト数をそのままトークン数の
  上限として使うのは常に安全側(トークン数を大きく見積もりすぎ)に倒れる。初回はまだ
  プロバイダから実測トークン数をもらっていないので、この安全側バイト数を仕方なく使う。
  2回目以降は前回のレスポンスに含まれる実測`input_tokens`(正確な値)を土台にし、
  「今回追加された分」のバイト数だけをワーストケース換算で足す。こうすることで、
  会話履歴が伸びるたびに履歴全体をバイト単位で再見積もりする無駄(＝予算チェックが
  過度に保守的になり、本来まだ余裕があるのに早期に打ち切ってしまう事態)を避けている。

- **`ShutdownRequested` は `BaseException`、しかも例外は「シグナルハンドラの中で即座に」
  送出される**(`orchestrator.py:21-34, 91-99`、`agent_mbpp.py:91-99` /
  `agent_swebench.py:91-99` と対):

  まず一般的な前提として、Pythonの例外クラスには階層がある。ほぼすべての「プログラムのエラー」を表す例外(`ValueError`、`KeyError`、`TypeError`など)は`Exception`というクラスを継承しており、`except Exception:`という書き方は「通常起こりうるエラーは一通り拾う」という意味で広く使われる。一方`Exception`自身は、さらに上位の`BaseException`というクラスを継承している。`KeyboardInterrupt`(Ctrl+Cによる割り込み)や`SystemExit`(`sys.exit()`)のような「プログラムの実行そのものを打ち切りたい」特別な信号は、あえて`Exception`ではなく`BaseException`を**直接**継承するよう設計されている——これにより、コード中に大量にある`except Exception:`の網に引っかからず、必ず外側まで伝播する。`ShutdownRequested`がわざわざ`BaseException`を継承しているのは、まさにこの性質(`Exception`ベースの汎用catch節を素通りする)を借りるため。

  よくある誤解は「SIGTERMが届いたら`_stop_requested`フラグが立ち、ループが次にそれを
  チェックしたタイミングで気づいて止まる」というポーリング的なイメージだが、実際の主経路は
  それとは別にある。CLI側(`agent_mbpp.py:93-99`)で登録されるシグナルハンドラを見ると分かる:

  ```python
  orchestrator: Optional[Orchestrator] = None

  def handle_sigterm(signum: int, frame: object) -> None:
      if orchestrator is None:
          raise ShutdownRequested("shutdown requested (e.g. SIGTERM)")  # ループ開始前
      orchestrator.request_stop()

  signal.signal(signal.SIGTERM, handle_sigterm)
  ```

  Pythonのシグナルハンドラは、SIGTERMが実際に届いた瞬間に**そのときプロセスが実行していた
  任意のPythonバイトコードの合間に割り込んで**呼び出される。「ポーリング」(ループが定期的に「止まれと言われていないか」を自分から確認しに行く方式)とは対照的に、これは「OS側からの通知がPython側の実行を強制的に中断させる」割り込み駆動の方式——ループの次のチェックポイントまで待つ必要が一切ない。つまり`handle_sigterm`は
  「LLM APIへの`requests.post()`がブロックしている最中」でも「サンドボックス内でLLM生成コードが
  実行されている最中」でも、任意のタイミングで発火しうる。`Orchestrator`インスタンスが
  まだ生成されていなければ(起動シーケンスの初期段階でSIGTERMが来た場合)その場で直接
  `ShutdownRequested`を送出し、生成済みなら`orchestrator.request_stop()`(`orchestrator.py:91-99`)
  を呼ぶ:

  ```python
  def request_stop(self) -> None:
      self._stop_requested = True
      raise ShutdownRequested("shutdown requested (e.g. SIGTERM)")
  ```

  `request_stop()`自身も、フラグを立てるだけでなく**その場で即座に`raise`する**。つまり
  「フラグを立てて後で気づかせる」のではなく、「シグナルが届いたPythonの実行ポイントに
  例外を注入して、今まさに実行中の呼び出しを強制的に巻き戻す」という設計。これが
  `Exception`ではなく`BaseException`を継承している理由に直結する:
  `Sandbox.run()`内部の汎用`except Exception`や、`requests`/`urllib3`が内部で使う
  幅広いtry/exceptに、この割り込み例外が誤って捕捉されて握りつぶされてはならない
  (`sandbox/executor.py`が`SandboxTimeoutError`を`SIGALRM`ハンドラから同じ発想で
  送出しているのと同一の技法)。`Orchestrator.run()`側では`except ShutdownRequested`
  (171-173行目)がLLM呼び出しを包む`try`のすぐ外側に置かれ、素通りしてきた例外を
  ここで初めて捕捉して通常の`error`セット→`break`の経路に合流させる。

  この即時中断が必要な理由はmoulinetteの運用制約にある: 外部ハーネスはSIGTERMを送った後、
  プロセスが自発的に終了しなければ**約10秒後にSIGKILLを送る**。SIGKILLはOSレベルで
  プロセスを即座に強制終了させるため、Python側のコードは`finally`節すら実行する猶予がない。
  `agent_swebench.py`は`finally: container.cleanup()`でDockerコンテナの後始末をしなければ
  ならないので、「SIGTERMを受けてから10秒以内に、今実行中の処理を抜けて`finally`ブロックまで
  到達する」ことが必須になる。ポーリングでフラグを見るだけの設計だと、ループの次の
  チェックポイントまで到達するのにLLM呼び出しやサンドボックス実行の残り時間(最大で数十秒)
  がかかってしまい、10秒の猶予に間に合わない可能性がある。シグナルハンドラの中で
  即座に例外を送出する設計は、この時間制約を満たすための直接的な必然性から来ている。

### 終了時に返す `SolutionOutput`

`success`/`solution`/`iterations`/`total_requests`/`total_input_tokens`/`total_output_tokens`/
`total_time_seconds`/`steps`/`system_prompt`/`error` をすべて埋めて返す。
失敗時(例外、タイムアウト、予算超過、上限イテレーション到達)でも空の `SolutionOutput` ではなく、
**そこまでの steps を含んだ** `SolutionOutput` を返す設計になっている。`iterations=len(steps)`
(`orchestrator.py:231`)である点も見落としやすい ── これは「ループが何周走ろうとしたか」では
なく「実際に`StepMetrics`が1件でも記録されたステップの数」であり、例えば最初のLLM呼び出しが
`AllProvidersExhaustedError`で失敗した場合は`steps`が1件も積まれないまま`iterations=0`で
終了する(それでも`total_requests`だけは`exc.attempted_requests`ぶん加算されている、
という非対称性はSection 9.2で扱う`AllProvidersExhaustedError`の設計と対になっている)。

---

## 6. `code_extraction.py` — フォーマット非依存化レイヤー

LLM ごとにツール呼び出しの書き方の癖が異なる。このモジュールは、あらゆる形式を
**サンドボックスが実行できる等価な Python コード文字列**に変換してから渡す。
これによりサンドボックス自体は完全にフォーマット非依存でいられる。176行の小さなファイルで、
中身は正規表現5個 + 変換ヘルパー2個 + 形式別抽出関数3個 + それを順に試す `extract_code()` 1個だけ。

### 使う正規表現(優先順位そのままの定義順、`code_extraction.py:17-32`)

コードを見る前に、正規表現(regex)側の記法を3つだけ確認しておく。

- **`re.DOTALL`フラグ**: 通常、正規表現の`.`(任意の1文字)は改行文字`\n`にはマッチしない。`re.DOTALL`を付けると`.`が改行も含めたあらゆる文字にマッチするようになる——LLMの出力は複数行にまたがるコードブロックなので、これを付けないと1行目しか拾えない。
- **`(?:...)`という書き方**: 通常の`(...)`は「グループ化」と「あとで`match.group(N)`として取り出せるようにキャプチャする」の両方を兼ねるが、`(?:...)`は前者(グループ化して`|`(OR)や繰り返しをまとめる)だけを行い、キャプチャはしない。取り出す必要のない部分をこう書くことで、`match.group(1)`のような番号が意図通りの箇所を指すようにできる。
- **貪欲(greedy)マッチと非貪欲(non-greedy)マッチ**: `.*`は既定で「マッチできる限り長く」文字列を食う(貪欲)。末尾に`?`を付けた`.*?`は逆に「マッチできる限り短く」で止まる(非貪欲)。次の`_PYTHON_FENCE_RE`が`(.*?)`を使っているのは、コードフェンスの中身を「最初に現れた閉じタグの直前まで」で止めたい(貪欲だと、応答の中に複数のコードブロックがある場合に不必要に長く食ってしまう)ため。

```python
_PYTHON_FENCE_RE   = re.compile(r"```python\s*\n(.*?)(?:```|<end_code>)", re.DOTALL)   # 17
_GENERIC_FENCE_RE  = re.compile(r"```(?:\w+)?\s*\n(.*?)```", re.DOTALL)                 # 19
_UNCLOSED_FENCE_RE = re.compile(r"```python\s*\n(.*)$", re.DOTALL)                      # 21
_XML_INVOKE_RE     = re.compile(r'<invoke\s+name="([^"]+)">(.*?)</invoke>', re.DOTALL)  # 24
_XML_PARAM_RE      = re.compile(r'<parameter(?:\s+name="([^"]+)")?>(.*?)</parameter>', re.DOTALL)  # 26
_JSON_TOOLCALL_RE  = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)   # 29
_REACT_RE          = re.compile(r"Action:\s*(\S+)\s*\nAction Input:\s*(\{.*?\}|\S.*)", re.DOTALL)  # 32
```

`_PYTHON_FENCE_RE` が `(?:```|<end_code>)` の**どちらでも**閉じられるのがポイント:
モデルが `<end_code>` を書き忘れて ``` ``` ``` だけで閉じても、逆に ``` ``` ``` を忘れて
`<end_code>` だけ書いても、どちらも正常系として拾える。`_GENERIC_FENCE_RE` は言語指定
(```` ```json ```` など)の有無を問わない代わりに閉じフェンス必須、`_UNCLOSED_FENCE_RE` は
逆に`python`指定必須の代わりに閉じフェンス不要(`$`まで食う)という、互いに補い合う正規表現に
なっている。

### `ExtractionResult` — 抽出結果と「注記」の運搬役(`code_extraction.py:35-48`)

コードの前に`@dataclass`という書き方について: これはPython標準の`dataclasses`モジュールが提供するデコレータで、クラス本体にフィールド名と型だけを並べて書くと、コンストラクタ(`__init__`)・等価比較(`__eq__`)・見やすい文字列表現(`__repr__`)などを自動生成してくれる。素の`class`で同じことをすると`__init__`を手書きする必要があるが、`@dataclass`を付けるだけで「値をいくつか束ねて持ち運ぶだけの入れ物」を数行で書ける——この`ExtractionResult`のような「処理結果をまとめて返すだけの型」に向いている書き方。

```python
@dataclass
class ExtractionResult:
    code: Optional[str]  # 実行すべきPythonコード文字列。抽出失敗時はNone
    note: str            # 抽出処理の説明・注記(空文字列の場合もある)
```

`note`は単なるログではなく、**Orchestratorが次のObservationの先頭にそのまま連結してLLMへ
返す文字列**(Section 5, `orchestrator.py:186, 191`)。つまり`code_extraction.py`は
「コードを抜き出す」だけでなく「抜き出す過程で何が起きたかをLLMに直接語りかける」という
2つ目の役割を負っている。

### 変換のコア: `_call_from_kwargs()` と `_py_literal()`(`code_extraction.py:51-69`)

```python
def _py_literal(value: str) -> str:                       # 51
    stripped = value.strip()
    try:
        return repr(json.loads(stripped))                 # 55  JSONとして解釈できればその型のPython repr
    except (json.JSONDecodeError, TypeError):
        return repr(value)                                 # 57  解釈できなければ元の文字列のまま

def _call_from_kwargs(name: str, kwargs: dict, parse_string_literals: bool = False) -> str:  # 60
    parts = []
    for key, value in kwargs.items():
        if parse_string_literals and isinstance(value, str):
            parts.append(f"{key}={_py_literal(value)}")    # 65  XML経由のみ: 型の再解釈を試みる
        else:
            parts.append(f"{key}={value!r}")                # 67  JSON/ReAct経由: 既に型が付いた値をそのままrepr
    return f"result = {name}({', '.join(parts)})\nprint(result)"  # 69
```

ここに **XML形式だけが特別扱いされる非対称性**がある。XMLの`<parameter>`はワイヤ形式として
テキストしか運べない(`<parameter name="start_line">1</parameter>`の`1`は文字列`"1"`でしか
ない)ため、`_extract_xml_invoke()`は`parse_string_literals=True`を渡し、`_py_literal()`に
「`json.loads`で数値・真偽値として読めるなら読み直す」救済を行わせる。一方JSON/Hermesの
`<tool_call>`とReActの`Action Input`はどちらも中身がJSON文字列であり、`json.loads()`した
時点で**既に正しい型**(数値なら`int`/`float`、文字列なら`str`)が付いている。ここで
もし`parse_string_literals=True`を使ってしまうと、`"123"`という**文字列としての引数**が
`_py_literal()`によって数値`123`に**誤って再解釈**されてしまう——これを防ぐために
`_extract_json_tool_call()`と`_extract_react()`は`parse_string_literals`を指定しない
(デフォルトの`False`のまま`_call_from_kwargs()`に渡す)。この非対称性は
`tests/test_code_extraction.py`の`test_json_tool_call_preserves_string_argument_types`
(`{"pattern": "123", "file_pattern": "false"}` → `pattern='123', file_pattern='false'`と
**文字列のまま**変換されることを確認)と`test_react_format_preserves_json_string_argument_types`
(`{"pattern": "null"}` → `pattern='null'`と、JSON特殊値`null`ではなく文字列`"null"`のまま
であることを確認)の2つのテストで直接ロックされている。

### `extract_code()` が試す順序と、それぞれの変換結果(`code_extraction.py:117-175`)

`extract_code(llm_output: str) -> ExtractionResult` が上から順に試し、最初にヒットした形式を使う。

```python
match = _PYTHON_FENCE_RE.search(llm_output)                          # 125
if match:
    return ExtractionResult(code=match.group(1).strip(), note="")    # 127  プライマリ形式、note=""

unclosed = _UNCLOSED_FENCE_RE.search(llm_output)                     # 130
if unclosed:
    return ExtractionResult(code=unclosed.group(1).strip(), note="[MalformedCodeBlock] ...")  # 132-138

for extractor, format_name in (                                      # 141
    (_extract_xml_invoke, "XML <invoke> tool call"),
    (_extract_json_tool_call, "JSON/Hermes <tool_call>"),
    (_extract_react, "ReAct Action / Action Input"),
):
    code = extractor(llm_output)
    if code:
        return ExtractionResult(code=code, note=f"[FormatConverted] Response used a {format_name} "
                                 "format; converted to an equivalent Python call before execution.")  # 148-154

generic = _GENERIC_FENCE_RE.search(llm_output)                       # 157
if generic:
    return ExtractionResult(code=generic.group(1).strip(), note="[MalformedCodeBlock] ...")  # 159-165

return ExtractionResult(code=None, note="[NoCodeBlock] ...")         # 168-175
```

1. **正しく閉じた ```` ```python ... ``` ```` フェンス** — プライマリ形式。`note=""`(空文字列、
   `None`ではない点に注意——Orchestrator側は`if extraction.note:`のような真偽判定ではなく
   常に`note`の有無を文字列として扱う)でそのまま抽出。変換なし。
   `tests/test_code_extraction.py::test_closed_python_fence`がこの経路を固定している。
2. **閉じられていない ```` ```python ```` フェンス**(`_UNCLOSED_FENCE_RE`)— 救済策。
   残り全部をコードとして使い、`note`に
   `[MalformedCodeBlock] The ```python fence was never closed with ``` or <end_code>; the
   rest of the response was used as the code anyway.`を付ける
   (`test_unclosed_python_fence_is_salvaged`)。
3. **XML `<invoke>`** — `_extract_xml_invoke()`(`code_extraction.py:72-83`)が
   `<parameter name="...">...</parameter>`を`_XML_PARAM_RE.finditer()`で1つずつ拾い、
   `name`属性がなければ`arg0`, `arg1`, ...という仮名を振りつつ`kwargs`辞書に詰め、
   `_call_from_kwargs(name, kwargs, parse_string_literals=True)`で等価コードに変換する。
   `test_xml_invoke_is_converted`が使う具体例:

   ```xml
   <invoke name="read_file">
     <parameter name="filepath">/testbed/a.py</parameter>
     <parameter name="start_line">1</parameter>
   </invoke>
   ```

   は次のPythonコードに変換される(`filepath`は文字列のまま、`start_line`は`_py_literal()`が
   `"1"`を`json.loads`で読み直して整数`1`にする):

   ```python
   result = read_file(filepath='/testbed/a.py', start_line=1)
   print(result)
   ```

   `note`には`[FormatConverted] Response used a XML <invoke> tool call format; converted to
   an equivalent Python call before execution.`が付く。
4. **JSON/Hermes `<tool_call>{"name": ..., "arguments": {...}}</tool_call>`** —
   `_extract_json_tool_call()`(`code_extraction.py:86-99`)がJSONとしてパースし、
   `name`が無い・`arguments`が辞書でない・JSONとして壊れている、のいずれかなら黙って`None`を
   返し次の形式へフォールバックする。`test_json_tool_call_is_converted`の例:

   ```
   <tool_call>{"name": "search_code", "arguments": {"pattern": "foo"}}</tool_call>
   →  result = search_code(pattern='foo')
      print(result)
   ```

5. **ReAct `Action: name\nAction Input: {...}`** — `_extract_react()`(`code_extraction.py:102-114`)
   が`Action Input`をJSONとしてパース。辞書でなければ(例えば`Action Input: 42`のように裸の値
   の場合)`{"value": raw_input}`として1引数関数呼び出し扱いにするフォールバックまで
   用意されている。`test_react_format_is_converted`の例:

   ```
   Action: list_files
   Action Input: {"directory": "/testbed"}
   →  result = list_files(directory='/testbed')
      print(result)
   ```

6. **末尾の保険**: 上記いずれにも当たらなければ `_GENERIC_FENCE_RE` で最初の汎用フェンス
   (```` ```json ```` や ```` ``` ```` だけの無言語指定フェンスなど)を最後の手段として使う。
   `note`は`[MalformedCodeBlock] No ```python fence found; used the first generic fenced
   block instead.`
7. **それでも何も見つからなければ** `code=None` を返し、`note`に
   `[NoCodeBlock] No valid Python code block or recognized tool-call format ... was found in
   the model's response. Reply with a \`\`\`python ... \`\`\` block ending in <end_code>.`
   という、次に何を書けばいいかまで指示する明示的なフィードバックを付ける
   (`test_no_code_block_found`)。

`code=None` になったケースは `orchestrator.py:185-186` で「サンドボックス実行そのものを
スキップし、`note` をそのままObservationとして渡す」という分岐に落ちる。ループが
「たぶんこう直せばいいだろう」と推測して何かを実行することは絶対にない ── 常に
「何が起きたか／何を直すべきか」を明示的にLLMへ返す、という一貫した設計方針がここにも表れている。
なお優先順位が固定されているため、仮にモデルの応答が正しく閉じた ```` ```python ```` フェンスと
XMLの`<invoke>`タグを両方含んでいたとしても、**常にフェンスが優先**される——1番目に判定される
プライマリ形式は、それ単体で完結した`ExtractionResult`を即座に返して関数を抜けるため、
以降の形式は評価すらされない。

---

## 7. `prompts.py` — システムプロンプト構築

まず前提として、「システムプロンプト」とは何かを確認しておく。LLM APIに送るメッセージ列(会話履歴)には、通常のユーザー発言・アシスタント発言とは別に「システムメッセージ」という特別な役割の1通を先頭に置ける。ここには「あなたはこういう役割のアシスタントで、こういうルールに従って応答してください」という、対話全体を通じて効かせたい指示をまとめて書く。このプロジェクトでは、`FRAMEWORK_EXPLANATION`(ルール)・`sandbox_manual`(使える道具)・`final_answer`の使い方・worked exampleを1本のテキストに連結したものがシステムプロンプトになり、`Orchestrator.run()`(Section 5)が組み立てる`messages`リストの先頭(`role: "system"`)に置かれる。

`build_system_prompt(benchmark, sandbox_manual, include_example=True)` が、以下の4パーツを
文字列結合してシステムプロンプト全文を組み立てる。ファイル全体で130行、うち大半は
定数として定義された素のテキストブロックで、ロジックは末尾の関数1つだけ。冒頭のモジュール
docstring(`prompts.py:1-4`)が「明確なツールのドキュメント、構造化されたThought/Code/
Observationの各枠、そして効果的な推論ループの例」という3つの要素を明言しており、それが
そのまま後述のパーツ2・パーツ1・パーツ4に対応する。各定数の中身が英語のままなのは、
`FRAMEWORK_EXPLANATION`直前のコメント(`prompts.py:7`)に「文字列リテラルの中身は英語のまま、
モデルへの指示なので変更しない」と明記されている通り、これはLLMに送られる実データそのもの
だからである。

### パーツ1: `FRAMEWORK_EXPLANATION`(両ベンチマーク共通、丸ごと固定文字列、`prompts.py:8-33`)

```
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
```

「必ずキーワード引数で呼べ」という指示は**あくまで助言**でしかない — モデルが位置引数で
呼んできても壊れないように、実際の強制は `sandbox/mcp_client.py` の `_make_wrapper()` 側
(Section 8.3)で保証されている。ここに書かれているルールと、コード側の防御が
二重に噛み合っている一例。

見落としやすいが重要な点: **この`FRAMEWORK_EXPLANATION`のどこにも「ツール呼び出しの結果を
`print()`せよ」という指示は存在しない。** ルールが説明しているのは「Observationは次のターンまで
見えない」という**事実**だけで、「だからツール呼び出しは`result = tool(...); print(result)`という
形で書け」という**やり方**までは教えていない。この情報がどこから来るかはパーツ4で扱う。

### パーツ2: `sandbox_manual`(呼び出し元から注入される、動的パート)

`build_system_prompt()` 自身はツール名を1つも知らない。`sandbox_manual` 引数として
`MCPToolProxy.manual_text()`(Section 8.3)の出力をそのまま受け取り、
`## Available tools\n{sandbox_manual}\n\n` として埋め込むだけ。**別のMCPサーバーに繋ぎ変える
だけでこの部分も自動的に変わる**ため、「未知のMCPサーバーでテストされる」という課題要件に
そのまま対応できる。`manual_text()`はツール名・パラメータ名・型・必須/任意・説明文を
機械的に整形するだけであり、こちらにも「`print()`で結果を出力せよ」という運用上の指示は
含まれていない(Section 8.3参照)。

### パーツ3: `final_answer` の使い方(ベンチマークごとに完全に別文面、`prompts.py:35-52`)

```python
# prompts.py:36-45
_MBPP_FINAL_ANSWER = """\
Call final_answer(code) exactly once, where `code` is a string containing the
complete Python function that solves the task (matching the given function
signature). Example: final_answer("def add(a, b):\\n    return a + b")

As soon as run_tests(...) reports {"success": true}, call final_answer with
that exact code immediately in your NEXT turn - do not re-verify a solution
that already passed, and do not keep exploring alternatives. Every extra turn
spends part of your limited token and iteration budget.
"""

# prompts.py:48-52
_SWEBENCH_FINAL_ANSWER = """\
Call final_answer(get_patch()) exactly once, once you have verified your fix
with run_tests(). get_patch() returns the unified git diff of every change you
made to the repository - do not hand-write the patch yourself.
"""
```

`_SWEBENCH_FINAL_ANSWER` の「`get_patch()`を呼べ、自分でパッチを手書きするな」という一文は
地味だが重要 — LLMが差分を手で組み立てると、行番号やコンテキスト行のズレで `git apply`
不能な壊れたdiffになりがちなので、必ずツール(`git diff` の薄いラッパー、Section 12)を
経由させて機械的に正しいものだけを提出させている。

`_MBPP_FINAL_ANSWER` の「テストが通ったら次のターンで即 `final_answer` を呼べ、再検証するな」
という指示は、トークン予算・イテレーション予算をエージェント自身に節約させるための
プロンプトレベルのガードレール。

### パーツ4: worked example(`include_example=True` の場合のみ、`prompts.py:54-97`)

「worked example」(お手本例、いわゆるfew-shot example)とは、LLMに何かのやり方を教える際、**説明文で指示する代わりに、実際にその通りに書かれた具体例をそのまま見せる**という手法。人間に「こういう形式で書いてください」と口頭で説明するより、実物のサンプルを1つ渡すほうが速く正確に伝わることが多いのと同じ発想で、プロンプトエンジニアリングでも広く使われる。

MBPP用・SWE-bench用それぞれに、Thought→Code→Observationを1〜2ターン分そのまま書いた例が
定数として埋め込まれている。**この定数こそが、Observationとして印字すべき値をどう作るかを
モデルへ教える唯一の実演**になっている。MBPP用(`_MBPP_EXAMPLE`, `prompts.py:55-74`)を
全文見ると分かりやすい:

```
Example turn:

Thought: I'll write the function and check it against the public tests before submitting.
Code:
```python
code = "def add(a, b):\n    return a + b"
print(run_tests(code=code, test_list=["assert add(2, 3) == 5", "assert add(-1, 1) == 0"]))
```
<end_code>

Observation: {"success": true, "output": ""}

Thought: All public tests passed. I'm confident in this solution.
Code:
```python
final_answer("def add(a, b):\n    return a + b")
```
<end_code>
```

ここで実演されているのは3つのこと: (1) ツール呼び出し`run_tests(...)`を丸ごと`print(...)`で
包むという構文パターン、(2) `run_tests`が`{"success": true, ...}`というJSON文字列を返すこと、
(3) それを見た**次のターンで即座に**、同一のコード文字列で`final_answer(...)`を呼ぶという
`_MBPP_FINAL_ANSWER`の指示の実例。SWE-bench用(`_SWEBENCH_EXAMPLE`, `prompts.py:77-97`)も
同じ構造で、`search_function_or_class_definition_in_code(...)`と`read_file(...)`の2回の
呼び出しをどちらも`print(result)`で包んで見せている。

`include_example`フラグは本番の2つのCLIでは常に`True`で呼ばれるが、`BENCHMARK_REPORT.md`の
アブレーション実験(同じ関数を`include_example=False`で呼び、worked exampleだけを抜いた
「before」プロンプトを作る)のためにわざわざ引数化されている。パーツ1・パーツ2で確認した通り、
「ツール呼び出しの結果を`print()`で明示的に出力せよ」という指示は`FRAMEWORK_EXPLANATION`にも
`sandbox_manual`にもどこにも存在せず、**唯一この worked example だけがそれを実演している**。
したがって`include_example=False`にすると、モデルにとって「ツール呼び出し結果をどう
Observationに載せるか」を学ぶ手がかりが文字通りゼロになる。Section 17で触れる通り、実際に
この1引数の有無だけで「`success: true`の意味」が根本的に変わる実験結果が出ている ──
worked exampleが無いと、モデルはツール呼び出し結果を`print()`せずにコードを実行し続け
(例えば`run_tests(code=code, test_list=test_list)`とだけ書いて結果を変数に受けるだけで
出力しない、あるいは戻り値を無視してコードを書き進める)、`sandbox_output`が空文字列に近い
まま`final_answer()`を呼んでしまう。プロンプトのルールを読んだだけでは導けない、
「結果は`print()`しないとサンドボックスの標準出力に現れず、Observationとして戻ってこない」
という**サンドボックスの実行モデル特有の暗黙知**を、worked exampleだけが埋めている。

### 組み立て本体(`build_system_prompt()`, `prompts.py:100-129`)

```python
def build_system_prompt(benchmark: str, sandbox_manual: str, include_example: bool = True) -> str:
    if benchmark == "mbpp":                                              # 114
        final_answer_doc, example = _MBPP_FINAL_ANSWER, _MBPP_EXAMPLE
    elif benchmark == "swebench":                                        # 116
        final_answer_doc, example = _SWEBENCH_FINAL_ANSWER, _SWEBENCH_EXAMPLE
    else:                                                                 # 118
        raise ValueError(f"Unknown benchmark: {benchmark}")

    prompt = (                                                            # 122-126
        f"{FRAMEWORK_EXPLANATION}\n"
        f"## Available tools\n{sandbox_manual}\n\n"
        f"## Submitting your solution\n{final_answer_doc}\n"
    )
    if include_example:                                                   # 127
        prompt += f"## {example}"
    return prompt
```

未知の `benchmark` 文字列が来たら黙ってどちらかにフォールバックするのではなく即座に
`ValueError`(119行目)— システムプロンプトの中身を静かに間違えるくらいなら、起動直後に
はっきり落ちたほうがいい、という判断。組み立て順序自体も意味を持っている: 枠組み説明 →
ツール一覧 → 提出方法 → (あれば)worked example、という並びは、実際のThought→Code→
Observationループを読む順番そのままに「ルールを教え、道具を見せ、ゴールを示し、最後に
実演する」というチュートリアルの型を踏襲している。
---
</content>

## 8. `sandbox/` — 実行境界（最重要パート）

LLMが生成した**信頼できないコード**を安全に実行するための多層防御。`sandbox/`パッケージは5ファイルで役割分担しており、大きく2層に分かれる: Pythonレベルの制限（`executor.py`、8.1）と、OSレベルの隔離（`isolated_process.py`/`isolated_worker.py`、8.2）。前者だけでは防ぎきれない脱出経路があることが`executor.py`自身のコメントに明記されており、「OS境界が主たる防御線であり、Pythonレベルの制限は多層防御の一枚に過ぎない」という位置づけが繰り返し強調されている。

### 8.1 `sandbox/executor.py`

`Sandbox`クラス本体（`sandbox/executor.py:350-527`）。コンストラクタ（353-388行目）は`isolated`引数で2つのモードに分岐する:

```python
if isolated:
    self.namespace = {}
    self._isolated_process = IsolatedSandboxProcess(config, extra, apply_process_memory_limit)
    return
# isolated=False: このプロセス内で直接実行する
if apply_process_memory_limit:
    self._apply_memory_limit()
self.namespace = self._build_namespace(extra)
```

ここで言う「ラッパー」「ワーカー」が何を指すか、先にはっきりさせておく。

- **ラッパー(wrapper)**とは、中身の実処理を自分では持たず、別の場所にある本体を「包んで」呼び出しやすくするだけのオブジェクトのこと。`isolated=True`のときの`Sandbox`がまさにこれ: コンストラクタは`self.namespace = {}`（空っぽ）にして`IsolatedSandboxProcess`を作り、実行の仕事を丸ごとそちらへ投げて`return`するだけである。つまり見た目は`Sandbox`でも、実体は「別プロセスへの取り次ぎ役」でしかない。
- **ワーカー(worker)**とは、実際の作業（ここではLLMが書いたコードの`exec()`実行）を請け負う、別プロセスとして起動される実行主体のこと。8.2で詳しく見る`isolated_worker.py`がその実体であり、OSレベルで隔離された「檻」の中で動く。

この2つを踏まえて`isolated`引数の分岐を読むと:

`isolated=True`（デフォルト）なら実行そのものを`IsolatedSandboxProcess`（8.2）に丸ごと委譲し、このクラス自身は空の`namespace`を持つだけの薄いラッパーになる。`isolated=False`は「OS境界が既に確立された後、ワーカー自身がコードを実行するための内部モード」で、実際には`isolated_worker.py:83-88`が`Sandbox(config, ..., isolated=False)`という形で使うだけであり、外部から信頼できないコードに対して直接この経路を使うべきではないと`_apply_memory_limit()`のdocstring（390-399行目）にも明記されている。

まとめると呼び出し関係はこうなる:

```
Orchestrator
  → Sandbox(isolated=True) ...... ラッパー。自分では何も実行しない
      → IsolatedSandboxProcess ... 親プロセス側。unshare/bwrapでOS隔離された
                                    子プロセス(ワーカー)を起動する(8.2で詳述)
          → ワーカー = isolated_worker.py が動いている
              → その内部でさらに Sandbox(isolated=False) を作り、
                実際にexec()する(Pythonレベルの制限つき)
```

つまり「OSレベルの隔離(主たる防御線)」と「Pythonレベルの制限(このあと説明する多層防御)」は、`isolated=True`という1つの入り口の裏側で、親プロセス／ワーカープロセスという2つの別プロセスに役割分担されている。

#### インポート制限（多重チェック）

ここで言う「静的チェック」「動的チェック」の違いを先に押さえておく。**静的チェック**とは、コードを**実行せずに**、書かれたテキストを解析してあやしい箇所を見つける方法。ここで使われる`ast`（Abstract Syntax Tree＝抽象構文木）はPython標準ライブラリの1つで、ソースコードの文字列を「`import`文」「関数呼び出し」「属性アクセス」のような部品(ノード)の木構造に分解してくれる。`ast.walk(tree)`はその木を1つ残らず辿るための関数で、「コードのどこかに`import os`という文が**書かれているか**」をテキストレベルで機械的に調べられる。ただしこれには弱点がある——「書かれた形」を見ているだけなので、`import`という単語を一切使わずに同じ効果を実行時に起こすコード（例えば`__import__("os")`という**関数呼び出し**）は素通りしてしまう。そこを埋めるのが**動的チェック**——コードが実際に**実行されている最中**に、危険な操作(`__import__`の呼び出しそのもの)を横取りして止める方法。このファイルの防御はこの2つを重ねる形になっている。

**静的チェック** `check_imports()`（165-186行目）は`ast.walk(tree)`で`ast.Import`/`ast.ImportFrom`ノードを拾い、`_is_authorized()`（90-92行目、`fnmatch.fnmatch`によるglobマッチ）で許可リストと照合する。`from foo import *`は「どの名前が実際にインポートされるか静的に分からない」という理由で無条件禁止（185-186行目）。

```python
def check_imports(tree: ast.AST, authorized: list) -> None:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if not _is_authorized(alias.name, authorized):
                    raise SandboxViolation(f"import of '{alias.name}' is not permitted")
        elif isinstance(node, ast.ImportFrom):
            if node.module is None or not _is_authorized(node.module, authorized):
                raise SandboxViolation(f"import of '{node.module}' is not permitted")
            if any(alias.name == "*" for alias in node.names):
                raise SandboxViolation("star imports are not permitted")
```

**動的チェック** `_make_restricted_import()`（253-283行目）は`builtins.__import__`自体を差し替える。`import`文はコンパイル時に暗黙で`__import__`を呼ぶだけなので、静的チェックをすり抜けて`__import__("os")`のように**関数として直接呼ぶ**バイパス（`test_dynamic_import_bypass_is_blocked`が検証）に対しては、この実行時版が最後の砦になる:

```python
def restricted_import(name, globals=None, locals=None, fromlist=(), level=0):
    if not _is_authorized(name, authorized):
        raise SandboxViolation(f"import of '{name}' is not permitted")
    module = real_import(name, globals, locals, fromlist, level)
    return wrap_module(module)
```

**モジュールプロキシ** `_RestrictedModule`（189-250行目、`ModuleType`を継承）: 許可されたモジュールをそのまま返すと、`random._os`や`typing.sys`のように「許可モジュールが内部で非許可モジュールへの参照を保持している」経路から脱出できてしまう（`test_private_module_reference_escape_is_blocked`, `test_public_unauthorized_nested_module_is_blocked`が実際にこれを検証）。`__getattribute__`をオーバーライドし（214-245行目）、属性を返す前に3段階のチェックを行う:

1. 属性名が`_is_forbidden_attribute()`に該当する（プライベートっぽい名前、または`str.format`のような明示的禁止名）なら即座に拒否。
2. `operator.attrgetter`/`operator.methodcaller`は、`operator`モジュール自身に対して個別に禁止（226-227行目）— これらは「文字列から動的に属性アクセス・メソッド呼び出しを組み立てる」機能を持ち、AST上の`Attribute`ノードとして現れないため静的チェックを迂回できる。実際に`test_operator_attrgetter_private_attribute_bypass_is_blocked`は`operator.attrgetter('_os')(random)`という具体的なペイロードでこれを検証している。
3. 取得した値がさらに別のモジュール（入れ子モジュール）だった場合、それ自身が許可リストに独立して載っていなければ公開しない（233-241行目）。載っていれば同じ`_RestrictedModule`で再帰的にラップしてから返す（再帰的な保護）。

コンストラクタで`object.__setattr__`を使って内部状態（`_restricted_module`等）を設定しているのは（208-212行目）、自身の`__getattribute__`の制限ロジックに巻き込まれないようにするためのテクニック。

#### ファイルアクセス制限

`_make_restricted_open()`（286-306行目）が`open`を差し替える。`os.path.realpath()`でシンボリックリンクまで解決した実パスを求めた上で、`allowed_directories`（同様に`realpath`解決済み）のいずれかと一致するか配下にあるかを判定する:

```python
target = os.path.realpath(os.fspath(file))
allowed = any(target == base or target.startswith(base + os.sep) for base in resolved_allowed)
```

`realpath`解決を両側で行っているのは、シンボリックリンクを使って許可ディレクトリの外を指す経路を防ぐため。

#### 危険な組み込みの除去

前提として、このサンドボックスは`exec(compiled, self.namespace)`（後述の`Sandbox.run()`）というPython標準の仕組みでコードを実行している。`exec()`は文字列やコンパイル済みコードを、指定した「名前空間」(変数名→値の辞書)の中でそのまま実行する組み込み関数——普段何気なく使っている`print(...)`や`open(...)`のような関数は、実は`__builtins__`という特別な名前空間経由で暗黙に見えているだけであり、`exec()`にどんな`__builtins__`を渡すかを差し替えれば、そのコードから見える「使える関数の一覧」自体を絞り込める。これがこの節の土台になっている考え方。

`_UNSAFE_BUILTINS`（56-59行目）= `{eval, exec, compile, input, breakpoint, help, exit, quit, __import__, open, vars}`を、`_build_namespace()`（411-431行目）が通常の`builtins`の辞書から取り除いたものを`__builtins__`として渡す。`__import__`/`open`/`getattr`/`setattr`の4つはさらに個別の制限版に差し替えられる（423-426行目）— `getattr`/`setattr`まで差し替えているのは後述のdunder属性保護のためで、単なる除去では済まない。

#### サンドボックスエスケープ対策（`check_dunder_attribute_access`）

このファイルで最も重要な部分。まず「dunder」という言葉について: Pythonでは`__init__`や`__class__`のように前後をアンダースコア2つ(_ _)で挟んだ名前を"double underscore"→**dunder**と呼び、オブジェクトの型情報・継承関係・内部状態にアクセスするための特別な属性名として使われている。これらは普段は`x.__class__`のように「今扱っているオブジェクトの型を知る」といった無害な用途に使うが、CPython(標準のPython実装)は実行中のプロセスの**あらゆるクラス・関数・モジュールの内部構造**をこのdunder属性経由でどこまでも辿れるように作られている——この「どこまでも辿れる」性質(イントロスペクション)こそが、次に説明する古典的なin-process Pythonサンドボックス脱出手法の土台になる:

```python
().__class__.__bases__[0].__subclasses__()
```

`test_subclasses_escape_via_dot_attribute_is_blocked`（`tests/test_sandbox.py:96-120`）が実際の攻撃コードを再現している:

```python
for cls in ().__class__.__bases__[0].__subclasses__():
    try:
        g = cls.__init__.__globals__
    except Exception:
        continue
    if "__builtins__" in g:
        b = g["__builtins__"]
        real_import = b["__import__"] if isinstance(b, dict) else b.__import__
        real_import("os")
        print("ESCAPED")
        break
```

これがどう機能するか順を追うと: `().__class__`は`tuple`型オブジェクト、`.__bases__[0]`は`object`型、`.__subclasses__()`は**現在ロードされている`object`の全サブクラス**（＝事実上プロセス内の全クラス）を返す。それを1つずつ`cls.__init__.__globals__`（関数オブジェクトが定義時に閉じ込めているモジュールグローバル辞書）まで辿ると、いずれかのクラスの`__init__`の`__globals__['__builtins__']`は**このサンドボックスが一度も差し替えていない、本物の無制限`builtins`**を指している（なぜなら`_build_namespace()`が差し替えるのは`exec()`に渡す名前空間の`__builtins__`エントリだけで、既にロード済みの標準ライブラリ内の関数が持つグローバル辞書までは書き換えないため）。そこから`real_import("os")`→`os.system(...)`のように到達すれば、`__import__`の差し替えも`open`の差し替えも完全にバイパスされる。`__import__`/`open`の差し替えは**名前ルックアップ**しか守らないため、このような「サンドボックスが一度も手渡していないオブジェクトへの任意の属性アクセス」には無力 — というのが`BENCHMARK_REPORT.md`に記録されている「独立レビューで見つかった重大な脆弱性」の正体。

対策は**デフォルト拒否のダンダー属性アローリスト**（107-118行目 `_SAFE_DUNDER_ATTRS`）:

```python
_SAFE_DUNDER_ATTRS = {
    "__init__", "__name__", "__doc__", "__module__", "__qualname__",
    "__repr__", "__str__", "__format__", "__hash__",
    "__eq__", "__ne__", "__lt__", "__le__", "__gt__", "__ge__",
    "__bool__", "__len__", "__iter__", "__next__", "__contains__",
    "__getitem__", "__setitem__", "__delitem__", "__call__",
    "__enter__", "__exit__",
    "__add__", "__radd__", ... （演算子オーバーロード群）
}
```

`_is_forbidden_attribute()`（121-132行目）が判定ロジック本体:

```python
def _is_forbidden_attribute(name: object) -> bool:
    return (
        isinstance(name, str)
        and (
            name in _FORBIDDEN_PUBLIC_ATTRIBUTES         # "format"
            or (name.startswith("_") and name not in _SAFE_DUNDER_ATTRS)
        )
    )
```

「`_`で始まり、かつこのアローリストに無い名前」を全て拒否する。これにより`__subclasses__`, `__globals__`, `__bases__`/`__base__`/`__mro__`, `__builtins__`, `__code__`/`__closure__`, `__getattribute__`, `__reduce__`/`__reduce_ex__`といった危険な属性を個別に列挙する必要なく**まとめて**塞げる — ブロックリスト方式ではなくアローリスト方式にしている理由がコード中のコメント（95-106行目）に明記されている: 「稼働中のCPythonプロセスのイントロスペクション可能な表面積が大きすぎて完全にブロックリスト化できないため」。

このチェックには静的・動的の2段構えがある:

1. **静的**: `check_dunder_attribute_access()`（135-162行目）がAST内の`ast.Attribute`ノード（ドット記法 `obj.__subclasses__`）を走査して拒否する。`test_globals_attribute_access_is_blocked`（`f.__globals__`）がこれを検証。
2. **動的**: `getattr(obj, "__sub" + "classes__")`のように**文字列を組み立てて`getattr`経由でアクセス**するケースは、ソースコード上に`Attribute`ノードとして現れないため静的チェックをすり抜ける。そこで`getattr`/`setattr`自体を`_make_restricted_getattr()`/`_make_restricted_setattr()`（309-334行目）で差し替え、同じ`_is_forbidden_attribute()`判定をランタイムでも強制する:

```python
def restricted_getattr(obj, name, *default):
    if _is_forbidden_attribute(name):
        raise SandboxViolation(f"access to '{name}' is not permitted")
    return real_getattr(obj, name, *default)
```

   `test_subclasses_escape_via_getattr_is_blocked`（`getattr(object, '__subclasses__')()`）と`test_subclasses_escape_via_dynamically_built_name_is_blocked`（`name = '__sub' + 'classes__'; getattr(object, name)()`）がこの2段構えのそれぞれを個別に検証している — 後者のテストのdocstringが明言する通り「ガードはソースコード上のリテラル文字列ではなく、実際に解決された名前をチェックしなければならない」。

`str.format`も明示的に禁止（62行目 `_FORBIDDEN_PUBLIC_ATTRIBUTES = {"format"}`）。理由は`"{0.__class__}".format(x)`のような属性ミニ言語が**文字列から実行時に属性名を解釈する**ため、AST上の`Attribute`ノードとしては現れず、上記の静的/動的チェックを両方すり抜けてしまうから（`test_format_attribute_escape_is_blocked`が検証）。さらに`string.Formatter().get_field('0.__class__', ...)`という、`str.format`を経由せず同じ書式ミニ言語だけを直接使う迂回路もあり、これは`string.Formatter`自体を`_FORBIDDEN_MODULE_ATTRIBUTES = {"string": {"Formatter"}}`（63行目、`_RestrictedModule.__getattribute__`内229-230行目でチェック）でモジュール属性レベルで個別禁止することで塞いでいる（`test_formatter_field_escape_is_blocked`が検証）。

**既知の残存ギャップ**としてコード中に明記されているのは、それでも`str.format`関連の書式ミニ言語を完全に無効化しない限り防ぎきれない経路がある可能性があること。ただし完全禁止するとMBPP/SWE-bench用の正当な解答コードを壊す実害の方が大きいため、この項目は「OS境界が主たる防御線であり、これは多層防御の一枚」という前提のもとで受容されている。`test_common_dunders_still_work_for_legitimate_code`（182-209行目）は逆方向の回帰テストで、`__init__`/`__repr__`/`__add__`/`__eq__`のような正当な演算子オーバーロードやイテレーションが壊れていないことを確認する — 防御を固めすぎて必要な機能まで壊していないかという「過剰ブロック」の確認も同じファイルに同居している。

#### タイムアウト・メモリ制限

ここからは、Pythonのコードレベルではなく**OSがプロセスに対して直接かけてくる制限**の話になる。「シグナル」はOSカーネルがプロセスに送る非同期の割り込み通知（後述のSection 5でSIGTERMについて詳しく扱う仕組みと同じ種類のもの）で、`SIGALRM`は「指定した秒数が経過したら送られてくる」タイマー専用のシグナル。「リソース上限(rlimit)」はOSがプロセスごとに設定できる使用量の天井（メモリ量やファイル数など）で、超えた瞬間にOS側が強制的に失敗させる仕組み。どちらもPythonのコードを一切信用せず、**OSレベルで**時間とメモリに歯止めをかける。

- `signal.alarm()` + `SIGALRM`ハンドラ（344-347行目、467-470行目）でコード実行に壁時計タイムアウトをかける。`_alarm_handler`が`SandboxTimeoutError`を送出し、`run()`側の`except SandboxTimeoutError`（476-482行目）がそれまでの部分出力を添えて`[Timeout] ...`を返す。`finally`節（498-502行目）で必ずアラームを解除し元のハンドラに戻す。
- `resource.setrlimit(RLIMIT_AS, ...)`（`_apply_memory_limit()`, 390-409行目）でプロセスのアドレス空間を制限し、暴走したメモリ確保をOSのOOM killerではなくPythonの`MemoryError`として捕捉できるようにする。ハード上限が既存より小さければそちらを尊重する（`new_hard = hard if hard != resource.RLIM_INFINITY and hard < limit_bytes else limit_bytes`, 406行目）。サンドボックス化されたCI環境などで上限をこれ以上下げられない`ValueError`/`OSError`はベストエフォートとして握りつぶす（403-409行目）。

#### `Sandbox.run()`の返り値ポリシー

**通常のコードエラーで例外を投げることは絶対にない。** `run()`（433-502行目）の`try/except`構造を見ると分岐が一望できる:

```python
try:
    with contextlib.redirect_stdout(output):
        exec(compiled, self.namespace)
    return self._truncate(output.getvalue())
except SandboxTimeoutError:
    return f"[Timeout] ..."
except MemoryError:
    return f"[MemoryLimitExceeded] ..."
except SandboxViolation as exc:
    return f"[SandboxViolation] {exc}"
except FinalAnswer:
    raise                                    # ← 特別扱い、伝播させる
except (KeyboardInterrupt, SystemExit):
    raise                                    # ← 特別扱い、伝播させる
except Exception as exc:                     # noqa: BLE001 - 意図的
    return f"[Error] {type(exc).__name__}: {exc}"
```

`FinalAnswer`, `KeyboardInterrupt`, `SystemExit`だけが呼び出し元へそのまま伝播する。それ以外のあらゆる例外（候補コードが投げた任意のエラー含む）は`"[ErrorKind] ..."`という文字列に変換されて`return`される — Orchestratorがそれをそのまま次のObservationとして使える形。実行前チェック（構文エラー→`[SyntaxError]`、import/dunder違反→`[SandboxViolation]`、452-461行目）も同じ文字列返却ポリシーに従う。

### 8.2 `sandbox/isolated_process.py` と `sandbox/isolated_worker.py`

上記のPythonレベルの制限は「多層防御の一枚」に過ぎない。**本丸はOSレベルの隔離**。`IsolatedSandboxProcess`（親プロセス側、`isolated_process.py`）が`unshare`+`bubblewrap`（`bwrap`）で常駐ワーカープロセスを起動し、標準入出力越しのJSONプロトコルで通信する。`isolated_worker.py`（子プロセス側のエントリポイント）が、実際に`Sandbox(isolated=False)`を使ってコードを実行する。

#### `unshare`と`bwrap`は何をしているのか（概念編）

具体的なコマンドラインを読む前に、そもそも`unshare`と`bwrap`が何を根拠に「隔離」を実現しているのかを押さえておく。この2つは役割が違う、**二段重ね**の仕組みになっている。

**大前提：Linuxの「名前空間（namespace）」**——Linuxカーネルには、プロセスに「グローバルなリソース(ネットワーク、ファイルシステムのマウント状況、プロセスID一覧、ユーザーID体系など)の**専用の見え方**」を持たせる機能がある。これが名前空間。新しい名前空間の中にいるプロセスは、ホスト側の本物のリソースが見えなくなり、「その名前空間の中だけの、空っぽの状態」からスタートする。`unshare`も`bwrap`も、この名前空間機能を使うためのコマンド。

**`unshare`：外側の箱を作る**——`unshare --user --map-root-user --net`は、新しい**ユーザー名前空間**（誰が何の権限を持つかをホストとは別管理にする）と新しい**ネットワーク名前空間**（NICを1つも持たないため外部ネットワークに一切到達できない）を作る。`--map-root-user`は、その新しいユーザー名前空間の**中でだけ**、今のユーザーを`uid 0`（root）に見せかける——ホスト上で本物のrootになるわけではない（ホストからはただの一般ユーザーのまま）。これは次の`bwrap`が内部でさらに名前空間を作る操作(マウント操作など)をするのに「名前空間内でのroot権限」を必要とするための下ごしらえで、suid-rootでインストールされた`bwrap`バイナリを使わずに、この「にせroot」経由で必要な権限だけを与える、という一般的なテクニック。

**`bwrap`（bubblewrap）：中身（ファイルシステムなど）を組み立てる**——`unshare`が用意した「にせroot」の権限を使って、`bwrap`はさらに細かい隔離環境を組み立てる。`--unshare-user --uid 65534 --gid 65534`でもう一段ユーザー名前空間を切り、実際にコードを実行する段階では`nobody`/`nogroup`相当の無権限ユーザーに戻す。そして最も重要なのが**ファイルシステム**: `bwrap`は既定で「何も見えない空っぽのルートファイルシステム」から始まり、明示的に`--ro-bind`（読み取り専用でマウント）や`--bind`（読み書き可能でマウント）したパスだけが見えるようになる——リポジトリのルート全体や`.env`ファイルなど、明示的にマウントしなかったものは、脱出したコードから見ても**そもそも存在しない**扱いになる。

つまりPythonレベルの制限(import禁止・dunder属性ブロックなど)を仮に突破して`os.system(...)`のような本物のシステムコールに到達しても、ネットワーク名前空間のおかげで外部と通信できず、マウント名前空間のおかげで見えているファイルシステムが最小限のコピー＋明示的に許可されたディレクトリだけであり、無権限ユーザーなので大した権限もない——脱出しようにも脱出先が物理的にない、という設計になっている。次の節では、この考え方が実際のコマンドライン(各フラグ)としてどう具体化されているかを見る。

#### 隔離の中身（`_build_command`, `isolated_process.py:88-215`）

まず起動前提のチェック（91-116行目）: Linux以外なら`RuntimeError`、`unshare`/`bwrap`どちらかのコマンドが`shutil.which()`で見つからなければ`RuntimeError`、Pythonインタプリタが`/usr`配下になければ`RuntimeError`。**フォールバックせず、要件を満たさなければ即座に失敗する（fail-closed）**という設計方針がここに現れている。

組み立てられる実際のコマンド列:

```
unshare --user --map-root-user --net --
  bwrap --clearenv --die-with-parent --unshare-user
        --uid 65534 --gid 65534
        --ro-bind /usr /usr, /lib, /lib64
        --dev /dev --proc /proc --tmpfs /tmp
        --dir /agent --dir /agent/sandbox --ro-bind <project>/sandbox /agent/sandbox
        --dir /agent/site-packages --ro-bind <venv>/site-packages /agent/site-packages
        --ro-bind <project>/models.py /agent/models.py
        --bind <allowed_directory> <allowed_directory>   (SandboxConfig.allowed_directoriesの各エントリ)
        --setenv PYTHONPATH /agent:/agent/site-packages
        --setenv PYTHONUNBUFFERED 1 --setenv PYTHONDONTWRITEBYTECODE 1 --setenv HOME /tmp
        --chdir /agent
        python /agent/sandbox/isolated_worker.py
```

各フラグの役割（119-208行目のコメントに対応）:

| フラグ | 意味 |
|---|---|
| `unshare --user --map-root-user` | 新しいuser namespaceを作り、その中では自分をrootに見せる（bwrap内部でのuid切替の土台） |
| `unshare --net` | 新しいnetwork namespaceを作成 — NICが1つも無いため外部ネットワークに一切到達できない |
| `bwrap --clearenv` | 親の環境変数を全消去（APIキー等の情報漏洩防止） |
| `bwrap --die-with-parent` | 親プロセスが死んだら道連れで終了（プロセスの取り残し防止） |
| `bwrap --unshare-user --uid 65534 --gid 65534` | bwrap内でさらにuser namespaceを分離し、"nobody"相当の非特権ユーザーとして動作 |
| `--ro-bind /usr /usr` 等 | Python本体・共有ライブラリの実行に必要な最小限を読み取り専用でバインド |
| `--dev /dev --proc /proc --tmpfs /tmp` | 最小限のデバイスファイル、隔離されたPID namespace用`/proc`、空の一時領域 |
| `--ro-bind <project>/sandbox /agent/sandbox` 等 | **プロジェクトルートを丸ごとマウントしない** — `sandbox/`パッケージ、`models.py`、依存site-packagesだけを個別にro-bind |
| `--bind <allowed_directory> ...` | `SandboxConfig.allowed_directories`のみ読み書き可能でバインド |

重要なのは、`.env`ファイルなど機微な情報を含みうるリポジトリルートではなく、必要なファイルだけを個別にマウントしている点（163-165行目のコメントに明記）。`allowed_directories`のマウントは`_validate_allowed_target()`（252-276行目）によるガードを経てから行われる:

```python
protected = {Path("/"), Path("/agent"), Path("/dev"), Path("/lib"), Path("/lib64"),
             Path("/proc"), Path("/tmp"), Path("/usr")}
if target in protected:
    raise ValueError(f"allowed directory '{target}' would weaken the isolated root")
protected_roots = protected - {Path("/"), Path("/tmp")}
if any(root in target.parents for root in protected_roots):
    raise ValueError(f"allowed directory '{target}' is inside a protected root")
```

`/`と`/tmp`はサブディレクトリを許可することを認めている（`/tmp`は元々空のtmpfsとして書き込み可能だから）一方、`/usr`のようなちょうど読み取り専用でro-bindしたルートの内部を`allowed_directories`に指定すると、その中身が書き込み可能な`--bind`で**上書き**されて隔離が弱まってしまうため、これを明示的に拒否している。`_add_directory_mounts()`（278-291行目）は、bwrapが「マウント先の親ディレクトリが事前に存在している必要がある」という制約を満たすため、ターゲットパスまでの各階層に`--dir`を積み上げていくヘルパー。

#### 通信プロトコル

親プロセスとワーカープロセス(子)は、メモリを共有できない別々のプロセスなので、何かをやり取りするには標準入出力(stdin/stdout)のようなバイト列のパイプを介するしかない。ここで使われているのは「1行＝1メッセージ」という単純なプロトコル: 送りたい情報をJSON文字列にシリアライズし、末尾に改行を付けて書き出すだけ。受け取る側は1行読んで`json.loads()`すればいい。複雑なフレーミング(メッセージの区切りを長さプレフィックスで表現する等)を使わずに済むのは、JSONの中身自体に改行を含めない前提（テキストは適切にエスケープされる）があるため。

親↔子は改行区切りのJSONメッセージで通信する（`_send`/`_read_message`、`isolated_process.py:293-314`と`isolated_worker.py`側の`_send`/`_read`, 18-34行目が対応する実装）:

- 親→子 `init`（`isolated_process.py:68-75`）: `{"type": "init", "config": ..., "tool_names": [...], "apply_process_memory_limit": ...}`。子側は`isolated_worker.py:63-93`の`main()`冒頭でこれを受け取り、`SandboxConfig.model_validate()`でバリデーションしてから`Sandbox(config, extra_namespace=namespace, isolated=False)`を構築し、成功すれば`{"type": "ready"}`、失敗すれば`{"type": "worker_error", ...}`を返す（親側は`_start()`の77-86行目でこれを`STARTUP_TIMEOUT_SECONDS=10.0`秒待つ）。
- 親→子 `run`（`isolated_process.py:326`）: `{"type": "run", "code": "..."}`。子側は95-124行目のメインループでこれを受け取り`sandbox.run(...)`を呼ぶ。
- 子→親 `tool_call`（`isolated_worker.py`内`_ToolBridge.__call__`, 46-52行目）: `{"type": "tool_call", "name": ..., "args": [...], "kwargs": {...}}`。
- 親→子 `tool_result`（`isolated_process.py:364`）: `{"type": "tool_result", "ok": bool, "result": ...}`。
- 子→親: `{"type": "result", "output": "..."}` / `{"type": "final_answer", "answer": ...}` / `{"type": "keyboard_interrupt"}` / `{"type": "system_exit", "code": ...}` / `{"type": "worker_error", "error": ...}`（`isolated_worker.py:108-124`で送出、`isolated_process.py:369-383`で受信・再構築）。

**`_ToolBridge`によるトリック**（`isolated_worker.py:37-60`）: ワーカー内では、MCPツール呼び出しは**ただの関数呼び出しに見える**。実体は次の通り:

```python
class _ToolBridge:
    def __init__(self, name, input_stream, output):
        self._name = name; self._input_stream = input_stream; self._output = output

    def __call__(self, *args, **kwargs):
        _send(self._output, {"type": "tool_call", "name": self._name, "args": list(args), "kwargs": kwargs})
        response = _read(self._input_stream)
        if response.get("type") != "tool_result":
            raise RuntimeError(...)
        if not response.get("ok", False):
            raise RuntimeError(str(response.get("result", "MCP tool call failed")))
        return response.get("result")
```

各ツール名につき1つの`_ToolBridge`インスタンスが名前空間に登録され（`main()`内77-80行目）、サンドボックス内のコードから`search_code("foo")`のように呼ぶと`__call__`が発火し、JSON1往復に変換されて親プロセスへ転送される。ワーカー自身は生の`MCPToolProxy`（や`asyncio`イベントループ）を一切知らない。

親側の`_invoke_tool()`（`isolated_process.py:385-420`）が実際のツール実行を担う。ここで初めて`self._extra_namespace`（＝`MCPToolProxy.build_namespace()`が返した本物の関数群）を引く:

```python
def invoke():
    try:
        result_queue.put((True, function(*args, **kwargs)))
    except BaseException as exc:
        result_queue.put((False, f"{type(exc).__name__}: {exc}"))

thread = threading.Thread(target=invoke, daemon=True)
thread.start()
thread.join(max(timeout, 0.0))
if thread.is_alive():
    return False, "MCP tool call timed out", True
```

結果が`json.dumps()`可能かどうかを試し（416-419行目）、できなければ文字列化してフォールバックする — ワーカーに送り返すプロトコル自体がJSONなので、シリアライズ不能な値をそのまま返そうとしてプロトコル全体を壊さないための保険。

**ワーカーは常駐する**ので、`namespace`に定義された変数はエージェントの各ステップ間で保持される（1タスク＝1ワーカープロセスのライフサイクル、`IsolatedSandboxProcess`が`Sandbox`インスタンスと同じ寿命を持つ）。

#### 二重（実質三重）のタイムアウト

1. **親側の壁時計デッドライン**（`isolated_process.py:run()`, 316-383行目）: `deadline = time.monotonic() + timeout`を設定し、`_read_message(remaining)`がその残り時間内に応答しなければ`_terminate_process()`（422-445行目、`os.killpg`で`SIGTERM`→`WORKER_TERMINATE_TIMEOUT_SECONDS=0.5`秒待って`SIGKILL`）でワーカーのプロセスグループごと殺す。
2. **ワーカー側自身のSIGALRM**（`Sandbox`内部、8.1参照）: `signal.alarm()`でクリーンな`[Timeout]`文字列を返せるようにするための、より内側のタイムアウト。
3. **MCPツール呼び出し専用のスレッドタイムアウト**（`_invoke_tool()`内`thread.join(timeout)`）: 親プロセス側での`function(*args, **kwargs)`呼び出し自体が（MCPサーバーのハング等で）ブロックし続けても、外側の壁時計デッドラインとは独立に検知できるようにするためのもう一段の防御。ツール呼び出しを別スレッドで実行しているのは、メインスレッドをブロックせずにタイムアウト監視を続けられるようにするため。

### 8.3 `sandbox/mcp_client.py`

まず前提知識を2つ。**MCP(Model Context Protocol)**とは、LLMエージェントが「外部の道具(ツール)」を呼び出すための標準規格——「このサーバーにはどんな関数があるか(名前・引数・説明)を教えてもらい、名前を指定して引数付きで実行し、結果を受け取る」という一連のやり取りを、どんなツールサーバーに対しても同じ手順で行えるようにする。このプロジェクトの`mcp_tools_mbpp.py`/`mcp_tools_swebench.py`（Section 12）がまさに「MCPサーバー」であり、エージェント側はこのMCPToolProxyを通してそれらに接続する。**asyncio**とは、Pythonの非同期処理の仕組みで、「I/O待ち(ネットワーク応答待ちなど)の間、他の作業を進められるようにする」ためのシングルスレッドの協調的なタスクスケジューラ(=イベントループ)を指す。`async def`で定義された関数(コルーチン)は、`await`した箇所で一旦他のタスクに実行を譲る——複数のI/O待ちを効率よくさばける代わりに、呼び出す側のコードも基本的に`async`の世界に揃える必要がある、という制約がある。

`MCPToolProxy`（`sandbox/mcp_client.py:40-319`）— サンドボックスの外（信頼された親プロセス側）で動く、MCP公式SDKの非同期クライアントに対する**同期ファサード**（ファサード＝複雑な内部実装を、シンプルな窓口だけに整理して見せる設計パターン）。

- **なぜ必要か**: サンドボックスの`exec()`名前空間には`result = search_code("foo")`のような普通の同期Python関数が必要だが、`mcp`パッケージの`ClientSession`はasyncioベース。コンストラクタ（48-95行目）がバックグラウンドスレッド（`_run_event_loop`, 97-105行目）で専用イベントループを1つ回し、`_run()`（121-129行目）が`asyncio.run_coroutine_threadsafe()`で全呼び出しを橋渡しすることで、他のコードは一切asyncioを意識しなくて済むようにしている。
- **2つの必須トランスポートに両対応**: `_connection_owner()`（131-191行目）が`stdio_command`か`http_url`かで分岐し、`stdio_client`（MCPサーバーをサブプロセスとして起動）または`streamablehttp_client`（既に起動しているHTTPサーバーに接続）いずれかのコンテキストに入る（154-164行目）。
- **ツール名を一切ハードコードしない**: 接続確立後に`self.session.list_tools()`を呼び（169行目）、返ってきた`self.tools`から`build_namespace()`（246-252行目）が動的にラッパー関数を生成する。
- **`manual_text()`**（285-305行目）: 同じ`tool.inputSchema`から、パラメータ名・型・必須（`?`マーカーなし）/任意（`?`マーカーあり）・説明文を自動整形した人間可読ドキュメントを生成する。`prompts.py`は接続先のMCPサーバーが何であるかを一切知らなくてよい。
- **`_make_wrapper()`（254-283行目）— 位置引数もキーワード引数も受け付ける**:

  ```python
  def wrapper(*args, **kwargs):
      if len(args) > len(param_names):
          return f"[Error] {name}() takes at most {len(param_names)} positional arguments but {len(args)} were given"
      arguments = dict(zip(param_names, args))
      duplicates = arguments.keys() & kwargs.keys()
      if duplicates:
          return f"[Error] {name}() got multiple values for {sorted(duplicates)}"
      arguments.update(kwargs)
      return self.call_tool(name, arguments)
  ```

  `param_names`はMCPツールのJSON Schemaの`properties`宣言順（266行目）。FastMCPベースのサーバー（このプロジェクト自身を含む）では、この宣言順が元の関数の実引数順と一致するという前提に立っている。実際にこの位置引数マッピングが無かったために`wrapper() takes 0 positional arguments but 1 was given`という実行時エラーが起きたバグが`BENCHMARK_REPORT.md`に記録されている。
- **すべての境界にタイムアウト**（35-37行目の定数）: 接続確立`CONNECT_TIMEOUT_SECONDS=30.0`秒、ツール呼び出し`CALL_TOOL_TIMEOUT_SECONDS=300.0`秒（`call_tool()`, 215-244行目）、close`CLOSE_TIMEOUT_SECONDS=10.0`秒（`close()`, 307-319行目）。死んだ/ハングしたMCPサーバーが、サンドボックス（延いてはエージェント全体、延いては`container.cleanup()`）を無期限にブロックしないようにするため — `close()`のdocstring（308-314行目）が「この呼び出しの直後が`container.cleanup()`であり、それが飢餓状態にされてはならない」と明記している。
- **1つの「オーナーコルーチン」が全トランスポートコンテキストを所有**する設計（`_connection_owner`）: AnyIOのトランスポートコンテキストは、それを`enter`したのと同じタスクが`exit`しなければならない制約があるため、接続確立からclose要求までを1つの長寿命コルーチンとして実装し（`while not self._close_requested.is_set(): await asyncio.sleep(0.05)`, 172-175行目）、クロスタスクな`AsyncExitStack`の後始末失敗を防いでいる。`_stop_owner()`（193-213行目）はまず`graceful=True`で穏やかな終了要求（`_close_requested`フラグ）を送り、`CLOSE_TIMEOUT_SECONDS`以内に`_owner_stopped`されなければ`_cancel_requested`による強制キャンセルに切り替える2段構え。

### 8.4 `sandbox/cli.py`

`uv run sandbox`で起動する対話的REPL（`main()`, 85-123行目）。

```
uv run sandbox                                       # ツール無しのREPL
uv run sandbox sandbox_template.json                 # カスタム設定
uv run sandbox --mcp-stdio "python mcp_tools_mbpp.py" # MBPPツール付き
uv run sandbox --mcp-server http://localhost:8000/mcp # HTTP経由でMCP接続
```

`repl()`（48-82行目）の入力ループは単純: 最初の行が空でなければ、以後空行が来るまで継続行を`input("... ")`で溜め続け（53-71行目）、空行で確定したら`"\n".join(lines)`をまとめて`sandbox.run(code)`に渡す（73-77行目）。`FinalAnswer`が送出されたら（78-81行目）REPL自体は終了せず、提出内容を表示して次の入力ループへ戻る点が、実際のエージェントループ（`final_answer()`でタスク終了）とは異なる — こちらは人間が繰り返し試せるデバッグ用インターフェースなので、1回の`final_answer()`で強制終了させない設計になっている。`main()`の`finally`節（114-119行目）は`agent_mbpp.py`等と同じパターンで、`sandbox.close()`→`mcp_proxy.close()`を確実に呼ぶ。デバッグ・動作確認用の簡易フロントエンドであり、`Sandbox`と`MCPToolProxy`の実際の使い方の最小サンプルにもなっている。

---

## 9. `llm/` — LLM プロバイダ抽象化層

このセクションから先は、サンドボックス(実行境界)の話ではなく「LLM API をどう安定して呼び続けるか」という別の関心事に移る。前提として、OpenRouter・Groq・Together AI・Fireworks AI・Google AI Studioのような無料枠付きのLLM APIは、どれも**リクエスト数やトークン数に上限(レート制限)がある**うえに、上限に達すると一時的にHTTPエラー(429など)を返してくる。1つのプロバイダ・1本のAPIキーだけに頼っていると、そこが制限に達した瞬間にエージェントが止まってしまう。この`llm/`ディレクトリは、その不安定さを吸収して「複数のプロバイダ・複数のAPIキーを、順番に・自動的に試す」ための層になっている。

### 9.1 `llm/provider.py`

冒頭のdocstring（`llm/provider.py:1-8`）が存在理由を明言している: requirements.mdが指摘する実装ギャップ——「`generate()`は生テキストだけでなく、`StepMetrics`を埋めるために十分なメタデータ（トークン数、時間、api_url、model_name、retries）を返さなければならない」——を埋めるための契約がこのファイル。63行に3つの型しかない。

```python
@dataclass
class GenerationResult:                    # provider.py:15-25
    text: str                # LLMが生成したテキスト本文
    input_tokens: int        # 入力(プロンプト)側のトークン数
    output_tokens: int       # 出力(生成結果)側のトークン数
    request_time_ms: float   # このリクエストにかかった実時間(ミリ秒)
    api_url: str             # リクエストを送信したAPIのベースURL
    model_name: str          # 使用したモデル名
    retries: int = 0         # 成功するまでにかかったリトライ回数
```

`@dataclass`は、Pythonの標準ライブラリ`dataclasses`が提供するデコレータ。「データを保持するだけのクラス」を書くとき、素のクラスなら自分で書かなければならない`__init__`（コンストラクタ）や`__repr__`（`print()`したときの表示形式）を、フィールド名と型を並べるだけで自動生成してくれる——ここでの`GenerationResult`のように「値をひとまとめにして持ち運ぶだけ」の型を簡潔に書くための道具だと考えればよい。

`StepMetrics`（models.py）のフィールドと1対1で対応していることが分かる——`orchestrator.py:197-209`の`StepMetrics(...)`組み立てが`gen.input_tokens`/`gen.output_tokens`/`gen.request_time_ms`/`gen.api_url`/`gen.model_name`/`gen.retries`をそのまま横流しできているのは、この型がそう設計されているから。`retries`だけデフォルト値`0`を持つのは、`llm/client.py`側で成功結果を返す直前に`result.retries = retries`（後述）と後から書き換える運用になっているため。

```python
class ChatProvider(Protocol):               # provider.py:28-40
    def chat(self, messages, model, api_key, stop,
              max_output_tokens, timeout) -> GenerationResult: ...
```

`Protocol`（`typing.Protocol`）というのは、Pythonの型システムにおける少し変わった仕組みで、**構造的部分型(structural typing)**と呼ばれる考え方を実現する。普段Pythonでクラスの互換性を保証したければ、`class OpenAICompatibleProvider(ChatProvider):`のように明示的に継承させるのが一般的な発想だが、`Protocol`はそれを要求しない——「継承関係が実際にあるかどうか」ではなく「同じ名前・同じ引数のメソッドを実際に持っているかどうか」だけを見て、互換とみなす。俗に言う「アヒルのように歩き、アヒルのように鳴くなら、それはアヒルとみなす(ダックタイピング)」を型チェッカーのレベルで形式化したもの、とイメージすると分かりやすい。実際`OpenAICompatibleProvider`や`GeminiProvider`は明示的にこのクラスを継承していない（Section 9.3/9.4で見る通り、どちらも素の`class`で、同じシグネチャの`chat()`メソッドを持つだけ）。継承関係を強制せず「このシグネチャさえ満たせば`ChatProvider`として扱える」という緩い契約にしているのは、新しいプロバイダを追加する際に既存クラス階層を意識させないための設計。

```python
@dataclass
class UsageStats:                            # provider.py:43-62
    total_requests: int = 0
    total_retries: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_latency_ms: float = 0.0
    errors: List[str] = field(default_factory=list)

    def record(self, gen: GenerationResult) -> None:   # 56-62
        self.total_requests += 1 + gen.retries
        self.total_retries += gen.retries
        self.total_input_tokens += gen.input_tokens
        self.total_output_tokens += gen.output_tokens
        self.total_latency_ms += gen.request_time_ms
```

`record()`は「1回の**成功**した生成結果」だけを引数に取る点に注意——失敗した試行分の集計（全滅時の`total_requests += retries`など）は`llm/client.py`側の`generate()`末尾で別途手動で行われており（Section 9.2）、`UsageStats`自身はそれを知らない。`UsageStats`は`LLMClient`インスタンスが内部で保持する（`self.usage`）だけの補助的な集計オブジェクトで、`Orchestrator`側の`total_requests`/`total_input_tokens`等（`orchestrator.py`内でループのたびに手動加算されている値、Section 5参照）とは別系統の独立した集計である点も見落としやすい——両者は同じ元データ（`GenerationResult`）から独立に積み上げられており、`SolutionOutput`に実際に書き出されるのは`Orchestrator`側の値の方で、`LLMClient.usage`はあくまでクライアント内部のデバッグ・監視用途にとどまる。

### 9.2 `llm/client.py`

`LLMClient`（143行）が「1つの論理モデルを、複数プロバイダにわたるフォールバック付きで呼び出す」中核部分。フォールバックとは、第一候補がうまくいかなかったときに、あらかじめ用意しておいた次善の候補へ自動的に切り替える設計のこと——ここでは「プロバイダA→ダメならプロバイダB→ダメならプロバイダC」という順序で候補を用意しておき、エージェント側のコードは「候補が何個あるか」「今どれを使っているか」を一切意識せずに済むようにしている。

#### コンストラクタ（`client.py:61-89`）— 使えるプロバイダだけをスロット化する

```python
self._slots: List[_ProviderSlot] = []
for spec in provider_specs:
    keys = spec.collect_api_keys()      # 77  config.ProviderSpec.collect_api_keys()
    if not keys:
        continue                         # 78-79  キーが1つも無いプロバイダは黙って除外
    self._slots.append(_ProviderSlot(spec=spec, chat_provider=_build_chat_provider(spec), api_keys=keys))

if not self._slots:
    raise ValueError(f"No API keys found for provider(s): {names}. ...")   # 84-89
```

`_ProviderSlot`（`client.py:44-50`）は`spec`（静的設定）・`chat_provider`（実際にHTTPを叩く実装）・`api_keys`（収集済みキー一覧）・`next_key_index`（ラウンドロビン位置）をまとめたデータクラス。ここで言う**ラウンドロビン方式**とは、複数の候補（この場合はAPIキー）を順番に1つずつ巡回して使っていく方式のこと——時計の針のように「今使ったら、次は次の候補へ」と一巡してまた最初に戻る、という単純な割り当てルールで、特定の1つだけを使い続けて偏らせないための基本パターン。`_build_chat_provider()`（`client.py:37-41`）は`spec.kind`が`"gemini"`かどうかだけで`GeminiProvider`と`OpenAICompatibleProvider`を出し分ける2行のファクトリ。キーが1件も無いプロバイダは`_slots`に**そもそも登録されない**——`generate()`側のフォールバックループはキー無しプロバイダの存在を意識する必要が一切なく、コンストラクタの時点で「使えるものだけのリスト」に絞り込まれている。全プロバイダでキーが1つも見つからなければ、実行を始める前に`ValueError`で即座に落ちる（起動直後に気づかせる設計は`prompts.py`の未知`benchmark`即エラーと同じ思想）。

`from_provider_url()`（`client.py:91-94`）は`config.resolve_provider(provider_url)`（Section 10）が返す単一の`ProviderSpec`だけを使う`LLMClient`を組み立てるショートカット。`agent_mbpp.py:125`/`agent_swebench.py`が実際に呼んでいるのはこちらで、CLI引数`--provider-url`1つから「複数プロバイダにフォールバックする汎用クライアント」ではなく「指定された1プロバイダだけを使うクライアント」を作る（複数プロバイダにまたがるフォールバックを使いたい場合はコンストラクタを直接呼ぶ必要がある、という非対称性がある）。

#### `generate()`のフォールバック構造（`client.py:96-142`）— 3重ループ

```python
for slot in self._slots:                              # 111  プロバイダを優先順に
    for _ in range(len(slot.api_keys)):                # 112  そのプロバイダの全キーを一巡
        api_key = slot.api_keys[slot.next_key_index]   # 113  次に使うキーを取得
        slot.next_key_index = (slot.next_key_index + 1) % len(slot.api_keys)  # 114  ラウンドロビン更新

        for attempt in range(self.max_retries_per_key): # 116  同一キーでのリトライ
            try:
                result = slot.chat_provider.chat(...)   # 118-125
                result.retries = retries                # 126  ここまでの失敗回数を結果に刻む
                self.usage.record(result)                # 127
                return result                            # 128  成功したら即return、以降のループは一切実行されない
            except (requests.RequestException, KeyError, IndexError) as exc:  # 129
                last_error = exc; retries += 1
                self.usage.errors.append(f"{slot.spec.name}: {exc}")
                if attempt < self.max_retries_per_key - 1:
                    time.sleep(self.backoff_seconds * (attempt + 1))  # 134  線形バックオフ
        # このキーのリトライを使い果たした → 次のキー/プロバイダへフォールスルー(コメントのみ、135行目)
```

ここで使われている**リトライとバックオフ**という設計パターンについて: 通信エラーやレート制限は「少し待てば直る」一過性の失敗であることが多いので、失敗したら即座にもう一度叩くのではなく、いったん待機time.sleep()してから再試行する。この待機時間を毎回少しずつ伸ばしていく(ここでは`backoff_seconds * (attempt + 1)`という単純な線形増加、1回目1.5秒、2回目3.0秒、...)のが「バックオフ」で、相手のサーバーに立て続けに負荷をかけて状況を悪化させるのを防ぐ狙いがある(より高度な実装では倍々に増やす「指数バックオフ」を使うことも多いが、ここでは単純な線形増加が採用されている)。

`slot.next_key_index`の更新（114行目）が**リクエスト送信の成否に関わらず必ず実行される**点が地味に重要——失敗しても次回はローテーションが1つ進んだ状態から始まる（同じ壊れた/レート制限中のキーを毎回先頭で引き続けることがない）。`except`が捕まえる例外の型が`(requests.RequestException, KeyError, IndexError)`の3種類に絞られているのも意図的——`requests.RequestException`はHTTPエラー・接続エラー・タイムアウトを、`KeyError`/`IndexError`はレスポンスJSONの形式が想定と違う場合（`data["choices"][0]`が無い等、Section 9.3参照）を拾う。これ以外の例外（例えばプログラムのバグに起因する`TypeError`など）は意図的に伝播させ、リトライで握りつぶさない。

`max_retries_per_key`回のリトライを使い切ると`for attempt`ループを抜け、`# フォールスルー`のコメント通りそのまま外側の`for _ in range(len(slot.api_keys))`に戻って**次のキー**を試す。それも尽きれば次の`slot`（＝次のプロバイダ）へ。3重ループが1つも`return`せずに完走すると、`generate()`は次で締めくくられる:

```python
self.usage.total_requests += retries    # 137
self.usage.total_retries += retries     # 138
raise AllProvidersExhaustedError(
    f"All providers/keys exhausted for model '{self.model_name}'. Last error: {last_error}",
    attempted_requests=retries,
)                                        # 139-142
```

`AllProvidersExhaustedError`（`client.py:22-34`）は`attempted_requests`を独自に保持する`RuntimeError`のサブクラス。docstringが明言する通り、これは「成功した生成が一切ないので`StepMetrics`のエントリは1件も作れないが、実際に送信された失敗HTTPリクエストの回数だけは失われてはならない」という要件のための専用フィールド。`orchestrator.py:167-168`の`except AllProvidersExhaustedError as exc: total_requests += exc.attempted_requests`が、この値を`SolutionOutput.total_requests`に合流させる受け皿（Section 5参照）。

具体例で線形バックオフを追うと: `backoff_seconds=1.5`（デフォルト、`client.py:66`）、`max_retries_per_key=2`（デフォルト、`client.py:65`）の場合、1つのキーにつき最大2回試行され、1回目が失敗すると`attempt=0`なので`1.5 * (0+1) = 1.5秒`待って2回目を試す。2回目も失敗すれば（`attempt=1`は`max_retries_per_key - 1 = 1`と等しいため）待たずにそのキーを諦めて次のキーへ移る。`test_llm_client.py`の`test_multiple_keys_rotate`（88-100行目）が`seen_api_keys == ["key-one", "key-two"]`を検証し、`test_all_providers_exhausted_reports_attempted_requests`（116-132行目）が全滅3回試行での`attempted_requests == 3`かつ`usage.total_requests == 3`を検証している——これらのテスト名自体が、上で追った3重ループの各分岐に対応する仕様書になっている。

### 9.3 `llm/providers/openai_compatible.py`

OpenRouter・Groq・Together AI・Fireworks AIなど、標準的な`/chat/completions`ワイヤ形式を話すプロバイダ共通の実装（66行）。冒頭のdocstring（1-6行目）が明言する通り、「これらが全て同じワイヤーフォーマットを話すからこそ、マルチプロバイダ抽象化をこれほど薄く保てる」——実装は事実上この1メソッドだけ:

```python
def chat(self, messages, model, api_key, stop, max_output_tokens, timeout) -> GenerationResult:
    url = f"{self.base_url}{self.chat_path}"                       # 32
    payload: dict = {"model": model, "messages": messages, "max_tokens": max_output_tokens}  # 34-38
    if stop:
        payload["stop"] = stop                                     # 39-40
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}  # 43-46

    start = time.monotonic()
    response = requests.post(url, json=payload, headers=headers, timeout=timeout)  # 49
    elapsed_ms = (time.monotonic() - start) * 1000
    response.raise_for_status()                                    # 51  HTTPエラーなら例外送出
    data = response.json()

    choice = data["choices"][0]                                    # 54
    text = choice["message"].get("content") or ""                  # 55
    usage = data.get("usage", {})                                  # 56
    return GenerationResult(
        text=text,
        input_tokens=usage.get("prompt_tokens", 0),                # 61
        output_tokens=usage.get("completion_tokens", 0),           # 62
        request_time_ms=elapsed_ms, api_url=self.base_url, model_name=model,
    )
```

`headers = {"Authorization": f"Bearer {api_key}", ...}`（44行目）は**HTTPヘッダー認証**の一例——APIキーをHTTPリクエストの「ヘッダー」という、URLやボディとは別枠の付加情報領域に載せる方式で、`Authorization: Bearer <トークン>`という書式はWeb API全般で広く使われる標準的な認証パターン。これが後述するGemini（クエリパラメータ方式、9.4節——APIキーをURL自体の一部として`?key=...`のように埋め込む方式）と根本的に構造が異なる部分であり、`llm/client.py`が`ChatProvider`という共通インターフェースの裏でこの違いを吸収している。

`data["choices"][0]`（54行目）と`usage.get(...)`（56行目）の非対称性に注目——前者は角括弧アクセス（キーが無ければ`KeyError`を送出し、`llm/client.py`の`except (requests.RequestException, KeyError, IndexError)`（Section 9.2）にちょうど捕捉されてリトライ対象になる）、後者は`.get(..., {})`/`.get(..., 0)`（無くても例外にならず`0`扱い）。これは「候補（`choices`）が無いレスポンスは明確な異常なのでリトライすべき」だが「使用量情報（`usage`）が欠けているのは一部プロバイダの仕様上の揺れであり、致命的ではないので`0`として処理を続行すべき」という区別を、例外を投げるか否かのコード上の選択だけで表現している。`response.raise_for_status()`（51行目）は、HTTPのステータスコードが4xx(クライアント側エラー、例えば401=認証失敗や429=レート制限超過)や5xx(サーバー側エラー)であれば`requests`ライブラリが自動的に例外を送出してくれる仕組みで、これも同じ`except requests.RequestException`で拾われてリトライにつながる。

### 9.4 `llm/providers/gemini.py`

Google AI Studio専用実装（99行）。冒頭docstring（1-8行目）の通り、エンドポイント形式・認証方式・リクエスト/レスポンススキーマのいずれもOpenAI互換勢と別物であるため独立ファイルになっている——「抽象化がOpenAI形式のラッパーに過ぎない」状態を避け、`ChatProvider`プロトコルが本当に構造の異なる2実装で機能することを証明する存在。

#### メッセージ形式の変換（`_to_gemini_contents`, `gemini.py:24-36`）

```python
system_instruction = None
contents = []
for msg in messages:
    role = msg["role"]
    if role == "system":
        system_instruction = msg["content"]; continue     # 31-33  systemは別扱いで分離
    gemini_role = "model" if role == "assistant" else "user"  # 34  assistant→model、それ以外は全てuser
    contents.append({"role": gemini_role, "parts": [{"text": msg["content"]}]})  # 35
return system_instruction, contents
```

OpenAI形式は`{"role": "system"|"user"|"assistant", "content": "..."}`のフラットな配列だが、Geminiは「システム指示」を`contents`配列から切り離した別フィールド（`systemInstruction`）として扱い、かつロール名も`"assistant"`ではなく`"model"`を使う。`Orchestrator`（Section 5）が組み立てる`messages`（`[{"role": "system", ...}, {"role": "user", ...}, {"role": "assistant", ...}, {"role": "user", "content": "Observation:\n..."}]`という往復履歴）は、このメソッドを通ることで初めてGeminiが理解できる形になる——`Orchestrator`自身はこの変換の存在を一切知らない。

#### リクエスト組み立てとエンドポイント（`gemini.py:48-60`）

```python
url = f"{self.base_url}/models/{model}:generateContent"          # 48  OpenAI互換勢の"/chat/completions"と全く違う形
generation_config: dict = {"maxOutputTokens": max_output_tokens} # 50
if stop:
    generation_config["stopSequences"] = stop                     # 52  キー名もcamelCase
payload: dict = {"contents": contents, "generationConfig": generation_config}
if system_instruction:
    payload["systemInstruction"] = {"parts": [{"text": system_instruction}]}  # 56

response = requests.post(url, params={"key": api_key}, json=payload, timeout=timeout)  # 60
```

`params={"key": api_key}`（60行目）——**クエリパラメータ認証**という方式。`requests`ライブラリの`params`引数に渡した辞書は、送信直前にURLの末尾へ`?key=<値>`という形で自動的に連結される。つまりBearerヘッダー方式(9.3節)とは違い、APIキー自体がリクエストの「宛先URL」の一部になってしまう——これが次のインシデントの直接の原因になる。

#### キー漏洩インシデントの対策コード（`gemini.py:59-81`）

```python
try:
    response = requests.post(url, params={"key": api_key}, json=payload, timeout=timeout)
    response.raise_for_status()
except requests.RequestException as exc:
    status = getattr(getattr(exc, "response", None), "status_code", None)
    status_part = f"status={status}" if status is not None else type(exc).__name__
    raise requests.RequestException(
        f"Gemini request failed ({status_part}) for url: {url} "
        "(query parameters, including the API key, redacted)"
    ) from None
```

コード中のコメント（63-75行目）が事故の経緯そのものを記録している: `requests`/`urllib3`は`HTTPError`/`ConnectionError`/`Timeout`いずれのデフォルトメッセージも**リクエストの完全なURL**（＝`?key=...`のクエリ文字列込み）から組み立てる。その例外メッセージは`llm/client.py`の`except requests.RequestException`（Section 9.2）で捕まり、最終的に`AllProvidersExhaustedError`のメッセージ文字列に埋め込まれ、`agent_*.py`の`except Exception`経由で`SolutionOutput.error`（あるいは成功パスでも`StepMetrics.sandbox_output`）に入り、**`solution.json`としてディスクにそのまま書き出される**。実際にこの経路で本物のAPIキーが3本の`solution.json`に残ってしまい、GitHubのpush protectionがpush直前に検知して発覚した（`BENCHMARK_REPORT.md`に記録）。

対策の要点は2つ:

1. **例外を素通りさせず、必ずここで作り直す**——再構築後のメッセージは、例外オブジェクトが内部に持つ`url`属性（キー入り）ではなく、**この関数のローカル変数`url`**（48行目で組み立てた、`params`を含まないベースURL）だけを使って組み立てる。ステータスコードも`exc.response.status_code`から安全に取り出す（`getattr`の二重適用でどちらの属性も存在しない例外型でも`None`になるようガードしている）。
2. **`from None`で例外チェーンを断つ**——これを付けないと、Pythonは自動的に元の例外を`__context__`として保持し、トレースバック表示時に「During handling of the above exception, another exception occurred」として元の（キーを含む）例外も一緒に表示してしまう。`from None`は`__suppress_context__`を立てて、少なくとも標準のトレースバック整形ではその元例外が見えないようにする。

`test_gemini_provider.py`（Section 15でも扱う）はこの対策を3本のテストでピンポイントに検証している: `test_http_error_message_never_contains_the_api_key`と`test_connection_error_message_never_contains_the_api_key`は、それぞれ`raise_for_status()`内で発生する`HTTPError`と、`requests.post`自体が投げる`ConnectionError`の**両方の発生タイミング**を偽の例外で再現し、`assert FAKE_KEY not in str(exc_info.value)`で検証する（前者はステータスチェックの中、後者はPOST自体が失敗する場合——`try`ブロックが両方を1つの`except`で一括して拾えている設計であることの裏付け）。3本目の`test_successful_call_still_works`は「キーを隠す対策のせいで正常系まで壊していないか」の回帰確認で、`assert params == {"key": FAKE_KEY}`によって実際のリクエストには正しくキーが渡っていることも確認する——防御コードを追加したら、その防御が正常系を壊していないかのテストも必ずセットで書く、という本プロジェクト全体で繰り返されるパターン（Section 8のサンドボックステストにも同じ構造がある）がここにも現れている。

#### レスポンスの解析（`gemini.py:83-99`）

```python
candidate = data["candidates"][0]                                  # 85
parts = candidate.get("content", {}).get("parts", [])              # 86
text = "".join(part.get("text", "") for part in parts)             # 87  複数partsを連結
usage = data.get("usageMetadata", {})                               # 89
return GenerationResult(
    text=text,
    input_tokens=usage.get("promptTokenCount", 0),                 # 94
    output_tokens=usage.get("candidatesTokenCount", 0),            # 95
    ...
)
```

`text`が単一の文字列フィールドではなく`parts`という**リスト**を連結して作られる（87行目）のはGemini特有の構造——1つの応答が複数の`part`（テキスト片）に分割されて返ってくることがあるための対応。フィールド名も`prompt_tokens`/`completion_tokens`（OpenAI互換、snake_case）に対して`promptTokenCount`/`candidatesTokenCount`（Gemini、camelCase）と単に大文字小文字が違うだけでなく単語自体も違う——`OpenAICompatibleProvider`と`GeminiProvider`のどちらも、この差異を`GenerationResult`という共通の出口に正規化してから返しているからこそ、`llm/client.py`や`Orchestrator`はプロバイダの違いを一切意識せずに済む。

---

## 10. `config.py` — 環境変数とプロバイダ設定

冒頭のdocstring（`config.py:1-6`）が存在理由を明言する通り、「`os.environ`に直接触るコードをこのファイル1つに集約し、他のどこにもAPIキーをハードコードさせないための建て付け」——「APIキーのハードコード禁止」というルールを、レビューや規約で守らせるのではなく**アーキテクチャで強制する**ための1ファイル。108行で`load_env()`/`ProviderSpec`/`KNOWN_PROVIDERS`/`resolve_provider()`の4点だけ。

ここでいう**環境変数(environment variable)**とは、OSがプロセスごとに持たせているキー・バリュー形式の設定領域のこと——プログラムのソースコードを書き換えずに、実行する環境（開発者のPC、CI、本番サーバーなど）ごとに異なる値(APIキーなど)を渡すための標準的な仕組み。**`.env`ファイル**は、その環境変数を開発時にローカルで手軽に再現するためのテキストファイル形式（`KEY=value`を1行ずつ並べるだけ）で、`python-dotenv`のようなライブラリがこれを読み込んで実際のプロセスの環境変数として反映してくれる。APIキーのような機密情報を`.env`に書いて`.gitignore`で除外しておけば、ソースコード自体にはキーが一切現れないので、誤ってGitリポジトリへコミットしてしまう事故を防げる——これがまさにこのファイルの存在理由になっている。

### `load_env()` — 冪等な`.env`読み込み（`config.py:18-31`）

```python
_ENV_LOADED = False                                    # 18  モジュールレベルのフラグ

def load_env() -> None:
    global _ENV_LOADED
    if _ENV_LOADED:
        return                                          # 24-25  2回目以降は即return(冪等性)
    env_path = Path(__file__).resolve().parent / ".env" # 26  このファイルと同じディレクトリの.env
    load_dotenv(dotenv_path=env_path, override=False)   # 27  既存の環境変数は上書きしない
    _ENV_LOADED = True

load_env()                                              # 31  モジュールがimportされた瞬間に1回だけ実行
```

**冪等(idempotent)**という言葉は、「同じ操作を何度実行しても、結果が1回だけ実行したときと変わらない」性質を指す。この関数が冪等であるべき理由は単純で、`config`モジュールはプロジェクトの複数箇所（`agent_mbpp.py`と`llm/client.py`の両方など）から`import`される可能性があり、それぞれが独自に`load_env()`を呼んでも、ファイルの読み込みという処理自体は最初の1回しか実際には走らせたくないため。

`override=False`（27行目）が地味に重要——CIやDocker実行時にシェル側で既に環境変数がセットされている場合、リポジトリに置かれた`.env`の値でその値を**上書きしてしまわない**。`_ENV_LOADED`フラグ（18行目）はモジュール内グローバル変数なので、`config`モジュールが複数箇所から`import`されても、Pythonのモジュールキャッシュにより同一プロセス内では`load_env()`の中身（＝`python-dotenv`によるファイルI/O）は最初の1回しか実際には走らない。31行目でモジュールトップレベルに`load_env()`の呼び出しが直接書かれているため、「`import config`した時点で自動的に`.env`が読み込まれている」という副作用が保証される——呼び出し側のコードが明示的に`config.load_env()`を呼ぶ必要はない。

### `ProviderSpec` — 1プロバイダの静的定義とキー収集（`config.py:34-62`）

```python
@dataclass(frozen=True)
class ProviderSpec:
    name: str
    base_url: str
    api_key_env_prefix: str
    kind: str = "openai_compatible"          # "openai_compatible" または "gemini"

    def collect_api_keys(self) -> List[str]:
        keys = []
        primary = os.environ.get(self.api_key_env_prefix)   # 52
        if primary:
            keys.append(primary)                             # 53-54
        index = 2
        while True:
            value = os.environ.get(f"{self.api_key_env_prefix}_{index}")  # 57
            if not value:
                break                                          # 58-59
            keys.append(value)
            index += 1                                         # 60-61
        return keys
```

`frozen=True`（34行目）——`@dataclass`デコレータに渡せるオプションの1つで、これを付けると生成されたインスタンスのフィールドを後から書き換えようとすると例外になる（イミュータブル＝不変になる）。`ProviderSpec`は一度作られたら書き換え不可であることを型レベルで保証しており、`KNOWN_PROVIDERS`（後述）のようなモジュールレベルの共有リストの要素として複数箇所から参照されても、どこかのコードが誤ってフィールドを書き換えて他の利用箇所に影響を与える事故を構造的に防いでいる。

具体例として`.env`に

```
GROQ_API_KEY=key_a
GROQ_API_KEY_2=key_b
GROQ_API_KEY_3=key_c
```

と書かれている状態で`ProviderSpec("groq", ..., "GROQ_API_KEY", ...).collect_api_keys()`を呼ぶと: まず52行目で`GROQ_API_KEY`（接尾辞なし）を取得し`["key_a"]`。次に`index=2`から`while True`ループに入り、`GROQ_API_KEY_2`→`"key_b"`が見つかるので追加、`index=3`で`GROQ_API_KEY_3`→`"key_c"`も追加、`index=4`で`GROQ_API_KEY_4`が存在しないので`break`。最終的に`["key_a", "key_b", "key_c"]`が返る。もし`GROQ_API_KEY_2`が未設定で`GROQ_API_KEY_3`だけ設定されていた場合は、`index=2`の時点で`value`が空になり即座に`break`するため、`GROQ_API_KEY_3`は一切探索されず収集結果は主キーのみになる——「歯抜けの番号は末尾切り捨て」という単純だが明確な仕様。この`List[str]`が`llm/client.py`の`_ProviderSlot.api_keys`（Section 9.2）にそのまま渡り、ラウンドロビンでのキー切り替えの土台になる。

### `KNOWN_PROVIDERS` — 既知の無料枠プロバイダのレジストリ（`config.py:67-80`）

```python
KNOWN_PROVIDERS: List[ProviderSpec] = [
    ProviderSpec("openrouter", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY", "openai_compatible"),
    ProviderSpec("groq", "https://api.groq.com/openai/v1", "GROQ_API_KEY", "openai_compatible"),
    ProviderSpec("together", "https://api.together.xyz/v1", "TOGETHER_API_KEY", "openai_compatible"),
    ProviderSpec("fireworks", "https://api.fireworks.ai/inference/v1", "FIREWORKS_API_KEY", "openai_compatible"),
    ProviderSpec("google_ai_studio", "https://generativelanguage.googleapis.com/v1beta",
                  "GOOGLE_AI_STUDIO_API_KEY", "gemini"),
]
```

5行のリテラルなリストで、新しいプロバイダを"公式サポート"扱いにしたければここに1行足すだけ。`kind`フィールドが`"openai_compatible"`か`"gemini"`かで、`llm/client.py`の`_build_chat_provider()`（Section 9.2）がどちらの`ChatProvider`実装（Section 9.3/9.4）を使うかが決まる——Gemini以外は全て`"openai_compatible"`である点からも、Section 9.3冒頭で触れた「ほとんどの無料枠プロバイダが同じワイヤ形式を話す」という前提がそのままこのレジストリの構造に反映されていることが分かる。

### `resolve_provider(base_url)` — 未知のプロバイダも自動サポート（`config.py:83-107`）

```python
def _env_var_from_url(base_url: str) -> str:
    host = re.sub(r"^https?://", "", base_url).split("/")[0]          # 85  スキーム除去→ホスト部分だけ抽出
    host = re.sub(r"[^a-zA-Z0-9]+", "_", host).strip("_").upper()      # 86  英数字以外を_に、前後の_を除去、大文字化
    return f"{host}_API_KEY"                                           # 87

def resolve_provider(base_url: str) -> ProviderSpec:
    normalized = base_url.rstrip("/")                                  # 97
    for spec in KNOWN_PROVIDERS:
        spec_url = spec.base_url.rstrip("/")
        if normalized == spec_url or normalized.startswith(spec_url):  # 100  完全一致 or 前方一致
            return spec
    return ProviderSpec(                                                # 102-107  一致しなければ即席で合成
        name=normalized, base_url=base_url,
        api_key_env_prefix=_env_var_from_url(base_url), kind="openai_compatible",
    )
```

具体例を`_env_var_from_url`の変換過程まで追うと:

```
--provider-url "https://api.novita.ai/v3/openai"
  → re.sub(r"^https?://", "", ...) → "api.novita.ai/v3/openai"
  → .split("/")[0]                 → "api.novita.ai"                (85行目、パス部分を切り捨て)
  → re.sub(r"[^a-zA-Z0-9]+", "_", "api.novita.ai") → "api_novita_ai" (86行目、"."を"_"に)
  → .strip("_").upper()            → "API_NOVITA_AI"
  → f"{host}_API_KEY"              → "API_NOVITA_AI_API_KEY"          (87行目)
```

`KNOWN_PROVIDERS`のどれとも前方一致しなければ、URLのホスト名から機械的に環境変数名を組み立てて即席の`ProviderSpec`を作る——つまり「新しいプロバイダを追加するのにコード変更は一切不要、`.env`に対応する環境変数を1本用意するだけでよい」という要件を、コードを1行も書かずに満たしている。`normalized.startswith(spec_url)`（100行目）が完全一致だけでなく**前方一致**も許しているのは、同じベースURLの配下に複数のサブパス（例えば将来的にモデル種別ごとに`/v1/chat`と`/v1/embeddings`のようなURLが混在するケース）が現れても、ホスト部分が一致していれば同じ`ProviderSpec`として扱えるようにするための緩さ。

ただし`kind`は102-107行目で常に`"openai_compatible"`に固定されるため、Geminiのような非互換なAPI構造を持つ未知のプロバイダをURLとして渡しても正しく動かない——これは仕様として妥当なトレードオフで、「本当に異なるワイヤ形式」は`KNOWN_PROVIDERS`に明示的に登録するしかない設計になっている（`_env_var_from_url`はあくまで「認証ヘッダー方式と`/chat/completions`互換のペイロードさえ守っていれば」という前提の上に成り立つ推測にすぎない）。

## 11. `agent_mbpp.py` / `agent_swebench.py` — エージェント CLI

両者とも骨格は完全に共通で、行番号を対比すると差分の少なさが際立つ:

| 処理ステップ | `agent_mbpp.py` | `agent_swebench.py` |
|---|---|---|
| タスクファイル読み込み・バリデーション | 82-89行目 (`MBPPTaskInput.model_validate`) | 79-86行目 (`SWEBenchTaskInput.model_validate`) |
| SIGTERMハンドラ登録 | 91-99行目 | 93-99行目 |
| MCPプロキシ起動 | 109-110行目 | 101-105行目(コンテナ起動後) |
| `SandboxConfig`組み立て・`Sandbox`生成 | 112-122行目 | 107-113行目 |
| `build_system_prompt()` | 123行目 | 114行目 |
| `LLMClient.from_provider_url()` | 125行目 | 116行目 |
| `Orchestrator`生成・`run()` | 126-140行目 | 117-131行目 |
| 例外処理(`ShutdownRequested`/`Exception`) | 142-147行目 | 133-140行目 |
| `finally`クリーンアップ | 148-152行目 | 141-147行目 |
| `solution.json`書き出し | 154行目 | 149行目 |

タスクファイル読み込みの`try/except`ブロック(両ファイルとも冒頭)は、`Orchestrator`が一切関与しない最も早い失敗点であることに注意——ここで失敗した場合、タスクIDすらPydanticモデルからは得られていない(バリデーション自体が失敗している)ため、`error_solution("unknown", ...)`という**タスクID不明のまま**エラー結果を書き出す特別扱いになっている(`agent_mbpp.py:87`, `agent_swebench.py:84`)。

SIGTERMハンドラ(`handle_sigterm`)の詳細な仕組み——シグナルが届いた瞬間に`ShutdownRequested`が`BaseException`として送出され、実行中の任意の処理を強制的に中断させる設計と、その理由(moulinetteのSIGTERM→SIGKILL猶予10秒)——はSection 5で`orchestrator.py`側の`request_stop()`と対にして詳しく扱った。ここでは重複を避け、両CLIで**全く同じ9行**(`agent_mbpp.py:93-99` / `agent_swebench.py:93-99`)がコピーされている、という事実だけ指摘しておく。

### 予算の数値と、その置き場所

```python
# agent_mbpp.py:27-30
MAX_ITERATIONS = 10
MAX_INPUT_TOKENS = 6_000
MAX_OUTPUT_TOKENS = 1_500
TIMEOUT_SECONDS = 120

# agent_swebench.py:32-35
MAX_ITERATIONS = 30
MAX_INPUT_TOKENS = 300_000
MAX_OUTPUT_TOKENS = 10_000
TIMEOUT_SECONDS = 900
```

いずれもモジュールレベルの定数として2ファイルそれぞれの冒頭にハードコードされており、`config.py`のような共通設定ファイルには置かれていない——ベンチマークごとに別々のCLIファイルとして完全に分離されているので、値が違って当然という前提。入力トークン予算だけを見ても**MBPPはSWE-benchの1/50**しかない。理由は扱う情報量の非対称性にある: MBPPは問題文と関数シグネチャと数行のテストという固定サイズの入力で完結するのに対し、SWE-benchは`read_file`/`search_code`で実リポジトリのソースコードを何度もObservationとして読み込む必要があり、1回の`read_file`だけで数百〜数千トークンを消費しうる。`max_iterations`も3倍(10→30)、`max_execution_time_seconds`も3倍(20→60、Section 4のSandboxConfig比較表を参照)——「MBPPは小さく速く、SWE-benchは大きくじっくり」という設計思想が、予算のあらゆる軸に一貫して反映されている。

`OrchestratorConfig.max_time_seconds`に渡す値がそれぞれ`TIMEOUT_SECONDS - 10`(`agent_mbpp.py:134`)と`TIMEOUT_SECONDS - 30`(`agent_swebench.py:125`)で、差し引く秒数が異なる点も見落としやすい。MBPPは`sandbox.close()`/`mcp_proxy.close()`だけで後片付けが済むのに対し、SWE-benchは追加で`container.cleanup()`(`stop(timeout=5)`→`remove(force=True)`、Section 13参照)が必要になる分、安全余白を大きく(10秒→30秒)取っている。

### `agent_mbpp.py`固有の設定

- MCPサーバーはローカルサブプロセスとして`f"{sys.executable} {MCP_TOOLS_SCRIPT}"`(`agent_mbpp.py:110`)、すなわち**今動いているのと同じPythonインタプリタ**で`mcp_tools_mbpp.py`をstdio起動する。`sys.executable`を使うのは、`uv run`が作った仮想環境のPythonと違うインタプリタ(例えばシステムの`python3`)を誤って使ってしまい、依存パッケージが見つからない事故を防ぐため。

  ここで言う「stdio起動」について: プロセス同士が情報をやり取りする方法は大きく分けて2種類ある。1つは同じマシン上のプロセス間で、標準入力(stdin)・標準出力(stdout)というパイプをそのまま通信路として使う方法(stdio通信)——親プロセスが子プロセスをサブプロセスとして起動し、子の標準入出力を親側から読み書きできるように繋ぎ変えるだけでよいので、ネットワークの設定が一切不要で、同一ホスト内で完結する用途に向く。もう1つはHTTPのようなネットワーク越しの通信で、別のマシン(あるいは同じマシンでも独立して起動済みのサーバープロセス)に接続する場合に使う。`mcp_tools_mbpp.py`はローカルサブプロセスとして毎回新しく起動されるので前者(stdio)、Section 8.3で見た`MCPToolProxy`の`http_url`引数を使うケースは後者にあたる——同じ`MCPToolProxy`がどちらのトランスポートも受け付けられるように作られているのは、このプロジェクトが「未知のMCPサーバーに接続される」ことを前提にしているため(Section 3)。
- `tool_env = {"AGENT_SMITH_TEST_IMPORTS": json.dumps(task.test_imports)}`(`agent_mbpp.py:109`)を`MCPToolProxy(..., env=tool_env)`に渡す。これが`mcp_tools_mbpp.py`の`_test_imports()`(Section 12)が読む環境変数の**唯一の注入経路**。
- `max_execution_time_seconds=20`(`agent_mbpp.py:119`)には、直上の116-118行目に実運用で踏んだ地雷の跡がコメントとして残っている: `mcp_tools_mbpp.py`内部の`run_tests()`が使う10秒のサブサンドボックスタイムアウトより確実に大きくしておかないと、正しいが少し遅いテスト実行に対して外側のサンドボックスアラームが先に発火し、正解を誤ってタイムアウト扱いにしてしまう(実プロバイダに対するライブスモークテストで発見)。

### `agent_swebench.py`固有の設定

- `container = SweBenchContainer(task.docker_image)`→`container.start(eval_script=..., tools_file=TOOLS_FILE)`(`agent_swebench.py:102-104`)でDockerコンテナを起動してから、`MCPToolProxy(stdio_command=container.mcp_stdio_command())`(105行目)を作る——コンテナが起動してMCPツールサーバーが応答可能になっていることが、MCPプロキシ生成の前提条件になっている点がMBPP版と異なる(MBPP版はローカルサブプロセスなので起動順の制約が緩い)。
- `allowed_directories=["/testbed", str(SCRATCH_DIR)]`(`agent_swebench.py:109`)。`/testbed`はコンテナ内のパスに見えるが、実際にはSection 13で説明する通り**サンドボックス(Pythonインタプリタ)自体はホスト側で動いている**ため、これはホスト上の`/testbed`ディレクトリを指す設定であり、コンテナ内の`/testbed`とは別物である点に注意(コンテナ内のファイルへのアクセスはすべて`mcp_tools_swebench.py`経由、つまり`docker exec`をまたいだMCPツール呼び出しを通してのみ行われる)。
- `except ShutdownRequested`の直上コメント(`agent_swebench.py:134-136`)が明言する通り、SIGTERMが`Orchestrator.run()`自身がガードしていない区間(例えばサンドボックス実行の最中)で届いた場合でも、この`except`節に正常に到達することで`finally`節の`container.cleanup()`がmoulinetteのSIGTERM→SIGKILL猶予期間内に確実に実行される。

---

## 12. `mcp_tools_mbpp.py` / `mcp_tools_swebench.py` — MCP ツールサーバー

エージェント本体（`orchestrator.py`＋`sandbox/`）から見ると、これらは「MCPサーバーという名の**別プロセス**」でしかない。Section 8.3で見た通り、MCPは「LLMエージェントが外部の道具(ツール)を呼び出すための標準規格」だった——ここで登場するのはその**サーバー側**の実装、つまり実際に呼び出される道具そのものを提供する側。どちらも `mcp.server.fastmcp.FastMCP` を使い、`@mcp.tool()` デコレータを付けた素のPython関数を1つ書くだけで、それが自動的にJSON Schema付きのMCPツールとして公開される。

「デコレータを1つ付けるだけで自動的にツールとして公開される」という部分を少し補足すると: Pythonの**デコレータ**とは、`@何か`という記法で関数の直前に書き、その関数を別の関数でラップ(加工)する仕組みのこと。`@mcp.tool()`が付いた関数は、関数名・引数の型ヒント・docstring(関数の説明文)を`FastMCP`が実行時に自動的に読み取り、「この名前で、こういう型の引数を受け取り、こういう説明を持つツールです」という情報(JSON Schema)を組み立てて登録する。開発者は普通のPython関数を書くだけでよく、MCPのワイヤ形式(通信データの形式)を手で組み立てる必要はない——`mcp_tools_mbpp.py:21`で`mcp = FastMCP("agent-smith-mbpp-tools")`とサーバーインスタンスを作り、`mcp_tools_mbpp.py:41`の`@mcp.tool()`がその1個だけのツールをこのサーバーに登録している。`--http <port>` を渡せば streamable HTTP、渡さなければ stdio（標準入出力）で待ち受ける——起動オプション以外の設計思想は2ファイルで大きく異なる。

### `mcp_tools_mbpp.py`（109行、ツールは1個だけ）

MBPPは「短い関数を書いて、公開テストが通るか確認する」だけのタスクなので、ツールは`run_tests(code: str, test_list: List[str]) -> str`のたった1つ（Section 4.3.2）。

```python
# mcp_tools_mbpp.py:55-90
imports_prefix = "\n".join(_test_imports())
marker = f"__AGENT_SMITH_MBPP_PASS_{secrets.token_hex(16)}__"
full_code = (
    (imports_prefix + "\n" if imports_prefix else "")
    + code
    + "\n" + "\n".join(test_list)
    + f"\nprint({marker!r})"
)
sandbox = None
try:
    sandbox = Sandbox(SandboxConfig(
        authorized_imports=DEFAULT_AUTHORIZED_IMPORTS,
        allowed_directories=[],
        max_execution_time_seconds=10,
        max_memory_mb=256,
    ))
    try:
        output = sandbox.run(full_code)
    except (FinalAnswer, KeyboardInterrupt, SystemExit) as exc:
        output = f"[Error] {type(exc).__name__}: {exc}"
except Exception as exc:
    output = f"[Error] {type(exc).__name__}: {exc}"
finally:
    if sandbox is not None:
        sandbox.close()

success = marker in output
if success:
    output = output.replace(marker, "").rstrip()
return json.dumps({"success": success, "output": output})
```

ここで起きていることを順に追うと分かりやすい:

1. **候補コード＋テストのassert文＋予測不可能なマーカー文字列**を1つのPythonスクリプトに連結する（57-63行目）。マーカーは`secrets.token_hex(16)`（暗号論的に安全な乱数）から作られるので、候補コードが`print("__AGENT_SMITH_MBPP_PASS__")`のようなハードコードで偽装することは事実上不可能。
2. それを**エージェント本体が使うのとは別の、使い捨ての`Sandbox`インスタンス**（`allowed_directories=[]` — ファイルアクセスなし、10秒タイムアウト、67-74行目）で実行する。つまり「候補コードを検証するためのコード」自体が、エージェント本体と全く同じ多層防御（Section 8）の中で動く——LLMが書いたコードは`run_tests`を介してもMCPサーバー自身のホストファイルシステムやネットワークには一切触れられない。
3. **内側の`try`が`FinalAnswer`/`KeyboardInterrupt`/`SystemExit`だけを個別に捕捉している**（77-78行目）点が地味だが重要: `Sandbox.run()`はこれら3つを通常の`except Exception`では握りつぶさずそのまま伝播させる設計（Section 8.1）のため、候補コードがうっかり`final_answer(...)`を呼んでしまった場合でも、それがこの`run_tests`ツール自体をクラッシュさせて`success`判定不能になることなく、`[Error] FinalAnswer: ...`というObservationとして扱われる。
4. 全部の`assert`文を通過すれば最後の`print(marker)`まで到達し、出力にマーカー文字列が含まれる（含まれていれば`success: true`）。1つでも`assert`が失敗すれば例外でスクリプトが途中終了し、マーカーは出力されない（`success: false`）。
5. `_test_imports()`（24-38行目）が環境変数`AGENT_SMITH_TEST_IMPORTS`（`agent_mbpp.py`が`MBPPTaskInput.test_imports`から詰め込んだJSON文字列、Section 11参照）を読み、候補コードの**前**に自動的に前置する。`json.loads`が失敗した場合は例外を上げず空リストにフォールバックする（36-37行目）——MCPツールの設定読み込み自体がクラッシュの原因になってはならない、という一貫した姿勢。
6. **成功時のみ**マーカー文字列を出力から除去する（88-89行目）。失敗時にマーカーを残しているのは、`success: false`のときの`output`は「テストが失敗した理由の生のtraceback」であり、そこにマーカーが混入していても実害がない（というよりマーカーはそもそも出力されていない）ため、無駄な文字列処理を省いている。
7. `85-86行目`のタイムアウトメッセージ書き換え（`"Execution exceeded 10s"`→`"Execution timed out after 10s"`）は、サンドボックス側の汎用的なタイムアウト文言を、このツール固有の文脈（10秒はこのMCPサーバーが指定した値）に合わせてより分かりやすく言い換えるための、小さな後処理。

`test_mcp_tools_mbpp.py`はこの実装を直接関数呼び出しでテストしている（`@mcp.tool()`デコレータは元の関数呼び出し可能性を保持するため、MCPトランスポート無しでテストできる——デコレータはあくまで「関数に付加情報を付けて登録する」だけで、関数自体を`run_tests(code=..., test_list=...)`のように普通に呼び出す能力は失われない）: `test_run_tests_uses_task_test_imports_env_var`が環境変数注入の経路を、`test_run_tests_syntax_error_in_candidate`/`test_run_tests_infinite_loop_times_out`が異常系を、`test_run_tests_rejects_unauthorized_host_import`が「候補コードが`os`のような未許可importを試みても`run_tests`経由でブロックされる」ことを検証している——つまり「サンドボックスの防御は`run_tests`を経由しても素通りできない」ことの回帰テスト。

### `mcp_tools_swebench.py`（412行、必須9ツールすべて）

SWE-benchは実リポジトリの調査・修正が必要なので、ツール数も複雑さも桁違い。しかし設計の芯は1つ: **`TESTBED_PATH`環境変数さえ設定されていれば動く**（`_testbed_root()`、29-37行目）、Docker固有のロジックを一切持たないプレーンなファイルシステム/subprocess操作の集合体、という点（Section 4.4）。同じコードがホスト上のベアなチェックアウトでも、コンテナの中でも変わらず動く。

#### パスの脱出防止 — `_resolve_within_testbed()` / `_validate_glob_pattern()` / `_matching_files()`

先に「glob(グロブ)パターン」という言葉について: シェルでよく見る`*.py`や`**/*.py`のような、ファイル名やパスを**ワイルドカード(あいまい一致)で表現する記法**のことをglobパターンと呼ぶ。`*`は「区切り(`/`)を跨がない任意の文字列」、`**`は「区切りを跨いでもよい任意の階層」にマッチする、というのがよくある慣習(Pythonの`Path.glob()`/`Path.rglob()`もこの記法に従う)。`search_code`のようなツールが「あるパターンに一致する全ファイル」を探すために、この記法をそのままLLMからの入力として受け取っている。

```python
# mcp_tools_swebench.py:74-116
def _resolve_within_testbed(filepath: str) -> Path:
    root = _testbed_root()
    candidate = Path(filepath)
    resolved = candidate if candidate.is_absolute() else root / candidate
    resolved = resolved.resolve()               # シンボリックリンク・".."を解決
    if not _is_within(root, resolved):
        raise ValueError(f"'{filepath}' resolves outside the repository root {root}")
    return resolved

def _validate_glob_pattern(pattern: str) -> None:
    pattern_path = Path(pattern)
    if not pattern or pattern_path.is_absolute() or ".." in pattern_path.parts:
        raise ValueError("glob pattern must be relative and must not contain '..'")

def _matching_files(directory: Path, pattern: str, recursive: bool) -> list:
    _validate_glob_pattern(pattern)
    root = _testbed_root()
    candidates = list(directory.rglob(pattern) if recursive else directory.glob(pattern))
    matches = []
    for candidate in candidates:
        resolved = candidate.resolve()
        if not _is_within(root, resolved):
            raise ValueError(f"glob pattern '{pattern}' matched a path outside {root}")
        if resolved.is_file() and ".git" not in resolved.parts:
            matches.append(resolved)
    return matches
```

`read_file`/`edit_file`/`list_files`のような単発パス引数は`_resolve_within_testbed()`（74-82行目）が守る一方、`search_code`のような**globパターン展開結果**は別経路のチェックが必要になる——`_validate_glob_pattern()`（90-94行目）が「パターン文字列自体」が絶対パスや`..`を含んでいないかを先に拒否し、それでも`directory.glob(pattern)`が実際に返した**展開後のファイルパス**を`_matching_files()`（97-116行目）がもう一段`_is_within()`で検証する。この二段構えが必要な理由は、`pattern`自体は無害に見えても、シンボリックリンクを経由した展開結果が`TESTBED_PATH`の外を指しうるため（`.git`ディレクトリの除外も同じ関数の114行目で行われる——`.git`内部のオブジェクトファイルまで`search_code`の対象にするとノイズと出力サイズの両方が無駄に増える）。`test_mcp_tools_swebench.py`の`test_list_files_rejects_symlink_target_outside_testbed`がこの経路をピンポイントで検証する。

#### `edit_file` の「完全一致1箇所のみ」制約（125-191行目）

```python
occurrences = content.count(old_str)
if occurrences == 0:
    return f"[Error] old_str not found in {filepath}"
if occurrences > 1:
    return (f"[Error] old_str is not unique in {filepath} "
            f"({occurrences} occurrences) - include more context")
new_content = content.replace(old_str, new_str, 1)
path.write_text(new_content)
if path.suffix == ".py":
    result = subprocess.run(["python3", "-m", "py_compile", str(path)],
                             capture_output=True, text=True)
    if result.returncode != 0:
        return f"[EditSyntaxError] Edit applied, but introduced a syntax error:\n{result.stderr}"
return f"Edit applied to {filepath}"
```

曖昧な置換を許すと「LLMが意図した箇所と違う場所を書き換えてしまう」事故につながるため、**あいまいさを検出したら実行せず、LLMに前後関係を増やして書き直させる**という設計（`test_edit_file_rejects_ambiguous_match`で検証）。`.py`ファイルへの編集は書き込み後に即座に`py_compile`でコンパイルチェックされ（184-189行目）、構文エラーを混入させた場合は**編集自体は既に適用された状態のまま**`[EditSyntaxError]`が返る——ロールバックはしない。これは「編集はもう起きた、次のターンでLLMがそれを踏まえて追加修正すべき」というThought→Code→Observationループの哲学(常に実際に起きたことをそのまま見せる、Section 6)と一貫している。

ここで使われている`subprocess.run(...)`について補足しておく: `subprocess`はPython標準ライブラリで、OSに対して**別のコマンド/プログラムを子プロセスとして起動する**ための機能。`subprocess.run(command, capture_output=True, text=True)`は、指定したコマンド(ここでは`python3 -m py_compile <path>`——`py_compile`はファイルを実行せずコンパイル(構文チェック)だけを行う標準モジュール)を1回実行し、完了するまで待ち、その標準出力・標準エラー出力・終了コード(`returncode`)をまとめて返す。終了コードは「0なら成功、0以外なら何らかの失敗」という慣習がUNIX系コマンド全般にある——ここでは明示していないが、`subprocess.run`には`check`という引数もあり、`check=True`にすると終了コードが非ゼロだったときに自動的に例外(`CalledProcessError`)を送出してくれる。このコードでは`check`を指定せず(=デフォルトの`False`)、代わりに`result.returncode != 0`を自分で判定している——例外にせず、失敗を`[EditSyntaxError] ...`という通常の文字列Observationとして返したいためで、Section 8.1で見た「候補コードの実行エラーは例外ではなく文字列として返す」という一貫したポリシーがここにも表れている。

#### `run_command` の二段階kill（311-354行目）

```python
process = subprocess.Popen(command, shell=True, cwd=cwd,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, start_new_session=True)
try:
    stdout, stderr = process.communicate(timeout=120)
except subprocess.TimeoutExpired:
    os.killpg(process.pid, signal.SIGTERM)
    try:
        stdout, stderr = process.communicate(timeout=2)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        stdout, stderr = process.communicate()
    return "[Error] command timed out after 120s"
```

`subprocess.Popen(...)`は`subprocess.run(...)`より一段低レベルなAPIで、コマンドを起動した直後に**待たずに**戻ってくる(=`Popen`＝"Process open"、プロセスを開いたハンドルを返すだけ)。ここでは`process.communicate(timeout=120)`で「最大120秒だけ完了を待つ」という制御を自前で行いたいため、待機まで自動でやってしまう`subprocess.run`ではなく`Popen`を使っている。

`start_new_session=True`で起動することが何を意味するかも補足する: LLMが指示するコマンド(例えば`shell=True`が示す通りシェル経由で実行される)は、それ自身がさらに子プロセスを生み出すことがある(`sleep 999 &`のようにバックグラウンドジョブを起動する、パイプで複数コマンドを繋ぐ、等)。`start_new_session=True`は、起動したプロセスを**新しいプロセスグループのリーダー**にする指定——これにより、その後に生まれた孫プロセスも含めた「グループ全体」に対して、`os.killpg(process.pid, signal)`という1回の呼び出しでシグナルをまとめて送れるようになる(`killpg`＝"kill process group"）。これが無いと、直接の子プロセスだけを`SIGTERM`しても孫プロセスが生き残ってしまう(タイムアウト後も居座るゾンビ/迷子プロセスの原因になる)。まず`SIGTERM`で2秒だけ猶予を与え、それでも生きていれば`SIGKILL`で強制終了する二段構え（`sandbox/isolated_process.py`の親プロセスがワーカーに対して行う終了処理と同じパターン、Section 8.2）。

#### `get_patch()` と `_cap_output()` の非対称性（51-71行目、377-392行目）

```python
def _cap_output(text: str) -> str:
    if len(text) <= TOOL_OUTPUT_LIMIT_CHARS:   # 20,000文字
        return text
    omitted = len(text) - TOOL_OUTPUT_LIMIT_CHARS
    return text[:TOOL_OUTPUT_LIMIT_CHARS] + f"\n[TruncatedToolOutput] {omitted} ..."
```

`read_file`/`list_files`/`search_code`/`run_command`/`run_tests`はすべてこの`_cap_output()`を通す——巨大リポジトリでの検索や冗長なテスト出力が、SWE-benchの累積300,000トークン予算（Section 11）を1ステップで大きく消費しかねないため。一方`get_patch()`（377-392行目、`git -c core.fileMode=false diff`の生出力をそのまま返す）だけは**意図的にこの関数を通さない**——その返り値は`final_answer(get_patch())`の直接の引数になりうるため、切り詰めると壊れた（`git apply`不能な）diffをそのまま提出してしまうことになる。`docstring`にもその理由が明記されている。`core.fileMode=false`を指定しているのは、コンテナ内でのファイル操作によってパーミッションビット（実行権限など）だけが変わってしまい、意味のある変更が何もないのに`diff`にノイズが混ざる事故を防ぐため。`test_get_patch_is_never_truncated`がこの非対称性そのものを検証している。

---

## 13. `docker_runner.py` — Docker ブリッジ

`SweBenchContainer`がタスクのDockerイメージのライフサイクル全体を管理する。まずDockerの基礎用語を2つ整理しておく:

- **イメージ(image)**とは、アプリケーションとその実行に必要な一式(OSの最小構成・ライブラリ・ファイル)をまとめた、**読み取り専用のひな型**のこと。ファイルのスナップショットのようなもので、それ自体は「動いていない」。
- **コンテナ(container)**とは、そのイメージから実際に**起動した、動いている実行環境**のこと。同じイメージから何個でもコンテナを起動できる(同じひな型のコピーを何個も動かすイメージ)。コンテナは、ホストOSのカーネルを共有しながらも、ファイルシステムやプロセス空間が(名前空間によって、Section 8.2で説明したのと同種の仕組みで)隔離されている。

このクラスが実装しているのはSection 4.4の**アプローチ(b)**: サンドボックス（Pythonインタプリタ）自体はホストに置いたまま、**MCPツールサーバーのプロセスだけ**を`docker exec`でコンテナの中に立てる。`mcp_tools_swebench.py`自体はDockerを意識しないので、同じコードがベアなホストチェックアウトでもコンテナ内でも動く。

### `start()` の実行順序（40-53行目）

```python
def start(self, eval_script: str, tools_file: Path) -> None:
    self._client.images.pull(self.docker_image)                              # 41
    self._container = self._client.containers.run(
        self.docker_image, command="tail -f /dev/null", detach=True)          # 42-44
    self._write_into_container(eval_script, EVAL_SCRIPT_PATH_IN_CONTAINER)     # 46
    self._write_into_container(tools_file.read_text(), TOOLS_PATH_IN_CONTAINER)  # 47
    self._bootstrap_dependencies()                                             # 49
```

`command="tail -f /dev/null"`でコンテナを起動しているのは、SWE-benchのDockerイメージ自体は（テスト実行用であって）常駐サーバーではないため、何もしなければコンテナがすぐ終了してしまう——`tail -f /dev/null`は「何も出力せずに永遠にブロックし続ける」ダミーコマンドとして、`docker exec`で後から中に入れる状態を維持するための定石。

ここで登場する`docker exec`と、次の節で出てくる`docker cp`の違いも整理しておく。**`docker exec`**は、**起動中のコンテナの内部で新しいコマンドを実行する**操作——コンテナがすでに動いていることが前提で、その中に入って何かを行う(ちょうどSSHでリモートサーバーに接続してコマンドを打つのに近い)。**`docker cp`**は、ホストとコンテナの間で**ファイルをコピーする**操作——コンテナが動いているかどうかに関わらず使える、ファイル転送専用のコマンド。この2つの使い分けが、次の`_write_into_container()`の設計判断の核になる。

### `_write_into_container()` — `docker cp`のUIDバグ回避策（55-82行目）

```python
def _write_into_container(self, content: str, container_path: str) -> None:
    subprocess.run(
        ["docker", "exec", "-i", self._container.id, "sh", "-c",
         f"cat > {shlex.quote(container_path)}"],
        input=content, text=True, check=True, timeout=30,
    )
```

`docker cp`はtarベースのコピーで、抽出したファイルをホストのUIDに`lchown`しようとする。ここで言う**UID(User ID)**とは、OSがユーザーを識別するための数値のことで、Linuxのファイルにはそれぞれ「所有者はこのUID」という情報が付いている。コンテナは通常、ホストとは独立したUID体系を持つが、ホスト環境によっては「コンテナ内のUIDを、ホスト側の別のUID範囲に自動的にマッピング(対応付け)する」設定(user namespace remapping、`/etc/subuid`で指定される範囲)が入っていることがある。ユーザーごとのsubuid/subgidレンジ（`/etc/subuid`）でコンテナのUIDリマッピングを行っているホスト環境では、ホストユーザーのUIDがそのレンジ外に落ちて`Error response from daemon: failed to Lchown ...: invalid argument`で失敗する（実際にこのホストで遭遇し、`BENCHMARK_REPORT.md`に記録されている。moulinette自身の`validate swebench`も内部で同じエラーを踏むことが確認されている＝このプロジェクト固有のバグではなくホスト環境依存の既知の問題）。

対策として、`docker cp`を使わず`docker exec -i <id> sh -c 'cat > <path>'`に標準入力で内容を流し込む方式にする。これは`docker exec`が「コンテナの内部で」コマンドを実行する(=書き込みもコンテナ内部のプロセスとして行われる)ため、ホスト側のUIDマッピングというレイヤーを一切経由せずに済む——`docker cp`のようなホスト⇔コンテナ間のファイル転送特有の変換処理を、そもそも発生させない、という回避策になっている。この`docker exec`呼び出しは`docker-py`クライアントではなく`docker`CLIを直接呼んでいるため、`docker-py`クライアントのデフォルトタイムアウトを継承しない ── そこで明示的に`timeout=30`を指定し、コンテナが応答不能になった場合でも`container.start()`（＝エージェント全体）を無期限にハングさせないようにしている。`subprocess.run(..., check=True, ...)`——ここでは`check=True`が指定されている点にも注目——コンテナへの書き込みが失敗したのに気づかずそのまま処理を続けてしまう(例えば空のファイルのまま後続処理が進む)方が危険なので、終了コードが非ゼロなら即座に例外を送出させ、失敗をその場で顕在化させている。

### `_bootstrap_dependencies()` — ベストエフォートのセットアップ（84-120行目）

```python
check = subprocess.run(["docker", "exec", container_id, "python3", "-c", "import mcp"],
                        capture_output=True, timeout=30, check=False)
if check.returncode == 0:
    return
install = subprocess.run(
    ["docker", "exec", container_id, "pip", "install", "--quiet",
     "mcp>=1.2.0,<2", "pydantic>=2"],
    capture_output=True, timeout=300, check=False)
if install.returncode != 0:
    raise RuntimeError(
        "Could not install the MCP server's dependencies inside the container "
        f"(likely no network access in this image): {install.stderr.decode(errors='replace')}")
```

まず`import mcp`が既に通るか確認し（多くのSWE-benchイメージには入っていない）、通らなければ`pip install`でその場にブートストラップする。この最初の確認コマンドは`check=False`（デフォルト）で呼ばれている点に注目——ここでの非ゼロ終了コード(`import`失敗)は「異常事態」ではなく「想定内の分岐(まだインストールされていない)」なので、例外にせず`returncode`を自分で見て次のステップに進む、という判断。ネットワークが無いイメージではこれが失敗するが、それは例外を握りつぶさず`RuntimeError`として`agent_swebench.py`側に伝播し、そこで「グレースフルなエージェントエラー」（クラッシュではなく`solution.json`へのエラー記録）として処理される——General Rulesの「すべてのエラーはgracefulに処理されなければならない」要件に対応。

### `mcp_stdio_command()` と `cleanup()`（122-147行目）

```python
def mcp_stdio_command(self) -> str:
    return (f"docker exec -i -e TESTBED_PATH={TESTBED_PATH_IN_CONTAINER} "
            f"-e AGENT_SMITH_EVAL_SCRIPT={EVAL_SCRIPT_PATH_IN_CONTAINER} "
            f"{container_id} python3 {TOOLS_PATH_IN_CONTAINER}")

def cleanup(self) -> None:
    if self._container is None:
        return
    try:
        self._container.stop(timeout=5)
    except Exception:
        pass
    try:
        self._container.remove(force=True)
    except Exception:
        pass
    self._container = None
```

`mcp_stdio_command()`が組み立てる文字列がそのまま`MCPToolProxy(stdio_command=...)`（`agent_swebench.py:105`）に渡され、`-e`フラグで`TESTBED_PATH`/`AGENT_SMITH_EVAL_SCRIPT`を環境変数として注入する——これが`mcp_tools_swebench.py`側の`_testbed_root()`/`_eval_script_path()`が読む値の出所そのもの。注目したいのは、この文字列自体が「`docker exec`でコンテナの中の`python3 <MCPサーバースクリプト>`をstdio起動する」というコマンドになっていること——つまり`MCPToolProxy`から見れば、相手がベアなローカルプロセスなのか、Docker越しのコンテナ内プロセスなのかの違いは、「どんなコマンド文字列を`stdio_command`として渡すか」だけに閉じ込められている(Section 11の`agent_mbpp.py`がローカルの`sys.executable`をそのまま使っていたのと対比すると分かりやすい)。`cleanup()`は`stop`/`remove`の両方を個別の`try/except Exception: pass`で包んでいる（例外を握りつぶす）。これは「後始末の失敗でエージェント全体をクラッシュさせない」という設計判断で、Section 8.1の「通常のコードエラーで例外を投げることは絶対にない」というサンドボックスの方針と同じ発想がここにも表れている——ただし対象は「信頼できないコードの実行結果」ではなく「後片付け処理自体の失敗」である点が異なる。`self._container = None`で内部状態をリセットするのは、`cleanup()`が万一2回呼ばれても（例えば`finally`節と何らかの例外パスの両方から）2回目は即座に early return するための保険（137-138行目）。

---

## 14. 設定ファイル・補助ファイル

### `pyproject.toml` — 依存関係とプロジェクトメタデータ（`pyproject.toml:1-35`）

`pyproject.toml`はPythonプロジェクトの標準的な設定ファイル形式(TOML＝Tom's Obvious, Minimal Language、`key = value`を中心とした人間に読みやすい設定ファイル記法)で、「このプロジェクトにはどんな依存パッケージが必要か」「どうビルドするか」「コマンドラインツールとして何を公開するか」などをまとめて宣言する。

```toml
[project]
requires-python = "==3.10.*"                                          # 6

dependencies = [                                                       # 8-14
    "pydantic>=2.13.5",
    "python-dotenv>=1.2.3",
    "requests>=2.34.2",
    "mcp>=1.2.0,<2",
    "docker>=7.1.0",
]

[dependency-groups]
dev = ["flake8>=7.3.0", "mypy>=2.3.1", "pytest>=9.1.1"]                # 17-21

[project.scripts]
sandbox = "sandbox.cli:main"                                           # 24

[build-system]
requires = ["uv_build>=0.12.6,<0.13"]                                  # 27
build-backend = "uv_build"                                             # 28

[tool.uv.build-backend]
module-name = "sandbox"                                                # 31
module-root = ""                                                       # 32

[tool.pytest.ini_options]
testpaths = ["tests"]                                                  # 35
```

`uv` で管理されており、ランタイム依存はわずか5つ——`pydantic`(モデル契約)、
`python-dotenv`(`.env`読み込み)、`requests`(LLM API呼び出し)、`mcp`(MCPクライアント/
サーバーSDK)、`docker`(`docker_runner.py`用の`docker-py`)。依存が少ないほど攻撃対象面
(サプライチェーンリスク)も小さくなる、という意図が読み取れる。`requires-python = "==3.10.*"`
はレンジ指定ではなく**ピン留め**——`match`文の有無やasyncioの挙動差など、マイナーバージョンの
違いがサンドボックス(Section 8)のような低レベルなコードの挙動に影響しうるため、
「動作確認したバージョンだけを保証する」という選択。`[project.scripts]` の1行により
`uv run sandbox` というコマンドが使えるようになる仕組みも、実体は`sandbox/cli.py`の`main()`
関数を指すエントリポイント定義でしかない。`[build-system]`/`[tool.uv.build-backend]`
(26-32行目)は`uv`独自のビルドバックエンド`uv_build`を使い、`module-name = "sandbox"`で
パッケージ名を`sandbox`に固定している——リポジトリのトップレベルには`agent_mbpp.py`等の
スクリプトも並んでいるが、実際にwheelとしてパッケージ化・配布可能な単位は`sandbox/`
パッケージ1つだけ、という意図が読み取れる。`[tool.pytest.ini_options] testpaths = ["tests"]`
(34-35行目)は「`pytest`をリポジトリのどこから実行しても`tests/`ディレクトリだけを
収集対象にする」設定で、後述の`conftest.py`(importパス解決)と役割分担している:
`testpaths`は「何を集めるか」、`conftest.py`は「集めたテストが依存モジュールを解決できるか」
を担う、別レイヤーの問題を別ファイルで解決している。

### `Makefile` — よく使う操作のショートカット（`Makefile:1-35`）

`Makefile`は`make <ターゲット名>`という短いコマンドで、あらかじめ定義しておいた一連のシェルコマンドをまとめて実行できるようにするビルドツールの設定ファイル。ここでは本来の「ビルド」用途ではなく、開発時によく使う操作(依存インストール・実行・lint・テストなど)へのショートカットとして使われている。

```makefile
.PHONY: install run debug sandbox clean fclean lint lint-strict test    # 1

install:                                                                # 3-4
	uv sync

run:                                                                    # 6-7
	uv run python -m agent_mbpp --help

debug:                                                                  # 9-10
	uv run python -m pdb -m agent_mbpp --help

sandbox:                                                                # 12-13
	uv run sandbox

clean:                                                                  # 15-19
	find . -type d -name "__pycache__" -not -path "./.venv/*" -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
	find . -type f -name "*.pyo" -delete
	rm -rf .mypy_cache .pytest_cache .coverage htmlcov

fclean: clean                                                           # 21-22
	rm -rf .venv

lint:                                                                   # 24-28
	uv run flake8 --exclude=.venv .
	uv run mypy --exclude .venv --exclude models.py --explicit-package-bases \
		--warn-return-any --warn-unused-ignores --ignore-missing-imports \
		--disallow-untyped-defs --check-untyped-defs .

lint-strict:                                                            # 30-32
	uv run flake8 --exclude=.venv .
	uv run mypy --exclude .venv --exclude models.py --strict .

test:                                                                   # 34-35
	uv run pytest -v
```

`lint` が `mypy` に渡している大量のフラグに設計判断が見える: `--exclude models.py` は
「moulinetteからのコピーなので型エラーがあってもこちらでは直さない(直せない)」という
Section 4の「形は編集禁止」ルールがそのままツール設定にも反映されたもの。
`--disallow-untyped-defs --check-untyped-defs` は「型注釈のない関数を書かせない」厳格運用。
`lint-strict`(30-32行目)はさらに `mypy --strict` を使う、より厳しい別バリアント——
日常的な`lint`はCI/開発ループで気軽に回せる速さを優先し、`lint-strict`はリリース前などに
手動で回す厳格モード、という2段構えになっていると読める。`clean`(15-19行目)の1行目、
`find . -type d -name "__pycache__" -not -path "./.venv/*" -exec rm -rf {} +` の
`-not -path "./.venv/*"` が地味に重要——`.venv`配下には依存パッケージの`__pycache__`が
大量に存在するが、それを毎回消しては`uv`が次回起動時に再コンパイルする無駄が生じる
(`fclean`のように`.venv`ごと消す場合は当然問題ないが、`clean`は「速い開発ループ用の
軽い掃除」という位置づけなので、依存関係のキャッシュには触れない設計)。`fclean: clean`
という依存チェーン(21-22行目)は、Makefileのターゲット依存機能を使って「まず`clean`を
実行してから、追加で`.venv`ごと消す」処理を1行で表現している。

### `.env.example` — 環境変数テンプレート（`.env.example:1-17`）

```
# Copy this file to .env and fill in real keys. .env is gitignored - never commit real
# keys, and never hardcode them in source (instant security failure per Section 6.3).
#
# Multi-token management (Section 5.6.1): add _2, _3, ... suffixes for extra keys
# per provider; the LLM client rotates between them automatically.

OPENROUTER_API_KEY=
OPENROUTER_API_KEY_2=

GROQ_API_KEY=

TOGETHER_API_KEY=

FIREWORKS_API_KEY=

GOOGLE_AI_STUDIO_API_KEY=
```

キーの値は書かれていない、いわば「穴埋め用の空フォーム」。`config.py`(Section 10)の
`ProviderSpec.api_key_env_prefix` と1対1で対応する変数名になっているので、このファイルを見れば
`config.py` のどのプロバイダがサポート対象か一目で分かる。`OPENROUTER_API_KEY_2`だけが
例として明示されているのも意図的——`config.py:collect_api_keys()`(Section 10)の
「`_2`, `_3`, ...と連番で増やせる」という仕様を、コメントで説明するだけでなく実際の
変数名としても1つ実演している。コメントの「`.env`は
gitignore済み、ソースへのハードコードは即セキュリティ違反」という一文は、Section 9.4の
Geminiキー漏洩インシデントが実際に起きたあとに重みを増す注記。

### `sandbox_template.json` — `SandboxConfig` のJSON実例（`sandbox_template.json:1-17`）

```json
{
  "authorized_imports": [
    "math", "math.*", "collections", "collections.*", "itertools", "re", "json",
    "typing", "typing.*", "functools", "operator", "heapq", "bisect", "copy",
    "string", "random", "datetime", "datetime.*", "array", "cmath"
  ],
  "allowed_directories": ["/testbed", "/tmp/agent"],
  "max_execution_time_seconds": 30,
  "max_memory_mb": 512,
  "max_output_chars": 20000
}
```

これは `sandbox/executor.py` の `DEFAULT_AUTHORIZED_IMPORTS`/`DEFAULT_ALLOWED_DIRECTORIES`
と同じ内容をJSONとして書き出したもので(詳細はSection 8.1)、`uv run sandbox sandbox_template.json`
(Section 8.4)に渡す設定例として、また「`SandboxConfig`のフィールドをJSONでどう表現するか」の
リファレンスとして機能する。ホワイトリストの中身自体も観察に値する——`os`, `sys`, `subprocess`,
`socket` のようなI/O系モジュールは1つも含まれておらず、`math`/`itertools`/`collections`
のような**純粋な計算用ライブラリ**だけに絞られている。`random` が入っているのは意外に見えるが、
MBPPのアルゴリズム問題には乱数を使う解法もあるため、危険度と必要性を天秤にかけた結果の許可。
`.*` サフィックス(`math.*`, `collections.*`, `typing.*`, `datetime.*`)はSection 8.1の
`check_imports()`が対応するglobパターンで、`from math import isclose`のような
サブモジュール/属性importまで許可する。逆に`math`はあるが`math.*`が無い
インポートホワイトリストを書いてしまうと`from math import isclose`だけが拒否される、
という運用上の落とし穴もこの一覧から読み取れる。

### `conftest.py` — pytestブートストラップ（`conftest.py:1-7`）

```python
"""pytestがどこから実行されても、プロジェクトルートをimport可能にするための設定ファイル。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
```

たった7行。`sample_JPN/`をカレントディレクトリにせず、リポジトリのどこから`pytest`を呼んでも
`from models import ...`のような絶対importが解決できるようにするためだけの存在。
`pyproject.toml`の`testpaths = ["tests"]`が「何を集めるか」を決め、この`conftest.py`が
「集めたテストがどこからimportを解決するか」を決める、という役割分担になっている。

### `.flake8`（`.flake8:1-3`）

```ini
[flake8]
max-line-length = 110
extend-exclude = .venv,models.py
```

`max-line-length = 110`はPEP8標準の79より緩め——サンドボックス周りのコメントや正規表現が
長くなりがちな実態に合わせた現実的な妥協。`models.py`の除外理由は`Makefile`の`mypy`設定と同じ
(moulinetteからのコピーは自分たちのスタイルルールの対象外)。

### `cache/` と `solutions/` — 生成物置き場

- **`cache/`**: `dump`してきたタスクJSON(`mbpp_task.json`など)や、実行結果の`solution.json`
  を一時的に置くためのディレクトリ。中身自体はコミット対象外で、`.gitkeep`だけがGitに
  含まれている(空ディレクトリでもディレクトリ構造自体はリポジトリに残したいという意図)。
- **`solutions/`**: こちらは逆に**実測結果を証跡として意図的にコミットしている**ディレクトリ。
  中身は大きく3種類に分かれる:
  1. `swebench_sympy-13480_task.json` / `swebench_sympy-14711_task.json` /
     `swebench_xarray-4629_task.json` / `mbpp-113_task.json` — 検証に使った
     タスク定義そのもの(入力側の証跡)。
  2. `<provider>_<model>_<task>.json` という命名の15本の`solution.json`
     (`google_gemini-flash-lite-latest_*.json` ×3、`groq_allam-2-7b_*.json` ×3、
     `groq_openai_gpt-oss-20b_*.json` ×3、`groq_qwen_qwen3.8-27b_*.json` ×3、
     `openrouter_minimax_minimax-m3_free_*.json` ×3)— `BENCHMARK_REPORT.md`
     (Section 17)の5モデル×3タスクの結果表と1対1で対応する出力側の証跡。
  3. `ablation_no_example__openrouter_minimax-m3_xarray-4629.json` —
     Section 17のアブレーション実験(`include_example=False`)用の追加1本。
  さらに`groq_allam-2-7b_mbpp-113.json`のように、5モデル×3タスクの主比較表には
  含まれないMBPP側の実行結果も1本混じっている——これは「同じ`Orchestrator`/CLI骨格が
  MBPPでも動く」ことを示す別系統の証跡で、`BENCHMARK_REPORT.md`本体の主眼である
  SWE-bench比較とは独立して置かれている。ファイル名だけでどのプロバイダ・モデル・
  タスクの組み合わせの結果かが分かるようになっている点は一貫している。

---

## 15. テストスイート（`tests/`）

まず前提知識をいくつか。**`pytest`**はPythonで最もよく使われるテストフレームワークで、関数名が`test_`で始まる関数を自動的に見つけて片っ端から実行し、その中の`assert`文（「これが真でなければ失敗」という表明）が1つでも失敗すればそのテストを不合格として報告してくれる——テストを実行する仕組み自体を自分で書く必要がない。**フィクスチャ(fixture)**は、複数のテストで共通して必要になる「準備」(例えば「一時ディレクトリに小さな偽リポジトリを作る」)や「後片付け」をひとまとめにして使い回すための仕組みで、`@pytest.fixture()`を付けた関数をテスト関数の引数名として指定するだけで、そのテストの実行前に自動的に呼び出される（後述の`fake_repo`フィクスチャがその実例）。**モック/フェイク(mock/fake)**とは、本物の依存先(LLM API、ネットワーク、本物のDockerコンテナなど)をテストの中でそのまま使うと「遅い」「お金がかかる」「結果が毎回変わる」という問題が起きるため、代わりに「決まった入力に対して決まった出力を返すだけの、挙動を固定した偽物」に差し替えてテストする、というテスト設計の基本手法——後述の`_ScriptedLLMClient`(あらかじめ用意した応答を順番に返すだけの偽LLMクライアント)がその実例。

`make test`(=`uv run pytest -v`)で、**ネットワークもAPIキーも一切不要**に完結するように
設計されている。各ファイルの狙い:

| ファイル | 検証対象 |
|---|---|
| `test_code_extraction.py` | Section 6 の全フォーマット(fence/XML/JSON/ReAct)が正しくPython呼び出しへ正規化され、未知の形式は明示的に失敗すること |
| `test_sandbox.py` | インポート制限・ファイルアクセス制限・タイムアウト・メモリ制限・**サンドボックスエスケープ対策**(dot記法、`getattr`、動的組み立て名、`__globals__`、`setattr`、通常の演算子オーバーロード/イテレーション/reprが壊れていないこと)を検証。メモリ上限のテストは(pytestプロセス自体のRLIMIT_ASを下げないよう)isolatedワーカー経由で行う |
| `test_llm_client.py` | キーローテーション・プロバイダフォールバック・リトライ・使用量集計。偽の`ChatProvider`でネットワークを完全に置き換える |
| `test_gemini_provider.py` | **APIキーが例外メッセージに絶対に漏れないこと**にフォーカスした専用テスト |
| `test_orchestrator.py` | 偽のLLMクライアント＋本物の`Sandbox`でThought→Code→Observationループをend-to-endで検証 |
| `test_mcp_client.py` | `mcp_tools_mbpp.py` を実サブプロセスとして起動した`MCPToolProxy`の結合テスト(ネットワーク不要) |
| `test_mcp_tools_mbpp.py` | `run_tests`ツール関数を直接呼び出し(`@mcp.tool()`デコレータは元の関数呼び出し可能性を保持するため、MCPトランスポート無しでテストできる) |
| `test_mcp_tools_swebench.py` | 必須9ツールを、`TESTBED_PATH`直下に作った小さな偽リポジトリ(`fake_repo`フィクスチャ)に対して検証 |
| `test_agent_startup.py` | 両エージェントCLIの起動時エラー処理とシャットダウン処理 |
| `test_edge_cases.py` | 各コンポーネント共通の境界値・不正入力ケース |

この表に出てくる「結合テスト」という言葉にも触れておく。1つの関数やクラス単体だけを検証する**単体テスト(unit test)**に対し、**結合テスト(integration test)**は複数の部品を実際に組み合わせて(ここでは「本物のサブプロセスとして起動したMCPサーバー」と「本物のクライアント」を実際に接続して)動かし、部品同士の繋ぎ込みが正しく機能するかを検証する。`test_mcp_client.py`はネットワークこそ使わないが、ローカルのサブプロセス起動というOS機能は本物を使っているという点で単体テストより一段上のレイヤーを検証している。`fake_repo`フィクスチャの実体を覗いてみると(`tests/test_mcp_tools_swebench.py:11-30`)、`tmp_path`(pytestが標準で提供する「このテストの間だけ存在する一時ディレクトリ」のフィクスチャ)の中に小さなPythonファイルを1つ書いて`git init`まで行い、本物の(ただし使い捨ての)gitリポジトリを毎テストごとにまっさらな状態で用意している——これにより、あるテストが前のテストの残骸に影響されて結果が変わる、という事故を防いでいる。

### `test_orchestrator.py` — ループ全体のend-to-endテスト

ここで言う**end-to-endテスト**とは、個々の部品(コード抽出、サンドボックス、LLMクライアントの各々)を単体で検証するのではなく、「タスクの入力を渡してから`SolutionOutput`が返ってくるまで」という実際の使われ方に最も近い流れを、部品を組み合わせたまま通しで検証するテストのこと。ここでは`_ScriptedLLMClient`という「呼ばれるたびにあらかじめ用意した応答テキストを順番に1つずつ返す」
フェイクLLMクライアントを使い、**本物の`Sandbox`**(`apply_process_memory_limit=False`で
プロセス全体へのメモリ制限だけ無効化)と組み合わせてSection 5の`Orchestrator.run()`を
実際に走らせる。LLM API呼び出しだけを偽物に差し替え、それ以外(サンドボックスでの実際のコード実行、コード抽出、ループの停止判定)は本物のまま動かしているのがポイント——「LLMの気まぐれな応答」という制御しづらい部分だけを固定値に置き換えることで、ループ制御ロジック自体の正しさを再現性ある形でテストできる。代表的なテスト:

- `test_final_answer_ends_the_loop_successfully`: 1ターン目は`print(1+1)`、2ターン目で
  `final_answer("def f():\n    return 1")`を呼ぶ2ステップの会話を用意し、
  `result.solution`が引数の文字列そのものになること、`result.steps[0].sandbox_output`が
  実際のサンドボックス実行結果`"2"`になること、`total_input_tokens`/`total_output_tokens`が
  各ステップの合計(20×2, 10×2)になることまで検証する。
- `test_shutdown_requested_during_llm_call_preserves_partial_steps`: `_ShutdownOnSecondCallLLMClient`
  というフェイクが2回目の`generate()`呼び出しで`ShutdownRequested`を送出するよう仕込み、
  Section 5で解説した「LLM呼び出し中のSIGTERM」を再現する。1回目のステップの
  `StepMetrics`は失われず`result.iterations == 1`として残ること、`result.error`に
  `"shutdown requested"`が含まれることを検証——`orchestrator.py:171-173`の
  `except ShutdownRequested`経路がまさにこの状況のために存在することの裏付け。
- `test_total_requests_counts_failed_attempts_when_all_providers_exhausted`:
  `_AlwaysExhaustedLLMClient`が`AllProvidersExhaustedError("...", attempted_requests=4)`を
  必ず送出するよう仕込み、`result.total_requests == 4`かつ`result.iterations == 0`
  (1件も`StepMetrics`が積まれない)ことを検証する。「成功パスを一度も通っていなくても
  試行回数だけは失われない」というSection 9.2の設計が、ここで直接テストされている。
- `test_token_budget_stops_the_loop_before_exceeding_it`: `max_input_tokens=15`という
  極端に低い上限を設定し、`llm_client.calls == 0`(LLMが一度も呼ばれない)ことまで
  検証する——`_conservative_input_token_bound()`が「送信前に」判定するというSection 5の
  説明が、実際に「送信されなかったこと」で裏付けられている。
- `test_output_request_is_clamped_to_remaining_hard_limit`: `max_output_tokens=7`という
  全体予算を設定したとき、実際に`LLMClient.generate()`へ渡される`max_output_tokens`引数が
  ちょうど`7`にクランプされることを、フェイククライアント側で受け取った引数を記録して検証する。

### `test_edge_cases.py` — コンポーネント横断の境界値テスト

特定の1ファイルに属さない、複数コンポーネントの「際どい入力」をまとめて検証する:

- `test_extraction_rejects_malformed_alternate_formats`: 壊れたJSON
  `<tool_call>{bad}</tool_call>`は`code is None`になる一方、ReAct形式の
  `Action Input: {"bad"}`(辞書として不正なJSON)は**フォールバックとして文字列扱いされ**、
  `value='{"bad"}'`という1引数呼び出しコードに変換される——Section 6で説明した
  「ReActの`Action Input`が辞書でなければ`{"value": raw_input}`扱い」という仕様の
  実例になっている。
- `test_extraction_preserves_json_null_boolean_and_numbers`: `{"a":null,"b":true,"c":1}`が
  `result = tool(a=None, b=True, c=1)\nprint(result)`という、Pythonの`None`/`True`/整数
  リテラルに正確に変換されることをアサートする——JSON値とPythonリテラルの対応表を
  1テストで具体的に固定している。
- `test_provider_key_collection_stops_at_first_missing_key`: `EDGE_KEY`と`EDGE_KEY_3`だけを
  設定し(`EDGE_KEY_2`を意図的に欠番にする)、`collect_api_keys()`が`["one"]`だけを返し
  `EDGE_KEY_3`の値`"three"`を拾わないことを検証する——Section 10で説明した
  「歯抜けの番号は末尾切り捨て」という仕様の直接的な証拠。
- `test_sandbox_default_getattr_is_honored`: `getattr(object(), 'missing', None)`が
  例外にならず`None`を返すことを検証する——`_make_restricted_getattr()`
  (Section 8.1)が「危険な属性名だけを拒否し、正当な3引数`getattr`の挙動
  (デフォルト値フォールバック)は一切壊さない」ことの回帰確認。
- `test_models_reject_missing_required_fields` / `test_models_round_trip_unicode_and_empty_optional_fields`:
  Section 4の`models.py`が本当にPydanticのバリデーションを機能させていること
  (必須フィールド欠如で例外、Unicode文字列やJSONラウンドトリップは無事に通る)を確認する、
  モデル層に対する数少ない直接テスト。

### 他のテストファイルの代表例

- `test_code_extraction.py`: `test_unclosed_python_fence_is_salvaged`(```python`フェンスが
  閉じられていなくても救済されること)、`test_json_tool_call_preserves_string_argument_types`
  (JSON側の型がPython値に正しく変換されること)、`test_no_code_block_found`
  (どの形式にも当たらない場合に`[NoCodeBlock]`が返ること)が、Section 6の7段階の
  フォールバックそれぞれに対応する。
- `test_llm_client.py`: `test_multiple_keys_rotate`(複数キーが実際に順番に使われること)、
  `test_generate_retries_then_succeeds`(1回失敗しても同一キーでリトライして成功すること)、
  `test_all_providers_exhausted_reports_attempted_requests`(Section 9.2で説明した
  `attempted_requests`の値が例外に正しく載ること)が中心。
- `test_mcp_client.py`: `test_tool_call_accepts_positional_arguments`と
  `test_tool_call_accepts_mixed_positional_and_keyword_arguments`が、Section 8.3の
  `_make_wrapper()`が位置引数・キーワード引数どちらでも(あるいは混在でも)正しく
  動作することを検証する。`test_connection_timeout_stops_background_loop`は、
  MCPサーバーが応答しない場合でもバックグラウンドイベントループが無期限にハングしない
  ことを確認する。
- `test_mcp_tools_mbpp.py`: `test_run_tests_uses_task_test_imports_env_var`が
  Section 12で説明した`AGENT_SMITH_TEST_IMPORTS`環境変数の実際の効果を検証し、
  `test_run_tests_rejects_unauthorized_host_import`が「候補コードがホスト側の
  非許可importを試みても`run_tests`用の使い捨てサンドボックスが拒否すること」を確認する。
- `test_mcp_tools_swebench.py`: `test_edit_file_rejects_ambiguous_match`が
  Section 12の「完全一致1箇所のみ」制約を、`test_path_traversal_outside_testbed_is_rejected`
  と`test_list_files_rejects_symlink_target_outside_testbed`が`_resolve_within_testbed()`の
  経路脱出防止を、`test_get_patch_is_never_truncated`が「`get_patch()`だけは
  `_cap_output()`を通さない」という設計を、それぞれ直接検証する。

### `test_sandbox.py` の内訳（テスト名がそのまま脅威モデルの一覧になっている）

Section 8.1で見たエスケープ対策1つ1つに、それをピンポイントで殺そうとするテストが対応している:

```
test_authorized_import_is_allowed
test_unauthorized_import_is_blocked
test_dynamic_import_bypass_is_blocked          # __import__("os") のような関数呼び出し経由
test_private_module_reference_escape_is_blocked # random._os のような非公開の入れ子モジュール
test_public_unauthorized_nested_module_is_blocked
test_operator_attrgetter_private_attribute_bypass_is_blocked
test_vars_builtin_and_star_import_are_blocked
test_subclasses_escape_via_dot_attribute_is_blocked      # ().__class__.__bases__[0].__subclasses__()
test_subclasses_escape_via_getattr_is_blocked            # getattr(obj, "__subclasses__")
test_subclasses_escape_via_dynamically_built_name_is_blocked  # getattr(obj, "__sub"+"classes__")
test_globals_attribute_access_is_blocked                 # obj.__init__.__globals__
test_setattr_on_dangerous_dunder_is_blocked
test_format_attribute_escape_is_blocked                  # "{0.__class__}".format(x)
test_formatter_field_escape_is_blocked
test_common_dunders_still_work_for_legitimate_code        # 過剰ブロックしていないことの回帰確認
test_reserved_namespace_names_cannot_override_sandbox_controls
test_isolated_worker_cannot_see_host_root_files
test_variables_persist_between_calls
test_final_answer_raises_and_carries_value
test_filesystem_restriction_blocks_outside_paths
test_timeout_interrupts_infinite_loop
test_memory_limit_is_enforced
```

`test_subclasses_escape_via_*` が3種類（dot記法／`getattr`／動的組み立て名）に分かれているのは、
Section 8.1で説明した「静的AST拒否だけでは `getattr` 経由のバイパスを防げず、動的パッチだけでは
文字列連結で名前を組み立てるケースを防げない」という多層防御が、実際にテストレベルでも
1対1に対応していることの証拠。`test_common_dunders_still_work_for_legitimate_code`
の存在も重要 — 防御を固めすぎて `__init__`/`__eq__`/`__iter__` のような正当な用途まで
壊していないか、という**過剰ブロックの回帰テスト**も同じファイルに同居している。

### `test_gemini_provider.py` — キー漏洩防止の専用テスト

Section 9.4 のキー漏洩インシデントを踏まえて作られた3テスト:

```
test_http_error_message_never_contains_the_api_key
test_connection_error_message_never_contains_the_api_key
test_successful_call_still_works
```

前者2つは、`requests` が投げる `HTTPError`/`ConnectionError` を偽の異常応答で意図的に
発生させ、そのメッセージ文字列に `?key=...` の値が**含まれていないこと**を assert する
(`assert fake_api_key not in str(exc)` のような形)。3つ目は「キーを隠す対策のせいで
正常系のレスポンス処理まで壊していないか」の回帰確認 — ここでも「防御を追加したら、
正常系を壊していないかのテストも必ずセットで書く」という同じパターンが繰り返されている。

---

## 16. セキュリティ設計まとめ

`README.md` の「Sandbox design」節を踏まえて要点だけ再掲する。各対策には
「もし無かったら何が起きるか」を併記する。ここに登場する用語(AST・Linux namespace・`unshare`/`bwrap`・`RLIMIT_AS`・`final_answer()`の例外伝播など)はいずれもSection 8で1つずつ平易な言葉から説明済みなので、この表はそれらの「まとめ・索引」として読んでほしい。

| 懸念 | 対策 | もし無かったら | トレードオフ |
|---|---|---|---|
| インポート制限 | ASTウォーク + 実行時`__import__`パッチの二重チェック。許可モジュールも制限プロキシで包み、非公開属性・非許可の入れ子モジュールを隠す | `import os`はASTチェックで防げても`__import__("os")`という関数呼び出しは防げず、任意のモジュールをロードできてしまう | — |
| OS隔離 | `unshare`+`bwrap`でネットワーク無効・専用PID/mount namespace・最小読み取り専用ルート・UID/GID 65534 | Pythonレベルの制限を1つでもすり抜ければ、ホストの実ファイルシステム・実ネットワークに触れてしまう(多層防御の最終防衛線) | Linux user namespaceと`bwrap`が必須。セットアップ失敗時はfail-closed(無許可の代替実行にフォールバックしない) |
| ファイルシステム制限 | `open`を`realpath`解決＋`allowed_directories`チェック付きに差し替え | `../../etc/passwd`のような相対パスや、シンボリックリンク経由で許可外ディレクトリを読み書きできてしまう | OS境界とこのラッパーの両方が必要(片方だけでは不十分という前提) |
| ネットワーク | bubblewrapの外側でnetwork namespaceを作成、NICなし | LLMが生成したコードが外部に任意のHTTPリクエストを送れてしまう(データ流出・SSRFの経路になる) | Linux namespaceサポートに依存 |
| 実行タイムアウト | 親がSIGTERM→SIGKILLでプロセスグループを終了、ワーカー自身もSIGALRM | 無限ループを書くコードが1つでもあれば、そのタスクのエージェント実行が永久にハングする | 親側MCP呼び出しのハングは別途RPCタイムアウトで対応 |
| メモリ制限 | `RLIMIT_AS`をワーカー自身に適用 | メモリを無制限に確保するコードがホストのOOM killerを誘発し、他プロセスまで巻き込みうる | ワーカーにのみ適用、エージェント本体やテストランナーには影響しない |
| 制限付きbuiltins | `eval`/`exec`/`compile`/`input`/`__import__`/`open`等を除去・差し替え | `eval(open("/etc/shadow").read())`のような明白な脱出経路がそのまま使えてしまう | — |
| 非公開属性アクセス | デフォルト拒否のアローリスト。`getattr`/`setattr`も同一ルールで動的強制。`str.format`と`string.Formatter`、`operator.attrgetter`/`methodcaller`は使用不可 | `().__class__.__bases__[0].__subclasses__()`から制限なしbuiltinsを持つクラスを辿られ、Pythonレベルの制限が全て無効化される(Section 8.1参照) | OS境界が依然として主たる防御線 |
| `final_answer()` | どのMCPサーバーが繋がっていても常に注入される独立クロージャ。予約名として衝突を拒否し、`FinalAnswer`は汎用例外処理で握りつぶされない | LLMが`final_answer`という名前の変数やツールを定義してしまうと、正規の提出シグナルが握りつぶされ、`success: true`が正しく検出されなくなる | Section 4.2の「例外伝播」要件に厳密準拠 |

---

## 17. `BENCHMARK_REPORT.md` からの知見

Section 4.7 要求(5モデル以上×2プロバイダ以上×3タスク以上)を満たす実測レポート。以下の表を読む前に、ベンチマーク特有の用語を3つ定義しておく。**レイテンシ(latency)**は「リクエストを送ってから応答が返ってくるまでにかかった時間」——ここでは1リクエストあたりの平均応答時間(ミリ秒)として使われている。**可用性(availability)**は「そのプロバイダ・モデルの組み合わせに、実際にどれだけ安定してリクエストを通せたか」——後述の表では「何回に1回リクエストが失敗せずに済んだか」というニュアンスで使われている。また表中の`429`/`400`は**HTTPステータスコード**で、`429`は「レート制限超過(Too Many Requests)」——一定時間あたりに送れるリクエスト数の上限を超えたときにサーバーが返すエラー、`400`は「不正なリクエスト(Bad Request)」——プロバイダ側がこちらの送ったリクエストの形式自体を受け付けられなかったことを意味する(ここでは後述の通り「モデルがこのプロジェクトのプロンプト形式に非互換だった」ことに起因する)。

### 実測結果(15ラン全件、`solutions/`の証跡と1対1対応)

| モデル | プロバイダ | タスク | 結果 | Iter | 入力tok | 出力tok | 所要時間 |
|---|---|---|---|---|---|---|---|
| `qwen3.8-27b` | Groq | sympy-14711 | Fail (429) | 2 | 3,882 | 73 | 2.9s |
| `qwen3.8-27b` | Groq | sympy-13480 | Fail (429) | 2 | 3,678 | 157 | 2.8s |
| `qwen3.8-27b` | Groq | xarray-4629 | Fail (429) | 1 | 2,334 | 47 | 2.3s |
| `allam-2-7b` | Groq | sympy-14711 | Fail (429) | 1 | 2,400 | 106 | 2.4s |
| `allam-2-7b` | Groq | sympy-13480 | Fail (429) | 1 | 2,209 | 1,079 | 3.0s |
| `allam-2-7b` | Groq | xarray-4629 | Fail (429) | 0 | 0 | 0 | 1.8s |
| `gpt-oss-20b` | Groq | sympy-14711 | Fail (400) | 0 | 0 | 0 | 3.4s |
| `gpt-oss-20b` | Groq | sympy-13480 | Fail (400) | 0 | 0 | 0 | 2.1s |
| `gpt-oss-20b` | Groq | xarray-4629 | Fail (400) | 0 | 0 | 0 | 2.3s |
| `minimax-m3` | OpenRouter | sympy-14711 | Fail (budget) | 30 | 235,630 | 3,051 | 282.2s |
| `minimax-m3` | OpenRouter | sympy-13480 | Fail (budget) | 30 | 222,626 | 1,988 | 267.2s |
| `minimax-m3` | OpenRouter | **xarray-4629** | **Pass** | 30 | 172,610 | 2,236 | 229.9s |
| `gemini-flash-lite` | Google AI Studio | sympy-14711 | Fail (429) | 18 | 121,984 | 1,728 | 33.9s |
| `gemini-flash-lite` | Google AI Studio | **sympy-13480** | **Pass** | 14 | 56,408 | 2,529 | 44.1s |
| `gemini-flash-lite` | Google AI Studio | **xarray-4629** | **Pass** | 10 | 52,818 | 448 | 51.3s |

この表を読むと、`minimax-m3`が合格した唯一の1件でも**30イテレーション全部を使い切って
ようやく成功**しているのに対し、`gemini-flash-lite`は10〜18イテレーションと大幅に少ない
反復で決着している——同じ「合格」でも探索効率がモデルによって大きく異なることが分かる。
`allam-2-7b`のxarray-4629行が`Iter=0, 入力tok=0`なのは、最初のLLM呼び出し自体が
429で失敗し`AllProvidersExhaustedError`に落ちたケース(Section 9.2/5参照)——
`total_requests`だけは失われないというあの設計のおかげで、少なくとも「何回叩こうとしたか」は
`solution.json`から追跡できる。

### プロバイダ信頼性

| モデル / プロバイダ | 平均レイテンシ | リトライ | リクエスト数 | 可用性 |
|---|---|---|---|---|
| `qwen3.8-27b` / Groq | 435ms | 0 | 11 | 非常に低い — 1〜2リクエストで429 |
| `allam-2-7b` / Groq | 854ms | 0 | 8 | 非常に低い — 0〜1リクエストで429 |
| `gpt-oss-20b` / Groq | n/a | 0 | 6 | 0% — 全リクエストが400(クォータではなく非互換) |
| `minimax-m3` / OpenRouter | 5,456ms | 3 | 93 | 高い — 30イテレーション予算をフル消化、クォータの壁なし |
| `gemini-flash-lite` / Google AI Studio | 1,467ms | 1 | 45 | まちまち — 3件中2件は予算内、1件は429 |

Groqの「分あたり8,000トークン」という上限は、このプロジェクトのシステムプロンプトの
サイズだけで1〜2リクエストで使い切ってしまう——モデル自体の能力不足ではなく、
純粋にクォータの問題であることが明記されている。

### 中間指標(Section 4)

- **探索効率**: 検証に合格した2モデルはどちらも、イテレーション予算の**最初の1/3以内**に
  バグのある行/ファイルを発見していた。
- **提出の規律**(テスト合格から`final_answer()`までのステップ数): `minimax-m3`/xarray-4629は
  6ステップの「様子見」を挟んでから提出、`gemini-flash-lite`/xarray-4629は2ステップ、
  `gemini-flash-lite`/sympy-13480に至っては**テスト実行の確認記録が1つも無いまま**提出
  (結果的に正解だったが、規律ある検証手順を踏んだとは言えない、という指摘つき)。

### アブレーション実験の詳細(Section 7も参照)

まず「アブレーション実験(ablation study)」という言葉について。これはもともと機械学習分野でよく使われる用語で、「システムからある要素を1つだけ取り除いて(切除=ablationして)、取り除く前後の結果を比較することで、その要素が実際にどれだけ結果に寄与していたかを確かめる」という実験手法を指す。ここでは`prompts.py`の`include_example`引数を`True`/`False`で切り替え、同じモデル・同じタスク
(`minimax-m3`, xarray-4629)で比較:

| | worked exampleあり | worked exampleなし |
|---|---|---|
| `success`フィールド | `true` | `true` |
| 実際の結果 | 合格(独立検証済み) | **失敗 — 空のパッチ** |
| イテレーション数／トークン数 | 30 / 172,610 | 18 / 57,134 |

数値だけ見ると「exampleを抜いた方が効率的」に見えてしまうが、実際には`print(...)`で
ツール呼び出し結果を出力する習慣が失われ、モデルは何も見えないまま盲目的に編集を続け、
最終的に空のパッチを`success: true`として提出していた。worked exampleがこの失敗を防ぐ
という、Section 4.7実験要件の直接的な結論。

### 結論

- **`gemini-flash-lite-latest`が第一候補**: 3件中2件が独立検証済みで合格、`minimax-m3`の
  1/3程度のコストで済む。唯一の失敗も能力不足ではなく429(レート制限)。
- **`minimax/minimax-m3:free`はバックアップとして妥当**: クォータの壁には一度も当たらないが、
  1リクエストあたり約5.5秒と遅く、慎重すぎる(予算をフル消化しがち)。
- **Groqの2モデルは今回の構成では見送るべき**: 8,000 tok/minの上限ではループを維持できない。
- **`gpt-oss-20b`は構成に関わらず見送るべき**: このプロンプト方式(非ネイティブfunction-calling)
  との根本的な非互換で、全リクエストが400エラー。
- **メタ的な結論**: 単純な`success`のpass/fail集計だけを見ると、実際には壊れていた
  アブレーション実行の方が「良い結果」に見えてしまう。`solution`が空でないこと、
  `final_answer()`の前にテスト合格の記録があること、そして可能な限り独立に再検証すること
  ——この3点セットが無ければ、ベンチマーク結果そのものが信用できない、というのが
  レポート全体の一番のメッセージ。

いずれのバグ(サンドボックスエスケープ、`docker cp`のUIDマッピング問題、MCPラッパーの
位置引数拒否、MBPPの`test_imports`無視、ツール出力の無制限サイズ、MCP呼び出しの無限待ち、
`total_requests`の数え漏れ)も `make test` のオフラインテストでは検出されず、実際の
Docker/実プロバイダ/独立レビューによって初めて見つかったものである点は変わらない
——ネットワーク不要のユニットテストと、実環境での統合的な検証は、互いに代替できない
別種の保証を提供している。

---

## 18. 実行方法チートシート

以下は実際にこのプロジェクトを動かすためのコマンド集。`uv`はこのプロジェクトが使っているPythonのパッケージ/仮想環境管理ツールで、`uv sync`が`pyproject.toml`に書かれた依存関係を仮想環境にインストールし、`uv run <コマンド>`がその仮想環境の中でコマンドを実行する、という2つだけ覚えておけば以下は読める。

```sh
cd sample_JPN
uv sync
cp .env.example .env   # 実際のAPIキーを埋める

# 対話サンドボックス
uv run sandbox
uv run sandbox sandbox_template.json
uv run sandbox --mcp-stdio "python mcp_tools_mbpp.py"
uv run sandbox --mcp-server http://localhost:8000/mcp

# MBPPエージェント
uv run python -m agent_mbpp --task-file cache/mbpp_task.json \
    --output cache/mbpp_solution.json \
    --model-name "qwen/qwen3.8-27b" --provider-url "https://api.groq.com/openai/v1"

# SWE-benchエージェント(要 Docker daemon)
uv run python -m agent_swebench --task-file cache/swebench_task.json \
    --output cache/swebench_solution.json \
    --model-name "minimax/minimax-m3:free" --provider-url "https://openrouter.ai/api/v1"

# デバッグ(pdbでagent_mbppを起動)
uv run python -m pdb -m agent_mbpp --help

# テスト / Lint
make test          # pytest(ネットワーク・APIキー不要)
make lint          # flake8 + mypy(通常運用)
make lint-strict   # flake8 + mypy --strict(より厳格)
make clean         # __pycache__ 等の掃除(.venvの中身には触れない)
make fclean        # clean に加えて .venv ごと削除
```

タスク定義自体は自分で書く以外に、moulinette側の`moulinette_eval dump mbpp/swebench`
コマンドで取得することを前提にしている(README.md)——`cache/mbpp_task.json`等は
このダンプの出力を想定した置き場所。

サンドボックスは Linux の `unshare` と `bubblewrap`(`bwrap`)を必要とする(Section 8.2で説明した通り、両者を組み合わせてネットワーク遮断・専用ファイルシステムなどのOSレベル隔離を実現している)。
どちらかが無い環境では、未許可の実行にフォールバックすることなく **fail-closed**
(明示的に失敗する)ように作られている。`agent_swebench`はさらにDocker daemonへの
アクセスも必要とする(`docker_runner.py`がイメージのpull・コンテナ起動を行うため)。
