import argparse


def main() -> None:
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "config",
        nargs="?",
        default=None,
    )

    parser.add_argument(
        "--mcp-stdio",
        default=None,
    )

    parser.add_argument(
        "--mcp-server",
        default=None,
    )

    args = parser.parse_args()

    print(args)
