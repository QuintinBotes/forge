# Forge: Research Foundation for the OSS Engineering Orchestration Platform

## Executive Summary

This report synthesises the research behind every major technical decision in the Forge platform specification. It covers state-machine orchestration, hybrid retrieval, MCP, spec-driven development, multi-agent patterns, self-hosting, and the technology stack. Every recommendation traces to a real source rather than informed assumption.

---

## Naming

After surveying the existing OSS ecosystem and the name space of developer tools, the recommended product name is **Forge**.

Rationale:
- A forge is where raw material is shaped into something precise and durable — an accurate metaphor for turning a spec or a task into production-quality code.
- The word is short, memorable, distinctive, and domain-appropriate for developer tooling.
- It connotes craftsmanship and control, which fits a platform explicitly designed around policy, approvals, and structured workflows rather than unconstrained automation.
- No major developer infrastructure tool currently occupies this exact name in the agent orchestration space.
- It works as a CLI prefix (`forge run`, `forge spec`, `forge plan`, `forge task`), a GitHub org, a docs domain, and a product name simultaneously.
- "Forge" scales from a solo developer's self-hosted stack to an enterprise engineering control plane without sounding either too small or too enterprise.

Alternative considered names and why they are weaker:

| Name | Issue |
|---|---|
| Conductor | Multiple existing tools share this name.[cite:44] |
| Baton | Already an existing OSS orchestrator.[cite:26] |
| Architect | Too generic; overused in enterprise software. |
| Nexus | Heavily associated with Sonatype Nexus artifact repository. |
| Conduit | Exists in the streaming/connector space. |
| Weaver | No strong engineering-native connotation. |
| Scaffold | Exists in many frameworks; implies temporary structure. |
| **Forge** | Short, distinctive, domain-perfect, no conflicts found in this exact niche. |

---

## Workflow Engine: LangGraph, Temporal, or Hybrid

### What the evidence says

The most important architectural decision is what underpins workflow execution. The choice is between LangGraph, Temporal, or a hybrid of both — and the evidence is clear that the right answer depends on what you are orchestrating.[cite:96]

LangGraph models agents as directed state graphs with conditional edges and durable checkpointing.[cite:91][cite:92] It is purpose-built for LLM-agent workflows and excels at low-latency, graph-based routing, agentic decision-making, and human-in-the-loop interrupts where the workflow waits for a human response and resumes from the checkpoint.[cite:93][cite:95] Its 2026 production guide recommends shipping the smallest useful agent loop first, building a golden evaluation set, and using StateGraph with checkpointers.[cite:93]

Temporal is a general-purpose durable execution engine.[cite:96][cite:102] It guarantees that workflow code will complete even across failures, restarts, and infrastructure events, and it has been battle-tested in mission-critical systems where reliability guarantees matter over everything else.[cite:96][cite:105] A practical analysis published in June 2026 concludes that the best production approach is to combine both: use Temporal for core durable orchestration of long-running task workflows and use LangGraph for agent-level routing, context enrichment, and RAG-enabled decision-making.[cite:96]

For Forge, this hybrid approach is the right call. The top-level workflow engine — the one that manages task state through Created → Executing → Verifying → Approved → Merged — should use Temporal-style durable execution because tasks can run for hours or days and must not be lost on restart.[cite:96][cite:102] The agent runtime layer — the one that manages how the agent plans, retrieves context, selects tools, and decides when to request clarification — can use LangGraph-style graph routing, which is optimised for the iterative, context-sensitive patterns agents use.[cite:93][cite:96]

### Why this matters for buildability

An important practical finding from 2026 LangGraph production guides is that teams should start with the simplest useful version, build a golden eval set of 30–100 representative inputs, and only add framework complexity once the baseline is proven.[cite:93] This strongly supports the Phase 1 approach of a single-agent loop before supervised multi-agent orchestration.

---

## Hybrid Retrieval: Code-Aware RAG

### What the evidence says

