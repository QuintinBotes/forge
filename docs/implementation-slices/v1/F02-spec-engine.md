# F02 — Spec-Driven Development Engine

> Phase: v1 · Spec module(s): Spec Engine (`packages/spec-engine`), SDD lifecycle, spec folder layout, manifest schema, spec gating rules · Status target: "done" = the full SDD lifecycle (Constitution → Specify → Clarify → Plan → Tasks → Validate) is persisted, version-snapshotted, and human-gated; the manifest round-trips losslessly between YAML and the database; the spec's gating rules are enforced as callable contracts — a spec-approval readiness check, an implementation gate, and a merge gate — consumed by the workflow engine (`v1/F07-feature-workflow-fsm`) and the PR-approval flow (`v1/F08-plan-execute-verify-pr-approval`); requirement→acceptance-criteria→test→diff traceability is queryable; and the phases are drivable through the FastAPI routes, the `forge spec` / `forge constitution` / `forge validate` CLI, and a minimal review/approval UI. A deterministic template generator ships so the slice is fully testable without an LLM; the real LLM generator (spec-analyst skill) plugs in behind a Protocol.

---

## 1. Intent — what & why

Forge's core principle #4 is "Spec-driven development is native. No ad hoc prompting for feature-class work." This slice builds the Spec Engine: the system of record for the SDD lifecycle and the gatekeeper that prevents agents from writing or merging code without an approved, traceable specification.

Concretely the Spec Engine must:

1. Persist the seven SDD artifacts (`spec.md`, `clarify.md`, `plan.md`, `tasks.md`, `validation.md`, `decisions.md`, `manifest.yaml`) as version-snapshotted, immutable-history records in Postgres + MinIO.
2. Maintain a strict lifecycle state machine for each `SpecDocument` (`draft → clarifying → approved → implementing → validated → closed`) plus a plan sub-state, with every transition recorded for audit.
3. Provide a machine-readable `manifest.yaml` projection that round-trips losslessly to/from the database, with full schema + cross-field validation (requirement/AC id formats, AC→requirement reference integrity).
4. Expose the spec gating rules as deterministic, side-effect-free functions that the workflow engine (`v1/F07-feature-workflow-fsm`) and the PR-approval flow (`v1/F08-plan-execute-verify-pr-approval`) call before `start_agent_run` and before merge — Forge never lets the agent self-grant these.
5. Build requirement→acceptance-criteria→test→diff traceability so the approval UI can show "which acceptance criteria are satisfied and how" (spec gating rules 3 and 4: agent output cites which AC it satisfies, and the approval UI shows requirement-to-test and requirement-to-diff traceability).

Why it matters: the Spec Engine is the contract layer that makes agent output auditable and trustworthy. It is an upstream dependency for the workflow engine (`v1/F07-feature-workflow-fsm`), the PR-approval flow (`v1/F08-plan-execute-verify-pr-approval`), and the evaluation harness (`v1/F12-eval-harness`, requirement-to-test traceability reports).

## 2. User-facing behavior / journeys

**Journey A — Author a constitution (once per project).** An admin runs `forge constitution init` (or uses the constitution editor UI). Forge seeds `constitution.md` from `spec-templates/constitution.md`, the admin edits engineering principles and architecture guardrails, then approves it (human review gate). Specs cannot be approved until a constitution exists and is approved for the project.

**Journey B — Specify.** A member creates a spec from a feature epic: `forge spec create --epic EPIC-17 --title "Customer search endpoint"` (or "New Spec" in the UI). The Spec Engine allocates a key (`SPEC-17`), a slug, and dispatches a generation task that produces `spec.md` + a draft manifest with `requirements[]`, `acceptance_criteria[]`, and `open_questions[]`. Status = `draft`. The user reviews/edits requirements and acceptance criteria in the UI.

**Journey C — Clarify.** If open questions exist, `forge spec clarify` moves the spec to `clarifying` and surfaces each question. A human answers each (sign-off). When all questions are resolved, the spec returns to `draft` (ready for approval).

**Journey D — Approve the spec (gate).** A member/admin clicks Approve in the spec review panel. The panel shows the requirement↔acceptance-criteria matrix. Approval is blocked with explicit reasons if: no approved constitution, zero requirements, zero acceptance criteria, any acceptance criterion references no requirement, or any open question is unresolved. On approval, status = `approved` and the event is recorded.

**Journey E — Plan & Tasks.** `forge spec plan` generates `plan.md` + `decisions.md` (ADRs); if the task/skill profile requires a plan, a human approves it (plan sub-gate). `forge spec tasks` generates `tasks.md` (phased units) and creates `Task` rows on the board (F01). Tasks are auto-generated and human-editable.

**Journey F — Implement gate.** When the workflow engine attempts `start_agent_run` for a feature-class task, it calls `assert_implementation_allowed`. If the spec is not `approved` (or plan not approved when required), the run is blocked and the task moves to `needs_human_input`. On the first task entering execution, the spec moves `approved → implementing`.

**Journey G — Validate (merge gate).** After an agent run, `forge validate <task-id>` (or the verify worker step) records a `ValidationReport` whose verdicts map each acceptance criterion to the tests and diffs that satisfy it. `assert_merge_allowed` blocks merge unless every acceptance criterion has a satisfied verdict with at least one test reference. When all acceptance criteria are validated across the spec's tasks, status = `validated`. Closing the spec moves it to `closed`.

## 3. Vertical slice

### 3.1 Data model (tables/columns/migrations touched)

New Alembic migration `apps/api/alembic/versions/0003_spec_engine.py` (the `0003` is ordering only; it chains after `0002_board_core` from `v1/F01-project-board`, which itself chains after the `0001_foundations` baseline from the foundation slice). All ORM models import the shared `Base`/`TimestampMixin` and async session from `packages/db-core` (provided by the foundation slice). New Postgres enums (created if not already present from F01): `spec_status`, `plan_status`, `spec_artifact_kind`, `open_question_status`, `spec_decision_status`, `validation_status`. `execution_mode` is reused from F01's board-core enum if present, otherwise created here.

**`constitutions`** (one active version per project; full version history retained)
- `id` uuid PK
- `project_id` uuid FK → `projects.id` ON DELETE CASCADE
- `version` int NOT NULL
- `status` enum(`draft`,`approved`) NOT NULL DEFAULT `draft`
- `content` text NOT NULL — markdown body
- `principles` jsonb NOT NULL DEFAULT `[]` — `[{id, text}]`, ids like `engineering/api-principles`
- `guardrails` jsonb NOT NULL DEFAULT `[]`
- `approved_by` uuid FK → `users.id` NULL; `approved_at` timestamptz NULL
- `created_by` uuid FK → `users.id`; `created_at`, `updated_at` timestamptz NOT NULL
- UNIQUE(`project_id`, `version`); partial UNIQUE(`project_id`) WHERE `status='approved'` enforced at service layer via "only latest approved is active"

