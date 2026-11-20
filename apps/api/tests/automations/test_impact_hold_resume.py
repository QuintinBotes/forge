"""A held automatic action runs only after a person approves it (#191, FR-027/FR-031).

The hold side is the real :class:`ApprovalActionHolder` behind the real executor;
the resume side is the real :class:`HeldActionResolutionHook` inside
``ApprovalService.resolve``. Storage is SQLite through the production repository.
Only the outbound ports are fakes, so every call that would leave SCF is counted.
"""

from __future__ import annotations

import asyncio
import json
import threading
import uuid
from collections.abc import Iterator
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import StaticPool, create_engine, event, select, update
from sqlalchemy.orm import Session, sessionmaker

from scf_api.main import create_app
from scf_api.observability.audit import AuditLog
from scf_api.observability.redaction import redact_mapping
from scf_api.realtime import approval_events
from scf_api.services.approval_repository_db import SqlAlchemyApprovalRepository
from scf_api.services.approval_service import get_approval_service
from scf_api.services.automation_actions import (
    ApprovalActionHolder,
    AutomationActionAuditSink,
    HeldActionResolutionHook,
    claim_held_action,
    release_executor,
)
from scf_api.settings import get_settings
from scf_approval import (
    AlreadyResolvedError,
    ApprovalAuthorizer,
    ApprovalRepository,
    ApprovalService,
    AuthorizationError,
    GateRegistry,
    InMemoryApprovalRepository,
    RecordingAuditSink,
)
from scf_approval.escalation import SlaPolicy
from scf_approval.models import (
    ApprovalAction,
    ApprovalDecisionRequest,
    GateStatus,
    GateType,
    Principal,
    Role,
)
from scf_board.automation import (
    ActionContext,
    ActionSpec,
    EntitySnapshot,
    ExternalActionExecutor,
    WebhookPostAction,
)
from scf_contracts import DeployRules, Policy, ReviewRules
from scf_contracts.automation import (
    AutomationEntityType,
    AutomationTriggerEnvelope,
    AutomationTriggerSource,
    AutomationTriggerType,
)
from scf_db.base import Base
from scf_db.models import ApprovalRequest as ApprovalRequestRow
from scf_db.models import AutomationRule, HeldAutomationAction, User, Workspace
from scf_db.models.enums import ApprovalStatus as DbApprovalStatus

WS = uuid.UUID("00000000-0000-0000-0000-0000000001a1")
OTHER_WS = uuid.UUID("00000000-0000-0000-0000-0000000001a2")
AUTHOR = uuid.UUID("00000000-0000-0000-0000-0000000001b1")
APPROVER = uuid.UUID("00000000-0000-0000-0000-0000000001b2")
SECOND = uuid.UUID("00000000-0000-0000-0000-0000000001b3")
#: Every secret the webhook action carries; none may reach a payload or audit entry.
SECRETS = ("s3cr3t-pass", "T0KEN-in-path", "q-token-value", "payload-api-key")
URL = "https://relay:s3cr3t-pass@hooks.example.test/services/T0KEN-in-path?token=q-token-value"
ALLOW = Policy(
    repo_id="acme/repo",
    deploy_rules=DeployRules(allow_agent_deploy=True, environments=["staging"]),
    review_rules=ReviewRules(approval_required_for_merge=False),
    allowed_actions=["merge"],
)


