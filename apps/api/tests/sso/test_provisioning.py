"""Unit tests for JIT provision / link / deprovision + break-glass (AC3, AC15, AC17)."""

from __future__ import annotations

import uuid

import pytest
from conftest import ADMIN_ID, WS_ID, Keypair, install_config
from sqlalchemy import select

from forge_api.sso.errors import LastAdminError, SsoConfigError
from forge_api.sso.provisioning import (
    deprovision_user,
    link_or_jit_provision,
)
from forge_contracts.sso import MappedIdentity
from forge_db.models import AuditLog, ExternalIdentity, User
from forge_db.models.enums import UserRole


def _identity(email: str = "dana@acme.com", role: str = "member", **kw) -> MappedIdentity:
    return MappedIdentity(email=email, role=role, external_id=kw.pop("external_id", email), **kw)


@pytest.fixture
def config(session_factory, idp_keypair: Keypair):
    return install_config(session_factory, idp_keypair)


class TestLinkOrJitProvision:
    def test_jit_creates_user_with_role_and_identity(self, session_factory, config):
        with session_factory() as session:
            user = link_or_jit_provision(
                session=session, config=config, identity=_identity(role="member")
            )
            session.commit()
            assert user.email == "dana@acme.com"
            assert user.role == UserRole.MEMBER
            link = session.execute(
                select(ExternalIdentity).where(ExternalIdentity.user_id == user.id)
            ).scalar_one()
            assert link.provider.value == "saml"
            assert link.external_id == "dana@acme.com"
            # JIT provision emits an audit event.
            actions = [
                a
                for (a,) in session.execute(
                    select(AuditLog.action).where(AuditLog.workspace_id == WS_ID)
                ).all()
            ]
            assert "sso.user_provisioned" in actions

    def test_second_login_links_no_duplicate(self, session_factory, config):
        with session_factory() as session:
            first = link_or_jit_provision(
                session=session, config=config, identity=_identity()
            )
            session.commit()
            first_id = first.id
        with session_factory() as session:
            again = link_or_jit_provision(
                session=session, config=config, identity=_identity()
            )
            session.commit()
            assert again.id == first_id
            count = len(
                session.execute(
                    select(User).where(User.email == "dana@acme.com")
                ).all()
            )
            assert count == 1
            link = session.execute(
                select(ExternalIdentity).where(ExternalIdentity.user_id == first_id)
            ).scalar_one()
            assert link.last_login_at is not None

    def test_links_existing_user_by_email(self, session_factory, config):
        with session_factory() as session:
            user = link_or_jit_provision(
                session=session,
                config=config,
                identity=_identity(email="member@acme.test", external_id="member@acme.test"),
            )
            session.commit()
            assert user.email == "member@acme.test"
            assert user.id is not None
            link = session.execute(
                select(ExternalIdentity).where(ExternalIdentity.user_id == user.id)
            ).scalar_one()
            assert link.external_id == "member@acme.test"

    def test_jit_disabled_raises(self, session_factory, idp_keypair):
        config = install_config(session_factory, idp_keypair, jit_provisioning=False)
        with session_factory() as session, pytest.raises(SsoConfigError):
            link_or_jit_provision(
                session=session, config=config, identity=_identity(email="new@acme.com")
            )

    def test_role_updated_on_login(self, session_factory, config):
        with session_factory() as session:
            link_or_jit_provision(session=session, config=config, identity=_identity())
            session.commit()
        with session_factory() as session:
            user = link_or_jit_provision(
                session=session, config=config, identity=_identity(role="viewer")
            )
            session.commit()
            assert user.role == UserRole.VIEWER

    def test_deactivated_user_cannot_login(self, session_factory, config):
        with session_factory() as session:
            user = link_or_jit_provision(
                session=session, config=config, identity=_identity()
            )
            session.commit()
            user_id = user.id
        with session_factory() as session:
            deprovision_user(
                session=session,
                workspace_id=WS_ID,
                user_id=user_id,
                revoke_sessions=lambda ws, uid: 0,
            )
            session.commit()
        with session_factory() as session, pytest.raises(SsoConfigError):
            link_or_jit_provision(session=session, config=config, identity=_identity())


class TestDeprovision:
    def test_deprovision_revokes_and_audits(self, session_factory, config):
        calls: list[tuple[uuid.UUID, uuid.UUID]] = []

        def revoker(ws: uuid.UUID, uid: uuid.UUID) -> int:
            calls.append((ws, uid))
            return 3

        with session_factory() as session:
            user = link_or_jit_provision(
                session=session, config=config, identity=_identity()
            )
            session.commit()
            user_id = user.id
        with session_factory() as session:
            deprovision_user(
                session=session,
                workspace_id=WS_ID,
                user_id=user_id,
                revoke_sessions=revoker,
            )
            session.commit()
        assert calls == [(WS_ID, user_id)]
        with session_factory() as session:
            user = session.get(User, user_id)
            assert user.is_active is False
            assert user.deactivated_at is not None
            actions = [
                a
                for (a,) in session.execute(
                    select(AuditLog.action).where(AuditLog.workspace_id == WS_ID)
                ).all()
            ]
            assert "sso.user_deprovisioned" in actions

    def test_last_local_admin_protected(self, session_factory, config):
        """AC17: deprovisioning the last active local admin is rejected."""
        with session_factory() as session, pytest.raises(LastAdminError):
            deprovision_user(
                session=session,
                workspace_id=WS_ID,
                user_id=ADMIN_ID,
                revoke_sessions=lambda ws, uid: 0,
            )

    def test_second_admin_can_be_deprovisioned(self, session_factory, config):
        with session_factory() as session:
            extra = User(
                workspace_id=WS_ID,
                email="admin2@acme.test",
                role=UserRole.ADMIN,
            )
            session.add(extra)
            session.commit()
            extra_id = extra.id
        with session_factory() as session:
            deprovision_user(
                session=session,
                workspace_id=WS_ID,
                user_id=extra_id,
                revoke_sessions=lambda ws, uid: 0,
            )
            session.commit()
            assert session.get(User, extra_id).is_active is False
