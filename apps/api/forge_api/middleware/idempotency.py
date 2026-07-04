"""Request idempotency middleware (HARD-11).

A client (or the web app on a flaky connection) may retry an unsafe request. When
the retry carries the same ``Idempotency-Key`` header, the second call must
return the *same* response the first produced and must not run the side effect
twice — no duplicate sync runs, no duplicate agent-run enqueues.

Scope: unsafe methods (``POST``/``PUT``/``PATCH``/``DELETE``) that carry an
``Idempotency-Key``. The key is tenant-scoped by the presented credential (the
same scoping the rate limiter uses; the raw credential is hashed, never stored).
A replay of the same key with a *different* body is a client bug → ``422``.

The default store is in-process (dev/test, no Redis); production supplies a
Redis-backed store. Everything is default-on but no-ops when disabled or when no
``Idempotency-Key`` is present.
"""

from __future__ import annotations

import hashlib
import threading
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

__all__ = [
    "IdempotencyMiddleware",
    "IdempotencyStore",
    "InMemoryIdempotencyStore",
    "StoredResponse",
]

_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_HEADER = b"idempotency-key"


class StoredResponse(BaseModel):
    """A cached response keyed by an idempotency token."""

    request_hash: str
    status_code: int
    body: bytes
    content_type: str = "application/json"
    created_at: datetime


class IdempotencyStore(Protocol):
    """A tenant-scoped idempotency-key → response store."""

    def get(self, key: str) -> StoredResponse | None: ...

    def put_if_absent(self, key: str, value: StoredResponse, ttl_s: int) -> bool:
        """Store ``value`` iff ``key`` is absent; return ``True`` when it wrote."""


class InMemoryIdempotencyStore:
    """Process-local, TTL-aware idempotency store (dev/test; no Redis)."""

    def __init__(self) -> None:
        self._data: dict[str, tuple[StoredResponse, float]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> StoredResponse | None:
        now = time.monotonic()
        with self._lock:
            self._evict(now)
            entry = self._data.get(key)
            return entry[0] if entry else None

    def put_if_absent(self, key: str, value: StoredResponse, ttl_s: int) -> bool:
        now = time.monotonic()
        with self._lock:
            self._evict(now)
            if key in self._data:
                return False
            self._data[key] = (value, now + max(1, ttl_s))
            return True

    def _evict(self, now: float) -> None:
        for k in [k for k, (_, exp) in self._data.items() if exp <= now]:
            del self._data[k]


def _tenant_scope(scope: Scope) -> str:
    """Stable per-credential scope (hashed); anonymous falls back to the IP."""
    for name, value in scope.get("headers", []):
        if name in (b"authorization", b"x-api-key"):
            return "cred:" + hashlib.sha256(value).hexdigest()[:16]
    client = scope.get("client")
    return f"ip:{client[0]}" if client else "ip:unknown"


async def _read_body(receive: Receive) -> bytes:
    chunks: list[bytes] = []
    more = True
    while more:
        message = await receive()
        if message["type"] == "http.request":
            chunks.append(message.get("body", b""))
            more = message.get("more_body", False)
        else:  # http.disconnect
            more = False
    return b"".join(chunks)


def _replay_receive(body: bytes) -> Receive:
    sent = False

    async def receive() -> Message:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    return receive


class IdempotencyMiddleware:
    """ASGI middleware collapsing retries of an ``Idempotency-Key`` request."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        store: IdempotencyStore | None = None,
        ttl_s: int = 86_400,
        methods: frozenset[str] = _UNSAFE_METHODS,
        enabled: bool = True,
    ) -> None:
        self.app = app
        self.store = store if store is not None else InMemoryIdempotencyStore()
        self.ttl_s = ttl_s
        self.methods = methods
        self.enabled = enabled

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            not self.enabled
            or scope["type"] != "http"
            or scope.get("method", "GET").upper() not in self.methods
        ):
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        idem_header = headers.get(_HEADER)
        if not idem_header:
            await self.app(scope, receive, send)
            return

        body = await _read_body(receive)
        request_hash = hashlib.sha256(
            b"|".join(
                [
                    scope.get("method", "").encode(),
                    scope.get("path", "").encode(),
                    _tenant_scope(scope).encode(),
                    body,
                ]
            )
        ).hexdigest()
        key = f"forge:idem:{_tenant_scope(scope)}:{idem_header.decode('latin-1')}"

        cached = self.store.get(key)
        if cached is not None:
            if cached.request_hash != request_hash:
                await _send_json(
                    send,
                    422,
                    b'{"detail":"Idempotency-Key reused with a different request body"}',
                )
                return
            await _send_stored(send, cached)
            return

        # First sight: run the handler, capturing the response to cache it.
        status_code = 500
        resp_headers: list[tuple[bytes, bytes]] = []
        resp_body = bytearray()

        async def capture_send(message: Message) -> None:
            nonlocal status_code, resp_headers
            if message["type"] == "http.response.start":
                status_code = message["status"]
                resp_headers = list(message.get("headers", []))
            elif message["type"] == "http.response.body":
                resp_body.extend(message.get("body", b""))
            await send(message)

        await self.app(scope, _replay_receive(body), capture_send)

        # Cache anything that is not a server error so a later retry replays it;
        # 5xx is left uncached so the client can genuinely re-drive the work.
        if status_code < 500:
            content_type = "application/json"
            for name, value in resp_headers:
                if name.lower() == b"content-type":
                    content_type = value.decode("latin-1")
                    break
            self.store.put_if_absent(
                key,
                StoredResponse(
                    request_hash=request_hash,
                    status_code=status_code,
                    body=bytes(resp_body),
                    content_type=content_type,
                    created_at=datetime.now(UTC),
                ),
                self.ttl_s,
            )


async def _send_json(send: Send, status: int, body: bytes) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


async def _send_stored(send: Send, stored: StoredResponse) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": stored.status_code,
            "headers": [
                (b"content-type", stored.content_type.encode("latin-1")),
                (b"content-length", str(len(stored.body)).encode()),
                (b"idempotency-replayed", b"true"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": stored.body})
