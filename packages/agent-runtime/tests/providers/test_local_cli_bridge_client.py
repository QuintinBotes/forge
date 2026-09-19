import httpx
import pytest

from forge_agent.providers.base import ModelClientError
from forge_agent.providers.bridge_client import LocalCliBridgeClient
from forge_contracts import ModelMessage, ModelRequest


def _request() -> ModelRequest:
    return ModelRequest(model="claude", messages=[ModelMessage(role="user", content="hi")])


def _client(handler) -> LocalCliBridgeClient:
    return LocalCliBridgeClient(
        base_url="http://host.docker.internal:8099",
        token="t0ken",
        transport=httpx.MockTransport(handler),
    )


def test_successful_round_trip_and_token_header():
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization", "")
        return httpx.Response(
            200, json={"content": "ok", "tool_calls": [], "stop_reason": "end_turn"}
        )

    assert _client(handler).complete(_request()).content == "ok"
    assert seen["auth"] == "Bearer t0ken"


def test_connection_refused_is_provider_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    assert _client(handler).complete(_request()).stop_reason == "provider_unavailable"


def test_401_is_auth_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "nope"})

    assert _client(handler).complete(_request()).stop_reason == "auth_error"


def test_500_is_provider_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    assert _client(handler).complete(_request()).stop_reason == "provider_error"


def test_timeout_is_timeout():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    assert _client(handler).complete(_request()).stop_reason == "timeout"


def test_malformed_response_body_is_parse_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    assert _client(handler).complete(_request()).stop_reason == "parse_error"


def test_stream_yields_one_text_event_from_complete():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"content": "streamed", "tool_calls": [], "stop_reason": "end_turn"}
        )

    events = list(_client(handler).stream(_request()))
    assert len(events) == 1
    assert events[0].type == "text"
    assert events[0].text == "streamed"
    assert events[0].delta == "streamed"


def test_blank_base_url_raises_at_construction():
    with pytest.raises(ModelClientError):
        LocalCliBridgeClient(base_url="   ", token="t0ken")


def test_blank_token_raises_at_construction():
    with pytest.raises(ModelClientError):
        LocalCliBridgeClient(base_url="http://host.docker.internal:8099", token="  ")
