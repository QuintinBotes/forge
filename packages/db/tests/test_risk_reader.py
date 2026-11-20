"""The production risk reader: a change's stored classification and its gate observations (#194).

Criterion 7 (a change cannot pass as a class below its derived one without a
named override) and criterion 9 (the reader reads #193's rows and writes
observations). Classifications are inserted as rows, so a row that lowers its
class without an override can exist here as it could in a damaged database.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import StaticPool, create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from scf_contracts import (
    GatedChange,
    GateRequirements,
    RiskClass,
    RiskReader,
    gate_decision,
    tighten_deploy,
)
from scf_db.base import Base
from scf_db.models import AgentRun, RiskClassification, RiskGateObservation, Workspace
from scf_db.models.enums import RunStatus
from scf_db.risk_repository import DbRiskReader, RiskRepository

WS = uuid.UUID("00000000-0000-0000-0000-000000000194")
OTHER_WS = uuid.UUID("00000000-0000-0000-0000-000000000195")
WORKFLOW_RUN = uuid.UUID("00000000-0000-0000-0000-0000000001f4")
#: Every class's requirements, as close-out records them.
REQUIREMENTS: dict[str, dict[str, Any]] = {
    risk_class.value: GateRequirements(
        risk_class=risk_class,
        approvals=index,
        deploy_mode="gated" if index >= 2 else "auto",
        red_team=index == 3,
        self_eval=index >= 1,
    ).model_dump(mode="json")
    for index, risk_class in enumerate(RiskClass)
}


@pytest.fixture
def factory() -> Iterator[sessionmaker[Session]]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with session_factory() as s:
        s.add_all(
            [
                Workspace(id=WS, name="Acme", slug="acme"),
                Workspace(id=OTHER_WS, name="Other", slug="other"),
            ]
        )
        s.commit()
    yield session_factory
    engine.dispose()


def _classify(
    factory: sessionmaker[Session],
    *,
    workspace_id: uuid.UUID = WS,
    commit_sha: str = "c" * 40,
    agent_run_id: uuid.UUID | None = None,
    workflow_run_id: uuid.UUID | None = None,
    derived: RiskClass = RiskClass.HIGH,
    final: RiskClass | None = None,
    override: tuple[str, str] | None = None,
    mode: str = "shadow",
    requirements: dict[str, Any] | None = REQUIREMENTS,
    age: timedelta = timedelta(0),
) -> uuid.UUID:
    with factory() as s:
        if agent_run_id is not None:
            s.add(AgentRun(id=agent_run_id, workspace_id=workspace_id, status=RunStatus.SUCCEEDED))
            s.flush()
        row = RiskClassification(
            workspace_id=workspace_id,
            commit_sha=commit_sha,
            agent_run_id=agent_run_id,
            workflow_run_id=workflow_run_id,
            derived_class=derived,
            final_class=final or derived,
            override_approver=override[0] if override else None,
            override_reason=override[1] if override else None,
            enforcement_mode=mode,
            class_requirements=requirements,
            created_at=datetime.now(UTC) - age,
        )
        s.add(row)
        s.commit()
        return row.id


def test_the_reader_is_a_risk_reader(factory: sessionmaker[Session]) -> None:
    assert isinstance(DbRiskReader(factory), RiskReader)


def test_the_reader_returns_the_classification_its_class_requires_and_its_mode(
    factory: sessionmaker[Session],
) -> None:
    classification = _classify(factory, derived=RiskClass.HIGH, mode="enforcing")

    found = DbRiskReader(factory).risk_for(GatedChange(workspace_id=WS, commit_sha="c" * 40))

    assert found is not None
    assert (found.classification_id, found.workspace_id) == (classification, WS)
    assert found.enforcement == "enforcing"
    assert found.requirements == GateRequirements.model_validate(REQUIREMENTS["high"])


def test_the_most_specific_identity_with_a_classification_wins(
    factory: sessionmaker[Session],
) -> None:
    run = uuid.uuid4()
    by_run = _classify(factory, commit_sha="a" * 40, agent_run_id=run)
    older = _classify(factory, commit_sha="b" * 40, workflow_run_id=WORKFLOW_RUN, age=timedelta(1))
    newest = _classify(factory, commit_sha="d" * 40, workflow_run_id=WORKFLOW_RUN)
    reader = DbRiskReader(factory)

    def found(**identity: Any) -> uuid.UUID | None:
        risk = reader.risk_for(GatedChange(workspace_id=WS, **identity))
        return risk.classification_id if risk else None

    assert found(commit_sha="b" * 40, agent_run_id=run) == older  # the commit first
    assert found(commit_sha="e" * 40, agent_run_id=run) == by_run  # then the agent run
    assert found(commit_sha="e" * 40, workflow_run_id=WORKFLOW_RUN) == newest  # newest of the run
    assert older != newest
    assert found(commit_sha="e" * 40) is None
    assert found() is None


def test_another_workspaces_classification_is_never_read(factory: sessionmaker[Session]) -> None:
    _classify(factory, workspace_id=OTHER_WS)
    change = GatedChange(workspace_id=WS, commit_sha="c" * 40)
    assert DbRiskReader(factory).risk_for(change) is None


def test_a_classification_recorded_without_requirements_is_not_risk(
    factory: sessionmaker[Session],
) -> None:
    _classify(factory, requirements=None)
    change = GatedChange(workspace_id=WS, commit_sha="c" * 40)
    assert DbRiskReader(factory).risk_for(change) is None


@pytest.mark.parametrize(
    ("override", "ships_as"),
    [
        (None, RiskClass.CRITICAL),
        (("", "hotfix"), RiskClass.CRITICAL),
        (("lead@example.test", " "), RiskClass.CRITICAL),
        (("lead@example.test", "reviewed by hand"), RiskClass.LOW),
    ],
)
def test_a_critical_change_passes_as_a_lower_class_only_with_a_named_override(
    factory: sessionmaker[Session], override: tuple[str, str] | None, ships_as: RiskClass
) -> None:
    """Criterion 7: a path matching a ``critical`` rule derives ``critical``; a row whose
    final class is lower without an override approver and reason is read as critical."""
    _classify(factory, derived=RiskClass.CRITICAL, final=RiskClass.LOW, override=override)

    found = DbRiskReader(factory).risk_for(GatedChange(workspace_id=WS, commit_sha="c" * 40))

    assert found is not None
    assert found.requirements.risk_class is ships_as
    assert found.requirements == GateRequirements.model_validate(REQUIREMENTS[ships_as.value])


def test_a_class_declared_above_the_floor_ships_as_declared(factory: sessionmaker[Session]) -> None:
    _classify(factory, derived=RiskClass.LOW, final=RiskClass.HIGH)
    found = DbRiskReader(factory).risk_for(GatedChange(workspace_id=WS, commit_sha="c" * 40))
    assert found is not None and found.requirements.risk_class is RiskClass.HIGH


@pytest.mark.parametrize(
    ("actual", "risk_decision", "agreed"), [("2", "2", True), ("1", "2", False)]
)
def test_each_observation_is_one_row_naming_the_gate_both_decisions_and_agreement(
    factory: sessionmaker[Session], actual: str, risk_decision: str, agreed: bool
) -> None:
    _classify(factory)
    reader = DbRiskReader(factory)
    risk = reader.risk_for(GatedChange(workspace_id=WS, commit_sha="c" * 40))
    assert risk is not None

    reader.observe(risk, gate="approval", actual=actual, risk_decision=risk_decision)

    with factory() as s:
        [row] = s.scalars(select(RiskGateObservation)).all()
    assert (row.workspace_id, row.classification_id) == (WS, risk.classification_id)
    assert (row.gate_name, row.actual_decision, row.risk_decision) == (
        "approval",
        actual,
        risk_decision,
    )
    assert row.agreed is agreed


def test_a_failed_observation_leaves_the_gates_own_transaction_usable(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Given the gate's session (the deploy gate's), the reader's failure stays its own (FR-013)."""
    _classify(factory)

    def failing_insert(self: RiskRepository, workspace_id: uuid.UUID, **_: Any) -> None:
        self._session.add(RiskGateObservation(workspace_id=workspace_id))  # NOT NULL columns unset
        self._session.flush()

    monkeypatch.setattr(RiskRepository, "record_gate_observation", failing_insert)
    with factory() as s:
        s.add(Workspace(name="Gate", slug="gate-own-work"))  # the gate's own write
        s.flush()
        decided = gate_decision(
            DbRiskReader(s),
            GatedChange(workspace_id=WS, commit_sha="c" * 40),
            "deploy",
            "auto",
            lambda own, req: tighten_deploy(own, req.deploy_mode),
        )
        s.commit()

    assert decided == "auto"  # shadow: the gate's own decision
    with factory() as s:
        assert s.scalar(select(Workspace.id).where(Workspace.slug == "gate-own-work")) is not None
        assert s.scalars(select(RiskGateObservation)).all() == []
