"""Installing, updating and uninstalling ``workflow_template`` lines (spec K §4.4).

The §4.5 ``implementation_loop`` is signed with the test keypair, served through the
FakeGateway and installed through the API; the editor and run routes read the same
database, so the test sees what an admin would.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

import pytest
from conftest import (
    LINE,
    LINE_NAME,
    LINE_SLUG,
    OTHER_WS_ID,
    WS_ID,
    FakeGateway,
    Keypair,
    count_rows,
    make_line_version,
    make_skill_version,
)
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from forge_api.observability.audit import AuditCategory
from forge_api.services import marketplace_service
from forge_api.services.marketplace_service import MarketplaceService
from forge_contracts import UserRole
from forge_db.models import Project, Task
from forge_db.models.marketplace import (
    MarketplaceAuditLog,
    MarketplaceInstallation,
    MarketplaceRegistry,
)
from forge_db.models.workflow_editor import (
    RevisionValidationStatus,
    WorkflowDefinition,
    WorkflowDefinitionRevision,
    WorkflowDefinitionSource,
)
from forge_workflow.editor.repository import DbWorkflowDefinitionRepository

V2_LINE = LINE.replace("max_usd: 60", "max_usd: 70")


def _install(client: TestClient, registry, *, version: str | None = None, **extra):
    body = {
        "registry_id": str(registry.id),
        "kind": "workflow_template",
        "slug": LINE_SLUG,
        "version": version,
        **extra,
    }
    return client.post("/marketplace/install", json=body)


def _definition(sf: sessionmaker[Session], name: str = LINE_NAME) -> WorkflowDefinition | None:
    with sf() as s:
        return s.scalars(select(WorkflowDefinition).where(WorkflowDefinition.name == name)).first()


def _revisions(sf: sessionmaker[Session], definition_id: uuid.UUID):
    with sf() as s:
        return list(
            s.scalars(
                select(WorkflowDefinitionRevision)
                .where(WorkflowDefinitionRevision.workflow_definition_id == definition_id)
                .order_by(WorkflowDefinitionRevision.revision)
            )
        )


def _audit(sf: sessionmaker[Session], **where) -> list[MarketplaceAuditLog]:
    with sf() as s:
        stmt = select(MarketplaceAuditLog).order_by(MarketplaceAuditLog.created_at)
        for column, value in where.items():
            stmt = stmt.where(getattr(MarketplaceAuditLog, column) == value)
        return list(s.scalars(stmt))


def _f39_actions(service: MarketplaceService) -> list[str]:
    return [e.action for e in service._audit.query(category=AuditCategory.SYSTEM)]


def _start(client: TestClient, sf: sessionmaker[Session]):
    with sf() as s:
        project = Project(workspace_id=WS_ID, name="Core", key=f"C{uuid.uuid4().hex[:4]}")
        s.add(project)
        s.flush()
        task = Task(
            workspace_id=WS_ID, project_id=project.id, key=f"T-{uuid.uuid4().hex[:6]}", title="t"
        )
        s.add(task)
        s.commit()
        task_id = task.id
    return client.post("/workflow/runs", json={"task_id": str(task_id), "definition": LINE_NAME})


@pytest.fixture
def installed(admin_client: TestClient, registry, gateway: FakeGateway, keypair: Keypair):
    gateway.add(*make_line_version(keypair))
    resp = _install(admin_client, registry)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_install_publishes_a_marketplace_definition(
    installed: dict, session_factory, service: MarketplaceService
) -> None:
    definition = _definition(session_factory)
    assert definition is not None
    assert (definition.source, definition.is_active) == ("marketplace", True)
    assert installed["target_kind"] == "workflow_definition"
    assert installed["target_object_id"] == str(definition.id)

    [revision] = _revisions(session_factory, definition.id)
    assert definition.current_published_revision_id == revision.id
    assert definition.draft_revision_id is None
    assert (revision.status, revision.validation_status) == ("published", "valid")
    assert revision.dsl_version == "2"
    assert revision.dsl_yaml == LINE  # verbatim
    assert {n["id"] for n in revision.graph_json["nodes"]} >= {"prepare", "ready_for_review"}
    with session_factory() as s:
        row = s.get(MarketplaceInstallation, uuid.UUID(installed["installation_id"]))
        assert row is not None
        assert (row.kind, row.target_object_id) == ("workflow_template", definition.id)
    assert revision.notes == f"acme-community/{LINE_SLUG}@1.0.0 {row.content_hash}"

    [ok] = _audit(session_factory, operation="install", result_status="ok")
    assert (ok.listing_slug, ok.version) == (LINE_SLUG, "1.0.0")
    actions = _f39_actions(service)
    assert {"marketplace.install", "workflow_definition.published"} <= set(actions)


@pytest.mark.usefixtures("installed")
def test_editor_refuses_edits_and_allows_a_fork(admin_client: TestClient) -> None:
    base = f"/workflow/editor/definitions/{LINE_NAME}"
    graph = admin_client.get(base).json()["current_published"]["graph"]
    put = admin_client.put(f"{base}/draft", json={"graph": graph})
    assert (put.status_code, put.json()["detail"]) == (409, "definition_managed_by_marketplace")

    fork = admin_client.post(f"{base}/fork", json={"name": "my_loop", "title": "Mine"})
    assert fork.status_code == 201, fork.text
    assert fork.json()["origin"] == "custom"


@pytest.mark.usefixtures("installed")
def test_starting_the_line_needs_the_durable_engine(
    admin_client: TestClient, session_factory
) -> None:
    resp = _start(admin_client, session_factory)
    assert (resp.status_code, resp.json()["detail"]) == (503, "durable_engine_required")


def test_reinstalling_the_same_version_is_a_noop(
    installed: dict, admin_client: TestClient, registry, session_factory
) -> None:
    again = _install(admin_client, registry, version="1.0.0")
    assert again.status_code == 200, again.text
    assert again.json()["installation_id"] == installed["installation_id"]
    definition = _definition(session_factory)
    assert definition is not None
    assert len(_revisions(session_factory, definition.id)) == 1
    assert _audit(session_factory, operation="install", result_status="noop")


def test_newer_version_updates_and_naming_the_older_one_rolls_back(
    installed: dict,
    admin_client: TestClient,
    registry,
    gateway: FakeGateway,
    keypair,
    session_factory,
) -> None:
    gateway.add(*make_line_version(keypair, version="1.1.0", dsl_yaml=V2_LINE))
    newer = _install(admin_client, registry)  # latest: routed to update
    assert newer.status_code == 200, newer.text
    assert newer.json()["version"] == "1.1.0"
    definition = _definition(session_factory)
    assert definition is not None
    first, second = _revisions(session_factory, definition.id)
    assert definition.current_published_revision_id == second.id
    assert (second.dsl_yaml, second.status) == (V2_LINE, "published")
    assert _audit(session_factory, operation="update", result_status="ok")

    back = admin_client.post(
        f"/marketplace/installations/{installed['installation_id']}/update?version=1.0.0"
    )
    assert back.status_code == 200, back.text
    definition = _definition(session_factory)
    assert definition is not None
    revisions = _revisions(session_factory, definition.id)
    assert [r.revision for r in revisions] == [1, 2, 3]
    assert definition.current_published_revision_id == revisions[2].id
    assert revisions[2].dsl_yaml == LINE
    assert "@1.0.0 " in (revisions[2].notes or "")
    # Earlier revisions are never mutated.
    assert (revisions[0].dsl_yaml, revisions[0].published_at) == (LINE, first.published_at)
    assert (revisions[1].dsl_yaml, revisions[1].published_at) == (V2_LINE, second.published_at)


def test_a_pinned_installation_refuses_update(
    installed: dict,
    admin_client: TestClient,
    registry,
    gateway: FakeGateway,
    keypair,
    session_factory,
) -> None:
    installation_id = installed["installation_id"]
    pin = admin_client.patch(f"/marketplace/installations/{installation_id}?pinned=true")
    assert pin.status_code == 200, pin.text
    gateway.add(*make_line_version(keypair, version="1.1.0", dsl_yaml=V2_LINE))

    for resp in (
        admin_client.post(f"/marketplace/installations/{installation_id}/update?version=1.1.0"),
        _install(admin_client, registry, version="1.1.0"),
    ):
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"]["error_code"] == "installation_pinned"
    definition = _definition(session_factory)
    assert definition is not None
    assert len(_revisions(session_factory, definition.id)) == 1
    assert len(_audit(session_factory, error_code="installation_pinned")) == 2
    assert _audit(session_factory, operation="pin", result_status="ok")


def test_uninstall_archives_and_reinstall_reactivates(
    installed: dict,
    admin_client: TestClient,
    registry,
    session_factory,
    service: MarketplaceService,
) -> None:
    installation_id = installed["installation_id"]
    gone = admin_client.delete(f"/marketplace/installations/{installation_id}")
    assert gone.status_code == 204, gone.text

    definition = _definition(session_factory)
    assert definition is not None
    assert definition.is_active is False
    assert len(_revisions(session_factory, definition.id)) == 1
    with session_factory() as s:
        row = s.get(MarketplaceInstallation, uuid.UUID(installation_id))
        assert row is not None
        assert (row.status, row.target_object_id) == ("uninstalled", definition.id)
    assert _audit(session_factory, operation="uninstall", result_status="ok")
    assert {"marketplace.uninstall", "workflow_definition.archived"} <= set(_f39_actions(service))
    started = _start(admin_client, session_factory)
    assert (started.status_code, started.json()["detail"]) == (404, "unknown_definition")

    again = _install(admin_client, registry)
    assert again.status_code == 200, again.text
    assert again.json()["installation_id"] == installation_id
    reactivated = _definition(session_factory)
    assert reactivated is not None
    assert (reactivated.id, reactivated.is_active) == (definition.id, True)
    assert len(_revisions(session_factory, definition.id)) == 2
    started = _start(admin_client, session_factory)
    assert (started.status_code, started.json()["detail"]) == (503, "durable_engine_required")


def _assert_refused_without_a_definition(sf, resp, code: str, status: int = 422) -> None:
    assert resp.status_code == status, resp.text
    assert resp.json()["detail"]["error_code"] == code
    assert _audit(sf, result_status="denied", error_code=code)


def test_name_conflict_with_a_custom_definition(
    admin_client: TestClient, registry, gateway: FakeGateway, keypair, session_factory
) -> None:
    with session_factory() as s:
        DbWorkflowDefinitionRepository(s).create_definition(
            workspace_id=WS_ID,
            name=LINE_NAME,
            title="Mine",
            description=None,
            source=WorkflowDefinitionSource.CUSTOM,
            base_bundled_name=None,
            actor=None,
        )
        s.commit()
    gateway.add(*make_line_version(keypair))

    resp = _install(admin_client, registry)
    _assert_refused_without_a_definition(session_factory, resp, "name_conflict", 409)
    assert count_rows(session_factory, MarketplaceInstallation) == 0
    definition = _definition(session_factory)
    assert definition is not None
    assert definition.source == "custom"


def test_a_lost_race_is_a_409_concurrent_install(
    admin_client: TestClient,
    registry,
    gateway: FakeGateway,
    keypair,
    session_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Another install committed the name after this one checked it."""
    with session_factory() as s:
        DbWorkflowDefinitionRepository(s).create_definition(
            workspace_id=WS_ID,
            name=LINE_NAME,
            title="Winner",
            description=None,
            source=WorkflowDefinitionSource.MARKETPLACE,
            base_bundled_name=None,
            actor=None,
        )
        s.commit()
    monkeypatch.setattr(DbWorkflowDefinitionRepository, "get", lambda *_a, **_k: None)
    gateway.add(*make_line_version(keypair))

    resp = _install(admin_client, registry)
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["error_code"] == "concurrent_install"
    assert _audit(session_factory, result_status="denied", error_code="concurrent_install")
    assert count_rows(session_factory, MarketplaceInstallation) == 0
    assert count_rows(session_factory, WorkflowDefinition) == 1


