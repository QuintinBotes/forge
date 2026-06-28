"""Secret redaction (AC 20) + isolated objective/artifact scoping (AC 10, 11)."""

from __future__ import annotations

from forge_contracts import (
    AgentObjective,
    RetrievedChunk,
    SubAgentArtifact,
    SubAgentAssignment,
    SubAgentRole,
)
from forge_coordinator import build_subagent_objective
from forge_coordinator.artifacts import normalize_artifact_to_chunks
from forge_coordinator.redaction import REDACTED, redact_obj, redact_secrets


def test_redact_secrets_masks_common_patterns() -> None:
    text = "api_key=sk-supersecretvalue and Authorization: Bearer abc.def.ghi"
    out = redact_secrets(text)
    assert "sk-supersecretvalue" not in out
    assert REDACTED in out


def test_redact_obj_masks_secret_keys_recursively() -> None:
    obj = {"model": {"api_key_ref": "vault://k", "name": "gpt"}, "notes": ["token=xyz123"]}
    red = redact_obj(obj)
    assert red["model"]["api_key_ref"] == REDACTED
    assert "xyz123" not in red["notes"][0]
    assert red["model"]["name"] == "gpt"


def test_subagent_objective_is_isolated_to_its_context_refs() -> None:
    parent = AgentObjective(
        objective="big feature",
        allowed_actions=["read_repo", "write_code"],
        context={"agents_md": "rules"},
    )
    chunk = RetrievedChunk(id="artifact:sa-planner-1", content="the plan")
    assignment = SubAgentAssignment(
        id="sa-implementer-1",
        role=SubAgentRole.IMPLEMENTER,
        objective="implement the plan",
        allowed_actions=["read_repo", "write_code"],
        context_refs=[chunk],
        depends_on=["sa-planner-1"],
    )
    obj = build_subagent_objective(
        parent=parent,
        assignment=assignment,
        repo=None,
        integration_branch="forge/int",
        worktree_path=None,
        branch_name=None,
    )
    ctx_refs = obj.context["initial_context"]
    assert len(ctx_refs) == 1
    assert ctx_refs[0]["content"] == "the plan"
    # The child cannot spawn its own subagents.
    assert obj.subagent_policy.allowed is False


def test_handoff_passes_artifact_not_raw_trace() -> None:
    artifact = SubAgentArtifact(kind="spec_draft", summary="PLAN: do A then B")
    chunks = normalize_artifact_to_chunks(
        artifact, assignment_id="sa-planner-1", role=SubAgentRole.PLANNER
    )
    assert len(chunks) == 1
    assert "PLAN: do A then B" in chunks[0].content
    assert chunks[0].metadata["kind"] == "spec_draft"
