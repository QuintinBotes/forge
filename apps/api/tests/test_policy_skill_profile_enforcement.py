"""F40-POL-GOVERNANCE — ``skill_profiles.allowed`` enforced at run creation (422)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from forge_api.main import create_app


@pytest.fixture
def client(authenticate_app: Callable[..., FastAPI]) -> Iterator[TestClient]:
    app = create_app()
    authenticate_app(app)
    with TestClient(app) as c:
        yield c


def _write_policy(root: Path) -> None:
    (root / ".forge").mkdir(parents=True, exist_ok=True)
    (root / ".forge" / "policy.yaml").write_text(
        "repo_id: demo\n"
        "skill_profiles:\n"
        "  default: backend-tdd\n"
        "  allowed: [backend-tdd, security-review]\n",
        encoding="utf-8",
    )


def _objective(root: Path, profile: str | None) -> dict:
    obj: dict = {"objective": "do work", "context": {"repo_root": str(root)}}
    if profile is not None:
        obj["skill_profile"] = {"name": profile}
    return obj


def test_disallowed_profile_is_rejected_422(client: TestClient, tmp_path: Path) -> None:
    _write_policy(tmp_path)
    resp = client.post("/agent/runs", json=_objective(tmp_path, "yolo-ship-it"))
    assert resp.status_code == 422, resp.text
    assert "yolo-ship-it" in resp.text


def test_allowed_profile_is_admitted(client: TestClient, tmp_path: Path) -> None:
    _write_policy(tmp_path)
    resp = client.post("/agent/runs", json=_objective(tmp_path, "security-review"))
    assert resp.status_code == 201, resp.text


def test_no_repo_root_skips_enforcement(client: TestClient) -> None:
    resp = client.post("/agent/runs", json={"objective": "no policy context"})
    assert resp.status_code == 201, resp.text


def test_default_profile_admitted_when_unspecified(client: TestClient, tmp_path: Path) -> None:
    _write_policy(tmp_path)
    resp = client.post("/agent/runs", json=_objective(tmp_path, None))
    assert resp.status_code == 201, resp.text
