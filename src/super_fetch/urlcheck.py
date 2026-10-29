"""URL validation, including blocking of private network targets (SSRF)."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit


class BlockedURLError(ValueError):
    pass


async def check_url(url: str, *, allow_private: bool) -> None:
    """Raise BlockedURLError unless `url` is http(s) and resolves only to public addresses."""
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        raise BlockedURLError(f"Only http and https URLs are supported, got {parts.scheme or 'none'!r}")
    host = parts.hostname
    if not host:
        raise BlockedURLError(f"URL has no host: {url}")
    if allow_private:
        return

    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, parts.port or 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise BlockedURLError(f"Cannot resolve host {host!r}: {exc}") from exc

    for *_, sockaddr in infos:
        ip = ipaddress.ip_address(sockaddr[0])
        if not ip.is_global:
            raise BlockedURLError(
                f"{host} resolves to non-public address {ip}; set SUPER_FETCH_ALLOW_PRIVATE=1 to allow it"
            )
