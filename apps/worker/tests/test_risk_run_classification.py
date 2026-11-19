"""An agent run's change is classified and recorded at close-out (#193 criteria 4 and 5).

A local repository whose default branch carries the policy, a run branch with the
agent's commit, and the run closed out without delivery, as a workflow run is.
"""

from __future__ import annotations

import logging
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from sqlalchemy import StaticPool, create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

import scf_worker.agent_runner as agent_runner_module
import scf_worker.risk_classification as risk_module
from scf_contracts import AgentObjective, AgentRunResult, RiskClass, RiskEvaluation, RunStatus
from scf_db.base import Base
from scf_db.models import AgentRun, RiskClassification, TraceabilityCriterionLink, Workspace
from scf_db.models.enums import RunStatus as DbRunStatus
from scf_db.risk_repository import RiskRepository
from scf_policy.loader import POLICY_RELATIVE_PATH
from scf_worker.agent_runner import PreparedRun, close_out_run

WS = uuid.UUID("00000000-0000-0000-0000-000000000193")
WORKFLOW_RUN = uuid.UUID("00000000-0000-0000-0000-0000000001f3")
SECRET = "ghp_" + "s" * 36
RISK_RULES = {
    "enforcement": "shadow",
    "default": "low",
    "derive": [
        {"id": "docs", "paths": ["docs/**"], "class": "minimal"},
        {"id": "auth", "paths": ["app/auth/**"], "class": "high"},
    ],
}


@pytest.fixture
def factory(monkeypatch: pytest.MonkeyPatch) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    monkeypatch.setattr(agent_runner_module, "create_session_factory", lambda: session_factory)
    with session_factory() as s:
        s.add(Workspace(id=WS, name="Acme", slug="acme"))
        s.commit()
    yield session_factory
    engine.dispose()


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repo(tmp_path: Path, risk_rules: dict[str, Any] | None) -> tuple[Path, str, str]:
    """A repository whose default branch has the policy; the run branch adds two files."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    policy: dict[str, Any] = {"repo_id": "acme/app"}
    if risk_rules is not None:
        policy["risk_rules"] = risk_rules
    (repo / POLICY_RELATIVE_PATH).parent.mkdir()
    (repo / POLICY_RELATIVE_PATH).write_text(yaml.safe_dump(policy))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "-b", "run")
    (repo / "app" / "auth").mkdir(parents=True)
    (repo / "app" / "auth" / "login.py").write_text("a = 1\nb = 2\n")
    (repo / "notes").mkdir()
    (repo / "notes" / f"{SECRET}.txt").write_text("x\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "change")
    head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")  # the default branch stays checked out
    return repo, base, head


def _run(
    factory: sessionmaker[Session],
    repo: Path,
    base: str,
    head: str,
    *,
    task_id: uuid.UUID | None = None,
) -> tuple[PreparedRun, AgentRunResult]:
    run_id = uuid.uuid4()
    context = {
        "agent_run_id": str(run_id),
        "workspace_id": str(WS),
        "workflow_run_id": str(WORKFLOW_RUN),
        "repo_path": str(repo),
    }
    objective = AgentObjective(objective="Add login", task_id=task_id, context=context)
    with factory() as s:
        s.add(AgentRun(id=run_id, workspace_id=WS, status=DbRunStatus.PENDING, inputs={}))
        s.commit()
    result = AgentRunResult(
        run_id=run_id,
        status=RunStatus.SUCCEEDED,
        summary="done",
        artifacts={"branch": "run", "base_commit": base, "head_commit": head},
    )
    return PreparedRun(objective, run_id, WS, persisted_row=True, events=None), result


def _rows(factory: sessionmaker[Session]) -> list[RiskClassification]:
    with factory() as s:
        return list(s.scalars(select(RiskClassification)))


def _closed_out_today(
    factory: sessionmaker[Session], repo: Path, base: str, head: str, mp: pytest.MonkeyPatch
) -> dict[str, Any]:
    """The result a close-out leaves with no risk classification at all."""
    with mp.context() as patched:
        patched.setattr(agent_runner_module, "classify_change", lambda *_a, **_kw: None)
        run, result = _run(factory, repo, base, head)
        close_out_run(run, result, deliver=False)
    return result.model_dump(exclude={"run_id"})


def test_a_run_with_commits_is_classified_once(
    factory: sessionmaker[Session], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, base, head = _repo(tmp_path, RISK_RULES)
    today = _closed_out_today(factory, repo, base, head, monkeypatch)
    run, result = _run(factory, repo, base, head)

    close_out_run(run, result, deliver=False)

    [row] = _rows(factory)
    assert (row.workspace_id, row.commit_sha) == (WS, head)
    assert (row.agent_run_id, row.workflow_run_id) == (run.run_id, WORKFLOW_RUN)
    assert row.pr_numbers == [] and row.spec_key is None
    assert row.derived_class == row.final_class == RiskClass.HIGH
    assert row.declared_class is None
    assert row.matched_rule_ids == ["auth"]
    assert (row.enforcement_mode, row.fallback_applied) == ("shadow", False)
    assert row.signals["lines_added"] == 3 and row.signals["available"] is True
    assert "app/auth/login.py" in row.signals["paths"]
    assert SECRET not in str(row.signals)  # the snapshot is redacted
    assert len(row.signals["paths"]) == 2
    # Classification adds nothing to the run's result or its recorded outcome.
    assert result.model_dump(exclude={"run_id"}) == today
    with factory() as s:
        stored = s.get(AgentRun, run.run_id)
        assert stored is not None and stored.status is DbRunStatus.SUCCEEDED


def test_a_retried_close_out_records_one_classification(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    repo, base, head = _repo(tmp_path, RISK_RULES)
    run, result = _run(factory, repo, base, head)

    close_out_run(run, result, deliver=False)
    close_out_run(run, result, deliver=False, reuse_existing=True)  # #239 retries close-out

    [row] = _rows(factory)
    # The database holds it too: one classification per agent run.
    with factory() as s, pytest.raises(IntegrityError):
        RiskRepository(s).record_classification(
            WS,
            RiskEvaluation(derived_class=RiskClass.LOW, final_class=RiskClass.LOW),
            commit_sha=head,
            signals={},
            enforcement_mode="shadow",
            agent_run_id=row.agent_run_id,
        )


def test_the_change_identity_carries_the_tasks_spec_and_pull_requests(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """As the attestation reads them: the traceability links that name the run's task."""
    repo, base, head = _repo(tmp_path, RISK_RULES)
    task_id = uuid.uuid4()
    with factory() as s:
        s.add(
            TraceabilityCriterionLink(
                workspace_id=WS,
                project_id=uuid.uuid4(),
                spec_id=uuid.uuid4(),
                spec_key="SPEC-7",
                criterion_ext_id="AC-1",
                criterion_text="logins are rate limited",
                status="satisfied",
                task_ids=[str(task_id)],
                pr_numbers=[12, 9],
                current_spec_version=3,
            )
        )
        s.commit()
    workflow_run, delivered = _run(factory, repo, base, head, task_id=task_id)
    celery_run, opened = _run(factory, repo, base, head, task_id=task_id)
    opened.artifacts["pull_request"] = {"status": "opened", "number": 31, "url": "u"}

    close_out_run(workflow_run, delivered, deliver=False)
    close_out_run(celery_run, opened, deliver=False)

    rows = {row.agent_run_id: row for row in _rows(factory)}
    durable = rows[workflow_run.run_id]
    assert (durable.spec_key, durable.spec_version, durable.pr_numbers) == ("SPEC-7", 3, [9, 12])
    assert rows[celery_run.run_id].pr_numbers == [31]  # the pull request this close-out opened


