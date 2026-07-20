# Audit Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close every fixable gap found in the 2026-07-20 whole-project audit: wrong/stale docs, dead config knobs, unrun CI lanes, and four "looks wired but silently does nothing" code paths, plus missing trust-layer surfaces and docs.

**Architecture:** Four phases. Phase 1 makes docs/config tell the truth (independent file sets, parallel-safe). Phase 2 makes CI actually gate what exists. Phase 3 wires the silent-failure code paths. Phase 4 completes the trust-layer surfaces (REST/UI). Each task is independently reviewable and committed separately.

**Tech Stack:** Python 3.12 + uv workspace (FastAPI, Celery, SQLAlchemy/Alembic, pydantic), Next.js 16/React 19 + vitest (`apps/web`), GitHub Actions, docker compose, Caddy/nginx.

## Global Constraints

- Branch: all work lands on `audit-remediation` (branched from `main` @ `24832c7`). One conventional commit per task — `cz check` gates PR commits, so messages MUST match `type(scope): subject` (feat/fix/docs/ci/chore/test/refactor).
- NEVER run whole-repo `pytest` in a task (db migration tests alone take ~20 min). Run targeted paths only, e.g. `uv run pytest apps/worker/tests -q`.
- New Python test files MUST either have a repo-unique basename or live in a directory containing `__init__.py` — whole-repo collection breaks on duplicate bare basenames (until Task 9 lands importlib mode).
- Python: `uv run <cmd>` (pytest, mypy, ruff). Web: `pnpm --filter @forge/web <script>` (lint, typecheck, test, build).
- Follow existing layout patterns exactly: API routers in `apps/api/forge_api/routers/`, pydantic schemas in `apps/api/forge_api/schemas/`, services in `apps/api/forge_api/services/`, worker tasks in `apps/worker/forge_worker/tasks/`, web components in `apps/web/src/components/<area>/` with colocated `.test.tsx`.
- Do not touch `security/waivers.yaml`, `.semgrep/forge.yml`, or anything under `docs/security/evidence/`.
- Honesty rule (house style): never fake a capability. If something can't work yet, the UI/docs must say so plainly (see existing `dev-banner.tsx` pattern).
- Out of scope (do NOT attempt): creds-gated live gates/runbooks, pentest/soak human gates, learned-reranker eval numbers, `infra/` apply, Self-Eval Phase B, object-storage (MinIO) client subsystem, frontend workspace routing (F02), coverage-floor expansion beyond current 2 modules, multi-process SSO replay stores, SSO deprovision run-cancel.

## Model assignment

| Task | Title | Model |
|---|---|---|
| 1 | BYOK env truth + scripted-fallback warning | opus |
| 2 | Doc-drift sweep (secret var, shipped features, package maps) | sonnet |
| 3 | CHANGELOG + RELEASE_READINESS regeneration | sonnet |
| 4 | Trust-layer + realtime + orchestration docs | opus |
| 5 | Nginx reverse-proxy config + doc | sonnet |
| 6 | FORGE_SPEC realignment | sonnet |
| 7 | Web CI lanes (vitest + tsc) | opus |
| 8 | mypy full package coverage | opus |
| 9 | pytest basename-collision guard + importlib | opus |
| 10 | CI/config hygiene (ui-kit excludes, board_v1, release draft input) | sonnet |
| 11 | Schema-drift gate | opus |
| 12 | Slack `/forge status` wiring | opus |
| 13 | Legacy in-memory approval router retirement | opus |
| 14 | Deployment worker task registration | opus |
| 15 | SAML SLO honest UI | sonnet |
| 16 | Spec review reject/request-changes persistence | fable |
| 17 | Supervised multi-agent dispatch | fable |
| 18 | PM sync board-write path | fable |
| 19 | Attested Changesets REST + web UI | fable |
| 20 | Red-Team Gate trigger endpoint + V1 worker path | fable |
| 21 | Self-Eval Gate web UI | fable |

---

## Phase 1 — Truth in docs & config

### Task 1: BYOK env truth + scripted-fallback warning

**Files:**
- Modify: `.env.example` (lines ~88-105 model/rerank/embedding block)
- Modify: `docs/integrations/byok-and-boards.md:46,51`
- Modify: `docs/getting-started.md`, `docs/architecture.md` (every `MODEL_PROVIDER`/`MODEL_PROVIDER_KEY` mention)
- Modify: `apps/worker/forge_worker/agent_runner.py:118-130` (`_resolve_model_client`)
- Test: `apps/worker/tests/` (existing test file for agent_runner; add warning assertions there)

**Interfaces:**
- Consumes: `forge_agent.providers.config.ModelClientConfig.from_env()` — returns `None` when `FORGE_MODEL_PROVIDER` unset (`packages/agent-runtime/forge_agent/providers/config.py:65-79`).
- Produces: no API change; a `logging` warning emitted exactly once per `_resolve_model_client` fallback.

