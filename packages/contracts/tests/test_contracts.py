"""Tests for the frozen shared contracts (Task 0.3).

These exercise the SHARED SUBSTRATE every later package builds against:

- every Protocol and DTO listed in plan Task 0.3 is importable from
  ``forge_contracts`` (the frozen public surface),
- DTOs round-trip through ``model_validate`` / ``model_dump`` (including JSON
  mode where enums serialise to their string values),
- enum values match ``docs/FORGE_SPEC.md`` exactly (statuses, kinds, roles,
  workflow/incident states, approval gates),
- the frozen retrieval constants (RRF k=60, chunk-type weights, confidence
  threshold) match the spec, and
- Protocols are ``runtime_checkable`` so structural conformance can be asserted.

No external services are required: contracts depend only on Pydantic.
"""

from __future__ import annotations

import uuid
from typing import Protocol, get_type_hints

import pytest

import forge_contracts as fc

# --------------------------------------------------------------------------- #
# Importability of the full frozen surface                                    #
# --------------------------------------------------------------------------- #

PROTOCOLS = [
    "KnowledgeStore",
    "Retriever",
    "BoardService",
    "SpecEngine",
    "AgentRuntime",
    "WorkflowEngine",
    "PolicyEvaluator",
    "SkillProfileRegistry",
    "MCPClient",
    "PMAdapter",
    "IntegrationClient",
    "GitHubClient",
    "SlackNotifier",
    "EmbeddingClient",
    "RerankerClient",
    "ModelClient",
]

# Every DTO named in plan Task 0.3.
LISTED_DTOS = [
    "Chunk",
    "RetrievedChunk",
    "Ranked",
    "KnowledgeScope",
    "AgentObjective",
    "AgentRunResult",
    "Step",
    "ToolCall",
    "Decision",
    "Policy",
    "SkillProfile",
    "ValidationReport",
    "ApprovalRequest",
]

SUPPORTING_DTOS = [
    "IndexResult",
    "RerankResult",
    "AcceptanceCriterion",
    "RepoTarget",
    "ApprovalPolicy",
    "SubAgentPolicy",
    "HandoffRules",
    "WriteRules",
    "ReviewRules",
    "DeployRules",
    "KnowledgeRules",
    "PolicySkillProfiles",
    "SubagentRules",
    "Requirement",
    "OpenQuestion",
    "ADR",
    "Constitution",
    "SpecManifest",
    "RequirementTrace",
    "CheckResult",
    "TaskDTO",
    "EpicDTO",
    "SprintDTO",
    "MilestoneDTO",
    "IncidentDTO",
    "BoardFilter",
    "BulkUpdate",
    "WorkflowTransition",
    "RetryPolicy",
    "EscalationPolicy",
    "WorkflowDefinition",
    "WorkflowRun",
    "MCPAuth",
    "MCPCapabilities",
    "MCPConnection",
    "MCPResource",
    "MCPResourceContent",
    "MCPToolResult",
    "MCPAuditEntry",
    "RepositoryConnection",
    "RepoSyncResult",
    "PullRequestRequest",
    "PullRequest",
    "CIStatus",
    "SlackMessage",
    "SlackDeliveryResult",
    "ExternalTask",
    "WebhookEvent",
    "HealthResult",
    "ModelMessage",
    "ModelToolCall",
    "TokenUsage",
    "ModelRequest",
    "ModelResponse",
    "ModelStreamEvent",
]

ENUMS = [
    "UserRole",
    "APIKeyKind",
    "RepoProvider",
    "MCPTransport",
    "MCPAuthType",
    "MCPIndexStrategy",
    "SyncMode",
    "KnowledgeSourceKind",
    "ChunkType",
    "TaskKind",
    "TaskStatus",
    "Priority",
    "ExecutionMode",
    "SpecStatus",
    "WorkflowState",
    "IncidentSeverity",
    "IncidentState",
    "RunStatus",
    "ApprovalGate",
    "ApprovalStatus",
    "SubAgentRole",
    "StepKind",
    "DecisionEffect",
    "Direction",
    "PRState",
    "CIState",
]

