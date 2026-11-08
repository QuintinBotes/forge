"""Spec K P0 (§4.7, ADR-0005): feature-run gates resolve through approvals (V1 engine).

Real handlers on hermetic SQLite, with the real approval service composed the
way the composition root composes it (``build_gate_registry`` with the gate
hooks over this test's database). A gate event reaches a run only through an
approval resolution: ``/transition`` refuses the four gate events, entering a
review state opens the request, and the decider becomes the transition actor.
"""

from __future__ import annotations

import asyncio
import base64
import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import StaticPool, create_engine, select
from sqlalchemy.orm import Session, sessionmaker

import forge_api.db
from forge_api.db import get_db
from forge_api.deps import Principal, get_current_principal
from forge_api.main import create_app
from forge_api.observability.redaction import redact_mapping
from forge_api.services.approval_service import build_gate_registry, get_approval_service
from forge_api.services.workflow_engine import select_workflow_engine
from forge_approval import ApprovalAuthorizer, ApprovalService, InMemoryApprovalRepository
from forge_approval.audit import RecordingAuditSink
from forge_approval.authorizer import DefaultPolicyReader
from forge_approval.events import APPROVAL_RESOLVED_TOPIC, InMemoryActivityBus
from forge_approval.models import ApprovalRequest, GateStatus
from forge_approval.providers import InMemoryGrantStore
from forge_contracts import UserRole
from forge_contracts.dtos import ReviewRules
from forge_db.base import Base
from forge_db.models import AgentRun, Attestation, Project, Task, Workspace

WS = uuid.UUID("00000000-0000-0000-0000-0000000000c1")
WS2 = uuid.UUID("00000000-0000-0000-0000-0000000000c2")

GATE_EVENTS = (
    "spec_approved_by_human",
    "plan_approved_by_human",
    "review_approved_by_human",
    "spec_changes_requested",
)

#: The V1 ``default_feature`` paths between the three gates.
_TO_SPEC_REVIEW = ("generate_spec_draft", "gather_clarifications", "submit_spec_for_review")
_TO_PLAN_REVIEW = ("generate_plan", "submit_plan_for_review")
_TO_AWAITING_REVIEW = (
    "generate_tasks",
    "start_agent_run",
    "run_checks",
    "all_checks_passed",
    "request_reviews",
)
_STATE_AT_GATE = {"spec": "spec_review", "plan": "plan_review", "pr": "awaiting_review"}


def _principal(
    workspace_id: uuid.UUID = WS,
    role: UserRole = UserRole.MEMBER,
    user_id: uuid.UUID | None = None,
) -> Principal:
    return Principal(
        user_id=user_id or uuid.uuid4(),
        workspace_id=workspace_id,
        role=role,
        email="member@acme.test",
        auth_method="test",
        scopes=["*"],
    )


