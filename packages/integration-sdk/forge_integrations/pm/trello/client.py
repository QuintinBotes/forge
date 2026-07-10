"""Trello REST v1 client over the offline-testable ``PMTransport``.

Mirrors :mod:`forge_integrations.pm.asana.client`: all methods are async,
delegate HTTP to the injected transport (so tests replay
:class:`FixturePMTransport` records with **no** sockets). Trello supports both
key/token query-string auth and an ``Authorization: OAuth ...`` header; this
client uses the latter (like every other F40 client) so ``build_adapter``'s
uniform ``auth_header`` parameter works unchanged across providers.

Cards are fetched with ``list=true`` so the enclosing list (Trello's board
column — "lists -> status categories" per spec) comes back embedded, avoiding
a second round-trip on every read.
"""

from __future__ import annotations

from typing import Any

from forge_contracts.pm import HttpResponse, PMTransport
from forge_integrations.pm.errors import ExternalNotFound, PMAuthError, ProviderError

API = "https://api.trello.com/1"

CARD_FIELDS = "id,name,desc,idList,idMembers,url,shortUrl,dateLastActivity"


class TrelloClient:
    def __init__(
        self,
        transport: PMTransport,
        *,
        base_url: str = API,
        auth_header: str | None = None,
    ) -> None:
        self._t = transport
        self._base = base_url.rstrip("/")
        self._headers: dict[str, str] = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if auth_header:
            self._headers["Authorization"] = auth_header

    def _url(self, path: str) -> str:
        return f"{self._base}{path}"

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        params: dict | None = None,
    ) -> Any:
        resp: HttpResponse = await self._t.request(
            method, self._url(path), headers=self._headers, json=json, params=params
        )
        if resp.status_code in (401, 403):
            raise PMAuthError(f"trello auth failed ({resp.status_code})")
        if resp.status_code == 404:
            raise ExternalNotFound(f"trello resource not found: {path}")
        if resp.status_code >= 400:
            raise ProviderError(
                f"trello error {resp.status_code} on {path}", status_code=resp.status_code
            )
        return resp.json_body

    async def get_card(self, card_id: str) -> dict[str, Any]:
        body = await self._request(
            "GET",
            f"/cards/{card_id}",
            params={"fields": CARD_FIELDS, "list": "true", "labels": "true"},
        )
        return dict(body or {})

    async def create_card(self, list_id: str, name: str, desc: str) -> dict[str, Any]:
        body = await self._request(
            "POST", "/cards", params={"idList": list_id, "name": name, "desc": desc}
        )
        return dict(body or {})

    async def update_card(self, card_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        body = await self._request("PUT", f"/cards/{card_id}", params=fields)
        return dict(body or {})

    async def list_board_lists(self, board_id: str) -> list[dict[str, Any]]:
        body = await self._request("GET", f"/boards/{board_id}/lists")
        return list(body or [])

    async def list_board_labels(self, board_id: str) -> list[dict[str, Any]]:
        body = await self._request("GET", f"/boards/{board_id}/labels")
        return list(body or [])

    async def list_board_cards(
        self, board_id: str, *, before: str | None = None, limit: int = 50
    ) -> tuple[list[dict[str, Any]], str | None]:
        params: dict[str, Any] = {
            "fields": CARD_FIELDS,
            "list": "true",
            "labels": "true",
            "limit": limit,
        }
        if before:
            params["before"] = before
        body = await self._request("GET", f"/boards/{board_id}/cards", params=params)
        cards = list(body or [])
        next_cursor = cards[-1].get("id") if len(cards) == limit else None
        return cards, next_cursor

    async def me(self) -> dict[str, Any]:
        body = await self._request("GET", "/members/me")
        return dict(body or {})

    async def create_webhook(self, model_id: str, callback_url: str) -> dict[str, Any]:
        body = await self._request(
            "POST", "/webhooks", params={"idModel": model_id, "callbackURL": callback_url}
        )
        return dict(body or {})

    async def delete_webhook(self, webhook_id: str) -> None:
        await self._request("DELETE", f"/webhooks/{webhook_id}")


__all__ = ["API", "CARD_FIELDS", "TrelloClient"]
