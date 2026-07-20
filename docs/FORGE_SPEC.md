# Forge — Engineering Orchestration Platform Specification

> **Product name: Forge** — A forge shapes raw material into something precise and durable, exactly what this platform does with specs and tasks. Short, memorable, works as a CLI prefix: `forge run`, `forge spec`, `forge plan`, `forge task`.

---

## Research Links

Provided so AI coding agents (Claude Code etc.) can research each topic before implementing. **Read these before making any architectural decision.**

### Orchestration & Workflow Engines
- LangGraph official docs: https://langchain-ai.github.io/langgraph/
- LangGraph source: https://github.com/langchain-ai/langgraph
- LangGraph production guide 2026: https://www.reactify-solutions.com/articles/langgraph-production-agents-2026
- Temporal self-hosting guide: https://docs.temporal.io/self-hosted-guide
- Temporal vs LangGraph (June 2026): https://suhasbhairav.com/blog/temporal-vs-langgraph-durable-workflow-orchestration-vs-llm-agent-state-machines
- Human-in-the-loop with LangGraph: https://www.youtube.com/watch?v=4F8wvpb8JkI

### Reference Implementations
- Symphony (OpenAI) — task-as-control-plane: https://openai.com/index/open-source-codex-orchestration-symphony/
- Symphony on InfoQ: https://www.infoq.com/news/2026/05/openai-symphony-agents/
- Open SWE (LangChain): https://www.langchain.com/blog/open-swe-an-open-source-framework-for-internal-coding-agents
- Open SWE GitHub: https://github.com/langchain-ai/open-swe
- Open SWE demo: https://www.youtube.com/watch?v=TaYVvXbOs8c
- Open SWE deep dive: https://byteiota.com/open-swe-langchain-autonomous-coding-agent/

### Spec-Driven Development
- GitHub Spec Kit: https://github.com/github/spec-kit
- GitHub Spec Kit blog: https://github.blog/ai-and-ml/generative-ai/spec-driven-development-with-ai-get-started-with-a-new-open-source-toolkit/
- Microsoft SDD: https://developer.microsoft.com/blog/spec-driven-development-ai-native-engineering
- Microsoft Spec Kit deep dive: https://developer.microsoft.com/blog/spec-driven-development-spec-kit
- SDD arXiv paper: https://arxiv.org/html/2602.00180v1

### Multi-Agent Orchestration
- 6 production patterns (2026): https://beam.ai/agentic-insights/multi-agent-orchestration-patterns-production
- Pattern selection guide: https://www.kore.ai/blog/choosing-the-right-orchestration-pattern-for-multi-agent-systems
- Best multi-agent frameworks 2026: https://gurusup.com/blog/best-multi-agent-frameworks-2026
- LangChain agent frameworks 2026: https://www.langchain.com/resources/ai-agent-frameworks

### Skill Profiles
- Superpowers framework: https://www.termdock.com/en/blog/superpowers-framework-agent-skills
- Superpowers MCP server: https://mcpmarket.com/server/superpowers
- Superpowers explained: https://www.verdent.ai/guides/what-is-superpowers-ai-coding-framework

### Model Context Protocol (MCP)
- MCP specification (2025-11-25): https://modelcontextprotocol.io/specification/2025-11-25
- MCP 2026 RC (stateless HTTP, ships July 28 2026): https://blog.modelcontextprotocol.io/posts/2026-07-28-release-candidate/
- MCP June 2025 update (OAuth, structured output): https://forgecode.dev/blog/mcp-spec-updates/
- MCP GitHub org: https://github.com/modelcontextprotocol
- MCP security advisory (DoD 2026): https://media.defense.gov/2026/Jun/02/2003943289/-1/-1/0/CSI_MCP_SECURITY.PDF
- MCP knowledge retrieval servers: https://mcpservers.org/topics/knowledge-retrieval-mcp
- MCP resources guide: https://github.com/cyanheads/model-context-protocol-resources

### RAG and Hybrid Retrieval
- Production RAG guide 2026: https://lushbinary.com/blog/rag-retrieval-augmented-generation-production-guide/
- Hybrid search BM25 + pgvector: https://github.com/Syed007Hassan/Hybrid-Search-For-Rag
- pgvector: https://github.com/pgvector/pgvector
- Jina Reranker v2: https://jina.ai/reranker/
- ColBERT v2: https://github.com/stanford-futuredata/ColBERT

### Self-Hosting & Deployment
- Docker Compose in production 2026: https://distr.sh/blog/running-docker-in-production/
- Docker Compose best practices: https://nickjanetakis.com/blog/best-practices-around-production-ready-web-apps-with-docker-compose
- Langfuse self-hosting reference: https://langfuse.com/self-hosting/deployment/docker-compose
- Trigger.dev self-hosting reference: https://trigger.dev/docs/self-hosting/docker
- Plane.so self-hosting reference: https://developers.plane.so/self-hosting/methods/docker-compose
- Comprehensive self-hosting guide: https://github.com/mikeroyal/self-hosting-guide
- Local Kubernetes guide: https://www.plural.sh/blog/local-kubernetes-guide/

### Full-Stack Templates
- FastAPI + LangGraph + Next.js: https://www.youtube.com/watch?v=B3PT5_ALg94
- Full-stack AI agent template: https://github.com/vstorm-co/full-stack-ai-agent-template
- Tiangolo FastAPI template: https://github.com/tiangolo/full-stack-fastapi-template

### Supporting Tools & Libraries
- shadcn/ui: https://ui.shadcn.com/
- TanStack Query: https://tanstack.com/query
- TanStack Table: https://tanstack.com/table
- Celery: https://docs.celeryq.dev/
- OpenTelemetry Python: https://opentelemetry.io/docs/languages/python/
- LangSmith: https://smith.langchain.com/
- MinIO: https://min.io/
- Caddy: https://caddyserver.com/
- Better Auth: https://www.better-auth.com/
- Ruff: https://docs.astral.sh/ruff/
- uv: https://docs.astral.sh/uv/
- Pydantic v2: https://docs.pydantic.dev/latest/
- SQLAlchemy 2.x: https://docs.sqlalchemy.org/en/20/
- Alembic: https://alembic.sqlalchemy.org/en/latest/

