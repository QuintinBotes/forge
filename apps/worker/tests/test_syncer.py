"""Tests for the worker syncer task (plan Task 1.4 — full + incremental sync).

Hermetic: in-memory SQLite-backed :class:`KnowledgeService`, deterministic
embedding, fixture reranker. No network, no live Redis/Celery broker.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from forge_api.auth.crypto import HmacAeadCipher
from forge_api.auth.vault import SecretVault
from forge_api.observability.audit import AuditLog
from forge_api.services.pm_service import PMConnectionService
from forge_contracts.enums import APIKeyKind, SyncMode
from forge_db.base import Base
from forge_db.models import KnowledgeSource, Project, Workspace
from forge_db.models.enums import PMConnectionStatus, PMProvider
from forge_db.models.pm import PMConnection
from forge_db.session import create_session_factory
from forge_integrations.github import RetryPolicy
from forge_knowledge import (
    DeterministicEmbeddingClient,
    FixtureRerankerClient,
    KnowledgeService,
)
from forge_worker.syncer import (
    TRACKER_NOT_CONNECTED,
    TrackerNotConnectedError,
    github_tracker_client,
    sync_files,
    sync_repo,
    sync_source_task,
)


@pytest.fixture
def session_factory() -> sessionmaker[Session]:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


@pytest.fixture
def source_id(session_factory: sessionmaker[Session]) -> uuid.UUID:
    with session_factory() as session:
        workspace = Workspace(name="Acme", slug="acme")
        session.add(workspace)
        session.flush()
        source = KnowledgeSource(
            workspace_id=workspace.id, kind="repo", name="api", uri="github.com/org/api"
        )
        session.add(source)
        session.flush()
        src_id = source.id
        session.commit()
    return src_id


@pytest.fixture
def service(session_factory: sessionmaker[Session]) -> KnowledgeService:
    return KnowledgeService.from_session_factory(
        session_factory,
        DeterministicEmbeddingClient(dimension=256),
        FixtureRerankerClient(),
    )


FILES = {
    "auth.py": "def validate_jwt(token):\n    return verify(token)\n",
    "README.md": "# Service\n\nValidates oauth jwt tokens.\n",
}


def test_sync_files_indexes_then_prunes(service: KnowledgeService, source_id: uuid.UUID) -> None:
    first = sync_files(service, str(source_id), FILES)
    assert first.indexed > 0

    # Drop README -> a pruning full sync removes its chunks.
    pruned = sync_files(service, str(source_id), {"auth.py": FILES["auth.py"]})
    assert pruned.deleted > 0


def test_sync_files_is_idempotent(service: KnowledgeService, source_id: uuid.UUID) -> None:
    first = sync_files(service, str(source_id), FILES)
    second = sync_files(service, str(source_id), FILES)
    assert first.indexed > 0
    assert second.indexed == 0


def test_celery_task_is_registered() -> None:
    from forge_worker.celery_app import celery_app

    assert "forge.knowledge.sync_source" in celery_app.tasks
    assert sync_source_task.name == "forge.knowledge.sync_source"


def test_sync_repo_full_sync_from_tree(
    service: KnowledgeService, source_id: uuid.UUID, tmp_path: Path
) -> None:
    (tmp_path / "auth.py").write_text(FILES["auth.py"])
    (tmp_path / "README.md").write_text(FILES["README.md"])
    result = sync_repo(service, str(source_id), str(tmp_path), mode=SyncMode.FULL)
    assert result.indexed > 0
    assert result.source_id == str(source_id)


def test_sync_source_task_with_files(
    monkeypatch: pytest.MonkeyPatch,
    service: KnowledgeService,
    source_id: uuid.UUID,
) -> None:
    monkeypatch.setattr("forge_worker.syncer.build_knowledge_service", lambda: service)
    result = sync_source_task(str(source_id), files=FILES)
    assert isinstance(result, dict)
    assert result["indexed"] > 0


def test_sync_source_task_with_root(
    monkeypatch: pytest.MonkeyPatch,
    service: KnowledgeService,
    source_id: uuid.UUID,
    tmp_path: Path,
) -> None:
    (tmp_path / "auth.py").write_text(FILES["auth.py"])
    monkeypatch.setattr("forge_worker.syncer.build_knowledge_service", lambda: service)
    result = sync_source_task(str(source_id), root=str(tmp_path))
    assert isinstance(result, dict)
    assert result["indexed"] > 0


def test_sync_source_task_requires_files_or_root(
    monkeypatch: pytest.MonkeyPatch,
    service: KnowledgeService,
    source_id: uuid.UUID,
) -> None:
    monkeypatch.setattr("forge_worker.syncer.build_knowledge_service", lambda: service)
    with pytest.raises(ValueError, match="requires either"):
        sync_source_task(str(source_id))


# --------------------------------------------------------------------------- #
# GitHub tracker sources (spec J §2.3)                                         #
# --------------------------------------------------------------------------- #


class _FakeGitHub:
    """``iter_pages`` over one issue in GitHub's REST shape."""

    closed = False

    def iter_pages(
        self, repo: str, path: str, params: Mapping[str, str] | None = None
    ) -> Iterator[dict[str, Any]]:
        assert repo == "org/api"
        if path == "issues":
            yield {
                "number": 7,
                "title": "Search drops hits",
                "state": "open",
                "user": {"login": "ana"},
                "labels": [],
                "body": "Steps.",
                "created_at": "2026-09-01T10:00:00Z",
            }

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def tracker_id(session_factory: sessionmaker[Session], source_id: uuid.UUID) -> uuid.UUID:
    with session_factory() as session:
        repo_source = session.get(KnowledgeSource, source_id)
        assert repo_source is not None
        tracker = KnowledgeSource(
            workspace_id=repo_source.workspace_id,
            kind="url",
            name="api tracker",
            uri="https://github.com/org/api",
            config={"tracker": "github"},
        )
        session.add(tracker)
        session.commit()
        return tracker.id


