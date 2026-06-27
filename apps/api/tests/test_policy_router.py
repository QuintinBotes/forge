"""Integration tests for the policy router (Phase 2 Task 2.1 wires ``/policy/*``).

Exercises the real handlers wired to a :class:`~forge_policy.RepoPolicyEvaluator`:
loading ``.forge/policy.yaml`` from a repo root and evaluating a tool call against
an inline or loaded policy. Errors map: missing policy -> 404; no policy/repo
-> 422.
"""

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
        "name: Demo\n"
        "write_rules:\n"
        "  allow: ['app/**']\n"
        "  deny: ['secrets/**']\n"
        "allowed_actions: ['read_file']\n",
        encoding="utf-8",
    )


def test_load_policy_from_repo_root(client: TestClient, tmp_path: Path) -> None:
    _write_policy(tmp_path)
    resp = client.get("/policy", params={"repo_root": str(tmp_path)})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["repo_id"] == "demo"
    assert body["write_rules"]["allow"] == ["app/**"]


def test_load_policy_missing_is_404(client: TestClient, tmp_path: Path) -> None:
    resp = client.get("/policy", params={"repo_root": str(tmp_path)})
    assert resp.status_code == 404


def test_evaluate_with_inline_policy_allows_app_write(client: TestClient) -> None:
    payload = {
        "action": {"tool": "write_file", "action": "write_file", "path": "app/main.py"},
        "policy": {
            "repo_id": "demo",
            "write_rules": {"allow": ["app/**"], "deny": ["secrets/**"]},
        },
    }
    resp = client.post("/policy/evaluate", json=payload)
    assert resp.status_code == 200, resp.text
    assert resp.json()["effect"] == "allow"


def test_evaluate_denies_secrets_write(client: TestClient) -> None:
    payload = {
        "action": {"tool": "write_file", "action": "write_file", "path": "secrets/x"},
        "policy": {
            "repo_id": "demo",
            "write_rules": {"allow": ["app/**"], "deny": ["secrets/**"]},
        },
    }
    resp = client.post("/policy/evaluate", json=payload)
    assert resp.status_code == 200, resp.text
    assert resp.json()["effect"] == "deny"


def test_evaluate_loads_policy_from_repo_root(client: TestClient, tmp_path: Path) -> None:
    _write_policy(tmp_path)
    payload = {
        "action": {"tool": "write_file", "action": "write_file", "path": "secrets/x"},
        "repo_root": str(tmp_path),
    }
    resp = client.post("/policy/evaluate", json=payload)
    assert resp.status_code == 200, resp.text
    assert resp.json()["effect"] == "deny"


def test_evaluate_without_policy_or_repo_is_422(client: TestClient) -> None:
    payload = {"action": {"tool": "write_file", "action": "write_file", "path": "app/x"}}
    resp = client.post("/policy/evaluate", json=payload)
    assert resp.status_code == 422
