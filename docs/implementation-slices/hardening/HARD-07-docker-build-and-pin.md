# HARD-07 — Container & Web Build + Image Digest Pinning

> Phase: hardening · Blocker(s): #3 (`docker compose build` + `next build` never run for real; images not pinned by `@sha256`) · Gates realized: **G-BUILD** (BETA) + **G-IMG-PINNED** (PRODUCTION) — these are the build/pin gates the production-hardening spec attributes to its build workstream (spec §HARD-08); this slice is that workstream, renumbered HARD-07 for the hardening slice set. · Status target: **"verified"** means, on a **networked CI runner** (this CANNOT run in the no-network sandbox), `docker compose -f deploy/docker-compose.yml build` builds all 4 first-party images, `pnpm -r build` (`next build`) produces a production web bundle, every *pulled* image in both compose files and every Dockerfile `FROM` is pinned by an immutable `@sha256` digest, `docker compose up -d` brings the stack to `healthy` and `/health` returns 200 on api + mcp-gateway, every built image has a generated SBOM, and runtime `docker inspect` confirms non-root + healthcheck + memory limit on each app container — all evidence captured. **No external credentials are required**; a build/registry network is. The offline half (digest-presence lint, `.dockerignore` presence, standalone-output config, manifest schema) runs in the hermetic suite and is part of the whole-suite green gate.

---

## 1. Intent — what & why

The overnight ALPHA proved the deploy substrate *parses* — `docker compose config` validates both compose files and `tests/test_deploy_infra.py` (19 passed) pins the structural guarantees (healthchecks, resource limits, named volumes, segmented networks, non-root `user:`, capped logs). But three things were never exercised, and they are exactly release blocker #3 (MORNING_REPORT §5(8), §6, §7.3):

1. **The 4 Dockerfiles were never built.** `deploy/docker/{api,worker,mcp-gateway,web}.Dockerfile` were authored against `uv sync --no-dev --no-editable` / `pnpm --filter ./apps/web build` but `docker compose build` could not run (no base-image network, `uv sync`/`pnpm` disallowed). They are *compile-on-paper* artifacts.
2. **`next build` was not re-run at report time.** A `.next/` tree exists on disk locally, but the production Next.js bundle is not verified by the pipeline as part of the build gate (the web CI job *does* run `pnpm -r build`, but the image build path that ships to operators — `deploy/docker/web.Dockerfile` — was never executed end-to-end, and `next.config.mjs` does not emit a `standalone` server, so the runtime image copies the whole workspace).
3. **Images float on mutable tags.** Every `image:` is a version tag, not an immutable digest: `pgvector/pgvector:pg16`, `redis:7.4-alpine`, `minio/minio:RELEASE.2024-10-13T13-34-11Z`, `caddy:2.8-alpine`, `willfarrell/autoheal:1.2.0`, and the Dockerfile bases `ghcr.io/astral-sh/uv:python3.13-bookworm-slim` + `node:22-bookworm-slim`. A floating tag can be repointed under us (supply-chain / reproducibility risk). The existing test `test_prod_images_are_pinned_not_latest` only forbids `:latest` — it accepts a floating tag. There is also **no root `.dockerignore`**, so the build context is the entire repo (`.venv/`, `node_modules/`, `.next/`, `*.pem`, `.env*`) — slow, and a secret-leak-into-context hazard.

**Why now.** Blocker #3 is the difference between "a compose file that lints" and "a stack a self-hoster can actually `docker compose up`." Production deployment best-practice in `docs/FORGE_SPEC.md` ("Production Docker Compose Requirements") calls for pinned, non-root, health-checked, resource-limited images; ALPHA satisfies that *structurally* but not *operationally*. This slice closes the loop: build for real, pin by digest for reproducibility/supply-chain integrity, smoke the running stack, and emit a per-image SBOM that feeds the HARD-09 security-evidence pack. It changes **no product surface** — it proves the deploy surface against reality and extends `deploy/`, `apps/web` build config, the `Makefile`, `.github/workflows/ci.yml`, and `tests/test_deploy_infra.py`.

## 2. User-facing / operator behavior

This is an operator/CI-facing slice; there is no end-user UI change. Observable behavior:

- **Operator — reproducible build & bring-up.** From the repo root on a host with Docker, the operator runs `make build-images` (→ `docker compose -f deploy/docker-compose.yml build`) and gets all 4 first-party images (`forge/api`, `forge/worker`, `forge/mcp-gateway`, `forge/web`) built from pinned, digest-locked base images. `docker compose -f deploy/docker-compose.yml up -d` then brings the stack up; within the configured `start_period` every container reports `healthy` (`docker compose ps` shows `(healthy)`), and `curl -fsS http://localhost:8000/health` / `…:8001/health` return `200 {"status":"ok"}`. The same images come up bit-for-bit on a re-pull because every dependency is digest-pinned.
- **Operator — supply-chain integrity.** Every pulled image carries `name:tag@sha256:<digest>`; if an upstream tag is repointed, the digest mismatch is caught at pull, not silently adopted. A committed `deploy/build-manifest.json` records the resolved digest + build date + SBOM path of each image so an operator can diff what they're running against what was released.
- **Operator — supply-chain transparency.** A per-image SBOM (CycloneDX JSON) under `deploy/sbom/<image>.cdx.json` lists every OS + language package, ready to feed a CVE scan (HARD-09) or an auditor.
- **Maintainer / CI.** A new `build` job in CI builds the 4 images on a networked runner, runs `next build`, runs `docker compose up -d` + a `/health` smoke, generates + uploads the SBOMs as artifacts, and fails the build if any pulled image or Dockerfile base is not digest-pinned. The hermetic `pytest` lane still asserts the digest/`.dockerignore`/standalone invariants offline so a regression is caught even without a Docker daemon.
- **Operator — slimmer web image.** With `output: "standalone"`, the web runtime image ships only the Next.js standalone server + static assets (not the whole pnpm workspace), so it starts faster and has a smaller attack surface and SBOM.

## 3. Vertical slice

### 3.1 Data model

**No database tables, columns, or Alembic migrations.** This slice is infra/build; it touches no `forge_db` schema and adds nothing to `forge_contracts`. The only persisted artifacts are **build-evidence files committed to the repo** (not DB rows):

- `deploy/build-manifest.json` — the digest/SBOM manifest (schema in §4). Generated by the pin step, committed, and asserted by an offline test.
- `deploy/sbom/<image>.cdx.json` — one CycloneDX SBOM per built image (generated in CI, uploaded as an artifact; a checked-in copy of the *release* SBOMs lives under `deploy/sbom/` for the evidence pack).

These files are evidence, not runtime state; nothing reads them at request time.

### 3.2 Backend (FastAPI)

**No route, schema, or service changes.** The slice *depends on* the already-present liveness endpoints and asserts them at runtime; it does not add or modify handlers:

- `apps/api/forge_api/routers/health.py` already serves `GET /health` and `GET /healthz` (`HealthResponse`, mounted at root via `HEALTH_ROUTER` in `apps/api/forge_api/main.py`). The compose `api` healthcheck (`curl -f http://localhost:8000/health`) and the smoke step both hit this.
- `apps/mcp-gateway/forge_mcp_gateway/app.py` already serves `GET /health`. The compose `mcp-gateway` healthcheck and smoke hit this.
- The `worker` image has no inbound port; its healthcheck stays `celery -A forge_worker inspect ping` (unchanged), and the smoke verifies the worker reaches `healthy` via `docker compose ps`.

The only backend-adjacent consideration is that the **api/mcp-gateway healthchecks invoke `curl`**, which must exist in the runtime image (the `ghcr.io/astral-sh/uv:python3.13-bookworm-slim` base ships `curl`; if a future base drops it, the healthcheck must switch to a `python -c "urllib.request..."` probe — noted as an acceptance check, see §6.9).

### 3.3 Worker / agent runtime

**No `forge_worker` / `forge_agent` code change.** The worker is built as one of the 4 images (`deploy/docker/worker.Dockerfile`) and its bring-up health (`celery … inspect ping`) is verified by the compose smoke. The only build-relevant requirement is that `uv sync --no-dev --no-editable` resolves the worker's deps from a **complete `uv.lock`** — which is why this slice is sequenced after HARD-14's re-lock (see §5): an incomplete lock would make the worker image build non-reproducible.

### 3.4 Frontend (Next.js)

Two minimal, build-only changes to `apps/web` (no component/UX change):

1. **Enable standalone output** in `apps/web/next.config.mjs`:
   ```js
   const nextConfig = {
     reactStrictMode: true,
     output: "standalone",           // emit .next/standalone self-contained server
     env: { NEXT_PUBLIC_API_URL: process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000" },
   };
   ```
   `next build` then produces `.next/standalone/` (a minimal `server.js` + traced `node_modules`) and `.next/static/`. This is the officially recommended container output and is required for a small runtime image.

2. **Rewrite `deploy/docker/web.Dockerfile` to a standalone multi-stage copy.** The build stage stays (`pnpm install --frozen-lockfile` + `pnpm --filter ./apps/web build`); the runtime stage copies only the standalone server + static assets and runs `node server.js` as the non-root `node` user (uid 1000), preserving the compose `user: "1000:1000"` contract. Example runtime stage:
   ```dockerfile
   FROM node:22-bookworm-slim@sha256:<digest> AS runtime
   ENV NODE_ENV=production PORT=3000 HOSTNAME=0.0.0.0
   WORKDIR /app
   COPY --from=build --chown=node:node /app/apps/web/.next/standalone ./
   COPY --from=build --chown=node:node /app/apps/web/.next/static ./apps/web/.next/static
   COPY --from=build --chown=node:node /app/apps/web/public ./apps/web/public
   USER node
   EXPOSE 3000
   CMD ["node", "apps/web/server.js"]
   ```
   The compose `web` healthcheck stays a TCP/HTTP liveness probe on `:3000` (`wget -qO- http://localhost:3000`); a `public/` dir is created if absent so the `COPY` does not fail.

