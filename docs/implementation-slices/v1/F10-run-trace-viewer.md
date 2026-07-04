# F10 — Run Trace Viewer (step-level inspection)

> Phase: v1 · Spec module(s): Observability Layer (run traces, token/cost logs, task lineage), Core Data Model (`AgentRun.steps[]`), Review & Approval Layer ("Approval UI Must Show" item 8 — full run trace), Workflow Engine (consumes F07 `workflow_transition` audit log), Security (secret redaction, immutable audit, tenant isolation) · Status target: **Done** = the step rows that the execution agent (F06) writes to `agent_steps` are merged with the FSM `workflow_transition` rows (F07) into one ordered **unified timeline** that is queryable via authenticated REST and renders in a keyboard-navigable timeline UI; steps **stream live over SSE** while a run is active and **replay deterministically** from Postgres after it ends; each step exposes node/type/status/timing/token-cost and redacted input/output (full overflow payloads, already spilled to MinIO by F06, served behind short-TTL signed URLs); secrets are re-asserted as `[REDACTED]` on every read surface; `viewer` role can read but never mutate; every query is workspace-isolated; and the `RunTraceTimeline` React component is reusable by F08's PR approval review page. Lint + types + `pytest` green on the new `forge_workflow.trace` module, the `apps/api` trace router, and the worker trace-sink wiring; new-code coverage ≥ 80%.

---

## 1. Intent — what & why

The Forge core data model defines `AgentRun.steps[]` ("tool calls, decisions, outputs") and the Observability Layer is responsible for "run traces, token/cost logs, task lineage, retrieval debug." The Observability & Evaluation section requires "**Replayable workflow runs with step-level inspection**," and the Human Approval System's "Approval UI Must Show" list requires (item 8) a "**Full run trace with step-by-step actions taken**." This slice builds the **read/stream/viewer** for that trace.

**Ownership boundary (important — this is a read-side slice).** Two sibling slices already own the *write* side and the underlying tables:

- **F06 (`v1/F06-single-execution-agent`) owns the step record.** F06 creates the `agent_steps` table, defines the `Step` shape and `step_type` vocabulary, and writes one append-only `agent_steps` row per step through its injected `StepSink` (`RuntimeDeps.step_sink`). F06 also performs **write-time** secret redaction, truncates `output` to 64 KB, and spills overflow to MinIO refs in `agent_steps.artifacts`. F06 explicitly labels `agent_steps` "the high-volume, step-level *trace* source consumed by the run-trace viewer `v1/F10-run-trace-viewer`" and exposes `GET /api/v1/agent-runs/{agent_run_id}` (run + ordered `steps[]`) that "feeds F10 viewer."
- **F07 (`v1/F07-feature-workflow-fsm`) owns the FSM audit log.** F07 creates the append-only `workflow_transition` table and the `workflow_run` header (`current_state`, terminal detection via `TERMINAL_STATES`). F07 states `GET /workflow/runs/{id}` + `GET /workflow/runs/{id}/transitions` "feed the unified timeline and run-trace viewer."

F10 therefore owns exactly three things the spec mandates but neither F06 nor F07 implements:

1. **The unified read model (`TraceAssembler`).** A run-level timeline that deterministically merges `agent_steps` rows (read from F06) with `workflow_transition` rows (read from F07) into one ordered sequence, plus per-step detail with re-asserted redaction, plus token/cost rollups per run, per workflow phase, and per model provider. Served paginated and workspace-isolated.
2. **Live delivery (`TracePublisher`/`TraceSubscriber` + SSE).** While a run is active, steps stream over **SSE** (the spec's real-time channel for "run traces"); after a run terminates, the same trace **replays** deterministically from Postgres. F10 supplies a thin `PublishingStepSink` decorator that wraps F06's `StepSink` so each persisted step is also published to Redis — without changing F06's package — plus a `publish_transition` hook F07's engine calls on commit (anticipated by F07, which says transitions "feed the run-trace viewer").
3. **The viewer UI.** The `RunTraceViewer` page and the reusable `RunTraceTimeline` component render the unified timeline keyboard-first, with redaction visible as `[REDACTED]`.

Why it matters: a trace viewer is what makes an autonomous, policy-gated agent **trustable and auditable**. Reviewers approve PRs partly by reading the trace (F08 embeds `RunTraceTimeline`); operators debug failed/escalated runs through it; the evaluation harness (F12) shares the same "replayable, step-level" guarantee. Without F10, the agent's reasoning and tool usage are persisted (by F06/F07) but never assembled, streamed, or rendered for a human.

**F10 does NOT** create the `agent_steps` table, define the step write contract, redact at write time, or spill payloads (all F06); does NOT own the `workflow_run`/`workflow_transition` tables or FSM logic (F07); and does NOT implement the central tamper-evident `audit_log` (`cross-cutting/F39-audit-log`). F10 reads those sources, merges them, streams them, and renders them.

---

## 2. User-facing behavior / journeys

**Journey A — Live trace of an executing task (engineer):**
1. An engineer opens `TASK-123`'s active run. The **Run Trace** tab shows a live, auto-scrolling timeline with a pulsing "Live" badge (badge state derived from F07 `current_state ∉ TERMINAL_STATES`).
2. As the agent works, rows append in real time as F06 writes `agent_steps`: `decision plan`, `tool_call read_repo (app/middleware/auth.py)` (ok, 12 ms), `message model.completion` (ok, 1.8 s, token usage), `tool_call write_code (app/api/customers.py)`, `verification run_tests` (error). Each row shows a node/type icon, name, status, duration (`latency_ms`), and (where present) token/cost from `token_usage`.
3. The header shows running totals: elapsed time, total tokens, total cost, step count, current FSM state (from F07).
4. The engineer clicks a step; a detail panel slides in with the redacted `input`/`output` JSON, the tool/action name, the `policy_decision` (for `tool_call` steps), and a "Download full payload" link when F06 spilled an overflow ref into `artifacts`.

**Journey B — Replay a completed run (reviewer, inside F08 approval page):**
- On the PR approval review page, the embedded `RunTraceTimeline` (mounted `{ workflowRunId, live: false }`) shows the entire trace from Postgres (no live connection). The reviewer scrolls the step-by-step actions to satisfy the spec criteria, expands the `decision plan` step that produced the plan, and confirms the agent read the policy file before writing code. The trace is read-only.

**Journey C — Debug an escalated / failed run (operator):**
- A run routed to `needs_human_input`. The operator opens the trace, filters to `status = error` and `type = verification`, and finds `run_tests` failed three times with the same assertion error; the verification step's `artifacts` ref points to the full pytest log in MinIO (the object F06 spilled). The unified timeline interleaves the FSM transitions (`verifying → executing` retry, `verifying → needs_human_input`) so the operator sees exactly when and why escalation happened.

**Journey D — Cost & lineage inspection:**
- A team lead opens the run's **Cost** breakdown: total $0.37, split by workflow phase (`spec_drafting`, `executing`, `verifying` — derived by attributing each step's cost to the FSM state active at the step's `created_at`) and by model provider (the run's `ModelConfig` model id). Task lineage links the run back to its task (F01), spec (F02), and PR (F08).

