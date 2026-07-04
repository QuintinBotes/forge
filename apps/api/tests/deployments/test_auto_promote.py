"""Auto-promote-on-merge (AC21)."""

from __future__ import annotations

import uuid
from collections.abc import Callable

from conftest import REPO_ID, WS_ID, configure_pipeline
from fastapi.testclient import TestClient

from forge_contracts import UserRole
from forge_deploy.states import DeploymentTrigger


def test_auto_promote_creates_deployment(
    client_factory: Callable[..., TestClient],
    service,
    project_id: uuid.UUID,
) -> None:
    admin = client_factory(UserRole.ADMIN)
    # dev has auto_promote_on_merge in the body below.
    body = {
        "repo_id": REPO_ID,
        "enabled": True,
        "version": 1,
        "environments": [
            {
                "name": "dev",
                "rank": 0,
                "requires_approval": False,
                "gate_config": {
                    "required_checks": ["ci_green"],
                    "auto_promote_on_merge": True,
                },
                "provider_config": {"provider": "null"},
                "health_check": {"kind": "none"},
            },
        ],
    }
    assert admin.put(f"/projects/{project_id}/pipeline", json=body).status_code == 200

    dto = service.auto_promote_on_merge(
        ws=WS_ID, project_id=project_id, repo_id=REPO_ID, commit_sha="abc123"
    )
    assert dto is not None
    assert dto.trigger == DeploymentTrigger.AUTO_PROMOTE
    assert dto.commit_sha == "abc123"


def test_no_auto_promote_when_disabled(
    client_factory: Callable[..., TestClient],
    service,
    project_id: uuid.UUID,
) -> None:
    admin = client_factory(UserRole.ADMIN)
    configure_pipeline(admin, project_id)  # dev has no auto_promote_on_merge
    dto = service.auto_promote_on_merge(
        ws=WS_ID, project_id=project_id, repo_id=REPO_ID, commit_sha="abc123"
    )
    assert dto is None
