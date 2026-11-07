# 0002. Forge is the factory's system of record; tervin is its desktop face

**Status:** Accepted
**Date:** 2026-11-07

## Context

Tervin's local v3 plan (#122, #118) adds a board, specs, a coordinator, a merge queue and approvals —
the same things forge owns. Two cores would mean two boards drifting, two approval records and two
policy engines. Tervin's own plan says it will call forge "only if Tervin gets a team server". Forge
is multi-user, durable and governed; tervin is single-user, macOS-only, holds no secrets and opens no
port. Forge cannot host a PTY, a shell or Touch ID; tervin can.

## Decision

Forge owns the record: board, specs, approvals, policy, world model, knowledge, runs, audit and cost.
Tervin is a **client and a gated local runner**: it renders forge's record, answers approvals
locally, and runs agents on the developer's machine behind its own Rules, reporting to forge.
Integration is by protocol only (ADR-0015); no code moves in either direction.

## Consequences

- Tervin's specs 012–014 (coordinator, merge queue, knowledge) are deferred in connected mode; tervin
  keeps them for offline use.
- Forge must expose what tervin consumes: an MCP server (spec I), `forge --json`, a run event stream,
  approval push and a runner protocol (spec L).
- The Apache-2.0 → MIT licence direction never comes up, because nothing is copied.
