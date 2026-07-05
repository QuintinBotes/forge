"""LinearAdapter — implements the async ``PMAdapter`` Protocol over ``LinearClient``."""

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
from forge_integrations.pm.linear import mapping
from forge_integrations.pm.linear.client import LinearClient
from forge_integrations.pm.linear.webhooks import parse_linear, verify_linear

SIGNATURE_HEADER = "linear-signature"


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


class LinearAdapter(BaseAdapter):
    provider = PMProvider.linear
    status_out_table = mapping.STATUS_OUT
    status_in_table = mapping.STATUS_IN
    priority_out_table = mapping.PRIORITY_OUT
    priority_in_table = mapping.PRIORITY_IN
    field_in_table: ClassVar[dict[str, str]] = {
        "title": "title",
        "description": "description",
    }

    def __init__(
        self,
        client: LinearClient,
        ctx: AdapterContext,
        *,
        tolerance_seconds: int = 60,
    ) -> None:
        super().__init__(
            status_map=ctx.status_map,
            priority_map=ctx.priority_map,
            field_map=ctx.field_map,
        )
        self.client = client
        self.ctx = ctx
        self.tolerance_seconds = tolerance_seconds

    # ------------------------------------------------------------------ #
    # parsing                                                            #
    # ------------------------------------------------------------------ #

    def _issue_to_external(self, issue: dict[str, Any]) -> ExternalTask:
        state = issue.get("state") or {}
        state_type = state.get("type") or ""
        try:
            status_category = StatusCategory(self.map_status(state_type, Direction.IN))
        except (MappingError, ValueError):
            status_category = None
        assignee = issue.get("assignee") or {}
        labels = [
            n.get("name") for n in ((issue.get("labels") or {}).get("nodes") or []) if n.get("name")
        ]
        priority = issue.get("priority")
        return ExternalTask(
            provider=PMProvider.linear,
            external_id=str(issue.get("id") or ""),
            external_key=issue.get("identifier") or "",
            url=issue.get("url") or "",
            title=issue.get("title") or "",
            description_md=issue.get("description"),
            status_name=state.get("name") or "",
            status_category=status_category,
            priority_token=str(priority) if priority is not None else None,
            assignee_external_id=assignee.get("id"),
            assignee_email=assignee.get("email"),
            labels=labels,
            external_updated_at=_parse_dt(issue.get("updatedAt")),
            raw={},
        )

    async def _state_id_for(self, forge_task: ForgeTask) -> str | None:
        target_type = self.map_status(forge_task.status_category.value, Direction.OUT)
        states = await self.client.workflow_states(self.ctx.external_project_id)
        for st in states:
            if st.get("type") == target_type:
                return str(st.get("id"))
        raise ProviderError(f"no linear workflow state of type {target_type!r} in team")

    def _forge_to_input(self, forge_task: ForgeTask) -> dict[str, Any]:
        priority = int(self.map_priority(forge_task.priority.value, Direction.OUT))
        payload: dict[str, Any] = {
            "title": forge_task.title,
            "description": mapping.markdown_passthrough(forge_task.description_md),
            "priority": priority,
        }
        return payload

    # ------------------------------------------------------------------ #
    # external I/O                                                        #
    # ------------------------------------------------------------------ #

    async def fetch_external(self, external_id: str) -> ExternalTask:
        return self._issue_to_external(await self.client.get_issue(external_id))

    async def create_external(self, forge_task: ForgeTask) -> ExternalTask:
        payload = self._forge_to_input(forge_task)
        payload["teamId"] = self.ctx.external_project_id
        state_id = await self._state_id_for(forge_task)
        if state_id:
            payload["stateId"] = state_id
        issue = await self.client.create_issue(payload)
        return self._issue_to_external(issue)

    async def update_external(self, external_id: str, forge_task: ForgeTask) -> ExternalTask:
        payload = self._forge_to_input(forge_task)
        state_id = await self._state_id_for(forge_task)
        if state_id:
            payload["stateId"] = state_id
        issue = await self.client.update_issue(external_id, payload)
        return self._issue_to_external(issue)

    async def list_external(
        self, *, cursor: str | None = None, limit: int = 50
    ) -> tuple[list[ExternalTask], str | None]:
        page = await self.client.list_team_issues(
            self.ctx.external_project_id, after=cursor, first=limit
        )
        nodes = page.get("nodes") or []
        tasks = [self._issue_to_external(n) for n in nodes]
        info = page.get("pageInfo") or {}
        next_cursor = info.get("endCursor") if info.get("hasNextPage") else None
        return tasks, next_cursor

    # ------------------------------------------------------------------ #
    # webhook + health                                                   #
    # ------------------------------------------------------------------ #

    def parse_webhook(self, body: bytes, headers: dict[str, str]) -> WebhookEvent:
        import hashlib
        import json

        try:
            data = json.loads(body or b"{}")
        except ValueError as exc:
            raise ProviderError(f"invalid linear webhook body: {exc}") from exc
        delivery_id = str(data.get("webhookId") or "") + ":" + hashlib.sha256(body).hexdigest()[:16]
        return parse_linear(data, delivery_id=delivery_id, signature_valid=True)

    def verify_webhook(self, body: bytes, headers: dict[str, str], secret: str) -> bool:
        import json

        lowered = {k.lower(): v for k, v in headers.items()}
        signature = lowered.get(SIGNATURE_HEADER)
        timestamp_ms: int | None = None
        try:
            data = json.loads(body or b"{}")
            ts = data.get("webhookTimestamp")
            timestamp_ms = int(ts) if ts is not None else None
        except (ValueError, TypeError):
            timestamp_ms = None
        return verify_linear(
            secret,
            body,
            signature,
            timestamp_ms=timestamp_ms,
            tolerance_seconds=self.tolerance_seconds,
        )

    async def register_webhook(self, callback_url: str, secret: str) -> str:
        payload = {
            "url": callback_url,
            "teamId": self.ctx.external_project_id,
            "secret": secret,
            "resourceTypes": ["Issue"],
        }
        result = await self.client.webhook_create(payload)
        return str(result.get("id") or "")

    async def unregister_webhook(self, external_webhook_id: str) -> None:
        await self.client.webhook_delete(external_webhook_id)

    async def get_connection_health(self) -> HealthResult:
        start = time.perf_counter()
        try:
            viewer = await self.client.viewer()
        except (PMAuthError, ProviderError) as exc:
            return HealthResult(status="error", provider=PMProvider.linear, error=str(exc))
        latency = (time.perf_counter() - start) * 1000
        return HealthResult(
            status="connected",
            provider=PMProvider.linear,
            latency_ms=latency,
            account=viewer.get("email") or viewer.get("name"),
            granted_scopes=list(self.ctx.config.get("granted_scopes", [])),
        )


__all__ = ["LinearAdapter"]
