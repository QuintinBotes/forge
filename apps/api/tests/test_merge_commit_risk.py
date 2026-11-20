"""A merged pull request's merge commit names its change at the deploy gate (#335 criterion 1).

The squash or merge commit that reaches the base branch has another hash than the
agent branch's head, which the classification stores. The signed ``closed``
delivery records the merge commit's full hash against the run's change outcome,
and the risk reader finds the classification by it.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import StaticPool, create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from scf_api.main import create_app
from scf_api.routers.integration import get_feedback_session_factory, get_github_webhook_secret
from scf_contracts import GatedChange, GateRequirements, RiskClass
from scf_db.base import Base
from scf_db.models import AgentRun, ChangeOutcome, RiskClassification, RunStatus, Workspace
from scf_db.risk_repository import DbRiskReader
from scf_integrations import sign_github_payload

WS = uuid.UUID("00000000-0000-0000-0000-0000000335a1")
WS2 = uuid.UUID("00000000-0000-0000-0000-0000000335a2")
SECRET = "whsec_merge_commit_test"
REPO = "org/api"
HEAD = "3d" * 20
MERGE = "7e" * 20
GATED = GateRequirements(
    risk_class=RiskClass.HIGH, approvals=2, deploy_mode="gated", red_team=False, self_eval=True
).model_dump(mode="json")


@pytest.fixture
def factory() -> Iterator[sessionmaker[Session]]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    sf: sessionmaker[Session] = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with sf() as session:
        session.add_all(
            [Workspace(id=WS, name="A", slug="a335"), Workspace(id=WS2, name="B", slug="b335")]
        )
        session.commit()
    yield sf
    engine.dispose()


@pytest.fixture
def client(factory: sessionmaker[Session]) -> Iterator[TestClient]:
    app = create_app()
    app.dependency_overrides[get_github_webhook_secret] = lambda: SECRET
    app.dependency_overrides[get_feedback_session_factory] = lambda: factory
    with TestClient(app) as tc:
        yield tc


def _classified_run(
    factory: sessionmaker[Session], *, number: int, workspace_id: uuid.UUID = WS
) -> uuid.UUID:
    """An agent run whose pull request is ``number``, classified by its head commit."""
    with factory() as session:
        run = AgentRun(
            workspace_id=workspace_id,
            status=RunStatus.SUCCEEDED,
            inputs={"context": {"repo_full_name": REPO}},
            output={"artifacts": {"pull_request": {"status": "opened", "number": number}}},
        )
        session.add(run)
        session.flush()
        row = RiskClassification(
            workspace_id=workspace_id,
            commit_sha=HEAD,
            agent_run_id=run.id,
            derived_class=RiskClass.HIGH,
            final_class=RiskClass.HIGH,
            enforcement_mode="enforcing",
            class_requirements={"high": GATED},
        )
        session.add(row)
        session.commit()
        return row.id


def _deliver_closed(
    client: TestClient, number: int, *, merged: bool, merge_commit_sha: str | None
) -> None:
    pull_request: dict[str, Any] = {
        "number": number,
        "merged": merged,
        "merged_at": "2026-11-20T10:00:00Z" if merged else None,
        "closed_at": "2026-11-20T10:00:00Z",
        "merge_commit_sha": merge_commit_sha,
        "user": {"login": "scf-app[bot]"},
        "head": {"ref": "scf/run-000000000000"},
    }
    body = json.dumps(
        {"repository": {"full_name": REPO}, "action": "closed", "pull_request": pull_request}
    ).encode()
    resp = client.post(
        "/integration/github/webhooks",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Event": "pull_request",
            "X-Hub-Signature-256": sign_github_payload(SECRET, body),
        },
    )
    assert resp.status_code == 200, resp.text


def _risk_for(factory: sessionmaker[Session], workspace_id: uuid.UUID, commit: str) -> Any:
    with factory() as session:
        return DbRiskReader(session).risk_for(
            GatedChange(workspace_id=workspace_id, commit_sha=commit)
        )


def _merge_commits(factory: sessionmaker[Session]) -> list[str | None]:
    with factory() as session:
        return [row.merge_commit_sha for row in session.scalars(select(ChangeOutcome))]


def test_a_merged_pull_request_names_its_change_by_the_merge_commit(
    factory: sessionmaker[Session], client: TestClient
) -> None:
    classification = _classified_run(factory, number=7)
    assert _risk_for(factory, WS, MERGE) is None  # before the merge, only the head names it

    _deliver_closed(client, 7, merged=True, merge_commit_sha=MERGE)

    assert _merge_commits(factory) == [MERGE]
    found = _risk_for(factory, WS, MERGE)
    assert found is not None and found.classification_id == classification
    assert found.requirements.deploy_mode == "gated"
    assert _risk_for(factory, WS2, MERGE) is None  # another workspace never sees it


def test_an_unmerged_close_or_a_malformed_hash_records_no_merge_commit(
    factory: sessionmaker[Session], client: TestClient
) -> None:
    _classified_run(factory, number=8)
    _classified_run(factory, number=9)

    # GitHub reports a test-merge hash on a pull request closed without merging.
    _deliver_closed(client, 8, merged=False, merge_commit_sha=MERGE)
    _deliver_closed(client, 9, merged=True, merge_commit_sha="7e7e7e7")

    assert _merge_commits(factory) == [None, None]
    assert _risk_for(factory, WS, MERGE) is None
