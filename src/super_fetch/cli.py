"""Command line entry point: `super-fetch [serve [--http]|fetch|install|doctor|reset]`."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from dataclasses import replace
from typing import get_args

from . import __version__
from .config import Settings
from .extract import Format

FINGERPRINT_URL = "https://tls.peet.ws/api/all"
BROWSER_CHECK_URL = "https://example.com/"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="super-fetch", description="MCP server that fetches web pages the way a normal browser user sees them."
    )
    parser.add_argument("--version", action="version", version=f"super-fetch {__version__}")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="Run the MCP server over stdio (default), or over HTTP with --http")
    serve.add_argument(
        "--http", action="store_true", help="serve streamable HTTP at /mcp instead of stdio (see SUPER_FETCH_HTTP_*)"
    )
    serve.add_argument("--host", help="address to listen on (default: SUPER_FETCH_HTTP_HOST or 127.0.0.1)")
    serve.add_argument("--port", type=int, help="port to listen on (default: SUPER_FETCH_HTTP_PORT or 8000)")

    fetch = sub.add_parser("fetch", help="Fetch one URL and print what the MCP tool would return")
    fetch.add_argument("url")
    fetch.add_argument("--format", choices=get_args(Format), default="markdown")
    fetch.add_argument("--render", choices=("auto", "never", "always"), default="auto")
    fetch.add_argument("--wait-for", default=None, metavar="CSS_SELECTOR")
    fetch.add_argument("--start-index", type=int, default=0)
    fetch.add_argument("--max-length", type=int, default=None)

    sub.add_parser("install", help="Download the browser used for bot challenges and JavaScript pages")
    sub.add_parser("doctor", help="Check that fetching works on this machine")
    sub.add_parser("reset", help="Forget the saved browser identity and cookies (e.g. after it gets flagged)")
    return parser


async def _fetch(args: argparse.Namespace, settings: Settings) -> int:
    from .http_tier import FetchError
    from .pipeline import Pipeline
    from .server import format_result
    from .urlcheck import BlockedURLError

    pipeline = Pipeline(settings)
    try:
        result = await pipeline.fetch(args.url, args.format, args.render, args.wait_for)
    except (BlockedURLError, FetchError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        await pipeline.close()
    print(format_result(result, args.start_index, args.max_length or settings.max_length))
    return 0


def _install() -> int:
    code = subprocess.call([sys.executable, "-m", "camoufox", "fetch"])
    if code == 0 and sys.platform.startswith("linux") and not shutil.which("Xvfb"):
        print(
            "\nNote: Xvfb is not installed, so the browser will run headless. Sites detect headless browsers more "
            "often; install Xvfb (e.g. `sudo apt install xvfb`) to run it on a virtual display instead."
        )
    return code


def _reset(settings: Settings) -> int:
    from .identity import Identity

    if settings.data_dir is None:
        print("Nothing to reset: SUPER_FETCH_DATA_DIR is empty, so nothing is saved.")
        return 0
    Identity(settings.data_dir).reset()
    print(f"Forgot the browser identity and cookies in {settings.data_dir}. Restart running servers to apply.")
    return 0


async def _doctor(settings: Settings) -> int:
    from .browser_tier import BrowserFetcher, browser_installed
    from .http_tier import HttpFetcher
    from .identity import CLEARANCE_COOKIES, Identity

    identity = Identity(settings.data_dir)
    print(f"super-fetch {__version__}, Python {sys.version.split()[0]}")
    print(f"Impersonating: {settings.impersonate}")
    print(f"Proxy: {settings.proxy or 'none'}")
    print(f"Private network access: {'allowed' if settings.allow_private else 'blocked'}")
    print(f"Data directory: {settings.data_dir or 'none (nothing saved)'}")
    cleared = sorted({c["domain"].lstrip(".") for c in identity.cookies if c["name"] in CLEARANCE_COOKIES})
    print(f"Saved bot-protection cookies for: {', '.join(cleared) or 'no sites yet'}")

    code = 0
    http = HttpFetcher(settings)
    try:
        raw = await http.fetch(FINGERPRINT_URL)
        info = json.loads(raw.text())
    except Exception as exc:  # noqa: BLE001 - report any failure to the user
        print(f"[FAIL] HTTP tier: could not reach {FINGERPRINT_URL}: {exc}")
        code = 1
    else:
        tls, http2 = info.get("tls", {}), info.get("http2", {})
        print("[ OK ] HTTP tier reachable. Fingerprint seen by the server:")
        print(f"       HTTP version: {info.get('http_version')}")
        print(f"       User-Agent:   {info.get('user_agent')}")
        print(f"       JA4:          {tls.get('ja4')}")
        print(f"       HTTP/2 hash:  {http2.get('akamai_fingerprint_hash')}")
    finally:
        await http.close()

    if not browser_installed():
        print("[FAIL] Browser tier: browser not installed; run `super-fetch install`.")
        return 1
    browser = BrowserFetcher(settings, identity)
    if browser.mode == "headless" and sys.platform.startswith("linux"):
        print("[WARN] Xvfb not found: the browser runs headless, which some sites detect.")
    started = time.monotonic()
    try:
        raw = await browser.fetch(BROWSER_CHECK_URL)
    except Exception as exc:  # noqa: BLE001 - report any failure to the user
        print(f"[FAIL] Browser tier ({browser.mode}): {exc}")
        return 1
    finally:
        await browser.close()
    print(f"[ OK ] Browser tier ({browser.mode}) loaded {raw.url} in {time.monotonic() - started:.1f}s.")
    print(f"       User-Agent:   {identity.headers.get('user-agent')}")
    return code


def main(argv: list[str] | None = None) -> None:
    # stdout carries the MCP protocol in serve mode, so logs must go to stderr.
    level = os.environ.get("SUPER_FETCH_LOG_LEVEL", "WARNING").upper()
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("super_fetch").setLevel(level)
    # trafilatura warns on every page it can't extract from; the fetch result already says so.
    logging.getLogger("trafilatura").setLevel(logging.ERROR)
    args = _parser().parse_args(argv)
    settings = Settings.from_env()

    match args.command:
        case "fetch":
            sys.exit(asyncio.run(_fetch(args, settings)))
        case "install":
            sys.exit(_install())
        case "doctor":
            sys.exit(asyncio.run(_doctor(settings)))
        case "reset":
            sys.exit(_reset(settings))
        case _ if getattr(args, "http", False):
            from .http_serve import check_settings, serve
            from .server import build_server

            host, port = args.host or settings.http_host, args.port or settings.http_port
            settings = replace(settings, http_host=host, http_port=port)
            try:
                check_settings(settings)
            except ValueError as exc:
                sys.exit(f"Error: {exc}")
            serve(build_server(settings), settings, level)
        case _:
            from .server import build_server

            build_server(settings).run("stdio")
