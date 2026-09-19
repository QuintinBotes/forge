"""A ``ModelClient`` that reaches local agent CLIs through the host bridge.

Maps every transport failure onto the same ``stop_reason`` vocabulary the
subprocess client uses, so the agent runtime cannot tell which transport served
a request — and does not need to.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator
from typing import Any

import httpx

from forge_agent.providers.base import ModelClientError
from forge_agent.providers.local_cli.detect import CliName
from forge_contracts import (
    ModelRequest,
    ModelResponse,
    ModelStreamEvent,
    ProviderFailureStopReason,
)

__all__ = ["LocalCliBridgeClient"]

_logger = logging.getLogger("forge_agent.providers.bridge")


def _identity(value: str) -> str:
    return value


class LocalCliBridgeClient:
    """A ``ModelClient`` speaking HTTP to a host-side CLI bridge."""

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        cli_name: CliName | None = None,
        timeout_s: float = 600.0,
        redactor: Callable[[str], str] = _identity,
        transport: Any | None = None,
    ) -> None:
        # Amendment B: `build_model_client` selects this branch on a bare
        # truthiness check of `config.bridge_url`, so a whitespace-only value
        # can reach here even though `ModelClientConfig.from_env` itself
        # normalises blanks to `None`. Failing here, at construction, turns
        # what would otherwise be an obscure connection failure on every call
        # into a clear configuration error up front.
        if not base_url or not base_url.strip():
            raise ModelClientError("LocalCliBridgeClient requires a non-blank base_url")
        # The bridge refuses to serve any request without a matching bearer
        # token (`app.py`'s `_authorize` dependency), so a blank token can
        # only ever produce a 401 on every call -- fail at construction
        # instead of letting that surprise show up call by call.
        if not token or not token.strip():
            raise ModelClientError("LocalCliBridgeClient requires a non-blank token")
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout_s = timeout_s
        self._redactor = redactor
        self._transport = transport
        #: The namespaced model id this transport reports when the bridge does
        #: not (every failure path, and any bridge that returns an untagged
        #: response). ``cli_name`` is the operator's configured preference, so
        #: it is a best guess at which backend the *host* actually ran -- the
        #: bridge's own answer wins when it gives one. What matters for cost is
        #: only that it always stays inside the ``local-cli`` namespace: a
        #: subscription run must never be priced against a real API model's
        #: row (spec F §5).
        self._model_id = f"local-cli:{cli_name}" if cli_name is not None else "local-cli"

    def complete(self, request: ModelRequest) -> ModelResponse:
        auth_header = self._auth_header()
        if auth_header is None:
            return self._failure("provider_error")

        body = self._encode_body(request)
        if body is None:
            return self._failure("provider_error")

        try:
            with httpx.Client(timeout=self._timeout_s, transport=self._transport) as client:
                response = client.post(
                    f"{self._base_url}/v1/complete",
                    content=body,
                    # bytes-keyed throughout: httpx's typed overloads accept a
                    # str-keyed *or* bytes-keyed header mapping, not a mix, and
                    # the Authorization value must stay bytes (see
                    # `_auth_header`), so Content-Type is bytes too here.
                    headers={
                        b"Authorization": auth_header,
                        b"Content-Type": b"application/json",
                    },
                )
        except (httpx.ConnectError, httpx.ConnectTimeout):
            return self._failure("provider_unavailable")
        except httpx.TimeoutException:
            return self._failure("timeout")
        except httpx.HTTPError as exc:
            _logger.warning("cli bridge transport error: %s", self._redactor(str(exc)))
            return self._failure("provider_error")

        if response.status_code in (401, 403):
            return self._failure("auth_error")
        if not (200 <= response.status_code < 300):
            # Deliberate catch-all for everything outside 2xx that isn't a
            # clean auth rejection. The bridge never issues a redirect, but a
            # bare `>= 400` check used to let a 3xx fall through both status
            # branches and land on `parse_error` only because `.json()` then
            # failed on it -- an accident of control flow, not a decision
            # about what a non-2xx/non-auth status means. Handling the full
            # range here is deliberate instead.
            _logger.warning("cli bridge returned %s", response.status_code)
            return self._failure("provider_error")

        try:
            return self._namespaced(ModelResponse.model_validate(response.json()))
        except Exception:
            return self._failure("parse_error")

    def stream(self, request: ModelRequest) -> Iterator[ModelStreamEvent]:
        response = self.complete(request)
        yield ModelStreamEvent(type="text", text=response.content, delta=response.content)

    # -- internals ---------------------------------------------------------

    def _failure(self, reason: ProviderFailureStopReason) -> ModelResponse:
        """A failed turn, carrying a namespaced model id like any other.

        ``reason`` is typed with the shared ``ProviderFailureStopReason``
        literal (``forge_contracts``) -- the same definition the agent runtime
        matches against to fail the run, so a value outside it is a
        type-checking error here rather than a silent empty success there.
        """
        return ModelResponse(content="", model=self._model_id, stop_reason=reason)

    def _namespaced(self, response: ModelResponse) -> ModelResponse:
        """Force the bridge's reply into the ``local-cli`` pricing namespace.

        The host-side ``LocalCliModelClient`` already stamps
        ``local-cli:<cli>``, and that value is the authoritative one (it names
        the CLI that actually ran, which can differ from this side's
        configured preference). Anything else -- an untagged reply, or a real
        API model id from something that is not this bridge -- is replaced,
        because the one thing that must hold is that a subscription run is
        never metered against a priced model row (spec F §5).
        """
        reported = response.model or ""
        if reported == "local-cli" or reported.startswith("local-cli:"):
            return response
        return response.model_copy(update={"model": self._model_id})

    def _auth_header(self) -> bytes | None:
        """Bytes for the ``Authorization`` header, latin-1 encoded.

        httpx refuses to transmit a non-ASCII ``str`` header value -- it tries
        to ascii-encode it internally and raises ``UnicodeEncodeError`` before
        a request is ever sent. Encoding the value ourselves and handing httpx
        ``bytes`` bypasses that check entirely.

        The encoding matters, not just avoiding the raise: HTTP header values
        are wire bytes, and Starlette decodes them as latin-1 on receipt
        before handing the route a ``str``; ``app.py``'s ``_authorize`` then
        re-encodes *that* string as UTF-8 (``surrogateescape``) before
        comparing it to the configured token, encoded the same way (see
        ``apps/cli-bridge/forge_cli_bridge/app.py``). Encoding here with UTF-8
        -- the codec ``_authorize`` uses on its side of that chain -- sends
        wire bytes Starlette's latin-1 decode cannot round-trip back to the
        original token: verified against the live bridge app, a UTF-8-encoded
        header for a matching non-ASCII token is rejected with 401, while a
        latin-1-encoded one authenticates. Encoding with latin-1 here is what
        reverses Starlette's decode step, so the string ``_authorize``
        ultimately re-encodes is the original ``expected`` string again.
        """
        try:
            return f"Bearer {self._token}".encode("latin-1", "surrogateescape")
        except UnicodeEncodeError:
            _logger.warning("cli bridge token is not representable in an HTTP header")
            return None

    def _encode_body(self, request: ModelRequest) -> bytes | None:
        """JSON-encode ``request``, degrading unserialisable values to ``str``.

        Mirrors ``translate.build_local_cli_prompt``'s own
        ``json.dumps(..., default=str)`` degrade, and for the identical
        reason: a stray non-JSON-native value inside ``tools``/``metadata``
        (both ``dict[str, Any]``) is advisory payload, not a contract, and
        should degrade to its string form rather than take the whole
        transport down. This is also the parity boundary: the subprocess
        client degrades the same way when it renders ``tools`` into the CLI
        prompt, so a request the agent runtime can send through one transport
        is never rejected outright by the other.

        ``model_dump(mode="python")`` (not ``"json"``) is deliberate:
        ``mode="json"`` itself raises trying to coerce ``Any``-typed values
        into JSON-native types (``PydanticSerializationError`` for an
        arbitrary object, ``UnicodeDecodeError`` for raw bytes that are not
        valid UTF-8); ``mode="python"`` leaves those values untouched and lets
        ``json.dumps``'s own ``default=`` do the one degrade, in one place.
        """
        try:
            payload = request.model_dump(mode="python")
            return json.dumps(payload, default=str).encode("utf-8")
        except Exception:
            _logger.warning("cli bridge request payload is not serialisable")
            return None
