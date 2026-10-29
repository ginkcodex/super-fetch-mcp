"""Runtime settings, read from SUPER_FETCH_* environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, get_args

from platformdirs import user_data_dir

BrowserMode = Literal["auto", "headless", "virtual", "headful"]
BrowserOS = Literal["any", "windows", "macos", "linux"]


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_choice(name: str, choices: tuple[Any, ...], default: Any) -> Any:
    value = os.environ.get(name, default).strip().lower()
    if value not in choices:
        raise ValueError(f"{name} must be one of {', '.join(choices)}, got {value!r}")
    return value


def _env_list(name: str) -> tuple[str, ...]:
    """Comma-separated values, blanks dropped."""
    return tuple(item for item in (part.strip() for part in os.environ.get(name, "").split(",")) if item)


@dataclass(frozen=True)
class Settings:
    timeout: float = 30.0
    """Seconds allowed for one fetch, including redirects."""
    max_length: int = 20_000
    """Default number of characters returned per tool call."""
    max_bytes: int = 10 * 1024 * 1024
    """Response bodies larger than this are cut off."""
    max_redirects: int = 10
    proxy: str | None = None
    allow_private: bool = False
    """Allow fetching localhost / private network addresses."""
    impersonate: str = "chrome"
    """curl_cffi browser fingerprint target ("chrome" tracks the newest supported)."""
    cache_ttl: float = 300.0
    """Seconds a fetched page is kept so paginated reads see the same content."""
    browser_mode: BrowserMode = "auto"
    """auto: virtual display (Xvfb) on Linux when available, else headless."""
    browser_concurrency: int = 3
    """Pages rendered in the browser at the same time."""
    browser_idle_timeout: float = 300.0
    """Seconds without browser use before it is shut down to free memory."""
    browser_os: BrowserOS = "any"
    """Operating system the browser's fingerprint claims (the browser itself always runs on this machine)."""
    data_dir: Path | None = None
    """Where the browser identity and cookies are kept between runs; None keeps them in memory only."""
    http_host: str = "127.0.0.1"
    """Address `serve --http` listens on."""
    http_port: int = 8000
    http_token: str | None = field(default=None, repr=False)
    """Bearer token `serve --http` requires on every request; required unless listening on loopback."""
    http_allowed_hosts: tuple[str, ...] = ()
    """Host names (besides localhost) that requests to `serve --http` may be addressed to, e.g. its domain."""

    @classmethod
    def from_env(cls) -> Settings:
        env = os.environ.get
        data_dir = env("SUPER_FETCH_DATA_DIR", user_data_dir("super-fetch"))
        return cls(
            data_dir=Path(data_dir).expanduser() if data_dir else None,
            timeout=float(env("SUPER_FETCH_TIMEOUT", cls.timeout)),
            max_length=int(env("SUPER_FETCH_MAX_LENGTH", cls.max_length)),
            max_bytes=int(env("SUPER_FETCH_MAX_BYTES", cls.max_bytes)),
            max_redirects=int(env("SUPER_FETCH_MAX_REDIRECTS", cls.max_redirects)),
            proxy=env("SUPER_FETCH_PROXY") or None,
            allow_private=_env_bool("SUPER_FETCH_ALLOW_PRIVATE", cls.allow_private),
            impersonate=env("SUPER_FETCH_IMPERSONATE", cls.impersonate),
            cache_ttl=float(env("SUPER_FETCH_CACHE_TTL", cls.cache_ttl)),
            browser_mode=_env_choice("SUPER_FETCH_BROWSER_MODE", get_args(BrowserMode), cls.browser_mode),
            browser_concurrency=int(env("SUPER_FETCH_BROWSER_CONCURRENCY", cls.browser_concurrency)),
            browser_idle_timeout=float(env("SUPER_FETCH_BROWSER_IDLE_TIMEOUT", cls.browser_idle_timeout)),
            browser_os=_env_choice("SUPER_FETCH_BROWSER_OS", get_args(BrowserOS), cls.browser_os),
            http_host=env("SUPER_FETCH_HTTP_HOST", cls.http_host),
            http_port=int(env("SUPER_FETCH_HTTP_PORT", cls.http_port)),
            http_token=env("SUPER_FETCH_HTTP_TOKEN") or None,
            http_allowed_hosts=_env_list("SUPER_FETCH_HTTP_ALLOWED_HOSTS"),
        )
