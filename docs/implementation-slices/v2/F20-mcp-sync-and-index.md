# F20 — MCP Sync-and-Index Mode

> Phase: v2 · Spec module(s): Knowledge Service (`packages/knowledge-core`), MCP Connector Layer (`packages/mcp-sdk`, `apps/mcp-gateway`), `apps/worker` (Celery indexer/syncer), Observability (retrieval debug, MCP freshness lag) · Status target: A workspace admin can flip an existing MCP connection from `query_through` to `index_strategy=sync_and_index`; Forge auto-provisions a linked `mcp_resource` KnowledgeSource, and a periodic worker pulls the connection's namespace-scoped MCP resources through the read-only gateway, chunks/redacts/embeds them, and **upserts them into the local `retrieval_chunks` (pgvector + BM25) index** with full `mcp://{slug}/{uri}` provenance. Subsequent task/ad-hoc retrieval for that source is served **entirely from the local index with zero live MCP calls**, giving fast hybrid search over external data. Incremental re-sync only re-indexes changed resources (server change-token, else content hash), tombstones removed resources, respects `freshness_sla_minutes`, and writes one `knowledge_sync_runs` row per run while inheriting every F09 MCP security guarantee (read-only, namespace scoping, redaction, per-call audit).

---

## 1. Intent — what & why

The spec defines five Knowledge Sync Modes (`docs/FORGE_SPEC.md` → "Knowledge Sync Modes"). F09 shipped **MCP query-through** in V1 ("Call MCP server live at retrieval time | Always-fresh external data"). F20 is its V2 counterpart, **MCP sync-and-index**: "Periodically pull MCP resources into the local index | Fast hybrid search" — listed explicitly as a Phase 2 roadmap item ("MCP sync-and-index mode") and driven by the connection's `index_strategy: sync_and_index` field (`docs/FORGE_SPEC.md` → "MCP Connection Schema").

The trade-off is freshness-vs-latency. Query-through (F09) is always-fresh but pays the MCP round-trip and the server's own latency on the hot retrieval path for every query, and it cannot benefit from the full hybrid pipeline (semantic + BM25 + RRF + rerank) because the candidates only exist transiently. Sync-and-index inverts that: pay the cost once, periodically, in the background; persist normalized chunks into the same `retrieval_chunks` table F05 already searches; then every retrieval is a fast local hybrid query with reranking, ranked side-by-side with repo/spec/doc chunks. Both F09's slice and F05's slice name this as the deferred Phase-2 work in their own §12 "Out of scope".

F20 deliberately **composes the two existing slices rather than reinventing them**:

- It reuses F09's `MCPGatewayClient` + the read-only gateway for resource `list`/`read` (so all seven MCP Security Rules — read-only default, RFC 8707 token binding, input validation, audit, namespace scoping, redaction, policy parity — are inherited unchanged).
- It reuses F05's `knowledge_sources` / `retrieval_chunks` / `knowledge_sync_runs` schema, `Chunker`/`EmbeddingProvider` protocols, `Indexer` (chunk→redact→embed→upsert), `redact_secrets`, `CHUNK_TYPE_WEIGHTS` (MCP weight 1.0), and the whole hybrid retrieval pipeline at query time.

The genuinely **new** work is: (1) an `mcp_indexed_resources` ledger table for per-resource change detection + tombstoning; (2) an MCP-resource chunking strategy keyed on mime-type; (3) the `McpSyncIndexer` pipeline + Celery sync/beat tasks; (4) auto-provisioning the linked KnowledgeSource when `index_strategy` flips; and (5) a one-line-but-load-bearing routing change in F05's retriever so `sync_and_index` sources are served from the local index and are NOT also queried live.

## 2. User-facing behavior / journeys

- **Journey A — Switch a connection to indexed mode (admin).** An admin opens Settings → MCP → a `connected` connection (e.g. `confluence-engineering`) and changes "Retrieval mode" from "Query-through (live)" to "Sync & index (fast)". On save, Forge sets `mcp_connections.index_strategy = sync_and_index`, auto-provisions a linked `KnowledgeSource` (`source_type=mcp_resource`, `sync_mode=mcp_sync_and_index`, `config.mcp_connection_id=<slug>`, `allowed_namespaces`, `freshness_sla_minutes` copied from the connection), and enqueues a full sync. The connection card now shows an "Index" panel: status `indexing → ready`, resource count, chunk count, last synced, and a freshness badge.
- **Journey B — Background freshness.** Every poll interval the beat task finds the indexed source is older than `freshness_sla_minutes` and enqueues an incremental sync. The sync lists the server's resources, skips those whose change-token/hash is unchanged, re-indexes changed ones, tombstones resources the server no longer returns, and bumps `last_synced_at`. The admin sees the resource/chunk counts and "last synced" update without any manual action.
- **Journey C — Fast retrieval during a run.** A `Task` whose `knowledge_scope.mcp_sources` includes `confluence-engineering` enters `executing`. F05's retriever resolves that slug to the provisioned local source and includes its chunks in the pgvector + BM25 + RRF + rerank pipeline — **no live MCP call is made**. The agent gets reranked external context as fast as repo context, and the Approval UI later shows `mcp://confluence-engineering/...` provenance identical to query-through.
- **Journey D — Manual reindex.** The admin clicks "Reindex now" on the connection (or hits the API). A full sync runs, re-enumerating all resources and reconciling the index against the ledger.
- **Journey E — Switch back / disconnect.** The admin switches the connection back to "Query-through", or deletes the connection. Forge disables the linked source and purges its `retrieval_chunks` + `mcp_indexed_resources` rows (so stale external content cannot leak into retrieval), and retrieval reverts to live query-through (or the source disappears).
- **Journey F — Observability.** A maintainer inspects the source's sync history (`knowledge_sync_runs`: resources seen/indexed/skipped/deleted, duration, errors) and the per-resource ledger; "MCP freshness lag" (spec → Observability "Retrieval quality") is computed as `now - last_synced_at` per indexed source.

## 3. Vertical slice

### 3.1 Data model (tables/columns/migrations touched)

