"""Jira Cloud REST v3 client over the offline-testable ``PMTransport``.

All methods are async and delegate HTTP to the injected transport, so tests drive
them via :class:`FixturePMTransport` with **no** sockets. Auth headers are built
once and never logged.
"""

from __future__ import annotations

from typing import Any

from forge_contracts.pm import HttpResponse, PMTransport
from forge_integrations.pm.errors import ExternalNotFound, PMAuthError, ProviderError

API = "/rest/api/3"


class JiraClient:
    def __init__(
        self,
        transport: PMTransport,
        *,
        base_url: str,
        auth_header: str | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self._t = transport
        self._base = base_url.rstrip("/")
        self._headers: dict[str, str] = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if auth_header:
            self._headers["Authorization"] = auth_header
        if extra_headers:
            self._headers.update(extra_headers)

    def _url(self, path: str) -> str:
        return f"{self._base}{path}"

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        params: dict | None = None,
    ) -> HttpResponse:
        resp = await self._t.request(
            method, self._url(path), headers=self._headers, json=json, params=params
        )
        if resp.status_code in (401, 403):
            raise PMAuthError(f"jira auth failed ({resp.status_code})")
        if resp.status_code == 404:
            raise ExternalNotFound(f"jira resource not found: {path}")
        if resp.status_code >= 400:
            raise ProviderError(
                f"jira error {resp.status_code} on {path}", status_code=resp.status_code
            )
        return resp

    async def get_issue(self, issue_id_or_key: str) -> dict[str, Any]:
        resp = await self._request("GET", f"{API}/issue/{issue_id_or_key}")
        return dict(resp.json_body or {})

    async def create_issue(self, fields: dict[str, Any]) -> dict[str, Any]:
        resp = await self._request("POST", f"{API}/issue", json={"fields": fields})
        return dict(resp.json_body or {})

    async def update_issue(self, issue_id_or_key: str, fields: dict[str, Any]) -> None:
        await self._request(
            "PUT", f"{API}/issue/{issue_id_or_key}", json={"fields": fields}
        )

    async def search(
        self, jql: str, *, start_at: int = 0, max_results: int = 50
    ) -> dict[str, Any]:
        resp = await self._request(
            "GET",
            f"{API}/search",
            params={"jql": jql, "startAt": start_at, "maxResults": max_results},
        )
        return dict(resp.json_body or {})

    async def get_transitions(self, issue_id_or_key: str) -> list[dict[str, Any]]:
        resp = await self._request("GET", f"{API}/issue/{issue_id_or_key}/transitions")
        return list((resp.json_body or {}).get("transitions", []))  # type: ignore[union-attr]

    async def do_transition(self, issue_id_or_key: str, transition_id: str) -> None:
        await self._request(
            "POST",
            f"{API}/issue/{issue_id_or_key}/transitions",
            json={"transition": {"id": transition_id}},
        )

    async def list_statuses(self, project_key: str) -> list[dict[str, Any]]:
        resp = await self._request("GET", f"{API}/project/{project_key}/statuses")
        body = resp.json_body or []
        return list(body) if isinstance(body, list) else []

    async def list_priorities(self) -> list[dict[str, Any]]:
        resp = await self._request("GET", f"{API}/priority")
        body = resp.json_body or []
        return list(body) if isinstance(body, list) else []

    async def myself(self) -> dict[str, Any]:
        resp = await self._request("GET", f"{API}/myself")
        return dict(resp.json_body or {})

    async def register_webhook(self, payload: dict[str, Any]) -> dict[str, Any]:
        resp = await self._request("POST", "/rest/webhooks/1.0/webhook", json=payload)
        return dict(resp.json_body or {})

    async def unregister_webhook(self, webhook_id: str) -> None:
        await self._request("DELETE", f"/rest/webhooks/1.0/webhook/{webhook_id}")


__all__ = ["API", "JiraClient"]
