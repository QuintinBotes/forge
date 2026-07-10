"""Unit tests for the F40 external/incident/sprint/merge actions.

Hermetic: no DB, no network — every side-effecting port is a fake. Covers the
dispatch + audit contract, the auto-merge default-off gate, the deploy policy
gate, and the SLA-breach -> declare-incident wiring end to end through the pure
:class:`~forge_board.automation.engine.AutomationEngine`.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from forge_board.automation import (
    ActionContext,
    AutomationEngine,
    AutomationRuleSpec,
    AutomationRuleSpecWithMeta,
    AutoMergeAction,
    ConditionGroup,
    CreateExternalIssueAction,
    DeclareIncidentAction,
    EntitySnapshot,
    ExternalActionExecutor,
    SetPriorityAction,
    SprintStarter,
    StartSprintAction,
    TriggerDeployAction,
    TriggerSpec,
    WebhookPostAction,
)
from forge_contracts import DeployRules, Policy, ReviewRules
from forge_contracts.automation import (
    AutomationActionType,
    AutomationEntityType,
    AutomationExecutionStatus,
    AutomationTriggerEnvelope,
    AutomationTriggerSource,
    AutomationTriggerType,
)
from forge_contracts.enums import IncidentSeverity, Priority, TaskStatus

WS_ID = uuid.uuid4()


# --------------------------------------------------------------------------- #
# fakes                                                                       #
# --------------------------------------------------------------------------- #


class _FakeWebhook:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def send(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((url, payload))
        return {"status_code": 200}


class _FakeIssueCreator:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def create_issue(self, provider: str, *, title: str, ctx: ActionContext) -> str:
        self.calls.append((provider, title))
        return "EXT-1"


class _FakeDeployDispatcher:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def trigger_deploy(self, *, environment: str, ref: str, ctx: ActionContext) -> str:
        self.calls.append((environment, ref))
        return "deploy-1"


class _FakeIncidentDeclarer:
    def __init__(self) -> None:
        self.calls: list[tuple[str, IncidentSeverity]] = []

    def declare_incident(
        self, *, title: str, severity: IncidentSeverity, ctx: ActionContext
    ) -> str:
        self.calls.append((title, severity))
        return "INC-1"


class _FakeSprintStarter:
    def __init__(self, sprint_id: str | None = "sprint-1") -> None:
        self.calls = 0
        self._sprint_id = sprint_id

    def start_sprint(self, *, ctx: ActionContext) -> str | None:
        self.calls += 1
        return self._sprint_id


class _FakeMergeDispatcher:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def merge(self, *, ref: str, method: str, ctx: ActionContext) -> str:
        self.calls.append((ref, method))
        return "merge-1"


class _FakeAudit:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def record(
        self,
        *,
        action_type: AutomationActionType,
        rule_id: uuid.UUID,
        status: str,
        detail: dict[str, Any],
    ) -> None:
        self.records.append(
            {"action_type": action_type, "rule_id": rule_id, "status": status, "detail": detail}
        )


# --------------------------------------------------------------------------- #
# helpers                                                                      #
# --------------------------------------------------------------------------- #


def _ctx(rule_id: uuid.UUID | None = None) -> ActionContext:
    rule_id = rule_id or uuid.uuid4()
    entity_id = uuid.uuid4()
    envelope = AutomationTriggerEnvelope(
        trigger_type=AutomationTriggerType.TASK_SLA_BREACHED,
        trigger_source=AutomationTriggerSource.BOARD_ACTIVITY,
        trigger_event_id=uuid.uuid4(),
        workspace_id=WS_ID,
        project_id=uuid.uuid4(),
        entity_type=AutomationEntityType.TASK,
        entity_id=entity_id,
    )
    snapshot = EntitySnapshot(
        entity_type=AutomationEntityType.TASK,
        entity_id=entity_id,
        fields={"status": TaskStatus.IN_PROGRESS.value, "priority": Priority.HIGH.value},
    )
    return ActionContext(
        rule_id=rule_id, rule_name="test-rule", snapshot=snapshot, envelope=envelope, depth=0
    )


# --------------------------------------------------------------------------- #
# webhook_post                                                                 #
# --------------------------------------------------------------------------- #


def test_webhook_post_dispatches_and_is_audited() -> None:
    webhook = _FakeWebhook()
    audit = _FakeAudit()
    executor = ExternalActionExecutor(webhook=webhook, audit=audit)
    ctx = _ctx()
    action = WebhookPostAction(url="https://example.test/hook", payload_template={"k": "v"})

    result = executor.execute(action, ctx)

    assert result.status == "ok"
    assert webhook.calls == [("https://example.test/hook", {"k": "v", **_ids(ctx)})]
    assert audit.records[0]["status"] == "ok"
    assert audit.records[0]["action_type"] is AutomationActionType.WEBHOOK_POST
    assert audit.records[0]["rule_id"] == ctx.rule_id


def _ids(ctx: ActionContext) -> dict[str, str]:
    return {"rule_id": str(ctx.rule_id), "rule_name": ctx.rule_name}


def test_webhook_post_without_sender_errors_and_is_audited() -> None:
    audit = _FakeAudit()
    executor = ExternalActionExecutor(audit=audit)
    result = executor.execute(WebhookPostAction(url="https://example.test/hook"), _ctx())
    assert result.status == "error"
    assert audit.records[0]["status"] == "error"


# --------------------------------------------------------------------------- #
# create_external_issue                                                       #
# --------------------------------------------------------------------------- #


def test_create_external_issue_dispatches_and_is_audited() -> None:
    issues = _FakeIssueCreator()
    audit = _FakeAudit()
    executor = ExternalActionExecutor(issues=issues, audit=audit)
    action = CreateExternalIssueAction(provider="asana", title_template="Escalate {{entity.id}}")

    result = executor.execute(action, _ctx())

    assert result.status == "ok"
    assert result.detail["external_id"] == "EXT-1"
    assert issues.calls[0][0] == "asana"
    assert audit.records[0]["action_type"] is AutomationActionType.CREATE_EXTERNAL_ISSUE


# --------------------------------------------------------------------------- #
# trigger_deploy — policy gated                                                #
# --------------------------------------------------------------------------- #


def test_trigger_deploy_denied_by_default_policy() -> None:
    """The secure-by-default ``Policy()`` denies agent deploys out of the box."""
    deploys = _FakeDeployDispatcher()
    audit = _FakeAudit()
    executor = ExternalActionExecutor(deploys=deploys, audit=audit)

    result = executor.execute(TriggerDeployAction(environment="staging"), _ctx())

    assert result.status == "forbidden"
    assert deploys.calls == []
    assert audit.records[0]["status"] == "forbidden"


def test_trigger_deploy_dispatches_when_policy_allows() -> None:
    deploys = _FakeDeployDispatcher()
    audit = _FakeAudit()
    policy = Policy(
        repo_id="acme/repo",
        deploy_rules=DeployRules(allow_agent_deploy=True, environments=["staging"]),
    )
    executor = ExternalActionExecutor(policy=policy, deploys=deploys, audit=audit)
    ctx = _ctx()

    result = executor.execute(TriggerDeployAction(environment="staging"), ctx)

    assert result.status == "ok"
    assert result.detail["deploy_id"] == "deploy-1"
    assert deploys.calls == [("staging", str(ctx.snapshot.entity_id))]
    assert audit.records[0]["status"] == "ok"


# --------------------------------------------------------------------------- #
# declare_incident — SLA breach -> incident                                    #
# --------------------------------------------------------------------------- #


def test_declare_incident_dispatches_and_is_audited() -> None:
    incidents = _FakeIncidentDeclarer()
    audit = _FakeAudit()
    executor = ExternalActionExecutor(incidents=incidents, audit=audit)
    action = DeclareIncidentAction(severity=IncidentSeverity.HIGH)

    result = executor.execute(action, _ctx())

    assert result.status == "ok"
    assert result.detail["incident_key"] == "INC-1"
    assert incidents.calls[0][1] is IncidentSeverity.HIGH
    assert audit.records[0]["action_type"] is AutomationActionType.DECLARE_INCIDENT


def test_sla_breach_trigger_declares_incident_end_to_end() -> None:
    """A ``TASK_SLA_BREACHED`` rule with a ``declare_incident`` action fires."""
    incidents = _FakeIncidentDeclarer()
    executor = ExternalActionExecutor(incidents=incidents)
    rule = AutomationRuleSpecWithMeta(
        spec=AutomationRuleSpec(
            name="sla-to-incident",
            trigger=TriggerSpec(type=AutomationTriggerType.TASK_SLA_BREACHED),
            condition=ConditionGroup(),
            actions=[DeclareIncidentAction(severity=IncidentSeverity.CRITICAL)],
        ),
        id=uuid.uuid4(),
    )
    envelope = AutomationTriggerEnvelope(
        trigger_type=AutomationTriggerType.TASK_SLA_BREACHED,
        trigger_source=AutomationTriggerSource.BOARD_ACTIVITY,
        trigger_event_id=uuid.uuid4(),
        workspace_id=WS_ID,
        entity_type=AutomationEntityType.TASK,
        entity_id=uuid.uuid4(),
    )
    snapshot = EntitySnapshot(
        entity_type=AutomationEntityType.TASK, entity_id=envelope.entity_id, fields={}
    )

    results = AutomationEngine().evaluate(envelope, [rule], executor, snapshot)

    assert len(results) == 1
    assert results[0].status is AutomationExecutionStatus.SUCCEEDED
    expected_title = f"SLA breach: {envelope.entity_id}"
    assert incidents.calls == [(expected_title, IncidentSeverity.CRITICAL)]


# --------------------------------------------------------------------------- #
# start_sprint                                                                 #
# --------------------------------------------------------------------------- #


def test_start_sprint_dispatches_and_is_audited() -> None:
    sprints = _FakeSprintStarter()
    audit = _FakeAudit()
    executor = ExternalActionExecutor(sprints=sprints, audit=audit)

    result = executor.execute(StartSprintAction(), _ctx())

    assert result.status == "ok"
    assert sprints.calls == 1
    assert audit.records[0]["action_type"] is AutomationActionType.START_SPRINT


def test_start_sprint_no_planned_sprint_is_no_op() -> None:
    sprints: SprintStarter = _FakeSprintStarter(sprint_id=None)
    executor = ExternalActionExecutor(sprints=sprints)
    result = executor.execute(StartSprintAction(), _ctx())
    assert result.status == "no_op"


# --------------------------------------------------------------------------- #
# auto_merge — DEFAULT OFF, double-gated                                       #
# --------------------------------------------------------------------------- #


def test_auto_merge_forbidden_by_default() -> None:
    """``AutoMergeAction`` defaults to ``enabled=False``: never dispatches."""
    merges = _FakeMergeDispatcher()
    audit = _FakeAudit()
    executor = ExternalActionExecutor(merges=merges, audit=audit)

    result = executor.execute(AutoMergeAction(), _ctx())

    assert result.status == "forbidden"
    assert result.detail["reason"] == "auto_merge_disabled"
    assert merges.calls == []
    assert audit.records[0]["status"] == "forbidden"


def test_auto_merge_enabled_but_default_policy_still_forbidden() -> None:
    """Opting in at the rule level is not enough: the repo policy floor holds."""
    merges = _FakeMergeDispatcher()
    executor = ExternalActionExecutor(merges=merges)  # default Policy() requires approval

    result = executor.execute(AutoMergeAction(enabled=True), _ctx())

    assert result.status == "forbidden"
    assert merges.calls == []


def test_auto_merge_dispatches_when_enabled_and_policy_allows() -> None:
    merges = _FakeMergeDispatcher()
    audit = _FakeAudit()
    policy = Policy(
        repo_id="acme/repo",
        review_rules=ReviewRules(approval_required_for_merge=False),
        allowed_actions=["merge"],
    )
    executor = ExternalActionExecutor(policy=policy, merges=merges, audit=audit)

    result = executor.execute(AutoMergeAction(enabled=True, merge_method="squash"), _ctx())

    assert result.status == "ok"
    assert merges.calls[0][1] == "squash"
    assert audit.records[0]["status"] == "ok"


# --------------------------------------------------------------------------- #
# unhandled action types fall back                                             #
# --------------------------------------------------------------------------- #


def test_unhandled_action_delegates_to_fallback() -> None:
    class _Fallback:
        def __init__(self) -> None:
            self.seen: list[Any] = []

        def execute(self, action: Any, ctx: ActionContext) -> Any:
            self.seen.append(action)
            from forge_board.automation import ActionResult

            return ActionResult(type=action.type, status="ok", detail={})

    fallback = _Fallback()
    executor = ExternalActionExecutor(fallback=fallback)
    action = SetPriorityAction(priority=Priority.URGENT)

    result = executor.execute(action, _ctx())

    assert result.status == "ok"
    assert fallback.seen == [action]


def test_audit_sink_failure_never_masks_the_action_result() -> None:
    class _BoomAudit:
        def record(self, **kwargs: Any) -> None:
            raise RuntimeError("audit store is down")

    webhook = _FakeWebhook()
    executor = ExternalActionExecutor(webhook=webhook, audit=_BoomAudit())
    result = executor.execute(WebhookPostAction(url="https://example.test/hook"), _ctx())
    assert result.status == "ok"


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