EXCEPTIONS = [
    "ForgeError",
    "CycleError",
    "UnknownSkillProfileError",
    "SpecGateError",
    "PolicyViolationError",
    "ApprovalRequiredError",
    "MCPWriteForbiddenError",
]


@pytest.mark.parametrize("name", PROTOCOLS)
def test_protocol_importable_and_is_protocol(name: str) -> None:
    obj = getattr(fc, name)
    assert isinstance(obj, type)
    # ``Protocol`` subclasses set this private flag to True.
    assert getattr(obj, "_is_protocol", False) is True, f"{name} is not a Protocol"


@pytest.mark.parametrize("name", LISTED_DTOS + SUPPORTING_DTOS)
def test_dto_importable(name: str) -> None:
    obj = getattr(fc, name)
    assert isinstance(obj, type)
    assert hasattr(obj, "model_validate"), f"{name} is not a Pydantic model"


@pytest.mark.parametrize("name", ENUMS)
def test_enum_importable(name: str) -> None:
    obj = getattr(fc, name)
    assert issubclass(obj, str)


@pytest.mark.parametrize("name", EXCEPTIONS)
def test_exception_importable(name: str) -> None:
    obj = getattr(fc, name)
    assert issubclass(obj, Exception)


def test_all_exports_resolve() -> None:
    for name in fc.__all__:
        assert hasattr(fc, name), f"__all__ lists missing symbol: {name}"


# --------------------------------------------------------------------------- #
# Enum values match the spec                                                   #
# --------------------------------------------------------------------------- #


def test_user_roles_match_spec() -> None:
    assert {r.value for r in fc.UserRole} == {"admin", "member", "viewer", "agent-runner"}


def test_task_status_values() -> None:
    assert fc.TaskStatus.READY_FOR_AGENT.value == "ready_for_agent"
    assert fc.TaskStatus.IN_PROGRESS.value == "in_progress"
    assert "backlog" in {s.value for s in fc.TaskStatus}


def test_task_kind_values_match_spec() -> None:
    assert {k.value for k in fc.TaskKind} == {
        "feature",
        "bug",
        "chore",
        "spike",
        "incident",
        "change_request",
        "doc",
    }


def test_workflow_states_match_spec() -> None:
    states = {s.value for s in fc.WorkflowState}
    for expected in [
        "created",
        "spec_drafting",
        "clarification",
        "spec_review",
        "spec_approved",
        "plan_drafting",
        "plan_review",
        "task_generation",
        "task_ready",
        "executing",
        "verifying",
        "pr_opened",
        "awaiting_review",
        "merged",
        "closed",
        "needs_human_input",
        "failed",
        "cancelled",
    ]:
        assert expected in states


def test_incident_states_match_spec() -> None:
    states = {s.value for s in fc.IncidentState}
    for expected in [
        "alert_received",
        "incident_created",
        "context_gathering",
        "impact_assessed",
        "remediation_proposed",
        "awaiting_approval",
        "executing_runbook",
        "monitoring",
        "resolved",
        "postmortem_created",
    ]:
        assert expected in states


def test_approval_gates_match_spec() -> None:
    assert {g.value for g in fc.ApprovalGate} == {
        "spec",
        "plan",
        "pr",
        "deploy",
        "incident_remediation",
        "policy_override",
    }


def test_subagent_roles_match_spec() -> None:
    assert {r.value for r in fc.SubAgentRole} == {
        "planner",
        "researcher",
        "implementer",
        "tester",
        "reviewer",
        "security",
    }


def test_spec_status_values() -> None:
    assert {s.value for s in fc.SpecStatus} == {
        "draft",
        "clarifying",
        "approved",
        "implementing",
        "validated",
        "closed",
    }


