*This project has been created as part of the 42 curriculum by amakino, takawaka.*

# Agent Smith — Sample Reference Implementation

This `sample/` directory is a full reference implementation of the **Agent
Smith** subject (agentic coding framework for MBPP and SWE-bench), built from
`en.subject.pdf` at the repository root. It is a companion to — not a
replacement for — the team's own submission being developed at the repository
root (`sandbox/`, `models.py`, etc.): a working example to study, test, and
adapt rather than hand in as-is.

## Description

An autonomous **Code Agent** that solves programming tasks by repeating a
**Thought → Code → Observation** loop: the LLM reasons, writes Python code,
that code runs inside a security-restricted sandbox with dynamically
discovered MCP tools, and the result feeds back into the next iteration until
the agent calls `final_answer(...)`. Two benchmarks are supported end to end:

- **MBPP** — short algorithmic Python problems, verified with `run_tests`.
- **SWE-bench** — real repository bug fixes inside Docker containers,
  explored with a mandatory MCP toolset (file, search, execution tools) and
  submitted as a `git diff`.

## Instructions

```sh
cd sample
uv sync                      # installs pydantic, requests, mcp, docker, dotenv
cp .env.example .env         # fill in real, free-tier API keys
```

The sandbox requires Linux `unshare` and `bubblewrap` (`bwrap`) commands. If
they are unavailable, untrusted code execution fails closed instead of
falling back to an in-process runner.

### Run the interactive sandbox

```sh
uv run sandbox                                            # REPL, no tools
uv run sandbox sandbox_template.json                      # custom config
uv run sandbox --mcp-stdio "python mcp_tools_mbpp.py"      # with MBPP tools
```
Type code, press Enter on a blank line to run it, `exit` or Ctrl+D to quit.

### Run the MBPP agent against a real task

```sh
# 1. Get a task.json, e.g. via moulinette:
#    cd ../moulinette && uv run python -m moulinette.__main__ dump mbpp --output ../sample/cache/mbpp_task.json
# 2. Run the agent
uv run python -m agent_mbpp --task-file cache/mbpp_task.json \
    --output cache/mbpp_solution.json \
    --model-name "qwen/qwen3.8-27b" --provider-url "https://api.groq.com/openai/v1"
```
`solution.json` is written even on failure (crash, timeout, exhausted keys) —
see `error` and `steps` inside it for what happened. (Model IDs on free-tier
providers change often — see BENCHMARK_REPORT.md for models confirmed
working at the time this was last run.)

### Run the SWE-bench agent

```sh
# 1. Get a task.json:
#    cd ../moulinette && uv run python -m moulinette.__main__ dump swebench \
#        --task-id pydata__xarray-4629 --output ../sample/cache/swebench_task.json
# 2. Run the agent (needs a running Docker daemon)
uv run python -m agent_swebench --task-file cache/swebench_task.json \
    --output cache/swebench_solution.json \
    --model-name "minimax/minimax-m3:free" --provider-url "https://openrouter.ai/api/v1"
```

### Tests / lint

```sh
make test          # pytest — sandbox, code extraction, LLM client, orchestrator,
                    # both MCP tool servers, all with no network/API keys required
make lint           # flake8 + mypy
```

## System architecture

```
LLM API  <--Prompt/Response-->  Orchestrator (orchestrator.py)
                                      |
                               code_extraction.py   (Section 4.1: normalizes
                                      |               python fences / XML
                                      v               <invoke> / JSON
                              +---------------+       <tool_call> / ReAct
                              |    Sandbox    |        into one Python call)
                              | (sandbox/     |
                              |  executor.py) |
                              +-------+-------+
                                      | tool call (as a plain Python function)
                                      v
                              MCPToolProxy (sandbox/mcp_client.py)
                                      | stdio or streamable HTTP
                                      v
                       mcp_tools_mbpp.py / mcp_tools_swebench.py
                          (separate process(es), Section 4.5)
```

