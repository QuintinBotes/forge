# F32 — Integration Marketplace (community MCP connectors & skill profiles)

> Phase: v3 · Spec module(s): OSS Strategy → "Extension Points" (MCP gateway accepts any MCP-compatible server; "Skill profiles: plain YAML — community contributions welcome"; PMAdapter / Tool registry pluggability), Phase 3 roadmap → "Integration marketplace for community MCP connectors and skill profiles", MCP Connector Layer (`packages/mcp-sdk`, `apps/mcp-gateway`), Skill Profiles (`packages/skill-sdk`), Auth & Secrets (BYOK vault, RBAC), Observability (audit), Self-Hosting (federated/OSS catalog) · Status target: **Done** = a workspace admin can register one or more **marketplace registry sources** (the bundled official registry plus arbitrary community git/HTTP indexes), browse a cached, searchable catalog of community **MCP connector templates** and **skill profiles** (and reserved-kind workflow/policy templates), inspect a listing's versions/README/provenance, and **install a specific version into the workspace**. Install is supply-chain-safe: the artifact is fetched, its `content_hash` and detached signature are verified against the registry's trusted Ed25519 key, the embedded artifact is re-validated **fail-closed** against the authoritative F09 (`MCPConnectionConfig`) / F11 (`SkillProfile`) schemas, a security gate is applied (MCP connectors install **`allow_write=false`, `status=pending`, never auto-connected/OAuth'd**; skill profiles install as workspace-custom rows that can only act as a *floor*), and the install creates the corresponding **F09 `mcp_connections` / F11 `skill_profile`** object linked to an immutable `marketplace_installations` provenance record. No package ever executes code (artifacts are declarative YAML only); catalog sync is SSRF-bounded; every catalog sync / install / update / uninstall / verification outcome is audited. Lint + types + `pytest` green on `packages/marketplace-sdk`, the `apps/api` marketplace router, and the `apps/worker` catalog-sync tasks.

---

## 1. Intent — what & why

The spec promises Forge is extensible by design: *"MCP gateway: accepts any MCP-compatible server"*, *"Skill profiles: plain YAML — community contributions welcome"*, and the Phase 3 roadmap commits to an *"Integration marketplace for community MCP connectors and skill profiles"* (`docs/FORGE_SPEC.md` → OSS Strategy → Extension Points; Phased Roadmap → Phase 3). Today (v1/v2) the only path to share a connector or skill is to hand-edit YAML or copy from `examples/mcp-connectors/` and `examples/skills/`. F32 turns those one-off artifacts into a **discoverable, versioned, provenance-tracked distribution channel** so a team can adopt a community Confluence connector or a hardened `backend-tdd-strict` skill profile in one click instead of copy-paste.

F32 deliberately does **not** invent a new artifact format. The two first-class artifact kinds are exactly the existing, schema-frozen objects from earlier slices:

- **MCP connector** = the `mcp_connection` template (F09 `MCPConnectionConfig`, `docs/FORGE_SPEC.md` → "MCP Connection Schema"). Installing one creates an F09 `mcp_connections` row.
- **Skill profile** = the F11 `SkillProfile` YAML (`docs/FORGE_SPEC.md` → "Skill Profiles"). Installing one creates an F11 `skill_profile` workspace-custom row.

F32 is the thin-but-load-bearing layer **on top** of those: a registry/catalog model, a manifest format that wraps an artifact with distribution metadata + integrity hash, a registry client that fetches federated indexes, a signature/hash verifier, and an installer that delegates validation to the F09/F11 loaders and applies the security floor before materializing a local object.

Because Forge is self-hosting-first and Apache-2.0, the marketplace is **federated and git/HTTP-native**, not a single hosted SaaS. A registry is just a signed `index.json` published at a URL (or in a git repo) — exactly the Homebrew-tap / Helm-repo / VS Code-extension-index pattern. Forge ships a default **official registry** (`forge-platform/marketplace`) and lets admins add community ones. This keeps the OSS promise intact (anyone can host a registry, anyone can publish by opening a PR to one) while giving Forge a single chokepoint to enforce integrity, provenance, and the read-only/least-privilege defaults that the rest of the platform depends on.

The reason this is security-critical and not a CRUD feature: marketplace content is **untrusted, third-party, supply-chain input**. The slice's primary job is to make installing community content *as safe as the platform's own defaults* — declarative-only (no code execution), signed + hash-verified, schema-validated fail-closed, write-blocked, never auto-connected, fully audited.

## 2. User-facing behavior / journeys

- **Journey A — Add a registry (admin).** Settings → Marketplace → Registries → "Add registry". Admin enters a name, `type` (`git` | `http_index`), URL (e.g. `https://marketplace.forge.dev/index.json`), and optionally pastes the registry's Ed25519 public key (base64) and a trust level. On save the registry is `enabled`, a first catalog sync is enqueued, and the official registry is present out of the box (seeded, read-only, `trust_level=official`).
- **Journey B — Browse & search (member).** Marketplace home shows the merged catalog across enabled registries: cards with name, kind badge (`MCP connector` | `Skill profile`), summary, tags, author, latest version, registry/trust badge, and a verification badge (`Verified` / `Unsigned` / `Untrusted registry`). Filters: kind, tag, registry, free-text `q`. Members can browse but not install.
- **Journey C — Inspect a listing (member).** Listing detail renders the README (description), version history with published dates, license, homepage/repo links, the resolved artifact preview (the `mcp_connection` or `SkillProfile` body, with `SkillDirectives` shown for skill profiles via F11's panel), and a provenance panel: source registry, trust level, `content_hash`, signature status, and `min_forge_version` compatibility.
- **Journey D — Preview & install a skill profile (admin).** Admin clicks "Install" on `backend-tdd-strict@1.2.0`. Forge fetches the version manifest, verifies hash + signature, validates the embedded `SkillProfile` (F11 loader, fail-closed), and shows an **install preview**: verification result, resolved profile + directives, and any warnings (e.g. "name `backend-tdd` shadows a builtin — installing creates an override"). Admin confirms → an F11 `skill_profile` row is created (or, if name collides with an existing custom row, the admin is prompted to update instead), and a `marketplace_installations` record links listing→version→`skill_profile.id`. The profile immediately appears in the skill picker.
- **Journey E — Install an MCP connector (admin).** Admin installs `confluence-readonly@2.0.0`. Verification + F09 `MCPConnectionConfig` validation run. The install preview clearly states the **security follow-ups**: "Installs read-only (`allow_write=false`), `status=pending` — you must review the endpoint and click Connect (and complete OAuth / supply an API key) in MCP settings to activate." On confirm, an F09 `mcp_connections` row is created `status=pending, allow_write=false`, with **no credentials and no live MCP call**. The connector then flows through the normal F09 test/connect lifecycle; nothing is auto-enabled.
- **Journey F — Update an installed package (admin).** Hourly catalog sync detects `backend-tdd-strict@1.3.0`. The installed item shows an "Update available" badge. Admin opens it, sees a diff of the resolved artifact (old vs new directives/config) plus the new version's verification result, and confirms. The local object body is updated (skill profile body replaced; for an MCP connector, scope/endpoint changes are shown but the connection is set back to `status=pending` and must be re-confirmed/re-connected — never silently re-pointed while connected).
- **Journey G — Uninstall (admin).** Admin uninstalls. The linked local object is deleted (skill profile custom row removed → reverts to builtin if it was an override; MCP connection deleted, with F09 audit rows retained via slug snapshot). The `marketplace_installations` row is marked `uninstalled` (kept for audit).
- **Journey H — Yanked / unverified content is visible.** If a registry index marks a version `yanked` (security issue), catalog sync flags it; workspaces that installed it see a red "Yanked: <reason>" banner with a recommended action. A registry with a signature present but no trusted key, or an unsigned package, is badged `Untrusted`/`Unsigned`, and install requires an explicit "I understand this is unverified" admin acknowledgement.
- **Journey I — Publish a package (author, CLI).** A maintainer runs `forge marketplace package --kind skill_profile examples/skills/backend-tdd.yaml --out dist/` to produce a canonical `forge-package.yaml` + computed `content_hash`, then signs it and opens a PR to a registry's git repo. In-app publishing UI is out of scope (§12); the CLI + git/PR flow is the OSS publishing path.

## 3. Vertical slice

### 3.1 Data model (tables/columns/migrations touched)

One Alembic migration `apps/api/alembic/versions/0032_integration_marketplace.py` (depends on the F09 `0009_mcp_gateway` migration and the F11 `skill_profile` migration; the `0032` ordinal is illustrative — set `down_revision` to the actual Alembic head at integration time, mirroring F09's note). No new Postgres extensions. Five tables, all strictly `workspace_id`-scoped (no global/shared rows). The official registry is **not** a cross-tenant shared row: each workspace gets its own enabled copy, seeded on workspace creation and backfilled for pre-existing workspaces on startup (see §3.5 / AC2).

**Table `marketplace_registries`** (a trusted registry source)

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid` PK | |
| `workspace_id` | `uuid` NOT NULL FK → `workspaces.id` ON DELETE CASCADE | tenant key |
| `slug` | `text` NOT NULL | stable handle (e.g. `official`, `acme-internal`) |
| `name` | `text` NOT NULL | display name |
| `type` | `text` NOT NULL | enum: `git \| http_index` |
| `url` | `text` NOT NULL | index URL (http_index) or git remote (git) |
| `ref` | `text` NULL | git branch/tag (git type); default `main` |
| `public_key` | `text` NULL | Ed25519 verify key (base64); null ⇒ packages from here are `unsigned`/`untrusted` |
| `trust_level` | `text` NOT NULL DEFAULT `'community'` | enum: `official \| trusted \| community \| unverified` |
| `enabled` | `boolean` NOT NULL DEFAULT `true` | |
| `etag` | `text` NULL | HTTP caching for index fetch |
| `last_sync_at` | `timestamptz` NULL | |
| `last_sync_status` | `text` NULL | `ok \| error` |
| `last_sync_error` | `text` NULL | redacted |
| `created_at` / `updated_at` | `timestamptz` NOT NULL | |

Constraints/indexes: `UNIQUE (workspace_id, slug)`; `CHECK (type IN ('git','http_index'))`; `CHECK (trust_level IN ('official','trusted','community','unverified'))`.

**Table `marketplace_listings`** (cached catalog entry; one per package per registry)

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid` PK | |
| `workspace_id` | `uuid` NOT NULL FK → `workspaces.id` ON DELETE CASCADE | |
| `registry_id` | `uuid` NOT NULL FK → `marketplace_registries.id` ON DELETE CASCADE | |
| `kind` | `text` NOT NULL | enum: `mcp_connector \| skill_profile \| workflow_template \| policy_template` |
| `slug` | `text` NOT NULL | package slug, kebab-case |
| `name` | `text` NOT NULL | |
| `summary` | `text` NOT NULL | |
| `tags` | `jsonb` NOT NULL DEFAULT `'[]'` | |
| `latest_version` | `text` NOT NULL | semver |
| `homepage` | `text` NULL | |
| `repository` | `text` NULL | |
| `license` | `text` NOT NULL DEFAULT `'Apache-2.0'` | |
| `cached_at` | `timestamptz` NOT NULL | from last catalog sync |
| `created_at` / `updated_at` | `timestamptz` NOT NULL | |

Constraints/indexes: `UNIQUE (registry_id, kind, slug)`; btree `(workspace_id, kind)`; GIN on `tags`; a Postgres full-text index on `to_tsvector('english', name || ' ' || summary)` for catalog search.

**Table `marketplace_listing_versions`** (per-version metadata cached from the index)

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid` PK | |
| `listing_id` | `uuid` NOT NULL FK → `marketplace_listings.id` ON DELETE CASCADE | |
| `version` | `text` NOT NULL | semver |
| `content_hash` | `text` NOT NULL | `sha256:<hex>` over the canonical embedded artifact |
| `manifest_hash` | `text` NOT NULL | `sha256:<hex>` over canonical manifest JSON (the signed payload) |
| `signature` | `text` NULL | base64 detached Ed25519 sig over `manifest_hash` bytes |
| `manifest_uri` | `text` NOT NULL | resolvable URI to the `forge-package.yaml` |
| `min_forge_version` | `text` NULL | semver compat gate |
| `published_at` | `timestamptz` NOT NULL | |
| `yanked` | `boolean` NOT NULL DEFAULT `false` | |
| `yanked_reason` | `text` NULL | |

Constraints/indexes: `UNIQUE (listing_id, version)`; btree `(listing_id, published_at DESC)`.

**Table `marketplace_installations`** (what is installed into the workspace + provenance)

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid` PK | |
| `workspace_id` | `uuid` NOT NULL FK → `workspaces.id` ON DELETE CASCADE | |
| `registry_slug` | `text` NOT NULL | snapshot (survives registry deletion) |
| `listing_slug` | `text` NOT NULL | snapshot |
| `kind` | `text` NOT NULL | artifact kind |
| `installed_version` | `text` NOT NULL | |
| `pinned` | `boolean` NOT NULL DEFAULT `false` | if true, update flags suppressed |
| `target_kind` | `text` NOT NULL | `mcp_connection \| skill_profile` (the created object type) |
| `target_object_id` | `uuid` NULL | FK-by-convention to `mcp_connections.id` / `skill_profile.id`; NULL after uninstall |
| `content_hash` | `text` NOT NULL | the hash that was verified at install |
| `verification_status` | `text` NOT NULL | enum `VerificationStatus` value recorded at install |
| `status` | `text` NOT NULL DEFAULT `'pending'` | enum: `pending \| installed \| update_available \| failed \| uninstalled` |
| `available_version` | `text` NULL | set by update-flag refresh when newer non-yanked version exists |
| `installed_by` | `uuid` NULL FK → `users.id` | |
| `installed_at` / `updated_at` | `timestamptz` NOT NULL | |

Constraints/indexes: `UNIQUE (workspace_id, registry_slug, listing_slug)` (one active install per package per workspace; re-install of a new version updates the row); btree `(workspace_id, status)`.

**Table `marketplace_audit_log`** (immutable, append-only domain audit — same per-domain pattern as F09 `mcp_audit_log` / F29 `policy_rule_evaluation` / F31 `deployment_transition`; in addition, every row is emitted as a compact `AuditEvent` through the canonical `AuditSink` from `cross-cutting/F39-audit-log`, so marketplace activity also lands in the central platform `audit_log` + Observability stream)

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid` PK | |
| `workspace_id` | `uuid` NOT NULL FK → `workspaces.id` ON DELETE CASCADE | |
| `actor` | `text` NOT NULL | `user:<uuid>` \| `system` |
| `operation` | `text` NOT NULL | enum: `registry.add \| registry.sync \| registry.remove \| install \| update \| uninstall \| verify` |
| `registry_slug` | `text` NULL | |
| `listing_slug` | `text` NULL | |
| `version` | `text` NULL | |
| `content_hash` | `text` NULL | |
| `verification_status` | `text` NULL | |
| `result_status` | `text` NOT NULL | `ok \| denied \| error` |
| `error_code` | `text` NULL | non-exhaustive: `hash_mismatch`, `signature_invalid`, `signature_required`, `schema_invalid`, `name_collision`, `forge_version_incompatible`, `ssrf_blocked` |
| `detail` | `text` NULL | redacted |
| `created_at` | `timestamptz` NOT NULL DEFAULT `now()` | |

Immutability: append-only. The migration calls F39's reusable `attach_immutability_trigger('marketplace_audit_log')` helper (`cross-cutting/F39-audit-log`) — the same helper F09/F29/F31 domain audit tables opt into — which installs a trigger that `RAISE`s on `UPDATE`/`DELETE` (delete only via the workspace `ON DELETE CASCADE` at teardown). Indexes: `(workspace_id, operation, created_at DESC)`. Verified by AC18.

`alembic downgrade` cleanly drops all five tables + trigger (AC1).

### 3.2 Backend (FastAPI routes + services/packages)

Two layers: the reusable **`packages/marketplace-sdk`** (catalog/manifest/registry-client/verifier/installer core; no FastAPI imports) and the **`apps/api`** router + service that own the DB records and orchestrate installs by delegating to F09/F11 services.

**`packages/marketplace-sdk/forge_marketplace/`**

```
packages/marketplace-sdk/forge_marketplace/
├── models.py            # Pydantic v2 models + enums (§4)
├── manifest.py          # load/validate forge-package.yaml; canonical_artifact_bytes(); compute_content_hash()
├── index.py             # parse/validate registry index.json -> RegistryIndex
├── registry_client.py   # RegistryClient Protocol + HttpIndexRegistryClient, GitRegistryClient (SSRF-bounded fetch)
├── verifier.py          # Ed25519SignatureVerifier; verify_version() -> VerificationResult
├── installer.py         # ArtifactInstaller Protocol + planning; delegates validation to mcp-sdk / skill-sdk loaders
├── catalog.py           # merge_index_into_listings(); update-flag computation; semver compare + min_forge_version gate
├── packaging.py         # build_package() used by `forge marketplace package` (author-side)
├── package-manifest.schema.json   # generated from PackageManifest.model_json_schema() (drift guard, like F11)
└── errors.py            # HashMismatch, SignatureInvalid, UntrustedRegistry, SchemaInvalid, ForgeVersionIncompatible, RegistryFetchError, SsrfBlocked, YankedVersion
```

Design rules baked into the SDK:
- **Declarative-only.** `installer.py` only ever produces a validated `MCPConnectionConfig` / `SkillProfile` and hands it to F09/F11 services. It never imports/executes anything from a package. There is no "plugin code" path in V3 (§12).
- **Fail-closed validation.** The embedded `artifact` dict is validated by the kind's installer using the *authoritative* loader: `mcp_sdk.models.MCPConnectionConfig(**artifact)` for `mcp_connector`, `forge_skills.loader.load_skill_profile(yaml)` for `skill_profile`. Any schema violation → `SchemaInvalid`, install blocked.
- **Security floor at plan time.** For `mcp_connector`: force `allow_write=false`, reject `transport=stdio` (matches F09 V1 limits), warn if `allowed_namespaces` is empty (over-broad), and mark `requires_admin_followup=["review endpoint", "connect / supply credentials"]`. For `skill_profile`: rely on F11's fail-closed schema; warn (not block) if `name` shadows a builtin (becomes an override).
- **Verification precedence** (`verifier.verify_version`): compute sha256 of the fetched artifact and compare to `content_hash` → on mismatch return `VerificationStatus.hash_mismatch` (hard block). Then, if a signature + registry public key exist, verify the detached Ed25519 sig over `manifest_hash` → `verified` / `signature_invalid` (block). If a signature exists but the registry has no `public_key` → `untrusted_registry` (soft: install allowed only with explicit `acknowledge_unverified=true`). If no signature → `unsigned` (same soft gate). `official`/`trusted` registries with a key always require `verified`.

**`apps/api` — router `apps/api/app/api/v1/marketplace.py`, mounted at `/api/v1/marketplace`.** All routes resolve an authenticated `Principal` + workspace membership via `get_principal` (`cross-cutting/F37-auth-secrets-byok`); mutating routes require `admin` via that slice's `require_role("admin")` dependency. Controllers delegate to `apps/api/app/services/marketplace_service.py`.

> Cross-slice note: the v1 foundation diverges on the api package root — F09 (the MCP install target) and this slice use `apps/api/app/...`, while F11 (the skill install target) and F29 use `apps/api/forge_api/...`. Use whichever root the foundation standardized on, and keep the delegation calls (`mcp_service.create_connection`, `skill_service.create/update/delete`) pointed at each dependency's actual module regardless of the chosen root.

| Method | Path | RBAC | Purpose |
|---|---|---|---|
| `GET` | `/registries` | member | List registries + last sync status. |
| `POST` | `/registries` | admin | Add a registry (`RegistrySourceIn`); enqueues first sync. |
| `PATCH` | `/registries/{id}` | admin | Enable/disable, update key/trust/name. |
| `DELETE` | `/registries/{id}` | admin | Remove a registry (cascades cached listings; installations keep their slug snapshot). |
| `POST` | `/registries/{id}/sync` | admin | Force catalog sync now (proxies to worker task; eager-able). |
| `GET` | `/listings` | member | Browse catalog: filters `kind`, `tag`, `registry_id`, `q`, pagination. |
| `GET` | `/listings/{registry_slug}/{slug}` | member | Listing detail + version list + resolved artifact preview. |
| `POST` | `/preview` | admin | `InstallRequest` → `InstallPlan` (fetch+verify+validate, **no writes**). |
| `POST` | `/install` | admin | `InstallRequest` → `InstallResult` (verify+validate+apply; creates F09/F11 object + installation row). |
| `GET` | `/installations` | member | List installs + status + `available_version`. |
| `POST` | `/installations/{id}/update` | admin | Re-install to a newer version (verify+validate+apply diff). |
| `PATCH` | `/installations/{id}` | admin | Pin/unpin. |
| `DELETE` | `/installations/{id}` | admin | Uninstall (delete linked object via F09/F11 service; mark row `uninstalled`). |
| `GET` | `/audit` | admin | Paginated `marketplace_audit_log`. |

`marketplace_service.py` responsibilities: persist registries/listings/installations; call the registry client (via worker for scheduled sync, inline for `/preview` and `/install`); run the verifier; call the installer which delegates to **`mcp_service.create_connection`** (F09) or **`skill_service.create`/`update`** (F11) to materialize the local object inside the same DB transaction; write the provenance + audit rows. The marketplace service **never** speaks MCP, stores no secrets, and never sets `allow_write=true` or triggers OAuth.

### 3.3 Worker / agent runtime (Celery tasks, LangGraph, if any)

No LangGraph, no agent involvement (the marketplace is admin tooling, not an agent capability). Celery tasks in `apps/worker/tasks/marketplace.py` (queue `marketplace`):

- `marketplace.sync_registry(registry_id) -> SyncReport` — fetch the registry index (SSRF-bounded, ETag-aware, size-capped), validate it against `RegistryIndex`, upsert `marketplace_listings` + `marketplace_listing_versions`, prune entries removed upstream, set `last_sync_*`, write one `registry.sync` audit row. Per-registry timeout; one failing registry never fails another.
- `marketplace.sync_all_registries()` — Celery beat (hourly, configurable) fan-out to `sync_registry` for every `enabled` registry across all workspaces.
- `marketplace.refresh_update_flags()` — beat (after each sync): for each non-pinned `installed` installation, compute the highest non-yanked compatible version (semver, honoring `min_forge_version`); set `status=update_available` + `available_version`, or flag a `yanked` warning if the installed version was yanked.

Catalog sync does **not** download or validate every artifact body eagerly (the index carries hashes/metadata only); the full artifact is fetched + verified lazily at `/preview` and `/install` time, keeping sync cheap and the integrity check at the trust boundary.

### 3.4 Frontend / UI (Next.js routes/components, if any)

Routes under `apps/web/app/(app)/marketplace/`:

- `page.tsx` — catalog browse: search box, kind/tag/registry filters, listing cards with kind + trust + verification badges. TanStack Query against `/listings`.
- `[registry]/[slug]/page.tsx` — listing detail: README (rendered markdown), version selector, license/homepage/repo, provenance panel (registry, trust, `content_hash`, signature status, `min_forge_version`), and the artifact preview — reusing F11's `SkillDirectivesPanel` for skill profiles and a read-only `mcp_connection` summary for connectors. "Install" / "Preview install" (admin).
- `installed/page.tsx` — installed packages with `Update available` / `Yanked` badges, Update / Pin / Uninstall actions (admin).
- `settings/registries/page.tsx` — registry CRUD, public-key paste, trust level, "Sync now", last-sync status.
- Components in `apps/web/components/marketplace/`: `ListingCard`, `VerificationBadge`, `TrustBadge`, `InstallDialog` (renders the `InstallPlan`: verification result, warnings, `requires_admin_followup`, and the unverified-acknowledgement checkbox), `UpdateDiffDialog`, `RegistryForm`.
- `apps/web/lib/api/marketplace.ts` — typed client matching §4.

The `InstallDialog` is the UX security gate: it shows the verification badge prominently, lists the security follow-ups for MCP connectors ("installs read-only & pending; connect manually"), and disables the confirm button until any required unverified acknowledgement is checked.

### 3.5 Infra / deploy (compose, helm, caddy, if any)

- **No new service.** Marketplace logic runs inside existing `apps/api` (routes) and `apps/worker` (sync) containers. The Celery beat schedule adds `marketplace.sync_all_registries` (hourly) and `marketplace.refresh_update_flags`.
- **Egress / SSRF controls.** Registry fetches make outbound HTTP(S)/git requests from the worker. Add an allowlist + guard (`MARKETPLACE_ALLOWED_REGISTRY_HOSTS`, default empty = allow public DNS but **deny RFC1918 / link-local / metadata `169.254.169.254`** via DNS-resolution check in `registry_client`), per-fetch timeout, and a response size cap. The worker's network policy (compose `marketplace-egress` / helm `NetworkPolicy`) restricts it to outbound 443/git; it cannot reach the `db`/internal networks beyond what it already needs.
- **New env** (add to `deploy/.env.example` + `.env.production.example`): `MARKETPLACE_OFFICIAL_REGISTRY_URL=https://marketplace.forge.dev/index.json`, `MARKETPLACE_OFFICIAL_REGISTRY_PUBKEY=<base64-ed25519>`, `MARKETPLACE_SYNC_INTERVAL_MINUTES=60`, `MARKETPLACE_FETCH_TIMEOUT_SECONDS=20`, `MARKETPLACE_MAX_INDEX_BYTES=5242880`, `MARKETPLACE_MAX_MANIFEST_BYTES=262144`, `MARKETPLACE_ALLOWED_REGISTRY_HOSTS=`, `MARKETPLACE_REQUIRE_SIGNATURE=false`. The official registry URL + pubkey seed a per-workspace `official` registry row on workspace creation.
- **Caddy/helm:** no new public route beyond `/api/v1/marketplace/*` already served by `api`. Helm values gain `marketplace.officialRegistry.{url,pubkey}` and the beat schedule entries.
- **Bundled fallback:** the repo's existing `examples/mcp-connectors/*.yaml` and `examples/skills/*.yaml` are mirrored into the official registry; an air-gapped install can point `MARKETPLACE_OFFICIAL_REGISTRY_URL` at a local file/git path so the marketplace works offline.

## 4. Public interfaces / contracts (exact signatures, Pydantic models, Protocols, YAML schemas)

`packages/marketplace-sdk/forge_marketplace/models.py`:

```python
from datetime import datetime
from enum import StrEnum
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, field_validator
import re

SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{1,63}$")
HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

class ArtifactKind(StrEnum):
    mcp_connector = "mcp_connector"
    skill_profile = "skill_profile"
    workflow_template = "workflow_template"   # reserved; installer only registered if F21 present
    policy_template = "policy_template"       # reserved; installer only registered if F04 present

class RegistryType(StrEnum):
    git = "git"; http_index = "http_index"

class TrustLevel(StrEnum):
    official = "official"; trusted = "trusted"; community = "community"; unverified = "unverified"

class VerificationStatus(StrEnum):
    verified = "verified"                    # content hash ok AND signature ok against a trusted key
    unsigned = "unsigned"                    # content hash ok, no signature present
    untrusted_registry = "untrusted_registry"  # signature present but registry has no trusted key
    signature_invalid = "signature_invalid"  # hard block
    hash_mismatch = "hash_mismatch"          # hard block

class InstallStatus(StrEnum):
    pending = "pending"; installed = "installed"; update_available = "update_available"
    failed = "failed"; uninstalled = "uninstalled"

class PackageAuthor(BaseModel):
    name: str
    email: str | None = None
    url: str | None = None

class PackageManifest(BaseModel):
    """The forge-package.yaml schema. extra='forbid' => fail-closed on typos."""
    model_config = ConfigDict(extra="forbid")
    schema_version: int = 1
    kind: ArtifactKind
    slug: str
    name: str
    version: str                              # semver
    summary: str
    description: str | None = None
    authors: list[PackageAuthor] = Field(default_factory=list)
    license: str = "Apache-2.0"
    homepage: str | None = None
    repository: str | None = None
    tags: list[str] = Field(default_factory=list)
    min_forge_version: str | None = None      # semver gate
    artifact: dict                            # embedded body: F09 mcp_connection | F11 SkillProfile
    content_hash: str                         # sha256:<hex> over canonical_artifact_bytes(artifact)

    @field_validator("slug")
    @classmethod
    def _slug(cls, v: str) -> str:
        if not SLUG_RE.match(v):
            raise ValueError("slug must be kebab-case ^[a-z][a-z0-9-]{1,63}$")
        return v

    @field_validator("version", "min_forge_version")
    @classmethod
    def _semver(cls, v: str | None) -> str | None:
        if v is not None and not re.match(r"^\d+\.\d+\.\d+([-+].+)?$", v):
            raise ValueError("must be semver MAJOR.MINOR.PATCH")
        return v

    @field_validator("content_hash")
    @classmethod
    def _hash(cls, v: str) -> str:
        if not HASH_RE.match(v):
            raise ValueError("content_hash must be sha256:<64 hex>")
        return v

class RegistryIndexVersion(BaseModel):
    version: str
    content_hash: str                         # sha256:<hex> of artifact
    manifest_hash: str                        # sha256:<hex> of canonical manifest JSON (signed payload)
    signature: str | None = None             # base64 detached Ed25519 sig over manifest_hash bytes
    manifest_uri: str                         # resolvable URI to forge-package.yaml
    min_forge_version: str | None = None
    published_at: datetime
    yanked: bool = False
    yanked_reason: str | None = None

class RegistryIndexEntry(BaseModel):
    kind: ArtifactKind
    slug: str
    name: str
    summary: str
    tags: list[str] = Field(default_factory=list)
    homepage: str | None = None
    repository: str | None = None
    license: str = "Apache-2.0"
    latest_version: str
    versions: list[RegistryIndexVersion]

class RegistryIndex(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: int = 1
    registry_name: str
    public_key: str | None = None            # Ed25519 verify key (base64); informational copy of the trusted key
    generated_at: datetime
    entries: list[RegistryIndexEntry]

class VerificationResult(BaseModel):
    status: VerificationStatus
    content_hash_ok: bool
    signature_ok: bool | None = None         # None when no signature/key involved
    detail: str | None = None
    @property
    def blocked(self) -> bool:               # hard-block statuses
        return self.status in (VerificationStatus.hash_mismatch, VerificationStatus.signature_invalid)

class InstallRequest(BaseModel):
    registry_id: UUID
    kind: ArtifactKind
    slug: str
    version: str | None = None               # None => latest non-yanked compatible
    acknowledge_unverified: bool = False     # required to install unsigned/untrusted_registry packages
    override_name: str | None = None         # optional rename to avoid collisions

class InstallPlan(BaseModel):
    registry_id: UUID
    kind: ArtifactKind
    slug: str
    version: str
    verification: VerificationResult
    resolved_config: dict                     # MCPConnectionConfig.model_dump() | SkillProfile.model_dump()
    warnings: list[str] = Field(default_factory=list)
    requires_admin_followup: list[str] = Field(default_factory=list)
    blocked: bool = False
    block_reason: str | None = None

class InstallResult(BaseModel):
    installation_id: UUID
    target_kind: str                          # "mcp_connection" | "skill_profile"
    target_object_id: UUID
    status: InstallStatus
    version: str
    verification: VerificationResult
    warnings: list[str] = Field(default_factory=list)

class SyncReport(BaseModel):
    registry_id: UUID
    listings_upserted: int
    versions_upserted: int
    listings_pruned: int
    status: str                               # "ok" | "error"
    error: str | None = None
```

`packages/marketplace-sdk/forge_marketplace/protocols.py`:

```python
from typing import Protocol
from uuid import UUID
from .models import (RegistryIndex, PackageManifest, VerificationResult,
                     RegistryIndexVersion, InstallPlan)

class RegistryClient(Protocol):
    async def fetch_index(self) -> RegistryIndex: ...
    async def fetch_manifest(self, manifest_uri: str) -> tuple[PackageManifest, bytes]:
        """Returns the parsed manifest and the raw canonical manifest bytes (for manifest_hash check)."""

class SignatureVerifier(Protocol):
    def verify(self, *, payload: bytes, signature_b64: str, public_key_b64: str) -> bool:
        """Detached Ed25519 verify. Pure; raises nothing — returns bool."""

class ArtifactInstaller(Protocol):
    kind: "ArtifactKind"
    def validate(self, artifact: dict) -> dict:
        """Validate the embedded artifact via the authoritative loader (mcp-sdk / skill-sdk),
        apply the security floor, and return the normalized config dict. Raises SchemaInvalid."""
    async def apply(self, *, workspace_id: UUID, manifest: PackageManifest, actor: str,
                    override_name: str | None) -> tuple[str, UUID]:
        """Materialize the local object via the F09/F11 service.
        Returns (target_kind, target_object_id). Never enables write / connects / stores secrets."""
```

`forge_marketplace/verifier.py` — the trust boundary:

```python
def verify_version(*, artifact_bytes: bytes, version: RegistryIndexVersion,
                   registry_public_key: str | None,
                   verifier: "SignatureVerifier") -> VerificationResult:
    """1) content_hash: sha256(artifact_bytes) must equal version.content_hash -> else hash_mismatch.
       2) if version.signature and registry_public_key: Ed25519 verify(manifest_hash, sig, key)
          -> verified | signature_invalid.
       3) if version.signature and not registry_public_key -> untrusted_registry.
       4) if not version.signature -> unsigned.
    Returns a VerificationResult; never raises."""
```

`forge_marketplace/manifest.py`:

```python
def canonical_artifact_bytes(artifact: dict) -> bytes:
    """Deterministic JSON encoding (sorted keys, no whitespace, UTF-8) so the same logical
    artifact hashes identically across producers. Used for content_hash on both sides."""

def compute_content_hash(artifact: dict) -> str:  # "sha256:<hex>"
    ...

def load_manifest(raw: str) -> PackageManifest:
    """Parse YAML/JSON -> PackageManifest (fail-closed). Recomputes content_hash and asserts
    it matches the declared content_hash (SchemaInvalid on mismatch)."""
```

FastAPI request/response (`apps/api/app/schemas/marketplace.py`): `RegistrySourceIn`, `RegistryResponse`, `ListingResponse`, `ListingDetailResponse`, `InstallRequest` (reused from sdk), `InstallPlan`, `InstallResult`, `InstallationResponse`, `MarketplaceAuditResponse` — thin DTOs over the SDK models plus DB-derived fields (`trust_level`, `verification_status`, `available_version`).

**`forge-package.yaml`** (authoritative wrapper; the unit a community author publishes — example for a skill profile):

```yaml
schema_version: 1
kind: skill_profile
slug: backend-tdd-strict
name: Backend TDD (strict, 95% coverage)
version: 1.2.0
summary: Hardened backend-tdd profile requiring 95% coverage and integration tests.
description: |
  A stricter variant of the built-in backend-tdd profile...
authors:
  - name: Jane Dev
    url: https://github.com/janedev
license: Apache-2.0
homepage: https://github.com/janedev/forge-skills
repository: https://github.com/janedev/forge-skills
tags: [backend, python, tdd, hardened]
min_forge_version: "3.0.0"
artifact:                                # F11 SkillProfile body, validated by skill-sdk
  schema_version: 1
  name: backend-tdd-strict
  description: Backend feature development with strict TDD discipline
  requires_plan: true
  requires_tests_before_implementation: true
  min_test_coverage: 95
  verification_steps: [lint, type_check, unit_tests, integration_tests]
  review_required: true
  forbidden_shortcuts: [skip_tests, no_error_handling, hardcoded_secrets]
content_hash: "sha256:<64-hex over canonical artifact bytes>"
```

**MCP connector package** uses the same wrapper with `kind: mcp_connector` and `artifact:` set to the F09 `mcp_connection` body (`allow_write` forced false at install regardless of declared value; `transport: stdio` rejected).

**Registry `index.json`** (what a registry publishes; one per registry, signed per version):

```json
{
  "schema_version": 1,
  "registry_name": "Forge Official Marketplace",
  "public_key": "<base64 ed25519>",
  "generated_at": "2026-06-26T00:00:00Z",
  "entries": [
    {
      "kind": "skill_profile",
      "slug": "backend-tdd-strict",
      "name": "Backend TDD (strict)",
      "summary": "Hardened backend-tdd profile...",
      "tags": ["backend", "tdd"],
      "license": "Apache-2.0",
      "latest_version": "1.2.0",
      "versions": [
        {
          "version": "1.2.0",
          "content_hash": "sha256:...",
          "manifest_hash": "sha256:...",
          "signature": "<base64 ed25519 over manifest_hash>",
          "manifest_uri": "skill_profile/backend-tdd-strict/1.2.0/forge-package.yaml",
          "min_forge_version": "3.0.0",
          "published_at": "2026-06-20T00:00:00Z",
          "yanked": false
        }
      ]
    }
  ]
}
```

## 5. Dependencies — features/slices that must exist first

IDs follow the roadmap; **slugs are authoritative** if numbering differs. The auth/secrets/RBAC slice resolved to the canonical slug `cross-cutting/F37-auth-secrets-byok` (it supersedes the stale `v1/F02-auth-workspace-byok` / `v1/F15-auth-secrets-rbac` references used in early drafts of F09/F11); F32 needs it for RBAC roles + the encrypted vault. The foundation slice is referenced as `v1/F00-foundation-substrate` (per F09/F11/F29).

- **v1/F09-mcp-gateway-v1** (REQUIRED) — supplies `packages/mcp-sdk` `MCPConnectionConfig` (the `mcp_connector` artifact schema), the `mcp_connections` table + `mcp_service.create_connection` (the install target), and the read-only/`allow_write=false`/no-auto-connect security model F32 reuses verbatim. F32 adds no MCP protocol logic of its own.
- **v1/F11-skill-profiles** (REQUIRED) — supplies `packages/skill-sdk` `SkillProfile` + `load_skill_profile` (the `skill_profile` artifact schema), the `skill_profile` table + `skill_service.create/update/delete` (install target), `DbSkillProfileRegistry` precedence (override/revert on install/uninstall), and the `SkillDirectivesPanel` reused in the listing preview. F32's `examples/` mirror reuses F11's drift-guard pattern.
- **cross-cutting/F37-auth-secrets-byok** (REQUIRED) — workspaces, users, the authenticated `Principal` + `get_principal` dependency, the flat `require_role("admin"|"member")` RBAC dependency, and the AES-256-GCM per-workspace encrypted secrets vault. F32 routes are RBAC-gated; F32 stores **no** secrets itself (MCP credentials are supplied by the admin post-install via F09 + this vault). Canonical auth slug (supersedes the stale `v1/F02-auth-workspace-byok` / `v1/F15-auth-secrets-rbac`).
- **cross-cutting/F39-audit-log** (REQUIRED) — the canonical immutable `audit_log`, the frozen `AuditEvent` contract + `AuditSink` Protocol, and the reusable `attach_immutability_trigger(table)` helper. F32 emits a compact `marketplace.<op>` `AuditEvent` through the `AuditSink` for every `registry.add/sync/remove`, `install`, `update`, `uninstall`, and `verify` operation, **and** keeps the rich domain `marketplace_audit_log` table (whose immutability trigger is installed via that helper). The redaction is performed by F39's `SecretRedactor`-backed sink.
- **v1/F00-foundation-substrate** (REQUIRED) — uv workspace, FastAPI skeleton (incl. the `Principal`/`get_principal` auth dep wiring point), async SQLAlchemy session, Alembic baseline, Celery + beat, web app shell. F32 adds migration `0032`, the `marketplace` Celery queue + beat schedules, and the marketplace UI routes. Referenced as `v1/F00-foundation-substrate` by F09/F11/F29.
- **cross-cutting/F38-observability-cost-metrics** (SOFT) — if present, the `marketplace.<op>` `AuditEvent`s and sync/install counters surface in the Observability stream / dashboards via the shared `forge_obs` facade; F32 degrades cleanly (audit still written) when observability is disabled. Not required to ship F32.
- **v2/F18-pm-adapters** (SOFT) — `packages/integration-sdk`'s adapter-registry pattern is the model for a future `pm_adapter`/connector kind; not required for the V3 MCP-connector + skill-profile scope.
- **v2/F21-workflow-automations** (SOFT) — the `workflow_template` reserved kind installs F21 automation/DSL artifacts; the installer for that kind is only registered when F21 is present (its §12 explicitly anticipates "Automation marketplace … shareable rule templates" extending this slice). Likewise **v1/F04-repo-policy** (SOFT) for the reserved `policy_template` kind.

**Consumers (not dependencies):** none block on F32. Installed objects are plain F09/F11 rows consumed by the knowledge service / agent runtime exactly as if hand-created.

## 6. Acceptance criteria (numbered, testable)

1. Migration `0032` creates `marketplace_registries`, `marketplace_listings`, `marketplace_listing_versions`, `marketplace_installations`, `marketplace_audit_log` with all columns, the unique/check constraints, the GIN/full-text/btree indexes, and the `marketplace_audit_log_immutable` trigger; `alembic downgrade` cleanly drops them.
2. **Official registry seeded.** On workspace creation a `marketplace_registries` row `slug='official', trust_level='official'` is created from `MARKETPLACE_OFFICIAL_REGISTRY_URL` + pubkey; it is enabled and read-only (cannot be edited to a different URL, can be disabled).
3. **Catalog sync.** `marketplace.sync_registry` fetches and validates `index.json` against `RegistryIndex`, upserts listings + versions, prunes upstream-removed entries, sets `last_sync_*`, and writes one `registry.sync` audit row. A malformed index (`extra` key / schema violation) sets `last_sync_status=error` and upserts nothing.
4. **Hash mismatch is a hard block.** When the fetched artifact's sha256 ≠ the version `content_hash`, `verify_version` returns `hash_mismatch`, `/preview` returns `blocked=true, block_reason`, `/install` returns 422 and creates **no** object, and an audit row `result_status=denied, error_code=hash_mismatch` is written.
4b. **content_hash internal consistency.** `load_manifest` recomputes `content_hash` from the embedded artifact and rejects a manifest whose declared `content_hash` disagrees (`SchemaInvalid`).
5. **Signature verification.** For a registry with a public key, a version whose detached Ed25519 signature over `manifest_hash` is valid → `verified`; a tampered signature → `signature_invalid` (hard block, audited). A signed version on a registry with no public key → `untrusted_registry`; an unsigned version → `unsigned`.
6. **Unverified gate.** Installing an `unsigned` or `untrusted_registry` package without `acknowledge_unverified=true` is rejected (422, `error_code=signature_required`); with the acknowledgement it succeeds and records `verification_status` accordingly. An `official`/`trusted` registry package that is not `verified` is **always** rejected regardless of the acknowledgement.
7. **Fail-closed artifact validation.** A `skill_profile` package whose embedded artifact violates the F11 schema (e.g. unknown key, bad slug, coverage 150) is blocked (`schema_invalid`); an `mcp_connector` package whose artifact violates `MCPConnectionConfig` is blocked. Verified by asserting no object is created.
8. **MCP connector security floor.** Installing an `mcp_connector` creates an `mcp_connections` row with `allow_write=false` and `status=pending`, regardless of a declared `allow_write: true` in the artifact; no live MCP call and no OAuth/credential write occurs at install; `requires_admin_followup` includes connect/credentials guidance. A package declaring `transport: stdio` is blocked.
9. **Skill profile install.** Installing a `skill_profile` creates an F11 `skill_profile` custom row whose body equals the validated artifact; if the name shadows a builtin a warning is returned and `overrides_builtin=true`; the profile is immediately resolvable via `DbSkillProfileRegistry`.
10. **Name-collision handling.** Installing a `skill_profile` whose name collides with an existing **custom** row is rejected (409) unless `override_name` is provided or the caller uses `/installations/{id}/update`.
11. **Preview is side-effect-free.** `POST /preview` returns a complete `InstallPlan` (verification + resolved config + warnings + follow-ups) and creates **no** `mcp_connections` / `skill_profile` / `marketplace_installations` rows (assert row counts unchanged).
12. **Install provenance.** A successful `/install` creates exactly one `marketplace_installations` row linking `registry_slug`/`listing_slug`/`installed_version`/`content_hash`/`verification_status`/`target_object_id`, and one `install` audit row.
13. **Update flow.** After a sync surfaces a newer non-yanked compatible version, the installation shows `status=update_available, available_version`; `POST /installations/{id}/update` re-verifies + re-validates + applies (skill body replaced; MCP connection scope/endpoint change resets `status=pending`), bumps `installed_version`, and audits `update`. A pinned installation never flips to `update_available`.
14. **Compatibility gate.** A version whose `min_forge_version` is greater than the running Forge version is excluded from "latest compatible" resolution and, if explicitly requested, blocked with `error_code=forge_version_incompatible`.
15. **Yank propagation.** When a sync marks the installed version `yanked`, `refresh_update_flags` flags the installation with the yank reason; the UI surfaces it (asserted via the installation response field).
16. **Uninstall.** `DELETE /installations/{id}` deletes the linked object via the F09/F11 service (skill override → reverts to builtin; MCP connection deleted with F09 audit retained), sets the installation row `status=uninstalled, target_object_id=null`, and audits `uninstall`.
17. **SSRF bounds.** A registry URL resolving to a private/link-local/metadata address (`10.0.0.0/8`, `127.0.0.1`, `169.254.169.254`, `::1`) is refused by the registry client (`SsrfBlocked`, audited `ssrf_blocked`) unless its host is in `MARKETPLACE_ALLOWED_REGISTRY_HOSTS`; index/manifest fetches over `MARKETPLACE_MAX_*_BYTES` or past timeout are aborted.
18. **Audit immutability + completeness.** Every `registry.add/sync/remove`, `install`, `update`, `uninstall`, and `verify` writes exactly one immutable `marketplace_audit_log` row; `UPDATE`/`DELETE` on the table raises (trigger).
19. **RBAC + tenant isolation.** Browse/list (`GET /listings`, `/installations`) require `member`; add-registry/install/update/uninstall require `admin` (403 for `member`); all reads/writes scope by `workspace_id`; cross-workspace access to a registry/installation returns 404. Unauthenticated → 401.
20. **No code execution + drift guards.** The installer has no path that imports/executes package-provided code (asserted by the absence of `eval`/`exec`/dynamic import in `installer.py` and a test that a package with an extra `artifact.__code__`-style key is ignored/rejected); the committed `package-manifest.schema.json` equals `PackageManifest.model_json_schema()`; `forge marketplace package` emits a manifest whose `content_hash` re-verifies.

### Traceability — spec → criteria

| Spec element | Criteria |
|---|---|
| Phase 3 "Integration marketplace for community MCP connectors and skill profiles" | 2, 3, 7, 8, 9, 12, 13 |
| Extension Points (MCP gateway accepts any server; skill profiles = plain YAML) | 8, 9, 10 |
| Security — secrets/audit/RBAC/redaction; supply-chain integrity | 4, 4b, 5, 6, 17, 18, 19, 20 |
| MCP Security Rules (read-only default, no auto-connect) | 8 |
| OSS / self-hosting (federated registries, offline) | 2, 3, 14, 20 |

## 7. Test plan (TDD) — concrete test cases (unit + integration), key fixtures

Tests-first, backend-tdd discipline (≥80% coverage). Tests live in `packages/marketplace-sdk/tests/`, `apps/api/tests/marketplace/`, `apps/worker/tests/`, and `apps/web`.

Key fixtures:
- `signing_keypair` — an Ed25519 keypair (via `cryptography`); a helper `sign_manifest(manifest_hash) -> b64` to forge valid/invalid signatures.
- `FakeRegistryClient` — in-memory `RegistryClient` returning a configurable `RegistryIndex` + manifests with controllable `content_hash`/`signature`/`yanked`/`min_forge_version`; **no network**.
- `make_skill_package` / `make_mcp_package` — build a `forge-package.yaml` + matching index version (correct hash + signature) and "corrupt" variants (hash mismatch, bad sig, schema-invalid artifact, `stdio` transport, `allow_write: true`, shadow-builtin name).
- `FakeMcpService` / `FakeSkillService` — record `create_connection` / `create` calls and assert the security floor (`allow_write=false`, `status=pending`, no secret/connect call).
- `pg` — real Postgres via testcontainers, migration `0032` applied (catalog SQL, full-text search, audit immutability trigger).
- `api_app` — httpx `AsyncClient` against FastAPI with DI overrides injecting the fakes; Celery eager.

Unit — `packages/marketplace-sdk/tests/` (pure, no network):
- `test_manifest_loads_and_rejects_extra_key` and `test_manifest_content_hash_consistency` (AC7, AC4b).
- `test_canonical_artifact_bytes_is_deterministic` (cross-producer hash stability, AC4/AC20).
- `test_verify_version_hash_mismatch_blocks` (AC4); `test_verify_version_valid_invalid_signature` (AC5); `test_verify_version_unsigned_and_untrusted_registry` (AC5).
- `test_index_parse_rejects_malformed` (AC3).
- `test_semver_compat_gate_and_latest_compatible` (AC14); `test_yank_excluded_from_latest` (AC14/AC15).
- `test_skill_installer_validates_via_skill_sdk_and_blocks_bad_profile` (AC7, AC9).
- `test_mcp_installer_forces_readonly_and_blocks_stdio` (AC8) — asserts resolved config `allow_write=false`, follow-ups present, `stdio` raises `SchemaInvalid`.
- `test_no_dynamic_execution_in_installer` (AC20) — static assert installer source has no `eval`/`exec`/`__import__`.
- `test_json_schema_matches_committed` (AC20).

Service/integration — `apps/api/tests/marketplace/` (DI fakes + Postgres):
- `test_preview_is_side_effect_free` (AC11) — row counts unchanged before/after.
- `test_install_skill_creates_object_and_provenance` (AC9, AC12); `test_install_skill_name_collision_409` (AC10).
- `test_install_mcp_creates_pending_readonly_connection` (AC8) — via `FakeMcpService`, assert `allow_write=false`, `status=pending`, no connect/credential call.
- `test_install_blocked_on_hash_mismatch_and_bad_signature` (AC4, AC5, AC18).
- `test_unverified_requires_acknowledgement_and_official_always_strict` (AC6).
- `test_update_flow_replaces_body_and_resets_mcp_pending` (AC13); `test_pinned_not_flagged_update` (AC13).
- `test_forge_version_incompatible_blocked` (AC14).
- `test_uninstall_reverts_skill_override_and_deletes_connection` (AC16).
- `test_audit_completeness_one_row_per_op` (AC18); `test_audit_update_delete_raises` (AC18).
- `test_rbac_member_cannot_install_and_tenant_isolation` (AC19); `test_unauth_401` (AC19).

Worker — `apps/worker/tests/`:
- `test_sync_registry_upserts_prunes_and_audits` (AC3) — with `FakeRegistryClient`.
- `test_sync_malformed_index_records_error_no_writes` (AC3).
- `test_refresh_update_flags_sets_available_and_yank` (AC13, AC15).
- `test_official_registry_seeded_on_workspace_create` (AC2).
- `test_registry_client_blocks_private_addresses` (AC17) — monkeypatch DNS resolution to RFC1918/metadata; assert `SsrfBlocked`; allowlist override permits.

Migration/DB — `apps/api/tests/marketplace/`:
- `test_migration_0032_up_down` (AC1) — tables/constraints/indexes via `pg_indexes`; full-text index present.

CLI — `apps/api/tests/test_marketplace_cli.py`:
- `forge marketplace package` round-trips a skill profile → manifest with re-verifiable `content_hash` (AC20); `search`/`show`/`install`/`list`/`update` exit codes.

Frontend — `apps/web` (Vitest + RTL):
- `InstallDialog` disables confirm until `acknowledge_unverified` checked for unsigned packages; renders verification + follow-ups.
- `VerificationBadge` maps each `VerificationStatus` to the correct label/variant.

## 8. Security & policy considerations

The marketplace ingests **untrusted third-party supply-chain content**, so safety is the slice's core, not an add-on:

1. **Declarative-only, zero code execution.** Packages are YAML data describing F09/F11 artifacts. The installer never imports, evals, or runs anything from a package; there is no "plugin-as-code" path in V3 (deferred, §12). AC20.
2. **Integrity: content hashing.** Every artifact is sha256-hashed with a canonical encoding on both producer and consumer; a mismatch hard-blocks install. AC4/AC4b.
3. **Provenance: detached signatures + trust levels.** Registries carry an Ed25519 public key; each version is signed over its `manifest_hash`. `verified` requires a good signature against a trusted key; `unsigned`/`untrusted_registry` are soft-gated behind an explicit admin acknowledgement; `official`/`trusted` registries demand `verified`. Tampered signatures hard-block. AC5/AC6.
4. **Fail-closed schema validation.** Embedded artifacts are validated by the *authoritative* F09/F11 loaders (`extra='forbid'`), so a malformed connector/profile can never be installed. AC7.
5. **Least-privilege defaults inherited from F09.** MCP connectors install `allow_write=false`, `status=pending`, with **no auto-connect, no OAuth, no credential write**; `stdio` is rejected; namespace-scope warnings surface over-broad configs. The admin must consciously connect via F09 — the marketplace cannot widen scope or open a live session. AC8. (Matches `docs/FORGE_SPEC.md` → MCP Security Rules 1, 7 and Build Prompt constraint #8.)
6. **Skill profiles are a floor, never an escalation.** Installed profiles flow through F11 where `skill_permits_action` can only *deny*; an installed profile can never widen repo policy (Build Prompt constraint #2). Name collisions with builtins become explicit overrides, surfaced as warnings. AC9/AC10.
7. **No secrets in the marketplace.** Manifests and the catalog carry no credentials; the schema has no secret fields. MCP credentials are supplied by the admin post-install via the F02 vault. Sync/install errors are redacted before persistence/logging.
8. **SSRF / egress hardening.** Registry fetches resolve and reject RFC1918 / loopback / link-local / cloud-metadata (`169.254.169.254`) addresses unless host-allowlisted; per-fetch timeouts + size caps bound DoS; the worker network policy limits egress. AC17.
9. **Tenant isolation + RBAC.** Registries, listings, and installations are workspace-scoped; cross-workspace access 404s. Browse = member; mutate = admin; no anonymous access. AC19.
10. **Immutable audit.** Every registry/install/update/uninstall/verify operation writes one append-only `marketplace_audit_log` row (registry, slug, version, content_hash, verification status, result, error code) **and** emits a compact `marketplace.<op>` `AuditEvent` through the canonical `AuditSink` (`cross-cutting/F39-audit-log`), so marketplace activity lands in the central platform `audit_log` + Observability stream; the domain table's immutability trigger is installed via F39's `attach_immutability_trigger` helper, and the redacted write flows through F39's `SecretRedactor`-backed sink. AC18.
11. **Yank / recall path.** A registry can mark a version `yanked`; sync propagates the warning to installed workspaces so a known-bad connector/profile is surfaced for remediation. AC15.
12. **No autonomous / agent / merge path.** F32 is admin tooling — no agent consumes the marketplace, and nothing it does merges code or bypasses a human gate. Installs, updates, and uninstalls are explicit admin actions with no auto-apply (esp. for MCP scope/endpoint changes, which reset to `status=pending`). The platform's human-approval-before-merge non-negotiable (`cross-cutting/F36-human-approval-system`) is therefore unaffected: F32 only materializes declarative F09/F11 rows that still flow through their normal test/connect/review gates.

## 9. Effort estimate & risk (S/M/L + key risks)

**Effort: L** — a new SDK plus a security trust boundary and full CRUD/UI, but **no new service and no new protocol** (it composes F09 + F11). Breakdown: models + manifest/index + canonical hashing (S); registry client (http + git) with SSRF guard (M); Ed25519 verifier + verification precedence (M); installer delegating to F09/F11 + security floor (M); migration + service + 13 routes (M); worker sync/flag tasks + seeding (S); UI (M); tests (M).

| Risk | Severity | Mitigation |
|---|---|---|
| **Supply-chain trust** (malicious/poisoned package) | High | Declarative-only (no code exec), hash + signature verification, fail-closed schema validation via authoritative loaders, least-privilege install (read-only/pending MCP), yank propagation, full audit. AC4–AC9, AC15, AC18, AC20. |
| **Signature/canonicalization correctness** (hash differs across producers; sig verified wrong) | High | One canonical encoder shared by producer (`forge marketplace package`) and consumer; pure unit-tested `verifier`/`manifest`; round-trip CLI test (AC20); detached-sig over a single declared `manifest_hash`. |
| **SSRF via registry URL** | High | DNS-resolution guard against private/metadata ranges + host allowlist + timeouts + size caps; worker egress network policy. AC17. |
| **Silently re-pointing a connected MCP server on update** | Medium | Updates that change MCP scope/endpoint reset `status=pending` and require admin re-confirm/re-connect; never mutate a live connection's scope. AC13. |
| **Name collisions / builtin shadowing** | Medium | Explicit 409 + `override_name`; shadow-builtin becomes an audited override warning; uninstall reverts. AC9/AC10/AC16. |
| **Registry availability / offline installs** | Low | ETag caching, per-registry isolation (one failure doesn't fail others), file/git URLs for air-gapped official registry. AC3. |
| **Schema drift between package manifest and committed JSON schema / examples** | Low | Drift-guard tests (committed `package-manifest.schema.json` == generated; examples mirror official registry). AC20. |

## 10. Key files / paths (exact)

- `apps/api/alembic/versions/0032_integration_marketplace.py` — five tables + immutability trigger + full-text/GIN indexes.
- `packages/marketplace-sdk/pyproject.toml` — uv workspace member (depends on `mcp-sdk`, `skill-sdk`).
- `packages/marketplace-sdk/forge_marketplace/models.py` — Pydantic models + enums.
- `packages/marketplace-sdk/forge_marketplace/protocols.py` — `RegistryClient`, `SignatureVerifier`, `ArtifactInstaller`.
- `packages/marketplace-sdk/forge_marketplace/manifest.py` — `load_manifest`, `canonical_artifact_bytes`, `compute_content_hash`.
- `packages/marketplace-sdk/forge_marketplace/index.py` — `RegistryIndex` parsing.
- `packages/marketplace-sdk/forge_marketplace/registry_client.py` — `HttpIndexRegistryClient`, `GitRegistryClient`, SSRF guard.
- `packages/marketplace-sdk/forge_marketplace/verifier.py` — `Ed25519SignatureVerifier`, `verify_version`.
- `packages/marketplace-sdk/forge_marketplace/installer.py` — `McpConnectorInstaller`, `SkillProfileInstaller`, registry of installers by kind.
- `packages/marketplace-sdk/forge_marketplace/catalog.py` — index→listings merge, update-flag/semver logic.
- `packages/marketplace-sdk/forge_marketplace/packaging.py` — `build_package` (author CLI).
- `packages/marketplace-sdk/forge_marketplace/package-manifest.schema.json` — generated, checked in (drift guard).
- `packages/marketplace-sdk/forge_marketplace/errors.py`.
- `packages/marketplace-sdk/tests/` — unit tests + `FakeRegistryClient`, `signing_keypair`, `make_*_package` fixtures.
- `apps/api/app/api/v1/marketplace.py` — router.
- `apps/api/app/services/marketplace_service.py` — orchestration (delegates to `mcp_service`/`skill_service`).
- `apps/api/app/schemas/marketplace.py` — request/response DTOs.
- `apps/api/app/models/marketplace.py` — SQLAlchemy ORM (`MarketplaceRegistry`, `MarketplaceListing`, `MarketplaceListingVersion`, `MarketplaceInstallation`, `MarketplaceAuditLog`).
- `apps/api/app/cli/marketplace.py` — `forge marketplace search|show|install|list|update|package`.
- `apps/api/app/startup/seed_official_registry.py` — per-workspace official registry seeding hook.
- `apps/api/tests/marketplace/` — service/API/migration tests.
- `apps/worker/tasks/marketplace.py` — `sync_registry`, `sync_all_registries`, `refresh_update_flags`.
- `apps/worker/tests/test_marketplace_tasks.py`.
- `apps/web/app/(app)/marketplace/{page.tsx,[registry]/[slug]/page.tsx,installed/page.tsx,settings/registries/page.tsx}`.
- `apps/web/components/marketplace/{ListingCard,VerificationBadge,TrustBadge,InstallDialog,UpdateDiffDialog,RegistryForm}.tsx`.
- `apps/web/lib/api/marketplace.ts`.
- `deploy/docker-compose.yml` (worker `marketplace-egress` network + beat schedule), `deploy/.env.example`, `deploy/.env.production.example`, `deploy/helm/values.yaml` (`marketplace.officialRegistry.*`).
- `examples/marketplace/{registry-index.example.json, skill-package.example.yaml, mcp-package.example.yaml}` — author templates with signing notes.

## 11. Research references (relevant links from the spec/research report)

- `docs/FORGE_SPEC.md` → "OSS Strategy" / "Extension Points" — *"MCP gateway: accepts any MCP-compatible server"*, *"Skill profiles: plain YAML — community contributions welcome"*, "Tool registry: pluggable" (the extension surface the marketplace distributes over).
- `docs/FORGE_SPEC.md` → "Phased Roadmap" → Phase 3 — *"Integration marketplace for community MCP connectors and skill profiles"* (the feature charter).
- `docs/FORGE_SPEC.md` → "MCP Integration" (the `mcp_connection` schema the `mcp_connector` artifact reuses) and "MCP Security Rules" 1 & 7 (read-only default, policy parity — the install security floor).
- `docs/FORGE_SPEC.md` → "Skill Profiles" (the `SkillProfile` schema the `skill_profile` artifact reuses).
- `docs/FORGE_SPEC.md` → "Security" table (secrets isolation, RBAC, immutable audit, secret redaction — all applied to marketplace operations) and "Self-Hosting" (federated/offline-capable registry, Apache-2.0 OSS).
- MCP security advisory (DoD 2026) — supply-chain & least-privilege rationale carried into install defaults: https://media.defense.gov/2026/Jun/02/2003943289/-1/-1/0/CSI_MCP_SECURITY.PDF
- MCP June 2025 update (OAuth/RFC 8707 token binding handled by F09 at connect time, not by the marketplace): https://forgecode.dev/blog/mcp-spec-updates/
- Superpowers skill framework (the community-skills ecosystem the profile half of the marketplace serves): https://mcpmarket.com/server/superpowers , https://www.termdock.com/en/blog/superpowers-framework-agent-skills
- Sibling slices: `docs/implementation-slices/v1/F09-mcp-gateway-v1.md` (MCP install target + audit pattern), `docs/implementation-slices/v1/F11-skill-profiles.md` (skill install target + drift-guard/examples pattern; its §12 explicitly defers "Marketplace / sharing of community skill profiles" to this slice), `docs/implementation-slices/v2/F21-workflow-automations.md` §12 (anticipates extending this marketplace to shareable rule templates).

## 12. Out of scope / future

- **Plugin-as-code / custom tools as installable packages** — V3 distributes only declarative artifacts (MCP connectors, skill profiles). Installing executable tool code (the "Tool registry: pluggable" extension point) requires sandboxed execution + a far stronger trust model and is deferred.
- **`workflow_template` and `policy_template` kinds** — enums and installer Protocol slots exist, but their installers are registered only when F21 (`packages/workflow-engine`) / F04 (`packages/policy-sdk`) provide the loaders + targets. Shipped as reserved kinds; full support lands with those integrations.
- **PM-adapter packages** — distributing community `PMAdapter` implementations (F18) is a code-bearing extension, deferred with plugin-as-code.
- **In-app publishing / one-click submit** — V3 publishing is the OSS path: `forge marketplace package` + a PR to a registry git repo. A hosted submit/review UI and a Forge-operated registry backend are future.
- **Ratings, reviews, install counts, telemetry** — no centralized social/telemetry layer in V3 (privacy + self-hosting first); the catalog shows author/version/provenance only.
- **Automatic/unattended updates** — updates are always admin-reviewed (diff + re-verify); no auto-apply, especially for MCP connectors whose scope/endpoint changes reset to `pending`.
- **Cross-workspace shared registry cache** — each workspace caches its own catalog for strict tenant isolation; a shared cache is a later optimization.
- **Dependency graphs between packages** (a connector requiring a specific skill) — V3 packages are standalone; inter-package dependencies are future.
- **Sigstore/keyless (OIDC) signing & transparency log** — V3 uses static Ed25519 keys per registry; keyless signing + a transparency log are a future hardening step.
