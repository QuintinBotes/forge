# HARD-14 — Future-Scope Execution (implementing the F40 deferred backlog on the real foundation)

> Phase: hardening · Blocker(s): #5 (parked / deferred items may remain reverted or unbuilt) · Status target: **"done" = the F40 *execution machinery and guardrails* exist and at least the V2 *spine* increments land green** — not "all 125 `D-*` items shipped" (that is an explicit multi-quarter program). Concretely: (a) a real-foundation mapping that re-targets every F40 idealized path onto the live `forge_*` packages, the singular `forge_db` schema, and the frozen `forge_contracts`; (b) a default-off feature-flag + backend-selector registry; (c) a protocol-stability audit proving no V1 caller leaked a backend-specific assumption; (d) a dual-lane (flag-off / flag-on) whole-suite green gate with a documented **revert-to-green** procedure per increment; (e) the four highest-leverage V2 spine increments (D-WF-1 Temporal, D-MR-1 multi-repo, D-RET-1 MCP sync-and-index, D-AUT-1 rule engine) either landed-green-behind-a-flag or carrying a dated, owned, slice-linked deferral. **Most of this runs offline / no creds** (governance, flags, protocol audit, Temporal test env, BM25/Qdrant containers, deterministic fakes). The cred-bearing / cluster-bound tail (real GitLab/Jira/Linear, real SSO/SCIM IdP, real Temporal/K8s cluster, Firecracker microVM, GPU scheduling, multi-week soak) is **named and deferred**, never claimed on simulated evidence.

---

## 1. Intent — what & why

Blocker #5 of the hardening program is that the ALPHA's deferred and "looks-done / parked" work may quietly stay un-built or reverted. Three sibling workstreams close the *named* parked items directly: **HARD-10** un-parks the crypto/OAuth seam, **HARD-11** un-parks tree-sitter chunking + verifies the LangGraph swap on the real path, and the toolchain workstream re-locks (`uv lock`) and stands up the 3.14-RC lane. Those retire the specific `# PARKED:` markers from `MORNING_REPORT.md §5`.

But the *systemic* form of blocker #5 is larger: every one of the 39 numbered slices deferred its richer capability into a `## 12. Out of scope / future` block, and `docs/implementation-slices/future/` consolidates those into **F40 — 125 `D-<THEME>-<n>` requirements across 17 themes** (`SPEC-FUTURE-deferred-scope.md` + `F40-deferred-scope.md`). Read as written, F40 is a backlog *catalogue*, not a build plan against the real tree. Two problems make it un-executable as-is:

1. **Path drift.** F40 was written against an idealized layout (`packages/repo-policy/`, `packages/pm-adapters/`, `packages/forge-automation/`, `packages/verification/`, `apps/api/app/api/v1/`, `apps/api/alembic/versions/`). The real monorepo uses flat `forge_*` packages (`forge_policy`, `forge_integrations`, `forge_workflow`, `forge_agent`, `forge_eval`, …), routers under `apps/api/forge_api/routers/`, models in `packages/db/forge_db/models/` (singular tables), and migrations in `packages/db/migrations/versions/` (with `packages/db/alembic.ini`). An execution plan that points at the idealized paths will create duplicate packages and diverge from the frozen contracts — exactly what the foundation rules forbid.
2. **No guardrail.** F40's high-blast-radius work (Temporal swap, Firecracker, SSO/SCIM, conditional policy, cross-repo atomic merge) is precisely the kind that can leave the tree red or silently regress the green V1 baseline. F40 says "default-off flag" once (§8); it never defines the *mechanism* or the *revert-to-green* contract that keeps the ~944-test ALPHA suite trustworthy while a multi-quarter backlog lands incrementally.

**HARD-14 is the execution slice that makes F40 buildable on the real foundation under the hardening whole-suite gate.** It does three things and only three things:

- **Re-targets F40 onto reality** — a single authoritative mapping (§3, §10) from every idealized F40 path to the live `forge_*` package / `forge_db` singular table / frozen `forge_contracts` seam, calling out which themes are *already scaffolded* in the ALPHA (the `sub_agent_run` table, `forge_integrations.pm.{jira,linear,sync_engine,registry}`, `forge_integrations.alerts.{datadog,sentry,pagerduty,grafana}`, `forge_knowledge.treesitter_chunking`, the `forge_coordinator` reserved stub) and which are genuinely net-new.
- **Builds the guardrail machinery** — a default-off feature-flag + backend-selector registry, a protocol-stability audit gate, and a dual-lane CI (flag-off must equal the recorded green baseline; flag-on is the creds/container-gated integration lane) with a one-command **revert-to-green** per increment.
- **Lands the spine** — executes the four V2 spine increments that unblock the most fan-out, each behind a flag, each leaving the whole suite green, each demonstrating the revert procedure; everything past the spine is scheduled, not finished.

This slice does **not** re-specify any `D-*` item's feature behaviour — that lives in the originating numbered slice's §1–§11 and in F40's per-theme §3/§6/§7. HARD-14 owns the *cross-theme execution contract*: mapping, flags, gate, sequencing, and the proof that landing future scope never breaks the present.

## 2. User-facing / operator behavior

HARD-14 is overwhelmingly an **engineering-governance** capability; its observable surface is for operators and maintainers, not end users. End-user behaviour belongs to each `D-*` item's own slice.

