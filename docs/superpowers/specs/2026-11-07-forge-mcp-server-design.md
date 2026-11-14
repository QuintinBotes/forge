# I — Forge MCP server

**Date:** 2026-11-07
**Status:** Approved, implementation-ready
**Depends on:** H1 (world model read surface)
**Blocks:** J (context tools), L (tervin bridge)
**Decisions:** ADR-0006 (forge serves MCP itself), ADR-0005 (writes are proposals)

---

## 1. Problem

Forge speaks MCP only as a **client**: `apps/mcp-gateway` connects forge's agents to other
people's MCP servers. The only MCP server in the repo is `forge_mcp.reference_server`, a test
double. So:

- Claude Code, Codex, Cursor and tervin cannot read forge's knowledge, world model, specs or runs
  without custom glue. This is lore's main job, done over 42 `lore_*` tools.
- Tervin's rules admit a tool only "as a process, a CLI whose JSON tervin draws, MCP, or `.tervin/`
  data". MCP is the lowest-friction seam into tervin.

## 2. Approach

A Streamable-HTTP MCP endpoint **inside `forge_api`**, mounted at `POST {api_prefix}/mcp`, reusing
forge's API-key authentication, RBAC, workspace scoping and audit. The tool handlers call the same
services the REST routers call; there is no second implementation of any read.

Rejected:

- **Serve it from `apps/mcp-gateway`.** Rejected: the gateway is the *outbound* control plane with
  its own connection registry and threat model (other people's servers are untrusted). Serving
  forge's own data from it would mix trust directions and duplicate auth. Caddy's `/mcp/*` route
  stays the gateway's.
- **A stdio server only.** Rejected: it needs a local forge install and a DB connection on the
  laptop. HTTP + token reaches a team server from any client. A stdio shim can wrap HTTP later (L).
- **An MCP SDK dependency.** Rejected for H1: the surface is four JSON-RPC methods, and the repo
  already speaks the wire protocol in `forge_mcp.transports.jsonrpc` and the reference server.
  Revisit if resources, prompts or server-initiated streams are added.

## 3. Protocol

- JSON-RPC 2.0 over HTTP POST; one request or notification per POST; the response is
  `application/json`. `GET` returns `405` (no server-initiated stream in this phase).
- Protocol versions: `2025-06-18` (preferred) and `2025-03-26`. `initialize` answers with the
  client's requested version when supported, else the preferred one.
- Methods: `initialize`, `notifications/initialized` (HTTP 202, empty body), `ping`, `tools/list`,
  `tools/call`. Anything else is JSON-RPC `-32601`.
- `initialize` result: `serverInfo {name: "forge", version}`, `capabilities {tools: {listChanged:
  false}}`, `instructions` (one paragraph: the two time axes and that writes are proposals).
- No `Mcp-Session-Id`: the server is stateless; every call re-authenticates.

## 4. Security

| Control | Rule |
|---|---|
| Authentication | `Authorization: Bearer <forge API key>` through `get_current_principal`. No token → HTTP 401 before JSON-RPC parsing. |
| Workspace | Every tool is scoped to `principal.workspace_id`. Foreign ids look nonexistent. |
| Authorization | Read tools need `READ`. Write tools are **hidden from `tools/list`** and refused in `tools/call` (`-32003`) unless the principal has `WRITE` or `RUN_AGENT`. |
| Writes are proposals | The only write tool, `world_propose`, creates a `world_proposal`. No MCP call can assert, supersede, retract, merge or decide. |
| DNS rebinding | When an `Origin` header is present it must be in `FORGE_MCP_ALLOWED_ORIGINS` (default: none); otherwise HTTP 403. Non-browser clients send no `Origin`. |
| Audit | Every `tools/call` writes one audit event `mcp.tool_called` with tool, principal, workspace, outcome and duration, plus a SHA-256 of the arguments (never the arguments). |
| Size limits | Request body ≤ 256 KiB (413). `limit` arguments clamp to 100. Text fields in results are truncated to 4,000 characters with `truncated: true`. |
| Redaction | Text results pass through both `forge_knowledge.redaction.redact_secrets` (PEM, `key=value`, JWT, AWS) and `forge_api.observability.redaction.redact_text` (bearer, GitHub, provider keys) before leaving the server; the two cover different patterns. |

## 5. Tools

Every result returns both `structuredContent` (the object) and `content: [{type: "text", text:
<the same object as JSON>}]`, so older clients still work. Domain errors (404, 409, 422) come back
as `isError: true` results with a message, not JSON-RPC errors; protocol errors are JSON-RPC
errors.

| Tool | Arguments | Backed by |
|---|---|---|
| `world_search` | `query`, `type?`, `at?`, `as_of?`, `limit?` | `WorldModelService.search_entities` |
| `world_entity` | `entity_id`, `at?`, `as_of?` | `WorldModelService.entity_profile` |
| `world_facts` | `subject_id?`, `predicate?`, `object_id?`, `layer?`, `at?`, `as_of?`, `limit?` | `WorldModelService.query_facts` |
| `world_changes` | `entity_id`, `since?`, `until?` | `WorldModelService.changes` |
| `world_conflicts` | `at?` | `WorldModelService.conflicts` |
| `world_decision_drift` | `decision_id` | `WorldModelService.drift` |
| `world_propose` *(write)* | `subject`, `phrase`, `object?` XOR `value?`, `valid_from?`, `valid_to?`, `rationale` | `WorldModelService.propose` |

Input schemas are JSON Schema generated from the same Pydantic models the REST router uses, so the
two surfaces cannot drift. `knowledge_search`, `spec_get`, `board_items`, `approvals_pending` and
`run_status` are added by J and L through the same registry; this spec defines the registry they
plug into:

```python
@dataclass(frozen=True)
class McpTool:
    name: str
    description: str
    input_model: type[BaseModel]
    write: bool
    handler: Callable[[ToolContext, BaseModel], BaseModel]
```

## 6. Components

| File | Contents |
|---|---|
| `apps/api/forge_api/mcp_server/protocol.py` | JSON-RPC parsing, version negotiation, error codes; pure |
| `apps/api/forge_api/mcp_server/registry.py` | `McpTool`, `ToolContext`, `ToolRegistry` (list filtered by permission, call with validation) |
| `apps/api/forge_api/mcp_server/world_tools.py` | The seven world tools |
| `apps/api/forge_api/routers/mcp_server.py` | `POST /mcp`, `GET /mcp` → 405; auth, Origin check, body limit, audit |

The router is registered in `FEATURE_ROUTERS` after `world.router`.

## 7. Client setup (documented in the guide)

Claude Code:

```bash
claude mcp add --transport http forge https://forge.example.com/api/v1/mcp \
  --header "Authorization: Bearer $FORGE_TOKEN"
```

Tervin (`mcp.json`, `mcpServers` format, token from an env var — tervin stores no secrets):

```json
{"mcpServers": {"forge": {"type": "http", "url": "https://forge.example.com/api/v1/mcp",
  "headers": {"Authorization": "Bearer ${FORGE_TOKEN}"}}}}
```

## 8. Testing

- Protocol unit tests: version negotiation, notification → 202, unknown method → -32601, malformed
  JSON → -32700, batch arrays rejected (-32600).
- Registry: write tools hidden from a viewer, listed for a member and an agent-runner; calling a
  hidden tool → -32003; invalid arguments → -32602 with the validation message.
- API tests through `TestClient`: 401 without token; 403 for a foreign `Origin`; 413 over 256 KiB;
  every tool's happy path against a seeded workspace; cross-workspace id → `isError` "not found";
  `world_propose` creates a proposal and no fact; one audit row per call with an argument hash and
  no argument text.
- A round-trip test drives the endpoint with forge's own MCP client (`forge_mcp` HTTP transport) —
  forge's client and server must agree on the wire.

## 9. Acceptance criteria

1. `initialize`, `tools/list` and `tools/call` work from forge's own MCP HTTP client against the
   running API.
2. A viewer token lists only read tools; a member or agent-runner also sees `world_propose`.
3. `world_propose` creates a proposal; no MCP call can change the ledger.
4. Every `tools/call` has exactly one audit event without argument text.
5. Foreign-workspace ids are indistinguishable from nonexistent ones.
6. `make lint`, `make typecheck`, `make test` pass.

## 10. What I deliberately does not do

- Resources, prompts, sampling, elicitation, server-initiated streams, sessions.
- OAuth 2.1 authorization-server discovery. Bearer API keys are forge's existing machine
  credential; OAuth for MCP is a follow-up once forge's OIDC provider issues audience-bound tokens
  (RFC 8707 is already understood by forge's MCP *client*).
- lore-compatible `lore_*` aliases. J decides whether a compatibility shim is worth keeping.
