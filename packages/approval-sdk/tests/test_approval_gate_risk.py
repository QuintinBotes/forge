"""Risk at the approval gate (#194 criteria 3 and 6).

The approval gate decides how many distinct approvals a ``pr`` gate needs. Every
case of the existing create and quorum suites is collected here again and re-run
with a shadow reader that classifies every change as strictly as possible: each
must pass unchanged, with one observation per decision. Enforcing mode raises the
count to the class's approvals and never lowers it.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
import test_approval_service as _service_matrix
import test_policy_min_approvals as _min_matrix
import test_policy_quorum_wired as _quorum_matrix
from conftest import WS

from scf_approval import ApprovalAuthorizer, ApprovalService, GateRegistry
from scf_approval.models import (
    ApprovalAction,
    ApprovalDecisionRequest,
    GateStatus,
    GateType,
    Principal,
    Role,
)
from scf_approval.repository import InMemoryApprovalRepository
from scf_contracts import GatedChange
from scf_contracts.testing import RecordingRiskReader

_MATRIX = (_service_matrix, _quorum_matrix, _min_matrix)
# Criterion 3: the existing gate matrix, collected again under the shadow reader below.
for _module in _MATRIX:
    globals().update({n: t for n, t in vars(_module).items() if n.startswith("test_")})

RUN = uuid.uuid4()


@pytest.fixture(autouse=True)
def _shadow(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    if request.function.__module__ not in {m.__name__ for m in _MATRIX}:
        yield
        return
    reader = RecordingRiskReader("shadow")
    init = ApprovalService.__init__

    def with_risk(self: ApprovalService, *args: Any, **kwargs: Any) -> None:
        init(self, *args, risk=reader, **kwargs)

    monkeypatch.setattr(ApprovalService, "__init__", with_risk)
    yield
    reader.assert_observed_each_lookup("approval", {"1", "2", "3", "4", "5"})


def _service(reader: RecordingRiskReader) -> ApprovalService:
    return ApprovalService(
        InMemoryApprovalRepository(), GateRegistry(), ApprovalAuthorizer(), risk=reader
    )


async def _open(service: ApprovalService, gate_type: GateType = GateType.PR, **over: Any) -> Any:
    params: dict[str, Any] = {
        "workspace_id": WS,
        "gate_type": gate_type,
        "subject_type": "workflow_run",
        "subject_id": RUN,
        "workflow_run_id": RUN,
        "requested_actor": "system",
    }
    return await service.create(**params | over)


async def test_shadow_keeps_the_count_and_records_what_the_class_requires() -> None:
    reader = RecordingRiskReader("shadow", approvals=3)

    gate = await _open(_service(reader))

    assert gate.required_approvals == 1
    assert reader.looked_up == [GatedChange(workspace_id=WS, workflow_run_id=RUN)]
    assert reader.observed == [{"gate": "approval", "actual": "1", "risk_decision": "3"}]


async def test_enforcing_requires_the_approvals_of_the_changes_class() -> None:
    service = _service(RecordingRiskReader("enforcing", approvals=2))

    gate = await _open(service)

    assert gate.required_approvals == 2
    member = Principal(kind="user", id=uuid.uuid4(), role=Role.MEMBER, workspace_id=WS)
    approve = ApprovalDecisionRequest(decision=ApprovalAction.APPROVE)
    first = await service.resolve(gate.id, approve, member, workspace_id=WS)
    assert first.status is GateStatus.PENDING  # one approval no longer resolves it


async def test_enforcing_never_lowers_a_stricter_gate() -> None:
    reader = RecordingRiskReader("enforcing", approvals=2)

    gate = await _open(_service(reader), gate_payload={"review_rules": {"min_approvals": 3}})

    assert gate.required_approvals == 3
    assert reader.observed == [{"gate": "approval", "actual": "3", "risk_decision": "3"}]


async def test_a_gate_that_approves_no_change_is_not_classified() -> None:
    reader = RecordingRiskReader("enforcing", approvals=3)
    service = _service(reader)

    spec = await _open(service, GateType.SPEC)
    unattached = await _open(
        service, subject_type="policy_override", subject_id=uuid.uuid4(), workflow_run_id=None
    )

    assert (spec.required_approvals, unattached.required_approvals) == (1, 1)
    assert reader.looked_up == []
