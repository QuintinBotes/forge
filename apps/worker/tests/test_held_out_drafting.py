"""A verifier agent drafts held-out scenarios before the spec gate (#258, part 1)."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import StaticPool, create_engine, select
from sqlalchemy.orm import Session, sessionmaker

import scf_worker.held_out_drafting as drafting_module
from scf_agent.testing import ScriptedModelClient, finish_response
from scf_contracts.orchestration_config import AgentRole, Effort
from scf_db.base import Base
from scf_db.models import AgentRun, AgentRunEvent, Project, Task, WorkflowRun, Workspace
from scf_db.models.audit import AuditLog
from scf_db.models.held_out import HeldOutScenario
from scf_db.models.role_config import AgentRoleConfig
from scf_worker.held_out_drafting import (
    HomogeneousVerifierError,
    draft_scenarios_service,
    verifier_tools,
)
from scf_workflow.temporal.payloads import DraftScenariosInput

WS = uuid.UUID("00000000-0000-0000-0000-000000000258")
MARKER = "HELD-OUT-MARKER-7f3a"
SCENARIO = {
    "title": "A refund settles in the original currency",
    "files": [{"path": "tests/held_out/test_refund.py", "content": f"# {MARKER}\nassert True\n"}],
    "command": f"echo {MARKER} && pytest tests/held_out",
}


@pytest.fixture
def factory() -> Iterator[sessionmaker[Session]]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    engine.dispose()


@pytest.fixture(autouse=True)
def builder_on_anthropic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(drafting_module, "configured_model", lambda: "claude-opus-5-5")


def _seed(factory: sessionmaker[Session], reviewer_model: str | None = "gpt-6-astra") -> uuid.UUID:
    with factory() as s:
        s.add(Workspace(id=WS, name="Acme", slug="acme"))
        s.flush()
        project = Project(workspace_id=WS, name="Core", key="CORE")
        s.add(project)
        s.flush()
        task = Task(
            workspace_id=WS,
            project_id=project.id,
            key="CORE-258",
            title="Refunds",
            description="Refund a payment.",
            acceptance_criteria=[{"id": "AC-1", "text": "A refund settles in its currency"}],
        )
        s.add(task)
        s.flush()
        run = WorkflowRun(workspace_id=WS, task_id=task.id, engine_backend="temporal")
        s.add(run)
        if reviewer_model is not None:
            s.add(
                AgentRoleConfig(
                    workspace_id=WS,
                    role=AgentRole.REVIEWER,
                    model_or_tier=reviewer_model,
                    effort=Effort.HIGH,
                )
            )
        s.commit()
        return run.id


class Verifier:
    """The verifier's model clients, replying ``reply``."""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.clients: list[ScriptedModelClient] = []

    def __call__(self, _model: str) -> ScriptedModelClient:
        self.clients.append(ScriptedModelClient([finish_response(self.reply, confidence=0.9)]))
        return self.clients[-1]


def _draft(factory: sessionmaker[Session], run_id: uuid.UUID, verifier: Verifier) -> Any:
    with factory() as s:
        run = s.get(WorkflowRun, run_id)
        assert run is not None
        task_id = run.task_id
    inp = DraftScenariosInput(
        workflow_run_id=run_id, workspace_id=WS, task_id=task_id, idempotency_key="d0"
    )
    return draft_scenarios_service(factory, verifier=verifier)(inp)


def _context(factory: sessionmaker[Session], run_id: uuid.UUID) -> dict[str, Any]:
    with factory() as s:
        run = s.get(WorkflowRun, run_id)
        assert run is not None
        return dict(run.context)


