# F09 — MCP Gateway V1 (read-only query-through)

> Phase: v1 · Spec module(s): MCP Connector Layer, `apps/mcp-gateway`, `packages/mcp-sdk`, Auth & Secrets (BYOK vault), Observability (audit) · Status target: A workspace admin can register an MCP server (http/sse transport) with namespace + auth scoping; the dedicated gateway service negotiates capabilities, performs **read-only** resource list/read and read-only tool calls, exposes a `query` endpoint that returns normalized, redacted resource snapshots for live retrieval at task time, enforces every spec MCP security rule (write-blocked by default, RFC 8707 token binding, input validation, namespace scoping, secret redaction, per-tool policy evaluation), and writes an immutable audit row for every MCP operation. No write/mutating MCP call ever executes in V1.

---

## 1. Intent — what & why

Forge's knowledge layer is source-agnostic: "Any MCP-compatible source ingests through the same interface" (`docs/FORGE_SPEC.md` → Core Design Principles #7). MCP eliminates per-source integration code — Confluence, Notion, Postgres, GitHub, Slack, internal APIs all expose the same JSON-RPC capability surface (`docs/forge-research-report.md` → "Model Context Protocol"). F09 builds the **dedicated gateway service** the stack calls out as a first-class component (`apps/mcp-gateway/` in the monorepo, `mcp-gateway` in the compose service list, "MCP Python SDK + dedicated gateway service" in the tech stack).

V1 is intentionally the smallest useful slice: **read-only query-through** (Knowledge Sync Modes → "MCP query-through | Call MCP server live at retrieval time | Always-fresh external data"; Phase 1 roadmap → "MCP gateway V1: read-only query-through mode"). It does NOT index MCP content into pgvector (that is `mcp_sync_and_index`, Phase 2). It exposes a live, fresh, normalized resource feed that F05's hybrid retriever joins into its candidate pool at query time.

The gateway is also the single chokepoint that makes MCP **trustworthy** for a self-hosted engineering platform. The spec's MCP Security Rules and the cited DoD 2026 advisory (`docs/forge-research-report.md` → "Security considerations") require: read-only defaults, least-privilege RFC 8707 token binding, input validation before tool execution, per-connection namespace scoping, secret redaction, and a full audit log on every call. F09 enforces all seven structurally in `packages/mcp-sdk` so no consumer (knowledge service, agent runtime) can bypass them.

This slice delivers two artifacts: the reusable `packages/mcp-sdk` (connection model, transports, client, security guards, audit, normalization) and the `apps/mcp-gateway` FastAPI service that owns live MCP sessions and exposes an internal API to `apps/api` and `apps/worker`.

## 2. User-facing behavior / journeys

- **Journey A — Register an MCP server (admin).** An admin opens Settings → MCP, clicks "Add connection", fills a form matching the spec's `mcp_connection` schema (transport `http`, endpoint `https://mcp.company.internal/confluence`, auth `oauth`, allowed namespaces `[engineering, architecture]`). `allow_write` is shown but **disabled and false** with the note "Write is not available in V1 (read-only)". On save, the connection is created with `status=pending`.
- **Journey B — Test / connect.** The admin clicks "Test connection". The api proxies to the gateway, which opens a session, runs MCP `initialize`, negotiates capabilities, and returns `{resources:true, tools:true, prompts:false, server_name, protocol_version, latency_ms}`. The card flips to `connected` and shows the capability badges. For `auth_type=oauth`, "Connect" launches the OAuth flow (audience-bound per RFC 8707); on callback the status becomes `connected`.
- **Journey C — Query-through during a run.** A `Task` whose `knowledge_scope.mcp_sources` includes `confluence-engineering` enters `executing`. F05's retriever calls the gateway `POST /connections/{id}/query` with the user/agent query. The gateway calls the server **live** (via the configured read-only `query_tool`, falling back to resources list+read), redacts secrets, scopes to allowed namespaces, and returns fresh `McpResourceSnapshot[]` that join the hybrid candidate pool. The agent sees always-fresh external context; the Approval UI later shows `mcp://confluence-engineering/...` provenance.
- **Journey D — A blocked write is observable.** If any code path attempts a mutating tool call (or a write is requested via policy override), the gateway refuses with `WriteBlocked`, performs no MCP call, and writes an audit row `result_status=denied`. The admin sees the denial in the connection's Audit tab.
- **Journey E — Audit & health (admin).** The admin opens the Audit tab and sees every operation: operation type, target (tool/resource), namespace, payload hash, result status, latency. A background health probe keeps `status` and capabilities current; a server that starts failing flips to `error` with a redacted reason.

## 3. Vertical slice

### 3.1 Data model (tables/columns/migrations touched)

Single Alembic migration `apps/api/alembic/versions/0009_mcp_gateway.py` — depends on the `workspaces`/`users` baseline from `v1/F00-foundation-substrate` and the encrypted BYOK secrets vault from `cross-cutting/F37-auth-secrets-byok`. The `0009` ordinal is illustrative; set `down_revision` to the actual Alembic head at integration time. No new extensions required.

