"""Editor API on DSL v2 lines and marketplace-managed definitions (spec K §4.3, §4.4)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from forge_contracts import parse_line
from forge_db.models.workflow_editor import (
    RevisionValidationStatus,
    WorkflowDefinition,
    WorkflowDefinitionRevision,
    WorkflowDefinitionSource,
)
from forge_workflow.editor.graph import line_graph_from_yaml
from forge_workflow.editor.repository import DbWorkflowDefinitionRepository

from .conftest import USER_ID, WS_A

BASE = "/workflow/editor"
LINE = (
    Path(__file__).resolve().parents[4]
    / "packages/contracts/tests/fixtures/implementation_loop.yaml"
).read_text()
MANAGED = "definition_managed_by_marketplace"


def _install(session: Session) -> None:
    """The §4.4 installer's repository calls, flush-only on the caller's session."""
    repo = DbWorkflowDefinitionRepository(session)
    graph_json, _ = line_graph_from_yaml(LINE)
    defn = repo.create_definition(
        workspace_id=WS_A,
        name="implementation_loop",
        title="Implementation loop",
        description=None,
        source=WorkflowDefinitionSource.MARKETPLACE,
        base_bundled_name=None,
        actor=USER_ID,
    )
    draft = repo.create_draft(
        defn,
        dsl_yaml=LINE,
        graph_json=graph_json,
        dsl_version="2",
        notes="registry/implementation_loop@1.0.0 sha256:x",
        validation_status=RevisionValidationStatus.UNVALIDATED,
        validation_issues=[],
        actor=USER_ID,
    )
    repo.set_validation(
        draft, validation_status=RevisionValidationStatus.VALID, validation_issues=[]
    )
    repo.publish_draft(defn, draft)
    repo.set_active(defn, is_active=True)


@pytest.fixture
def installed(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session:
        _install(session)
        session.commit()


def test_installer_repository_calls_never_commit(session_factory: sessionmaker[Session]) -> None:
    """Item 6: the whole install sequence rolls back with the caller's session."""
    with session_factory() as session:
        _install(session)
        assert session.scalars(select(WorkflowDefinition)).one().source == "marketplace"
        session.rollback()
    with session_factory() as session:
        assert session.scalars(select(WorkflowDefinition)).all() == []
        assert session.scalars(select(WorkflowDefinitionRevision)).all() == []


@pytest.mark.usefixtures("installed")
def test_marketplace_definition_lists_and_reads(admin_client: TestClient) -> None:
    listed = {d["name"]: d for d in admin_client.get(f"{BASE}/definitions").json()}
    assert listed["implementation_loop"]["origin"] == "marketplace"
    detail = admin_client.get(f"{BASE}/definitions/implementation_loop")
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert (body["origin"], body["editable"]) == ("marketplace", False)
    kinds = {n["id"]: n["kind"] for n in body["current_published"]["graph"]["nodes"]}
    assert (kinds["prepare"], kinds["assess"], kinds["ready_for_review"]) == (
        "service",
        "agent",
        "terminal",
    )
    export = admin_client.get(f"{BASE}/definitions/implementation_loop/export")
    assert export.text == LINE


@pytest.mark.usefixtures("installed")
def test_marketplace_definition_refuses_editor_mutations(admin_client: TestClient) -> None:
    name = "implementation_loop"
    graph = admin_client.get(f"{BASE}/definitions/{name}").json()["current_published"]["graph"]
    calls = [
        admin_client.put(f"{BASE}/definitions/{name}/draft", json={"graph": graph}),
        admin_client.post(f"{BASE}/definitions/{name}/draft/validate"),
        admin_client.post(f"{BASE}/definitions/{name}/publish"),
        admin_client.post(f"{BASE}/definitions/{name}/rollback", json={"to_revision": 1}),
        admin_client.post(f"{BASE}/definitions/{name}/archive"),
    ]
    for resp in calls:
        assert (resp.status_code, resp.json()["detail"]) == (409, MANAGED), resp.request.url
    revisions = admin_client.get(f"{BASE}/definitions/{name}/revisions").json()
    assert [r["status"] for r in revisions] == ["published"]


