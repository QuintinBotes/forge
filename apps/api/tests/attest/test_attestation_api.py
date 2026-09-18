"""Integration tests for the Attested Changesets read-only REST surface
(Task 19: ``GET /attestations``, ``GET /attestations/{id}``,
``GET /approvals/{id}/attestation``).

Mints real records through :class:`AttestationService` (the same path the
``pr``-gate approval hook uses — mirroring ``test_attestation_service.py``'s
seeding) on hermetic SQLite (mirrors ``test_red_team_api.py``'s convention: the
Postgres immutability trigger is a no-op here; the append-only property itself
is proven by the service tests). ``verified`` must be computed by the exact
verification path ``forge-verify --run`` uses — re-derive ``payload_hash`` from
the envelope's PAE, then Ed25519-verify against the deployment key — so a
record signed by a *different* key honestly reads ``verified: false``.
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.orm import Session, sessionmaker

from forge_api.db import get_db
from forge_api.deps import Principal, get_current_principal
from forge_api.main import create_app
from forge_api.services.approval_service import get_approval_service
from forge_api.services.attestation_service import AttestationService
from forge_approval import (
    ApprovalAuthorizer,
    ApprovalService,
    GateRegistry,
    InMemoryApprovalRepository,
)
from forge_contracts import UserRole
from forge_db.base import Base
from forge_db.models import AgentRun, Attestation, Project, Task, WorkflowRun, Workspace
from forge_obs.attest.signing import DsseSigner, EnvSigningKeyProvider

WS = uuid.UUID("00000000-0000-0000-0000-0000000000e1")
WS2 = uuid.UUID("00000000-0000-0000-0000-0000000000e2")

#: Fixed 32-byte Ed25519 seeds → deterministic, silent signers. ``_SEED_B64`` is
#: also exported as ``FORGE_ATTEST_SIGNING_KEY`` (autouse below), so the REST
#: surface's env-fallback verification key matches records minted with it —
#: and does NOT match records minted with ``_OTHER_SEED_B64``.
_SEED_B64 = base64.b64encode(bytes(range(1, 33))).decode("ascii")
_OTHER_SEED_B64 = base64.b64encode(bytes(range(33, 65))).decode("ascii")


@pytest.fixture(autouse=True)
def _deployment_signing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORGE_ATTEST_SIGNING_KEY", _SEED_B64)


@pytest.fixture
def signer() -> DsseSigner:
    return DsseSigner(EnvSigningKeyProvider(environ={"FORGE_ATTEST_SIGNING_KEY": _SEED_B64}))


@pytest.fixture
def other_signer() -> DsseSigner:
    return DsseSigner(EnvSigningKeyProvider(environ={"FORGE_ATTEST_SIGNING_KEY": _OTHER_SEED_B64}))


@pytest.fixture
def factory() -> Iterator[sessionmaker[Session]]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    sf: sessionmaker[Session] = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with sf() as s:
        s.add(Workspace(id=WS, name="Acme", slug="acme"))
        s.add(Workspace(id=WS2, name="Rival", slug="rival"))
        s.commit()
    yield sf
    engine.dispose()


def _seed_run(factory: sessionmaker[Session], *, workspace_id: uuid.UUID = WS) -> uuid.UUID:
    """Seed workspace -> project -> task -> workflow_run + agent_run; return run id."""
    with factory() as s:
        project = Project(workspace_id=workspace_id, name="Core", key=f"C{uuid.uuid4().hex[:4]}")
        s.add(project)
        s.flush()
        task = Task(
            workspace_id=workspace_id,
            project_id=project.id,
            key=f"TASK-{uuid.uuid4().hex[:6]}",
            title="attested changeset task",
        )
        s.add(task)
        s.flush()
        run = WorkflowRun(workspace_id=workspace_id, task_id=task.id)
        s.add(run)
        s.flush()
        s.add(
            AgentRun(
                workspace_id=workspace_id,
                workflow_run_id=run.id,
                task_id=task.id,
                role="implementer",
                model="claude-sonnet-4-5",
                sandbox_kind="gvisor",
                steps=[{"index": 0, "kind": "tool_call", "tool_call": {"tool": "edit_file"}}],
                output={"artifacts": {"model_usage": {"version": "2025-09-29"}}},
            )
        )
        s.commit()
        run_id = run.id
    return run_id


def _mint(
    factory: sessionmaker[Session],
    signer: DsseSigner,
    run_id: uuid.UUID,
    *,
    pr_numbers: list[int] | None = None,
    created_at: datetime | None = None,
) -> uuid.UUID:
    """Mint one attestation via the real service (the approval hook's path)."""
    with factory() as s:
        row = AttestationService(s, signer=signer).attest_changeset(
            run_id, pr_numbers=pr_numbers if pr_numbers is not None else [7, 9]
        )
        if created_at is not None:
            # Deterministic ordering on SQLite (second-resolution timestamps);
            # the Postgres trigger forbidding this is proven by the service tests.
            s.query(Attestation).filter(Attestation.id == row.id).update({"created_at": created_at})
        s.commit()
        att_id = row.id
    return att_id


def _principal(workspace_id: uuid.UUID = WS) -> Principal:
    return Principal(
        user_id=uuid.uuid4(),
        workspace_id=workspace_id,
        role=UserRole.MEMBER,
        email="member@acme.test",
        auth_method="test",
        scopes=["*"],
    )


def _client(
    factory: sessionmaker[Session],
    principal: Principal,
    approval_service: ApprovalService | None = None,
) -> TestClient:
    app: FastAPI = create_app()

    def _get_db() -> Iterator[Session]:
        with factory() as session:
            yield session

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_current_principal] = lambda: principal
    if approval_service is not None:
        app.dependency_overrides[get_approval_service] = lambda: approval_service
    return TestClient(app)


