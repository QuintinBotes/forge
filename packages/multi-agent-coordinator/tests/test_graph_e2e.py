"""End-to-end supervisor graph runs with scripted subagents (AC 3,5,7,8,9,10,11,
12,14,15,17,18)."""

from __future__ import annotations

from pathlib import Path

from _helpers import AgentScript, ScriptingHub, make_objective, obj_parent

from forge_contracts import AcceptanceCriterion, RunStatus, SubAgentPolicy, SubagentRules


def test_maker_checker_happy_path(
    tmp_git_repo: Path, hub: ScriptingHub, sink, make_supervisor, allow_all_rules
) -> None:
    hub.set("implementer", AgentScript(confidence=0.9, files=[("app/feature.py", "X=1\n")]))
    hub.set("reviewer", AgentScript(confidence=0.95, review_verdict="approved"))
    obj = make_objective(
        tmp_git_repo,
        rules=allow_all_rules,
        review_required=True,
        acceptance=[AcceptanceCriterion(id="ac1", text="feature", spec_ref="app/feature.py")],
    )
    result = make_supervisor().run(obj)

    assert result.status is RunStatus.SUCCEEDED
    assert result.needs_human is False
    assert result.artifacts["pattern"] == "maker_checker"
    rcs = result.repo_change_sets[0]
    assert rcs.branch_name == "forge/TASK-123"
    assert rcs.diff_stat["files"] >= 1
    assert "app/feature.py" in rcs.changed_files

    rows = sink.rows_for_parent(obj_parent(obj))
    roles = [r["role"] for r in rows]
    assert roles == ["implementer", "reviewer"]
    impl_row = rows[0]
    assert impl_row["merged"] is True
    assert impl_row["agent_run_id"] is not None
    # Reviewer is read-only: it never produced a branch / merge.
    assert rows[1]["merged"] is False
    # AC 11/10: reviewer's isolated context is the implementer ARTIFACT, not raw steps.
    reviewer_call = hub.calls_for("reviewer")[0]
    assert any("Changed files" in c["content"] for c in reviewer_call.initial_context)


def test_token_aggregation(tmp_git_repo: Path, hub: ScriptingHub, make_supervisor, allow_all_rules):
    hub.set(
        "implementer",
        AgentScript(files=[("app/f.py", "1\n")], token_usage={"input": 100, "output": 40}),
    )
    hub.set(
        "reviewer",
        AgentScript(review_verdict="approved", token_usage={"input": 30, "output": 10}),
    )
    obj = make_objective(tmp_git_repo, rules=allow_all_rules, review_required=True)
    result = make_supervisor().run(obj)
    assert result.artifacts["token_usage"]["total"] == 100 + 40 + 30 + 10


def test_implementer_objective_excludes_query_mcp(
    tmp_git_repo: Path, hub: ScriptingHub, make_supervisor, allow_all_rules
) -> None:
    hub.set("implementer", AgentScript(files=[("app/f.py", "1\n")]))
    hub.set("reviewer", AgentScript(review_verdict="approved"))
    obj = make_objective(
        tmp_git_repo, rules=allow_all_rules, review_required=True, allowed_actions=[]
    )
    make_supervisor().run(obj)
    impl_call = hub.calls_for("implementer")[0]
    assert "query_mcp" not in impl_call.allowed_actions
    assert "write_code" in impl_call.allowed_actions


def test_sequential_pipeline_structured_handoff(
    tmp_git_repo: Path, hub: ScriptingHub, make_supervisor, allow_all_rules
) -> None:
    hub.set("researcher", AgentScript(summary="findings: use lib Z"))
    hub.set("planner", AgentScript(summary="PLAN: edit app/p.py"))
    hub.set("implementer", AgentScript(files=[("app/p.py", "P=1\n")]))
    hub.set("tester", AgentScript(files=[("tests/test_p.py", "def test(): pass\n")]))
    hub.set("reviewer", AgentScript(review_verdict="approved"))
    obj = make_objective(
        tmp_git_repo,
        rules=allow_all_rules,
        context_extra={"task_kind": "feature"},
    )
    result = make_supervisor().run(obj)
    assert result.artifacts["pattern"] == "sequential_pipeline"
    assert result.status is RunStatus.SUCCEEDED
    # Planner sees the researcher ARTIFACT, not its raw trace.
    planner_ctx = hub.calls_for("planner")[0].initial_context
    assert any("findings: use lib Z" in c["content"] for c in planner_ctx)
    # Implementer sees the planner artifact (plan), never the researcher's raw steps.
    impl_ctx = hub.calls_for("implementer")[0].initial_context
    assert any("PLAN: edit app/p.py" in c["content"] for c in impl_ctx)


