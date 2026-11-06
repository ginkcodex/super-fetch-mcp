import pytest

from super_fetch.extract import DocumentError, _render_images, html_to_content, paginate, pdf_to_content

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


ARTICLE_TEXT = "<p>" + "Plenty of article text about the topic. " * 30 + "</p>"
IMAGE_PAGE = (
    """<html><head><title>Post</title></head><body><nav><img src="/logo.png" alt="Logo" width="32"></nav>
<article><h1>Post</h1>"""
    + ARTICLE_TEXT
    + """<p><img src="charts/sales.png" alt="Bar chart of sales by month" width="800" height="400"></p>"""
    + ARTICLE_TEXT
    + """<p><img src="data:image/gif;base64,R0lGOD" data-src="https://cdn.example/lazy.jpg" alt="A lazy photo"></p>
<p><img src="/spacer.gif" alt=""><img src="/deco.png" role="presentation"></p>
<p><a href="https://example.com/full.png"><img src="/thumb.png" alt="Diagram"></a></p>
<p><img srcset="/small.webp 320w, /big.webp 960w"></p>
<noscript><img src="https://cdn.example/lazy.jpg" alt="A lazy photo"></noscript>"""
    + ARTICLE_TEXT
    + "</article></body></html>"
)


def test_images_kept_as_lines():
    _, md = html_to_content(IMAGE_PAGE, "https://example.com/blog/post", "markdown")
    images = [line for line in md.splitlines() if line.startswith("[Image")]
    assert images == [
        "[Image: Bar chart of sales by month] https://example.com/blog/charts/sales.png",
        "[Image: A lazy photo] https://cdn.example/lazy.jpg",
        "[Image: Diagram] https://example.com/thumb.png",
        "[Image] https://example.com/small.webp",
    ]
    assert "](https://example.com/full.png)" not in md  # the image was the link's only content


def test_link_holding_two_images():
    link = '<p><a href="/story"><img src="/a.jpg" alt="First"><img src="/b.jpg" alt="Second"></a></p>'
    html = f"<html><body><article>{ARTICLE_TEXT}{link}{ARTICLE_TEXT}</article></body></html>"
    _, md = html_to_content(html, "https://example.com/", "markdown")
    assert "[Image: First] https://example.com/a.jpg" in md
    assert "[Image: Second] https://example.com/b.jpg" in md


def test_repeated_image_marker_rendered_once():
    marker = " abcs0m[Image] https://x/a.png abce "
    content = f"Intro\n\n{marker}\n\nText\n\nIntro\n\n{marker}\n\nText"
    assert _render_images(content, ["[Image] https://x/a.png"], "abc") == (
        "Intro\n\n[Image] https://x/a.png\n\nText\n\nIntro\n\nText"
    )


def test_images_in_text_format():
    _, text = html_to_content(IMAGE_PAGE, "https://example.com/blog/post", "text")
    assert "\n[Image: Bar chart of sales by month] https://example.com/blog/charts/sales.png\n" in text


def test_images_off():
    _, md = html_to_content(IMAGE_PAGE, "https://example.com/blog/post", "markdown", images=False)
    assert "Image" not in md
    assert "sales.png" not in md


def test_image_inside_paragraph_gets_its_own_line():
    html = (
        "<html><body><article>"
        + ARTICLE_TEXT
        + '<p><img src="/a.png" alt="Chart">The chart shows growth.</p>'
        + ARTICLE_TEXT
        + "</article></body></html>"
    )
    _, md = html_to_content(html, "https://example.com/", "markdown")
    assert "\n[Image: Chart] https://example.com/a.png\n\nThe chart shows growth." in md


def test_figure_keeps_image_and_caption():
    figure = (
        '<figure><a href="/wiki/File:Tower.jpg"><img src="//upload.example/tower.jpg" width="250" height="300"></a>'
        "<figcaption>The tower in 1889</figcaption></figure>"
    )
    html = f"<html><body><article>{ARTICLE_TEXT}{figure}{ARTICLE_TEXT}</article></body></html>"
    _, md = html_to_content(html, "https://wiki.example/wiki/Tower", "markdown")
    assert "\n[Image] https://upload.example/tower.jpg\n\nThe tower in 1889\n" in md


def test_images_kept_on_layout_table_pages():
    rows = "".join(STORY_ROW.format(i=i) for i in range(1, 4))
    html = LAYOUT_PAGE.format(rows=rows + '<tr><td><img src="/shot.png" alt="Screenshot"></td></tr>')
    _, content = html_to_content(html, "https://news.example/", "markdown")
    assert "[Image: Screenshot] https://news.example/shot.png" in content


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
