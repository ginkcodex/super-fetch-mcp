"""super-fetch: an MCP server that fetches web pages the way a normal browser user sees them."""

from importlib.metadata import version

__version__ = version("super-fetch")


def main() -> None:
    from .cli import main as cli_main

    cli_main()
