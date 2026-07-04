# Forge Threat Model (STRIDE)

> HARD-09 deliverable. This models the real Forge attack surface with STRIDE
> (Spoofing, Tampering, Repudiation, Information disclosure, Denial of service,
> Elevation of privilege). Each surface lists the threats, the **existing**
> controls, and any **residual** risk (which is carried into
> [`pentest-punch-list.md`](pentest-punch-list.md)). Controls marked ✅ are
> asserted on the wired path by `uv run pytest -m security` (see
> [`evidence/enforcement-matrix.md`](evidence/enforcement-matrix.md)).

## System overview & trust boundaries

```
            (untrusted)                    (semi-trusted)              (trusted)
 Browser ───► Web (Next.js) ──HTTP──► API (FastAPI) ──► Postgres/pgvector
                                        │  │  │            Redis (queue)
                                        │  │  └─► BYOK Vault (encrypted-at-rest)
                                        │  └────► Worker ──► Agent sandbox (git worktree)
                                        │                     │
                                        └────► MCP Gateway ───┴─► external MCP servers
                                                              └─► embedder / reranker (BYOK URLs)
                          webhooks ◄───── GitHub / Slack / PM / alert providers
```

Trust boundaries crossed: browser→API (authn/z), API→DB (tenant isolation),
API/worker→external hosts (SSRF, TLS, BYOK secrets), external→API (webhook
signature verification), operator-config→outbound (admin-set embedder/reranker/
MCP/OAuth URLs).

---

## S1 — Authentication & RBAC (API edge)

| STRIDE | Threat | Control |
|---|---|---|
| **S** | Anonymous caller acts as a user/admin | API-key auth wired into `get_current_principal`; every feature router rejects missing/invalid creds with 401 ✅ (`auth-required-401`). No hardcoded principal. |
| **E** | Under-privileged role performs a write/admin action | Deny-by-default RBAC matrix (`forge_api.auth.rbac`); `require_permission` returns 403 per role ✅ (`rbac-default-deny`, `rbac-wired-403`). |
| **E** | Partially-constructed `Principal` defaults to admin (fail-open) | `Principal.role` defaults to **VIEWER** ✅ (`principal-fails-safe`). |
| **S** | Session-JWT / OAuth spoofing | HS256 session JWT verified against the shared `AUTH_SECRET`; OAuth code exchange is server-side; RFC 8707 resource indicators bound for MCP tokens. |
| **T/R** | CORS credential leak via reflected origin | CORS deny-by-default; wildcard never paired with credentials ✅ (`cors-lockdown`). |

**Residual:** depth of multi-team / conditional RBAC (F29/F30) is out of V1
scope; the human pentest should probe role-confusion across workspace switches
(**PT-01**).

## S2 — BYOK secret vault

| STRIDE | Threat | Control |
|---|---|---|
| **I** | Plaintext secret leaks via repr/logs/API | `StoredSecret`/`SecretInfo` never expose plaintext; `repr` is secret-safe ✅ (`vault-no-plaintext`). |
| **I** | Secret readable at rest | Encrypt-at-rest via `FernetCipher` (AES-128-CBC + HMAC-SHA256); ciphertext ≠ plaintext ✅. |
| **T** | Tampered ciphertext decrypts to attacker data | Fernet MAC verified in constant time → `InvalidTokenError`, no padding oracle ✅. |
| **E** | Cross-tenant secret read | Vault reads filter by `workspace_id` ✅ (`tenant-isolation`). |
| **S** | Ephemeral key silently used in prod (secrets vanish on restart / weak key) | `FORGE_SECRET_KEY` required outside development; no silent ephemeral fallback ✅ (`secret-key-required-prod`). HARD-10 owns the deeper key-rotation seam. |

**Residual:** external KMS/Vault/SOPS backends are future work; key rotation is
operator-driven (see rotation runbook).

## S3 — MCP transport & tool dispatch

| STRIDE | Threat | Control |
|---|---|---|
| **E** | A write/mutating tool runs on a read-only connection | `is_write_tool` fails **closed** (unknown verb ⇒ write); `MCPGatewayClient` raises `MCPWriteForbiddenError` before any transport I/O ✅ (`mcp-write-default-deny`). |
| **I** | Secrets in tool args/results reach logs/audit | `forge_mcp.security.redact` + `forge_api.observability.redaction` scrub every sink; payloads stored only as hashes ✅ (`redaction-sinks`). |
| **E** | Resource access outside the connection namespace | Per-connection namespace scoping (`resource_in_scope` / `filter_resources`). |
| **R** | Tool call not attributable | Tamper-evident MCP audit entries (hash chain). |

