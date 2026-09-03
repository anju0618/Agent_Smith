"""フェイクのLLMクライアントと本物のSandboxを使った、Thought -> Code -> Observation
ループ(セクション4.1)のエンドツーエンドテスト。ネットワークもAPIキーも不要。
"""
from typing import List, Optional  # 型ヒントに使用

import pytest  # pytest.raisesに使用

from llm.client import AllProvidersExhaustedError  # 全プロバイダ失敗時の例外
from llm.provider import GenerationResult  # generate()の戻り値型
from models import SandboxConfig  # サンドボックス設定
from orchestrator import Orchestrator, OrchestratorConfig, ShutdownRequested  # テスト対象のOrchestrator本体
from sandbox.executor import Sandbox  # 実際に使うサンドボックス実行環境


class _ScriptedLLMClient:
    """generate()が呼ばれるたびに、あらかじめ用意した固定の応答シーケンスを1つずつ返す。"""

    def __init__(self, texts: List[str]) -> None:
        self.texts = texts  # 順番に返す応答テキストのリスト
        self.calls = 0  # これまでの呼び出し回数

    def generate(
        self,
        messages: List[dict],
        stop: Optional[List[str]] = None,
        max_output_tokens: int = 1024,
    ) -> GenerationResult:
        text = self.texts[self.calls]  # 現在の呼び出し回数に対応する応答を取得
        self.calls += 1
        return GenerationResult(
            text=text,
            input_tokens=20,  # 固定の入力トークン数(テスト用のダミー値)
            output_tokens=10,  # 固定の出力トークン数(テスト用のダミー値)
            request_time_ms=5.0,
            api_url="https://fake.example/v1",
            model_name="fake-model",
        )


def _build_orchestrator(
    llm_client: _ScriptedLLMClient,
    max_iterations: int = 5,
    max_input_tokens: int = 10_000,
    max_output_tokens: int = 10_000,
    max_time_seconds: float = 30.0,
) -> Orchestrator:
    # テスト用に共通のOrchestratorを組み立てるヘルパー関数
    sandbox = Sandbox(
        SandboxConfig(authorized_imports=[], allowed_directories=[]),
        apply_process_memory_limit=False,  # テスト中にプロセス全体のメモリ制限をかけないようにする
    )
    config = OrchestratorConfig(
        max_iterations=max_iterations,  # 最大反復回数
        max_input_tokens=max_input_tokens,  # 入力トークンの予算上限
        max_output_tokens=max_output_tokens,  # 出力トークンの予算上限
        max_time_seconds=max_time_seconds,  # 実行時間の上限(秒)
    )
    return Orchestrator(llm_client, sandbox, "system prompt", config)  # type: ignore[arg-type]


def test_final_answer_ends_the_loop_successfully() -> None:
    # final_answer()が呼ばれるとループが正常終了し、解答が結果に反映されることを検証
    llm_client = _ScriptedLLMClient(
        [
            "Thought: try\nCode:\n```python\nprint(1 + 1)\n```\n<end_code>",  # 1回目: ただのコード実行
            'Thought: done\nCode:\n```python\nfinal_answer("def f():\\n    return 1")\n```\n<end_code>',  # 2回目: 最終解答
        ]
    )
    orchestrator = _build_orchestrator(llm_client)
    result = orchestrator.run("1", "mbpp", "solve this task")

    assert result.success is True  # 成功として終了すること
    assert result.error is None  # エラーは記録されないこと
    assert result.solution == "def f():\n    return 1"  # final_answer()の引数が解答として記録されること
    assert result.iterations == 2  # 2回のイテレーションで終了すること
    assert result.steps[0].sandbox_output.strip() == "2"  # 1回目の実行結果(1+1=2)が記録されること
    assert result.system_prompt == "system prompt"  # システムプロンプトがそのまま記録されること
    assert result.total_input_tokens == 40  # 入力トークン数の合計(20*2)
    assert result.total_output_tokens == 20  # 出力トークン数の合計(10*2)


