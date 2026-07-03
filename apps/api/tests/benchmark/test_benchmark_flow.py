"""F35 integration tests — submit -> verify -> moderate -> rank (AC6-13, 19, 23)."""

from __future__ import annotations

import uuid

import pytest
from conftest import OTHER_WS_ID, SLUG, VERSION, faithful_submission

from forge_contracts import UserRole

BASE = f"/benchmarks/{SLUG}/{VERSION}"


def _submit(client, payload=None):
    response = client.post(f"{BASE}/submissions", json=payload or faithful_submission())
    assert response.status_code == 201, response.text
    return response.json()


def test_suites_listed_and_detail(make_client) -> None:
    client = make_client(UserRole.MEMBER)
    suites = client.get("/benchmarks").json()
    assert [s["slug"] for s in suites] == [SLUG]
    detail = client.get(BASE).json()
    assert detail["frozen"] is True
    assert detail["task_count"] == 3
    assert detail["content_hash"].startswith("sha256:")


def test_external_ingest_creates_pending_bound_to_hash(make_client) -> None:
    client = make_client(UserRole.MEMBER)
    body = _submit(client)
    assert body["status"] == "pending"
    assert body["visibility"] == "private"
    assert body["verified"] is False
    # The response never carries the private contact field.
    assert "submitter_contact" not in body

    listed = client.get(f"{BASE}/submissions").json()
    assert len(listed) == 1


def test_ingest_rejects_suite_hash_drift_409(make_client) -> None:
    client = make_client(UserRole.MEMBER)
    payload = faithful_submission()
    payload["suite_content_hash"] = "sha256:" + "0" * 64
    response = client.post(f"{BASE}/submissions", json=payload)
    assert response.status_code == 409


def test_ingest_rejects_oversize_413(make_client) -> None:
    client = make_client(UserRole.MEMBER)
    payload = faithful_submission()
    payload["config"]["padding"] = "x" * 100_000  # > 64k service cap
    response = client.post(f"{BASE}/submissions", json=payload)
    assert response.status_code == 413


def test_verify_accepts_faithful_submission(make_client) -> None:
    member = make_client(UserRole.MEMBER)
    admin = make_client(UserRole.ADMIN)
    submission = _submit(member)

    response = admin.post(f"/benchmarks/submissions/{submission['id']}/verify")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "verified"
    assert body["verification"]["verified"] is True
    assert body["verification"]["bundle_hash_matches"] is True
    assert body["verification"]["score_delta"] <= 0.005

    detail = admin.get(f"/benchmarks/submissions/{submission['id']}").json()
    assert detail["verified"] is True
    assert detail["status"] == "verified"


def test_verify_rejects_inflated_claim(make_client) -> None:
    member = make_client(UserRole.MEMBER)
    admin = make_client(UserRole.ADMIN)
    payload = faithful_submission()
    payload["claimed"]["composite"] = 0.999999  # tampered headline number
    submission = _submit(member, payload)

    body = admin.post(f"/benchmarks/submissions/{submission['id']}/verify").json()
    assert body["status"] == "rejected"
    assert body["verification"]["verified"] is False
    assert any("epsilon" in r for r in body["verification"]["reasons"])


def test_verify_rejects_tampered_bundle(make_client) -> None:
    member = make_client(UserRole.MEMBER)
    admin = make_client(UserRole.ADMIN)
    payload = faithful_submission()
    # Mutate recorded outputs after hashing: hash can no longer match.
    payload["bundles"][0]["output_ids"] = ["tampered"]
    submission = _submit(member, payload)

    body = admin.post(f"/benchmarks/submissions/{submission['id']}/verify").json()
    assert body["verification"]["bundle_hash_matches"] is False
    assert body["status"] == "rejected"


def test_publish_requires_verified_unless_forced(make_client) -> None:
    member = make_client(UserRole.MEMBER)
    admin = make_client(UserRole.ADMIN)
    submission = _submit(member)

    denied = admin.post(f"/benchmarks/submissions/{submission['id']}/publish", json={})
    assert denied.status_code == 409

    forced = admin.post(
        f"/benchmarks/submissions/{submission['id']}/publish", json={"force": True}
    )
    assert forced.status_code == 200
    assert forced.json()["visibility"] == "public"


