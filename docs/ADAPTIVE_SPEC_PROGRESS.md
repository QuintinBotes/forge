# Adaptive Orchestration + Spec Studio — Progress

Running record of the "hard finalise" adaptive-spec build
(`docs/MORNING_SUMMARY-2026-07-08.md`'s "Resuming the adaptive-spec build
(Spec Studio, adaptive orchestration, real-time/CRDT)"), against the whole-repo
green gate (ruff + ruff-format + mypy + full pytest on real pgvector + bandit +
gitleaks + web lint/build/test/typecheck).

**Phase 1 — Adaptive Orchestration: shipped, all six slices committed to
`main`.** **Phase 2 — Spec Studio (dual-format `spec.md` round-trip,
BYOK spec draft, real-time co-editing): not yet started** — design-approved
and written up in `docs/spec-studio/DESIGN.md` for the next build phase.

## Slice ledger

| id | phase | refuted | repaired | decision | commit |
|---|---|---|---|---|---|
| ao-config | Adaptive Orchestration | 0 | no | committed | `ba33d6d` |
| ao-policy | Adaptive Orchestration | 0 | no | committed | `b539082` |
| ao-effort | Adaptive Orchestration | 0 | no | committed | `37428c2` |
| ao-settings-api | Adaptive Orchestration | 0† | no | committed | `213a1b4` |
| ao-settings-ui | Adaptive Orchestration | 1 | no | committed | `1cedd4e` |
| ao-observability | Adaptive Orchestration | 1 | no | committed | `1c048d8` |

`refuted`/`repaired` for **ao-settings-ui** and **ao-observability** are taken
verbatim from those slices' own completion reports (both refuted findings were
investigated and held as not requiring a code change — "repaired: false" — and
both slices were still committed). `ao-config`/`ao-policy`/`ao-effort` predate
this reporting convention in-tree; their rows are reconstructed from repo
evidence (clean additive diffs, no revert/fixup commits between them and the
next slice, and the whole-repo gate re-verified green after each — see
"Gate confirmation" below) and are reported as `0`/`no` on that basis, not
from a preserved review transcript. †`ao-settings-api`'s commit message notes
it was "recovered from a verifier's stray git-stash" — a process hiccup during
verification, not a refuted finding; the commit body records the gate as green
at 3863 tests before landing.

**Committed:** all six. **Reverted:** none.

## What shipped — Adaptive Orchestration

A policy sizes a task/spec into `{tier: junior|medior|senior, strategy:
single|swarm}`; a per-role config (planner/coder/reviewer/spec_author/
coordinator) resolves each role's `{model_or_tier, effort}` with human
overrides at workspace- and project-scope; a provider-agnostic model router
maps `tier -> concrete model` per BYOK provider; effort maps onto each
provider's native "thinking" knob; a settings API + web UI exposes all of it;
observability's cost ledger carries `tier`/`strategy` columns end-to-end
(pipeline ready — see "known gap" below).

- **`ao-config`** (`ba33d6d`) — `forge_contracts.orchestration_config`:
  `AgentRole`, `Effort`, `RoleModelConfig`/`RoleConfigOverride`/
  `EffectiveRoleConfig`, `DEFAULT_ROLE_CONFIG`, and the `RoleConfigStore`
  Protocol. `forge_db`: `role_config` table (migration `0029`) +
  `SqlRoleConfigStore`. `forge_orchestration_policy.role_config`: the
  workspace/project-override resolver.
- **`ao-policy`** (`b539082`) — `forge_agent.execution_plan.ExecutionPlan`:
  sizes a spec/task via the existing complexity policy and resolves
  `for_role(role)` to an effective `{tier, effort, strategy}`; wired into
  `forge_coordinator` (`deps.py`, `nodes.py`, `selector.py`, `state.py`,
  `supervisor.py`) so a swarm run is sized once and every role reads off the
  same plan.
- **`ao-effort`** (`37428c2`) — `forge_agent.providers.effort`: translates the
  provider-agnostic `low|medium|high|max` effort into each BYOK provider's
  native parameter (Anthropic extended-thinking budget, OpenAI reasoning
  effort), reused by both `openai_client.py` and the shared
  `providers/translate.py` request-building path — no new model-call code
  path, extends the existing HARD-02 translate layer.
- **`ao-settings-api`** (`213a1b4`) — `apps/api` `/ao` router: `GET/PUT/DELETE
  /ao/role-config[/{role}]`, `GET/PUT /ao/settings` (auto-route toggle,
  `tier -> model` overrides, complexity thresholds), `POST
  /ao/routing-preview` (dry-run: what tier/model/strategy a sample task would
  get today). Reads are `Permission.READ`; every mutation is
  `Permission.ADMIN` (workspace-wide routing config, not a per-member
  setting — same precedent as the cost price-book endpoints). Backed by
  `forge_db.ao_settings.SqlAoSettingsStore` (migration `0030`) + a typed web
  API client (`apps/web/src/lib/api/{client,types}.ts`).
