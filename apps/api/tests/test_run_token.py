"""A run-scoped token and the run-record routes that accept only it (#256).

A token belongs to one run of one workspace and expires; with it a run process reads its
run's inputs, appends events and records its handoff through
``/agent/runs/{run_id}/record/*``, and nothing else. Every other credential is refused
there, and the token is refused everywhere else.
"""

from __future__ import annotations

import json
import logging
import math
import sys
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.routing import APIRoute, iter_route_contexts
from fastapi.testclient import TestClient
from sqlalchemy import select
from starlette.websockets import WebSocketDisconnect

import scf_api.auth.run_token as run_token_module
import scf_api.auth.service as auth_service_module
from scf_api.auth.run_token import (
    RUN_TOKEN_KEY,
    issue_run_token,
    require_run_token,
    revoke_run_token,
)
from scf_api.auth.service import AuthenticationError, AuthService
from scf_api.deps import SESSION_COOKIE
from scf_api.main import create_app
from scf_api.observability.redaction import redact_text
from scf_api.routers.benchmarks import get_benchmark_service
from scf_api.routers.run_events import _message
from scf_api.services.benchmark_service import BenchmarkService
from scf_api.services.run_record import HANDOFF_KEY, MAX_TEXT, record_handoff
from scf_api.settings import Settings
from scf_auth.tokens import encode_session_jwt
from scf_contracts import ENFORCEMENT, AgentRunResult, RunEventDraft, RunEventType, RunStatus
from scf_contracts.auth import SessionClaims
from scf_contracts.authz import PrincipalType, ScopeType
from scf_contracts.enums import UserRole
from scf_db.models import AgentRun, AgentRunEvent, AuditLog, RoleGrant, User, Workspace
from scf_worker.run_events import RunEventSink
from scf_worker.run_record import RunRecord

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "tests" / "security"))
from route_matrix import routes, url

AUTH_SECRET = "run-token-test-secret"
WS = uuid.UUID("00000000-0000-0000-0000-00000000a201")
OTHER_WS = uuid.UUID("00000000-0000-0000-0000-00000000a202")
ADMIN = uuid.UUID("00000000-0000-0000-0000-00000000a203")
MEMBER = uuid.UUID("00000000-0000-0000-0000-00000000a204")
RUN_A = uuid.UUID("00000000-0000-0000-0000-00000000a205")
RUN_B = uuid.UUID("00000000-0000-0000-0000-00000000a206")
RUN_C = uuid.UUID("00000000-0000-0000-0000-00000000a207")  # OTHER_WS
INPUTS = {"objective": "fix the flaky test", "task_id": None, "context": {"k": "v"}}
#: The default wall-clock limit (3,600 s) plus the reconciler's default grace (300 s).
DEADLINE = 3_900.0
RECORD = "/agent/runs/{run}/record"
EVENT = {"type": "agent.message", "summary": "thinking", "payload": {"text": "hello"}}
HANDOFF = AgentRunResult(status=RunStatus.SUCCEEDED, summary="done").model_dump(mode="json")


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def session_jwt(user_id: uuid.UUID, role: UserRole) -> str:
    now = int(time.time())
    claims = SessionClaims(
        sub=user_id, wsid=WS, role=role, email="p@acme.test", exp=now + 3600, iat=now
    )
    return encode_session_jwt(claims, secret=AUTH_SECRET)


@pytest.fixture
def auth(monkeypatch: pytest.MonkeyPatch) -> AuthService:
    service = AuthService(secret_key=b"6" * 32, auth_secret=AUTH_SECRET)
    monkeypatch.setattr(auth_service_module, "_default_service", service)
    return service


@pytest.fixture
def db(workflow_db: Any) -> Any:
    with workflow_db.factory() as s:
        s.add(Workspace(id=WS, name="Acme", slug="acme"))
        s.add(Workspace(id=OTHER_WS, name="Other", slug="other"))
        s.flush()
        s.add(User(id=ADMIN, workspace_id=WS, email="a@acme.test", role=UserRole.ADMIN))
        s.add(User(id=MEMBER, workspace_id=WS, email="m@acme.test", role=UserRole.MEMBER))
        s.add(
            RoleGrant(
                workspace_id=WS,
                principal_type=PrincipalType.USER,
                principal_id=ADMIN,
                scope_type=ScopeType.WORKSPACE,
                scope_id=WS,
                role=UserRole.ADMIN,
            )
        )
        s.add(AgentRun(id=RUN_A, workspace_id=WS, inputs=INPUTS))
        s.add(AgentRun(id=RUN_B, workspace_id=WS, inputs={"objective": "b"}))
        s.add(AgentRun(id=RUN_C, workspace_id=OTHER_WS, inputs={"objective": "c"}))
        s.commit()
    return workflow_db


