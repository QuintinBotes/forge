"""The action-executor seam for the automation engine (F21).

The engine is side-effect-free *except* through an :class:`ActionExecutor`. The
concrete executor (DB/board/workflow adapter) lives in the worker; the engine
and dry-run path use the :class:`RecordingActionExecutor` test double, which
plans actions without mutating anything.

F40-AUT-ACTIONS adds :class:`ExternalActionExecutor`, a second
:class:`ActionExecutor` for the actions that reach *outside* the board DB —
webhook / PM-adapter issue / deploy / incident / sprint / merge. Every
side-effecting dependency is an injected Protocol (a real implementation lives
in the API/worker layer; tests inject a fake), the two policy-sensitive actions
(``trigger_deploy``, ``auto_merge``) are gated through the shared
:class:`~forge_contracts.Policy` (``forge_policy.evaluate``, fail-closed), and
every dispatched outcome — ``ok``/``no_op``/``error``/``forbidden`` — is
reported to an injected audit sink. Actions this executor does not own
delegate to ``fallback`` (default: :class:`RecordingActionExecutor`), so it
composes with the worker's ``DbActionExecutor`` without duplicating the F21
action set.
"""

from __future__ import annotations

import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from forge_board.automation.schemas import (
    ActionResult,
    ActionSpec,
    AutoMergeAction,
    CreateExternalIssueAction,
    DeclareIncidentAction,
    EntitySnapshot,
    StartSprintAction,
    TriggerDeployAction,
    WebhookPostAction,
)
from forge_contracts import Decision, Policy, ToolCall
from forge_contracts.automation import AutomationActionType, AutomationTriggerEnvelope
from forge_contracts.enums import IncidentSeverity
from forge_policy import evaluate as evaluate_policy


@dataclass
class ActionContext:
    """Everything an executor needs to perform + attribute one action."""

    rule_id: uuid.UUID
    rule_name: str
    snapshot: EntitySnapshot
    envelope: AutomationTriggerEnvelope
    depth: int
    causation_chain: list[uuid.UUID] = field(default_factory=list)


@runtime_checkable
class ActionExecutor(Protocol):
    """Performs one planned action and returns its :class:`ActionResult`."""

    def execute(self, action: ActionSpec, ctx: ActionContext) -> ActionResult: ...


class RecordingActionExecutor:
    """Dry-run / test double: records planned actions, never mutates."""

    def __init__(self) -> None:
        self.planned: list[ActionSpec] = []

    def execute(self, action: ActionSpec, ctx: ActionContext) -> ActionResult:
        self.planned.append(action)
        return ActionResult(type=action.type, status="ok", detail={"simulated": True})


# --------------------------------------------------------------------------- #
# F40-AUT-ACTIONS: injected side-effecting ports                               #
# --------------------------------------------------------------------------- #


@runtime_checkable
class WebhookSender(Protocol):
    """Posts a JSON payload to an external URL (``WEBHOOK_POST``)."""

    def send(self, url: str, payload: dict[str, Any]) -> dict[str, Any]: ...


@runtime_checkable
class ExternalIssueCreator(Protocol):
    """Creates an issue via a connected PM adapter (``CREATE_EXTERNAL_ISSUE``)."""

    def create_issue(self, provider: str, *, title: str, ctx: ActionContext) -> str: ...


@runtime_checkable
class DeployDispatcher(Protocol):
    """Triggers a deploy (``TRIGGER_DEPLOY``); returns an opaque deploy id."""

    def trigger_deploy(self, *, environment: str, ref: str, ctx: ActionContext) -> str: ...


@runtime_checkable
class IncidentDeclarer(Protocol):
    """Declares an incident (``DECLARE_INCIDENT``); returns the incident key."""

    def declare_incident(
        self, *, title: str, severity: IncidentSeverity, ctx: ActionContext
    ) -> str: ...


@runtime_checkable
class SprintStarter(Protocol):
    """Auto-starts the next planned sprint (``START_SPRINT``).

    Returns the started sprint id, or ``None`` when there is no planned sprint
    to start (a benign ``no_op``, never an error).
    """

    def start_sprint(self, *, ctx: ActionContext) -> str | None: ...


@runtime_checkable
class MergeDispatcher(Protocol):
    """Merges a pull request (``AUTO_MERGE``); returns an opaque merge id."""

    def merge(self, *, ref: str, method: str, ctx: ActionContext) -> str: ...


