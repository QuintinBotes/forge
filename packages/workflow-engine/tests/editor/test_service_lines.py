"""Editor service on DSL v2 lines and marketplace-managed definitions (spec K §4.3, §4.4)."""

from __future__ import annotations

import pytest
import yaml

from forge_db.models.workflow_editor import RevisionValidationStatus, WorkflowDefinitionSource
from forge_workflow.editor.errors import (
    DefinitionManagedByMarketplaceError,
    DefinitionNameConflictError,
    PublishBlockedError,
)
from forge_workflow.editor.graph import WorkflowGraph, line_graph_from_yaml, yaml_to_graph
from forge_workflow.editor.schemas import (
    CreateDefinition,
    ForkRequest,
    ImportRequest,
    SaveDraftRequest,
)
from forge_workflow.editor.service import RecordingAuditSink, WorkflowEditorService

from .conftest import IMPLEMENTATION_LOOP, REVIEW_LINE
from .test_service import ACTOR, WS, FakeRepo


@pytest.fixture
def repo() -> FakeRepo:
    return FakeRepo()


@pytest.fixture
def service(repo: FakeRepo) -> WorkflowEditorService:
    return WorkflowEditorService(repo, audit=RecordingAuditSink())


def _import(service: WorkflowEditorService, name: str, text: str) -> None:
    raw = yaml.safe_load(text)
    raw["workflow"] = name
    service.import_yaml(
        WS, ImportRequest(name=name, title=name, dsl_yaml=yaml.safe_dump(raw)), actor=ACTOR
    )


def _install(repo: FakeRepo, name: str = "implementation_loop") -> None:
    """The §4.4 installer's repository calls, as wave B will make them."""
    graph_json, _ = line_graph_from_yaml(IMPLEMENTATION_LOOP)
    defn = repo.create_definition(
        workspace_id=WS,
        name=name,
        title="Implementation loop",
        description=None,
        source=WorkflowDefinitionSource.MARKETPLACE,
        base_bundled_name=None,
        actor=ACTOR,
    )
    draft = repo.create_draft(
        defn,
        dsl_yaml=IMPLEMENTATION_LOOP,
        graph_json=graph_json,
        dsl_version="2",
        notes="registry/implementation_loop@1.0.0 sha256:x",
        validation_status=RevisionValidationStatus.UNVALIDATED,
        validation_issues=[],
        actor=ACTOR,
    )
    repo.set_validation(
        draft, validation_status=RevisionValidationStatus.VALID, validation_issues=[]
    )
    repo.publish_draft(defn, draft)


def _revision_row(repo: FakeRepo, name: str, revision: int):
    defn = repo.get(WS, name)
    return repo.get_revision(defn.id, revision)


@pytest.mark.parametrize("text", [IMPLEMENTATION_LOOP, REVIEW_LINE], ids=["impl_loop", "review"])
def test_v2_line_imports_clean_and_publishes_with_dsl_version_2(
    service: WorkflowEditorService, repo: FakeRepo, text: str
) -> None:
    _import(service, "my_line", text)
    detail = service.get_definition(WS, "my_line")
    assert detail.origin == "custom"
    assert detail.draft.validation_issues == []
    assert detail.draft.graph.dsl_version == "2"
    published = service.publish(WS, "my_line", actor=ACTOR)
    assert published.status == "published"
    assert _revision_row(repo, "my_line", 1).dsl_version == "2"


def test_v1_revision_stores_dsl_version_1_not_the_workflow_version(
    service: WorkflowEditorService, repo: FakeRepo
) -> None:
    """The column holds the DSL version; a v1 workflow at ``version: '7'`` is still "1"."""
    v1 = "workflow: v1_flow\nversion: '7'\ntransitions:\n  - {from: created, to: closed}\n"
    service.import_yaml(WS, ImportRequest(name="v1_flow", title="V1", dsl_yaml=v1), actor=ACTOR)
    assert _revision_row(repo, "v1_flow", 1).dsl_version == "1"
    service.fork_bundled(WS, "default_feature", actor=ACTOR)
    assert _revision_row(repo, "default_feature", 1).dsl_version == "1"


