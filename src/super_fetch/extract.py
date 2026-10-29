"""Turn fetched documents into agent-friendly text."""

from __future__ import annotations

import io
from typing import Literal

import lxml.etree
import lxml.html
import trafilatura
from markdownify import markdownify

Format = Literal["markdown", "text", "html"]

_NON_CONTENT_TAGS = ("script", "style", "noscript", "template", "svg", "iframe")
_PERMALINK_TEXTS = {"¶", "#", "§", "🔗"}

# If the readability extraction keeps less than this share of the page's text,
# it probably dropped real content (common on docs/reference pages), so fall
# back to converting the whole body.
_MIN_EXTRACTED_RATIO = 0.3
_LAYOUT_TABLE_SHARE = 0.6
"""Nested tables holding at least this share of a page's text mean it is laid out with tables."""
_PDF_MAX_PAGES = 500


def parse_html(html: str) -> lxml.html.HtmlElement | None:
    try:
        return lxml.html.document_fromstring(html)
    except (ValueError, lxml.etree.ParserError):
        return None


def strip_non_content(doc: lxml.html.HtmlElement) -> lxml.html.HtmlElement:
    """Remove scripts, styles and similar nodes in place; return the body (or the document)."""
    for node in list(doc.iter(*_NON_CONTENT_TAGS)):
        node.drop_tree()
    body = doc.find("body")
    return body if body is not None else doc


def visible_text(element: lxml.html.HtmlElement) -> str:
    return " ".join(element.text_content().split())


def page_title(doc: lxml.html.HtmlElement) -> str | None:
    og = doc.find(".//meta[@property='og:title']")
    if og is not None and og.get("content"):
        return og.get("content").strip()
    title = doc.findtext(".//title")
    return " ".join(title.split()) if title else None


def _drop_permalink_anchors(doc: lxml.html.HtmlElement) -> None:
    """Remove heading self-links like `¶` / `#` that docs generators add."""
    for a in list(doc.iter("a")):
        if "headerlink" in (a.get("class") or "") or a.text_content().strip() in _PERMALINK_TEXTS:
            a.drop_tree()


def _unwrap_layout_tables(doc: lxml.html.HtmlElement) -> bool:
    """If the page is laid out with tables, turn them into plain blocks (a paragraph per row).

    Old-school sites (Hacker News, forums, mailing-list archives) build the whole page from
    nested tables; converted as tables, every cell ends up on its own line or in a garbled
    Markdown grid. A table that contains or sits inside another table is taken as layout, and
    only when such tables hold most of the page's text (Wikipedia's nested navboxes don't).
    """
    layout = [
        table
        for table in doc.iter("table")
        if table.find(".//table") is not None and next(table.iterancestors("table"), None) is None
    ]
    layout_text = sum(len(visible_text(table)) for table in layout)
    if not layout or layout_text < len(visible_text(doc)) * _LAYOUT_TABLE_SHARE:
        return False
    for table in layout:
        for cell in list(table.iter("td", "th")):
            cell.tag = "span"
            cell.tail = " " + (cell.tail or "")
        for element in list(table.iter("tr", "thead", "tbody", "tfoot", "caption", "table")):
            element.tag = "p" if element.tag == "tr" else "div"
    return True


def _convert_body(body: lxml.html.HtmlElement, fmt: Format) -> str:
    if fmt == "text":
        return _collapse_blank_lines(body.text_content())
    body_html = lxml.html.tostring(body, encoding="unicode")
    return _collapse_blank_lines(markdownify(body_html, heading_style="ATX", bullets="-", strip=["img"]))


def _collapse_blank_lines(text: str) -> str:
    out: list[str] = []
    for line in (line.rstrip() for line in text.splitlines()):
        if line or (out and out[-1]):
            out.append(line)
    return "\n".join(out).strip()


def html_to_content(html: str, url: str, fmt: Format) -> tuple[str | None, str]:
    """Return (title, content) for an HTML document in the requested format."""
    doc = parse_html(html)
    if doc is None:
        return None, html if fmt == "html" else ""
    title = page_title(doc)
    if fmt == "html":
        return title, html

    # trafilatura resolves relative links against the site root rather than the
    # page URL, so make them absolute first.
    doc.make_links_absolute(url, resolve_base_href=True, handle_failures="ignore")
    _drop_permalink_anchors(doc)
    if _unwrap_layout_tables(doc):
        # Readability extraction scrambles the inline content of such pages; convert all of it.
        return title, _convert_body(strip_non_content(doc), fmt)
    html = lxml.html.tostring(doc, encoding="unicode")

    body = strip_non_content(doc)
    page_text_length = len(visible_text(body))
    extracted = trafilatura.extract(
        html,
        url=url,
        output_format="markdown" if fmt == "markdown" else "txt",
        include_links=fmt == "markdown",
        include_tables=True,
        include_formatting=fmt == "markdown",
        favor_recall=True,
    )
    if extracted and len(extracted) >= page_text_length * _MIN_EXTRACTED_RATIO:
        return title, extracted
    return title, _convert_body(body, fmt)


class DocumentError(ValueError):
    """A document that can't be turned into text (corrupt, encrypted, ...)."""


def pdf_to_content(data: bytes) -> tuple[str | None, str, list[str]]:
    """Return (title, text, warnings) for a PDF, with a marker before each page."""
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    warnings: list[str] = []
    try:
        reader = PdfReader(io.BytesIO(data))
        encrypted = reader.is_encrypted and not reader.decrypt("")
        if not encrypted:
            title = reader.metadata.title if reader.metadata else None
            pages = [_collapse_blank_lines(page.extract_text() or "") for page in reader.pages[:_PDF_MAX_PAGES]]
            total = len(reader.pages)
    except (PdfReadError, ValueError, KeyError, TypeError) as exc:
        raise DocumentError(f"Could not read the PDF: {exc}") from exc
    if encrypted:
        raise DocumentError("The PDF is password-protected.")

    if total > _PDF_MAX_PAGES:
        warnings.append(f"The PDF has {total} pages; only the first {_PDF_MAX_PAGES} were read.")
    if not any(pages):
        warnings.append("The PDF has no extractable text (probably scanned images).")
    text = "\n\n".join(f"[Page {number}]\n\n{page}" for number, page in enumerate(pages, start=1))
    return (title.strip() or None) if isinstance(title, str) else None, text, warnings


def paginate(content: str, start_index: int, max_length: int) -> tuple[str, int | None]:
    """Slice `content` and return (chunk, next start index or None when finished).

    Cuts at a line break near the end of the window when possible so chunks
    don't split words or code lines.
    """
    end = start_index + max_length
    if end >= len(content):
        return content[start_index:], None
    newline = content.rfind("\n", start_index + int(max_length * 0.8), end)
    if newline != -1:
        end = newline + 1
    return content[start_index:end], end
