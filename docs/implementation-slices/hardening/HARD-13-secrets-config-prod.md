# HARD-13 — Production Secrets & Config Hardening

> Phase: hardening · Blocker(s): #4 (no real security audit — secrets handling) ·
> Status target: **DONE/verified** means: the BYOK vault uses two-tier envelope
> encryption with rotatable keys; `FORGE_SECRET_KEY` is the *only* master-key
> ingress, resolvable from env/file/Vault, with **no silent ephemeral fallback in
> production**; stored BYOK secrets and agent tokens honour `expires_at` at
> read-time; all config is 12-factor (one prefix, one settings surface across
> api/worker/mcp-gateway) with the current `SECRET_KEY`/`FORGE_ENV` drift bugs
> closed; and redaction is *structural* (a logging filter + OTel span processor +
> a build-time image secret scan) so secrets cannot reach logs, traces, or
> images. Most of this runs **offline** (hermetic unit tests, no creds). Only the
> optional HashiCorp-Vault provider path and the live key-rotation drill against a
> populated Postgres are `@pytest.mark.integration` (the rotation drill needs a
> live pgvector DB from HARD-01, not external creds; the Vault path needs a Vault
> dev server). The 3rd-party human pentest of this surface stays a HARD-09
> punch-list item.

---

## 1. Intent — what & why

The ALPHA's secret handling is *correct in shape but unproven and unsafe to
deploy as shipped*. MORNING_REPORT §5(6,7) parks the production crypto/OAuth
seams; §6 calls out that "no real external system has ever happened"; the
FORGE_SPEC Security table demands "Secrets — Encrypted at rest, per-workspace
isolation, **automatic expiry for agent tokens**" and "Secret redaction — secrets
stripped from logs, traces, and retrieval results". HARD-10 already promotes
`FernetCipher` to the default cipher and makes `FORGE_SECRET_KEY` *required* in
non-dev environments. HARD-13 sits **on top of HARD-10** and closes what HARD-10
does not: the operational, production-grade properties of the secret subsystem.

Three concrete, load-bearing defects in the *real* monorepo motivate this slice
(found by reading the shipped code, not assumed):

1. **Config var-name drift silently re-opens the ephemeral-key hole HARD-10
   closed.** `deploy/docker-compose.yml` sets `FORGE_ENV: production` and
   `SECRET_KEY: ${SECRET_KEY:-change-me}` (lines 122/127, 174/178), but the auth
   layer reads **`FORGE_ENVIRONMENT`** for dev-detection
   (`auth/service.py:_is_development_environment`) and **`FORGE_SECRET_KEY`** for
   the master key (`auth/service.py:_resolve_master_key`). On the *shipped*
   compose, `FORGE_ENVIRONMENT` is unset → it defaults to `"development"` →
   `_is_development_environment()` returns `True` → the service classifies a
   production deployment as *dev* and falls back to a **process-ephemeral key**
   (vault contents + API-key verification silently reset on every restart), and
   `FORGE_SECRET_KEY` is never plumbed at all. HARD-10's "no silent ephemeral
   fallback in prod" guarantee is a no-op because prod is mis-classified as dev.
2. **BYOK secrets never expire.** `vault.get_secret()` returns plaintext
   regardless of `StoredSecret.expires_at`; the column exists and the API accepts
   `expires_at`, but nothing enforces it. The spec's "automatic expiry for agent
   tokens" holds for *API keys* (`apikeys.verify` checks expiry) but **not** for
   vaulted BYOK credentials.
3. **No envelope encryption / no rotation path.** `auth/service.py` derives the
   cipher subkey directly from a single master key over *all* secrets. Rotating
   the master key today would require decrypting and re-encrypting every stored
   secret — there is no per-secret data key and no key-version on the wire, so
   rotation is effectively impossible in production.

Plus two structural gaps: secret ingress is ad-hoc `os.environ` reads scattered
across `settings.py`, `auth/service.py`, `auth/oauth.py`, and
`forge_worker/celery_app.py` (no single, testable, Docker/K8s-secret-aware
abstraction), and redaction is *call-site-only* (`observability/redaction.py` is
applied by the audit writer and trace assembler but is **not** installed as a
logging sink filter or an OTel span processor, so an accidental
`logger.info(secret)` anywhere leaks).

HARD-13 turns the secret subsystem from "right-shaped" into "operable in
production": envelope encryption with rotatable, versioned keys; read-time expiry;
a unified 12-factor config + secret-provider abstraction (env / file / Vault); and
*guaranteed* (structural, not best-effort) redaction across logs, traces, and
images. This is the secrets half of Blocker #4; HARD-09 consumes its evidence and
the rotation runbook into the security pack, and the human pentest of the surface
remains HARD-09's punch-list item.

## 2. User-facing / operator behavior

This slice is operator-facing (self-hosters and platform admins), with a thin
end-user surface (BYOK secret expiry shown in the UI).

- **Journey A — First production boot, fail-closed.** An operator copies
  `.env.production.example`, fills `FORGE_SECRET_KEY` (or points
  `FORGE_SECRET_KEY_FILE` at a Docker/K8s secret), sets
  `FORGE_ENVIRONMENT=production`, and runs `docker compose up`. If
  `FORGE_SECRET_KEY` is absent the api/worker/mcp-gateway **refuse to start** with
  a single clear error and a generator hint — no service ever boots with an
  ephemeral key in production. The deprecated `SECRET_KEY`/`FORGE_ENV` names still
  work for one release but log a loud deprecation warning naming the replacement.