V1_PLAIN = """
workflow: plain
version: "1"
transitions:
  - {from: created, to: in_progress, when: start}
  - {from: in_progress, to: awaiting_review, when: pr_opened}
  - {from: awaiting_review, to: merged, when: ci_status_green}
"""


@pytest.mark.parametrize("target", ["default_feature", "incident"])
@pytest.mark.parametrize("source", ["implementation_loop", "plain"])
def test_fork_definition_refuses_a_bundled_target_name(
    service: WorkflowEditorService, repo: FakeRepo, source: str, target: str
) -> None:
    """rp1ce repro_reserved: a fork must not land a gate-less graph under a bundled name."""
    _install(repo)
    service.import_yaml(
        WS, ImportRequest(name="plain", title="Plain", dsl_yaml=V1_PLAIN), actor=ACTOR
    )
    with pytest.raises(DefinitionNameConflictError):
        service.fork_definition(WS, source, ForkRequest(name=target, title="T"), actor=ACTOR)
    assert repo.get(WS, target) is None


def _line_graph(name: str) -> WorkflowGraph:
    raw = yaml.safe_load(REVIEW_LINE)
    raw["workflow"] = name
    return yaml_to_graph(yaml.safe_dump(raw))


def _assert_reserved(service: WorkflowEditorService, name: str, issues) -> None:
    assert ("reserved_name", "error") in {(i.code, i.severity) for i in issues}
    with pytest.raises(PublishBlockedError) as exc:
        service.publish(WS, name, actor=ACTOR)
    assert "reserved_name" in {i.code for i in exc.value.errors}


@pytest.mark.parametrize("name", ["default_feature", "incident"])
def test_v2_line_under_a_bundled_name_never_publishes(service: WorkflowEditorService, name) -> None:
    """rp1ce repro_v2df: import, create-with-graph and the v1 -> v2 flip all get reserved_name."""
    _import(service, name, REVIEW_LINE)
    _assert_reserved(service, name, service.get_definition(WS, name).draft.validation_issues)

    created = WorkflowEditorService(FakeRepo(), audit=RecordingAuditSink())
    detail = created.create_definition(
        WS, CreateDefinition(name=name, title="T", graph=_line_graph(name)), actor=ACTOR
    )
    _assert_reserved(created, name, detail.draft.validation_issues)

    flipped = WorkflowEditorService(FakeRepo(), audit=RecordingAuditSink())
    flipped.fork_bundled(WS, name, actor=ACTOR)  # a legit v1 fork, then saved as a line
    saved = flipped.save_draft(WS, name, SaveDraftRequest(graph=_line_graph(name)), actor=ACTOR)
    _assert_reserved(flipped, name, saved.validation_issues)


def test_v2_graph_with_v1_only_fields_is_invalid(service: WorkflowEditorService) -> None:
    """§4.1 rejects modes / retry_policy / escalation_policy on a line; never drop them."""
    _import(service, "my_line", REVIEW_LINE)
    graph = service.get_definition(WS, "my_line").draft.graph.model_copy(deep=True)
    graph.modes = {"default": "multi_agent"}
    graph.retry_policy = graph.retry_policy.model_copy(update={"max_retries": 9})
    graph.escalation_policy = graph.escalation_policy.model_copy(
        update={"confidence_threshold": 0.1}
    )
    saved = service.save_draft(WS, "my_line", SaveDraftRequest(graph=graph), actor=ACTOR)
    assert [(i.code, i.severity, i.message.split(":")[0]) for i in saved.validation_issues] == [
        ("invalid_definition", "error", key)
        for key in ("modes", "retry_policy", "escalation_policy")
    ]
    with pytest.raises(PublishBlockedError):
        service.publish(WS, "my_line", actor=ACTOR)