def test_max_iterations_reached_without_final_answer() -> None:
    # final_answer()が一度も呼ばれないまま最大反復回数に達した場合、失敗として終了することを検証
    llm_client = _ScriptedLLMClient(
        ["Thought: loop\nCode:\n```python\nprint('again')\n```\n<end_code>" for _ in range(3)]
    )
    orchestrator = _build_orchestrator(llm_client, max_iterations=3)
    result = orchestrator.run("1", "mbpp", "solve this task")

    assert result.success is False  # 失敗として終了すること
    assert result.iterations == 3  # 最大反復回数まで実行されること
    assert result.error is not None  # エラーメッセージが設定されること
    assert "max iterations reached" in result.error  # 最大反復回数に達した旨が記録されること


def test_missing_code_block_is_reported_and_loop_continues() -> None:
    # コードブロックのないLLM応答があっても、エラーとして記録しつつループを継続することを検証
    llm_client = _ScriptedLLMClient(
        [
            "I am just thinking out loud with no code.",  # 1回目: コードなしの応答
            'Thought: ok now\nCode:\n```python\nfinal_answer("done")\n```\n<end_code>',  # 2回目: 正常な最終解答
        ]
    )
    orchestrator = _build_orchestrator(llm_client)
    result = orchestrator.run("1", "mbpp", "solve this task")

    assert "[NoCodeBlock]" in result.steps[0].sandbox_output  # 1回目のステップにコードなしエラーが記録されること
    assert result.steps[0].sandbox_input == ""  # 実行されたコードは空文字であること
    assert result.success is True  # 2回目で最終的に成功すること
    assert result.solution == "done"  # 2回目の解答が反映されること


class _ShutdownOnSecondCallLLMClient:
    """2回目のgenerate()呼び出し中にSIGTERM(ShutdownRequested)による割り込みが起きた状況を再現する。"""

    def __init__(self, first_response: str) -> None:
        self.first_response = first_response  # 1回目に返す応答テキスト
        self.calls = 0  # 呼び出し回数

    def generate(
        self,
        messages: List[dict],
        stop: Optional[List[str]] = None,
        max_output_tokens: int = 1024,
    ) -> GenerationResult:
        self.calls += 1
        if self.calls == 1:
            # 1回目は通常通り成功レスポンスを返す
            return GenerationResult(
                text=self.first_response,
                input_tokens=20,
                output_tokens=10,
                request_time_ms=5.0,
                api_url="https://fake.example/v1",
                model_name="fake-model",
            )
        # 2回目以降はシャットダウン要求(SIGTERM相当)を例外として送出する
        raise ShutdownRequested("shutdown requested (e.g. SIGTERM)")


def test_shutdown_requested_during_llm_call_preserves_partial_steps() -> None:
    # 2回目のLLM呼び出し中にシャットダウンが要求されても、
    # それまでに完了した1回目のステップの情報は保持されることを検証
    llm_client = _ShutdownOnSecondCallLLMClient(
        "Thought: x\nCode:\n```python\nprint(1)\n```\n<end_code>"
    )
    orchestrator = _build_orchestrator(llm_client, max_iterations=5)  # type: ignore[arg-type]

    result = orchestrator.run("1", "mbpp", "solve this task")

    assert result.success is False  # シャットダウンにより失敗扱いになること
    assert result.iterations == 1  # 1回目のステップの指標は破棄されず保持されること
    assert result.error is not None  # エラーメッセージが設定されること
    assert "shutdown requested" in result.error  # シャットダウン要求が原因である旨が記録されること


def test_request_stop_raises_shutdown_requested_immediately() -> None:
    # request_stop()を呼ぶと即座にShutdownRequestedが送出され、
    # 内部の停止フラグが立つことを検証
    orchestrator = _build_orchestrator(_ScriptedLLMClient([]))

    with pytest.raises(ShutdownRequested):
        orchestrator.request_stop()
    assert orchestrator._stop_requested is True  # 停止フラグが立っていること


class _AlwaysExhaustedLLMClient:
    # generate()を呼ぶたびに必ずAllProvidersExhaustedErrorを送出するフェイククライアント
    def generate(
        self,
        messages: List[dict],
        stop: Optional[List[str]] = None,
        max_output_tokens: int = 1024,
    ) -> GenerationResult:
        raise AllProvidersExhaustedError("all keys exhausted", attempted_requests=4)