### Project Management Reference Products
- Linear (UX reference): https://linear.app/
- Plane.so (OSS PM reference): https://plane.so/
- GitHub Projects: https://docs.github.com/en/issues/planning-and-tracking-with-projects

---

## Product Vision

Forge is an open-source engineering operating system built around a deterministic orchestration layer. It operates in two modes:

1. **Single-agent execution mode** (default) — one active execution agent per task run, orchestration handled by explicit state machines and workflow policies.
2. **Supervised multi-agent mode** (optional, Phase 3) — context-isolated specialist subagents bounded by explicit policy and the orchestrator.

### Core Design Principles

1. Default single-agent. Multi-agent is supervised, opt-in, and policy-controlled — never the default.
2. Human-in-the-loop by design. Review, approval, escalation, and rollback are first-class workflow states.
3. Repo-aware and policy-aware. Every run must know its repo target, allowed commands, restricted paths, completion criteria, and skill requirements before starting.
4. Spec-driven development is native. No ad hoc prompting for feature-class work.
5. Bring-your-own-model and bring-your-own-key (BYOK). No vendor lock-in.
6. Internal board first, integrations second. Native board competes with Linear while exposing adapters for Jira, Linear, Asana, Monday.com.
7. Knowledge ingestion is source-agnostic. Any MCP-compatible source ingests through the same interface.
8. Skill profiles enforce quality structurally — not via prompts.
9. Self-hosting is first-class. `docker compose up` delivers a working platform.
10. Evaluation is built in from day one. Golden test set ships with V1.

---

## Product Scope

| Module | Responsibility |
|---|---|
| Orchestrator | Workflow graph execution, retries, approvals, branching, audit logs, scheduling, mode selection |
| Execution Agent Runtime | Runs agent in isolated workspaces with scoped tools, repo context, skill profile, structured objectives |
| Multi-Agent Coordinator | Launches context-isolated specialist subagents when policy permits; merges outputs |
| Project Board | Native epics, tasks, specs, bugs, incidents, roadmaps, dependencies, automations, SLAs |
| Spec Engine | Full SDD lifecycle: Constitution → Specify → Clarify → Plan → Tasks → Implement → Validate |
| Knowledge Service | Hybrid semantic + keyword + metadata retrieval with reranking over repos and MCP sources |
| MCP Connector Layer | Discovers, authenticates, synchronizes, and audits MCP servers |
| Repo Policy Layer | Per-repo policy files, allowed commands, write rules, branch rules, reviewers |
| Integration Layer | GitHub App (V1); Jira, Linear, Asana, Monday.com, GitLab, Slack (V2) |
| Review & Approval Layer | Human gates for spec, plan, PR, deploy, and incident actions |
| Observability Layer | Run traces, token/cost logs, task lineage, retrieval debug, eval harness |
| Auth & Secrets Service | BYOK, workspace isolation, API key management, OAuth providers |

---

## Technology Stack

| Layer | Technology | Rationale |
|---|---|---|
| Frontend | Next.js, TypeScript, Tailwind CSS, shadcn/ui, TanStack Query + Table | Keyboard-first; Linear-quality UX |
| Backend API | Python, FastAPI, Pydantic v2, SQLAlchemy 2.x, Alembic | Dominant agent tooling ecosystem; async; proven templates exist |
| Workflow engine (V1) | Postgres FSM + LangGraph for agent routing | Low operational overhead for V1 |
| Workflow engine (V2) | Temporal (durable top-level) + LangGraph (agent routing) | June 2026: best production combination for AI systems |
| Agent runtime | Python async workers, LangGraph StateGraph | Best fit for coding-agent tools and repo operations |
| Multi-agent layer | LangGraph Supervisor pattern | Same framework for single-agent and multi-agent |
| Primary database | PostgreSQL | Reliable relational core |
| Vector search | pgvector (cosine similarity) | Co-locates embeddings with metadata |
| Keyword search | Postgres full-text / BM25 | Hybrid retrieval reduces RAG failure rate |
| Result fusion | RRF: score(d) = sum(1/(k + rank_i(d))), k=60 | Standard parameter-free fusion |
| Reranking | Jina Reranker v2 (self-hosted, open-weight) | 15-30% quality improvement; fully self-hostable |
| Background jobs | Redis + Celery (V1); Temporal activities (V2) | Simpler for V1; Temporal covers both in V2 |
| Real-time | WebSockets + SSE | Live task updates, run traces, approvals |
| Sandbox (V1) | Git worktrees | Fast, no infra; proven in Open SWE |
| Sandbox (V2) | Docker containers / Firecracker | Stronger isolation for multi-tenant |
| Git integration | GitHub App | Best PR/CI/review event source |
| MCP | MCP Python SDK + dedicated gateway service | 2026 RC moves to stateless HTTP |
| Object storage | MinIO (self-hosted S3) | Artifacts, logs, spec snapshots |
| Observability | OpenTelemetry + Prometheus + Grafana + Loki + LangSmith | Standard self-hostable stack |
| Auth | Better Auth / Auth.js | OSS-friendly; OAuth + API key |
| Secrets | Encrypted Postgres vault (V1), HashiCorp Vault (V2) | Required for BYOK |
| Reverse proxy | Caddy (auto HTTPS) with Nginx alternative | Simplest TLS for self-hosted |
| Python tooling | uv + Ruff | Fastest Python tooling available |

---

## Monorepo Structure

