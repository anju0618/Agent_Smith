"""エージェント/オーケストレータ: Thought -> Code -> Observationループ本体(セクション4.1)。

agent_mbpp.pyとagent_swebench.pyの間でそのまま共有される - 両ベンチマーク間で異なるのは
システムプロンプト、サンドボックス設定、接続するMCPサーバ、そしてfinal_answer()の引数が
どうSolutionOutput.solutionになるかだけである。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

from code_extraction import extract_code
from llm.client import AllProvidersExhaustedError, LLMClient
from models import SolutionOutput, StepMetrics
from sandbox.executor import FinalAnswer, Sandbox


class ShutdownRequested(BaseException):
    """request_stop()がSIGTERM受信のまさにその瞬間に送出する例外。

    意図的にExceptionではなくBaseExceptionを継承している: Sandbox.run()の汎用的な
    ``except Exception``(およびrequests/urllib3自身の内部エラー処理)がこれを
    誤って握りつぶしてはならないため。LLM呼び出し中/サンドボックス実行中に
    SIGTERMが届いた場合、その呼び出しが自然に終わるまで何もしないままでは、
    外部のハーネス(例: moulinetteのrun-agentコマンド)がSIGTERMとSIGKILLの間に
    与える猶予期間(約10秒)より長くかかってしまうことがあり、その結果SIGKILLが
    先に命中してagent_swebench.pyの`finally: container.cleanup()`がスキップ
    されてしまう。シグナルハンドラ自身の中で即座に例外を送出することで、
    ブロックされている呼び出しをすぐに中断させる(sandbox/executor.py自身の
    SIGALRMハンドラがSandboxTimeoutErrorを送出するのと同じ技法・同じ理由による)。
    """


@dataclass
class OrchestratorConfig:

    max_iterations: int
    max_input_tokens: int
    max_output_tokens: int
    max_time_seconds: float

    stop_sequences: List[str] = field(default_factory=lambda: ["<end_code>"])
    max_tokens_per_request: int = 1024


    budget_warning_threshold: float = 0.35


def _serialized_message_bytes(messages: List[dict]) -> int:


    serialized = json.dumps(messages, ensure_ascii=False, separators=(",", ":"))
    return len(serialized.encode("utf-8"))


_ESTIMATED_BYTES_PER_TOKEN = 3
"""英語主体のプロンプト/コードのUTF-8バイト長をトークン数に変換する際の目安。
実際のトークナイザは概ね1トークンあたり3~4バイト(英語散文やコード)なので、
3を使えばまだ余裕を残しつつ、1バイト=1トークン(4倍近い過大評価)より遥かに
現実的な見積もりになる。"""


def _conservative_input_token_bound(
    current_message_bytes: int,
    previous_message_bytes: Optional[int],
    previous_input_tokens: Optional[int],
) -> int:
    """次回のチャット入力に対する、プロバイダに依存しない安全側の上限トークン数を返す。

    実際のトークン数がまだ分からない最初のリクエストでは、UTF-8バイト長を
    `_ESTIMATED_BYTES_PER_TOKEN`で割った値に余裕(エンベロープ分)を足したものを
    見積もりとして使う(1バイト=1トークン扱いだと英語テキストで3~4倍も過大評価し、
    実際にはトークン予算に十分余裕があるタスクまで即座に失敗させてしまう)。
    それ以降のリクエストでは、プロバイダが返した直前の正確なトークン数を再利用し、
    新たに増えたバイト分だけ同じ比率でトークンを加算する。これにより、変化していない
    プロンプト部分に毎回見積もりを適用することなく、安全側の見積もりを維持できる。
    """
    if previous_message_bytes is None or previous_input_tokens is None:
        return current_message_bytes // _ESTIMATED_BYTES_PER_TOKEN + 32
    added_bytes = max(0, current_message_bytes - previous_message_bytes)
    return previous_input_tokens + added_bytes // _ESTIMATED_BYTES_PER_TOKEN + 16


class Orchestrator:
    """1つのタスクを完了(または上限到達)まで実行し、SolutionOutputを返すクラス。"""

    def __init__(
        self,
        llm_client: LLMClient,
        sandbox: Sandbox,
        system_prompt: str,
        config: OrchestratorConfig,
    ) -> None:

        self.llm_client = llm_client
        self.sandbox = sandbox
        self.system_prompt = system_prompt
        self.config = config
        self._stop_requested = False

    def request_stop(self) -> None:
        """SIGTERMハンドラから呼び出され、killされたエージェントでも部分的な計測値を
        返せるようにする(SWE-benchの場合はコンテナのクリーンアップにも到達できるようにする)、
        失われてしまわないようにするための関数。ShutdownRequestedを即座に送出するため
        (詳細はShutdownRequested参照)、LLM呼び出し中に届いたSIGTERMも自然終了を
        待たずにすぐに中断される。外側のハードタイムアウトは、セクション6.1に従い
        moulinetteのrun-agentコマンド側で別途強制される。"""
        self._stop_requested = True
        raise ShutdownRequested("shutdown requested (e.g. SIGTERM)")

    def run(self, task_id: str, benchmark: str, task_prompt: str) -> SolutionOutput:

        start = time.monotonic()
        messages: List[dict] = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": task_prompt},
        ]
        steps: List[StepMetrics] = []
        total_input_tokens = 0
        total_output_tokens = 0
        total_requests = 0
        error: Optional[str] = None
        solution_text = ""
        success = False
        previous_message_bytes: Optional[int] = None
        previous_input_tokens: Optional[int] = None

        for step_number in range(1, self.config.max_iterations + 1):

            if self._stop_requested:
                error = "stopped: shutdown requested (e.g. SIGTERM)"
                break
            elapsed = time.monotonic() - start
            if elapsed >= self.config.max_time_seconds:

                error = f"time budget exhausted ({elapsed:.1f}s >= {self.config.max_time_seconds}s)"
                break
            if total_input_tokens >= self.config.max_input_tokens:
                error = (
                    f"input token budget exhausted "
                    f"({total_input_tokens} >= {self.config.max_input_tokens})"
                )
                break
            if total_output_tokens >= self.config.max_output_tokens:
                error = (
                    f"output token budget exhausted "
                    f"({total_output_tokens} >= {self.config.max_output_tokens})"
                )
                break

            current_message_bytes = _serialized_message_bytes(messages)
            input_token_bound = _conservative_input_token_bound(
                current_message_bytes,
                previous_message_bytes,
                previous_input_tokens,
            )
            remaining_input_tokens = self.config.max_input_tokens - total_input_tokens
            if input_token_bound > remaining_input_tokens:
                error = (
                    "input token budget would be exceeded by the next request "
                    f"(conservative bound {input_token_bound} > "
                    f"remaining {remaining_input_tokens})"
                )
                break

            remaining_output_tokens = self.config.max_output_tokens - total_output_tokens
            request_output_limit = min(
                self.config.max_tokens_per_request,
                remaining_output_tokens,
            )

            try:
                gen = self.llm_client.generate(
                    messages,
                    stop=self.config.stop_sequences,
                    max_output_tokens=request_output_limit,
                )
            except AllProvidersExhaustedError as exc:
                total_requests += exc.attempted_requests
                error = f"LLM request failed: {exc}"
                break
            except ShutdownRequested as exc:
                error = str(exc)
                break

            total_requests += 1 + gen.retries
            total_input_tokens += gen.input_tokens
            total_output_tokens += gen.output_tokens
            previous_message_bytes = current_message_bytes
            previous_input_tokens = gen.input_tokens

            extraction = extract_code(gen.text)
            sandbox_input = extraction.code or ""
            final_answer_raised: Optional[FinalAnswer] = None

            if extraction.code is None:
                observation = extraction.note
            else:
                try:
                    sandbox_output = self.sandbox.run(extraction.code)
                    observation = (
                        f"{extraction.note}\n{sandbox_output}" if extraction.note else sandbox_output
                    )
                except FinalAnswer as fa:
                    final_answer_raised = fa
                    observation = f"[FinalAnswer submitted] {fa.answer!r}"

            if final_answer_raised is None:

                remaining_input_frac = (
                    self.config.max_input_tokens - total_input_tokens
                ) / self.config.max_input_tokens
                remaining_output_frac = (
                    self.config.max_output_tokens - total_output_tokens
                ) / self.config.max_output_tokens
                if min(remaining_input_frac, remaining_output_frac) <= self.config.budget_warning_threshold:

                    observation += (
                        "\n\n[BUDGET WARNING] Your token budget is nearly exhausted. "
                        "If you already have a verified solution, call final_answer(...) "
                        "in your very next turn - do not run further exploration or verification."
                    )

            steps.append(
                StepMetrics(
                    step=step_number,
                    input_tokens=gen.input_tokens,
                    output_tokens=gen.output_tokens,
                    request_time_ms=gen.request_time_ms,
                    api_url=gen.api_url,
                    model_name=gen.model_name,
                    llm_output=gen.text,
                    sandbox_input=sandbox_input,
                    sandbox_output=observation,
                    retries=gen.retries,
                )
            )

            if final_answer_raised is not None:
                success = True
                solution_text = str(final_answer_raised.answer)
                break

            messages.append({"role": "assistant", "content": gen.text})

            messages.append({"role": "user", "content": f"Observation:\n{observation}"})
        else:

            error = f"max iterations reached ({self.config.max_iterations})"

        if not success and error is None:
            error = "loop ended without a final_answer() call"

        return SolutionOutput(
            task_id=task_id,
            benchmark=benchmark,
            success=success,
            solution=solution_text,
            iterations=len(steps),
            total_requests=total_requests,
            total_input_tokens=total_input_tokens,
            total_output_tokens=total_output_tokens,
            total_time_seconds=time.monotonic() - start,
            steps=steps,
            system_prompt=self.system_prompt,
            error=None if success else error,
            timestamp=datetime.now().isoformat(),
        )
