"""PM provider transport: offline ``FixturePMTransport`` + real httpx transports.

Mirrors F03's ``GitHubTransport`` / ``FixtureGitHubTransport`` split so the whole
PM test-suite runs with **zero** sockets: every provider call goes through
:class:`FixturePMTransport`, which replays recorded :class:`HttpResponse` records
keyed by ``(method, key)`` where ``key`` is the request URL path (REST) or the
GraphQL operation name (GraphQL). Unexpected calls raise loudly.

The real ``HttpxJiraTransport`` / ``HttpxLinearTransport`` are only constructed in
production wiring; importing this module does not open any connection.
"""

from __future__ import annotations

import re

from forge_contracts.pm import HttpResponse, PMTransport
from forge_integrations.pm.errors import ProviderError, RateLimitError

__all__ = [
    "FixturePMTransport",
    "HttpResponse",
    "HttpxJiraTransport",
    "HttpxLinearTransport",
    "PMTransport",
    "graphql_operation_name",
    "url_path",
]


_OP_RE = re.compile(r"(?:query|mutation)\s+([A-Za-z_][A-Za-z0-9_]*)")
_FIELD_RE = re.compile(r"\{\s*([A-Za-z_][A-Za-z0-9_]*)")


def url_path(url: str) -> str:
    """Return the path component of a URL (drops scheme/host/query)."""
    s = url
    for prefix in ("https://", "http://"):
        if s.startswith(prefix):
            s = s[len(prefix) :]
            slash = s.find("/")
            s = s[slash:] if slash >= 0 else "/"
            break
    return s.split("?", 1)[0].rstrip("/") or "/"


def graphql_operation_name(query: str) -> str | None:
    """Extract a stable key for a GraphQL document (named op or first field)."""
    m = _OP_RE.search(query)
    if m:
        return m.group(1)
    m = _FIELD_RE.search(query)
    return m.group(1) if m else None


class FixturePMTransport:
    """Replays recorded responses; records a call log; no sockets.

    Parameters
    ----------
    records:
        Maps ``(method, key)`` -> a single :class:`HttpResponse` or a list of
        them (popped in order to model paginated / repeated calls). ``method`` is
        upper-cased; ``key`` is a URL path or a GraphQL operation name.
    """

    def __init__(
        self,
        records: dict[tuple[str, str], HttpResponse | list[HttpResponse]] | None = None,
    ) -> None:
        self._records: dict[tuple[str, str], list[HttpResponse]] = {}
        for (method, key), value in (records or {}).items():
            seq = list(value) if isinstance(value, list) else [value]
            self._records[(method.upper(), key)] = seq
        self.call_log: list[dict] = []

    def add(
        self, method: str, key: str, response: HttpResponse | list[HttpResponse]
    ) -> None:
        seq = list(response) if isinstance(response, list) else [response]
        self._records.setdefault((method.upper(), key), []).extend(seq)

    def _candidate_keys(self, url: str, json: dict | None) -> list[str]:
        keys: list[str] = []
        if json and isinstance(json.get("query"), str):
            op = graphql_operation_name(json["query"])
            if op:
                keys.append(op)
        keys.append(url_path(url))
        keys.append(url)
        return keys

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict | None = None,
        json: dict | None = None,
        params: dict | None = None,
    ) -> HttpResponse:
        method = method.upper()
        self.call_log.append(
            {"method": method, "url": url, "json": json, "params": params}
        )
        for key in self._candidate_keys(url, json):
            seq = self._records.get((method, key))
            if seq:
                resp = seq.pop(0)
                if resp.status_code == 429:
                    retry = resp.headers.get("Retry-After")
                    raise RateLimitError(
                        "rate limited",
                        retry_after=float(retry) if retry else None,
                    )
                return resp
        raise ProviderError(
            f"FixturePMTransport: no recorded response for {method} "
            f"{url_path(url)} (candidates: {self._candidate_keys(url, json)})"
        )


class _HttpxTransport:
    """Thin async httpx wrapper used by the real provider clients in production.

    Constructed only in live wiring; the test-suite never instantiates it, so no
    sockets are opened during CI (AC23).
    """

    def __init__(
        self,
        *,
        base_url: str = "",
        default_headers: dict[str, str] | None = None,
        timeout: float = 30.0,
    ) -> None:
        import httpx  # local import: keep the module import socket-free

        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers=default_headers or {},
            timeout=timeout,
        )

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict | None = None,
        json: dict | None = None,
        params: dict | None = None,
    ) -> HttpResponse:
        resp = await self._client.request(
            method, url, headers=headers, json=json, params=params
        )
        if resp.status_code == 429:
            retry = resp.headers.get("Retry-After")
            raise RateLimitError(
                "rate limited", retry_after=float(retry) if retry else None
            )
        body: dict | list | None
        try:
            body = resp.json()
        except ValueError:
            body = None
        return HttpResponse(
            status_code=resp.status_code,
            json_body=body,
            headers=dict(resp.headers),
        )

    async def aclose(self) -> None:
        await self._client.aclose()


class HttpxJiraTransport(_HttpxTransport):
    """Real Jira Cloud REST transport (production only)."""


class HttpxLinearTransport(_HttpxTransport):
    """Real Linear GraphQL transport (production only)."""