def test_v2_publish_is_blocked_by_validate_line_codes(service: WorkflowEditorService) -> None:
    _import(service, "my_line", REVIEW_LINE)
    graph = service.get_definition(WS, "my_line").draft.graph.model_copy(deep=True)
    graph.edges = [e for e in graph.edges if e.when != "approved"]  # review loses its exit
    saved = service.save_draft(WS, "my_line", SaveDraftRequest(graph=graph), actor=ACTOR)
    assert saved.validation_status == "invalid"
    assert "unrouted_success" in {i.code for i in saved.validation_issues}
    with pytest.raises(PublishBlockedError) as exc:
        service.publish(WS, "my_line", actor=ACTOR)
    unrouted = next(i for i in exc.value.errors if i.code == "unrouted_success")
    assert unrouted.node_id == "review"
    assert service.get_definition(WS, "my_line").published_revision is None


def test_v2_validate_line_edge_index_maps_to_the_edge_id(service: WorkflowEditorService) -> None:
    _import(service, "my_line", REVIEW_LINE)
    graph = service.get_definition(WS, "my_line").draft.graph.model_copy(deep=True)
    graph.edges[0].when = "not_an_outcome"
    graph.edges[0].id = "client-edge"
    issues = service.save_draft(
        WS, "my_line", SaveDraftRequest(graph=graph), actor=ACTOR
    ).validation_issues
    unknown = next(i for i in issues if i.code == "unknown_outcome")
    assert unknown.edge_id == "client-edge"


def test_v2_shape_error_is_an_invalid_definition_issue(service: WorkflowEditorService) -> None:
    _import(service, "my_line", REVIEW_LINE)
    graph = service.get_definition(WS, "my_line").draft.graph.model_copy(deep=True)
    review = next(n for n in graph.nodes if n.id == "review")
    review.station = {k: v for k, v in review.station.items() if k != "sla_seconds"}
    saved = service.save_draft(WS, "my_line", SaveDraftRequest(graph=graph), actor=ACTOR)
    assert [(i.code, i.message) for i in saved.validation_issues] == [
        ("invalid_definition", "stations.review.human.sla_seconds: Field required")
    ]
    with pytest.raises(PublishBlockedError):
        service.publish(WS, "my_line", actor=ACTOR)


def test_marketplace_definition_shows_as_read_only(
    service: WorkflowEditorService, repo: FakeRepo
) -> None:
    _install(repo)
    summary = next(s for s in service.list_definitions(WS) if s.name == "implementation_loop")
    assert summary.origin == "marketplace"
    detail = service.get_definition(WS, "implementation_loop")
    assert detail.origin == "marketplace"
    assert detail.editable is False
    assert detail.current_published.graph.dsl_version == "2"
    assert service.list_revisions(WS, "implementation_loop")[0].status == "published"
    assert service.export_yaml(WS, "implementation_loop") == IMPLEMENTATION_LOOP


def test_marketplace_definition_refuses_every_editor_mutation(
    service: WorkflowEditorService, repo: FakeRepo
) -> None:
    _install(repo)
    name = "implementation_loop"
    graph = service.get_definition(WS, name).current_published.graph
    mutations = [
        lambda: service.save_draft(WS, name, SaveDraftRequest(graph=graph), actor=ACTOR),
        lambda: service.validate_draft(WS, name),
        lambda: service.publish(WS, name, actor=ACTOR),
        lambda: service.rollback(WS, name, 1, actor=ACTOR),
        lambda: service.archive(WS, name, actor=ACTOR),
    ]
    for mutate in mutations:
        with pytest.raises(DefinitionManagedByMarketplaceError):
            mutate()
    assert repo.committed == 0
    assert len(repo.revisions) == 1
    assert repo.get(WS, name).is_active is True


