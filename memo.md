# memo.md — Agent Smith 知識・手法メモ

## 1. Code Agent とは何か(背景知識)
古典的なLLMツール呼び出しは「JSON形式で1回だけ関数を呼ぶ」形式(OpenAI function calling等)。
Code Agent はこれをさらに一歩進め、**LLMにPythonコードそのものを書かせてサンドボックスで実行させる**方式。
利点: 変数を跨いだ状態保持、条件分岐・ループ、複数ツール呼び出しの合成が1ステップで書ける。
この設計思想はHugging Faceの `smolagents` の "Code Agents" のブログ記事が元ネタ
(ライブラリ自体の使用は課題で禁止されているので、**考え方だけ**参考にし、実装は自作する)。

## 2. サンドボックスの作り方(標準ライブラリのみ、RestrictedPython禁止)

### 2.1 コード実行そのもの
- `exec(compiled_code, restricted_globals, local_ns)` が基本形。
- `compile(code, "<agent>", "exec")` してASTチェックしてから実行すると安全性が上げやすい。
- `ast.parse()` して `ast.walk()` で `Import`/`ImportFrom` ノードを列挙し、許可リスト外なら実行前に拒否するのが定石(importフックだけに頼らない)。
- `__builtins__` を絞った辞書に差し替えて globals に渡すことで `open`, `eval`, `exec`, `__import__` などを制御できる。

### 2.2 プロセス分離 vs インプロセス
- **インプロセス実行**: 実装は楽だが、タイムアウトさせるには別スレッド+`threading.Timer`か、シグナル(`signal.alarm`、Unix限定)が必要。メモリ制限も難しい。
- **別プロセス実行**(`subprocess.Popen` でサンドボックス専用の子プロセスを起動し、コードを渡す): OSレベルで殺せる(`SIGTERM`→`SIGKILL`)。`resource.setrlimit`(Unix)でCPU時間・メモリ上限をかけやすい。通信はstdin/stdoutでJSON等をやり取り。
- 課題文でも「プロセス内か別プロセスか、それぞれタイムアウト/通信のトレードオフがある」と明言されている。Windowsで開発する場合は `resource` モジュールが使えないので要注意(WSL2かLinux VM上で最終検証するのが無難)。

### 2.3 タイムアウトの正しい実装
- 課題は「殺すのは事後チェックではなくforce-kill」と明言。`subprocess` + `Process.terminate()` → 猶予後 `Process.kill()` のパターンを使う。
- Windowsでは `SIGTERM` の挙動がUnixと異なる(実質即killになりがち)ので、評価環境がLinuxである前提で設計する。

### 2.4 final_answer の位置づけ
- サンドボックスが**常に**注入する組み込み関数。MCPサーバーとは無関係。
- 呼ばれたらエージェントループを終了させるシグナルとして使う(例外を投げてループ側でキャッチする実装が扱いやすい)。

## 3. MCP (Model Context Protocol) の基礎
- AnthropicがLLMとツール/データソースを繋ぐために策定したオープンプロトコル。
- Python実装は公式SDK `mcp`(`pip install mcp`)にFastMCPというサーバー構築用の高レベルAPIがある。
- トランスポート2種: **stdio**(子プロセスの標準入出力でJSON-RPCをやり取り)と **streamable HTTP**(HTTPエンドポイント)。両対応が必須。
- サーバー側で `@mcp.tool()` デコレータ等を使い関数を登録すると、クライアント側で `list_tools()` によりツール名・説明・パラメータ型(JSON Schema)が取得できる → これを使って「サンドボックスマニュアル」を動的生成する。
- サンドボックスは「MCPクライアントを内包しつつ、ツールをPython関数としてラップして実行名前空間に注入する」という二重構造になる。

## 4. LLM側の実装知識
- **stop_sequences**: モデルAPI呼び出し時に `<end_code>` 等を stop token として渡さないと、モデルがツールの実行結果を待たずに続きを「幻覚」で書いてしまう。OpenAI互換API・Anthropic API双方で `stop`/`stop_sequences` パラメータとして存在。
- **複数フォーマット対応が要る理由**: モデルによって学習時のツール呼び出し形式が異なる(Anthropic系はXML `<invoke>`、Hermes系はJSON `<tool_call>`、素朴なReActは `Action:`/`Action Input:`)。抽出層でこれらを正規化してからサンドボックスに渡す設計にすると、サンドボックス自体はフォーマット非依存のままにできる。
- **無料プロバイダの代表例**: OpenRouter, Together AI, Groq, Google AI Studio(Gemini), Mistral AI, Cohere, Fireworks AI, Perplexity AI。複数トークン管理・ローテーションが必須要件。

## 5. ベンチマーク知識
- **MBPP** (Mostly Basic Python Problems): 短いアルゴリズム問題+テストケース。`sanitized_tasks.json` にタスク定義が入っている(moulinette.zip内)。
- **SWE-bench Verified**: 実リポジトリ(sympy, xarray等)のGitHub issueを実際のPRベースで再現したベンチマーク。Dockerイメージ上でテストを走らせて合否判定。リーダーボード・エージェント設計の論文/ブログが多数あるので、モデル選定の参考にできる(課題文でも探すよう推奨されている)。
- 易しめの初手タスクとして課題側が例示: `sympy__sympy-14711`, `sympy__sympy-13480`, `pydata__xarray-4629`。

## 6. Pydantic (v2想定) の使い方メモ
- `class Foo(BaseModel): field: int = Field(..., description="...")` の形で必須フィールドを定義。
- `default_factory=lambda: [...]` でリストのデフォルト値を安全に生成(ミュータブルデフォルト回避)。
- JSON設定ファイル → `SandboxConfig.model_validate_json(text)` や `Foo(**json.load(f))` で読み込む。

## 7. うっかりミスしやすいポイント
- APIキーをコードに直書き → **即セキュリティ失格**。必ず `.env` / 環境変数経由にする。
- `KeyboardInterrupt`/`SystemExit` を広い `except Exception` で握りつぶさない(明示的に再送出する)。
- トークン上限は「全イテレーション合計」であり1ステップごとではない。reasoningモデルの thinking トークンも合算される点に注意。
- 評価にリトライはない(1発勝負)。エージェント内部のLLM呼び出しリトライ(`StepMetrics.retries`)とは別物。
