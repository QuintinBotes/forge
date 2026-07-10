"""F40-POL-GOVERNANCE — the ``POST /policy/static-gate`` verification check."""

from __future__ import annotations

from collections.abc import Callable, Iterator

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


def test_clean_scan_passes(client: TestClient) -> None:
    resp = client.post(
        "/policy/static-gate",
        json={"files": {"a.py": "print('ok')\n"}, "forbidden_shortcuts": ["# type: ignore"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["passed"] is True
    assert body["violations"] == []


def test_forbidden_shortcut_fails_check_422(client: TestClient) -> None:
    resp = client.post(
        "/policy/static-gate",
        json={
            "files": {"a.py": "x = 1  # type: ignore\n"},
            "forbidden_shortcuts": ["# type: ignore"],
        },
    )
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["passed"] is False
    assert detail["violations"][0]["file"] == "a.py"
    assert detail["violations"][0]["line"] == 1
