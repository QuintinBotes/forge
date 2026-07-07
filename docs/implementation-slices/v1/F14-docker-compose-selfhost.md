# F14 — Docker Compose Self-Hosted (production best practices)

> Phase: v1 · Spec module(s): Self-Hosting & Deployment (`deploy/`, Docker Compose Production), Monorepo `deploy/` layout, Security (non-root, network policies, secrets), Observability (healthchecks) · Status target: **Done** = a fresh Ubuntu 24.04 LTS VM (4 vCPU / 8 GB / 50 GB) can run `docker compose -f deploy/docker-compose.yml --env-file .env.production up -d --remove-orphans` and reach a fully working Forge stack over HTTPS through Caddy; every service image is pinned by `@sha256` digest, runs non-root, has a healthcheck + CPU/memory limits + capped logs, persists only to named volumes, and sits on a segmented network where the data tier (`db`/`redis`/`minio`) is unreachable from the host; the `willfarrell/autoheal` sidecar restarts any container that goes unhealthy; `forge-cli db migrate` + `forge-cli users create-admin` complete; and a backup→restore round-trip of Postgres + MinIO + the **vault KEK material (`FORGE_VAULT_KEYS`)** verifiably reproduces the workspace and decrypts a stored BYOK secret. A YAML-parsing **compose contract test suite** and a Docker-gated **end-to-end smoke test** both pass in CI, and the operator runbook lives in `deploy/README.md`. The nine `docs/self-hosting/` guides are authored and continuously verified by `v1/F15-selfhosting-docs` (this slice owns the `deploy/` artifacts they document, not the docs themselves).

---

## 1. Intent — what & why

Forge's Core Design Principle #9 is "Self-hosting is first-class — `docker compose up` delivers a working platform," and the V1 roadmap lists "Docker Compose self-hosted with production best practices" as a launch item (the sibling roadmap item "Full `docs/self-hosting/` documentation" is a **separate slice, `v1/F15-selfhosting-docs`**). This slice is the **production single-node deployment substrate**: the `deploy/docker-compose.yml` that wires together the eight V1 services (`db`, `redis`, `minio`, `api`, `worker`, `mcp-gateway`, `web`, `caddy`) plus the `autoheal` sidecar, the hardened per-app Dockerfiles those images are built from, the env-var contract, the Caddy/Nginx reverse-proxy configs, the install/backup/restore scripts, and the production-target Makefile entries + `deploy/README.md` runbook. The nine `docs/self-hosting/` guides are **not** authored here — `v1/F15-selfhosting-docs` writes and CI-verifies them against the artifacts this slice owns.

The spec is unusually prescriptive here (FORGE_SPEC.md §"Production Docker Compose Requirements", derived from https://distr.sh/blog/running-docker-in-production/), so this slice's job is to turn that checklist into **enforced, testable invariants** rather than prose:

1. **Pin all images by `@sha256` digest, not tag.**
2. **Run `willfarrell/docker-autoheal`; label every service `autoheal=true`** (except autoheal itself).
3. **Named volumes for Postgres/Redis/MinIO — never bind mounts for production data.**
4. **CPU + memory resource limits on every container.**
5. **Healthcheck on every service.**
6. **Network segmentation — separate networks for API, database, MCP gateway, observability.**
7. **All containers run as non-root.**
8. **Cap container log size** (`max-size: 100m`, `max-file: 5`).
9. **`--remove-orphans` on every compose up.**

Why this is its own slice and not folded into each app: the hardening concerns (digest pinning, non-root, network segmentation, healthcheck wiring, log caps, autoheal labelling, backup/restore) are **cross-cutting deployment policy** that must be uniform across all services and continuously verified. Centralizing them lets a single contract-test enforce them and lets every other feature slice simply *append* its env vars / service tweaks to a base that is already correct. The app source code (FastAPI handlers, Next.js pages, MCP gateway logic, Celery tasks) is owned by other slices; **this slice owns the packaging, composition, hardening, and operational scripts** (the prose self-hosting docs are owned by `v1/F15-selfhosting-docs`, the local-dev compose + dev Makefile targets by `v1/F13-local-quickstart`).

---

## 2. User-facing behavior / journeys

The "user" here is a **self-hosting operator** (admin running Forge on their own VM), not an end-user clicking the board. Journeys map to the spec's "Docker Compose Production" runbook.

**J1 — First production install (operator, ~15 min on a clean VM).**
```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER && newgrp docker

git clone https://github.com/QuintinBotes/forge && cd forge
sudo cp deploy/docker/daemon.json /etc/docker/daemon.json && sudo systemctl restart docker  # log caps
cp deploy/.env.production.example .env.production
$EDITOR .env.production   # fill SECRET_KEY, AUTH_SECRET, API_KEY_PEPPER, FORGE_VAULT_KEYS, INTERNAL_SERVICE_TOKEN,
                          #   POSTGRES_PASSWORD, REDIS_PASSWORD, MINIO_ROOT_*, GITHUB_APP_*, MODEL_PROVIDER_KEY, DOMAIN, ACME_EMAIL
bash deploy/scripts/preflight.sh --env-file .env.production    # validates env, ports 80/443 free, disk/RAM, daemon.json
docker compose -f deploy/docker-compose.yml --env-file .env.production up -d --remove-orphans
docker compose -f deploy/docker-compose.yml --env-file .env.production exec api forge-cli db migrate
docker compose -f deploy/docker-compose.yml --env-file .env.production exec api forge-cli users create-admin
```
Operator opens `https://$DOMAIN`, Caddy has already provisioned a Let's Encrypt cert, the Forge login page loads, and the admin signs in. The whole flow is the spec's runbook verbatim, with a `preflight.sh` gate added so misconfigurations fail before `up`.

**J2 — One-command bootstrap (operator).** `bash deploy/scripts/install.sh` performs J1 non-interactively (installs Docker if absent, writes daemon.json, prompts for / accepts `--env-file`, runs preflight, `up`, `migrate`, `create-admin`), idempotent on re-run.

**J3 — Self-healing (system, observable).** A container's healthcheck starts failing (e.g. `api` loses its DB connection). After `AUTOHEAL_INTERVAL` the autoheal sidecar restarts only that container; the operator sees the restart in `docker compose ps` / logs without manual action.

**J4 — Backup before upgrade (operator).** `bash deploy/scripts/backup.sh --env-file .env.production --out /var/backups/forge` produces a single timestamped, checksummed archive containing a Postgres dump (includes the immutable `audit_log` and the encrypted BYOK secret rows), a MinIO mirror, and the vault KEK material (`FORGE_VAULT_KEYS` + `FORGE_VAULT_ACTIVE_KEY_VERSION`, extracted from `--env-file` — the KEK is env-resident per `cross-cutting/F37-auth-secrets-byok`, never stored in Postgres). `restore.sh` reverses it onto a clean stack and prints a verification summary.

**J5 — Safe upgrade with rollback (operator).** Following `docs/self-hosting/upgrade.md`: `backup.sh` → bump the pinned digests in `.env.production` (or pull a tagged release that ships new digests) → `up -d --remove-orphans` → `forge-cli db migrate`. If health does not go green within the start-period, roll back digests and `restore.sh`.

**J6 — Local dev (contributor).** `make dev` (`docker-compose.dev.yml`) gives hot-reload app containers, exposed DB/Redis/MinIO ports, and no Caddy/TLS — explicitly **not** production-hardened. The dev compose file and the `setup`/`dev`/`seed`/`migrate`/`reset`/`doctor` Make targets are owned by `v1/F13-local-quickstart`; this slice's production contract test deliberately does **not** run against the dev file (F13 lints it separately).

---

## 3. Vertical slice

### 3.1 Data model (tables/columns/migrations touched)

**N/A — this is a deployment/infra slice; it introduces no tables, columns, or migrations.** It *invokes* migrations via `forge-cli db migrate` (the migration chain is owned by the data-model foundation slice and each feature slice) and *operates on* the database at the volume/backup level. The only "schema" this slice owns is YAML (compose) and env (`.env.*`) — frozen in §4.

### 3.2 Backend (FastAPI routes + services/packages)

This slice does **not** implement business routes, but it **freezes and depends on a health-endpoint contract** that the API and MCP gateway must expose for compose healthchecks and the autoheal sidecar to work. The endpoint *implementations* belong to the app-foundation slices (see §5); this slice (a) defines the contract, (b) wires the compose `healthcheck:` blocks against it, and (c) fails the e2e smoke test if any endpoint is missing or wrong.

| Service | Endpoint | Semantics |
|---|---|---|
| `api` | `GET /healthz` | Liveness. No dependency checks. Always `200 {"status":"ok"}` if the process is serving. |
| `api` | `GET /readyz` | Readiness. `200` only when Postgres + Redis + MinIO are reachable; `503 {"status":"not_ready","failed":[...]}` otherwise. Used as the compose healthcheck so a service is "healthy" only when usable. |
| `mcp-gateway` | `GET /healthz` | Liveness (process up). |
| `web` | `GET /healthz` | Next.js route handler returning `200 {"status":"ok"}` (liveness; does not call the API). |

The `forge-cli` subcommands this slice's runbook/scripts invoke (owned by `v1/F00-foundation-substrate` for the `db` group and `cross-cutting/F37-auth-secrets-byok` for the `users`/`secrets` groups, contract-frozen here): `forge-cli db migrate`, `forge-cli db current` (asserts head after migrate), `forge-cli users create-admin [--email --password|--prompt]`, `forge-cli secrets rotate-key` (KEK rotation; referenced by `upgrade.md`'s rotation guidance, owned by F15). **There is no `forge-cli vault export-key`/`import-key`** — the vault master key (KEK) is *env-resident* (`FORGE_VAULT_KEYS`, see §4.2), so `backup.sh`/`restore.sh` capture and restore it from the `--env-file`, not via a CLI export (corrected against the F37 contract). DB connectivity at runtime is proven by the `api` `/readyz` probe (below), not by a CLI call, so `preflight.sh` performs **no** DB probe (it runs before the stack is up).

