"""Shared fixtures for the PM-adapter test-suite.

All provider responses are recorded :class:`HttpResponse` objects replayed by
:class:`FixturePMTransport` — zero sockets are opened (AC23). Fixtures are loaded
from ``fixtures/<provider>/*.json`` (recorded provider payloads) so the same data
can be re-captured from live APIs during the post-merge verification phase.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from forge_contracts.pm import AdapterContext, HttpResponse, PMProvider
from forge_integrations.pm import FixturePMTransport
from forge_integrations.pm.asana.adapter import AsanaAdapter
from forge_integrations.pm.asana.client import AsanaClient
from forge_integrations.pm.github_projects.adapter import GitHubProjectsAdapter
from forge_integrations.pm.github_projects.client import GitHubProjectsClient
from forge_integrations.pm.jira.adapter import JiraAdapter
from forge_integrations.pm.jira.client import JiraClient
from forge_integrations.pm.linear.adapter import LinearAdapter
from forge_integrations.pm.linear.client import LinearClient
from forge_integrations.pm.monday.adapter import MondayAdapter
from forge_integrations.pm.monday.client import MondayClient

FIXTURES = Path(__file__).parent / "fixtures"

JIRA_BASE = "https://acme.atlassian.net"
JIRA_API = "/rest/api/3"
ASANA_API = "/api/1.0"


def load_json(rel: str) -> Any:
    path = FIXTURES / rel
    return json.loads(path.read_text())


def ok(body: Any, headers: dict[str, str] | None = None) -> HttpResponse:
    return HttpResponse(status_code=200, json_body=body, headers=headers or {})


@pytest.fixture
def jira_ctx() -> AdapterContext:
    return AdapterContext(
        connection_id=uuid4(),
        workspace_id=uuid4(),
        provider=PMProvider.jira,
        external_project_key="ENG",
        external_project_id="10000",
        external_base_url=JIRA_BASE,
        config={"granted_scopes": ["read:jira-work", "write:jira-work"]},
    )


@pytest.fixture
def linear_ctx() -> AdapterContext:
    return AdapterContext(
        connection_id=uuid4(),
        workspace_id=uuid4(),
        provider=PMProvider.linear,
        external_project_key="ENG",
        external_project_id="team-1",
        config={"granted_scopes": ["read", "write"]},
    )


@pytest.fixture
def jira_records() -> dict[tuple[str, str], Any]:
    issue = load_json("jira/get_issue.json")
    return {
        ("GET", f"{JIRA_API}/issue/10001"): ok(issue),
        ("POST", f"{JIRA_API}/issue"): ok(load_json("jira/create_issue.json")),
        ("PUT", f"{JIRA_API}/issue/10001"): ok({}),
        ("GET", f"{JIRA_API}/issue/10001/transitions"): ok(load_json("jira/transitions.json")),
        ("POST", f"{JIRA_API}/issue/10001/transitions"): ok({}),
        ("GET", f"{JIRA_API}/search"): ok(load_json("jira/search.json")),
        ("GET", f"{JIRA_API}/myself"): ok(load_json("jira/myself.json")),
        ("POST", "/rest/webhooks/1.0/webhook"): ok(load_json("jira/webhook_register.json")),
        ("DELETE", "/rest/webhooks/1.0/webhook/55"): ok({}),
    }


@pytest.fixture
def linear_records() -> dict[tuple[str, str], Any]:
    return {
        ("POST", "Issue"): ok(load_json("linear/issue_query.json")),
        ("POST", "IssueCreate"): ok(load_json("linear/issue_create.json")),
        ("POST", "IssueUpdate"): ok(load_json("linear/issue_update.json")),
        ("POST", "TeamIssues"): ok(load_json("linear/team_issues.json")),
        ("POST", "States"): ok(load_json("linear/workflow_states.json")),
        ("POST", "Viewer"): ok(load_json("linear/viewer.json")),
        ("POST", "WebhookCreate"): ok(load_json("linear/webhook_create.json")),
        ("POST", "WebhookDelete"): ok({"data": {"webhookDelete": {"success": True}}}),
    }


@pytest.fixture
def jira_transport(jira_records: dict[tuple[str, str], Any]) -> FixturePMTransport:
    return FixturePMTransport(jira_records)


@pytest.fixture
def linear_transport(linear_records: dict[tuple[str, str], Any]) -> FixturePMTransport:
    return FixturePMTransport(linear_records)


@pytest.fixture
def jira_adapter(jira_transport: FixturePMTransport, jira_ctx: AdapterContext) -> JiraAdapter:
    client = JiraClient(jira_transport, base_url=JIRA_BASE, auth_header="Basic xxx")
    return JiraAdapter(client, jira_ctx)


@pytest.fixture
def linear_adapter(
    linear_transport: FixturePMTransport, linear_ctx: AdapterContext
) -> LinearAdapter:
    client = LinearClient(linear_transport, auth_header="lin_api_xxx")
    return LinearAdapter(client, linear_ctx)


# --- Asana -------------------------------------------------------------- #


@pytest.fixture
def asana_ctx() -> AdapterContext:
    return AdapterContext(
        connection_id=uuid4(),
        workspace_id=uuid4(),
        provider=PMProvider.asana,
        external_project_key="999",
        external_project_id="999",
        config={"granted_scopes": ["default"]},
    )


@pytest.fixture
def asana_records() -> dict[tuple[str, str], Any]:
    return {
        ("GET", f"{ASANA_API}/tasks/10001"): ok(load_json("asana/get_task.json")),
        ("POST", f"{ASANA_API}/tasks"): ok(load_json("asana/create_task.json")),
        ("PUT", f"{ASANA_API}/tasks/10001"): ok(load_json("asana/update_task.json")),
        ("GET", f"{ASANA_API}/projects/999/sections"): ok(load_json("asana/list_sections.json")),
        ("GET", f"{ASANA_API}/projects/999/custom_field_settings"): ok(
            load_json("asana/list_custom_field_settings.json")
        ),
        ("POST", f"{ASANA_API}/sections/s2/addTask"): ok({}),
        ("GET", f"{ASANA_API}/projects/999/tasks"): ok(load_json("asana/list_project_tasks.json")),
        ("GET", f"{ASANA_API}/users/me"): ok(load_json("asana/me.json")),
        ("POST", f"{ASANA_API}/webhooks"): ok(load_json("asana/webhook_create.json")),
        ("DELETE", f"{ASANA_API}/webhooks/wh1"): ok({}),
    }


@pytest.fixture
def asana_transport(asana_records: dict[tuple[str, str], Any]) -> FixturePMTransport:
    return FixturePMTransport(asana_records)


@pytest.fixture
def asana_adapter(asana_transport: FixturePMTransport, asana_ctx: AdapterContext) -> AsanaAdapter:
    client = AsanaClient(asana_transport, auth_header="Bearer asana_xxx")
    return AsanaAdapter(client, asana_ctx)


# --- Monday.com ----------------------------------------------------------- #


@pytest.fixture
def monday_ctx() -> AdapterContext:
    return AdapterContext(
        connection_id=uuid4(),
        workspace_id=uuid4(),
        provider=PMProvider.monday,
        external_project_key="500",
        external_project_id="500",
        config={"granted_scopes": ["default"]},
    )


@pytest.fixture
def monday_records() -> dict[tuple[str, str], Any]:
    return {
        ("POST", "Item"): ok(load_json("monday/item.json")),
        ("POST", "CreateItem"): ok(load_json("monday/create_item.json")),
        ("POST", "ChangeMultipleColumnValues"): ok(
            load_json("monday/change_multiple_column_values.json")
        ),
        ("POST", "BoardGroups"): ok(load_json("monday/board_groups.json")),
        ("POST", "BoardItems"): ok(load_json("monday/board_items.json")),
        ("POST", "Me"): ok(load_json("monday/me.json")),
        ("POST", "CreateWebhook"): ok(load_json("monday/create_webhook.json")),
        ("POST", "DeleteWebhook"): ok({"data": {"delete_webhook": {"id": "wh1"}}}),
    }


@pytest.fixture
def monday_transport(monday_records: dict[tuple[str, str], Any]) -> FixturePMTransport:
    return FixturePMTransport(monday_records)


@pytest.fixture
def monday_adapter(
    monday_transport: FixturePMTransport, monday_ctx: AdapterContext
) -> MondayAdapter:
    client = MondayClient(monday_transport, auth_header="monday_xxx")
    return MondayAdapter(client, monday_ctx)


# --- GitHub Projects (v2) --------------------------------------------------- #


@pytest.fixture
def github_projects_ctx() -> AdapterContext:
    return AdapterContext(
        connection_id=uuid4(),
        workspace_id=uuid4(),
        provider=PMProvider.github_projects,
        external_project_key="PVT_1",
        external_project_id="PVT_1",
        config={"granted_scopes": ["default"]},
    )


@pytest.fixture
def github_projects_records() -> dict[tuple[str, str], Any]:
    # Unlike Jira (one transition lookup) / Linear (one workflow-state lookup),
    # GitHubProjectsAdapter resolves *each* single-select field independently
    # (status, then priority) and re-fetches the item after every write, so a
    # single create/update call issues several "Item"/"ProjectFields" queries —
    # provide a few identical replays of each rather than exactly one.
    item = ok(load_json("github_projects/item.json"))
    fields = ok(load_json("github_projects/project_fields.json"))
    field_value = ok(load_json("github_projects/update_item_field_value.json"))
    return {
        ("POST", "Item"): [item, item, item, item],
        ("POST", "AddDraftIssue"): ok(load_json("github_projects/add_draft_issue.json")),
        ("POST", "UpdateDraftIssue"): ok(load_json("github_projects/update_draft_issue.json")),
        ("POST", "UpdateItemFieldValue"): [field_value, field_value, field_value, field_value],
        ("POST", "ProjectFields"): [fields, fields, fields, fields],
        ("POST", "ProjectItems"): ok(load_json("github_projects/project_items.json")),
        ("POST", "Viewer"): ok(load_json("github_projects/viewer.json")),
    }


@pytest.fixture
def github_projects_transport(
    github_projects_records: dict[tuple[str, str], Any],
) -> FixturePMTransport:
    return FixturePMTransport(github_projects_records)


@pytest.fixture
def github_projects_adapter(
    github_projects_transport: FixturePMTransport, github_projects_ctx: AdapterContext
) -> GitHubProjectsAdapter:
    client = GitHubProjectsClient(github_projects_transport, auth_header="Bearer ghs_xxx")
    return GitHubProjectsAdapter(client, github_projects_ctx)
