# F37 — Auth & Secrets Service (BYOK, OAuth, API keys, RBAC)

> Phase: cross-cutting · Spec module(s): **Auth & Secrets Service** (BYOK, workspace isolation, API key management, OAuth providers), **Security** (encrypted vault, RBAC, audit log, secret redaction, rate limiting, "no anonymous API access", automatic agent-token expiry), Core Data Model (`Workspace → User[]`, `APIKey[]`), Tech Stack (Better Auth / Auth.js; Encrypted Postgres vault V1) · Status target: **Done** = every inbound request to `apps/api` resolves to an authenticated `Principal` (a human OAuth user **or** a platform API key **or** an internal service token) or is rejected `401`; a workspace admin can sign in via Google / GitHub / GitLab, store BYOK provider secrets that are **AES-256-GCM envelope-encrypted at rest with per-workspace key isolation** and never returned in plaintext after creation, mint/rotate/revoke **hashed** platform API keys (shown once), and every route is gated by the flat `require_role(...)` RBAC dependency; a single canonical `SecretRedactor` strips secrets from logs/traces/responses; every auth, secret, and key mutation **emits a canonical `AuditEvent`** (auth-domain action vocabulary) through the shared `AuditSink` owned by `cross-cutting/F39-audit-log`, which persists it to the central immutable `audit_log` (F37 is a *producer*, not the table owner); agent-runner keys carry automatic expiry purged by a worker beat task; per-workspace / per-user rate limiting is enforced — `ruff` + `mypy` + `pytest` green on `packages/auth-sdk`, the `apps/api` auth/secrets/keys routers + deps, and the worker expiry task; Vitest/Playwright green on the web sign-in + settings surfaces.

---

## 1. Intent — what & why

F37 is the **security spine** of Forge. The spec lists Auth & Secrets as a first-class module ("BYOK, workspace isolation, API key management, OAuth providers") and the Security table makes nine hard requirements that are all auth/secret concerns: encrypted-at-rest per-workspace secrets, RBAC roles, an immutable audit log, secret redaction, OAuth + API-key auth with **no anonymous API access**, automatic expiry for agent tokens, and per-workspace/per-user rate limiting. None of the other slices can be built safely until this exists: F01 (board) needs `require_role`, F03 (GitHub App) and F09 (MCP gateway) need the vault to store integration/MCP credentials, F06/F08 (agent runtime) need short-lived `agent-runner` identities, and F30 (multi-team RBAC) explicitly *replaces* F37's flat role with a scoped resolver while *keeping* F37's `Principal`/`PrincipalType`, the shared vault + `SecretRedactor`, and the `require_role` back-compat shim, and emitting its own grant/team events through the same `cross-cutting/F39-audit-log` `AuditSink`.

The data substrate already exists in-tree: `packages/db/forge_db/models/workspace.py` defines `Workspace`, `User` (with `role`, `auth_provider`, `auth_subject`), and `APIKey` (with `encrypted_secret: bytes`, `key_prefix`, `kind`, `expires_at`). F37 does **not** redefine those tables — it implements the *behavior* the models only gesture at:

1. **The encrypted vault (BYOK).** Implement the actual envelope encryption that fills `APIKey.encrypted_secret`: AES-256-GCM with a versioned master key (KEK) from env and an **HKDF-derived per-workspace data key (DEK)**, with the workspace id bound into the GCM AAD so a ciphertext is cryptographically useless outside its workspace. Provider keys are decryptable on demand (Forge must use them outbound) but never leave the backend in plaintext after creation. Key rotation + crypto-shred are supported.
2. **OAuth sign-in.** Google / GitHub / GitLab via **Better Auth** in `apps/web` (the recommended stack); the API trusts only a **verifiable session JWT** minted by Better Auth (HS256 over the shared `AUTH_SECRET`, claims = forge user id + workspace + role). First login **provisions** a Forge `User` (+ default `Workspace`) and links the OAuth identity.
3. **Platform API keys.** A *separate* mechanism from BYOK: inbound machine/agent auth tokens that are **one-way hashed** (never decryptable), shown once at creation, parsed by an embedded key-id for O(1) lookup, verified in constant time, scoped to a role no higher than the creator's, and (for agents) short-lived with automatic expiry.
4. **RBAC (flat, V1).** A role hierarchy (`admin > member > {agent-runner, viewer}`) and a `require_role(min_role)` FastAPI dependency on every route, plus ownership/self checks. F30 supersedes this with per-scope grants; F37 ships the flat version and the contract F30 shims.
5. **Secret redaction.** One canonical `SecretRedactor` (regex + entropy + a per-workspace known-secret registry) reused by logs, run traces, the MCP gateway (F09), and retrieval results.
6. **Auth-domain audit events (producer, not owner).** Every auth/secret/key/role/rate-limit mutation emits a canonical `AuditEvent` through the shared `AuditSink` (contract `forge_contracts.audit`, persisted to the central immutable `audit_log` by `cross-cutting/F39-audit-log`'s `SqlAuditWriter`). F37 does **not** define the audit table, contract, immutability trigger, or query surface — those are owned by F39. F37 establishes the **auth-domain slice of the action vocabulary**, maps its `Principal` onto F39's `ActorType`/`actor_label`, and guarantees these events are audited from day one (key/role/secret mutations fail-closed). Until F39 lands, F37 wires a log-only fallback sink so no event is silently dropped.
7. **Rate limiting & no-anonymous-access.** A Redis token-bucket limiter keyed per workspace/user/key, and a global guard so every route is authenticated.

This is intentionally a large cross-cutting slice: it is the trust foundation a "serious engineering team must be able to audit and fully trust in production" (Build Prompt quality bar).

---

## 2. User-facing behavior / journeys

**Journey A — First sign-in & provisioning (new admin).** A user opens `https://forge.example.com`, is redirected to `/login`, clicks "Continue with GitHub". Better Auth runs the OAuth code+PKCE flow, returns to the callback, and a provisioning hook creates a Forge `Workspace` (slug from email domain) + a `User` with role `admin` (first user in a fresh workspace is admin) linked to `(provider=github, subject=...)`. The browser now holds a Better Auth session; subsequent API calls carry a session JWT. The user lands on the board, authenticated.

**Journey B — Link a second provider.** The same user opens **Settings → Account**, clicks "Link Google". After the OAuth dance a second `oauth_account` row points at the same Forge `User`; signing in later with *either* Google or GitHub resolves to the same identity.

**Journey C — Store a BYOK provider key (admin).** Admin opens **Settings → Secrets**, clicks "Add secret", picks kind `model_provider`, provider `anthropic`, pastes the key. On save the API encrypts it via the vault and stores only ciphertext + `key_prefix` (e.g. `sk-ant-…a3f9`). The list shows name, provider, prefix, created/last-used — **never** the secret. There is no "reveal" action; the value can only be re-entered, not read back.

**Journey D — Create a platform API key (admin/member).** Admin opens **Settings → API Keys**, clicks "Create key", names it "CI deploy bot", picks role `agent-runner`, optional expiry 90 days. The plaintext token `forge_svc_a1b2c3d4_…` is shown **once** in a copy-to-clipboard dialog with the warning "you will not see this again". Thereafter only the masked prefix is shown. A "Revoke" action immediately invalidates it.

**Journey E — Machine auth.** The CI bot calls `POST /api/v1/run …` with `Authorization: Bearer forge_svc_a1b2c3d4_…`. The API parses the key id, looks up the row, HMAC-verifies the secret in constant time, checks it is not revoked/expired, resolves a `service`/`agent-runner` `Principal` scoped to the key's workspace + role, updates `last_used_at`, and processes the request. An unauthenticated request to the same route gets `401` with `WWW-Authenticate`.

**Journey F — Agent run identity.** When F08 starts an `AgentRun`, the orchestrator mints a short-lived (`expires_at = now + run_ttl`) `agent-runner` platform key bound to the run's workspace; the agent uses it for tool/API calls and literally cannot escalate (role capped at `agent-runner`). After `expires_at` it stops authenticating and is purged by the beat task.

**Journey G — Audit & rotation (admin).** Every login, secret create/delete, key create/revoke, and role change emitted by F37 appears — with actor, target, timestamp, and outcome — in the audit viewer owned by `cross-cutting/F39-audit-log` (Settings → Audit). F37 produces the events; F39 owns the table, query API, and viewer. Admin runs `forge-cli secrets rotate-key` after adding a new KEK version; all ciphertexts re-encrypt to the new version with zero plaintext exposure and a `secret.key_rotated` `AuditEvent` is emitted.

**Journey H — A blocked action.** A `viewer` calls `DELETE /api/v1/secrets/{id}` → `403 {"error":"forbidden","required_role":"admin"}`. An `agent-runner` key tries `POST /api/v1/api-keys` (mint another key) → `403`. Both emit a `denied`-outcome `AuditEvent`.

---

## 3. Vertical slice

### 3.1 Data model (tables/columns/migrations touched)

One reversible Alembic migration `packages/db/forge_db/migrations/versions/0002_auth_secrets.py` (depends on `0001_baseline`, which already creates `workspace`, `app_user`, `api_key`). F37 **adds two tables** (`platform_api_key`, `oauth_account`) and leaves the existing identity tables in place. The central `audit_log` table is **not** created here — it is owned by `cross-cutting/F39-audit-log`; F37 only emits `AuditEvent`s into it (see §3.2). ORM models live in `packages/db/forge_db/models/`. All new tenant tables compose the existing `WorkspaceScopedModel` (UUID PK + timestamps + `workspace_id` CASCADE) and `enum_type(...)` from `forge_db.base`.

**Existing tables consumed (no schema change required):**
- `app_user` — `role` (`UserRole`), `auth_provider`, `auth_subject`, `is_active`, `email`. F37 *uses* `auth_provider`/`auth_subject` as the **primary** linked identity; additional links go in `oauth_account`.
- `api_key` — the **BYOK** table. `encrypted_secret: bytes` (vault blob), `key_prefix`, `kind` (`APIKeyKind`), `provider`, `last_used_at`, `expires_at`. F37 implements the encryption that populates `encrypted_secret`. (Optional additive column `key_version smallint NULL` is **not** added — the key version is embedded in the ciphertext blob; see §4.)

> Naming clarity (load-bearing): **`api_key`** = BYOK secrets Forge stores and **decrypts** to use *outbound* (model/integration/MCP credentials). **`platform_api_key`** (new) = inbound auth tokens Forge only **verifies** (one-way hashed, never decryptable). They are different security primitives and must not be conflated.

**New — `platform_api_key`** (`models/platform_api_key.py`, `WorkspaceScopedModel`): inbound machine/agent auth tokens.

| column | type | notes |
|---|---|---|
| `id` | UUID PK | |
| `workspace_id` | UUID FK→`workspace` CASCADE | tenant scope |
| `name` | String(255) | display name |
| `key_id` | String(16) | non-secret public lookup id embedded in the token; `uq_platform_api_key_key_id` UNIQUE; indexed |
| `key_hash` | String(128) | HMAC-SHA256(pepper, secret) hex — **one-way**, never the token |
| `key_prefix` | String(40) | masked display, e.g. `forge_svc_a1b2c3d4…last4` |
| `kind` | enum(`PlatformKeyKind`: personal/service/agent_runner) | controls token prefix + lifecycle |
| `role` | enum(`UserRole`) | role this key authenticates as; capped at creator's role |
| `created_by` | UUID FK→`app_user` SET NULL | actor |
| `last_used_at` | timestamptz NULL | refreshed on verify (throttled write, see §3.2) |
| `expires_at` | timestamptz NULL | NULL = no expiry; required for `agent_runner` |
| `revoked_at` | timestamptz NULL | non-null ⇒ rejected immediately |

Indexes: `ix_platform_api_key_key_id` (hot auth lookup), `ix_platform_api_key_workspace_id` (from mixin), partial `ix_platform_api_key_expiring (expires_at) WHERE expires_at IS NOT NULL`. CHECK: `kind='agent_runner' → expires_at IS NOT NULL` (agents must expire). Revoke = set `revoked_at` (kept for audit), not delete.

**New — `oauth_account`** (`models/oauth_account.py`, `WorkspaceScopedModel`): linked external identities → one Forge user.

| column | type | notes |
|---|---|---|
| `id` | UUID PK | |
| `workspace_id` | UUID FK CASCADE | |
| `user_id` | UUID FK→`app_user` CASCADE | the Forge user this identity logs into |
| `provider` | enum(`OAuthProvider`: google/github/gitlab) | |
| `provider_subject` | String(255) | the provider's stable `sub` |
| `email` | String(320) NULL | email asserted by provider at link time |
| `linked_at` | timestamptz | |

Constraints: `uq_oauth_account_provider` UNIQUE(`provider`,`provider_subject`) (one external identity → one Forge user globally); index `ix_oauth_account_user_id`. **No provider access/refresh tokens are stored here** — Better Auth holds OAuth tokens in its own tables; Forge stores only the linkage needed to resolve a login to a `User`.

**Central `audit_log` — owned by `cross-cutting/F39-audit-log`, NOT created here.** The platform-wide immutable, tamper-evident (per-workspace hash chain) `audit_log` table, its `audit_log_immutable` Postgres trigger, the reusable `attach_immutability_trigger(table)` helper, and the `audit_chain_head` companion are all defined by F39. F37 is a **producer**: every auth/secret/key/role mutation emits a `forge_contracts.audit.AuditEvent` through the injected `AuditSink` (F39's `SqlAuditWriter`), with `metadata` redacted by F37's canonical `SecretRedactor` before persistence (AC16: no secret ever lands in audit). F37 contributes the auth-domain action strings to F39's `AuditAction` vocabulary (§4).

**New enums** (added to `packages/db/forge_db/models/enums.py` + exported): `PlatformKeyKind`, `OAuthProvider`, `PrincipalType`. (`UserRole`, `APIKeyKind` already exist and are reused verbatim; `PrincipalType` is also mirrored in `forge_contracts.auth` and imported by `cross-cutting/F30-multi-team-rbac`. The audit enums — `ActorType`/`AuditAction`/`AuditOutcome`/`AuditSeverity`/`AuditResourceType` — are owned by F39.)

Migration is otherwise a no-op data migration. Downgrade drops the two tables and the new enum CHECK constraints.

### 3.2 Backend (FastAPI routes + services/packages)

**New pure package `packages/auth-sdk/forge_auth/` (no FastAPI / SQLAlchemy imports — mirrors `policy-sdk` / `authz-sdk` discipline; the crypto + RBAC + redaction heart of the slice):**

```
packages/auth-sdk/
├── pyproject.toml                 # uv workspace member: forge-auth
└── forge_auth/
    ├── __init__.py
    ├── models.py                  # Principal, AuthContext, SessionClaims, enums re-export (§4)
    ├── protocols.py               # KeyProvider, Vault, RateLimiter, PrincipalResolver (AuditSink is imported from forge_contracts.audit, owned by F39)
    ├── vault.py                   # SecretVault: AES-256-GCM envelope enc + HKDF per-ws DEK + rotation
    ├── keys.py                    # platform API key generate / hash / verify / parse
    ├── tokens.py                  # session JWT encode/decode + internal service token
    ├── rbac.py                    # Role rank, ROLE_RANK, has_at_least, coarse capability checks (flat V1)
    ├── redaction.py               # canonical SecretRedactor (regex + entropy + dynamic registry)
    └── errors.py                  # AuthenticationError, AuthorizationError, DecryptionError,
                                   #   KeyRotationError, TokenExpired, InvalidToken, KeyMaterialError
```

**Vault (the BYOK core, `vault.py`).** Pure crypto over an injected `KeyProvider` (env-backed in prod, fixed-key in tests). Envelope scheme, fully specified in §4:
- KEK = 32-byte master key, **versioned** via env (`FORGE_VAULT_KEYS="1:<b64>,2:<b64>"`, `FORGE_VAULT_ACTIVE_KEY_VERSION=2`).
- Per-workspace DEK = `HKDF-SHA256(ikm=KEK_v, salt=workspace_id.bytes, info=b"forge-vault-dek", length=32)` → leaking one workspace's derived key never exposes another; a workspace can be crypto-shredded by rotating its salt domain.
- Ciphertext blob = `b"\x01"` (format ver) `+ key_version (1B) + nonce (12B) + AESGCM(dek).encrypt(nonce, plaintext, aad=workspace_id.bytes)`. The workspace id in the **AAD** binds the ciphertext to its tenant: a blob copied to another `workspace_id` fails authentication on decrypt.
- `rotate(blob, workspace_id)` decrypts under the embedded version and re-encrypts under the active version.

**Service `apps/api/forge_api/services/secrets_service.py`** (BYOK orchestration over `api_key` + vault; the only sanctioned reader of plaintext provider secrets):
- `create_secret(actor, name, kind, provider, plaintext) -> SecretMeta` — encrypts via vault, stores `encrypted_secret`/`key_prefix`, audits `secret.created`. Never returns plaintext.
- `list_secrets(workspace_id) -> list[SecretMeta]` / `get_secret_meta(id)` — metadata only.
- `reveal_for_use(id) -> str` — **internal-only** (no HTTP route); decrypts for outbound use by F03/F08/F09; refreshes `last_used_at`; emits `secret.accessed` (sampled, `emit_async`). Callers receive plaintext only in-process.
- `delete_secret(actor, id)` — audits `secret.deleted`.
- `rotate_workspace_secrets(workspace_id) -> int` — re-encrypts every row to the active KEK version (powers `forge-cli secrets rotate-key`); audits `secret.key_rotated`.

**Service `apps/api/forge_api/services/api_key_service.py`** (platform key lifecycle):
- `create_key(actor, name, kind, role, expires_at=None) -> CreatedKey` — **caps `role` at the actor's role** (no self-escalation); generates token, stores `key_id`/`key_hash`/`key_prefix`; returns the **one-time** plaintext; emits `apikey.created` (critical, `emit(session,...)`).
- `verify(token) -> Principal | None` — parse `key_id`, fetch row, constant-time HMAC compare, reject if revoked/expired; throttled `last_used_at` refresh (only if stale > 60s, to avoid a write per request).
- `revoke_key(actor, id)` — sets `revoked_at`; emits `apikey.revoked` (critical).
- `list_keys(workspace_id)` — masked metadata only.
- `mint_agent_key(workspace_id, ttl) -> CreatedKey` — internal helper for F08; `kind=agent_runner`, `role=agent-runner`, `expires_at=now+ttl`.

**Service `apps/api/forge_api/services/auth_service.py`** (OAuth provisioning + session):
- `provision_from_oauth(provider, subject, email, name) -> User` — find `oauth_account` → existing user; else find-or-create `Workspace` + create `User` (first user in a new workspace ⇒ `admin`, else `member`) + `oauth_account`; audits `auth.user_provisioned`. Idempotent on `(provider, subject)`.
- `link_oauth(actor, provider, subject, email)` / `unlink_oauth(actor, id)` — manage additional identities (cannot unlink the last login method).
- `build_session_claims(user) -> SessionClaims` — the JWT claim set the web layer embeds (forge user id, workspace, role, email).

**Auth audit emission `apps/api/forge_api/services/auth_audit.py`** — a thin helper (NOT a sink implementation) that maps an authenticated `Principal` onto a canonical `forge_contracts.audit.AuditEvent` (`Principal.type==user → ActorType.USER`, `actor_label=f"user:{email}"`; an `agent-runner` platform key → `ActorType.AGENT_RUNNER`; service/personal platform keys + the internal service token → `ActorType.SYSTEM`, `actor_label=f"key:{name}"`/`service:{name}`; `resource_type ∈ {API_KEY, USER, ...}`), then calls the **injected** `AuditSink`. The concrete sink is F39's `SqlAuditWriter` at runtime, an in-memory `FakeAuditSink` in tests, and a log-only fallback until F39 lands. Key/role/secret mutations are emitted as **critical** events via `emit(session, ...)` (fail-closed: audit and action commit in one transaction); login/oauth/secret-access/rate-limit events use `emit_async(...)` (fail-open). F37 implements neither the `audit_log` table nor the sink — both are owned by `cross-cutting/F39-audit-log`.

**Auth dependency module `apps/api/forge_api/deps/auth.py`** — the unified resolver:

```python
async def get_principal(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> Principal:
    """Resolve exactly one Principal or raise 401. Order:
    1) `Authorization: Bearer <jwt>`  -> decode_session_jwt (HS256/AUTH_SECRET, aud=forge-api)
                                          -> user Principal.
    2) `Authorization: Bearer forge_*`-> api_key_service.verify -> api_key/agent Principal.
    3) `X-Forge-Service-Token: <tok>` -> internal service token  -> service Principal.
    No credential -> AuthenticationError (401, WWW-Authenticate: Bearer)."""

def require_role(min_role: UserRole) -> Callable[..., Awaitable[Principal]]:
    """Dependency factory. Resolves the principal and asserts
    ROLE_RANK[principal.role] >= ROLE_RANK[min_role], else 403 (AuthorizationError).
    `require_admin = require_role(UserRole.ADMIN)`. F30 will replace this with
    require_permission(...) and provide a shim that maps role->workspace permission."""

async def get_current_user(p: Principal = Depends(get_principal)) -> User: ...
def require_self_or_admin(user_id_param: str) -> Callable[...]: ...   # ownership check
```

**Global no-anonymous-access guard.** A small middleware (`apps/api/forge_api/middleware/auth_guard.py`) rejects any request that did not resolve a `Principal`, except an explicit allowlist: `/healthz`, `/readyz`, `/api/v1/auth/sync` (service-token only), `/docs`+`/openapi.json` (dev only, env-gated). This guarantees "all routes authenticated; no anonymous API access" structurally, independent of per-route dependencies.

**Rate-limit middleware `apps/api/forge_api/middleware/rate_limit.py`** — Redis fixed-window/token-bucket via `RateLimiter`. After auth, checks `rl:ws:{workspace_id}`, `rl:user:{user_id}` / `rl:key:{key_id}`; on breach returns `429` + `Retry-After`. Limits from env (defaults in §3.5). Per-model-provider limiting is enforced at the BYOK call site in the agent runtime (soft, see §12).

**Routers (`apps/api/forge_api/routers/`), all under `/api/v1`, all behind `get_principal`:**

| Router (file) | Endpoints | Role |
|---|---|---|
| `auth.py` | `GET /auth/me` (current principal), `POST /auth/sync` (Better-Auth→Forge provisioning; service-token), `POST /auth/oauth/link` / `DELETE /auth/oauth/{id}` (link/unlink), `GET /auth/oauth/accounts` | self / service |
| `secrets.py` | `POST /secrets`, `GET /secrets`, `GET /secrets/{id}`, `DELETE /secrets/{id}` | admin (write), member (read meta) |
| `api_keys.py` | `POST /api-keys` (returns plaintext once), `GET /api-keys`, `DELETE /api-keys/{id}` (revoke) | member+ (role ≤ self), admin to manage others |

The audit **query** surface (`GET /api/v1/audit`), the viewer UI, the chain verifier, and the NDJSON export are owned by `cross-cutting/F39-audit-log`; F37 ships **no audit router** — it only produces events.

`apps/api` never decrypts a BYOK secret for the wire — `reveal_for_use` is in-process only and has no route. The OAuth code/token exchange happens in `apps/web` (Better Auth); the API only validates the resulting JWT and provisions the user.

### 3.3 Worker / agent runtime (Celery tasks, LangGraph, if any)

Celery only (no LangGraph). F37 owns expiry hygiene in `apps/worker/forge_worker/tasks/auth.py` (queue `default`, registered on Beat):

- `auth.purge_expired_keys` (every 15m) — for `platform_api_key` rows where `expires_at < now()` and `revoked_at IS NULL`: set `revoked_at = expires_at` and emit one `apikey.expired` `AuditEvent` each via the injected `AuditSink` (batched). Authoritative expiry is at *verify time* (`api_key_service.verify` rejects expired keys even before this runs), so a missed run never grants stale access — this task is hygiene + audit. Idempotent (already-revoked rows skipped). Specifically covers Security "automatic expiry for agent tokens" for `kind=agent_runner`.
- `auth.reencrypt_workspace_secrets(workspace_id)` (on-demand, enqueued by `forge-cli secrets rotate-key` for large workspaces) — calls `secrets_service.rotate_workspace_secrets` in chunks; audits progress.

**Agent-runtime consumption (contract here; wired by F06/F08):** when a `WorkflowRun`/`AgentRun` starts, the orchestrator calls `api_key_service.mint_agent_key(workspace_id, ttl=run_ttl)` and injects the token into the sandbox env; the agent authenticates to `apps/api`/the MCP gateway as an `agent-runner` `Principal`. The key's role is hard-capped at `agent-runner`, so the agent cannot mint another key, write secrets, or read the audit log — the auth half of Build-Prompt constraint #2 ("the agent never self-assigns permissions or expands its own scope").

### 3.4 Frontend / UI (Next.js routes/components)

**App `apps/web`** (App Router, TS, Tailwind, shadcn/ui, TanStack Query). **Better Auth is the auth runtime here** (Tech Stack: "Better Auth / Auth.js"):

- `apps/web/lib/auth/server.ts` — Better Auth server config: social providers Google/GitHub/GitLab (client id/secret from env), the **JWT plugin** configured for HS256 over `AUTH_SECRET` with claims `{ sub: forge_user_id, wsid, role, email, aud:"forge-api" }`, a Postgres adapter (Better-Auth-managed tables, see §3.5), and a `databaseHooks.user.create.after` hook that POSTs `/api/v1/auth/sync` (service token) to provision the Forge `User`/`Workspace` and obtain `forge_user_id`/`wsid`/`role` for the JWT claims.
- `apps/web/lib/auth/client.ts` — Better Auth React client (`signIn.social`, `useSession`, `signOut`).
- `apps/web/middleware.ts` — protects `(app)` routes (redirect unauthenticated → `/login`); attaches the session JWT as `Authorization: Bearer` on API fetches via the API client.
- `apps/web/lib/api/client.ts` — fetch wrapper injecting the JWT; 401 → redirect to `/login`, 429 → backoff toast.

Routes / components:
- `app/(auth)/login/page.tsx` — provider buttons ("Continue with Google/GitHub/GitLab"), empty-state guidance.
- `app/(app)/settings/account/page.tsx` + `components/auth/LinkedAccounts.tsx` — linked OAuth identities, link/unlink.
- `app/(app)/settings/secrets/page.tsx` + `components/auth/SecretForm.tsx`, `SecretsTable.tsx` — BYOK CRUD; **no reveal control**; create shows prefix only afterward.
- `app/(app)/settings/api-keys/page.tsx` + `components/auth/ApiKeyTable.tsx`, `CreateApiKeyDialog.tsx` — create (one-time plaintext copy dialog + "you won't see this again"), list (masked), revoke. Role picker disables roles above the current user's.
- *(The audit viewer — `app/(app)/settings/audit/page.tsx` — is owned by `cross-cutting/F39-audit-log`, which renders the events F37 produces; F37 ships no audit UI.)*
- `apps/web/lib/auth/useCapabilities.ts` — exposes the current user's role for capability-aware hide/disable (server remains authoritative).

### 3.5 Infra / deploy (compose, helm, caddy, if any)

No new compose service — reuses `db`, `redis` (rate-limit buckets + Celery broker + Beat), `api`, `worker`, `web`, `caddy`. Requirements:

- **Better Auth tables** are created by Better Auth's own migrator (`pnpm --filter web exec @better-auth/cli migrate`) against the same Postgres, in dedicated `auth_*` tables — **separate from Alembic** (documented in `docs/self-hosting/` and run by `make setup`). Alembic owns only the Forge tables in §3.1.
- **New env** (add to `.env.example` + `deploy/.env.production.example`):
  - `FORGE_VAULT_KEYS` — versioned KEK map `"1:<base64-32B>"`; **required**, no default (startup fails fast if absent in prod).
  - `FORGE_VAULT_ACTIVE_KEY_VERSION=1`.
  - `API_KEY_PEPPER` — HMAC pepper for platform key hashing (required).
  - `AUTH_SECRET` (exists) — shared HS256 secret for the session JWT (web ↔ api).
  - `INTERNAL_SERVICE_TOKEN` — service principal token for worker→api and web→`/auth/sync`.
  - OAuth apps (distinct from the GitHub **App** in F03): `GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET`, `GITHUB_OAUTH_CLIENT_ID`/`GITHUB_OAUTH_CLIENT_SECRET`, `GITLAB_CLIENT_ID`/`GITLAB_CLIENT_SECRET`.
  - Rate limits: `RATE_LIMIT_WORKSPACE_PER_MIN=600`, `RATE_LIMIT_USER_PER_MIN=300`, `RATE_LIMIT_KEY_PER_MIN=300`.
- **Caddy** serves `apps/web` (Better Auth callback URLs `/api/auth/callback/{provider}`) and proxies `/api/*` to `apps/api`. The gateway (F09) stays internal-only.
- `forge-cli` (extended): `users create-admin` (creates workspace + admin user + first admin platform key, prints the token once) and `secrets rotate-key` (re-encrypt all secrets to the active KEK version). Both referenced by the Docker Compose Production quickstart in the spec.
- **Key generation helper** documented in `docs/self-hosting/security.md`: `python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"` for `FORGE_VAULT_KEYS` / `API_KEY_PEPPER` / `AUTH_SECRET` / `INTERNAL_SERVICE_TOKEN`.

---

## 4. Public interfaces / contracts (exact signatures, Pydantic models, Protocols, YAML schemas)

> **Audit contracts are owned by `cross-cutting/F39-audit-log`**, not F37. `AuditEvent`, `AuditEntry`, the `AuditSink` Protocol (`emit(session, event)` critical / `emit_async(event)` non-critical), and the audit enums (`ActorType`, `AuditAction`, `AuditOutcome`, `AuditSeverity`, `AuditResourceType`) live in `packages/contracts/forge_contracts/audit.py`. F37 **imports and emits** them; it defines only the auth/secret/key DTOs below in `forge_contracts/auth.py`.

**Frozen DTOs + Protocols (`packages/contracts/forge_contracts/auth.py`):**

```python
from __future__ import annotations
from datetime import datetime
from enum import StrEnum
from uuid import UUID
from typing import Protocol, runtime_checkable
from pydantic import BaseModel, ConfigDict

# --- enums (UserRole / APIKeyKind already live in forge_db.models.enums; mirrored here for the contract layer) ---
class UserRole(StrEnum):
    ADMIN = "admin"; MEMBER = "member"; VIEWER = "viewer"; AGENT_RUNNER = "agent-runner"

class PrincipalType(StrEnum):
    USER = "user"; API_KEY = "api_key"; SERVICE = "service"

class OAuthProvider(StrEnum):
    GOOGLE = "google"; GITHUB = "github"; GITLAB = "gitlab"

class PlatformKeyKind(StrEnum):
    PERSONAL = "personal"; SERVICE = "service"; AGENT_RUNNER = "agent_runner"

# NOTE: audit enums (ActorType / AuditAction / AuditOutcome / ...) live in
# forge_contracts.audit (owned by cross-cutting/F39-audit-log), not here.

class Principal(BaseModel):
    """The authenticated identity attached to every request."""
    model_config = ConfigDict(frozen=True)
    type: PrincipalType
    id: UUID                      # user id | platform_api_key id | service id
    workspace_id: UUID
    role: UserRole                # flat role (V1); F30 layers scoped grants on top
    display: str                  # e.g. "user:alice@x.com" / "key:CI deploy bot"
    email: str | None = None

class SessionClaims(BaseModel):
    """The JWT claim set minted by Better Auth (web) and verified by the API."""
    model_config = ConfigDict(frozen=True)
    sub: UUID                     # forge user id
    wsid: UUID                    # workspace id
    role: UserRole
    email: str | None = None
    aud: str = "forge-api"
    iss: str = "forge-web"
    exp: int                      # unix seconds
    iat: int

class SecretMeta(BaseModel):       # BYOK metadata — NEVER carries the secret
    model_config = ConfigDict(frozen=True)
    id: UUID
    name: str
    kind: str                      # APIKeyKind value
    provider: str | None = None
    key_prefix: str | None = None
    last_used_at: datetime | None = None
    expires_at: datetime | None = None
    created_at: datetime

class CreatedKey(BaseModel):        # returned EXACTLY once on platform-key creation
    id: UUID
    name: str
    kind: PlatformKeyKind
    role: UserRole
    token: str                     # plaintext — present only in the create response
    key_prefix: str
    expires_at: datetime | None = None

class PlatformKeyMeta(BaseModel):   # list/detail — no token
    model_config = ConfigDict(frozen=True)
    id: UUID
    name: str
    kind: PlatformKeyKind
    role: UserRole
    key_prefix: str
    last_used_at: datetime | None = None
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    created_at: datetime

# AuditEvent / AuditEntry / AuditSink are imported from forge_contracts.audit
# (owned by cross-cutting/F39-audit-log). F37 emits them; see §3.2 auth_audit.py.

@runtime_checkable
class Vault(Protocol):
    def encrypt(self, plaintext: str, *, workspace_id: UUID) -> bytes: ...
    def decrypt(self, blob: bytes, *, workspace_id: UUID) -> str: ...
    def rotate(self, blob: bytes, *, workspace_id: UUID) -> bytes: ...

@runtime_checkable
class KeyProvider(Protocol):
    def get(self, version: int) -> bytes: ...        # 32-byte KEK for a version
    def active_version(self) -> int: ...

@runtime_checkable
class RateLimiter(Protocol):
    async def check(self, key: str, *, limit: int, window_seconds: int) -> "RateDecision": ...

@runtime_checkable
class SecretRedactor(Protocol):
    def redact(self, text: str) -> str: ...
    def register_known_secret(self, value: str) -> None: ...   # per-workspace dynamic scrub
```

**Vault implementation contract (`packages/auth-sdk/forge_auth/vault.py`):**

```python
class SecretVault:                 # satisfies Vault
    def __init__(self, keys: KeyProvider): ...
    # blob = b"\x01" + version(1B) + nonce(12B) + AESGCM(dek).encrypt(nonce, pt, aad=workspace_id.bytes)
    # dek  = HKDF-SHA256(ikm=keys.get(version), salt=workspace_id.bytes,
    #                    info=b"forge-vault-dek", length=32)
    def encrypt(self, plaintext: str, *, workspace_id: UUID) -> bytes: ...
    def decrypt(self, blob: bytes, *, workspace_id: UUID) -> str:
        """Raises DecryptionError on tampered/cross-workspace/wrong-version blob."""
    def rotate(self, blob: bytes, *, workspace_id: UUID) -> bytes:
        """Decrypt under embedded version, re-encrypt under keys.active_version()."""
```

**Platform key helpers (`packages/auth-sdk/forge_auth/keys.py`):**

```python
_PREFIX = {PlatformKeyKind.PERSONAL: "forge_pat",
           PlatformKeyKind.SERVICE: "forge_svc",
           PlatformKeyKind.AGENT_RUNNER: "forge_agt"}

def generate_api_key(kind: PlatformKeyKind) -> tuple[str, str, str, str]:
    """Returns (token, key_id, secret, display_prefix).
    token = f"{_PREFIX[kind]}_{key_id}_{secret_b64url}"  where key_id = 8 url-safe chars
    (public lookup id) and secret = 32 random bytes (the verified portion)."""

def hash_api_key(secret: str, *, pepper: str) -> str:
    """hex HMAC-SHA256(pepper, secret). One-way; high-entropy token so a fast MAC is
    sufficient and keeps the auth path O(1) (Argon2 is for low-entropy passwords)."""

def verify_api_key(secret: str, key_hash: str, *, pepper: str) -> bool:
    """hmac.compare_digest — constant time."""

def parse_token(token: str) -> tuple[PlatformKeyKind, str, str] | None:
    """(kind, key_id, secret) or None if the token isn't a well-formed forge_* key."""
```

**Session token helpers (`packages/auth-sdk/forge_auth/tokens.py`):**

```python
def encode_session_jwt(claims: SessionClaims, *, secret: str) -> str: ...    # HS256
def decode_session_jwt(token: str, *, secret: str, audience: str = "forge-api") -> SessionClaims:
    """Verifies signature, exp, aud. Raises TokenExpired / InvalidToken."""
def make_service_token(*, secret: str) -> str: ...                            # internal service principal
def verify_service_token(token: str, *, secret: str) -> bool: ...
```

**RBAC (`packages/auth-sdk/forge_auth/rbac.py`):**

```python
ROLE_RANK: dict[UserRole, int] = {
    UserRole.VIEWER: 0, UserRole.AGENT_RUNNER: 0, UserRole.MEMBER: 1, UserRole.ADMIN: 2,
}
def has_at_least(role: UserRole, minimum: UserRole) -> bool:
    return ROLE_RANK[role] >= ROLE_RANK[minimum]
def max_grantable_role(actor: UserRole) -> UserRole:
    return actor            # cannot create a key/role above your own
```

**REST request/response models (`apps/api/forge_api/schemas/auth.py`):**

```python
class SecretIn(BaseModel):
    name: str; kind: str; provider: str | None = None; plaintext: str   # plaintext write-only
class ApiKeyIn(BaseModel):
    name: str; kind: PlatformKeyKind = PlatformKeyKind.SERVICE
    role: UserRole = UserRole.AGENT_RUNNER; expires_at: datetime | None = None
class OAuthSyncIn(BaseModel):                                  # web -> /auth/sync (service token)
    provider: OAuthProvider; provider_subject: str; email: str | None; name: str | None
class OAuthSyncOut(BaseModel):
    forge_user_id: UUID; workspace_id: UUID; role: UserRole
# (No AuditQuery here — the audit query schema + GET /audit are owned by
#  cross-cutting/F39-audit-log: forge_contracts.audit + apps/api audit router.)
```

**Audit event catalog (the `AuditAction` values F37 emits).** F37 reuses the auth-domain members already defined in F39's `forge_contracts.audit.AuditAction`: `auth.login` (`AUTH_LOGIN`), `auth.failed` (`AUTH_FAILED`, failed login), `secret.accessed` (`SECRET_ACCESSED`), `apikey.created` (`APIKEY_CREATED`), `apikey.revoked` (`APIKEY_REVOKED`), `rbac.role_changed` (`RBAC_ROLE_CHANGED`). F37 **contributes these additions** to F39's `AuditAction` enum (a PR against the F39-owned contract, mirroring how `cross-cutting/F30-multi-team-rbac` extends it with `role_grant.*`): `auth.user_provisioned`, `auth.oauth_linked`, `auth.oauth_unlinked`, `secret.created`, `secret.deleted`, `secret.key_rotated`, `apikey.expired`, `rate_limit.exceeded`. All carry `actor_type`/`actor_label` (mapped from `Principal`), `outcome` (`AuditOutcome`), and redacted `metadata`.

**Error contract:** `401 {"error":"unauthenticated"}` + `WWW-Authenticate: Bearer`; `403 {"error":"forbidden","required_role":"<role>"}`; `403 {"error":"escalation","requested_role":...,"actor_role":...}` for over-privileged key/role creation; `404` for cross-workspace resources (no existence leak); `429 {"error":"rate_limited","retry_after":<s>}`; `422` FastAPI validation. The plaintext token / secret appears **only** in the `CreatedKey.token` create response and the inbound `SecretIn.plaintext` — never in any GET, list, log, trace, or audit row.

---

## 5. Dependencies — features/slices that must exist first

F37 is the **root of the auth/secrets graph**; it has no *auth or secret* prerequisites, only the in-tree scaffolding plus one contract-level peer (F39, the audit sink):

- **(in-tree) `packages/db` baseline** — `0001_baseline` migration + `Workspace` / `User` / `APIKey` ORM models and the `UserRole` / `APIKeyKind` enums (`packages/db/forge_db/models/workspace.py`, `models/enums.py`). F37 adds migration `0002` (the two new tables + enums) and implements the encryption that fills `APIKey.encrypted_secret`.
- **(in-tree) uv workspace + service scaffolding** — `apps/api/forge_api`, `apps/worker/forge_worker`, `packages/contracts`, Redis + Postgres in `deploy/`, Celery Beat. F37 adds the new `packages/auth-sdk` member and fleshes out the empty api/worker packages.
- **(peer, contract-level) `cross-cutting/F39-audit-log`** — owns the central `audit_log` table, the `AuditEvent`/`AuditSink` contract (`forge_contracts.audit`), the `SqlAuditWriter`, the `attach_immutability_trigger` helper, and the audit query/verify/export/viewer surfaces. F37 is a **producer**: it imports the contract and emits auth-domain events through the injected `AuditSink`. This is **not a circular dependency** — the contract lives in the shared `packages/contracts`; F37 compiles against the `AuditSink` interface and is wired with F39's `SqlAuditWriter` (or a log-only fallback before F39 lands) at app composition, while F39's *admin* query API in turn consumes F37's `require_role`/`Principal`. F37's pure `packages/auth-sdk` (no SQLAlchemy) also exports the canonical `SecretRedactor` that F39's `SqlAuditWriter` imports to redact metadata — so the package order is `contracts` ← `auth-sdk` (F37) ← `forge_db.audit` (F39), with no cycle.

**Consumers (depend on F37; not prerequisites):**
- `cross-cutting/F30-multi-team-rbac` (a.k.a. the v3 RBAC upgrade) — **replaces** F37's flat `require_role` with scoped `require_permission`, reuses F37's `Principal`, `PrincipalType`, vault, `SecretRedactor`, and `create-admin`, emits its grant/team events through the same `cross-cutting/F39-audit-log` `AuditSink`, and ships the `require_role`→`require_permission` shim. (F30's "table origin: `cross-cutting/F37-auth-secrets-byok`" note for `audit_log` is superseded by this slice — the table is owned by F39; F37 only produces events.)
- `v1/F01-project-board` — every board route uses `require_role` + `get_principal`; writes activity to the shared audit log.
- `v1/F03-github-app`, `v1/F09-mcp-gateway-v1` — store integration/MCP credentials via `secrets_service` (the vault); F09's `redaction.py` re-exports F37's canonical `SecretRedactor`; both emit to the shared `AuditSink` owned by `cross-cutting/F39-audit-log`.
- `v1/F06-single-execution-agent`, `v1/F08-plan-execute-verify-pr-approval` — mint short-lived `agent-runner` keys via `mint_agent_key`.

> Cross-slice reconciliation: F09 references this foundation as `v1/F02-auth-workspace-byok` and F30 references it as `v1/F15-auth-secrets-rbac` / `cross-cutting/C02-auth-and-rbac`. **The authoritative slug is `cross-cutting/F37-auth-secrets-byok`** — update those soft references when this lands.

---

## 6. Acceptance criteria (numbered, testable)

1. **Migration up/down.** `0002_auth_secrets` creates `platform_api_key` and `oauth_account` with all columns, unique/check constraints, and indexes; `alembic downgrade` cleanly drops them. New enums (`PlatformKeyKind`, `OAuthProvider`, `PrincipalType`) register on `Base.metadata`. (The `audit_log` table + trigger are created by `cross-cutting/F39-audit-log`, not this migration.)
2. **Vault round-trip.** `encrypt(pt, workspace_id=W)` then `decrypt(blob, workspace_id=W) == pt`; the blob contains no plaintext substring; format byte + version byte + 12-byte nonce are present.
3. **Per-workspace isolation (AAD binding).** A blob produced for workspace `W1` fails `decrypt(blob, workspace_id=W2)` with `DecryptionError` (GCM auth failure via the workspace-bound AAD); a one-bit flip in the blob also raises.
4. **Key rotation.** With KEK versions `{1,2}` and active=2, `rotate(blob_v1, workspace_id=W)` returns a v2 blob that `decrypt`s to the original; secrets encrypted under a retired version still decrypt; `secrets_service.rotate_workspace_secrets` re-encrypts every row and audits `secret.key_rotated`.
5. **BYOK never leaks.** `POST /secrets` returns `SecretMeta` with `key_prefix` but no secret; there is **no** route that returns plaintext; `GET /secrets`/`/secrets/{id}` never include the value; the value appears in no log line or audit row. `reveal_for_use` returns plaintext only in-process and has no HTTP route.
6. **Platform key one-time plaintext.** `POST /api-keys` returns `CreatedKey.token` exactly once; subsequent `GET /api-keys` returns only masked `key_prefix`; the stored row has `key_hash` (HMAC) and never the token.
7. **Platform key verify.** A presented token authenticates iff `parse_token` succeeds, the `key_id` row exists, `verify_api_key` (constant-time) matches, and it is neither revoked nor expired; tampered/unknown/revoked/expired tokens → `401`. `last_used_at` is refreshed (throttled, not every request).
8. **No self-escalation.** A `member` creating a key with `role=admin` → `403 escalation`; creating `role≤member` → `201`. An `agent-runner` principal calling `POST /api-keys` → `403`.
9. **OAuth provisioning idempotent.** First `POST /auth/sync` for `(github, subj)` creates a `Workspace` (first user ⇒ `admin`) + `User` + `oauth_account`; a second call for the same `(provider, subject)` returns the same `forge_user_id` with no duplicate rows.
10. **Multi-provider linking.** Linking `google` to an existing user creates a second `oauth_account` → both providers resolve to the same `User`; unlinking the **last** login method → `409`.
11. **Session JWT validation.** A valid HS256 JWT (correct `aud`, unexpired) resolves a `user` `Principal` with the claim's role/workspace; an expired JWT → `401 TokenExpired`; a wrong-secret or wrong-`aud` JWT → `401 InvalidToken`.
12. **No anonymous access.** Every `/api/v1/*` route without a credential returns `401` + `WWW-Authenticate: Bearer`; only `/healthz`, `/readyz`, and (service-token) `/auth/sync` are reachable unauthenticated. Verified by a route-coverage test that asserts no `/api/v1` route is missing the auth dependency/guard.
13. **RBAC enforcement.** `require_role(ADMIN)` rejects `member`/`viewer`/`agent-runner` (`403`), allows `admin`; `require_role(MEMBER)` allows `member`+`admin`, rejects `viewer`/`agent-runner`. `ROLE_RANK` ordering asserted (`viewer=agent_runner<member<admin`).
14. **Tenant isolation.** A principal for workspace `W1` requesting a secret/key resource owned by `W2` gets `404` (no existence leak); all list/read queries are `workspace_id`-scoped. (Audit-row tenant isolation is F39's; F37 exposes no audit query route.)
15. **Secret redaction.** `SecretRedactor.redact` strips AWS keys, PEM blocks, `*_KEY=`/`*_TOKEN=` assignments, bearer tokens, and high-entropy strings; a registered known secret value is scrubbed wherever it appears; non-secret text is preserved. Applied to every `AuditEvent.metadata` before emission and the logging filter (and re-applied by F39's `SqlAuditWriter` before persistence).
16. **Audit emission (producer).** Each of login, oauth link/unlink, user provisioning, secret create/access/delete/rotate, key create/revoke/expire, role change, and rate-limit breach emits exactly one canonical `AuditEvent` through the injected `AuditSink` (asserted with an in-memory `FakeAuditSink`) with the correct `action`, `outcome`, redacted `metadata`, and `Principal`→`actor_type`/`actor_label` mapping; key/role/secret mutations are emitted as **critical** events via `emit(session, ...)` (fail-closed). No mutation path skips emission, and no emitted event contains a secret substring. (The `audit_log` table, its immutability trigger, the hash chain, and the query API are owned by `cross-cutting/F39-audit-log` and asserted in its suite.)
17. **Agent-token expiry.** An `agent_runner` key requires `expires_at`; a key past `expires_at` fails `verify` immediately (authoritative); `auth.purge_expired_keys` then sets `revoked_at` and emits one `apikey.expired` `AuditEvent` each; re-run is idempotent (no second event).
18. **Rate limiting.** Exceeding `RATE_LIMIT_USER_PER_MIN` for a principal returns `429` + `Retry-After` and audits `rate_limit.exceeded`; under the limit passes; buckets are per-workspace and per-user/key independently.
19. **`create-admin` CLI.** `forge-cli users create-admin` creates a workspace + an `admin` user + a first admin platform key, printing the token once; re-running is safe (idempotent on the admin email).
20. **Determinism / fail-closed config.** Missing `FORGE_VAULT_KEYS` / `API_KEY_PEPPER` / `AUTH_SECRET` in a non-dev env fails startup with a clear error (no silent insecure default); `decrypt` of a malformed blob raises rather than returning garbage.

### Traceability: spec requirement → criteria

| Spec (Security / Auth & Secrets) | Criteria |
|---|---|
| Secrets encrypted at rest, per-workspace isolation | 2, 3, 4, 5, 20 |
| BYOK (bring your own key) | 4, 5, 19 |
| Automatic expiry for agent tokens | 8, 17 |
| RBAC (admin/member/viewer/agent-runner) | 8, 13, 19 |
| OAuth providers (Google/GitHub/GitLab) | 9, 10, 11 |
| API key management | 6, 7, 8 |
| Auth: OAuth + API key; no anonymous API access | 11, 12 |
| Secret redaction (logs/traces/results) | 5, 15, 16 |
| Audit events emitted for auth/secret/key actions (table, immutability + query owned by `cross-cutting/F39-audit-log`) | 16, 17 |
| Rate limiting per workspace/user | 18 |
| Tenant isolation / fail-closed | 14, 20 |

---

## 7. Test plan (TDD) — concrete test cases (unit + integration), key fixtures

Write tests first; backend-tdd discipline (≥80% coverage per the spec's own profile). Tests live in `packages/auth-sdk/tests/`, `apps/api/tests/auth/`, `apps/worker/tests/`, and `apps/web/tests/`.

**Unit — vault (`packages/auth-sdk/tests/test_vault.py`, no DB):** round-trip (AC2); cross-workspace AAD failure + bit-flip tamper (AC3); rotation across KEK versions + retired-version decrypt (AC4); malformed-blob `DecryptionError` (AC20); Hypothesis property `decrypt(encrypt(x))==x` for arbitrary text and the blob never contains the plaintext.

**Unit — platform keys (`tests/test_keys.py`):** `generate_api_key` shape (prefix/key_id/secret) per kind; `hash_api_key`/`verify_api_key` match + constant-time mismatch (AC6, AC7); `parse_token` rejects malformed tokens.

**Unit — tokens (`tests/test_tokens.py`):** encode→decode round-trip; expired → `TokenExpired`; wrong secret / wrong `aud` → `InvalidToken` (AC11); service token verify.

**Unit — rbac (`tests/test_rbac.py`):** `ROLE_RANK` ordering; `has_at_least` matrix; `max_grantable_role` caps at actor (AC8, AC13).

**Unit — redaction (`tests/test_redaction.py`):** strips AWS/PEM/`*_KEY=`/bearer/high-entropy; registered known secret scrubbed; non-secret preserved (AC15).

**API integration (`apps/api/tests/auth/`, httpx ASGI + Postgres testcontainer + factory-boy, Celery eager):**
- `test_secrets.py` (AC5, AC14) — create returns prefix-only; no reveal route; GETs never leak; cross-workspace 404.
- `test_api_keys.py` (AC6, AC7, AC8) — one-time token; verify happy/revoked/expired/tampered; escalation 403; agent-runner cannot mint.
- `test_auth_sync.py` (AC9, AC10) — provisioning idempotency; multi-provider link; last-method unlink 409; service-token gate on `/auth/sync`.
- `test_session_jwt.py` (AC11) — valid/expired/wrong-aud JWT resolution.
- `test_no_anonymous.py` (AC12) — parametrized over **every** mounted `/api/v1` route: no credential → 401; route-coverage assertion that each route declares the auth dependency.
- `test_rbac_routes.py` (AC13) — role matrix against representative admin/member routes.
- `test_audit_emission.py` (AC16) — each mutation emits exactly one redacted `AuditEvent` into a `FakeAuditSink` with the correct `action` + `Principal`→`actor_type`/`actor_label` mapping; key/role/secret mutations use `emit(session, ...)` (critical), login/oauth/rate-limit use `emit_async`; no secret substring in any emitted event. (Table immutability + hash chain are F39's suite, not asserted here.)
- `test_rate_limit.py` (AC18) — breach → 429 + Retry-After + emitted `rate_limit.exceeded` event; under-limit passes; per-key vs per-user independence.
- `test_migration_0002_up_down.py` (AC1) — `platform_api_key` + `oauth_account` tables/constraints/indexes via `pg_indexes`; downgrade clean. (No `audit_log` table or trigger — owned by F39.)

**Worker (`apps/worker/tests/test_auth_tasks.py`, AC17):** seed expired + live + revoked keys; run `purge_expired_keys`; assert `revoked_at` set + one `apikey.expired` `AuditEvent` emitted (into `FakeAuditSink`) each; idempotent re-run; verify rejects expired even before purge.

**CLI (`apps/api/tests/auth/test_cli.py`, AC19):** `create-admin` creates workspace+admin+key, prints token once; re-run idempotent. `secrets rotate-key` re-encrypts + audits (AC4).

**Frontend (`apps/web/tests/`, Vitest + RTL + MSW; Playwright e2e):**
- `CreateApiKeyDialog.test.tsx` — token shown once; role picker disables roles above current user.
- `SecretForm.test.tsx` — no reveal control; create shows prefix afterward.
- `login.spec.ts` (Playwright) — provider buttons present; unauthenticated `(app)` route redirects to `/login`; mocked OAuth completes to the board.

**Key fixtures:** `fixed_key_provider` (deterministic KEK map for vault tests), `vault` (SecretVault over it), `workspace_factory`, `user_factory(role=...)`, `platform_key_factory(kind, role, expires_at=None)`, `oauth_account_factory`, `principal(user|key|service)` builder, `mint_jwt(claims)` helper (HS256 over the test `AUTH_SECRET`), `service_token` fixture, `FakeAuditSink` (an in-memory implementation of F39's `forge_contracts.audit.AuditSink` that records emitted events for assertion), and `seed_workspace_with_roles` (one of each role) reused across RBAC + audit-emission tests.

---

## 8. Security & policy considerations

- **Encryption at rest, per-workspace isolation.** AES-256-GCM envelope encryption with an HKDF-derived per-workspace DEK and the `workspace_id` bound into the GCM AAD — a stolen ciphertext is useless without the KEK *and* worthless outside its workspace (AC2, AC3). KEK material lives only in env/secret-manager, never in the DB; rotation + crypto-shred supported (AC4). Startup fails closed if key material is absent (AC20).
- **Hash, don't encrypt, inbound auth.** Platform API keys are one-way HMAC-SHA256 (with a server pepper), never decryptable — a DB dump cannot recover any usable token; high-entropy tokens make a fast MAC safe and keep the hot auth path O(1) (AC6, AC7). BYOK secrets are *reversibly* encrypted because Forge must use them outbound; the two primitives are deliberately separate tables.
- **No self-escalation.** Key/role creation is capped at the actor's own role; `agent-runner` principals can neither mint keys nor write secrets nor read audit (AC8) — the auth half of Build-Prompt constraint #2.
- **No anonymous access, fail-closed.** A global guard + per-route `require_role` ensures every `/api/v1` route is authenticated; the route-coverage test (AC12) prevents a future unguarded route from leaking. Unknown/expired/tampered credentials are rejected, never coerced.
- **Automatic agent-token expiry.** `agent_runner` keys must carry `expires_at`, are rejected at verify time once past, and are purged + audited by the beat task (AC17) — directly implements Security "automatic expiry for agent tokens".
- **Secret redaction everywhere.** One canonical `SecretRedactor` is applied to logs, audit metadata, run traces (F10), retrieval results (F05), and the MCP gateway (F09) — secrets cannot reach logs, traces, or responses (AC15, AC16). The per-workspace known-secret registry scrubs the actual stored values, not just patterns.
- **Audit (producer, fail-closed for critical events).** Every auth/secret/key/role/rate-limit mutation emits a canonical `AuditEvent` through the shared `AuditSink` with actor + outcome + redacted metadata (AC16). Key/role/secret mutations are emitted as **critical** events on the caller's session (`emit(session, ...)`) so the audit commits atomically with the action (fail-closed); login/oauth/secret-access/rate-limit events use `emit_async` (fail-open). The permanent, tamper-evident (hash-chain), queryable `audit_log` table + immutability trigger are owned by `cross-cutting/F39-audit-log`.
- **Tenant isolation / no existence leak.** Every table is `workspace_id`-scoped; cross-workspace access returns `404`, never `403` (AC14).
- **Rate limiting / DoS.** Per-workspace + per-user/key Redis buckets bound abuse and brute-force; breaches are audited (AC18).
- **OAuth hardening.** Better Auth runs PKCE on the authorization-code flow; the API trusts only an `aud="forge-api"`, signature-verified, unexpired JWT (AC11); provider tokens are never stored in Forge's tables. GitHub **sign-in** OAuth credentials are kept distinct from the GitHub **App** (F03) credentials to avoid confused-deputy reuse.
- **Capability UI is convenience only.** The web layer hides/disables controls by role, but `require_role`/`get_principal` on the server is always authoritative.

---

## 9. Effort estimate & risk (S/M/L + key risks)

**Effort: L** (~3 engineer-weeks: ~0.75 auth-sdk crypto core — vault/keys/tokens/redaction/rbac + their unit suites; ~0.75 api deps/services/routers + middleware (auth guard, rate limit) + migration; ~0.5 Better Auth wiring + provisioning + sign-in/settings UI; ~0.5 audit log + CLI + worker expiry; ~0.5 integration tests + no-anonymous route coverage).

| Risk | Severity | Mitigation |
|---|---|---|
| **Crypto implemented incorrectly** (nonce reuse, missing AAD, key-version confusion) leaks or corrupts secrets | High | Single small `vault.py` using `cryptography` `AESGCM` + `HKDF`; random 96-bit nonce per encryption; workspace-bound AAD; exhaustive round-trip/tamper/rotation/Hypothesis tests (AC2–4, 20); never hand-roll primitives |
| **Web (Better Auth, TS) ↔ API (FastAPI, Py) auth seam** drifts or mis-verifies the JWT | High | The contract is a single typed `SessionClaims` JWT (HS256/`AUTH_SECRET`, `aud=forge-api`); API side fully testable with `mint_jwt`; provisioning isolated behind `/auth/sync`; document the seam as an ADR |
| **An unguarded route** ships, allowing anonymous access | High | Global no-anonymous middleware + a route-coverage test (AC12) that fails CI if any `/api/v1` route lacks the auth dependency |
| **BYOK plaintext leaks** into a response/log/audit | High | No reveal route; `reveal_for_use` in-process only; `SecretRedactor` on all audit metadata + logs; tests assert no secret substring anywhere (AC5, 15, 16) |
| **Key material / config missing in prod** → silent insecure default | High | Fail-closed startup validation for `FORGE_VAULT_KEYS`/`API_KEY_PEPPER`/`AUTH_SECRET` (AC20); key-gen helper documented |
| `last_used_at` write-per-request hot-path contention | Med | Throttled update (only if stale > 60s); indexed `key_id` lookup |
| F30 later replacing flat RBAC mis-maps a permission | Med | F37 keeps `require_role` semantics simple + documented; F30 ships the shim + per-route tests |
| Redaction false-negatives | Low/Med | Shared, well-tested pattern set + entropy heuristic + dynamic known-secret registry; applied at single egress points |

---

## 10. Key files / paths (exact)

**Contracts:**
- `packages/contracts/forge_contracts/auth.py` (auth/secret/key DTOs + `Principal`/`SessionClaims`/`Vault`/`KeyProvider`/`RateLimiter`/`SecretRedactor`; imports `AuditEvent`/`AuditSink` from `forge_contracts/audit.py`, owned by `cross-cutting/F39-audit-log`)

**Core package (`packages/auth-sdk`):**
- `packages/auth-sdk/pyproject.toml`
- `packages/auth-sdk/forge_auth/__init__.py`
- `packages/auth-sdk/forge_auth/{models,protocols,vault,keys,tokens,rbac,redaction,errors}.py`
- `packages/auth-sdk/tests/test_{vault,keys,tokens,rbac,redaction}.py`

**Data model + migration:**
- `packages/db/forge_db/models/{platform_api_key,oauth_account}.py` (NOT `audit_log` — owned by `cross-cutting/F39-audit-log`)
- `packages/db/forge_db/models/enums.py` (add `PlatformKeyKind`, `OAuthProvider`, `PrincipalType`)
- `packages/db/forge_db/models/__init__.py` (register new models/enums)
- `packages/db/forge_db/migrations/versions/0002_auth_secrets.py`

**API (`apps/api/forge_api`):**
- `deps/auth.py` (`get_principal`, `require_role`, `require_admin`, `get_current_user`, `require_self_or_admin`)
- `middleware/{auth_guard,rate_limit}.py`
- `services/{auth_service,secrets_service,api_key_service,auth_audit}.py` (`auth_audit.py` = `Principal`→`AuditEvent` mapper emitting via the injected F39 `AuditSink`; no audit table/sink defined here)
- `routers/{auth,secrets,api_keys}.py` (no `audit.py` — the query/viewer surface is F39's)
- `schemas/auth.py`
- `cli/{users,secrets}.py` (`users create-admin`, `secrets rotate-key`)
- `apps/api/tests/auth/test_*.py`

**Worker:**
- `apps/worker/forge_worker/tasks/auth.py` (`purge_expired_keys`, `reencrypt_workspace_secrets` + beat schedule)
- `apps/worker/tests/test_auth_tasks.py`

**Frontend (`apps/web`):**
- `apps/web/lib/auth/{server,client,useCapabilities}.ts`, `apps/web/lib/api/client.ts`, `apps/web/middleware.ts`
- `apps/web/app/(auth)/login/page.tsx`
- `apps/web/app/(app)/settings/{account,secrets,api-keys}/page.tsx` (the `audit` settings page is owned by `cross-cutting/F39-audit-log`)
- `apps/web/components/auth/{LinkedAccounts,SecretForm,SecretsTable,ApiKeyTable,CreateApiKeyDialog}.tsx`
- `apps/web/tests/auth/*.{test.tsx,spec.ts}`

**Infra / docs:**
- `.env.example`, `deploy/.env.production.example` (vault keys, pepper, OAuth apps, internal token, rate limits)
- `deploy/docker-compose.yml` (Better Auth migrate step in `make setup`; no new service)
- `docs/self-hosting/security.md` (key generation, rotation, credential hardening)

---

## 11. Research references (relevant links from the spec/research report)

- `docs/FORGE_SPEC.md` → **"Auth & Secrets Service"** module ("BYOK, workspace isolation, API key management, OAuth providers") and **"Security"** table (encrypted at rest + per-workspace isolation + automatic agent-token expiry; RBAC admin/member/viewer/agent-runner; immutable queryable audit log; secret redaction; OAuth + API key, no anonymous access; rate limiting per workspace/user/provider).
- `docs/FORGE_SPEC.md` → **Technology Stack**: "Auth: Better Auth / Auth.js — OSS-friendly; OAuth + API key"; "Secrets: Encrypted Postgres vault (V1), HashiCorp Vault (V2) — Required for BYOK".
- `docs/FORGE_SPEC.md` → **Core Data Model**: `Workspace → User[] (roles: admin, member, viewer, agent-runner)`, `APIKey[] (BYOK: model provider keys, integration tokens)`.
- `docs/FORGE_SPEC.md` → **Integrations → V1**: "OAuth: Google, GitHub, GitLab sign-in".
- `docs/FORGE_SPEC.md` → **Build Prompt** constraint #2 ("the agent never self-assigns permissions or expands its own scope") and the quality bar ("audit, and fully trust this system in production").
- `docs/FORGE_SPEC.md` → **Docker Compose Production**: `forge-cli db migrate`, `forge-cli users create-admin`, `.env` fields (`SECRET_KEY`, `MODEL_PROVIDER_KEY`).
- Better Auth (OAuth + API key, JWT plugin): https://www.better-auth.com/
- RFC 8707 (resource-bound tokens) — the same least-privilege token-binding discipline F09 applies to MCP OAuth; referenced for OAuth hardening.
- In-repo precedent + reconciliation: `docs/implementation-slices/cross-cutting/F39-audit-log.md` (the **owner** of the central `audit_log` table, the `forge_contracts.audit.AuditEvent`/`AuditSink` contract, `SqlAuditWriter`, `attach_immutability_trigger`, and the audit query/viewer — F37 is a producer into this sink); `docs/implementation-slices/v3/F30-multi-team-rbac.md` (scoped RBAC that builds on this `Principal`/vault and emits to F39's sink); `docs/implementation-slices/v1/F09-mcp-gateway-v1.md` (vault credential storage, the canonical `SecretRedactor`, internal service token, and audit fan-out to F39); and `packages/db/forge_db/models/workspace.py` / `models/base.py` (the existing `Workspace`/`User`/`APIKey` substrate + mixins F37 builds on).
- `docs/forge-research-report.md` → "Self-Hosting and Deployment" and "Technology Recommendations" (FastAPI + Next.js stack the auth seam spans) and "Model Context Protocol → Security considerations" (least-privilege, validate-before-use principles applied here).

---

## 12. Out of scope / future

- **Scoped / hierarchical RBAC** (per-team, per-project grants, `require_permission`, `role_grants`) — owned by `cross-cutting/F30-multi-team-rbac`, which builds directly on F37's `Principal` and `require_role` shim. F37 ships only the flat workspace role.
- **Central audit log infrastructure** — the `audit_log` table, the `AuditEvent`/`AuditSink` contract, `SqlAuditWriter`, the hash chain + immutability trigger, the chain verifier, the NDJSON export, and the audit viewer/query API are all owned by `cross-cutting/F39-audit-log`. F37 only *produces* auth-domain `AuditEvent`s into F39's `AuditSink` and contributes the auth slice of the `AuditAction` vocabulary.
- **HashiCorp Vault backend** (V2 secrets) — F37's `Vault` protocol is the seam; the Postgres envelope vault is the V1 implementation, a Vault-backed `KeyProvider`/`Vault` is a drop-in future.
- **Enterprise SSO (SAML, SCIM)** — Phase-3; SCIM provisioning will map external groups to Forge users/roles via `auth_service`.
- **Password / email-magic-link auth** — out of scope; V1 is OAuth + API key only (per spec).
- **Per-model-provider rate limiting** — F37 ships per-workspace/per-user/per-key limiting; provider-scoped limits are enforced at the BYOK call site in the agent runtime (F06/F08) where the outbound model call happens.
- **Secret versioning / "previous value" history for BYOK** — F37 stores the current ciphertext only; versioned secret history is future.
- **Break-glass temporary elevation & approval-to-grant** — future; F37 supports `expires_at` on keys but not an approval flow for temporary role elevation.
- **WebAuthn / hardware-key 2FA on the platform key creation flow** — future hardening.
- **Distributed/JWKS-based asymmetric session tokens** — F37 uses HS256 over the shared `AUTH_SECRET` for V1 simplicity; an EdDSA + JWKS rotation scheme (Better Auth default) is a documented, compatible upgrade behind `decode_session_jwt`.
