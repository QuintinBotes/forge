"""Cross-workspace isolation over every workspace-scoped resource type (PT-13, FR-023; #189).

Every :class:`~scf_db.base.WorkspaceScopedModel` subclass is mapped here: in
:data:`ROUTES`, :data:`NAMED`, :data:`LISTS` or :data:`SERVED_UNDER` through the routes
that read, list, change or delete it, or in :data:`NO_API_SURFACE` with the reviewed
reason it has none (:func:`coverage`). ``check_tenant_isolation_matrix`` in
``matrix_checks.py`` seeds one of each in workspace A (:data:`SEEDS`), then has workspace
B's admin name A's identifiers on every route that takes one, in its path, query or body,
and on the MCP server's tools (:func:`run`). Defects not yet fixed are in
:data:`OPEN_DEFECTS`.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any
from unittest import mock

from fastapi.testclient import TestClient

from scf_spec import spec_id_for_key

WORKSPACE_A = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
WORKSPACE_B = uuid.UUID("00000000-0000-0000-0000-0000000000bb")
ADMIN_A = uuid.UUID("00000000-0000-0000-0000-00000000a0a0")
ADMIN_B = uuid.UUID("00000000-0000-0000-0000-00000000b0b0")
NOW = datetime(2026, 1, 1, tzinfo=UTC)
_DATES = {"start_date": "2026-01-01", "end_date": "2026-01-14"}
_RULE = {
    "name": "a-rule",
    "trigger": {"type": "workflow_state_changed", "config": {"to_state": "merged"}},
    "actions": [{"type": "send_workflow_event", "event": "close"}],
}

#: How workspace A's instance of each resource type is made, in order: a create route
#: called by A's admin (the id is read from the response's ``id``), or a model whose row
#: is inserted with these columns. ``"{Type}"`` stands for A's instance of that type.
SEEDS: list[tuple[str, str, dict[str, Any]]] = [
    ("Workspace", "=workspace", {}),
    ("User", "=admin", {}),
    ("Project", "Project", {"name": "Apollo", "key": "APO"}),
    ("PlatformAPIKey", "POST /auth/api-keys", {"name": "a-key"}),
    ("Secret", "POST /auth/secrets", {"name": "a-secret", "secret": "sk-a"}),
    ("Epic", "POST /board/epics", {"title": "a-epic", "project_id": "{Project}"}),
    ("Sprint", "POST /projects/{Project}/sprints", {**_DATES, "name": "a-sprint"}),
    ("Sprint2", "POST /projects/{Project}/sprints", {**_DATES, "name": "a-next"}),
    ("SprintOld", "POST /projects/{Project}/sprints", {**_DATES, "name": "a-old"}),
    ("SprintOldStarted", "POST /sprints/{SprintOld}/start", {}),
    ("SprintOldDone", "POST /sprints/{SprintOld}/complete", {"carryover": "backlog"}),
    ("SprintStarted", "POST /sprints/{Sprint}/start", {}),
    ("Milestone", "POST /board/milestones", {"name": "a-ms", "project_id": "{Project}"}),
    ("Incident", "POST /board/incidents", {"title": "a-inc", "project_id": "{Project}"}),
    (
        "Task",
        "POST /board/tasks",
        {
            "title": "a-task",
            "project_id": "{Project}",
            "epic_id": "{Epic}",
            "sprint_id": "{Sprint}",
            "assignee_id": "{User}",
        },
    ),
    ("Team", "POST /teams", {"key": "ateam", "name": "A team"}),
    ("ProjectTeamAccess", "POST /projects/{Project}/team-access", {"team_id": "{Team}"}),
    ("EstimationScale", "POST /estimation-scales", {"name": "a-scale"}),
    (
        "EstimationScaleOfProject",
        "POST /estimation-scales",
        {"name": "a-project-scale", "project_id": "{Project}"},
    ),
    ("PMConnection", "POST /integrations/pm/connections", {"project_id": "{Project}"}),
    ("MCPConnection", "POST /mcp/connections", {"id": "a-conn", "name": "a-conn"}),
    ("MarketplaceRegistry", "POST /marketplace/registries", {"name": "a-reg", "url": "x"}),
    ("WorldClass", "POST /world/ontology/classes", {"key": "a_class", "label": "A"}),
    ("WorldProposal", "POST /world/proposals", {"phrase": "a proposal"}),
    ("WorldDecision", "POST /world/decisions", {"title": "a-decision", "fact_ids": []}),
    (
        "AutomationRule",
        "POST /projects/{Project}/automations",
        _RULE,
    ),
    ("AgentRun", "AgentRun", {"status": "succeeded"}),
    ("WorkflowRun", "WorkflowRun", {"task_id": "{Task}"}),
    (
        "ApprovalRequest",
        "ApprovalRequest",
        {"gate": "spec", "workflow_run_id": "{WorkflowRun}", "project_id": "{Project}"},
    ),
    (
        "RedTeamRecord",
        "RedTeamRecord",
        {"workflow_run_id": "{WorkflowRun}", "verdict": "pass", "kind": "a", "evidence": {}},
    ),
    (
        "TaskStatusEvent",
        "TaskStatusEvent",
        {
            "task_id": "{Task}",
            "project_id": "{Project}",
            "from_status": "in_progress",
            "to_status": "done",
            "changed_at": NOW,
        },
    ),
    (
        "CostEvent",
        "CostEvent",
        {
            "project_id": "{Project}",
            "task_id": "{Task}",
            "kind": "completion",
            "provider": "a",
            "model": "a",
            "cost_usd": 1,
            "request_id": "a",
            "occurred_at": NOW,
        },
    ),
    (
        "RoleConfigOfProject",
        "AgentRoleConfig",
        {"project_id": "{Project}", "role": "coder", "model_or_tier": "x", "effort": "low"},
    ),
    ("Gate", "=spec", {}),
    ("Revision", "=1", {}),
    ("SpecVersion", "=1", {}),
    ("SpecKey", "POST /spec/specs", {"epic_id": "{Epic}", "name": "a-spec"}),
    # A spec's id derives from its key, which each workspace numbers from SPEC-1.
    ("Spec", f"={spec_id_for_key('SPEC-1')}", {}),
    ("Constitution", "POST /spec/constitution", {"project_id": "{Project}"}),
    ("AgentRunLive", "AgentRun", {"status": "running"}),
    ("RunTrace", "POST /observability/runs/{AgentRun}/trace", {}),
    ("WorldEntity", "WorldEntity", {"label": "a-entity"}),
    ("WorldEntity2", "WorldEntity", {"label": "a-entity-2"}),
    (
        "WorldFact",
        "POST /world/facts",
        {"subject_id": "{WorldEntity}", "phrase": "is", "value": "x"},
    ),
    (
        "WorldFact2",
        "POST /world/facts",
        {"subject_id": "{WorldEntity}", "phrase": "uses", "object_id": "{WorldEntity2}"},
    ),
    (
        "WorldEntityMerge",
        "POST /world/merges",
        {"survivor_id": "{WorldEntity}", "merged_id": "{WorldEntity2}"},
    ),
    ("WorldRelation", "POST /world/ontology/relations", {"key": "a_rel", "label": "A"}),
    ("WorkflowDefinition", "POST /workflow/editor/definitions#name", {"name": "a_flow"}),
    ("RegistrySlug", "=a-reg", {}),
    (
        "MarketplaceListing",
        "MarketplaceListing",
        {
            "registry_id": "{MarketplaceRegistry}",
            "kind": "mcp_connector",
            "slug": "a-listing",
            "name": "A",
            "summary": "A",
            "latest_version": "1.0.0",
            "cached_at": NOW,
        },
    ),
    ("ListingSlug", "=a-listing", {}),
    (
        "MarketplaceInstallation",
        "MarketplaceInstallation",
        {
            "registry_slug": "a-reg",
            "listing_slug": "a-listing",
            "kind": "mcp_connector",
            "installed_version": "1.0.0",
            "target_kind": "workspace",
            "content_hash": "sha256:0",
            "verification_status": "verified",
        },
    ),
    (
        "MarketplaceAuditLog",
        "MarketplaceAuditLog",
        {"actor": "a", "operation": "install", "result_status": "ok"},
    ),
    (
        "BenchmarkSuite",
        "BenchmarkSuite",
        {
            "slug": "a-suite",
            "version": "1.0",
            "title": "A",
            "task_count": 1,
            "content_hash": "sha256:0",
            "private": True,
        },
    ),
    ("SuiteSlug", "=a-suite", {}),
    ("SuiteVersion", "=1.0", {}),
    (
        "BenchmarkSubmission",
        "BenchmarkSubmission",
        {
            "benchmark_suite_id": "{BenchmarkSuite}",
            "suite_content_hash": "sha256:0",
            "submitter_name": "a",
            "model_label": "a",
        },
    ),
    ("RepositoryConnection", "RepositoryConnection", {"repo_id": "a/repo", "name": "a-repo"}),
    (
        "Attestation",
        "Attestation",
        {
            "subject_digest": "sha256:0",
            "predicate_type": "https://example.com/p",
            "envelope": {"payloadType": "application/json", "payload": "e30=", "signatures": []},
            "payload_hash": "0" * 64,
            "keyid": "a",
            "workflow_run_id": "{WorkflowRun}",
            "agent_run_id": "{AgentRun}",
        },
    ),
    ("HeldOutScenario", "POST /tasks/{Task}/held-out-scenarios", {}),
    ("AuditLog", "AuditLog", {"action": "a.action", "actor_id": "{User}", "target_id": "{Task}"}),
    ("DeclaredIncident", "POST /incidents", {"project_id": "{Project}", "title": "a-inc"}),
    ("Remediation", "POST /incidents/{DeclaredIncident}/remediation", {}),
    ("Budget", "Budget", {"name": "a-budget", "amount": 10}),
    ("Pipeline", "PUT /projects/{Project}/pipeline", {}),
    ("Deployment", "POST /projects/{Project}/deployments", {}),
    ("TeamMember", "POST /teams/{Team}/members", {"user_id": "{User}"}),
    (
        "RoleGrant",
        "RoleGrant",
        {
            "principal_type": "user",
            "principal_id": "{User}",
            "scope_type": "project",
            "scope_id": "{Project}",
            "role": "viewer",
        },
    ),
    ("ScimToken", "ScimToken", {"name": "a", "token_hash": "a" * 64, "token_prefix": "a"}),
    (
        "SsoConfiguration",
        "SsoConfiguration",
        {
            "idp_entity_id": "e",
            "idp_sso_url": "u",
            "idp_x509_certs": ["c"],
            "sp_entity_id": "e",
            "sp_private_key_encrypted": b"k",
            "sp_cert_pem": "c",
        },
    ),
    (
        "OidcConfiguration",
        "OidcConfiguration",
        {
            "issuer": "i",
            "client_id": "c",
            "client_secret_ref": "r",
            "client_secret_encrypted": b"s",
        },
    ),
    ("Users", "POST /scim/v2/Users", {"userName": "scim-a@example.com"}),
    ("Groups", "POST /scim/v2/Groups", {"displayName": "A group"}),
    (
        "RiskClassification",
        "RiskClassification",
        {
            "commit_sha": "a" * 40,
            "agent_run_id": "{AgentRun}",
            "workflow_run_id": "{WorkflowRun}",
            "derived_class": "low",
            "final_class": "low",
            "enforcement_mode": "shadow",
        },
    ),
    (
        "RiskGateObservation",
        "RiskGateObservation",
        {
            "classification_id": "{RiskClassification}",
            "gate_name": "a_only_gate",
            "actual_decision": "approved",
            "risk_decision": "approved",
            "agreed": True,
        },
    ),
    ("RiskGate", "=a_only_gate", {}),
    ("KnowledgeSource", "KnowledgeSource", {"kind": "document", "name": "a", "uri": "a.md"}),
    (
        "RetrievalChunk",
        "RetrievalChunk",
        {
            "knowledge_source_id": "{KnowledgeSource}",
            "chunk_type": "markdown",
            "weight": 1.0,
            "content": "a",
            "path": "a.md",
            "content_hash": "a",
            "metadata": {},
            "token_count": 1,
        },
    ),
    (
        "PolicyRuleEvaluation",
        "PolicyRuleEvaluation",
        {
            "action": "a",
            "base_effect": "allow",
            "final_effect": "allow",
            "agent_run_id": "{AgentRun}",
        },
    ),
    ("AgentRoleConfig", "PUT /ao/role-config/coder", {}),
]

#: Every route that takes a resource's identifier in its path, and the resource types
#: its path parameters name, in order.
ROUTES: dict[str, tuple[str, ...]] = {
    "DELETE /auth/api-keys/{key_id}": ("PlatformAPIKey",),
    **dict.fromkeys(
        [
            "POST /auth/secrets/{secret_id}/check",
            "POST /auth/secrets/{secret_id}/rotate",
            "DELETE /auth/secrets/{secret_id}",
        ],
        ("Secret",),
    ),
    **{
        f"{method} /board/{kind}s/{{{kind}_id}}": (kind.title(),)
        for kind in ("task", "epic", "sprint", "milestone", "incident")
        for method in ("GET", "PATCH", "DELETE")
    },
    "POST /board/tasks/{task_id}/status": ("Task",),
    "POST /board/tasks/{task_id}/dependencies": ("Task",),
    **dict.fromkeys(
        [
            "GET /spec/specs/{spec_id}",
            "PUT /spec/specs/{spec_id}",
            "GET /spec/specs/{spec_id}/markdown",
            "PUT /spec/specs/{spec_id}/markdown",
            "GET /spec/specs/{spec_id}/manifest",
            "PUT /spec/specs/{spec_id}/manifest",
            "POST /spec/specs/{spec_id}/clarify",
            "POST /spec/specs/{spec_id}/plan",
            "POST /spec/specs/{spec_id}/approve",
            "POST /spec/specs/{spec_id}/reject",
            "POST /spec/specs/{spec_id}/request-changes",
            "POST /spec/specs/{spec_id}/tasks",
            "GET /spec/specs/{spec_id}/versions",
        ],
        ("Spec",),
    ),
    "GET /spec/specs/{spec_id}/versions/{version_number}": ("Spec", "SpecVersion"),
    "GET /spec/specs/{spec_id}/versions/{from_version}/diff/{to_version}": (
        "Spec",
        "SpecVersion",
        "SpecVersion",
    ),
    "POST /spec/tasks/{task_id}/validate": ("Task",),
    "GET /spec/constitution/{project_id}": ("Project",),
    "GET /projects/{project_id}/specs": ("Project",),
    "GET /world/entities/{entity_id}": ("WorldEntity",),
    "GET /world/entities/{entity_id}/changes": ("WorldEntity",),
    "POST /world/facts/{fact_id}/supersede": ("WorldFact",),
    "POST /world/facts/{fact_id}/retract": ("WorldFact",),
    "POST /world/proposals/{proposal_id}/decision": ("WorldProposal",),
    "POST /world/merges/{merge_id}/revert": ("WorldEntityMerge",),
    "GET /world/decisions/{decision_id}": ("WorldDecision",),
    "GET /world/decisions/{decision_id}/drift": ("WorldDecision",),
    **dict.fromkeys(
        [
            "GET /workflow/runs/{run_id}",
            "POST /workflow/runs/{run_id}/transition",
            "GET /workflow/runs/{run_id}/stations",
            "GET /workflow/runs/{run_id}/red-team",
            "POST /workflow/runs/{run_id}/red-team",
        ],
        ("WorkflowRun",),
    ),
    "POST /workflow/runs/{run_id}/gates/{gate}/sync": ("WorkflowRun", "=spec"),
    **dict.fromkeys(
        [
            "GET /workflow/editor/definitions/{name}",
            "POST /workflow/editor/definitions/{name}/fork",
            "PUT /workflow/editor/definitions/{name}/draft",
            "POST /workflow/editor/definitions/{name}/draft/validate",
            "POST /workflow/editor/definitions/{name}/publish",
            "GET /workflow/editor/definitions/{name}/revisions",
            "GET /workflow/editor/definitions/{name}/diff",
            "POST /workflow/editor/definitions/{name}/rollback",
            "POST /workflow/editor/definitions/{name}/archive",
            "GET /workflow/editor/definitions/{name}/export",
        ],
        ("WorkflowDefinition",),
    ),
    "GET /workflow/editor/definitions/{name}/revisions/{revision}": (
        "WorkflowDefinition",
        "Revision",
    ),
    **dict.fromkeys(
        [
            "GET /agent/runs/{run_id}",
            "POST /agent/runs/{run_id}/replay",
            "POST /agent/runs/{run_id}/fork",
            "GET /agent/runs/{run_id}/events",
            "GET /observability/runs/{run_id}/trace",
            "POST /observability/runs/{run_id}/trace",
        ],
        ("AgentRun",),
    ),
    **dict.fromkeys(
        [
            "GET /mcp/connections/{connection_id}/resources",
            "GET /mcp/connections/{connection_id}/resources/read",
            "POST /mcp/connections/{connection_id}/tools/call",
            "GET /mcp/connections/{connection_id}/prompts",
            "POST /mcp/connections/{connection_id}/prompts/get",
            "GET /mcp/connections/{connection_id}/audit",
            "PATCH /mcp/connections/{connection_id}",
            "POST /mcp/connections/{connection_id}/index/reindex",
            "GET /mcp/connections/{connection_id}/index",
        ],
        ("MCPConnection",),
    ),
    **dict.fromkeys(
        [
            "PATCH /marketplace/registries/{registry_id}",
            "DELETE /marketplace/registries/{registry_id}",
            "POST /marketplace/registries/{registry_id}/sync",
        ],
        ("MarketplaceRegistry",),
    ),
    "GET /marketplace/listings/{registry_slug}/{slug}": ("RegistrySlug", "ListingSlug"),
    **dict.fromkeys(
        [
            "POST /marketplace/installations/{installation_id}/update",
            "PATCH /marketplace/installations/{installation_id}",
            "DELETE /marketplace/installations/{installation_id}",
        ],
        ("MarketplaceInstallation",),
    ),
    **dict.fromkeys(
        [
            "GET /benchmarks/submissions/{submission_id}",
            "POST /benchmarks/submissions/{submission_id}/verify",
            "POST /benchmarks/submissions/{submission_id}/publish",
            "POST /benchmarks/submissions/{submission_id}/flag",
        ],
        ("BenchmarkSubmission",),
    ),
    **dict.fromkeys(
        [
            "GET /benchmarks/{slug}/{version}",
            "POST /benchmarks/{slug}/{version}/submissions",
            "GET /benchmarks/{slug}/{version}/submissions",
        ],
        ("SuiteSlug", "SuiteVersion"),
    ),
    "POST /integration/github/repos/{connection_id}/sync": ("RepositoryConnection",),
    **dict.fromkeys(
        [
            "POST /integration/slack/approvals/{approval_id}/notify",
            "GET /approvals/{approval_id}",
            "GET /approvals/{approval_id}/context",
            "GET /approvals/{approval_id}/decisions",
            "POST /approvals/{approval_id}/decision",
            "GET /approvals/{approval_id}/attestation",
        ],
        ("ApprovalRequest",),
    ),
    "GET /attestations/{attestation_id}": ("Attestation",),
    "GET /attestations/{attestation_id}/envelope": ("Attestation",),
    **dict.fromkeys(
        [
            "POST /tasks/{task_id}/held-out-scenarios",
            "GET /tasks/{task_id}/held-out-scenarios",
            "GET /tasks/{task_id}/held-out-scenarios/results",
            "GET /tasks/{task_id}/cost",
            "GET /tasks/{task_id}/estimate-history",
        ],
        ("Task",),
    ),
    **dict.fromkeys(
        [
            "POST /tasks/{task_id}/held-out-scenarios/{scenario_id}/approve",
            "POST /tasks/{task_id}/held-out-scenarios/{scenario_id}/reject",
            "DELETE /tasks/{task_id}/held-out-scenarios/{scenario_id}",
        ],
        ("Task", "HeldOutScenario"),
    ),
    "GET /audit/{entry_id}": ("AuditLog",),
    **dict.fromkeys(
        [
            "GET /incidents/{incident_id}",
            "GET /incidents/{incident_id}/timeline",
            "POST /incidents/{incident_id}/events",
            "POST /incidents/{incident_id}/remediation",
            "GET /incidents/{incident_id}/remediation",
            "GET /incidents/{incident_id}/postmortem",
            "POST /incidents/{incident_id}/postmortem/publish",
        ],
        ("DeclaredIncident",),
    ),
    "GET /observability/analytics/budgets/{budget_id}/status": ("Budget",),
    **dict.fromkeys(
        [
            "GET /integrations/pm/connections/{connection_id}",
            "PATCH /integrations/pm/connections/{connection_id}",
            "DELETE /integrations/pm/connections/{connection_id}",
            "POST /integrations/pm/connections/{connection_id}/test",
            "GET /integrations/pm/connections/{connection_id}/links",
        ],
        ("PMConnection",),
    ),
    **dict.fromkeys(
        [
            "POST /projects/{project_id}/automations",
            "GET /projects/{project_id}/automations",
            "GET /projects/{project_id}/pipeline",
            "PUT /projects/{project_id}/pipeline",
            "POST /projects/{project_id}/deployments",
            "GET /projects/{project_id}/deployments",
            "POST /projects/{project_id}/sprints",
            "GET /projects/{project_id}/sprints",
            "GET /projects/{project_id}/velocity",
            "GET /projects/{project_id}/velocity/export",
            "GET /projects/{project_id}/cfd",
            "GET /projects/{project_id}/cycle-lead-time",
            "GET /projects/{project_id}/access",
            "PUT /projects/{project_id}/visibility",
            "GET /projects/{project_id}/team-access",
            "POST /projects/{project_id}/team-access",
        ],
        ("Project",),
    ),
    "DELETE /projects/{project_id}/team-access/{team_id}": ("Project", "Team"),
    **dict.fromkeys(
        [
            "GET /automations/{rule_id}",
            "PATCH /automations/{rule_id}",
            "POST /automations/{rule_id}/enable",
            "POST /automations/{rule_id}/disable",
            "DELETE /automations/{rule_id}",
            "POST /automations/{rule_id}/test",
            "GET /automations/{rule_id}/executions",
        ],
        ("AutomationRule",),
    ),
    **dict.fromkeys(
        [
            "GET /deployments/{deployment_id}",
            "GET /deployments/{deployment_id}/gate",
            "POST /deployments/{deployment_id}/decision",
            "POST /deployments/{deployment_id}/cancel",
            "POST /deployments/{deployment_id}/rollback",
            "POST /deployments/{deployment_id}/freeze-override",
        ],
        ("Deployment",),
    ),
    **dict.fromkeys(
        [
            "GET /sprints/{sprint_id}",
            "PATCH /sprints/{sprint_id}",
            "POST /sprints/{sprint_id}/start",
            "POST /sprints/{sprint_id}/complete",
            "POST /sprints/{sprint_id}/cancel",
            "POST /sprints/{sprint_id}/recompute",
            "GET /sprints/{sprint_id}/burndown",
            "GET /sprints/{sprint_id}/report",
            "GET /sprints/{sprint_id}/capacity",
            "PUT /sprints/{sprint_id}/capacity",
            "GET /sprints/{sprint_id}/goal-alignment",
        ],
        ("Sprint",),
    ),
    "PATCH /estimation-scales/{scale_id}": ("EstimationScale",),
    **dict.fromkeys(
        [
            "GET /teams/{team_id}",
            "PATCH /teams/{team_id}",
            "POST /teams/{team_id}/archive",
            "GET /teams/{team_id}/members",
            "POST /teams/{team_id}/members",
        ],
        ("Team",),
    ),
    "PATCH /teams/{team_id}/members/{user_id}": ("Team", "User"),
    "DELETE /teams/{team_id}/members/{user_id}": ("Team", "User"),
    "DELETE /access/grants/{grant_id}": ("RoleGrant",),
    "PUT /ao/role-config/{role}": ("=coder",),
    "DELETE /ao/role-config/{role}": ("=coder",),
    **dict.fromkeys(
        [
            "GET /workspaces/{workspace_id}/sso",
            "PUT /workspaces/{workspace_id}/sso",
            "GET /workspaces/{workspace_id}/oidc",
            "PUT /workspaces/{workspace_id}/oidc",
            "POST /workspaces/{workspace_id}/sso/enable",
            "POST /workspaces/{workspace_id}/sso/disable",
            "DELETE /workspaces/{workspace_id}/sso",
            "POST /workspaces/{workspace_id}/sso/test",
            "GET /workspaces/{workspace_id}/scim/tokens",
            "POST /workspaces/{workspace_id}/scim/tokens",
        ],
        ("Workspace",),
    ),
    "DELETE /workspaces/{workspace_id}/scim/tokens/{token_id}": ("Workspace", "ScimToken"),
    **{
        f"{method} /scim/v2/{kind}/{{scim_id}}": (kind,)
        for kind in ("Users", "Groups")
        for method in ("GET", "PUT", "PATCH", "DELETE")
    },
}

#: Query or body parameters naming a resource: ``"METHOD /path param"`` -> its type.
NAMED: dict[str, str] = {
    # Before B makes a spec of its own (B's SPEC-1 has the same id as A's).
    "POST /context/assemble spec_ids": "Spec",
    "POST /context/assemble agent_run_id": "AgentRunLive",
    "GET /board/tasks project_id": "Project",
    "GET /board/tasks epic_id": "Epic",
    "GET /board/tasks sprint_id": "Sprint",
    "POST /board/tasks project_id": "Project",
    "POST /board/tasks epic_id": "Epic",
    "POST /board/tasks sprint_id": "Sprint",
    "POST /board/tasks milestone_id": "Milestone",
    "POST /board/tasks/bulk sprint_id": "Sprint",
    "POST /board/tasks/bulk task_id": "Task",
    "GET /board/epics project_id": "Project",
    "POST /board/epics project_id": "Project",
    "GET /board/sprints project_id": "Project",
    "POST /board/sprints project_id": "Project",
    "POST /board/sprints task_ids": "Task",
    "GET /board/milestones project_id": "Project",
    "POST /board/milestones project_id": "Project",
    "GET /board/incidents project_id": "Project",
    "POST /board/incidents project_id": "Project",
    "POST /spec/constitution project_id": "Project",
    "POST /spec/specs epic_id": "Epic",
    "POST /knowledge/index source_id": "KnowledgeSource",
    "POST /knowledge/sync source_id": "KnowledgeSource",
    "GET /knowledge/document source": "KnowledgeSource",
    "POST /world/facts subject_id": "WorldEntity",
    "POST /world/facts object_id": "WorldEntity",
    "POST /world/proposals subject_id": "WorldEntity",
    "POST /world/merges survivor_id": "WorldEntity",
    "POST /world/merges merged_id": "WorldEntity2",
    "POST /world/decisions fact_ids": "WorldFact",
    "GET /world/facts subject": "WorldEntity",
    "GET /world/facts object": "WorldEntity",
    "GET /board/tasks assignee_id": "User",
    "GET /audit actor_id": "User",
    "GET /audit target_id": "Task",
    "GET /tasks/{task_id}/held-out-scenarios/results workflow_run_id": "WorkflowRun",
    "POST /board/tasks assignee_id": "User",
    "POST /board/tasks/bulk assignee_id": "User",
    "POST /board/tasks/{task_id}/dependencies depends_on_id": "Task",
    "PATCH /board/tasks/{task_id} assignee_id": "User",
    "PATCH /board/tasks/{task_id} epic_id": "Epic",
    "PATCH /board/tasks/{task_id} milestone_id": "Milestone",
    "PATCH /board/tasks/{task_id} project_id": "Project",
    "PATCH /board/tasks/{task_id} sprint_id": "Sprint",
    "PATCH /board/epics/{epic_id} project_id": "Project",
    "PATCH /board/sprints/{sprint_id} project_id": "Project",
    "PATCH /board/sprints/{sprint_id} task_ids": "Task",
    "PATCH /board/milestones/{milestone_id} project_id": "Project",
    "PATCH /board/incidents/{incident_id} project_id": "Project",
    "PATCH /teams/{team_id} parent_team_id": "Team",
    "POST /approvals subject_id": "Task",
    "POST /incidents commander_id": "User",
    "POST /incidents repo_id": "RepositoryConnection",
    "POST /sprints/{sprint_id}/complete next_sprint_id": "Sprint2",
    "POST /world/facts/{fact_id}/supersede subject_id": "WorldEntity",
    "POST /world/facts/{fact_id}/supersede object_id": "WorldEntity",
    "POST /world/proposals object_id": "WorldEntity2",
    "PUT /spec/specs/{spec_id} epic_id": "Epic",
    "POST /workflow/runs task_id": "Task",
    "POST /agent/runs task_id": "Task",
    "GET /policy/rule-evaluations agent_run_id": "AgentRun",
    "GET /marketplace/listings registry_id": "MarketplaceRegistry",
    "POST /marketplace/publish registry_id": "MarketplaceRegistry",
    "POST /marketplace/preview registry_id": "MarketplaceRegistry",
    "POST /marketplace/install registry_id": "MarketplaceRegistry",
    "POST /approvals project_id": "Project",
    "POST /approvals task_id": "Task",
    "POST /approvals agent_run_id": "AgentRun",
    "POST /approvals workflow_run_id": "WorkflowRun",
    "GET /approvals project_id": "Project",
    "GET /attestations agent_run_id": "AgentRun",
    "GET /attestations workflow_run_id": "WorkflowRun",
    "GET /cost/summary scope_id": "Project",
    "GET /cost/timeseries scope_id": "Project",
    "POST /incidents project_id": "Project",
    "GET /incidents project_id": "Project",
    "GET /incidents/reliability project_id": "Project",
    "POST /integrations/alerts/manual project_id": "Project",
    "GET /observability/audit run_id": "AgentRun",
    "GET /observability/analytics/incidents/reliability project_id": "Project",
    "GET /observability/analytics/dora project_id": "Project",
    "GET /observability/analytics/coverage/trend project_id": "Project",
    "POST /integrations/pm/connections project_id": "Project",
    "POST /automations/{rule_id}/test task_id": "Task",
    "POST /projects/{project_id}/deployments agent_run_id": "AgentRun",
    "POST /projects/{project_id}/deployments workflow_run_id": "WorkflowRun",
    "POST /estimation-scales project_id": "Project",
    "GET /estimation-scales project_id": "Project",
    "PUT /sprints/{sprint_id}/capacity member_id": "User",
    "GET /portfolio/velocity project_ids": "Project",
    "POST /teams parent_team_id": "Team",
    "POST /teams/{team_id}/members user_id": "User",
    "GET /access/grants principal_id": "User",
    "GET /access/grants scope_id": "Project",
    "GET /access/effective principal_id": "User",
    "GET /access/effective project_id": "Project",
    "PUT /projects/{project_id}/visibility owner_team_id": "Team",
    "POST /projects/{project_id}/team-access team_id": "Team",
    "GET /ao/role-config project_id": "Project",
    "GET /risk/classifications agent_run_id": "AgentRun",
    "GET /risk/classifications workflow_run_id": "WorkflowRun",
    "PUT /ao/role-config/{role} project_id": "Project",
    "DELETE /ao/role-config/{role} project_id": "Project",
    "POST /mcp/connections id": "MCPConnection",
}

#: Routes listing the caller's resources, and the types whose instances they list.
LISTS: dict[str, tuple[str, ...]] = {
    "GET /auth/api-keys": ("PlatformAPIKey",),
    "GET /auth/secrets": ("Secret",),
    "GET /board/tasks": ("Task",),
    "GET /board/epics": ("Epic",),
    "GET /board/sprints": ("Sprint",),
    "GET /board/milestones": ("Milestone",),
    "GET /board/incidents": ("Incident",),
    "GET /knowledge/sources": ("KnowledgeSource",),
    "GET /world/entities": ("WorldEntity",),
    "GET /world/facts": ("WorldFact",),
    "GET /world/proposals": ("WorldProposal",),
    "GET /world/ontology/classes": ("WorldClass",),
    "GET /world/ontology/relations": ("WorldRelation",),
    "GET /workflow/editor/definitions": ("WorkflowDefinition",),
    "GET /policy/rule-evaluations": ("PolicyRuleEvaluation",),
    "GET /mcp/connections": ("MCPConnection",),
    "GET /marketplace/registries": ("MarketplaceRegistry",),
    "GET /marketplace/listings": ("MarketplaceListing",),
    "GET /marketplace/installations": ("MarketplaceInstallation",),
    "GET /marketplace/audit": ("MarketplaceAuditLog",),
    "GET /approvals": ("ApprovalRequest",),
    "GET /attestations": ("Attestation",),
    "GET /audit": ("AuditLog",),
    "GET /incidents": ("DeclaredIncident",),
    "GET /observability/analytics/budgets": ("Budget",),
    "GET /integrations/pm/connections": ("PMConnection",),
    "GET /estimation-scales": ("EstimationScale",),
    "GET /teams": ("Team",),
    "GET /access/grants": ("RoleGrant",),
    "GET /projects": ("Project",),
    "GET /benchmarks": ("SuiteSlug",),
    "GET /risk/classifications": ("RiskClassification",),
    # Per-gate counts only: a gate only A observed must not appear in B's.
    "GET /risk/agreement": ("RiskGate",),
    "GET /scim/v2/Users": ("Users",),
    "GET /scim/v2/Groups": ("Groups",),
}

#: Bodies for writes, so a call reaches its handler (others are built from the schema).
#: ``"{Type}"`` stands for the caller's own instance of that type.
#: A key ``"METHOD /path param"`` gives the body for naming ``param`` there.
BODIES: dict[str, Any] = {
    "POST /board/tasks/bulk": [{"task_id": "{Task}"}],
    "POST /world/facts subject_id": {"phrase": "x", "value": "x"},
    "POST /world/facts object_id": {"phrase": "x", "subject_id": "{WorldEntity}"},
    "POST /world/facts/{fact_id}/supersede subject_id": {"phrase": "x", "value": "x"},
    "POST /world/facts/{fact_id}/supersede object_id": {
        "phrase": "x",
        "subject_id": "{WorldEntity}",
    },
    "POST /world/proposals object_id": {"phrase": "x", "subject_id": "{WorldEntity}"},
    "POST /sprints/{sprint_id}/complete": {"carryover": "next_sprint"},
    "POST /world/merges": {"survivor_id": "{WorldEntity}", "merged_id": "{WorldEntity2}"},
    "POST /agent/runs": {"objective": "x"},
    "POST /approvals": {"gate_type": "spec", "subject_type": "task"},
    "POST /projects/{project_id}/automations": _RULE,
    "POST /workflow/editor/definitions/{name}/fork": {"name": "b_fork", "title": "B"},
    "PUT /spec/specs/{spec_id}": {"id": "SPEC-1", "name": "x"},
    "PUT /spec/specs/{spec_id}/manifest": {"content": "id: SPEC-1\nname: x\n"},
}

#: Writes that create the caller's own resource at an identifier it names, each with why.
#: B naming A's identifier there must answer as naming one of nothing and leave A's alone;
#: they run after every other call, so what they make cannot answer another check.
UPSERTS: dict[str, str] = {
    **dict.fromkeys(
        ["PUT /spec/specs/{spec_id}", "PUT /spec/specs/{spec_id}/manifest"],
        "Create or update a spec in the caller's own spec root: a spec's id derives from "
        "its key, which each workspace numbers from SPEC-1",
    ),
    "POST /mcp/connections id": "A connection's id is a name its workspace chooses",
    "POST /observability/runs/{run_id}/trace": "Records the caller's own trace for a run id; "
    "traces are kept per workspace",
}

SPEC_EPIC = ("POST /spec/specs epic_id", "PUT /spec/specs/{spec_id} epic_id")
WORLD_PROPOSE = ("POST /world/proposals", "MCP world_propose")

#: OPEN DEFECTS the matrix found and this change did not fix (PT-13, #189), each with the
#: behaviour required. Every entry must still fail: once fixed, it must be removed here.
OPEN_DEFECTS: dict[str, str] = {
    **dict.fromkeys(
        [
            *(f"POST /board/tasks {p}" for p in ("project_id", "epic_id", "sprint_id")),
            *(f"POST /board/tasks {p}" for p in ("milestone_id", "assignee_id")),
            *(f"POST /board/tasks/bulk {p}" for p in ("sprint_id", "assignee_id")),
            *(f"POST /board/{kind}s project_id" for kind in ("epic", "sprint", "milestone")),
            "POST /board/incidents project_id",
            "POST /board/sprints task_ids",
            *(f"PATCH /board/tasks/{{task_id}} {p}" for p in ("project_id", "epic_id")),
            *(f"PATCH /board/tasks/{{task_id}} {p}" for p in ("sprint_id", "milestone_id")),
            "PATCH /board/tasks/{task_id} assignee_id",
            "PATCH /board/epics/{epic_id} project_id",
            "PATCH /board/sprints/{sprint_id} project_id",
            "PATCH /board/sprints/{sprint_id} task_ids",
            "PATCH /board/milestones/{milestone_id} project_id",
            "PATCH /board/incidents/{incident_id} project_id",
            "GET /sprints/{sprint_id}/goal-alignment (A changed)",
        ],
        "#336 Board items (the database board): a task, epic, sprint, milestone or incident must "
        "refuse a project, epic, sprint, milestone, task or assignee of another workspace",
    ),
    **dict.fromkeys(
        ["POST /spec/constitution project_id", *SPEC_EPIC],
        "#337 Specs: a spec or constitution must refuse an epic or project of another workspace",
    ),
    **dict.fromkeys(
        [f"{route} {p}" for route in WORLD_PROPOSE for p in ("subject_id", "object_id")],
        "#337 World model: a proposal must refuse an entity of another workspace",
    ),
    **dict.fromkeys(
        [
            f"POST /approvals {p}"
            for p in ("project_id", "task_id", "subject_id", "agent_run_id", "workflow_run_id")
        ],
        "#337 Approvals: a gate must refuse a project, task or run of another workspace; "
        "B's pending gate on A's subject also blocks A's own gate (the pending-unique index "
        "has no workspace)",
    ),
    **dict.fromkeys(
        [
            *(f"POST /incidents {p}" for p in ("project_id", "commander_id", "repo_id")),
            "POST /integrations/alerts/manual project_id",
        ],
        "#338 Incidents: an incident must refuse a project, person or repository of another "
        "workspace",
    ),
    "POST /integrations/pm/connections project_id": "#338 PM connections: must refuse a project "
    "of another workspace, answering as for a project that does not exist; B's connection on "
    "A's project also blocks A's own (project and provider are unique across workspaces)",
    **dict.fromkeys(
        [
            f"POST /projects/{{project_id}}/deployments {p}"
            for p in ("agent_run_id", "workflow_run_id")
        ],
        "#338 Deployments: must refuse a run of another workspace",
    ),
    "POST /estimation-scales project_id": "#338 Estimation scales: must refuse a project of "
    "another workspace",
    "PUT /sprints/{sprint_id}/capacity member_id": "#338 Sprint capacity: must refuse a member who "
    "is not a person of the workspace",
    "POST /teams/{team_id}/members user_id": "#338 Teams: must refuse a person of another "
    "workspace",
    **dict.fromkeys(
        ["PUT /ao/role-config/{role} project_id", "DELETE /ao/role-config/{role} project_id"],
        "#338 Role configuration: must refuse a project of another workspace",
    ),
}

#: Reads whose answer for A's own resource the matrix cannot make differ from one for
#: nothing, each with why. B's answer must still match the one for nothing.
NOT_SEEDED: dict[str, str] = {
    "GET /incidents/{incident_id}/postmortem": "A postmortem exists only once an incident "
    "is resolved through its lifecycle; the read answers 'no postmortem' otherwise",
    "GET /tasks/{task_id}/held-out-scenarios/results workflow_run_id": "Results exist only "
    "once a run has executed the held-out scenarios",
    "GET /observability/audit run_id": "The observability audit trail fills only from a "
    "running agent's events",
}

#: Query parameters a route needs, so a call reaches its handler.
QUERIES: dict[str, dict[str, Any]] = {
    "GET /projects/{project_id}/cfd": {"start": "2026-01-01", "end": "2026-02-01"},
    "GET /workflow/editor/definitions/{name}/diff": {"from": 1, "to": 1},
    "GET /mcp/connections/{connection_id}/resources/read": {
        "uri": "confluence://engineering/page-2"
    },
    "PATCH /marketplace/installations/{installation_id}": {"pinned": True},
    "GET /cost/summary": {"scope": "project"},
    "GET /cost/timeseries": {"scope": "project"},
    "GET /knowledge/document": {"path": "a.md"},
}

#: The types workspace B makes too, so B's own calls name B's parents. Types a workspace
#: names itself (spec key, definition name, connection id, slug) are left out.
B_SEEDS = {
    "Project",
    "Workspace",
    "User",
    "Gate",
    "Revision",
    "SpecVersion",
    "Epic",
    "Sprint",
    "Sprint2",
    "SprintStarted",
    "Milestone",
    "Incident",
    "Task",
    "Team",
    "EstimationScale",
    "AutomationRule",
    "WorldEntity",
    "WorldEntity2",
    "WorldFact",
    "Spec",
    "AgentRun",
    "WorkflowRun",
    "Pipeline",
}

#: Models the API serves only under a parent's identifier (swept in :data:`ROUTES`) or for
#: the caller's own workspace with no identifier at all, and the routes that serve them.
SERVED_UNDER: dict[str, tuple[str, ...]] = {
    "AoWorkspaceSettings": ("GET /ao/settings", "PUT /ao/settings"),
    "AgentRoleConfig": ("PUT /ao/role-config/{role}", "DELETE /ao/role-config/{role}"),
    "AgentRunEvent": ("GET /agent/runs/{run_id}/events",),
    "ApprovalDecision": ("GET /approvals/{approval_id}/decisions",),
    "PolicyOverrideGrant": ("POST /approvals/{approval_id}/decision",),
    "AutomationExecution": ("GET /automations/{rule_id}/executions",),
    "CostEvent": ("GET /tasks/{task_id}/cost", "GET /cost/summary", "GET /cost/timeseries"),
    "CoverageSnapshot": ("GET /observability/analytics/coverage/trend",),
    "SkillProfileSnapshot": ("GET /observability/analytics/coverage/rollup",),
    "DeploymentApproval": ("POST /deployments/{deployment_id}/decision",),
    "DeploymentTransition": ("GET /deployments/{deployment_id}",),
    "DeploymentCheckResult": ("GET /deployments/{deployment_id}/gate",),
    "Environment": ("GET /projects/{project_id}/pipeline",),
    "EnvironmentPipeline": ("GET /projects/{project_id}/pipeline",),
    "ExternalIdentity": ("GET /scim/v2/Users/{scim_id}",),
    "ScimGroup": ("GET /scim/v2/Groups/{scim_id}",),
    "ScimGroupMember": ("GET /scim/v2/Groups/{scim_id}",),
    "SsoConfiguration": ("GET /workspaces/{workspace_id}/sso",),
    "SsoDomain": ("GET /workspaces/{workspace_id}/sso",),
    "OidcConfiguration": ("GET /workspaces/{workspace_id}/oidc",),
    "KnowledgeSyncRun": ("GET /mcp/connections/{connection_id}/index",),
    "MCPIndexedResource": ("GET /mcp/connections/{connection_id}/index",),
    "RetrievalChunk": ("POST /knowledge/search", "GET /knowledge/document"),
    "RetrievalTermStat": ("POST /knowledge/search",),
    "RetrievalWorkspaceStat": ("POST /knowledge/search",),
    "PMTaskLink": ("GET /integrations/pm/connections/{connection_id}/links",),
    "ProjectTeamAccess": ("GET /projects/{project_id}/team-access",),
    "RedTeamRecord": ("GET /workflow/runs/{run_id}/red-team",),
    "RunRecording": ("POST /agent/runs/{run_id}/replay", "POST /agent/runs/{run_id}/fork"),
    "SkillProfile": ("GET /marketplace/installations",),
    "SprintBurndownSnapshot": ("GET /sprints/{sprint_id}/burndown",),
    "SprintMemberCapacity": ("GET /sprints/{sprint_id}/capacity",),
    "SprintScopeEvent": ("GET /sprints/{sprint_id}/report",),
    "SprintVelocity": ("GET /projects/{project_id}/velocity",),
    "TaskDependency": ("POST /board/tasks/{task_id}/dependencies",),
    "TaskEstimateEvent": ("GET /tasks/{task_id}/estimate-history",),
    "TaskStatusEvent": ("GET /projects/{project_id}/cfd",),
    "TeamMember": ("GET /teams/{team_id}/members",),
    "WorkflowDefinitionRevision": ("GET /workflow/editor/definitions/{name}/revisions",),
    "WorkflowStationVisit": ("GET /workflow/runs/{run_id}/stations",),
    "WorldDerivation": ("POST /world/derive",),
    "WorldEvidence": ("GET /world/entities/{entity_id}",),
    "WorldExtractionDrop": ("POST /world/ingest",),
    "WorldFactFeedback": ("GET /world/facts",),
    "RiskGateObservation": ("GET /risk/agreement",),
    # Counted, never listed, in the caller's own factory metrics (no identifier taken).
    "ChangeOutcome": ("GET /observability/analytics/factory",),
    "RunIntervention": ("GET /observability/analytics/factory",),
}

#: Workspace-scoped models with no API surface, each with the reviewed reason.
NO_API_SURFACE: dict[str, str] = {
    "HeldAutomationAction": "Read only by the release hook of its approval request, in that "
    "request's workspace (swept as ApprovalRequest); no route serves it",
    **dict.fromkeys(
        ["IncidentAlert", "IncidentEvent", "RemediationPlan", "Postmortem", "PostmortemActionItem"],
        "The incident routes keep incidents in process memory per workspace "
        "(IncidentServiceRegistry), swept as DeclaredIncident; no API code reads this table",
    ),
    **dict.fromkeys(
        ["AgentRepoWorkspace", "PRGroup", "SandboxInstance", "SpecDocument", "PolicyProfile"],
        "No code outside its model and migrations reads or writes this table",
    ),
    "APIKey": "No code reads or writes this table; the API keeps its keys in platform_api_key",
    "Constitution": "The spec engine keeps a project's constitution in the workspace's own spec "
    "root (swept through GET /spec/constitution/{project_id}); no code reads this table",
    "AuditChainHead": "The head of a workspace's audit hash chain, kept by the audit writer; "
    "no route reads or names it",
    "OAuthAccount": "Written and read only while its own person signs in through OAuth; no "
    "route names it",
    "SignInLink": "Redeemed by its one-time secret (GET /auth/sign-in-link), never by an id",
    "SubAgentRun": "Written and read only by the multi-agent coordinator inside a run",
    **dict.fromkeys(
        ["TraceabilityCriterionLink", "TraceabilitySpecRollup"],
        "The traceability projection: written and read by the projection and attestation "
        "services inside a run; no route reads it or takes its identifier",
    ),
}

#: Routes taking a path parameter that the matrix does not sweep, each with why.
NOT_AN_IDENTIFIER: dict[str, str] = {
    "WS /ws/spec/{spec_id}": "A WebSocket: the spec room resolves the spec in the caller's own "
    "spec root, as the spec routes swept here do",
    "POST /integrations/alerts/{provider}/webhook": "Webhook (a non-goal of #189); its "
    "workspace selector is open defect #321 (UNAUTHENTICATED_WORKSPACE_SELECTORS)",
    **dict.fromkeys(
        [
            "GET /agent/runs/{run_id}/record/inputs",
            "POST /agent/runs/{run_id}/record/events",
            "POST /agent/runs/{run_id}/record/handoff",
        ],
        "Run token only (require_run_token): every credential but the live token of the run "
        "in the path gets the same 401, so B's admin is refused whatever it names; "
        "test_run_token.py refuses another workspace's run token alike for any run id",
    ),
    **dict.fromkeys(
        [
            "POST /integrations/pm/webhooks/jira/{connection_id}",
            "POST /integrations/pm/webhooks/linear/{connection_id}",
            "POST /integrations/pm/webhooks/github_issues/{connection_id}",
        ],
        "Webhook (a non-goal of #189): authenticated by the connection's own shared secret",
    ),
    **dict.fromkeys(
        [
            "GET /auth/saml/{slug}/metadata",
            "GET /auth/saml/{slug}/login",
            "POST /auth/saml/{slug}/acs",
            "POST /auth/saml/{slug}/slo",
            "GET /auth/saml/{slug}/slo",
            "GET /auth/oidc/{slug}/login",
            "GET /auth/oidc/{slug}/callback",
        ],
        "Pre-authentication sign-in (a non-goal of #189): the slug selects whose identity "
        "provider a person signs in with",
    ),
    **dict.fromkeys(
        [
            "GET /public/leaderboard/{slug}/{version}",
            "GET /public/leaderboard/{slug}/{version}/submissions/{submission_id}",
            "GET /public/leaderboard/{slug}/{version}/submissions/{submission_id}/bundles",
        ],
        "Public by design: serves only published, public suites and submissions",
    ),
}

#: Query or body parameters named like an identifier that name no workspace resource of
#: another workspace, each with why (every other one is in :data:`NAMED`).
NOT_NAMED: dict[str, str] = {
    **dict.fromkeys(
        [
            "POST /board/tasks id",
            "PATCH /board/tasks/{task_id} id",
            "POST /board/epics id",
            "PATCH /board/epics/{epic_id} id",
            "POST /board/sprints id",
            "PATCH /board/sprints/{sprint_id} id",
            "POST /board/milestones id",
            "PATCH /board/milestones/{milestone_id} id",
            "POST /board/incidents id",
            "PATCH /board/incidents/{incident_id} id",
            "PUT /spec/specs/{spec_id} id",
            "POST /scim/v2/Users id",
            "PUT /scim/v2/Users/{scim_id} id",
            "POST /scim/v2/Groups id",
            "PUT /scim/v2/Groups/{scim_id} id",
        ],
        "A response field echoed in the request model; the handler ignores it",
    ),
    **dict.fromkeys(
        [
            *(f"POST /board/{kind}s spec_id" for kind in ("task", "epic")),
            *(f"PATCH /board/{kind}s/{{{kind}_id}} spec_id" for kind in ("task", "epic")),
        ],
        "A spec's id derives from its key, which each workspace numbers from SPEC-1: it "
        "names the caller's own spec",
    ),
    **dict.fromkeys(
        ["POST /spec/draft epic_id", "POST /spec/draft project_id"],
        "Refused before any lookup in the matrix: drafting needs a model provider",
    ),
    **dict.fromkeys(
        [
            "GET /observability/analytics/coverage/trend repo_id",
            "PUT /projects/{project_id}/pipeline repo_id",
        ],
        "A repository's name, matched inside the project named alongside it",
    ),
    "PUT /workspaces/{workspace_id}/oidc client_id": "The identity provider's client id",
    "POST /agent/runs/{run_id}/record/handoff run_id": "Replaced by the run token's own run "
    "(test_the_handoff_is_stored_under_its_own_run_whatever_the_body_says)",
    "POST /agent/runs/{run_id}/record/handoff task_id": "Kept only in the run's own handoff; "
    "close-out acts on the task in the run's stored objective, never on this field",
    "GET /observability/audit connection_id": "An MCP connection's id: a name its workspace "
    "chooses, matched inside the caller's own audit trail",
    "POST /integrations/alerts/{provider}/webhook workspace_id": "Open defect #321",
    "POST /integrations/alerts/{provider}/webhook project_id": "Webhook (a non-goal of #189)",
}


def workspace_scoped_models() -> set[str]:
    import scf_db.models  # noqa: F401  (registers every model)
    from scf_db.base import WorkspaceScopedModel

    found: set[str] = set()
    stack = list(WorkspaceScopedModel.__subclasses__())
    while stack:
        cls = stack.pop()
        stack.extend(cls.__subclasses__())
        if not cls.__dict__.get("__abstract__"):
            found.add(cls.__name__)
    return found


def coverage() -> list[str]:
    """Every workspace-scoped model, and every identifier a route takes, is accounted for."""
    from route_matrix import routes

    from scf_api.main import create_app
    from scf_api.settings import Settings

    app = create_app(Settings(public_leaderboard_enabled=True))
    spec = app.openapi()
    wrong: list[str] = []
    models = workspace_scoped_models()
    named = {t for ts in (*ROUTES.values(), *LISTS.values()) for t in ts} | set(NAMED.values())
    mapped = (named & models) | SERVED_UNDER.keys()
    unmapped = models - mapped - NO_API_SURFACE.keys()
    wrong += [f"workspace-scoped model with no route or reason: {m}" for m in sorted(unmapped)]
    wrong += [f"not a workspace-scoped model: {m}" for m in sorted(NO_API_SURFACE.keys() - models)]
    wrong += [
        f"mapped and without API surface: {m}" for m in sorted(NO_API_SURFACE.keys() & mapped)
    ]
    seeded = {name for name, _, _ in SEEDS}
    wrong += [f"named but never seeded: {t}" for t in sorted(named - seeded) if t[0] != "="]

    walked = {f"{method} {path}" for method, path, _ in routes(app)}
    taking = {key for key in walked if "{" in key} - {k for k in walked if "/workspaces/" in k}
    listed = ROUTES.keys() | NOT_AN_IDENTIFIER.keys() | {k for k in ROUTES if "/workspaces/" in k}
    wrong += [f"route taking an identifier, not swept: {k}" for k in sorted(taking - listed)]
    served = {route for routes_ in SERVED_UNDER.values() for route in routes_}
    tables = served | listed | LISTS.keys() | {e.rsplit(" ", 1)[0] for e in NAMED} | UPSERTS.keys()
    stale = {k.rsplit(" ", 1)[0] if k.count(" ") > 1 else k for k in tables} - walked
    wrong += [f"route the app does not serve: {k}" for k in sorted(stale)]

    found: set[str] = set()
    taken: set[str] = set()
    for path, operations in spec["paths"].items():
        for method, operation in operations.items():
            key = f"{method.upper()} {path}"
            params = [p["name"] for p in operation.get("parameters", []) if p["in"] == "query"]
            body = operation.get("requestBody", {}).get("content", {}).get("application/json", {})
            schema = body.get("schema", {})
            schema = schema.get("items", schema)
            if "$ref" in schema:
                schema = spec["components"]["schemas"][schema["$ref"].rsplit("/", 1)[-1]]
            params += list(schema.get("properties", {}))
            taken |= {f"{key} {p}" for p in params}
            found |= {f"{key} {p}" for p in params if p == "id" or p.endswith(("_id", "_ids"))}
    # Workspace selectors are check_workspace_selector_scoped's (the alerts webhook's is #321).
    found -= {k for k in found if k.endswith(" workspace_id") and "/alerts/" not in k}
    accounted = NAMED.keys() | NOT_NAMED.keys()
    wrong += [f"identifier parameter, not swept: {k}" for k in sorted(found - accounted)]
    wrong += [f"parameter the app does not take: {k}" for k in sorted(accounted - taken)]
    return wrong


_BACKENDS = (
    "BOARD",
    "AUDIT",
    "APPROVAL",
    "APIKEY",
    "PROJECTION",
    "POLICY_AUDIT",
    "PM_LINK",
    "OVERRIDE_GRANT",
    "SECRET",
)


def _clear_cached_services() -> None:
    """Drop every cached settings object and service, so they are built against this env."""
    for name, module in list(sys.modules.items()):
        if name.startswith("scf_"):
            for value in list(vars(module).values()):
                if callable(getattr(value, "cache_clear", None)):
                    value.cache_clear()


class Tenants:
    """Two workspaces with an admin each, on one app and one SQLite database."""

    def __init__(self, client: TestClient, factory: Any) -> None:
        self.client = client
        self.factory = factory
        self.spec: dict[str, Any] = client.app.openapi()  # type: ignore[attr-defined]
        self.ids: dict[uuid.UUID, dict[str, Any]] = {WORKSPACE_A: {}, WORKSPACE_B: {}}
        self.workspace = WORKSPACE_A

    def act_as(self, workspace: uuid.UUID) -> None:
        self.workspace = workspace

    def principal(self) -> Any:
        from scf_api.deps import Principal
        from scf_contracts import UserRole

        return Principal(
            user_id=ADMIN_A if self.workspace == WORKSPACE_A else ADMIN_B,
            workspace_id=self.workspace,
            role=UserRole.ADMIN,
            email="tenant@example.com",
            auth_method="test",
            scopes=["*"],
            decides=True,
        )


@contextmanager
def tenants() -> Iterator[Tenants]:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session, sessionmaker

    from scf_api.auth.service import get_authenticated_principal
    from scf_api.deps import get_current_principal
    from scf_api.main import create_app
    from scf_api.routers.agent import get_agent_enqueue
    from scf_api.routers.integration import get_github_client
    from scf_api.routers.mcp import get_mcp_manager
    from scf_api.routers.scim import require_scim_token
    from scf_api.settings import Settings
    from scf_db.base import Base
    from scf_db.models import RoleGrant, ScimToken, User, Workspace
    from scf_mcp.manager import MCPConnectionManager
    from scf_mcp.testing import FakeTransport

    with tempfile.TemporaryDirectory() as tmp:
        url = f"sqlite:///{tmp}/tenants.db"
        env = {
            "FORGE_DATABASE_URL": url,
            "FORGE_SECRET_KEY": "tenant-matrix-master-key-0123456789",
            "FORGE_SPEC_ROOT": f"{tmp}/specs",
            "FORGE_RATELIMIT_ENABLED": "false",
            "FORGE_ALLOW_SCRIPTED_AGENT": "true",
            "FORGE_DEV_INSECURE": "1",
            **{f"FORGE_{name}_BACKEND": "db" for name in _BACKENDS},
        }
        engine = create_engine(url)
        Base.metadata.create_all(engine)
        factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
        with factory() as session:
            for ws, user, slug in ((WORKSPACE_A, ADMIN_A, "a"), (WORKSPACE_B, ADMIN_B, "b")):
                session.add(Workspace(id=ws, name=slug.upper(), slug=f"tenant-{slug}"))
                session.flush()
                session.add(User(id=user, workspace_id=ws, email=f"{slug}@example.com"))
                session.add(
                    RoleGrant(
                        workspace_id=ws,
                        principal_type="user",
                        principal_id=user,
                        scope_type="workspace",
                        scope_id=ws,
                        role="admin",
                    )
                )
            session.commit()
        # Triggers fire no Celery task: the sweep needs no broker.
        with (
            mock.patch.dict(os.environ, env),
            mock.patch("scf_api.services.automations.ApiAutomationDispatcher.dispatch"),
            # Module-global services: built against this env, the process's restored on exit.
            mock.patch("scf_api.auth.service._default_service", None),
            mock.patch("scf_api.observability.service._default_service", None),
        ):
            _clear_cached_services()
            try:
                app = create_app(Settings())
                with TestClient(app, raise_server_exceptions=False) as client:
                    world = Tenants(client, factory)
                    app.dependency_overrides[get_current_principal] = world.principal
                    app.dependency_overrides[get_authenticated_principal] = world.principal
                    # SCIM authenticates a workspace's SCIM token, not a principal.
                    app.dependency_overrides[require_scim_token] = lambda: ScimToken(
                        workspace_id=world.workspace, name="t", token_hash="", token_prefix="t"
                    )
                    # Live servers stand in, so a call gets past "not configured" to its lookup.
                    manager = MCPConnectionManager(transport_factory=lambda _: FakeTransport())
                    app.dependency_overrides[get_mcp_manager] = lambda: manager
                    app.dependency_overrides[get_github_client] = lambda: mock.Mock()
                    app.dependency_overrides[get_agent_enqueue] = lambda: lambda _objective: None
                    yield world
            finally:
                _clear_cached_services()
                engine.dispose()


def _schema_body(spec: dict[str, Any], method: str, path: str) -> Any:
    """A minimal body the route's request schema accepts (required fields only)."""
    components = spec.get("components", {}).get("schemas", {})

    def build(schema: dict[str, Any]) -> Any:
        if "$ref" in schema:
            return build(components[schema["$ref"].rsplit("/", 1)[-1]])
        for key in ("allOf", "anyOf", "oneOf"):
            if key in schema:
                options = [o for o in schema[key] if o.get("type") != "null"]
                return build(options[0])
        if "const" in schema:
            return schema["const"]
        if "enum" in schema:
            return schema["enum"][0]
        if schema.get("default") is not None:
            return schema["default"]
        kind = schema.get("type")
        if kind == "object" or "properties" in schema:
            required = schema.get("required", [])
            return {k: build(v) for k, v in schema.get("properties", {}).items() if k in required}
        if kind == "array":
            return [build(schema.get("items", {})) for _ in range(schema.get("minItems", 0))]
        if kind == "string":
            return {
                "uuid": str(uuid.uuid4()),
                "date-time": "2026-01-01T00:00:00Z",
                "date": "2026-01-01",
                "uri": "https://example.com",
                "email": "x@example.com",
            }.get(schema.get("format", ""), "x" * max(1, schema.get("minLength", 1)))
        if kind in ("integer", "number"):
            return max(1, schema.get("minimum", 1))
        if kind == "boolean":
            return False
        return None

    operation = spec["paths"][_openapi_path(spec, path)].get(method.lower(), {})
    content = operation.get("requestBody", {}).get("content", {})
    schema = content.get("application/json", {}).get("schema")
    return None if schema is None else build(schema)


