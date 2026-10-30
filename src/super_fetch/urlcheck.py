"""URL validation: private network targets (SSRF) and, optionally, Google Safe Browsing."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
from collections import OrderedDict
from urllib.parse import urlsplit

from curl_cffi import CurlError
from curl_cffi.requests import AsyncSession

from . import __version__

SAFE_BROWSING_API = "https://safebrowsing.googleapis.com/v4/threatMatches:find"
SAFE_BROWSING_ADVISORY = "https://developers.google.com/safe-browsing/v4/advisory"
_THREATS = {
    "SOCIAL_ENGINEERING": "is a suspected phishing or deceptive site",
    "MALWARE": "may host malware",
    "UNWANTED_SOFTWARE": "may host unwanted software",
    "POTENTIALLY_HARMFUL_APPLICATION": "may host harmful apps",
}
_SAFE_TTL = 300.0
"""Seconds a URL found on no list is trusted before it is looked up again."""
_CACHE_SIZE = 1024
_API_TIMEOUT = 10.0


class BlockedURLError(ValueError):
    pass


class UnsafeURLError(BlockedURLError):
    """Google Safe Browsing lists the URL as dangerous."""


async def check_url(url: str, *, allow_private: bool, safe_browsing: SafeBrowsing | None = None) -> None:
    """Raise BlockedURLError unless `url` is http(s), resolves only to public addresses (unless
    `allow_private`), and isn't on a Safe Browsing list (if `safe_browsing` is given)."""
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        raise BlockedURLError(f"Only http and https URLs are supported, got {parts.scheme or 'none'!r}")
    host = parts.hostname
    if not host:
        raise BlockedURLError(f"URL has no host: {url}")
    if not allow_private:
        await _check_public(host, parts.port)
    if safe_browsing is not None:
        await safe_browsing.check(url)


async def _check_public(host: str, port: int | None) -> None:
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port or 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise BlockedURLError(f"Cannot resolve host {host!r}: {exc}") from exc

    for *_, sockaddr in infos:
        ip = ipaddress.ip_address(sockaddr[0])
        if not ip.is_global:
            raise BlockedURLError(
                f"{host} resolves to non-public address {ip}; set SUPER_FETCH_ALLOW_PRIVATE=1 to allow it"
            )


class SafeBrowsing:
    """Looks URLs up in Google Safe Browsing (Lookup API v4), caching verdicts.

    The full URL is sent to Google. If the lookup itself fails (bad key, quota, network), the URL
    is refused rather than fetched unchecked.
    """

    def __init__(self, api_key: str, api_url: str | None = None):
        self._api_key = api_key
        self._api_url = api_url or SAFE_BROWSING_API
        self._session: AsyncSession | None = None
        self._cache: OrderedDict[str, tuple[float, list[str]]] = OrderedDict()
        """URL -> (expiry, threat types found; empty if none)."""

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def check(self, url: str) -> None:
        cached = self._cache.get(url)
        if cached and cached[0] > time.monotonic():
            threats = cached[1]
        else:
            threats, ttl = await self._lookup(url)
            self._cache[url] = (time.monotonic() + ttl, threats)
            self._cache.move_to_end(url)
            while len(self._cache) > _CACHE_SIZE:
                self._cache.popitem(last=False)
        if threats:
            # Google's terms: qualified wording ("suspected", "may") and an attribution with a link.
            verdicts = " and ".join(_THREATS.get(t, f"is listed as {t}") for t in threats)
            raise UnsafeURLError(
                f"Not fetching {url}: Google Safe Browsing reports that this page {verdicts}. "
                f"Don't try to reach it another way. Advisory provided by Google: {SAFE_BROWSING_ADVISORY}"
            )

    async def _lookup(self, url: str) -> tuple[list[str], float]:
        """The threat types `url` is listed under, and how long that answer may be cached."""
        if self._session is None:
            # Straight to Google, not through SUPER_FETCH_PROXY.
            self._session = AsyncSession()
        body = {
            "client": {"clientId": "super-fetch", "clientVersion": __version__},
            "threatInfo": {
                "threatTypes": list(_THREATS),
                "platformTypes": ["ANY_PLATFORM"],
                "threatEntryTypes": ["URL"],
                "threatEntries": [{"url": url}],
            },
        }
        failed = f"Could not check {url} with Google Safe Browsing, so not fetching it"
        try:
            # The key goes in a header, not the URL, so it can't end up in error messages.
            resp = await self._session.post(
                self._api_url, json=body, headers={"X-Goog-Api-Key": self._api_key}, timeout=_API_TIMEOUT
            )
        except CurlError as exc:
            raise BlockedURLError(f"{failed}: {exc}") from exc
        try:
            data = resp.json()
        except ValueError:
            data = None
        if resp.status_code != 200 or not isinstance(data, dict):
            error = data.get("error") if isinstance(data, dict) else None
            message = error.get("message", "") if isinstance(error, dict) else ""
            raise BlockedURLError(f"{failed}: HTTP {resp.status_code} {message}".rstrip())

        matches = data.get("matches", [])
        if not matches:
            return [], _SAFE_TTL
        threats = list(dict.fromkeys(m.get("threatType", "UNKNOWN") for m in matches))
        ttl = min(_parse_duration(m.get("cacheDuration")) for m in matches)
        return threats, ttl


def _parse_duration(value: str | None) -> float:
    """A protobuf Duration such as "300.000s", in seconds (0 if missing or malformed)."""
    try:
        return float(value.removesuffix("s")) if value else 0.0
    except ValueError:
        return 0.0
