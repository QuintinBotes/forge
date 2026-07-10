"""GitHubProjectsAdapter — implements the async ``PMAdapter`` Protocol over
``GitHubProjectsClient``.

Tasks map onto Projects v2 *draft issues* so a project-only board (no backing
repository Issue) is fully supported. Status/priority are single-select field
values (see :mod:`forge_integrations.pm.github_projects.mapping`), resolved by
field/option lookup at write time — the same pattern Jira uses for transitions
and Linear for workflow states — with a missing *Status* option raising
:class:`ProviderError` (never silently dropped) while a missing *Priority*
field/option is tolerated (it is supplementary, matching Asana's precedent).

``projects_v2_item`` webhooks are delivered to the GitHub App's single
org/installation-level endpoint (configured once, in the App manifest) rather
than per-connection like Jira/Linear/Asana/Monday, so
:meth:`register_webhook`/:meth:`unregister_webhook` are deliberate no-ops here
(there is nothing to create or delete per connection) — the endpoint itself
still verifies/parses real deliveries via
:mod:`forge_integrations.pm.github_projects.webhooks`.
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
from forge_integrations.pm.github_projects import mapping
from forge_integrations.pm.github_projects.client import GitHubProjectsClient
from forge_integrations.pm.github_projects.webhooks import (
    SIGNATURE_HEADER,
    parse_github_projects,
    synthesize_delivery_id,
    verify_github_projects,
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


class GitHubProjectsAdapter(BaseAdapter):
    provider = PMProvider.github_projects
    status_out_table = mapping.STATUS_OUT
    status_in_table = mapping.STATUS_IN
    priority_out_table = mapping.PRIORITY_OUT
    priority_in_table = mapping.PRIORITY_IN
    field_in_table: ClassVar[dict[str, str]] = {
        "title": "title",
        "body": "description",
    }

    def __init__(self, client: GitHubProjectsClient, ctx: AdapterContext) -> None:
        super().__init__(
            status_map=ctx.status_map,
            priority_map=ctx.priority_map,
            field_map=ctx.field_map,
        )
        self.client = client
        self.ctx = ctx
        self.status_field_name = str(
            ctx.config.get("status_field_name", mapping.DEFAULT_STATUS_FIELD_NAME)
        )
        self.priority_field_name = str(
            ctx.config.get("priority_field_name", mapping.DEFAULT_PRIORITY_FIELD_NAME)
        )

    # ------------------------------------------------------------------ #
    # parsing                                                            #
    # ------------------------------------------------------------------ #

    def _field_values(self, item: dict[str, Any]) -> dict[str, str]:
        values: dict[str, str] = {}
        for node in (item.get("fieldValues") or {}).get("nodes") or []:
            field = node.get("field") or {}
            name = field.get("name")
            if name and node.get("name") is not None:
                values[name] = node["name"]
        return values

    def _item_to_external(self, item: dict[str, Any]) -> ExternalTask:
        content = item.get("content") or {}
        values = self._field_values(item)
        status_text = values.get(self.status_field_name, "")
        try:
            status_category = StatusCategory(self.map_status(status_text, Direction.IN))
        except (MappingError, ValueError):
            status_category = None
        gid = str(item.get("id") or "")
        return ExternalTask(
            provider=PMProvider.github_projects,
            external_id=gid,
            external_key=gid,
            url=content.get("url") or gid,
            title=content.get("title") or "",
            description_md=content.get("body"),
            status_name=status_text,
            status_category=status_category,
            priority_token=values.get(self.priority_field_name),
            assignee_external_id=None,
            assignee_email=None,
            labels=[],
            external_updated_at=_parse_dt(item.get("updatedAt")),
            raw={"content_id": content.get("id")},
        )

    async def _set_field(
        self, item_id: str, field_name: str, target_option: str, *, required: bool
    ) -> None:
        fields = await self.client.list_project_fields(self.ctx.external_project_id)
        for field in fields:
            if str(field.get("name") or "").strip().lower() != field_name.strip().lower():
                continue
            for option in field.get("options") or []:
                if str(option.get("name") or "").strip().lower() == target_option.strip().lower():
                    await self.client.update_single_select_field(
                        self.ctx.external_project_id, item_id, str(field["id"]), str(option["id"])
                    )
                    return
            if required:
                raise ProviderError(
                    f"no github projects option {target_option!r} for field {field_name!r}"
                )
            return
        if required:
            raise ProviderError(
                f"no github projects field named {field_name!r} in project "
                f"{self.ctx.external_project_id!r}"
            )

    async def _apply_fields(self, item_id: str, forge_task: ForgeTask) -> None:
        target_status = self.map_status(forge_task.status_category.value, Direction.OUT)
        await self._set_field(item_id, self.status_field_name, target_status, required=True)
        target_priority = self.map_priority(forge_task.priority.value, Direction.OUT)
        await self._set_field(item_id, self.priority_field_name, target_priority, required=False)

    # ------------------------------------------------------------------ #
    # external I/O                                                        #
    # ------------------------------------------------------------------ #

    async def fetch_external(self, external_id: str) -> ExternalTask:
        return self._item_to_external(await self.client.get_item(external_id))

    async def create_external(self, forge_task: ForgeTask) -> ExternalTask:
        created = await self.client.add_draft_issue(
            self.ctx.external_project_id, forge_task.title, forge_task.description_md or ""
        )
        item_id = str(created.get("id") or "")
        await self._apply_fields(item_id, forge_task)
        return await self.fetch_external(item_id)

    async def update_external(self, external_id: str, forge_task: ForgeTask) -> ExternalTask:
        current = await self.client.get_item(external_id)
        content = current.get("content") or {}
        content_id = content.get("id")
        if content_id:
            await self.client.update_draft_issue(
                str(content_id), forge_task.title, forge_task.description_md or ""
            )
        await self._apply_fields(external_id, forge_task)
        return await self.fetch_external(external_id)

    async def list_external(
        self, *, cursor: str | None = None, limit: int = 50
    ) -> tuple[list[ExternalTask], str | None]:
        items, next_cursor = await self.client.list_items(
            self.ctx.external_project_id, after=cursor, first=limit
        )
        return [self._item_to_external(i) for i in items], next_cursor

    # ------------------------------------------------------------------ #
    # webhook + health                                                   #
    # ------------------------------------------------------------------ #

    def parse_webhook(self, body: bytes, headers: dict[str, str]) -> WebhookEvent:
        import json

        try:
            data = json.loads(body or b"{}")
        except ValueError as exc:
            raise ProviderError(f"invalid github projects webhook body: {exc}") from exc
        lowered = {k.lower(): v for k, v in headers.items()}
        delivery_id = lowered.get("x-github-delivery") or synthesize_delivery_id(body)
        return parse_github_projects(data, delivery_id=delivery_id, signature_valid=True)

    def verify_webhook(self, body: bytes, headers: dict[str, str], secret: str) -> bool:
        lowered = {k.lower(): v for k, v in headers.items()}
        signature = lowered.get(SIGNATURE_HEADER)
        return verify_github_projects(secret, body, signature)

    async def register_webhook(self, callback_url: str, secret: str) -> str:
        # projects_v2_item deliveries arrive at the App's single installation-
        # level endpoint (configured once in the App manifest) — there is no
        # per-connection webhook resource to create.
        return f"app-webhook:{self.ctx.connection_id}"

    async def unregister_webhook(self, external_webhook_id: str) -> None:
        return None

    async def get_connection_health(self) -> HealthResult:
        start = time.perf_counter()
        try:
            viewer = await self.client.viewer()
        except (PMAuthError, ProviderError) as exc:
            return HealthResult(status="error", provider=PMProvider.github_projects, error=str(exc))
        latency = (time.perf_counter() - start) * 1000
        return HealthResult(
            status="connected",
            provider=PMProvider.github_projects,
            latency_ms=latency,
            account=viewer.get("login") or viewer.get("email"),
            granted_scopes=list(self.ctx.config.get("granted_scopes", [])),
        )


__all__ = ["GitHubProjectsAdapter"]
