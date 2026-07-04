# F05 — Hybrid Knowledge Retrieval (pgvector + BM25 + RRF + Jina rerank)

> Phase: v1 · Spec module(s): Knowledge Service, `packages/knowledge-core`, Observability (retrieval debug) · Status target: A query against an indexed repo/spec/doc corpus returns the top-k chunks via parallel semantic (pgvector cosine) + keyword (Postgres full-text) search, fused with Reciprocal Rank Fusion (k=60), chunk-type-weighted, reranked by self-hosted Jina Reranker v2, with per-result source attribution and per-stage debug scores — all tenant-isolated and policy-scoped. Full + incremental + on-demand sync modes index repo/spec/doc sources; MCP query-through joins the candidate pool at retrieval time.

---

## 1. Intent — what & why

Forge agents are only as good as the context they receive. Production RAG research cited in the spec attributes 73% of RAG failures to retrieval, not generation (`docs/forge-research-report.md`, "Hybrid Retrieval"). Pure vector search misses exact identifiers, symbol names, and error strings that dominate code queries; pure keyword search misses semantic intent in natural-language architecture questions.

F05 builds the `knowledge-core` package and the Knowledge Service surface that:

1. Chunks repo files, specs/plans/validation docs, READMEs, and policy/`AGENTS.md` files using source-appropriate strategies and assigns priority weights (`docs/FORGE_SPEC.md` → "Chunk Types and Priority Weights").
2. Embeds chunks and stores them co-located with metadata in Postgres (`pgvector`), plus a full-text `tsvector` for keyword search — no second datastore.
3. At query time, runs semantic and keyword search in parallel, fuses with RRF (`score(d) = Σ 1/(k + rank_i(d))`, `k=60`), applies chunk-type weights, then reranks the top candidates with a self-hosted Jina Reranker v2 cross-encoder.
4. Returns top-5..top-10 chunks with full source attribution (for the Approval UI "knowledge provenance" requirement) and an optional per-stage debug payload (for the "retrieval debug" observability requirement).
5. Keeps the index fresh through full, incremental (git diff / webhook), and on-demand sync modes, with freshness-SLA enforcement.

This is the V1 knowledge substrate consumed by the agent runtime, the spec engine, and the Approval UI. It is deliberately the "simplest production-valid hybrid retrieval with no additional infrastructure" (research report, tech table).

## 2. User-facing behavior / journeys

F05 is mostly an internal capability, but it has concrete user-observable behavior:

- **Journey A — Connect a repo and search it.** An admin registers a repo `KnowledgeSource` (or it is auto-created when a `RepositoryConnection` is added). A full sync runs in the background; the source shows `indexing → ready` with a chunk count. A member opens the knowledge search box (or the API), types "how does auth middleware reject expired tokens", and gets ranked results showing file path, line range, and a snippet, with the most relevant function-level code chunk first.
- **Journey B — Agent retrieval during a run.** When a `Task` enters `executing`, the agent runtime calls task-scoped retrieval honoring the task's `knowledge_scope` (`repos[]`, `mcp_sources[]`, `source_types[]`, `freshness_min_hours`). Retrieved chunks are injected into agent context with source attribution and surfaced later in the Approval UI as "knowledge provenance".
- **Journey C — Freshness.** A repo push fires a GitHub webhook; an incremental sync re-indexes only the changed files within minutes. If a source is older than its freshness SLA, the search response flags it `stale` and (for `on_demand` sources) can trigger a sync-before-retrieve.
- **Journey D — Retrieval debugging.** A maintainer calls search with `debug=true` and inspects, per result, the semantic rank, keyword rank, fused RRF score, chunk weight, and reranker score — to tune weights or diagnose a bad answer. This feeds the "reranker delta" and "retrieval latency p50/p95/p99" observability metrics.

## 3. Vertical slice

### 3.1 Data model (tables/columns/migrations touched)

Single Alembic migration `apps/api/alembic/versions/0005_knowledge_retrieval.py`. It chains after the foundation baseline `0001_*` (the cross-cutting platform-foundation slice that creates `workspaces`/`users` and the shared `Base`/naming convention — see §5); the `0005` revision number indicates ordering only. It must `CREATE EXTENSION IF NOT EXISTS vector;`.