class Harness:
    """Apps (one per principal) over one database and one approval service."""

    def __init__(self, factory: sessionmaker[Session], authorizer: ApprovalAuthorizer) -> None:
        self.factory = factory
        self.repo = InMemoryApprovalRepository()
        #: Submits the gate hook's engine factory refuses before it opens an engine.
        self.failing_submits = 0
        self.audit = RecordingAuditSink()
        self.events = InMemoryActivityBus()
        self.approvals = ApprovalService(
            self.repo,
            build_gate_registry(InMemoryGrantStore(), engine_factory=self._engine_for),
            authorizer,
            events=self.events,
            audit=self.audit,
            redactor=redact_mapping,
        )

    @contextmanager
    def _engine_for(self, workspace_id: uuid.UUID) -> Iterator[object]:
        if self.failing_submits:
            self.failing_submits -= 1
            raise RuntimeError("engine unavailable")
        with self.factory() as session:
            yield select_workflow_engine(session=session, workspace_id=workspace_id)
            session.commit()

    def client(self, principal: Principal) -> TestClient:
        app = create_app()

        def _get_db() -> Iterator[Session]:
            with self.factory() as session:
                yield session

        app.dependency_overrides[get_db] = _get_db
        app.dependency_overrides[get_approval_service] = lambda: self.approvals
        app.dependency_overrides[get_current_principal] = lambda: principal
        return TestClient(app)

    def requests_for(self, run_id: str) -> list[ApprovalRequest]:
        rows = asyncio.run(self.repo.list(workspace_id=WS))
        return [r for r in rows if r.subject_id == uuid.UUID(run_id)]

    def pending(self, run_id: str) -> list[ApprovalRequest]:
        return [r for r in self.requests_for(run_id) if r.status is GateStatus.PENDING]

    def request(self, request_id: uuid.UUID) -> ApprovalRequest:
        return asyncio.run(self.approvals.get(request_id, workspace_id=WS))

    def seed_task(self, workspace_id: uuid.UUID = WS) -> uuid.UUID:
        with self.factory() as s:
            project = Project(
                workspace_id=workspace_id, name="Core", key=f"C{uuid.uuid4().hex[:4]}"
            )
            s.add(project)
            s.flush()
            task = Task(
                workspace_id=workspace_id,
                project_id=project.id,
                key=f"TASK-{uuid.uuid4().hex[:6]}",
                title="gated feature",
            )
            s.add(task)
            s.commit()
            return task.id

    def seed_pr_evidence(self, run_id: str, *, merge_signals: bool) -> None:
        """What a real run has by ``awaiting_review``: an agent run to attest,
        and (when CI reported) the merge guard's signals in the run context."""
        rid = uuid.UUID(run_id)
        with self.factory() as s:
            engine = select_workflow_engine(session=s, workspace_id=WS)
            run = engine.get_run(rid)
            s.add(
                AgentRun(
                    workspace_id=WS,
                    workflow_run_id=rid,
                    task_id=run.task_id,
                    role="implementer",
                    model="claude-sonnet-4-5",
                    sandbox_kind="gvisor",
                    steps=[{"index": 0, "kind": "tool_call", "tool_call": {"tool": "edit_file"}}],
                )
            )
            if merge_signals:
                engine.update_context(rid, {"ci_status_green": True, "spec_validated": True})
            s.commit()


@pytest.fixture
def factory() -> Iterator[sessionmaker[Session]]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    sf: sessionmaker[Session] = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with sf() as s:
        s.add(Workspace(id=WS, name="Acme", slug="acme"))
        s.add(Workspace(id=WS2, name="Rival", slug="rival"))
        s.commit()
    yield sf
    engine.dispose()


@pytest.fixture
def make_harness(factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch):
    # The pr gate's attestation hook opens its session from the process
    # factory; point it at this database. No signing key is configured, as on
    # a deployment without one (the signer falls back to an ephemeral key).
    monkeypatch.setattr(forge_api.db, "get_session_factory", lambda: factory)
    monkeypatch.delenv("FORGE_ATTEST_SIGNING_KEY", raising=False)

    def _make(authorizer: ApprovalAuthorizer | None = None) -> Harness:
        return Harness(factory, authorizer or ApprovalAuthorizer())

    return _make


@pytest.fixture
def h(make_harness) -> Harness:
    return make_harness()


@pytest.fixture
def member(h: Harness) -> TestClient:
    """Drives runs (WRITE); never decides a gate in these tests."""
    return h.client(_principal())


@pytest.fixture
def decider_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def decider(h: Harness, decider_id: uuid.UUID) -> TestClient:
    return h.client(_principal(user_id=decider_id))


def _start(h: Harness, client: TestClient) -> str:
    resp = client.post("/workflow/runs", json={"task_id": str(h.seed_task())})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _drive(client: TestClient, run_id: str, *events: str) -> None:
    for event in events:
        resp = client.post(f"/workflow/runs/{run_id}/transition", json={"event": event})
        assert resp.status_code == 200, f"{event}: {resp.text}"


def _run(client: TestClient, run_id: str) -> dict:
    resp = client.get(f"/workflow/runs/{run_id}")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _the_pending(h: Harness, run_id: str, gate: str) -> ApprovalRequest:
    pending = h.pending(run_id)
    assert len(pending) == 1, pending
    assert pending[0].gate_type.value == gate
    return pending[0]


def _decide(client: TestClient, request_id: uuid.UUID, decision: str, *, expect: int = 200) -> dict:
    resp = client.post(f"/approvals/{request_id}/decision", json={"decision": decision})
    assert resp.status_code == expect, resp.text
    return resp.json()


