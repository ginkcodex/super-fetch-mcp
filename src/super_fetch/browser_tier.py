"""Tier 1: a real, fingerprint-consistent Firefox (Camoufox) that runs the page's JavaScript."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import sys
import time
from urllib.parse import unquote, urlsplit

from playwright.async_api import Browser, BrowserContext, Page, Playwright, Request, Response, Route, async_playwright
from playwright.async_api import Error as PlaywrightError

from .config import Settings
from .detect import detect_challenge, detect_hard_block
from .http_tier import FetchError, RawResponse
from .identity import Identity
from .urlcheck import BlockedURLError, check_url

log = logging.getLogger(__name__)

_POLL_INTERVAL = 0.5
_SETTLE_MAX = 8.0
"""Seconds to wait for content to stop changing once the page (and any challenge) has loaded."""
_STABLE_GROWTH = 0.02
"""Visible text that grows less than this fraction between polls counts as settled."""
_READ_MARGIN = 3.0
"""Seconds of the fetch timeout kept for serializing the page after waiting."""
_STATE_JS = "[document.readyState, document.body ? document.body.innerText.length : 0]"
_LONG_LIVED_TYPES = {"websocket", "eventsource", "media"}
"""Request types that can stay open indefinitely, so they don't count against "network quiet"."""
_CLICK_AFTER = 3.0
"""Seconds a Cloudflare challenge may try to pass on its own before its checkbox is clicked."""
_CLICK_INTERVAL = 5.0
_MAX_CLICKS = 3
_TURNSTILE_ORIGIN = "https://challenges.cloudflare.com/"


class _NetworkTracker:
    """Counts a page's in-flight requests and when the last one finished."""

    def __init__(self, page: Page):
        self._inflight: set[Request] = set()
        self._last_activity = time.monotonic()
        page.on("request", self._started)
        page.on("requestfinished", self._finished)
        page.on("requestfailed", self._finished)

    @property
    def inflight(self) -> int:
        return len(self._inflight)

    def quiet_for(self, now: float) -> float:
        return 0.0 if self._inflight else now - self._last_activity

    def _started(self, request: Request) -> None:
        if request.resource_type not in _LONG_LIVED_TYPES:
            self._inflight.add(request)
            self._last_activity = time.monotonic()

    def _finished(self, request: Request) -> None:
        if request in self._inflight:
            self._inflight.discard(request)
            self._last_activity = time.monotonic()


class BrowserNotInstalledError(FetchError):
    pass


def browser_installed() -> bool:
    from camoufox.pkgman import camoufox_path

    try:
        camoufox_path(download_if_missing=False)
    except Exception:  # noqa: BLE001 - any failure means "not usable"
        return False
    return True


def resolve_mode(settings: Settings) -> str:
    """The browser mode actually used: auto picks Xvfb on Linux when it is installed."""
    if settings.browser_mode != "auto":
        return settings.browser_mode
    return "virtual" if sys.platform.startswith("linux") and shutil.which("Xvfb") else "headless"


def _playwright_proxy(proxy_url: str | None) -> dict[str, str] | None:
    if not proxy_url:
        return None
    parts = urlsplit(proxy_url)
    proxy = {"server": f"{parts.scheme}://{parts.hostname}:{parts.port}" if parts.port else proxy_url}
    if parts.username:
        proxy["username"] = unquote(parts.username)
        proxy["password"] = unquote(parts.password or "")
    return proxy


