"""
Streamable HTTP transport for remote MCP clients (Copilot Studio, Claude, VS Code…).

Endpoints:

* ``POST /mcp``    — the MCP endpoint (Streamable HTTP, stateless, JSON responses)
* ``GET /healthz`` — liveness probe for Azure Container Apps (no auth)

Credentials: ``Authorization: Bearer <api-key or Entra ID token>``, or
``X-API-Key: <api-key>``.
* ``GET /.well-known/oauth-protected-resource/mcp`` — RFC 9728 metadata, only when
  Entra ID auth and ``MCP_PUBLIC_URL`` are configured

The server runs *stateless*: every request is independent, so the container can
scale out to several replicas without sticky sessions.
"""
from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Any

from mcp.server.auth.middleware.auth_context import AuthContextMiddleware
from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend, RequireAuthMiddleware
from mcp.server.auth.routes import build_resource_metadata_url, create_protected_resource_routes
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from .auth import AuthConfig, MultiTokenVerifier

_LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")


class _McpEndpoint:
    """ASGI wrapper so Starlette treats the session manager as a raw ASGI app."""

    def __init__(self, manager: StreamableHTTPSessionManager) -> None:
        self.manager = manager

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self.manager.handle_request(scope, receive, send)


class _ApiKeyHeaderMiddleware:
    """Accept ``X-API-Key: <key>`` as an alternative to ``Authorization: Bearer <key>``.

    Some MCP clients (e.g. Copilot Studio's "API key" option) send the key in a
    custom header instead of a bearer token.
    """

    def __init__(self, app: Any, header: str = "x-api-key") -> None:
        self.app = app
        self.header = header.lower().encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = list(scope.get("headers", []))
            names = {k.lower() for k, _ in headers}
            if b"authorization" not in names:
                key = next((v for k, v in headers if k.lower() == self.header), None)
                if key:
                    headers.append((b"authorization", b"Bearer " + key.strip()))
                    scope = {**scope, "headers": headers}
        await self.app(scope, receive, send)


def _security_settings(bind_host: str, allowed_hosts: list[str]) -> TransportSecuritySettings:
    if allowed_hosts:
        return TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=allowed_hosts,
            allowed_origins=[f"https://{h}" for h in allowed_hosts]
            + [f"http://{h}" for h in allowed_hosts],
        )
    if bind_host in _LOCAL_HOSTS:
        # Protect a server bound to localhost from DNS-rebinding attacks by web pages.
        return TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*"],
            allowed_origins=["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"],
        )
    # Public deployment behind an ingress: requests are authenticated instead.
    return TransportSecuritySettings(enable_dns_rebinding_protection=False)


def create_app(
    mcp_server: Any,
    auth: AuthConfig,
    *,
    bind_host: str = "127.0.0.1",
    allowed_hosts: list[str] | None = None,
    verifier: MultiTokenVerifier | None = None,
) -> Starlette:
    manager = StreamableHTTPSessionManager(
        app=mcp_server,
        stateless=True,
        json_response=True,
        security_settings=_security_settings(bind_host, allowed_hosts or []),
    )

    async def healthz(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "auth": auth.enabled})

    routes: list[Any] = [Route("/healthz", healthz, methods=["GET"])]
    middleware: list[Middleware] = []
    endpoint: Any = _McpEndpoint(manager)

    if auth.enabled:
        resource_metadata_url = None
        if auth.entra_enabled and auth.public_url:
            resource_url = AnyHttpUrl(f"{auth.public_url}/mcp")
            routes += create_protected_resource_routes(
                resource_url=resource_url,
                authorization_servers=[
                    AnyHttpUrl(f"https://login.microsoftonline.com/{auth.entra_tenant_id}/v2.0")
                ],
                scopes_supported=auth.required_scopes or None,
                resource_name="SSIS to ADF agent",
            )
            resource_metadata_url = build_resource_metadata_url(resource_url)
        endpoint = RequireAuthMiddleware(endpoint, auth.required_scopes, resource_metadata_url)
        middleware = [
            Middleware(_ApiKeyHeaderMiddleware),
            Middleware(
                AuthenticationMiddleware,
                backend=BearerAuthBackend(verifier or MultiTokenVerifier(auth)),
            ),
            Middleware(AuthContextMiddleware),
        ]

    routes.append(Route("/mcp", endpoint=endpoint))

    @contextlib.asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        async with manager.run():
            yield

    return Starlette(routes=routes, middleware=middleware, lifespan=lifespan)