def test_override_name_is_refused(
    admin_client: TestClient, registry, gateway: FakeGateway, keypair, session_factory
) -> None:
    gateway.add(*make_line_version(keypair))
    resp = _install(admin_client, registry, override_name="my_loop")
    _assert_refused_without_a_definition(session_factory, resp, "override_not_supported")
    assert count_rows(session_factory, WorkflowDefinition) == 0


#: The API and the CLI share the SDK default detector, which catches both shapes.
_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJlMTIz"
_SLACK = "xoxb-1234567890-abcdefghij"


@pytest.mark.parametrize(
    ("package", "code"),
    [
        ({"sign": False}, "signature_required"),
        ({"yanked": True}, "version_yanked"),
        (
            {"dsl_yaml": LINE.replace("Cite files", f"Use {_JWT}. Cite files")},
            "secret_like_content",
        ),
        (
            {"dsl_yaml": LINE.replace("Cite files", f"Use {_SLACK}. Cite files")},
            "secret_like_content",
        ),
        (
            {"name": "default_feature", "dsl_yaml": LINE.replace(LINE_NAME, "default_feature", 1)},
            "reserved_name",
        ),
        (
            {"dsl_yaml": LINE.replace("forge.confirm_red", "forge.no_such_service")},
            "line_invalid",
        ),
    ],
    ids=["unverified", "yanked", "secret_jwt", "secret_slack", "reserved_name", "invalid_line"],
)
def test_refused_packages_leave_only_a_denied_row(
    admin_client: TestClient,
    registry,
    gateway: FakeGateway,
    keypair,
    session_factory,
    package: dict,
    code: str,
) -> None:
    gateway.add(*make_line_version(keypair, **package))
    resp = _install(admin_client, registry, version="1.0.0")
    _assert_refused_without_a_definition(session_factory, resp, code)
    assert count_rows(session_factory, WorkflowDefinition) == 0
    assert count_rows(session_factory, MarketplaceInstallation) == 0
    for row in _audit(session_factory, error_code=code):
        assert _JWT not in (row.detail or "") and _SLACK not in (row.detail or "")