**Journey E — Permissions:**
- A `viewer` can open and read any trace in their workspace but sees no mutating controls and cannot hit any write endpoint (there are none in F10). A user from another workspace requesting the run id gets a 404 (no existence leak).

**Journey F — Secret redaction:**
- A tool step's input echoed an API key. F06 already redacted it at write time, so the stored `input_json` and the spilled MinIO payload are clean; F10 re-asserts the shared `SecretRedactor` at read time so the timeline, the detail panel, and the signed-URL download all render `[REDACTED]`.

---

## 3. Vertical slice

### 3.1 Data model (tables/columns/migrations touched)

**F10 adds NO new table.** The step record is `agent_steps` (created by `v1/F06-single-execution-agent`); the FSM event log is `workflow_transition` (created by `v1/F07-feature-workflow-fsm`). F10 reads both.

**Read source `agent_steps` (owned by F06 — F10 reads only).** Columns F10 consumes:
`id`, `agent_run_id` (FK→`agent_runs.id`), `seq` (`int`, monotonic per `agent_run`, unique `(agent_run_id, seq)`), `node` (`load_context|plan|act|observe|verify|finalize`), `step_type` (`decision|tool_call|tool_result|message|verification|interrupt|error`), `tool_name`, `action`, `input` (jsonb, **already redacted by F06**), `output` (jsonb, **redacted + truncated to 64 KB**, overflow ref in `artifacts`), `status` (`ok|denied|error`), `policy_decision` (jsonb `{allowed, reason, requires_approval}`), `artifacts` (jsonb list of MinIO refs — raw logs, diffs, output overflow), `latency_ms`, `token_usage` (jsonb `{prompt, completion, total, cost_usd}`), `created_at`. F06's existing index `ix_agent_steps_run_seq (agent_run_id, seq)` is the primary ordering path. The run-level fields (`workflow_run_id`, `workspace_id`, run `confidence`, run `token_usage`, model) come from `agent_runs` via the `agent_steps.agent_run_id → agent_runs.id` join.

**Read source `workflow_transition` (owned by F07 — F10 reads only).** Columns: `id`, `workflow_run_id`, `sequence` (`int`, monotonic per run from 1), `from_state`, `to_state`, `event`, `guard_results`, `effects_dispatched`, `record`, `actor` (`system|user:<uuid>|agent:<uuid>`), `payload` (jsonb, **redacted by F07**), `created_at`. Read via F07's `WorkflowRunRepository.history(run_id)` / `GET /workflow/runs/{id}/transitions` or a direct read-only query.

**Read source `workflow_run` + `agent_runs` (owned by F07/F06 — F10 reads only).** `workflow_run.current_state` + F07's `TERMINAL_STATES` give `is_live`. `agent_runs` gives the run's `workspace_id` (tenant isolation), `workflow_run_id` (run-level grouping), `confidence`, `token_usage`, and model id.

**F10's only DB change — one additive, reversible index migration.** File `packages/db/migrations/versions/<rev>_trace_read_indexes.py`, `down_revision` = the F06 migration that creates `agent_steps` (`0006_agent_runtime`). It creates **indexes only** (no columns, no tables, no constraints that mutate F06 data) to support the filter read path (Journey C):
- `ix_agent_steps_run_type (agent_run_id, step_type)`
- `ix_agent_steps_run_status (agent_run_id, status)`

`downgrade()` drops exactly these two indexes. If F06 chooses to ship these indexes itself, this migration becomes a no-op and is dropped — coordinate via the shared Alembic history.

**Tenant isolation:** F10 has no denormalized `workspace_id`; every query joins `agent_steps → agent_runs` and `workflow_transition → workflow_run` and filters on `agent_runs.workspace_id` / `workflow_run.workspace_id`. Cross-workspace ids return 404 (no existence leak).

### 3.2 Backend (FastAPI routes + services/packages)

