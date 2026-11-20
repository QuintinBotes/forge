"""The impact gate: irreversible external actions wait for a person (#191, FR-026/027).

Hermetic: every port is a fake. The executor never calls a dispatcher or sender
for an irreversible external action; it hands the action to the injected
:class:`ActionHolder` and returns ``held``. Policy refusals still come first, and
reversible or internal actions run exactly as before.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from scf_board.automation import (
    ActionContext,
    ActionResult,
    ActionSpec,
    AutomationEngine,
    AutomationRuleSpec,
    AutomationRuleSpecWithMeta,
    AutoMergeAction,
    ConditionGroup,
    CreateExternalIssueAction,
    DeclareIncidentAction,
    EntitySnapshot,
    ExternalActionExecutor,
    SendNotificationAction,
    SetPriorityAction,
    StartSprintAction,
    TriggerDeployAction,
    TriggerSpec,
    WebhookPostAction,
)
from scf_contracts import DeployRules, Policy, ReviewRules
from scf_contracts.automation import (
    ACTION_IMPACT,
    AutomationActionType,
    AutomationEntityType,
    AutomationExecutionStatus,
    AutomationTriggerEnvelope,
    AutomationTriggerSource,
    AutomationTriggerType,
)
from scf_contracts.enums import IncidentSeverity, Priority, TaskStatus

WS_ID = uuid.uuid4()
ALLOW_DEPLOY = Policy(
    repo_id="acme/repo",
    deploy_rules=DeployRules(allow_agent_deploy=True, environments=["staging"]),
)
ALLOW_MERGE = Policy(
    repo_id="acme/repo",
    review_rules=ReviewRules(approval_required_for_merge=False),
    allowed_actions=["merge"],
)


class _Ports:
    """Every side-effecting port, recording what reached it."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def send(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("webhook", (url, payload)))
        return {"status_code": 200}

    def trigger_deploy(self, *, environment: str, ref: str, ctx: ActionContext) -> str:
        self.calls.append(("deploy", (environment, ref)))
        return "deploy-1"

    def merge(self, *, ref: str, method: str, ctx: ActionContext) -> str:
        self.calls.append(("merge", (ref, method)))
        return "merge-1"

    def create_issue(self, provider: str, *, title: str, ctx: ActionContext) -> str:
        self.calls.append(("issue", (provider, title)))
        return "EXT-1"

    def declare_incident(
        self, *, title: str, severity: IncidentSeverity, ctx: ActionContext
    ) -> str:
        self.calls.append(("incident", (title, severity)))
        return "INC-1"

    def start_sprint(self, *, ctx: ActionContext) -> str | None:
        self.calls.append(("sprint", None))
        return "sprint-1"

    def execute(self, action: ActionSpec, ctx: ActionContext) -> ActionResult:
        self.calls.append(("fallback", action.type))
        return ActionResult(type=action.type, status="ok")


class _Holder:
    def __init__(self) -> None:
        self.approval_id = uuid.uuid4()
        self.holds: list[tuple[ActionSpec, ActionContext, dict[str, Any]]] = []

    def hold(self, action: ActionSpec, ctx: ActionContext, parameters: dict[str, Any]) -> uuid.UUID:
        self.holds.append((action, ctx, parameters))
        return self.approval_id


class _Audit:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def record(self, **entry: Any) -> None:
        self.records.append(entry)


def _ctx() -> ActionContext:
    entity_id = uuid.uuid4()
    envelope = AutomationTriggerEnvelope(
        trigger_type=AutomationTriggerType.TASK_STATUS_CHANGED,
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
        fields={"status": TaskStatus.IN_REVIEW.value},
    )
    return ActionContext(
        rule_id=uuid.uuid4(), rule_name="ship-it", snapshot=snapshot, envelope=envelope, depth=0
    )


def _executor(
    ports: _Ports, *, policy: Policy | None = None, holder: _Holder | None = None, **kw: Any
) -> ExternalActionExecutor:
    return ExternalActionExecutor(
        fallback=ports,
        policy=policy,
        webhook=ports,
        issues=ports,
        deploys=ports,
        incidents=ports,
        sprints=ports,
        merges=ports,
        holds=holder,
        **kw,
    )