### 3.3 Worker / agent runtime (Celery tasks, LangGraph, if any)

No Celery tasks or LangGraph graphs are authored here. This slice **packages and composes** the `worker` service and defines its healthcheck. Because Celery exposes no HTTP port, the worker healthcheck uses a broker ping:

```
healthcheck:
  test: ["CMD-SHELL", "celery -A forge_worker.app inspect ping -d celery@$$HOSTNAME --timeout 5 | grep -q pong"]
```

The worker shares the `data` network (Redis broker + Postgres + MinIO) and the `backend` network (to reach `mcp-gateway` and for outbound egress to model providers / GitHub), and mounts the `forge_repos` named volume (worktree mirrors) read-write, same volume the `api` mounts. It does **not** join `mcp-egress` (only the gateway speaks to external MCP servers). No agent code is in scope.

### 3.4 Frontend / UI (Next.js routes/components, if any)

No product UI. The only frontend artifact this slice **requires** is the `web` service's `GET /healthz` route handler (contract in §3.2) and a production Dockerfile that runs the Next.js standalone server as a non-root user. The route implementation is owned by the web-foundation slice; this slice authors `apps/web/Dockerfile` and wires the healthcheck. (`apps/web/app/healthz/route.ts` is listed in §10 as a contract dependency, not a deliverable of this slice's UI work.)

### 3.5 Infra / deploy (compose, helm, caddy, if any)

**This is the entire slice.** Deliverables under `deploy/` (paths exact, per the monorepo layout in FORGE_SPEC.md):