**`spec_documents`**
- `id` uuid PK
- `key` text NOT NULL — human id, e.g. `SPEC-17`
- `project_id` uuid FK → `projects.id` ON DELETE CASCADE
- `epic_id` uuid FK → `epics.id` NULL
- `name` text NOT NULL
- `slug` text NOT NULL — e.g. `customer-endpoint`
- `status` enum `spec_status`(`draft`,`clarifying`,`approved`,`implementing`,`validated`,`closed`) NOT NULL DEFAULT `draft`
- `plan_status` enum `plan_status`(`none`,`drafting`,`review`,`approved`) NOT NULL DEFAULT `none`
- `execution_mode` enum `execution_mode`(`single_agent`,`supervised_multi_agent`) NOT NULL DEFAULT `single_agent`
- `skill_profile` text NOT NULL
- `repos` jsonb NOT NULL DEFAULT `[]` — list of repo ids
- `constitution_refs` jsonb NOT NULL DEFAULT `[]`
- `constraints` jsonb NOT NULL DEFAULT `[]`
- `plan_ref` text NULL, `tasks_ref` text NULL, `validation_ref` text NULL
- `current_version` int NOT NULL DEFAULT 1
- `created_by` uuid FK → `users.id`
- `approved_by` uuid NULL, `approved_at` timestamptz NULL
- `plan_approved_by` uuid NULL, `plan_approved_at` timestamptz NULL
- `created_at`, `updated_at` timestamptz NOT NULL
- UNIQUE(`project_id`, `key`); INDEX(`project_id`, `status`); INDEX(`epic_id`)

**`spec_requirements`**: `id` uuid PK, `spec_id` FK ON DELETE CASCADE, `ext_id` text (`R1`), `text` text, `position` int; UNIQUE(`spec_id`,`ext_id`).

**`spec_acceptance_criteria`**: `id` uuid PK, `spec_id` FK ON DELETE CASCADE, `ext_id` text (`A1`), `text` text, `req_refs` jsonb NOT NULL DEFAULT `[]` (list of requirement `ext_id`s), `position` int; UNIQUE(`spec_id`,`ext_id`).

**`spec_open_questions`**: `id` uuid PK, `spec_id` FK ON DELETE CASCADE, `ext_id` text (`Q1`), `text` text, `status` enum `open_question_status`(`open`,`resolved`) DEFAULT `open`, `resolution` text NULL, `resolved_by` uuid NULL, `resolved_at` timestamptz NULL, `position` int; UNIQUE(`spec_id`,`ext_id`).

**`spec_decisions`** (ADRs): `id` uuid PK, `spec_id` FK ON DELETE CASCADE, `ext_id` text (`ADR-1`), `title` text, `status` enum `spec_decision_status`(`proposed`,`accepted`,`superseded`), `context` text, `decision` text, `consequences` text, `position` int; UNIQUE(`spec_id`,`ext_id`).

**`spec_artifacts`** (version-snapshotted markdown/manifest; append-only): `id` uuid PK, `spec_id` FK ON DELETE CASCADE, `kind` enum `spec_artifact_kind`(`spec`,`clarify`,`plan`,`tasks`,`validation`,`decisions`,`manifest`), `version` int NOT NULL, `content` text NOT NULL, `content_hash` text NOT NULL (sha256), `storage_uri` text NULL (MinIO key), `created_by` uuid, `created_at` timestamptz NOT NULL; UNIQUE(`spec_id`,`kind`,`version`); INDEX(`spec_id`,`kind`).

**`spec_status_transitions`** (immutable audit): `id` uuid PK, `spec_id` FK ON DELETE CASCADE, `from_status` text NULL, `to_status` text NOT NULL, `gate` text NULL (`constitution`|`spec`|`clarify`|`plan`|`validation`), `actor_id` uuid, `reason` text NULL, `created_at` timestamptz NOT NULL; INDEX(`spec_id`,`created_at`).

**`spec_validation_reports`**: `id` uuid PK, `spec_id` FK ON DELETE CASCADE, `task_id` uuid FK → `tasks.id`, `status` enum `validation_status`(`pass`,`fail`), `verdicts` jsonb NOT NULL (list of `CriterionVerdict`), `coverage` numeric NULL, `created_by` uuid, `created_at` timestamptz NOT NULL; INDEX(`spec_id`), INDEX(`task_id`).

> Canonical source of truth = relational rows. `manifest.yaml` and the markdown artifacts are generated projections; editing the manifest diffs back into the rows (see `manifest.py`). MinIO stores immutable artifact snapshots in bucket `forge-specs` under `specs/{spec_key}/{kind}/v{version}.md`.

### 3.2 Backend (FastAPI routes + services/packages)

**New package `packages/spec-engine`** (`forge_spec_engine`) holds all domain logic; `apps/api` holds thin routers + dependency wiring; `apps/worker` holds the generation Celery tasks.

FastAPI routers (`apps/api/forge_api/routers/specs.py`, `constitution.py`). All routes require auth; RBAC noted in §8. Base prefix `/api/v1`.

Constitution:
- `POST /projects/{project_id}/constitution` — create/replace draft (init from template if absent). Body: `ConstitutionUpsert`. → `ConstitutionRead`
- `POST /projects/{project_id}/constitution/approve` — approve current draft (admin). → `ConstitutionRead`
- `GET  /projects/{project_id}/constitution` — latest version + status.

Spec lifecycle:
- `POST /projects/{project_id}/specs` — Specify. Body `SpecCreate{epic_id?, title, prompt, repos[], skill_profile, execution_mode?}`. Allocates key+slug, enqueues `spec.generate_draft`, returns `SpecRead` (status `draft`, generation pending flag).
- `GET  /specs/{spec_id}` → `SpecRead` (full aggregate: requirements, AC, open questions, decisions, statuses).
- `GET  /specs/{spec_id}/manifest` → `text/yaml` rendered from rows.
- `PUT  /specs/{spec_id}/manifest` — body `text/yaml`; parse + validate + diff into rows; snapshots a new `manifest` artifact version. 422 on validation failure with per-field errors.
- `POST /specs/{spec_id}/clarify` — enqueue `spec.generate_clarifications`; set status `clarifying` if questions exist.
- `POST /specs/{spec_id}/clarify/resolve` — body `ClarifyResolve{resolutions:[{question_id, resolution}]}`; marks questions resolved; when none remain open, returns status to `draft`.
- `POST /specs/{spec_id}/approve` — spec gate. Runs `evaluate_spec_approval_readiness`; 409 with reasons if not ready; else transition → `approved`.
- `POST /specs/{spec_id}/reject` — body `{reason}`; transition `approved|clarifying → draft`.
- `POST /specs/{spec_id}/plan` — enqueue `spec.generate_plan`; set `plan_status=drafting`.
- `POST /specs/{spec_id}/plan/approve` — set `plan_status=approved` (records `plan_approved_*`).
- `POST /specs/{spec_id}/tasks` — enqueue `spec.generate_tasks` (creates board Task rows via F01).
- `GET  /specs/{spec_id}/artifacts/{kind}` — latest snapshot (or `?version=`).
- `GET  /specs/{spec_id}/traceability` → `list[TraceRow]` (requirement→AC→test→diff matrix).
- `POST /specs/{spec_id}/validation` — body `ValidationSubmit{task_id, agent_claims[], test_results[]}`; builds + stores a `ValidationReport`; updates status to `validated` when all AC validated.
- `GET  /specs/{spec_id}/validation` — latest report(s).