def test_total_requests_counts_failed_attempts_when_all_providers_exhausted() -> None:
    """失敗したHTTPの各試行は、その呼び出しが最終的に成功せずStepMetricsの
    エントリを一切生成しなかったとしても、SolutionOutput.total_requests
    (セクション5.1の「リトライを含む」)に確実にカウントされなければならない。"""
    orchestrator = _build_orchestrator(_AlwaysExhaustedLLMClient())  # type: ignore[arg-type]

    result = orchestrator.run("1", "mbpp", "solve this task")

    assert result.success is False  # 失敗として終了すること
    assert result.total_requests == 4  # 試行回数4がそのまま反映されること
    assert result.iterations == 0  # 1つのステップも完了しなかったこと
    assert "LLM request failed" in (result.error or "")  # LLMリクエスト失敗が原因である旨が記録されること


def test_token_budget_stops_the_loop_before_exceeding_it() -> None:
    # 入力トークン予算を超えそうな場合、実際にはLLM呼び出しすら行わずにループを停止することを検証
    llm_client = _ScriptedLLMClient(
        ["Thought: x\nCode:\n```python\nprint(1)\n```\n<end_code>" for _ in range(5)]
    )
    orchestrator = _build_orchestrator(llm_client, max_input_tokens=15, max_iterations=5)  # 極端に低い上限
    result = orchestrator.run("1", "mbpp", "solve this task")

    assert result.success is False  # 予算超過により失敗扱いになること
    assert result.iterations == 0  # 1回もイテレーションが実行されないこと
    assert result.total_input_tokens == 0  # 入力トークンは消費されていないこと
    assert llm_client.calls == 0  # LLMは一度も呼ばれていないこと
    assert result.error is not None  # エラーメッセージが設定されること
    assert "would be exceeded" in result.error  # 予算超過が原因である旨が記録されること


def test_input_budget_uses_previous_observed_usage_for_later_requests() -> None:
    # 次のリクエストの予算チェックには、前回実際に観測されたトークン使用量が使われることを検証
    llm_client = _ScriptedLLMClient(
        [
            "```python\nprint(1)\n```",
            '```python\nfinal_answer("done")\n```',
        ]
    )
    orchestrator = _build_orchestrator(llm_client, max_input_tokens=300)

    result = orchestrator.run("1", "mbpp", "task")

    assert result.success is True  # 予算内に収まり成功すること
    assert result.iterations == 2  # 2回のイテレーションで完了すること
    assert result.total_input_tokens == 40  # 入力トークン数の合計(20*2)


class _OutputLimitCapturingLLMClient:
    # generate()に実際に渡されたmax_output_tokensの値を記録するフェイククライアント
    def __init__(self) -> None:
        self.requested_limits: List[int] = []  # 呼び出しごとのmax_output_tokens値を記録するリスト

    def generate(
        self,
        messages: List[dict],
        stop: Optional[List[str]] = None,
        max_output_tokens: int = 1024,
    ) -> GenerationResult:
        self.requested_limits.append(max_output_tokens)  # 渡された上限値を記録
        return GenerationResult(
            text='```python\nfinal_answer("done")\n```',
            input_tokens=20,
            output_tokens=max_output_tokens,  # 上限いっぱいまで出力したことにする
            request_time_ms=5.0,
            api_url="https://fake.example/v1",
            model_name="fake-model",
        )


def test_output_request_is_clamped_to_remaining_hard_limit() -> None:
    # リクエストする出力トークン上限が、設定された全体のハード上限に収まるよう
    # クランプ(切り詰め)されることを検証
    llm_client = _OutputLimitCapturingLLMClient()
    orchestrator = _build_orchestrator(
        llm_client,  # type: ignore[arg-type]
        max_output_tokens=7,  # 全体のハード上限を非常に小さく設定
    )

    result = orchestrator.run("1", "mbpp", "solve this task")

    assert result.success is True  # 成功すること
    assert result.total_output_tokens == 7  # 出力トークン数が上限7に収まること
    assert llm_client.requested_limits == [7]  # 実際にLLMへ渡されたmax_output_tokensも7であること