The most important finding from 2026 RAG production research is that retrieval failure accounts for 73% of RAG failures — not generation.[cite:100] Pure semantic search misses exact identifiers, symbol names, and error strings that are critical in codebase retrieval. Pure keyword search (BM25) misses semantic similarity for natural language queries about patterns and architecture.[cite:100]

The production-proven pattern is hybrid retrieval: run BM25 and vector search in parallel, fuse results using Reciprocal Rank Fusion (RRF), and then add a reranking step.[cite:100][cite:103] The formula for RRF is: `score(doc) = Σ 1 / (k + rank_i(doc))` where `k=60` is the standard constant.[cite:100]

After initial hybrid retrieval of top-50 candidates, a cross-encoder reranker model re-scores each result with full attention, typically improving answer quality by 15–30% on RAGAS metrics.[cite:100] Self-hosted open-weight rerankers suitable for code include Jina Reranker v2 (400ms latency) and ColBERT v2 (token-level late interaction, fastest for large candidate sets).[cite:100]

For Forge's knowledge service, pgvector is the right choice for V1 because it co-locates embeddings with Postgres metadata, supports hybrid BM25+vector search patterns, and is recommended for under 1 million vectors.[cite:100][cite:97] One practical 2025 Postgres implementation combined vector search (weighted 5×), BM25 keyword search (weighted 3×), and recency scoring (weighted 0.2×) into a unified hybrid score.[cite:103]

---

## Model Context Protocol

### What the protocol actually specifies

MCP is an open-source standard introduced by Anthropic in November 2024 that standardises connections between AI applications and external data sources, tools, and services.[cite:108][cite:53] It uses JSON-RPC 2.0 for message format with stateful connections and server-client capability negotiation.[cite:106] Servers expose three feature types: Resources (context and data), Prompts (templated messages), and Tools (functions the model can call).[cite:106]

The most recent specification update (2025-06-18) added structured JSON tool output, OAuth 2.0 Resource Server classification, server-side elicitation of user input, and explicit security considerations for PKCE, token binding, and confused deputy protection.[cite:110] A release candidate for a 2026 revision is now locked as of May 2026, with the final specification shipping July 28, 2026; the major change is that MCP becomes stateless at the protocol layer, enabling deployment on ordinary HTTP infrastructure while supporting extensions for server-rendered UIs and long-running tasks.[cite:107]

For Forge, MCP is the right abstraction for the knowledge connector layer because it eliminates per-source integration code: any source that exposes an MCP server — Confluence, Notion, Postgres, GitHub, Slack, internal APIs, file stores — can be registered and queried through the same interface.[cite:108][cite:60] The 2026 revision moving to stateless HTTP will make MCP connectors easier to scale and maintain.[cite:107]

### Security considerations

A 2026 security advisory from the Department of Defense explicitly addresses MCP security design considerations and classifies the MCP client library as the middleware layer that translates LLM-generated requests into protocol messages.[cite:115] The advisory and the June 2025 spec both recommend that MCP connections be scoped with least-privilege access, that tokens be bound to specific servers, and that all inputs be validated before tool execution.[cite:110][cite:115] Forge must enforce read-only defaults, per-source permission profiles, and audit logging on all MCP calls.

---

## Spec-Driven Development

### What the evidence says

Spec-Driven Development (SDD) is a methodology published in a January 2026 arXiv paper and operationalised in open-source toolkits including GitHub Spec Kit.[cite:63][cite:65] The core idea is that AI agents need unambiguous, executable contracts to generate reliable code: by providing clear specifications, constraints, and acceptance criteria up front, SDD enhances agent reliability and enables scalable, reproducible delivery.[cite:63]

GitHub Spec Kit, open-sourced in September 2025, implements SDD in four sequential phases: Specify (what and why, user journeys, success criteria), Plan (technical stack, architecture, constraints), Tasks (break spec+plan into small reviewable units), and Implement (agent executes tasks against the spec).[cite:65][cite:111] The key design insight is that each phase has a specific job and teams do not move forward until the current phase is validated, which is a form of quality gate on AI-assisted work.[cite:65]

