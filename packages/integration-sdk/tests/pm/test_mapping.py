"""Unit tests for Jira/Linear status + priority + field mapping (AC4, AC5)."""

from __future__ import annotations

import pytest

from forge_contracts.enums import Direction
from forge_contracts.pm import AdapterContext, PMProvider, StatusCategory
from forge_integrations.pm.errors import MappingError
from forge_integrations.pm.jira import mapping as jmap
from forge_integrations.pm.jira.adapter import JiraAdapter
from forge_integrations.pm.jira.client import JiraClient
from forge_integrations.pm.linear.adapter import LinearAdapter
from forge_integrations.pm.linear.client import LinearClient
from forge_integrations.pm.transport import FixturePMTransport


def _jira(**overrides) -> JiraAdapter:
    ctx = AdapterContext(
        connection_id=__import__("uuid").uuid4(),
        workspace_id=__import__("uuid").uuid4(),
        provider=PMProvider.jira,
        external_project_key="ENG",
        external_project_id="10000",
        external_base_url="https://acme.atlassian.net",
        **overrides,
    )
    return JiraAdapter(JiraClient(FixturePMTransport({}), base_url="https://x"), ctx)


def _linear(**overrides) -> LinearAdapter:
    ctx = AdapterContext(
        connection_id=__import__("uuid").uuid4(),
        workspace_id=__import__("uuid").uuid4(),
        provider=PMProvider.linear,
        external_project_key="ENG",
        external_project_id="team-1",
        **overrides,
    )
    return LinearAdapter(LinearClient(FixturePMTransport({})), ctx)


# --- Jira status (§4 table) ------------------------------------------------- #

JIRA_STATUS_OUT = [
    ("backlog", "new"),
    ("unstarted", "new"),
    ("started", "indeterminate"),
    ("completed", "done"),
    ("canceled", "done"),
]
JIRA_STATUS_IN = [
    ("new", "backlog"),
    ("indeterminate", "started"),
    ("done", "completed"),
]


@pytest.mark.parametrize(("category", "expected"), JIRA_STATUS_OUT)
def test_status_map_table_jira_out(category: str, expected: str) -> None:
    assert _jira().map_status(category, Direction.OUT) == expected


@pytest.mark.parametrize(("key", "expected"), JIRA_STATUS_IN)
def test_status_map_table_jira_in(key: str, expected: str) -> None:
    assert _jira().map_status(key, Direction.IN) == expected


# --- Linear status (§4 table — 1:1) ----------------------------------------- #

LINEAR_STATUS = ["backlog", "unstarted", "started", "completed", "canceled"]


@pytest.mark.parametrize("category", LINEAR_STATUS)
def test_status_map_table_linear_both_directions(category: str) -> None:
    a = _linear()
    assert a.map_status(category, Direction.OUT) == category
    assert a.map_status(category, Direction.IN) == category


# --- Priority (§4 table) ---------------------------------------------------- #

JIRA_PRIORITY_OUT = [
    ("none", "Medium"),
    ("low", "Low"),
    ("medium", "Medium"),
    ("high", "High"),
    ("urgent", "Highest"),
]
LINEAR_PRIORITY_OUT = [
    ("none", "0"),
    ("low", "4"),
    ("medium", "3"),
    ("high", "2"),
    ("urgent", "1"),
]


@pytest.mark.parametrize(("forge", "expected"), JIRA_PRIORITY_OUT)
def test_priority_map_table_jira_out(forge: str, expected: str) -> None:
    assert _jira().map_priority(forge, Direction.OUT) == expected


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Lowest", "low"),
        ("Low", "low"),
        ("Medium", "medium"),
        ("High", "high"),
        ("Highest", "urgent"),
    ],
)
def test_priority_map_table_jira_in(name: str, expected: str) -> None:
    assert _jira().map_priority(name, Direction.IN) == expected


@pytest.mark.parametrize(("forge", "expected"), LINEAR_PRIORITY_OUT)
def test_priority_map_table_linear_both_directions(forge: str, expected: str) -> None:
    a = _linear()
    assert a.map_priority(forge, Direction.OUT) == expected
    assert a.map_priority(expected, Direction.IN) == forge


# --- Overrides + errors ----------------------------------------------------- #


def test_status_map_override_precedence() -> None:
    a = _jira(status_map={"started": "in_dev"})
    assert a.map_status("started", Direction.OUT) == "in_dev"
    assert a.map_status("in_dev", Direction.IN) == "started"