@pytest.mark.parametrize("role", [UserRole.MEMBER, UserRole.VIEWER])
def test_non_admins_are_403(
    installed: dict,
    client_factory: Callable[..., TestClient],
    registry,
    role: UserRole,
    session_factory,
) -> None:
    client = client_factory(role)
    installation_id = installed["installation_id"]
    assert _install(client, registry).status_code == 403
    assert client.post(f"/marketplace/installations/{installation_id}/update").status_code == 403
    assert client.delete(f"/marketplace/installations/{installation_id}").status_code == 403
    definition = _definition(session_factory)
    assert definition is not None
    assert definition.is_active is True


def test_another_workspace_cannot_see_or_uninstall_it(
    installed: dict, client_factory: Callable[..., TestClient], session_factory
) -> None:
    other = client_factory(UserRole.ADMIN, workspace_id=OTHER_WS_ID)
    installation_id = installed["installation_id"]
    assert other.get("/marketplace/installations").json() == []
    assert other.delete(f"/marketplace/installations/{installation_id}").status_code == 404
    assert other.post(f"/marketplace/installations/{installation_id}/update").status_code == 404
    assert other.get(f"/workflow/editor/definitions/{LINE_NAME}").status_code == 404
    definition = _definition(session_factory)
    assert definition is not None
    assert definition.is_active is True


