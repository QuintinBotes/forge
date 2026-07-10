# Forge

> Self-hostable orchestration for AI engineering work — spec-driven development,
> a sandboxed agent runtime, hybrid knowledge retrieval, and a native project
> board, all on one Postgres-backed platform you run yourself.

> ⚠️ **Under active development — pre-1.0, not production-ready.** Forge is shared
> openly for **evaluation and testing**, not for production use yet. Expect rough
> edges, changing APIs, and features that are API/CLI-first with their UI or live
> integrations still landing. Read **[Status](#status)** for the honest per-area
> state before you rely on it, and please contribute via pull request.

[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](./LICENSE)
[![CI](https://github.com/QuintinBotes/forge/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/QuintinBotes/forge/actions/workflows/ci.yml)
[![Status: pre-1.0](https://img.shields.io/badge/status-pre--1.0%20(active)-orange.svg)](#status)
[![Python 3.14](https://img.shields.io/badge/python-3.14-3776AB.svg)](./.python-version)

Forge turns a written spec into orchestrated engineering work: a spec engine
plans and validates the work, a LangGraph agent runtime executes it inside
isolated sandboxes, a hybrid (semantic + keyword) knowledge pipeline grounds the
agents in your codebase, and a native board tracks it all. It is designed to be
**self-hosted first** — every component ships in a single, hardened Docker
Compose stack (or a Helm chart) that you own end to end.

## Status

Forge is **pre-1.0 and under active development** — usable for evaluation and
self-host testing, **not yet for production**. The backend platform, HTTP API,
CLI, workflow/agent runtime, and self-hosting substrate are the mature surface,
exercised by a large test suite (~3,700 tests on real pgvector Postgres, green
in CI). The **web UI ships 15 feature screens** (board, approvals, run-trace
viewer, spec dashboard, marketplace, incidents, observability, sprints, audit,
deployment gates, SSO/SCIM, RBAC admin, PM integrations, workflow editor, and a
guided walkthrough) on the Forge design system. Some screens carry **honestly
marked gaps** where a backend projection or live credential is still landing
(e.g. a couple of dashboard projections, OIDC), and the
third-party integrations (GitHub App, model BYOK, reranker, MCP, Slack) are
code-complete with tests + runbooks but need **your keys** to verify live. We
try hard not to advertise anything that is only parked — the in-app
"under development" banners and
[`docs/RELEASE_READINESS.md`](./docs/RELEASE_READINESS.md) track the honest
status.

## Key features

- **Spec-driven development** — author a `manifest.yaml` spec; the spec engine
  validates it and drives the work. Includes a spec-validation dashboard.
- **Agent runtime** — a LangGraph agent loop that runs work inside sandboxed
  execution (Docker today; gVisor / Firecracker isolation classes are modelled
  and mapped, with the real-runtime tiers gated behind a virtualization-enabled
  CI job).
- **Multi-agent coordination** — a coordinator for fanning work across agents.
- **Workflow engine** — a Postgres finite-state-machine workflow layer, with
  Temporal available in the production stack for durable orchestration.
- **Hybrid knowledge / RAG** — pgvector cosine search + Postgres full-text
  (BM25-style) fused with Reciprocal Rank Fusion (k=60) and a reranker.
- **Native project board** — a board core for tracking runs and work items.
- **Policy, skill, integration & MCP SDKs** — declarative `.forge/policy.yaml`,
  skill profiles, integration definitions, and an MCP gateway for tool sources.
- **Integration marketplace** — publish and install integrations (backend +
  offline author CLI today; **UI in progress**).
- **Enterprise SSO / SCIM** — SAML SSO and SCIM provisioning on the API
  (**UI in progress**).
- **Human approval system** — gated approvals for sensitive agent actions.
- **Benchmark leaderboard** — submit, verify, and rank agent benchmark runs
  (backend; **UI in progress**).
- **Auth, secrets & BYOK** — envelope-encrypted secrets, a key vault, and
  bring-your-own-key model-provider credentials.
- **Observability & cost metrics + audit log** — structured, redaction-aware
  telemetry and an append-only audit trail.
- **Self-hosting & security** — a hardened, digest-pinned Compose stack, a Helm
  chart, per-image SBOMs, backup/restore runbooks, a STRIDE threat model, and a
  wired enforcement-matrix regression suite enforced in CI.

## Quickstart (self-host, one command)

Requires Docker Engine 24+ and the Docker Compose v2 plugin, plus `make`.

```bash
git clone https://github.com/QuintinBotes/forge.git
cd forge
cp .env.example .env     # then set SECRET_KEY, POSTGRES_PASSWORD, DOMAIN, ...
make dev                 # build + start the full stack, migrate, seed, wait healthy
```

`make dev` brings up Postgres (pgvector), Redis, MinIO, the API, worker, MCP
gateway, web UI, and the Caddy edge proxy, then runs migrations and seeds a demo
workspace. When it reports healthy:

- Web UI: <http://localhost:3000>
- API + health check: <http://localhost:8000/health>

For a **production** deployment (hardened, digest-pinned images) use the
production compose file directly:

```bash
docker compose -f deploy/docker-compose.yml up -d --remove-orphans
```

See [`docs/self-hosting/quickstart.md`](./docs/self-hosting/quickstart.md) for
the full walkthrough, and [`deploy/`](./deploy) for the Compose files, Caddy
config, and Helm chart.

## Tech stack

- **Backend:** Python 3.14, FastAPI, Pydantic v2, SQLAlchemy 2.x, Alembic
- **Agents / workflow:** LangGraph, Postgres FSM, Temporal, Redis + Celery
- **Knowledge / RAG:** pgvector (cosine) + Postgres full-text, RRF fusion (k=60), reranker
- **Frontend:** Next.js 16, React 19, TypeScript, Tailwind CSS v4, shadcn/ui, TanStack Query/Table
- **Infra:** Docker Compose, Caddy, MinIO
- **Tooling:** uv + Ruff + mypy (Python), pnpm (Node)

## Repository layout

```
forge/
├── apps/
│   ├── api/                     # FastAPI backend + CLI (forge_api)
│   ├── worker/                  # Celery workers (forge_worker)
│   ├── mcp-gateway/             # MCP client manager service (forge_mcp_gateway)
│   └── web/                     # Next.js 16 frontend (@forge/web)
├── packages/
│   ├── contracts/               # Frozen Pydantic DTOs + Protocols (forge_contracts)
│   ├── db/                      # SQLAlchemy models + Alembic (forge_db)
│   ├── workflow-engine/         # forge_workflow
│   ├── agent-runtime/           # forge_agent
│   ├── multi-agent-coordinator/ # forge_coordinator
│   ├── spec-engine/             # forge_spec
│   ├── board-core/              # forge_board
│   ├── knowledge-core/          # forge_knowledge
│   ├── integration-sdk/         # forge_integrations
│   ├── mcp-sdk/                 # forge_mcp
│   ├── policy-sdk/              # forge_policy
│   ├── skill-sdk/               # forge_skill
│   ├── evaluation/              # forge_eval
│   ├── auth-sdk/                # forge_auth
│   ├── authz-sdk/               # forge_authz
│   ├── approval-sdk/            # forge_approval
│   ├── marketplace-sdk/         # forge_marketplace
│   ├── observability/           # forge_obs
│   └── deploy-core/             # forge_deploy
├── deploy/                      # docker-compose, Caddy, Helm, scripts, SBOMs
├── examples/                    # policies, skills, workflows, mcp-connectors, specs (tested fixtures)
└── docs/                        # spec, self-hosting, architecture, security
```

The Python workspace is managed by `uv` (members declared in the root
[`pyproject.toml`](./pyproject.toml)). The web app is a separate `pnpm`
workspace ([`pnpm-workspace.yaml`](./pnpm-workspace.yaml)) and is excluded from
the uv workspace.

## Development

```bash
make setup      # uv sync + pnpm install
make dev        # bring up the full local stack (build, migrate, seed, healthcheck)
make test       # uv run pytest
make lint       # ruff check + ruff format --check
```

| Command          | Description                                    |
|------------------|------------------------------------------------|
| `make setup`     | Install Python (uv) + Node (pnpm) deps         |
| `make dev`       | Build + start the full dev stack via Compose   |
| `make test`      | Run the Python test suite (pytest)             |
| `make lint`      | Ruff lint + format check                       |
| `make fmt`       | Ruff auto-format + auto-fix                    |
| `make typecheck` | mypy static type checking                      |
| `make migrate`   | Apply Alembic migrations                       |
| `make seed`      | Seed a demo workspace                          |

Web-app checks run through pnpm:

```bash
pnpm --filter @forge/web lint
pnpm --filter @forge/web build
pnpm --filter @forge/web test
```

TDD is the norm and CI is the source of truth: `ruff check`, `ruff format
--check`, `mypy`, and `pytest` (against a real pgvector Postgres) must be green,
and the web job must lint + build. See [CONTRIBUTING.md](./CONTRIBUTING.md) for
the full workflow.

## Documentation

- [Self-hosting](./docs/self-hosting/quickstart.md) — quickstart, Docker Compose,
  Kubernetes/Helm, backup, restore, upgrade, security, troubleshooting.
- [Infrastructure as Code](./docs/self-hosting/iac.md) — OpenTofu apply runbook
  for Hetzner + Cloudflare + Fly.io (dev/staging/prod).
- [Architecture & spec](./docs/FORGE_SPEC.md) — the platform specification.
- [Security policy](./SECURITY.md) and the
  [threat model](./docs/security/threat-model.md).
- [Examples](./examples/README.md) — copy-paste, schema-validated configuration.

## Contributing

Contributions are welcome — please read [CONTRIBUTING.md](./CONTRIBUTING.md) and
our [Code of Conduct](./CODE_OF_CONDUCT.md). To report a vulnerability, follow
[SECURITY.md](./SECURITY.md) (do not open a public issue).

## License

Apache-2.0 — see [LICENSE](./LICENSE).
