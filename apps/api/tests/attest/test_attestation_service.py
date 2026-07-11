"""Postgres integration tests for the attestation-service slice.

Proves the end-to-end "Attested Changesets" flow against a real Postgres (the
append-only ``attestation`` table needs its immutability trigger, and the F39
hash chain needs ``audit_log``/``audit_chain_head``): reading truthful
provenance off a workflow run, building + Ed25519-signing an in-toto Statement,
inserting the attestation row, and chaining a ``changeset.attested`` audit event
whose ``seq`` is recorded back on the row. Also drives it through a real
``pr``-gate resolution so the F36 hook wiring is exercised, not just the
service. Skips (parked) without Postgres — runs under FORGE_TEST_DATABASE_URL.
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from forge_api.services.attestation_service import (
    ATTESTED_ACTION,
    AttestationService,
    PrAttestationResolutionHook,
    compute_policy_version_hash,
)
from forge_approval import (
    ApprovalAuthorizer,
    ApprovalService,
    GateRegistry,
    InMemoryApprovalRepository,
)
from forge_approval.models import (
    ApprovalAction,
    ApprovalDecisionRequest,
    GateType,
    Principal,
    Role,
)
from forge_contracts.attestation import (
    CHANGESET_PROVENANCE_PREDICATE_TYPE,
    DsseEnvelope,
    decode_statement,
)
from forge_db.audit.chain import verify_chain
from forge_db.base import Base
from forge_db.models import (
    AgentRun,
    Attestation,
    Project,
    SpecDocument,
    SpecVersion,
    Task,
    TraceabilityCriterionLink,
    User,
    WorkflowRun,
    Workspace,
)
from forge_obs.attest.signing import DsseSigner, DsseVerifier, EnvSigningKeyProvider

pytestmark = pytest.mark.usefixtures("pg_engine")

#: A fixed 32-byte Ed25519 seed so the signer is deterministic and silent (an
#: unset key would fail open to a warned, process-ephemeral key).
_SEED_B64 = base64.b64encode(bytes(range(1, 33))).decode("ascii")


@pytest.fixture
def factory(pg_engine) -> Iterator[sessionmaker[Session]]:
    Base.metadata.create_all(pg_engine)
    try:
        yield sessionmaker(bind=pg_engine, expire_on_commit=False, class_=Session)
    finally:
        Base.metadata.drop_all(pg_engine)


@pytest.fixture
def signer() -> DsseSigner:
    return DsseSigner(EnvSigningKeyProvider(environ={"FORGE_ATTEST_SIGNING_KEY": _SEED_B64}))


class _Seed:
    def __init__(
        self,
        *,
        workspace_id: uuid.UUID,
        task_id: uuid.UUID,
        workflow_run_id: uuid.UUID,
        agent_run_id: uuid.UUID,
        approver_id: uuid.UUID,
    ) -> None:
        self.workspace_id = workspace_id
        self.task_id = task_id
        self.workflow_run_id = workflow_run_id
        self.agent_run_id = agent_run_id
        self.approver_id = approver_id


def _seed(session: Session, *, with_traceability: bool = True) -> _Seed:
    """Seed workspace -> project -> task -> workflow_run + agent_run.

    With ``with_traceability`` also seeds a spec document, a SpecVersion (rev 4),
    and a traceability link (PRs 7/9, spec version 2) served by the task, so the
    service's spec/PR provenance derivation has real rows to read.
    """
    ws = Workspace(name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}")
    session.add(ws)
    session.flush()
    approver = User(
        workspace_id=ws.id,
        email=f"approver-{uuid.uuid4().hex[:8]}@forge.local",
        role="admin",
    )
    session.add(approver)
    session.flush()
    project = Project(workspace_id=ws.id, name="Core", key=f"C{uuid.uuid4().hex[:4]}")
    session.add(project)
    session.flush()
    task = Task(
        workspace_id=ws.id,
        project_id=project.id,
        key=f"TASK-{uuid.uuid4().hex[:6]}",
        title="attested changeset task",
    )
    session.add(task)
    session.flush()
    run = WorkflowRun(workspace_id=ws.id, task_id=task.id)
    session.add(run)
    session.flush()
    agent = AgentRun(
        workspace_id=ws.id,
        workflow_run_id=run.id,
        task_id=task.id,
        role="implementer",
        model="claude-sonnet-4-5",
        sandbox_kind="gvisor",
        steps=[
            {"index": 0, "kind": "tool_call", "tool_call": {"tool": "read_file"}},
            {"index": 1, "kind": "message", "thought": "no tool here"},
            {"index": 2, "kind": "tool_call", "tool_call": {"tool": "edit_file"}},
        ],
        output={"artifacts": {"model_usage": {"version": "2025-09-29"}}},
    )
    session.add(agent)
    session.flush()

    if with_traceability:
        spec = SpecDocument(
            workspace_id=ws.id,
            project_id=project.id,
            spec_key="F41",
            name="Attested Changesets",
        )
        session.add(spec)
        session.flush()
        session.add(
            SpecVersion(
                workspace_id=ws.id,
                spec_id=uuid.uuid4(),
                spec_key="F41",
                version_number=4,
                name="Attested Changesets",
                status="active",
                spec_md="# spec",
                manifest_yaml="key: F41",
            )
        )
        session.add(
            TraceabilityCriterionLink(
                workspace_id=ws.id,
                project_id=project.id,
                spec_id=spec.id,
                spec_key="F41",
                criterion_ext_id="AC-1",
                criterion_text="the changeset is attested",
                status="satisfied",
                task_ids=[str(task.id)],
                pr_numbers=[9, 7],
                current_spec_version=2,
            )
        )
        session.flush()

    return _Seed(
        workspace_id=ws.id,
        task_id=task.id,
        workflow_run_id=run.id,
        agent_run_id=agent.id,
        approver_id=approver.id,
    )


# --------------------------------------------------------------------------- #
# Service: build + sign + persist + chain                                     #
# --------------------------------------------------------------------------- #


def test_attest_changeset_inserts_signs_and_chains(
    factory: sessionmaker[Session], signer: DsseSigner
) -> None:
    with factory() as session:
        seed = _seed(session)
        approver = seed.approver_id
        service = AttestationService(session, signer=signer)
        attestation = service.attest_changeset(
            seed.workflow_run_id, human_approver=approver, actor_id=approver
        )
        session.commit()
        att_id = attestation.id

    with factory() as session:
        row = session.get(Attestation, att_id)
        assert row is not None
        # Row inserted with the truthful, strongly-linked provenance.
        assert row.workflow_run_id == seed.workflow_run_id
        assert row.agent_run_id == seed.agent_run_id
        assert row.predicate_type == CHANGESET_PROVENANCE_PREDICATE_TYPE
        assert row.keyid == signer.keyid
        assert row.subject_digest.startswith("sha256:")
        # Traceability-sourced spec + PRs.
        assert row.spec_key == "F41"
        assert row.spec_version == 2
        assert row.pr_numbers == [7, 9]
        # audit_seq was written in the initial INSERT (append-only table).
        assert row.audit_seq is not None

        # The signed Statement round-trips and carries the truthful predicate.
        envelope = DsseEnvelope.model_validate(row.envelope)
        statement = decode_statement(envelope.payload)
        predicate = statement.predicate
        assert predicate["agent_role"] == "implementer"
        assert predicate["model"] == "claude-sonnet-4-5"
        assert predicate["model_version"] == "2025-09-29"
        assert predicate["sandbox_tier"] == "gvisor"
        assert predicate["tool_calls"] == ["read_file", "edit_file"]
        assert predicate["human_approver"] == str(approver)
        assert predicate["prompt_spec_revision"] == 4  # latest SpecVersion
        assert predicate["policy_version_hash"] == compute_policy_version_hash(None)

        # Signature verifies over the PAE encoding (and a wrong key does not).
        verifier = DsseVerifier()
        assert verifier.verify(envelope, public_key_b64=signer.public_key_b64) is True
        other = DsseSigner(EnvSigningKeyProvider(environ={}))
        assert verifier.verify(envelope, public_key_b64=other.public_key_b64) is False

        # The audit chain includes the attested event and re-verifies cleanly.
        result = verify_chain(session, seed.workspace_id)
        assert result.ok is True
        assert result.entries_checked >= 1

        from forge_db.models import AuditLog

        entry = session.scalars(
            select(AuditLog).where(
                AuditLog.workspace_id == seed.workspace_id,
                AuditLog.seq == row.audit_seq,
            )
        ).one()
        assert entry.action == ATTESTED_ACTION
        assert entry.target_type == "pull_request"
        assert entry.detail_ref == {"table": "attestation", "id": str(att_id)}


def test_attest_changeset_without_traceability_degrades(
    factory: sessionmaker[Session], signer: DsseSigner
) -> None:
    with factory() as session:
        seed = _seed(session, with_traceability=False)
        service = AttestationService(session, signer=signer)
        attestation = service.attest_changeset(seed.workflow_run_id, pr_numbers=[42])
        session.commit()

        assert attestation.spec_key == ""
        assert attestation.spec_version == 0
        assert attestation.pr_numbers == [42]  # explicit override still honored
        assert attestation.audit_seq is not None
        assert verify_chain(session, seed.workspace_id).ok is True


def test_attest_changeset_unknown_run_raises(
    factory: sessionmaker[Session], signer: DsseSigner
) -> None:
    with factory() as session, pytest.raises(ValueError, match="not found"):
        AttestationService(session, signer=signer).attest_changeset(uuid.uuid4())


# --------------------------------------------------------------------------- #
# Wiring: attestation emitted on a real pr-gate resolution                    #
# --------------------------------------------------------------------------- #


def _approval_service(registry: GateRegistry) -> ApprovalService:
    return ApprovalService(
        InMemoryApprovalRepository(),
        registry,
        ApprovalAuthorizer(),
    )


async def test_pr_gate_resolution_emits_attestation(
    factory: sessionmaker[Session], signer: DsseSigner
) -> None:
    with factory() as session:
        seed = _seed(session)
        session.commit()

    registry = GateRegistry()
    registry.register_hook(PrAttestationResolutionHook(factory, signer=signer))
    service = _approval_service(registry)

    approver = seed.approver_id
    actor = Principal(kind="user", id=approver, role=Role.ADMIN, workspace_id=seed.workspace_id)
    request = await service.create(
        workspace_id=seed.workspace_id,
        gate_type=GateType.PR,
        subject_type="workflow_run",
        subject_id=seed.workflow_run_id,
        workflow_run_id=seed.workflow_run_id,
        gate_payload={"pr_numbers": [7, 9]},
        title="PR gate",
    )

    resolution = await service.resolve(
        request.id,
        ApprovalDecisionRequest(decision=ApprovalAction.APPROVE),
        actor,
        workspace_id=seed.workspace_id,
    )

    assert resolution.status.value == "approved"
    assert resolution.outcome.completed is True
    assert resolution.outcome.details["attested"] is True

    # The hook opened its own session and committed the attestation + audit row.
    with factory() as session:
        row = session.scalars(
            select(Attestation).where(Attestation.workflow_run_id == seed.workflow_run_id)
        ).one()
        assert row.keyid == signer.keyid
        assert row.pr_numbers == [7, 9]
        assert row.audit_seq is not None
        assert str(row.id) == resolution.outcome.details["attestation_id"]

        envelope = DsseEnvelope.model_validate(row.envelope)
        assert DsseVerifier().verify(envelope, public_key_b64=signer.public_key_b64) is True
        predicate = decode_statement(envelope.payload).predicate
        assert predicate["human_approver"] == str(approver)

        assert verify_chain(session, seed.workspace_id).ok is True


async def test_pr_gate_without_workflow_run_does_not_attest(
    factory: sessionmaker[Session], signer: DsseSigner
) -> None:
    registry = GateRegistry()
    registry.register_hook(PrAttestationResolutionHook(factory, signer=signer))
    service = _approval_service(registry)

    ws = uuid.uuid4()
    actor = Principal(kind="user", id=uuid.uuid4(), role=Role.ADMIN, workspace_id=ws)
    request = await service.create(
        workspace_id=ws,
        gate_type=GateType.PR,
        subject_type="workflow_run",
        subject_id=uuid.uuid4(),
        title="PR gate (no run)",
    )
    resolution = await service.resolve(
        request.id,
        ApprovalDecisionRequest(decision=ApprovalAction.APPROVE),
        actor,
        workspace_id=ws,
    )
    assert resolution.status.value == "approved"
    assert resolution.outcome.details["attested"] is False

    with factory() as session:
        assert session.scalars(select(Attestation)).first() is None
