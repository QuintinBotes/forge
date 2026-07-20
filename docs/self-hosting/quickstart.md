# Forge self-hosting quickstart

Stand up a complete Forge instance on a single machine with Docker Compose. This
is the fastest path from a clone to a running board, knowledge pipeline, and
API. For the production hardening details see
[docker-compose.md](docker-compose.md); for the edge proxy (Caddy by default,
nginx alternative) see [reverse-proxy.md](reverse-proxy.md); for day-2
operations see [backup.md](backup.md), [restore.md](restore.md),
[upgrade.md](upgrade.md), [security.md](security.md), and
[troubleshooting.md](troubleshooting.md).

## Prerequisites

- Docker Engine 24+ and the Docker Compose v2 plugin (`docker compose version`).
- 4 CPU cores and 8 GB RAM available to Docker (the default resource limits in
  the production compose file assume roughly this).
- For local development from source instead of images: Python 3.14 with
  [uv](https://docs.astral.sh/uv/), Node 22 with `pnpm`, and `make`.

## 1. Clone and configure

```bash
git clone https://github.com/QuintinBotes/forge.git
cd forge
cp .env.example .env
```

Edit `.env` and set, at minimum:

- `FORGE_SECRET_KEY` and `AUTH_SECRET` — long random strings (e.g.
  `openssl rand -hex 32`). (`SECRET_KEY` is a deprecated alias that is
  detected and warned about — see `apps/api/forge_api/cli_secrets.py`.)
- `POSTGRES_PASSWORD` and `MINIO_ROOT_PASSWORD` — strong unique secrets.
- `DOMAIN` — the hostname Caddy will serve (use `localhost` for a local trial).
- `MODEL_PROVIDER_KEY` — your BYOK model provider key (Forge is provider
  agnostic; leave blank to run the platform without live model calls).

The full set of variables is documented inline in
[../../.env.example](../../.env.example).

## 2. Bring the stack up

```bash
docker compose -f deploy/docker-compose.yml up -d --remove-orphans
```

This starts Postgres (pgvector), Redis, MinIO, the API, the worker, the MCP
gateway, the web UI, the Caddy edge proxy, and the autoheal sidecar. Watch
services become healthy:

```bash
docker compose -f deploy/docker-compose.yml ps
```

## 3. Run database migrations

Apply the schema once the database is healthy:

```bash
docker compose -f deploy/docker-compose.yml exec api \
  alembic -c packages/db/alembic.ini upgrade head
```

## 4. Verify

```bash
curl -fsS http://localhost:8000/health
```

A `200` response means the API is up. Open the web UI at `http://localhost:3000`
(or `https://$DOMAIN` once Caddy has issued a certificate).

## Local development (from source)

To iterate on the code instead of running published images, use the dev stack
and the `Makefile` targets defined in [../../Makefile](../../Makefile):

```bash
make setup    # uv sync + pnpm install
make migrate  # alembic upgrade head
make dev      # docker compose -f deploy/docker-compose.dev.yml up
```

Then run the test suite and linters:

```bash
make test     # uv run pytest
make lint     # ruff check + format --check
```

## Next steps

- Add a repo policy: copy an example from
  [../../examples/policies/](../../examples/policies) to your repo's
  `.forge/policy.yaml`.
- Connect an MCP source: see [../../examples/mcp-connectors/](../../examples/mcp-connectors).
- Schedule backups before you put real data in: [backup.md](backup.md).
