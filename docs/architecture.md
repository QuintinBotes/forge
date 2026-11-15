# Architecture

How Swarm Code Factory fits together. This is the map from the [concepts](./concepts.md) to
the running system — the services, the packages behind them, and how a spec
becomes a merged, tracked change. For standing it up, see
[Getting started](./getting-started.md) and
[Self-hosting](./self-hosting/quickstart.md). The full platform specification
lives in [`SCF_SPEC.md`](./SCF_SPEC.md).

## Design principles

- **Self-hosted first.** Every component ships in one hardened Docker Compose
  stack (or a Helm chart) that you own end to end. No hosted control plane is
  required.
- **Postgres-backed substrate.** Board, workflow state, specs, knowledge
  vectors, and audit all live in one Postgres (with pgvector) — one database to
  back up, one source of truth.
- **Spec-driven.** Work is gated on an approved spec, not a free-form prompt.
- **Contracts at the boundaries.** Frozen Pydantic DTOs and Protocols
  (`scf_contracts`) define the seams between packages, so implementations swap
  without breaking callers.

## Runtime topology

The Compose/Helm stack runs these services:

| Service | Package(s) | Responsibility |
|---------|-----------|----------------|
| **web** | `@scf/web` (Next.js 16) | The UI — board, specs, runs, approvals, admin |
| **api** | `scf_api` (FastAPI) + CLI | HTTP API, auth, spec engine, orchestration entrypoints |
| **worker** | `scf_worker` (Celery) | Async execution: agent runs, knowledge indexing, syncs |
| **mcp-gateway** | `scf_mcp_gateway` | Manages MCP tool-source clients for agents |
| **db** | Postgres + pgvector | Board, workflow FSM, specs, vectors, audit — the substrate |
| **redis** | Redis | Celery broker/result backend, rate-limit + cache state |
| **minio** | MinIO (S3 API) | Object storage (artifacts, blobs) |
| **caddy** | Caddy | TLS-terminating edge proxy in front of web + api |

`make dev` builds and starts all of them, runs Alembic migrations, and seeds a
demo workspace.

## Package map

The Python workspace is managed by `uv`; the web app is a separate `pnpm`
workspace. Business logic lives in `packages/*` behind contracts, and the
`apps/*` services compose them.

```
apps/
  api/            scf_api            FastAPI backend + CLI
  worker/         scf_worker         Celery workers
  mcp-gateway/    scf_mcp_gateway    MCP client manager
  web/            @scf/web           Next.js 16 frontend
packages/
  contracts/      scf_contracts      Frozen Pydantic DTOs + Protocols (the seams)
  db/             scf_db             SQLAlchemy models + Alembic migrations
  spec-engine/    scf_spec           Spec validation + implementation gate
  workflow-engine/scf_workflow       Postgres FSM workflow layer
  agent-runtime/  scf_agent          LangGraph plan/execute/verify + sandboxes
  multi-agent-coordinator/ scf_coordinator  Fan-out / supervision
  orchestration-policy/ scf_orchestration_policy  Adaptive Orchestration: complexity sizing + role→tier/model routing
  board-core/     scf_board          Native project board
  knowledge-core/ scf_knowledge      Hybrid retrieval (pgvector + FTS + RRF + rerank)
  integration-sdk/scf_integrations   Integrations incl. PM adapters
  mcp-sdk/        scf_mcp            MCP gateway SDK
  policy-sdk/     scf_policy         .forge/policy.yaml engine
  skill-sdk/      scf_skill          Skill profiles
  evaluation/     scf_eval           Eval harness + release-readiness gate
  auth-sdk/       scf_auth           Auth, secrets vault, BYOK, SSO/SCIM
  authz-sdk/      scf_authz          Multi-team RBAC
  approval-sdk/   scf_approval       Human approval gates
  marketplace-sdk/scf_marketplace    Integration marketplace
  observability/  scf_obs            Telemetry + cost metrics
  deploy-core/    scf_deploy         Deployment gates
```

## How a change flows