def _client(db: Any, **settings: Any) -> TestClient:
    config = Settings(ratelimit_enabled=False, public_leaderboard_enabled=True, **settings)
    app = db.install(create_app(config))
    benchmarks = BenchmarkService(session_factory=db.factory)  # the public leaderboard's reads
    app.dependency_overrides[get_benchmark_service] = lambda: benchmarks
    return TestClient(app)


@pytest.fixture
def client(auth: AuthService, db: Any) -> Iterator[TestClient]:
    with _client(db) as c:
        yield c


def issue(db: Any, run: uuid.UUID = RUN_A, workspace: uuid.UUID = WS, **kw: Any) -> str:
    with db.factory() as s:
        return issue_run_token(
            s, run_id=run, workspace_id=workspace, run_deadline_seconds=DEADLINE, **kw
        ).token


def _row(db: Any, run: uuid.UUID) -> AgentRun:
    with db.factory() as s:
        row = s.get(AgentRun, run)
        assert row is not None
        return row


def _events(db: Any, run: uuid.UUID) -> list[AgentRunEvent]:
    with db.factory() as s:
        stmt = select(AgentRunEvent).where(AgentRunEvent.run_id == run).order_by(AgentRunEvent.seq)
        return list(s.scalars(stmt))


def _audit(db: Any) -> list[AuditLog]:
    with db.factory() as s:
        return list(s.scalars(select(AuditLog).order_by(AuditLog.seq)))


def _calls(run: uuid.UUID) -> list[tuple[str, str, Any]]:
    base = RECORD.format(run=run)
    return [
        ("GET", f"{base}/inputs", None),
        ("POST", f"{base}/events", EVENT),
        ("POST", f"{base}/handoff", HANDOFF),
    ]


def _nothing_stored(db: Any, run: uuid.UUID) -> None:
    assert _events(db, run) == []
    assert HANDOFF_KEY not in (_row(db, run).output or {})


# --------------------------------------------------------------------------- #
# Criterion 1: issuing                                                         #
# --------------------------------------------------------------------------- #


def test_a_run_token_is_issued_for_one_open_run_and_its_plaintext_is_never_kept(db: Any) -> None:
    now = datetime.now(UTC)
    with db.factory() as s:
        issued = issue_run_token(
            s, run_id=RUN_A, workspace_id=WS, run_deadline_seconds=DEADLINE, now=now
        )

    assert (issued.run_id, issued.workspace_id) == (RUN_A, WS)
    assert issued.expires_at == now + timedelta(seconds=DEADLINE)
    assert issued.token not in repr(issued) and issued.token not in str(issued)
    stored = _row(db, RUN_A).output or {}
    assert issued.token not in json.dumps(stored)
    assert set(stored[RUN_TOKEN_KEY]) == {"sha256", "expires_at"}


@pytest.mark.parametrize(("run", "workspace"), [(RUN_C, WS), (RUN_A, OTHER_WS), (uuid.uuid4(), WS)])
def test_no_token_is_issued_for_a_run_that_is_not_the_workspaces(
    db: Any, run: uuid.UUID, workspace: uuid.UUID
) -> None:
    with pytest.raises(LookupError):
        issue(db, run, workspace)
    assert _audit(db) == []


def test_no_token_is_issued_for_a_closed_out_run(db: Any) -> None:
    with db.factory() as s:
        row = s.get(AgentRun, RUN_A)
        assert row is not None
        row.completed_at = datetime.now(UTC)
        s.commit()

    with pytest.raises(LookupError):
        issue(db)


@pytest.mark.parametrize("deadline", [0.0, -5.0, math.inf, math.nan])
def test_an_unusable_run_deadline_issues_nothing(db: Any, deadline: float) -> None:
    with db.factory() as s, pytest.raises(ValueError, match="run_deadline_seconds"):
        issue_run_token(s, run_id=RUN_A, workspace_id=WS, run_deadline_seconds=deadline)


