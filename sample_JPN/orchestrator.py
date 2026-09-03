"""エージェント/オーケストレータ: Thought -> Code -> Observationループ本体(セクション4.1)。

agent_mbpp.pyとagent_swebench.pyの間でそのまま共有される - 両ベンチマーク間で異なるのは
システムプロンプト、サンドボックス設定、接続するMCPサーバ、そしてfinal_answer()の引数が
どうSolutionOutput.solutionになるかだけである。
"""
from __future__ import annotations  # 型注釈を文字列として遅延評価する(将来のアノテーション構文をサポート)

import json  # メッセージ列をシリアライズしてバイト長を測るために使用
import time  # 経過時間の計測に使用
from dataclasses import dataclass, field  # 設定用データクラスの定義に使用
from datetime import datetime  # 結果に付与するタイムスタンプ生成用
from typing import List, Optional  # 型注釈のため

from code_extraction import extract_code  # LLM出力からコードブロックを抽出するユーティリティ
from llm.client import AllProvidersExhaustedError, LLMClient  # LLMクライアントと、全プロバイダが失敗した際の例外
from models import SolutionOutput, StepMetrics  # 最終結果・各ステップの計測値のデータモデル
from sandbox.executor import FinalAnswer, Sandbox  # サンドボックス実行環境と、final_answer()が投げる例外


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
    # オーケストレータの動作を制御する設定値をまとめたデータクラス
    max_iterations: int  # 最大反復(ステップ)回数
    max_input_tokens: int  # 累積入力トークン数の上限
    max_output_tokens: int  # 累積出力トークン数の上限
    max_time_seconds: float  # 実行時間の上限(秒)
    stop_sequences: List[str] = field(default_factory=lambda: ["<end_code>"])  # LLM生成を止める停止シーケンス(デフォルトは"<end_code>")
    max_tokens_per_request: int = 1024  # 1回のLLMリクエストあたりの最大出力トークン数


def _serialized_message_bytes(messages: List[dict]) -> int:
    # メッセージ列をJSONにシリアライズし、そのUTF-8バイト長を返す(トークン数見積もりの基礎データ)
    serialized = json.dumps(messages, ensure_ascii=False, separators=(",", ":"))  # 余分な空白なしでJSON文字列化(ASCIIエスケープはしない)
    return len(serialized.encode("utf-8"))  # UTF-8エンコード後のバイト数を返す


def _conservative_input_token_bound(
    current_message_bytes: int,
    previous_message_bytes: Optional[int],
    previous_input_tokens: Optional[int],
) -> int:
    """次回のチャット入力に対する、プロバイダに依存しない安全側の上限トークン数を返す。

    対応しているプロバイダはバイト列またはUnicodeテキストからトークン化を行うため、
    UTF-8バイト長に小さな余裕(エンベロープ分)を足したものが最初のリクエストを
    安全に見積もる上限となる。それ以降のリクエストでは、プロバイダが返した直前の
    正確なトークン数を再利用し、新たに増えたバイト1つにつき最大1トークンを加算する。
    これにより、変化していないプロンプト部分に毎回バイト単位の最悪見積もりを
    適用することなく、安全側の見積もりを維持できる。
    """
    if previous_message_bytes is None or previous_input_tokens is None:
        return current_message_bytes + 32  # 初回リクエストの場合はバイト数に固定の余裕32を足した値を返す
    added_bytes = max(0, current_message_bytes - previous_message_bytes)  # 前回からメッセージが何バイト増えたかを計算(負にはしない)
    return previous_input_tokens + added_bytes + 16  # 前回の実測トークン数に、増加バイト数と余裕16を足して返す


