# F01 — Native Project Board

> Phase: v1 · Spec module(s): Native Project Board (entity hierarchy, board features, UX standards, command palette, views), Core Data Model, Task Schema · Status target: A keyboard-first, Linear-quality board where a user can create Projects/Epics/Tasks/SubTasks, move them through per-project custom statuses, view them in List / Board / Roadmap / Backlog / My Tasks, filter & save filters, manage blocks/blocked-by dependencies with cycle detection, run bulk actions, see a unified per-entity timeline, and drive everything from a Cmd+K palette — with optimistic updates and live multi-client sync. "Done" = all acceptance criteria in §6 pass in CI (board-core unit suite, API integration suite, web component/e2e suite) and the board renders against a seeded demo workspace.

---

## 1. Intent — what & why

Forge's board is the **task-as-control-plane** surface (borrowed from Symphony) and the human entry point for every workflow. It must compete with Linear on speed and ergonomics, not be a generic admin CRUD panel. This slice builds the **native board core**: the entity hierarchy, the data model, the CRUD + query APIs, the domain logic (status policy, dependency graph, ranking, filtering), and the Next.js views/command-palette/keyboard layer.

Why it is foundational: every other v1 feature (Spec Engine, GitHub App, Workflow Engine, Approvals, Run Trace) reads from and writes to board entities. Tasks carry `repo_targets`, `skill_profile`, `spec_id`, `execution_mode`, and `requires_approval` — the contract the agent runtime consumes. The board's unified timeline is where run events, PRs, approvals, and spec decisions surface to humans. Shipping a fast, correct board first unblocks the rest of v1.

Scope discipline (per phased roadmap): this slice ships **board CRUD + views + command palette + dependencies + saved filters + bulk actions + unified timeline + live sync + SLA-breach detection**. It explicitly defers the **automation rule engine** and **sprint velocity dashboards** to v2 (the `sprints`/`milestones` tables are created here, but only basic create/assign, not velocity analytics). External PM adapters are v2.

---

## 2. User-facing behavior / journeys

1. **Create a project.** User opens Cmd+K → "Create project" → enters name + key (e.g. `CORE`). A default status set (`Backlog / Todo / In Progress / In Review / Done / Canceled`) is seeded. User lands on the project's Board view.
2. **Create work fast.** From any view, `C` opens the create-task dialog (or `Cmd+K → Create task`). Title, then optional inline assignment of status/priority/assignee/estimate/labels/epic. On submit the row appears **instantly** (optimistic) with a temporary key, reconciled to the server key (`CORE-123`) on response. ESC anywhere closes overlays.
3. **Move work through statuses.** On the Board (kanban) view, drag a card between columns, or select a task and press `S` to open the status picker. The move is optimistic; if a per-project transition policy forbids it, the server returns 409 and the card snaps back with a toast.
4. **Switch views without reload.** Tabs (or `G then L/B/R/K/M`) switch List / Board / Roadmap / Backlog / My Tasks client-side; the URL updates (`?view=board`). No full-page reload; scroll/selection state for the project is preserved.
5. **Filter and save.** `F` opens the filter bar; user adds conditions (status in [In Progress], priority ≥ high, assignee = me, label = backend). Results update sub-100ms. `Cmd+S` saves the current view+filter+sort+group as a named saved filter, scoped to self or team.
6. **Manage dependencies.** In a task's detail panel, "Add blocked-by" / "Add blocks" opens a task search; selecting a task that would form a cycle is rejected inline with the offending path shown.
7. **Bulk edit.** `X` toggles multi-select; `Shift+click` range-selects; a bulk bar offers status / assignee / priority / label / sprint / archive. One request mutates all selected; all clients update live.
8. **Unified timeline.** The task detail panel shows one chronological feed: comments, status changes, assignments, dependency edits, and (when other slices land) run events, PR events, approvals, and spec decisions. User can comment (Markdown) with `@mention`.
9. **Live collaboration.** A second user/agent changing a task is reflected in the first user's open views within ~1s via SSE, with no manual refresh.
10. **SLA awareness.** Tasks with an `sla_due_at` show a countdown chip; when breached, a `sla_breached` activity event is emitted and the chip turns red.

---

## 3. Vertical slice

### 3.1 Data model (tables/columns/migrations touched)

All tables live in a single Alembic migration `apps/api/alembic/versions/0002_board_core.py` (depends on baseline `0001_foundations` from the foundation substrate slice — see §5 — which creates `workspaces`, `users`, `teams` base, and the shared `Base`/naming convention). SQLAlchemy 2.x ORM models live in `packages/board-core/src/board_core/models/`. Every board table carries `workspace_id` (denormalized for tenant isolation + fast filtering) and `created_at`/`updated_at` (via `TimestampMixin` from `packages/db` (`forge_db`), provided by the foundation slice).

Naming convention (Alembic `target_metadata.naming_convention`): `ix_%(table_name)s_%(column_0_name)s`, `uq_`, `fk_`, `ck_`, `pk_`. All PKs are `uuid` (`gen_random_uuid()`); all `*_id` FKs are `uuid` with `ON DELETE` rules noted.

