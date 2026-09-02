"""End-to-end tests of the Thought -> Code -> Observation loop (Section 4.1)
using a fake LLM client and a real Sandbox - no network or API keys required.
"""
# ============================================================================
# 日本語解説: このファイルは orchestrator.py の Orchestrator クラス
# （Thought→Code→Observationループの心臓部）をエンドツーエンドでテストして
# います。LLMの呼び出し部分だけは _ScriptedLLMClient という「あらかじめ
# 決めておいた応答を順番に返すだけの偽物」に差し替えていますが、
# サンドボックスは本物のSandboxクラスをそのまま使っています。つまり
# 「LLMが何を書いてくるか」は台本通りに固定しつつ、「実際にコードを
# 実行して結果を得る」という部分は本物の動きをそのまま検証している、
# という構成です。
#
# ここで確認しているのは、前の会話で読んだ orchestrator.py の各仕組み
# （予算チェック、final_answer()での終了、SIGTERM相当の割り込み、
# 全プロバイダ枯渇時の挙動、トークン予算の事前見積もりなど）が、
# 実際にコードとして動かしたときに期待通りの結果を返すか、という
# 「仕様の裏付け」です。
# ============================================================================
from typing import List, Optional

import pytest

from llm.client import AllProvidersExhaustedError
from llm.provider import GenerationResult
from models import SandboxConfig
from orchestrator import Orchestrator, OrchestratorConfig, ShutdownRequested
from sandbox.executor import Sandbox


