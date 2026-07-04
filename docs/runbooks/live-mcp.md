# Runbook — Live MCP transport over real HTTP/stdio (HARD-05)

Forge's MCP control plane (`forge_mcp` SDK, the `apps/mcp-gateway` service, and
the `apps/api` `/mcp/*` router) lets operators register MCP servers and lets
agents read from them (namespace-scoped, redacted) and call read-only-by-default
tools — all audited. ALPHA proved every security rule against an in-memory
`FakeTransport`; **HARD-05 makes the transport real**.

The single load-bearing seam `forge_mcp.transport.Transport` now has two live
implementations:

- `HttpMcpTransport` — MCP **Streamable-HTTP** (JSON-RPC 2.0 over HTTP, revision
  `2025-06-18`); sends the RFC 8707 `resource` indicator + `Mcp-Session-Id`.
- `StdioMcpTransport` — JSON-RPC 2.0 over a subprocess' stdin/stdout.

They are produced by `live_transport_factory(token_resolver=…)`, which the
gateway and API wire in **only when `MCP_LIVE_TRANSPORT` is truthy** — otherwise
the ALPHA `NullTransport` stays (503 on any live op). The live path is opt-in and
never implicit.

> **No external SaaS or credential is required.** The live substrate is the
> **self-hosted reference MCP server** (`deploy/mcp-reference/`, implemented in
> `packages/mcp-sdk/forge_mcp/reference_server.py`). The only "cred" is a local
> URL. HARD-05 proves the path against it; a hosted Confluence/Jira/GitHub MCP
> connector + a human SSRF/transport pentest are explicitly HARD-09's punch-list.

See the slice doc `docs/implementation-slices/hardening/HARD-05-live-mcp-server.md`
for the acceptance criteria this runbook verifies.

---

## 1. When it activates

`MCP_LIVE_TRANSPORT` is the master switch:

| `MCP_LIVE_TRANSPORT` | Behavior |
|---|---|
| unset / `false` (default) | `NullTransport` — registration + metadata work, any live op returns **503**. Exactly ALPHA. The hermetic suite stays green and network-free. |
| `true` | The gateway/API build the live transport factory; MCP connections drive **real** servers over real HTTP/stdio. |

The default `uv run pytest -q` run keeps `MCP_LIVE_TRANSPORT` unset, so the
`live_mcp`-marked integration tests **skip clean** and no socket is opened
(there is a no-socket guard in the unit lane).

---

## 2. Verify the live path (self-hosted, no external cred)

The live integration tests self-host the reference server on a loopback socket,
so you need **nothing but the flag**:

```bash
MCP_LIVE_TRANSPORT=true uv run pytest -m live_mcp -q
```

That runs, over **real transport** against the reference server:

- **AC4** — a live `read_resource` returns a normalized `MCPResourceContent`,
  and `query_through` returns attributed `RetrievedChunk`s
  (`chunk_type=mcp_resource`, non-empty `source_uri`) — HTTP **and** stdio.
- **AC5** — a write (`create_page`, or any un-annotated verb) on an
  `allow_write:false` connection is **denied by default** (403); it succeeds only
  when the connection is re-registered `allow_write:true`.
- **AC6** — the RFC 8707 `resource` indicator is sent over the wire (asserted via
  the reference server's `GET /inspect`); an authed connection with no binding is
  rejected at factory time.
- **AC7** — namespace scoping blocks an out-of-scope `finance://…` read before it
  reaches the server; `list_resources` returns only in-scope resources.
- **AC8** — a redacted `MCPAuditEntry` is written for every live list/read/call;
  a planted server secret is `[redacted]` in the returned content and the audit.
- **AC10** — connection-refused / 5xx / JSON-RPC error / timeout map to the right
  SDK exception; no secret ever appears in the error message.

### Optional: against the containerized reference server

```bash
docker compose -f deploy/docker-compose.integration.yml up -d mcp-reference
export MCP_LIVE_TRANSPORT=true MCP_SERVER_URL=http://localhost:8901/mcp
uv run pytest -m live_mcp -q
docker compose -f deploy/docker-compose.integration.yml down
```

### Optional: durable Postgres audit (AC8 durable row)

The redaction + "row-per-op" assertions run against the in-memory platform store
in the command above. The **durable, immutable Postgres** row needs HARD-01's
Postgres `AuditStore` + a live pgvector DB:

```bash
export FORGE_MCP_AUDIT_BACKEND=db \
       FORGE_TEST_DATABASE_URL=postgresql+psycopg://forge:forge@localhost:5433/forge
MCP_LIVE_TRANSPORT=true uv run pytest -m live_mcp -q
```

> **PARKED:** with the Postgres-backed `AuditStore` not yet delivered by HARD-01,
> the durable-row assertion is deferred to that store; the bridge
> (`forge_api.observability.MCPAuditSink` → `AuditLog.record_mcp_call`) and the
> redaction/every-op assertions are proven here.

---

## 3. Environment variables

| Var | Purpose | Default |
|---|---|---|
| `MCP_LIVE_TRANSPORT` | master switch; falsy -> NullTransport (ALPHA) | `false` |
| `MCP_SERVER_URL` | reference/real server endpoint for the lane | unset -> self-host |
| `MCP_TOKEN` | bearer token for an authed server (dev/integration only) | unset |
| `MCP_RESOURCE` | RFC 8707 resource indicator override | falls back to endpoint |
| `MCP_TRANSPORT` | `http` \| `stdio` | `http` |
| `MCP_STDIO_COMMAND` | argv for the stdio server | unset -> in-repo reference `--stdio` |
| `MCP_HTTP_TIMEOUT_S` | per-request timeout (seconds) | `30` |
| `MCP_PROTOCOL_VERSION` | MCP revision in `MCP-Protocol-Version` | `2025-06-18` |
| `FORGE_MCP_AUDIT_BACKEND` | `memory` \| `db` | `memory` |

Copy `.env.integration.example` to `.env.integration` and fill in as needed —
values are never committed.

---

## 4. Credentials & token binding (production)

For a **real, authenticated** MCP server, the per-connection token is stored in
the encrypted per-workspace vault (`APIKeyKind.mcp_token`) and referenced from
the connection as `auth.token_ref: secret://mcp/<slug>`. The live transport
resolves it **at call time**, sends `Authorization: Bearer <token>`, and binds it
to the server's canonical URI via the RFC 8707 `resource` indicator
(`auth.resource` or `endpoint`). The token value is never written to
`mcp_connection`, never held in a module global, and never logged. An
authenticated connection with no resource binding is rejected before any byte is
sent.

The full interactive OAuth authorization-code dance against a live IdP is
HARD-10's surface; HARD-05 proves token-binding with a pre-provisioned token.

---

## 5. Security notes (SSRF)

`endpoint`/`resource` are operator-supplied URLs the gateway fetches — a classic
SSRF vector. The transport restricts schemes to `http`/`https`, applies a bounded
timeout, caps the response size, and verifies TLS by default. **Link-local /
metadata-address deny-listing and a human pentest of the transport/SSRF surface
are handed to HARD-09** as a named punch-list item; do not treat the reference
server as coverage for that.
