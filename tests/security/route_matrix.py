"""The declared authorisation of every route the API serves (PT-01, FR-022, FR-024; #188).

:func:`routes` walks the running application's route table. Every route and method is
declared exactly once: in :data:`DECLARED` with the permission its ``require_permission``
guard needs (the least a role must hold to call it), or in :data:`UNGUARDED` with the
reviewed reason it has no such guard. A route with no entry, a stale entry, or a guard
that needs another permission than declared fails ``check_route_role_matrix`` in
``matrix_checks.py``; ``apps/api/tests/test_narrowed_token_route_walk.py`` walks the same
table with narrowed and agent tokens.
"""

from __future__ import annotations

import inspect
import re
import uuid
from collections.abc import Iterator
from typing import Any

from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute, APIWebSocketRoute, iter_route_contexts

from scf_api.auth.rbac import Permission

_F30 = (
    "F30: authorised in AuthzService against get_principal_context, which gives a narrowed "
    "key one workspace viewer grant and an agent's token its owner's scopes capped at "
    "agent-runner; the token walk calls the writes"
)
_SIGN_IN = "Sign-in: reads no API key, only what an identity provider or a one-time link returns"
_WEBHOOK = "Webhook: verified by the sender's signature or shared secret, never an API key"
_SCIM = "SCIM: require_scim_token accepts only a SCIM bearer token; an API key gets 401"
_READ = "Reads the caller's own workspace and changes nothing; every role holds read"
_PUBLIC = "Public by design: liveness, service info and API docs read no credential"

