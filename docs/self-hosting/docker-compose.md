# Docker Compose deployment

Swarm Code Factory ships three Compose files: a hardened single-node production stack, a
lighter local-development stack, and a single-user profile that runs only the API, the
worker and their data stores ([single-user.md](single-user.md)). This guide explains the
production topology, the hardening applied, and how to operate it. To get running quickly
first, see [quickstart.md](quickstart.md).

## Files

| File | Purpose |
|---|---|
| [../../deploy/docker-compose.yml](../../deploy/docker-compose.yml) | Production single-node stack |
| [../../deploy/docker-compose.dev.yml](../../deploy/docker-compose.dev.yml) | Local development stack (`make dev`) |
| [../../deploy/docker-compose.single-user.yml](../../deploy/docker-compose.single-user.yml) | Single-user profile: API, worker, Postgres and Redis only. See [single-user.md](single-user.md) |
| [../../deploy/caddy/Caddyfile](../../deploy/caddy/Caddyfile) | Edge reverse proxy + automatic HTTPS |

## Services

| Service | Image | Role |
|---|---|---|
| `db` | `pgvector/pgvector:pg16` | Postgres with the pgvector extension |
| `redis` | `redis:7.4-alpine` | Queue, cache, and session store |
| `minio` | `cgr.dev/chainguard/minio` | S3-compatible object storage for artifacts. MinIO's own images on Docker Hub and quay.io are no longer anonymously pullable, so this is Chainguard's public MinIO build (the `-dev` variant, which has the `sh`, `wget` and `mc` that the healthcheck and backup script use). It runs as root, as the previous image did, so existing `minio-data` volumes keep working |
| `api` | built from `deploy/docker/api.Dockerfile` | FastAPI application |
| `worker` | built from `deploy/docker/worker.Dockerfile` | Celery worker (indexer, syncer, agent runner) |
| `mcp-gateway` | built from `deploy/docker/mcp-gateway.Dockerfile` | MCP client manager |
| `web` | built from `deploy/docker/web.Dockerfile` | Next.js frontend |
| `caddy` | `caddy:2.8-alpine` | TLS termination and reverse proxy |
| `autoheal` | `willfarrell/autoheal` | Restarts containers that fail their healthcheck |
| `docker-proxy` | `tecnativa/docker-socket-proxy:0.3.0` | Read-only-socket, verb-restricted Docker API proxy — the only component near the raw Docker socket, used by `worker` to create/exec/inspect per-task sandbox containers |
| `sandbox-proxy` | `ubuntu/squid:6.6-24.04_edge` | Allow-listing forward proxy (squid) — the only egress route for sandboxed agent containers on the `network: egress` sandbox mode |

## Hardening applied

The production file follows the spec's "Production Docker Compose Requirements":

- **Pinned images** — every image uses an explicit version tag.
- **Healthchecks** on every long-running service.
- **Autoheal sidecar** restarts any container labelled `autoheal=true` that goes
  unhealthy.
- **Resource limits** — CPU and memory limits on every container.
- **Named volumes** for all stateful data (`db-data`, `redis-data`,
  `minio-data`, `caddy-data`, `caddy-config`) — never bind mounts.
- **Segmented networks** — seven networks: `edge`, `backend`, `data`, `mcp`,
  `sandbox_ctl`, `sandbox_egress`, and `observability`. `data`, `sandbox_ctl`,
  `sandbox_egress`, and `observability` are marked `internal` (no route to the
  public internet): `data` isolates the database from the edge; `sandbox_ctl`
  is `worker`'s private route to `docker-proxy`; `sandbox_egress` is the only
  route out for sandboxed agent containers, bridged by `sandbox-proxy`.
  `observability` is only populated when the `observability` Compose profile
  is enabled (`otel-collector`, `prometheus`, `grafana`, `loki`, `tempo`,
  `alertmanager`); `temporal`, `temporal-ui`, and `temporal-worker` similarly
  only start under the `temporal` profile.

  `minio-data`, `forge-specs`, `forge_repos`, `caddy-data`, `caddy-config`) —
  never bind mounts. `forge-specs` is the spec engine's document store, mounted
  into `api`, `worker` and `mcp-gateway` so all three read the same specs;
  without it each container keeps its own copy on its writable layer and loses
  every spec on redeploy. It is a **separate store from Postgres** and must be
  in your backups — see [backup.md](backup.md).
- **Segmented networks** — `edge`, `backend`, `data`, `mcp`; the `data` network
  is marked `internal`, so the database is unreachable from the edge.
- **Non-root** app containers (`user: "1000:1000"`).
- **Log rotation** — `json-file` driver capped at `max-size: 100m`,
  `max-file: 5` per container.

## Operating the stack

```bash
# Start (detached)
docker compose -f deploy/docker-compose.yml up -d --remove-orphans

# Status and health
docker compose -f deploy/docker-compose.yml ps

# Tail logs for one service
docker compose -f deploy/docker-compose.yml logs -f api

# Stop (containers removed, named volumes preserved)
docker compose -f deploy/docker-compose.yml down

# Validate the file without starting anything
docker compose -f deploy/docker-compose.yml config
```

## Configuration

All services read configuration from environment variables, sourced from `.env`
in the repo root. Compose substitutes them at `up` time. See
[../../.env.example](../../.env.example) for the full list and
[security.md](security.md) for which values must be treated as secrets.

`FORGE_PUBLIC_URL` is required: Compose refuses every command on this file
without it (`up`, `build`, `config`, `exec`). It is the address at which a
browser reaches the API, including the path prefix the edge proxy serves the API
under. The API builds the SSO callback, the SAML and SCIM
addresses and the CLI sign-in link from it. It also uses it for the same-origin
check on cookie writes and to decide whether the session cookie is `Secure`.

- **Behind the bundled Caddy** (or the bundled nginx alternative), the API is
  served under `/api`, so set `FORGE_PUBLIC_URL=https://<DOMAIN>/api`, with the
  same host as `DOMAIN`.
- **Behind another proxy**, set it to the external `https://` origin browsers
  use plus the path prefix under which that proxy forwards to the API. The
  proxy must strip that prefix, as Caddy does. For example,
  `https://scf.example.com/api` when it forwards `/api/*` to `api:8000` as `/*`.

## Known follow-ups

- **Image digests:** images are pinned to version tags, not immutable
  `@sha256` digests. Resolving digests needs registry network access; pin by
  digest before a production rollout.
- **Building images:** `docker compose -f deploy/docker-compose.yml build`
  builds the four application images from the `deploy/docker/` Dockerfiles. Run
  it on a host with network access to fetch base images.