def test_priority_map_override_precedence() -> None:
    a = _linear(priority_map={"urgent": "9"})
    assert a.map_priority("urgent", Direction.OUT) == "9"
    assert a.map_priority("9", Direction.IN) == "urgent"


def test_status_map_unmappable_raises() -> None:
    with pytest.raises(MappingError):
        _jira().map_status("totally-unknown", Direction.IN)


def test_priority_map_unmappable_raises() -> None:
    with pytest.raises(MappingError):
        _linear().map_priority("not-a-priority", Direction.IN)


def test_map_fields_rename_both_directions() -> None:
    a = _jira()
    inward = a.map_fields({"summary": "T", "description": "D"}, Direction.IN)
    assert inward == {"title": "T", "description": "D"}
    outward = a.map_fields({"title": "T"}, Direction.OUT)
    assert outward == {"summary": "T"}


def test_jira_adf_roundtrip_basic() -> None:
    adf = jmap.markdown_to_adf("Hello world\n\nSecond para")
    assert adf["type"] == "doc"
    md = jmap.adf_to_markdown(adf)
    assert "Hello world" in md
    assert "Second para" in md


def test_linear_markdown_passthrough() -> None:
    from forge_integrations.pm.linear import mapping as lmap

    assert lmap.markdown_passthrough("**bold**") == "**bold**"
    assert lmap.markdown_passthrough(None) == ""


# --- Asana (section-name status, custom-field priority) --------------------- #

ASANA_STATUS_IN = [
    ("To Do", "unstarted"),
    ("to do", "unstarted"),
    ("In Progress", "started"),
    ("Done", "completed"),
    ("Cancelled", "canceled"),
]


def _asana(**overrides):
    from forge_integrations.pm.asana.adapter import AsanaAdapter
    from forge_integrations.pm.asana.client import AsanaClient

    ctx = AdapterContext(
        connection_id=__import__("uuid").uuid4(),
        workspace_id=__import__("uuid").uuid4(),
        provider=PMProvider.asana,
        external_project_key="999",
        external_project_id="999",
        **overrides,
    )
    return AsanaAdapter(AsanaClient(FixturePMTransport({})), ctx)


@pytest.mark.parametrize(("name", "expected"), ASANA_STATUS_IN)
def test_status_map_table_asana_in(name: str, expected: str) -> None:
    assert _asana().map_status(name, Direction.IN) == expected


@pytest.mark.parametrize(
    ("category", "expected"),
    [
        ("backlog", "Backlog"),
        ("unstarted", "To Do"),
        ("started", "In Progress"),
        ("completed", "Done"),
        ("canceled", "Cancelled"),
    ],
)
def test_status_map_table_asana_out(category: str, expected: str) -> None:
    assert _asana().map_status(category, Direction.OUT) == expected


@pytest.mark.parametrize(
    ("forge", "expected"),
    [
        ("none", "Medium"),
        ("low", "Low"),
        ("medium", "Medium"),
        ("high", "High"),
        ("urgent", "Urgent"),
    ],
)
def test_priority_map_table_asana_out(forge: str, expected: str) -> None:
    assert _asana().map_priority(forge, Direction.OUT) == expected


def test_asana_status_override_precedence() -> None:
    a = _asana(status_map={"started": "Doing"})
    assert a.map_status("started", Direction.OUT) == "Doing"
    assert a.map_status("Doing", Direction.IN) == "started"


def test_asana_status_unmappable_raises() -> None:
    with pytest.raises(MappingError):
        _asana().map_status("some-unknown-column", Direction.IN)


# --- Monday.com (status-column label) ---------------------------------------- #


def _monday(**overrides):
    from forge_integrations.pm.monday.adapter import MondayAdapter
    from forge_integrations.pm.monday.client import MondayClient

    ctx = AdapterContext(
        connection_id=__import__("uuid").uuid4(),
        workspace_id=__import__("uuid").uuid4(),
        provider=PMProvider.monday,
        external_project_key="500",
        external_project_id="500",
        **overrides,
    )
    return MondayAdapter(MondayClient(FixturePMTransport({})), ctx)


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("Not Started", "unstarted"),
        ("Working on it", "started"),
        ("stuck", "started"),
        ("Done", "completed"),
        ("Cancelled", "canceled"),
    ],
)
def test_status_map_table_monday_in(label: str, expected: str) -> None:
    assert _monday().map_status(label, Direction.IN) == expected


