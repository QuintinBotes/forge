# Forge

> OSS engineering-orchestration platform — spec-driven development, agent runtime,
> hybrid knowledge retrieval, native project board, and self-hosting first.

Forge is a self-hostable platform for orchestrating AI engineering work: it pairs a
spec-driven development engine with a LangGraph agent runtime, a hybrid (semantic +
keyword + RRF + rerank) knowledge pipeline, a native project board, repo policy and
skill profiles, MCP integration, and a Postgres-backed workflow engine.

**License:** Apache-2.0 · **Status:** V1 in active development.

## Tech stack

- **Backend:** Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2.x, Alembic
- **Agents/workflow:** LangGraph, Postgres FSM, Redis + Celery
- **Knowledge/RAG:** pgvector (cosine) + Postgres full-text (BM25), RRF fusion (k=60), Jina reranker
- **Frontend:** Next.js 15, TypeScript, Tailwind, shadcn/ui, TanStack Query/Table
- **Infra:** Docker Compose, Caddy, MinIO
- **Tooling:** uv + Ruff (Python), pnpm (Node)

## Repository layout

```
forge/
├── apps/
│   ├── api/            # FastAPI backend (forge_api)
│   ├── worker/         # Celery workers (forge_worker)
│   ├── mcp-gateway/    # MCP client manager service (forge_mcp_gateway)
│   └── web/            # Next.js frontend
├── packages/
│   ├── contracts/      # Frozen Pydantic DTOs + Protocols (forge_contracts)
│   ├── db/             # SQLAlchemy models + Alembic (forge_db)
│   ├── workflow-engine/        # forge_workflow
│   ├── agent-runtime/          # forge_agent
│   ├── multi-agent-coordinator/# forge_coordinator
│   ├── spec-engine/            # forge_spec
│   ├── board-core/             # forge_board
│   ├── knowledge-core/         # forge_knowledge
│   ├── integration-sdk/        # forge_integrations
│   ├── mcp-sdk/                # forge_mcp
│   ├── policy-sdk/             # forge_policy
│   ├── skill-sdk/              # forge_skill
│   ├── evaluation/             # forge_eval
│   └── ui-kit/         # Shared React components
├── deploy/             # docker-compose, Caddy, scripts
├── examples/           # policies, skills, workflows, mcp-connectors, specs
└── docs/               # spec, self-hosting, architecture
```

The Python workspace is managed by `uv` (members declared in the root
`pyproject.toml`). The Node workspace is managed by `pnpm` (`pnpm-workspace.yaml`).

## Quickstart (local)

```bash
git clone https://github.com/forge-platform/forge
cd forge
cp .env.example .env
make setup    # installs python + node deps
# start Postgres/Redis/MinIO (see deploy/), then:
make migrate  # apply database migrations
make seed     # seed a demo workspace
make dev      # start all services
# Web UI: http://localhost:3000 | API: http://localhost:8000
```

## Development

| Command          | Description                                   |
|------------------|-----------------------------------------------|
| `make install`   | Install python (uv) + node (pnpm) deps        |
| `make test`      | Run the python test suite                     |
| `make lint`      | Ruff lint + format check                      |
| `make fmt`       | Ruff auto-format + auto-fix                    |
| `make typecheck` | mypy static type checking                      |
| `make migrate`   | Apply Alembic migrations                       |
| `make seed`      | Seed a demo workspace                          |

TDD is mandatory: write a failing test, implement, then run the green gate
(`ruff check`, type check, and `pytest`) before a unit is considered done.

## License

Apache-2.0 — see [LICENSE](./LICENSE).
