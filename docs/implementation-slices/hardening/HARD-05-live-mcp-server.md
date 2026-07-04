# HARD-05 — Live MCP Server Over Real Transport (stdio / Streamable-HTTP)

> Phase: hardening · Blocker(s): #1 (no real external systems exercised) · Maps to spec gate **G-MCP** (SPEC-PRODUCTION-HARDENING.md "Real MCP gateway transport" workstream). · Status target: **"verified"** = the `forge_mcp` client/gateway reach a **real MCP server over a real transport** (Streamable-HTTP and/or stdio), a query-through read returns normalized snapshots end-to-end, and the four MCP Security Rules are proven on the *live* path — read-only-by-default deny, RFC 8707 `resource` token binding sent + enforced, namespace scoping, and a redacted audit row persisted. The default hermetic suite stays green and network-free; the live path runs only behind `@pytest.mark.integration` with creds present (a **self-hosted** reference MCP server is the preferred substrate, so the only "cred" is a local server URL + optional token — no external SaaS required).

---

## 1. Intent — what & why

The ALPHA built the entire MCP control plane — `forge_mcp` SDK (client, manager, security, audit, query-through), the `apps/mcp-gateway` FastAPI service, and the `apps/api` `/mcp/*` router — and proved every security rule against an in-memory `forge_mcp.testing.FakeTransport`. The single load-bearing seam, `forge_mcp.transport.Transport`, has exactly **two** concrete implementations today: `FakeTransport` (tests/fixtures) and `NullTransport` (refuses all live traffic). MORNING_REPORT §1.12 and §6 are explicit: *"MCP SDK + gateway … DONE (live transport mocked)"* and *"GitHub, Slack, MCP transport … are all fixture/mock-backed. No interaction with a real external system has ever happened."*

This is blocker #1 for the MCP subsystem: every claim about MCP — read-only default, RFC 8707 token binding, namespace scoping, redacted audit — is currently a claim about how the client *orchestrates a fake*, never about how it behaves against a server that speaks the real MCP wire protocol (JSON-RPC 2.0 over Streamable-HTTP per MCP revision 2025-06-18, or over stdio). A server could, for example, advertise a tool with no `readOnlyHint`, return content with embedded secrets, or expose resources in namespaces the connection never allow-listed; the fixtures can simulate these, but only a real round-trip proves the SDK enforces the rules against bytes it did not author.

HARD-05 writes the two real `Transport` implementations (`HttpMcpTransport`, `StdioMcpTransport`), a credential-aware `TransportFactory` that resolves the token from the vault and binds it to the server (RFC 8707), wires those into the gateway and API manager behind an explicit enablement flag, makes MCP audit persist to the platform audit store (so the audit row is real and immutable, not in-memory), and stands up a **self-hosted reference MCP server** so the whole path is exercised end-to-end in CI without any third-party SaaS. It changes **no** product surface and **no** frozen contract — it makes the existing `Transport` protocol have a real implementation and proves the FORGE_SPEC "MCP Security Rules" against a live server.

This extends existing packages only: `packages/mcp-sdk` (`forge_mcp`), `apps/mcp-gateway` (`forge_mcp_gateway`), `apps/api` (`forge_api.routers.mcp`, `forge_api.observability.audit`). No new `forge_*` package is created.

## 2. User-facing / operator behavior

MCP is operator/agent-facing, not end-user-facing; its observable behavior is through the gateway/API and the audit trail.

- **Journey A — Operator registers a real MCP server and reads from it.** An admin POSTs an `MCPConnection` to `/mcp/connections` describing a real server (`transport: http`, `endpoint: https://mcp.internal/confluence`, `auth.type: oauth`, `auth.token_ref: secret://mcp/confluence`, `auth.resource: https://mcp.internal/confluence`, `allow_write: false`, `allowed_namespaces: [engineering]`). With live transport enabled, the gateway opens a real session (`initialize` handshake), and `GET …/resources` returns the server's actual resource list filtered to the allow-listed namespaces. `GET …/resources/read?uri=…` returns the server's real content, redacted.
- **Journey B — Agent query-through during retrieval.** A retrieval scope includes an `mcp_query_through` source. `forge_mcp.query_through(client, query)` lists+reads in-scope resources from the **live** server at query time and returns attributed `RetrievedChunk`s (`chunk_type=mcp_resource`, `source_uri="confluence://…"`), already-redacted, fed into the F05 fusion pool. The data is always-fresh because it is read live, not indexed.
- **Journey C — A write is refused by default.** An agent (or operator) attempts `POST …/tools/call` with a mutating tool (`create_page`, or any un-annotated/unknown verb) on a `allow_write:false` connection. The call is denied **before any byte hits the server** → HTTP 403, and a `forbidden`-status audit row is written. Writes only succeed after an admin explicitly re-registers the connection with `allow_write: true` *and* policy permits the action.
- **Journey D — Token binding is enforced.** When the connection is authenticated, the live transport sends the RFC 8707 `resource` indicator bound to the server's canonical URI on the OAuth token request / in the session; a connection that is authenticated but carries no `resource` (and no `endpoint` to fall back to) is rejected at `connect()` time with a security error — so a bearer token can never be replayed against a different audience.
- **Journey E — Audit is real and queryable.** Every list/read/call against the live server writes a redacted, append-only `MCPAuditEntry` to the platform audit store (Postgres in deploy), with the tool/resource name, a payload hash (secret-free), status, and latency. `GET …/audit` returns the trail; no secret substring ever appears in it.

