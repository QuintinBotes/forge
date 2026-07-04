# Docker Compose deployment

Forge ships two Compose files: a hardened single-node production stack and a
lighter local-development stack. This guide explains the production topology,
the hardening applied, and how to operate it. To get running quickly first, see
[quickstart.md](quickstart.md).

## Files

| File | Purpose |
|---|---|
| [../../deploy/docker-compose.yml](../../deploy/docker-compose.yml) | Production single-node stack |
| [../../deploy/docker-compose.dev.yml](../../deploy/docker-compose.dev.yml) | Local development stack (`make dev`) |
| [../../deploy/caddy/Caddyfile](../../deploy/caddy/Caddyfile) | Edge reverse proxy + automatic HTTPS |

## Services

| Service | Image | Role |
|---|---|---|
| `db` | `pgvector/pgvector:pg16` | Postgres with the pgvector extension |
| `redis` | `redis:7.4-alpine` | Queue, cache, and session store |
| `minio` | `minio/minio` | S3-compatible object storage for artifacts |
| `api` | built from `deploy/docker/api.Dockerfile` | FastAPI application |
| `worker` | built from `deploy/docker/worker.Dockerfile` | Celery worker (indexer, syncer, agent runner) |
| `mcp-gateway` | built from `deploy/docker/mcp-gateway.Dockerfile` | MCP client manager |
| `web` | built from `deploy/docker/web.Dockerfile` | Next.js frontend |
| `caddy` | `caddy:2.8-alpine` | TLS termination and reverse proxy |
| `autoheal` | `willfarrell/autoheal` | Restarts containers that fail their healthcheck |

## Hardening applied

The production file follows the spec's "Production Docker Compose Requirements":

- **Pinned images** — every image uses an explicit version tag.
- **Healthchecks** on every long-running service.
- **Autoheal sidecar** restarts any container labelled `autoheal=true` that goes
  unhealthy.
- **Resource limits** — CPU and memory limits on every container.
- **Named volumes** for all stateful data (`db-data`, `redis-data`,
  `minio-data`, `caddy-data`, `caddy-config`) — never bind mounts.
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

## Known follow-ups

- **Image digests:** images are pinned to version tags, not immutable
  `@sha256` digests. Resolving digests needs registry network access; pin by
  digest before a production rollout.
- **Building images:** `docker compose -f deploy/docker-compose.yml build`
  builds the four application images from the `deploy/docker/` Dockerfiles. Run
  it on a host with network access to fetch base images.
