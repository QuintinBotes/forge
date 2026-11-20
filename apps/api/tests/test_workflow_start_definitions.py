"""Starting a run by definition name (spec K §4.2 "Start", P1).

``POST /workflow/runs`` resolves an explicit ``definition`` through the workspace's
published definitions over the bundled ones. An unknown name is 404 and never
falls back to ``default_feature``. A line (DSL v2) is refused with 503 on the V1
engine; the Temporal engine validates it again (409 ``line_invalid``), refuses an
explicit station model (409 ``model_not_allowed``) and a start past
``max_active_runs`` (409 ``line_concurrency_exceeded``), each before any run row is
written or Temporal is contacted, and otherwise starts ``forge.StationWorkflow``. A
station run then takes only ``resume`` and ``cancel``. The V1 engine refuses states
outside ``WorkflowState`` (409); the Temporal engine runs no other v1 definition
than the bundled ``default_feature`` (503).
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable, Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from scf_api.deps import Principal
from scf_api.main import create_app
from scf_api.services import workflow_engine
from scf_contracts import UserRole
from scf_contracts.lines import validate_line
from scf_db.base import Base
from scf_db.models import Project, RepositoryConnection, Task, Workspace
from scf_db.models.runs import WorkflowRun as DbWorkflowRun
from scf_db.models.workflow_editor import (
    RevisionValidationStatus,
    WorkflowDefinitionSource,
)
from scf_db.models.workflow_editor import WorkflowDefinition as DbDefinition
from scf_workflow.dsl import parse_definition
from scf_workflow.editor.errors import UnknownDefinitionError
from scf_workflow.editor.repository import DbWorkflowDefinitionRepository
from scf_workflow.editor.schemas import ImportRequest
from scf_workflow.editor.service import WorkflowEditorService
from scf_workflow.editor.store import (
    ResolvingDefinitionProvider,
    bundled_line_text,
    bundled_revision_id,
)
from scf_workflow.temporal.payloads import StationParams, WorkflowEventPayload

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

#: Parses, but ``validate_line`` refuses it (a dead end; ``done`` unreachable).
INVALID_LINE = LINE.replace("to: done", "to: nowhere")

#: Reads ``task.repo``, which a task with no single repository cannot give.
REPO_LINE = LINE.replace(
    "forge.prepare_worktree}", "forge.prepare_worktree, inputs: {repo: task.repo}}"
)

#: Valid, but names a model, which no workspace allowlist admits yet.
MODEL_LINE = """\
workflow: tiny_line
dsl_version: "2"
initial: implement
terminals: {done: succeeded}
budget: {max_tokens: 1000}
stations:
  implement:
    kind: agent
    role: implementer
    skill_profile: backend-tdd
    context_template: implementation
    objective: Implement it.
    model: some-model
transitions:
  - {from: implement, to: done, when: succeeded}
"""

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


class _LineHandle:
    result_run_id = "fake-temporal-run"

    def __init__(self, client: _LineClient) -> None:
        self._client = client

    async def signal(self, _signal: object, args: list[object]) -> None:
        self._client.sent.append(("signal", args))

    async def execute_update(self, _update: object, event: WorkflowEventPayload) -> str:
        self._client.sent.append(("update", event))
        return "prepare"


class _LineClient:
    """Starts nothing; records the params started and what reaches a run's workflow."""

    def __init__(self) -> None:
        self.started: list[object] = []
        self.sent: list[tuple[str, object]] = []

    async def start_workflow(self, _workflow: object, params: object, **_kw: object) -> _LineHandle:
        self.started.append(params)
        return _LineHandle(self)

    def get_workflow_handle(self, _wf_id: str) -> _LineHandle:
        return _LineHandle(self)


@pytest.fixture
def line_client(monkeypatch: pytest.MonkeyPatch) -> _LineClient:
    """Select the Temporal engine over a client that runs nothing."""
    fake = _LineClient()

    async def _connect(settings: object) -> _LineClient:
        return fake

    monkeypatch.setenv("WORKFLOW_ENGINE_BACKEND", "temporal")
    monkeypatch.setattr(workflow_engine, "get_temporal_client", _connect)
    return fake


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


def _task_on(workflow_db, repo: str = "acme/app", *, active: bool | None = True) -> uuid.UUID:
    """A task whose one target is ``repo``: connected, deactivated (False) or never (None)."""
    task_id = workflow_db.seed_task()
    with workflow_db.factory() as session:
        task = session.get(Task, task_id)
        assert task is not None
        task.repo_targets = [{"repo": repo}]
        if active is not None:
            session.add(
                RepositoryConnection(
                    workspace_id=task.workspace_id, repo_id=repo, name=repo, is_active=active
                )
            )
        session.commit()
    return task_id


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


