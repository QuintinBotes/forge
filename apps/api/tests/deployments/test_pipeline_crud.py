"""Pipeline CRUD + policy binding (AC1, AC2, AC3)."""

from __future__ import annotations

import uuid
from collections.abc import Callable

from conftest import configure_pipeline, pipeline_body
from fastapi.testclient import TestClient

from forge_contracts import UserRole


def test_upsert_persists_ordered_and_restricted(
    client_factory: Callable[..., TestClient], project_id: uuid.UUID
) -> None:
    admin = client_factory(UserRole.ADMIN)
    resp = configure_pipeline(admin, project_id)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    envs = {e["name"]: e for e in data["environments"]}
    assert [e["name"] for e in data["environments"]] == ["dev", "staging", "production"]
    assert envs["dev"]["is_restricted"] is False
    assert envs["staging"]["is_restricted"] is True
    assert envs["production"]["is_restricted"] is True
    assert envs["production"]["requires_approval"] is True


def test_restricted_unset_rejected_422(
    client_factory: Callable[..., TestClient], project_id: uuid.UUID
) -> None:
    admin = client_factory(UserRole.ADMIN)
    body = pipeline_body()
    body["environments"][2]["is_restricted"] = False  # production is policy-restricted
    resp = admin.put(f"/projects/{project_id}/pipeline", json=body)
    assert resp.status_code == 422


def test_unknown_env_rejected_422(
    client_factory: Callable[..., TestClient], project_id: uuid.UUID
) -> None:
    admin = client_factory(UserRole.ADMIN)
    body = pipeline_body()
    body["environments"].append(
        {
            "name": "qa",
            "rank": 3,
            "provider_config": {"provider": "null"},
            "health_check": {"kind": "none"},
        }
    )
    resp = admin.put(f"/projects/{project_id}/pipeline", json=body)
    assert resp.status_code == 422


def test_optimistic_version_conflict_409(
    client_factory: Callable[..., TestClient], project_id: uuid.UUID
) -> None:
    admin = client_factory(UserRole.ADMIN)
    assert configure_pipeline(admin, project_id, version=1).status_code == 200
    # First edit at version 1 -> 200 (bumps to 2). Second stale edit at 1 -> 409.
    assert configure_pipeline(admin, project_id, version=1).status_code == 200
    assert configure_pipeline(admin, project_id, version=1).status_code == 409


def test_get_pipeline_roundtrip(
    client_factory: Callable[..., TestClient], project_id: uuid.UUID
) -> None:
    admin = client_factory(UserRole.ADMIN)
    configure_pipeline(admin, project_id)
    resp = admin.get(f"/projects/{project_id}/pipeline")
    assert resp.status_code == 200
    assert resp.json()["repo_id"] == "github.com/org/api"
