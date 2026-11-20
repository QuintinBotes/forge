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
:class:`~scf_contracts.Policy` (``scf_policy.evaluate``, fail-closed), and
every dispatched outcome — ``ok``/``no_op``/``error``/``forbidden`` — is
reported to an injected audit sink. Actions this executor does not own
delegate to ``fallback`` (default: :class:`RecordingActionExecutor`), so it
composes with the worker's ``DbActionExecutor`` without duplicating the F21
action set.

#191 adds the impact gate: an action whose declared impact
(:func:`~scf_contracts.automation.action_impact`) is irreversible and external is
never dispatched by :meth:`ExternalActionExecutor.execute`. After the policy
refusals, it goes to the injected :class:`ActionHolder`, which opens an approval
request, and the result is ``held``. :meth:`ExternalActionExecutor.release` runs
it once a person approves.
"""

from __future__ import annotations

import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from scf_board.automation.schemas import (
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
from scf_contracts import Decision, Policy, ToolCall
from scf_contracts.automation import (
    AutomationActionType,
    AutomationTriggerEnvelope,
    action_impact,
)
from scf_contracts.enums import IncidentSeverity
from scf_policy import evaluate as evaluate_policy


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


@runtime_checkable
class ActionHolder(Protocol):
    """Holds an irreversible external action for a person (#191).

    ``parameters`` are what dispatching it would send, for the person to see.
    Returns the id of the approval request that now holds it.
    """

    def hold(
        self, action: ActionSpec, ctx: ActionContext, parameters: dict[str, Any]
    ) -> uuid.UUID: ...


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
        holds: ActionHolder | None = None,
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
        self._holds = holds

    def execute(self, action: ActionSpec, ctx: ActionContext) -> ActionResult:
        result = self._refusal(action)
        if result is None and action_impact(action.type).needs_person:
            result = self._hold(action, ctx)
        if result is None:
            result = self._dispatch(action, ctx)
        if result is None:
            return self._fallback.execute(action, ctx)
        self._record(action, ctx, result)
        return result

    def release(self, action: ActionSpec, ctx: ActionContext) -> ActionResult:
        """Run an action a person approved: the refusals still apply; it is never held again."""
        result = self._refusal(action) or self._dispatch(action, ctx)
        if result is None:
            result = ActionResult(
                type=action.type, status="error", detail={"error": "action_not_releasable"}
            )
        self._record(action, ctx, result)
        return result

    def parameters(self, action: ActionSpec, ctx: ActionContext) -> dict[str, Any]:
        """What dispatching ``action`` sends, rendered for ``ctx`` (a hold shows these)."""
        if isinstance(action, WebhookPostAction):
            payload = {
                **action.payload_template,
                "rule_id": str(ctx.rule_id),
                "rule_name": ctx.rule_name,
            }
            return {"url": action.url, "payload": payload}
        if isinstance(action, TriggerDeployAction):
            return {"environment": action.environment, "ref": _render(action.ref_template, ctx)}
        if isinstance(action, AutoMergeAction):
            return {"ref": str(ctx.snapshot.entity_id), "method": action.merge_method}
        return action.model_dump(mode="json", exclude={"type"})

    def _refusal(self, action: ActionSpec) -> ActionResult | None:
        """The policy refusals, which come before any hold (a denied action is ``forbidden``)."""
        if isinstance(action, AutoMergeAction) and not action.enabled:
            return ActionResult(
                type=action.type, status="forbidden", detail={"reason": "auto_merge_disabled"}
            )
        if isinstance(action, TriggerDeployAction):
            decision = self._gate(tool="deploy", action_name=f"deploy_{action.environment}")
        elif isinstance(action, AutoMergeAction):
            decision = self._gate(tool="merge", action_name="merge")
        else:
            return None
        if decision.allowed:
            return None
        return ActionResult(
            type=action.type,
            status="forbidden",
            detail={"reason": decision.reason or "denied_by_policy"},
        )

    def _hold(self, action: ActionSpec, ctx: ActionContext) -> ActionResult:
        if not isinstance(action, _HANDLED):
            # Nothing here could run it after an approval: refused, not held for nothing.
            return ActionResult(
                type=action.type,
                status="forbidden",
                detail={"reason": "irreversible_external_action_unreleasable"},
            )
        if self._holds is None:
            return ActionResult(
                type=action.type, status="error", detail={"error": "action_holder_unavailable"}
            )
        try:
            approval_id = self._holds.hold(action, ctx, self.parameters(action, ctx))
        except Exception as exc:
            return ActionResult(
                type=action.type,
                status="error",
                detail={"error": "action_hold_failed", "error_type": type(exc).__name__},
            )
        return ActionResult(
            type=action.type, status="held", detail={"approval_id": str(approval_id)}
        )

    def _dispatch(self, action: ActionSpec, ctx: ActionContext) -> ActionResult | None:
        """Run one of this executor's own actions; ``None`` for any other type."""
        if isinstance(action, WebhookPostAction):
            return self._webhook_post(action, ctx)
        if isinstance(action, CreateExternalIssueAction):
            return self._create_issue(action, ctx)
        if isinstance(action, TriggerDeployAction):
            return self._trigger_deploy(action, ctx)
        if isinstance(action, DeclareIncidentAction):
            return self._declare_incident(action, ctx)
        if isinstance(action, StartSprintAction):
            return self._start_sprint(action, ctx)
        if isinstance(action, AutoMergeAction):
            return self._auto_merge(action, ctx)
        return None

    # ------------------------------------------------------------------ #
    # per-action handlers                                                 #
    # ------------------------------------------------------------------ #

    def _webhook_post(self, action: WebhookPostAction, ctx: ActionContext) -> ActionResult:
        if self._webhook is None:
            return ActionResult(
                type=action.type, status="error", detail={"error": "webhook_sender_unavailable"}
            )
        params = self.parameters(action, ctx)
        try:
            response = self._webhook.send(params["url"], params["payload"])
        except Exception as exc:
            # The type only: an HTTP error's text can carry the URL and its credentials.
            return ActionResult(
                type=action.type, status="error", detail={"error": type(exc).__name__}
            )
        # No URL in the result, which is audited: it can carry credentials.
        return ActionResult(type=action.type, status="ok", detail={"response": response})

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
        if self._deploys is None:
            return ActionResult(
                type=action.type, status="error", detail={"error": "deploy_dispatcher_unavailable"}
            )
        params = self.parameters(action, ctx)
        try:
            deploy_id = self._deploys.trigger_deploy(
                environment=params["environment"], ref=params["ref"], ctx=ctx
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
        if self._merges is None:
            return ActionResult(
                type=action.type, status="error", detail={"error": "merge_dispatcher_unavailable"}
            )
        params = self.parameters(action, ctx)
        try:
            merge_id = self._merges.merge(ref=params["ref"], method=params["method"], ctx=ctx)
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


#: The action types :meth:`ExternalActionExecutor._dispatch` runs, and so can release.
_HANDLED = (
    WebhookPostAction,
    CreateExternalIssueAction,
    TriggerDeployAction,
    DeclareIncidentAction,
    StartSprintAction,
    AutoMergeAction,
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
    "ActionHolder",
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
