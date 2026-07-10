"""Asana REST v1 client over the offline-testable ``PMTransport``.

Mirrors :mod:`forge_integrations.pm.jira.client`: all methods are async,
delegate HTTP to the injected transport (so tests replay :class:`FixturePMTransport`
records with **no** sockets), and unwrap Asana's ``{"data": ...}`` response
envelope. Auth headers are built once and never logged.
"""

from __future__ import annotations

from typing import Any

from forge_contracts.pm import HttpResponse, PMTransport
from forge_integrations.pm.errors import ExternalNotFound, PMAuthError, ProviderError

API = "https://app.asana.com/api/1.0"

TASK_OPT_FIELDS = (
    "name,notes,html_notes,completed,permalink_url,modified_at,"
    "assignee.email,assignee.gid,tags.name,memberships.section.name,"
    "memberships.section.gid,memberships.project.gid,"
    "custom_fields.name,custom_fields.enum_value.name,custom_fields.resource_subtype"
)


class AsanaClient:
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
            raise PMAuthError(f"asana auth failed ({resp.status_code})")
        if resp.status_code == 404:
            raise ExternalNotFound(f"asana resource not found: {path}")
        if resp.status_code >= 400:
            raise ProviderError(
                f"asana error {resp.status_code} on {path}", status_code=resp.status_code
            )
        body = resp.json_body or {}
        return dict(body) if isinstance(body, dict) else {}

    async def get_task(self, task_gid: str) -> dict[str, Any]:
        body = await self._request(
            "GET", f"/tasks/{task_gid}", params={"opt_fields": TASK_OPT_FIELDS}
        )
        return dict(body.get("data") or {})

    async def create_task(self, fields: dict[str, Any]) -> dict[str, Any]:
        body = await self._request(
            "POST", "/tasks", json={"data": fields}, params={"opt_fields": TASK_OPT_FIELDS}
        )
        return dict(body.get("data") or {})

    async def update_task(self, task_gid: str, fields: dict[str, Any]) -> dict[str, Any]:
        body = await self._request(
            "PUT",
            f"/tasks/{task_gid}",
            json={"data": fields},
            params={"opt_fields": TASK_OPT_FIELDS},
        )
        return dict(body.get("data") or {})

    async def list_project_tasks(
        self, project_gid: str, *, offset: str | None = None, limit: int = 50
    ) -> tuple[list[dict[str, Any]], str | None]:
        params: dict[str, Any] = {"opt_fields": TASK_OPT_FIELDS, "limit": limit}
        if offset:
            params["offset"] = offset
        body = await self._request("GET", f"/projects/{project_gid}/tasks", params=params)
        tasks = list(body.get("data") or [])
        next_page = body.get("next_page") or {}
        next_offset = next_page.get("offset") if isinstance(next_page, dict) else None
        return tasks, next_offset

    async def list_sections(self, project_gid: str) -> list[dict[str, Any]]:
        body = await self._request("GET", f"/projects/{project_gid}/sections")
        return list(body.get("data") or [])

    async def list_custom_field_settings(self, project_gid: str) -> list[dict[str, Any]]:
        body = await self._request(
            "GET",
            f"/projects/{project_gid}/custom_field_settings",
            params={
                "opt_fields": (
                    "custom_field.gid,custom_field.name,custom_field.resource_subtype,"
                    "custom_field.enum_options.gid,custom_field.enum_options.name"
                )
            },
        )
        return list(body.get("data") or [])

    async def add_task_to_section(self, section_gid: str, task_gid: str) -> None:
        await self._request(
            "POST", f"/sections/{section_gid}/addTask", json={"data": {"task": task_gid}}
        )

    async def me(self) -> dict[str, Any]:
        body = await self._request("GET", "/users/me")
        return dict(body.get("data") or {})

    async def create_webhook(self, resource_gid: str, target: str) -> dict[str, Any]:
        body = await self._request(
            "POST", "/webhooks", json={"data": {"resource": resource_gid, "target": target}}
        )
        return dict(body.get("data") or {})

    async def delete_webhook(self, webhook_gid: str) -> None:
        await self._request("DELETE", f"/webhooks/{webhook_gid}")


__all__ = ["API", "TASK_OPT_FIELDS", "AsanaClient"]
