"""Agent-runner token auto-expiry tests (HARD-13 AC9).

Spec Security: "automatic expiry for agent tokens". A minted agent token carries
``expires_at = now + FORGE_AGENT_TOKEN_TTL`` (default 24h) and ``apikeys.verify``
rejects it once expired.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from forge_api.auth.service import AuthService
from forge_contracts.enums import UserRole


def _service() -> AuthService:
    return AuthService(secret_key=b"k" * 32)


def test_minted_agent_token_gets_default_ttl() -> None:
    svc = _service()
    info, token = svc.mint_agent_token(
        workspace_id=uuid.uuid4(), name="agent-run", role=UserRole.MEMBER
    )
    assert info.expires_at is not None
    delta = (info.expires_at - datetime.now(UTC)).total_seconds()
    assert 86_000 < delta <= 86_400  # ~24h default
    # A freshly-minted token authenticates.
    assert svc.api_keys.verify(token) is not None


def test_agent_token_ttl_is_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORGE_AGENT_TOKEN_TTL", "3600")
    svc = _service()
    info, _ = svc.mint_agent_token(
        workspace_id=uuid.uuid4(), name="agent-run", role=UserRole.MEMBER
    )
    assert info.expires_at is not None
    delta = (info.expires_at - datetime.now(UTC)).total_seconds()
    assert 3_500 < delta <= 3_600


def test_expired_agent_token_is_rejected_by_verify() -> None:
    svc = _service()
    _, token = svc.mint_agent_token(
        workspace_id=uuid.uuid4(),
        name="stale-agent",
        role=UserRole.MEMBER,
        ttl_seconds=1,
        now=datetime.now(UTC) - timedelta(hours=1),  # expiry already in the past
    )
    assert svc.api_keys.verify(token) is None