def _attestations(h: Harness, run_id: str) -> int:
    with h.factory() as s:
        rows = s.scalars(
            select(Attestation).where(Attestation.workflow_run_id == uuid.UUID(run_id))
        )
        return len(rows.all())


def _approved_then_crashed(h: Harness, request: ApprovalRequest, decider_id: uuid.UUID) -> None:
    """What the repository holds when the process dies after ``resolve`` recorded the
    decision but before the hook attested: ``approved``, no attestation, no marker."""
    request.status = GateStatus.APPROVED
    request.resolved_at = datetime.now(UTC)
    request.resolver_user_id = decider_id
    asyncio.run(h.repo.update(request))


def _to_gate(h: Harness, member: TestClient, approver: TestClient, gate: str) -> str:
    """A fresh run waiting at ``gate``, earlier gates passed through approvals."""
    run_id = _start(h, member)
    _drive(member, run_id, *_TO_SPEC_REVIEW)
    if gate == "spec":
        return run_id
    _decide(approver, _the_pending(h, run_id, "spec").id, "approve")
    _drive(member, run_id, *_TO_PLAN_REVIEW)
    if gate == "plan":
        return run_id
    _decide(approver, _the_pending(h, run_id, "plan").id, "approve")
    _drive(member, run_id, *_TO_AWAITING_REVIEW)
    assert _run(member, run_id)["current_state"] == "awaiting_review"
    return run_id


# --------------------------------------------------------------------------- #
# 1a. Entering a review state opens exactly one request for the run           #
# --------------------------------------------------------------------------- #


def test_reaching_spec_review_opens_one_pending_request_for_the_run(
    h: Harness, member: TestClient
) -> None:
    run_id = _start(h, member)
    _drive(member, run_id, *_TO_SPEC_REVIEW[:2])
    assert h.requests_for(run_id) == []  # nothing before the gate

    _drive(member, run_id, _TO_SPEC_REVIEW[2])

    request = _the_pending(h, run_id, "spec")
    assert request.subject_type == "workflow_run"
    assert request.subject_id == uuid.UUID(run_id)
    assert request.workflow_run_id == uuid.UUID(run_id)
    assert request.requested_actor == f"agent:{run_id}"
    assert request.title == "gated feature"  # the run's task title


def test_reentering_the_spec_gate_never_leaves_two_pending(
    h: Harness, member: TestClient, decider: TestClient
) -> None:
    run_id = _to_gate(h, member, decider, "spec")
    first = _the_pending(h, run_id, "spec")
    # The engine's open is idempotent while pending (a retried open, as the
    # Temporal activity's at-least-once delivery would make).
    again = asyncio.run(
        h.approvals.create(
            workspace_id=WS,
            gate_type=first.gate_type,
            subject_type="workflow_run",
            subject_id=uuid.UUID(run_id),
            workflow_run_id=uuid.UUID(run_id),
            requested_actor=f"agent:{run_id}",
        )
    )
    assert again.id == first.id

    _decide(decider, first.id, "request_changes")  # spec_review -> clarification
    _drive(member, run_id, "submit_spec_for_review")  # re-enter the gate

    second = _the_pending(h, run_id, "spec")
    assert second.id != first.id
    assert h.request(first.id).status is GateStatus.CHANGES_REQUESTED
    assert len(h.requests_for(run_id)) == 2


# --------------------------------------------------------------------------- #
# 1b. Approval moves the run, with the decider as the transition actor        #
# --------------------------------------------------------------------------- #


def test_approving_the_spec_gate_moves_the_run_as_the_decider(
    h: Harness, member: TestClient, decider: TestClient, decider_id: uuid.UUID
) -> None:
    run_id = _to_gate(h, member, decider, "spec")
    request = _the_pending(h, run_id, "spec")

    body = _decide(decider, request.id, "approve")

    assert body["status"] == "approved"
    run = _run(member, run_id)
    assert run["current_state"] == "spec_approved"
    assert run["context"]["last_event"] == "spec_approved_by_human"
    assert run["context"]["last_actor"] == f"user:{decider_id}"
    assert h.request(request.id).gate_payload["workflow_event"] == {
        "status": "submitted",
        "event": "spec_approved_by_human",
        "actor": f"user:{decider_id}",
        "to_state": "spec_approved",
    }
    assert h.pending(run_id) == []