class Orchestrator:
    """1つのタスクを完了(または上限到達)まで実行し、SolutionOutputを返すクラス。"""

    def __init__(
        self,
        llm_client: LLMClient,
        sandbox: Sandbox,
        system_prompt: str,
        config: OrchestratorConfig,
    ) -> None:
        # 依存オブジェクトと設定を保持し、停止フラグを初期化するコンストラクタ
        self.llm_client = llm_client  # LLMへの問い合わせに使うクライアント
        self.sandbox = sandbox  # コード実行に使うサンドボックス
        self.system_prompt = system_prompt  # 会話冒頭で使うシステムプロンプト
        self.config = config  # 反復回数・トークン数などの制限設定
        self._stop_requested = False  # SIGTERMなどによる停止要求フラグ(初期値はFalse)

    def request_stop(self) -> None:
        """SIGTERMハンドラから呼び出され、killされたエージェントでも部分的な計測値を
        返せるようにする(SWE-benchの場合はコンテナのクリーンアップにも到達できるようにする)、
        失われてしまわないようにするための関数。ShutdownRequestedを即座に送出するため
        (詳細はShutdownRequested参照)、LLM呼び出し中に届いたSIGTERMも自然終了を
        待たずにすぐに中断される。外側のハードタイムアウトは、セクション6.1に従い
        moulinetteのrun-agentコマンド側で別途強制される。"""
        self._stop_requested = True  # 停止要求フラグを立てる
        raise ShutdownRequested("shutdown requested (e.g. SIGTERM)")  # 呼び出し元(通常はシグナルハンドラ内)から直ちに例外を送出する

    def run(self, task_id: str, benchmark: str, task_prompt: str) -> SolutionOutput:
        # 1タスク分のThought->Code->Observationループを実行し、最終的なSolutionOutputを組み立てて返すメインメソッド
        start = time.monotonic()  # 経過時間計測用の開始時刻を記録
        messages: List[dict] = [
            {"role": "system", "content": self.system_prompt},  # システムプロンプトを最初のメッセージとして設定
            {"role": "user", "content": task_prompt},  # タスク内容をユーザーメッセージとして追加
        ]
        steps: List[StepMetrics] = []  # 各ステップの計測値を貯めるリスト
        total_input_tokens = 0  # 累積入力トークン数のカウンタ
        total_output_tokens = 0  # 累積出力トークン数のカウンタ
        total_requests = 0  # LLMへの総リクエスト回数のカウンタ(リトライ含む)
        error: Optional[str] = None  # 途中で発生したエラーメッセージ(正常終了ならNoneのまま)
        solution_text = ""  # 最終的な解答文字列(final_answer()が呼ばれるまでは空)
        success = False  # タスクが成功したかどうかのフラグ
        previous_message_bytes: Optional[int] = None  # 直前ステップでのメッセージ列バイト数(トークン見積もりに使用)
        previous_input_tokens: Optional[int] = None  # 直前ステップでの実測入力トークン数(トークン見積もりに使用)

        for step_number in range(1, self.config.max_iterations + 1):
            # 1からmax_iterationsまでステップ番号を回すメインループ
            if self._stop_requested:
                error = "stopped: shutdown requested (e.g. SIGTERM)"  # 停止要求が来ていればエラーメッセージを設定
                break  # ループを抜ける
            elapsed = time.monotonic() - start  # ここまでの経過時間を計算
            if elapsed >= self.config.max_time_seconds:
                error = f"time budget exhausted ({elapsed:.1f}s >= {self.config.max_time_seconds}s)"  # 時間制限超過のエラーメッセージを設定
                break  # ループを抜ける
            if total_input_tokens >= self.config.max_input_tokens:
                error = (
                    f"input token budget exhausted "
                    f"({total_input_tokens} >= {self.config.max_input_tokens})"
                )  # 入力トークン予算超過のエラーメッセージを設定
                break  # ループを抜ける
            if total_output_tokens >= self.config.max_output_tokens:
                error = (
                    f"output token budget exhausted "
                    f"({total_output_tokens} >= {self.config.max_output_tokens})"
                )  # 出力トークン予算超過のエラーメッセージを設定
                break  # ループを抜ける

            current_message_bytes = _serialized_message_bytes(messages)  # 現在のメッセージ列のバイト数を計算
            input_token_bound = _conservative_input_token_bound(
                current_message_bytes,
                previous_message_bytes,
                previous_input_tokens,
            )  # 次のリクエストで消費されうる入力トークン数の安全側の上限を見積もる
            remaining_input_tokens = self.config.max_input_tokens - total_input_tokens  # 入力トークン予算の残量を計算
            if input_token_bound > remaining_input_tokens:
                error = (
                    "input token budget would be exceeded by the next request "
                    f"(conservative bound {input_token_bound} > "
                    f"remaining {remaining_input_tokens})"
                )  # 次のリクエストで予算を超えると見込まれる場合のエラーメッセージを設定
                break  # ループを抜ける

            remaining_output_tokens = self.config.max_output_tokens - total_output_tokens  # 出力トークン予算の残量を計算
            request_output_limit = min(
                self.config.max_tokens_per_request,
                remaining_output_tokens,
            )  # 1リクエストあたりの上限と残り予算の小さい方を今回の出力上限として採用

            try:
                gen = self.llm_client.generate(
                    messages,
                    stop=self.config.stop_sequences,
                    max_output_tokens=request_output_limit,
                )  # LLMにメッセージ列を送り、応答を生成させる
            except AllProvidersExhaustedError as exc:
                total_requests += exc.attempted_requests  # 試行した全リクエスト数を加算
                error = f"LLM request failed: {exc}"  # 全プロバイダ失敗のエラーメッセージを設定
                break  # ループを抜ける
            except ShutdownRequested as exc:
                error = str(exc)  # 停止要求による中断メッセージを設定
                break  # ループを抜ける

            total_requests += 1 + gen.retries  # 今回の呼び出し分(本試行+リトライ回数)を総リクエスト数に加算
            total_input_tokens += gen.input_tokens  # 今回消費した入力トークン数を累積に加算
            total_output_tokens += gen.output_tokens  # 今回消費した出力トークン数を累積に加算
            previous_message_bytes = current_message_bytes  # 次回の見積もりのために今回のメッセージバイト数を保存
            previous_input_tokens = gen.input_tokens  # 次回の見積もりのために今回の実測入力トークン数を保存

            extraction = extract_code(gen.text)  # LLM出力からPythonコード部分を抽出
            sandbox_input = extraction.code or ""  # 抽出されたコード(なければ空文字列)をサンドボックス入力として記録用に保持
            final_answer_raised: Optional[FinalAnswer] = None  # final_answer()が呼ばれた場合にその例外を保持する変数

            if extraction.code is None:
                observation = extraction.note  # コードが抽出できなかった場合は、その理由メモをそのままObservationとする
            else:
                try:
                    sandbox_output = self.sandbox.run(extraction.code)  # 抽出したコードをサンドボックス内で実行
                    observation = (
                        f"{extraction.note}\n{sandbox_output}" if extraction.note else sandbox_output
                    )  # 補足メモがあれば実行結果の前に付加してObservationとする
                except FinalAnswer as fa:
                    final_answer_raised = fa  # final_answer()呼び出しによる例外を捕捉して保持
                    observation = f"[FinalAnswer submitted] {fa.answer!r}"  # 提出された解答内容をObservationとして記録

            steps.append(
                StepMetrics(
                    step=step_number,  # このステップの番号
                    input_tokens=gen.input_tokens,  # このステップで消費した入力トークン数
                    output_tokens=gen.output_tokens,  # このステップで消費した出力トークン数
                    request_time_ms=gen.request_time_ms,  # このステップのLLMリクエストにかかった時間(ミリ秒)
                    api_url=gen.api_url,  # 実際に使用されたAPIのURL
                    model_name=gen.model_name,  # 実際に使用されたモデル名
                    llm_output=gen.text,  # LLMが生成した生テキスト
                    sandbox_input=sandbox_input,  # サンドボックスに渡したコード
                    sandbox_output=observation,  # サンドボックス実行結果(Observation)
                    retries=gen.retries,  # このステップでのリトライ回数
                )
            )  # 今回のステップの計測値をstepsリストに追加

            if final_answer_raised is not None:
                success = True  # final_answer()が呼ばれたのでタスク成功とみなす
                solution_text = str(final_answer_raised.answer)  # 提出された解答を文字列化して保存
                break  # ループを抜ける(タスク完了)

            messages.append({"role": "assistant", "content": gen.text})  # LLMの応答をアシスタントメッセージとして会話履歴に追加
            messages.append({"role": "user", "content": f"Observation:\n{observation}"})  # 実行結果(Observation)をユーザーメッセージとして会話履歴に追加
        else:
            # forループがbreakされずに最後まで回りきった場合(=最大反復回数に到達した場合)の処理
            error = f"max iterations reached ({self.config.max_iterations})"  # 最大反復回数到達のエラーメッセージを設定

        if not success and error is None:
            error = "loop ended without a final_answer() call"  # 成功もエラーもない状態でループが終わった場合の保険的なエラーメッセージ

        return SolutionOutput(
            task_id=task_id,  # 対象タスクのID
            benchmark=benchmark,  # ベンチマーク種別("mbpp"または"swebench")
            success=success,  # タスクが成功したかどうか
            solution=solution_text,  # 最終的な解答文字列
            iterations=len(steps),  # 実際に実行されたステップ数
            total_requests=total_requests,  # LLMへの総リクエスト回数
            total_input_tokens=total_input_tokens,  # 累積入力トークン数
            total_output_tokens=total_output_tokens,  # 累積出力トークン数
            total_time_seconds=time.monotonic() - start,  # 全体の実行時間(秒)
            steps=steps,  # 各ステップの計測値一覧
            system_prompt=self.system_prompt,  # 使用したシステムプロンプト
            error=None if success else error,  # 成功時はNone、失敗時はエラーメッセージ
            timestamp=datetime.now().isoformat(),  # 現在時刻をISO形式で記録
        )