#: (action, the policy that allows it, the parameters a hold must carry)
def _held_cases(ctx: ActionContext) -> list[tuple[ActionSpec, Policy | None, dict[str, Any]]]:
    return [
        (
            WebhookPostAction(url="https://hooks.example.test/ci", payload_template={"k": "v"}),
            None,
            {
                "url": "https://hooks.example.test/ci",
                "payload": {"k": "v", "rule_id": str(ctx.rule_id), "rule_name": "ship-it"},
            },
        ),
        (
            TriggerDeployAction(environment="staging", ref_template="{{entity.status}}"),
            ALLOW_DEPLOY,
            {"environment": "staging", "ref": TaskStatus.IN_REVIEW.value},
        ),
        (
            AutoMergeAction(enabled=True, merge_method="rebase"),
            ALLOW_MERGE,
            {"ref": str(ctx.snapshot.entity_id), "method": "rebase"},
        ),
    ]


@pytest.mark.parametrize("case", range(3), ids=["webhook_post", "trigger_deploy", "auto_merge"])
def test_an_allowed_opted_in_irreversible_action_is_held_never_dispatched(case: int) -> None:
    ctx = _ctx()
    action, policy, parameters = _held_cases(ctx)[case]
    ports, holder, audit = _Ports(), _Holder(), _Audit()

    result = _executor(ports, policy=policy, holder=holder, audit=audit).execute(action, ctx)

    assert result.status == "held"
    assert result.detail == {"approval_id": str(holder.approval_id)}
    assert ports.calls == []
    assert holder.holds == [(action, ctx, parameters)]
    # The automation audit entry names the action type and the approval, nothing else.
    assert audit.records == [
        {
            "action_type": action.type,
            "rule_id": ctx.rule_id,
            "status": "held",
            "detail": {"approval_id": str(holder.approval_id)},
        }
    ]


def test_without_a_holder_an_irreversible_action_errors_and_never_dispatches() -> None:
    ports = _Ports()
    result = _executor(ports).execute(WebhookPostAction(url="https://hooks.example.test"), _ctx())
    assert result.status == "error"
    assert result.detail == {"error": "action_holder_unavailable"}
    assert ports.calls == []


def test_a_holder_that_fails_leaves_the_action_undispatched() -> None:
    class _Boom:
        def hold(self, *_: Any) -> uuid.UUID:
            raise RuntimeError("approval store is down")

    ports = _Ports()
    executor = ExternalActionExecutor(webhook=ports, holds=_Boom())
    result = executor.execute(WebhookPostAction(url="https://hooks.example.test"), _ctx())
    assert result.status == "error"
    assert ports.calls == []


@pytest.mark.parametrize(
    ("action", "policy", "reason"),
    [
        (TriggerDeployAction(environment="production"), ALLOW_DEPLOY, None),
        (TriggerDeployAction(environment="staging"), None, None),
        (AutoMergeAction(enabled=False), ALLOW_MERGE, "auto_merge_disabled"),
        (AutoMergeAction(enabled=True), None, None),
    ],
    ids=["env-not-allowed", "default-policy-deploy", "merge-not-opted-in", "default-policy-merge"],
)
def test_a_refusal_comes_before_the_hold(
    action: ActionSpec, policy: Policy | None, reason: str | None
) -> None:
    ports, holder = _Ports(), _Holder()
    result = _executor(ports, policy=policy, holder=holder).execute(action, _ctx())
    assert result.status == "forbidden"
    if reason is not None:
        assert result.detail["reason"] == reason
    assert holder.holds == []
    assert ports.calls == []


@pytest.mark.parametrize(
    ("action", "port"),
    [
        (CreateExternalIssueAction(provider="linear"), "issue"),
        (DeclareIncidentAction(severity=IncidentSeverity.HIGH), "incident"),
        (StartSprintAction(), "sprint"),
        (SetPriorityAction(priority=Priority.URGENT), "fallback"),
    ],
)
def test_reversible_or_internal_actions_run_as_today(action: ActionSpec, port: str) -> None:
    ports, holder = _Ports(), _Holder()
    result = _executor(ports, holder=holder).execute(action, _ctx())
    assert result.status == "ok"
    assert [name for name, _ in ports.calls] == [port]
    assert holder.holds == []