**Residual:** a malicious third-party MCP **server** is out of scope (it is the
operator's trust decision); pentest should fuzz tool-arg injection (**PT-04**).

## S4 — SSRF via admin-configured outbound URLs

| STRIDE | Threat | Control |
|---|---|---|
| **I/E** | Admin/member sets an embedder/reranker/MCP/OAuth URL to `169.254.169.254`, loopback, or an internal host → metadata theft / internal port scan | `assert_safe_url` rejects metadata, loopback, RFC1918, link-local, ULA, `0.0.0.0`, and non-http(s) schemes; injected (DI) into the leaf HTTP clients ✅ (`ssrf-guard`). `FORGE_OUTBOUND_ALLOWLIST` / `FORGE_SSRF_ALLOW_PRIVATE` for intentional internal hosts (metadata stays blocked even then). |
| **I** | DNS-rebinding to slip past the check | Guard resolves and classifies **every** A/AAAA answer, fails closed on resolve error. |

**Residual:** the guard validates at construction/config time; time-of-check/
time-of-use rebinding between validation and the actual socket connect is a
known gap — network segmentation (compose `data`/`mcp` networks) remains the
primary control. Redirect-following to an internal host in the marketplace
registry client is **PT-07**. Pentest: SSRF via redirect + rebind (**PT-05**).

## S5 — Webhook ingest (external → API)

| STRIDE | Threat | Control |
|---|---|---|
| **S** | Forged GitHub/Slack/PM/alert delivery | Constant-time signature verification over the **exact raw bytes**; unsigned/stale/forged rejected; fail-closed when no secret is configured ✅ (`webhook-signature-fail-closed`). |
| **R** | Replay | Slack timestamp window; providers' nonces where available. |

**Residual:** replay window tuning per provider; pentest: signature-bypass and
timing (**PT-06**).

## S6 — Agent git-worktree sandbox (worker)

| STRIDE | Threat | Control |
|---|---|---|
| **E** | Agent executes a policy-denied tool | Default-deny policy (`forge_policy.evaluate`); the dispatch path never runs a denied call ✅ (`policy-default-deny`, `agent-policy-gate`). |
| **E** | Path-traversal write escaping the repo root | Traversal denial is an immutable floor a conditional rule can never loosen ✅. |
| **E** | Command injection via tool args | Commands run as argv lists, never `shell=True` (semgrep-enforced). |
| **T** | Worktree escape / host FS access | Isolated git worktrees; container/gVisor/Firecracker sandbox tiers (F19/F34). |

**Residual:** deep sandbox-escape testing is HARD-12 + a pentest item
(**PT-02**); resource-exhaustion inside a run (**PT-03**).

## S7 — Denial of service (API edge)

| STRIDE | Threat | Control |
|---|---|---|
| **D** | Request flood exhausts the service | Per-caller token-bucket rate limit → 429 + `Retry-After`; `/health` exempt ✅ (`ratelimit-429`). |
| **D** | Oversized body exhausts memory | Body-size bound → 413 (declared length + streamed cap) ✅ (`bodylimit-413`). |

**Residual:** the limiter is **per-process** (in-memory) — in a multi-replica
deploy the effective limit scales with replica count; a shared Redis-backed
limiter is future work. A WAF / L7 protection is the operator's edge control.

## S8 — Information disclosure (API surface)

| STRIDE | Threat | Control |
|---|---|---|
| **I** | OpenAPI schema exposed in prod aids recon | `/docs` `/redoc` `/openapi.json` return 404 in production unless `FORGE_DOCS_ENABLED` is explicit ✅ (`docs-lockdown-prod`). |
| **I** | Missing hardening headers (clickjacking/sniffing) | HSTS, `X-Content-Type-Options`, `X-Frame-Options: DENY`, `Referrer-Policy`, deny-all CSP on JSON ✅ (`security-headers`); mirrored in `apps/web/next.config`. |

## S9 — Audit integrity & repudiation

| STRIDE | Threat | Control |
|---|---|---|
| **T/R** | Actor tampers with or denies an action | Append-only audit hash chain detects any mutation; no update/delete API ✅ (`audit-tamper-evident`). On Postgres a BEFORE UPDATE/DELETE trigger rejects mutation ✅ (`audit-immutability-trigger`, live-DB). |

---

## Continuous verification

All ✅ rows are re-checked on every CI run by the `security` job (SAST +
dependency audit + secret scan + SBOM + `pytest -m security`). A dropped control
is a red test; a new CVE or SAST hit blocks the PR. See
[`evidence/enforcement-matrix.md`](evidence/enforcement-matrix.md).

The residual (**PT-\***) items are carried, scoped and owned, into
[`pentest-punch-list.md`](pentest-punch-list.md) for a third-party human
penetration test — the one part of this that build agents cannot perform.
