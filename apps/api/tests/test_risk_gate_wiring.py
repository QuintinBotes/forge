"""The API builds its approval and Self-Eval gates with the production risk reader (#194)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.orm import Session, sessionmaker

import scf_api.db as api_db
from scf_api.services.approval_service import get_approval_service, get_gate_registry
from scf_api.services.self_eval_gate import build_self_eval_gate
from scf_api.settings import get_settings
from scf_approval.models import GateType
from scf_contracts import GatedChange, GateRequirements, RiskClass
from scf_db.base import Base
from scf_db.models import RiskClassification, Workspace
from scf_eval.sweval import SelfEvalRequiredError

WS = uuid.UUID("00000000-0000-0000-0000-0000000001a4")
RUN = uuid.UUID("00000000-0000-0000-0000-0000000001b4")


def _clear() -> None:
    get_settings.cache_clear()
    get_gate_registry.cache_clear()
    get_approval_service.cache_clear()


@pytest.fixture
def factory(monkeypatch: pytest.MonkeyPatch) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    requirements = GateRequirements(
        risk_class=RiskClass.HIGH, approvals=2, deploy_mode="gated", red_team=False, self_eval=True
    )
    with session_factory() as s:
        s.add(Workspace(id=WS, name="Acme", slug="acme"))
        s.flush()
        s.add(
            RiskClassification(
                workspace_id=WS,
                commit_sha="c" * 40,
                workflow_run_id=RUN,
                derived_class=RiskClass.HIGH,
                final_class=RiskClass.HIGH,
                enforcement_mode="enforcing",
                class_requirements={"high": requirements.model_dump(mode="json")},
            )
        )
        s.commit()
    monkeypatch.setattr(api_db, "get_session_factory", lambda: session_factory)
    _clear()
    yield session_factory
    _clear()
    engine.dispose()


def _open_pr_gate() -> int:
    request = asyncio.run(
        get_approval_service().create(
            workspace_id=WS,
            gate_type=GateType.PR,
            subject_type="workflow_run",
            subject_id=RUN,
            workflow_run_id=RUN,
            requested_actor=f"agent:{RUN}",
        )
    )
    return request.required_approvals


def test_with_the_database_backend_a_pr_gate_needs_its_classes_approvals(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FORGE_APPROVAL_BACKEND", "db")
    _clear()
    assert _open_pr_gate() == 2


def test_the_hermetic_memory_backend_reads_no_risk(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FORGE_APPROVAL_BACKEND", "memory")
    _clear()
    assert _open_pr_gate() == 1


async def test_the_self_eval_gate_requires_the_self_eval_an_enforced_class_demands(
    factory: sessionmaker[Session],
) -> None:
    """Cold start (no baseline) passes today; the run's class requires a Self-Eval."""
    gate = build_self_eval_gate(session_factory=factory)
    with pytest.raises(SelfEvalRequiredError):
        await gate.check_config(WS, {}, change=GatedChange(workspace_id=WS, workflow_run_id=RUN))
    assert await gate.check_config(WS, {}) is None  # no change named: decided as before