@pytest.mark.parametrize(
    ("forge", "expected"),
    [("low", "Low"), ("medium", "Medium"), ("high", "High"), ("urgent", "Critical")],
)
def test_priority_map_table_monday_out(forge: str, expected: str) -> None:
    assert _monday().map_priority(forge, Direction.OUT) == expected


def test_monday_priority_override_precedence() -> None:
    m = _monday(priority_map={"urgent": "P0"})
    assert m.map_priority("urgent", Direction.OUT) == "P0"
    assert m.map_priority("P0", Direction.IN) == "urgent"


# --- GitHub Projects v2 (single-select "Status"/"Priority" fields) ---------- #


def _gh_projects(**overrides):
    from forge_integrations.pm.github_projects.adapter import GitHubProjectsAdapter
    from forge_integrations.pm.github_projects.client import GitHubProjectsClient

    ctx = AdapterContext(
        connection_id=__import__("uuid").uuid4(),
        workspace_id=__import__("uuid").uuid4(),
        provider=PMProvider.github_projects,
        external_project_key="PVT_1",
        external_project_id="PVT_1",
        **overrides,
    )
    return GitHubProjectsAdapter(GitHubProjectsClient(FixturePMTransport({})), ctx)


@pytest.mark.parametrize(
    ("option", "expected"),
    [
        ("Todo", "unstarted"),
        ("In Progress", "started"),
        ("Done", "completed"),
        ("Cancelled", "canceled"),
    ],
)
def test_status_map_table_github_projects_in(option: str, expected: str) -> None:
    assert _gh_projects().map_status(option, Direction.IN) == expected


@pytest.mark.parametrize(
    ("category", "expected"),
    [
        ("backlog", "Backlog"),
        ("unstarted", "Todo"),
        ("started", "In Progress"),
        ("completed", "Done"),
        ("canceled", "Cancelled"),
    ],
)
def test_status_map_table_github_projects_out(category: str, expected: str) -> None:
    assert _gh_projects().map_status(category, Direction.OUT) == expected


def test_github_projects_status_unmappable_raises() -> None:
    with pytest.raises(MappingError):
        _gh_projects().map_status("Some Unknown Column", Direction.IN)


# --- ClickUp (native status/priority fields) -------------------------------- #


def _clickup(**overrides):
    from forge_integrations.pm.clickup.adapter import ClickUpAdapter
    from forge_integrations.pm.clickup.client import ClickUpClient

    ctx = AdapterContext(
        connection_id=__import__("uuid").uuid4(),
        workspace_id=__import__("uuid").uuid4(),
        provider=PMProvider.clickup,
        external_project_key="lst1",
        external_project_id="lst1",
        **overrides,
    )
    return ClickUpAdapter(ClickUpClient(FixturePMTransport({})), ctx)


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("to do", "unstarted"),
        ("in progress", "started"),
        ("complete", "completed"),
        ("closed", "canceled"),
    ],
)
def test_status_map_table_clickup_in(label: str, expected: str) -> None:
    assert _clickup().map_status(label, Direction.IN) == expected


@pytest.mark.parametrize(
    ("forge", "expected"),
    [("low", "low"), ("medium", "normal"), ("high", "high"), ("urgent", "urgent")],
)
def test_priority_map_table_clickup_out(forge: str, expected: str) -> None:
    assert _clickup().map_priority(forge, Direction.OUT) == expected


def test_clickup_status_unmappable_raises() -> None:
    with pytest.raises(MappingError):
        _clickup().map_status("some-unknown-status", Direction.IN)


# --- Trello (list-name status, label priority) ------------------------------- #


def _trello(**overrides):
    from forge_integrations.pm.trello.adapter import TrelloAdapter
    from forge_integrations.pm.trello.client import TrelloClient

    ctx = AdapterContext(
        connection_id=__import__("uuid").uuid4(),
        workspace_id=__import__("uuid").uuid4(),
        provider=PMProvider.trello,
        external_project_key="brd1",
        external_project_id="brd1",
        **overrides,
    )
    return TrelloAdapter(TrelloClient(FixturePMTransport({})), ctx)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("To Do", "unstarted"),
        ("Doing", "started"),
        ("Done", "completed"),
        ("Cancelled", "canceled"),
    ],
)
def test_status_map_table_trello_in(name: str, expected: str) -> None:
    assert _trello().map_status(name, Direction.IN) == expected


def test_trello_status_override_precedence() -> None:
    t = _trello(status_map={"started": "Doing now"})
    assert t.map_status("started", Direction.OUT) == "Doing now"
    assert t.map_status("Doing now", Direction.IN) == "started"


