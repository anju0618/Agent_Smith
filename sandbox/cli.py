import argparse
import json
from pathlib import Path

from models import SandboxConfig


def load_config(path: str | None) -> SandboxConfig:
    if path is None:
        return SandboxConfig()

    config_path = Path(path)

    with config_path.open("r", encoding="utf-8") as file:
        data = json.load(file)

    return SandboxConfig.model_validate(data)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Agent Smith sandbox"
    )

    parser.add_argument(
        "config",
        nargs="?",
        default=None,
        help="Path to sandbox configuration JSON file",
    )

    parser.add_argument(
        "--mcp-stdio",
        default=None,
        help="Command used to launch an MCP server over stdio",
    )

    parser.add_argument(
        "--mcp-server",
        default=None,
        help="URL of an MCP server",
    )

    args = parser.parse_args()

    config = load_config(args.config)

    print("Sandbox config:")
    print(config)

    print("MCP stdio:", args.mcp_stdio)
    print("MCP server:", args.mcp_server)


if __name__ == "__main__":
    main()