The operator can degrade safely: with `MCP_LIVE_TRANSPORT` unset/false the system behaves exactly as ALPHA (NullTransport → 503 on any live op), so the live path is opt-in and never implicit.

## 3. Vertical slice

### 3.1 Data model (tables/columns/migrations touched)

No **new** tables; HARD-05 conforms to the existing `forge_db` schema.

- **`mcp_connection`** (`packages/db/forge_db/models/connections.py`, `WorkspaceScopedModel`, singular table — already present): carries `transport` (`enum_type(MCPTransport)`: `http|stdio|sse`), `endpoint`, `auth_type` (`enum_type(MCPAuthType)`: `oauth|api_key|none`), `allow_write` (default **false**), `allowed_namespaces` (json), and `resource_param` (the RFC 8707 `resource` value). HARD-05 **uses** these columns to build live connections; it adds **no** column. The one optional, additive migration (see below) is for token-ref storage if not already covered.
- **Token reference, not token value.** The connection persists only `auth.token_ref` (a `secret://mcp/<slug>` pointer); the actual token lives in the encrypted per-workspace vault (`forge_api.auth.vault`, `APIKeyKind.mcp_token` — confirm the enum member name during build; add it if absent as the single additive enum change). No token value is ever written to `mcp_connection`.
- **Audit persistence.** MCP audit currently lands in `forge_mcp.audit.InMemoryAuditLog` only. HARD-05 routes it to the platform audit store via `forge_api.observability.audit.AuditLog.record_mcp_call(...)` (this method already exists, per `apps/api/forge_api/observability/audit.py:280`), whose `AuditStore` protocol (`apps/api/forge_api/observability/audit.py:109`) has an `InMemoryAuditStore` today and a documented Postgres swap. HARD-05 implements/activates the Postgres-backed `AuditStore` so the MCP audit row is durable and immutable (the immutability trigger is delivered by HARD-01). **Optional additive migration** `migrations/versions/00xx_mcp_audit_store.py` only if a dedicated audit table is chosen over the shared platform audit table — prefer the existing platform audit table; no new table unless the schema review requires it.

### 3.2 Backend (FastAPI routes + services/packages)

No new routes — the `/mcp/*` surface (`apps/api/forge_api/routers/mcp.py`) and the gateway routes (`apps/mcp-gateway/forge_mcp_gateway/app.py`) are unchanged in shape. The change is **what the manager's transport factory produces**.

New module `packages/mcp-sdk/forge_mcp/transports/` (extends the existing `forge_mcp.transport` seam; the singular `transport.py` stays as the protocol home and re-exports for back-compat):

```
packages/mcp-sdk/forge_mcp/transports/
├── __init__.py          # exports HttpMcpTransport, StdioMcpTransport, live_transport_factory
├── jsonrpc.py           # minimal JSON-RPC 2.0 envelope: request/response, id mgmt, error→exception
├── http.py              # HttpMcpTransport — MCP Streamable-HTTP (2025-06-18), httpx.Client (sync)
├── stdio.py             # StdioMcpTransport — spawn server subprocess, frame over stdin/stdout
└── factory.py           # live_transport_factory(conn, *, token_resolver) -> Transport
```