# --------------------------------------------------------------------------- #
# 1c. Changes / rejection                                                     #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("decision", ["request_changes", "reject"])
def test_spec_changes_or_rejection_sends_the_run_back(
    h: Harness, member: TestClient, decider: TestClient, decider_id: uuid.UUID, decision: str
) -> None:
    run_id = _to_gate(h, member, decider, "spec")
    request = _the_pending(h, run_id, "spec")

    _decide(decider, request.id, decision)

    run = _run(member, run_id)
    assert run["current_state"] == "clarification"
    assert run["context"]["last_event"] == "spec_changes_requested"
    assert run["context"]["last_actor"] == f"user:{decider_id}"
    assert h.request(request.id).gate_payload["workflow_event"]["event"] == (
        "spec_changes_requested"
    )


@pytest.mark.parametrize(
    ("decision", "status"), [("reject", "rejected"), ("request_changes", "changes_requested")]
)
@pytest.mark.parametrize("gate", ["plan", "pr"])
def test_rejecting_a_plan_or_pr_gate_cancels_the_run_as_the_decider(
    h: Harness,
    member: TestClient,
    decider: TestClient,
    decider_id: uuid.UUID,
    gate: str,
    decision: str,
    status: str,
) -> None:
    """Spec K §4.11: there is no edge back from plan or pr review, so a rejection
    ends the run (rework is a new run)."""
    run_id = _to_gate(h, member, decider, gate)
    if gate == "pr":
        h.seed_pr_evidence(run_id, merge_signals=True)
    request = _the_pending(h, run_id, gate)

    body = _decide(decider, request.id, decision)

    assert body["status"] == status
    run = _run(member, run_id)
    assert (run["current_state"], run["status"]) == ("cancelled", "cancelled")
    assert run["completed_at"] is not None
    assert run["context"]["last_event"] == "cancel"
    assert run["context"]["last_actor"] == f"user:{decider_id}"
    assert run["context"]["cancel_reason"] == f"gate_{gate}_{status}"
    assert h.request(request.id).gate_payload["workflow_event"] == {
        "status": "submitted",
        "event": "cancel",
        "actor": f"user:{decider_id}",
        "to_state": "cancelled",
        "reason": f"gate_{gate}_{status}",
    }

    # Sync applies nothing more to a cancelled run.
    synced = member.post(f"/workflow/runs/{run_id}/gates/{gate}/sync")
    assert synced.status_code == 200, synced.text
    assert synced.json() == run


def test_a_failed_cancel_is_recovered_by_sync_once(
    h: Harness, member: TestClient, decider: TestClient, decider_id: uuid.UUID
) -> None:
    run_id = _to_gate(h, member, decider, "plan")
    request = _the_pending(h, run_id, "plan")
    h.failing_submits = 1

    _decide(decider, request.id, "reject")

    assert h.request(request.id).gate_payload["workflow_event"]["status"] == "failed"
    assert _run(member, run_id)["current_state"] == "plan_review"

    synced = member.post(f"/workflow/runs/{run_id}/gates/plan/sync")
    assert synced.status_code == 200, synced.text
    run = synced.json()
    assert (run["current_state"], run["status"]) == ("cancelled", "cancelled")
    assert run["context"]["last_actor"] == f"user:{decider_id}"
    assert run["context"]["cancel_reason"] == "gate_plan_rejected"

    again = member.post(f"/workflow/runs/{run_id}/gates/plan/sync")
    assert again.status_code == 200, again.text
    assert again.json() == run


# --------------------------------------------------------------------------- #
# 1d. The approval service's rules apply to workflow-run gates                #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("role", [UserRole.VIEWER, UserRole.AGENT_RUNNER])
def test_a_principal_who_may_not_decide_is_refused_and_the_run_stays(
    h: Harness, member: TestClient, decider: TestClient, role: UserRole
) -> None:
    run_id = _to_gate(h, member, decider, "spec")
    request = _the_pending(h, run_id, "spec")

    _decide(h.client(_principal(role=role)), request.id, "approve", expect=403)

    assert _run(member, run_id)["current_state"] == "spec_review"
    assert h.request(request.id).status is GateStatus.PENDING


