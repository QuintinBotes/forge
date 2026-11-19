"""Set and clear a workspace's GitHub App credentials (#186).

Role and cross-workspace refusal live in the enforcement matrix
(``tests/security/matrix_checks.py``: ``check_rbac_wired_403``,
``check_tenant_isolation``); this file covers what the route stores and returns.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from scf_api.auth.service import AuthService, get_auth_service
from scf_api.deps import Principal, get_current_principal
from scf_api.main import create_app
from scf_contracts import UserRole
from scf_contracts.audit import AuditEvent
from scf_integrations.github_auth import WORKSPACE_SECRET_NAME

WS = uuid.UUID("00000000-0000-0000-0000-0000000186a1")
ROUTE = "/integration/github/app-credentials"


@pytest.fixture(scope="module")
def pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


class Sink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def emit(self, event: AuditEvent) -> None:
        self.events.append(event)


@pytest.fixture
def sink() -> Sink:
    return Sink()


@pytest.fixture
def service(sink: Sink) -> AuthService:
    return AuthService(secret_key=b"g" * 32, audit_sink=sink)


@pytest.fixture
def client(service: AuthService) -> Iterator[TestClient]:
    app = create_app()
    app.dependency_overrides[get_auth_service] = lambda: service
    app.dependency_overrides[get_current_principal] = lambda: Principal(
        user_id=uuid.uuid4(), workspace_id=WS, role=UserRole.ADMIN, auth_method="test"
    )
    with TestClient(app) as c:
        yield c


def _key_body(pem: str) -> str:
    return pem.split("-----")[2].strip()


def _stored(service: AuthService) -> list[dict[str, str]]:
    return [
        json.loads(service.vault.get_secret(WS, s.id))
        for s in service.vault.list_secrets(WS)
        if s.name == WORKSPACE_SECRET_NAME
    ]


def test_an_admin_sets_the_credentials_and_never_gets_the_key_back(
    client: TestClient, service: AuthService, sink: Sink, pem: str
) -> None:
    resp = client.put(ROUTE, json={"app_id": "11", "installation_id": "111", "private_key": pem})

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"app_id": "11", "installation_id": "111"}
    assert _stored(service) == [{"app_id": "11", "installation_id": "111", "private_key_pem": pem}]
    [event] = sink.events
    assert event.action == "github_app.credentials_set"
    assert _key_body(pem) not in event.model_dump_json()


def test_setting_again_replaces_and_clearing_removes(
    client: TestClient, service: AuthService, pem: str
) -> None:
    client.put(ROUTE, json={"app_id": "11", "installation_id": "111", "private_key": pem})
    client.put(ROUTE, json={"app_id": "12", "private_key": pem})
    assert _stored(service) == [{"app_id": "12", "installation_id": None, "private_key_pem": pem}]

    assert client.delete(ROUTE).status_code == 204
    assert _stored(service) == []
    assert client.delete(ROUTE).status_code == 204, "clearing is idempotent"


def test_a_key_that_is_not_a_private_key_is_refused_without_echoing_it(
    client: TestClient, service: AuthService
) -> None:
    junk = "-----BEGIN PRIVATE KEY-----\nc2VjcmV0LW5vdC1hLWtleQ==\n-----END PRIVATE KEY-----"
    resp = client.put(ROUTE, json={"app_id": "11", "private_key": junk})

    assert resp.status_code == 422
    assert "c2VjcmV0LW5vdC1hLWtleQ" not in resp.text
    assert _stored(service) == []


@pytest.mark.parametrize(
    "body",
    [
        {"app_id": "11", "installation_id": "../x"},
        {"app_id": "11", "installation_id": "12/34"},
        {"app_id": "a b", "installation_id": "1"},
    ],
)
def test_ids_that_could_leave_their_url_segment_are_refused_without_echoing_the_key(
    client: TestClient, service: AuthService, pem: str, body: dict[str, str]
) -> None:
    resp = client.put(ROUTE, json={**body, "private_key": pem})

    assert resp.status_code == 422
    assert _key_body(pem) not in resp.text
    assert _stored(service) == []


def test_the_request_body_never_shows_the_key_in_repr(pem: str) -> None:
    from scf_api.routers.integration import GitHubAppCredentialsIn

    body = GitHubAppCredentialsIn(app_id="11", private_key=pem)
    assert _key_body(pem) not in f"{body!r} {body}"