- **`HttpMcpTransport`** implements the existing `forge_mcp.transport.Transport` protocol (sync `list_resources`/`read_resource`/`list_tools`/`call_tool`) by speaking MCP over **Streamable-HTTP** (single endpoint; client→server is JSON-RPC 2.0 POST, server may stream via SSE). On construction it performs `initialize` (sending `MCP-Protocol-Version: 2025-06-18` and capturing the `Mcp-Session-Id` header), then maps protocol methods: `resources/list` → `list[MCPResource]`, `resources/read` → `MCPResourceContent`, `tools/list` → `list[ToolSpec]` (carrying `readOnlyHint`/`destructiveHint` into `ToolSpec.read_only`/`destructive`), `tools/call` → raw result. It sends `Authorization: Bearer <token>` and the RFC 8707 `resource` indicator (from `conn.auth.resource or conn.endpoint`). Uses `httpx.Client` with a bounded timeout; **never** logs headers or bodies (redaction-on-error).
- **`StdioMcpTransport`** spawns the server process (command from config/env), writes newline-delimited JSON-RPC to stdin, reads framed responses from stdout, and tears the process down on close. Same method mapping. For stdio, "token binding" is N/A (no network audience) but namespace scoping and read-only default still apply.
- **`live_transport_factory(conn, *, token_resolver)`** is a `forge_mcp.manager.TransportFactory`: it inspects `conn.transport` (`http|stdio|sse`) and returns the matching transport, resolving `conn.auth.token_ref` through an injected `token_resolver` (the vault) **at construction time**, never holding the value in a module global. `sse` (legacy) maps to the HTTP transport's SSE-compat mode or raises a clear "use http" error.
- **Manager wiring.** `MCPConnectionManager.__init__` already accepts `transport_factory`. The gateway (`apps/mcp-gateway/forge_mcp_gateway/app.py` `create_gateway_app`) and the API router (`apps/api/forge_api/routers/mcp.py` `_mcp_manager_singleton`) construct the manager with `live_transport_factory` **only when `MCP_LIVE_TRANSPORT` is truthy**; otherwise they keep the default `NullTransport` factory (ALPHA behavior). The audit sink passed to the manager becomes the Postgres-backed sink in deploy.
- **Audit bridge.** A thin `forge_mcp.audit.AuditSink` adapter (`forge_api`-side) forwards each `MCPAuditEntry` to `AuditLog.record_mcp_call`, so the gateway's `GET …/audit` and the platform observability audit share one durable, redacted trail.

### 3.3 Worker / agent runtime

No new Celery task. The agent runtime consumes MCP only through retrieval query-through (`forge_mcp.query_through`) and policy-gated tool dispatch — both already route through `MCPGatewayClient`, so once the manager has a live transport, the agent path is live with no agent-runtime code change. The bounded-loop / policy backstops are unchanged. (Cross-reference: HARD-02 exercises the agent run; HARD-05 only needs to guarantee `query_through` works against the live client, asserted in §7.)

### 3.4 Frontend

None. MCP is operator/agent-facing; no Next.js change. (The connection-management UI, if any, already targets the unchanged `/mcp/*` routes.)

### 3.5 Infra / deploy / CI

- **Self-hosted reference MCP server.** Add `deploy/mcp-reference/` — a minimal, self-hosted MCP server (preferred: a small FastMCP/JSON-RPC HTTP server seeded with the same fixture corpus as `forge_mcp.testing` — multiple namespaces, one read tool, one write tool, one resource containing a planted secret to exercise redaction). This is the **integration substrate**: no external SaaS, runnable in CI as a service container. A `docker-compose.integration.yml` overlay (or a CI service) runs it at `MCP_SERVER_URL`.
- **stdio substrate.** For the stdio path, the reference server is also runnable as a stdio subprocess (`MCP_STDIO_COMMAND`), or a pinned off-the-shelf stdio server (e.g. a filesystem server) over a temp dir.
- **CI lane.** A new `mcp-integration` job (gated, runs when `MCP_SERVER_URL` is set / on the integration workflow) starts the reference server, exports `MCP_LIVE_TRANSPORT=true`, and runs `uv run pytest -m integration -k mcp`. The default `ci.yml` test job stays hermetic (no live transport, integration tests skip-clean).
- **Compose.** `apps/mcp-gateway` already exists in `deploy/docker-compose.yml` (service `mcp-gateway`, port 8001, healthcheck, non-root `1000:1000`, `mcp` + `backend` networks, resource limits). HARD-05 adds the live-transport env wiring (`MCP_LIVE_TRANSPORT`, server URL/token from secrets) to that service block — it does **not** add a new gateway service. Image digest pinning is HARD-08's job.
- **`.env.integration.example`** gains the MCP key names (values never committed).

## 4. Public interfaces / contracts (exact signatures, env vars, config keys)

