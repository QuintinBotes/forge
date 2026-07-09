"""Cross-provider conformance suite (F40-PM-ADAPTERS-1).

Every ``PMAdapter`` implementation — Jira and Linear (F18), plus Asana,
Monday.com, and GitHub Projects (F40) — must satisfy the same async Protocol
and behave identically from the sync engine's point of view: fetch/create/
update/list a task, report connection health, and register/unregister a
webhook. This module parametrizes one shared assertion set over all five
providers' adapter fixtures (declared in ``conftest.py``) so a new provider
that fails here is *not* conformant, regardless of how its provider-specific
unit tests (``test_clients.py``, ``test_mapping.py``, ``test_webhooks.py``)
look.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from forge_contracts.enums import Direction
from forge_contracts.pm import ForgePriority, ForgeTask, StatusCategory
from forge_contracts.pm import PMAdapter as PMAdapterProtocol

PROVIDERS = ["jira", "linear", "asana", "monday", "github_projects"]

# A pre-existing external id each provider's fixture data resolves via GET/fetch.
EXTERNAL_IDS: dict[str, str] = {
    "jira": "10001",
    "linear": "uuid-1",
    "asana": "10001",
    "monday": "1001",
    "github_projects": "PVTI_1",
}


def _adapter(request: pytest.FixtureRequest, provider: str):
    return request.getfixturevalue(f"{provider}_adapter")


def _forge_task(**overrides) -> ForgeTask:
    base = {
        "id": uuid4(),
        "key": "CONF-1",
        "project_id": uuid4(),
        "title": "Conformance task",
        "description_md": "conformance body",
        "status_category": StatusCategory.started,
        "priority": ForgePriority.high,
        "version": 1,
        "updated_at": datetime.now(UTC),
    }
    base.update(overrides)
    return ForgeTask(**base)


# --------------------------------------------------------------------------- #
# Protocol conformance                                                        #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("provider", PROVIDERS)
def test_adapter_conforms_to_pm_adapter_protocol(
    provider: str, request: pytest.FixtureRequest
) -> None:
    adapter = _adapter(request, provider)
    assert isinstance(adapter, PMAdapterProtocol)
    assert adapter.provider == provider


# --------------------------------------------------------------------------- #
# Pure mapping — never raises for every StatusCategory / ForgePriority         #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("provider", PROVIDERS)
def test_status_category_out_round_trips_for_every_category(
    provider: str, request: pytest.FixtureRequest
) -> None:
    adapter = _adapter(request, provider)
    for category in StatusCategory:
        external_value = adapter.map_status(category.value, Direction.OUT)
        assert isinstance(external_value, str) and external_value


@pytest.mark.parametrize("provider", PROVIDERS)
def test_priority_out_round_trips_for_every_priority(
    provider: str, request: pytest.FixtureRequest
) -> None:
    adapter = _adapter(request, provider)
    for priority in ForgePriority:
        external_value = adapter.map_priority(priority.value, Direction.OUT)
        assert isinstance(external_value, str) and external_value


# --------------------------------------------------------------------------- #
# External I/O — identical shape across every provider                        #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_fetch_external_returns_populated_task(
    provider: str, request: pytest.FixtureRequest
) -> None:
    adapter = _adapter(request, provider)
    ext = await adapter.fetch_external(EXTERNAL_IDS[provider])
    assert ext.provider == provider
    assert ext.external_id
    assert ext.title
    assert ext.status_category in set(StatusCategory)


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_create_external_returns_task_with_external_id(
    provider: str, request: pytest.FixtureRequest
) -> None:
    adapter = _adapter(request, provider)
    created = await adapter.create_external(_forge_task())
    assert created.provider == provider
    assert created.external_id


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_update_external_returns_task_with_same_external_id(
    provider: str, request: pytest.FixtureRequest
) -> None:
    adapter = _adapter(request, provider)
    external_id = EXTERNAL_IDS[provider]
    updated = await adapter.update_external(external_id, _forge_task())
    assert updated.external_id == external_id


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_list_external_returns_a_page_of_tasks(
    provider: str, request: pytest.FixtureRequest
) -> None:
    adapter = _adapter(request, provider)
    tasks, _cursor = await adapter.list_external()
    assert isinstance(tasks, list)
    assert len(tasks) >= 1
    assert all(t.provider == provider for t in tasks)


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_get_connection_health_reports_connected(
    provider: str, request: pytest.FixtureRequest
) -> None:
    adapter = _adapter(request, provider)
    health = await adapter.get_connection_health()
    assert health.status == "connected"
    assert health.provider == provider
    assert health.latency_ms >= 0


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_register_and_unregister_webhook_round_trips(
    provider: str, request: pytest.FixtureRequest
) -> None:
    adapter = _adapter(request, provider)
    webhook_id = await adapter.register_webhook("https://forge.example/webhook", "shh-secret")
    assert isinstance(webhook_id, str) and webhook_id
    await adapter.unregister_webhook(webhook_id)  # must not raise
