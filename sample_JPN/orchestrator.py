"""Agent/Orchestrator: the Thought -> Code -> Observation loop (Section 4.1).

Shared verbatim between agent_mbpp.py and agent_swebench.py - only the system
prompt, sandbox configuration, connected MCP server, and how final_answer's
argument becomes SolutionOutput.solution differ between the two benchmarks.
"""
# ============================================================================
# 【日本語解説】このファイルの立ち位置
# ============================================================================
# 234行、クラス1個＋ヘルパー関数2個＋例外1個というコンパクトな構成。
# インポートを見ると役割分担がそのまま見える:
#   - code_extraction.extract_code   … LLM出力からコードを抜き出す
#   - llm.client.LLMClient           … LLMを呼ぶ
#   - models.SolutionOutput/StepMetrics … 結果を型にまとめる（契約）
#   - sandbox.executor.Sandbox/FinalAnswer … コードを実行する
# Orchestrator自身はこの4役だけを指揮する「指揮者」で、それぞれの実処理は
# 一切持たない。MBPP/SWE-benchどちらのエージェントも、このファイルを
# 一字一句同じまま使う——違いはシステムプロンプト・サンドボックス設定・
# 接続するMCPサーバーだけ。
# ============================================================================
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
    """Raised by request_stop() at the exact point a SIGTERM was delivered.

    Deliberately a BaseException, not an Exception: Sandbox.run()'s generic
    ``except Exception`` (and requests/urllib3's own internal error handling)
    must never swallow it, or a SIGTERM arriving mid-LLM-call/mid-sandbox-exec
    would silently do nothing until that call finishes on its own - which can
    take longer than the ~10s grace period external harnesses (e.g.
    moulinette's run-agent) give between SIGTERM and SIGKILL, causing SIGKILL
    to hit first and skip agent_swebench.py's `finally: container.cleanup()`.
    Raising immediately, in the signal handler itself, interrupts a blocked
    call right away (the same technique - and the same reason - as
    sandbox/executor.py's own SIGALRM handler raising SandboxTimeoutError).
    """
    # -------------------------------------------------------------------
    # 【日本語解説】なぜ Exception ではなく BaseException を継承するのか
    # -------------------------------------------------------------------
    # SIGTERMは「プロセスに終了してくれと頼む信号」。moulinette（採点
    # システム）はSIGTERMを送ったあと、約10秒だけ待ってそれでも終わら
    # なければSIGKILL（問答無用の即死、後始末する隙を与えない）を送る。
    #
    # もしこの例外が普通の Exception だったら、LLM API呼び出し中や
    # サンドボックス実行中に多用されている汎用の except Exception:（例:
    # Sandbox.run() 内部や requests/urllib3 のエラーハンドリング）に
    # 握りつぶされてしまい、SIGTERMが届いても「その処理が自然に終わるまで」
    # 何も起きない。それが10秒を超えたらSIGKILLが先に来て、
    # agent_swebench.py の finally: container.cleanup()（Dockerコンテナの
    # 後片付け）が実行されないままプロセスごと消されてしまう。
    #
    # BaseException を直接継承しておけば、except Exception では捕まらず
    # スルーされるため、request_stop() が呼ばれた瞬間にどこで実行中でも
    # 即座にこの例外が上（agent_*.py側のtry/except/finally）まで伝播する。
    # -------------------------------------------------------------------


@dataclass
class OrchestratorConfig:
    # -------------------------------------------------------------------
    # 【日本語解説】OrchestratorConfig = 予算の入れ物
    # -------------------------------------------------------------------
    # イテレーション数・入力トークン・出力トークン・時間という4種類の予算と、
    # stop_sequences（後述）をまとめた設定。agent_mbpp.py/agent_swebench.py
    # がこの値をベンチマークごとに変えて渡す
    #   MBPP     : max_iterations=10,  max_input_tokens=6,000,   ...
    #   SWE-bench: max_iterations=30,  max_input_tokens=300,000, ...
    # -------------------------------------------------------------------
    max_iterations: int
    max_input_tokens: int
    max_output_tokens: int
    max_time_seconds: float
    stop_sequences: List[str] = field(default_factory=lambda: ["<end_code>"])
    # 【重要】ここがprompts.pyで説明した <end_code> の実体。LLM API呼び出し
    # 時にこの文字列を stop sequence として渡すことで、LLMが <end_code> を
    # 出力した瞬間に生成が強制的に打ち切られる。これが無いと、LLMが
    # まだ実行してもいないツールの結果（Observation）を幻覚で先読みして
    # 自分で書き続けてしまう危険がある。
    max_tokens_per_request: int = 1024
    # 1回のLLM APIリクエストで許可する最大出力トークン数（予算全体とは別に、
    # 1回あたりの上限も設けている）。


