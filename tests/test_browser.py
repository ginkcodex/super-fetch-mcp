"""Browser tier against local pages. Skipped when the browser is not installed (`super-fetch install`)."""

import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest

from super_fetch.browser_tier import browser_installed
from super_fetch.config import Settings
from super_fetch.pipeline import Pipeline

from conftest import build_pdf

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(not browser_installed(), reason="browser not installed"),
]

ARTICLE_TEXT = "Rendered by JavaScript. " * 20

SPA = f"""<html><head><title>App</title></head><body><div id="root"></div>
<script>setTimeout(() => {{
  document.getElementById('root').innerHTML = '<article><h1>Hello</h1><p>{ARTICLE_TEXT}</p></article>';
}}, 300);</script></body></html>"""

# Looks like a Cloudflare challenge, then "passes" by navigating to the real page.
PASSING_CHALLENGE = """<html><head><title>Just a moment...</title></head><body>
<script src="/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page/v1"></script>
<script>setTimeout(() => { location.href = '/spa'; }, 1000);</script></body></html>"""

STUCK_CHALLENGE = """<html><head><title>Just a moment...</title></head><body>
<script>window._cf_chl_opt = {};</script></body></html>"""

# Sets a clearance cookie and reloads, like a challenge being solved. The server only accepts the
# cookie from a Firefox User-Agent, as real clearance only holds for the client that earned it.
SETS_CLEARANCE = """<html><head><title>Just a moment...</title></head><body>
<script>window._cf_chl_opt = {};
setTimeout(() => { document.cookie = 'cf_clearance=ok; max-age=3600; path=/'; location.reload(); }, 500);
</script></body></html>"""

GATED_ARTICLE = (
    "<html><head><title>Gated</title></head><body><article><p>" + "Behind the gate. " * 20 + "</p></article>"
)

DATADOME_BAN = """<html><head><title>example.com</title></head><body>
<script>var dd={'rt':'c','t':'bv','host':'geo.captcha-delivery.com'}</script></body></html>"""

# Cloudflare's test sitekey that always shows the interactive checkbox.
TURNSTILE = """<html><head><title>Just a moment...</title>
<script src="https://challenges.cloudflare.com/turnstile/v0/api.js" async defer></script></head>
<body><div class="cf-turnstile" data-sitekey="3x00000000000000000000FF" data-callback="passed"></div>
<script>function passed() { location.href = '/spa'; }</script></body></html>"""

ROUTES = {
    "/spa": (200, SPA),
    "/challenge-passes": (403, PASSING_CHALLENGE),
    "/challenge-stuck": (403, STUCK_CHALLENGE),
    "/datadome-ban": (403, DATADOME_BAN),
    "/turnstile": (403, TURNSTILE),
    "/paper.pdf": (200, build_pdf("Rendered PDF text")),
}

REQUESTS: list[tuple[str, str]] = []
"""(path, User-Agent) of every request the server got."""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlsplit(self.path).path
        user_agent = self.headers.get("User-Agent", "")
        REQUESTS.append((path, user_agent))
        if path in ("/gate", "/gate-app"):
            cleared = "cf_clearance=ok" in self.headers.get("Cookie", "") and "Firefox/" in user_agent
            status, body = (200, GATED_ARTICLE if path == "/gate" else SPA) if cleared else (403, SETS_CLEARANCE)
        else:
            status, body = ROUTES.get(path, (404, "not found"))
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/pdf" if path.endswith(".pdf") else "text/html; charset=utf-8")
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
async def pipeline():
    p = Pipeline(Settings(allow_private=True, timeout=10))
    yield p
    await p.close()


async def test_spa_shell_escalates_to_browser(pipeline, base_url):
    result = await pipeline.fetch(f"{base_url}/spa")
    assert result.tier.startswith("browser")
    assert "Rendered by JavaScript." in result.content
    assert not result.warnings


async def test_render_never_keeps_the_shell(pipeline, base_url):
    result = await pipeline.fetch(f"{base_url}/spa", render="never")
    assert result.tier == "http"
    assert result.needs_js
    assert "Rendered by JavaScript." not in result.content


async def test_waits_for_challenge_to_pass(pipeline, base_url):
    result = await pipeline.fetch(f"{base_url}/challenge-passes")
    assert result.blocked_by is None
    assert result.url == f"{base_url}/spa"
    assert result.status == 200
    assert "Rendered by JavaScript." in result.content


async def test_stuck_challenge_is_reported_not_raised(pipeline, base_url):
    result = await pipeline.fetch(f"{base_url}/challenge-stuck")
    assert result.tier.startswith("browser")
    assert result.blocked_by == "cloudflare"


async def test_wait_for_missing_selector_warns(pipeline, base_url):
    result = await pipeline.fetch(f"{base_url}/spa", wait_for="#never-there")
    assert "Rendered by JavaScript." in result.content
    assert any("#never-there" in w for w in result.warnings)


async def test_http_reuses_clearance_the_browser_earned(pipeline, base_url):
    first = await pipeline.fetch(f"{base_url}/gate?page=1")
    assert first.tier.startswith("browser")
    assert "Behind the gate." in first.content

    second = await pipeline.fetch(f"{base_url}/gate?page=2")
    assert second.tier == "http (browser cookies)"
    assert "Behind the gate." in second.content


async def test_app_behind_reused_clearance_still_escalates(pipeline, base_url):
    await pipeline.fetch(f"{base_url}/gate-app?page=1")
    result = await pipeline.fetch(f"{base_url}/gate-app?page=2")
    assert result.tier.startswith("browser")
    assert "Rendered by JavaScript." in result.content


async def test_recovers_after_browser_crash(pipeline, base_url):
    await pipeline.fetch(f"{base_url}/spa", render="always")
    await pipeline._browser._browser.close()  # stands in for the browser process dying
    result = await pipeline.fetch(f"{base_url}/spa?again", render="always")
    assert "Rendered by JavaScript." in result.content


async def test_pdf_in_browser_returns_text(pipeline, base_url):
    result = await pipeline.fetch(f"{base_url}/paper.pdf", render="always")
    assert result.content_type == "application/pdf"
    assert "Rendered PDF text" in result.content


async def test_host_that_needed_the_browser_skips_http_next_time(pipeline, base_url):
    await pipeline.fetch(f"{base_url}/spa?page=1")
    REQUESTS.clear()
    result = await pipeline.fetch(f"{base_url}/spa?page=2")
    assert result.tier.startswith("browser")
    assert all("Firefox/" in user_agent for _, user_agent in REQUESTS)  # no Chrome-impersonating HTTP request


async def test_hard_block_returns_without_waiting(pipeline, base_url):
    started = time.monotonic()
    result = await pipeline.fetch(f"{base_url}/datadome-ban", render="always")
    assert time.monotonic() - started < 5  # a challenge would be waited on for ~7s
    assert result.blocked_by == "datadome"
    assert any("Access denied by datadome" in w for w in result.warnings)


@pytest.mark.skipif(
    not os.environ.get("SUPER_FETCH_NETWORK_TESTS"), reason="needs internet; set SUPER_FETCH_NETWORK_TESTS=1"
)
async def test_clicks_turnstile_checkbox(base_url):
    pipeline = Pipeline(Settings(allow_private=True, timeout=20))
    try:
        result = await pipeline.fetch(f"{base_url.replace('127.0.0.1', 'localhost')}/turnstile", render="always")
    finally:
        await pipeline.close()
    assert result.blocked_by is None
    assert "Rendered by JavaScript." in result.content
