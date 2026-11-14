# 0015. Desktop integration contract with tervin

**Status:** Accepted
**Date:** 2026-11-07

## Context

Tervin has no plugin API by policy, stores no secrets (only env-var names), opens no listening port,
and admits tools only as a process, a JSON CLI, MCP or `.tervin/` data.

## Decision

Forge integrates with tervin through, in order: its MCP server (spec I), `forge --json`, an SSE run
event stream, approval push over `/ws`, a runner protocol, and an ACP stdio shim (spec L).
Authentication is a workspace-scoped token or a device-code flow, read by tervin from a named
environment variable. No loopback OIDC, no embedded web UI, no shared code. Every object forge sends
carries provenance (`observed_by`, `recorded_at`, `enforceable`).

## Consequences

- Each seam is useful to other clients too (Claude Code, CI, scripts).
- Tervin decides how much of forge to consume; forge never depends on tervin.