def test_verify_then_publish_then_flag_lifecycle(make_client, service) -> None:
    member = make_client(UserRole.MEMBER)
    admin = make_client(UserRole.ADMIN)
    submission = _submit(member)
    sid = submission["id"]

    admin.post(f"/benchmarks/submissions/{sid}/verify")
    published = admin.post(f"/benchmarks/submissions/{sid}/publish", json={})
    assert published.status_code == 200
    assert published.json()["visibility"] == "public"

    _suite, rows = service.leaderboard(SLUG, VERSION, public_only=True)
    assert [str(r.submission_id) for r in rows] == [sid]

    flagged = admin.post(
        f"/benchmarks/submissions/{sid}/flag", json={"reason": "suspicious outputs"}
    )
    assert flagged.status_code == 200
    assert flagged.json()["status"] == "flagged"

    _suite, rows_after = service.leaderboard(SLUG, VERSION, public_only=True)
    assert rows_after == []


def test_verify_fails_when_on_disk_suite_drifts(make_client, benchmark_root) -> None:
    """AC7: on-disk case drift is detected before any scoring/verification."""
    member = make_client(UserRole.MEMBER)
    admin = make_client(UserRole.ADMIN)
    submission = _submit(member)

    case_file = benchmark_root / SLUG / VERSION / "cases" / "all.yaml"
    case_file.write_text(case_file.read_text().replace("auth.py::refresh", "drifted"))

    response = admin.post(f"/benchmarks/submissions/{submission['id']}/verify")
    assert response.status_code == 409
    # Fail-closed: the submission was not advanced.
    detail = admin.get(f"/benchmarks/submissions/{submission['id']}").json()
    assert detail["status"] == "pending"


def test_config_secret_redacted_on_ingest(make_client, service) -> None:
    """AC15: an injected key is redacted in the stored row itself."""
    member = make_client(UserRole.MEMBER)
    submission = _submit(member)
    row, _suite = service.get_submission(
        uuid.UUID(submission["id"]), workspace_id=None
    )
    stored = str(row.config)
    assert "sk-ant-SECRETSECRET123" not in stored
    assert row.config["api_key"] == "[REDACTED]"


def test_management_actions_emit_audit_events(make_client, audit_log) -> None:
    """AC23: submit/verify/publish/flag each emit exactly one audit event."""
    member = make_client(UserRole.MEMBER)
    admin = make_client(UserRole.ADMIN)
    submission = _submit(member)
    sid = submission["id"]
    admin.post(f"/benchmarks/submissions/{sid}/verify")
    admin.post(f"/benchmarks/submissions/{sid}/publish", json={})
    admin.post(f"/benchmarks/submissions/{sid}/flag", json={"reason": "gaming"})

    actions = [e.action for e in audit_log.store.query()]
    for expected in (
        "benchmark.submitted",
        "benchmark.verified",
        "benchmark.published",
        "benchmark.flagged",
    ):
        assert actions.count(expected) == 1, actions


def test_cross_workspace_submission_is_404(make_client) -> None:
    member = make_client(UserRole.MEMBER)
    submission = _submit(member)
    stranger = make_client(UserRole.ADMIN, workspace_id=OTHER_WS_ID)
    response = stranger.get(f"/benchmarks/submissions/{submission['id']}")
    assert response.status_code == 404


def test_unknown_suite_404(make_client) -> None:
    client = make_client(UserRole.MEMBER)
    assert client.get("/benchmarks/nope/9.9.9").status_code == 404
    response = client.post(
        "/benchmarks/nope/9.9.9/submissions", json=faithful_submission()
    )
    assert response.status_code == 404


@pytest.mark.parametrize("bad_status", ["pending"])
def test_unverified_never_on_public_board(make_client, service, bad_status) -> None:
    member = make_client(UserRole.MEMBER)
    _submit(member)
    _suite, rows = service.leaderboard(SLUG, VERSION, public_only=True)
    assert rows == []