@pytest.mark.parametrize(("setting", "lifetime"), [(None, DEADLINE), (600, 600), (7_200, DEADLINE)])
def test_the_lifetime_is_the_runs_deadline_or_the_setting_when_shorter(
    db: Any, monkeypatch: pytest.MonkeyPatch, setting: int | None, lifetime: float
) -> None:
    settings = Settings(run_token_ttl_seconds=setting)
    monkeypatch.setattr(run_token_module, "get_settings", lambda: settings)
    now = datetime.now(UTC)

    with db.factory() as s:
        issued = issue_run_token(
            s, run_id=RUN_A, workspace_id=WS, run_deadline_seconds=DEADLINE, now=now
        )

    assert issued.expires_at == now + timedelta(seconds=lifetime)


# --------------------------------------------------------------------------- #
# Criterion 2: the run's own record                                            #
# --------------------------------------------------------------------------- #


def test_with_its_token_a_run_reads_its_inputs_appends_an_event_and_records_its_handoff(
    client: TestClient, db: Any
) -> None:
    token = issue(db)
    base = RECORD.format(run=RUN_A)

    inputs = client.get(f"{base}/inputs", headers=bearer(token))
    event = client.post(f"{base}/events", headers=bearer(token), json=EVENT)
    handoff = client.post(f"{base}/handoff", headers=bearer(token), json=HANDOFF)

    assert inputs.status_code == 200, inputs.text
    assert inputs.json() == INPUTS
    assert event.status_code == 204, event.text
    stored_events = [(e.seq, e.type, e.summary) for e in _events(db, RUN_A)]
    assert stored_events == [(1, "agent.message", "thinking")]
    assert handoff.status_code == 204, handoff.text
    stored = AgentRunResult.model_validate((_row(db, RUN_A).output or {})[HANDOFF_KEY])
    assert (stored.status, stored.summary, stored.run_id) == (RunStatus.SUCCEEDED, "done", RUN_A)


def test_the_handoff_is_stored_under_its_own_run_whatever_the_body_says(
    client: TestClient, db: Any
) -> None:
    body = {**HANDOFF, "run_id": str(RUN_B)}

    resp = client.post(f"{RECORD.format(run=RUN_A)}/handoff", headers=bearer(issue(db)), json=body)

    assert resp.status_code == 204, resp.text
    assert _row(db, RUN_A).output[HANDOFF_KEY]["run_id"] == str(RUN_A)  # type: ignore[index]
    _nothing_stored(db, RUN_B)


# --------------------------------------------------------------------------- #
# Criterion 3: another run's record                                            #
# --------------------------------------------------------------------------- #


def test_a_run_token_is_refused_on_another_runs_record_whether_or_not_it_exists(
    client: TestClient, db: Any
) -> None:
    token = issue(db)
    issue(db, RUN_B)  # B has a live token of its own
    issue(db, RUN_C, OTHER_WS)

    answers = {}
    for run in (RUN_B, RUN_C, uuid.uuid4()):
        for method, path, body in _calls(run):
            resp = client.request(method, path, headers=bearer(token), json=body)
            answers[(run, method, path.rsplit("/", 1)[1])] = (resp.status_code, resp.json())

    assert {status for status, _ in answers.values()} == {401}
    assert len({json.dumps(body) for _, body in answers.values()}) == 1  # nothing tells them apart
    _nothing_stored(db, RUN_B)
    _nothing_stored(db, RUN_C)


# --------------------------------------------------------------------------- #
# Criterion 5: every other credential                                          #
# --------------------------------------------------------------------------- #


