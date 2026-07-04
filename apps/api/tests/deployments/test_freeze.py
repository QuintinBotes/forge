"""Freeze window blocks + admin override (AC14)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from conftest import (
    APPROVER_ID,
    MEMBER_ID,
    REPO_ID,
    WS_ID,
    FakeGitHub,
    FakePolicy,
)
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from forge_api.deps import Principal, get_current_principal
from forge_api.main import create_app
from forge_api.routers.deployments import get_deployment_service
from forge_api.services.deployment_service import DeploymentService
from forge_contracts import UserRole
from forge_contracts.dtos import DeployRules
from forge_deploy.freeze import FakeClock

SATURDAY = FakeClock(datetime(2026, 6, 27, 12, 0, tzinfo=UTC))
FREEZE = {
    "start_day": 4,
    "start_time": "17:00",
    "end_day": 0,
    "end_time": "09:00",
    "reason": "weekend",
}


def _frozen_body() -> dict:
    return {
        "repo_id": REPO_ID,
        "enabled": True,
        "version": 1,
        "environments": [
            {
                "name": "dev",
                "rank": 0,
                "requires_approval": False,
                "gate_config": {"required_checks": ["ci_green"]},
                "provider_config": {"provider": "null"},
                "health_check": {"kind": "none"},
            },
            {
                "name": "staging",
                "rank": 1,
                "gate_config": {"required_checks": ["ci_green"], "min_approvals": 1},
                "provider_config": {"provider": "null"},
                "health_check": {"kind": "none"},
            },
            {
                "name": "production",
                "rank": 2,
                "gate_config": {
                    "required_checks": ["ci_green"],
                    "min_approvals": 1,
                    "freeze_windows": [FREEZE],
                },
                "provider_config": {"provider": "null"},
                "health_check": {"kind": "none"},
            },
        ],
    }


def _client(factory: sessionmaker[Session], svc: DeploymentService, role, user_id):
    app = create_app()
    principal = Principal(
        user_id=user_id,
        workspace_id=WS_ID,
        role=role,
        email="x@f.local",
        auth_method="test",
        scopes=["*"],
    )
    app.dependency_overrides[get_current_principal] = lambda: principal
    app.dependency_overrides[get_deployment_service] = lambda: svc
    return TestClient(app)


def test_freeze_blocks_then_admin_override_clears(
    session_factory: sessionmaker[Session], project_id: uuid.UUID
) -> None:
    rules = DeployRules(
        allow_agent_deploy=False,
        environments=["dev"],
        restricted_environments=["staging", "production"],
    )
    svc = DeploymentService(
        session_factory=session_factory,
        policy_reader=FakePolicy(rules),
        ci_reader=FakeGitHub("success"),
        clock=SATURDAY,
    )
    admin = _client(session_factory, svc, UserRole.ADMIN, MEMBER_ID)
    member = _client(session_factory, svc, UserRole.MEMBER, MEMBER_ID)
    approver = _client(session_factory, svc, UserRole.MEMBER, APPROVER_ID)

    admin.put(f"/projects/{project_id}/pipeline", json=_frozen_body())
    member.post(
        f"/projects/{project_id}/deployments",
        json={"environment": "dev", "commit_sha": "abc123"},
    )
    staging = member.post(
        f"/projects/{project_id}/deployments",
        json={"environment": "staging", "commit_sha": "abc123"},
    ).json()
    approver.post(f"/deployments/{staging['id']}/decision", json={"decision": "approve"})

    # Production during the weekend freeze -> gate_rejected.
    prod = member.post(
        f"/projects/{project_id}/deployments",
        json={"environment": "production", "commit_sha": "abc123"},
    ).json()
    assert prod["state"] == "gate_rejected"

    # Non-admin override is forbidden.
    assert (
        member.post(
            f"/deployments/{prod['id']}/freeze-override", json={"reason": "hotfix"}
        ).status_code
        == 403
    )

    # Admin override creates a fresh, override-flagged deployment that clears
    # not_frozen and proceeds to the approval gate.
    resp = admin.post(
        f"/deployments/{prod['id']}/freeze-override", json={"reason": "hotfix"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["state"] == "awaiting_approval"
