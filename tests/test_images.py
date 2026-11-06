"""The fetch_image tool: image preparation, and MCP client → server → local HTTP server."""

import base64
import io
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from mcp import Client
from PIL import Image

from super_fetch.config import Settings
from super_fetch.imaging import ImageError, is_svg, prepare_image
from super_fetch.server import build_server

from conftest import build_image


def test_model_ready_image_passes_through():
    data = build_image("PNG")
    image = prepare_image(data)
    assert (image.data, image.mime_type, image.width, image.height) == (data, "image/png", 40, 30)
    assert image.notes == []


def test_large_image_is_scaled_down():
    image = prepare_image(build_image("JPEG", size=(4000, 2000)))
    assert (image.width, image.height) == (1568, 784)
    assert image.mime_type == "image/jpeg"
    assert image.notes == ["Scaled down from 4000x2000."]


@pytest.mark.parametrize("fmt", ["BMP", "TIFF", "AVIF"])
def test_other_formats_are_converted(fmt):
    image = prepare_image(build_image(fmt))
    assert image.mime_type == "image/jpeg"
    assert image.source_format == fmt
    assert f"Converted from {fmt}." in image.notes


def test_transparency_kept_as_png():
    image = prepare_image(build_image("TIFF", mode="RGBA"))
    assert image.mime_type == "image/png"
    assert Image.open(io.BytesIO(image.data)).mode == "RGBA"


def test_animation_noted():
    image = prepare_image(build_image("GIF", frames=3))
    assert image.mime_type == "image/gif"
    assert image.notes == ["Animated (3 frames): you see the first frame."]


@pytest.mark.parametrize("data", [b"<html>not an image</html>", build_image("PNG")[:60], b""])
def test_unreadable_bytes_raise(data):
    with pytest.raises(ImageError):
        prepare_image(data)


def test_decompression_bomb_refused():
    out = io.BytesIO()
    Image.new("1", (10_000, 10_000)).save(out, "PNG")  # 100M pixels in a few KB
    with pytest.raises(ImageError):
        prepare_image(out.getvalue())


def test_svg_detection():
    assert is_svg(b"<svg xmlns='http://www.w3.org/2000/svg'/>", "")
    assert is_svg(b"<?xml version='1.0'?>\n<svg/>", "application/octet-stream")
    assert is_svg(b"anything", "image/svg+xml")
    assert not is_svg(b"<html><svg/></html>", "text/html")


PNG = build_image("PNG")
SVG = b"<svg xmlns='http://www.w3.org/2000/svg' width='10' height='10'><rect width='10' height='10'/></svg>"
SEEN: dict[str, dict[str, str]] = {}
"""Request headers per path."""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        SEEN[self.path] = {k.lower(): v for k, v in self.headers.items()}
        page = f"http://127.0.0.1:{self.server.server_port}/article"
        routes = {
            "/photo.png": (200, "image/png", PNG),
            "/drawing.svg": (200, "image/svg+xml", SVG),
            "/gone.png": (404, "text/html", b"<html>not found</html>"),
            "/page.png": (200, "text/html", b"<html><body>a page</body></html>"),
            "/mislabeled": (200, "application/octet-stream", PNG),
        }
        if self.path == "/hotlinked.png":  # served only to the site's own pages
            ok = self.headers.get("Referer") == page
            routes[self.path] = (200, "image/png", PNG) if ok else (403, "text/plain", b"hotlinking")
        status, content_type, body = routes.get(self.path, (404, "text/plain", b"not found"))
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def base_url():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


@pytest.fixture
async def client():
    async with Client(build_server(Settings(allow_private=True))) as c:
        yield c


@pytest.mark.anyio
async def test_tool_returns_the_image(client, base_url):
    result = await client.call_tool("fetch_image", {"url": f"{base_url}/photo.png", "render": "never"})
    assert not result.is_error
    header, image = result.content
    assert "Image: 40x30 PNG, sent as image/png" in header.text
    assert image.mime_type == "image/png"
    assert base64.b64decode(image.data) == PNG


@pytest.mark.anyio
async def test_referer_makes_an_image_request(client, base_url):
    args = {"url": f"{base_url}/hotlinked.png", "render": "never"}
    assert (await client.call_tool("fetch_image", args)).is_error
    result = await client.call_tool("fetch_image", {**args, "referer": f"{base_url}/article"})
    assert not result.is_error
    headers = SEEN["/hotlinked.png"]
    assert headers["sec-fetch-dest"] == "image"
    assert headers["sec-fetch-site"] == "same-origin"
    assert headers["accept"].startswith("image/")
    assert "sec-fetch-user" not in headers


@pytest.mark.anyio
async def test_svg_returned_as_source(client, base_url):
    result = await client.call_tool("fetch_image", {"url": f"{base_url}/drawing.svg", "render": "never"})
    assert [block.type for block in result.content] == ["text"]
    assert "SVG source:" in result.content[0].text
    assert "<rect" in result.content[0].text


@pytest.mark.anyio
async def test_mislabeled_image_still_read(client, base_url):
    result = await client.call_tool("fetch_image", {"url": f"{base_url}/mislabeled", "render": "never"})
    assert not result.is_error
    assert result.content[1].mime_type == "image/png"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("path", "message"), [("/gone.png", "returned HTTP 404"), ("/page.png", "returned a web page instead of an image")]
)
async def test_not_an_image_is_an_error(client, base_url, path, message):
    result = await client.call_tool("fetch_image", {"url": f"{base_url}{path}"})
    assert result.is_error
    assert message in result.content[0].text


@pytest.mark.anyio
async def test_private_address_blocked_by_default(base_url):
    async with Client(build_server(Settings())) as c:
        result = await c.call_tool("fetch_image", {"url": f"{base_url}/photo.png"})
    assert result.is_error
    assert "non-public" in result.content[0].text
