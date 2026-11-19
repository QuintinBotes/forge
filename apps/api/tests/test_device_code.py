"""Device sign-in for a client with no browser (#207, RFC 8628, ADR-0015).

A client asks ``POST /auth/device/code`` for a code with no credential; a signed-in
person approves or denies its user code at ``POST /auth/device/approve``; the client
polls ``POST /auth/device/token`` and receives a run-start personal token (#206) once.
``scf-cli login`` runs the flow. Real authentication end to end over one SQLite schema.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import logging
import re
import shlex
import subprocess
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

import scf_api.auth.service as auth_service_module
from scf_api import cli as cli_mod
from scf_api import cli_remote
from scf_api.auth import device_code
from scf_api.auth.service import AuthService
from scf_api.main import create_app
from scf_api.observability.redaction import redact_mapping
from scf_api.services.approval_service import build_gate_registry, get_approval_service
from scf_api.settings import get_settings
from scf_approval import ApprovalAuthorizer, ApprovalService, InMemoryApprovalRepository
from scf_approval.providers import InMemoryGrantStore
from scf_auth.tokens import encode_session_jwt
from scf_contracts.auth import SessionClaims
from scf_contracts.authz import PrincipalType, ScopeType
from scf_contracts.enums import UserRole
from scf_db.models import AuditLog, DeviceAuthorization, RoleGrant, User, Workspace

AUTH_SECRET = "device-code-test-secret"
WS = uuid.UUID("00000000-0000-0000-0000-0000000207a1")
ADMIN = uuid.UUID("00000000-0000-0000-0000-0000000207b1")
MEMBER = uuid.UUID("00000000-0000-0000-0000-0000000207b2")
VIEWER = uuid.UUID("00000000-0000-0000-0000-0000000207b3")
USER_CODE = re.compile(r"[BCDFGHJKLMNPQRSTVWXZ]{4}-[BCDFGHJKLMNPQRSTVWXZ]{4}")
NOT_VALID = {"detail": "this code is not valid"}


def session_jwt(user_id: uuid.UUID, role: UserRole) -> str:
    now = int(time.time())
    claims = SessionClaims(
        sub=user_id, wsid=WS, role=role, email="p@acme.test", exp=now + 3600, iat=now
    )
    return encode_session_jwt(claims, secret=AUTH_SECRET)


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def auth(monkeypatch: pytest.MonkeyPatch) -> AuthService:
    service = AuthService(secret_key=b"9" * 32, auth_secret=AUTH_SECRET)
    monkeypatch.setattr(auth_service_module, "_default_service", service)
    return service


@pytest.fixture
def db(workflow_db: Any) -> Any:
    with workflow_db.factory() as s:
        s.add(Workspace(id=WS, name="Acme", slug="acme"))
        s.flush()
        s.add(User(id=ADMIN, workspace_id=WS, email="a@acme.test", role=UserRole.ADMIN))
        s.add(User(id=MEMBER, workspace_id=WS, email="m@acme.test", role=UserRole.MEMBER))
        s.add(User(id=VIEWER, workspace_id=WS, email="v@acme.test", role=UserRole.VIEWER))
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
        s.commit()
    return workflow_db


@pytest.fixture
def client(auth: AuthService, db: Any) -> Iterator[TestClient]:
    approvals = ApprovalService(
        InMemoryApprovalRepository(),
        build_gate_registry(InMemoryGrantStore()),
        ApprovalAuthorizer(),
        redactor=redact_mapping,
    )
    app = db.install(create_app())
    app.dependency_overrides[get_approval_service] = lambda: approvals
    with TestClient(app) as c:
        yield c


def start(client: TestClient, name: str = "laptop cli") -> dict[str, Any]:
    resp = client.post("/auth/device/code", json={"client_name": name})
    assert resp.status_code == 200, resp.text
    return dict(resp.json())


def approve(
    client: TestClient, credential: str, user_code: str, decision: str = "approve"
) -> httpx.Response:
    return client.post(
        "/auth/device/approve",
        headers=bearer(credential),
        json={"user_code": user_code, "decision": decision},
    )


def poll(client: TestClient, db: Any, code: str, *, wait: bool = True) -> httpx.Response:
    """Poll for the token; ``wait`` lets the polling interval pass first."""
    if wait:
        with db.factory() as s:
            s.execute(update(DeviceAuthorization).values(last_polled_at=None))
            s.commit()
    return client.post("/auth/device/token", json={"device_code": code})


def error_of(resp: httpx.Response) -> str:
    assert resp.status_code == 400, resp.text
    return str(resp.json()["error"])


def only_request(db: Any) -> DeviceAuthorization:
    with db.factory() as s:
        return s.scalars(select(DeviceAuthorization)).one()


def member() -> str:
    return session_jwt(MEMBER, UserRole.MEMBER)


def admin() -> str:
    return session_jwt(ADMIN, UserRole.ADMIN)


def sign_in(client: TestClient, db: Any) -> dict[str, Any]:
    started = start(client)
    assert approve(client, member(), started["user_code"]).status_code == 200
    issued = poll(client, db, started["device_code"])
    assert issued.status_code == 200, issued.text
    return dict(issued.json())


def wrong_code(user_code: str) -> str:
    """The same first half (it names the request) with a different second half."""
    head, tail = user_code.split("-")
    alphabet = device_code.ALPHABET
    return f"{head}-{''.join(alphabet[(alphabet.index(c) + 1) % len(alphabet)] for c in tail)}"


# --------------------------------------------------------------------------- #
# Criterion 1: a code for a client with no credential                         #
# --------------------------------------------------------------------------- #


def test_a_client_with_no_credential_gets_a_device_code(client: TestClient) -> None:
    resp = client.post("/auth/device/code", json={"client_name": "laptop cli"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert USER_CODE.fullmatch(body["user_code"])
    assert len(body["device_code"]) >= 43  # 32 random bytes
    public_url = get_settings().public_url.rstrip("/")
    assert body["verification_uri"] == f"{public_url}/auth/device/approve"
    assert (body["expires_in"], body["interval"]) == (600, 5)
    assert (body["scope"], body["client_name"]) == ("read runs:execute", "laptop cli")
    assert resp.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("body", [{}, {"client_name": ""}, {"client_name": "x" * 65}])
def test_a_device_code_request_names_its_client(client: TestClient, body: dict[str, Any]) -> None:
    assert client.post("/auth/device/code", json=body).status_code == 422


# --------------------------------------------------------------------------- #
# Criterion 2: a person approves (bound to them) or denies, finally           #
# --------------------------------------------------------------------------- #


def test_an_approval_binds_the_request_to_the_approving_person(client: TestClient, db: Any) -> None:
    started = start(client)
    typed = started["user_code"].lower().replace("-", " ")  # as a person might type it

    resp = approve(client, member(), typed)

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"client_name": "laptop cli", "decision": "approve"}
    request = only_request(db)
    assert (request.status, request.workspace_id, request.user_id) == ("approved", WS, MEMBER)


def _demote_member(db: Any) -> None:
    with db.factory() as s:
        s.execute(update(User).where(User.id == MEMBER).values(role=UserRole.VIEWER))
        s.commit()


def _deactivate_member(db: Any) -> None:
    with db.factory() as s:
        s.execute(update(User).where(User.id == MEMBER).values(is_active=False))
        s.commit()


@pytest.mark.parametrize(
    ("credential", "change", "refusal"),
    [
        (lambda _a: session_jwt(VIEWER, UserRole.VIEWER), None, "run_agent"),
        # The session still says member; the person's current role is viewer.
        (lambda _a: member(), _demote_member, "run_agent"),
        # The person may run agents; the credential they approve with may not.
        (
            lambda a: a.bootstrap_key(
                workspace_id=WS, name="web", role=UserRole.VIEWER, user_id=MEMBER
            )[1],
            None,
            "run_agent",
        ),
        (lambda _a: member(), _deactivate_member, "no current role"),
    ],
    ids=["viewer", "demoted-member", "viewer-key", "deactivated"],
)
def test_an_approval_is_refused_without_run_agent_in_the_current_role(
    client: TestClient,
    db: Any,
    auth: AuthService,
    credential: Callable[[AuthService], str],
    change: Callable[[Any], None] | None,
    refusal: str,
) -> None:
    started = start(client)
    if change is not None:
        change(db)

    resp = approve(client, credential(auth), started["user_code"])

    assert resp.status_code == 403, resp.text
    assert refusal in resp.json()["detail"]
    assert only_request(db).status == "pending"
    assert error_of(poll(client, db, started["device_code"])) == "authorization_pending"


def test_a_token_never_approves_a_device(client: TestClient, auth: AuthService, db: Any) -> None:
    started = start(client)
    tokens = [
        client.post(
            "/auth/personal-tokens",
            headers=bearer(admin()),
            json={"name": "t", "surface": "cli", **flags},
        ).json()["token"]
        for flags in ({}, {"decide": True}, {"start_runs": True})
    ]
    _, unbound = auth.bootstrap_key(workspace_id=WS, name="ci", role=UserRole.ADMIN)

    for token in [*tokens, unbound]:
        resp = approve(client, token, started["user_code"])
        assert resp.status_code == 403, resp.text
        assert "minted by a person" in resp.json()["detail"]
    assert only_request(db).status == "pending"


def test_a_denial_is_final(client: TestClient, db: Any) -> None:
    started = start(client)

    resp = approve(client, admin(), started["user_code"], decision="deny")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"client_name": "laptop cli", "decision": "deny"}
    request = only_request(db)
    assert (request.status, request.user_id) == ("denied", ADMIN)
    assert error_of(poll(client, db, started["device_code"])) == "access_denied"
    # Nobody can overturn it.
    assert approve(client, admin(), started["user_code"]).json() == NOT_VALID
    assert error_of(poll(client, db, started["device_code"])) == "access_denied"


# --------------------------------------------------------------------------- #
# Criterion 3: pending, the token once, denied, expired                        #
# --------------------------------------------------------------------------- #


def test_the_token_is_returned_once_after_approval(client: TestClient, db: Any) -> None:
    started = start(client)
    assert error_of(poll(client, db, started["device_code"])) == "authorization_pending"
    assert approve(client, member(), started["user_code"]).status_code == 200

    issued = poll(client, db, started["device_code"])

    assert issued.status_code == 200, issued.text
    body = issued.json()
    assert (body["token_type"], body["scope"], body["expires_in"]) == (
        "Bearer",
        "read runs:execute",
        86_400,
    )
    assert issued.headers["cache-control"] == "no-store"
    assert client.get("/auth/me", headers=bearer(body["access_token"])).status_code == 200
    again = poll(client, db, started["device_code"])
    assert error_of(again) == "invalid_grant"
    assert "access_token" not in again.json()


def test_of_two_racing_polls_only_one_gets_a_token(
    client: TestClient, db: Any, auth: AuthService
) -> None:
    started = start(client)
    assert approve(client, member(), started["user_code"]).status_code == 200
    with db.factory() as late, db.factory() as early:
        # The late poll has read the request as approved before the early one took it.
        read_early = late.scalars(select(DeviceAuthorization)).one()
        won = device_code.collect(early, started["device_code"], auth.api_keys)
        lost = device_code.collect(late, started["device_code"], auth.api_keys)
        assert read_early.status == "issued"

    assert isinstance(won, tuple)
    assert lost == "invalid_grant"
    assert len(auth.api_keys.list_keys(WS)) == 1


def test_an_unknown_device_code_gets_nothing(client: TestClient, db: Any) -> None:
    start(client)
    assert error_of(poll(client, db, "not-a-device-code")) == "invalid_grant"


def _expire(db: Any) -> None:
    with db.factory() as s:
        past = datetime.now(UTC) - timedelta(seconds=1)
        s.execute(update(DeviceAuthorization).values(expires_at=past))
        s.commit()


def test_an_approved_request_expires_uncollected(client: TestClient, db: Any) -> None:
    started = start(client)
    assert approve(client, member(), started["user_code"]).status_code == 200
    _expire(db)

    assert error_of(poll(client, db, started["device_code"])) == "expired_token"


def test_an_expired_request_cannot_be_approved(client: TestClient, db: Any) -> None:
    started = start(client)
    _expire(db)

    assert approve(client, member(), started["user_code"]).json() == NOT_VALID
    assert only_request(db).status == "pending"
    assert error_of(poll(client, db, started["device_code"])) == "expired_token"


# --------------------------------------------------------------------------- #
# Criterion 4: slow down; repeated wrong user codes end the request            #
# --------------------------------------------------------------------------- #


def test_polling_faster_than_the_interval_is_told_to_slow_down(client: TestClient, db: Any) -> None:
    started = start(client)
    code = started["device_code"]

    assert error_of(poll(client, db, code, wait=False)) == "authorization_pending"
    assert error_of(poll(client, db, code, wait=False)) == "slow_down"
    assert error_of(poll(client, db, code, wait=True)) == "authorization_pending"


def test_repeated_wrong_user_codes_end_the_request(client: TestClient, db: Any) -> None:
    started = start(client)

    for _ in range(device_code.MAX_WRONG_CODES):
        assert approve(client, member(), wrong_code(started["user_code"])).json() == NOT_VALID

    assert approve(client, member(), started["user_code"]).json() == NOT_VALID
    assert only_request(db).status == "invalid"
    assert error_of(poll(client, db, started["device_code"])) == "expired_token"


def test_fewer_wrong_user_codes_leave_the_request_open(client: TestClient, db: Any) -> None:
    started = start(client)
    for _ in range(device_code.MAX_WRONG_CODES - 1):
        assert approve(client, member(), wrong_code(started["user_code"])).status_code == 400

    assert approve(client, member(), started["user_code"]).status_code == 200


def test_an_unknown_and_a_wrong_user_code_get_the_same_answer(client: TestClient) -> None:
    started = start(client)
    head = started["user_code"][:4]
    unknown = next(c * 4 for c in device_code.ALPHABET if c * 4 != head) + "-BBBB"

    assert approve(client, member(), unknown).json() == NOT_VALID
    assert approve(client, member(), wrong_code(started["user_code"])).json() == NOT_VALID


# --------------------------------------------------------------------------- #
# Criterion 5: hashes only, constant time, no secret in logs or audit          #
# --------------------------------------------------------------------------- #


def test_only_hashes_of_the_codes_are_stored(client: TestClient, db: Any) -> None:
    started = start(client)

    request = only_request(db)

    stored = repr({c.name: getattr(request, c.key) for c in DeviceAuthorization.__mapper__.columns})
    assert started["device_code"] not in stored
    assert started["user_code"].replace("-", "") not in stored
    assert started["user_code"] not in stored
    assert request.device_code_hash == hashlib.sha256(started["device_code"].encode()).hexdigest()


def test_the_user_code_is_compared_in_constant_time(
    client: TestClient, db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = start(client)
    compared: list[tuple[Any, Any]] = []
    real = hmac.compare_digest

    def spy(a: Any, b: Any) -> bool:
        compared.append((a, b))
        return real(a, b)

    monkeypatch.setattr(device_code.hmac, "compare_digest", spy)

    assert approve(client, member(), started["user_code"]).status_code == 200
    digest = hashlib.sha256(started["user_code"].replace("-", "").encode()).hexdigest()
    assert (only_request(db).user_code_hash, digest) in compared


def test_no_code_or_token_reaches_a_log_an_audit_entry_or_an_error(
    client: TestClient, db: Any, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    started = start(client)
    code = started["device_code"]
    errors = [
        poll(client, db, code, wait=False),
        poll(client, db, code, wait=False),
        approve(client, member(), wrong_code(started["user_code"])),
    ]
    assert approve(client, member(), started["user_code"]).status_code == 200
    token = poll(client, db, code).json()["access_token"]
    errors.append(poll(client, db, code))
    denied = start(client, "other")
    approve(client, admin(), denied["user_code"], decision="deny")
    errors.append(poll(client, db, denied["device_code"]))

    with db.factory() as s:
        audit = repr([(r.action, r.details) for r in s.scalars(select(AuditLog)).all()])
    logs = [f"{r.getMessage()} {r.args!r}" for r in caplog.records]
    assert "auth.device_token_issued" in audit
    assert logs, "expected the flow to log something"
    for secret in (
        code,
        started["user_code"],
        started["user_code"].replace("-", ""),
        token,
        denied["device_code"],
    ):
        assert secret not in audit
        assert all(secret not in line for line in logs)
        assert all(secret not in resp.text for resp in errors)


# --------------------------------------------------------------------------- #
# Criterion 6: request, approval, denial and issue are audited                 #
# --------------------------------------------------------------------------- #


def _audit(db: Any) -> list[AuditLog]:
    with db.factory() as s:
        return list(s.scalars(select(AuditLog).order_by(AuditLog.seq)).all())


def test_request_approval_and_issue_are_audited_with_client_and_approver(
    client: TestClient, db: Any
) -> None:
    issued = sign_in(client, db)

    rows = _audit(db)

    assert [r.action for r in rows] == [
        "auth.device_code_requested",
        "auth.device_code_approved",
        "auth.device_token_issued",
    ]
    for row in rows:
        assert (row.workspace_id, row.actor_id, row.result) == (WS, MEMBER, "success")
        assert row.details["client_name"] == "laptop cli"
    request = only_request(db)
    assert [r.target_id for r in rows] == [request.id, request.id, uuid.UUID(issued["key_id"])]
    assert rows[2].details["scopes"] == ["read", "runs:execute"]


def test_a_denial_is_audited_with_client_and_approver(client: TestClient, db: Any) -> None:
    started = start(client, "build box")
    approve(client, admin(), started["user_code"], decision="deny")

    rows = _audit(db)

    assert [r.action for r in rows] == ["auth.device_code_requested", "auth.device_code_denied"]
    assert all((r.actor_id, r.details["client_name"]) == (ADMIN, "build box") for r in rows)


# --------------------------------------------------------------------------- #
# Criterion 8: a run-start personal token of #206, revocable                   #
# --------------------------------------------------------------------------- #


def test_the_issued_token_is_a_revocable_run_start_personal_token(
    client: TestClient, db: Any
) -> None:
    before = datetime.now(UTC)
    issued = sign_in(client, db)
    token, key_id = issued["access_token"], issued["key_id"]

    keys = client.get("/auth/api-keys", headers=bearer(admin())).json()
    key = next(k for k in keys if k["id"] == key_id)
    assert (key["scopes"], key["user_id"], key["surface"], key["name"], key["role"]) == (
        ["read", "runs:execute"],
        str(MEMBER),
        "cli",
        "laptop cli",
        "viewer",
    )
    expires = datetime.fromisoformat(key["expires_at"])
    assert before + timedelta(days=1) <= expires <= datetime.now(UTC) + timedelta(days=1)
    me = client.get("/auth/me", headers=bearer(token)).json()
    assert (me["role"], me["decides"], me["narrowed"]) == ("agent-runner", False, True)
    gate = client.post(
        "/approvals",
        headers=bearer(member()),
        json={
            "gate_type": "pr",
            "subject_type": "changeset",
            "subject_id": str(uuid.uuid4()),
            "requested_actor": f"agent:{uuid.uuid4()}",
            "title": "pr gate",
        },
    )
    assert gate.status_code == 201, gate.text
    decision = client.post(
        f"/approvals/{gate.json()['id']}/decision",
        headers=bearer(token),
        json={"decision": "approve"},
    )
    assert decision.status_code == 403, decision.text

    assert client.delete(f"/auth/api-keys/{key_id}", headers=bearer(admin())).status_code == 204
    assert client.get("/auth/me", headers=bearer(token)).status_code == 401


def test_no_token_is_issued_once_the_approver_may_no_longer_run(
    client: TestClient, db: Any
) -> None:
    started = start(client)
    assert approve(client, member(), started["user_code"]).status_code == 200
    keys_before = len(client.get("/auth/api-keys", headers=bearer(admin())).json())
    _demote_member(db)

    assert error_of(poll(client, db, started["device_code"])) == "access_denied"
    assert len(client.get("/auth/api-keys", headers=bearer(admin())).json()) == keys_before
    refused = _audit(db)[-1]
    assert (refused.action, refused.result) == ("auth.device_token_issued", "denied")


def test_a_retried_poll_never_replays_the_token(client: TestClient, db: Any) -> None:
    started = start(client)
    assert approve(client, member(), started["user_code"]).status_code == 200
    retry = {"Idempotency-Key": "poll-1"}
    body = {"device_code": started["device_code"]}

    first = client.post("/auth/device/token", json=body, headers=retry)
    second = client.post("/auth/device/token", json=body, headers=retry)

    assert first.status_code == 200, first.text
    assert error_of(second) == "invalid_grant"
    assert "idempotency-replayed" not in second.headers


# --------------------------------------------------------------------------- #
# Criterion 7: scf-cli login                                                   #
# --------------------------------------------------------------------------- #


def _forward(client: TestClient) -> httpx.MockTransport:
    """Send the CLI's requests to the app under test."""

    def handle(request: httpx.Request) -> httpx.Response:
        resp = client.request(
            request.method,
            request.url.path,
            content=request.content,
            headers={"content-type": "application/json"},
        )
        return httpx.Response(resp.status_code, content=resp.content, headers=resp.headers)

    return httpx.MockTransport(handle)