def test_line_starts_as_a_station_run_on_the_temporal_engine(
    client, workflow_db, line_client: _LineClient
) -> None:
    revision_id = _publish_line(workflow_db)
    task_id = _task_on(workflow_db)
    with workflow_db.factory() as session:
        task_key = session.get(Task, task_id).key  # type: ignore[union-attr]

    resp = _start(client, task_id, definition="tiny_line", trigger={"external_ref": "LIN-9"})

    assert resp.status_code == 201, resp.text
    run = resp.json()
    assert (run["workflow_name"], run["current_state"], run["status"]) == (
        "tiny_line",
        "prepare",
        "running",
    )
    assert run["context"]["dsl_version"] == "2"
    assert _revision_column(workflow_db, run["id"]) == revision_id
    (params,) = line_client.started
    assert isinstance(params, StationParams)
    assert (params.workflow_run_id, params.task_id) == (uuid.UUID(run["id"]), task_id)
    assert (params.definition_revision_id, params.dsl_yaml) == (revision_id, LINE)
    assert params.task == {
        "key": task_key,
        "title": "workflow task",
        "description": "",
        "repo": "acme/app",
    }
    assert params.trigger == {"external_ref": "LIN-9"}


def test_the_bundled_implementation_loop_starts_by_name(
    client, workflow_db, line_client: _LineClient
) -> None:
    """#181: the first line ships bundled, read-only and valid; it starts with nothing installed."""
    text = bundled_line_text("implementation_loop")
    assert text is not None
    assert validate_line(parse_definition(text)) == []  # type: ignore[arg-type]
    task_id = _task_on(workflow_db)

    resp = _start(client, task_id, definition="implementation_loop")

    assert resp.status_code == 201, resp.text
    run = resp.json()
    assert (run["workflow_name"], run["current_state"]) == ("implementation_loop", "prepare")
    (params,) = line_client.started
    assert isinstance(params, StationParams)
    # The revision is the bundled one the editor shows read-only; no definition row exists.
    revision = bundled_revision_id("implementation_loop")
    assert (params.definition_revision_id, params.dsl_yaml) == (revision, text)
    assert _revision_column(workflow_db, run["id"]) == revision
    with workflow_db.factory() as session:
        assert session.scalar(select(func.count()).select_from(DbDefinition)) == 0


@pytest.mark.parametrize("active", [None, False], ids=["never_connected", "deactivated"])
def test_a_line_on_a_repository_the_workspace_has_not_connected_is_refused(
    client, workflow_db, line_client: _LineClient, active: bool | None
) -> None:
    """#181 fix: as every other run path, a line acts only on a connected repository."""
    resp = _start(client, _task_on(workflow_db, active=active), definition="implementation_loop")
    assert resp.status_code == 422
    assert resp.json()["detail"] == "repository 'acme/app' is not connected to this workspace"
    assert (_run_rows(workflow_db), line_client.started) == (0, [])


@pytest.mark.usefixtures("temporal")
@pytest.mark.parametrize(
    ("dsl", "detail"),
    [
        (INVALID_LINE, "line_invalid"),
        (FUTURE_LINE, "line_invalid"),
        (MODEL_LINE, "model_not_allowed"),
        (REPO_LINE, "repo_target_ambiguous"),  # the seeded task has no repository
    ],
    ids=["invalid", "unparseable", "model", "no_single_repo"],
)
def test_temporal_refuses_a_line_it_must_not_run_before_connecting(
    client, workflow_db, dsl: str, detail: str
) -> None:
    _publish_raw(workflow_db, dsl=dsl)
    resp = _start(client, workflow_db.seed_task(), definition="tiny_line")
    assert (resp.status_code, resp.json()["detail"]) == (409, detail)
    assert _run_rows(workflow_db) == 0


def test_a_line_start_past_max_active_runs_is_refused(
    client, workflow_db, line_client: _LineClient
) -> None:
    _publish_raw(workflow_db, dsl=LINE + "concurrency: {max_active_runs: 1}\n")
    assert _start(client, workflow_db.seed_task(), definition="tiny_line").status_code == 201
    refused = _start(client, workflow_db.seed_task(), definition="tiny_line")
    assert (refused.status_code, refused.json()["detail"]) == (409, "line_concurrency_exceeded")
    assert _run_rows(workflow_db) == 1
    assert len(line_client.started) == 1


def test_a_station_run_takes_only_resume_and_cancel(
    client, workflow_db, line_client: _LineClient
) -> None:
    _publish_line(workflow_db)
    run_id = _start(client, workflow_db.seed_task(), definition="tiny_line").json()["id"]
    for event in ("succeeded", "generate_spec_draft", "start_agent_run"):
        resp = client.post(f"/workflow/runs/{run_id}/transition", json={"event": event})
        assert (resp.status_code, resp.json()["detail"]) == (409, "station_run_not_event_driven")
    assert line_client.sent == []

    resumed = client.post(
        f"/workflow/runs/{run_id}/transition", json={"event": "resume", "input": "use v2"}
    )
    assert resumed.status_code == 200, resumed.text
    cancelled = client.post(f"/workflow/runs/{run_id}/transition", json={"event": "cancel"})
    assert cancelled.status_code == 200, cancelled.text

    (kind, event), (signal, args) = line_client.sent
    assert kind == "update" and isinstance(event, WorkflowEventPayload)
    assert (event.type, event.payload) == ("resume", {"input": "use v2"})
    assert event.actor.startswith("user:")
    assert (signal, args) == ("signal", ["cancelled", event.actor])


