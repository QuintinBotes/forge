"""SCF's MCP server at the current, handshake-free revision (spec 007 FR-001 to FR-006).

A current-revision request carries its protocol version in ``params._meta`` and
mirrors it, the method and (for ``tools/call``) the tool name into HTTP headers.
Legacy ``initialize`` clients are covered by ``test_mcp_server.py``.
"""

from __future__ import annotations

import base64
import json
import uuid
from collections.abc import Callable, Iterator

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from scf_api.db import get_db
from scf_api.deps import Principal
from scf_api.main import create_app
from scf_api.mcp_server.protocol import (
    CURRENT_VERSION,
    FORBIDDEN,
    HEADER_MISMATCH,
    INVALID_PARAMS,
    METHOD_NOT_FOUND,
    UNSUPPORTED_PROTOCOL_VERSION,
    Message,
    modern_version,
)
from scf_api.mcp_server.registry import McpTool, ToolContext, ToolRegistry
from scf_api.observability.audit import AuditCategory, AuditLog
from scf_api.routers.mcp_server import (
    MAX_BODY_BYTES,
    get_mcp_server_audit_log,
    get_mcp_tool_registry,
)
from scf_api.settings import get_settings
from scf_contracts import UserRole

WORKSPACE_ID = uuid.UUID("00000000-0000-0000-0000-0000000000e5")
META = {
    "io.modelcontextprotocol/protocolVersion": CURRENT_VERSION,
    "io.modelcontextprotocol/clientInfo": {"name": "scf-tests", "version": "1.0.0"},
    "io.modelcontextprotocol/clientCapabilities": {},
}
#: Every argument a tool handler received, so a test can prove no tool ran.
CALLS: list[str] = []


class EchoIn(BaseModel):
    text: str


class EchoOut(BaseModel):
    text: str


class ProposeIn(BaseModel):
    claim: str


def _echo(_: ToolContext, args: EchoIn) -> EchoOut:
    CALLS.append(args.text)
    return EchoOut(text=args.text)


def _propose(_: ToolContext, args: ProposeIn) -> EchoOut:
    CALLS.append(args.claim)
    return EchoOut(text=args.claim)


REGISTRY = ToolRegistry(
    [
        McpTool("echo", "Echo text back.", EchoIn, _echo),
        McpTool("propose", "Create a proposal.", ProposeIn, _propose, write=True),
        McpTool("shout", "Echo text back, louder.", EchoIn, _echo),
    ]
)


@pytest.fixture(autouse=True)
def _reset_calls() -> None:
    CALLS.clear()


@pytest.fixture
def audit() -> AuditLog:
    return AuditLog()


@pytest.fixture
def make_client(
    authenticate_app: Callable[..., FastAPI], audit: AuditLog
) -> Iterator[Callable[..., TestClient]]:
    clients: list[TestClient] = []

    def _make(role: UserRole = UserRole.MEMBER, origins: list[str] | None = None) -> TestClient:
        app = create_app()
        authenticate_app(
            app,
            Principal(
                user_id=uuid.uuid4(), workspace_id=WORKSPACE_ID, role=role, auth_method="test"
            ),
        )
        app.dependency_overrides[get_mcp_tool_registry] = lambda: REGISTRY
        app.dependency_overrides[get_mcp_server_audit_log] = lambda: audit
        app.dependency_overrides[get_db] = lambda: None
        if origins is not None:
            settings = get_settings().model_copy(update={"mcp_allowed_origins": origins})
            app.dependency_overrides[get_settings] = lambda: settings
        client = TestClient(app)
        clients.append(client)
        return client

    yield _make
    for client in clients:
        client.close()


def post(
    client: TestClient,
    method: str,
    params: dict | None = None,
    *,
    headers: dict[str, str | None] | None = None,
    meta: dict | None = None,
    rpc_id: int = 7,
) -> httpx.Response:
    """POST a current-revision request; ``headers`` overrides (``None`` drops) a header."""
    body_params = {"_meta": META if meta is None else meta, **(params or {})}
    sent: dict[str, str | None] = {"MCP-Protocol-Version": CURRENT_VERSION, "Mcp-Method": method}
    if method == "tools/call":
        sent["Mcp-Name"] = body_params.get("name")
    sent.update(headers or {})
    return client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": body_params},
        headers={key: value for key, value in sent.items() if value is not None},
    )


def ok(resp: httpx.Response) -> dict:
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "error" not in body, body
    return body["result"]