def _approval_service() -> ApprovalService:
    """A hermetic approval service (no resolution hooks — read/create only)."""
    return ApprovalService(InMemoryApprovalRepository(), GateRegistry(), ApprovalAuthorizer())


# --------------------------------------------------------------------------- #
# GET /attestations (list)                                                    #
# --------------------------------------------------------------------------- #


def test_list_returns_minted_record_with_verified_true(factory, signer) -> None:
    run_id = _seed_run(factory)
    att_id = _mint(factory, signer, run_id)
    client = _client(factory, _principal())

    resp = client.get("/attestations")
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert len(items) == 1
    item = items[0]

    assert item["id"] == str(att_id)
    assert item["changeset_hash"].startswith("sha256:")
    assert item["keyid"] == signer.keyid
    assert item["verified"] is True
    assert item["created_at"] is not None
    assert item["predicate_type"].startswith("https://")
    # sha256 hex of the PAE-encoded payload (64 hex chars).
    assert len(item["payload_hash"]) == 64
    # Provenance mirrors the queryable model columns, truthfully degraded
    # (no traceability seeded -> spec_key "" / spec_version 0).
    prov = item["provenance"]
    assert prov["workflow_run_id"] == str(run_id)
    assert prov["agent_run_id"] is not None
    assert prov["pr_numbers"] == [7, 9]
    assert prov["spec_key"] == ""
    assert prov["spec_version"] == 0
    assert prov["audit_seq"] is not None


def test_list_is_newest_first_and_paginates(factory, signer) -> None:
    now = datetime.now(UTC)
    run_ids = [_seed_run(factory) for _ in range(3)]
    att_ids = [
        _mint(factory, signer, run_id, created_at=now + timedelta(minutes=i))
        for i, run_id in enumerate(run_ids)
    ]
    client = _client(factory, _principal())

    resp = client.get("/attestations", params={"limit": 2})
    assert resp.status_code == 200, resp.text
    page_one = resp.json()["items"]
    assert [i["id"] for i in page_one] == [str(att_ids[2]), str(att_ids[1])]

    resp = client.get("/attestations", params={"limit": 2, "offset": 2})
    assert resp.status_code == 200, resp.text
    page_two = resp.json()["items"]
    assert [i["id"] for i in page_two] == [str(att_ids[0])]


def test_list_filters_by_workflow_run_id(factory, signer) -> None:
    run_a = _seed_run(factory)
    run_b = _seed_run(factory)
    att_a = _mint(factory, signer, run_a)
    _mint(factory, signer, run_b)
    client = _client(factory, _principal())

    resp = client.get("/attestations", params={"workflow_run_id": str(run_a)})
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert [i["id"] for i in items] == [str(att_a)]


def test_list_never_leaks_cross_workspace_records(factory, signer) -> None:
    """The row-level workspace scope, not record existence, is the boundary."""
    own_run = _seed_run(factory, workspace_id=WS)
    foreign_run = _seed_run(factory, workspace_id=WS2)
    own_att = _mint(factory, signer, own_run)
    foreign_att = _mint(factory, signer, foreign_run)

    resp = _client(factory, _principal(workspace_id=WS)).get("/attestations")
    assert resp.status_code == 200, resp.text
    assert [i["id"] for i in resp.json()["items"]] == [str(own_att)]

    resp = _client(factory, _principal(workspace_id=WS2)).get("/attestations")
    assert resp.status_code == 200, resp.text
    assert [i["id"] for i in resp.json()["items"]] == [str(foreign_att)]


