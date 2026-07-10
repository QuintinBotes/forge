"""GitHub Projects v2 GraphQL client over the offline-testable ``PMTransport``.

Tasks are modeled as Projects v2 *draft issues* (``addProjectV2DraftIssue``) so
sync never requires a full repository Issue to exist — a project-only board
works the same as one backed by real Issues. Auth is a GitHub App installation
token (see :mod:`forge_integrations.pm.github_projects.auth`), sent as
``Authorization: Bearer <token>`` and never logged.
"""

from __future__ import annotations

from typing import Any

from forge_contracts.pm import HttpResponse, PMTransport
from forge_integrations.pm.errors import PMAuthError, ProviderError

ENDPOINT = "https://api.github.com/graphql"

_ITEM_FIELDS = """
  id
  updatedAt
  content {
    ... on DraftIssue { id title body }
    ... on Issue { id title body url }
    ... on PullRequest { id title body url }
  }
  fieldValues(first: 20) {
    nodes {
      ... on ProjectV2ItemFieldSingleSelectValue {
        name
        field { ... on ProjectV2SingleSelectField { id name } }
      }
    }
  }
"""


class GitHubProjectsClient:
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
            raise PMAuthError(f"github projects auth failed ({resp.status_code})")
        if resp.status_code >= 400:
            raise ProviderError(
                f"github projects http {resp.status_code}", status_code=resp.status_code
            )
        body = resp.json_body or {}
        if isinstance(body, dict) and body.get("errors"):
            raise ProviderError(f"github projects graphql errors: {body['errors']}")
        return dict(body.get("data", {})) if isinstance(body, dict) else {}

    async def get_item(self, item_node_id: str) -> dict[str, Any]:
        query = f"""
        query Item($id: ID!) {{
          node(id: $id) {{ ... on ProjectV2Item {{ {_ITEM_FIELDS} }} }}
        }}
        """
        data = await self._gql(query, {"id": item_node_id})
        return dict(data.get("node") or {})

    async def add_draft_issue(self, project_id: str, title: str, body: str) -> dict[str, Any]:
        mutation = f"""
        mutation AddDraftIssue($input: AddProjectV2DraftIssueInput!) {{
          addProjectV2DraftIssue(input: $input) {{
            projectItem {{ {_ITEM_FIELDS} }}
          }}
        }}
        """
        data = await self._gql(
            mutation, {"input": {"projectId": project_id, "title": title, "body": body}}
        )
        result = data.get("addProjectV2DraftIssue") or {}
        return dict(result.get("projectItem") or {})

    async def update_draft_issue(self, draft_issue_id: str, title: str, body: str) -> None:
        mutation = """
        mutation UpdateDraftIssue($input: UpdateProjectV2DraftIssueInput!) {
          updateProjectV2DraftIssue(input: $input) { draftIssue { id } }
        }
        """
        await self._gql(
            mutation, {"input": {"draftIssueId": draft_issue_id, "title": title, "body": body}}
        )

    async def update_single_select_field(
        self, project_id: str, item_id: str, field_id: str, option_id: str
    ) -> None:
        mutation = """
        mutation UpdateItemFieldValue($input: UpdateProjectV2ItemFieldValueInput!) {
          updateProjectV2ItemFieldValue(input: $input) { projectV2Item { id } }
        }
        """
        await self._gql(
            mutation,
            {
                "input": {
                    "projectId": project_id,
                    "itemId": item_id,
                    "fieldId": field_id,
                    "value": {"singleSelectOptionId": option_id},
                }
            },
        )

    async def list_project_fields(self, project_id: str) -> list[dict[str, Any]]:
        query = """
        query ProjectFields($id: ID!) {
          node(id: $id) {
            ... on ProjectV2 {
              fields(first: 50) {
                nodes { ... on ProjectV2SingleSelectField { id name options { id name } } }
              }
            }
          }
        }
        """
        data = await self._gql(query, {"id": project_id})
        node = data.get("node") or {}
        return list((node.get("fields") or {}).get("nodes") or [])

    async def list_items(
        self, project_id: str, *, after: str | None = None, first: int = 50
    ) -> tuple[list[dict[str, Any]], str | None]:
        query = f"""
        query ProjectItems($id: ID!, $first: Int!, $after: String) {{
          node(id: $id) {{
            ... on ProjectV2 {{
              items(first: $first, after: $after) {{
                nodes {{ {_ITEM_FIELDS} }}
                pageInfo {{ hasNextPage endCursor }}
              }}
            }}
          }}
        }}
        """
        data = await self._gql(query, {"id": project_id, "first": first, "after": after})
        node = data.get("node") or {}
        items = node.get("items") or {}
        nodes = list(items.get("nodes") or [])
        info = items.get("pageInfo") or {}
        next_cursor = info.get("endCursor") if info.get("hasNextPage") else None
        return nodes, next_cursor

    async def viewer(self) -> dict[str, Any]:
        query = "query Viewer { viewer { login email } }"
        data = await self._gql(query)
        return dict(data.get("viewer") or {})


__all__ = ["ENDPOINT", "GitHubProjectsClient"]