def _credentials(client: TestClient, auth: AuthService, db: Any) -> dict[str, dict[str, str]]:
    """Every credential the run-record routes must refuse, as request headers."""
    admin = session_jwt(ADMIN, UserRole.ADMIN)
    creds: dict[str, dict[str, str]] = {"none": {}, "session JWT": bearer(admin)}
    for role in UserRole:
        _, key = auth.bootstrap_key(workspace_id=WS, name=role.value, role=role, user_id=ADMIN)
        creds[f"{role.value} API key"] = bearer(key)
    _, web = auth.bootstrap_key(workspace_id=WS, name="web", role=UserRole.ADMIN, user_id=ADMIN)
    _, unbound = auth.bootstrap_key(workspace_id=WS, name="ci", role=UserRole.ADMIN)
    creds["unbound admin API key"] = bearer(unbound)
    _, agent = auth.mint_agent_token(workspace_id=WS, name="r", user_id=ADMIN)
    creds["agent token"] = bearer(agent)
    for name, extra in {
        "read": {"decide": False},
        "read+decide": {"decide": True},
        "read+runs:execute": {"start_runs": True},
    }.items():
        resp = client.post(
            "/auth/personal-tokens",
            headers=bearer(admin),
            json={"name": name, "surface": "cli", **extra},
        )
        assert resp.status_code == 201, resp.text
        creds[f"personal token {name}"] = bearer(resp.json()["token"])
    creds["session cookie"] = {"Cookie": f"{SESSION_COOKIE}={web}"}

    own = issue(db)
    run, workspace, secret = own.removeprefix("scf_run_").split("_", 2)
    creds["own token in X-API-Key"] = {"X-API-Key": own}
    creds["own token in the session cookie"] = {"Cookie": f"{SESSION_COOKIE}={own}"}
    creds["own token as Basic"] = {"Authorization": f"Basic {own}"}
    creds["own token, secret changed"] = bearer(f"scf_run_{run}_{workspace}_{'A' * len(secret)}")
    creds["own token, workspace changed"] = bearer(f"scf_run_{run}_{OTHER_WS.hex}_{secret}")
    creds["own token, cut short"] = bearer(own[:-1])
    creds["token of run B"] = bearer(issue(db, RUN_B))
    return creds


def test_every_other_credential_is_refused_on_the_run_record_routes(
    client: TestClient, auth: AuthService, db: Any
) -> None:
    credentials = _credentials(client, auth, db)

    accepted = []
    for name, headers in credentials.items():
        for method, path, body in _calls(RUN_A):
            resp = client.request(method, path, headers=headers, json=body)
            if resp.status_code != 401:
                accepted.append(f"{name}: {method} {path} -> {resp.status_code}")

    assert accepted == []
    assert len(credentials) == 19
    _nothing_stored(db, RUN_A)


# --------------------------------------------------------------------------- #
# Criterion 8: expired, revoked, closed out                                    #
# --------------------------------------------------------------------------- #


def _expire(db: Any) -> str:
    return issue(db, now=datetime.now(UTC) - timedelta(seconds=DEADLINE + 1))


def _revoke(db: Any) -> str:
    token = issue(db)
    with db.factory() as s:
        assert revoke_run_token(s, run_id=RUN_A, workspace_id=WS)
    return token


def _close_out(db: Any) -> str:
    token = issue(db)
    with db.factory() as s:  # close-out's marker, the token's record left in place
        row = s.get(AgentRun, RUN_A)
        assert row is not None
        row.completed_at = datetime.now(UTC)
        s.commit()
    return token


def _replace(db: Any) -> str:
    token = issue(db)
    issue(db)
    return token


def _move(db: Any) -> str:
    token = issue(db)
    with db.factory() as s:  # the token names WS; the run is no longer there
        row = s.get(AgentRun, RUN_A)
        assert row is not None
        row.workspace_id = OTHER_WS
        s.commit()
    return token


@pytest.mark.parametrize("make", [_expire, _revoke, _close_out, _replace, _move])
def test_a_run_token_is_refused_once_expired_revoked_replaced_or_closed_out(
    client: TestClient, db: Any, make: Any
) -> None:
    token = make(db)

    for method, path, body in _calls(RUN_A):
        resp = client.request(method, path, headers=bearer(token), json=body)
        assert resp.status_code == 401, f"{method} {path} -> {resp.status_code}"
    _nothing_stored(db, RUN_A)


def test_authentication_refuses_a_run_token_on_every_api_key_route(
    auth: AuthService, db: Any
) -> None:
    with pytest.raises(AuthenticationError, match="only by its own run's record"):
        auth.authenticate(issue(db))


