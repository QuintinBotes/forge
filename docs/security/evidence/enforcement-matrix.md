# Security Enforcement Matrix — evidence rendering

> GENERATED from [`security/enforcement-matrix.yaml`](../../../security/enforcement-matrix.yaml)
> by `scripts/security/gen_matrix_evidence.py` — do not edit by hand.
> Every row is asserted on the wired request path by
> `tests/security/test_enforcement_matrix.py` (`uv run pytest -m security -q`).
> `offline` rows run hermetically; `live-db` rows need `FORGE_TEST_DATABASE_URL`
> (pgvector) and skip cleanly without it.

| Control | Title | Mode | Spec source | Verified by |
|---|---|---|---|---|
| `rbac-default-deny` | RBAC role matrix is least-privilege / default-deny | offline | FORGE_SPEC Security: "RBAC — admin, member, viewer, agent-runner roles per workspace" | `check_rbac_default_deny` |
| `rbac-wired-403` | Wired routes return 403 for under-privileged roles | offline | FORGE_SPEC Security (RBAC) / Task 2.3-fix-r2 | `check_rbac_wired_403` |
| `auth-required-401` | Every feature router rejects unauthenticated calls | offline | FORGE_SPEC Security / MORNING_REPORT 2.3 (auth wired) | `check_auth_required_401` |
| `mcp-write-default-deny` | MCP tools are read-only by default; writes fail closed | offline | FORGE_SPEC MCP Security Rules | `check_mcp_write_default_deny` |
| `policy-default-deny` | Repo policy evaluation is default-deny | offline | FORGE_SPEC Repo Policy System | `check_policy_default_deny` |
| `agent-policy-gate` | Agent dispatch never executes a policy-denied tool | offline | FORGE_SPEC Security / Repo Policy System | `check_agent_policy_gate` |
| `redaction-sinks` | Secrets are redacted in audit, trace, and MCP snapshots | offline | FORGE_SPEC Security ("secret redaction in logs") | `check_redaction_sinks` |
| `ssrf-guard` | Outbound URL guard blocks metadata/loopback/private targets | offline | OWASP A10:2021 / HARD-09 threat model | `check_ssrf_guard` |
| `ratelimit-429` | Request rate limiting returns 429 + Retry-After | offline | HARD-09 threat model (DoS) | `check_ratelimit_429` |
| `bodylimit-413` | Oversized request bodies are rejected with 413 | offline | HARD-09 threat model (resource exhaustion) | `check_bodylimit_413` |
| `security-headers` | API responses carry the security header set | offline | HARD-09 threat model (clickjacking / sniffing) | `check_security_headers` |
| `docs-lockdown-prod` | OpenAPI docs are disabled in production by default | offline | HARD-09 threat model (information disclosure) | `check_docs_lockdown_prod` |
| `principal-fails-safe` | Principal.role defaults to the least-privileged role | offline | HARD-09 threat model (fail-open default) | `check_principal_fails_safe` |
| `vault-no-plaintext` | BYOK vault never exposes plaintext | offline | FORGE_SPEC Security ("API keys encrypted at rest") | `check_vault_no_plaintext` |
| `tenant-isolation` | Vault reads are workspace-scoped | offline | FORGE_SPEC Security ("per-workspace isolation") | `check_tenant_isolation` |
| `crypto-default-fernet` | The default cipher is Fernet (authenticated encryption) | offline | HARD-10 handshake (asserted here as a regression pin) | `check_crypto_default_fernet` |
| `secret-key-required-prod` | No silent ephemeral secret key outside development | offline | HARD-10 handshake / SPEC-PRODUCTION-HARDENING creds rule 6 | `check_secret_key_required_prod` |
| `audit-tamper-evident` | The audit hash chain detects tampering; store is append-only | offline | FORGE_SPEC Security ("audit log immutable") | `check_audit_tamper_evident` |
| `webhook-signature-fail-closed` | Webhook ingest rejects unsigned/forged deliveries | offline | FORGE_SPEC Security (webhook verification) | `check_webhook_signature_fail_closed` |
| `cors-lockdown` | CORS is deny-by-default; wildcard never pairs with credentials | offline | Task 2.3-fix-r1 (regression pin) | `check_cors_lockdown` |
| `audit-immutability-trigger` | Postgres trigger rejects UPDATE/DELETE on audit_log | live-db | FORGE_SPEC Security ("audit log immutable") / migration 0022 | `check_audit_immutability_trigger` |
| `pg-vault-roundtrip` | DB-backed secret column round-trips under the real cipher | live-db | HARD-01/HARD-10 handshake | `check_pg_vault_roundtrip` |