def test_policy_disallowed_no_subagents(
    tmp_git_repo: Path, hub: ScriptingHub, sink, make_supervisor, deny_rules
) -> None:
    obj = make_objective(tmp_git_repo, rules=deny_rules, review_required=True)
    result = make_supervisor().run(obj)
    assert result.needs_human is True
    assert result.artifacts["needs_human_reason"] == "subagents_not_permitted"
    # No subagent ran.
    assert hub.calls == []
    # No code-producing subagent rows persisted.
    assert all(r["status"] in (RunStatus.CANCELLED,) for r in sink.rows_for_parent(obj_parent(obj)))


def test_feature_flag_off(
    tmp_git_repo: Path, hub: ScriptingHub, make_supervisor, allow_all_rules
) -> None:
    from forge_coordinator import CoordinatorSettings

    obj = make_objective(tmp_git_repo, rules=allow_all_rules, review_required=True)
    result = make_supervisor(settings=CoordinatorSettings(enabled=False)).run(obj)
    assert result.needs_human is True
    assert result.artifacts["needs_human_reason"] == "multi_agent_disabled"
    assert hub.calls == []


def test_fan_out_clean_merge_and_concurrency_bound(
    tmp_git_repo: Path, hub: ScriptingHub, sink, make_supervisor
) -> None:
    rules = SubagentRules(allow_subagents=True, allowed_roles=["implementer"], max_parallel=2)
    # 3 implementers, disjoint files, with a slow run to expose concurrency.
    hub.set(
        "implementer",
        AgentScript(files=[("app/a.py", "A\n")], sleep=0.05),
        AgentScript(files=[("app/b.py", "B\n")], sleep=0.05),
        AgentScript(files=[("app/c.py", "C\n")], sleep=0.05),
    )
    obj = make_objective(
        tmp_git_repo,
        rules=rules,
        task_policy=SubAgentPolicy(allowed=True, max_parallel=3),
        context_extra={
            "fan_out_units": [
                {"objective": "do a"},
                {"objective": "do b"},
                {"objective": "do c"},
            ]
        },
    )
    result = make_supervisor().run(obj)
    assert result.artifacts["pattern"] == "fan_out_fan_in"
    assert result.status is RunStatus.SUCCEEDED
    # min(policy=2, task=3, cap=4) = 2 -> never more than 2 concurrent.
    assert hub.max_concurrency <= 2
    changed = result.repo_change_sets[0].changed_files
    assert {"app/a.py", "app/b.py", "app/c.py"} <= set(changed)
    assert all(r["merged"] for r in sink.rows_for_parent(obj_parent(obj)))


def test_fan_out_conflict_interrupts(
    tmp_git_repo: Path, hub: ScriptingHub, make_supervisor
) -> None:
    rules = SubagentRules(allow_subagents=True, allowed_roles=["implementer"], max_parallel=2)
    # Two implementers editing the same file's same line -> conflict.
    hub.set(
        "implementer",
        AgentScript(files=[("app/__init__.py", "VALUE='one'\n")]),
        AgentScript(files=[("app/__init__.py", "VALUE='two'\n")]),
    )
    obj = make_objective(
        tmp_git_repo,
        rules=rules,
        task_policy=SubAgentPolicy(allowed=True, max_parallel=2),
        context_extra={"fan_out_units": [{"objective": "a"}, {"objective": "b"}]},
    )
    result = make_supervisor().run(obj)
    assert result.needs_human is True
    assert result.artifacts["needs_human_reason"] == "merge_conflict"
    assert result.artifacts["merge"]["conflicts"]
    # No integration change set committed.
    assert result.repo_change_sets == []


def test_reject_loop_bounded(
    tmp_git_repo: Path, hub: ScriptingHub, sink, make_supervisor, allow_all_rules
) -> None:
    hub.set(
        "implementer",
        AgentScript(files=[("app/r.py", "R=1\n")]),
        AgentScript(files=[("app/r.py", "R=2\n")]),
    )
    # Reviewer rejects both the first and the retry -> exhausts budget=1.
    hub.set(
        "reviewer",
        AgentScript(review_verdict="changes_requested", findings=["fix naming"]),
        AgentScript(review_verdict="changes_requested", findings=["still wrong"]),
    )
    obj = make_objective(tmp_git_repo, rules=allow_all_rules, review_required=True)
    result = make_supervisor().run(obj)
    assert result.needs_human is True
    assert result.artifacts["needs_human_reason"] == "review_rejected"
    # Exactly two implementer subagent rows (original + one retry).
    impl_rows = [r for r in sink.rows_for_parent(obj_parent(obj)) if r["role"] == "implementer"]
    assert len(impl_rows) == 2
    # The retry implementer received the reviewer findings as scoped context.
    retry_impl_ctx = hub.calls_for("implementer")[1].initial_context
    assert any("fix naming" in c["content"] for c in retry_impl_ctx)
