"""Attested Changesets: build, sign, persist, and audit-chain an attestation.

:class:`AttestationService` reads the *truthful runtime* provenance of a
workflow run (what actually ran, never a planned value — see the slice's
grounded seams) and turns it into a signed, tamper-evident record:

1. read the strongly-linked rows — :class:`AgentRun` (``model`` / ``role`` /
   ``steps`` / ``sandbox_kind`` / ``output["artifacts"]["model_usage"]``) plus
   the run's :class:`TraceabilityCriterionLink` (``pr_numbers`` / ``spec_key`` /
   ``current_spec_version``) and :class:`SpecVersion` (``version_number``);
2. assemble a :class:`~forge_contracts.attestation.ChangesetProvenance`
   predicate inside an in-toto :class:`~forge_contracts.attestation.Statement`;
3. sign it with the DSSE signer (``forge_obs.attest.signing.DsseSigner``) over
   the PAE encoding, yielding a :class:`DsseEnvelope`;
4. INSERT one append-only :class:`Attestation` row;
5. emit ``changeset.attested`` through :class:`SqlAuditWriter` (atomic + per-
   workspace hash-chained), whose ``detail_ref`` points back at the attestation
   row; the returned :attr:`AuditLog.seq` is recorded on the attestation.

Because the ``attestation`` table is append-only (a BEFORE UPDATE trigger on
Postgres), ``audit_seq`` cannot be written by a post-insert UPDATE. The service
therefore pre-generates the attestation id, emits the audit event *first* (so
its ``detail_ref`` can reference that id), and then performs the single INSERT
with ``audit_seq`` already populated — one atomic transaction owned by the
caller.

:class:`PrAttestationResolutionHook` wires this into the F36 approval system:
approving a ``pr`` gate that carries a ``workflow_run_id`` attests the changeset
as a side effect (registered by ``forge_api.services.approval_service``).
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable, Mapping
from typing import Any, ClassVar

from sqlalchemy import select
from sqlalchemy.orm import Session

from forge_approval.models import (
    ApprovalAction,
    ApprovalDecisionRequest,
    ApprovalRequest,
    GateType,
    Principal,
    ResolutionOutcome,
)
from forge_contracts.attestation import (
    CHANGESET_PROVENANCE_PREDICATE_TYPE,
    DSSE_PAYLOAD_TYPE_INTOTO,
    ChangesetProvenance,
    DigestSet,
    Statement,
    Subject,
)
from forge_contracts.audit import AuditEvent, canonical_json
from forge_contracts.sandbox import SandboxKind
from forge_db.attest.repository import AttestationRepository
from forge_db.audit.writer import SqlAuditWriter
from forge_db.models import (
    AgentRun,
    Attestation,
    SpecVersion,
    TraceabilityCriterionLink,
    WorkflowRun,
)
from forge_obs.attest.signing import DsseSigner, pae

__all__ = [
    "ATTESTED_ACTION",
    "AttestationService",
    "PrAttestationResolutionHook",
    "compute_policy_version_hash",
]

#: Audit action emitted when a changeset is attested.
ATTESTED_ACTION = "changeset.attested"


def compute_policy_version_hash(policy: Mapping[str, Any] | None) -> str:
    """``sha256`` hex over the canonical form of the resolved ``.forge/policy.yaml``.

    There is no first-class policy-version primitive, so a content hash of the
    resolved policy mapping is the version identity. ``None``/empty hashes the
    empty mapping deterministically (an honest "no policy resolved").
    """
    return hashlib.sha256(canonical_json(dict(policy or {})).encode("utf-8")).hexdigest()


def _tool_calls(steps: list[Any]) -> list[str]:
    """Tool names invoked in order, distilled from ``AgentRun.steps``.

    Names only (no arguments): an attestation may be exported outside the trust
    boundary the raw step log lives in. Tolerant of the persisted JSON shape —
    each step is a ``Step``-like mapping whose ``tool_call.tool`` names the tool.
    """
    names: list[str] = []
    for step in steps:
        if not isinstance(step, Mapping):
            continue
        call = step.get("tool_call")
        if isinstance(call, Mapping):
            tool = call.get("tool")
            if isinstance(tool, str) and tool:
                names.append(tool)
    return names


def _model_version(output: Mapping[str, Any] | None) -> str | None:
    """The provider-reported model version from ``artifacts["model_usage"]``.

    Truthful and defensive: only returns a value the run actually recorded (a
    ``version`` field on the ``model_usage`` artifact), else ``None``.
    """
    if not isinstance(output, Mapping):
        return None
    artifacts = output.get("artifacts")
    if not isinstance(artifacts, Mapping):
        return None
    usage = artifacts.get("model_usage")
    if isinstance(usage, Mapping):
        version = usage.get("version")
        if isinstance(version, str) and version:
            return version
    return None


class AttestationService:
    """Builds, signs, persists, and audit-chains a changeset attestation.

    Operates on the caller's :class:`Session` and participates in its
    transaction (fail-closed: the attestation row and its audit entry commit
    together). The caller commits.
    """

    def __init__(
        self,
        session: Session,
        *,
        signer: DsseSigner | None = None,
        redactor: object | None = None,
    ) -> None:
        self._session = session
        self._signer = signer or DsseSigner()
        self._repo = AttestationRepository(session)
        self._audit = SqlAuditWriter(session, redactor=redactor)

    @property
    def signer(self) -> DsseSigner:
        """The DSSE signer (exposes ``keyid`` / ``public_key_b64`` for verify)."""
        return self._signer

    def attest_changeset(
        self,
        workflow_run_id: uuid.UUID,
        *,
        human_approver: uuid.UUID | None = None,
        pr_numbers: list[int] | None = None,
        policy: Mapping[str, Any] | None = None,
        actor_id: uuid.UUID | None = None,
    ) -> Attestation:
        """Attest the changeset produced by ``workflow_run_id``.

        Reads the truthful provenance, signs an in-toto Statement, inserts the
        append-only :class:`Attestation`, and chains a ``changeset.attested``
        audit event whose ``seq`` is recorded on the row. Returns the row.
        """
        run = self._session.get(WorkflowRun, workflow_run_id)
        if run is None:
            raise ValueError(f"workflow_run {workflow_run_id} not found")
        workspace_id = run.workspace_id

        agent = self._representative_agent_run(workflow_run_id)
        if agent is None:
            raise ValueError(f"workflow_run {workflow_run_id} has no agent_run to attest")

        spec_key, spec_version, link_prs = self._spec_and_prs(workspace_id, run.task_id)
        resolved_prs = pr_numbers if pr_numbers is not None else link_prs
        prompt_spec_revision = self._prompt_spec_revision(workspace_id, spec_key, spec_version)
        policy_source = policy if policy is not None else _run_policy(run)

        provenance = ChangesetProvenance(
            agent_role=agent.role,
            model=agent.model or "",
            model_version=_model_version(agent.output),
            prompt_spec_revision=prompt_spec_revision,
            sandbox_tier=SandboxKind(agent.sandbox_kind.value),
            policy_version_hash=compute_policy_version_hash(policy_source),
            tool_calls=_tool_calls(agent.steps),
            human_approver=human_approver,
            workflow_run_id=workflow_run_id,
            agent_run_id=agent.id,
            pr_numbers=list(resolved_prs),
            spec_key=spec_key,
            spec_version=spec_version,
        )

        predicate = provenance.model_dump(mode="json")
        subject_hex = hashlib.sha256(canonical_json(predicate).encode("utf-8")).hexdigest()
        statement = Statement(
            subject=[
                Subject(
                    name=f"workflow-run/{workflow_run_id}",
                    digest=DigestSet(sha256=subject_hex),
                )
            ],
            predicateType=CHANGESET_PROVENANCE_PREDICATE_TYPE,
            predicate=predicate,
        )

        payload_bytes = canonical_json(statement.model_dump(mode="json", by_alias=True)).encode(
            "utf-8"
        )
        envelope = self._signer.sign(payload_type=DSSE_PAYLOAD_TYPE_INTOTO, payload=payload_bytes)
        payload_hash = hashlib.sha256(pae(DSSE_PAYLOAD_TYPE_INTOTO, payload_bytes)).hexdigest()
        subject_digest = f"sha256:{subject_hex}"

        # Pre-generate the id so the audit event's detail_ref can point at this
        # row before it is inserted; the append-only table forbids writing
        # audit_seq via a later UPDATE, so it must land in this single INSERT.
        attestation_id = uuid.uuid4()
        audit_log = self._audit.emit(
            AuditEvent(
                workspace_id=workspace_id,
                action=ATTESTED_ACTION,
                actor_id=actor_id,
                actor_type="system" if actor_id is None else "user",
                target_type="pull_request",
                scope_type="workflow_run",
                scope_id=workflow_run_id,
                severity="notice",
                detail_ref={"table": "attestation", "id": str(attestation_id)},
                details={
                    "keyid": self._signer.keyid,
                    "predicate_type": CHANGESET_PROVENANCE_PREDICATE_TYPE,
                    "subject_digest": subject_digest,
                    "payload_hash": payload_hash,
                    "agent_run_id": str(agent.id),
                    "workflow_run_id": str(workflow_run_id),
                    "pr_numbers": list(resolved_prs),
                    "spec_key": spec_key,
                    "spec_version": spec_version,
                },
            )
        )

        return self._repo.insert(
            workspace_id,
            id=attestation_id,
            subject_digest=subject_digest,
            predicate_type=CHANGESET_PROVENANCE_PREDICATE_TYPE,
            envelope=envelope.model_dump(mode="json"),
            payload_hash=payload_hash,
            keyid=self._signer.keyid,
            workflow_run_id=workflow_run_id,
            agent_run_id=agent.id,
            pr_numbers=list(resolved_prs),
            spec_key=spec_key,
            spec_version=spec_version,
            audit_seq=audit_log.seq,
        )

    # ---------------------------------------------------------------- reads #

    def _representative_agent_run(self, workflow_run_id: uuid.UUID) -> AgentRun | None:
        """The agent run whose runtime facts the attestation records.

        Prefers the earliest non-supervisor run that actually recorded a model
        (what ran); falls back to the earliest run of the workflow.
        """
        runs = list(
            self._session.scalars(
                select(AgentRun)
                .where(AgentRun.workflow_run_id == workflow_run_id)
                .order_by(AgentRun.created_at, AgentRun.id)
            ).all()
        )
        for run in runs:
            if not run.is_supervisor and run.model:
                return run
        return runs[0] if runs else None

    def _spec_and_prs(
        self, workspace_id: uuid.UUID, task_id: uuid.UUID | None
    ) -> tuple[str, int, list[int]]:
        """Derive ``(spec_key, spec_version, pr_numbers)`` from traceability.

        Links the run to its acceptance-criterion rows by ``task_id`` (a
        ``TraceabilityCriterionLink`` carries the ``task_ids`` a criterion is
        served by, plus the ``pr_numbers`` that satisfied it and the spec it
        belongs to). Degrades to ``("", 0, [])`` when no traceability exists.
        """
        if task_id is None:
            return "", 0, []
        links = self._session.scalars(
            select(TraceabilityCriterionLink).where(
                TraceabilityCriterionLink.workspace_id == workspace_id
            )
        ).all()
        spec_key = ""
        spec_version = 0
        prs: list[int] = []
        for link in links:
            if task_id not in {_as_uuid(t) for t in link.task_ids}:
                continue
            if not spec_key:
                spec_key = link.spec_key
                spec_version = link.current_spec_version
            for number in link.pr_numbers:
                if isinstance(number, int) and number not in prs:
                    prs.append(number)
        return spec_key, spec_version, sorted(prs)

    def _prompt_spec_revision(self, workspace_id: uuid.UUID, spec_key: str, fallback: int) -> int:
        """Latest recorded :attr:`SpecVersion.version_number` for ``spec_key``."""
        if not spec_key:
            return fallback
        latest = self._session.scalars(
            select(SpecVersion.version_number)
            .where(
                SpecVersion.workspace_id == workspace_id,
                SpecVersion.spec_key == spec_key,
            )
            .order_by(SpecVersion.version_number.desc())
            .limit(1)
        ).first()
        return latest if latest is not None else fallback


def _as_uuid(value: Any) -> uuid.UUID | None:
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (ValueError, TypeError):
        return None


def _run_policy(run: WorkflowRun) -> Mapping[str, Any] | None:
    """The resolved policy recorded on the run's context, if any."""
    policy = run.context.get("policy") if isinstance(run.context, Mapping) else None
    return policy if isinstance(policy, Mapping) else None


