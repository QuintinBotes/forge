"""Line installs on Postgres, where column limits and row locks are real (spec K §4.4).

SQLite stores an over-long title silently and ignores ``FOR UPDATE``; these run under
``FORGE_TEST_DATABASE_URL`` and skip without it.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from typing import Any

import pytest
from conftest import LINE, LINE_SLUG, WS_ID, FakeGateway, Keypair, make_line_version
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from forge_api.services.marketplace_service import InstallBlockedError, MarketplaceService
from forge_db.base import Base
from forge_db.models import Workspace
from forge_db.models.marketplace import (
    MarketplaceAuditLog,
    MarketplaceInstallation,
    MarketplaceRegistry,
)
from forge_db.models.workflow_editor import WorkflowDefinition, WorkflowDefinitionRevision
from forge_marketplace.installer import INSTALLERS, ValidatedArtifact
from forge_marketplace.models import ArtifactKind, InstallRequest
from forge_workflow.editor.repository import DbWorkflowDefinitionRepository

pytestmark = [pytest.mark.postgres]

V2_LINE = LINE.replace("max_usd: 60", "max_usd: 70")


@pytest.fixture
def pg_sf(pg_engine: Engine) -> Iterator[sessionmaker[Session]]:
    Base.metadata.create_all(pg_engine)
    sf = sessionmaker(bind=pg_engine, expire_on_commit=False)
    with sf() as s:
        s.add(Workspace(id=WS_ID, name="Acme", slug="acme"))
        s.commit()
    try:
        yield sf
    finally:
        Base.metadata.drop_all(pg_engine)


@pytest.fixture
def pg(pg_sf: sessionmaker[Session], keypair: Keypair):
    with pg_sf() as s:
        registry = MarketplaceRegistry(
            workspace_id=WS_ID,
            slug="acme-community",
            name="Acme Community",
            type="http_index",
            url="https://registry.acme.test/index.json",
            public_key=keypair.public_b64,
            trust_level="community",
            enabled=True,
        )
        s.add(registry)
        s.commit()
        registry_id = registry.id
    gateway = FakeGateway(public_key=keypair.public_b64)
    service = MarketplaceService(session_factory=pg_sf, gateway=gateway)
    return service, gateway, registry_id


def _request(registry_id, version: str | None = None) -> InstallRequest:
    return InstallRequest(
        registry_id=registry_id,
        kind=ArtifactKind.workflow_template,
        slug=LINE_SLUG,
        version=version,
    )


def _count(sf: sessionmaker[Session], model: Any) -> int:
    with sf() as s:
        return s.scalar(select(func.count()).select_from(model)) or 0


def _denied(sf: sessionmaker[Session], code: str) -> list[MarketplaceAuditLog]:
    with sf() as s:
        return list(
            s.scalars(
                select(MarketplaceAuditLog).where(
                    MarketplaceAuditLog.result_status == "denied",
                    MarketplaceAuditLog.error_code == code,
                )
            )
        )


def _install(service: MarketplaceService, registry_id, version: str | None = None):
    return service.install(
        workspace_id=WS_ID,
        actor="user:a",
        actor_user_id=None,
        request=_request(registry_id, version),
    )


# --- item 1: values the definition row cannot hold ------------------------------ #


@pytest.mark.parametrize(
    "package",
    [
        {"title": "T" * 400},
        {"name": "a" * 80, "dsl_yaml": LINE.replace("implementation_loop", "a" * 80, 1)},
    ],
    ids=["title_400", "name_80"],
)
def test_an_oversized_field_is_artifact_invalid_not_a_500(
    pg, pg_sf: sessionmaker[Session], keypair: Keypair, package: dict
) -> None:
    service, gateway, registry_id = pg
    gateway.add(*make_line_version(keypair, **package))
    with pytest.raises(InstallBlockedError) as info:
        _install(service, registry_id)
    assert info.value.error_code == "artifact_invalid"
    assert _denied(pg_sf, "artifact_invalid")
    assert _count(pg_sf, WorkflowDefinition) == 0


def test_a_value_the_database_refuses_is_audited(
    pg, pg_sf: sessionmaker[Session], keypair: Keypair, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defence in depth: a DataError at flush/commit is a committed denied row."""
    service, gateway, registry_id = pg
    real = INSTALLERS[ArtifactKind.workflow_template]

    class TooLongTitle:
        kind = real.kind
        target_kind = real.target_kind

        def validate(self, artifact: dict) -> ValidatedArtifact:
            validated = real.validate(artifact)
            validated.resolved_config["title"] = "T" * 400
            return validated

    monkeypatch.setitem(INSTALLERS, ArtifactKind.workflow_template, TooLongTitle())
    gateway.add(*make_line_version(keypair))
    with pytest.raises(InstallBlockedError) as info:
        _install(service, registry_id)
    assert info.value.error_code == "database_refused"
    assert _denied(pg_sf, "database_refused")
    assert _count(pg_sf, WorkflowDefinition) == 0
    assert _count(pg_sf, MarketplaceInstallation) == 0


