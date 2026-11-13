import hmac
import json
import subprocess

import httpx
import pytest

from forge_agent.providers.base import ModelClientError
from forge_agent.providers.bridge_client import LocalCliBridgeClient
from forge_agent.providers.local_cli.client import LocalCliModelClient
from forge_agent.providers.local_cli.detect import CliName
from forge_contracts import ModelMessage, ModelRequest

ENVELOPE = '{"content": "ok", "tool_calls": [], "stop_reason": "end_turn"}'


def _request() -> ModelRequest:
    return ModelRequest(model="claude", messages=[ModelMessage(role="user", content="hi")])


def _client(handler, *, token: str = "t0ken") -> LocalCliBridgeClient:
    return LocalCliBridgeClient(
        base_url="http://host.docker.internal:8099",
        token=token,
        transport=httpx.MockTransport(handler),
    )


def _authorizes(raw_header: bytes, *, configured_token: str) -> bool:
    """Reproduce ``app.py``'s ``_authorize`` dependency exactly, so a test can
    tell whether a header this client actually sent over the wire would
    authenticate against the real bridge -- not just against a hand-rolled
    mock that happens to decode headers the same way it encoded them.

    Starlette decodes a raw ASGI header value as latin-1 before a route ever
    sees it; ``_authorize`` then re-encodes that decoded string as UTF-8
    (``surrogateescape``) and compares it to the configured token, encoded
    the same way. See ``apps/cli-bridge/forge_cli_bridge/app.py``.
    """
    decoded = raw_header.decode("latin-1")
    provided = decoded.encode("utf-8", "surrogateescape")
    wanted = f"Bearer {configured_token}".encode("utf-8", "surrogateescape")
    return hmac.compare_digest(provided, wanted)


def test_successful_round_trip_and_token_header():
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization", "")
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["content_type"] = request.headers.get("content-type", "")
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"content": "ok", "tool_calls": [], "stop_reason": "end_turn"}
        )

    assert _client(handler).complete(_request()).content == "ok"
    assert seen["auth"] == "Bearer t0ken"
    assert seen["method"] == "POST"
    assert seen["path"] == "/v1/complete"
    assert seen["content_type"] == "application/json"
    assert seen["body"] == {
        "model": "claude",
        "messages": [{"role": "user", "content": "hi"}],
        "system": None,
        "tools": [],
        "max_tokens": None,
        "temperature": None,
        "stop": None,
        "metadata": {},
    }


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


