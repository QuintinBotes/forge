"""GenericAdapter — a config-driven ``PMAdapter`` for boards with no native adapter.

Unlike every other F40 provider (whose endpoints/field-shapes are hardcoded in
a ``client.py``), :class:`GenericAdapter` is driven entirely by a
:class:`~forge_contracts.pm.GenericAdapterConfig` supplied on
``AdapterContext.config["generic_config"]`` — a base URL, endpoint path
templates, a forge-field -> dotted-JSON-path field map, and a status/priority
-category map. No code change is required to bring a BYO board: a user fills
in the config and gets the same ``PMAdapter`` Protocol surface (mapping,
external I/O, webhook, health) every native adapter provides.

Path templates may reference ``{external_id}``, ``{project_id}``,
``{webhook_id}``, ``{cursor}``, ``{limit}`` — placeholders the given call
doesn't use are simply left blank rather than raising, since a BYO board's
endpoint shape is unknown ahead of time.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from typing import Any

from forge_contracts.enums import Direction
from forge_contracts.pm import (
    AdapterContext,
    ExternalTask,
    ForgeTask,
    GenericAdapterConfig,
    HealthResult,
    HttpResponse,
    PMProvider,
    PMTransport,
    StatusCategory,
    WebhookEvent,
)
from forge_integrations.pm.base import BaseAdapter
from forge_integrations.pm.errors import ExternalNotFound, MappingError, PMAuthError, ProviderError
from forge_integrations.pm.generic.paths import get_path, set_path
from forge_integrations.pm.generic.webhooks import (
    parse_generic,
    synthesize_delivery_id,
    verify_generic,
)


class _SafeFormatDict(dict):
    def __missing__(self, key: str) -> str:  # unused placeholders resolve to ""
        return ""


def _format(template: str, **kwargs: Any) -> str:
    return template.format_map(_SafeFormatDict(**kwargs))


def _parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if not value:
        return datetime.now(UTC)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(UTC)


class GenericAdapter(BaseAdapter):
    provider = PMProvider.generic

    def __init__(
        self,
        transport: PMTransport,
        ctx: AdapterContext,
        config: GenericAdapterConfig,
        *,
        auth_header: str | None = None,
    ) -> None:
        # config.status_map/priority_map are the BYO board's defaults; a
        # per-connection ctx.status_map/priority_map override still wins,
        # exactly like every native adapter's class-table + ctx-override split.
        super().__init__(
            status_map={**config.status_map, **(ctx.status_map or {})},
            priority_map={**config.priority_map, **(ctx.priority_map or {})},
            field_map=ctx.field_map,
        )
        self.transport = transport
        self.ctx = ctx
        self.config = config
        self._headers: dict[str, str] = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if auth_header:
            self._headers[config.auth_header_name] = auth_header

    # ------------------------------------------------------------------ #
    # transport                                                           #
    # ------------------------------------------------------------------ #

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        params: dict | None = None,
    ) -> HttpResponse:
        url = f"{self.config.base_url}{path}"
        resp = await self.transport.request(
            method, url, headers=self._headers, json=json, params=params
        )
        if resp.status_code in (401, 403):
            raise PMAuthError(f"generic auth failed ({resp.status_code})")
        if resp.status_code == 404:
            raise ExternalNotFound(f"generic resource not found: {path}")
        if resp.status_code >= 400:
            raise ProviderError(
                f"generic error {resp.status_code} on {path}", status_code=resp.status_code
            )
        return resp

    # ------------------------------------------------------------------ #
    # field mapping (config-driven)                                       #
    # ------------------------------------------------------------------ #

    def _field(self, item: dict, forge_field: str) -> Any:
        path = self.config.fields.get(forge_field)
        return get_path(item, path) if path else None

    def _item_to_external(self, item: dict) -> ExternalTask:
        external_id = str(get_path(item, self.config.item_id_path) or "")
        external_key = str(get_path(item, self.config.item_key_path) or external_id)
        url = get_path(item, self.config.item_url_path)
        raw_status = self._field(item, "status")
        status_category: StatusCategory | None = None
        if raw_status is not None:
            try:
                status_category = StatusCategory(self.map_status(str(raw_status), Direction.IN))
            except (MappingError, ValueError):
                status_category = None
        raw_labels = self._field(item, "labels")
        labels: list[str] = []
        if isinstance(raw_labels, list):
            for entry in raw_labels:
                if isinstance(entry, dict):
                    name = entry.get("name")
                    if name:
                        labels.append(str(name))
                elif entry:
                    labels.append(str(entry))
        return ExternalTask(
            provider=PMProvider.generic,
            external_id=external_id,
            external_key=external_key,
            url=str(url) if url else external_id,
            title=str(self._field(item, "title") or ""),
            description_md=self._field(item, "description_md"),
            status_name=str(raw_status) if raw_status is not None else "",
            status_category=status_category,
            priority_token=self._field(item, "priority_token"),
            assignee_external_id=None,
            assignee_email=self._field(item, "assignee_email"),
            labels=labels,
            external_updated_at=_parse_dt(self._field(item, "external_updated_at")),
            raw={},
        )

    def _build_body(self, forge_task: ForgeTask, *, include_status: bool = True) -> dict:
        body: dict = {}
        for forge_field, path in self.config.fields.items():
            if forge_field == "title":
                set_path(body, path, forge_task.title)
            elif forge_field == "status":
                # When a dedicated transition endpoint is configured, status is
                # applied via that call instead of folded into this body (the
                # Jira-shaped "workflow transition" case).
                if include_status:
                    set_path(
                        body,
                        path,
                        self.map_status(forge_task.status_category.value, Direction.OUT),
                    )
            elif forge_field == "description_md":
                set_path(body, path, forge_task.description_md or "")
            elif forge_field == "priority_token":
                set_path(body, path, self.map_priority(forge_task.priority.value, Direction.OUT))
            elif forge_field == "assignee_email" and forge_task.assignee_email:
                set_path(body, path, forge_task.assignee_email)
            elif forge_field == "labels":
                set_path(body, path, list(forge_task.label_names))
            # external_updated_at is provider-set; never written.
        return body

    # ------------------------------------------------------------------ #
    # external I/O                                                        #
    # ------------------------------------------------------------------ #

    async def fetch_external(self, external_id: str) -> ExternalTask:
        path = _format(
            self.config.endpoints.get,
            external_id=external_id,
            project_id=self.ctx.external_project_id,
        )
        resp = await self._request("GET", path)
        item = resp.json_body if isinstance(resp.json_body, dict) else {}
        return self._item_to_external(item)

    async def create_external(self, forge_task: ForgeTask) -> ExternalTask:
        path = _format(self.config.endpoints.create, project_id=self.ctx.external_project_id)
        resp = await self._request("POST", path, json=self._build_body(forge_task))
        item = resp.json_body if isinstance(resp.json_body, dict) else {}
        return self._item_to_external(item)

    async def update_external(self, external_id: str, forge_task: ForgeTask) -> ExternalTask:
        has_transition = bool(self.config.endpoints.transition)
        path = _format(
            self.config.endpoints.update,
            external_id=external_id,
            project_id=self.ctx.external_project_id,
        )
        resp = await self._request(
            "PUT", path, json=self._build_body(forge_task, include_status=not has_transition)
        )
        if has_transition:
            await self._apply_transition(external_id, forge_task)
            get_path_template = _format(
                self.config.endpoints.get,
                external_id=external_id,
                project_id=self.ctx.external_project_id,
            )
            resp = await self._request("GET", get_path_template)
        item = resp.json_body if isinstance(resp.json_body, dict) else {}
        return self._item_to_external(item)

    async def _apply_transition(self, external_id: str, forge_task: ForgeTask) -> None:
        """POST the mapped status to the dedicated ``transition`` endpoint.

        Mirrors the Jira adapter's separate transition call: some BYO boards
        validate status changes through a workflow-specific endpoint rather
        than accepting an arbitrary status value on the plain update body.
        The transition body carries just the status, addressed by the same
        dotted path configured for the ``status`` field.
        """
        assert self.config.endpoints.transition  # guarded by the has_transition check above
        path = _format(
            self.config.endpoints.transition,
            external_id=external_id,
            project_id=self.ctx.external_project_id,
        )
        status_path = self.config.fields.get("status", "status")
        body: dict = {}
        mapped = self.map_status(forge_task.status_category.value, Direction.OUT)
        set_path(body, status_path, mapped)
        await self._request("POST", path, json=body)

    async def list_external(
        self, *, cursor: str | None = None, limit: int = 50
    ) -> tuple[list[ExternalTask], str | None]:
        path = _format(
            self.config.endpoints.list,
            project_id=self.ctx.external_project_id,
            cursor=cursor or "",
            limit=limit,
        )
        params = {"limit": limit, **({"cursor": cursor} if cursor else {})}
        resp = await self._request("GET", path, params=params)
        body = resp.json_body
        items = get_path(body, self.config.list_items_path) if self.config.list_items_path else body
        items = items if isinstance(items, list) else []
        return [self._item_to_external(i) for i in items], None

    # ------------------------------------------------------------------ #
    # webhook + health                                                   #
    # ------------------------------------------------------------------ #

    def parse_webhook(self, body: bytes, headers: dict[str, str]) -> WebhookEvent:
        try:
            data = json.loads(body or b"{}")
        except ValueError as exc:
            raise ProviderError(f"invalid generic webhook body: {exc}") from exc
        lowered = {k.lower(): v for k, v in headers.items()}
        delivery_id = None
        if self.config.webhook.delivery_id_header:
            delivery_id = lowered.get(self.config.webhook.delivery_id_header.lower())
        delivery_id = delivery_id or synthesize_delivery_id(body)
        return parse_generic(
            self.config.webhook, data, delivery_id=delivery_id, signature_valid=True
        )

    def verify_webhook(self, body: bytes, headers: dict[str, str], secret: str) -> bool:
        return verify_generic(self.config.webhook, secret, body, headers)

    async def register_webhook(self, callback_url: str, secret: str) -> str:
        if not self.config.endpoints.register_webhook:
            raise ProviderError("generic connector config has no endpoints.register_webhook")
        path = _format(
            self.config.endpoints.register_webhook, project_id=self.ctx.external_project_id
        )
        resp = await self._request("POST", path, json={"url": callback_url, "secret": secret})
        body = resp.json_body if isinstance(resp.json_body, dict) else {}
        webhook_id = get_path(body, "id")
        return str(webhook_id) if webhook_id is not None else ""

    async def unregister_webhook(self, external_webhook_id: str) -> None:
        if not self.config.endpoints.unregister_webhook:
            return
        path = _format(
            self.config.endpoints.unregister_webhook,
            webhook_id=external_webhook_id,
            project_id=self.ctx.external_project_id,
        )
        await self._request("DELETE", path)

    async def get_connection_health(self) -> HealthResult:
        start = time.perf_counter()
        try:
            if self.config.endpoints.me:
                path = _format(self.config.endpoints.me, project_id=self.ctx.external_project_id)
                await self._request("GET", path)
            else:
                path = _format(self.config.endpoints.list, project_id=self.ctx.external_project_id)
                await self._request("GET", path, params={"limit": 1})
        except (PMAuthError, ProviderError) as exc:
            return HealthResult(status="error", provider=PMProvider.generic, error=str(exc))
        latency = (time.perf_counter() - start) * 1000
        return HealthResult(
            status="connected",
            provider=PMProvider.generic,
            latency_ms=latency,
            account=None,
            granted_scopes=list(self.ctx.config.get("granted_scopes", [])),
        )


__all__ = ["GenericAdapter"]
