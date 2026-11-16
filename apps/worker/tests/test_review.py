"""The independent reviewer station's parts (#173)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.orm import Session, sessionmaker

from scf_agent.providers.family import provider_family, same_family
from scf_contracts.orchestration_config import AgentRole, Effort
from scf_db.base import Base
from scf_db.models import Project, Workspace
from scf_db.models.role_config import AgentRoleConfig
from scf_worker.agent_runner import configured_model
from scf_worker.review import (
    HomogeneousReviewerError,
    ReviewerRefused,
    ensure_heterogeneous,
    parse_verdict,
    reviewer_model,
    reviewer_tools,
)

WS = uuid.UUID("00000000-0000-0000-0000-0000000001a7")


def test_the_reviewer_can_only_read(tmp_path: Path) -> None:
    """Criterion 7: no tool writes files, commits or runs commands."""
    assert sorted(reviewer_tools(tmp_path).names()) == ["list_dir", "read_file", "search_code"]


def test_a_verdict_parses_with_its_findings() -> None:
    output = (
        '```json\n{"verdict": "request_changes", "findings": [{"file": "app.py", "line": 3, '
        '"severity": "blocking", "statement": "health() returns None"}]}\n```'
    )
    verdict = parse_verdict(output)

    assert verdict.verdict == "request_changes"
    [finding] = verdict.findings
    assert (finding.file, finding.line, finding.severity) == ("app.py", 3, "blocking")
    assert verdict.reason is None


@pytest.mark.parametrize(
    "output",
    [
        None,
        "Looks good to me.",
        '{"verdict": "lgtm"}',
        '{"verdict": "approve", "findings": [{"file": "a.py"}]}',
    ],
    ids=["nothing", "prose", "unknown verdict", "finding without line"],
)
def test_output_that_does_not_parse_requests_changes(output: str | None) -> None:
    """Criterion 2: an unparseable verdict is never an approval."""
    verdict = parse_verdict(output)

    assert verdict.verdict == "request_changes"
    assert verdict.reason == "unparseable_verdict"


def test_the_reviewer_cannot_vouch_for_a_whole_diff_or_its_models() -> None:
    """What the station knows (the diff was cut, the model ids) is not the model's to say."""
    verdict = parse_verdict(
        '{"verdict": "approve", "truncated": false, "reviewer_model": "x", "reason": "y"}'
    )

    assert verdict.verdict == "approve"
    assert verdict.reason is None and verdict.reviewer_model is None


def test_the_family_comparison_is_the_red_teams() -> None:
    assert provider_family("claude-sonnet-5-5").value == "anthropic"
    assert provider_family("gpt-6-astra").value == "openai"
    assert same_family("claude-opus-5-5", "claude-haiku-5-5")
    assert not same_family("claude-opus-5-5", "gpt-6-astra")


def test_a_reviewer_of_the_builders_family_is_refused() -> None:
    """Criterion 3."""
    with pytest.raises(HomogeneousReviewerError) as refused:
        ensure_heterogeneous("claude-opus-5-5", "claude-sonnet-5-5")
    assert refused.value.reason == "reviewer_same_family"

    ensure_heterogeneous("claude-opus-5-5", "gpt-6-astra")  # another family is fine


@pytest.mark.parametrize("builder", [None, "a-local-model"], ids=["unrecorded", "unknown"])
def test_a_builder_of_unknown_family_cannot_be_shown_to_differ(builder: str | None) -> None:
    with pytest.raises(ReviewerRefused) as refused:
        ensure_heterogeneous(builder, "gpt-6-astra")
    assert refused.value.reason == "reviewer_family_unknown"


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as s:
        s.add(Workspace(id=WS, name="Acme", slug="acme"))
        s.commit()
        yield s
    engine.dispose()


def _project(session: Session) -> uuid.UUID:
    project = Project(workspace_id=WS, name="Core", key="CORE")
    session.add(project)
    session.commit()
    return project.id


def test_the_reviewer_model_comes_from_the_role_configuration(session: Session) -> None:
    project_id = _project(session)
    with pytest.raises(ReviewerRefused) as unset:
        reviewer_model(session, workspace_id=WS, project_id=project_id)
    assert unset.value.reason == "reviewer_not_configured"  # the built-in default is a tier

    session.add(
        AgentRoleConfig(
            workspace_id=WS, role=AgentRole.REVIEWER, model_or_tier="senior", effort=Effort.HIGH
        )
    )
    session.commit()
    with pytest.raises(ReviewerRefused) as tier:
        reviewer_model(session, workspace_id=WS, project_id=project_id)
    assert tier.value.reason == "reviewer_not_configured"

    session.add(
        AgentRoleConfig(
            workspace_id=WS,
            project_id=project_id,
            role=AgentRole.REVIEWER,
            model_or_tier="gpt-6-astra",
            effort=Effort.HIGH,
        )
    )
    session.commit()
    assert reviewer_model(session, workspace_id=WS, project_id=project_id) == "gpt-6-astra"


def test_the_builders_model_is_the_one_its_environment_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("FORGE_MODEL_PROVIDER", "FORGE_MODEL_NAME", "FORGE_LOCAL_CLI_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    assert configured_model() is None

    monkeypatch.setenv("FORGE_MODEL_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("FORGE_MODEL_NAME", "gpt-6-astra")
    assert configured_model() == "gpt-6-astra"
