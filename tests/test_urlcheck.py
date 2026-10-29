import pytest

from super_fetch.urlcheck import BlockedURLError, check_url

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/", "javascript:alert(1)", "https://"])
async def test_rejects_non_http_urls(url):
    with pytest.raises(BlockedURLError):
        await check_url(url, allow_private=True)


@pytest.mark.parametrize("url", ["http://localhost/", "http://127.0.0.1:8080/", "http://10.0.0.1/", "http://[::1]/"])
async def test_blocks_private_addresses(url):
    with pytest.raises(BlockedURLError, match="non-public"):
        await check_url(url, allow_private=False)


async def test_allow_private_permits_localhost():
    await check_url("http://127.0.0.1:8080/", allow_private=True)
