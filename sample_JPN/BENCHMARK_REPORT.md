# Model Benchmark Report

Section 4.7 requires comparing at least 5 models across at least 2 providers
on the same ≥3 SWE-bench tasks, with the backing `solution.json` files kept in
the repository. This report is real: every number below comes from an actual
run against a real Docker image, a real free-tier API key, and (where marked)
an independent manual correctness check — see **"How this was run"** for
exactly how, and for the real bugs this exercise found and fixed along the way
(including one critical sandbox-escape vulnerability, found by an independent
code review of this same codebase and reproduced and fixed here).

---

## How this was run

- **Providers/keys**: real free-tier keys for Groq, OpenRouter, and Google AI
  Studio (Together AI was excluded - its API key had no genuine free-tier
  chat-completion models available, only pay-per-token ones, which Section
  4.6.1 rules out).
- **Tasks**: the same 3 SWE-bench instances the subject calls out as good
  first targets (Section 4.4): `sympy__sympy-14711`, `sympy__sympy-13480`,
  `pydata__xarray-4629`. Dumped once via `moulinette_eval dump swebench
  --task-id ...` and reused unchanged for every model.
- **Docker**: `docker_runner.py`'s approach (b) - sandbox on the host, MCP
  tool server bridged into the container via `docker exec` - was exercised
  against all three real `swebench/sweb.eval.x86_64.*` images end to end:
  image pull, container start, MCP dependency bootstrap, tool calls,
  `get_patch()`, container cleanup.
- **Correctness**: an agent's own `success: true` only means it called
  `final_answer(...)` - it is not proof the patch is correct (see the
  ablation study below for a case where it very much wasn't). Every task that
  finished `success: true` here was independently verified by applying its
  exact patch inside a fresh container from the same image and running the
  task's real `eval_script` directly (moulinette's own `validate` could not
  do this on this host - see below).
- **Two runs, by necessity**: the Groq and Google AI Studio combinations were
  run twice - once before and once after the `total_requests`-undercounting
  fix below - so that the committed `solution.json` files report accurate
  request counts. Free-tier LLM output is not deterministic and provider load
  varies minute to minute, so the two runs' pass/fail outcomes for Google AI
  Studio actually differ (see below) - this is itself real data about
  live-provider reproducibility, not an inconsistency in this report.

### Bugs found and fixed by this work

None of these were caught by the offline test suite (`make test`) before
being found live - either against a real Docker daemon, a real MBPP dataset
task, or an independent security review of this same code:

1. **Critical - the sandbox could be escaped.** An independent review of this
   codebase reproduced a classic in-process Python sandbox escape:
   `().__class__.__bases__[0].__subclasses__()` walks every already-loaded
   class to find one whose `__init__.__globals__['__builtins__']` is the
   *real*, unrestricted builtins module - completely bypassing the sandbox's
   restricted `__import__`/`open`, since those only guard *name lookups* in
   sandboxed code, not arbitrary introspection of objects the sandbox never
   handed out. Reproduced independently in this environment (confirmed
   `os.getcwd()` was reachable from inside `Sandbox.run()`), then fixed with
   a default-deny allowlist: `sandbox/executor.py`'s
   `check_dunder_attribute_access()` statically rejects any explicit
   `.__dunder__` attribute access outside a small safe list (`__init__`,
   `__repr__`, `__add__`, iteration/comparison protocol methods, etc.), and
   `getattr`/`setattr` are replaced with wrappers enforcing the same rule
   dynamically (closing `getattr(obj, "__sub" + "classes__")`-style bypasses
   of a purely static check). Six new tests in `tests/test_sandbox.py` cover
   the dot-attribute route, the `getattr` route, a dynamically-built name,
   `__globals__` specifically, `setattr` on a dangerous dunder, and (equally
   important) that ordinary operator overloading/iteration/repr still work.
   **Known residual gap, documented in code**: `str.format()`'s attribute
   mini-language (e.g. `"{0.__class__}".format(x)`) parses attribute names
   from a string at runtime, not as AST `Attribute` nodes, so it evades both
   the static check and the `getattr` wrapper. Disabling `str.format`
   entirely would break far more legitimate solution code than it protects
   against for MBPP/SWE-bench style tasks, so this is accepted the same way
   this file's other in-process-sandboxing trade-offs are.
