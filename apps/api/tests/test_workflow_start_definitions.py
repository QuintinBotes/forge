"""Starting a run by definition name (spec K §4.2 "Start", P1).

``POST /workflow/runs`` resolves an explicit ``definition`` through the workspace's
published definitions over the bundled ones. An unknown name is 404 and never
falls back to ``default_feature``; a line (DSL v2) is refused with 503 until
``forge.StationWorkflow`` exists, on both engines, before any run row is written
or Temporal is contacted. The V1 engine refuses states outside ``WorkflowState``
(409); the Temporal engine runs only the bundled ``default_feature`` (503).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from forge_api.main import create_app
from forge_api.services import workflow_engine
from forge_db.models.runs import WorkflowRun as DbWorkflowRun
from forge_db.models.workflow_editor import (
    RevisionValidationStatus,
    WorkflowDefinitionSource,
)
from forge_workflow.editor.errors import UnknownDefinitionError
from forge_workflow.editor.repository import DbWorkflowDefinitionRepository
from forge_workflow.editor.schemas import ImportRequest
from forge_workflow.editor.service import WorkflowEditorService
from forge_workflow.editor.store import ResolvingDefinitionProvider

#: The authenticated principal's workspace (conftest's ``TEST_WORKSPACE_ID``).
TEST_WORKSPACE_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
OTHER_WS = uuid.UUID("00000000-0000-0000-0000-0000000000c3")

HOTFIX = """\
workflow: hotfix
version: "1"
transitions:
  - from: created
    to: spec_drafting
    action: generate_spec_draft
  - from: spec_drafting
    to: closed
    action: gather_clarifications
"""

LINE = """\
workflow: tiny_line
dsl_version: "2"
initial: prepare
terminals: {done: succeeded}
stations:
  prepare: {kind: service, service: forge.prepare_worktree}
transitions:
  - {from: prepare, to: done, when: succeeded}
"""

#: A line with a key this release's ``LineDefinition`` does not know: it cannot parse.
FUTURE_LINE = LINE + 'schedule: {cron: "0 * * * *"}\n'

#: A v1 document that cannot parse (no transitions).
BROKEN_V1 = 'workflow: broken\nversion: "1"\ntransitions: []\n'

INCIDENT_STATES = """\
workflow: triage_flow
version: "1"
transitions:
  - from: created
    to: incident_created
    action: generate_spec_draft
  - from: incident_created
    to: closed
    action: gather_clarifications
"""

WORKSPACE_STATES = """\
workflow: triage_flow
version: "1"
transitions:
  - from: created
    to: triaging
    action: generate_spec_draft
  - from: triaging
    to: closed
    action: gather_clarifications
"""


@pytest.fixture
def client(authenticate_app: Callable[..., FastAPI], workflow_db) -> Iterator[TestClient]:
    app = workflow_db.install(create_app())
    authenticate_app(app)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def temporal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Select the Temporal engine; connecting to Temporal fails the test."""

    def _no_connection(settings: object) -> object:
        pytest.fail("the Temporal client was requested")

    monkeypatch.setenv("WORKFLOW_ENGINE_BACKEND", "temporal")
    monkeypatch.setattr(workflow_engine, "get_temporal_client", _no_connection)


def _publish_v1(
    workflow_db,
    workspace_id: uuid.UUID = TEST_WORKSPACE_ID,
    *,
    name: str = "hotfix",
    dsl: str = HOTFIX,
) -> uuid.UUID:
    with workflow_db.factory() as session:
        service = WorkflowEditorService(DbWorkflowDefinitionRepository(session))
        service.import_yaml(
            workspace_id, ImportRequest(name=name, title=name, dsl_yaml=dsl), actor=None
        )
        return service.publish(workspace_id, name, actor=None).id