def _vault() -> SecretVault:
    return SecretVault(cipher=HmacAeadCipher(b"0" * 32))


def _connections(session_factory: sessionmaker[Session], vault: SecretVault) -> PMConnectionService:
    return PMConnectionService(session_factory=session_factory, vault=vault, audit=AuditLog())


def _connect(
    session_factory: sessionmaker[Session],
    vault: SecretVault,
    workspace_id: uuid.UUID,
    *,
    repo: str = "org/api",
    status: PMConnectionStatus = PMConnectionStatus.CONNECTED,
    base_url: str | None = None,
) -> None:
    secret = vault.put_secret(
        workspace_id=workspace_id,
        name="github issues",
        secret="ghs_from_the_vault",
        kind=APIKeyKind.INTEGRATION_TOKEN,
    )
    with session_factory() as session:
        project = Project(workspace_id=workspace_id, name="Core", key=f"C{uuid.uuid4().hex[:6]}")
        session.add(project)
        session.flush()
        session.add(
            PMConnection(
                workspace_id=workspace_id,
                provider=PMProvider.GITHUB_ISSUES,
                name="GitHub Issues",
                project_id=project.id,
                external_project_key=repo,
                external_project_id="10",
                external_base_url=base_url,
                credential_ref=str(secret.id),
                status=status,
            )
        )
        session.commit()


def _tracker(session_factory: sessionmaker[Session], tracker_id: uuid.UUID) -> KnowledgeSource:
    with session_factory() as session:
        source = session.get(KnowledgeSource, tracker_id)
        assert source is not None
        return source


def test_sync_source_task_syncs_a_tracker_through_the_client_factory(
    monkeypatch: pytest.MonkeyPatch,
    service: KnowledgeService,
    tracker_id: uuid.UUID,
) -> None:
    clients: list[_FakeGitHub] = []

    def factory(source: KnowledgeSource) -> _FakeGitHub:
        assert source.id == tracker_id
        clients.append(_FakeGitHub())
        return clients[-1]

    monkeypatch.setattr("forge_worker.syncer.build_knowledge_service", lambda: service)
    monkeypatch.setattr("forge_worker.syncer.tracker_client_factory", factory)

    first = sync_source_task(str(tracker_id))
    second = sync_source_task(str(tracker_id))

    assert first["indexed"] == 1
    assert (second["indexed"], second["deleted"]) == (0, 0)
    assert [c.closed for c in clients] == [True, True]


def test_a_tracker_sync_without_a_github_connection_fails_tracker_not_connected(
    monkeypatch: pytest.MonkeyPatch,
    service: KnowledgeService,
    session_factory: sessionmaker[Session],
    tracker_id: uuid.UUID,
) -> None:
    vault = _vault()
    workspace_id = _tracker(session_factory, tracker_id).workspace_id
    # Neither another repository's connection nor a disabled one covers it.
    _connect(session_factory, vault, workspace_id, repo="org/other")
    _connect(session_factory, vault, workspace_id, status=PMConnectionStatus.DISABLED)
    connections = _connections(session_factory, vault)
    monkeypatch.setattr("forge_worker.syncer.build_knowledge_service", lambda: service)
    monkeypatch.setattr(
        "forge_worker.syncer.tracker_client_factory",
        lambda source: github_tracker_client(source, connections),
    )

    with pytest.raises(TrackerNotConnectedError) as raised:
        sync_source_task(str(tracker_id))

    assert raised.value.code == TRACKER_NOT_CONNECTED == "tracker_not_connected"
    assert str(raised.value).startswith("tracker_not_connected")