@pytest.mark.parametrize("case", ["another workspace", "closed out", "handed off"])
def test_a_handoff_is_stored_only_once_for_an_open_run_of_its_workspace(db: Any, case: str) -> None:
    first = AgentRunResult(status=RunStatus.FAILED, summary="first")
    with db.factory() as s:
        row = s.get(AgentRun, RUN_A)
        assert row is not None
        if case == "closed out":
            row.completed_at = datetime.now(UTC)
        if case == "handed off":
            row.output = {HANDOFF_KEY: first.model_dump(mode="json")}
        s.commit()
        before = dict(row.output or {})
        workspace = OTHER_WS if case == "another workspace" else WS

        assert not record_handoff(s, RUN_A, workspace, AgentRunResult(summary="second"))
        s.commit()
    assert (_row(db, RUN_A).output or {}) == before


def test_revoking_a_run_without_a_token_or_of_another_workspace_changes_nothing(db: Any) -> None:
    issue(db)
    with db.factory() as s:
        assert not revoke_run_token(s, run_id=RUN_A, workspace_id=OTHER_WS)
        assert not revoke_run_token(s, run_id=RUN_B, workspace_id=WS)
    assert RUN_TOKEN_KEY in (_row(db, RUN_A).output or {})


# --------------------------------------------------------------------------- #
# Criterion 4: every other route                                               #
# --------------------------------------------------------------------------- #


def _socket_close_code(client: TestClient, path: str, headers: dict[str, str]) -> int:
    try:
        with client.websocket_connect(path, headers=headers) as ws:
            while (message := ws.receive())["type"] != "websocket.close":
                pass
    except WebSocketDisconnect as exc:
        return exc.code
    return int(message["code"])


def test_a_run_token_is_refused_on_every_route_but_its_own_runs_record(
    client: TestClient, db: Any
) -> None:
    token = issue(db)
    wrong: list[str] = []
    walked = 0
    for method, path, needs in routes(client.app):
        target = url(path)  # any run's record here is another run's
        walked += 1
        if method == "WS":
            code = _socket_close_code(client, target, bearer(token))
            if code != 1008:
                wrong.append(f"WS {path} -> {code}")
            continue
        resp = client.request(method, target, headers=bearer(token))
        if needs is not None or "/record/" in path:
            if resp.status_code != 401:
                wrong.append(f"{method} {path} -> {resp.status_code}")
            continue
        # Routes without a role guard: the token must gain nothing over no credential.
        anonymous = client.request(method, target)
        if resp.status_code != anonymous.status_code:
            wrong.append(f"{method} {path} -> {resp.status_code}, {anonymous.status_code} bare")

    assert wrong == []
    assert walked > 300
    # The token still works on its own run's record: each refusal above was the route's.
    assert client.get(f"{RECORD.format(run=RUN_A)}/inputs", headers=bearer(token)).is_success


# --------------------------------------------------------------------------- #
# Criterion 6: what an appended event is                                       #
# --------------------------------------------------------------------------- #

SECRET = "ghp_" + "a1B2c3D4e5" * 4
#: What a caller may claim about an event; SCF decides all of it.
CLAIMS = {
    "enforcement": "observed",
    "enforceable": True,
    "observed_by": "agent",
    "seq": 99,
    "run_id": str(RUN_B),
    "ts": "2000-01-01T00:00:00Z",
}
DRAFTS = [
    RunEventDraft(type=RunEventType.THREAD_STARTED, summary="started"),
    RunEventDraft(
        type=RunEventType.AGENT_MESSAGE,
        summary=f"found {SECRET}",
        payload={"text": "x" * (MAX_TEXT + 500), "nested": [{"auth": f"Bearer {SECRET}"}]},
    ),
    RunEventDraft(
        type=RunEventType.TOOL_COMPLETED, summary="ran tests", payload={"ok": True, "n": 3}
    ),
]


def _shape(rows: list[AgentRunEvent]) -> list[tuple[Any, ...]]:
    return [(r.seq, r.type, r.enforcement, r.summary, r.payload) for r in rows]