**Frozen contracts re-used unchanged** (`forge_contracts`): `MCPConnection`, `MCPAuth(type, token_ref, resource)`, `MCPCapabilities`, `MCPResource`, `MCPResourceContent`, `MCPToolResult`, `MCPAuditEntry`; enums `MCPTransport(http|stdio|sse)`, `MCPAuthType(oauth|api_key|none)`. The `forge_contracts.MCPClient` protocol and `forge_mcp.transport.Transport` protocol are **not** modified — HARD-05 adds implementations of the latter.

`packages/mcp-sdk/forge_mcp/transports/http.py`:

```python
class HttpMcpTransport:  # implements forge_mcp.transport.Transport (sync)
    def __init__(
        self,
        endpoint: str,
        *,
        token: str | None = None,          # resolved from vault by the factory; never logged
        resource: str | None = None,       # RFC 8707 resource indicator (audience binding)
        protocol_version: str = "2025-06-18",
        timeout_s: float = 30.0,
        client: httpx.Client | None = None,  # injectable for tests
    ) -> None: ...
    def list_resources(self) -> list[MCPResource]: ...
    def read_resource(self, uri: str) -> MCPResourceContent: ...
    def list_tools(self) -> list[ToolSpec]: ...           # maps readOnlyHint/destructiveHint
    def call_tool(self, name: str, arguments: Mapping[str, Any]) -> Any: ...
    def close(self) -> None: ...
```

`packages/mcp-sdk/forge_mcp/transports/stdio.py`:

```python
class StdioMcpTransport:  # implements forge_mcp.transport.Transport (sync)
    def __init__(self, command: Sequence[str], *, cwd: str | None = None,
                 env: Mapping[str, str] | None = None, timeout_s: float = 30.0) -> None: ...
    # same four Transport methods + close()
```

`packages/mcp-sdk/forge_mcp/transports/factory.py`:

```python
TokenResolver = Callable[[MCPConnection], str | None]  # vault-backed; returns None for auth.none

def live_transport_factory(*, token_resolver: TokenResolver) -> TransportFactory:
    """Return a TransportFactory that builds a real transport per connection.
    http/sse -> HttpMcpTransport(endpoint, token=resolve(conn), resource=token_binding(conn));
    stdio    -> StdioMcpTransport(command_from(conn)).
    Raises MCPSecurityError on an authed http connection with no resource binding."""
```

`packages/mcp-sdk/forge_mcp/transports/jsonrpc.py`: `JsonRpcError(ForgeError)` carrying `code`/`message`; a `call(method, params) -> result` helper that raises `JsonRpcError` on a JSON-RPC error object and `MCPTransportUnavailableError` on connection failure.

**Environment variables** (read in `apps/mcp-gateway` and `apps/api`; values from gitignored `.env.integration`):

| Var | Purpose | Default |
|---|---|---|
| `MCP_LIVE_TRANSPORT` | master switch; when falsy, NullTransport (ALPHA behavior) | `false` |
| `MCP_SERVER_URL` | reference/real server endpoint for the integration lane | unset → tests skip |
| `MCP_TOKEN` | bearer token for the test server (if authed) | unset |
| `MCP_RESOURCE` | RFC 8707 resource indicator override | falls back to endpoint |
| `MCP_TRANSPORT` | `http` \| `stdio` for the integration test target | `http` |
| `MCP_STDIO_COMMAND` | command line for the stdio reference server | unset |
| `MCP_HTTP_TIMEOUT_S` | per-request timeout | `30` |
| `MCP_PROTOCOL_VERSION` | MCP revision sent in `MCP-Protocol-Version` | `2025-06-18` |
| `FORGE_MCP_AUDIT_BACKEND` | `memory` \| `db` (db persists via platform AuditStore) | `memory` |

**Config keys** (per-connection, already in the `MCPConnection` DTO / `mcp_connection` row): `transport`, `endpoint`, `auth.type`, `auth.token_ref`, `auth.resource`/`resource_param`, `allow_write` (must stay false by default), `allowed_namespaces`, `index_strategy`. Token ref format `secret://mcp/<slug>` resolved via the vault (`APIKeyKind.mcp_token`).

## 5. Dependencies (other slices / foundation that must exist first)

