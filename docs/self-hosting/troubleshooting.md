# Troubleshooting

Common failures when self-hosting Forge and how to fix them. Start with the
triage commands, then jump to the matching section.

## Triage

```bash
# What is running and is it healthy?
docker compose -f deploy/docker-compose.yml ps

# Recent logs across the stack (or name a service)
docker compose -f deploy/docker-compose.yml logs --since 10m
docker compose -f deploy/docker-compose.yml logs -f api

# Validate the compose file itself
docker compose -f deploy/docker-compose.yml config
```

A healthy stack shows every service `running (healthy)`. A service stuck in
`starting` or flapping `restarting` is the first thing to investigate — the
`autoheal` sidecar restarts unhealthy containers, so a crash-looping service
often shows repeated restarts rather than a clean `exited`.

## A service won't become healthy

- **Read its healthcheck.** Each service's check is defined in
  [../../deploy/docker-compose.yml](../../deploy/docker-compose.yml). Run the same
  command inside the container to see the real error:

  ```bash
  docker compose -f deploy/docker-compose.yml exec api \
    curl -f http://localhost:8000/health
  ```

- **`depends_on` ordering:** `api`/`worker` wait for `db` and `redis` to be
  healthy. If `db` never goes healthy, nothing above it starts.

## Database

- **`api` logs `connection refused` / `could not connect`:** `db` is not healthy
  yet, or `FORGE_DATABASE_URL` is wrong. Confirm:

  ```bash
  docker compose -f deploy/docker-compose.yml exec db \
    pg_isready -U forge -d forge
  ```

- **`relation "..." does not exist`:** migrations have not been applied. Run:

  ```bash
  docker compose -f deploy/docker-compose.yml exec api \
    alembic -c packages/db/alembic.ini upgrade head
  ```

- **`type "vector" does not exist`:** the `pgvector` extension is missing. The
  `pgvector/pgvector:pg16` image ships it; confirm with `\dx` in `psql`. If you
  swapped to a vanilla Postgres image, switch back.

- **`password authentication failed`:** `.env` `POSTGRES_PASSWORD` does not match
  the password baked into the existing `db-data` volume. Either restore the
  original password or recreate the volume (destroys data — back up first via
  [backup.md](backup.md)).

## Migrations

- **`alembic` reports multiple heads:** you have divergent migration branches.
  Inspect with `alembic -c packages/db/alembic.ini heads` and merge before
  upgrading. See [upgrade.md](upgrade.md).
- **A migration half-applied and failed:** restore the pre-upgrade backup
  ([restore.md](restore.md)) rather than hand-editing the schema.

## Auth and secrets

- **Everyone is logged out after a restart:** `AUTH_SECRET` changed (or is
  unset), invalidating sessions. Set a stable value in `.env`.
- **Integrations fail after a restore with clean data:** the `.env` `SECRET_KEY`
  does not match the one used at backup time, so the BYOK vault cannot decrypt.
  Restore the matching `.env` (see [restore.md](restore.md)).
- **A viewer gets `403` on a write:** that is RBAC working as intended; grant
  `member` if the write is legitimate (see [security.md](security.md)).

## TLS / Caddy

- **Certificate not issued / browser warning:** Caddy needs `DOMAIN` to resolve
  to this host and ports 80 and 443 reachable from the internet for the ACME
  challenge. For a local trial use `DOMAIN=localhost` and accept the local cert.
  Check `docker compose -f deploy/docker-compose.yml logs caddy`.
- **502 from Caddy:** the upstream (`api` or `web`) is not healthy yet. Confirm
  with `... ps` and retry once it reports healthy.

## MCP gateway

- **A tool call is rejected as read-only:** the connection has
  `allow_write: false` (the default and the example posture). Enable writes only
  deliberately and audited — see [security.md](security.md).
- **Resources from another server appear:** check namespace scoping in the
  connection definition; compare against
  [../../examples/mcp-connectors](../../examples/mcp-connectors).

## Knowledge / search returns nothing

- Confirm the indexer worker ran: `docker compose -f deploy/docker-compose.yml logs worker`.
- Confirm chunks exist:
  `psql -U forge -d forge -c "select count(*) from retrieval_chunks;"`.
- Blank `EMBEDDING_*` config means no live embeddings are produced; set it to
  enable real retrieval. Separately, with no `FORGE_MODEL_PROVIDER` + BYOK key
  (`FORGE_MODEL_API_KEY`, or `ANTHROPIC_API_KEY`/`OPENAI_API_KEY`) the worker runs
  the offline scripted model — set those to enable live agent runs.

## Resource pressure

- **OOM kills / container restarts under load:** the per-service memory limits in
  [../../deploy/docker-compose.yml](../../deploy/docker-compose.yml) assume ~8 GB
  available to Docker. Raise the limits (and the host) if you exceed them.
- **Disk full:** Postgres, MinIO, and rotated logs grow over time. Monitor
  `docker system df` and the `db-data`/`minio-data` volumes; prune old backups.

## Still stuck?

Collect a redacted log bundle and the output of
`docker compose -f deploy/docker-compose.yml ps` and
`... config` before opening an issue. Confirm secrets are not present in the
logs you share — Forge redacts known secret fields, but double-check.

## Related

- [quickstart.md](quickstart.md) — the happy path, for comparison.
- [docker-compose.md](docker-compose.md) — service topology and hardening.
- [backup.md](backup.md) / [restore.md](restore.md) — recovery procedures.
