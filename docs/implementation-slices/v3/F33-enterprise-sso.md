# F33 — Enterprise SSO (SAML, SCIM)

> Phase: v3 · Spec module(s): Auth & Secrets Service (BYOK, workspace isolation, OAuth providers, API key management), Security (RBAC roles admin/member/viewer/agent-runner; secrets encrypted at rest; immutable audit log; per-workspace isolation), Multi-team workspace controls & full RBAC hierarchy (V3 sibling `v3/F30-multi-team-rbac`) · Status target: **Done** = a workspace admin can configure a SAML 2.0 identity provider (Okta / Entra ID / Google Workspace / generic) for their workspace, end users sign in via SP-initiated and IdP-initiated SAML with signed, validated, replay-protected assertions that just-in-time provision or link a Forge `User`; an IdP can automatically provision, update, deactivate, and group-assign users through a conformant SCIM 2.0 service-provider API authenticated by a per-workspace bearer token; SCIM `active=false` / DELETE deprovisions a user and revokes their sessions, agent tokens, and SSO; every SSO/SCIM action is written to the immutable audit log with secrets redacted; the entire SAML and SCIM surface is exercised offline against signed fixtures and golden SCIM request/response pairs with zero live IdP calls. Lint + types + `pytest` green on `apps/api/forge_api/sso/`, the SAML + SCIM routers, the worker SSO tasks, and the new `packages/db` models + migration; new-code coverage ≥ 80%.

---

## 1. Intent — what & why

V1 ships authentication as social OAuth (Google, GitHub, GitLab) plus workspace API keys (the Auth & Secrets Service). That is sufficient for individual teams but blocks enterprise adoption, where IT mandates **centralized identity** (one corporate IdP, conditional access, MFA at the IdP) and **automated lifecycle** (joiners/movers/leavers managed by the directory, not by hand in each app). F33 closes that gap by adding the two protocols every enterprise buyer's security review requires:

1. **SAML 2.0 (Forge as Service Provider).** Per-workspace federation with a corporate IdP. Forge generates SP metadata, issues signed `AuthnRequest`s (SP-initiated), accepts and *cryptographically validates* `SAMLResponse`s at an Assertion Consumer Service (ACS) endpoint (signature, audience, conditions window, `InResponseTo`, replay), maps assertion attributes to a Forge identity, and just-in-time (JIT) provisions or links a `User` scoped to exactly the workspace that owns the IdP config. Email-domain Home-Realm-Discovery routes corporate users to their IdP from the normal login screen. Single Logout (SLO) is supported.

2. **SCIM 2.0 (Forge as Service Provider — RFC 7643 schema, RFC 7644 protocol).** The IdP (Okta, Entra ID, OneLogin, etc.) pushes the user/group lifecycle: create, read, search (`filter`), update (`PUT`/`PATCH`), and deactivate/delete `Users` and `Groups`. SCIM is authenticated with a per-workspace, hashed-at-rest bearer token. Group membership maps to Forge RBAC roles (admin/member/viewer/agent-runner). Deactivation propagates to session/token revocation so a removed employee loses access immediately.

Why this is its own V3 slice and not folded into V1 auth: SAML/SCIM are high-surface, security-critical protocols with their own threat models (XML signature wrapping, XXE, assertion replay, confused-deputy provisioning, SCIM token leakage). They are opt-in per workspace, must not regress the V1 OAuth path, and depend on the V3 multi-team RBAC hierarchy for group→role mapping. The slice extends — never replaces — the V1 `Principal`/session/secrets substrate.

---

## 2. User-facing behavior / journeys

**J1 — Admin configures SAML (workspace admin).**
Admin opens Settings → Security → Single Sign-On. They paste their IdP **metadata URL** (or upload metadata XML, or enter `entity_id` + `sso_url` + signing `x509cert` manually), set the email **domains** that should be routed to this IdP (e.g. `acme.com`), choose the **default role** for JIT-provisioned users, optionally map **IdP groups → Forge roles**, and toggle `allow_idp_initiated`, `sign_authn_requests`, `want_assertions_signed`. They click **Download SP metadata** (or copy the ACS URL + SP entity id + SP signing cert) to paste into their IdP. A **Test connection** button runs an SP-initiated round trip in a popup and reports the parsed NameID + attributes without creating a session.

