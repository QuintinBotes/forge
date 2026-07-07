"""API tests for the F36 unified approvals router (``/approvals``).

Exercises the real handlers against a fresh ``forge_approval.ApprovalService``
(in-memory repository, real deploy + policy_override providers, real
authorizer, recording bus) injected via DI — the same composition the
process-wide singleton performs.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from forge_api.deps import Principal
from forge_api.main import create_app
from forge_api.observability.redaction import redact_mapping
from forge_api.services.approval_service import (
    build_gate_registry,
    get_approval_service,
)
from forge_approval import (
    ApprovalAuthorizer,
    ApprovalService,
    InMemoryActivityBus,
    InMemoryApprovalRepository,
)
from forge_approval.providers import InMemoryGrantStore, action_fingerprint
from forge_contracts import UserRole

OTHER_WORKSPACE = uuid.UUID("00000000-0000-0000-0000-0000000000f9")

# Deterministic identities (the tests dir is not an importable package, so
# these mirror conftest rather than importing it).
TEST_WORKSPACE_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
TEST_USER_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b2")


def make_test_principal(
    *,
    role: UserRole = UserRole.ADMIN,
    workspace_id: uuid.UUID = TEST_WORKSPACE_ID,
) -> Principal:
    return Principal(
        user_id=TEST_USER_ID,
        workspace_id=workspace_id,
        role=role,
        email="test-principal@forge.local",
        auth_method="test",
        scopes=["*"],
    )


@pytest.fixture
def grant_store() -> InMemoryGrantStore:
    return InMemoryGrantStore()


@pytest.fixture
def bus() -> InMemoryActivityBus:
    return InMemoryActivityBus()


@pytest.fixture
def service(grant_store: InMemoryGrantStore, bus: InMemoryActivityBus) -> ApprovalService:
    return ApprovalService(
        InMemoryApprovalRepository(),
        build_gate_registry(grant_store),
        ApprovalAuthorizer(),
        events=bus,
        redactor=redact_mapping,
    )


@pytest.fixture
def make_client(
    authenticate_app: Callable[..., FastAPI], service: ApprovalService
) -> Iterator[Callable[..., TestClient]]:
    """Factory building a client authenticated with a chosen role/workspace."""
    stack: list[TestClient] = []

    def _make(
        role: UserRole = UserRole.ADMIN,
        workspace_id: uuid.UUID = TEST_WORKSPACE_ID,
    ) -> TestClient:
        app = create_app()
        authenticate_app(app, make_test_principal(role=role, workspace_id=workspace_id))
        app.dependency_overrides[get_approval_service] = lambda: service
        client = TestClient(app)
        client.__enter__()
        stack.append(client)
        return client

    yield _make
    for client in stack:
        client.__exit__(None, None, None)


def _create(
    client: TestClient,
    gate_type: str = "pr",
    **over: object,
) -> dict:
    body: dict = {
        "gate_type": gate_type,
        "subject_type": "workflow_run",
        "subject_id": str(uuid.uuid4()),
        "requested_actor": f"agent:{uuid.uuid4()}",
        "title": f"{gate_type} gate",
    }
    body.update(over)
    resp = client.post("/approvals", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_create_get_and_context(make_client: Callable[..., TestClient]) -> None:
    """AC#1/#3: create persists; context comes from the registered provider."""
    client = make_client()
    created = _create(
        client,
        "deploy",
        gate_payload={"environment": "production", "restricted_environment": True},
    )
    assert created["status"] == "pending"
    assert created["gate_type"] == "deploy"

    fetched = client.get(f"/approvals/{created['id']}")
    assert fetched.status_code == 200

    context = client.get(f"/approvals/{created['id']}/context")
    assert context.status_code == 200
    body = context.json()
    assert any(flag["category"] == "restricted_env" for flag in body["risk_flags"])
    assert "escalate" not in body["available_actions"]


def test_create_idempotent_while_pending(make_client: Callable[..., TestClient]) -> None:
    """AC#2: duplicate create returns the same pending gate."""
    client = make_client()
    subject_id = str(uuid.uuid4())
    first = _create(client, subject_id=subject_id)
    second = _create(client, subject_id=subject_id)
    assert second["id"] == first["id"]


