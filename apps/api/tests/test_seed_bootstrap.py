"""The seed's bootstrap credential (F1).

A fresh self-host had no way to sign in at all: the seed created a workspace and
an admin *user* but no credential, there was no bootstrap command, and
``POST /auth/login`` starts an OAuth handshake against an IdP nobody has
configured. The only way in was minting a key by hand inside the container.

These tests pin the seed's opt-in bootstrap key: that it stays off unless asked
for, that the printed token really authenticates, that re-running retires the
previous one, and that a seed run against an in-memory key backend says so
rather than handing back a token that silently does nothing.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import StaticPool, create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from forge_api.auth.service import AuthenticationError, AuthService
from forge_api.scripts import seed as seed_module
from forge_db.base import Base
from forge_db.models import PlatformAPIKey, Project, User, Workspace


@pytest.fixture
def factory(monkeypatch: pytest.MonkeyPatch) -> Iterator[sessionmaker[Session]]:
    """A hermetic SQLite database wired in everywhere the seed reaches for one."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)

    # The seed opens its own session; the DB API-key backend resolves the
    # process-wide factory. Point both at this database.
    monkeypatch.setattr(seed_module, "create_session_factory", lambda: session_factory)
    monkeypatch.setattr("forge_api.db.get_session_factory", lambda: session_factory)
    yield session_factory
    Base.metadata.drop_all(engine)


@pytest.fixture
def durable_keys(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Select the Postgres-backed key store, as the compose files now do."""
    monkeypatch.setenv("FORGE_APIKEY_BACKEND", "db")
    from forge_api.settings import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _token_from(capsys: pytest.CaptureFixture[str]) -> str | None:
    for word in capsys.readouterr().out.split():
        if word.startswith("forge_"):
            return word
    return None


# --- the seed itself is unchanged and still idempotent --------------------- #


def test_seed_creates_the_demo_workspace_and_admin(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FORGE_SEED_ADMIN_KEY", raising=False)
    seed_module.seed()

    with factory() as session:
        workspace = session.scalar(
            select(Workspace).where(Workspace.slug == seed_module.DEMO_WORKSPACE_SLUG)
        )
        assert workspace is not None
        admin = session.scalar(select(User).where(User.email == seed_module.DEMO_ADMIN_EMAIL))
        assert admin is not None
        assert admin.workspace_id == workspace.id


def test_seed_creates_a_default_project(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A task's project is a required FK, so no project means no work at all."""
    monkeypatch.delenv("FORGE_SEED_ADMIN_KEY", raising=False)
    seed_module.seed()

    with factory() as session:
        project = session.scalar(select(Project).where(Project.key == seed_module.DEMO_PROJECT_KEY))
        assert project is not None
        workspace = session.scalar(
            select(Workspace).where(Workspace.slug == seed_module.DEMO_WORKSPACE_SLUG)
        )
        assert project.workspace_id == workspace.id


def test_seed_is_idempotent(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FORGE_SEED_ADMIN_KEY", raising=False)
    seed_module.seed()
    seed_module.seed()

    with factory() as session:
        assert len(session.scalars(select(Workspace)).all()) == 1
        assert len(session.scalars(select(User)).all()) == 1
        assert len(session.scalars(select(Project)).all()) == 1


# --- the bootstrap key ------------------------------------------------------ #


def test_no_key_is_minted_unless_asked_for(
    factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The token goes to stdout, which on compose is a log — never by default."""
    monkeypatch.delenv("FORGE_SEED_ADMIN_KEY", raising=False)
    seed_module.seed()

    out = capsys.readouterr().out
    assert "forge_" not in out
    assert "FORGE_SEED_ADMIN_KEY=1" in out  # says how to get one


@pytest.mark.parametrize("flag", ["1", "true", "TRUE", "yes", "on"])
def test_the_flag_accepts_the_usual_truthy_spellings(
    monkeypatch: pytest.MonkeyPatch, flag: str
) -> None:
    monkeypatch.setenv("FORGE_SEED_ADMIN_KEY", flag)
    assert seed_module._mint_admin_key_enabled()


@pytest.mark.parametrize("flag", ["", "0", "false", "no", "off", "maybe"])
def test_the_flag_rejects_everything_else(monkeypatch: pytest.MonkeyPatch, flag: str) -> None:
    monkeypatch.setenv("FORGE_SEED_ADMIN_KEY", flag)
    assert not seed_module._mint_admin_key_enabled()


def test_the_minted_key_is_printed_and_persisted(
    factory: sessionmaker[Session],
    durable_keys: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("FORGE_SEED_ADMIN_KEY", "1")
    seed_module.seed()

    token = _token_from(capsys)
    assert token is not None and token.startswith("forge_")

    with factory() as session:
        rows = session.scalars(select(PlatformAPIKey)).all()
        assert len(rows) == 1
        assert rows[0].name == seed_module.BOOTSTRAP_KEY_NAME
        # Only the keyed hash is stored — never the token itself.
        assert token not in rows[0].key_hash


def test_the_minted_key_authenticates_as_a_workspace_admin(
    factory: sessionmaker[Session],
    durable_keys: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The point of the whole feature: this token actually gets you in."""
    monkeypatch.setenv("FORGE_SEED_ADMIN_KEY", "1")
    seed_module.seed()
    token = _token_from(capsys)
    assert token is not None

    principal = AuthService().authenticate(token)
    assert principal.role.value == "admin"
    assert principal.auth_method == "api_key"
    with factory() as session:
        workspace = session.scalar(
            select(Workspace).where(Workspace.slug == seed_module.DEMO_WORKSPACE_SLUG)
        )
        assert principal.workspace_id == workspace.id


def test_reseeding_retires_the_previous_key(
    factory: sessionmaker[Session],
    durable_keys: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A re-run must not leave a second live admin credential behind."""
    monkeypatch.setenv("FORGE_SEED_ADMIN_KEY", "1")
    seed_module.seed()
    first = _token_from(capsys)
    seed_module.seed()
    second = _token_from(capsys)

    assert first and second and first != second

    service = AuthService()
    assert service.authenticate(second) is not None
    with pytest.raises(AuthenticationError):
        service.authenticate(first)

    with factory() as session:
        rows = session.scalars(select(PlatformAPIKey)).all()
        assert len(rows) == 2
        assert sum(1 for r in rows if r.revoked_at is None) == 1


# --- the F3 trap the evaluator hit ----------------------------------------- #


def test_an_inmemory_backend_is_called_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORGE_APIKEY_BACKEND", "memory")
    from forge_api.settings import get_settings

    get_settings.cache_clear()
    try:
        warning = seed_module.warn_if_key_is_ephemeral()
        assert warning is not None
        assert "FORGE_APIKEY_BACKEND=db" in warning
    finally:
        get_settings.cache_clear()


def test_a_db_backend_warns_about_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORGE_APIKEY_BACKEND", "db")
    from forge_api.settings import get_settings

    get_settings.cache_clear()
    try:
        assert seed_module.warn_if_key_is_ephemeral() is None
    finally:
        get_settings.cache_clear()
