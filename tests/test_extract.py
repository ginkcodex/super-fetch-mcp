import pytest

from super_fetch.extract import DocumentError, html_to_content, paginate, pdf_to_content

DOCS_PAGE = (
    """<html><head><title>Guide</title></head><body>
<nav><a href="/">Home</a></nav>
<main>
<h1>Install<a class="headerlink" href="#install">¶</a></h1>
<p>Read the <a href="glossary.html">glossary</a> before you start. """
    + "More words here. " * 30
    + """</p>
<pre><code>pip install thing</code></pre>
</main>
<script>var tracking = "x".repeat(10000);</script>
</body></html>"""
)


def test_markdown_extraction():
    title, md = html_to_content(DOCS_PAGE, "https://example.com/docs/3/guide.html", "markdown")
    assert title == "Guide"
    assert "# Install" in md
    assert "pip install thing" in md
    assert "tracking" not in md


def test_relative_links_resolve_against_page_url():
    _, md = html_to_content(DOCS_PAGE, "https://example.com/docs/3/guide.html", "markdown")
    assert "https://example.com/docs/3/glossary.html" in md


def test_permalink_anchors_removed():
    _, md = html_to_content(DOCS_PAGE, "https://example.com/docs/3/guide.html", "markdown")
    assert "¶" not in md


def test_text_format_has_no_markdown_links():
    _, text = html_to_content(DOCS_PAGE, "https://example.com/docs/3/guide.html", "text")
    assert "glossary" in text
    assert "](" not in text


def test_html_format_is_raw():
    _, html = html_to_content(DOCS_PAGE, "https://example.com/", "html")
    assert html == DOCS_PAGE


LAYOUT_PAGE = """<html><body><table><tr><td><b>Site</b> <a href="/new">new</a></td></tr>
<tr><td><table>{rows}</table></td></tr></table></body></html>"""
STORY_ROW = (
    '<tr><td>{i}.</td><td><a href="https://ex.com/{i}">Story {i}</a></td></tr><tr><td>{i}0 points by bob</td></tr>'
)


def test_layout_tables_become_one_paragraph_per_row():
    html = LAYOUT_PAGE.format(rows="".join(STORY_ROW.format(i=i) for i in range(1, 4)))
    _, content = html_to_content(html, "https://news.example/", "markdown")
    assert "[Story 2](https://ex.com/2)" in content
    assert "20 points by bob" in content
    assert "|" not in content


def test_data_tables_stay_tables():
    article = "<p>" + "Long article text. " * 40 + "</p>"
    nav = "<table><tr><td><table><tr><td>nav</td></tr></table></td></tr></table>"
    data = "<table><tr><th>City</th><th>Population</th></tr><tr><td>Tokyo</td><td>37M</td></tr></table>"
    _, content = html_to_content(
        f"<html><body><article>{article}{data}</article>{nav}</body></html>", "https://x/", "markdown"
    )
    assert "| Tokyo | 37M |" in content


def test_paginate_whole_content_fits():
    assert paginate("abc", 0, 10) == ("abc", None)


def test_paginate_cuts_at_line_break():
    content = "line one\n" * 20  # 180 chars
    chunk, next_index = paginate(content, 0, 50)
    assert chunk.endswith("\n")
    assert next_index == len(chunk)
    rest, _ = paginate(content, next_index, 1000)
    assert chunk + rest == content


def test_paginate_past_end():
    assert paginate("abc", 10, 5) == ("", None)


def test_pdf_text_with_page_markers(make_pdf):
    title, text, warnings = pdf_to_content(make_pdf("First page text", "Second page text"))
    assert text == "[Page 1]\n\nFirst page text\n\n[Page 2]\n\nSecond page text"
    assert title is None
    assert warnings == []


def test_pdf_without_text_warns(make_pdf):
    _, _, warnings = pdf_to_content(make_pdf(""))
    assert any("no extractable text" in w for w in warnings)


def test_corrupt_pdf_raises():
    with pytest.raises(DocumentError):
        pdf_to_content(b"%PDF-1.4\nthis is not really a pdf")
