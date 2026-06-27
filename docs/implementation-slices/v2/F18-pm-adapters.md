# F18 — External PM Adapters (Jira, Linear)

> Phase: v2 · Spec module(s): Native Project Board → "External PM Adapter Contract (`PMAdapter` Protocol)", Integrations → V2 (Jira, Linear — PM sync bidirectional), Core Data Model (`Project`/`Task`), Security (secrets, audit, RBAC, redaction), OSS Strategy → Extension Points ("PMAdapter interface: add any external board integration") · Status target: **Done** = a workspace admin can connect a Forge project to a Jira project or a Linear team; an initial backfill links existing issues to Forge tasks; thereafter changes flow **bidirectionally** (Forge task create/update/status/priority/assignee → external issue, and external issue create/update/delete → Forge task) with loop/echo suppression, deterministic conflict resolution, signature/secret-verified inbound webhooks, per-operation immutable audit, and zero live network calls in the test suite (all provider I/O goes through a `FixturePMTransport`). Lint + types + `pytest` green on `packages/integration-sdk` (the `forge_integrations/pm/*` tree), the `apps/api` PM router, and the `apps/worker` PM tasks.

---

## 1. Intent — what & why

Forge's board is the task-as-control-plane (Core Design Principle #6: "Internal board first, integrations second. Native board competes with Linear while exposing adapters for Jira, Linear, Asana, Monday.com"). Many teams already run Jira or Linear and will not move wholesale; F18 lets Forge act as the orchestration brain while keeping the team's existing tracker in sync. This is the first concrete realization of the spec's **`PMAdapter` Protocol** (Native Project Board → "External PM Adapter Contract") and of the V2 integration line item "External PM adapters (Jira, Linear)" (Phased Roadmap → Phase 2).

This slice delivers four things:

1. **The provider-agnostic `PMAdapter` Protocol + sync engine** — a single `PMSyncEngine` that maps Forge `ForgeTask` ↔ external `ExternalTask`, suppresses echo loops, detects and resolves conflicts, and persists durable links — usable by any future adapter (Asana, Monday, GitLab issues) without re-implementing sync logic. This satisfies OSS Strategy → Extension Points ("PMAdapter interface: add any external board integration").
2. **A `JiraAdapter`** over the Jira Cloud REST API v3.
3. **A `LinearAdapter`** over the Linear GraphQL API.
4. **The plumbing**: connection registration + OAuth/API-token auth (stored in the F37 vault), inbound webhook intake (signature/secret verified, deduped), an outbound change-feed driven off the board's append-only `activity_events` table, Celery sync tasks, an admin settings UI, and a manual-conflict inbox.