Microsoft's developer blog on SDD describes a constitution-first approach where teams define guardrails, constraints, and architectural principles before any spec work begins.[cite:61] This constitution becomes the grounding layer that keeps all downstream specs and plans aligned with organizational standards.[cite:61]

Forge should implement the full SDD lifecycle natively: Constitution → Specify → Clarify → Plan → Tasks → Implement → Validate. This is more structured than most existing coding-agent systems because it requires human review at both the spec-approval and plan-approval stages before any agent writes code.[cite:61][cite:65]

---

## Multi-Agent Orchestration

### What the evidence says

The 2026 landscape for multi-agent patterns is well-documented.[cite:112][cite:114][cite:117][cite:131] The most important finding for Forge is that production teams consistently over-architect multi-agent systems and that the simplest pattern that fits the problem should be used.[cite:114]

Six production patterns are documented for 2026: Orchestrator-Worker (single accountability point, known task decomposition), Sequential Pipeline (linear deterministic steps), Fan-out/Fan-in (parallel independent tasks), Multi-agent debate (maker-checker loops for quality), Dynamic handoff (unknown routing until runtime), and Adaptive planning (plan discovery, not plan execution).[cite:114] For Forge's default, Orchestrator-Worker maps directly to a single primary agent receiving structured task context from the orchestrator, which is the right pattern for coding tasks with known subtasks.[cite:114]

The Supervisor pattern in multi-agent systems uses a hierarchical architecture where a central orchestrator coordinates all agent interactions.[cite:112] The key design principle for the supervisor is to give it explicit decision-making criteria rather than expecting it to infer coordination strategy from general prompts.[cite:117] Forge's multi-agent coordinator should be a deterministic supervisor, not a prompted reasoning agent — it should make routing, assignment, and context-passing decisions based on explicit policy, not LLM judgement.

The framework landscape for 2026 shows LangGraph as the leader for graph-based stateful multi-agent orchestration, CrewAI for role-based prototypes, OpenAI Agents SDK for explicit handoffs, and Google ADK for hierarchical agent trees.[cite:131][cite:17] LangGraph's directed graph model with conditional edges is the best match for Forge because it supports both the default single-agent mode and optional supervised multi-agent mode within the same framework, which minimises the architectural complexity of supporting both.[cite:131]

---

## Self-Hosting and Deployment

### What the evidence says

Docker Compose is a viable production deployment method for small teams in 2026 when paired with appropriate operational practices.[cite:120] A detailed 2026 analysis confirms that Compose works for real production workloads provided teams add an autoheal sidecar for unhealthy containers, pin image digests rather than tags, set resource limits, use named volumes for persistence, segment networks, and treat Watchtower or similar tools as necessary companions for update management.[cite:120] For Forge, the recommended Docker Compose production configuration should include all of these patterns by default.

Docker Compose limitations that matter for Forge's design include the absence of native auto-scaling, high availability, and cross-host container restart.[cite:121] For teams that need these properties — typically larger engineering organizations — Kubernetes is the correct escalation path.[cite:120][cite:121] Forge should document this explicitly: Compose handles single-node deployments for small teams; Kubernetes is for teams that need HA, scaling, or managed cloud infrastructure.[cite:120]

Temporal provides a self-hosted service guide and a local development server, which makes it appropriate as the optional workflow backbone for teams who choose it.[cite:87] For teams that do not want to operate Temporal, a lightweight alternative workflow state machine backed by Postgres is a valid V1 approach that avoids Temporal's operational overhead during early adoption.

Full-stack AI agent templates combining FastAPI, LangGraph, Next.js, and Docker are available as open-source starting points and demonstrate the stack's viability for production deployments.[cite:130][cite:133] One template ships with Docker Compose for local development and a production preset that includes Redis, rate limiting, Sentry, Prometheus, and Kubernetes manifests.[cite:133]

---

## Symphony and Open SWE: What to Borrow

### Symphony