# --- GitLab issues (label-based status + priority pools) --------------------- #


def _gitlab(**overrides):
    from forge_integrations.pm.gitlab.adapter import GitLabAdapter
    from forge_integrations.pm.gitlab.client import GitLabClient

    ctx = AdapterContext(
        connection_id=__import__("uuid").uuid4(),
        workspace_id=__import__("uuid").uuid4(),
        provider=PMProvider.gitlab,
        external_project_key="701",
        external_project_id="701",
        **overrides,
    )
    return GitLabAdapter(GitLabClient(FixturePMTransport({})), ctx)


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("To Do", "unstarted"),
        ("Doing", "started"),
        ("Done", "completed"),
        ("Closed", "canceled"),
    ],
)
def test_status_map_table_gitlab_in(label: str, expected: str) -> None:
    assert _gitlab().map_status(label, Direction.IN) == expected


@pytest.mark.parametrize(
    ("forge", "expected"),
    [
        ("low", "Priority: Low"),
        ("medium", "Priority: Medium"),
        ("high", "Priority: High"),
        ("urgent", "Priority: Urgent"),
    ],
)
def test_priority_map_table_gitlab_out(forge: str, expected: str) -> None:
    assert _gitlab().map_priority(forge, Direction.OUT) == expected


def test_gitlab_status_and_priority_label_pools_do_not_collide() -> None:
    from forge_integrations.pm import gitlab as gitlab_pkg  # noqa: F401
    from forge_integrations.pm.gitlab import mapping as glmap

    status_names = {v.lower() for v in glmap.STATUS_OUT.values()}
    priority_names = {v.lower() for v in glmap.PRIORITY_OUT.values()}
    assert status_names.isdisjoint(priority_names)


# --- Generic / BYO-board connector (config-driven mapping) ------------------- #


def test_generic_status_and_priority_out_use_config_map(generic_config) -> None:
    from forge_integrations.pm.generic.adapter import GenericAdapter

    ctx = AdapterContext(
        connection_id=__import__("uuid").uuid4(),
        workspace_id=__import__("uuid").uuid4(),
        provider=PMProvider.generic,
        external_project_key="proj1",
        external_project_id="proj1",
    )
    adapter = GenericAdapter(FixturePMTransport({}), ctx, generic_config)
    assert adapter.map_status("started", Direction.OUT) == "in_progress"
    assert adapter.map_status("in_progress", Direction.IN) == "started"
    assert adapter.map_priority("high", Direction.OUT) == "P2"


def test_generic_status_map_override_precedence(generic_config) -> None:
    from forge_integrations.pm.generic.adapter import GenericAdapter

    ctx = AdapterContext(
        connection_id=__import__("uuid").uuid4(),
        workspace_id=__import__("uuid").uuid4(),
        provider=PMProvider.generic,
        external_project_key="proj1",
        external_project_id="proj1",
        status_map={"started": "custom_doing"},
    )
    adapter = GenericAdapter(FixturePMTransport({}), ctx, generic_config)
    assert adapter.map_status("started", Direction.OUT) == "custom_doing"
    assert adapter.map_status("custom_doing", Direction.IN) == "started"


def test_generic_field_mapping_reads_dotted_paths(generic_config) -> None:
    from forge_integrations.pm.generic.adapter import GenericAdapter

    ctx = AdapterContext(
        connection_id=__import__("uuid").uuid4(),
        workspace_id=__import__("uuid").uuid4(),
        provider=PMProvider.generic,
        external_project_key="proj1",
        external_project_id="proj1",
    )
    adapter = GenericAdapter(FixturePMTransport({}), ctx, generic_config)
    item = {
        "id": "t-9",
        "key": "BYO-9",
        "subject": "A title",
        "state": "open",
        "body": "desc",
        "priority": "P1",
        "assignee": {"email": "bob@acme.test"},
        "tags": ["x", {"name": "y"}],
        "updated_at": "2024-02-01T00:00:00+00:00",
        "link": "https://byo.example/tickets/t-9",
    }
    ext = adapter._item_to_external(item)
    assert ext.title == "A title"
    assert ext.status_category is StatusCategory.unstarted
    assert ext.priority_token == "P1"
    assert ext.assignee_email == "bob@acme.test"
    assert ext.labels == ["x", "y"]
    assert ext.external_key == "BYO-9"
    assert ext.url == "https://byo.example/tickets/t-9"


