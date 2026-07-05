"""Unit tests for Jira/Linear status + priority + field mapping (AC4, AC5)."""

from __future__ import annotations

import pytest

from forge_contracts.enums import Direction
from forge_contracts.pm import AdapterContext, PMProvider
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
