"""The identity both tiers present to sites: one browser fingerprint and one cookie jar, kept on disk.

Bot protections tie a passed challenge (e.g. Cloudflare's `cf_clearance` cookie) to the client that
solved it. Keeping the browser's fingerprint stable across restarts keeps those cookies valid, and
lets the HTTP tier reuse them by sending the browser's headers over a Firefox TLS fingerprint.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

CLEARANCE_COOKIES = frozenset({"cf_clearance", "datadome", "_px3", "aws-waf-token"})
"""Cookies that show a bot challenge was passed."""

_IDENTITY_FILE = "identity.json"
_COOKIES_FILE = "cookies.json"
_RECORDED_HEADERS = ("user-agent", "accept-language")

Cookie = dict[str, Any]
"""A cookie in Playwright's format: name, value, domain, path, expires (-1 = session), secure, ..."""


class Identity:
    def __init__(self, data_dir: Path | None):
        """`data_dir=None` keeps everything in memory for this process only."""
        self.data_dir = data_dir
        self._preset: dict[str, Any] | None = None
        self.headers: dict[str, str] = {}
        """User-Agent and Accept-Language the browser sends, as recorded from its requests."""
        self._cookies: list[Cookie] = []
        if data_dir is not None:
            saved = _read_json(data_dir / _IDENTITY_FILE)
            if isinstance(saved, dict):
                self._preset = saved.get("preset")
                self.headers = saved.get("headers") or {}
            self._cookies = _unexpired(_read_cookies(data_dir / _COOKIES_FILE))

    def preset(self, os: str = "any") -> dict[str, Any]:
        """The fingerprint preset the browser launches with: drawn once, then reused.

        `os` limits it to one operating system ("windows", "macos", "linux"). A saved identity
        for another one is reset, cookies included: they belong to the old fingerprint.
        """
        if self._preset is not None and os not in ("any", _preset_os(self._preset)):
            self.reset()
        if self._preset is None:
            from camoufox.fingerprints import get_random_preset

            self._preset = get_random_preset(os=None if os == "any" else os)
            self._save_identity()
        return self._preset

    def record_headers(self, sent: dict[str, str]) -> None:
        headers = {name: sent[name] for name in _RECORDED_HEADERS if name in sent}
        if headers and headers != self.headers:
            self.headers = headers
            self._save_identity()

    @property
    def cookies(self) -> list[Cookie]:
        return _unexpired(self._cookies)

    def update_cookies(self, cookies: list[Cookie]) -> None:
        """Take the browser's current cookies; persist those that outlive the session."""
        self._cookies = cookies
        if self.data_dir is None:
            return
        # Other server processes save to the same file: keep their cookies unless this browser has its own copy.
        path = self.data_dir / _COOKIES_FILE
        mine = {_cookie_key(c) for c in cookies}
        others = [c for c in _read_cookies(path) if _cookie_key(c) not in mine]
        _write_json(path, [c for c in _unexpired(others + cookies) if c.get("expires", -1) > 0])

    def cookies_for(self, url: str) -> dict[str, str]:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        path = parts.path or "/"
        return {
            c["name"]: c["value"]
            for c in self.cookies
            if _domain_matches(host, c.get("domain", ""))
            and path.startswith(c.get("path") or "/")
            and (parts.scheme == "https" or not c.get("secure"))
        }

    def has_clearance(self, url: str) -> bool:
        """True when the browser passed a bot challenge for this site and the HTTP tier can reuse it."""
        return "user-agent" in self.headers and not CLEARANCE_COOKIES.isdisjoint(self.cookies_for(url))

    def reset(self) -> None:
        """Forget the fingerprint and all cookies; the next browser start draws a new identity."""
        self._preset = None
        self.headers = {}
        self._cookies = []
        if self.data_dir is not None:
            for name in (_IDENTITY_FILE, _COOKIES_FILE):
                (self.data_dir / name).unlink(missing_ok=True)

    def _save_identity(self) -> None:
        if self.data_dir is not None:
            _write_json(self.data_dir / _IDENTITY_FILE, {"preset": self._preset, "headers": self.headers})


def _preset_os(preset: dict[str, Any]) -> str:
    platform = preset.get("navigator", {}).get("platform", "")
    return "windows" if platform.startswith("Win") else "macos" if platform.startswith("Mac") else "linux"


def _domain_matches(host: str, domain: str) -> bool:
    # Playwright marks domain cookies with a leading dot; without one the cookie is for that exact host.
    domain = domain.lower()
    if not domain.startswith("."):
        return host == domain
    return host == domain[1:] or host.endswith(domain)


def _cookie_key(cookie: Cookie) -> tuple[str, str, str]:
    return cookie.get("name", ""), cookie.get("domain", ""), cookie.get("path", "/")


def _unexpired(cookies: list[Cookie]) -> list[Cookie]:
    now = time.time()
    return [c for c in cookies if c.get("expires", -1) <= 0 or c["expires"] > now]


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        log.warning("Ignoring unreadable %s: %s", path, exc)
        return None


def _read_cookies(path: Path) -> list[Cookie]:
    data = _read_json(path)
    return [c for c in data if isinstance(c, dict) and "name" in c] if isinstance(data, list) else []


def _write_json(path: Path, data: Any) -> None:
    """Write atomically, readable only by the user: cookies are login-equivalent secrets."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except OSError as exc:
        log.warning("Could not save %s: %s", path, exc)
        with contextlib.suppress(OSError):
            tmp.unlink()