def _publish_raw(
    workflow_db,
    workspace_id: uuid.UUID = TEST_WORKSPACE_ID,
    *,
    name: str = "tiny_line",
    dsl: str = LINE,
    dsl_version: str = "2",
) -> uuid.UUID:
    """Publish a revision directly, past the editor's checks (it cannot publish v2 yet).

    A new revision of ``name`` when it exists, else a new definition.
    """
    with workflow_db.factory() as session:
        repo = DbWorkflowDefinitionRepository(session)
        defn = repo.get(workspace_id, name) or repo.create_definition(
            workspace_id=workspace_id,
            name=name,
            title=name,
            description=None,
            source=WorkflowDefinitionSource.MARKETPLACE,
            base_bundled_name=None,
            actor=None,
        )
        draft = repo.create_draft(
            defn,
            dsl_yaml=dsl,
            graph_json={},
            dsl_version=dsl_version,
            notes=None,
            validation_status=RevisionValidationStatus.VALID,
            validation_issues=[],
            actor=None,
        )
        repo.publish_draft(defn, draft)
        repo.commit()
        return draft.id


def _publish_line(workflow_db, workspace_id: uuid.UUID = TEST_WORKSPACE_ID) -> uuid.UUID:
    return _publish_raw(workflow_db, workspace_id)


def _start(client: TestClient, task_id: uuid.UUID, **body: object):
    return client.post("/workflow/runs", json={"task_id": str(task_id), **body})


def _run_rows(workflow_db) -> int:
    with workflow_db.factory() as session:
        return session.scalar(select(func.count()).select_from(DbWorkflowRun)) or 0


def _revision_column(workflow_db, run_id: str) -> uuid.UUID | None:
    with workflow_db.factory() as session:
        row = session.get(DbWorkflowRun, uuid.UUID(run_id))
        assert row is not None
        return row.definition_revision_id


def test_omitted_definition_starts_default_feature_unchanged(client, workflow_db) -> None:
    _publish_v1(workflow_db)
    resp = _start(client, workflow_db.seed_task())
    assert resp.status_code == 201, resp.text
    run = resp.json()
    assert run["workflow_name"] == "default_feature"
    assert run["current_state"] == "created"
    assert run["context"] == {"retry_count": 0}
    assert _revision_column(workflow_db, run["id"]) is None


def test_explicit_default_feature_starts(client, workflow_db) -> None:
    resp = _start(client, workflow_db.seed_task(), definition="default_feature")
    assert resp.status_code == 201, resp.text
    run = resp.json()
    assert run["workflow_name"] == "default_feature"
    assert run["current_state"] == "created"
    assert "trigger" not in run["context"]


def test_published_v1_definition_starts_and_pins_its_revision(client, workflow_db) -> None:
    revision_id = _publish_v1(workflow_db)
    resp = _start(client, workflow_db.seed_task(), definition="hotfix")
    assert resp.status_code == 201, resp.text
    run = resp.json()
    assert run["workflow_name"] == "hotfix"
    assert run["context"]["definition_revision_id"] == str(revision_id)
    assert _revision_column(workflow_db, run["id"]) == revision_id

    # Transitions follow the pinned revision, not default_feature.
    steps = (("generate_spec_draft", "spec_drafting"), ("gather_clarifications", "closed"))
    for event, state in steps:
        moved = client.post(f"/workflow/runs/{run['id']}/transition", json={"event": event})
        assert moved.status_code == 200, moved.text
        assert moved.json()["current_state"] == state


def test_unknown_definition_is_404_without_a_run(client, workflow_db) -> None:
    resp = _start(client, workflow_db.seed_task(), definition="no_such_flow")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "unknown_definition"
    assert _run_rows(workflow_db) == 0


def test_another_workspaces_definition_is_unknown(client, workflow_db) -> None:
    workflow_db.seed_task(OTHER_WS)
    _publish_v1(workflow_db, OTHER_WS)
    _publish_line(workflow_db, OTHER_WS)
    task_id = workflow_db.seed_task()
    for name in ("hotfix", "tiny_line"):
        resp = _start(client, task_id, definition=name)
        assert resp.status_code == 404, resp.text
        assert resp.json()["detail"] == "unknown_definition"
    assert _run_rows(workflow_db) == 0


