"""Monday.com GraphQL client over the offline-testable ``PMTransport``.

Mirrors :mod:`forge_integrations.pm.linear.client`: a single GraphQL endpoint,
requests keyed by operation name for the fixture transport, and a personal API
token / OAuth bearer sent as the raw ``Authorization`` header (monday.com does
not use a ``Bearer`` prefix). No sockets are opened by importing this module.
"""

from __future__ import annotations

import json
from typing import Any

from forge_contracts.pm import HttpResponse, PMTransport
from forge_integrations.pm.errors import PMAuthError, ProviderError

ENDPOINT = "https://api.monday.com/v2"

ITEM_FIELDS = "id name url updated_at group { id title } column_values { id text value type }"


class MondayClient:
    def __init__(
        self,
        transport: PMTransport,
        *,
        endpoint: str = ENDPOINT,
        auth_header: str | None = None,
    ) -> None:
        self._t = transport
        self._endpoint = endpoint
        self._headers: dict[str, str] = {"Content-Type": "application/json"}
        if auth_header:
            self._headers["Authorization"] = auth_header

    async def _gql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        resp: HttpResponse = await self._t.request(
            "POST",
            self._endpoint,
            headers=self._headers,
            json={"query": query, "variables": variables or {}},
        )
        if resp.status_code in (401, 403):
            raise PMAuthError(f"monday auth failed ({resp.status_code})")
        if resp.status_code >= 400:
            raise ProviderError(f"monday http {resp.status_code}", status_code=resp.status_code)
        body = resp.json_body or {}
        if isinstance(body, dict) and body.get("errors"):
            raise ProviderError(f"monday graphql errors: {body['errors']}")
        return dict(body.get("data", {})) if isinstance(body, dict) else {}

    async def get_item(self, item_id: str) -> dict[str, Any]:
        query = f"""
        query Item($ids: [ID!]) {{
          items(ids: $ids) {{ {ITEM_FIELDS} }}
        }}
        """
        data = await self._gql(query, {"ids": [item_id]})
        items = data.get("items") or []
        return dict(items[0]) if items else {}

    async def create_item(
        self, board_id: str, group_id: str | None, item_name: str, column_values: dict[str, Any]
    ) -> dict[str, Any]:
        mutation = f"""
        mutation CreateItem(
          $boardId: ID!, $groupId: String, $itemName: String!, $columnValues: JSON
        ) {{
          create_item(
            board_id: $boardId, group_id: $groupId, item_name: $itemName,
            column_values: $columnValues
          ) {{ {ITEM_FIELDS} }}
        }}
        """
        data = await self._gql(
            mutation,
            {
                "boardId": board_id,
                "groupId": group_id,
                "itemName": item_name,
                "columnValues": json.dumps(column_values),
            },
        )
        return dict(data.get("create_item") or {})

    async def change_multiple_column_values(
        self, board_id: str, item_id: str, column_values: dict[str, Any]
    ) -> dict[str, Any]:
        mutation = f"""
        mutation ChangeMultipleColumnValues(
          $boardId: ID!, $itemId: ID!, $columnValues: JSON!
        ) {{
          change_multiple_column_values(
            board_id: $boardId, item_id: $itemId, column_values: $columnValues
          ) {{ {ITEM_FIELDS} }}
        }}
        """
        data = await self._gql(
            mutation,
            {"boardId": board_id, "itemId": item_id, "columnValues": json.dumps(column_values)},
        )
        return dict(data.get("change_multiple_column_values") or {})

    async def list_board_groups(self, board_id: str) -> list[dict[str, Any]]:
        query = """
        query BoardGroups($ids: [ID!]) {
          boards(ids: $ids) { groups { id title } }
        }
        """
        data = await self._gql(query, {"ids": [board_id]})
        boards = data.get("boards") or []
        return list((boards[0] or {}).get("groups") or []) if boards else []

    async def list_board_items(
        self, board_id: str, *, cursor: str | None = None, limit: int = 50
    ) -> tuple[list[dict[str, Any]], str | None]:
        query = f"""
        query BoardItems($ids: [ID!], $limit: Int!, $cursor: String) {{
          boards(ids: $ids) {{
            items_page(limit: $limit, cursor: $cursor) {{
              cursor
              items {{ {ITEM_FIELDS} }}
            }}
          }}
        }}
        """
        data = await self._gql(query, {"ids": [board_id], "limit": limit, "cursor": cursor})
        boards = data.get("boards") or []
        page = (boards[0] or {}).get("items_page") or {} if boards else {}
        items = list(page.get("items") or [])
        next_cursor = page.get("cursor")
        return items, next_cursor

    async def me(self) -> dict[str, Any]:
        query = "query Me { me { id name email } }"
        data = await self._gql(query)
        return dict(data.get("me") or {})

    async def create_webhook(
        self, board_id: str, url: str, event: str = "change_status_column"
    ) -> dict[str, Any]:
        mutation = """
        mutation CreateWebhook($boardId: ID!, $url: String!, $event: WebhookEventType!) {
          create_webhook(board_id: $boardId, url: $url, event: $event) { id board_id }
        }
        """
        data = await self._gql(mutation, {"boardId": board_id, "url": url, "event": event})
        return dict(data.get("create_webhook") or {})

    async def delete_webhook(self, webhook_id: str) -> None:
        mutation = """
        mutation DeleteWebhook($id: ID!) {
          delete_webhook(id: $id) { id }
        }
        """
        await self._gql(mutation, {"id": webhook_id})


__all__ = ["ENDPOINT", "ITEM_FIELDS", "MondayClient"]