**Table `mcp_connections`** (maps to `MCPConnection[]` in Core Data Model; columns mirror the `mcp_connection` YAML schema)

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid` PK | `gen_random_uuid()` |
| `workspace_id` | `uuid` NOT NULL FK → `workspaces.id` ON DELETE CASCADE | tenant key |
| `slug` | `text` NOT NULL | the YAML `id` (e.g. `confluence-engineering`); stable external handle used in `knowledge_scope.mcp_sources` and `source_uri` |
| `name` | `text` NOT NULL | display name |
| `transport` | `text` NOT NULL DEFAULT `'http'` | enum: `http \| sse \| stdio` (V1 implements `http`, `sse`; `stdio` rejected — see §12) |
| `endpoint` | `text` NULL | required for `http`/`sse` |
| `command` | `jsonb` NULL | argv for `stdio` (reserved, V2) |
| `auth_type` | `text` NOT NULL DEFAULT `'none'` | enum: `oauth \| api_key \| none` |
| `resource_audience` | `text` NULL | RFC 8707 `resource` value for token binding (defaults to canonicalized `endpoint`) |
| `credential_ref` | `text` NULL | vault secret id (api_key value, or oauth token bundle); never the secret itself |
| `capabilities` | `jsonb` NOT NULL DEFAULT `'{}'` | cached `MCPCapabilities` from last `initialize` |
| `allow_write` | `boolean` NOT NULL DEFAULT `false` | MUST default false (spec rule 1). V1 gateway blocks writes even when true |
| `allowed_namespaces` | `jsonb` NOT NULL DEFAULT `'[]'` | per-connection namespace allowlist (spec rule 5); empty = all namespaces from this server allowed |
| `index_strategy` | `text` NOT NULL DEFAULT `'query_through'` | enum: `query_through \| sync_and_index`; V1 only honors `query_through` |
| `sync_mode` | `text` NOT NULL DEFAULT `'incremental'` | advisory in V1 (used by Phase-2 sync-and-index) |
| `freshness_sla_minutes` | `integer` NOT NULL DEFAULT `30` | advisory freshness target surfaced to F05 |
| `query_tool` | `text` NULL | name of the server's read-only search/query tool used for query-through; null → list+read fallback |
| `status` | `text` NOT NULL DEFAULT `'pending'` | enum: `pending \| connected \| error \| disabled` |
| `last_health_at` | `timestamptz` NULL | |
| `last_health_error` | `text` NULL | redacted |
| `config` | `jsonb` NOT NULL DEFAULT `'{}'` | extra transport headers, oauth scopes, etc. |
| `created_at` / `updated_at` | `timestamptz` NOT NULL | |

Constraints/indexes: `UNIQUE (workspace_id, slug)`; btree `(workspace_id, status)`. `CHECK (transport IN ('http','sse','stdio'))`, `CHECK (auth_type IN ('oauth','api_key','none'))`.

**Table `mcp_audit_log`** (spec Security → "Audit log … immutable, queryable"; MCP rule 4)

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid` PK | |
| `workspace_id` | `uuid` NOT NULL FK → `workspaces.id` ON DELETE CASCADE | |
| `connection_id` | `uuid` NULL FK → `mcp_connections.id` ON DELETE SET NULL | nullable so audit survives connection deletion |
| `connection_slug` | `text` NOT NULL | snapshot so audit is durable after deletion |
| `actor` | `text` NOT NULL | `user:<uuid>` \| `agent_run:<uuid>` \| `system` |
| `operation` | `text` NOT NULL | enum: `resources.list \| resources.read \| tools.list \| tools.call \| query \| health \| oauth` |
| `target` | `text` NULL | tool name or resource uri |
| `namespace` | `text` NULL | namespace touched |
| `payload_hash` | `text` NOT NULL | sha256 hex of canonical request payload (spec rule 4: "payload hash") |
| `result_status` | `text` NOT NULL | enum: `ok \| denied \| error` |
| `error_code` | `text` NULL | e.g. `write_blocked`, `policy_denied`, `namespace_denied`, `schema_invalid`, `capability_unavailable`, `unsupported_transport`, `transport_error` |
| `latency_ms` | `numeric(10,2)` NOT NULL | spec rule 4: "latency" |
| `bytes_out` | `integer` NOT NULL DEFAULT 0 | size of returned (post-redaction) payload |
| `created_at` | `timestamptz` NOT NULL DEFAULT `now()` | |

Indexes: `(workspace_id, connection_id, created_at DESC)`, `(workspace_id, operation, created_at DESC)`.

Immutability: append-only. The ORM exposes only insert + select; the migration applies the reusable `attach_immutability_trigger("mcp_audit_log")` helper from `cross-cutting/F39-audit-log` (falling back to a local trigger `mcp_audit_log_immutable` if F39 has not yet landed) that `RAISE`s on `UPDATE`/`DELETE` (delete permitted solely by the `ON DELETE CASCADE` from `workspaces`, handled at the workspace-teardown layer). Verified by AC18.

**OAuth transient state** is held in Redis, not Postgres: key `mcp:oauth:{state}` → `{connection_id, code_verifier, resource_audience, redirect_uri}` with TTL 600s. PKCE `code_verifier` is short-lived and never persisted at rest.

### 3.2 Backend (FastAPI routes + services/packages)

Two backend components: the user-facing routes in `apps/api`, and the dedicated `apps/mcp-gateway` service. `apps/api` never speaks MCP directly — it owns the connection records and proxies live operations to the gateway over an internal, service-token-authenticated HTTP API.

**`apps/api` — router `apps/api/app/api/v1/mcp.py`, mounted at `/api/v1/mcp`.** All routes require auth + workspace membership. Thin controllers delegate to `apps/api/app/services/mcp_service.py`, which persists connections, resolves/stores credentials via the `cross-cutting/F37-auth-secrets-byok` encrypted vault, and calls the gateway via `MCPGatewayClient` (from `mcp-sdk`).

| Method | Path | RBAC | Purpose |
|---|---|---|---|
| `POST` | `/connections` | admin | Register a connection (validates `MCPConnectionConfig`; forces `allow_write=false`). |
| `GET` | `/connections` | member | List connections (status, capabilities, namespaces). |
| `GET` | `/connections/{id}` | member | Detail + last health. |
| `PATCH` | `/connections/{id}` | admin | Update name/namespaces/query_tool/enable/disable. `allow_write` cannot be set true in V1 (422). |
| `DELETE` | `/connections/{id}` | admin | Delete connection (audit rows retained via SET NULL + slug snapshot). |
| `POST` | `/connections/{id}/test` | admin | Proxy → gateway health probe; persists capabilities + status. |
| `POST` | `/connections/{id}/oauth/start` | admin | Begin OAuth (returns authorization URL; stores PKCE+state+resource in Redis). |
| `GET` | `/connections/{id}/oauth/callback` | admin | Exchange code (audience-bound, RFC 8707), store tokens in vault, set `connected`. |
| `GET` | `/connections/{id}/audit` | admin | Paginated `mcp_audit_log` query (filter by operation/status/date). |

**`apps/mcp-gateway` — internal service (FastAPI).** Not exposed via Caddy. Authenticated by a shared internal service token (`MCP_GATEWAY_INTERNAL_TOKEN`) on every request via `app/auth.py`. Every internal request carries `{workspace_id, actor}`; the gateway verifies the target connection belongs to that workspace before acting.

| Method | Path | Caller | Purpose |
|---|---|---|---|
| `GET` | `/healthz` | compose healthcheck | Liveness. |
| `POST` | `/connections/{id}/health` | api `/test`, worker beat | `initialize` + capability negotiation; returns `HealthResult`. |
| `GET` | `/connections/{id}/capabilities` | api | Cached capabilities. |
| `POST` | `/connections/{id}/resources/list` | api/worker | MCP `resources/list` (namespace-scoped, paginated). |
| `POST` | `/connections/{id}/resources/read` | api/worker | MCP `resources/read` (namespace-checked, redacted, size-capped). |
| `POST` | `/connections/{id}/tools/list` | api | MCP `tools/list` (annotated with read-only hints). |
| `POST` | `/connections/{id}/tools/call` | api/worker | Read-only tool call (write-guarded, schema-validated, policy-evaluated, redacted). |
| `POST` | `/connections/{id}/query` | worker (F05) | Query-through: live resource snapshots for retrieval. |