def test_an_event_appended_through_the_route_is_stored_as_the_sink_stores_it(
    client: TestClient, db: Any
) -> None:
    sink = RunEventSink(RUN_B, WS, session_factory=db.factory, redact=redact_text)
    first = RunEventDraft(type=RunEventType.THREAD_STATE, summary="queued")
    assert sink.emit(first)
    RunEventSink(RUN_A, WS, session_factory=db.factory, redact=redact_text).emit(first)
    token = issue(db)

    for draft in DRAFTS:
        assert sink.emit(draft)
        body = {**CLAIMS, **draft.model_dump(mode="json")}
        resp = client.post(f"{RECORD.format(run=RUN_A)}/events", headers=bearer(token), json=body)
        assert resp.status_code == 204, resp.text

    through_route, through_sink = _events(db, RUN_A), _events(db, RUN_B)
    assert _shape(through_route) == _shape(through_sink)
    assert [r.seq for r in through_route] == [1, 2, 3, 4]
    assert all(r.workspace_id == WS and r.ts.year > 2000 for r in through_route)
    assert all(r.enforcement == ENFORCEMENT[RunEventType(r.type)] for r in through_route)
    text = json.dumps(_shape(through_route))
    assert SECRET not in text and "[REDACTED]" in text
    assert len(through_route[2].payload["text"]) == MAX_TEXT
    streamed = [json.loads(m.split("data: ", 1)[1]) for r in through_route if (m := _message(r))]
    assert {(e["observed_by"], e["enforceable"]) for e in streamed} == {
        ("forge", False),
        ("forge", True),
    }
    assert all(e["enforceable"] == (e["enforcement"] == "gated") for e in streamed)


@pytest.mark.parametrize("kind", ["agent.thinking", "", "THREAD.STARTED", None])
def test_an_event_type_outside_the_vocabulary_is_refused(
    client: TestClient, db: Any, kind: str | None
) -> None:
    resp = client.post(
        f"{RECORD.format(run=RUN_A)}/events",
        headers=bearer(issue(db)),
        json={**EVENT, "type": kind},
    )

    assert resp.status_code == 422
    assert _events(db, RUN_A) == []


@pytest.mark.parametrize(
    "kind",
    ["thread.state", "thread.completed", "thread.failed", "artifact.created", "cost.updated"],
)
def test_an_event_only_scf_writes_is_refused_from_the_run(
    client: TestClient, db: Any, kind: str
) -> None:
    """The workflow's pause notices and close-out's terminal events are SCF's own record:
    a run that could write them could hide an escalation or end every follower's stream."""
    resp = client.post(
        f"{RECORD.format(run=RUN_A)}/events",
        headers=bearer(issue(db)),
        json={**EVENT, "type": kind},
    )

    assert resp.status_code == 422
    assert _events(db, RUN_A) == []


# --------------------------------------------------------------------------- #
# Criterion 7: once                                                            #
# --------------------------------------------------------------------------- #


def test_a_repeated_idempotency_key_stores_one_event(auth: AuthService, db: Any) -> None:
    # With the request-replay middleware off, the stored event itself must hold the key.
    with _client(db, idempotency_enabled=False) as client:
        token = issue(db)
        post = f"{RECORD.format(run=RUN_A)}/events"

        statuses = [
            client.post(
                post, headers={**bearer(token), "Idempotency-Key": key}, json=EVENT
            ).status_code
            for key in ("k1", "k1", "k2", "k1")
        ]
        other = client.post(
            f"{RECORD.format(run=RUN_B)}/events",
            headers={**bearer(issue(db, RUN_B)), "Idempotency-Key": "k1"},
            json=EVENT,
        )

    assert statuses == [204, 204, 204, 204]
    assert [e.seq for e in _events(db, RUN_A)] == [1, 2]
    assert other.status_code == 204  # the key is the run's own
    assert len(_events(db, RUN_B)) == 1


def test_a_handoff_is_recorded_once_and_the_first_is_kept(client: TestClient, db: Any) -> None:
    token = issue(db)
    post = f"{RECORD.format(run=RUN_A)}/handoff"
    failed = {**HANDOFF, "status": "failed", "summary": "second"}

    first = client.post(post, headers=bearer(token), json=HANDOFF)
    second = client.post(post, headers=bearer(token), json=failed)

    assert (first.status_code, second.status_code) == (204, 409)
    assert _row(db, RUN_A).output[HANDOFF_KEY]["summary"] == "done"  # type: ignore[index]
    assert [a.action for a in _audit(db)].count("agent_run.handoff_recorded") == 1


# --------------------------------------------------------------------------- #
# Criterion 9: audit, and the token never written down                         #
# --------------------------------------------------------------------------- #