class _ScriptedLLMClient:
    """Replays a fixed sequence of LLM responses, one per generate() call."""
    # 日本語解説: 本物のLLMClientの代わりに使う「台本読み上げ係」。
    # コンストラクタで渡したtexts(文字列のリスト)を、generate()が
    # 呼ばれるたびに1つずつ順番に返していく。これにより「1ターン目は
    # こう言い、2ターン目にfinal_answerを呼ぶ」といったシナリオを
    # 決定論的に(毎回同じ結果になるように)再現できる。

    def __init__(self, texts: List[str]) -> None:
        self.texts = texts
        self.calls = 0

    def generate(
        self,
        messages: List[dict],
        stop: Optional[List[str]] = None,
        max_output_tokens: int = 1024,
    ) -> GenerationResult:
        text = self.texts[self.calls]
        self.calls += 1
        return GenerationResult(
            text=text,
            input_tokens=20,
            output_tokens=10,
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
    # テスト用にOrchestratorを組み立てるヘルパー。サンドボックスは
    # authorized_imports=[]（何もimportできない）という最小構成にしている
    # ——このテストファイルの目的はサンドボックスの中身を検証することでは
    # なく、あくまでOrchestratorのループ制御ロジックの検証だから。
    sandbox = Sandbox(
        SandboxConfig(authorized_imports=[], allowed_directories=[]),
        apply_process_memory_limit=False,
    )
    config = OrchestratorConfig(
        max_iterations=max_iterations,
        max_input_tokens=max_input_tokens,
        max_output_tokens=max_output_tokens,
        max_time_seconds=max_time_seconds,
    )
    return Orchestrator(llm_client, sandbox, "system prompt", config)  # type: ignore[arg-type]


def test_final_answer_ends_the_loop_successfully() -> None:
    # 最も基本的な正常系シナリオ:
    #   1ターン目: print(1+1) というコードを実行するだけ
    #   2ターン目: final_answer(...) を呼んで終了
    # これにより success=True, error=None, solution に渡した関数コードが
    # 入り、iterations=2（2ターン分回った）、1ターン目のsandbox_outputが
    # "2"（1+1の結果）になっていること、system_promptがそのまま
    # SolutionOutputに保存されていること、トークン集計が
    # 正しく積算されている(1ターン20入力・10出力×2ターン=40/20)ことを
    # まとめて確認している。
    llm_client = _ScriptedLLMClient(
        [
            "Thought: try\nCode:\n```python\nprint(1 + 1)\n```\n<end_code>",
            'Thought: done\nCode:\n```python\nfinal_answer("def f():\\n    return 1")\n```\n<end_code>',
        ]
    )
    orchestrator = _build_orchestrator(llm_client)
    result = orchestrator.run("1", "mbpp", "solve this task")

    assert result.success is True
    assert result.error is None
    assert result.solution == "def f():\n    return 1"
    assert result.iterations == 2
    assert result.steps[0].sandbox_output.strip() == "2"
    assert result.system_prompt == "system prompt"
    assert result.total_input_tokens == 40
    assert result.total_output_tokens == 20


def test_max_iterations_reached_without_final_answer() -> None:
    # LLMが一度もfinal_answer()を呼ばずに同じことを繰り返し続けた場合、
    # max_iterations（ここでは3）に達した時点でループが打ち切られ、
    # success=False、errorに"max iterations reached"という理由が
    # 入ることを確認する。これはorchestrator.py内の
    # for...else構文（breakされずにループが自然終了した場合だけelse節が
    # 実行される）の挙動を裏付けるテスト。
    llm_client = _ScriptedLLMClient(
        ["Thought: loop\nCode:\n```python\nprint('again')\n```\n<end_code>" for _ in range(3)]
    )
    orchestrator = _build_orchestrator(llm_client, max_iterations=3)
    result = orchestrator.run("1", "mbpp", "solve this task")

    assert result.success is False
    assert result.iterations == 3
    assert result.error is not None
    assert "max iterations reached" in result.error


def test_missing_code_block_is_reported_and_loop_continues() -> None:
    # 1ターン目はLLMがコードを一切書かず、ただ考えごとを述べているだけの
    # 応答をした場合の挙動を確認する。この場合extract_code()がcode=Noneを
    # 返すので、Orchestratorは「サンドボックス実行をスキップし、
    # [NoCodeBlock]という注記をそのままObservationとして次のターンに
    # 渡す」という分岐を通る(sandbox_inputは空文字列のまま)。
    # それでもループ自体はエラーにならず継続し、2ターン目で
    # final_answer()が呼ばれれば普通に成功することを確認している。
    llm_client = _ScriptedLLMClient(
        [
            "I am just thinking out loud with no code.",
            'Thought: ok now\nCode:\n```python\nfinal_answer("done")\n```\n<end_code>',
        ]
    )
    orchestrator = _build_orchestrator(llm_client)
    result = orchestrator.run("1", "mbpp", "solve this task")

    assert "[NoCodeBlock]" in result.steps[0].sandbox_output
    assert result.steps[0].sandbox_input == ""
    assert result.success is True
    assert result.solution == "done"


class _ShutdownOnSecondCallLLMClient:
    """Simulates a SIGTERM (ShutdownRequested) interrupting the second generate() call."""
    # 日本語解説: 1回目の呼び出しは通常のレスポンスを返すが、2回目の
    # 呼び出しでは（あたかもその瞬間にSIGTERMが届いたかのように）
    # ShutdownRequested例外を投げる偽物のLLMクライアント。実際のOSの
    # シグナルを飛ばさなくても、「LLM呼び出しの真っ最中にシャットダウンが
    # 要求されたらどうなるか」というシナリオを、テストの中で確実に
    # 再現できるようにするための仕掛け。

    def __init__(self, first_response: str) -> None:
        self.first_response = first_response
        self.calls = 0

    def generate(
        self,
        messages: List[dict],
        stop: Optional[List[str]] = None,
        max_output_tokens: int = 1024,
    ) -> GenerationResult:
        self.calls += 1
        if self.calls == 1:
            return GenerationResult(
                text=self.first_response,
                input_tokens=20,
                output_tokens=10,
                request_time_ms=5.0,
                api_url="https://fake.example/v1",
                model_name="fake-model",
            )
        raise ShutdownRequested("shutdown requested (e.g. SIGTERM)")


def test_shutdown_requested_during_llm_call_preserves_partial_steps() -> None:
    # SIGTERM相当の割り込みが2ターン目のLLM呼び出し中に発生した場合、
    # 1ターン目の記録(StepMetrics)は失われずにresult.stepsに残っている
    # こと(iterations==1、つまり「途中まででも成果を捨てない」設計)、
    # success=Falseで、errorに"shutdown requested"という理由が
    # 入ることを確認する。これはorchestrator.pyのrun()メソッド内の
    # except ShutdownRequested as exc: break という分岐の挙動そのもの。
    llm_client = _ShutdownOnSecondCallLLMClient(
        "Thought: x\nCode:\n```python\nprint(1)\n```\n<end_code>"
    )
    orchestrator = _build_orchestrator(llm_client, max_iterations=5)  # type: ignore[arg-type]

    result = orchestrator.run("1", "mbpp", "solve this task")

    assert result.success is False
    assert result.iterations == 1  # the first step's metrics were kept, not discarded
    assert result.error is not None
    assert "shutdown requested" in result.error


def test_request_stop_raises_shutdown_requested_immediately() -> None:
    # request_stop()を直接呼んだときの単体テスト。呼んだ瞬間に
    # ShutdownRequestedが送出されること、そして内部フラグ
    # _stop_requestedがTrueになっていることを確認する
    # （このフラグはループの毎ターン冒頭でもチェックされる二重の安全策）。
    orchestrator = _build_orchestrator(_ScriptedLLMClient([]))

    with pytest.raises(ShutdownRequested):
        orchestrator.request_stop()
    assert orchestrator._stop_requested is True


class _AlwaysExhaustedLLMClient:
    # generate()を呼ぶと必ずAllProvidersExhaustedErrorを投げる偽物。
    # 「全プロバイダ・全キーが尽きた」という最悪のシナリオを再現するための道具。
    def generate(
        self,
        messages: List[dict],
        stop: Optional[List[str]] = None,
        max_output_tokens: int = 1024,
    ) -> GenerationResult:
        raise AllProvidersExhaustedError("all keys exhausted", attempted_requests=4)


def test_total_requests_counts_failed_attempts_when_all_providers_exhausted() -> None:
    """Every failed HTTP attempt still counts toward SolutionOutput.total_requests
    (Section 5.1's "including retries"), even though the call never succeeds
    and so never produces a StepMetrics entry to attach it to."""
    # 日本語解説: test_llm_client.pyで見た「全滅した試行の回数も失われては
    # いけない」という要件を、今度はOrchestrator側から見て確認している
    # テスト。AllProvidersExhaustedErrorが持つattempted_requests(4回分の
    # 実際のHTTPリクエスト)が、たとえ一度も成功せずStepMetricsが
    # 1つも作られなかった(iterations==0)場合でも、
    # result.total_requestsに正しく反映されることを確認する。
    orchestrator = _build_orchestrator(_AlwaysExhaustedLLMClient())  # type: ignore[arg-type]

    result = orchestrator.run("1", "mbpp", "solve this task")

    assert result.success is False
    assert result.total_requests == 4
    assert result.iterations == 0
    assert "LLM request failed" in (result.error or "")


def test_token_budget_stops_the_loop_before_exceeding_it() -> None:
    # max_input_tokensを非常に小さく(15)設定した状態で、この会話文脈
    # (system prompt + user prompt)を送ろうとすると、実際にAPIを叩く前の
    # 「事前見積もり」(_conservative_input_token_bound)の段階で
    # 「次のリクエストで予算を超過しそうだ」と判断され、
    # llm_client.generate()が一度も呼ばれない(llm_client.calls == 0)まま
    # ループが打ち切られることを確認する。実際にAPIを叩いてから
    # 超過が判明するのではなく、送信前に防ぐ設計であることの裏付け。
    llm_client = _ScriptedLLMClient(
        ["Thought: x\nCode:\n```python\nprint(1)\n```\n<end_code>" for _ in range(5)]
    )
    orchestrator = _build_orchestrator(llm_client, max_input_tokens=15, max_iterations=5)
    result = orchestrator.run("1", "mbpp", "solve this task")

    assert result.success is False
    assert result.iterations == 0
    assert result.total_input_tokens == 0
    assert llm_client.calls == 0
    assert result.error is not None
    assert "would be exceeded" in result.error


def test_input_budget_uses_previous_observed_usage_for_later_requests() -> None:
    # _conservative_input_token_bound()の「2回目以降は前回の実測トークン数を
    # 土台にする」という挙動を確認するテスト。max_input_tokens=300という、
    # 初回のバイト数ベースの見積もりだけではギリギリ足りるかどうか
    # 微妙な値を設定し、それでも2ターン目まで正しく完走して
    # final_answer()に到達できることを確認している
    # （もし毎回バイト数ベースの粗い見積もりを使っていたら、より早い
    # 段階で予算超過と誤判定されてしまう可能性がある、という設計意図の裏付け）。
    llm_client = _ScriptedLLMClient(
        [
            "```python\nprint(1)\n```",
            '```python\nfinal_answer("done")\n```',
        ]
    )
    orchestrator = _build_orchestrator(llm_client, max_input_tokens=300)

    result = orchestrator.run("1", "mbpp", "task")

    assert result.success is True
    assert result.iterations == 2
    assert result.total_input_tokens == 40


class _OutputLimitCapturingLLMClient:
    # generate()が呼ばれるたびに、実際に渡されたmax_output_tokensの値を
    # requested_limitsに記録しておく偽物。「Orchestratorが1リクエストあたりの
    # 出力上限をどう計算して渡しているか」を外から観察するための道具。
    def __init__(self) -> None:
        self.requested_limits: List[int] = []

    def generate(
        self,
        messages: List[dict],
        stop: Optional[List[str]] = None,
        max_output_tokens: int = 1024,
    ) -> GenerationResult:
        self.requested_limits.append(max_output_tokens)
        return GenerationResult(
            text='```python\nfinal_answer("done")\n```',
            input_tokens=20,
            output_tokens=max_output_tokens,
            request_time_ms=5.0,
            api_url="https://fake.example/v1",
            model_name="fake-model",
        )


def test_output_request_is_clamped_to_remaining_hard_limit() -> None:
    # max_output_tokens(タスク全体の出力トークン予算)を7という
    # 非常に小さい値に設定すると、orchestrator.pyのrun()内で
    # request_output_limit = min(max_tokens_per_request, remaining_output_tokens)
    # という計算により、1回のリクエストに渡すmax_output_tokensの値も
    # 残り予算(7)に合わせて切り詰められることを確認する。
    # これにより、たとえLLM側の1リクエストあたりの上限設定
    # (max_tokens_per_request、デフォルト1024)がもっと大きくても、
    # タスク全体の予算を超えて出力させてしまうことがない。
    llm_client = _OutputLimitCapturingLLMClient()
    orchestrator = _build_orchestrator(
        llm_client,  # type: ignore[arg-type]
        max_output_tokens=7,
    )

    result = orchestrator.run("1", "mbpp", "solve this task")

    assert result.success is True
    assert result.total_output_tokens == 7
    assert llm_client.requested_limits == [7]