**J2 — Employee signs in via SAML (SP-initiated).**
On the login screen the user types `dana@acme.com` and clicks **Continue**. Forge looks up `acme.com` in SSO domain config, finds Acme's IdP, builds a signed `AuthnRequest`, and 302-redirects to the IdP SSO URL. The user authenticates at the IdP (corporate MFA etc.). The IdP POSTs a `SAMLResponse` to Forge's ACS. Forge validates it, finds an existing `external_identity` for that NameID (or JIT-provisions a new `User` in Acme's workspace with the default role), establishes a Forge session, and lands the user on their board. RelayState carries the originally requested deep link.

**J3 — Employee signs in via SAML (IdP-initiated).**
From the Okta/Entra app dashboard the user clicks the **Forge** tile. The IdP POSTs an unsolicited `SAMLResponse` to ACS (no `InResponseTo`). Forge accepts it **only if** `allow_idp_initiated=true` for that IdP config; otherwise it is rejected. Same JIT/link + session result.

**J4 — Directory provisions/deprovisions users (SCIM, no human in Forge).**
IT assigns the Forge app to a new hire in Okta. Okta calls `POST /scim/v2/Users` on Forge with a bearer token. Forge creates a (login-disabled-until-first-SSO or active) `User` linked by `externalId`. When the employee changes name or group, Okta `PATCH`es the user. When the employee is offboarded, Okta sets `active=false` (or `DELETE`s) — Forge marks the `User` inactive, revokes all their sessions and agent tokens, and refuses subsequent SSO. Group assignments push role changes (e.g. moving a user into the `forge-admins` IdP group elevates them to `admin`).

**J5 — Admin rotates a SCIM token / IdP cert.**
Admin generates a SCIM token (shown once, copied into the IdP), with optional expiry; old token can be revoked. When the IdP rotates its signing certificate, admin updates the cert (or Forge auto-refreshes from the metadata URL via a scheduled task) and both old+new certs are accepted during the overlap window.

**J6 — Admin disables SSO / falls back.**
Admin can disable the SAML config (workspace reverts to OAuth/password for break-glass admins) without deleting it. At least one local break-glass admin must remain so a misconfigured IdP cannot lock the workspace out (enforced — see §8).

---

## 3. Vertical slice

### 3.1 Data model (tables/columns/migrations touched)

Alembic migration `packages/db/migrations/versions/00NN_enterprise_sso.py` (revision id `enterprise_sso`) in the **authoritative** `packages/db/migrations/` tree (baseline `0001_baseline`). It chains after `cross-cutting/F37-auth-secrets-byok`'s `0002_auth_secrets` (which creates `workspace`/`app_user`/`platform_api_key`), and after `v3/F30-multi-team-rbac`'s migration when that lands (since group→role provisioning then targets `role_grants`); `down_revision` is reconciled to the migration head when the slice is built (`00NN` follows the latest revision in the tree). All new tables are workspace-scoped (`WorkspaceScopedModel`). New models live under `packages/db/forge_db/models/sso.py`; register in `forge_db/models/__init__.py`. New enums added to `forge_db/models/enums.py`.

**New enums (`enums.py`):**

```python
class SsoProtocol(enum.StrEnum):
    SAML = "saml"            # SCIM is a provisioning channel, not a sign-in protocol

class ExternalIdentityProvider(enum.StrEnum):
    SAML = "saml"
    SCIM = "scim"
    OAUTH = "oauth"          # also used by V1 social login linkage

class ScimResourceType(enum.StrEnum):
    USER = "User"
    GROUP = "Group"
```

**`sso_configuration`** (one active SAML config per workspace; unique on `workspace_id`):

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `workspace_id` | UUID FK → `workspace.id` | tenant scope; **UNIQUE** (one SAML config per workspace in V3) |
| `protocol` | enum `SsoProtocol` | `saml` |
| `enabled` | bool | default `false`; J6 disable without delete |
| `metadata_url` | TEXT null | if set, IdP fields auto-refreshed by `refresh_saml_metadata` |
| `idp_entity_id` | TEXT | IdP issuer / entity id |
| `idp_sso_url` | TEXT | IdP SSO (Redirect/POST) endpoint |
| `idp_slo_url` | TEXT null | IdP Single Logout endpoint |
| `idp_x509_certs` | JSONB | list[str] PEM signing certs (supports rollover); **public**, not secret |
| `name_id_format` | TEXT | default `urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress` |
| `sp_entity_id` | TEXT | computed `{public_url}/auth/saml/{slug}/metadata`; stored for stability |
| `sp_private_key_encrypted` | BYTEA | SP signing key (Fernet via secrets vault); used to sign AuthnRequest/SLO |
| `sp_cert_pem` | TEXT | SP signing cert (public; published in SP metadata) |
| `allow_idp_initiated` | bool | default `false` |
| `sign_authn_requests` | bool | default `true` |
| `want_assertions_signed` | bool | default `true` |
| `want_name_id_encrypted` | bool | default `false` |
| `attribute_mapping` | JSONB | `AttributeMapping` (§4) |
| `default_role` | enum `UserRole` | role for JIT users without a group mapping; default `member` |
| `group_role_map` | JSONB | `{idp_group_value: forge_role}` |
| `jit_provisioning` | bool | default `true` |
| `domains` | JSONB | list[str] lower-cased email domains for HRD; each domain unique across all workspaces |
| `last_metadata_refresh_at` | TIMESTAMPTZ null | |
| `created_at`/`updated_at` | TIMESTAMPTZ | |

Indexes: `UNIQUE(workspace_id)`; a separate uniqueness guarantee on each domain (see `sso_domain` below).

**`sso_domain`** (HRD lookup; a domain routes to exactly one IdP globally):

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `domain` | TEXT | lower-case; **GLOBALLY UNIQUE** (`uq_sso_domain_domain`) |
| `sso_configuration_id` | UUID FK → `sso_configuration.id` ON DELETE CASCADE | |
| `workspace_id` | UUID FK → `workspace.id` | denormalized for tenant filters |
| `verified` | bool | default `false`; domain ownership verification gate (see §8) |

> Stored separately from `sso_configuration.domains` JSONB so the unique constraint is enforceable in the DB. The JSONB column is the editable input; the service projects it into rows.

**`external_identity`** (links a Forge `User` to an IdP subject for SAML/SCIM federation; **complements** — does not replace — `cross-cutting/F37-auth-secrets-byok`'s primary `app_user.auth_provider`/`auth_subject` and its `oauth_account` link table, which keep owning social-OAuth identity):

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `workspace_id` | UUID FK → `workspace.id` | tenant scope |
| `user_id` | UUID FK → `app_user.id` ON DELETE CASCADE | |
| `provider` | enum `ExternalIdentityProvider` | `saml` \| `scim` \| `oauth` |
| `idp_entity_id` | TEXT null | for SAML |
| `external_id` | TEXT | SAML NameID, or SCIM `externalId`/`id` |
| `name_id_format` | TEXT null | SAML |
| `scim_resource_id` | TEXT null | the SCIM `id` Forge issues for this user (for SCIM resource URLs) |
| `last_login_at` | TIMESTAMPTZ null | |
| `created_at`/`updated_at` | TIMESTAMPTZ | |

Constraints: `UNIQUE(workspace_id, provider, external_id)`, `UNIQUE(workspace_id, scim_resource_id)` where not null, index on `user_id`. **Raw assertion attributes are NOT persisted** (PII/secret-redaction; only the mapped, minimal fields land on `app_user`/`external_identity`).

**`scim_token`** (per-workspace SCIM bearer credential):

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `workspace_id` | UUID FK → `workspace.id` | tenant scope |
| `name` | TEXT | label, e.g. "Okta production" |
| `token_hash` | TEXT | SHA-256 of the raw token (constant-time compared); raw token shown once |
| `token_prefix` | TEXT | first 8 chars for UI display / log correlation |
| `created_by` | UUID FK → `app_user.id` null | |
| `last_used_at` | TIMESTAMPTZ null | |
| `expires_at` | TIMESTAMPTZ null | |
| `revoked_at` | TIMESTAMPTZ null | soft-revoke |

Index: `UNIQUE(workspace_id, name)`, index on `token_prefix`.

**`scim_group`** (SCIM-managed group → role/team mapping):

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `workspace_id` | UUID FK → `workspace.id` | tenant scope |
| `scim_id` | TEXT | Forge-issued SCIM resource id |
| `external_id` | TEXT null | IdP group externalId |
| `display_name` | TEXT | |
| `mapped_role` | enum `UserRole` null | resolved from `group_role_map`; null = no role effect |
| `created_at`/`updated_at` | TIMESTAMPTZ | |

Constraints: `UNIQUE(workspace_id, scim_id)`, `UNIQUE(workspace_id, display_name)`. Membership is derived (a user's effective role = highest-privilege role across their SCIM group memberships, falling back to `default_role`); membership edges stored in `scim_group_member` (`group_id`, `user_id`, `UNIQUE` pair) for `PATCH`/`PUT` member ops.

**Extend `app_user`** (`models/workspace.py`): add `deactivated_at: datetime | None` (set on SCIM deprovision; distinct from `is_active=false` so we keep an audit trail of *when*), and `external_managed: bool` (default `false`; `true` when the user is owned by SCIM, blocking manual edits that the directory will overwrite). `is_active`, `auth_provider`, `auth_subject` already exist and remain (V1 OAuth).

**Sessions / agent tokens (referenced, owned by `cross-cutting/F37-auth-secrets-byok`):** F33 calls F37's session layer `revoke_all_for_user(workspace_id, user_id)` and F37's `api_key_service.revoke_key(...)` to revoke the user's `platform_api_key` rows (including `kind=agent_runner`) on deprovision. If F37 sessions are JWT-stateless, F33 adds a `session_revocation` deny-list keyed by `user_id` + `revoked_after` timestamp (a small table `session_revocation(user_id PK, revoked_after TIMESTAMPTZ)` checked by F37's session dependency).

**Replay protection store:** SAML assertion IDs and outstanding `AuthnRequest` ids live in **Redis** (`saml:assertion:{id}` and `saml:authnreq:{id}`) with TTL = assertion validity window + skew. A Postgres fallback table `saml_replay(assertion_id PK, expires_at)` is specified for deployments running without Redis (eviction by a periodic cleanup task).

### 3.2 Backend (FastAPI routes + services/packages)

All SSO logic lives in **`apps/api/forge_api/sso/`** (extends the V1 auth package `apps/api/forge_api/auth/`). Pure, FastAPI-free modules so they are unit-testable and importable by the worker:

| Module | Responsibility |
|---|---|
| `sso/saml.py` | `SamlSpService` — build `AuthnRequest` (signed), validate `SAMLResponse`, build/parse SLO. Wraps the SAML toolkit; exposes the `SamlValidator` Protocol (§4). |
| `sso/saml_metadata.py` | Pure: parse IdP metadata XML → `SamlIdpConfig`; render SP metadata XML from a config. **XXE-hardened** lxml parser (`resolve_entities=False`, `no_network=True`, DTD load off). |
| `sso/scim_models.py` | Pydantic RFC 7643 resources (`ScimUser`, `ScimGroup`, `ScimListResponse`, `ScimError`, `ScimPatchOp`) — re-exported from `packages/contracts`. |
| `sso/scim_service.py` | `ScimUserService`, `ScimGroupService` — map SCIM ↔ `User`/`scim_group`; create/get/list/replace/patch/deactivate. |
| `sso/scim_filter.py` | RFC 7644 §3.4.2.2 filter parser → SQLAlchemy predicate (supports `eq`, `ne`, `co`, `sw`, `ew`, `pr`, `and`, `or` on `userName`,`externalId`,`active`,`emails.value`). |
| `sso/provisioning.py` | `link_or_jit_provision(config, assertion)` and `deprovision_user(...)` (revoke sessions/tokens) — shared by SAML and SCIM. |
| `sso/attribute_mapping.py` | Pure: apply `AttributeMapping` + `group_role_map` to assertion/SCIM attributes → `(email, name, role, groups)`. |
| `sso/errors.py` | `SamlValidationError`, `ScimError`-raising helpers, `SsoConfigError`, `DomainConflictError`, `LastAdminError`. |

**Routers** (registered in the FastAPI app):

`apps/api/forge_api/routers/sso_admin.py` — config management; **RBAC `admin` only** via `require_role("admin")`:

| Method & path | Purpose |
|---|---|
| `GET /workspaces/{ws}/sso` | Return `SsoConfigOut` (cert/public fields; **no** SP private key). |
| `PUT /workspaces/{ws}/sso` | Create/replace SAML config (`SsoConfigIn`); fetches metadata if `metadata_url`; generates SP keypair if absent; projects `domains` into `sso_domain` rows (409 `domain_conflict` on collision). |
| `POST /workspaces/{ws}/sso/disable` / `…/enable` | Toggle `enabled` (enforces ≥1 break-glass admin before disabling local auth — §8). |
| `DELETE /workspaces/{ws}/sso` | Remove config + domains (cascade); SCIM-managed users retained. |
| `POST /workspaces/{ws}/sso/test` | Run a validation-only round trip; returns parsed NameID + attributes; never creates a session. |
| `GET /workspaces/{ws}/scim/tokens` / `POST` / `DELETE …/{token_id}` | Manage SCIM tokens; `POST` returns the raw token **once**. |

`apps/api/forge_api/routers/saml.py` — the SAML protocol surface; **unauthenticated** (the SAML response *is* the auth), workspace resolved by slug in the path:

| Method & path | Purpose |
|---|---|
| `GET /auth/saml/{ws_slug}/metadata` | SP metadata XML (`application/xml`). |
| `GET /auth/saml/{ws_slug}/login` | SP-initiated: build signed `AuthnRequest`, store request id (Redis), 302 to IdP; `?next=` → RelayState. |
| `POST /auth/saml/{ws_slug}/acs` | ACS: validate `SAMLResponse`, JIT/link, create session, 302 to RelayState/next. |
| `GET\|POST /auth/saml/{ws_slug}/slo` | SLO endpoint (handle IdP-initiated logout + SP-initiated `LogoutRequest`). |
| `POST /auth/saml/discover` | HRD: `{email}` → `{redirect: "/auth/saml/{slug}/login"}` or `{sso: false}` if domain has no IdP. |

`apps/api/forge_api/routers/scim.py` — SCIM 2.0 service provider; mounted at `/scim/v2`; **bearer auth via `require_scim_token` dependency** (resolves `workspace_id` from the token); `Content-Type: application/scim+json`:

| Method & path | Purpose |
|---|---|
| `GET /scim/v2/ServiceProviderConfig` | Capabilities doc (patch=true, filter=true, bulk=false, etag=false, sort=false). |
| `GET /scim/v2/ResourceTypes`, `GET /scim/v2/Schemas` | Static discovery docs. |
| `GET /scim/v2/Users` | List/search: `filter`, `startIndex`, `count` → `ScimListResponse`. |
| `POST /scim/v2/Users` | Create → 201 + `ScimUser` + `Location`. |
| `GET /scim/v2/Users/{id}` | Read. |
| `PUT /scim/v2/Users/{id}` | Replace. |
| `PATCH /scim/v2/Users/{id}` | Partial (RFC 7644 PatchOp); the `active:false` op deprovisions. |
| `DELETE /scim/v2/Users/{id}` | Deprovision (204). |
| `…/Groups` (GET/POST/GET{id}/PUT/PATCH/DELETE) | Group CRUD + membership ops. |

**Settings** (`apps/api/forge_api/settings.py`): add `FORGE_PUBLIC_URL` (already present per F03), `SAML_CLOCK_SKEW_SECONDS=120`, `SCIM_TOKEN_BYTES=32`, `SAML_AUTHNREQUEST_TTL_SECONDS=600`.

### 3.3 Worker / agent runtime (Celery tasks, LangGraph, if any)

`apps/worker/forge_worker/tasks/sso.py` (queue `auth`); these import the **pure** modules from `forge_api.sso` (the worker image installs `apps/api`):

- `refresh_saml_metadata(sso_configuration_id: str)` — fetch `metadata_url`, parse via `saml_metadata.parse_idp_metadata`, update `idp_entity_id`/`idp_sso_url`/`idp_slo_url`/`idp_x509_certs` (append new certs, keep old during rollover), set `last_metadata_refresh_at`. Idempotent.
- `refresh_all_saml_metadata()` — Celery beat, every 6h: enqueue `refresh_saml_metadata` for every config with a `metadata_url`.
- `cleanup_saml_replay()` — Celery beat, every 15min: delete expired `saml_replay` rows (Postgres-fallback deployments only; no-op when Redis is the replay store).
- `propagate_deprovision(user_id: str, workspace_id: str)` — best-effort fan-out after SCIM deactivation: revoke sessions, expire agent tokens, emit a `user_deprovisioned` audit + board/activity event. The synchronous SCIM handler performs the critical revocation inline; this task handles eventual cleanup (e.g. cancelling in-flight `AgentRun`s owned by the user) so the SCIM call stays fast.

No LangGraph involvement. The agent runtime is a *consumer*: `propagate_deprovision` requests cancellation of the user's running tasks via the `v1/F07-feature-workflow-fsm` `cancel` transition (which drives `v1/F06-single-execution-agent`'s `agent_run_status.cancelled`); F33 only emits the request. `v1/F10-run-trace-viewer` is the read-only trace UI and is **not** a cancellation surface.

### 3.4 Frontend / UI (Next.js routes/components)

Under `apps/web`:

- `app/(auth)/login/page.tsx` (extend): add the email field + **Continue** button that calls `POST /auth/saml/discover`; if `redirect` returned, navigate there; otherwise show the existing OAuth/password options. Add an explicit **Sign in with SSO** affordance.
- `app/(settings)/security/sso/page.tsx` — admin SSO config form: metadata URL / XML / manual fields, domains editor, attribute-mapping editor, group→role map, toggles, **Download SP metadata**, **Copy ACS URL / SP entity id**, **Test connection** (opens `/workspaces/{ws}/sso/test` popup), enable/disable.
- `app/(settings)/security/scim/page.tsx` — SCIM token list, **Generate token** (one-time reveal modal), revoke; shows the SCIM base URL (`{public}/scim/v2`) and last-used timestamps.
- Components (`apps/web/components/sso/`): `SamlConfigForm.tsx`, `AttributeMappingEditor.tsx`, `DomainListEditor.tsx`, `ScimTokenTable.tsx`, `SsoStatusBadge.tsx`, `GroupRoleMapEditor.tsx`.
- Server-side: an SSO-disabled fallback note and a **break-glass admin warning** banner if disabling SSO would orphan the workspace.

### 3.5 Infra / deploy (compose, helm, caddy, if any)

- `deploy/docker-compose.yml` / `.env.example`: no new services. Add env `SAML_CLOCK_SKEW_SECONDS`, `SCIM_TOKEN_BYTES`, `SAML_AUTHNREQUEST_TTL_SECONDS`; confirm `FORGE_PUBLIC_URL` is set (SP entity id, ACS URL, SP metadata, SCIM base URL are derived from it — **must be the externally reachable HTTPS URL**, not `localhost`).
- **`xmlsec1` system dependency**: the SAML toolkit (`python3-saml`) requires `libxml2`/`libxmlsec1` native libs. Add `libxmlsec1-dev`, `libxml2-dev`, `xmlsec1`, `pkg-config` to the `api` and `worker` Dockerfiles' build stage and `libxmlsec1` + `xmlsec1` to the runtime stage. Document in `docs/self-hosting/` (security.md SSO section).
- `deploy/caddy/Caddyfile`: ensure `POST /auth/saml/{ws}/acs` and `/scim/v2/*` are proxied to `api` with bodies **untouched** (SAML signature is over exact XML bytes; SCIM uses `application/scim+json`). No special TLS config beyond Caddy's auto-HTTPS (SAML mandates HTTPS for ACS in production).
- Helm (`deploy/helm/`): expose the same env in `values.yaml`; no new pods.

---

## 4. Public interfaces / contracts (exact signatures, Pydantic models, Protocols, YAML schemas)

Canonical DTOs + Protocols in **`packages/contracts/forge_contracts/sso.py`**:

```python
# packages/contracts/forge_contracts/sso.py
from __future__ import annotations
from datetime import datetime
from typing import Literal, Protocol, runtime_checkable
from pydantic import BaseModel, EmailStr, Field

# ---------- SAML config DTOs ----------

class AttributeMapping(BaseModel):
    # source attribute name in the assertion; "" / None means "use NameID"
    email: str = ""                              # default: NameID
    name: str | None = None
    first_name: str | None = "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/givenname"
    last_name: str | None = "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/surname"
    groups: str | None = None                    # multi-valued group/role attribute

class SamlIdpConfig(BaseModel):
    entity_id: str
    sso_url: str
    slo_url: str | None = None
    x509_certs: list[str] = Field(min_length=1)  # PEM; first is primary, rest for rollover
    name_id_format: str = "urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress"

class SsoConfigIn(BaseModel):
    protocol: Literal["saml"] = "saml"
    enabled: bool = False
    metadata_url: str | None = None
    metadata_xml: str | None = None              # alternative to metadata_url
    idp: SamlIdpConfig | None = None             # alternative to metadata_*
    domains: list[str] = Field(default_factory=list)
    allow_idp_initiated: bool = False
    sign_authn_requests: bool = True
    want_assertions_signed: bool = True
    want_name_id_encrypted: bool = False
    attribute_mapping: AttributeMapping = Field(default_factory=AttributeMapping)
    default_role: Literal["admin", "member", "viewer", "agent-runner"] = "member"
    group_role_map: dict[str, str] = Field(default_factory=dict)   # idp_group -> forge_role
    jit_provisioning: bool = True

class SsoConfigOut(BaseModel):
    id: str
    workspace_id: str
    protocol: Literal["saml"]
    enabled: bool
    idp: SamlIdpConfig
    sp_entity_id: str
    sp_acs_url: str
    sp_slo_url: str
    sp_metadata_url: str
    sp_cert_pem: str                             # public; sp_private_key NEVER serialized
    domains: list[str]
    allow_idp_initiated: bool
    sign_authn_requests: bool
    want_assertions_signed: bool
    attribute_mapping: AttributeMapping
    default_role: str
    group_role_map: dict[str, str]
    jit_provisioning: bool
    last_metadata_refresh_at: datetime | None

# ---------- SAML runtime DTOs ----------

class SamlAssertion(BaseModel):
    name_id: str
    name_id_format: str
    session_index: str | None
    issuer: str
    attributes: dict[str, list[str]]            # raw multi-valued attrs (in-memory only)
    not_on_or_after: datetime
    in_response_to: str | None

class MappedIdentity(BaseModel):
    email: EmailStr
    name: str | None
    role: str                                    # resolved Forge role
    groups: list[str]
    external_id: str                             # NameID
    name_id_format: str

# ---------- SAML Protocols ----------

@runtime_checkable
class SamlValidator(Protocol):
    def build_authn_request(
        self, config: SamlIdpConfig, *, sp_entity_id: str, acs_url: str,
        relay_state: str, sign: bool, sp_private_key_pem: str | None,
    ) -> tuple[str, str]: ...                    # (redirect_url_with_SAMLRequest, request_id)

    def validate_response(
        self, *, saml_response_b64: str, config: SamlIdpConfig,
        sp_entity_id: str, acs_url: str, want_signed: bool,
        expected_in_response_to: str | None, now: datetime, clock_skew_seconds: int,
    ) -> SamlAssertion: ...                      # raises SamlValidationError

@runtime_checkable
class ReplayGuard(Protocol):
    def register_request(self, request_id: str, ttl_seconds: int) -> None: ...
    def consume_request(self, request_id: str) -> bool: ...      # True if present (and deletes)
    def seen_assertion(self, assertion_id: str, ttl_seconds: int) -> bool: ...  # True if replay

# ---------- SCIM 2.0 resources (RFC 7643) ----------

USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
GROUP_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Group"
LIST_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"
PATCHOP_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"

class ScimName(BaseModel):
    givenName: str | None = None
    familyName: str | None = None
    formatted: str | None = None

class ScimEmail(BaseModel):
    value: EmailStr
    type: str | None = "work"
    primary: bool = True

class ScimMeta(BaseModel):
    resourceType: Literal["User", "Group"]
    created: datetime
    lastModified: datetime
    location: str
    version: str | None = None

class ScimGroupRef(BaseModel):
    value: str                                   # group id
    display: str | None = None

class ScimUser(BaseModel):
    schemas: list[str] = Field(default_factory=lambda: [USER_SCHEMA])
    id: str | None = None
    externalId: str | None = None
    userName: str                                # mapped to email
    name: ScimName | None = None
    displayName: str | None = None
    emails: list[ScimEmail] = Field(default_factory=list)
    active: bool = True
    groups: list[ScimGroupRef] = Field(default_factory=list)
    meta: ScimMeta | None = None

class ScimMember(BaseModel):
    value: str                                   # user id
    display: str | None = None

class ScimGroup(BaseModel):
    schemas: list[str] = Field(default_factory=lambda: [GROUP_SCHEMA])
    id: str | None = None
    externalId: str | None = None
    displayName: str
    members: list[ScimMember] = Field(default_factory=list)
    meta: ScimMeta | None = None

class ScimListResponse(BaseModel):
    schemas: list[str] = Field(default_factory=lambda: [LIST_SCHEMA])
    totalResults: int
    startIndex: int = 1
    itemsPerPage: int
    Resources: list[dict]

class ScimError(BaseModel):
    schemas: list[str] = Field(default_factory=lambda: [ERROR_SCHEMA])
    status: str                                  # HTTP status as string
    scimType: str | None = None                  # e.g. "uniqueness", "invalidFilter"
    detail: str | None = None

class ScimPatchOperation(BaseModel):
    op: Literal["add", "remove", "replace"]
    path: str | None = None
    value: object | None = None

class ScimPatchRequest(BaseModel):
    schemas: list[str] = Field(default_factory=lambda: [PATCHOP_SCHEMA])
    Operations: list[ScimPatchOperation]

# ---------- SCIM service Protocols ----------

@runtime_checkable
class ScimUserService(Protocol):
    def create(self, workspace_id: str, payload: ScimUser) -> ScimUser: ...
    def get(self, workspace_id: str, scim_id: str) -> ScimUser: ...
    def list(self, workspace_id: str, *, filter: str | None,
             start_index: int, count: int) -> ScimListResponse: ...
    def replace(self, workspace_id: str, scim_id: str, payload: ScimUser) -> ScimUser: ...
    def patch(self, workspace_id: str, scim_id: str, req: ScimPatchRequest) -> ScimUser: ...
    def deactivate(self, workspace_id: str, scim_id: str) -> None: ...   # active=false / DELETE
```

**Provisioning + deprovisioning service (in `forge_api.sso.provisioning`):**

```python
def link_or_jit_provision(
    *, session, config: "SsoConfiguration", identity: MappedIdentity,
) -> "User":
    """Find external_identity by (workspace, saml, NameID); else find User by
    email within the workspace and attach identity; else JIT-create User when
    config.jit_provisioning, with role=identity.role. Raises SsoConfigError when
    JIT disabled and no user exists. Updates name/role on each login if changed.
    Role is written to flat app_user.role (F37); when v3/F30-multi-team-rbac is
    present, the resolved role is applied as a workspace-scope role_grants entry
    via F30's grant API (the flat column is deprecated for authz there)."""

def deprovision_user(*, session, workspace_id: str, user_id: str) -> None:
    """Set is_active=false, deactivated_at=now; revoke all sessions
    (session store / session_revocation), expire agent tokens (APIKey kind
    SYSTEM/integration owned by user), enqueue propagate_deprovision, write audit."""
```

**SCIM ↔ Forge field mapping (frozen):**

| SCIM | Forge | Notes |
|---|---|---|
| `userName` | `app_user.email` | primary identity |
| `emails[primary].value` | `app_user.email` (fallback) | if `userName` not email-shaped |
| `name.formatted` / `givenName familyName` | `app_user.name` | |
| `externalId` | `external_identity.external_id` (provider=`scim`) | stable IdP key |
| `active` | `app_user.is_active` | `false` → deprovision flow |
| `id` (Forge-issued) | `external_identity.scim_resource_id` | SCIM resource URL |
| Group membership | `scim_group_member` → effective `app_user.role` | via `group_role_map` |

**SAML attribute → role resolution (frozen, in `attribute_mapping.py`):** role = highest-privilege role among `{group_role_map[g] for g in groups}` if any mapped; else `config.default_role`. Privilege ordering reuses `cross-cutting/F37-auth-secrets-byok`'s canonical `ROLE_RANK` (`admin=2 > member=1 > {agent-runner, viewer}=0`) via its `has_at_least(...)` helper — F33 does **not** invent its own ordering; when two mapped groups tie on rank (e.g. `agent-runner` vs `viewer`), resolution is deterministic by the documented secondary order `member > agent-runner > viewer`. An IdP-asserted `admin` is honored **only** if present in `group_role_map` (no implicit admin from arbitrary attributes — non-negotiable #2, §8).

---

## 5. Dependencies — features/slices that must exist first

> Slug reconciliation (matching the convention F15/F24 froze): the authoritative auth/secrets/RBAC slug is **`cross-cutting/F37-auth-secrets-byok`** (it supersedes the stale `cross-cutting/C02-auth-and-rbac` / `v1/F02-auth-workspace-byok` references seen in older siblings); the audit log is **`cross-cutting/F39-audit-log`** (NOT `v1/F10-run-trace-viewer`, which is the read-only trace UI); multi-team RBAC is **`v3/F30-multi-team-rbac`** (`v3/F31` is Deployment Gates). The platform foundation has no dedicated numbered file yet and is referenced as **`cross-cutting/C01-monorepo-and-api-foundations`** (a.k.a. `v1/F00-foundation-substrate`).

**Hard prerequisites:**
- **`cross-cutting/F37-auth-secrets-byok`** (Auth & Secrets Service) — provides the `Workspace` / `app_user` (`role: UserRole`, `auth_provider`, `auth_subject`, `is_active`, `email`) / `platform_api_key` models, the `Principal` + `get_principal` auth dependency, the flat `require_role(min_role)` RBAC dependency plus the canonical `ROLE_RANK` / `has_at_least(...)` ordering F33's group→role resolution reuses, the AES-256-GCM envelope-encrypted per-workspace secrets vault (used here to store `sp_private_key_encrypted`), the **canonical `SecretRedactor`** (reused for AC18, never re-implemented), the `oauth_account` link table + OAuth login path SSO sits beside, and the Better Auth / Auth.js **session** layer. F33 requires F37's session layer to expose a revoke-all hook (`revoke_all_for_user(workspace_id, user_id)`); if F37 sessions are JWT-stateless, F33 adds the `session_revocation` deny-list (§3.1) consulted by F37's session dependency.
- **`cross-cutting/C01-monorepo-and-api-foundations`** (a.k.a. `v1/F00-foundation-substrate`) — `apps/api` FastAPI app + router registration, `apps/worker` Celery app + beat, `packages/db` (`Base`, `WorkspaceScopedModel`, `enum_type`/`json_type` helpers, the authoritative Alembic tree `packages/db/migrations/` with baseline `0001_baseline`), `packages/contracts`, `forge-cli db migrate`, and Redis in compose (replay store).
- **`cross-cutting/F39-audit-log`** — the ONE canonical frozen `AuditEvent` contract + `AuditSink` Protocol (`forge_contracts.audit`) and `SqlAuditWriter` every SSO/SCIM action records through. F33 emits its security events as `severity=critical` on the caller's session (fail-closed), so a failed audit write rolls the action back (§6 AC19).

**Soft / integration points (need not exist first; F33 emits against frozen contracts):**
- **`v3/F30-multi-team-rbac`** (Phase 3 "Multi-team workspace controls and full RBAC hierarchy") — introduces `role_grants` as the authz source of truth and deprecates flat `app_user.role`. When present, F33's group→role provisioning writes **workspace-scope `role_grants`** via F30's grant API instead of `app_user.role`, and SCIM `Group` → team mapping / per-team roles become available; without F30, F33 maps groups to the flat workspace role and degrades gracefully.
- **`v1/F16-slack-notifications` / email** — consume the `sso.user_provisioned` / `sso.user_deprovisioned` audit/activity events for admin alerts.
- **`v1/F01-project-board`** — `propagate_deprovision` pushes a `sso.user_deprovisioned` activity event onto the timeline and requests cancellation of the user's in-flight `agent_runs` via the `v1/F07-feature-workflow-fsm` `cancel` transition / `v1/F06-single-execution-agent` `agent_run_status.cancelled` contract (F33 only emits the request; `v1/F10-run-trace-viewer` is the read-only trace viewer, not a cancellation surface).

---

## 6. Acceptance criteria (numbered, testable)

1. **SP metadata.** `GET /auth/saml/{slug}/metadata` returns valid SP metadata XML containing the SP `entityID` (= `{FORGE_PUBLIC_URL}/auth/saml/{slug}/metadata`), the ACS `Location` (= `…/acs`, binding `HTTP-POST`), the SLO `Location`, and the SP signing `X509Certificate`; the XML validates against the SAML metadata XSD and contains **no** private key.
2. **Signed AuthnRequest.** `GET /auth/saml/{slug}/login?next=/board` 302-redirects to the IdP `sso_url` with a deflated, base64 `SAMLRequest` query param and a `RelayState` carrying `/board`; when `sign_authn_requests=true` the request carries a valid `Signature` verifiable by the SP cert; the request id is stored in the replay guard with TTL `SAML_AUTHNREQUEST_TTL_SECONDS`.
3. **ACS happy path + JIT.** POSTing a correctly-signed `SAMLResponse` (NameID `dana@acme.com`, fixture-signed by the IdP test key) to `…/acs` with a matching `InResponseTo` creates a `User` (role = `default_role`) + `external_identity(provider=saml, external_id="dana@acme.com")` in Acme's workspace, establishes a session, and 302s to the RelayState target. A second login for the same NameID **links** (no duplicate user; `last_login_at` updated).
4. **Signature is mandatory.** A `SAMLResponse` with no signature, a signature by a non-configured key, or a body with one tampered byte is rejected with `SamlValidationError` → ACS returns `400` and creates **no** user/session.
5. **Conditions window.** A response whose assertion `NotOnOrAfter` is in the past (beyond `SAML_CLOCK_SKEW_SECONDS`) or `NotBefore` in the future is rejected; a response within skew is accepted (driven by an injected `now`).
6. **Audience restriction.** A response whose `AudienceRestriction` is not the SP `entity_id` is rejected.
7. **Replay rejection.** Replaying a previously-accepted assertion (same assertion ID) is rejected even if otherwise valid (`seen_assertion` returns true); replaying with an unknown/already-consumed `InResponseTo` for an SP-initiated flow is rejected.
8. **IdP-initiated gating.** An unsolicited response (no `InResponseTo`) succeeds when `allow_idp_initiated=true` and is rejected when `false`.
9. **Attribute → role mapping.** With `group_role_map={"forge-admins":"admin"}`, an assertion carrying group `forge-admins` provisions/updates the user to role `admin`; an assertion carrying only unmapped groups yields `default_role`; an IdP attribute literally claiming `role=admin` without a mapping does **not** grant admin.
10. **HRD discovery.** `POST /auth/saml/discover {"email":"dana@acme.com"}` returns the Acme login redirect; an email whose domain has no `sso_domain` row returns `{"sso": false}`; a domain already bound to another workspace's config cannot be added (`PUT …/sso` → 409 `domain_conflict`).
11. **XXE hardening.** A metadata/response XML containing a DOCTYPE/external-entity declaration is parsed without resolving the entity (no file read, no network fetch) and is rejected or stripped — proven by a fixture that would exfiltrate `/etc/passwd` if entities resolved.
12. **SCIM auth.** Every `/scim/v2/*` request without a valid, non-revoked, non-expired bearer token returns `401` with a `ScimError` body; a valid token resolves the correct `workspace_id`; token comparison is constant-time; `last_used_at` is updated.
13. **SCIM create/read/list/filter.** `POST /scim/v2/Users` with `userName=eve@acme.com` returns `201` + `ScimUser` (server `id`, `meta.location`) and creates a `User` + `external_identity(provider=scim)`; `GET /Users/{id}` returns it; `GET /Users?filter=userName eq "eve@acme.com"` returns a `ListResponse` with `totalResults=1`; `startIndex`/`count` paginate deterministically.
14. **SCIM uniqueness.** Creating a second user with an existing `userName` in the same workspace returns `409` with `scimType="uniqueness"`.
15. **SCIM PATCH deactivate = deprovision.** `PATCH /Users/{id}` with `[{op:"replace", path:"active", value:false}]` (and equivalently `DELETE /Users/{id}` → 204) sets `is_active=false` + `deactivated_at`, revokes the user's sessions and agent tokens, and refuses any subsequent SAML login for that user (ACS returns `400`/`403` until re-activated).
16. **SCIM group → role.** Creating a `Group` mapped (via `group_role_map`) to `viewer` and `PATCH`-adding a user as a member sets that user's effective role to `viewer`; removing membership reverts to `default_role`; effective role is the highest-privilege across memberships.
17. **Break-glass protection.** Disabling SSO or deprovisioning the last active local (non-SSO) `admin` is rejected with `409 last_admin`; the workspace always retains at least one admin who can sign in without the IdP.
18. **Secret redaction.** Using `cross-cutting/F37-auth-secrets-byok`'s canonical `SecretRedactor` (registered with the SP private key + the active SCIM token values), SP private key, SCIM raw tokens, and IdP-asserted PII never appear in any audit-log entry, structured log line, or API response (asserted by serializing `SsoConfigOut`, dumping logs across a full SAML login + SCIM create + deprovision flow, and grepping for the known secrets).
19. **Audit completeness.** Each of: config create/update/disable, SCIM token issue/revoke, SAML login (success+failure with reason code), JIT provision, SCIM create/update/deactivate produces exactly one immutable `AuditEvent` written through `cross-cutting/F39-audit-log`'s `SqlAuditWriter` (dotted actions `sso.config_updated`, `sso.config_disabled`, `scim.token_issued`, `scim.token_revoked`, `sso.login`, `sso.login_failed`, `sso.user_provisioned`, `scim.user_created`, `scim.user_updated`, `sso.user_deprovisioned`) with actor, action, workspace, result, and `SecretRedactor`-redacted detail; security-critical events are emitted `severity=critical` on the caller's session (fail-closed — a failed audit write rolls the originating action back).
20. **Metadata refresh + cert rollover.** `refresh_saml_metadata` fetches the metadata fixture, appends a newly-rotated cert to `idp_x509_certs` (keeping the prior cert), and a response signed by **either** cert validates during the overlap; `last_metadata_refresh_at` is set.
21. **Offline guarantee.** The entire suite passes with no live IdP/network: SAML responses and IdP metadata are signed/served from fixtures via an injected transport, the replay guard uses an in-memory fake, and CI asserts no sockets open during `apps/api` SSO tests.
22. **OAuth non-regression.** With SSO enabled for a workspace, V1 OAuth/password login for break-glass admins still works and is unaffected.

> §3 completeness: all sub-sections 3.1–3.5 are present and non-trivial; none are N/A.

---

## 7. Test plan (TDD) — concrete test cases (unit + integration), key fixtures

Write tests first; implement to green. Test roots: `apps/api/tests/sso/` (unit, FastAPI-free where possible), `apps/api/tests/integration/` (ASGI + Postgres test container), `apps/worker/tests/`.

**Unit — `saml.py` / `SamlValidator`**
- `test_build_authn_request_signed` — assert `SAMLRequest` present + deflated + RelayState; signature verifies with SP cert; request id returned + registered.
- `test_validate_response_happy` — fixture signed by IdP test key; assert `SamlAssertion` fields (NameID, attributes, session_index, not_on_or_after).
- `test_validate_response_unsigned_rejected`, `test_validate_response_wrong_key_rejected`, `test_validate_response_tampered_rejected` (flip one byte).
- `test_validate_conditions_expired` / `_notbefore_future` / `_within_skew` — injected `now`.
- `test_validate_audience_mismatch_rejected`.
- `test_validate_in_response_to_required_for_sp_initiated` / `_idp_initiated_allowed_when_flag` / `_idp_initiated_rejected_when_off`.
- `test_xsw_signature_wrapping_rejected` — fixture with a second injected unsigned assertion (XML Signature Wrapping); must reject.

**Unit — `saml_metadata.py`**
- `test_parse_idp_metadata` — extract entity_id/sso_url/slo_url/certs.
- `test_render_sp_metadata_has_acs_slo_cert_no_private_key`.
- `test_metadata_xxe_blocked` — DOCTYPE + SYSTEM entity referencing a local file → no read, parse rejected (AC11).

**Unit — `attribute_mapping.py`**
- `test_role_from_group_map_highest_privilege`, `test_role_defaults_when_no_mapping`, `test_no_implicit_admin_from_attribute` (AC9).
- `test_email_from_nameid_when_unmapped`, `test_name_from_first_last`.

**Unit — `scim_filter.py`**
- `test_filter_eq/ne/co/sw/ew/pr` → expected SQLAlchemy predicate (compile to SQL string and compare).
- `test_filter_and_or_precedence`, `test_invalid_filter_raises_invalidFilter`.

**Unit — `scim_service.py` / `provisioning.py`** (in-memory or SQLite session)
- `test_create_user_maps_fields`, `test_create_user_uniqueness_conflict` (AC14).
- `test_patch_active_false_deprovisions` (spies on session-revoke + token-expire) (AC15).
- `test_replace_user_updates`, `test_list_pagination_stable`.
- `test_group_membership_sets_effective_role` / `_removal_reverts` (AC16).
- `test_link_or_jit_provision_links_existing_by_email`, `_jit_creates_with_role`, `_jit_disabled_raises`.
- `test_deprovision_user_revokes_sessions_and_tokens_and_audits`.
- `test_break_glass_last_admin_protected` (AC17).

**Integration — `apps/api` (ASGI + Postgres)**
- `test_sp_metadata_endpoint` (AC1), `test_login_redirect_signed` (AC2).
- `test_acs_happy_jit_and_link` (AC3), `test_acs_unsigned_400` (AC4), `test_acs_expired_400` (AC5), `test_acs_audience_mismatch_400` (AC6), `test_acs_replay_rejected` (AC7), `test_acs_idp_initiated_flag` (AC8).
- `test_discover_routes_and_unknown_domain` + `test_put_config_domain_conflict_409` (AC10).
- `test_scim_requires_token_401` + `test_scim_token_resolves_workspace` (AC12).
- `test_scim_user_crud_and_filter` (AC13), `test_scim_uniqueness_409` (AC14), `test_scim_deactivate_revokes_and_blocks_login` (AC15).
- `test_scim_group_role_mapping` (AC16).
- `test_disable_sso_blocked_when_last_admin` (AC17).
- `test_secret_redaction_full_flow` (AC18), `test_audit_entries_emitted` (AC19).
- `test_oauth_still_works_with_sso_enabled` (AC22).
- `test_rbac_member_cannot_configure_sso` — non-admin → 403 on `/workspaces/{ws}/sso`.

**Integration — `apps/worker`**
- `test_refresh_saml_metadata_appends_cert` (AC20), `test_cleanup_saml_replay_evicts_expired`, `test_propagate_deprovision_emits_event_and_requests_cancellation`.

**Key fixtures (`apps/api/tests/sso/fixtures/`)**
- `idp_test_keypair` — ephemeral RSA-2048 keypair + self-signed X509 (cryptography) acting as the **IdP signing key**; never a real key.
- `sp_test_keypair` — ephemeral SP signing keypair.
- `sign_saml_response(template, *, key, now, in_response_to, audience, name_id, attributes)` helper — builds and `xmlsec`-signs a `SAMLResponse` so tests control every validated field (signature, conditions, audience, InResponseTo, assertion id).
- `saml/metadata/okta_metadata.xml`, `saml/metadata/rotated_cert_metadata.xml`, `saml/responses/*.xml` (happy, unsigned, wrong_key, tampered, expired, notbefore_future, audience_mismatch, idp_initiated, xsw, xxe).
- `scim/requests/*.json` — `create_user.json`, `patch_active_false.json`, `replace_user.json`, `create_group.json`, `patch_add_member.json`; `scim/responses/*.json` golden outputs.
- `FakeReplayGuard` (in-memory dict), `FixtureClock` (injectable `now()`), `FakeSessionStore` (records `revoke_all_for_user` calls), `FixtureMetadataTransport` (serves metadata fixtures to the worker refresh task).
- `scim_token` factory — issues a token + stores its hash; returns raw for the test client.

---

## 8. Security & policy considerations

- **XML signature validation is mandatory and toolkit-delegated.** Use `python3-saml` (OneLogin) for canonicalization + signature verification — do **not** hand-roll XML signing. Configure `wantAssertionsSigned=true`, `wantMessagesSigned` per config, and reject responses that fail signature or whose signing cert is not in `idp_x509_certs`. Defends against forged/tampered assertions (AC4).
- **XML Signature Wrapping (XSW).** Validate the signature references the exact asserted element and reject documents with multiple/extra assertions or relocated signed nodes (AC `test_xsw_*`). This is the canonical SAML SP vulnerability class.
- **XXE / SSRF.** All XML parsed with a hardened lxml parser (`resolve_entities=False`, `no_network=True`, `load_dtd=False`, `forbid_dtd` where available). Metadata-URL fetch (worker) must block private/link-local IP ranges and non-HTTPS schemes (SSRF guard) and cap response size.
- **Replay + freshness.** Enforce `NotBefore`/`NotOnOrAfter` with bounded `SAML_CLOCK_SKEW_SECONDS`, `AudienceRestriction` = SP entity id, one-time `InResponseTo` consumption for SP-initiated, and one-time assertion-id caching (AC5–AC7). IdP-initiated only when explicitly enabled (AC8).
- **No privilege self-escalation via IdP.** A SAML/SCIM-asserted role is honored only through the admin-configured `group_role_map`; arbitrary `role`/`admin` attributes are ignored (AC9). JIT provisioning is bounded to the single workspace owning the IdP config — a NameID can never provision into another tenant.
- **Confused-deputy / tenant isolation.** Workspace is resolved from the URL slug (SAML) or the SCIM token (SCIM), never from attacker-controlled assertion/body fields; every query is workspace-scoped; cross-workspace resource access returns 404 (not 403) to avoid existence leaks.
- **SCIM token security.** Tokens are CSPRNG (`secrets.token_urlsafe(SCIM_TOKEN_BYTES)`), stored only as SHA-256 hashes, compared constant-time (`hmac.compare_digest`), support expiry + revocation, are shown once, and are rate-limited per workspace. SCIM endpoints require HTTPS in production.
- **Secrets at rest + redaction.** SP private key encrypted via `cross-cutting/F37-auth-secrets-byok`'s AES-256-GCM per-workspace vault; never serialized in `SsoConfigOut`, logs, traces, or audit. F33 registers `private_key`, `Authorization`, raw SCIM token values, and assertion PII with F37's **canonical `SecretRedactor`** (it does **not** implement its own) so they are stripped from all structured logs/audit (AC18). Raw assertion attributes are processed in memory and never persisted.
- **Break-glass / lockout prevention.** Disabling SSO or deprovisioning the last local admin is blocked (AC17); admins are warned in the UI; a documented CLI break-glass (`forge-cli users create-admin`) remains available out-of-band.
- **Deprovision immediacy.** SCIM `active=false`/DELETE revokes sessions and agent tokens synchronously; stateless-session deployments consult the `session_revocation` deny-list so a removed user cannot continue with a still-valid JWT.
- **RBAC.** Only `admin` may configure SSO or manage SCIM tokens; `member`/`viewer`/`agent-runner` get 403. The SAML/discovery endpoints are intentionally unauthenticated (they *establish* identity) but are signature/replay-gated.
- **Audit immutability.** Every config change, token lifecycle event, login (success + failure with reason), and provisioning action appends to the immutable audit log via `cross-cutting/F39-audit-log`'s append-only `SqlAuditWriter` (per-workspace hash chain + DB-level immutability trigger) (AC19, non-negotiable #9).

---

## 9. Effort estimate & risk (S/M/L + key risks)

**Effort: L** — two security-critical protocols (SAML SP + SCIM SP), each with a config surface, runtime endpoints, a worker task set, a settings UI, a native dependency (`xmlsec1`), five new tables + migration, and an extensive signed-fixture test corpus.

Key risks:
- **SAML correctness/security** (signature validation, XSW, XXE, replay, clock skew) — highest risk; mitigated by delegating crypto to `python3-saml`, an adversarial fixture corpus (unsigned/wrong-key/tampered/XSW/XXE/expired/audience), and injected clock + replay guard.
- **`xmlsec1` native dependency** in Docker (build + runtime libs, version skew) — mitigated by pinning `libxmlsec1`/`xmlsec1` in both Dockerfile stages and a smoke test that signs+verifies on container start; documented in self-hosting security.md.
- **IdP interop variance** (Okta vs Entra vs Google attribute names, NameID formats, IdP-initiated quirks) — mitigated by a configurable `AttributeMapping`, metadata-driven config, the **Test connection** tool, and per-IdP fixtures; live cross-IdP certification is a post-merge task (out of overnight scope, like F03's live-verify).
- **SCIM conformance** (PATCH semantics, filter grammar, error `scimType` codes, pagination) — mitigated by golden request/response fixtures captured from Okta/Entra SCIM specs and RFC 7644 §3.5.2 PATCH cases.
- **Lockout** from a misconfigured IdP — mitigated by break-glass admin enforcement (AC17) + CLI admin creation.
- **Session-revocation coupling** to the V1 auth design (stateful vs JWT) — mitigated by specifying the `session_revocation` deny-list fallback so F33 works regardless of the V1 session strategy.

---

## 10. Key files / paths (exact)

```
packages/contracts/forge_contracts/sso.py                         # DTOs + SamlValidator/ReplayGuard/Scim*Service Protocols (new)
packages/db/forge_db/models/sso.py                                # SsoConfiguration, SsoDomain, ExternalIdentity, ScimToken, ScimGroup, ScimGroupMember (new)
packages/db/forge_db/models/workspace.py                          # extend app_user: deactivated_at, external_managed
packages/db/forge_db/models/enums.py                              # SsoProtocol, ExternalIdentityProvider, ScimResourceType
packages/db/forge_db/models/__init__.py                           # register new models
packages/db/migrations/versions/00NN_enterprise_sso.py            # migration (rev id `enterprise_sso`; chains after F37 0002_auth_secrets) (+ optional session_revocation, saml_replay)

apps/api/forge_api/sso/__init__.py
apps/api/forge_api/sso/saml.py                                    # SamlSpService (SamlValidator impl)
apps/api/forge_api/sso/saml_metadata.py                           # XXE-hardened parse/render
apps/api/forge_api/sso/scim_models.py                             # re-export contracts SCIM resources
apps/api/forge_api/sso/scim_service.py                            # Scim{User,Group}Service impls
apps/api/forge_api/sso/scim_filter.py                             # RFC 7644 filter -> SQLAlchemy
apps/api/forge_api/sso/provisioning.py                            # link_or_jit_provision, deprovision_user
apps/api/forge_api/sso/attribute_mapping.py                       # attrs/groups -> MappedIdentity
apps/api/forge_api/sso/replay.py                                  # RedisReplayGuard + Postgres fallback
apps/api/forge_api/sso/errors.py
apps/api/forge_api/routers/sso_admin.py                           # /workspaces/{ws}/sso, /scim tokens
apps/api/forge_api/routers/saml.py                               # /auth/saml/{slug}/{metadata,login,acs,slo}, /auth/saml/discover
apps/api/forge_api/routers/scim.py                               # /scim/v2/*
apps/api/forge_api/deps.py                                       # add require_scim_token dependency (extend)
apps/api/forge_api/settings.py                                   # SAML_CLOCK_SKEW_SECONDS, SCIM_TOKEN_BYTES, SAML_AUTHNREQUEST_TTL_SECONDS
apps/api/tests/sso/...                                           # unit tests + fixtures (see §7)
apps/api/tests/integration/test_saml_flow.py
apps/api/tests/integration/test_scim_api.py

apps/worker/forge_worker/tasks/sso.py                            # refresh_saml_metadata, refresh_all_saml_metadata, cleanup_saml_replay, propagate_deprovision
apps/worker/forge_worker/beat.py                                 # register periodic SSO tasks (extend)
apps/worker/tests/test_sso_tasks.py

apps/web/app/(auth)/login/page.tsx                               # HRD email + SSO button (extend)
apps/web/app/(settings)/security/sso/page.tsx
apps/web/app/(settings)/security/scim/page.tsx
apps/web/components/sso/SamlConfigForm.tsx
apps/web/components/sso/AttributeMappingEditor.tsx
apps/web/components/sso/DomainListEditor.tsx
apps/web/components/sso/GroupRoleMapEditor.tsx
apps/web/components/sso/ScimTokenTable.tsx
apps/web/components/sso/SsoStatusBadge.tsx

apps/api/pyproject.toml                                          # add python3-saml, lxml, xmlsec deps
apps/worker/pyproject.toml                                       # add python3-saml/lxml (metadata refresh)
deploy/docker-compose.yml                                        # xmlsec1 build/runtime libs note, SSO env
deploy/caddy/Caddyfile                                           # raw-body passthrough for /auth/saml/*/acs and /scim/v2/*
docs/self-hosting/security.md                                    # SSO/SCIM hardening + xmlsec1 + break-glass
.env.example                                                     # SAML_CLOCK_SKEW_SECONDS, SCIM_TOKEN_BYTES, SAML_AUTHNREQUEST_TTL_SECONDS
```

---

## 11. Research references (relevant links from the spec/research report)

- **Auth & Secrets Service / OAuth providers** Forge extends: FORGE_SPEC.md "Product Scope" (Auth & Secrets Service row), "Technology Stack" (Auth → Better Auth / Auth.js; Secrets → Encrypted Postgres vault V1), "Integrations → V1 → OAuth (Google, GitHub, GitLab)". F33 adds SAML+SCIM beside this.
- **Phase 3 placement**: FORGE_SPEC.md "Phased Roadmap → Phase 3 — Scale (V3) → Enterprise SSO (SAML, SCIM)" and "Multi-team workspace controls and full RBAC hierarchy" (the soft dependency for group→role mapping).
- **Security requirements** F33 must satisfy: FORGE_SPEC.md "Security" table — Secrets (encrypted at rest, per-workspace isolation, automatic token expiry), RBAC (admin/member/viewer/agent-runner), Audit log (every action immutable + queryable), Secret redaction, Auth (all routes authenticated; no anonymous API), Rate limiting (per-workspace/user).
- **Token binding / least-privilege precedent**: FORGE_SPEC.md "MCP Security Rules" (RFC 8707 resource binding, read-only defaults) — applied here by analogy to SCIM token scoping and SAML audience restriction.
- **Better Auth** (the V1 auth library SSO sits beside): https://www.better-auth.com/ (FORGE_SPEC.md Research Links → Supporting Tools).
- **Pydantic v2 / SQLAlchemy 2.x / Alembic** (model + migration + DTO stack): https://docs.pydantic.dev/latest/ · https://docs.sqlalchemy.org/en/20/ · https://alembic.sqlalchemy.org/en/latest/ (FORGE_SPEC.md Research Links).
- **Caddy** (HTTPS for ACS/SCIM, raw-body passthrough): https://caddyserver.com/ (FORGE_SPEC.md Research Links).
- **External protocol specs (not in the Forge spec; required to implement against the wire format)**: SAML 2.0 Core/Bindings (OASIS), `python3-saml` toolkit (https://github.com/SAML-Toolkits/python3-saml), SCIM 2.0 — RFC 7643 (Core Schema) and RFC 7644 (Protocol). These are the authoritative references for assertion/condition validation, the SCIM resource schema, filter grammar, and PATCH semantics encoded in §4.

---

## 12. Out of scope / future

- **OpenID Connect (OIDC) enterprise SSO** as a distinct protocol path — V1 social OAuth covers Google/GitHub/GitLab; a generic OIDC enterprise connector is a follow-up (the `external_identity` model already accommodates it).
- **WS-Federation / LDAP / Active Directory direct bind** — not planned; enterprises integrate via SAML/SCIM through their IdP.
- **SCIM Bulk operations, sorting, ETags/versioning, `/Me`, enterprise user extension schema** — `ServiceProviderConfig` advertises these as unsupported in V3; add on demand.
- **SAML Single Logout fan-out to all SP sessions across devices** — V3 handles SLO for the initiating session + best-effort revocation; global front-channel logout choreography is future.
- **Multi-IdP per workspace** (e.g. one workspace federating several directories) — V3 is one SAML config per workspace; the model can relax the `UNIQUE(workspace_id)` later.
- **Automated domain-ownership verification** (DNS TXT / email challenge) — `sso_domain.verified` exists; the verification workflow itself is a follow-up (V3 ships admin-asserted with the global-uniqueness guard).
- **Team-scoped SCIM group → team mapping** — depends on `v3/F30-multi-team-rbac`; V3 maps groups to workspace roles only.
- **SAML attribute-based fine-grained authorization** beyond role mapping (e.g. project-level entitlements from assertions) — future, gated on the advanced policy engine (V3 F30).
- **Live IdP interop certification** (Okta/Entra/Google end-to-end) — post-merge verification task; this slice is fixture-driven and offline like F03.
