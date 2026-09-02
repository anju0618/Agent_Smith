"""Docker container lifecycle for SWE-bench tasks (Section 4.4).

Implements approach (b) from the subject: the sandbox itself (the Python
interpreter that executes the LLM's generated code) stays on the host. What
moves into the container is the *MCP tool server* - it is started there via
`docker exec` so its filesystem/test/git operations run against the actual
task environment, while the sandbox only ever talks to it over that exec'd
stdio pipe. mcp_tools_swebench.py has no Docker-awareness of its own; this
module is what decides *where* it runs.

Exercised end to end against three real `swebench/sweb.eval.x86_64.*` images
(pull, container start, MCP dependency bootstrap, tool calls, `get_patch()`,
cleanup) - see BENCHMARK_REPORT.md for the full run and for a real
`docker cp`/UID-remapping bug this found and fixed (now avoided entirely via
`_write_into_container`'s `docker exec` stdin redirection below).
"""
# このファイルの役割を一言でいうと「1タスク分のDockerコンテナの一生
# (起動→道具を仕込む→依存関係を整える→接続方法を教える→掃除する)を
# 管理するクラスを1つ提供するだけ」のファイル。SWE-bench固有のロジック
# (ファイル読み書きやgit diffの取り方)は一切ここには無い - それは
# mcp_tools_swebench.py の役目で、このファイルは「それをどこで走らせるか」
# だけを決める。
from __future__ import annotations

import shlex
import subprocess
from pathlib import Path
from typing import Any, Optional

import docker

# コンテナ内部でのパス。ホスト側のパスとは無関係の、コンテナの中だけで
# 意味を持つ固定値。
TESTBED_PATH_IN_CONTAINER = "/testbed"
EVAL_SCRIPT_PATH_IN_CONTAINER = f"{TESTBED_PATH_IN_CONTAINER}/eval.sh"
TOOLS_PATH_IN_CONTAINER = "/agent_smith_mcp_tools_swebench.py"