## What each row asserts

### `rbac-default-deny`

Full cartesian product of UserRole x Permission against ROLE_PERMISSIONS: viewer=READ only; agent-runner=READ+RUN_AGENT only; member has no MANAGE_KEYS/MANAGE_SECRETS/MANAGE_MEMBERS/ADMIN; an unknown role gets the empty set; `ensure` raises PermissionDeniedError for every denied pair.

### `rbac-wired-403`

Through create_app() with an authenticated principal per role: viewer and agent-runner get 403 from a WRITE-gated route; member gets 403 from a MANAGE_KEYS-gated route; admin passes the same gates.

### `auth-required-401`

For every mounted feature router, every route (except the reviewed public-by-design set) returns 401/403/405/422-without-2xx when called with no credentials, and at least one route per router returns exactly 401.

### `mcp-write-default-deny`

is_write_tool classifies merge_pull_request/approve/un-annotated tools as writes; MCPClient.call_tool raises MCPWriteForbiddenError on a read-only connection (wired path with FakeTransport) and permits read_only=True tools.

### `policy-default-deny`

evaluate() returns DENY for an unlisted action, a write to a non-allowlisted path, a path-traversal write, and an empty tool call.

### `agent-policy-gate`

An AgentRunner driven by a scripted model requesting a denied tool records the DENY decision and the tool handler is never invoked.

### `redaction-sinks`

Bearer tokens, api_key=... pairs, JWTs, and AWS AKIA keys survive in no sink: forge_api.observability.redaction (trace/log), the audit entry payload, and forge_mcp.security.redact (query-through snapshots).

### `ssrf-guard`

assert_safe_url rejects 169.254.169.254, 127.0.0.1, RFC1918, ::1, ULA, 0.0.0.0, and file://; accepts a public IP and an allowlisted host; the forge_knowledge HTTP clients raise through the injected validator.

### `ratelimit-429`

Exceeding FORGE_RATELIMIT_BURST on a wired app returns 429 with a Retry-After header; /health stays exempt.

### `bodylimit-413`

A body over FORGE_MAX_BODY_BYTES returns 413 on the wired app (declared Content-Length and the middleware's streamed-byte cap).

### `security-headers`

Responses carry HSTS, X-Content-Type-Options: nosniff, X-Frame-Options: DENY, Referrer-Policy, and a deny-all CSP on JSON endpoints.

### `docs-lockdown-prod`

With environment=production, /docs /redoc /openapi.json return 404 unless FORGE_DOCS_ENABLED is explicitly set.

### `principal-fails-safe`

A default-constructed Principal has role=VIEWER with no write/admin/run permission.

### `vault-no-plaintext`

StoredSecret.ciphertext != plaintext; repr(StoredSecret) and SecretInfo carry no plaintext; a wrong-key decrypt raises InvalidTokenError.

### `tenant-isolation`

A secret stored for workspace A is not readable or listable via workspace B (SecretNotFoundError; list returns no cross-tenant rows).

### `crypto-default-fernet`

default_cipher() returns FernetCipher and round-trips.

### `secret-key-required-prod`

_resolve_master_key raises RuntimeError when FORGE_SECRET_KEY is unset and FORGE_ENVIRONMENT=production (no ephemeral fallback).

### `audit-tamper-evident`

Mutating any recorded entry breaks verify_chain/verify_integrity; the audit store API exposes no update/delete surface.

### `webhook-signature-fail-closed`

verify_github_signature rejects a missing/forged X-Hub-Signature-256 and accepts only the HMAC over the exact raw bytes (constant-time compare).

### `cors-lockdown`

Default Settings has no origins; a wildcard+credentials misconfiguration does not reflect arbitrary origins with credentials.

### `audit-immutability-trigger`

On a live pgvector database, UPDATE and DELETE against the audit table raise (trigger audit_log_immutable); skips without FORGE_TEST_DATABASE_URL.

### `pg-vault-roundtrip`

An api_key.encrypted_secret row written through FernetCipher decrypts to the original plaintext on a live DB; ciphertext never equals plaintext; skips without FORGE_TEST_DATABASE_URL.

