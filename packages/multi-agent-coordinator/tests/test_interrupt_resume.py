"""HITL interrupt + resume across nesting (AC 16, 19 replay)."""

from __future__ import annotations

from pathlib import Path

from _helpers import AgentScript, ScriptingHub, make_objective, obj_parent

from forge_contracts import RunStatus, SubagentRules
from forge_coordinator import HumanResumeInput


def test_child_interrupt_then_resume_only_reruns_child(
    tmp_git_repo: Path, hub: ScriptingHub, sink, make_supervisor
) -> None:
    rules = SubagentRules(
        allow_subagents=True, allowed_roles=["researcher", "implementer"], max_parallel=1
    )
    hub.set("researcher", AgentScript(summary="research done", confidence=0.9))
    hub.set(
        "implementer",
        AgentScript(needs_human=True, confidence=0.2),  # first attempt interrupts
        AgentScript(files=[("app/done.py", "D=1\n")], confidence=0.9),  # resume succeeds
    )
    obj = make_objective(
        tmp_git_repo,
        rules=rules,
        pattern="dynamic_handoff",
        context_extra={
            "handoff_plan": [
                {"role": "researcher", "objective": "research"},
                {"role": "implementer", "objective": "build", "depends_on": [0]},
            ]
        },
    )
    sup = make_supervisor()
    first = sup.run(obj)
    assert first.needs_human is True
    assert first.artifacts["needs_human_reason"].startswith("subagent_awaiting_input")
    assert len(hub.calls_for("researcher")) == 1
    assert len(hub.calls_for("implementer")) == 1

    rows = {r["assignment_id"]: r for r in sink.rows_for_parent(obj_parent(obj))}
    researcher_row = rows["sa-researcher-1"]
    researcher_started_at = researcher_row["started_at"]
    assert researcher_row["status"] is RunStatus.SUCCEEDED

    resumed = sup.resume(obj_parent(obj), HumanResumeInput(decision="approve"))
    assert resumed.status is RunStatus.SUCCEEDED
    assert resumed.needs_human is False
    # Researcher (already complete) is NOT re-run; only the interrupted child reran.
    assert len(hub.calls_for("researcher")) == 1
    assert len(hub.calls_for("implementer")) == 2
    rows_after = {r["assignment_id"]: r for r in sink.rows_for_parent(obj_parent(obj))}
    assert rows_after["sa-researcher-1"]["started_at"] == researcher_started_at
    # Still exactly one researcher row + one implementer row (no duplicate rows).
    assert sum(1 for r in rows_after.values() if r["role"] == "researcher") == 1
    assert sum(1 for r in rows_after.values() if r["role"] == "implementer") == 1