- **`orchestrator.py`** — the Agent/Orchestrator loop itself: calls the LLM,
  extracts code, runs it in the sandbox, feeds the observation back, repeats
  until `final_answer()` or a hard limit is hit. Identical for both benchmarks;
  only the system prompt, sandbox config, and connected MCP server differ.
- **`code_extraction.py`** — the code-extraction transform step (Section 4.1):
  the primary format is a fenced ```` ```python ... ``` ```` block ending in
  `<end_code>`; XML `<invoke>` tool calls, JSON/Hermes `<tool_call>` blocks,
  and ReAct `Action:`/`Action Input:` are all converted into an equivalent
  Python call before the sandbox ever sees them, so the sandbox stays
  completely format-agnostic.
- **`sandbox/executor.py`** — the execution boundary (see "Sandbox design").
- **`sandbox/mcp_client.py`** — the MCP client living *inside* the sandbox: it
  discovers tools from whatever MCP server is connected and exposes them as
  plain synchronous Python functions in the exec() namespace.
- **`llm/`** — the multi-provider LLM abstraction (see "LLM provider layer").
- **`mcp_tools_mbpp.py` / `mcp_tools_swebench.py`** — the two MCP servers,
  each a separate process, kept at the repository root per Section 4.2.
- **`agent_mbpp.py` / `agent_swebench.py`** — the two benchmark CLIs that wire
  everything above together for one task and write `solution.json`.

## Agent loop explanation

Each iteration of `Orchestrator.run()` (`orchestrator.py`):

1. Sends the full conversation so far to the LLM via `LLMClient.generate()`,
   with `<end_code>` as a stop sequence (Section 4.6's tip: without a stop
   sequence, a model that doesn't wait for the real tool output can hallucinate
   one instead).
2. Passes the raw response through `code_extraction.extract_code()`. If no
   code block is found, that is fed back to the LLM verbatim as the
   Observation (`[NoCodeBlock] ...`) — the loop never lets the LLM guess what
   happened, per Section 4.1's explicit-feedback requirement.
3. Runs the extracted code in `Sandbox.run()`. A `final_answer(...)` call
   raises `FinalAnswer`, which ends the loop immediately and becomes
   `SolutionOutput.solution`. Any other outcome (`stdout`, `[SyntaxError]`,
   `[SandboxViolation]`, `[Timeout]`, `[MemoryLimitExceeded]`,
   `[TruncatedOutput]`) becomes the next Observation.
4. Appends the assistant turn and the Observation to the conversation and
   loops, until `final_answer()`, `max_iterations`, the cumulative token
   budgets, or the wall-clock budget is reached — whichever comes first.
   `StepMetrics` records both `input_tokens`/`output_tokens` and the raw
   `llm_output`/`sandbox_input`/`sandbox_output` for every step, exactly as
   Section 4.1.1 requires for provenance checking.

## Sandbox design

`sandbox/executor.py` runs LLM-generated code in a persistent worker process
inside a network-disabled user/PID/mount namespace. `bubblewrap` exposes only
the Python runtime, the sandbox package, declared allowed directories, and a
temporary `/tmp`; the project root is not mounted wholesale. The worker keeps
the restricted `exec()` namespace and reports each result over a JSON pipe.