- **HARD-01 — Real Postgres + pgvector substrate** (REQUIRED for the durable-audit gate): the audit-immutability trigger and the Postgres `AuditStore` are exercised on a live DB there. The MCP live-read tests themselves do not need Postgres; the *audit persistence* acceptance criterion does.
- **HARD-10 — Production crypto + OAuth seam** (REQUIRED for authed servers): the vault must be the real `FernetCipher` with `FORGE_SECRET_KEY` so `auth.token_ref` resolves a real, decryptable `mcp_token`. With an unauthenticated self-hosted reference server (`auth.type: none`), HARD-05's read/scoping/audit criteria can be met **without** HARD-10; token-binding-with-a-real-token needs it.
- **Foundation (already present):** `forge_mcp` SDK (transport/client/manager/security/audit/query_through), `apps/mcp-gateway`, `apps/api` `/mcp/*` router with RBAC (`require_permission`) + tenancy (`workspace_id`), `forge_contracts` MCP DTOs/enums (frozen), `forge_db.models.connections.MCPConnection`, `forge_api.observability.audit.AuditLog.record_mcp_call` + `AuditStore` protocol, root `conftest.py` `postgres_url`/`pg_engine` fixtures, the `integration` + `postgres` pytest markers (declared in `pyproject.toml`).
- **Credential handling rules** from SPEC-PRODUCTION-HARDENING.md "Credentials & secrets handling" (env-only ingress, never logged/committed, resolved at call time, skip-clean when absent).
- **SOFT:** HARD-02 (live agent run) consumes the live MCP client via query-through/tool dispatch but is not required for HARD-05's own gate.

## 6. Acceptance criteria (numbered, testable; offline vs real-creds marked)

1. **[offline]** `HttpMcpTransport` and `StdioMcpTransport` each satisfy the `forge_mcp.transport.Transport` protocol (`isinstance(..., Transport)` runtime-checkable passes) and the existing `MCPGatewayClient`/`MCPConnectionManager` accept them with no contract change.
2. **[offline]** With an injected `httpx.MockTransport` (HTTP) and a fake subprocess (stdio), `list_resources`/`read_resource`/`list_tools`/`call_tool` correctly encode JSON-RPC 2.0 requests and decode responses, including mapping `readOnlyHint`/`destructiveHint` → `ToolSpec.read_only`/`destructive`. (Unit-level wire correctness, no network.)
3. **[offline]** `live_transport_factory` builds the right transport per `conn.transport`; an authenticated `http` connection with neither `auth.resource` nor `endpoint` raises `MCPSecurityError` (token-binding precondition) **before** any I/O.
4. **[real-creds: self-hosted server]** Against the running reference MCP server (`MCP_SERVER_URL`, `MCP_LIVE_TRANSPORT=true`), a live `read_resource` over **real HTTP** returns a normalized `MCPResourceContent`, and `forge_mcp.query_through(client, query)` returns attributed `RetrievedChunk`s (`chunk_type=mcp_resource`, non-empty `source_uri`) — full end-to-end query-through. *(integration marker; skips clean when `MCP_SERVER_URL` unset.)*
5. **[real-creds]** A write/tool call (`create_page`, or any un-annotated/unknown-verb tool) on a `allow_write:false` connection is **denied by default** on the live path → `MCPWriteForbiddenError` (HTTP 403) and the server is never contacted for that call; the same call succeeds only when the connection is re-registered `allow_write:true` *and* policy permits.
6. **[real-creds]** RFC 8707 token binding is sent on the authed live request (asserted by inspecting the outbound request via the reference server's echo/inspection endpoint or a request-capturing proxy): the `resource` indicator equals the server's canonical URI; a connection whose token lacks the binding is rejected by policy/precondition.
7. **[real-creds]** Namespace scoping holds on the live server: with `allowed_namespaces:[engineering]`, a `read_resource` for a `finance://…` URI raises `MCPNamespaceError` (403) without reaching the server, and `list_resources` returns only in-scope resources even though the server advertises more.
8. **[real-creds]** A redacted `MCPAuditEntry` is written for **every** live list/read/call: with `FORGE_MCP_AUDIT_BACKEND=db` the row lands in the platform `AuditStore` (Postgres), carries the tool/resource name + a secret-free `payload_hash` + status + `latency_ms`, and contains **no** secret substring; a planted secret in the server's resource content is `[redacted]` in both the returned content and any logged trace. *(durable-row assertion needs HARD-01's Postgres; the redaction assertion runs offline against the in-memory sink too.)*
9. **[offline]** Degradation/safety: with `MCP_LIVE_TRANSPORT` unset, every `/mcp/*` live operation returns 503 (NullTransport) exactly as ALPHA; the hermetic suite stays green and makes **no** network call (verified by a no-socket guard in the unit lane).
10. **[real-creds]** Error paths: server 5xx / connection refused / JSON-RPC error / timeout each map to the correct SDK exception (`MCPTransportUnavailableError`/`JsonRpcError`) and produce an `error`-status audit row; secrets never appear in the error message.
11. **[offline]** No secret leakage anywhere: a grep/assert over source, fixtures, lockfile, and captured CI logs finds no real token value; `auth.token_ref` (not the token) is the only thing persisted to `mcp_connection`.
12. **[offline]** Whole-suite green gate holds at the end of the workstream: `uv run pytest -q`, `uv run ruff check .`, `uv run ruff format --check .`, `make typecheck` (exit 0), `cd apps/web && pnpm test` — with the integration tests skipping cleanly when creds/server are absent.

