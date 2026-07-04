# Forge deploy

Self-hosting substrate (plan Task 0.6; hardened by HARD-07). Detailed guides
live in `docs/self-hosting/` (Task 1.17); this is the operator quick reference.

## Files

| File | Purpose |
|---|---|
| `docker-compose.yml` | Production single-node stack (hardened, digest-pinned) |
| `docker-compose.dev.yml` | Self-contained local-dev stack (`make dev`) |
| `caddy/Caddyfile` | Edge reverse proxy + automatic HTTPS |
| `docker/*.Dockerfile` | Build definitions for api / worker / mcp-gateway / web |
| `build-manifest.json` | Digest + SBOM record of every image a release ships |
| `sbom/<image>.cdx.json` | Per-image CycloneDX SBOM (feeds the HARD-09 evidence pack) |
| `scripts/backup.sh` | Postgres + MinIO backup |
| `scripts/restore.sh` | Restore from a backup directory |
| `scripts/pin-digests.sh` | Resolve + rewrite `@sha256` pins; `--check` = offline lint |
| `scripts/sbom.sh` | Generate CycloneDX SBOMs for the built images (syft) |
| `scripts/smoke.sh` | up -> healthy -> `/health` -> down -v smoke of the prod stack |

## Production

```bash
cp .env.example .env            # fill SECRET_KEY, POSTGRES_PASSWORD, DOMAIN, ...
make build-images               # docker compose -f deploy/docker-compose.yml build
docker compose -f deploy/docker-compose.yml up -d --remove-orphans
make smoke                      # optional: bring-up smoke under project forge-prod-smoke
```

Hardening applied (see the spec's "Production Docker Compose Requirements"):
every pulled image and Dockerfile base pinned `name:tag@sha256:<digest>`
(immutable — a repointed upstream tag is rejected at pull), `willfarrell/autoheal`
sidecar, named volumes, CPU+memory limits, healthchecks (stdlib probes: the slim
runtime images ship no curl/wget, so api/mcp-gateway probe via `python -c
"urllib.request..."` and web via `node -e "fetch(...)"`), segmented networks
(`edge` / `backend` / `data` / `mcp`, with `data` marked `internal`), non-root app
containers, and per-container log rotation. The web image ships the Next.js
`output: "standalone"` server only (no pnpm workspace at runtime).

### Digest pinning + build evidence

`deploy/build-manifest.json` records the resolved digest of every pulled/base
image and the locally-built image IDs + SBOM paths of the 4 first-party images
(`forge/api|worker|mcp-gateway|web`). To roll digests forward deliberately:

```bash
make pin-digests    # docker buildx imagetools inspect each tag; rewrite refs + manifest
make compose-build  # rebuild against the new pins
make sbom           # regenerate deploy/sbom/*.cdx.json
make smoke          # verify the stack still comes up healthy
```

`deploy/scripts/pin-digests.sh --check` is the offline lint (no daemon, no
network) that CI + the hermetic pytest suite run to reject unpinned references.

## Local development

```bash
make dev   # docker compose -f deploy/docker-compose.dev.yml up --remove-orphans
```

Web UI: http://localhost:3000 — API: http://localhost:8000