2. **`docker_runner.py`'s `docker cp` failed on this host**: `Error response
   from daemon: failed to Lchown "/eval.sh" ... invalid argument`. This host
   uses per-user subuid/subgid ranges (`/etc/subuid`) for container UID
   remapping, and the host user's UID falls outside the range `docker cp`'s
   tar-based extraction tries to `lchown` the copied file to - the agent
   crashed at 0 iterations with no way to proceed, and (see below) nothing
   timed it out either. Fixed by writing both the eval script and the MCP
   tool server into the container via `docker exec -i <id> sh -c
   'cat > <path>'` (stdin redirection) instead of `docker cp` - this writes
   as whatever user the container's own entrypoint runs as, sidestepping the
   host's UID mapping entirely. The `docker exec` call itself is now also
   given an explicit 30s timeout (it shells out to the `docker` CLI directly,
   so it doesn't inherit the docker-py client's own default timeout the way
   `images.pull()`/`exec_run()` elsewhere in the same file do) - closing the
   real, separate "no timeout" gap the same review flagged: previously, a
   stuck container at this exact step would have hung with no bound at all
   until moulinette's outer 900s+10s kill. (Confirmed this `Lchown` failure
   is a host quirk, not specific to our code: moulinette's own `validate
   swebench` command hits the identical error internally when it tries to
   copy a patch into its own verification container - see below.)
3. **MCP tool wrappers rejected positional arguments**: calling
   `search_function_or_class_definition_in_code("_check_vector")` crashed
   with `wrapper() takes 0 positional arguments but 1 was given`. The
   dynamically-generated wrappers in `sandbox/mcp_client.py` only accepted
   `**kwargs` - but the subject's own worked example (Section 3.1) calls
   tools positionally (`result = search_code("validate_email")`), and our
   system prompt's "always use keyword arguments" instruction is guidance to
   the LLM, not a guarantee it follows it. Fixed by making
   `MCPToolProxy._make_wrapper` accept `*args` too, mapping them onto
   parameter names from the tool's declared JSON-schema property order -
   restoring normal Python calling convention.
4. **MBPP `test_imports` were silently ignored**: 13 of 427 tasks in
   moulinette's `sanitized_tasks.json` have a non-empty `test_imports` field
   (e.g. task 82's tests need `math` for `math.isclose(...)`), but neither
   the MBPP task prompt nor `run_tests()` ever surfaced it - a candidate
   solution that doesn't independently need that same import for its own
   logic would `NameError` on the test assertions with no way to know why.
   Fixed on both sides: `agent_mbpp.py` now passes `task.test_imports` to
   `mcp_tools_mbpp.py` via an `AGENT_SMITH_TEST_IMPORTS` env var (the MCP
   equivalent of how `TESTBED_PATH` is already passed for SWE-bench), and
   `run_tests()` prepends them itself - guaranteed correct regardless of
   whether the LLM happened to add the same import for unrelated reasons.
   The task prompt also mentions them now, for transparency.
5. **MCP tool output had no size cap**: `run_command()`, `run_tests()`, and
   `search_code()`/`read_file()`/`list_files()` returned arbitrarily large
   text directly into the agent loop - a verbose test suite or a big repo
   search could burn a large fraction of the 300,000-token cumulative
   SWE-bench budget (Section 6.1.2) in a single step. Fixed with a shared
   `_cap_output()` in `mcp_tools_swebench.py` (20,000 chars, matching
   `SandboxConfig.max_output_chars`'s existing scale), applied everywhere
   except `get_patch()` - which is deliberately left uncapped, since its
   return value can be the literal argument to `final_answer(get_patch())`,
   and truncating it would silently submit a corrupted, unappliable diff.
6. **An MCP call could hang indefinitely**: `sandbox/mcp_client.py`'s
   `_run()` waited on `future.result()` with no timeout. In normal use this
   is usually still bounded by the sandbox's own execution alarm, but a hang
   during `MCPToolProxy.close()` - which agent_swebench.py's `finally` block
   calls immediately *before* `container.cleanup()` - would have starved the
   container cleanup of a chance to ever run, with no signal coming to
   interrupt it. Fixed with an explicit timeout on every `_run()` call:
   300s for a normal tool call (generous enough for a real test suite or
   shell command, via `run_command()`'s own 120s timeout), 10s for
   `close()`'s teardown.
7. **`total_requests` undercounted failed attempts**: `LLMClient.generate()`
   only added to `total_requests` on its success path
   (`total_requests += 1 + gen.retries`); a call that exhausted every
   provider/key raised `AllProvidersExhaustedError` without ever reporting
   how many real HTTP requests it had made, so a task that failed outright
   after, say, 3 real 429 responses recorded `total_requests: 0` for that
   attempt - contradicting Section 5.1's "total number of LLM API requests
   made, including retries." Fixed by attaching `attempted_requests` to the
   exception and having `Orchestrator.run()` add it to `total_requests` on
   this path too.

### What moulinette's own `validate` could not do here

`moulinette_eval validate swebench` internally also uses a `docker cp`-style
copy to get the submitted patch into its verification container, and hit the
identical `Lchown ... invalid argument` error on this host - this is
moulinette's own code, not something this submission can patch. Rather than
report an unverified `success` field, every `success: true` run below was
verified manually: the exact patch from `solution.json` was applied with
`git apply` inside a fresh container from the same image (via `docker exec`,
same workaround as above) and the task's real `eval_script` was run directly.

### Groq's free-tier quota was already under pressure

Groq's own rate-limit headers (captured live, `qwen/qwen3.8-27b`) show
`x-ratelimit-limit-tokens: 8000` - an 8,000-**tokens-per-minute** cap for that
model. Our SWE-bench system prompt (full tool manual + framework rules +
worked example) is several thousand tokens on its own before any
conversation history accumulates, so it can take as few as 1-2 requests to
exhaust a whole minute's budget. Combined with the API-exploration calls made
earlier in this session (checking which models existed, spot-testing
providers), Groq's account-wide quota was already partly spent before this
matrix even started. This is itself the finding for Groq: **not** "the
models are incapable," but "this free tier's TPM ceiling is too tight to
sustain a multi-iteration agent loop back-to-back with other testing on the
same key," which is exactly the kind of provider-reliability data Section
4.7 asks for.

---

## 1. Setup

| | Choice | Why |
|---|---|---|
| Providers (3) | Groq, OpenRouter, Google AI Studio | All three have a genuine, documented $0 free tier; Together AI was excluded (see above) |
| Models (5) | `qwen/qwen3.8-27b` (Groq), `allam-2-7b` (Groq), `openai/gpt-oss-20b` (Groq), `minimax/minimax-m3:free` (OpenRouter), `gemini-flash-lite-latest` (Google AI Studio) | Mix of small/fast free models plus one (`gpt-oss-20b`) known from earlier MBPP testing to have a real compatibility problem with this prompting style, kept in deliberately as a negative control |
| Tasks (3, same set for every model) | `sympy__sympy-14711`, `sympy__sympy-13480`, `pydata__xarray-4629` | The subject's own suggested first targets (Section 4.4); small, self-contained, single-file fixes suited to a 30-iteration budget |

## 2. Results table

| Model | Provider | Task | Pass/Fail | Iterations | Input tokens | Output tokens | Wall time |
|---|---|---|---|---|---|---|---|
| `qwen/qwen3.8-27b` | Groq | sympy-14711 | Fail (429) | 2 | 3,882 | 73 | 2.9s |
| `qwen/qwen3.8-27b` | Groq | sympy-13480 | Fail (429) | 2 | 3,678 | 157 | 2.8s |
| `qwen/qwen3.8-27b` | Groq | xarray-4629 | Fail (429) | 1 | 2,334 | 47 | 2.3s |
| `allam-2-7b` | Groq | sympy-14711 | Fail (429) | 1 | 2,400 | 106 | 2.4s |
| `allam-2-7b` | Groq | sympy-13480 | Fail (429) | 1 | 2,209 | 1,079 | 3.0s |
| `allam-2-7b` | Groq | xarray-4629 | Fail (429) | 0 | 0 | 0 | 1.8s |
| `openai/gpt-oss-20b` | Groq | sympy-14711 | Fail (400) | 0 | 0 | 0 | 3.4s |
| `openai/gpt-oss-20b` | Groq | sympy-13480 | Fail (400) | 0 | 0 | 0 | 2.1s |
| `openai/gpt-oss-20b` | Groq | xarray-4629 | Fail (400) | 0 | 0 | 0 | 2.3s |
| `minimax/minimax-m3:free` | OpenRouter | sympy-14711 | Fail (30-iter budget) | 30 | 235,630 | 3,051 | 282.2s |
| `minimax/minimax-m3:free` | OpenRouter | sympy-13480 | Fail (30-iter budget) | 30 | 222,626 | 1,988 | 267.2s |
| `minimax/minimax-m3:free` | OpenRouter | **xarray-4629** | **Pass** ✅ (verified: 33/33 tests) | 30 | 172,610 | 2,236 | 229.9s |
| `gemini-flash-lite-latest` | Google AI Studio | sympy-14711 | Fail (429) | 18 | 121,984 | 1,728 | 33.9s |
| `gemini-flash-lite-latest` | Google AI Studio | **sympy-13480** | **Pass** ✅ (verified: 45/45 tests) | 14 | 56,408 | 2,529 | 44.1s |
| `gemini-flash-lite-latest` | Google AI Studio | **xarray-4629** | **Pass** ✅ (verified: 33/33 tests) | 10 | 52,818 | 448 | 51.3s |

**3 of 15 runs produced an independently-verified passing patch**, from 2 of
the 5 models. All 15 `solution.json` files are in `solutions/` (named
`<provider>_<model>_<task>.json`), each with full `steps[]` traces
(`llm_output`/`sandbox_input`/`sandbox_output`/`retries` per step) for
provenance checking, per Section 5.1.1 and 6.4.1.

## 3. Provider reliability

Computed from every successfully-completed request's `request_time_ms`
across all 15 runs:

| Model / Provider | Avg response time | Retries needed | Total requests (incl. failed attempts) | Availability during this run |
|---|---|---|---|---|
| `qwen/qwen3.8-27b` / Groq | 435 ms | 0 | 11 | Very low - 429'd after 1-2 successful requests on all 3 tasks |
| `allam-2-7b` / Groq | 854 ms | 0 | 8 | Very low - 429'd after 0-1 successful requests on all 3 tasks |
| `openai/gpt-oss-20b` / Groq | n/a (0 successful requests) | 0 | 6 | 0% - every request 400'd (see Conclusions), not a quota issue |
| `minimax/minimax-m3:free` / OpenRouter | 5,456 ms | 3 | 93 | High - ran all 3 tasks to the full 30-iteration budget without a hard quota wall |
| `gemini-flash-lite-latest` / Google AI Studio | 1,467 ms | 1 | 45 | Mixed - 2 of 3 tasks completed well under budget (10, 14 iterations); the third hit 429 at iteration 18 |

OpenRouter's free tier sustained a full 30-iteration agent loop three times
in a row without a hard quota wall, at the cost of being by far the slowest
per request (~5.5s avg) - it "succeeds" partly by not running out of budget
mid-task, not by being fast. Google AI Studio was the opposite profile: fast
enough and reliable enough to finish two tasks in well under half the
iteration budget, but still hit a hard 429 wall on the third.

## 4. Intermediary metrics

Read by hand from `steps[]`, across all 3 verified passes:

- **Exploration efficiency**:
  - `minimax-m3` / xarray-4629: found the exact buggy line
    (`return variable_attrs[0]`) via `search_code()` at **step 1**, read the
    file at **step 2**.
  - `gemini-flash-lite` / xarray-4629: found it via `search_code()` at
    **step 3** (after 2 wasted steps producing no code block at all - see
    below), read the file at **step 5**.
  - `gemini-flash-lite` / sympy-13480: correctly diagnosed the actual bug
    (`cotm` vs `cothm`) in its *Thought* text as early as **step 4**, despite
    that step's code block being malformed and not actually executing.
  - All three found the right file/line well inside the first third of their
    iteration budget - exploration was never the bottleneck for a model that
    made it this far at all.
- **Submission discipline** (iterations between a confirmed passing
  `run_tests()`/`run_command()` and `final_answer()`):
  - `minimax-m3` / xarray-4629: clean test run at **step 24**, then 3 more
    redundant re-verifications before `final_answer()` at **step 30** - a
    gap of **6 iterations**, more caution than necessary.
  - `gemini-flash-lite` / xarray-4629: `run_tests()` confirmed at **step 8**,
    `final_answer()` at **step 10** - a gap of **2 iterations** (one step
    printing `get_patch()` to inspect it first).
  - `gemini-flash-lite` / sympy-13480: **0 iterations, and that's a problem,
    not a strength.** Every visible tool call in this run's `steps[]` is
    either `[NoCodeBlock]` or `[MalformedCodeBlock]` (missing `<end_code>`)
    - there is no step showing a clean `run_tests()` result at all. The
    model's own reasoning text correctly identified the bug at step 4, and
    it called `final_answer(get_patch())` directly at step 14 with *no*
    prior confirmed-passing test run visible in the trace. It happened to be
    right - the patch is correct and 45/45 tests pass - but this is
    effectively an unverified submission that got lucky, not disciplined
    verification. A model that reasons correctly but can't reliably close a
    ```` ```python ```` fence is a real, distinct risk from one that can't
    reason at all.

