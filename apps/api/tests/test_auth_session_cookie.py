"""The SSO session cookie authenticates the API, ``/ws`` and sign-out (#220, FR-001..005).

The OIDC and SAML callbacks set the HTTP-only ``forge_session`` cookie to a 12-hour
session key. A header credential always wins over it; a state-changing request it
alone authenticates must come from SCF's own origin (``public_url``); a socket opened
with it must come from SCF's own pages and closes at the API-key socket lifetime, so
a signed-out session stops receiving; sign-out revokes the key the cookie carries.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import scf_api.auth.service as auth_service_module
from scf_api.auth.service import AuthService
from scf_api.deps import CurrentPrincipal
from scf_api.main import create_app
from scf_api.realtime.manager import ConnectionManager, get_connection_manager
from scf_api.routers.realtime import get_api_key_socket_lifetime
from scf_api.settings import get_settings
from scf_contracts.enums import APIKeyKind, UserRole

WS = uuid.UUID("00000000-0000-0000-0000-0000000220a1")
OTHER_WS = uuid.UUID("00000000-0000-0000-0000-0000000220a2")
PERSON = uuid.UUID("00000000-0000-0000-0000-0000000220b1")
COOKIE = "forge_session"
FOREIGN = "https://evil.example"


def own_origin() -> str:
    parts = urlsplit(get_settings().public_url)
    return f"{parts.scheme}://{parts.netloc}"


@pytest.fixture
def auth(monkeypatch: pytest.MonkeyPatch) -> AuthService:
    service = AuthService(secret_key=b"2" * 32)
    # ``get_current_principal`` calls ``get_auth_service()`` directly, so patch the singleton.
    monkeypatch.setattr(auth_service_module, "_default_service", service)
    return service


@pytest.fixture
def manager() -> ConnectionManager:
    return ConnectionManager()


@pytest.fixture
def app(manager: ConnectionManager) -> FastAPI:
    app = create_app()
    app.dependency_overrides[get_connection_manager] = lambda: manager
    return app


@pytest.fixture
def client(app: FastAPI, auth: AuthService) -> Iterator[TestClient]:
    with TestClient(app) as client:
        yield client


def session_key(auth: AuthService, *, expires_in: timedelta = timedelta(hours=12)) -> tuple:
    """A key exactly as the OIDC/SAML callback mints it for the session cookie."""
    return auth.bootstrap_key(
        workspace_id=WS,
        name=f"sso-session-{uuid.uuid4().hex[:8]}",
        role=UserRole.ADMIN,
        user_id=PERSON,
        kind=APIKeyKind.SYSTEM,
        expires_at=datetime.now(UTC) + expires_in,
        surface="web",
    )


def with_cookie(token: str, **headers: str) -> dict[str, str]:
    return {"cookie": f"{COOKIE}={token}", **headers}


# --------------------------------------------------------------------------- #
# Criterion 1: the cookie resolves the same principal as the bearer header     #
# --------------------------------------------------------------------------- #


def test_the_cookie_resolves_the_same_principal_as_the_same_token_as_a_bearer(
    client: TestClient, auth: AuthService
) -> None:
    _, token = session_key(auth)

    by_cookie = client.get("/auth/me", headers=with_cookie(token))
    by_bearer = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert by_cookie.status_code == 200, by_cookie.text
    assert by_cookie.json() == by_bearer.json()
    body = by_cookie.json()
    assert (body["user_id"], body["workspace_id"], body["role"]) == (
        str(PERSON),
        str(WS),
        "admin",
    )


def test_an_expired_session_cookie_is_401(client: TestClient, auth: AuthService) -> None:
    _, token = session_key(auth, expires_in=timedelta(seconds=-1))
    assert client.get("/auth/me", headers=with_cookie(token)).status_code == 401


def test_a_revoked_session_cookie_is_401(client: TestClient, auth: AuthService) -> None:
    info, token = session_key(auth)
    assert auth.api_keys.revoke(WS, info.id)
    assert client.get("/auth/me", headers=with_cookie(token)).status_code == 401


def test_an_unknown_session_cookie_is_401(client: TestClient, auth: AuthService) -> None:
    assert client.get("/auth/me", headers=with_cookie("not-a-key")).status_code == 401


# --------------------------------------------------------------------------- #
# Criterion 2: header keys behave as before; a header wins over the cookie     #
# --------------------------------------------------------------------------- #


def test_a_header_key_wins_over_the_cookie(client: TestClient, auth: AuthService) -> None:
    _, cookie_token = session_key(auth)
    _, header_key = auth.bootstrap_key(workspace_id=OTHER_WS, name="ci", role=UserRole.MEMBER)

    for headers in ({"Authorization": f"Bearer {header_key}"}, {"X-API-Key": header_key}):
        resp = client.get("/auth/me", headers=with_cookie(cookie_token, **headers))
        assert resp.status_code == 200, resp.text
        assert (resp.json()["workspace_id"], resp.json()["role"]) == (str(OTHER_WS), "member")


def test_an_invalid_header_key_is_401_even_with_a_valid_cookie(
    client: TestClient, auth: AuthService
) -> None:
    _, cookie_token = session_key(auth)
    resp = client.get("/auth/me", headers=with_cookie(cookie_token, Authorization="Bearer nope"))
    assert resp.status_code == 401


def test_a_header_key_without_a_cookie_behaves_as_before(
    client: TestClient, auth: AuthService
) -> None:
    _, key = auth.bootstrap_key(workspace_id=WS, name="script", role=UserRole.ADMIN)
    me = client.get("/auth/me", headers={"X-API-Key": key})
    assert (me.status_code, me.json()["auth_method"]) == (200, "api_key")
    created = client.post(
        "/auth/api-keys", headers={"Authorization": f"Bearer {key}"}, json={"name": "k"}
    )
    assert created.status_code == 201, created.text


# --------------------------------------------------------------------------- #
# Criterion 3: a cookie-only state change must come from SCF's own origin      #
# --------------------------------------------------------------------------- #


def _create_key(client: TestClient, headers: dict[str, str]) -> int:
    return client.post("/auth/api-keys", headers=headers, json={"name": "via-cookie"}).status_code


def test_a_cookie_only_state_change_from_another_origin_is_403(
    client: TestClient, auth: AuthService
) -> None:
    _, token = session_key(auth)
    assert _create_key(client, with_cookie(token, origin=FOREIGN)) == 403
    assert _create_key(client, with_cookie(token, origin="null")) == 403
    # ``Origin`` decides when present, whatever ``Sec-Fetch-Site`` says.
    spoofed = with_cookie(token, origin=FOREIGN, **{"sec-fetch-site": "same-origin"})
    assert _create_key(client, spoofed) == 403
    assert all(k.name != "via-cookie" for k in auth.api_keys.list_keys(WS))


def test_a_cookie_only_state_change_from_scfs_own_origin_succeeds(
    client: TestClient, auth: AuthService
) -> None:
    _, token = session_key(auth)
    assert _create_key(client, with_cookie(token, origin=own_origin())) == 201


def test_a_cookie_only_state_change_without_an_origin_needs_sec_fetch_site_same_origin(
    client: TestClient, auth: AuthService
) -> None:
    _, token = session_key(auth)
    assert _create_key(client, with_cookie(token)) == 403
    assert _create_key(client, with_cookie(token, **{"sec-fetch-site": "cross-site"})) == 403
    assert _create_key(client, with_cookie(token, **{"sec-fetch-site": "same-site"})) == 403
    assert _create_key(client, with_cookie(token, **{"sec-fetch-site": "same-origin"})) == 201


def test_a_cookie_read_from_another_origin_is_not_refused(
    client: TestClient, auth: AuthService
) -> None:
    _, token = session_key(auth)
    for method in ("GET", "HEAD", "OPTIONS"):
        resp = client.request(method, "/auth/me", headers=with_cookie(token, origin=FOREIGN))
        assert resp.status_code != 403, (method, resp.status_code)


def test_a_header_authenticated_state_change_is_not_subject_to_the_origin_rule(
    client: TestClient, auth: AuthService
) -> None:
    _, token = session_key(auth)
    _, key = auth.bootstrap_key(workspace_id=WS, name="script", role=UserRole.ADMIN)
    headers = with_cookie(token, origin=FOREIGN, Authorization=f"Bearer {key}")
    assert _create_key(client, headers) == 201


def test_feature_routes_read_the_cookie_under_the_same_origin_rule(auth: AuthService) -> None:
    """Feature routers authenticate through ``scf_api.deps.get_current_principal``."""
    probe = FastAPI()

    @probe.api_route("/probe", methods=["GET", "POST"])
    def _probe(principal: CurrentPrincipal) -> str:
        return str(principal.workspace_id)

    _, token = session_key(auth)
    with TestClient(probe) as client:
        assert client.get("/probe").status_code == 401
        read = client.get("/probe", headers=with_cookie(token, origin=FOREIGN))
        assert (read.status_code, read.json()) == (200, str(WS))
        assert client.post("/probe", headers=with_cookie(token, origin=FOREIGN)).status_code == 403
        assert client.post("/probe", headers=with_cookie(token)).status_code == 403
        own = client.post("/probe", headers=with_cookie(token, origin=own_origin()))
        assert (own.status_code, own.json()) == (200, str(WS))


# --------------------------------------------------------------------------- #
# Criterion 4: /ws authenticates with the cookie alone                         #
# --------------------------------------------------------------------------- #


def wait_connected(manager: ConnectionManager, count: int = 1) -> None:
    deadline = time.monotonic() + 5
    while manager.connection_count(WS) != count:
        assert time.monotonic() < deadline, f"never reached {count} socket(s)"
        time.sleep(0.01)


def test_ws_authenticates_with_the_session_cookie_alone(
    client: TestClient, auth: AuthService, manager: ConnectionManager
) -> None:
    _, token = session_key(auth)
    with client.websocket_connect("/ws", headers=with_cookie(token, origin=own_origin())) as ws:
        wait_connected(manager)
        ws.portal.call(manager.broadcast, WS, {"type": "task.updated"})
        assert ws.receive_json() == {"type": "task.updated"}


@pytest.mark.parametrize("origin", [FOREIGN, "null", None])
def test_ws_refuses_the_cookie_from_another_origin(
    client: TestClient, auth: AuthService, origin: str | None
) -> None:
    # A browser sends the cookie on any page's handshake: only SCF's own pages may use it.
    _, token = session_key(auth)
    headers = with_cookie(token, **({"origin": origin} if origin else {}))
    with (
        client.websocket_connect("/ws", headers=headers) as ws,
        pytest.raises(WebSocketDisconnect) as exc,
    ):
        ws.receive_json()
    assert exc.value.code == 1008


def test_the_spec_socket_refuses_the_cookie_from_another_origin(
    client: TestClient, auth: AuthService
) -> None:
    _, token = session_key(auth)
    with (
        client.websocket_connect(
            f"/ws/spec/{uuid.uuid4()}", headers=with_cookie(token, origin=FOREIGN)
        ) as ws,
        pytest.raises(WebSocketDisconnect) as exc,
    ):
        ws.receive_bytes()
    assert exc.value.code == 1008


def test_ws_refuses_a_revoked_session_cookie(client: TestClient, auth: AuthService) -> None:
    info, token = session_key(auth)
    assert auth.api_keys.revoke(WS, info.id)
    with (
        client.websocket_connect("/ws", headers=with_cookie(token, origin=own_origin())) as ws,
        pytest.raises(WebSocketDisconnect) as exc,
    ):
        ws.receive_json()
    assert exc.value.code == 1008


def test_a_cookie_socket_closes_at_the_api_key_socket_lifetime(
    app: FastAPI, client: TestClient, auth: AuthService, manager: ConnectionManager
) -> None:
    # Decision: sign-out revokes the cookie's key, so a cookie socket follows the header-key
    # rule (spec L 6a): it closes (1000) at the lifetime and the client reconnects.
    app.dependency_overrides[get_api_key_socket_lifetime] = lambda: 0.2
    _, token = session_key(auth)
    with client.websocket_connect("/ws", headers=with_cookie(token, origin=own_origin())) as ws:
        wait_connected(manager)
        wait_connected(manager, 0)  # the server closed it; fails rather than hangs
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
    assert closed.value.code == 1000


def test_a_query_token_still_wins_over_the_cookie_on_ws(
    app: FastAPI, client: TestClient, auth: AuthService, manager: ConnectionManager
) -> None:
    # The web's ``?token=`` socket with an unscoped key keeps its unbounded behaviour.
    app.dependency_overrides[get_api_key_socket_lifetime] = lambda: 0.05
    _, cookie_token = session_key(auth)
    _, key = auth.bootstrap_key(workspace_id=WS, name="web", role=UserRole.MEMBER, user_id=PERSON)
    headers = with_cookie(cookie_token, origin=own_origin())
    with client.websocket_connect(f"/ws?token={key}", headers=headers) as ws:
        wait_connected(manager)
        time.sleep(0.3)
        ws.portal.call(manager.broadcast, WS, {"type": "task.updated"})
        assert ws.receive_json() == {"type": "task.updated"}


# --------------------------------------------------------------------------- #
# Criterion 5: sign-out revokes the cookie's key and clears the cookie         #
# --------------------------------------------------------------------------- #


def test_logout_revokes_the_session_key_and_clears_the_cookie(
    client: TestClient, auth: AuthService
) -> None:
    info, token = session_key(auth)
    _, other_token = session_key(auth)  # the same person's other session stays signed in

    resp = client.post("/auth/logout", headers=with_cookie(token, origin=own_origin()))

    assert resp.status_code == 204
    cleared = resp.headers["set-cookie"]
    assert cleared.startswith(f'{COOKIE}=""') or cleared.startswith(f"{COOKIE}=;")
    assert "Max-Age=0" in cleared and "HttpOnly" in cleared
    assert client.get("/auth/me", headers=with_cookie(token)).status_code == 401
    assert client.get("/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401
    assert client.get("/auth/me", headers=with_cookie(other_token)).status_code == 200
    assert next(k for k in auth.api_keys.list_keys(WS) if k.id == info.id).is_active is False


def test_logout_without_a_cookie_is_still_204(client: TestClient, auth: AuthService) -> None:
    resp = client.post("/auth/logout")
    assert resp.status_code == 204
    assert "set-cookie" not in resp.headers


def test_logout_with_a_dead_cookie_still_clears_it(client: TestClient, auth: AuthService) -> None:
    resp = client.post("/auth/logout", headers=with_cookie("not-a-key", origin=own_origin()))
    assert resp.status_code == 204
    assert resp.headers["set-cookie"].startswith(COOKIE)


def test_logout_from_another_origin_is_refused_and_revokes_nothing(
    client: TestClient, auth: AuthService
) -> None:
    _, token = session_key(auth)
    assert (
        client.post("/auth/logout", headers=with_cookie(token, origin=FOREIGN)).status_code == 403
    )
    assert client.get("/auth/me", headers=with_cookie(token)).status_code == 200
