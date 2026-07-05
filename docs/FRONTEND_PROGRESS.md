# Frontend Progress — Forge Web (`apps/web`)

Status of the 15 product screens built against the Forge design system
(ember-on-graphite-steel) and the existing FastAPI routers. Every screen ships
with real Vitest + Testing-Library component tests (render, key interactions,
empty / loading / error states) and uses only Forge design tokens — no hardcoded
hex/rgb.

## Green gate

- `pnpm --filter @forge/web build` — **green (exit 0)**; all routes render (see route table below).
- `pnpm --filter @forge/web test` — passing per-screen.
- `pnpm --filter @forge/web typecheck` — clean **except** one pre-existing, unrelated `tsc` error in `apps/web/src/lib/security-headers.test.ts` (see Board depth park note); `next build`'s own TypeScript pass succeeds.
- No hardcoded hex/rgb in added files (grep-verified).

## Screen ledger

| id | title | route | refuted | repaired | decision |
|----|-------|-------|:------:|:--------:|----------|
| board-depth | Board depth | `/depth` | 1 | no | committed |
| approval-inbox | Approval inbox | `/approvals` | 0 | no | committed |
| run-trace-viewer | Run-trace viewer | `/runs/[[...run]]` | 0 | no | committed |
| spec-dashboard | Spec-validation dashboard | `/specs` | 0 | no | committed |
| marketplace | Marketplace | `/marketplace` | 0 | no | committed |
| incidents | Incidents | `/incidents` | 0 | no | committed |
| observability | Observability & cost | `/observability` | 0 | no | committed |
| sprints | Sprints & velocity | `/sprints` | 0 | no | committed |
| audit-log | Audit viewer | `/audit` | 0 | no | committed |
| deployment-gates | Deployment gates | `/deployments` | 0 | no | committed |
| sso-settings | SSO / SCIM settings | `/settings/sso` | 0 | no | committed |
| rbac-admin | Multi-team & RBAC admin | `/settings/rbac` | 0 | no | committed |
| pm-integrations | PM integrations | `/settings/integrations` | 0 | no | committed |
| workflow-editor | Workflow visual editor | `/workflow` | 0 | no | committed |
| walkthrough | In-app guided walkthrough | `/walkthrough` | 0 | no | committed |

## Built vs reverted vs parked

- **Built & committed (15 of 15):** all screens above. Each has its own feature
  commit (`git log --oneline`: `e3acac3 board-depth` … `d49cd6f walkthrough`).
- **Reverted (0):** none. No screen was rolled back.
- **Parked items (5 screens carry disclosed, non-faked gaps):** board-depth,
  spec-dashboard, observability, sso-settings, pm-integrations — detailed below.
  Parks are honest degraded states or backend-not-yet-wired disclosures, never
  skipped/deleted tests and never fabricated data.

Only **board-depth** is a partial gate (one refuted claim, and a pre-existing
unrelated `typecheck` error); the other 14 pass their gate cleanly.

## Remaining UI gaps (parked — disclosed, not faked)

### board-depth — `typecheck` blocked by a pre-existing unrelated error
`tsc --noEmit` exits non-zero on `apps/web/src/lib/security-headers.test.ts(3,1)`
— *"Unused `@ts-expect-error` directive"* — a file I never touched. Proven
pre-existing by stashing my edits + relocating my new files and reproducing the
identical error on clean `HEAD`. My own files are `tsc`-clean and `next build`'s
TypeScript pass succeeds. Left unfixed as out-of-scope (belongs to the Next 16 /
TS 6 upgrade workstream). **Fix:** delete the now-unused `// @ts-expect-error` on
line 3 of that file.

### spec-dashboard — backend projection endpoint missing
The dashboard is built to a `GET /projects/{id}/specs` projection (constitution +
`specs[]` each with an embedded `ValidationReport`). The live `/spec` router today
exposes only per-spec reads (`GET /spec/specs/{id}`) and per-task `POST validate`.
Tests mock the F02/F23 contract; against the live backend the screen renders its
graceful **"Live specs are unavailable"** degraded state until that projection is
wired.

### observability — true latency percentiles unavailable
The F38 `/observability/metrics` Prometheus scrape emits histogram `_count`+`_sum`
only (no buckets/quantiles), and no other router serves aggregate p50/p95/p99.
Computing them would require fabricating data (PARK-DON'T-FAKE), so the latency
panel shows **real per-stage mean + throughput** from the live scrape and says so
inline. Unblocked by the backend follow-up (histogram-bucket exposition, noted in
`docs/self-hosting/performance.md`).

### sso-settings — several admin affordances parked to keep the screen focused
- SAML connection-test UI (`POST /sso/test`): client `testSsoConfig` wired, no UI (a raw-SAMLResponse debug affordance).
- OIDC: F33 backend is SAML-only, so OIDC is shown honestly as a disabled **"Soon"** protocol, not faked.
- Attribute-mapping / group→role editor: existing values round-trip on save; no dedicated editor (server defaults apply).
- Delete SSO config (`DELETE /sso`): client method exists; only the reversible **disable** path is surfaced.
- Per-domain DNS/email verification: backend marks admin-asserted domains `verified=true` (no server-side challenge flow); the live HRD probe is the routing proof.
- Workspace id: uses `DEFAULT_WORKSPACE_ID = "default"` (mirrors the board's `DEFAULT_PROJECT_ID`) until workspace routing lands.

### pm-integrations — backend-parked write paths reflected honestly in the UI
F18 `pm.py` parks OAuth code-exchange, backfill enqueue, and manual
conflict-resolve execution. The UI mirrors this rather than faking calls: OAuth
connect saves a **pending** connection with a "finish authorization with the
provider" note (no code-exchange), and the conflict inbox reads via
`GET /connections/{id}/links?state=conflict` with a per-row **"Open in Jira/Linear"**
external link (no resolve endpoint invoked). No frontend test is skipped or faked.

## Notes

- Shared-file edits (nav/sidebar, `src/lib/api` client, layout) were kept minimal
  and additive; each screen lives in its own route dir + components.
- All screens are keyboard-reachable, use the Cmd+K palette surface, optimistic
  updates where applicable, and token-only styling.