def _openapi_path(spec: dict[str, Any], path: str) -> str:
    """The schema's path template that ``path`` (a template or a concrete path) matches."""
    if path in spec["paths"]:
        return path
    return next(p for p in spec["paths"] if re.fullmatch(re.sub(r"{[^}]+}", "[^/]+", p), path))


def _fill(value: Any, ids: dict[str, Any]) -> Any:
    """Replace each ``"{Type}"`` in ``value`` with that type's identifier from ``ids``."""
    if isinstance(value, str):
        match = re.fullmatch(r"{(\w+)}", value)
        return str(ids[match.group(1)]) if match else value
    if isinstance(value, dict):
        return {k: _fill(v, ids) for k, v in value.items()}
    if isinstance(value, list):
        return [_fill(v, ids) for v in value]
    return value


class _Fixed(str):
    """A path value that names no resource (``"=value"`` in :data:`ROUTES`)."""


def _missing(value: Any) -> Any:
    """An identifier of the same shape as ``value`` that names nothing."""
    if isinstance(value, _Fixed):
        return value
    try:
        uuid.UUID(str(value))
    except ValueError:
        return f"{value}0" if str(value)[-1:].isdigit() else f"{value}-none"
    return uuid.uuid4()


def _uuids(ids: dict[str, Any]) -> set[str]:
    found = set()
    for value in ids.values():
        try:
            found.add(str(uuid.UUID(str(value))))
        except ValueError:
            continue
    return found


