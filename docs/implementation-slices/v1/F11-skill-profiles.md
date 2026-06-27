# F11 — Skill Profiles (`packages/skill-sdk`)

> Phase: v1 · Spec module(s): Skill Profiles (`packages/skill-sdk`, FORGE_SPEC.md §"Skill Profiles"), Core Design Principle #8 (*"Skill profiles enforce quality structurally — not via prompts"*), Task Schema `skill_profile`, Repo Policy `skill_profiles`, Workflow DSL `skill:` · Status target: **Done** = the seven canonical profiles (`backend-tdd`, `backend-fast`, `frontend-ui`, `incident-response`, `spec-analyst`, `security-review`, `chore-fast`) load and validate through a strict (fail-closed, extra-keys-forbidden) `SkillProfile` schema; a `SkillProfileRegistry` resolves any profile **by name** with workspace-custom-overrides-builtin precedence; a pure `to_directives()` projection turns a profile into normalized, machine-consumable `SkillDirectives` (plan/TDD/coverage/verification/review/action-scope/output) that the spec engine, agent runtime, verification service, and approval layer consume **structurally, never via prompt text**; custom workspace profiles are CRUD-able over `/skills/*` with RBAC; `forge skills lint|list|show` works; and `examples/skills/*.yaml` is asserted byte-identical to the bundled builtins (drift guard) along with the generated `skill-profile.schema.json`.

---

## 1. Intent — what & why

Core Design Principle #8 is the entire reason this slice exists: *"Skill profiles enforce quality structurally — not via prompts."* A skill profile is a named, declarative quality contract attached to a task (Task Schema `skill_profile: backend-tdd`), a spec (`manifest.yaml skill_profile`), and constrained by a repo (`.forge/policy.yaml skill_profiles.{default,allowed}`). When a run starts, the chosen profile is resolved into a set of **directives** that other subsystems obey as data — not as instructions injected into an LLM prompt that the model may ignore.

Concretely, a profile decides, before and during a run:

1. **Planning discipline** — `requires_plan` gates the spec engine's plan sub-gate (F02); `requires_tests_before_implementation` forces the TDD ordering in the agent runtime (F06).
2. **Verification** — `verification_steps` selects which checks the verification service runs (F08); `min_test_coverage` is the coverage threshold the report is judged against; `accessibility_check` adds an a11y gate.
3. **Human gating** — `review_required` / `human_review_required` force the PR approval gate; `requires_human_approval_before_action` + `max_blast_radius` force the incident-response approval-before-action gate (`cross-cutting/F36-human-approval-system`).
4. **Action scope** — `allowed_actions` (positive allowlist for specialist profiles) and `forbidden_actions` compose with the repo-policy `Decision` (F04) inside the runtime tool gate, so e.g. `incident-response` structurally cannot `deploy_prod`.
5. **Specialist output shape** — `output_type` (`code` | `spec_document` | `security_report`), `tools` (`sast`/`dependency_audit`/`secrets_scan`), `report_format` (`sarif`) drive what artifact a run produces and which specialist tools are enabled.
6. **Anti-shortcuts** — `forbidden_shortcuts` (`skip_tests`, `no_error_handling`, `hardcoded_secrets`) project onto concrete gates rather than a polite request to the model.

Without F11, the spec engine can't resolve `plan_required` (F02 currently stubs it), the verification service has no source for `verification_steps`/`min_test_coverage` (F08 hard-depends on it), the repo-policy linter can't cross-check that `skill_profiles.allowed` names resolve (F04 soft-depends), and the agent runtime has no skill-level action scope to compose with policy (F06). F11 is small but load-bearing: it is the structural-quality registry the whole workflow reads from.

This slice ships **deterministically and without any LLM** — a skill profile is plain YAML plus a pure projection function. That is exactly the OSS extension point the spec promises: *"Skill profiles: plain YAML — community contributions welcome."*

---

## 2. User-facing behavior / journeys

**Task author (human, on the board).**
1. Creating/editing a task, picks a `skill_profile` from a dropdown populated by `GET /skills` (builtins + workspace-custom). The picker is constrained to the repo's `policy.skill_profiles.allowed` set when a repo target is set (cross-check via F04).
2. Sees a read-only "what this profile enforces" panel (resolved `SkillDirectives`): plan required, tests-first, coverage ≥ N, verification steps, review required, allowed/forbidden actions, output type.

**Repo maintainer (human).**
1. Lists `skill_profiles.allowed` in `.forge/policy.yaml`. The policy linter (F04) calls `registry.exists(name)` and surfaces a **warning** for any name that doesn't resolve to a builtin or workspace profile (non-fatal, so policies stay loadable without the registry).

**Workspace admin (human).**
1. Opens **Settings → Skill Profiles**, sees the seven builtins (badged `builtin`) plus any custom ones.
2. Creates a custom profile (e.g. `backend-tdd-strict` with `min_test_coverage: 95`) in the in-UI YAML editor; it is linted live via `POST /skills/validate`, then saved via `POST /skills`.
3. Overrides a builtin by creating a custom profile with the same name (e.g. raise `frontend-ui` coverage); the registry now resolves that name to the workspace row (`source: override`), and can `DELETE` it to revert to the builtin.

**Agent runtime / workflow (machine), during a run.**
1. At `task_ready → executing` the orchestrator resolves `directives = registry.resolve_directives(task.skill_profile, workspace_id=...)` once and attaches it to the `WorkflowRun`; this satisfies the `skill_profile_set` precondition (Workflow DSL).
2. The spec engine plan gate reads `directives.plan_required`; the runtime enforces `directives.tdd_required`; the tool gate composes `skill_permits_action(directives, tool_call)` with the repo-policy `Decision`; the verification service runs `directives.verification_steps` against the coverage threshold; the approval layer reads `directives.review_required` and (for incidents) `directives.approval_before_action`/`max_blast_radius`.

**CLI (human / CI).**
- `forge skills list` — table of resolvable profiles and their source.
- `forge skills show backend-tdd` — pretty-prints the profile and its resolved directives.
- `forge skills lint examples/skills/backend-tdd.yaml` — exit 0/1 with line-referenced errors.

