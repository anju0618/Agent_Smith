"""Docker container lifecycle for SWE-bench tasks (Section 4.4).

Implements approach (b) from the subject: the sandbox itself (the Python
interpreter that executes the LLM's generated code) stays on the host. What
moves into the container is the *MCP tool server* - it is started there via
`docker exec` so its filesystem/test/git operations run against the actual
task environment, while the sandbox only ever talks to it over that exec'd
stdio pipe. mcp_tools_swebench.py has no Docker-awareness of its own; this
module is what decides *where* it runs.

NOTE: this module has not been exercised against a real SWE-bench image in
this environment (no Docker image pull / live run was performed while writing
it) - see BENCHMARK_REPORT.md and README.md for what has and hasn't been
verified end-to-end.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Optional

import docker

TESTBED_PATH_IN_CONTAINER = "/testbed"
EVAL_SCRIPT_PATH_IN_CONTAINER = "/eval.sh"
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
        container_id: str = self._container.id

        eval_tmp = Path("/tmp") / f"agent_smith_eval_{container_id[:12]}.sh"
        eval_tmp.write_text(eval_script)
        subprocess.run(
            ["docker", "cp", str(eval_tmp), f"{container_id}:{EVAL_SCRIPT_PATH_IN_CONTAINER}"], check=True
        )
        subprocess.run(
            ["docker", "cp", str(tools_file), f"{container_id}:{TOOLS_PATH_IN_CONTAINER}"], check=True
        )

        self._bootstrap_dependencies()

    def _bootstrap_dependencies(self) -> None:
        """Best-effort install of the MCP tool server's runtime deps inside the
        container. If the container has no network access this fails cleanly -
        the caller (agent_swebench.py) surfaces that as a graceful agent error
        rather than crashing (General Rules: "all errors must be handled
        gracefully")."""
        assert self._container is not None
        check = self._container.exec_run("python3 -c 'import mcp'")
        if check.exit_code == 0:
            return
        install = self._container.exec_run("pip install --quiet 'mcp>=1.2.0,<2' 'pydantic>=2'")
        if install.exit_code != 0:
            raise RuntimeError(
                "Could not install the MCP server's dependencies inside the container "
                f"(likely no network access in this image): {install.output.decode(errors='replace')}"
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