**Background (verified):** `.env.example:90-91` ships `MODEL_PROVIDER=anthropic` / `MODEL_PROVIDER_KEY=` but NOTHING reads those names. Real knobs: `FORGE_MODEL_PROVIDER` (`anthropic|openai`), `FORGE_MODEL_NAME`, key resolution order `FORGE_MODEL_API_KEY` → `ANTHROPIC_API_KEY`/`OPENAI_API_KEY`. When unset the worker silently uses the offline `ScriptedModelClient`.

- [ ] **Step 1:** Read `packages/agent-runtime/forge_agent/providers/config.py` fully; list every env var it reads (also `EMBEDDING_MODEL`, `EMBEDDING_BASE_URL`, `EMBEDDING_DIM` readers in `packages/knowledge-core`, and `FORGE_RERANK_*` readers in `packages/knowledge-core/forge_knowledge/reranker.py`).
- [ ] **Step 2:** Rewrite the `.env.example` model block: replace `MODEL_PROVIDER`/`MODEL_PROVIDER_KEY` with commented, documented `FORGE_MODEL_PROVIDER=anthropic`, `FORGE_MODEL_NAME=`, `FORGE_MODEL_API_KEY=` (+ note about `ANTHROPIC_API_KEY`/`OPENAI_API_KEY` fallback). Delete dead knobs `EMBEDDING_PROVIDER`, `RERANKER_URL`, `RERANKER_MODEL`. KEEP `FORGE_RERANK_MODEL` — it is live via pydantic `env_prefix="FORGE_"` → `Settings.rerank_model` (settings.py:298, reranker.py:518). Add missing real knobs `EMBEDDING_BASE_URL`, `EMBEDDING_DIM`. Keep `MINIO_ROOT_USER`/`MINIO_ROOT_PASSWORD` ONLY if `grep MINIO deploy/docker-compose*.yml` shows compose consumes them (it does for the minio service); delete `MINIO_ENDPOINT`, `MINIO_BUCKET`, `FORGE_POSTMORTEMS_BUCKET` if nothing reads them, with a one-line comment pointing to the parked object-storage scope.
- [ ] **Step 3:** Fix the same var names in `docs/getting-started.md`, `docs/architecture.md`, and `docs/integrations/byok-and-boards.md` (drop `EMBEDDING_PROVIDER` rows; the real rerank knobs are `FORGE_RERANK_{ENABLED,PROVIDER,MODEL,BASE_URL,TIMEOUT_MS,ALLOW_INSECURE_URL}` — `FORGE_RERANK_MODEL` is live via the pydantic `FORGE_` prefix).
- [ ] **Step 4:** Write failing test: when `FORGE_MODEL_PROVIDER` is unset, `_resolve_model_client` logs a WARNING containing `ScriptedModelClient` and `FORGE_MODEL_PROVIDER` (use `caplog`). Run it; expect FAIL (no warning today).
- [ ] **Step 5:** Add the warning in `_resolve_model_client` fallback branch. Run the worker test file; expect PASS.
- [ ] **Step 6:** Verify no stale references remain: `grep -rn "MODEL_PROVIDER_KEY\|EMBEDDING_PROVIDER=\|RERANKER_URL\|RERANKER_MODEL" --include="*.md" docs README.md .env.example | grep -v FORGE_RERANK` → 0 hits (except historical CHANGELOG).
- [ ] **Step 7:** Commit: `fix(config): document real FORGE_MODEL_* env vars and warn on scripted-client fallback`

### Task 2: Doc-drift sweep

**Files:**
- Modify: `docs/self-hosting/quickstart.md:28` (`SECRET_KEY` → `FORGE_SECRET_KEY`)
- Modify: `README.md:39-44,57-58,69,74,130-149`
- Modify: `docs/concepts.md:134-145` (+ new OIDC bullet, orchestration mention)
- Modify: `docs/architecture.md:53-73` (package map)
- Modify: `docs/self-hosting/docker-compose.md:18-41`
- Modify: `apps/web/src/lib/api/client.ts:1-8` (stale "Phase-0 stub" docstring)

**Interfaces:** none (docs only).