def test_the_runs_agent_identity_cannot_approve_its_own_gate(
    h: Harness, member: TestClient, decider: TestClient
) -> None:
    """The gate is requested by ``agent:<run id>``, so the authorizer's
    agent-author rule refuses that identity even as a member. (The opt-in
    ``forbid_self_approval`` rule keys on ``user:<id>`` requesters and cannot
    match an engine-opened gate.)"""
    run_id = _to_gate(h, member, decider, "spec")
    request = _the_pending(h, run_id, "spec")
    as_run_agent = h.client(_principal(user_id=uuid.UUID(run_id)))

    body = _decide(as_run_agent, request.id, "approve", expect=403)

    assert "agent identity" in body["detail"]
    assert _run(member, run_id)["current_state"] == "spec_review"


class _TwoApprovalsForPr(DefaultPolicyReader):
    """Repo ``review_rules.min_approvals: 2``. Engine-opened gates carry no
    ``review_rules`` payload, and spec/plan gates never take a policy quorum,
    so a policy reader is the only way a workflow-run gate needs two."""

    def review_rules_for(self, request: ApprovalRequest) -> ReviewRules:
        return ReviewRules(min_approvals=2)


def test_a_two_approval_pr_gate_moves_the_run_only_on_the_second_approval(
    make_harness,
) -> None:
    h = make_harness(ApprovalAuthorizer(_TwoApprovalsForPr()))
    member = h.client(_principal())
    first, second = h.client(_principal()), h.client(_principal())
    run_id = _to_gate(h, member, first, "pr")
    h.seed_pr_evidence(run_id, merge_signals=True)
    request = _the_pending(h, run_id, "pr")
    assert request.required_approvals == 2

    body = _decide(first, request.id, "approve")

    assert body["status"] == "pending"
    assert _run(member, run_id)["current_state"] == "awaiting_review"
    _decide(first, request.id, "approve", expect=409)  # one vote per approver

    assert _decide(second, request.id, "approve")["status"] == "approved"
    assert _run(member, run_id)["current_state"] == "merged"


# --------------------------------------------------------------------------- #
# 1e. /transition refuses the gate events                                     #
# --------------------------------------------------------------------------- #

_GATE_FOR_EVENT = {
    "spec_approved_by_human": "spec",
    "spec_changes_requested": "spec",
    "plan_approved_by_human": "plan",
    "review_approved_by_human": "pr",
}


@pytest.mark.parametrize("event", GATE_EVENTS)
def test_transition_refuses_gate_events(
    h: Harness, member: TestClient, decider: TestClient, event: str
) -> None:
    """At the very gate where the event would otherwise apply."""
    gate = _GATE_FOR_EVENT[event]
    run_id = _to_gate(h, member, decider, gate)
    if gate == "pr":
        h.seed_pr_evidence(run_id, merge_signals=True)
    before = _run(member, run_id)

    resp = member.post(f"/workflow/runs/{run_id}/transition", json={"event": event})

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == "gate_events_come_from_approvals"
    after = _run(member, run_id)
    assert after["current_state"] == _STATE_AT_GATE[gate]
    assert after["context"] == before["context"]


# --------------------------------------------------------------------------- #
# 1f. A failed submit keeps the decision; gate sync recovers                  #
# --------------------------------------------------------------------------- #


