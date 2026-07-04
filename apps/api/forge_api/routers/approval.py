"""Approval router — the human-in-the-loop gate surface (wired in Phase 2 Task 2.1).

Serves the approval queue over HTTP:

* ``POST /approval/requests``                 — open an approval request (a gate
  raised by the workflow/agent layer when a task needs human sign-off).
* ``GET  /approval/requests``                 — list pending/decided requests.
* ``GET  /approval/requests/{approval_id}``   — fetch one request (full context).
* ``POST /approval/requests/{approval_id}/decision`` — approve / reject /
  request changes.

Handlers delegate to a process-wide in-memory :class:`ApprovalStore` (the
DB-backed store is swapped in behind the same dependency via
``app.dependency_overrides`` / config). Unknown ids map to HTTP 404.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from functools import lru_cache
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from forge_api.auth.rbac import Permission
from forge_api.deps import Principal, get_current_principal
from forge_api.routers._rbac import require_permission
from forge_contracts import ApprovalRequest
from forge_contracts.enums import ApprovalStatus

router = APIRouter(
    prefix="/approval",
    tags=["approval"],
    dependencies=[Depends(get_current_principal)],
)

# Permission-gated principals (authenticate + authorize, returning the principal
# so handlers can scope by workspace and record the decider identity). Opening
# and deciding a gate are WRITE operations — a read-only ``viewer`` and the
# ``agent-runner`` (which lacks WRITE) are therefore denied, preserving the
# human-in-the-loop guarantee that an agent cannot approve its own gate.
ReaderDep = Annotated[Principal, Depends(require_permission(Permission.READ))]
WriterDep = Annotated[Principal, Depends(require_permission(Permission.WRITE))]


# --------------------------------------------------------------------------- #
# In-memory approval store                                                     #
# --------------------------------------------------------------------------- #


class ApprovalStore:
    """A tiny in-memory store of approval requests (keyed by id).

    Each request is tagged with the ``workspace_id`` that created it; every read
    and decision is scoped to a workspace so one tenant can never see, fetch, or
    decide another tenant's gates. ``ApprovalRequest`` carries no ``workspace_id``
    field (it is a frozen contract), so ownership is tracked alongside the items.
    """

    def __init__(self) -> None:
        self._items: dict[uuid.UUID, ApprovalRequest] = {}
        self._owner: dict[uuid.UUID, uuid.UUID] = {}

    def create(self, request: ApprovalRequest, *, workspace_id: uuid.UUID) -> ApprovalRequest:
        if request.id is None:
            request.id = uuid.uuid4()
        if request.created_at is None:
            request.created_at = datetime.now(UTC)
        self._items[request.id] = request
        self._owner[request.id] = workspace_id
        return request

    def list(
        self, *, workspace_id: uuid.UUID, status: ApprovalStatus | None = None
    ) -> list[ApprovalRequest]:
        items = [req for key, req in self._items.items() if self._owner.get(key) == workspace_id]
        if status is not None:
            items = [i for i in items if i.status == status]
        return items

    def get(self, approval_id: uuid.UUID, *, workspace_id: uuid.UUID) -> ApprovalRequest | None:
        if self._owner.get(approval_id) != workspace_id:
            return None
        return self._items.get(approval_id)

    def owner_of(self, approval_id: uuid.UUID) -> uuid.UUID | None:
        """Return the workspace that owns ``approval_id``, or ``None`` if unknown.

        Read-only, cross-tenant-safe resolution used by the Slack interactivity
        handler: a Slack ``block_actions`` callback is unauthenticated (it carries
        no Forge principal) and embeds only the approval id, so the handler must
        resolve the owning workspace before applying the decision through the
        normal workspace-scoped :meth:`decide` path. An unknown id yields
        ``None`` -> the handler no-ops (never leaks another tenant's gate).
        """
        return self._owner.get(approval_id)

    def decide(
        self,
        approval_id: uuid.UUID,
        *,
        workspace_id: uuid.UUID,
        status: ApprovalStatus,
        decided_by: str | None,
        reason: str | None,
    ) -> ApprovalRequest | None:
        request = self.get(approval_id, workspace_id=workspace_id)
        if request is None:
            return None
        request.status = status
        request.decided_by = decided_by
        request.decision_reason = reason
        request.decided_at = datetime.now(UTC)
        return request


@lru_cache(maxsize=1)
def _approval_store_singleton() -> ApprovalStore:
    return ApprovalStore()


def get_approval_store() -> ApprovalStore:
    """Return the process-wide approval store (override in tests via DI)."""
    return _approval_store_singleton()


StoreDep = Annotated[ApprovalStore, Depends(get_approval_store)]


# --------------------------------------------------------------------------- #
# Request bodies                                                              #
# --------------------------------------------------------------------------- #


class DecisionRequest(BaseModel):
    """Body for ``POST /approval/requests/{approval_id}/decision``.

    There is deliberately **no** ``decided_by`` field: the decider identity is
    taken from the authenticated principal, never from the request body, so the
    gate's accountability record cannot be forged.
    """

    status: ApprovalStatus
    reason: str | None = None


def _decider_identity(principal: Principal) -> str:
    """Stable, human-readable identity for the authenticated decider."""
    return principal.email or str(principal.user_id)


def _require(request: ApprovalRequest | None, approval_id: uuid.UUID) -> ApprovalRequest:
    if request is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"no approval request {approval_id}"
        )
    return request


# --------------------------------------------------------------------------- #
# Routes                                                                      #
# --------------------------------------------------------------------------- #


@router.post("/requests", response_model=ApprovalRequest, status_code=status.HTTP_201_CREATED)
def create_request(
    store: StoreDep, principal: WriterDep, request: ApprovalRequest
) -> ApprovalRequest:
    """Open an approval request in the caller's workspace."""
    return store.create(request, workspace_id=principal.workspace_id)


@router.get("/requests", response_model=list[ApprovalRequest])
def list_requests(
    store: StoreDep,
    principal: ReaderDep,
    status: Annotated[ApprovalStatus | None, Query()] = None,
) -> list[ApprovalRequest]:
    """List the caller workspace's approval requests (optionally by status)."""
    return store.list(workspace_id=principal.workspace_id, status=status)


@router.get("/requests/{approval_id}", response_model=ApprovalRequest)
def get_request(store: StoreDep, principal: ReaderDep, approval_id: uuid.UUID) -> ApprovalRequest:
    """Fetch one approval request (only within the caller's workspace)."""
    return _require(store.get(approval_id, workspace_id=principal.workspace_id), approval_id)


@router.post("/requests/{approval_id}/decision", response_model=ApprovalRequest)
def decide(
    store: StoreDep,
    principal: WriterDep,
    approval_id: uuid.UUID,
    payload: DecisionRequest,
) -> ApprovalRequest:
    """Approve / reject / request changes on an approval request.

    The decider identity is the authenticated principal (WRITE-capable: a human
    ``member``/``admin``); the read-only ``viewer`` and the ``agent-runner`` are
    rejected upstream by the WRITE gate, so an agent cannot decide its own gate.
    """
    decided = store.decide(
        approval_id,
        workspace_id=principal.workspace_id,
        status=payload.status,
        decided_by=_decider_identity(principal),
        reason=payload.reason,
    )
    return _require(decided, approval_id)


__all__ = ["ApprovalStore", "DecisionRequest", "get_approval_store", "router"]