Symphony, open-sourced by OpenAI in April 2026, is not a complete software system — it is a SPEC.md file that describes an orchestration pattern every organization can use to build their own orchestrator.[cite:126] Its core contribution is the idea that a project management issue tracker becomes the control plane for coding agents: tasks are assigned to agents, agents work in dedicated workspaces, the orchestrator monitors for stalls, and workflow files define how work moves through statuses.[cite:34][cite:126] Forge should borrow Symphony's task-as-control-plane model and its workflow-file concept, but it needs to build the actual software infrastructure that Symphony intentionally leaves unspecified.[cite:126]

### Open SWE

Open SWE, built on LangGraph and launched by LangChain in 2026, demonstrates production-ready internal coding agent architecture: isolated sandboxes per task, AGENTS.md-based repo context loading, a curated set of tools, Slack and Linear integration, and PR-opening workflows.[cite:35][cite:52][cite:55] The architectural pattern of loading repo instructions before execution and providing structured task context rather than free-form prompting is directly applicable to Forge.[cite:35]

---

## Technology Recommendations: Detailed Justification

| Decision | Recommendation | Evidence |
|---|---|---|
| Backend framework | FastAPI + Python | Strong agent tooling ecosystem, async support, Pydantic for structured data, and proven full-stack AI agent templates.[cite:125][cite:130] |
| Frontend framework | Next.js + TypeScript + Tailwind + shadcn/ui | Delivers fast keyboard-first UIs; well-matched to LangGraph streaming output.[cite:125][cite:128] |
| Workflow orchestration | Hybrid: Temporal for durable top-level workflows, LangGraph for agent-level routing | June 2026 analysis recommends this exact combination for production AI.[cite:96] |
| Vector + keyword search | pgvector + Postgres full-text (BM25) + RRF fusion | Simplest production-valid hybrid retrieval with no additional infrastructure.[cite:97][cite:100][cite:103] |
| Reranking | Jina Reranker v2 (self-hosted) or Cohere Rerank v3.5 (API) | 15–30% quality improvement; Jina is open-weight and self-hostable.[cite:100] |
| Local deployment | Docker Compose with autoheal, pinned digests, named volumes | Viable for small-team production when operational gaps are addressed.[cite:120][cite:127] |
| Kubernetes deployment | Helm chart | Standard production path for HA, scaling, and cloud-native operations.[cite:84] |
| Git integration | GitHub App | Best source of PR, CI, and review events; Open SWE uses same approach.[cite:35] |
| MCP integration | Official MCP SDKs + dedicated MCP gateway service | June 2025 spec adds structured output, auth, and elicitation; 2026 spec moves to stateless HTTP.[cite:106][cite:107][cite:110] |
| Spec workflow | SDD lifecycle inspired by GitHub Spec Kit and Microsoft SDD | Proven four-phase model with explicit checkpoints and agent reliability benefits.[cite:61][cite:65] |
| Multi-agent mode | Supervisor pattern with bounded specialist subagents | Most production multi-agent implementations work best with hierarchical control and explicit routing criteria.[cite:112][cite:114] |
| Observability | OpenTelemetry + Prometheus + Grafana | Standard self-hostable observability stack; compatible with LangSmith for agent-specific tracing.[cite:17] |

---

## What Makes Forge Buildable

Three practical principles derived from the research make this platform buildable rather than aspirational:

**1. Phase across complexity.** Every component listed in the spec has a proven simpler V1 alternative: pgvector before a dedicated vector database, LangGraph before full Temporal, single-agent before multi-agent, Docker Compose before Kubernetes.[cite:93][cite:120][cite:100] The platform can ship useful value at each phase without requiring the full architecture to be complete first.

**2. Buy patterns, not platforms.** The spec borrows the task-as-control-plane pattern from Symphony, the repo-aware context pattern from Open SWE, the SDD phases from GitHub Spec Kit, and the hybrid retrieval pattern from documented production RAG systems.[cite:34][cite:35][cite:65][cite:100] None of these require inventing novel algorithms — they require disciplined integration of proven patterns.

**3. Eval-first development.** LangGraph's 2026 production guide is explicit: build a golden set of 30–100 representative task-and-output pairs before adding orchestration complexity.[cite:93] Forge's V1 should include a task evaluation harness so quality can be measured, not estimated, at every phase.