def seed(world: Tenants, workspace: uuid.UUID, names: set[str] | None = None) -> None:
    """Create ``workspace``'s instance of each type in :data:`SEEDS` (of ``names`` if given)."""
    import scf_db.models as models

    world.act_as(workspace)
    ids = world.ids[workspace]
    own = {"workspace": workspace, "admin": ADMIN_A if workspace == WORKSPACE_A else ADMIN_B}
    for name, how, values in SEEDS:
        if names is not None and name not in names:
            continue
        filled = _fill(values, ids)
        if how.startswith("="):
            ids[name] = own.get(how[1:], how[1:])
            continue
        if " " in how:
            route, _, key = how.partition("#")
            method, template = route.split(" ", 1)
            path = re.sub(r"{(\w+)}", lambda m: str(ids[m.group(1)]), template)
            body = {**(_schema_body(world.spec, method, path) or {}), **filled}
            resp = world.client.request(method, path, json=body)
            assert resp.status_code < 300, (
                f"seeding {name}: {how} -> {resp.status_code} {resp.text}"
            )
            ids[name] = resp.json().get(key or "id") or name
            continue
        columns = {
            k: uuid.UUID(filled[k]) if re.fullmatch(r"{\w+}", str(v)) else v
            for k, v in values.items()
        }
        row = getattr(models, how)(id=uuid.uuid4(), workspace_id=workspace, **columns)
        with world.factory() as session:
            session.add(row)
            session.commit()
        ids[name] = row.id


