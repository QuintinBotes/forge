"""Replay of ``forge.StationWorkflow`` histories (spec K §4.2 "Versioning").

``fixtures/station_visit_limit_resume.json`` is a line run recorded on the
time-skipping server: a service station, an agent station, checks that fail and
send the run back, a ``visit_limit`` park, a resume, and success. A replay runs
no activity and opens no connection, so a clean replay shows the workflow reads
nothing but its history (never the database), and that later interpreter changes
keep the command order of recorded runs. The canary shows an order change is caught.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Any

import pytest
from temporalio import workflow
from temporalio.client import WorkflowHistory
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Replayer

from scf_workflow.temporal.payloads import (
    AgentRunResultDTO,
    ServiceStationInput,
    ServiceStationResult,
    StationParams,
)
from scf_workflow.temporal.station_workflow import StationWorkflow
from scf_workflow.temporal.worker import feature_workflow_runner

pytestmark = pytest.mark.asyncio

FIXTURE = Path(__file__).parent / "fixtures" / "station_visit_limit_resume.json"

LINE = """\
workflow: replayed_line
dsl_version: "2"
initial: prepare
terminals: {done: succeeded}
budget: {max_tokens: 1000}
stations:
  prepare: {kind: service, service: forge.prepare_worktree}
  implement:
    kind: agent
    role: implementer
    skill_profile: backend-tdd
    context_template: implementation
    objective: "Fix {{task.title}}"
    budget: {max_tokens: 400}
  check: {kind: service, service: forge.run_checks, max_visits: 1}
transitions:
  - {from: prepare, to: implement, when: succeeded}
  - {from: implement, to: check, when: succeeded}
  - {from: check, to: done, when: succeeded}
  - {from: check, to: implement, when: checks_failed}
"""


async def record_history(harness: Any) -> WorkflowHistory:
    """Run the fixture's scenario; also how the fixture was recorded."""
    checks = iter(["checks_failed", "succeeded"])

    def check(_inp: ServiceStationInput) -> ServiceStationResult:
        return ServiceStationResult(next(checks))

    h = harness(
        run_agent_fn=lambda _inp: AgentRunResultDTO(
            agent_run_id=uuid.uuid4(), status="succeeded", tokens_used=50
        ),
        service_bodies={
            "forge.prepare_worktree": lambda _inp: ServiceStationResult(),
            "forge.run_checks": check,
        },
    )
    async with h.worker():
        run = await h.engine.astart(
            uuid.uuid4(),
            "replayed_line",
            definition_revision_id=uuid.uuid4(),
            dsl_yaml=LINE,
            task={"key": "T-1", "title": "Fix login", "description": ""},
        )
        await h.wait_awaiting(run, {"resume"})  # check's second entry is past its limit
        await h.engine.atransition(run.id, "resume", actor="user:replay")
        assert await h.handle(run).result() == "done"
        history: WorkflowHistory = await h.handle(run).fetch_history()
        return history


def _replayer(*workflows: type) -> Replayer:
    return Replayer(
        workflows=list(workflows) or [StationWorkflow],
        workflow_runner=feature_workflow_runner(),
        data_converter=pydantic_data_converter,
    )


def _frozen() -> WorkflowHistory:
    return WorkflowHistory.from_json("wf-frozen-station-run", FIXTURE.read_text())


async def test_a_recorded_station_run_replays() -> None:
    await _replayer().replay_workflow(_frozen())


async def test_a_live_station_run_replays(harness: Any) -> None:
    await _replayer().replay_workflow(await record_history(harness))


def test_the_station_workflow_module_reads_no_database_or_clock() -> None:
    source = Path(StationWorkflow.__module__.replace(".", "/") + ".py")
    text = (Path(__file__).parents[2] / source).read_text()
    for forbidden in ("sqlalchemy", "scf_db", "import os", "import time", "datetime.now"):
        assert not re.search(rf"\b{re.escape(forbidden)}\b", text), forbidden


@workflow.defn(name="forge.StationWorkflow")
class _ReorderedStationWorkflow:
    """Same name, one activity before the first visit: a command-order change."""

    @workflow.run
    async def run(self, params: StationParams) -> str:  # pragma: no cover - replay only
        from datetime import timedelta

        await workflow.execute_activity(
            "forge.pause_and_notify", arg=None, start_to_close_timeout=timedelta(seconds=5)
        )
        return "done"

    @workflow.update
    async def submit_event(self, event: Any) -> str:  # pragma: no cover
        return "done"

    @workflow.signal
    async def cancel_run(
        self, reason: str = "cancelled", actor: str | None = None
    ) -> None:  # pragma: no cover
        return None


async def test_replay_catches_a_station_command_order_change() -> None:
    with pytest.raises(workflow.NondeterminismError):
        await _replayer(_ReorderedStationWorkflow).replay_workflow(_frozen())
