"""Persistence boundary for approval gates.

:class:`ApprovalRepository` is the Protocol the service depends on; the
in-memory implementation here follows the foundation precedent (the apps wire
services in-memory; the DB-backed repository swaps in behind the same boundary
— the canonical tables ship in ``scf_db.models.approval``).
"""

from __future__ import annotations

import builtins
import threading
import uuid
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from scf_approval.models import (
    ApprovalDecisionRecord,
    ApprovalRequest,
    GateStatus,
    GateType,
)


class ApprovalNotFoundError(LookupError):
    """Unknown approval id in this workspace (maps to HTTP 404 — no leak)."""

    def __init__(self, approval_id: uuid.UUID) -> None:
        self.approval_id = approval_id
        super().__init__(f"no approval request {approval_id}")


class DuplicateDecisionError(ValueError):
    """The approver already voted on this gate (maps to HTTP 409)."""

    def __init__(self, approval_id: uuid.UUID, approver_user_id: uuid.UUID) -> None:
        self.approval_id = approval_id
        self.approver_user_id = approver_user_id
        super().__init__(f"approver {approver_user_id} already voted on approval {approval_id}")


class AlreadyResolvedError(ValueError):
    """The gate is no longer pending (maps to HTTP 409)."""

    def __init__(self, approval_id: uuid.UUID, status: GateStatus) -> None:
        self.approval_id = approval_id
        self.status = status
        super().__init__(f"approval {approval_id} is already {status.value}")


@runtime_checkable
class ApprovalRepository(Protocol):
    """Storage boundary — all reads are workspace-scoped (tenant isolation)."""

    async def add(self, request: ApprovalRequest) -> ApprovalRequest: ...

    async def get(
        self, approval_id: uuid.UUID, *, workspace_id: uuid.UUID
    ) -> ApprovalRequest | None: ...

    async def find_pending(
        self,
        *,
        workspace_id: uuid.UUID,
        subject_type: str,
        subject_id: uuid.UUID | None,
        gate_type: GateType,
    ) -> ApprovalRequest | None: ...

    async def list(
        self,
        *,
        workspace_id: uuid.UUID,
        status: GateStatus | None = None,
        gate_type: GateType | None = None,
        project_id: uuid.UUID | None = None,
    ) -> list[ApprovalRequest]: ...

    async def update(
        self, request: ApprovalRequest, *, from_status: GateStatus | None = None
    ) -> ApprovalRequest:
        """Write ``request``. With ``from_status``, only while the stored status
        still is that, else :class:`AlreadyResolvedError` (a decision never
        overwrites an outcome recorded meanwhile)."""
        ...

    async def add_decision(self, record: ApprovalDecisionRecord) -> ApprovalDecisionRecord: ...

    async def decisions_for(
        self, approval_id: uuid.UUID
    ) -> builtins.list[ApprovalDecisionRecord]: ...

    async def held_action_decisions(
        self,
        *,
        workspace_id: uuid.UUID,
        action_type: str,
        limit: int,
        exclude: uuid.UUID | None = None,
    ) -> builtins.list[ApprovalDecisionRecord]:
        """Decisions on this workspace's ``automation_action`` requests for
        ``action_type`` (``exclude``'s left out), newest first, at most ``limit``."""
        ...


class InMemoryApprovalRepository:
    """Hermetic repository honouring the DB invariants (pending-unique,
    one-vote-per-approver, append-only decisions)."""

    def __init__(self) -> None:
        self._items: dict[uuid.UUID, ApprovalRequest] = {}
        self._decisions: dict[uuid.UUID, list[ApprovalDecisionRecord]] = {}
        self._lock = threading.Lock()

    async def add(self, request: ApprovalRequest) -> ApprovalRequest:
        stored = request.model_copy(deep=True)
        if stored.requested_at is None:
            stored.requested_at = datetime.now(UTC)
        with self._lock:
            self._items[stored.id] = stored
        return stored.model_copy(deep=True)

    async def get(
        self, approval_id: uuid.UUID, *, workspace_id: uuid.UUID
    ) -> ApprovalRequest | None:
        item = self._items.get(approval_id)
        if item is None or item.workspace_id != workspace_id:
            return None
        return item.model_copy(deep=True)

    async def find_pending(
        self,
        *,
        workspace_id: uuid.UUID,
        subject_type: str,
        subject_id: uuid.UUID | None,
        gate_type: GateType,
    ) -> ApprovalRequest | None:
        if subject_id is None:
            return None
        for item in self._items.values():
            if (
                item.workspace_id == workspace_id
                and item.status is GateStatus.PENDING
                and item.subject_type == subject_type
                and item.subject_id == subject_id
                and item.gate_type is gate_type
            ):
                return item.model_copy(deep=True)
        return None

    async def list(
        self,
        *,
        workspace_id: uuid.UUID,
        status: GateStatus | None = None,
        gate_type: GateType | None = None,
        project_id: uuid.UUID | None = None,
    ) -> list[ApprovalRequest]:
        rows = [i for i in self._items.values() if i.workspace_id == workspace_id]
        if status is not None:
            rows = [i for i in rows if i.status is status]
        if gate_type is not None:
            rows = [i for i in rows if i.gate_type is gate_type]
        if project_id is not None:
            rows = [i for i in rows if i.project_id == project_id]
        return [i.model_copy(deep=True) for i in rows]

    async def update(
        self, request: ApprovalRequest, *, from_status: GateStatus | None = None
    ) -> ApprovalRequest:
        with self._lock:
            if request.id not in self._items:
                raise ApprovalNotFoundError(request.id)
            stored = self._items[request.id].status
            if from_status is not None and stored is not from_status:
                raise AlreadyResolvedError(request.id, stored)
            self._items[request.id] = request.model_copy(deep=True)
        return request.model_copy(deep=True)

    async def add_decision(self, record: ApprovalDecisionRecord) -> ApprovalDecisionRecord:
        stored = record.model_copy(deep=True)
        if stored.created_at is None:
            stored.created_at = datetime.now(UTC)
        with self._lock:
            votes = self._decisions.setdefault(stored.approval_request_id, [])
            if any(v.approver_user_id == stored.approver_user_id for v in votes):
                raise DuplicateDecisionError(stored.approval_request_id, stored.approver_user_id)
            votes.append(stored)
        return stored.model_copy(deep=True)

    async def decisions_for(self, approval_id: uuid.UUID) -> builtins.list[ApprovalDecisionRecord]:
        return [v.model_copy(deep=True) for v in self._decisions.get(approval_id, [])]

    async def held_action_decisions(
        self,
        *,
        workspace_id: uuid.UUID,
        action_type: str,
        limit: int,
        exclude: uuid.UUID | None = None,
    ) -> builtins.list[ApprovalDecisionRecord]:
        held = [
            i.id
            for i in self._items.values()
            if i.workspace_id == workspace_id
            and i.gate_type is GateType.AUTOMATION_ACTION
            and i.gate_payload.get("action_type") == action_type
            and i.id != exclude
        ]
        votes = [v for request_id in held for v in self._decisions.get(request_id, [])]
        # Insertion order breaks equal timestamps, so "newest first" is total.
        ranked = sorted(enumerate(votes), key=lambda p: (p[1].created_at, p[0]), reverse=True)
        return [v.model_copy(deep=True) for _, v in ranked[:limit]]


__all__ = [
    "AlreadyResolvedError",
    "ApprovalNotFoundError",
    "ApprovalRepository",
    "DuplicateDecisionError",
    "InMemoryApprovalRepository",
]