Internal (in-process; called by the workflow FSM `v1/F07-feature-workflow-fsm` before `start_agent_run` and by the PR-approval flow `v1/F08-plan-execute-verify-pr-approval` before merge — not HTTP): `assert_implementation_allowed`, `assert_merge_allowed`, `mark_implementing`. Exposed from `forge_spec_engine.gates` and `forge_spec_engine.service`.

`SpecService` (`packages/spec-engine/src/forge_spec_engine/service.py`) orchestrates repository + lifecycle + generator + artifacts, and is the single entry point both routers and Celery tasks call. It takes an injected `SpecGenerator`, `SpecRepository`, `ArtifactStore`, and three thin ports defined in `forge_spec_engine.ports` (each satisfied by another slice, each with a permissive in-package stub so F02 builds and tests standalone):

- `SkillProfilePort` — `plan_required(skill_profile, *, workspace_id) -> bool` and `profile_exists(skill_profile, *, workspace_id) -> bool`. Satisfied by the `SkillProfileRegistry` from `v1/F11-skill-profiles` (`registry.resolve_directives(name, workspace_id=...).plan_required`). This replaces the earlier `PolicyPort.plan_required` design: `requires_plan` is a skill-profile attribute, not a repo-policy attribute (FORGE_SPEC.md §"Skill Profiles").
- `RepoPolicyPort` — `repo_in_workspace(repo_id, *, workspace_id) -> bool` (backed by `RepositoryConnection` from `v1/F03-github-app`) and `skill_profile_allowed(repo_id, skill_profile, *, workspace_id) -> bool` (backed by the repo `Policy.skill_profiles.allowed` from `v1/F04-repo-policy`). Used by manifest validation to scope `repos[]`/`skill_profile`.
- `BoardPort` — `create_task(generated_task, *, project_id, spec_id, epic_id, skill_profile, execution_mode, requires_spec_approval) -> task_key` and `emit_spec_decision(*, task_id, spec_id, payload) -> None`. Backed by board-core (`v1/F01-project-board`): task creation via `POST /projects/{project_id}/tasks` (`TaskCreate`, setting `spec_id`/`skill_profile`/`requires_approval.spec`) and timeline events via `POST /internal/activity-events` with `event_type=spec_decision`.

CLI (`apps/api/forge_cli/spec.py`, registered under `forge` Typer app): `forge constitution init|approve`, `forge spec create|clarify|plan|tasks|show`, `forge validate <task-id>`. Each command is a thin wrapper that calls `SpecService` directly (when run inside the API container) so the lifecycle is scriptable for self-hosting and the golden eval harness.

### 3.3 Worker / agent runtime (Celery tasks, LangGraph, if any)

Celery tasks in `apps/worker/forge_worker/tasks/spec_tasks.py`, queue `spec`. Each task loads the spec aggregate, calls the injected `SpecGenerator`, persists results through `SpecService`, snapshots artifacts, and emits a `spec_decision` timeline event onto the linked task/epic via `BoardPort.emit_spec_decision` (board-core `POST /internal/activity-events`, `event_type=spec_decision`, service scope):

- `spec.generate_draft(spec_id)` → `SpecGenerator.draft_spec` → writes requirements/AC/open-questions rows + `spec` + `manifest` artifacts.
- `spec.generate_clarifications(spec_id)` → `draft_clarifications` → writes/updates open-question rows + `clarify` artifact.
- `spec.generate_plan(spec_id)` → `draft_plan` → writes `plan` + `decisions` artifacts + ADR rows; sets `plan_status=review`.
- `spec.generate_tasks(spec_id)` → `draft_tasks` → writes the `tasks` artifact and creates board `Task` rows via `BoardPort.create_task` (board-core `POST /projects/{project_id}/tasks`, `v1/F01-project-board`), setting each task's `spec_id` (link task→spec), `skill_profile`, `execution_mode`, and `requires_approval.spec=true` for feature-class units; the per-AC coverage mapping (`spec_ref` values like `SPEC-17/A1`) is recorded in the `tasks` artifact and on each created task. Also backfills `epics.spec_id` for the spec's epic (per `v1/F01-project-board` §"Downstream consumers": F02 owns `tasks.spec_id`/`epics.spec_id` resolution).
- `spec.snapshot_artifacts(spec_id, kind, version)` → uploads artifact content to MinIO and backfills `storage_uri`.

No LangGraph graph is owned by this slice. The real `SpecGenerator` implementation is the **spec-analyst** skill profile (`v1/F11-skill-profiles`) executed by the agent runtime (`v1/F06-single-execution-agent`); it is injected via the worker's container wiring and resolves its model through the BYOK secrets resolver owned by the foundation slice (no provider keys live in this slice). For V1 buildability and tests, `TemplateSpecGenerator` (deterministic, in `forge_spec_engine.generator`) renders `spec-templates/*` with the request data and parses requirements/AC from a structured prompt block — no LLM, no network.

### 3.4 Frontend / UI (Next.js routes/components, if any)

Next.js (App Router) under `apps/web`:
- `app/projects/[projectId]/constitution/page.tsx` — markdown editor + principles list + Approve action.
- `app/projects/[projectId]/specs/page.tsx` — spec list with `SpecStatusBadge`, key, name, status, plan status.
- `app/specs/[specId]/page.tsx` — detail with tabs: Spec, Clarify, Plan, Tasks, Validation, Decisions, Manifest.
- Spec tab embeds the review/approval panel: `TraceabilityMatrix`, `RequirementList`, `AcceptanceCriteriaList`, `OpenQuestionsPanel`, `ApprovalActions` (Approve / Reject / Request changes). Approve button is disabled with a tooltip listing blocking reasons from `evaluate_spec_approval_readiness`.