- **`ao-settings-ui`** (`1cedd4e`) — `/settings/models` page
  (`apps/web/src/components/ao-settings/ao-settings-view.tsx`): per-role
  model/effort editors with an inherited-vs-overridden indicator, the
  auto-route toggle, provider tier→model override fields, complexity
  threshold fields, and a live routing-preview panel calling
  `POST /ao/routing-preview` as you type. Wired into the app shell and
  command palette.
- **`ao-observability`** (`1c048d8`) — `ModelUsage` gains `tier: str | None`
  and `strategy: str | None` (migration `0031`); `CostBucket` gains
  `request_count` (call counts per bucket, not just spend); `GET
  /cost/summary` and `GET /cost/timeseries` accept `group_by=tier` and
  `group_by=strategy` alongside the existing `phase|provider|model`; the
  observability web view and its metrics helper render the new groupings.

### Model router — default map and how to configure

`forge_agent.providers.router.DEFAULT_TIER_MODELS` (per provider, overridable
per tier):

| tier | Anthropic (default) | OpenAI (default) |
|---|---|---|
| junior | `claude-haiku-4-5` | `gpt-4.1-mini` |
| medior | `claude-sonnet-5` | `gpt-4.1` |
| senior | `claude-opus-4-8` | `o3` |

Per-role defaults (`forge_contracts.orchestration_config.DEFAULT_ROLE_CONFIG`,
before any override): planner/reviewer/coordinator default to `senior` @
`high` effort; coder/spec_author default to `medior` @ `medium` effort
(escalated to `senior` by the sizing policy for complex work).

**How to configure** (three layers, most-specific wins):

1. **Per-role override** — `PUT /ao/role-config/{role}` (optionally scoped to
   a `project_id`) pins a role to a literal `model_or_tier` (a tier keyword
   like `"senior"`, or a concrete model id such as `"claude-opus-4-8"`) and an
   `effort`. `DELETE` reverts to the next fallback. The
   `/settings/models` web UI is the human-facing surface for this.
