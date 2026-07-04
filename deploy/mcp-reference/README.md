# Forge reference MCP server (HARD-05 integration substrate)

A self-hosted, dependency-free MCP server that speaks the **real** JSON-RPC 2.0
wire protocol (MCP revision `2025-06-18`) over both real transports:

- **Streamable-HTTP** — `python -m forge_mcp.reference_server --http --port 8901`
- **stdio** — `python -m forge_mcp.reference_server --stdio`

It is the integration substrate for HARD-05: the live `forge_mcp` transports
(`HttpMcpTransport` / `StdioMcpTransport`) drive it over a real socket / real
subprocess, so **"live MCP" is proven end-to-end without any external SaaS or
credential** — the only "cred" is a local URL.

The server is implemented in the `forge_mcp` SDK
(`packages/mcp-sdk/forge_mcp/reference_server.py`) so tests can also self-host it
in-process on a loopback socket. It serves the same fixture corpus the security
tests trust:

- namespaces `engineering` / `architecture` / `finance`,
- a read tool (`search_pages`, `get_document`) and a write tool (`create_page`,
  annotated `readOnlyHint: false`, `destructiveHint: true`),
- one resource (`confluence://engineering/page-1`) containing a **planted FAKE
  secret** so the redaction path is exercised on server-authored bytes.

It also exposes `GET /inspect`, which reports the last `initialize` handshake it
observed — the captured RFC 8707 `resource` indicator and whether an
`Authorization` header was present (never the token value) — so a test can
assert token-binding was sent over the wire.

> **Not for production.** This is a test double, not a real knowledge source.

## Run it

```bash
# In-repo (no Docker):
python -m forge_mcp.reference_server --http --host 127.0.0.1 --port 8901

# Via the integration compose overlay (built from this Dockerfile):
docker compose -f deploy/docker-compose.integration.yml up -d mcp-reference
curl -s localhost:8901/health
```

See `docs/runbooks/live-mcp.md` for the full live-verification runbook.