def test_failed_submit_is_recorded_and_sync_recovers_it_once(
    h: Harness, member: TestClient, decider: TestClient, decider_id: uuid.UUID
) -> None:
    run_id = _to_gate(h, member, decider, "spec")
    request = _the_pending(h, run_id, "spec")
    h.failing_submits = 1

    body = _decide(decider, request.id, "approve")

    assert body["status"] == "approved"  # the decision stands
    stored = h.request(request.id)
    assert stored.status is GateStatus.APPROVED
    assert stored.resolver_user_id == decider_id
    event = stored.gate_payload["workflow_event"]
    assert event["status"] == "failed"
    assert event["event"] == "spec_approved_by_human"
    assert "engine unavailable" in event["error"]
    assert _run(member, run_id)["current_state"] == "spec_review"

    # Any writer may sync; the transition is still the decider's.
    synced = member.post(f"/workflow/runs/{run_id}/gates/spec/sync")
    assert synced.status_code == 200, synced.text
    assert synced.json()["current_state"] == "spec_approved"
    assert synced.json()["context"]["last_actor"] == f"user:{decider_id}"

    again = member.post(f"/workflow/runs/{run_id}/gates/spec/sync")
    assert again.status_code == 200, again.text
    assert again.json() == synced.json()


def test_sync_uses_the_latest_request_for_the_gate(
    h: Harness, member: TestClient, decider: TestClient
) -> None:
    """Changes requested, resubmitted, then an approval whose submit fails:
    sync must apply the approval, never re-apply the old changes request."""
    run_id = _to_gate(h, member, decider, "spec")
    _decide(decider, _the_pending(h, run_id, "spec").id, "request_changes")
    _drive(member, run_id, "submit_spec_for_review")
    h.failing_submits = 1
    _decide(decider, _the_pending(h, run_id, "spec").id, "approve")
    assert _run(member, run_id)["current_state"] == "spec_review"

    synced = member.post(f"/workflow/runs/{run_id}/gates/spec/sync")

    assert synced.status_code == 200, synced.text
    assert synced.json()["current_state"] == "spec_approved"


def test_sync_of_a_foreign_run_is_404(h: Harness, member: TestClient, decider: TestClient) -> None:
    run_id = _to_gate(h, member, decider, "spec")
    h.failing_submits = 1
    _decide(decider, _the_pending(h, run_id, "spec").id, "approve")

    foreign = h.client(_principal(workspace_id=WS2))
    resp = foreign.post(f"/workflow/runs/{run_id}/gates/spec/sync")

    assert resp.status_code == 404
    assert _run(member, run_id)["current_state"] == "spec_review"


def test_sync_while_the_gate_is_pending_changes_nothing(
    h: Harness, member: TestClient, decider: TestClient
) -> None:
    run_id = _to_gate(h, member, decider, "spec")
    before = _run(member, run_id)

    resp = member.post(f"/workflow/runs/{run_id}/gates/spec/sync")

    assert resp.status_code == 200, resp.text
    assert resp.json() == before
    assert _the_pending(h, run_id, "spec").status is GateStatus.PENDING


# --------------------------------------------------------------------------- #
# 1g. Plan and PR gates                                                       #
# --------------------------------------------------------------------------- #


def test_approving_the_plan_gate_moves_the_run_on(
    h: Harness, member: TestClient, decider: TestClient, decider_id: uuid.UUID
) -> None:
    run_id = _to_gate(h, member, decider, "plan")
    request = _the_pending(h, run_id, "plan")
    assert request.requested_actor == f"agent:{run_id}"

    _decide(decider, request.id, "approve")

    run = _run(member, run_id)
    assert run["current_state"] == "task_generation"
    assert run["context"]["last_actor"] == f"user:{decider_id}"


def test_approving_the_pr_gate_attests_then_merges(
    h: Harness, member: TestClient, decider: TestClient, decider_id: uuid.UUID
) -> None:
    run_id = _to_gate(h, member, decider, "pr")
    h.seed_pr_evidence(run_id, merge_signals=True)
    request = _the_pending(h, run_id, "pr")

    body = _decide(decider, request.id, "approve")

    # The wrapped attestation hook ran (no signing key: an ephemeral one).
    details = body["outcome"]["details"]
    assert details["attested"] is True
    with h.factory() as s:
        attestation = s.scalars(
            select(Attestation).where(Attestation.workflow_run_id == uuid.UUID(run_id))
        ).one()
    assert str(attestation.id) == details["attestation_id"]
    # ...and the run merged, as the decider.
    assert details["workflow_event"] == "review_approved_by_human"
    run = _run(member, run_id)
    assert run["current_state"] == "merged"
    assert run["context"]["last_actor"] == f"user:{decider_id}"