def test_mcp_transport_and_index_strategy() -> None:
    assert {t.value for t in fc.MCPTransport} == {"http", "stdio", "sse"}
    assert {s.value for s in fc.MCPIndexStrategy} == {"sync_and_index", "query_through"}


# --------------------------------------------------------------------------- #
# Frozen retrieval constants                                                   #
# --------------------------------------------------------------------------- #


def test_rrf_k_is_60() -> None:
    assert fc.RRF_K == 60


def test_confidence_threshold_is_spec_value() -> None:
    assert fc.DEFAULT_CONFIDENCE_THRESHOLD == 0.72


def test_embedding_dim_default() -> None:
    assert fc.DEFAULT_EMBEDDING_DIM == 1536


def test_chunk_type_weights_match_spec() -> None:
    w = fc.CHUNK_TYPE_WEIGHTS
    assert w[fc.ChunkType.README] == 1.3
    assert w[fc.ChunkType.POLICY] == 1.5
    assert w[fc.ChunkType.SPEC] == 1.4
    assert w[fc.ChunkType.SUMMARY] == 1.2
    assert w[fc.ChunkType.MARKDOWN] == 1.0
    assert w[fc.ChunkType.CODE] == 1.0
    assert w[fc.ChunkType.MCP_RESOURCE] == 1.0
    for ct in fc.ChunkType:
        assert ct in w


# --------------------------------------------------------------------------- #
# DTO round-trips                                                              #
# --------------------------------------------------------------------------- #


def test_chunk_round_trip() -> None:
    chunk = fc.Chunk(
        content="def f():\n    return 1",
        chunk_type=fc.ChunkType.CODE,
        path="app/main.py",
        start_line=1,
        end_line=2,
        content_hash="abc",
    )
    dumped = chunk.model_dump()
    assert fc.Chunk.model_validate(dumped) == chunk
    # JSON mode serialises the enum to its string value.
    assert chunk.model_dump(mode="json")["chunk_type"] == "code"


def test_retrieved_chunk_carries_attribution() -> None:
    rc = fc.RetrievedChunk(
        content="x",
        chunk_type=fc.ChunkType.MARKDOWN,
        score=0.9,
        source_id="src-1",
        source_uri="github.com/org/api",
        path="README.md",
    )
    assert fc.RetrievedChunk.model_validate(rc.model_dump()) == rc


def test_ranked_carries_chunk() -> None:
    rc = fc.RetrievedChunk(content="x", chunk_type=fc.ChunkType.CODE, score=0.5)
    ranked = fc.Ranked(chunk_id="c1", score=0.5, rank=1, chunk=rc)
    again = fc.Ranked.model_validate(ranked.model_dump())
    assert again.chunk_id == "c1"
    assert again.chunk is not None
    assert again.chunk.content == "x"


def test_knowledge_scope_defaults() -> None:
    scope = fc.KnowledgeScope()
    assert scope.repos == []
    assert scope.mcp_sources == []
    assert scope.source_types == []


def test_tool_call_and_decision() -> None:
    call = fc.ToolCall(tool="write_file", action="write_code", path="app/main.py")
    decision = fc.Decision(effect=fc.DecisionEffect.ALLOW, reason="matched app/**")
    assert decision.allowed is True
    deny = fc.Decision(effect=fc.DecisionEffect.DENY)
    assert deny.allowed is False
    assert fc.ToolCall.model_validate(call.model_dump()) == call


def test_step_round_trip_with_nested() -> None:
    step = fc.Step(
        index=0,
        kind=fc.StepKind.TOOL_CALL,
        thought="write the file",
        tool_call=fc.ToolCall(tool="write_file", path="app/main.py"),
    )
    again = fc.Step.model_validate(step.model_dump())
    assert again.tool_call is not None
    assert again.tool_call.tool == "write_file"
    assert step.model_dump(mode="json")["kind"] == "tool_call"