def test_github_tracker_client_is_built_from_the_workspace_connection(
    session_factory: sessionmaker[Session], tracker_id: uuid.UUID
) -> None:
    vault = _vault()
    source = _tracker(session_factory, tracker_id)
    _connect(
        session_factory, vault, source.workspace_id, repo="Org/API", base_url="https://ghe.test/api"
    )
    connections = _connections(session_factory, vault)

    client = github_tracker_client(source, connections)

    [connection] = connections.list(source.workspace_id)
    # The GitHub Issues adapter's own header, base URL and retry policy.
    assert client._client.headers["Authorization"] == connections._auth_header(connection)
    assert client._client.headers["Authorization"] == "Bearer ghs_from_the_vault"
    assert str(client._client.base_url) == "https://ghe.test/api/"
    assert client._retry == RetryPolicy()
    client.close()


@pytest.mark.parametrize(
    ("config", "commit_paths"),
    [({}, 0), ({"commits": {"enabled": True, "max_count": 1}}, 1)],
    ids=["not_enabled", "enabled_bounded"],
)
def test_sync_source_task_indexes_commits_only_when_the_source_enables_them(
    monkeypatch: pytest.MonkeyPatch,
    service: KnowledgeService,
    session_factory: sessionmaker[Session],
    source_id: uuid.UUID,
    tmp_path: Path,
    config: dict[str, Any],
    commit_paths: int,
) -> None:
    import subprocess

    from forge_db.models import RetrievalChunk

    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    for n in range(2):
        (tmp_path / "auth.py").write_text(f"def validate_jwt(token):\n    return {n}\n")
        subprocess.run([*git, "add", "-A"], cwd=tmp_path, check=True)
        subprocess.run([*git, "commit", "-q", "-m", f"change {n}"], cwd=tmp_path, check=True)
    with session_factory() as session:
        session.get(KnowledgeSource, source_id).config = config
        session.commit()
    monkeypatch.setattr("forge_worker.syncer.build_knowledge_service", lambda: service)

    sync_source_task(str(source_id), root=str(tmp_path))

    with session_factory() as session:
        paths = {
            row.path
            for row in session.query(RetrievalChunk).filter_by(knowledge_source_id=source_id)
        }
    assert len({p for p in paths if p and p.startswith("commits/")}) == commit_paths


# --------------------------------------------------------------------------- #
# Freshness: a successful sync stamps last_synced_at, a failed one never does  #
# --------------------------------------------------------------------------- #


def _last_synced(session_factory: sessionmaker[Session], source_id: uuid.UUID) -> object:
    with session_factory() as session:
        source = session.get(KnowledgeSource, source_id)
        assert source is not None
        return source.last_synced_at


def test_a_successful_sync_records_freshness(
    monkeypatch: pytest.MonkeyPatch,
    service: KnowledgeService,
    session_factory: sessionmaker[Session],
    source_id: uuid.UUID,
    tracker_id: uuid.UUID,
) -> None:
    monkeypatch.setattr("forge_worker.syncer.build_knowledge_service", lambda: service)
    monkeypatch.setattr("forge_worker.syncer.tracker_client_factory", lambda _s: _FakeGitHub())

    sync_source_task(str(source_id), files=FILES)
    sync_source_task(str(tracker_id))

    assert _last_synced(session_factory, source_id) is not None
    assert _last_synced(session_factory, tracker_id) is not None


def test_a_failed_sync_never_records_freshness(
    monkeypatch: pytest.MonkeyPatch,
    service: KnowledgeService,
    session_factory: sessionmaker[Session],
    source_id: uuid.UUID,
    tracker_id: uuid.UUID,
) -> None:
    from forge_contracts import IndexResult

    def no_connection(_source: KnowledgeSource) -> _FakeGitHub:
        raise TrackerNotConnectedError("tracker_not_connected: no GitHub Issues connection")

    monkeypatch.setattr("forge_worker.syncer.build_knowledge_service", lambda: service)
    monkeypatch.setattr("forge_worker.syncer.tracker_client_factory", no_connection)
    monkeypatch.setattr(
        "forge_worker.syncer.sync_files",
        lambda _svc, sid, _files, prune: IndexResult(source_id=sid, errors=["embedding: down"]),
    )

    with pytest.raises(TrackerNotConnectedError):
        sync_source_task(str(tracker_id))
    reported = sync_source_task(str(source_id), files=FILES)

    assert reported["errors"] == ["embedding: down"]
    assert _last_synced(session_factory, tracker_id) is None
    assert _last_synced(session_factory, source_id) is None