class PrAttestationResolutionHook:
    """Attests the changeset when a ``pr`` gate is approved (F36 resolution hook).

    Only a gate that carries a ``workflow_run_id`` is attestable; gates without
    one (or non-approve decisions) resolve without attesting. The hook uses the
    session threaded through :meth:`ApprovalService.resolve` when present, else
    opens one from its injected factory and commits it (the HTTP approval path
    does not thread a session).
    """

    gate_type: ClassVar[GateType] = GateType.PR

    def __init__(
        self,
        session_factory: Callable[[], Session] | None = None,
        *,
        signer: DsseSigner | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._signer = signer

    async def on_resolved(
        self,
        request: ApprovalRequest,
        decision: ApprovalDecisionRequest,
        actor: Principal,
        *,
        session: Any = None,
    ) -> ResolutionOutcome:
        if decision.decision is not ApprovalAction.APPROVE:
            return ResolutionOutcome(
                completed=True,
                follow_up_state=f"pr_{request.status.value}",
            )
        if request.workflow_run_id is None:
            return ResolutionOutcome(
                completed=True,
                follow_up_state="pr_approved",
                details={"attested": False, "reason": "no_workflow_run"},
            )

        pr_numbers = _pr_numbers_from_payload(request.gate_payload)
        if session is not None:
            attestation = self._attest(session, request, actor, pr_numbers)
            return _attested_outcome(attestation)
        if self._session_factory is not None:
            with self._session_factory() as owned:
                attestation = self._attest(owned, request, actor, pr_numbers)
                owned.commit()
                return _attested_outcome(attestation)
        return ResolutionOutcome(
            completed=False,
            blocking_reasons=["no database session available to record attestation"],
            follow_up_state="pr_approved",
            details={"attested": False, "reason": "no_session"},
        )

    def _attest(
        self,
        session: Session,
        request: ApprovalRequest,
        actor: Principal,
        pr_numbers: list[int] | None,
    ) -> Attestation:
        assert request.workflow_run_id is not None
        service = AttestationService(session, signer=self._signer)
        return service.attest_changeset(
            request.workflow_run_id,
            human_approver=actor.id,
            actor_id=actor.id,
            pr_numbers=pr_numbers,
        )


def _pr_numbers_from_payload(payload: Mapping[str, Any]) -> list[int] | None:
    raw = payload.get("pr_numbers")
    if isinstance(raw, list):
        numbers = [n for n in raw if isinstance(n, int)]
        return numbers or None
    return None


def _attested_outcome(attestation: Attestation) -> ResolutionOutcome:
    return ResolutionOutcome(
        completed=True,
        follow_up_state="pr_attested",
        details={
            "attested": True,
            "attestation_id": str(attestation.id),
            "keyid": attestation.keyid,
            "audit_seq": attestation.audit_seq,
            "subject_digest": attestation.subject_digest,
        },
    )
