# Forge Implementation Slices — Master Index

All 39 features, grouped by the [FORGE_SPEC phased roadmap](../FORGE_SPEC.md). One row per
slice. See [`README.md`](./README.md) for what a slice is and how to use one.

> Verified on 2026-06-26: all 39 slice files (F01–F39) exist on disk. The only referenced
> artifact without a file is the **F00 foundation substrate** (see legend) — it is specified
> by Phase 0 of the [overnight V1 build plan](../superpowers/plans/2026-06-26-forge-v1-overnight.md)
> rather than a numbered slice.

## Legend

- **Phase** — `v1` Foundation, `v2` Depth, `v3` Scale, `cross` cross-cutting platform module (spans all phases).
- **Key dependencies** — primary *upstream* features that must exist (or be stubbed) first; not exhaustive — see each slice's §5 for the full list including stubbable/soft deps.
  - **`F00*`** = foundation substrate (monorepo + `packages/db` data model + `packages/contracts` + `apps/api` skeleton + deploy/test infra). **No slice file yet**; built by overnight-plan Phase 0 (tasks 0.1–0.6). Repoint to `cross-cutting/C01-monorepo-and-api-foundations.md` once that slice exists.
- **Effort** — `S` ≈ ≤1 engineer-week, `M` ≈ 1–2 weeks, `L` ≈ 3–4 weeks. Slashes/dashes (`S/M`, `M–L`) mean it straddles two bands.
- **Risk** — build/correctness + security risk: `Low` < `Low-Med` < `Med` < `Med-High` < `High`. Derived from each slice's §9.
- **Slice link** — relative path to the slice file.

---

## Phase 1 — Foundation (V1) · `v1/` · F01–F16

| ID | Feature | Phase | Key dependencies | Effort | Risk | Slice link |
|---|---|---|---|---|---|---|
| F01 | Native Project Board | v1 | F00*, F37, F39 | L | Med-High | [v1/F01-project-board.md](./v1/F01-project-board.md) |
| F02 | Spec-Driven Development Engine | v1 | F00*, F01, F11 | L | Med | [v1/F02-spec-engine.md](./v1/F02-spec-engine.md) |
| F03 | GitHub App Integration | v1 | F00*, F37, F39 | L | Med-High | [v1/F03-github-app.md](./v1/F03-github-app.md) |
| F04 | Repo Policy System (`.forge/policy.yaml` + `AGENTS.md`) | v1 | F00*, F03 | M | Med-High | [v1/F04-repo-policy.md](./v1/F04-repo-policy.md) |
| F05 | Hybrid Knowledge Retrieval (pgvector + BM25 + RRF + Jina rerank) | v1 | F00*, F03, F04 | L | Med | [v1/F05-hybrid-knowledge-retrieval.md](./v1/F05-hybrid-knowledge-retrieval.md) |
| F06 | Single Execution Agent (LangGraph routing) | v1 | F00*, F04, F05, F11, F37 | L | Med-High | [v1/F06-single-execution-agent.md](./v1/F06-single-execution-agent.md) |
| F07 | Default Feature Workflow State Machine (Postgres FSM) | v1 | F00*, F02, F06 | M | Med | [v1/F07-feature-workflow-fsm.md](./v1/F07-feature-workflow-fsm.md) |
| F08 | Plan → Execute → Verify → PR → Approval Flow | v1 | F06, F07, F03, F02, F36 | L | Med-High | [v1/F08-plan-execute-verify-pr-approval.md](./v1/F08-plan-execute-verify-pr-approval.md) |
| F09 | MCP Gateway V1 (read-only query-through) | v1 | F00*, F37, F39 | L | Med | [v1/F09-mcp-gateway-v1.md](./v1/F09-mcp-gateway-v1.md) |
| F10 | Run Trace Viewer (step-level inspection) | v1 | F06, F39, F37 | S/M | Low-Med | [v1/F10-run-trace-viewer.md](./v1/F10-run-trace-viewer.md) |
| F11 | Skill Profiles (`packages/skill-sdk`) | v1 | F00*, F04 | M | Med | [v1/F11-skill-profiles.md](./v1/F11-skill-profiles.md) |
| F12 | Golden Test Set & Evaluation Harness | v1 | F02, F05, F06 | L | Med | [v1/F12-eval-harness.md](./v1/F12-eval-harness.md) |
| F13 | Local Quickstart (<10 min) | v1 | F14, F37, V1 core | M | Med | [v1/F13-local-quickstart.md](./v1/F13-local-quickstart.md) |
| F14 | Docker Compose Self-Hosted (production best practices) | v1 | F00*, F09, F37, F39 | L | Med-High | [v1/F14-docker-compose-selfhost.md](./v1/F14-docker-compose-selfhost.md) |
| F15 | Self-Hosting Documentation (`docs/self-hosting/`) | v1 | F13, F14, F16 | M | Low-Med | [v1/F15-selfhosting-docs.md](./v1/F15-selfhosting-docs.md) |
| F16 | Slack Notifications | v1 | F03, F08, F36, F37 | L | Med-High | [v1/F16-slack-notifications.md](./v1/F16-slack-notifications.md) |

## Phase 2 — Depth (V2) · `v2/` · F17–F26

