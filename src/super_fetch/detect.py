"""Recognize bot-challenge pages and JavaScript-only app shells."""

from __future__ import annotations

import re
from collections.abc import Iterable

from .extract import parse_html, strip_non_content, visible_text

# Challenge pages are small; only scan bodies under this size to avoid matching
# articles that merely mention a vendor.
_CHALLENGE_SCAN_LIMIT = 64 * 1024
_BLOCKING_STATUSES = {401, 403, 405, 429, 503}

_CHALLENGE_MARKERS: list[tuple[str, re.Pattern[str]]] = [
    ("cloudflare", re.compile(r"/cdn-cgi/challenge-platform/|_cf_chl_opt|cf-chl-|<title>Just a moment\.\.\.</title>")),
    ("cloudflare", re.compile(r"Attention Required! \| Cloudflare|cf-error-details")),
    ("datadome", re.compile(r"captcha-delivery\.com")),
    ("perimeterx", re.compile(r"_pxCaptcha|px-captcha|perimeterx", re.IGNORECASE)),
    ("imperva", re.compile(r"_Incapsula_Resource|Incapsula incident ID")),
    ("aws-waf", re.compile(r"awswaf|aws-waf-token", re.IGNORECASE)),
    ("akamai", re.compile(r"<title>Access Denied</title>[\s\S]*Reference #")),
]

# Pages that refuse access outright rather than asking the client to prove itself;
# waiting or solving won't get past them.
_HARD_BLOCK_MARKERS: list[tuple[str, re.Pattern[str]]] = [
    ("cloudflare", re.compile(r"Sorry, you have been blocked|You are being rate limited")),
    # DataDome's `t=bv` ("banned visitor"), in its config object or the captcha frame URL.
    ("datadome", re.compile(r"""['"]t['"]\s*:\s*['"]bv['"]|captcha-delivery\.com/captcha/\?\S*\bt=bv\b""")),
    ("imperva", re.compile(r"Incapsula incident ID")),
    ("akamai", re.compile(r"<title>Access Denied</title>[\s\S]*Reference #")),
]

_APP_ROOT_IDS = {"root", "app", "__next", "__nuxt", "svelte", "main-app", "application"}
_MIN_VISIBLE_TEXT = 200
_NEAR_EMPTY_TEXT = 50
_JS_NOTICE = re.compile(
    r"enable javascript|javascript (?:is )?(?:required|disabled|turned off)|requires? javascript|turn on javascript",
    re.IGNORECASE,
)
_SCRIPT_DATA_RATIO = 10
"""Inline script this many times the visible text: the content likely ships as data for JavaScript to render."""


def detect_challenge(status: int, headers: dict[str, str], html: str) -> str | None:
    """Return the anti-bot vendor name if the response is a challenge or block page."""
    if headers.get("cf-mitigated", "").lower() == "challenge":
        return "cloudflare"
    if status not in _BLOCKING_STATUSES and len(html) > _CHALLENGE_SCAN_LIMIT:
        return None
    sample = html[:_CHALLENGE_SCAN_LIMIT]
    for vendor, pattern in _CHALLENGE_MARKERS:
        if pattern.search(sample):
            return vendor
    if "x-datadome" in headers and status in _BLOCKING_STATUSES:
        return "datadome"
    return None


def detect_hard_block(html: str, frame_urls: Iterable[str] = ()) -> str | None:
    """Return the vendor if a challenge page is a final refusal; only meaningful after `detect_challenge`."""
    sample = "\n".join([html[:_CHALLENGE_SCAN_LIMIT], *frame_urls])
    for vendor, pattern in _HARD_BLOCK_MARKERS:
        if pattern.search(sample):
            return vendor
    return None


def is_spa_shell(html: str) -> bool:
    """True when the page has almost no server-rendered text and relies on JavaScript to build it."""
    doc = parse_html(html)
    if doc is None:
        return False

    has_app_root = doc.find(".//app-root") is not None or any(el.get("id") in _APP_ROOT_IDS for el in doc.iter())
    noscript_js = any("javascript" in el.text_content().lower() for el in doc.iter("noscript"))
    scripts = list(doc.iter("script"))
    inline_script = sum(len(el.text or "") for el in scripts if el.get("type") != "application/ld+json")
    text = visible_text(strip_non_content(doc))

    # A mount point or "enable JavaScript" notice is a strong sign; a script tag alone
    # is not (small static pages have analytics), so then require the page to be near-empty,
    # or to carry far more inline script (e.g. the content as a JSON array) than text.
    if has_app_root or noscript_js or _JS_NOTICE.search(text):
        return len(text) < _MIN_VISIBLE_TEXT
    if len(text) < _MIN_VISIBLE_TEXT and inline_script >= _SCRIPT_DATA_RATIO * max(len(text), _NEAR_EMPTY_TEXT):
        return True
    return bool(scripts) and len(text) < _NEAR_EMPTY_TEXT