def error(resp: httpx.Response, status: int, code: int) -> dict:
    assert resp.status_code == status, resp.text
    body = resp.json()
    assert body["error"]["code"] == code, body
    assert body["id"] == 7
    return body["error"]


# --------------------------------------------------------------------------- #
# server/discover, tools/list, tools/call                                      #
# --------------------------------------------------------------------------- #


def test_discover_answers_without_an_earlier_request(
    make_client: Callable[..., TestClient],
) -> None:
    result = ok(post(make_client(), "server/discover"))
    assert result["supportedVersions"] == [CURRENT_VERSION, "2025-06-18", "2025-03-26"]
    assert result["capabilities"] == {"tools": {"listChanged": False}}
    server_info = result["_meta"]["io.modelcontextprotocol/serverInfo"]
    assert server_info["name"] and server_info["version"]
    assert "as_of" in result["instructions"]
    assert result["resultType"] == "complete"
    assert isinstance(result["ttlMs"], int) and result["ttlMs"] >= 0
    assert result["cacheScope"] == "private"


def test_tools_list_is_complete_private_and_stable(
    make_client: Callable[..., TestClient],
) -> None:
    client = make_client()
    results = [ok(post(client, "tools/list")) for _ in range(3)]
    for result in results:
        assert result["resultType"] == "complete"
        assert isinstance(result["ttlMs"], int) and result["ttlMs"] >= 0
        assert result["cacheScope"] == "private"
    orders = [[tool["name"] for tool in result["tools"]] for result in results]
    assert orders == [["echo", "propose", "shout"]] * 3


def test_tools_call_runs_without_initialize(
    make_client: Callable[..., TestClient], audit: AuditLog
) -> None:
    result = ok(post(make_client(), "tools/call", {"name": "echo", "arguments": {"text": "hi"}}))
    assert result["resultType"] == "complete"
    assert result["isError"] is False
    assert result["structuredContent"] == {"text": "hi"}
    assert CALLS == ["hi"]
    (entry,) = audit.query(category=AuditCategory.MCP_CALL)
    assert (entry.target, entry.status) == ("echo", "ok")


def test_base64_encoded_tool_name_header_is_decoded(
    make_client: Callable[..., TestClient],
) -> None:
    encoded = "=?base64?" + base64.b64encode(b"echo").decode() + "?="
    resp = post(
        make_client(),
        "tools/call",
        {"name": "echo", "arguments": {"text": "hi"}},
        headers={"Mcp-Name": encoded},
    )
    assert ok(resp)["structuredContent"] == {"text": "hi"}


def test_header_names_are_case_insensitive(make_client: Callable[..., TestClient]) -> None:
    resp = post(
        make_client(),
        "tools/list",
        headers={"Mcp-Method": None, "MCP-METHOD": "tools/list"},
    )
    assert ok(resp)["tools"]


# --------------------------------------------------------------------------- #
# header validation                                                            #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "headers",
    [
        {"MCP-Protocol-Version": None},
        {"MCP-Protocol-Version": "2025-06-18"},
        {"Mcp-Method": None},
        {"Mcp-Method": "tools/list"},
        {"Mcp-Method": "TOOLS/CALL"},
        {"Mcp-Name": None},
        {"Mcp-Name": "shout"},
        {"Mcp-Name": "=?base64?not base64!?="},
    ],
    ids=[
        "version-missing",
        "version-differs",
        "method-missing",
        "method-differs",
        "method-case-differs",
        "name-missing",
        "name-differs",
        "name-malformed",
    ],
)
def test_header_mismatch_is_400_and_runs_nothing(
    make_client: Callable[..., TestClient], audit: AuditLog, headers: dict[str, str | None]
) -> None:
    resp = post(
        make_client(), "tools/call", {"name": "echo", "arguments": {"text": "x"}}, headers=headers
    )
    error(resp, 400, HEADER_MISMATCH)
    assert CALLS == []
    assert audit.query(category=AuditCategory.MCP_CALL) == []


def test_version_header_alone_marks_a_current_request(
    make_client: Callable[..., TestClient],
) -> None:
    """A current version header with no ``_meta`` is not served as a legacy request."""
    resp = make_client().post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 7, "method": "tools/list"},
        headers={"MCP-Protocol-Version": CURRENT_VERSION, "Mcp-Method": "tools/list"},
    )
    error(resp, 400, INVALID_PARAMS)


