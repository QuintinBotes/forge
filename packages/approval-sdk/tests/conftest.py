"""Shared fixtures for the forge_approval unit suite (slice F36 §7)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

import pytest

from forge_approval.audit import RecordingAuditSink
from forge_approval.authorizer import ApprovalAuthorizer
from forge_approval.events import InMemoryActivityBus
from forge_approval.models import (
    ApprovalAction,
    ApprovalContext,
    ApprovalDecisionRequest,
    ApprovalRequest,
    GateStatus,
    GateType,
    Principal,
    ResolutionOutcome,
    Role,
)
from forge_approval.registry import GateRegistry, default_actions
from forge_approval.repository import InMemoryApprovalRepository
from forge_approval.service import ApprovalService

WS = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
OTHER_WS = uuid.UUID("00000000-0000-0000-0000-0000000000a2")
ADMIN_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b1")
MEMBER_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b2")
VIEWER_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b3")
AGENT_ID = uuid.UUID("00000000-0000-0000-0000-0000000000c1")


def make_principal(
    kind: str = "user",
    role: Role | None = Role.MEMBER,
    principal_id: uuid.UUID | None = MEMBER_ID,
    workspace_id: uuid.UUID = WS,
) -> Principal:
    return Principal(kind=kind, id=principal_id, role=role, workspace_id=workspace_id)


@pytest.fixture
def principal_admin() -> Principal:
    return make_principal(role=Role.ADMIN, principal_id=ADMIN_ID)


@pytest.fixture
def principal_member() -> Principal:
    return make_principal()


@pytest.fixture
def principal_viewer() -> Principal:
    return make_principal(role=Role.VIEWER, principal_id=VIEWER_ID)


@pytest.fixture
def principal_agent() -> Principal:
    return make_principal(kind="agent", role=Role.AGENT_RUNNER, principal_id=AGENT_ID)


@pytest.fixture
def principal_system() -> Principal:
    return make_principal(kind="system", role=None, principal_id=None)


def make_request(gate_type: GateType = GateType.PR, **over: Any) -> ApprovalRequest:
    defaults: dict[str, Any] = {
        "id": uuid.uuid4(),
        "workspace_id": WS,
        "gate_type": gate_type,
        "status": GateStatus.PENDING,
        "subject_type": "workflow_run",
        "subject_id": uuid.uuid4(),
        "requested_actor": f"agent:{AGENT_ID}",
        "requested_at": datetime.now(UTC),
    }
    defaults.update(over)
    return ApprovalRequest(**defaults)


class RecordingProvider:
    """Fake GateContextProvider capturing build_context calls."""

    def __init__(self, gate_type: GateType) -> None:
        self.gate_type = gate_type
        self.calls: list[ApprovalRequest] = []

    async def build_context(
        self, request: ApprovalRequest, *, session: Any = None
    ) -> ApprovalContext:
        self.calls.append(request)
        return ApprovalContext(
            approval_id=request.id,
            gate_type=request.gate_type,
            goal=f"goal for {request.gate_type.value}",
            available_actions=self.available_actions(request),
            gate_payload=dict(request.gate_payload),
        )

    def available_actions(self, request: ApprovalRequest) -> list[ApprovalAction]:
        return default_actions(request.gate_type)


class ScriptedHook:
    """Fake GateResolutionHook returning a scripted outcome."""

    gate_type: ClassVar[GateType] = GateType.PR

    def __init__(self, gate_type: GateType, outcome: ResolutionOutcome) -> None:
        self.gate_type = gate_type
        self.outcome = outcome
        self.calls: list[tuple[ApprovalRequest, ApprovalDecisionRequest, Principal]] = []

    async def on_resolved(
        self,
        request: ApprovalRequest,
        decision: ApprovalDecisionRequest,
        actor: Principal,
        *,
        session: Any = None,
    ) -> ResolutionOutcome:
        self.calls.append((request, decision, actor))
        return self.outcome


class RecordingRedactor:
    """Redactor fake proving redaction ran pre-persist/emit."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(payload)
        return {
            key: "[REDACTED]" if "secret" in key.lower() else value
            for key, value in payload.items()
        }


@pytest.fixture
def fake_registry() -> GateRegistry:
    registry = GateRegistry()
    for gate_type in GateType:
        registry.register_provider(RecordingProvider(gate_type))
    registry.register_hook(
        ScriptedHook(GateType.PR, ResolutionOutcome(completed=True, follow_up_state="merged"))
    )
    return registry


@pytest.fixture
def fake_bus() -> InMemoryActivityBus:
    return InMemoryActivityBus()


@pytest.fixture
def fake_audit() -> RecordingAuditSink:
    return RecordingAuditSink()


@pytest.fixture
def fake_redactor() -> RecordingRedactor:
    return RecordingRedactor()


@pytest.fixture
def repo() -> InMemoryApprovalRepository:
    return InMemoryApprovalRepository()


@pytest.fixture
def service(
    repo: InMemoryApprovalRepository,
    fake_registry: GateRegistry,
    fake_bus: InMemoryActivityBus,
    fake_audit: RecordingAuditSink,
    fake_redactor: RecordingRedactor,
) -> ApprovalService:
    return ApprovalService(
        repo,
        fake_registry,
        ApprovalAuthorizer(),
        events=fake_bus,
        audit=fake_audit,
        redactor=fake_redactor,
    )


def soon(minutes: int = 5) -> datetime:
    return datetime.now(UTC) + timedelta(minutes=minutes)