def test_issuing_and_recording_a_handoff_each_write_one_audit_entry(
    client: TestClient, db: Any
) -> None:
    now = datetime.now(UTC)
    with db.factory() as s:
        issued = issue_run_token(
            s, run_id=RUN_A, workspace_id=WS, run_deadline_seconds=DEADLINE, now=now
        )
    resp = client.post(
        f"{RECORD.format(run=RUN_A)}/handoff", headers=bearer(issued.token), json=HANDOFF
    )
    assert resp.status_code == 204, resp.text

    entries = _audit(db)
    assert [(a.action, a.actor_type) for a in entries] == [
        ("run_token.issued", "system"),
        ("agent_run.handoff_recorded", "agent_runner"),
    ]
    for entry in entries:
        assert (entry.workspace_id, entry.target_type, entry.target_id) == (WS, "agent_run", RUN_A)
        assert entry.details["run_id"] == str(RUN_A)
        assert datetime.fromisoformat(entry.details["expires_at"]) == issued.expires_at


def test_the_token_is_never_in_a_log_audit_entry_event_or_error(
    client: TestClient, db: Any, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    with db.factory() as s:
        issued = issue_run_token(s, run_id=RUN_A, workspace_id=WS, run_deadline_seconds=DEADLINE)
    token = issued.token
    base = RECORD.format(run=RUN_A)
    logging.getLogger("scf_api.test").info("issued %s", issued)
    leaky = {
        "type": "tool.completed",
        "summary": f"env: SCF_RUN_TOKEN={token}",
        "payload": {"stdout": f"token={token}\n", "args": [token]},
    }
    answers = [
        client.post(f"{base}/events", headers=bearer(token), json=leaky),
        client.post(f"{base}/events", headers=bearer(token), json={**EVENT, "type": "x"}),
        client.post(f"{base}/handoff", headers=bearer(token), json=HANDOFF),
        client.post(f"{base}/handoff", headers=bearer(token), json=HANDOFF),
        client.get(f"{RECORD.format(run=RUN_B)}/inputs", headers=bearer(token)),
        client.get("/board/tasks", headers=bearer(token)),
        client.get(f"{base}/inputs", headers=bearer(token[:-3])),
    ]
    with db.factory() as s:
        revoke_run_token(s, run_id=RUN_A, workspace_id=WS)
    answers.append(client.get(f"{base}/inputs", headers=bearer(token)))

    assert [a.status_code for a in answers] == [204, 422, 204, 409, 401, 401, 401, 401]
    secret = token.rsplit("_", 1)[1]
    written = [
        *(a.text for a in answers),
        *(f"{r.getMessage()} {r.exc_text or ''}" for r in caplog.records),
        *(json.dumps([a.details, a.reason, a.actor_label]) for a in _audit(db)),
        *(json.dumps([e.summary, e.payload]) for e in _events(db, RUN_A)),
        json.dumps(_row(db, RUN_A).output),
    ]
    assert [text for text in written if token in text or secret in text] == []
    assert len(caplog.records) > 0 and len(_events(db, RUN_A)) == 1


# --------------------------------------------------------------------------- #
# Criterion 10: the port                                                       #
# --------------------------------------------------------------------------- #

#: The route each method of the run-record port uses (``on_step`` appends the events
#: ``events_for_step`` makes of a step, as the in-process sink does).
PORT_ROUTES = {
    "inputs": "GET /agent/runs/{run_id}/record/inputs",
    "emit": "POST /agent/runs/{run_id}/record/events",
    "on_step": "POST /agent/runs/{run_id}/record/events",
    "record_handoff": "POST /agent/runs/{run_id}/record/handoff",
}


def test_every_method_of_the_run_record_port_has_a_route_bound_to_its_run(
    client: TestClient,
) -> None:
    methods = {name for name in vars(RunRecord) if not name.startswith("_")}
    bound = set()
    for route in iter_route_contexts(client.app.routes):
        if not isinstance(route.original_route, APIRoute):
            continue
        stack = list(route.dependant.dependencies)
        calls = set()
        while stack:
            dep = stack.pop()
            calls.add(dep.call)
            stack.extend(dep.dependencies)
        if require_run_token in calls:
            bound.update(f"{m} {route.path}" for m in route.methods or ())

    assert methods == PORT_ROUTES.keys()
    assert bound == set(PORT_ROUTES.values())