def test_generic_build_body_writes_dotted_paths(generic_config) -> None:
    from forge_integrations.pm.generic.adapter import GenericAdapter

    ctx = AdapterContext(
        connection_id=__import__("uuid").uuid4(),
        workspace_id=__import__("uuid").uuid4(),
        provider=PMProvider.generic,
        external_project_key="proj1",
        external_project_id="proj1",
    )
    adapter = GenericAdapter(FixturePMTransport({}), ctx, generic_config)
    task = _forge_task_for_generic(status_category="started")
    body = adapter._build_body(task)
    assert body["subject"] == task.title
    assert body["state"] == "in_progress"
    assert body["priority"] == "P2"


async def test_generic_transition_endpoint_used_for_status_instead_of_update_body(
    generic_config,
) -> None:
    """When ``endpoints.transition`` is configured, the mapped status is posted
    there (Jira-shaped workflow-transition call) rather than folded into the
    plain ``update`` PUT body, and the adapter re-fetches via ``get`` after.
    """
    from forge_integrations.pm.generic.adapter import GenericAdapter

    cfg = generic_config.model_copy(
        update={
            "endpoints": generic_config.endpoints.model_copy(
                update={"transition": "/tickets/{external_id}/transition"}
            ),
        }
    )
    ctx = AdapterContext(
        connection_id=__import__("uuid").uuid4(),
        workspace_id=__import__("uuid").uuid4(),
        provider=PMProvider.generic,
        external_project_key="proj1",
        external_project_id="proj1",
    )
    transport = FixturePMTransport(
        {
            ("PUT", "/tickets/t-1"): _ok_response(
                {
                    "id": "t-1",
                    "key": "BYO-1",
                    "subject": "Updated title",
                    "state": "in_progress",
                    "body": "b",
                    "priority": "P2",
                    "assignee": {},
                    "tags": [],
                    "updated_at": "2024-01-01T00:10:00+00:00",
                    "link": "https://byo.example/tickets/t-1",
                }
            ),
            ("POST", "/tickets/t-1/transition"): _ok_response({"ok": True}),
            ("GET", "/tickets/t-1"): _ok_response(
                {
                    "id": "t-1",
                    "key": "BYO-1",
                    "subject": "Updated title",
                    "state": "resolved",
                    "body": "b",
                    "priority": "P2",
                    "assignee": {},
                    "tags": [],
                    "updated_at": "2024-01-01T00:20:00+00:00",
                    "link": "https://byo.example/tickets/t-1",
                }
            ),
        }
    )
    adapter = GenericAdapter(transport, ctx, cfg)
    task = _forge_task_for_generic(status_category="completed")
    result = await adapter.update_external("t-1", task)

    put_calls = [c for c in transport.call_log if c["method"] == "PUT"]
    assert "state" not in (put_calls[0]["json"] or {})

    transition_calls = [c for c in transport.call_log if c["method"] == "POST"]
    assert transition_calls, "expected a POST to the transition endpoint"
    assert transition_calls[0]["json"] == {"state": "resolved"}

    # Result reflects the re-fetched (GET) state, not the PUT response.
    assert result.status_category is StatusCategory.completed


def _ok_response(body):
    from forge_contracts.pm import HttpResponse

    return HttpResponse(status_code=200, json_body=body)


def _forge_task_for_generic(**overrides):
    from uuid import uuid4

    from forge_contracts.pm import ForgePriority, ForgeTask

    base: dict = {
        "id": uuid4(),
        "key": "GEN-1",
        "project_id": uuid4(),
        "title": "Generic task",
        "description_md": "body",
        "status_category": StatusCategory.started,
        "priority": ForgePriority.high,
        "version": 1,
        "updated_at": __import__("datetime").datetime.now(__import__("datetime").UTC),
    }
    base.update(overrides)
    if isinstance(base.get("status_category"), str):
        base["status_category"] = StatusCategory(base["status_category"])
    return ForgeTask(**base)


# --- GenericAdapterConfig validation (bad-config rejection) ------------------ #


def test_generic_config_rejects_non_http_base_url() -> None:
    import pytest as _pytest
    from pydantic import ValidationError

    from forge_contracts.pm import GenericAdapterConfig, GenericEndpointConfig

    with _pytest.raises(ValidationError):
        GenericAdapterConfig(
            base_url="ftp://example.com",
            endpoints=GenericEndpointConfig(get="/a", create="/b", update="/c", list="/d"),
            fields={"title": "subject", "status": "state"},
            status_map={
                "backlog": "b",
                "unstarted": "u",
                "started": "s",
                "completed": "c",
                "canceled": "x",
            },
        )