| ID | Feature | Phase | Key dependencies | Effort | Risk | Slice link |
|---|---|---|---|---|---|---|
| F17 | Incident Workflows & Postmortem Generation | v2 | F07, F06, F16, F36 | L | Med-High | [v2/F17-incident-workflows.md](./v2/F17-incident-workflows.md) |
| F18 | External PM Adapters (Jira, Linear) | v2 | F01, F03, F16 | L | Med | [v2/F18-pm-adapters.md](./v2/F18-pm-adapters.md) |
| F19 | Container Sandboxing (Docker per-task isolation) | v2 | F06, F08, F14 | L | Med-High | [v2/F19-container-sandboxing.md](./v2/F19-container-sandboxing.md) |
| F20 | MCP Sync-and-Index Mode | v2 | F09, F05, F04 | M | Med | [v2/F20-mcp-sync-and-index.md](./v2/F20-mcp-sync-and-index.md) |
| F21 | Saved Workflow Automations (rule engine) | v2 | F07, F01, F02 | L | Med | [v2/F21-workflow-automations.md](./v2/F21-workflow-automations.md) |
| F22 | Multi-Repo Task Execution | v2 | F06, F08, F03, F04 | L | Med-High | [v2/F22-multi-repo-execution.md](./v2/F22-multi-repo-execution.md) |
| F23 | Spec Validation Dashboard (Requirement Traceability) | v2 | F02, F08, F12 | M | Med | [v2/F23-spec-validation-dashboard.md](./v2/F23-spec-validation-dashboard.md) |
| F24 | Kubernetes Helm Chart | v2 | F14, F25, F37 | L | Med | [v2/F24-kubernetes-helm.md](./v2/F24-kubernetes-helm.md) |
| F25 | Temporal Workflow Engine Integration | v2 | F07, F08, F06 | L | Med-High | [v2/F25-temporal-integration.md](./v2/F25-temporal-integration.md) |
| F26 | Sprint Management & Velocity Dashboards | v2 | F01, F23, F38 | M | Med | [v2/F26-sprint-velocity.md](./v2/F26-sprint-velocity.md) |

## Phase 3 — Scale (V3) · `v3/` · F27–F35

| ID | Feature | Phase | Key dependencies | Effort | Risk | Slice link |
|---|---|---|---|---|---|---|
| F27 | Supervised Multi-Agent Mode (Supervisor) | v3 | F06, F04, F08, F36 | L | Med-High | [v3/F27-supervised-multi-agent.md](./v3/F27-supervised-multi-agent.md) |
| F28 | Workflow Visual Editor | v3 | F07, F21, F27, F29 | L | Med | [v3/F28-workflow-visual-editor.md](./v3/F28-workflow-visual-editor.md) |
| F29 | Advanced Policy Engine (conditional rules) | v3 | F04, F30, F31 | L | High | [v3/F29-advanced-policy-engine.md](./v3/F29-advanced-policy-engine.md) |
| F30 | Multi-Team Workspace Controls & Full RBAC | v3 | F37, F01, F29 | L | High | [v3/F30-multi-team-rbac.md](./v3/F30-multi-team-rbac.md) |
| F31 | Deployment Gates & Environment Promotion | v3 | F08, F29, F30 | L | Med-High | [v3/F31-deployment-gates.md](./v3/F31-deployment-gates.md) |
| F32 | Integration Marketplace (community MCP connectors & skill profiles) | v3 | F09, F11, F18, F29 | L | High | [v3/F32-integration-marketplace.md](./v3/F32-integration-marketplace.md) |
| F33 | Enterprise SSO (SAML, SCIM) | v3 | F37, F30 | L | High | [v3/F33-enterprise-sso.md](./v3/F33-enterprise-sso.md) |
| F34 | Firecracker / gVisor Sandbox Isolation | v3 | F19, F24, F06 | L | Med-High | [v3/F34-firecracker-sandbox.md](./v3/F34-firecracker-sandbox.md) |
| F35 | Benchmark Suite & Public Eval Leaderboard | v3 | F12, F32, F27 | M–L | High | [v3/F35-benchmark-leaderboard.md](./v3/F35-benchmark-leaderboard.md) |

## Cross-cutting platform modules · `cross-cutting/` · F36–F39

| ID | Feature | Phase | Key dependencies | Effort | Risk | Slice link |
|---|---|---|---|---|---|---|
| F36 | Human Approval System (gates + approval UI) | cross | F08, F07, F01, F16 | L | Med-High | [cross-cutting/F36-human-approval-system.md](./cross-cutting/F36-human-approval-system.md) |
| F37 | Auth & Secrets Service (BYOK, OAuth, API keys, RBAC) | cross | F00* | L | High | [cross-cutting/F37-auth-secrets-byok.md](./cross-cutting/F37-auth-secrets-byok.md) |
| F38 | Observability & Cost Metrics | cross | F00*, F06, F39, F10 | L | Med | [cross-cutting/F38-observability-cost-metrics.md](./cross-cutting/F38-observability-cost-metrics.md) |
| F39 | Immutable Audit Log | cross | F00* | L | Med | [cross-cutting/F39-audit-log.md](./cross-cutting/F39-audit-log.md) |

---

## Counts

| Phase | Folder | Features | Effort: L / M(±) / S(±) |
|---|---|---|---|
| Phase 1 — Foundation (V1) | `v1/` | 16 (F01–F16) | 10 L · 5 M · 1 S/M |
| Phase 2 — Depth (V2) | `v2/` | 10 (F17–F26) | 7 L · 3 M |
| Phase 3 — Scale (V3) | `v3/` | 9 (F27–F35) | 8 L · 1 M–L |
| Cross-cutting | `cross-cutting/` | 4 (F36–F39) | 4 L |
| **Total** | | **39** | |

**Build-order note:** `F00*` (foundation substrate) is the prerequisite for almost
everything and has no slice yet — build overnight-plan Phase 0 first. Within V1 the
critical path is roughly `F00* → F37/F39 → F04 → F05/F11 → F06 → F07 → F08 → F36`, with
F01 (board) buildable in parallel once F37/F39 exist.