@pytest.mark.parametrize(
    "drop",
    ["io.modelcontextprotocol/protocolVersion", "io.modelcontextprotocol/clientCapabilities"],
)
def test_meta_without_a_required_key_is_400_invalid_params(
    make_client: Callable[..., TestClient], drop: str
) -> None:
    meta = {key: value for key, value in META.items() if key != drop}
    error(post(make_client(), "server/discover", meta=meta), 400, INVALID_PARAMS)


def test_unsupported_version_is_400_and_lists_supported(
    make_client: Callable[..., TestClient],
) -> None:
    resp = post(
        make_client(),
        "tools/list",
        meta={**META, "io.modelcontextprotocol/protocolVersion": "1999-01-01"},
        headers={"MCP-Protocol-Version": "1999-01-01"},
    )
    err = error(resp, 400, UNSUPPORTED_PROTOCOL_VERSION)
    assert err["data"] == {
        "supported": [CURRENT_VERSION, "2025-06-18", "2025-03-26"],
        "requested": "1999-01-01",
    }


@pytest.mark.parametrize("method", ["ping", "initialize", "prompts/list", "unknown/method"])
def test_unimplemented_method_is_404(make_client: Callable[..., TestClient], method: str) -> None:
    error(post(make_client(), method), 404, METHOD_NOT_FOUND)


def test_legacy_requests_keep_their_shape(make_client: Callable[..., TestClient]) -> None:
    """A request with a handshake-era header and no ``_meta`` takes the legacy path."""
    resp = make_client().post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"MCP-Protocol-Version": "2025-06-18"},
    )
    assert resp.status_code == 200
    assert set(resp.json()["result"]) == {"tools"}


def test_era_detection_is_pure() -> None:
    legacy = Message(method="tools/list")
    assert modern_version({}, legacy) is None
    assert modern_version({"mcp-protocol-version": "2025-06-18"}, legacy) is None
    current = Message(method="tools/list", params={"_meta": META})
    headers = {"mcp-protocol-version": CURRENT_VERSION, "mcp-method": "tools/list"}
    assert modern_version(headers, current) == CURRENT_VERSION


# --------------------------------------------------------------------------- #
# security (spec I section 4) for current-revision requests                    #
# --------------------------------------------------------------------------- #


def test_unauthenticated_current_request_is_401_before_parsing() -> None:
    with TestClient(create_app()) as client:
        resp = client.post(
            "/mcp",
            content=b"{not json",
            headers={"MCP-Protocol-Version": CURRENT_VERSION, "Mcp-Method": "tools/list"},
        )
    assert resp.status_code == 401


def test_foreign_origin_is_403(make_client: Callable[..., TestClient]) -> None:
    client = make_client(origins=["https://scf.example.com"])
    assert (
        post(client, "tools/list", headers={"Origin": "https://scf.example.com"}).status_code == 200
    )
    assert post(client, "tools/list", headers={"Origin": "https://evil.example"}).status_code == 403


def test_oversized_current_request_is_413(make_client: Callable[..., TestClient]) -> None:
    resp = post(make_client(), "tools/list", {"pad": "x" * (MAX_BODY_BYTES + 1)})
    assert resp.status_code == 413


def test_viewer_neither_sees_nor_calls_write_tools(
    make_client: Callable[..., TestClient], audit: AuditLog
) -> None:
    client = make_client(UserRole.VIEWER)
    assert [t["name"] for t in ok(post(client, "tools/list"))["tools"]] == ["echo", "shout"]
    resp = post(client, "tools/call", {"name": "propose", "arguments": {"claim": "c"}})
    assert resp.json()["error"]["code"] == FORBIDDEN
    assert CALLS == []
    (entry,) = audit.query(category=AuditCategory.MCP_CALL)
    assert entry.status == "refused"


def test_one_audit_event_per_call_with_a_hash_and_no_argument_text(
    make_client: Callable[..., TestClient], audit: AuditLog
) -> None:
    client = make_client()
    ok(post(client, "tools/call", {"name": "echo", "arguments": {"text": "sensitive words"}}))
    ok(post(client, "tools/list"))
    ok(post(client, "server/discover"))
    (entry,) = audit.query(category=AuditCategory.MCP_CALL)
    assert entry.payload_hash and len(entry.payload_hash) == 64
    assert "sensitive words" not in json.dumps(entry.model_dump(mode="json"))
