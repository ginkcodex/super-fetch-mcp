"""`serve --http`: MCP client → super-fetch over streamable HTTP, with and without a token."""

import socket
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx2
import pytest
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

from super_fetch import cli
from super_fetch.config import Settings
from super_fetch.http_serve import build_app, check_settings
from super_fetch.server import build_server

pytestmark = pytest.mark.anyio

TOKEN = "t" * 40
PAGE = b"<html><head><title>Served page</title></head><body><article><p>Hello over HTTP.</p></article></body></html>"
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}},
}
MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


class PageHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(PAGE)))
        self.end_headers()
        self.wfile.write(PAGE)

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def page_url():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), PageHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}/"
    httpd.shutdown()


@contextmanager
def running(settings: Settings):
    """Serve the app on a free port in a background thread; yield the MCP endpoint URL."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    app = build_app(build_server(settings), settings)
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline, "server did not start"
        time.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{sock.getsockname()[1]}/mcp"
    finally:
        server.should_exit = True
        thread.join(10)


@pytest.fixture(scope="module")
def token_url():
    settings = Settings(allow_private=True, http_token=TOKEN, http_allowed_hosts=("fetch.example.com",), data_dir=None)
    with running(settings) as url:
        yield url


async def test_requests_without_the_right_token_are_rejected(token_url):
    async with httpx2.AsyncClient() as http:
        for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": TOKEN}):
            response = await http.post(token_url, json=INITIALIZE, headers=MCP_HEADERS | headers)
            assert response.status_code == 401, headers
            assert response.headers["www-authenticate"] == "Bearer"


async def test_agent_with_the_token_can_fetch(token_url, page_url):
    http = create_mcp_http_client(headers={"Authorization": f"Bearer {TOKEN}"})
    async with http, Client(streamable_http_client(token_url, http_client=http)) as client:
        assert [t.name for t in (await client.list_tools()).tools] == ["fetch"]
        result = await client.call_tool("fetch", {"url": page_url, "render": "never"})
    text = result.content[0].text
    assert "Title: Served page" in text
    assert "Hello over HTTP." in text


async def test_only_allowed_host_names_are_accepted(token_url):
    # Behind a reverse proxy the Host header is the public domain; other names (a rebound domain) fail.
    headers = MCP_HEADERS | {"Authorization": f"Bearer {TOKEN}"}
    async with httpx2.AsyncClient() as http:
        domain = await http.post(token_url, json=INITIALIZE, headers=headers | {"Host": "fetch.example.com"})
        other = await http.post(token_url, json=INITIALIZE, headers=headers | {"Host": "attacker.example"})
        origin = await http.post(token_url, json=INITIALIZE, headers=headers | {"Origin": "https://attacker.example"})
    assert domain.status_code == 200
    assert other.status_code == 421
    assert origin.status_code == 403


async def test_loopback_server_without_token_rejects_other_hosts():
    with running(Settings(data_dir=None)) as url:
        async with httpx2.AsyncClient() as http:
            ok = await http.post(url, json=INITIALIZE, headers=MCP_HEADERS)
            rebound = await http.post(url, json=INITIALIZE, headers=MCP_HEADERS | {"Host": "attacker.example"})
    assert ok.status_code == 200
    assert rebound.status_code == 421


def test_public_address_needs_a_long_token():
    check_settings(Settings(http_host="127.0.0.1"))
    check_settings(Settings(http_host="0.0.0.0", http_token=TOKEN))
    with pytest.raises(ValueError, match="without SUPER_FETCH_HTTP_TOKEN"):
        check_settings(Settings(http_host="0.0.0.0"))
    with pytest.raises(ValueError, match="at least 32 characters"):
        check_settings(Settings(http_host="0.0.0.0", http_token="short"))


def test_cli_refuses_public_address_without_token(monkeypatch):
    monkeypatch.delenv("SUPER_FETCH_HTTP_TOKEN", raising=False)
    with pytest.raises(SystemExit, match="without SUPER_FETCH_HTTP_TOKEN"):
        cli.main(["serve", "--http", "--host", "0.0.0.0"])


def test_token_is_kept_out_of_settings_repr():
    assert TOKEN not in repr(Settings(http_token=TOKEN))
