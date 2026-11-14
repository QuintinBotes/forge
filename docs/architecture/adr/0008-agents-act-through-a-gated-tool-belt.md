# 0008. Agents act through a policy-gated tool belt with scoped credentials and enforced budgets

**Status:** Accepted
**Date:** 2026-11-07

## Context

Forge's agent paths have no tools. The 2026 incident pattern (Amazon Q wiper prompt, Nx
"s1ngularity", Replit's production database deletion) is an agent acting on untrusted content with
broad credentials. Lore ran a weekend loop for at least $2,415 with no hard cap and hands a full-access
token to agent pods.

## Decision

- Every main agent path registers tools from `ROLE_TOOLS`, executed inside the run's sandbox, each
  call checked by `ActionPolicyGate`. Scopes are intersected, never widened.
- Sandboxes have no network by default. Pushing and opening PRs happen in the finalizer, outside the
  sandbox, with a GitHub App installation token minted per run, scoped to one repository and
  `contents:write` + `pull_requests:write`, expiring at run end.
- Budgets are checked before every model call and stop the run when exceeded. Local-CLI runs are
  metered as unpriced, never free.
- Human approval before merge stays mandatory.

## Consequences

- A compromised run can damage at most its own branch in one repository.
- Agents cannot open, approve or merge PRs themselves.