Shared components in `packages/ui-kit/src/spec/`: `SpecStatusBadge`, `TraceabilityMatrix`, `RequirementList`, `AcceptanceCriteriaList`, `OpenQuestionsPanel`, `ArtifactViewer` (markdown render), `ApprovalActions`, `ManifestEditor` (YAML with inline validation errors). Data fetched via TanStack Query hooks in `apps/web/src/lib/api/specs.ts`. Optimistic updates for resolve/approve with rollback on error.

### 3.5 Infra / deploy (compose, helm, caddy, if any)

No new services. Reuses `db` (Postgres + pgvector), `redis` + `worker` (adds the `spec` Celery queue), `api`, `minio`. Add MinIO bucket `forge-specs` to the bootstrap script (`deploy/scripts/install.sh` and `make setup`). Ship `spec-templates/{constitution,spec,clarify,plan,tasks,validation,decisions}.md` and `spec-templates/manifest.yaml` in the repo and bake them into the `api`/`worker` images. No Caddy or Helm changes beyond the existing worker deployment gaining the `spec` queue.

## 4. Public interfaces / contracts (exact signatures, Pydantic models, Protocols, YAML schemas)

Pydantic v2 (`packages/spec-engine/src/forge_spec_engine/schemas.py`):

```python
from enum import StrEnum
from datetime import datetime
from typing import Literal, Protocol
from pydantic import BaseModel, Field, model_validator

class SpecStatus(StrEnum):
    DRAFT = "draft"; CLARIFYING = "clarifying"; APPROVED = "approved"
    IMPLEMENTING = "implementing"; VALIDATED = "validated"; CLOSED = "closed"

class PlanStatus(StrEnum):
    NONE = "none"; DRAFTING = "drafting"; REVIEW = "review"; APPROVED = "approved"

class ExecutionMode(StrEnum):
    SINGLE_AGENT = "single_agent"; SUPERVISED_MULTI_AGENT = "supervised_multi_agent"

class Requirement(BaseModel):
    id: str = Field(pattern=r"^R\d+$")
    text: str = Field(min_length=1)

class AcceptanceCriterion(BaseModel):
    id: str = Field(pattern=r"^A\d+$")
    req_refs: list[str] = Field(default_factory=list)
    text: str = Field(min_length=1)

class OpenQuestion(BaseModel):
    id: str = Field(pattern=r"^Q\d+$")
    text: str
    status: Literal["open", "resolved"] = "open"
    resolution: str | None = None

class Decision(BaseModel):  # ADR
    id: str = Field(pattern=r"^ADR-\d+$")
    title: str
    status: Literal["proposed", "accepted", "superseded"]
    context: str; decision: str; consequences: str

class SpecManifest(BaseModel):
    id: str = Field(pattern=r"^SPEC-\d+$")
    name: str
    status: SpecStatus
    constitution_refs: list[str] = Field(default_factory=list)
    repos: list[str] = Field(default_factory=list)
    requirements: list[Requirement]
    acceptance_criteria: list[AcceptanceCriterion]
    constraints: list[str] = Field(default_factory=list)
    plan_ref: str | None = None
    tasks_ref: str | None = None
    validation_ref: str | None = None
    execution_mode: ExecutionMode = ExecutionMode.SINGLE_AGENT
    skill_profile: str

    @model_validator(mode="after")
    def _check(self) -> "SpecManifest":
        rids = [r.id for r in self.requirements]
        if len(rids) != len(set(rids)):
            raise ValueError("duplicate requirement ids")
        aids = [a.id for a in self.acceptance_criteria]
        if len(aids) != len(set(aids)):
            raise ValueError("duplicate acceptance criterion ids")
        known = set(rids)
        for a in self.acceptance_criteria:
            unknown = set(a.req_refs) - known
            if unknown:
                raise ValueError(f"{a.id} references unknown requirements {sorted(unknown)}")
        return self
```

Gating (`forge_spec_engine/gates.py`) — pure functions, no I/O:

```python
from dataclasses import dataclass, field

@dataclass(frozen=True)
class GateResult:
    allowed: bool
    gate: str
    reasons: list[str] = field(default_factory=list)  # empty iff allowed

def evaluate_spec_approval_readiness(
    manifest: SpecManifest, *, constitution_approved: bool, open_questions_unresolved: int
) -> GateResult: ...
# blocks unless: constitution_approved, >=1 requirement, >=1 acceptance criterion,
# every AC has >=1 req_ref, open_questions_unresolved == 0

FEATURE_CLASS_KINDS: frozenset[str] = frozenset({"feature", "change_request"})

def assert_implementation_allowed(
    *, status: SpecStatus, plan_status: PlanStatus, plan_required: bool,
    task_kind: str, requires_spec: bool = False,
) -> GateResult: ...
# spec gate applies iff (task_kind in FEATURE_CLASS_KINDS or requires_spec) — the latter
# carries the task's requires_approval.spec flag (Task Schema). When it applies: requires
# status == APPROVED|IMPLEMENTING and (not plan_required or plan_status == APPROVED).
# Otherwise allowed (non-feature-class, spec not required). `plan_required` is supplied by
# the caller from SkillProfilePort.plan_required (F11), never inferred here.

def assert_merge_allowed(report: "ValidationReport", manifest: SpecManifest) -> GateResult: ...
# Enforces ONLY the spec-validation portion of the merge gate (Workflow DSL `spec_validated`):
# blocks unless report.status == "pass" AND every manifest AC id has a verdict with
# satisfied == True and >=1 test_ref. Human PR approval + green CI are composed alongside
# this by v1/F08-plan-execute-verify-pr-approval (awaiting_review -> merged).
```

Ports (`forge_spec_engine/ports.py`) — narrow Protocols the service depends on; each has a permissive in-package stub (`AllowAllSkillProfilePort`, `AllowAllRepoPolicyPort`, `InMemoryBoardPort`) so the slice builds and tests without F01/F03/F04/F11:

```python
from typing import Protocol

class SkillProfilePort(Protocol):  # satisfied by v1/F11-skill-profiles SkillProfileRegistry
    def plan_required(self, skill_profile: str, *, workspace_id: str) -> bool: ...
    def profile_exists(self, skill_profile: str, *, workspace_id: str) -> bool: ...

class RepoPolicyPort(Protocol):  # repo_in_workspace ← v1/F03-github-app; skill_profile_allowed ← v1/F04-repo-policy
    def repo_in_workspace(self, repo_id: str, *, workspace_id: str) -> bool: ...
    def skill_profile_allowed(self, repo_id: str, skill_profile: str, *, workspace_id: str) -> bool: ...

class BoardPort(Protocol):  # satisfied by v1/F01-project-board board-core
    def create_task(
        self, task: "GeneratedTask", *, project_id: str, spec_id: str, epic_id: str | None,
        skill_profile: str, execution_mode: ExecutionMode, requires_spec_approval: bool,
    ) -> str: ...  # returns the board task key (e.g. CORE-123)
    def emit_spec_decision(self, *, task_id: str | None, spec_id: str, payload: dict) -> None: ...
```