**Table `knowledge_sources`**

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid` PK | `gen_random_uuid()` |
| `workspace_id` | `uuid` NOT NULL FK → `workspaces.id` ON DELETE CASCADE | tenant key |
| `source_type` | `text` NOT NULL | enum: `repo \| spec \| doc \| mcp_resource` |
| `name` | `text` NOT NULL | display name |
| `config` | `jsonb` NOT NULL DEFAULT `'{}'` | repo_id/ref, index_paths, exclude_paths, mcp_connection_id, etc. |
| `sync_mode` | `text` NOT NULL DEFAULT `'full'` | enum: `full \| incremental \| on_demand \| mcp_query_through \| mcp_sync_and_index` |
| `freshness_sla_minutes` | `integer` NOT NULL DEFAULT `1440` | from policy `knowledge_rules.freshness_sla_hours` × 60 |
| `status` | `text` NOT NULL DEFAULT `'pending'` | enum: `pending \| indexing \| ready \| error` |
| `last_synced_at` | `timestamptz` NULL | |
| `last_sync_error` | `text` NULL | |
| `last_indexed_commit` | `text` NULL | git SHA for incremental diffing (repo sources) |
| `created_at` / `updated_at` | `timestamptz` NOT NULL | |

Constraints/indexes: `UNIQUE (workspace_id, source_type, name)`; btree `(workspace_id, status)`.

**Table `retrieval_chunks`**

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid` PK | |
| `workspace_id` | `uuid` NOT NULL FK → `workspaces.id` ON DELETE CASCADE | denormalized for fast tenant filtering |
| `source_id` | `uuid` NOT NULL FK → `knowledge_sources.id` ON DELETE CASCADE | |
| `chunk_type` | `text` NOT NULL | enum: `markdown_doc \| code \| file_summary \| readme \| policy \| spec_plan_validation \| mcp_resource` |
| `weight` | `numeric(3,2)` NOT NULL DEFAULT `1.00` | denormalized priority weight (see §4 weight table) |
| `content` | `text` NOT NULL | redacted chunk text (secrets stripped) |
| `content_hash` | `text` NOT NULL | sha256 of normalized content — incremental dedup |
| `token_count` | `integer` NOT NULL | |
| `embedding` | `vector(1024)` NULL | NULL allowed until embed step completes; Jina embeddings v3 default dim |
| `ts` | `tsvector` GENERATED ALWAYS AS (`to_tsvector('english', content)`) STORED | keyword search column |
| `source_uri` | `text` NOT NULL | attribution: `repo://org/repo@sha/path#L10-L42`, `spec://SPEC-17/plan.md`, `mcp://confluence-engineering/<id>` |
| `chunk_index` | `integer` NOT NULL | ordinal within (source_id, file_path) |
| `metadata` | `jsonb` NOT NULL DEFAULT `'{}'` | file_path, language, symbol_name, start_line, end_line, repo_ref, mcp_namespace, title, url |
| `created_at` / `updated_at` | `timestamptz` NOT NULL | |

Indexes:
- `CREATE INDEX ix_chunks_embedding_hnsw ON retrieval_chunks USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);`
- `CREATE INDEX ix_chunks_ts_gin ON retrieval_chunks USING gin (ts);`
- `CREATE INDEX ix_chunks_tenant_source ON retrieval_chunks (workspace_id, source_id);`
- `CREATE UNIQUE INDEX ux_chunks_dedup ON retrieval_chunks (source_id, (metadata->>'file_path'), chunk_index);` — upsert target for incremental sync.
- partial/btree `ix_chunks_content_hash ON retrieval_chunks (source_id, content_hash);`