- **Operator — everything is off until chosen.** A fresh self-host runs exactly the V1 behaviour: FSM workflow backend, Postgres FTS keyword search, pgvector store, Jina reranker, worktree sandbox, Postgres vault. Every F40 backend is selected by an explicit `FORGE_*` config value or a default-off `FORGE_FEATURE_*` flag; nothing future-scoped activates implicitly. `GET /health` reports the active backend selection set so an operator can see, at a glance, which future-scope features are enabled in their deployment.
- **Operator — flip one backend at a time.** Setting `FORGE_WORKFLOW_BACKEND=temporal` (with the Temporal profile deployed) switches the workflow backend with no DSL/definition change and no other behaviour change; reverting the value returns to the FSM with no data migration required for new runs. The same one-knob-per-backend pattern holds for keyword/vector/reranker/sandbox/secrets.
- **Maintainer — revert-to-green is a documented, tested procedure.** Each future-scope increment ships as a single flag-introducing commit with a recorded green baseline (suite counts + SHA) in the execution ledger. A maintainer can return any increment to the last green state by either flipping the flag default or `git revert`-ing the increment commit; CI proves both restore the baseline.
- **Maintainer — the backlog is schedulable.** The mapping doc + flag registry turn F40 from a catalogue into a worklist: each theme mini-slice has a real target package, a flag name, a spine/fan-out position, and a green-gate definition. A grooming session opens the ledger, not 39 slice files.
- **Developer journeys (per F40 §2, unchanged, now gated)** are inherited verbatim — durable workflow survives a crash (WF), one task many repos (MR), supervised run (MA), indexed MCP + true BM25 (RET), conditional gate (POL), saved automations (AUT), external systems (INT), enterprise hardening (SEC), deep observability (OBS), Kubernetes (INF), isolation ladder (SBX), depth surfaces (PM/UI/DOC/EVAL/GEN) — each only when its flag/selector is enabled.

## 3. Vertical slice

> The "vertical" here is the **execution substrate** that cuts across all 17 themes, plus the four spine increments wired through it end-to-end. Per-theme verticals live in F40 §3 and the originating numbered slices; this section grounds them on the real tree and adds the guardrail layer.

### 3.1 Data model

All future-scope schema changes are **additive, reversible, singular-table, `Enum(native_enum=False)` string enums**, authored as Alembic revisions in `packages/db/migrations/versions/` (NOT the F40-idealized `apps/api/alembic/`), chained after the live baseline, applied/rolled-back on real pgvector under **HARD-01**.

- **Seams that already exist in `forge_db.models` (no new table needed — extend in place):**
  - `sub_agent_run` (MA: D-MA-1/3 parent linkage) — already present in `runs.py`; the multi-agent work populates it, it does not create it.
  - `incident`, `incident_alert`, `incident_event`, `remediation_plan`, `postmortem`, `postmortem_action_item` (`incidents.py`) — the WF-incident / AUT-SLO / OBS-incident-analytics / INT-ops themes ride these.
  - `pm_connection`, `pm_task_link`, `pm_webhook_delivery` (`pm.py`) — the INT PM-sync theme (D-INT-1/8/9) extends these.
  - `sprint`, `milestone`, `epic`, `task`, `project` (`planning.py`/`project.py`) — the PM-depth theme (D-PM-*) adds columns (`team_id`, estimate scale/history, goal↔criteria links) **additively, without breaking the event log**.
  - `policy_profile`, `skill_profile` (`profiles.py`) — POL/EVAL extend; `repository_connection`, `mcp_connection`, `knowledge_source`, `retrieval_chunk` (`connections.py`/`knowledge.py`) — MR/RET extend.