Lifecycle (`forge_spec_engine/lifecycle.py`):

```python
ALLOWED_TRANSITIONS: dict[SpecStatus, set[SpecStatus]] = {
    SpecStatus.DRAFT: {SpecStatus.CLARIFYING, SpecStatus.APPROVED, SpecStatus.CLOSED},
    SpecStatus.CLARIFYING: {SpecStatus.DRAFT, SpecStatus.CLOSED},
    SpecStatus.APPROVED: {SpecStatus.IMPLEMENTING, SpecStatus.DRAFT, SpecStatus.CLOSED},
    SpecStatus.IMPLEMENTING: {SpecStatus.VALIDATED, SpecStatus.DRAFT, SpecStatus.CLOSED},
    SpecStatus.VALIDATED: {SpecStatus.CLOSED, SpecStatus.IMPLEMENTING},
    SpecStatus.CLOSED: set(),
}

class InvalidTransition(Exception): ...

class SpecLifecycle:
    def can_transition(self, frm: SpecStatus, to: SpecStatus) -> bool: ...
    def transition(self, spec, to: SpecStatus, *, actor_id: str,
                   gate: str | None = None, reason: str | None = None): ...
    # writes a spec_status_transitions row; raises InvalidTransition if not allowed
```

Generator port + deterministic impl (`forge_spec_engine/generator.py`):

```python
class ConstitutionSnapshot(BaseModel):
    principles: list[dict]; guardrails: list[str]; approved: bool

class SpecDraftRequest(BaseModel):
    spec_key: str; project_id: str; epic_id: str | None
    title: str; prompt: str; repos: list[str]
    skill_profile: str; execution_mode: ExecutionMode
    constitution: ConstitutionSnapshot

class SpecDraftResult(BaseModel):
    manifest: SpecManifest; spec_markdown: str; open_questions: list[OpenQuestion]

class PlanContext(BaseModel):
    repos: list[str]; constraints: list[str]; constitution: ConstitutionSnapshot

class PlanResult(BaseModel):
    plan_markdown: str; decisions: list[Decision]

class TasksResult(BaseModel):
    tasks_markdown: str
    tasks: list["GeneratedTask"]  # {title, kind, ac_refs:[str], estimate?, skill_profile}

class SpecGenerator(Protocol):
    def draft_spec(self, req: SpecDraftRequest) -> SpecDraftResult: ...
    def draft_clarifications(self, manifest: SpecManifest, spec_markdown: str) -> list[OpenQuestion]: ...
    def draft_plan(self, manifest: SpecManifest, spec_markdown: str, ctx: PlanContext) -> PlanResult: ...
    def draft_tasks(self, manifest: SpecManifest, plan_markdown: str) -> TasksResult: ...

class TemplateSpecGenerator:  # deterministic V1 fallback + test double; renders spec-templates/*
    ...
```

Validation + traceability (`forge_spec_engine/validation.py`, `traceability.py`):

```python
class AgentClaim(BaseModel):
    criterion_id: str; rationale: str; diff_refs: list[str] = []

class TestResult(BaseModel):
    node_id: str; criterion_id: str | None; outcome: Literal["passed", "failed", "skipped"]

class CriterionVerdict(BaseModel):
    criterion_id: str; satisfied: bool
    test_refs: list[str] = []; diff_refs: list[str] = []; rationale: str | None = None

class ValidationReport(BaseModel):
    spec_id: str; task_id: str
    status: Literal["pass", "fail"]
    verdicts: list[CriterionVerdict]
    coverage: float | None = None
    created_at: datetime

def build_validation_report(
    manifest: SpecManifest, claims: list[AgentClaim],
    test_results: list[TestResult], *, spec_id: str, task_id: str, coverage: float | None
) -> ValidationReport: ...
# a criterion is satisfied iff it has a passing test mapped to it AND an agent claim;
# status == "pass" iff all manifest AC are satisfied.

class TraceRow(BaseModel):
    requirement_id: str; requirement_text: str
    criteria: list[AcceptanceCriterion]
    tests: list[str]; diffs: list[str]
    status: Literal["uncovered", "claimed", "validated"]

def build_traceability_matrix(
    manifest: SpecManifest, report: ValidationReport | None
) -> list[TraceRow]: ...
```

Manifest round-trip (`forge_spec_engine/manifest.py`):

```python
class ManifestValidationError(Exception):
    def __init__(self, errors: list[str]): ...

def parse_manifest(yaml_text: str) -> SpecManifest: ...      # raises ManifestValidationError
def dump_manifest(manifest: SpecManifest) -> str: ...        # canonical, key-ordered YAML
def validate_manifest_dict(data: dict) -> list[str]: ...     # JSON-schema + cross-field; [] if valid
```

JSON Schema file (`packages/spec-engine/src/forge_spec_engine/schema/manifest.schema.json`) mirrors `SpecManifest` (Draft 2020-12): `id` pattern `^SPEC-\d+$`, `status` enum, required `requirements`/`acceptance_criteria`/`skill_profile`, item id patterns, `acceptance_criteria[].req_refs` typed `array<string>`. Used by `PUT /manifest` and the UI `ManifestEditor`.

## 5. Dependencies — features/slices that must exist first

