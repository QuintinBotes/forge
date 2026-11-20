"""Risk at the worker's red-team check (#194 criteria 3, 6 and 9).

Every case of ``test_temporal_run_checks`` that reaches the review, and so the
red-team scan, is collected here again and re-run with a shadow reader that
classifies every change as requiring red-team: each must pass unchanged, with
one observation per scan decision. When the change's repository enforces it, a
scan the workspace turned off no longer lets the change through.
"""

from __future__ import annotations

import inspect
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import test_temporal_run_checks as _matrix
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from scf_contracts import GateRequirements, RiskClass
from scf_contracts.testing import RecordingRiskReader
from scf_db.models import RiskClassification, RiskGateObservation
from scf_worker import temporal_main
from scf_workflow.red_team_gate import RED_TEAM_REQUIRED


def _reaches_the_scan(test: Any) -> bool:
    return bool({"reviewer", "adversary"} & set(inspect.signature(test).parameters))


# Criterion 3: the existing gate matrix (and its fixtures), collected again below.
_FIXTURES = {"factory", "repo", "reviewer", "adversary"}
globals().update(
    {
        name: value
        for name, value in vars(_matrix).items()
        if name in _FIXTURES or (name.startswith("test_") and _reaches_the_scan(value))
    }
)


@pytest.fixture(autouse=True)
def _shadow(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    if request.function.__module__ != _matrix.__name__:
        yield
        return
    reader = RecordingRiskReader("shadow")
    monkeypatch.setattr(temporal_main, "DbRiskReader", lambda _factory: reader)
    yield
    reader.assert_observed_each_lookup("red_team", {"pass", "block"})


async def test_a_change_its_repository_enforces_red_team_on_waits_for_a_scan(
    factory: sessionmaker[Session], repo: tuple[Path, Path], adversary: Any
) -> None:
    """The workspace turned the scan off, which passes today; the change's class,
    enforced by its repository, requires one, so the run pauses for a person."""
    _, mirror = repo
    head = _matrix._git(mirror, "rev-parse", _matrix.BRANCH)
    run_id = _matrix._seed(factory, head)
    _matrix._configure_review(factory, run_id, adversary_model="off")
    required = GateRequirements(
        risk_class=RiskClass.CRITICAL,
        approvals=2,
        deploy_mode="gated",
        red_team=True,
        self_eval=True,
    )
    with factory() as s:
        s.add(
            RiskClassification(
                workspace_id=_matrix.WS,
                commit_sha=head,
                workflow_run_id=run_id,
                derived_class=RiskClass.CRITICAL,
                final_class=RiskClass.CRITICAL,
                enforcement_mode="enforcing",
                class_requirements={"critical": required.model_dump(mode="json")},
            )
        )
        s.commit()

    result = await _matrix._checks(factory, run_id)

    assert result.pause_reason == RED_TEAM_REQUIRED
    assert "red_team" not in result.results
    [scan] = _matrix._scans(factory, run_id)
    assert scan.verdict == "not_run"  # the record stays truthful
    with factory() as s:
        [seen] = s.scalars(select(RiskGateObservation)).all()
    assert (seen.gate_name, seen.actual_decision, seen.risk_decision) == (
        "red_team",
        "pass",
        "block",
    )