@pytest.mark.usefixtures("installed")
def test_fork_marketplace_line_then_publish_runs_validate_line(
    admin_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    resp = admin_client.post(
        f"{BASE}/definitions/implementation_loop/fork", json={"name": "my_loop", "title": "Mine"}
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert (body["origin"], body["editable"]) == ("custom", True)
    assert body["draft"]["graph"]["dsl_version"] == "2"
    assert admin_client.post(f"{BASE}/definitions/my_loop/publish").status_code == 200

    graph = admin_client.get(f"{BASE}/definitions/my_loop").json()["current_published"]["graph"]
    graph["edges"] = [e for e in graph["edges"] if e["when"] != "already_done"]
    assert admin_client.put(f"{BASE}/definitions/my_loop/draft", json={"graph": graph}).is_success
    blocked = admin_client.post(f"{BASE}/definitions/my_loop/publish")
    assert blocked.status_code == 409
    codes = {e["code"] for e in blocked.json()["detail"]["errors"]}
    assert "unrouted_success" in codes

    with session_factory() as session:
        versions = session.scalars(
            select(WorkflowDefinitionRevision.dsl_version)
            .join(WorkflowDefinition)
            .where(WorkflowDefinition.name == "my_loop")
        ).all()
    assert versions == ["2", "2"]


def test_import_v2_line_round_trips_through_the_api(admin_client: TestClient) -> None:
    raw = yaml.safe_load(LINE)
    raw["workflow"] = "imported_line"
    resp = admin_client.post(
        f"{BASE}/import",
        json={"name": "imported_line", "title": "Imported", "dsl_yaml": yaml.safe_dump(raw)},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["draft"]["validation_issues"] == []
    assert admin_client.post(f"{BASE}/definitions/imported_line/publish").status_code == 200
    exported = admin_client.get(f"{BASE}/definitions/imported_line/export").text

    assert parse_line(yaml.safe_load(exported)) == parse_line(raw)


@pytest.mark.usefixtures("installed")
@pytest.mark.parametrize("target", ["default_feature", "incident"])
def test_fork_into_a_bundled_name_is_409(admin_client: TestClient, target: str) -> None:
    resp = admin_client.post(
        f"{BASE}/definitions/implementation_loop/fork", json={"name": target, "title": "T"}
    )
    assert resp.status_code == 409, resp.text
    # Still the read-only bundled view, not a DB row.
    assert admin_client.get(f"{BASE}/definitions/{target}").json()["origin"] == "bundled"


def test_import_of_an_unquoted_dsl_version_2_is_refused(admin_client: TestClient) -> None:
    raw = yaml.safe_load(LINE)
    raw["dsl_version"] = 2
    raw["workflow"] = "intline"
    resp = admin_client.post(
        f"{BASE}/import", json={"name": "intline", "title": "I", "dsl_yaml": yaml.safe_dump(raw)}
    )
    assert resp.status_code == 400, resp.text
    assert "unsupported dsl_version" in resp.json()["detail"]
    assert admin_client.get(f"{BASE}/definitions/intline").status_code == 404


@pytest.mark.usefixtures("installed")
def test_v2_diff_reports_the_changed_station(admin_client: TestClient) -> None:
    """rp1ce repro_diff: a station-only edit used to diff as no change at all."""
    admin_client.post(
        f"{BASE}/definitions/implementation_loop/fork", json={"name": "mine", "title": "M"}
    )
    assert admin_client.post(f"{BASE}/definitions/mine/publish").status_code == 200
    graph = admin_client.get(f"{BASE}/definitions/mine").json()["current_published"]["graph"]
    push = next(n for n in graph["nodes"] if n["id"] == "push")
    push["station"]["service"] = "forge.mark_pr_ready"
    push["station"].pop("retry")
    saved = admin_client.put(f"{BASE}/definitions/mine/draft", json={"graph": graph})
    assert saved.json()["validation_issues"] == []
    assert admin_client.post(f"{BASE}/definitions/mine/publish").status_code == 200

    diff = admin_client.get(f"{BASE}/definitions/mine/diff", params={"from": 1, "to": 2}).json()
    assert diff["stations_changed"] == ["push"]
    assert diff["header_changed"] is False
    assert diff["transition_diffs"] == []


def test_v1_diff_response_has_no_line_keys(admin_client: TestClient) -> None:
    admin_client.post(f"{BASE}/definitions/default_feature/fork")
    diff = admin_client.get(
        f"{BASE}/definitions/default_feature/diff", params={"from": 1, "to": 1}
    ).json()
    assert not {"stations_changed", "header_changed"} & diff.keys()


def test_v1_graph_response_has_no_v2_keys(admin_client: TestClient) -> None:
    graph = admin_client.get(f"{BASE}/definitions/default_feature").json()["current_published"][
        "graph"
    ]
    assert not {"dsl_version", "initial", "terminals", "budget", "concurrency"} & graph.keys()
    assert all("station" not in n for n in graph["nodes"])
