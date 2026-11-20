"""Concrete side-effecting ports for the F40 automation actions.

Bridges ``scf_board.automation.executor.ExternalActionExecutor``'s injected
protocols to the real platform seams:

* :class:`AutomationActionAuditSink` records every dispatched outcome on the
  central, hash-chained :class:`~scf_api.observability.audit.AuditLog`.
* :class:`IncidentServiceDeclarer` wires ``DECLARE_INCIDENT`` to the real
  (in-memory, F17) :class:`~scf_api.services.incident_service.IncidentService`.
* :class:`SprintServiceAutoStarter` wires ``START_SPRINT`` to the real,
  DB-backed :class:`~scf_board.sprint_service.SprintService` (F26).
* :class:`PmAdapterIssueCreator` bridges ``CREATE_EXTERNAL_ISSUE`` to a
  connected :class:`~scf_contracts.pm.PMAdapter` (F40-PM-ADAPTERS);
  resolving *which* connection backs a given provider is the caller's job
  (typically ``PMConnectionService``), so this only crosses the executor's
  sync port to the adapter's ``async create_external``.
* :class:`HttpxWebhookSender` performs the real ``WEBHOOK_POST`` HTTP call.
* :class:`MockDeployDispatcher` / :class:`MockGitHubMergeDispatcher` are
  documented mocks: no CD webhook or GitHub merge call exists in this
  foundation yet, so they record what *would* be dispatched rather than
  faking a real outbound call (mirrors the "mock the outbound side" slice
  instruction and the foundation-deviation pattern used elsewhere in F21).

#191: :class:`ApprovalActionHolder` holds an irreversible external action as an
``automation_action`` approval request; :class:`HeldActionResolutionHook` runs it,
once, when a person approves.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any, ClassVar
from urllib.parse import urlsplit

import httpx
from pydantic import TypeAdapter
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from scf_api.observability.audit import AuditCategory, AuditLog
from scf_api.realtime.approval_events import create_published, relay_url
from scf_api.security.ssrf import assert_safe_url
from scf_api.services.incident_service import IncidentService
from scf_api.settings import get_settings
from scf_approval import ApprovalAuditSink, ApprovalService
from scf_approval.models import (
    ApprovalDecisionRequest,
    ApprovalRequest,
    GateStatus,
    GateType,
    Principal,
    ResolutionOutcome,
)
from scf_board.automation.executor import ActionContext, ExternalActionExecutor
from scf_board.automation.schemas import ActionSpec
from scf_board.sprint_service import SprintService
from scf_contracts.automation import AutomationActionType
from scf_contracts.enums import IncidentSeverity, SprintState
from scf_contracts.pm import PMAdapter, ScfPriority, ScfTask, StatusCategory
from scf_db.models import ApprovalRequest as ApprovalRequestRow
from scf_db.models import AutomationRule, HeldAutomationAction
from scf_db.models.enums import ApprovalStatus as DbApprovalStatus

logger = logging.getLogger(__name__)

#: Action types recorded under the platform's ``integration`` audit category;
#: the rest (internal board-domain effects) are ``agent_action``.
_INTEGRATION_ACTIONS = frozenset(
    {
        AutomationActionType.WEBHOOK_POST,
        AutomationActionType.CREATE_EXTERNAL_ISSUE,
        AutomationActionType.TRIGGER_DEPLOY,
        AutomationActionType.AUTO_MERGE,
    }
)


class AutomationActionAuditSink:
    """Bridges one ``ExternalActionExecutor`` outcome onto the platform audit log.

    ``actor`` is ``automation``, or the deciding person when a held action runs (#191).
    """

    def __init__(
        self,
        audit_log: AuditLog,
        *,
        workspace_id: uuid.UUID | None = None,
        actor: str = "automation",
    ) -> None:
        self._log = audit_log
        self._workspace_id = workspace_id
        self._actor = actor

    def record(
        self,
        *,
        action_type: AutomationActionType,
        rule_id: uuid.UUID,
        status: str,
        detail: dict[str, Any],
    ) -> None:
        category = (
            AuditCategory.INTEGRATION
            if action_type in _INTEGRATION_ACTIONS
            else AuditCategory.AGENT_ACTION
        )
        self._log.record(
            category=category,
            action=action_type.value,
            actor=self._actor,
            workspace_id=self._workspace_id,
            target=str(rule_id),
            status=status,
            metadata=detail,
        )


class HttpxWebhookSender:
    """Posts an automation webhook over HTTP once ``url_validator`` (SSRF guard) passes."""

    def __init__(
        self,
        *,
        timeout: float = 5.0,
        client: httpx.Client | None = None,
        url_validator: Callable[[str], str] | None = None,
    ) -> None:
        self._timeout = timeout
        self._client = client
        self._validate = url_validator

    def send(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        if self._validate is not None:
            self._validate(url)
        if self._client is not None:
            response = self._client.post(url, json=payload, timeout=self._timeout)
        else:
            response = httpx.post(url, json=payload, timeout=self._timeout)
        return {"status_code": response.status_code}


class MockDeployDispatcher:
    """Records a triggered deploy; no real CD webhook exists in this foundation yet."""

    def __init__(self) -> None:
        self.dispatched: list[dict[str, str]] = []

    def trigger_deploy(self, *, environment: str, ref: str, ctx: ActionContext) -> str:
        deploy_id = f"deploy-{uuid.uuid4().hex[:10]}"
        self.dispatched.append({"deploy_id": deploy_id, "environment": environment, "ref": ref})
        return deploy_id


class MockGitHubMergeDispatcher:
    """Records an auto-merge dispatch; the real GitHub merge call is mocked.

    ``AutoMergeAction`` is DEFAULT OFF, policy-gated and held for a person well
    before this class is ever reached — see ``ExternalActionExecutor._refusal``
    and the impact gate (#191).
    """

    def __init__(self) -> None:
        self.merged: list[dict[str, str]] = []

    def merge(self, *, ref: str, method: str, ctx: ActionContext) -> str:
        merge_id = f"merge-{uuid.uuid4().hex[:10]}"
        self.merged.append({"merge_id": merge_id, "ref": ref, "method": method})
        return merge_id


class IncidentServiceDeclarer:
    """Wires ``DECLARE_INCIDENT`` to the real (in-memory) incident service."""

    def __init__(self, service: IncidentService, *, project_id: uuid.UUID) -> None:
        self._service = service
        self._project_id = project_id

    def declare_incident(
        self, *, title: str, severity: IncidentSeverity, ctx: ActionContext
    ) -> str:
        record = self._service.declare(
            project_id=self._project_id, title=title, severity=severity, actor="automation"
        )
        return record.key


class SprintServiceAutoStarter:
    """Wires ``START_SPRINT`` to the real board ``SprintService`` (F26).

    Starts the oldest ``planned`` sprint in the trigger's project — the natural
    "auto-start the next sprint" semantics for a ``sprint_completed`` rule.
    Returns ``None`` (a benign ``no_op``, never an error) when the project has
    no planned sprint queued, or the trigger carried no project.
    """

    def __init__(self, service: SprintService, *, workspace_id: uuid.UUID) -> None:
        self._service = service
        self._workspace_id = workspace_id

    def start_sprint(self, *, ctx: ActionContext) -> str | None:
        project_id = ctx.envelope.project_id
        if project_id is None:
            return None
        planned = self._service.list_sprints(
            workspace_id=self._workspace_id, project_id=project_id, state=SprintState.PLANNED
        )
        if not planned:
            return None
        started = self._service.start(workspace_id=self._workspace_id, sprint_id=planned[0].id)
        return str(started.id)


class PmConnectionDisabledError(RuntimeError):
    """``CREATE_EXTERNAL_ISSUE`` was refused: the PM connection is disabled."""


class PmAdapterIssueCreator:
    """Wires ``CREATE_EXTERNAL_ISSUE`` to a connected PM adapter (F40-PM-ADAPTERS).

    ``resolver`` looks up the already-connected adapter for one provider name
    (the workspace/project -> connection lookup — e.g. via
    ``PMConnectionService`` — is the caller's job); this seam only bridges the
    executor's sync port to the adapter's ``async create_external``.

    A person configuring the action opts in to creating tracker issues (spec
    M §3), but a *disabled* connection refuses: ``disabled_check`` decides when
    given, otherwise the adapter's forge-set ``ctx.config["connection_status"]``
    (stamped by ``PMConnectionService`` when it builds the adapter) does.
    """

    def __init__(
        self,
        resolver: Callable[[str], PMAdapter],
        *,
        disabled_check: Callable[[str], bool] | None = None,
    ) -> None:
        self._resolver = resolver
        self._disabled_check = disabled_check

    def create_issue(self, provider: str, *, title: str, ctx: ActionContext) -> str:
        adapter = self._resolver(provider)
        if self._connection_disabled(provider, adapter):
            raise PmConnectionDisabledError(
                f"PM connection for provider {provider!r} is disabled; "
                "refusing to create an external issue"
            )
        project_id = ctx.envelope.project_id or ctx.snapshot.entity_id
        scf_task = ScfTask(
            id=ctx.snapshot.entity_id,
            key=str(ctx.snapshot.entity_id),
            project_id=project_id,
            title=title,
            status_category=StatusCategory.unstarted,
            priority=ScfPriority.medium,
            updated_at=datetime.now(UTC),
        )
        external = asyncio.run(adapter.create_external(scf_task))
        return external.external_id

    def _connection_disabled(self, provider: str, adapter: PMAdapter) -> bool:
        if self._disabled_check is not None:
            return self._disabled_check(provider)
        config = getattr(getattr(adapter, "ctx", None), "config", None) or {}
        return isinstance(config, dict) and config.get("connection_status") == "disabled"


# --------------------------------------------------------------------------- #
# #191: hold an irreversible external action for a person, run it on approval  #
# --------------------------------------------------------------------------- #

#: The subject every ``automation_action`` request carries.
HELD_ACTION_SUBJECT = "automation_action"
#: Approval audit statuses the hook writes beside the decision's own entry.
HELD_ACTION_RELEASED = "held_action_released"
HELD_ACTION_WITHHELD = "held_action_withheld"

_ACTION: TypeAdapter[ActionSpec] = TypeAdapter(ActionSpec)
_CONTEXT: TypeAdapter[ActionContext] = TypeAdapter(ActionContext)

SessionFactory = Callable[[], Session]


def _shown(parameters: dict[str, Any]) -> dict[str, Any]:
    """Parameters a person may see: a URL cut to its origin, since credentials ride
    in its userinfo, path or query, which the approval service's redactor (applied
    to every ``gate_payload``) does not recognise."""
    shown = dict(parameters)
    url = shown.get("url")
    if isinstance(url, str):
        parts = urlsplit(url)
        shown["url"] = f"{parts.scheme}://{parts.netloc.rpartition('@')[2]}"
    return shown


class ApprovalActionHolder:
    """The executor's :class:`~scf_board.automation.executor.ActionHolder` (#191).

    Opens an ``automation_action`` request in the trigger's workspace, as the
    rule's author (so the self-approval rule applies to them), and stores the
    exact action beside it. Idempotent: the subject is derived from the rule,
    the trigger event and the action, so a redelivered trigger finds the same
    request, pending or already decided, and never opens a second one.
    """

    def __init__(self, approvals: ApprovalService, session_factory: SessionFactory) -> None:
        self._approvals = approvals
        self._sf = session_factory

    def hold(self, action: ActionSpec, ctx: ActionContext, parameters: dict[str, Any]) -> uuid.UUID:
        # The executor's port is synchronous (the Celery worker has no running loop).
        return asyncio.run(self._hold(action, ctx, parameters))

    async def _hold(
        self, action: ActionSpec, ctx: ActionContext, parameters: dict[str, Any]
    ) -> uuid.UUID:
        stored = _ACTION.dump_python(action, mode="json")
        fingerprint = json.dumps(stored, sort_keys=True)
        subject_id = uuid.uuid5(ctx.rule_id, f"{ctx.envelope.trigger_event_id}:{fingerprint}")
        with self._sf() as session:
            rule = session.get(AutomationRule, ctx.rule_id)
            author = rule.created_by if rule is not None else None
            decided = session.scalar(
                select(ApprovalRequestRow.id).where(
                    ApprovalRequestRow.workspace_id == ctx.envelope.workspace_id,
                    ApprovalRequestRow.subject_type == HELD_ACTION_SUBJECT,
                    ApprovalRequestRow.subject_id == subject_id,
                    ApprovalRequestRow.status != DbApprovalStatus.PENDING,
                )
            )
        if decided is not None:
            return decided
        # Announced on /ws like every gate the worker opens; undecided, it expires
        # under the SLA sweep after FORGE_HELD_ACTION_TTL_HOURS and never runs.
        request = await create_published(
            self._approvals,
            redis_url=relay_url(),
            expires_at=datetime.now(UTC) + timedelta(hours=get_settings().held_action_ttl_hours),
            workspace_id=ctx.envelope.workspace_id,
            gate_type=GateType.AUTOMATION_ACTION,
            subject_type=HELD_ACTION_SUBJECT,
            subject_id=subject_id,
            project_id=ctx.envelope.project_id,
            requested_by=author,
            requested_actor=f"user:{author}" if author else "automation",
            risk_level="warning",
            title=f"{action.type.value} from automation rule '{ctx.rule_name}'",
            gate_payload={
                "action_type": action.type.value,
                "parameters": _shown(parameters),
                "rule_id": str(ctx.rule_id),
                "rule_name": ctx.rule_name,
                "trigger_event_id": str(ctx.envelope.trigger_event_id),
                "entity_type": ctx.snapshot.entity_type.value,
                "entity_id": str(ctx.snapshot.entity_id),
            },
        )
        try:
            self._store(request, stored, ctx)
        except Exception:
            # The trigger is not evaluated again: a request with nothing behind it
            # must not wait for an approval that would run nothing.
            if not self._stored(request.id):  # another delivery may have stored it
                await self._approvals.expire(
                    request, actor="automation", reason="held_action_not_stored"
                )
            raise
        return request.id

    def _stored(self, approval_id: uuid.UUID) -> bool:
        with self._sf() as session:
            return (
                session.scalar(
                    select(HeldAutomationAction.id).where(
                        HeldAutomationAction.approval_request_id == approval_id
                    )
                )
                is not None
            )

    def _store(self, request: ApprovalRequest, stored: dict[str, Any], ctx: ActionContext) -> None:
        if self._stored(request.id):
            return
        with self._sf() as session:
            session.add(
                HeldAutomationAction(
                    workspace_id=request.workspace_id,
                    approval_request_id=request.id,
                    action=stored,
                    context=_CONTEXT.dump_python(ctx, mode="json"),
                )
            )
            session.commit()


def claim_held_action(
    session_factory: SessionFactory, request: ApprovalRequest, *, released_by: uuid.UUID | None
) -> tuple[ActionSpec, ActionContext] | None:
    """Take ``request``'s held action for its one run; ``None`` if already taken or absent.

    The conditional update is the exactly-once guard: of two racing claims, one
    updates the row and the other finds nothing.
    """
    owned = (
        HeldAutomationAction.approval_request_id == request.id,
        HeldAutomationAction.workspace_id == request.workspace_id,
    )
    with session_factory() as session:
        claimed = session.execute(
            update(HeldAutomationAction)
            .where(*owned, HeldAutomationAction.released_at.is_(None))
            .values(released_at=datetime.now(UTC), released_by=released_by)
        )
        if claimed.rowcount != 1:  # type: ignore[attr-defined]
            session.rollback()
            return None
        row = session.scalars(select(HeldAutomationAction).where(*owned)).one()
        session.commit()
        return _ACTION.validate_python(row.action), _CONTEXT.validate_python(row.context)


def release_executor(workspace_id: uuid.UUID, actor: str) -> ExternalActionExecutor:
    """The executor that runs an approved action in the API process, audited as ``actor``.

    The same ports the worker wires (the deploy and merge dispatchers are the
    documented mocks), and the secure default policy, so a deploy or merge the
    policy denies is still refused at release. The webhook leaves from this
    process, so it passes the API's SSRF guard first (private networks and the
    allowlist as configured for every other outbound URL).
    """
    from scf_api.observability.service import get_observability_service

    settings = get_settings()
    guard = partial(
        assert_safe_url,
        allow_private=settings.ssrf_allow_private,
        allowlist=settings.outbound_allowlist,
    )
    return ExternalActionExecutor(
        webhook=HttpxWebhookSender(url_validator=guard),
        deploys=MockDeployDispatcher(),
        merges=MockGitHubMergeDispatcher(),
        audit=AutomationActionAuditSink(
            get_observability_service().audit, workspace_id=workspace_id, actor=actor
        ),
    )


def _default_session_factory() -> Session:
    from scf_api.db import get_session_factory

    return get_session_factory()()


class HeldActionResolutionHook:
    """Runs a held action once a person approves it; any other outcome runs nothing."""

    gate_type: ClassVar[GateType] = GateType.AUTOMATION_ACTION

    def __init__(
        self,
        *,
        audit: ApprovalAuditSink,
        session_factory: SessionFactory | None = None,
        executor_factory: Callable[[uuid.UUID, str], ExternalActionExecutor] | None = None,
    ) -> None:
        self._audit = audit
        self._sf = session_factory or _default_session_factory
        self._executor = executor_factory or release_executor

    async def on_resolved(
        self,
        request: ApprovalRequest,
        decision: ApprovalDecisionRequest,
        actor: Principal,
        *,
        session: Any = None,
    ) -> ResolutionOutcome:
        if request.status is not GateStatus.APPROVED:
            self._record(request, actor, HELD_ACTION_WITHHELD, result="not_run")
            return ResolutionOutcome(completed=True, follow_up_state="not_run")
        claimed = claim_held_action(self._sf, request, released_by=actor.id)
        if claimed is None:
            self._record(request, actor, HELD_ACTION_RELEASED, result="not_stored")
            return ResolutionOutcome(
                completed=False,
                blocking_reasons=["no held action is left to run for this request"],
            )
        action, ctx = claimed
        executor = self._executor(request.workspace_id, actor.actor_ref)
        # Off the event loop: the ports are blocking calls (a webhook waits up to 5s).
        result = await asyncio.to_thread(executor.release, action, ctx)
        request.gate_payload = {
            **request.gate_payload,
            "dispatch": {"status": result.status, "actor": actor.actor_ref},
        }
        self._record(request, actor, HELD_ACTION_RELEASED, result=result.status)
        ran = result.status == "ok"
        return ResolutionOutcome(
            completed=ran,
            follow_up_state=f"dispatch_{result.status}",
            blocking_reasons=[] if ran else [f"the approved action did not run: {result.status}"],
            # The error is a type name only (e.g. SsrfBlockedError): never a URL.
            details={
                "dispatch_status": result.status,
                "dispatch_error": result.detail.get("error"),
            },
        )

    def _record(
        self, request: ApprovalRequest, actor: Principal, status: str, *, result: str
    ) -> None:
        """Audit with the action type and the person, never the parameters; a failed
        write is logged, never raised (the decision and the run already stand)."""
        try:
            self._audit.record_approval(
                request.gate_type.value,
                status=status,
                actor=actor.actor_ref,
                request_id=request.id,
                workspace_id=request.workspace_id,
                metadata={"action_type": request.gate_payload.get("action_type"), "result": result},
            )
        except Exception:
            logger.exception("auditing %s for held action %s failed", status, request.id)


__all__ = [
    "HELD_ACTION_RELEASED",
    "HELD_ACTION_SUBJECT",
    "HELD_ACTION_WITHHELD",
    "ApprovalActionHolder",
    "AutomationActionAuditSink",
    "HeldActionResolutionHook",
    "HttpxWebhookSender",
    "IncidentServiceDeclarer",
    "MockDeployDispatcher",
    "MockGitHubMergeDispatcher",
    "PmAdapterIssueCreator",
    "PmConnectionDisabledError",
    "SprintServiceAutoStarter",
    "claim_held_action",
    "release_executor",
]
