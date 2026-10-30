"""Google Safe Browsing checks, against a local stand-in for the Lookup API."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from super_fetch import urlcheck
from super_fetch.browser_tier import browser_installed
from super_fetch.config import Settings
from super_fetch.pipeline import Pipeline
from super_fetch.urlcheck import BlockedURLError, SafeBrowsing, UnsafeURLError

pytestmark = pytest.mark.anyio

KEY = "test-key-123"
PAGE = "<html><head><title>Fine</title></head><body><article><p>" + "Harmless text. " * 20 + "</p></article>"
JS_TO_EVIL = "<html><body><p>Moving on...</p><script>location.href = '/evil';</script></body></html>"

LOOKUPS: list[dict] = []
"""Every request the fake API got: headers, path and the URLs looked up."""
PAGES: list[str] = []
"""Paths of every page request."""


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        urls = [entry["url"] for entry in body["threatInfo"]["threatEntries"]]
        LOOKUPS.append({"key": self.headers.get("X-Goog-Api-Key"), "path": self.path, "urls": urls})
        if self.headers.get("X-Goog-Api-Key") != KEY:
            self._send(400, {"error": {"code": 400, "message": "API key not valid.", "status": "INVALID_ARGUMENT"}})
            return
        matches = [
            {"threatType": "SOCIAL_ENGINEERING", "platformType": "ANY_PLATFORM", "threat": {"url": url},
             "cacheDuration": "300s"}
            for url in urls
            if "/evil" in url
        ]  # fmt: skip
        self._send(200, {"matches": matches} if matches else {})

    def do_GET(self):
        PAGES.append(self.path)
        if self.path == "/redirect-to-evil":
            self.send_response(302)
            self.send_header("Location", "/evil")
            self.end_headers()
            return
        data = (JS_TO_EVIL if self.path == "/js-to-evil" else PAGE).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send(self, status, payload):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
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
def api(base_url, monkeypatch):
    monkeypatch.setattr(urlcheck, "SAFE_BROWSING_API", f"{base_url}/v4/threatMatches:find")
    LOOKUPS.clear()
    PAGES.clear()


@pytest.fixture
async def pipeline(api):
    pipeline = Pipeline(Settings(allow_private=True, data_dir=None, safe_browsing_key=KEY))
    yield pipeline
    await pipeline.close()


async def test_flagged_url_is_refused_with_googles_wording(api):
    safe_browsing = SafeBrowsing(KEY)
    try:
        with pytest.raises(UnsafeURLError, match="suspected phishing") as exc_info:
            await safe_browsing.check("https://example.com/evil")
    finally:
        await safe_browsing.close()
    assert "Advisory provided by Google" in str(exc_info.value)
    # The key travels in a header, never in the URL.
    assert LOOKUPS == [{"key": KEY, "path": "/v4/threatMatches:find", "urls": ["https://example.com/evil"]}]


async def test_verdicts_are_cached(api):
    safe_browsing = SafeBrowsing(KEY)
    try:
        for _ in range(2):
            await safe_browsing.check("https://example.com/fine")
            with pytest.raises(UnsafeURLError):
                await safe_browsing.check("https://example.com/evil")
    finally:
        await safe_browsing.close()
    assert len(LOOKUPS) == 2


async def test_failed_lookup_refuses_the_url(api):
    safe_browsing = SafeBrowsing("wrong-key")
    try:
        with pytest.raises(BlockedURLError, match="API key not valid") as exc_info:
            await safe_browsing.check("https://example.com/fine")
    finally:
        await safe_browsing.close()
    assert not isinstance(exc_info.value, UnsafeURLError)
    assert "wrong-key" not in str(exc_info.value)


async def test_unreachable_api_refuses_the_url(monkeypatch):
    monkeypatch.setattr(urlcheck, "SAFE_BROWSING_API", "http://127.0.0.1:9/v4/threatMatches:find")
    safe_browsing = SafeBrowsing(KEY)
    try:
        with pytest.raises(BlockedURLError, match="Could not check"):
            await safe_browsing.check("https://example.com/fine")
    finally:
        await safe_browsing.close()


async def test_pipeline_fetches_clean_pages(pipeline, base_url):
    result = await pipeline.fetch(f"{base_url}/page", render="never")
    assert "Harmless text" in result.content
    assert LOOKUPS[0]["urls"] == [f"{base_url}/page"]


async def test_redirect_to_flagged_url_is_not_followed(pipeline, base_url):
    with pytest.raises(UnsafeURLError):
        await pipeline.fetch(f"{base_url}/redirect-to-evil")
    assert PAGES == ["/redirect-to-evil"]


async def test_no_key_means_no_lookups(base_url, api):
    pipeline = Pipeline(Settings(allow_private=True, data_dir=None))
    try:
        await pipeline.fetch(f"{base_url}/evil", render="never")
    finally:
        await pipeline.close()
    assert LOOKUPS == []


@pytest.mark.skipif(not browser_installed(), reason="browser not installed")
async def test_browser_landing_on_flagged_url_is_refused(pipeline, base_url):
    with pytest.raises(UnsafeURLError):
        await pipeline.fetch(f"{base_url}/js-to-evil", render="always")