Single Alembic migration `apps/api/alembic/versions/0021_mcp_sync_and_index.py`, revision id `mcp_sync_and_index` (the `0021` ordinal is illustrative for ordering only; set `down_revision` to the actual Alembic head at integration time, matching the convention F09 uses for its `0009` ordinal). Its `down_revision` is the current migration head and **must have both F05's `0005_knowledge_retrieval` and F09's `0009_mcp_gateway` in its ancestry** (the migration touches `knowledge_sources`/`retrieval_chunks` and `mcp_connections`). It needs no new Postgres extension (`vector` already enabled by 0005).

**Reused, unchanged:** `knowledge_sources`, `retrieval_chunks`, `knowledge_sync_runs` (F05) and `mcp_connections`, `mcp_audit_log` (F09). The `mcp_connections.index_strategy` column (enum `query_through | sync_and_index`) and `sync_mode`/`freshness_sla_minutes` columns already exist from F09 — F20 finally **honors** `sync_and_index`. `knowledge_sources.sync_mode` already accepts `mcp_sync_and_index` and `retrieval_chunks.chunk_type` already accepts `mcp_resource` (weight 1.0) from F05 — F20 finally produces persisted rows of that type.

**New table `mcp_indexed_resources`** — the per-resource sync ledger enabling incremental diffing, tombstoning, and per-resource provenance. One row per `(source_id, resource_uri)`.

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid` PK | `gen_random_uuid()` |
| `workspace_id` | `uuid` NOT NULL FK → `workspaces.id` ON DELETE CASCADE | tenant key, denormalized for fast filtering |
| `source_id` | `uuid` NOT NULL FK → `knowledge_sources.id` ON DELETE CASCADE | the provisioned `mcp_resource` source |
| `connection_id` | `uuid` NOT NULL FK → `mcp_connections.id` ON DELETE CASCADE | source MCP connection |
| `connection_slug` | `text` NOT NULL | snapshot of slug (provenance stable after rename) |
| `resource_uri` | `text` NOT NULL | the MCP resource `uri` (server-side id) |
| `namespace` | `text` NULL | namespace this resource belongs to (within `allowed_namespaces`) |
| `title` | `text` NULL | resource display name / title |
| `mime_type` | `text` NULL | drives chunking-strategy selection |
| `change_token` | `text` NULL | server-reported version (etag / `lastModified` / revision from `McpResource.annotations`); NULL when server gives none |
| `content_hash` | `text` NOT NULL | sha256 of the redacted resource text — fallback change detector when `change_token` is NULL |
| `byte_size` | `integer` NOT NULL DEFAULT 0 | post-redaction text size |
| `chunk_count` | `integer` NOT NULL DEFAULT 0 | chunks currently persisted for this resource |
| `last_seen_sync_run_id` | `uuid` NULL FK → `knowledge_sync_runs.id` ON DELETE SET NULL | set every run a resource is enumerated; rows not bearing the current run id are tombstoned |
| `last_indexed_at` | `timestamptz` NULL | last time chunks were (re)written |
| `deleted_at` | `timestamptz` NULL | soft-tombstone marker; set when the server stops returning the resource, before chunk purge |
| `created_at` / `updated_at` | `timestamptz` NOT NULL DEFAULT `now()` | |

Constraints/indexes:
- `CREATE UNIQUE INDEX ux_mcp_idx_resource ON mcp_indexed_resources (source_id, resource_uri);` — upsert target.
- `CREATE INDEX ix_mcp_idx_tenant_source ON mcp_indexed_resources (workspace_id, source_id);`
- `CREATE INDEX ix_mcp_idx_seen ON mcp_indexed_resources (source_id, last_seen_sync_run_id);` — tombstone sweep.
- `CHECK (deleted_at IS NULL OR chunk_count = 0)` — a tombstoned resource must have had its chunks purged.

**Chunk linkage:** every `retrieval_chunks` row produced by F20 stores `metadata->>'resource_uri'`, `metadata->>'mcp_namespace'`, `metadata->>'connection_slug'`, optional `metadata->>'url'`/`title`, and `source_uri = mcp://{slug}/{uri}` (F05's `source_uri` column). Purge-by-resource is `DELETE FROM retrieval_chunks WHERE source_id = :sid AND metadata->>'resource_uri' = :uri`. The existing F05 dedup index `ux_chunks_dedup (source_id, (metadata->>'file_path'), chunk_index)` is reused by storing the `resource_uri` in `metadata.file_path` for MCP chunks (so the same upsert path and unique constraint apply with no schema change), and `metadata.resource_uri` mirrors it for clarity.

`alembic downgrade` drops `mcp_indexed_resources` and its indexes; it does not touch F05/F09 tables.

### 3.2 Backend (FastAPI routes + services/packages)

Most logic lives in `packages/knowledge-core` (the indexer pipeline) and `apps/worker` (scheduling); `apps/api` adds thin MCP-index control endpoints and the auto-provisioning service.

**`packages/knowledge-core` additions:**

```
packages/knowledge-core/src/knowledge_core/
├── chunking/
│   └── mcp.py            # McpResourceChunker: mime-type -> chunk strategy, emits ChunkType.mcp_resource
└── sync/
    └── mcp_indexer.py    # McpSyncIndexer: list -> read -> snapshot -> chunk -> redact -> embed -> upsert + ledger
```

