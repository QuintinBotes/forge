# Adaptive Orchestration + Spec Studio — Progress

Running record of the "hard finalise" adaptive-spec build
(`docs/MORNING_SUMMARY-2026-07-08.md`'s "Resuming the adaptive-spec build
(Spec Studio, adaptive orchestration, real-time/CRDT)"), against the whole-repo
green gate (ruff + ruff-format + mypy + full pytest on real pgvector + bandit +
gitleaks + web lint/build/test/typecheck).

**Phase 1 — Adaptive Orchestration: shipped, all six slices committed to
`main`.** **Phase 2 — Spec Studio: shipped, all fourteen slices committed to
`main`** — dual-format `spec.md`↔`manifest.yaml` round-trip, the five-mode
Spec Studio web editor (Guided/Markdown/YAML/Read/History), BYOK AI drafting,
external import, acceptance-criterion styles, and version history + diff all
landed. **Real-time co-editing is still design-only** (Yjs chosen, nothing
wired) — see "What's parked" below. Full design: `docs/spec-studio/DESIGN.md`.

## Slice ledger

### Phase 1 — Adaptive Orchestration

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

### Phase 2 — Spec Studio

| id | refuted | repaired | decision | commit | parked |
|---|---|---|---|---|---|
| ss-parser | 0 | no | committed | `19d165a` | — |
| ss-engine | 1 | no | committed | `dee72e1` | — |
| ss-endpoints | 3 | yes | committed | `26c938f` | — |
| ss-yaml | 1 | no | committed | `899a5e9` | — |
| ss-draft | 3 | yes | committed | `bec9813` | — |
| ss-guided | 2 | yes | committed | `13ae1ce` | — |
| ss-markdown | 1 | no | committed | `08af69e` | — |
| ss-read | 1 | no | committed | `d8bfb51` | reject/request-changes have no backend persistence |
| ss-lifecycle | 2 | yes | committed | `a9f381f` | kubeconform network tests (pre-existing, unrelated) |
| ss-ai-panel | 2 | yes | committed | `777e51f` | — |
| ss-entry | 1 | no | committed | `c081a3c` | — |
| ss-versioning | 2 | yes | committed | `24f5601` | final full-repo pytest confirmation |
| ss-import | 2 | yes | committed | `7f1bebc` | final full-repo pytest confirmation |
| ss-criteria | 0 | no | committed | `804a840` | — |

`refuted`/`repaired` are taken verbatim from each slice's own completion
report at build time (21 findings raised across the 14 slices; 7 slices had
at least one repaired before commit — `ss-endpoints`, `ss-draft`,
`ss-guided`, `ss-lifecycle`, `ss-ai-panel`, `ss-versioning`, `ss-import` — the
other 7 had findings investigated and held as not requiring a change, or none
raised). **Committed:** all fourteen. **Reverted:** none. The two
"final full-repo pytest confirmation" parked items (`ss-versioning`,
`ss-import`) asked for an uncontaminated full-suite rerun after a background
run hadn't finished before their own report was due — that rerun is the one
recorded in "Gate confirmation" below, performed for *this* report with
nothing else concurrently touching the test database; see that section for
the result. The `ss-read` and `ss-lifecycle` parked items are unresolved by
design/scope respectively — carried forward, see "What's parked" below.

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

## What shipped — Spec Studio

The dual-format design (`SpecManifest` canonical, `spec.md`/`manifest.yaml`
both first-class editable views) is now fully wired end-to-end, plus the web
editor, BYOK drafting, external import, criterion styles, and version
history:

- **`ss-parser`** (`19d165a`) — `forge_spec.markdown.parse_spec_md`: parses
  the frontmatter + `## Goal`/`## Requirements`/`## Acceptance Criteria`
  (Given/When/Then)/`## Constraints`/`## Open Questions`/`## Decisions`
  shape back into a `SpecManifest`, completing the round-trip the design
  called for (`render_spec_md` already emitted this shape). `SpecParseError`
  reports malformed input.
- **`ss-engine`** (`dee72e1`) — `FileSpecEngine` gains `save_spec_md`/
  `read_spec_md`/`save_manifest_yaml`/`read_manifest_yaml`: editing either
  serialization parses it back to a `SpecManifest`, then re-renders and
  writes *both* files from that single manifest, so `spec.md` and
  `manifest.yaml` never drift apart. Legacy manifest-only specs still load.
- **`ss-endpoints`** (`26c938f`) — `apps/api` `GET/PUT /spec/specs/{id}` (raw
  manifest), `GET/PUT /spec/specs/{id}/markdown`, and
  `GET/PUT /spec/specs/{id}/manifest` — the HTTP surface for
  create/edit-from-either-format, plus a typed web API client
  (`apps/web/src/lib/api/client.ts`).
- **`ss-yaml`** (`899a5e9`) — the first cut of the Spec Studio web component
  (`apps/web/src/components/spec-studio`): a mode-switching shell plus the
  YAML editor mode with client-side schema validation
  (`lib/spec-studio/yaml-schema.ts`).
- **`ss-draft`** (`bec9813`) — `POST /spec/draft`: resolves the
  `spec_author` role through the Adaptive Orchestration model router built
  in Phase 1, streams a constitution-seeded draft through the BYOK
  `ModelClient`, and returns a parsed `SpecManifest` preview plus token/cost
  accounting. Draft-only — nothing is persisted until a human saves it
  through the normal editing endpoints. `ModelClient` is mocked in tests.
- **`ss-guided`** (`13ae1ce`) — the Guided-mode form editor (structured
  requirement/AC/constraint fields, no raw text), `/specs/new` and
  `/specs/{id}` pages, and the `spec-studio-page` wrapper that wires the
  editor to a real spec id.
- **`ss-markdown`** (`08af69e`) — the Markdown editor mode plus
  `lib/spec-studio/markdown-parse.ts` (the web-side mirror of
  `parse_spec_md`, used for client-side live preview/validation before the
  save round-trips through the API).
- **`ss-read`** (`d8bfb51`) — the Read mode: a rendered, non-editable view
  of a spec with a keyboard-driven approval gate (`a`/`x`/`r` for
  approve/reject/request-changes + a note). Approve calls the real
  `POST /spec/specs/{id}/approve`; reject/request-changes are recorded
  locally only (see "What's parked").
- **`ss-lifecycle`** (`a9f381f`) — replaced `lifecycle-rail` with
  `lifecycle-stepper`, a clearer draft→clarified→planned→approved status
  stepper wired into `spec-dashboard` and the Spec Studio page header.
- **`ss-ai-panel`** (`777e51f`) — the `AiDraftPanel`: a streaming "typing"
  reveal of a `POST /spec/draft` response with an accept action that seeds
  the Guided/Markdown editor from the draft, wired into `/specs/new`.
- **`ss-entry`** (`c081a3c`) — the `/specs/new` entry flow: choose an epic
  (or create one inline), pick a starter template (feature/bugfix/spike —
  `lib/spec-studio/templates.ts`) or start from an AI draft, then land in
  Guided mode with the seed applied.
- **`ss-versioning`** (`24f5601`) — `spec_version` table (migration `0032`,
  additive/reversible): every save through the editing endpoints records an
  immutable snapshot (manifest + both serializations). `GET
  /spec/specs/{id}/versions`, `.../versions/{n}`, and
  `.../versions/{from}/diff/{to}` (line-level markdown diff +
  id-keyed structured manifest diff, `forge_spec.diff`) back the web
  `VersionHistory` panel.
- **`ss-import`** (`7f1bebc`) — `POST /spec/import`: turns an
  externally-authored markdown or YAML spec into a `spec.md` draft via
  direct parse → best-effort normalize → graceful-failure-with-`parse_error`
  fallback, so existing docs can enter the SDD lifecycle without retyping.
  Draft-only, same contract as `ss-draft`.
- **`ss-criteria`** (`804a840`) — `forge_spec.criteria`: acceptance criteria
  can be authored in three styles — Gherkin (Given/When/Then, the default),
  a plain declarative assertion, or a `- [ ]`/`- [x]` checklist — all
  encoded losslessly in the existing `AcceptanceCriterion.text` field
  (style is derived via `classify_criterion`, never stored, so the frozen
  contract and `req_refs` linking are untouched). Wired into Guided mode and
  `spec.md` rendering/parsing.

Net result: a spec can be created from scratch, a template, an AI draft, or
an external import; edited in Guided, Markdown, or YAML mode with both files
always in sync; reviewed in Read mode; approved through the real gate; and
every save is a recoverable, diffable version.

### Known limitation, by design (not a gap)

`POST /spec/draft` and `POST /spec/import` are both **draft-only** — neither
persists anything. This matches the approved design exactly (a human always
refines and explicitly saves through the normal spec-editing endpoints), not
an oversight.

## What's parked — Spec Studio

- **Reject / Request-changes have no backend persistence.** Read mode's
  approval gate is fully keyboard-driven (`a`/`x`/`r`) and calls optional
  `onReject`/`onRequestChanges` callbacks, but records the decision + note
  only in the browser — `forge_spec.FileSpecEngine` exposes `approve_spec`
  (wired to the real `POST /spec/specs/{id}/approve`) with no
  `reject_spec`/`request_changes` counterpart, and the frozen `SpecStatus`
  enum has no such values. Unblock: add
  `POST /spec/specs/{id}/reject` and `.../request-changes` to
  `forge_spec/engine.py` + `apps/api/forge_api/routers/spec.py` (mirroring
  `approve_spec`), or wire the existing F36 `/approvals` generic gate
  (`gate_type='spec'`) once `ApprovalSummary`/`ApprovalRequest` gain a way to
  resolve the pending gate for a given `spec_id`.
- **Real-time co-editing — still design-only, nothing wired.** Repo-evidence
  check performed for this report: no `yjs`/`y-websocket` dependency in
  `apps/web/package.json`, no `/ws` route or CRDT/OT dependency anywhere in
  `apps/api`. **Library choice (design decision, unchanged from Phase 1):
  Yjs** — CRDT, no central sequencing server, mature markdown/text-editor
  bindings, zero-runtime-dep core; rejected Automerge (heavier WASM payload
  for this use case), Operational Transform (needs a central sequencing
  server that conflicts with the stateless API/worker split and with an
  agent editing the same file outside the OT server's view), and vendor
  real-time services (external network dependency incompatible with the
  self-hosted/BYOK deploy story). Full rationale and the relay-transport
  shape: `docs/spec-studio/DESIGN.md` §4. This is the same deferred `/ws`
  websocket noted in `docs/MORNING_SUMMARY-2026-07-08.md` as the "`rt-ws`
  real-time slice" — one relay substrate would serve both that
  public-readiness item and Spec Studio co-editing. Unblock:
  `spec-studio-realtime` (`docs/spec-studio/DESIGN.md` §2.2) — the editor it
  co-edits (Guided/Markdown/YAML modes, `parse_spec_md` round-trip) now
  exists, so this slice is unblocked and ready to start.
- **Historical note, now resolved for this environment** (carried from
  `ss-lifecycle`'s own report): that report saw
  `deploy/helm/tests/test_render_contract.py::test_kubeconform_conformance[...]`
  fail in its sandbox for lack of network access to fetch k8s JSON schemas.
  Re-run explicitly for this report's gate confirmation, those same tests
  **passed** (`kubeconform` was on `PATH` and reached its schema store here)
  — not touched by any Spec Studio slice either way; flagged only so the
  discrepancy between the two sandboxes' network posture is on record.

## Gate confirmation

### Phase 1 (Adaptive Orchestration) — as originally recorded

Full green-gate run performed at the time (2026-07-08, working tree clean at
`1c048d8`): `uv run ruff check .` clean; `uv run ruff format --check .` clean
(953 files); `make typecheck` 0 errors across 486 source files;
full pytest **3868 passed, 53 skipped, 0 failed** in 814.37s; `bandit` exit
0; `gitleaks` no leaks (169 commits); `pnpm lint` 0 errors (6 pre-existing
warnings); `pnpm build` 19 routes; `pnpm test` 507 passed (66 files); `pnpm
typecheck` clean; no hardcoded hex/rgb in the Adaptive Orchestration web
files.

### Phase 2 (Spec Studio) — this report, whole repo re-verified

Full green-gate run performed for **this** report, working tree clean at
`804a840` (all 14 `ss-*` slices):

- `uv run ruff check .` — clean.
- `uv run ruff format --check .` — clean (967 files already formatted).
- `make typecheck` (mypy, all 18 first-party packages) — 0 errors across 493
  source files.
- `FORGE_TEST_DATABASE_URL=postgresql+psycopg://forge:forge@localhost:5433/forge
  uv run pytest -q` — full suite against real pgvector on `:5433`, run
  standalone (nothing else touching the DB concurrently) to avoid the
  DB-contention artifacts noted in the `ss-versioning` slice's own report:
  **3980 passed, 53 skipped, 0 failed, 23 warnings in 942.73s (15m42s)**.
  This resolves the `ss-versioning`/`ss-import` parked "final full-repo
  pytest confirmation" items. Every skip is a documented opt-in/live-cred/
  virtualization-gated lane (e.g. `FORGE_RUN_SOAK`/`FORGE_RUN_PERF`/
  `FORGE_BUILD_INTEGRATION_TESTS`, live GitHub/Slack/MCP/reranker/
  model-provider creds, gVisor/Firecracker kernel-boundary tests,
  `promtool`/`amtool` not on `PATH`) — none are Spec Studio related. The
  `deploy/helm` kubeconform tests `ss-lifecycle`'s own report carried
  forward as network-blocked in a different sandbox were re-run explicitly
  in *this* environment (`pytest deploy/helm/tests/test_render_contract.py
  -k kubeconform`) and **passed** (3 passed) — `kubeconform` is on `PATH`
  and reached its schema store here, so that note no longer applies to this
  gate run (kept in "What's parked" only as historical context).
- `uv run bandit -c pyproject.toml -r packages apps --severity-level high -q`
  — exit 0.
- `gitleaks detect --source . --config .gitleaks.toml --no-banner --redact` —
  no leaks (188 commits scanned).
- `pnpm --filter @forge/web lint` — 0 errors (12 pre-existing warnings,
  unrelated to Spec Studio files — `pm-integrations-view.tsx`,
  `members-panel.tsx`, `step-meta.ts`, `walkthrough-view.tsx`,
  `workflow-canvas.tsx`).
- `pnpm --filter @forge/web build` — succeeds (20 routes, including
  `/specs`, `/specs/new`, `/specs/[id]`).
- `pnpm --filter @forge/web test` — 673 passed (81 test files).
- `pnpm --filter @forge/web typecheck` — clean.
- No hardcoded hex/rgb color literals across the full Spec Studio web diff
  (all `apps/web/src/components/spec-studio/**`, `apps/web/src/lib/
  spec-studio/**`, and the touched `spec`/`lib/api` files) — design tokens
  only.
- Migration `0032_ss_versioning_spec_version` (the one new migration
  introduced across all 14 slices) applies and reverses cleanly against real
  Postgres on `:5433`: verified in an isolated scratch database on the same
  server (`forge_migration_check`, dropped after) so the shared test DB's
  fixture-managed state was never touched — `alembic upgrade
  0031_ao_observability_cost_tier` (full baseline chain, 0001→0031) then
  `upgrade head` created `spec_version` with its indexes/unique constraint/
  FK exactly as declared; `downgrade 0031_ao_observability_cost_tier`
  dropped it cleanly (`\d spec_version` → "did not find any relation"); a
  final `upgrade head` re-created it, confirming a full round-trip.

`git log --oneline | head`:

```
804a840 feat(ss-criteria): Spec Studio
7f1bebc feat(ss-import): Spec Studio
24f5601 feat(ss-versioning): Spec Studio
c081a3c feat(ss-entry): Spec Studio
777e51f feat(ss-ai-panel): Spec Studio
a9f381f feat(ss-lifecycle): Spec Studio
d8bfb51 feat(ss-read): Spec Studio
08af69e feat(ss-markdown): Spec Studio
13ae1ce feat(ss-guided): Spec Studio
bec9813 feat(ss-draft): Spec Studio
```

## Next steps

Phase 2's remaining item, per `docs/spec-studio/DESIGN.md` §2.2/§4:
`spec-studio-realtime` (Yjs co-editing over the shared `/ws` relay — the
same substrate as the `rt-ws` slice noted as deferred in
`docs/MORNING_SUMMARY-2026-07-08.md`, so it should land once, serving both).
Its prerequisites (`spec-md-roundtrip`, the Guided/Markdown/YAML editor) are
now shipped, so it is unblocked. Independently: the `ss-read` reject/
request-changes backend persistence gap above.
