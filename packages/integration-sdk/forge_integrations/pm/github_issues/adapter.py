"""Repository issues over GitHub REST, using issue numbers scoped to owner/repo.

Status/priority overrides follow BaseAdapter's {forge_value: external_label}
convention. Closed state wins over active status labels; terminal labels can
distinguish canceled from completed. App webhooks are registered in the App
manifest, as for GitHub Projects, rather than per PM connection.
"""

from __future__ import annotations

import re
import time
from datetime import datetime
from typing import Any, ClassVar
from urllib.parse import parse_qs, quote, urlparse

import httpx

from forge_contracts.enums import Direction
from forge_contracts.pm import (
    AdapterContext,
    ExternalTask,
    ForgeTask,
    HealthResult,
    PMProvider,
    PMTransport,
    StatusCategory,
    WebhookEvent,
)
from forge_integrations.github import DEFAULT_BASE_URL, GitHubClient
from forge_integrations.pm.base import BaseAdapter
from forge_integrations.pm.errors import ExternalNotFound, MappingError, PMError, ProviderError
from forge_integrations.pm.github_issues import mapping
from forge_integrations.pm.github_issues.client import request_github
from forge_integrations.pm.github_issues.webhooks import parse_github_issues, verify_github_issues

_TERMINAL = {StatusCategory.completed, StatusCategory.canceled}