# --- item 2: updates and uninstalls are serialized --------------------------------- #


def _state(sf: sessionmaker[Session]) -> tuple[bool, str, bool, int]:
    with sf() as s:
        definition = s.scalars(select(WorkflowDefinition)).one()
        row = s.scalars(select(MarketplaceInstallation)).one()
        revisions = s.scalar(select(func.count()).select_from(WorkflowDefinitionRevision)) or 0
        return definition.is_active, row.status, row.pinned, revisions


def test_an_update_after_a_committed_uninstall_is_refused(
    pg, pg_sf: sessionmaker[Session], keypair: Keypair
) -> None:
    """The uninstall commits between the update's first read and its lock."""
    service, gateway, registry_id = pg
    gateway.add(*make_line_version(keypair))
    installed = _install(service, registry_id)
    gateway.add(*make_line_version(keypair, version="1.1.0", dsl_yaml=V2_LINE))

    fetch = gateway.fetch_manifest

    def uninstall_then_fetch(registry, uri):
        if uri.endswith("/1.1.0/forge-package.yaml"):
            service.uninstall(
                workspace_id=WS_ID, actor="user:b", installation_id=installed.installation_id
            )
        return fetch(registry, uri)

    gateway.fetch_manifest = uninstall_then_fetch  # type: ignore[method-assign]
    with pytest.raises(InstallBlockedError) as info:
        service.update_installation(
            workspace_id=WS_ID,
            actor="user:a",
            installation_id=installed.installation_id,
            version="1.1.0",
        )
    assert info.value.error_code == "installation_uninstalled"
    assert _state(pg_sf) == (False, "uninstalled", False, 1)
    assert _denied(pg_sf, "installation_uninstalled")