def test_the_verifier_drafts_scenarios_from_the_spec(factory: sessionmaker[Session]) -> None:
    """Criteria 1 and 2: a verifier run of its own role, on another family, given the
    spec and no tools, whose scenarios are stored as drafts it is named the drafter of."""
    run_id = _seed(factory)
    verifier = Verifier(json.dumps({"scenarios": [SCENARIO]}))

    result = _draft(factory, run_id, verifier)

    assert result.reason is None and len(result.scenario_ids) == 1
    [request] = verifier.clients[0].requests
    assert request.system is not None and "# Spec under review" in request.system
    assert "AC-1: A refund settles in its currency" in request.system
    assert {t["name"] for t in request.tools} <= {"finish"}
    assert verifier_tools().names() == []
    with factory() as s:
        [scenario] = s.scalars(select(HeldOutScenario)).all()
        [verifier_run] = s.scalars(select(AgentRun)).all()
    assert str(scenario.id) == result.scenario_ids[0] and scenario.state == "draft"
    assert scenario.drafted_by == f"agent:{verifier_run.id}/gpt-6-astra"
    assert scenario.drafted_by_user_id is None
    assert (verifier_run.role, verifier_run.model) == ("verifier", "gpt-6-astra")
    assert verifier_run.workflow_run_id == run_id


def test_scenario_content_reaches_no_record_of_the_verifier(
    factory: sessionmaker[Session],
) -> None:
    """Criterion 6 (the verifier's side): its run, steps, events, the workflow run and
    the audit log hold scenario ids and titles only."""
    run_id = _seed(factory)

    _draft(factory, run_id, Verifier(json.dumps({"scenarios": [SCENARIO]})))

    with factory() as s:
        records = [
            *(
                json.dumps([r.inputs, r.output, r.steps], default=str)
                for r in s.scalars(select(AgentRun))
            ),
            *(json.dumps(e.payload, default=str) for e in s.scalars(select(AgentRunEvent))),
            json.dumps(_context(factory, run_id), default=str),
            *(json.dumps(a.details, default=str) for a in s.scalars(select(AuditLog))),
        ]
    assert records and not any(MARKER in record for record in records)


def test_a_verifier_of_the_builders_family_is_refused_before_any_model_call(
    factory: sessionmaker[Session],
) -> None:
    """Criterion 3: the reviewer's shared family comparison, under its own named error."""
    run_id = _seed(factory, reviewer_model="claude-sonnet-5-5")
    verifier = Verifier("{}")

    result = _draft(factory, run_id, verifier)

    assert result.reason == "verifier_same_family" and result.scenario_ids == []
    assert verifier.clients == []
    assert HomogeneousVerifierError("x").reason == "verifier_same_family"


def test_with_no_verifier_nothing_is_drafted(factory: sessionmaker[Session]) -> None:
    """Criterion 4."""
    run_id = _seed(factory, reviewer_model=None)

    result = _draft(factory, run_id, Verifier("{}"))

    assert result.reason == "verifier_not_configured" and result.scenario_ids == []
    assert _context(factory, run_id)["held_out"] == "verifier_not_configured"
    with factory() as s:
        assert s.scalars(select(HeldOutScenario)).all() == []


def test_output_that_does_not_parse_drafts_nothing(factory: sessionmaker[Session]) -> None:
    """Criterion 5."""
    run_id = _seed(factory)

    result = _draft(factory, run_id, Verifier("Here are some ideas for tests."))

    assert result.reason == "unparseable_scenarios" and result.scenario_ids == []
    assert _context(factory, run_id)["held_out"] == "unparseable_scenarios"


def test_a_scenario_the_store_refuses_is_dropped_and_the_others_kept(
    factory: sessionmaker[Session],
) -> None:
    """Criterion 5: a path that leaves the checkout is dropped with its reason."""
    run_id = _seed(factory)
    escaping = {**SCENARIO, "title": "Escapes", "files": [{"path": "../x.py", "content": "x"}]}
    verifier = Verifier(json.dumps({"scenarios": [escaping, SCENARIO]}))

    result = _draft(factory, run_id, verifier)

    assert len(result.scenario_ids) == 1
    with factory() as s:
        [verifier_run] = s.scalars(select(AgentRun)).all()
    [dropped] = verifier_run.output["dropped"]
    assert dropped["title"] == "Escapes" and "checkout" in dropped["reason"]
    assert _context(factory, run_id)["held_out_drafting"]["d0"]["dropped"] == 1