| Concern | This implementation | Trade-off |
|---|---|---|
| Import allowlist | AST walk (`ast.Import`/`ImportFrom`) **and** a runtime-patched `__import__`, so `__import__("os")` called as a plain function is caught too. Imported modules are exposed through restricted proxies which hide private attributes and unauthorized nested modules (`random._os`, `typing.sys`, etc.) | — |
| OS isolation | Worker is launched through `unshare` + `bubblewrap` with no network, a private PID/mount namespace, a minimal read-only root, and UID/GID 65534 | Requires Linux user namespaces and the `bwrap` executable; setup failure is fail-closed |
| Filesystem allowlist | `open` is replaced with a wrapper that `os.path.realpath`-resolves the target and checks it's under `SandboxConfig.allowed_directories`; only those existing directories are mounted into the worker | The OS boundary and the wrapper are both required |
| Network access | Network namespace is created outside bubblewrap and has no network interfaces | Depends on Linux namespace support |
| Execution timeout | Parent enforces a wall-clock deadline and terminates the worker process group with `SIGTERM`→`SIGKILL`; the worker also uses `signal.alarm()` for a clean Python observation | A stuck parent-side MCP call is separately bounded by the RPC wait |
| Memory limit | Worker applies `resource.setrlimit(RLIMIT_AS, ...)` to its own process | Applies to the worker, not the agent or test runner |
| Restricted builtins | `eval`, `exec`, `compile`, `input`, `breakpoint`, `help`, `exit`, `quit`, `vars`, `__import__`, `open` are removed/replaced in the sandboxed builtins | — |
| Private attribute access | A default-deny allowlist: `check_dunder_attribute_access()` rejects private attributes outside a small safe dunder list; `getattr`/`setattr` enforce the same rule dynamically, `str.format()` and `string.Formatter` are unavailable, and `operator.attrgetter`/`methodcaller` are unavailable | The OS boundary remains the primary security boundary |
| `final_answer()` | A closure injected into every sandbox namespace, independent of whatever MCP server is connected; reserved-name collisions from MCP namespaces are rejected, and `FinalAnswer` is deliberately **not** caught by the generic exception handler | Matches Section 4.2's "exception propagation" requirement exactly |

MCP tool wrappers are represented in the worker by bridge functions. Calls are
sent to the trusted parent process, which invokes the existing dynamic
`MCPToolProxy` wrapper and returns a JSON-safe result. The worker stays alive
between calls, so variables persist between agent steps without exposing the
parent process to generated code.

`SandboxConfig` (`models.py`) is a Pydantic model loadable from a JSON file
(`sandbox_template.json` is a working example); `sandbox/cli.py` is the
interactive REPL (`uv run sandbox`) described in Section 4.2, wired to connect
to either transport.

## Tool implementation details

**MCP integration** (`sandbox/mcp_client.py`): the sandbox never hardcodes
tool names. `MCPToolProxy` connects (stdio or streamable HTTP — both
required), calls `list_tools()`, and builds one synchronous wrapper function
per discovered tool. `manual_text()` renders those same tool schemas into the
system prompt (the "sandbox manual" from Section 4.2), so connecting a
different MCP server automatically changes both what the sandbox can call
*and* what the LLM is told it can call — this is how the system stays
compatible with the "unknown MCP server" the subject says it will be tested
against. Connection establishment, each tool call, and shutdown are all
bounded; one owner coroutine enters and exits the AnyIO transport contexts in
the same task so timeout cleanup cannot leak the server subprocess.

**MBPP tools** (`mcp_tools_mbpp.py`): `run_tests(code, test_list)` runs the
candidate + assertions in a throwaway OS-isolated sandbox with a 10s timeout,
so candidate code cannot access the MCP server's host filesystem or network.
It returns `{"success": bool, "output": str}` as JSON, per Section 4.3.2.

**SWE-bench tools** (`mcp_tools_swebench.py`): all nine mandatory tools
(`read_file`, `edit_file`, `list_files`, `search_code`,
`search_function_or_class_definition_in_code`, `find_references`,
`run_tests`, `get_patch`, `run_command`) are plain filesystem/subprocess
operations rooted at the `TESTBED_PATH` environment variable — this file has
**no Docker-specific logic at all**, so it behaves identically whether
`TESTBED_PATH` points at a bare host checkout (e.g. for independent tool
testing) or at a path inside a container it happens to be running in. Every
path is resolved and checked against `TESTBED_PATH` before use, refusing to
read/write/list outside it. `edit_file` runs `python3 -m py_compile` on `.py`
files after editing and reports `[EditSyntaxError]` explicitly if the edit
broke the file — the "edit introduced a syntax error" feedback mandated by
Section 4.1. `get_patch` shells out to `git -c core.fileMode=false diff`
exactly as Section 4.4 specifies.

