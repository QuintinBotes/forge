"""Attested Changesets read-only REST surface (Task 19).

* ``GET /attestations``                    — workspace-scoped page, newest first
* ``GET /attestations/{id}``               — one record (foreign ids look nonexistent)
* ``GET /approvals/{id}/attestation``      — the record minted when the gate's
  linked workflow run was attested (404 when none)

Read-only by design: attestations are minted exclusively as a side effect of
approving a ``pr`` gate (``PrAttestationResolutionHook``); there is no HTTP
route that creates one, mirroring the audit router's producer/consumer split.
Reads go straight through the append-only ``AttestationRepository`` scoped to
the caller's workspace on the row itself (the red-team surface's convention).

``verified`` is computed per record by the exact seam ``forge-verify --run``
uses (:func:`forge_api.cli_verify.verify_stored_attestation`): re-derive
``payload_hash`` from the envelope's PAE and Ed25519-verify against the
deployment's verification key (the public half of ``FORGE_ATTEST_SIGNING_KEY``,
resolved once per request). Signature verification is never re-implemented
here, so the REST answer and the CLI's exit code can never disagree.

Auth/tenancy mirror the F36 approvals router exactly: every route hangs off the
authenticated principal (READ permission) and scopes by
``principal.workspace_id``; cross-workspace ids map to ``404`` (no existence
leak).
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from forge_api.auth.rbac import Permission
from forge_api.cli_verify import resolve_verification_key, verify_stored_attestation
from forge_api.db import get_db
from forge_api.deps import Principal, get_current_principal
from forge_api.routers._rbac import require_permission
from forge_api.schemas.attestation import (
    AttestationListResponse,
    AttestationOut,
    AttestationProvenance,
)
from forge_api.services.approval_service import get_approval_service
from forge_approval import ApprovalNotFoundError, ApprovalService
from forge_contracts.attestation import DsseEnvelope
from forge_db.attest.repository import AttestationRepository
from forge_db.models import Attestation

router = APIRouter(tags=["attestations"], dependencies=[Depends(get_current_principal)])

ReaderDep = Annotated[Principal, Depends(require_permission(Permission.READ))]
SessionDep = Annotated[Session, Depends(get_db)]
ApprovalServiceDep = Annotated[ApprovalService, Depends(get_approval_service)]


def _to_out(row: Attestation, *, public_key_b64: str) -> AttestationOut:
    """Map one ORM row onto the response schema, verifying it live.

    Verification is total (a malformed envelope is "not verified", never a
    500): both the envelope re-validation and the shared seam degrade to
    ``verified=False`` on any decode failure.
    """
    try:
        envelope = DsseEnvelope.model_validate(row.envelope)
    except ValueError:
        verified = False
    else:
        verified = verify_stored_attestation(
            envelope, row.payload_hash, public_key_b64=public_key_b64
        ).ok
    return AttestationOut(
        id=row.id,
        changeset_hash=row.subject_digest,
        predicate_type=row.predicate_type,
        keyid=row.keyid,
        payload_hash=row.payload_hash,
        created_at=row.created_at,
        verified=verified,
        provenance=AttestationProvenance.model_validate(row),
    )


def _not_found(attestation_id: uuid.UUID) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND, detail=f"no attestation {attestation_id}"
    )


@router.get("/attestations", response_model=AttestationListResponse)
def list_attestations(
    principal: ReaderDep,
    session: SessionDep,
    workflow_run_id: Annotated[uuid.UUID | None, Query()] = None,
    agent_run_id: Annotated[uuid.UUID | None, Query()] = None,
    spec_key: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AttestationListResponse:
    """One workspace-scoped page of attestations, newest first."""
    rows = AttestationRepository(session).list(
        principal.workspace_id,
        workflow_run_id=workflow_run_id,
        agent_run_id=agent_run_id,
        spec_key=spec_key,
        limit=limit,
        offset=offset,
    )
    public_key = resolve_verification_key()
    return AttestationListResponse(
        items=[_to_out(row, public_key_b64=public_key) for row in rows],
        limit=limit,
        offset=offset,
    )


@router.get("/attestations/{attestation_id}", response_model=AttestationOut)
def get_attestation(
    principal: ReaderDep, session: SessionDep, attestation_id: uuid.UUID
) -> AttestationOut:
    """One attestation (workspace-scoped; cross-workspace ids look nonexistent)."""
    row = session.get(Attestation, attestation_id)
    if row is None or row.workspace_id != principal.workspace_id:
        raise _not_found(attestation_id)
    return _to_out(row, public_key_b64=resolve_verification_key())


@router.get("/approvals/{approval_id}/attestation", response_model=AttestationOut)
async def get_approval_attestation(
    principal: ReaderDep,
    session: SessionDep,
    service: ApprovalServiceDep,
    approval_id: uuid.UUID,
) -> AttestationOut:
    """The attestation minted for the gate's linked workflow run (404 when none).

    Resolves the gate through the same workspace-scoped ``ApprovalService.get``
    the approvals router uses (foreign gate ids 404 identically), then reads the
    newest attestation for its ``workflow_run_id``. A gate without a linked run,
    or whose run was never attested (e.g. still pending), is an honest 404 —
    the UI renders that as "not attested", never a fake state.
    """
    try:
        request = await service.get(approval_id, workspace_id=principal.workspace_id)
    except ApprovalNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"no approval request {approval_id}"
        ) from None

    row = (
        AttestationRepository(session).get_by_run(
            principal.workspace_id, workflow_run_id=request.workflow_run_id
        )
        if request.workflow_run_id is not None
        else None
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no attestation recorded for approval {approval_id}",
        )
    return _to_out(row, public_key_b64=resolve_verification_key())


__all__ = ["router"]
