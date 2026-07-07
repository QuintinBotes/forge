# F13 — Local Quickstart (<10 min)

> Phase: v1 · Spec module(s): Self-Hosting and Deployment → "Local Quickstart" (`make setup` / `make dev`, seed demo workspace), Monorepo Structure (`deploy/`, `Makefile`), Required Self-Hosting Documentation (`docs/self-hosting/quickstart.md`) · Status target: On a clean checkout, a contributor or evaluator who has the documented prerequisites installed and the base container images pre-pulled can run `make setup && make dev`, reach a working Web UI at `http://localhost:3000` and API at `http://localhost:8000` backed by a **seeded demo workspace**, in **under 10 minutes of wall-clock time**, with **no model-provider key required**. "Done" = the quickstart smoke test (`scripts/quickstart_smoke.sh`) passes in CI within the time budget, `make setup`/`make seed` are idempotent, and `docs/self-hosting/quickstart.md` matches the actual `make` targets.

---

## 1. Intent — what & why

The spec makes self-hosting a first-class, non-negotiable principle (Core Design Principle #9: "Self-hosting is first-class. `docker compose up` delivers a working platform") and lists "Local quickstart (under 10 minutes)" as an explicit Phase 1 deliverable. This slice owns that **onboarding experience**: the single command path that takes a fresh `git clone` to a running, explorable platform with realistic demo data.

This is the first thing every contributor, evaluator, and prospective adopter touches. If it is slow, flaky, or requires a paid model key just to look around, adoption dies at the front door. The slice therefore optimizes for three properties:

1. **Speed** — measurable, CI-enforced sub-10-minute path to a usable UI.
2. **Zero-secret exploration** — the demo workspace seeds **static** data only (no LLM/embedding calls), so the platform is fully browsable before the user supplies any BYOK key. Agent *runs* require a key; *exploring* does not.
3. **Idempotence and determinism** — `make setup`, `make seed`, and `make reset` can be re-run safely and always converge to the same demo state, which is what makes the smoke test (and the golden eval harness, `v1/F12-eval-harness`) stable.

This slice is **mechanism + content-orchestration + docs**, not new product surface. It wires together infrastructure (Postgres/Redis/MinIO via `deploy/docker-compose.dev.yml`), the existing migration entrypoint, and a **pluggable demo-seed orchestrator** that composes per-domain seed contributions (board from `v1/F01-project-board`, spec from `v1/F02-spec-engine`, policy from `v1/F04-repo-policy`, a non-indexed knowledge-source record from `v1/F05-hybrid-knowledge-retrieval`). Skill profiles are **not** seeded as rows — the seven built-ins are code-resolved at runtime by `v1/F11-skill-profiles`' registry (per that slice: "Builtins are not stored in the DB"), and the demo tasks merely reference them by name. The slice defines the `DemoSeeder` contract those slices implement.

---

## 2. User-facing behavior / journeys

The canonical journey (mirrors the spec's "Local Quickstart" block verbatim):

```bash
git clone https://github.com/QuintinBotes/forge
cd forge
make setup    # preflight, install deps, bring up infra, migrate, seed demo workspace
make dev      # start all services
# Web UI: http://localhost:3000  |  API: http://localhost:8000
```

1. **Preflight / doctor.** `make setup` first runs `make doctor` (a read-only check). If Docker, Docker Compose v2 (≥ 2.17, for `up --wait`), `uv`, Node ≥ 22, or `pnpm` is missing or too old, or a required port is occupied, or the Docker daemon is not running, it prints a specific, copy-pasteable remediation and exits non-zero **before** doing any work. Example: `ERROR: port 5432 is in use (PID 9123 postgres) — FIX: stop the conflicting service or set POSTGRES_PORT in .env and re-run`.
2. **Env bootstrap.** If `.env` is absent, setup copies `.env.example → .env` and replaces the placeholder `SECRET_KEY` and `AUTH_SECRET` with freshly generated random values. If `.env` already exists, it is **never** overwritten. Local dev DB/MinIO credentials are deterministic dev-only defaults (so `DATABASE_URL` stays self-consistent without manual editing).
3. **Dependency install.** `uv sync` (Python workspace) and `pnpm install` (Node workspace) run. On a warm cache this is seconds; cold, a couple of minutes.
4. **Infra up + wait.** Setup brings up only the stateful infra services detached and waits for health: `docker compose -f deploy/docker-compose.dev.yml up -d --wait db redis minio`, then runs the one-shot `minio-setup` to create the artifacts bucket. No app images are built at this step (keeps setup fast).
5. **Migrate.** `make migrate` applies Alembic migrations against the now-healthy Postgres.
6. **Seed.** `make seed` runs the demo-seed orchestrator. It is idempotent: a second run mutates nothing.
7. **Summary.** Setup prints a concise success panel: Web URL, API URL, MinIO console URL, the **local-only** demo admin email + password (Better Auth email/password sign-in for the Web UI — see §5 on the `cross-cutting/F37-auth-secrets-byok` credentials provider) + the demo platform API key (inbound machine token for CLI/API/smoke), seeded entity counts, and a one-line note: "Agent runs require a model provider key — set `MODEL_PROVIDER_KEY` in `.env` to enable them." The same data is written to `.forge/demo-manifest.json`.
8. **Start services.** `make dev` brings up every service (db, redis, minio, api, worker, mcp-gateway, web) in the foreground with hot reload. First run builds the app images; subsequent runs are near-instant.
9. **Explore.** The user opens `http://localhost:3000`, signs in with the printed demo email/password (Better Auth credentials sign-in, which needs no OAuth provider — see §5), and lands on the **DEMO** project board pre-populated with epics, tasks across multiple statuses (each tagged with a built-in skill profile), an approved spec, and an example repo policy — enough to understand the product without configuring anything.
10. **Reset (optional).** `make reset` tears down volumes and re-runs `setup`, returning to a pristine deterministic demo state. Useful for demos, screenshots, and debugging.

Non-goals for the user journey: production hardening, TLS, external GitHub App wiring, and real knowledge indexing — those belong to the production self-hosting slice (see §5/§12).

---

## 3. Vertical slice

### 3.1 Data model (tables/columns/migrations touched)

This slice introduces **no new tables and no migrations**. It is a *consumer* of the schema created by the **foundation substrate** (baseline migration `0001_foundations`: `workspaces`, `users`, `teams`, shared `Base`/`TimestampMixin`), `cross-cutting/F37-auth-secrets-byok` (`platform_api_key` + the credentials/hasher machinery), and the domain slices (board entities from `v1/F01-project-board`, `spec_documents` from `v1/F02-spec-engine`, `policy_profiles` from `v1/F04-repo-policy`, `knowledge_sources` from `v1/F05-hybrid-knowledge-retrieval`). Skill profiles are **not** a seeded table — F11's seven built-ins are code-resolved (see §1).

What it *writes* (rows, via the seed orchestrator):

| Entity (owning slice) | Demo rows seeded | Idempotency key |
|---|---|---|
| `workspaces` (foundation substrate) | 1 — slug `demo`, `is_demo=true` | slug `demo` |
| `users` (foundation substrate) | 1 — admin, email from `FORGE_DEMO_ADMIN_EMAIL` (default `admin@demo.forge.local`), role `admin` | email |
| `platform_api_key` (`cross-cutting/F37-auth-secrets-byok`) | 1 — label `demo-cli`, role `admin`, scoped to the demo workspace (inbound machine token; **hashed** at rest, plaintext shown once) | (workspace, label) |
| `projects` (`v1/F01-project-board`) | 1 — key `DEMO`, name "Forge Demo" + its 6 default statuses | (workspace, key) |
| `epics` (`v1/F01-project-board`) | ≥ 1 | (project, number) |
| `tasks` (`v1/F01-project-board`) | ≥ 6 across ≥ 3 status categories; some with `repo_targets`, `skill_profile` (a built-in name), `spec_id` | (project, number) |
| `skill_profiles` (`v1/F11-skill-profiles`) | **0 rows** — the seven built-ins (incl. the spec's `backend-tdd`/`frontend-ui`/`incident-response`/`spec-analyst`) are code-resolved by F11's registry, never seeded; F11 *may* contribute an optional seeder adding **1 custom** profile to showcase the override UX | (workspace, name) |
| `policy_profiles` (`v1/F04-repo-policy`) | 1 — example Python-API policy mirroring the spec `policy.yaml` | (workspace, name) |
| `spec_documents` (`v1/F02-spec-engine`) | 1 — approved `SPEC-DEMO-1` linked to the demo epic | (workspace, spec_id) |
| `knowledge_sources` (`v1/F05-hybrid-knowledge-retrieval`) | 1 — a *record only* pointing at the local repo, status `registered` (un-indexed); indexing needs BYOK embeddings | (workspace, name) |

> Determinism requirement: every demo row's primary key is derived via `uuid5(DEMO_NAMESPACE, "<entity>:<natural-key>")` (fixed namespace UUID constant), and any randomized field (timestamps offsets, ordering) uses a fixed-seed `random.Random(1337)`. This makes two fresh seeds byte-identical in the manifest, which the smoke test relies on. `is_demo=true` on the workspace lets `make reset`/`--reset` wipe demo data without touching anything else.

### 3.2 Backend (FastAPI routes + services/packages)

No new HTTP routes. The slice adds a **CLI entrypoint** and a **seed orchestrator package** inside `apps/api` (the app already owns the DB session factory and config), plus the `DemoSeeder` contract in `packages/contracts`.

- **Contract** — `packages/contracts/forge_contracts/seed.py`: `SeedContext`, `SeedResult`, and the `DemoSeeder` Protocol (see §4). This is the only thing other slices import to contribute demo data.
- **Orchestrator** — `apps/api/forge_api/seed/orchestrator.py`: `run_seed(session, *, reset: bool, admin_email: str, admin_password: str) -> SeedManifest`. It (a) ensures the demo workspace/admin/api-key via the core seeder, (b) iterates the registered `DemoSeeder` list in `order`, (c) aggregates `SeedResult`s into a `SeedManifest`, (d) commits once. If `reset=True`, it first deletes the demo workspace row, relying on the `workspaces` FK `ON DELETE CASCADE` to remove children; before issuing the delete it sets the session-local `forge.allow_audit_cascade` GUC (from `cross-cutting/F39-audit-log`) so the `audit_log` immutability trigger permits the cascade for this one tenant. The delete is keyed on `is_demo=true` / slug `demo`, so a real workspace can never be wiped by this path.
- **Core seeder** — `apps/api/forge_api/seed/core.py`: creates the demo workspace + admin user (credentials hashed via `cross-cutting/F37-auth-secrets-byok`'s hasher) + a scoped demo `platform_api_key` (issued via F37's key issuer; plaintext returned once). When `cross-cutting/F39-audit-log`'s `AuditSink` is wired, it emits one bootstrap `AuditEvent` (`actor="system:demo-seed"`) per privileged creation (workspace/admin/key); a no-op sink is injected in unit tests so the seeder compiles against the Protocol before F39 lands. Always runs first (`order=0`).
- **Registry** — `apps/api/forge_api/seed/registry.py`: an **explicit, ordered list** of seeders (no entry-point magic, for determinism and auditability). Each domain package exposes `get_demo_seeder() -> DemoSeeder | None`; the registry calls these and skips any that return `None` (slice not present, or nothing to seed). Order: core(0) → skill(10) → policy(20) → spec(30) → board(40) → knowledge(50). `skill(10)` is optional — F11's built-ins need no seeding, so its hook returns `None` or seeds a single custom demo profile; `board(40)` runs after `spec(30)` because demo tasks reference the seeded `spec_id`.
- **CLI** — `apps/api/forge_api/scripts/seed.py` (run as `python -m forge_api.scripts.seed`, exactly what the existing `make seed` target invokes). Args: `--reset`, `--admin-email`, `--admin-password`, `--quiet`. Opens an async session, calls `run_seed`, prints the summary panel, and writes `.forge/demo-manifest.json`.
- **Doctor (Python helper, optional)** — preflight logic that needs structured checks (port scan, version parse) lives in `apps/api/forge_api/scripts/doctor.py` and is callable both from `make doctor` and from tests; the thin `scripts/preflight.sh` shells out to it for the parts that must run before deps are installed (it falls back to pure-shell checks for tool presence so it works even if `uv sync` has not run yet).

The seed orchestrator MUST NOT call any model provider, embedding endpoint, or reranker — enforced by a test that runs the full seed with all `*_PROVIDER_KEY` / `RERANKER_URL` env vars unset and asserts success (AC5).

### 3.3 Worker / agent runtime (Celery tasks, LangGraph, if any)

N/A — this slice defines **no** Celery tasks and **no** LangGraph graphs. It does include the `worker` service in `deploy/docker-compose.dev.yml` so that background tasks contributed by *other* slices run during local dev, and the smoke test confirms the worker container reaches a healthy state, but F13 itself ships no task code or agent runtime logic.

### 3.4 Frontend / UI (Next.js routes/components, if any)

N/A for new routes/components — F13 ships **no** new React routes or components; the demo experience is the *existing* F01 board UI rendering the seeded data. The slice's only frontend touchpoints are operational and live in `deploy/docker-compose.dev.yml`:

- The `web` service runs `next dev` with `NEXT_PUBLIC_API_URL=http://localhost:8000` so the seeded data is reachable.
- The quickstart success criterion is that `http://localhost:3000` returns HTTP 200 and, after login with the demo admin, the DEMO project board renders the seeded tasks.

(Optional, deferred to the F01 owner, not required for this slice's "done": a small "Demo workspace" banner when `workspace.is_demo` — noted in §12, not built here.)

### 3.5 Infra / deploy (compose, helm, caddy, if any)

The core infra deliverable is **`deploy/docker-compose.dev.yml`** (the file the existing `make dev` already points at). Local-dev posture differs deliberately from production (that is `v1/F14-docker-compose-selfhost`): bind to `127.0.0.1` only, use deterministic dev credentials, enable hot-reload bind mounts, and keep it single-file.

Services (all images **pinned by `@sha256` digest**, all with healthchecks, all on a single `forge_dev` network, all ports bound to `127.0.0.1`):

| Service | Image (pinned by digest) | Purpose | Healthcheck | Port (127.0.0.1) | Volume |
|---|---|---|---|---|---|
| `db` | `pgvector/pgvector:pg16` | Postgres + pgvector | `pg_isready -U $POSTGRES_USER -d $POSTGRES_DB` | 5432 | `forge_pgdata` |
| `redis` | `redis:7-alpine` | queue/cache/sessions | `redis-cli ping` | 6379 | `forge_redisdata` |
| `minio` | `minio/minio` | object storage | `curl -f http://localhost:9000/minio/health/ready` | 9000, 9001 | `forge_miniodata` |
| `minio-setup` | `minio/mc` | one-shot bucket create (`$MINIO_BUCKET`); `restart: "no"` | — (exits 0) | — | — |
| `api` | built from `apps/api` (foundation-substrate Dockerfile) | FastAPI + `--reload` | `curl -f http://localhost:8000/healthz` | 8000 | source bind-mount |
| `worker` | built from `apps/worker` | Celery worker (+beat) | `celery -A forge_worker inspect ping` | — | source bind-mount |
| `mcp-gateway` | built from `apps/mcp-gateway` | MCP client manager | `curl -f http://localhost:8001/healthz` | 8001 | source bind-mount |
| `web` | built from `apps/web` | Next.js `next dev` | `curl -f http://localhost:3000` | 3000 | source bind-mount |
| `reranker` | (profile `rerank`, off by default) | Jina reranker v2 | `curl -f $RERANKER_URL` | 8080 | — |

Compose details: `depends_on` with `condition: service_healthy` so `api`/`worker`/`web`/`mcp-gateway` only start once `db`/`redis`/`minio` are healthy; named volumes (never bind mounts) for stateful data; `--remove-orphans` on every `up` (matches the existing `make dev`). The `rerank` profile is opt-in because knowledge indexing is out of scope for the zero-key quickstart.

**Service ownership / skeleton boundary.** The `api`/`worker`/`mcp-gateway`/`web` images are built from app **skeletons** owned by the foundation substrate; the dev compose only packages and wires them. Full MCP behavior is `v1/F09-mcp-gateway-v1` (this slice needs only the `mcp-gateway` skeleton's `/healthz` for AC8), and the `reranker` image is owned by `v1/F05-hybrid-knowledge-retrieval` (off by default here).

**In-container vs host URL resolution (load-bearing).** `make migrate` and `make seed` run from the **host** via `uv run …`, so they use the `.env` `DATABASE_URL`/`REDIS_URL`/`MINIO_ENDPOINT` pointing at the published `127.0.0.1` ports. In-container services (`api`/`worker`) cannot reach `127.0.0.1`, so the compose file **overrides** those three URLs in the service `environment:` to use Compose service DNS (`db:5432`, `redis:6379`, `minio:9000`). The compose-lint test (AC14/§7) asserts these in-container overrides are present so host-side and container-side wiring never silently diverge.

**Makefile changes** (the file already exists; F13 edits target bodies). New/edited targets:

- `setup` — becomes the full first-run path: `doctor` → env bootstrap → `install` → infra `up -d --wait db redis minio` → `minio-setup` → `migrate` → `seed` → print summary. (Today it only runs `install` and prints a manual to-do.)
- `dev` — unchanged invocation (`docker compose -f deploy/docker-compose.dev.yml up --remove-orphans`) but now resolves because the compose file exists.
- `seed` — unchanged invocation (`python -m forge_api.scripts.seed`).
- `doctor` *(new)* — runs preflight checks read-only; non-zero on failure.
- `reset` *(new)* — `docker compose -f deploy/docker-compose.dev.yml down -v --remove-orphans` then `make setup`.
- `down` *(new)* — `docker compose -f deploy/docker-compose.dev.yml down --remove-orphans` (stop, keep volumes).

**Scripts** (`scripts/`): `preflight.sh` (POSIX `sh`, no GNU-only flags, runs before deps installed), `quickstart_smoke.sh` (the CI/end-to-end timed verification). `wait-for-health` is delegated to compose `up --wait`; no bespoke wait script.

No Caddy/Helm in this slice (local dev hits services directly on `127.0.0.1`). Caddy + Helm belong to `v1/F14-docker-compose-selfhost` / the production self-hosting docs (`v1/F15-selfhosting-docs`).

---

## 4. Public interfaces / contracts (exact signatures, Pydantic models, Protocols, YAML schemas)

**Demo-seed contract** (`packages/contracts/forge_contracts/seed.py`):

```python
from __future__ import annotations
from typing import Protocol, runtime_checkable
from uuid import UUID, NAMESPACE_URL, uuid5
import random
from datetime import datetime
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

# Fixed namespace so demo PKs are reproducible across machines/runs.
DEMO_NAMESPACE: UUID = uuid5(NAMESPACE_URL, "forge.demo.v1")

def demo_id(entity: str, natural_key: str) -> UUID:
    """Deterministic primary key for a demo row, e.g. demo_id('task', 'DEMO-1')."""
    return uuid5(DEMO_NAMESPACE, f"{entity}:{natural_key}")

class SeedContext(BaseModel):
    model_config = {"arbitrary_types_allowed": True}
    session: AsyncSession
    workspace_id: UUID
    admin_user_id: UUID
    now: datetime
    rng: random.Random                       # seeded random.Random(1337)

class SeedResult(BaseModel):
    seeder: str                              # e.g. "board"
    created: dict[str, int] = Field(default_factory=dict)   # {"tasks": 6, "epics": 1}
    updated: dict[str, int] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)

@runtime_checkable
class DemoSeeder(Protocol):
    name: str
    order: int
    async def seed(self, ctx: SeedContext) -> SeedResult:
        """Idempotently create/update this domain's demo rows. MUST NOT call any
        external model/embedding/reranker service. MUST use demo_id(...) for PKs
        and ctx.rng for any randomness so results are deterministic."""
        ...
```

**Per-slice contribution hook** (implemented by `v1/F01-project-board` / `v1/F02-spec-engine` / `v1/F04-repo-policy` / `v1/F05-hybrid-knowledge-retrieval`, and optionally `v1/F11-skill-profiles`; called by the registry):

```python
# e.g. packages/board-core/src/board_core/seed.py
def get_demo_seeder() -> DemoSeeder | None: ...   # returns None if there is nothing to seed
```

**Orchestrator** (`apps/api/forge_api/seed/orchestrator.py`):

```python
class SeedManifest(BaseModel):
    workspace_slug: str
    workspace_id: UUID
    admin_email: str
    admin_password: str | None       # Better Auth email/password login secret; echoed only on first create; None on idempotent re-run
    api_key: str | None              # demo platform_api_key plaintext; only on first create (hashed at rest thereafter)
    api_url: str
    web_url: str
    minio_console_url: str
    results: list[SeedResult]
    model_provider_key_present: bool
    created_at: datetime

async def run_seed(
    session: AsyncSession,
    *,
    reset: bool = False,
    admin_email: str = "admin@demo.forge.local",
    admin_password: str = "forge-demo",
) -> SeedManifest: ...
```

**Seed CLI** (`python -m forge_api.scripts.seed`):

```
usage: seed [--reset] [--admin-email EMAIL] [--admin-password PASS] [--quiet]

--reset            Delete the demo workspace (is_demo=true) and reseed from scratch.
--admin-email      Override demo admin email (default $FORGE_DEMO_ADMIN_EMAIL or admin@demo.forge.local).
--admin-password   Override demo admin password (default $FORGE_DEMO_ADMIN_PASSWORD or forge-demo).
--quiet            Suppress the summary panel (still writes .forge/demo-manifest.json).
Exit codes: 0 success · 1 DB unreachable · 2 migration not applied (missing tables).
```

**Preflight contract** (`scripts/preflight.sh`, also `forge_api.scripts.doctor:run_checks() -> list[CheckResult]`):

```python
class CheckResult(BaseModel):
    name: str            # "docker", "compose>=2.17", "uv", "node>=22", "pnpm",
                         # "docker-daemon", "port:5432", "port:6379", "port:9000",
                         # "port:8000", "port:3000", "disk>=5GB"
    ok: bool
    detail: str          # observed value / what was found
    fix: str             # actionable remediation, shown when ok is False
```

Exit codes for `preflight.sh` / `make doctor`: `0` all pass · `1` a required tool is missing · `2` Docker daemon not running · `3` a required port is occupied · `4` a tool is present but too old · `5` insufficient disk. Each failing check prints `ERROR: <name> — <detail> — FIX: <fix>`.

**Demo manifest file** (`.forge/demo-manifest.json`) — serialization of `SeedManifest`; gitignored (`.forge/` patterns already excluded via `.gitignore`'s `.forge/cache/`; this slice adds `.forge/demo-manifest.json` to `.gitignore`).

**`.env` bootstrap rules** (implemented in `make setup`): copy `.env.example`→`.env` only if `.env` is absent; then substitute lines matching `^SECRET_KEY=` and `^AUTH_SECRET=` whose value is the placeholder with `openssl rand -hex 32` (or `python -c "import secrets;print(secrets.token_hex(32))"` fallback). Never touch an existing `.env`.

**Quickstart doc contract** — `docs/self-hosting/quickstart.md` is the authoritative version of the journey in §2; a doc-lint test (AC12) asserts every `make <target>` referenced in the doc's fenced code blocks exists in the `Makefile`.

---

## 5. Dependencies — features/slices that must exist first

Hard (the mechanism cannot run without these):

- **Foundation substrate** (REQUIRED) — `uv`/`pnpm` workspaces (present), `apps/{api,worker,mcp-gateway,web}` skeletons that the dev-compose Dockerfiles build, the `apps/api` FastAPI app with an async session factory + config and a **`/healthz`** liveness route, the `apps/worker` Celery app (`forge_worker.app`), the `packages/db` (`forge_db`) Alembic env + `alembic.ini` referenced by `make migrate`, the baseline migration `0001_foundations` (`workspaces`/`users`/`teams`), and `Base`/`TimestampMixin`. This Phase-0 substrate has no settled numbered file yet; sibling slices refer to it variously as `cross-cutting/C01-monorepo-and-api-foundations` / `cross-cutting/F00-platform-foundation` / `v1/F00-foundation-substrate` — reconcile to the final foundation slug when it lands. (Repo today is a skeleton: these targets are referenced by the Makefile but not yet implemented.)
- `cross-cutting/F37-auth-secrets-byok` (REQUIRED — the authoritative auth/RBAC/secrets slug; supersedes the stale `C02-auth-and-rbac` reference) — the password hasher and **Better Auth email/password credentials sign-in** the demo admin uses for the Web UI; `platform_api_key` issuance + constant-time verification (the demo `demo-cli` key the headless smoke test authenticates with, AC9); the `Principal` + `require_role(...)` RBAC dependency every seeded route is gated by. **Flag:** F37's slice currently emphasizes OAuth (Google/GitHub/GitLab); the zero-OAuth local quickstart additionally requires F37 to enable Better Auth's email/password credentials provider in `FORGE_ENV=development`. The headless smoke path does **not** depend on it (it uses the platform API key), so this slice's CI "done" is not blocked by the credentials provider — only the interactive Web-UI login is.

Soft (the seed orchestrator runs without them and simply produces a smaller demo; each contributes a `DemoSeeder` when present):

- `v1/F01-project-board` — board demo seeder (project/epics/tasks/statuses). Required for a *meaningful* demo and for AC4's task counts; the smoke test's project/task assertions (AC9) are gated on F01.
- `v1/F02-spec-engine` — approved demo spec linked to the demo epic.
- `v1/F04-repo-policy` — example policy profile mirroring the spec `policy.yaml`.
- `v1/F05-hybrid-knowledge-retrieval` — the (non-indexed) `knowledge_sources` record only; also owns the `reranker` image gated behind the off-by-default `rerank` compose profile.
- `v1/F11-skill-profiles` — provides the seven built-in skill profiles that the demo tasks' `skill_profile` names resolve against (code-resolved registry, **no seeding**); MAY contribute an optional `get_demo_seeder()` that creates one custom override profile.
- `v1/F09-mcp-gateway-v1` — full MCP-gateway behavior; F13 needs only the gateway **skeleton**'s `/healthz` (foundation substrate) for AC8. If F09 is absent the skeleton still answers health.
- `cross-cutting/F39-audit-log` — contract-only soft dep: the core seeder emits bootstrap `AuditEvent`s through F39's `AuditSink`, and `make reset` sets F39's `forge.allow_audit_cascade` GUC so the demo-workspace cascade clears the `audit_log` immutability trigger. The `AuditEvent`/`AuditSink` contract lives in `packages/contracts`, so the seeder compiles against the Protocol (no-op sink in tests) before F39's writer ships.

Sibling (must NOT be folded into this slice; referenced for boundaries):

- `v1/F14-docker-compose-selfhost` — production single-node compose (`deploy/docker-compose.yml`), Caddy, autoheal, production pinned-digest posture.
- `v1/F15-selfhosting-docs` — the rest of `docs/self-hosting/` (docker-compose.md, kubernetes.md, reverse-proxy.md, backup/restore/upgrade/security/troubleshooting). F13 ships only `quickstart.md` + `docker-compose.dev.yml`.

---

## 6. Acceptance criteria (numbered, testable)

1. **Fresh-clone time budget.** On a host meeting the documented prerequisites with base images pre-pulled, `make setup` followed by `docker compose -f deploy/docker-compose.dev.yml up -d --wait` completes with exit 0 in **< 10 minutes** wall-clock (measured by `scripts/quickstart_smoke.sh`).
2. **Infra healthy.** After `make setup`, `docker compose -f deploy/docker-compose.dev.yml ps` reports `db`, `redis`, and `minio` as `healthy`, and the `$MINIO_BUCKET` bucket exists.
3. **Seed idempotency.** Running `make seed` twice in a row both exit 0; the second run creates zero new rows (every `SeedResult.created` sums to 0) and the workspace/project/task counts are unchanged.
4. **Seed content.** After seeding, the demo workspace contains exactly: 1 workspace (slug `demo`, `is_demo=true`), 1 admin user, 1 demo `platform_api_key`, 1 project `DEMO` with 6 statuses, ≥ 1 epic, ≥ 6 tasks spanning ≥ 3 status categories, and — when the corresponding slices are present — 1 policy profile, 1 approved spec, 1 (non-indexed) knowledge source. Counts asserted against `SeedManifest`. Skill profiles are **not** counted as seeded rows: the assertion instead is that the F11 registry resolves the four spec-named built-ins (`backend-tdd`, `frontend-ui`, `incident-response`, `spec-analyst`) and that every seeded task's `skill_profile` name resolves to a built-in (plus the optional 1 custom profile if F11's seeder ran).
5. **No model key required.** The full seed runs to success with `MODEL_PROVIDER_KEY`, `EMBEDDING_PROVIDER`, `RERANKER_URL`, and `LANGSMITH_API_KEY` all unset, and a network-blocking fixture asserts **zero** outbound HTTP calls to any model/embedding/reranker host during seeding.
6. **Doctor fails fast and specifically.** With a required port occupied (simulated by binding it in the test), `make doctor` exits with code `3` and prints a line containing the port number and the word `FIX`. With `docker` removed from `PATH`, it exits `1` naming `docker`.
7. **Env bootstrap.** When `.env` is absent, `make setup` creates it from `.env.example` with non-placeholder `SECRET_KEY` and `AUTH_SECRET` (each ≥ 32 hex chars). When `.env` already exists, its bytes are unchanged after `make setup`.
8. **Services reachable.** After `make dev` (or `up -d --wait`), `GET http://localhost:8000/healthz` returns 200, `GET http://localhost:8001/healthz` returns 200, and `GET http://localhost:3000/` returns 200, each within the compose `--wait` window.
9. **Demo access works end-to-end.** Sending the manifest's demo `platform_api_key` as the `Authorization: Bearer …` header resolves to an authenticated `Principal` (no browser/OAuth needed), and `GET /api/v1/projects` (scoped to the demo workspace) includes the `DEMO` project; `POST /api/v1/projects/{project_id}/tasks/query` returns ≥ 6 tasks. (Gated on F01.) The interactive Web-UI email/password login is the human path and depends on F37's credentials provider (§5); it is exercised manually, not in the headless smoke gate.
10. **Reset determinism.** `make reset` exits 0 and produces a `SeedManifest` whose entity ids (all `uuid5`-derived) and per-entity counts are identical to the pre-reset manifest.
11. **Smoke test in CI.** `scripts/quickstart_smoke.sh` runs the whole flow headless on a clean checkout, asserts ACs 2/4/8/9 and the AC1 time budget, and exits non-zero (failing the build) on any breach or timeout.
12. **Doc ↔ Makefile parity.** A test parses fenced code blocks in `docs/self-hosting/quickstart.md`; every `make <target>` referenced exists in `Makefile`, and the documented URLs (`http://localhost:3000`, `:8000`) match the compose port bindings.
13. **Idempotent infra.** Running `make setup` twice (without `reset`) does not error on already-running containers or an already-applied migration, and does not duplicate the MinIO bucket.
14. **Localhost-only binding.** Every published port in `deploy/docker-compose.dev.yml` is bound to `127.0.0.1` (asserted by a static lint of the compose file), so the dev stack is not exposed on the host's external interfaces.

---

## 7. Test plan (TDD) — concrete test cases (unit + integration), key fixtures

Write tests first; each maps to an acceptance criterion (AC#). Python tests live under `tests/quickstart/` and `apps/api/tests/seed/`; shell flow is covered by the smoke script invoked from a marked integration test.

**Unit (pytest, no Docker)** — `tests/quickstart/`:
- `test_doctor_checks.py`: `doctor.run_checks()` returns a `CheckResult` per probe; with a socket bound on a probe port, the matching `port:*` check is `ok=False` with a `fix` mentioning the port (AC6); a stubbed `which` missing `docker` yields `ok=False name="docker"` (AC6); version parsing flags Node 20 as too old vs 22 as ok (AC6, exit-code mapping tested via `doctor.exit_code(results)`).
- `test_env_bootstrap.py`: bootstrap into a temp dir with no `.env` creates one with randomized `SECRET_KEY`/`AUTH_SECRET` (≥ 32 hex, ≠ placeholder) and leaves all other lines equal to `.env.example` (AC7); bootstrap when `.env` exists is a byte-for-byte no-op (AC7).
- `test_demo_ids.py`: `demo_id("task","DEMO-1")` is stable across calls/processes; different natural keys differ (determinism backbone for AC10).
- `test_registry_order.py`: the registry yields seeders strictly ordered by `order`, skips contributors returning `None`, and always places `core` first (AC4 mechanism).
- `test_compose_lint.py`: parse `deploy/docker-compose.dev.yml`; assert every published port is `127.0.0.1:`-prefixed (AC14), every long-running service has a `healthcheck`, every image has an `@sha256:` digest, and stateful services use named volumes.
- `test_quickstart_doc_matches_make.py`: every `make <target>` in `quickstart.md` exists in `Makefile`; documented URLs match compose bindings (AC12).

**Integration (pytest + ephemeral Postgres via testcontainers/transactional DB)** — `apps/api/tests/seed/`:
- `test_seed_orchestrator.py`: `run_seed` on a migrated empty DB creates the workspace/admin + demo `platform_api_key` and the registered domain content; `SeedManifest` counts match AC4 (and `skill_profiles` contributes **0** seeded rows); admin password + platform API key are present on first create, `None` on re-run; with a recording `AuditSink` injected, one bootstrap `AuditEvent` per privileged creation is emitted (none on idempotent re-run).
- `test_seed_idempotent.py`: run `run_seed` twice; second run's aggregated `created` is all zeros, counts unchanged (AC3).
- `test_seed_reset.py`: seed → `run_seed(reset=True)` → manifest ids and counts identical (AC10); a stray non-demo workspace is untouched by reset; with an `audit_log` row present for the demo workspace, reset sets `forge.allow_audit_cascade` and the cascade succeeds (no immutability-trigger error), while a reset that does **not** set the GUC raises (guards the F39 interaction).
- `test_seed_no_model_key.py`: monkeypatch env to clear all provider keys and install an autouse fixture that patches `httpx`/`anyio` socket connect to raise on any non-localhost host; assert `run_seed` succeeds and the network guard recorded zero external calls (AC5).
- `test_seed_requires_migration.py`: running the CLI against a DB with no tables exits `2` (AC: CLI exit-code contract).

**End-to-end smoke (marked `quickstart`, opt-in locally, required in CI)** — `tests/quickstart/test_smoke.py` shells out to `scripts/quickstart_smoke.sh`:
- Times `make setup` + `up -d --wait`; fails if > 10 min (AC1).
- Asserts `compose ps` health + bucket exists (AC2/AC13).
- Polls `:8000/healthz`, `:8001/healthz`, `:3000/` for 200 (AC8).
- Authenticates with the demo `platform_api_key` (`Authorization: Bearer …`), lists projects (includes `DEMO`), queries tasks ≥ 6 (AC9, skipped with a clear reason if F01 absent).
- Tears down with `make down`/`make reset` in a `finally`.

**Key fixtures:**
- `migrated_db` — testcontainers Postgres (pgvector image) with `alembic upgrade head` applied.
- `seed_session` — `AsyncSession` bound to `migrated_db`, rolled back per test where possible (full-seed tests commit then truncate).
- `no_network_guard` — autouse patch raising on outbound connect to any host not in `{127.0.0.1, localhost, db, redis, minio}`.
- `free_port` / `occupied_port` — helpers that bind a socket to drive the doctor port checks.
- `tmp_repo_env` — temp dir with a copy of `.env.example` for env-bootstrap tests.

CI wiring: a dedicated `quickstart-smoke` job pre-pulls the pinned images (so the AC1 budget measures setup, not registry latency), then runs the smoke test on a clean checkout. The fast unit/integration suite runs in the normal `pytest` gate; the smoke job runs on PRs touching `Makefile`, `deploy/**`, `scripts/**`, `apps/api/forge_api/seed/**`, or `docs/self-hosting/quickstart.md`.

---

## 8. Security & policy considerations

- **Dev-only credentials, never production.** The deterministic local DB/MinIO credentials and the demo admin password exist solely for `localhost` dev. The quickstart doc states this in bold and points to `v1/F14-docker-compose-selfhost` / `security.md` (`v1/F15-selfhosting-docs`) for real deployments. The dev compose binds **only** to `127.0.0.1` (AC14) so the stack is never reachable from the LAN.
- **Generated secrets for crypto-sensitive values.** `SECRET_KEY` and `AUTH_SECRET` are randomized on `.env` creation (AC7) so two different machines never share signing keys, even though DB/MinIO creds are fixed dev defaults.
- **`.env` never committed.** Already enforced by `.gitignore` (`*.env` patterns with `!.env.example`); this slice additionally gitignores `.forge/demo-manifest.json` (it contains the local admin password + API key in plaintext).
- **No secret-bearing outbound calls during seeding.** The seed path makes zero model/embedding/reranker calls (AC5), so no BYOK key is read, logged, or transmitted during onboarding.
- **Demo data isolation.** Demo rows live in a single `is_demo=true` workspace; `make reset` / `--reset` scope deletion to that workspace, so the mechanism cannot wipe a real workspace.
- **Manifest hygiene.** The printed summary and `demo-manifest.json` are the only places the local admin password/API key surface; they are explicitly labeled local-only and the file is gitignored. The plaintext API key is echoed once (first create) and never re-derivable on idempotent runs.
- **Policy alignment.** The seeded policy profile mirrors the spec `policy.yaml` (read-only-by-default ethos), demonstrating correct posture rather than a permissive example. Agent execution remains gated by the normal policy/approval machinery (other slices) — the demo never relaxes those gates.
- **Audit log (non-negotiable interplay).** Seeding is a local first-run **bootstrap**, not an agent action / tool call / MCP call / approval, so the spec's enumerated audit triggers do not fire. Even so, the privileged creations it performs (workspace, admin user, platform API key) are audited: when `cross-cutting/F39-audit-log`'s `AuditSink` is present the core seeder emits one `AuditEvent` per creation (`actor="system:demo-seed"`), and `make reset` deletes audit rows only via F39's sanctioned `forge.allow_audit_cascade` teardown path — it never mutates or silently drops the append-only `audit_log`.
- **No MCP connection seeded.** The demo seeds a `knowledge_sources` *record only* and **no** `MCPConnection`, so the MCP read-only-by-default rule is not exercised at quickstart (exploration needs no external MCP). Any future seeded MCP connection must keep `allow_write: false` per the spec's MCP Security Rules.
- **Spec-gating preserved.** The seeded `SPEC-DEMO-1` is `status: approved`, so the board demonstrates the spec-gated posture honestly; because no model key is present the demo runs no agents, so the spec-approval / human-approval-before-merge gates are never bypassed — they are simply not invoked.

---

## 9. Effort estimate & risk (S/M/L + key risks)

**Effort: M** (~1.5–2 engineer-weeks: ~0.5 compose + Makefile + preflight, ~0.5 seed orchestrator + contract, ~0.5 smoke test + CI wiring + docs). Most of the *content* (board/spec/policy/knowledge rows; skill profiles are code-resolved, not seeded) is contributed by the owning slices via the `DemoSeeder` contract, keeping this slice's surface focused on mechanism.

Key risks:
- **Cold image pull blows the 10-min budget** (Med): a slow network pulling Postgres/Redis/MinIO/base images can dominate. Mitigation: pin slim images by digest, document a broadband + pre-pull assumption, and have CI pre-pull so the measured budget is setup-only (not registry latency). The doc states the budget is "prereqs installed + images pre-pulled."
- **Cross-platform shell/make differences** (Med): macOS (BSD) vs Linux (GNU) `sed`/`openssl`/`date`. Mitigation: `preflight.sh` is POSIX `sh` with no GNU-only flags; secret generation prefers `openssl rand` with a `python -c secrets` fallback; avoid in-place `sed -i` portability traps (write to temp + move).
- **Compose `--wait` availability** (Low): requires Compose ≥ 2.17. Mitigation: preflight checks the version and fails with a clear upgrade `FIX` (exit 4).
- **Seed coupling to evolving domain models** (Med): if F01/F02/F04/F05/F11 schemas drift, seeders break. Mitigation: the `DemoSeeder` Protocol + per-slice ownership means each slice keeps *its own* seeder green; F13's orchestrator/contract stay stable, and missing slices are skipped (soft deps) rather than crashing the seed.
- **Port conflicts on developer machines** (Med): 5432/6379/3000/8000 are commonly occupied. Mitigation: doctor port checks (AC6/AC14) with a remediation that points at the overridable `*_PORT` env vars.
- **First `make dev` app-image build latency** (Med): bundled into the perceived "<10 min" by users. Mitigation: layer-cached uv/pnpm installs in the foundation-substrate Dockerfiles; document that the *headline* budget is setup+infra and the first dev build is incremental on top.

---

## 10. Key files / paths (exact)

Created by this slice:
- `deploy/docker-compose.dev.yml`
- `scripts/preflight.sh`
- `scripts/quickstart_smoke.sh`
- `packages/contracts/forge_contracts/seed.py`  *(DemoSeeder, SeedContext, SeedResult, demo_id, DEMO_NAMESPACE)*
- `apps/api/forge_api/seed/__init__.py`
- `apps/api/forge_api/seed/orchestrator.py`
- `apps/api/forge_api/seed/core.py`
- `apps/api/forge_api/seed/registry.py`
- `apps/api/forge_api/scripts/__init__.py`
- `apps/api/forge_api/scripts/seed.py`  *(entrypoint for `python -m forge_api.scripts.seed`, already referenced by `make seed`)*
- `apps/api/forge_api/scripts/doctor.py`
- `docs/self-hosting/quickstart.md`
- `tests/quickstart/test_doctor_checks.py`
- `tests/quickstart/test_env_bootstrap.py`
- `tests/quickstart/test_demo_ids.py`
- `tests/quickstart/test_registry_order.py`
- `tests/quickstart/test_compose_lint.py`
- `tests/quickstart/test_quickstart_doc_matches_make.py`
- `tests/quickstart/test_smoke.py`  *(marked `quickstart`)*
- `apps/api/tests/seed/test_seed_orchestrator.py`
- `apps/api/tests/seed/test_seed_idempotent.py`
- `apps/api/tests/seed/test_seed_reset.py`
- `apps/api/tests/seed/test_seed_no_model_key.py`
- `apps/api/tests/seed/test_seed_requires_migration.py`

Edited by this slice:
- `Makefile`  *(rewrite `setup`; add `doctor`, `reset`, `down`; keep `dev`/`seed`/`migrate` invocations)*
- `.gitignore`  *(add `.forge/demo-manifest.json`)*

Contributed by other slices (consumed here via `get_demo_seeder()`):
- `packages/board-core/src/board_core/seed.py` (`v1/F01-project-board`)
- `packages/spec-engine/src/forge_spec_engine/seed.py` (`v1/F02-spec-engine`)
- `packages/policy-sdk/forge_policy/seed.py` (`v1/F04-repo-policy`)
- `packages/knowledge-core/src/knowledge_core/seed.py` (`v1/F05-hybrid-knowledge-retrieval`)
- `packages/skill-sdk/forge_skills/seed.py` (`v1/F11-skill-profiles`, optional — built-ins are code-resolved, so this only adds a custom demo profile)

Pre-existing, referenced (must exist via deps):
- `Makefile` `migrate` target → `packages/db/alembic.ini` (foundation substrate / `forge_db`)
- `apps/api` `/healthz` route (foundation substrate)
- `cross-cutting/F37-auth-secrets-byok` — password hasher, `platform_api_key` issuer/verifier, `Principal`/`require_role`
- `packages/contracts/forge_contracts/audit.py` — `AuditEvent` + `AuditSink` (`cross-cutting/F39-audit-log`)
- `.env.example` (root, present)

---

## 11. Research references (relevant links from the spec/research report)

- Self-hosting first / `docker compose up` delivers a working platform — FORGE_SPEC.md "Product Vision" Principle #9 and "Self-Hosting and Deployment → Local Quickstart".
- Required self-hosting docs (`quickstart.md` "local setup under 10 minutes") — FORGE_SPEC.md "Required Self-Hosting Documentation (must ship at launch)".
- Docker Compose viable for small-team/dev with operational discipline (digests, named volumes, healthchecks, network hygiene) — forge-research-report.md "Self-Hosting and Deployment"; https://distr.sh/blog/running-docker-in-production/ and https://nickjanetakis.com/blog/best-practices-around-production-ready-web-apps-with-docker-compose
- Reference self-hosting compose layouts (multi-service local stacks, env bootstrap, bucket init): Langfuse https://langfuse.com/self-hosting/deployment/docker-compose · Trigger.dev https://trigger.dev/docs/self-hosting/docker · Plane.so https://developers.plane.so/self-hosting/methods/docker-compose
- Full-stack FastAPI + LangGraph + Next.js + Docker templates (local dev compose + seeding patterns) — forge-research-report.md "Self-Hosting and Deployment"; https://github.com/vstorm-co/full-stack-ai-agent-template · https://github.com/tiangolo/full-stack-fastapi-template
- pgvector image / Postgres-as-everything for dev — https://github.com/pgvector/pgvector
- MinIO local object storage — https://min.io/
- uv / Ruff (Python tooling for `make install`) — https://docs.astral.sh/uv/ · https://docs.astral.sh/ruff/
- Eval-first / deterministic fixtures rationale (why the demo seed must be reproducible for the golden set) — forge-research-report.md "What Makes Forge Buildable → Eval-first development".

---

## 12. Out of scope / future

- **Production single-node compose** (`deploy/docker-compose.yml`), Caddy auto-HTTPS, autoheal sidecar, production digest posture, and `docker compose exec api forge-cli ...` admin bootstrap — owned by `v1/F14-docker-compose-selfhost`.
- **The rest of `docs/self-hosting/`** — `docker-compose.md`, `kubernetes.md`, `reverse-proxy.md`, `backup.md`, `restore.md`, `upgrade.md`, `security.md`, `troubleshooting.md` — sibling slice(s); F13 ships only `quickstart.md`.
- **Kubernetes / Helm local dev** (kind/minikube) — v2 (`Kubernetes Helm chart`).
- **Knowledge indexing in the demo** (embeddings + Jina reranker over the repo) — requires BYOK + the `rerank` profile; the demo seeds a *record only*. Indexing-on-quickstart is future once a self-hosted embedding option ships.
- **Live agent run in the demo** — requires `MODEL_PROVIDER_KEY`; the quickstart deliberately works without one. A scripted "demo agent run" walkthrough is future.
- **GitHub App wiring in the demo** — the demo `repo_targets` reference a placeholder repo; real installation flow is F03 and documented separately.
- **"Demo workspace" UI banner / guided tour** — a nice-to-have for the F01 board UI (gated on `workspace.is_demo`); not required for this slice's "done".
- **One-line remote installer** (`curl … | sh`) for VMs — that is the production path (`deploy/scripts/install.sh`), not local dev.