def _send(
    world: Tenants, key: str, values: list[Any], named: tuple[str, Any] | None = None
) -> tuple[int, str]:
    """Call ``key`` with ``values`` in its path (and ``named`` in its query or body).

    The body is the caller's own: :data:`BODIES` filled with the caller's identifiers. The
    answer's text has every identifier sent replaced, so two answers compare as equal when
    they differ only in the identifier named.
    """
    method, path = key.split(" ", 1)
    for param, value in zip(re.findall(r"{(\w+)}", path), values, strict=True):
        path = path.replace("{" + param + "}", str(value))
    entry = f"{key} {named[0]}" if named else key
    if entry in BODIES or key in BODIES:
        body = _fill(BODIES.get(entry, BODIES.get(key)), world.ids[world.workspace])
    else:
        body = _schema_body(world.spec, method, key.split(" ", 1)[1])
    query = dict(QUERIES.get(key, {}))
    sent = [str(v) for v in values]
    if named is not None:
        param, value = named
        sent.append(str(value))
        value = [str(value)] if param.endswith("_ids") else str(value)
        if method == "GET" or _in_query(world.spec, key, param):
            query[param] = value
        elif isinstance(body, list):
            body = [{**item, param: value} for item in body]
        else:
            body = {**(body or {}), param: value}
    resp = world.client.request(method, path, params=query, json=body)
    text = resp.text
    for value in sent:
        text = text.replace(value, "<id>")
    return resp.status_code, text


