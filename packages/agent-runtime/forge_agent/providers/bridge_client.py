"""A ``ModelClient`` that reaches local agent CLIs through the host bridge.

Maps every transport failure onto the same ``stop_reason`` vocabulary the
subprocess client uses, so the agent runtime cannot tell which transport served
a request — and does not need to.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from typing import Any

import httpx

from forge_agent.providers.base import ModelClientError
from forge_contracts import ModelRequest, ModelResponse, ModelStreamEvent

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

    def complete(self, request: ModelRequest) -> ModelResponse:
        try:
            with httpx.Client(timeout=self._timeout_s, transport=self._transport) as client:
                response = client.post(
                    f"{self._base_url}/v1/complete",
                    json=request.model_dump(mode="json"),
                    headers={"Authorization": f"Bearer {self._token}"},
                )
        except (httpx.ConnectError, httpx.ConnectTimeout):
            return ModelResponse(content="", stop_reason="provider_unavailable")
        except httpx.TimeoutException:
            return ModelResponse(content="", stop_reason="timeout")
        except httpx.HTTPError as exc:
            _logger.warning("cli bridge transport error: %s", self._redactor(str(exc)))
            return ModelResponse(content="", stop_reason="provider_error")

        if response.status_code in (401, 403):
            return ModelResponse(content="", stop_reason="auth_error")
        if response.status_code >= 400:
            _logger.warning("cli bridge returned %s", response.status_code)
            return ModelResponse(content="", stop_reason="provider_error")

        try:
            return ModelResponse.model_validate(response.json())
        except Exception:
            return ModelResponse(content="", stop_reason="parse_error")

    def stream(self, request: ModelRequest) -> Iterator[ModelStreamEvent]:
        response = self.complete(request)
        yield ModelStreamEvent(type="text", text=response.content, delta=response.content)