Fixes, each with verified reality:
1. quickstart: set `FORGE_SECRET_KEY` (deprecated alias `SECRET_KEY` warned at `apps/api/forge_api/cli_secrets.py:41`).
2. README:69 + concepts:143 — marketplace in-app publish SHIPPED (`apps/api/forge_api/routers/marketplace.py:293`, `apps/web/src/components/marketplace/publish-dialog.tsx`). Remove "offline author CLI only / UI in progress".
3. README:74 + concepts:145 — leaderboard UI SHIPPED (`apps/web/src/app/(board)/leaderboard/page.tsx`).
4. README:44,71 — OIDC SHIPPED (`apps/api/forge_api/routers/oidc.py`); add OIDC to concepts SSO section (concepts currently SAML/SCIM only).
5. README:57 — default sandbox is `worktree` (`.env.example` `FORGE_SANDBOX_KIND=worktree`), container optional.
6. README:39-42 — replace the named 15-screen list with the actual `(board)` routes: approvals, audit, board, deployments, depth, incidents, leaderboard, marketplace, observability, runs, settings, specs, sprints, walkthrough, workflow.
7. README repo layout + architecture package map — add `packages/orchestration-policy/` (Adaptive Orchestration, `forge_orchestration_policy`); both maps currently list 19 of 20.
8. docker-compose.md — add `docker-proxy` (`deploy/docker-compose.yml:472`) and `sandbox-proxy` (`:523`) to the service table; list all 7 networks (edge, backend, data, mcp, sandbox_ctl, sandbox_egress, observability), noting profile-gated ones.
9. client.ts header comment — routes are implemented; describe the 501 handler as a residual safety net.

- [ ] **Step 1:** Apply all nine edits (evidence lines above; re-verify each against current code before editing).
- [ ] **Step 2:** `grep -n "UI in progress\|still landing\|offline author CLI" README.md docs/concepts.md` → 0 hits.
- [ ] **Step 3:** `pnpm --filter @forge/web lint` (client.ts comment change) → clean.
- [ ] **Step 4:** Commit: `docs: fix stale feature status, secret var name, package maps, compose tables`

### Task 3: CHANGELOG + RELEASE_READINESS regeneration

**Files:**
- Modify: `CHANGELOG.md`, `RELEASE_READINESS.md`, `Makefile` (release-readiness target)

**Interfaces:** none.