class Ports:
    """The outbound side: every call that would leave SCF."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def send(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("webhook", (url, payload)))
        return {"status_code": 204}

    def trigger_deploy(self, *, environment: str, ref: str, ctx: ActionContext) -> str:
        self.calls.append(("deploy", (environment, ref)))
        return "deploy-1"

    def merge(self, *, ref: str, method: str, ctx: ActionContext) -> str:
        self.calls.append(("merge", (ref, method)))
        return "merge-1"


class Stack:
    def __init__(
        self,
        factory: sessionmaker[Session],
        *,
        repo: ApprovalRepository | None = None,
        forbid_self_approval: bool = False,
        release: Any = None,  # the production ``release_executor`` instead of fake ports
    ) -> None:
        self.factory = factory
        self.ports = Ports()
        self.approval_audit = RecordingAuditSink()
        self.automation_log = AuditLog()
        registry = GateRegistry()
        registry.register_hook(
            HeldActionResolutionHook(
                audit=self.approval_audit,
                session_factory=factory,
                executor_factory=release or self.executor,
            )
        )
        self.approvals = ApprovalService(
            repo or SqlAlchemyApprovalRepository(factory),
            registry,
            ApprovalAuthorizer(forbid_self_approval=forbid_self_approval),
            audit=self.approval_audit,
            redactor=redact_mapping,
        )
        with factory() as session:
            rule = AutomationRule(
                workspace_id=WS,
                name="ship-it",
                trigger_type=AutomationTriggerType.TASK_STATUS_CHANGED,
                created_by=AUTHOR,
            )
            session.add(rule)
            session.commit()
            self.rule_id = rule.id

    def executor(
        self, workspace_id: uuid.UUID, actor: str = "automation"
    ) -> ExternalActionExecutor:
        return ExternalActionExecutor(
            policy=ALLOW,
            webhook=self.ports,
            deploys=self.ports,
            merges=self.ports,
            holds=ApprovalActionHolder(self.approvals, self.factory),
            audit=AutomationActionAuditSink(
                self.automation_log, workspace_id=workspace_id, actor=actor
            ),
        )

    def ctx(self) -> ActionContext:
        entity_id = uuid.uuid4()
        return ActionContext(
            rule_id=self.rule_id,
            rule_name="ship-it",
            snapshot=EntitySnapshot(
                entity_type=AutomationEntityType.TASK,
                entity_id=entity_id,
                fields={"status": "in_review"},
            ),
            envelope=AutomationTriggerEnvelope(
                trigger_type=AutomationTriggerType.TASK_STATUS_CHANGED,
                trigger_source=AutomationTriggerSource.BOARD_ACTIVITY,
                trigger_event_id=uuid.uuid4(),
                workspace_id=WS,
                entity_type=AutomationEntityType.TASK,
                entity_id=entity_id,
            ),
            depth=0,
        )

    def hold(self, action: ActionSpec | None = None) -> uuid.UUID:
        result = self.executor(WS).execute(action or webhook(), self.ctx())
        assert result.status == "held", result
        return uuid.UUID(result.detail["approval_id"])

    def decide(
        self,
        approval_id: uuid.UUID,
        decision: ApprovalAction,
        *,
        by: uuid.UUID = APPROVER,
        note: str | None = None,
        role: Role = Role.MEMBER,
    ) -> Any:
        return asyncio.run(
            self.approvals.resolve(
                approval_id,
                ApprovalDecisionRequest(decision=decision, note=note),
                person(by, role),
                workspace_id=WS,
            )
        )

    def request(self, approval_id: uuid.UUID) -> Any:
        return asyncio.run(self.approvals.get(approval_id, workspace_id=WS))


def webhook() -> WebhookPostAction:
    return WebhookPostAction(
        url=URL, payload_template={"event": "shipped", "api_key": "payload-api-key"}
    )


def person(user_id: uuid.UUID, role: Role = Role.MEMBER) -> Principal:
    return Principal(kind="user", id=user_id, role=role, workspace_id=WS)


@pytest.fixture
def factory() -> Iterator[sessionmaker[Session]]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        session.add_all(
            [Workspace(id=WS, name="Acme", slug="acme"), Workspace(id=OTHER_WS, name="O", slug="o")]
        )
        session.flush()
        for user_id in (AUTHOR, APPROVER, SECOND):
            session.add(
                User(id=user_id, workspace_id=WS, email=f"{user_id.hex[-4:]}@acme.test", name="P")
            )
        session.commit()
    yield factory
    engine.dispose()


@pytest.fixture
def stack(factory: sessionmaker[Session]) -> Stack:
    return Stack(factory)


def _no_secret(value: Any) -> None:
    text = json.dumps(value, default=str)
    for secret in SECRETS:
        assert secret not in text


def test_a_held_webhook_is_an_approval_request_with_its_type_and_redacted_parameters(
    stack: Stack,
) -> None:
    approval_id = stack.hold()

    request = stack.request(approval_id)
    assert request.gate_type is GateType.AUTOMATION_ACTION
    assert request.status is GateStatus.PENDING
    assert request.requested_actor == f"user:{AUTHOR}"  # the rule's author asked for it
    payload = request.gate_payload
    assert payload["action_type"] == "webhook_post"
    assert payload["rule_id"] == str(stack.rule_id)
    assert payload["parameters"]["url"] == "https://hooks.example.test"
    assert payload["parameters"]["payload"]["event"] == "shipped"
    _no_secret(payload)
    _no_secret(request.title)
    assert stack.ports.calls == []


def test_an_approved_hold_runs_the_stored_action_once_as_the_decider(
    stack: Stack, factory: sessionmaker[Session]
) -> None:
    approval_id = stack.hold()

    resolution = stack.decide(approval_id, ApprovalAction.APPROVE)

    assert resolution.status is GateStatus.APPROVED
    assert resolution.outcome.completed is True
    assert stack.ports.calls == [
        (
            "webhook",
            (
                URL,
                {
                    "event": "shipped",
                    "api_key": "payload-api-key",
                    "rule_id": str(stack.rule_id),
                    "rule_name": "ship-it",
                },
            ),
        )
    ]
    request = stack.request(approval_id)
    assert request.gate_payload["dispatch"] == {"status": "ok", "actor": f"user:{APPROVER}"}
    with factory() as session:
        row = session.scalars(select(HeldAutomationAction)).one()
    assert row.released_by == APPROVER
    assert row.released_at is not None

    # Never twice: not on a second decision, nor if the hook ran again.
    with pytest.raises(AlreadyResolvedError):
        stack.decide(approval_id, ApprovalAction.APPROVE, by=SECOND)
    hook = stack.approvals.resolution_hook(GateType.AUTOMATION_ACTION)
    assert hook is not None
    again = asyncio.run(
        hook.on_resolved(
            request, ApprovalDecisionRequest(decision=ApprovalAction.APPROVE), person(SECOND)
        )
    )
    assert again.completed is False
    assert len(stack.ports.calls) == 1


@pytest.mark.parametrize("note", [None, "the relay is being replaced"])
@pytest.mark.parametrize("decision", [ApprovalAction.REJECT, ApprovalAction.REQUEST_CHANGES])
def test_a_rejected_hold_never_runs(
    stack: Stack, decision: ApprovalAction, note: str | None
) -> None:
    approval_id = stack.hold()

    resolution = stack.decide(approval_id, decision, note=note)

    assert resolution.status is not GateStatus.APPROVED
    assert stack.ports.calls == []
    [record] = asyncio.run(stack.approvals.decisions(approval_id, workspace_id=WS))
    assert record.note == note
    withheld = [r for r in stack.approval_audit.records if r["status"] == "held_action_withheld"]
    assert [(r["request_id"], r["actor"], r["metadata"]["action_type"]) for r in withheld] == [
        (approval_id, f"user:{APPROVER}", "webhook_post")
    ]


def test_an_expired_hold_never_runs(stack: Stack) -> None:
    approval_id = stack.hold()
    asyncio.run(stack.approvals.expire(stack.request(approval_id), actor="system"))

    with pytest.raises(AlreadyResolvedError):
        stack.decide(approval_id, ApprovalAction.APPROVE)
    assert stack.ports.calls == []


def test_an_undecided_hold_expires_under_the_sla_and_never_runs(
    factory: sessionmaker[Session],
) -> None:
    stack = Stack(factory, repo=InMemoryApprovalRepository())
    approval_id = stack.hold()
    later = datetime.now(UTC) + timedelta(hours=2)

    asyncio.run(stack.approvals.sweep_sla(sla=SlaPolicy(expire_after_seconds=3600), now=later))

    assert stack.request(approval_id).status is GateStatus.EXPIRED
    assert stack.ports.calls == []


def test_a_hold_waits_for_every_required_approval(stack: Stack) -> None:
    approval_id = stack.hold()
    request = stack.request(approval_id)
    request.required_approvals = 2
    asyncio.run(SqlAlchemyApprovalRepository(stack.factory).update(request))

    first = stack.decide(approval_id, ApprovalAction.APPROVE)
    assert first.status is GateStatus.PENDING
    assert stack.ports.calls == []

    stack.decide(approval_id, ApprovalAction.APPROVE, by=SECOND)
    assert len(stack.ports.calls) == 1


def test_the_rules_author_cannot_approve_its_hold_when_self_approval_is_forbidden(
    factory: sessionmaker[Session],
) -> None:
    stack = Stack(factory, forbid_self_approval=True)
    approval_id = stack.hold()

    with pytest.raises(AuthorizationError):
        stack.decide(approval_id, ApprovalAction.APPROVE, by=AUTHOR)
    assert stack.ports.calls == []

    stack.decide(approval_id, ApprovalAction.APPROVE, by=APPROVER)
    assert len(stack.ports.calls) == 1


def test_a_hold_with_no_stored_action_runs_nothing(
    stack: Stack, factory: sessionmaker[Session]
) -> None:
    approval_id = stack.hold()
    with factory() as session:
        session.query(HeldAutomationAction).delete()
        session.commit()

    resolution = stack.decide(approval_id, ApprovalAction.APPROVE)

    assert resolution.outcome.completed is False
    assert stack.ports.calls == []


@pytest.mark.parametrize(
    ("decision", "meanwhile"),
    [
        (ApprovalAction.APPROVE, DbApprovalStatus.EXPIRED),
        (ApprovalAction.ESCALATE, DbApprovalStatus.REJECTED),
    ],
    ids=["approve-vs-expiry", "escalate-vs-reject"],
)
def test_a_decision_racing_another_outcome_never_reopens_or_runs_the_hold(
    stack: Stack,
    factory: sessionmaker[Session],
    decision: ApprovalAction,
    meanwhile: DbApprovalStatus,
) -> None:
    """An expiry or reject lands after this decision read the request as pending: the
    decision must not overwrite it, so a later approval cannot run the action."""
    approval_id = stack.hold()

    def land_meanwhile(_: Any) -> None:
        with factory() as session:
            session.execute(
                update(ApprovalRequestRow)
                .where(ApprovalRequestRow.id == approval_id)
                .values(status=meanwhile)
            )
            session.commit()

    with pytest.raises(AlreadyResolvedError):
        asyncio.run(
            stack.approvals.resolve(
                approval_id,
                ApprovalDecisionRequest(decision=decision),
                person(APPROVER),
                workspace_id=WS,
                before_decision=land_meanwhile,
            )
        )
    assert stack.request(approval_id).status.value == meanwhile.value
    with pytest.raises(AlreadyResolvedError):
        stack.decide(approval_id, ApprovalAction.APPROVE, by=SECOND, role=Role.ADMIN)
    assert stack.ports.calls == []


def test_a_redelivered_trigger_opens_no_second_hold_after_a_decision(stack: Stack) -> None:
    ctx = stack.ctx()
    first = stack.executor(WS).execute(webhook(), ctx)
    approval_id = uuid.UUID(first.detail["approval_id"])
    stack.decide(approval_id, ApprovalAction.REJECT)

    again = stack.executor(WS).execute(webhook(), ctx)

    assert again.detail == {"approval_id": str(approval_id)}
    holds = asyncio.run(
        stack.approvals.list(
            workspace_id=WS, actor=person(APPROVER), gate_type=GateType.AUTOMATION_ACTION
        )
    )
    assert [h.id for h in holds] == [approval_id]
    assert stack.ports.calls == []


def test_a_held_action_is_claimed_only_in_its_own_workspace(stack: Stack) -> None:
    request = stack.request(stack.hold())
    foreign = request.model_copy(update={"workspace_id": OTHER_WS})

    assert claim_held_action(stack.factory, foreign, released_by=SECOND) is None
    claimed = claim_held_action(stack.factory, request, released_by=APPROVER)
    assert claimed is not None
    assert claimed[0] == webhook()
    assert claim_held_action(stack.factory, request, released_by=APPROVER) is None


def test_the_hold_and_its_decision_are_audited_without_a_secret(stack: Stack) -> None:
    approval_id = stack.hold()

    held = [e for e in stack.automation_log.query() if e.status == "held"]
    assert [(e.action, e.metadata.get("approval_id")) for e in held] == [
        ("webhook_post", str(approval_id))
    ]
    requested = [r for r in stack.approval_audit.records if r["status"] == "requested"]
    assert [r["request_id"] for r in requested] == [approval_id]

    stack.decide(approval_id, ApprovalAction.APPROVE, note="agreed with ops")

    released = [r for r in stack.approval_audit.records if r["status"] == "held_action_released"]
    assert len(released) == 1
    assert released[0]["request_id"] == approval_id
    assert released[0]["actor"] == f"user:{APPROVER}"
    assert released[0]["metadata"]["action_type"] == "webhook_post"
    assert released[0]["metadata"]["result"] == "ok"
    # The run itself is on the automation chain as the person who approved it.
    ran = [e for e in stack.automation_log.query() if e.status == "ok"]
    assert [(e.action, e.actor) for e in ran] == [("webhook_post", f"user:{APPROVER}")]
    _no_secret(stack.approval_audit.records)
    _no_secret([e.model_dump(mode="json") for e in stack.automation_log.query()])


def test_a_caller_cannot_open_an_automation_action_gate(
    stack: Stack, authenticate_app: Any
) -> None:
    app = authenticate_app(create_app())
    app.dependency_overrides[get_approval_service] = lambda: stack.approvals
    response = TestClient(app).post(
        "/approvals",
        json={
            "gate_type": "automation_action",
            "subject_type": "automation_action",
            "subject_id": str(uuid.uuid4()),
            "gate_payload": {"action_type": "webhook_post"},
        },
    )
    assert response.status_code == 422, response.text
    assert "automation executor" in response.text


@pytest.mark.parametrize("url", ["http://127.0.0.1:8080/hook", "http://169.254.169.254/latest"])
def test_an_approved_webhook_to_an_internal_address_is_refused_unsent(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    posts: list[Any] = []
    monkeypatch.setattr(httpx, "post", lambda *a, **kw: posts.append(a))
    stack = Stack(factory, release=release_executor)
    approval_id = stack.hold(WebhookPostAction(url=url))

    resolution = stack.decide(approval_id, ApprovalAction.APPROVE)

    assert posts == []
    assert resolution.outcome.completed is False
    assert resolution.outcome.details["dispatch_error"] == "SsrfBlockedError"
    assert stack.request(approval_id).gate_payload["dispatch"]["status"] == "error"


def test_opening_a_hold_announces_it_once(stack: Stack, monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[Any] = []
    monkeypatch.setattr(approval_events, "publish_sync", lambda _url, event: events.append(event))
    approval_id = stack.hold()
    assert [(e.type.value, e.approval_id) for e in events] == [("approval.requested", approval_id)]


def test_a_hold_whose_action_was_not_stored_is_expired(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _fail(*_: Any) -> None:
        raise RuntimeError("held row not written")

    monkeypatch.setattr(ApprovalActionHolder, "_store", _fail)
    assert stack.executor(WS).execute(webhook(), stack.ctx()).status == "error"

    [hold] = asyncio.run(stack.approvals.list(workspace_id=WS, actor=person(APPROVER)))
    request = stack.request(hold.id)
    assert request.status is GateStatus.EXPIRED
    assert request.gate_payload["expired"]["reason"] == "held_action_not_stored"


def test_a_hold_expires_after_the_configured_window(stack: Stack) -> None:
    request = stack.request(stack.hold())
    assert request.expires_at is not None
    window = request.expires_at - request.requested_at
    ttl = timedelta(hours=get_settings().held_action_ttl_hours)
    assert abs(window - ttl) < timedelta(seconds=5)


@pytest.mark.postgres
def test_two_racing_approvals_run_the_hold_once(pg_engine: Any) -> None:
    """Both read the request as pending; the row lock lets only one write its outcome."""
    Base.metadata.create_all(pg_engine)
    try:
        factory = sessionmaker(bind=pg_engine, expire_on_commit=False)
        with factory() as session:
            session.add(Workspace(id=WS, name="Acme", slug=f"acme-{uuid.uuid4().hex[:6]}"))
            session.flush()
            for user_id in (AUTHOR, APPROVER, SECOND):
                session.add(
                    User(id=user_id, workspace_id=WS, email=f"{user_id}@acme.test", name="P")
                )
            session.commit()
        stack = Stack(factory)
        approval_id = stack.hold()
        voted, barrier = threading.local(), threading.Barrier(2)

        @event.listens_for(pg_engine, "after_cursor_execute")
        def _pause(_c: Any, _cur: Any, statement: str, *_: Any) -> None:
            if statement.startswith("INSERT INTO approval_decision"):
                voted.yes = True
            elif getattr(voted, "yes", False) and "FROM approval_request" in statement:
                voted.yes = False  # this thread's status write read the row: wait for the other
                with suppress(threading.BrokenBarrierError):
                    barrier.wait(timeout=3)

        outcomes: list[str] = []

        def _approve(by: uuid.UUID) -> None:
            try:
                stack.decide(approval_id, ApprovalAction.APPROVE, by=by)
                outcomes.append("approved")
            except AlreadyResolvedError:
                outcomes.append("refused")

        threads = [threading.Thread(target=_approve, args=(u,)) for u in (APPROVER, SECOND)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        event.remove(pg_engine, "after_cursor_execute", _pause)

        assert sorted(outcomes) == ["approved", "refused"]
        assert len(stack.ports.calls) == 1
    finally:
        Base.metadata.drop_all(pg_engine)
