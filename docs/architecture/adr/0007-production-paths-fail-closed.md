# 0007. Production paths fail closed

**Status:** Accepted
**Date:** 2026-11-07

## Context

Forge's house rule is "never fake a capability". Yet the Temporal activities default to success
(`pr_url = https://example.invalid/pr/1`, all checks true, red-team parked as a pass), the knowledge
router hard-wires a hash-based embedder, and MCP defaults to a null transport. Lore's auto-merge
fails open when its engine is unreachable (#2374).

## Decision

Offline, scripted or fake defaults run only in tests or behind an explicit, named development flag
(`FORGE_ALLOW_FAKE_ACTIVITIES`, `FORGE_ALLOW_SCRIPTED_AGENT`). On production paths an unconfigured
dependency **refuses** with a reason (HTTP 503, an activity error, a failed run). A repository test
forbids `example.invalid` outside test code.

## Consequences

- Some flows that "worked" in a bare install now refuse until configured; release readiness reflects
  reality.
- Every new capability in G–L must define its unconfigured behaviour as a refusal.