```
forge/
├── apps/
│   ├── web/                      # Next.js frontend
│   ├── api/                      # FastAPI backend
│   ├── worker/                   # Python async workers
│   └── mcp-gateway/              # MCP client manager service
├── packages/
│   ├── workflow-engine/          # Workflow DSL, state machines, Temporal activities
│   ├── agent-runtime/            # Agent execution loop, tool registry, sandboxing
│   ├── multi-agent-coordinator/  # Supervisor, subagent spawning, context scoping
│   ├── spec-engine/              # SDD lifecycle, spec artifacts, validation
│   ├── board-core/               # Task, epic, incident, project domain logic
│   ├── knowledge-core/           # Chunking, embedding, hybrid search, RRF, reranking
│   ├── integration-sdk/          # GitHub, Slack, Jira, Linear, Asana adapters
│   ├── mcp-sdk/                  # MCP client, connection model, sync modes, audit
│   ├── policy-sdk/               # Repo policy loading, permission evaluation
│   ├── skill-sdk/                # Skill profiles, workflow behavior injection
│   ├── evaluation/               # Golden test set, RAGAS metrics, quality harness
│   ├── contracts/                # Shared DTOs/schemas used across services
│   ├── db/                       # SQLAlchemy models, migrations, repositories
│   ├── auth-sdk/                 # Auth/session primitives (BYOK, OAuth, API keys)
│   ├── authz-sdk/                # RBAC/permission evaluation
│   ├── approval-sdk/             # Human approval gate primitives
│   ├── orchestration-policy/     # Workflow/orchestration policy evaluation
│   ├── deploy-core/              # Deployment/environment-promotion domain logic
│   ├── observability/            # Run traces, audit log, attestations, metrics
│   └── marketplace-sdk/          # Integration/skill-profile marketplace primitives
├── docs/
│   ├── self-hosting/             # quickstart, docker-compose, kubernetes, backup, upgrade
│   ├── architecture/             # ADRs, service boundaries, data model
│   ├── integrations/             # GitHub App setup, MCP connectors, Jira, Linear
│   └── contributing/
├── examples/
│   ├── policies/                 # .forge/policy.yaml for common repo types
│   ├── skills/                   # Skill profile examples
│   ├── workflows/                # Workflow DSL examples
│   ├── mcp-connectors/           # MCP connection configs with security notes
│   └── specs/                   # Example spec documents
├── spec-templates/               # Starter spec.md, plan.md, validation.md
└── deploy/
    ├── docker-compose.yml        # Production single-node
    ├── docker-compose.dev.yml    # Local development
    ├── docker-compose.override.yml  # Local overrides (gitignored)
    ├── .env.example
    ├── .env.production.example
    ├── helm/                     # Kubernetes Helm chart
    ├── caddy/Caddyfile
    ├── nginx/forge.conf
    └── scripts/
        ├── install.sh            # VM bootstrap
        ├── backup.sh
        └── restore.sh
```

---

## Core Data Model

```
Workspace
├── User[]                        (roles: admin, member, viewer, agent-runner)
├── APIKey[]                      (BYOK: model provider keys, integration tokens)
├── RepositoryConnection[]        (GitHub App installations)
├── MCPConnection[]               (registered MCP servers)
├── PolicyProfile[]               (reusable policy templates)
├── SkillProfile[]                (reusable skill/behavior templates)
├── KnowledgeSource[]
│   └── RetrievalChunk[]          (indexed, embedded, attributed)
└── Project
    ├── Constitution              (engineering principles, arch guardrails)
    ├── Epic[]
    │   ├── SpecDocument
    │   │   ├── requirements[]
    │   │   ├── acceptance_criteria[]
    │   │   ├── open_questions[]
    │   │   ├── plan_ref
    │   │   ├── tasks_ref
    │   │   ├── validation_ref
    │   │   └── decisions[]       (ADRs)
    │   └── Task[]
    │       ├── repo_targets[]
    │       ├── instructions_profile
    │       ├── skill_profile
    │       ├── execution_mode    (single_agent | supervised_multi_agent)
    │       ├── allowed_actions[]
    │       ├── restricted_actions[]
    │       ├── requires_approval {spec, plan, pr, deploy}
    │       ├── knowledge_scope   {repos[], mcp_sources[], source_types[]}
    │       └── subagent_policy
    ├── Incident[]
    ├── Sprint[]
    └── Milestone[]

WorkflowRun
├── task_id
├── current_state
├── execution_mode
└── AgentRun[]
    ├── inputs                    (task, spec, retrieved context, policy)
    ├── steps[]                   (tool calls, decisions, outputs)
    ├── ApprovalRequest[]
    └── SubAgentRun[]             (if multi-agent mode)
```

---

## Spec-Driven Development Engine

> GitHub Spec Kit: https://github.com/github/spec-kit
> Microsoft SDD: https://developer.microsoft.com/blog/spec-driven-development-ai-native-engineering

### SDD Lifecycle