def test_line_is_refused_on_the_v1_engine(client, workflow_db) -> None:
    _publish_line(workflow_db)
    resp = _start(client, workflow_db.seed_task(), definition="tiny_line")
    assert resp.status_code == 503
    assert resp.json()["detail"] == "durable_engine_required"
    assert _run_rows(workflow_db) == 0


@pytest.mark.usefixtures("temporal")
def test_line_is_refused_on_the_temporal_engine_before_connecting(client, workflow_db) -> None:
    _publish_line(workflow_db)
    resp = _start(client, workflow_db.seed_task(), definition="tiny_line")
    assert resp.status_code == 503
    assert resp.json()["detail"] == "durable_engine_required"
    assert _run_rows(workflow_db) == 0


@pytest.mark.usefixtures("temporal")
def test_temporal_refuses_unknown_and_non_default_definitions_before_connecting(
    client, workflow_db
) -> None:
    """FeatureWorkflow runs only default_feature: anything else would never progress."""
    _publish_v1(workflow_db)
    task_id = workflow_db.seed_task()
    unknown = _start(client, task_id, definition="no_such_flow")
    assert (unknown.status_code, unknown.json()["detail"]) == (404, "unknown_definition")
    for name in ("hotfix", "incident"):
        refused = _start(client, task_id, definition=name)
        assert (refused.status_code, refused.json()["detail"]) == (
            503,
            "definition_unsupported_by_engine",
        )
    assert _run_rows(workflow_db) == 0


@pytest.mark.usefixtures("temporal")
def test_temporal_refuses_a_published_default_feature_fork_before_connecting(
    client, workflow_db
) -> None:
    """FeatureWorkflow runs the bundled file, so a fork would run something else."""
    with workflow_db.factory() as session:
        service = WorkflowEditorService(DbWorkflowDefinitionRepository(session))
        service.fork_bundled(TEST_WORKSPACE_ID, "default_feature", actor=None)
        service.publish(TEST_WORKSPACE_ID, "default_feature", actor=None)
    resp = _start(client, workflow_db.seed_task(), definition="default_feature")
    assert (resp.status_code, resp.json()["detail"]) == (503, "definition_unsupported_by_engine")
    assert _run_rows(workflow_db) == 0


