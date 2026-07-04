"""The returned AgentRunResult is byte-compatible with the F08 verify->PR flow (AC 14)."""

from __future__ import annotations

from pathlib import Path

from _helpers import AgentScript, ScriptingHub, make_objective

from forge_contracts import AcceptanceCriterion, RunStatus


class _F08Fake:
    """Stand-in for F08: builds a PR request purely from the F06/F27 contract."""

    def consume(self, result) -> dict:
        # F08 re-verifies the MERGED integration branch independently; it only
        # needs these fields from the contract (no coordinator-specific shape).
        assert result.status in (RunStatus.SUCCEEDED, RunStatus.ESCALATED)
        assert isinstance(result.confidence, float)
        change = result.repo_change_sets[0]
        token = result.artifacts["token_usage"]
        return {
            "branch_name": change.branch_name,
            "head_commit_sha": change.head_commit_sha,
            "changed_files": change.changed_files,
            "diff_stat": change.diff_stat,
            "acceptance": result.acceptance_criteria_satisfied,
            "total_tokens": token["total"],
        }


def test_f08_consumes_supervised_result_unchanged(
    tmp_git_repo: Path, hub: ScriptingHub, make_supervisor, allow_all_rules
) -> None:
    hub.set("implementer", AgentScript(files=[("app/api.py", "def x(): ...\n")], confidence=0.9))
    hub.set("reviewer", AgentScript(review_verdict="approved", confidence=0.95))
    obj = make_objective(
        tmp_git_repo,
        rules=allow_all_rules,
        review_required=True,
        acceptance=[AcceptanceCriterion(id="ac1", text="api", spec_ref="app/api.py")],
    )
    result = make_supervisor().run(obj)

    pr = _F08Fake().consume(result)
    assert pr["branch_name"] == "forge/TASK-123"
    assert pr["head_commit_sha"]
    assert "app/api.py" in pr["changed_files"]
    assert pr["diff_stat"]["files"] >= 1
    assert pr["acceptance"] == ["ac1"]
    assert pr["total_tokens"] > 0
