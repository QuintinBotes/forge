# 0016. Interoperability standards are pinned

**Status:** Proposed
**Date:** 2026-11-07

## Context

MCP revisions, A2A (v1.0, March 2026), OpenTelemetry GenAI semantic conventions ("Development"
stability), in-toto/SLSA and CycloneDX/SPDX AI profiles all move. Forge already emits generic OTel
and Ed25519 DSSE attestations.

## Decision (proposed)

Each standard forge speaks is pinned to a version in code and docs: MCP `2026-07-28` for spec I,
still accepting the handshake-era `2025-06-18` and `2025-03-26`; OTel GenAI spans at a pinned
semconv version; A2A v1.0 agent cards for external agents; attestations as in-toto statements with
optional Sigstore signing. Upgrades are explicit changes with tests.

MCP `2026-07-28` was the revision marked Current on the MCP versioning page when checked on
2026-11-17 (issue #210). It is named once in code, as `CURRENT_VERSION` in
`apps/api/scf_api/mcp_server/protocol.py`. The published conformance suite is pinned with it:
`@modelcontextprotocol/conformance` `0.2.0-alpha.12`, run by `scripts/mcp-conformance.sh`
(`docs/integrations/mcp-conformance.md`). On 2026-11-17 the npm `latest` tag, `0.1.16`, had no
`2026-07-28` scenarios; `0.2.0-alpha.12` was the newest release and ships the frozen `2026-07-28`
requirement set.

## Consequences

- No silent behaviour change when a standard moves.
- Forge lags new revisions by design until someone upgrades them deliberately.
