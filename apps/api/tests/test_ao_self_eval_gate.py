"""Self-Eval Gate enforcement at the AO config-change endpoints (A1).

Real handlers over a real Postgres session, with a Self-Eval Gate injected via
DI (an in-memory eval runner + baseline) and enforcement toggled through the
settings flag. Proves a regressing config is refused (409, not applied, audited),
a passing one is allowed, ``force`` overrides + audits, and the whole thing
no-ops when the flag is off or on cold start.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from forge_api.db import get_db
from forge_api.deps import Principal
from forge_api.main import create_app
from forge_api.routers import ao_settings as ao_module
from forge_api.services.self_eval_gate import get_self_eval_gate
from forge_api.settings import Settings
from forge_contracts import UserRole
from forge_db.base import Base
from forge_db.models import AuditLog, User, Workspace
from forge_eval.sweval import SelfEvalGate, SelfEvalScorecard

pytestmark = pytest.mark.usefixtures("pg_engine")

_BODY = {"model_or_tier": "senior", "effort": "max"}


@pytest.fixture
def factory(pg_engine) -> Iterator[sessionmaker[Session]]:
    Base.metadata.create_all(pg_engine)
    try:
        yield sessionmaker(bind=pg_engine, expire_on_commit=False, class_=Session)
    finally:
        Base.metadata.drop_all(pg_engine)


def _principal(workspace_id: uuid.UUID, user_id: uuid.UUID) -> Principal:
    return Principal(
        user_id=user_id,
        workspace_id=workspace_id,
        role=UserRole.ADMIN,
        email="admin@forge.local",
        auth_method="test",
        scopes=["*"],
    )


def _gate(*, rate: float | None, baseline: float | None) -> SelfEvalGate:
    async def runner(_ws: uuid.UUID, _cfg: object) -> SelfEvalScorecard | None:
        if rate is None:
            return None
        return SelfEvalScorecard(total=10, resolved=round(rate * 10), resolution_rate=rate)

    return SelfEvalGate(eval_runner=runner, baseline_for=lambda _ws: baseline)


@pytest.fixture
def client_for(
    factory: sessionmaker[Session],
    authenticate_app: Callable[..., FastAPI],
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[..., TestClient]:
    with factory() as session:
        ws = Workspace(name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}")
        session.add(ws)
        session.flush()
        workspace_id = ws.id
        admin = User(workspace_id=workspace_id, email="admin@forge.local", role=UserRole.ADMIN)
        session.add(admin)
        session.flush()
        user_id = admin.id
        session.commit()

    def _make(*, gate: SelfEvalGate, enforce: bool = True) -> TestClient:
        monkeypatch.setattr(
            ao_module, "get_app_settings", lambda: Settings(self_eval_enforce=enforce)
        )
        app = create_app()
        authenticate_app(app, _principal(workspace_id, user_id))

        def _get_db() -> Iterator[Session]:
            with factory() as session:
                yield session

        app.dependency_overrides[get_db] = _get_db
        app.dependency_overrides[get_self_eval_gate] = lambda: gate
        client = TestClient(app)
        client.workspace_id = workspace_id  # type: ignore[attr-defined]
        return client

    return _make


def _audit_actions(factory: sessionmaker[Session], workspace_id: uuid.UUID) -> list[str]:
    with factory() as session:
        return list(
            session.scalars(
                select(AuditLog.action).where(AuditLog.workspace_id == workspace_id)
            ).all()
        )


def test_regressing_config_is_blocked_409_and_not_applied(client_for, factory) -> None:
    client = client_for(gate=_gate(rate=0.5, baseline=0.9))
    resp = client.put("/ao/role-config/coder", json=_BODY)
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "self_eval_regression"

    # The override was never applied — the role still resolves to its default.
    coder = next(c for c in client.get("/ao/role-config").json()["items"] if c["role"] == "coder")
    assert coder["source"] == "default"
    assert "ao.config.self_eval_blocked" in _audit_actions(factory, client.workspace_id)


def test_passing_config_is_allowed(client_for) -> None:
    client = client_for(gate=_gate(rate=0.95, baseline=0.9))
    resp = client.put("/ao/role-config/coder", json=_BODY)
    assert resp.status_code == 200
    assert resp.json()["source"] == "workspace"


def test_force_overrides_regression_and_audits(client_for, factory) -> None:
    client = client_for(gate=_gate(rate=0.5, baseline=0.9))
    resp = client.put("/ao/role-config/coder?force=true", json=_BODY)
    assert resp.status_code == 200
    assert resp.json()["source"] == "workspace"  # applied despite the regression
    assert "ao.config.self_eval_forced" in _audit_actions(factory, client.workspace_id)


def test_enforcement_off_skips_the_gate(client_for) -> None:
    # A regressing gate, but the flag is off — the change goes through untouched.
    client = client_for(gate=_gate(rate=0.1, baseline=0.9), enforce=False)
    assert client.put("/ao/role-config/coder", json=_BODY).status_code == 200


def test_cold_start_no_baseline_allows(client_for) -> None:
    client = client_for(gate=_gate(rate=0.1, baseline=None))
    assert client.put("/ao/role-config/coder", json=_BODY).status_code == 200


def test_settings_update_is_gated_too(client_for) -> None:
    client = client_for(gate=_gate(rate=0.5, baseline=0.9))
    resp = client.put("/ao/settings", json={"auto_route": False})
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "self_eval_regression"
