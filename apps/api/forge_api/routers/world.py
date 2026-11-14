"""World-model REST surface, mounted at ``/world``.

Every route requires an authenticated principal and scopes by
``principal.workspace_id``; cross-workspace ids are 404. Reads need READ;
writes need WRITE, except proposing and decision-recording, which also
admit RUN_AGENT (agents propose; only people with WRITE — never the
proposer — decide). Ingest resolves the workspace model client and refuses
with 503 when none is configured. Every mutation emits exactly one audit
event (``world.<verb>``) in the same transaction, with fact text clipped to
200 characters. Domain rules live in ``forge_world``; this module only maps
HTTP <-> service and translates domain errors (404/403/409/422/503).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status

from forge_api.auth.rbac import Permission, can
from forge_api.deps import DbSession, Principal, get_current_principal
from forge_api.routers._rbac import require_permission
from forge_api.schemas.world import (
    ChangeOut,
    ChangesOut,
    ClassesOut,
    ClassIn,
    ClassOut,
    ConflictOut,
    ConflictsOut,
    DecisionIn,
    DecisionOut,
    DeriveOut,
    DriftItemOut,
    DriftOut,
    EntityListOut,
    EntityOut,
    EntityProfileOut,
    EvidenceOut,
    FactListOut,
    FactOut,
    FactWriteOut,
    IngestIn,
    IngestOut,
    MergeIn,
    MergeOut,
    ProposalDecisionIn,
    ProposalDecisionOut,
    ProposalIn,
    ProposalListOut,
    ProposalOut,
    RelationIn,
    RelationOut,
    RelationsOut,
    RevertIn,
    StatementIn,
    ViolationOut,
)
from forge_api.services.audit import SqlAuditWriter
from forge_api.services.world_service import (
    get_world_service,
    make_world_service,
    resolve_world_model_client,
)
from forge_contracts import ModelClient, UserRole
from forge_contracts.audit import AuditEvent
from forge_world import (
    ClassDef,
    ConflictError,
    DecisionData,
    EntityData,
    EvidenceData,
    FactData,
    ForbiddenError,
    Layer,
    MergeData,
    ModelNotConfiguredError,
    NotFoundError,
    ProposalData,
    ProposalStatus,
    RelationDef,
    SourceKind,
    WorldModelService,
    WorldValidationError,
)

router = APIRouter(
    prefix="/world",
    tags=["world"],
    dependencies=[Depends(get_current_principal)],
)

ReaderDep = Annotated[Principal, Depends(require_permission(Permission.READ))]
WriterDep = Annotated[Principal, Depends(require_permission(Permission.WRITE))]
ServiceDep = Annotated[WorldModelService, Depends(get_world_service)]
ModelClientDep = Annotated[ModelClient, Depends(resolve_world_model_client)]


def _require_write_or_run_agent(
    principal: Annotated[Principal, Depends(get_current_principal)],
) -> Principal:
    """WRITE or RUN_AGENT (propose / record): agents may accountable-propose."""
    if can(principal.role, Permission.WRITE) or can(principal.role, Permission.RUN_AGENT):
        return principal
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=f"role '{principal.role.value}' lacks permission 'write' or 'run_agent'",
    )


ProposerDep = Annotated[Principal, Depends(_require_write_or_run_agent)]


@contextmanager
def _world_errors() -> Iterator[None]:
    """Translate world-model domain errors into HTTP responses."""
    try:
        yield
    except NotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ForbiddenError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except ConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except WorldValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    except ModelNotConfiguredError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc


def _clip(text: str | None) -> str | None:
    """Fact text in audit payloads is capped at 200 characters."""
    if text is None:
        return None
    return text[:200]


def _audit(
    service: WorldModelService,
    principal: Principal,
    action: str,
    *,
    target_type: str,
    target_id: uuid.UUID | None,
    details: dict[str, Any] | None = None,
) -> None:
    SqlAuditWriter(service.session).emit(
        AuditEvent(
            workspace_id=principal.workspace_id,
            action=f"world.{action}",
            actor_id=principal.user_id,
            target_type=target_type,
            target_id=target_id,
            result="success",
            details=details or {},
        )
    )


def _actor(principal: Principal) -> str:
    return str(principal.user_id)


def _evidence_out(evidence: EvidenceData) -> EvidenceOut:
    return EvidenceOut(
        id=evidence.id,
        source_ref=evidence.source_ref,
        chunk_index=evidence.chunk_index,
        quote=evidence.quote,
        start_offset=evidence.start_offset,
        end_offset=evidence.end_offset,
        doc_time=evidence.doc_time,
    )


def _effective(
    service: WorldModelService, workspace_id: uuid.UUID, facts: Sequence[FactData]
) -> Mapping[uuid.UUID, float]:
    """Effective confidences for a batch of facts (spec J §2.5)."""
    return service.effective_confidences(workspace_id, facts)


def _fact_out(
    fact: FactData,
    evidence: Mapping[uuid.UUID, Sequence[EvidenceData]] | None = None,
    *,
    effective_confidence: float,
) -> FactOut:
    return FactOut(
        id=fact.id,
        subject_id=fact.subject_id,
        phrase=fact.phrase,
        predicate_key=fact.predicate_key,
        object_id=fact.object_id,
        value=fact.value,
        layer=fact.layer.value,
        mood=fact.mood.value if fact.mood is not None else None,
        qualifiers=dict(fact.qualifiers),
        confidence=fact.confidence,
        effective_confidence=effective_confidence,
        valid_from=fact.valid_from,
        valid_from_precision=fact.valid_from_precision.value,
        valid_to=fact.valid_to,
        valid_to_precision=fact.valid_to_precision.value,
        end_derived=fact.end_derived,
        end_after_period=fact.end_after_period,
        source_kind=fact.source_kind.value,
        actor=fact.actor,
        proposal_id=fact.proposal_id,
        supersedes_id=fact.supersedes_id,
        recorded_at=fact.recorded_at,
        invalidated_at=fact.invalidated_at,
        invalidated_reason=(
            fact.invalidated_reason.value if fact.invalidated_reason is not None else None
        ),
        evidence=[_evidence_out(item) for item in (evidence or {}).get(fact.id, [])],
    )


def _fact_write_out(
    service: WorldModelService,
    workspace_id: uuid.UUID,
    fact: FactData,
    *,
    created: bool,
) -> FactWriteOut:
    effective = _effective(service, workspace_id, [fact])[fact.id]
    return FactWriteOut(
        **_fact_out(fact, effective_confidence=effective).model_dump(), created=created
    )


def _entity_out(entity: EntityData) -> EntityOut:
    return EntityOut(
        id=entity.id,
        label=entity.label,
        type_key=entity.type_key,
        type_source=entity.type_source,
        described=entity.described,
        described_in=entity.described_in,
        created_at=entity.created_at,
    )


def _proposal_out(proposal: ProposalData) -> ProposalOut:
    return ProposalOut(
        id=proposal.id,
        kind=proposal.kind,
        payload=dict(proposal.payload),
        status=proposal.status.value,
        proposer_kind=proposal.proposer_kind.value,
        proposer=proposal.proposer,
        rationale=proposal.rationale,
        gate=dict(proposal.gate),
        decided_by=proposal.decided_by,
        decided_at=proposal.decided_at,
        decision_reason=proposal.decision_reason,
        fact_id=proposal.fact_id,
        created_at=proposal.created_at,
    )


def _merge_out(merge: MergeData) -> MergeOut:
    return MergeOut(
        id=merge.id,
        survivor_id=merge.survivor_id,
        merged_id=merge.merged_id,
        actor=merge.actor,
        reason=merge.reason,
        created_at=merge.created_at,
        reverted_at=merge.reverted_at,
        reverted_by=merge.reverted_by,
        revert_reason=merge.revert_reason,
    )


def _class_out(definition: ClassDef) -> ClassOut:
    return ClassOut(
        id=definition.id,
        key=definition.key,
        label=definition.label,
        parent_key=definition.parent_key,
        disjoint_with=list(definition.disjoint_with),
        iri=definition.iri,
    )


def _relation_out(definition: RelationDef) -> RelationOut:
    return RelationOut(
        id=definition.id,
        key=definition.key,
        label=definition.label,
        kind=definition.kind.value,
        domain_class=definition.domain_class,
        range_class=definition.range_class,
        functional=definition.functional,
        inverse_functional=definition.inverse_functional,
        transitive=definition.transitive,
        symmetric=definition.symmetric,
        inverse_of=definition.inverse_of,
        sub_property_of=definition.sub_property_of,
        iri=definition.iri,
    )


def _decision_out(decision: DecisionData) -> DecisionOut:
    return DecisionOut(
        id=decision.id,
        title=decision.title,
        outcome=decision.outcome,
        actor=decision.actor,
        as_of=decision.as_of,
        at=decision.at,
        fact_ids=list(decision.fact_ids),
        links=dict(decision.links),
        created_at=decision.created_at,
    )


# --------------------------------------------------------------------------- #
# Entities and facts (reads)                                                  #
# --------------------------------------------------------------------------- #


@router.get("/entities", response_model=EntityListOut)
def search_entities(
    principal: ReaderDep,
    service: ServiceDep,
    q: Annotated[str | None, Query()] = None,
    type: Annotated[str | None, Query(alias="type")] = None,
    at: Annotated[datetime | None, Query()] = None,
    as_of: Annotated[datetime | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> EntityListOut:
    """Search entities by name (``known_as`` values and labels)."""
    with _world_errors():
        entities = service.search_entities(
            principal.workspace_id,
            q=q,
            type_key=type,
            at=at,
            as_of=as_of,
            limit=limit,
            offset=offset,
        )
    return EntityListOut(
        items=[_entity_out(entity) for entity in entities], limit=limit, offset=offset
    )


@router.get("/entities/{entity_id}", response_model=EntityProfileOut)
def get_entity(
    principal: ReaderDep,
    service: ServiceDep,
    entity_id: uuid.UUID,
    at: Annotated[datetime | None, Query()] = None,
    as_of: Annotated[datetime | None, Query()] = None,
) -> EntityProfileOut:
    """One entity profile: resolved id, names, and facts on both clocks."""
    with _world_errors():
        profile = service.get_entity_profile(principal.workspace_id, entity_id, at=at, as_of=as_of)
        evidence = service.evidence_for(
            principal.workspace_id,
            [fact.id for fact in (*profile.facts, *profile.object_of)],
        )
    effective = _effective(service, principal.workspace_id, [*profile.facts, *profile.object_of])
    return EntityProfileOut(
        entity=_entity_out(profile.entity),
        resolved_id=profile.resolved_id,
        names=list(profile.names),
        facts=[
            _fact_out(fact, evidence, effective_confidence=effective[fact.id])
            for fact in profile.facts
        ],
        object_of=[
            _fact_out(fact, evidence, effective_confidence=effective[fact.id])
            for fact in profile.object_of
        ],
    )


@router.get("/entities/{entity_id}/changes", response_model=ChangesOut)
def get_entity_changes(
    principal: ReaderDep,
    service: ServiceDep,
    entity_id: uuid.UUID,
    since: Annotated[datetime | None, Query()] = None,
    until: Annotated[datetime | None, Query()] = None,
) -> ChangesOut:
    """Record-axis events for an entity: asserted / invalidated(reason)."""
    with _world_errors():
        events = service.entity_changes(principal.workspace_id, entity_id, since=since, until=until)
    return ChangesOut(
        items=[
            ChangeOut(
                kind=event.kind,
                fact_id=event.fact_id,
                occurred_at=event.occurred_at,
                reason=event.reason,
            )
            for event in events
        ]
    )


@router.get("/facts", response_model=FactListOut)
def query_facts(
    principal: ReaderDep,
    service: ServiceDep,
    subject: Annotated[uuid.UUID | None, Query()] = None,
    predicate: Annotated[str | None, Query()] = None,
    object_id: Annotated[uuid.UUID | None, Query(alias="object")] = None,
    layer: Annotated[Layer | None, Query()] = None,
    at: Annotated[datetime | None, Query()] = None,
    as_of: Annotated[datetime | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> FactListOut:
    """Facts holding at ``at`` as known at ``as_of``, with evidence."""
    with _world_errors():
        facts = service.query_facts(
            principal.workspace_id,
            subject=subject,
            predicate=predicate,
            object_id=object_id,
            layer=layer,
            at=at,
            as_of=as_of,
            limit=limit,
            offset=offset,
        )
        evidence = service.evidence_for(principal.workspace_id, [fact.id for fact in facts])
    effective = _effective(service, principal.workspace_id, facts)
    return FactListOut(
        items=[_fact_out(fact, evidence, effective_confidence=effective[fact.id]) for fact in facts]
    )


# --------------------------------------------------------------------------- #
# Fact writes                                                                 #
# --------------------------------------------------------------------------- #


@router.post("/facts", response_model=FactWriteOut, status_code=status.HTTP_201_CREATED)
def assert_fact(principal: WriterDep, service: ServiceDep, body: StatementIn) -> FactWriteOut:
    """Assert a fact as a person (a duplicate returns the live fact)."""
    with _world_errors():
        result = service.assert_input(
            principal.workspace_id,
            body.to_input(),
            source_kind=SourceKind.PERSON,
            actor=_actor(principal),
        )
    _audit(
        service,
        principal,
        "fact_asserted",
        target_type="world_fact",
        target_id=result.fact.id,
        details={
            "subject_id": str(result.fact.subject_id),
            "object_id": str(result.fact.object_id) if result.fact.object_id else None,
            "predicate_key": result.fact.predicate_key,
            "phrase": _clip(result.fact.phrase),
            "layer": result.fact.layer.value,
            "created": result.created,
            "created_entity_ids": [str(item) for item in result.created_entity_ids],
            "typed_entity_ids": [str(item) for item in result.typed_entity_ids],
        },
    )
    service.session.commit()
    return _fact_write_out(service, principal.workspace_id, result.fact, created=result.created)


@router.post(
    "/facts/{fact_id}/supersede",
    response_model=FactWriteOut,
    status_code=status.HTTP_201_CREATED,
)
def supersede_fact(
    principal: WriterDep, service: ServiceDep, fact_id: uuid.UUID, body: StatementIn
) -> FactWriteOut:
    """Correct a fact: invalidate the old row and assert its successor."""
    with _world_errors():
        result = service.supersede(
            principal.workspace_id,
            fact_id,
            body.to_input(),
            source_kind=SourceKind.PERSON,
            actor=_actor(principal),
        )
    _audit(
        service,
        principal,
        "fact_superseded",
        target_type="world_fact",
        target_id=result.fact.id,
        details={
            "supersedes_id": str(fact_id),
            "created": result.created,
            "object_id": str(result.fact.object_id) if result.fact.object_id else None,
            "created_entity_ids": [str(item) for item in result.created_entity_ids],
            "typed_entity_ids": [str(item) for item in result.typed_entity_ids],
        },
    )
    service.session.commit()
    return _fact_write_out(service, principal.workspace_id, result.fact, created=result.created)


@router.post("/facts/{fact_id}/retract", response_model=FactOut)
def retract_fact(principal: WriterDep, service: ServiceDep, fact_id: uuid.UUID) -> FactOut:
    """Invalidate a fact with reason ``retracted``."""
    with _world_errors():
        fact = service.retract(principal.workspace_id, fact_id, actor=_actor(principal))
    _audit(
        service,
        principal,
        "fact_retracted",
        target_type="world_fact",
        target_id=fact.id,
        details={"phrase": _clip(fact.phrase)},
    )
    service.session.commit()
    effective = _effective(service, principal.workspace_id, [fact])
    return _fact_out(fact, effective_confidence=effective[fact.id])


@router.post("/ingest", response_model=IngestOut)
def ingest(
    principal: WriterDep,
    session: DbSession,
    client: ModelClientDep,
    body: IngestIn,
) -> IngestOut:
    """Extract facts from a document (503 when no model is configured)."""
    service = make_world_service(session, model_client=client)
    with _world_errors():
        result = service.ingest(
            principal.workspace_id,
            source_ref=body.source_ref,
            title=body.title,
            text=body.text,
            doc_time=body.doc_time,
            actor=_actor(principal),
        )
    _audit(
        service,
        principal,
        "document_ingested",
        target_type="world_source",
        target_id=None,
        details={
            "source_ref": _clip(body.source_ref),
            "chunks": result.chunks,
            "facts_created": result.facts_created,
            "duplicates": result.duplicates,
            "entities_created": result.entities_created,
            "drops": result.drops,
            "drops_by_reason": result.drops_by_reason,
        },
    )
    service.session.commit()
    return IngestOut(
        source_ref=body.source_ref,
        chunks=result.chunks,
        facts_created=result.facts_created,
        duplicates=result.duplicates,
        entities_created=result.entities_created,
        drops=result.drops,
        drops_by_reason=result.drops_by_reason,
        fact_ids=list(result.fact_ids),
    )


# --------------------------------------------------------------------------- #
# Proposals (machines propose, people decide)                                 #
# --------------------------------------------------------------------------- #


@router.get("/proposals", response_model=ProposalListOut)
def list_proposals(
    principal: ReaderDep,
    service: ServiceDep,
    status_filter: Annotated[ProposalStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ProposalListOut:
    """The review queue, newest first, optionally filtered by status."""
    with _world_errors():
        proposals = service.list_proposals(
            principal.workspace_id, status=status_filter, limit=limit, offset=offset
        )
    return ProposalListOut(items=[_proposal_out(proposal) for proposal in proposals])


@router.post("/proposals", response_model=ProposalOut, status_code=status.HTTP_201_CREATED)
def create_proposal(principal: ProposerDep, service: ServiceDep, body: ProposalIn) -> ProposalOut:
    """Propose a fact (never writes one; carries the shadow gate verdict)."""
    # Provenance is derived from the authenticated role (ADR-0005): an agent can never
    # present its claim as a person's, which would make an accepted fact's typing final.
    proposer_kind = (
        SourceKind.AGENT if principal.role is UserRole.AGENT_RUNNER else SourceKind.PERSON
    )
    with _world_errors():
        proposal = service.create_proposal(
            principal.workspace_id,
            body.to_input(),
            proposer_kind=proposer_kind,
            proposer=_actor(principal),
            rationale=body.rationale,
            verdict=body.verdict,
            confidence=body.gate_confidence,
        )
    _audit(
        service,
        principal,
        "proposal_created",
        target_type="world_proposal",
        target_id=proposal.id,
        details={
            "proposer_kind": proposal.proposer_kind.value,
            "phrase": _clip(body.phrase),
            "predicate_key": body.predicate_key,
            "gate_decision": proposal.gate.get("decision"),
        },
    )
    service.session.commit()
    return _proposal_out(proposal)


@router.post("/proposals/{proposal_id}/decision", response_model=ProposalDecisionOut)
def decide_proposal(
    principal: WriterDep,
    service: ServiceDep,
    proposal_id: uuid.UUID,
    body: ProposalDecisionIn,
) -> ProposalDecisionOut:
    """Accept or reject a proposal (never by its proposer)."""
    with _world_errors():
        outcome = service.decide_proposal(
            principal.workspace_id,
            proposal_id,
            decider=_actor(principal),
            accept=body.accept,
            reason=body.reason,
        )
    _audit(
        service,
        principal,
        "proposal_decided",
        target_type="world_proposal",
        target_id=proposal_id,
        details={
            "accepted": body.accept,
            "fact_id": str(outcome.fact.id) if outcome.fact is not None else None,
            "object_id": (
                str(outcome.fact.object_id)
                if outcome.fact is not None and outcome.fact.object_id
                else None
            ),
            "created_entity_ids": (
                [str(item) for item in outcome.result.created_entity_ids]
                if outcome.result is not None
                else []
            ),
            "typed_entity_ids": (
                [str(item) for item in outcome.result.typed_entity_ids]
                if outcome.result is not None
                else []
            ),
            "gate_decision": outcome.proposal.gate.get("decision"),
        },
    )
    service.session.commit()
    return ProposalDecisionOut(
        proposal=_proposal_out(outcome.proposal),
        fact=(
            _fact_out(
                outcome.fact,
                effective_confidence=_effective(service, principal.workspace_id, [outcome.fact])[
                    outcome.fact.id
                ],
            )
            if outcome.fact is not None
            else None
        ),
    )


# --------------------------------------------------------------------------- #
# Merges, reasoning, conflicts                                                #
# --------------------------------------------------------------------------- #


@router.post("/merges", response_model=MergeOut, status_code=status.HTTP_201_CREATED)
def merge_entities(principal: WriterDep, service: ServiceDep, body: MergeIn) -> MergeOut:
    """Merge one entity into another (reads resolve to the survivor)."""
    with _world_errors():
        merge = service.merge_entities(
            principal.workspace_id,
            survivor_id=body.survivor_id,
            merged_id=body.merged_id,
            actor=_actor(principal),
            reason=body.reason,
        )
    _audit(
        service,
        principal,
        "entities_merged",
        target_type="world_entity_merge",
        target_id=merge.id,
        details={
            "survivor_id": str(body.survivor_id),
            "merged_id": str(body.merged_id),
        },
    )
    service.session.commit()
    return _merge_out(merge)


@router.post("/merges/{merge_id}/revert", response_model=MergeOut)
def revert_merge(
    principal: WriterDep, service: ServiceDep, merge_id: uuid.UUID, body: RevertIn
) -> MergeOut:
    """Revert a merge (the merged entity stands on its own again)."""
    with _world_errors():
        merge = service.revert_merge(
            principal.workspace_id,
            merge_id,
            actor=_actor(principal),
            reason=body.reason,
        )
    _audit(
        service,
        principal,
        "merge_reverted",
        target_type="world_entity_merge",
        target_id=merge.id,
        details={
            "survivor_id": str(merge.survivor_id),
            "merged_id": str(merge.merged_id),
        },
    )
    service.session.commit()
    return _merge_out(merge)


@router.post("/derive", response_model=DeriveOut)
def derive(principal: WriterDep, service: ServiceDep) -> DeriveOut:
    """Run reasoning; returns counts, capped predicates, violations."""
    with _world_errors():
        result = service.derive(principal.workspace_id, actor=_actor(principal))
    _audit(
        service,
        principal,
        "derived",
        target_type="world_workspace",
        target_id=None,
        details={
            "created": result.created,
            "invalidated": result.invalidated,
            "capped": list(result.capped),
            "violations": len(result.violations),
        },
    )
    service.session.commit()
    return DeriveOut(
        created=result.created,
        invalidated=result.invalidated,
        capped=list(result.capped),
        violations=[
            ViolationOut(
                predicate_key=violation.predicate_key,
                subject_id=violation.subject_id,
                object_id=violation.object_id,
                value=violation.value,
                asserted_fact_id=violation.asserted_fact_id,
                rule=violation.rule,
                premise_ids=list(violation.premise_ids),
                detail=violation.detail,
            )
            for violation in result.violations
        ],
    )


@router.get("/conflicts", response_model=ConflictsOut)
def list_conflicts(
    principal: ReaderDep,
    service: ServiceDep,
    at: Annotated[datetime | None, Query()] = None,
) -> ConflictsOut:
    """Functional conflicts, unordered successions, reasoning violations."""
    with _world_errors():
        conflicts = service.conflicts(principal.workspace_id, at=at)
    return ConflictsOut(
        items=[
            ConflictOut(
                kind=conflict.kind,
                predicate_key=conflict.predicate_key,
                subject_id=conflict.subject_id,
                object_id=conflict.object_id,
                fact_ids=list(conflict.fact_ids),
                detail=conflict.detail,
            )
            for conflict in conflicts
        ]
    )


# --------------------------------------------------------------------------- #
# Ontology and decisions                                                      #
# --------------------------------------------------------------------------- #


@router.get("/ontology/classes", response_model=ClassesOut)
def list_classes(principal: ReaderDep, service: ServiceDep) -> ClassesOut:
    with _world_errors():
        classes = service.list_classes(principal.workspace_id)
    return ClassesOut(items=[_class_out(entry) for entry in classes])


@router.post("/ontology/classes", response_model=ClassOut, status_code=status.HTTP_201_CREATED)
def create_class(principal: WriterDep, service: ServiceDep, body: ClassIn) -> ClassOut:
    with _world_errors():
        created = service.create_class(
            principal.workspace_id,
            key=body.key,
            label=body.label,
            parent_key=body.parent_key,
            disjoint_with=body.disjoint_with,
            iri=body.iri,
        )
    _audit(
        service,
        principal,
        "class_created",
        target_type="world_class",
        target_id=created.id,
        details={"key": created.key},
    )
    service.session.commit()
    return _class_out(created)


@router.get("/ontology/relations", response_model=RelationsOut)
def list_relations(principal: ReaderDep, service: ServiceDep) -> RelationsOut:
    with _world_errors():
        relations = service.list_relations(principal.workspace_id)
    return RelationsOut(items=[_relation_out(entry) for entry in relations])


@router.post(
    "/ontology/relations",
    response_model=RelationOut,
    status_code=status.HTTP_201_CREATED,
)
def create_relation(principal: WriterDep, service: ServiceDep, body: RelationIn) -> RelationOut:
    with _world_errors():
        created = service.create_relation(
            principal.workspace_id,
            key=body.key,
            label=body.label,
            kind=body.kind,
            domain_class=body.domain_class,
            range_class=body.range_class,
            functional=body.functional,
            inverse_functional=body.inverse_functional,
            transitive=body.transitive,
            symmetric=body.symmetric,
            inverse_of=body.inverse_of,
            sub_property_of=body.sub_property_of,
            iri=body.iri,
        )
    _audit(
        service,
        principal,
        "relation_created",
        target_type="world_relation",
        target_id=created.id,
        details={"key": created.key, "kind": created.kind.value},
    )
    service.session.commit()
    return _relation_out(created)


@router.post("/decisions", response_model=DecisionOut, status_code=status.HTTP_201_CREATED)
def record_decision(principal: ProposerDep, service: ServiceDep, body: DecisionIn) -> DecisionOut:
    """Record a decision and the facts it rested on (append-only)."""
    with _world_errors():
        decision = service.record_decision(
            principal.workspace_id,
            title=body.title,
            outcome=body.outcome,
            actor=_actor(principal),
            fact_ids=body.fact_ids,
            links=body.links,
            as_of=body.as_of,
            at=body.at,
        )
    _audit(
        service,
        principal,
        "decision_recorded",
        target_type="world_decision",
        target_id=decision.id,
        details={"title": _clip(decision.title), "fact_count": len(decision.fact_ids)},
    )
    service.session.commit()
    return _decision_out(decision)


@router.get("/decisions/{decision_id}", response_model=DecisionOut)
def get_decision(principal: ReaderDep, service: ServiceDep, decision_id: uuid.UUID) -> DecisionOut:
    with _world_errors():
        decision = service.get_decision(principal.workspace_id, decision_id)
    return _decision_out(decision)


@router.get("/decisions/{decision_id}/drift", response_model=DriftOut)
def get_decision_drift(
    principal: ReaderDep, service: ServiceDep, decision_id: uuid.UUID
) -> DriftOut:
    """How the facts this decision rested on stand now."""
    with _world_errors():
        report = service.decision_drift(principal.workspace_id, decision_id)
    return DriftOut(
        decision_id=decision_id,
        drifted=report.drifted,
        items=[
            DriftItemOut(
                fact_id=item.fact_id,
                status=item.status,
                reason=item.reason,
                superseded_by=item.superseded_by,
            )
            for item in report.items
        ],
    )


__all__ = ["router"]