class GitHubIssuesAdapter(BaseAdapter):
    provider = PMProvider.github_issues
    status_out_table = mapping.STATUS_OUT
    status_in_table = mapping.STATUS_IN
    priority_out_table = mapping.PRIORITY_OUT
    priority_in_table = mapping.PRIORITY_IN
    field_in_table: ClassVar[dict[str, str]] = {"title": "title", "body": "description"}

    def __init__(
        self,
        client: GitHubClient | PMTransport,
        ctx: AdapterContext,
        *,
        auth_header: str | None = None,
    ) -> None:
        super().__init__(
            status_map=ctx.status_map, priority_map=ctx.priority_map, field_map=ctx.field_map
        )
        self.client = client
        self.ctx = ctx
        self.auth_header = auth_header
        self.repo = GitHubClient.owner_repo(ctx.external_project_key)
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repo) or any(
            part in (".", "..") for part in self.repo.split("/")
        ):
            raise ProviderError("github issues requires an owner/repo project key")
        self.path = f"/repos/{self.repo}/issues"

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        return await request_github(
            self.client,
            method,
            path,
            repo=self.repo,
            base_url=self.ctx.external_base_url or DEFAULT_BASE_URL,
            auth_header=self.auth_header,
            **kwargs,
        )

    def _issue_path(self, external_id: str) -> str:
        if not re.fullmatch(r"[1-9][0-9]*", external_id):
            raise ProviderError("github issue id must be a positive issue number")
        return f"{self.path}/{external_id}"

    def _issue_to_external(self, issue: dict[str, Any]) -> ExternalTask:
        if "pull_request" in issue:
            raise ExternalNotFound("pull requests are not github issues")
        labels = [label["name"] if isinstance(label, dict) else label for label in issue["labels"]]
        closed = issue["state"] == "closed"
        status = "closed" if closed else "open"
        category = StatusCategory(self.map_status(status, Direction.IN))
        priority = None
        for label in labels:
            try:
                mapped = StatusCategory(self.map_status(label, Direction.IN))
                if (mapped in _TERMINAL) == closed:
                    status, category = label, mapped
                    break
            except (MappingError, ValueError):
                continue
        for label in labels:
            try:
                self.map_priority(label, Direction.IN)
                priority = label
                break
            except MappingError:
                continue
        number = str(issue["number"])
        return ExternalTask(
            provider=self.provider,
            external_id=number,
            external_key=f"{self.repo}#{number}",
            url=issue["html_url"],
            title=issue["title"],
            description_md=issue.get("body"),
            status_name=status,
            status_category=category,
            priority_token=priority,
            assignee_external_id=(issue.get("assignee") or {}).get("login"),
            labels=labels,
            external_updated_at=datetime.fromisoformat(issue["updated_at"].replace("Z", "+00:00")),
            raw={},
        )

    def _forge_to_payload(self, task: ForgeTask) -> dict[str, Any]:
        status = self.map_status(task.status_category.value, Direction.OUT)
        priority = self.map_priority(task.priority.value, Direction.OUT)
        managed = {
            value.lower()
            for table in (
                self.status_out_table,
                self.status_map,
                self.priority_out_table,
                self.priority_map,
            )
            for value in table.values()
        }
        labels = [label for label in task.label_names if label.lower() not in managed]
        state = "closed" if task.status_category in _TERMINAL else "open"
        if status.lower() in ("open", "closed"):
            state = status.lower()
        else:
            labels.append(status)
        labels.append(priority)
        return {
            "title": task.title,
            "body": task.description_md or "",
            "labels": list(dict.fromkeys(labels)),
            "state": state,
        }

    async def fetch_external(self, external_id: str) -> ExternalTask:
        response = await self._request("GET", self._issue_path(external_id))
        return self._issue_to_external(response.json())

    async def create_external(self, forge_task: ForgeTask) -> ExternalTask:
        payload = self._forge_to_payload(forge_task)
        state = payload.pop("state")  # GitHub creates issues open; closing requires PATCH.
        response = await self._request("POST", self.path, json=payload)
        created = self._issue_to_external(response.json())
        if state == "closed":
            response = await self._request(
                "PATCH", self._issue_path(created.external_id), json={"state": state}
            )
            return self._issue_to_external(response.json())
        return created

    async def update_external(self, external_id: str, forge_task: ForgeTask) -> ExternalTask:
        payload = self._forge_to_payload(forge_task)
        await self.fetch_external(external_id)  # The issues endpoint also accepts PR numbers.
        response = await self._request("PATCH", self._issue_path(external_id), json=payload)
        return self._issue_to_external(response.json())

    async def comment_external(self, external_id: str, body: str) -> str:
        """Post a comment and return its id; does not sync human-authored comments."""
        await self.fetch_external(external_id)
        response = await self._request(
            "POST", f"{self._issue_path(external_id)}/comments", json={"body": body}
        )
        return str(response.json()["id"])

    async def transition_external(self, external_id: str, category: StatusCategory) -> ExternalTask:
        """Move only the issue's status (spec M M2's one mapped transition).

        PATCHes ``state`` alone, adds the target status label, and removes
        stale managed status labels one by one — the whole label set is never
        sent, so a label a person adds concurrently cannot be dropped, and
        title, description, assignee, priority and free labels are never
        rewritten.
        """
        current = await self.fetch_external(external_id)
        status = self.map_status(category.value, Direction.OUT)
        # ``open``/``closed`` are states, never labels; every other managed
        # status label is forge's to add/remove.
        managed_status = {
            value.lower()
            for table in (self.status_out_table, self.status_map)
            for value in table.values()
        } - {"open", "closed"}
        state = "closed" if category in _TERMINAL else "open"
        target_label: str | None = None
        if status.lower() in ("open", "closed"):
            state = status.lower()
        else:
            target_label = status
        issue_path = self._issue_path(external_id)
        await self._request("PATCH", issue_path, json={"state": state})
        current_labels = {label.lower() for label in current.labels}
        if target_label is not None and target_label.lower() not in current_labels:
            await self._request("POST", f"{issue_path}/labels", json={"labels": [target_label]})
        for label in current.labels:
            if label.lower() in managed_status and label.lower() != (target_label or "").lower():
                try:
                    await self._request("DELETE", f"{issue_path}/labels/{quote(label, safe='')}")
                except ExternalNotFound:
                    continue  # already removed (e.g. by a person meanwhile)
        return await self.fetch_external(external_id)

    async def list_external(
        self, *, cursor: str | None = None, limit: int = 50
    ) -> tuple[list[ExternalTask], str | None]:
        page = cursor or "1"
        if not re.fullmatch(r"[1-9][0-9]*", page) or not 1 <= limit <= 100:
            raise ProviderError("github issues requires a positive page and limit of 1..100")
        response = await self._request(
            "GET", self.path, params={"state": "all", "per_page": limit, "page": page}
        )
        tasks = [
            self._issue_to_external(issue)
            for issue in response.json()
            if "pull_request" not in issue
        ]
        next_url = response.links.get("next", {}).get("url", "")
        next_cursor = parse_qs(urlparse(next_url).query).get("page", [None])[0]
        return tasks, next_cursor

    def parse_webhook(self, body: bytes, headers: dict[str, str]) -> WebhookEvent:
        return parse_github_issues(body, headers, repo=self.repo)

    def verify_webhook(self, body: bytes, headers: dict[str, str], secret: str) -> bool:
        return verify_github_issues(body, headers, secret)

    async def register_webhook(self, callback_url: str, secret: str) -> str:
        return f"app-webhook:{self.ctx.connection_id}"

    async def unregister_webhook(self, external_webhook_id: str) -> None:
        return None

    async def get_connection_health(self) -> HealthResult:
        start = time.perf_counter()
        try:
            response = await self._request("GET", f"/repos/{self.repo}")
        except PMError as exc:
            return HealthResult(status="error", provider=self.provider, error=str(exc))
        return HealthResult(
            status="connected",
            provider=self.provider,
            latency_ms=(time.perf_counter() - start) * 1000,
            account=response.json().get("full_name"),
            granted_scopes=list(self.ctx.config.get("granted_scopes", [])),
        )