`packages/mcp-sdk` (framework-agnostic; the security and protocol heart of the slice):

```
packages/mcp-sdk/src/mcp_sdk/
├── models.py            # Pydantic v2 models + enums (§4)
├── protocols.py         # MCPTransport, MCPClient, CredentialResolver, AuditSink, SecretRedactor, ToolPolicy
├── client.py            # MCPClientSession: initialize/list_resources/read_resource/list_tools/call_tool
├── pool.py              # SessionManager: per-connection session lifecycle, reconnect, concurrency
├── gateway_client.py    # MCPGatewayClient: typed HTTP client used by apps/api + apps/worker
├── transports/
│   ├── http.py          # StreamableHttpTransport (2026 RC stateless HTTP) — default
│   └── sse.py           # SseTransport (legacy remote)
├── security/
│   ├── write_guard.py   # WriteGuard.assert_read_only(tool, allow_write) — blocks mutating calls
│   ├── namespace.py     # NamespaceScope.filter()/assert_allowed()
│   ├── validation.py    # validate_arguments(input_schema, arguments) via jsonschema
│   ├── token_binding.py # build_token_request(resource_audience) — RFC 8707 resource param + PKCE
│   └── redaction.py     # redact_secrets() (regex + entropy heuristics)
├── audit.py             # AuditEntry builder, canonical_payload_hash()
├── normalize.py         # to_snapshot(): MCP resource/tool result -> McpResourceSnapshot
└── errors.py            # WriteBlocked, NamespaceDenied, SchemaInvalid, CapabilityUnavailable, TransportError
```