def test_inbox_scoped_and_sorted(make_client: Callable[..., TestClient]) -> None:
    """AC#18: critical first; filters work; count matches the inbox."""
    client = make_client()
    _create(client, "pr", risk_level="info")
    _create(client, "policy_override", risk_level="critical")
    _create(client, "deploy", risk_level="warning")

    inbox = client.get("/approvals", params={"status": "pending"})
    assert inbox.status_code == 200
    assert [row["risk_level"] for row in inbox.json()] == ["critical", "warning", "info"]

    by_gate = client.get("/approvals", params={"gate_type": "deploy"})
    assert [row["gate_type"] for row in by_gate.json()] == ["deploy"]

    count = client.get("/approvals/count", params={"status": "pending"})
    assert count.json()["count"] == 3


def test_mine_filter_excludes_unresolvable(make_client: Callable[..., TestClient]) -> None:
    """AC#18: mine=true keeps only gates the actor may resolve."""
    admin = make_client(role=UserRole.ADMIN)
    _create(admin, "pr")
    _create(admin, "policy_override", risk_level="critical")

    member = make_client(role=UserRole.MEMBER)
    mine = member.get("/approvals", params={"mine": "true", "status": "pending"})
    assert {row["gate_type"] for row in mine.json()} == {"pr"}
    count = member.get("/approvals/count", params={"mine": "true"})
    assert count.json()["count"] == 1


def test_cross_workspace_404(make_client: Callable[..., TestClient]) -> None:
    """AC#16: another workspace's gate id looks nonexistent (never 403)."""
    owner = make_client()
    created = _create(owner)

    intruder = make_client(workspace_id=OTHER_WORKSPACE)
    for path in (
        f"/approvals/{created['id']}",
        f"/approvals/{created['id']}/context",
        f"/approvals/{created['id']}/decisions",
    ):
        assert intruder.get(path).status_code == 404
    denied = intruder.post(f"/approvals/{created['id']}/decision", json={"decision": "approve"})
    assert denied.status_code == 404

    inbox = intruder.get("/approvals")
    assert inbox.json() == []