def test_fork_of_a_marketplace_line_is_a_custom_v2_definition(
    service: WorkflowEditorService, repo: FakeRepo
) -> None:
    _install(repo)
    detail = service.fork_definition(
        WS, "implementation_loop", ForkRequest(name="my_loop", title="My loop"), actor=ACTOR
    )
    assert (detail.origin, detail.editable, detail.base_bundled_name) == ("custom", True, None)
    assert detail.draft.graph.dsl_version == "2"
    assert detail.draft.validation_issues == []
    assert yaml.safe_load(detail.draft.dsl_yaml)["workflow"] == "my_loop"
    assert _revision_row(repo, "my_loop", 1).dsl_version == "2"
    service.publish(WS, "my_loop", actor=ACTOR)

    # validate_line still guards the fork's next publish.
    graph = service.get_definition(WS, "my_loop").current_published.graph.model_copy(deep=True)
    graph.edges = [e for e in graph.edges if e.from_state != "mark_ready"]
    service.save_draft(WS, "my_loop", SaveDraftRequest(graph=graph), actor=ACTOR)
    with pytest.raises(PublishBlockedError) as exc:
        service.publish(WS, "my_loop", actor=ACTOR)
    assert "dead_end_state" in {i.code for i in exc.value.errors}
    # The installed definition is untouched.
    assert len(repo.list_revisions(repo.get(WS, "implementation_loop").id)) == 1


def test_fork_keeps_the_v1_base_so_protected_invariants_still_apply(
    service: WorkflowEditorService,
) -> None:
    service.fork_bundled(WS, "default_feature", actor=ACTOR)
    detail = service.fork_definition(
        WS, "default_feature", ForkRequest(name="my_feature", title="Mine"), actor=ACTOR
    )
    assert (detail.origin, detail.base_bundled_name) == ("custom", "default_feature")
    graph = detail.draft.graph.model_copy(deep=True)
    for edge in graph.edges:
        if (edge.from_state, edge.to_state) == ("awaiting_review", "merged"):
            edge.when = [s for s in edge.when if s != "review_approved_by_human"]
    service.save_draft(WS, "my_feature", SaveDraftRequest(graph=graph), actor=ACTOR)
    with pytest.raises(PublishBlockedError) as exc:
        service.publish(WS, "my_feature", actor=ACTOR)
    assert any(i.invariant_id == "merge_human_gate" for i in exc.value.errors)


def test_a_v2_save_stores_the_line_not_the_clients_node_list(
    service: WorkflowEditorService,
) -> None:
    """rp1ce2 attack T1-T4: duplicate, mis-kinded and phantom nodes never reach the
    stored or published canvas; it is rebuilt from the stations, layout kept."""
    _import(service, "my_line", REVIEW_LINE)
    graph = service.get_definition(WS, "my_line").draft.graph.model_copy(deep=True)
    first = graph.nodes[0]
    first.layout = first.layout.model_copy(update={"x": 123, "y": 45})
    twin = first.model_copy(deep=True)
    twin.kind = "human" if first.kind != "human" else "agent"
    phantom = first.model_copy(update={"id": "phantom", "station": None, "kind": "normal"})
    graph.nodes = [twin, *graph.nodes, phantom]

    saved = service.save_draft(WS, "my_line", SaveDraftRequest(graph=graph), actor=ACTOR)

    expected = yaml_to_graph(service.get_definition(WS, "my_line").draft.dsl_yaml)
    stored = saved.graph
    assert [(n.id, n.kind, n.station) for n in stored.nodes] == [
        (n.id, n.kind, n.station) for n in expected.nodes
    ]
    assert len({n.id for n in stored.nodes}) == len(stored.nodes)
    assert "phantom" not in {n.id for n in stored.nodes}
    assert next(n for n in stored.nodes if n.id == first.id).layout.x == 123


def test_a_v1_graph_cannot_carry_line_fields(service: WorkflowEditorService) -> None:
    """rp1ce2 attack T5: v2 header or stations on a v1 graph are an error, not dropped."""
    service.fork_bundled(WS, "default_feature", actor=ACTOR)
    graph = service.get_definition(WS, "default_feature").draft.graph.model_copy(deep=True)
    graph.initial = graph.nodes[0].id
    graph.nodes[0].station = {"kind": "service", "service": "forge.open_pr"}

    saved = service.save_draft(WS, "default_feature", SaveDraftRequest(graph=graph), actor=ACTOR)

    assert [(i.code, i.severity) for i in saved.validation_issues] == [
        ("invalid_definition", "error")
    ]
    with pytest.raises(PublishBlockedError):
        service.publish(WS, "default_feature", actor=ACTOR)
