# Changelog

All notable changes to Forge are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project adheres
to [Semantic Versioning](https://semver.org/). This file is generated from the
[Conventional Commits](https://www.conventionalcommits.org/) history by
`uv run cz changelog` (run automatically by `make bump`) — do not edit released
sections by hand; write a well-formed commit instead.

> **Pre-history note.** A few of the earliest bootstrap commits
> (`checkpoint: phase 1 partial build`) predate the conventional-commit
> requirement and are not represented below. From HARD-12 onward the format is
> enforced by a `commit-msg` hook (`make hooks`) and a CI `cz check` gate, so the
> changelog is always derivable going forward.

## Unreleased

### Added

- **self-eval**: enforce the gate on config changes + worker-driven run (Phase A wiring) (#66)
- **self-eval**: baseline persistence + live agent-backed eval runner (Phase A substrate) (#65)
- Self-Eval Gate — block config changes that regress a workspace's private per-repo suite (trust-layer 4/4) (#64)
- Red-Team Gate (trust layer, phase 3) — adversary must break the change in-sandbox before the human gate (#63)
- Time-Travel Runs (trust layer, phase 2) — deterministic record-replay of agent runs (#62)
- Attested Changesets (trust layer, phase 1) — signed provenance chained into the tamper-evident audit log (#61)
- OIDC SSO + marketplace publish + benchmark leaderboard + under-dev banner (#57)
- frontend UX pass — clearer IA/nav, one primary action, progressive disclosure, empty/loading/error states, a11y (#39)
- IaC — OpenTofu infra/ (Hetzner control-plane + Cloudflare + Fly agents), dev/staging/prod, remote state, runbook (#38)
- F40 deferred-scope deltas — PM adapters (BYO board), MCP, policy, automations, sprint depth, observability (#37)
- realtime co-editing (WS server + CRDT spec co-editing + live push) (#36)
- Spec Studio — dual-format spec authoring (Guided/Markdown/YAML/Read), BYOK AI draft, lifecycle, versioning (#33)
- adaptive orchestration (auto model routing + per-role effort + settings + cost-by-tier) (#32)
- public-readiness — under-dev banner, honest status, live spec dashboard (#30)
- **web/walkthrough**: In-app guided walkthrough
- **web/workflow-editor**: Workflow visual editor
- **web/pm-integrations**: PM integrations
- **web/rbac-admin**: Multi-team & RBAC admin
- **web/sso-settings**: SSO / SCIM settings
- **web/deployment-gates**: Deployment gates
- **web/audit-log**: Audit viewer
- **web/sprints**: Sprints & velocity
- **web/observability**: Observability & cost
- **web/incidents**: Incidents
- **web/marketplace**: Marketplace
- **web/spec-dashboard**: Spec-validation dashboard
- **web/run-trace-viewer**: Run-trace viewer
- **web/approval-inbox**: Approval inbox
- **web/board-depth**: Board depth
- **HARD-06**: live-slack
- **HARD-05**: live-mcp-server
- **HARD-03**: live-reranker
- **HARD-02**: live-model-byok
- **HARD-01**: live-github-app
- **HARD-08**: kubernetes-helm-deploy
- **HARD-12**: release-engineering
- **HARD-10**: observability-cost-prod
- **HARD-04**: real-eval-corpus
- **HARD-11**: reliability-maturity
- **design**: Forge brand identity + design system
- **HARD-13**: secrets-config-prod
- **HARD-09**: security-hardening
- **HARD-07**: docker-build-and-pin
- **deploy**: one-command local dev stack (compose up --wait, migrate+seed)
- **F39**: audit-log
- **F38**: observability-cost-metrics
- **F37**: auth-secrets-byok
- **F36**: human-approval-system
- **F35**: benchmark-leaderboard
- **F34**: firecracker-sandbox
- **F33**: enterprise-sso
- **F32**: integration-marketplace
- **F23**: spec-validation-dashboard
- **F31**: F31 deployment-gates
- **F30**: F30 multi-team-rbac
- **F29**: F29 advanced-policy-engine
- **F28**: F28 workflow-visual-editor
- **F27**: F27 supervised-multi-agent
- **F26**: F26 sprint-velocity
- **F25**: F25 temporal-integration
- **F24**: F24 kubernetes-helm
- **F22**: F22 multi-repo-execution
- **F21**: F21 workflow-automations
- **F20**: F20 mcp-sync-and-index
- **F19**: F19 container-sandboxing
- **F18**: F18 pm-adapters
- **F17**: F17 incident-workflows
- **H6**: coverage: worker + agent-runtime
- **H5**: LangGraph StateGraph swap
- **H4**: OAuth code exchange
- **H3**: Fernet crypto backend
- **H2**: tree-sitter chunking backend
- **H1**: mypy whole-workspace typecheck
- phase 2 integration, fixes, verification, and RAG spine smoke
- Forge V1 feature implementations (phase 1 fan-out)
- **phase0**: 0.6 deploy+ci+test-infra
- **phase0**: 0.5 web skeleton (apps/web)
- **phase0**: 0.4 api skeleton (apps/api)
- **phase0**: 0.3 contracts (packages/contracts)
- **phase0**: 0.2 data-model (packages/db)
- **phase0**: 0.1 workspace+tooling

### Fixed

- **config**: document real FORGE_MODEL_* env vars and warn on scripted-client fallback
