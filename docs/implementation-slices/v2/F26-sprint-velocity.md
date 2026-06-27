# F26 — Sprint Management & Velocity Dashboards

> Phase: v2 · Spec module(s): Native Project Board (`Sprint` entity, "Sprint management" board feature, UX standards), Core Data Model (`Project.Sprint[]`), Observability Layer (workflow-quality metrics: task completion rate / mean time to completion) · Status target: **Done** = a project can run a full sprint lifecycle (`planned → active → completed`, plus `cancelled`) where starting a sprint snapshots its **committed scope**, mid-sprint scope changes (add/remove task, estimate change, complete/reopen) are captured as an append-only scope-event log, a daily worker snapshots burndown, and three read surfaces render sub-100ms and keyboard-first: per-sprint **burndown** (ideal vs actual), per-sprint **report** (committed/completed/added/removed/carryover + task breakdown), and a project **velocity dashboard** (committed-vs-completed bars across the last N sprints with average, rolling-3, and predictability). All velocity/burndown math is pure and unit-tested; lifecycle, scope-capture, snapshots, and routes are integration-tested against real Postgres; the `sprint_velocity` rollup and `sprint_burndown_snapshots` are **derived state** rebuildable from the scope-event log via `sprint.reconcile_sprint`. Green under `ruff` + `mypy` + `pytest`.

---

## 1. Intent — what & why

F01 (Native Project Board) created the `sprints` table and basic task→sprint assignment but **explicitly deferred** velocity/burndown to v2: *"Sprint velocity dashboards & burndown — v2 (Sprint management and velocity dashboards). v1 ships `sprints`/`milestones` tables + basic assignment only."* The Phased Roadmap lists *"Sprint management and velocity dashboards"* as a Phase 2 item, and the Board Features table lists *"Sprint management — Create sprints, move tasks, track velocity."* **F26 is that slice.**

The product value: a team that runs the board as its task-as-control-plane needs to plan time-boxed work, see whether they are on track *during* the sprint (burndown), and forecast future capacity from past throughput (velocity). The subtle, correctness-critical parts are not the charts — they are:

1. **Committed scope must be snapshotted at sprint start**, not read live. Otherwise mid-sprint additions silently inflate "committed" and make every sprint look like it hit its plan.
2. **Scope changes must be an append-only event log.** Velocity and burndown are reconstructions over that log; without it, an estimate edited after a task is done corrupts history. This mirrors F01's `activity_events` discipline (append-only, derived views) and F23's "projection is derived, rebuildable from source" stance.
3. **"Done" is defined by `StatusCategory.COMPLETED`** (from F01's per-project status categories), not by a hardcoded status name — so custom per-project workflows still compute velocity correctly.

F26 is additive on F01: it extends the `sprints` table, adds three tables (one append-only log + one time-series + one rollup), a pure compute module, lifecycle services, two Celery beat tasks, a read API, and a sprint/velocity UI. It introduces **no new workflow gates** and changes no execution/agent behavior.

## 2. User-facing behavior / journeys

**Journey A — Plan & start a sprint.** On the project's **Sprints** view a member creates "Sprint 7" (name, goal, `start_date`, `end_date`, optional `capacity_points`). It is `planned`. They drag/bulk-assign backlog tasks into it (reuses F01 `set_sprint_id`), watching a running "planned points" total against capacity. They press **Start**. The sprint becomes `active`, its **committed scope is frozen** (sum of estimates of in-sprint, non-done, non-cancelled tasks), `started_at` is set, and a day-0 burndown point appears.

**Journey B — Work the sprint (live burndown).** Over the sprint, members move tasks to a `completed`-category status; the burndown's actual line steps down. Adding a task mid-sprint raises the scope line above ideal and is flagged as **added scope**; removing one lowers it. Each change updates the sprint header chips ("18/34 pts · 4 added · 2 removed") within ~1s via SSE.

**Journey C — Complete the sprint (carryover).** At sprint end a member presses **Complete**. A dialog shows committed 34 / completed 25 (74% predictability), 3 incomplete tasks (9 pts), and asks where carryover goes: **move to next sprint**, **move to backlog** (default), or **leave in sprint**. On confirm the sprint becomes `completed`, velocity is finalized, carryover tasks are moved, and the sprint enters velocity history.

**Journey D — Velocity dashboard / forecast.** On the **Velocity** view a lead sees a bar chart of the last N sprints (committed vs completed points), summary cards (*Average velocity 26 pts*, *Rolling-3 24 pts*, *Predictability 81%*, *Avg scope change 18%*), and a planning forecast band (low/avg/high) used to size the next sprint. Keyboard: `j/k` move sprint bars, `Enter` opens the sprint report, `/` search, `e` export.

**Journey E — Cancel a sprint.** A `planned` or `active` sprint can be **cancelled**: it leaves velocity history, its tasks return to the backlog, and no predictability is recorded (cancelled sprints never count as misses).

**Journey F — Drift repair.** If a member suspects the rollup is wrong (e.g. after a dev DB edit), **Recompute** (member/admin) enqueues `sprint.reconcile_sprint`; the burndown + velocity rebuild from the scope-event log and the UI updates when `velocity_version` advances.

## 3. Vertical slice

### 3.1 Data model (tables/columns/migrations touched)

New Alembic migration `apps/api/alembic/versions/<rev>_sprint_velocity.py` (the `<rev>` numeric/hash id is assigned at creation to chain onto the current Alembic head; its `down_revision` ultimately follows F01's `0002_board_core`; additive + one enum value + one partial unique index; reversible). ORM models extend `packages/board-core` and follow F01's naming convention (`ix_`/`uq_`/`fk_`/`ck_`/`pk_`), `uuid` PKs (`gen_random_uuid()`), `TimestampMixin` (from `packages/db` / `forge_db`), and a denormalized `workspace_id` on every row.

**Extend F01 `sprints`** (additive columns; existing rows valid):
| column | type | notes |
|---|---|---|
| `started_at` | timestamptz null | set on `start` |
| `completed_at` | timestamptz null | set on `complete`/`cancel` |
| `capacity_points` | int null | planning capacity (display/forecast only) |
| `committed_points` | int not null default 0 | **snapshot** at start; immutable after |
| `committed_task_count` | int not null default 0 | snapshot at start |
| `position` | text null | lexorank for ordering the sprint list (F01 `ranking.rank_between`) |
| `velocity_version` | bigint not null default 0 | monotonic; bumped each rollup refresh (UI "Recompute" completion signal) |

Alter the F01 `sprint_state` enum to add value `cancelled` (existing `planned`/`active`/`completed`). Add partial unique index `uq_active_sprint_per_project ON sprints (project_id) WHERE state = 'active'` — **at most one active sprint per project** (team-scoped sprints are §12 future).

**New `sprint_scope_events`** (append-only log; grain = one row per scope change; the source of truth for all reconstruction):
| column | type | notes |
|---|---|---|
| `id` | uuid PK | |
| `workspace_id` | uuid not null | RBAC scoping |
| `project_id` | uuid FK→projects CASCADE not null | |
| `sprint_id` | uuid FK→sprints CASCADE not null | |
| `task_id` | uuid FK→tasks SET NULL null | null for sprint-level markers |
| `event_type` | enum `sprint_scope_event_type` | see §4 |
| `points_delta` | int not null default 0 | signed: + adds scope/uncompletes, − removes/completes |
| `points_before` | int null | task estimate before (for `estimate_changed`) |
| `points_after` | int null | task estimate after |
| `scope_points_after` | int not null | running total committed scope after this event |
| `remaining_points_after` | int not null | running remaining (scope − completed) after this event |
| `actor_kind` | enum(user/agent/system) | |
| `actor_id` | uuid null | |
| `occurred_at` | timestamptz not null | event time (may be backdated by `reconcile`) |
| `created_at` | timestamptz not null | |

Append-only: enforced at the service layer (no update/delete) **and** hardened DB-side via `attach_immutability_trigger(sprint_scope_events)` from `cross-cutting/F39-audit-log` — the same helper F01's `activity_events` adopts. Index `ix_sprint_scope_events_sprint_occurred (sprint_id, occurred_at)`.

**New `sprint_burndown_snapshots`** (time-series; grain = one row per sprint per calendar day; authoritative history robust to retroactive estimate edits):
| column | type | notes |
|---|---|---|
| `id` | uuid PK | |
| `workspace_id`/`project_id` | uuid (+FK CASCADE on project) | |
| `sprint_id` | uuid FK→sprints CASCADE not null | |
| `snapshot_date` | date not null | |
| `scope_points` | int not null | committed + added − removed as of end-of-day |
| `remaining_points` | int not null | scope − completed |
| `completed_points` | int not null | |
| `ideal_points` | numeric(10,2) not null | linear committed→0 over the sprint window |
| `completed_task_count` | int not null | |
| `remaining_task_count` | int not null | |
| `created_at` | timestamptz not null | |

`UNIQUE(sprint_id, snapshot_date)` (idempotent daily upsert). Index `ix_sprint_burndown_sprint_date (sprint_id, snapshot_date)`.

**New `sprint_velocity`** (rollup; one row per sprint; powers the dashboard in one scan; **derived/rebuildable**):
| column | type | notes |
|---|---|---|
| `sprint_id` | uuid PK FK→sprints CASCADE | |
| `workspace_id`/`project_id` | uuid (+FK) | index `ix_sprint_velocity_project (project_id)` |
| `committed_points`/`completed_points`/`added_points`/`removed_points`/`carryover_points` | int not null | |
| `committed_task_count`/`completed_task_count`/`carryover_task_count` | int not null | |
| `predictability` | numeric(5,4) not null | `completed/committed` (0 if committed=0) |
| `scope_change_ratio` | numeric(5,4) not null | `(added+removed)/committed` (0 if committed=0) |
| `state` | text not null | mirror of `sprints.state` at compute time (dashboard filters cancelled/planned without a join) |
| `computed_at` | timestamptz not null | |

> `sprint_velocity` + `sprint_burndown_snapshots` are **derived state**: droppable and fully rebuildable from `sprint_scope_events` + current task rows by `sprint.reconcile_sprint`. Nothing reads them as source of truth; no gate/agent decision consults them.

### 3.2 Backend (FastAPI routes + services/packages)

**Domain package: `packages/board-core`** (extends F01; `board_core` module; depends on `packages/db` (`forge_db`) + `pydantic`, must NOT import `fastapi`). New modules:
- `board_core/velocity.py` — pure compute (no I/O): `compute_velocity`, `compute_burndown`, `compute_velocity_summary`, `ideal_line` (§4).
- `board_core/sprint_state.py` — `SprintStateMachine` (allowed transitions + guards).
- `board_core/models/sprint_scope_event.py`, `models/sprint_burndown_snapshot.py`, `models/sprint_velocity.py` (ORM); extend `models/sprint.py` with the new columns.
- `board_core/services/sprints.py` — extends F01's sprint CRUD with `start`, `complete`, `cancel`, `reconcile`, and `record_scope_event`. Services take an injected `AsyncSession` + `Principal`, never request objects, and use F01's `scoped_query`.
- `board_core/services/velocity.py` — read assembly for burndown/report/velocity DTOs (reads projection rows; never computes on read in the hot path).
- `board_core/errors.py` — extend with `ActiveSprintExistsError`, `SprintStateError`.

**Scope-capture hook (the integration seam with F01):** F01's `tasks.update`, `tasks.move`, and `tasks.bulk` services gain a post-commit hook `sprints.record_scope_event(...)` that fires **only when** the task is in an `active` sprint and one of these changed: `sprint_id` (add/remove), `estimate` (estimate_changed), or `status_id` crossing into/out of the `COMPLETED` category (task_completed/task_reopened). The hook writes one `sprint_scope_events` row (computing the running `scope_points_after`/`remaining_points_after`), enqueues `sprint.recompute_velocity(sprint_id)`, and publishes an SSE envelope. This is the only new coupling into F01 and is covered by AC #3–#5.

**App `apps/api`** (module `forge_api`) — router `apps/api/forge_api/routers/board/sprints.py` (extend F01's) + `apps/api/forge_api/routers/board/velocity.py`. Mounted under `/api/v1`, require an authenticated `Principal` (resolved by `cross-cutting/F37-auth-secrets-byok`'s `forge_api/deps/auth.py::get_principal`), `workspace_id` resolved from the entity path, every query workspace-filtered via F01's `scoped_query`.
- `POST /projects/{project_id}/sprints` — create (`planned`). *(F01 had basic create; F26 accepts `capacity_points`.)*
- `PATCH /sprints/{sprint_id}` — edit name/goal/dates/capacity (only while `planned`/`active`; `committed_points` is read-only).
- `POST /sprints/{sprint_id}/start` → `SprintRead` (snapshots committed scope; 409 if an active sprint exists).
- `POST /sprints/{sprint_id}/complete` body `CompleteSprintRequest{carryover: backlog|next_sprint|leave, next_sprint_id?}` → `SprintReport`.
- `POST /sprints/{sprint_id}/cancel` → `SprintRead`.
- `POST /sprints/{sprint_id}/recompute` → `{enqueued: true, velocity_version}` (member/admin).
- `GET /projects/{project_id}/sprints?state=&cursor=&limit=` → `Page[SprintRead]`.
- `GET /sprints/{sprint_id}` → `SprintRead` (includes live metric chips).
- `GET /sprints/{sprint_id}/burndown?as_of=` → `BurndownSeries`.
- `GET /sprints/{sprint_id}/report` → `SprintReport`.
- `GET /projects/{project_id}/velocity?last=N` (N default 6, max 26) → `VelocityDashboard`.
- `GET /projects/{project_id}/velocity/export?format=csv|json` → streaming export (one row per sprint).

Live sync: every sprint mutation records (where meaningful) and publishes a JSON envelope to F01's Redis channel `board:{project_id}`; F01's existing `GET /projects/{project_id}/stream` SSE handler fans it out (no new stream endpoint).

### 3.3 Worker / agent runtime (Celery tasks, LangGraph, if any)

No LangGraph / agent runtime. Three Celery tasks in `apps/worker/forge_worker/tasks/sprint_tasks.py` (alongside F01's `apps/worker/forge_worker/tasks/board_tasks.py`; Celery app `forge_worker.app`, Redis broker + Beat from the foundation substrate):
- `sprint.snapshot_burndown` (Beat, daily at 23:55 workspace-naive UTC + once on `start`): for every `active` sprint, upsert one `sprint_burndown_snapshots` row for `snapshot_date = today` (idempotent via `UNIQUE(sprint_id, snapshot_date)`); publish `burndown.updated`. AC #8.
- `sprint.recompute_velocity(sprint_id)` (enqueued by the scope-capture hook and by lifecycle ops): reload tasks + scope log for the sprint, run `compute_velocity`, upsert `sprint_velocity` (bump `sprints.velocity_version`), publish `sprint.updated`. Idempotent (wholesale recompute → duplicate deliveries converge). AC #14.
- `sprint.reconcile_sprint(sprint_id)` (enqueued by `POST /recompute`; also a `forge` CLI command): rebuild `sprint_velocity` **and** replay `sprint_burndown_snapshots` day-by-day from `sprint_scope_events`; result is byte-identical to the live path for the same source data. AC #18.

A Beat schedule entry (added to `apps/worker/forge_worker/beat.py` alongside F01's `board.scan_sla_breaches`) runs `sprint.snapshot_burndown` daily; `sprint.recompute_velocity` runs on demand (no periodic sweep needed because the hook covers every mutation and reconcile is the safety net).

### 3.4 Frontend / UI (Next.js routes/components, if any)

App `apps/web` (App Router, TypeScript, Tailwind, shadcn/ui, TanStack Query). Charts use **Recharts** (the charting lib behind shadcn/ui charts) — added as the one new web dependency. Shared chart primitives go in `packages/ui-kit/src/sprint/`.

Routes:
- `app/[workspace]/projects/[projectKey]/sprints/page.tsx` — sprint list + active-sprint planning panel.
- `app/[workspace]/projects/[projectKey]/sprints/[sprintId]/page.tsx` — sprint detail: burndown + report + task breakdown.
- `app/[workspace]/projects/[projectKey]/velocity/page.tsx` — velocity dashboard.

Components (`apps/web/components/board/sprint/`): `SprintList.tsx`, `SprintPlanningPanel.tsx` (planned-points vs capacity bar), `StartSprintButton.tsx`, `CompleteSprintDialog.tsx` (carryover picker), `BurndownChart.tsx` (Recharts ideal vs actual + scope line), `SprintReport.tsx`, `ScopeChangeChips.tsx`, `VelocityBarChart.tsx` (committed vs completed), `VelocitySummaryCards.tsx`, `VelocityForecastBand.tsx`, `SprintEmptyState.tsx`. Data hooks in `apps/web/lib/board/sprintApi.ts` + `sprintQueries.ts` (TanStack Query); live patches via F01's `useBoardStream` (filter `sprint.updated`/`burndown.updated`). Keyboard via F01's keymap provider (`j/k/Enter//e`). UX standards inherited from F01: optimistic where applicable, no full-page reload, sub-100ms (projection-backed reads), per-view empty states.

### 3.5 Infra / deploy (compose, helm, caddy, if any)

No new compose services. Reuses `db`, `redis`, `api`, `worker`, `web`, `caddy` from the foundation substrate + `v1/F14-docker-compose-selfhost`, and F01's `board:{project_id}` SSE channel + Caddy SSE matcher (already `flush_interval -1`). The `worker` already runs Celery Beat (F01's `board.scan_sla_breaches`); register the `sprint.snapshot_burndown` daily schedule entry in `apps/worker/forge_worker/beat.py` (code, not compose). Migration runs via existing `forge-cli db migrate`. One new web dependency (`recharts`) added to `apps/web/package.json`.

## 4. Public interfaces / contracts (exact signatures, Pydantic models, Protocols, YAML schemas)

**Enums & state machine** (`board_core/sprint_state.py`, `board_core/enums.py`):
```python
from enum import StrEnum

class SprintState(StrEnum):
    PLANNED = "planned"; ACTIVE = "active"
    COMPLETED = "completed"; CANCELLED = "cancelled"

class SprintScopeEventType(StrEnum):
    SPRINT_STARTED = "sprint_started"     # baseline marker; points_delta=committed
    TASK_ADDED = "task_added"             # +estimate
    TASK_REMOVED = "task_removed"         # −estimate
    TASK_COMPLETED = "task_completed"     # −estimate from remaining
    TASK_REOPENED = "task_reopened"       # +estimate back to remaining
    ESTIMATE_CHANGED = "estimate_changed" # delta = after − before
    SPRINT_COMPLETED = "sprint_completed" # finalize marker
    SPRINT_CANCELLED = "sprint_cancelled"

class CarryoverTarget(StrEnum):
    BACKLOG = "backlog"; NEXT_SPRINT = "next_sprint"; LEAVE = "leave"

class SprintStateMachine:
    ALLOWED: dict[SprintState, set[SprintState]] = {
        SprintState.PLANNED:  {SprintState.ACTIVE, SprintState.CANCELLED},
        SprintState.ACTIVE:   {SprintState.COMPLETED, SprintState.CANCELLED},
        SprintState.COMPLETED: set(), SprintState.CANCELLED: set(),
    }
    def assert_transition(self, frm: SprintState, to: SprintState) -> None: ...  # raises SprintStateError
```

**Pure compute** (`board_core/velocity.py`) — no I/O, deterministic:
```python
from datetime import date, datetime
from pydantic import BaseModel

class SprintWindow(BaseModel):
    start_date: date
    end_date: date
    started_at: datetime | None
    completed_at: datetime | None

class SprintTaskSnapshot(BaseModel):
    task_id: str
    points: int                    # tasks.estimate or 0 if None
    is_completed: bool             # current status category == COMPLETED
    is_cancelled: bool             # current status category == CANCELED
    in_committed_scope: bool       # was in sprint, non-done, non-cancelled at start
    added_at: datetime | None      # set if added to sprint after started_at
    removed_at: datetime | None    # set if removed from sprint after started_at
    completed_at: datetime | None  # when it crossed into COMPLETED category

class VelocityResult(BaseModel):
    committed_points: int; completed_points: int
    added_points: int; removed_points: int; carryover_points: int
    committed_task_count: int; completed_task_count: int; carryover_task_count: int
    predictability: float          # completed/committed, 0.0 if committed==0
    scope_change_ratio: float      # (added+removed)/committed, 0.0 if committed==0

def compute_velocity(window: SprintWindow, tasks: list[SprintTaskSnapshot]) -> VelocityResult: ...
# committed = sum(points where in_committed_scope); completed = sum(points where is_completed and in sprint at completion);
# added = sum(points added after start and still in sprint); removed = sum(points removed after start);
# carryover = committed_or_added still-in-sprint and not completed at completion.

class BurndownPoint(BaseModel):
    snapshot_date: date
    scope_points: int; remaining_points: int; completed_points: int
    ideal_points: float
    completed_task_count: int; remaining_task_count: int

class ScopeEvent(BaseModel):     # mirror of a sprint_scope_events row, ordered by occurred_at
    occurred_at: datetime; event_type: SprintScopeEventType
    points_delta: int; scope_points_after: int; remaining_points_after: int

def ideal_line(committed_points: int, start: date, end: date) -> dict[date, float]: ...
# linear committed→0 across inclusive calendar days; end-day == 0.0.

def compute_burndown(window: SprintWindow, committed_points: int,
                     events: list[ScopeEvent], *, as_of: date | None = None) -> list[BurndownPoint]: ...
# one point per calendar day start_date..min(end_date, as_of or end_date); end-of-day state from the last event on/before that day.

class VelocitySummary(BaseModel):
    sprint_count: int
    average_velocity: float        # mean(completed_points) over completed sprints
    rolling_3_velocity: float      # mean of last 3 completed
    predictability_avg: float
    scope_change_avg: float
    forecast_low: float; forecast_avg: float; forecast_high: float  # min/avg/max of last N completed

def compute_velocity_summary(history: list[VelocityResult]) -> VelocitySummary: ...
# history is completed sprints only, newest last; empty history -> all zeros (no divide-by-zero).
```

**API DTOs** (`apps/api/forge_api/schemas/sprint.py`, Pydantic v2):
```python
class SprintCreate(BaseModel):
    name: str = Field(min_length=1, max_length=256)
    goal: str | None = None
    start_date: date
    end_date: date
    capacity_points: int | None = Field(default=None, ge=0)
    # ck end_date >= start_date enforced in service + DB check constraint

class SprintRead(BaseModel):
    id: UUID; project_id: UUID; name: str; goal: str | None
    state: SprintState; start_date: date; end_date: date
    started_at: datetime | None; completed_at: datetime | None
    capacity_points: int | None
    committed_points: int; committed_task_count: int
    # live chips (computed from sprint_velocity rollup):
    completed_points: int; added_points: int; removed_points: int
    remaining_points: int; predictability: float
    velocity_version: int
    model_config = ConfigDict(from_attributes=True)

class CompleteSprintRequest(BaseModel):
    carryover: CarryoverTarget = CarryoverTarget.BACKLOG
    next_sprint_id: UUID | None = None      # required iff carryover == NEXT_SPRINT

class BurndownSeries(BaseModel):
    sprint_id: UUID; start_date: date; end_date: date
    committed_points: int
    points: list[BurndownPoint]

class SprintReportTask(BaseModel):
    task_id: UUID; key: str; title: str; points: int
    bucket: Literal["completed", "carryover", "added", "removed"]

class SprintReport(BaseModel):
    sprint: SprintRead
    velocity: VelocityResult
    completed: list[SprintReportTask]
    carryover: list[SprintReportTask]
    added: list[SprintReportTask]
    removed: list[SprintReportTask]

class VelocitySprintBar(BaseModel):
    sprint_id: UUID; name: str; end_date: date
    committed_points: int; completed_points: int; predictability: float

class VelocityDashboard(BaseModel):
    project_id: UUID
    sprints: list[VelocitySprintBar]    # completed sprints, oldest→newest
    summary: VelocitySummary
```

**Error contract** (reuses F01's envelope): `409 {"error":"active_sprint_exists","sprint_id":...}` on `start` when one is active; `409 {"error":"sprint_state","from":...,"to":...}` on illegal lifecycle transition; `422` on `CompleteSprintRequest.carryover==next_sprint` without `next_sprint_id`; `403 forbidden` for RBAC; `404` for cross-workspace ids.

**SSE envelopes** (on F01's `board:{project_id}` channel):
```
event: sprint.updated
data: {"entity_type":"sprint","entity_id":"...","state":"active","velocity_version":12,"ts":"..."}

event: burndown.updated
data: {"entity_type":"sprint","entity_id":"...","snapshot_date":"2026-06-26","remaining_points":18,"ts":"..."}
```

**CLI** (`apps/api/forge_api/cli/sprint.py`, registered as the `sprint` sub-command group on the `forge-cli` console script — matching the sibling convention in `v1/F04-repo-policy` / `v1/F11-skill-profiles`): `forge-cli sprint reconcile <sprint-id>` (rebuild rollup + snapshots), `forge-cli sprint velocity <project-id> [--json]` (print the dashboard).

## 5. Dependencies — features/slices that must exist first

- `v1/F01-project-board` (REQUIRED, hard) — owns `sprints`, `tasks` (`estimate`, `sprint_id`, `status_id`), `task_statuses.category` + `StatusCategory.COMPLETED/CANCELED` (the "done" signal), `activity_events`, `board_core` services + `scoped_query`, `ranking.rank_between`, the `board:{project_id}` SSE channel + `useBoardStream`, and the bulk `set_sprint_id` path F26 hooks into. F26 is meaningless without it; F01 explicitly defers this feature to here.
- **Foundation substrate** (`v1/F00-foundation-substrate`; siblings also name it `cross-cutting/C01-monorepo-and-api-foundations` / `cross-cutting/F00-platform-foundation` — reconcile to the final foundation slug when it lands) (REQUIRED, hard) — monorepo + `uv` workspaces, `apps/api` FastAPI skeleton (`forge_api`), `apps/worker` Celery app (`forge_worker.app`) + Beat, `apps/web` Next.js shell, `packages/db` (`forge_db`: shared `Base`, `TimestampMixin`, async session, Alembic env + naming convention), Postgres+Redis, `forge-cli db migrate`. Container/compose packaging owned by `v1/F14-docker-compose-selfhost`.
- `cross-cutting/F37-auth-secrets-byok` (REQUIRED, hard) — the `Principal` (`workspace_id`, `user_id`, `role ∈ {admin, member, viewer, agent-runner}`) resolved by `forge_api/deps/auth.py::get_principal`, the `require_role(...)` RBAC dependency, and the Redis rate limiter used by `POST /recompute` (§8). (Siblings also call this `C02-auth-and-rbac`.)
- `cross-cutting/F39-audit-log` (REQUIRED, lands with) — the `attach_immutability_trigger(table)` helper `sprint_scope_events` adopts for DB-level append-only enforcement, plus the canonical `AuditEvent` DTO + `AuditSink` Protocol (in `packages/contracts`): every lifecycle action (`start`/`complete`/`cancel`/`recompute`) and every agent-runner-triggered scope event fans out a redacted `AuditEvent` with a `detail_ref` back to the originating `sprints`/`sprint_scope_events` row, satisfying the audit-log non-negotiable (§8). Mirrors F01's `activity_events` treatment.
- `cross-cutting/F38-observability-cost-metrics` (SOFT) — F26 emits the spec's throughput Key Metrics (`task completion rate`, `mean time to task completion`) and velocity/predictability gauges through F38's `ForgeMetrics` facade when present; F38 ships a no-op fallback so F26 calls it unconditionally and degrades to zero overhead if absent.
- `v1/F16-slack-notifications` (SOFT) — F26 emits sprint-start/complete/cancel events; if present, F16 can notify channels. F26 only emits; it does not depend on delivery.
- `v2/F23-spec-validation-dashboard` (REFERENCE only, not a dependency) — same "derived projection rebuildable from source + SSE patch" pattern; reuse its conventions, no code coupling.

No other v2 feature is a prerequisite; F26 is additive on the v1 board stack.

## 6. Acceptance criteria (numbered, testable)

1. **Committed-scope snapshot.** Starting a sprint holding tasks with estimates `[3,5,2,0,8]` where the `8`-pt task is already in a `COMPLETED`-category status sets `committed_points=10` and `committed_task_count=4`, `state=active`, `started_at` set, writes one `SPRINT_STARTED` scope event (`scope_points_after=10`, `remaining_points_after=10`), and creates a day-0 `sprint_burndown_snapshots` row with `remaining_points=10`.
2. **One active sprint per project.** `POST /sprints/{id}/start` when another sprint in the same project is `active` returns `409 active_sprint_exists`; the DB partial unique index also rejects it.
3. **Add/remove capture.** Setting `sprint_id` to the active sprint on a 5-pt task writes a `TASK_ADDED` event (`points_delta=+5`, `scope_points_after` increased) and bumps `sprint_velocity.added_points` by 5; clearing it writes `TASK_REMOVED` (`points_delta=−5`) and bumps `removed_points`.
4. **Complete/reopen capture.** Moving an in-sprint 3-pt task into a `COMPLETED`-category status writes `TASK_COMPLETED` (`remaining_points_after` −3) and raises `completed_points`; moving it back out writes `TASK_REOPENED` reversing both.
5. **Estimate-change capture.** Changing an in-sprint task's `estimate` 2→5 writes `ESTIMATE_CHANGED` with `points_before=2`, `points_after=5`, `points_delta=+3`, and adjusts `scope_points_after`/`remaining_points_after` by +3.
6. **`compute_velocity` purity & math.** Pure (no DB; identical inputs → identical output). For a constructed task set: `committed`, `completed`, `added`, `removed`, `carryover` match hand-computed values; `predictability = completed/committed`; `predictability == 0.0` when `committed == 0` (no divide-by-zero).
7. **Complete finalizes velocity + carryover.** Completing a sprint with committed 34 / completed 25 sets `state=completed`, `completed_at`, finalizes `sprint_velocity` (`carryover_points=9`, `carryover_task_count` correct), moves the 3 incomplete tasks per the chosen `CarryoverTarget` (backlog → `sprint_id=null`; next_sprint → target id), and writes a `SPRINT_COMPLETED` event.
8. **Burndown snapshot idempotency.** `sprint.snapshot_burndown` writes exactly one row per active sprint per `snapshot_date`; re-running the same day updates that row (no duplicate) per `UNIQUE(sprint_id, snapshot_date)`.
9. **Burndown read.** `GET /sprints/{id}/burndown` returns ordered points `start_date..min(end_date, today)`; `ideal_points` is a linear committed→0 line reaching `0.0` on `end_date`; for an active sprint the final point reflects live remaining.
10. **`compute_burndown` purity.** Pure; given an ordered `ScopeEvent` list + window, produces deterministic per-day `remaining_points`; the day-of-completion remaining equals `committed + added − removed − completed`.
11. **Velocity dashboard.** `GET /projects/{id}/velocity?last=6` returns up to 6 **completed** sprints oldest→newest (excludes `planned`/`active`/`cancelled`), `summary.average_velocity == mean(completed_points)`, `rolling_3_velocity == mean(last 3 completed)`, and `predictability_avg` correct.
12. **Sprint report.** `GET /sprints/{id}/report` returns `VelocityResult` plus `completed`/`carryover`/`added`/`removed` task lists whose point sums reconcile with the rollup counters.
13. **RBAC.** `viewer` gets 200 on every sprint/velocity GET; `viewer` `POST .../start|complete|cancel|recompute` → 403; `member` may start/complete/cancel; any cross-workspace `sprint_id`/`project_id` → 404 (no existence leak).
14. **Recompute idempotency.** Running `sprint.recompute_velocity` twice for an unchanged sprint yields identical `sprint_velocity` content except `computed_at`/`velocity_version`.
15. **Cancel.** Cancelling a `planned` or `active` sprint sets `state=cancelled`, returns its tasks to the backlog, writes a `SPRINT_CANCELLED` event, and the sprint never appears in `GET /velocity` history.
16. **Live SSE.** With an open F01 stream on project P, starting/completing a sprint or any captured scope change emits a `sprint.updated` (and burndown a `burndown.updated`) envelope referencing that sprint id within 1s.
17. **Frontend charts.** `BurndownChart` renders ideal + actual + scope series from `BurndownSeries`; `VelocityBarChart` renders paired committed/completed bars; `CompleteSprintDialog` requires a `next_sprint_id` when carryover = next-sprint (Vitest component + Playwright e2e).
18. **Derived-state rebuild.** Dropping all `sprint_velocity` + `sprint_burndown_snapshots` rows and running `sprint.reconcile_sprint(id)` reproduces byte-identical `sprint_velocity` content and per-day burndown points (vs the live path) from `sprint_scope_events` for the same source data.

## 7. Test plan (TDD) — concrete test cases (unit + integration), key fixtures

Write tests first; each maps to an AC#. `pytest` + `pytest-asyncio`; integration against real Postgres via `testcontainers[postgres]` (enums, partial unique index, jsonb); Celery with `task_always_eager=True`.

**board-core unit** (`packages/board-core/tests/`, no DB):
- `test_velocity.py`: `compute_velocity` table-driven — all-completed → predictability 1.0; committed 0 → 0.0 (AC #6); added/removed/carryover arithmetic; determinism (call twice, assert equal). `compute_velocity_summary` — average, rolling-3, forecast low/avg/high; empty history → zeros.
- `test_burndown.py`: `ideal_line` reaches 0.0 on `end_date` and is monotone; `compute_burndown` per-day remaining from an ordered event list; day-of-completion identity `committed+added−removed−completed` (AC #10); purity (AC #10).
- `test_sprint_state.py`: `SprintStateMachine.assert_transition` allows planned→active→completed and planned/active→cancelled; rejects completed→active, active→planned with `SprintStateError`.

**API + worker integration** (`apps/api/tests/sprint/`, `httpx.AsyncClient`, factory-boy):
- `test_start.py`: committed-scope snapshot incl. excluding already-done/cancelled tasks, day-0 snapshot, `SPRINT_STARTED` event (AC #1); second active start → 409 + index rejection (AC #2).
- `test_scope_capture.py`: add/remove (AC #3), complete/reopen (AC #4), estimate change (AC #5) each write the right event and bump the rollup; **no** event when the task is in a `planned` sprint.
- `test_complete.py`: finalize + carryover to backlog / next_sprint / leave (AC #7); `next_sprint` without id → 422; report sums reconcile (AC #12).
- `test_cancel.py`: cancel from planned and active; tasks returned to backlog; excluded from velocity (AC #15).
- `test_burndown_api.py`: snapshot task idempotency (AC #8); `GET /burndown` ordering + ideal line + live last point (AC #9).
- `test_velocity_api.py`: dashboard filters completed-only, average/rolling-3/predictability (AC #11); export CSV row-per-sprint.
- `test_recompute_reconcile.py`: recompute idempotency (AC #14); drop-and-reconcile byte-identical to live (AC #18).
- `test_rbac.py`: viewer GET 200 / mutate 403; member start/complete; cross-workspace 404 (AC #13).
- `test_sse.py`: open F01 stream, start sprint + scope change, assert `sprint.updated`/`burndown.updated` within timeout (AC #16).

**Frontend** (`apps/web`, Vitest + Testing Library + MSW; Playwright e2e):
- `BurndownChart.test.tsx`: renders ideal/actual/scope series from a fixture (AC #17).
- `VelocityBarChart.test.tsx`: paired committed/completed bars (AC #17).
- `CompleteSprintDialog.test.tsx`: next-sprint carryover disables confirm until a sprint is picked (AC #17).
- `sprint-flow.spec.ts` (Playwright): plan → start → complete with carryover, no full-page reload, live chip update.

**Key fixtures**: extend F01's `project_factory`/`task_factory`; add `sprint_factory(state=, capacity_points=)`, `active_sprint(project, tasks=[(points,status_category)])` (creates + starts + assigns), `scope_event_factory`, and `completed_sprint_history(project, n, velocities=[...])` for dashboard tests. A `seed_velocity_history` fixture (6 completed sprints with known committed/completed) backs the velocity-summary assertions.

## 8. Security & policy considerations

- **Platform non-negotiables (scoped check).** F26 makes no model-provider calls (BYOK N/A), performs no retrieval (hybrid-retrieval N/A), and registers no MCP calls (MCP read-only-by-default N/A). It introduces no workflow gate and no PR/merge path, so it can neither bypass spec-gated implementation nor human-approval-before-merge; per "Derived state is advisory" below, no gate/agent/merge check reads `sprint_velocity`/`sprint_burndown_snapshots`. The applicable non-negotiables — tenant isolation, RBAC, and the append-only audit log — are honored as detailed in this section.
- **Tenant isolation.** Every sprint/scope/snapshot/velocity row carries `workspace_id`; all reads go through F01's `scoped_query`; cross-workspace ids return 404 (no existence leak), consistent with F01/F23.
- **RBAC** (`cross-cutting/F37-auth-secrets-byok`). `viewer` read-only across all sprint/velocity routes; `member` may create/edit/start/complete/cancel sprints within the workspace; `admin` adds delete; `agent-runner` may **trigger** scope events only as a side effect of legitimate task writes it is already permitted to make (it cannot start/complete sprints or call `recompute`). Enforced via `require_role(...)`.
- **Append-only audit (non-negotiable).** `sprint_scope_events` is append-only — service-layer enforced (no update/delete) and hardened DB-side via `attach_immutability_trigger(sprint_scope_events)` from `cross-cutting/F39-audit-log` (the same pattern F01's `activity_events` uses) — the immutable record that makes velocity auditable and reconstructable. It is **not** the central audit log: every security-relevant lifecycle action (`start`/`complete`/`cancel`/`recompute`) and every agent-runner-triggered scope event additionally emits a redacted `AuditEvent` through F39's `AuditSink` with a `detail_ref` back to the originating `sprints`/`sprint_scope_events` row, satisfying the spec Security requirement "Audit log — every agent action … immutable, queryable."
- **Derived state is advisory.** `sprint_velocity`/`sprint_burndown_snapshots` are display/reporting only; no workflow gate, agent decision, or merge check may read them (documented + a contract test asserting nothing in the workflow/agent packages imports the velocity read service).
- **Input validation.** All bodies Pydantic v2 validated; `last`/`limit` capped (velocity `last ≤ 26`, sprint list `limit ≤ 250`); `end_date ≥ start_date` enforced by service + DB check constraint; carryover `next_sprint_id` must belong to the same project (404 otherwise).
- **No secrets in the surface.** Sprint/velocity payloads contain only ids, names, goals, points, and dates — no credentials; the SSE envelope publisher reuses the shared `SecretRedactor` (from `cross-cutting/F37-auth-secrets-byok` / `cross-cutting/F39-audit-log`) that F01's `board_core.events.publish` already applies before publish.
- **Rate limiting.** `POST /recompute` is member/admin-only and rate-limited per project (the Redis limiter from `cross-cutting/F37-auth-secrets-byok`) since it enqueues worker recompute; the daily snapshot task is internal (not user-triggerable beyond `recompute`).

## 9. Effort estimate & risk (S/M/L + key risks)

**Effort: M** (~2–2.5 engineer-weeks: ~1 backend/board-core compute+lifecycle+hook, ~0.75 frontend charts, ~0.5 tests/wiring). Smaller than F01; read-mostly with one new write seam.

Key risks:
1. **Scope-capture coverage (medium).** If a task-mutation path (move/bulk/patch) forgets to fire `record_scope_event`, history silently drifts. Mitigation: single shared hook called from all three F01 write services; `sprint.reconcile_sprint` rebuilds from the log as the safety net; AC #3–#5 + #18 guard it.
2. **Retroactive estimate edits (medium).** Editing an estimate after completion would corrupt naive recomputation. Mitigation: burndown history is **persisted daily** (authoritative), and velocity is computed from the event log with `points_before/after` deltas rather than re-reading current estimates; reconcile replays the log.
3. **"Done" definition (low/medium).** Hardcoding a status name breaks custom workflows. Mitigation: derive completion from `StatusCategory.COMPLETED` (F01), tested with a custom-status project fixture.
4. **Active-sprint contention (low).** Two concurrent `start` calls. Mitigation: DB partial unique index `uq_active_sprint_per_project` makes it fail-closed (AC #2), not just the service check.
5. **Charting dependency (low).** Recharts is the one new web dep; isolated to `packages/ui-kit/src/sprint/` so it can be swapped without touching API/domain.
6. **Calendar vs working days (low).** v2 ideal line uses calendar days; a working-day/holiday calendar is §12 future — documented so burndown isn't misread over weekends.

## 10. Key files / paths (exact)

Backend / domain:
- `packages/board-core/src/board_core/velocity.py`
- `packages/board-core/src/board_core/sprint_state.py`
- `packages/board-core/src/board_core/enums.py` (extend: `SprintState`, `SprintScopeEventType`, `CarryoverTarget`)
- `packages/board-core/src/board_core/models/{sprint,sprint_scope_event,sprint_burndown_snapshot,sprint_velocity}.py`
- `packages/board-core/src/board_core/services/{sprints,velocity}.py`
- `packages/board-core/src/board_core/errors.py` (extend: `ActiveSprintExistsError`, `SprintStateError`)
- `packages/board-core/tests/test_{velocity,burndown,sprint_state}.py`
- `apps/api/forge_api/routers/board/{sprints,velocity}.py`
- `apps/api/forge_api/schemas/sprint.py`
- `apps/worker/forge_worker/tasks/sprint_tasks.py` (Celery `snapshot_burndown`, `recompute_velocity`, `reconcile_sprint`) + Beat schedule entry in `apps/worker/forge_worker/beat.py`
- `apps/api/forge_api/cli/sprint.py` (`forge-cli sprint reconcile|velocity`)
- `apps/api/alembic/versions/<rev>_sprint_velocity.py` (extend `sprints`, add enum value `cancelled`, partial unique index, 3 new tables; `<rev>` assigned at creation, `down_revision` chains after F01's `0002_board_core`)
- `apps/api/tests/sprint/test_*.py`

Frontend:
- `apps/web/app/[workspace]/projects/[projectKey]/sprints/page.tsx`
- `apps/web/app/[workspace]/projects/[projectKey]/sprints/[sprintId]/page.tsx`
- `apps/web/app/[workspace]/projects/[projectKey]/velocity/page.tsx`
- `apps/web/components/board/sprint/{SprintList,SprintPlanningPanel,StartSprintButton,CompleteSprintDialog,BurndownChart,SprintReport,ScopeChangeChips,VelocityBarChart,VelocitySummaryCards,VelocityForecastBand,SprintEmptyState}.tsx`
- `apps/web/lib/board/{sprintApi,sprintQueries}.ts`
- `packages/ui-kit/src/sprint/{BurndownChart,VelocityBarChart,CoverageBar}.tsx`
- `apps/web/tests/sprint/*.{test.tsx,spec.ts}`

Infra:
- `apps/worker/forge_worker/beat.py` (register the `sprint.snapshot_burndown` daily Beat schedule entry — code, not compose; the existing `worker` service already runs Beat)
- `apps/web/package.json` (add `recharts`)

## 11. Research references (relevant links from the spec/research report)

- `docs/FORGE_SPEC.md` — "Native Project Board → Board Features" (*Sprint management — Create sprints, move tasks, track velocity*) and "Entity Hierarchy" (`Sprint (optional time-boxed container)`); "Core Data Model" (`Project.Sprint[]`); "UX Standards" (keyboard-first, optimistic, no full-page reload, sub-100ms); "Observability and Evaluation → Key Metrics" (task completion rate, mean time to task completion — the throughput velocity reports); "Phased Roadmap → Phase 2 (V2)" item *"Sprint management and velocity dashboards"*.
- `docs/implementation-slices/v1/F01-project-board.md` — upstream contracts consumed: `sprints` table, `tasks.estimate`/`sprint_id`/`status_id`, `StatusCategory.COMPLETED/CANCELED`, `activity_events` (append-only pattern), `scoped_query`, `ranking.rank_between`, `board:{project_id}` SSE channel + `useBoardStream`, bulk `set_sprint_id`; and F01's explicit deferral of velocity/burndown to this slice.
- `docs/implementation-slices/v2/F23-spec-validation-dashboard.md` — the derived-projection pattern (rebuildable from source, idempotent refresh, SSE `{id, version}` patch, viewer-read/member-recompute RBAC) F26 mirrors.
- `docs/forge-research-report.md` — "What Makes Forge Buildable → Eval-first / measured-not-estimated": velocity is a *measured* throughput signal, not an estimate; F26 records the raw event log so the metric is auditable.
- Linear (sprint/cycle + velocity UX reference): https://linear.app/ · Plane.so (OSS sprint/cycle reference): https://plane.so/ · TanStack Query (live cache): https://tanstack.com/query · shadcn/ui charts on Recharts (chart layer): https://ui.shadcn.com/ · SQLAlchemy 2.x / Alembic / Pydantic v2 (stack): https://docs.sqlalchemy.org/en/20/ , https://alembic.sqlalchemy.org/en/latest/ , https://docs.pydantic.dev/latest/

## 12. Out of scope / future

- **Team-scoped / parallel active sprints** — v2 enforces one active sprint per project (partial unique index). Per-team concurrent sprints and a "cross-team" rollup are future; the schema already carries `project_id` and could add `team_id` without breaking the event log.
- **Working-day / holiday calendar for the ideal line** — v2 uses inclusive calendar days; a configurable working-day calendar (skip weekends/holidays, capacity per day) is future.
- **Capacity planning from per-member availability** — `capacity_points` is a single display/forecast number; per-assignee capacity, leave, and load balancing are future.
- **Story-point estimation scales** — F26 uses the existing integer `tasks.estimate` as points; Fibonacci/T-shirt scales and re-estimation history are future (the `ESTIMATE_CHANGED` log already captures transitions).
- **Cross-project / portfolio velocity** — F26 scopes to one project; a workspace portfolio dashboard is a later aggregate over the same `sprint_velocity` rollup (cf. F23 §12).
- **Historical trend lines beyond the rollup** (e.g. cumulative-flow diagram, cycle-time/lead-time scatter) — needs additional time-series; F26 ships burndown + velocity only.
- **Automations on sprint events** ("when sprint completes → roll carryover automatically", "auto-start sprint on start_date") — F26 ships manual lifecycle + an optional opt-in note only; rule-driven automation belongs to the v2 automation rule engine (F01 §12).
- **Sprint goal tracking against acceptance criteria** (linking sprint goal to spec validation) — out of scope; that is F23's surface.
