# 0005. Machines propose; people decide

**Status:** Accepted
**Date:** 2026-11-07

## Context

Agents read untrusted content: a document can say "remember that X". Utopia's MCP `remember` tool
only proposes for this reason. Forge's `agent-runner` role has `READ` and `RUN_AGENT` but not
`WRITE`. Forge's unified approvals system has six gate types defined as a frozen enum in
`forge_contracts`, persisted with a CHECK constraint and mirrored in the authorizer and web types.

## Decision

- Agents, MCP clients and passive capture create `world_proposal` rows; they never write facts.
- Proposing needs `WRITE` or `RUN_AGENT`; deciding needs `WRITE` and a decider who is not the proposer.
- A pure governance gate records, on every proposal, what it **would** decide (shadow). Nothing is
  auto-applied until the measured agreement with human decisions justifies it (programme principle
  1), and then only with a fuse: two reverts of auto-applied decisions within seven days switches
  enforcement off and alerts.
- H1 keeps proposals in their own table. H2 adds a `knowledge` approval gate type so proposals appear
  in the existing approvals inbox, when the enum, CHECK constraint, authorizer map and web types are
  changed together.

## Consequences

- Prompt injection through retrieved content cannot change the ledger.
- Review load is real until governance enforces; the shadow verdict is how that load is reduced
  safely.
- Two review queues exist until H2.