| Table | Key columns | Notes |
|---|---|---|
| `projects` | `id`, `workspace_id` (FK→workspaces, CASCADE), `key` (citext, unique per workspace), `name`, `description`, `archived_at` nullable, `created_by` (FK→users) | `key` is the task-prefix (e.g. `CORE`). `uq_projects_workspace_id_key`. |
| `project_counters` | `project_id` (PK, FK→projects CASCADE), `next_task_number` int default 1, `next_epic_number` int default 1 | Atomic per-project sequence; bumped with `UPDATE ... RETURNING` under row lock. |
| `task_statuses` | `id`, `workspace_id`, `project_id` (FK CASCADE), `name`, `category` enum(StatusCategory), `color` (hex), `position` (text lexorank), `is_default` bool | Custom per-project statuses. `category` maps to workflow policy. `uq_task_statuses_project_id_name`. |
| `task_status_transitions` | `id`, `project_id` (FK CASCADE), `from_status_id` nullable (FK→task_statuses), `to_status_id` (FK→task_statuses) | Optional allow-list. If a project has zero rows, all transitions are permitted. |
| `labels` | `id`, `workspace_id`, `project_id` (FK CASCADE), `name`, `color` | `uq_labels_project_id_name`. |
| `epics` | `id`, `workspace_id`, `project_id` (FK CASCADE), `number` int, `key` (computed `CORE-E<number>`), `title`, `description`, `status_id` (FK→task_statuses SET NULL), `owner_id` (FK→users SET NULL), `spec_id` uuid nullable (**soft** ref to SpecDocument), `milestone_id` (FK→milestones SET NULL), `position` (lexorank), `version` int default 1, `archived_at` nullable | `uq_epics_project_id_number`. |
| `tasks` | `id`, `workspace_id`, `project_id` (FK CASCADE), `epic_id` (FK→epics SET NULL), `parent_task_id` (FK→tasks CASCADE, self-ref for subtasks), `number` int, `key` (computed `CORE-<number>`), `kind` enum(TaskKind), `title`, `description`, `status_id` (FK→task_statuses RESTRICT), `priority` enum(Priority) default `none`, `estimate` int nullable, `assignee_id` (FK→users SET NULL), `team_id` (FK→teams SET NULL), `sprint_id` (FK→sprints SET NULL), `milestone_id` (FK→milestones SET NULL), `due_date` date nullable, `sla_due_at` timestamptz nullable, `sla_breached_at` timestamptz nullable, `spec_id` uuid nullable (**soft**), `skill_profile` text nullable (**soft** ref to SkillProfile name), `execution_mode` enum(ExecutionMode) default `single_agent`, `repo_targets` jsonb default `[]`, `requires_approval` jsonb default `{"spec":false,"plan":false,"pr":true,"deploy":true}`, `position` (lexorank), `version` int default 1, `archived_at` nullable, `created_by` (FK→users) | `uq_tasks_project_id_number`. Indexes: `ix_tasks_workspace_id`, `ix_tasks_project_id_status_id`, `ix_tasks_assignee_id`, `ix_tasks_sprint_id`, partial `ix_tasks_open (status category != completed/canceled)`. `repo_targets` shape mirrors Task Schema `repo_targets[]`. |
| `task_labels` | `task_id` (FK CASCADE), `label_id` (FK CASCADE), PK(`task_id`,`label_id`) | M2M. |
| `task_dependencies` | `id`, `project_id` (FK CASCADE), `blocking_task_id` (FK→tasks CASCADE), `blocked_task_id` (FK→tasks CASCADE), `type` enum(DependencyType default `blocks`), `created_by` | Edge = "blocking blocks blocked". `uq_task_dependencies_blocking_blocked`, `ck` `blocking_task_id <> blocked_task_id`. |
| `sprints` | `id`, `workspace_id`, `project_id` (FK CASCADE), `name`, `goal`, `start_date`, `end_date`, `state` enum(planned/active/completed) | Velocity analytics deferred to v2. |
| `milestones` | `id`, `workspace_id`, `project_id` (FK CASCADE), `name`, `description`, `target_date`, `state` enum(open/closed) | Roadmap anchors. |
| `incidents` | `id`, `workspace_id`, `project_id` (FK CASCADE), `number`, `key` (`CORE-INC<number>`), `title`, `severity` enum(sev1..sev4), `status_id` (FK→task_statuses), `version` int, `archived_at` | Entity exists for hierarchy completeness + board display. Incident **workflow** is v2. |
| `comments` | `id`, `workspace_id`, `entity_type` enum(task/epic/incident), `entity_id` uuid, `author_id` (FK→users), `author_kind` enum(user/agent/system) default user, `body_md` text, `edited_at` nullable, `deleted_at` nullable | Editable/soft-deletable. Surfaced in timeline. Index `ix_comments_entity (entity_type, entity_id, created_at)`. |
| `activity_events` | `id`, `workspace_id`, `entity_type` enum(task/epic/incident), `entity_id` uuid, `event_type` enum (see §4), `actor_kind` enum(user/agent/system), `actor_id` uuid nullable, `payload` jsonb, `created_at` | **Append-only** audit + timeline feed. No update/delete. Index `ix_activity_events_entity (entity_type, entity_id, created_at)`, `ix_activity_events_workspace_created`. |
| `saved_filters` | `id`, `workspace_id`, `project_id` nullable (FK CASCADE; null = workspace-wide), `owner_id` (FK→users), `scope` enum(user/team), `team_id` nullable (FK→teams), `name`, `view_type` enum(ViewType), `filter_json` jsonb, `sort_json` jsonb, `group_by` text nullable, `created_at`, `updated_at` | `uq_saved_filters_owner_name_project`. |

Optimistic concurrency: `tasks`, `epics`, `incidents` carry an integer `version`, incremented on every mutating write inside the same transaction; conflicting writes return HTTP 409 (see §4).

**Task Schema coverage.** The board persists the human-facing + execution-routing subset of the spec's Task Schema: `repo_targets`, `skill_profile`, `spec_id`, `execution_mode`, `requires_approval`, plus priority/estimate/labels/team/assignee/sla/due-date. The remaining Task Schema fields are owned and resolved by other slices at execution time and are deliberately **not** board columns: `instructions_profile`, `allowed_actions[]`, `restricted_actions[]`, `knowledge_scope`, and `subagent_policy` are resolved from repo policy (`v1/F04-repo-policy`) + skill profile (`v1/F11-skill-profiles`) by the agent runtime (`v1/F06-single-execution-agent`); `acceptance_criteria[]` live in the spec and are referenced via `spec_id` (`v1/F02-spec-engine`, which back-links each generated task per criterion); `handoff_rules` are owned by the workflow engine (`v1/F07-feature-workflow-fsm`).

### 3.2 Backend (FastAPI routes + services/packages)