**Table `knowledge_sync_runs`** (observability + idempotency)

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid` PK | |
| `source_id` | `uuid` NOT NULL FK | |
| `mode` | `text` NOT NULL | `full \| incremental \| on_demand` |
| `status` | `text` NOT NULL | `running \| succeeded \| failed` |
| `chunks_indexed` / `chunks_deleted` / `chunks_skipped` | `integer` NOT NULL DEFAULT 0 | |
| `from_commit` / `to_commit` | `text` NULL | |
| `error` | `text` NULL | |
| `started_at` / `finished_at` | `timestamptz` | |

Note: native Postgres FTS (`tsvector` + `ts_rank_cd`) is the V1 keyword ranker. Because RRF is rank-based, the exact keyword score scale is irrelevant to fusion — see §9 for the documented upgrade path to true BM25 via the ParadeDB `pg_search` extension, which is a non-breaking swap behind the `KeywordSearcher` protocol.

### 3.2 Backend (FastAPI routes + services/packages)

New router `apps/api/app/api/v1/knowledge.py`, mounted at `/api/v1/knowledge`. All routes require auth + workspace membership (RBAC: `viewer`+ may read sources and run ad hoc searches, `member`+ additionally call task-scoped `/retrieve`, `admin` to mutate sources; the `agent-runner` role is allowed `/search` and `/retrieve` only — never source mutation). Thin controllers delegate to `apps/api/app/services/knowledge_service.py`, which wires `packages/knowledge-core` to the SQLAlchemy session, the BYOK key vault, the MCP gateway client, and Celery.

| Method | Path | RBAC | Purpose |
|---|---|---|---|
| `POST` | `/sources` | admin | Register a `KnowledgeSource` (validates `KnowledgeSourceConfig`). |
| `GET` | `/sources` | viewer | List sources for the workspace with status + chunk counts. |
| `GET` | `/sources/{source_id}` | viewer | Source detail + last sync run. |
| `DELETE` | `/sources/{source_id}` | admin | Delete source and cascade chunks. |
| `POST` | `/sources/{source_id}/sync` | admin | Enqueue a sync (`{"mode": "full\|incremental\|on_demand", "changed_paths"?: [...]}`); returns `sync_run_id`. |
| `GET` | `/sources/{source_id}/sync/{sync_run_id}` | viewer | Sync run status. |
| `POST` | `/search` | viewer | Ad hoc hybrid search (`HybridSearchRequest` → `RetrievalResult`); supports `debug`. |
| `POST` | `/retrieve` | member / agent-runner | Task-scoped retrieval honoring `KnowledgeScope` (used by agent runtime). |

`packages/knowledge-core` (the heart of the slice; framework-agnostic, no FastAPI/SQLAlchemy import leakage in the pure layers):

```
packages/knowledge-core/src/knowledge_core/
├── models.py            # Pydantic v2 models + enums (§4)
├── protocols.py         # Chunker, EmbeddingProvider, SemanticSearcher, KeywordSearcher, Reranker, HybridRetriever
├── weights.py           # CHUNK_TYPE_WEIGHTS map + resolve_weight()
├── fusion.py            # reciprocal_rank_fusion() pure function
├── redaction.py         # redact_secrets() (regex + entropy heuristics)
├── chunking/
│   ├── markdown.py      # semantic paragraph splitter (headings + paragraphs)
│   ├── code.py          # tree-sitter function/class AST chunker (+ line-window fallback)
│   ├── summary.py       # file-summary generator (LLM, optional)
│   └── registry.py      # ChunkStrategyRegistry: (source_type, path/ext) -> Chunker
├── embedding/
│   ├── jina.py          # JinaEmbeddingProvider (default, dim=1024)
│   └── openai_compat.py # OpenAICompatEmbeddingProvider (BYOK)
├── search/
│   ├── pgvector.py      # PgVectorSemanticSearcher (SQLAlchemy)
│   ├── fts.py           # PostgresKeywordSearcher (SQLAlchemy, ts_rank_cd)
│   └── jina_reranker.py # JinaRerankerV2 (HTTP to self-hosted reranker)
├── sync/
│   ├── indexer.py       # Indexer: chunk -> redact -> embed -> upsert
│   └── differ.py        # incremental diff resolution (git/webhook paths)
└── service.py           # HybridRetrievalService (orchestrates the pipeline)
```

`HybridRetrievalService.retrieve()` pipeline: (1) embed query via `EmbeddingProvider.embed_query`; (2) run `SemanticSearcher.search` and `KeywordSearcher.search` concurrently (`asyncio.gather`), each scoped + limited to 50; (3) if scope includes MCP query-through sources, call the `v1/F09-mcp-gateway-v1` read-only `/query` endpoint (returns `McpResourceSnapshot[]` with already-redacted `text` + `source_uri == "mcp://{slug}/{uri}"`) and normalize each snapshot into an ephemeral `RankedChunk` (`chunk_id=None`, `chunk_type=mcp_resource`); (4) `reciprocal_rank_fusion` over the ranked lists with `k=60` — DB chunks keyed by `str(chunk_id)`, ephemeral MCP candidates keyed by their `source_uri` (they have no `chunk_id`) — multiplying each candidate's fused score by its chunk-type weight; (5) take the top-50 weighted candidates → `Reranker.rerank`; (6) `final_score = rerank_score * weight`, order desc, take `top_k` (default 8); (7) assemble `RetrievalResult` with provenance and optional `RetrievalDebug`.

### 3.3 Worker / agent runtime (Celery tasks, LangGraph, if any)

Celery tasks in `apps/worker/tasks/knowledge.py` (queue `knowledge`):

- `knowledge.full_sync(source_id: str) -> dict` — enumerate in-scope files (repo worktree / spec dir / doc dir), chunk → redact → embed → bulk upsert; mark stale chunks (not seen this run) for deletion; write a `knowledge_sync_runs` row; set source `status=ready`, `last_indexed_commit`.
- `knowledge.incremental_sync(source_id: str, changed_paths: list[str] | None, to_commit: str | None) -> dict` — resolve changed files via `differ` (git diff `last_indexed_commit..to_commit`, or explicit `changed_paths` from webhook); re-chunk only those files; delete chunks for removed files; skip unchanged chunks by `content_hash`.
- `knowledge.index_paths(source_id: str, paths: list[str]) -> dict` — shared worker used by both sync tasks (idempotent upsert).
- `knowledge.generate_file_summaries(source_id: str, paths: list[str]) -> dict` — optional; produces `file_summary` chunks via the configured model provider (skipped when no model key present — degraded mode).
- `knowledge.refresh_stale_sources() -> dict` — Celery beat (every 5 min) scans sources past their `freshness_sla_minutes` and enqueues the appropriate sync.

Concurrency/idempotency: per-`source_id` Redis lock to serialize syncs; embedding calls batched (default 64 texts/request) with bounded retry. No LangGraph in this slice — F05 exposes a retrieval function consumed by the agent runtime's LangGraph graph (`v1/F06-single-execution-agent`, in its `load_context` node), it does not own a graph.

### 3.4 Frontend / UI (Next.js routes/components, if any)

Minimal in V1 (the rich provenance UI is owned by the Approval UI slice). This slice ships:

- `apps/web/app/(app)/knowledge/page.tsx` — Knowledge Sources list (name, type, status, chunk count, last synced, freshness badge) with a "Sync now" action. Uses TanStack Query against `/sources`.
- `apps/web/components/knowledge/search-panel.tsx` — a search box + ranked results list (path, snippet, line range, scores when debug on). Reused by the command palette later.
- `apps/web/lib/api/knowledge.ts` — typed client matching the Pydantic contracts in §4.

The Approval UI's "knowledge provenance" panel (spec → "Approval UI Must Show", item 5), built in `v1/F08-plan-execute-verify-pr-approval`, consumes the `RankedChunk.source_uri` + `metadata`; F05 guarantees those fields, the panel itself is out of scope here.

### 3.5 Infra / deploy (compose, helm, caddy, if any)

- `db` service: use a pgvector-enabled Postgres image, pinned by digest, e.g. `pgvector/pgvector:pg16@sha256:<digest>`. Migration enables the extension.
- New service `reranker` in `deploy/docker-compose.yml`: self-hosted Jina Reranker v2 (`jinaai/jina-reranker-v2-base-multilingual`) served over HTTP on an internal network; `autoheal=true`, healthcheck `GET /health`, CPU/mem limits, non-root. Env `JINA_RERANKER_URL=http://reranker:8080` consumed by the worker and api.
- New env vars (add to `deploy/.env.example` and `.env.production.example`): `EMBEDDING_PROVIDER` (default `jina`), `EMBEDDING_MODEL` (default `jina-embeddings-v3`), `EMBEDDING_DIM` (default `1024`, advisory — column is pinned), `JINA_API_URL`/key reference, `JINA_RERANKER_URL`, `RERANK_ENABLED` (default `true`), `RETRIEVAL_TOP_K` (default `8`), `RRF_K` (default `60`), `HYBRID_CANDIDATES` (default `50`).
- Network segmentation: `reranker` sits on an internal network reachable only by `api` and `worker`, never exposed via Caddy.