> **Shipped reality:** the `forge` command surface in the table below
> (`forge constitution init`, `forge spec create`, etc.) is a **roadmap**
> command-line wrapper — it does not exist yet (see
> [Phased Roadmap](#phased-roadmap)). Today the lifecycle ships **API/web-first**:
> every phase is a live endpoint on the spec router
> (`apps/api/forge_api/routers/spec.py`) driven from the web UI, plus an agent
> run endpoint (`apps/api/forge_api/routers/agent.py`) for Implement. The
> "CLI Action" column names the logical action; the "Shipped As" column is
> what actually runs it today.
>
> Two SDD-adjacent console scripts do ship as real CLI entry points from
> `apps/api/pyproject.toml`: `forge-verify` (offline attestation/audit-chain
> verification) and `forge-replay` (Time-Travel Runs replay-and-diff) — see
> [trust layer](./trust-layer.md). `forge bench` and `forge marketplace` also
> exist as installed CLIs, but only their offline subcommands work standalone
> (`bench freeze/hash/verify`, `marketplace package`); their `run`/`submit`/
> `leaderboard` and `search`/`show`/`list`/`install`/`update` subcommands are
> **parked stubs** that print `PARKED ... use the HTTP API or web UI` and exit
> non-zero (`apps/api/forge_api/cli_bench.py`, `cli_marketplace.py`).

| Phase | CLI Action (roadmap) | Shipped As | Output | Gate |
|---|---|---|---|---|
| Constitution | `forge constitution init` | `POST /spec/constitution` | Engineering principles, arch guardrails | Human review |
| Specify | `forge spec create` | `POST /spec/specs` | Requirements, user journeys, acceptance criteria | Human approval |
| Clarify | `forge spec clarify` | `POST /spec/specs/{id}/clarify` | Resolved open questions | Human sign-off |
| Plan | `forge spec plan` | `POST /spec/specs/{id}/plan` | Architecture, data model, interfaces, ADRs | Human approval |
| Tasks | `forge spec tasks` | `POST /spec/specs/{id}/tasks` | Phased implementation units | Auto-generated; human editable |
| Implement | `forge run <task-id>` | `POST /agent/runs` | Code, tests, PRs | Spec-gated |
| Validate | `forge validate <task-id>` | `POST /spec/tasks/{id}/validate` | Requirement-to-test traceability | Required before merge |

### Spec Folder Layout

```
specs/
└── SPEC-17-customer-endpoint/
    ├── spec.md         # Requirements, user journeys, acceptance criteria
    ├── clarify.md      # Open questions and resolutions
    ├── plan.md         # Architecture, data model, tech decisions
    ├── tasks.md        # Phased implementation units
    ├── validation.md   # Requirement-to-test mapping
    ├── decisions.md    # ADRs
    └── manifest.yaml  # Machine-readable metadata
```

### Spec Manifest Schema

```yaml
id: SPEC-17
name: Customer endpoint improvements
status: approved   # draft | clarifying | approved | implementing | validated | closed
constitution_refs:
  - engineering/api-principles
repos:
  - github.com/org/api
requirements:
  - id: R1
    text: Add customer search endpoint with cursor-based pagination
  - id: R2
    text: Endpoint must support bearer token authentication
acceptance_criteria:
  - id: A1
    req_refs: [R1]
    text: Endpoint accepts cursor and limit params, returns next_cursor
  - id: A2
    req_refs: [R2]
    text: Requests without valid bearer token return 401
constraints:
  - Follow existing auth middleware pattern in app/middleware/auth.py
  - No breaking changes before v2 migration
plan_ref: plan.md
tasks_ref: tasks.md
validation_ref: validation.md
execution_mode: single_agent
skill_profile: backend-tdd
```

### Spec Gating Rules

- No implementation run without an approved spec for feature-class work.
- No PR merge without a validation pass mapped back to acceptance criteria.
- Agent output must cite which acceptance criteria it believes are satisfied.
- Approval UI must show requirement-to-diff and requirement-to-test traceability.

---

## Repo Policy System

Every repo should include:
- `.forge/policy.yaml` — machine-readable policy
- `AGENTS.md` — narrative instructions loaded into agent context at run time

### policy.yaml Schema

```yaml
repo_id: github.com/org/api
name: Core API Service
purpose: Backend REST API for customer operations.
languages: [python]
entrypoints:
  - app/main.py
commands:
  install: uv sync
  lint: ruff check . && ruff format --check .
  type_check: mypy app/
  test: pytest -q
  test_coverage: pytest --cov=app --cov-report=term-missing -q
  build: docker build -t api .
write_rules:
  allow: [app/**, tests/**, docs/**, alembic/versions/**]
  deny: [infra/prod/**, .env*, secrets/**, "*.pem", "*.key"]
review_rules:
  required_reviewers: [team-backend]
  approval_required_for_merge: true
  min_approvals: 1
deploy_rules:
  allow_agent_deploy: false
  environments: [dev]
  restricted_environments: [staging, production]
knowledge_rules:
  index_paths: [app/**, docs/**, specs/**]
  exclude_paths: [.venv/**, __pycache__/**, "*.pyc"]
  freshness_sla_hours: 24
skill_profiles:
  default: backend-tdd
  allowed: [backend-tdd, backend-fast, security-review, spec-analyst]
subagent_rules:
  allow_subagents: true
  allowed_roles: [reviewer, tester]
  max_parallel: 2
```

---

## Knowledge and Retrieval Architecture

> Production RAG guide: https://lushbinary.com/blog/rag-retrieval-augmented-generation-production-guide/
> Hybrid search: https://github.com/Syed007Hassan/Hybrid-Search-For-Rag

Production RAG research shows retrieval failure accounts for 73% of RAG failures. Hybrid retrieval with reranking is the solution.

### Pipeline

```
Query
 +---> Semantic Search (pgvector, cosine) ---> top-50
 +---> Keyword Search (Postgres BM25) -------> top-50
                   |
                   v
     RRF Fusion: score(d) = sum(1/(k + rank_i(d))), k=60
                   |
                   v
     Jina Reranker v2 (cross-encoder) -------> top-5 to top-10
                   |
                   v
     Context injection with source attribution
```

### Chunk Types and Priority Weights

| Source Type | Strategy | Weight |
|---|---|---|
| Markdown / doc paragraphs | Semantic paragraph splitting | 1.0x |
| Code files | Function/class-level AST | 1.0x |
| File summaries | Auto-generated at index time | 1.2x |
| README files | Always indexed, short TTL | 1.3x |
| Policy files / AGENTS.md | Always indexed, highest freshness | 1.5x |
| Spec / plan / validation | Boosted for planning queries | 1.4x |
| MCP resource snapshots | Normalized, tagged by source | 1.0x |

### Knowledge Sync Modes

| Mode | Description | Best For |
|---|---|---|
| Full sync | Re-index all chunks in scope | Initial setup |
| Incremental sync | Index changed files via git diff or webhook | Continuous freshness |
| On-demand | Index triggered before task execution | Critical freshness |
| MCP query-through | Call MCP server live at retrieval time | Always-fresh external data |
| MCP sync-and-index | Periodically pull MCP resources into local index | Fast hybrid search |

---

## MCP Integration

> Spec: https://modelcontextprotocol.io/specification/2025-11-25
> 2026 RC: https://blog.modelcontextprotocol.io/posts/2026-07-28-release-candidate/
> Security: https://media.defense.gov/2026/Jun/02/2003943289/-1/-1/0/CSI_MCP_SECURITY.PDF

### MCP Connection Schema

```yaml
mcp_connection:
  id: confluence-engineering
  name: Engineering Confluence
  transport: http              # http (2026 RC preferred) | stdio | sse (legacy)
  endpoint: https://mcp.company.internal/confluence
  auth:
    type: oauth                # oauth | api_key | none
  capabilities:
    resources: true
    tools: true
    prompts: false
  sync_mode: incremental
  index_strategy: sync_and_index  # sync_and_index | query_through
  freshness_sla_minutes: 30
  allow_write: false           # MUST default to false
  allowed_namespaces: [engineering, architecture]
```

### MCP Security Rules

1. All connections default to `allow_write: false` — enabling write requires explicit admin action.
2. Tokens bound to specific servers using RFC 8707 `resource` parameter (per June 2025 spec).
3. All inputs validated before tool execution.
4. Full audit log: tool name, payload hash, result status, latency.
5. Per-connection namespace scoping.
6. Secrets redacted from logs, traces, and retrieval results.
7. MCP tool invocations require the same policy evaluation as any other agent tool call.

---

## Workflow Engine

> Temporal vs LangGraph: https://suhasbhairav.com/blog/temporal-vs-langgraph-durable-workflow-orchestration-vs-llm-agent-state-machines

### Architecture

| Layer | Engine | Why |
|---|---|---|
| Top-level task workflows | Temporal (V2) / Postgres FSM (V1) | Durable execution, guaranteed completion |
| Agent-level routing | LangGraph StateGraph | Conditional edges, agentic decisions, HITL interrupts |

### Default Feature Workflow States

```
created -> spec_drafting -> clarification -> spec_review -> spec_approved
-> plan_drafting -> plan_review -> task_generation -> task_ready
-> executing -> verifying -> pr_opened -> awaiting_review -> merged -> closed

Error paths: -> needs_human_input | -> failed | -> cancelled
```

### Incident Workflow States

```
alert_received -> incident_created -> context_gathering -> impact_assessed
-> remediation_proposed -> awaiting_approval -> executing_runbook
-> monitoring -> resolved -> postmortem_created
```

### Workflow DSL

```yaml
workflow: default_feature
version: "1"
modes:
  default: single_agent
  optional: [supervised_multi_agent]

transitions:
  - from: created
    to: spec_drafting
    action: generate_spec_draft
    skill: spec-analyst

  - from: spec_review
    to: spec_approved
    when: spec_approved_by_human
    record: approval_event

  - from: task_ready
    to: executing
    action: start_agent_run
    preconditions: [repo_target_set, policy_loaded, skill_profile_set, knowledge_synced]

  - from: executing
    to: verifying
    action: run_checks
    checks: [lint, type_check, tests, coverage]

  - from: verifying
    to: pr_opened
    when: all_checks_passed
    action: open_pr_with_spec_traceability

  - from: verifying
    to: executing
    when: checks_failed
    condition: retry_budget_remaining

  - from: verifying
    to: needs_human_input
    when: checks_failed
    condition: retry_budget_exhausted

  - from: awaiting_review
    to: merged
    when: [review_approved_by_human, ci_status_green, spec_validated]

retry_policy:
  max_retries: 3
  backoff: exponential
  initial_delay_seconds: 30

escalation_policy:
  confidence_threshold: 0.72
  on_low_confidence: pause_and_notify
  on_policy_conflict: escalate_to_admin
```

---

## Multi-Agent Orchestration

> 6 production patterns: https://beam.ai/agentic-insights/multi-agent-orchestration-patterns-production

Multi-agent mode is always opt-in and policy-controlled. The default is single-agent.

### Pattern Selection Guide

| Situation | Pattern |
|---|---|
| Default feature task | Single agent (Orchestrator-Worker) |
| Parallel independent subtasks | Fan-out / Fan-in |
| Quality validation needed | Maker-Checker (Implementer + Reviewer) |
| Research + implement + test + review | Sequential specialist pipeline |
| Complex multi-domain task | Dynamic handoff with Supervisor |

### Subagent Role Definitions

| Role | Responsibilities | Scoped Tools Only |
|---|---|---|
| planner | Spec drafts, plans, task breakdowns | read_repo, read_spec, read_knowledge, write_spec |
| researcher | Knowledge retrieval, context summary | read_repo, search_knowledge, query_mcp |
| implementer | Code writing, test writing, PR creation | read_repo, write_code, run_tests, open_pr |
| tester | Test suite writing, coverage validation | read_repo, write_tests, run_tests |
| reviewer | Code review, spec adherence | read_repo, read_spec, write_review_comment |
| security | SAST scan, dependency audit, secrets detection | read_repo, run_sast, audit_dependencies |

### Multi-Agent Rules

- Subagents receive scoped tools only — implementer cannot query MCP; researcher cannot write code.
- Each subagent receives scoped context only — isolated from other subagents' state.
- Orchestrator merges outputs into structured artifacts, not free-form chat.
- Human approval still gates all risky actions and merges.
- Final acceptance validates against approved spec, not subagent agreement.
- Supervisor makes routing decisions via explicit policy, not LLM judgement.

---

## Skill Profiles

> Superpowers: https://www.termdock.com/en/blog/superpowers-framework-agent-skills

```yaml
skill_profiles:

  backend-tdd:
    description: Backend feature development with test-driven discipline
    requires_plan: true
    requires_tests_before_implementation: true
    min_test_coverage: 80
    verification_steps: [lint, type_check, unit_tests, integration_tests]
    review_required: true
    forbidden_shortcuts: [skip_tests, no_error_handling, hardcoded_secrets]

  backend-fast:
    requires_plan: false
    min_test_coverage: 60
    verification_steps: [lint, type_check, unit_tests]
    review_required: true

  frontend-ui:
    requires_plan: true
    verification_steps: [lint, type_check, unit_tests]
    accessibility_check: true
    review_required: true

  incident-response:
    requires_plan: false
    requires_human_approval_before_action: true
    max_blast_radius: low
    allowed_actions: [read_logs, query_metrics, read_repo, run_diagnostic_scripts]
    forbidden_actions: [deploy_prod, delete_data, modify_access_controls]

  spec-analyst:
    output_type: spec_document
    human_review_required: true
    allowed_actions: [read_repo, read_knowledge, write_spec, query_mcp]

  security-review:
    output_type: security_report
    tools: [sast, dependency_audit, secrets_scan]
    human_review_required: true
    report_format: sarif

  chore-fast:
    requires_plan: false
    verification_steps: [lint, type_check, unit_tests]
    review_required: false
```

---

## Task Schema

```yaml
id: TASK-123
project_id: proj_core
epic_id: EPIC-17
spec_id: SPEC-17
kind: feature      # feature | bug | chore | spike | incident | change_request | doc
title: "Add customer search endpoint with pagination"
status: ready_for_agent
priority: high
estimate: 3

execution_mode: single_agent
repo_targets:
  - repo: github.com/org/api
    branch_strategy: task_branch
    branch_prefix: forge/TASK-123
    base_branch: main
    worktree: true

instructions_profile: backend-default
skill_profile: backend-tdd

acceptance_criteria:
  - id: A1
    spec_ref: SPEC-17/A1
    text: Endpoint supports cursor-based pagination
  - id: A2
    spec_ref: SPEC-17/A2
    text: Requests without valid bearer token return 401

allowed_actions: [read_repo, write_code, run_tests, open_pr, read_knowledge, query_mcp]
restricted_actions: [deploy_prod, delete_files, push_to_main, modify_access_controls]

requires_approval:
  spec: true
  plan: false
  pr: true
  deploy: true

knowledge_scope:
  repos: [github.com/org/api]
  mcp_sources: [confluence-engineering]
  source_types: [repo, spec, mcp_resource]
  freshness_min_hours: 12

subagent_policy:
  allowed: false
  max_parallel: 0

handoff_rules:
  confidence_below: 0.72
  on_test_failure_after_retries: 2
  on_policy_conflict: escalate
  on_missing_spec_approval: block
```

---

## Human Approval System

### Approval Gate Types

| Gate | Trigger | Default |
|---|---|---|
| Spec approval | Spec moves to approved | Required for feature-class work |
| Plan approval | Plan generated, policy requires | Optional, per task policy |
| PR approval | PR opened by agent | Always required before merge |
| Deploy approval | Agent requests env promotion | Required unless policy relaxes for dev |
| Incident remediation | High-risk runbook step | Required for blast-radius > low |
| Policy override | Agent requests out-of-policy action | Always required |

### Approval UI Must Show

1. Task goal and spec requirements being addressed
2. Changed files with diff preview and syntax highlighting
3. Verification results: lint, type check, tests, coverage
4. Spec traceability: which acceptance criteria are satisfied and how
5. Knowledge provenance: which retrieved chunks informed the implementation
6. Confidence score with rationale
7. Risks flagged: policy warnings, security findings, uncertain areas
8. Full run trace with step-by-step actions taken
9. Actions: Approve / Reject / Request changes

---

## Native Project Board

The board is a first-class product. Keyboard-first, fast, opinionated — Linear quality, not generic admin panel.

### Entity Hierarchy

```
Workspace
└── Project
    ├── Epic (groups tasks; links to SpecDocument)
    │   └── Task (atomic unit of work)
    │       └── SubTask (optional decomposition)
    ├── Incident
    ├── Sprint (optional time-boxed container)
    └── Milestone (deadline anchor)
```

### Board Features

| Feature | Description |
|---|---|
| Command palette | Cmd+K for all actions: create, status change, assign, search, navigate |
| Views | List, Board (kanban), Roadmap (timeline), Backlog, My Tasks |
| Custom statuses | Per-project with workflow policy rules |
| Task fields | Priority, estimate, labels, team, SLA, due date, spec link, repo target, skill profile |
| Dependencies | Blocks/blocked-by graph with cycle detection |
| Unified timeline | Comments, run events, PRs, approvals, spec decisions — all in one place |
| Saved filters | Per-user and per-team filter sets |
| Keyboard shortcuts | Full keyboard navigation, no mouse required |
| Automations | Rule-based: "when status = merged -> close linked spec task" |
| Bulk actions | Multi-select for status, assignment, labeling |
| Sprint management | Create sprints, move tasks, track velocity |

### UX Standards

- Keyboard-first: every action accessible without mouse
- Optimistic updates: state changes appear instantly, rollback on error
- No full-page reloads: all navigation client-side
- Sub-100ms interactions: list renders, status changes, filter updates
- Empty states guide users to first action

### External PM Adapter Contract

```python
class PMAdapter(Protocol):
    def sync_in(self, external_task: ExternalTask) -> ForgeTask: ...
    def sync_out(self, forge_task: ForgeTask) -> ExternalTask: ...
    def subscribe(self, webhook_event: WebhookEvent) -> None: ...
    def map_status(self, external: str, direction: Direction) -> str: ...
    def map_priority(self, external: str, direction: Direction) -> str: ...
    def map_fields(self, external: dict, direction: Direction) -> dict: ...
    def get_connection_health(self) -> HealthResult: ...
```

V2 targets: Jira, Linear, Asana, Monday.com

---

## Self-Hosting and Deployment

> Docker Compose production 2026: https://distr.sh/blog/running-docker-in-production/
> Reference: https://langfuse.com/self-hosting/deployment/docker-compose

Forge is self-hosting-first. Full feature parity for self-hosted deployments.

### Three Install Paths

| Mode | Target | Infrastructure | Complexity |
|---|---|---|---|
| Local dev | 1-2 (contributors, evaluators) | Docker Desktop | Low |
| Docker Compose | 1-50 engineers | 1-2 VMs, Docker, Caddy/Nginx | Medium |
| Kubernetes | 50+ engineers | K8s cluster, ingress, managed DB | High |

### Local Quickstart

```bash
git clone https://github.com/QuintinBotes/forge
cd forge
cp .env.example .env
make setup    # installs deps, runs migrations, seeds demo workspace
make dev      # starts all services
# Web UI: http://localhost:3000 | API: http://localhost:8000
```

### Docker Compose Production

Target: Ubuntu 24.04 LTS — minimum 4 vCPU / 8 GB RAM / 50 GB SSD

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER && newgrp docker

git clone https://github.com/QuintinBotes/forge
cd forge
cp .env.production.example .env.production
# Fill in: SECRET_KEY, DB_PASSWORD, GITHUB_APP_*, MODEL_PROVIDER_KEY, DOMAIN

docker compose -f docker-compose.yml --env-file .env.production up -d
docker compose exec api forge-cli db migrate
docker compose exec api forge-cli users create-admin
```

### docker-compose.yml Service List

```yaml
services:
  db:           # PostgreSQL with pgvector extension
  redis:        # Queue, cache, sessions
  minio:        # Object storage
  api:          # FastAPI backend
  worker:       # Celery workers (agent runtime, indexer, syncer)
  mcp-gateway:  # MCP client manager
  web:          # Next.js frontend
  caddy:        # Reverse proxy with automatic HTTPS
  autoheal:     # Docker autoheal sidecar
  # Optional (V2):
  temporal:     # Temporal workflow server
  prometheus:   # Metrics
  grafana:      # Dashboards
  loki:         # Logs
```

### Production Docker Compose Requirements

Based on 2026 best practices (https://distr.sh/blog/running-docker-in-production/):

- Pin all images by @sha256 digest, not tag
- Run willfarrell/docker-autoheal sidecar; label all services autoheal=true
- Named volumes for Postgres, Redis, MinIO — never bind mounts for production data
- CPU and memory resource limits on all containers
- Healthcheck endpoints on all services
- Network segmentation: separate networks for API, database, MCP gateway, observability
- Run all containers as non-root users
- Cap container log size in daemon.json (max-size: 100m, max-file: 5)
- Pass --remove-orphans on every compose up

### Required Self-Hosting Documentation (must ship at launch)

docs/self-hosting/ must include:
- quickstart.md (local setup under 10 minutes)
- docker-compose.md (production single-node guide)
- kubernetes.md (Helm chart install and values)
- reverse-proxy.md (Caddy and Nginx config examples)
- backup.md (Postgres + MinIO + secrets procedures)
- restore.md (full restore with verification)
- upgrade.md (safe upgrade with rollback instructions)
- security.md (hardening, credential rotation, network policies)
- troubleshooting.md (common errors and fixes)

**Shipped:** all nine files above exist in `docs/self-hosting/`.
`reverse-proxy.md` documents both Caddy and the nginx alternative
(`deploy/nginx/forge.conf`) side by side.

---

## Integrations

### V1 (Launch)

| Integration | Scope |
|---|---|
| GitHub App | Repo sync, PRs, CI, reviews, webhooks |
| Slack | Task status, approval requests, `/forge` slash commands |
| Email | Approval requests, @mentions, digest |
| OAuth | Google, GitHub, GitLab sign-in |
| MCP servers | Any MCP-compatible knowledge source |

### V2

Jira, Linear, Asana, Monday.com (PM sync bidirectional), GitLab, Datadog, Sentry, PagerDuty, Grafana

---

## Observability and Evaluation

### Key Metrics

Workflow quality: spec completeness score, task completion rate, human approval accept/reject rate, PR acceptance rate, mean time to merge, mean time to task completion.

Agent quality: retry rate, failure rate, confidence score distribution, spec requirement satisfaction rate.

Retrieval quality: hybrid search hit rate, reranker delta, MCP freshness lag, retrieval latency p50/p95/p99.

Cost: token cost per task, per workflow phase, per model provider.

### Evaluation Harness

- Golden test set: 30-100 representative task inputs with known-good outputs — built before any framework complexity is added (per LangGraph 2026 production guidance)
- Every release runs against the golden test set; regressions block merge
- Requirement-to-test traceability reports from Spec Engine
- Replayable workflow runs with step-level inspection
- A/B evaluation for retrieval strategies and model providers

---

## Security

| Area | Requirement |
|---|---|
| Secrets | Encrypted at rest, per-workspace isolation, automatic expiry for agent tokens |
| RBAC | admin, member, viewer, agent-runner roles per workspace and per project |
| Audit log | Every agent action, tool call, MCP call, and approval — immutable, queryable |
| Policy evaluation | Every tool invocation checked against repo policy before execution |
| Secret redaction | Secrets stripped from logs, traces, and retrieval results |
| MCP security | Read-only by default, token binding per RFC 8707, namespace scoping |
| Sandbox isolation | Git worktrees (V1), Docker containers (V2) — no cross-task filesystem access |
| Auth | OAuth + API key; all routes authenticated; no anonymous API access |
| Rate limiting | Per-workspace, per-user, per-model-provider |

---

## OSS Strategy

**License**: Apache-2.0 — maximum adoption, commercial-friendly.

### Community Artifacts (ship at launch)

- Example .forge/policy.yaml for 5 common repo types (Python API, TypeScript frontend, Go service, infrastructure, docs)
- Example skill profiles for common engineering disciplines
- Example MCP connector templates with security notes
- Example workflow DSL configs (feature, bugfix, incident, chore)
- Spec templates for common feature types
- ADR format and examples
- Contribution guide and PR template

### Extension Points

- PMAdapter interface: add any external board integration
- Skill profiles: plain YAML — community contributions welcome
- Workflow DSL: declarative — custom workflows without code changes
- MCP gateway: accepts any MCP-compatible server
- Tool registry: pluggable — custom tools per workspace

---

## Phased Roadmap

### Phase 1 — Foundation (V1)

- [ ] Native project board (epics, tasks, views, roadmap, command palette)
- [ ] Spec engine (constitution, specify, clarify, plan, tasks, validate)
- [ ] GitHub App integration (repo sync, PRs, reviews, CI webhooks)
- [ ] Repo policy system (.forge/policy.yaml + AGENTS.md loading)
- [ ] Knowledge sync: repos -> hybrid pgvector + BM25 + Jina Reranker v2
- [ ] Single execution agent with LangGraph routing
- [ ] Default feature workflow state machine (Postgres-backed)
- [ ] Plan -> execute -> verify -> PR -> approval flow
- [ ] MCP gateway V1: read-only query-through mode
- [ ] Run trace viewer with step-level inspection
- [ ] Skill profiles: backend-tdd, frontend-ui, incident-response, spec-analyst
- [ ] Golden test set and evaluation harness (30+ tasks)
- [ ] Local quickstart (under 10 minutes)
- [ ] Docker Compose self-hosted with production best practices
- [ ] Full docs/self-hosting/ documentation
- [ ] Slack notifications for approvals and task status

### Phase 2 — Depth (V2)

- [ ] Incident workflows and postmortem task generation
- [ ] External PM adapters (Jira, Linear)
- [ ] Container sandboxing (Docker-based per-task isolation)
- [ ] MCP sync-and-index mode
- [ ] Saved workflow automations (rule engine)
- [ ] Multi-repo task execution
- [ ] Spec validation dashboard with requirement traceability
- [ ] Kubernetes Helm chart
- [ ] Temporal workflow engine integration
- [ ] Sprint management and velocity dashboards
- [ ] Full `forge` CLI (`forge constitution init`, `forge spec create/clarify/plan/tasks`, `forge run`, `forge validate`) as a local command-line wrapper over the already-shipped API/web SDD lifecycle (see [SDD Lifecycle](#sdd-lifecycle))

### Phase 3 — Scale (V3)

- [ ] Supervised multi-agent mode (Supervisor pattern, opt-in)
- [ ] Workflow visual editor
- [ ] Advanced policy engine with conditional rules
- [ ] Multi-team workspace controls and full RBAC hierarchy
- [ ] Deployment gates and environment promotion workflows
- [x] Integration marketplace for community MCP connectors and skill profiles — **shipped early** (F32; `packages/marketplace-sdk/`, `apps/web` marketplace screens), in-app publish flow (PR #57); `forge marketplace search/show/list/install/update` remain parked CLI stubs pending a live registry
- [x] Enterprise SSO (SAML, SCIM) — **shipped early** (F33; `apps/api/forge_api/sso/`, `routers/saml.py`, `routers/scim.py`), plus OIDC (PR #57; `routers/oidc.py`)
- [x] Firecracker / gVisor sandbox isolation — **shipped early** (F34; `packages/agent-runtime/forge_agent/sandbox/microvm.py`, `sandbox/gvisor.py`, Helm `runtimeclass-kata-fc`/`runtimeclass-gvisor` templates)
- [x] Benchmark suite and public evaluation leaderboard — **shipped early** (PR #57/F35; `apps/web` leaderboard screen, benchmarks API); `forge bench run/submit/leaderboard` remain parked CLI stubs

### Trust Layer — Shipped Ahead of Roadmap

Not scoped in any phase above when this spec was first written; shipped as
the platform's category-defining differentiator (PRs #61-#64). See
[docs/trust-layer.md](./trust-layer.md) for the full write-up.

- [x] Attested Changesets — DSSE-signed in-toto provenance over every changeset (PR #61)
- [x] Time-Travel Runs — deterministic record-replay (and counterfactual fork) of an agent run (PR #62)
- [x] Red-Team Gate — a heterogeneous adversary must fail to break the change in-sandbox before the human gate (PR #63)
- [x] Self-Eval Gate — blocks a config change that regresses a workspace's private per-repo evaluation suite (PR #64)

---

## Build Prompt for Claude Code

Use the following as the system prompt when bootstrapping implementation:

```
You are the lead architect and principal engineer for Forge, a production-ready
open-source engineering orchestration platform.

Forge combines the best patterns from:
- Symphony (task-as-control-plane): https://openai.com/index/open-source-codex-orchestration-symphony/
- Open SWE (repo-aware coding agent): https://github.com/langchain-ai/open-swe
- GitHub Spec Kit (spec-driven development): https://github.com/github/spec-kit
- MCP (knowledge connectivity): https://modelcontextprotocol.io/specification/2025-11-25
- Superpowers (skill profiles): https://mcpmarket.com/server/superpowers
- Production multi-agent patterns: https://beam.ai/agentic-insights/multi-agent-orchestration-patterns-production

Stack:
- Backend: Python, FastAPI, Pydantic v2, SQLAlchemy 2.x, Alembic, uv, Ruff
- Frontend: Next.js, TypeScript, Tailwind CSS, shadcn/ui, TanStack Query + Table
- Workflow: Postgres FSM (V1) -> Temporal (V2)
  See: https://suhasbhairav.com/blog/temporal-vs-langgraph-durable-workflow-orchestration-vs-llm-agent-state-machines
- Agent routing: LangGraph StateGraph always
  See: https://langchain-ai.github.io/langgraph/
- Retrieval: pgvector + Postgres BM25 + RRF + Jina Reranker v2
  See: https://lushbinary.com/blog/rag-retrieval-augmented-generation-production-guide/
- Default: single execution agent per workflow run
- Multi-agent: supervised, policy-controlled, opt-in ONLY (Phase 3)
- MCP: stateless HTTP gateway, read-only by default, OAuth token binding RFC 8707
  See: https://blog.modelcontextprotocol.io/posts/2026-07-28-release-candidate/
- Deployment: Docker Compose (V1), Kubernetes Helm (V2)
  See: https://distr.sh/blog/running-docker-in-production/

Non-negotiable constraints:
1. Every task must know its repo target, policy profile, and skill profile BEFORE execution.
2. The agent never self-assigns permissions or expands its own scope.
3. Retrieval is always hybrid (semantic + keyword + metadata + reranking).
4. Spec approval is required before implementation for feature-class tasks.
5. Human approval is required before PR merge — always.
6. The board is keyboard-first and fast — Linear quality, not admin panel.
7. Self-hosting works with a single docker compose up from day one.
8. MCP connections are read-only by default with least-privilege token binding.
9. An audit log exists for every agent action, tool call, and MCP call.
10. A golden evaluation set of 30+ tasks must exist before framework complexity is added.

Before writing any code:
1. Read all research links in the FORGE_SPEC.md Research Links section.
2. Define all service boundaries and data models first.
3. Build the golden evaluation set (30+ representative tasks with known-good outputs).
4. Start with the simplest working version of each component.
5. Write tests first — the backend-tdd skill profile applies to Forge itself.

Quality bar: a serious engineering team must be able to adopt, self-host, extend,
audit, and fully trust this system in production.
```

---

*Forge — built on Symphony, Open SWE, GitHub Spec Kit, MCP, Superpowers, and 2026 production agent research.*
*Apache-2.0 open-source. Self-hosting first. Bring your own model.*