def test_an_incomplete_attestation_blocks_the_gate_like_a_failed_one(
    h: Harness,
    member: TestClient,
    decider: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An attestation hook that returns ``completed=False`` is no proof: the hook
    and sync both block the run, exactly as when it raises."""
    from forge_api.services.attestation_service import PrAttestationResolutionHook
    from forge_approval.models import ResolutionOutcome

    async def _incomplete(*_a: object, **_k: object) -> ResolutionOutcome:
        return ResolutionOutcome(completed=False, blocking_reasons=["no_session"])

    monkeypatch.setattr(PrAttestationResolutionHook, "on_resolved", _incomplete)
    run_id = _to_gate(h, member, decider, "pr")
    h.seed_pr_evidence(run_id, merge_signals=True)
    request = _the_pending(h, run_id, "pr")

    _decide(decider, request.id, "approve")

    assert h.request(request.id).gate_payload["workflow_event"]["status"] == "blocked"
    assert _run(member, run_id)["current_state"] == "awaiting_review"
    synced = member.post(f"/workflow/runs/{run_id}/gates/pr/sync")
    assert synced.status_code == 409, synced.text
    assert synced.json()["detail"] == "gate_blocked"
    assert _run(member, run_id)["current_state"] == "awaiting_review"
    assert _attestations(h, run_id) == 0


def test_a_failed_attestation_keeps_the_decision_and_blocks_the_gate(
    h: Harness,
    member: TestClient,
    decider: TestClient,
    decider_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec K §4.11: a raising pr attestation hook never turns the recorded
    decision into a 500, and never lets the run merge (not even through sync)."""
    from forge_api.services.attestation_service import AttestationService

    def _fail(*_a: object, **_k: object) -> None:
        raise RuntimeError("signing key unavailable")

    attest = AttestationService.attest_changeset
    monkeypatch.setattr(AttestationService, "attest_changeset", _fail)
    run_id = _to_gate(h, member, decider, "pr")
    h.seed_pr_evidence(run_id, merge_signals=True)
    request = _the_pending(h, run_id, "pr")

    body = _decide(decider, request.id, "approve")

    assert body["status"] == "approved"
    assert body["outcome"]["completed"] is False
    assert body["outcome"]["details"]["error_code"] == "resolution_hook_failed"
    stored = h.request(request.id)
    assert stored.status is GateStatus.APPROVED
    assert stored.resolver_user_id == decider_id
    decisions = asyncio.run(h.approvals.decisions(request.id, workspace_id=WS))
    assert [d.approver_user_id for d in decisions] == [decider_id]
    assert h.audit.records[-1]["status"] == "approved"
    assert [e.approval_id for e in h.events.by_topic(APPROVAL_RESOLVED_TOPIC)][-1] == request.id
    assert stored.gate_payload["workflow_event"] == {
        "status": "blocked",
        "reason": "attestation_failed",
        "event": "review_approved_by_human",
        "actor": f"user:{decider_id}",
    }
    assert _run(member, run_id)["current_state"] == "awaiting_review"

    synced = member.post(f"/workflow/runs/{run_id}/gates/pr/sync")
    assert synced.status_code == 409, synced.text
    assert synced.json()["detail"] == "gate_blocked"
    assert _run(member, run_id)["current_state"] == "awaiting_review"
    assert _attestations(h, run_id) == 0

    # Sync is also the blocked gate's recovery route: once attesting works, it merges.
    monkeypatch.setattr(AttestationService, "attest_changeset", attest)
    recovered = member.post(f"/workflow/runs/{run_id}/gates/pr/sync")
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["current_state"] == "merged"
    assert recovered.json()["context"]["last_actor"] == f"user:{decider_id}"
    assert _attestations(h, run_id) == 1


def test_sync_attests_a_pr_approval_left_unattested_then_merges(
    h: Harness, member: TestClient, decider: TestClient, decider_id: uuid.UUID
) -> None:
    run_id = _to_gate(h, member, decider, "pr")
    h.seed_pr_evidence(run_id, merge_signals=True)
    _approved_then_crashed(h, _the_pending(h, run_id, "pr"), decider_id)

    synced = member.post(f"/workflow/runs/{run_id}/gates/pr/sync")

    assert synced.status_code == 200, synced.text
    assert synced.json()["current_state"] == "merged"
    with h.factory() as s:
        attestation = s.scalars(
            select(Attestation).where(Attestation.workflow_run_id == uuid.UUID(run_id))
        ).one()
    statement = json.loads(base64.b64decode(attestation.envelope["payload"]))
    assert statement["predicate"]["human_approver"] == str(decider_id)  # not the syncing member


def test_sync_never_merges_a_pr_approval_it_cannot_attest(
    h: Harness,
    member: TestClient,
    decider: TestClient,
    decider_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from forge_api.services.attestation_service import AttestationService

    def _fail(*_a: object, **_k: object) -> None:
        raise RuntimeError("db unavailable")

    run_id = _to_gate(h, member, decider, "pr")
    h.seed_pr_evidence(run_id, merge_signals=True)
    request = _the_pending(h, run_id, "pr")
    _approved_then_crashed(h, request, decider_id)
    monkeypatch.setattr(AttestationService, "attest_changeset", _fail)

    synced = member.post(f"/workflow/runs/{run_id}/gates/pr/sync")

    assert synced.status_code == 409, synced.text
    assert synced.json()["detail"] == "gate_blocked"
    assert _run(member, run_id)["current_state"] == "awaiting_review"
    assert _attestations(h, run_id) == 0
    stored = h.request(request.id)
    assert stored.status is GateStatus.APPROVED
    assert stored.gate_payload["workflow_event"] == {
        "status": "blocked",
        "reason": "attestation_failed",
        "event": "review_approved_by_human",
        "actor": f"user:{decider_id}",
    }


def test_pr_approval_without_merge_signals_is_recorded_and_synced_later(
    h: Harness, member: TestClient, decider: TestClient
) -> None:
    """The merge guard needs ``ci_status_green`` and ``spec_validated``; an
    approval before they hold records the failure, and sync applies it once
    they do."""
    run_id = _to_gate(h, member, decider, "pr")
    h.seed_pr_evidence(run_id, merge_signals=False)
    request = _the_pending(h, run_id, "pr")

    _decide(decider, request.id, "approve")

    assert h.request(request.id).gate_payload["workflow_event"]["status"] == "failed"
    assert _run(member, run_id)["current_state"] == "awaiting_review"
    assert _attestations(h, run_id) == 1  # the approval attested before the guard refused

    with h.factory() as s:
        engine = select_workflow_engine(session=s, workspace_id=WS)
        engine.update_context(uuid.UUID(run_id), {"ci_status_green": True, "spec_validated": True})
        s.commit()
    resp = member.post(f"/workflow/runs/{run_id}/gates/pr/sync")
    assert resp.status_code == 200, resp.text
    assert resp.json()["current_state"] == "merged"
    assert _attestations(h, run_id) == 1  # the existing attestation is the proof: not redone


# --------------------------------------------------------------------------- #
# 1h. Callers cannot open workflow-run gates                                  #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("gate", ["spec", "plan", "pr"])
def test_post_approvals_refuses_workflow_run_gates(
    h: Harness, member: TestClient, gate: str
) -> None:
    run_id = uuid.uuid4()
    resp = member.post(
        "/approvals",
        json={
            "gate_type": gate,
            "subject_type": "workflow_run",
            "subject_id": str(run_id),
            "workflow_run_id": str(run_id),
            "requested_actor": f"agent:{run_id}",
            "gate_payload": {"ci_status_green": True, "spec_validated": True},
        },
    )

    assert resp.status_code == 422, resp.text
    assert asyncio.run(h.repo.list(workspace_id=WS)) == []


def test_a_caller_made_request_cannot_take_over_a_runs_gate(
    h: Harness, member: TestClient, decider: TestClient
) -> None:
    run_id = _start(h, member)
    forged = member.post(
        "/approvals",
        json={"gate_type": "spec", "subject_type": "workflow_run", "subject_id": run_id},
    )
    assert forged.status_code == 422, forged.text
    _drive(member, run_id, *_TO_SPEC_REVIEW)

    _decide(decider, _the_pending(h, run_id, "spec").id, "approve")

    assert _run(member, run_id)["current_state"] == "spec_approved"