#: Routes that take a body to answer a query and change nothing.
READ_POSTS = {"POST /context/assemble"}


def _reads(key: str) -> bool:
    return key.startswith("GET ") or key in READ_POSTS


def _in_query(spec: dict[str, Any], key: str, param: str) -> bool:
    method, path = key.split(" ", 1)
    operation = spec["paths"][path][method.lower()]
    return any(p["name"] == param and p["in"] == "query" for p in operation.get("parameters", []))


def sweep(world: Tenants) -> list[str]:
    """Every way workspace B can name A's resources, and what each answered."""
    wrong: list[str] = []
    theirs, mine = world.ids[WORKSPACE_A], world.ids[WORKSPACE_B]

    def of(ids: dict[str, Any], names: tuple[str, ...]) -> list[Any]:
        return [_Fixed(n[1:]) if n.startswith("=") else ids[n] for n in names]

    paths = {
        key: of(theirs, names)
        for key, names in ROUTES.items()
        if not all(n.startswith("=") for n in names)
    }
    reads = [key for key in paths if key.startswith("GET ")]
    named = [(*entry.rsplit(" ", 1), name) for entry, name in NAMED.items()]
    writes = [entry for entry in named if not _reads(entry[0])]
    last = [entry for entry in writes if " ".join(entry[:2]) in UPSERTS]

    # A reads and lists its own resources, and its own identifier changes what each write
    # answers, so a refusal to B is not an empty seed or a body refused before any lookup.
    world.act_as(WORKSPACE_A)
    for key in set(reads) - NOT_SEEDED.keys():
        answer = _send(world, key, paths[key])
        if answer[0] >= 300 or answer == _send(world, key, [_missing(v) for v in paths[key]]):
            wrong.append(
                f"{key}: A's read shows nothing of its own -> {answer[0]} {answer[1][:200]}"
            )
    for key, param, name in named:
        own = of(theirs, ROUTES.get(key, ()))
        seeded = f"{key} {param}" not in NOT_SEEDED
        if (
            seeded
            and _reads(key)
            and _send(world, key, own, (param, theirs[name]))
            == _send(world, key, own, (param, _missing(theirs[name])))
        ):
            wrong.append(f"{key} {param}: A's own changes nothing")
    for key, names in LISTS.items():
        text = world.client.get(key.split(" ", 1)[1]).text
        wrong += [f"{key}: A's list lacks its {n}" for n in names if str(theirs[n]) not in text]
    for key, param, name in writes:
        if f"{key} {param}" in UPSERTS:
            continue
        values = of(theirs, ROUTES.get(key, ()))
        answer = _send(world, key, values, (param, theirs[name]))
        if answer == _send(world, key, values, (param, _missing(theirs[name]))):
            wrong.append(f"{key} {param}: A's own changes nothing -> {answer[0]} {answer[1][:200]}")
    # What A reads the same twice, B's calls must leave the same.
    first = {key: _send(world, key, paths[key]) for key in reads}
    stable = {k: answer for k, answer in first.items() if _send(world, k, paths[k]) == answer}

    world.act_as(WORKSPACE_B)
    leaked = _uuids(theirs)
    for key, names in LISTS.items():
        resp = world.client.get(key.split(" ", 1)[1])
        seen = sorted(i for i in leaked | {str(theirs[n]) for n in names} if i in resp.text)
        if resp.status_code != 200 or seen:
            wrong.append(f"{key}: B's list -> {resp.status_code}, shows A's {seen}")
    # Reads before writes, so a write B wrongly made cannot hide or fake a read's answer;
    # path writes before body writes, which make B's own resources.
    for key in reads:
        _expect_hidden(world, wrong, key, paths[key])
    for key, param, name in named:
        if _reads(key):
            own = of(mine, ROUTES.get(key, ()))
            _expect_named_hidden(world, wrong, key, param, theirs[name], own)
    wrong += mcp_sweep(world)
    world.act_as(WORKSPACE_B)
    for key in paths.keys() - set(reads) - UPSERTS.keys():
        _expect_hidden(world, wrong, key, paths[key])
    for key, param, name in [entry for entry in writes if entry not in last] + last:
        own = of(mine, ROUTES.get(key, ()))
        _expect_named_hidden(world, wrong, key, param, theirs[name], own)
    for key in paths.keys() & UPSERTS.keys():
        _expect_hidden(world, wrong, key, paths[key])

    # B's refused calls left A's resources as they were.
    world.act_as(WORKSPACE_A)
    for key, answer in stable.items():
        if _send(world, key, paths[key]) != answer:
            wrong.append(f"{key} (A changed): A's resource changed after B's calls")
    return wrong


