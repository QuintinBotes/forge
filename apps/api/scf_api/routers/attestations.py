"""Attested Changesets read-only REST surface (Task 19).

* ``GET /attestations``                    — workspace-scoped page, newest first
* ``GET /attestations/{id}``               — one record (foreign ids look nonexistent)
* ``GET /attestations/{id}/envelope``      — its signed DSSE envelope, exactly as stored,
  for ``scf-verify --attestation`` and other DSSE/in-toto tooling (#214)
* ``GET /approvals/{id}/attestation``      — the record minted when the gate's
  linked workflow run was attested (404 when none)

Read-only by design: attestations are minted exclusively as a side effect of
approving a ``pr`` gate (``PrAttestationResolutionHook``); there is no HTTP
route that creates one, mirroring the audit router's producer/consumer split.
Reads go straight through the append-only ``AttestationRepository`` scoped to
the caller's workspace on the row itself (the red-team surface's convention).

``verified`` is computed per record by the exact seam ``scf-verify --run``
uses (:func:`scf_api.cli_verify.verify_stored_attestation`): re-derive
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
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from scf_api.auth.rbac import Permission
from scf_api.cli_verify import resolve_verification_key, verify_stored_attestation
from scf_api.db import get_db
from scf_api.deps import Principal, get_current_principal
from scf_api.routers._rbac import require_permission
from scf_api.schemas.attestation import (
    AttestationListResponse,
    AttestationOut,
    AttestationProvenance,
)
from scf_api.services.approval_service import get_approval_service
from scf_approval import ApprovalNotFoundError, ApprovalService
from scf_auth.errors import KeyMaterialError
from scf_contracts.attestation import DsseEnvelope, decode_statement
from scf_db.attest.repository import AttestationRepository
from scf_db.models import Attestation

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
    public_key = _verification_key(session)
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
    return _to_out(row, public_key_b64=_verification_key(session))


@router.get("/attestations/{attestation_id}/envelope", response_model=DsseEnvelope)
def get_attestation_envelope(
    principal: ReaderDep, session: SessionDep, attestation_id: uuid.UUID
) -> dict[str, Any]:
    """The stored DSSE envelope, unchanged, so it verifies offline with the public key alone.

    Workspace-scoped like the record itself: a foreign id is a 404, never a 403.
    """
    row = session.get(Attestation, attestation_id)
    if row is None or row.workspace_id != principal.workspace_id:
        raise _not_found(attestation_id)
    return dict(row.envelope)


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
    newest attestation for its ``workflow_run_id`` that names an approver, else
    its newest (the one signed when the pull request opened, #175). A gate without
    a linked run, or whose run was never attested, is an honest 404 — the UI
    renders that as "not attested", never a fake state.
    """
    try:
        request = await service.get(approval_id, workspace_id=principal.workspace_id)
    except ApprovalNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"no approval request {approval_id}"
        ) from None

    rows = (
        AttestationRepository(session).list(
            principal.workspace_id, workflow_run_id=request.workflow_run_id
        )
        if request.workflow_run_id is not None
        else []
    )
    row = next((r for r in rows if _names_an_approver(r)), rows[0] if rows else None)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no attestation recorded for approval {approval_id}",
        )
    return _to_out(row, public_key_b64=_verification_key(session))


def _verification_key(session: Session) -> str:
    """The deployment's public key, or ``""`` when there is none: then nothing verifies."""
    try:
        return resolve_verification_key(session=session)
    except KeyMaterialError:
        return ""


def _names_an_approver(row: Attestation) -> bool:
    try:
        statement = decode_statement(dict(row.envelope or {}).get("payload", ""))
    except (ValueError, TypeError):
        return False
    return statement.predicate.get("human_approver") is not None


__all__ = ["router"]