## 4. Public interfaces / contracts (exact signatures, Pydantic models, Protocols, YAML schemas)

`packages/knowledge-core/src/knowledge_core/models.py`:

```python
from enum import StrEnum
from pydantic import BaseModel, Field
from uuid import UUID

class SourceType(StrEnum):
    repo = "repo"; spec = "spec"; doc = "doc"; mcp_resource = "mcp_resource"

class ChunkType(StrEnum):
    markdown_doc = "markdown_doc"; code = "code"; file_summary = "file_summary"
    readme = "readme"; policy = "policy"; spec_plan_validation = "spec_plan_validation"
    mcp_resource = "mcp_resource"

class SyncMode(StrEnum):
    full = "full"; incremental = "incremental"; on_demand = "on_demand"
    mcp_query_through = "mcp_query_through"; mcp_sync_and_index = "mcp_sync_and_index"

# Priority weights — mirrors docs/FORGE_SPEC.md "Chunk Types and Priority Weights"
CHUNK_TYPE_WEIGHTS: dict[ChunkType, float] = {
    ChunkType.markdown_doc: 1.0, ChunkType.code: 1.0, ChunkType.file_summary: 1.2,
    ChunkType.readme: 1.3, ChunkType.policy: 1.5,
    ChunkType.spec_plan_validation: 1.4, ChunkType.mcp_resource: 1.0,
}

class SourceDocument(BaseModel):
    source_id: UUID
    source_type: SourceType
    path: str                       # file_path or logical id
    text: str
    metadata: dict = Field(default_factory=dict)

class Chunk(BaseModel):
    chunk_type: ChunkType
    content: str
    chunk_index: int
    source_uri: str
    metadata: dict = Field(default_factory=dict)   # file_path, language, symbol_name, start_line, end_line, ...
    weight: float = 1.0
    content_hash: str | None = None
    token_count: int | None = None

class KnowledgeScope(BaseModel):
    repos: list[str] = Field(default_factory=list)
    mcp_sources: list[str] = Field(default_factory=list)
    source_types: list[SourceType] = Field(default_factory=list)
    freshness_min_hours: int | None = None

class RetrievalQuery(BaseModel):
    workspace_id: UUID
    text: str
    scope: KnowledgeScope = Field(default_factory=KnowledgeScope)
    top_k: int = 8
    rerank: bool = True
    debug: bool = False

class StageScores(BaseModel):
    semantic_rank: int | None = None
    keyword_rank: int | None = None
    rrf_score: float | None = None
    weight: float
    rerank_score: float | None = None
    final_score: float

class RankedChunk(BaseModel):
    chunk_id: UUID | None           # None for ephemeral MCP query-through results
    source_id: UUID | None
    chunk_type: ChunkType
    content: str
    source_uri: str
    metadata: dict
    scores: StageScores

class RetrievalDebug(BaseModel):
    rrf_k: int
    candidates_considered: int
    reranked: bool
    stale_sources: list[UUID]
    latency_ms: dict[str, float]    # {"embed":..,"semantic":..,"keyword":..,"fusion":..,"rerank":..,"total":..}

class RetrievalResult(BaseModel):
    query: str
    results: list[RankedChunk]
    debug: RetrievalDebug | None = None
```

`packages/knowledge-core/src/knowledge_core/protocols.py`:

```python
from typing import Protocol, Sequence, Mapping
from uuid import UUID
from .models import Chunk, SourceDocument, RetrievalQuery, RetrievalResult, RankedChunk

class Chunker(Protocol):
    def chunk(self, document: SourceDocument) -> list[Chunk]: ...

class EmbeddingProvider(Protocol):
    name: str
    dim: int
    async def embed_documents(self, texts: list[str]) -> list[list[float]]: ...
    async def embed_query(self, text: str) -> list[float]: ...

class ScoredCandidate(Protocol):
    chunk_id: UUID
    score: float

class SemanticSearcher(Protocol):
    async def search(self, *, workspace_id: UUID, query_embedding: list[float],
                     source_ids: Sequence[UUID], limit: int) -> list[RankedChunk]: ...

class KeywordSearcher(Protocol):
    async def search(self, *, workspace_id: UUID, query_text: str,
                     source_ids: Sequence[UUID], limit: int) -> list[RankedChunk]: ...

class Reranker(Protocol):
    async def rerank(self, *, query: str, candidates: list[RankedChunk],
                     top_n: int) -> list[RankedChunk]: ...   # sets scores.rerank_score

class HybridRetriever(Protocol):
    async def retrieve(self, query: RetrievalQuery) -> RetrievalResult: ...
```

`packages/knowledge-core/src/knowledge_core/fusion.py` — the load-bearing fusion contract:

```python
def reciprocal_rank_fusion(
    ranked_lists: Sequence[Sequence[str]],          # each inner list: chunk_ids ordered best->worst
    *,
    k: int = 60,
    weights: Mapping[str, float] | None = None,     # chunk_id -> chunk-type weight (default 1.0)
) -> list[tuple[str, float]]:
    """RRF: score(d) = weight(d) * Σ_i 1 / (k + rank_i(d)), rank starting at 1.
    Returns (chunk_id, fused_score) sorted by fused_score desc, ties broken by chunk_id."""
```

FastAPI request/response (`apps/api/app/schemas/knowledge.py`):

```python
class HybridSearchRequest(BaseModel):
    query: str
    scope: KnowledgeScope = KnowledgeScope()
    top_k: int = Field(8, ge=1, le=50)
    rerank: bool = True
    debug: bool = False
# Response is knowledge_core.models.RetrievalResult

class CreateSourceRequest(BaseModel):
    name: str
    source_type: SourceType
    sync_mode: SyncMode = SyncMode.full
    config: "KnowledgeSourceConfig"
    freshness_sla_minutes: int = 1440
```

YAML `KnowledgeSourceConfig` (stored in `knowledge_sources.config`, repo variant):

```yaml
# source_type: repo
repo_id: github.com/org/api
ref: main
index_paths: ["app/**", "docs/**", "specs/**"]   # from policy knowledge_rules.index_paths
exclude_paths: [".venv/**", "__pycache__/**", "*.pyc"]
generate_file_summaries: true
# source_type: mcp_resource
mcp_connection_id: confluence-engineering
index_strategy: query_through        # query_through (V1) | sync_and_index (V2)
allowed_namespaces: [engineering, architecture]
```

## 5. Dependencies — features/slices that must exist first

Referenced by `<phase>/<id>-<slug>` path. The platform-foundation and auth/secrets concerns are cross-cutting Phase-0 prerequisites that every v1 slice assumes; they do not yet have a dedicated numbered feature file, so the slugs below are placeholders to reconcile against the final foundation slice id (sibling slices reference them variously as `v1/F00-platform-foundation`, `cross-cutting/C01-monorepo-and-api-foundations`, and `cross-cutting/C02-auth-and-rbac`). The numbered v1 slugs below are authoritative and match the files in `docs/implementation-slices/v1/`.

