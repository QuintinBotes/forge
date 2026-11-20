"""Risk at the multi-repo merge gate (#194 criterion 3; enforcing mode tightens only).

Every case of ``test_merge`` is collected here again and re-run with a shadow
reader that classifies every change as strictly as possible: each must pass
unchanged, with one observation per gate evaluation. When enforcing, a class that
requires approvals makes every changed repository need its review, required for
merge or not.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from multi_repo import test_merge as _matrix
from scf_contracts import GatedChange, MultiRepoMergeGateResult, RepoMergeStatus
from scf_contracts.testing import RecordingRiskReader
from scf_workflow.multi_repo import MultiRepoMergeGate, MultiRepoMerger

# Criterion 3: the existing gate matrix, collected again under the shadow reader below.
globals().update({name: test for name, test in vars(_matrix).items() if name.startswith("test_")})

CHANGE = GatedChange(workspace_id=uuid.uuid4(), workflow_run_id=uuid.uuid4())


@pytest.fixture(autouse=True)
def _shadow(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    if request.function.__module__ != _matrix.__name__:
        yield
        return
    reader = RecordingRiskReader("shadow")
    evaluate = MultiRepoMergeGate.evaluate

    def with_risk(
        statuses: list[RepoMergeStatus], merge_order: list[str] | None = None, **_: Any
    ) -> MultiRepoMergeGateResult:
        return evaluate(statuses, merge_order, risk=reader, change=CHANGE)

    monkeypatch.setattr(MultiRepoMergeGate, "evaluate", staticmethod(with_risk))
    yield
    assert reader.looked_up, "every case evaluates the gate"
    reader.assert_observed_each_lookup("merge", {"pass", "block"})


def _status(repo_id: str, *, required: bool = True, reviewed: bool = True) -> RepoMergeStatus:
    return RepoMergeStatus(
        repo_id=repo_id,
        required_for_merge=required,
        review_approved=reviewed,
        ci_green=True,
        spec_validated=True,
    )


#: ``web`` is changed but not required for merge, and nobody reviewed it.
UNREVIEWED = [_status("api"), _status("web", required=False, reviewed=False)]


def test_shadow_merges_an_unreviewed_optional_repo_and_records_that_risk_would_block() -> None:
    reader = RecordingRiskReader("shadow")

    gate = MultiRepoMergeGate.evaluate(UNREVIEWED, risk=reader, change=CHANGE)

    assert gate.can_merge is True and gate.blocking_reasons == []
    assert reader.observed == [{"gate": "merge", "actual": "pass", "risk_decision": "block"}]


def test_enforcing_requires_a_review_of_every_changed_repo() -> None:
    gate = MultiRepoMergeGate.evaluate(
        UNREVIEWED, risk=RecordingRiskReader("enforcing"), change=CHANGE
    )

    assert gate.can_merge is False
    assert gate.blocking_reasons == ["web: review required by the change's risk class"]


def test_enforcing_never_opens_a_gate_that_was_already_blocked() -> None:
    blocked = [_status("api", reviewed=False)]
    gate = MultiRepoMergeGate.evaluate(
        blocked, risk=RecordingRiskReader("enforcing"), change=CHANGE
    )
    assert gate.can_merge is False
    assert gate.blocking_reasons == ["api: review not approved"]


def test_a_class_without_approvals_changes_nothing() -> None:
    reader = RecordingRiskReader("enforcing", approvals=0)
    gate = MultiRepoMergeGate.evaluate(UNREVIEWED, risk=reader, change=CHANGE)
    assert gate.can_merge is True


def test_the_merger_rechecks_the_gate_under_the_changes_risk() -> None:
    github = _matrix.FakeMultiGitHub()
    merger = MultiRepoMerger(github, risk=RecordingRiskReader("enforcing"))

    outcome = merger.merge_in_order(
        _matrix._group(("api", 1), ("web", 2)), UNREVIEWED, change=CHANGE
    )

    assert outcome.status == "blocked" and github.merge_calls == []
