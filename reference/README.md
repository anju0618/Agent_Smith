# reference/ — papers behind the Agent Smith architecture

Background reading, not code to copy. Cited here because they map directly onto
TASKS.md's sections — the point is to understand *why* the design looks the way it
does before implementing each piece. `README.md`/`__main__.py` of the moulinette and
memo.md already cite the ideas informally; these are the primary sources.

| File | Paper | Relevant to |
|---|---|---|
| `react_2210.03629.pdf` | Yao et al., *ReAct: Synergizing Reasoning and Acting in Language Models* (ICLR 2023) | Section 1 — the Thought → Action → Observation loop `run_agent_loop` implements is this paper's core idea, predating the "Code" variant below. Read this first for *why* interleaving reasoning and acting beats reasoning-only or acting-only prompting. |
| `codeact_2402.01030.pdf` | Wang et al., *Executable Code Actions Elicit Better LLM Agents* (ICML 2024) | Section 1 & 2 — this is the "Code Agents" idea memo.md attributes to the smolagents blog post (whose *library* is banned, but whose *idea* — using a Python interpreter as the action space instead of fixed JSON tool calls — is exactly what this subject asks you to build from scratch). Explains why letting the LLM write real code (loops, variables, composing multiple tools in one turn) beats single-JSON-call tool use. |
| `swe_agent_2405.15793.pdf` | Yang et al., *SWE-agent: Agent-Computer Interfaces Enable Automated Software Engineering* (NeurIPS 2024) | Section 7 — the exact File System / Code Search / Execution tool split TASKS.md asks for (`read_file`, `search_code`, `run_tests`, ...) comes from this paper's "Agent-Computer Interface" idea: agents solve more SWE-bench issues when given purpose-built tools instead of a raw shell. Read before designing `mcp_tools_swebench.py`. |
| `swebench_2310.06770.pdf` | Jimenez et al., *SWE-bench: Can Language Models Resolve Real-World GitHub Issues?* (ICLR 2024) | Section 6 — defines the benchmark itself: task format, the `git diff`-based patch evaluation, and why real GitHub issues are harder than synthetic ones. Useful for understanding what `SWEBenchTaskInput`/eval_script are actually testing. |
| `mbpp_2108.07732.pdf` | Austin et al., *Program Synthesis with Large Language Models* (2021) | Section 5 — introduces the MBPP dataset `sanitized_tasks.json` comes from, and the few-shot prompting setup it was designed to be evaluated under. |
| `sandboxeval_2504.00018.pdf` | *SandboxEval: Towards Securing Test Environment for Untrusted Code* (2025) | Section 3 — practical failure modes of "just `exec()` it" sandboxes (filesystem/network escapes, resource exhaustion) and what a test suite for sandbox security looks like; a useful checklist against `exam_sandbox.sh`. |

## Suggested reading order

1. `react_2210.03629.pdf` — the loop shape.
2. `codeact_2402.01030.pdf` — why code-as-action instead of JSON tool calls.
3. `sandboxeval_2504.00018.pdf` — what "sandbox" has to actually defend against, before writing section 3.
4. `mbpp_2108.07732.pdf` and `swebench_2310.06770.pdf` — skim for task format, not full read.
5. `swe_agent_2405.15793.pdf` — right before section 7 (tool design for SWE-bench).
