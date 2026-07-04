"""Security response headers for the API edge (HARD-09).

Adds the conservative header set every JSON API response should carry:
HSTS, ``X-Content-Type-Options: nosniff``, ``X-Frame-Options: DENY``, a
no-referrer policy, and a deny-everything ``Content-Security-Policy``.

The CSP is skipped on the OpenAPI doc pages (``/docs``/``/redoc``) because
Swagger UI legitimately loads scripts; those pages are disabled in production
anyway (see ``forge_api.main.create_app``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

__all__ = ["SecurityHeadersMiddleware"]

_DOC_PATHS = ("/docs", "/redoc")

_BASE_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (b"strict-transport-security", b"max-age=63072000; includeSubDomains"),
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
)

_CSP_HEADER: tuple[bytes, bytes] = (
    b"content-security-policy",
    b"default-src 'none'; frame-ancestors 'none'; sandbox",
)


class SecurityHeadersMiddleware:
    """ASGI middleware stamping the security header set on every response."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        skip_csp = any(path.startswith(doc) for doc in _DOC_PATHS)

        async def sending(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {name.lower() for name, _ in headers}
                for name, value in _BASE_HEADERS:
                    if name not in present:
                        headers.append((name, value))
                if not skip_csp and _CSP_HEADER[0] not in present:
                    headers.append(_CSP_HEADER)
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, sending)