A fourth finding, from the ablation study below, matters more than any
single number here: **a `success: true` field is not proof of a passing
patch**, and (new in this section) **neither, on its own, is a correct final
patch with no verified test run behind it** - both need the full trace, not
just the outcome fields.

## 5. Ablation study

**Change**: `prompts.py`'s `build_system_prompt(..., include_example=True)`
by default includes a worked Thought/Code/Observation example (Section 4.1
point 6). Ablated variant: same model, same task, same everything else, with
`include_example=False`.

**Task**: `pydata__xarray-4629`, model `minimax/minimax-m3:free` (OpenRouter)
- chosen because it was the first task+model pair with a real, verified
passing baseline to compare against.

| | Before (default: with example) | After (ablated: no example) |
|---|---|---|
| Reported `success` | `true` | `true` |
| **Actual correctness** | **Pass - verified 33/33 tests** | **Fail - `solution` field is an empty string** |
| Iterations | 30 | 18 |
| Input tokens | 172,610 | 57,134 |
| Output tokens | 2,236 | 1,648 |
| Wall time | 229.9s | 122.9s |

At a glance, the ablated run looks *better* - fewer iterations, a third of
the tokens, half the time, and it still reports `success: true`. Reading its
`steps[]` shows why that's misleading: from step 4 onward it almost never
wrapped tool calls in `print(...)` (e.g. `list_files("/testbed/xarray")`,
`edit_file(...)`, `run_tests()` as bare expressions), so nearly every
`sandbox_output` it received back was an **empty string**. It edited
`xarray/tests/test_dataset.py` (a test file, not the actual bug in
`merge.py`) while flying blind on whether that edit even applied, then called
`get_patch()` - which returned nothing, because no working-tree change had
actually landed - and submitted that empty string via `final_answer()`,
which the orchestrator dutifully recorded as `success: true`.