def _expect_hidden(world: Tenants, wrong: list[str], key: str, values: list[Any]) -> None:
    """B's answer for A's identifiers is the same as for identifiers of nothing.

    A write must be refused (403 or 404); a read may answer 200 only with what it answers
    for an identifier of nothing, so it shows none of A's resource.
    """
    answer = _send(world, key, values)
    none = _send(world, key, [_missing(v) for v in values])
    refused = answer[0] in (403, 404) or (_reads(key) and answer[0] == 200)
    if answer != none or not (refused or key in UPSERTS):
        wrong.append(f"{key}: A's -> {answer[0]} {answer[1][:200]}; none -> {none[0]}")


def _expect_named_hidden(
    world: Tenants, wrong: list[str], key: str, param: str, value: Any, own: list[Any]
) -> None:
    """B naming A's resource in ``param`` answers as naming nothing; a write is refused."""
    answer = _send(world, key, own, (param, value))
    none = _send(world, key, own, (param, _missing(value)))
    write = not _reads(key) and f"{key} {param}" not in UPSERTS
    if answer != none or (write and answer[0] < 400):
        wrong.append(f"{key} {param}: A's -> {answer[0]} {answer[1][:200]}; none -> {none[0]}")


#: Every argument of SCF's own MCP server tools that names a resource, and its type.
MCP_TOOLS: dict[str, str] = {
    "world_entity entity_id": "WorldEntity",
    "world_facts subject_id": "WorldEntity",
    "world_facts object_id": "WorldEntity",
    "world_changes entity_id": "WorldEntity",
    "world_decision_drift decision_id": "WorldDecision",
    "spec_list project_id": "Project",
    "spec_get spec_id": "Spec",
    "board_items project_id": "Project",
    "approvals_pending project_id": "Project",
    "run_status run_id": "AgentRun",
    "assemble_context agent_run_id": "AgentRunLive",
    "assemble_context spec_ids": "Spec",
    "knowledge_document source": "KnowledgeSource",
    "world_propose subject_id": "WorldEntity",
    "world_propose object_id": "WorldEntity2",
}

