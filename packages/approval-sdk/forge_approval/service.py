"""``ApprovalService`` — the one service every gate type routes through.

Orchestrates repository + authorizer + registry + activity bus + audit +
redaction. Any slice creates a gate with :meth:`create`; any surface resolves
through :meth:`resolve` (identical authorization + audit everywhere).
"""

from __future__ import annotations

import builtins
import uuid
from datetime import UTC, datetime
from typing import Any

from forge_approval.audit import ApprovalAuditSink, NullAuditSink
from forge_approval.authorizer import ApprovalAuthorizer, AuthorizationError
from forge_approval.events import (
    APPROVAL_REQUESTED_TOPIC,
    APPROVAL_RESOLVED_TOPIC,
    ActivityBus,
    ApprovalRequestedEvent,
    ApprovalResolvedEvent,
    InMemoryActivityBus,
)
from forge_approval.models import (
    ApprovalAction,
    ApprovalContext,
    ApprovalDecisionRecord,
    ApprovalDecisionRequest,
    ApprovalRequest,
    ApprovalResolution,
    ApprovalSummary,
    GateStatus,
    GateType,
    Principal,
    ResolutionOutcome,
    RiskLevel,
    risk_rank,
)
from forge_approval.redaction import Redactor, passthrough_redactor
from forge_approval.registry import GateRegistry, MissingProviderError, default_actions
from forge_approval.repository import (
    AlreadyResolvedError,
    ApprovalNotFoundError,
    ApprovalRepository,
)