def test_agent_objective_round_trip() -> None:
    objective = fc.AgentObjective(
        objective="Add customer search endpoint",
        execution_mode=fc.ExecutionMode.SINGLE_AGENT,
        skill_profile=fc.SkillProfile(name="backend-tdd", requires_plan=True),
        knowledge_scope=fc.KnowledgeScope(repos=["github.com/org/api"]),
        repo_targets=[fc.RepoTarget(repo="github.com/org/api", base_branch="main")],
        acceptance_criteria=[fc.AcceptanceCriterion(id="A1", text="cursor pagination")],
        allowed_actions=["read_repo", "write_code"],
    )
    again = fc.AgentObjective.model_validate(objective.model_dump())
    assert again.skill_profile is not None
    assert again.skill_profile.requires_plan is True
    assert again.confidence_threshold == fc.DEFAULT_CONFIDENCE_THRESHOLD


def test_agent_run_result_round_trip() -> None:
    result = fc.AgentRunResult(
        status=fc.RunStatus.SUCCEEDED,
        steps=[fc.Step(kind=fc.StepKind.OUTPUT, output="done")],
        confidence=0.91,
    )
    again = fc.AgentRunResult.model_validate(result.model_dump())
    assert again.status is fc.RunStatus.SUCCEEDED
    assert len(again.steps) == 1


def test_policy_round_trip_matches_schema() -> None:
    policy = fc.Policy(
        repo_id="github.com/org/api",
        name="Core API",
        write_rules=fc.WriteRules(allow=["app/**", "tests/**"], deny=["secrets/**"]),
        deploy_rules=fc.DeployRules(allow_agent_deploy=False, environments=["dev"]),
        skill_profiles=fc.PolicySkillProfiles(default="backend-tdd", allowed=["backend-tdd"]),
    )
    again = fc.Policy.model_validate(policy.model_dump())
    assert again.write_rules.allow == ["app/**", "tests/**"]
    assert again.write_rules.deny == ["secrets/**"]
    assert again.skill_profiles.default == "backend-tdd"


def test_skill_profile_covers_all_spec_fields() -> None:
    profile = fc.SkillProfile(
        name="incident-response",
        requires_human_approval_before_action=True,
        max_blast_radius="low",
        allowed_actions=["read_logs"],
        forbidden_actions=["deploy_prod"],
    )
    again = fc.SkillProfile.model_validate(profile.model_dump())
    assert again.forbidden_actions == ["deploy_prod"]
    assert again.requires_human_approval_before_action is True


def test_spec_manifest_round_trip() -> None:
    manifest = fc.SpecManifest(
        id="SPEC-17",
        name="Customer endpoint",
        status=fc.SpecStatus.APPROVED,
        requirements=[fc.Requirement(id="R1", text="Add search endpoint")],
        acceptance_criteria=[
            fc.AcceptanceCriterion(id="A1", req_refs=["R1"], text="cursor pagination")
        ],
        execution_mode=fc.ExecutionMode.SINGLE_AGENT,
        skill_profile="backend-tdd",
    )
    again = fc.SpecManifest.model_validate(manifest.model_dump())
    assert again.status is fc.SpecStatus.APPROVED
    assert again.requirements[0].id == "R1"
    assert again.acceptance_criteria[0].req_refs == ["R1"]


def test_validation_report_round_trip() -> None:
    report = fc.ValidationReport(
        task_id=str(uuid.uuid4()),
        passed=True,
        traceability=[
            fc.RequirementTrace(
                requirement_id="R1",
                acceptance_criteria_ids=["A1"],
                test_refs=["tests/test_x.py::test_a1"],
                satisfied=True,
            )
        ],
        checks=[fc.CheckResult(name="tests", passed=True)],
    )
    again = fc.ValidationReport.model_validate(report.model_dump())
    assert again.passed is True
    assert again.traceability[0].satisfied is True


