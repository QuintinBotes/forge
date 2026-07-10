"""GitLab REST v4 client over the offline-testable ``PMTransport``.

Mirrors :mod:`forge_integrations.pm.jira.client`: all methods are async,
delegate HTTP to the injected transport (so tests replay
:class:`FixturePMTransport` records with **no** sockets), and target either
gitlab.com or a self-managed instance via ``base_url``. Personal/project
access tokens are sent as the ``PRIVATE-TOKEN`` header (GitLab's convention
for non-OAuth tokens); OAuth deployments pass a full ``Authorization: Bearer``
value instead via ``auth_header``/``auth_header_name``.
"""

from __future__ import annotations

from typing import Any

from forge_contracts.pm import HttpResponse, PMTransport
from forge_integrations.pm.errors import ExternalNotFound, PMAuthError, ProviderError

API = "https://gitlab.com/api/v4"


class GitLabClient:
    def __init__(
        self,
        transport: PMTransport,
        *,
        base_url: str = API,
        auth_header: str | None = None,
        auth_header_name: str = "PRIVATE-TOKEN",
    ) -> None:
        self._t = transport
        self._base = base_url.rstrip("/")
        self._headers: dict[str, str] = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if auth_header:
            self._headers[auth_header_name] = auth_header

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
        resp: HttpResponse = await self._t.request(
            method, self._url(path), headers=self._headers, json=json, params=params
        )
        if resp.status_code in (401, 403):
            raise PMAuthError(f"gitlab auth failed ({resp.status_code})")
        if resp.status_code == 404:
            raise ExternalNotFound(f"gitlab resource not found: {path}")
        if resp.status_code >= 400:
            raise ProviderError(
                f"gitlab error {resp.status_code} on {path}", status_code=resp.status_code
            )
        return resp

    async def get_issue(self, project_id: str, issue_iid: str) -> dict[str, Any]:
        resp = await self._request("GET", f"/projects/{project_id}/issues/{issue_iid}")
        body = resp.json_body or {}
        return dict(body) if isinstance(body, dict) else {}

    async def create_issue(self, project_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        resp = await self._request("POST", f"/projects/{project_id}/issues", json=fields)
        body = resp.json_body or {}
        return dict(body) if isinstance(body, dict) else {}

    async def update_issue(
        self, project_id: str, issue_iid: str, fields: dict[str, Any]
    ) -> dict[str, Any]:
        resp = await self._request("PUT", f"/projects/{project_id}/issues/{issue_iid}", json=fields)
        body = resp.json_body or {}
        return dict(body) if isinstance(body, dict) else {}

    async def list_issues(
        self, project_id: str, *, page: int | None = None, per_page: int = 50
    ) -> tuple[list[dict[str, Any]], str | None]:
        resp = await self._request(
            "GET",
            f"/projects/{project_id}/issues",
            params={"page": page or 1, "per_page": per_page},
        )
        body = resp.json_body or []
        issues = list(body) if isinstance(body, list) else []
        next_page = resp.headers.get("x-next-page") or None
        return issues, next_page or None

    async def me(self) -> dict[str, Any]:
        resp = await self._request("GET", "/user")
        body = resp.json_body or {}
        return dict(body) if isinstance(body, dict) else {}

    async def create_project_hook(self, project_id: str, url: str, token: str) -> dict[str, Any]:
        resp = await self._request(
            "POST",
            f"/projects/{project_id}/hooks",
            json={"url": url, "issues_events": True, "token": token},
        )
        body = resp.json_body or {}
        return dict(body) if isinstance(body, dict) else {}

    async def delete_project_hook(self, project_id: str, hook_id: str) -> None:
        await self._request("DELETE", f"/projects/{project_id}/hooks/{hook_id}")


__all__ = ["API", "GitLabClient"]
