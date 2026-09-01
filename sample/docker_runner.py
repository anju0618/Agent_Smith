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
    """Owns one running SWE-bench container for the lifetime of one task."""

    def __init__(self, docker_image: str) -> None:
        self.docker_image = docker_image
        self._client = docker.from_env()
        self._container: Optional[Any] = None

    def start(self, eval_script: str, tools_file: Path) -> None:
        """Pull the task's image, start a long-lived container, and copy in
        the eval script + MCP tool server (Section 4.4: "(a) deploy the
        sandbox inside the Docker container, or (b) run the sandbox on the
        host with MCP tools bridging into Docker" - we implement (b))."""
        self._client.images.pull(self.docker_image)
        self._container = self._client.containers.run(
            self.docker_image, command="tail -f /dev/null", detach=True
        )

        self._write_into_container(eval_script, EVAL_SCRIPT_PATH_IN_CONTAINER)
        self._write_into_container(tools_file.read_text(), TOOLS_PATH_IN_CONTAINER)

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
        assert self._container is not None
        subprocess.run(
            ["docker", "exec", "-i", self._container.id, "sh", "-c", f"cat > {shlex.quote(container_path)}"],
            input=content,
            text=True,
            check=True,
            timeout=30,
        )

    def _bootstrap_dependencies(self) -> None:
        """Best-effort install of the MCP tool server's runtime deps inside the
        container. If the container has no network access this fails cleanly -
        the caller (agent_swebench.py) surfaces that as a graceful agent error
        rather than crashing (General Rules: "all errors must be handled
        gracefully")."""
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
        """Shell command that starts the MCP tool server inside this container,
        for handing to MCPToolProxy(stdio_command=...)."""
        assert self._container is not None
        container_id: str = self._container.id
        return (
            f"docker exec -i "
            f"-e TESTBED_PATH={TESTBED_PATH_IN_CONTAINER} "
            f"-e AGENT_SMITH_EVAL_SCRIPT={EVAL_SCRIPT_PATH_IN_CONTAINER} "
            f"{container_id} python3 {TOOLS_PATH_IN_CONTAINER}"
        )

    def cleanup(self) -> None:
        """Stop and remove the container - Section 4.4: "you are responsible
        for cleaning it up after your program execution"."""
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
