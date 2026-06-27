# F23 — Spec Validation Dashboard (Requirement Traceability)

> Phase: v2 · Spec module(s): Spec Engine (`packages/spec-engine` — traceability/validation), Review & Approval Layer (requirement→diff/test traceability UI), Observability Layer (spec metrics from F12) · Status target: "Done" = a project-scoped, keyboard-navigable **Spec Validation Dashboard** renders, for every spec in a project, the requirement → acceptance-criterion → test → diff → PR traceability matrix with a live coverage rollup (requirement coverage, acceptance-criteria coverage, gap counts, staleness); the dashboard reads from a denormalized projection that is refreshed event-driven (on validation, spec edit, PR/CI events) and reconciled periodically; gaps (uncovered requirements, untested criteria, failed criteria, stale validations) are computed, listed, and drillable into the spec, the run trace (F10), and the PR (F08); the data is exportable as CSV/JSON; reads are sub-100ms and RBAC-scoped to the workspace. The rollup/staleness/gap logic is pure and unit-tested; the projection refresh and routes are integration-tested against real Postgres.

---

## 1. Intent — what & why

The Forge spec (`Spec Gating Rules`) requires: "Approval UI must show requirement-to-diff and requirement-to-test traceability." F02 (Spec Engine) already persists the underlying truth — `spec_requirements`, `spec_acceptance_criteria`, `spec_validation_reports` (with `CriterionVerdict`s), and a per-spec `GET /traceability` endpoint — and F02 explicitly defers "Spec validation dashboard with full requirement traceability visualization" to v2. **F23 is that dashboard.**

The single-spec `TraceRow` from F02 answers "is this one spec validated?" but cannot answer the questions an engineering lead actually asks:

1. **Project health:** across all specs in this project, what fraction of requirements are validated, what fraction of acceptance criteria are covered by a passing test, and where are the gaps?
2. **Traceability at a glance:** for a given requirement, which acceptance criteria address it, which tests prove each criterion, which diffs/PRs implemented it, and is that evidence still current?
3. **Staleness:** an acceptance criterion that was validated against spec v2 but the spec is now v5 — its "validated" badge is a lie. The dashboard must surface stale validations as a first-class gap.
4. **Drill-down:** from a red cell, jump to the failing test in the run trace (F10), the PR diff (F08), or the spec criterion editor (F02).

Why a projection (not live joins): a project can hold dozens of specs, each with many criteria, each with verdicts that span `spec_validation_reports` (F02), the `pull_request.traceability` jsonb of `TraceabilityRow`s (F08), and `tasks` (F01, via `pull_request.workflow_run_id → workflow_run.task_id → tasks.spec_id`). Joining those per page load would blow the board's sub-100ms interaction budget. F23 maintains a **denormalized read model** (`traceability_criterion_links` + `traceability_spec_rollup`) refreshed event-driven, so the dashboard reads are flat table scans on indexed `project_id`.

This feature is read-mostly and additive: it introduces no new gates and changes no lifecycle. It consumes F02's contracts and adds one small write-path field (`spec_version` on validation reports) needed for staleness.

## 2. User-facing behavior / journeys

**Journey A — Project validation overview.** A lead opens **Validation** in the project nav (`/projects/{id}/validation`). The page shows summary cards: *Requirement coverage 78% (39/50)*, *Acceptance-criteria coverage 71% (85/120)*, *Specs validated 6/14*, *Open gaps 23*, *Stale validations 4*. Below is a sortable, filterable table (TanStack Table) of specs: key, name, spec status, AC coverage bar, requirement coverage bar, gap count, last validated, a "stale" pill. Filters: spec status, epic, "only with gaps", "only stale", text search. Sub-100ms sort/filter (client-side over the projection rows).

**Journey B — Per-spec traceability matrix.** Clicking a spec row opens `/specs/{specId}/validation`. The matrix lists one expandable row per **requirement** (R1, R2, …) with its text and a rollup chip (`validated` / `partial` / `uncovered` / `failed`). Expanding shows its acceptance criteria as cells, each with: status chip (`validated` green / `claimed` amber / `failed` red / `uncovered` grey / `stale` striped), the mapped test node ids, the changed files / diff refs, and the PR number. Each evidence item is a link: test → run trace step (F10), diff → PR file (F08), criterion → spec criterion (F02).

**Journey C — Gap triage.** A "Gaps" tab on either page lists actionable gaps: *R7 has no acceptance criteria*, *A12 has an agent claim but no passing test*, *A4 failed in TASK-88's last validation*, *A9 was validated against spec v2, current is v5 (stale)*. Each gap links to the place to fix it. The list is the dashboard's worklist.

**Journey D — Live update.** While the lead watches the dashboard, an agent run finishes and a new `ValidationReport` lands. Within ~2s the affected spec row's coverage bar and the matrix cells update in place (SSE/WebSocket push), no reload. A "stale" pill clears when the spec is re-validated against the current version.

**Journey E — Export / audit.** The lead clicks **Export** and downloads `validation-{project}-{date}.csv` (one row per criterion: spec_key, requirement_ids, criterion_id, status, tests, pr, validated_at, stale) or `.json` (the full structured matrix). Used for audit evidence and offline review.

**Journey F — Force refresh.** If a member suspects the projection is stale (e.g. after a manual DB edit during dev), they click **Recompute** (member/admin only), which enqueues a refresh for that spec and shows a spinner until the projection version advances.

## 3. Vertical slice

### 3.1 Data model (tables/columns/migrations touched)

New Alembic migration `apps/api/alembic/versions/spec_validation_dashboard.py` (revision id `spec_validation_dashboard`; `down_revision` chains after the latest of F02's `0003_spec_engine` and F08's `pull_request`/`verification_report` migration — both must precede it). One new enum `traceability_cell_status`; two new projection tables; one additive column on an F02 table. All ORM models import the shared `Base`/`TimestampMixin` and async session from `packages/db-core` (foundation), matching F02's migration/model placement convention.