2. **Workspace-wide tier→model overrides** — `PUT /ao/settings` with
   `tier_model_overrides: {provider: {tier: model}}` remaps a tier to a
   different concrete model for the whole workspace without touching
   per-role pins (e.g., "route every `senior` call to a specific pinned Opus
   snapshot").
3. **Complexity thresholds** — the same `PUT /ao/settings` call accepts
   `junior_max`/`medior_max` to retune where the sizing policy's complexity
   score crosses from junior→medior→senior; unset falls back to
   `forge_orchestration_policy.complexity`'s hardcoded defaults
   (`junior_max_is_default`/`medior_max_is_default` in the response tell the
   UI which fields are inherited).

`auto_route` (default `true`) is the workspace kill-switch: when `false`,
Adaptive Orchestration sizing is bypassed entirely and every role runs at its
configured (or default) tier with no complexity-based escalation — an escape
hatch for workspaces that want fully manual control.

Programmatically: `ModelRouter(provider=..., tier_models={...})` merges a
partial override onto `DEFAULT_TIER_MODELS` for any code path that isn't
going through the settings API (e.g., a script or a future CLI).

### Known gap (carried forward, not introduced by these slices)

No production call site yet stamps `tier`/`strategy` onto a real `ModelUsage`
from a live agent run: `forge_agent`/`forge_coordinator` construct an
`ExecutionPlan` and resolve routing decisions, but do not yet call
`UsageMeter`/construct a `ModelUsage` anywhere in-tree (only
`apps/api/forge_api/cli_cost.py`, the worker observability init, and tests do
today) — the cost ledger is not yet wired into the live agent-run/model-client
path. The `ao-observability` slice's schema/API/dashboard are ready to receive
`tier`/`strategy` the moment that wiring lands; unblock with a follow-up slice
that wires `ExecutionPlan.for_role(...).tier`/`.strategy` into the
`ModelUsage` built at each model-client call site.

## What's parked — Spec Studio (not yet started)

Design is written up in full in `docs/spec-studio/DESIGN.md`; nothing in this
section has code yet. Repo-evidence check performed for this report: no
`parse_spec_md`, no `POST /spec/draft` route/schema/service, no
`spec-studio`-named web component, and no websocket/CRDT dependency exist
anywhere in the tree (`git log --all` has no `spec-studio`, `co-editing`, or
`websocket` commit either — this phase has not begun on any branch).

- **Dual-format spec authoring** — `manifest.yaml` round-trip
  (`dump_manifest`/`load_manifest`) is shipped and has been for several
  phases; `spec.md` rendering (`render_spec_md`) is shipped but **one-way**
  (manifest → markdown only, and without the frontmatter/`## Goal`/
  Given-When-Then/`## Decisions` shape the approved design calls for).
  `parse_spec_md` (markdown → manifest) does not exist, so editing `spec.md`
  today does not update the canonical `SpecManifest` or re-render
  `manifest.yaml`. Unblock: the `spec-md-roundtrip` slice in
  `docs/spec-studio/DESIGN.md` §2.2.
- **`POST /spec/draft` (BYOK AI draft)** — no route, schema, or service
  exists. Unblock: the `spec-draft-api` slice (§2.2), which resolves the
  `spec_author` role through the Adaptive Orchestration router built above
  and streams through the existing BYOK `ModelClient` (mocked in tests, per
  the approved design — no live key in CI).
- **Spec Studio web UI** — `apps/web` has a read-only validation dashboard
  (`components/spec/spec-dashboard.tsx`) but no editor. Unblock:
  `spec-studio-ui` (§2.2).
- **Real-time co-editing** — no `/ws` route, no CRDT/OT dependency anywhere
  in `apps/api` or `apps/web`. **Library choice (design decision, not yet
  wired): Yjs** — CRDT, no central sequencing server, mature markdown/
  text-editor bindings, zero-runtime-dep core; rejected Automerge (heavier
  WASM payload for this use case), Operational Transform (needs a central
  sequencing server that conflicts with the stateless API/worker split and
  with an agent editing the same file outside the OT server's view), and
  vendor real-time services (external network dependency incompatible with
  the self-hosted/BYOK deploy story). Full rationale and the relay-transport
  shape: `docs/spec-studio/DESIGN.md` §4. This is the same deferred `/ws`
  websocket noted in `docs/MORNING_SUMMARY-2026-07-08.md` as the "`rt-ws`
  real-time slice" — one relay substrate serves both that public-readiness
  item and Spec Studio co-editing. Unblock: `spec-studio-realtime` (§2.2),
  sequenced after `spec-md-roundtrip` and `spec-studio-ui` exist to co-edit.

## Gate confirmation

Full green-gate run performed for this report (2026-07-08, working tree
clean at `1c048d8`):

- `uv run ruff check .` — clean.
- `uv run ruff format --check .` — clean (953 files already formatted).
- `make typecheck` (mypy, all 18 first-party packages) — 0 errors across 486
  source files.
- `FORGE_TEST_DATABASE_URL=postgresql+psycopg://forge:forge@localhost:5433/forge
  uv run pytest -q` — full suite against real pgvector on `:5433`: **3868
  passed, 53 skipped, 0 failed, 23 warnings in 814.37s (13m34s)**. Every skip
  is a documented opt-in/live-cred/virtualization-gated lane (e.g.
  `FORGE_RUN_SOAK`/`FORGE_RUN_PERF`/`FORGE_BUILD_INTEGRATION_TESTS`,
  live GitHub/Slack/MCP/reranker/model-provider creds, gVisor/Firecracker
  kernel-boundary tests, `promtool`/`amtool` not on `PATH`) — none are
  Adaptive Orchestration or Spec Studio related.
- `uv run bandit -c pyproject.toml -r packages apps --severity-level high -q`
  — exit 0.
- `gitleaks detect --source . --config .gitleaks.toml --no-banner --redact` —
  no leaks (169 commits scanned).
- `pnpm --filter @forge/web lint` — 0 errors (6 pre-existing warnings,
  unrelated to Adaptive Orchestration files).
- `pnpm --filter @forge/web build` — succeeds (19 routes, including
  `/settings/models`).
- `pnpm --filter @forge/web test` — 507 passed (66 test files).
- `pnpm --filter @forge/web typecheck` — clean.
- No hardcoded hex/rgb color literals in the Adaptive Orchestration web files
  (`ao-settings-view.tsx`, `settings/models/page.tsx`, `lib/api/ao-settings.ts`)
  — design tokens only.

`git log --oneline | head`:

```
1c048d8 feat(ao-observability): Adaptive Orchestration
1cedd4e feat(ao-settings-ui): Adaptive Orchestration
213a1b4 feat(ao-settings-api): per-role model+effort settings endpoints, store, migration + web client
37428c2 feat(ao-effort): Adaptive Orchestration
b539082 feat(ao-policy): Adaptive Orchestration
ba33d6d feat(ao-config): Adaptive Orchestration
98bfab7 docs: progress summary — public-readiness merged, hard finalise starting
4a2dac0 feat: public-readiness — under-dev banner, honest status, live spec dashboard (#30)
2afb8f7 chore(deps): bump astral-sh/setup-uv from 5.4.2 to 8.3.1 (#24)
25732b9 chore(deps): bump actions/checkout from 4.2.2 to 7.0.0 (#25)
```

## Next steps (Phase 2)

In priority order, per `docs/spec-studio/DESIGN.md` §2.2: `spec-md-roundtrip`
→ `spec-draft-api` → `spec-studio-ui` → `spec-studio-realtime`. The `rt-ws`
real-time slice noted as deferred in `docs/MORNING_SUMMARY-2026-07-08.md`
should land as the same relay substrate `spec-studio-realtime` needs, not as
a second websocket implementation.
