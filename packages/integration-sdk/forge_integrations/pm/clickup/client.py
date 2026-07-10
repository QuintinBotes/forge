"""ClickUp REST v2 client over the offline-testable ``PMTransport``.

Mirrors :mod:`forge_integrations.pm.asana.client`: all methods are async,
delegate HTTP to the injected transport (so tests replay
:class:`FixturePMTransport` records with **no** sockets). ClickUp sends the
personal/OAuth token as the raw ``Authorization`` header value (no ``Bearer``
prefix), never logged.
"""

from __future__ import annotations

from typing import Any

from forge_contracts.pm import HttpResponse, PMTransport
from forge_integrations.pm.errors import ExternalNotFound, PMAuthError, ProviderError

API = "https://api.clickup.com/api/v2"


class ClickUpClient:
    def __init__(
        self,
        transport: PMTransport,
        *,
        base_url: str = API,
        auth_header: str | None = None,
    ) -> None:
        self._t = transport
        self._base = base_url.rstrip("/")
        self._headers: dict[str, str] = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if auth_header:
            self._headers["Authorization"] = auth_header

    def _url(self, path: str) -> str:
        return f"{self._base}{path}"

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        params: dict | None = None,
    ) -> dict[str, Any]:
        resp: HttpResponse = await self._t.request(
            method, self._url(path), headers=self._headers, json=json, params=params
        )
        if resp.status_code in (401, 403):
            raise PMAuthError(f"clickup auth failed ({resp.status_code})")
        if resp.status_code == 404:
            raise ExternalNotFound(f"clickup resource not found: {path}")
        if resp.status_code >= 400:
            raise ProviderError(
                f"clickup error {resp.status_code} on {path}", status_code=resp.status_code
            )
        body = resp.json_body or {}
        return dict(body) if isinstance(body, dict) else {}

    async def get_task(self, task_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/task/{task_id}")

    async def create_task(self, list_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", f"/list/{list_id}/task", json=fields)

    async def update_task(self, task_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        return await self._request("PUT", f"/task/{task_id}", json=fields)

    async def list_list_tasks(
        self, list_id: str, *, page: int | None = None
    ) -> tuple[list[dict[str, Any]], int | None]:
        params: dict[str, Any] = {"page": page or 0}
        body = await self._request("GET", f"/list/{list_id}/task", params=params)
        tasks = list(body.get("tasks") or [])
        last_page = bool(body.get("last_page", True))
        next_page = None if last_page else (page or 0) + 1
        return tasks, next_page

    async def me(self) -> dict[str, Any]:
        body = await self._request("GET", "/user")
        return dict(body.get("user") or {})

    async def create_webhook(self, team_id: str, list_id: str, endpoint: str) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/team/{team_id}/webhook",
            json={
                "endpoint": endpoint,
                "events": ["taskCreated", "taskUpdated", "taskDeleted"],
                "list_id": list_id,
            },
        )

    async def delete_webhook(self, webhook_id: str) -> None:
        await self._request("DELETE", f"/webhook/{webhook_id}")


__all__ = ["API", "ClickUpClient"]
