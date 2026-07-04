# F30 — Multi-Team Workspace Controls & Full RBAC

> Phase: v3 · Spec module(s): Security §"RBAC" (admin/member/viewer/agent-runner **per workspace and per project**), Core Data Model (`Workspace → User[] roles`, `teams`), Native Project Board (team-scoped projects/filters), Auth & Secrets Service · Status target: **Done** = the flat per-workspace role model from the v1 auth foundation is replaced by a **scoped, hierarchical** model in which a principal (user, API key, or agent/service identity) holds role grants at **workspace / team / project** scope; a deterministic, pure `PermissionResolver` computes a principal's effective permission set on any resource via a documented precedence rule; teams are first-class workspace control units with membership, team-leads, optional nesting, and per-project access levels; projects can be **`team_restricted`** so non-member workspace users (except workspace admins) cannot see or touch them; every board/spec/run/approval/admin route is gated by `require_permission(...)`; board list/query endpoints transparently filter to the caller's **visible projects**; all grant/team/access changes are written to the platform immutable audit log; lockout and self-escalation are structurally impossible; and the whole surface is exercised by a table-driven resolver suite (incl. Hypothesis totality), API integration tests, and a board-visibility integration test — lint + types + `pytest` green on `packages/authz-sdk`, the `apps/api` authz routers/deps, and the worker expiry task; Vitest/Playwright green on the web settings surface.

---

## 1. Intent — what & why

The v1 auth foundation ships a **flat** model: one role (`admin | member | viewer | agent-runner`) per user per workspace, enforced by a coarse `require_role(role)` FastAPI dependency. That is correct for a single team but cannot express the spec's actual RBAC requirement — Security §RBAC states roles are *"per workspace **and per project**"* — and it has no concept of teams owning or restricting work. As soon as two teams share one Forge workspace (the explicit Phase-3 line item *"Multi-team workspace controls and full RBAC hierarchy"*), the flat model leaks every project to everyone and forces all-or-nothing admin.

F30 makes RBAC **scoped and hierarchical** without inventing an ABAC/conditional-rule engine (that is the separate Phase-3 *"Advanced policy engine"* slice). Concretely it:

