"""F35 public-surface tests — unauth reads, privacy, rate limit (AC14-17, 20)."""

from __future__ import annotations

import pytest
from conftest import SLUG, VERSION, WS_ID, faithful_submission

from forge_api.routers import public_leaderboard as public_module
from forge_api.settings import Settings
from forge_contracts import UserRole

BOARD = f"/public/leaderboard/{SLUG}/{VERSION}"


def _published_submission(make_client) -> str:
    member = make_client(UserRole.MEMBER)
    admin = make_client(UserRole.ADMIN)
    created = member.post(
        f"/benchmarks/{SLUG}/{VERSION}/submissions", json=faithful_submission()
    ).json()
    admin.post(f"/benchmarks/submissions/{created['id']}/verify")
    admin.post(f"/benchmarks/submissions/{created['id']}/publish", json={})
    return created["id"]


def test_public_routes_404_when_disabled(make_client, test_settings) -> None:
    """AC16: with the privacy default (disabled) every /public/* path 404s."""
    disabled = Settings(public_leaderboard_enabled=False)
    client = make_client(None, settings=disabled)
    assert client.get("/public/benchmarks").status_code == 404
    assert client.get(BOARD).status_code == 404


def test_public_leaderboard_unauthenticated_ok_and_cache_header(make_client) -> None:
    """AC14: 200 for an unauthenticated request, ranked, cache-fronted."""
    sid = _published_submission(make_client)
    anon = make_client(None)  # no auth override, no credentials

    suites = anon.get("/public/benchmarks")
    assert suites.status_code == 200
    assert [s["slug"] for s in suites.json()] == [SLUG]

    response = anon.get(BOARD)
    assert response.status_code == 200
    assert response.headers["Cache-Control"].startswith("public, max-age=")
    board = response.json()
    assert board["slug"] == SLUG
    assert board["content_hash"].startswith("sha256:")
    assert len(board["entries"]) == 1
    entry = board["entries"][0]
    assert entry["rank"] == 1
    assert entry["verified"] is True
    assert entry["submission_id"] == sid


def test_public_excludes_private_and_leaks_no_secrets(make_client) -> None:
    """AC15/AC16: private submissions invisible; contact + keys never appear."""
    member = make_client(UserRole.MEMBER)
    member.post(f"/benchmarks/{SLUG}/{VERSION}/submissions", json=faithful_submission())
    sid = _published_submission(make_client)

    anon = make_client(None)
    board = anon.get(BOARD)
    entries = board.json()["entries"]
    assert [e["submission_id"] for e in entries] == [sid]  # private one absent

    detail = anon.get(f"{BOARD}/submissions/{sid}")
    assert detail.status_code == 200
    bundles = anon.get(f"{BOARD}/submissions/{sid}/bundles")
    assert bundles.status_code == 200

    for payload in (board.text, detail.text, bundles.text):
        assert "secret-contact@example.com" not in payload
        assert "sk-ant-SECRETSECRET123" not in payload
        assert "submitter_contact" not in payload


def test_private_submission_detail_404_on_public_surface(make_client) -> None:
    member = make_client(UserRole.MEMBER)
    created = member.post(
        f"/benchmarks/{SLUG}/{VERSION}/submissions", json=faithful_submission()
    ).json()
    anon = make_client(None)
    assert anon.get(f"{BOARD}/submissions/{created['id']}").status_code == 404


def test_public_detail_reproduce_affordance(make_client) -> None:
    """AC20: the detail page ships the exact verify command + bundle download."""
    sid = _published_submission(make_client)
    anon = make_client(None)
    detail = anon.get(f"{BOARD}/submissions/{sid}").json()
    assert "forge-cli bench verify" in detail["reproduce_command"]
    assert detail["replay_bundle_urls"] == [f"{BOARD}/submissions/{sid}/bundles"]
    assert detail["verified"] is True
    assert detail["scores"]["composite"] == detail["composite_score"]

    bundle_payload = anon.get(detail["replay_bundle_urls"][0]).json()
    assert bundle_payload["submission_id"] == sid
    assert bundle_payload["claimed"]["composite"] == detail["composite_score"]
    assert len(bundle_payload["bundles"]) == 3
    # Bundles are payload-free: case ids + ordered output ids + hash only.
    assert set(bundle_payload["bundles"][0]) == {"case_id", "output_ids", "content_hash"}


def test_public_rate_limited_429(make_client, monkeypatch: pytest.MonkeyPatch) -> None:
    """AC17: requests beyond the per-IP budget return 429."""
    _published_submission(make_client)
    tight = Settings(
        public_leaderboard_enabled=True,
        leaderboard_public_rate_limit=3,
        leaderboard_cache_ttl_seconds=60,
    )
    monkeypatch.setattr(public_module, "get_settings", lambda: tight)
    public_module.rate_limiter.reset()

    anon = make_client(None, settings=tight)
    statuses = [anon.get(BOARD).status_code for _ in range(5)]
    assert statuses[:3] == [200, 200, 200]
    assert statuses[3] == 429
    assert statuses[4] == 429


def test_unpublished_suite_hidden_from_public(make_client, service) -> None:
    with service._session_factory() as session:  # test-only reach-in
        from forge_db.models.benchmark import BenchmarkSuite

        suite = session.query(BenchmarkSuite).one()
        suite.published = False
        session.commit()
    anon = make_client(None)
    assert anon.get("/public/benchmarks").json() == []
    assert anon.get(BOARD).status_code == 404


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda s: setattr(s, "private", True), id="private-flag"),
        pytest.param(lambda s: setattr(s, "workspace_id", WS_ID), id="workspace-scoped"),
    ],
)
def test_private_self_eval_suite_never_public(make_client, service, mutate) -> None:
    """A private/workspace-scoped Self-Eval suite is invisible to /public/*.

    Even when it is ``published`` (Self-Eval suites are published so the owning
    workspace can rank against its own baseline), the public surface must treat
    it as if it does not exist — no listing, no leaderboard.
    """
    sid = _published_submission(make_client)  # give the suite a rankable entry
    with service._session_factory() as session:  # test-only reach-in
        from forge_db.models.benchmark import BenchmarkSuite

        suite = session.query(BenchmarkSuite).one()
        assert suite.published is True  # precondition: only privacy hides it now
        mutate(suite)
        session.commit()

    anon = make_client(None)
    assert anon.get("/public/benchmarks").json() == []
    assert anon.get(BOARD).status_code == 404
    # The submission detail for that suite must 404 too — no back-door leak.
    assert anon.get(f"{BOARD}/submissions/{sid}").status_code == 404