def test_a_skill_and_a_line_may_share_a_slug(
    installed: dict, admin_client: TestClient, registry, gateway: FakeGateway, keypair
) -> None:
    """Installation lookups key on the kind too (spec K §4.9)."""
    gateway.add(*make_skill_version(keypair, slug=LINE_SLUG, name="impl-skill"))
    skill = admin_client.post(
        "/marketplace/install",
        json={"registry_id": str(registry.id), "kind": "skill_profile", "slug": LINE_SLUG},
    )
    assert skill.status_code == 200, skill.text
    assert skill.json()["installation_id"] != installed["installation_id"]
    kinds = {i["kind"] for i in admin_client.get("/marketplace/installations").json()}
    assert kinds == {"skill_profile", "workflow_template"}


def test_registry_key_and_trust_changes_are_audited(
    admin_client: TestClient, registry, session_factory
) -> None:
    resp = admin_client.patch(
        f"/marketplace/registries/{registry.id}",
        json={"trust_level": "trusted", "public_key": "bmV3LWtleQ=="},
    )
    assert resp.status_code == 200, resp.text
    [row] = _audit(session_factory, operation="registry.update")
    assert row.detail == "public_key changed; trust_level community -> trusted"
    assert row.registry_slug == registry.slug


# --- review corrections (spec K P1c wave B) ----------------------------------- #