#: Other arguments a tool needs, so a call reaches its lookup.
MCP_ARGS: dict[str, dict[str, Any]] = {
    "assemble_context": {"task": "x"},
    "knowledge_document": {"path": "a.md"},
    "world_propose": {"phrase": "x", "subject_id": "{WorldEntity}"},
}


def _tool(world: Tenants, tool: str, param: str, value: Any) -> tuple[bool, str]:
    """Call one MCP tool; its error flag and its result, with ``value`` and the clock masked."""
    args = {**_fill(MCP_ARGS.get(tool, {}), world.ids[world.workspace])}
    args[param] = [str(value)] if param.endswith("_ids") else str(value)
    call = {"name": tool, "arguments": args}
    resp = world.client.post(
        "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": call}
    )
    result = resp.json().get("result", resp.json())
    structured = {
        k: v for k, v in result.get("structuredContent", result).items() if k != "recorded_at"
    }
    return bool(result.get("isError")), json.dumps(structured).replace(str(value), "<id>")


def mcp_sweep(world: Tenants) -> list[str]:
    """B naming A's resources through the MCP server's tools gets the not-found result."""
    from scf_api.routers.mcp_server import get_mcp_tool_registry

    wrong: list[str] = []
    theirs = world.ids[WORKSPACE_A]
    for tool in get_mcp_tool_registry().visible(world.principal()):
        for field in tool.input_model.model_fields:
            if field.endswith(("_id", "_ids")) and f"{tool.name} {field}" not in MCP_TOOLS:
                wrong.append(f"MCP tool argument naming a resource, not swept: {tool.name} {field}")
    for entry, name in MCP_TOOLS.items():
        tool, param = entry.split(" ")
        world.act_as(WORKSPACE_A)
        if _tool(world, tool, param, theirs[name]) == _tool(
            world, tool, param, _missing(theirs[name])
        ):
            wrong.append(f"MCP {entry}: A's own changes nothing")
        world.act_as(WORKSPACE_B)
        answer = _tool(world, tool, param, theirs[name])
        if answer != _tool(world, tool, param, _missing(theirs[name])) or (
            tool == "world_propose" and not answer[0]
        ):
            wrong.append(f"MCP {entry}: A's -> {answer[1][:200]}")
    return wrong


def run() -> list[str]:
    with tenants() as world:
        seed(world, WORKSPACE_A)
        seed(world, WORKSPACE_B, B_SEEDS)
        return sweep(world)
