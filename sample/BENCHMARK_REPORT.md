# Model Benchmark Report

Section 4.7 requires comparing at least 5 models across at least 2 providers
on the same ≥3 SWE-bench tasks, with the backing `solution.json` files kept in
the repository. This document is honest about what has and hasn't actually
been executed in this environment — see **"What has actually been run"**
before reading the rest as a template rather than as a finished report.

## What has actually been run

This `sample/` implementation was built and verified in a sandboxed
development environment with **no Docker daemon usable for pulling real
SWE-bench images and no time budget for multi-hour Docker-based grading
runs**, but **with real, working free-tier API keys** (Groq, Together AI,
Google AI Studio) supplied for testing. Concretely:

- **Verified offline** (`make test`, 42 tests, no network/API keys needed):
  the sandbox's security constraints, the multi-format code extractor, the
  LLM client's retry/key-rotation logic, the full Thought→Code→Observation
  loop against a scripted fake LLM, and both MCP tool servers.
- **Verified live against real providers**: `agent_mbpp.py` was run
  end-to-end multiple times against a real MBPP task (task 113,
  `check_integer`, pulled directly from moulinette's `sanitized_tasks.json`)
  through Groq (`allam-2-7b`, `openai/gpt-oss-20b`) and Google AI Studio
  (`gemini-3.6-flash`). This confirmed the full real pipeline works: LLM
  call → `<end_code>`-terminated code extraction → sandbox execution → a real
  MCP `run_tests` tool call over stdio → the JSON result fed back as the next
  Observation → either `final_answer()` or a graceful `error` in
  `solution.json`. See `solutions/groq_allam-2-7b_mbpp-113.json` for one such
  real (unsuccessful, but honestly reported) run and
  `solutions/mbpp-113_task.json` for the task it was given.
- **Two real bugs were found and fixed by this live testing** (not caught by
  the offline test suite, since both only manifest when the actual MCP stdio
  subprocess and a real slow/imperfect LLM are involved):
  1. `agent_mbpp.py`'s sandbox `max_execution_time_seconds` was originally set
     to 10s — the same as `mcp_tools_mbpp.py`'s own internal `run_tests()`
     subprocess timeout. A legitimate (correct, fast) test run could still
     trip the *outer* sandbox alarm first once IPC/asyncio-bridge overhead was
     added, producing a spurious `[Timeout]` observation. Fixed by giving the
     outer sandbox a larger budget (20s) than the inner tool timeout (10s).
  2. `mcp_tools_mbpp.py`'s `run_tests()` used `multiprocessing.Process()` with
     the platform default **fork** start method to isolate candidate code.
     Because the MCP server itself runs an asyncio event loop (via `anyio`,
     for the stdio/HTTP transport) with background threads, forking from it
     intermittently deadlocked the child before it could run anything —
     manifesting as a bogus 10-second timeout on trivially fast, correct
     code. Confirmed by reproducing the exact hang through
     `MCPToolProxy.call_tool()` but not when calling `run_tests()` directly
     in-process, then fixed by switching to the **spawn** start method
     (`multiprocessing.get_context("spawn")`), which starts a clean
     interpreter instead of forking a multi-threaded one. Verified fixed:
     the same call went from timing out at 10s to completing in ~0.5s.
- **Provider quirks observed live** (see "Provider reliability" below):
  Groq's `openai/gpt-oss-20b` rejects plain prompting outright — it has
  built-in tool-call detection that 400s with `"Tool choice is none, but
  model called a tool"` the moment its output looks like a JSON tool call,
  even with no `tools` schema sent and `tool_choice: "none"` explicitly set.
  Both Groq and Google AI Studio's free tiers returned `429` after only a
  handful of requests during this session; Google AI Studio's error body was
  explicit about the ceiling: `generate_content_free_tier_requests` is capped
  at **20 requests/day per project per model** on the free tier, which this
  session exhausted for `gemini-3.6-flash` after a small number of live runs
  (a real, useful data point for planning the actual 5-model comparison: it
  needs to be spread across enough providers/models/days, or accounts, to
  avoid burning a whole day's quota on setup and debugging alone).
- **Implemented but not exercised against a real SWE-bench Docker image**:
  `agent_swebench.py` and `docker_runner.py`. The mandatory SWE-bench tools
  (`mcp_tools_swebench.py`) are unit-tested against a plain filesystem
  fixture, not against a container — pulling a real SWE-bench image (several
  GB) and running a full 900s×30-iteration task was outside this session's
  time budget.
- **Not run**: the full 5-model × 2-provider × 3-task SWE-bench comparison
  this report is supposed to contain, and no MBPP task was actually solved
  successfully within budget during this session (every live run above ended
  in a reported `error`, honestly — small free-tier models produced malformed
  code fences, called tools positionally, or ran out of budget before calling
  `final_answer()`; runs were also cut short by provider rate limits before
  more iterations could be attempted). Section 6.4.1 explicitly treats
  fabricated `solution.json` data as a **grade of 0**; the same standard
  applies here, so the tables below stay as an unfilled template rather than
  invented numbers.

**To turn this into a real report**: run the workflow in each section below
with a Docker daemon and enough free-tier quota, save every `solution.json`
under `solutions/<provider>_<model>_<task>.json`, and replace the placeholder
tables with the real numbers those files contain.

---

## 1. Setup

*(Template — fill in once real runs exist)*

| | Choice | Why |
|---|---|---|
| Providers | e.g. Groq, Together AI, (+ Google AI Studio, OpenRouter, Fireworks for ≥5 models) | Free tier, OpenAI-compatible or well-documented API, independent quotas so exhausting one doesn't block the whole run (Section 4.7's own tip) |
| Models (≥5) | e.g. `llama-3.1-8b-instant` (Groq), `llama-3.3-70b` (Together), `gemini-2.0-flash` (Google AI Studio), ... | Mix of small/fast and larger/more capable models to see whether raw capability or iteration discipline dominates |
| Tasks (≥3, same set for every model) | e.g. `sympy__sympy-14711`, `sympy__sympy-13480`, `pydata__xarray-4629` | These are the tasks the subject itself calls out as good first targets (Section 4.4) |

## 2. Results table

*(Template)*

| Model | Provider | Task | Pass/Fail | Iterations | Input tokens | Output tokens | Wall time |
|---|---|---|---|---|---|---|---|
| model/a | provider1 | sympy__sympy-14711 | | | | | |
| model/a | provider1 | sympy__sympy-13480 | | | | | |
| model/a | provider1 | pydata__xarray-4629 | | | | | |
| ... | | | | | | | |

## 3. Provider reliability

*(Template)*

| Model / Provider | Avg response time | Retries needed | Availability during the run |
|---|---|---|---|
| | | | |

## 4. Intermediary metrics

Pick at least 2 of the following (Section 4.7), measured by hand from
`solution.json`'s `steps[]` — no automation required, only the analysis matters:

- **Exploration efficiency**: the step at which the agent first reads/edits the
  file that appears in its final patch.
- **Partial progress**: the step at which the number of failing tests first
  decreases versus the baseline (unpatched) run.
- **Submission discipline**: iterations between "tests first pass" and the
  `final_answer()` call — 0 is ideal; a large gap suggests the agent kept
  fiddling after it already had a working fix, burning iteration/token budget.

*(Template — fill in with real step numbers once solution.json files exist)*

## 5. Ablation study

*(Template)* Pick one concrete change and compare before/after on the same
task + model. Good candidates given this implementation:

- **System prompt**: with vs. without the worked example in `prompts.py`
  (`_MBPP_EXAMPLE` / `_SWEBENCH_EXAMPLE`) — does the agent converge on the
  `Thought:`/```python fence format faster?
- **Stop sequence**: with vs. without `<end_code>` passed to the LLM API —
  does the model hallucinate tool output when it isn't forced to stop?
- **`max_tokens_per_request`**: a tighter vs. looser per-step token cap for
  the same model — does it change how many iterations it needs to converge?

| | Before | After |
|---|---|---|
| Pass/Fail | | |
| Iterations | | |
| Notes | | |

## 6. Conclusions

*(Template)* Once the table in Section 2 is real: which model(s) were
selected for the final pipeline and why (cite specific numbers — pass rate,
iteration count, token cost, reliability); which model(s) can be disregarded
and on what basis (e.g. consistently timed out, consistently hallucinated
tool output despite the stop sequence, too expensive in tokens per solved
task relative to a similarly-capable alternative).
