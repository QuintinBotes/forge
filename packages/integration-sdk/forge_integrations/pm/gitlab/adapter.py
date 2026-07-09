"""GitLabAdapter — implements the async ``PMAdapter`` Protocol over ``GitLabClient``.

GitLab issues carry only a binary open/closed ``state``: status *and*
priority are modeled as labels (distinct name pools — see
:mod:`forge_integrations.pm.gitlab.mapping`), matched by name off the issue's
``labels`` array exactly like Trello's board-label priority. Writes replace
any existing recognized status/priority label with the new target (computed
from a fresh label snapshot on update, so unrelated labels are preserved) and
additionally set ``state_event`` (closed for ``completed``/``canceled``, open
otherwise) so a board that only tracks open/closed still gets a reasonable
signal — mirroring monday.com's precedent of a tolerated secondary signal.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any, ClassVar

from forge_contracts.enums import Direction
from forge_contracts.pm import (
    AdapterContext,
    ExternalTask,
    ForgeTask,
    HealthResult,
    PMProvider,
    StatusCategory,
    WebhookEvent,
)
from forge_integrations.pm.base import BaseAdapter
from forge_integrations.pm.errors import MappingError, PMAuthError, ProviderError
from forge_integrations.pm.gitlab import mapping
from forge_integrations.pm.gitlab.client import GitLabClient
from forge_integrations.pm.gitlab.webhooks import (
    DELIVERY_ID_HEADER,
    parse_gitlab,
    synthesize_delivery_id,
    verify_gitlab,
)


def _parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if not value:
        return datetime.now(UTC)
    text = str(value).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return datetime.now(UTC)


class GitLabAdapter(BaseAdapter):
    provider = PMProvider.gitlab
    status_out_table = mapping.STATUS_OUT
    status_in_table = mapping.STATUS_IN
    priority_out_table = mapping.PRIORITY_OUT
    priority_in_table = mapping.PRIORITY_IN
    field_in_table: ClassVar[dict[str, str]] = {
        "title": "title",
        "description": "description_md",
    }

    def __init__(self, client: GitLabClient, ctx: AdapterContext) -> None:
        super().__init__(
            status_map=ctx.status_map,
            priority_map=ctx.priority_map,
            field_map=ctx.field_map,
        )
        self.client = client
        self.ctx = ctx

    # ------------------------------------------------------------------ #
    # label pools                                                         #
    # ------------------------------------------------------------------ #

    def _status_label_pool(self) -> set[str]:
        return {v.strip().lower() for v in {**self.status_out_table, **self.status_map}.values()}

    def _priority_label_pool(self) -> set[str]:
        return {
            v.strip().lower() for v in {**self.priority_out_table, **self.priority_map}.values()
        }

    # ------------------------------------------------------------------ #
    # parsing                                                            #
    # ------------------------------------------------------------------ #

    def _issue_to_external(self, issue: dict[str, Any]) -> ExternalTask:
        iid = str(issue.get("iid") or "")
        labels = [str(label) for label in (issue.get("labels") or [])]
        status_label = next(
            (label for label in labels if label.strip().lower() in self._status_label_pool()),
            None,
        )
        if status_label is not None:
            try:
                status_category = StatusCategory(self.map_status(status_label, Direction.IN))
            except (MappingError, ValueError):
                status_category = None
        else:
            status_category = (
                StatusCategory.completed
                if issue.get("state") == "closed"
                else StatusCategory.unstarted
            )
        priority_label = next(
            (label for label in labels if label.strip().lower() in self._priority_label_pool()),
            None,
        )
        assignee = issue.get("assignee") or {}
        return ExternalTask(
            provider=PMProvider.gitlab,
            external_id=iid,
            external_key=f"#{iid}" if iid else iid,
            url=issue.get("web_url") or iid,
            title=issue.get("title") or "",
            description_md=issue.get("description"),
            status_name=status_label or str(issue.get("state") or ""),
            status_category=status_category,
            priority_token=priority_label,
            assignee_external_id=str(assignee.get("id")) if assignee.get("id") else None,
            assignee_email=assignee.get("email"),
            labels=labels,
            external_updated_at=_parse_dt(issue.get("updated_at")),
            raw={},
        )

    def _target_labels(self, forge_task: ForgeTask, existing_labels: list[str]) -> list[str]:
        status_pool = self._status_label_pool()
        priority_pool = self._priority_label_pool()
        kept = [
            label
            for label in existing_labels
            if label.strip().lower() not in status_pool
            and label.strip().lower() not in priority_pool
        ]
        status_label = self.map_status(forge_task.status_category.value, Direction.OUT)
        priority_label = self.map_priority(forge_task.priority.value, Direction.OUT)
        return [*kept, status_label, priority_label]

    def _state_event(self, forge_task: ForgeTask) -> str:
        if forge_task.status_category in (StatusCategory.completed, StatusCategory.canceled):
            return "close"
        return "reopen"

    # ------------------------------------------------------------------ #
    # external I/O                                                        #
    # ------------------------------------------------------------------ #

    async def fetch_external(self, external_id: str) -> ExternalTask:
        issue = await self.client.get_issue(self.ctx.external_project_id, external_id)
        return self._issue_to_external(issue)

    async def create_external(self, forge_task: ForgeTask) -> ExternalTask:
        labels = self._target_labels(forge_task, [])
        created = await self.client.create_issue(
            self.ctx.external_project_id,
            {
                "title": forge_task.title,
                "description": forge_task.description_md or "",
                "labels": ",".join(labels),
            },
        )
        return self._issue_to_external(created)

    async def update_external(self, external_id: str, forge_task: ForgeTask) -> ExternalTask:
        current = await self.client.get_issue(self.ctx.external_project_id, external_id)
        existing_labels = [str(label) for label in (current.get("labels") or [])]
        labels = self._target_labels(forge_task, existing_labels)
        updated = await self.client.update_issue(
            self.ctx.external_project_id,
            external_id,
            {
                "title": forge_task.title,
                "description": forge_task.description_md or "",
                "labels": ",".join(labels),
                "state_event": self._state_event(forge_task),
            },
        )
        return self._issue_to_external(updated)

    async def list_external(
        self, *, cursor: str | None = None, limit: int = 50
    ) -> tuple[list[ExternalTask], str | None]:
        page = int(cursor) if cursor else None
        issues, next_page = await self.client.list_issues(
            self.ctx.external_project_id, page=page, per_page=limit
        )
        return [self._issue_to_external(i) for i in issues], next_page

    # ------------------------------------------------------------------ #
    # webhook + health                                                   #
    # ------------------------------------------------------------------ #

    def parse_webhook(self, body: bytes, headers: dict[str, str]) -> WebhookEvent:
        import json

        try:
            data = json.loads(body or b"{}")
        except ValueError as exc:
            raise ProviderError(f"invalid gitlab webhook body: {exc}") from exc
        lowered = {k.lower(): v for k, v in headers.items()}
        delivery_id = lowered.get(DELIVERY_ID_HEADER) or synthesize_delivery_id(body)
        return parse_gitlab(data, delivery_id=delivery_id, signature_valid=True)

    def verify_webhook(self, body: bytes, headers: dict[str, str], secret: str) -> bool:
        lowered = {k.lower(): v for k, v in headers.items()}
        return verify_gitlab(secret, lowered.get("x-gitlab-token"))

    async def register_webhook(self, callback_url: str, secret: str) -> str:
        result = await self.client.create_project_hook(
            self.ctx.external_project_id, callback_url, secret
        )
        return str(result.get("id") or "")

    async def unregister_webhook(self, external_webhook_id: str) -> None:
        await self.client.delete_project_hook(self.ctx.external_project_id, external_webhook_id)

    async def get_connection_health(self) -> HealthResult:
        start = time.perf_counter()
        try:
            me = await self.client.me()
        except (PMAuthError, ProviderError) as exc:
            return HealthResult(status="error", provider=PMProvider.gitlab, error=str(exc))
        latency = (time.perf_counter() - start) * 1000
        return HealthResult(
            status="connected",
            provider=PMProvider.gitlab,
            latency_ms=latency,
            account=me.get("email") or me.get("username"),
            granted_scopes=list(self.ctx.config.get("granted_scopes", [])),
        )


__all__ = ["GitLabAdapter"]