**Additive change to F02 `spec_validation_reports`** (needed for staleness):
- ADD COLUMN `spec_version` int NOT NULL DEFAULT 1 — the `spec_documents.current_version` the report was built against. The F02 validation write path (`POST /specs/{id}/validation`, `spec.snapshot_artifacts`) is updated to populate it from the spec aggregate at report-build time. Backfill existing rows with the spec's **current** `current_version` at migration time — a one-time approximation that marks all pre-migration reports as fresh (historical spec versions are not reconstructable); staleness becomes exact from the first post-migration validation onward.

**`traceability_criterion_links`** (read model; grain = one row per acceptance criterion per spec; rebuilt wholesale per spec on refresh)
- `id` uuid PK
- `workspace_id` uuid NOT NULL — denormalized for fast RBAC scoping
- `project_id` uuid FK → `projects.id` ON DELETE CASCADE NOT NULL
- `spec_id` uuid FK → `spec_documents.id` ON DELETE CASCADE NOT NULL
- `spec_key` text NOT NULL — denormalized (`SPEC-17`) for export without a join
- `criterion_ext_id` text NOT NULL — e.g. `A1`
- `criterion_text` text NOT NULL
- `requirement_ext_ids` jsonb NOT NULL DEFAULT `[]` — the requirement ids this AC references (`["R1","R3"]`)
- `status` enum `traceability_cell_status`(`uncovered`,`claimed`,`validated`,`failed`,`stale`) NOT NULL
- `satisfied` bool NOT NULL DEFAULT false — latest verdict's `satisfied`
- `test_refs` jsonb NOT NULL DEFAULT `[]` — test node ids proving this AC
- `diff_refs` jsonb NOT NULL DEFAULT `[]` — changed files / diff anchors
- `task_ids` jsonb NOT NULL DEFAULT `[]` — tasks whose validation touched this AC
- `pr_numbers` jsonb NOT NULL DEFAULT `[]` — GitHub PR numbers (from F08)
- `last_report_id` uuid NULL FK → `spec_validation_reports.id` ON DELETE SET NULL
- `report_spec_version` int NULL — spec_version the latest verdict was built against
- `current_spec_version` int NOT NULL — spec's `current_version` at refresh time (drives `stale`)
- `last_validated_at` timestamptz NULL
- `refreshed_at` timestamptz NOT NULL
- UNIQUE(`spec_id`, `criterion_ext_id`); INDEX(`project_id`, `status`); INDEX(`project_id`); INDEX(`spec_id`); INDEX(`workspace_id`)

