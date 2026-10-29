"""End-to-end: MCP client → super-fetch server → local HTTP server."""

import asyncio
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from mcp import Client

from super_fetch.config import Settings
from super_fetch.pipeline import Pipeline
from super_fetch.server import build_server

from conftest import build_pdf

pytestmark = pytest.mark.anyio

LONG_PAGE = (
    "<html><head><title>Long page</title></head><body><article>"
    + "".join(f"<p>Paragraph {i}: some real article text here.</p>" for i in range(200))
    + "</article></body></html>"
)

ROUTES = {
    "/page": (200, "text/html; charset=utf-8", LONG_PAGE),
    "/json": (200, "application/json", '{"ok": true}'),
    "/challenge": (403, "text/html", "<html><head><title>Just a moment...</title></head><body></body></html>"),
    "/binary": (200, "application/octet-stream", "\x00\x01"),
    "/paper.pdf": (200, "application/pdf", build_pdf("Attention is all you need")),
}

SLEEPY = {"requests": 0, "active": 0, "peak": 0}
SLEEPY_LOCK = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/page")
            self.end_headers()
            return
        if self.path == "/slow":
            time.sleep(3)
        if self.path.startswith("/sleepy"):
            with SLEEPY_LOCK:
                SLEEPY["requests"] += 1
                SLEEPY["active"] += 1
                SLEEPY["peak"] = max(SLEEPY["peak"], SLEEPY["active"])
            time.sleep(0.3)
            with SLEEPY_LOCK:
                SLEEPY["active"] -= 1
            self.path = "/page"
        status, content_type, body = ROUTES.get(self.path, (404, "text/plain", "not found"))
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def base_url():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


@pytest.fixture
async def client():
    server = build_server(Settings(allow_private=True, max_length=2000))
    async with Client(server) as c:
        yield c


async def call_fetch(client, **args) -> tuple[bool, str]:
    result = await client.call_tool("fetch", args)
    return bool(result.is_error), result.content[0].text


async def test_tool_is_listed(client):
    tools = (await client.list_tools()).tools
    assert [t.name for t in tools] == ["fetch"]


async def test_fetch_and_paginate(client, base_url):
    is_error, first = await call_fetch(client, url=f"{base_url}/page")
    assert not is_error
    assert "Title: Long page" in first
    assert "Paragraph 0:" in first
    assert "start_index=" in first

    next_index = int(first.rsplit("start_index=", 1)[1].split(" ")[0])
    _, second = await call_fetch(client, url=f"{base_url}/page", start_index=next_index)
    assert "Paragraph 0:" not in second
    assert f"showing {next_index}-" in second


async def test_redirect_reports_final_url(client, base_url):
    _, text = await call_fetch(client, url=f"{base_url}/redirect")
    assert f"URL: {base_url}/page" in text


async def test_json_returned_verbatim(client, base_url):
    _, text = await call_fetch(client, url=f"{base_url}/json")
    assert text.endswith('{"ok": true}')


async def test_challenge_is_reported(client, base_url):
    is_error, text = await call_fetch(client, url=f"{base_url}/challenge", render="never")
    assert not is_error
    assert "Warning: Blocked by a cloudflare bot challenge" in text


async def test_binary_is_an_error(client, base_url):
    is_error, text = await call_fetch(client, url=f"{base_url}/binary")
    assert is_error
    assert "Unsupported content type" in text


async def test_private_address_blocked_by_default(base_url):
    async with Client(build_server(Settings())) as c:
        is_error, text = await call_fetch(c, url=f"{base_url}/page")
    assert is_error
    assert "non-public" in text


async def test_pdf_is_converted_to_text(client, base_url):
    is_error, text = await call_fetch(client, url=f"{base_url}/paper.pdf")
    assert not is_error
    assert "Content-Type: application/pdf" in text
    assert text.endswith("[Page 1]\n\nAttention is all you need")


async def test_slow_server_times_out(base_url):
    async with Client(build_server(Settings(allow_private=True, timeout=1))) as c:
        started = time.monotonic()
        is_error, text = await call_fetch(c, url=f"{base_url}/slow", render="never")
    assert is_error
    assert "timed out after 1s" in text
    assert time.monotonic() - started < 2.5


async def test_parallel_fetches_are_limited_per_host_and_deduplicated(base_url):
    pipeline = Pipeline(Settings(allow_private=True))
    SLEEPY.update(requests=0, peak=0)
    try:
        urls = [f"{base_url}/sleepy?{i}" for i in range(4)] + [f"{base_url}/sleepy?0"] * 2
        results = await asyncio.gather(*(pipeline.fetch(url, render="never") for url in urls))
    finally:
        await pipeline.close()
    assert all("Paragraph 0:" in r.content for r in results)
    assert SLEEPY["peak"] == 2
    assert SLEEPY["requests"] == 4