**Docker bridging** (`docker_runner.py`, used only by `agent_swebench.py`):
implements approach (b) from Section 4.4 — the sandbox (the Python
interpreter executing the LLM's code) stays on the host; only the *MCP tool
server process* is started inside the task's container, via `docker exec -i`,
so its filesystem/git/test operations run against the real task environment
while the sandbox itself never touches the container directly.

## LLM provider layer

`llm/provider.py` defines a small `ChatProvider` protocol
(`chat(messages, model, api_key, stop, max_output_tokens, timeout) ->
GenerationResult`) plus `GenerationResult`/`UsageStats`, closing the gap
`requirements.md` calls out explicitly: `generate()` must return tokens,
timing, `api_url`, and `model_name`, not just text, so `StepMetrics` can be
populated faithfully.

Two structurally different implementations back it:
`llm/providers/openai_compatible.py` (OpenRouter, Groq, Together AI,
Fireworks AI — anything speaking the standard `/chat/completions` wire
format) and `llm/providers/gemini.py` (Google AI Studio's REST API, which
uses a different endpoint shape, an API-key query parameter, and a
`contents`/`candidates` schema) — proving the abstraction isn't just an
OpenAI-shaped interface with a different base URL.

`LLMClient` (`llm/client.py`) rotates across every `<PROVIDER>_API_KEY`,
`<PROVIDER>_API_KEY_2`, ... found in the environment (Section 4.6.1's
mandatory multi-token management) and falls back across providers if one is
fully exhausted, tracking `UsageStats` (requests, retries, tokens, latency)
throughout. `config.py` resolves a `--provider-url` against a small registry
of known providers, or synthesizes a generic `<HOST>_API_KEY` env var name for
an unlisted one — new providers need no code changes, just a matching env var.

## Benchmark results and analysis

See `BENCHMARK_REPORT.md` (next to this README) for the full comparison
(Section 4.7): 5 models across 3 providers on the same 3 SWE-bench tasks, run
against real Docker images, with 3 independently-verified passing patches
(from 2 different models/providers), a critical sandbox-escape vulnerability
found by independent review and fixed (see "Sandbox design" above), several
other real bugs this exercise found and fixed (a host-specific `docker cp`
UID issue, MCP tool wrappers rejecting positional arguments, MBPP
`test_imports` being silently ignored, unbounded MCP tool output, an
MCP call with no timeout, and an undercounted `total_requests`), and an
ablation study showing why a `success: true` field alone isn't proof of a
real fix.

## Resources

#### Classic references
- Model Context Protocol specification & Python SDK docs — https://modelcontextprotocol.io/
- MBPP dataset — Austin et al., *Program Synthesis with Large Language
  Models* (see `reference/mbpp_2108.07732.pdf` at the repository root)
- SWE-bench — Jimenez et al., *SWE-bench: Can Language Models Resolve
  Real-World GitHub Issues?* (see `reference/swebench_2310.06770.pdf`)
- ReAct prompting format — Yao et al., *ReAct: Synergizing Reasoning and
  Acting in Language Models* (see `reference/react_2210.03629.pdf`)
- CodeAct — Wang et al., *Executable Code Actions Elicit Better LLM Agents*
  (see `reference/codeact_2402.01030.pdf`) — the paper behind the
  code-based-tool-calling idea this whole project is built on
- Python `signal`, `resource`, and `ast` standard library documentation —
  used directly for the sandbox's timeout, memory limit, and import checking

#### How AI was used in this project
This entire `sample/` directory was produced by Claude (Anthropic), acting on
a request to build a complete reference implementation of the Agent Smith
subject, given `en.subject.pdf` and the team's existing in-progress work
(`sandbox/executor.py`, `models.py`) as context. Concretely, AI was used for:
architecture design (the Orchestrator/code-extraction/Sandbox/MCP split), all
of the code in this directory, the test suite, and this documentation. It was
**not** used to fabricate benchmark data — see `BENCHMARK_REPORT.md` for what
was and wasn't actually executed. Per the subject's own AI-use guidance
(Chapter II), this sample is meant to be read, tested, and understood — not
submitted verbatim as the team's own work.
