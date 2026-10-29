"""Fetch a URL with the cheapest tier that works and turn it into readable content."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import OrderedDict
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urlsplit

from .browser_tier import BrowserFetcher, BrowserNotInstalledError
from .config import Settings
from .detect import detect_challenge, detect_hard_block, is_spa_shell
from .extract import DocumentError, Format, html_to_content, pdf_to_content
from .http_tier import FetchError, HttpFetcher, RawResponse
from .identity import Identity

Render = Literal["auto", "never", "always"]

_HTML_TYPES = {"text/html", "application/xhtml+xml"}
_TEXT_TYPES = {"application/json", "application/xml", "application/javascript", "application/x-javascript"}
_CACHE_SIZE = 32
_HOST_CONCURRENCY = 2
_BROWSER_HOST_TTL = 1800.0
"""Seconds a host whose pages needed the browser goes straight to it, skipping the HTTP attempt."""


@dataclass
class FetchResult:
    url: str
    status: int
    content_type: str
    content: str
    tier: str
    title: str | None = None
    blocked_by: str | None = None
    needs_js: bool = False
    """The HTTP tier got a JavaScript app shell rather than the content."""
    warnings: list[str] = field(default_factory=list)


@dataclass
class _HostSlot:
    semaphore: asyncio.Semaphore
    users: int = 0


def _is_html(raw: RawResponse) -> bool:
    if raw.content_type in _HTML_TYPES:
        return True
    return not raw.content_type and raw.body.lstrip()[:1] == b"<"


def _is_text(content_type: str) -> bool:
    return content_type.startswith("text/") or content_type in _TEXT_TYPES or content_type.endswith(("+json", "+xml"))


class Pipeline:
    def __init__(self, settings: Settings):
        self._settings = settings
        self.identity = Identity(settings.data_dir)
        self._http = HttpFetcher(settings, self.identity)
        self._browser = BrowserFetcher(settings, self.identity)
        self._cache: OrderedDict[tuple, tuple[float, FetchResult]] = OrderedDict()
        self._browser_hosts: dict[str, float] = {}
        """Hosts the HTTP tier recently failed on, mapped to when to give it another try."""
        self._host_slots: dict[str, _HostSlot] = {}

    async def close(self) -> None:
        await self._http.close()
        await self._browser.close()

    async def fetch(
        self, url: str, fmt: Format = "markdown", render: Render = "auto", wait_for: str | None = None
    ) -> FetchResult:
        key = (url, fmt, render, wait_for)
        if cached := self._cached(key):
            return cached

        async with self._host_slot(urlsplit(url).hostname or ""):
            # A parallel call for the same page may have fetched it while this one waited.
            if cached := self._cached(key):
                return cached
            result = await self._fetch_uncached(url, fmt, render, wait_for)

        # Cache so follow-up calls with start_index page through identical content.
        # Blocked results are not cached, so a retry gets a fresh attempt.
        if result.blocked_by is None:
            self._cache[key] = (time.monotonic() + self._settings.cache_ttl, result)
            self._cache.move_to_end(key)
            while len(self._cache) > _CACHE_SIZE:
                self._cache.popitem(last=False)
        return result

    def _cached(self, key: tuple) -> FetchResult | None:
        cached = self._cache.get(key)
        return cached[1] if cached and cached[0] > time.monotonic() else None

    @contextlib.asynccontextmanager
    async def _host_slot(self, host: str) -> AsyncIterator[None]:
        """Limit simultaneous fetches per host: bursts are what get clients rate-limited or flagged."""
        slot = self._host_slots.setdefault(host, _HostSlot(asyncio.Semaphore(_HOST_CONCURRENCY)))
        slot.users += 1
        try:
            async with slot.semaphore:
                yield
        finally:
            slot.users -= 1
            if slot.users == 0:
                del self._host_slots[host]

    async def _fetch_uncached(self, url: str, fmt: Format, render: Render, wait_for: str | None) -> FetchResult:
        host = urlsplit(url).hostname or ""
        # wait_for only means something once JavaScript runs.
        if render == "always" or (render == "auto" and (wait_for or self._prefers_browser(host))):
            return await self._fetch_browser(url, fmt, wait_for)

        # One time budget for the whole call: the HTTP attempt may use at most half of it when the
        # browser could still take over, and the browser gets whatever is left (at least half).
        timeout = self._settings.timeout
        deadline = time.monotonic() + timeout
        reused_clearance = self.identity.has_clearance(url)
        try:
            raw = await self._http.fetch(url, timeout=timeout if render == "never" else timeout / 2)
        except FetchError as exc:
            # Some protections drop or reset non-browser connections outright.
            if render == "never":
                raise
            result = await self._fetch_browser(url, fmt, wait_for, http_error=exc, deadline=deadline)
            self._learn(host, url, result, reused_clearance)
            return result

        tier = "http (browser cookies)" if reused_clearance else "http"
        result = await asyncio.to_thread(self._process, raw, fmt, tier)
        if render == "never" or not (result.blocked_by or result.needs_js):
            return result
        try:
            browser_result = await self._fetch_browser(url, fmt, wait_for, deadline=deadline)
        except FetchError as exc:
            result.warnings.append(f"Retrying in the browser failed: {exc}")
            return result
        self._learn(host, url, browser_result, reused_clearance)
        return browser_result

    def _prefers_browser(self, host: str) -> bool:
        retry_http_at = self._browser_hosts.get(host)
        if retry_http_at is None:
            return False
        if retry_http_at <= time.monotonic():
            del self._browser_hosts[host]
            return False
        return True

    def _learn(self, host: str, url: str, result: FetchResult, reused_clearance: bool) -> None:
        """After the browser got a page that HTTP couldn't, decide how to fetch this host next time.

        If the browser earned a clearance cookie the HTTP tier hasn't tried yet, HTTP gets another
        go with it; otherwise skip straight to the browser for a while.
        """
        if result.blocked_by is None and (reused_clearance or not self.identity.has_clearance(url)):
            self._browser_hosts[host] = time.monotonic() + _BROWSER_HOST_TTL

    async def _fetch_browser(
        self,
        url: str,
        fmt: Format,
        wait_for: str | None,
        http_error: FetchError | None = None,
        deadline: float | None = None,
    ) -> FetchResult:
        timeout = None
        if deadline is not None:
            timeout = max(deadline - time.monotonic(), self._settings.timeout / 2)
        try:
            raw = await self._browser.fetch(url, wait_for, timeout)
        except FetchError as exc:
            if http_error is None:
                raise
            if isinstance(exc, BrowserNotInstalledError):
                raise http_error from None
            raise FetchError(f"{http_error} Retrying in the browser also failed: {exc}") from exc
        return await asyncio.to_thread(self._process, raw, fmt, f"browser ({self._browser.mode})")

    def _process(self, raw: RawResponse, fmt: Format, tier: str) -> FetchResult:
        result = FetchResult(
            url=raw.url, status=raw.status, content_type=raw.content_type or "unknown", content="", tier=tier
        )
        result.warnings.extend(raw.warnings)
        if raw.truncated:
            result.warnings.append(f"Response was larger than {self._settings.max_bytes} bytes and was cut off.")

        if _is_html(raw):
            html = raw.text()
            result.blocked_by = detect_challenge(raw.status, raw.headers, html)
            hard_block = result.blocked_by and (raw.hard_block or detect_hard_block(html))
            if hard_block:
                result.blocked_by = hard_block
                result.warnings.append(
                    f"Access denied by {hard_block} bot protection (HTTP {raw.status}): the site refused this "
                    "client or network outright, so retrying won't help. The content below is the block page."
                )
            elif result.blocked_by:
                result.warnings.append(
                    f"Blocked by a {result.blocked_by} bot challenge (HTTP {raw.status}); "
                    "the content below is the challenge page, not the real page."
                )
            elif tier.startswith("http") and is_spa_shell(html):
                result.needs_js = True
                result.warnings.append(
                    "This looks like a JavaScript app shell, so content is probably missing. "
                    "Use render='always' to load it in the browser."
                )
            result.title, result.content = html_to_content(html, raw.url, fmt)
        elif raw.content_type == "application/pdf" or raw.body.startswith(b"%PDF-"):
            if raw.truncated:
                raise FetchError(
                    f"The PDF at {raw.url} is larger than {self._settings.max_bytes} bytes (SUPER_FETCH_MAX_BYTES)."
                )
            try:
                result.title, result.content, pdf_warnings = pdf_to_content(raw.body)
            except DocumentError as exc:
                raise FetchError(f"{exc} ({raw.url})") from exc
            result.content_type = "application/pdf"
            result.warnings.extend(pdf_warnings)
        elif _is_text(raw.content_type):
            result.content = raw.text()
        else:
            raise FetchError(f"Unsupported content type {raw.content_type!r} at {raw.url}")

        if raw.status >= 400 and not result.blocked_by:
            result.warnings.append(f"Server returned HTTP {raw.status}.")
        return result
