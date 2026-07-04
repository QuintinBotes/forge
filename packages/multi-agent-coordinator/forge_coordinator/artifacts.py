"""Structured subagent artifacts + the (non-chat) handoff normalization (F27 §4).

Cross-subagent influence flows ONLY through reviewed **structured artifacts**
(Multi-Agent Rule), never free-form chat or another subagent's raw trace. A
predecessor's :class:`SubAgentArtifact` is normalized into
:class:`RetrievedChunk`s that become the successor's isolated ``context_refs``.
"""

from __future__ import annotations

from typing import Any

from forge_contracts import (
    AgentRunResult,
    ChunkType,
    RetrievedChunk,
    RunStatus,
    SubAgentArtifact,
    SubAgentAssignment,
    SubAgentResult,
    SubAgentRole,
    TokenUsage,
)

__all__ = [
    "ARTIFACT_KIND_BY_ROLE",
    "build_subagent_result",
    "normalize_artifact_to_chunks",
]

ARTIFACT_KIND_BY_ROLE: dict[SubAgentRole, str] = {
    SubAgentRole.PLANNER: "spec_draft",
    SubAgentRole.RESEARCHER: "research_brief",
    SubAgentRole.IMPLEMENTER: "code_change",
    SubAgentRole.TESTER: "test_suite",
    SubAgentRole.REVIEWER: "review",
    SubAgentRole.SECURITY: "security_report",
}

_STATUS_MAP: dict[RunStatus, str] = {
    RunStatus.SUCCEEDED: "succeeded",
    RunStatus.FAILED: "failed",
    RunStatus.ESCALATED: "awaiting_input",
    RunStatus.CANCELLED: "blocked",
    RunStatus.PENDING: "blocked",
    RunStatus.RUNNING: "blocked",
}


def _token_usage(raw: Any) -> TokenUsage:
    if isinstance(raw, dict):
        return TokenUsage(
            input_tokens=int(raw.get("input_tokens", raw.get("input", 0)) or 0),
            output_tokens=int(raw.get("output_tokens", raw.get("output", 0)) or 0),
        )
    return TokenUsage()


def build_subagent_result(
    *,
    assignment: SubAgentAssignment,
    child: AgentRunResult,
    branch_name: str | None,
) -> SubAgentResult:
    """Fold a child :class:`AgentRunResult` into a structured :class:`SubAgentResult`."""
    arts: dict[str, Any] = dict(child.artifacts or {})
    kind = ARTIFACT_KIND_BY_ROLE[assignment.role]

    status = _STATUS_MAP.get(child.status, "blocked")
    if child.needs_human and status == "succeeded":
        status = "awaiting_input"

    changed_files = list(child.changed_files)
    if not changed_files and isinstance(arts.get("changed_files"), list):
        changed_files = [str(f) for f in arts["changed_files"]]

    findings = arts.get("findings")
    findings_list = [str(f) for f in findings] if isinstance(findings, list) else []

    artifact = SubAgentArtifact(
        kind=kind,  # type: ignore[arg-type]
        summary=str(arts.get("summary") or child.summary or child.output or ""),
        review_verdict=arts.get("review_verdict"),
        findings=findings_list,
        branch_name=branch_name or arts.get("branch_name"),
        changed_files=changed_files,
        report_ref=arts.get("report_ref"),
    )

    return SubAgentResult(
        assignment_id=assignment.id,
        role=assignment.role,
        agent_run_id=child.run_id,
        status=status,  # type: ignore[arg-type]
        confidence=float(child.confidence) if child.confidence is not None else 0.0,
        artifact=artifact,
        token_usage=_token_usage(arts.get("token_usage")),
    )


def normalize_artifact_to_chunks(
    artifact: SubAgentArtifact, *, assignment_id: str, role: SubAgentRole
) -> list[RetrievedChunk]:
    """Normalize a predecessor artifact into isolated context chunks for handoff."""
    parts = [artifact.summary] if artifact.summary else []
    if artifact.findings:
        parts.append("Findings:\n" + "\n".join(f"- {f}" for f in artifact.findings))
    if artifact.changed_files:
        parts.append("Changed files:\n" + "\n".join(f"- {p}" for p in artifact.changed_files))
    content = "\n\n".join(parts) or f"(empty {artifact.kind} artifact)"
    return [
        RetrievedChunk(
            id=f"artifact:{assignment_id}",
            content=content,
            chunk_type=ChunkType.SUMMARY,
            score=1.0,
            source_id=assignment_id,
            metadata={
                "kind": artifact.kind,
                "role": role.value,
                "from_assignment": assignment_id,
                "review_verdict": artifact.review_verdict,
            },
        )
    ]
