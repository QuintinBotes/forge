"""Replay-protection stores for SAML (F33 §3.1).

Two implementations of the frozen :class:`forge_contracts.sso.ReplayGuard`
Protocol:

* :class:`InMemoryReplayGuard` — the process-local default (also the test
  fake). Sufficient for single-process deployments and hermetic suites.
* :class:`DbReplayGuard` — the Postgres fallback documented for deployments
  running without Redis, backed by the ``saml_replay`` table; expired rows are
  evicted by the worker's ``cleanup_saml_replay`` beat task.

A Redis-backed guard (``saml:assertion:{id}`` / ``saml:authnreq:{id}`` with
TTL) is the documented production store for multi-process deployments; it is
PARKED until a slice runs against a live Redis (no fakes for infrastructure).
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from forge_db.models import SamlReplay

_REQUEST_PREFIX = "authnreq:"
_ASSERTION_PREFIX = "assertion:"


class InMemoryReplayGuard:
    """Process-local one-time stores with TTL expiry (monotonic clock)."""

    def __init__(self) -> None:
        self._requests: dict[str, float] = {}
        self._assertions: dict[str, float] = {}

    def _prune(self, store: dict[str, float]) -> None:
        now = time.monotonic()
        for key in [k for k, exp in store.items() if exp <= now]:
            del store[key]

    def register_request(self, request_id: str, ttl_seconds: int) -> None:
        self._prune(self._requests)
        self._requests[request_id] = time.monotonic() + ttl_seconds

    def consume_request(self, request_id: str) -> bool:
        self._prune(self._requests)
        return self._requests.pop(request_id, None) is not None

    def seen_assertion(self, assertion_id: str, ttl_seconds: int) -> bool:
        self._prune(self._assertions)
        if assertion_id in self._assertions:
            return True
        self._assertions[assertion_id] = time.monotonic() + ttl_seconds
        return False


class DbReplayGuard:
    """``saml_replay``-table guard (Postgres/SQLite fallback when Redis absent)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def _now(self) -> datetime:
        return datetime.now(UTC)

    def register_request(self, request_id: str, ttl_seconds: int) -> None:
        key = _REQUEST_PREFIX + request_id
        expires_at = self._now() + timedelta(seconds=ttl_seconds)
        existing = self._session.execute(
            select(SamlReplay).where(SamlReplay.replay_id == key)
        ).scalar_one_or_none()
        if existing is None:
            self._session.add(SamlReplay(replay_id=key, expires_at=expires_at))
        else:
            existing.expires_at = expires_at
        self._session.flush()

    def consume_request(self, request_id: str) -> bool:
        key = _REQUEST_PREFIX + request_id
        row = self._session.execute(
            select(SamlReplay).where(SamlReplay.replay_id == key)
        ).scalar_one_or_none()
        if row is None:
            return False
        expires = row.expires_at
        if expires.tzinfo is None:  # SQLite loses tzinfo
            expires = expires.replace(tzinfo=UTC)
        self._session.execute(delete(SamlReplay).where(SamlReplay.replay_id == key))
        self._session.flush()
        return expires > self._now()

    def seen_assertion(self, assertion_id: str, ttl_seconds: int) -> bool:
        key = _ASSERTION_PREFIX + assertion_id
        existing = self._session.execute(
            select(SamlReplay).where(SamlReplay.replay_id == key)
        ).scalar_one_or_none()
        if existing is not None:
            return True
        try:
            with self._session.begin_nested():
                self._session.add(
                    SamlReplay(
                        replay_id=key,
                        expires_at=self._now() + timedelta(seconds=ttl_seconds),
                    )
                )
                self._session.flush()
        except IntegrityError:
            return True  # concurrent insert == replay
        return False

    def cleanup_expired(self) -> int:
        """Delete expired rows; returns the count (worker beat task)."""
        result = self._session.execute(
            delete(SamlReplay).where(SamlReplay.expires_at <= self._now())
        )
        self._session.flush()
        return int(result.rowcount or 0)


__all__ = ["DbReplayGuard", "InMemoryReplayGuard"]
