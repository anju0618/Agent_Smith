"""SWE-benchタスク用のDockerコンテナのライフサイクル管理(Section 4.4)。

課題の方式(b)を実装している: サンドボックス自体(LLMが生成したコードを実行する
Pythonインタプリタ)はホスト側に留まる。コンテナ内に移動するのは *MCPツール
サーバー* であり、`docker exec` 経由で起動することで、そのファイルシステム/
テスト/git操作が実際のタスク環境に対して実行される一方、サンドボックスは
その exec されたstdioパイプ越しにしかそのサーバーと会話しない。
mcp_tools_swebench.py自体はDockerについて何も知らない。このモジュールが
「どこで実行するか」を決めている。

実際の `swebench/sweb.eval.x86_64.*` イメージ3つに対してエンドツーエンドで
検証済み(pull、コンテナ起動、MCP依存関係のブートストラップ、ツール呼び出し、
`get_patch()`、クリーンアップ) - 完全な実行結果と、これによって発見・修正された
実際の `docker cp`/UIDリマッピングのバグについてはBENCHMARK_REPORT.mdを参照
(下記の `_write_into_container` の `docker exec` 標準入力リダイレクトによって
現在は完全に回避されている)。
"""
from __future__ import annotations

import shlex
import subprocess
from pathlib import Path
from typing import Any, Optional

import docker

TESTBED_PATH_IN_CONTAINER = "/testbed"
EVAL_SCRIPT_PATH_IN_CONTAINER = f"{TESTBED_PATH_IN_CONTAINER}/eval.sh"
TOOLS_PATH_IN_CONTAINER = "/agent_smith_mcp_tools_swebench.py"


class SweBenchContainer:
    """1つのタスクの生存期間中、実行中のSWE-benchコンテナを1つ所有するクラス。"""

    def __init__(self, docker_image: str) -> None:
        self.docker_image = docker_image
        self._client = docker.from_env()
        self._container: Optional[Any] = None

    def start(self, eval_script: str, tools_file: Path) -> None:
        """タスクのイメージをpullし、長時間稼働するコンテナを起動し、評価スクリプトと
        MCPツールサーバーをコピーする(Section 4.4: 「(a) サンドボックスをDocker
        コンテナ内にデプロイする、または (b) サンドボックスをホスト上で実行し、
        MCPツールでDockerへブリッジする」のうち、我々は(b)を実装している)。"""
        self._client.images.pull(self.docker_image)
        self._container = self._client.containers.run(
            self.docker_image, command="tail -f /dev/null", detach=True
        )

        self._write_into_container(eval_script, EVAL_SCRIPT_PATH_IN_CONTAINER)

        self._write_into_container(tools_file.read_text(), TOOLS_PATH_IN_CONTAINER)

        self._bootstrap_dependencies()

    def _write_into_container(self, content: str, container_path: str) -> None:
        """`content` をコンテナ内の `container_path` に書き込む。

        `docker cp` ではなく `docker exec` の標準入力リダイレクトを使用している:
        `docker cp` のtarベースのコピーは、展開したファイルをホストのUIDに
        合わせて `lchown` しようとするが、そのUIDがコンテナのユーザー名前空間の
        リマッピング範囲外にある場合(ユーザーごとのsubuid範囲を持つ共有ホストで
        実際に本環境で発生した)「invalid argument」エラーで失敗する。
        `docker exec` 経由でパイプすると、コンテナ自身のエントリポイントが
        実行しているユーザーとして書き込まれるため、このホスト側のUID
        マッピング問題を完全に回避できる。

        このメソッドは(標準入力のパイピングのために)docker CLIを直接シェル
        呼び出ししており、このクラスの他の箇所で使われているdocker-pyクライアント
        経由ではない。そのため、そのクライアントのデフォルト60秒のタイムアウトを
        継承しない - 代わりにここで明示的にタイムアウトを設定しているので、
        応答のないコンテナがあった場合、moulinette自身の外側のプロセスkillまで
        タイムアウトなしにcontainer.start()(ひいてはエージェント全体)がハングする
        のではなく、この工程が明確なTimeoutExpiredで失敗するようになる。
        """
        assert self._container is not None
        subprocess.run(
            ["docker", "exec", "-i", self._container.id, "sh", "-c", f"cat > {shlex.quote(container_path)}"],
            input=content,
            text=True,
            check=True,
            timeout=30,
        )

    def _bootstrap_dependencies(self) -> None:
        """MCPツールサーバーの実行時依存関係をコンテナ内にベストエフォートでインストールする。
        コンテナにネットワークアクセスがない場合、これはきれいに失敗する -
        呼び出し元(agent_swebench.py)はこれをクラッシュではなく、エージェントの
        穏当なエラーとして表面化させる(General Rules: 「全てのエラーは
        gracefulに処理されなければならない」)。"""
        assert self._container is not None
        container_id = self._container.id
        try:
            check = subprocess.run(
                ["docker", "exec", container_id, "python3", "-c", "import mcp"],
                capture_output=True,
                timeout=30,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("Timed out checking MCP dependencies in the container") from exc
        if check.returncode == 0:
            return
        try:
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
            raise RuntimeError(
                "Could not install the MCP server's dependencies inside the container "
                f"(likely no network access in this image): "
                f"{install.stderr.decode(errors='replace')}"
            )

    def mcp_stdio_command(self) -> str:
        """このコンテナ内でMCPツールサーバーを起動するシェルコマンドを返す。
        MCPToolProxy(stdio_command=...)に渡すためのもの。"""
        assert self._container is not None
        container_id: str = self._container.id
        return (
            f"docker exec -i "
            f"-e TESTBED_PATH={TESTBED_PATH_IN_CONTAINER} "
            f"-e AGENT_SMITH_EVAL_SCRIPT={EVAL_SCRIPT_PATH_IN_CONTAINER} "
            f"{container_id} python3 {TOOLS_PATH_IN_CONTAINER}"
        )

    def cleanup(self) -> None:
        """コンテナを停止・削除する - Section 4.4: 「プログラム実行後に
        自分でクリーンアップする責任がある」に対応。"""
        if self._container is None:
            return
        try:
            self._container.stop(timeout=5)
        except Exception:
            pass
        try:
            self._container.remove(force=True)
        except Exception:
            pass
        self._container = None
