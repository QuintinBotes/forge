"""Linear GraphQL client over the offline-testable ``PMTransport``.

All operations POST to the single GraphQL endpoint; the fixture transport keys
responses by GraphQL operation name. Auth is the ``Authorization`` header
(personal API key or OAuth bearer) and is never logged.
"""

from __future__ import annotations

from typing import Any

from forge_contracts.pm import HttpResponse, PMTransport
from forge_integrations.pm.errors import PMAuthError, ProviderError

ENDPOINT = "https://api.linear.app/graphql"


class LinearClient:
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
            raise PMAuthError(f"linear auth failed ({resp.status_code})")
        if resp.status_code >= 400:
            raise ProviderError(f"linear http {resp.status_code}", status_code=resp.status_code)
        body = resp.json_body or {}
        if isinstance(body, dict) and body.get("errors"):
            raise ProviderError(f"linear graphql errors: {body['errors']}")
        return dict(body.get("data", {})) if isinstance(body, dict) else {}

    async def get_issue(self, issue_id: str) -> dict[str, Any]:
        query = """
        query Issue($id: String!) {
          issue(id: $id) {
            id identifier url title description priority
            updatedAt
            state { id name type }
            assignee { id email }
            labels { nodes { name } }
          }
        }
        """
        data = await self._gql(query, {"id": issue_id})
        return dict(data.get("issue") or {})

    async def create_issue(self, input_payload: dict[str, Any]) -> dict[str, Any]:
        mutation = """
        mutation IssueCreate($input: IssueCreateInput!) {
          issueCreate(input: $input) {
            success
            issue {
              id identifier url title description priority updatedAt
              state { id name type } assignee { id email }
              labels { nodes { name } }
            }
          }
        }
        """
        data = await self._gql(mutation, {"input": input_payload})
        result = data.get("issueCreate") or {}
        return dict(result.get("issue") or {})

    async def update_issue(self, issue_id: str, input_payload: dict[str, Any]) -> dict[str, Any]:
        mutation = """
        mutation IssueUpdate($id: String!, $input: IssueUpdateInput!) {
          issueUpdate(id: $id, input: $input) {
            success
            issue {
              id identifier url title description priority updatedAt
              state { id name type } assignee { id email }
              labels { nodes { name } }
            }
          }
        }
        """
        data = await self._gql(mutation, {"id": issue_id, "input": input_payload})
        result = data.get("issueUpdate") or {}
        return dict(result.get("issue") or {})

    async def list_team_issues(
        self, team_id: str, *, after: str | None = None, first: int = 50
    ) -> dict[str, Any]:
        query = """
        query TeamIssues($teamId: String!, $after: String, $first: Int!) {
          team(id: $teamId) {
            issues(after: $after, first: $first) {
              nodes {
                id identifier url title description priority updatedAt
                state { id name type } assignee { id email }
                labels { nodes { name } }
              }
              pageInfo { hasNextPage endCursor }
            }
          }
        }
        """
        data = await self._gql(query, {"teamId": team_id, "after": after, "first": first})
        return dict((data.get("team") or {}).get("issues") or {})

    async def workflow_states(self, team_id: str) -> list[dict[str, Any]]:
        query = """
        query States($teamId: String!) {
          team(id: $teamId) { states { nodes { id name type } } }
        }
        """
        data = await self._gql(query, {"teamId": team_id})
        return list(((data.get("team") or {}).get("states") or {}).get("nodes", []))

    async def viewer(self) -> dict[str, Any]:
        query = "query Viewer { viewer { id name email } }"
        data = await self._gql(query)
        return dict(data.get("viewer") or {})

    async def webhook_create(self, input_payload: dict[str, Any]) -> dict[str, Any]:
        mutation = """
        mutation WebhookCreate($input: WebhookCreateInput!) {
          webhookCreate(input: $input) { success webhook { id } }
        }
        """
        data = await self._gql(mutation, {"input": input_payload})
        return dict((data.get("webhookCreate") or {}).get("webhook") or {})

    async def webhook_delete(self, webhook_id: str) -> None:
        mutation = """
        mutation WebhookDelete($id: String!) {
          webhookDelete(id: $id) { success }
        }
        """
        await self._gql(mutation, {"id": webhook_id})


__all__ = ["ENDPOINT", "LinearClient"]
