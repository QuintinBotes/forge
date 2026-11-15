"""Request body size bound (HARD-09).

Rejects a declared ``Content-Length`` over the limit immediately with ``413``
and, defensively, caps streamed/chunked bodies by counting bytes as they
arrive — a client that lies about (or omits) ``Content-Length`` is cut off at
the same bound instead of buffering unbounded input into memory.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

__all__ = ["DEFAULT_MAX_BODY_BYTES", "BodySizeLimitMiddleware", "BodyTooLargeError"]

DEFAULT_MAX_BODY_BYTES = 1_048_576  # 1 MiB


class BodyTooLargeError(Exception):
    """Raised mid-stream when a request body exceeds the configured bound."""


async def _send_413(send: Send, max_bytes: int) -> None:
    body = b'{"detail":"request body exceeds the maximum of %d bytes"}' % max_bytes
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class BodySizeLimitMiddleware:
    """ASGI middleware enforcing ``max_bytes`` on request bodies (413 over)."""

    def __init__(self, app: ASGIApp, *, max_bytes: int = DEFAULT_MAX_BODY_BYTES) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declared: int | None = None
        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    declared = int(value.decode("ascii"))
                except ValueError:
                    declared = None
                break
        if declared is not None and declared > self.max_bytes:
            await _send_413(send, self.max_bytes)
            return

        received = 0
        response_started = False

        async def counting_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    # Fail the in-flight read; the handler sees a broken body
                    # rather than an unbounded buffer.
                    raise BodyTooLargeError
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, counting_receive, tracking_send)
        except BodyTooLargeError:
            if not response_started:
                await _send_413(send, self.max_bytes)
            # If the response already started there is nothing safe to send;
            # the server will terminate the connection.