The default run (with the worked example) never has this problem: its
`steps[]` show it consistently wrapping every tool call in
`result = tool(...); print(result)`, matching the example's exact pattern.

**Conclusion of this ablation**: removing the worked example did not make
this model worse at *reasoning about the bug* - it still found and could
plausibly have fixed the same issue - but it made it dramatically less
reliable at the *mechanics* of reading tool output, which cascaded into a
submission that looks identical to a real pass by every metric except the
one that matters. This is direct evidence for keeping the worked example, and
a concrete illustration of why Section 6.4.1 requires evaluators to be able
to trace `system_prompt`/`llm_output`/`sandbox_input`/`sandbox_output`
instead of trusting `success` alone.

## 6. Conclusions

- **`gemini-flash-lite-latest` (Google AI Studio) is the strongest candidate
  for a final pipeline** among the models tested here: 2 of 3 tasks
  independently verified passing, using roughly a third of the tokens and a
  third of the iterations `minimax-m3` needed for its one pass. Its one
  failure was a hard 429 at iteration 18, not an incorrect patch - a pacing/
  quota problem, not a reasoning problem.
- **`minimax/minimax-m3:free` (OpenRouter) is a solid secondary choice**: the
  only model that never hit a hard provider quota wall across all 3 tasks,
  and produced one genuinely verified pass with very efficient exploration
  (found the bug in 1-2 steps) - but its own double-checking discipline (6
  idle iterations after the fix already worked) and much higher per-request
  latency (~5.5s) make it a slower, costlier fallback rather than the first
  choice.