**`traceability_spec_rollup`** (read model; grain = one row per spec; powers the project table in one scan)
- `spec_id` uuid PK FK → `spec_documents.id` ON DELETE CASCADE
- `workspace_id` uuid NOT NULL; `project_id` uuid FK NOT NULL; `spec_key` text NOT NULL; `spec_name` text NOT NULL
- `epic_id` uuid NULL
- `spec_status` text NOT NULL — mirror of `spec_documents.status` at refresh
- `total_requirements` int NOT NULL; `covered_requirements` int NOT NULL
- `total_criteria` int NOT NULL; `validated_criteria` int NOT NULL; `failed_criteria` int NOT NULL; `uncovered_criteria` int NOT NULL; `claimed_criteria` int NOT NULL
- `stale_criteria` int NOT NULL
- `requirement_coverage` numeric(5,4) NOT NULL — `covered_requirements / total_requirements` (0 if no reqs)
- `acceptance_criteria_coverage` numeric(5,4) NOT NULL — `validated_criteria / total_criteria` (0 if no AC)
- `uncovered_requirement_ext_ids` jsonb NOT NULL DEFAULT `[]` — requirements with zero AC
- `validation_status` text NOT NULL — `none` | `partial` | `passing` | `failing` | `stale`
- `gap_count` int NOT NULL
- `last_validated_at` timestamptz NULL
- `projection_version` bigint NOT NULL — monotonic counter bumped each refresh (lets the UI's "Recompute" detect completion)
- `refreshed_at` timestamptz NOT NULL
- INDEX(`project_id`, `validation_status`); INDEX(`project_id`); INDEX(`workspace_id`); INDEX(`epic_id`)

> Both projection tables are **derived state**: they can be dropped and fully rebuilt from F02 + F08 source rows by `dashboard.reconcile_project`. Nothing reads them as source of truth; gate decisions (F02/F08) never consult them.

### 3.2 Backend (FastAPI routes + services/packages)

Domain logic lives in **`packages/spec-engine`** (extends F02; same package owns requirement/AC/validation truth). New modules:

- `forge_spec_engine/dashboard.py` — pure rollup/gap/staleness functions (no I/O).
- `forge_spec_engine/projection.py` — `TraceabilityProjector` (reads F02 + F08 source rows via injected ports, upserts the two projection tables) and `ProjectionRepository`.
- `forge_spec_engine/dashboard_schemas.py` — Pydantic v2 DTOs (§4).

`apps/api` holds thin routers + DI; `apps/worker` holds the refresh tasks.

FastAPI router `apps/api/forge_api/routers/spec_dashboard.py`, prefix `/api/v1`, all routes authenticated, RBAC in §8:

- `GET /projects/{project_id}/validation/summary` → `ProjectValidationSummary` (aggregate of `traceability_spec_rollup`).
- `GET /projects/{project_id}/validation/specs` → `Page[SpecValidationRow]`. Query: `status`, `epic_id`, `has_gaps: bool`, `stale: bool`, `q` (matches key/name), `sort`, `cursor`, `limit` (default 50, max 200). Pure read of `traceability_spec_rollup`.
- `GET /specs/{spec_id}/validation/matrix` → `SpecTraceabilityMatrix` (requirement rows with nested criterion cells + evidence + drill links). Reads `traceability_criterion_links` + spec requirement/AC rows.
- `GET /specs/{spec_id}/validation/gaps` → `list[TraceabilityGap]`.
- `GET /projects/{project_id}/validation/export?format=csv|json` → streaming `text/csv` or `application/json` (one criterion per row/object). Honors the same filters as `/specs`.
- `POST /specs/{spec_id}/validation/refresh` → enqueues `dashboard.refresh_spec_traceability(spec_id)`; returns `{ enqueued: true, projection_version }` where `projection_version` is the **current** (pre-refresh) value, so the UI polls `/matrix` until it advances (member/admin). The enqueue is recorded in the audit log (§8).
- `GET /projects/{project_id}/validation/stream` → SSE channel emitting `dashboard.updated {spec_id, projection_version}` events for live UI updates (reuses F10's event-stream infra).

`DashboardService` (`packages/spec-engine/src/forge_spec_engine/dashboard_service.py`) is the single entry point routers call; it reads projection rows through `ProjectionRepository` and assembles DTOs (it does **not** compute on read — computation happens in the refresh path). It takes injected `ProjectionRepository`, `SpecRepository` (F02), and `WorkspaceScope` (resolves `project_id → workspace_id` for RBAC).

CLI (`apps/api/forge_cli/validation_dashboard.py`, under the `forge` Typer app): `forge validation dashboard <project-id> [--json]` prints the project summary + per-spec table; `forge validation reconcile <project-id>` forces a full rebuild (ops/debug + golden-eval use).

### 3.3 Worker / agent runtime (Celery tasks, LangGraph, if any)

No LangGraph. Celery tasks in `apps/worker/forge_worker/tasks/dashboard_tasks.py`, queue `dashboard`:

- `dashboard.refresh_spec_traceability(spec_id)` — loads the spec aggregate (F02) + its validation reports + the `pull_request.traceability` rows tied to the spec's tasks (F08, resolved via `pull_request.workflow_run_id → workflow_run.task_id → tasks.spec_id`), runs `TraceabilityProjector.refresh_spec`, upserts both projection tables in one transaction (bumps `projection_version`), then emits a `dashboard.updated` event on the project channel. Idempotent: recomputes wholesale, so duplicate deliveries converge.
- `dashboard.reconcile_project(project_id)` — iterates every spec in the project and calls the refresh logic; used by the periodic beat job and `forge validation reconcile`.

**Event subscriptions** — F23 binds to the **existing** domain/timeline events emitted by F02 and F03/F08 (Forge has no generic abstract "event bus"; the concrete carriers are the board activity-event timeline used by F02, the F03/F08 GitHub domain events, and Redis pub/sub from the foundation). Each handler resolves the affected `spec_id` and enqueues `refresh_spec_traceability(spec_id)`:
- `spec.validation.recorded` — **primary trigger.** F23 adds this emission to the F02 validation write path (`POST /specs/{id}/validation`) — the same path F23 already extends to populate `spec_version` — so every stored `ValidationReport` (including the one F08's `run_checks` submits to F02) fires it.
- `spec_decision` activity event (F02, board-core `POST /internal/activity-events`, `event_type=spec_decision`) — F02 emits exactly one per spec **status transition** and per generation phase; F23 subscribes and refreshes on transitions/manifest updates because they change coverage denominators and staleness.
- `pull_request_opened`, `pull_request_merged` (F03/F08 domain events) — PR evidence changes; resolve `spec_id` via `pull_request.workflow_run_id → workflow_run.task_id → tasks.spec_id`.
- `ci_status_changed` (F03/F08 domain event) — CI/test evidence may change an AC's `test_refs`; the authoritative re-validation also re-fires `spec.validation.recorded`, so this is a secondary safety trigger.

A Celery beat entry runs `dashboard.reconcile_project` for each active project every 6h to heal any missed event (drift safety net; the spec calls out freshness SLAs elsewhere — this is the dashboard's).

### 3.4 Frontend / UI (Next.js routes/components, if any)

Next.js (App Router) under `apps/web`:
- `app/projects/[projectId]/validation/page.tsx` — project dashboard: `CoverageSummaryCards`, `SpecValidationTable` (TanStack Table over `/validation/specs`), `ValidationFilters`, `ExportButton`. Server-renders the first page; client hydrates with TanStack Query + SSE subscription to `/validation/stream` for live patches.
- `app/specs/[specId]/validation/page.tsx` — per-spec matrix: `RequirementTraceMatrix` (expandable requirement rows → criterion cells), `GapList` tab, `RecomputeButton`. (This page is also surfaced as a "Validation" tab on the F02 spec detail page.)

Shared components in `packages/ui-kit/src/validation/`: `CoverageSummaryCards`, `CoverageBar` (validated/claimed/failed/uncovered segmented bar), `CellStatusBadge` (validated/claimed/failed/uncovered/stale), `SpecValidationTable`, `RequirementTraceMatrix`, `TraceCell`, `GapList`, `StalePill`, `ValidationFilters`, `ExportButton`. Drill links resolve to F02 spec routes, F08 PR/diff view, and F10 run-trace step deep links. Data hooks in `apps/web/src/lib/api/validation-dashboard.ts` (TanStack Query). Keyboard-first: `j/k` move spec rows, `Enter` opens matrix, `g` jumps to gaps, `/` focuses search, `e` exports — matching the board UX standard.

### 3.5 Infra / deploy (compose, helm, caddy, if any)

No new services. Adds the `dashboard` Celery queue to the existing `worker` (compose `worker` command and the Helm worker deployment add `-Q dashboard`) and one Celery-beat schedule entry for `dashboard.reconcile_project`. Reuses `db`, `redis`, `api`, `web`. No Caddy changes. SSE reuses the streaming route config already added by F10.

## 4. Public interfaces / contracts (exact signatures, Pydantic models, Protocols, YAML schemas)

DTOs (`packages/spec-engine/src/forge_spec_engine/dashboard_schemas.py`), Pydantic v2:

```python
from enum import StrEnum
from datetime import datetime
from pydantic import BaseModel, Field

class CellStatus(StrEnum):
    UNCOVERED = "uncovered"   # no report references this AC
    CLAIMED = "claimed"       # agent claim but no passing test ref
    VALIDATED = "validated"   # satisfied=true against the current spec version
    FAILED = "failed"         # latest report attempted this AC, satisfied=false
    STALE = "stale"           # was validated, but report_spec_version < current_spec_version

class ValidationStatus(StrEnum):
    NONE = "none"; PARTIAL = "partial"; PASSING = "passing"
    FAILING = "failing"; STALE = "stale"

class TraceCell(BaseModel):
    criterion_id: str                       # "A1"
    criterion_text: str
    requirement_ids: list[str]              # ["R1","R3"]
    status: CellStatus
    test_refs: list[str] = Field(default_factory=list)
    diff_refs: list[str] = Field(default_factory=list)
    task_ids: list[str] = Field(default_factory=list)
    pr_numbers: list[int] = Field(default_factory=list)
    last_validated_at: datetime | None = None
    report_spec_version: int | None = None

class RequirementTraceRow(BaseModel):
    requirement_id: str                     # "R1"
    requirement_text: str
    rollup_status: CellStatus               # worst-of its criteria (uncovered if it has none)
    criteria: list[TraceCell]

class SpecTraceabilityMatrix(BaseModel):
    spec_id: str; spec_key: str; spec_name: str
    spec_status: str
    current_spec_version: int
    requirement_coverage: float             # covered_requirements / total_requirements
    acceptance_criteria_coverage: float     # validated_criteria / total_criteria
    validation_status: ValidationStatus
    rows: list[RequirementTraceRow]
    last_validated_at: datetime | None = None
    projection_version: int

class SpecValidationRow(BaseModel):
    spec_id: str; spec_key: str; spec_name: str
    epic_id: str | None = None
    spec_status: str
    requirement_coverage: float
    acceptance_criteria_coverage: float
    total_requirements: int; covered_requirements: int
    total_criteria: int; validated_criteria: int
    failed_criteria: int; uncovered_criteria: int; stale_criteria: int
    validation_status: ValidationStatus
    gap_count: int
    last_validated_at: datetime | None = None

class ProjectValidationSummary(BaseModel):
    project_id: str
    spec_count: int; specs_validated: int
    total_requirements: int; covered_requirements: int
    total_criteria: int; validated_criteria: int
    requirement_coverage: float
    acceptance_criteria_coverage: float
    open_gap_count: int; stale_validation_count: int

class GapKind(StrEnum):
    REQUIREMENT_NO_CRITERIA = "requirement_no_criteria"
    CRITERION_NO_TEST = "criterion_no_test"          # claimed, never validated
    CRITERION_FAILED = "criterion_failed"
    CRITERION_STALE = "criterion_stale"

class TraceabilityGap(BaseModel):
    spec_id: str; spec_key: str
    kind: GapKind
    requirement_id: str | None = None
    criterion_id: str | None = None
    detail: str                              # human-readable, e.g. "A9 validated against v2, current v5"
    deep_link: str                           # route to fix it (spec criterion / run trace / PR)
```

Pure rollup logic (`forge_spec_engine/dashboard.py`) — no I/O, deterministic:

```python
from forge_spec_engine.schemas import SpecManifest
from forge_spec_engine.validation import ValidationReport, CriterionVerdict

def classify_cell(
    *, criterion_id: str, verdict: CriterionVerdict | None,
    report_spec_version: int | None, current_spec_version: int,
) -> CellStatus: ...
# None verdict -> UNCOVERED; verdict.satisfied & version current -> VALIDATED;
# verdict.satisfied & report_spec_version < current -> STALE;
# verdict present, not satisfied, has test_refs/attempted -> FAILED;
# verdict present, claim only, no test_ref -> CLAIMED.

def build_criterion_links(
    manifest: SpecManifest, report: ValidationReport | None,
    *, current_spec_version: int, report_spec_version: int | None,
    evidence: "EvidenceIndex",
) -> list[TraceCell]: ...
# one TraceCell per manifest AC, merging F02 verdicts with F08 PR/test/diff evidence.

def compute_spec_rollup(
    manifest: SpecManifest, cells: list[TraceCell], *, spec_status: str,
) -> "SpecRollupValues": ...
# requirement_coverage: a requirement is COVERED iff it has >=1 referencing AC and
#   every referencing AC is VALIDATED (not stale/failed/claimed/uncovered).
# acceptance_criteria_coverage: count(VALIDATED) / count(all AC).
# validation_status: NONE (no AC validated & none failed) | FAILING (any FAILED) |
#   STALE (any STALE and none FAILED) | PASSING (all AC VALIDATED) | PARTIAL (otherwise).

def detect_gaps(
    manifest: SpecManifest, cells: list[TraceCell], *, spec_key: str, spec_id: str,
) -> list[TraceabilityGap]: ...
# REQUIREMENT_NO_CRITERIA: requirement id not referenced by any AC.
# CRITERION_NO_TEST: cell CLAIMED. CRITERION_FAILED: cell FAILED. CRITERION_STALE: cell STALE.
```

Source ports (`forge_spec_engine/projection.py`) — F23 reads F08/F02 truth through narrow Protocols so it is testable with doubles:

```python
from typing import Protocol

class EvidenceIndex(BaseModel):
    # per-criterion evidence harvested from F08 PRs/check runs, keyed by criterion_id
    test_refs: dict[str, list[str]] = {}
    diff_refs: dict[str, list[str]] = {}
    pr_numbers: dict[str, list[int]] = {}
    task_ids: dict[str, list[str]] = {}

class EvidencePort(Protocol):
    def evidence_for_spec(self, spec_id: str) -> EvidenceIndex: ...
    # Adapter reads F08 `pull_request` rows for the spec's tasks and their
    # `traceability` jsonb (list of F08 `TraceabilityRow`), mapping per criterion:
    #   TraceabilityRow.evidence_tests -> EvidenceIndex.test_refs
    #   TraceabilityRow.evidence_files -> EvidenceIndex.diff_refs
    #   pull_request.provider_pr_number -> EvidenceIndex.pr_numbers
    #   workflow_run.task_id           -> EvidenceIndex.task_ids
    # The no-op port (pre-F08) returns an empty EvidenceIndex (cells show verdict-only evidence).

class SpecSourcePort(Protocol):
    def load_manifest(self, spec_id: str) -> SpecManifest: ...
    def current_version(self, spec_id: str) -> int: ...
    def latest_report(self, spec_id: str) -> tuple[ValidationReport | None, int | None]: ...
        # (report, report_spec_version)
    def spec_header(self, spec_id: str) -> "SpecHeader": ...          # key, name, status, epic_id, ids

class TraceabilityProjector:
    def __init__(self, specs: SpecSourcePort, evidence: EvidencePort,
                 repo: "ProjectionRepository") -> None: ...
    def refresh_spec(self, spec_id: str) -> int: ...                  # returns new projection_version
    def reconcile_project(self, project_id: str) -> int: ...          # count of specs refreshed
```

CSV export schema (one row per criterion): `spec_key,requirement_ids,criterion_id,status,satisfied,test_refs,pr_numbers,last_validated_at,stale` (list columns pipe-joined). JSON export = `{ summary: ProjectValidationSummary, specs: list[SpecTraceabilityMatrix] }`.

> F23 supersedes F02's `GET /specs/{id}/traceability` for UI use (richer payload, projection-backed). F02's endpoint remains for the eval harness (F12) and is unchanged.

## 5. Dependencies — features/slices that must exist first

- `v1/F02-spec-engine` (REQUIRED, hard) — source of truth: `spec_requirements`, `spec_acceptance_criteria`, `spec_validation_reports` (+ the new `spec_version` column F23 adds and F02's write path populates), `SpecManifest`, `ValidationReport`/`CriterionVerdict`, `build_traceability_matrix`. F23 is meaningless without it.
- `v1/F08-plan-execute-verify-pr-approval` (REQUIRED, stubbable) — provides per-criterion PR evidence: the `pull_request` table and its `traceability` jsonb (list of F08 `TraceabilityRow{criterion_id, satisfied, evidence_files, evidence_tests, agent_rationale}`), consumed via `EvidencePort`. (There is no `verification_check_runs` table; F08's `verification_report`/`check_result` tables are per-check, not per-criterion, and are not read by F23.) A no-op `EvidencePort` returning empty evidence keeps F23 buildable before/without F08 (cells then show verdict-only evidence).
- `v1/F01-project-board` (REQUIRED) — `Project`, `Epic`, `Task` for scoping, `epic_id` filter, and resolving PR→`workflow_run`→`task`→`spec` (`tasks.spec_id` is F02-owned).
- `v1/F00-foundation-substrate` (REQUIRED) — Workspace/User/Project, `packages/db-core` shared `Base`/`TimestampMixin` + async SQLAlchemy session + Alembic baseline, auth + RBAC dependency (`admin`/`member`/`viewer`/`agent-runner`), Celery app + beat, Redis pub/sub, and the shared secret-redaction filter.
- `cross-cutting/F39-audit-log` (REQUIRED) — the immutable, queryable audit log F23 writes to on `POST /validation/refresh` (manual recompute) and on export (evidence download), satisfying the audit-log non-negotiable for the slice's two non-read actions.
- `v1/F10-run-trace-viewer` (SOFT) — SSE/event-stream transport reused by `/validation/stream` (the Redis pub/sub → SSE response helper; F23 publishes on a project-scoped channel, distinct from F10's run-scoped one), and the run-trace step deep-link target for test evidence. Live updates degrade to poll-on-focus if absent.
- `v1/F12-eval-harness` (SOFT, non-blocking) — F23 must keep `requirement_coverage` / `acceptance_criteria_coverage` numerically consistent with F12's existing `spec.py` metrics. Directionally, F23 (v2) mirrors F12 (v1), not the reverse: F23 introduces the shared formula in `forge_spec_engine` (the `packages/spec-engine` F12 already depends on) and a cross-consistency golden test pins F23 == F12 on the all-validated, current-version SPEC-001 fixture (where F23's `stale`/`failed` refinements — which F12's 3-status traceability does not model — do not apply, so the numbers provably coincide). Adopting the shared helper inside F12's `spec.py` is a follow-up, not a precondition.
- `v1/F11-skill-profiles` (SOFT) — not required; coverage is derived from validation reports, not skill thresholds.

No v2 features are prerequisites; F23 is additive on the v1 spec/validation stack.

## 6. Acceptance criteria (numbered, testable)

1. `dashboard.refresh_spec_traceability(spec_id)` writes exactly one `traceability_criterion_links` row per manifest acceptance criterion and exactly one `traceability_spec_rollup` row for the spec, and bumps `projection_version` by 1.
2. A criterion with a `satisfied=true` verdict built against the spec's `current_version` is classified `validated`; its row carries the verdict's `test_refs`.
3. A criterion with a `satisfied=true` verdict whose `report_spec_version < current_spec_version` is classified `stale` (not `validated`), and counts toward `stale_criteria` and a `CRITERION_STALE` gap.
4. A criterion with an agent claim but no passing test ref is classified `claimed` and produces a `CRITERION_NO_TEST` gap; a criterion with an attempted-but-unsatisfied latest verdict is `failed` and produces a `CRITERION_FAILED` gap.
5. A criterion with no verdict in any report is classified `uncovered`.
6. A requirement referenced by no acceptance criterion appears in `uncovered_requirement_ext_ids` and yields a `REQUIREMENT_NO_CRITERIA` gap.
7. `requirement_coverage` = covered_requirements / total_requirements, where a requirement is *covered* iff it has ≥1 referencing AC and **every** referencing AC is `validated`; equals 0.0 when the spec has no requirements (no divide-by-zero).
8. `acceptance_criteria_coverage` = validated_criteria / total_criteria; equals 0.0 when the spec has no AC.
9. `validation_status` resolves to `failing` if any AC is `failed`, else `stale` if any AC is `stale`, else `passing` if all AC are `validated`, else `none` if no AC is validated and none failed, else `partial`.
10. `GET /projects/{id}/validation/summary` returns a `ProjectValidationSummary` whose totals equal the sum of the project's `traceability_spec_rollup` rows.
11. `GET /projects/{id}/validation/specs?stale=true` returns only specs with `stale_criteria > 0`; `?has_gaps=true` returns only specs with `gap_count > 0`; `?epic_id=` filters by epic; `?q=` matches key or name (case-insensitive). Results are paginated by cursor with a 200 max limit.
12. `GET /specs/{id}/validation/matrix` returns one `RequirementTraceRow` per requirement with its nested `TraceCell`s; an AC referencing two requirements appears under both rows with identical cell content.
13. `GET /specs/{id}/validation/gaps` returns exactly the gaps implied by the matrix (one per uncovered requirement, claimed AC, failed AC, and stale AC) with populated `deep_link`s.
14. Emitting a `spec.validation.recorded` event enqueues `dashboard.refresh_spec_traceability` for that spec, and after it runs the matrix reflects the new verdicts (live path).
15. Re-validating a stale spec against the current version clears its `stale` cells (status → `validated`) and decrements `stale_validation_count` in the project summary on next refresh.
16. `dashboard.refresh_spec_traceability` is idempotent: running it twice for an unchanged spec leaves `traceability_criterion_links`/`traceability_spec_rollup` content identical except `projection_version`/`refreshed_at`.
17. `dashboard.reconcile_project(project_id)` rebuilt from empty projection tables yields byte-identical link/rollup content to the event-driven path for the same source data.
18. `GET /projects/{id}/validation/export?format=csv` streams one header + one row per criterion across all specs matching the active filters; `format=json` returns `{summary, specs[]}`.
19. All rollup/classification/gap functions in `dashboard.py` are pure: identical inputs yield identical outputs and touch no DB (verified by calling twice and by no DB session in the unit tests).
20. RBAC: a `viewer` can read all dashboard GET routes for their workspace; calling `POST /validation/refresh` as `viewer` returns 403; any cross-workspace `project_id`/`spec_id` returns 404 (no existence disclosure).
21. The F02 validation write path populates `spec_validation_reports.spec_version` with the spec's `current_version` at report-build time (verified by submitting a report and reading the column).
22. `POST /specs/{id}/validation/refresh` and `GET /projects/{id}/validation/export` each write exactly one immutable audit-log entry (actor, action, `spec_id`/`project_id`, applied filters, timestamp) via `cross-cutting/F39-audit-log`, secret-redacted; the read-only GET dashboard routes write no audit entries.

## 7. Test plan (TDD) — concrete test cases (unit + integration), key fixtures

Framework: `pytest` + `pytest-asyncio`. Integration tests run against real Postgres via `testcontainers[postgres]` (jsonb + enums). Celery tasks tested with `task_always_eager=True`. F08 evidence supplied via a fake `EvidencePort`; one integration test wires the real F08 reader.

Key fixtures (`packages/spec-engine/tests/conftest.py`, extending F02's): `spec_aggregate_factory` (reused), `validation_report_factory(spec, verdicts, spec_version)`, `evidence_index_factory`, `projection_repo` (real DB), `projector` (`TraceabilityProjector` wired to a fake `SpecSourcePort` + fake `EvidencePort`), and a `matrix_case` parametrization helper that builds a manifest + report + evidence and the expected cells.

Unit — `dashboard.py` (no DB; covers AC #2–#9, #19), table-driven:
- `classify_cell`: matrix over {verdict None / satisfied / unsatisfied-with-test / claim-only} × {report_version == current, report_version < current} asserting exact `CellStatus` (validated/stale/failed/claimed/uncovered).
- `compute_spec_rollup`: cases — all AC validated → `passing`, coverage 1.0; one stale → `stale`; one failed → `failing`; mixed validated+uncovered → `partial`; zero requirements → `requirement_coverage == 0.0`; zero AC → `acceptance_criteria_coverage == 0.0` (AC #7, #8 no divide-by-zero).
- `compute_spec_rollup` requirement coverage: requirement with two AC where one is `claimed` → requirement NOT covered; both validated → covered (AC #7 strict definition).
- `detect_gaps`: manifest with an unreferenced requirement, a claimed AC, a failed AC, a stale AC → exactly four gaps of the right kinds with non-empty `deep_link` (AC #6, #13).
- Determinism: call `compute_spec_rollup`/`detect_gaps` twice, assert equality (AC #19).
- A two-requirement AC produces a cell that appears under both requirement rows (AC #12 logic).

Unit — `build_criterion_links` evidence merge: verdict `test_refs` plus `EvidenceIndex` `pr_numbers`/`diff_refs` for the same criterion are merged (deduped) into the cell.

Integration — projector + DB (covers AC #1, #14–#17, #21):
- `refresh_spec` writes N criterion rows + 1 rollup row, `projection_version` increments (AC #1).
- Idempotency: refresh twice → identical content, version +1 each time (AC #16).
- `reconcile_project` from empty tables equals event-driven content (AC #17).
- Staleness lifecycle: report at v2, bump spec to v3 → cell `stale`; submit report at v3 → cell `validated`, stale count drops (AC #3, #15).
- Event wiring: emit `spec.validation.recorded` (eager) → matrix reflects new verdicts (AC #14).
- F02 write-path: `POST /specs/{id}/validation` stores `spec_version` = `current_version` (AC #21).

Integration — API + DB (covers AC #10–#13, #18, #20):
- Summary totals equal sum of rollups (AC #10).
- `/specs` filters: `stale`, `has_gaps`, `epic_id`, `q`, and cursor pagination boundary at limit 200 (AC #11).
- `/matrix` shape: requirement rows with nested cells, shared AC under two requirements (AC #12).
- `/gaps` equals the matrix-implied gaps (AC #13).
- Export CSV row count == total criteria for the filter; JSON has `summary` + `specs` (AC #18).
- RBAC: `viewer` GET 200, `viewer` POST `/refresh` 403, cross-workspace spec 404 (AC #20).
- Audit: `POST /refresh` and `GET /export` each append one redacted audit-log entry with actor/action/scope; GET dashboard reads append none (AC #22) — asserted against the F39 audit-log store/fake.

Performance smoke (not gating): seed 50 specs × 10 AC, assert `/validation/specs` first page returns < 100ms p95 on the test DB (rollup table scan, no per-spec joins).

Golden/eval hook: reuse F02's `examples/specs/SPEC-001-customer-endpoint/` fixture so F12's `spec.acceptance_criteria_coverage` and F23's `acceptance_criteria_coverage` produce the same number for the same report (cross-feature consistency test).

## 8. Security & policy considerations

- **Read-only, no new authority.** F23 introduces no gates and cannot approve/merge anything; it cannot mutate spec/validation truth. The only mutation is the derived projection, rebuildable from source. This keeps the spec-gating chain (F02/F08) authoritative.
- **RBAC.** All GET routes require `viewer`+ within the workspace; `POST /validation/refresh` requires `member`/`admin` (it consumes worker capacity). The agent-runner role gets read access only and can never trigger reconcile of projects it isn't scoped to.
- **Workspace isolation.** Every projection row carries `workspace_id`; all queries filter by the caller's workspace; cross-workspace `project_id`/`spec_id` returns 404 (not 403) to avoid existence disclosure — consistent with F02.
- **Secret redaction in evidence.** `diff_refs` and `test_refs` (file paths, test node ids) and any rationale snippets pass through the platform secret-redaction filter before persistence to the projection and again on export, satisfying the spec's "secrets stripped from logs, traces, and retrieval results."
- **No raw diff content stored.** The projection holds only references (file path / PR number / test node id), not file contents, bounding blast radius and storage and avoiding duplicating source-of-truth diffs.
- **Export scope.** Export honors the same workspace + filter scope as the live views; there is no "export all workspaces" path. Exports are generated on demand (not cached) so a revoked user cannot re-download stale evidence.
- **Projection is advisory.** Documented and enforced in code review: no security or gate decision may read the projection tables; they exist solely for display/reporting. A contract test asserts F08's merge guard reads F02 reports, not F23 tables.
- **Audit log (non-negotiable).** F23's only two non-read actions are recorded in the immutable audit log (`cross-cutting/F39-audit-log`): the manual `POST /validation/refresh` (actor, `spec_id`) and each `GET /validation/export` (actor, `project_id`, applied filters, format). Read-only dashboard GETs are not audited (consistent with other read surfaces). Entries are secret-redacted and queryable, satisfying the spec's "every … approval — immutable, queryable" requirement for the actions F23 introduces. F23 performs no LLM/model calls, so the BYOK and MCP-read-only non-negotiables do not apply to this slice.

## 9. Effort estimate & risk (S/M/L + key risks)

**Effort: M.** Read-mostly: two projection tables, one additive column, a pure rollup module, a projector with two ports, a thin read API, refresh tasks + event wiring, and a dashboard UI. The hard part is correctness of classification/coverage/staleness and keeping the projection consistent — not volume.

Key risks:
1. **Projection drift (medium).** Missed events leave the dashboard wrong. Mitigate: wholesale per-spec rebuild on every refresh (no incremental patching), idempotent refresh, and a 6h `reconcile_project` beat safety net; an integration test asserts event-driven == reconcile-from-empty (AC #17).
2. **Metric-definition divergence from F12 (medium).** If F23 and the eval harness compute coverage differently, leads see two "truths." This is real: F23 uses a 5-status model (`validated`/`stale`/`failed`/`claimed`/`uncovered`) while F12 reads F02's 3-status traceability, so the two can only be guaranteed equal where no `stale`/`failed` cells exist. Mitigate: put the canonical formula in a shared `forge_spec_engine` helper (the `packages/spec-engine` F12 already depends on) and have F23's `dashboard.py` call it; pin F23 == F12 with a cross-consistency golden test on the all-validated, current-version SPEC-001 fixture (no stale/failed). F23 mirrors F12 (the established v1 definition); refactoring F12's `spec.py` onto the shared helper is a follow-up, not a precondition (F12 cannot import the v2-added `dashboard.py` at its own build time).
3. **Staleness depends on the new `spec_version` column (low/medium).** If F02's write path forgets to populate it, everything looks stale (default) or never stale. Mitigate: AC #21 test on the write path + backfill in the migration default.
4. **Evidence coupling to F08 internals (low).** Reach F08 only through `EvidencePort`; ship a no-op port so F23 builds and tests without F08, then a thin adapter when F08 is present.
5. **Live-update fan-out (low).** Many viewers on a busy project. Reuse F10's SSE infra and push only `{spec_id, projection_version}` (clients refetch the affected row), avoiding large payload broadcasts.

## 10. Key files / paths (exact)

Package (extends F02 `packages/spec-engine`):
- `packages/spec-engine/src/forge_spec_engine/dashboard.py` (pure rollup/gap/staleness)
- `packages/spec-engine/src/forge_spec_engine/dashboard_schemas.py`
- `packages/spec-engine/src/forge_spec_engine/projection.py` (`TraceabilityProjector`, `ProjectionRepository`, ports)
- `packages/spec-engine/src/forge_spec_engine/dashboard_service.py`
- `packages/spec-engine/src/forge_spec_engine/models_dashboard.py` (SQLAlchemy ORM for the two projection tables)
- `packages/spec-engine/tests/test_dashboard.py`, `tests/test_projection.py`, `tests/test_dashboard_service.py`

API / worker / CLI / migration:
- `apps/api/forge_api/routers/spec_dashboard.py`
- `apps/api/forge_api/deps/spec_dashboard.py` (DI wiring; `EvidencePort` adapter over F08)
- `apps/api/alembic/versions/spec_validation_dashboard.py` (revision `spec_validation_dashboard`, chained after F02's `0003_spec_engine` and F08's PR/verification migration; 2 tables + enum + `spec_validation_reports.spec_version` column + backfill)
- `apps/api/forge_cli/validation_dashboard.py`
- `apps/worker/forge_worker/tasks/dashboard_tasks.py`
- `apps/worker/forge_worker/events/dashboard_subscriptions.py` (binds spec/PR/CI events → refresh)
- `apps/api/tests/dashboard/test_routes.py`, `apps/worker/tests/test_dashboard_tasks.py`

Frontend:
- `apps/web/app/projects/[projectId]/validation/page.tsx`
- `apps/web/app/specs/[specId]/validation/page.tsx`
- `apps/web/src/lib/api/validation-dashboard.ts`
- `packages/ui-kit/src/validation/{CoverageSummaryCards,CoverageBar,CellStatusBadge,SpecValidationTable,RequirementTraceMatrix,TraceCell,GapList,StalePill,ValidationFilters,ExportButton}.tsx`

## 11. Research references (relevant links from the spec/research report)

- `docs/FORGE_SPEC.md` — "Spec Gating Rules" ("Approval UI must show requirement-to-diff and requirement-to-test traceability"); "Human Approval System → Approval UI Must Show" (#4 spec traceability, #5 knowledge provenance); "Phased Roadmap → Phase 2 (V2)" item "Spec validation dashboard with requirement traceability"; "Observability and Evaluation → Key Metrics" (spec requirement satisfaction rate) and "Evaluation Harness" (requirement-to-test traceability reports from Spec Engine); "Native Project Board → UX Standards" (sub-100ms interactions, keyboard-first) applied to the dashboard.
- `docs/forge-research-report.md` — "Spec-Driven Development" (Constitution → … → Validate; executable contracts; human review at spec/plan gates) and "Eval-first development" (requirement-to-test traceability as a measured, not estimated, signal).
- `docs/implementation-slices/v1/F02-spec-engine.md` — upstream contracts consumed: `SpecManifest`, `ValidationReport`/`CriterionVerdict`, `build_traceability_matrix`, `GET /specs/{id}/traceability`, and the explicit deferral of "Spec validation dashboard with full requirement traceability visualization — v2".
- `docs/implementation-slices/v1/F08-plan-execute-verify-pr-approval.md` — `TraceabilityRow` (`criterion_id`/`satisfied`/`evidence_files`/`evidence_tests`/`agent_rationale`), `SpecTraceabilityComposer`, and the `pull_request` table whose `traceability` jsonb holds per-criterion PR/test/diff evidence (F23's `EvidencePort` source).
- `docs/implementation-slices/cross-cutting/F39-audit-log.md` — the immutable, queryable audit log F23 writes to on `/validation/refresh` and `/validation/export`.
- `docs/implementation-slices/v1/F12-eval-harness.md` — `spec.requirement_coverage` / `spec.acceptance_criteria_coverage` metric definitions F23 must stay numerically consistent with.
- TanStack Table (dashboard grid): https://tanstack.com/table · TanStack Query (data + live cache): https://tanstack.com/query · shadcn/ui (components): https://ui.shadcn.com/ · Linear (UX reference for the keyboard-first dashboard): https://linear.app/

## 12. Out of scope / future

- **Workspace-wide cross-project rollup** and org dashboards — F23 scopes to a single project (plus per-spec drill-down); a workspace aggregate is a later add over the same projection.
- **Historical trend charts** (coverage over time, burndown of gaps) — would require a time-series of rollup snapshots; F23 stores only the current projection. Future: append `traceability_rollup_history`.
- **Changing the merge gate.** F23 visualizes validation; the authoritative `assert_merge_allowed` stays in F02/F08. F23 never blocks anything.
- **The real LLM-backed validation** (mapping tests↔criteria via the spec-analyst/agent) — owned by F06/F02; F23 consumes whatever verdicts exist.
- **Knowledge-provenance traceability** (which retrieved chunks informed a criterion) — that is the knowledge slice's surface; F23 links only requirement→AC→test→diff→PR.
- **Multi-repo evidence aggregation** for a single criterion across repos — depends on v2 multi-repo execution; the `EvidencePort` shape allows it later without schema change.
- **Editing/annotating gaps inline** (assigning a gap as a task) — future; for now gaps deep-link to the existing create-task flow on the board (F01).
