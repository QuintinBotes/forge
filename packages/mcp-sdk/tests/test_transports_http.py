"""Unit tests for :class:`HttpMcpTransport` (HARD-05 AC1/AC2/AC10/AC11).

Hermetic: an injected :class:`httpx.MockTransport` stands in for the wire, so no
socket is opened. These prove JSON-RPC request encoding, response decoding, tool
hint mapping, session-id reuse, and redaction on error — all without a network.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from forge_contracts import MCPResource, MCPResourceContent
from forge_mcp.exceptions import MCPSecurityError, MCPTransportUnavailableError
from forge_mcp.transport import ToolSpec, Transport
from forge_mcp.transports.http import HttpMcpTransport
from forge_mcp.transports.jsonrpc import JsonRpcError

ENDPOINT = "https://mcp.test/confluence"


def _mock_client(handler: Any) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _with_notification(handler: Any) -> Any:
    """Wrap a handler so the ``notifications/initialized`` post is acknowledged."""

    def wrapped(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        if body.get("method") == "notifications/initialized":
            return httpx.Response(202)
        return handler(request)

    return wrapped


def _default_handler(captured: list[httpx.Request]) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        body = json.loads(request.content or b"{}")
        method = body.get("method")
        if method == "initialize":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {"protocolVersion": "2025-06-18", "capabilities": {}},
                },
                headers={"Mcp-Session-Id": "sess-xyz"},
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "resources/list":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {
                        "resources": [
                            {
                                "uri": "confluence://engineering/p1",
                                "name": "P1",
                                "namespace": "engineering",
                            }
                        ]
                    },
                },
            )
        if method == "resources/read":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {
                        "contents": [
                            {
                                "uri": body["params"]["uri"],
                                "mimeType": "text/plain",
                                "text": "hello world",
                            }
                        ]
                    },
                },
            )
        if method == "tools/list":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {
                        "tools": [
                            {"name": "search_pages", "annotations": {"readOnlyHint": True}},
                            {
                                "name": "create_page",
                                "annotations": {"readOnlyHint": False, "destructiveHint": True},
                            },
                        ]
                    },
                },
            )
        if method == "tools/call":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {"content": [{"type": "text", "text": "done"}], "isError": False},
                },
            )
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": body.get("id"),
                "error": {"code": -32601, "message": "no"},
            },
        )

    return handler


def test_http_transport_conforms_to_protocol() -> None:
    tr = HttpMcpTransport(ENDPOINT, client=_mock_client(_default_handler([])))
    assert isinstance(tr, Transport)


def test_constructor_performs_no_io() -> None:
    # Lazy init: building the transport must not touch the wire (isinstance +
    # factory construction stay hermetic). Handler asserts it is never called.
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("no request should be sent at construction time")

    HttpMcpTransport(ENDPOINT, client=_mock_client(handler))


def test_list_resources_encodes_jsonrpc_and_decodes() -> None:
    captured: list[httpx.Request] = []
    tr = HttpMcpTransport(ENDPOINT, client=_mock_client(_default_handler(captured)))
    resources = tr.list_resources()
    assert resources == [
        MCPResource(uri="confluence://engineering/p1", name="P1", namespace="engineering")
    ]
    methods = [json.loads(r.content)["method"] for r in captured]
    assert methods[0] == "initialize"
    assert "resources/list" in methods


def test_protocol_version_header_and_session_reuse() -> None:
    captured: list[httpx.Request] = []
    tr = HttpMcpTransport(ENDPOINT, client=_mock_client(_default_handler(captured)))
    tr.list_resources()
    tr.list_tools()
    # Every request carries the protocol version header.
    assert all(r.headers.get("MCP-Protocol-Version") == "2025-06-18" for r in captured)
    # After initialize returned Mcp-Session-Id, later requests re-send it.
    post_init = [
        r
        for r in captured
        if json.loads(r.content)["method"] not in ("initialize", "notifications/initialized")
    ]
    assert post_init and all(r.headers.get("Mcp-Session-Id") == "sess-xyz" for r in post_init)


def test_read_resource_returns_content() -> None:
    tr = HttpMcpTransport(ENDPOINT, client=_mock_client(_default_handler([])))
    content = tr.read_resource("confluence://engineering/p1")
    assert isinstance(content, MCPResourceContent)
    assert content.content == "hello world"
    assert content.mime_type == "text/plain"


def test_tool_hint_mapping() -> None:
    tr = HttpMcpTransport(ENDPOINT, client=_mock_client(_default_handler([])))
    tools = {t.name: t for t in tr.list_tools()}
    assert isinstance(tools["search_pages"], ToolSpec)
    assert tools["search_pages"].read_only is True
    assert tools["create_page"].read_only is False
    assert tools["create_page"].destructive is True


def test_call_tool_sends_name_and_arguments() -> None:
    captured: list[httpx.Request] = []
    tr = HttpMcpTransport(ENDPOINT, client=_mock_client(_default_handler(captured)))
    tr.call_tool("search_pages", {"q": "vault"})
    call = next(
        json.loads(r.content) for r in captured if json.loads(r.content)["method"] == "tools/call"
    )
    assert call["params"] == {"name": "search_pages", "arguments": {"q": "vault"}}


def test_bearer_token_sent_when_provided() -> None:
    captured: list[httpx.Request] = []
    tr = HttpMcpTransport(
        ENDPOINT,
        token="tok-123",
        resource=ENDPOINT,
        client=_mock_client(_default_handler(captured)),
    )
    tr.list_resources()
    assert all(r.headers.get("Authorization") == "Bearer tok-123" for r in captured)


def test_resource_indicator_sent_in_initialize_meta() -> None:
    captured: list[httpx.Request] = []
    tr = HttpMcpTransport(
        ENDPOINT,
        token="tok-123",
        resource="https://canonical/uri",
        client=_mock_client(_default_handler(captured)),
    )
    tr.list_resources()
    init = next(
        json.loads(r.content) for r in captured if json.loads(r.content)["method"] == "initialize"
    )
    assert init["params"]["_meta"]["resource"] == "https://canonical/uri"


def test_non_http_scheme_is_rejected() -> None:
    with pytest.raises(MCPSecurityError):
        HttpMcpTransport("file:///etc/passwd")


def test_server_5xx_maps_to_transport_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        if body.get("method") == "initialize":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {}})
        return httpx.Response(503, text="upstream down")

    tr = HttpMcpTransport(ENDPOINT, client=_mock_client(handler))
    with pytest.raises(MCPTransportUnavailableError):
        tr.list_resources()


def test_connection_error_maps_to_transport_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    tr = HttpMcpTransport(ENDPOINT, client=_mock_client(handler))
    with pytest.raises(MCPTransportUnavailableError):
        tr.list_resources()


def test_jsonrpc_error_from_server_propagates() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        if body.get("method") == "initialize":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {}})
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": body["id"],
                "error": {"code": -32602, "message": "bad uri"},
            },
        )

    tr = HttpMcpTransport(ENDPOINT, client=_mock_client(_with_notification(handler)))
    with pytest.raises(JsonRpcError):
        tr.read_resource("confluence://engineering/p1")


def test_redaction_on_http_error_body() -> None:
    # A server error whose message echoes a bearer token must not leak it.
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        if body.get("method") == "initialize":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {}})
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": body["id"],
                "error": {"code": -32000, "message": "rejected Bearer sk-should-not-leak-42"},
            },
        )

    tr = HttpMcpTransport(ENDPOINT, client=_mock_client(_with_notification(handler)))
    with pytest.raises(JsonRpcError) as exc:
        tr.list_resources()
    assert "sk-should-not-leak-42" not in str(exc.value)


def test_sse_response_is_parsed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        if body.get("method") == "initialize":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {}})
        payload = {"jsonrpc": "2.0", "id": body["id"], "result": {"resources": []}}
        return httpx.Response(
            200,
            text=f"event: message\ndata: {json.dumps(payload)}\n\n",
            headers={"Content-Type": "text/event-stream"},
        )

    tr = HttpMcpTransport(ENDPOINT, client=_mock_client(_with_notification(handler)))
    assert tr.list_resources() == []
