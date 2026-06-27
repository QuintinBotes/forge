"""RBAC matrix for the automations router (AC16)."""

from __future__ import annotations

from forge_contracts import UserRole

from .conftest import close_spec_rule_body


def _create_as(client, project_id):
    return client.post(f"/projects/{project_id}/automations", json=close_spec_rule_body())


def test_viewer_is_read_only(client_factory, project_id) -> None:
    admin = client_factory(role=UserRole.ADMIN)
    rid = _create_as(admin, project_id).json()["id"]

    viewer = client_factory(role=UserRole.VIEWER)
    # Reads: allowed.
    assert viewer.get(f"/projects/{project_id}/automations").status_code == 200
    assert viewer.get(f"/automations/{rid}").status_code == 200
    assert viewer.get(f"/automations/{rid}/executions").status_code == 200
    # Writes: forbidden.
    assert _create_as(viewer, project_id).status_code == 403
    assert viewer.patch(f"/automations/{rid}", json={"version": 1, "name": "x"}).status_code == 403
    assert viewer.post(f"/automations/{rid}/disable").status_code == 403
    assert viewer.delete(f"/automations/{rid}").status_code == 403


def test_member_can_author_but_not_delete(client_factory, project_id) -> None:
    member = client_factory(role=UserRole.MEMBER)
    rid = _create_as(member, project_id).json()["id"]
    assert member.post(f"/automations/{rid}/disable").status_code == 200
    # Delete is admin-only.
    assert member.delete(f"/automations/{rid}").status_code == 403


def test_agent_runner_cannot_author(client_factory, project_id) -> None:
    agent = client_factory(role=UserRole.AGENT_RUNNER)
    assert _create_as(agent, project_id).status_code == 403


def test_admin_can_delete(client_factory, project_id) -> None:
    admin = client_factory(role=UserRole.ADMIN)
    rid = _create_as(admin, project_id).json()["id"]
    assert admin.delete(f"/automations/{rid}").status_code == 204