**Read-only enforcement (the load-bearing invariant).** Every `call_tool` passes through `WriteGuard.assert_read_only(tool, allow_write)` BEFORE any transport request. In V1 the guard is hard-wired: a tool is callable only if it is non-mutating — `read_only_hint is True`, OR (`read_only_hint is None` AND `destructive_hint is not True` AND the tool name is in the connection's `query_tool`/explicit read allowlist). Any tool with `destructive_hint=True`, or a write request via policy override, raises `WriteBlocked` regardless of `allow_write`. Write execution is V2 (§12).

**Query-through pipeline** (`/query`): (1) resolve connection + verify workspace; (2) acquire pooled session; (3) if `query_tool` configured → `call_tool(query_tool, {"query": text, "limit": n})` (read-only guarded + schema-validated); else → `resources/list` (namespace-scoped) then `resources/read` the top-`limit` candidates; (4) `redact_secrets` every text payload; (5) `normalize.to_snapshot` → `McpResourceSnapshot` with `source_uri = mcp://{slug}/{uri}`, `namespace`, `retrieved_at`, `metadata`; (6) write one audit row; (7) return `QueryResponse`. F05's `HybridRetrievalService` treats each snapshot as an ephemeral `RankedChunk` candidate (chunk_type `mcp_resource`, weight 1.0).

### 3.3 Worker / agent runtime (Celery tasks, LangGraph, if any)

Minimal — V1 query-through is synchronous/live, so there is no MCP indexing task (that is Phase-2 sync-and-index). One Celery component in `apps/worker/tasks/mcp.py` (queue `mcp`):

- `mcp.health_probe_all() -> dict` — Celery beat (every 5 min). For each `connected`/`error` connection, calls the gateway `/connections/{id}/health`; updates `status`, `capabilities`, `last_health_at`, `last_health_error`. Bounds: per-connection timeout, isolated failures don't fail the batch.

No LangGraph in this slice. The agent runtime (`v1/F06-single-execution-agent`) and knowledge service (`v1/F05-hybrid-knowledge-retrieval`) consume the gateway via `MCPGatewayClient`; the `query_mcp` agent tool (Subagent Role Definitions → `researcher`) is a thin wrapper over `MCPGatewayClient.query()` registered in F06's agent tool registry, and is the only MCP entry point exposed to agents — write tools are never registered.

### 3.4 Frontend / UI (Next.js routes/components, if any)

- `apps/web/app/(app)/settings/mcp/page.tsx` — MCP connections list: name, transport, status badge, capability badges (resources/tools/prompts), allowed namespaces, last health. Admin-only mutate actions; members read-only. Uses TanStack Query against `/api/v1/mcp/connections`.
- `apps/web/components/mcp/connection-form.tsx` — create/edit form matching `MCPConnectionConfig`. `allow_write` toggle is rendered **disabled + off** with helptext "Read-only in V1".
- `apps/web/components/mcp/connection-card.tsx` — status, "Test connection", OAuth "Connect" button (when `auth_type=oauth`).
- `apps/web/components/mcp/audit-table.tsx` — paginated audit view (operation, target, namespace, status, latency) using TanStack Table.
- `apps/web/lib/api/mcp.ts` — typed client matching §4 contracts.

The Approval UI "knowledge provenance" panel (spec → "Approval UI Must Show", item 5; owned by `cross-cutting/F36-human-approval-system`'s `KnowledgeProvenancePanel`) consumes the `mcp://{slug}/...` `source_uri` produced by F09 and surfaced via `v1/F05-hybrid-knowledge-retrieval`; the panel itself is out of scope here.

### 3.5 Infra / deploy (compose, helm, caddy, if any)

- `mcp-gateway` service already listed in `deploy/docker-compose.yml`. This slice fleshes it out: pinned `@sha256` image built from `apps/mcp-gateway`, `autoheal=true` label, healthcheck `GET /healthz`, CPU/mem limits, non-root user.
- **Network segmentation** (spec → "Network segmentation: separate networks for API, database, MCP gateway, observability"): `mcp-gateway` joins `backend` (reachable by `api` + `worker`), `db` (read `mcp_connections` + the secrets vault), and a dedicated `mcp-egress` network for outbound calls to MCP servers. It is **never** reachable through Caddy.
- New env (add to `deploy/.env.example` + `.env.production.example`): `MCP_GATEWAY_URL=http://mcp-gateway:8090`, `MCP_GATEWAY_INTERNAL_TOKEN` (shared internal service token), `MCP_HTTP_TIMEOUT_SECONDS=30`, `MCP_MAX_RESOURCE_BYTES=1048576`, `MCP_SESSION_TTL_SECONDS=300`, `MCP_QUERY_DEFAULT_LIMIT=10`, `MCP_DEFAULT_ALLOW_WRITE=false`.
- Caddy: no public route for the gateway. The `/api/v1/mcp/connections/{id}/oauth/callback` redirect URI is served by `api` (public) per existing Caddy config.

## 4. Public interfaces / contracts (exact signatures, Pydantic models, Protocols, YAML schemas)

`packages/mcp-sdk/src/mcp_sdk/models.py`:

```python
from datetime import datetime
from enum import StrEnum
from uuid import UUID
from pydantic import BaseModel, Field

class Transport(StrEnum):
    http = "http"; sse = "sse"; stdio = "stdio"

class AuthType(StrEnum):
    oauth = "oauth"; api_key = "api_key"; none = "none"

class IndexStrategy(StrEnum):
    query_through = "query_through"; sync_and_index = "sync_and_index"

class AuditOperation(StrEnum):
    resources_list = "resources.list"; resources_read = "resources.read"
    tools_list = "tools.list"; tools_call = "tools.call"
    query = "query"; health = "health"; oauth = "oauth"

class MCPCapabilities(BaseModel):
    resources: bool = False
    tools: bool = False
    prompts: bool = False
    server_name: str | None = None
    server_version: str | None = None
    protocol_version: str | None = None

class MCPConnectionConfig(BaseModel):
    id: str                                   # slug, e.g. "confluence-engineering"
    name: str
    transport: Transport = Transport.http
    endpoint: str | None = None               # required for http/sse
    command: list[str] | None = None          # stdio (reserved, V2)
    auth_type: AuthType = AuthType.none
    resource_audience: str | None = None       # RFC 8707 resource value
    capabilities: MCPCapabilities = Field(default_factory=MCPCapabilities)
    index_strategy: IndexStrategy = IndexStrategy.query_through
    sync_mode: str = "incremental"
    freshness_sla_minutes: int = 30
    allow_write: bool = False                   # MUST default false (rule 1)
    allowed_namespaces: list[str] = Field(default_factory=list)
    query_tool: str | None = None
    config: dict = Field(default_factory=dict)  # extra headers, oauth scopes

class McpResource(BaseModel):
    uri: str
    name: str | None = None
    mime_type: str | None = None
    description: str | None = None
    namespace: str | None = None
    annotations: dict = Field(default_factory=dict)

class McpResourceContent(BaseModel):
    uri: str
    mime_type: str | None = None
    text: str | None = None                     # redacted
    blob_base64: str | None = None
    truncated: bool = False

class McpTool(BaseModel):
    name: str
    description: str | None = None
    input_schema: dict = Field(default_factory=dict)   # JSON Schema
    read_only_hint: bool | None = None          # MCP tool annotation
    destructive_hint: bool | None = None

class ToolCallRequest(BaseModel):
    name: str
    arguments: dict = Field(default_factory=dict)

class ToolCallResult(BaseModel):
    is_error: bool = False
    content: list[dict] = Field(default_factory=list)   # MCP content blocks, redacted
    structured: dict | None = None

class McpResourceSnapshot(BaseModel):           # normalized for knowledge-core (F05)
    connection_id: str                          # slug
    uri: str
    namespace: str | None = None
    title: str | None = None
    text: str                                   # redacted
    mime_type: str | None = None
    retrieved_at: datetime
    source_uri: str                             # "mcp://{slug}/{uri}"
    metadata: dict = Field(default_factory=dict)

class QueryRequest(BaseModel):
    workspace_id: UUID
    actor: str
    text: str
    limit: int = Field(10, ge=1, le=50)
    namespaces: list[str] | None = None

class QueryResponse(BaseModel):
    connection_id: str
    snapshots: list[McpResourceSnapshot]
    truncated: bool = False

class HealthResult(BaseModel):
    status: str                                 # connected | error
    capabilities: MCPCapabilities
    latency_ms: float
    error: str | None = None                    # redacted

class AuditEntry(BaseModel):
    workspace_id: UUID
    connection_id: UUID | None
    connection_slug: str
    actor: str
    operation: AuditOperation
    target: str | None = None
    namespace: str | None = None
    payload_hash: str
    result_status: str                          # ok | denied | error
    error_code: str | None = None
    latency_ms: float
    bytes_out: int = 0
```

`packages/mcp-sdk/src/mcp_sdk/protocols.py`:

```python
from typing import Protocol
from uuid import UUID
from .models import (MCPCapabilities, McpResource, McpResourceContent, McpTool,
                     ToolCallRequest, ToolCallResult, AuditEntry)

class MCPTransport(Protocol):
    async def open(self) -> None: ...
    async def request(self, method: str, params: dict) -> dict: ...   # JSON-RPC 2.0
    async def close(self) -> None: ...

class MCPClient(Protocol):
    async def initialize(self) -> MCPCapabilities: ...
    async def list_resources(self, *, namespace: str | None = None,
                             cursor: str | None = None) -> tuple[list[McpResource], str | None]: ...
    async def read_resource(self, uri: str) -> McpResourceContent: ...
    async def list_tools(self) -> list[McpTool]: ...
    async def call_tool(self, req: ToolCallRequest) -> ToolCallResult: ...

class CredentialResolver(Protocol):
    async def resolve(self, connection_id: UUID) -> "Credential": ...   # api_key / oauth bundle from vault

class AuditSink(Protocol):
    async def record(self, entry: AuditEntry) -> None: ...

class SecretRedactor(Protocol):
    def redact(self, text: str) -> str: ...

class ToolPolicy(Protocol):
    # Spec MCP rule 7: MCP tool invocations require the same policy evaluation as any agent tool call.
    def evaluate_tool_call(self, *, connection_slug: str, tool: McpTool,
                           arguments: dict, allow_write: bool) -> "PolicyDecision": ...
```

**Audit contract reconciliation.** `mcp-sdk`'s `AuditSink`/`AuditEntry` are the MCP-domain shape written to `mcp_audit_log` (the system of record for MCP ops). When `cross-cutting/F39-audit-log` is present, the gateway's concrete `AuditSink` ALSO maps each `AuditEntry` onto F39's canonical `AuditEvent` and emits it through F39's central `AuditSink` (`action ∈ {mcp.tool_call, mcp.resource_read, mcp.resource_list, mcp.query, mcp.write_blocked, mcp.health, mcp.oauth}`, `actor_type ∈ {user, agent_runner, system}`, `detail_ref={"table":"mcp_audit_log","id":<row_id>}`), so MCP calls appear in the platform-wide tamper-evident log per spec ("audit log for every … MCP call"). If F39 has not landed, `mcp_audit_log` is the self-contained record.

`packages/mcp-sdk/src/mcp_sdk/security/write_guard.py` — the read-only invariant:

```python
def is_mutating(tool: McpTool) -> bool:
    """True unless the tool is provably read-only.
    Read-only iff read_only_hint is True, OR (read_only_hint is None AND
    destructive_hint is not True AND name in the connection read-allowlist)."""

def assert_read_only(tool: McpTool, *, allow_write: bool, allowlist: set[str]) -> None:
    """Raise WriteBlocked if the tool is mutating. In V1, raises regardless of allow_write."""
```

`packages/mcp-sdk/src/mcp_sdk/security/token_binding.py` — RFC 8707 (spec rule 2):

```python
def build_authorization_url(*, auth_endpoint: str, client_id: str, redirect_uri: str,
                            resource_audience: str, scopes: list[str],
                            state: str, code_challenge: str) -> str: ...

def build_token_request(*, token_endpoint: str, code: str, code_verifier: str,
                        redirect_uri: str, client_id: str,
                        resource_audience: str) -> dict:
    """Returns the token-exchange form body INCLUDING resource=<resource_audience>
    (RFC 8707) so the issued access token is audience-bound to this MCP server only."""
```

`packages/mcp-sdk/src/mcp_sdk/gateway_client.py` — typed client used by api + worker:

```python
class MCPGatewayClient:
    def __init__(self, base_url: str, internal_token: str, *, timeout: float = 30.0): ...
    async def health(self, connection_id: UUID, *, workspace_id: UUID, actor: str) -> HealthResult: ...
    async def list_resources(self, connection_id: UUID, *, workspace_id: UUID, actor: str,
                             namespace: str | None = None, cursor: str | None = None
                             ) -> tuple[list[McpResource], str | None]: ...
    async def read_resource(self, connection_id: UUID, uri: str, *,
                            workspace_id: UUID, actor: str) -> McpResourceContent: ...
    async def list_tools(self, connection_id: UUID, *, workspace_id: UUID, actor: str) -> list[McpTool]: ...
    async def call_tool(self, connection_id: UUID, req: ToolCallRequest, *,
                        workspace_id: UUID, actor: str) -> ToolCallResult: ...
    async def query(self, connection_id: UUID, req: QueryRequest) -> QueryResponse: ...
```

FastAPI request/response (`apps/api/app/schemas/mcp.py`):

```python
class CreateConnectionRequest(BaseModel):
    config: MCPConnectionConfig          # allow_write forced false server-side

class UpdateConnectionRequest(BaseModel):
    name: str | None = None
    allowed_namespaces: list[str] | None = None
    query_tool: str | None = None
    status: str | None = None            # only "connected" -> "disabled" / re-enable
    # allow_write intentionally absent — cannot be enabled in V1

class ConnectionResponse(BaseModel):
    id: UUID
    slug: str
    name: str
    transport: Transport
    auth_type: AuthType
    allow_write: bool                    # always false in V1
    allowed_namespaces: list[str]
    capabilities: MCPCapabilities
    status: str
    last_health_at: datetime | None
```

YAML `mcp_connection` (the spec's authoritative shape, accepted by `POST /connections` and shipped as an example connector template):

```yaml
mcp_connection:
  id: confluence-engineering
  name: Engineering Confluence
  transport: http                 # http (2026 RC preferred) | sse (legacy) ; stdio reserved for V2
  endpoint: https://mcp.company.internal/confluence
  auth:
    type: oauth                    # oauth | api_key | none
    resource_audience: https://mcp.company.internal/confluence   # RFC 8707
  capabilities:
    resources: true
    tools: true
    prompts: false
  sync_mode: incremental
  index_strategy: query_through    # query_through (V1) | sync_and_index (V2)
  freshness_sla_minutes: 30
  allow_write: false               # MUST default false; ignored/blocked in V1
  allowed_namespaces: [engineering, architecture]
  query_tool: search               # optional read-only tool used for query-through
```

## 5. Dependencies — features/slices that must exist first

Slugs below match the actual files in `docs/implementation-slices/` and are authoritative. (Slug reconciliation: the platform foundation does not yet have a dedicated numbered file; sibling slices use `v1/F00-foundation-substrate` as the authoritative placeholder, adopted here.)

- **`v1/F00-foundation-substrate`** (required) — uv workspace, FastAPI skeleton, `packages/db` SQLAlchemy 2.x async session + `workspaces`/`users` baseline + Alembic baseline, the `apps/worker` Celery app, Redis, the four RBAC roles (`admin`/`member`/`viewer`/`agent-runner`) + auth dependency, and the `mcp-gateway` compose service stub. F09 adds migration `0009` and fleshes out the gateway service.
- **`cross-cutting/F37-auth-secrets-byok`** (required) — the AES-256-GCM envelope-encrypted, per-workspace-isolated BYOK secrets vault, the OAuth/API-key auth resolving every request to a `Principal`, and the single canonical `SecretRedactor`. F09 stores MCP `api_key`/`oauth` credentials in this vault and resolves them in the gateway; `mcp-sdk`'s `redaction.py` implements/extends F37's canonical `SecretRedactor` (shared pattern set) rather than diverging; all `apps/api` MCP routes use F37's `require_role(...)` dependency.
- **`v1/F04-repo-policy`** (soft) — supplies the `PolicyEvaluator` (`packages/contracts` `Policy`/`ToolCall`/`Decision`) that backs `mcp-sdk`'s `ToolPolicy`, so MCP tool calls get the same policy evaluation as any agent tool (spec rule 7). Until present, the gateway enforces a built-in read-only + namespace policy as a hard floor (write always blocked); full `PolicyEvaluator` integration is the complete form.
- **`cross-cutting/F39-audit-log`** (soft) — provides the canonical `AuditEvent` contract + `AuditSink` Protocol in `packages/contracts` and the reusable `attach_immutability_trigger(table)` helper. F09's gateway `AuditSink` fans each MCP `AuditEntry` out to F39's central, tamper-evident `audit_log` as a canonical `AuditEvent`, and `mcp_audit_log` opts into F39's immutability trigger helper. If F39 lags, `mcp_audit_log` is the self-contained system of record with its own local trigger.

**Consumers (not dependencies):** `v1/F05-hybrid-knowledge-retrieval` (MCP query-through candidate source, calls the gateway `/query` and consumes `McpResourceSnapshot[]` as `mcp_resource` candidates) and `v1/F06-single-execution-agent` (the `query_mcp` agent tool wraps `MCPGatewayClient.query()`) both depend on F09; F09 has no dependency on them and ships its own fixtures to be testable in isolation.

## 6. Acceptance criteria (numbered, testable)

1. Migration `0009` creates `mcp_connections` + `mcp_audit_log` with all columns, the unique/check constraints, the audit indexes, and the `mcp_audit_log_immutable` trigger; `alembic downgrade` cleanly drops them.
2. **Write-blocked by default (rule 1).** New connections persist `allow_write=false`; `PATCH` cannot set it true (422). A `call_tool` for any mutating tool raises `WriteBlocked`, executes NO transport request, and writes an audit row `result_status=denied, error_code=write_blocked` — even when the connection row is forced to `allow_write=true`.
3. **Read-only tool allowed.** A tool with `read_only_hint=true` (or in the connection read-allowlist with no destructive hint) is callable and returns its result; a tool with `destructive_hint=true` is blocked.
4. **Namespace scoping (rule 5).** `resources/list`, `resources/read`, and `query` return only resources within `allowed_namespaces`; a read of a resource outside the allowlist raises `NamespaceDenied` and audits `denied`. Empty `allowed_namespaces` means all of this server's namespaces are permitted.
5. **Input validation (rule 3).** A `call_tool` whose `arguments` violate the tool's `input_schema` is rejected by `validate_arguments` BEFORE any transport request (assert no MCP call made), audited `error/schema_invalid`.
6. **RFC 8707 token binding (rule 2).** `build_token_request` includes `resource=<resource_audience>` in the token exchange body, and `build_authorization_url` includes the `resource` + PKCE `code_challenge`; an oauth connection's stored token bundle is audience-scoped. Verified by asserting the outgoing request payload.
7. **Audit completeness (rule 4).** Every gateway operation (`resources.list/read`, `tools.list/call`, `query`, `health`, `oauth`) writes exactly one `mcp_audit_log` row with `operation`, `target`, `payload_hash` (sha256 of canonical payload), `result_status`, `latency_ms`, `bytes_out`.
8. **Secret redaction (rule 6).** Resource/tool content and `last_health_error` are passed through `redact_secrets` before leaving the gateway; no secret substring (AWS keys, PEM blocks, `*_API_KEY=...`, high-entropy tokens) appears in any `QueryResponse`, `ToolCallResult`, `McpResourceContent`, audit row, or log line.
9. **Capability negotiation.** `/health` runs MCP `initialize` and returns negotiated `MCPCapabilities`; calling `resources/read` on a server that does not advertise `resources` raises `CapabilityUnavailable` (audited `denied`).
10. **Query-through normalization.** `/query` returns `McpResourceSnapshot[]` with redacted `text`, `source_uri == "mcp://{slug}/{uri}"`, populated `namespace`/`retrieved_at`/`metadata`, honoring `limit` and `namespaces`; uses `query_tool` when configured, else `resources/list`+`read`. Snapshots are consumable by F05 as `mcp_resource` candidates.
11. **Tenant isolation.** A gateway request whose `workspace_id` does not own the target `connection_id` is rejected (404/forbidden, audited `denied`); api routes scope all reads/writes by `workspace_id`.
12. **Internal auth.** Gateway requests without a valid `MCP_GATEWAY_INTERNAL_TOKEN` return 401; the api↔gateway client always sends it.
13. **RBAC.** Create/update/delete/test/oauth require `admin`; list/detail/audit-read require `member` (audit-read may be admin-only — assert chosen policy); `agent-runner` may invoke `query`/`resources.read`/read-only `tools.call` via the gateway client but not mutate connections.
14. **Transport selection.** `http` (Streamable HTTP) is the default transport and is exercised end-to-end; `sse` is supported; registering `transport=stdio` is rejected at the gateway with a clear `unsupported_transport` error (V1).
15. **Health probe lifecycle.** `/test` and `mcp.health_probe_all` set `status`, `capabilities`, `last_health_at`; a failing server sets `status=error` + redacted `last_health_error`.
16. **Session reuse.** The `SessionManager` reuses a single pooled session per `connection_id` across concurrent requests and transparently reconnects after a dropped/expired session (TTL `MCP_SESSION_TTL_SECONDS`).
17. **Resource size cap.** A resource larger than `MCP_MAX_RESOURCE_BYTES` is truncated with `truncated=true` (or rejected per config) and never streamed unbounded into memory/response.
18. **Audit immutability.** `UPDATE` or `DELETE` on `mcp_audit_log` raises (trigger); no route exposes mutation of audit rows.
19. **Policy-evaluation parity (rule 7).** Every `tools.call` (including the query-through `query_tool` call) is passed through `ToolPolicy.evaluate_tool_call` AFTER the write-guard floor; a `deny` decision blocks the call, executes NO transport request, and audits `result_status=denied, error_code=policy_denied`. With `v1/F04-repo-policy`'s `PolicyEvaluator` injected, an MCP tool call gets the identical decision an agent tool call of the same name/args would get; with no evaluator injected, the built-in read-only+namespace floor still applies.

## 7. Test plan (TDD) — concrete test cases (unit + integration), key fixtures

Write tests first; backend-tdd discipline (min coverage 80% per the spec's own profile). Tests live in `packages/mcp-sdk/tests/`, `apps/mcp-gateway/tests/`, and `apps/api/tests/mcp/`.

Key fixtures:
- `FakeMCPServer` — in-memory MCP server implementing `initialize`, `resources/list`, `resources/read`, `tools/list`, `tools/call`. Configurable: resources tagged with namespaces; a read-only `search` tool (`read_only_hint=true`, `input_schema` requiring `query:str`); a `delete_page` write tool (`destructive_hint=true`); one resource whose body contains a fake AWS key + PEM block for redaction tests.
- `FakeTransport(server)` — wraps `FakeMCPServer`, satisfies `MCPTransport`, no network; lets the real `MCPClientSession` + security guards run.
- `FakeCredentialResolver` / `FakeVault` — return deterministic api_key/oauth bundles; assert secrets never logged.
- `InMemoryAuditSink` — captures `AuditEntry` list for assertions (and, when wired, the mapped canonical `AuditEvent`s for the F39 fan-out).
- `FakeToolPolicy` — satisfies the `ToolPolicy` Protocol; configurable to return `allow`/`deny` to exercise rule-7 parity (AC19) independently of `v1/F04-repo-policy`.
- `pg` fixture — real Postgres via testcontainers, migration `0009` applied (for connection/audit SQL + immutability trigger).
- `gateway_app` / `api_app` — httpx `AsyncClient` against the FastAPI apps with DI overrides to inject fakes.

Unit tests — `packages/mcp-sdk/tests/` (pure, no network):
- `test_write_guard_blocks_destructive_and_unannotated` and `test_write_guard_allows_readonly` (AC2, AC3); `test_write_guard_blocks_even_when_allow_write_true` (AC2).
- `test_namespace_scope_filters_and_denies` (AC4).
- `test_validate_arguments_rejects_schema_violation_before_call` (AC5).
- `test_token_request_includes_rfc8707_resource_param` and `test_authorization_url_has_resource_and_pkce` (AC6).
- `test_redact_secrets_strips_aws_pem_apikey_entropy` and `test_redaction_preserves_nonsecret_text` (AC8).
- `test_canonical_payload_hash_stable_and_secret_free` (AC7, AC8).
- `test_to_snapshot_builds_source_uri_and_metadata` (AC10).
- `test_client_initialize_negotiates_capabilities` and `test_read_resource_requires_resources_capability` (AC9).
- `test_session_manager_reuses_and_reconnects` (AC16).
- `test_tool_call_invokes_tool_policy_and_denial_blocks_call` (AC19) — a `FakeToolPolicy` returning `deny` blocks the call before any transport request and yields `policy_denied`; a `FakeToolPolicy` returning `allow` lets a read-only tool through; absence of an injected policy falls back to the built-in floor.

Integration tests — `apps/mcp-gateway/tests/` (gateway service + `FakeTransport`):
- `test_query_through_via_query_tool` and `test_query_through_list_read_fallback` (AC10).
- `test_resources_list_read_namespace_scoped` (AC4).
- `test_tool_call_readonly_ok_write_denied_and_audited` (AC2, AC3, AC7).
- `test_resource_size_cap_truncates` (AC17).
- `test_health_probe_updates_capabilities` (AC9, AC15).
- `test_workspace_mismatch_rejected` (AC11).
- `test_missing_internal_token_401` (AC12).
- `test_unsupported_stdio_transport_rejected` (AC14).
- `test_every_operation_writes_one_audit_row` (AC7).

API tests — `apps/api/tests/mcp/` (httpx AsyncClient, Celery eager):
- `test_create_connection_forces_allow_write_false_and_requires_admin` (AC2, AC13).
- `test_patch_cannot_enable_write` (AC2).
- `test_list_detail_require_membership_and_scope_to_workspace` (AC11, AC13).
- `test_test_endpoint_proxies_gateway_and_persists_capabilities` (AC15).
- `test_oauth_start_callback_binds_audience_and_stores_token` (AC6) — assert vault write + status `connected`, token bundle audience-scoped.
- `test_audit_endpoint_returns_redacted_rows` (AC7, AC8).
- `test_health_probe_beat_updates_statuses` (AC15) — `mcp.health_probe_all` eager.

DB tests — `apps/api/tests/mcp/`:
- `test_migration_0009_up_down` (AC1) — asserts tables, constraints, indexes via `pg_indexes`.
- `test_audit_log_update_delete_raises` (AC18) — trigger blocks `UPDATE`/`DELETE`.

## 8. Security & policy considerations

This slice's reason for existing is to make MCP safe. It implements every numbered rule from `docs/FORGE_SPEC.md` → "MCP Security Rules":

1. **Read-only / write-blocked by default (rule 1).** `allow_write` defaults false, cannot be enabled in V1, and `WriteGuard.assert_read_only` blocks every mutating tool call irrespective of the flag. AC2/AC3.
2. **Token binding RFC 8707 (rule 2).** OAuth authorization + token requests carry the `resource` parameter set to `resource_audience` (PKCE on the code flow), so issued tokens are audience-bound to the specific MCP server — preventing confused-deputy/token-replay against other servers. AC6.
3. **Input validation before execution (rule 3).** Every `call_tool` validates `arguments` against the tool's `input_schema` (jsonschema) before any transport request. AC5.
4. **Full audit log (rule 4).** One immutable `mcp_audit_log` row per operation: tool/resource name, sha256 payload hash, result status, latency, bytes out. Append-only via trigger. AC7/AC18. Feeds the Observability layer (MCP freshness lag, latency) and the platform audit stream.
5. **Per-connection namespace scoping (rule 5).** `NamespaceScope` filters list/read/query to `allowed_namespaces`; out-of-scope reads are denied + audited. AC4.
6. **Secret redaction (rule 6).** `redact_secrets` (implementing/extending the canonical `SecretRedactor` from `cross-cutting/F37-auth-secrets-byok`, shared pattern set with F05's redaction filter) runs on all returned content, health errors, and before hashing/logging — no secret reaches logs, traces, retrieval results, or audit. Credentials live only in the `cross-cutting/F37-auth-secrets-byok` vault, resolved at session time, never in request/response bodies between api and gateway. AC8.
7. **Policy evaluation parity (rule 7).** MCP tool invocations pass through the `ToolPolicy` evaluator (policy-sdk when present), the same gate as any agent tool call. The gateway itself enforces a built-in read-only+namespace policy as a hard floor even if policy-sdk is absent.

Additional controls: tenant isolation (workspace ownership verified on every gateway op, AC11); internal-only network + service-token auth for the gateway, never exposed via Caddy (AC12); RBAC on all api routes with no anonymous access (AC13); DoS bounds (`MCP_HTTP_TIMEOUT_SECONDS`, `MCP_MAX_RESOURCE_BYTES`, `query.limit ≤ 50`, pooled sessions with TTL). The gateway is the MCP "middleware layer" the DoD 2026 advisory flags as the security-critical translation point (`docs/forge-research-report.md` → "Security considerations") — all enforcement lives here, not in callers.

## 9. Effort estimate & risk (S/M/L + key risks)

**Effort: L** — a new standalone service plus a security-critical SDK: connection model + migration (S), transports http/sse (M), client + session pool (M), six security guards + audit + normalization (M), internal gateway API + OAuth/RFC 8707 flow (M), api CRUD + UI (S), tests (M).

Key risks:
- **MCP spec churn / 2026 RC timing.** The stateless-HTTP 2026 RC final ships **2026-07-28** — after today (2026-06-26); only the RC is locked. Mitigation: target the locked RC (`StreamableHttpTransport`) plus legacy `sse`, isolate protocol differences behind `MCPTransport`, pin the MCP Python SDK version. (Medium)
- **OAuth + RFC 8707 correctness.** Audience binding, PKCE, refresh, and per-server token isolation are easy to get subtly wrong. Mitigation: keep token-binding logic in one pure, unit-tested module (`token_binding.py`); assert the exact outgoing payloads; store only audience-scoped tokens. (Medium)
- **Read-only enforcement gaps via under-annotated servers.** Many MCP servers omit `read_only_hint`/`destructive_hint`, so naive allow-by-default would permit writes. Mitigation: treat unannotated tools as mutating unless explicitly allowlisted (`query_tool`/read allowlist); default-deny. (Medium → mitigated to Low)
- **Session lifecycle under concurrency.** Pooled sessions, reconnects, and per-connection serialization can race. Mitigation: `SessionManager` with per-connection async locks + TTL; explicit reconnect test. (Medium)
- **Latency added to retrieval.** Live query-through is on the hot retrieval path. Mitigation: timeouts, `limit` caps, and F05 treats MCP as one optional leg — its absence/timeout degrades gracefully without failing the hybrid query. (Low)
- **Redaction false-negatives.** Mitigation: shared, well-tested patterns + entropy heuristic; redaction applied at the single gateway egress point. (Low)

## 10. Key files / paths (exact)

- `apps/api/alembic/versions/0009_mcp_gateway.py` — `mcp_connections` + `mcp_audit_log` + immutability trigger.
- `packages/mcp-sdk/pyproject.toml` — uv workspace member.
- `packages/mcp-sdk/src/mcp_sdk/models.py` — Pydantic models + enums.
- `packages/mcp-sdk/src/mcp_sdk/protocols.py` — `MCPTransport`, `MCPClient`, `CredentialResolver`, `AuditSink`, `SecretRedactor`, `ToolPolicy`.
- `packages/mcp-sdk/src/mcp_sdk/client.py` — `MCPClientSession`.
- `packages/mcp-sdk/src/mcp_sdk/pool.py` — `SessionManager`.
- `packages/mcp-sdk/src/mcp_sdk/gateway_client.py` — `MCPGatewayClient`.
- `packages/mcp-sdk/src/mcp_sdk/transports/{http.py,sse.py}`.
- `packages/mcp-sdk/src/mcp_sdk/security/{write_guard.py,namespace.py,validation.py,token_binding.py,redaction.py}`.
- `packages/mcp-sdk/src/mcp_sdk/{audit.py,normalize.py,errors.py}`.
- `packages/mcp-sdk/tests/` — unit tests + `FakeMCPServer`/`FakeTransport` fixtures.
- `apps/mcp-gateway/pyproject.toml`, `apps/mcp-gateway/app/main.py`, `app/config.py`, `app/auth.py`, `app/registry.py`, `app/credentials.py`, `app/audit.py`, `app/deps.py`.
- `apps/mcp-gateway/app/routes/{health.py,connections.py,resources.py,tools.py,query.py}`.
- `apps/mcp-gateway/tests/` — gateway integration tests.
- `apps/api/app/api/v1/mcp.py` — user-facing router.
- `apps/api/app/services/mcp_service.py` — persistence + vault + gateway wiring + OAuth orchestration.
- `apps/api/app/schemas/mcp.py` — request/response schemas.
- `apps/api/app/models/mcp.py` — SQLAlchemy ORM (`MCPConnection`, `MCPAuditLog`).
- `apps/api/tests/mcp/` — API + migration + immutability tests.
- `apps/worker/tasks/mcp.py` — `mcp.health_probe_all` beat task.
- `apps/web/app/(app)/settings/mcp/page.tsx`, `apps/web/components/mcp/{connection-form.tsx,connection-card.tsx,audit-table.tsx}`, `apps/web/lib/api/mcp.ts`.
- `examples/mcp-connectors/confluence-engineering.yaml` — example connector template with security notes.
- `deploy/docker-compose.yml` (flesh out `mcp-gateway`, network segmentation), `deploy/.env.example`, `deploy/.env.production.example`.

## 11. Research references (relevant links from the spec/research report)

- MCP specification (2025-11-25) — resources/tools/prompts, JSON-RPC 2.0, capability negotiation: https://modelcontextprotocol.io/specification/2025-11-25
- MCP 2026 RC (stateless HTTP, ships 2026-07-28) — the `http` transport target: https://blog.modelcontextprotocol.io/posts/2026-07-28-release-candidate/
- MCP June 2025 update (OAuth 2.0 Resource Server, RFC 8707 token binding, PKCE, structured output): https://forgecode.dev/blog/mcp-spec-updates/
- MCP security advisory (DoD 2026) — middleware layer, least-privilege, input validation: https://media.defense.gov/2026/Jun/02/2003943289/-1/-1/0/CSI_MCP_SECURITY.PDF
- MCP knowledge-retrieval servers (query-through targets): https://mcpservers.org/topics/knowledge-retrieval-mcp
- MCP Python SDK / org: https://github.com/modelcontextprotocol
- Spec sections: `docs/FORGE_SPEC.md` → "MCP Integration" (connection schema, 7 security rules), "Knowledge Sync Modes" (MCP query-through), Core Data Model (`MCPConnection[]`), Technology Stack ("MCP Python SDK + dedicated gateway service"), Monorepo (`apps/mcp-gateway`, `packages/mcp-sdk`), Security table (MCP read-only/token-binding/namespace), Phase 1 roadmap ("MCP gateway V1: read-only query-through mode").
- Research: `docs/forge-research-report.md` → "Model Context Protocol" (protocol shape, June 2025 + 2026 RC changes, security considerations).

## 12. Out of scope / future

- **MCP sync-and-index mode** (`index_strategy=sync_and_index`) — periodic pull of MCP resources into the local pgvector/BM25 index. Phase 2 roadmap; V1 is `query_through` only.
- **Write / mutating MCP tool calls** — enabling `allow_write=true` to actually execute mutations (with admin gate + policy-override approval). V1 hard-blocks all writes; write execution is V2+.
- **`stdio` transport** — running subprocess MCP servers inside the gateway requires process sandboxing/lifecycle controls; deferred to V2 alongside Docker sandboxing. The column/enum exist; registration is rejected in V1.
- **MCP `prompts` capability** — V1 negotiates and reports it but does not consume server prompts.
- **Server-side elicitation** (June 2025 spec) — gateway returns elicitation as a denied/unsupported result in V1; interactive elicitation is future.
- **Per-connection rate limiting & quotas** beyond timeouts/size caps — folded into the platform-wide rate-limiting slice.
- **MCP connector marketplace** (community-contributed connectors) — Phase 3 roadmap; V1 ships example templates only.
- **Approval UI provenance panel** consuming `mcp://` `source_uri` — built in `cross-cutting/F36-human-approval-system` (`KnowledgeProvenancePanel`); F09 only guarantees the field via `v1/F05-hybrid-knowledge-retrieval`.
