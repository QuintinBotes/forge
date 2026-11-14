# 0006. Forge serves MCP itself

**Status:** Accepted
**Date:** 2026-11-07

## Context

Lore's main value is org context served to Claude Code over 42 MCP tools. Tervin admits outside
tools only as a process, a JSON CLI, MCP or `.tervin/` data. Forge speaks MCP only as a client
(`apps/mcp-gateway`); its server is a test double. The gateway's threat model is "other people's
servers are untrusted".

## Decision

Forge exposes a Streamable-HTTP MCP server inside `forge_api` at `POST {api_prefix}/mcp`, reusing API
key authentication, RBAC, workspace scoping and audit. It is stateless, read-only by default, hides
write tools from principals that cannot use them, and its only write tool creates a proposal. Tool
handlers call the same services as the REST routers. The outbound gateway stays separate.

## Consequences

- Claude Code, Codex, Cursor and tervin reach forge with a URL and a token.
- No MCP SDK dependency in this phase; the JSON-RPC surface is small and forge already speaks it.
- OAuth 2.1 discovery for MCP is deferred until forge issues audience-bound tokens.