def test_decision_authz_matrix(make_client: Callable[..., TestClient]) -> None:
    """AC#5/#6: viewer 403 (via the domain authorizer), agent-runner 403,
    member 403 on policy_override, admin 200."""
    admin = make_client(role=UserRole.ADMIN)
    override = _create(admin, "policy_override")
    pr = _create(admin, "pr")

    viewer = make_client(role=UserRole.VIEWER)
    resp = viewer.post(f"/approvals/{pr['id']}/decision", json={"decision": "approve"})
    assert resp.status_code == 403

    agent = make_client(role=UserRole.AGENT_RUNNER)
    resp = agent.post(f"/approvals/{pr['id']}/decision", json={"decision": "approve"})
    assert resp.status_code == 403

    member = make_client(role=UserRole.MEMBER)
    resp = member.post(f"/approvals/{override['id']}/decision", json={"decision": "approve"})
    assert resp.status_code == 403
    assert "admin" in resp.json()["detail"]

    resp = admin.post(f"/approvals/{pr['id']}/decision", json={"decision": "approve"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "approved"


def test_already_resolved_and_duplicate_vote_409(
    make_client: Callable[..., TestClient],
) -> None:
    client = make_client()
    created = _create(client)
    first = client.post(f"/approvals/{created['id']}/decision", json={"decision": "approve"})
    assert first.status_code == 200
    second = client.post(f"/approvals/{created['id']}/decision", json={"decision": "reject"})
    assert second.status_code == 409


def test_approve_blocked_returns_reasons(
    make_client: Callable[..., TestClient], service: ApprovalService
) -> None:
    """AC#11 (F08 approve-but-CI-red regression lock): status approved,
    completed False, blocking_reasons surfaced — no advance."""
    from forge_approval.models import (
        ApprovalDecisionRequest as _Dec,  # noqa: F401 (type reference)
    )
    from forge_approval.models import GateType, ResolutionOutcome

    class BlockedPrHook:
        gate_type = GateType.PR

        async def on_resolved(self, request, decision, actor, *, session=None):
            return ResolutionOutcome(
                completed=False,
                blocking_reasons=["CI status is failure (1 of 3 checks)"],
            )

    client = make_client()
    created = _create(client)
    service._registry.register_hook(BlockedPrHook())
    resp = client.post(f"/approvals/{created['id']}/decision", json={"decision": "approve"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "approved"
    assert body["outcome"]["completed"] is False
    assert body["outcome"]["blocking_reasons"] == ["CI status is failure (1 of 3 checks)"]
    assert body["outcome"]["follow_up_state"] is None  # no review_approved emitted


def test_policy_override_approve_mints_consumable_grant(
    make_client: Callable[..., TestClient], grant_store: InMemoryGrantStore
) -> None:
    """AC#14 end-to-end over HTTP: approve mints the single-use grant."""
    client = make_client(role=UserRole.ADMIN)
    agent_run_id = uuid.uuid4()
    call = {"tool": "shell", "action": "restricted_action"}
    fingerprint = action_fingerprint(call)
    created = _create(
        client,
        "policy_override",
        subject_type="agent_run",
        subject_id=str(agent_run_id),
        agent_run_id=str(agent_run_id),
        risk_level="critical",
        gate_payload={
            "action": call,
            "blocked_by": ["restricted_actions"],
            "action_fingerprint": fingerprint,
        },
    )
    resp = client.post(f"/approvals/{created['id']}/decision", json={"decision": "approve"})
    assert resp.status_code == 200
    details = resp.json()["outcome"]["details"]
    assert details["signal"] == "policy_override.granted"
    assert details["single_use"] is True

    import asyncio

    async def consume_twice() -> tuple[bool, bool]:
        first = await grant_store.consume(agent_run_id=agent_run_id, action_fingerprint=fingerprint)
        second = await grant_store.consume(
            agent_run_id=agent_run_id, action_fingerprint=fingerprint
        )
        return first, second

    first, second = asyncio.run(consume_twice())
    assert first is True
    assert second is False


def test_escalate_keeps_pending_and_requires_admin(
    make_client: Callable[..., TestClient],
) -> None:
    """AC#13 over HTTP: escalate -> pending + critical; member then refused."""
    member = make_client(role=UserRole.MEMBER)
    admin = make_client(role=UserRole.ADMIN)
    created = _create(admin, "incident_remediation")

    resp = member.post(f"/approvals/{created['id']}/decision", json={"decision": "escalate"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "pending"

    stored = admin.get(f"/approvals/{created['id']}").json()
    assert stored["risk_level"] == "critical"

    # NOTE: member and admin share TEST_USER_ID in this suite, so the admin
    # retry below also exercises the duplicate-vote guard when ids collide;
    # use the authz refusal assertion on the member only.
    refused = member.post(f"/approvals/{created['id']}/decision", json={"decision": "approve"})
    assert refused.status_code == 403


def test_decision_body_cannot_forge_decider(make_client: Callable[..., TestClient]) -> None:
    """The resolver identity comes from the authenticated principal only."""
    client = make_client()
    created = _create(client)
    resp = client.post(
        f"/approvals/{created['id']}/decision",
        json={"decision": "approve", "resolver_user_id": str(uuid.uuid4())},
    )
    assert resp.status_code == 200
    stored = client.get(f"/approvals/{created['id']}").json()
    assert stored["resolver_user_id"] == str(TEST_USER_ID)


def test_gate_payload_secret_redaction(make_client: Callable[..., TestClient]) -> None:
    """AC#17: secret-shaped values never survive into the stored gate."""
    client = make_client()
    created = _create(
        client,
        gate_payload={"api_key": "sk-supersecret1234567890", "note": "plain"},
    )
    assert created["gate_payload"]["api_key"] == "[REDACTED]"
    assert created["gate_payload"]["note"] == "plain"


def test_expired_gates_swept(
    make_client: Callable[..., TestClient], service: ApprovalService
) -> None:
    """SLA sweep marks overdue pending gates expired (surfaced via the API)."""
    import asyncio

    client = make_client()
    created = _create(client, expires_at=(datetime.now(UTC) - timedelta(minutes=1)).isoformat())
    asyncio.run(service.expire_pending())
    stored = client.get(f"/approvals/{created['id']}").json()
    assert stored["status"] == "expired"