def test_a_retried_step_returns_what_it_drafted(factory: sessionmaker[Session]) -> None:
    run_id = _seed(factory)
    verifier = Verifier(json.dumps({"scenarios": [SCENARIO]}))

    first = _draft(factory, run_id, verifier)
    again = _draft(factory, run_id, verifier)

    assert again.scenario_ids == first.scenario_ids
    assert len(verifier.clients) == 1
    with factory() as s:
        assert len(s.scalars(select(HeldOutScenario)).all()) == 1


class FailingClient:
    """A provider that keeps refusing (a 429 after its own retries)."""

    def complete(self, request: Any) -> Any:
        from scf_agent.providers import ModelClientError

        raise ModelClientError("429 too many requests")


@pytest.mark.parametrize(
    "client",
    [
        FailingClient(),
        ScriptedModelClient(
            [finish_response(json.dumps({"scenarios": [SCENARIO]}), needs_human=True)]
        ),
    ],
    ids=["provider error", "escalated"],
)
def test_a_verifier_run_that_does_not_succeed_drafts_nothing_and_the_gate_opens(
    factory: sessionmaker[Session], client: Any
) -> None:
    """A failed verifier is recorded as one, and does not fail the run (#258 review)."""
    run_id = _seed(factory)

    with factory() as s:
        run = s.get(WorkflowRun, run_id)
        assert run is not None
        task_id = run.task_id
    inp = DraftScenariosInput(
        workflow_run_id=run_id, workspace_id=WS, task_id=task_id, idempotency_key="d0"
    )
    result = draft_scenarios_service(factory, verifier=lambda _m: client)(inp)

    assert (result.reason, result.scenario_ids) == ("verifier_run_failed", [])
    assert _context(factory, run_id)["held_out"] == "verifier_run_failed"
    with factory() as s:
        [verifier_run] = s.scalars(select(AgentRun)).all()
        assert verifier_run.status.value in {"failed", "escalated"}
        assert s.scalars(select(HeldOutScenario)).all() == []


def test_parsed_output_with_nothing_to_store_says_so(factory: sessionmaker[Session]) -> None:
    run_id = _seed(factory)

    result = _draft(factory, run_id, Verifier(json.dumps({"scenarios": []})))

    assert result.reason == "no_scenarios_drafted"
    assert _context(factory, run_id)["held_out"] == "no_scenarios_drafted"


def test_a_scenario_with_a_nul_is_dropped_and_the_others_kept(
    factory: sessionmaker[Session],
) -> None:
    """Postgres refuses a NUL in JSON or text, and would refuse the whole batch."""
    run_id = _seed(factory)
    nul = {**SCENARIO, "title": "Nul", "command": "echo \x00"}

    result = _draft(factory, run_id, Verifier(json.dumps({"scenarios": [nul, SCENARIO]})))

    assert len(result.scenario_ids) == 1
    assert _context(factory, run_id)["held_out_drafting"]["d0"]["dropped"] == 1


def test_each_drafted_scenario_is_audited_as_the_verifiers(
    factory: sessionmaker[Session],
) -> None:
    run_id = _seed(factory)

    result = _draft(factory, run_id, Verifier(json.dumps({"scenarios": [SCENARIO]})))

    with factory() as s:
        [created] = s.scalars(select(AuditLog).where(AuditLog.action == "held_out.created")).all()
        [verifier_run] = s.scalars(select(AgentRun)).all()
    assert created.details["scenario_id"] == result.scenario_ids[0]
    assert created.actor_type == "agent"
    assert created.details["agent_run_id"] == str(verifier_run.id)