**Package `packages/workflow-engine/forge_workflow/trace/`** (framework-agnostic; importable by both `apps/api` and `apps/worker`, mirroring F08's dual-import pattern):

- `models.py` — Pydantic read models/enums: `StepType`, `StepStatus`, `TraceStep`, `TimelineTransition`, `TimelineEntryKind`, `TimelineEntry`, `RunTrace`, `RunCostSummary`, `TraceStreamEvent` (§4). Reuses `forge_contracts` `TokenUsage` (F06) rather than redefining it.
- `assembler.py` — `TraceAssembler.assemble(...) -> RunTrace`: deterministic merge of `agent_steps` and `workflow_transition` into one list ordered by `(created_at, kind_rank, seq)` with stable tie-breaking; computes `RunCostSummary` (totals + per-phase via FSM-interval attribution + per-model); keyset pagination cursor encode/decode. Pure and unit-testable.
- `repository.py` — `TraceReadRepository`: workspace-scoped reads (run header, steps page, transitions, single step). No write methods.
- `redaction.py` — `reassert_redaction(value) -> Any`: read-time defense-in-depth pass over already-redacted payloads using the shared `SecretRedactor` (from `cross-cutting/F37-auth-secrets-byok`, the same instance F08/F09 reuse). F10 does **not** truncate or spill (F06 owns that).
- `stream.py` — `TracePublisher.publish(event)` (Redis `PUBLISH forge:trace:{workflow_run_id}`), `publish_transition(...)` (called by F07's engine on commit), `TraceSubscriber.subscribe(workflow_run_id, after_cursor) -> AsyncIterator[TraceStreamEvent]` (Redis pub/sub + initial DB backfill for `Last-Event-ID` resume), and `PublishingStepSink` (the decorator described in §3.3).

**App `apps/api/app/`:**

- `api/v1/trace.py` — router mounted at `/api/v1` (auth + workspace membership required; `viewer`+ may read all endpoints). Endpoints in §4.
- `services/trace_service.py` — `TraceService`: wires `TraceReadRepository` + `TraceAssembler` + the foundation `ArtifactStore` signed-URL minting + `TraceSubscriber`; enforces workspace isolation (404 on cross-workspace id) and read-only access.
- `schemas/trace.py` — API response schemas (re-export/adapt the `forge_workflow.trace` models for the OpenAPI surface).

Repositories operate over the shared async SQLAlchemy session (foundation slice). No ORM model is created by F10; reads use F06's `agent_steps`/`agent_runs` and F07's `workflow_transition`/`workflow_run` ORM models already defined in `packages/db/forge_db/models/`.

### 3.3 Worker / agent runtime (Celery tasks, LangGraph)

F10 contributes **no new Celery task** and **no write to `agent_steps`** (F06 writes it). F10 contributes the live-publish seam:

- **`PublishingStepSink`** (F10, in `forge_workflow.trace.stream`) — a decorator implementing F06's `StepSink` Protocol. The worker composes the trace publisher around F06's persistence sink at `RuntimeDeps` wiring time: `step_sink = PublishingStepSink(inner=<F06 persistence sink>, publisher=TracePublisher(redis))`. On each call, it delegates to `inner` (F06 persists the redacted, truncated `agent_steps` row), then publishes a `TraceStreamEvent(kind="step", ...)` to Redis. This requires **zero edits to F06's package** — it is composition at the shared worker wiring layer.
- **`publish_transition`** (F10) — F07's `PostgresWorkflowEngine` calls `publisher.publish_transition(workflow_run_id, transition)` after committing a `workflow_transition` row, emitting a `TraceStreamEvent(kind="transition", ...)`. F07 already declares that transitions "feed the run-trace viewer," so this is the documented seam. If the hook is not yet wired, the unified timeline remains fully correct via the authoritative DB-replay `GET .../trace` and SSE reconnect-backfill (which read `workflow_transition` directly); only the *live push* of a transition lags until the next backfill.
- **Resilience:** publishing is best-effort — `TracePublisher` swallows Redis errors and logs, so a streaming failure never affects F06's persistence or the agent run. Persistence correctness is F06's guarantee; F10 only adds an optional live fan-out.

No LangGraph graph definition is owned here (F06).

### 3.4 Frontend / UI (Next.js routes/components)

**App `apps/web/`:**

- Route `app/(dashboard)/projects/[projectId]/tasks/[taskId]/runs/[runId]/page.tsx` — the **Run Trace** page: header (state, elapsed, totals, cost), the timeline, the detail panel, and filter controls.
- `components/trace/RunTraceTimeline.tsx` — **the reusable component** (F08's approval review page imports this for "Full run trace," item 8). Props: `{ workflowRunId: string; live?: boolean; height?: number }`. Renders ordered `TimelineEntry[]`, virtualized for large traces (TanStack Virtual / windowed list), with a live SSE subscription when `live` and the run is active, falling back to DB replay otherwise.
- `components/trace/StepRow.tsx` — one row: node/type icon, name (`tool_name`/`action`/`node`), status chip (`ok|denied|error`), duration (`latency_ms`), token/cost badge from `token_usage`.
- `components/trace/StepDetailPanel.tsx` — redacted `input`/`output` JSON viewer, tool/action metadata, `policy_decision`, "Download full payload" (signed URL) when an overflow `artifacts` ref is present.
- `components/trace/CostSummary.tsx` — totals + per-phase + per-model breakdown.
- `components/trace/TraceFilters.tsx` — filter by `type` (`step_type`), `status`, free-text name match (client-side over loaded page + server param for deep filter).
- `components/trace/LiveBadge.tsx` — pulsing indicator; flips to "Completed" when the `run_terminal` event arrives or `current_state ∈ TERMINAL_STATES`.
- Data layer: `lib/api/trace.ts` (typed client matching §4) + hooks `useRunTrace(runId)` (TanStack Query, keyset-paginated), `useStep(stepId)`, `useRunCost(runId)`, and `useTraceStream(runId)` (an `EventSource` wrapper that appends incoming `TraceStreamEvent`s into the TanStack Query cache and reconnects with `Last-Event-ID`).

Keyboard-first (board UX standard): `j`/`k` move selection between entries, `o`/`Enter` opens the detail panel, `Esc` closes it, `f` focuses the filter, `g` jumps to the latest entry (and re-enables follow/auto-scroll when live). No mouse required.

### 3.5 Infra / deploy (compose, helm, caddy)

- **Redis pub/sub** (already in the stack) carries live trace events on channel `forge:trace:{workflow_run_id}`; no new service. Document the channel in `docs/architecture/`.
- **MinIO — no new bucket.** Overflow step payloads are already spilled by F06 into the foundation `ArtifactStore` (refs in `agent_steps.artifacts`). F10 only **mints short-TTL signed URLs** over those existing refs; it neither uploads nor owns a bucket.
- **Caddy / reverse proxy:** the SSE stream path needs response buffering disabled. Following the convention F01 established for `/api/v1/projects/*/stream`, add a matcher block in `deploy/caddy/Caddyfile` (and the Nginx alternative `deploy/nginx/forge.conf`) for `/api/v1/workflow-runs/*/trace/stream` setting `flush_interval -1` (disable buffering) and a read/idle timeout ≥ the SSE heartbeat interval (e.g. 1h). The endpoint emits a `: keepalive\n\n` comment every 15 s to defeat idle timeouts.
- **No new compose service.** The SSE endpoint holds one long-lived connection per open trace tab; document a per-worker connection cap (reuse the F37 Redis-backed connection limiter F01 uses for its board stream) and that horizontal API scaling works because subscriptions are Redis-backed (any API replica can serve any run's stream).

Helm: N/A — V1 ships Docker Compose only (Helm is V2).

---

## 4. Public interfaces / contracts

**Trace read models** (`packages/workflow-engine/forge_workflow/trace/models.py`) — shaped to F06's `agent_steps` and F07's `workflow_transition`:

```python
from __future__ import annotations
from enum import Enum
from uuid import UUID
from datetime import datetime
from typing import Any, Literal
from pydantic import BaseModel
from forge_contracts import TokenUsage   # F06: {prompt, completion, total, cost_usd}

class StepType(str, Enum):                 # mirrors F06 agent_step_type exactly
    DECISION = "decision"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    MESSAGE = "message"
    VERIFICATION = "verification"
    INTERRUPT = "interrupt"
    ERROR = "error"

class StepStatus(str, Enum):               # mirrors F06 agent_step_status exactly
    OK = "ok"
    DENIED = "denied"
    ERROR = "error"

class PolicyDecision(BaseModel):           # mirrors F06/F04 Decision
    allowed: bool
    reason: str
    requires_approval: bool = False

class TraceStep(BaseModel):
    """Read model for one persisted agent_steps row (input/output already redacted by F06)."""
    id: UUID
    agent_run_id: UUID
    workflow_run_id: UUID
    seq: int                               # monotonic per agent_run (F06)
    node: str                              # load_context|plan|act|observe|verify|finalize
    type: StepType                         # agent_steps.step_type
    name: str                              # display name: tool_name | action | node
    status: StepStatus
    tool_name: str | None
    action: str | None
    input: dict[str, Any]                  # redacted
    output: dict[str, Any]                 # redacted; truncated if output_truncated
    output_truncated: bool                 # true iff an overflow ref exists in artifacts
    artifact_refs: list[str]               # MinIO object keys (F06 spilled)
    policy_decision: PolicyDecision | None
    latency_ms: int | None
    tokens: TokenUsage | None              # per-step token_usage
    created_at: datetime
    cursor: str                            # keyset cursor for this entry

class TimelineTransition(BaseModel):
    """Normalized read model for one workflow_transition row (F07)."""
    id: UUID
    workflow_run_id: UUID
    sequence: int
    from_state: str
    to_state: str
    event: str
    actor: str                             # system | user:<uuid> | agent:<uuid>
    record: str | None                     # e.g. approval_event
    created_at: datetime
    cursor: str

class TimelineEntryKind(str, Enum):
    STEP = "step"
    TRANSITION = "transition"

class TimelineEntry(BaseModel):
    kind: TimelineEntryKind
    at: datetime                           # ordering key (created_at)
    cursor: str
    step: TraceStep | None = None
    transition: TimelineTransition | None = None

class RunCostSummary(BaseModel):
    workflow_run_id: UUID
    total_cost_usd: float
    total_prompt_tokens: int
    total_completion_tokens: int
    by_phase: dict[str, float]             # FSM state -> cost_usd (interval attribution)
    by_model: dict[str, float]             # model id -> cost_usd
    step_count: int

class RunTrace(BaseModel):
    workflow_run_id: UUID
    agent_run_ids: list[UUID]
    current_state: str                     # F07 workflow_run.current_state
    is_live: bool                          # current_state not in F07.TERMINAL_STATES
    entries: list[TimelineEntry]
    cost: RunCostSummary
    next_cursor: str | None                # keyset cursor for the next page (None = end)

class TraceStreamEvent(BaseModel):
    """Published to Redis and emitted over SSE."""
    kind: Literal["step", "transition", "run_terminal"]
    workflow_run_id: UUID
    cursor: str                            # SSE id / Last-Event-ID value
    step: TraceStep | None = None
    transition: TimelineTransition | None = None
    current_state: str | None = None       # set on run_terminal
```

**Consumed write contract (owned by F06 — F10 only decorates it):**

```python
# forge_contracts (F06): F10 implements StepSink as a pass-through publisher only.
class StepSink(Protocol):
    async def record(self, agent_run_id: UUID, step: "Step") -> UUID: ...  # returns step id

class PublishingStepSink:                  # F10: forge_workflow.trace.stream
    def __init__(self, *, inner: StepSink, publisher: "TracePublisher") -> None: ...
    async def record(self, agent_run_id: UUID, step: "Step") -> UUID:
        step_id = await self.inner.record(agent_run_id, step)   # F06 persists + redacts + spills
        await self.publisher.try_publish_step(agent_run_id, step_id)  # best-effort, swallows errors
        return step_id
```

**Assembler / read repository (F10-owned):**

```python
class TraceAssembler:
    def assemble(
        self, *, run: WorkflowRunDTO, steps: list[TraceStep],
        transitions: list[TimelineTransition], cursor: str | None, limit: int,
        terminal_states: frozenset[str],
    ) -> RunTrace: ...
    def cost_summary(
        self, *, steps: list[TraceStep], transitions: list[TimelineTransition],
        run_models: dict[UUID, str],
    ) -> RunCostSummary: ...

class TraceReadRepository(Protocol):
    async def load_run(self, workflow_run_id: UUID, *, workspace_id: UUID) -> WorkflowRunDTO | None: ...
    async def list_steps(self, workflow_run_id: UUID, *, workspace_id: UUID,
                         cursor: str | None, limit: int,
                         type: StepType | None = None, status: StepStatus | None = None,
                         ) -> tuple[list[TraceStep], str | None]: ...
    async def list_agent_run_steps(self, agent_run_id: UUID, *, workspace_id: UUID,
                                   cursor: str | None, limit: int) -> tuple[list[TraceStep], str | None]: ...
    async def list_transitions(self, workflow_run_id: UUID, *, workspace_id: UUID) -> list[TimelineTransition]: ...
    async def get_step(self, step_id: UUID, *, workspace_id: UUID) -> TraceStep | None: ...
```

**Streaming (F10-owned):**

```python
class TracePublisher:
    def __init__(self, *, redis: Redis, repo: TraceReadRepository) -> None: ...
    async def try_publish_step(self, agent_run_id: UUID, step_id: UUID) -> None: ...      # best-effort
    async def publish_transition(self, workflow_run_id: UUID, transition_id: UUID) -> None: ...  # called by F07
    async def publish_terminal(self, workflow_run_id: UUID, current_state: str) -> None: ...

class TraceSubscriber:
    def subscribe(self, workflow_run_id: UUID, *, after_cursor: str | None
                  ) -> AsyncIterator[TraceStreamEvent]: ...   # DB backfill after cursor, then tail Redis
```

**REST API (all under `/api/v1`, all authenticated; `viewer`+ may read; no mutating endpoints):**

```
GET /workflow-runs/{run_id}/trace?cursor=<c>&limit=200&type=<t>&status=<s>
        -> RunTrace                          # unified timeline, keyset-paginated
GET /agent-runs/{agent_run_id}/steps?cursor=<c>&limit=200
        -> { items: TraceStep[], next_cursor: str | null }   # complements F06 GET /agent-runs/{id}
GET /steps/{step_id}
        -> TraceStep                          # full redacted inline payloads
GET /steps/{step_id}/payload?which=output|input
        -> 302 redirect to short-TTL signed MinIO URL   # only if overflow artifact ref present, else 404
GET /workflow-runs/{run_id}/cost
        -> RunCostSummary
GET /workflow-runs/{run_id}/trace/stream      (SSE; header Accept: text/event-stream)
        -> stream of TraceStreamEvent          # id: <cursor>; supports Last-Event-ID resume
```

SSE framing: each message is `id: <cursor>\nevent: <kind>\ndata: <TraceStreamEvent JSON>\n\n`; a `: keepalive\n\n` comment every 15 s; the stream backfills from Postgres for any `Last-Event-ID` cursor gap, then tails Redis; it closes after emitting `run_terminal`. For a run already terminal at connect time, the endpoint replays all entries from the DB and closes (deterministic replay identical to `GET .../trace`).

**Cursor:** opaque base64 of `(created_at_iso, kind_rank, seq_or_sequence)` where `kind_rank` orders a `step` and a `transition` sharing an instant deterministically; keyset pagination and SSE resume both order by `(created_at, kind_rank, seq)` ascending. Idempotent and stable under appends.

**Consumed contracts (owned elsewhere):**
- `Step` shape, `agent_steps` table, `StepSink` Protocol, `TokenUsage`, run-level `confidence`/`token_usage`/model — `v1/F06-single-execution-agent`.
- `WorkflowRunDTO`, `WorkflowTransitionDTO`, `workflow_transition`/`workflow_run`, `TERMINAL_STATES`, `WorkflowRunRepository.history` — `v1/F07-feature-workflow-fsm`.
- `ArtifactStore` (put/get/signed-url), shared `SecretRedactor`, `Principal`/`require_role(...)`, async session, Redis client — foundation + `cross-cutting/F37-auth-secrets-byok`.

---

## 5. Dependencies — features/slices that must exist first

- `v1/F00-foundation-substrate` — **hard.** Workspace/User, auth + RBAC dependency (`viewer`/`member`/`admin`/`agent-runner`), async SQLAlchemy session + Alembic baseline, Redis client, MinIO `ArtifactStore` + signed URLs, SSE/streaming plumbing in FastAPI, `packages/contracts`. (Slug is authoritative; referred to elsewhere as `cross-cutting/F00-foundation`.)
- `v1/F06-single-execution-agent` — **hard.** Owns the `agent_steps` table, the `Step`/`StepSink`/`TokenUsage` contracts, write-time redaction + 64 KB truncation + MinIO overflow, and the per-run `seq`. F10 reads `agent_steps` and decorates `StepSink`. (F10 can be **built and tested independently** by seeding `agent_runs`/`agent_steps` rows directly; F06 must exist for end-to-end live traces.)
- `v1/F07-feature-workflow-fsm` — **hard.** Owns `workflow_run` + the `workflow_transition` audit log that F10 reads to build the unified timeline; provides `current_state`, `TERMINAL_STATES`, `WorkflowTransitionDTO`, and the `publish_transition` seam.
- `cross-cutting/F37-auth-secrets-byok` — **hard.** Provides the shared `SecretRedactor` reused by `redaction.py`, `require_role(...)`/`Principal` route auth, the BYOK model price/cost source behind `token_usage.cost_usd`, and the Redis connection limiter.
- `v1/F08-plan-execute-verify-pr-approval` — **soft (bidirectional).** F08 *consumes* F10's `RunTraceTimeline` in the approval review page (item 8) and its `VerificationChecklist` subscribes to F10/F07 run events; F08 explicitly lists F10 as a soft dependency with a stub fallback. Neither blocks the other to start.
- `v1/F05-hybrid-knowledge-retrieval` — **soft.** Retrieval surfaces as `read_knowledge` `tool_call` steps F06 records; if F05 lags, those steps simply do not appear.
- `v1/F09-mcp-gateway-v1` — **soft.** MCP calls surface as `query_mcp` `tool_call` steps F06 records; the authoritative MCP audit (tool name, payload hash, result status, latency) lives in F09's own immutable audit log, not in F10.
- `cross-cutting/F39-audit-log` — **soft.** Owns the central tamper-evident `audit_log` and the reusable `attach_immutability_trigger(table)` helper that `agent_steps`/`workflow_transition` adopt; F10 reads the trace sources, not the central audit log.

---

## 6. Acceptance criteria (numbered, testable)

1. `GET /workflow-runs/{run_id}/trace` returns a `RunTrace` whose `entries` merge `agent_steps` rows and `workflow_transition` rows, ordered ascending by `(created_at, kind_rank, seq)` with stable tie-breaking; `current_state` is read from F07's `workflow_run`; `is_live == (current_state not in TERMINAL_STATES)`.
2. Each `TraceStep` in the response maps F06's `agent_steps` columns exactly: `type` == `step_type` (one of the 7 values), `status` == one of `ok|denied|error`, `name` resolves to `tool_name | action | node`, and `latency_ms`/`tokens`/`policy_decision`/`artifact_refs` are populated from the row.
3. `RunCostSummary.total_cost_usd` equals the sum of per-step `token_usage.cost_usd`; `total_prompt_tokens`/`total_completion_tokens` equal the summed token counts; `step_count` equals the number of `agent_steps` rows.
4. `by_phase` attributes each step's cost to the FSM state active at the step's `created_at`, derived from the `workflow_transition` interval sequence; a step before the first transition is attributed to the run's initial state.
5. `by_model` attributes each agent run's step costs to that run's `ModelConfig` model id (single model per `agent_run` in V1); a run with no recorded model is bucketed under `"unknown"`.
6. Keyset pagination: requesting with `limit=N` returns ≤ N entries plus a `next_cursor`; following the cursor returns the next contiguous page with no gaps or duplicates; the final page returns `next_cursor=null`.
7. Read-time redaction: any value matching the shared secret-pattern set is rendered `[REDACTED]` in `input`/`output` and in any error/payload field, verified for nested dict/list structures, even though F06 already redacted at write time (defense-in-depth).
8. `GET /steps/{step_id}` returns the redacted step with full inline payloads when not truncated; `output_truncated` is true exactly when an overflow ref exists in `artifact_refs`.
9. `GET /steps/{step_id}/payload?which=output` returns a 302 to a short-TTL signed MinIO URL over the existing F06 overflow ref when present, else 404; the signed URL is workspace-scoped.
10. `GET /workflow-runs/{run_id}/cost` returns totals plus `by_phase` and `by_model` breakdowns consistent with `GET .../trace` for the same run.
11. RBAC: a `viewer` can read every F10 endpoint (200); there is **no** F10 endpoint that mutates data; an authenticated user from another workspace requesting any `run_id`/`step_id` gets **404** (no existence leak), not 403.
12. `PublishingStepSink.record` delegates to F06's inner `StepSink` (the step is persisted exactly once by F06, returning its id) and then publishes one `TraceStreamEvent(kind="step")`; a Redis failure in `try_publish_step` is swallowed and the persisted id is still returned (publish never breaks the run).
13. Filtering: `GET .../trace?type=verification&status=error` returns only matching `agent_steps` entries (transitions excluded when a `type`/`status` filter is set), using the `(agent_run_id, step_type)` / `(agent_run_id, status)` indexes.
14. SSE live: a client connected to `/workflow-runs/{run_id}/trace/stream` receives a `TraceStreamEvent(kind="step")` for each `agent_steps` row written after connect, in order, each carrying `id: <cursor>`; a `run_terminal` event is emitted when `current_state` enters `TERMINAL_STATES`, after which the stream closes.
15. SSE resume: reconnecting with `Last-Event-ID: <cursor>` backfills only entries after that cursor from Postgres (both `agent_steps` and `workflow_transition`) before tailing Redis, producing no duplicate and no missing entries.
16. SSE replay of a completed run: connecting to the stream of an already-terminal run replays all entries from Postgres in order and then closes — an entry sequence identical to the paginated `GET .../trace` (deterministic replay).
17. Determinism: assembling the same terminal run twice yields byte-identical `entries` and `cost` (the assembler is pure; ordering is total via the cursor key).
18. The `RunTraceTimeline` component renders the merged timeline, shows a live badge that flips to "Completed" on `run_terminal`, renders redacted values as `[REDACTED]`, and supports `j/k` navigation + `o/Enter` to open and `Esc` to close the detail panel; it mounts read-only with `{ workflowRunId, live: false }` (the F08 embedding contract).

### 6.x Definition of Done
All ACs covered by passing tests; an integration test seeds a multi-step run (real Postgres testcontainer with the F06 `agent_steps`, F07 `workflow_transition`, and F10 index migrations + a real Redis + a fake/MinIO `ArtifactStore`), drives a fake producer through `PublishingStepSink`, then asserts the paginated trace, the SSE live stream, and the deterministic replay all agree; new-code coverage ≥ 80% (the `backend-tdd` bar Forge applies to itself); the component test suite passes under React Testing Library.

---

## 7. Test plan (TDD) — concrete test cases (unit + integration), key fixtures

Write tests first. Layout: `packages/workflow-engine/tests/trace/` (unit), `apps/api/tests/` (API + SSE), `apps/worker/tests/` (sink wiring), `apps/web/__tests__/` (component).

**Key fixtures:**
- `pg` — Postgres testcontainer + `alembic upgrade head` (includes the F06 `agent_steps`, F07 `workflow_transition`, and F10 index migrations).
- `redis` — real Redis (testcontainer or fakeredis with pub/sub) for `TracePublisher`/`TraceSubscriber`.
- `fake_artifacts` — in-memory `ArtifactStore` recording puts and minting deterministic signed URLs (stands in for the objects F06 spilled).
- `seed_run` — factory inserting `workflow_run` + `agent_runs` + a chosen sequence of `agent_steps` and `workflow_transition` rows at a chosen state, returning ids. Includes a `secretful_step` variant with API-key-shaped strings nested in `input`/`output` and an overflow `artifacts` ref.
- `inner_sink` — a fake F06 `StepSink` that records into `agent_steps`; `publishing_sink` — `PublishingStepSink(inner=inner_sink, publisher=...)`.

**Unit — assembler (`tests/trace/test_assembler.py`):**
- `test_merges_steps_and_transitions_in_order` — interleaved `agent_steps` + `workflow_transition` ordered by `(created_at, kind_rank, seq)`, stable ties; AC#1.
- `test_step_field_mapping` — `type`/`status`/`name`/`latency_ms`/`tokens`/`policy_decision`/`artifact_refs` map from the row; AC#2.
- `test_cost_totals` — totals/prompt/completion/step_count from mixed steps; AC#3.
- `test_cost_by_phase_interval_attribution` — steps attributed to the FSM state active at `created_at` using transition intervals; pre-first-transition step → initial state; AC#4.
- `test_cost_by_model` — per-run model attribution; missing model → `"unknown"`; AC#5.
- `test_pagination_cursor_roundtrip` — slicing by cursor yields contiguous, gapless pages; final `next_cursor=null`; AC#6.
- `test_is_live_from_terminal_states` — `is_live` flips off for states in `TERMINAL_STATES`; AC#1.
- `test_assemble_is_byte_stable` — assembling the same terminal run twice yields identical `entries`+`cost`; AC#17.

**Unit — redaction (`tests/trace/test_redaction.py`):**
- `test_reasserts_nested_secrets` — values matching the shared secret set become `[REDACTED]` at any depth (dict in list in dict); AC#7.

**Unit — publishing sink (`tests/trace/test_publishing_sink.py`):**
- `test_delegates_and_publishes` — `record` calls `inner.record` once, returns its id, publishes one `step` event; AC#12.
- `test_publish_failure_is_swallowed` — a raising Redis publisher does not propagate; persisted id still returned; AC#12.

**API tests (`apps/api/tests/test_trace_api.py`):**
- `test_get_trace_unified_timeline` — seeded run returns merged entries + cost; AC#1/#3.
- `test_pagination_endpoints` — `limit`/`cursor` over `/trace` and `/agent-runs/{id}/steps`; AC#6.
- `test_filter_type_status` — `type`/`status` filter returns only matching step entries; AC#13.
- `test_get_step_and_signed_payload` — inline step; `payload?which=output` 302 when overflow ref present, 404 when not; AC#8/#9.
- `test_cost_endpoint_matches_trace` — per-phase/per-model breakdown consistent with `/trace`; AC#10.
- `test_viewer_can_read_no_mutation_endpoints` — `viewer` 200 on all reads; the router exposes no write verb; AC#11.
- `test_cross_workspace_is_404` — foreign `run_id`/`step_id` → 404; AC#11.

**SSE tests (`apps/api/tests/test_trace_stream.py`):**
- `test_live_step_events_in_order` — record steps after connect via `PublishingStepSink`; client receives `step` events with `id: <cursor>` in order, then `run_terminal`, then close; AC#14.
- `test_last_event_id_resume` — reconnect with `Last-Event-ID` backfills only entries after the cursor (steps and transitions), no dup/gap; AC#15.
- `test_terminal_run_replays_then_closes` — connecting to a finished run replays all entries from DB and closes; matches `GET .../trace`; AC#16.
- `test_keepalive_emitted` — a `: keepalive` comment is sent within the heartbeat window on an idle live stream.

**Worker integration (`apps/worker/tests/test_trace_sink_wiring.py`):**
- `test_fake_producer_end_to_end` — a fake multi-node "agent" records `decision`/`tool_call`/`tool_result`/`verification` steps through `PublishingStepSink` (inner persists to `agent_steps`); the paginated trace, SSE live capture, and replay all agree on the same ordered sequence; AC#1/#14/#16.

**Frontend (`apps/web/__tests__/run-trace.test.tsx`, React Testing Library):**
- `test_renders_merged_timeline` — step rows + transition rows render in order with node/type icons and token/cost badges.
- `test_live_badge_flips_on_terminal` — mock EventSource emits `run_terminal` → badge shows "Completed"; AC#18.
- `test_redacted_values_shown` — `[REDACTED]` displayed, never the raw secret; AC#7/#18.
- `test_keyboard_nav_and_detail_panel` — `j/k` move selection, `o` opens, `Esc` closes; AC#18.
- `test_reusable_in_review_page` — `RunTraceTimeline` mounts with `{ workflowRunId, live:false }` (the F08 embedding contract) and renders from a mocked `GET .../trace`; AC#18.

---

## 8. Security & policy considerations

- **Secret redaction is defense-in-depth.** The spec requires secrets stripped from "logs, traces, and retrieval results." F06 redacts at **write time** (so Postgres and the spilled MinIO objects never hold the secret); F10 re-asserts the **same shared `SecretRedactor`** at **read time** so timelines, detail panels, and signed-URL downloads stay clean even if a future producer regresses. F10 reuses the single shared filter (from `cross-cutting/F37-auth-secrets-byok`), never a local copy.
- **Read-only, RBAC-gated.** Traces expose agent reasoning, file contents, and tool I/O; all endpoints require auth + workspace membership and `viewer` is the minimum role. F10 exposes **no** write/mutating endpoint — steps are written only by F06's trusted in-process `StepSink`, never via HTTP.
- **Tenant isolation.** Every query joins to `agent_runs.workspace_id` / `workflow_run.workspace_id` (F10 holds no denormalized workspace column); cross-workspace ids return **404** to avoid existence leakage. Signed URLs are workspace-scoped and short-TTL.
- **Immutability / audit integrity.** F10 never writes `agent_steps` or `workflow_transition`. Both are append-only by their owning slices, and both adopt the `attach_immutability_trigger(table)` DB guard exported by `cross-cutting/F39-audit-log`. F06's unique `(agent_run_id, seq)` and F07's unique `(workflow_run_id, sequence)` prevent reordering/forgery; F10's total-order cursor preserves that ordering on read.
- **MCP audit linkage.** MCP usage appears in the trace as `query_mcp` `tool_call` steps F06 records (redacted input, status, `latency_ms`). The authoritative MCP audit required by the spec ("tool name, payload hash, result status, latency") is F09's own immutable audit log; F10 surfaces the agent-side step and links to the run, it does not re-implement MCP audit fields.
- **Availability / DoS.** SSE connections are capped per worker (F37 Redis-backed limiter) and Redis-backed so they fan out across API replicas; the `PublishingStepSink`/`TracePublisher` swallow-on-error path ensures trace-streaming failures degrade observability, never execution. Keyset pagination + the F06/F10 indexes bound query cost on very long runs.

---

## 9. Effort estimate & risk (S/M/L + key risks)

**Effort: S/M.** F10 is a pure read/stream/UI surface over data F06 and F07 already persist; there is no novel write path (only the one-line `PublishingStepSink` decorator and the `publish_transition` seam). Rough split: assembler + cost + pagination (S), read repository + redaction re-assert + signed-URL minting (S), API + SSE + resume/replay (M), web timeline + detail + live hook (M).

**Key risks:**
1. **Ordering across two tables.** Merging `agent_steps` with `workflow_transition` is ambiguous when `created_at` ties. *Mitigation:* total order via `(created_at, kind_rank, seq)`; the assembler is pure and property-tested for byte-stable replay (AC#17).
2. **SSE correctness (gaps/dupes on reconnect).** *Mitigation:* use the same keyset cursor for SSE `id` and pagination; `Last-Event-ID` drives a precise `> cursor` backfill across both tables before tailing Redis; tested explicitly (AC#15).
3. **Cost per-phase attribution.** F06's `agent_steps` has no `phase` column, so phase cost is derived from F07 transition intervals. *Mitigation:* deterministic interval attribution (AC#4), unit-tested with pre-first-transition edge case.
4. **Schema coupling to F06/F07.** F10's read model is bound to F06's `agent_steps` columns and F07's `workflow_transition` columns. *Mitigation:* depend only on the frozen `forge_contracts` `Step`/`TokenUsage`/`WorkflowTransitionDTO`; integration test runs the real migrations so a column drift fails CI.
5. **Redaction false-negatives.** A missed pattern would surface a secret on read. *Mitigation:* reuse the single shared `SecretRedactor`, re-assert at read, and unit-test deep/nested structures (AC#7); F06's write-time redaction is the primary defense.
6. **Trace volume / UI perf.** Long runs produce thousands of steps. *Mitigation:* keyset pagination + the `(agent_run_id, step_type/status)` indexes on the read path; virtualized list in the timeline component.

---

## 10. Key files / paths (exact)

**packages/workflow-engine/forge_workflow/trace/**
- `models.py` — Pydantic read models/enums (§4)
- `assembler.py` — `TraceAssembler`, cost rollups (totals/by_phase/by_model), cursor pagination
- `repository.py` — `TraceReadRepository` (read-only; workspace-scoped)
- `redaction.py` — `reassert_redaction()` over the shared `SecretRedactor` (read-time only)
- `stream.py` — `TracePublisher`, `TraceSubscriber`, `PublishingStepSink`
- `tests/trace/test_{assembler,redaction,publishing_sink}.py`

**packages/db/migrations/versions/**
- `<rev>_trace_read_indexes.py` — additive indexes on `agent_steps` only (`(agent_run_id, step_type)`, `(agent_run_id, status)`); `down_revision` = F06 `0006_agent_runtime`; reversible. No tables/columns.

**apps/api/app/**
- `api/v1/trace.py` — router (trace, steps, payload, cost, SSE stream)
- `services/trace_service.py` — `TraceService`
- `schemas/trace.py` — API schemas
- `tests/test_trace_api.py`, `tests/test_trace_stream.py`

**apps/worker/forge_worker/**
- `runtime/trace_sink_wiring.py` — composes `PublishingStepSink(inner=<F06 sink>, publisher=TracePublisher(redis))` into `RuntimeDeps.step_sink`
- `tests/test_trace_sink_wiring.py`

**apps/web/**
- `app/(dashboard)/projects/[projectId]/tasks/[taskId]/runs/[runId]/page.tsx`
- `components/trace/{RunTraceTimeline,StepRow,StepDetailPanel,CostSummary,TraceFilters,LiveBadge}.tsx`
- `lib/api/trace.ts`
- `__tests__/run-trace.test.tsx`

**deploy/**
- `caddy/Caddyfile` + `nginx/forge.conf` — `flush_interval -1` + long read timeout for `/api/v1/workflow-runs/*/trace/stream`

---

## 11. Research references (relevant links from the spec/research report)

- Core Data Model — `AgentRun.steps[] (tool calls, decisions, outputs)`: `docs/FORGE_SPEC.md` §Core Data Model.
- Observability Layer responsibility — "Run traces, token/cost logs, task lineage, retrieval debug": `docs/FORGE_SPEC.md` §Product Scope.
- Observability and Evaluation — "Replayable workflow runs with step-level inspection" and Cost metrics ("token cost per task, per workflow phase, per model provider"): `docs/FORGE_SPEC.md` §Observability and Evaluation.
- Human Approval System — "Approval UI Must Show" item 8 ("Full run trace with step-by-step actions taken"): `docs/FORGE_SPEC.md` §Human Approval System. (F08 embeds F10's `RunTraceTimeline` here.)
- Real-time channel — "WebSockets + SSE — Live task updates, run traces, approvals": `docs/FORGE_SPEC.md` §Technology Stack.
- Security — Secret redaction ("Secrets stripped from logs, traces, and retrieval results") and Audit log ("Every agent action, tool call, MCP call, and approval — immutable, queryable"): `docs/FORGE_SPEC.md` §Security.
- MCP audit requirement — "Full audit log: tool name, payload hash, result status, latency" (owned by F09, surfaced as `query_mcp` steps in the trace): `docs/FORGE_SPEC.md` §MCP Security Rules.
- LangGraph streaming + checkpointed step state (the agent loop F06 records, F10 reads): https://langchain-ai.github.io/langgraph/ ; production guidance https://www.reactify-solutions.com/articles/langgraph-production-agents-2026 (research report §Workflow Engine).
- LangSmith as the agent-tracing reference Forge mirrors self-hosted: https://smith.langchain.com/ (spec §Technology Stack / research report observability row).
- Sibling slice cross-refs: `docs/implementation-slices/v1/F06-single-execution-agent.md` (`agent_steps` table, `Step`/`StepSink`/`TokenUsage` contracts, write-time redaction + overflow spill), `docs/implementation-slices/v1/F07-feature-workflow-fsm.md` (`workflow_transition`, `WorkflowTransitionDTO`, `TERMINAL_STATES`, transition→viewer seam), `docs/implementation-slices/v1/F08-plan-execute-verify-pr-approval.md` (review-page `RunTraceTimeline` embedding, `flush_interval -1` SSE convention from F01).

---

## 12. Out of scope / future

- **Producing the step content and the `agent_steps` table** — the LangGraph agent loop, the `StepSink` write contract, write-time redaction, 64 KB truncation, and MinIO overflow spill are all `v1/F06-single-execution-agent`. F10 reads and renders.
- **The `workflow_transition`/FSM audit log itself** — owned by `v1/F07-feature-workflow-fsm`; F10 reads it.
- **The central tamper-evident audit log** — `cross-cutting/F39-audit-log` owns `audit_log` and the immutability-trigger helper; F10 reads the trace sources, not that log.
- **MCP per-call audit fields** (payload hash, etc.) — owned by `v1/F09-mcp-gateway-v1`; F10 surfaces `query_mcp` steps.
- **Golden-set replay bundles and the eval dashboards** — `v1/F12-eval-harness` builds its own deterministic `replay_bundle`s and a `/replay/{bundle_id}` inspector for offline scoring; it shares the spec's "replayable runs with step-level inspection" goal but does not consume F10's REST API.
- **Cross-run / project-level analytics dashboards** (aggregate cost trends, retry-rate charts, p50/p95 latency across runs) — V2 observability dashboards (Grafana/Prometheus stack); F10 exposes per-run cost only.
- **WebSocket transport / bidirectional control** — V1 uses one-way SSE for trace streaming (the spec lists both; SSE is sufficient for read-only trace push). A WebSocket multiplexer is future work.
- **Sub-agent (multi-agent) trace nesting** — `SubAgentRun` traces are Phase 3 (supervised multi-agent); F10's `agent_run_ids[]` shape on `RunTrace` is forward-compatible, but multi-agent trace UI is out of scope for V1.
- **Tamper-evident hash chaining of steps** (Merkle/append-only proofs beyond the unique-seq + F39 immutability trigger) — a security hardening for V2.
- **Trace export** (download a run as JSON/HAR-like bundle) — fast-follow once the read model stabilizes.
</content>
</invoke>