class BrowserFetcher:
    """One lazily started browser shared by all fetches, closed again after a period of no use."""

    def __init__(self, settings: Settings, identity: Identity | None = None):
        self._settings = settings
        self.identity = identity or Identity(None)
        self.mode = resolve_mode(settings)
        self._lock = asyncio.Lock()
        self._slots = asyncio.Semaphore(settings.browser_concurrency)
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._idle_task: asyncio.Task[None] | None = None
        self._active = 0
        self._last_used = time.monotonic()
        self._host_allowed: dict[str, bool] = {}

    async def close(self) -> None:
        async with self._lock:
            await self._shutdown()

    async def fetch(self, url: str, wait_for: str | None = None, timeout: float | None = None) -> RawResponse:
        timeout = timeout or self._settings.timeout
        await check_url(url, allow_private=self._settings.allow_private)
        async with self._slots:
            self._active += 1
            try:
                context = await self._ensure_context()
                page = await context.new_page()
                try:
                    async with asyncio.timeout(timeout):
                        # Waiting stops a little early so a stuck challenge or missing selector
                        # still returns the page (and why) instead of a bare timeout.
                        deadline = time.monotonic() + timeout - min(_READ_MARGIN, timeout / 4)
                        return await self._load(page, url, wait_for, deadline)
                except TimeoutError as exc:
                    raise FetchError(f"Browser timed out after {timeout:g}s loading {url}") from exc
                finally:
                    await page.close()
                    # Share what the page earned (e.g. challenge clearance) with the HTTP tier and later runs.
                    with contextlib.suppress(PlaywrightError):
                        self.identity.update_cookies(await context.cookies())
            finally:
                self._active -= 1
                self._last_used = time.monotonic()

    async def _ensure_context(self) -> BrowserContext:
        async with self._lock:
            if self._context is not None:
                if self._browser is not None and self._browser.is_connected():
                    return self._context
                log.warning("The browser exited unexpectedly; starting a new one")
                await self._shutdown()
            if not browser_installed():
                raise BrowserNotInstalledError("The browser is not installed; run `super-fetch install`.")

            from camoufox.async_api import AsyncNewBrowser, AsyncNewContext

            headless: bool | str = {"headless": True, "virtual": "virtual", "headful": False}[self.mode]
            preset = self.identity.preset(self._settings.browser_os)
            try:
                self._playwright = await async_playwright().start()
                # humanize: the cursor moves like a person's when a challenge checkbox is clicked.
                self._browser = await AsyncNewBrowser(
                    self._playwright, headless=headless, humanize=True, fingerprint_preset=preset
                )
                # One context for the server's lifetime: one consistent identity whose
                # cookies (consent, challenge clearance) carry over between fetches.
                self._context = await AsyncNewContext(
                    self._browser, preset=preset, proxy=_playwright_proxy(self._settings.proxy), accept_downloads=False
                )
                if cookies := self.identity.cookies:
                    try:
                        await self._context.add_cookies(cookies)
                    except PlaywrightError as exc:
                        log.warning("Could not restore saved cookies: %s", exc.message.splitlines()[0])
                if not self._settings.allow_private:
                    await self._context.route("**/*", self._guard_request)
            except Exception as exc:
                await self._shutdown()
                raise FetchError(f"Could not start the browser ({self.mode}): {exc}") from exc

            self._idle_task = asyncio.create_task(self._close_when_idle())
            return self._context

    async def _shutdown(self) -> None:
        if self._idle_task is not None and self._idle_task is not asyncio.current_task():
            self._idle_task.cancel()
        self._idle_task = None
        for closer in (self._context and self._context.close, self._browser and self._browser.close):
            if closer:
                with contextlib.suppress(PlaywrightError):
                    await closer()
        if self._playwright is not None:
            await self._playwright.stop()
        self._playwright = self._browser = self._context = None
        self._host_allowed.clear()

    async def _close_when_idle(self) -> None:
        timeout = self._settings.browser_idle_timeout
        while True:
            await asyncio.sleep(min(30.0, timeout))
            async with self._lock:
                if self._active == 0 and time.monotonic() - self._last_used >= timeout:
                    log.info("Closing idle browser")
                    await self._shutdown()
                    return

    async def _guard_request(self, route: Route, request: Request) -> None:
        """Keep pages from reaching private network addresses through the browser.

        Playwright does not route redirect hops, so the final page URL is checked again in `_load`.
        """
        host = urlsplit(request.url).hostname
        if host is None or request.url.startswith(("data:", "blob:")):
            await route.continue_()
            return
        allowed = self._host_allowed.get(host)
        if allowed is None:
            try:
                await check_url(request.url, allow_private=False)
                allowed = True
            except BlockedURLError:
                allowed = False
            self._host_allowed[host] = allowed
        if allowed:
            await route.continue_()
        else:
            await route.abort("blockedbyclient")

    async def _load(self, page: Page, url: str, wait_for: str | None, deadline: float) -> RawResponse:
        document: list[Response] = []
        network = _NetworkTracker(page)

        def on_response(response: Response) -> None:
            if response.request.is_navigation_request() and response.frame == page.main_frame:
                document.append(response)

        page.on("response", on_response)
        try:
            await page.goto(url, wait_until="domcontentloaded")
        except PlaywrightError as exc:
            raise FetchError(f"Browser could not load {url}: {exc.message.splitlines()[0]}") from exc

        first = document[-1] if document else None
        if first is not None:
            # What the browser really sends, for the HTTP tier to match when reusing its cookies.
            self.identity.record_headers(await first.request.all_headers())
            if not _is_html_response(first):
                # JSON, plain text, PDF...: return the bytes rather than Firefox's viewer page.
                return await _raw_from_response(page.url, first, self._settings.max_bytes)

        warnings, hard_block = await self._wait_until_ready(page, document, network, wait_for, deadline)
        await check_url(page.url, allow_private=self._settings.allow_private)

        html = await _page_html(page)
        last = document[-1] if document else None
        headers = (await last.all_headers()) if last else {}
        body = html.encode()
        limit = self._settings.max_bytes
        return RawResponse(
            url=page.url,
            status=last.status if last else 200,
            # The DOM is serialized as UTF-8 HTML whatever the original encoding was.
            headers={**headers, "content-type": "text/html; charset=utf-8"},
            body=body[:limit],
            truncated=len(body) > limit,
            warnings=warnings,
            hard_block=hard_block,
        )

    async def _wait_until_ready(
        self, page: Page, document: list[Response], network: _NetworkTracker, wait_for: str | None, deadline: float
    ) -> tuple[list[str], str | None]:
        """Wait out bot challenges, then for the page to finish loading and its text to stop changing.

        Ready means: no challenge, document complete, network quiet, and visible text unchanged
        (within a small margin) over consecutive polls. Pages that never go quiet (polling,
        analytics) are read once the settle time runs out. Returns warnings and the vendor of a
        block page that waiting can't get past, if the page ended on one.
        """
        started = time.monotonic()
        settle_deadline: float | None = None
        previous_length = -1
        stable_polls = 0
        challenge_since: float | None = None
        clicks, last_click = 0, 0.0
        while time.monotonic() < deadline:
            await asyncio.sleep(_POLL_INTERVAL)
            try:
                html = await page.content()
                ready_state, length = await page.evaluate(_STATE_JS)
            except PlaywrightError:
                stable_polls = 0
                continue  # navigating (e.g. a challenge redirecting to the real page)

            status = document[-1].status if document else 200
            headers = (await document[-1].all_headers()) if document else {}
            now = time.monotonic()
            challenge = detect_challenge(status, headers, html)
            log.debug(
                "%.1fs %s state=%s text=%d inflight=%d challenge=%s",
                now - started, page.url, ready_state, length, network.inflight, challenge,
            )  # fmt: skip
            if challenge:
                if hard_block := detect_hard_block(html, [frame.url for frame in page.frames]):
                    return [], hard_block
                settle_deadline = None
                stable_polls = 0
                challenge_since = challenge_since or now
                if (
                    challenge == "cloudflare"
                    and clicks < _MAX_CLICKS
                    and now - challenge_since >= _CLICK_AFTER
                    and now - last_click >= _CLICK_INTERVAL
                    and await _click_turnstile(page)
                ):
                    clicks, last_click = clicks + 1, time.monotonic()
                    log.debug("Clicked the Turnstile checkbox on %s (attempt %d)", page.url, clicks)
                continue

            challenge_since = None
            settle_deadline = settle_deadline or now + _SETTLE_MAX
            unchanged = length > 0 and abs(length - previous_length) <= previous_length * _STABLE_GROWTH
            stable_polls = stable_polls + 1 if unchanged else 0
            previous_length = length
            if (stable_polls >= 2 and ready_state == "complete" and network.quiet_for(now) >= _POLL_INTERVAL) or (
                now >= settle_deadline
            ):
                break

        if wait_for:
            timeout_ms = max(0.0, deadline - time.monotonic()) * 1000
            try:
                await page.wait_for_selector(wait_for, state="attached", timeout=timeout_ms)
            except PlaywrightError as exc:
                return [f"wait_for selector {wait_for!r} not found: {exc.message.splitlines()[0]}"], None
        return [], None


