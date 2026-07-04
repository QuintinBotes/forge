# Forge examples

Copy-paste-ready configuration for the four declarative subsystems plus sample
spec manifests. Every file here is **executed as a test fixture** by
`examples/tests/` — each one is loaded and validated against the exact same
loader the running platform uses, so an example can never silently drift from
the schema.

Run the example gate:

```bash
uv run pytest examples
```

## What's here

| Directory | Loaded by | What it shows |
|---|---|---|
| [policies/](policies) | `forge_policy.load_policy` | `.forge/policy.yaml` for 5 repo types |
| [skills/](skills) | `forge_skill.load_profiles` / `load_profile` | Community skill profiles |
| [workflows/](workflows) | `forge_workflow.load_definition` | Workflow DSL state machines |
| [mcp-connectors/](mcp-connectors) | `forge_mcp.load_connection_file` | MCP connection definitions |
| [specs/](specs) | `forge_spec.load_manifest` | Spec `manifest.yaml` documents |

## Policies (`policies/`)

One `.forge/policy.yaml` per common repo shape. Drop the matching file into your
repo at `.forge/policy.yaml` and adjust `repo_id`, paths, and reviewers.

- [python-backend.yaml](policies/python-backend.yaml) — Python REST API service.
- [typescript-frontend.yaml](policies/typescript-frontend.yaml) — Next.js / TypeScript app.
- [go-microservice.yaml](policies/go-microservice.yaml) — Go gRPC microservice.
- [infra-terraform.yaml](policies/infra-terraform.yaml) — Terraform IaC (most restrictive).
- [data-pipeline.yaml](policies/data-pipeline.yaml) — Python data / ML pipelines.

Policy is **deny-by-default**: a `deny` glob beats an `allow` glob, write
actions are matched against `write_rules`, deploys against `deploy_rules`, and
anything in `restricted_actions` is blocked outright.

## Skill profiles (`skills/`)

Skill profiles enforce engineering discipline structurally (test-first, coverage
floors, forbidden shortcuts) rather than via prompt text.

- [community-profiles.yaml](skills/community-profiles.yaml) — a `skill_profiles:`
  collection of new profiles (`docs-writer`, `dependency-upgrade`,
  `data-migration`, `api-contract-review`).
- [single-profile.yaml](skills/single-profile.yaml) — one profile per file
  (`performance-tuning`).

These extend the seven built-in profiles shipped in `forge_skill`; register them
with a `SkillProfileRegistry` to resolve them by name.

## Workflows (`workflows/`)

Declarative state machines parsed and graph-validated at load time.

- [default-feature.yaml](workflows/default-feature.yaml) — the full SDD lifecycle.
- [hotfix.yaml](workflows/hotfix.yaml) — a lean bugfix flow.
- [incident-response.yaml](workflows/incident-response.yaml) — incident handling.

## MCP connectors (`mcp-connectors/`)

MCP server connection definitions. All keep `allow_write: false` (security rule
1) and bind tokens to their server via the RFC 8707 `resource` parameter.

- [confluence.yaml](mcp-connectors/confluence.yaml) — HTTP + OAuth, sync-and-index.
- [github-issues.yaml](mcp-connectors/github-issues.yaml) — HTTP + API key, query-through.
- [postgres-readonly.yaml](mcp-connectors/postgres-readonly.yaml) — read replica, resources only.
- [filesystem-docs.yaml](mcp-connectors/filesystem-docs.yaml) — stdio, no auth.

## Spec manifests (`specs/`)

Machine-readable spec metadata with requirement -> acceptance traceability.

- [SPEC-17-customer-search/manifest.yaml](specs/SPEC-17-customer-search/manifest.yaml) — an approved spec.
- [SPEC-42-rate-limiting/manifest.yaml](specs/SPEC-42-rate-limiting/manifest.yaml) — a spec still clarifying.

## See also

Self-hosting and operations guides live in
[../docs/self-hosting/](../docs/self-hosting).
