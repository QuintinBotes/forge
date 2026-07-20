# Getting started

> **Status:** Forge is **pre-1.0 and under active development**. This guide runs
> the self-hosted stack for **evaluation and testing** — not production. See
> [Status](../README.md#status) and
> [`RELEASE_READINESS.md`](../RELEASE_READINESS.md) for the honest per-area
> state before you rely on it.

This walkthrough takes you from a clone to your first orchestrated run: stand up
the stack, open the board, write a spec, and watch an agent execute it. It
should take about 15 minutes on a machine with Docker.

![The Forge in-app walkthrough — the loop every change travels through: create a
spec, run an agent, review the PR, merge & ship.](./assets/screenshots/walkthrough.png)

For the deeper self-hosting reference (production hardening, day-2 operations,
Kubernetes) start at the
[self-hosting quickstart](./self-hosting/quickstart.md). For the mental model
behind specs, workflows, agents, and runs, read [Concepts](./concepts.md).

## Prerequisites

- **Docker Engine 24+** and the **Docker Compose v2** plugin
  (`docker compose version`).
- **`make`**.
- Roughly **4 CPU cores and 8 GB RAM** available to Docker.
- A **model-provider key** for the agent runtime to call an LLM (Anthropic by
  default; OpenAI is also supported). You can explore the board and spec engine
  without one — you only need it to execute agent runs. See
  [BYOK & bring-your-own board](./integrations/byok-and-boards.md).

## 1. Clone and configure

```bash
git clone https://github.com/QuintinBotes/forge.git
cd forge
cp .env.example .env
```

Edit `.env` and set, at minimum:

- `FORGE_SECRET_KEY` and `AUTH_SECRET` — long random strings
  (`openssl rand -hex 32`).
- `POSTGRES_PASSWORD` and `MINIO_ROOT_PASSWORD` — strong unique secrets.
- `DOMAIN` — `localhost` for a local run.

To run agent work, also set your model provider:

- `FORGE_MODEL_PROVIDER=anthropic` and a BYOK key (`ANTHROPIC_API_KEY` /
  `OPENAI_API_KEY`, or `FORGE_MODEL_API_KEY`) — or configure it later through the
  encrypted vault (see [BYOK](./integrations/byok-and-boards.md)). Left unset, the
  worker runs an offline scripted model and logs a warning.

Never commit `.env`; it is git-ignored.

## 2. Bring up the stack

```bash
make dev
```

`make dev` builds and starts the full local stack — Postgres (pgvector), Redis,
MinIO, the API, worker, MCP gateway, web UI, and the Caddy edge proxy — then runs
migrations and seeds a demo workspace. When it reports healthy:

- **Web UI:** <http://localhost:3000>
- **API + health check:** <http://localhost:8000/health>

If a service fails to come up, see
[troubleshooting](./self-hosting/troubleshooting.md).

## 3. Open the board

Open <http://localhost:3000>. The **board** is the home surface — it tracks work
items and runs across your workspace. Individual screens honestly flag any area
whose backend projection or live credential is still landing, and the
[Status](../README.md#status) section tracks the honest per-area state.

The left navigation is grouped by area — the board and specs, runs and
approvals, integrations and settings, and the admin surfaces (RBAC, SSO, audit).
Each view leads with a single primary action, so the next step is always
obvious.

If this is your first visit, the **[Walkthrough](http://localhost:3000/walkthrough)**
gives a guided tour of the platform.

## 4. Write a spec

Forge is **spec-driven**: work begins from a written specification, not a bare
prompt. A spec is a `manifest.yaml` describing what to build — requirements,
acceptance criteria, open questions, and constraints — that the **spec engine**
validates before any agent is allowed to run.

Create one from the UI at
**[Specs → New](http://localhost:3000/specs/new)**, or start from a tested
example in the repo:

```yaml
# examples/specs/SPEC-42-rate-limiting/manifest.yaml (excerpt)
id: SPEC-42
name: API rate limiting
status: clarifying          # -> approved once open_questions are resolved
requirements:
  - id: R1
    text: Apply per-API-key request rate limiting on all public endpoints
acceptance_criteria:
  - id: A1
    req_refs: [R1]
    text: A key exceeding its quota is throttled within one limit window
open_questions:
  - id: Q1
    text: Is the limiter fixed-window or token-bucket?
execution_mode: single_agent
skill_profile: backend-tdd
```

While a spec has unresolved `open_questions` its status stays `clarifying`, and
the spec engine's **implementation gate blocks any run** — Forge will not let an
agent execute an ambiguous spec. Resolve the questions and set `status:
approved` to unblock it. The
**[Specs dashboard](http://localhost:3000/specs)** shows each spec's validation
state.

See [`examples/specs/`](../examples/specs) for complete, schema-validated
manifests you can copy.

## 5. Run it and watch the trace

Once a spec is `approved`, start a run against it. The **agent runtime** — a
LangGraph plan → execute → verify loop — picks up the work inside a sandbox
(git-worktree isolation by default; per-task Docker containers available), grounds itself in your codebase through
the hybrid knowledge pipeline, and opens a pull request for the change.

Follow it live in the **[run-trace viewer](http://localhost:3000/runs)**: every
step, tool call, and decision is recorded, so you can see exactly what the agent
did and why. Sensitive actions pause at the
**[approvals](http://localhost:3000/approvals)** queue for a human decision
before they proceed.

## Where to go next

- **[Concepts](./concepts.md)** — the mental model: specs, workflows, agents,
  runs, knowledge, approvals, policies, and integrations.
- **[Architecture](./architecture.md)** — how the pieces fit together and how
  data flows through the platform.
- **[BYOK & bring-your-own board](./integrations/byok-and-boards.md)** — connect
  your model provider keys and your existing Jira / Linear / Asana / Monday /
  GitHub Projects / ClickUp / Trello / GitLab board.
- **[Self-hosting](./self-hosting/quickstart.md)** — production hardening,
  backups, upgrades, Kubernetes/Helm, and Infrastructure as Code.
- **[Examples](../examples/README.md)** — copy-paste, schema-validated policies,
  skills, workflows, MCP connectors, and specs.