## 7. Test plan (TDD) — unit + integration (gated on env creds) + how to run

Write tests first. Unit tests are hermetic; integration tests carry `@pytest.mark.integration` and skip cleanly when `MCP_SERVER_URL`/`MCP_LIVE_TRANSPORT` are absent (mirrors the `postgres` marker pattern in `conftest.py`). Tests live in `packages/mcp-sdk/tests/` and `apps/mcp-gateway/tests/` + `apps/api/tests/`.

**Unit (no network — `httpx.MockTransport` for HTTP, a fake `subprocess` for stdio):**
- `test_http_transport_conforms_to_protocol` / `test_stdio_transport_conforms_to_protocol` (AC1).
- `test_http_jsonrpc_request_encoding` — asserts method names (`resources/list`, `resources/read`, `tools/list`, `tools/call`), `MCP-Protocol-Version` header, and `Mcp-Session-Id` re-use after `initialize` (AC2).
- `test_tool_hint_mapping` — `readOnlyHint:false`/`destructiveHint:true` → `ToolSpec` flags, exercising `is_write_tool` fail-closed default for un-annotated tools (AC2/AC5).
- `test_factory_selects_transport_by_conn_transport` and `test_factory_rejects_authed_http_without_resource_binding` (AC3).
- `test_redaction_on_http_error` — a server error body containing a bearer token is `[redacted]` in the raised message/log (AC10/AC11).
- `test_no_live_transport_is_503` — manager with default factory → `MCPTransportUnavailableError` (AC9); plus a socket-blocking fixture asserting the unit lane opens no sockets.

**Integration (gated; against the self-hosted reference server):**
- `test_live_read_and_query_through` — real HTTP read + `query_through` returns attributed chunks (AC4).
- `test_live_write_denied_by_default_then_allowed` (AC5).
- `test_live_token_binding_resource_sent` — inspect the captured outbound request's `resource` (AC6).
- `test_live_namespace_scoping_blocks_out_of_scope` (AC7).
- `test_live_audit_row_persisted_and_redacted` — with `FORGE_MCP_AUDIT_BACKEND=db` + `pg_engine`, assert the durable redacted row; planted secret is `[redacted]` (AC8).
- `test_live_stdio_transport_read` — same read over the stdio substrate (AC4 via stdio).
- `test_live_error_paths_map_to_exceptions` (AC10).

**How to run:**
```bash
# Hermetic default (no network, integration skips):
uv run pytest packages/mcp-sdk apps/mcp-gateway apps/api -q

# Integration lane (self-hosted reference server, no external SaaS):
docker compose -f deploy/docker-compose.integration.yml up -d mcp-reference
export MCP_LIVE_TRANSPORT=true MCP_SERVER_URL=http://localhost:8901 \
       FORGE_MCP_AUDIT_BACKEND=db FORGE_TEST_DATABASE_URL=postgresql+psycopg://...
uv run pytest -m integration -k mcp -q
```

## 8. Security & policy considerations