**Compose files**
- `deploy/docker-compose.yml` — production single-node base. Eight V1 services + `autoheal`. Optional V2 services (`temporal`, `prometheus`, `grafana`, `loki`) declared behind a Compose `profiles: [observability]` / `profiles: [temporal]` so they do **not** start by default but ship in the file (matching the spec's "Optional (V2)" comment block). **Owned here.**
- `deploy/docker-compose.override.yml.example` — template for the gitignored local override. **Owned here.**
- `deploy/docker-compose.dev.yml` — local dev (hot reload, exposed infra ports, no Caddy/TLS). **Owned by `v1/F13-local-quickstart`, not this slice**; listed only because the production contract test must explicitly *exclude* it.

**Shared YAML extension fields (anchors)** at the top of `docker-compose.yml` to keep hardening DRY and uniform:
- `x-logging: &logging` → `driver: json-file`, `options: {max-size: "100m", max-file: "5"}`.
- `x-healthcheck-defaults: &hc` → `interval: 15s`, `timeout: 5s`, `retries: 5`, `start_period: 60s`.
- `x-security: &security` → `security_opt: ["no-new-privileges:true"]`, `cap_drop: ["ALL"]`, `restart: unless-stopped`.
- `x-app-image-defaults`, `x-resources-small/medium` reservation+limit presets.

**Per-service hardening matrix** (every service merges `*logging`, `*security`, a healthcheck, resource limits, the `autoheal=true` label, and joins only the networks it needs):

| Service | Image (digest-pinned) | Networks | Volumes | User | Published ports |
|---|---|---|---|---|---|
| `db` | `pgvector/pgvector:pg16@sha256:…` | `data` | `forge_pgdata:/var/lib/postgresql/data` | `postgres` (image default, non-root) | none |
| `redis` | `redis:7-alpine@sha256:…` (`--requirepass`, AOF on) | `data` | `forge_redisdata:/data` | `redis` (image default) | none |
| `minio` | `minio/minio:RELEASE…@sha256:…` | `data` | `forge_miniodata:/data` | `minio` via `user: "10002:10002"` | none |
| `api` | `ghcr.io/forge-platform/forge-api@sha256:…` | `edge`,`backend`,`data` | `forge_repos:/var/lib/forge/repos` | `10001:10001` | none (Caddy fronts it) |
| `worker` | `ghcr.io/forge-platform/forge-worker@sha256:…` | `backend`,`data` | `forge_repos:/var/lib/forge/repos` | `10001:10001` | none |
| `mcp-gateway` | `ghcr.io/forge-platform/forge-mcp-gateway@sha256:…` | `backend`,`data`,`mcp-egress` | none | `10003:10003` | none |
| `web` | `ghcr.io/forge-platform/forge-web@sha256:…` | `edge`,`backend` | none | `10004:10004` | none |
| `caddy` | `caddy:2-alpine@sha256:…` | `edge` | `forge_caddydata:/data`, `forge_caddyconfig:/config`, `./caddy/Caddyfile:/etc/caddy/Caddyfile:ro` | non-root + `cap_add: [NET_BIND_SERVICE]` | **80,443** (only published service) |
| `autoheal` | `willfarrell/autoheal:1.2.0@sha256:…` | none | `/var/run/docker.sock:/var/run/docker.sock:ro` | root (documented exception, §8) | none |

**Networks** (`docker-compose.yml`) — the four segmented networks the spec calls for ("separate networks for API, database, MCP gateway, observability") plus `edge` for ingress; topology matches the network contract in `v1/F09-mcp-gateway-v1`:
```yaml
networks:
  edge:           # caddy <-> web/api ingress; caddy is the only host-published service
  backend:        # API segment: service-to-service (api, worker, web, mcp-gateway) + outbound egress
  data:           # database segment: db, redis, minio + their consumers (api, worker, mcp-gateway) — NO host egress/ingress
    internal: true
  mcp-egress:     # MCP-gateway segment: gateway's OUTBOUND path to external MCP servers (has egress; gateway only)
  observability:  # V2 profile only: prometheus/grafana/loki + scrape targets
    internal: true
```
`data` and `observability` are `internal: true` (no outbound internet, no host port). The data-tier services (`db`/`redis`/`minio`) attach **only** to `data` and publish **no** ports — they are unreachable from the host and from the public internet. `mcp-gateway` joins `data` because it reads `mcp_connections` + the BYOK vault rows and writes the immutable `mcp_audit_log` directly (per `v1/F09-mcp-gateway-v1`); `api`/`worker` reach the gateway over `backend`; and the gateway joins `mcp-egress` for its outbound calls to external MCP servers. No `api`/`worker` service joins `mcp-egress`.

**Named volumes**: `forge_pgdata`, `forge_redisdata`, `forge_miniodata`, `forge_caddydata`, `forge_caddyconfig`, `forge_repos`. All `driver: local`. **No bind mounts for production data** (the only bind mounts permitted are read-only config files such as the Caddyfile).

**Reverse proxy**
- `deploy/caddy/Caddyfile` — auto-HTTPS for `${DOMAIN}` via `${ACME_EMAIL}`; routes `/api/*`, `/healthz`, `/readyz`, `/integrations/github/webhook`, WebSocket/SSE upgrades → `api:8000`; everything else → `web:10004`. The GitHub webhook path is proxied **without body transformation** (raw-body HMAC requirement from F03). Security headers (HSTS, X-Content-Type-Options, Referrer-Policy), gzip/zstd, request-body size cap.
- `deploy/nginx/forge.conf` — functionally equivalent Nginx alternative (manual TLS / certbot) documented in `reverse-proxy.md`.

**Host Docker config**
- `deploy/docker/daemon.json` — log caps applied at the daemon level (`{"log-driver":"json-file","log-opts":{"max-size":"100m","max-file":"5"},"live-restore":true}`); installed to `/etc/docker/daemon.json` by `install.sh`. (Belt-and-suspenders: per-service `*logging` anchor also sets caps so logs are bounded even if the operator skips daemon.json.)

**Scripts** (`deploy/scripts/`, POSIX `sh`, `set -euo pipefail`)
- `install.sh`, `preflight.sh`, `backup.sh`, `restore.sh`, `healthcheck.sh` (poll all compose healthchecks until green or timeout), `pin-digests.sh` (resolve current tags → digests to refresh the pins).

**Env**
- `deploy/.env.production.example` (production) — schema in §4.2. **Owned here.** The dev `deploy/.env.example` is owned by `v1/F13-local-quickstart` (its `make setup` copies it to `.env`); this slice only keeps the production example in sync with the production compose file.

**Makefile** (repo root, **co-owned** — disjoint target sets). `v1/F13-local-quickstart` owns the dev targets (`setup`, `dev`, `seed`, `migrate`, `reset`, `down`, `doctor`, all bound to `docker-compose.dev.yml`). This slice owns the **production** targets, namespaced `prod-*` so they never collide with F13's dev `down`/`migrate`/`up` semantics: `prod-up`, `prod-down`, `prod-logs`, `prod-ps`, `prod-pull`, `prod-migrate`, plus the unambiguous `backup`, `restore`, `create-admin`, `verify`. Every production `up`/`down` invocation passes `--remove-orphans` and `-f deploy/docker-compose.yml --env-file .env.production` (e.g. `prod-up: docker compose -f deploy/docker-compose.yml --env-file .env.production up -d --remove-orphans`).

**Operator runbook**
- `deploy/README.md` — the in-repo production runbook (the exact `prod-up` → `prod-migrate` → `create-admin` sequence with `--remove-orphans`), an env-var quick reference pointing at `.env.production.example`, and a link to the full guides under `docs/self-hosting/` (authored by `v1/F15-selfhosting-docs`). This is the only Markdown this slice authors; it does **not** duplicate the nine guides.

**Per-app production Dockerfiles** (authored & hardened here; source code from app slices): `apps/api/Dockerfile`, `apps/worker/Dockerfile`, `apps/mcp-gateway/Dockerfile`, `apps/web/Dockerfile`. All multi-stage, pinned base digests, non-root `USER`, embedded `HEALTHCHECK`, `.dockerignore`, `uv`-based Python builds / Next.js `output: "standalone"`.

**Helm**: **N/A — Kubernetes/Helm is V2** (`deploy/helm/` exists in the layout but is out of scope here, owned by `v2/F24-kubernetes-helm`; the `docs/self-hosting/kubernetes.md` preview/pointer page is authored by `v1/F15-selfhosting-docs`, not here).

---

## 4. Public interfaces / contracts (exact signatures, Pydantic models, Protocols, YAML schemas)

This slice's "interfaces" are the compose service schema, the env-var schema, and the script CLIs. They are validated by the contract test suite (§7).

### 4.1 `docker-compose.yml` service invariant schema (enforced per service, except `autoheal`)

Every service in `deploy/docker-compose.yml` MUST satisfy:

```yaml
<service>:
  image: <registry>/<name>@sha256:<64-hex>      # digest-pinned; tag-only is a contract failure
  <<: [*security]                               # no-new-privileges, cap_drop ALL, restart: unless-stopped
  user: "<uid>:<gid>"                           # non-root (uid != 0); image-default-non-root services exempt by allowlist
  healthcheck:                                  # present and non-trivial (not ["CMD","true"])
    test: [...]
    <<: *hc
  deploy:
    resources:
      limits:   {cpus: "<n>", memory: "<n>m|g"}  # REQUIRED
      reservations: {cpus: "<n>", memory: "<n>m|g"}
  logging: *logging                             # max-size 100m / max-file 5
  labels:
    autoheal: "true"                            # REQUIRED on all but the autoheal service
  networks: [ ... ]                             # explicit; no service on the implicit default network
```

Data-tier services (`db`,`redis`,`minio`) MUST have **no** `ports:` key. `caddy` is the **only** service permitted a published `ports:` (80/443). The `data` and `observability` networks MUST set `internal: true`.

### 4.2 `.env.production.example` schema (every key documented; required keys have no default)

| Key | Req | Example / default | Used by | Notes |
|---|---|---|---|---|
| `DOMAIN` | ✅ | `forge.example.com` | caddy, api | Public FQDN; drives TLS + `FORGE_PUBLIC_URL`. |
| `ACME_EMAIL` | ✅ | `ops@example.com` | caddy | Let's Encrypt registration. |
| `SECRET_KEY` | ✅ | (64-char random) | api, worker | App signing/session key. `preflight.sh` rejects < 32 chars / the example value. |
| `AUTH_SECRET` | ✅ | (64-char random) | api, web | Auth/JWT signing secret (`cross-cutting/F37-auth-secrets-byok`; HS256). Distinct from `SECRET_KEY`. `preflight.sh` rejects < 32 chars / placeholder. |
| `API_KEY_PEPPER` | ✅ | (random) | api | Pepper for hashed platform API keys (F37). `preflight.sh` rejects placeholder. |
| `FORGE_VAULT_KEYS` | ✅ | `1:<base64-32B>` | api, worker | **Vault master-key (KEK) map**, versioned `"v:<base64-32B>[,v:<…>]"` (F37). Required, **no default** — startup fails closed if absent. This is the "secrets key" the backup must preserve; the KEK is env-resident, never stored in Postgres. |
| `FORGE_VAULT_ACTIVE_KEY_VERSION` | ✅ | `1` | api, worker | Which `FORGE_VAULT_KEYS` version new ciphertexts use (F37). `preflight.sh` checks it indexes a present version. |
| `INTERNAL_SERVICE_TOKEN` | ✅ | (random) | api, worker, mcp-gateway | Service-to-service token authenticating `apps/api`↔`mcp-gateway` internal calls (F37/F09). |
| `OAUTH_GOOGLE_CLIENT_ID` / `_SECRET` | ◻ | — | api | Google sign-in (F37). Optional: `users create-admin` does not require OAuth. |
| `OAUTH_GITHUB_CLIENT_ID` / `_SECRET` | ◻ | — | api | GitHub sign-in (F37). Distinct from the `GITHUB_APP_*` repo-integration creds below. |
| `OAUTH_GITLAB_CLIENT_ID` / `_SECRET` | ◻ | — | api | GitLab sign-in (F37). |
| `POSTGRES_USER` | ✅ | `forge` | db, api, worker | |
| `POSTGRES_PASSWORD` | ✅ | (random) | db, api, worker | spec's `DB_PASSWORD`. |
| `POSTGRES_DB` | ✅ | `forge` | db, api, worker | |
| `DATABASE_URL` | derived | `postgresql+asyncpg://${POSTGRES_USER}:${POSTGRES_PASSWORD}@db:5432/${POSTGRES_DB}` | api, worker | Built from the above; no host. |
| `REDIS_PASSWORD` | ✅ | (random) | redis, api, worker | |
| `REDIS_URL` | derived | `redis://:${REDIS_PASSWORD}@redis:6379/0` | api, worker | |
| `MINIO_ROOT_USER` | ✅ | `forge` | minio, api, worker | |
| `MINIO_ROOT_PASSWORD` | ✅ | (random, ≥ 8) | minio, api, worker | |
| `MINIO_ENDPOINT` | derived | `http://minio:9000` | api, worker | Internal endpoint. |
| `MODEL_PROVIDER_KEY` | ✅ | `sk-…` | api, worker | BYOK default model key (bootstrap; per-workspace keys live in the vault). |
| `GITHUB_APP_ID` | ✅ | `123456` | api, worker | (F03; appended here.) |
| `GITHUB_APP_PRIVATE_KEY_PATH` | ✅ | `/run/secrets/github_app.pem` | api, worker | Mounted, not inlined. |
| `GITHUB_APP_WEBHOOK_SECRET` | ✅ | (random) | api | F03 raw-body HMAC. |
| `GITHUB_APP_CLIENT_ID` / `_SECRET` / `_SLUG` | ✅ | — | api | F03. |
| `FORGE_PUBLIC_URL` | derived | `https://${DOMAIN}` | api, web | Webhook + OAuth callback base. |
| `FORGE_DATA_DIR` | ◻ | `/var/lib/forge` | api, worker | Repo-mirror root inside the `forge_repos` volume. |
| `LOG_LEVEL` | ◻ | `info` | all apps | |
| `FORGE_API_IMAGE` / `_WORKER_` / `_MCP_` / `_WEB_` | ✅ | `ghcr.io/forge-platform/forge-api@sha256:…` | compose | Digest pins, overridable per env for upgrades/rollback. |
| `POSTGRES_IMAGE` / `REDIS_IMAGE` / `MINIO_IMAGE` / `CADDY_IMAGE` / `AUTOHEAL_IMAGE` | ✅ | `…@sha256:…` | compose | Third-party digest pins. |
| `AUTOHEAL_INTERVAL` | ◻ | `10` | autoheal | Seconds between checks. |
| `AUTOHEAL_START_PERIOD` | ◻ | `300` | autoheal | Grace before first restart. |

**Contract rule (enforced by `test_env_schema_in_sync` in `test_compose_contract.py`):** (a) **no undocumented interpolation** — every `${VAR}` referenced in `docker-compose.yml` MUST be a key documented in `.env.production.example`; and (b) **no required-key drift** — every key marked ✅ required in `.env.production.example` MUST reach the app either as a `${VAR}` interpolation **or** via a service's `env_file: .env.production` / `environment:` passthrough (so a required key can never be silently dropped). The relationship is intentionally a *subset* for interpolations (not equality): app-consumed keys such as `SECRET_KEY`, `AUTH_SECRET`, `FORGE_VAULT_KEYS`, and `LOG_LEVEL` are delivered through `env_file` without appearing as literal `${VAR}` substitutions in the YAML. Required **secret/config** keys carry **no value** in the example file and are present-but-blank with a comment. The one exception is the `*_IMAGE` digest-pin keys: they are required *and* carry the canonical `@sha256` value in the example (so `docker compose config` renders valid image refs and AC2 passes); `pin-digests.sh --write` is the only writer of those values.

### 4.3 Script CLI contracts

```text
preflight.sh   --env-file <path>
  Exit 0 iff: every ✅-required env key present & non-placeholder; SECRET_KEY and AUTH_SECRET each ≥ 32 chars;
  FORGE_VAULT_KEYS parses as a versioned KEK map (each value base64 of 32 bytes) and FORGE_VAULT_ACTIVE_KEY_VERSION
  indexes a present version (fail-closed, mirrors F37 startup validation); API_KEY_PEPPER + INTERNAL_SERVICE_TOKEN present;
  ports 80/443 free; disk ≥ 50 GB free on Docker root; RAM ≥ 8 GB; docker + compose v2 present; daemon log caps set.
  Exit 1 with a per-check failure list otherwise. No mutations. Runs BEFORE `up`, so it performs no DB/Redis/MinIO probe.

install.sh     [--env-file <path>] [--non-interactive] [--skip-docker-install]
  Installs Docker if absent, writes /etc/docker/daemon.json, runs preflight, `up -d --remove-orphans`,
  `db migrate`, `users create-admin`. Idempotent. Exit 0 only when healthcheck.sh reports all-green.

healthcheck.sh --env-file <path> [--timeout 300]
  Polls `docker compose ps --format json` until every service is healthy/running or timeout.
  Exit 0 all-green; Exit 1 on timeout printing the unhealthy set.

backup.sh      --env-file <path> --out <dir> [--name <label>]
  Produces <dir>/forge-backup-<UTC-timestamp>.tar.gz containing:
    postgres.dump          (pg_dump -Fc via `docker compose exec -T db`; includes the immutable audit_log
                            and the AES-256-GCM-encrypted BYOK secret rows)
    minio/                 (mc mirror of all buckets)
    vault-keys.env         (FORGE_VAULT_KEYS + FORGE_VAULT_ACTIVE_KEY_VERSION extracted from --env-file; chmod 600
                            — the KEK is env-resident per cross-cutting/F37-auth-secrets-byok, NOT in Postgres,
                            so without it the restored encrypted secrets are unrecoverable; there is no
                            `forge-cli vault export-key`)
    backup-manifest.json   ({version, created_at, image_digests, pg_row_counts:{table:int},
                            minio_object_count:int, members:[{name, sha256}]})
  The manifest name + the {version, created_at, pg_row_counts, minio_object_count} fields are the contract
  v1/F15-selfhosting-docs' verify-restore drill reads as ground truth (image_digests + members are this slice's
  supply-chain extension). Writes <archive>.sha256 alongside. vault-keys.env is hashed (not plaintext) in the
  manifest. Exit 0 on success; non-zero if any member fails (no partial archive left behind).

restore.sh     --env-file <path> --archive <path> [--yes] [--restore-keys]
  Verifies <archive>.sha256, quiesces app services (`stop api worker mcp-gateway web`), restores postgres.dump
  (pg_restore --clean --if-exists), mirrors MinIO back, then VALIDATES the vault KEK: the target --env-file must
  carry the FORGE_VAULT_KEYS version(s) needed to decrypt the restored ciphertexts and exit-fails closed if the
  active version is absent (with --restore-keys it instead installs vault-keys.env into the target env file).
  Runs `db migrate`, restarts, runs healthcheck.sh. Prints a verification summary (row counts of key tables vs
  manifest, bucket object counts, and a decrypt-probe of one BYOK secret). Exit 0 only when green.

pin-digests.sh [--write] [--check]
  Default: resolves the current floating tag of each image to its @sha256 digest and prints them.
  --write: updates the *_IMAGE values in .env.production.example. --check: exit 0 only if every *_IMAGE in
  .env.production.example is already @sha256-pinned (used by the AC19 handshake test + CI). Used to refresh pins
  for releases; the trivy/docker-scout scan step that precedes a pin bump is documented by F15's upgrade.md.
```

---

## 5. Dependencies — features/slices that must exist first

Dependencies are referenced by `<phase>/<id>-<slug>` path. The foundation slice does not yet have a file in this index; it is referred to across sibling slices as `v1/F00-foundation-substrate` (a.k.a. `cross-cutting/C01-monorepo-and-api-foundations`) — match by slug.

- **`v1/F00-foundation-substrate`** (REQUIRED) — the `apps/{api,worker,mcp-gateway,web}` skeletons that the Dockerfiles build, the `forge-cli` entrypoint (`db migrate`, `db current`), the Celery app (`forge_worker.app`), the four RBAC roles, the api `GET /healthz`+`/readyz` and web `GET /healthz` handlers, and the SQLAlchemy/Alembic + Redis + MinIO client wiring the services boot against. Without runnable apps there is nothing to compose.
- **`cross-cutting/F37-auth-secrets-byok`** (REQUIRED for full runbook) — provides `forge-cli users create-admin` and `secrets rotate-key`, the AES-256-GCM envelope vault whose **env-resident KEK** (`FORGE_VAULT_KEYS` / `FORGE_VAULT_ACTIVE_KEY_VERSION`) `backup.sh`/`restore.sh` capture and validate, and the `SECRET_KEY`/`AUTH_SECRET`/`API_KEY_PEPPER`/`INTERNAL_SERVICE_TOKEN` consumers (all added to §4.2). The compose stack will *start* a bare api only if these env keys are present (F37 fails closed otherwise); J1's create-admin step and the secrets-backup round-trip require it.
- **`v1/F09-mcp-gateway-v1`** (REQUIRED for the `mcp-gateway` service + its network contract) — owns the `apps/mcp-gateway` service this slice composes, its `GET /healthz` liveness probe (frozen in §3.2), and the network topology this slice mirrors (gateway on `backend` + `data` + `mcp-egress`).
- **`cross-cutting/F39-audit-log`** (REQUIRED only for backup completeness) — owns the immutable, tamper-evident `audit_log`; this slice's `backup.sh`/`restore.sh` must preserve it byte-for-byte (it rides inside `postgres.dump`), so the spec non-negotiable "audit log … immutable, queryable" survives a restore.

**Consumers (NOT prerequisites — they append to artifacts this slice owns):**
- `v1/F15-selfhosting-docs` — authors and CI-verifies the nine `docs/self-hosting/` guides against the `deploy/` artifacts owned here, and adds `deploy/scripts/verify-restore.sh` + `verify-upgrade.sh` (it consumes `backup-manifest.json`, the compose file, Caddyfile, and `.env.production.example`).
- `v1/F03-github-app` — appends `GITHUB_APP_*` env, the `forge_repos` named volume, and the Caddy raw-body webhook route (all already accommodated here so F03 only fills values).
- `v1/F05-hybrid-knowledge-retrieval` — needs the `pgvector/pgvector` image (this slice pins it) and the `db` `CREATE EXTENSION vector` (run by F05's migration, not here).
- `cross-cutting/F38-observability-cost-metrics` (V2 wiring) — the `prometheus`/`grafana`/`loki` profile stub lands here; real dashboards/config are V2.

---

## 6. Acceptance criteria (numbered, testable)

Each criterion is checked by a named test in §7 (contract suite = static YAML/file checks; e2e = Docker-gated).

1. **Compose validity.** `docker compose -f deploy/docker-compose.yml --env-file deploy/.env.production.example config --quiet` exits 0 (renders with the example env). *(contract)*
2. **Digest pinning.** Every `image:` in `docker-compose.yml` (after env interpolation) matches `…@sha256:[0-9a-f]{64}` — no floating tags. *(contract)*
3. **Healthcheck coverage.** Every service except `autoheal` declares a non-trivial `healthcheck.test` (not `true`/empty). *(contract)*
4. **Resource limits.** Every service declares `deploy.resources.limits.cpus` and `…memory`. *(contract)*
5. **Non-root.** Every service either sets `user:` to a non-zero uid or is on the documented image-default-non-root allowlist (`db`,`redis`,`caddy`); `autoheal` is the only root container and is explicitly allow-listed with a justification comment. *(contract)*
6. **Autoheal labelling.** Every service except `autoheal` carries `labels.autoheal: "true"`; the `autoheal` service mounts `/var/run/docker.sock` read-only and sets `AUTOHEAL_CONTAINER_LABEL=autoheal`. *(contract)*
7. **Named volumes only.** No service uses a bind mount for data; the only bind mounts present are read-only config files (`:ro`). Postgres/Redis/MinIO data each map to a named volume under `volumes:`. *(contract)*
8. **Log caps.** Every service sets `logging.options.max-size: "100m"` and `max-file: "5"`; `deploy/docker/daemon.json` sets the same daemon-level defaults. *(contract)*
9. **Network segmentation.** `data` and `observability` networks are `internal: true`; `db`/`redis`/`minio` attach **only** to `data` and declare **no** `ports:`; `caddy` is the **only** service with published `ports:` (80,443); `mcp-egress` is **not** internal (gateway needs outbound) and `mcp-gateway` is the only service on it; `mcp-gateway` is also on `data` (per `v1/F09-mcp-gateway-v1`). *(contract)*
10. **Env schema sync.** Every `${VAR}` ref in `docker-compose.yml` is a documented key in `.env.production.example` (no undocumented interpolations — a *subset* relation, since `env_file` passthrough vars like `SECRET_KEY`/`FORGE_VAULT_KEYS` are not literal `${VAR}` substitutions), and every ✅-required example key reaches the app via an interpolation **or** an `env_file`/`environment` passthrough (no required-key drift); every required secret/config key is present-but-blank with a comment (the `*_IMAGE` pins are the exception — required and carrying the canonical `@sha256` value so AC2 renders). *(contract — `test_env_schema_in_sync`)*
11. **Caddyfile validity & raw-body webhook.** `caddy validate --config deploy/caddy/Caddyfile` succeeds (with required env stubbed), and the `/integrations/github/webhook` route has no `request_body`/rewrite directive. *(contract)*
12. **`--remove-orphans` everywhere.** Every `docker compose … up`/`down` invocation in this slice's artifacts — the `Makefile` `prod-*` targets, `install.sh`, `restore.sh`, and `deploy/README.md` — includes `--remove-orphans`. (The dev compose targets are F13's; the docs guides are F15's, separately CI-checked.) *(contract: grep assertion)*
13. **Stack boots & is reachable.** `up -d --remove-orphans` against a test env brings all services to healthy within the start-period; `curl -fk https://localhost/healthz` through Caddy returns `200`; `curl http://localhost:8000` from the host **fails** (api not host-published). *(e2e)*
14. **Migrate + admin.** `forge-cli db migrate` then `forge-cli users create-admin --email … --password …` both exit 0 against the running stack; a follow-up `db current` shows head. *(e2e)*
15. **Data tier isolation at runtime.** From the host, TCP connect to `5432`/`6379`/`9000` is refused (no published ports); from inside the `web` container, `db`/`redis` are **not** resolvable/reachable (web is not on `data`). *(e2e)*
16. **Autoheal restarts unhealthy.** Killing the api's readiness dependency (or `docker kill --signal=SIGSTOP` to force unhealthy) leads the autoheal sidecar to restart only the api container within `AUTOHEAL_INTERVAL + grace`, observed via container start-count increment. *(e2e, may be marked slow)*
17. **Backup→restore round-trip (incl. secrets key).** After seeding a workspace, an object in MinIO, and a **stored BYOK provider secret** (encrypted under the active KEK), `backup.sh` produces a checksum-valid archive whose `backup-manifest.json` carries the F15 contract fields; `restore.sh` onto a freshly recreated stack (volumes pruned) reproduces the workspace row, the MinIO object, and **decrypts the BYOK secret to its original plaintext** (proving the env-resident KEK was preserved); restore with a missing active KEK version fails closed; `healthcheck.sh` goes green. *(e2e, slow)*
18. **Preflight gating.** `preflight.sh` exits non-zero when `SECRET_KEY` is the placeholder / < 32 chars, when a required key is missing, or when port 80 is occupied — each with a specific message. *(unit: bats/subprocess)*
19. **Operator handshake (artifacts, not docs).** (a) Every `deploy/scripts/*.sh` prints its usage/flag contract on `--help` and exits 0. (b) `deploy/README.md` exists, contains the exact production `prod-up` → `prod-migrate` → `create-admin` sequence (each compose invocation with `--remove-orphans`), and links to `docs/self-hosting/` (authored by `v1/F15-selfhosting-docs`). (c) `pin-digests.sh --check` exits 0 only when every `*_IMAGE` in `.env.production.example` is `@sha256`-pinned. The nine prose guides themselves are F15's deliverable, asserted by F15's harness — **not** here. *(contract: file/flag/grep assertions)*
20. **Image hardening.** Each `apps/*/Dockerfile` declares a non-root `USER`, a `HEALTHCHECK`, and pins its base image by digest; `apps/*/.dockerignore` exists. *(contract: Dockerfile lint)*

---

## 7. Test plan (TDD) — concrete test cases (unit + integration), key fixtures

Tests live under `deploy/tests/` and run with `pytest`. Two tiers: **contract** (pure file/YAML parsing, no Docker — always run in CI) and **e2e** (Docker-gated behind `@pytest.mark.docker`, run in a CI job that has Docker). Write the contract tests first; they will fail red against an empty `deploy/` and drive the compose file into existence criterion by criterion.

**Contract suite — `deploy/tests/test_compose_contract.py`** (loads `docker-compose.yml` via `ruamel.yaml`, resolves anchors, interpolates `.env.production.example` with a helper):
- `test_compose_config_renders` (AC1) — shells `docker compose … config --quiet` (this is the one contract test allowed to call the docker CLI for validation only; if the CLI is absent, fall back to a pure-Python YAML+env render check).
- `test_all_images_digest_pinned` (AC2) — regex over resolved `image:` values.
- `test_every_service_has_healthcheck` (AC3) — parametrized over services, excludes `autoheal`, rejects trivial tests.
- `test_every_service_has_resource_limits` (AC4).
- `test_non_root_users` (AC5) — asserts uid≠0 or allowlist membership; asserts autoheal is the sole root + has a justification comment marker.
- `test_autoheal_labels_and_socket` (AC6).
- `test_named_volumes_no_data_bind_mounts` (AC7) — any bind mount must end with `:ro`.
- `test_log_caps` (AC8) — services + `daemon.json`.
- `test_network_segmentation` (AC9) — `internal: true` on data/observability (and **not** on `mcp-egress`); data-tier services have no `ports` and only the `data` network; caddy is the sole publisher; `mcp-gateway` is the sole member of `mcp-egress` and is also on `data` + `backend`.
- `test_env_schema_in_sync` (AC10) — assert (a) every `${VAR}` ref in the compose file ∈ `.env.production.example` keys (helper `extract_env_refs(path)`), and (b) every ✅-required example key is delivered to some service via `${VAR}` or an `env_file`/`environment` passthrough; assert required keys carry no inline default. *Subset*, not equality (see §4.2).
- `test_remove_orphans_everywhere` (AC12) — grep the `Makefile` `prod-*` targets, `install.sh`, `restore.sh`, and `deploy/README.md` for `compose .* up`/`down` and assert `--remove-orphans` is present on each (the dev `Makefile` targets and the `docs/self-hosting/` guides are out of scope here — F13/F15 check those).

**Contract suite — `deploy/tests/test_caddyfile.py`** (AC11): `caddy validate` via the pinned caddy image (`docker run --rm`) or skip-if-absent; static assert no `request_body` directive under the webhook matcher.

**Contract suite — `deploy/tests/test_dockerfiles.py`** (AC20): for each `apps/*/Dockerfile` assert a non-root `USER`, a `HEALTHCHECK`, a digest-pinned `FROM`, and a sibling `.dockerignore`.

**Contract suite — `deploy/tests/test_artifact_handshake.py`** (AC19): assert every `deploy/scripts/*.sh` exits 0 on `--help` and emits its documented flags; assert `deploy/README.md` exists, contains the `prod-up`/`prod-migrate`/`create-admin` sequence with `--remove-orphans`, and links to `docs/self-hosting/`; run `pin-digests.sh --check` and assert exit 0 (every `*_IMAGE` is `@sha256`-pinned). Authoring/verification of the nine prose guides is `v1/F15-selfhosting-docs`' job, not asserted here.

**Unit — `deploy/tests/test_scripts.py`** (subprocess; or `deploy/tests/scripts.bats` if bats is available) (AC18):
- `test_preflight_rejects_placeholder_secret`, `test_preflight_rejects_short_auth_secret`, `test_preflight_rejects_invalid_vault_keys` (missing `FORGE_VAULT_KEYS`, or `FORGE_VAULT_ACTIVE_KEY_VERSION` pointing at an absent version), `test_preflight_rejects_missing_required_key`, `test_preflight_rejects_busy_port_80` (bind a socket to 80 in the test or stub `ss`), `test_preflight_passes_on_good_env` (uses a fully-populated temp env incl. a valid KEK map + stubbed `docker`/`ss` on PATH).
- `test_backup_no_partial_archive_on_failure` — point `backup.sh` at a `docker` stub that fails on `pg_dump`; assert no archive left behind, non-zero exit.
- `test_backup_manifest_shape` — with all stubs succeeding, assert `backup-manifest.json` contains `{version, created_at, image_digests, pg_row_counts, minio_object_count, members[]}` and that `vault-keys.env` is captured (chmod 600) but only hashed in the manifest.
- `test_restore_fails_closed_on_missing_kek` — restore against an env file lacking the active KEK version exits non-zero before mutating data.

**E2E — `deploy/tests/e2e/test_stack_lifecycle.py`** (`@pytest.mark.docker`, `@pytest.mark.slow`; module-scoped fixture brings the real stack up and tears it down with `--remove-orphans`):
- `test_stack_becomes_healthy` (AC13) — `healthcheck.sh` green; `curl -fk https://localhost/healthz` → 200.
- `test_api_not_host_published` (AC13) — host `curl http://localhost:8000` connection-refused.
- `test_migrate_and_create_admin` (AC14).
- `test_data_tier_isolation` (AC15) — host connect to 5432/6379/9000 refused; `docker compose exec web sh -c 'nc -z db 5432'` fails.
- `test_autoheal_restarts_api` (AC16) — record `.State.StartedAt`/restart count, force api unhealthy, poll for restart.
- `test_backup_restore_roundtrip` (AC17) — seed via `forge-cli` (or SQL) a workspace + a stored BYOK secret (encrypted under the active KEK) + put a MinIO object, `backup.sh`, `docker compose down -v`, recreate, `restore.sh`, assert the workspace row + MinIO object are reproduced AND the BYOK secret decrypts to its original plaintext; a second sub-case restores with the active KEK version removed from the env file and asserts a fail-closed non-zero exit.

**Key fixtures**
- `compose_doc` — parsed `docker-compose.yml` with anchors resolved.
- `prod_env` / `example_env` — dict loaders for `.env.production.example`.
- `example_env_file` — a tmp `.env` fully populated with throwaway secrets + locally-built `*_IMAGE` digests, for `config` rendering and e2e.
- `stack` (e2e, module scope) — `up -d --remove-orphans` with a unique `-p forge_test_<rnd>` project name; yields a small client (compose-exec helpers, `host_curl`, `container_exec`); finalizer `down -v --remove-orphans`.
- `docker_stub_path` (unit) — temp dir prepended to `PATH` with fake `docker`/`ss`/`mc` for hermetic script tests.

**CI wiring note**: the contract + script-unit tiers run on every PR (no Docker daemon needed beyond the optional `compose config`); the `@pytest.mark.docker` tier runs in a dedicated job with a Docker-enabled runner and is required before tagging a release (it builds the four app images locally so the digests resolve).

---

## 8. Security & policy considerations

- **Network segmentation is the primary control.** `db`/`redis`/`minio` publish no host ports and live only on the `internal: true` `data` network — they are unreachable from the internet and from the VM host; only Caddy is host-exposed (80/443). App services reach data only over the internal network. This directly satisfies the spec's "no anonymous access / network policies" posture and the distr.sh production guidance.
- **Non-root everywhere + least Linux capabilities.** `cap_drop: [ALL]`, `no-new-privileges: true`, non-root `user:` for app/minio containers, and `read_only: true` root filesystems with explicit `tmpfs` for writable scratch **where the image supports it** (app containers do; `db`/`redis`/`minio` keep a writable data path via their named volume, so `read_only` is applied selectively, not contract-enforced for stateful images). Caddy is the single capability exception: `cap_add: [NET_BIND_SERVICE]` so it can bind 80/443 as non-root.
- **The autoheal docker.sock exception is the highest-risk surface.** `willfarrell/autoheal` must read the Docker socket to restart containers, so it runs as root with `/var/run/docker.sock:ro`. Mitigations (written up in F15's `security.md`, enforced by the compose file here): mount **read-only**, restrict autoheal to label-matched containers (`AUTOHEAL_CONTAINER_LABEL=autoheal`), keep it off all app networks (it needs no network), pin its digest, and note this as the one privileged sidecar. (A socket-proxy hardening — `tecnativa/docker-socket-proxy` fronting the socket with `CONTAINERS=1,POST=1` only — is offered as optional; default ships the simpler ro-mount.)
- **Secrets handling.** No secret is baked into an image or committed: passwords/keys come from `.env.production` (chmod 600, gitignored), the GitHub App PEM and any TLS material are file-mounts (`GITHUB_APP_PRIVATE_KEY_PATH`). The BYOK vault's master key (KEK) is **env-resident** (`FORGE_VAULT_KEYS`, per `cross-cutting/F37-auth-secrets-byok`) and never stored in Postgres; `backup.sh` captures it into a chmod-600 `vault-keys.env` member and records only its sha256 in `backup-manifest.json` (never plaintext). `preflight.sh` refuses placeholder/example secret values, short `SECRET_KEY`/`AUTH_SECRET`, and a malformed/absent KEK (fail-closed).
- **Caddy raw-body integrity** for the GitHub webhook (F03 dependency): the webhook route must not buffer-transform/rewrite the body or the HMAC breaks — asserted by `test_caddyfile.py` and a comment in the Caddyfile.
- **Image provenance & supply chain.** Digest pinning (AC2) prevents silent tag mutation; `pin-digests.sh` (owned here) + a `trivy image` / `docker scout` scan step documented in F15's `upgrade.md` cover CVE checking before pins are bumped. Third-party images are pinned to specific published digests, not `latest`.
- **TLS by default.** Caddy auto-provisions Let's Encrypt certs for `${DOMAIN}`; HSTS and security headers are set; no plaintext HTTP service is reachable except Caddy's 80→443 redirect.
- **Non-negotiables this slice carries vs. delegates.** The spec's runtime non-negotiables are enforced by the app slices, not here, but this slice must not *break* them: (a) **MCP read-only default + RFC 8707 token binding + per-call audit** are enforced inside the gateway by `v1/F09-mcp-gateway-v1`; this slice only provides the isolated `mcp-egress` segment for the gateway's outbound calls and the `data` segment it writes audit rows over. (b) The **immutable, queryable audit log** (`cross-cutting/F39-audit-log`) lives in Postgres and is preserved verbatim by `backup.sh`/`restore.sh` (AC17). (c) **BYOK** is realized by carrying `MODEL_PROVIDER_KEY` + the per-workspace encrypted vault (KEK in env). (d) **Spec-gated implementation / human-approval-before-merge** are workflow concerns (F07/F08) with no deployment surface here. This slice authors no agent-tool calls, so the runtime `PolicyEvaluator` (`v1/F04-repo-policy`) is not invoked; its "policy" is the **deployment hardening contract** mechanically enforced by §7.

---

## 9. Effort estimate & risk (S/M/L + key risks)

**Effort: L.** Although it writes no product code, the surface is wide: the production compose file + anchors + segmented networks, four hardened multi-stage Dockerfiles, Caddy + Nginx configs, daemon.json, six operational scripts (`install`/`preflight`/`healthcheck`/`backup`/`restore`/`pin-digests`), the env contract, the `deploy/README.md` runbook, the YAML-parsing contract suite, and a real Docker-gated e2e lifecycle test (the most expensive piece). The nine prose guides are F15's separate effort.

Key risks:
- **Healthcheck contract drift.** The owning slices (`v1/F00-foundation-substrate` for api `/healthz`+`/readyz` and web `/healthz`; `v1/F09-mcp-gateway-v1` for the gateway `/healthz`) may not yet expose those endpoints exactly as specced; mitigated by freezing the contract in §3.2, wiring compose against it, and failing the e2e early with a clear message (and providing a `wait-for` fallback in dev).
- **`deploy.resources.limits` semantics in non-Swarm Compose.** Compose v2 honors `deploy.resources.limits` on `up` but historically ignored `reservations`; mitigated by documenting v2 behavior, treating limits as the enforced value, and asserting only limits in AC4.
- **autoheal + docker.sock blast radius** — see §8; mitigated by ro-mount, label scoping, no-network, optional socket-proxy.
- **E2E flakiness / CI Docker availability.** Bringing eight services + TLS up in CI is slow and can flake on cert provisioning; mitigated by an internal/test TLS mode (Caddy `tls internal` for localhost in the e2e env), generous `start_period`, polling `healthcheck.sh`, and marking the tier `slow`/required-only-at-release.
- **Digest pin staleness / rot.** Pinned base images accrue CVEs; mitigated by `pin-digests.sh` + a scheduled scan documented in `upgrade.md` (Watchtower-style auto-update is explicitly **not** enabled by default — the spec wants deliberate, backed-up upgrades).
- **Resource sizing vs the 4 vCPU / 8 GB target.** Per-service limits must sum within the minimum VM; mitigated by conservative presets and a documented sizing table in `docker-compose.md`.

---

## 10. Key files / paths (exact)

**Owned by this slice:**
```
deploy/docker-compose.yml                         # production base (8 services + autoheal; V2 behind profiles)
deploy/docker-compose.override.yml.example        # template for gitignored local override
deploy/.env.production.example                     # production env contract (§4.2)
deploy/docker/daemon.json                         # host log caps + live-restore
deploy/caddy/Caddyfile                            # auto-HTTPS reverse proxy (raw-body webhook route)
deploy/nginx/forge.conf                           # Nginx alternative
deploy/README.md                                  # in-repo production runbook + link to docs/self-hosting/ (F15)
deploy/scripts/install.sh
deploy/scripts/preflight.sh
deploy/scripts/healthcheck.sh
deploy/scripts/backup.sh
deploy/scripts/restore.sh
deploy/scripts/pin-digests.sh

apps/api/Dockerfile                               # multi-stage, non-root, HEALTHCHECK, digest-pinned base
apps/api/.dockerignore
apps/worker/Dockerfile
apps/worker/.dockerignore
apps/mcp-gateway/Dockerfile
apps/mcp-gateway/.dockerignore
apps/web/Dockerfile                               # Next.js standalone, non-root
apps/web/.dockerignore

Makefile                                          # PRODUCTION targets only: prod-up/prod-down/prod-logs/prod-ps/
                                                  #   prod-pull/prod-migrate/backup/restore/create-admin/verify
                                                  #   (--remove-orphans); dev targets are F13's

deploy/tests/test_compose_contract.py             # AC1-10,12
deploy/tests/test_caddyfile.py                    # AC11
deploy/tests/test_dockerfiles.py                  # AC20
deploy/tests/test_artifact_handshake.py           # AC19 (script --help, deploy/README.md, pin-digests --check)
deploy/tests/test_scripts.py                      # AC18 (or deploy/tests/scripts.bats)
deploy/tests/e2e/test_stack_lifecycle.py          # AC13-17 (@pytest.mark.docker)
deploy/tests/conftest.py                          # compose_doc / env loaders / stack fixture / docker_stub_path
deploy/tests/fixtures/                            # populated .env, fake docker/ss/mc stubs
```

**Referenced but owned by other slices (NOT authored here):**
```
deploy/docker-compose.dev.yml                     # v1/F13-local-quickstart (dev compose)
deploy/.env.example                               # v1/F13-local-quickstart (dev env)
deploy/scripts/verify-restore.sh                  # v1/F15-selfhosting-docs (restore drill probe)
deploy/scripts/verify-upgrade.sh                  # v1/F15-selfhosting-docs (upgrade drill probe)
docs/self-hosting/*.md (the nine guides + index)  # v1/F15-selfhosting-docs (documents these deploy/ artifacts)
deploy/helm/                                       # v2/F24-kubernetes-helm
apps/api  /healthz + /readyz handlers             # v1/F00-foundation-substrate
apps/mcp-gateway  /healthz handler                # v1/F09-mcp-gateway-v1
apps/web/app/healthz/route.ts                     # v1/F00-foundation-substrate (web foundation)
forge-cli entrypoint (db/users/secrets groups)    # v1/F00-foundation-substrate + cross-cutting/F37-auth-secrets-byok
```

---

## 11. Research references (relevant links from the spec/research report)

- **Production Docker Compose Requirements checklist** (digest pinning, autoheal, named volumes, resource limits, healthchecks, network segmentation, non-root, log caps, `--remove-orphans`): FORGE_SPEC.md §"Production Docker Compose Requirements" + §"docker-compose.yml Service List" + §"Docker Compose Production".
- **Docker Compose in production 2026** (the source of the requirements list — autoheal sidecar, pinned digests, resource limits, named volumes, network segmentation): https://distr.sh/blog/running-docker-in-production/ ; research report §"Self-Hosting and Deployment" [cite:120][cite:121].
- **Docker Compose production best practices** (non-root, healthchecks, log rotation, env layering): https://nickjanetakis.com/blog/best-practices-around-production-ready-web-apps-with-docker-compose [cite:127].
- **Reference self-hosting compose stacks** to mirror structure/secrets/healthcheck patterns: Langfuse https://langfuse.com/self-hosting/deployment/docker-compose · Trigger.dev https://trigger.dev/docs/self-hosting/docker · Plane.so https://developers.plane.so/self-hosting/methods/docker-compose · comprehensive guide https://github.com/mikeroyal/self-hosting-guide.
- **Caddy** (auto-HTTPS reverse proxy, the spec's default): https://caddyserver.com/ (FORGE_SPEC.md Technology Stack "Reverse proxy → Caddy (auto HTTPS) with Nginx alternative").
- **MinIO** (self-hosted S3 object storage service): https://min.io/ .
- **pgvector** (the `db` image must carry the extension F05 enables): https://github.com/pgvector/pgvector .
- **Three install paths & Local Quickstart** (Local dev / Docker Compose / Kubernetes; quickstart < 10 min; Compose handles single-node, K8s is the escalation path): FORGE_SPEC.md §"Self-Hosting and Deployment" + §"Required Self-Hosting Documentation"; research report §"Self-Hosting and Deployment" [cite:120][cite:121].
- **Kubernetes escalation path** (explicitly V2): https://www.plural.sh/blog/local-kubernetes-guide/ (FORGE_SPEC.md Phase 2 "Kubernetes Helm chart").
- **External canonical refs needed at implementation time** (not in spec): docker-compose `deploy.resources` semantics in Compose v2 https://docs.docker.com/compose/compose-file/deploy/ ; willfarrell/autoheal https://github.com/willfarrell/docker-autoheal ; optional socket-proxy hardening https://github.com/Tecnativa/docker-socket-proxy .

---

## 12. Out of scope / future

- **The nine `docs/self-hosting/` guides + their verification harness** and `deploy/scripts/verify-restore.sh` / `verify-upgrade.sh` — owned by `v1/F15-selfhosting-docs`. This slice authors only `deploy/README.md`; F15 documents and CI-verifies the `deploy/` artifacts owned here.
- **The dev compose + dev Make targets** (`deploy/docker-compose.dev.yml`, `deploy/.env.example`, `make setup/dev/seed/migrate/reset/doctor`) — owned by `v1/F13-local-quickstart`.
- **Kubernetes / Helm chart** (`deploy/helm/`) — V2, owned by `v2/F24-kubernetes-helm`; the `kubernetes.md` preview page is F15's.
- **Temporal + observability stack** (`temporal`, `prometheus`, `grafana`, `loki`) — declared as opt-in Compose `profiles` placeholders but their real configuration, dashboards, and Temporal worker wiring are V2 (`cross-cutting/F38-observability-cost-metrics`, `v2/F25-temporal-integration`).
- **Multi-node / HA / auto-scaling / cross-host restart** — explicitly outside Compose's capabilities (research report [cite:121]); that is the Kubernetes path.
- **Automatic image updates (Watchtower)** — intentionally not enabled; upgrades are deliberate, backed-up, digest-pinned operations (documented in F15's `upgrade.md`).
- **App `/healthz`/`/readyz` endpoint implementations** and the `forge-cli` command bodies — owned by `v1/F00-foundation-substrate` (db group, health probes) and `cross-cutting/F37-auth-secrets-byok` (users/secrets groups); this slice freezes their contract and wires/validates against them.
- **The MCP read-only default, RFC 8707 token binding, and vault implementation** — owned by `v1/F09-mcp-gateway-v1` and `cross-cutting/F37-auth-secrets-byok`; this slice only composes the gateway and segments its network.
- **Per-app source code, migrations, and the `CREATE EXTENSION vector`** — owned by their feature slices; this slice only packages and runs them.
- **Cloud-provider images / managed-DB variants / Terraform** — future infra slices.
- **Secrets via HashiCorp Vault** — V2 (V1 uses the encrypted Postgres vault, whose env-resident KEK this slice backs up).