def test_an_unclassified_action_type_is_held(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delitem(ACTION_IMPACT, AutomationActionType.CREATE_EXTERNAL_ISSUE)
    ports, holder = _Ports(), _Holder()
    result = _executor(ports, holder=holder).execute(
        CreateExternalIssueAction(provider="linear"), _ctx()
    )
    assert result.status == "held"
    assert ports.calls == []
    assert len(holder.holds) == 1


def test_an_irreversible_external_action_with_no_release_path_is_refused() -> None:
    """``send_notification`` leaves SCF and cannot be recalled, and this executor
    cannot run it after an approval: it is refused, never held or delegated."""
    ports, holder = _Ports(), _Holder()
    action = SendNotificationAction(
        channel="email", target="ops@example.test", message_template="x"
    )
    result = _executor(ports, holder=holder).execute(action, _ctx())
    assert result.status == "forbidden"
    assert result.detail == {"reason": "irreversible_external_action_unreleasable"}
    assert holder.holds == []
    assert ports.calls == []


@pytest.mark.parametrize("case", range(3), ids=["webhook_post", "trigger_deploy", "auto_merge"])
def test_release_dispatches_the_held_action_exactly_once(case: int) -> None:
    ctx = _ctx()
    action, policy, parameters = _held_cases(ctx)[case]
    ports, holder = _Ports(), _Holder()

    result = _executor(ports, policy=policy, holder=holder).release(action, ctx)

    assert result.status == "ok"
    assert holder.holds == []
    assert len(ports.calls) == 1
    name, args = ports.calls[0]
    expected = {
        "webhook": (parameters.get("url"), parameters.get("payload")),
        "deploy": (parameters.get("environment"), parameters.get("ref")),
        "merge": (parameters.get("ref"), parameters.get("method")),
    }[name]
    assert args == expected


def test_a_released_webhook_that_fails_reports_no_url() -> None:
    class _Down:
        def send(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
            raise ConnectionError(f"cannot reach {url}")

    executor = ExternalActionExecutor(webhook=_Down())
    action = WebhookPostAction(url="https://relay:pass-word@hooks.example.test/x")
    result = executor.release(action, _ctx())
    assert result.status == "error"
    assert result.detail == {"error": "ConnectionError"}


def test_release_still_refuses_what_policy_denies() -> None:
    ports = _Ports()
    result = _executor(ports).release(TriggerDeployAction(environment="staging"), _ctx())
    assert result.status == "forbidden"
    assert ports.calls == []


def test_release_never_runs_an_action_it_cannot_dispatch() -> None:
    ports = _Ports()
    result = _executor(ports).release(SetPriorityAction(priority=Priority.LOW), _ctx())
    assert result.status == "error"
    assert ports.calls == []


@pytest.mark.parametrize(
    ("also", "expected"),
    [
        ([], AutomationExecutionStatus.NO_OP),
        ([DeclareIncidentAction()], AutomationExecutionStatus.SUCCEEDED),
    ],
    ids=["only-held", "held-and-ran"],
)
def test_a_rule_whose_actions_are_only_held_has_not_run(
    also: list[ActionSpec], expected: AutomationExecutionStatus
) -> None:
    ctx = _ctx()
    rule = AutomationRuleSpecWithMeta(
        spec=AutomationRuleSpec(
            name="ship-it",
            trigger=TriggerSpec(type=AutomationTriggerType.TASK_STATUS_CHANGED),
            condition=ConditionGroup(),
            actions=[WebhookPostAction(url="https://hooks.example.test"), *also],
        ),
        id=ctx.rule_id,
    )
    executor = _executor(_Ports(), holder=_Holder())

    [result] = AutomationEngine().evaluate(ctx.envelope, [rule], executor, ctx.snapshot)

    assert result.status is expected
