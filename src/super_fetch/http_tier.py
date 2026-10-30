"""Tier 0: plain HTTP with a real-browser TLS/HTTP2 fingerprint (curl_cffi)."""

from __future__ import annotations

import asyncio
import codecs
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin

from curl_cffi import CurlError
from curl_cffi.requests import AsyncSession, BrowserType

from .config import Settings
from .identity import Identity
from .urlcheck import SafeBrowsing, check_url

_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_META_CHARSET = re.compile(rb"""<meta[^>]+charset=["']?([\w-]+)""", re.IGNORECASE)
_FIREFOX_TARGET = max(
    (t.value for t in BrowserType if t.value.startswith("firefox") and t.value[7:].isdigit()),
    key=lambda value: int(value[7:]),
)
"""Newest Firefox TLS fingerprint curl_cffi has, to match the (Firefox-based) browser tier."""


class FetchError(Exception):
    pass


@dataclass
class RawResponse:
    url: str
    status: int
    headers: dict[str, str]
    """Lower-cased header names."""
    body: bytes
    truncated: bool
    warnings: list[str] = field(default_factory=list)
    hard_block: str | None = None
    """Vendor whose block page the browser ended on, when only the browser could tell (e.g. from frames)."""

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "").split(";")[0].strip().lower()

    def text(self) -> str:
        return self.body.decode(self._charset(), errors="replace")

    def _charset(self) -> str:
        match = re.search(r"charset=([\w-]+)", self.headers.get("content-type", ""), re.IGNORECASE)
        if not match:
            match = _META_CHARSET.search(self.body[:4096])
        charset = match.group(1) if match else "utf-8"
        if isinstance(charset, bytes):
            charset = charset.decode("ascii", errors="ignore")
        try:
            codecs.lookup(charset)
        except LookupError:
            return "utf-8"
        return charset


class HttpFetcher:
    def __init__(self, settings: Settings, identity: Identity | None = None, safe_browsing: SafeBrowsing | None = None):
        self._settings = settings
        self._identity = identity
        self._safe_browsing = safe_browsing
        self._session: AsyncSession | None = None
        self._identity_session: AsyncSession | None = None

    def _get_session(self, url: str) -> tuple[AsyncSession, dict[str, Any]]:
        """The session and per-request options for `url`.

        Sites whose challenge the browser passed get the browser's own headers and cookies over a
        Firefox TLS fingerprint, since clearance only holds for a client that looks the same.
        """
        # Sessions live for the server's lifetime and keep cookies (consent, ...) between fetches.
        s = self._settings
        if self._identity is not None and self._identity.has_clearance(url):
            if self._identity_session is None:
                self._identity_session = AsyncSession(impersonate=_FIREFOX_TARGET, proxy=s.proxy)
            return self._identity_session, {
                "headers": self._identity.headers,
                "cookies": self._identity.cookies_for(url),
            }
        if self._session is None:
            self._session = AsyncSession(impersonate=s.impersonate, proxy=s.proxy)
        return self._session, {}

    async def close(self) -> None:
        for session in (self._session, self._identity_session):
            if session is not None:
                await session.close()
        self._session = self._identity_session = None

    async def fetch(self, url: str, timeout: float | None = None) -> RawResponse:
        """GET `url` within `timeout` seconds in total (default: the configured timeout)."""
        timeout = timeout or self._settings.timeout
        try:
            async with asyncio.timeout(timeout):
                return await self._fetch(url, timeout)
        except TimeoutError as exc:
            raise FetchError(f"Request to {url} timed out after {timeout:g}s") from exc

    async def _fetch(self, url: str, timeout: float) -> RawResponse:
        """Follow redirects manually so every hop passes the URL check."""
        s = self._settings
        for _ in range(s.max_redirects + 1):
            await check_url(url, allow_private=s.allow_private, safe_browsing=self._safe_browsing)
            session, options = self._get_session(url)
            try:
                resp = await session.request("GET", url, stream=True, allow_redirects=False, timeout=timeout, **options)
            except CurlError as exc:
                raise FetchError(f"Request to {url} failed: {exc}") from exc
            try:
                headers = {k.lower(): v for k, v in resp.headers.items()}
                location = headers.get("location")
                if resp.status_code in _REDIRECT_STATUSES and location:
                    url = urljoin(url, location)
                    continue
                body, truncated = await self._read_body(resp)
                return RawResponse(url=url, status=resp.status_code, headers=headers, body=body, truncated=truncated)
            finally:
                await resp.aclose()
        raise FetchError(f"Too many redirects (more than {s.max_redirects})")

    async def _read_body(self, resp) -> tuple[bytes, bool]:
        limit = self._settings.max_bytes
        chunks: list[bytes] = []
        size = 0
        try:
            async for chunk in resp.aiter_content():
                chunks.append(chunk)
                size += len(chunk)
                if size >= limit:
                    return b"".join(chunks)[:limit], True
        except CurlError as exc:
            raise FetchError(f"Reading response from {resp.url} failed: {exc}") from exc
        return b"".join(chunks), False
