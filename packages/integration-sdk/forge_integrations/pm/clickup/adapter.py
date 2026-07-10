"""ClickUpAdapter — implements the async ``PMAdapter`` Protocol over ``ClickUpClient``.

ClickUp models both status and priority as native task fields (unlike Asana's
section-membership / custom-field workarounds), so this adapter is closer in
shape to Jira/monday.com: status and priority are read straight off the task
payload and written directly on create/update. Webhook registration is
team-scoped (``AdapterContext.config["team_id"]``, required only for that
call — every other operation is list/task scoped via ``ctx.external_project_id``).
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
from forge_integrations.pm.clickup import mapping
from forge_integrations.pm.clickup.client import ClickUpClient
from forge_integrations.pm.clickup.webhooks import (
    parse_clickup,
    synthesize_delivery_id,
    verify_clickup,
)
from forge_integrations.pm.errors import MappingError, PMAuthError, ProviderError


def _parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if not value:
        return datetime.now(UTC)
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=UTC)
    except (TypeError, ValueError):
        return datetime.now(UTC)


class ClickUpAdapter(BaseAdapter):
    provider = PMProvider.clickup
    status_out_table = mapping.STATUS_OUT
    status_in_table = mapping.STATUS_IN
    priority_out_table = mapping.PRIORITY_OUT
    priority_in_table = mapping.PRIORITY_IN
    field_in_table: ClassVar[dict[str, str]] = {
        "name": "title",
        "description": "description_md",
    }

    def __init__(self, client: ClickUpClient, ctx: AdapterContext) -> None:
        super().__init__(
            status_map=ctx.status_map,
            priority_map=ctx.priority_map,
            field_map=ctx.field_map,
        )
        self.client = client
        self.ctx = ctx
        self.team_id = str(ctx.config.get("team_id", ""))

    # ------------------------------------------------------------------ #
    # parsing                                                            #
    # ------------------------------------------------------------------ #

    def _task_to_external(self, task: dict[str, Any]) -> ExternalTask:
        task_id = str(task.get("id") or "")
        status_label = str((task.get("status") or {}).get("status") or "")
        status_category: StatusCategory | None = None
        if status_label:
            try:
                status_category = StatusCategory(self.map_status(status_label, Direction.IN))
            except (MappingError, ValueError):
                status_category = None
        priority = task.get("priority") or {}
        assignees = task.get("assignees") or []
        first_assignee = assignees[0] if assignees else {}
        tags = [t.get("name") for t in (task.get("tags") or []) if t.get("name")]
        return ExternalTask(
            provider=PMProvider.clickup,
            external_id=task_id,
            external_key=task.get("custom_id") or task_id,
            url=task.get("url") or task_id,
            title=task.get("name") or "",
            description_md=task.get("description") or task.get("text_content"),
            status_name=status_label,
            status_category=status_category,
            priority_token=priority.get("priority"),
            assignee_external_id=str(first_assignee.get("id"))
            if first_assignee.get("id") is not None
            else None,
            assignee_email=first_assignee.get("email"),
            labels=list(tags),
            external_updated_at=_parse_dt(task.get("date_updated")),
            raw={},
        )

    def _forge_to_fields(self, forge_task: ForgeTask) -> dict[str, Any]:
        status_label = self.map_status(forge_task.status_category.value, Direction.OUT)
        priority_label = self.map_priority(forge_task.priority.value, Direction.OUT)
        fields: dict[str, Any] = {
            "name": forge_task.title,
            "description": forge_task.description_md or "",
            "status": status_label,
        }
        priority_id = mapping.PRIORITY_LABEL_TO_ID.get(priority_label.strip().lower())
        if priority_id is not None:
            fields["priority"] = priority_id
        return fields

    # ------------------------------------------------------------------ #
    # external I/O                                                        #
    # ------------------------------------------------------------------ #

    async def fetch_external(self, external_id: str) -> ExternalTask:
        return self._task_to_external(await self.client.get_task(external_id))

    async def create_external(self, forge_task: ForgeTask) -> ExternalTask:
        created = await self.client.create_task(
            self.ctx.external_project_id, self._forge_to_fields(forge_task)
        )
        return self._task_to_external(created)

    async def update_external(self, external_id: str, forge_task: ForgeTask) -> ExternalTask:
        updated = await self.client.update_task(external_id, self._forge_to_fields(forge_task))
        return self._task_to_external(updated)

    async def list_external(
        self, *, cursor: str | None = None, limit: int = 50
    ) -> tuple[list[ExternalTask], str | None]:
        page = int(cursor) if cursor else None
        tasks, next_page = await self.client.list_list_tasks(
            self.ctx.external_project_id, page=page
        )
        return [self._task_to_external(t) for t in tasks], (
            str(next_page) if next_page is not None else None
        )

    # ------------------------------------------------------------------ #
    # webhook + health                                                   #
    # ------------------------------------------------------------------ #

    def parse_webhook(self, body: bytes, headers: dict[str, str]) -> WebhookEvent:
        import json

        try:
            data = json.loads(body or b"{}")
        except ValueError as exc:
            raise ProviderError(f"invalid clickup webhook body: {exc}") from exc
        delivery_id = synthesize_delivery_id(body)
        return parse_clickup(data, delivery_id=delivery_id, signature_valid=True)

    def verify_webhook(self, body: bytes, headers: dict[str, str], secret: str) -> bool:
        lowered = {k.lower(): v for k, v in headers.items()}
        signature = lowered.get("x-signature")
        return verify_clickup(secret, body, signature)

    async def register_webhook(self, callback_url: str, secret: str) -> str:
        if not self.team_id:
            raise ProviderError("clickup register_webhook requires config['team_id']")
        result = await self.client.create_webhook(
            self.team_id, self.ctx.external_project_id, callback_url
        )
        webhook = result.get("webhook") or {}
        return str(webhook.get("id") or result.get("id") or "")

    async def unregister_webhook(self, external_webhook_id: str) -> None:
        await self.client.delete_webhook(external_webhook_id)

    async def get_connection_health(self) -> HealthResult:
        start = time.perf_counter()
        try:
            me = await self.client.me()
        except (PMAuthError, ProviderError) as exc:
            return HealthResult(status="error", provider=PMProvider.clickup, error=str(exc))
        latency = (time.perf_counter() - start) * 1000
        return HealthResult(
            status="connected",
            provider=PMProvider.clickup,
            latency_ms=latency,
            account=me.get("email") or me.get("username"),
            granted_scopes=list(self.ctx.config.get("granted_scopes", [])),
        )


__all__ = ["ClickUpAdapter"]
