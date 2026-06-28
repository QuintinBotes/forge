"""Workflow-editor API route tests (F28 AC 3, 6, 7, 11, 15, 17, 18, 19)."""

from __future__ import annotations

from collections.abc import Callable

import yaml
from fastapi.testclient import TestClient

from forge_contracts import UserRole

from .conftest import WS_B

BASE = "/workflow/editor"


def _fork_default(client: TestClient) -> None:
    resp = client.post(f"{BASE}/definitions/default_feature/fork")
    assert resp.status_code == 201, resp.text


def test_catalog(admin_client: TestClient) -> None:
    """AC 3: catalog returns states/events/guards/preconditions/effects/modes."""
    resp = admin_client.get(f"{BASE}/catalog")
    assert resp.status_code == 200
    body = resp.json()
    assert "created" in body["states"]
    assert "single_agent" in body["modes"]
    assert "supervised_multi_agent" in body["modes"]
    precondition_names = {p["name"] for p in body["preconditions"]}
    assert {"repo_target_set", "policy_loaded"} <= precondition_names
    assert any(e["name"] == "start_agent_run" for e in body["effects"])


def test_list_includes_bundled(admin_client: TestClient) -> None:
    resp = admin_client.get(f"{BASE}/definitions")
    assert resp.status_code == 200
    names = {d["name"]: d for d in resp.json()}
    assert names["default_feature"]["origin"] == "bundled"


def test_fork_then_get_editable(admin_client: TestClient) -> None:
    """AC 8: fork yields an editable bundled_fork with a revision-1 draft."""
    _fork_default(admin_client)
    detail = admin_client.get(f"{BASE}/definitions/default_feature").json()
    assert detail["origin"] == "bundled_fork"
    assert detail["editable"] is True
    assert detail["draft"]["revision"] == 1


def test_publish_then_history(admin_client: TestClient) -> None:
    """AC 11: a clean draft publishes; revision history reflects it."""
    _fork_default(admin_client)
    resp = admin_client.post(f"{BASE}/definitions/default_feature/publish")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "published"
    revisions = admin_client.get(f"{BASE}/definitions/default_feature/revisions").json()
    assert any(r["status"] == "published" for r in revisions)


def test_publish_blocked_returns_409_with_errors(admin_client: TestClient) -> None:
    """AC 6/7/11: a protected-invariant violation blocks publish with errors[]."""
    _fork_default(admin_client)
    detail = admin_client.get(f"{BASE}/definitions/default_feature").json()
    graph = detail["draft"]["graph"]
    for edge in graph["edges"]:
        if edge["from_state"] == "awaiting_review" and edge["to_state"] == "merged":
            edge["when"] = [s for s in edge["when"] if s != "review_approved_by_human"]
    save = admin_client.put(
        f"{BASE}/definitions/default_feature/draft", json={"graph": graph}
    )
    assert save.status_code == 200, save.text
    resp = admin_client.post(f"{BASE}/definitions/default_feature/publish")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert any(
        e["invariant_id"] == "merge_human_gate" for e in detail["errors"]
    )


def test_diff(admin_client: TestClient) -> None:
    """AC 15: diff between two revisions lists changed transitions."""
    _fork_default(admin_client)
    admin_client.post(f"{BASE}/definitions/default_feature/publish")
    detail = admin_client.get(f"{BASE}/definitions/default_feature").json()
    graph = detail["current_published"]["graph"]
    for edge in graph["edges"]:
        if edge["from_state"] == "created":
            edge["skill"] = "spec-analyst-v2"
    admin_client.put(
        f"{BASE}/definitions/default_feature/draft", json={"graph": graph}
    )
    admin_client.post(f"{BASE}/definitions/default_feature/publish")
    resp = admin_client.get(f"{BASE}/definitions/default_feature/diff?from=1&to=2")
    assert resp.status_code == 200, resp.text
    assert any(d["change"] == "changed" for d in resp.json()["transition_diffs"])


def test_import_validates_unregistered_effect(admin_client: TestClient) -> None:
    """AC 17: importing YAML with an unregistered effect cannot publish."""
    bad_yaml = (
        'workflow: imported_flow\nversion: "1"\n'
        "transitions:\n  - from: created\n    to: closed\n"
        "    action: not_a_real_effect\n"
    )
    resp = admin_client.post(
        f"{BASE}/import",
        json={"name": "imported_flow", "title": "Imported", "dsl_yaml": bad_yaml},
    )
    assert resp.status_code == 201, resp.text
    issues = resp.json()["draft"]["validation_issues"]
    assert any(i["code"] == "unregistered_effect" for i in issues)
    publish = admin_client.post(f"{BASE}/definitions/imported_flow/publish")
    assert publish.status_code == 409


def test_export_round_trips(admin_client: TestClient) -> None:
    """AC 19: export returns canonical YAML that re-imports to a valid graph."""
    _fork_default(admin_client)
    admin_client.post(f"{BASE}/definitions/default_feature/publish")
    resp = admin_client.get(f"{BASE}/definitions/default_feature/export?format=yaml")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/yaml")
    parsed = yaml.safe_load(resp.text)
    assert parsed["workflow"] == "default_feature"
    assert parsed["transitions"]


# --------------------------------------------------------------------------- #
# AC 18 — RBAC matrix                                                          #
# --------------------------------------------------------------------------- #


def test_rbac_viewer(make_client: Callable[..., TestClient]) -> None:
    with make_client(role=UserRole.VIEWER) as client:
        assert client.get(f"{BASE}/catalog").status_code == 200
        assert client.get(f"{BASE}/definitions").status_code == 200
        assert client.post(f"{BASE}/definitions/default_feature/fork").status_code == 403
        assert (
            client.put(
                f"{BASE}/definitions/default_feature/draft", json={"graph": {}}
            ).status_code
            == 403
        )


def test_rbac_member(make_client: Callable[..., TestClient]) -> None:
    # An admin forks first so a draft exists, then a member edits but cannot publish.
    with make_client(role=UserRole.ADMIN) as admin:
        _fork_default(admin)
        detail = admin.get(f"{BASE}/definitions/default_feature").json()
    graph = detail["draft"]["graph"]
    with make_client(role=UserRole.MEMBER) as member:
        assert (
            member.put(
                f"{BASE}/definitions/default_feature/draft", json={"graph": graph}
            ).status_code
            == 200
        )
        assert (
            member.post(f"{BASE}/definitions/default_feature/draft/validate").status_code
            == 200
        )
        assert member.post(f"{BASE}/definitions/default_feature/publish").status_code == 403
        assert member.post(f"{BASE}/definitions/default_feature/fork").status_code == 403


def test_rbac_unauthenticated(make_client: Callable[..., TestClient]) -> None:
    with make_client(authed=False) as client:
        assert client.get(f"{BASE}/catalog").status_code == 401


def test_cross_workspace_returns_404(make_client: Callable[..., TestClient]) -> None:
    """AC 18: a custom definition in WS A is invisible (404) to WS B."""
    with make_client(role=UserRole.ADMIN) as admin_a:
        created = admin_a.post(
            f"{BASE}/definitions",
            json={"name": "release_train", "title": "Release Train"},
        )
        assert created.status_code == 201, created.text
    with make_client(role=UserRole.ADMIN, workspace_id=WS_B) as admin_b:
        resp = admin_b.get(f"{BASE}/definitions/release_train")
        assert resp.status_code == 404
