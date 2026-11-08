"""Run the shared synchronous GitHub client off the PM event loop.

The registry supplies PMTransport; direct callers can supply GitHubClient.from_app
to retain installation-token refresh, retries and auditing without another auth path.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from forge_contracts.pm import PMTransport
from forge_integrations.errors import GitHubError
from forge_integrations.github import DEFAULT_BASE_URL, GitHubClient, RetryPolicy
from forge_integrations.pm.errors import (
    ExternalNotFound,
    PMAuthError,
    ProviderError,
    RateLimitError,
)


class _PMTransportBridge(httpx.BaseTransport):
    def __init__(self, transport: PMTransport, loop: asyncio.AbstractEventLoop) -> None:
        self.transport = transport
        self.loop = loop

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        call = self.transport.request(
            request.method,
            str(request.url.copy_with(query=None)),
            headers=dict(request.headers),
            json=json.loads(request.content) if request.content else None,
            params=dict(request.url.params),
        )
        try:
            response = asyncio.run_coroutine_threadsafe(call, self.loop).result()
        except RateLimitError as exc:
            headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after is not None else {}
            return httpx.Response(429, headers=headers)
        return httpx.Response(
            response.status_code, json=response.json_body, headers=response.headers
        )


async def request_github(
    client: GitHubClient | PMTransport,
    method: str,
    path: str,
    *,
    repo: str,
    base_url: str = DEFAULT_BASE_URL,
    auth_header: str | None = None,
    **kwargs: Any,
) -> httpx.Response:
    if isinstance(client, GitHubClient):
        github = client
        owned = False
    else:
        owned = True
        github = GitHubClient(
            base_url=base_url,
            transport=_PMTransportBridge(client, asyncio.get_running_loop()),
            retry=RetryPolicy(),
        )
    try:
        response = await asyncio.to_thread(
            github._request,
            method,
            path,
            action="github_issues",
            repo=repo,
            headers={"Authorization": auth_header} if auth_header else None,
            **kwargs,
        )
    except GitHubError as exc:
        raise ProviderError("github issues request failed", status_code=exc.status_code) from None
    finally:
        if owned:
            github.close()
    code = response.status_code
    # Upstream error bodies may echo credentials; expose only the status.
    if code == 429 or (code == 403 and response.headers.get("x-ratelimit-remaining") == "0"):
        raise RateLimitError("github issues rate limited")
    if code in (401, 403):
        raise PMAuthError(f"github issues auth failed ({code})")
    if code == 404:
        raise ExternalNotFound("github issue or repository not found")
    if code >= 400:
        raise ProviderError(f"github issues http {code}", status_code=code)
    return response