**App:** `apps/api` (module `forge_api`). **Domain package:** `packages/board-core` (module `board_core`: ORM models + pure domain logic + service layer; depends on `packages/db` (`forge_db`) + `pydantic`, must NOT import `fastapi`). All routes are mounted under `/api/v1`, require an authenticated `Principal` (resolved by `cross-cutting/F37-auth-secrets-byok`'s `forge_api/deps/auth.py::get_principal`), and resolve `workspace_id` from the project / entity path; every query is filtered by `workspace_id`.

Routers (`apps/api/forge_api/routers/board/`):

- `projects.py` — `POST /projects`, `GET /projects`, `GET /projects/{project_id}`, `PATCH /projects/{project_id}`, `POST /projects/{project_id}/archive`.
- `statuses.py` — `GET|POST /projects/{project_id}/statuses`, `PATCH /statuses/{status_id}`, `POST /projects/{project_id}/statuses/reorder`, `DELETE /statuses/{status_id}?reassign_to={status_id}`, `GET|PUT /projects/{project_id}/status-transitions`.
- `epics.py` — CRUD `…/epics`, `PATCH /epics/{id}`, `POST /epics/{id}/archive`.
- `tasks.py` — `POST /projects/{project_id}/tasks`, `GET /tasks/{id}`, `PATCH /tasks/{id}`, `POST /tasks/{id}/archive`, `POST /tasks/{id}/move` (status + rank), `POST /projects/{project_id}/tasks/query` (filter/sort/group → page).
- `subtasks.py` — `POST /tasks/{id}/subtasks` (creates task with `parent_task_id`), `GET /tasks/{id}/subtasks`.
- `dependencies.py` — `POST /tasks/{id}/dependencies` (cycle-checked), `DELETE /dependencies/{id}`, `GET /projects/{project_id}/dependency-graph`.
- `labels.py`, `sprints.py`, `milestones.py` — CRUD + assignment.
- `comments.py` — `GET|POST /{entity_type}/{entity_id}/comments`, `PATCH|DELETE /comments/{id}`.
- `timeline.py` — `GET /{entity_type}/{entity_id}/timeline` (merged comments + activity_events, cursor-paginated).
- `events_ingest.py` — `POST /internal/activity-events` (service-to-service; used by Workflow/GitHub/Approval slices to push `run_event`/`pr_event`/`approval_event`/`spec_decision` into a task timeline). Requires `agent-runner`/service scope.
- `filters.py` — CRUD `…/saved-filters`, `GET /saved-filters?project_id=&scope=`.
- `bulk.py` — `POST /tasks/bulk` (apply one patch to many ids).
- `search.py` — `GET /search?q=&project_id=&types=task,epic,project` (command-palette backing).
- `stream.py` — `GET /projects/{project_id}/stream` (SSE; emits entity-change + activity envelopes).

Service layer (`packages/board-core/src/board_core/services/`): `projects.py`, `statuses.py`, `epics.py`, `tasks.py`, `dependencies.py`, `timeline.py`, `filters.py`, `search.py`. Services take an injected `AsyncSession` + `Principal`, never raw request objects. Cross-cutting helpers: `keys.py` (key formatting + counter bump), `ranking.py` (lexorank), `enums.py`, `filters.py` (predicate→SQLAlchemy), `dependencies.py` (graph + cycle detection), `events.py` (activity-event recording + Redis publish), `errors.py` (`CycleError`, `StatusTransitionError`, `VersionConflictError`, `PolicyError`).

Live sync: every mutating service call records an `activity_events` row (where meaningful) and publishes a JSON envelope to Redis channel `board:{project_id}` via `board_core.events.publish`. `stream.py` SSE handler subscribes to that channel (Redis pub/sub) so events fan out across all `api` workers/replicas.

### 3.3 Worker / agent runtime (Celery tasks, LangGraph, if any)

Celery (broker = Redis, app `forge_worker.app`, from the foundation slice) — board owns **one** periodic task registered on Celery Beat. The task lives in `apps/worker/forge_worker/tasks/board_tasks.py`; the Beat schedule entry is added in `apps/worker/forge_worker/beat.py`. No LangGraph.

- `board.scan_sla_breaches` (every 60s): selects open tasks where `sla_due_at < now()` and `sla_breached_at IS NULL`; sets `sla_breached_at`, writes `activity_events(event_type=sla_breached)`, publishes to `board:{project_id}`, and enqueues a notification handoff on the `notifications` queue (consumed by `v1/F16-slack-notifications` — out of scope here beyond emitting the event). Idempotent: the `sla_breached_at IS NULL` guard means a re-run never emits a second `sla_breached` event for an already-breached task (AC14).

Milestone progress for the Roadmap view (per open milestone: completed vs total task counts) is **computed-on-read** in the Roadmap query path in v1 — no cache table and no periodic recompute task. A materialized `milestone_progress` cache + recompute task is deferred to v2 (§12) and introduced only if a perf regression is observed against the `seed_demo_board` fixture.

No agent runtime / LangGraph involvement in this slice; the board is the data substrate the agent runtime (other slices) reads.

### 3.4 Frontend / UI (Next.js routes/components, if any)

**App:** `apps/web` (Next.js App Router, TypeScript, Tailwind, shadcn/ui, TanStack Query + TanStack Table). Shared primitives go in `packages/ui-kit`.

Routes (App Router):
- `app/[workspace]/projects/[projectKey]/page.tsx` — board shell; view chosen via `?view=list|board|roadmap|backlog` (default `board`).
- `app/[workspace]/my-tasks/page.tsx` — cross-project My Tasks (assignee = current user).
- `app/[workspace]/projects/[projectKey]/[taskKey]/page.tsx` — deep-linkable task detail (also opens as a side panel over a view via intercepting route `@panel`).

Key components (`apps/web/components/board/`): `CommandPalette.tsx` (Cmd+K, cmdk lib), `ViewSwitcher.tsx`, `FilterBar.tsx`, `ListView.tsx` (TanStack Table, virtualized rows), `BoardView.tsx` + `BoardColumn.tsx` + `TaskCard.tsx` (dnd-kit drag), `RoadmapView.tsx` (milestone/epic timeline), `BacklogView.tsx`, `TaskDetailPanel.tsx`, `Timeline.tsx`, `CommentComposer.tsx`, `DependencyEditor.tsx`, `BulkActionBar.tsx`, `CreateTaskDialog.tsx`, `StatusPicker.tsx`/`PriorityPicker.tsx`/`AssigneePicker.tsx`/`LabelPicker.tsx`.

State & data: `apps/web/lib/board/api.ts` (typed client over the API contracts in §4), `apps/web/lib/board/queries.ts` (TanStack Query hooks), `apps/web/lib/board/mutations.ts` (optimistic mutations with `onMutate` cache patch + `onError` rollback + `onSettled` invalidate). Live updates: `apps/web/lib/board/useBoardStream.ts` subscribes to the SSE endpoint and patches/invalidates the query cache by entity id. Keyboard: `apps/web/lib/board/keymap.ts` + a `useHotkeys` provider implementing the shortcut table in §4.

UX standard implementation:
- **Optimistic**: all single-entity mutations patch the cache before the request; rollback the exact prior snapshot on error and toast.
- **No full-page reload**: view switching is client-side route state; data fetched via TanStack Query.
- **Sub-100ms**: list virtualization, memoized rows, derived client-side filtering for already-loaded pages (server filter for the authoritative query).
- **Empty states**: each view has an `EmptyState` component with a primary CTA (e.g. "Create your first task — press C").

### 3.5 Infra / deploy (compose, helm, caddy, if any)

No new compose services. Re-uses `db` (Postgres+pgvector), `redis` (Celery broker + board pub/sub), `api`, `worker`, `web`, `caddy` from the foundation substrate + `v1/F14-docker-compose-selfhost`. Requirements:
- Caddy already proxies `/api/*` → `api:8000` and `/` → `web:3000`; SSE endpoint needs `flush_interval -1` (disable response buffering) for `/api/v1/projects/*/stream` — add a matcher block to `deploy/caddy/Caddyfile`.
- `worker` must run Celery Beat (add `--beat` or a dedicated `beat` command) so `board.scan_sla_breaches` fires; document in `deploy/docker-compose.yml`.
- Migration `0002_board_core` runs via existing `forge-cli db migrate`.

---

## 4. Public interfaces / contracts (exact signatures, Pydantic models, Protocols, YAML schemas)

**board-core enums** (`packages/board-core/src/board_core/enums.py`):
```python
from enum import StrEnum

class StatusCategory(StrEnum):
    BACKLOG = "backlog"; UNSTARTED = "unstarted"; STARTED = "started"
    COMPLETED = "completed"; CANCELED = "canceled"

class TaskKind(StrEnum):
    FEATURE = "feature"; BUG = "bug"; CHORE = "chore"; SPIKE = "spike"
    INCIDENT = "incident"; CHANGE_REQUEST = "change_request"; DOC = "doc"

class Priority(StrEnum):
    NONE = "none"; LOW = "low"; MEDIUM = "medium"; HIGH = "high"; URGENT = "urgent"

class ExecutionMode(StrEnum):
    SINGLE_AGENT = "single_agent"; SUPERVISED_MULTI_AGENT = "supervised_multi_agent"

class DependencyType(StrEnum):
    BLOCKS = "blocks"

class ViewType(StrEnum):
    LIST = "list"; BOARD = "board"; ROADMAP = "roadmap"
    BACKLOG = "backlog"; MY_TASKS = "my_tasks"

class ActivityEventType(StrEnum):
    CREATED = "created"; STATUS_CHANGED = "status_changed"; ASSIGNED = "assigned"
    PRIORITY_CHANGED = "priority_changed"; FIELD_CHANGED = "field_changed"
    LABEL_ADDED = "label_added"; LABEL_REMOVED = "label_removed"
    DEPENDENCY_ADDED = "dependency_added"; DEPENDENCY_REMOVED = "dependency_removed"
    ARCHIVED = "archived"; SLA_BREACHED = "sla_breached"
    # populated by other slices via /internal/activity-events:
    RUN_EVENT = "run_event"; PR_EVENT = "pr_event"
    APPROVAL_EVENT = "approval_event"; SPEC_DECISION = "spec_decision"
```

**Ranking** (`board_core/ranking.py`):
```python
def rank_between(prev: str | None, nxt: str | None) -> str:
    """Lexicographic (base-62) rank strictly between prev and nxt.
    rank_between(None, None) -> midpoint seed. Raises ValueError if prev >= nxt."""
```

**Keys** (`board_core/keys.py`):
```python
def task_key(project_key: str, number: int) -> str:      # "CORE-123"
def epic_key(project_key: str, number: int) -> str:      # "CORE-E12"
def incident_key(project_key: str, number: int) -> str:  # "CORE-INC4"

async def next_number(session, project_id: UUID, kind: Literal["task", "epic"]) -> int:
    """Atomically bump and return the per-project counter (row-locked UPDATE ... RETURNING)."""
```

**Dependency graph** (`board_core/dependencies.py`):
```python
class CycleError(Exception):
    def __init__(self, path: list[str]) -> None: ...   # path = task ids forming the cycle

class DependencyGraph:
    def __init__(self, edges: Iterable[tuple[str, str]]) -> None: ...   # (blocking_id, blocked_id)
    def would_create_cycle(self, blocking_id: str, blocked_id: str) -> list[str] | None: ...
    def add_edge(self, blocking_id: str, blocked_id: str) -> None: ...  # raises CycleError
    def blockers_of(self, task_id: str) -> set[str]: ...
    def blocked_by(self, task_id: str) -> set[str]: ...
    def topological_order(self) -> list[str]: ...
```

**Status policy** (`board_core/statuses.py`):
```python
class StatusTransitionError(Exception): ...

class StatusTransitionValidator:
    def __init__(self, transitions: list[tuple[str | None, str]] | None) -> None:
        """transitions == None or [] -> all transitions allowed."""
    def is_allowed(self, from_status_id: str | None, to_status_id: str) -> bool: ...
    def assert_allowed(self, from_status_id: str | None, to_status_id: str) -> None: ...
```

**Filter predicate** (`board_core/filters.py`):
```python
class FilterOp(StrEnum):
    EQ="eq"; NE="ne"; IN="in"; NOT_IN="not_in"; LT="lt"; LTE="lte"
    GT="gt"; GTE="gte"; CONTAINS="contains"; IS_NULL="is_null"

class FilterCondition(BaseModel):
    field: str          # must be in FILTERABLE_FIELDS
    op: FilterOp
    value: Any = None

class FilterPredicate(BaseModel):
    match: Literal["all", "any"] = "all"
    conditions: list[FilterCondition] = Field(default_factory=list)
    groups: list["FilterPredicate"] = Field(default_factory=list)

class SortSpec(BaseModel):
    field: str
    direction: Literal["asc", "desc"] = "asc"

# Whitelisted filter/sort/group fields -> ORM column resolvers.
FILTERABLE_FIELDS: dict[str, ...] = {
    "status_id", "priority", "assignee_id", "kind", "epic_id", "sprint_id",
    "milestone_id", "team_id", "label", "estimate", "due_date", "sla_due_at",
    "created_at", "updated_at", "archived",  # archived is bool-ish
}

def build_filter(pred: FilterPredicate, *, special_token_user_id: UUID | None) -> ColumnElement[bool]:
    """Translate predicate to a SQLAlchemy boolean clause against Task.
    Resolves the special value '@me' against special_token_user_id.
    Raises ValueError on unknown field or invalid op/value pairing."""
```

**Representative API Pydantic v2 models** (`apps/api/forge_api/schemas/board.py`):
```python
class TaskRepoTarget(BaseModel):
    repo: str
    branch_strategy: Literal["task_branch", "shared_branch"] = "task_branch"
    branch_prefix: str | None = None
    base_branch: str = "main"
    worktree: bool = True

class RequiresApproval(BaseModel):
    spec: bool = False; plan: bool = False; pr: bool = True; deploy: bool = True

class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=512)
    kind: TaskKind = TaskKind.FEATURE
    description: str | None = None
    status_id: UUID | None = None          # defaults to project default status
    priority: Priority = Priority.NONE
    estimate: int | None = Field(default=None, ge=0)
    assignee_id: UUID | None = None
    epic_id: UUID | None = None
    parent_task_id: UUID | None = None
    sprint_id: UUID | None = None
    milestone_id: UUID | None = None
    team_id: UUID | None = None
    label_ids: list[UUID] = []
    due_date: date | None = None
    sla_due_at: datetime | None = None
    spec_id: UUID | None = None
    skill_profile: str | None = None
    execution_mode: ExecutionMode = ExecutionMode.SINGLE_AGENT
    repo_targets: list[TaskRepoTarget] = []
    requires_approval: RequiresApproval = RequiresApproval()
    before_task_id: UUID | None = None     # rank placement hint
    after_task_id: UUID | None = None

class TaskUpdate(BaseModel):           # all optional; partial PATCH
    title: str | None = None
    # ...same fields as TaskCreate, all Optional...
    version: int                       # required for optimistic concurrency

class TaskMove(BaseModel):
    status_id: UUID
    before_task_id: UUID | None = None
    after_task_id: UUID | None = None
    version: int

class TaskRead(BaseModel):
    id: UUID; key: str; project_id: UUID; number: int
    kind: TaskKind; title: str; description: str | None
    status_id: UUID; priority: Priority; estimate: int | None
    assignee_id: UUID | None; epic_id: UUID | None; parent_task_id: UUID | None
    sprint_id: UUID | None; milestone_id: UUID | None; team_id: UUID | None
    label_ids: list[UUID]; due_date: date | None
    sla_due_at: datetime | None; sla_breached_at: datetime | None
    spec_id: UUID | None; skill_profile: str | None
    execution_mode: ExecutionMode; repo_targets: list[TaskRepoTarget]
    requires_approval: RequiresApproval; position: str; version: int
    archived_at: datetime | None; created_at: datetime; updated_at: datetime
    model_config = ConfigDict(from_attributes=True)

class TaskQuery(BaseModel):
    filter: FilterPredicate = FilterPredicate()
    sort: list[SortSpec] = [SortSpec(field="position", direction="asc")]
    group_by: str | None = None
    include_archived: bool = False
    cursor: str | None = None
    limit: int = Field(default=100, le=250)

class Page[T](BaseModel):
    items: list[T]
    next_cursor: str | None = None

class DependencyCreate(BaseModel):
    blocking_task_id: UUID            # this task blocks ->
    blocked_task_id: UUID             # this task is blocked
    type: DependencyType = DependencyType.BLOCKS

class BulkTaskAction(BaseModel):
    task_ids: list[UUID] = Field(min_length=1, max_length=500)
    set_status_id: UUID | None = None
    set_assignee_id: UUID | None = None
    set_priority: Priority | None = None
    add_label_ids: list[UUID] = []
    remove_label_ids: list[UUID] = []
    set_sprint_id: UUID | None = None
    archive: bool | None = None

class CommentCreate(BaseModel):
    body_md: str = Field(min_length=1, max_length=20000)

class TimelineItem(BaseModel):
    kind: Literal["comment", "activity"]
    id: UUID; created_at: datetime
    actor_kind: Literal["user", "agent", "system"]; actor_id: UUID | None
    comment: CommentRead | None = None
    event_type: ActivityEventType | None = None
    payload: dict | None = None

class SavedFilterUpsert(BaseModel):
    name: str; view_type: ViewType; scope: Literal["user", "team"] = "user"
    team_id: UUID | None = None; project_id: UUID | None = None
    filter: FilterPredicate; sort: list[SortSpec] = []; group_by: str | None = None
```

**Error contract (all routers):** `409 Conflict` body `{"error":"version_conflict","current_version":<int>}` for stale `version`; `409` `{"error":"cycle","path":[...]}` for dependency cycles; `409` `{"error":"status_transition_forbidden","from":...,"to":...}`; `403` `{"error":"forbidden"}` for RBAC; `404` for cross-workspace access (never 403, to avoid existence leak). Validation errors use FastAPI's `422`.

**SSE envelope** (`GET /projects/{project_id}/stream`, `text/event-stream`):
```
event: task.updated
data: {"entity_type":"task","entity_id":"...","version":7,"changed":["status_id","position"],"actor_id":"...","ts":"..."}

event: activity.created
data: {"entity_type":"task","entity_id":"...","event_type":"status_changed","payload":{...},"ts":"..."}
```

**External PM adapter** — the `PMAdapter` Protocol from the spec is **defined in v2** (`packages/integration-sdk`); this slice only guarantees the `board-core` domain model is serializable to a stable `ForgeTask` dict so the future adapter can map it. See §12.

---

## 5. Dependencies — features/slices that must exist first

Referenced by `<phase>/<id>-<slug>` path.

Hard (must land before/with this slice):
- **Foundation substrate** (REQUIRED) — monorepo + `uv` workspaces, `apps/api` FastAPI skeleton (`forge_api`), `apps/worker` Celery app (`forge_worker.app`) + Beat, `apps/web` Next.js shell, `packages/db` (`forge_db`: shared `Base`, `TimestampMixin`, async session, Alembic env + naming convention, baseline `0001_foundations` creating `workspaces`/`users`/`teams`), Postgres+Redis in compose, `forge-cli db migrate`. This cross-cutting Phase-0 substrate has no dedicated numbered feature file yet; sibling slices refer to it variously as `cross-cutting/C01-monorepo-and-api-foundations` / `cross-cutting/F00-platform-foundation` / `v1/F00-foundation-substrate` — reconcile to the final foundation slug when it lands. Its container/compose packaging is owned by `v1/F14-docker-compose-selfhost`.
- `cross-cutting/F37-auth-secrets-byok` (REQUIRED) — `workspaces`/`users`/`teams` tables; the `Principal` (carrying `workspace_id`, `user_id`, `role ∈ {admin, member, viewer, agent-runner}`) resolved by `forge_api/deps/auth.py::get_principal`; the `require_role(min_role)` RBAC dependency; the internal/service-token path used by `/internal/activity-events`; and the Redis rate limiter. (Sibling slices also call this `C02-auth-and-rbac` / `F15-auth-secrets-rbac`.)
- `cross-cutting/F39-audit-log` (REQUIRED, lands with) — the canonical `AuditEvent` DTO + `AuditSink` Protocol (in `packages/contracts`) and the reusable `attach_immutability_trigger(table)` helper that `activity_events` adopts for DB-level append-only enforcement; board agent-runner / internal-ingest writes also fan out a redacted `AuditEvent` with a `detail_ref` back to the originating `activity_events` row (see §8).

Soft (nullable references / event-ingest consumers — board ships with these columns/endpoints but they are exercised when the producing slice lands):
- `v1/F02-spec-engine` — `tasks.spec_id` / `epics.spec_id` resolution; consumes `POST /projects/{project_id}/tasks` + `POST /internal/activity-events` (`event_type=spec_decision`) through its `BoardPort`.
- `v1/F03-github-app` — `repo_targets` validation + `pr_event` timeline events via `/internal/activity-events`.
- `v1/F11-skill-profiles` — `tasks.skill_profile` validation against the skill registry.
- `v1/F07-feature-workflow-fsm` + `v1/F08-plan-execute-verify-pr-approval` + `v1/F10-run-trace-viewer` — `run_event` / `approval_event` ingest via `/internal/activity-events`; F07/F08 own the spec-gate + human-approval-before-merge enforcement the board's `requires_approval` flags feed.
- `cross-cutting/F36-human-approval-system` — emits `approval_event` timeline items the board surfaces in the unified timeline (the board itself gates no merges).
- `v1/F16-slack-notifications` — consumes the `sla_breached` and approval activity events.

---

## 6. Acceptance criteria (numbered, testable)

1. **Project + default statuses**: `POST /projects` with `{name, key:"CORE"}` returns 201 and seeds exactly 6 statuses (`Backlog/Todo/In Progress/In Review/Done/Canceled`) with categories `backlog/unstarted/started/started/completed/canceled`, one `is_default=true`.
2. **Sequential keys**: Creating three tasks in `CORE` returns keys `CORE-1`, `CORE-2`, `CORE-3`; numbers are gap-free and unique per project even under concurrent creation (10 parallel creates → 10 distinct numbers).
3. **SubTasks**: `POST /tasks/{id}/subtasks` creates a task with `parent_task_id` set; `GET /tasks/{id}/subtasks` returns it; deleting/archiving a parent cascades archive to subtasks.
4. **Custom status + transition policy**: With a `PUT status-transitions` allow-list excluding `Done → In Progress`, a `POST /tasks/{id}/move` to that transition returns 409 `status_transition_forbidden`; an allowed transition returns 200 and writes a `status_changed` activity event.
5. **Optimistic concurrency**: Two `PATCH /tasks/{id}` with the same stale `version` → first 200, second 409 `version_conflict` with `current_version`.
6. **Dependency cycle detection**: Given `A blocks B` and `B blocks C`, `POST` `C blocks A` returns 409 `cycle` with `path=[A,B,C]` (or rotation); no edge is persisted.
7. **Dependency happy path**: `A blocks B` persists, `GET /projects/{id}/dependency-graph` lists the edge, and `B`'s detail shows `blocked_by=[A]`.
8. **Filter query**: `POST /tasks/query` with predicate `priority in [high,urgent] AND assignee_id = @me` returns only matching tasks for the calling user; `group_by=status_id` returns items grouped; cursor pagination returns stable, non-overlapping pages.
9. **Saved filters**: A saved filter with `scope=user` is returned to its owner and not to other users; `scope=team` is returned to all members of `team_id`.
10. **Bulk action atomicity**: `POST /tasks/bulk` setting status on 50 tasks updates all 50 in one transaction and emits 50 `status_changed` events; if any single task is cross-workspace/not-found, the whole request 404s and nothing is mutated.
11. **Unified timeline ordering**: `GET /tasks/{id}/timeline` returns comments + activity events merged in `created_at` order; a `run_event` posted via `/internal/activity-events` appears in the same feed.
12. **RBAC**: A `viewer` principal gets 403 on any mutating board route and 200 on reads; `agent-runner` may write tasks/comments/activity but cannot delete projects; cross-workspace access returns 404.
13. **Live sync (SSE)**: With an open SSE stream on project P, a status change to a task in P emits a `task.updated` event referencing that task id within 1s.
14. **SLA breach**: A task with `sla_due_at` in the past, after `board.scan_sla_breaches` runs, has `sla_breached_at` set and exactly one `sla_breached` activity event (idempotent on re-run).
15. **Frontend optimistic + rollback**: In the Board view, dragging a card to a new column updates the UI immediately; on a server 409 the card returns to its original column and a toast is shown (Playwright/MSW test).
16. **Keyboard-first**: Command palette opens on `Cmd+K`/`Ctrl+K`; `C` opens create-task; `G then B/L/R/K` switches views; all assert focusable/operable without mouse (component test).
17. **View switching no reload**: Switching List↔Board updates `?view=` and re-renders client-side with no full document navigation (e2e asserts no page load event).
18. **Empty states**: A project with zero tasks renders the per-view `EmptyState` with a create CTA.
19. **Spec-gating default**: `POST /projects/{id}/tasks` with `kind=feature` and no explicit `requires_approval` persists `requires_approval.spec=true` (spec-gated by default, per FORGE_SPEC "Spec Gating Rules"); with `kind=bug|chore` it persists `spec=false`; an explicit `requires_approval` in the body is honored verbatim. The board stores the flag only — gate enforcement is owned by F02/F07/F08.

---

## 7. Test plan (TDD) — concrete test cases (unit + integration), key fixtures

Write tests first; each maps to an acceptance criterion (AC#).

**board-core unit** (`packages/board-core/tests/`, pytest, no DB):
- `test_ranking.py`: `rank_between(None,None)` seeds; `rank_between(a,b)` is strictly between and lexicographically ordered; 1000 sequential inserts at head/tail/middle never collide; `prev>=nxt` raises.
- `test_dependencies.py`: cycle detection on direct (A→A guarded by CK earlier), 2-node, N-node cycles returns path (AC6); `would_create_cycle` returns None on DAG add; `topological_order` valid; `blockers_of`/`blocked_by` correct.
- `test_statuses.py`: empty/None transitions allow all; allow-list blocks excluded transition, permits listed; `assert_allowed` raises `StatusTransitionError` (AC4).
- `test_filters.py`: each `FilterOp` builds expected SQL clause (compile to string); `@me` resolves; unknown field/invalid value raises `ValueError`; nested `any`/`all` groups compose (AC8).
- `test_keys.py`: key formatting for task/epic/incident.

**API integration** (`apps/api/tests/board/`, pytest + `httpx.AsyncClient` + ephemeral Postgres via testcontainers or a transactional test DB, factory-boy fixtures):
- `test_projects.py`: create seeds statuses (AC1); archive hides from default list.
- `test_tasks_numbering.py`: gap-free keys; `asyncio.gather` of 10 creates → 10 distinct numbers (AC2).
- `test_task_defaults.py`: `kind=feature` → `requires_approval.spec=true`; `kind=bug|chore` → `spec=false`; explicit `requires_approval` honored verbatim; default status applied; rank seeded (AC19).
- `test_subtasks.py`: AC3.
- `test_status_move.py`: transition policy 409 + activity event (AC4); valid move reranks position.
- `test_concurrency.py`: stale-version 409 (AC5).
- `test_dependencies_api.py`: cycle 409 + nothing persisted (AC6); happy path + graph (AC7).
- `test_query.py`: filter/group/cursor (AC8).
- `test_saved_filters.py`: scope visibility (AC9).
- `test_bulk.py`: 50-task atomic update + rollback on bad id (AC10).
- `test_timeline.py`: merged ordering + internal ingest (AC11).
- `test_rbac.py`: viewer/agent-runner/cross-workspace (AC12).
- `test_sla.py`: invoke `scan_sla_breaches` directly against seeded data; idempotent (AC14).
- `test_sse.py`: open stream, mutate, assert event within timeout (AC13).

**Frontend** (`apps/web`, Vitest + Testing Library + MSW for unit/component; Playwright for e2e):
- `CommandPalette.test.tsx`: Cmd+K opens; arrow/enter run actions (AC16).
- `BoardView.test.tsx`: optimistic drag patches cache; MSW 409 rolls back + toast (AC15).
- `useBoardStream.test.ts`: SSE message patches the matching query entry (AC13).
- `view-switching.spec.ts` (Playwright): List↔Board no full reload, URL updates (AC17).
- `empty-state.spec.ts`: empty project shows CTA (AC18).

**Key fixtures**: `workspace_factory`, `user_factory(role=...)`, `project_factory` (auto-seeds statuses), `task_factory`, `principal(role)` auth dependency override, `seed_demo_board` (1 project, 6 statuses, 2 epics, ~30 tasks, 3 dependencies, 1 sprint, 2 milestones) reused by query/filter/perf tests.

---

## 8. Security & policy considerations

- **Tenant isolation**: every query is filtered by `workspace_id` resolved from the authenticated `Principal`; cross-workspace ids return 404 (no existence leak). A shared `scoped_query(model, principal)` helper enforces this and is the only sanctioned way services load board rows.
- **RBAC** (from `cross-cutting/F37-auth-secrets-byok`): `viewer` read-only; `member` full board CRUD within workspace; `admin` adds project/status/team admin + delete; `agent-runner` may create/patch tasks, post comments, and push `activity_events` via `/internal/activity-events` but cannot delete projects or change RBAC. Enforced via `require_role(...)` dependencies per route.
- **Internal ingest endpoint** (`/internal/activity-events`) requires a service/`agent-runner` scope and validates `entity_type`/`entity_id` belongs to the caller's workspace; payloads are size-capped and stored as `jsonb` without executing anything.
- **Audit (non-negotiable)**: `activity_events` is the board's per-domain append-only change feed — append-only at the service layer and hardened DB-side via the `attach_immutability_trigger(activity_events)` helper from `cross-cutting/F39-audit-log` (the same pattern F06/F07/F09 adopt). It is **not** the central audit log: every security-relevant board action (all `agent-runner`/service writes, all `/internal/activity-events` ingests, project/status deletes, bulk mutations) additionally emits a redacted `AuditEvent` through F39's `AuditSink` with a `detail_ref` pointing back to the originating `activity_events`/entity row — satisfying the spec Security requirement "Audit log — every agent action … immutable, queryable."
- **Spec-gating & human-approval-before-merge (non-negotiables)**: the board stores routing/gate flags but enforces neither. `TaskCreate.requires_approval` defaults to `{spec:false, plan:false, pr:true, deploy:true}`; the task service overrides `spec=true` when `kind == feature` so feature-class work is spec-gated by default (FORGE_SPEC "Spec Gating Rules"), and `v1/F02-spec-engine` sets it explicitly for spec-generated tasks (AC19). The `pr:true` default encodes "human approval is required before PR merge — always"; the board never opens or merges PRs — merge gating lives in `v1/F08-plan-execute-verify-pr-approval` + `cross-cutting/F36-human-approval-system`, which read these flags.
- **Input validation**: all bodies are Pydantic v2 validated; `FilterPredicate` fields are whitelisted (`FILTERABLE_FIELDS`) so user filters cannot reference arbitrary columns or inject SQL (clauses are built via SQLAlchemy expression objects, never string interpolation).
- **Secret redaction**: `repo_targets`/`description`/comment bodies may contain user text but never store credentials; the events publisher strips any `token`/`secret`/`authorization` keys from `payload` before persisting/publishing.
- **Rate limiting**: per-user write limit and per-workspace SSE connection cap (Redis-backed, via the `cross-cutting/F37-auth-secrets-byok` rate limiter) to prevent abuse of optimistic mutation + stream endpoints.
- **Optimistic concurrency** prevents lost updates from concurrent humans/agents (version check → 409).

---

## 9. Effort estimate & risk (S/M/L + key risks)

**Effort: L** (~3–4 engineer-weeks: ~1.5 backend/board-core, ~1.5 frontend, ~0.5 tests/wiring). It is the largest v1 slice by surface area.

Key risks:
- **Ordering scheme** (Med): naive integer/float positions collide or require mass rewrites on reorder. Mitigation: lexorank `rank_between` with a periodic rebalance job; covered by `test_ranking.py`.
- **Live-sync consistency** (Med): SSE + optimistic updates can drift. Mitigation: every envelope carries `version` + `changed[]`; client reconciles by id+version and falls back to query invalidation on gaps. Caddy SSE buffering must be disabled (3.5).
- **Cycle-detection cost** (Low/Med): per-add full-graph load is fine at v1 scale (<10k edges/project) but should load only the project subgraph; documented limit.
- **Sub-100ms perf** (Med): large lists need virtualization + indexed `tasks` queries; the `seed_demo_board` perf fixture and an EXPLAIN check on the hottest query (`tasks` by project+status+position) guard against regressions.
- **Scope creep** (Med): automations/velocity must stay out (v2) to keep the slice shippable — enforced by §12.

---

## 10. Key files / paths (exact)

Backend / domain:
- `packages/board-core/pyproject.toml`
- `packages/board-core/src/board_core/{enums,ranking,keys,dependencies,statuses,filters,events,errors}.py`
- `packages/board-core/src/board_core/models/{project,status,epic,task,dependency,label,sprint,milestone,incident,comment,activity_event,saved_filter}.py`
- `packages/board-core/src/board_core/services/{projects,statuses,epics,tasks,dependencies,timeline,filters,search}.py`
- `packages/board-core/tests/test_{ranking,dependencies,statuses,filters,keys}.py`
- `apps/api/forge_api/routers/board/{projects,statuses,epics,tasks,subtasks,dependencies,labels,sprints,milestones,comments,timeline,events_ingest,filters,bulk,search,stream}.py`
- `apps/api/forge_api/schemas/board.py`
- `apps/api/forge_api/deps/scoping.py` (the `scoped_query` helper added here; `get_principal`/`require_role` imported from `forge_api/deps/auth.py`, owned by `cross-cutting/F37-auth-secrets-byok`)
- `apps/api/alembic/versions/0002_board_core.py`
- `apps/worker/forge_worker/tasks/board_tasks.py` (Celery `scan_sla_breaches`) + Beat schedule entry in `apps/worker/forge_worker/beat.py`
- `apps/api/tests/board/test_*.py`

Frontend:
- `apps/web/app/[workspace]/projects/[projectKey]/page.tsx`
- `apps/web/app/[workspace]/projects/[projectKey]/[taskKey]/page.tsx`
- `apps/web/app/[workspace]/my-tasks/page.tsx`
- `apps/web/components/board/{CommandPalette,ViewSwitcher,FilterBar,ListView,BoardView,BoardColumn,TaskCard,RoadmapView,BacklogView,TaskDetailPanel,Timeline,CommentComposer,DependencyEditor,BulkActionBar,CreateTaskDialog,StatusPicker,PriorityPicker,AssigneePicker,LabelPicker,EmptyState}.tsx`
- `apps/web/lib/board/{api,queries,mutations,useBoardStream,keymap,types}.ts`
- `apps/web/tests/board/*.{test.tsx,spec.ts}`

Infra:
- `deploy/caddy/Caddyfile` (SSE matcher block)
- `deploy/docker-compose.yml` (worker beat)

---

## 11. Research references (relevant links from the spec/research report)

- Linear (UX/keyboard-first reference): https://linear.app/
- Plane.so (OSS PM reference): https://plane.so/
- GitHub Projects (entity/field model reference): https://docs.github.com/en/issues/planning-and-tracking-with-projects
- Symphony — task-as-control-plane (why the board is the control plane): https://openai.com/index/open-source-codex-orchestration-symphony/ , https://www.infoq.com/news/2026/05/openai-symphony-agents/
- Open SWE — structured task context over free-form prompting (informs Task fields): https://github.com/langchain-ai/open-swe
- TanStack Table (List view) / TanStack Query (optimistic cache): https://tanstack.com/table , https://tanstack.com/query
- shadcn/ui (component layer): https://ui.shadcn.com/
- SQLAlchemy 2.x / Alembic / Pydantic v2 / FastAPI (stack): https://docs.sqlalchemy.org/en/20/ , https://alembic.sqlalchemy.org/en/latest/ , https://docs.pydantic.dev/latest/
- Spec sources of truth: `docs/FORGE_SPEC.md` (Native Project Board, Core Data Model, Task Schema, Human Approval System, Security) and `docs/forge-research-report.md` (Symphony/Open SWE "what to borrow").

---

## 12. Out of scope / future

- **Automation rule engine** ("when status=merged → close linked spec task") — v2 (`Saved workflow automations`). This slice persists the data the rules will act on but ships no rule evaluator.
- **Sprint velocity dashboards & burndown** — v2 (`Sprint management and velocity dashboards`, `v2/F26-sprint-velocity`). v1 ships `sprints`/`milestones` tables + basic assignment only.
- **Materialized `milestone_progress` cache + periodic recompute task** — deferred; v1 computes Roadmap milestone progress (completed vs total task counts per open milestone) on read. Introduced only if a perf regression appears against the `seed_demo_board` fixture.
- **Incident workflow states** (`alert_received → … → postmortem_created`) — v2. v1 ships only the minimal `incidents` table for hierarchy/board display.
- **External PM adapters** (`PMAdapter` Protocol: Jira/Linear/Asana/Monday) — v2 (`packages/integration-sdk`). v1 guarantees a stable, serializable `ForgeTask` domain shape so the adapter can map cleanly.
- **Real-time multi-cursor / presence** and conflict-free collaborative text editing of descriptions — future; v1 uses optimistic single-writer + version conflict.
- **Roadmap drag-to-reschedule with dependency propagation** — v1 Roadmap is read-mostly (view + milestone/epic placement); advanced scheduling is future.
- **Saved filter sharing across workspaces** and org-level views — future (workspace-scoped only in v1).
