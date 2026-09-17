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

- `MODEL_PROVIDER=anthropic` and `MODEL_PROVIDER_KEY=<your key>` (or configure it
  later through the encrypted vault — see
  [BYOK](./integrations/byok-and-boards.md)).

Never commit `.env`; it is git-ignored.

> **Which env file the dev stack reads.** `make dev` runs Compose with
> `--env-file deploy/.env.dev`, so the **root `.env` above is not read by the
> local dev stack** — it configures a production/manual `docker compose -f
> deploy/docker-compose.yml` run. Override a dev value in `deploy/.env.dev` or
> your shell instead. Setting `POSTGRES_PASSWORD` only in the root `.env` is a
> common way to end up with a database volume whose password no longer matches
> what the stack passes, which fails as
> `password authentication failed for user "forge"` during `migrate`.

## 2. Bring up the stack

```bash
make dev
```

`make dev` builds and starts the full local stack — Postgres (pgvector), Redis,
MinIO, the API, worker, MCP gateway, web UI, and the Caddy edge proxy — then runs
migrations and seeds a demo workspace. When it reports healthy:

- **Forge:** <http://localhost:8080>
- **API health check:** <http://localhost:8080/api/health>

Use **port 8080**. That is the Caddy edge, which serves the UI and `/api/*` from
one origin — the browser makes no cross-origin request and everything resolves.
The web container's own port (3000) and the API's (8000) are published for
debugging only; a browser pointed at 3000 has no `/api` to talk to and the UI
sits there looking broken with nothing on screen explaining why.

`make dev` also prints an **admin API key**. Copy it — you need it in the next
step, and it is shown only once. Lost it? `scripts/dev.sh seed` retires the old
one and prints a fresh one.

If a service fails to come up, see
[troubleshooting](./self-hosting/troubleshooting.md).

## 3. Connect and open the board

Open <http://localhost:8080>. Forge asks you to **connect** before it shows any
data: every API route is authenticated, and until OIDC lands a Forge API key
*is* the credential. Paste the key `make dev` printed.

By default the key is held only for the current tab. "Remember on this browser"
persists it — worth understanding before you tick it, because browser storage is
shared with every other app served from the same origin, and on `localhost` that
means any other local app that has used this port. See
[self-hosting security](./self-hosting/security.md#browser-storage-on-localhost).

The **board** is the home surface — it tracks work
items and runs across your workspace. Individual screens honestly flag any area
whose backend projection or live credential is still landing, and the
[Status](../README.md#status) section tracks the honest per-area state.

The left navigation is grouped by area — the board and specs, runs and
approvals, integrations and settings, and the admin surfaces (RBAC, SSO, audit).
Each view leads with a single primary action, so the next step is always
obvious.

If this is your first visit, the **[Walkthrough](http://localhost:8080/walkthrough)**
gives a guided tour of the platform.

## 4. Write a spec

Forge is **spec-driven**: work begins from a written specification, not a bare
prompt. A spec is a `manifest.yaml` describing what to build — requirements,
acceptance criteria, open questions, and constraints — that the **spec engine**
validates before any agent is allowed to run.

Create one from the UI at
**[Specs → New](http://localhost:8080/specs/new)**, or start from a tested
example in the repo:

```yaml
# examples/specs/SPEC-42-rate-limiting/manifest.yaml (excerpt)
id: SPEC-42
name: API rate limiting
status: clarifying          # authored here; set by /clarify, not inferred
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

A spec starts at `draft`, and the spec engine's **implementation gate blocks any
run** until it reaches `approved` — Forge will not let an agent execute a spec
nobody has signed off. The gate names the status it refused, so a blocked run
tells you exactly why:

```
409  spec "SPEC-1" is "draft"; an approved spec is required before task
     generation or implementation (allowed: ["approved", "implementing", "validated"])
```

The statuses are set by **explicit transitions**, not inferred from the
document's contents: `POST /spec/specs/{id}/clarify` moves a spec to
`clarifying`, and `POST /spec/specs/{id}/approve` to `approved`. Unresolved
`open_questions` do **not** move a spec to `clarifying` on their own — a spec
with open questions sits at `draft`, which the gate blocks just the same. The
**[Specs dashboard](http://localhost:8080/specs)** shows each spec's validation
state.

See [`examples/specs/`](../examples/specs) for complete, schema-validated
manifests you can copy.

## 5. Run it and watch the trace

Once a spec is `approved`, start a run against it. The **agent runtime** — a
LangGraph plan → execute → verify loop — picks up the work inside a sandbox
(git-worktree isolation by default; per-task Docker containers available), grounds itself in your codebase through
the hybrid knowledge pipeline, and opens a pull request for the change.

Follow it live in the **[run-trace viewer](http://localhost:8080/runs)**: every
step, tool call, and decision is recorded, so you can see exactly what the agent
did and why. Sensitive actions pause at the
**[approvals](http://localhost:8080/approvals)** queue for a human decision
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
