# Model Benchmark Report

5 models × 3 free-tier providers (Groq, OpenRouter, Google AI Studio) on the
same 3 SWE-bench tasks, run against real Docker images. `success: true` only
means the agent called `final_answer(...)`; every such run was independently
re-verified by re-applying the patch and running the real `eval_script`. All
15 backing `solution.json` files are in `solutions/`. This run also found and
fixed a classic in-process sandbox escape
(`().__class__.__bases__[0].__subclasses__()`), closed by the dunder-attribute
allowlist described in `README.md`.

## 1. Setup

Providers: Groq, OpenRouter, Google AI Studio (Together AI excluded — no
free chat-completion models). Models: `qwen/qwen3.8-27b`, `allam-2-7b`,
`openai/gpt-oss-20b` (Groq), `minimax/minimax-m3:free` (OpenRouter),
`gemini-flash-lite-latest` (Google AI Studio). Tasks (same 3 for every
model): `sympy__sympy-14711`, `sympy__sympy-13480`, `pydata__xarray-4629`.

## 2. Results

| Model | Provider | Task | Result | Iter | In tok | Out tok | Time |
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
| `minimax-m3` | OpenRouter | **xarray-4629** | **Pass** ✅ | 30 | 172,610 | 2,236 | 229.9s |
| `gemini-flash-lite` | Google AI Studio | sympy-14711 | Fail (429) | 18 | 121,984 | 1,728 | 33.9s |
| `gemini-flash-lite` | Google AI Studio | **sympy-13480** | **Pass** ✅ | 14 | 56,408 | 2,529 | 44.1s |
| `gemini-flash-lite` | Google AI Studio | **xarray-4629** | **Pass** ✅ | 10 | 52,818 | 448 | 51.3s |

**3 of 15 runs passed** (independently verified), from 2 of 5 models.

## 3. Provider reliability

| Model / Provider | Avg latency | Retries | Requests | Availability |
|---|---|---|---|---|
| `qwen3.8-27b` / Groq | 435 ms | 0 | 11 | Very low — 429'd after 1-2 requests |
| `allam-2-7b` / Groq | 854 ms | 0 | 8 | Very low — 429'd after 0-1 requests |
| `gpt-oss-20b` / Groq | n/a | 0 | 6 | 0% — every request 400'd, not a quota issue |
| `minimax-m3` / OpenRouter | 5,456 ms | 3 | 93 | High — full 30-iter budget, no quota wall |
| `gemini-flash-lite` / Google AI Studio | 1,467 ms | 1 | 45 | Mixed — 2/3 under budget, 1 hit 429 |

Groq's 8,000 tokens/min cap exhausts in 1-2 requests given our system prompt
size — a quota problem, not a capability one.

## 4. Intermediary metrics

**Exploration efficiency**: both verified-pass models found the buggy
line/file within the first third of their iteration budget.
**Submission discipline** (steps between a confirmed passing test and
`final_answer()`): `minimax-m3`/xarray-4629 — 6 idle steps; `gemini-flash-
lite`/xarray-4629 — 2 steps; `gemini-flash-lite`/sympy-13480 — **no**
confirmed test run at all before submitting (correct, but lucky).

## 5. Ablation study

`prompts.py`'s worked example (`include_example=True/False`), same model/task
(`minimax-m3`, xarray-4629):

| | With example | Without |
|---|---|---|
| `success` | `true` | `true` |
| **Actual result** | **Pass, verified** | **Fail — empty patch** |
| Iterations / tokens | 30 / 172,610 | 18 / 57,134 |

The ablated run looks better (fewer iterations/tokens) but stopped wrapping
tool calls in `print(...)`, so it edited blind and submitted an empty patch
as a false `success: true`. The worked example prevents this.

## 6. Conclusions

- **`gemini-flash-lite-latest`** is the strongest candidate: 2/3 verified
  passes at a third of `minimax-m3`'s cost; its one loss was a 429.
- **`minimax/minimax-m3:free`** is a solid backup: never quota-walled, but
  slower (~5.5s/req) and over-cautious.
- **Both Groq models tested should be disregarded as configured** — an
  8k tok/min cap can't sustain the loop.
- **`gpt-oss-20b` should be disregarded outright** — 400s on every call, a
  native-function-calling incompatibility with this prompting style.
- **Meta-conclusion**: raw pass/fail alone would rank the broken ablated run
  above the working one. Always check `solution` is non-empty, a test run
  precedes `final_answer()`, and independently re-verify.

---

## 日本語セクション（要約）

無料枠の3プロバイダ(Groq / OpenRouter / Google AI Studio)にまたがる5モデルを、
同じ3つのSWE-benchタスク(実際のDockerイメージ上)で比較しました。エージェントが
`success: true` と自己申告しても鵜呑みにせず、パッチを別コンテナに適用して実際
の評価スクリプトを走らせ、全て独立に検証しています。裏付けとなる15本の
`solution.json` はすべて `solutions/` に含まれています。

- **結果**: 15回中3回、独立検証済みの正解パッチが得られました(`minimax-m3` が
  1回、`gemini-flash-lite-latest` が2回)。Groqの2モデルはレート制限(429)で
  ほぼ即失敗、`gpt-oss-20b` は全リクエストが400エラーで、このプロンプト方式と
  そもそも相性が悪いことが分かりました。
- **プロバイダ信頼性**: OpenRouterは1リクエストあたり約5.5秒と遅いものの、
  クォータの壁に当たらず30イテレーションを最後まで走らせられました。Google AI
  Studioは速くて2/3のタスクは余裕を持って成功しましたが、残り1つは429で失敗。
- **アブレーション実験**: システムプロンプトに「お手本の実行例」を含めるかどう
  かを比較したところ、含めない場合はイテレーション数もトークン数も減って一見
  「良い結果」に見えましたが、実際にはツールの出力を`print`し忘れて何も見えな
  いまま編集を続け、空のパッチを`success: true`として提出していました。お手本
  の例がこの失敗を防いでいることが分かります。
- **結論**: 最終的に `gemini-flash-lite-latest` を第一候補、`minimax-m3` を
  バックアップとして採用しました。Groqの2モデルと `gpt-oss-20b` は、今回の
  構成では見送りとしました。`success` フィールドだけでなく、実際にテストが
  通った記録があるか・パッチが空でないかまで見て初めて、本当に正しい結果かどう
  か判断できるというのが、この検証全体を通じての一番の学びです。