@pytest.mark.usefixtures("temporal")
def test_temporal_admits_the_bundled_default_feature(
    client, workflow_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Admitted, the start goes on to Temporal (stubbed here to stop at the client)."""

    class ReachedTemporal(Exception):
        pass

    def _reached(settings: object) -> object:
        raise ReachedTemporal

    monkeypatch.setattr(workflow_engine, "get_temporal_client", _reached)
    with pytest.raises(ReachedTemporal):
        _start(client, workflow_db.seed_task(), definition="default_feature")


@pytest.mark.parametrize(
    ("name", "dsl"),
    [("incident", None), ("triage_flow", INCIDENT_STATES), ("triage_flow", WORKSPACE_STATES)],
)
def test_v1_refuses_states_outside_workflow_state(
    client, workflow_db, name: str, dsl: str | None
) -> None:
    if dsl is not None:
        _publish_v1(workflow_db, name=name, dsl=dsl)
    resp = _start(client, workflow_db.seed_task(), definition=name)
    assert (resp.status_code, resp.json()["detail"]) == (409, "definition_unsupported_by_engine")
    assert _run_rows(workflow_db) == 0


@pytest.mark.parametrize(
    ("dsl", "dsl_version", "expected"),
    [
        (FUTURE_LINE, "2", (503, "durable_engine_required")),
        (BROKEN_V1, "1", (409, "definition_invalid")),
    ],
)
def test_unparseable_published_revision_is_refused_not_500(
    client, workflow_db, dsl: str, dsl_version: str, expected: tuple[int, str]
) -> None:
    _publish_raw(workflow_db, name="broken", dsl=dsl, dsl_version=dsl_version)
    resp = _start(client, workflow_db.seed_task(), definition="broken")
    assert (resp.status_code, resp.json()["detail"]) == expected
    assert _run_rows(workflow_db) == 0


@pytest.mark.parametrize("line", [LINE, FUTURE_LINE], ids=["line", "unparseable_line"])
def test_line_published_between_admission_and_start_is_503_without_a_run(
    client, workflow_db, monkeypatch: pytest.MonkeyPatch, line: str
) -> None:
    """The V1 start resolves the name again; what it pinned must be what was admitted."""
    _publish_v1(workflow_db)
    original = ResolvingDefinitionProvider.resolve
    calls = 0

    def resolve(self, name, *, workspace_id=None):
        nonlocal calls
        calls += 1
        if calls == 2:  # admission was the first resolve; engine.start is the second
            _publish_raw(workflow_db, name="hotfix", dsl=line.replace("tiny_line", "hotfix"))
        return original(self, name, workspace_id=workspace_id)

    monkeypatch.setattr(ResolvingDefinitionProvider, "resolve", resolve)
    resp = _start(client, workflow_db.seed_task(), definition="hotfix")
    assert (resp.status_code, resp.json()["detail"]) == (503, "durable_engine_required")
    assert calls == 2
    assert _run_rows(workflow_db) == 0


def test_trigger_is_kept_in_the_run_context(client, workflow_db) -> None:
    resp = _start(client, workflow_db.seed_task(), trigger={"external_ref": "LIN-42"})
    assert resp.status_code == 201, resp.text
    run_id = resp.json()["id"]
    assert resp.json()["context"]["trigger"] == {"external_ref": "LIN-42"}
    fetched = client.get(f"/workflow/runs/{run_id}").json()
    assert fetched["context"]["trigger"] == {"external_ref": "LIN-42"}


@pytest.mark.parametrize(
    "trigger",
    [
        {"external_ref": "x" * 513},
        {"external_ref": 7},
        {"source": "tracker"},
        {"external_ref": "a\x00b"},
        {"external_ref": "line\nbreak"},
    ],
)
def test_bad_trigger_is_422_without_a_run(client, workflow_db, trigger: dict) -> None:
    resp = _start(client, workflow_db.seed_task(), trigger=trigger)
    assert resp.status_code == 422, resp.text
    assert _run_rows(workflow_db) == 0


@pytest.mark.usefixtures("temporal")
def test_a_control_character_in_the_trigger_is_refused_before_temporal(client, workflow_db) -> None:
    """rp1cs2: Postgres JSON refuses NUL only after start_workflow; refuse it first."""
    resp = _start(client, workflow_db.seed_task(), trigger={"external_ref": "a\x00b"})
    assert resp.status_code == 422, resp.text
    assert _run_rows(workflow_db) == 0


@pytest.mark.parametrize("name", ["hot\x00fix", "Hotfix", "a" * 100, "", "x"])
def test_a_name_no_definition_can_have_is_unknown_without_a_query(
    client, workflow_db, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    def resolve(self, *_a, **_k):
        pytest.fail("an impossible name reached the provider")

    monkeypatch.setattr(ResolvingDefinitionProvider, "resolve", resolve)
    resp = _start(client, workflow_db.seed_task(), definition=name)
    assert (resp.status_code, resp.json()["detail"]) == (404, "unknown_definition")
    assert _run_rows(workflow_db) == 0


def test_definition_archived_between_admission_and_start_is_404_without_a_run(
    client, workflow_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    _publish_v1(workflow_db)
    original = ResolvingDefinitionProvider.resolve
    calls = 0

    def resolve(self, name, *, workspace_id=None):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise UnknownDefinitionError(name)
        return original(self, name, workspace_id=workspace_id)

    monkeypatch.setattr(ResolvingDefinitionProvider, "resolve", resolve)
    resp = _start(client, workflow_db.seed_task(), definition="hotfix")
    assert (resp.status_code, resp.json()["detail"]) == (404, "unknown_definition")
    assert _run_rows(workflow_db) == 0