def _serialized_message_bytes(messages: List[dict]) -> int:
    # ---------------------------------------------------------------
    # 【日本語解説】メッセージ全体をJSONにシリアライズしたUTF-8バイト数
    # ---------------------------------------------------------------
    # トークン数そのものではなく「バイト数」を測っているのは、実際に
    # APIを叩く前にプロバイダ非依存でおおよそのサイズを見積もるため
    # （後述の _conservative_input_token_bound で使われる）。
    # ---------------------------------------------------------------
    serialized = json.dumps(messages, ensure_ascii=False, separators=(",", ":"))
    return len(serialized.encode("utf-8"))


def _conservative_input_token_bound(
    current_message_bytes: int,
    previous_message_bytes: Optional[int],
    previous_input_tokens: Optional[int],
) -> int:
    """Return a provider-independent upper bound for the next chat input.

    Supported providers tokenize from bytes or Unicode text, so the UTF-8 byte
    length plus a small envelope allowance safely bounds the first request.
    Later requests reuse the provider's previous exact token count and add at
    most one token per newly-added byte. This remains conservative without
    repeatedly applying the byte-level worst case to the unchanged prompt.
    """
    # =====================================================================
    # 【日本語解説】トークン予算を「送信前に」見積もる仕組み
    # =====================================================================
    # 「実際にAPIを叩いてから初めてトークン超過が分かる」のではなく、送る前に
    # 上限を見積もりたい。でもトークン数はプロバイダ依存（バイト数から計算
    # するとは限らない）なので厳密には分からない。そこで:
    #
    #   ・初回（previous_* が None）:
    #       メッセージのUTF-8バイト数 + 32 を保守的な上限とみなす。
    #       「バイト数 ≧ トークン数」という安全側の前提に基づく単純な見積もり。
    #
    #   ・2回目以降:
    #       前回の"実測"トークン数（previous_input_tokens、実際にLLM APIの
    #       レスポンスから得られた正確な値）を土台にして、
    #       「今回増えた分のバイト数 + 16」だけを足す。
    #       プロンプト全体に毎回バイト単位のワーストケースを適用するのでは
    #       なく、変化していない部分は前回の実測値をそのまま信用し、増分
    #       だけに保守的な見積もりを適用することで、無駄に厳しくなりすぎ
    #       ないようにしている。
    #
    # +32 や +16 という定数は「メッセージのJSON構造（role, content などの
    # キー名やクォート）が増える分」を吸収するための小さな余裕（envelope
    # allowance）。
    # =====================================================================
    if previous_message_bytes is None or previous_input_tokens is None:
        return current_message_bytes + 32
    added_bytes = max(0, current_message_bytes - previous_message_bytes)
    return previous_input_tokens + added_bytes + 16