def test_redirect_is_handled_deliberately_as_provider_error():
    """A 3xx used to fall through both status branches and land on
    ``parse_error`` by accident (via a failed ``.json()`` on a redirect body).
    The bridge never issues one, but the client must still classify it on
    purpose rather than by the accident of what ``.json()`` does with it.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "https://elsewhere.invalid"})

    assert _client(handler).complete(_request()).stop_reason == "provider_error"


def test_timeout_is_timeout():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    assert _client(handler).complete(_request()).stop_reason == "timeout"


def test_malformed_response_body_is_parse_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    assert _client(handler).complete(_request()).stop_reason == "parse_error"


def test_empty_response_body_is_parse_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="")

    assert _client(handler).complete(_request()).stop_reason == "parse_error"


def test_valid_json_that_is_not_a_valid_envelope_is_parse_error():
    def handler(request: httpx.Request) -> httpx.Response:
        # Valid JSON, wrong shape: tool_calls must be a list.
        return httpx.Response(200, json={"tool_calls": "nope"})

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


# -- non-ASCII token (fix round 1, finding 1) ------------------------------- #


def test_non_ascii_token_round_trip_does_not_raise_and_authenticates():
    """Regression: httpx raises ``UnicodeEncodeError`` from inside
    ``client.post`` for a plain ``str`` non-ASCII header value, before a
    request is ever sent. ``complete()`` must never raise, and the header
    this client actually puts on the wire must be the one that authenticates
    against the real bridge's latin-1-decode / UTF-8-surrogateescape-compare
    chain -- not just satisfy a mock that decodes however it was encoded.
    """
    token = "t\xf6ken"
    seen: dict[str, bytes] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        for name, value in request.headers.raw:
            if name.lower() == b"authorization":
                seen["raw"] = value
        return httpx.Response(
            200, json={"content": "ok", "tool_calls": [], "stop_reason": "end_turn"}
        )

    response = _client(handler, token=token).complete(_request())
    assert response.content == "ok"
    assert response.stop_reason == "end_turn"
    assert _authorizes(seen["raw"], configured_token=token)


def test_non_ascii_token_rejected_cleanly_as_401_not_a_raise():
    token = "t\xf6ken"

    def handler(request: httpx.Request) -> httpx.Response:
        # Simulates the bridge rejecting it (e.g. it does not match the
        # configured token); the point is only that sending it never raises.
        return httpx.Response(401, json={"detail": "invalid or missing bridge token"})

    response = _client(handler, token=token).complete(_request())
    assert response.stop_reason == "auth_error"


def test_token_with_a_codepoint_outside_latin1_is_provider_error_not_a_raise():
    """A codepoint above U+00FF cannot be carried in an HTTP header at all
    (headers are wire-transmitted as ISO-8859-1); ``complete()`` still must
    not raise for it.
    """

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never reached
        raise AssertionError("should not reach the transport")

    response = _client(handler, token="tokあen").complete(_request())
    assert response.stop_reason == "provider_error"


# -- unserialisable tools/metadata (fix round 1, finding 2) ----------------- #


class _Weird:
    """A plain object with no JSON encoder registered anywhere."""

    def __str__(self) -> str:
        return "weird-repr"


def _subprocess_client() -> LocalCliModelClient:
    def runner(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, stdout=ENVELOPE, stderr="")

    return LocalCliModelClient(cli_name=CliName.claude, runner=runner)


def _ok_handler(request: httpx.Request) -> httpx.Response:
    # Also proves the body was actually JSON-encodable and reached the
    # handler -- a body-encoding failure would never get this far.
    json.loads(request.content)
    return httpx.Response(200, json={"content": "ok", "tool_calls": [], "stop_reason": "end_turn"})


def test_custom_object_in_tools_matches_subprocess_client_parity():
    request = ModelRequest(
        model="claude",
        messages=[ModelMessage(role="user", content="hi")],
        tools=[{"name": "t", "odd": _Weird()}],
    )

    bridge_response = _client(_ok_handler).complete(request)
    subprocess_response = _subprocess_client().complete(request)

    assert bridge_response.stop_reason == "end_turn"
    assert subprocess_response.stop_reason == "end_turn"
    assert bridge_response.stop_reason == subprocess_response.stop_reason


def test_bytes_value_in_metadata_matches_subprocess_client_parity():
    request = ModelRequest(
        model="claude",
        messages=[ModelMessage(role="user", content="hi")],
        metadata={"blob": b"\xff\xfe\x00binary"},
    )

    bridge_response = _client(_ok_handler).complete(request)
    subprocess_response = _subprocess_client().complete(request)

    assert bridge_response.stop_reason == "end_turn"
    assert subprocess_response.stop_reason == "end_turn"
    assert bridge_response.stop_reason == subprocess_response.stop_reason


def test_unserialisable_value_that_str_still_cannot_render_is_provider_error():
    class Cursed:
        def __str__(self) -> str:
            raise RuntimeError("nope")

    request = ModelRequest(
        model="claude",
        messages=[ModelMessage(role="user", content="hi")],
        tools=[{"name": "t", "odd": Cursed()}],
    )

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never reached
        raise AssertionError("should not reach the transport")

    response = _client(handler).complete(request)
    assert response.stop_reason == "provider_error"


# -- namespaced model id (final review, I2) --------------------------------- #


def test_bridge_model_id_falls_back_to_the_configured_cli():
    """An untagged 200 reply is still metered inside the ``local-cli`` namespace."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=json.loads(ENVELOPE))

    client = LocalCliBridgeClient(
        base_url="http://host.docker.internal:8099",
        token="t0ken",
        cli_name=CliName.codex,
        transport=httpx.MockTransport(handler),
    )
    assert client.complete(_request()).model == "local-cli:codex"


def test_bridge_model_id_without_a_configured_cli_is_the_bare_namespace():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=json.loads(ENVELOPE))

    assert _client(handler).complete(_request()).model == "local-cli"


def test_the_bridges_own_model_id_wins():
    """The host knows which CLI actually ran; this side only knows a preference."""

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(ENVELOPE) | {"model": "local-cli:claude"}
        return httpx.Response(200, json=payload)

    client = LocalCliBridgeClient(
        base_url="http://host.docker.internal:8099",
        token="t0ken",
        cli_name=CliName.codex,
        transport=httpx.MockTransport(handler),
    )
    assert client.complete(_request()).model == "local-cli:claude"


def test_a_priced_model_id_from_the_wire_is_never_trusted():
    """Whatever answered, a local-CLI run must not be priced on a real API row."""

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(ENVELOPE) | {"model": "claude-opus-4-8"}
        return httpx.Response(200, json=payload)

    client = LocalCliBridgeClient(
        base_url="http://host.docker.internal:8099",
        token="t0ken",
        cli_name=CliName.claude,
        transport=httpx.MockTransport(handler),
    )
    assert client.complete(_request()).model == "local-cli:claude"


def test_bridge_model_id_is_namespaced_on_every_failure_path():
    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    handlers = {
        "provider_unavailable": refused,
        "timeout": slow,
        "auth_error": lambda request: httpx.Response(401, json={"detail": "nope"}),
        "provider_error": lambda request: httpx.Response(500, text="boom"),
        "parse_error": lambda request: httpx.Response(200, text="not json"),
    }
    for expected_reason, handler in handlers.items():
        client = LocalCliBridgeClient(
            base_url="http://host.docker.internal:8099",
            token="t0ken",
            cli_name=CliName.claude,
            transport=httpx.MockTransport(handler),
        )
        response = client.complete(_request())
        assert response.stop_reason == expected_reason
        assert response.model == "local-cli:claude", (
            f"{expected_reason} must still be metered as a local-CLI run"
        )