No change to `apps/web/package.json` scripts, eslint config (held at 9 per HARD-14), or any React component. `pnpm-lock.yaml` must remain frozen-lockfile-clean (HARD-14).

### 3.5 Infra / deploy / CI

This is the bulk of the slice. **EXTEND the existing `deploy/` files — do not fork.**

**(a) Digest-pin every pulled image** in `deploy/docker-compose.yml`, `deploy/docker-compose.dev.yml`, and every Dockerfile `FROM`. Keep the human-readable tag for clarity and append the digest:

| File / service | Before | After (shape) |
|---|---|---|
| compose `db` | `pgvector/pgvector:pg16` | `pgvector/pgvector:pg16@sha256:<d>` |
| compose `redis` | `redis:7.4-alpine` | `redis:7.4-alpine@sha256:<d>` |
| compose `minio` | `minio/minio:RELEASE.2024-10-13T13-34-11Z` | `…@sha256:<d>` |
| compose `caddy` | `caddy:2.8-alpine` | `caddy:2.8-alpine@sha256:<d>` |
| compose `autoheal` | `willfarrell/autoheal:1.2.0` | `willfarrell/autoheal:1.2.0@sha256:<d>` |
| `api/worker/mcp-gateway.Dockerfile` `FROM` | `ghcr.io/astral-sh/uv:python3.13-bookworm-slim` | `…python3.13-bookworm-slim@sha256:<d>` |
| `web.Dockerfile` `FROM` (build+runtime) | `node:22-bookworm-slim` | `node:22-bookworm-slim@sha256:<d>` |

The 4 first-party services (`api`, `worker`, `mcp-gateway`, `web`) keep `build:` + a tagged `image: forge/<svc>:${FORGE_VERSION:-0.1.0}` — they are built locally, so they are not `@sha256`-pinned in the compose `image:` line; instead their **post-build digests are recorded in `deploy/build-manifest.json`**, and an optional `deploy/docker-compose.release.yml` override can pin them by `@sha256` for a registry-pull (no-build) deployment.

**(b) Add a root `.dockerignore`** so the build context excludes heavy/secret paths (the Dockerfiles `COPY` from the repo-root context `..`):
```
.git
.venv
**/__pycache__
**/.pytest_cache
**/.mypy_cache
**/.ruff_cache
node_modules
**/node_modules
apps/web/.next
deploy/sbom
.env
.env.*
!.env.example
!.env.production.example
*.pem
*.key
deploy/secrets
htmlcov
dist
build
```

**(c) Pin-resolution + manifest script** `deploy/scripts/pin-digests.sh` (shellcheck-clean): for each pulled image, `docker pull <tag>` then resolve `docker buildx imagetools inspect <tag> --format '{{json .Manifest.Digest}}'` (or `docker inspect --format '{{index .RepoDigests 0}}'`), rewrite the compose/Dockerfile lines in place, and emit `deploy/build-manifest.json`. Idempotent; re-runnable to roll digests forward deliberately.

**(d) SBOM script** `deploy/scripts/sbom.sh`: for each built image, run `syft <image> -o cyclonedx-json=deploy/sbom/<image>.cdx.json` (Anchore Syft; CycloneDX format). Records the SBOM path back into `build-manifest.json`.

**(e) Smoke script** `deploy/scripts/smoke.sh` (shellcheck-clean): `docker compose -f deploy/docker-compose.yml up -d`, wait for `db`/`redis` healthy, run `alembic upgrade head` (via the api image or `make migrate`), poll `docker compose ps --format json` until `api`, `mcp-gateway`, `web`, `worker` are `healthy` (bounded timeout), `curl -fsS localhost:8000/health` + `localhost:8001/health`, then `docker compose down -v`. Prints a PASS/FAIL summary and non-zero exits on any failure.

**(f) Makefile targets** (extend the existing `Makefile`): `build-images`, `pin-digests`, `sbom`, `smoke`, `compose-build` (see §4).