Why bidirectional via a durable change-feed rather than ad-hoc callbacks: the board (F01) already writes an **append-only `activity_events`** row for every meaningful task mutation and publishes to Redis `board:{project_id}`. F18 treats `activity_events` as a transactional **outbox** (durable, ordered, replayable) for the OUT direction and provider webhooks (verified, deduped, then reconciled by re-fetch) as the IN direction. State never depends solely on a single webhook — webhooks are change *hints*; the authoritative external state is always re-fetched (same philosophy as F03's GitHub reconciliation), which is what makes the sync robust against out-of-order/at-least-once delivery and unsigned Jira payloads.

Why fixtures-only in the build: the build constraints forbid live external calls. Every provider interaction goes through a `PMTransport` Protocol with recorded HTTP/GraphQL fixtures (`FixturePMTransport`), mirroring F03's `FixtureGitHubTransport`. Live verification against real Jira/Linear is a post-merge task, out of overnight scope.

---

## 2. User-facing behavior / journeys

**J1 — Connect a Forge project to Jira/Linear (admin).** Admin opens Settings → Integrations → Project Management → "Add connection", picks a provider (Jira | Linear), selects the Forge project to sync, authenticates (Jira: OAuth 3LO or email+API-token; Linear: OAuth or personal API key), then chooses the external target (Jira project key, e.g. `ENG`; Linear team key, e.g. `ENG`). Forge fetches the external project's statuses/priorities and pre-fills the **status map** and **priority map** (category-based defaults, editable). Admin picks a **sync direction** (bidirectional | inbound-only | outbound-only) and a **conflict policy** (forge-wins | external-wins | newest-wins | manual). On save the connection is `pending`; "Test connection" flips it to `connected` and shows the connected account + granted scopes.

**J2 — Register the webhook (admin, automatic).** On connect, Forge registers a provider-side webhook pointed at `…/api/v1/integrations/pm/webhooks/{provider}/{connection_id}` for create/update/delete issue events, storing the provider webhook id (for teardown) and a per-connection signing secret in the vault. Linear webhooks are HMAC-signed; Jira REST webhooks carry a per-connection secret in the registered URL.

**J3 — Initial backfill (admin).** Admin clicks "Import & link". `pm.backfill` pulls every issue in the external project/team, and for each: if a Forge task already maps (by stored link) it reconciles; otherwise it creates a linked Forge task. Optionally (outbound-only or bidirectional) existing Forge tasks without an external counterpart are pushed out as new external issues. Progress + counts are shown; the connection records `last_full_sync_at`.

**J4 — Forge → external (system).** A user edits a Forge task (title, description, status, priority, assignee, labels). The board writes an `activity_events` row. F18's outbound scanner (beat) picks it up, the sync engine maps the task → `ExternalTask` and the `JiraAdapter`/`LinearAdapter` issues the create/update/transition. The Forge change appears in Jira/Linear within the sync interval (default ≤ 30s), and the task's timeline shows a `pm_sync` activity event.

**J5 — External → Forge (system).** Someone moves the Jira/Linear issue to "In Progress" or reassigns it. The provider sends a webhook; Forge verifies it, dedupes by event id, re-fetches the authoritative issue, maps it → `ForgeTask` patch, and applies it via the board service. The Forge task updates (and its timeline shows a `pm_sync` event) without a manual refresh (the board's SSE stream from F01 pushes it to open clients).

**J6 — Conflict (system + admin).** Both sides changed the same task since the last sync. With `newest_wins`/`forge_wins`/`external_wins` the engine applies the policy automatically and records which side won. With `manual`, the engine writes the link to `sync_state=conflict`, applies **no** write, and surfaces it in the **Conflict inbox**; the admin sees both versions side-by-side and clicks "Keep Forge" / "Keep external", which resolves and re-syncs.

**J7 — Disconnect (admin).** Admin clicks "Disconnect": Forge unregisters the provider webhook, marks the connection `disabled`, stops processing its webhooks and outbound scan, and retains links + audit history (re-connect re-links by stored external ids).

---

## 3. Vertical slice

### 3.1 Data model (tables/columns/migrations touched)

One Alembic migration `packages/db/migrations/versions/<rev>_pm_adapters.py` (depends on the F01 board migration that creates `projects`/`tasks`/`task_statuses`/`activity_events`, and the F37 vault). ORM models live in `packages/db/forge_db/models/`. Every table carries `workspace_id` (tenant scope) + `created_at`/`updated_at`.

> Cross-slice convention note: F03 places integration models/migrations in `packages/db` (`forge_db`); F09 placed MCP models in `apps/api/app/models` + `apps/api/alembic`. F18 follows **F03** (it is the direct continuation of the integration-sdk surface). If the foundation slice standardized on the F09 location, move the three models there and keep the migration in the chosen single Alembic tree — the column set below is unchanged either way.

**`pm_connections`** — one row per (Forge project ↔ external project/team). Maps the spec's PM integration onto `Project` + `RepositoryConnection`-style connection records.

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | `gen_random_uuid()` |
| `workspace_id` | uuid FK→`workspaces.id` CASCADE | tenant scope; indexed |
| `provider` | enum `pm_provider` (`jira`\|`linear`) | |
| `name` | text | display name |
| `project_id` | uuid FK→`projects.id` CASCADE | the Forge project synced by this connection |
| `external_base_url` | text NULL | Jira site (`https://acme.atlassian.net`); Linear fixed (null) |
| `jira_cloud_id` | text NULL | Jira OAuth cloudid for `api.atlassian.net/ex/jira/{cloudid}`; null for Basic-auth/Linear |
| `external_project_key` | text | Jira project key / Linear team key (e.g. `ENG`) |
| `external_project_id` | text | Jira project id / Linear team id (stable) |
| `auth_type` | enum `pm_auth_type` (`oauth`\|`api_token`) | |
| `credential_ref` | text | F37 vault secret id (token bundle / API key); **never** the secret |
| `account_label` | text NULL | connected account / email / org for display |
| `granted_scopes` | jsonb default `[]` | OAuth scopes granted (display + capability checks) |
| `sync_direction` | enum `pm_sync_direction` (`bidirectional`\|`inbound_only`\|`outbound_only`) default `bidirectional` | |
| `conflict_policy` | enum `pm_conflict_policy` (`forge_wins`\|`external_wins`\|`newest_wins`\|`manual`) default `newest_wins` | |
| `status_map` | jsonb default `{}` | bidirectional status mapping (see §4); empty → category-default resolver |
| `priority_map` | jsonb default `{}` | bidirectional priority mapping; empty → default resolver |
| `field_map` | jsonb default `{}` | extra field mappings (custom fields → Forge fields) |
| `webhook_secret_ref` | text NULL | vault id of per-connection webhook signing secret / Jira URL secret |
| `external_webhook_id` | text NULL | provider-side webhook id (for unregister on disconnect) |
| `outbound_cursor_at` | timestamptz NULL | high-water mark over `activity_events.created_at` for the OUT scan |
| `outbound_cursor_event_id` | uuid NULL | tie-breaker for equal `created_at` |
| `status` | enum `pm_connection_status` (`pending`\|`connected`\|`error`\|`disabled`) default `pending` | |
| `last_health_at` / `last_health_error` | timestamptz / text(redacted) NULL | |
| `last_full_sync_at` | timestamptz NULL | last backfill completion |
| `config` | jsonb default `{}` | extra transport headers, webhook event filter, etc. |
| `created_at` / `updated_at` | timestamptz | |

Constraints/indexes: `UNIQUE(project_id, provider)` (one connection per provider per Forge project), `UNIQUE(workspace_id, provider, external_project_key)`, index `(workspace_id, status)`, `CHECK` enums. `credential_ref`/`webhook_secret_ref` reference the vault only.

**`pm_task_links`** — the durable Forge-task ↔ external-issue mapping + sync watermarks (the heart of loop suppression + conflict detection).

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `workspace_id` | uuid FK CASCADE | |
| `connection_id` | uuid FK→`pm_connections.id` CASCADE | |
| `forge_task_id` | uuid FK→`tasks.id` CASCADE | |
| `provider` | enum `pm_provider` | denormalized |
| `external_id` | text | stable external id (Jira issue id / Linear issue uuid) |
| `external_key` | text | human key (`ENG-123` / `ENG-45`) |
| `external_url` | text | deep link |
| `last_synced_at` | timestamptz NULL | last successful sync (either direction) |
| `forge_version_at_sync` | int NULL | `tasks.version` observed at last successful sync |
| `external_updated_at_at_sync` | timestamptz NULL | external `updatedAt` observed at last successful sync |
| `last_outbound_hash` | text NULL | canonical hash of the Forge-side payload last reconciled (OUT echo suppression) |
| `last_inbound_hash` | text NULL | canonical hash of the external-side payload last reconciled (IN echo suppression) |
| `sync_state` | enum `pm_sync_state` (`synced`\|`pending_out`\|`pending_in`\|`conflict`\|`error`) default `synced` | |
| `conflict_detail` | jsonb NULL | both candidate payloads + diff when `conflict` |
| `last_error` | text NULL | redacted |
| `created_at` / `updated_at` | timestamptz | |

Constraints/indexes: `UNIQUE(connection_id, external_id)`, `UNIQUE(connection_id, forge_task_id)`, index `(workspace_id, sync_state)`, index `(connection_id, sync_state)`.

**`pm_webhook_deliveries`** — inbound idempotency + audit dedup (mirrors F03's `github_webhook_deliveries`).

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `provider` | enum `pm_provider` | |
| `connection_id` | uuid FK→`pm_connections.id` SET NULL | nullable so audit survives deletion |
| `delivery_id` | text **UNIQUE** | provider event id; synthesized as `sha256(body)+received_minute` when the provider supplies none (Jira) |
| `event_type` | text | normalized (`issue.created`\|`issue.updated`\|`issue.deleted`) |
| `external_id` | text NULL | issue id parsed from payload |
| `payload_hash` | text | sha256 hex of raw body |
| `signature_valid` | boolean | result of HMAC/secret verification |
| `received_at` | timestamptz | |
| `processed_at` | timestamptz NULL | |
| `status` | enum `pm_delivery_status` (`received`\|`processed`\|`skipped`\|`echo_suppressed`\|`error`) | |
| `error` | text NULL | redacted |

Index: `(connection_id, received_at DESC)`, unique `delivery_id`.

Per-operation **sync audit** is written to the **platform immutable audit log (F39)** (operation, connection slug, direction, forge_task_id/external_id, payload hash, result, latency, redacted) rather than a dedicated table — consistent with F03. No raw external payloads are persisted beyond `payload_hash` + the normalized, redacted subset.

### 3.2 Backend (FastAPI routes + services/packages)

**Router:** `apps/api/forge_api/routers/pm.py`, mounted at `/api/v1/integrations/pm`. All routes auth-required (RBAC below) **except** the two webhook intake routes, which are signature/secret-verified instead. Thin controllers delegate to `apps/api/forge_api/services/pm_service.py` (persistence + vault + provider-webhook provisioning + OAuth orchestration + enqueueing sync tasks).

| Method & path | Purpose | Auth / RBAC |
|---|---|---|
| `POST /connections` | Create a connection (validates `PMConnectionConfig`; resolves external project; pre-fills maps). | bearer, `admin` |
| `GET /connections` | List workspace connections (status, provider, project, counts). | bearer, `member` |
| `GET /connections/{id}` | Detail + last health + link counts by `sync_state`. | bearer, `member` |
| `PATCH /connections/{id}` | Update name/maps/direction/conflict_policy/enable-disable. | bearer, `admin` |
| `DELETE /connections/{id}` | Unregister provider webhook, set `disabled`; retain links + audit. | bearer, `admin` |
| `POST /connections/{id}/test` | Health probe (`get_connection_health`) → persist status/scopes/account. | bearer, `admin` |
| `POST /connections/{id}/oauth/start` | Begin OAuth (returns authorization URL; PKCE+state+resource in Redis). | bearer, `admin` |
| `GET /connections/{id}/oauth/callback` | Exchange code, store token bundle in vault, set `connected`, register webhook. | bearer, `admin` |
| `POST /connections/{id}/backfill?direction=in\|out\|both` | Enqueue `pm.backfill`. | bearer, `admin` |
| `GET /connections/{id}/links?state=` | Paginated `pm_task_links` (filter by `sync_state`). | bearer, `member` |
| `POST /links/{link_id}/resolve` | Manual conflict resolution body `{winner:"forge"\|"external"}`; re-syncs. | bearer, `admin`/`member` |
| `POST /webhooks/jira/{connection_id}` | Jira intake: verify per-connection URL secret (path-/header-bound, constant-time), dedupe, persist `pm_webhook_deliveries`, enqueue `pm.process_webhook`, return `202`. | secret (no bearer) |
| `POST /webhooks/linear/{connection_id}` | Linear intake: verify `Linear-Signature` HMAC-SHA256 over **raw body** + `webhookTimestamp` freshness, dedupe, enqueue, `202`. | signature (no bearer) |

**Package:** `packages/integration-sdk/forge_integrations/pm/`

| File | Responsibility |
|---|---|
| `base.py` | `PMAdapter` Protocol (async, implemented form) + `Direction`/result enums; abstract field-mapping helpers. |
| `transport.py` | `PMTransport` Protocol; `HttpxJiraTransport` (REST, httpx async); `HttpxLinearTransport` (GraphQL, httpx async); `FixturePMTransport` (replays recorded responses keyed by `(provider, method, path_or_op)`). |
| `registry.py` | `build_adapter(provider, ctx) -> PMAdapter` factory (provider → adapter). The single extension point. |
| `sync_engine.py` | `PMSyncEngine` — provider-agnostic orchestration: link upsert, echo suppression, conflict detection/resolution, board write via `board-core`, external write via adapter. |
| `hashing.py` | `forge_content_hash(ForgeTask, map)` / `external_content_hash(ExternalTask, map)` — canonical, field-scoped sha256 for echo suppression + conflict detection. |
| `jira/auth.py` | `JiraAuth` — OAuth 3LO (token + refresh, cloudid resolve) and Basic (email+API-token); vault-backed. |
| `jira/client.py` | `JiraClient` — REST v3: get/create/update issue, list+do transitions, list statuses/priorities, webhook register/unregister, myself. |
| `jira/mapping.py` | Jira status/priority defaults, category mapping, markdown↔ADF (best-effort). |
| `jira/webhooks.py` | `verify_jira(secret, body, request)`, `parse_jira(body) -> WebhookEvent`. |
| `jira/adapter.py` | `JiraAdapter(PMAdapter)`. |
| `linear/auth.py` | `LinearAuth` — OAuth + personal API key; vault-backed. |
| `linear/client.py` | `LinearClient` — GraphQL: issue query, issueCreate/issueUpdate, workflowStates/labels, webhookCreate/Delete, viewer. |
| `linear/mapping.py` | Linear workflow-state-type ↔ Forge category (near 1:1), priority int↔token. |
| `linear/webhooks.py` | `verify_linear(secret, body, signature, timestamp)`, `parse_linear(body) -> WebhookEvent`. |
| `linear/adapter.py` | `LinearAdapter(PMAdapter)`. |
| `errors.py` | `PMAuthError`, `WebhookVerificationError`, `ExternalNotFound`, `MappingError`, `SyncConflict`, `RateLimitError`, `ProviderError`. |

`forge_integrations/github/*` and `forge_integrations/slack/*` are not touched. The **canonical DTOs + the `PMAdapter` Protocol live in `packages/contracts/forge_contracts/pm.py`** (§4), so `apps/api`, `apps/worker`, and the adapters share one frozen surface.

**Board writes** go exclusively through `board-core` services (F01): `board_core.services.tasks.create_task/update_task/move` invoked with a **service principal** (`actor_kind=system`, role `agent-runner`) so writes respect status-transition policy, optimistic `version`, and emit `activity_events`. Every PM-originated board write tags the activity event payload with `{"source":"pm_sync","connection_id":...,"link_id":...,"direction":"in"}` — the primary echo-suppression signal for the outbound scanner.

### 3.3 Worker / agent runtime (Celery tasks, LangGraph, if any)

Celery tasks in `apps/worker/forge_worker/tasks/pm.py` (queue `integrations`; Celery Beat for the scanners). No LangGraph (this slice is integration sync, not agent execution).

- `pm.process_webhook(delivery_id: str)` — load the `pm_webhook_deliveries` row, idempotency-check (`processed_at` set → return), parse → `WebhookEvent`, **re-fetch** the authoritative issue via the adapter (`fetch_external(external_id)`), and call `PMSyncEngine.sync_in(connection, external_task, event)`. Sets `status` (`processed`/`echo_suppressed`/`skipped`/`error`) + `processed_at`. `issue.deleted` → archive/unlink per `config.on_external_delete` (default `unlink`, retains the Forge task).
- `pm.sync_task_out(connection_id: str, forge_task_id: str)` — load the Forge task + link (or none), call `PMSyncEngine.sync_out(connection, forge_task)`.
- `pm.sync_task_in(connection_id: str, external_id: str)` — explicit IN reconcile (used by backfill + retries).
- `pm.scan_outbound()` — **beat, every `PM_OUTBOUND_SCAN_SECONDS` (default 20s)**. For each `connected`, outbound-capable connection: select `activity_events` for the connection's `project_id` with `(created_at, id) > (outbound_cursor_at, outbound_cursor_event_id)`, event types in {`created`,`status_changed`,`assigned`,`priority_changed`,`field_changed`,`label_added`,`label_removed`,`archived`}, **excluding** events whose `payload.source == "pm_sync"` (echo suppression #1). Dedupe to affected `task_id`s, enqueue `pm.sync_task_out` per task, advance the cursor. Durable + replayable (re-processing is safe because `sync_out` is idempotent via the content hash).
- `pm.health_probe_all()` — **beat, every 5 min**. Per connection, `get_connection_health()`; update `status`/`last_health_at`/`last_health_error`. Isolated failures don't fail the batch.
- `pm.backfill(connection_id: str, direction: Literal["in","out","both"])` — page through the external project (IN) and/or unlinked Forge tasks (OUT); reconcile/create + link each. Bounded page size, resumable (re-run is idempotent on `(connection_id, external_id)`).

**Real-time optimization (optional):** a worker may also subscribe to Redis `board:{project_id}` (F01's SSE backbone) to enqueue `sync_task_out` with sub-second latency; the beat scan remains the durable source of truth. The OUT direction is correct even if the subscription is absent.

**Outbound rate limiting:** the adapter clients honor provider rate-limit headers (`Retry-After`, Jira `X-RateLimit-*`, Linear `RateLimit-*`) and back off; `pm.sync_task_out` retries with exponential backoff on `RateLimitError`.

### 3.4 Frontend / UI (Next.js routes/components, if any)

Settings surface under `apps/web` (TanStack Query + Table; shadcn/ui), plus a conflict inbox.

- Route `apps/web/app/(settings)/integrations/pm/page.tsx` — connections list (provider badge, Forge project, external project, status, link counts, conflict count). Admin mutate actions; members read-only.
- Route `apps/web/app/(settings)/integrations/pm/[connectionId]/page.tsx` — connection detail: auth status, status/priority/field map editors, sync direction, conflict policy, "Test", "Import & link", "Disconnect".
- Route `apps/web/app/(settings)/integrations/pm/oauth/callback/page.tsx` — thin OAuth landing that posts `code`/`state` to the callback route then redirects back.
- Components (`apps/web/components/integrations/pm/`): `PMConnectButton.tsx`, `PMConnectionsTable.tsx`, `PMConnectionForm.tsx`, `StatusMapEditor.tsx`, `PriorityMapEditor.tsx`, `SyncDirectionPicker.tsx`, `ConflictPolicyPicker.tsx`, `ConflictInbox.tsx` (side-by-side Forge vs external, "Keep Forge"/"Keep external"), `LinkStatusBadge.tsx`.
- `apps/web/lib/api/pm.ts` — typed client matching §4 contracts.

PR/CI activity and the per-task PM-sync events render in the **board task timeline** (owned by F01); this slice only adds the connection-management + conflict-inbox screens.

### 3.5 Infra / deploy (compose, helm, caddy, if any)

- `deploy/docker-compose.yml` / `deploy/.env.example` / `.env.production.example`: add `JIRA_OAUTH_CLIENT_ID`, `JIRA_OAUTH_CLIENT_SECRET`, `LINEAR_OAUTH_CLIENT_ID`, `LINEAR_OAUTH_CLIENT_SECRET`, `PM_WEBHOOK_PUBLIC_URL` (base for registered webhook URLs; usually `${FORGE_PUBLIC_URL}`), `PM_OUTBOUND_SCAN_SECONDS=20`, `PM_HEALTH_PROBE_SECONDS=300`, `PM_HTTP_TIMEOUT_SECONDS=30`, `PM_LINEAR_WEBHOOK_TOLERANCE_SECONDS=60`, `PM_BACKFILL_PAGE_SIZE=50`.
- `deploy/caddy/Caddyfile`: `POST /api/v1/integrations/pm/webhooks/*` must be reverse-proxied to `api` **without** body buffering/rewrite (Linear HMAC is computed over exact raw bytes). Caddy passes bodies untouched by default; add a comment asserting no `request_body` transform on this path (same requirement F03 documents for the GitHub webhook).
- `worker` must run Celery Beat so `pm.scan_outbound` / `pm.health_probe_all` fire (already required by F01's `board.scan_sla_breaches`).
- No new compose service; reuses `api`, `worker`, `db`, `redis`, `caddy`. Helm (V2 Kubernetes) inherits the same env keys.

---

## 4. Public interfaces / contracts (exact signatures, Pydantic models, Protocols, YAML schemas)

**Spec verbatim (source of truth — Native Project Board → External PM Adapter Contract):**

```python
class PMAdapter(Protocol):
    def sync_in(self, external_task: ExternalTask) -> ForgeTask: ...
    def sync_out(self, forge_task: ForgeTask) -> ExternalTask: ...
    def subscribe(self, webhook_event: WebhookEvent) -> None: ...
    def map_status(self, external: str, direction: Direction) -> str: ...
    def map_priority(self, external: str, direction: Direction) -> str: ...
    def map_fields(self, external: dict, direction: Direction) -> dict: ...
    def get_connection_health(self) -> HealthResult: ...
```

**Implemented (async) form** — Forge's runtime is async I/O, so the concrete Protocol used in code is `async`. The mapping methods stay sync (pure functions). `subscribe` honors the spec exactly (ingests a normalized `WebhookEvent` and routes it into `sync_in`).

`packages/contracts/forge_contracts/pm.py`:

```python
from __future__ import annotations
from datetime import datetime
from enum import StrEnum
from typing import Literal, Protocol, runtime_checkable
from uuid import UUID
from pydantic import BaseModel, Field

# ---------- Enums ----------

class PMProvider(StrEnum):
    jira = "jira"; linear = "linear"

class Direction(StrEnum):
    IN = "in"     # external -> forge
    OUT = "out"   # forge -> external

class SyncDirection(StrEnum):
    bidirectional = "bidirectional"
    inbound_only = "inbound_only"
    outbound_only = "outbound_only"

class ConflictPolicy(StrEnum):
    forge_wins = "forge_wins"; external_wins = "external_wins"
    newest_wins = "newest_wins"; manual = "manual"

# Normalized status categories (align with board_core.StatusCategory).
class StatusCategory(StrEnum):
    backlog = "backlog"; unstarted = "unstarted"; started = "started"
    completed = "completed"; canceled = "canceled"

class ForgePriority(StrEnum):
    none = "none"; low = "low"; medium = "medium"; high = "high"; urgent = "urgent"

# ---------- DTOs ----------

class ExternalTask(BaseModel):
    provider: PMProvider
    external_id: str                       # stable id (Jira issue id / Linear issue uuid)
    external_key: str                      # human key (ENG-123 / ENG-45)
    url: str
    title: str
    description_md: str | None = None      # normalized to markdown (ADF/GraphQL decoded)
    status_name: str                       # raw external status / workflow-state name
    status_category: StatusCategory | None = None
    priority_token: str | None = None      # raw external priority token
    assignee_external_id: str | None = None
    assignee_email: str | None = None
    labels: list[str] = Field(default_factory=list)
    external_updated_at: datetime
    raw: dict = Field(default_factory=dict)  # redacted provider extras for field_map

class ForgeTask(BaseModel):                  # stable serialization of a board task (F01)
    id: UUID
    key: str
    project_id: UUID
    title: str
    description_md: str | None = None
    status_id: UUID
    status_category: StatusCategory
    priority: ForgePriority
    assignee_id: UUID | None = None
    assignee_email: str | None = None
    label_names: list[str] = Field(default_factory=list)
    version: int
    updated_at: datetime

class WebhookEvent(BaseModel):
    provider: PMProvider
    delivery_id: str
    event_type: Literal["issue.created", "issue.updated", "issue.deleted"]
    external_id: str | None = None
    external_key: str | None = None
    signature_valid: bool
    received_at: datetime
    payload: dict = Field(default_factory=dict)   # parsed, secret-free subset

class HealthResult(BaseModel):
    status: Literal["connected", "error"]
    provider: PMProvider
    latency_ms: float
    account: str | None = None
    granted_scopes: list[str] = Field(default_factory=list)
    error: str | None = None              # redacted

class SyncOutcome(BaseModel):
    direction: Direction
    forge_task_id: UUID | None = None
    external_id: str | None = None
    action: Literal["created", "updated", "no_change", "skipped_echo",
                    "conflict", "error"]
    winner: Literal["forge", "external"] | None = None   # set when a conflict was auto-resolved
    detail: dict = Field(default_factory=dict)

# ---------- Adapter context + Protocol ----------

class AdapterContext(BaseModel):
    connection_id: UUID
    workspace_id: UUID
    provider: PMProvider
    external_project_key: str
    external_project_id: str
    status_map: dict = Field(default_factory=dict)
    priority_map: dict = Field(default_factory=dict)
    field_map: dict = Field(default_factory=dict)
    config: dict = Field(default_factory=dict)

@runtime_checkable
class PMAdapter(Protocol):
    provider: PMProvider

    # --- mapping (pure; honor spec signatures) ---
    def map_status(self, value: str, direction: Direction) -> str: ...
    def map_priority(self, value: str, direction: Direction) -> str: ...
    def map_fields(self, fields: dict, direction: Direction) -> dict: ...

    # --- external I/O ---
    async def fetch_external(self, external_id: str) -> ExternalTask: ...
    async def create_external(self, forge_task: ForgeTask) -> ExternalTask: ...
    async def update_external(self, external_id: str, forge_task: ForgeTask) -> ExternalTask: ...
    async def list_external(self, *, cursor: str | None = None,
                            limit: int = 50) -> tuple[list[ExternalTask], str | None]: ...

    # --- webhook + health ---
    def parse_webhook(self, body: bytes, headers: dict[str, str]) -> WebhookEvent: ...
    def verify_webhook(self, body: bytes, headers: dict[str, str], secret: str) -> bool: ...
    async def register_webhook(self, callback_url: str, secret: str) -> str: ...   # -> external_webhook_id
    async def unregister_webhook(self, external_webhook_id: str) -> None: ...
    async def get_connection_health(self) -> HealthResult: ...
```

> The spec's `sync_in`/`sync_out`/`subscribe` are realized by `PMSyncEngine` (below) calling the adapter's `fetch/create/update_external` + mapping methods. `subscribe(webhook_event)` is the route `pm.process_webhook` follows: parse → fetch → `sync_in`. Keeping pure mapping on the adapter and the stateful sync (links, hashes, conflicts) in one engine lets every future adapter reuse the loop-suppression + conflict logic unchanged — the OSS extension-point promise.

**Sync engine** (`forge_integrations/pm/sync_engine.py`):

```python
class PMSyncEngine:
    def __init__(self, *, adapter: PMAdapter, board: "BoardTaskService",
                 links: "LinkRepository", audit: "AuditSink",
                 conflict_policy: ConflictPolicy, sync_direction: SyncDirection,
                 status_map: dict, priority_map: dict) -> None: ...

    async def sync_out(self, forge_task: ForgeTask) -> SyncOutcome:
        """Forge -> external. No link -> create_external + link. Linked ->
        if forge_content_hash == link.last_outbound_hash: no_change.
        Else detect conflict (external changed since external_updated_at_at_sync);
        resolve per policy; on apply update_external + refresh link watermarks/hashes."""

    async def sync_in(self, external_task: ExternalTask,
                      event: WebhookEvent | None = None) -> SyncOutcome:
        """External -> Forge. Mirror of sync_out. Writes the Forge task via
        BoardTaskService tagged source='pm_sync' (echo suppression #1).
        external_content_hash == link.last_inbound_hash -> skipped_echo."""

    async def resolve_conflict(self, link_id: UUID,
                               winner: Literal["forge", "external"]) -> SyncOutcome: ...
```

**Provider transport** (`forge_integrations/pm/transport.py`) — offline-testable, mirrors F03:

```python
class HttpResponse(BaseModel):
    status_code: int
    json_body: dict | list | None = None
    headers: dict[str, str] = Field(default_factory=dict)

@runtime_checkable
class PMTransport(Protocol):
    async def request(self, method: str, url: str, *,
                      headers: dict | None = None, json: dict | None = None,
                      params: dict | None = None) -> HttpResponse: ...

class FixturePMTransport:
    """Replays recorded responses keyed by (method, url-or-graphql-op).
    Records a call log; raises loudly on unexpected calls. No sockets."""
    def __init__(self, records: dict[tuple[str, str], HttpResponse]) -> None: ...
```

**Default mapping tables (frozen; overridable per connection via `status_map`/`priority_map`).**

Status — by category (Linear workflow-state `type` is 1:1 with Forge category; Jira maps via `statusCategory`):

| Forge `StatusCategory` | Linear state `type` | Jira `statusCategory.key` |
|---|---|---|
| `backlog` | `backlog` | `new` (To Do) |
| `unstarted` | `unstarted` | `new` (To Do) |
| `started` | `started` | `indeterminate` (In Progress) |
| `completed` | `completed` | `done` (Done) |
| `canceled` | `canceled` | `done` (Done; resolution=Won't Do where available) |

Within a category, OUT picks the connection's configured concrete status (or the external default for that category); Jira status changes are applied via `POST /issue/{key}/transitions` (look up the transition whose `to.statusCategory` matches), never a direct field write.

Priority:

| Forge `ForgePriority` | Linear int | Jira priority name |
|---|---|---|
| `none` | 0 | (no priority / Medium fallback) |
| `low` | 4 | Low |
| `medium` | 3 | Medium |
| `high` | 2 | High |
| `urgent` | 1 | Highest |

**Connection config (request body for `POST /connections`; also the OSS connector-template YAML):**

```python
class PMConnectionConfig(BaseModel):
    provider: PMProvider
    name: str
    project_id: UUID                         # Forge project to sync
    external_base_url: str | None = None     # Jira site; null for Linear
    external_project_key: str                # Jira project key / Linear team key
    auth_type: Literal["oauth", "api_token"] = "oauth"
    api_token: str | None = None             # api_token mode only; stored to vault, never returned
    api_token_email: str | None = None       # Jira Basic (email + token)
    sync_direction: SyncDirection = SyncDirection.bidirectional
    conflict_policy: ConflictPolicy = ConflictPolicy.newest_wins
    status_map: dict = Field(default_factory=dict)
    priority_map: dict = Field(default_factory=dict)
    field_map: dict = Field(default_factory=dict)
    on_external_delete: Literal["unlink", "archive"] = "unlink"
```

```yaml
# examples/integrations/pm/jira-eng.yaml  (connector template w/ security notes)
pm_connection:
  provider: jira
  name: Engineering Jira
  project_id: <forge-project-uuid>
  external_base_url: https://acme.atlassian.net
  external_project_key: ENG
  auth_type: oauth            # oauth (3LO) | api_token (email + API token, Basic)
  sync_direction: bidirectional
  conflict_policy: newest_wins   # forge_wins | external_wins | newest_wins | manual
  status_map: {}              # empty -> category-based default resolver
  priority_map: {}
  field_map: {}
  on_external_delete: unlink
```

**Error contract (routers):** `409` `{"error":"sync_conflict","link_id":...}` (manual conflict on a non-resolve write); `401` on webhook signature failure; `404` for cross-workspace ids (no existence leak); `422` for invalid config / `allow`-prohibited fields; `502` `{"error":"provider_error","provider":...}` on upstream failure (with redacted detail).

---

## 5. Dependencies — features/slices that must exist first

**Hard (must exist before this slice builds):**
- `v1/F01-project-board` — `projects`/`tasks`/`task_statuses`/`activity_events` tables; `board_core` task service (`create_task`/`update_task`/`move`) with version concurrency + status-transition policy; the append-only `activity_events` change-feed F18 consumes as its outbound outbox; the stable `ForgeTask` serialization the spec contract names; the `/internal/activity-events` + SSE backbone for surfacing `pm_sync` events.
- `v1/F03-github-app` — establishes the `packages/integration-sdk` (`forge_integrations`) package, the `packages/contracts/forge_contracts/integration.py` module pattern, the `PMTransport`/`FixturePMTransport` design (cloned from `GitHubTransport`/`FixtureGitHubTransport`), and the webhook-delivery idempotency-table pattern (`github_webhook_deliveries` → `pm_webhook_deliveries`).
- `cross-cutting/F37-auth-secrets-byok` — encrypted secrets vault (stores Jira/Linear OAuth token bundles, API tokens, per-connection webhook secrets) + RBAC roles (`admin`/`member`/`viewer`/`agent-runner`). The PM service principal used for board writes is an `agent-runner`/system principal. (Referred to as "F37" in the body.)
- `cross-cutting/F39-audit-log` — immutable audit-log writer used for every outbound provider call and every accepted inbound webhook (operation, connection slug, direction, payload hash, status, latency, redacted). (Referred to as "F39" in the body.)
- `v1/F00-foundation-substrate` (a.k.a. the monorepo/`uv`-workspace + FastAPI/Celery/compose foundation; some slices label this `cross-cutting/C01`) — `apps/api` router registration, `apps/worker` Celery app + Beat, Redis, `deploy/docker-compose.yml` + `.env`.

**Soft / consumers (need not exist first):**
- `v1/F07-feature-workflow-fsm` — may drive external status from workflow state transitions; F18 syncs whatever the board task reflects, so no hard ordering.
- `v1/F16-slack-notifications` — may notify on `sync_state=conflict`; F18 emits the event regardless.

This slice ships its own `FixturePMTransport` + recorded fixtures so it is fully testable in isolation, with **zero** dependency on a running Jira/Linear.

---

## 6. Acceptance criteria (numbered, testable)

1. **Migration up/down.** `<rev>_pm_adapters` creates `pm_connections`, `pm_task_links`, `pm_webhook_deliveries` with all columns, enums, the unique constraints (`UNIQUE(project_id, provider)`, `UNIQUE(connection_id, external_id)`, `UNIQUE(connection_id, forge_task_id)`, `UNIQUE(pm_webhook_deliveries.delivery_id)`) and indexes; `alembic downgrade` drops them cleanly.
2. **Connection create + map prefill.** `POST /connections` for Jira/Linear validates `PMConnectionConfig`, stores any `api_token` to the vault (never echoed in the response), resolves the external project id, and persists category-default `status_map`/`priority_map` when none supplied. Re-create on the same `(project_id, provider)` → 409/idempotent (no duplicate).
3. **Health.** `get_connection_health()` returns `connected` with `account` + `granted_scopes` + `latency_ms` on a healthy fixture, and `error` with a redacted message on a failing one; `POST /connections/{id}/test` persists `status`/`last_health_at`.
4. **Status mapping (both directions).** For each provider, `map_status(value, Direction.OUT)` and `map_status(value, Direction.IN)` produce the exact values in the §4 default table for every category; a per-connection `status_map` override takes precedence; an unmappable value raises `MappingError` (never silently drops).
5. **Priority mapping (both directions).** `map_priority` matches the §4 table for every Forge priority and every provider token, both directions, with override precedence.
6. **sync_out create.** An unlinked Forge task synced OUT issues the provider create (Jira `POST /issue`; Linear `issueCreate`), creates a `pm_task_links` row with `external_id`/`external_key`/`external_url`, sets `forge_version_at_sync`/`external_updated_at_at_sync`/`last_outbound_hash`/`sync_state=synced`, and returns `SyncOutcome(action="created")`.
7. **sync_out update + Jira transition.** A linked task whose status category changed syncs OUT via the correct mechanism: Jira via `POST /issue/{key}/transitions` (the transition whose target category matches), Linear via `issueUpdate(stateId)`. A title/description/priority change uses field update. Returns `action="updated"`.
8. **sync_out no-op.** Re-running `sync_out` with no Forge change (forge content hash == `last_outbound_hash`) makes **zero** external write calls and returns `action="no_change"`.
9. **sync_in create.** An `issue.created` webhook → re-fetch → `sync_in` creates a linked Forge task via `board-core` (status/priority/assignee mapped), tags the board activity event `source="pm_sync"`, sets `last_inbound_hash`, returns `action="created"`.
10. **sync_in update.** An `issue.updated` webhook applies a Forge task patch through the board service (respecting status-transition policy + version); returns `action="updated"`.
11. **Echo suppression #1 (origin tag).** A board change produced by `sync_in` (activity event `source="pm_sync"`) is **excluded** by `pm.scan_outbound` and never enqueues an OUT sync (assert no `sync_task_out` enqueued).
12. **Echo suppression #2 (content hash).** When a human edit immediately follows a `sync_in` write, the OUT path computes `forge_content_hash == last_outbound_hash` and returns `skipped_echo`/`no_change` with no external write; symmetrically a re-delivered webhook whose `external_content_hash == last_inbound_hash` returns `skipped_echo` with no board write.
13. **Conflict — newest_wins.** With both sides changed since last sync, `newest_wins` applies the side with the later timestamp, writes only that side, sets `winner`, and refreshes both watermarks; `forge_wins`/`external_wins` apply the configured side deterministically regardless of timestamps.
14. **Conflict — manual.** With `conflict_policy=manual` and both sides changed, the engine performs **no** write, sets `sync_state=conflict` + `conflict_detail` (both candidate payloads), returns `action="conflict"`; `POST /links/{id}/resolve {winner}` then applies the chosen side and returns to `synced`.
15. **Webhook signature — Linear.** `POST /webhooks/linear/{id}` returns `401` when `Linear-Signature` is missing/malformed/over a tampered body or when `webhookTimestamp` is older than `PM_LINEAR_WEBHOOK_TOLERANCE_SECONDS`; returns `202` on a valid HMAC-SHA256 over the exact raw body (constant-time compare).
16. **Webhook secret — Jira.** `POST /webhooks/jira/{id}` returns `401` on a missing/incorrect per-connection secret and `202` on the correct one; the handler treats the payload as a hint and re-fetches authoritative state before applying.
17. **Webhook idempotency.** Two POSTs with the same provider event id (or synthesized delivery id) both return `202`, create exactly one `pm_webhook_deliveries` row, and enqueue `pm.process_webhook` exactly once.
18. **Backfill.** `pm.backfill(direction="in")` pages the external project and creates+links one Forge task per issue (idempotent on re-run: no duplicate links/tasks); `direction="out"` pushes unlinked Forge tasks as new external issues; `last_full_sync_at` is set.
19. **Sync direction enforcement.** An `inbound_only` connection ignores outbound activity (no external writes); an `outbound_only` connection ignores inbound webhooks (recorded `skipped`).
20. **Tenant isolation + RBAC.** Cross-workspace `connection_id` → 404; `viewer` gets 403 on all mutating routes and 200 on reads; `member` may resolve conflicts and read; only `admin` may create/patch/delete/oauth/backfill; webhook routes require no bearer but a valid signature/secret.
21. **Secret redaction.** OAuth tokens, API tokens, the webhook secret, and provider auth headers never appear in any API response, `pm_task_links`/`pm_connections`/`pm_webhook_deliveries` row, audit entry, or log line (asserted across a full connect → backfill → sync → webhook flow).
22. **Audit completeness.** Every outbound provider call and every accepted inbound webhook writes exactly one immutable audit entry (operation, connection slug, direction, payload hash, result, latency).
23. **Offline guarantee.** The full suite passes with `HttpxJiraTransport`/`HttpxLinearTransport` unimported at runtime; all provider I/O goes through `FixturePMTransport`; CI asserts no sockets are opened during the integration-sdk PM tests (`pytest-socket`).
24. **Disconnect.** `DELETE /connections/{id}` calls `unregister_webhook` (best-effort), sets `status=disabled`; subsequent webhooks for it are recorded `skipped` and the outbound scan ignores it; links + audit are retained.

---

## 7. Test plan (TDD) — concrete test cases (unit + integration), key fixtures

Write tests first; backend-tdd discipline (≥80% coverage per the spec's own profile). Roots: `packages/integration-sdk/tests/pm/`, `apps/api/tests/pm/`, `apps/worker/tests/pm/`.

**Unit — mapping (`jira/mapping.py`, `linear/mapping.py`)**
- `test_status_map_table_jira_both_directions` / `test_status_map_table_linear_both_directions` — parametrized over the full §4 table (AC4).
- `test_status_map_override_precedence` — connection `status_map` beats default (AC4).
- `test_status_map_unmappable_raises` — `MappingError` (AC4).
- `test_priority_map_table_*` both providers, both directions (AC5).
- `test_jira_adf_roundtrip_basic` / `test_linear_markdown_passthrough` — description normalization best-effort.

**Unit — hashing (`hashing.py`)**
- `test_forge_content_hash_stable_and_field_scoped` — only mapped fields affect the hash; `version`/`updated_at` do not.
- `test_external_content_hash_stable` — same for `ExternalTask`.
- `test_hash_excludes_secrets` — `raw`/`config` secrets never feed the hash output (and hash inputs are redaction-safe).

**Unit — sync engine (`sync_engine.py`, fakes for board/links/adapter)**
- `test_sync_out_create_links_and_sets_watermarks` (AC6).
- `test_sync_out_update_uses_transition_for_status` (AC7) — assert Jira transition vs Linear stateId path.
- `test_sync_out_no_change_makes_no_external_call` (AC8).
- `test_sync_in_create_via_board_service_tagged_pm_sync` (AC9, AC11).
- `test_sync_in_update_respects_version` (AC10).
- `test_echo_suppression_content_hash_both_directions` (AC12).
- `test_conflict_newest_wins` / `test_conflict_forge_wins` / `test_conflict_external_wins` (AC13).
- `test_conflict_manual_no_write_sets_conflict_state` + `test_resolve_conflict_applies_winner` (AC14).
- `test_sync_direction_inbound_only_skips_out` / `test_outbound_only_skips_in` (AC19).

**Unit — webhooks (`jira/webhooks.py`, `linear/webhooks.py`)**
- `test_verify_linear_valid_invalid_missing_tampered` (AC15) — tampered body flips one byte; constant-time via `hmac.compare_digest`.
- `test_verify_linear_rejects_stale_timestamp` (AC15).
- `test_verify_jira_secret_match_mismatch` (AC16).
- `test_parse_jira_*` / `test_parse_linear_*` — one per fixture → `WebhookEvent` (event_type, external_id/key) (AC9, AC10, AC17).

**Unit — clients (`jira/client.py`, `linear/client.py`) with `FixturePMTransport`**
- `test_jira_get_create_update_issue` / `test_jira_list_and_do_transition` / `test_jira_register_unregister_webhook` / `test_jira_health_myself`.
- `test_linear_issue_query_create_update` / `test_linear_workflow_states` / `test_linear_webhook_create_delete` / `test_linear_health_viewer`.
- `test_clients_redact_auth_in_serialized_output` (AC21).
- `test_clients_backoff_on_rate_limit` — `Retry-After`/`RateLimit-*` honored.

**Integration — `apps/api` (Postgres testcontainer + httpx AsyncClient, Celery eager)**
- `test_create_connection_stores_token_in_vault_not_response` (AC2, AC21).
- `test_create_connection_prefills_maps` (AC2, AC4).
- `test_test_endpoint_persists_health` (AC3).
- `test_webhook_linear_signed_202_unsigned_401` (AC15).
- `test_webhook_jira_secret_202_401` (AC16).
- `test_webhook_dedup_one_row_one_task` (AC17).
- `test_resolve_conflict_endpoint` (AC14).
- `test_rbac_matrix_and_cross_workspace_404` (AC20).
- `test_disconnect_unregisters_and_skips` (AC24).
- `test_migration_up_down` (AC1).

**Integration — `apps/worker`**
- `test_scan_outbound_excludes_pm_sync_events` (AC11) — seed `activity_events`, assert only non-`pm_sync` enqueue.
- `test_scan_outbound_advances_cursor_and_is_replayable` (AC8, AC11) — re-run enqueues nothing new.
- `test_process_webhook_refetches_then_sync_in` (AC9, AC16) — assert authoritative re-fetch before write.
- `test_backfill_in_creates_links_idempotent` / `test_backfill_out_pushes_unlinked` (AC18).
- `test_health_probe_all_updates_statuses` (AC3).
- `test_audit_one_row_per_op` (AC22).

**Frontend — `apps/web` (Vitest + Testing Library + MSW; Playwright for e2e)**
- `PMConnectionForm.test.tsx` — provider switch, map editors render, api_token field is write-only.
- `ConflictInbox.test.tsx` — side-by-side render; "Keep Forge"/"Keep external" call `resolve` (AC14).
- `pm-connect.spec.ts` (Playwright) — connect → list shows `connected`; no full reload.

**Key fixtures**
- `packages/integration-sdk/tests/pm/fixtures/jira/{get_issue,create_issue,transitions,statuses,priorities,myself,webhook_register}.json` and `webhooks/{issue_created,issue_updated,issue_deleted}.json`.
- `.../fixtures/linear/{issue_query,issue_create,issue_update,workflow_states,viewer,webhook_create}.json` (GraphQL responses) and `webhooks/{Issue_create,Issue_update,Issue_remove}.json`.
- `FixturePMTransport(records, call_log)` — deterministic; unexpected calls fail loudly; no sockets.
- `FakeBoardTaskService` — records create/update/move calls + activity-event `source` tags (no DB) for sync-engine unit tests; the real `board_core` service is used in API/worker integration tests.
- `FakeLinkRepository` / real `pm_task_links` repo for integration.
- `FakeVault` — deterministic token bundle; asserts secrets never logged.
- `InMemoryAuditSink` — captures audit entries for AC22.
- `linear_signed(body, secret, ts)` / `jira_secret_url(secret)` helpers.
- `clock` fixture — injectable `now()` for timestamp-tolerance + newest-wins tests.

---

## 8. Security & policy considerations

- **Webhook authenticity.** Linear: HMAC-SHA256 over the **raw** request body (`Linear-Signature`), constant-time compare, plus `webhookTimestamp` freshness window — read `Request.body()` before any JSON parse; Caddy must not rewrite/buffer the path. Jira REST webhooks are not signed by Atlassian, so F18 binds a per-connection random secret into the registered webhook URL/header and verifies it constant-time, **and never trusts the payload** — every webhook triggers an authoritative re-fetch via the authenticated API before any board write. Enforce a max body size and per-connection rate limit on intake.
- **Secret storage & redaction.** OAuth token bundles, refresh tokens, Jira API tokens, and per-connection webhook secrets live only in the F37 encrypted vault, referenced by `credential_ref`/`webhook_secret_ref`; never written to a column, response, log, trace, or audit entry. A redaction filter strips `authorization`, `token`, `refresh_token`, `api_token`, `client_secret`, `webhook_secret`, and high-entropy values from all structured logs/audit. `pm_webhook_deliveries` persists only `payload_hash` + normalized fields, never the raw external payload.
- **Least-privilege scopes / token binding.** Request the minimal provider scopes — Jira: `read:jira-work`, `write:jira-work`, `manage:jira-webhook`, `offline_access`; Linear: `read`, `write`. OAuth uses PKCE and (where supported) audience/`resource`-bound tokens analogous to the spec's RFC 8707 MCP requirement, so a token issued for one connection cannot be replayed against another site/org. Each connection's token is used **only** against its own external project.
- **Write gating & blast radius.** All external mutations (`create_external`/`update_external`/transitions/`register_webhook`) are agent-tool-equivalent calls. They are bounded to the connection's single external project and to issues the connection owns; the adapter refuses to write to projects/teams other than `external_project_id`. Outbound-only/inbound-only directions are enforced engine-side (AC19). `on_external_delete=unlink` (default) means an external delete never destroys Forge data.
- **Tenant isolation + RBAC.** Every query is workspace-scoped (cross-workspace ids → 404, no existence leak). `viewer` read-only; `member` reads + resolves conflicts; `admin` only for connect/disconnect/oauth/backfill/patch; board writes use a constrained `agent-runner`/system principal that cannot delete projects or change RBAC. Webhook routes carry no bearer (verified by signature/secret instead).
- **Audit immutability.** Every outbound call + accepted inbound webhook is appended to the F39 immutable audit log (redacted payload hash, latency, result) — the spec's "every … MCP/tool call … immutable, queryable" requirement applied to PM sync.
- **Loop & storm safety.** Dual echo suppression (origin tag + content hash) prevents infinite Forge↔external ping-pong; the outbound scan is cursor-bounded and idempotent; provider rate-limit headers are honored with backoff to avoid hammering external APIs.
- **No outward calls in build/CI.** All provider I/O runs through `FixturePMTransport`; CI asserts no sockets (`pytest-socket`).

---

## 9. Effort estimate & risk (S/M/L + key risks)

**Effort: L** — two providers (REST + GraphQL, two auth modes each, two webhook verification schemes), a provider-agnostic bidirectional sync engine with conflict resolution + dual echo suppression, three tables + migration, an API router + OAuth + webhook intake, six Celery tasks (incl. two beat scanners), a settings UI + conflict inbox, and a full offline fixture suite. Roughly: contracts+models+migration (S), sync engine + hashing + conflict (M), Jira adapter+client+webhooks+ADF (M), Linear adapter+client+webhooks (M), API+OAuth+webhook intake (M), worker tasks+backfill (M), UI (S), tests/fixtures (M).

Key risks:
- **Echo loops / double-writes** (High → mitigated): naive bidirectional sync ping-pongs forever. Mitigation: dual suppression (activity-event `source="pm_sync"` origin tag as the primary, content-hash comparison as the safety net), idempotent cursor-bounded outbound scan; covered by AC11/AC12.
- **Conflict semantics** (Med): concurrent edits both sides. Mitigation: explicit `ConflictPolicy` with watermark-based detection (`forge_version_at_sync` + `external_updated_at_at_sync`), `manual` path with a human inbox, deterministic auto policies; AC13/AC14.
- **Jira description fidelity (ADF)** (Med): Jira stores rich text as ADF, not markdown. Mitigation: best-effort markdown↔ADF for common nodes (paragraphs, lists, code, links); preserve unconvertible content as a fenced block; never block a sync on formatting; documented as a known limitation (§12).
- **Jira status changes require transitions** (Med): you cannot set `status` directly. Mitigation: look up `/transitions` and pick by target `statusCategory`; if no valid transition exists, record `error` on the link (no silent drop) and surface it; AC7.
- **Unsigned Jira webhooks** (Med): payloads aren't HMAC-signed. Mitigation: per-connection URL secret + authoritative re-fetch (webhook is a hint only); AC16.
- **Provider API drift / fixtures vs live** (Med): fixtures are recorded, not live. Mitigation: capture real payloads into the fixtures dir; flag a post-merge live-verification task (out of overnight scope) — same posture as F03.
- **Rate limits / large backfills** (Low/Med): big projects exhaust quotas. Mitigation: paged, resumable backfill; rate-limit-header backoff; bounded page size.

---

## 10. Key files / paths (exact)

```
packages/contracts/forge_contracts/pm.py                       # DTOs + PMAdapter Protocol + enums

packages/db/forge_db/models/pm_connection.py                   # new
packages/db/forge_db/models/pm_task_link.py                    # new
packages/db/forge_db/models/pm_webhook_delivery.py             # new
packages/db/migrations/versions/<rev>_pm_adapters.py           # new migration

packages/integration-sdk/forge_integrations/pm/__init__.py
packages/integration-sdk/forge_integrations/pm/base.py
packages/integration-sdk/forge_integrations/pm/transport.py
packages/integration-sdk/forge_integrations/pm/registry.py
packages/integration-sdk/forge_integrations/pm/sync_engine.py
packages/integration-sdk/forge_integrations/pm/hashing.py
packages/integration-sdk/forge_integrations/pm/errors.py
packages/integration-sdk/forge_integrations/pm/jira/{__init__,auth,client,mapping,webhooks,adapter}.py
packages/integration-sdk/forge_integrations/pm/linear/{__init__,auth,client,mapping,webhooks,adapter}.py
packages/integration-sdk/tests/pm/test_{mapping,hashing,sync_engine,webhooks,jira_client,linear_client}.py
packages/integration-sdk/tests/pm/fixtures/jira/**, fixtures/linear/**

apps/api/forge_api/routers/pm.py                               # connections + oauth + webhooks + conflict resolve
apps/api/forge_api/services/pm_service.py                      # persistence + vault + webhook provisioning + oauth
apps/api/forge_api/schemas/pm.py                               # request/response models
apps/api/forge_api/settings.py                                 # JIRA_/LINEAR_/PM_ settings
apps/api/tests/pm/test_{connections,webhooks,conflict,rbac,migration}.py

apps/worker/forge_worker/tasks/pm.py                           # process_webhook, sync_task_out/in, scan_outbound, health_probe_all, backfill
apps/worker/tests/pm/test_{scan_outbound,process_webhook,backfill,health}.py

apps/web/app/(settings)/integrations/pm/page.tsx
apps/web/app/(settings)/integrations/pm/[connectionId]/page.tsx
apps/web/app/(settings)/integrations/pm/oauth/callback/page.tsx
apps/web/components/integrations/pm/{PMConnectButton,PMConnectionsTable,PMConnectionForm,StatusMapEditor,PriorityMapEditor,SyncDirectionPicker,ConflictPolicyPicker,ConflictInbox,LinkStatusBadge}.tsx
apps/web/lib/api/pm.ts
apps/web/tests/pm/*.{test.tsx,spec.ts}

examples/integrations/pm/{jira-eng.yaml,linear-eng.yaml}       # OSS connector templates w/ security notes
deploy/docker-compose.yml                                      # JIRA_/LINEAR_/PM_ env
deploy/caddy/Caddyfile                                         # raw-body passthrough note for /integrations/pm/webhooks/*
deploy/.env.example, deploy/.env.production.example
docs/integrations/pm-adapters.md                               # setup guide (Jira app, Linear OAuth, webhook URLs)
```

---

## 11. Research references (relevant links from the spec/research report)

- **External PM Adapter Contract (`PMAdapter` Protocol)** — `docs/FORGE_SPEC.md` → Native Project Board → "External PM Adapter Contract"; V2 targets Jira, Linear, Asana, Monday.com (F18 implements Jira + Linear).
- **Integrations V2 line item** — `docs/FORGE_SPEC.md` → Integrations → "V2: Jira, Linear, Asana, Monday.com (PM sync bidirectional) …"; Phased Roadmap → Phase 2 → "External PM adapters (Jira, Linear)".
- **Extension point** — `docs/FORGE_SPEC.md` → OSS Strategy → Extension Points → "PMAdapter interface: add any external board integration" (why the sync engine is provider-agnostic and the adapter is the only provider-specific surface).
- **Task-as-control-plane / board first** — `docs/FORGE_SPEC.md` → Core Design Principle #6 ("Internal board first, integrations second … exposing adapters for Jira, Linear …"); `docs/forge-research-report.md` → "Symphony … task-as-control-plane" and "Open SWE … Linear integration" (precedent for tracker sync).
- **Security model reused** — `docs/FORGE_SPEC.md` → Security table (secrets encrypted at rest + per-workspace isolation, audit every action, secret redaction, RBAC, rate limiting) and MCP Security Rules (token binding RFC 8707 — applied by analogy to OAuth audience binding); `docs/forge-research-report.md` → "Model Context Protocol → Security considerations" (least-privilege, input validation, audit).
- **Linear (UX + API reference)** — https://linear.app/ (spec Research Links → Project Management Reference Products). Linear GraphQL API: https://developers.linear.app/docs/graphql/working-with-the-graphql-api ; webhooks (HMAC signature): https://developers.linear.app/docs/graphql/webhooks .
- **Jira Cloud REST API v3** (issues, transitions, dynamic webhooks): https://developer.atlassian.com/cloud/jira/platform/rest/v3/ ; OAuth 2.0 (3LO): https://developer.atlassian.com/cloud/jira/platform/oauth-2-3lo-apps/ . (External canonical references for the live-verification phase; not in the spec but required to implement against the real APIs.)
- **Webhook raw-body + idempotency precedent** — sibling slice `docs/implementation-slices/v1/F03-github-app.md` (signature over raw bytes, delivery-id dedup, transport/fixture offline testing, audit-per-call) — F18 mirrors this design for PM providers.
- **Board change-feed / `ForgeTask` shape** — sibling slice `docs/implementation-slices/v1/F01-project-board.md` (`activity_events` append-only outbox, `actor_kind`/payload tagging, optimistic `version`, status-transition policy, SSE backbone) — the substrate F18's sync engine reads and writes.
- **Stack** — FastAPI, Pydantic v2, SQLAlchemy 2.x + Alembic, Celery + Redis, Next.js + TanStack Query/Table, shadcn/ui (`docs/FORGE_SPEC.md` → Technology Stack): https://docs.pydantic.dev/latest/ , https://docs.sqlalchemy.org/en/20/ , https://alembic.sqlalchemy.org/en/latest/ , https://docs.celeryq.dev/ , https://tanstack.com/query .

---

## 12. Out of scope / future

- **Asana & Monday.com adapters** — the `PMAdapter` Protocol + `PMSyncEngine` are built to accept them (just add `forge_integrations/pm/asana/*` + `monday/*` and register in `registry.py`), but only Jira + Linear ship in F18 (per the feature title and the Phase-2 roadmap line "Jira, Linear").
- **GitLab issues / Datadog / Sentry / PagerDuty / Grafana** — separate V2 integrations, not PM-board sync.
- **Comment / attachment / sub-task / dependency sync** — F18 syncs the core task fields (title, description, status, priority, assignee, labels). Threaded comments, attachments, sub-task hierarchy, and blocks/blocked-by graph sync are future.
- **Sprint / cycle / epic mapping** — Forge sprints/milestones ↔ Jira sprints / Linear cycles & projects is future; F18 maps at the issue/task grain only.
- **Custom-field auto-discovery UI** — `field_map` is honored by the engine, but a point-and-click custom-field mapper UI (beyond status/priority editors) is future; v2 ships JSON-edit + the two structured editors.
- **Full ADF rich-text fidelity** — Jira description sync is best-effort markdown↔ADF for common nodes; tables/panels/media round-tripping is future.
- **User/account auto-mapping** — assignee mapping uses email match (`assignee_email`); a managed Forge-user ↔ external-account directory with SCIM is future (Phase 3 SSO/SCIM territory).
- **Two-way label/state *creation*** — F18 maps onto existing external statuses/labels; auto-creating missing external workflow states or labels from Forge is future.
- **Real-time outbound via durable streaming (Temporal)** — F18's OUT path is a Celery beat scan over the `activity_events` outbox (+ optional Redis subscription). Moving sync onto Temporal activities is deferred to the broader V2 Temporal migration (Phased Roadmap → "Temporal workflow engine integration").
