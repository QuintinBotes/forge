"""AsanaAdapter — implements the async ``PMAdapter`` Protocol over ``AsanaClient``.

Asana has no native "status" or "priority" field: status is modeled as section
(board column) membership within the connected project, and priority is a
project custom field (name configurable, default ``"Priority"``). Both are
resolved by lookup at write time — exactly like Jira resolves a transition and
Linear resolves a workflow-state id — and a missing target section raises
:class:`ProviderError` (status is never silently dropped). A missing priority
custom field is tolerated (priority is supplementary, not part of the core
sync grain), matching Linear's precedent of not round-tripping labels on write.
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
from forge_integrations.pm.asana import mapping
from forge_integrations.pm.asana.client import AsanaClient
from forge_integrations.pm.asana.webhooks import parse_asana, synthesize_delivery_id, verify_asana
from forge_integrations.pm.base import BaseAdapter
from forge_integrations.pm.errors import MappingError, PMAuthError, ProviderError


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


class AsanaAdapter(BaseAdapter):
    provider = PMProvider.asana
    status_out_table = mapping.STATUS_OUT
    status_in_table = mapping.STATUS_IN
    priority_out_table = mapping.PRIORITY_OUT
    priority_in_table = mapping.PRIORITY_IN
    field_in_table: ClassVar[dict[str, str]] = {
        "name": "title",
        "notes": "description",
    }

    def __init__(self, client: AsanaClient, ctx: AdapterContext) -> None:
        super().__init__(
            status_map=ctx.status_map,
            priority_map=ctx.priority_map,
            field_map=ctx.field_map,
        )
        self.client = client
        self.ctx = ctx
        self.priority_field_name = str(
            ctx.config.get("priority_field_name", mapping.DEFAULT_PRIORITY_FIELD_NAME)
        )

    # ------------------------------------------------------------------ #
    # parsing                                                            #
    # ------------------------------------------------------------------ #

    def _section_name(self, task: dict[str, Any]) -> str | None:
        for membership in task.get("memberships") or []:
            project = membership.get("project") or {}
            if str(project.get("gid") or "") == self.ctx.external_project_id:
                section = membership.get("section") or {}
                return section.get("name")
        return None

    def _priority_token(self, task: dict[str, Any]) -> str | None:
        for field in task.get("custom_fields") or []:
            name = str(field.get("name") or "")
            if name.strip().lower() == self.priority_field_name.strip().lower():
                enum_value = field.get("enum_value") or {}
                return enum_value.get("name")
        return None

    def _task_to_external(self, task: dict[str, Any]) -> ExternalTask:
        gid = str(task.get("gid") or "")
        section_name = self._section_name(task)
        status_category: StatusCategory | None = None
        if section_name:
            try:
                forge_cat = self.map_status(section_name, Direction.IN)
                status_category = StatusCategory(forge_cat)
            except (MappingError, ValueError):
                status_category = None
        assignee = task.get("assignee") or {}
        tags = [t.get("name") for t in (task.get("tags") or []) if t.get("name")]
        return ExternalTask(
            provider=PMProvider.asana,
            external_id=gid,
            external_key=gid,
            url=task.get("permalink_url") or gid,
            title=task.get("name") or "",
            description_md=task.get("notes"),
            status_name=section_name or "",
            status_category=status_category,
            priority_token=self._priority_token(task),
            assignee_external_id=assignee.get("gid"),
            assignee_email=assignee.get("email"),
            labels=list(tags),
            external_updated_at=_parse_dt(task.get("modified_at")),
            raw={},
        )

    def _forge_to_fields(self, forge_task: ForgeTask) -> dict[str, Any]:
        return {
            "name": forge_task.title,
            "notes": forge_task.description_md or "",
        }

    async def _section_gid_for(self, forge_task: ForgeTask) -> str:
        target_name = self.map_status(forge_task.status_category.value, Direction.OUT)
        sections = await self.client.list_sections(self.ctx.external_project_id)
        for section in sections:
            if str(section.get("name") or "").strip().lower() == target_name.strip().lower():
                return str(section.get("gid"))
        raise ProviderError(
            f"no asana section named {target_name!r} in project {self.ctx.external_project_id!r}"
        )

    async def _priority_custom_field(self, forge_task: ForgeTask) -> dict[str, str] | None:
        target_option = self.map_priority(forge_task.priority.value, Direction.OUT)
        settings = await self.client.list_custom_field_settings(self.ctx.external_project_id)
        for setting in settings:
            field = setting.get("custom_field") or {}
            if str(field.get("name") or "").strip().lower() != self.priority_field_name.lower():
                continue
            for option in field.get("enum_options") or []:
                if str(option.get("name") or "").strip().lower() == target_option.strip().lower():
                    return {str(field.get("gid")): str(option.get("gid"))}
        return None

    # ------------------------------------------------------------------ #
    # external I/O                                                        #
    # ------------------------------------------------------------------ #

    async def fetch_external(self, external_id: str) -> ExternalTask:
        return self._task_to_external(await self.client.get_task(external_id))

    async def create_external(self, forge_task: ForgeTask) -> ExternalTask:
        fields = self._forge_to_fields(forge_task)
        fields["projects"] = [self.ctx.external_project_id]
        custom_fields = await self._priority_custom_field(forge_task)
        if custom_fields:
            fields["custom_fields"] = custom_fields
        created = await self.client.create_task(fields)
        gid = str(created.get("gid") or "")
        section_gid = await self._section_gid_for(forge_task)
        await self.client.add_task_to_section(section_gid, gid)
        return await self.fetch_external(gid)

    async def update_external(self, external_id: str, forge_task: ForgeTask) -> ExternalTask:
        fields = self._forge_to_fields(forge_task)
        custom_fields = await self._priority_custom_field(forge_task)
        if custom_fields:
            fields["custom_fields"] = custom_fields
        await self.client.update_task(external_id, fields)
        section_gid = await self._section_gid_for(forge_task)
        await self.client.add_task_to_section(section_gid, external_id)
        return await self.fetch_external(external_id)

    async def list_external(
        self, *, cursor: str | None = None, limit: int = 50
    ) -> tuple[list[ExternalTask], str | None]:
        tasks, next_cursor = await self.client.list_project_tasks(
            self.ctx.external_project_id, offset=cursor, limit=limit
        )
        return [self._task_to_external(t) for t in tasks], next_cursor

    # ------------------------------------------------------------------ #
    # webhook + health                                                   #
    # ------------------------------------------------------------------ #

    def parse_webhook(self, body: bytes, headers: dict[str, str]) -> WebhookEvent:
        import json

        try:
            data = json.loads(body or b"{}")
        except ValueError as exc:
            raise ProviderError(f"invalid asana webhook body: {exc}") from exc
        delivery_id = synthesize_delivery_id(body)
        return parse_asana(data, delivery_id=delivery_id, signature_valid=True)

    def verify_webhook(self, body: bytes, headers: dict[str, str], secret: str) -> bool:
        lowered = {k.lower(): v for k, v in headers.items()}
        signature = lowered.get("x-hook-signature")
        return verify_asana(secret, body, signature)

    async def register_webhook(self, callback_url: str, secret: str) -> str:
        result = await self.client.create_webhook(self.ctx.external_project_id, callback_url)
        return str(result.get("gid") or "")

    async def unregister_webhook(self, external_webhook_id: str) -> None:
        await self.client.delete_webhook(external_webhook_id)

    async def get_connection_health(self) -> HealthResult:
        start = time.perf_counter()
        try:
            me = await self.client.me()
        except (PMAuthError, ProviderError) as exc:
            return HealthResult(status="error", provider=PMProvider.asana, error=str(exc))
        latency = (time.perf_counter() - start) * 1000
        return HealthResult(
            status="connected",
            provider=PMProvider.asana,
            latency_ms=latency,
            account=me.get("email") or me.get("name"),
            granted_scopes=list(self.ctx.config.get("granted_scopes", [])),
        )


__all__ = ["AsanaAdapter"]