- **cross-cutting/F00-platform-foundation** (REQUIRED) — uv workspace, `apps/api` FastAPI skeleton, async SQLAlchemy 2.x session, `packages/db` shared `Base`/`TimestampMixin` + Alembic env/naming convention, Alembic baseline `0001_*` (creates `workspaces`, `users`, the four RBAC roles admin/member/viewer/agent-runner, and the auth dependency), Celery app, and a pgvector-capable Postgres in compose. F05 adds `CREATE EXTENSION vector` + migration `0005`.
- **cross-cutting/F00-auth-secrets** (REQUIRED) — the encrypted per-workspace API-key vault (BYOK) + resolver used to fetch embedding / reranker / model-provider credentials at call time; never logged. (Sibling slices also call this `v1/F15-auth-secrets-rbac` / `cross-cutting/C02-auth-and-rbac` — reconcile the slug when the auth/secrets slice lands.)
- **v1/F04-repo-policy** (REQUIRED for repo sources, stubbable) — supplies `knowledge_rules.index_paths`, `exclude_paths`, and `freshness_sla_hours` that populate `KnowledgeSourceConfig` and gate the indexer's path allowlist. F05 runs with explicit config if policy is absent (degraded).
- **v1/F03-github-app** (SOFT) — provides the bare-mirror worktree/content and push webhooks that drive `incremental_sync` (F03's status target explicitly "triggers incremental knowledge indexing" on push). Until present, repo sources index from a local worktree path via `full_sync` only.
- **v1/F02-spec-engine** (SOFT) — produces the `spec.md` / `plan.md` / `validation.md` artifacts that `source_type=spec` indexes as `spec_plan_validation` chunks. Absent F02, only `repo` / `doc` / `mcp_resource` sources are populated.
- **v1/F09-mcp-gateway-v1** (SOFT) — required only for the MCP query-through retrieval path; F05 calls its read-only `/query` endpoint and consumes `McpResourceSnapshot[]` as `mcp_resource` candidates. Absence disables that candidate source; the rest of the pipeline is unchanged.
- **v1/F12-eval-harness** (SOFT) — golden retrieval set used to tune weights/reranker and to run the recall@k regression gate; F05 ships its own fixtures so it is testable independently.

**Consumers (not dependencies):** `v1/F06-single-execution-agent` calls `/retrieve` from its `load_context` graph node to inject context, and `v1/F08-plan-execute-verify-pr-approval` surfaces the returned `source_uri`/`metadata` as the Approval UI "knowledge provenance" panel. Both depend on F05; F05 does not depend on them.

## 6. Acceptance criteria (numbered, testable)

1. Migration `0005` creates `knowledge_sources`, `retrieval_chunks`, `knowledge_sync_runs`, enables `vector`, and creates the HNSW, GIN, and dedup indexes; `alembic downgrade` cleanly drops them.
2. `reciprocal_rank_fusion([[a,b,c],[b,d]], k=60)` returns scores equal to the hand-computed RRF values (`a=1/61`, `b=1/62+1/61`, `c=1/63`, `d=1/62`) ordered desc; changing `k` changes scores; `k=60` is the default.
3. Chunk-type weights from `CHUNK_TYPE_WEIGHTS` are applied: given equal RRF rank, a `policy` chunk (1.5) outranks a `code` chunk (1.0); weights exactly match the spec table (policy 1.5, spec_plan_validation 1.4, readme 1.3, file_summary 1.2, others 1.0).
4. A hybrid search returns a result that vector-only search misses (exact symbol/error-string query) AND a result that keyword-only search misses (paraphrased natural-language query), proving fusion adds recall over either leg alone.
5. `final_score = rerank_score * weight`; with `rerank=true` the result order can differ from the fused order, and with `RERANK_ENABLED=false` (or `rerank=false`) the reranker is not called and order equals the weighted-RRF order.
6. Retrieval is tenant-isolated: a query with `workspace_id=A` never returns chunks belonging to workspace `B`, even when both index identical content (verified by row-count and id assertions).
7. Every `RankedChunk` carries a non-empty `source_uri` and `metadata` with `file_path` (or `url`) and, for code chunks, `start_line`/`end_line` — satisfying the Approval UI provenance contract.
8. Incremental sync of a single changed file re-indexes only that file's chunks: unchanged chunks are skipped by `content_hash` (`chunks_skipped > 0`), removed files' chunks are deleted, and other sources are untouched.
9. Full sync is idempotent: running it twice on identical content yields the same chunk set (no duplicates; `ux_chunks_dedup` holds) and `chunks_indexed` on the second run reflects only re-embeds, not new rows.
10. Freshness: a source older than `freshness_sla_minutes` appears in `RetrievalDebug.stale_sources`; `knowledge.refresh_stale_sources` enqueues a sync for it.
11. Secret redaction: content matching the redaction patterns (AWS keys, PEM blocks, `*_API_KEY=...`, high-entropy tokens) is replaced with `«redacted»` before persistence — no secret substring exists in `retrieval_chunks.content` or in any `RetrievalResult`.
12. Task-scoped `/retrieve` honors `KnowledgeScope`: only sources matching `repos`/`mcp_sources`/`source_types` are searched, and `freshness_min_hours` overrides the per-source SLA for staleness flagging.
13. `debug=true` returns `RetrievalDebug` with per-result `StageScores` (semantic_rank, keyword_rank, rrf_score, weight, rerank_score, final_score) and a `latency_ms` breakdown with a `total` key.
14. `EmbeddingProvider` and `Reranker` are swappable via DI: tests run end-to-end with deterministic fakes and no network; production resolves Jina implementations from env/BYOK.

## 7. Test plan (TDD) — concrete test cases (unit + integration), key fixtures

Write tests first; backend-tdd discipline (min coverage 80% per the spec's own profile). Tests live in `packages/knowledge-core/tests/` and `apps/api/tests/knowledge/`.

Key fixtures:
- `FakeEmbeddingProvider(dim=8)` — deterministic embedding = hashed bag-of-words vector, so similarity is reproducible offline.
- `FakeReranker` — scores candidates by lexical overlap with the query (deterministic, monotonic), lets us assert reorder behavior.
- `pg` fixture — real Postgres+pgvector via testcontainers (or a CI service container), migrations applied; required for HNSW/GIN/SQL behavior.
- `seed_corpus` — a tiny fixed corpus: one code file with a function `reject_expired_token`, one README, one `.forge/policy.yaml`, one `plan.md`, one markdown doc; with known expected rankings for two canned queries.

Unit tests (pure, no DB):
- `test_rrf_matches_hand_computed` (AC2); `test_rrf_k_changes_scores`; `test_rrf_tie_break_stable`.
- `test_weight_applied_breaks_rank_tie` (AC3); `test_weights_match_spec_table`.
- `test_markdown_chunker_splits_on_headings_with_overlap`; `test_code_chunker_emits_function_and_class_chunks_with_line_ranges`; `test_code_chunker_falls_back_to_line_window_for_unsupported_lang`.
- `test_redact_secrets_strips_aws_pem_apikey_entropy` and `test_redaction_preserves_nonsecret_text` (AC11).
- `test_content_hash_stable_under_whitespace_normalization`.

Integration tests (with `pg` + fakes):
- `test_full_sync_indexes_and_is_idempotent` (AC9).
- `test_incremental_sync_only_touches_changed_file` (AC8) — modify one file, assert `chunks_skipped>0`, deleted-on-remove, untouched siblings.
- `test_hybrid_beats_either_leg` (AC4) — symbol-exact query found via keyword leg; paraphrase query found via semantic leg.
- `test_rerank_reorders_and_can_be_disabled` (AC5).
- `test_tenant_isolation` (AC6) — two workspaces, identical content, assert no cross-leak.
- `test_provenance_fields_present` (AC7).
- `test_stale_source_flagged_and_refresh_enqueued` (AC10) — backdate `last_synced_at`, assert `stale_sources` + beat task enqueues.
- `test_scope_filters_sources_and_freshness_override` (AC12).
- `test_debug_payload_has_stage_scores_and_latency` (AC13).

API tests (`apps/api/tests/knowledge/`, httpx AsyncClient):
- `test_create_source_requires_admin_and_validates_config`.
- `test_search_requires_membership_and_scopes_to_workspace`.
- `test_sync_endpoint_enqueues_celery_task` (Celery in eager mode).
- `test_retrieve_endpoint_honors_task_knowledge_scope`.

Migration test: `test_migration_0005_up_down` applies and reverses cleanly, asserts indexes exist via `pg_indexes`.

Eval hook (soft, gated on F12): a `test_golden_retrieval_recall_at_k` reads the golden retrieval set and asserts recall@8 ≥ a baseline and that enabling rerank does not reduce recall — wired into the evaluation harness, skipped if absent.

## 8. Security & policy considerations

- **Secret redaction (spec Security → "Secret redaction").** All chunk content passes through `redact_secrets()` before persistence; redaction also applies to MCP query-through results before they enter the candidate pool and the response (note: `v1/F09-mcp-gateway-v1` already redacts its snapshots — F05 re-applies defensively). `redact_secrets()` uses the same pattern set as the shared foundation redaction filter and F09 (AWS keys, PEM blocks, `*_API_KEY=...`, high-entropy tokens) to avoid divergence. Covered by AC11.
- **Tenant isolation.** Every read/write query filters by `workspace_id`; the column is denormalized onto `retrieval_chunks` precisely so no query can omit it. AC6 enforces.
- **Policy-scoped indexing.** Repo sources index only `knowledge_rules.index_paths` and never `exclude_paths` (`.env*`, `secrets/**`, `*.pem`, `*.key` from the policy `write_rules.deny`/`exclude_paths`). The indexer rejects paths outside the allowlist.
- **MCP read-only & least privilege.** Query-through goes through the MCP gateway with `allow_write:false` and `allowed_namespaces` scoping (spec "MCP Security Rules"); F05 never invokes MCP tools, only resource reads, and records an audit entry per MCP call (tool/resource name, payload hash, status, latency).
- **BYOK credentials.** Embedding/reranker/model-provider keys are resolved from the encrypted vault per workspace at call time; never logged, never embedded in chunk metadata.
- **Audit logging.** Index runs and retrieval calls emit audit events (source_id, query hash, result chunk ids/uris, latency) to the platform audit log for the Observability layer's retrieval-debug and freshness-lag metrics.
- **No anonymous access.** All routes authenticated; `agent-runner` role is allowed `/retrieve` and `/search` only, never source mutation.
- **DoS bounds.** `top_k ≤ 50`, candidate cap = `HYBRID_CANDIDATES` (50), reranker batch caps, and per-source sync locks bound resource use.

## 9. Effort estimate & risk (S/M/L + key risks)

**Effort: L** (core retrieval engine, multi-strategy chunking, three sync modes, reranker integration, and the pgvector/FTS schema). Roughly: schema+migration S, chunkers M, search+fusion+rerank M, sync tasks M, API+UI S, tests M.

Key risks:
- **Native FTS ≠ true BM25.** Mitigation: RRF is rank-based so fusion is robust to the score scale; keep `KeywordSearcher` behind a protocol so ParadeDB `pg_search` (true BM25) is a drop-in V2 upgrade without touching fusion/rerank. (Medium)
- **Embedding dimension lock-in.** `vector(1024)` is pinned in the migration; changing the embedding model's dim requires a migration + full re-index. Mitigation: document it; default to Jina embeddings v3 (1024) which is stable for V1. (Medium)
- **HNSW recall/latency tuning.** `m`/`ef_construction`/`ef_search` defaults may need tuning for >100k chunks. Mitigation: start with documented defaults, expose `ef_search` via session GUC, validate against the golden set. (Medium)
- **Reranker hosting cost/latency (~400ms).** Mitigation: `RERANK_ENABLED` flag, cap candidates to 50, run reranker on its own container with resource limits; degrade to weighted-RRF if the reranker is unhealthy. (Medium)
- **AST chunking breadth.** tree-sitter grammar coverage varies by language. Mitigation: line-window fallback chunker for unsupported languages, behind the registry. (Low)
- **File-summary LLM cost.** Optional and skipped without a model key. (Low)

## 10. Key files / paths (exact)

- `apps/api/alembic/versions/0005_knowledge_retrieval.py` — schema + `vector` extension + indexes.
- `packages/knowledge-core/pyproject.toml` — package metadata (uv workspace member).
- `packages/knowledge-core/src/knowledge_core/models.py` — Pydantic models + enums + `CHUNK_TYPE_WEIGHTS`.
- `packages/knowledge-core/src/knowledge_core/protocols.py` — Chunker/EmbeddingProvider/Searcher/Reranker/HybridRetriever.
- `packages/knowledge-core/src/knowledge_core/fusion.py` — `reciprocal_rank_fusion`.
- `packages/knowledge-core/src/knowledge_core/weights.py` — weight resolution.
- `packages/knowledge-core/src/knowledge_core/redaction.py` — `redact_secrets`.
- `packages/knowledge-core/src/knowledge_core/chunking/{markdown.py,code.py,summary.py,registry.py}`.
- `packages/knowledge-core/src/knowledge_core/embedding/{jina.py,openai_compat.py}`.
- `packages/knowledge-core/src/knowledge_core/search/{pgvector.py,fts.py,jina_reranker.py}`.
- `packages/knowledge-core/src/knowledge_core/sync/{indexer.py,differ.py}`.
- `packages/knowledge-core/src/knowledge_core/service.py` — `HybridRetrievalService`.
- `packages/knowledge-core/tests/` — unit + integration tests and fixtures.
- `apps/api/app/api/v1/knowledge.py` — router.
- `apps/api/app/services/knowledge_service.py` — DI wiring (session, vault, MCP client, Celery).
- `apps/api/app/schemas/knowledge.py` — request/response schemas.
- `apps/api/app/models/knowledge.py` — SQLAlchemy ORM models.
- `apps/api/tests/knowledge/` — API tests.
- `apps/worker/tasks/knowledge.py` — Celery sync/index tasks + beat.
- `apps/web/app/(app)/knowledge/page.tsx`, `apps/web/components/knowledge/search-panel.tsx`, `apps/web/lib/api/knowledge.ts`.
- `deploy/docker-compose.yml` (add `reranker`, pgvector `db` image), `deploy/.env.example`, `deploy/.env.production.example`.

## 11. Research references (relevant links from the spec/research report)

- Production RAG guide 2026 (73% retrieval-failure stat, hybrid + rerank pattern): https://lushbinary.com/blog/rag-retrieval-augmented-generation-production-guide/
- Hybrid search BM25 + pgvector reference: https://github.com/Syed007Hassan/Hybrid-Search-For-Rag
- pgvector: https://github.com/pgvector/pgvector
- Jina Reranker v2 (self-hosted, ~400ms, 15–30% quality lift): https://jina.ai/reranker/
- ColBERT v2 (alternative late-interaction reranker, future): https://github.com/stanford-futuredata/ColBERT
- Spec sections: `docs/FORGE_SPEC.md` → "Knowledge and Retrieval Architecture" (pipeline, chunk types/weights, sync modes), Technology Stack rows (pgvector cosine, Postgres BM25, RRF k=60, Jina Reranker v2), "Core Data Model" (KnowledgeSource/RetrievalChunk), Phase 1 roadmap item "Knowledge sync: repos → hybrid pgvector + BM25 + Jina Reranker v2".
- Research: `docs/forge-research-report.md` → "Hybrid Retrieval: Code-Aware RAG" (RRF formula, k=60, weighted hybrid 5×/3×/0.2× example, pgvector under 1M vectors).

## 12. Out of scope / future

- **MCP sync-and-index mode** (periodic pull of MCP resources into the local index) — Phase 2 roadmap; V1 supports MCP `query_through` only.
- **True BM25 via ParadeDB `pg_search`** — V2 swap behind `KeywordSearcher`; V1 uses native Postgres FTS (`ts_rank_cd`).
- **ColBERT/token-level late-interaction reranking** and recency-decay scoring as a tunable RRF leg — future tuning once the golden eval set guides it.
- **Cross-encoder/embedding A/B evaluation harness UI** — owned by the Observability/Evaluation slice; F05 only emits the metrics (reranker delta, freshness lag, latency p50/p95/p99).
- **Multi-repo retrieval merging at task scope** — Phase 2 ("Multi-repo task execution"); V1 retrieves across whatever sources are in `KnowledgeScope` but multi-repo task orchestration is elsewhere.
- **Dedicated vector DB** (e.g., Qdrant/pgvector→external) — only if the corpus exceeds ~1M vectors per the research guidance.
- **Approval UI provenance panel** — consumes F05's `source_uri`/`metadata`; built in `v1/F08-plan-execute-verify-pr-approval` (Review & Approval Layer).