- `v1/F00-foundation-substrate` (REQUIRED) — Workspace/User/Project, `packages/db-core` shared `Base`/`TimestampMixin` + async SQLAlchemy session + Alembic baseline (`0001_foundations`), `packages/contracts` frozen DTOs/Protocols, auth + RBAC dependency (`admin`/`member`/`viewer`/`agent-runner`), MinIO `ArtifactStore`, Celery app, encrypted BYOK secrets vault + resolver, shared secret-redaction filter. (Slug is authoritative; the foundation slice is referred to elsewhere as `v1/F00-foundation-substrate` / `cross-cutting/C01-monorepo-and-api-foundations`.)
- `v1/F01-project-board` (REQUIRED) — `Project`, `Epic`, `Task` entities and the board-core task-creation API (`POST /projects/{project_id}/tasks`) + `/internal/activity-events` ingest used by `spec.generate_tasks` (`BoardPort`); `SpecDocument.epic_id` FK and F02-owned `tasks.spec_id`/`epics.spec_id` resolution.
- `v1/F11-skill-profiles` (REQUIRED, stubbable) — `SkillProfileRegistry.resolve_directives(name).plan_required` is the authoritative source of the plan sub-gate (`SkillProfilePort`); F11 §"Downstream consumers" explicitly states this replaces F02's earlier `PolicyPort.plan_required` stub. `AllowAllSkillProfilePort` (treats every profile as existing, `plan_required=False`) ships here until F11 lands.
- `v1/F04-repo-policy` (REQUIRED, stubbable) — `Policy.skill_profiles.allowed` backs `RepoPolicyPort.skill_profile_allowed`. `AllowAllRepoPolicyPort` is acceptable until F04 lands.
- `v1/F03-github-app` (REQUIRED, stubbable) — `RepositoryConnection` backs `RepoPolicyPort.repo_in_workspace` (manifest `repos[]` must resolve to a connected repo). Covered by `AllowAllRepoPolicyPort` until F03 lands.
- `v1/F06-single-execution-agent` (SOFT, deferred) — provides the real `SpecGenerator` (the spec-analyst skill profile executed by the agent runtime). The slice ships with `TemplateSpecGenerator` so it is fully buildable and testable without F06.

Downstream consumers (NOT blocking this slice; they depend on F02): the workflow FSM `v1/F07-feature-workflow-fsm` (`assert_implementation_allowed`, `mark_implementing` as a `task_ready → executing` precondition), the PR-approval flow `v1/F08-plan-execute-verify-pr-approval` (`assert_merge_allowed` + the traceability matrix in the approval UI), and the evaluation harness `v1/F12-eval-harness` (requirement-to-test traceability reports).

## 6. Acceptance criteria (numbered, testable)

