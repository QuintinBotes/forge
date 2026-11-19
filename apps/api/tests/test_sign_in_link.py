"""One-time sign-in links for the first administrator (#260, FR-010).

``scf-cli admin sign-in-link --workspace <slug> --email <address>`` prints a link and its
expiry; ``GET /auth/sign-in-link?token=...`` redeems it once into the session cookie an
SSO sign-in sets. Exercised through ``cli.main`` and the ASGI app over one SQLite file the
CLI's session-factory seam and the app's ``get_db`` share.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import threading
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

import scf_api.auth.service as auth_service_module
from scf_api import cli as cli_mod
from scf_api.auth import sign_in_link
from scf_api.auth.service import AuthService
from scf_api.db import get_db
from scf_api.main import create_app
from scf_api.observability.redaction import RedactingLogFilter
from scf_api.routers.saml import SESSION_TTL
from scf_api.settings import Settings, get_settings
from scf_db.base import Base
from scf_db.models import (
    AuditLog,
    OidcConfiguration,
    SignInLink,
    SsoConfiguration,
    User,
    Workspace,
)
from scf_db.models.enums import UserRole

WS = uuid.UUID("00000000-0000-0000-0000-0000000260a1")
OTHER_WS = uuid.UUID("00000000-0000-0000-0000-0000000260a2")
ADMIN = uuid.UUID("00000000-0000-0000-0000-0000000260b1")
REFUSED = {"detail": "this sign-in link is not valid"}


@pytest.fixture
def factory(tmp_path: Path) -> sessionmaker[Session]:
    engine = create_engine(
        f"sqlite:///{tmp_path / 'sign_in_link.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        session.add_all(
            [
                Workspace(id=WS, name="Acme", slug="acme"),
                Workspace(id=OTHER_WS, name="Globex", slug="globex"),
            ]
        )
        session.commit()
    return factory


@pytest.fixture
def auth(monkeypatch: pytest.MonkeyPatch) -> AuthService:
    service = AuthService(secret_key=b"6" * 32)
    monkeypatch.setattr(auth_service_module, "_default_service", service)
    return service


@pytest.fixture
def client(factory: sessionmaker[Session], auth: AuthService) -> Iterator[TestClient]:
    app = create_app()

    def _get_db() -> Iterator[Session]:
        with factory() as session:
            yield session

    app.dependency_overrides[get_db] = _get_db
    with TestClient(app, follow_redirects=False) as client:
        yield client


@pytest.fixture
def cli(
    factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> Callable[..., tuple[int, str, str]]:
    monkeypatch.setattr(cli_mod, "_db_session_factory", lambda: factory)

    def _run(email: str = "first@acme.test", workspace: str = "acme") -> tuple[int, str, str]:
        code = cli_mod.main(["admin", "sign-in-link", "--workspace", workspace, "--email", email])
        out, err = capsys.readouterr()
        return code, out, err

    return _run


def add_user(
    factory: sessionmaker[Session],
    email: str,
    role: UserRole = UserRole.ADMIN,
    *,
    active: bool = True,
    user_id: uuid.UUID | None = None,
) -> uuid.UUID:
    with factory() as session:
        user = User(
            id=user_id or uuid.uuid4(), workspace_id=WS, email=email, role=role, is_active=active
        )
        session.add(user)
        session.commit()
        return user.id


def link_of(out: str) -> str:
    return next(word for word in out.split() if "/auth/sign-in-link?" in word)


def token_of(link: str) -> str:
    return parse_qs(urlsplit(link).query)["token"][0]


def issue(cli: Callable[..., tuple[int, str, str]], email: str = "first@acme.test") -> str:
    code, out, err = cli(email)
    assert code == 0, err
    return token_of(link_of(out))


def counts(factory: sessionmaker[Session]) -> tuple[int, int]:
    with factory() as session:
        return (
            session.scalar(select(func.count()).select_from(User)) or 0,
            session.scalar(select(func.count()).select_from(SignInLink)) or 0,
        )


def redeem(client: TestClient, token: str, **params: str) -> httpx.Response:
    return client.get("/auth/sign-in-link", params={"token": token, **params})


def enable_identity_provider(factory: sessionmaker[Session], protocol: str) -> None:
    with factory() as session:
        if protocol == "oidc":
            session.add(
                OidcConfiguration(
                    workspace_id=WS,
                    enabled=True,
                    issuer="https://idp.example",
                    client_id="client",
                    client_secret_ref="vault://oidc",
                    client_secret_encrypted=b"x",
                )
            )
        else:
            session.add(
                SsoConfiguration(
                    workspace_id=WS,
                    enabled=True,
                    idp_entity_id="https://idp.example",
                    idp_sso_url="https://idp.example/sso",
                    idp_x509_certs=[],
                    sp_entity_id="https://scf.example/sp",
                    sp_private_key_encrypted=b"x",
                    sp_cert_pem="pem",
                )
            )
        session.commit()


# --------------------------------------------------------------------------- #
# Criterion 1: the command prints one link from the public address + expiry   #
# --------------------------------------------------------------------------- #


def test_the_command_prints_a_link_from_the_public_url_and_when_it_expires(
    cli: Callable[..., tuple[int, str, str]], factory: sessionmaker[Session]
) -> None:
    before = datetime.now(UTC)
    code, out, err = cli()

    assert code == 0, err
    link = link_of(out)
    assert link.startswith(f"{get_settings().public_url.rstrip('/')}/auth/sign-in-link?token=")
    token = token_of(link)
    assert len(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))) >= 32
    with factory() as session:
        row = session.scalars(select(SignInLink)).one()
    # Only a one-way hash is stored; the token itself is in no column.
    assert row.token_hash == hashlib.sha256(token.encode()).hexdigest()
    assert token not in repr({c.name: getattr(row, c.key) for c in SignInLink.__mapper__.columns})
    expires = datetime.fromisoformat(next(w for w in out.split() if w.startswith("20")))
    assert before + timedelta(minutes=15) <= expires <= datetime.now(UTC) + timedelta(minutes=15)


def test_the_link_keeps_the_path_prefix_of_the_public_url(
    cli: Callable[..., tuple[int, str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FORGE_PUBLIC_URL", "https://scf.example/api/")
    get_settings.cache_clear()
    try:
        code, out, err = cli()
    finally:
        get_settings.cache_clear()
    assert code == 0, err
    assert link_of(out).startswith("https://scf.example/api/auth/sign-in-link?token=")


# --------------------------------------------------------------------------- #
# Criterion 2: a first administrator is created; otherwise it must exist       #
# --------------------------------------------------------------------------- #


def test_a_workspace_without_an_administrator_gets_one_with_the_given_email(
    cli: Callable[..., tuple[int, str, str]], factory: sessionmaker[Session]
) -> None:
    assert cli("First@Acme.test")[0] == 0
    with factory() as session:
        user = session.scalars(select(User)).one()
    assert (user.workspace_id, user.email, user.role, user.is_active) == (
        WS,
        "First@Acme.test",
        UserRole.ADMIN,
        True,
    )


def test_an_existing_active_administrator_gets_a_link(
    cli: Callable[..., tuple[int, str, str]], factory: sessionmaker[Session]
) -> None:
    admin = add_user(factory, "boss@acme.test")
    assert cli("BOSS@acme.test")[0] == 0
    with factory() as session:
        assert session.scalars(select(SignInLink)).one().user_id == admin
        assert session.scalar(select(func.count()).select_from(User)) == 1


@pytest.mark.parametrize(
    "email",
    ["stranger@acme.test", "member@acme.test", "gone@acme.test"],
    ids=["unknown", "not-an-admin", "deactivated-admin"],
)
def test_with_an_administrator_the_email_must_name_an_active_one(
    cli: Callable[..., tuple[int, str, str]], factory: sessionmaker[Session], email: str
) -> None:
    add_user(factory, "boss@acme.test")
    add_user(factory, "member@acme.test", UserRole.MEMBER)
    add_user(factory, "gone@acme.test", active=False)
    before = counts(factory)

    code, out, err = cli(email)

    assert code != 0
    assert err.strip() and "/auth/sign-in-link" not in out
    assert counts(factory) == before


def test_a_member_with_the_email_of_a_first_administrator_is_refused(
    cli: Callable[..., tuple[int, str, str]], factory: sessionmaker[Session]
) -> None:
    add_user(factory, "member@acme.test", UserRole.MEMBER)
    before = counts(factory)
    code, _, err = cli("member@acme.test")
    assert code != 0 and err.strip()
    assert counts(factory) == before


# --------------------------------------------------------------------------- #
# Criterion 3: no workspace, or an enabled identity provider -> refused        #
# --------------------------------------------------------------------------- #


def test_an_unknown_workspace_is_refused(
    cli: Callable[..., tuple[int, str, str]], factory: sessionmaker[Session]
) -> None:
    code, _, err = cli(workspace="nope")
    assert code != 0 and "nope" in err
    assert counts(factory) == (0, 0)


@pytest.mark.parametrize("protocol", ["oidc", "saml"])
def test_a_workspace_with_an_enabled_identity_provider_is_refused(
    cli: Callable[..., tuple[int, str, str]], factory: sessionmaker[Session], protocol: str
) -> None:
    enable_identity_provider(factory, protocol)
    code, _, err = cli()
    assert code != 0 and err.strip()
    assert counts(factory) == (0, 0)


# --------------------------------------------------------------------------- #
# Criterion 4: redeeming sets the session cookie and redirects                 #
# --------------------------------------------------------------------------- #


def test_redeeming_signs_the_administrator_in_with_the_sso_session_cookie(
    cli: Callable[..., tuple[int, str, str]],
    client: TestClient,
    auth: AuthService,
    factory: sessionmaker[Session],
) -> None:
    admin = add_user(factory, "boss@acme.test", user_id=ADMIN)
    token = issue(cli, "boss@acme.test")

    resp = redeem(client, token, next="/board?x=1")

    assert resp.status_code == 302, resp.text
    assert resp.headers["location"] == "/board?x=1"
    attributes = {a.strip().split("=")[0].lower() for a in resp.headers["set-cookie"].split(";")}
    assert {"httponly", "path", "samesite"} <= attributes and "secure" not in attributes
    assert "samesite=lax" in resp.headers["set-cookie"].lower()
    assert f"max-age={int(SESSION_TTL.total_seconds())}" in resp.headers["set-cookie"].lower()
    principal = auth.authenticate(resp.cookies["forge_session"])
    assert (principal.user_id, principal.workspace_id, principal.role) == (
        admin,
        WS,
        UserRole.ADMIN,
    )
    assert principal.credential_expires_at is not None
    assert abs(principal.credential_expires_at - (datetime.now(UTC) + SESSION_TTL)) < timedelta(
        minutes=1
    )


@pytest.mark.parametrize("unsafe", [None, "//evil.example", "https://evil.example", "/\\evil"])
def test_redeeming_redirects_only_to_a_safe_relative_path(
    cli: Callable[..., tuple[int, str, str]], client: TestClient, unsafe: str | None
) -> None:
    token = issue(cli)
    resp = redeem(client, token, **({} if unsafe is None else {"next": unsafe}))
    assert resp.status_code == 302 and resp.headers["location"] == "/"


def test_the_cookie_is_secure_when_the_public_url_is_https(
    cli: Callable[..., tuple[int, str, str]], client: TestClient
) -> None:
    token = issue(cli)
    client.app.dependency_overrides[get_settings] = lambda: Settings(
        public_url="https://scf.example/api"
    )
    resp = redeem(client, token)
    assert resp.status_code == 302
    assert "secure" in {a.strip().lower() for a in resp.headers["set-cookie"].split(";")}


# --------------------------------------------------------------------------- #
# Criterion 5: single use, also under a race                                   #
# --------------------------------------------------------------------------- #


def test_a_link_works_once(cli: Callable[..., tuple[int, str, str]], client: TestClient) -> None:
    token = issue(cli)
    assert redeem(client, token).status_code == 302
    again = redeem(client, token)
    assert again.status_code == 401
    assert "set-cookie" not in again.headers


def test_two_concurrent_redemptions_of_one_link_let_exactly_one_through(
    cli: Callable[..., tuple[int, str, str]], factory: sessionmaker[Session]
) -> None:
    token = issue(cli)
    barrier = threading.Barrier(4)
    results: list[object] = []

    def _redeem() -> None:
        with factory() as session:
            barrier.wait()
            results.append(sign_in_link.redeem(session, token))

    threads = [threading.Thread(target=_redeem) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == 4
    assert sum(result is not None for result in results) == 1


# --------------------------------------------------------------------------- #
# Criterion 6: the lifetime                                                    #
# --------------------------------------------------------------------------- #


def test_a_link_is_refused_after_its_lifetime(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    ttl = get_settings().sign_in_link_ttl_seconds
    with factory() as session:
        issued = sign_in_link.issue(
            session,
            workspace_slug="acme",
            email="first@acme.test",
            public_url="http://localhost:8000",
            ttl_seconds=ttl,
            now=datetime.now(UTC) - timedelta(seconds=ttl + 1),
        )
    resp = redeem(client, token_of(issued.url))
    assert resp.status_code == 401 and "set-cookie" not in resp.headers


def test_the_lifetime_is_fifteen_minutes_by_default_and_at_most_an_hour(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert Settings().sign_in_link_ttl_seconds == 15 * 60
    assert Settings(sign_in_link_ttl_seconds=3600).sign_in_link_ttl_seconds == 3600
    with pytest.raises(ValidationError):
        Settings(sign_in_link_ttl_seconds=3601)
    with pytest.raises(ValidationError):
        Settings(sign_in_link_ttl_seconds=0)
    monkeypatch.setenv("FORGE_SIGN_IN_LINK_TTL_SECONDS", "7200")
    with pytest.raises(ValidationError):
        Settings()


# --------------------------------------------------------------------------- #
# Criterion 7: one answer for every refusal; the token is not a credential     #
# --------------------------------------------------------------------------- #


def test_unknown_expired_and_used_links_get_the_same_answer(
    cli: Callable[..., tuple[int, str, str]], client: TestClient, factory: sessionmaker[Session]
) -> None:
    used = issue(cli)
    assert redeem(client, used).status_code == 302
    with factory() as session:
        expired = token_of(
            sign_in_link.issue(
                session,
                workspace_slug="globex",
                email="first@globex.test",
                public_url="http://localhost:8000",
                ttl_seconds=60,
                now=datetime.now(UTC) - timedelta(hours=1),
            ).url
        )

    answers = [redeem(client, t) for t in ("not-a-link-token", expired, used)]
    answers.append(client.get("/auth/sign-in-link"))

    for resp in answers:
        assert (resp.status_code, resp.json()) == (401, REFUSED)
        assert "set-cookie" not in resp.headers


@pytest.mark.parametrize("path", ["/auth/me", "/auth/api-keys", "/auth/sign-in-link"])
def test_the_link_token_is_not_a_credential(
    cli: Callable[..., tuple[int, str, str]], client: TestClient, path: str
) -> None:
    token = issue(cli)
    for headers in ({"Authorization": f"Bearer {token}"}, {"X-API-Key": token}):
        assert client.get(path, headers=headers).status_code == 401
    # Sent as a credential it consumed nothing: the link still works.
    assert redeem(client, token).status_code == 302


# --------------------------------------------------------------------------- #
# Criterion 8: a new link replaces the old one; an identity provider ends both #
# --------------------------------------------------------------------------- #


def test_a_new_link_invalidates_the_earlier_unredeemed_one(
    cli: Callable[..., tuple[int, str, str]], client: TestClient
) -> None:
    first = issue(cli)
    second = issue(cli)
    assert redeem(client, first).status_code == 401
    assert redeem(client, second).status_code == 302


@pytest.mark.parametrize("protocol", ["oidc", "saml"])
def test_a_link_issued_before_an_identity_provider_was_enabled_is_refused(
    cli: Callable[..., tuple[int, str, str]],
    client: TestClient,
    factory: sessionmaker[Session],
    protocol: str,
) -> None:
    token = issue(cli)
    enable_identity_provider(factory, protocol)
    resp = redeem(client, token)
    assert (resp.status_code, resp.json()) == (401, REFUSED)
    assert "set-cookie" not in resp.headers


def test_a_link_of_an_administrator_deactivated_since_is_refused(
    cli: Callable[..., tuple[int, str, str]], client: TestClient, factory: sessionmaker[Session]
) -> None:
    token = issue(cli)
    with factory() as session:
        session.scalars(select(User)).one().is_active = False
        session.commit()
    assert redeem(client, token).status_code == 401

    # The refusal consumed the link: reactivating the user does not bring it back.
    with factory() as session:
        session.scalars(select(User)).one().is_active = True
        session.commit()
        assert session.scalars(select(SignInLink)).one().used_at is not None
    resp = redeem(client, token)
    assert resp.status_code == 401 and "set-cookie" not in resp.headers


def test_a_link_of_an_administrator_demoted_since_is_refused(
    cli: Callable[..., tuple[int, str, str]], client: TestClient, factory: sessionmaker[Session]
) -> None:
    token = issue(cli)
    with factory() as session:
        session.scalars(select(User)).one().role = UserRole.MEMBER
        session.commit()
    resp = redeem(client, token)
    assert (resp.status_code, resp.json()) == (401, REFUSED)
    assert "set-cookie" not in resp.headers


# --------------------------------------------------------------------------- #
# Criterion 9: audit, logs, and response headers                               #
# --------------------------------------------------------------------------- #


def test_issuing_and_redeeming_each_write_one_audit_entry_and_never_the_token(
    cli: Callable[..., tuple[int, str, str]],
    client: TestClient,
    factory: sessionmaker[Session],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    token = issue(cli)
    assert redeem(client, token).status_code == 302

    with factory() as session:
        user = session.scalars(select(User)).one()
        rows = session.scalars(select(AuditLog)).all()
        dumped = repr(
            [{c.name: getattr(r, c.key) for c in AuditLog.__mapper__.columns} for r in rows]
        )
    actions = [row.action for row in rows]
    assert actions.count("auth.sign_in_link_issued") == 1
    assert actions.count("auth.sign_in_link_redeemed") == 1
    for row in rows:
        if row.action.startswith("auth.sign_in_link_"):
            assert (row.workspace_id, row.target_id) == (WS, user.id)
    # The workspace had no administrator, so this issue created one: the entry says so.
    issued = next(row for row in rows if row.action == "auth.sign_in_link_issued")
    assert (issued.details["user_created"], issued.details["role"]) == (True, "admin")
    assert token not in dumped
    # The test client logs the URL it requested; that record is the client's, not the route's.
    emitted = [r for r in caplog.records if not r.name.startswith(("httpx", "httpcore"))]
    assert emitted, "expected the command or the route to log something"
    for record in emitted:
        assert token not in record.getMessage() and token not in repr(record.args)


def test_a_link_for_an_existing_administrator_is_audited_as_creating_no_one(
    cli: Callable[..., tuple[int, str, str]], factory: sessionmaker[Session]
) -> None:
    add_user(factory, "boss@acme.test")
    issue(cli, "boss@acme.test")
    with factory() as session:
        issued = session.scalars(
            select(AuditLog).where(AuditLog.action == "auth.sign_in_link_issued")
        ).one()
    assert (issued.details["user_created"], issued.details["role"]) == (False, "admin")


@pytest.mark.parametrize("change", ["deactivated", "demoted", "identity_provider_enabled"])
def test_a_consumed_but_refused_link_is_audited_without_the_token(
    cli: Callable[..., tuple[int, str, str]],
    client: TestClient,
    factory: sessionmaker[Session],
    change: str,
) -> None:
    token = issue(cli)
    with factory() as session:
        user = session.scalars(select(User)).one()
        if change == "deactivated":
            user.is_active = False
        elif change == "demoted":
            user.role = UserRole.MEMBER
        session.commit()
    if change == "identity_provider_enabled":
        enable_identity_provider(factory, "oidc")

    assert (redeem(client, token).status_code, redeem(client, token).status_code) == (401, 401)

    with factory() as session:
        rows = session.scalars(select(AuditLog)).all()
        dumped = repr(
            [{c.name: getattr(r, c.key) for c in AuditLog.__mapper__.columns} for r in rows]
        )
    refused = [row for row in rows if row.action == "auth.sign_in_link_refused"]
    # One entry for the redemption that consumed the link; the replay matched no link.
    assert len(refused) == 1
    assert (refused[0].workspace_id, refused[0].target_id, refused[0].result) == (
        WS,
        user.id,
        "failure",
    )
    assert refused[0].details["reason"] == change
    assert not [row for row in rows if row.action == "auth.sign_in_link_redeemed"]
    assert token not in dumped


def test_the_servers_access_log_line_for_a_link_keeps_no_token(
    cli: Callable[..., tuple[int, str, str]],
) -> None:
    token = issue(cli)
    path = f"/api/auth/sign-in-link?next=/board&token={token}"
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        0,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:5000", "GET", path, "1.1", 302),
        None,
    )
    RedactingLogFilter().filter(record)
    assert token not in record.getMessage()
    assert "/api/auth/sign-in-link?next=/board&token=" in record.getMessage()


def test_every_redeem_answer_forbids_caching_and_referrers(
    cli: Callable[..., tuple[int, str, str]], client: TestClient
) -> None:
    token = issue(cli)
    for resp in (redeem(client, token), redeem(client, token)):
        assert resp.headers["referrer-policy"] == "no-referrer"
        assert resp.headers["cache-control"] == "no-store"


# --------------------------------------------------------------------------- #
# Criterion 10: the cookie alone authenticates (#220)                          #
# --------------------------------------------------------------------------- #


def test_the_cookie_the_link_set_alone_gets_200_from_auth_me(
    cli: Callable[..., tuple[int, str, str]], client: TestClient
) -> None:
    token = issue(cli)
    cookie = redeem(client, token).cookies["forge_session"]
    client.cookies.clear()
    resp = client.get("/auth/me", headers={"cookie": f"forge_session={cookie}"})
    assert resp.status_code == 200, resp.text
    assert (resp.json()["workspace_id"], resp.json()["role"]) == (str(WS), "admin")
