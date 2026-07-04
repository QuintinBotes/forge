"""Tests for Forge API-key authentication (Task 1.15 — auth & secrets).

Forge-issued API keys authenticate clients to the Forge API. Only a one-way hash
is stored; the plaintext token is shown exactly once at mint time.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

from forge_api.auth.apikeys import APIKeyInfo, APIKeyStore, generate_api_token
from forge_contracts.enums import APIKeyKind, UserRole


def _store() -> APIKeyStore:
    return APIKeyStore(secret_key=b"0" * 32)


def test_generate_api_token_has_forge_prefix() -> None:
    token = generate_api_token(APIKeyKind.SYSTEM)
    assert token.startswith("forge_")
    assert len(token) > 30


def test_mint_returns_token_once_and_redacted_info() -> None:
    store = _store()
    ws = uuid.uuid4()
    info, token = store.mint(workspace_id=ws, name="ci", role=UserRole.AGENT_RUNNER)
    assert isinstance(info, APIKeyInfo)
    assert token.startswith("forge_")
    # The redacted info must never carry the token or its hash.
    dumped = json.dumps(info.model_dump(mode="json"))
    assert token not in dumped
    assert "token_hash" not in dumped


def test_mint_stores_hash_not_plaintext() -> None:
    store = _store()
    ws = uuid.uuid4()
    _, token = store.mint(workspace_id=ws, name="ci", role=UserRole.MEMBER)
    record = store.verify(token)
    assert record is not None
    assert record.token_hash != token
    assert token not in record.token_hash


def test_verify_roundtrip() -> None:
    store = _store()
    ws = uuid.uuid4()
    _, token = store.mint(workspace_id=ws, name="ci", role=UserRole.ADMIN)
    record = store.verify(token)
    assert record is not None
    assert record.workspace_id == ws
    assert record.role is UserRole.ADMIN


def test_verify_rejects_unknown_token() -> None:
    store = _store()
    store.mint(workspace_id=uuid.uuid4(), name="ci", role=UserRole.MEMBER)
    assert store.verify("forge_system_deadbeefdeadbeefdeadbeef") is None


def test_verify_rejects_tampered_token() -> None:
    store = _store()
    _, token = store.mint(workspace_id=uuid.uuid4(), name="ci", role=UserRole.MEMBER)
    assert store.verify(token + "x") is None


def test_revoked_key_fails_verify() -> None:
    store = _store()
    ws = uuid.uuid4()
    info, token = store.mint(workspace_id=ws, name="ci", role=UserRole.MEMBER)
    store.revoke(ws, info.id)
    assert store.verify(token) is None


def test_expired_key_fails_verify() -> None:
    store = _store()
    ws = uuid.uuid4()
    _, token = store.mint(
        workspace_id=ws,
        name="ephemeral",
        role=UserRole.AGENT_RUNNER,
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    assert store.verify(token) is None


def test_list_is_workspace_scoped_and_redacted() -> None:
    store = _store()
    ws_a, ws_b = uuid.uuid4(), uuid.uuid4()
    store.mint(workspace_id=ws_a, name="a", role=UserRole.MEMBER)
    store.mint(workspace_id=ws_b, name="b", role=UserRole.MEMBER)
    infos = store.list_keys(ws_a)
    assert [i.name for i in infos] == ["a"]
    assert all(not hasattr(i, "token_hash") for i in infos)


def test_verify_updates_last_used() -> None:
    store = _store()
    ws = uuid.uuid4()
    info, token = store.mint(workspace_id=ws, name="ci", role=UserRole.MEMBER)
    assert info.last_used_at is None
    store.verify(token)
    refreshed = next(i for i in store.list_keys(ws) if i.id == info.id)
    assert refreshed.last_used_at is not None
