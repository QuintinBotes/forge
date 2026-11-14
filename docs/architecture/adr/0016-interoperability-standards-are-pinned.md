# 0016. Interoperability standards are pinned

**Status:** Proposed
**Date:** 2026-11-07

## Context

MCP revisions, A2A (v1.0, March 2026), OpenTelemetry GenAI semantic conventions ("Development"
stability), in-toto/SLSA and CycloneDX/SPDX AI profiles all move. Forge already emits generic OTel
and Ed25519 DSSE attestations.

## Decision (proposed)

Each standard forge speaks is pinned to a version in code and docs: MCP `2025-06-18` (accepting
`2025-03-26`) for spec I; OTel GenAI spans at a pinned semconv version; A2A v1.0 agent cards for
external agents; attestations as in-toto statements with optional Sigstore signing. Upgrades are
explicit changes with tests.

## Consequences

- No silent behaviour change when a standard moves.
- Forge lags new revisions by design until someone upgrades them deliberately.