- **Journey B — Secret rotation drill (KEK).** An operator runs
  `python -m forge_api.cli.secrets rotate-kek` (future `forge-cli secrets
  rotate-kek`) after setting `FORGE_SECRET_KEY` to the new key and
  `FORGE_SECRET_KEY_V1` to the previous one. The command **re-wraps every stored
  data key** under the new KEK and bumps `api_key.key_version` — without
  decrypting any BYOK plaintext — printing `{rewrapped: N, skipped: M}`. The old
  KEK can be removed from the env after the run completes. Zero downtime: reads
  during the window transparently unwrap under whichever KEK version each row
  carries.
- **Journey C — BYOK key expiry.** A member stores an `ANTHROPIC_API_KEY` with a
  90-day `expires_at`. After expiry, any agent run or retrieval that resolves the
  key gets a clear `SecretExpiredError` (mapped to HTTP 409 with a "rotate this
  credential" message), the UI badges the key `expired`, and the nightly
  `auth.expire_secrets` sweep flags/purges it. Agent-runner API keys get a default
  TTL so dormant agent tokens auto-expire per the spec.
- **Journey D — Secrets via files / Vault (12-factor).** Instead of inline env
  values, an operator mounts Docker secrets and sets `FORGE_SECRET_PROVIDER=file`
  (reads `/run/secrets/<key>`) or `FORGE_SECRET_PROVIDER=vault` (reads a
  HashiCorp Vault KV path). The same `Settings`/resolver surface works unchanged;
  any `FORGE_*` key may instead be supplied as `FORGE_*_FILE` pointing at a file.
- **Journey E — Redaction is guaranteed.** Whatever an operator (or a bug) logs,
  the root/uvicorn/celery log handlers scrub secret-shaped substrings and
  secret-named fields before the record is emitted; OTel spans are scrubbed by a
  span processor before export; and `docker compose build` is gated by a CI step
  that fails if any built image layer/history or `.env*`/`*.pem` is baked into the
  image.

## 3. Vertical slice

### 3.1 Data model

Single additive Alembic migration (extends `packages/db/forge_db`, chains after
the baseline `0001_*`; revision number is ordering only — pick the next free, e.g.
`0019_envelope_key_version`). It is **data-preserving** and reversible.

Changes to the existing **`api_key`** table (the production-backed vault store —
`encrypted_secret` already exists, `LargeBinary`, holding the envelope blob):

| Column | Type | Notes |
|---|---|---|
| `key_version` | `SMALLINT NOT NULL DEFAULT 1` | KEK version the row's *data key* is currently wrapped under. Lets KEK rotation target `WHERE key_version < :current` cheaply. |
| `rotated_at` | `timestamptz NULL` | Last time the row's DEK was re-wrapped (rotation audit). |

`expires_at` already exists on `api_key` (`packages/db/forge_db/models/workspace.py`
line 71) — no schema change; HARD-13 makes it **enforced** (see §3.2). No new
table. The envelope wrapped-DEK travels *inside* the `encrypted_secret` blob
(self-describing header, §4), so no separate DEK column is needed; `key_version`
is denormalised purely to make rotation queries index-friendly
(`CREATE INDEX ix_api_key_key_version ON api_key (key_version);`).

Downgrade drops `key_version`, `rotated_at`, and the index. (Rows written by the
envelope cipher remain decryptable post-downgrade only while the envelope blob
format is still understood by the code; the migration note documents that a
downgrade must be paired with a code rollback — captured in the upgrade runbook.)

### 3.2 Backend

Extends `apps/api/forge_api/auth/*` (the home the SPEC already assigns to all
crypto/vault/secret work — HARD-02/-10 point here too) plus
`apps/api/forge_api/settings.py` and `apps/api/forge_api/observability/*`. **No new
package** — every symbol lands in existing `forge_api` modules.

**(a) `auth/keyring.py` (new module, `forge_api.auth.keyring`).** Versioned KEK
material resolved from the secret provider:

- `KeyRing` — `current_version: int`, `kek(version: int) -> bytes`,
  `current_kek() -> bytes`, `versions() -> list[int]`.
- `KeyRing.from_provider(provider, *, current_version: int | None = None)` — reads
  `FORGE_SECRET_KEY` as the current KEK and `FORGE_SECRET_KEY_V<n>` as older
  versions; `current_version` defaults to the highest present (or
  `FORGE_SECRET_KEY_VERSION`). Raises if no current key in a non-dev env.

**(b) `auth/crypto.py` (extend).** Add a two-tier envelope cipher implementing the
existing `SecretCipher` Protocol so the vault is unchanged:

- `class EnvelopeCipher(SecretCipher)` — wraps the existing `default_cipher`
  (Fernet) as both the **KEK-wrap** primitive (over a per-secret DEK) and the
  **DEK-encrypt** primitive (over plaintext). `encrypt()` mints a fresh DEK
  (`generate_key()`), encrypts plaintext under it, wraps the DEK under
  `keyring.current_kek()`, and emits the versioned blob (§4). `decrypt()` reads the
  blob's KEK version, unwraps the DEK with `keyring.kek(version)`, then decrypts.
- `rewrap(blob: bytes, *, to_version: int | None = None) -> tuple[bytes, int]` —
  unwraps the DEK under its stored version and re-wraps it under `to_version`
  (default current) **without touching the data ciphertext**; returns
  `(new_blob, to_version)`. This is the KEK-rotation primitive.
- **Legacy compat:** a `\x01`-version blob (today's `HmacAeadCipher`/`FernetCipher`
  single-tier format) decrypts via the wrapped legacy cipher, satisfying HARD-10
  AC3 (existing-data decrypt path). `encrypt()` always writes the new `\x02`
  format; the next rotation upgrades legacy rows.
- `envelope_cipher(keyring) -> SecretCipher` factory; `default_cipher` stays the
  single-tier seam HARD-10 ships, and `AuthService` selects envelope-vs-single via
  config (`FORGE_ENVELOPE_ENCRYPTION`, default `true` in prod).

**(c) `auth/providers.py` (new module, `forge_api.auth.providers`).**
Secret-provider abstraction (env / file / Vault):

- `SecretProvider` Protocol — `name: str`, `get(key: str) -> str | None`.
- `EnvSecretProvider` — reads `os.environ[key]`; also honours `<key>_FILE`
  indirection (reads + strips file contents) so any var can be a mounted secret.
- `FileSecretProvider(root: Path)` — reads `root / key` (default
  `/run/secrets`, the Docker/K8s secret convention).
- `VaultSecretProvider(addr, token, mount, path, *, client=None)` — HashiCorp
  Vault KV-v2 via an injectable client (`hvac`); **integration-gated**, never on
  the hermetic path.
- `ChainSecretProvider([...])` — first non-`None` wins; default chain is
  `[EnvSecretProvider(), FileSecretProvider()]`.
- `build_provider(settings) -> SecretProvider` and module-level
  `resolve_secret(key) -> str | None` used by `settings.py`, `service.py`,
  `oauth.py`, and the worker so there is exactly **one** secret ingress.

**(d) `auth/vault.py` (extend) — read-time expiry + rotation.**

- `SecretExpiredError(SecretNotFoundError)`.
- `get_secret(...)` raises `SecretExpiredError` when
  `record.expires_at is not None and record.expires_at <= now` (clock injectable
  for tests). `raw_record` is unchanged (rotation needs the ciphertext even when
  expired).
- `rotate_secret(workspace_id, secret_id, new_secret, *, expires_at=None)` —
  re-encrypts a BYOK value (value rotation), preserving id/name/kind, updating
  `key_prefix`/`updated_at`, emitting an audit event.
- `rewrap_all(*, keyring, to_version) -> dict` — iterates the store, calls
  `EnvelopeCipher.rewrap` per row, persists the new blob + `key_version` +
  `rotated_at`; returns `{rewrapped, skipped}` (KEK rotation).
- `sweep_expired(*, now=None, purge=False) -> int` — flags or deletes expired
  records (used by the beat task).

**(e) `auth/service.py` (extend).** `_resolve_master_key`/`_is_development_environment`
move to read through `resolve_secret` and a single `Settings.environment`
(killing the `FORGE_ENV` vs `FORGE_ENVIRONMENT` and `SECRET_KEY` vs
`FORGE_SECRET_KEY` drift). `AuthService.__init__` builds a `KeyRing` and selects
`envelope_cipher(keyring)` when `FORGE_ENVELOPE_ENCRYPTION` is on. The dev
ephemeral fallback is gated behind an explicit `FORGE_DEV_INSECURE=1` flag (not
mere absence of `FORGE_ENVIRONMENT`) and logs a loud warning — so a misconfigured
prod can never *accidentally* land on the dev path.

**(f) `routers/auth.py` (extend).** `SecretExpiredError -> HTTP 409`; the
`/auth/secrets` list view already returns `expires_at` via `SecretInfo` — add a
computed `is_expired` field. New admin route `POST /auth/secrets/{id}/rotate`
(value rotation; RBAC `MANAGE_KEYS`).

**(g) `forge_api/cli/secrets.py` (new, module-runnable).** `rotate-kek`,
`sweep-expired`, and `check-config` (a preflight that asserts `FORGE_SECRET_KEY`
resolves, envelope is on, and no deprecated alias is in use) — runnable as
`python -m forge_api.cli.secrets <cmd>`; wired as the future `forge-cli secrets`
subcommand.

**(h) `observability/redaction.py` (extend) — structural redaction.**

- `class RedactingLogFilter(logging.Filter)` — scrubs `record.msg` + `record.args`
  via the existing `redact_text`/`redact_value`; idempotent and allocation-light.
- `install_log_redaction()` — attaches the filter to the root logger and the
  `uvicorn`, `uvicorn.access`, `gunicorn`, and `celery` loggers at app/worker
  startup.
- `class RedactingSpanProcessor` (in `observability/otel.py`) — an OTel
  `SpanProcessor` that redacts span attributes/events on `on_end` before export.

### 3.3 Worker/agent

Extends `apps/worker/forge_worker`:

- `celery_app.py` reads config through `resolve_secret`/`Settings` (no direct
  `os.environ`), and calls `install_log_redaction()` on worker startup
  (`worker_process_init`) so worker logs are scrubbed identically to the API.
- New beat task `auth.expire_secrets` (queue `default`, every 15 min) → calls
  `SecretVault.sweep_expired(purge=False)` and a metric counter; agent runs that
  resolve a BYOK key get `SecretExpiredError` surfaced as an escalation, not a
  silent fallback (ties into HARD-12's worker error-path coverage).
- The agent-runner key-mint path (used by `apps/worker` when it bootstraps a
  run-scoped token) sets a default `expires_at = now + FORGE_AGENT_TOKEN_TTL`
  (default 24h), satisfying the spec's "automatic expiry for agent tokens".

### 3.4 Frontend

Minimal (`apps/web`):

- `components/settings/secrets-list.tsx` — render `kind`, `provider`,
  `key_prefix`, `created_at`, and an **`expired`/`expires in N days`** badge from
  the `is_expired`/`expires_at` fields; a "Rotate" action (admin-only) POSTing to
  `/auth/secrets/{id}/rotate`.
- `lib/api/secrets.ts` — typed client matching the `SecretInfo` + rotate contract.
- No secret value is ever sent to or rendered in the client (only `key_prefix`).

### 3.5 Infra/deploy/CI

Extends `deploy/` and `.github/workflows/ci.yml`:

- **Compose drift fix.** `deploy/docker-compose.yml`: rename `FORGE_ENV` →
  `FORGE_ENVIRONMENT` and `SECRET_KEY` → `FORGE_SECRET_KEY` on the api/worker/
  mcp-gateway services; add `FORGE_SECRET_KEY: ${FORGE_SECRET_KEY:?set a stable
  FORGE_SECRET_KEY}` (compose `:?` fails the up if unset — fail-closed at the
  orchestration layer too). Support Docker secrets: add a `secrets:` block and
  `FORGE_SECRET_KEY_FILE: /run/secrets/forge_secret_key` as the recommended path.
- **`.env.example` / new `.env.production.example`.** Replace `SECRET_KEY`/
  `FORGE_ENV` with `FORGE_SECRET_KEY`/`FORGE_ENVIRONMENT`; add
  `FORGE_SECRET_KEY_VERSION`, `FORGE_SECRET_KEY_V1` (rotation),
  `FORGE_SECRET_PROVIDER`, `FORGE_ENVELOPE_ENCRYPTION`, `FORGE_DEV_INSECURE`,
  `FORGE_AGENT_TOKEN_TTL`, and Vault knobs. Ship `.env.integration.example`
  (names only) per the SPEC's credentials section.
- **`.dockerignore`.** Ensure `.env`, `.env.*` (except `*.example`), `*.pem`,
  `*.key`, and `deploy/secrets/` are excluded from every build context so secrets
  can never be baked into a layer. Add `deploy/secrets/` to `.gitignore` (today
  `*.pem`/`*.key`/`.env*` are covered but the directory is not explicitly listed).
- **CI.** New `secrets-config` job: (1) `python -m forge_api.cli.secrets
  check-config` against a sample prod-like env (asserts fail-closed + no
  deprecated aliases); (2) gitleaks over the repo *and* over each built image's
  filesystem/history (the image-secret scan, coordinated with HARD-08's build job);
  (3) the hermetic crypto/vault/provider unit suite. Dockerfiles must use runtime
  env/secret mounts only — a check asserts no secret-named `ARG`/`ENV` literal is
  baked in.

## 4. Public interfaces / contracts (exact signatures, env vars, config keys)

**Envelope blob format (`encrypted_secret` bytes).**

```
# v2 envelope (written by EnvelopeCipher.encrypt):
blob = b"\x02"                 # format version (2 = envelope)
     + kek_version (1 byte)    # which KeyRing KEK wrapped the DEK
     + wrapped_dek_len (2 bytes, big-endian)
     + wrapped_dek            # default_cipher(KEK).encrypt(base64(DEK))  (a v1 blob)
     + inner                  # default_cipher(DEK).encrypt(plaintext)   (a v1 blob)

# v1 legacy (still decryptable): blob[0] == 0x01 -> single-tier HmacAead/Fernet.
```

**`forge_api/auth/crypto.py` (added):**

```python
class EnvelopeCipher:
    def __init__(self, keyring: KeyRing, *, wrap: Callable[[bytes], SecretCipher] = default_cipher,
                 legacy: SecretCipher | None = None) -> None: ...
    def encrypt(self, plaintext: str) -> bytes: ...                      # writes v2
    def decrypt(self, blob: bytes) -> str: ...                            # reads v1 + v2
    def rewrap(self, blob: bytes, *, to_version: int | None = None) -> tuple[bytes, int]: ...

def envelope_cipher(keyring: KeyRing) -> SecretCipher: ...
```

**`forge_api/auth/keyring.py` (new):**

```python
class KeyRing:
    current_version: int
    def kek(self, version: int) -> bytes: ...
    def current_kek(self) -> bytes: ...
    def versions(self) -> list[int]: ...
    @classmethod
    def from_provider(cls, provider: SecretProvider, *, current_version: int | None = None,
                      require: bool = True) -> "KeyRing": ...
```

**`forge_api/auth/providers.py` (new):**

```python
@runtime_checkable
class SecretProvider(Protocol):
    name: str
    def get(self, key: str) -> str | None: ...

class EnvSecretProvider: ...      # os.environ + <KEY>_FILE indirection
class FileSecretProvider:         # default root = /run/secrets
    def __init__(self, root: Path = Path("/run/secrets")) -> None: ...
class VaultSecretProvider:        # hvac KV v2; integration-only
    def __init__(self, *, addr: str, token: str, mount: str, path: str, client=None) -> None: ...
class ChainSecretProvider:
    def __init__(self, providers: Sequence[SecretProvider]) -> None: ...

def build_provider(settings: "Settings") -> SecretProvider: ...
def resolve_secret(key: str, *, provider: SecretProvider | None = None) -> str | None: ...
```

**`forge_api/auth/vault.py` (added/changed):**

```python
class SecretExpiredError(SecretNotFoundError): ...

class SecretVault:
    def get_secret(self, workspace_id: UUID, secret_id: UUID, *, now: datetime | None = None) -> str: ...
    def rotate_secret(self, *, workspace_id: UUID, secret_id: UUID, new_secret: str,
                      expires_at: datetime | None = None) -> SecretInfo: ...
    def rewrap_all(self, *, keyring: KeyRing, to_version: int | None = None) -> dict[str, int]: ...
    def sweep_expired(self, *, now: datetime | None = None, purge: bool = False) -> int: ...
```

**`forge_api/observability/redaction.py` (added):**

```python
class RedactingLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool: ...   # mutates msg/args, returns True
def install_log_redaction(extra_loggers: Sequence[str] = ...) -> None: ...
```

**`SecretInfo` (extend, `auth/vault.py`):** add computed `is_expired: bool`.

**Config keys (all `FORGE_`-prefixed via `Settings`; resolvable as `<KEY>_FILE`):**

| Key | Default | Meaning |
|---|---|---|
| `FORGE_ENVIRONMENT` | `development` | canonical env; `production` requires a real key. (Replaces compose's `FORGE_ENV`.) |
| `FORGE_SECRET_KEY` | — (required in prod) | current master KEK (>= 16 bytes). (Replaces compose's `SECRET_KEY`.) |
| `FORGE_SECRET_KEY_FILE` | — | path to a file containing `FORGE_SECRET_KEY` (Docker/K8s secret). |
| `FORGE_SECRET_KEY_VERSION` | highest present | current KEK version int. |
| `FORGE_SECRET_KEY_V<n>` | — | previous KEK version `n` (kept during a rotation window). |
| `FORGE_ENVELOPE_ENCRYPTION` | `true` (prod) / `false` (dev) | use `EnvelopeCipher` vs single-tier. |
| `FORGE_SECRET_PROVIDER` | `env` | `env` \| `file` \| `vault`. |
| `FORGE_SECRET_FILE_ROOT` | `/run/secrets` | root for `FileSecretProvider`. |
| `FORGE_VAULT_ADDR` / `FORGE_VAULT_TOKEN` / `FORGE_VAULT_MOUNT` / `FORGE_VAULT_PATH` | — | HashiCorp Vault (integration). |
| `FORGE_DEV_INSECURE` | `false` | explicit opt-in to the dev ephemeral-key path. |
| `FORGE_AGENT_TOKEN_TTL` | `86400` (s) | default expiry for minted agent-runner tokens. |

**Back-compat aliases (one release, with deprecation warning):** `SECRET_KEY` →
`FORGE_SECRET_KEY`; `FORGE_ENV` → `FORGE_ENVIRONMENT`.

## 5. Dependencies (other slices/foundation that must exist first)

- **HARD-10 — Production crypto + OAuth seam (REQUIRED).** HARD-13 builds directly
  on HARD-10's `FernetCipher`-as-default and `FORGE_SECRET_KEY`-required work;
  `EnvelopeCipher` wraps `default_cipher` (Fernet) as its KEK/DEK primitive and
  the dev-insecure flag formalises HARD-10's dev fallback. HARD-10 must land first
  (`cryptography` re-locked, Fernet default).
- **HARD-01 — Real Postgres + pgvector substrate (REQUIRED for the rotation
  drill).** The `rewrap_all` / migration / data-preserving rotation acceptance
  criteria run against the Postgres-backed `api_key` store and the
  `postgres_url`/`pg_engine` conftest fixtures. The hermetic unit tests need only
  the in-memory store.
- **HARD-09 — Security audit (CONSUMER, soft).** HARD-09 ingests this slice's
  rotation runbook (`docs/self-hosting/security.md`), the `check-config`
  preflight, and the image secret scan into the security evidence pack and the
  enforcement matrix; the human pentest of this surface is HARD-09's punch-list.
- **HARD-08 — Container & web build (soft, for the image-secret scan).** The
  image-layer secret scan is wired alongside HARD-08's `docker compose build` job.
- **HARD-14 — Re-lock (soft, downstream).** Adds `hvac` (Vault provider) and any
  new deps to `uv.lock`; HARD-14 re-locks after HARD-13.
- **Foundation (already present):** `forge_api.auth.{crypto,vault,apikeys,service,oauth}`,
  `forge_api.settings`, `forge_api.observability.redaction`, `forge_db.models`
  `api_key` table, frozen `forge_contracts.enums.APIKeyKind`/`UserRole`, root
  `conftest.py` postgres fixtures, pytest markers `postgres` + `integration`.

## 6. Acceptance criteria (numbered, testable)

Marked **[offline]** (hermetic, no creds) or **[integration]** (gated; needs a
live Postgres or a Vault dev server — *no external SaaS creds*).

1. **[offline]** `EnvelopeCipher.encrypt` writes a `\x02` blob carrying a
   per-message DEK wrapped under the current KEK; `decrypt` round-trips it; two
   encryptions of the same plaintext yield different blobs (fresh DEK + IV).
2. **[offline]** `EnvelopeCipher.decrypt` still decrypts a legacy `\x01`
   single-tier blob (HARD-10 existing-data path), proving zero-downtime upgrade.
3. **[offline]** `rewrap(blob, to_version=2)` produces a blob that (a) decrypts to
   the same plaintext, (b) reports `kek_version == 2`, and (c) leaves the inner
   data ciphertext **byte-identical** (the DEK, not the data, was re-wrapped).
4. **[offline]** A tampered envelope (flip a byte in the wrapped DEK *or* the inner
   ciphertext) raises `InvalidTokenError` — no plaintext, uniform error.
5. **[offline]** With `FORGE_ENVIRONMENT=production` and `FORGE_SECRET_KEY` unset,
   `AuthService()` (and the worker/mcp-gateway bootstrap) raise at construction —
   no ephemeral key, no boot. With `FORGE_DEV_INSECURE` unset, *no* environment
   string can reach the ephemeral path; only `FORGE_DEV_INSECURE=1` enables it
   (and warns).
6. **[offline]** Config-drift regression: a config-loaded-from the *shipped*
   compose env (`FORGE_ENV=production`, `SECRET_KEY=…`, no `FORGE_ENVIRONMENT`/
   `FORGE_SECRET_KEY`) is detected — the alias shim maps them and the app boots in
   production mode with the provided key (and warns), and a test asserts the
   *post-fix* compose uses the canonical names with `:?` fail-closed.
7. **[offline]** `vault.get_secret` raises `SecretExpiredError` for a record whose
   `expires_at <= now` (injected clock) and returns plaintext when not expired;
   `raw_record` still returns the (expired) ciphertext for rotation.
8. **[offline]** `SecretInfo.is_expired` reflects `expires_at`; the
   `/auth/secrets` list and the `secrets-list.tsx` test render an `expired` badge;
   resolving an expired BYOK key over the API yields HTTP 409.
9. **[offline]** A minted agent-runner token gets `expires_at ≈ now +
   FORGE_AGENT_TOKEN_TTL`; `apikeys.verify` rejects it after expiry (spec
   "automatic expiry for agent tokens").
10. **[offline]** `EnvSecretProvider` resolves `FOO` from `FOO` *and* from a
    `FOO_FILE` path; `FileSecretProvider` reads `/run/secrets/FOO`;
    `ChainSecretProvider` returns the first non-`None`; `resolve_secret` is the
    single ingress used by `settings`, `service`, `oauth`, and the worker (asserted
    by patching the provider and observing all four resolve through it).
11. **[offline]** `RedactingLogFilter` scrubs a secret passed through
    `logger.info("key=%s", "sk-deadbeef…")`: the emitted record contains
    `[REDACTED]` and no secret substring; the filter is installed on root +
    uvicorn + celery loggers by `install_log_redaction`.
12. **[offline]** `RedactingSpanProcessor` redacts a span attribute containing a
    bearer token before export (captured via an in-memory span exporter).
13. **[offline]** No real secret value appears anywhere in source, fixtures,
    snapshots, or the lockfile (gitleaks over the tree is clean in CI); test creds
    are obvious fakes.
14. **[integration · Postgres]** Rotation drill on a populated `api_key` table
    (HARD-01 DB): seed N secrets under KEK v1, run `rewrap-kek` to v2; **every**
    BYOK plaintext still decrypts, `key_version` is 2 for all rows, the data
    ciphertext is preserved, and a v1-only env (old KEK removed) still reads them.
15. **[integration · Postgres]** The additive migration `upgrade head` →
    `downgrade` → `upgrade head` is data-preserving on a populated DB (rows keep
    decrypting), with the paired code-rollback note exercised.
16. **[integration · Vault]** `VaultSecretProvider` reads `FORGE_SECRET_KEY` from a
    Vault dev server KV path and the app boots in production mode against it; the
    test **skips cleanly** when no Vault is configured.
17. **[offline]** `python -m forge_api.cli.secrets check-config` exits non-zero on
    a missing key / deprecated alias / envelope-off-in-prod and zero on a valid
    prod config; wired as a blocking CI `secrets-config` step.
18. **[offline]** Whole-suite green gate: `uv run pytest -q`,
    `uv run ruff check .`, `uv run ruff format --check .`, `make typecheck` (exit
    0), and `cd apps/web && pnpm test` all pass at the end of the workstream;
    integration tests skip cleanly when their substrate is absent.

## 7. Test plan (TDD) — unit + integration (gated on env creds) + how to run

Write tests first. Hermetic tests use the in-memory `InMemorySecretStore` and
injected `KeyRing`s/clocks — no network, no creds. Integration tests use the
`postgres_url`/`pg_engine` conftest fixtures (HARD-01) and a Vault dev server.

**Unit (offline) — `apps/api/tests/auth/`:**
- `test_crypto_envelope.py` — AC1–4: round-trip, legacy `\x01` decrypt, `rewrap`
  preserves inner ciphertext + bumps version, tamper → `InvalidTokenError`.
- `test_keyring.py` — `from_provider` reads current + `V<n>`; `current_version`
  selection; missing-key behaviour by env.
- `test_providers.py` — AC10: env, `_FILE` indirection, file root, chain
  precedence, `resolve_secret` is the single ingress (patch + assert).
- `test_service_secret_key.py` — AC5/6: prod refuses to boot without a key; only
  `FORGE_DEV_INSECURE` enables ephemeral; alias shim + warning; canonical-name
  assertion against the fixed compose env.
- `test_vault_expiry.py` — AC7/8: `SecretExpiredError`, `is_expired`,
  `rotate_secret`, `sweep_expired`.
- `test_agent_token_ttl.py` — AC9.
- `test_log_redaction.py` / `test_span_redaction.py` — AC11/12 (caplog +
  in-memory OTel exporter).
- `test_cli_secrets.py` — AC17: `check-config` exit codes.

**Integration (gated) — `apps/api/tests/auth/test_rotation_integration.py`
(`@pytest.mark.postgres`) and `test_vault_provider_integration.py`
(`@pytest.mark.integration`):** AC14–16.

**Migration test — `packages/db/tests/test_migration.py` (extend):** AC15 on
SQLite (offline, structural) and Postgres (gated) for upgrade/downgrade/upgrade.

**Web — `apps/web/src/components/settings/secrets-list.test.tsx`:** AC8 badge +
admin-only rotate (Vitest).

**How to run:**
```bash
# Hermetic (default; no creds, no network):
uv run pytest apps/api/tests/auth -q
uv run ruff check . && make typecheck
cd apps/web && pnpm test

# Rotation/migration drill (needs live pgvector — HARD-01):
export FORGE_TEST_DATABASE_URL=postgresql+psycopg://forge:forge@localhost:5432/forge_test
uv run pytest -m postgres apps/api/tests/auth/test_rotation_integration.py -q

# Vault provider (needs a Vault dev server; skips otherwise):
export FORGE_VAULT_ADDR=http://127.0.0.1:8200 FORGE_VAULT_TOKEN=dev
uv run pytest -m integration apps/api/tests/auth/test_vault_provider_integration.py -q

# Config preflight (CI gate):
FORGE_ENVIRONMENT=production FORGE_SECRET_KEY=$(python -c 'import secrets;print(secrets.token_urlsafe(32))') \
  python -m forge_api.cli.secrets check-config
```

## 8. Security & policy considerations

- **Envelope encryption (defence in depth).** A per-secret DEK limits blast radius
  (one compromised DEK ≠ all secrets) and makes KEK rotation O(rows) cheap
  re-wraps instead of full re-encryption. The KEK never touches a data row; only
  wrapped DEKs do.
- **Fail-closed by construction.** Production with no `FORGE_SECRET_KEY` refuses to
  boot at three layers: the app (`AuthService`), the orchestrator (compose `:?`),
  and CI (`check-config`). The dev ephemeral path requires an *explicit* opt-in
  flag, so misconfiguration cannot silently downgrade security (this is the
  concrete fix for the Intent §1.1 drift bug).
- **No secret in logs/traces/images — guaranteed structurally.** Redaction is a
  logging `Filter` + OTel `SpanProcessor` (sink-level, not call-site), plus a
  `.dockerignore` + build-time image-layer secret scan, so an accidental
  `logger.info(secret)` or a baked `.env` is caught regardless of developer
  discipline. The existing `observability/redaction.py` pattern set is the single
  source of truth (no divergence).
- **Least-knowledge ingress.** All secrets resolve at call time through
  `resolve_secret`; the KEK and BYOK plaintext are never held in module globals,
  never serialised in `SecretInfo`/`repr`, and never returned by list views.
- **Rotation hygiene.** Old KEK versions are retained only for a documented
  rotation window (runbook), then removed; `rotated_at`/`key_version` give an audit
  trail. Value rotation (`rotate_secret`) and KEK rotation (`rewrap_all`) are
  distinct and separately auditable.
- **Expiry enforcement** closes the gap between the spec's "automatic expiry" and
  the code: both API keys (already) and vaulted BYOK secrets (new) are time-bound;
  agent tokens get a default TTL.
- **Vault provider** keeps the master key out of the env entirely for operators who
  want it, without changing the app surface.
- **Policy:** secret files (`.env*`, `*.pem`, `*.key`, `secrets/**`) remain on the
  FORGE_SPEC policy deny/exclude list (line 366) so the knowledge indexer never
  ingests them; HARD-13 adds the deploy-context `.dockerignore` mirror.

## 9. Effort & risk (S/M/L + risks)

**Effort: M.** Crypto envelope + keyring (S–M), provider abstraction (S), vault
expiry/rotation (S), config reconciliation + compose/.env fixes (S), structural
redaction (S), CLI + CI wiring (S), tests (M). The bulk is careful, well-tested
code over an existing surface — not new architecture.

Risks:
- **Rotation correctness on live data (High impact, Medium likelihood).** A buggy
  `rewrap` could brick stored secrets. Mitigation: `rewrap` never touches the data
  ciphertext (only re-wraps the DEK), AC3/14 assert byte-identical inner blobs,
  the drill runs on a *populated* DB before any runbook step, and the runbook
  mandates a DB backup first.
- **Legacy-blob migration (Medium).** Existing `\x01` rows must keep decrypting
  through the transition. Mitigation: `EnvelopeCipher` decrypts both formats; the
  first rotation upgrades them; AC2 guards it.
- **Config alias removal (Low/Medium).** Renaming `SECRET_KEY`/`FORGE_ENV` could
  break an existing operator. Mitigation: one-release alias shim with a loud
  deprecation warning + a documented cutover; the compose/`.env` examples lead.
- **Cross-service config duplication (Low).** Worker/mcp-gateway currently lack a
  settings surface; HARD-13 routes them through `forge_api.settings`/
  `resolve_secret` rather than adding a parallel package (honours "extend, no
  duplicate packages").

**Cannot be done in-sandbox (named, not hidden):**
- A **3rd-party human penetration test** of the secret surface (KEK handling,
  Vault transit, side channels) — stays a HARD-09 punch-list item.
- The **image-layer secret scan** and the **Vault-provider integration** need a
  networked/CI runner (image build) and a Vault dev server respectively; the
  rotation/migration drill needs a live pgvector container (HARD-01). All three
  skip cleanly on the hermetic path.
- A **real HSM/KMS-backed KEK** (AWS KMS / GCP KMS) is out of scope (future, §12);
  HARD-13 ships the env/file/Vault providers and a clean seam for it.

## 10. Key files / paths (exact, in the real monorepo)

- `apps/api/forge_api/auth/crypto.py` — extend: `EnvelopeCipher`, `envelope_cipher`.
- `apps/api/forge_api/auth/keyring.py` — **new**: `KeyRing`.
- `apps/api/forge_api/auth/providers.py` — **new**: `SecretProvider` + impls + `resolve_secret`.
- `apps/api/forge_api/auth/vault.py` — extend: `SecretExpiredError`, expiry in `get_secret`, `rotate_secret`, `rewrap_all`, `sweep_expired`, `SecretInfo.is_expired`.
- `apps/api/forge_api/auth/service.py` — extend: keyring + envelope selection, single env/key ingress, `FORGE_DEV_INSECURE` gate.
- `apps/api/forge_api/auth/__init__.py` — export new symbols.
- `apps/api/forge_api/settings.py` — extend: `secret_key`, `secret_key_version`, `secret_provider`, `envelope_encryption`, `dev_insecure`, `agent_token_ttl`, Vault knobs, alias shim.
- `apps/api/forge_api/routers/auth.py` — extend: 409 mapping, `is_expired`, `POST /auth/secrets/{id}/rotate`.
- `apps/api/forge_api/cli/secrets.py` — **new**: `rotate-kek`, `sweep-expired`, `check-config`.
- `apps/api/forge_api/observability/redaction.py` — extend: `RedactingLogFilter`, `install_log_redaction`.
- `apps/api/forge_api/observability/otel.py` — extend: `RedactingSpanProcessor`.
- `apps/worker/forge_worker/celery_app.py` — extend: config via `resolve_secret`, `install_log_redaction`, `auth.expire_secrets` beat task, agent-token TTL.
- `packages/db/forge_db/models/workspace.py` — extend `APIKey`: `key_version`, `rotated_at`.
- `packages/db/migrations/versions/0019_envelope_key_version.py` — **new** additive migration.
- `apps/web/src/components/settings/secrets-list.tsx`, `apps/web/src/lib/api/secrets.ts` — **new**.
- `deploy/docker-compose.yml` — fix env names, add `secrets:` + `:?` fail-closed.
- `.env.example`, `deploy/.env.production.example` (**new**), `.env.integration.example` (**new**), `.dockerignore` (extend), `.gitignore` (add `deploy/secrets/`).
- `.github/workflows/ci.yml` — add `secrets-config` job (check-config + gitleaks + image scan).
- `docs/self-hosting/security.md` — credential + KEK rotation runbook (consumed by HARD-09).
- Tests: `apps/api/tests/auth/{test_crypto_envelope,test_keyring,test_providers,test_service_secret_key,test_vault_expiry,test_agent_token_ttl,test_log_redaction,test_span_redaction,test_cli_secrets,test_rotation_integration,test_vault_provider_integration}.py`; `packages/db/tests/test_migration.py` (extend); `apps/web/src/components/settings/secrets-list.test.tsx`.

## 11. Research references

- FORGE_SPEC.md → Security table ("Secrets — Encrypted at rest, per-workspace
  isolation, automatic expiry for agent tokens"; "Secret redaction"); "Production
  Docker Compose Requirements" (run as non-root, no secrets in images);
  "Required Self-Hosting Documentation" → `security.md` (credential rotation).
- MORNING_REPORT.md → §5(6) crypto backend (Fernet seam), §5(7) `FORGE_SECRET_KEY`
  ephemeral fallback `# PARKED-FOR-PROD`, §6 (provider/transport realism).
- SPEC-PRODUCTION-HARDENING.md → HARD-10 (G-CRYPTO), HARD-09 (G-SEC-EVIDENCE,
  rotation runbook), Credentials & secrets handling §1–7.
- The Twelve-Factor App, factor III (Config in the environment):
  https://12factor.net/config
- Envelope encryption (DEK/KEK) — AWS KMS concept docs:
  https://docs.aws.amazon.com/kms/latest/developerguide/concepts.html#enveloping
- `cryptography` Fernet spec: https://cryptography.io/en/latest/fernet/
- RFC 8705 / OAuth secret handling, OWASP Secrets Management Cheat Sheet:
  https://cheatsheetseries.owasp.org/cheatsheets/Secrets_Management_Cheat_Sheet.html
- Docker secrets & `_FILE` convention; HashiCorp Vault KV v2 / `hvac`:
  https://docs.docker.com/engine/swarm/secrets/ ·
  https://hvac.readthedocs.io/
- gitleaks (repo + image secret scanning): https://github.com/gitleaks/gitleaks

## 12. Out of scope / future

- **HSM/KMS-backed KEK** (AWS KMS / GCP KMS / Vault Transit as the *wrapping*
  authority, so the KEK never leaves the HSM) — HARD-13 ships the provider seam and
  the envelope format that make this a drop-in; the KMS integration itself is V2.
- **Automatic scheduled KEK rotation** (cron-driven, zero-touch) — V1 ships the
  `rotate-kek` command + runbook; scheduling it is operator policy / future.
- **Per-tenant KEKs** (a distinct KEK per workspace rather than per-instance) —
  the envelope design allows it; not required for V1's per-workspace *isolation*
  (already enforced at the store boundary).
- **Secret leasing / dynamic secrets** (short-lived DB creds via Vault) — V2.
- **The human penetration test** of the secret surface — owned by HARD-09's
  punch-list; cannot be performed by build agents.
- **Better Auth / Auth.js session-secret unification** with `FORGE_SECRET_KEY`
  (the web `AUTH_SECRET` in `.env.example`) — noted for a follow-up; HARD-13
  scopes the backend BYOK/KEK path, not the web session layer.
</content>
</invoke>
