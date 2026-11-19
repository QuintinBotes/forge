# Single-user profile

The single-user profile runs a small Swarm Code Factory on one machine for one
person: the API, the worker, the database, and nothing those do not strictly
need. It is the instance a desktop client such as tervin talks to (spec
`specs/006-tervin-bridge`, FR-019). For a shared or internet-facing instance use
the full stack instead ([docker-compose.md](docker-compose.md)).

The profile is [`deploy/docker-compose.single-user.yml`](../../deploy/docker-compose.single-user.yml).
It has its own Compose project name, `scf-single-user`, so it can run beside the
full stack or the development stack without sharing containers, volumes or ports.

## What runs

| Service | Role |
|---|---|
| `db` | Postgres with pgvector |
| `redis` | The Celery broker. The worker cannot run without it, so it stays. The API also uses it to fan realtime (`/ws`) events out |
| `migrate` | One-shot `alembic upgrade head`. The API and the worker start only after it exits successfully |
| `api` | The API, published on `127.0.0.1` only |
| `worker` | The Celery worker |

Every long-running service has a healthcheck. The API's healthcheck probes
`/health/ready`, which returns 503 until the database answers.

## Before the first start: the secrets file

The profile refuses to start without two secrets. Nothing in the Compose file
supplies a default for them. Generate them once into `deploy/.env.single-user`,
which git ignores (every `.env.*` file is ignored):

```bash
umask 077
[ -e deploy/.env.single-user ] || printf 'FORGE_SECRET_KEY=%s\nPOSTGRES_PASSWORD=%s\n' \
  "$(openssl rand -hex 32)" "$(openssl rand -hex 24)" > deploy/.env.single-user
```

The command writes the file only when it does not exist yet, so running it again
never replaces the secrets of an instance already in use. Keep this file. `FORGE_SECRET_KEY` encrypts stored credentials and keys the
API-key hashes: a new key makes the credentials unreadable and every issued key
invalid. `POSTGRES_PASSWORD` is fixed when the database
volume is first created.

Optional variables, read from the same file:

| Variable | Default | Effect |
|---|---|---|
| `SCF_API_PORT` | `8100` | Host port of the API, on `127.0.0.1`. The default avoids port 8000, which the development stack uses |
| `FORGE_PUBLIC_URL` | `http://127.0.0.1:<SCF_API_PORT>` | The address at which a browser reaches the API. The CLI sign-in link and the cookie checks use it. If you put a proxy in front, set it to the proxy's `https://` address plus the path the proxy serves the API under |
| `FORGE_MODEL_PROVIDER`, `FORGE_MODEL_NAME` | unset | `anthropic` or `openai`. Unset, the API refuses agent runs (HTTP 503) |
| `FORGE_MODEL_API_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` | unset | The model key (BYOK) |
| `FORGE_EMBEDDING_PROVIDER`, `FORGE_EMBEDDING_MODEL`, `FORGE_EMBEDDING_BASE_URL`, `FORGE_EMBEDDING_DIMENSION`, `FORGE_EMBEDDING_API_KEY` | unset | The embedder. Unset, knowledge search is keyword (BM25) only |
| `POSTGRES_USER`, `POSTGRES_DB` | `forge` | Database user and name |
| `FORGE_VERSION` | `0.1.0` | Tag of the locally built `api` and `worker` images |

## Start and stop

Start (builds the images on the first run and waits until the API and the worker
are healthy):

```bash
docker compose -f deploy/docker-compose.single-user.yml --env-file deploy/.env.single-user up -d --build --wait api worker
```

Naming `api worker` starts their dependencies too. It keeps `--wait` from
treating the finished `migrate` step as a failure on older Compose releases.

Stop (containers removed, data kept in the named volumes):

```bash
docker compose -p scf-single-user down
```

Add `-v` to delete the database, the broker and the spec volumes as well.

Check the API:

```bash
curl -fsS http://127.0.0.1:8100/health
```

## The first admin and a token

Every route is authenticated, and a fresh install has no account. Create the
first workspace and admin and print a one-time admin API key:

```bash
docker compose -p scf-single-user exec -e FORGE_SEED_ADMIN_KEY=1 api python -m scf_api.scripts.seed
```

The seed creates the workspace `demo` and the admin `admin@forge.local`, and
prints the key once. The `curl` example it prints names the development stack's
address. On this profile use `http://127.0.0.1:8100` (no `/api` prefix):

```bash
curl -fsS -H "Authorization: Bearer $ADMIN_KEY" http://127.0.0.1:8100/auth/me
```

The admin key carries the whole admin role. Give a desktop client a personal
access token instead. It reads as a viewer, can also decide approvals when
`decide` is true, and expires (90 days unless `expires_in_days` says otherwise):

```bash
curl -fsS -X POST http://127.0.0.1:8100/auth/personal-tokens \
  -H "Authorization: Bearer $ADMIN_KEY" -H 'Content-Type: application/json' \
  -d '{"name": "tervin", "surface": "tervin", "decide": true}'
```

The response carries the token once. Then revoke the admin key: list the keys
with `GET /auth/api-keys` and delete the `seed-bootstrap` one with
`DELETE /auth/api-keys/{id}`. Running the seed again replaces that key, which
is how to get back in after losing it.

## What is left out

Compared with the full stack (`deploy/docker-compose.yml`):

| Left out | What that means here |
|---|---|
| Object store (`minio`) | Nothing in the API or the worker reads an object store today: benchmark bundles are stored in Postgres, and sandbox output over the size cap is truncated in the full stack too. No feature is lost, and the API starts and reports healthy without one |
| Reverse proxy (`caddy`) | No TLS and no edge. The API listens on loopback only |
| Web app (`web`) | No browser UI. Use the API, `scf-cli` or a desktop client |
| MCP gateway (`mcp-gateway`) | The standalone gateway service is not started. The API serves its own `/mcp` routes |
| Workflow server (the `temporal` profile) | No durable engine. A repository-changing agent run is refused with `ungoverned_run_refused` (#277). Runs that change no repository, the board, specs, approvals and knowledge work. For repository runs, use the full stack with `--profile temporal` and `WORKFLOW_ENGINE_BACKEND=temporal` |
| Observability (the `observability` profile) | No collector, metrics, logs or traces stack |
| `autoheal` | Compose restarts a container that exits (`restart: unless-stopped`), but nothing restarts one that only turns unhealthy |
| Sandbox proxies (`docker-proxy`, `sandbox-proxy`) | No access to a Docker daemon. See the next section |

The profile fails closed like the production file (FR-023, ADR-0007): it runs
with `FORGE_ENVIRONMENT=production`, sets none of the development-only flags
(`FORGE_DEV_INSECURE`, `FORGE_ALLOW_SCRIPTED_AGENT`, `FORGE_ALLOW_FAKE_ACTIVITIES`,
`FORGE_DEBUG`, `FORGE_SEED_ADMIN_KEY`), and keeps authentication on. The API
docs pages (`/docs`) are off, as in production.

## Sandbox tier

The worker runs with `FORGE_SANDBOX_KIND=worktree`: build and test commands run
as subprocesses of the worker, in a git worktree inside the worker container. A
worktree is not a security boundary. Code from the repository runs with the
worker's privileges, contained only by the worker container itself. The profile
gives the worker no Docker daemon: there is no socket proxy, and the host's
Docker socket is never mounted. A repository policy that asks for a stronger
tier (`container`, `gvisor`, `microvm`) therefore fails the run loudly rather
than downgrading it. For those tiers, use the full stack
([security.md](security.md#container-sandboxing-f19)).