#: Every route without a ``require_permission`` guard, and how it authorises instead or why
#: it is public by design. The token walk calls the writes among them that take an API key
#: with narrowed and agent tokens. Each handler was read before it was listed.
UNGUARDED: dict[str, str] = {
    **dict.fromkeys(
        [
            "GET /public/benchmarks",
            "GET /public/leaderboard/{slug}/{version}",
            "GET /public/leaderboard/{slug}/{version}/submissions/{submission_id}",
            "GET /public/leaderboard/{slug}/{version}/submissions/{submission_id}/bundles",
        ],
        "Public by design, mounted only when FORGE_PUBLIC_LEADERBOARD_ENABLED is on: reads "
        "published benchmark results through payload-free models, rate limited per IP",
    ),
    **dict.fromkeys(
        [
            "GET /health",
            "GET /healthz",
            "GET /readyz",
            "GET /health/ready",
            "GET /",
            "GET /openapi.json",
            "GET /docs",
            "GET /docs/oauth2-redirect",
            "GET /redoc",
        ],
        _PUBLIC,
    ),
    **dict.fromkeys(
        [
            "POST /auth/login",
            "POST /auth/callback",
            "GET /auth/sign-in-link",
            "GET /auth/saml/discover",
            "POST /auth/saml/discover",
            "GET /auth/saml/{slug}/metadata",
            "GET /auth/saml/{slug}/login",
            "POST /auth/saml/{slug}/acs",
            "GET /auth/saml/{slug}/slo",
            "POST /auth/saml/{slug}/slo",
            "GET /auth/oidc/{slug}/login",
            "GET /auth/oidc/{slug}/callback",
        ],
        _SIGN_IN,
    ),
    "POST /auth/logout": "Revokes only the key in the session cookie; reads no header token",
    "POST /auth/personal-tokens": "Refuses every narrowed, unbound or agent key, so no token "
    "mints a token (the token walk calls it)",
    "POST /auth/device/code": "Device sign-in (#207): reads no credential and returns only "
    "a pending request's codes; nothing is issued until a person approves it",
    "POST /auth/device/token": "Device sign-in: reads no credential; returns a token only for "
    "a request a person approved, once, to the holder of its device code",
    "POST /auth/device/approve": "Refuses every narrowed or unbound key, as minting a personal "
    "token does, so no token approves a device (the token walk calls it)",
    "POST /auth/device/preview": "Refuses every narrowed or unbound key, as approving does, and "
    "changes nothing but the wrong-code count (the token walk calls it)",
    **dict.fromkeys(
        [
            "POST /integration/github/webhooks",
            "POST /integration/slack/commands",
            "POST /integration/slack/interactions",
            "POST /integrations/alerts/{provider}/webhook",
            "POST /integrations/pm/webhooks/jira/{connection_id}",
            "POST /integrations/pm/webhooks/linear/{connection_id}",
            "POST /integrations/pm/webhooks/github_issues/{connection_id}",
        ],
        _WEBHOOK,
    ),
    **dict.fromkeys(
        [
            "GET /scim/v2/ServiceProviderConfig",
            "GET /scim/v2/ResourceTypes",
            "GET /scim/v2/Schemas",
            "GET /scim/v2/Users",
            "POST /scim/v2/Users",
            "GET /scim/v2/Users/{scim_id}",
            "PUT /scim/v2/Users/{scim_id}",
            "PATCH /scim/v2/Users/{scim_id}",
            "DELETE /scim/v2/Users/{scim_id}",
            "GET /scim/v2/Groups",
            "POST /scim/v2/Groups",
            "GET /scim/v2/Groups/{scim_id}",
            "PUT /scim/v2/Groups/{scim_id}",
            "PATCH /scim/v2/Groups/{scim_id}",
            "DELETE /scim/v2/Groups/{scim_id}",
        ],
        _SCIM,
    ),
    **dict.fromkeys(
        [
            "GET /auth/me",
            "GET /board/tasks",
            "GET /board/tasks/{task_id}",
            "GET /board/epics",
            "GET /board/epics/{epic_id}",
            "GET /board/sprints",
            "GET /board/sprints/{sprint_id}",
            "GET /board/milestones",
            "GET /board/milestones/{milestone_id}",
            "GET /board/incidents",
            "GET /board/incidents/{incident_id}",
            "POST /knowledge/search",
            "GET /observability/audit",
            "GET /observability/metrics",
            "GET /observability/runs/{run_id}/trace",
        ],
        _READ,
    ),
    "WS /ws": "Pushes the workspace's events to the caller; what the client sends is ignored",
    "WS /ws/spec/{spec_id}": "Closes a socket that sends an edit without write "
    "(can(principal.role, WRITE)); neither token holds write",
    "POST /world/proposals": "Needs write or run_agent (_require_write_or_run_agent): the "
    "read token is refused (the token walk calls it); create_proposal records the run token's "
    "proposal as an agent's (SourceKind.AGENT)",
    "POST /world/decisions": "Needs write or run_agent, and refuses every narrowed key and "
    "agent's token (_require_decision_recorder), whose decision would be stored as its "
    "owner's own (the token walk calls it)",
    **dict.fromkeys(
        ["GET /mcp", "POST /mcp"],
        "Hides and refuses write tools without write or run_agent (registry.can_write); "
        "test_a_narrowed_token_on_mcp_is_narrowed_as_on_ws",
    ),
    **dict.fromkeys(
        [
            "GET /teams",
            "POST /teams",
            "GET /teams/{team_id}",
            "PATCH /teams/{team_id}",
            "POST /teams/{team_id}/archive",
            "GET /teams/{team_id}/members",
            "POST /teams/{team_id}/members",
            "PATCH /teams/{team_id}/members/{user_id}",
            "DELETE /teams/{team_id}/members/{user_id}",
            "GET /access/grants",
            "POST /access/grants",
            "DELETE /access/grants/{grant_id}",
            "GET /access/effective",
            "GET /projects",
            "GET /projects/{project_id}/access",
            "PUT /projects/{project_id}/visibility",
            "GET /projects/{project_id}/team-access",
            "POST /projects/{project_id}/team-access",
            "DELETE /projects/{project_id}/team-access/{team_id}",
        ],
        _F30,
    ),
}