def _login_args(*extra: str) -> Any:
    return cli_mod.build_parser().parse_args(
        ["login", "--url", "http://testserver", "--client-name", "laptop cli", *extra]
    )


def test_login_prints_only_a_shell_line_that_sets_the_named_variable(
    client: TestClient, db: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    out, err = io.StringIO(), io.StringIO()

    def person_approves(_seconds: float) -> None:
        # The person approves the code the client showed them, then the client polls.
        if only_request(db).status == "pending":
            shown = USER_CODE.search(err.getvalue())
            assert shown is not None, err.getvalue()
            assert approve(client, member(), shown.group()).status_code == 200

    code = cli_remote.login(
        _login_args("--token-env", "SCF_TOKEN"),
        env={},
        out=out,
        err=err,
        transport=_forward(client),
        sleep=person_approves,
    )

    assert code == 0, err.getvalue()
    line = out.getvalue()
    assert line.count("\n") == 1 and line.startswith("export SCF_TOKEN=")
    token = shlex.split(line)[1].removeprefix("SCF_TOKEN=")
    shell = subprocess.run(
        ["sh", "-c", f'{line}printf %s "$SCF_TOKEN"'], capture_output=True, text=True, check=True
    )
    assert shell.stdout == token
    me = client.get("/auth/me", headers=bearer(token)).json()
    assert (me["user_id"], me["role"]) == (str(MEMBER), "agent-runner")
    shown = err.getvalue()
    assert USER_CODE.search(shown) and "/auth/device/approve" in shown
    assert token not in shown
    written = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert all(token not in p.read_text(errors="ignore") for p in written)


def test_login_slows_down_when_told_and_stops_on_a_denial() -> None:
    answers = iter(["authorization_pending", "slow_down", "authorization_pending", "access_denied"])
    started = {
        "device_code": "dc",
        "user_code": "BCDF-GHJK",
        "verification_uri": "http://testserver/auth/device/approve",
        "expires_in": 600,
        "interval": 5,
    }

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/device/code":
            return httpx.Response(200, json=started)
        assert json.loads(request.content) == {"device_code": "dc"}
        return httpx.Response(400, json={"error": next(answers)})

    slept: list[float] = []
    out, err = io.StringIO(), io.StringIO()

    code = cli_remote.login(
        _login_args("--token-env", "SCF_TOKEN"),
        env={},
        out=out,
        err=err,
        transport=httpx.MockTransport(handle),
        sleep=slept.append,
    )

    assert code == cli_remote.EXIT_HTTP
    assert slept == [5, 5, 10, 10]
    assert out.getvalue() == ""
    assert "access_denied" in err.getvalue()


@pytest.mark.parametrize("name", ["X; rm -rf ~", "$(id)", "1TOKEN", "A-B", ""])
def test_login_refuses_a_variable_name_a_shell_would_run(name: str) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request may be sent")

    out, err = io.StringIO(), io.StringIO()

    code = cli_remote.login(
        _login_args("--token-env", name),
        env={},
        out=out,
        err=err,
        transport=httpx.MockTransport(handle),
        sleep=lambda _s: None,
    )

    assert code == cli_remote.EXIT_CONFIG
    assert out.getvalue() == ""
    assert "--token-env" in err.getvalue()
