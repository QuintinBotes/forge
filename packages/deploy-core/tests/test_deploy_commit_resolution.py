"""However a deploy names its change, the gate reads the same risk (#335 criteria 2 and 3).

A branch name or an abbreviated hash is resolved to the full hash through the
repository host before the gate reads risk. A name the host cannot resolve is
refused when the repository enforces risk rules; in shadow mode the gate decides
as it did and records nothing. A repository without risk rules is never asked
about. The head hash, the merge hash, an abbreviated hash and a branch name of
one change all get the same gate decision.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from conftest import REPO_ID, WS_ID, FakeGitHub, FakePolicyReader, make_deployment, seed_pipeline
from sqlalchemy import select
from sqlalchemy.orm import Session

from scf_contracts import GateRequirements, RiskClass
from scf_db.models import AgentRun, ChangeOutcome, RiskClassification, RiskGateObservation
from scf_db.models.enums import RunStatus
from scf_db.risk_repository import DbRiskReader
from scf_deploy.gate import CommitResolver, DeploymentGateEvaluator
from scf_deploy.repository import DeploymentRepository
from scf_deploy.schemas import GateEvaluation
from scf_deploy.states import DeploymentState

HEAD = "3d" * 20
MERGE = "7e" * 20
GATED = GateRequirements(
    risk_class=RiskClass.HIGH, approvals=2, deploy_mode="gated", red_team=False, self_eval=True
).model_dump(mode="json")


class Host:
    """The repository host: the full hash each name points at, and every name it was asked."""

    def __init__(self, refs: dict[str, str]) -> None:
        self.refs = refs
        self.asked: list[tuple[str, str]] = []

    def __call__(self, repo_id: str, ref: str) -> str | None:
        self.asked.append((repo_id, ref))
        return self.refs.get(ref)


#: What the host knows of ``org/api``: its default branch is the landed change.
REFS = {"main": MERGE, HEAD[:7]: HEAD, MERGE[:12]: MERGE}


def _classify(session: Session, mode: str, *, repo: str = "org/api") -> uuid.UUID:
    """A run of ``repo`` classified by its head, whose pull request landed as ``MERGE``."""
    run = AgentRun(
        workspace_id=WS_ID,
        status=RunStatus.SUCCEEDED,
        inputs={"context": {"repo_full_name": repo}},
    )
    session.add(run)
    session.flush()
    row = RiskClassification(
        workspace_id=WS_ID,
        commit_sha=HEAD,
        agent_run_id=run.id,
        derived_class=RiskClass.HIGH,
        final_class=RiskClass.HIGH,
        enforcement_mode=mode,
        class_requirements={"high": GATED},
    )
    session.add(row)
    session.add(
        ChangeOutcome(
            workspace_id=WS_ID,
            agent_run_id=run.id,
            repo_full_name=repo,
            pr_number=7,
            outcome="landed",
            outcome_at=datetime(2026, 11, 20, tzinfo=UTC),
            merge_commit_sha=MERGE,
        )
    )
    session.commit()
    return row.id


def _evaluate(
    session: Session, dep_id: uuid.UUID, commits: CommitResolver | None, *, risk: bool = True
) -> GateEvaluation:
    gate = DeploymentGateEvaluator(
        DeploymentRepository(session, workspace_id=WS_ID),
        policy=FakePolicyReader(),
        ci=FakeGitHub(),
        risk=DbRiskReader(session) if risk else None,
        commits=commits,
    )
    return gate.evaluate(dep_id)


def _observations(session: Session) -> list[Any]:
    return list(session.scalars(select(RiskGateObservation)))


def _dev_deploy(session: Session, project_id: uuid.UUID, commit: str) -> uuid.UUID:
    env = seed_pipeline(session, project_id=project_id)["env"]["dev"]
    return make_deployment(session, env, commit).id


@pytest.mark.parametrize("mode", ["shadow", "enforcing"])
def test_every_name_of_one_change_gets_the_same_gate_decision(
    session: Session, project_id: uuid.UUID, mode: str
) -> None:
    classification = _classify(session, mode)
    env = seed_pipeline(session, project_id=project_id)["env"]["dev"]
    host = Host(REFS)
    names = (HEAD, MERGE, HEAD[:7], MERGE[:12], "main")

    decisions = []
    for name in names:
        # One active deploy per environment: each is evaluated, then settled.
        dep = make_deployment(session, env, name, state=DeploymentState.SUCCEEDED)
        ev = _evaluate(session, dep.id, host)
        decisions.append((ev.can_proceed, ev.requires_human_approval, ev.blocking_reasons))

    assert decisions == [(True, mode == "enforcing", [])] * len(names)
    # A full hash is taken as given; every other name is asked of the host.
    assert host.asked == [(REPO_ID, HEAD[:7]), (REPO_ID, MERGE[:12]), (REPO_ID, "main")]
    rows = _observations(session)
    assert [r.classification_id for r in rows] == [classification] * len(names)
    assert {(r.actual_decision, r.risk_decision) for r in rows} == {("auto", "gated")}


def test_an_enforcing_repository_refuses_a_name_the_host_cannot_resolve(
    session: Session, project_id: uuid.UUID
) -> None:
    _classify(session, "enforcing")
    dep_id = _dev_deploy(session, project_id, "release-candidate")

    ev = _evaluate(session, dep_id, Host(REFS))

    assert ev.can_proceed is False
    assert ev.blocking_reasons == [
        "commit 'release-candidate' could not be resolved to a full hash through the "
        f"repository host, and {REPO_ID} enforces risk rules: name the full commit hash"
    ]
    assert _observations(session) == []


def test_a_host_that_fails_resolves_nothing(session: Session, project_id: uuid.UUID) -> None:
    _classify(session, "enforcing")
    dep_id = _dev_deploy(session, project_id, "main")

    def down(repo_id: str, ref: str) -> str | None:
        raise RuntimeError("host unreachable")

    assert _evaluate(session, dep_id, down).can_proceed is False
    # An answer that is not a full hash is no answer either.
    assert _evaluate(session, dep_id, lambda repo_id, ref: "7e7e7e7").can_proceed is False


def test_shadow_mode_decides_an_unresolvable_name_as_before_and_records_nothing(
    session: Session, project_id: uuid.UUID
) -> None:
    _classify(session, "shadow")
    dep_id = _dev_deploy(session, project_id, "release-candidate")

    with_risk = _evaluate(session, dep_id, Host(REFS))

    assert with_risk == _evaluate(session, dep_id, None, risk=False)
    assert _observations(session) == []


def test_a_repository_without_risk_rules_is_decided_as_before_and_never_asked_about(
    session: Session, project_id: uuid.UUID
) -> None:
    _classify(session, "enforcing", repo="org/other")  # another repository enforces
    dep_id = _dev_deploy(session, project_id, "release-candidate")
    host = Host(REFS)

    ev = _evaluate(session, dep_id, host)

    assert ev == _evaluate(session, dep_id, None, risk=False)
    assert host.asked == []
    assert _observations(session) == []