def test_list_verification_failure_is_reported_honestly(factory, other_signer) -> None:
    """A record signed by a key that is NOT the deployment's verification key
    reads ``verified: false`` — never a fake pass."""
    run_id = _seed_run(factory)
    _mint(factory, other_signer, run_id)
    client = _client(factory, _principal())

    resp = client.get("/attestations")
    assert resp.status_code == 200, resp.text
    item = resp.json()["items"][0]
    assert item["keyid"] == other_signer.keyid
    assert item["verified"] is False


# --------------------------------------------------------------------------- #
# GET /attestations/{id} (detail)                                             #
# --------------------------------------------------------------------------- #


def test_detail_returns_one_record(factory, signer) -> None:
    run_id = _seed_run(factory)
    att_id = _mint(factory, signer, run_id)
    client = _client(factory, _principal())

    resp = client.get(f"/attestations/{att_id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == str(att_id)
    assert body["verified"] is True
    assert body["provenance"]["workflow_run_id"] == str(run_id)


def test_detail_unknown_id_404s(factory) -> None:
    client = _client(factory, _principal())
    resp = client.get(f"/attestations/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_detail_foreign_workspace_id_404s(factory, signer) -> None:
    """A cross-workspace id looks nonexistent (no existence leak)."""
    run_id = _seed_run(factory, workspace_id=WS)
    att_id = _mint(factory, signer, run_id)

    resp = _client(factory, _principal(workspace_id=WS2)).get(f"/attestations/{att_id}")
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# GET /approvals/{id}/attestation (by-approval lookup)                        #
# --------------------------------------------------------------------------- #


def _open_pr_gate(
    client: TestClient, *, workflow_run_id: uuid.UUID | None, subject_id: uuid.UUID | None = None
) -> str:
    resp = client.post(
        "/approvals",
        json={
            "gate_type": "pr",
            "subject_type": "workflow_run",
            "subject_id": str(subject_id or workflow_run_id or uuid.uuid4()),
            "workflow_run_id": str(workflow_run_id) if workflow_run_id else None,
            "title": "PR gate",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def test_by_approval_returns_the_runs_attestation(factory, signer) -> None:
    run_id = _seed_run(factory)
    att_id = _mint(factory, signer, run_id)
    client = _client(factory, _principal(), approval_service=_approval_service())
    approval_id = _open_pr_gate(client, workflow_run_id=run_id)

    resp = client.get(f"/approvals/{approval_id}/attestation")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == str(att_id)
    assert body["verified"] is True
    assert body["provenance"]["workflow_run_id"] == str(run_id)


def test_by_approval_404_when_gate_has_no_workflow_run(factory) -> None:
    client = _client(factory, _principal(), approval_service=_approval_service())
    approval_id = _open_pr_gate(client, workflow_run_id=None)

    resp = client.get(f"/approvals/{approval_id}/attestation")
    assert resp.status_code == 404


def test_by_approval_404_when_run_is_unattested(factory) -> None:
    run_id = _seed_run(factory)
    client = _client(factory, _principal(), approval_service=_approval_service())
    approval_id = _open_pr_gate(client, workflow_run_id=run_id)

    resp = client.get(f"/approvals/{approval_id}/attestation")
    assert resp.status_code == 404


def test_by_approval_unknown_approval_404s(factory) -> None:
    client = _client(factory, _principal(), approval_service=_approval_service())
    resp = client.get(f"/approvals/{uuid.uuid4()}/attestation")
    assert resp.status_code == 404


def test_by_approval_cross_workspace_404s(factory, signer) -> None:
    """A foreign workspace's approval id looks nonexistent — same contract as
    the approvals router itself."""
    run_id = _seed_run(factory, workspace_id=WS)
    _mint(factory, signer, run_id)
    service = _approval_service()
    owner_client = _client(factory, _principal(workspace_id=WS), approval_service=service)
    approval_id = _open_pr_gate(owner_client, workflow_run_id=run_id)

    foreign_client = _client(factory, _principal(workspace_id=WS2), approval_service=service)
    resp = foreign_client.get(f"/approvals/{approval_id}/attestation")
    assert resp.status_code == 404
