"""Unit tests for the JSON-RPC 2.0 envelope (HARD-05 AC2/AC10/AC11).

Pure, no-I/O tests of request/notification building and response decoding,
including redaction of any secret a server might echo into an error object.
"""

from __future__ import annotations

import pytest

from forge_mcp.transports.jsonrpc import (
    IdGenerator,
    JsonRpcError,
    build_notification,
    build_request,
    parse_response,
)


def test_build_request_shape() -> None:
    req = build_request("resources/list", {"a": 1}, 7)
    assert req == {"jsonrpc": "2.0", "id": 7, "method": "resources/list", "params": {"a": 1}}


def test_build_request_omits_absent_params() -> None:
    req = build_request("tools/list", None, 1)
    assert "params" not in req
    assert req["method"] == "tools/list"


def test_build_notification_has_no_id() -> None:
    note = build_notification("notifications/initialized")
    assert "id" not in note
    assert note == {"jsonrpc": "2.0", "method": "notifications/initialized"}


def test_id_generator_is_monotonic() -> None:
    gen = IdGenerator()
    assert [gen.next() for _ in range(3)] == [1, 2, 3]


def test_parse_response_returns_result() -> None:
    assert parse_response({"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}) == {"ok": True}


def test_parse_response_raises_on_error_object() -> None:
    with pytest.raises(JsonRpcError) as exc:
        parse_response({"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "nope"}})
    assert exc.value.code == -32601


def test_parse_response_redacts_secret_in_error_message() -> None:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "error": {"code": -32000, "message": "bad token: Bearer sk-leaked-9999"},
    }
    with pytest.raises(JsonRpcError) as exc:
        parse_response(payload)
    assert "sk-leaked-9999" not in str(exc.value)
    assert "[redacted]" in str(exc.value)


def test_parse_response_rejects_id_mismatch() -> None:
    with pytest.raises(JsonRpcError):
        parse_response({"jsonrpc": "2.0", "id": 2, "result": {}}, expected_id=1)


def test_parse_response_rejects_non_object() -> None:
    with pytest.raises(JsonRpcError):
        parse_response(["not", "an", "object"])
