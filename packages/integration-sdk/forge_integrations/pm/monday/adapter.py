"""MondayAdapter — implements the async ``PMAdapter`` Protocol over ``MondayClient``.

monday.com's native Kanban column is a "status"-type column whose current
value is a board-defined free-text label (column id configurable via
``AdapterContext.config["status_column_id"]``, default ``"status"``) — that is
the primary status-category source, resolved/written exactly like Jira
transitions and Linear workflow states. Items are *additionally* placed into
the board group ("board groups ... -> status categories" per F40) whose title
matches the target status label when one exists, but a missing group is
tolerated (group placement is organizational, not the sync-of-record).

Priority is a second configurable column (default id ``"priority"``).
Description is optional (``config["description_column_id"]``, unset by
default — many boards have no dedicated long-text column, mirroring the
"item name is the primary field" reality of monday.com boards). Assignee and
labels are read-only best-effort (a "person"/"tags" column's display text),
matching Linear's precedent of not round-tripping supplementary fields on
write.
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
from forge_integrations.pm.monday import mapping
from forge_integrations.pm.monday.client import MondayClient
from forge_integrations.pm.monday.webhooks import SECRET_HEADER, parse_monday, verify_monday


def _parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if not value:
        return datetime.now(UTC)
    text = str(value).replace(" UTC", "").replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return datetime.now(UTC)


class MondayAdapter(BaseAdapter):
    provider = PMProvider.monday
    status_out_table = mapping.STATUS_OUT
    status_in_table = mapping.STATUS_IN
    priority_out_table = mapping.PRIORITY_OUT
    priority_in_table = mapping.PRIORITY_IN
    field_in_table: ClassVar[dict[str, str]] = {
        "name": "title",
    }

    def __init__(self, client: MondayClient, ctx: AdapterContext) -> None:
        super().__init__(
            status_map=ctx.status_map,
            priority_map=ctx.priority_map,
            field_map=ctx.field_map,
        )
        self.client = client
        self.ctx = ctx
        self.status_column_id = str(
            ctx.config.get("status_column_id", mapping.DEFAULT_STATUS_COLUMN_ID)
        )
        self.priority_column_id = str(
            ctx.config.get("priority_column_id", mapping.DEFAULT_PRIORITY_COLUMN_ID)
        )
        self.description_column_id = ctx.config.get("description_column_id")
        self.tags_column_id = ctx.config.get("tags_column_id")

    # ------------------------------------------------------------------ #
    # parsing                                                            #
    # ------------------------------------------------------------------ #

    def _columns(self, item: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return {cv.get("id"): cv for cv in item.get("column_values") or []}

    def _item_to_external(self, item: dict[str, Any]) -> ExternalTask:
        columns = self._columns(item)
        status_text = (columns.get(self.status_column_id) or {}).get("text") or ""
        try:
            status_category = StatusCategory(self.map_status(status_text, Direction.IN))
        except (MappingError, ValueError):
            status_category = None
        priority_text = (columns.get(self.priority_column_id) or {}).get("text") or None
        description_md = None
        if self.description_column_id:
            description_md = (columns.get(self.description_column_id) or {}).get("text")
        labels: list[str] = []
        if self.tags_column_id:
            raw = (columns.get(self.tags_column_id) or {}).get("text") or ""
            labels = [t.strip() for t in raw.split(",") if t.strip()]
        gid = str(item.get("id") or "")
        return ExternalTask(
            provider=PMProvider.monday,
            external_id=gid,
            external_key=gid,
            url=item.get("url") or gid,
            title=item.get("name") or "",
            description_md=description_md,
            status_name=status_text,
            status_category=status_category,
            priority_token=priority_text,
            assignee_external_id=None,
            assignee_email=None,
            labels=labels,
            external_updated_at=_parse_dt(item.get("updated_at")),
            raw={},
        )

    def _column_values(self, forge_task: ForgeTask) -> dict[str, Any]:
        status_label = self.map_status(forge_task.status_category.value, Direction.OUT)
        priority_label = self.map_priority(forge_task.priority.value, Direction.OUT)
        values: dict[str, Any] = {
            self.status_column_id: {"label": status_label},
            self.priority_column_id: {"label": priority_label},
        }
        if self.description_column_id:
            values[self.description_column_id] = {"text": forge_task.description_md or ""}
        return values

    async def _group_id_for(self, forge_task: ForgeTask) -> str | None:
        """Best-effort board-group placement matching the target status label.

        Group placement is organizational sugar, not the sync-of-record (the
        status column is), so an unmatched group is tolerated (``None``).
        """
        target_name = self.map_status(forge_task.status_category.value, Direction.OUT)
        groups = await self.client.list_board_groups(self.ctx.external_project_id)
        for group in groups:
            if str(group.get("title") or "").strip().lower() == target_name.strip().lower():
                return str(group.get("id"))
        return None

    # ------------------------------------------------------------------ #
    # external I/O                                                        #
    # ------------------------------------------------------------------ #

    async def fetch_external(self, external_id: str) -> ExternalTask:
        return self._item_to_external(await self.client.get_item(external_id))

    async def create_external(self, forge_task: ForgeTask) -> ExternalTask:
        group_id = await self._group_id_for(forge_task)
        created = await self.client.create_item(
            self.ctx.external_project_id,
            group_id,
            forge_task.title,
            self._column_values(forge_task),
        )
        return self._item_to_external(created)

    async def update_external(self, external_id: str, forge_task: ForgeTask) -> ExternalTask:
        updated = await self.client.change_multiple_column_values(
            self.ctx.external_project_id, external_id, self._column_values(forge_task)
        )
        if not updated:
            # Some monday API versions echo only {id}; re-fetch to normalize.
            return await self.fetch_external(external_id)
        return self._item_to_external(updated)

    async def list_external(
        self, *, cursor: str | None = None, limit: int = 50
    ) -> tuple[list[ExternalTask], str | None]:
        items, next_cursor = await self.client.list_board_items(
            self.ctx.external_project_id, cursor=cursor, limit=limit
        )
        return [self._item_to_external(i) for i in items], next_cursor

    # ------------------------------------------------------------------ #
    # webhook + health                                                   #
    # ------------------------------------------------------------------ #

    def parse_webhook(self, body: bytes, headers: dict[str, str]) -> WebhookEvent:
        import hashlib
        import json

        try:
            data = json.loads(body or b"{}")
        except ValueError as exc:
            raise ProviderError(f"invalid monday webhook body: {exc}") from exc
        delivery_id = hashlib.sha256(body).hexdigest()[:32]
        return parse_monday(data, delivery_id=delivery_id, signature_valid=True)

    def verify_webhook(self, body: bytes, headers: dict[str, str], secret: str) -> bool:
        lowered = {k.lower(): v for k, v in headers.items()}
        provided = lowered.get(SECRET_HEADER)
        return verify_monday(secret, provided)

    async def register_webhook(self, callback_url: str, secret: str) -> str:
        result = await self.client.create_webhook(self.ctx.external_project_id, callback_url)
        return str(result.get("id") or "")

    async def unregister_webhook(self, external_webhook_id: str) -> None:
        await self.client.delete_webhook(external_webhook_id)

    async def get_connection_health(self) -> HealthResult:
        start = time.perf_counter()
        try:
            me = await self.client.me()
        except (PMAuthError, ProviderError) as exc:
            return HealthResult(status="error", provider=PMProvider.monday, error=str(exc))
        latency = (time.perf_counter() - start) * 1000
        return HealthResult(
            status="connected",
            provider=PMProvider.monday,
            latency_ms=latency,
            account=me.get("email") or me.get("name"),
            granted_scopes=list(self.ctx.config.get("granted_scopes", [])),
        )


__all__ = ["MondayAdapter"]