1. **Introduces `role_grants`** as the single source of truth for authorization: a grant binds `(principal, scope, role)` where scope ∈ {workspace, team, project}. The flat `app_user.role` column (the `User.role` set by the auth foundation `cross-cutting/F37-auth-secrets-byok`) is backfilled into workspace-scope grants and deprecated for authz.
2. **Promotes teams to control units**: `team_members` (with `lead`/`member` team roles, optional nested teams) plus `project_team_access` (a team's access level on a project: `read|write|admin`). Membership in a team confers role on every project that team can access — the multi-team control surface.
3. **Adds project visibility** (`workspace | team_restricted`) so a project can be walled off to its owning/accessing teams (workspace admins always retain access; nothing else bypasses it).
4. **Ships a deterministic `PermissionResolver`** (pure, total, no I/O — same engineering discipline as F04's `PolicyEvaluator`) that unions the permission sets of all applicable, unexpired grants under an explicit precedence rule, and applies the visibility gate.
5. **Replaces `require_role` with `require_permission(permission, resource)`** across the API, and gives the board a `visible_project_ids` filter so list/query endpoints only return what the caller may see.
6. **Audits every authorization change** to the platform immutable audit log and makes **lockout** (removing the last workspace admin) and **self-escalation** (granting yourself or others a role above your own at a scope) structurally impossible.

Why now (v3): supervised multi-agent (`v3/F27-supervised-multi-agent`) needs project-scoped `agent_runner` identities that cannot self-expand; deployment gates (`v3/F31-deployment-gates`) need `deploy.approve` scoped per project/team; and the board, spec engine, and approval layer already assume a `Principal` + role check (from `cross-cutting/F37-auth-secrets-byok`) that F30 upgrades in place. F30 is the security spine for every multi-team deployment.

---

## 2. User-facing behavior / journeys

**Workspace admin — stand up two teams.**
1. Opens **Settings → Teams**, creates `Team Backend` (key `BE`) and `Team Platform` (key `PLT`); optionally nests `BE` under a parent `Engineering` team.
2. Adds users to each team; marks Dana as **lead** of `BE` (Dana can now manage `BE` membership but not other teams).
3. Opens **Settings → Members**, sees every workspace member with their **effective** workspace role and a per-row "manage roles" action; grants Sam workspace `member`, Riya workspace `viewer`.

**Workspace admin / project admin — wall off a project.**
1. Opens **Project → Settings → Access**, switches visibility from `Workspace` to `Team-restricted`.
2. Grants `Team Backend` **write** and `Team Platform` **read** on the project. A banner explains: "Workspace admins always retain access; all other access is via the teams listed here."
3. Riya (workspace viewer, not in either team) now gets a **404** on that project's board URL and it no longer appears in her project list — without any error leak that it exists.

**Team lead — manage their team.**
1. Dana (lead of `BE`, not a workspace admin) adds/removes `BE` members and sets a member's team role; she **cannot** see the Members page's workspace-role controls, create teams, or grant workspace-level roles (those actions 403).

**Member — scoped elevation.**
1. Sam is workspace `member` but is granted project `admin` on `CORE`. On `CORE` Sam can manage statuses, project access, and grant project-scoped roles to others (but never above `admin`, and never workspace-level). On other projects Sam is a normal member.

**Agent / service identity — bounded by scope.**
1. A run for a task in project `CORE` executes as an `agent_runner` principal whose grant is **project-scoped to `CORE`** with an `expires_at`. The agent can read/write tasks and open PRs in `CORE` only; it cannot approve its own PR (`pr.approve` denied), cannot grant roles, and cannot touch other projects. After `expires_at` the grant stops resolving and is purged.

**Any user — see why.**
1. On any 403, the response carries the missing permission + scope. **Settings → Members → (user) → Access** shows an **effective-access inspector**: "On project CORE, Sam has {project.read, project.write, task.write, pr.approve} via [workspace:member, team BE→write]."

---

## 3. Vertical slice

### 3.1 Data model (tables/columns/migrations touched)

One reversible Alembic migration `packages/db/migrations/versions/00NN_f30_multi_team_rbac.py` (additive tables/columns + one data backfill; `00NN` follows the latest revision in the tree — in-tree the chain is `0001_baseline` → `cross-cutting/F37-auth-secrets-byok`'s `0002_auth_secrets`, so F30 is a later revision). ORM models live in `packages/db/forge_db/models/` (importable as `forge_db.models.*`), reusing the `WorkspaceScopedModel` base + `enum_type`/`json_type` helpers from `forge_db.base`. Every table carries `workspace_id` (tenant scope) + `created_at`/`updated_at` (provided by `WorkspaceScopedModel`).

> Cross-slice note: the authoritative in-tree Alembic tree is `packages/db/migrations/` (`env.py` + `versions/`, baseline `0001_baseline.py`) — NOT `packages/db/forge_db/migrations/` or `apps/api/alembic/` (some sibling slices reference those paths; reconcile to `packages/db/migrations/versions/` when they land). The column set below is identical regardless of tree.

**New — `role_grants`** (the authorization source of truth; `models/role_grant.py`):

| column | type | notes |
|---|---|---|
| `id` | UUID PK | |
| `workspace_id` | UUID FK→`workspaces` CASCADE | tenant scope |
| `principal_type` | enum(`PrincipalType`: user/api_key/service) | |
| `principal_id` | UUID | user id, api_key id, or service identity id |
| `scope_type` | enum(`ScopeType`: workspace/team/project) | |
| `scope_id` | UUID | equals `workspace_id` when `scope_type=workspace`; team id / project id otherwise |
| `role` | enum(`Role`: admin/member/viewer/agent_runner) | |
| `granted_by` | UUID FK→`users` SET NULL | actor for audit |
| `expires_at` | timestamptz NULL | automatic expiry (Security: "automatic expiry for agent tokens"); NULL = permanent |
| `created_at` | timestamptz | |

- `uq_role_grants_principal_scope_role` UNIQUE(`workspace_id`,`principal_type`,`principal_id`,`scope_type`,`scope_id`,`role`).
- Indexes: `ix_role_grants_principal (workspace_id, principal_type, principal_id)` (the hot lookup), `ix_role_grants_scope (scope_type, scope_id)`, partial `ix_role_grants_expiring (expires_at) WHERE expires_at IS NOT NULL`.
- CHECK: `scope_type='workspace' → scope_id = workspace_id`.
- Append-mostly: revoke = DELETE row **and** emit an immutable audit event (the audit log, not this table, is the permanent history).

**Extended — `teams`** (the base `teams` table is created by the foundation substrate / `v1/F01-project-board`, which uses a `team_id` on tasks and team-scoped saved filters; note the in-tree baseline `0001_baseline` does **not** yet include it, so if no `teams` table exists when F30 lands its migration **creates** the base table as well; `models/team.py` adds these columns):

| column | type | notes |
|---|---|---|
| `key` | citext | short slug, e.g. `BE`; `uq_teams_workspace_id_key` |
| `description` | text NULL | |
| `parent_team_id` | UUID FK→`teams` RESTRICT, NULL | optional nesting; cycle-checked + depth-capped (`MAX_TEAM_DEPTH=5`) |
| `archived_at` | timestamptz NULL | soft-archive |
| `created_by` | UUID FK→`users` SET NULL | |

**New — `team_members`** (create if v1 only had bare teams; `models/team_member.py`):

| column | type | notes |
|---|---|---|
| `id` | UUID PK | |
| `workspace_id` | UUID FK CASCADE | |
| `team_id` | UUID FK→`teams` CASCADE | |
| `user_id` | UUID FK→`users` CASCADE | |
| `team_role` | enum(`TeamRole`: lead/member) default `member` | `lead` ⇒ `team.member.manage` on this team |
| `created_at` | timestamptz | `uq_team_members_team_user` UNIQUE(`team_id`,`user_id`) |

**New — `project_team_access`** (`models/project_team_access.py`):

| column | type | notes |
|---|---|---|
| `id` | UUID PK | |
| `workspace_id` | UUID FK CASCADE | |
| `project_id` | UUID FK→`projects` CASCADE | |
| `team_id` | UUID FK→`teams` CASCADE | |
| `access_level` | enum(`AccessLevel`: read/write/admin) | maps to role read→viewer, write→member, admin→admin |
| `created_at` | timestamptz | `uq_project_team_access_project_team` UNIQUE(`project_id`,`team_id`); index `ix_project_team_access_team (team_id)` |

**Extended — `projects`** (the `project` table — `packages/db/forge_db/models/project.py`, created by the baseline and used by `v1/F01-project-board`; add columns):

| column | type | notes |
|---|---|---|
| `visibility` | enum(`ProjectVisibility`: workspace/team_restricted) default `workspace` | |
| `owner_team_id` | UUID FK→`teams` SET NULL | display + default access grant on creation |

**Data backfill (in-migration, reversible):** the flat role lives on `app_user.role` (`User.role: UserRole`, set by `cross-cutting/F37-auth-secrets-byok`) — there is **no** `workspace_members` join table in the foundation (a `User` belongs to one workspace via `app_user.workspace_id`). So for every existing `app_user(id, workspace_id, role)` row, insert a `role_grants(principal_type=user, principal_id=user_id, scope_type=workspace, scope_id=workspace_id, role)`. `app_user.role` is **retained but deprecated for authz** (kept for a release for rollback; the resolver reads only `role_grants`). Downgrade drops the new tables/columns and the backfilled grants.

> `app_user` (the `User` table) remains the "who is in the workspace" identity/membership table; `app_user.role` is retained-but-deprecated; `role_grants` becomes the "what can they do" table.

### 3.2 Backend (FastAPI routes + services/packages)

**New pure package `packages/authz-sdk/forge_authz/` (no FastAPI/SQLAlchemy imports — mirrors `policy-sdk`):**

- `permissions.py` — the `Permission` enum + the frozen `ROLE_PERMISSIONS: Mapping[Role, frozenset[Permission]]` table + `ACCESS_LEVEL_ROLE: Mapping[AccessLevel, Role]` (§4).
- `resolver.py` — `DefaultPermissionResolver(PermissionResolver)`; pure `resolve()`/`can()` (§4 algorithm).
- `errors.py` — `AccessDenied(permission, scope)`, `EscalationError`, `LastAdminError`, `TeamCycleError`, `TeamDepthError`.
- `schema.py` — re-exports the `forge_contracts.authz` DTOs so the resolver and contract share one object set.

**Service `apps/api/forge_api/services/authz_service.py`** (DB + audit orchestration; the only sanctioned writer of grants/teams/access). Every mutation below emits its audit event through the **canonical `AuditSink`** owned by `cross-cutting/F39-audit-log` (contract `forge_contracts.audit.AuditEvent` / `AuditSink`, persisted by `SqlAuditWriter` into the shared append-only `audit_log` table established by `cross-cutting/F37-auth-secrets-byok`) — F30 does **not** define its own audit table, it extends the shared action vocabulary (§4):

- `grant_role(actor, principal_ref, scope, role, expires_at=None) -> RoleGrant` — validates **escalation** (actor must hold `role.grant` at that scope **and** a role ≥ the granted role at that scope) and **lockout** (cannot remove/replace the last workspace `admin`); upserts; emits `role_grant.created` audit.
- `revoke_role(actor, grant_id) -> None` — escalation + lockout checked; DELETE; emits `role_grant.revoked`.
- `create_team / update_team / archive_team` — cycle + depth checks on `parent_team_id`; emits `team.*` audit.
- `add_team_member / remove_team_member / set_team_role` — actor needs `team.member.manage` on the team (workspace admin or team lead); emits `team_member.*`.
- `set_project_visibility / upsert_project_team_access / remove_project_team_access` — actor needs `project.admin` on the project; emits `project_access.*`.
- `effective_access(principal_ref, resource_ref) -> EffectiveAccess` — introspection (powers the inspector + `GET /access/effective`).
- `load_principal_context(principal_ref) -> PrincipalContext` — loads all grants + team memberships for a principal in one batch (cached per request).

**Dependency module `apps/api/forge_api/deps/authz.py`** (replaces/augments the v1 `require_role`):

```python
async def get_principal_context(principal: Principal = Depends(get_principal),
                               session: AsyncSession = Depends(get_session)) -> PrincipalContext: ...

def require_permission(permission: Permission,
                      resource: ResourceResolver = workspace_resource) -> Callable[..., Awaitable[PrincipalContext]]:
    """FastAPI dependency factory. `resource` extracts a ResourceRef from path params
    (e.g. project_resource loads project_id -> {project_id, visibility, owner_team_id}).
    Resolves effective access via DefaultPermissionResolver; raises 403 (AccessDenied)
    if `permission` not present, or 404 if the resource is invisible (project.read absent
    on a team_restricted project) — 404, never 403, to avoid existence leak."""

async def visible_project_ids(ctx: PrincipalContext, workspace_id: UUID,
                             session: AsyncSession) -> set[UUID] | ALL:
    """Project ids the principal has project.read on. Returns sentinel ALL for workspace admin.
    Board list/query endpoints AND it into their workspace_id filter."""
```

`ResourceResolver` implementations: `workspace_resource`, `project_resource(project_id)`, `task_resource(task_id)` (loads the task's project), `team_resource(team_id)`. The v1 `require_role(min_role: UserRole)` dependency (owned by `cross-cutting/F37-auth-secrets-byok`, in `apps/api/forge_api/deps/auth.py`) is **re-homed to delegate to `require_permission`** as a back-compat shim — `require_role(UserRole.ADMIN)` resolves to `require_permission(Permission.WORKSPACE_ADMIN, workspace_resource)`, etc. — so already-written v1 routes keep working unchanged until migrated.

**Routers (`apps/api/forge_api/routers/`), mounted under `/api/v1`, all auth-required:**

- `teams.py` — `GET /teams`, `POST /teams` (`team.manage`), `GET /teams/{id}`, `PATCH /teams/{id}` (`team.manage`), `POST /teams/{id}/archive`; `GET /teams/{id}/members`, `POST /teams/{id}/members` (`team.member.manage`), `PATCH /teams/{id}/members/{user_id}` (set `team_role`), `DELETE /teams/{id}/members/{user_id}`.
- `access.py` — `GET /access/grants?principal_id=&scope_type=&scope_id=` (`role.grant` at scope, or self), `POST /access/grants` (grant), `DELETE /access/grants/{id}` (revoke); `GET /access/effective?principal_id=&project_id=` → `EffectiveAccess`.
- `project_access.py` — `GET /projects/{id}/access` (`project.read`), `PUT /projects/{id}/visibility` (`project.admin`), `GET|POST /projects/{id}/team-access` (`project.admin`), `DELETE /projects/{id}/team-access/{team_id}`.

**Board integration (F01) — minimal, contract-driven edits:** the board `projects`/`tasks`/`search`/`stream` list+query services call `visible_project_ids(ctx, ...)` and intersect it with their existing `workspace_id` filter. Single-entity board mutations swap `require_role` for `require_permission(Permission.PROJECT_WRITE | TASK_WRITE, project_resource)`. Project create checks `project.create`; project delete checks `project.delete`. These are the only board changes F30 owns.

### 3.3 Worker / agent runtime (Celery tasks, LangGraph, if any)

Celery (no LangGraph) — F30 owns one Beat task:

- `authz.purge_expired_grants` (every 5m, `apps/worker/forge_worker/tasks/authz.py`): `DELETE FROM role_grants WHERE expires_at IS NOT NULL AND expires_at < now()`; emits one `role_grant.expired` audit event per deleted grant (batched). Expiry is **authoritative at resolution time** (the resolver ignores `expires_at < now`); this task is hygiene/audit, so a missed run never grants stale access.

**Agent-runtime consumption (contract defined here; wired by `v1/F06-single-execution-agent` and `v3/F27-supervised-multi-agent`):** when a `WorkflowRun`/`AgentRun` starts, the orchestrator mints/uses a **project-scoped `agent_runner` grant** (with `expires_at = now + run_ttl`) for the run's service identity (the short-lived `agent_runner` platform key minted by `cross-cutting/F37-auth-secrets-byok`'s `mint_agent_key`), and builds the run's `PrincipalContext` from it. Every agent tool call that maps to an authz-gated action (open PR, write task, deploy) is checked via the same resolver — so an agent literally cannot resolve a permission outside its project-scoped `agent_runner` set (no `pr.approve`, no `role.grant`, no other project). This is the authz half of Build-Prompt constraint #2 ("the agent never self-assigns permissions or expands its own scope"); the `v1/F04-repo-policy` policy evaluator is the repo-policy half.

### 3.4 Frontend / UI (Next.js routes/components)

**App `apps/web`** (App Router, TS, Tailwind, shadcn/ui, TanStack Query + Table). Routes:

- `app/[workspace]/settings/teams/page.tsx` — teams list/create/archive; nesting shown as a tree.
- `app/[workspace]/settings/teams/[teamKey]/page.tsx` — team detail: members table, lead toggle, project-access summary.
- `app/[workspace]/settings/members/page.tsx` — workspace members with effective workspace role + per-row role management.
- Extend project settings: `app/[workspace]/projects/[projectKey]/settings/access/page.tsx` — visibility toggle + team-access table.

Components (`apps/web/components/authz/`): `TeamsTree.tsx`, `TeamDetail.tsx`, `MembersTable.tsx`, `RoleGrantDialog.tsx` (scope + role pickers; client disables roles above the actor's own at that scope), `ProjectAccessPanel.tsx` (visibility switch + per-team access-level rows), `EffectiveAccessInspector.tsx` (renders `EffectiveAccess` with the deriving grants). Data: `apps/web/lib/authz/{api,queries,mutations}.ts`. **Capability-aware UI**: `useCapabilities()` hook exposes the current user's `EffectiveAccess`; controls are hidden/disabled when the permission is absent (UI convenience only — the server is always authoritative). Team-restricted projects simply don't appear in the project list for users without access (driven by the `visible_project_ids` server filter).

### 3.5 Infra / deploy (compose, helm, caddy, if any)

No new compose services. Re-uses `db`, `redis` (Celery broker for the purge beat), `api`, `worker`, `web` from the foundation stack. Requirements: the `worker` must run Celery Beat so `authz.purge_expired_grants` fires (the foundation already enables Beat for board SLA scans — register one more schedule). Migration runs via the existing `forge-cli db migrate`. A `forge-cli users create-admin` (owned by `cross-cutting/F37-auth-secrets-byok`, which already creates the workspace + admin `app_user` + first admin platform key) must, post-F30, **also write a workspace-scope `admin` role_grant** for that user (in addition to setting `app_user.role = admin`), so the new admin is authorized under the scoped resolver.

---

## 4. Public interfaces / contracts (exact signatures, Pydantic models, Protocols, YAML schemas)

**Frozen DTOs + Protocol (`packages/contracts/forge_contracts/authz.py`):**

```python
from enum import StrEnum
from uuid import UUID
from datetime import datetime
from typing import Protocol, runtime_checkable
from pydantic import BaseModel, ConfigDict

# Reuse the foundation's flat role + principal type VERBATIM — do NOT redefine a parallel
# enum (that would drift the "agent-runner" value and break backfill equivalence AC1).
# UserRole already lives in forge_contracts.enums (mirroring forge_db.models.enums.UserRole):
#   ADMIN="admin", MEMBER="member", VIEWER="viewer", AGENT_RUNNER="agent-runner"  (HYPHEN).
from forge_contracts.enums import UserRole as Role          # the four spec roles, scoped by F30
from forge_contracts.auth import PrincipalType              # USER/API_KEY/SERVICE (from F37)

class ScopeType(StrEnum):
    WORKSPACE = "workspace"; TEAM = "team"; PROJECT = "project"

class TeamRole(StrEnum):
    LEAD = "lead"; MEMBER = "member"

class AccessLevel(StrEnum):
    READ = "read"; WRITE = "write"; ADMIN = "admin"

class ProjectVisibility(StrEnum):
    WORKSPACE = "workspace"; TEAM_RESTRICTED = "team_restricted"

class Permission(StrEnum):
    WORKSPACE_ADMIN     = "workspace.admin"
    MEMBER_MANAGE       = "member.manage"
    TEAM_MANAGE         = "team.manage"
    TEAM_MEMBER_MANAGE  = "team.member.manage"
    ROLE_GRANT          = "role.grant"
    PROJECT_CREATE      = "project.create"
    PROJECT_READ        = "project.read"
    PROJECT_WRITE       = "project.write"
    PROJECT_ADMIN       = "project.admin"
    PROJECT_DELETE      = "project.delete"
    TASK_READ           = "task.read"
    TASK_WRITE          = "task.write"
    SPEC_APPROVE        = "spec.approve"
    PR_APPROVE          = "pr.approve"
    DEPLOY_APPROVE      = "deploy.approve"
    AGENT_RUN           = "agent.run"
    AUDIT_READ          = "audit.read"
    KNOWLEDGE_MANAGE    = "knowledge.manage"
    MCP_MANAGE          = "mcp.manage"
    POLICY_MANAGE       = "policy.manage"
    INTEGRATION_MANAGE  = "integration.manage"
    SECRETS_MANAGE      = "secrets.manage"

class ScopeRef(BaseModel):
    model_config = ConfigDict(frozen=True)
    type: ScopeType
    id: UUID

class PrincipalRef(BaseModel):
    model_config = ConfigDict(frozen=True)
    type: PrincipalType
    id: UUID

class RoleGrant(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: UUID
    workspace_id: UUID
    principal: PrincipalRef
    scope: ScopeRef
    role: Role
    expires_at: datetime | None = None

class TeamMembership(BaseModel):
    model_config = ConfigDict(frozen=True)
    team_id: UUID
    team_role: TeamRole
    parent_team_id: UUID | None = None   # for nested-team inheritance

class ProjectTeamAccess(BaseModel):
    model_config = ConfigDict(frozen=True)
    project_id: UUID
    team_id: UUID
    access_level: AccessLevel

class ResourceRef(BaseModel):
    model_config = ConfigDict(frozen=True)
    workspace_id: UUID
    project_id: UUID | None = None
    team_id: UUID | None = None
    visibility: ProjectVisibility = ProjectVisibility.WORKSPACE

class PrincipalContext(BaseModel):
    model_config = ConfigDict(frozen=True)
    principal: PrincipalRef
    workspace_id: UUID
    grants: tuple[RoleGrant, ...]
    team_memberships: tuple[TeamMembership, ...]

class EffectiveAccess(BaseModel):
    permissions: frozenset[Permission]
    roles_by_scope: dict[ScopeType, frozenset[Role]]
    granting_sources: tuple[str, ...]   # human-readable, e.g. "workspace:member", "team BE→write"
    def can(self, permission: Permission) -> bool:
        return permission in self.permissions

@runtime_checkable
class PermissionResolver(Protocol):
    def resolve(self, principal: PrincipalContext, resource: ResourceRef, *,
                project_team_access: tuple[ProjectTeamAccess, ...] = (),
                now: datetime | None = None) -> EffectiveAccess: ...
    def can(self, principal: PrincipalContext, permission: Permission, resource: ResourceRef, *,
            project_team_access: tuple[ProjectTeamAccess, ...] = (),
            now: datetime | None = None) -> bool: ...
```

**Permission table (`packages/authz-sdk/forge_authz/permissions.py`) — frozen, the canonical role→permission mapping:**

```python
P = Permission
ROLE_PERMISSIONS: Mapping[Role, frozenset[Permission]] = {
    Role.ADMIN: frozenset(Permission),                      # every permission within the granted scope
    Role.MEMBER: frozenset({
        P.PROJECT_CREATE, P.PROJECT_READ, P.PROJECT_WRITE,
        P.TASK_READ, P.TASK_WRITE, P.SPEC_APPROVE, P.PR_APPROVE,
        P.KNOWLEDGE_MANAGE, P.AGENT_RUN, P.AUDIT_READ,
    }),
    Role.VIEWER: frozenset({P.PROJECT_READ, P.TASK_READ, P.AUDIT_READ}),
    Role.AGENT_RUNNER: frozenset({
        P.PROJECT_READ, P.TASK_READ, P.TASK_WRITE, P.AGENT_RUN, P.KNOWLEDGE_MANAGE,
    }),   # NOTE: no PR_APPROVE / DEPLOY_APPROVE / ROLE_GRANT / *_MANAGE-admin / MEMBER_MANAGE
}
ACCESS_LEVEL_ROLE: Mapping[AccessLevel, Role] = {
    AccessLevel.READ: Role.VIEWER, AccessLevel.WRITE: Role.MEMBER, AccessLevel.ADMIN: Role.ADMIN,
}
ROLE_RANK: Mapping[Role, int] = {Role.VIEWER: 0, Role.AGENT_RUNNER: 0, Role.MEMBER: 1, Role.ADMIN: 2}
```

`workspace.admin`-only permissions (e.g. `TEAM_MANAGE`, `MEMBER_MANAGE`, `SECRETS_MANAGE`, `MCP_MANAGE`, `INTEGRATION_MANAGE`, `POLICY_MANAGE`, `DEPLOY_APPROVE`, `PROJECT_DELETE`, `ROLE_GRANT`) are in `Role.ADMIN` only; at **project** scope `Role.ADMIN` confers the project-scoped subset (`PROJECT_ADMIN`, `PROJECT_WRITE/READ/DELETE`, `ROLE_GRANT` for project scope, `DEPLOY_APPROVE`, `TASK_*`) but **not** workspace-only permissions — see the scope-narrowing rule below.

**`resolve()` algorithm (pure, total, no I/O — every input returns an `EffectiveAccess`):**

1. `now := now or utcnow()`. Drop any grant with `expires_at is not None and expires_at <= now`.
2. Start `perms = ∅`, `roles_by_scope = {}`, `sources = []`.
3. **Workspace grants** (`scope.type=workspace`, `scope.id=resource.workspace_id`): for each, `r=grant.role`. If `resource.project_id` is set and `resource.visibility == TEAM_RESTRICTED` and `r != ADMIN`: **skip** (multi-team wall — only workspace admin bypasses restriction). Else `perms |= scope_narrow(ROLE_PERMISSIONS[r], ScopeType.WORKSPACE)`; record source.
4. **Project grants** (`scope.type=project`, `scope.id=resource.project_id`): `perms |= scope_narrow(ROLE_PERMISSIONS[grant.role], ScopeType.PROJECT)`.
5. **Team-derived** (only when `resource.project_id` set): build the principal's team set = memberships ∪ ancestor teams of each membership via `parent_team_id` (capped at `MAX_TEAM_DEPTH`). For each `ProjectTeamAccess(project_id=resource.project_id, team_id ∈ team set)`: `r=ACCESS_LEVEL_ROLE[access_level]`; `perms |= scope_narrow(ROLE_PERMISSIONS[r], ScopeType.PROJECT)`. If the principal is `lead` of `resource.team_id` (or any team), add `TEAM_MEMBER_MANAGE` for that team scope.
6. **Team-scope grants** (`scope.type=team`): if a matching `ProjectTeamAccess(team_id=scope.id, project_id=resource.project_id)` exists, `perms |= scope_narrow(ROLE_PERMISSIONS[grant.role], ScopeType.PROJECT)`.
7. Return `EffectiveAccess(permissions=frozenset(perms), roles_by_scope=…, granting_sources=tuple(sources))`.

`scope_narrow(perms, scope_type)`: at `PROJECT`/`TEAM` scope, intersect out workspace-only permissions (`MEMBER_MANAGE`, `TEAM_MANAGE`, `SECRETS_MANAGE`, `MCP_MANAGE`, `INTEGRATION_MANAGE`, `POLICY_MANAGE`, `WORKSPACE_ADMIN`) so a project admin cannot manage workspace members; `ROLE_GRANT`/`DEPLOY_APPROVE`/`PROJECT_DELETE` remain (project admins manage project-scoped grants/deploys). The narrow set is a frozen constant `WORKSPACE_ONLY_PERMISSIONS`.

**Result is the union of all applicable grants** (additive RBAC; absence of a granting role ⇒ no permission ⇒ deny). There is **no explicit-deny** rule — conditional/deny rules are the separate Advanced Policy Engine slice (§12).

**Escalation & lockout invariants (enforced in `authz_service`, unit-tested):**

- `grant_role`/`revoke_role`: actor must have `ROLE_GRANT` at the target scope **and** `ROLE_RANK[actor_role_at_scope] >= ROLE_RANK[target_role]`. Otherwise `EscalationError` → 403.
- `LastAdminError` (409) if a revoke/replace would leave a workspace with **zero** workspace-scope `admin` grants.
- An `agent_runner`/service principal never satisfies `ROLE_GRANT` (not in its set) → cannot grant.

**REST request/response models (`apps/api/forge_api/schemas/authz.py`):**

```python
class TeamIn(BaseModel): key: str; name: str; description: str | None = None; parent_team_id: UUID | None = None
class TeamOut(TeamIn): id: UUID; archived_at: datetime | None; created_at: datetime
class TeamMemberIn(BaseModel): user_id: UUID; team_role: TeamRole = TeamRole.MEMBER
class TeamMemberOut(BaseModel): user_id: UUID; team_role: TeamRole; created_at: datetime
class RoleGrantIn(BaseModel):
    principal: PrincipalRef; scope: ScopeRef; role: Role; expires_at: datetime | None = None
class RoleGrantOut(RoleGrantIn): id: UUID; granted_by: UUID | None; created_at: datetime
class ProjectVisibilityIn(BaseModel): visibility: ProjectVisibility; owner_team_id: UUID | None = None
class ProjectTeamAccessIn(BaseModel): team_id: UUID; access_level: AccessLevel
class EffectiveAccessOut(BaseModel):
    principal: PrincipalRef; resource: ResourceRef
    permissions: list[Permission]; roles_by_scope: dict[ScopeType, list[Role]]
    granting_sources: list[str]
```

**Error contract:** `403 {"error":"forbidden","missing_permission":"<perm>","scope":{"type":...,"id":...}}`; `404` for invisible/cross-workspace resources (no existence leak); `409 {"error":"last_admin"}`, `409 {"error":"team_cycle","path":[...]}`, `409 {"error":"team_depth_exceeded"}`; `422` for escalation attempts surfaced as `{"error":"escalation","granted_role":...,"actor_max_role":...}` (403 status). Validation = FastAPI `422`.

**Audit event types** (emitted as `forge_contracts.audit.AuditEvent` through the canonical `AuditSink` of `cross-cutting/F39-audit-log`, persisted into the shared immutable `audit_log` table; these extend the auth action vocabulary established by `cross-cutting/F37-auth-secrets-byok`): `role_grant.created|revoked|expired`, `team.created|updated|archived`, `team_member.added|removed|role_changed`, `project_access.visibility_changed|team_access_set|team_access_removed`. Each carries actor, workspace, target principal/scope, before/after role, result, timestamp.

---

## 5. Dependencies — features/slices that must exist first

Hard prerequisites:
- **`cross-cutting/F37-auth-secrets-byok`** (the auth/secrets/flat-RBAC foundation; sibling slices also call it `v1/F15-auth-secrets-rbac` / `cross-cutting/C02-auth-and-rbac` / `v1/F02-auth-workspace-byok` — the authoritative slug is `cross-cutting/F37-auth-secrets-byok`) — provides `workspace`, `app_user` (with the flat `role: UserRole`), `api_key` + `platform_api_key`, the `Principal` + `get_principal` auth dependency (`forge_api/deps/auth.py`), the flat `require_role(min_role)`, `forge_contracts.auth` (`Principal`/`PrincipalType`/`UserRole`), and `forge-cli users create-admin`. F30 **replaces** the flat role with `role_grants`, builds `PrincipalContext` on top of `get_principal`, re-homes `require_role` as a shim, and extends `create-admin`.
- **`v1/F01-project-board`** — provides the `project`/`task`/`teams` tables (the base `teams` table + `task.team_id` + team-scoped saved filters) and the board routers/services F30 extends (`visibility`/`owner_team_id` columns, `visible_project_ids` filter, `require_permission` swap).
- **`cross-cutting/F39-audit-log`** — the canonical `forge_contracts.audit.AuditEvent` + `AuditSink` + `SqlAuditWriter` and the shared immutable `audit_log` table (table origin: `cross-cutting/F37-auth-secrets-byok`); the durable sink for all F30 authz audit events.
- **`packages/contracts` (`forge_contracts`)** — frozen DTO/Protocol home (F30 adds `authz.py`; reuses `forge_contracts.enums.UserRole`, `forge_contracts.auth.PrincipalType`, `forge_contracts.audit`).

Soft / consumers (do not block F30; they bind to F30's resolver when they land):
- **`v3/F27-supervised-multi-agent`** — uses project-scoped `agent_runner` grants + scope-narrowed permission sets for subagents.
- **`v3/F31-deployment-gates`** — uses `DEPLOY_APPROVE` resolved per project/team.
- **`cross-cutting/F36-human-approval-system`** — the canonical approval service that owns the `spec`/`plan`/`pr`/`deploy`/`policy_override` gates; F30 supplies the `spec.approve` / `pr.approve` / `deploy.approve` permissions it authorizes against, and the rule that an `agent_runner` can never hold them (no self-approval).
- **`v1/F04-repo-policy`, `v1/F08-plan-execute-verify-pr-approval`, `v1/F02-spec-engine`** — their `require_role` checks migrate to `require_permission` (`policy.manage`, `pr.approve`/`spec.approve`).
- **`v1/F09-mcp-gateway-v1`, `v1/F05-hybrid-knowledge-retrieval`, `v1/F03-github-app`** — admin routes migrate to `mcp.manage`/`knowledge.manage`/`integration.manage` (MCP write-enable stays admin-only per the spec's read-only default).
- **`v3/F29-advanced-policy-engine`** — the separate conditional/ABAC policy slice; F30 stays additive-union RBAC and does not implement deny/conditional rules.

---

## 6. Acceptance criteria (numbered, testable)

1. **Backfill**: after migration, every prior `app_user(role)` row has exactly one matching workspace-scope `role_grants` row (`principal_type=user`, `scope_type=workspace`, `scope_id=workspace_id`, same `role`); `resolve()` for that user on a workspace-visibility project yields the same permissions the flat role gave in v1.
2. **Workspace role precedence**: a workspace `member` has `{project.read, project.write, task.write, pr.approve}` on a `workspace`-visibility project and **not** `role.grant`/`member.manage`.
3. **Viewer read-only**: a workspace `viewer` resolves `project.read`/`task.read` only; any write/approve/grant permission is absent.
4. **Project elevation (additive union)**: a workspace `member` **plus** a project-scope `admin` grant on `CORE` has `project.admin` + `role.grant` (project-scoped) on `CORE`, but on another project remains a plain member.
5. **Scope narrowing**: a **project** `admin` does **not** resolve `member.manage`/`team.manage`/`secrets.manage`/`workspace.admin` (workspace-only permissions excluded at project scope) but **does** resolve project-scoped `role.grant` + `deploy.approve` + `project.delete`.
6. **Team membership confers access**: a user in `Team BE` where `project_team_access(CORE, BE, write)` exists resolves `project.write`/`task.write` on `CORE` with **no** workspace grant on `CORE` (team-only path).
7. **Nested teams**: a user in child team `BE` (parent `Engineering`), where access is granted to `Engineering`, inherits that access on `CORE`; inheritance stops at `MAX_TEAM_DEPTH`.
8. **Team-restricted wall**: with `CORE.visibility=team_restricted` and access granted only to `Team BE`, a workspace `viewer`/`member` **not** in `BE` resolves no `project.read` on `CORE`; `GET /projects/CORE` returns **404** and `CORE` is absent from their `visible_project_ids`.
9. **Admin bypass**: a workspace `admin` resolves full access on a `team_restricted` project with no team membership (admin is the only bypass).
10. **Agent cannot self-approve / escalate**: a project-scoped `agent_runner` resolves `task.write`/`agent.run` but **not** `pr.approve`, `deploy.approve`, or `role.grant`; `POST /access/grants` as that principal → 403.
11. **No upward escalation**: a workspace `member` (or project admin) calling `POST /access/grants` to grant `admin` at a scope where they are not admin → 403 `escalation`; granting a role ≤ their own at a scope they admin → 201.
12. **Lockout prevention**: revoking/replacing the **last** workspace `admin` grant → 409 `last_admin`; the grant is not removed.
13. **Expiry**: a grant with `expires_at` in the past does not contribute to `resolve()` (authoritative) even before the purge task runs; after `authz.purge_expired_grants`, the row is deleted and a `role_grant.expired` audit event exists; re-run is idempotent (no second event).
14. **Team CRUD + cycle/depth**: creating a team with `parent_team_id` forming a cycle → 409 `team_cycle`; exceeding `MAX_TEAM_DEPTH` → 409 `team_depth_exceeded`.
15. **Team-lead scope**: a team `lead` (not workspace admin) can add/remove members and set team roles on **their** team (200) but gets 403 managing another team, creating teams, or granting workspace roles.
16. **Project access management**: a `project.admin` can set visibility and upsert/remove `project_team_access` (200); a workspace `member` without project admin → 403.
17. **Board list filtering**: `POST /projects/{id}/tasks/query` and the project list only return projects/tasks the caller has `project.read` on; a `team_restricted` project's tasks never appear for a non-member non-admin (integration test against a seeded multi-team workspace).
18. **Audit completeness**: every grant/revoke/expire, team change, membership change, and visibility/team-access change writes exactly one immutable audit event with actor + scope + before/after; no authz mutation path skips the audit.
19. **Resolver totality**: a Hypothesis suite over random `PrincipalContext`/`ResourceRef`/`ProjectTeamAccess` always returns an `EffectiveAccess` and never raises; `resolve()` is deterministic (same inputs → identical output) and order-independent over grants.
20. **Permission-table integrity**: `ROLE_PERMISSIONS[VIEWER] ⊆ ROLE_PERMISSIONS[MEMBER] ⊆ ROLE_PERMISSIONS[ADMIN]`; `AGENT_RUNNER` excludes `PR_APPROVE`, `DEPLOY_APPROVE`, `ROLE_GRANT`, `MEMBER_MANAGE` (asserted by a test, guarding against accidental privilege drift).
21. **Back-compat shim**: an unmigrated route still using `require_role(UserRole.ADMIN)` (the F37 dependency) behaves identically to `require_permission(Permission.WORKSPACE_ADMIN, workspace_resource)` for the same principal; `require_role(UserRole.MEMBER)`/`(VIEWER)`/`(AGENT_RUNNER)` map to the equivalent workspace-scope permission of that role.
22. **No anonymous access / cross-workspace**: every F30 route rejects unauthenticated requests (401) and returns 404 for resources in another workspace.

### Traceability: spec requirement → criteria

| Spec requirement | Criteria |
|---|---|
| RBAC roles per workspace **and per project** | 2, 3, 4, 5, 16, 21 |
| Multi-team workspace controls | 6, 7, 8, 9, 14, 15, 16, 17 |
| Full RBAC hierarchy / precedence | 4, 5, 7, 11 |
| Agent never self-expands scope | 10, 11, 20 |
| Lockout / escalation safety | 11, 12 |
| Automatic expiry for agent tokens | 13 |
| Immutable audit log | 18 |
| Determinism / fail-closed | 19, 20, 22 |
| Backward compatibility with v1 flat RBAC | 1, 21 |

---

## 7. Test plan (TDD) — concrete test cases (unit + integration), key fixtures

Write tests first; each maps to an AC.

**Unit — resolver (`packages/authz-sdk/tests/test_resolver.py`, no DB, table-driven):** a parametrized matrix of `(grants, team_memberships, project_team_access, resource, expected_permissions)` covering AC2–AC10 (workspace roles, project elevation/union, scope narrowing, team membership, nested teams, team-restricted wall, admin bypass, agent-runner exclusions). `test_resolve_is_total` (Hypothesis, AC19) — random inputs never raise, deterministic, order-independent (shuffle `grants`, assert equal output). `test_expired_grant_ignored` (AC13).

**Unit — permission table (`tests/test_permissions.py`, AC20):** subset chain Viewer⊆Member⊆Admin; agent-runner exclusions; `WORKSPACE_ONLY_PERMISSIONS` excluded by `scope_narrow` at project scope (AC5).

**Unit — invariants (`tests/test_invariants.py`):** `grant_role` escalation guard (AC11) and `LastAdminError` (AC12) tested against an in-memory grant set (service logic factored to operate on a list for pure testing); team cycle/depth (AC14).

**API integration (`apps/api/tests/authz/`, httpx ASGI + Postgres test-container, factory-boy):**
- `test_backfill.py` (AC1) — seed v1-style `app_user` rows (one per role) under the pre-F30 schema, run migration to head, assert one workspace-scope `role_grants` row per user + equivalent `resolve()` output.
- `test_grants.py` (AC11, AC12, AC22) — grant/revoke happy paths; escalation 403; last-admin 409; unauth 401; cross-workspace 404.
- `test_teams.py` (AC14, AC15) — team CRUD, cycle/depth 409, lead-scope 200/403.
- `test_project_access.py` (AC16, AC8, AC9) — visibility switch, team-access upsert/remove, restricted 404 for outsiders, admin bypass.
- `test_effective_access.py` (AC4, AC5, AC6) — `GET /access/effective` returns expected permission set + `granting_sources`.
- `test_board_visibility.py` (AC17) — **board integration**: seed two teams + a `team_restricted` project; assert task-query and project-list filtering for member-in-team vs outsider vs admin.
- `test_audit.py` (AC18) — each mutation writes exactly one audit event; assert actor/scope/before-after.
- `test_agent_runner.py` (AC10) — project-scoped agent_runner principal: write OK, approve/grant 403.
- `test_require_role_shim.py` (AC21).

**Worker (`apps/worker/tests/test_authz_task.py`, AC13):** seed expired + live grants; run `purge_expired_grants`; assert deletes + one `role_grant.expired` event each; idempotent re-run.

**Frontend (`apps/web`, Vitest + RTL + MSW; Playwright for e2e):**
- `RoleGrantDialog.test.tsx` — roles above the actor's own at a scope are disabled.
- `ProjectAccessPanel.test.tsx` — visibility toggle + team-access rows post correctly.
- `EffectiveAccessInspector.test.tsx` — renders permissions + sources from a mocked `EffectiveAccess`.
- `team-restricted.spec.ts` (Playwright) — a non-member user does not see a restricted project in the list and 404s on its URL.

**Key fixtures:** `workspace_factory`, `user_factory`, `team_factory(parent=...)`, `grant_factory(principal, scope, role, expires_at=None)`, `project_factory(visibility=...)`, `project_team_access_factory`, `principal_context(...)` builder, and `seed_multi_team_workspace` (2 teams, 1 nested child, 3 projects: workspace-visible / team-restricted-to-BE / team-restricted-to-PLT, ~6 users across roles) reused by resolver and board-visibility tests.

---

## 8. Security & policy considerations

- **Deny-by-default / fail-closed.** Authorization is the **union of explicit grants**; the absence of a granting role is a deny. `require_permission` denies on any unmatched permission. The resolver is total (AC19) so no input path silently allows.
- **Existence-leak prevention.** Invisible (`team_restricted` without access) and cross-workspace resources return **404, never 403**, so role/structure isn't enumerable (AC8, AC22).
- **No self-escalation / no self-grant.** Granting requires `ROLE_GRANT` **and** rank ≥ the granted role at that scope (AC11). `agent_runner` never holds `ROLE_GRANT` → an agent literally cannot widen its own scope (Build-Prompt constraint #2; complements F04's pure policy evaluator).
- **No lockout.** The last workspace admin cannot be removed/demoted (AC12) — operational safety.
- **Least privilege by scope.** Project/team admins get a **scope-narrowed** set; workspace-only powers (member/team/secrets/integration management) never leak to project admins (AC5).
- **Spec-gate / approval / MCP / BYOK non-negotiables map to explicit permissions.** `spec.approve` gates spec-approved-before-implementation; `pr.approve` (held by `member`+, never `agent_runner`) gates human-approval-before-merge — resolved through `cross-cutting/F36-human-approval-system`, which additionally forbids self-approval of one's own run. `mcp.manage` is **admin-only**, so enabling an MCP connection's write access (the spec's read-only-by-default rule, `v1/F09-mcp-gateway-v1`) is structurally an admin action. `secrets.manage` (BYOK vault writes) and `integration.manage` are likewise admin-only. No role below `admin` (or its scope-narrowed project equivalent) can resolve these.
- **Automatic expiry.** Agent/service grants carry `expires_at`, ignored at resolution once past and purged by the beat task (Security: "automatic expiry for agent tokens"; AC13).
- **Immutable, queryable audit.** Every authz change is an append-only audit event (actor, scope, before/after) — the permanent history is the audit log, not the mutable `role_grants` table (AC18).
- **Tenant isolation.** Every table is `workspace_id`-scoped; the resolver only consumes grants/memberships within the principal's workspace; routes assert workspace ownership.
- **Performance / DoS.** Per-request `PrincipalContext` is loaded once (batched query, indexed by `(workspace_id, principal_type, principal_id)`) and cached on the request; resolution is in-memory and bounded by the principal's grant count. `visible_project_ids` returns an `ALL` sentinel for workspace admins to avoid materializing every project id.
- **Capability UI is convenience only.** The web layer hides/disables controls by capability, but the server (`require_permission`) is always authoritative — a hand-crafted request is still rejected.

---

## 9. Effort estimate & risk (S/M/L + key risks)

**Effort: L** (~3 engineer-weeks: ~1.0 authz-sdk core + service + deps, ~0.75 router/board integration + migration/backfill, ~0.75 frontend settings surface, ~0.5 tests/wiring/migration of existing `require_role` call sites).

| Risk | Severity | Mitigation |
|---|---|---|
| **Migrating every existing `require_role` call site** mis-maps a permission, silently widening/narrowing access | High | Back-compat shim (AC21) keeps unmigrated routes correct; migrate route-by-route with per-route tests; the shim is the safe default until a route is explicitly mapped |
| Resolver precedence subtly wrong (team-restricted bypass, nested-team inheritance, scope narrowing) | High | Pure, total resolver with an exhaustive table-driven matrix + Hypothesis (AC19); precedence rule documented in §4 and asserted per-case |
| Lockout / privilege-escalation hole | High | Explicit `LastAdminError`/`EscalationError` invariants unit-tested on pure logic (AC11, AC12); enforced only in the single `authz_service` writer |
| Board-visibility filter missed on a list endpoint (data leak) | Med/High | `visible_project_ids` is the single sanctioned filter; the board-visibility integration test (AC17) covers query + list + stream; a checklist of every project-scoped read route in §3.2 |
| Resolution latency on hot board paths | Med | Indexed batched `PrincipalContext` load, per-request cache, in-memory resolution, `ALL` sentinel for admins |
| Backfill data correctness across many workspaces | Med | Idempotent, reversible migration with an equivalence assertion test (AC1) before deprecating `app_user.role` |
| Nested teams enabling unbounded inheritance | Low/Med | `MAX_TEAM_DEPTH` cap + cycle check (AC7, AC14) |

---

## 10. Key files / paths (exact)

**Contracts:**
- `packages/contracts/forge_contracts/authz.py`

**Core package:**
- `packages/authz-sdk/pyproject.toml`
- `packages/authz-sdk/forge_authz/__init__.py`
- `packages/authz-sdk/forge_authz/{permissions,resolver,errors,schema}.py`
- `packages/authz-sdk/tests/{test_resolver,test_permissions,test_invariants}.py`

**Data model + migration:**
- `packages/db/forge_db/models/{role_grant,team,team_member,project_team_access}.py`
- `packages/db/forge_db/models/project.py` (add `visibility`, `owner_team_id`)
- `packages/db/migrations/versions/00NN_f30_multi_team_rbac.py` (the in-tree Alembic tree is `packages/db/migrations/`, baseline `0001_baseline`)

**API:**
- `apps/api/forge_api/routers/{teams,access,project_access}.py`
- `apps/api/forge_api/services/authz_service.py`
- `apps/api/forge_api/schemas/authz.py`
- `apps/api/forge_api/deps/authz.py` (`require_permission`, `get_principal_context`, `visible_project_ids`, resource resolvers, `require_role` shim)
- `apps/api/forge_api/cli/users.py` (extend `create-admin` to write a workspace-admin grant)
- `apps/api/tests/authz/test_*.py`

**Worker:**
- `apps/worker/forge_worker/tasks/authz.py` (`purge_expired_grants` + beat schedule)
- `apps/worker/tests/test_authz_task.py`

**Board integration (edits owned by F30):**
- `apps/api/forge_api/routers/board/{projects,tasks,search,stream}.py` (swap `require_role`→`require_permission`; AND `visible_project_ids`)

**Frontend:**
- `apps/web/app/[workspace]/settings/teams/page.tsx`
- `apps/web/app/[workspace]/settings/teams/[teamKey]/page.tsx`
- `apps/web/app/[workspace]/settings/members/page.tsx`
- `apps/web/app/[workspace]/projects/[projectKey]/settings/access/page.tsx`
- `apps/web/components/authz/{TeamsTree,TeamDetail,MembersTable,RoleGrantDialog,ProjectAccessPanel,EffectiveAccessInspector}.tsx`
- `apps/web/lib/authz/{api,queries,mutations}.ts`, `apps/web/lib/authz/useCapabilities.ts`
- `apps/web/tests/authz/*.{test.tsx,spec.ts}`

---

## 11. Research references (relevant links from the spec/research report)

- `docs/FORGE_SPEC.md` §"Security" — *"RBAC: admin, member, viewer, agent-runner roles **per workspace and per project**"*; immutable audit log; automatic expiry for agent tokens; no anonymous access.
- `docs/FORGE_SPEC.md` §"Core Data Model" — `Workspace → User[] (roles…)`, `teams`, and the per-project Task fields that carry `requires_approval`/`execution_mode` (the actions F30 gates).
- `docs/FORGE_SPEC.md` §"Native Project Board" — team-scoped projects, `team` field, team-scoped saved filters (the multi-team surface F30 secures).
- `docs/FORGE_SPEC.md` §"Phased Roadmap → Phase 3" — *"Multi-team workspace controls and full RBAC hierarchy"* (this slice); adjacent items "Advanced policy engine with conditional rules", "Enterprise SSO (SAML, SCIM)", "Deployment gates and environment promotion workflows" (see §12).
- `docs/FORGE_SPEC.md` §"Build Prompt" constraint #2 — *"The agent never self-assigns permissions or expands its own scope."*
- `docs/FORGE_SPEC.md` §"Multi-Agent Orchestration" — subagent scoped-tools rationale (the `agent_runner` project-scoped, scope-narrowed permission set F30 supplies to `v3/F27-supervised-multi-agent`).
- Better Auth (OSS auth provider used by the foundation `cross-cutting/F37-auth-secrets-byok` F30 builds on): https://www.better-auth.com/
- In-repo precedent:
  - `docs/implementation-slices/v1/F04-repo-policy.md` — pure, total, deny-by-default `PolicyEvaluator`; F30's `PermissionResolver` mirrors its discipline.
  - `docs/implementation-slices/v1/F01-project-board.md` — tenant isolation, `scoped_query`, `teams`, `task.team_id`, team-scoped saved filters, audit/activity events.
  - `docs/implementation-slices/cross-cutting/F37-auth-secrets-byok.md` — `Principal`/`PrincipalType`/`UserRole`, `get_principal`, flat `require_role` (the shim target), `forge_contracts.auth`, `audit_log` table origin, `create-admin`.
  - `docs/implementation-slices/cross-cutting/F39-audit-log.md` — canonical `forge_contracts.audit.AuditEvent` + `AuditSink` + `SqlAuditWriter` that F30 emits through.
  - In-tree models F30 builds on: `packages/db/forge_db/models/workspace.py` (`User.role: UserRole`, no `workspace_members`), `models/project.py`, `models/enums.py` (`UserRole.AGENT_RUNNER = "agent-runner"`).

---

## 12. Out of scope / future

- **Custom roles / arbitrary permission sets.** F30 ships the four built-in roles (scoped). Workspace-defined custom roles with bespoke permission bundles are future.
- **Conditional / attribute-based (ABAC) and explicit-deny rules** — owned by `v3/F29-advanced-policy-engine` (*"Advanced policy engine with conditional rules"*). F30 is additive-union RBAC only; `v1/F04-repo-policy` remains the repo-policy gate.
- **Enterprise SSO (SAML) and SCIM directory provisioning** — owned by `v3/F33-enterprise-sso` (*"Enterprise SSO (SAML, SCIM)"*). F30 manages grants/teams within Forge; F33's SCIM auto-mapping of external accounts/groups to teams+grants binds to F30's `authz_service` later.
- **Organization tier above workspace** (multiple workspaces under one org with org-level admins) — future; F30 is workspace-rooted.
- **`DEPLOY_APPROVE`-driven environment-promotion workflow** — F30 defines and resolves the `deploy.approve` permission per scope; the actual gate/promotion FSM is `v3/F31-deployment-gates`.
- **Migrating every legacy `require_role` call site in one pass** — F30 provides the shim and migrates the board + its own routes; remaining v1/v2 admin routes (MCP, knowledge, integrations, policy, approvals) migrate incrementally behind the shim, tracked per consuming slice (§5).
- **Time-bounded "break-glass" elevation and approval-to-grant workflows** — future; F30 supports `expires_at` but not an approval flow for temporary elevation.
