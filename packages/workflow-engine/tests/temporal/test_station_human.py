"""Human stations in ``forge.StationWorkflow`` (spec K §4.2, ADR-0020; #180).

Entering a human station opens its visit's gate (``forge.open_station_gate``) and
waits. Only ``forge.read_station_gate`` answers: the ``station_gate_changed`` Signal
just wakes the wait, which also wakes itself every 15 minutes and at the gate's
deadline, when a gate still pending expires. On the time-skipping server with a
scripted gate standing in for the worker's database reads.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta
from typing import Any

import pytest
from temporalio import activity

from scf_contracts import RunStatus, WorkflowRun
from scf_workflow.temporal.payloads import CleanupInput, StationGateInput, StationGateResult

from .conftest import Harness
from .test_station_replay import _replayer

pytestmark = pytest.mark.asyncio

REVISION = uuid.UUID("66666666-6666-6666-6666-666666666666")

REVIEWED = """\
workflow: reviewed
dsl_version: "2"
initial: review
terminals: {done: succeeded}
stations:
  review:
    kind: human
    gate: plan
    sla_seconds: SLA
    required_approvals: 2
    summary: "Review {{task.title}}"
transitions:
  - {from: review, to: done, when: approved}
"""


class Gate:
    """A scripted gate: ``open`` records the request; ``read`` answers ``decision``.

    ``decision`` is ``(status, actor)`` once someone decided; until then a read is
    ``pending``, or ``expired`` when the workflow says the deadline passed.
    """

    def __init__(self) -> None:
        self.id = uuid.uuid4()
        self.decision: tuple[str, str] | None = None
        self.opened: list[StationGateInput] = []
        self.reads: list[StationGateInput] = []
        #: When each read was scheduled, in workflow time.
        self.read_at: list[datetime] = []

    def open(self, inp: StationGateInput) -> StationGateResult:
        self.opened.append(inp)
        return StationGateResult(approval_request_id=self.id, status="pending")

    def read(self, inp: StationGateInput) -> StationGateResult:
        self.reads.append(inp)
        self.read_at.append(activity.info().current_attempt_scheduled_time)
        if self.decision is not None:
            status, actor = self.decision
            return StationGateResult(approval_request_id=self.id, status=status, actor=actor)
        return StationGateResult(self.id, "expired" if inp.expire else "pending")


def _harness(harness: Any, gate: Gate, **fns: Any) -> Harness:
    h: Harness = harness(open_station_gate_fn=gate.open, read_station_gate_fn=gate.read, **fns)
    return h


async def _start(h: Harness, *, sla: int = 86_400) -> WorkflowRun:
    return await h.engine.astart(
        uuid.uuid4(),
        "reviewed",
        definition_revision_id=REVISION,
        dsl_yaml=REVIEWED.replace("SLA", str(sla)),
        task={"key": "T-9", "title": "Fix login", "description": ""},
    )


async def _until(check: Any) -> None:
    for _ in range(300):
        if check():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition never held")


def _waiting(h: Harness) -> bool:
    return any(r.status == "waiting" for r in h.store.reports)


async def test_a_human_station_opens_its_gate_and_takes_the_decision_it_reads(
    harness: Any, time_skip_env: Any
) -> None:
    gate = Gate()
    h = _harness(harness, gate)
    async with h.worker():
        before = await time_skip_env.get_current_time()
        run = await _start(h)
        await _until(lambda: _waiting(h))
        gate.decision = ("approved", "user:decider")
        assert run.id is not None
        await h.engine.astation_gate_changed(run.id, gate.id)
        assert await h.handle(run).result() == "done"
        history = await h.handle(run).fetch_history()

    (opened,) = gate.opened
    assert (opened.workflow_run_id, opened.workspace_id) == (run.id, h.workspace_id)
    assert (opened.state, opened.visit, opened.gate) == ("review", 1, "plan")
    assert opened.required_approvals == 2
    assert opened.summary == "Review Fix login"
    assert timedelta(days=1) <= opened.expires_at - before < timedelta(days=1, minutes=1)
    # The read names the same visit; the outcome is what it read, never the Signal's id.
    assert [(r.state, r.visit, r.expire) for r in gate.reads] == [("review", 1, False)]
    reports = [(r.state, r.visit, r.status, r.outcome) for r in h.store.reports]
    assert reports == [
        ("review", 1, "running", None),
        ("review", 1, "waiting", None),
        ("review", 1, "succeeded", "approved"),
    ]
    assert h.store.reports[-1].approval_request_id == gate.id
    (moved,) = h.transitions(run)
    assert (moved["from_state"], moved["to_state"], moved["event"]) == (
        "review",
        "done",
        "approved",
    )
    assert moved["actor"] == "user:decider"  # the deciding user, read from the database
    assert h.engine.get_run(run.id).status is RunStatus.SUCCEEDED
    await _replayer().replay_workflow(history)


async def test_a_hint_with_no_recorded_decision_or_a_forged_id_leaves_the_run_waiting(
    harness: Any,
) -> None:
    gate = Gate()
    cleanups: list[CleanupInput] = []

    def cleanup(inp: CleanupInput) -> bool:
        cleanups.append(inp)
        return True

    h = _harness(harness, gate, cleanup_fn=cleanup)
    async with h.worker():
        run = await _start(h)
        assert run.id is not None
        await _until(lambda: _waiting(h))
        await h.engine.astation_gate_changed(run.id, gate.id)
        await _until(lambda: len(gate.reads) == 1)
        await h.engine.astation_gate_changed(run.id, uuid.uuid4())  # another run's request
        await _until(lambda: len(gate.reads) == 2)
        await asyncio.sleep(0.2)
        assert h.transitions(run) == []
        assert h.engine.get_run(run.id).current_state == "review"
        assert h.store.reports[-1].status == "waiting"

        # Criterion 7: a cancel while waiting cleans up as the canceller (the worker's
        # cleanup expires the open gate with reason ``run_cancelled``).
        await h.engine.atransition(run.id, "cancel", actor="user:abc", reason="stop")
        assert await h.handle(run).result() == "cancelled"

    assert [(c.actor, c.workflow_run_id) for c in cleanups] == [("user:abc", run.id)]
    last = h.store.reports[-1]
    assert (last.status, last.approval_request_id) == ("cancelled", gate.id)
    assert h.engine.get_run(run.id).status is RunStatus.CANCELLED


async def test_with_no_hint_the_periodic_wake_finds_the_decision_within_15_minutes(
    harness: Any,
) -> None:
    gate = Gate()
    gate.decision = ("approved", "user:decider")  # recorded, but no Signal is ever sent
    h = _harness(harness, gate)
    async with h.worker():
        run = await _start(h)
        assert await h.handle(run).result() == "done"  # skips time to the next timer

    (opened,) = gate.opened
    entered = opened.expires_at - timedelta(days=1)
    (read_at,) = gate.read_at
    assert timedelta(minutes=15) <= read_at - entered < timedelta(minutes=16)
    assert h.transitions(run)[-1]["actor"] == "user:decider"


async def test_a_gate_still_pending_at_its_deadline_expires_and_parks_the_run(
    harness: Any, time_skip_env: Any
) -> None:
    gate = Gate()
    h = _harness(harness, gate)
    async with h.worker():
        run = await _start(h, sla=1_800)
        await _until(lambda: _waiting(h))
        await time_skip_env.sleep(timedelta(minutes=31))
        await h.wait_awaiting(run, {"resume"})

    # Woken at 15 minutes (still pending), then at the deadline, when it may expire.
    expires = [r.expire for r in gate.reads]
    assert expires[0] is False and expires[-1] is True and expires.count(True) == 1
    assert run.id is not None
    parked = h.engine.get_run(run.id)
    assert (parked.current_state, parked.status) == ("review", RunStatus.ESCALATED)
    assert parked.context["parked"] == {"state": "review", "visit": 1, "reason": "expired"}
    assert h.transitions(run)[-1]["actor"] == "system"


async def test_an_approval_whose_attestation_is_missing_parks_the_run_naming_it(
    harness: Any,
) -> None:
    """A ``pr`` gate approved without a recorded attestation never advances."""
    gate = Gate()
    gate.decision = ("attestation_missing", "user:decider")
    h = _harness(harness, gate)
    async with h.worker():
        run = await _start(h)
        await _until(lambda: _waiting(h))
        assert run.id is not None
        await h.engine.astation_gate_changed(run.id, gate.id)
        await h.wait_awaiting(run, {"resume"})
        parked = h.engine.get_run(run.id)

    assert (parked.current_state, parked.status) == ("review", RunStatus.ESCALATED)
    reason = "attestation_missing"
    assert parked.context["parked"] == {"state": "review", "visit": 1, "reason": reason}
    assert h.store.reports[-1].status == "parked"