def test_approval_request_round_trip() -> None:
    req = fc.ApprovalRequest(
        gate=fc.ApprovalGate.PR,
        status=fc.ApprovalStatus.PENDING,
        summary="Open PR for TASK-123",
        confidence=0.8,
        risks=["touches auth middleware"],
    )
    again = fc.ApprovalRequest.model_validate(req.model_dump())
    assert again.gate is fc.ApprovalGate.PR
    assert again.status is fc.ApprovalStatus.PENDING
    assert req.model_dump(mode="json")["gate"] == "pr"


def test_workflow_definition_parses_dsl_fields() -> None:
    definition = fc.WorkflowDefinition(
        name="default_feature",
        version="1",
        transitions=[
            fc.WorkflowTransition.model_validate(
                {"from": "created", "to": "spec_drafting", "action": "generate_spec_draft"}
            )
        ],
        retry_policy=fc.RetryPolicy(max_retries=3, backoff="exponential"),
        escalation_policy=fc.EscalationPolicy(confidence_threshold=0.72),
    )
    t = definition.transitions[0]
    assert t.from_state == "created"
    assert t.to_state == "spec_drafting"
    # The DSL key is ``from``/``to``; serialising by alias preserves it.
    assert definition.transitions[0].model_dump(by_alias=True)["from"] == "created"


def test_mcp_connection_defaults_read_only() -> None:
    conn = fc.MCPConnection(
        id="confluence-engineering",
        name="Engineering Confluence",
        transport=fc.MCPTransport.HTTP,
        endpoint="https://mcp.example.internal/confluence",
        auth=fc.MCPAuth(type=fc.MCPAuthType.OAUTH),
        capabilities=fc.MCPCapabilities(resources=True, tools=True),
        index_strategy=fc.MCPIndexStrategy.SYNC_AND_INDEX,
        allowed_namespaces=["engineering"],
    )
    # Spec rule 1: connections MUST default to allow_write=false.
    assert conn.allow_write is False
    assert fc.MCPConnection.model_validate(conn.model_dump()) == conn


def test_pmadapter_aliases_forge_task_to_task_dto() -> None:
    assert fc.ForgeTask is fc.TaskDTO


# --------------------------------------------------------------------------- #
# Protocols are runtime-checkable and structurally usable                      #
# --------------------------------------------------------------------------- #


def test_embedding_client_runtime_checkable() -> None:
    class FakeEmbedder:
        @property
        def dimension(self) -> int:
            return 3

        def embed(self, texts: list[str]) -> list[list[float]]:
            return [[0.0, 0.0, 0.0] for _ in texts]

        def embed_query(self, text: str) -> list[float]:
            return [0.0, 0.0, 0.0]

    assert isinstance(FakeEmbedder(), fc.EmbeddingClient)


def test_policy_evaluator_runtime_checkable() -> None:
    class FakeEvaluator:
        def load(self, repo_root: object) -> object:
            return fc.Policy(repo_id="x")

        def evaluate(self, action: object, policy: object) -> object:
            return fc.Decision(effect=fc.DecisionEffect.DENY)

    assert isinstance(FakeEvaluator(), fc.PolicyEvaluator)


def test_knowledge_store_protocol_signature_is_frozen() -> None:
    # Guards the frozen ``search(query, scope, k=10)`` default the spine relies on.
    hints = get_type_hints(fc.KnowledgeStore.search)
    assert "query" in hints
    assert "scope" in hints
    import inspect

    sig = inspect.signature(fc.KnowledgeStore.search)
    assert sig.parameters["k"].default == 10


def test_retriever_fuse_default_k_is_60() -> None:
    import inspect

    sig = inspect.signature(fc.Retriever.fuse)
    assert sig.parameters["k"].default == 60


def test_protocols_are_runtime_checkable_subclass_of_protocol() -> None:
    for name in PROTOCOLS:
        obj = getattr(fc, name)
        assert issubclass(obj, Protocol)  # type: ignore[arg-type]