#: Every route guarded by a role-matrix ``require_permission`` and the permission the guard
#: needs. Each was read against what the handler does before it was listed.
DECLARED: dict[str, Permission] = {
    **dict.fromkeys(
        [
            "GET /auth/api-keys",
            "POST /auth/api-keys",
            "DELETE /auth/api-keys/{key_id}",
            "PUT /integration/github/app-credentials",
            "DELETE /integration/github/app-credentials",
        ],
        Permission.MANAGE_KEYS,
    ),
    **dict.fromkeys(
        [
            "GET /auth/secrets",
            "POST /auth/secrets",
            "POST /auth/secrets/{secret_id}/check",
            "POST /auth/secrets/{secret_id}/rotate",
            "DELETE /auth/secrets/{secret_id}",
            "POST /integrations/pm/connections",
            "PATCH /integrations/pm/connections/{connection_id}",
            "DELETE /integrations/pm/connections/{connection_id}",
            "POST /integrations/pm/connections/{connection_id}/test",
        ],
        Permission.MANAGE_SECRETS,
    ),
    **dict.fromkeys(
        [
            "POST /board/tasks",
            "POST /board/tasks/bulk",
            "PATCH /board/tasks/{task_id}",
            "DELETE /board/tasks/{task_id}",
            "POST /board/tasks/{task_id}/status",
            "POST /board/tasks/{task_id}/dependencies",
            "POST /board/epics",
            "PATCH /board/epics/{epic_id}",
            "DELETE /board/epics/{epic_id}",
            "POST /board/sprints",
            "PATCH /board/sprints/{sprint_id}",
            "DELETE /board/sprints/{sprint_id}",
            "POST /board/milestones",
            "PATCH /board/milestones/{milestone_id}",
            "DELETE /board/milestones/{milestone_id}",
            "POST /board/incidents",
            "PATCH /board/incidents/{incident_id}",
            "DELETE /board/incidents/{incident_id}",
            "POST /spec/constitution",
            "POST /spec/specs",
            "PUT /spec/specs/{spec_id}",
            "PUT /spec/specs/{spec_id}/markdown",
            "PUT /spec/specs/{spec_id}/manifest",
            "POST /spec/specs/{spec_id}/clarify",
            "POST /spec/specs/{spec_id}/plan",
            "POST /spec/specs/{spec_id}/approve",
            "POST /spec/specs/{spec_id}/reject",
            "POST /spec/specs/{spec_id}/request-changes",
            "POST /spec/specs/{spec_id}/tasks",
            "POST /spec/tasks/{task_id}/validate",
            "POST /spec/draft",
            "POST /spec/import",
            "POST /knowledge/index",
            "POST /knowledge/sync",
            "POST /world/facts",
            "POST /world/facts/{fact_id}/supersede",
            "POST /world/facts/{fact_id}/retract",
            "POST /world/ingest",
            "POST /world/proposals/{proposal_id}/decision",
            "POST /world/merges",
            "POST /world/merges/{merge_id}/revert",
            "POST /world/derive",
            "POST /world/ontology/classes",
            "POST /world/ontology/relations",
            "POST /workflow/runs",
            "POST /workflow/runs/{run_id}/transition",
            "POST /workflow/runs/{run_id}/gates/{gate}/sync",
            "POST /workflow/runs/{run_id}/red-team",
            "PUT /workflow/editor/definitions/{name}/draft",
            "POST /workflow/editor/definitions/{name}/draft/validate",
            "POST /policy/test",
            "POST /mcp/connections",
            "POST /marketplace/publish",
            "POST /benchmarks/{slug}/{version}/submissions",
            "POST /integration/github/repos/{connection_id}/sync",
            "POST /integration/github/pull-requests",
            "POST /integration/slack/notify",
            "POST /integration/slack/approvals/{approval_id}/notify",
            "POST /approvals",
            "POST /tasks/{task_id}/held-out-scenarios",
            "GET /tasks/{task_id}/held-out-scenarios",
            "GET /tasks/{task_id}/held-out-scenarios/results",
            "POST /tasks/{task_id}/held-out-scenarios/{scenario_id}/approve",
            "POST /tasks/{task_id}/held-out-scenarios/{scenario_id}/reject",
            "DELETE /tasks/{task_id}/held-out-scenarios/{scenario_id}",
            "POST /incidents",
            "POST /incidents/{incident_id}/events",
            "POST /incidents/{incident_id}/remediation",
            "POST /incidents/{incident_id}/postmortem/publish",
            "POST /integrations/alerts/manual",
            "POST /projects/{project_id}/automations",
            "PATCH /automations/{rule_id}",
            "POST /automations/{rule_id}/enable",
            "POST /automations/{rule_id}/disable",
            "POST /automations/{rule_id}/test",
            "POST /projects/{project_id}/deployments",
            "POST /deployments/{deployment_id}/decision",
            "POST /deployments/{deployment_id}/cancel",
            "POST /deployments/{deployment_id}/rollback",
            "POST /projects/{project_id}/sprints",
            "PATCH /sprints/{sprint_id}",
            "POST /sprints/{sprint_id}/start",
            "POST /sprints/{sprint_id}/complete",
            "POST /sprints/{sprint_id}/cancel",
            "POST /sprints/{sprint_id}/recompute",
            "POST /estimation-scales",
            "PATCH /estimation-scales/{scale_id}",
            "PUT /sprints/{sprint_id}/capacity",
        ],
        Permission.WRITE,
    ),
    **dict.fromkeys(
        [
            "GET /spec/specs/{spec_id}",
            "GET /spec/specs/{spec_id}/markdown",
            "GET /spec/specs/{spec_id}/manifest",
            "GET /spec/constitution/{project_id}",
            "GET /spec/specs/{spec_id}/versions",
            "GET /spec/specs/{spec_id}/versions/{version_number}",
            "GET /spec/specs/{spec_id}/versions/{from_version}/diff/{to_version}",
            "GET /projects/{project_id}/specs",
            "GET /knowledge/sources",
            "GET /knowledge/document",
            "GET /world/entities",
            "GET /world/entities/{entity_id}",
            "GET /world/entities/{entity_id}/changes",
            "GET /world/facts",
            "GET /world/proposals",
            "GET /world/conflicts",
            "GET /world/ontology/classes",
            "GET /world/ontology/relations",
            "GET /world/decisions/{decision_id}",
            "GET /world/decisions/{decision_id}/drift",
            "POST /context/assemble",
            "GET /workflow/runs/{run_id}",
            "GET /workflow/runs/{run_id}/red-team",
            "GET /workflow/runs/{run_id}/stations",
            "GET /workflow/editor/catalog",
            "GET /workflow/editor/definitions",
            "GET /workflow/editor/definitions/{name}",
            "GET /workflow/editor/definitions/{name}/revisions",
            "GET /workflow/editor/definitions/{name}/revisions/{revision}",
            "GET /workflow/editor/definitions/{name}/diff",
            "GET /workflow/editor/definitions/{name}/export",
            "GET /agent/runs/{run_id}",
            "POST /agent/runs/{run_id}/replay",
            "GET /agent/runs/{run_id}/events",
            "GET /policy",
            "POST /policy/evaluate",
            "GET /policy/rule-evaluations",
            "GET /ao/role-config",
            "GET /ao/settings",
            "POST /ao/routing-preview",
            "GET /ao/self-eval/status",
            "GET /mcp/connections",
            "GET /mcp/connections/{connection_id}/resources",
            "GET /mcp/connections/{connection_id}/resources/read",
            "GET /mcp/connections/{connection_id}/prompts",
            "POST /mcp/connections/{connection_id}/prompts/get",
            "GET /mcp/connections/{connection_id}/audit",
            "GET /mcp/connections/{connection_id}/index",
            "GET /marketplace/registries",
            "GET /marketplace/listings",
            "GET /marketplace/listings/{registry_slug}/{slug}",
            "GET /marketplace/installations",
            "GET /benchmarks",
            "GET /benchmarks/submissions/{submission_id}",
            "GET /benchmarks/{slug}/{version}",
            "GET /benchmarks/{slug}/{version}/submissions",
            "GET /integration/github/health",
            "GET /approvals",
            "GET /approvals/count",
            "GET /approvals/{approval_id}",
            "GET /approvals/{approval_id}/context",
            "GET /approvals/{approval_id}/decisions",
            "POST /approvals/{approval_id}/decision",
            "GET /attestations",
            "GET /attestations/{attestation_id}",
            "GET /attestations/{attestation_id}/envelope",
            "GET /approvals/{approval_id}/attestation",
            "GET /tasks/{task_id}/cost",
            "GET /cost/summary",
            "GET /cost/timeseries",
            "GET /cost/prices",
            "GET /incidents",
            "GET /incidents/reliability",
            "GET /incidents/{incident_id}",
            "GET /incidents/{incident_id}/timeline",
            "GET /incidents/{incident_id}/remediation",
            "GET /incidents/{incident_id}/postmortem",
            "GET /observability/analytics/incidents/reliability",
            "GET /observability/analytics/dora",
            "GET /observability/analytics/factory",
            "GET /observability/analytics/budgets/{budget_id}/status",
            "GET /observability/analytics/budgets",
            "GET /observability/analytics/coverage/trend",
            "GET /observability/analytics/coverage/rollup",
            "GET /integrations/pm/connections",
            "GET /integrations/pm/connections/{connection_id}",
            "GET /integrations/pm/connections/{connection_id}/links",
            "GET /automations/catalog",
            "GET /projects/{project_id}/automations",
            "GET /automations/{rule_id}",
            "GET /automations/{rule_id}/executions",
            "GET /projects/{project_id}/pipeline",
            "GET /projects/{project_id}/deployments",
            "GET /deployments/{deployment_id}",
            "GET /deployments/{deployment_id}/gate",
            "GET /projects/{project_id}/sprints",
            "GET /projects/{project_id}/velocity",
            "GET /projects/{project_id}/velocity/export",
            "GET /sprints/{sprint_id}",
            "GET /sprints/{sprint_id}/burndown",
            "GET /sprints/{sprint_id}/report",
            "GET /tasks/{task_id}/estimate-history",
            "GET /estimation-scales",
            "GET /sprints/{sprint_id}/capacity",
            "GET /sprints/{sprint_id}/goal-alignment",
            "GET /projects/{project_id}/cfd",
            "GET /projects/{project_id}/cycle-lead-time",
            "GET /portfolio/velocity",
        ],
        Permission.READ,
    ),
    **dict.fromkeys(
        [
            "POST /workflow/editor/definitions",
            "POST /workflow/editor/definitions/{name}/fork",
            "POST /workflow/editor/definitions/{name}/publish",
            "POST /workflow/editor/definitions/{name}/rollback",
            "POST /workflow/editor/definitions/{name}/archive",
            "POST /workflow/editor/import",
            "PUT /ao/role-config/{role}",
            "DELETE /ao/role-config/{role}",
            "PUT /ao/settings",
            "POST /ao/self-eval/runs",
            "PATCH /mcp/connections/{connection_id}",
            "POST /mcp/connections/{connection_id}/index/reindex",
            "POST /marketplace/registries",
            "PATCH /marketplace/registries/{registry_id}",
            "DELETE /marketplace/registries/{registry_id}",
            "POST /marketplace/registries/{registry_id}/sync",
            "POST /marketplace/preview",
            "POST /marketplace/install",
            "POST /marketplace/installations/{installation_id}/update",
            "PATCH /marketplace/installations/{installation_id}",
            "DELETE /marketplace/installations/{installation_id}",
            "GET /marketplace/audit",
            "POST /benchmarks/submissions/{submission_id}/verify",
            "POST /benchmarks/submissions/{submission_id}/publish",
            "POST /benchmarks/submissions/{submission_id}/flag",
            "GET /audit",
            "GET /audit/actions",
            "GET /audit/export",
            "POST /audit/verify",
            "GET /audit/{entry_id}",
            "POST /cost/prices",
            "POST /cost/reprice",
            "DELETE /automations/{rule_id}",
            "PUT /projects/{project_id}/pipeline",
            "POST /deployments/{deployment_id}/freeze-override",
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
            "DELETE /workspaces/{workspace_id}/scim/tokens/{token_id}",
        ],
        Permission.ADMIN,
    ),
    **dict.fromkeys(
        [
            "POST /agent/runs",
            "POST /agent/runs/{run_id}/fork",
            "POST /policy/simulate",
            "POST /policy/static-gate",
            "POST /mcp/connections/{connection_id}/tools/call",
            "POST /observability/runs/{run_id}/trace",
        ],
        Permission.RUN_AGENT,
    ),
}


def guard(dependant: Dependant) -> set[Permission] | None:
    """The permissions the route's ``require_permission`` guards need; ``None`` if it has none."""
    needs: set[Permission] = set()
    found = False
    stack = list(dependant.dependencies)
    while stack:
        dep = stack.pop()
        stack.extend(dep.dependencies)
        call = dep.call
        if getattr(call, "__qualname__", "") == "require_permission.<locals>._dependency":
            permission = inspect.getclosurevars(call).nonlocals["permission"]
            if isinstance(permission, Permission):  # not F30's scoped permission
                needs.add(permission)
                found = True
    return needs if found else None


def routes(app: Any) -> Iterator[tuple[str, str, set[Permission] | None]]:
    for route in iter_route_contexts(app.routes):
        original = route.original_route
        if isinstance(original, APIWebSocketRoute):
            yield "WS", route.path, None
            continue
        needs = guard(route.dependant) if isinstance(original, APIRoute) else None
        for method in sorted((route.methods or set()) - {"HEAD"}):
            yield method, route.path, needs


def url(path: str) -> str:
    return re.sub(r"{[^}]+}", lambda _: str(uuid.uuid4()), path)
