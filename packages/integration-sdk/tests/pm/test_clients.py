"""Adapter + client tests over FixturePMTransport (no sockets) — AC3/6/7/21/23."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from forge_contracts.enums import Direction
from forge_contracts.pm import (
    ForgePriority,
    ForgeTask,
    HttpResponse,
    PMProvider,
    StatusCategory,
)
from forge_integrations.pm import FixturePMTransport
from forge_integrations.pm.errors import RateLimitError
from forge_integrations.pm.jira.client import JiraClient


def _forge_task(**overrides) -> ForgeTask:
    base = {
        "id": uuid4(),
        "key": "TASK-1",
        "project_id": uuid4(),
        "title": "Add pagination",
        "description_md": "Add pagination to the list endpoint",
        "status_category": StatusCategory.started,
        "priority": ForgePriority.high,
        "version": 1,
        "updated_at": datetime.now(UTC),
    }
    base.update(overrides)
    return ForgeTask(**base)


# --- Jira ------------------------------------------------------------------- #


async def test_jira_fetch_external(jira_adapter) -> None:
    ext = await jira_adapter.fetch_external("10001")
    assert ext.provider is PMProvider.jira
    assert ext.external_key == "ENG-1"
    assert ext.status_category is StatusCategory.started
    assert ext.priority_token == "High"
    assert ext.assignee_email == "alice@acme.test"
    assert "backend" in ext.labels
    assert ext.url.endswith("/browse/ENG-1")


async def test_jira_create_external(jira_adapter, jira_transport) -> None:
    ext = await jira_adapter.create_external(_forge_task())
    assert ext.external_id == "10001"
    # the POST /issue create call was actually made
    methods = {(c["method"], c["url"].split("atlassian.net")[-1]) for c in jira_transport.call_log}
    assert ("POST", "/rest/api/3/issue") in methods


async def test_jira_update_uses_transition_for_status(jira_adapter, jira_transport) -> None:
    await jira_adapter.update_external("10001", _forge_task(status_category=StatusCategory.started))
    paths = [c["url"] for c in jira_transport.call_log]
    assert any(
        p.endswith("/issue/10001/transitions") and m == "POST"
        for p, m in [(c["url"], c["method"]) for c in jira_transport.call_log]
    )
    assert any("/issue/10001/transitions" in p for p in paths)


async def test_jira_list_external_paginates(jira_adapter) -> None:
    tasks, cursor = await jira_adapter.list_external(limit=50)
    assert len(tasks) == 2
    assert cursor is None  # total == returned


async def test_jira_health_myself(jira_adapter) -> None:
    health = await jira_adapter.get_connection_health()
    assert health.status == "connected"
    assert health.account == "me@acme.test"
    assert health.latency_ms >= 0


async def test_jira_register_unregister_webhook(jira_adapter) -> None:
    wid = await jira_adapter.register_webhook("https://forge/webhook", "secret")
    assert wid == "55"
    await jira_adapter.unregister_webhook("55")  # no raise


async def test_jira_health_error_on_auth_failure(jira_ctx) -> None:
    transport = FixturePMTransport(
        {("GET", "/rest/api/3/myself"): HttpResponse(status_code=401, json_body={})}
    )
    from forge_integrations.pm.jira.adapter import JiraAdapter

    adapter = JiraAdapter(JiraClient(transport, base_url="https://x"), jira_ctx)
    health = await adapter.get_connection_health()
    assert health.status == "error"
    assert health.error  # redacted message present


# --- Linear ----------------------------------------------------------------- #


async def test_linear_fetch_external(linear_adapter) -> None:
    ext = await linear_adapter.fetch_external("uuid-1")
    assert ext.provider is PMProvider.linear
    assert ext.external_key == "ENG-1"
    assert ext.status_category is StatusCategory.started
    assert ext.priority_token == "2"
    assert ext.assignee_email == "alice@acme.test"


async def test_linear_create_external_resolves_state(linear_adapter, linear_transport) -> None:
    ext = await linear_adapter.create_external(_forge_task())
    assert ext.external_id == "uuid-1"
    ops = [c["json"].get("query", "") for c in linear_transport.call_log if c.get("json")]
    assert any("IssueCreate" in q for q in ops)
    assert any("States" in q for q in ops)  # workflow-state lookup happened


async def test_linear_update_external(linear_adapter) -> None:
    ext = await linear_adapter.update_external("uuid-1", _forge_task())
    assert ext.external_id == "uuid-1"


async def test_linear_list_external(linear_adapter) -> None:
    tasks, cursor = await linear_adapter.list_external()
    assert len(tasks) == 2
    assert cursor is None


async def test_linear_health_viewer(linear_adapter) -> None:
    health = await linear_adapter.get_connection_health()
    assert health.status == "connected"
    assert health.account == "me@acme.test"


async def test_linear_webhook_create_delete(linear_adapter) -> None:
    wid = await linear_adapter.register_webhook("https://forge/webhook", "secret")
    assert wid == "wh-1"
    await linear_adapter.unregister_webhook("wh-1")


# --- Cross-cutting ---------------------------------------------------------- #


def test_clients_redact_auth_in_serialized_output(jira_adapter, linear_adapter) -> None:
    # The auth header lives only on the client's private headers; it must not be
    # discoverable via the adapter's public/serialized surface.
    for adapter in (jira_adapter, linear_adapter):
        blob = repr(vars(adapter))
        assert "Basic xxx" not in blob
        assert "lin_api_xxx" not in blob


async def test_clients_raise_on_rate_limit(jira_ctx) -> None:
    transport = FixturePMTransport(
        {
            ("GET", "/rest/api/3/issue/10001"): HttpResponse(
                status_code=429, json_body={}, headers={"Retry-After": "2"}
            )
        }
    )
    from forge_integrations.pm.jira.adapter import JiraAdapter

    adapter = JiraAdapter(JiraClient(transport, base_url="https://x"), jira_ctx)
    with pytest.raises(RateLimitError) as exc:
        await adapter.fetch_external("10001")
    assert exc.value.retry_after == 2.0


async def test_unexpected_call_raises_loudly() -> None:
    transport = FixturePMTransport({})
    with pytest.raises(Exception):  # noqa: B017 - ProviderError subclass
        await transport.request("GET", "https://x/rest/api/3/issue/999")


def test_map_fields_via_protocol(jira_adapter) -> None:
    assert jira_adapter.map_fields({"summary": "T"}, Direction.IN) == {"title": "T"}


# --- Asana -------------------------------------------------------------- #


async def test_asana_fetch_external(asana_adapter) -> None:
    ext = await asana_adapter.fetch_external("10001")
    assert ext.provider is PMProvider.asana
    assert ext.status_category is StatusCategory.started
    assert ext.priority_token == "High"
    assert ext.assignee_email == "alice@acme.test"
    assert "backend" in ext.labels


async def test_asana_create_external_moves_to_matching_section(
    asana_adapter, asana_transport
) -> None:
    ext = await asana_adapter.create_external(_forge_task(status_category=StatusCategory.started))
    assert ext.external_id == "10001"
    methods = {(c["method"], c["url"].split("asana.com")[-1]) for c in asana_transport.call_log}
    assert ("POST", "/api/1.0/tasks") in methods
    assert ("POST", "/api/1.0/sections/s2/addTask") in methods


async def test_asana_update_external(asana_adapter) -> None:
    ext = await asana_adapter.update_external(
        "10001", _forge_task(status_category=StatusCategory.started)
    )
    assert ext.external_id == "10001"


async def test_asana_list_external(asana_adapter) -> None:
    tasks, cursor = await asana_adapter.list_external()
    assert len(tasks) == 2
    assert cursor is None


async def test_asana_health_me(asana_adapter) -> None:
    health = await asana_adapter.get_connection_health()
    assert health.status == "connected"
    assert health.account == "alice@acme.test"


async def test_asana_register_unregister_webhook(asana_adapter) -> None:
    wid = await asana_adapter.register_webhook("https://forge/webhook", "secret")
    assert wid == "wh1"
    await asana_adapter.unregister_webhook(wid)


async def test_asana_no_matching_section_raises(asana_transport, asana_ctx) -> None:
    from forge_integrations.pm.asana.adapter import AsanaAdapter
    from forge_integrations.pm.asana.client import AsanaClient
    from forge_integrations.pm.errors import ProviderError

    adapter = AsanaAdapter(AsanaClient(asana_transport), asana_ctx)
    with pytest.raises(ProviderError):
        await adapter._section_gid_for(_forge_task(status_category=StatusCategory.canceled))


# --- Monday.com --------------------------------------------------------- #


async def test_monday_fetch_external(monday_adapter) -> None:
    ext = await monday_adapter.fetch_external("1001")
    assert ext.provider is PMProvider.monday
    assert ext.status_category is StatusCategory.started
    assert ext.priority_token == "High"


async def test_monday_create_external(monday_adapter, monday_transport) -> None:
    ext = await monday_adapter.create_external(_forge_task(status_category=StatusCategory.started))
    assert ext.external_id == "1002"
    ops = [c["json"].get("query", "") for c in monday_transport.call_log if c.get("json")]
    assert any("CreateItem" in q for q in ops)
    assert any("BoardGroups" in q for q in ops)


async def test_monday_update_external(monday_adapter) -> None:
    ext = await monday_adapter.update_external(
        "1001", _forge_task(status_category=StatusCategory.started)
    )
    assert ext.external_id == "1001"


async def test_monday_list_external(monday_adapter) -> None:
    tasks, cursor = await monday_adapter.list_external()
    assert len(tasks) == 2
    assert cursor is None


async def test_monday_health_me(monday_adapter) -> None:
    health = await monday_adapter.get_connection_health()
    assert health.status == "connected"
    assert health.account == "alice@acme.test"


async def test_monday_register_unregister_webhook(monday_adapter) -> None:
    wid = await monday_adapter.register_webhook("https://forge/webhook", "secret")
    assert wid == "wh1"
    await monday_adapter.unregister_webhook(wid)


# --- GitHub Projects (v2) ------------------------------------------------ #


async def test_github_projects_fetch_external(github_projects_adapter) -> None:
    ext = await github_projects_adapter.fetch_external("PVTI_1")
    assert ext.provider is PMProvider.github_projects
    assert ext.status_category is StatusCategory.started
    assert ext.priority_token == "High"


async def test_github_projects_create_external_sets_fields(
    github_projects_adapter, github_projects_transport
) -> None:
    ext = await github_projects_adapter.create_external(
        _forge_task(status_category=StatusCategory.started)
    )
    assert ext.external_id == "PVTI_1"
    ops = [c["json"].get("query", "") for c in github_projects_transport.call_log if c.get("json")]
    assert any("AddDraftIssue" in q for q in ops)
    assert any("UpdateItemFieldValue" in q for q in ops)


async def test_github_projects_update_external(github_projects_adapter) -> None:
    ext = await github_projects_adapter.update_external(
        "PVTI_1", _forge_task(status_category=StatusCategory.started)
    )
    assert ext.external_id == "PVTI_1"


async def test_github_projects_list_external(github_projects_adapter) -> None:
    tasks, cursor = await github_projects_adapter.list_external()
    assert len(tasks) == 2
    assert cursor is None


async def test_github_projects_health_viewer(github_projects_adapter) -> None:
    health = await github_projects_adapter.get_connection_health()
    assert health.status == "connected"
    assert health.account == "octocat"


async def test_github_projects_webhook_is_app_level_no_op(github_projects_adapter) -> None:
    wid = await github_projects_adapter.register_webhook("https://forge/webhook", "secret")
    assert wid.startswith("app-webhook:")
    await github_projects_adapter.unregister_webhook(wid)  # no raise, no network call