**(g) CI: upgrade the `compose` job and add a `build` job** in `.github/workflows/ci.yml` (the `compose` job today only runs `config --quiet`):
- Keep `compose` (config validation — fast, runs always).
- New `build` job (runs on `ubuntu-latest`, which has Docker + Buildx): checkout → set up Buildx → `docker compose -f deploy/docker-compose.yml build` (uses GHA build cache) → install Syft → `make sbom` → run `deploy/scripts/smoke.sh` → upload `deploy/sbom/*.cdx.json` + `deploy/build-manifest.json` as artifacts. Gate the job's pin-check via the offline pytest (also run in the `python` job) plus a CI step `deploy/scripts/pin-digests.sh --check` that fails if any pulled image/`FROM` lacks `@sha256`.
- Add a `shellcheck` step (or reuse HARD-09's) over `deploy/scripts/*.sh` (the existing `backup.sh`/`restore.sh` plus the new `pin-digests.sh`/`sbom.sh`/`smoke.sh`).
- The web `build` job already runs `pnpm -r build` (`next build`); with `output: standalone` it now also produces the standalone tree the image consumes — keep it as the fast non-Docker `next build` gate.

## 4. Public interfaces / contracts (exact signatures, env vars, config keys)

**Image-reference contract (compose `image:` + Dockerfile `FROM`).** Every *pulled* reference MUST match the regex `^[^:@\s]+:[^@\s]+@sha256:[0-9a-f]{64}$` (tag **and** digest). First-party built images (`forge/api|worker|mcp-gateway|web`) MUST match `^forge/[a-z-]+:.+$` and carry a `build:` block.

**Env vars / build args** (no secrets; all have safe defaults):

| Name | Where | Default | Purpose |
|---|---|---|---|
| `FORGE_VERSION` | compose `image:` tag for built services | `0.1.0` | tag the 4 first-party images |
| `NEXT_PUBLIC_API_URL` | web build/runtime | `/api` (prod) / `http://localhost:8000` (dev) | API base baked at build (unchanged) |
| `DOCKER_BUILDKIT` | build env | `1` | BuildKit on (Dockerfiles already use `# syntax=docker/dockerfile:1`) |
| `COMPOSE_FILE` | scripts | `deploy/docker-compose.yml` | target compose file |
| `SBOM_DIR` | `sbom.sh` | `deploy/sbom` | SBOM output dir |
| `SMOKE_TIMEOUT_SECONDS` | `smoke.sh` | `180` | max wait for `healthy` |

**Makefile targets** (added; signatures are `make <target>`):
- `make compose-build` → `docker compose -f deploy/docker-compose.yml build`
- `make build-images` → alias of `compose-build` (build all 4)
- `make pin-digests` → `deploy/scripts/pin-digests.sh` (rewrite refs + manifest)
- `make sbom` → `deploy/scripts/sbom.sh` (per-image CycloneDX SBOM)
- `make smoke` → `deploy/scripts/smoke.sh` (up → health → down)

**Script CLIs:**
- `deploy/scripts/pin-digests.sh [--check]` — without `--check`: resolve + rewrite digests + write manifest (exit 0). With `--check`: exit non-zero if any pulled image / Dockerfile `FROM` is not `@sha256`-pinned (offline-friendly: parses files, no pull).
- `deploy/scripts/sbom.sh [image…]` — default: all 4 built images; writes `${SBOM_DIR}/<image>.cdx.json`.
- `deploy/scripts/smoke.sh` — bring up, wait healthy, curl `/health`, tear down; non-zero on failure.

**`deploy/build-manifest.json` schema** (committed evidence):
```json
{
  "generated_at": "2026-06-27T00:00:00Z",
  "forge_version": "0.1.0",
  "images": {
    "pgvector/pgvector:pg16":            { "digest": "sha256:…", "kind": "pulled" },
    "redis:7.4-alpine":                  { "digest": "sha256:…", "kind": "pulled" },
    "ghcr.io/astral-sh/uv:python3.13-bookworm-slim": { "digest": "sha256:…", "kind": "base" },
    "node:22-bookworm-slim":             { "digest": "sha256:…", "kind": "base" },
    "forge/api:0.1.0":     { "digest": "sha256:…", "kind": "built", "sbom": "deploy/sbom/api.cdx.json" },
    "forge/worker:0.1.0":  { "digest": "sha256:…", "kind": "built", "sbom": "deploy/sbom/worker.cdx.json" },
    "forge/mcp-gateway:0.1.0": { "digest": "sha256:…", "kind": "built", "sbom": "deploy/sbom/mcp-gateway.cdx.json" },
    "forge/web:0.1.0":     { "digest": "sha256:…", "kind": "built", "sbom": "deploy/sbom/web.cdx.json" }
  }
}
```

**Next config key:** `output: "standalone"` in `apps/web/next.config.mjs`.

**New/changed test names** (in `tests/test_deploy_infra.py`):
`test_prod_pulled_images_pinned_by_digest`, `test_dockerfile_base_images_pinned_by_digest`, `test_dockerignore_excludes_secrets_and_heavy_paths`, `test_web_next_config_emits_standalone`, `test_build_manifest_covers_every_image`, and the networked, marker-gated `test_compose_build_all_images`, `test_compose_up_health_smoke`, `test_runtime_containers_nonroot_healthcheck_limits`.

## 5. Dependencies (other slices/foundation that must exist first)

- **Foundation deploy substrate (overnight-plan Task 0.6)** — REQUIRED, present: `deploy/docker-compose.yml`, `deploy/docker-compose.dev.yml`, the 4 Dockerfiles, `deploy/caddy/Caddyfile`, `deploy/scripts/{backup,restore}.sh`, `.github/workflows/ci.yml`, and `tests/test_deploy_infra.py`. This slice extends all of them.
- **HARD-14 (dependency re-lock)** — STRONGLY RECOMMENDED before the real build: the Python images run `uv sync --no-dev --no-editable` and the web image `pnpm install --frozen-lockfile`, so `uv.lock` + `pnpm-lock.yaml` must be complete and frozen for a reproducible, digest-stable build. If HARD-14 has not landed, the build still works off the editable workspace resolution but is not guaranteed reproducible — call this out in the build report.
- **HARD-01 (real Postgres + pgvector)** — REQUIRED for the `compose up → /health` smoke to reach `healthy`: api/worker depend on `db` (`condition: service_healthy`) and the smoke runs `alembic upgrade head` against the live `pgvector/pgvector:pg16` container. (HARD-01 already stands up that container; the smoke reuses it.)
- **HARD-09 (security audit)** — DOWNSTREAM CONSUMER, not a prerequisite: HARD-09's G-SEC-EVIDENCE pack consumes the per-image SBOMs (`deploy/sbom/*.cdx.json`) and the digest manifest produced here, and a future Trivy/Grype CVE gate scans these images. The `shellcheck` step may be shared with HARD-09.
- **Frozen `forge_contracts` / `forge_db` schema** — unaffected; this slice adds no DTOs/Protocols and no tables, so no contract or schema change is needed (asserted by the whole-suite green gate).
- **No credentials and no other HARD slice's live integration is required.** Independent of HARD-02/05/06/07-Slack/10 creds.

## 6. Acceptance criteria (numbered, testable)

> Legend: **[offline]** runs in the hermetic suite with no Docker/network (part of the whole-suite green gate); **[networked]** requires a Docker daemon + build/registry network (CI or a networked runner) and CANNOT run in the no-network sandbox; **[creds]** none in this slice.

1. **[networked]** `docker compose -f deploy/docker-compose.yml build` exits 0 and produces all 4 first-party images (`forge/api`, `forge/worker`, `forge/mcp-gateway`, `forge/web`) for `${FORGE_VERSION}`.
2. **[networked]** `cd apps/web && pnpm build` (`next build`) exits 0 and, with `output: standalone`, emits `.next/standalone/apps/web/server.js` + `.next/static/`; the web CI job stays green.
3. **[offline]** Every *pulled* image in `deploy/docker-compose.yml` **and** `deploy/docker-compose.dev.yml` is pinned `name:tag@sha256:<64-hex>` (test `test_prod_pulled_images_pinned_by_digest`); no floating tag remains; `:latest` still forbidden.
4. **[offline]** Every Dockerfile `FROM` in `deploy/docker/*.Dockerfile` is `@sha256`-pinned (`test_dockerfile_base_images_pinned_by_digest`).
5. **[networked]** `docker compose -f deploy/docker-compose.yml config` still validates after digest rewrites (existing `test_docker_compose_config_validates` passes with Docker present).
6. **[offline]** A root `.dockerignore` exists and excludes `.venv`, `node_modules`, `apps/web/.next`, `.env*` (except examples), `*.pem`/`*.key`, and `deploy/secrets` (`test_dockerignore_excludes_secrets_and_heavy_paths`).
7. **[networked]** `docker compose up -d` brings the stack to `healthy`: `db`, `redis`, `minio`, `api`, `mcp-gateway`, `web` report `(healthy)` within `start_period`, and `worker` reports `healthy` via `celery inspect ping` (`test_compose_up_health_smoke`).
8. **[networked]** `curl -fsS http://localhost:8000/health` and `…:8001/health` return HTTP 200 with `{"status":"ok"}` against the running containers (smoke step).
9. **[networked]** `docker inspect` on each app container confirms: runs as uid 1000 / non-root (`Config.User == "1000:1000"` for api/worker/mcp-gateway, `node` for web), a healthcheck is configured, and a memory limit is enforced (`HostConfig.Memory > 0`) — `test_runtime_containers_nonroot_healthcheck_limits`. Also confirms the api/mcp-gateway runtime images contain `curl` (healthcheck binary present).
10. **[networked]** A CycloneDX SBOM is generated for each of the 4 built images at `deploy/sbom/<image>.cdx.json`, each parses as valid CycloneDX JSON and lists ≥1 component.
11. **[offline]** `deploy/build-manifest.json` exists, is valid JSON matching the §4 schema, and covers every image referenced by the compose files + Dockerfiles (`test_build_manifest_covers_every_image`).
12. **[offline]** `apps/web/next.config.mjs` sets `output: "standalone"` (`test_web_next_config_emits_standalone`).
13. **[networked]** `shellcheck` passes on all `deploy/scripts/*.sh` (`backup.sh`, `restore.sh`, `pin-digests.sh`, `sbom.sh`, `smoke.sh`); wired into CI.
14. **[offline]** Whole-suite green gate holds at slice end: `uv run pytest -q`, `uv run ruff check .`, `uv run ruff format --check .`, `make typecheck` (exit 0), and `cd apps/web && pnpm test` (28) all green; the new offline tests are included and the build/smoke tests skip cleanly when Docker is absent (clear skip reason, never faked — mirroring the existing `test_docker_compose_config_validates` skip pattern).

## 7. Test plan (TDD) — unit + integration (gated on env) + how to run

**Discipline.** Write the offline assertions first (they fail RED against today's floating tags / missing `.dockerignore` / missing standalone), then make them green by editing the deploy files; add the networked tests behind a marker so the hermetic suite stays green and Docker-free.

**Offline unit tests** — extend `tests/test_deploy_infra.py` (pure file parsing, no Docker):
- `test_prod_pulled_images_pinned_by_digest` — for both compose files, every service with an `image:` (and no `build:`) matches `…:tag@sha256:[0-9a-f]{64}`; built services (`api/worker/mcp-gateway/web`) are exempt (they have `build:`). Replaces the weak `test_prod_images_are_pinned_not_latest` assertion (keep `:latest` forbidden too).
- `test_dockerfile_base_images_pinned_by_digest` — parse each `deploy/docker/*.Dockerfile`, assert every `FROM <ref>` (including the web build/runtime stages) carries `@sha256:`.
- `test_dockerignore_excludes_secrets_and_heavy_paths` — root `.dockerignore` exists and contains entries for `.venv`, `node_modules`, `apps/web/.next`, `.env`, `*.pem`, `deploy/secrets`.
- `test_web_next_config_emits_standalone` — `apps/web/next.config.mjs` text contains `output: "standalone"`.
- `test_build_manifest_covers_every_image` — `deploy/build-manifest.json` parses; its `images` keys ⊇ the set of pulled+base+built refs extracted from the compose files and Dockerfiles; each entry has a `digest` (or `kind: built` with a `sbom` path).

**Integration tests (marker-gated, networked)** — new `tests/test_build_integration.py`, each `@pytest.mark.integration` and `skip`-clean when `shutil.which("docker") is None` (reason: `"requires a Docker daemon + build network — not available in this environment"`):
- `test_compose_build_all_images` — `docker compose -f deploy/docker-compose.yml build` returns 0; `docker images` lists all 4 `forge/*` tags.
- `test_compose_up_health_smoke` — run `deploy/scripts/smoke.sh`; assert exit 0 and that the captured `/health` responses are 200 `{"status":"ok"}`; always `docker compose down -v` in teardown (fixture `yield`/finalizer).
- `test_runtime_containers_nonroot_healthcheck_limits` — after `up`, `docker inspect` each app container; assert non-root `User`, present `Healthcheck`, `HostConfig.Memory > 0`.
- `test_sbom_generated_for_each_image` — run `deploy/scripts/sbom.sh`; assert each `deploy/sbom/*.cdx.json` parses as CycloneDX with `components` non-empty.

**Marker registration.** Add `integration` to `pyproject.toml` `[tool.pytest.ini_options] markers` (shared with the other HARD slices; the production-hardening spec proposes `@pytest.mark.integration` as the single creds/network gate). The default `uv run pytest -q` deselects nothing but the integration tests self-skip without Docker; CI's `build` job runs `uv run pytest -m integration tests/test_build_integration.py` on the networked runner.

**How to run.**
```bash
# Offline (hermetic, part of the green gate; passes with or without Docker):
uv run pytest tests/test_deploy_infra.py -q

# Full build + pin + smoke + SBOM (needs Docker + network; CI or a networked host):
make pin-digests          # resolve + write digests + build-manifest.json
make build-images         # docker compose build (4 images)
cd apps/web && pnpm build # next build (standalone) — fast non-Docker gate
make sbom                 # per-image CycloneDX SBOM
make smoke                # up -> wait healthy -> curl /health -> down -v
uv run pytest -m integration tests/test_build_integration.py
shellcheck deploy/scripts/*.sh

# Verify pins without pulling (offline-friendly CI check):
deploy/scripts/pin-digests.sh --check
```

## 8. Security & policy considerations

- **Supply-chain integrity (the core security win).** Digest pinning (`@sha256`) makes every base/infra image immutable — a repointed upstream tag (compromise, typosquat, or silent rebuild) is rejected at pull instead of silently adopted. The committed `build-manifest.json` is the auditable record of exactly what shipped. This is the FORGE_SPEC "Production Docker Compose Requirements" intent made real.
- **No secrets in images or build context.** The new root `.dockerignore` excludes `.env*` (except examples), `*.pem`, `*.key`, and `deploy/secrets/` from the build context, so BYOK keys / the GitHub App `.pem` (HARD-05) / `FORGE_SECRET_KEY` can never be `COPY`-ed into a layer. An acceptance-adjacent check: the SBOM/`docker history` of each built image must contain no secret-shaped strings (a `gitleaks`-on-image scan can be added in HARD-09). Secrets remain env-injected at runtime only — never build args (`ARG`/`ENV` for secrets is forbidden; nothing in these Dockerfiles takes a secret build arg).
- **Least privilege at runtime.** Non-root execution (`user: "1000:1000"` for the Python images, `node` uid 1000 for web) is verified at runtime (§6.9), not just asserted in compose YAML. Standalone Next output shrinks the web image's package surface (smaller SBOM, fewer CVEs). Resource limits (already in compose) are confirmed live (`HostConfig.Memory > 0`) so a runaway container cannot starve the host — a DoS bound.
- **SBOM as an audit artifact.** Per-image CycloneDX SBOMs feed HARD-09's evidence pack and any downstream CVE gate (Trivy/Grype). They are the input an auditor or the named human-pentest punch-list uses to reason about the dependency attack surface.
- **No new network exposure.** This slice changes no ports, no Caddy routes, no RBAC, and no MCP/policy default. The `data` network stays `internal`; `db` stays off the edge. The smoke binds `/health` only on the loopback of the CI runner and tears the stack down with `down -v`.
- **Reproducibility = tamper-evidence.** Frozen lockfiles (HARD-14) + digest-pinned bases mean two builds of the same commit yield the same dependency closure, so a diff in the produced SBOM is a signal worth investigating rather than noise.

## 9. Effort & risk (S/M/L + risks)

**Effort: M.** Mechanically straightforward but spans many files and the verification only fully exercises on a networked runner. Rough split: digest pinning + `.dockerignore` + manifest **S**; `pin-digests.sh`/`sbom.sh`/`smoke.sh` + Makefile + CI `build` job **M**; web standalone Dockerfile rewrite **S**; offline + integration tests **S/M**.

**Risks:**
- **CANNOT run in the no-network sandbox (named limitation).** `docker compose build`, `next build` inside the image, digest *resolution* (pull), `compose up`, runtime `docker inspect`, and SBOM generation all need a Docker daemon + registry network. This slice's *networked* acceptance (AC1–2, 5, 7–10, 13) is a **CI / networked-runner gate** — exactly the gap blocker #3 names. The offline half (AC3–4, 6, 11–12, 14) lands and is verified in-sandbox. *Mitigation:* the offline lint tests prevent regressions even where Docker is absent; the integration tests skip cleanly. (High visibility, low engineering risk.)
- **Digest drift / staleness.** Pinned digests go stale as upstreams publish security fixes; pinning can *hide* available patches. *Mitigation:* `pin-digests.sh` is a deliberate, re-runnable roll-forward; pair with a scheduled Dependabot/Renovate-style digest bump (future, §12) and the HARD-09 CVE scan so stale pins are flagged.
- **Next.js standalone behavior change.** Switching to `output: standalone` changes the runtime entrypoint (`node server.js` vs `pnpm start`) and asset layout; a missing `public/` or static copy breaks first paint. *Mitigation:* the `compose up` smoke hits `:3000` for a live 200; the web CI `next build` job catches build-time failures fast.
- **Healthcheck binary assumption.** api/mcp-gateway healthchecks shell out to `curl`; a future slimmer/distroless base may not ship it. *Mitigation:* AC9 asserts `curl` presence; the fallback is a stdlib `python -c "import urllib.request,…"` probe (documented).
- **Build-context size / cache.** Without `.dockerignore` the context was the whole repo (slow, leak-prone). *Mitigation:* `.dockerignore` is part of this slice; CI uses BuildKit + GHA cache.
- **Multi-arch.** Digests are architecture-specific (or multi-arch manifest-list digests). *Mitigation:* pin the multi-arch *manifest-list* digest (what `buildx imagetools inspect` returns by default) so `amd64`/`arm64` hosts both resolve; single-arch buildx push is out of scope (§12).
- **Out-of-sandbox / human-only:** none unique to this slice beyond the networked-runner requirement; no human pentest dependency here (that stays HARD-09).

## 10. Key files / paths (exact, in the real monorepo)

- `deploy/docker-compose.yml` — digest-pin `db`, `redis`, `minio`, `caddy`, `autoheal`; keep `build:`+tag for the 4 first-party services; remove the `# PARKED … @sha256` note.
- `deploy/docker-compose.dev.yml` — digest-pin `db`, `redis`, `minio` (and any other pulled image).
- `deploy/docker/api.Dockerfile`, `deploy/docker/worker.Dockerfile`, `deploy/docker/mcp-gateway.Dockerfile` — pin `FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim@sha256:…`.
- `deploy/docker/web.Dockerfile` — pin both `FROM node:22-bookworm-slim@sha256:…` (build + runtime); rewrite runtime stage to the standalone copy (`node apps/web/server.js`).
- `apps/web/next.config.mjs` — add `output: "standalone"`.
- `.dockerignore` (NEW, repo root) — exclude heavy/secret paths.
- `deploy/scripts/pin-digests.sh` (NEW) — resolve digests, rewrite refs, write manifest; `--check` mode.
- `deploy/scripts/sbom.sh` (NEW) — per-image CycloneDX SBOM via Syft.
- `deploy/scripts/smoke.sh` (NEW) — `up` → wait healthy → curl `/health` → `down -v`.
- `deploy/build-manifest.json` (NEW, committed evidence) — digest + SBOM manifest (§4 schema).
- `deploy/sbom/*.cdx.json` (NEW, generated; release copies committed) — per-image SBOMs.
- `Makefile` — add `compose-build`, `build-images`, `pin-digests`, `sbom`, `smoke` targets.
- `.github/workflows/ci.yml` — add the `build` job (compose build + next build + smoke + SBOM upload + pin `--check`), add `shellcheck` over `deploy/scripts/*.sh`; keep the `compose` config-validation job.
- `tests/test_deploy_infra.py` — add the offline digest/`.dockerignore`/standalone/manifest assertions; tighten `test_prod_images_are_pinned_not_latest`.
- `tests/test_build_integration.py` (NEW) — marker-gated build/up/health/inspect/SBOM tests.
- `pyproject.toml` — register the `integration` pytest marker (shared across HARD slices).
- `deploy/README.md` — replace the two PARKED follow-ups (image digests / image builds) with the verified procedure + manifest reference.

## 11. Research references

- Docker — pin base images by digest (`FROM image@sha256:…`) / reproducible builds: https://docs.docker.com/build/building/best-practices/#pin-base-image-versions
- Docker Compose `config` / image reference grammar (`name:tag@digest`): https://docs.docker.com/reference/compose-file/services/#image
- Resolve a tag's digest — `docker buildx imagetools inspect`: https://docs.docker.com/reference/cli/docker/buildx/imagetools/inspect/
- Next.js — `output: "standalone"` for minimal container images: https://nextjs.org/docs/app/api-reference/config/next-config-js/output
- Next.js self-hosting / Docker guidance: https://nextjs.org/docs/app/building-your-application/deploying#docker-image
- Anchore Syft — SBOM generation (CycloneDX): https://github.com/anchore/syft
- CycloneDX SBOM spec: https://cyclonedx.org/specification/overview/
- Docker container security best practices (non-root, least privilege, healthchecks): https://docs.docker.com/develop/security-best-practices/
- ShellCheck: https://www.shellcheck.net/
- `.dockerignore` reference: https://docs.docker.com/build/concepts/context/#dockerignore-files
- OWASP Docker Security Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/Docker_Security_Cheat_Sheet.html
- Spec/report anchors: `docs/FORGE_SPEC.md` → "Production Docker Compose Requirements"; `docs/MORNING_REPORT.md` §5(8)(9) (PARKED image build + sha256 pin + shellcheck), §6 ("`docker compose build` and full `next build` were not run"), §7.3 (next step); `scratchpad/hardening-docs/SPEC-PRODUCTION-HARDENING.md` → gates **G-BUILD** + **G-IMG-PINNED**, DoD BETA #9 / PRODUCTION #14; sample slice `docs/implementation-slices/v1/F05-hybrid-knowledge-retrieval.md` §3.5 (pinned-image precedent).

## 12. Out of scope / future

- **Image signing / provenance attestation** (Sigstore `cosign`, SLSA build provenance, in-toto) — verifying *who* built an image, not just *what* is in it. A natural follow-up once digests are pinned; pairs with HARD-09.
- **CVE scanning gate** (Trivy / Grype over the built images + SBOMs) — owned by HARD-09's security-evidence pack; this slice produces the SBOM inputs it consumes.
- **Multi-arch (`linux/amd64` + `linux/arm64`) buildx + registry push/publish** — this slice builds locally and records manifest-list digests; publishing tagged+digested images to a registry (GHCR) and a release pipeline are future.
- **Distroless / `chainguard` minimal runtime bases + read-only root filesystem + dropped Linux capabilities** (`cap_drop: [ALL]`, `read_only: true`, `no-new-privileges`) — further attack-surface reduction beyond non-root.
- **Automated digest roll-forward** (Renovate/Dependabot for `@sha256` bumps) — keeps pins from going stale without manual `pin-digests.sh` runs.
- **Helm chart image-digest pinning** (F24 Kubernetes) — the Helm `values.yaml` should reuse `build-manifest.json`'s digests; out of scope until F24.
- **BuildKit cache-mount + remote build cache** tuning for CI speed beyond the basic GHA cache.
- **`reranker` self-hosted image** (F05 §3.5) digest pinning — only when that service is added to the production compose; the same pin/SBOM machinery applies.