def test_generic_config_rejects_missing_required_field_mapping() -> None:
    import pytest as _pytest
    from pydantic import ValidationError

    from forge_contracts.pm import GenericAdapterConfig, GenericEndpointConfig

    with _pytest.raises(ValidationError):
        GenericAdapterConfig(
            base_url="https://example.com",
            endpoints=GenericEndpointConfig(get="/a", create="/b", update="/c", list="/d"),
            fields={"title": "subject"},  # missing required "status"
            status_map={
                "backlog": "b",
                "unstarted": "u",
                "started": "s",
                "completed": "c",
                "canceled": "x",
            },
        )


def test_generic_config_rejects_incomplete_status_map() -> None:
    import pytest as _pytest
    from pydantic import ValidationError

    from forge_contracts.pm import GenericAdapterConfig, GenericEndpointConfig

    with _pytest.raises(ValidationError):
        GenericAdapterConfig(
            base_url="https://example.com",
            endpoints=GenericEndpointConfig(get="/a", create="/b", update="/c", list="/d"),
            fields={"title": "subject", "status": "state"},
            status_map={"backlog": "b", "unstarted": "u"},  # missing 3 categories
        )


def test_generic_config_rejects_incomplete_priority_map_when_priority_field_mapped() -> None:
    """Mirrors status_map's completeness guarantee: priority_map is consumed via
    the same fail-closed ``resolve_value`` (MappingError, no fallback) whenever
    'priority_token' is in fields, so an incomplete/omitted priority_map must
    be rejected eagerly here rather than crashing uncaught at first write.
    """
    import pytest as _pytest
    from pydantic import ValidationError

    from forge_contracts.pm import GenericAdapterConfig, GenericEndpointConfig

    with _pytest.raises(ValidationError):
        GenericAdapterConfig(
            base_url="https://example.com",
            endpoints=GenericEndpointConfig(get="/a", create="/b", update="/c", list="/d"),
            fields={"title": "subject", "status": "state", "priority_token": "priority"},
            status_map={
                "backlog": "b",
                "unstarted": "u",
                "started": "s",
                "completed": "c",
                "canceled": "x",
            },
            # priority_map omitted entirely -- must not pass validation.
        )


def test_generic_config_allows_omitted_priority_map_when_priority_field_not_mapped() -> None:
    from forge_contracts.pm import GenericAdapterConfig, GenericEndpointConfig

    cfg = GenericAdapterConfig(
        base_url="https://example.com",
        endpoints=GenericEndpointConfig(get="/a", create="/b", update="/c", list="/d"),
        fields={"title": "subject", "status": "state"},  # no priority_token mapped
        status_map={
            "backlog": "b",
            "unstarted": "u",
            "started": "s",
            "completed": "c",
            "canceled": "x",
        },
    )
    assert cfg.priority_map == {}


def test_generic_config_rejects_empty_endpoint_template() -> None:
    import pytest as _pytest
    from pydantic import ValidationError

    from forge_contracts.pm import GenericAdapterConfig, GenericEndpointConfig

    with _pytest.raises(ValidationError):
        GenericAdapterConfig(
            base_url="https://example.com",
            endpoints=GenericEndpointConfig(get="", create="/b", update="/c", list="/d"),
            fields={"title": "subject", "status": "state"},
            status_map={
                "backlog": "b",
                "unstarted": "u",
                "started": "s",
                "completed": "c",
                "canceled": "x",
            },
        )


def test_generic_config_rejects_unknown_field_name() -> None:
    import pytest as _pytest
    from pydantic import ValidationError

    from forge_contracts.pm import GenericAdapterConfig, GenericEndpointConfig

    with _pytest.raises(ValidationError):
        GenericAdapterConfig(
            base_url="https://example.com",
            endpoints=GenericEndpointConfig(get="/a", create="/b", update="/c", list="/d"),
            fields={"title": "subject", "status": "state", "made_up_field": "x"},
            status_map={
                "backlog": "b",
                "unstarted": "u",
                "started": "s",
                "completed": "c",
                "canceled": "x",
            },
        )


def test_generic_config_accepts_a_well_formed_config(generic_config) -> None:
    assert generic_config.base_url == "https://byo.example"
    assert generic_config.status_map["started"] == "in_progress"