```
 1. Author spec (manifest.yaml)         scf_spec
        │  implementation gate: approved?
        ▼
 2. Workflow drives plan→execute→verify  scf_workflow (Postgres FSM)
        │
        ▼
 3. Agent run in a sandbox               scf_agent (LangGraph) + sandbox provider
        │  grounded by hybrid retrieval  scf_knowledge (pgvector + FTS + RRF)
        │  tools via MCP gateway          scf_mcp_gateway
        │  constrained by policy          scf_policy
        ▼
 4. Opens a pull request                 GitHub App integration
        │
        ▼
 5. Sensitive actions pause for approval  scf_approval
        │
        ▼
 6. Tracked on the board + audited        scf_board + audit log + scf_obs (cost)
```

Every hop is recorded: the run-trace viewer replays step 3, the cost metrics
attribute the model spend, and the append-only audit log records the decisions
in steps 4–6.

## Data & security substrate

- **Secrets** are envelope-encrypted in a per-workspace vault (`scf_auth`): a
  versioned KEK wraps per-workspace DEKs with workspace-id AAD binding, so a blob
  is useless outside its workspace. BYOK model keys use the same path.
- **AuthN/Z**: API keys (peppered), agent tokens with a TTL, SAML SSO + SCIM
  provisioning, and multi-team RBAC (`scf_authz`).
- **Network hardening**: outbound allowlist, SSRF protections, request-body
  limits, and rate limiting are configured through `FORGE_*` env vars.
- **Supply chain**: digest-pinned images, per-image SBOMs, and a wired
  enforcement-matrix regression suite. See the
  [threat model](./security/threat-model.md) and
  [security policy](../SECURITY.md).

## Trust layer & provenance

The trust-layer features are not a new service — each is a self-contained,
append-only record spread across the existing packages, hooked into the flow
above. Full detail (data models, API/CLI, and the parked/Phase-A limitations)
lives in **[Trust layer](./trust-layer.md)**.

| Feature | Where it lives | Hooks into |
|---------|----------------|------------|
| **Attested Changesets** | `scf_contracts.attestation` (DSSE + in-toto contract), `scf_obs.attest.signing` (Ed25519 signer/verifier), `scf_api` `attestation_service` + `scf-verify` CLI, `attestation` table (`scf_db`) | Minted on `pr` approval (step 5); chained into the audit log (step 6) |
| **Time-Travel Runs** | `scf_agent.replay` (record/replay), `scf_worker.agent_runner` (recorder sink), `scf_api` replay/fork endpoints + `scf-replay` CLI, `run_recording` table | Records the agent run (step 3) behind `FORGE_RECORD_RUNS=1` |
| **Red-Team Gate** | `scf_coordinator.red_team` (adversary harness), `scf_workflow` Temporal workflow + activity, `red_team_record` table | Runs inside the workflow (step 2), before the human spec-approval gate |
| **Self-Eval Gate** | `scf_eval.sweval` (gate + scorer), `scf_api` `self_eval_gate`/`self_eval_service` + `/ao` config API, `scf_worker` mint/run tasks, `self_eval_baseline` table | Gates Adaptive Orchestration config changes; baselines minted from merged PRs |

The two realtime WebSocket channels (`scf_api.routers.realtime`) — the board
push (`/ws`) and CRDT spec co-editing (`/ws/spec/{spec_id}`, `pycrdt` server doc
over the Yjs sync protocol) — mount at the app root rather than under the API
prefix, matching the URLs the web client opens.

## Deployment options

| Path | Use | Reference |
|------|-----|-----------|
| **Docker Compose (dev)** | Local evaluation | [`make dev`](./getting-started.md) |
| **Docker Compose (prod)** | Single-host, hardened, digest-pinned | [docker-compose.md](./self-hosting/docker-compose.md) |
| **Kubernetes / Helm** | Clustered | [kubernetes.md](./self-hosting/kubernetes.md) |
| **Infrastructure as Code** | Provisioned cloud (Hetzner + Cloudflare + Fly) | [iac.md](./self-hosting/iac.md) |

## Tech stack

- **Backend:** Python 3.14, FastAPI, Pydantic v2, SQLAlchemy 2.x, Alembic
- **Agents / workflow:** LangGraph, Postgres FSM, Temporal, Redis + Celery
- **Knowledge / RAG:** pgvector (cosine) + Postgres full-text, RRF fusion (k=60),
  reranker
- **Frontend:** Next.js 16, React 19, TypeScript, Tailwind CSS v4, shadcn/ui,
  TanStack Query/Table
- **Infra:** Docker Compose, Caddy, MinIO
- **Tooling:** uv + Ruff + mypy (Python), pnpm (Node)