def _row(sf: sessionmaker[Session], installation_id: str) -> MarketplaceInstallation:
    with sf() as s:
        row = s.get(MarketplaceInstallation, uuid.UUID(installation_id))
        assert row is not None
        return row


def _update(client: TestClient, installation_id: str, version: str | None = None):
    query = f"?version={version}" if version else ""
    return client.post(f"/marketplace/installations/{installation_id}/update{query}")


def test_ordinary_words_in_an_objective_install(
    admin_client: TestClient, registry, gateway: FakeGateway, keypair
) -> None:
    """The API uses the SDK detector, not redaction, which flags "basic" and "Bearer"."""
    dsl = LINE.replace("Cite files and tests.", "Cite files and tests. Write basic tests.")
    dsl = dsl.replace("No production code.", "No production code. Bearer of bad news.")
    gateway.add(*make_line_version(keypair, dsl_yaml=dsl))
    resp = _install(admin_client, registry)
    assert resp.status_code == 200, resp.text


def test_a_title_the_definition_row_cannot_hold_is_artifact_invalid(
    admin_client: TestClient, registry, gateway: FakeGateway, keypair, session_factory
) -> None:
    gateway.add(*make_line_version(keypair, title="T" * 161))
    resp = _install(admin_client, registry)
    _assert_refused_without_a_definition(session_factory, resp, "artifact_invalid")
    assert count_rows(session_factory, WorkflowDefinition) == 0