- **Net-new additive tables (created behind their theme's flag, dropped cleanly on downgrade):** `automation_rule` + `automation_run` (AUT), `traceability_rollup_history` (OBS-7/PM-5), `milestone_progress` materialized cache (INF-10, **only if** a measured `seed_demo_board` regression justifies it), `experiment` / `experiment_arm` (EVAL-7), `marketplace_package` + `package_version` (INT-6). Each follows the existing singular-name convention (`automation_rule`, not `automations`).
- **Backend-selection persistence.** Per-workspace backend overrides (where a workspace may pin a backend independent of the deployment default) are stored as additive JSONB on `workspace` (a `feature_overrides` column) rather than a new table — keeping the override surface auditable and migration-light. Deployment-wide defaults come from settings/env (§4), not the DB.
- **DB-level integrity for the high-risk themes** (SEC-4): an audit/transition **immutability trigger** Alembic revision (rejecting post-hoc UPDATE/DELETE on the audit + `workflow_run`/transition rows) is the same trigger HARD-01 exercises; SEC-4 adds the Merkle/chain verifier on top of it.

### 3.2 Backend

- **Flag + selector registry (the heart of the guardrail).** Backend selectors and feature flags are declared once as additive enums/constants in `packages/contracts/forge_contracts/features.py` (a **new module** — additive, does not mutate the frozen DTOs/Protocols in `dtos.py`/`protocols.py`), and resolved into concrete backend instances **at the composition root only** (`apps/api/forge_api/settings.py` + `deps.py`, `apps/worker/forge_worker/celery_app.py`). Pure packages (`forge_workflow`, `forge_knowledge`, `forge_agent`, `forge_policy`) never read env directly — they receive the chosen backend via constructor/DI, exactly as F05's `EmbeddingProvider`/`Reranker` are injected. This preserves the no-leak invariant and the "swap is non-breaking" contract.
- **Protocol-stability audit gate.** A new test module `tests/test_protocol_stability.py` (repo root, runs in the default suite) asserts that each swappable seam's V1 callers depend only on the Protocol, not a concrete backend: the FSM (`forge_workflow.engine`/`fsm`) is reached only via the `WorkflowEngine` Protocol; `forge_knowledge` callers use `KeywordSearcher`/`Reranker`/`EmbeddingProvider` + `reciprocal_rank_fusion`; `forge_agent` sandbox callers use the `SandboxProvider` Protocol; the vault is reached via its cipher/secrets interface. This gate is the precondition F40 §9 names as the single largest risk; it must be green before any backend-swap increment merges.
- **API surface — extend `forge_api`, add no new top-level namespace.** Future-scope endpoints attach to the existing routers under `apps/api/forge_api/routers/` (e.g. `workflow.py` gains a backend selector + `definitions/{name}` graph data for the editor; `policy.py` gains conditional authoring; `knowledge.py` gains `sync_mode=mcp_sync_and_index`; new `automation.py`, `trace.py` export + WS, `eval.py` A/B, `sprint.py` routers register alongside the existing set). Services live in `apps/api/forge_api/services/`. Every new route is RBAC-gated through the existing auth deps and is **inert when its flag is off** (returns `404`/`501` `feature_disabled`, never a half-built handler).
- **Spine increment 1 — D-WF-1 (Temporal backend).** Add `packages/workflow-engine/forge_workflow/temporal/` implementing the existing `WorkflowEngine` Protocol; the FSM stays the default. `FORGE_WORKFLOW_BACKEND` selects per-run. Exactly-once effect dispatch reuses the F08 effect path. `temporal` is an optional dependency extra so the default install/import is unchanged.
- **Spine increment 4 — D-AUT-1 (rule engine).** A net-new `forge_automation` package (`packages/automation/forge_automation/`, following the `forge_*` naming) holding a **deterministic, side-effect-isolated, dry-run-testable** `RuleEngine` (trigger + `ConditionGroup` + action), kept **separate from the FSM** per the spec mandate. It consumes the shared `Condition` primitive from `forge_contracts.conditions` (also consumed by POL — D-AUT-10/D-POL-1 share one primitive).

### 3.3 Worker / agent

- **`apps/worker/forge_worker`** gains: a Temporal worker + activity registration module (`temporal_worker.py`) for D-WF-1; Celery-Beat / Temporal schedules (`schedules.py`) for D-AUT-2 scheduled triggers and RET subscription/poll consumers; the MCP sync-and-index task path extends `forge_worker/syncer.py` / `tasks/` for D-RET-1.
- **`packages/agent-runtime/forge_agent`** gains the multi-repo run loop (extend `runtime.py` for D-MR-1 `repo_targets`) and the sandbox-provider ladder: `forge_agent/sandbox.py` (the existing worktree sandbox) becomes the `worktree` implementation of a new `SandboxProvider` Protocol; `forge_agent/sandbox/` adds `docker.py` (D-SBX-1), `k8s_job.py` (D-SBX-2), `firecracker.py`/`gvisor.py` (D-SBX-3) — each a drop-in behind the Protocol, default `worktree`.
- **`packages/multi-agent-coordinator/forge_coordinator`** — the reserved stub (currently a 2-statement placeholder, explicitly "V2 per the plan") is where the **supervised multi-agent** coordinator lands (D-MA-1: deterministic, policy-driven supervisor; subagent spawner; scoped specialist tool sets; `max_parallel`), populating the existing `sub_agent_run` rows and linking from `RunTrace.agent_run_ids`. This is the one place HARD-14 turns a stub into a real implementation; it does **not** create a parallel package — it fills the reserved one.
- **Determinism mandates carried through.** The supervisor (D-MA-1) and the rule engine (D-AUT-1) stay deterministic/policy-driven; LLM-class behaviour (D-MA-4 adaptive routing, D-AUT-5 LLM actions) is delegated to *graded* agent runs and gated behind D-EVAL-4 measurement — never embedded in the router/evaluator.

### 3.4 Frontend

`apps/web` (Next.js; excluded from the uv workspace; gated by `pnpm test` Vitest + `next build`) gains future-scope routes **behind the same flags**, each rendering a `feature_disabled` empty-state when off so the default web bundle and the 28 Vitest tests are unchanged:

- `app/(app)/workflow/editor/` (WF canvas, D-WF-3), `app/(app)/analytics/` + `components/trace/` (OBS deep viz / live overlay / export), `app/(app)/traceability/` (UI-4/7), `app/(app)/sprints/` (PM charts), `app/(app)/eval/` (EVAL A/B + experiments), `components/approval/KnowledgeProvenancePanel.tsx` (UI-5), `app/(app)/settings/notifications/` (UI-6), and the Slack interaction modal path. Each ships its own Vitest tests; none alters a V1 component's default render.

### 3.5 Infra / deploy / CI

- **`deploy/`** gains opt-in profiles consumed (not forked) by the existing compose: a Temporal server profile (D-WF-1/D-INF-3), `deploy/socket-proxy/` (D-SBX-1 host-socket mediation), `deploy/helm/` (D-INF-1 chart + kind/minikube profile, HPA, NetworkPolicy, PodSecurity), `deploy/terraform/` (D-INF-4). The Helm chart **consumes `apps/web`/`deploy/docker` images, it does not fork build ownership** (that stays HARD-08).
- **Dual-lane CI (`.github/workflows/ci.yml`).** Today CI runs one hermetic lane. HARD-14 adds:
  1. **flag-off lane (default, blocking):** the existing whole-suite gate — `uv run pytest -q` + `uv run ruff check .` + `uv run ruff format --check .` + `make typecheck` + `cd apps/web && pnpm test` — which **must match the recorded green baseline counts** (the off-path of new code adds unit tests but never changes V1 behaviour). This is the revert-to-green anchor.
  2. **flag-on integration lane (gated, non-blocking where creds/containers absent):** runs the `integration`/`postgres` markers plus per-theme flags enabled, using the existing `FORGE_TEST_DATABASE_URL`/testcontainers wiring and the `.env.integration` creds when present; skips cleanly otherwise (mirrors the SPEC's creds-gated discipline).
- **Markers.** Reuse the existing `postgres` and `integration` pytest markers (already registered in `pyproject.toml`); add a `future` marker for flag-on theme tests so the default suite stays hermetic and network-free.
- **Re-lock per increment.** Each theme that adds a dependency (`temporal`, `paradedb`/`pg_search` client, `qdrant-client`, `colbert`, `python-saml`, `hvac` for Vault, etc.) declares it as an **optional extra** and re-runs `uv lock`; CI uses `uv sync --frozen` (the toolchain workstream owns the lock-completeness gate, HARD-14 feeds it).

## 4. Public interfaces / contracts

HARD-14 introduces **no new top-level product API** — it implements behind existing V1/V2/V3 seams and adds only the cross-theme execution contract.

### 4.1 Backend-selector config keys (extend `forge_api/settings.py`, `FORGE_` prefix to match the existing settings convention)

```python
# packages/contracts/forge_contracts/features.py  (NEW, additive — frozen DTOs untouched)
from enum import StrEnum

class WorkflowBackend(StrEnum):  postgres_fsm = "postgres_fsm"; temporal = "temporal"          # D-WF-1
class KeywordBackend(StrEnum):   postgres_fts = "postgres_fts"; paradedb_bm25 = "paradedb_bm25" # D-RET-2
class RerankerBackend(StrEnum):  jina_v2 = "jina_v2"; colbert = "colbert"; off = "off"          # D-RET-3
class VectorStore(StrEnum):      pgvector = "pgvector"; qdrant = "qdrant"                        # D-RET-7
class SandboxProviderKind(StrEnum): worktree="worktree"; docker="docker"; k8s_job="k8s_job"; firecracker="firecracker"; gvisor="gvisor"  # D-SBX-*
class SecretsBackend(StrEnum):   postgres_vault = "postgres_vault"; vault = "vault"; external_secrets = "external_secrets"  # D-SEC-3
```

Env / settings keys (deployment-wide defaults; per-workspace override via `workspace.feature_overrides` JSONB):

| Key | Type | Default | Theme |
|---|---|---|---|
| `FORGE_WORKFLOW_BACKEND` | `WorkflowBackend` | `postgres_fsm` | WF / D-WF-1 |
| `FORGE_KEYWORD_BACKEND` | `KeywordBackend` | `postgres_fts` | RET / D-RET-2 |
| `FORGE_RERANKER` | `RerankerBackend` | `jina_v2` | RET / D-RET-3 |
| `FORGE_VECTOR_STORE` | `VectorStore` | `pgvector` | RET / D-RET-7 |
| `FORGE_SANDBOX_PROVIDER` | `SandboxProviderKind` | `worktree` | SBX / D-SBX-* |
| `FORGE_SECRETS_BACKEND` | `SecretsBackend` | `postgres_vault` | SEC / D-SEC-3 |
| `FORGE_FEATURE_<THEME>_<ITEM>` | `bool` | `False` | every flagged increment (e.g. `FORGE_FEATURE_MULTI_REPO`, `FORGE_FEATURE_SUPERVISED_AGENT`, `FORGE_FEATURE_MCP_SYNC_INDEX`, `FORGE_FEATURE_AUTOMATIONS`, `FORGE_FEATURE_CONDITIONAL_POLICY`) |

All defaults reproduce V1 behaviour exactly; with no `FORGE_*` set, the hermetic suite is byte-for-byte the ALPHA suite.

### 4.2 Cross-theme seams (additive; reconcile against the frozen `forge_contracts.protocols`)

```python
# SandboxProvider — introduced by D-SBX-1, the stable seam every higher rung implements
class SandboxProvider(Protocol):
    async def acquire(self, *, run_id: UUID, image: str, limits: ResourceLimits, worktree: Path) -> Sandbox: ...
    async def exec(self, sandbox: Sandbox, cmd: list[str], *, timeout_s: int) -> ExecResult: ...
    async def release(self, sandbox: Sandbox) -> None: ...   # destroys per-run isolation

# Shared Condition primitive — lifted to forge_contracts.conditions; consumed by POL (D-POL-1) AND AUT (D-AUT-10)
class ConditionOp(StrEnum): eq="eq"; ne="ne"; in_="in"; gt="gt"; lt="lt"; exists="exists"; matches="matches"
class Condition(BaseModel): field: str; op: ConditionOp; value: JSONValue
class ConditionGroup(BaseModel): all: list["Condition | ConditionGroup"] = []; any: list["Condition | ConditionGroup"] = []
def evaluate(group: ConditionGroup, context: Mapping[str, JSONValue]) -> bool: ...  # PROVABLY TERMINATING — the ceiling, no expression language

# RuleEngine — D-AUT-1, deterministic + side-effect-isolated
class RuleEngine(Protocol):
    def evaluate(self, event: DomainEvent, rules: list[Rule], *, dry_run: bool = False) -> list[ActionPlan]: ...

# Unchanged (Temporal/BM25/Qdrant/etc. are BACKENDS behind these, not new APIs):
#   WorkflowEngine (forge_workflow), KeywordSearcher/Reranker/EmbeddingProvider + reciprocal_rank_fusion (forge_knowledge),
#   PMAdapter + PMSyncEngine (forge_integrations.pm), NotificationEvent (forge_integrations), AuditSink + chain verifier (forge_db/forge_api audit),
#   SpecGenerator/PostmortemComposer (forge_spec/forge_integrations.incident).
```

`Condition`/`ConditionGroup`/`SandboxProvider`/`RuleEngine` land as **new modules** in `forge_contracts` (`conditions.py`, plus protocol additions in a new file) so the frozen `dtos.py`/`protocols.py`/`enums.py` signatures are not mutated; a contract test asserts the existing exported symbols are unchanged.

### 4.3 Execution ledger (the revert-to-green record)

A per-increment record (created in-repo at execution time under `docs/hardening/future-scope/ledger/`, one file per increment) capturing: theme + `D-id`s, flag/selector name + default, **baseline green SHA + suite counts** (pytest passed/skipped, web passed), the flag-on evidence (command + output), the originating numbered slice link, and the one-command revert. CI reads the baseline counts to enforce the flag-off lane has not regressed.

## 5. Dependencies

**Hardening-program prerequisites (must be green first):**

- **HARD-01 (real Postgres + pgvector)** — every additive migration in this slice is applied/rolled-back on the live container; the `sub_agent_run`/`incident*`/`pm_*` seams and the SEC-4 immutability trigger are only trustworthy on real PG.
- **HARD-12 (whole-workspace typecheck + coverage floor)** — `make typecheck` must be one green command and the worker/agent coverage floor must hold *before* this slice adds the most side-effecty future-scope code; otherwise the green gate is not trustworthy.
- **HARD-11 (tree-sitter un-park + LangGraph verified)** — the RET binary/chunking theme (D-RET-5) and the GEN/agent themes ride the real chunking + real `langgraph.StateGraph` path; HARD-14 does not re-do that un-parking, it builds on it.
- **HARD-10 (real crypto / secret-key / OAuth / vault)** — the SEC theme (D-SEC-1/3) and every cred-bearing increment resolve keys through the real vault; HARD-14's cred-gated lanes require it.
- **HARD-09 (automated security + enforcement matrix)** — the high-blast-radius themes (POL totality, MCP write-default, SBX escape, SEC) reuse HARD-09's enforcement tests and abuse-test discipline.
- **Toolchain re-lock workstream** — consumes the per-increment optional-extra deps; HARD-14 feeds it and depends on `uv sync --frozen` being the CI default.

**F40 / V1–V3 protocol seams (from F40 §5, restated against real packages):** the entire V1 critical path must have shipped with Protocol seams intact — `forge_db` foundation → auth/vault (`forge_api.auth`) + audit (`forge_db`/`forge_api`) → `forge_policy` → `forge_knowledge`/`forge_skill` → `forge_agent` → `forge_workflow` → PEV/PR/approval flow, with `forge_board` in parallel. The **protocol-stability audit (§3.2) is the gating dependency**: any V1 caller that leaked an FSM/pgvector/Docker assumption must be fixed before the corresponding swap.

**Spine ordering (build spines before fan-outs):** D-WF-1 (Temporal) precedes D-WF-2/6/7, D-MA-5, D-SEC-5, D-OBS-8, D-INF-3. D-MR-1 precedes all other MR items. D-MA-1 precedes D-MA-2..6, D-MR-4, D-EVAL-4/8, D-OBS-3. D-SBX-1 → D-SBX-2 → D-SBX-3. D-POL-1 precedes D-POL-2/3/6, D-AUT-4, D-MCP-6, D-AUT-10. D-INF-1 precedes D-SBX-2, D-INF-2/3/4/5/7/8, D-SEC-3. D-RET-1 precedes D-RET-4/5/6, D-MCP-6 (D-RET-2 is independent/parallel). D-AUT-1 precedes D-AUT-2/3/4/5/7/8/9. (Full graph in F40 §3/§5.)

## 6. Acceptance criteria

> "Offline" = runs in the hermetic default sandbox, no creds. "Creds/cluster" = requires real credentials or a networked/CI runner and is therefore gated + skip-clean.

**Governance machinery (all offline):**

1. A real-foundation **mapping document** exists in-repo that re-targets every F40 idealized path (§10 table) to a live `forge_*` package / singular `forge_db` table / frozen `forge_contracts` seam, and flags each theme as *already-scaffolded* (with the real module path) or *net-new*. No mapping entry points at a duplicate/idealized package. (offline)
2. The **flag + selector registry** (`forge_contracts.features` + `forge_api` settings) exists; with **no `FORGE_*` env set**, the whole-suite green gate produces counts **equal to the recorded ALPHA baseline** (≈944 passed / ≤3 skipped Python, 28 web), proving every future-scope default is V1 behaviour. (offline)
3. The **protocol-stability audit** test is green: each swappable seam (`WorkflowEngine`, `KeywordSearcher`/`Reranker`/`EmbeddingProvider`, `SandboxProvider`, vault cipher/secrets) is reached by V1 callers only via its Protocol; a deliberately leaked import fails the test. (offline)
4. A **contract test** asserts the frozen `forge_contracts` exported symbols (`dtos`, `protocols`, `enums`) are unchanged after the additive `features.py`/`conditions.py` modules land. (offline)
5. **Dual-lane CI** is wired: the flag-off lane is blocking and matches the baseline counts; the flag-on lane runs the `integration`/`postgres`/`future` markers and **skips cleanly** when creds/containers are absent (suite stays green). (offline; flag-on requires creds/cluster where noted)
6. The **revert-to-green** procedure is proven: for at least one landed increment, flipping the flag default *and* `git revert`-ing the increment commit each restore the baseline counts; a CI check enforces it. (offline)

**Shared primitives (offline):**

7. `forge_contracts.conditions.evaluate` is **total and non-Turing-complete** — a property/contract test shows no input causes it to loop or diverge; it is the ceiling (no expression language). (offline)
8. `SandboxProvider` Protocol + the `worktree` implementation pass a **provider contract suite** that every later rung must also pass; the existing `forge_agent.sandbox` behaviour is unchanged when `FORGE_SANDBOX_PROVIDER=worktree`. (offline)

**Spine increments (each leaves the whole suite green; each behind a default-off flag):**

9. **D-WF-1 (Temporal) parity** — a task drives `created→…→merged` on the Temporal backend with **identical observable `workflow_run`/transition rows** to the FSM, selectable by `FORGE_WORKFLOW_BACKEND` per-run, **no DSL/definition change**; killing + restarting the worker mid-run resumes with no lost/duplicated effect (effect-sink spy). (offline via the Temporal test env / time-skipping test server; a **real Temporal cluster** is creds/cluster, deferred)
10. **D-MR-1 (multi-repo)** — a two-`repo_targets` task produces one PR per repo each gated by its own composed policy, the run trace links both PRs to one task, and a merge-gate failure on either blocks completion; cross-repo retrieval returns one fused ranked list. (offline with deterministic GitHub fakes; **real GitHub/GitLab** is creds, deferred to HARD-05's live path)
11. **D-RET-1 (MCP sync-and-index)** — a configured MCP source polled on its SLA appears as searchable indexed chunks with `mcp://` provenance; re-sync updates changed + removes deleted (no orphans); selectable via `sync_mode=mcp_sync_and_index`. (offline with the MCP fake transport; **real MCP server** rides HARD-06's live path, deferred)
12. **D-AUT-1 (rule engine)** — a rule fires its actions on a matching trigger+conditions, evaluation is **deterministic + dry-run-testable** (effect-sink spy: dry-run dispatches nothing), the engine is separate from the FSM, and it consumes the shared `Condition` primitive with **no F21/workflow test regression**. (offline)

**Backlog governance (offline):**

13. Every F40 theme **not** landed in this slice carries a **dated, owned, slice-linked deferral** in the execution ledger (theme, spine position, owning numbered slice, blocking dependency, creds/cluster need) — so blocker #5 is closed *as a tracked program*, never as a silent gap.
14. The release notes / `BETA_REPORT`/`PRODUCTION` evidence pack lists, verbatim, which future-scope themes are **shipped-behind-a-flag**, **scheduled**, and **named-cannot-do-in-sandbox** (real cluster soak, Firecracker on the build host, real third-party IdP, GPU scheduling, multi-week multi-tenant soak). (offline)

## 7. Test plan (TDD)

Write tests first (backend-tdd, ≥80% per the skill profile). Tests live under each theme's **real** package/app test root; shared deterministic, no-network fakes are reused.

**Governance + guardrail (default suite, offline):**

- `tests/test_protocol_stability.py` — import-graph / AST assertions that V1 callers depend only on the swappable Protocols; a fixture that injects a concrete-backend import fails the gate. (AC3)
- `tests/test_feature_defaults_equal_baseline.py` — with `FORGE_*` unset, asserts the active backend set equals the V1 set and that the suite count matches the recorded baseline. (AC2)
- `packages/contracts/tests/test_frozen_surface_unchanged.py` — snapshot of `forge_contracts` public symbols; additive modules don't mutate frozen ones. (AC4)
- `packages/contracts/tests/test_condition_total_non_turing.py` — property test (Hypothesis) that `evaluate` terminates and never diverges on arbitrary nested groups. (AC7)
- `tests/test_revert_to_green.py` (CI-driven) — runs the flag-off lane against the ledger baseline counts; a companion CI job proves `git revert <increment-sha>` restores them. (AC6)

**Cross-theme contract suites (run against every backend/impl — the "swap is non-breaking" invariant):** `SandboxProvider`, `KeywordSearcher`, `Reranker`, vector-store, `PMAdapter`, `WorkflowEngine`, `RuleEngine`, `Condition` evaluator. Each suite is parametrized over its implementations; a new backend is "done" only when it passes the existing suite with no caller change.

**Spine increments (real package roots):**

- **WF** `packages/workflow-engine/tests/`: `test_temporal_backend_parity` (same `workflow_run`/transition rows as FSM), `test_worker_kill_resumes_exactly_once` (`FakeEffectSink`), `test_incident_def_loads_on_both_backends`, `test_dry_run_no_effects_dispatched`. Fixtures: in-memory/time-skipping Temporal test env (no real cluster). (offline)
- **MR** `packages/agent-runtime/tests/multi_repo/` + `apps/api/tests/multi_repo/`: `test_two_repos_two_prs_each_policy_gated`, `test_trace_links_both_prs_one_task`, `test_merge_gate_failure_blocks_completion`, `test_cross_repo_retrieval_single_fused_list` (reuse F05 fakes + GitHub fake). (offline)
- **RET** `packages/knowledge-core/tests/`: `test_mcp_sync_indexes_updates_and_removes` (no orphans, `mcp://` provenance), `test_bm25_passes_contract_and_recall_ge_fts`, `test_qdrant_passes_retrieval_contract` (testcontainer, offline-local). Reuse `FakeEmbeddingProvider`/`FakeReranker`. (offline; BM25/Qdrant via local containers)
- **AUT** `packages/automation/forge_automation/tests/`: `test_rule_fires_on_match_deterministic`, `test_dry_run_isolated`, `test_consumes_shared_condition_no_workflow_regression`. (offline)
- **MA** (fills `forge_coordinator`) `packages/multi-agent-coordinator/tests/`: `test_supervised_spawns_and_joins`, `test_subagent_scoped_tool_denied`, `test_subagent_run_linked_to_parent`, `test_max_parallel_queues_kplus1`. (offline)

**Cred/cluster-gated lanes (skip-clean when absent, never silently fake):** real Temporal cluster (WF-8 migration/cloud), real GitLab/Jira/Linear (MR-3/INT), real SSO/SCIM IdP (SEC-1/2), Firecracker microVM (SBX-3), K8s/Helm (INF/SBX-2), GPU reranker scheduling (INF-8). These ride the existing `integration` marker + `.env.integration`.

**How to run:**

```bash
# Default hermetic lane (must equal baseline; revert-to-green anchor):
uv run pytest -q && uv run ruff check . && uv run ruff format --check . && make typecheck
cd apps/web && pnpm test

# Governance gate only:
uv run pytest tests/test_protocol_stability.py tests/test_feature_defaults_equal_baseline.py \
              packages/contracts/tests/test_frozen_surface_unchanged.py -q

# Flag-on integration lane (creds/containers; skips clean otherwise):
export FORGE_TEST_DATABASE_URL=postgresql+psycopg://...   # HARD-01 container
export FORGE_FEATURE_MULTI_REPO=1 FORGE_FEATURE_AUTOMATIONS=1
uv run pytest -m "future or integration or postgres" -q
```

## 8. Security & policy considerations

The deferred backlog concentrates the platform's highest-blast-radius work; HARD-14's job is to make sure none of it can degrade the green V1 baseline or weaken a security boundary while landing incrementally.

- **Default-off discipline is the primary control.** Every High / Med-High theme ships behind a default-off flag with its own contract + abuse tests; with defaults, no future-scope code path is reachable. The flag-off CI lane proving baseline-equality (AC2/AC6) is what makes incremental landing safe.
- **Policy correctness is a security boundary (D-POL-1/2).** `forge_contracts.conditions.evaluate` MUST stay total and non-Turing-complete (AC7) — the expression-language temptation is a **permanent non-goal**. Conditional rules never *widen* a flat decision; task-level overlays narrow only; `kind=deploy` gates block dispatch until human approval.
- **Mutation safety (D-MCP-1).** Write MCP calls require `allow_write=true` AND a recorded admin approval; every mutation is audit-logged with payload hash (reuses HARD-06's live-path enforcement).
- **Kernel isolation + container escape (D-SBX-1/2/3).** No host Docker socket reachability (proxy-mediated); non-root + default-deny NetworkPolicy on K8s; microVM syscall mediation; resource breach terminates the run cleanly. The `SandboxProvider` contract suite encodes these as tests every rung must pass.
- **Identity + secrets (D-SEC-1/2/3).** SSO/SCIM/RBAC are auth-boundary changes — full role-matrix + deprovisioning tests; secrets backends leave **no plaintext at rest in the app DB**. Resolved through HARD-10's real vault.
- **Data integrity (D-MR-6, D-SEC-4).** Atomic cross-repo merge is all-or-none with audited rollback; audit/transition immutability is enforced by a DB trigger **and** a chain verifier (defense in depth) on top of HARD-01's trigger.
- **Tenant isolation (D-SEC-5, D-RET-1, D-MCP-6).** Per-tenant Temporal namespaces; indexed MCP chunks scoped by `allowed_namespaces`; per-resource ACLs exclude unentitled docs at query time; all reads filter by `workspace_id`.
- **Determinism mandates.** Supervisor (D-MA-1) and rule engine (D-AUT-1) stay deterministic/policy-driven; LLM-class behaviour is delegated to graded agent runs, never embedded in the router/evaluator.
- **Permanent non-goal reaffirmed.** Incident auto-remediation without human approval is never built; D-POL-9's bounded relaxed-posture profile is the only sanctioned relaxation and permits only enumerated actions.
- **Creds handling (where increments take real creds).** Env-only ingress from gitignored `.env.integration`, never committed/logged/in fixtures, resolved at call time from the vault, redacted on every live path — identical to the SPEC's §"Credentials & secrets handling" rules.

## 9. Effort & risk

**Overall: XL — a multi-quarter program, not a sprint.** HARD-14's *in-scope* deliverable (governance machinery + four spine increments + backlog ledger) is **L**; the full F40 fan-out (125 items) is the XL tail that HARD-14 schedules but does not finish.

Per-theme bands (from F40 §9, summed across each theme's `D-*`): WF L–XL, MA L, MR L, RET L, MCP M–L, SBX L, POL L, AUT M–L, INT L–XL, SEC L, OBS M–L, EVAL M–L, UI L, INF L–XL, DOC M, PM M, GEN M.

**Top risks:**
- **Protocol leakage (High).** Any V1 caller assuming FSM/pgvector/Docker/Postgres-vault specifics blocks the corresponding swap. Mitigation: the protocol-stability gate (AC3) must be green before any backend-swap increment.
- **Spine-ordering rework (Med-High).** Building a fan-out before its spine wastes work. Mitigation: the ledger encodes the spine graph; fan-out increments are blocked in CI until their spine flag exists.
- **Baseline regression via partial rollout (High).** Mitigation: the flag-off lane must equal the recorded baseline (AC2/AC6) — a regression there blocks merge regardless of flag-on results.
- **Scope creep into non-goals (Med).** The expression-language and auto-remediation temptations recur. Mitigation: AC7 totality test + the permanent-non-goal list rejected at review.

**Cannot be done in-sandbox (named, not hidden — these stay flag-on/deferred punch-list items):**
- A **real Temporal/K8s cluster** and a **multi-week multi-tenant soak** — WF-8 / INF-2 / SEC-5 are validated only against the Temporal *test env* and a *bounded simulated* soak (HARD-13); a real fleet is out of agent reach.
- **Firecracker / gVisor microVM isolation (D-SBX-3)** — kernel-level isolation cannot be exercised on the build host (esp. macOS); contract tests run, real microVM verification is a networked-Linux-runner punch-list item.
- **Real third-party SSO/SCIM IdP (D-SEC-1/2), real GitLab/Jira/Linear (D-INT-1/3), marketplace supply-chain signing (D-INT-6)** — require live external accounts/creds; gated, skip-clean, deferred.
- **GPU reranker scheduling (D-INF-8)** — needs GPU nodes.
- **Human pentest of the new high-risk surfaces** (MCP write, SBX escape, SSO) — HARD-09 produces the automated evidence + punch-list; the human engagement stays the program's named external gap.

## 10. Key files / paths (real monorepo) + F40→real mapping

**New (HARD-14 governance):**
- `packages/contracts/forge_contracts/features.py` — backend-selector enums + `FORGE_*` flag names (additive).
- `packages/contracts/forge_contracts/conditions.py` — shared `Condition`/`ConditionGroup`/`evaluate` (additive).
- `apps/api/forge_api/settings.py` + `deps.py` — selector resolution at the composition root (extend).
- `apps/worker/forge_worker/celery_app.py` — worker-side selector resolution (extend).
- `tests/test_protocol_stability.py`, `tests/test_feature_defaults_equal_baseline.py`, `tests/test_revert_to_green.py` (repo root).
- `packages/contracts/tests/test_frozen_surface_unchanged.py`, `test_condition_total_non_turing.py`.
- `.github/workflows/ci.yml` — dual-lane (flag-off blocking / flag-on gated); `future` marker in `pyproject.toml`.
- `docs/hardening/future-scope/mapping.md` + `docs/hardening/future-scope/ledger/<increment>.md` — mapping + revert-to-green ledger.

**F40 idealized path → real `forge_*` target (authoritative):**

| F40 idealized | Real target | Status |
|---|---|---|
| `packages/workflow-engine/{temporal,saga}/` | `packages/workflow-engine/forge_workflow/{temporal,saga}/` (alongside `engine.py`,`fsm.py`,`dsl.py`,`store.py`,`incident/`) | net-new submodules |
| `packages/agent-runtime/multi_agent/` | `packages/multi-agent-coordinator/forge_coordinator/` (**reserved stub — fill it**) + `packages/agent-runtime/forge_agent/` | scaffolded (stub) |
| `packages/agent-runtime/multi_repo/` | `packages/agent-runtime/forge_agent/runtime.py` (extend) | net-new |
| `packages/agent-runtime/sandbox/` | `packages/agent-runtime/forge_agent/sandbox.py` → `forge_agent/sandbox/{docker,k8s_job,firecracker,gvisor,warm_pool}.py` | scaffolded (worktree) |
| `packages/knowledge-core/.../search/{bm25_paradedb,colbert_reranker,qdrant}.py` | `packages/knowledge-core/forge_knowledge/` (extend `stores.py`,`reranker.py`; new modules) | net-new behind existing seams |
| `packages/knowledge-core/.../chunking/binary.py` | `packages/knowledge-core/forge_knowledge/` (`treesitter_chunking.py` **already exists**; add binary/OCR) | scaffolded (tree-sitter via HARD-11) |
| `packages/repo-policy/` | `packages/policy-sdk/forge_policy/` (`evaluator.py`,`loader.py`; add conditional/env-matrix/inheritance) | scaffolded |
| `packages/contracts/conditions/` | `packages/contracts/forge_contracts/conditions.py` (additive module) | net-new |
| `packages/forge-automation/` | `packages/automation/forge_automation/` (**net-new `forge_*` package**, follows naming) | net-new |
| `packages/pm-adapters/` | `packages/integration-sdk/forge_integrations/pm/` (`base`,`sync_engine`,`registry`,`transport`,`jira/`,`linear/` **already exist**; add asana/monday) | scaffolded |
| `packages/providers/gitlab/` | `packages/integration-sdk/forge_integrations/` (extend; `github.py` is the pattern) | net-new |
| `packages/verification/` + `packages/eval-harness/` | `packages/evaluation/forge_eval/` (extend) | scaffolded |
| `apps/api/app/api/v1/*` | `apps/api/forge_api/routers/*` | path remap |
| `apps/api/app/services/*` | `apps/api/forge_api/services/*` | path remap |
| `apps/api/app/models/*` | `packages/db/forge_db/models/*` (**singular** tables; `sub_agent_run`,`incident*`,`pm_*`,`sprint`,`milestone`,`epic` already exist) | scaffolded |
| `apps/api/alembic/versions/*` | `packages/db/migrations/versions/*` (`packages/db/alembic.ini`) | path remap |
| `apps/worker/*` | `apps/worker/forge_worker/*` (`celery_app.py`,`tasks/`,`agent_runner.py`,`syncer.py`,`indexer.py`) | path remap |
| `apps/api/app/services/integrations/{datadog,sentry,pagerduty,grafana}.py` | `packages/integration-sdk/forge_integrations/alerts/{datadog,sentry,pagerduty,grafana}.py` **already exist** | scaffolded |
| `apps/web/app/(app)/*` | `apps/web/` (Next.js; excluded from uv; `pnpm test` + `next build` gate) | path remap |
| `deploy/helm/`, `deploy/terraform/`, `deploy/socket-proxy/` | `deploy/` | net-new profiles |

## 11. Research references

- Backlog source of truth: `docs/implementation-slices/future/SPEC-FUTURE-deferred-scope.md` (125 `D-*` reqs, themes, sequencing, non-goals) and `docs/implementation-slices/future/F40-deferred-scope.md` (per-theme §3/§6/§7).
- Hardening contract: `scratchpad/hardening-docs/SPEC-PRODUCTION-HARDENING.md` (whole-suite green gate, BETA/PROD bars, creds handling, HARD-01/09/10/11/12/13 dependencies).
- Honest ground truth: `docs/MORNING_REPORT.md` §5 (PARKED items), §6 (gaps), §7 (ranked next steps).
- Product spec: `docs/FORGE_SPEC.md` — Phased Roadmap (V1 FSM → V2 Temporal; FTS → BM25; Docker → Firecracker; Celery → Temporal activities), Core Data Model, MCP Security Rules, Security table.
- `docs/forge-research-report.md` — Hybrid Retrieval (RRF k=60, pgvector < 1M → external store), **deterministic policy-driven supervisor** mandate, Temporal-vs-LangGraph durable-execution split.
- Slice format mirrored: `docs/implementation-slices/v1/F05-hybrid-knowledge-retrieval.md` (12-section format, protocol-behind-seam swap pattern).
- Backends: Temporal self-hosting https://docs.temporal.io/self-hosted-guide · Temporal vs LangGraph (Jun 2026) https://suhasbhairav.com/blog/temporal-vs-langgraph-durable-workflow-orchestration-vs-llm-agent-state-machines · ParadeDB `pg_search` https://github.com/paradedb/paradedb · ColBERT v2 https://github.com/stanford-futuredata/ColBERT · Qdrant https://github.com/qdrant/qdrant · LangGraph https://langchain-ai.github.io/langgraph/

## 12. Out of scope / future

**Owned by sibling hardening workstreams (not re-done here):** the specific `# PARKED:` un-parking — crypto/OAuth/secret-key (**HARD-10**), tree-sitter chunking + LangGraph verification (**HARD-11**), `uv lock` re-lock + Python 3.14-RC lane + eslint go/no-go (**toolchain workstream**), `docker compose build` + `next build` + `@sha256` pinning (**HARD-08**), the real external integrations' *live* paths (model/GitHub/MCP/Slack — **HARD-02/05/06/07**), perf/migration/soak (**HARD-13**), automated security evidence (**HARD-09**). HARD-14 *consumes* and *references* these; it owns only the broader F40 backlog execution governance.

**Inherited verbatim from F40 §12 — permanent non-goals (will NOT be built):**
- Expression language beyond declarative `ConditionGroup` — policy/automation evaluation stays total + non-Turing-complete; `ConditionGroup` is the ceiling.
- Editing bundled YAML/benchmark definitions in place — customization is always fork-into-workspace; benchmark/golden suites stay file-authored in git.
- New guards/effects without a code change + redeploy — the visual editor composes registered names only.
- Watchtower-style automatic image updates / unattended marketplace updates — deliberate, digest-pinned, admin-reviewed only.
- Incident auto-remediation without human approval; auto-approval of approval gates without a human; password / email-magic-link auth; WS-Federation / LDAP / AD direct bind.

**Explicitly deferred by HARD-14 (tracked in the ledger, not finished in this slice):** every F40 theme past the four V2 spine increments — the full WF/MA/MR/RET/MCP/SBX/POL/AUT/INT/SEC/OBS/EVAL/UI/INF/DOC/PM/GEN fan-out — each with a dated, owned, slice-linked deferral. Closing blocker #5 means the backlog is *mapped, gated, and scheduled with revert-to-green guarantees*, not that all 125 items ship in this slice. The cred-bearing and cluster-bound tail (real Temporal/K8s cluster, Firecracker microVM, real SSO/SCIM/GitLab/Jira/Linear, GPU scheduling, multi-week soak, human pentest of new surfaces) is named in §9 and stays a punch-list handoff.