@pytest.mark.parametrize(
    ("role", "auth_method"),
    [(UserRole.VIEWER, "test"), (UserRole.MEMBER, "api_key")],
    ids=["viewer", "member_key_bound_to_no_person"],
)
def test_only_a_person_who_may_write_resumes_or_cancels_a_station_run(
    client, authenticate_app, workflow_db, line_client: _LineClient, role, auth_method
) -> None:
    _publish_line(workflow_db)
    run_id = _start(client, workflow_db.seed_task(), definition="tiny_line").json()["id"]
    caller = Principal(
        user_id=uuid.uuid4(),  # an unbound key's principal carries the key's own id
        workspace_id=TEST_WORKSPACE_ID,
        role=role,
        auth_method=auth_method,
        scopes=["*"],
    )
    app = workflow_db.install(create_app())
    authenticate_app(app, caller)
    with TestClient(app) as as_viewer:
        for event in ("resume", "cancel"):
            resp = as_viewer.post(f"/workflow/runs/{run_id}/transition", json={"event": event})
            assert resp.status_code == 403, resp.text
    assert line_client.sent == []


def test_a_line_row_whose_workflow_never_started_is_started_again(
    client, workflow_db, line_client: _LineClient
) -> None:
    """The API died between committing the row and starting it: the next start recovers it.

    Temporal refuses a second ``wf-<id>`` when the first one is live (REJECT_DUPLICATE).
    """
    _publish_raw(workflow_db, dsl=LINE + "concurrency: {max_active_runs: 1}\n")
    task_id = workflow_db.seed_task()
    first = _start(client, task_id, definition="tiny_line").json()["id"]
    again = _start(client, task_id, definition="tiny_line")
    assert again.status_code == 201, again.text
    assert again.json()["id"] == first  # its own slot, not a second one
    assert [str(p.workflow_run_id) for p in line_client.started] == [first, first]  # type: ignore[attr-defined]
    assert _run_rows(workflow_db) == 1


def test_racing_line_starts_count_one_at_a_time(pg_engine: Engine) -> None:
    """The definition row lock serialises the count: two starts never both see a free slot."""
    from scf_api.services.workflow_engine import active_line_runs

    Base.metadata.create_all(pg_engine)
    factory = sessionmaker(bind=pg_engine, expire_on_commit=False, class_=Session)
    ws = uuid.uuid4()
    try:
        with factory() as s:
            s.add(Workspace(id=ws, name="WS", slug=f"ws-{ws.hex}"))
            s.flush()
            s.add(Project(workspace_id=ws, name="Core", key="CORE"))
            s.add(DbDefinition(workspace_id=ws, name="tiny_line", title="t", source="marketplace"))
            s.flush()
            project = s.scalar(select(Project.id).where(Project.workspace_id == ws))
            tasks = [
                Task(workspace_id=ws, project_id=project, key=f"T-{i}", title="t") for i in (1, 2)
            ]
            s.add_all(tasks)
            s.commit()
        counts: list[int] = []
        first_counted = threading.Event()

        def start(task_id: uuid.UUID, first: bool) -> None:
            with factory() as s:
                active = active_line_runs(s, workspace_id=ws, name="tiny_line", task_id=task_id)
                if first:
                    first_counted.set()
                    time.sleep(0.5)  # the other start counts now, unless the lock holds it
                if active < 1:
                    s.add(
                        DbWorkflowRun(workspace_id=ws, task_id=task_id, workflow_name="tiny_line")
                    )
                s.commit()
                counts.append(active)

        a = threading.Thread(target=start, args=(tasks[0].id, True))
        a.start()
        assert first_counted.wait(5)
        b = threading.Thread(target=start, args=(tasks[1].id, False))
        b.start()
        a.join(10)
        b.join(10)
        assert counts == [0, 1]
    finally:
        Base.metadata.drop_all(pg_engine)


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


@pytest.mark.usefixtures("temporal")
def test_temporal_start_carries_the_trigger_on_the_row_it_starts_from(
    client, workflow_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The engine commits the row before ``start_workflow`` (spec K §4.11), so
    the trigger must already be on it, not recorded after the start."""
    seen: dict[str, object] = {}

    class _Handle:
        result_run_id = "temporal-run-1"

    class _Client:
        async def start_workflow(self, _fn: object, params, **_kw: object) -> _Handle:
            with workflow_db.factory() as other:
                row = other.get(DbWorkflowRun, params.workflow_run_id)
                assert row is not None
                seen.update(row.context)
            return _Handle()

    async def _connect(settings: object) -> _Client:
        return _Client()

    monkeypatch.setattr(workflow_engine, "get_temporal_client", _connect)
    resp = _start(client, workflow_db.seed_task(), trigger={"external_ref": "LIN-42"})

    assert resp.status_code == 201, resp.text
    assert seen["trigger"] == {"external_ref": "LIN-42"}
    assert resp.json()["context"]["trigger"] == {"external_ref": "LIN-42"}


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