class SweBenchContainer:
    """Owns one running SWE-bench container for the lifetime of one task."""
    # 1インスタンス = 1タスク分のコンテナのライフサイクルを表すクラス。
    # agent_swebench.py の main() が1つだけ生成し、try/finallyで
    # start()→(エージェント実行)→cleanup() の順に使う。

    def __init__(self, docker_image: str) -> None:
        self.docker_image = docker_image
        # docker.from_env() はホストのDockerデーモンに接続するクライアントを
        # 環境変数(DOCKER_HOSTなど)から自動構成する、docker-pyの標準的な使い方。
        self._client = docker.from_env()
        # コンテナ本体への参照。start()が呼ばれるまではNoneのまま
        # (「まだ何も起動していない」ことを型で表現している)。
        self._container: Optional[Any] = None

    def start(self, eval_script: str, tools_file: Path) -> None:
        """Pull the task's image, start a long-lived container, and copy in
        the eval script + MCP tool server (Section 4.4: "(a) deploy the
        sandbox inside the Docker container, or (b) run the sandbox on the
        host with MCP tools bridging into Docker" - we implement (b))."""
        # (1) タスク指定のDockerイメージを取得する。このイメージには
        # 対象リポジトリのソースコードとバグ修正前の環境がまるごと
        # 入っている(SWE-bench標準の評価用イメージ)。
        self._client.images.pull(self.docker_image)
        # (2) コンテナを起動する。command="tail -f /dev/null" は
        # 「何もせず、ただ生き続けるだけ」のコマンド - このコンテナ自体は
        # 何かを実行するためではなく、あとから docker exec で個別に
        # コマンドを打ち込むための「土台」として存在する。detach=True で
        # バックグラウンド実行し、run()自体はすぐにcontainerオブジェクトを
        # 返す。
        self._container = self._client.containers.run(
            self.docker_image, command="tail -f /dev/null", detach=True
        )

        # (3) タスク固有のeval.sh(正解判定用スクリプト)と、
        # mcp_tools_swebench.py(このプロジェクト側が書いたMCPツール
        # サーバーの実体)を、コンテナの中の決まった場所に書き込む。
        # イメージ自体にはこれらのファイルは含まれていないので、
        # 実行のたびにこちらから注入する必要がある。
        self._write_into_container(eval_script, EVAL_SCRIPT_PATH_IN_CONTAINER)
        self._write_into_container(tools_file.read_text(), TOOLS_PATH_IN_CONTAINER)

        # (4) mcp_tools_swebench.py を動かすのに必要なPythonパッケージ
        # (mcp/pydantic)がコンテナ内に無ければインストールする。
        self._bootstrap_dependencies()

    def _write_into_container(self, content: str, container_path: str) -> None:
        """Write `content` to `container_path` inside the container.

        Uses `docker exec` stdin redirection rather than `docker cp`: `docker
        cp`'s tar-based copy tries to `lchown` the extracted file to match the
        host UID, which fails with "invalid argument" on hosts where that UID
        falls outside the container's user-namespace remapping range (hit live
        against a real SWE-bench image in this environment - a shared host
        with per-user subuid ranges). Piping through `docker exec` writes as
        whatever user the container's own entrypoint runs as, sidestepping
        that host-side UID mapping entirely.

        This shells out to the `docker` CLI directly (for stdin piping)
        rather than going through the docker-py client used elsewhere in this
        class, so it doesn't inherit that client's default 60s per-call
        timeout - bounded here explicitly instead, so a stuck/unresponsive
        container fails this step with a clear TimeoutExpired rather than
        hanging container.start() (and therefore the whole agent) with no
        timeout at all until moulinette's own outer process kill.
        """
        # ↑ 原文docstringの補足説明。要点を日本語でまとめると:
        #
        # 素朴には `docker cp` (docker-pyの client.containers.get(...).put_archive
        # やCLIの `docker cp`)でファイルをコンテナへコピーすればよさそうに
        # 見えるが、これはtarアーカイブとして展開する際にホスト側のUIDへ
        # `lchown`しようとする。このプロジェクトが動いている環境のように、
        # ユーザーごとに割り当てられたsubuid/subgidレンジでコンテナのUID
        # リマッピングを行うホストでは、ホストユーザーのUIDがそのレンジ外に
        # 落ちてしまい、`Error response from daemon: failed to Lchown ...:
        # invalid argument` で失敗する。これは実際にこの開発環境で遭遇した
        # 不具合で、moulinette自身の `validate swebench` コマンドも内部で
        # 同じエラーを踏むことが確認されている(=このプロジェクト固有の
        # バグではなく、環境依存の既知の問題)。
        #
        # 対策: `docker cp` を一切使わず、
        #   docker exec -i <container_id> sh -c 'cat > <path>'
        # という形で、標準入力(stdin)からファイルの中身をパイプで
        # 流し込む方式にする。この書き込みは「コンテナのエントリポイントが
        # 動いているユーザー」として行われるため、ホスト側のUIDマッピングの
        # 影響を一切受けない。
        #
        # さらに、この `docker exec` 呼び出しは docker-py の
        # クライアント(self._client)ではなく `subprocess.run(["docker", ...])`
        # で**dockerのCLIを直接**呼んでいる。docker-pyクライアント経由だと
        # デフォルトで60秒のタイムアウトが暗黙にかかるが、CLI呼び出しは
        # それを継承しない。そこでtimeout=30を明示的に指定し、コンテナが
        # 応答不能になった場合でも、このメソッド(延いてはstart()、延いては
        # エージェント全体)が無期限にハングしないようにしている。
        assert self._container is not None
        subprocess.run(
            # shlex.quote(container_path)で書き込み先パスをシェルエスケープし、
            # パスにスペースなど特殊文字が含まれていてもコマンドインジェクション
            # にならないようにしている。
            ["docker", "exec", "-i", self._container.id, "sh", "-c", f"cat > {shlex.quote(container_path)}"],
            input=content,      # ← ここでcontentがstdin経由でsh -cに渡る
            text=True,
            check=True,          # 失敗(非ゼロ終了コード)なら例外を送出させる
            timeout=30,
        )

    def _bootstrap_dependencies(self) -> None:
        """Best-effort install of the MCP tool server's runtime deps inside the
        container. If the container has no network access this fails cleanly -
        the caller (agent_swebench.py) surfaces that as a graceful agent error
        rather than crashing (General Rules: "all errors must be handled
        gracefully")."""
        # mcp_tools_swebench.py を実行するには mcp / pydantic パッケージが
        # コンテナ内のPythonから import できる必要がある。しかしSWE-bench標準
        # イメージにはそれらは入っていない前提なので、ここで確認・必要なら
        # インストールする。
        assert self._container is not None
        container_id = self._container.id
        try:
            # まず `python3 -c "import mcp"` を試し、既に入っているかどうかを
            # 確認する。check=False にしているのは、import失敗(returncode!=0)を
            # 例外ではなく「まだ入っていない」という正常な分岐として扱うため。
            check = subprocess.run(
                ["docker", "exec", container_id, "python3", "-c", "import mcp"],
                capture_output=True,
                timeout=30,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("Timed out checking MCP dependencies in the container") from exc
        if check.returncode == 0:
            # 既にimportできた = 追加インストール不要。ここで即return。
            return
        try:
            # pip install。--quiet で出力を抑制。timeout=300(5分)と、
            # 単なるimportチェックより大幅に長い猶予を与えているのは、
            # パッケージのダウンロード・ビルドには時間がかかりうるため。
            install = subprocess.run(
                [
                    "docker", "exec", container_id, "pip", "install", "--quiet",
                    "mcp>=1.2.0,<2", "pydantic>=2",
                ],
                capture_output=True,
                timeout=300,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("Timed out installing MCP dependencies in the container") from exc
        if install.returncode != 0:
            # ここに来るのは主に「イメージにネットワークアクセスが無く
            # pip installそのものが失敗する」ケース。RuntimeErrorとして
            # 投げることで、呼び出し元(agent_swebench.py)のtry/exceptが
            # これを「エージェントのグレースフルな失敗」として拾い、
            # クラッシュではなくerror_solution付きのsolution.jsonとして
            # 正常に書き出す。
            raise RuntimeError(
                "Could not install the MCP server's dependencies inside the container "
                f"(likely no network access in this image): "
                f"{install.stderr.decode(errors='replace')}"
            )

    def mcp_stdio_command(self) -> str:
        """Shell command that starts the MCP tool server inside this container,
        for handing to MCPToolProxy(stdio_command=...)."""
        # agent_swebench.py がこの戻り値を
        # MCPToolProxy(stdio_command=container.mcp_stdio_command())
        # へそのまま渡す。MCPToolProxy はこの文字列をシェルコマンドとして
        # サブプロセス起動するだけで、その中身が実は「別プロセスをdocker exec
        # 越しにコンテナの中で立ち上げるコマンドだ」ということは一切知らない
        # ── これが「mcp_tools_swebench.py自体にはDocker固有のロジックが
        # 無い」設計を成立させている、Docker側からの唯一の橋渡し。
        assert self._container is not None
        container_id: str = self._container.id
        return (
            f"docker exec -i "
            # -e TESTBED_PATH=... と -e AGENT_SMITH_EVAL_SCRIPT=... で、
            # コンテナ内で起動するmcp_tools_swebench.pyプロセスに
            # 環境変数を渡す。mcp_tools_swebench.py側の_testbed_root()と
            # _eval_script_path()がこれを読み取る。
            f"-e TESTBED_PATH={TESTBED_PATH_IN_CONTAINER} "
            f"-e AGENT_SMITH_EVAL_SCRIPT={EVAL_SCRIPT_PATH_IN_CONTAINER} "
            f"{container_id} python3 {TOOLS_PATH_IN_CONTAINER}"
        )

    def cleanup(self) -> None:
        """Stop and remove the container - Section 4.4: "you are responsible
        for cleaning it up after your program execution"."""
        # このメソッドはagent_swebench.pyのfinallyブロックから呼ばれる、
        # 「最後の砦」の後始末処理。したがって、途中で何が起きても
        # (コンテナが既に死んでいる、Dockerデーモンが応答しないなど)、
        # 例外を外へ投げてはいけない。
        if self._container is None:
            # そもそもstart()が呼ばれていない(=起動に失敗した等)なら
            # 何もすることが無い。
            return
        try:
            # timeout=5: SIGTERM相当の穏やかな停止要求を送り、5秒だけ待つ。
            self._container.stop(timeout=5)
        except Exception:
            # stop()が失敗しても(既に停止している等)気にせず次へ進む -
            # 後始末処理自体が例外で止まってしまう方が害が大きい。
            pass
        try:
            # force=True: 停止していてもいなくても強制的に削除する。
            self._container.remove(force=True)
        except Exception:
            pass
        # 最後に参照をクリアしておく(二重cleanup呼び出しへの耐性にもなる:
        # 次回呼ばれた際は self._container is None のガードで即returnする)。
        self._container = None