async def _click_turnstile(page: Page) -> bool:
    """Click the checkbox of a Cloudflare Turnstile widget on the page, if one is showing."""
    for frame in page.frames:
        if not frame.url.startswith(_TURNSTILE_ORIGIN):
            continue
        try:
            box = await (await frame.frame_element()).bounding_box()
        except PlaywrightError:
            continue
        if box and box["width"] > 0 and box["height"] > 0:
            # The widget's own frame is cross-origin; the checkbox sits at its left edge, vertically centred.
            await page.mouse.click(box["x"] + min(30.0, box["width"] / 2), box["y"] + box["height"] / 2)
            return True
    return False


def _is_html_response(response: Response) -> bool:
    content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
    return content_type in {"", "text/html", "application/xhtml+xml"}


async def _raw_from_response(url: str, response: Response, limit: int) -> RawResponse:
    try:
        body = await response.body()
    except PlaywrightError as exc:
        raise FetchError(f"Browser could not read the response body of {url}: {exc.message}") from exc
    headers = await response.all_headers()
    return RawResponse(url=url, status=response.status, headers=headers, body=body[:limit], truncated=len(body) > limit)


async def _page_html(page: Page) -> str:
    for _ in range(3):
        try:
            return await page.content()
        except PlaywrightError:
            await asyncio.sleep(_POLL_INTERVAL)
    return await page.content()