- **Read-only default (FORGE_SPEC MCP rule 1).** `MCPConnection.allow_write` defaults false in both the DTO and the `mcp_connection` row; `is_write_tool` fails closed (un-annotated/unknown verb ⇒ write). The live transport does **not** relax this — the client refuses the call before any byte reaches the server (AC5).
- **RFC 8707 token binding (rule 2).** The HTTP transport sends the `resource` indicator (audience binding) so a stolen token cannot be replayed against a different MCP server; `connect()` already rejects an authed connection with no binding. The token is resolved from the vault per request and discarded — never a module global, never logged (AC6, AC11).
- **Namespace scoping (rule 5).** Enforced client-side by `resource_in_scope`/`filter_resources` **and** re-checked on the live read path; an out-of-scope URI never reaches the server (AC7).
- **Audit + redaction (rules 4 & 6).** Every live op writes a redacted `MCPAuditEntry`; with `db` backend it is durable and immutable (HARD-01 trigger). `forge_mcp.security.redact` masks sensitive keys, bearer tokens, `key=value` secrets, and JWTs in both returned content and audit/trace; the live path re-applies redaction defensively to server-authored content (AC8).
- **Input validation (rule 3).** Tool name/arguments validated before dispatch (existing client logic), preserved on the live path.
- **SSRF surface.** `endpoint` and `resource` are operator-supplied URLs the gateway will fetch — a classic SSRF vector. Mitigation: the live transport restricts schemes to `https` (and `http` only for explicitly allow-listed internal hosts), honors a deny-list for link-local/metadata addresses, and applies a bounded timeout. This is an explicit line item handed to **HARD-09**'s pentest punch-list (attack surface: "SSRF via MCP/embedder URLs").
- **Transport hardening.** Bounded timeouts, response-size caps, and TLS verification on by default; stdio servers run with a constrained `env` and `cwd`.
- **Tenancy.** The API router already scopes every op by `workspace_id`; cross-tenant connection enumeration/read/call/audit returns 404 (existing manager behavior), unchanged.

## 9. Effort & risk (S/M/L + risks; what cannot be done in-sandbox)

**Effort: M.** Two transport implementations + JSON-RPC envelope + factory + audit bridge + a small reference server + tests. The protocol surface is small (5 JSON-RPC methods), the security logic already exists, and the seam (`Transport` + `TransportFactory`) is purpose-built for this — the work is "fill in the one real implementation behind a clean interface," not redesign.

Risks:
- **MCP protocol drift (Medium).** The MCP transport spec evolved (HTTP+SSE in 2024-11-05 → Streamable-HTTP in 2025-03/2025-06-18). Mitigation: pin `MCP-Protocol-Version: 2025-06-18`, negotiate in `initialize`, keep the wire code isolated in `jsonrpc.py`/`http.py` behind the stable `Transport` protocol so a future revision is a localized change.
- **Reference-server fidelity (Medium).** A self-hosted reference server may not exercise every quirk of a real third-party server (auth flows, pagination, partial content). Mitigation: seed it from the same fixture corpus the security tests already trust, and additionally smoke-test against at least one off-the-shelf server (stdio filesystem/everything server) so two independent implementations are hit.
- **Sync transport vs async I/O (Low–Medium).** The frozen `Transport` protocol is synchronous; live HTTP uses `httpx.Client` (sync). Keeps consistency with the existing SDK but means no concurrency within a single transport call — acceptable for control-plane + query-through volumes; documented.
- **SSRF (Medium).** Operator-supplied fetch URLs — mitigated as in §8 and escalated to HARD-09.
- **Cannot be done in-sandbox / hand-offs:** the *no-network sandbox* cannot run the live HTTP/stdio round-trip — the integration lane requires a **networked/CI runner** with the reference server up. A genuine **third-party SaaS MCP server** (e.g. a hosted Confluence/Jira MCP) and a **human pentest of the SSRF/transport surface** are explicitly **out of agent scope**: HARD-05 proves the path against a self-hosted reference server and hands the SaaS + pentest items to HARD-09's punch-list. No real third-party MCP credential is required for the gate.

## 10. Key files / paths (exact, in the real monorepo)