class Orchestrator:
    """Runs one task to completion (or to a limit) and returns a SolutionOutput."""

    def __init__(
        self,
        llm_client: LLMClient,
        sandbox: Sandbox,
        system_prompt: str,
        config: OrchestratorConfig,
    ) -> None:
        # 依存を全部コンストラクタで受け取るだけ。Orchestrator自身は
        # LLMクライアントもサンドボックスも「生成」しない——それは
        # agent_mbpp.py/agent_swebench.py側の責務。
        self.llm_client = llm_client
        self.sandbox = sandbox
        self.system_prompt = system_prompt
        self.config = config
        self._stop_requested = False
        # SIGTERM受信フラグ。request_stop()がTrueにする。

    def request_stop(self) -> None:
        """Called from a SIGTERM handler so a killed agent still returns partial
        metrics (and, for SWE-bench, still reaches its container cleanup)
        instead of losing them. Raises immediately (see ShutdownRequested) so a
        SIGTERM landing mid-LLM-call interrupts it right away rather than
        waiting for it to finish on its own; the outer hard timeout is still
        enforced by moulinette's run-agent command per Section 6.1."""
        # ---------------------------------------------------------------
        # 【日本語解説】SIGTERMハンドラから呼ばれる唯一の入口
        # ---------------------------------------------------------------
        # agent_mbpp.py/agent_swebench.py の signal.signal(SIGTERM, ...) が
        # 登録したハンドラから呼ばれる。フラグを立てるだけでなく、その場で
        # 即座に ShutdownRequested を raise することで、たとえ run() ループの
        # 「毎ターン先頭」のチェック（下記 self._stop_requested）に到達する
        # 前でも、LLM呼び出し中やサンドボックス実行中の処理をすぐ中断できる。
        # ---------------------------------------------------------------
        self._stop_requested = True
        raise ShutdownRequested("shutdown requested (e.g. SIGTERM)")

    def run(self, task_id: str, benchmark: str, task_prompt: str) -> SolutionOutput:
        # =====================================================================
        # 【日本語解説】run() = Thought → Code → Observation ループの本体
        # =====================================================================
        # 1タスクを最後まで走らせ、成功しても失敗しても必ず SolutionOutput を
        # 返す。以下、実行順に見ていく。
        # =====================================================================
        start = time.monotonic()
        # 壁時計の開始時刻。time.monotonic() はシステム時刻の変更（NTP補正
        # など）の影響を受けないので、経過時間の計測に適している。
        messages: List[dict] = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": task_prompt},
        ]
        # OpenAI形式のchatメッセージ配列。この後、ターンが進むたびに
        # assistant（LLMの応答）とuser（Observation）が交互に追記されて
        # いく——つまりこの messages リスト自体が会話履歴そのもの。
        steps: List[StepMetrics] = []
        total_input_tokens = 0
        total_output_tokens = 0
        total_requests = 0
        error: Optional[str] = None
        solution_text = ""
        success = False
        previous_message_bytes: Optional[int] = None
        previous_input_tokens: Optional[int] = None
        # 直前ターンのバイト数・トークン数。_conservative_input_token_bound
        # に渡すための状態。

        for step_number in range(1, self.config.max_iterations + 1):
            # -----------------------------------------------------------
            # 【日本語解説】6-1. 停止条件チェック（毎ターン最初の関門）
            # -----------------------------------------------------------
            # 4種類の「もう使い切った」チェック＋1種類の「次のリクエストで
            # 超過しそうか事前チェック」の、計5段階の関所を毎ターン通す。
            # どの条件で止まっても error に理由の文字列を入れて break する。
            # -----------------------------------------------------------
            if self._stop_requested:
                # SIGTERM済み。ループ先頭でのポーリングによる二重の安全網
                # （request_stop() 自体はもっと早く例外で割り込むが、万一
                # フラグだけ立って例外が別の場所で吸収されていた場合の保険）。
                error = "stopped: shutdown requested (e.g. SIGTERM)"
                break
            elapsed = time.monotonic() - start
            if elapsed >= self.config.max_time_seconds:
                # 時間予算切れ。
                error = f"time budget exhausted ({elapsed:.1f}s >= {self.config.max_time_seconds}s)"
                break
            if total_input_tokens >= self.config.max_input_tokens:
                # 入力トークン予算切れ（これまでの累計）。
                error = (
                    f"input token budget exhausted "
                    f"({total_input_tokens} >= {self.config.max_input_tokens})"
                )
                break
            if total_output_tokens >= self.config.max_output_tokens:
                # 出力トークン予算切れ（これまでの累計）。
                error = (
                    f"output token budget exhausted "
                    f"({total_output_tokens} >= {self.config.max_output_tokens})"
                )
                break

            # 「次に送るメッセージが、まだ送ってもいないのに予算を超過し
            # そうか」を事前に見積もる（_conservative_input_token_bound、
            # 上のヘルパー関数の説明を参照）。
            current_message_bytes = _serialized_message_bytes(messages)
            input_token_bound = _conservative_input_token_bound(
                current_message_bytes,
                previous_message_bytes,
                previous_input_tokens,
            )
            remaining_input_tokens = self.config.max_input_tokens - total_input_tokens
            if input_token_bound > remaining_input_tokens:
                # 送ったら確実に超過すると"保守的に"予測されるなら、実際には
                # 送らずに未然にループを止める。
                error = (
                    "input token budget would be exceeded by the next request "
                    f"(conservative bound {input_token_bound} > "
                    f"remaining {remaining_input_tokens})"
                )
                break

            # このリクエストで許可する出力トークン数 = 「1リクエストあたりの
            # 上限」と「残り出力トークン予算」の小さい方。
            remaining_output_tokens = self.config.max_output_tokens - total_output_tokens
            request_output_limit = min(
                self.config.max_tokens_per_request,
                remaining_output_tokens,
            )

            # -----------------------------------------------------------
            # 【日本語解説】6-2. LLM呼び出し
            # -----------------------------------------------------------
            try:
                gen = self.llm_client.generate(
                    messages,
                    stop=self.config.stop_sequences,  # ["<end_code>"]
                    max_output_tokens=request_output_limit,
                )
            except AllProvidersExhaustedError as exc:
                # 全プロバイダ・全APIキーが尽きた（llm/client.py参照）。
                # 成功パスを一度も通っていないのでStepMetricsは作れないが、
                # 実際に送られたHTTPリクエスト数だけは失わずに加算する。
                total_requests += exc.attempted_requests
                error = f"LLM request failed: {exc}"
                break
            except ShutdownRequested as exc:
                # LLM APIコール中にSIGTERMが届いたケース。
                error = str(exc)
                break

            # ここまで来たら、このターンのLLM呼び出しは成功している。
            total_requests += 1 + gen.retries
            total_input_tokens += gen.input_tokens
            total_output_tokens += gen.output_tokens
            previous_message_bytes = current_message_bytes
            previous_input_tokens = gen.input_tokens
            # 次のターンの _conservative_input_token_bound 計算のために、
            # 「実測された」今回の入力トークン数を保存しておく。

            # -----------------------------------------------------------
            # 【日本語解説】6-3. コード抽出とサンドボックス実行
            # -----------------------------------------------------------
            extraction = extract_code(gen.text)
            # gen.text = LLMのコード抽出前の生テキスト全文（後で
            # StepMetrics.llm_output にそのまま入る）。
            sandbox_input = extraction.code or ""
            # extraction.code = 抜き出されたコード部分だけ（後で
            # StepMetrics.sandbox_input に入る）。llm_output とは別物。
            final_answer_raised: Optional[FinalAnswer] = None

            if extraction.code is None:
                # コードが1つも見つからなかった（[NoCodeBlock]など）。
                # サンドボックス実行自体をスキップし、抽出層のnoteを
                # そのまま次のObservationとしてLLMに見せる。
                observation = extraction.note
            else:
                try:
                    sandbox_output = self.sandbox.run(extraction.code)
                    # note（フォーマット変換や救済が起きたことの説明）が
                    # あれば、実行結果の前に付け加えて見せる。
                    observation = (
                        f"{extraction.note}\n{sandbox_output}" if extraction.note else sandbox_output
                    )
                except FinalAnswer as fa:
                    # sandbox.py の設計ポリシー: FinalAnswer は特別扱いの
                    # 例外で、Sandbox.run() 内部の汎用 except Exception には
                    # 一切握りつぶされず、ここまで確実に伝播してくる。
                    final_answer_raised = fa
                    observation = f"[FinalAnswer submitted] {fa.answer!r}"

            # -----------------------------------------------------------
            # 【日本語解説】6-4. StepMetricsの記録
            # -----------------------------------------------------------
            # ここまでで集めた全情報（LLM生テキスト、抽出後のコード、
            # 実行結果、トークン数、レイテンシ、リトライ回数）を1個の
            # StepMetrics にまとめて steps に積む。
            # -----------------------------------------------------------
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
                # final_answer() が呼ばれた = このタスクは完了。
                # ループを即座に抜ける（以降のイテレーションは行わない）。
                success = True
                solution_text = str(final_answer_raised.answer)
                break

            # -----------------------------------------------------------
            # 【日本語解説】6-5. 会話履歴に今回のやり取りを追記して次のターンへ
            # -----------------------------------------------------------
            # LLMの発言（Thought+Code、gen.text そのもの）を assistant
            # メッセージとして、実行結果を user メッセージとして追記する。
            # 次のループ反復では、この積み上がった messages がそのまま
            # LLMへの入力になる（＝会話履歴として毎回全量を送っている）。
            # -----------------------------------------------------------
            messages.append({"role": "assistant", "content": gen.text})
            messages.append({"role": "user", "content": f"Observation:\n{observation}"})
        else:
            # -----------------------------------------------------------
            # 【日本語解説】for...else — 「breakされずにループが自然終了」を検出
            # -----------------------------------------------------------
            # Pythonのfor文のelse節は、ループがbreakで抜けなかった場合にのみ
            # 実行される。つまりここに来るのは「max_iterations回すべて
            # 使い切ったのに final_answer() も呼ばれず、他のどの break 条件
            # にも当たらなかった」場合だけ。
            # -----------------------------------------------------------
            error = f"max iterations reached ({self.config.max_iterations})"

        if not success and error is None:
            # 理論上ここには来ないはずだが（success=Trueにならずbreakした
            # 経路は必ずerrorをセットしている）、念のための防御的フォール
            # バック。
            error = "loop ended without a final_answer() call"

        # ---------------------------------------------------------------
        # 【日本語解説】最終的に必ず SolutionOutput を返す
        # ---------------------------------------------------------------
        # 成功でも失敗でも（予算切れ・SIGTERM・全プロバイダ全滅・
        # イテレーション上限到達、どの経路でも）、ここまでに積み上がった
        # steps を含んだ SolutionOutput を必ず返す。「途中経過を失わない」
        # というこのプロジェクト全体の設計方針が、最後のこの return 文に
        # 集約されている。
        # ---------------------------------------------------------------
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
