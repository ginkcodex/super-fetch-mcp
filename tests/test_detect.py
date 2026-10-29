from super_fetch.detect import detect_challenge, detect_hard_block, is_spa_shell

CLOUDFLARE_CHALLENGE = """<!DOCTYPE html><html><head><title>Just a moment...</title></head>
<body><script>window._cf_chl_opt={cvId:'3'};</script>
<script src="/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page/v1"></script></body></html>"""

ARTICLE = (
    "<html><head><title>Post</title></head><body><article><p>" + "Real content. " * 50 + "</p></article></body></html>"
)


def test_cloudflare_challenge_page():
    assert detect_challenge(403, {"server": "cloudflare"}, CLOUDFLARE_CHALLENGE) == "cloudflare"


def test_cloudflare_mitigated_header():
    assert detect_challenge(200, {"cf-mitigated": "challenge"}, "") == "cloudflare"


def test_datadome_header_on_block():
    assert detect_challenge(403, {"x-datadome": "protected"}, "<html></html>") == "datadome"


def test_large_page_mentioning_vendor_is_not_challenge():
    html = ARTICLE + "<p>How Cloudflare's /cdn-cgi/challenge-platform/ works</p>" + " " * 70_000
    assert detect_challenge(200, {}, html) is None


def test_normal_page_is_not_challenge():
    assert detect_challenge(200, {}, ARTICLE) is None


def test_challenge_is_not_a_hard_block():
    assert detect_hard_block(CLOUDFLARE_CHALLENGE) is None


def test_cloudflare_block_page_is_hard_block():
    html = "<title>Attention Required! | Cloudflare</title><h1>Sorry, you have been blocked</h1>"
    assert detect_hard_block(html) == "cloudflare"


def test_datadome_ban_is_hard_block():
    # The ban shows in the config object on the HTTP response, or in the frame URL in the browser.
    assert detect_hard_block("<script>var dd={'rt':'c','t':'bv','host':'geo.captcha-delivery.com'}</script>")
    interstitial = "<script>var dd={'rt':'i','host':'geo.captcha-delivery.com'}</script>"
    assert detect_hard_block(interstitial) is None
    frame = "https://geo.captcha-delivery.com/captcha/?initialCid=x&cid=y&t=bv&s=1"
    assert detect_hard_block(interstitial, [frame]) == "datadome"
    assert detect_hard_block(interstitial, [frame.replace("t=bv", "t=fe")]) is None


def test_react_shell():
    html = '<html><body><div id="root"></div><script src="/main.js"></script></body></html>'
    assert is_spa_shell(html)


def test_noscript_shell():
    html = "<html><body><noscript>You need to enable JavaScript to run this app.</noscript></body></html>"
    assert is_spa_shell(html)


def test_server_rendered_page_is_not_shell():
    html = ARTICLE.replace("<article>", '<div id="__next"><article>').replace("</article>", "</article></div>")
    assert not is_spa_shell(html + "<script src='/app.js'></script>")


def test_tiny_static_page_is_not_shell():
    assert not is_spa_shell("<html><body><p>Hello</p></body></html>")


def test_empty_page_with_js_challenge_is_shell():
    # Reddit-style proof-of-work page: a script, a form, no text.
    html = "<html><head><script>solve()</script></head><body><main><form></form></main></body></html>"
    assert is_spa_shell(html)


def test_small_static_page_with_analytics_is_not_shell():
    # example.com-style: one short paragraph plus a tracking script.
    html = "<html><body><h1>Example Domain</h1><p>" + "x" * 150 + "</p><script>track()</script></body></html>"
    assert not is_spa_shell(html)


def test_visible_enable_javascript_notice_is_shell():
    notice = "<div class='notice'>Enable JavaScript to see products</div>"
    assert is_spa_shell(f"<html><body><h1>Shop</h1>{notice}<script src='/a.js'></script></body></html>")


def test_content_shipped_as_script_data_is_shell():
    data = ",".join(f'{{"text": "Quote number {i} said by someone"}}' for i in range(40))
    page = "<h1>Quotes to Scrape</h1><a href='/login'>Login</a>"
    assert is_spa_shell(f"<html><body>{page}<script>var data = [{data}];</script></body></html>")


def test_small_page_with_analytics_snippet_is_not_shell():
    snippet = (
        "(function(w,d){w.dataLayer=w.dataLayer||[];"
        "w.dataLayer.push({'gtm.start':new Date().getTime()});})(window,document);"
    )
    text = "A small but real page with a few sentences of text. " * 2
    assert not is_spa_shell(f"<html><body><p>{text}</p><script>{snippet}</script></body></html>")
