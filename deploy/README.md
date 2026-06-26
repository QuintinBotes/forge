# Forge deploy

Self-hosting substrate (plan Task 0.6). Detailed guides live in
`docs/self-hosting/` (Task 1.17); this is the operator quick reference.

## Files

| File | Purpose |
|---|---|
| `docker-compose.yml` | Production single-node stack (hardened) |
| `docker-compose.dev.yml` | Self-contained local-dev stack (`make dev`) |
| `caddy/Caddyfile` | Edge reverse proxy + automatic HTTPS |
| `docker/*.Dockerfile` | Build definitions for api / worker / mcp-gateway / web |
| `scripts/backup.sh` | Postgres + MinIO backup |
| `scripts/restore.sh` | Restore from a backup directory |

## Production

```bash
cp .env.example .env            # fill SECRET_KEY, POSTGRES_PASSWORD, DOMAIN, ...
docker compose -f deploy/docker-compose.yml up -d --remove-orphans
```

Hardening applied (see the spec's "Production Docker Compose Requirements"):
pinned images, `willfarrell/autoheal` sidecar, named volumes, CPU+memory limits,
healthchecks, segmented networks (`edge` / `backend` / `data` / `mcp`, with `data`
marked `internal`), non-root app containers, and per-container log rotation.

## Local development

```bash
make dev   # docker compose -f deploy/docker-compose.dev.yml up --remove-orphans
```

Web UI: http://localhost:3000 — API: http://localhost:8000

## Known follow-ups

- **PARKED — image digests:** images are pinned to version tags, not immutable
  `@sha256` digests (digest resolution needs registry network access). Replace
  before a production rollout.
- **PARKED — image builds:** `docker compose build` is verified in Phase 2
  (Task 2.1); it cannot run in the no-network overnight sandbox.
