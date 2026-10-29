"""`serve --http`: the MCP server over streamable HTTP, so agents on other machines can use it."""

from __future__ import annotations

import hmac
import sys

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .config import Settings

LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
WILDCARD_HOSTS = ("0.0.0.0", "::", "")
MIN_TOKEN_LENGTH = 32
ENDPOINT = "/mcp"


class BearerTokenMiddleware:
    """Rejects HTTP requests that don't carry `Authorization: Bearer <token>`."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self._expected = f"Bearer {token}".encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            supplied = dict(scope["headers"]).get(b"authorization", b"")
            if not hmac.compare_digest(supplied, self._expected):
                response = PlainTextResponse(
                    "Missing or wrong bearer token.\n", status_code=401, headers={"WWW-Authenticate": "Bearer"}
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def check_settings(settings: Settings) -> None:
    """Raise ValueError for settings that would leave the server open to anyone who can reach it."""
    token = settings.http_token
    if token is None and settings.http_host not in LOOPBACK_HOSTS:
        raise ValueError(
            f"refusing to listen on {settings.http_host} without SUPER_FETCH_HTTP_TOKEN: anyone who can reach "
            "the port could use the server (and its browser identity). Generate a token with "
            "`openssl rand -hex 32`."
        )
    if token is not None and len(token) < MIN_TOKEN_LENGTH:
        raise ValueError(
            f"SUPER_FETCH_HTTP_TOKEN must be at least {MIN_TOKEN_LENGTH} characters; "
            "generate one with `openssl rand -hex 32`."
        )


def allowed_hosts(settings: Settings) -> list[str]:
    """Host names requests may be addressed to: localhost, the address listened on, and the configured ones."""
    names = ["127.0.0.1", "localhost", "[::1]", *settings.http_allowed_hosts]
    if settings.http_host not in (*LOOPBACK_HOSTS, *WILDCARD_HOSTS):
        names.append(f"[{settings.http_host}]" if ":" in settings.http_host else settings.http_host)
    return names


def build_app(server: MCPServer, settings: Settings) -> ASGIApp:
    check_settings(settings)
    # Requests must be addressed to a known host name (the SDK's DNS rebinding protection), so a web
    # page the user visits can't reach the server by pointing its own domain at the server's address.
    # The SDK only turns this on by itself for loopback addresses.
    names = allowed_hosts(settings)
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[h for name in names for h in (name, f"{name}:*")],
        allowed_origins=[o for name in names for o in (f"http://{name}", f"http://{name}:*", f"https://{name}")],
    )
    app = server.streamable_http_app(
        streamable_http_path=ENDPOINT, host=settings.http_host, transport_security=security
    )
    return BearerTokenMiddleware(app, settings.http_token) if settings.http_token else app


def serve(server: MCPServer, settings: Settings, log_level: str) -> None:
    import uvicorn

    app = build_app(server, settings)
    host = f"[{settings.http_host}]" if ":" in settings.http_host else settings.http_host
    auth = "bearer token required" if settings.http_token else "no token, loopback only"
    print(f"super-fetch MCP endpoint: http://{host}:{settings.http_port}{ENDPOINT} ({auth})", file=sys.stderr)
    print(f"Accepting requests addressed to: {', '.join(allowed_hosts(settings))}", file=sys.stderr)
    uvicorn.run(
        app,
        host=settings.http_host,
        port=settings.http_port,
        log_level=log_level.lower(),
        # Agents keep a response stream open; don't wait on them for long when stopping.
        timeout_graceful_shutdown=5,
    )
