# 0014. Untrusted content cannot drive privileged tools

**Status:** Accepted
**Date:** 2026-11-07

## Context

Repository files, issues, PR comments, retrieved passages and MCP results are prompt-injection
vectors. The "lethal trifecta" is private data + untrusted content + an exfiltration or write channel
in one agent.

## Decision

- Agent tools that write outside the run's worktree do not exist. Opening PRs, pushing, approving,
  merging, and asserting world facts are performed by finalizers or by people, never by a tool an agent
  can call mid-run.
- Sandboxes have no network by default.
- Machine writes to shared knowledge are proposals (ADR-0005).
- Retrieved content is labelled with its source in the prompt context so policies can key on it.

## Consequences

- Some autonomy is deliberately unavailable; it returns only as shadow-measured, policy-gated
  automation (programme principle 1).
