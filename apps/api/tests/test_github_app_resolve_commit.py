"""A deploy's commit name is resolved as its workspace's own GitHub App (#335).

Only GitHub's HTTP layer is faked (an ``httpx.MockTransport``); the vault is the
API's ``SecretVault`` and the token's audit record lands in a real table.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import StaticPool, create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from scf_api.auth.crypto import FernetCipher
from scf_api.auth.vault import SecretVault
from scf_api.services import github_app
from scf_api.services.github_app import resolve_commit
from scf_contracts.enums import APIKeyKind
from scf_db.base import Base
from scf_db.models import AuditLog
from scf_integrations.github_auth import WORKSPACE_SECRET_NAME

WS = uuid.UUID("00000000-0000-0000-0000-0000000335c1")
HEAD = "3d" * 20


@pytest.fixture
def factory() -> Iterator[sessionmaker[Session]]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    engine.dispose()


@pytest.fixture
def vault(monkeypatch: pytest.MonkeyPatch) -> SecretVault:
    vault = SecretVault(cipher=FernetCipher(b"r" * 32))
    monkeypatch.setattr(github_app, "_workspace_vault", lambda: vault)
    monkeypatch.setattr(github_app, "_development", lambda: False)
    return vault


def _store(vault: SecretVault) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    vault.put_secret(
        workspace_id=WS,
        name=WORKSPACE_SECRET_NAME,
        secret=json.dumps({"app_id": "7", "installation_id": "70", "private_key_pem": pem}),
        kind=APIKeyKind.INTEGRATION_TOKEN,
    )


class FakeGitHub:
    """Mints a token and answers a commit lookup with the full hash ``main`` points at."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/app/installations/70/access_tokens":
            return httpx.Response(201, json={"token": "ghs_" + "r" * 36, "expires_at": None})
        if request.url.path == "/repos/org/api/commits/main":
            return httpx.Response(200, text=HEAD)
        return httpx.Response(404, json={"message": "Not Found"})


def test_it_asks_github_with_a_token_that_reads_one_repository(
    factory: sessionmaker[Session], vault: SecretVault
) -> None:
    _store(vault)
    fake = FakeGitHub()

    full = resolve_commit(
        WS,
        "github.com/org/api",
        "main",
        session_factory=factory,
        transport=httpx.MockTransport(fake.handler),
    )

    assert full == HEAD
    mint, lookup = fake.requests
    assert json.loads(mint.content) == {
        "repositories": ["api"],
        "permissions": {"contents": "read"},
    }
    assert lookup.headers["authorization"] == "Bearer ghs_" + "r" * 36
    with factory() as s:
        audit = s.scalars(select(AuditLog).where(AuditLog.action == "github.token_issued")).one()
    assert audit.workspace_id == WS
    assert audit.details["repository"] == "org/api"
    assert audit.details["permissions"] == {"contents": "read"}


def test_a_workspace_without_credentials_asks_nothing(
    factory: sessionmaker[Session], vault: SecretVault
) -> None:
    fake = FakeGitHub()

    full = resolve_commit(
        WS, "org/api", "main", session_factory=factory, transport=httpx.MockTransport(fake.handler)
    )

    assert full is None
    assert fake.requests == []