def test_an_uninstall_waits_for_an_update_holding_the_lock(
    pg, pg_sf: sessionmaker[Session], keypair: Keypair, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, gateway, registry_id = pg
    gateway.add(*make_line_version(keypair))
    installed = _install(service, registry_id)
    gateway.add(*make_line_version(keypair, version="1.1.0", dsl_yaml=V2_LINE))

    locked, release = threading.Event(), threading.Event()
    create_draft = DbWorkflowDefinitionRepository.create_draft

    def paused(self, *args, **kwargs):
        locked.set()  # past the installation row lock, before any write
        release.wait(30)
        return create_draft(self, *args, **kwargs)

    monkeypatch.setattr(DbWorkflowDefinitionRepository, "create_draft", paused)
    results: dict[str, Any] = {}

    def run(key: str, fn) -> None:
        try:
            results[key] = fn()
        except Exception as exc:  # surfaced by the asserts below
            results[key] = exc

    update = threading.Thread(
        target=run,
        args=(
            "update",
            lambda: service.update_installation(
                workspace_id=WS_ID,
                actor="user:a",
                installation_id=installed.installation_id,
                version="1.1.0",
            ),
        ),
    )
    uninstall = threading.Thread(
        target=run,
        args=(
            "uninstall",
            lambda: service.uninstall(
                workspace_id=WS_ID, actor="user:b", installation_id=installed.installation_id
            ),
        ),
    )
    update.start()
    assert locked.wait(30)
    uninstall.start()
    uninstall.join(1.5)
    blocked = uninstall.is_alive()
    release.set()
    update.join(30)
    uninstall.join(30)

    assert blocked, "the uninstall did not wait for the update's row lock"
    assert results["update"].version == "1.1.0", results["update"]
    assert results["uninstall"] is None, results["uninstall"]
    # The uninstall came second, so the line ends uninstalled and archived.
    assert _state(pg_sf) == (False, "uninstalled", False, 2)


# --- re-check: refusal records that cannot fail, and locked flag writers ------------- #


def test_a_nul_in_a_station_key_is_a_denied_row_not_a_500(
    pg, pg_sf: sessionmaker[Session], keypair: Keypair
) -> None:
    """The refusal text echoes the decoded key; Postgres text refuses NUL."""
    service, gateway, registry_id = pg
    gateway.add(*make_line_version(keypair, dsl_yaml=LINE.replace("  prepare:", '  "pre\\0pare":')))
    with pytest.raises(InstallBlockedError):
        _install(service, registry_id)
    with pg_sf() as s:
        rows = list(s.scalars(select(MarketplaceAuditLog)))
    assert [r.result_status for r in rows] == ["denied"]
    assert "\x00" not in (rows[0].detail or "")


def test_an_over_long_index_version_is_a_denied_row_not_a_500(
    pg, pg_sf: sessionmaker[Session], keypair: Keypair
) -> None:
    """The index row is not the signed manifest: its version can exceed the columns."""
    service, gateway, registry_id = pg
    manifest, version = make_line_version(keypair)
    long = "1.0.0-" + "a" * 64
    gateway.add(manifest, version.model_copy(update={"version": long}))
    with pytest.raises(InstallBlockedError):
        _install(service, registry_id, long)
    with pg_sf() as s:
        rows = list(s.scalars(select(MarketplaceAuditLog)))
    assert [r.result_status for r in rows] == ["denied"]
    assert rows[0].version == long[:64]
    assert _count(pg_sf, MarketplaceInstallation) == 0


def _paused(monkeypatch: pytest.MonkeyPatch, name: str) -> tuple[threading.Event, threading.Event]:
    """Pause ``MarketplaceService.<name>`` once it is called, until released."""
    entered, release = threading.Event(), threading.Event()
    real = getattr(MarketplaceService, name)

    def paused(self, *args, **kwargs):
        entered.set()
        release.wait(30)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(MarketplaceService, name, paused)
    return entered, release


def _race(first, second, entered: threading.Event, release: threading.Event) -> bool:
    """Run ``first`` to its pause, start ``second``; True when ``second`` had to wait."""
    errors: list[BaseException] = []

    def run(fn) -> None:
        try:
            fn()
        except BaseException as exc:  # surfaced below
            errors.append(exc)

    a, b = threading.Thread(target=run, args=(first,)), threading.Thread(target=run, args=(second,))
    a.start()
    assert entered.wait(30)
    b.start()
    b.join(1.5)
    waited = b.is_alive()
    release.set()
    a.join(30)
    b.join(30)
    assert not errors, errors
    return waited


def test_a_refresh_never_flags_a_line_an_uninstall_changed(
    pg, pg_sf: sessionmaker[Session], keypair: Keypair, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, gateway, registry_id = pg
    gateway.add(*make_line_version(keypair))
    service.sync_registry(workspace_id=WS_ID, actor="u", registry_id=registry_id)
    installed = _install(service, registry_id)
    gateway.add(*make_line_version(keypair, version="1.1.0", dsl_yaml=V2_LINE))
    service.sync_registry(workspace_id=WS_ID, actor="u", registry_id=registry_id)

    entered, release = _paused(monkeypatch, "_cached_versions")  # after the locked read
    waited = _race(
        lambda: service.refresh_update_flags(workspace_id=WS_ID),
        lambda: service.uninstall(
            workspace_id=WS_ID, actor="user:b", installation_id=installed.installation_id
        ),
        entered,
        release,
    )
    assert waited, "the uninstall did not wait for the refresh's row lock"
    assert _state(pg_sf) == (False, "uninstalled", False, 1)
    with pytest.raises(InstallBlockedError) as info:
        service.update_installation(
            workspace_id=WS_ID,
            actor="user:a",
            installation_id=installed.installation_id,
            version="1.1.0",
        )
    assert info.value.error_code == "installation_uninstalled"


def test_a_pin_never_reinstates_a_line_an_uninstall_changed(
    pg, pg_sf: sessionmaker[Session], keypair: Keypair, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, gateway, registry_id = pg
    gateway.add(*make_line_version(keypair))
    service.sync_registry(workspace_id=WS_ID, actor="u", registry_id=registry_id)
    installed = _install(service, registry_id)
    gateway.add(*make_line_version(keypair, version="1.1.0", dsl_yaml=V2_LINE))
    service.sync_registry(workspace_id=WS_ID, actor="u", registry_id=registry_id)
    assert service.refresh_update_flags(workspace_id=WS_ID) == 1

    entered, release = _paused(monkeypatch, "_write_audit")  # the uninstall's, after its lock
    waited = _race(
        lambda: service.uninstall(
            workspace_id=WS_ID, actor="user:b", installation_id=installed.installation_id
        ),
        lambda: service.set_pin(
            workspace_id=WS_ID,
            actor="user:a",
            installation_id=installed.installation_id,
            pinned=True,
        ),
        entered,
        release,
    )
    assert waited, "the pin did not wait for the uninstall's row lock"
    is_active, row_status, _, _ = _state(pg_sf)
    assert (is_active, row_status) == (False, "uninstalled")


def test_a_refresh_clears_a_flag_the_update_satisfied(
    pg, pg_sf: sessionmaker[Session], keypair: Keypair
) -> None:
    service, gateway, registry_id = pg
    gateway.add(*make_line_version(keypair))
    service.sync_registry(workspace_id=WS_ID, actor="u", registry_id=registry_id)
    installed = _install(service, registry_id)
    gateway.add(*make_line_version(keypair, version="1.1.0", dsl_yaml=V2_LINE))
    service.sync_registry(workspace_id=WS_ID, actor="u", registry_id=registry_id)
    assert service.refresh_update_flags(workspace_id=WS_ID) == 1
    with pg_sf() as s:  # a flag left behind (the race the lock now prevents)
        row = s.get(MarketplaceInstallation, installed.installation_id)
        row.installed_version = "1.1.0"
        s.commit()
    assert service.refresh_update_flags(workspace_id=WS_ID) == 0
    with pg_sf() as s:
        row = s.get(MarketplaceInstallation, installed.installation_id)
        assert (row.status, row.available_version) == ("installed", None)