`McpSyncIndexer.sync()` pipeline (full or incremental):
1. Resolve the source + its `mcp_connection_id` slug + `allowed_namespaces`; open a `knowledge_sync_runs` row (`status=running`, `mode=full|incremental`).
2. Enumerate resources via `McpResourceFetcher.list_resources(namespaces=..., cursor=...)` (cursor-paginated; the fetcher is the gateway adapter, so namespace scoping + audit happen in F09's gateway).
3. For each `ResourceRef`: look up the ledger row. **Skip** (no read, no embed) when incremental AND the server `change_token` is present AND unchanged. When no `change_token`, fall through to read + content-hash compare and skip if hash unchanged.
4. For changed/new resources: `read_resource(uri)` → `McpResourceSnapshot` (already redacted + size-capped by the gateway) → `McpResourceChunker.chunk()` → `Indexer` upsert into `retrieval_chunks` (chunk_type `mcp_resource`, weight 1.0). Run `redact_secrets` again defensively before persist (idempotent). Update/insert the ledger row (`change_token`, `content_hash`, `chunk_count`, `last_indexed_at`, `byte_size`).
5. Mark every enumerated resource's ledger row with `last_seen_sync_run_id = <this run>`.
6. **Tombstone sweep:** any non-deleted ledger row for this source whose `last_seen_sync_run_id != <this run>` → `DELETE` its chunks, set `chunk_count=0`, `deleted_at=now()`. (Incremental over a cursor full-enumeration; if a partial/aborted enumeration is detected — non-exhausted cursor — the sweep is skipped to avoid false deletions, recorded as `sweep_skipped` on the run.)
7. Close the run (`succeeded`/`failed`, counters `resources_seen/indexed/skipped/deleted`, `chunks_indexed/deleted/skipped`), set source `status=ready|error`, `last_synced_at=now()`.

Idempotency/concurrency: per-`source_id` Redis lock (reuse F05's lock helper) serializes syncs; embedding calls batched (default 64) with bounded retry; the whole pipeline is restart-safe because the ledger + dedup upsert make re-runs converge.

**`McpResourceChunker`** selects a strategy by `mime_type`:
- `text/markdown`, `text/x-markdown` → F05's markdown splitter (headings + paragraphs).
- `text/html` → HTML→text (strip tags) then markdown splitter.
- `text/plain`, unknown text → paragraph/line-window splitter.
- `application/json` → pretty-print + recursive key-path windows.
- non-text (`blob`/binary) → **skipped** (logged, ledger row recorded with `chunk_count=0`; binary indexing is §12 future).
All emitted `Chunk`s have `chunk_type=ChunkType.mcp_resource`, `weight=1.0`, and `metadata` populated with `resource_uri`, `mcp_namespace`, `connection_slug`, `title`, `url`, `mime_type`, `file_path=<resource_uri>` (for dedup), `chunk_index`.

**`apps/api` additions — extend the F09 router `apps/api/app/api/v1/mcp.py`:**

| Method | Path | RBAC | Purpose |
|---|---|---|---|
| `PATCH` | `/connections/{id}` | admin | **F20 lifts F09's restriction** so `index_strategy` may be set to `sync_and_index` (or back to `query_through`). Switching to `sync_and_index` auto-provisions the linked source + enqueues a full sync; switching away disables the source + purges its index. `allow_write` is still immutable-false. |
| `POST` | `/connections/{id}/index/reindex` | admin | Enqueue a full re-sync of the connection's indexed source. 409 if `index_strategy != sync_and_index`. |
| `GET` | `/connections/{id}/index` | member | Index status: `{source_id, status, resource_count, chunk_count, last_synced_at, freshness_sla_minutes, stale, last_sync_run}`. |

New service `apps/api/app/services/mcp_index_service.py`:

```python
async def ensure_indexed_source(connection_id: UUID, *, workspace_id: UUID) -> KnowledgeSource:
    """Idempotently create/update the mcp_resource KnowledgeSource linked to this connection
    (config.mcp_connection_id=slug, allowed_namespaces, sync_mode=mcp_sync_and_index,
    freshness_sla_minutes from connection). Returns the source."""

async def teardown_indexed_source(connection_id: UUID, *, workspace_id: UUID, purge: bool = True) -> None:
    """Disable the linked source; if purge, delete its retrieval_chunks + mcp_indexed_resources."""

async def index_status(connection_id: UUID, *, workspace_id: UUID) -> McpIndexStatus: ...
```

The existing F05 endpoints (`POST /api/v1/knowledge/sources/{id}/sync`, `GET .../sync/{run_id}`) remain the generic mechanism; the MCP endpoints above are admin-facing convenience wrappers that resolve connection→source and delegate.

### 3.3 Worker / agent runtime (Celery tasks, LangGraph, if any)

New Celery tasks in `apps/worker/tasks/knowledge_mcp.py` (queue `knowledge`, Celery app `forge_worker.app`):

- `knowledge.mcp_full_sync(source_id: str) -> dict` — full enumerate + reconcile via `McpSyncIndexer.sync(mode="full")`.
- `knowledge.mcp_incremental_sync(source_id: str) -> dict` — `McpSyncIndexer.sync(mode="incremental")`; skips unchanged resources by change-token/hash, still does the tombstone sweep on a complete enumeration.
- `knowledge.refresh_stale_mcp_sources() -> dict` — Celery **beat** (every `MCP_INDEX_POLL_SECONDS`, default 300). Scans `knowledge_sources` where `source_type=mcp_resource AND sync_mode=mcp_sync_and_index AND status=ready` whose `last_synced_at` is older than `freshness_sla_minutes`, and enqueues `mcp_incremental_sync`. Isolated per-source failures do not fail the batch. This mirrors F05's `knowledge.refresh_stale_sources` and may be merged into it.

The `McpResourceFetcher` implementation `apps/worker/adapters/gateway_fetcher.py` wraps F09's `MCPGatewayClient`, binding `workspace_id`, `connection_id`, and `actor="system"` (or `agent_run:<id>` when triggered by a run), so every list/read goes through the gateway's read-only guard, namespace scope, redaction, and audit. No new MCP transport code — F20 never speaks MCP directly.

No LangGraph in this slice. (V2 note: the periodic schedule can later be expressed as a Temporal cron workflow once the Temporal slice (`v2/F25-temporal-integration`) lands; the indexer body is engine-agnostic and is invoked identically from a Celery task or a Temporal activity. Celery is the V2-default per the spec's "Background jobs: Temporal activities (V2)" being optional behind a compose profile — the Temporal compose-profile stub lives in `v1/F14-docker-compose-selfhost`.)

**Load-bearing change to F05 retrieval (`packages/knowledge-core/src/knowledge_core/service.py`):** `HybridRetrievalService.retrieve()` step (3) currently calls the gateway live for "MCP query-through sources". F20 changes the branch to route MCP sources by `index_strategy`:
- `query_through` → live gateway call (unchanged, F09 behavior).
- `sync_and_index` → **no live call**; the source's persisted chunks are already included in the pgvector + BM25 legs via `source_ids` (the slug resolves to the provisioned local `knowledge_sources.id`). This is what makes indexed retrieval fast and is asserted by AC8.

### 3.4 Frontend / UI (Next.js routes/components, if any)

- `apps/web/components/mcp/connection-index-panel.tsx` (new) — rendered inside F09's `connection-card.tsx`: a "Retrieval mode" select (`Query-through (live)` | `Sync & index (fast)`), and when indexed: status badge, resource count, chunk count, last-synced relative time, freshness badge (green/amber/red vs SLA), and a "Reindex now" button. Admin-only mutations; members read-only. TanStack Query against `GET /api/v1/mcp/connections/{id}/index`, mutations against `PATCH /connections/{id}` and `POST /connections/{id}/index/reindex`.
- `apps/web/lib/api/mcp.ts` (extend F09 client) — add `getIndexStatus`, `setIndexStrategy`, `reindex`.
- `apps/web/app/(app)/knowledge/page.tsx` (extend F05 list) — `mcp_resource` sources surface in the Knowledge Sources list with a small "MCP-indexed" tag and the connection link; the "Sync now" action already exists generically.

No new top-level route; F20 enriches the existing F09 MCP settings card and the F05 knowledge sources list.

### 3.5 Infra / deploy (compose, helm, caddy, if any)

No new services. F20 reuses the existing `worker` (Celery, on `backend`/`data`/`mcp` networks per F14), `mcp-gateway`, `db` (pgvector), and `reranker` (F05). New env vars (append to `deploy/.env.example` + `.env.production.example`, consumed by `worker` and `api`):

| Var | Default | Purpose |
|---|---|---|
| `MCP_INDEX_POLL_SECONDS` | `300` | beat interval for `refresh_stale_mcp_sources`. |
| `MCP_INDEX_PAGE_SIZE` | `100` | `resources/list` page size per cursor call. |
| `MCP_INDEX_MAX_RESOURCES` | `5000` | safety cap on resources indexed per source per run. |
| `MCP_INDEX_EMBED_BATCH` | `64` | embedding batch size (shared with F05). |
| `MCP_INDEX_CONCURRENCY` | `4` | max concurrent resource reads within one sync. |
| `MCP_INDEX_DELETE_ON_DISABLE` | `true` | purge chunks when switching away from `sync_and_index`. |

Caddy: unchanged (gateway stays internal; all MCP traffic is worker→gateway→server on the `mcp`/`mcp-egress` networks from F09). Helm: the V2 Helm chart slice picks up the same env vars on the worker/api deployments; nothing F20-specific beyond config keys.

## 4. Public interfaces / contracts (exact signatures, Pydantic models, Protocols, YAML schemas)

`packages/knowledge-core/src/knowledge_core/sync/mcp_indexer.py`:

```python
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID
from pydantic import BaseModel, Field
from mcp_sdk.models import McpResourceSnapshot           # F09
from knowledge_core.models import Chunk                   # F05

class SyncDirection(StrEnum):
    full = "full"; incremental = "incremental"

class ResourceRef(BaseModel):
    uri: str
    namespace: str | None = None
    title: str | None = None
    mime_type: str | None = None
    change_token: str | None = None          # etag / lastModified / revision from McpResource.annotations
    url: str | None = None

class McpResourceFetcher(Protocol):
    """Read-only adapter over the F09 MCP gateway. All calls are namespace-scoped,
    redacted, and audited by the gateway; F20 never speaks MCP directly."""
    async def list_resources(self, *, namespaces: list[str] | None,
                             cursor: str | None) -> tuple[list[ResourceRef], str | None]: ...
    async def read_resource(self, uri: str) -> McpResourceSnapshot: ...

class McpResourceChunker(Protocol):
    def chunk(self, snapshot: McpResourceSnapshot) -> list[Chunk]: ...   # all Chunk.chunk_type == mcp_resource

class SyncReport(BaseModel):
    source_id: UUID
    sync_run_id: UUID
    mode: SyncDirection
    resources_seen: int = 0
    resources_indexed: int = 0
    resources_skipped: int = 0
    resources_deleted: int = 0
    chunks_indexed: int = 0
    chunks_deleted: int = 0
    sweep_skipped: bool = False
    started_at: datetime
    finished_at: datetime | None = None
    error: str | None = None

class McpSyncIndexer:
    def __init__(self, *, fetcher: McpResourceFetcher, chunker: McpResourceChunker,
                 indexer: "Indexer", ledger: "ResourceLedger",
                 page_size: int = 100, max_resources: int = 5000,
                 concurrency: int = 4) -> None: ...
    async def sync(self, *, source_id: UUID, workspace_id: UUID,
                   connection_id: UUID, connection_slug: str,
                   allowed_namespaces: list[str], mode: SyncDirection) -> SyncReport: ...
```

`ResourceLedger` protocol (persistence boundary over `mcp_indexed_resources`, implemented in `apps/api`/`apps/worker` against SQLAlchemy; fakeable in tests):

```python
class LedgerRow(BaseModel):
    source_id: UUID; resource_uri: str
    namespace: str | None; title: str | None; mime_type: str | None
    change_token: str | None; content_hash: str
    chunk_count: int; byte_size: int
    last_indexed_at: datetime | None; deleted_at: datetime | None

class ResourceLedger(Protocol):
    async def get(self, source_id: UUID, resource_uri: str) -> LedgerRow | None: ...
    async def upsert(self, row: LedgerRow, *, sync_run_id: UUID) -> None: ...      # sets last_seen_sync_run_id
    async def mark_seen(self, source_id: UUID, resource_uri: str, *, sync_run_id: UUID) -> None: ...
    async def tombstone_unseen(self, source_id: UUID, *, sync_run_id: UUID) -> list[str]: ...  # returns purged uris
    async def purge_source(self, source_id: UUID) -> int: ...
```

`apps/api/app/schemas/mcp.py` (extend F09 schemas):

```python
from enum import StrEnum
from uuid import UUID
from datetime import datetime
from pydantic import BaseModel, Field
from mcp_sdk.models import IndexStrategy                  # F09: query_through | sync_and_index

class UpdateConnectionRequest(BaseModel):                 # F20 extends F09's version
    name: str | None = None
    allowed_namespaces: list[str] | None = None
    query_tool: str | None = None
    status: str | None = None
    index_strategy: IndexStrategy | None = None           # NEW in F20; allow_write still absent (immutable false)

class McpIndexStatus(BaseModel):
    source_id: UUID | None
    index_strategy: IndexStrategy
    status: str                                           # pending | indexing | ready | error | disabled
    resource_count: int = 0
    chunk_count: int = 0
    last_synced_at: datetime | None = None
    freshness_sla_minutes: int = 30
    stale: bool = False
    last_sync_run: dict | None = None                     # mirror of knowledge_sync_runs row
```

YAML — the spec's `mcp_connection` schema, now with `index_strategy: sync_and_index` honored end-to-end (shipped as an `examples/mcp-connectors/` indexed template):

```yaml
mcp_connection:
  id: confluence-engineering
  name: Engineering Confluence
  transport: http
  endpoint: https://mcp.company.internal/confluence
  auth: { type: oauth, resource_audience: https://mcp.company.internal/confluence }  # RFC 8707 (F09)
  capabilities: { resources: true, tools: true, prompts: false }
  sync_mode: incremental                 # advisory: drives incremental vs full poll behavior
  index_strategy: sync_and_index         # F20: pull resources into the local index
  freshness_sla_minutes: 30              # poll/refresh target for the background indexer
  allow_write: false                     # inherited read-only guarantee (F09)
  allowed_namespaces: [engineering, architecture]
```

Linked `KnowledgeSourceConfig` (F05) auto-provisioned by `ensure_indexed_source`, persisted in `knowledge_sources.config`:

```yaml
# source_type: mcp_resource
mcp_connection_id: confluence-engineering
index_strategy: sync_and_index
allowed_namespaces: [engineering, architecture]
```

## 5. Dependencies — features/slices that must exist first

IDs reference `<phase>/<id>-<slug>` paths. The numbered v1/v2 slugs below are authoritative and match files in `docs/implementation-slices/`. The platform-foundation and auth/secrets concerns are cross-cutting Phase-0 prerequisites that every slice assumes and that do not yet have a dedicated numbered feature file, so their slugs are placeholders to reconcile against the final foundation slice id (sibling slices reference them variously — e.g. foundation as `cross-cutting/F00-platform-foundation` (F05) or `v1/F00-foundation-substrate` (F09); auth/secrets as `cross-cutting/F37-auth-secrets-byok` (F09) or `cross-cutting/F00-auth-secrets` (F05)).

- **v1/F09-mcp-gateway-v1** (REQUIRED) — supplies `MCPGatewayClient`, the read-only gateway, `McpResourceSnapshot`, `McpResource.annotations` (change-token source), `IndexStrategy` enum, `mcp_connections` table (incl. `index_strategy`/`sync_mode`/`freshness_sla_minutes`), `mcp_audit_log`, the `connection-card.tsx`/`UpdateConnectionRequest` surfaces F20 extends, the `MCP_MAX_RESOURCE_BYTES` egress size cap, and the per-call audit/redaction/namespace guarantees F20 inherits. F09's `UpdateConnectionRequest` does not carry `index_strategy` (post-create it is effectively immutable in V1); F20 adds the field so PATCH can flip it.
- **v1/F05-hybrid-knowledge-retrieval** (REQUIRED) — supplies `knowledge_sources`/`retrieval_chunks`/`knowledge_sync_runs`, the `Chunk` model + `Chunker`/`EmbeddingProvider` protocols, the `Indexer` (`sync/indexer.py`, chunk→redact→embed→upsert) + `redact_secrets`, `ChunkType.mcp_resource` + `CHUNK_TYPE_WEIGHTS`, the `ux_chunks_dedup` upsert index, the markdown splitter reused by `McpResourceChunker`, the per-source Redis sync lock, the `knowledge.refresh_stale_sources` beat F20 mirrors, and the `HybridRetrievalService.retrieve()` step-3 MCP branch F20 modifies. F20 is meaningless without F05's index.
- **cross-cutting/F37-auth-secrets-byok** (REQUIRED, transitive) — workspaces, the four RBAC roles (admin to mutate, member to read, agent-runner consumes via `/retrieve`), and the encrypted per-workspace vault that resolves embedding/MCP credentials. Reached via F05/F09. (Slug is a placeholder per the note above.)
- **cross-cutting/F00-platform-foundation** (REQUIRED, transitive) — uv workspace, async SQLAlchemy 2.x session, the shared `Base`/Alembic env + baseline `0001_*`, the Celery app (`forge_worker.app`), and the `worker` compose service. (Slug is a placeholder per the note above.)
- **v1/F04-repo-policy** (SOFT) — `PolicyEvaluator` backing `mcp-sdk`'s `ToolPolicy` so MCP reads get the same policy evaluation as any agent tool (spec MCP rule 7). Until present, F09's gateway enforces a built-in read-only + namespace floor that F20 inherits unchanged.
- **v2/F25-temporal-integration** (SOFT, future) — if/when Temporal lands, the periodic schedule may move to a Temporal cron workflow; the indexer body is engine-agnostic and unchanged. Not required for F20 on Celery.

## 6. Acceptance criteria (numbered, testable)

1. Migration `0021_mcp_sync_and_index` creates `mcp_indexed_resources` with all columns, the `ux_mcp_idx_resource` unique index, the tenant/seen indexes, and the `CHECK (deleted_at IS NULL OR chunk_count = 0)` constraint; `alembic downgrade` drops them and leaves F05/F09 tables intact.
2. **Auto-provision on switch.** `PATCH /connections/{id}` with `index_strategy=sync_and_index` sets the connection field, creates exactly one linked `knowledge_sources` row (`source_type=mcp_resource`, `sync_mode=mcp_sync_and_index`, `config.mcp_connection_id=<slug>`, `allowed_namespaces` + `freshness_sla_minutes` copied from the connection), and enqueues `knowledge.mcp_full_sync`. Re-issuing the same PATCH is idempotent (no duplicate source).
3. **Full sync persists chunks.** After `mcp_full_sync` over a server with N text resources, `retrieval_chunks` contains rows with `chunk_type='mcp_resource'`, `weight=1.0`, `source_uri='mcp://{slug}/{uri}'`, and `metadata` carrying `resource_uri`, `mcp_namespace`, `connection_slug`; `mcp_indexed_resources` has one non-deleted row per resource with `chunk_count>0`; a `knowledge_sync_runs` row records `succeeded` with `resources_seen=N`.
4. **Namespace scoping (inherited).** Resources outside `allowed_namespaces` are never enumerated/read (the gateway scopes `resources/list`); no chunk or ledger row exists for an out-of-namespace resource, and the gateway audit log shows the scoped list call.
5. **Incremental skip by change-token.** Given a server that reports stable `change_token`s, a second `mcp_incremental_sync` with no upstream changes reads zero resource bodies (assert `read_resource` not called for unchanged uris), `resources_skipped=N`, `resources_indexed=0`, and chunk rows are byte-identical (no churn).
6. **Incremental skip by content-hash fallback.** Given a server that reports NO `change_token`, an incremental sync reads each resource but skips re-chunk/re-embed when `content_hash` is unchanged (`chunks_indexed` reflects only changed resources; embedding provider not called for unchanged ones).
7. **Change re-indexes only the changed resource.** Modifying one resource's body upstream, then `mcp_incremental_sync`, re-chunks/re-embeds only that resource (its old chunks replaced via the dedup upsert), `resources_indexed=1`, other resources untouched, `last_indexed_at` advanced only for it.
8. **Tombstone removed resources.** Removing a resource upstream, then a sync over a complete enumeration, deletes that resource's `retrieval_chunks`, sets its ledger row `deleted_at` + `chunk_count=0`, and reports `resources_deleted=1`; a sync whose cursor enumeration did not complete sets `sweep_skipped=true` and deletes nothing.
9. **Retrieval is index-served with ZERO live MCP calls.** With a `sync_and_index` source, `HybridRetrievalService.retrieve()` for a scope including that connection returns the indexed `mcp_resource` chunks through the pgvector + BM25 + RRF + rerank pipeline and makes **no** `MCPGatewayClient` call (assert the gateway client is never invoked during retrieval). A `query_through` source on the same path still triggers exactly one live call (regression guard for the F05 branch).
10. **Provenance parity.** An `mcp_resource` chunk retrieved via the index carries the same `source_uri` shape (`mcp://{slug}/{uri}`) and provenance metadata as F09 query-through, satisfying the Approval UI knowledge-provenance contract.
11. **Redaction.** A resource body containing a fake AWS key + PEM block is redacted before persistence; no secret substring appears in `retrieval_chunks.content`, `mcp_indexed_resources`, any `RetrievalResult`, or logs (defense-in-depth on top of the gateway's redaction).
12. **Freshness beat.** Back-dating a `sync_and_index` source's `last_synced_at` beyond `freshness_sla_minutes` causes `knowledge.refresh_stale_mcp_sources` to enqueue exactly one `mcp_incremental_sync` for it; a fresh source is not enqueued; an isolated per-source error does not abort the batch.
13. **Mime-type chunking.** `text/markdown` resources are split on headings; `text/html` is tag-stripped then split; `application/json` is structured-split; a binary (`blob`) resource is skipped with a ledger row `chunk_count=0` and no `retrieval_chunks` rows.
14. **Switch-away purge.** `PATCH index_strategy=query_through` (with `MCP_INDEX_DELETE_ON_DISABLE=true`) disables the linked source and deletes its `retrieval_chunks` + `mcp_indexed_resources`; subsequent retrieval for that connection reverts to live query-through.
15. **Tenant isolation.** Two workspaces indexing identical MCP content never cross-leak: retrieval for workspace A returns only A's chunks; ledger rows and chunks are filtered by `workspace_id`.
16. **RBAC + caps.** `PATCH index_strategy`, `reindex`, and switch-away require `admin`; `GET /index` requires `member`. A sync stops at `MCP_INDEX_MAX_RESOURCES`, records the cap hit on the run, and never streams unbounded resource bodies into memory.
17. **Read-only inheritance.** No code path in F20 ever issues a mutating MCP tool call; only `resources/list` + `resources/read` are used, and `allow_write` remains immutable-false on the connection.

## 7. Test plan (TDD) — concrete test cases (unit + integration), key fixtures

Write tests first; backend-tdd discipline (≥80% coverage per the spec's own profile). Tests in `packages/knowledge-core/tests/sync/`, `apps/worker/tests/`, and `apps/api/tests/mcp/`.

Key fixtures (compose F05 + F09 fixtures, add F20-specific):
- `FakeMcpResourceFetcher` — in-memory resource set with configurable `namespace`, `mime_type`, `change_token` (present or absent), and body. Supports mutating one resource, removing one, and a "partial enumeration" mode (raises mid-cursor) to exercise the sweep-skip path. Counts `read_resource` calls per uri so skip-behavior is assertable.
- `FakeResourceLedger` — in-memory `ResourceLedger`, captures upserts/tombstones for assertions; plus a real-`pg` ledger for SQL/constraint tests.
- `FakeEmbeddingProvider(dim=8)` — reused from F05 (deterministic, counts calls to assert no re-embed on skip).
- `pg` fixture — Postgres+pgvector via testcontainers, migrations through `0021` applied.
- `FakeMCPGatewayClient` — reused from F09 to assert the retrieval path makes zero/one live calls (AC9) and to back `GatewayMcpResourceFetcher`.
- A resource fixture whose body embeds a fake AWS key + PEM block (redaction).

Unit tests — `packages/knowledge-core/tests/sync/` (pure, no DB):
- `test_full_sync_indexes_all_text_resources` (AC3) — fetcher with N resources, assert chunks emitted + ledger upserts + report counters.
- `test_incremental_skips_unchanged_by_change_token` (AC5) — assert `read_resource` and embed both uncalled for unchanged uris.
- `test_incremental_skips_unchanged_by_content_hash_when_no_token` (AC6) — read called, embed/chunk skipped on equal hash.
- `test_change_reindexes_only_changed_resource` (AC7) — mutate one body, assert single re-index, dedup upsert replaces old chunks.
- `test_tombstone_removes_resource_chunks` and `test_sweep_skipped_on_partial_enumeration` (AC8).
- `test_mcp_chunker_markdown_html_json_and_skips_binary` (AC13).
- `test_indexer_redacts_secrets_before_persist` (AC11).
- `test_max_resources_cap_enforced` (AC16).

Integration tests — `apps/worker/tests/` + `apps/api/tests/mcp/` (with `pg` + fakes, Celery eager):
- `test_mcp_full_sync_persists_chunks_and_ledger` (AC3) — assert real `retrieval_chunks` + `mcp_indexed_resources` rows + `knowledge_sync_runs`.
- `test_namespace_scoped_enumeration` (AC4) — out-of-namespace resource never indexed.
- `test_retrieve_uses_index_no_live_mcp_call` and `test_query_through_still_calls_gateway` (AC9) — patch `FakeMCPGatewayClient`, assert call counts 0 vs 1 through `HybridRetrievalService.retrieve()`.
- `test_provenance_source_uri_and_metadata` (AC10).
- `test_refresh_stale_mcp_sources_enqueues_only_stale` (AC12) — back-date `last_synced_at`, run beat eager, assert enqueue; isolate a failing source.
- `test_switch_to_index_provisions_source_and_enqueues` and `test_switch_away_purges_index` (AC2, AC14).
- `test_tenant_isolation_two_workspaces_identical_content` (AC15).
- `test_patch_index_strategy_requires_admin_and_get_index_requires_member` (AC16).
- `test_no_mutating_tool_call_ever` (AC17) — assert only `list_resources`/`read_resource` invoked on the fetcher across a full+incremental cycle.

DB tests — `apps/api/tests/mcp/`:
- `test_migration_0021_up_down` (AC1) — assert table/indexes/constraint via `pg_indexes`/`pg_constraint`, and clean downgrade.
- `test_ledger_check_constraint_blocks_tombstone_with_chunks` (AC8) — inserting `deleted_at` with `chunk_count>0` raises.

## 8. Security & policy considerations

F20 introduces **no new MCP egress surface** — every list/read flows through F09's gateway, so all seven MCP Security Rules (`docs/FORGE_SPEC.md` → "MCP Security Rules") are inherited unchanged:

1. **Read-only by default (rule 1).** F20 uses only `resources/list` + `resources/read`; `allow_write` stays immutable-false; no mutating tool is ever called (AC17). Sync-and-index cannot expand write scope.
2. **Token binding RFC 8707 (rule 2).** OAuth tokens used for the background reads are the audience-bound tokens minted by F09; no new credential flow.
3. **Input validation (rule 3).** Resource reads carry no free-form tool arguments; namespace/uri inputs are validated by the gateway.
4. **Audit (rule 4).** Each `resources/list`/`read` writes an `mcp_audit_log` row in F09; F20 additionally records `knowledge_sync_runs` rows and the per-resource ledger, giving a full reconstructable index history.
5. **Namespace scoping (rule 5).** Enumeration is constrained to `allowed_namespaces`; out-of-scope resources are never indexed (AC4) and cannot leak into retrieval.
6. **Secret redaction (rule 6).** The gateway redacts on egress; F20 re-runs `redact_secrets` before persistence as defense-in-depth — important because indexed content is **persisted at rest** (unlike transient query-through), so a redaction miss would be durable. AC11.
7. **Policy parity (rule 7).** MCP reads pass the same `ToolPolicy`/read-only floor as any agent tool call.

Additional F20-specific controls:
- **Persistence-at-rest hygiene.** Indexed external content lives in `retrieval_chunks`; switching away or deleting the connection **purges** chunks + ledger (`MCP_INDEX_DELETE_ON_DISABLE`, AC14) so revoked sources cannot keep serving stale/leaked content. Tombstoning (AC8) removes upstream-deleted resources promptly, honoring source-of-truth deletions.
- **Tenant isolation.** `workspace_id` is denormalized onto both `mcp_indexed_resources` and `retrieval_chunks`; every read/write filters by it (AC15).
- **RBAC.** Enabling/disabling indexing and reindex are `admin`-only; status is `member`-only; no anonymous access (AC16). `agent-runner` consumes indexed chunks only via `/retrieve`.
- **DoS bounds.** `MCP_INDEX_MAX_RESOURCES`, `MCP_INDEX_PAGE_SIZE`, `MCP_INDEX_CONCURRENCY`, the gateway's `MCP_MAX_RESOURCE_BYTES` size cap, per-source Redis lock, and bounded embed batches keep a large or hostile MCP server from exhausting worker/DB resources.
- **Freshness honesty.** "MCP freshness lag" (`now - last_synced_at`) is surfaced per source so operators know how stale indexed data may be — the explicit trade-off vs query-through's always-fresh guarantee.

## 9. Effort estimate & risk (S/M/L + key risks)

**Effort: M.** It is largely composition of F05 + F09: ledger table + migration (S), `McpResourceChunker` mime routing (S), `McpSyncIndexer` full/incremental/tombstone pipeline (M), Celery tasks + beat (S), auto-provision/teardown service + API endpoints (S), the F05 retrieval-routing change (S), UI panel (S), tests (M). The risk lives in incremental correctness, not volume.

Key risks:
- **Change-token availability varies by server.** Many MCP servers omit `lastModified`/etag annotations, forcing read-then-hash on every incremental sync (defeats the "skip" optimization for those servers). Mitigation: content-hash fallback (AC6) still avoids re-chunk/re-embed (the expensive part); document that token-less servers pay the read cost but not the embed cost. (Medium)
- **Tombstone false-deletes on partial enumeration.** A failed/aborted `resources/list` mid-cursor could make resources look "gone" and wrongly purge their chunks. Mitigation: only sweep after a fully exhausted cursor; otherwise set `sweep_skipped=true` and keep chunks (AC8). (Medium → mitigated to Low)
- **Double-fetch / wrong routing.** If the F05 retrieval branch is not updated, indexed sources would be queried live AND served from the index. Mitigation: explicit `index_strategy` routing + AC9 asserting zero live calls for `sync_and_index` and exactly one for `query_through`. (Medium → mitigated)
- **Embedding cost/throughput for large corpora.** A big Confluence/Notion space can be thousands of resources. Mitigation: `MCP_INDEX_MAX_RESOURCES` cap, batched embeds, incremental skips, per-source lock, and the same pgvector ≤1M-vector guidance as F05. (Medium)
- **Stale data window.** Indexed mode is fundamentally less fresh than query-through. Mitigation: tunable `freshness_sla_minutes` + beat refresh + visible freshness lag; recommend query-through for high-volatility sources in docs. (Low — by design)
- **Persisted redaction misses are durable.** Mitigation: double redaction (gateway + pre-persist) and purge-on-disable. (Low)
- **MCP `resources/subscribe` push not used.** Polling can lag bursty changes. Mitigation: SLA-driven polling for V2; subscription-driven incremental is §12 future. (Low)

## 10. Key files / paths (exact)

- `apps/api/alembic/versions/0021_mcp_sync_and_index.py` — `mcp_indexed_resources` table + indexes + check constraint (down_revision = current head ⊇ 0005, 0009).
- `packages/knowledge-core/src/knowledge_core/sync/mcp_indexer.py` — `McpSyncIndexer`, `ResourceRef`, `SyncReport`, `McpResourceFetcher`/`ResourceLedger` protocols.
- `packages/knowledge-core/src/knowledge_core/chunking/mcp.py` — `McpResourceChunker` (mime-type strategy routing).
- `packages/knowledge-core/src/knowledge_core/service.py` — **modify** `HybridRetrievalService.retrieve()` MCP branch to route by `index_strategy` (live vs index-served).
- `packages/knowledge-core/tests/sync/` — unit tests + `FakeMcpResourceFetcher`/`FakeResourceLedger` fixtures.
- `apps/worker/tasks/knowledge_mcp.py` — `knowledge.mcp_full_sync`, `knowledge.mcp_incremental_sync`, `knowledge.refresh_stale_mcp_sources` (beat).
- `apps/worker/adapters/gateway_fetcher.py` — `GatewayMcpResourceFetcher` over F09 `MCPGatewayClient`.
- `apps/worker/tests/` — worker/integration tests.
- `apps/api/app/api/v1/mcp.py` — **extend** F09 router: PATCH `index_strategy`, `POST .../index/reindex`, `GET .../index`.
- `apps/api/app/services/mcp_index_service.py` — `ensure_indexed_source`, `teardown_indexed_source`, `index_status`.
- `apps/api/app/services/ledger_repo.py` — SQLAlchemy `ResourceLedger` impl over `mcp_indexed_resources`.
- `apps/api/app/schemas/mcp.py` — **extend** `UpdateConnectionRequest`, add `McpIndexStatus`.
- `apps/api/app/models/mcp.py` — **add** `MCPIndexedResource` ORM model.
- `apps/api/tests/mcp/` — API + migration + ledger-constraint tests.
- `apps/web/components/mcp/connection-index-panel.tsx`, `apps/web/lib/api/mcp.ts` (extend), `apps/web/app/(app)/knowledge/page.tsx` (extend).
- `examples/mcp-connectors/confluence-engineering-indexed.yaml` — `index_strategy: sync_and_index` template with freshness/security notes.
- `deploy/.env.example`, `deploy/.env.production.example` — add `MCP_INDEX_*` vars.

## 11. Research references (relevant links from the spec/research report)

- MCP specification (2025-11-25) — Resources feature, `resources/list` pagination/cursors, `resources/read`, annotations (change-token source), subscription notifications: https://modelcontextprotocol.io/specification/2025-11-25
- MCP 2026 RC (stateless HTTP, ships 2026-07-28) — the transport the gateway already targets in F09: https://blog.modelcontextprotocol.io/posts/2026-07-28-release-candidate/
- MCP security advisory (DoD 2026) — least-privilege, input validation, gateway-as-middleware (inherited via F09): https://media.defense.gov/2026/Jun/02/2003943289/-1/-1/0/CSI_MCP_SECURITY.PDF
- Production RAG guide 2026 — why local hybrid (semantic+BM25+RRF+rerank) beats live single-leg retrieval; the payoff of indexing external data: https://lushbinary.com/blog/rag-retrieval-augmented-generation-production-guide/
- Hybrid search BM25 + pgvector reference: https://github.com/Syed007Hassan/Hybrid-Search-For-Rag
- pgvector (≤1M-vector guidance, co-located embeddings+metadata): https://github.com/pgvector/pgvector
- Jina Reranker v2 (the rerank now applied to MCP chunks too): https://jina.ai/reranker/
- Spec sections: `docs/FORGE_SPEC.md` → "Knowledge Sync Modes" (MCP sync-and-index row), "MCP Integration" → MCP Connection Schema (`index_strategy: sync_and_index`, `freshness_sla_minutes`), "Chunk Types and Priority Weights" (MCP resource weight 1.0), "Core Data Model" (KnowledgeSource/RetrievalChunk, MCPConnection), Phase 2 roadmap ("MCP sync-and-index mode").
- Research: `docs/forge-research-report.md` → "Hybrid Retrieval: Code-Aware RAG" (RRF k=60, hybrid value) and "Model Context Protocol" (Resources, security considerations).
- Sibling slices: `docs/implementation-slices/v1/F09-mcp-gateway-v1.md` (§12 names this as deferred), `docs/implementation-slices/v1/F05-hybrid-knowledge-retrieval.md` (§12 names this as deferred; schema/protocols reused).

## 12. Out of scope / future

- **Subscription-driven incremental** via MCP `resources/subscribe` + `notifications/resources/updated` push — V2 polls on the freshness SLA; webhook/subscription-driven re-index (lower lag, no full enumeration) is a follow-up once the gateway holds durable sessions/subscriptions.
- **Binary / non-text resource indexing** (PDF, images, office docs) — V2 indexes text-bearing resources only; binary extraction/OCR is future.
- **Write-back / bidirectional MCP sync** — F20 is strictly read-only ingestion; mutating MCP calls remain V2+ and gated, per F09 §12.
- **Per-resource ACL/permission propagation** — F20 scopes by `allowed_namespaces`; mirroring fine-grained upstream per-document ACLs into retrieval-time filtering is future (out-of-the-box, all indexed chunks are visible to any workspace member with retrieval access).
- **Temporal cron scheduling** — the periodic poll runs on Celery beat in V2; moving it to a durable Temporal cron workflow is deferred to the Temporal slice (indexer body unchanged).
- **True BM25 (ParadeDB `pg_search`)** — inherited from F05's roadmap; F20 indexes into whatever keyword backend F05 uses, no change here.
- **Embedding model migration / re-index tooling** — changing `EMBEDDING_DIM` requires the same full re-index F05 documents; MCP-indexed sources re-sync from the server, no special path.
- **Cross-source dedup** (same document reachable via repo + MCP) — not handled; each source indexes independently.