def test_a_name_the_bundled_loader_knows_is_reserved(
    admin_client: TestClient,
    registry,
    gateway: FakeGateway,
    keypair,
    session_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Beyond the SDK's static set: whatever the bundled loader resolves."""
    monkeypatch.setattr(
        marketplace_service,
        "default_bundled_loader",
        lambda name: object() if name == LINE_NAME else None,
    )
    gateway.add(*make_line_version(keypair))
    resp = _install(admin_client, registry)
    _assert_refused_without_a_definition(session_factory, resp, "reserved_name")
    assert count_rows(session_factory, WorkflowDefinition) == 0
    assert count_rows(session_factory, MarketplaceInstallation) == 0


def test_a_newer_version_with_the_same_content_moves_the_version_only(
    installed: dict,
    admin_client: TestClient,
    registry,
    gateway: FakeGateway,
    keypair,
    session_factory,
    service: MarketplaceService,
) -> None:
    gateway.add(*make_line_version(keypair, version="1.0.1", title="Implementation loop 1.0.0"))
    service.sync_registry(workspace_id=WS_ID, actor="u", registry_id=registry.id)
    assert service.refresh_update_flags(workspace_id=WS_ID) == 1

    resp = _update(admin_client, installed["installation_id"])
    assert resp.status_code == 200, resp.text
    assert (resp.json()["version"], resp.json()["status"]) == ("1.0.1", "installed")
    row = _row(session_factory, installed["installation_id"])
    assert (row.installed_version, row.available_version, row.status) == (
        "1.0.1",
        None,
        "installed",
    )
    definition = _definition(session_factory)
    assert definition is not None
    assert len(_revisions(session_factory, definition.id)) == 1
    assert _audit(session_factory, operation="update", result_status="noop")
    assert service.refresh_update_flags(workspace_id=WS_ID) == 0


def test_a_same_content_version_does_not_bypass_a_pin(
    installed: dict, admin_client: TestClient, registry, gateway: FakeGateway, keypair
) -> None:
    pin = admin_client.patch(
        f"/marketplace/installations/{installed['installation_id']}?pinned=true"
    )
    assert pin.status_code == 200, pin.text
    gateway.add(*make_line_version(keypair, version="1.0.1", title="Implementation loop 1.0.0"))
    resp = _install(admin_client, registry, version="1.0.1")
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["error_code"] == "installation_pinned"


def test_uninstall_clears_the_pin_and_updating_it_is_refused(
    installed: dict,
    admin_client: TestClient,
    gateway: FakeGateway,
    keypair,
    session_factory,
) -> None:
    installation_id = installed["installation_id"]
    pin = admin_client.patch(f"/marketplace/installations/{installation_id}?pinned=true")
    assert pin.status_code == 200, pin.text
    assert admin_client.delete(f"/marketplace/installations/{installation_id}").status_code == 204
    assert _row(session_factory, installation_id).pinned is False

    gateway.add(*make_line_version(keypair, version="1.1.0", dsl_yaml=V2_LINE))
    resp = _update(admin_client, installation_id, "1.1.0")
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["error_code"] == "installation_uninstalled"
    definition = _definition(session_factory)
    assert definition is not None
    assert definition.is_active is False
    assert len(_revisions(session_factory, definition.id)) == 1
    assert _row(session_factory, installation_id).status == "uninstalled"
    assert _audit(session_factory, result_status="denied", error_code="installation_uninstalled")


def test_unpin_is_audited_as_unpin(installed: dict, admin_client: TestClient, session_factory):
    base = f"/marketplace/installations/{installed['installation_id']}"
    assert admin_client.patch(f"{base}?pinned=true").status_code == 200
    assert admin_client.patch(f"{base}?pinned=false").status_code == 200
    assert [r.operation for r in _audit(session_factory, result_status="ok")][-2:] == [
        "pin",
        "unpin",
    ]


def test_registry_add_audits_trust_and_whether_a_key_is_set(
    admin_client: TestClient, session_factory
) -> None:
    for slug, key in (("with-key", "c2VjcmV0LWtleQ=="), ("no-key", None)):
        resp = admin_client.post(
            "/marketplace/registries",
            json={
                "name": slug,
                "slug": slug,
                "type": "http_index",
                "url": f"https://{slug}.test/index.json",
                "trust_level": "trusted",
                "public_key": key,
            },
        )
        assert resp.status_code == 201, resp.text
    details = [r.detail for r in _audit(session_factory, operation="registry.add")]
    assert details == [
        "trust_level=trusted; public_key=set",
        "trust_level=trusted; public_key=none",
    ]
    assert all("c2VjcmV0" not in (d or "") for d in details)


def test_the_same_line_in_two_workspaces_is_two_installations(
    installed: dict,
    client_factory: Callable[..., TestClient],
    session_factory,
    keypair: Keypair,
) -> None:
    with session_factory() as s:
        other_registry = MarketplaceRegistry(
            workspace_id=OTHER_WS_ID,
            slug="acme-community",
            name="Other",
            type="http_index",
            url="https://registry.acme.test/index.json",
            public_key=keypair.public_b64,
            trust_level="community",
            enabled=True,
        )
        s.add(other_registry)
        s.commit()
        s.refresh(other_registry)
    other = client_factory(UserRole.ADMIN, workspace_id=OTHER_WS_ID)
    resp = _install(other, other_registry)
    assert resp.status_code == 200, resp.text
    assert resp.json()["installation_id"] != installed["installation_id"]
    assert resp.json()["target_object_id"] != installed["target_object_id"]
    with session_factory() as s:
        definitions = s.scalars(select(WorkflowDefinition)).all()
        installations = s.scalars(select(MarketplaceInstallation)).all()
    assert sorted(str(d.workspace_id) for d in definitions) == sorted(
        map(str, (WS_ID, OTHER_WS_ID))
    )
    assert sorted(str(i.workspace_id) for i in installations) == sorted(
        map(str, (WS_ID, OTHER_WS_ID))
    )


def test_preview_refuses_override_name(
    admin_client: TestClient, registry, gateway: FakeGateway, keypair
) -> None:
    gateway.add(*make_line_version(keypair))
    resp = admin_client.post(
        "/marketplace/preview",
        json={
            "registry_id": str(registry.id),
            "kind": "workflow_template",
            "slug": LINE_SLUG,
            "override_name": "my_loop",
        },
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["error_code"] == "override_not_supported"


@pytest.fixture
def skill_shares_the_slug(
    installed: dict, registry, gateway: FakeGateway, keypair, service: MarketplaceService
) -> None:
    """A newer skill under the line's slug, synced into the cached catalog."""
    gateway.add(*make_skill_version(keypair, slug=LINE_SLUG, name="impl-skill", version="9.0.0"))
    service.sync_registry(workspace_id=WS_ID, actor="u", registry_id=registry.id)


@pytest.mark.usefixtures("skill_shares_the_slug")
def test_update_flags_key_on_the_kind(
    installed: dict, service: MarketplaceService, session_factory
) -> None:
    assert service.refresh_update_flags(workspace_id=WS_ID) == 0
    row = _row(session_factory, installed["installation_id"])
    assert (row.status, row.available_version) == ("installed", None)


@pytest.mark.usefixtures("skill_shares_the_slug")
def test_listing_detail_takes_a_kind(admin_client: TestClient) -> None:
    base = f"/marketplace/listings/acme-community/{LINE_SLUG}"
    for kind, version in (("workflow_template", "1.0.0"), ("skill_profile", "9.0.0")):
        resp = admin_client.get(base, params={"kind": kind})
        assert resp.status_code == 200, resp.text
        assert (resp.json()["kind"], [v["version"] for v in resp.json()["versions"]]) == (
            kind,
            [version],
        )
    # Without a kind: the first by kind, whatever the insertion order.
    assert admin_client.get(base).json()["kind"] == "skill_profile"
    assert admin_client.get(base, params={"kind": "mcp_connector"}).status_code == 404


def test_updating_to_a_renamed_package_is_name_mismatch(
    installed: dict,
    admin_client: TestClient,
    gateway: FakeGateway,
    keypair,
    session_factory,
) -> None:
    renamed = LINE.replace(LINE_NAME, "impl_loop_v2", 1)
    gateway.add(*make_line_version(keypair, version="2.0.0", name="impl_loop_v2", dsl_yaml=renamed))
    resp = _update(admin_client, installed["installation_id"], "2.0.0")
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["error_code"] == "name_mismatch"
    assert _audit(session_factory, result_status="denied", error_code="name_mismatch")
    assert count_rows(session_factory, WorkflowDefinition) == 1
    assert _row(session_factory, installed["installation_id"]).installed_version == "1.0.0"


def test_an_open_draft_refuses_the_update(
    installed: dict,
    admin_client: TestClient,
    gateway: FakeGateway,
    keypair,
    session_factory,
) -> None:
    with session_factory() as s:
        definition = s.scalars(select(WorkflowDefinition)).one()
        DbWorkflowDefinitionRepository(s).create_draft(
            definition,
            dsl_yaml=LINE,
            graph_json={},
            dsl_version="2",
            notes="hand edit",
            validation_status=RevisionValidationStatus.VALID,
            validation_issues=[],
            actor=None,
        )
        s.commit()
        published = definition.current_published_revision_id
    gateway.add(*make_line_version(keypair, version="1.1.0", dsl_yaml=V2_LINE))
    resp = _update(admin_client, installed["installation_id"], "1.1.0")
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["error_code"] == "open_draft"
    assert _audit(session_factory, result_status="denied", error_code="open_draft")
    after = _definition(session_factory)
    assert after is not None
    assert after.current_published_revision_id == published