class ApprovalService:
    """Create, list, contextualize, and resolve approval gates (all six types)."""

    def __init__(
        self,
        repo: ApprovalRepository,
        registry: GateRegistry,
        authorizer: ApprovalAuthorizer,
        events: ActivityBus | None = None,
        audit: ApprovalAuditSink | None = None,
        redactor: Redactor | None = None,
    ) -> None:
        self._repo = repo
        self._registry = registry
        self._authorizer = authorizer
        self._events = events or InMemoryActivityBus()
        self._audit = audit or NullAuditSink()
        self._redact = redactor or passthrough_redactor

    # ------------------------------------------------------------------ #
    # create                                                              #
    # ------------------------------------------------------------------ #

    async def create(
        self,
        *,
        workspace_id: uuid.UUID,
        gate_type: GateType,
        subject_type: str,
        subject_id: uuid.UUID | None,
        workflow_run_id: uuid.UUID | None = None,
        agent_run_id: uuid.UUID | None = None,
        task_id: uuid.UUID | None = None,
        project_id: uuid.UUID | None = None,
        requested_by: uuid.UUID | None = None,
        requested_actor: str = "system",
        required_approvals: int = 1,
        risk_level: RiskLevel = "info",
        title: str | None = None,
        gate_payload: dict[str, Any] | None = None,
        expires_at: datetime | None = None,
    ) -> ApprovalRequest:
        """Open a gate; idempotent while a matching gate is still pending.

        Returns the existing pending gate for the same
        ``(subject_type, subject_id, gate_type)`` instead of a duplicate (the
        ``uq_pending_gate`` partial-unique invariant) — no second row, no
        second event. ``gate_payload`` is secret-redacted before persist.
        """
        existing = await self._repo.find_pending(
            workspace_id=workspace_id,
            subject_type=subject_type,
            subject_id=subject_id,
            gate_type=gate_type,
        )
        if existing is not None:
            return existing

        request = ApprovalRequest(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            project_id=project_id,
            gate_type=gate_type,
            status=GateStatus.PENDING,
            subject_type=subject_type,
            subject_id=subject_id,
            workflow_run_id=workflow_run_id,
            agent_run_id=agent_run_id,
            task_id=task_id,
            required_approvals=required_approvals,
            risk_level=risk_level,
            title=title,
            gate_payload=self._redact(gate_payload or {}),
            requested_by=requested_by,
            requested_actor=requested_actor,
            expires_at=expires_at,
            requested_at=datetime.now(UTC),
        )
        stored = await self._repo.add(request)

        self._audit.record_approval(
            stored.gate_type.value,
            status="requested",
            actor=stored.requested_actor,
            run_id=stored.workflow_run_id or stored.agent_run_id,
            request_id=stored.id,
            detail=stored.title,
        )
        self._events.publish(
            APPROVAL_REQUESTED_TOPIC,
            ApprovalRequestedEvent(
                approval_id=stored.id,
                workspace_id=stored.workspace_id,
                project_id=stored.project_id,
                gate_type=stored.gate_type,
                subject_type=stored.subject_type,
                subject_id=stored.subject_id,
                risk_level=stored.risk_level,
                requested_actor=stored.requested_actor,
                requested_at=stored.requested_at,
            ),
        )
        return stored

    # ------------------------------------------------------------------ #
    # read                                                                #
    # ------------------------------------------------------------------ #

    async def get(
        self, approval_id: uuid.UUID, *, workspace_id: uuid.UUID
    ) -> ApprovalRequest:
        """Workspace-scoped fetch; cross-workspace ids look nonexistent (404)."""
        request = await self._repo.get(approval_id, workspace_id=workspace_id)
        if request is None:
            raise ApprovalNotFoundError(approval_id)
        return request

    async def list(
        self,
        *,
        workspace_id: uuid.UUID,
        actor: Principal,
        status: GateStatus | None = None,
        gate_type: GateType | None = None,
        project_id: uuid.UUID | None = None,
        mine: bool = False,
    ) -> list[ApprovalSummary]:
        """Inbox listing: workspace-scoped, ``critical`` risk first (AC#18).

        ``mine=True`` keeps only gates the actor is authorized to resolve.
        """
        rows = await self._repo.list(
            workspace_id=workspace_id,
            status=status,
            gate_type=gate_type,
            project_id=project_id,
        )
        if mine:
            rows = [r for r in rows if self._can_resolve(actor, r)]
        rows.sort(
            key=lambda r: (
                -risk_rank(r.risk_level),
                r.requested_at or datetime.now(UTC),
            )
        )
        return [
            ApprovalSummary(
                id=r.id,
                gate_type=r.gate_type,
                status=r.status,
                title=r.title or f"{r.gate_type.value} approval",
                project_id=r.project_id,
                risk_level=r.risk_level,
                requested_actor=r.requested_actor,
                requested_at=r.requested_at,
            )
            for r in rows
        ]

    async def count(
        self,
        *,
        workspace_id: uuid.UUID,
        actor: Principal,
        status: GateStatus = GateStatus.PENDING,
        mine: bool = False,
    ) -> int:
        """Nav-badge count — matches the inbox length by construction."""
        return len(
            await self.list(
                workspace_id=workspace_id, actor=actor, status=status, mine=mine
            )
        )

    async def get_context(
        self, approval_id: uuid.UUID, *, workspace_id: uuid.UUID, session: Any = None
    ) -> ApprovalContext:
        """The nine "must-show" items, built by the registered provider.

        A gate whose provider is not registered degrades to a read-only
        fallback context (never a crash) — slice risk #3.
        """
        request = await self.get(approval_id, workspace_id=workspace_id)
        try:
            provider = self._registry.provider(request.gate_type)
        except MissingProviderError:
            return self._fallback_context(request)
        context = await provider.build_context(request, session=session)
        context.gate_payload = self._redact(context.gate_payload)
        return context

    async def decisions(
        self, approval_id: uuid.UUID, *, workspace_id: uuid.UUID
    ) -> builtins.list[ApprovalDecisionRecord]:
        await self.get(approval_id, workspace_id=workspace_id)  # 404 if cross-tenant
        return await self._repo.decisions_for(approval_id)

    # ------------------------------------------------------------------ #
    # resolve                                                             #
    # ------------------------------------------------------------------ #

    async def resolve(
        self,
        approval_id: uuid.UUID,
        decision: ApprovalDecisionRequest,
        actor: Principal,
        *,
        workspace_id: uuid.UUID,
        session: Any = None,
    ) -> ApprovalResolution:
        """Record one reviewer's decision and run the gate's resolution hook.

        1. load (workspace-scoped; cross-workspace => not-found, never 403);
        2. reject already-resolved gates;
        3. ``authorizer.check`` — the single policy;
        4. persist the (unique-per-approver) decision row;
        5. derive gate status (escalate keeps pending, raises the bar);
        6. invoke the gate's hook; fold its :class:`ResolutionOutcome` in;
        7. audit + emit ``approval.resolved``.
        """
        request = await self.get(approval_id, workspace_id=workspace_id)
        if request.status is not GateStatus.PENDING:
            raise AlreadyResolvedError(approval_id, request.status)

        self._authorizer.check(actor, request, decision)

        assert actor.id is not None  # guaranteed: authorizer requires kind == "user"
        await self._repo.add_decision(
            ApprovalDecisionRecord(
                approval_request_id=request.id,
                approver_user_id=actor.id,
                decision=decision.decision,
                note=decision.note,
            )
        )

        if decision.decision is ApprovalAction.ESCALATE:
            return await self._escalate(request, decision, actor)

        now = datetime.now(UTC)
        request.status = _STATUS_FOR_ACTION[decision.decision]
        request.resolved_at = now
        request.resolver_user_id = actor.id
        request.decision_note = decision.note
        request = await self._repo.update(request)

        hook = self._registry.hook(request.gate_type)
        if hook is None:
            outcome = ResolutionOutcome(
                completed=False,
                details={"result": "not_implemented"},
            )
        else:
            outcome = await hook.on_resolved(request, decision, actor, session=session)

        self._audit.record_approval(
            request.gate_type.value,
            status=request.status.value,
            actor=actor.actor_ref,
            run_id=request.workflow_run_id or request.agent_run_id,
            request_id=request.id,
            detail=decision.note,
        )
        self._events.publish(
            APPROVAL_RESOLVED_TOPIC,
            ApprovalResolvedEvent(
                approval_id=request.id,
                workspace_id=request.workspace_id,
                gate_type=request.gate_type,
                status=request.status,
                resolver_user_id=request.resolver_user_id,
                outcome=outcome,
                resolved_at=request.resolved_at,
            ),
        )
        return ApprovalResolution(
            approval_id=request.id, status=request.status, outcome=outcome
        )

    async def expire_pending(
        self, *, now: datetime | None = None
    ) -> builtins.list[ApprovalResolution]:
        """Mark pending gates past ``expires_at`` as ``expired`` (SLA sweep).

        Emits ``approval.resolved(status=expired)`` + audit per gate; routing
        the subject workflow is the gate hook's concern downstream.
        """
        now = now or datetime.now(UTC)
        resolutions: list[ApprovalResolution] = []
        # Sweep across workspaces: repository list is workspace-scoped, so the
        # sweep iterates the known workspaces from pending rows.
        for request in await self._all_pending():
            if request.expires_at is None or request.expires_at > now:
                continue
            request.status = GateStatus.EXPIRED
            request.resolved_at = now
            await self._repo.update(request)
            outcome = ResolutionOutcome(
                completed=False,
                blocking_reasons=["approval expired before a decision"],
                follow_up_state="needs_human_input",
            )
            self._audit.record_approval(
                request.gate_type.value,
                status=GateStatus.EXPIRED.value,
                actor="system",
                run_id=request.workflow_run_id or request.agent_run_id,
                request_id=request.id,
            )
            self._events.publish(
                APPROVAL_RESOLVED_TOPIC,
                ApprovalResolvedEvent(
                    approval_id=request.id,
                    workspace_id=request.workspace_id,
                    gate_type=request.gate_type,
                    status=GateStatus.EXPIRED,
                    resolver_user_id=None,
                    outcome=outcome,
                    resolved_at=now,
                ),
            )
            resolutions.append(
                ApprovalResolution(
                    approval_id=request.id, status=GateStatus.EXPIRED, outcome=outcome
                )
            )
        return resolutions

    # ------------------------------------------------------------------ #
    # helpers                                                             #
    # ------------------------------------------------------------------ #

    def available_actions(self, request: ApprovalRequest) -> builtins.list[ApprovalAction]:
        """Gate-correct actions (provider override, else the registry default)."""
        if self._registry.has_provider(request.gate_type):
            return self._registry.provider(request.gate_type).available_actions(request)
        return default_actions(request.gate_type)

    def _can_resolve(self, actor: Principal, request: ApprovalRequest) -> bool:
        try:
            self._authorizer.check(
                actor, request, ApprovalDecisionRequest(decision=ApprovalAction.APPROVE)
            )
        except AuthorizationError:
            return False
        return True

    def _fallback_context(self, request: ApprovalRequest) -> ApprovalContext:
        """Read-only degradation for gates whose provider is not registered."""
        return ApprovalContext(
            approval_id=request.id,
            gate_type=request.gate_type,
            goal=request.title or f"{request.gate_type.value} approval",
            risk_flags=[],
            run_trace_ref=(
                {
                    "workflow_run_id": str(request.workflow_run_id)
                    if request.workflow_run_id
                    else None,
                    "agent_run_id": str(request.agent_run_id)
                    if request.agent_run_id
                    else None,
                }
                if (request.workflow_run_id or request.agent_run_id)
                else None
            ),
            available_actions=default_actions(request.gate_type),
            gate_payload=self._redact(request.gate_payload),
        )

    async def _escalate(
        self,
        request: ApprovalRequest,
        decision: ApprovalDecisionRequest,
        actor: Principal,
    ) -> ApprovalResolution:
        """Escalate: keep pending, raise risk to critical + required role to admin."""
        request.risk_level = "critical"
        request.escalated = True
        request = await self._repo.update(request)
        self._audit.record_approval(
            request.gate_type.value,
            status="escalated",
            actor=actor.actor_ref,
            run_id=request.workflow_run_id or request.agent_run_id,
            request_id=request.id,
            detail=decision.note,
        )
        return ApprovalResolution(
            approval_id=request.id,
            status=GateStatus.PENDING,
            outcome=ResolutionOutcome(
                completed=True,
                follow_up_state="escalated_to_admin",
                details={"escalated": True, "risk_level": request.risk_level},
            ),
        )

    async def _all_pending(self) -> builtins.list[ApprovalRequest]:
        """Every pending gate across workspaces (in-memory sweep support)."""
        items = getattr(self._repo, "_items", None)
        if items is None:  # pragma: no cover — DB-backed repos sweep in the worker
            return []
        return [
            i.model_copy(deep=True)
            for i in items.values()
            if i.status is GateStatus.PENDING
        ]


_STATUS_FOR_ACTION: dict[ApprovalAction, GateStatus] = {
    ApprovalAction.APPROVE: GateStatus.APPROVED,
    ApprovalAction.REJECT: GateStatus.REJECTED,
    ApprovalAction.REQUEST_CHANGES: GateStatus.CHANGES_REQUESTED,
}


__all__ = ["ApprovalService"]