def test_a_spec_with_nothing_to_draft_from_is_not_drafted(
    factory: sessionmaker[Session],
) -> None:
    run_id = _seed(factory)
    with factory() as s:
        run = s.get(WorkflowRun, run_id)
        assert run is not None
        task = s.get(Task, run.task_id)
        assert task is not None
        task.description = None
        task.acceptance_criteria = []
        s.commit()
    verifier = Verifier("{}")

    result = _draft(factory, run_id, verifier)

    assert result.reason == "no_spec_to_draft_from" and verifier.clients == []


def test_the_builders_run_is_never_the_verifiers(factory: sessionmaker[Session]) -> None:
    """Checks and the pull request read the builder's run, never the verifier's."""
    from scf_worker.temporal_services import _latest_agent_run

    run_id = _seed(factory)
    _draft(factory, run_id, Verifier(json.dumps({"scenarios": [SCENARIO]})))

    with factory() as s:
        assert s.scalars(select(AgentRun)).one().role == "verifier"
        assert _latest_agent_run(s, run_id, WS) is None


def test_drafting_again_withdraws_the_earlier_specs_scenarios(
    factory: sessionmaker[Session],
) -> None:
    """#288 criterion 3: when the spec gate returns changes the verifier drafts again
    from the revised text, and what it drafted for the earlier text is withdrawn,
    whatever its state; the withdrawal is audited with the run, never the content."""
    run_id = _seed(factory)
    first = _draft(factory, run_id, Verifier(json.dumps({"scenarios": [SCENARIO, SCENARIO]})))
    with factory() as s:
        approved = s.get(HeldOutScenario, uuid.UUID(first.scenario_ids[0]))
        assert approved is not None and approved.workflow_run_id == run_id
        approved.state = "approved"
        run = s.get(WorkflowRun, run_id)
        assert run is not None
        task = s.get(Task, run.task_id)
        assert task is not None
        task.description = "Refund a payment, in part or in full."
        task_id = task.id
        s.commit()
    verifier = Verifier(json.dumps({"scenarios": [{**SCENARIO, "title": "A partial refund"}]}))

    again = draft_scenarios_service(factory, verifier=verifier)(
        DraftScenariosInput(
            workflow_run_id=run_id, workspace_id=WS, task_id=task_id, idempotency_key="d7"
        )
    )

    [request] = verifier.clients[0].requests
    assert request.system is not None and "in part or in full" in request.system
    with factory() as s:
        states = {str(r.id): r.state for r in s.scalars(select(HeldOutScenario))}
        withdrawals = list(
            s.scalars(select(AuditLog).where(AuditLog.action == "held_out.withdrawn"))
        )
    assert {states[i] for i in first.scenario_ids} == {"withdrawn"}
    assert [states[i] for i in again.scenario_ids] == ["draft"]
    assert sorted(w.details["scenario_id"] for w in withdrawals) == sorted(first.scenario_ids)
    assert all(w.details["workflow_run_id"] == str(run_id) for w in withdrawals)
    assert not any(MARKER in json.dumps(w.details) for w in withdrawals)


def test_drafting_again_withdraws_even_when_nothing_new_is_drafted(
    factory: sessionmaker[Session],
) -> None:
    """A redraft that drafts nothing still withdraws, and says why nothing was drafted."""
    run_id = _seed(factory)
    first = _draft(factory, run_id, Verifier(json.dumps({"scenarios": [SCENARIO]})))
    assert "held_out" not in _context(factory, run_id)
    with factory() as s:
        run = s.get(WorkflowRun, run_id)
        assert run is not None
        task_id = run.task_id

    again = draft_scenarios_service(factory, verifier=Verifier("not json"))(
        DraftScenariosInput(
            workflow_run_id=run_id, workspace_id=WS, task_id=task_id, idempotency_key="d9"
        )
    )

    assert again.reason == "unparseable_scenarios"
    assert _context(factory, run_id)["held_out"] == "unparseable_scenarios"
    with factory() as s:
        scenario = s.get(HeldOutScenario, uuid.UUID(first.scenario_ids[0]))
        assert scenario is not None and scenario.state == "withdrawn"