---

## 3. Vertical slice

### 3.1 Data model (tables/columns/migrations touched)

`SkillProfile[]` is already in the Phase-0 Core Data Model under `Workspace` (*"reusable skill/behavior templates"*). F11 makes it real. Builtins are **not** stored in the DB — they are bundled package data resolved at runtime; the table holds only **workspace-custom** profiles and **overrides** of builtins. This keeps the seven canonical profiles immutable/versioned in code (and golden-eval-stable) without a seed migration.

**New table — `skill_profile` (`packages/db/forge_db/models/skill_profile.py`):**

| column | type | notes |
|---|---|---|
| `id` | UUID PK | |
| `workspace_id` | UUID FK → `workspace.id` | tenant scope |
| `name` | text | kebab-case slug; **unique per `(workspace_id, name)`** |
| `body` | JSONB | validated `SkillProfile.model_dump()`; enforced in `SkillProfileService`, not the DB |
| `overrides_builtin` | bool | true when `name` shadows a bundled builtin (set on write by comparing to the builtin name set) |
| `created_by` | UUID FK → `app_user.id` null | actor for audit |
| `created_at` / `updated_at` | timestamptz | |

- Unique constraint: `uq_skill_profile_workspace_name (workspace_id, name)`.
- Index: `ix_skill_profile_workspace (workspace_id)`.

**Migration:** one Alembic revision `xxxx_add_skill_profile` — `CREATE TABLE skill_profile` with the unique constraint and index. Additive and backward-compatible.

No change to `Task`/`SpecDocument` rows: both already carry a `skill_profile` **string** (the name). F11 is the resolver for that string; it does not add columns to those tables.

### 3.2 Backend (FastAPI routes + services/packages)

**Package `packages/skill-sdk/forge_skills/` (the core; no FastAPI imports):**

- `schema.py` — the `SkillProfile` Pydantic tree + enums (§4), re-exported from `forge_contracts.skill` so the YAML-facing model and the frozen contract DTO are the same object.
- `directives.py` — `to_directives(profile) -> SkillDirectives` (pure normalization) and `skill_permits_action(directives, action) -> Decision` (composition helper) + the `ACTION_ALIASES` map.
- `loader.py` — `load_skill_profile()`, `load_skill_profile_file()`, `validate_skill_profile_yaml()`.
- `registry.py` — `BuiltinSkillProfileRegistry` (bundle-only, pure) and `DbSkillProfileRegistry` (builtins + `SkillProfileRepository`), both implementing the `SkillProfileRegistry` Protocol.
- `builtins/` — the seven canonical `*.yaml` (package data, loaded via `importlib.resources`).
- `errors.py` — `SkillProfileNotFoundError`, `SkillProfileValidationError`, `SkillProfileError`.
- `skill-profile.schema.json` — generated from `SkillProfile.model_json_schema()`, checked in (drift guard).

**Service `apps/api/forge_api/services/skill_service.py`:** orchestrates DB + skill-sdk.

- `list_effective(workspace_id) -> list[EffectiveSkillProfile]` — merge builtins with workspace rows; tag each `source = builtin | custom | override`.
- `get_effective(workspace_id, name) -> EffectiveSkillProfile` — resolve one (custom-row → builtin → 404), include resolved `SkillDirectives`.
- `validate_yaml(raw) -> SkillProfileLintResult` — parse-only, no persistence (UI + CLI).
- `create(workspace_id, body, actor) -> SkillProfileOut` — validate `body` against `SkillProfile`; reject if name collides with an existing **custom** row (use `PUT` to edit); set `overrides_builtin` if name ∈ builtins; emit an `AuditEvent(action="skill_profile.created")`.
- `update(workspace_id, name, body, actor) -> SkillProfileOut` — upsert the workspace row; emit `AuditEvent(action="skill_profile.updated")`.
- `delete(workspace_id, name, actor)` — delete the workspace row (reverts to builtin if it was an override; 404 if no custom row exists); emit `AuditEvent(action="skill_profile.deleted")`.

Every mutating method writes one immutable row through the shared `AuditSink` (`cross-cutting/F39-audit-log`) **after** the DB commit, recording `{workspace_id, actor, action, resource="skill_profile", name}`. Read paths (`list_effective`/`get_effective`/`validate_yaml`) never write audit rows.

**Router `apps/api/forge_api/routers/skills.py`** (router pre-stubbed in Phase 0; F11 fills handlers). All routes require auth; mutating routes require `admin` (the `require_role("admin")` dependency from `cross-cutting/F37-auth-secrets-byok`):

| Method & path | Body → Response | Purpose |
|---|---|---|
| `GET /skills` | → `list[EffectiveSkillProfileOut]` | list resolvable profiles + source |
| `GET /skills/{name}` | → `EffectiveSkillProfileOut` | one profile + resolved `SkillDirectives` |
| `POST /skills/validate` | `SkillValidateRequest{yaml}` → `SkillProfileLintResult` | lint without saving |
| `POST /skills` | `SkillProfileIn` → `SkillProfileOut` | create custom profile |
| `PUT /skills/{name}` | `SkillProfileIn` → `SkillProfileOut` | create/override (upsert) |
| `DELETE /skills/{name}` | → 204 | delete custom/override |

**CLI `apps/api/forge_api/cli/skills.py`** (under `forge-cli`):
- `forge skills list` — wraps `list_effective` (uses `BuiltinSkillProfileRegistry` when no DB context).
- `forge skills show <name>` — profile + directives.
- `forge skills lint <path>` — exit 0/1, line-referenced errors (wraps `validate_skill_profile_yaml`).

### 3.3 Worker / agent runtime (Celery tasks, LangGraph, if any)

**No Celery task and no LangGraph node are added by F11.** Skill-profile resolution is synchronous, pure, and cheap (parse + dict lookup); it is called inline by the orchestrator/runtime, not enqueued. This subsection documents the **consumption contracts** F11 provides to the runtime/workflow (defined here, implemented by their own slices):

