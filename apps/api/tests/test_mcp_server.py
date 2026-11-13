"""Forge's own MCP server (spec I): protocol, permissions, limits and audit."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from forge_api.db import get_db
from forge_api.deps import Principal
from forge_api.main import create_app
from forge_api.mcp_server.protocol import (
    FORBIDDEN,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    negotiate_version,
)
from forge_api.mcp_server.registry import (
    MAX_TEXT_CHARS,
    McpTool,
    ToolContext,
    ToolError,
    ToolRegistry,
)
from forge_api.observability.audit import AuditCategory, AuditLog
from forge_api.routers.mcp_server import (
    MAX_BODY_BYTES,
    get_mcp_server_audit_log,
    get_mcp_tool_registry,
)
from forge_api.settings import Settings, get_settings
from forge_contracts import UserRole

SECRET = "ghp_" + "a" * 36
WORKSPACE_ID = uuid.UUID("00000000-0000-0000-0000-0000000000c3")


def make_test_principal(role: UserRole = UserRole.MEMBER) -> Principal:
    return Principal(
        user_id=uuid.UUID("00000000-0000-0000-0000-0000000000d4"),
        workspace_id=WORKSPACE_ID,
        role=role,
        auth_method="test",
    )


class EchoIn(BaseModel):
    text: str


class EchoOut(BaseModel):
    text: str
    workspace: str


class ProposeIn(BaseModel):
    claim: str


class ProposeOut(BaseModel):
    proposal: str


def _echo(ctx: ToolContext, args: EchoIn) -> EchoOut:
    if args.text == "missing":
        raise ToolError("not found")
    return EchoOut(text=args.text, workspace=str(ctx.principal.workspace_id))


def _propose(_: ToolContext, args: ProposeIn) -> ProposeOut:
    return ProposeOut(proposal=f"proposed: {args.claim}")


REGISTRY = ToolRegistry(
    [
        McpTool("echo", "Echo text back.", EchoIn, _echo),
        McpTool("propose", "Create a proposal.", ProposeIn, _propose, write=True),
    ]
)


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
        authenticate_app(app, make_test_principal(role))
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


def rpc(client: TestClient, method: str, params: dict | None = None, **kw: object) -> dict:
    body: dict[str, object] = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        body["params"] = params
    resp = client.post("/mcp", json=body, **kw)  # type: ignore[arg-type]
    assert resp.status_code == 200, resp.text
    return resp.json()


# --------------------------------------------------------------------------- #
# protocol                                                                     #
# --------------------------------------------------------------------------- #


def test_version_negotiation_echoes_supported_and_defaults_otherwise() -> None:
    assert negotiate_version("2025-03-26") == "2025-03-26"
    assert negotiate_version("1999-01-01") == "2025-06-18"
    assert negotiate_version(None) == "2025-06-18"


def test_initialize_advertises_tools_and_instructions(
    make_client: Callable[..., TestClient],
) -> None:
    reply = rpc(make_client(), "initialize", {"protocolVersion": "2025-03-26"})
    result = reply["result"]
    assert result["protocolVersion"] == "2025-03-26"
    assert result["serverInfo"]["name"] == "forge"
    assert result["capabilities"] == {"tools": {"listChanged": False}}
    assert "as_of" in result["instructions"]


def test_notification_and_client_response_are_acknowledged_without_body(
    make_client: Callable[..., TestClient],
) -> None:
    client = make_client()
    note = client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert note.status_code == 202 and note.content == b""
    response = client.post("/mcp", json={"jsonrpc": "2.0", "id": 9, "result": {}})
    assert response.status_code == 202


@pytest.mark.parametrize(
    ("body", "code"),
    [
        (b"{not json", PARSE_ERROR),
        (b'[{"jsonrpc":"2.0","id":1,"method":"ping"}]', INVALID_REQUEST),
        (b'{"jsonrpc":"1.0","id":1,"method":"ping"}', INVALID_REQUEST),
        (b'{"jsonrpc":"2.0","id":1,"method":"nope"}', METHOD_NOT_FOUND),
        (b'{"jsonrpc":"2.0","id":1,"method":"ping","params":[1]}', INVALID_PARAMS),
    ],
)
def test_malformed_messages_map_to_jsonrpc_errors(
    make_client: Callable[..., TestClient], body: bytes, code: int
) -> None:
    resp = make_client().post("/mcp", content=body, headers={"content-type": "application/json"})
    assert resp.status_code == 200
    assert resp.json()["error"]["code"] == code


def test_ping(make_client: Callable[..., TestClient]) -> None:
    assert rpc(make_client(), "ping")["result"] == {}


def test_get_is_method_not_allowed(make_client: Callable[..., TestClient]) -> None:
    resp = make_client().get("/mcp")
    assert resp.status_code == 405
    assert resp.headers["allow"] == "POST"


# --------------------------------------------------------------------------- #
# security                                                                     #
# --------------------------------------------------------------------------- #


def test_unauthenticated_request_is_401_before_parsing() -> None:
    with TestClient(create_app()) as client:
        resp = client.post("/mcp", content=b"{not json")
    assert resp.status_code == 401


def test_foreign_origin_is_refused(make_client: Callable[..., TestClient]) -> None:
    client = make_client(origins=["https://forge.example.com"])
    ok = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        headers={"origin": "https://forge.example.com"},
    )
    assert ok.status_code == 200
    bad = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        headers={"origin": "https://evil.example"},
    )
    assert bad.status_code == 403


def test_oversized_body_is_413(make_client: Callable[..., TestClient]) -> None:
    padding = "x" * (MAX_BODY_BYTES + 1)
    resp = make_client().post("/mcp", content=json.dumps({"pad": padding}).encode())
    assert resp.status_code == 413


def test_viewer_sees_only_read_tools(make_client: Callable[..., TestClient]) -> None:
    tools = rpc(make_client(UserRole.VIEWER), "tools/list")["result"]["tools"]
    assert [t["name"] for t in tools] == ["echo"]
    assert tools[0]["inputSchema"]["properties"]["text"]["type"] == "string"


@pytest.mark.parametrize("role", [UserRole.MEMBER, UserRole.AGENT_RUNNER])
def test_writers_see_write_tools(make_client: Callable[..., TestClient], role: UserRole) -> None:
    tools = rpc(make_client(role), "tools/list")["result"]["tools"]
    assert {t["name"] for t in tools} == {"echo", "propose"}


def test_viewer_calling_hidden_write_tool_is_forbidden(
    make_client: Callable[..., TestClient], audit: AuditLog
) -> None:
    reply = rpc(
        make_client(UserRole.VIEWER), "tools/call", {"name": "propose", "arguments": {"claim": "x"}}
    )
    assert reply["error"]["code"] == FORBIDDEN
    (entry,) = audit.query(category=AuditCategory.MCP_CALL)
    assert entry.status == "refused"


# --------------------------------------------------------------------------- #
# tools/call                                                                   #
# --------------------------------------------------------------------------- #


def test_call_returns_structured_and_text_content(make_client: Callable[..., TestClient]) -> None:
    result = rpc(make_client(), "tools/call", {"name": "echo", "arguments": {"text": "hi"}})[
        "result"
    ]
    assert result["isError"] is False
    assert result["structuredContent"]["text"] == "hi"
    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]


def test_domain_error_is_in_band(make_client: Callable[..., TestClient]) -> None:
    result = rpc(make_client(), "tools/call", {"name": "echo", "arguments": {"text": "missing"}})[
        "result"
    ]
    assert result["isError"] is True
    assert result["structuredContent"] == {"error": "not found"}


@pytest.mark.parametrize(
    "params",
    [
        {"name": "nope"},
        {"arguments": {}},
        {"name": "echo", "arguments": {"wrong": 1}},
        {"name": "echo", "arguments": [1]},
    ],
)
def test_bad_call_params_are_invalid_params(
    make_client: Callable[..., TestClient], params: dict
) -> None:
    assert rpc(make_client(), "tools/call", params)["error"]["code"] == INVALID_PARAMS


def test_results_are_redacted_and_truncated(make_client: Callable[..., TestClient]) -> None:
    long_text = f"token {SECRET} " + "y" * (MAX_TEXT_CHARS + 50)
    result = rpc(make_client(), "tools/call", {"name": "echo", "arguments": {"text": long_text}})[
        "result"
    ]
    text = result["structuredContent"]["text"]
    assert SECRET not in text
    assert len(text) == MAX_TEXT_CHARS
    assert result["structuredContent"]["truncated"] is True


def test_every_call_is_audited_with_a_hash_not_the_arguments(
    make_client: Callable[..., TestClient], audit: AuditLog
) -> None:
    client = make_client()
    rpc(client, "tools/call", {"name": "echo", "arguments": {"text": "sensitive words"}})
    rpc(client, "tools/call", {"name": "echo", "arguments": {"text": "missing"}})
    rpc(client, "tools/list")
    entries = audit.query(category=AuditCategory.MCP_CALL)
    assert [(e.action, e.target, e.status) for e in entries] == [
        ("mcp.tool_called", "echo", "ok"),
        ("mcp.tool_called", "echo", "error"),
    ]
    dumped = json.dumps([e.model_dump(mode="json") for e in entries])
    assert "sensitive words" not in dumped
    assert all(e.payload_hash and len(e.payload_hash) == 64 for e in entries)
    assert all(e.workspace_id is not None for e in entries)


def test_tool_runs_in_the_principals_workspace(make_client: Callable[..., TestClient]) -> None:
    result = rpc(make_client(), "tools/call", {"name": "echo", "arguments": {"text": "a"}})[
        "result"
    ]
    assert uuid.UUID(result["structuredContent"]["workspace"]) == WORKSPACE_ID


def test_duplicate_tool_names_are_rejected() -> None:
    tool = McpTool("echo", "", EchoIn, _echo)
    with pytest.raises(ValueError, match="duplicate"):
        ToolRegistry([tool, tool])


def test_settings_default_allows_no_browser_origin() -> None:
    assert Settings().mcp_allowed_origins == []