1. Creating a spec via `POST /projects/{id}/specs` allocates a unique `key` (`SPEC-{n}` monotonic per project) and a slug, returns status `draft`, and enqueues `spec.generate_draft`.
2. `spec.generate_draft` with `TemplateSpecGenerator` persists ≥1 requirement, ≥1 acceptance criterion (each referencing a requirement), and an initial `spec` + `manifest` artifact at version 1.
3. `GET /specs/{id}/manifest` then `PUT /specs/{id}/manifest` with the unchanged body is a no-op (idempotent): rows are unchanged and no new `manifest` artifact version is created when content hash is unchanged.
4. `PUT /manifest` with an acceptance criterion referencing a non-existent requirement returns 422 with an error naming the offending AC id and missing requirement id; rows are not modified.
5. `POST /specs/{id}/approve` returns 409 with explicit reasons when any of these hold: no approved constitution, zero requirements, zero acceptance criteria, an AC with empty `req_refs`, or any unresolved open question.
6. `POST /specs/{id}/approve` on a ready spec transitions status `draft → approved`, sets `approved_by`/`approved_at`, and writes a `spec_status_transitions` row with `gate="spec"`.
7. `assert_implementation_allowed` returns `allowed=False` for a `feature` task (or any kind in `FEATURE_CLASS_KINDS = {feature, change_request}`) when spec status is `draft`, and `allowed=True` when status is `approved`/`implementing` and (plan not required OR plan_status `approved`).
8. `assert_implementation_allowed` returns `allowed=True` for a non-feature-class task kind (e.g. `chore`) with `requires_spec=False` regardless of spec status; but the same `chore` with `requires_spec=True` (task's `requires_approval.spec`) is gated like a feature-class task.
9. A `feature` task whose skill profile has `requires_plan=true` (resolved via `SkillProfilePort.plan_required`, sourced from `v1/F11-skill-profiles`, never inferred inside the gate) is blocked while `plan_status != approved`, and allowed after `POST /plan/approve`.
10. `POST /specs/{id}/validation` builds a `ValidationReport` whose `status` is `pass` only when every manifest AC has a verdict with `satisfied=true` and ≥1 `test_ref`; otherwise `fail`.
11. `assert_merge_allowed` returns `allowed=False` listing each unsatisfied/untested AC id when the report is `fail`, and `allowed=True` when all AC are satisfied with test refs.
12. When a validation report passes for the spec's coverage of all AC, status transitions `implementing → validated`.
13. `GET /specs/{id}/traceability` returns one `TraceRow` per requirement; each row lists its AC, mapped tests/diffs, and a status of `uncovered`/`claimed`/`validated` consistent with the latest report.
14. Every status change creates exactly one immutable `spec_status_transitions` row; illegal transitions (e.g. `closed → approved`) raise `InvalidTransition` and write no row.
15. Lifecycle transitions and gate checks are deterministic and side-effect-free in `gates.py` (calling them twice with the same inputs yields identical `GateResult`s and touches no DB).
16. `dump_manifest(parse_manifest(yaml)) == canonical(yaml)` for every example in `examples/specs/` and `spec-templates/manifest.yaml` (lossless round-trip).
17. Resolving the last open question via `POST /clarify/resolve` returns spec status from `clarifying` to `draft`.
18. `spec.generate_tasks` creates board tasks (one per generated unit) through `BoardPort.create_task`, each with `spec_id` set, `skill_profile` set, `requires_approval.spec=true` for feature-class units, and a `spec_ref` mapping (`SPEC-NN/Ax`) to the acceptance criteria it covers; it writes a `tasks` artifact and backfills `epics.spec_id`. (Asserted against an `InMemoryBoardPort` double, with one integration test against the real board-core API.)
19. A `viewer` or `agent-runner` role calling any approval endpoint (`/approve`, `/plan/approve`, `/constitution/approve`) receives 403; the spec gate cannot be self-granted by an agent.
20. Constitution must be approved before any spec in that project can be approved (AC #5 dependency holds end-to-end through the API).
21. Each generation task and each spec status transition emits exactly one `spec_decision` activity event via `BoardPort.emit_spec_decision` (board-core `POST /internal/activity-events`), so the spec lifecycle surfaces in the task's unified timeline; the event payload names the gate/phase and actor and contains no secrets.

## 7. Test plan (TDD) — concrete test cases (unit + integration), key fixtures

Framework: `pytest` + `pytest-asyncio`. Integration tests run against a real Postgres via `testcontainers[postgres]` (jsonb + enums require real PG, not SQLite). Celery tasks tested with `task_always_eager=True`. MinIO faked with an in-memory `ArtifactStore` double for unit tests; one integration test uses the real MinIO container to assert `storage_uri` backfill.

Key fixtures (`packages/spec-engine/tests/conftest.py`): `db_session` (transactional rollback per test), `project_factory`, `epic_factory`, `approved_constitution_factory`, `spec_aggregate_factory` (spec + requirements + AC + open questions at a given status), `template_generator`, `skill_profile_stub` (configurable `plan_required`/`profile_exists` — `SkillProfilePort`), `repo_policy_stub` (configurable `repo_in_workspace`/`skill_profile_allowed` — `RepoPolicyPort`), and `board_port_stub` (`InMemoryBoardPort` capturing `create_task`/`emit_spec_decision` calls — `BoardPort`).

Unit — `schemas.py` / `manifest.py`:
- `SpecManifest` rejects duplicate requirement ids, duplicate AC ids, and AC `req_refs` pointing at unknown requirements (3 cases).
- id pattern validation: `R1`/`A1`/`Q1`/`ADR-1`/`SPEC-17` accepted; `req-1`, `A`, `SPEC17` rejected.
- `parse_manifest` raises `ManifestValidationError` with aggregated field errors on malformed YAML and on schema violations.
- Round-trip: `dump_manifest(parse_manifest(x)) == dump_manifest(parse_manifest(dump_manifest(parse_manifest(x))))` for each `examples/specs/*` and `spec-templates/manifest.yaml` (covers AC #16).

Unit — `gates.py` (table-driven, no DB; covers AC #5, #7, #8, #9, #10, #11, #15):
- `evaluate_spec_approval_readiness`: matrix over {constitution_approved, n_requirements, n_ac, ac_without_refs, unresolved_questions} asserting `allowed` and the exact `reasons` list.
- `assert_implementation_allowed`: matrix over {task_kind (feature/change_request/chore), requires_spec, status, plan_required, plan_status} — asserts feature-class and `requires_spec=True` gate, non-feature-class + `requires_spec=False` bypasses.
- `assert_merge_allowed`: pass report; report missing a verdict; verdict satisfied but no test_ref; report status fail.
- Determinism: call each twice, assert equal results.

Unit — `lifecycle.py` (covers AC #14): every legal transition succeeds and records a row (mocked recorder); a parametrized list of illegal transitions raises `InvalidTransition` and records nothing.

Unit — `validation.py` / `traceability.py` (covers AC #10, #13): `build_validation_report` with full coverage → `pass`; one AC missing a passing test → `fail` listing that AC. `build_traceability_matrix` returns one row per requirement with correct rollup status across {no report, claims-only report, fully-validated report}.

Unit — `TemplateSpecGenerator` (covers AC #2): given a structured prompt, returns a `SpecDraftResult` whose manifest passes `SpecManifest` validation and contains ≥1 requirement/AC.

Integration — API + DB (covers AC #1–#9, #12, #17–#20):
- Specify → key/slug allocation, draft persisted, artifacts v1 created.
- Manifest GET/PUT idempotency (AC #3) and invalid-reference rejection (AC #4).
- Approve blocked-then-allowed lifecycle, including constitution precondition (AC #5, #6, #20).
- Clarify resolve returns to `draft` (AC #17).
- Plan-required gating end-to-end (AC #9).
- Tasks generation creates board tasks via `BoardPort` with `spec_id`/`skill_profile`/`requires_approval.spec`/`spec_ref` and backfills `epics.spec_id` (AC #18) — asserted against `InMemoryBoardPort` (unit) plus one integration test against the real board-core `POST /projects/{id}/tasks` (`v1/F01-project-board`).
- Each generation/transition emits one `spec_decision` event via `BoardPort.emit_spec_decision` (AC #21) — asserted on the `board_port_stub` call log.
- Validation submit → report stored → status `validated` (AC #12); merge gate blocked/allowed (AC #11).
- RBAC: `viewer`/`agent-runner` get 403 on all approval endpoints (AC #19).

Integration — MinIO: `spec.snapshot_artifacts` uploads content and backfills `storage_uri`; re-fetch returns identical bytes.

Eval hook: a golden fixture `examples/specs/SPEC-001-customer-endpoint/` feeds the evaluation harness (`v1/F12-eval-harness`) to assert requirement-to-test traceability output is stable (F12 computes spec completeness + requirement/AC coverage from this slice's `/traceability` output).

## 8. Security & policy considerations

- **Spec gate is non-bypassable & human-only.** Approval endpoints require `member` or `admin`. `viewer` and `agent-runner` are rejected (403). The agent runtime can never call `/approve` — it only reads gate results. This enforces principle #2 (human-in-the-loop) and #4.
- **No implementation without approval** (gating rule 1): `assert_implementation_allowed` blocks feature-class runs until spec (and required plan) are approved. The workflow engine must call it before `start_agent_run`; this slice provides the function and a contract test, but enforcement at the run site is owned by `v1/F07-feature-workflow-fsm` (the `task_ready → executing` precondition) and `v1/F08-plan-execute-verify-pr-approval`.
- **No merge without validation** (gating rule 2): `assert_merge_allowed` requires every AC satisfied with a test reference. Agent-claimed satisfaction alone is insufficient — a passing mapped test is mandatory. This function enforces only the spec-validation (`spec_validated`) portion of the merge gate; the human PR approval and green-CI conditions are composed alongside it by `v1/F08-plan-execute-verify-pr-approval`, preserving the non-negotiable "human approval is required before PR merge — always."
- **Manifest/markdown is untrusted input.** `PUT /manifest` and all generator output pass through `SpecManifest` + JSON-schema validation before persistence; no `yaml.load` (use `yaml.safe_load`); reject documents exceeding a size cap (e.g. 256 KB) to bound storage.
- **Secret redaction.** Artifact content (spec/plan/etc.) is scanned with the platform secret-redaction filter before persistence to Postgres/MinIO, satisfying the spec's "secrets stripped from logs, traces, and retrieval results" requirement (spec content is later indexed by the knowledge service).
- **Repo & skill-profile scoping.** `repos[]` in the manifest must resolve to repositories connected to the workspace (`RepoPolicyPort.repo_in_workspace`, backed by `RepositoryConnection` from `v1/F03-github-app`); `skill_profile` must exist (`SkillProfilePort.profile_exists`, `v1/F11-skill-profiles`) and be in the repo's allowed skill profiles (`RepoPolicyPort.skill_profile_allowed`, backed by `Policy.skill_profiles.allowed` from `v1/F04-repo-policy`). Validation failure → 422.
- **BYOK & read-only knowledge for the real generator.** This slice ships `TemplateSpecGenerator` (no model, no network). When the spec-analyst `SpecGenerator` (`v1/F06-single-execution-agent`) is wired in, it resolves its model strictly through the foundation BYOK secrets resolver (no provider keys in this slice), and its `read_knowledge`/`query_mcp` reads go through the read-only MCP gateway (`v1/F09-mcp-gateway-v1`, `allow_write:false` default) and policy evaluation — F02 stores only the redacted resulting artifacts.
- **Workspace isolation.** All queries are scoped by `project_id → workspace_id`; cross-workspace spec access returns 404 (not 403, to avoid existence disclosure).
- **Immutable audit.** `spec_status_transitions` and `spec_artifacts` are append-only (no UPDATE/DELETE in the repository layer); every gate decision records actor + reason.

## 9. Effort estimate & risk (S/M/L + key risks)

**Effort: L.** Seven persisted artifacts, multi-state lifecycle with a plan sub-gate, lossless manifest round-trip, traceability + validation engines, API + CLI + minimal UI. The deterministic `TemplateSpecGenerator` keeps the slice testable without F06, but the breadth is large.

Key risks:
1. **Generator boundary drift (medium).** The `SpecGenerator` Protocol must stay stable as the real spec-analyst agent (F06) is built; mitigate by freezing the Protocol + DTOs in this slice and contract-testing both the template and the real impl against the same fixtures.
2. **Manifest ↔ rows sync drift (medium).** DB is canonical; the manifest is a projection. Mitigate with the round-trip property tests (AC #16) and by computing artifact `content_hash` to make `PUT` idempotent.
3. **Gate enforcement is split across slices (medium).** F02 owns the gate functions; `v1/F07-feature-workflow-fsm` and `v1/F08-plan-execute-verify-pr-approval` own calling them. A contract test in F02 plus an integration test in F07/F08 prevents an unenforced gate. Document this seam explicitly.
4. **Tasks generation coupling to board-core (low).** Use the F01 task-creation API behind a port so `spec.generate_tasks` is testable with a board double.

## 10. Key files / paths (exact)

Package:
- `packages/spec-engine/pyproject.toml`
- `packages/spec-engine/src/forge_spec_engine/__init__.py`
- `packages/spec-engine/src/forge_spec_engine/models.py` (SQLAlchemy ORM)
- `packages/spec-engine/src/forge_spec_engine/schemas.py`
- `packages/spec-engine/src/forge_spec_engine/manifest.py`
- `packages/spec-engine/src/forge_spec_engine/schema/manifest.schema.json`
- `packages/spec-engine/src/forge_spec_engine/lifecycle.py`
- `packages/spec-engine/src/forge_spec_engine/gates.py`
- `packages/spec-engine/src/forge_spec_engine/validation.py`
- `packages/spec-engine/src/forge_spec_engine/traceability.py`
- `packages/spec-engine/src/forge_spec_engine/generator.py`
- `packages/spec-engine/src/forge_spec_engine/artifacts.py` (ArtifactStore + markdown render)
- `packages/spec-engine/src/forge_spec_engine/repository.py`
- `packages/spec-engine/src/forge_spec_engine/ports.py` (`SkillProfilePort`, `RepoPolicyPort`, `BoardPort` + permissive stubs)
- `packages/spec-engine/src/forge_spec_engine/service.py`
- `packages/spec-engine/src/forge_spec_engine/errors.py`
- `packages/spec-engine/tests/conftest.py` + `tests/test_{manifest,gates,lifecycle,validation,traceability,generator,service}.py`

API / worker / CLI:
- `apps/api/forge_api/routers/specs.py`, `apps/api/forge_api/routers/constitution.py`
- `apps/api/forge_api/deps/spec_engine.py` (DI wiring)
- `apps/api/alembic/versions/0003_spec_engine.py` (chains after `0002_board_core`)
- `apps/api/forge_cli/spec.py`, `apps/api/forge_cli/constitution.py`
- `apps/worker/forge_worker/tasks/spec_tasks.py`

Frontend / templates / examples:
- `apps/web/app/projects/[projectId]/constitution/page.tsx`
- `apps/web/app/projects/[projectId]/specs/page.tsx`
- `apps/web/app/specs/[specId]/page.tsx`
- `apps/web/src/lib/api/specs.ts`
- `packages/ui-kit/src/spec/{SpecStatusBadge,TraceabilityMatrix,RequirementList,AcceptanceCriteriaList,OpenQuestionsPanel,ArtifactViewer,ApprovalActions,ManifestEditor}.tsx`
- `spec-templates/{constitution,spec,clarify,plan,tasks,validation,decisions}.md`, `spec-templates/manifest.yaml`
- `examples/specs/SPEC-001-customer-endpoint/` (golden fixture)

## 11. Research references (relevant links from the spec/research report)

- GitHub Spec Kit (four-phase Specify→Plan→Tasks→Implement with validation gates): https://github.com/github/spec-kit and blog https://github.blog/ai-and-ml/generative-ai/spec-driven-development-with-ai-get-started-with-a-new-open-source-toolkit/
- Microsoft SDD — constitution-first approach: https://developer.microsoft.com/blog/spec-driven-development-ai-native-engineering and deep dive https://developer.microsoft.com/blog/spec-driven-development-spec-kit
- SDD methodology paper (executable contracts improve agent reliability): https://arxiv.org/html/2602.00180v1
- FORGE_SPEC.md — "Spec-Driven Development Engine" (lifecycle table, spec folder layout, manifest schema, gating rules), "Core Data Model" (SpecDocument tree), "Human Approval System" (Spec/Plan/PR gates, approval UI requirements), "Task Schema" (`spec_ref`, `requires_approval`), "Workflow Engine" (feature workflow states, DSL preconditions).
- forge-research-report.md — "Spec-Driven Development" section (Constitution → Specify → Clarify → Plan → Tasks → Implement → Validate; human review at spec- and plan-approval before any code) and "Eval-first development" (requirement-to-test traceability feeds the golden eval set).

## 12. Out of scope / future

- The real LLM-backed `SpecGenerator` (spec-analyst skill, LangGraph loop) — provided by `v1/F06-single-execution-agent`; F02 ships only the deterministic `TemplateSpecGenerator` + Protocol.
- Calling the gate functions at the actual run/merge sites — owned by `v1/F07-feature-workflow-fsm` (implementation gate precondition) and `v1/F08-plan-execute-verify-pr-approval` (merge gate); F02 provides the contracts + contract tests.
- Spec validation dashboard with full requirement traceability visualization — v2 (`v2/F23-spec-validation-dashboard`); F02 exposes the data via `/traceability`, the rich dashboard is later.
- Indexing spec/plan/validation artifacts into the knowledge service (boost weight 1.4x per the spec's chunk-priority table) — owned by `v1/F05-hybrid-knowledge-retrieval`; F02 only emits redacted, snapshot-able content.
- Writing artifacts into the repo working tree / committing `specs/SPEC-NN-*/` files — materialization happens at agent run time (`v1/F06-single-execution-agent`); F02 produces and stores the content.
- Multi-repo spec targets at execution time and supervised-multi-agent spec authoring — v2/v3.