def test_an_unreadable_diff_records_the_fallback_class(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    repo, base, _ = _repo(tmp_path, RISK_RULES)
    run, result = _run(factory, repo, base, "f" * 40)

    close_out_run(run, result, deliver=False)

    [row] = _rows(factory)
    assert row.fallback_applied is True
    assert row.final_class == RiskClass.CRITICAL  # the rules' default fallback
    assert row.signals["available"] is False and row.signals["paths"] == []


def test_a_policy_without_risk_rules_records_nothing_and_changes_nothing(
    factory: sessionmaker[Session], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, base, head = _repo(tmp_path, None)
    today = _closed_out_today(factory, repo, base, head, monkeypatch)
    run, result = _run(factory, repo, base, head)

    close_out_run(run, result, deliver=False)

    assert _rows(factory) == []
    assert result.model_dump(exclude={"run_id"}) == today


def test_a_run_without_commits_records_nothing(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    repo, base, _ = _repo(tmp_path, RISK_RULES)
    run, result = _run(factory, repo, base, base)

    close_out_run(run, result, deliver=False)

    assert _rows(factory) == []


def _fail_storing(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: Any, **_kw: Any) -> None:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(RiskRepository, "record_classification", boom)


def _fail_classifying(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: Any, **_kw: Any) -> None:
        raise RuntimeError("classifier broke")

    monkeypatch.setattr(risk_module, "derive", boom)


@pytest.mark.parametrize("fail", [_fail_storing, _fail_classifying])
def test_a_failure_while_classifying_or_storing_leaves_the_run_as_it_was(
    factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    fail: Any,
) -> None:
    repo, base, head = _repo(tmp_path, RISK_RULES)
    today = _closed_out_today(factory, repo, base, head, monkeypatch)
    run, result = _run(factory, repo, base, head)
    fail(monkeypatch)

    with caplog.at_level(logging.ERROR):
        close_out_run(run, result, deliver=False)

    assert _rows(factory) == []
    assert result.model_dump(exclude={"run_id"}) == today
    with factory() as s:
        stored = s.get(AgentRun, run.run_id)
        assert stored is not None and stored.status is DbRunStatus.SUCCEEDED
    assert any("could not classify" in r.getMessage() for r in caplog.records)
