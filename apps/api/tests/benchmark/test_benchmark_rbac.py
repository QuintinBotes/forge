"""F35 RBAC matrix on the management routes (AC18)."""

from __future__ import annotations

import pytest
from conftest import SLUG, VERSION, faithful_submission

from forge_contracts import UserRole

BASE = f"/benchmarks/{SLUG}/{VERSION}"


@pytest.fixture
def submission_id(make_client) -> str:
    member = make_client(UserRole.MEMBER)
    created = member.post(f"{BASE}/submissions", json=faithful_submission())
    assert created.status_code == 201
    return created.json()["id"]


@pytest.mark.parametrize(
    "role", [UserRole.ADMIN, UserRole.MEMBER, UserRole.VIEWER, UserRole.AGENT_RUNNER]
)
def test_every_role_can_read(make_client, role) -> None:
    client = make_client(role)
    assert client.get("/benchmarks").status_code == 200
    assert client.get(BASE).status_code == 200
    assert client.get(f"{BASE}/submissions").status_code == 200


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        (UserRole.ADMIN, 201),
        (UserRole.MEMBER, 201),
        (UserRole.VIEWER, 403),
        (UserRole.AGENT_RUNNER, 403),  # agent-runner is read-only on benchmarks
    ],
)
def test_submit_requires_write(make_client, role, expected) -> None:
    client = make_client(role)
    response = client.post(f"{BASE}/submissions", json=faithful_submission())
    assert response.status_code == expected


@pytest.mark.parametrize(
    "role", [UserRole.MEMBER, UserRole.VIEWER, UserRole.AGENT_RUNNER]
)
@pytest.mark.parametrize("action", ["verify", "publish", "flag"])
def test_moderation_is_admin_only(make_client, submission_id, role, action) -> None:
    client = make_client(role)
    body = {"reason": "x"} if action == "flag" else {}
    response = client.post(f"/benchmarks/submissions/{submission_id}/{action}", json=body)
    assert response.status_code == 403


def test_admin_can_moderate(make_client, submission_id) -> None:
    admin = make_client(UserRole.ADMIN)
    assert admin.post(f"/benchmarks/submissions/{submission_id}/verify").status_code == 200
    assert (
        admin.post(f"/benchmarks/submissions/{submission_id}/publish", json={}).status_code
        == 200
    )
    assert (
        admin.post(
            f"/benchmarks/submissions/{submission_id}/flag", json={"reason": "r"}
        ).status_code
        == 200
    )


def test_unauthenticated_requests_rejected_401(make_client, test_settings) -> None:
    anon = make_client(None)  # real auth dependency: no credentials -> 401
    assert anon.get("/benchmarks").status_code == 401
    assert anon.post(f"{BASE}/submissions", json=faithful_submission()).status_code == 401
    zero_id = "00000000-0000-0000-0000-000000000000"
    assert anon.post(f"/benchmarks/submissions/{zero_id}/verify").status_code == 401
