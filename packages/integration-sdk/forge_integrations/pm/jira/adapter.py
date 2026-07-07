"""JiraAdapter — implements the async ``PMAdapter`` Protocol over ``JiraClient``."""

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
from forge_integrations.pm.jira import mapping
from forge_integrations.pm.jira.client import JiraClient
from forge_integrations.pm.jira.webhooks import (
    parse_jira,
    synthesize_delivery_id,
    verify_jira,
)

WEBHOOK_SECRET_HEADER = "x-forge-pm-secret"


def _parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if not value:
        return datetime.now(UTC)
    text = str(value)
    # Jira uses e.g. 2024-01-02T10:00:00.000+0000 (no colon in offset).
    if len(text) >= 5 and (text[-5] in "+-") and text[-3] != ":":
        text = f"{text[:-2]}:{text[-2:]}"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return datetime.now(UTC)


class JiraAdapter(BaseAdapter):
    provider = PMProvider.jira
    status_out_table = mapping.STATUS_OUT
    status_in_table = mapping.STATUS_IN
    priority_out_table = mapping.PRIORITY_OUT
    priority_in_table = mapping.PRIORITY_IN
    field_in_table: ClassVar[dict[str, str]] = {
        "summary": "title",
        "description": "description",
    }

    def __init__(self, client: JiraClient, ctx: AdapterContext) -> None:
        super().__init__(
            status_map=ctx.status_map,
            priority_map=ctx.priority_map,
            field_map=ctx.field_map,
        )
        self.client = client
        self.ctx = ctx
        self.issue_type = str(ctx.config.get("issue_type", "Task"))

    # ------------------------------------------------------------------ #
    # parsing                                                            #
    # ------------------------------------------------------------------ #

    def _issue_to_external(self, issue: dict[str, Any]) -> ExternalTask:
        fields = issue.get("fields") or {}
        status = fields.get("status") or {}
        category_key = ((status.get("statusCategory") or {}).get("key")) or "new"
        try:
            forge_cat = self.map_status(category_key, Direction.IN)
            status_category = StatusCategory(forge_cat)
        except (MappingError, ValueError):
            status_category = None
        priority = fields.get("priority") or {}
        assignee = fields.get("assignee") or {}
        key = issue.get("key") or ""
        base = (self.ctx.external_base_url or "").rstrip("/")
        return ExternalTask(
            provider=PMProvider.jira,
            external_id=str(issue.get("id") or ""),
            external_key=key,
            url=f"{base}/browse/{key}" if base else key,
            title=fields.get("summary") or "",
            description_md=mapping.adf_to_markdown(fields.get("description")),
            status_name=status.get("name") or "",
            status_category=status_category,
            priority_token=priority.get("name"),
            assignee_external_id=assignee.get("accountId"),
            assignee_email=assignee.get("emailAddress"),
            labels=list(fields.get("labels") or []),
            external_updated_at=_parse_dt(fields.get("updated")),
            raw={},
        )

    def _forge_to_fields(self, forge_task: ForgeTask) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "summary": forge_task.title,
            "description": mapping.markdown_to_adf(forge_task.description_md),
            "priority": {"name": self.map_priority(forge_task.priority.value, Direction.OUT)},
        }
        if forge_task.label_names:
            fields["labels"] = list(forge_task.label_names)
        return fields

    # ------------------------------------------------------------------ #
    # external I/O                                                        #
    # ------------------------------------------------------------------ #

    async def fetch_external(self, external_id: str) -> ExternalTask:
        return self._issue_to_external(await self.client.get_issue(external_id))

    async def create_external(self, forge_task: ForgeTask) -> ExternalTask:
        fields = self._forge_to_fields(forge_task)
        fields["project"] = {"key": self.ctx.external_project_key}
        fields["issuetype"] = {"name": self.issue_type}
        created = await self.client.create_issue(fields)
        external_id = str(created.get("id") or created.get("key") or "")
        return await self.fetch_external(external_id)

    async def update_external(self, external_id: str, forge_task: ForgeTask) -> ExternalTask:
        await self.client.update_issue(external_id, self._forge_to_fields(forge_task))
        await self._apply_transition(external_id, forge_task)
        return await self.fetch_external(external_id)

    async def _apply_transition(self, external_id: str, forge_task: ForgeTask) -> None:
        target_category = self.map_status(forge_task.status_category.value, Direction.OUT)
        transitions = await self.client.get_transitions(external_id)
        for tr in transitions:
            to = tr.get("to") or {}
            cat = (to.get("statusCategory") or {}).get("key")
            if cat == target_category:
                await self.client.do_transition(external_id, str(tr.get("id")))
                return
        # No matching transition: surfaced as an error by the engine (no silent drop).
        raise ProviderError(f"no jira transition to category {target_category!r} for {external_id}")

    async def list_external(
        self, *, cursor: str | None = None, limit: int = 50
    ) -> tuple[list[ExternalTask], str | None]:
        start_at = int(cursor) if cursor else 0
        jql = f"project = {self.ctx.external_project_key} ORDER BY created ASC"
        result = await self.client.search(jql, start_at=start_at, max_results=limit)
        issues = result.get("issues") or []
        tasks = [self._issue_to_external(i) for i in issues]
        total = int(result.get("total") or 0)
        next_start = start_at + len(issues)
        next_cursor = str(next_start) if next_start < total else None
        return tasks, next_cursor

    # ------------------------------------------------------------------ #
    # webhook + health                                                   #
    # ------------------------------------------------------------------ #

    def parse_webhook(self, body: bytes, headers: dict[str, str]) -> WebhookEvent:
        import json

        try:
            data = json.loads(body or b"{}")
        except ValueError as exc:
            raise ProviderError(f"invalid jira webhook body: {exc}") from exc
        delivery_id = synthesize_delivery_id(body)
        return parse_jira(data, delivery_id=delivery_id, signature_valid=True)

    def verify_webhook(self, body: bytes, headers: dict[str, str], secret: str) -> bool:
        lowered = {k.lower(): v for k, v in headers.items()}
        provided = lowered.get(WEBHOOK_SECRET_HEADER)
        return verify_jira(secret, provided)

    async def register_webhook(self, callback_url: str, secret: str) -> str:
        jql = f"project = {self.ctx.external_project_key}"
        payload = {
            "name": f"forge-pm-{self.ctx.connection_id}",
            "url": callback_url,
            "events": ["jira:issue_created", "jira:issue_updated", "jira:issue_deleted"],
            "filters": {"issue-related-events-section": jql},
        }
        result = await self.client.register_webhook(payload)
        webhook_id = result.get("id") or (result.get("self") or "").rsplit("/", 1)[-1]
        return str(webhook_id)

    async def unregister_webhook(self, external_webhook_id: str) -> None:
        await self.client.unregister_webhook(external_webhook_id)

    async def get_connection_health(self) -> HealthResult:
        start = time.perf_counter()
        try:
            me = await self.client.myself()
        except PMAuthError as exc:
            return HealthResult(status="error", provider=PMProvider.jira, error=str(exc))
        except ProviderError as exc:
            return HealthResult(status="error", provider=PMProvider.jira, error=str(exc))
        latency = (time.perf_counter() - start) * 1000
        return HealthResult(
            status="connected",
            provider=PMProvider.jira,
            latency_ms=latency,
            account=me.get("emailAddress") or me.get("displayName"),
            granted_scopes=list(self.ctx.config.get("granted_scopes", [])),
        )


__all__ = ["JiraAdapter"]
