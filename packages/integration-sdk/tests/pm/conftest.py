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
from forge_integrations.pm.jira.adapter import JiraAdapter
from forge_integrations.pm.jira.client import JiraClient
from forge_integrations.pm.linear.adapter import LinearAdapter
from forge_integrations.pm.linear.client import LinearClient

FIXTURES = Path(__file__).parent / "fixtures"

JIRA_BASE = "https://acme.atlassian.net"
JIRA_API = "/rest/api/3"


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
