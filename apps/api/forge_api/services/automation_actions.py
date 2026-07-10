"""Concrete side-effecting ports for the F40 automation actions.

Bridges ``forge_board.automation.executor.ExternalActionExecutor``'s injected
protocols to the real platform seams:

* :class:`AutomationActionAuditSink` records every dispatched outcome on the
  central, hash-chained :class:`~forge_api.observability.audit.AuditLog`.
* :class:`IncidentServiceDeclarer` wires ``DECLARE_INCIDENT`` to the real
  (in-memory, F17) :class:`~forge_api.services.incident_service.IncidentService`.
* :class:`SprintServiceAutoStarter` wires ``START_SPRINT`` to the real,
  DB-backed :class:`~forge_board.sprint_service.SprintService` (F26).
* :class:`PmAdapterIssueCreator` bridges ``CREATE_EXTERNAL_ISSUE`` to a
  connected :class:`~forge_contracts.pm.PMAdapter` (F40-PM-ADAPTERS);
  resolving *which* connection backs a given provider is the caller's job
  (typically ``PMConnectionService``), so this only crosses the executor's
  sync port to the adapter's ``async create_external``.
* :class:`HttpxWebhookSender` performs the real ``WEBHOOK_POST`` HTTP call.
* :class:`MockDeployDispatcher` / :class:`MockGitHubMergeDispatcher` are
  documented mocks: no CD webhook or GitHub merge call exists in this
  foundation yet, so they record what *would* be dispatched rather than
  faking a real outbound call (mirrors the "mock the outbound side" slice
  instruction and the foundation-deviation pattern used elsewhere in F21).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx

from forge_api.observability.audit import AuditCategory, AuditLog
from forge_api.services.incident_service import IncidentService
from forge_board.automation.executor import ActionContext
from forge_board.sprint_service import SprintService
from forge_contracts.automation import AutomationActionType
from forge_contracts.enums import IncidentSeverity, SprintState
from forge_contracts.pm import ForgePriority, ForgeTask, PMAdapter, StatusCategory

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
    """Bridges one ``ExternalActionExecutor`` outcome onto the platform audit log."""

    def __init__(self, audit_log: AuditLog, *, workspace_id: uuid.UUID | None = None) -> None:
        self._log = audit_log
        self._workspace_id = workspace_id

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
            actor="automation",
            workspace_id=self._workspace_id,
            target=str(rule_id),
            status=status,
            metadata=detail,
        )


class HttpxWebhookSender:
    """Posts an automation webhook action over real HTTP."""

    def __init__(self, *, timeout: float = 5.0, client: httpx.Client | None = None) -> None:
        self._timeout = timeout
        self._client = client

    def send(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
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

    ``AutoMergeAction`` is DEFAULT OFF and policy-gated well before this class
    is ever reached — see ``ExternalActionExecutor._auto_merge``.
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


class PmAdapterIssueCreator:
    """Wires ``CREATE_EXTERNAL_ISSUE`` to a connected PM adapter (F40-PM-ADAPTERS).

    ``resolver`` looks up the already-connected adapter for one provider name
    (the workspace/project -> connection lookup — e.g. via
    ``PMConnectionService`` — is the caller's job); this seam only bridges the
    executor's sync port to the adapter's ``async create_external``.
    """

    def __init__(self, resolver: Callable[[str], PMAdapter]) -> None:
        self._resolver = resolver

    def create_issue(self, provider: str, *, title: str, ctx: ActionContext) -> str:
        adapter = self._resolver(provider)
        project_id = ctx.envelope.project_id or ctx.snapshot.entity_id
        forge_task = ForgeTask(
            id=ctx.snapshot.entity_id,
            key=str(ctx.snapshot.entity_id),
            project_id=project_id,
            title=title,
            status_category=StatusCategory.unstarted,
            priority=ForgePriority.medium,
            updated_at=datetime.now(UTC),
        )
        external = asyncio.run(adapter.create_external(forge_task))
        return external.external_id


__all__ = [
    "AutomationActionAuditSink",
    "HttpxWebhookSender",
    "IncidentServiceDeclarer",
    "MockDeployDispatcher",
    "MockGitHubMergeDispatcher",
    "PmAdapterIssueCreator",
    "SprintServiceAutoStarter",
]