- **A real, subtler risk surfaced only by reading full traces**:
  `gemini-flash-lite`'s sympy-13480 pass had *no* visible confirmed-passing
  test run before `final_answer()` - it reasoned its way to the right
  one-line fix despite consistently malformed code fences, and got lucky.
  A production pipeline should treat "correct patch, no verified `run_tests`
  step in the trace" as a flag worth surfacing even when the outcome happens
  to be right.
- **Both Groq models tested (`qwen/qwen3.8-27b`, `allam-2-7b`) should be
  disregarded as configured**, not necessarily because of raw capability but
  because this account's free-tier TPM ceiling (8,000 tokens/min measured
  live for `qwen/qwen3.8-27b`) cannot sustain more than 1-2 requests before
  a 429, and our system prompt alone is already a meaningful fraction of
  that budget. A shorter system prompt, a dedicated/rested API key, or a
  Groq model with a higher published TPM tier could change this conclusion.
- **`openai/gpt-oss-20b` (Groq) should be disregarded outright for this
  prompting style**: it 400'd identically on every single task
  (`"Tool choice is none, but model called a tool"`) across both runs of
  this matrix - a real, reproducible architectural incompatibility between
  this model's built-in native function-calling behavior and a CodeAct-style
  prompt that merely *describes* tools in text without registering an
  OpenAI-style `tools` schema. Not a transient quota issue: `total_requests`
  is nonzero (the requests were made and answered), every single one was a
  400, never a 429.
- **Meta-conclusion**: raw pass/fail and iteration/token counts alone would
  have ranked the ablated (no-example) run above the default one, and would
  have treated `gemini-flash-lite`'s lucky sympy-13480 submission identically
  to its genuinely-verified xarray-4629 one. Only reading `solution` and the
  full `steps[]` trace distinguished a real fix from an empty one in the
  first case, and a verified fix from a lucky guess in the second. Any
  future comparison on this project should always check `solution` is
  non-empty, look for a confirmed-passing test step before `final_answer()`,
  and, where possible, independently re-apply the patch and run the real
  eval script - exactly what this report did for all 3 of its passes.