- [ ] **Step 1:** `uv run cz changelog --dry-run` (per CHANGELOG.md:6-8 header). If output includes the trust-layer PRs (#61-#66, #57, #36), regenerate the file with `uv run cz changelog`; otherwise append an Unreleased section manually covering: Attested Changesets (#61), Time-Travel Runs (#62), Red-Team Gate (#63), Self-Eval Gate (#64-#66), OIDC SSO + marketplace publish + leaderboard (#57), realtime co-editing (#36). Keep existing entries.
- [ ] **Step 2:** Verify: `grep -in "attested\|time-travel\|red-team\|self-eval\|oidc" CHANGELOG.md` → all present.
- [ ] **Step 3:** Makefile `release-readiness` target currently forces `--bar production` while the committed artifact and `docs/README.md:51` describe the beta bar. Parameterize: `BAR ?= beta` and use `--bar $(BAR)`.
- [ ] **Step 4:** Regenerate: `uv run forge-release-readiness --bar beta > RELEASE_READINESS.md` (or the Make target's exact invocation — read `packages/evaluation/forge_eval/release/readiness.py` first for the real CLI shape; it writes the markdown itself). Expect: updated timestamp/commit, no-creds gates still SKIPPED_NO_CREDS. (G-ATTEST is `bar: production` in release/gates.yaml and correctly does NOT appear in a beta render — confirmed during execution.) This may take minutes; that's fine. Never let it run whole-repo pytest — if the render shells out to long gates, use its evidence/skip mode (read the module to confirm flags).
- [ ] **Step 5:** Sanity: `head -40 RELEASE_READINESS.md` shows HEAD commit hash and today's date.
- [ ] **Step 6:** Commit: `docs(release): regenerate changelog and readiness snapshot at current HEAD`

### Task 4: Trust-layer + realtime + orchestration docs

**Files:**
- Create: `docs/trust-layer.md`
- Modify: `docs/concepts.md` (new "Trust layer" section + realtime co-editing subsection + Adaptive Orchestration para)
- Modify: `docs/architecture.md` (cross-link trust-layer components)
- Modify: `docs/README.md` (index entry)

**Interfaces:** none (docs); MUST be written from code, not imagination.

- [ ] **Step 1:** Read, then document, each feature (one section each in `docs/trust-layer.md`, ~1 page per feature: what it is, data model, API/CLI surface, current limitations):
  - Attested Changesets: `packages/contracts/forge_contracts/attestation.py`, `packages/observability/forge_obs/attest/signing.py`, `apps/api/forge_api/services/attestation_service.py`, CLI `forge-verify` (`apps/api/forge_api/cli_verify.py`), migration `0036`. Limitation: minted as approval side-effect; REST/UI arrive in Task 19.
  - Time-Travel Runs: `apps/api/forge_api/routers/agent.py:259` (replay), `:316` (fork), `apps/worker/forge_worker/agent_runner.py` recording, `forge-replay` CLI, migration `0037`, web `run-trace/time-travel-replay.tsx`.
  - Red-Team Gate: `packages/workflow-engine/forge_workflow/temporal/workflows.py:141`, `packages/multi-agent-coordinator/forge_coordinator/red_team.py`, `GET /workflow/runs/{id}/red-team` (`routers/workflow.py:227`), migration `0038`. Limitation (state plainly): defaults to a recorded "parked-pass" verdict when no adversary model/sandbox is wired (`temporal/activities.py:128-134`); Temporal path only until Task 20.
  - Self-Eval Gate: `apps/api/forge_api/services/self_eval_gate.py`, `self_eval_service.py`, `routers/ao_settings.py`, worker `tasks/self_eval_{mint,run}.py`, migrations `0039`/`0040`. Limitation: Phase A; API-layer gate no-ops without a baseline scorecard.
- [ ] **Step 2:** Add concepts.md subsections: trust layer summary (link to docs/trust-layer.md), realtime co-editing (yjs/y-websocket, `apps/api/forge_api/routers/realtime.py`), Adaptive Orchestration (`packages/orchestration-policy/README.md` is the source).
- [ ] **Step 3:** Link check: every relative link added resolves (`ls` each target).
- [ ] **Step 4:** Commit: `docs: document trust layer, realtime co-editing, and adaptive orchestration`

### Task 5: Nginx reverse-proxy config + doc

**Files:**
- Create: `deploy/nginx/forge.conf`
- Create: `docs/self-hosting/reverse-proxy.md`
- Modify: `docs/self-hosting/quickstart.md` (one line linking the new doc)

**Interfaces:** mirrors `deploy/caddy/Caddyfile` routing exactly (same upstreams/ports/paths, incl. websocket upgrade for realtime + MCP SSE if present).

- [ ] **Step 1:** Read `deploy/caddy/Caddyfile` and `deploy/docker-compose.yml` service ports. Write `forge.conf` as a full `server` block equivalent: TLS notes as comments, `proxy_pass` per route, `proxy_set_header Upgrade/Connection` for websocket paths, sane timeouts matching Caddy's.
- [ ] **Step 2:** Validate syntax: `docker run --rm -v "$PWD/deploy/nginx/forge.conf:/etc/nginx/conf.d/forge.conf:ro" nginx:alpine nginx -t` (if the sandbox lacks docker, state so in the report and have the reviewer run it; do not claim validated).
- [ ] **Step 3:** Write `reverse-proxy.md`: Caddy (default, auto-TLS) vs nginx (this file), header/websocket requirements, health-check paths.
- [ ] **Step 4:** Commit: `feat(deploy): add nginx reverse-proxy config and reverse-proxy doc`

### Task 6: FORGE_SPEC realignment

**Files:**
- Modify: `docs/FORGE_SPEC.md` (structure list ~L190/L211, SDD CLI section L279-285, launch-docs list L889, roadmap L978-1022)

- [ ] **Step 1:** Structure list: remove `packages/ui-kit/` (UI lives in `apps/web`), keep `deploy/nginx/forge.conf` (exists after Task 5), add the 9 unlisted real packages (approval-sdk, auth-sdk, authz-sdk, contracts, db, deploy-core, marketplace-sdk, observability, orchestration-policy).
- [ ] **Step 2:** SDD CLI section: state the shipped reality — lifecycle is API/web-first (`routers/spec.py`), console scripts shipped are `forge-verify`/`forge-replay`; `forge bench|marketplace` remain parked stubs. Move the full `forge` CLI to the roadmap section explicitly.
- [ ] **Step 3:** Roadmap: add a shipped "Trust layer" entry (4 features, PR refs) and mark early-shipped Phase-3 items (OIDC/SAML/SCIM, marketplace, leaderboard, Firecracker) as done.
- [ ] **Step 4:** Commit: `docs(spec): realign FORGE_SPEC with shipped reality`

---

## Phase 2 — CI gates what exists

### Task 7: Web CI lanes

**Files:**
- Modify: `.github/workflows/ci.yml` (web job, ~lines 243-278)

- [ ] **Step 1:** Run locally first: `pnpm --filter @forge/web typecheck` and `pnpm --filter @forge/web test`. If either fails, fix trivial breakage (type errors, snapshot drift) as part of this task; if failures are deep/product bugs, report them and gate only what passes (do not disable tests to go green).
- [ ] **Step 2:** Add two steps to the `web` job after lint: `pnpm --filter @forge/web typecheck` and `pnpm --filter @forge/web test` (before build). No `continue-on-error`.
- [ ] **Step 3:** `actionlint` if available, else YAML sanity via `python -c "import yaml,sys; yaml.safe_load(open('.github/workflows/ci.yml'))"`.
- [ ] **Step 4:** Commit: `ci(web): run vitest suite and tsc typecheck on every PR`

### Task 8: mypy full package coverage

**Files:**
- Modify: `Makefile:13-17` (`MYPY_PACKAGES`) — add `forge_deploy`, `forge_auth`, `forge_marketplace`, `forge_obs`
- Modify: whatever those packages need to type-check clean

- [ ] **Step 1:** Add the 4 modules to `MYPY_PACKAGES`. Run `make typecheck`.
- [ ] **Step 2:** Fix every surfaced error properly (annotations, narrowing) — no blanket `# type: ignore`, no ignore_errors config. If a package has >30 errors, report the count before proceeding.
- [ ] **Step 3:** `make typecheck` → exit 0. Run the 4 packages' test dirs targeted (`uv run pytest packages/deploy-core/tests packages/auth-sdk/tests packages/marketplace-sdk/tests packages/observability/tests -q`) → green.
- [ ] **Step 4:** Commit: `ci(types): bring deploy-core, auth-sdk, marketplace-sdk, observability under mypy`

### Task 9: pytest basename-collision guard + importlib

**Files:**
- Modify: `pyproject.toml:90-94` (pytest addopts)
- Modify: `tests/test_test_infra.py` (add guard test — file already exists, correct home)

- [ ] **Step 1:** Write the guard test in `tests/test_test_infra.py`: walk the repo for `test_*.py` (exclude node_modules/.git/__pycache__), group by basename; for any duplicated basename assert every copy resolves to a unique dotted module (i.e. sits under a chain of `__init__.py` dirs). Fail message must name the colliding paths and cite this plan.
- [ ] **Step 2:** Run it: `uv run pytest tests/test_test_infra.py -q` → PASS (current 7 collision sets are all disambiguated today; if not, fix by adding `__init__.py`).
- [ ] **Step 3:** Add `--import-mode=importlib` to addopts. Verify collection of the WHOLE repo (collection only — fast, and it's where exit-2 lives): `uv run pytest --collect-only -q > /dev/null; echo $?` → 0. Then run two targeted suites that use fixtures/conftest heavily (`apps/api/tests/pm_api`, `packages/workflow-engine/tests/editor`) → green.
- [ ] **Step 4:** If importlib breaks collection or those suites, revert the addopts change (keep the guard test) and record why in the commit body.
- [ ] **Step 5:** Commit: `test(infra): guard against test-basename collisions; adopt importlib import mode`

### Task 10: CI/config hygiene

**Files:**
- Modify: `ruff.toml:10`, `mypy.ini:15`, `pyproject.toml:63` (drop nonexistent `packages/ui-kit` excludes)
- Delete: `apps/api/forge_api/routers/board_v1/` (contains only `__pycache__`; `git rm -r` won't see it — remove from disk, confirm untracked)
- Modify: `.github/workflows/release.yml` (wire dead `draft` input: `release.yml:22-25` vs hardcoded `--draft` at `:200`)
- Modify: `.github/workflows/security.yml` (top-of-file comment noting intentional overlap with ci.yml security job)

- [ ] **Step 1:** Apply the four edits. For release.yml: `--draft` becomes conditional on `${{ inputs.draft }}` with default true for tag pushes.
- [ ] **Step 2:** `uv run ruff check . -q` and `make typecheck` still clean; YAML sanity-parse both workflows.
- [ ] **Step 3:** Commit: `chore(ci): remove dead ui-kit excludes, orphaned board_v1 dir, wire release draft input`

### Task 11: Schema-drift gate

**Files:**
- Create: `packages/db/tests/test_schema_drift.py` (basename unique — verified no existing file by this name)

**Interfaces:**
- Consumes: `packages/db/alembic.ini`, `forge_db` metadata (find the canonical `Base.metadata` export in `packages/db/forge_db/models/__init__.py`), existing PG fixture pattern from `packages/db/tests/test_migration.py` (`@pytest.mark.postgres`, `FORGE_TEST_DATABASE_URL`).

- [ ] **Step 1:** Write failing-by-construction check: upgrade a fresh PG database to `head`, then run `alembic.autogenerate.compare_metadata(migration_context, Base.metadata)` and assert the diff list is empty. Copy the engine/env setup verbatim from `test_migration.py`'s postgres tests.
- [ ] **Step 2:** `uv run pytest packages/db/tests/test_schema_drift.py -q` with a local PG if available (`docker compose` pg service or skip-if-no `FORGE_TEST_DATABASE_URL` exactly like test_migration.py does). If it FAILS with real drift, list the diffs in the report — do NOT auto-generate a migration without review.
- [ ] **Step 3:** Commit: `test(db): gate model/migration schema drift via autogenerate compare`

---

## Phase 3 — Silent-failure wiring

### Task 12: Slack `/forge status` wiring

**Files:**
- Modify: `apps/api/forge_api/routers/integration.py:496` (stub) and its service layer
- Test: extend the existing integration router test file (same dir/tests package as current slash-command tests)

**Interfaces:**
- Consumes: agent-run lookup — find the existing service the `runs` UI uses (grep `routers/agent.py` for the run-detail dependency) and reuse it; do not open a new DB session pattern.
- Produces: slash response text `run <id>: <status> (<phase/step>)` or a not-found message.

- [ ] **Step 1:** Failing test: posting the slash payload `/forge status <known-run-id>` returns 200 with the run's real status string; unknown id returns the not-found copy. Seed a run via the same fixture the runs API tests use.
- [ ] **Step 2:** Implement lookup + response; keep Slack signature verification untouched.
- [ ] **Step 3:** Targeted tests green; commit: `feat(slack): wire /forge status to live run lookup`

### Task 13: Legacy in-memory approval router retirement

**Files:**
- Modify: `apps/api/forge_api/routers/__init__.py` or wherever `FEATURE_ROUTERS` lists `approval` (grep `FEATURE_ROUTERS`)
- Delete: `apps/api/forge_api/routers/approval.py` + its dedicated tests
- Check: `grep -rn "\"/approval/\|'/approval/" apps/web/src apps/api` for consumers first

- [ ] **Step 1:** Prove no consumer: grep web + api + tests for `/approval/` (singular, not `/approvals/`). If a consumer exists, STOP and report instead of deleting.
- [ ] **Step 2:** Remove router from registry, delete module + its tests, run `uv run pytest apps/api/tests -q -k "approval"` → remaining (DB-backed) approval tests green.
- [ ] **Step 3:** Commit: `refactor(api): retire legacy in-memory /approval router in favor of DB-backed /approvals`

### Task 14: Deployment worker task registration

**Files:**
- Modify: `apps/worker/forge_worker/celery_app.py:29-48` (add `forge_worker.tasks.deployments` to include)
- Modify: `apps/api/forge_api/services/deployment_service.py` ONLY if enqueueing is added (see Step 2 decision rule)
- Test: `apps/worker/tests/` (extend existing deployments task tests if present; else add to an existing worker test module)

- [ ] **Step 1:** Register the module in celery include. Failing test: `celery_app.tasks` contains `forge.deployments.advance` and `forge.deployments.request`.
- [ ] **Step 2:** Decision rule (record which branch in the commit body): if `forge.deployments.request` duplicates what `deployment_service` already does synchronously with no retry/backoff benefit surfaced anywhere (no beat schedule, no caller), keep the sync path as-is and just register + test the tasks execute `DeploymentOrchestrator.advance()` correctly. Do NOT rip out the sync path.
- [ ] **Step 3:** Targeted worker tests green; commit: `fix(worker): register deployment tasks with celery`

### Task 15: SAML SLO honest UI

**Files:**
- Modify: `apps/web/src/components/sso/sso-settings-view.tsx:590` (SLO URL field)
- Test: `apps/web/src/components/sso/sso-settings-view.test.tsx`

- [ ] **Step 1:** Failing test: SLO field renders disabled with helper text stating Single Logout is not yet supported (server returns 501) — align copy with the honest-status house style; the existing test at `:359` asserts no "coming soon" copy, so use plain "not yet supported" phrasing consistent with that assertion.
- [ ] **Step 2:** Implement; `pnpm --filter @forge/web test -- sso-settings-view` → green.
- [ ] **Step 3:** Commit: `fix(web): mark SAML single logout as not yet supported in SSO settings`

### Task 16: Spec review reject/request-changes persistence — model: fable

**Files:**
- Modify: `packages/spec-engine/forge_spec/` (engine: add `reject_spec` / `request_changes` transitions; find `approve_spec` and mirror its shape; extend the status enum — it is file-based, no DB migration)
- Modify: `apps/api/forge_api/routers/spec.py` (two new POST endpoints beside the approve endpoint at `:339`)
- Modify: `apps/api/forge_api/schemas/` (request body: `{note: str}`)
- Modify: `apps/web/src/components/spec-studio/read-mode.tsx:28-34,256` (replace local-only recording with real API calls; keep optimistic UI)
- Tests: spec-engine tests (existing files), `apps/api/tests` spec router tests, `read-mode.test.tsx`

**Interfaces:**
- Consumes: `FileSpecEngine.approve_spec` signature (read it; mirror exactly for the two new transitions).
- Produces: `POST /spec/{id}/reject` and `POST /spec/{id}/request-changes`, both accepting `{note}`, returning the updated spec payload the approve endpoint returns; new statuses must round-trip through `load_manifest`.

- [ ] **Step 1 (TDD, engine):** failing tests for both transitions incl. illegal-state transitions (e.g. rejecting an already-approved spec → domain error mirroring approve's guards). Implement. Targeted spec-engine tests green.
- [ ] **Step 2 (TDD, API):** failing router tests (status codes, body echo, 409 on illegal transition — mirror approve endpoint's error mapping). Implement. Green.
- [ ] **Step 3 (TDD, web):** failing read-mode tests: clicking Reject calls the endpoint and renders returned status; server error surfaces (no silent local state). Implement. Green.
- [ ] **Step 4:** Commit: `feat(spec): persist reject and request-changes review decisions end-to-end`

### Task 17: Supervised multi-agent dispatch — model: fable

**Files:**
- Modify: `apps/worker/forge_worker/agent_runner.py:292-312` (the `forge.agent.run` task — branch on the run's `execution_mode`)
- Consumes: `packages/multi-agent-coordinator/forge_coordinator/` (supervisor entrypoint — read `forge_coordinator/__init__.py` exports and its own tests to find the canonical run API), `ExecutionMode.SUPERVISED_MULTI_AGENT` enum, migration `0009_f27_multi_agent.py` tables (persistence via `forge_coordinator.persistence`)
- Test: `apps/worker/tests/` (new cases in the existing agent_runner test module; scripted model clients — the coordinator's own tests show how it runs offline)

**Interfaces:**
- Produces: when `execution_mode == supervised_multi_agent`, `forge.agent.run` drives the coordinator supervisor instead of a single `AgentRunner`; run status/artifacts land in the same tables the single path writes (same observable contract for the API/UI).

- [ ] **Step 1:** Read the coordinator package tests end-to-end to extract the intended integration surface (this package was built with tests but never wired — its tests ARE the spec).
- [ ] **Step 2 (TDD):** failing worker test: enqueue `forge.agent.run` for a run whose spec sets supervised mode with a scripted client → coordinator persistence rows exist and run completes; single-agent path untouched (regression test asserting default mode still builds one `AgentRunner`).
- [ ] **Step 3:** Implement the branch + adapter glue (model client construction reuses `_resolve_model_client`). Keep the diff inside `agent_runner.py` + a small `apps/worker/forge_worker/multi_agent.py` adapter if glue exceeds ~60 lines.
- [ ] **Step 4:** Targeted tests green (`apps/worker/tests` + `packages/multi-agent-coordinator/tests`). Commit: `feat(worker): dispatch supervised multi-agent runs through forge_coordinator`

### Task 18: PM sync board-write path — model: fable

**Files:**
- Create: `apps/worker/forge_worker/tasks/pm_sync.py` (task `forge.pm.process_webhook`)
- Modify: `apps/worker/forge_worker/celery_app.py` include list
- Modify: `apps/api/forge_api/services/pm_service.py:322-372` (enqueue after persist, replacing the parked NOTE at `:371`)
- Test: `apps/worker/tests/` (new module in an `__init__.py`-marked dir or unique basename `test_pm_sync_task.py`)

**Interfaces:**
- Consumes: persisted webhook row (id) from `pm_service`; provider adapters `packages/integration-sdk/forge_integrations/pm/registry.py` (9 providers, each with issue fetch + mapping — read the adapter conformance tests `packages/integration-sdk/tests/test_conformance.py` + `tests/pm/` for the fetch/map API); board write surface in `packages/board-core` (find the `sync_in` / upsert entrypoint the parked docstring names — `grep -rn "sync_in" packages/board-core apps`).
- Produces: webhook → task enqueue → adapter re-fetch → board item upsert, idempotent on redelivery (dedup key already persisted by pm_service).

- [ ] **Step 0 (gate):** Verify the substrate exists NOW: locate a concrete board upsert path in `packages/board-core` usable from the worker (the parked note predates board-core maturity). If genuinely absent, STOP after Step 0, implement the honest fallback instead — webhook response/UI surface a `sync: parked` status (`pm-integrations-view.tsx` health panel) — and report the blocker precisely.
- [ ] **Step 1 (TDD):** failing worker test with a fake adapter registered in the registry: task fetches the issue, upserts a board item, second delivery of same event is a no-op.
- [ ] **Step 2:** Implement task + enqueue in `pm_service` (post-persist, post-audit; keep 202 semantics). Signature verification untouched.
- [ ] **Step 3:** Targeted tests: new module + `apps/api/tests/pm_api` + `packages/integration-sdk/tests/pm` → green.
- [ ] **Step 4:** Update the parked docstrings (`pm.py:7-10`, `pm_service.py:9-14`) to reflect what now works vs. what remains parked (OAuth exchange, backfill, conflict resolve — leave parked unless trivially unlocked by the same substrate; do not scope-creep).
- [ ] **Step 5:** Commit: `feat(pm): wire webhook intake to board-write sync task`

---

## Phase 4 — Trust-layer completion

### Task 19: Attested Changesets REST + web UI — model: fable

**Files:**
- Create: `apps/api/forge_api/routers/attestations.py` (`GET /attestations`, `GET /attestations/{id}`, `GET /approvals/{id}/attestation`)
- Create: `apps/api/forge_api/schemas/attestation.py`
- Modify: router registry (same list Task 13 touches — coordinate: land after Task 13)
- Create: `apps/web/src/components/attestations/attestation-panel.tsx` (+ `.test.tsx`) rendered inside the approvals review surface (`apps/web/src/components/approvals/review-panel.tsx` — same slot pattern as `red-team-badge.tsx`)
- Tests: `apps/api/tests/attest/` (dir already exists with `__init__.py`)

**Interfaces:**
- Consumes: `attestation_service` read paths + `forge_db/models/attestation.py`; verification via the same seam `forge-verify` CLI uses (`cli_verify.py` — reuse its service call, not its parked stub printout).
- Produces: JSON: `{id, changeset_hash, signer, created_at, verified: bool, provenance: {...}}`; UI badge/panel: signed/verified/missing states, no fake states.

- [ ] **Step 1 (TDD, API):** failing router tests: list/detail/by-approval incl. 404, using fixtures from `apps/api/tests/attest/` (existing service tests show how records are minted). Implement. Green.
- [ ] **Step 2 (TDD, web):** failing panel tests: renders verified/unverified/absent from API payload; wire into review-panel next to red-team badge. Implement. `pnpm --filter @forge/web test -- attestation` green.
- [ ] **Step 3:** Append an API section to `docs/trust-layer.md` (Task 4 created it) and remove its "REST/UI pending" limitation line.
- [ ] **Step 4:** Commit: `feat(attest): expose attested changesets over REST with approvals UI panel`

### Task 20: Red-Team Gate trigger + V1 worker path — model: fable

**Files:**
- Modify: `apps/api/forge_api/routers/workflow.py` (add `POST /workflow/runs/{id}/red-team` beside the GET at `:227`)
- Modify: the V1 (Celery/FSM) approval-gate path in `apps/worker/` — locate where the V1 workflow FSM evaluates gates before the human approval step (grep `apps/worker` + `packages/workflow-engine` for the non-temporal gate evaluation; mirror `temporal/workflows.py:141`)
- Consumes: `packages/db/forge_db/redteam/repository.py`, `forge_coordinator/red_team.py`, verdict schema `apps/api/forge_api/schemas/red_team.py`
- Tests: worker V1-path test module + `apps/api/tests` workflow router tests

**Interfaces:**
- Produces: POST returns 202 + verdict record id; V1 path records a verdict (real adversary when a model is configured, explicit `parked` verdict otherwise — identical contract to the Temporal activity) before the approval gate resolves.

- [ ] **Step 1:** Map the V1 gate evaluation point; write it down in the task report before coding.
- [ ] **Step 2 (TDD, worker):** failing test: V1 run reaching approval gate persists a red-team verdict row (parked kind when no adversary configured — same shape as `temporal/activities.py:128-134`). Implement by extracting the Temporal activity's verdict-building into a shared helper both paths call (DRY — helper lives in `forge_workflow`, not copy-paste).
- [ ] **Step 3 (TDD, API):** failing test: POST triggers evaluation for a run, GET then returns the verdict; 409 when run not in a gateable state. Implement. Green.
- [ ] **Step 4:** Update `docs/trust-layer.md` Red-Team limitations (Temporal-only line goes away; parked-pass default stays documented).
- [ ] **Step 5:** Commit: `feat(red-team): trigger endpoint and V1 worker gate parity`

### Task 21: Self-Eval Gate web UI — model: fable

**Files:**
- Create: `apps/web/src/components/self-eval/self-eval-panel.tsx` (+ `.test.tsx`), surfaced in settings (same page family as `ao_settings` backend — find where AO settings render: `grep -rn "ao" apps/web/src/app/(board)/settings`)
- Consumes: existing endpoints from `routers/ao_settings.py` (suite scoping, baseline, run trigger `POST /ao/self-eval/runs`, gate verdicts) — read the router to enumerate the exact contract first; do NOT invent endpoints.

**Interfaces:**
- Produces: panel showing configured suite, current baseline (score + minted at), last run result, gate status for pending config changes; a "run self-eval" action calling the existing run endpoint; Phase-A limitation copy (API-layer gate requires a baseline) stated inline.

- [ ] **Step 1:** Enumerate the real API contract from `routers/ao_settings.py` + `schemas/` and paste it into the task report.
- [ ] **Step 2 (TDD):** failing tests: renders baseline/last-run from mocked API; run button posts and renders accepted state; no-baseline state shows the honest Phase-A copy. Implement. `pnpm --filter @forge/web test -- self-eval` green; `pnpm --filter @forge/web typecheck` clean.
- [ ] **Step 3:** Update `docs/trust-layer.md` Self-Eval section (UI now exists; Phase B still out of scope).
- [ ] **Step 4:** Commit: `feat(self-eval): settings panel for suite, baseline, runs, and gate status`

---

## Execution notes

- Phase 1 tasks 1-6 are file-disjoint → may run as parallel subagents in isolated worktrees, merged serially by the orchestrator. Phases 2-4 run sequentially (shared files: ci.yml, celery_app.py, router registry).
- Task 19 depends on Task 13 (router registry churn) and Task 4 (docs file exists). Task 20/21 depend on Task 4 (docs file).
- Every task ends green on its targeted tests before commit; final gate before PR: `uv run ruff check . -q`, `make typecheck`, `uv run pytest --collect-only -q`, `pnpm --filter @forge/web lint && pnpm --filter @forge/web typecheck && pnpm --filter @forge/web test && pnpm --filter @forge/web build`.