- **F02 spec engine** reads `directives.plan_required` to drive the plan sub-gate (replaces F02's `PolicyPort.plan_required` stub).
- **F06 agent runtime** reads `directives.tdd_required` (write-tests-before-impl ordering), `directives.output_type`/`directives.enabled_tools`/`directives.report_format` (specialist runs: `spec-analyst` produces a spec document, `security-review` runs SAST/dep-audit/secrets and emits SARIF), and wraps every dispatch with `skill_permits_action(directives, tool_call)` **composed** with the F04 repo-policy `Decision` (an action executes iff **both** allow).
- **F08 verification service** reads `directives.verification_steps` (mapped to repo `policy.commands`) and `directives.coverage_threshold`.
- **`cross-cutting/F36-human-approval-system` approval layer** reads `directives.review_required` (PR gate) and, for `incident-response`, `directives.approval_before_action` + `directives.max_blast_radius` (the `incident_remediation` approval gate, "Required for blast-radius > low").

### 3.4 Frontend / UI (Next.js routes/components, if any)

Route `apps/web/app/(board)/settings/skills/page.tsx` plus components in `apps/web/components/skills/`:

- `SkillProfileList` — TanStack Table of effective profiles with a `builtin | custom | override` badge.
- `SkillDirectivesPanel` — read-only render of resolved directives (plan/TDD/coverage/verification chips, review gate, allowed/forbidden action chips, output type). Reused inside the task detail "what this profile enforces" panel.
- `SkillProfileEditor` (admin) — YAML editor posting to `POST /skills/validate` (debounced) with inline `loc`+message errors, then `POST`/`PUT` to save.

Data via TanStack Query hooks (`useSkills()`, `useSkill(name)`, `useValidateSkill()`). The task-create skill picker (a small `SkillProfileSelect`) lives here too and is consumed by the board (F01).

### 3.5 Infra / deploy (compose, helm, caddy, if any)

N/A for runtime infra — no new service, container, or env var. F11 ships two repo artifacts:

- **`examples/skills/*.yaml`** — the seven canonical profiles, per OSS Strategy (*"Example skill profiles for common engineering disciplines"*). A CI test asserts each `examples/skills/<name>.yaml` is **byte-identical** to the bundled `packages/skill-sdk/forge_skills/builtins/<name>.yaml` (single source of truth; no drift).
- **`packages/skill-sdk/forge_skills/skill-profile.schema.json`** — generated from `SkillProfile.model_json_schema()`, checked in and used by the web validator/editor; a test asserts the committed file equals the regenerated schema (drift guard).

---

## 4. Public interfaces / contracts (exact signatures, Pydantic models, Protocols, YAML schemas)

**Frozen Protocol (from Phase-0 `forge_contracts`, implemented here):**

```python
from typing import Protocol
from uuid import UUID

class SkillProfileRegistry(Protocol):
    def exists(self, name: str, *, workspace_id: UUID | None = None) -> bool: ...
    def get(self, name: str, *, workspace_id: UUID | None = None) -> "SkillProfile": ...
    def list(self, *, workspace_id: UUID | None = None) -> list["SkillProfile"]: ...
    def resolve_directives(self, name: str, *, workspace_id: UUID | None = None) -> "SkillDirectives": ...
```

**Enums (`forge_contracts/skill.py`, re-exported by `forge_skills.schema`):**

```python
from enum import StrEnum

class VerificationStep(StrEnum):
    LINT = "lint"; TYPE_CHECK = "type_check"; UNIT_TESTS = "unit_tests"
    INTEGRATION_TESTS = "integration_tests"; COVERAGE = "coverage"
    ACCESSIBILITY = "accessibility"; SECRETS_SCAN = "secrets_scan"

class BlastRadius(StrEnum):
    LOW = "low"; MEDIUM = "medium"; HIGH = "high"

class OutputType(StrEnum):
    CODE = "code"; SPEC_DOCUMENT = "spec_document"; SECURITY_REPORT = "security_report"

class ReportFormat(StrEnum):
    SARIF = "sarif"; MARKDOWN = "markdown"; JSON = "json"

class SkillTool(StrEnum):
    SAST = "sast"; DEPENDENCY_AUDIT = "dependency_audit"; SECRETS_SCAN = "secrets_scan"

class ForbiddenShortcut(StrEnum):
    SKIP_TESTS = "skip_tests"; NO_ERROR_HANDLING = "no_error_handling"
    HARDCODED_SECRETS = "hardcoded_secrets"
```

**Schema (`forge_contracts/skill.py`) — the unified superset model.** Every profile in the spec is a subset of this; all behavior fields default to the *least-privilege / least-discipline* value so an omitted field is never silently stricter or laxer than the YAML states.

```python
import re
from pydantic import BaseModel, ConfigDict, Field, field_validator

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,63}$")

class SkillProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")  # typo'd keys => validation error, never silent drift
    schema_version: int = 1
    name: str
    description: str | None = None

    # --- Planning / TDD discipline ---
    requires_plan: bool = False
    requires_tests_before_implementation: bool = False
    min_test_coverage: int | None = None          # 0..100; None => no coverage gate

    # --- Verification ---
    verification_steps: list[VerificationStep] = Field(default_factory=list)
    accessibility_check: bool = False

    # --- Human gating ---
    review_required: bool = False
    human_review_required: bool = False            # spec uses this alias on specialist profiles
    requires_human_approval_before_action: bool = False
    max_blast_radius: BlastRadius | None = None

    # --- Action scope (composed with repo policy at runtime) ---
    allowed_actions: list[str] = Field(default_factory=list)   # positive allowlist when non-empty
    forbidden_actions: list[str] = Field(default_factory=list) # always-deny
    forbidden_shortcuts: list[ForbiddenShortcut] = Field(default_factory=list)

    # --- Specialist output ---
    output_type: OutputType = OutputType.CODE
    tools: list[SkillTool] = Field(default_factory=list)
    report_format: ReportFormat | None = None

    @field_validator("name")
    @classmethod
    def _slug(cls, v: str) -> str:
        if not NAME_RE.match(v):
            raise ValueError("name must be kebab-case slug ^[a-z][a-z0-9-]{1,63}$")
        return v

    @field_validator("min_test_coverage")
    @classmethod
    def _coverage_range(cls, v: int | None) -> int | None:
        if v is not None and not (0 <= v <= 100):
            raise ValueError("min_test_coverage must be between 0 and 100")
        return v
```

> `allowed_actions`/`forbidden_actions` are validated against `forge_contracts.actions.KNOWN_ACTIONS` (the union of `v1/F04-repo-policy`'s `ToolCall.name` vocabulary — including `read_repo` — and the incident/specialist actions in the spec: `read_logs`, `query_metrics`, `run_diagnostic_scripts`, `deploy_prod`, `delete_data`, `modify_access_controls`, `run_sast`, `audit_dependencies`, `write_review_comment`, `write_spec`, `write_tests`, `read_knowledge`, `search_knowledge`, `query_mcp`). Because every builtin's action lists are a subset of `KNOWN_ACTIONS`, no builtin ever emits an unknown-action warning (asserted by AC1). An **unknown** action name in a *custom* profile is a lint **warning**, not a hard error (forward-compat for custom tools).

**Normalized directives (`forge_contracts/skill.py`) — the contract every consumer reads:**

```python
class SkillDirectives(BaseModel):
    model_config = ConfigDict(frozen=True)
    profile_name: str
    plan_required: bool
    tdd_required: bool
    coverage_threshold: int | None
    verification_steps: tuple[VerificationStep, ...]   # canonicalized (see to_directives)
    review_required: bool                              # human review gate (review_required OR human_review_required)
    approval_before_action: bool
    max_blast_radius: BlastRadius | None
    allowed_actions: frozenset[str]
    forbidden_actions: frozenset[str]
    forbidden_shortcuts: frozenset[ForbiddenShortcut]
    output_type: OutputType
    enabled_tools: frozenset[SkillTool]
    report_format: ReportFormat | None
```

**Projection (`forge_skills.directives`) — pure, total, no I/O:**

```python
def to_directives(profile: SkillProfile) -> SkillDirectives:
    """Normalize a SkillProfile into machine-consumable directives.

    Canonicalization rules (deterministic):
      - review_required := profile.review_required or profile.human_review_required
      - verification_steps := profile.verification_steps, then append (dedup, stable order):
          * COVERAGE      if min_test_coverage is not None
          * ACCESSIBILITY if accessibility_check
          * SECRETS_SCAN  if ForbiddenShortcut.HARDCODED_SECRETS in forbidden_shortcuts
          * UNIT_TESTS    if ForbiddenShortcut.SKIP_TESTS in forbidden_shortcuts (tests cannot be skipped)
      - coverage_threshold := min_test_coverage
      - enabled_tools := set(profile.tools)
    """
```

**Action composition (`forge_skills.directives`):**

```python
# Maps spec-level semantic action names to canonical ToolCall.name (+ arg predicates).
ACTION_ALIASES: dict[str, frozenset[str]] = {
    "deploy_prod": frozenset({"deploy", "promote_environment"}),  # gated to prod env by runtime
    "delete_data": frozenset({"delete_file", "delete_files"}),
    "modify_access_controls": frozenset({"modify_access_controls"}),
    # read_logs / query_metrics / run_diagnostic_scripts pass through unchanged
}

def skill_permits_action(directives: SkillDirectives, action: "ToolCall") -> "Decision":
    """Skill-level scope check, independent of repo policy.
    Order:
      1. If action (or any alias) in forbidden_actions -> deny (requires_approval=True, severity=critical).
      2. If allowed_actions is non-empty and action not covered -> deny (positive allowlist;
         used by spec-analyst / incident-response).
      3. Else -> allow.
    The runtime executes an action IFF repo-policy Decision.allowed AND this Decision.allowed.
    """
```

> `ToolCall` and `Decision` are the F04 contract DTOs reused as-is; F11 adds no new action-decision type.

**Loader / registry (`forge_skills.loader`, `forge_skills.registry`):**

```python
BUILTIN_NAMES = ("backend-tdd", "backend-fast", "frontend-ui",
                 "incident-response", "spec-analyst", "security-review", "chore-fast")

def load_skill_profile(raw: str) -> SkillProfile:
    """Parse+validate one profile from YAML text. Raises SkillProfileValidationError
    on parse error or schema violation (fail-closed)."""

def load_skill_profile_file(path: Path) -> SkillProfile: ...

def validate_skill_profile_yaml(raw: str) -> "SkillProfileLintResult":
    """Parse-only; returns valid flag + errors + warnings, never raises on a schema error.
    Warnings: (a) an action in allowed_actions/forbidden_actions not in KNOWN_ACTIONS;
    (b) the empty-verification warning, emitted ONLY when output_type == CODE AND
    allowed_actions is empty (so the diagnostic/specialist builtins —
    incident-response with an allowlist, spec-analyst, security-review — never warn)."""

class BuiltinSkillProfileRegistry:
    """Pure, DB-free. Loads the seven bundled YAML via importlib.resources at import,
    validates them once, caches SkillProfile + SkillDirectives."""
    def exists(self, name, *, workspace_id=None) -> bool: ...
    def get(self, name, *, workspace_id=None) -> SkillProfile: ...     # raises SkillProfileNotFoundError
    def list(self, *, workspace_id=None) -> list[SkillProfile]: ...
    def resolve_directives(self, name, *, workspace_id=None) -> SkillDirectives: ...

class DbSkillProfileRegistry(BuiltinSkillProfileRegistry):
    """Wraps a SkillProfileRepository. Resolution order: workspace custom row by name
    -> builtin -> SkillProfileNotFoundError. list() merges (custom shadows builtin)."""
    def __init__(self, repo: "SkillProfileRepository"): ...
```

**API request/response models (`apps/api/forge_api/schemas/skills.py`):**

```python
class SkillValidateRequest(BaseModel): yaml: str
class SkillIssue(BaseModel): loc: list[str | int]; msg: str; type: str; severity: Literal["error","warning"]
class SkillProfileLintResult(BaseModel): valid: bool; errors: list[SkillIssue]; warnings: list[SkillIssue]
class SkillProfileIn(BaseModel): body: SkillProfile
class SkillProfileOut(BaseModel):
    id: str; name: str; body: SkillProfile; source: Literal["custom","override"]
    created_at: datetime; updated_at: datetime
class EffectiveSkillProfileOut(BaseModel):
    name: str; source: Literal["builtin","custom","override"]
    profile: SkillProfile; directives: SkillDirectives
```

**Canonical builtin YAML** — exactly the FORGE_SPEC.md §"Skill Profiles" block, one file per profile under `builtins/`, each gaining a `name:` field (the map key in the spec) and `schema_version: 1`. Example (`backend-tdd.yaml`):

```yaml
schema_version: 1
name: backend-tdd
description: Backend feature development with test-driven discipline
requires_plan: true
requires_tests_before_implementation: true
min_test_coverage: 80
verification_steps: [lint, type_check, unit_tests, integration_tests]
review_required: true
forbidden_shortcuts: [skip_tests, no_error_handling, hardcoded_secrets]
```

The other six (`backend-fast`, `frontend-ui`, `incident-response`, `spec-analyst`, `security-review`, `chore-fast`) reproduce their spec blocks verbatim under the same superset schema.

---

## 5. Dependencies — features/slices that must exist first

- **`v1/F00-foundation-substrate`** (Phase 0 foundation; referenced as `v1/F00-foundation-substrate` by the majority of slices — supplies `packages/contracts` declaring the `SkillProfile`/`SkillDirectives`/enum DTOs + `SkillProfileRegistry` Protocol + `KNOWN_ACTIONS`; `packages/db` base session + `Workspace` + `app_user`; `apps/api` router stubs incl. `skills.py`; web app shell). **REQUIRED** — F11 implements the frozen Protocol and DTOs declared there.
- **`cross-cutting/F37-auth-secrets-byok`** (the authoritative auth/RBAC slug — supersedes the stale `v1/F15-auth-secrets-rbac` reference). Provides the authenticated `Principal` resolution and the flat `require_role("admin")` RBAC dependency. **REQUIRED** to gate the `/skills` mutating routes (admin) and authenticate reads.
- **`cross-cutting/F39-audit-log`** (the frozen `AuditEvent` contract + `AuditSink` Protocol in `packages/contracts`, and the append-only `audit_log` writer). **REQUIRED** — the skill-profile CRUD service writes one immutable audit row per create/update/delete through `AuditSink`. The contract is in `forge_contracts`, so F11's service compiles against the Protocol even before F39's writer lands (a no-op sink is injected in unit tests).
- **`v1/F04-repo-policy`** (`packages/policy-sdk` `ToolCall`/`Decision` DTOs + `skill_profiles` cross-validation). **SOFT/PEER** — F11 reuses F04's `ToolCall`/`Decision` for `skill_permits_action` (both are Phase-0 contracts, so neither blocks the other), and F04's policy linter calls `registry.exists()` for an allowed-skill warning. The bundled `ToolCall`/`Decision` live in `forge_contracts`, so F11 builds and tests without the F04 evaluator.
- **Downstream consumers (NOT prerequisites; their slices depend on F11):** `v1/F02-spec-engine` (`directives.plan_required` replaces its stub), `v1/F06-single-execution-agent` (`tdd_required`, action scope, `output_type`/`tools`/`report_format`), `v1/F08-plan-execute-verify-pr-approval` (`verification_steps`, `coverage_threshold`, `review_required`), `cross-cutting/F36-human-approval-system` (`review_required`, `approval_before_action`, `max_blast_radius`), `v1/F01-project-board` (the `SkillProfileSelect` task-create picker), `v1/F12-eval-harness` (consumes the shared `directive_matrix`/`permits_matrix` golden fixtures). **F11 itself ships `examples/skills/*`** (§3.5) — there is no separate examples-docs slice.

---

## 6. Acceptance criteria (numbered, testable)

1. Each of the seven bundled builtins loads via `load_skill_profile_file` and validates; `BuiltinSkillProfileRegistry().list()` returns exactly the seven `BUILTIN_NAMES`.
2. `registry.get("backend-tdd").min_test_coverage == 80` and `verification_steps == [lint, type_check, unit_tests, integration_tests]` and `forbidden_shortcuts == [skip_tests, no_error_handling, hardcoded_secrets]` (verbatim spec fidelity).
3. A profile YAML with an unknown top-level key (e.g. `min_coverage:` typo) raises `SkillProfileValidationError`, and the same content through `validate_skill_profile_yaml` returns `valid=False` with an error whose `loc` points at the offending key.
4. `name` validation rejects `Backend_TDD` and `2fast` (not kebab-case slug); `min_test_coverage: 150` is rejected (range).
5. `to_directives(backend-tdd)`: `plan_required=True`, `tdd_required=True`, `coverage_threshold=80`, `review_required=True`, and `verification_steps` contains `COVERAGE` and `SECRETS_SCAN` (injected from `min_test_coverage` and the `hardcoded_secrets` shortcut) with no duplicates.
6. `to_directives(frontend-ui)`: `verification_steps` contains `ACCESSIBILITY` (injected from `accessibility_check: true`); `plan_required=True`.
7. `to_directives(chore-fast)`: `review_required=False`, `plan_required=False`, `coverage_threshold is None`.
8. `to_directives(spec-analyst)`: `review_required=True` (derived from `human_review_required`), `output_type == spec_document`, `allowed_actions == {read_repo, read_knowledge, write_spec, query_mcp}`.
9. `to_directives(security-review)`: `output_type == security_report`, `report_format == sarif`, `enabled_tools == {sast, dependency_audit, secrets_scan}`.
10. `to_directives(incident-response)`: `approval_before_action=True`, `max_blast_radius == low`, `allowed_actions` and `forbidden_actions` match the spec sets.
11. `skill_permits_action(incident_directives, ToolCall(name="deploy_prod"))` → `deny`, `requires_approval=True`, `severity="critical"`; aliasing: `ToolCall(name="deploy", args={"environment":"production"})` is also denied via `ACTION_ALIASES`.
12. `skill_permits_action(spec_analyst_directives, ToolCall(name="write_code"))` → `deny` (positive allowlist excludes it); `ToolCall(name="write_spec")` → `allow`.
13. `skill_permits_action(backend_tdd_directives, ToolCall(name="write_code"))` → `allow` (empty allowlist ⇒ no skill-level positive constraint; repo policy still applies separately).
14. `skill_permits_action` is **total**: a Hypothesis property test over random `ToolCall`s and all seven directive sets always returns a `Decision` and never raises.
15. `DbSkillProfileRegistry`: a workspace custom profile named `frontend-ui` shadows the builtin (`resolve_directives` returns the custom values, `source == override`); deleting it reverts to the builtin.
16. `DbSkillProfileRegistry.get("does-not-exist")` raises `SkillProfileNotFoundError`; `exists()` returns `False`.
17. `GET /skills` returns 200 with the seven builtins (`source="builtin"`) merged with workspace customs; an unauthenticated request → 401; a `viewer` calling `POST /skills` → 403; an `admin` create → 201.
18. `POST /skills` with a body whose `name` collides with an existing custom row → 409; `PUT /skills/{name}` upserts (override or edit) idempotently.
19. The committed `skill-profile.schema.json` equals `SkillProfile.model_json_schema()` (drift guard), and every `examples/skills/<name>.yaml` is byte-identical to `builtins/<name>.yaml`.
20. `forge skills lint examples/skills/backend-tdd.yaml` exits 0; linting a malformed file exits 1 and prints the error `loc`; `forge skills show backend-tdd` prints the resolved directives.
21. A successful `POST`/`PUT`/`DELETE /skills` each writes exactly one immutable `audit_log` row with `action ∈ {skill_profile.created, skill_profile.updated, skill_profile.deleted}`, the caller's `actor` id, the `workspace_id`, and the profile `name`; a `GET /skills` (read) writes **zero** audit rows; a rejected (422/409) write writes zero audit rows.

### Traceability — spec → criteria

| Spec element | Criteria |
|---|---|
| Seven profiles load with verbatim fields | 1, 2 |
| Strict/fail-closed schema (`extra=forbid`, slug, range) | 3, 4 |
| Principle #8 structural directives (plan/TDD/coverage/verification/review) | 5, 6, 7 |
| Specialist profiles (`output_type`/`tools`/`report_format`/allowlist) | 8, 9, 10 |
| Action scope composed with policy; incident `forbidden_actions` | 11, 12, 13, 14 |
| Workspace override precedence + resolution | 15, 16 |
| RBAC on `/skills` routes | 17, 18 |
| Immutable audit log on CRUD (non-negotiable) | 21 |
| OSS examples + drift guards | 19, 20 |

---

## 7. Test plan (TDD) — concrete test cases (unit + integration), key fixtures

Write tests first; each maps to a criterion. Layout under `packages/skill-sdk/tests/`, `apps/api/tests/`, `apps/web`.

**Unit — schema (`tests/test_schema.py`):**
- `test_builtins_all_parse` (AC1) — parametrize over `BUILTIN_NAMES`, load each file, assert validates.
- `test_backend_tdd_fields` (AC2) — exact field values.
- `test_extra_top_level_key_rejected` (AC3) — `min_coverage:` typo → `ValidationError`.
- `test_name_slug_and_coverage_range` (AC4) — bad slugs + out-of-range coverage rejected.
- `test_json_schema_matches_committed` (AC19) — `SkillProfile.model_json_schema()` == committed `skill-profile.schema.json`.

**Unit — directives (`tests/test_directives.py`):** one assertion block per profile:
- `test_backend_tdd_directives` (AC5), `test_frontend_ui_accessibility_step` (AC6), `test_chore_fast_lax` (AC7), `test_spec_analyst_output_and_allowlist` (AC8), `test_security_review_output` (AC9), `test_incident_response_gating` (AC10).
- `test_review_required_alias` — `human_review_required` alone ⇒ `directives.review_required is True`.
- `test_verification_step_dedup` — `min_test_coverage` set **and** `coverage` already listed ⇒ single `COVERAGE`.

**Unit — action composition (`tests/test_permits.py`):**
- table-driven `(directives, ToolCall) -> expected effect/severity` covering AC11–13; alias cases (`deploy_prod`, `deploy@production`).
- `test_skill_permits_action_is_total` (AC14) — Hypothesis: random `ToolCall` (random name/path/args) × seven directive sets ⇒ always a `Decision`.

**Unit — loader/validate (`tests/test_loader.py`):**
- `test_validate_reports_loc` (AC3) — malformed/extra-key YAML → `valid=False`, error `loc`.
- `test_unknown_action_is_warning` — `forbidden_actions: [frobnicate]` ⇒ `valid=True` + a warning (forward-compat).
- `test_empty_verification_warning` — a `output_type=code` profile with empty `allowed_actions` and empty `verification_steps` ⇒ warning; `test_specialist_no_empty_verification_warning` — `spec-analyst`/`incident-response` (non-code or allowlisted) ⇒ **no** empty-verification warning.

**Unit — builtin registry (`tests/test_registry_builtin.py`, AC1, AC16):** `list`/`get`/`exists`/`resolve_directives`; `get` on a bad name raises `SkillProfileNotFoundError`.

**Unit — examples drift (`tests/test_examples.py`, AC19):** assert each `examples/skills/<name>.yaml` bytes == `builtins/<name>.yaml` bytes.

**Integration — DB registry (`tests/test_registry_db.py`, Postgres test-container, AC15):**
- seed a `skill_profile` row named `frontend-ui` with `min_test_coverage: 95`; `resolve_directives` returns 95, `source=override`; delete ⇒ reverts to builtin 80.
- a custom-only name resolves; an unknown name raises.

**Integration — API (`apps/api/tests/test_skills_routes.py`, ASGI httpx + Postgres):**
- `test_list_skills_ok` (AC17) — 200, seven builtins present.
- `test_list_unauth_401` / `test_create_viewer_403` / `test_create_admin_201` (AC17).
- `test_create_duplicate_409` / `test_put_upsert_override` (AC18).
- `test_validate_endpoint_reports_loc` (AC3).
- `test_get_returns_directives` (AC5) — `GET /skills/backend-tdd` includes the resolved directives.
- `test_crud_writes_audit_rows` (AC21) — a captured/fake `AuditSink` records exactly one row per `POST`/`PUT`/`DELETE` with the right `action`+`actor`+`name`; `test_read_writes_no_audit` asserts `GET /skills` produces zero rows; `test_rejected_write_no_audit` asserts a 409 duplicate creates zero rows.

**CLI (`apps/api/tests/test_skills_cli.py`, AC20):** `forge skills lint`/`show`/`list` exit codes + output via `CliRunner`.

**Frontend (`apps/web`, Vitest + RTL):**
- `SkillDirectivesPanel` renders directive chips from a mocked query.
- `SkillProfileEditor` shows inline errors on a `valid=False` lint response.

**Key fixtures:**
- `tests/fixtures/skill_invalid_extra_key.yaml`, `skill_bad_name.yaml`, `skill_malformed.yaml`.
- `tests/fixtures/skill_custom_override.yaml` — a `frontend-ui` override with `min_test_coverage: 95`.
- `directive_matrix` — the seven `(name → expected SkillDirectives)` pairs, shared with the golden eval (`v1/F12-eval-harness`).
- `permits_matrix` — the parametrized `skill_permits_action` decision table, shared with the runtime tool-gate tests (`v1/F06-single-execution-agent`) and golden eval (`v1/F12-eval-harness`).

---

## 8. Security & policy considerations

- **Fail-closed schema.** `extra="forbid"` means a misspelled `forbidden_actions`/`min_test_coverage` is a hard validation error, never a silent loosening of a quality gate. A malformed custom profile is rejected on write (`POST`/`PUT` 422), so a broken profile can never govern a run.
- **Skill is a *floor*, never an *escalation*.** `skill_permits_action` can only **deny** an action the repo policy already allows (forbidden/allowlist); it never grants an action the policy denies. The runtime executes iff `policy.Decision.allowed AND skill.Decision.allowed`. The agent cannot use a permissive skill profile to widen repo policy (Build Prompt constraint #2).
- **Out-of-scope actions route to approval, not silent failure.** `skill_permits_action` denials set `requires_approval=True` so an out-of-scope action surfaces at the policy-override / approval gate (`cross-cutting/F36-human-approval-system`) rather than being dropped.
- **`forbidden_shortcuts` project onto concrete machine gates** (not prompt pleas): `skip_tests` ⇒ `UNIT_TESTS` forced into `verification_steps` and an empty-test-diff check (F08); `hardcoded_secrets` ⇒ `SECRETS_SCAN` forced into verification (fail on detection); `no_error_handling` ⇒ a **mandatory reviewer checklist item** in the approval UI (documented as the one shortcut that is review-enforced, not machine-enforced — see §9).
- **`incident-response` structural safety.** `approval_before_action=True` + `max_blast_radius=low` + the `forbidden_actions` set (`deploy_prod`, `delete_data`, `modify_access_controls`) are enforced as data by the approval/tool gates, matching the spec Approval Gate "Incident remediation — Required for blast-radius > low".
- **Tenant isolation.** Custom `skill_profile` rows are workspace-scoped; all reads/writes filter by the caller's workspace. Builtins are global read-only bundle data and carry no tenant data.
- **No secrets in profiles.** Skill profiles are non-secret declarative config; the schema has no credential fields. Builtins are checked into the repo and shipped as `examples/skills/*`.
- **RBAC.** Read = any authenticated workspace member; create/update/delete = `admin` (`require_role("admin")` from `cross-cutting/F37-auth-secrets-byok`). The CLI mutating verbs are admin-gated by the same service.
- **Audit log (non-negotiable).** Every custom-profile create/update/delete writes one immutable `audit_log` row via the shared `AuditSink` (`cross-cutting/F39-audit-log`), capturing actor, workspace, action, and profile name; reads are not audited. A malformed/rejected write produces no audit row (atomic: audit follows commit). Skill profiles hold no secrets, so no redaction is required, but the write still flows through the standard `SecretRedactor`-backed sink.
- **MCP stays read-only.** `query_mcp` may appear in a profile's `allowed_actions` (e.g. `spec-analyst`), but a skill profile cannot grant MCP writes: the MCP gateway (`v1/F09-mcp-gateway-v1`) is read-only by default and `skill_permits_action` can only deny, never escalate, so no profile widens the MCP surface.

---

## 9. Effort estimate & risk (S/M/L + key risks)

**Overall: M** (lower end). The `skill-sdk` core (schema + `to_directives` + `skill_permits_action` + builtins + loader/registry) is **S** and pure; the DB table + service + API + CLI + UI + drift guards add the rest. No LLM, no async, no new infra.

| Risk | Severity | Mitigation |
|---|---|---|
| Schema heterogeneity (every profile uses a different subset) modeled wrong | Medium | One superset model with least-privilege defaults; `test_builtins_all_parse` + per-profile directive tests pin each profile's exact projection |
| `review_required` vs `human_review_required` divergence (spec uses both) | Medium | Normalize in `to_directives` (`OR`); documented; AC `test_review_required_alias` |
| Action-vocabulary drift with F04 `ToolCall.name` (e.g. `deploy_prod` ≠ `deploy`) | Medium | Shared `KNOWN_ACTIONS` in contracts + explicit `ACTION_ALIASES` map; alias cases unit-tested (AC11) |
| `forbidden_shortcuts.no_error_handling` not machine-enforceable | Medium | Be honest: project it to a mandatory reviewer-checklist item (review-enforced), document in §8; `skip_tests`/`hardcoded_secrets` are machine-enforced via forced verification steps |
| Builtin vs custom resolution precedence bugs (override/revert) | Medium | `DbSkillProfileRegistry` resolution order asserted (AC15, AC16); integration test for override + delete-reverts |
| Drift between bundled builtins and `examples/skills/` | Low | Byte-identical CI assertion (AC19); single source of truth = `builtins/` |
| `extra="forbid"` rejecting forward-compat fields | Low | `schema_version` for evolution; unknown **actions** are warnings (not hard errors) so custom tooling isn't blocked |

---

## 10. Key files / paths (exact)

**Core package:**
- `packages/skill-sdk/forge_skills/__init__.py`
- `packages/skill-sdk/forge_skills/schema.py` (re-exports `forge_contracts.skill`)
- `packages/skill-sdk/forge_skills/directives.py` (`to_directives`, `skill_permits_action`, `ACTION_ALIASES`)
- `packages/skill-sdk/forge_skills/loader.py`
- `packages/skill-sdk/forge_skills/registry.py` (`BuiltinSkillProfileRegistry`, `DbSkillProfileRegistry`)
- `packages/skill-sdk/forge_skills/errors.py`
- `packages/skill-sdk/forge_skills/skill-profile.schema.json`
- `packages/skill-sdk/forge_skills/builtins/{backend-tdd,backend-fast,frontend-ui,incident-response,spec-analyst,security-review,chore-fast}.yaml`
- `packages/skill-sdk/tests/{test_schema,test_directives,test_permits,test_loader,test_registry_builtin,test_registry_db,test_examples}.py`
- `packages/skill-sdk/tests/fixtures/{skill_invalid_extra_key,skill_bad_name,skill_malformed,skill_custom_override}.yaml`

**Contracts (Phase-0 file F11 fills the skill DTOs in):**
- `packages/contracts/forge_contracts/skill.py`
- `packages/contracts/forge_contracts/actions.py` (`KNOWN_ACTIONS` superset; reused by F04)

**Data model + migration:**
- `packages/db/forge_db/models/skill_profile.py`
- `packages/db/migrations/versions/xxxx_add_skill_profile.py`

**API:**
- `apps/api/forge_api/routers/skills.py`
- `apps/api/forge_api/services/skill_service.py`
- `apps/api/forge_api/schemas/skills.py`
- `apps/api/forge_api/cli/skills.py`
- `apps/api/tests/{test_skills_routes,test_skills_cli}.py`

**Frontend:**
- `apps/web/app/(board)/settings/skills/page.tsx`
- `apps/web/components/skills/{SkillProfileList,SkillDirectivesPanel,SkillProfileEditor,SkillProfileSelect}.tsx`
- `apps/web/lib/hooks/useSkills.ts`

**Examples:**
- `examples/skills/{backend-tdd,backend-fast,frontend-ui,incident-response,spec-analyst,security-review,chore-fast}.yaml`

---

## 11. Research references (relevant links from the spec/research report)

- FORGE_SPEC.md §"Skill Profiles" — the authoritative seven-profile YAML block reproduced in §4 (the golden inputs).
- FORGE_SPEC.md §"Core Design Principles" #8 — *"Skill profiles enforce quality structurally — not via prompts."* (the slice's reason to exist).
- FORGE_SPEC.md §"Task Schema" — `skill_profile`, `allowed_actions`/`restricted_actions`, `requires_approval` (consumers of resolved directives).
- FORGE_SPEC.md §"Repo Policy System" — `skill_profiles.{default,allowed}` (F04 cross-validation via `registry.exists`).
- FORGE_SPEC.md §"Workflow Engine" → Workflow DSL — `skill: spec-analyst` on transitions; `task_ready → executing` precondition `skill_profile_set`.
- FORGE_SPEC.md §"Human Approval System" — Approval Gates "PR approval" and "Incident remediation (blast-radius > low)" backed by `review_required` / `approval_before_action` / `max_blast_radius`.
- FORGE_SPEC.md §"Multi-Agent Orchestration" → Subagent Role Definitions — the scoped-tools-per-role rationale that mirrors per-profile `allowed_actions`.
- FORGE_SPEC.md §"OSS Strategy" / "Extension Points" — *"Skill profiles: plain YAML — community contributions welcome"* (ship `examples/skills/*`).
- FORGE_SPEC.md Research Links → "Skill Profiles" — Superpowers framework (https://www.termdock.com/en/blog/superpowers-framework-agent-skills, https://mcpmarket.com/server/superpowers, https://www.verdent.ai/guides/what-is-superpowers-ai-coding-framework): the agent-skills pattern Forge's profiles are modeled on.
- forge-research-report.md §"Spec-Driven Development" — constitution/guardrails-before-work rationale that motivates structural (not prompt) enforcement of discipline.

---

## 12. Out of scope / future

- **`instructions_profile`** (Task Schema, e.g. `backend-default`) — the narrative-instruction set is a separate concept from the skill (quality) profile; resolving/injecting it is owned by the agent-runtime slice (`v1/F06-single-execution-agent`), not F11.
- **Repo-policy enforcement of `skill_profiles.allowed`** (a hard 422 when a task picks a disallowed profile) — composition lives in the board/spec validation (F01/F02) and runtime; F11 only provides `registry.exists()` and the directive resolution.
- **Skill-profile *versioning / pinning* per run** (snapshotting the exact profile body that governed an `AgentRun`, à la F04's `RepoPolicySnapshot`) — useful for audit; deferred to a future revision (the `WorkflowRun` can store the resolved `SkillDirectives` JSON in the interim).
- **LLM-driven skill selection / suggestion** ("which profile fits this task?") — V1 selection is explicit and human/board-driven; no model picks the profile.
- **`forbidden_shortcuts.no_error_handling` as a hard machine gate** — V1 surfaces it as a mandatory reviewer-checklist item only; a lint-rule/static-analysis enforcement is future.
- **Per-language verification-step → command mapping** beyond the repo `policy.commands` names — owned by F08's verification service; F11 only emits the canonical step list.
- **Marketplace / sharing of community skill profiles** (FORGE_SPEC.md Phase 3 "Integration marketplace for community MCP connectors and skill profiles") — V1 ships the seven builtins + workspace-local custom profiles only.
