"""F37 OAuth provisioning tests (AC9, AC10).

Hermetic in-memory SQLite (same substrate the worker suites use): first
sign-in provisions Workspace + admin User + oauth_account idempotently; later
users in the same workspace are members; multi-provider linking resolves both
identities to one user; unlinking the last login method is refused; every
mutation writes exactly one audit row on the same session (fail-closed).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import StaticPool, create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from forge_api.services.auth_provisioning import (
    LastLoginMethodError,
    OAuthProvisioningService,
    workspace_slug_for_email,
)
from forge_contracts.auth import OAuthProvider
from forge_db.base import Base
from forge_db.models import AuditLog, OAuthAccount, User, UserRole, Workspace


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    sf: sessionmaker[Session] = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with sf() as s:
        yield s


@pytest.fixture
def service(session: Session) -> OAuthProvisioningService:
    return OAuthProvisioningService(session)


def _actions(session: Session) -> list[str]:
    return [e.action for e in session.scalars(select(AuditLog)).all()]


def test_first_login_provisions_workspace_admin(
    session: Session, service: OAuthProvisioningService
) -> None:
    user = service.provision_from_oauth(
        provider=OAuthProvider.GITHUB, subject="gh-1", email="alice@acme.dev", name="Alice"
    )
    session.commit()

    assert user.role is UserRole.ADMIN  # first user in a fresh workspace
    assert user.auth_provider == "github" and user.auth_subject == "gh-1"
    workspace = session.get(Workspace, user.workspace_id)
    assert workspace.slug == "acme"
    accounts = session.scalars(select(OAuthAccount)).all()
    assert len(accounts) == 1 and accounts[0].user_id == user.id
    assert _actions(session) == ["auth.user_provisioned"]


def test_provisioning_is_idempotent(session: Session, service: OAuthProvisioningService) -> None:
    first = service.provision_from_oauth(
        provider=OAuthProvider.GITHUB, subject="gh-1", email="alice@acme.dev"
    )
    session.commit()
    again = service.provision_from_oauth(
        provider=OAuthProvider.GITHUB, subject="gh-1", email="alice@acme.dev"
    )
    session.commit()

    assert again.id == first.id
    assert len(session.scalars(select(User)).all()) == 1
    assert len(session.scalars(select(OAuthAccount)).all()) == 1
    assert _actions(session) == ["auth.user_provisioned"]  # no second event


def test_second_user_same_domain_is_member(
    session: Session, service: OAuthProvisioningService
) -> None:
    admin = service.provision_from_oauth(
        provider=OAuthProvider.GITHUB, subject="gh-1", email="alice@acme.dev"
    )
    member = service.provision_from_oauth(
        provider=OAuthProvider.GITLAB, subject="gl-2", email="bob@acme.dev"
    )
    session.commit()

    assert member.workspace_id == admin.workspace_id  # same domain ⇒ same workspace
    assert admin.role is UserRole.ADMIN and member.role is UserRole.MEMBER


def test_multi_provider_link_resolves_same_user(
    session: Session, service: OAuthProvisioningService
) -> None:
    user = service.provision_from_oauth(
        provider=OAuthProvider.GITHUB, subject="gh-1", email="alice@acme.dev"
    )
    service.link_oauth(
        user=user, provider=OAuthProvider.GOOGLE, subject="goog-9", email="alice@acme.dev"
    )
    session.commit()

    via_google = service.provision_from_oauth(
        provider=OAuthProvider.GOOGLE, subject="goog-9", email="alice@acme.dev"
    )
    assert via_google.id == user.id
    assert "auth.oauth_linked" in _actions(session)


def test_unlink_last_login_method_refused(
    session: Session, service: OAuthProvisioningService
) -> None:
    user = service.provision_from_oauth(
        provider=OAuthProvider.GITHUB, subject="gh-1", email="alice@acme.dev"
    )
    session.commit()
    only_account = session.scalars(select(OAuthAccount)).one()

    with pytest.raises(LastLoginMethodError):
        service.unlink_oauth(user=user, account_id=only_account.id)

    # With a second method linked, the first can be removed — and is audited.
    second = service.link_oauth(user=user, provider=OAuthProvider.GITLAB, subject="gl-7")
    service.unlink_oauth(user=user, account_id=only_account.id)
    session.commit()
    remaining = session.scalars(select(OAuthAccount)).all()
    assert [a.id for a in remaining] == [second.id]
    assert _actions(session).count("auth.oauth_unlinked") == 1


def test_unlink_foreign_account_not_found(
    session: Session, service: OAuthProvisioningService
) -> None:
    alice = service.provision_from_oauth(
        provider=OAuthProvider.GITHUB, subject="gh-1", email="alice@acme.dev"
    )
    service.provision_from_oauth(
        provider=OAuthProvider.GITHUB, subject="gh-2", email="carol@other.io"
    )
    session.commit()
    carol_account = session.scalars(
        select(OAuthAccount).where(OAuthAccount.provider_subject == "gh-2")
    ).one()

    with pytest.raises(LookupError):
        service.unlink_oauth(user=alice, account_id=carol_account.id)
    with pytest.raises(LookupError):
        service.unlink_oauth(user=alice, account_id=uuid.uuid4())


def test_provision_without_email_fails_closed(
    session: Session, service: OAuthProvisioningService
) -> None:
    with pytest.raises(ValueError, match="email"):
        service.provision_from_oauth(provider=OAuthProvider.GITHUB, subject="gh-x", email=None)


@pytest.mark.parametrize(
    ("email", "slug"),
    [
        ("alice@acme.dev", "acme"),
        ("bob@My-Startup.io", "my-startup"),
        (None, "workspace"),
        ("noatsign", "workspace"),
    ],
)
def test_workspace_slug_for_email(email: str | None, slug: str) -> None:
    assert workspace_slug_for_email(email) == slug