- `packages/mcp-sdk/forge_mcp/transport.py` — existing `Transport` protocol + `NullTransport` + `ToolSpec` (home of the seam; re-export the new transports).
- `packages/mcp-sdk/forge_mcp/transports/__init__.py` — **new**: package exports.
- `packages/mcp-sdk/forge_mcp/transports/jsonrpc.py` — **new**: JSON-RPC 2.0 envelope + `JsonRpcError`.
- `packages/mcp-sdk/forge_mcp/transports/http.py` — **new**: `HttpMcpTransport` (Streamable-HTTP).
- `packages/mcp-sdk/forge_mcp/transports/stdio.py` — **new**: `StdioMcpTransport`.
- `packages/mcp-sdk/forge_mcp/transports/factory.py` — **new**: `live_transport_factory` + `TokenResolver`.
- `packages/mcp-sdk/forge_mcp/manager.py` — accept the live factory (already parameterized); no signature change.
- `packages/mcp-sdk/forge_mcp/client.py` — unchanged (works against any `Transport`).
- `packages/mcp-sdk/forge_mcp/security.py` — unchanged (`token_binding`, `redact`, scoping reused).
- `packages/mcp-sdk/forge_mcp/audit.py` — add a `forge_api`-bound `AuditSink` adapter (or keep adapter in `forge_api`); no contract change.
- `packages/mcp-sdk/forge_mcp/__init__.py` — export the new transports/factory.
- `packages/mcp-sdk/forge_mcp/testing.py` — extend fixtures for the reference-server corpus (already has the planted secret).
- `packages/mcp-sdk/tests/test_transports_http.py`, `test_transports_stdio.py`, `test_transport_factory.py`, `test_live_mcp_integration.py` — **new**.
- `apps/mcp-gateway/forge_mcp_gateway/app.py` — wire `live_transport_factory` + db audit sink behind `MCP_LIVE_TRANSPORT`.
- `apps/mcp-gateway/tests/test_gateway_live.py` — **new** (integration-gated).
- `apps/api/forge_api/routers/mcp.py` — `_mcp_manager_singleton` builds live factory when enabled.
- `apps/api/forge_api/observability/audit.py` — activate Postgres-backed `AuditStore`; `record_mcp_call` is the bridge target (line ~280).
- `apps/api/tests/test_mcp_router.py` — extend with live-gated cases.
- `deploy/mcp-reference/` — **new**: self-hosted reference MCP server + Dockerfile.
- `deploy/docker-compose.integration.yml` — **new** overlay: `mcp-reference` service for the integration lane.
- `deploy/docker-compose.yml` — `mcp-gateway` service block: add live-transport env (no new service).
- `.github/workflows/ci.yml` — add the gated `mcp-integration` job.
- `.env.integration.example` — add MCP key names.
- `pyproject.toml` — `integration` marker already declared; pin `httpx` (already a dep) and any reference-server dep.

## 11. Research references

- FORGE_SPEC.md → "MCP Security Rules" (read-only default, RFC 8707 token binding, namespace scoping, full audit log), "MCP Connection Schema", "MCP query-through".
- SPEC-PRODUCTION-HARDENING.md → workstream "Real MCP gateway transport" (gate **G-MCP**), "Credentials & secrets handling", BETA DoD item 7.
- MORNING_REPORT.md → §1.12 ("MCP SDK + gateway DONE — live transport mocked"), §5(4) (live HTTP calls parked), §6 ("MCP transport … fixture/mock-backed").
- MCP specification, revision 2025-06-18 — Streamable-HTTP transport, JSON-RPC 2.0 methods (`initialize`, `resources/list|read`, `tools/list|call`), `Mcp-Session-Id` / `MCP-Protocol-Version` headers, `readOnlyHint`/`destructiveHint` tool annotations: https://modelcontextprotocol.io/specification/2025-06-18
- MCP Authorization (Resource Indicators / audience binding): https://modelcontextprotocol.io/specification/2025-06-18/basic/authorization
- RFC 8707 — Resource Indicators for OAuth 2.0: https://www.rfc-editor.org/rfc/rfc8707
- Reference servers (stdio/http substrate for smoke tests): https://github.com/modelcontextprotocol/servers
- F05-hybrid-knowledge-retrieval.md → MCP query-through candidate normalization (`mcp_resource` chunk type, `source_uri` provenance) consumed by retrieval fusion.

## 12. Out of scope / future

- **Third-party SaaS MCP servers** (hosted Confluence/Jira/GitHub MCP) and any external MCP credential — HARD-05 proves the path against a self-hosted reference server; SaaS connectors are a follow-on and a HARD-09 pentest line item.
- **MCP `sync_and_index` mode** (periodic pull of MCP resources into the local pgvector index) — V2 / F05 future; HARD-05 covers the live `query_through` path only.
- **Full OAuth dynamic client registration / interactive authorization-code flow against an MCP authorization server** — token-binding is proven with a pre-provisioned token (vault) + the `resource` indicator; the interactive OAuth dance against a live IdP is HARD-10's surface.
- **MCP `prompts/*` and `sampling/*` methods** — not exercised by Forge's retrieval/tool paths in this release; transport leaves room but they are untested here.
- **Async transport / streaming partial results** — the sync `Transport` protocol is retained; an async variant is a future protocol revision.
- **SSE-only legacy servers (2024-11-05 HTTP+SSE)** — mapped to a clear "use Streamable-HTTP" error or a thin compat shim; full legacy support is out of scope.
- **Human pentest of the transport/SSRF surface** — handed to HARD-09 as a named, scoped punch-list item (cannot be performed by build agents).
