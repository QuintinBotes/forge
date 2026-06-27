# F28 — Workflow Visual Editor

> Phase: v3 · Spec module(s): Workflow Engine (Workflow DSL, `WorkflowDefinition`/`TransitionRule`, `default_feature.yaml`/`incident.yaml`, guard/effect/precondition registries — all from F07/F17), Observability Layer (definition versioning + audit), Native Project Board (admin/settings surface, RBAC, SSE/ui-kit), Security (human-gate invariants are non-negotiable) · Status target: **Done** = an admin can open a visual, keyboard-navigable graph editor for any workflow definition (states = nodes, transitions = edges), fork a read-only **bundled** definition (`default_feature`, `incident`) into an editable workspace-scoped one or author a brand-new one, edit transitions by composing **only registered** guards/effects/preconditions/skills from a catalog palette, get **all** validation issues at once (server-authoritative, reusing F07's loader + registries), save immutable draft revisions, diff and roll back revisions, and **publish** — where publish is blocked unless zero ERROR issues remain and all protected human-gate invariants hold; new `WorkflowRun`s resolve the workspace's published definition (DB) over the bundled file and pin the exact revision so in-flight runs are unaffected by re-publish; every publish/rollback/archive is immutable and audited. Lint + types + `pytest` green on `packages/workflow-engine`; component/e2e green on `apps/web`.

---

## 1. Intent — what & why

F07 shipped the V1 workflow engine as a deterministic Postgres FSM driven by a YAML **Workflow DSL** (`WorkflowDefinition` → `TransitionRule[]` with named `guards`/`preconditions`/`effects`/`skill`). Definitions today are **bundled YAML files packaged in the wheel** (`forge_workflow/definitions/default_feature.yaml`; F17 adds `incident.yaml`), loaded by name, and editable only by changing source and redeploying. F07 explicitly left the door open: *"Workflow visual editor (V3) — `GET /workflow/definitions/{name}` already exposes the graph data it will consume."*

F28 is that editor. It turns the DSL from a developer-only artifact into a **governed, visual, workspace-customizable** one without weakening any guarantee:

- **Visual authoring.** States become nodes, transitions become edges on a graph canvas (React Flow). An admin composes a workflow by drawing edges and attaching guards/effects from a palette — no YAML hand-editing required, but full YAML round-trip is preserved.
- **Customization without forking the repo.** A team can fork `default_feature` to add, e.g., a mandatory `security_review` state before `pr_opened`, or author an entirely new definition, scoped to their workspace — persisted in the DB, resolved at run time over the bundled file.
- **Safety is structural, not cosmetic.** The editor can only reference **registered** guards/effects/preconditions (you cannot invent agent behavior from the UI; new predicates are still Python in F07's registries). Server-side validation reuses F07's `load_definition` semantics, and **protected invariants** make it impossible to publish a feature-class workflow that removes the human merge-approval gate or the spec-approval gate — directly enforcing the non-negotiables ("Human approval is required before PR merge — always"; "No implementation run without an approved spec for feature-class work").
- **Versioned + audited.** Every save is an immutable revision; publish/rollback/archive are attributable audit events; in-flight runs pin their resolved revision so a re-publish never mutates a workflow a human is mid-approval on.

This slice **owns**: the graph model + DSL round-trip, the multi-issue validator + protected-invariant engine, the registry catalog (palette), the DB definition store + revision lifecycle, the resolver that lets DB-published definitions override bundled ones, the `/workflow/editor/*` API, and the editor UI. It **reuses** F07's DSL types, registries, and engine wholesale and does **not** re-implement transition execution.

---

## 2. User-facing behavior / journeys

1. **Open the editor.** An admin goes to **Settings → Workflows** (`/[workspace]/settings/workflows`). A list shows every definition resolvable in the workspace: bundled (`default_feature`, `incident`) tagged **Bundled · read-only**, plus any custom/forked ones with their published revision number, draft badge, and validation state.
2. **View a bundled definition.** Clicking `default_feature` opens the canvas in read-only mode: 18 state nodes laid out automatically (dagre), edges labeled by event, human-gate states (`spec_review`, `plan_review`, `awaiting_review`, `needs_human_input`) badged, terminal states (`closed`/`failed`/`cancelled`) styled. A banner offers **Fork to customize**.
3. **Fork.** Admin clicks **Fork** → a workspace-scoped editable definition (`source=bundled_fork`, `base_bundled_name=default_feature`) is created with revision 1 as a **draft** copying the bundled graph + a fresh auto-layout. Editing is now enabled.
4. **Edit a transition.** Selecting an edge opens the **Edge Inspector**: pick the event (`on`) from a dropdown of known events, add guards/preconditions/effects from catalog pickers (each shows its description and whether it takes an arg, e.g. `approval_granted:<kind>`), set an optional `skill`, `record` label, and `priority`. Drawing a new edge from node A to node B creates a transition stub the inspector then fills. Adding a node prompts for a state name (existing enum value or a new custom state string).
5. **Validate.** Clicking **Validate** (or auto-on-save) runs server-side validation and renders the **Validation Panel**: a list of every issue (errors + warnings) — unknown state, unregistered guard/effect, unreachable state, dead-end non-terminal state, nondeterministic same-`(from,on,priority)` rules, unknown skill, and **protected-invariant violations**. Clicking an issue focuses the offending node/edge on the canvas.
6. **Save draft.** **Save** persists the current graph (nodes + edges + layout positions) as the single working draft revision with an optional note. Saves are cheap and frequent; only one draft exists at a time.
7. **Publish.** **Publish** is enabled only when the draft has **zero ERROR issues**. On publish, the draft becomes an immutable published revision, `current_published_version` advances, and from then on new `WorkflowRun`s using that definition name in this workspace resolve to it. If errors remain, Publish is disabled with a tooltip listing blockers; the API rejects a forced publish with 409.
8. **Diff & roll back.** **History** lists all revisions (draft + published) with author, timestamp, note, and validation state. Selecting two shows a side-by-side **YAML diff** (added/removed/changed transitions highlighted). **Roll back to rev N** creates a *new* draft from rev N's content (never mutates history); the admin reviews and publishes it.
9. **Import / export.** **Export** downloads the published (or selected) revision as canonical YAML. **Import** accepts a YAML file, parses it structurally, runs full validation, and stages it as a new draft — references to unregistered guards/effects surface as errors (you cannot smuggle in new behavior via YAML).
10. **Run-time effect (observable indirectly).** After publishing a forked `default_feature` that inserts a `security_review` state, the next feature workflow started in that workspace stops at `security_review`; a workflow started in a workspace that did **not** fork still uses the bundled definition. A workflow already running keeps its pinned revision.

---

## 3. Vertical slice

### 3.1 Data model (tables/columns/migrations touched)

New Alembic migration at `packages/db/migrations/versions/xxxx_f28_workflow_editor.py` (stacks on F07's `xxxx_f07_workflow_fsm` and the foundation baseline; same chain/location convention F07 uses). ORM models live in `packages/db/forge_db/models/workflow_editor.py` (the two new tables), subclassing `packages/db`'s shared `Base`/`TimestampMixin` and sitting alongside F07's `workflow_run` model — this keeps **all** ORM in `packages/db` so the single Alembic env's `target_metadata` sees them (the F07/F11 convention); the `forge_workflow/editor/` subpackage imports them. Two new tables + one additive column on F07's `workflow_run`. Status/source/validation enums are modeled as `VARCHAR` + `CHECK` constraints (matching F07's "CHECK constraint on the 18 values" pattern), **not** native Postgres `ENUM` types.

**Naming clarification (critical):** the F07 DSL's `WorkflowDefinition.version` is an author-set **semantic** string (`"1"`). F28's DB **revision** is a monotonic integer edit counter. To avoid collision, the DB column is `revision` (int) and the DTOs are `Revision*`; the UI labels these "versions" for users.

**`workflow_definitions`** (the named, workspace-scoped, editable definition):

| Column | Type | Notes |
|---|---|---|
| `id` | `UUID` PK | |
| `workspace_id` | `UUID` FK→`workspaces.id` CASCADE NOT NULL | indexed; tenant scoping |
| `name` | `VARCHAR(64)` NOT NULL | e.g. `default_feature`, `incident`, `release_train` |
| `title` | `VARCHAR(160)` NOT NULL | human label |
| `description` | `TEXT` NULL | |
| `source` | `VARCHAR(16)` NOT NULL | enum `WorkflowDefinitionSource`: `bundled_fork` \| `custom` |
| `base_bundled_name` | `VARCHAR(64)` NULL | set when `source='bundled_fork'` (`default_feature`/`incident`) |
| `current_published_revision_id` | `UUID` NULL FK→`workflow_definition_revisions.id` SET NULL | the live revision |
| `draft_revision_id` | `UUID` NULL FK→`workflow_definition_revisions.id` SET NULL | the single working draft (or NULL) |
| `is_active` | `BOOLEAN` NOT NULL default `true` | `false` = archived/disabled (resolver ignores it) |
| `created_by` | `UUID` FK→`users.id` | |
| `created_at`/`updated_at` | `TIMESTAMPTZ` | |

Constraint: `uq_workflow_definitions_workspace_name` UNIQUE(`workspace_id`,`name`) — at most one DB definition per name per workspace; a present-and-active row **overrides** the bundled file at resolution time.

**`workflow_definition_revisions`** (immutable snapshots; append-only except the `draft → published/archived` status flip + validation fields on the draft):

| Column | Type | Notes |
|---|---|---|
| `id` | `UUID` PK | |
| `workflow_definition_id` | `UUID` FK→`workflow_definitions.id` CASCADE NOT NULL | indexed |
| `workspace_id` | `UUID` NOT NULL | denormalized for RBAC scoping |
| `revision` | `INTEGER` NOT NULL | monotonic per definition, starts at 1 |
| `status` | `VARCHAR(16)` NOT NULL | enum `RevisionStatus`: `draft` \| `published` \| `archived` |
| `dsl_yaml` | `TEXT` NOT NULL | canonical YAML (the engine's source of truth for this revision) |
| `graph_json` | `JSONB` NOT NULL | `WorkflowGraph` (nodes+edges+**layout positions**) for canvas round-trip |
| `dsl_version` | `VARCHAR(16)` NOT NULL | the semantic DSL version string copied from the graph (`"1"`) |
| `validation_status` | `VARCHAR(16)` NOT NULL default `unvalidated` | `valid` \| `invalid` \| `unvalidated` |
| `validation_issues` | `JSONB` NOT NULL default `[]` | last `ValidationIssue[]` |
| `notes` | `TEXT` NULL | changelog/commit message |
| `created_by` | `UUID` FK→`users.id` | attribution (immutable) |
| `created_at` | `TIMESTAMPTZ` NOT NULL | |
| `published_at` | `TIMESTAMPTZ` NULL | set on publish |

Constraints: `uq_wf_def_rev_revision` UNIQUE(`workflow_definition_id`,`revision`); partial `uq_wf_def_one_draft` UNIQUE(`workflow_definition_id`) WHERE `status='draft'` — at most one draft per definition. Repository enforces append-only (no UPDATE/DELETE of `dsl_yaml`/`graph_json`/`created_*` after creation; only the draft's `status`/`validation_*`/`published_at` may change exactly once on publish).

**Additive change to F07 `workflow_run`** (run-time revision pinning):
- ADD COLUMN `definition_revision_id` `UUID` NULL FK→`workflow_definition_revisions.id` SET NULL — set when a run resolved to a DB definition; NULL means the bundled file was used. F07's existing `definition_version` (VARCHAR) keeps the semantic string and is set to the resolved `dsl_version` (or `"bundled"`). The F07 `WorkflowEngine.start` path (see §3.2 coordinated change) populates this from the resolver. In-flight runs read their pinned revision, never the latest published one.

Migration is reversible (`downgrade` drops both tables and the `workflow_run.definition_revision_id` column + its FK; there are no native Postgres `ENUM` types to drop because status/source/validation are `VARCHAR`+`CHECK`). Unit tests use SQLite (`JSON` for `graph_json`/`validation_issues`); partial unique indexes are verified on the Postgres test container.

### 3.2 Backend (FastAPI routes + services/packages)

Package: extend `packages/workflow-engine/forge_workflow/` with an `editor/` subpackage. The API router fills a new module in `apps/api`.

```
packages/workflow-engine/forge_workflow/editor/
├── __init__.py
├── graph.py          # WorkflowGraph, StateNode, TransitionEdge, NodeLayout; graph<->definition<->yaml round-trip + auto_layout()
├── validation.py     # collect_validation_issues(), ValidationIssue, Severity, IssueCode, ProtectedInvariant, FEATURE_INVARIANTS
├── catalog.py        # RegistryCatalog -> CatalogResponse (palette: states/events/guards/preconditions/effects/skills/modes)
├── store.py          # WorkflowDefinitionStore Protocol; DbWorkflowDefinitionStore; ResolvingDefinitionProvider (DB over bundled)
├── repository.py     # WorkflowDefinitionRepository (SQLAlchemy; revision lifecycle, append-only enforcement)
├── service.py        # WorkflowEditorService (fork/create/save-draft/validate/publish/diff/rollback/archive)
├── diff.py           # definition_diff(from_yaml, to_yaml) -> DefinitionDiff
├── schemas.py        # DTOs (DefinitionSummary/Detail, RevisionSummary/Detail, SaveDraftRequest, DefinitionDiff, ...)
└── errors.py         # PublishBlockedError, BundledReadOnlyError, DefinitionNotFoundError, DefinitionNameConflictError, RevisionNotFoundError
```

The two ORM models (`WorkflowDefinition`, `WorkflowDefinitionRevision`) do **not** live in this subpackage — they live in `packages/db/forge_db/models/workflow_editor.py` (alongside F07's `workflow_run`), per the F07/F11 convention that all ORM is in `packages/db`; `repository.py` imports them from `forge_db.models.workflow_editor`.

**Coordinated changes to F07 (all backward-compatible widenings):** F07's engine resolves definitions by name from bundled files via its internal loader. F28 inserts a `ResolvingDefinitionProvider` between the engine and the loader, plus two small registry/loader widenings:
- `forge_workflow/engine.py` `PostgresWorkflowEngine` gains an injected `definition_provider: ResolvingDefinitionProvider` (default = bundled-only, preserving F07 behavior). The Protocol/concrete `start` widens from F07's `start(self, task_id, definition_name="default_feature") -> WorkflowRunDTO` to `start(self, task_id, definition_name="default_feature", *, workspace_id: UUID | None = None)`; when `workspace_id` is omitted the engine derives it from the task's workspace, so F07's existing call sites and tests are unchanged. `start` calls `provider.resolve(name, workspace_id=...)` which returns `(WorkflowDefinition, revision_id | None, dsl_version)`; the engine stores `definition_revision_id` + `definition_version` on the run. `transition(...)` loads the run's **pinned** revision (by `definition_revision_id`, via `provider.load_pinned`) so execution never drifts to a newer publish. With the default bundled-only provider, F07's tests are unchanged.
- **Registry metadata + factory bundle.** F07's `GuardRegistry`/`EffectRegistry` gain optional metadata on `register()` (see §4 catalog) including an `is_precondition: bool` flag — F07 registers **preconditions and guards in the same `GuardRegistry`** (both are `Guard = Callable[[GuardContext, str|None], bool]`; the DSL distinguishes them only by position), so the catalog partitions them by `is_precondition` rather than by a second registry. F07's `default_registries()` factory (already used by F07's `load_definition` call sites and F17's `default_incident_registries()`) returns the `Registries` bundle (`.guards: GuardRegistry`, `.effects: EffectRegistry`) F28 threads through the provider, catalog, and validator. No `load_definition` signature change is needed — it stays `load_definition(source, *, guard_registry, effect_registry)` and validates precondition names against `guard_registry`.

Router `apps/api/forge_api/routers/workflow_editor.py`, prefix `/api/v1/workflow/editor`, all routes auth-required (principal from `deps.get_principal`); RBAC (see §8): **read = viewer+**, **draft/validate = member+**, **publish/fork/rollback/archive/import = admin only**.

| Method & path | Handler | RBAC | Returns |
|---|---|---|---|
| `GET /workflow/editor/catalog` | `get_catalog` | viewer+ | `CatalogResponse` |
| `GET /workflow/editor/definitions` | `list_definitions` | viewer+ | `list[DefinitionSummary]` (bundled + custom) |
| `GET /workflow/editor/definitions/{name}` | `get_definition` | viewer+ | `DefinitionDetail` |
| `POST /workflow/editor/definitions` | `create_definition(CreateDefinition)` | admin | `DefinitionDetail` 201 |
| `POST /workflow/editor/definitions/{name}/fork` | `fork_bundled` | admin | `DefinitionDetail` 201 |
| `PUT /workflow/editor/definitions/{name}/draft` | `save_draft(SaveDraftRequest)` | member+ | `RevisionDetail` |
| `POST /workflow/editor/definitions/{name}/draft/validate` | `validate_draft` | member+ | `list[ValidationIssue]` |
| `POST /workflow/editor/definitions/{name}/publish` | `publish` | admin | `RevisionDetail` (409 if ERRORs) |
| `GET /workflow/editor/definitions/{name}/revisions` | `list_revisions` | viewer+ | `list[RevisionSummary]` |
| `GET /workflow/editor/definitions/{name}/revisions/{revision}` | `get_revision` | viewer+ | `RevisionDetail` |
| `GET /workflow/editor/definitions/{name}/diff?from=&to=` | `diff_revisions` | viewer+ | `DefinitionDiff` |
| `POST /workflow/editor/definitions/{name}/rollback` | `rollback({to_revision})` | admin | `RevisionDetail` (new draft) |
| `POST /workflow/editor/definitions/{name}/archive` | `archive` | admin | 204 |
| `GET /workflow/editor/definitions/{name}/export?format=yaml&revision=` | `export` | viewer+ | `text/yaml` |
| `POST /workflow/editor/import` | `import_yaml(ImportRequest)` | admin | `DefinitionDetail` 201 (draft) |

Error mapping (FastAPI exception handlers): `PublishBlockedError`→409 `{detail, errors: ValidationIssue[]}`; `BundledReadOnlyError`→409 (cannot edit a bundled def without forking); `DefinitionNotFoundError`/cross-workspace→404; `DefinitionNameConflictError`→409; `RevisionNotFoundError`→404. Validation requests never raise on issues — they return the issue list (publish is the gate).

`WorkflowEditorService` is the single entry point the router calls; it takes an injected `WorkflowDefinitionRepository`, a `RegistryCatalog` (built from F07/F17 registries), the bundled loader, and `FEATURE_INVARIANTS`. It performs no I/O outside the repo and is unit-testable with a fake repo.

### 3.3 Worker / agent runtime (Celery tasks, LangGraph, if any)

**N/A — no Celery tasks or LangGraph.** F28 is a synchronous authoring/governance surface: validation and YAML round-trip are pure CPU and run inline in the request (validating a graph of ~20 states is microseconds–milliseconds). There is no agent loop here. The only run-time coupling is the resolver (§3.2) used by the F07 engine when a *run* starts; F28 does not start runs. No LangGraph — routing remains F07's deterministic FSM; the agent graph is F06's and untouched.

### 3.4 Frontend / UI (Next.js routes/components, if any)

App `apps/web` (App Router, TypeScript, Tailwind, shadcn/ui, TanStack Query). Graph rendering uses **`@xyflow/react` (React Flow)**; auto-layout uses **`dagre`**; YAML view/diff uses a lightweight Monaco editor in read-only mode (or `react-diff-viewer` for the diff). Shared graph primitives go in `packages/ui-kit/src/workflow-editor/`.

Routes:
- `app/[workspace]/settings/workflows/page.tsx` — definition list (`DefinitionList`, `CreateDefinitionDialog`).
- `app/[workspace]/settings/workflows/[name]/page.tsx` — the editor canvas (read-only for bundled until forked).

Components (`apps/web/components/workflow-editor/`): `WorkflowCanvas.tsx` (React Flow wrapper, fit-view, minimap, controls), `StateNode.tsx` (custom node: state label + initial/terminal/human-gate badges), `TransitionEdge.tsx` (custom labeled edge; tooltip lists guards/effects), `NodePalette.tsx` (catalog-driven palette: drag states/events, browse guards/effects/preconditions/skills with descriptions), `EdgeInspector.tsx` + `NodeInspector.tsx` (side panel to edit the selected element via catalog pickers), `EditorToolbar.tsx` (Save / Validate / Publish / Export / History; Publish disabled when ERRORs exist with a tooltip listing them), `ValidationPanel.tsx` (issue list; click focuses + highlights the node/edge), `YamlView.tsx` (read-only canonical YAML), `RevisionHistoryDrawer.tsx` + `YamlDiffView.tsx`, `ForkDialog.tsx`, `RollbackDialog.tsx`, `BundledReadOnlyBanner.tsx`, `EmptyState.tsx`.

Data/state: `apps/web/lib/workflow-editor/{api,queries,mutations,layout,types}.ts`. `layout.ts` runs dagre to seed positions when a graph has none; positions then persist in `graph_json`. Mutations are explicit (Save draft is a deliberate action, not optimistic) but the canvas is locally responsive (client-side graph state, server is the authority on validation/publish). Keyboard-first (board UX standard): `S` save, `V` validate, `Cmd/Ctrl+Enter` publish (admin), `Backspace`/`Del` remove selected edge/node, `A` add state, `H` history, `/` focus palette search, arrow keys pan, `+`/`-` zoom; nodes/edges are tab-focusable and operable without a mouse.

The editor renders **static authoring** graphs only. Overlaying a *live run's* current position on the canvas is deferred to F10 (run-trace) reuse — see §12.

### 3.5 Infra / deploy (compose, helm, caddy, if any)

No new services. Reuses `db` (Postgres+pgvector), `api`, `web` from C01. Requirements:
- The `editor/` subpackage adds no new bundled assets; it **reads** F07's/F17's packaged `definitions/*.yaml` via the bundled loader, so no new package-data entries beyond F07's existing include.
- New frontend deps `@xyflow/react` and `dagre` (and the chosen YAML viewer) are added to `apps/web/package.json` — bundled in the existing `web` image, no infra change.
- Migration `xxxx_f28_workflow_editor` runs via the existing `forge-cli db migrate`.
- No Caddy change (these are ordinary JSON routes; no SSE required for V1 of the editor — validation is synchronous).

---

## 4. Public interfaces / contracts (exact signatures, Pydantic models, Protocols, YAML schemas)

All types live in `packages/workflow-engine/forge_workflow/editor/`. They build on F07's `WorkflowDefinition`, `TransitionRule`, `RetryPolicy`, `EscalationPolicy`, `WorkflowState`, `WorkflowEventType`, `GuardRegistry`, `EffectRegistry`, `DSLValidationError`, and `load_definition` (imported, not re-declared).

```python
# forge_workflow/editor/graph.py  (Pydantic v2)
from typing import Literal
from pydantic import BaseModel, Field
from forge_workflow.dsl import WorkflowDefinition, TransitionRule, RetryPolicy, EscalationPolicy
from forge_workflow.states import WorkflowState, TERMINAL_STATES

# F07 exports HUMAN_GATE_EVENTS (a set of *events*), not states. F28 defines the
# human-gate *state* set used for node-kind derivation; it is the set of from-states
# of F07's HUMAN_GATE_EVENTS and is asserted equal to it in test_graph.py.
HUMAN_GATE_STATES: frozenset[WorkflowState] = frozenset({
    WorkflowState.spec_review, WorkflowState.plan_review,
    WorkflowState.awaiting_review, WorkflowState.needs_human_input,
})

class NodeLayout(BaseModel):
    x: float
    y: float

class StateNode(BaseModel):
    id: str                                   # WorkflowState value OR a new custom state string
    label: str | None = None
    kind: Literal["normal", "initial", "terminal", "human_gate"] = "normal"
    layout: NodeLayout

class TransitionEdge(BaseModel):
    id: str                                   # stable client id (uuid4 hex); not persisted into DSL
    from_state: str
    to_state: str
    on: str                                   # WorkflowEventType value
    guards: list[str] = []                    # "name" | "name:arg"
    preconditions: list[str] = []
    effects: list[str] = []
    skill: str | None = None
    record: str | None = None
    priority: int = 0

class WorkflowGraph(BaseModel):
    name: str
    dsl_version: str = "1"                     # the SEMANTIC DSL version (F07 WorkflowDefinition.version)
    title: str
    description: str | None = None
    default_mode: str = "single_agent"
    optional_modes: list[str] = []
    retry_policy: RetryPolicy = RetryPolicy()
    escalation_policy: EscalationPolicy = EscalationPolicy()
    nodes: list[StateNode]
    edges: list[TransitionEdge]

def graph_to_definition(graph: WorkflowGraph) -> WorkflowDefinition:
    """Map edges -> TransitionRule[] (from->from_state, on, to, guards, preconditions, effects, skill,
    record, priority); carry name/version/modes/policies. Pure; no registry needed (structural only).
    Layout/edge-ids are dropped (UI-only)."""

def definition_to_graph(defn: WorkflowDefinition, *,
                        title: str, description: str | None = None,
                        layout: dict[str, NodeLayout] | None = None) -> WorkflowGraph:
    """Inverse: one StateNode per distinct from/to state; one TransitionEdge per rule.
    Node.kind derived: terminal if in TERMINAL_STATES, human_gate if state in HUMAN_GATE_STATES,
    initial if it has no inbound edges (or is `created`). Uses `layout` if given else auto_layout()."""

def auto_layout(nodes: list[StateNode], edges: list[TransitionEdge]) -> dict[str, NodeLayout]:
    """Deterministic layered layout (server-side fallback; client also runs dagre). Same input -> same output."""

def graph_to_yaml(graph: WorkflowGraph) -> str:
    """Canonical YAML in F07's transition form (keys sorted, `from:` alias). Round-trips with yaml_to_graph."""

def yaml_to_graph(yaml_text: str, *, title: str | None = None) -> WorkflowGraph:
    """Structural parse only (no registry check). Raises DSLValidationError on malformed YAML/shape."""
```

```python
# forge_workflow/editor/validation.py
from enum import StrEnum

class Severity(StrEnum):
    ERROR = "error"; WARNING = "warning"

class IssueCode(StrEnum):
    UNKNOWN_STATE = "unknown_state"
    UNKNOWN_EVENT = "unknown_event"
    UNREGISTERED_GUARD = "unregistered_guard"
    UNREGISTERED_PRECONDITION = "unregistered_precondition"
    UNREGISTERED_EFFECT = "unregistered_effect"
    UNKNOWN_SKILL = "unknown_skill"
    NO_INITIAL_STATE = "no_initial_state"
    UNREACHABLE_STATE = "unreachable_state"
    DEAD_END_STATE = "dead_end_state"               # non-terminal, no outgoing rule (except needs_human_input)
    NONDETERMINISTIC_RULES = "nondeterministic_rules"
    DUPLICATE_EDGE = "duplicate_edge"
    PROTECTED_INVARIANT_VIOLATION = "protected_invariant_violation"

class ValidationIssue(BaseModel):
    code: IssueCode
    severity: Severity
    message: str
    node_id: str | None = None
    edge_id: str | None = None
    invariant_id: str | None = None

class ProtectedInvariant(BaseModel):
    """An edge selector + a guard that must be present on every matching edge.
    NOTE: this is deliberately NOT phrased as "edge into a terminal state" — the merge gate
    guards the awaiting_review --review_approved--> merged edge, and `merged` is NOT terminal
    in F07 (TERMINAL_STATES = {closed, failed, cancelled})."""
    id: str
    description: str
    applies_when_base_in: list[str]      # bundled bases this guards, e.g. ["default_feature"]
    from_state: str                      # selector: the edge's source state
    on_event: str | None = None          # optional selector: WorkflowEventType value
    to_state: str | None = None          # optional selector: target state
    required_guard: str                  # must appear in `guards` of every matching edge

# Non-negotiable invariants for feature-class workflows (spec: "human approval before merge — always";
# "no implementation run without an approved spec for feature-class work"):
FEATURE_INVARIANTS: list[ProtectedInvariant] = [
    ProtectedInvariant(
        id="merge_human_gate",
        description="Merge must require human approval: the awaiting_review --review_approved--> merged "
                    "edge must carry the merge_ready guard.",
        applies_when_base_in=["default_feature"],
        from_state="awaiting_review", on_event="review_approved", to_state="merged",
        required_guard="merge_ready"),
    ProtectedInvariant(
        id="spec_gate",
        description="Leaving spec_review on spec_approved must require approval_granted:spec.",
        applies_when_base_in=["default_feature"],
        from_state="spec_review", on_event="spec_approved",
        required_guard="approval_granted:spec"),
]
# Invariant semantics (enforced in collect_validation_issues): for each invariant whose
# `applies_when_base_in` contains `base_bundled_name`, the graph is valid iff (a) AT LEAST ONE
# edge matches the (from_state[, on_event][, to_state]) selector AND (b) EVERY matching edge's
# `guards` contains `required_guard`. Deleting the edge violates (a); stripping the guard
# violates (b); both raise PROTECTED_INVARIANT_VIOLATION (Severity.ERROR) carrying `invariant_id`.

def collect_validation_issues(
    graph: WorkflowGraph, *,
    guard_registry: GuardRegistry,
    effect_registry: EffectRegistry,
    skill_names: set[str],
    base_bundled_name: str | None,
    invariants: list[ProtectedInvariant] | None = None,
) -> list[ValidationIssue]:
    """Run ALL checks and return EVERY issue (does not fail-fast). Mirrors F07's DSL validator
    rules as collected ERRORs, plus protected-invariant ERRORs and reachability WARNINGs.
    Precondition names are validated against `guard_registry` (F07 holds preconditions and guards
    in the same registry). A graph is publishable iff this returns zero Severity.ERROR issues.

    Severity mapping (kept in lockstep with F07's `load_definition`, asserted by the AC-5 parity
    test): UNKNOWN_STATE, UNKNOWN_EVENT, UNREGISTERED_GUARD, UNREGISTERED_PRECONDITION,
    UNREGISTERED_EFFECT, NO_INITIAL_STATE, DEAD_END_STATE, NONDETERMINISTIC_RULES, DUPLICATE_EDGE,
    PROTECTED_INVARIANT_VIOLATION are ERROR (each maps to a condition that makes load_definition
    raise, or a publish-blocking gate); UNKNOWN_SKILL and UNREACHABLE_STATE are WARNING (F07 does
    not check these, so they must not block parity)."""
```

```python
# forge_workflow/editor/catalog.py
class GuardMeta(BaseModel):
    name: str
    description: str
    takes_arg: bool = False
    arg_hint: str | None = None                 # e.g. "kind: spec|plan|pr"
    is_precondition: bool = False               # True -> surfaced under CatalogResponse.preconditions

class EffectMeta(BaseModel):
    name: str
    description: str
    provided_by: str | None = None              # slice that owns the effect body (e.g. "spec-engine")

class CatalogResponse(BaseModel):
    states: list[str]                           # WorkflowState values + custom states used by this workspace's defs
    events: list[str]                           # WorkflowEventType values
    guards: list[GuardMeta]                     # guard_registry items where is_precondition == False
    preconditions: list[GuardMeta]              # guard_registry items where is_precondition == True
    effects: list[EffectMeta]
    skills: list[str]                           # skill-profile names (from F11 SkillProfileRegistry, workspace-scoped)
    modes: list[str]                            # ["single_agent","supervised_multi_agent"]

class RegistryCatalog:
    def __init__(self, *, guard_registry: GuardRegistry, effect_registry: EffectRegistry,
                 skill_names_provider: Callable[[UUID | None], list[str]]) -> None: ...
    def build(self, *, workspace_id: UUID | None = None) -> CatalogResponse: ...
    # Partitions guard_registry.items() into guards/preconditions by GuardMeta.is_precondition.
    # `skill_names_provider(workspace_id)` returns F11 builtins + workspace-custom (empty if F11 absent).
# Requires the F07 coordinated metadata widening: GuardRegistry/EffectRegistry gain optional metadata at
# register() (register(name, fn, *, description="", takes_arg=False, arg_hint=None, is_precondition=False,
# provided_by=None)) plus `items() -> list[GuardMeta|EffectMeta]`. Backward compatible (metadata defaults empty).
# There is NO separate precondition registry: preconditions live in guard_registry, tagged is_precondition.
```

```python
# forge_workflow/editor/store.py
from typing import Protocol

class WorkflowDefinitionStore(Protocol):
    def resolve_published(self, name: str, *, workspace_id: UUID
                          ) -> tuple[WorkflowDefinition, UUID, str] | None: ...
    # -> (definition, revision_id, dsl_version) for an ACTIVE workspace definition with a published
    #    revision; None to fall back to bundled.

class ResolvingDefinitionProvider:
    """Injected into the F07 engine. DB-published definition overrides the bundled file.
    `registries` is F07's `default_registries()` bundle (.guards: GuardRegistry,
    .effects: EffectRegistry); the provider parses persisted YAML via
    `load_definition(yaml, guard_registry=registries.guards, effect_registry=registries.effects)`."""
    def __init__(self, store: WorkflowDefinitionStore, bundled_loader: "BundledLoader",
                 registries: "Registries") -> None: ...
    def resolve(self, name: str, *, workspace_id: UUID
                ) -> tuple[WorkflowDefinition, UUID | None, str]:
        """Try store.resolve_published(name, workspace_id); else bundled_loader.load(name) with
        revision_id=None and dsl_version=defn.version. Raises UnknownDefinitionError if neither exists."""
    def load_pinned(self, revision_id: UUID) -> WorkflowDefinition:
        """Load+parse a specific persisted revision (used by transition() so in-flight runs don't drift)."""
```

```python
# forge_workflow/editor/schemas.py  (API DTOs)
class RevisionSummary(BaseModel):
    id: UUID; revision: int; status: Literal["draft","published","archived"]
    validation_status: Literal["valid","invalid","unvalidated"]
    error_count: int; warning_count: int
    notes: str | None; created_by: UUID; created_at: datetime; published_at: datetime | None

class RevisionDetail(RevisionSummary):
    graph: WorkflowGraph
    dsl_yaml: str
    validation_issues: list[ValidationIssue]

class DefinitionSummary(BaseModel):
    name: str; title: str; description: str | None
    origin: Literal["bundled","bundled_fork","custom"]   # bundled = read-only file, no DB row
    base_bundled_name: str | None
    is_active: bool
    published_revision: int | None
    has_draft: bool

class DefinitionDetail(DefinitionSummary):
    editable: bool                                # false for pure bundled (must fork first)
    current_published: RevisionDetail | None
    draft: RevisionDetail | None

class CreateDefinition(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{1,62}$")
    title: str = Field(min_length=1, max_length=160)
    description: str | None = None
    graph: WorkflowGraph | None = None           # if omitted, seeds an empty `created`->... starter

class SaveDraftRequest(BaseModel):
    graph: WorkflowGraph
    notes: str | None = None

class ImportRequest(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{1,62}$")
    title: str = Field(min_length=1, max_length=160)
    dsl_yaml: str

class TransitionDiff(BaseModel):
    change: Literal["added","removed","changed"]
    from_state: str; on: str; to: str
    before: TransitionEdge | None = None
    after: TransitionEdge | None = None

class DefinitionDiff(BaseModel):
    name: str
    from_revision: int; to_revision: int
    transition_diffs: list[TransitionDiff]
    states_added: list[str]; states_removed: list[str]
    policy_changed: bool
```

```python
# forge_workflow/editor/service.py
class WorkflowEditorService:
    def __init__(self, repo: WorkflowDefinitionRepository, catalog: RegistryCatalog,
                 bundled_loader: "BundledLoader", registries: "Registries",
                 invariants: list[ProtectedInvariant], audit: "AuditSink") -> None: ...
    # `registries` (F07 default_registries() bundle) is used for collect_validation_issues + the
    # belt-and-braces load_definition; `audit` is F39's SqlAuditWriter for publish/rollback/archive.
    def catalog(self, workspace_id: UUID) -> CatalogResponse: ...
    def list_definitions(self, workspace_id: UUID) -> list[DefinitionSummary]: ...        # merges bundled + DB
    def get_definition(self, workspace_id: UUID, name: str) -> DefinitionDetail: ...
    def fork_bundled(self, workspace_id: UUID, bundled_name: str, *, actor: UUID) -> DefinitionDetail: ...
    def create_definition(self, workspace_id: UUID, body: CreateDefinition, *, actor: UUID) -> DefinitionDetail: ...
    def save_draft(self, workspace_id: UUID, name: str, req: SaveDraftRequest, *, actor: UUID) -> RevisionDetail: ...
    def validate_draft(self, workspace_id: UUID, name: str) -> list[ValidationIssue]: ...
    def publish(self, workspace_id: UUID, name: str, *, actor: UUID) -> RevisionDetail: ...  # PublishBlockedError if ERRORs
    def list_revisions(self, workspace_id: UUID, name: str) -> list[RevisionSummary]: ...
    def get_revision(self, workspace_id: UUID, name: str, revision: int) -> RevisionDetail: ...
    def diff_revisions(self, workspace_id: UUID, name: str, frm: int, to: int) -> DefinitionDiff: ...
    def rollback(self, workspace_id: UUID, name: str, to_revision: int, *, actor: UUID) -> RevisionDetail: ...
    def archive(self, workspace_id: UUID, name: str, *, actor: UUID) -> None: ...
    def import_yaml(self, workspace_id: UUID, req: ImportRequest, *, actor: UUID) -> DefinitionDetail: ...
```

**Publish algorithm (single transaction):** (1) load definition + draft; (2) `issues = collect_validation_issues(draft.graph, guard_registry=registries.guards, effect_registry=registries.effects, skill_names=..., base_bundled_name=defn.base_bundled_name, invariants=self._invariants)`; (3) if any `Severity.ERROR` → raise `PublishBlockedError(errors=...)`; (4) belt-and-braces: `load_definition(draft.dsl_yaml, guard_registry=registries.guards, effect_registry=registries.effects)` must succeed (catches anything the collector missed); (5) flip draft `status='published'`, set `published_at`, set `validation_status='valid'`; (6) set `workflow_definitions.current_published_revision_id = draft.id`, `draft_revision_id = NULL`; (7) emit a **critical, fail-closed** audit event on the same session via F39 — `audit.emit(session, AuditEvent(action=AuditAction.WORKFLOW_DEFINITION_PUBLISHED, resource_type=AuditResourceType.WORKFLOW_DEFINITION, resource_id=str(defn.id), severity=AuditSeverity.CRITICAL, outcome=AuditOutcome.SUCCESS, metadata={"name": name, "revision": draft.revision}))` — so a failed audit write rolls the publish back. Commit. (`rollback`/`archive` emit `WORKFLOW_DEFINITION_ROLLED_BACK`/`WORKFLOW_DEFINITION_ARCHIVED` the same way.) These three `AuditAction` values and the `WORKFLOW_DEFINITION` `AuditResourceType` are an additive, coordinated addition to F39's enums (§5).

---

## 5. Dependencies — features/slices that must exist first

Dependencies referenced by `<phase>/<id>-<slug>` path matching files under `docs/implementation-slices/`. **Slug reconciliation** (per the convention F15/F24/F37 froze): the platform scaffold has no dedicated numbered file yet — referenced here as `cross-cutting/C01-monorepo-and-api-foundations` (a.k.a. `v1/F00-foundation-substrate`); the authoritative auth/secrets/RBAC slug is **`cross-cutting/F37-auth-secrets-byok`** (it supersedes the stale `cross-cutting/C02-auth-and-rbac` seen in older siblings); the audit log is **`cross-cutting/F39-audit-log`**. All other slugs below match real files.

Hard (build-time):
- **`v1/F07-feature-workflow-fsm`** (REQUIRED) — the entire DSL + engine F28 builds on: `WorkflowDefinition`/`TransitionRule`/`RetryPolicy`/`EscalationPolicy`, `WorkflowState`/`WorkflowEventType`/`TERMINAL_STATES`/`HUMAN_GATE_EVENTS`, `GuardRegistry`/`EffectRegistry`, `default_registries()`, `load_definition` (`(source, *, guard_registry, effect_registry)`), `DSLValidationError`, `PostgresWorkflowEngine`, the bundled `default_feature.yaml`, the `workflow_run` table (F28 adds `definition_revision_id`), and the `GET /workflow/definitions/{name}` graph contract F28 supersedes. F28 also requires F07's backward-compatible coordinated widenings (§3.2): registry metadata incl. `is_precondition`, the `definition_provider` injection, and the keyword-only `start(..., *, workspace_id=None)`.
- **`cross-cutting/C01-monorepo-and-api-foundations`** (a.k.a. `v1/F00-foundation-substrate`; REQUIRED) — monorepo/`uv` workspaces, `apps/api` FastAPI skeleton, `apps/web` Next.js shell + `packages/ui-kit`, `packages/db` (`Base`, `TimestampMixin`, async session, Alembic env + naming convention), `forge-cli db migrate`, Postgres in compose.
- **`cross-cutting/F37-auth-secrets-byok`** (REQUIRED) — the `Principal` (`workspace_id`, `user_id`, `role ∈ {admin,member,viewer,agent-runner}`) resolved by `forge_api/deps/auth.py::get_principal` and the `require_role(min_role)` RBAC dependency used for the admin-gated mutations.
- **`cross-cutting/F39-audit-log`** (REQUIRED) — the canonical `AuditEvent` DTO + `AuditSink`/`SqlAuditWriter.emit` that publish/rollback/archive call **on the caller's session as critical, fail-closed** events (§4 publish algorithm step 7). F28 needs F39's enums extended additively with `AuditResourceType.WORKFLOW_DEFINITION` and `AuditAction.WORKFLOW_DEFINITION_{PUBLISHED,ROLLED_BACK,ARCHIVED}`. (If F39 has not yet landed at build time, the audit emit degrades to F01's `activity_events` via the same internal path F07 uses; the non-negotiable audit guarantee is only fully met once F39 is present.)

Soft (integration-time; F28 builds and ships without them, palette/targets just smaller):
- **`v1/F11-skill-profiles`** (SOFT) — its `SkillProfileRegistry.list(workspace_id=...)` supplies the catalog's `skills` and the `unknown_skill` (WARNING) validation; absent → skill picker is empty and skill validation is skipped.
- **`v2/F17-incident-workflows`** (SOFT) — registers the incident guards/effects (`default_incident_registries()`) and adds the bundled `incident.yaml`; with it present the editor can fork/customize the incident workflow and the catalog shows incident effects. Absent → only `default_feature` is forkable.
- **`v1/F10-run-trace-viewer`** (SOFT, future) — the eventual live-run overlay on the canvas reuses F10's run/event stream; not required for static authoring.
- **`v3/F27-supervised-multi-agent`** (SOFT, sibling v3 roadmap item "Supervised multi-agent mode") — when present, `supervised_multi_agent` is a real mode and subagent-policy-gated edges are meaningful; F28 still lists the mode without it.
- **`v3/F29-advanced-policy-engine`** (SOFT, sibling v3 roadmap item "Advanced policy engine with conditional rules") — conditional/policy guards it registers surface automatically in the catalog; no F28 change needed.

---

## 6. Acceptance criteria (numbered, testable)

1. **Round-trip fidelity.** For each bundled definition (`default_feature`, and `incident` when F17 present), `definition_to_graph` → `graph_to_definition` reproduces a `WorkflowDefinition` equal to `load_definition(bundled_yaml)` (same transitions, guards, preconditions, effects, skills, priorities, policies); and `graph_to_yaml` → `yaml_to_graph` is a fixed point on that graph.
2. **Node kinds derived.** In the `default_feature` graph, `created` is `initial`; `closed`/`failed`/`cancelled` are `terminal`; `spec_review`/`plan_review`/`awaiting_review`/`needs_human_input` are `human_gate`; all others `normal`.
3. **Catalog completeness.** `GET /workflow/editor/catalog` returns every `WorkflowState` value, every `WorkflowEventType` value, every registered guard (`is_precondition=False`, with `takes_arg=true`/`arg_hint` for `approval_granted`) under `guards`, every registered precondition (the `is_precondition=True` guard-registry entries `repo_target_set`, `policy_loaded`, `skill_profile_set`, `knowledge_synced`) under `preconditions`, every registered effect (with `provided_by`), the skill list (from F11's `SkillProfileRegistry.list(workspace_id)` if present), and modes `[single_agent, supervised_multi_agent]`.
4. **Multi-issue validation (not fail-fast).** A graph that simultaneously references an unknown state, an unregistered guard, an unregistered effect, and leaves a non-terminal state with no outgoing rule yields **four** distinct `ValidationIssue`s (one per problem) in a single `collect_validation_issues` call — not one.
5. **Validation parity with F07.** Any graph for which `collect_validation_issues` returns zero ERRORs is accepted by `load_definition(graph_to_yaml(graph), guard_registry=registries.guards, effect_registry=registries.effects)` without raising; and every condition that makes `load_definition` raise produces at least one ERROR issue from the collector. (UNKNOWN_SKILL/UNREACHABLE_STATE are WARNING-only and never affect this parity since F07 does not check them.)
6. **Protected invariant — merge gate.** Forking `default_feature` and deleting the `awaiting_review --review_approved--> merged` edge's `merge_ready` guard (or the edge) produces a `PROTECTED_INVARIANT_VIOLATION` ERROR (`invariant_id="merge_human_gate"`); publish is rejected with 409 and the run-time resolver continues to serve the last valid published (or bundled) revision.
7. **Protected invariant — spec gate.** Removing the `approval_granted:spec` guard from the `spec_review` exit on a `default_feature` fork produces a `PROTECTED_INVARIANT_VIOLATION` ERROR (`invariant_id="spec_gate"`) and blocks publish.
8. **Fork creates editable draft.** `POST .../{default_feature}/fork` creates a `workflow_definitions` row (`source=bundled_fork`, `base_bundled_name=default_feature`) with revision 1 `status=draft` whose `graph_json` equals the bundled graph (plus layout); `get_definition` then reports `editable=true`, `origin="bundled_fork"`.
9. **Bundled is read-only.** `PUT .../{default_feature}/draft` against the *unforked* bundled name returns 409 `BundledReadOnlyError`; bundled definitions never get a `workflow_definitions` row until forked.
10. **Single draft invariant.** Two `save_draft` calls update the *same* draft revision (no second draft row); the partial unique index rejects any attempt to create a second `draft`.
11. **Publish gate.** `publish` succeeds only when the draft has zero ERROR issues: it flips the draft to `published`, sets `current_published_revision_id`, clears `draft_revision_id`, sets `published_at`, and emits a critical `WORKFLOW_DEFINITION_PUBLISHED` `AuditEvent` (resource_type `WORKFLOW_DEFINITION`) on the same transaction via F39's `SqlAuditWriter`; a draft with ERRORs returns 409 `{errors:[...]}` and changes nothing (no audit event). A test with a failing `AuditSink` asserts the publish rolls back (fail-closed).
12. **Resolution: DB overrides bundled.** With a workspace that published a forked `default_feature`, `ResolvingDefinitionProvider.resolve("default_feature", workspace_id=W)` returns the DB definition + its `revision_id`; a different workspace with no fork returns the bundled definition + `revision_id=None`.
13. **Run-time revision pinning.** Starting a `WorkflowRun` in workspace W records `definition_revision_id` = the published revision and `definition_version` = its `dsl_version`; publishing a *new* revision afterward does **not** change the in-flight run's pinned revision (its `transition` keeps loading the pinned one).
14. **Revisions are immutable & monotonic.** Each `save_draft`/`publish`/`rollback` that creates content yields a strictly increasing `revision`; existing revisions' `dsl_yaml`/`graph_json`/`created_by`/`created_at` are never mutated (only the draft's status/validation/published_at change exactly once on publish).
15. **Diff.** `GET .../diff?from=1&to=2` returns a `DefinitionDiff` listing each added/removed/changed transition (with before/after) and added/removed states; an unchanged transition does not appear.
16. **Rollback creates a new draft.** `POST .../rollback {to_revision:1}` creates a new draft revision whose `graph_json` equals revision 1's content (history rows 1..N untouched); it must still pass validation/publish to take effect.
17. **Import validates.** `POST /workflow/editor/import` with YAML referencing an unregistered effect creates a draft whose validation returns an `UNREGISTERED_EFFECT` ERROR and cannot be published; well-formed YAML referencing only registered names imports and publishes cleanly.
18. **RBAC.** `viewer` gets 200 on all GET routes and 403 on save/validate/publish/fork/rollback/archive/import; `member` may `save_draft`/`validate_draft` but gets 403 on `publish`/`fork`/`rollback`/`archive`/`import`; `admin` may do all; cross-workspace `name` returns 404 (no existence disclosure); unauthenticated → 401.
19. **Export.** `GET .../export?format=yaml` returns the canonical YAML of the published revision (or `?revision=N`) with `text/yaml` content type, and that YAML re-imports to an equal graph.
20. **Frontend.** Forking `default_feature` renders all states as nodes and transitions as edges; selecting an edge and removing its `merge_ready` guard then clicking Validate shows the `merge_human_gate` violation in the Validation Panel, clicking it focuses the offending edge, and the Publish button is disabled with a tooltip listing the blocker (component test with mocked API).

---

## 7. Test plan (TDD) — concrete test cases (unit + integration), key fixtures

Framework: `pytest` + `pytest-asyncio`; frontend Vitest + Testing Library + MSW (component) and Playwright (e2e). Unit tests use SQLite + a fake `WorkflowDefinitionStore`; integration tests use the Postgres test container. Write tests first; no module is done until `ruff check`, types, and `pytest` for `packages/workflow-engine` are green.

**Key fixtures (`packages/workflow-engine/tests/editor/conftest.py`):**
- `registries` — F07's `default_registries()` bundle (`.guards`/`.effects`; + F17's `default_incident_registries()` when available) wired with the new `is_precondition`/`description`/`takes_arg`/`arg_hint`/`provided_by` metadata.
- `bundled_default_graph` — `definition_to_graph(load_definition(default_feature.yaml, guard_registry=registries.guards, effect_registry=registries.effects), title="Default Feature")`.
- `catalog` — `RegistryCatalog(guard_registry=registries.guards, effect_registry=registries.effects, skill_names_provider=...).build(workspace_id=ws)`.
- `fake_audit` — a recording `AuditSink` double (and a `failing_audit` double that raises, for the AC-11 fail-closed test).
- `editor_service(repo)` — `WorkflowEditorService` with a real `WorkflowDefinitionRepository` (DB) or a `FakeRepo` (unit), the `registries` bundle, `FEATURE_INVARIANTS`, and `fake_audit`.
- `make_graph(**mutations)` — clones `bundled_default_graph` and applies edge/node edits (drop guard, delete edge, add unknown state) for validation cases.

**Unit — graph round-trip (`test_graph.py`):** AC 1 (definition↔graph↔yaml fixed points for `default_feature`/`incident`); AC 2 (node-kind derivation); `auto_layout` determinism (same input → same positions).

**Unit — validation (`test_validation.py`):** AC 4 (four-issue case returns four issues); per-code cases (unknown state/event, unregistered guard/precondition/effect, dead-end state, unreachable state warning, nondeterministic same-`(from,on,priority)` rules, duplicate edge); AC 5 parity property test — generate valid mutations → `load_definition` accepts; generate each invalid mutation → both the collector ERRORs and `load_definition` raises; AC 6 + AC 7 protected-invariant cases (drop `merge_ready` edge/guard; drop `approval_granted:spec`).

**Unit — catalog (`test_catalog.py`):** AC 3 — every state/event/guard/precondition/effect/skill/mode present; `approval_granted` has `takes_arg=true`+`arg_hint`; effects carry `provided_by`.

**Unit — diff (`test_diff.py`):** AC 15 — added/removed/changed transition classification; added/removed states; `policy_changed` flips when retry/escalation differs; unchanged transitions omitted.

**Unit — service with FakeRepo (`test_service.py`):** AC 8 (fork → editable draft = bundled graph), AC 9 (bundled read-only), AC 10 (single-draft upsert), AC 11 (publish gate happy + blocked), AC 16 (rollback creates new draft), AC 17 (import validates).

**Integration — repository + resolver + DB (`tests/integration/test_editor_repository.py`):** AC 10 partial-unique single-draft at DB level; AC 14 immutability/monotonic revisions; AC 12 `ResolvingDefinitionProvider` DB-over-bundled across two workspaces; migration `upgrade`/`downgrade` clean.

**Integration — engine pinning (`tests/integration/test_run_pinning.py`):** AC 13 — start run resolves+pins published revision; publish a new revision; assert the run still loads its pinned revision on `transition` (uses F07 engine wired with the F28 provider).

**Integration — API (`apps/api/tests/workflow_editor/test_routes.py`):** AC 3 (catalog), AC 6/7/11 (publish 409 with `errors[]`), AC 15 (diff), AC 18 (RBAC matrix: viewer/member/admin/cross-workspace/unauth), AC 17 (import), AC 19 (export round-trip).

**Frontend (`apps/web/tests/workflow-editor/`):** `WorkflowCanvas.test.tsx` renders nodes/edges from a mocked graph; `EdgeInspector.test.tsx` editing guards/effects updates client graph state; `ValidationPanel.test.tsx` + `EditorToolbar.test.tsx` — AC 20 (remove `merge_ready` → violation shown → click focuses edge → Publish disabled with tooltip); `YamlDiffView.test.tsx` highlights changed transitions; `workflow-editor.spec.ts` (Playwright) — list → fork → edit → validate-error → fix → publish, asserting no full-page reload.

---

## 8. Security & policy considerations

- **Authoring is admin governance.** Publishing a definition changes the gating that applies to every future run in a workspace, so publish/fork/rollback/archive/import are **admin-only**; draft/validate are member+; everything else read-only (viewer+). The `agent-runner` role gets **no** editor write access — an agent can never edit the workflow that governs it (prevents self-scope-expansion: "the agent never self-assigns permissions or expands its own scope").
- **Protected invariants are non-bypassable.** `FEATURE_INVARIANTS` make it a publish-blocking ERROR to remove the human merge-approval gate (`merge_ready`) or the spec-approval gate (`approval_granted:spec`) from any `default_feature`-derived definition. The check runs server-side at publish (and on import), so neither the API nor a hand-crafted YAML import can ship a feature workflow that violates the non-negotiables. This is the structural enforcement of "Human approval is required before PR merge — always" and the Spec Gating Rules.
- **No behavior injection via the editor.** Guards/effects/preconditions are **only** referenceable by registered name; the editor composes existing Python predicates, it cannot define new ones. An unregistered name is an ERROR, never silently accepted, so a malicious or mistaken YAML import cannot introduce new agent capability — it can only wire up already-trusted, already-policy-evaluated effects.
- **In-flight runs are pinned.** A run records `definition_revision_id` at start and loads that exact revision for every transition; re-publishing cannot alter a workflow a human is mid-approval on, eliminating a TOCTOU class where an admin republish weakens a gate a run already passed.
- **Immutable, attributable audit.** Every revision row is append-only with `created_by` (repository-enforced — not F39's `attach_immutability_trigger`, because the single draft must legally mutate once on publish; the trigger forbids all UPDATE). Publish/rollback/archive additionally emit `WORKFLOW_DEFINITION_{PUBLISHED,ROLLED_BACK,ARCHIVED}` `AuditEvent`s through F39's `SqlAuditWriter.emit` as **critical, fail-closed** events on the mutating transaction, landing in F39's per-workspace hash-chained, tamper-evident `audit_log` ("every approval/action recorded — immutable, queryable").
- **Non-negotiables coverage.** F28 directly enforces *human-approval-before-merge* and *spec-gated implementation* (protected invariants, §4), *audit log* (F39), *RBAC + tenant isolation*, and *the agent never expands its own scope* (agent-runner has no editor write). The remaining platform non-negotiables — *hybrid retrieval*, *BYOK*, and *MCP read-only-by-default* — are **out of F28's surface**: the editor makes no model/LLM calls, no retrieval, and no MCP calls (validation and YAML round-trip are pure CPU), so it neither relaxes nor depends on them.
- **Tenant isolation.** All definition/revision reads/writes are scoped by `workspace_id`; cross-workspace `name`/`revision` returns 404 (no existence leak). A workspace can never resolve or fork another workspace's custom definition; bundled files are global read-only.
- **Input validation & size caps.** `name` is pattern-restricted; `dsl_yaml`/`graph_json` are size-capped; YAML is parsed with a safe loader (no arbitrary object construction). `validation_issues` and graphs are bounded by a max node/edge count to prevent pathological payloads.
- **Reversibility.** Because resolution prefers a DB definition only when it has a *published* revision and `is_active=true`, archiving a custom definition instantly and safely reverts new runs to the bundled behavior — a clean kill-switch for a bad publish.

---

## 9. Effort estimate & risk (S/M/L + key risks)

**Effort: L** (~3 engineer-weeks: ~1 backend — graph/validation/catalog/store/service/repo + the F07 resolver wiring; ~1.5 frontend — React Flow canvas, inspectors, validation/diff/history; ~0.5 tests). The conceptual core (graph↔DSL round-trip, multi-issue validator, revision lifecycle) is well-bounded; the volume is the canvas UX.

Key risks:
- **Validator drift from F07 (high impact, med likelihood).** If `collect_validation_issues` diverges from `load_definition`, a graph could pass the editor yet break the engine (or vice-versa). Mitigation: the parity property test (AC 5) runs both on the same inputs; publish additionally calls `load_definition` as a belt-and-braces gate; share the underlying rule predicates with F07 rather than copying logic.
- **Round-trip/layout fidelity (med).** Losing transition order, priority, or layout on round-trip corrupts a definition. Mitigation: fixed-point tests on bundled definitions (AC 1); layout stored in `graph_json` separately from DSL semantics; DSL serialization is canonical (sorted keys) so diffs are stable.
- **Resolver coupling into F07 (med).** Inserting `ResolvingDefinitionProvider` touches F07's `start`/`transition`. Mitigation: default provider = bundled-only (F07 tests unchanged); the new column is nullable; pinning test (AC 13) guards behavior.
- **React Flow complexity / keyboard-first bar (med).** Graph editors are mouse-centric; the board UX standard demands full keyboard operability. Mitigation: scope V1 to a focused interaction set (select/add/delete/connect via keyboard + inspector forms); auto-layout removes manual positioning as a requirement.
- **Protected-invariant correctness (high impact, low likelihood).** A weak invariant check could let a gate be removed. Mitigation: dedicated AC 6/7 tests, invariants expressed declaratively + unit-tested against deliberately-weakened graphs, and the server (not the client) is authoritative.

---

## 10. Key files / paths (exact)

Create (backend):
- `packages/workflow-engine/forge_workflow/editor/__init__.py`
- `packages/workflow-engine/forge_workflow/editor/graph.py`
- `packages/workflow-engine/forge_workflow/editor/validation.py`
- `packages/workflow-engine/forge_workflow/editor/catalog.py`
- `packages/workflow-engine/forge_workflow/editor/store.py`
- `packages/workflow-engine/forge_workflow/editor/repository.py`
- `packages/workflow-engine/forge_workflow/editor/service.py`
- `packages/workflow-engine/forge_workflow/editor/diff.py`
- `packages/workflow-engine/forge_workflow/editor/schemas.py`
- `packages/db/forge_db/models/workflow_editor.py` (ORM: `WorkflowDefinition`, `WorkflowDefinitionRevision`; subclass shared `Base`/`TimestampMixin`; imported by `packages/db`'s Alembic env so the migration sees them)
- `packages/workflow-engine/forge_workflow/editor/errors.py` (`PublishBlockedError`, `BundledReadOnlyError`, `DefinitionNotFoundError`, `DefinitionNameConflictError`, `RevisionNotFoundError`)
- `packages/workflow-engine/tests/editor/conftest.py`
- `packages/workflow-engine/tests/editor/test_graph.py`
- `packages/workflow-engine/tests/editor/test_validation.py`
- `packages/workflow-engine/tests/editor/test_catalog.py`
- `packages/workflow-engine/tests/editor/test_diff.py`
- `packages/workflow-engine/tests/editor/test_service.py`
- `packages/workflow-engine/tests/integration/test_editor_repository.py`
- `packages/workflow-engine/tests/integration/test_run_pinning.py`
- `apps/api/forge_api/routers/workflow_editor.py`
- `apps/api/tests/workflow_editor/test_routes.py`
- `packages/db/migrations/versions/xxxx_f28_workflow_editor.py` (2 tables with VARCHAR+CHECK status/source/validation + `workflow_run.definition_revision_id` column/FK; reversible)

Edit (extend / wire):
- `packages/workflow-engine/forge_workflow/guards.py` + `effects.py` (registry metadata: `register(..., description=, takes_arg=, arg_hint=, is_precondition=, provided_by=)`, `items()`)
- `packages/workflow-engine/forge_workflow/engine.py` (`PostgresWorkflowEngine` accepts `definition_provider`; `start(..., *, workspace_id=None)` pins `definition_revision_id`; `transition` loads pinned revision via `provider.load_pinned`)
- `packages/db/forge_db/models/workflow.py` (add `WorkflowRun.definition_revision_id`)
- `cross-cutting/F39-audit-log` enums (additive): `AuditResourceType.WORKFLOW_DEFINITION`, `AuditAction.WORKFLOW_DEFINITION_{PUBLISHED,ROLLED_BACK,ARCHIVED}`
- `apps/api/forge_api/deps.py` (wire `WorkflowEditorService` with the `default_registries()` bundle + F39 `SqlAuditWriter`, `RegistryCatalog`, `ResolvingDefinitionProvider`, exception handlers)
- `apps/api/forge_api/main.py` (mount `workflow_editor` router)

Create (frontend):
- `apps/web/app/[workspace]/settings/workflows/page.tsx`
- `apps/web/app/[workspace]/settings/workflows/[name]/page.tsx`
- `apps/web/components/workflow-editor/{WorkflowCanvas,StateNode,TransitionEdge,NodePalette,EdgeInspector,NodeInspector,EditorToolbar,ValidationPanel,YamlView,YamlDiffView,RevisionHistoryDrawer,ForkDialog,RollbackDialog,CreateDefinitionDialog,BundledReadOnlyBanner,DefinitionList,EmptyState}.tsx`
- `apps/web/lib/workflow-editor/{api,queries,mutations,layout,types}.ts`
- `apps/web/tests/workflow-editor/*.{test.tsx,spec.ts}`
- `packages/ui-kit/src/workflow-editor/*` (shared canvas primitives)
- `apps/web/package.json` (add `@xyflow/react`, `dagre`, YAML viewer dep)

---

## 11. Research references (relevant links from the spec/research report)

- `docs/FORGE_SPEC.md` → **Workflow Engine** (the Workflow DSL, `transitions`/`retry_policy`/`escalation_policy`, default-feature + incident state lists) — the artifact F28 edits visually; **Phased Roadmap → Phase 3 (V3)** item "Workflow visual editor"; **Spec Gating Rules** + **Human Approval System** (the gates the protected invariants enforce); **Security** (RBAC, immutable audit, tenant isolation); **Native Project Board → UX Standards** (keyboard-first, sub-100ms) applied to the editor.
- `docs/forge-research-report.md` → **Workflow Engine: LangGraph, Temporal, or Hybrid** (Postgres FSM is the V1 durable layer F28 governs; LangGraph/agent routing is untouched) and **"What Makes Forge Buildable" → "Workflow DSL: declarative — custom workflows without code changes"** (the OSS extension point F28 operationalizes through a UI).
- `docs/implementation-slices/v1/F07-feature-workflow-fsm.md` — upstream contracts reused: `WorkflowDefinition`/`TransitionRule`/`load_definition`, `GuardRegistry`/`EffectRegistry`/`DSLValidationError`, `WorkflowState`/`WorkflowEventType`, `workflow_run`, the bundled `default_feature.yaml`, and the explicit `GET /workflow/definitions/{name}` "graph data the V3 visual editor will consume."
- `docs/implementation-slices/v2/F17-incident-workflows.md` — the second editable definition (`incident.yaml`) + incident guards/effects the catalog surfaces.
- LangGraph (agent routing stays out of the editor): https://langchain-ai.github.io/langgraph/ · Symphony "workflow-files define how work moves through statuses": https://openai.com/index/open-source-codex-orchestration-symphony/ · React Flow (graph canvas): https://reactflow.dev/ · shadcn/ui + TanStack Query: https://ui.shadcn.com/ , https://tanstack.com/query · Pydantic v2 / SQLAlchemy 2.x / Alembic: https://docs.pydantic.dev/latest/ , https://docs.sqlalchemy.org/en/20/ , https://alembic.sqlalchemy.org/en/latest/

## 12. Out of scope / future

- **Authoring new guards/effects/preconditions (code).** These are Python predicates in F07's registries; the editor composes only registered names. Adding a new predicate remains a code change + redeploy — by design (no behavior injection from the UI).
- **Advanced conditional/policy rules** ("if repo X then require reviewer Y") — owned by `v3/F29-advanced-policy-engine`; F28 surfaces whatever guards that slice registers but defines no rule DSL of its own.
- **Live run overlay on the canvas** (highlighting a running workflow's current state/edge in real time) — reuses `v1/F10-run-trace-viewer` event stream; F28 V1 is static authoring only.
- **Temporal definition translation** — when the V2 engine swap to Temporal lands, the same `WorkflowDefinition` feeds the translator; F28 edits the definition, not the executor.
- **Cross-workspace / marketplace sharing of definitions** (export/import is per-workspace here) — fits `v3/F32-integration-marketplace`; F28's canonical YAML export is the interchange format it would build on.
- **Simulation / dry-run "what-if"** (feed sample events through a draft to preview the path) — valuable future add; F28 ships validation + diff, not execution preview.
- **Branching/merge of concurrent drafts** — F28 enforces a single draft per definition (last-writer with revision history); collaborative concurrent editing is future.
- **Editing the bundled YAML files themselves** — bundled definitions stay read-only source artifacts; customization is always via fork into a workspace-scoped definition.