@runtime_checkable
class AutomationAuditSink(Protocol):
    """Records one dispatched action's outcome on the platform audit chain."""

    def record(
        self,
        *,
        action_type: AutomationActionType,
        rule_id: uuid.UUID,
        status: str,
        detail: dict[str, Any],
    ) -> None: ...


class ExternalActionExecutor:
    """Dispatches the F40 external/incident/sprint/merge actions.

    Every other :class:`ActionSpec` delegates to ``fallback``. Every port is
    optional: an action whose port is not wired returns an ``error`` result
    (never a silent no-op, so a missing wire-up is visible in the execution
    row) — except ``auto_merge``, which is ``forbidden`` (not ``error``) when
    disabled, matching the "DEFAULT OFF" contract even with no merge port at
    all.
    """

    def __init__(
        self,
        *,
        fallback: ActionExecutor | None = None,
        policy: Policy | None = None,
        webhook: WebhookSender | None = None,
        issues: ExternalIssueCreator | None = None,
        deploys: DeployDispatcher | None = None,
        incidents: IncidentDeclarer | None = None,
        sprints: SprintStarter | None = None,
        merges: MergeDispatcher | None = None,
        audit: AutomationAuditSink | None = None,
    ) -> None:
        self._fallback = fallback or RecordingActionExecutor()
        # A policy-less caller gets the secure-by-default ``Policy()``: deploys
        # require an explicit ``deploy_rules`` opt-in and merges require human
        # approval — both denied out of the box.
        self._policy = policy or Policy(repo_id="automation")
        self._webhook = webhook
        self._issues = issues
        self._deploys = deploys
        self._incidents = incidents
        self._sprints = sprints
        self._merges = merges
        self._audit = audit

    def execute(self, action: ActionSpec, ctx: ActionContext) -> ActionResult:
        if isinstance(action, WebhookPostAction):
            result = self._webhook_post(action, ctx)
        elif isinstance(action, CreateExternalIssueAction):
            result = self._create_issue(action, ctx)
        elif isinstance(action, TriggerDeployAction):
            result = self._trigger_deploy(action, ctx)
        elif isinstance(action, DeclareIncidentAction):
            result = self._declare_incident(action, ctx)
        elif isinstance(action, StartSprintAction):
            result = self._start_sprint(action, ctx)
        elif isinstance(action, AutoMergeAction):
            result = self._auto_merge(action, ctx)
        else:
            return self._fallback.execute(action, ctx)
        self._record(action, ctx, result)
        return result

    # ------------------------------------------------------------------ #
    # per-action handlers                                                 #
    # ------------------------------------------------------------------ #

    def _webhook_post(self, action: WebhookPostAction, ctx: ActionContext) -> ActionResult:
        if self._webhook is None:
            return ActionResult(
                type=action.type, status="error", detail={"error": "webhook_sender_unavailable"}
            )
        payload = {
            **action.payload_template,
            "rule_id": str(ctx.rule_id),
            "rule_name": ctx.rule_name,
        }
        try:
            response = self._webhook.send(action.url, payload)
        except Exception as exc:
            return ActionResult(type=action.type, status="error", detail={"error": str(exc)})
        return ActionResult(
            type=action.type, status="ok", detail={"url": action.url, "response": response}
        )

    def _create_issue(self, action: CreateExternalIssueAction, ctx: ActionContext) -> ActionResult:
        if self._issues is None:
            return ActionResult(
                type=action.type, status="error", detail={"error": "issue_creator_unavailable"}
            )
        title = _render(action.title_template, ctx)
        try:
            external_id = self._issues.create_issue(action.provider, title=title, ctx=ctx)
        except Exception as exc:
            return ActionResult(type=action.type, status="error", detail={"error": str(exc)})
        return ActionResult(
            type=action.type,
            status="ok",
            detail={"provider": action.provider, "external_id": external_id},
        )

    def _trigger_deploy(self, action: TriggerDeployAction, ctx: ActionContext) -> ActionResult:
        decision = self._gate(tool="deploy", action_name=f"deploy_{action.environment}")
        if not decision.allowed:
            return ActionResult(
                type=action.type,
                status="forbidden",
                detail={"reason": decision.reason or "denied_by_policy"},
            )
        if self._deploys is None:
            return ActionResult(
                type=action.type, status="error", detail={"error": "deploy_dispatcher_unavailable"}
            )
        ref = _render(action.ref_template, ctx)
        try:
            deploy_id = self._deploys.trigger_deploy(
                environment=action.environment, ref=ref, ctx=ctx
            )
        except Exception as exc:
            return ActionResult(type=action.type, status="error", detail={"error": str(exc)})
        return ActionResult(
            type=action.type,
            status="ok",
            detail={"environment": action.environment, "deploy_id": deploy_id},
        )

    def _declare_incident(self, action: DeclareIncidentAction, ctx: ActionContext) -> ActionResult:
        if self._incidents is None:
            return ActionResult(
                type=action.type, status="error", detail={"error": "incident_declarer_unavailable"}
            )
        title = _render(action.title_template, ctx)
        try:
            incident_key = self._incidents.declare_incident(
                title=title, severity=action.severity, ctx=ctx
            )
        except Exception as exc:
            return ActionResult(type=action.type, status="error", detail={"error": str(exc)})
        return ActionResult(
            type=action.type,
            status="ok",
            detail={"incident_key": incident_key, "severity": action.severity.value},
        )

    def _start_sprint(self, action: StartSprintAction, ctx: ActionContext) -> ActionResult:
        if self._sprints is None:
            return ActionResult(
                type=action.type, status="error", detail={"error": "sprint_starter_unavailable"}
            )
        try:
            sprint_id = self._sprints.start_sprint(ctx=ctx)
        except Exception as exc:
            return ActionResult(type=action.type, status="error", detail={"error": str(exc)})
        if sprint_id is None:
            return ActionResult(
                type=action.type, status="no_op", detail={"reason": "no_planned_sprint"}
            )
        return ActionResult(type=action.type, status="ok", detail={"sprint_id": sprint_id})

    def _auto_merge(self, action: AutoMergeAction, ctx: ActionContext) -> ActionResult:
        if not action.enabled:
            return ActionResult(
                type=action.type, status="forbidden", detail={"reason": "auto_merge_disabled"}
            )
        decision = self._gate(tool="merge", action_name="merge")
        if not decision.allowed:
            return ActionResult(
                type=action.type,
                status="forbidden",
                detail={"reason": decision.reason or "denied_by_policy"},
            )
        if self._merges is None:
            return ActionResult(
                type=action.type, status="error", detail={"error": "merge_dispatcher_unavailable"}
            )
        ref = str(ctx.snapshot.entity_id)
        try:
            merge_id = self._merges.merge(ref=ref, method=action.merge_method, ctx=ctx)
        except Exception as exc:
            return ActionResult(type=action.type, status="error", detail={"error": str(exc)})
        return ActionResult(
            type=action.type,
            status="ok",
            detail={"merge_id": merge_id, "method": action.merge_method},
        )

    # ------------------------------------------------------------------ #
    # policy gate + audit                                                 #
    # ------------------------------------------------------------------ #

    def _gate(self, *, tool: str, action_name: str) -> Decision:
        call = ToolCall(tool=tool, action=action_name, arguments={})
        return evaluate_policy(call, self._policy)

    def _record(self, action: ActionSpec, ctx: ActionContext, result: ActionResult) -> None:
        if self._audit is None:
            return
        with suppress(Exception):  # the audit sink must never mask the action's own result
            self._audit.record(
                action_type=action.type,
                rule_id=ctx.rule_id,
                status=result.status,
                detail=result.detail,
            )


def _render(template: str, ctx: ActionContext) -> str:
    """Render the small ``{{rule.name}}`` / ``{{entity.*}}`` token set.

    Mirrors the worker's ``_render_template`` convention, projected onto what a
    pure :class:`ActionContext` actually carries (no DB row access here).
    """
    tokens = {
        "{{rule.name}}": ctx.rule_name,
        "{{entity.id}}": str(ctx.snapshot.entity_id),
        "{{entity.status}}": str(ctx.snapshot.fields.get("status") or ""),
    }
    out = template
    for token, value in tokens.items():
        out = out.replace(token, value)
    return out


__all__ = [
    "ActionContext",
    "ActionExecutor",
    "AutomationAuditSink",
    "DeployDispatcher",
    "ExternalActionExecutor",
    "ExternalIssueCreator",
    "IncidentDeclarer",
    "MergeDispatcher",
    "RecordingActionExecutor",
    "SprintStarter",
    "WebhookSender",
]
