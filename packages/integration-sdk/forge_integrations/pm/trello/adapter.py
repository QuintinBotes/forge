"""TrelloAdapter — implements the async ``PMAdapter`` Protocol over ``TrelloClient``.

Trello has no native "status" or "priority" field: status is the card's list
membership within the connected board (resolved by name lookup at write time,
exactly like Asana's section resolution — a missing target list raises
:class:`ProviderError`, status is never silently dropped), and priority is a
board label matched by name (tolerated when missing, matching Asana's
precedent for its supplementary custom field).
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
from forge_integrations.pm.trello import mapping
from forge_integrations.pm.trello.client import TrelloClient
from forge_integrations.pm.trello.webhooks import (
    parse_trello,
    synthesize_delivery_id,
    verify_trello,
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


class TrelloAdapter(BaseAdapter):
    provider = PMProvider.trello
    status_out_table = mapping.STATUS_OUT
    status_in_table = mapping.STATUS_IN
    priority_out_table = mapping.PRIORITY_OUT
    priority_in_table = mapping.PRIORITY_IN
    field_in_table: ClassVar[dict[str, str]] = {
        "name": "title",
        "desc": "description_md",
    }

    def __init__(self, client: TrelloClient, ctx: AdapterContext) -> None:
        super().__init__(
            status_map=ctx.status_map,
            priority_map=ctx.priority_map,
            field_map=ctx.field_map,
        )
        self.client = client
        self.ctx = ctx
        # Required for verify_webhook (Trello signs body + callback_url); the
        # connection layer persists the URL used at register_webhook time.
        self.webhook_callback_url = str(ctx.config.get("webhook_callback_url", ""))

    # ------------------------------------------------------------------ #
    # parsing                                                            #
    # ------------------------------------------------------------------ #

    def _priority_token(self, labels: list[dict[str, Any]]) -> str | None:
        priority_names = {v.strip().lower() for v in self.priority_out_table.values()}
        for label in labels:
            name = str(label.get("name") or "").strip()
            if name.lower() in priority_names:
                return name
        return None

    def _card_to_external(self, card: dict[str, Any]) -> ExternalTask:
        card_id = str(card.get("id") or "")
        list_obj = card.get("list") or {}
        list_name = list_obj.get("name") or ""
        status_category: StatusCategory | None = None
        if list_name:
            try:
                status_category = StatusCategory(self.map_status(list_name, Direction.IN))
            except (MappingError, ValueError):
                status_category = None
        labels = card.get("labels") or []
        label_names = [str(label.get("name")) for label in labels if label.get("name")]
        members = card.get("idMembers") or []
        return ExternalTask(
            provider=PMProvider.trello,
            external_id=card_id,
            external_key=card_id,
            url=card.get("shortUrl") or card.get("url") or card_id,
            title=card.get("name") or "",
            description_md=card.get("desc"),
            status_name=list_name,
            status_category=status_category,
            priority_token=self._priority_token(labels),
            assignee_external_id=str(members[0]) if members else None,
            assignee_email=None,
            labels=label_names,
            external_updated_at=_parse_dt(card.get("dateLastActivity")),
            raw={},
        )

    async def _list_id_for(self, forge_task: ForgeTask) -> str:
        target_name = self.map_status(forge_task.status_category.value, Direction.OUT)
        lists = await self.client.list_board_lists(self.ctx.external_project_id)
        for lst in lists:
            if str(lst.get("name") or "").strip().lower() == target_name.strip().lower():
                return str(lst.get("id"))
        raise ProviderError(
            f"no trello list named {target_name!r} on board {self.ctx.external_project_id!r}"
        )

    async def _priority_label_id_for(self, forge_task: ForgeTask) -> str | None:
        target_name = self.map_priority(forge_task.priority.value, Direction.OUT)
        labels = await self.client.list_board_labels(self.ctx.external_project_id)
        for label in labels:
            if str(label.get("name") or "").strip().lower() == target_name.strip().lower():
                return str(label.get("id"))
        return None

    # ------------------------------------------------------------------ #
    # external I/O                                                        #
    # ------------------------------------------------------------------ #

    async def fetch_external(self, external_id: str) -> ExternalTask:
        return self._card_to_external(await self.client.get_card(external_id))

    async def create_external(self, forge_task: ForgeTask) -> ExternalTask:
        list_id = await self._list_id_for(forge_task)
        created = await self.client.create_card(
            list_id, forge_task.title, forge_task.description_md or ""
        )
        card_id = str(created.get("id") or "")
        label_id = await self._priority_label_id_for(forge_task)
        if label_id:
            await self.client.update_card(card_id, {"idLabels": label_id})
        return await self.fetch_external(card_id)

    async def update_external(self, external_id: str, forge_task: ForgeTask) -> ExternalTask:
        list_id = await self._list_id_for(forge_task)
        fields: dict[str, Any] = {
            "name": forge_task.title,
            "desc": forge_task.description_md or "",
            "idList": list_id,
        }
        label_id = await self._priority_label_id_for(forge_task)
        if label_id:
            fields["idLabels"] = label_id
        await self.client.update_card(external_id, fields)
        return await self.fetch_external(external_id)

    async def list_external(
        self, *, cursor: str | None = None, limit: int = 50
    ) -> tuple[list[ExternalTask], str | None]:
        cards, next_cursor = await self.client.list_board_cards(
            self.ctx.external_project_id, before=cursor, limit=limit
        )
        return [self._card_to_external(c) for c in cards], next_cursor

    # ------------------------------------------------------------------ #
    # webhook + health                                                   #
    # ------------------------------------------------------------------ #

    def parse_webhook(self, body: bytes, headers: dict[str, str]) -> WebhookEvent:
        import json

        try:
            data = json.loads(body or b"{}")
        except ValueError as exc:
            raise ProviderError(f"invalid trello webhook body: {exc}") from exc
        delivery_id = synthesize_delivery_id(body)
        return parse_trello(data, delivery_id=delivery_id, signature_valid=True)

    def verify_webhook(self, body: bytes, headers: dict[str, str], secret: str) -> bool:
        lowered = {k.lower(): v for k, v in headers.items()}
        signature = lowered.get("x-trello-webhook")
        return verify_trello(secret, self.webhook_callback_url, body, signature)

    async def register_webhook(self, callback_url: str, secret: str) -> str:
        self.webhook_callback_url = callback_url
        result = await self.client.create_webhook(self.ctx.external_project_id, callback_url)
        return str(result.get("id") or "")

    async def unregister_webhook(self, external_webhook_id: str) -> None:
        await self.client.delete_webhook(external_webhook_id)

    async def get_connection_health(self) -> HealthResult:
        start = time.perf_counter()
        try:
            me = await self.client.me()
        except (PMAuthError, ProviderError) as exc:
            return HealthResult(status="error", provider=PMProvider.trello, error=str(exc))
        latency = (time.perf_counter() - start) * 1000
        return HealthResult(
            status="connected",
            provider=PMProvider.trello,
            latency_ms=latency,
            account=me.get("email") or me.get("username"),
            granted_scopes=list(self.ctx.config.get("granted_scopes", [])),
        )


__all__ = ["TrelloAdapter"]
