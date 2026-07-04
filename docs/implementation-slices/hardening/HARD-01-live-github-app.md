# HARD-01 — Live GitHub App Integration

> Phase: hardening · Blocker(s): #1 (no real external systems exercised) · Status target: **Verified** = the real Forge GitHub App is driven end-to-end against a disposable test repository — a JWT is minted from the env-only private key, exchanged for a short-lived installation access token, a branch is pushed, a PR is opened with spec-traceability, review comments are read back, and a real CI/webhook delivery's HMAC is verified — all behind `@pytest.mark.integration` and gated on `FORGE_GITHUB_*` creds. The production client adds JWT+installation-token auth, retry/backoff, secondary-rate-limit handling, `Link`-header pagination, structured audit, and secret redaction. **This is the only HARD-NN that maps directly to G-GH; it requires real creds and a networked runner, and SKIPS cleanly when creds are absent so the hermetic default suite stays green and network-free.**

---

## 1. Intent — what & why

Per `docs/MORNING_REPORT.md` §1.13, §5(4), and §6 ("Provider/transport realism"), the entire GitHub surface — `forge_integrations.github.GitHubClient`, `forge_integrations.webhooks`, and the `apps/api` integration router — is exercised **only against recorded JSON fixtures via `httpx.MockTransport`**. No real GitHub call has ever happened. The client also assumes a caller supplies a ready-made `token`; there is **no JWT minting, no installation-token exchange, no retry/backoff, no rate-limit handling, and no pagination**. That is exactly release blocker #1.

HARD-01 closes the GitHub half of blocker #1 (the BETA gate **G-GH** in `SPEC-PRODUCTION-HARDENING.md` §"BETA bar" item 4). It does **not** rebuild the GitHub feature (that is V1 slice `docs/implementation-slices/v1/F03-github-app.md`, already DONE-against-fixtures). Instead it **extends the existing `forge_integrations` package** so the same `GitHubClient` works against the real api.github.com using the real Forge GitHub App, and proves it on a live test repo:

1. **App auth that GitHub actually accepts** — mint an `RS256` JWT from the App private key (loaded from the env-only `.pem` path, never the value), exchange it for a per-installation access token via `POST /app/installations/{id}/access_tokens`, cache it until just before expiry, and send it as `Authorization: Bearer <installation_token>` on every data-plane call.
2. **Production resilience** — retry with exponential backoff + jitter on 5xx and GitHub *secondary* rate limits (`403`/`429` with `Retry-After`), honor `X-RateLimit-Remaining`/`X-RateLimit-Reset`, and paginate via the `Link: rel="next"` header so `sync_repo` and review reads do not silently truncate.
3. **End-to-end real exercise** — on a disposable test repo: create a branch + push a file via the Git Data API, `open_pr` with a spec-traceability body, read back review comments, then fetch a **real webhook delivery** from the App's delivery log and verify its `X-Hub-Signature-256` HMAC against the configured secret (and prove a tampered body is rejected).
4. **Audit + redaction on the live path** — every GitHub operation emits a redacted, hash-chained audit entry through the existing `forge_api.observability.audit.AuditLog`; no token, JWT, or `.pem` material ever reaches logs, audit rows, traces, or error messages.

The product surface (the four V1 capabilities: repo sync, PRs, CI, reviews/webhooks) is unchanged — HARD-01 proves that surface against reality, per the spec's "Hardening does not change the product surface — it proves the surface against reality."

## 2. User-facing / operator behavior

HARD-01 is operator-facing (a self-hoster wiring real GitHub) and CI-facing (the integration lane). Observable behavior:

- **Operator J1 — Configure the App.** An operator creates a GitHub App (or uses Forge's published one), installs it on a test org/repo, downloads the private key, and drops it at `deploy/secrets/github-app.pem` (gitignored). They set `FORGE_GITHUB_APP_ID`, `FORGE_GITHUB_INSTALLATION_ID`, `FORGE_GITHUB_WEBHOOK_SECRET`, and (for the test lane) `FORGE_GITHUB_TEST_REPO` in a gitignored `.env.integration`. No token is pasted anywhere — Forge mints its own.
- **Operator J2 — Connection health.** `GET /integration/github/health` (or the existing `GitHubClient.health()` via `/rate_limit`) returns `healthy:true` with live `latency_ms` once the App can mint an installation token — turning a previously-mocked check into a real reachability probe. If the `.pem` is missing or the App id is wrong, it returns `healthy:false` with a redacted reason (never the key).
- **Operator J3 — Real PR round-trip.** With creds present, the integration lane pushes `forge/hardening-smoke-<runid>` to the test repo, opens a PR titled `[forge-hardening] smoke <runid>` carrying a spec-traceability section, asserts the returned `PullRequest.number`/`url`, optionally requests a reviewer, reads back review comments, and then **cleans up** (closes the PR, deletes the branch) so the test repo is left pristine and idempotent across reruns.
- **Operator J4 — Webhook trust boundary.** The existing `POST /integration/github/webhooks` route stays fail-closed (HMAC-only trust). HARD-01 proves it against a **real** delivery: it pulls the App's most recent delivery from `GET /app/hook/deliveries`, replays the exact bytes + `X-Hub-Signature-256` header at the route, and asserts a `200` parsed `CIStatus`; a one-byte-tampered body yields `401`.
- **CI behavior.** The integration lane is a separate, opt-in job that only runs where `FORGE_GITHUB_*` secrets exist (e.g. a protected CI context). On PRs from forks / without secrets, every HARD-01 test **skips with a clear reason** and the suite stays green — the default `uv run pytest -q` run is unchanged and network-free.

## 3. Vertical slice

### 3.1 Data model

No new tables and **no migration** — HARD-01 is auth/transport hardening over the existing GitHub surface. It conforms to the frozen `forge_contracts` DTOs (`RepositoryConnection`, `PullRequest`, `PullRequestRequest`, `RepoSyncResult`, `CIStatus`, `WebhookEvent`, `HealthResult`) unchanged.

Two non-schema, additive touches:

1. **`RepositoryConnection.metadata` usage (existing JSON field, no migration).** The installation id already lives in the frozen `RepositoryConnection.installation_id: str | None`; `metadata["last_synced_sha"]` is already used by `sync_repo`. HARD-01 additionally records `metadata["installation_token_expires_at"]` only **in process memory** (the token cache), never persisted — the token never touches the DB.
2. **Audit category (internal enum, not a frozen contract).** Extend `apps/api/forge_api/observability/audit.py::AuditCategory` with `INTEGRATION = "integration"` for GitHub/Slack/PM operations. `AuditEntry` is stored via the existing append-only, hash-chained store; on real Postgres (HARD-01's sibling DB substrate) it persists through the same `AuditStore` boundary and is subject to the immutability trigger. The DB column is a `str` enum (`native_enum=False`) per the foundation rule, so adding a value needs no DB migration.

### 3.2 Backend

**Extend `packages/integration-sdk/forge_integrations/` (no new package).**

New module `forge_integrations/github_auth.py` — the App-auth layer the fixture client never had:

```python
# forge_integrations/github_auth.py
from collections.abc import Callable

def load_private_key(path: str) -> str:
    """Read a PEM private key from a file path. The key VALUE is never logged
    and never returned in any error message (only the path, on read failure)."""

def build_app_jwt(
    app_id: str, private_key_pem: str, *, now: int | None = None, ttl_seconds: int = 540
) -> str:
    """RS256 App JWT. Claims: iat=now-60 (clock-skew), exp=now+ttl (capped at 600s
    per GitHub), iss=app_id. Signed with PyJWT[crypto]."""

class InstallationTokenProvider:
    """Mints + caches a short-lived installation access token.

    token() returns a cached token until ~60s before expiry, then re-mints the
    App JWT and calls POST /app/installations/{installation_id}/access_tokens.
    Thread-safe (lock around refresh). Never logs the JWT or the token."""
    def __init__(self, *, app_id: str, private_key_pem: str, installation_id: str,
                 base_url: str = DEFAULT_BASE_URL,
                 transport: httpx.BaseTransport | None = None,
                 clock: Callable[[], float] = time.time) -> None: ...
    def token(self) -> str: ...
    def invalidate(self) -> None: ...  # force re-mint (used on 401 retry)
```

**Extend `forge_integrations/github.py::GitHubClient`** (additive — the frozen `forge_contracts.GitHubClient` Protocol surface `sync_repo`/`open_pr`/`request_reviews`/`parse_webhook` is unchanged; new methods are concrete additions):

- `classmethod from_app(*, app_id, private_key_pem, installation_id, base_url=..., transport=None, retry=..., audit_sink=None) -> GitHubClient` — wires an `InstallationTokenProvider` and a `token_provider` callable so each request fetches a fresh (cached) installation token instead of a static `token`. The existing static-`token` constructor stays for fixtures/tests.
- Rework the existing private `_request` (same signature) to:
  - inject `Authorization: Bearer <token_provider()>` per call when an App provider is configured;
  - apply a `_RetryPolicy` (max attempts, base delay, jitter) that retries on `>=500`, on `403`/`429` carrying `Retry-After` or `X-RateLimit-Remaining: 0` (sleep until `X-RateLimit-Reset`, bounded), and on a single `401` after `provider.invalidate()` (token rotation);
  - emit one audit event per terminal outcome through the injected `audit_sink`.
- New `_paginate(method, path, *, params, items_key=None) -> Iterator[dict]` following `Link: rel="next"`; `sync_repo`'s tree/compare reads and review-comment reads use it so large repos/PRs are not truncated.
- New data-plane primitives (concrete; used by the integration lane and by F08/F06 in production):
  - `create_branch(repo: str, *, new_branch: str, from_ref: str = "main") -> str` — resolves the base ref SHA (`GET /repos/{r}/git/ref/heads/{from_ref}`) and creates `refs/heads/{new_branch}` (`POST /repos/{r}/git/refs`).
  - `push_files(repo: str, *, branch: str, files: dict[str, str], message: str, base_ref: str = "main") -> str` — Git Data API path: create blobs → tree → commit → update ref; returns the new commit SHA. (No local git needed → testable under `MockTransport` and runnable in a thin CI runner.)
  - `list_review_comments(pr: PullRequest) -> list[ReviewComment]` and `list_reviews(pr) -> list[Review]` — paginated reads of `GET /repos/{r}/pulls/{n}/comments` and `/reviews`, normalized to small local dataclasses (no frozen-contract change).
  - `close_pr(pr: PullRequest) -> PullRequest` and `delete_branch(repo, branch) -> None` — for test cleanup + the J5 disconnect path.

New module `forge_integrations/audit.py` (framework-agnostic, no `forge_api` import — preserves layering):

```python
@dataclass(frozen=True)
class GitHubAuditEvent:
    action: str                 # "mint_token" | "open_pr" | "push_files" | "sync_repo" | ...
    repo: str | None
    status: str                 # "ok" | "error"
    status_code: int | None
    latency_ms: int | None
    payload_hash: str | None    # sha256 of request body, never the body
    detail: str | None          # already free of secrets

AuditSink = Callable[[GitHubAuditEvent], None]
```

**Extend `apps/api/forge_api/routers/integration.py`** (the wiring layer that owns redaction + the real audit log):

- Replace `_github_client_singleton()` so that **when App creds are configured** it builds `GitHubClient.from_app(app_id=settings.github_app_id, private_key_pem=load_private_key(settings.github_app_private_key_path), installation_id=settings.github_installation_id, base_url=settings.github_api_url, audit_sink=<api sink>)`; it falls back to the static-`token` client only when `github_token` is set and App creds are not (dev/back-compat). If neither is configured the dependency raises `501 Not Configured` for write routes (fail-closed), never a silent fake.
- Provide the API `audit_sink`: a thin adapter that maps `GitHubAuditEvent` → `AuditLog.record(category=AuditCategory.INTEGRATION, action=..., target=repo, connection_id=..., status=..., payload_hash=..., latency_ms=..., metadata=redact_mapping({...}))`. `detail` is run through `redact_text`. This is where the existing redaction filter (`forge_api.observability.redaction`) is the single source of truth.
- Add `GET /integration/github/health` returning `HealthResult` (mints a token + hits `/rate_limit`), gated `Permission.READ`.
- The existing `POST /integration/github/webhooks` route is unchanged in contract; HARD-01 only adds tests that drive it with a **real** delivery's bytes+signature.

### 3.3 Worker/agent

No new Celery tasks. The existing `apps/worker` GitHub-driven flows (incremental knowledge sync on `push`, PR-open on `verifying → pr_opened`) call the **same** `forge_integrations.GitHubClient` via the API/service layer, so they inherit App-auth + retry + pagination transparently. HARD-01 adds one worker-side assertion to the integration lane: an agent-runner-initiated `open_pr` on the test repo uses the App token (not a static PAT) and produces an audit row with `actor` set to the run's agent identity and `payload_hash` present. No agent-runtime/LangGraph change.

### 3.4 Frontend

Minimal. The Settings → Integrations → GitHub panel (owned by F03/F08) gains one operator-visible signal sourced from `GET /integration/github/health`: a connection badge (`reachable` / `unreachable` / `not configured`) and the installation account login. No new pages; `apps/web/lib/api/integration.ts` adds the typed `getGithubHealth()` call. The token/`.pem` are never sent to or rendered by the frontend.

### 3.5 Infra/deploy/CI

- **Secrets at rest.** Create `deploy/secrets/` for the env-only `github-app.pem`. `.gitignore` already ignores `*.pem`, `.env`, and `.env.*` (verified); add an explicit `deploy/secrets/` line as defense-in-depth so nothing under it is ever staged. Ship `deploy/secrets/README.md` (no secrets) describing the expected files. Add `.env.integration.example` at repo root (key names only, no values).
- **Compose.** `deploy/docker-compose.yml` `api`/`worker` services mount `deploy/secrets/github-app.pem` read-only and read `FORGE_GITHUB_*` from env; the key is a **file mount**, never an env value. (Image digest pinning itself is HARD-08; HARD-01 only adds the secret mount + env passthrough.)
- **CI.** Add a new, opt-in job `integration-github` to `.github/workflows/ci.yml` that runs `uv run pytest -m integration -k github` **only** when `secrets.FORGE_GITHUB_APP_PRIVATE_KEY` (+ app id, installation id, webhook secret, test repo) are present (`if: ${{ secrets.FORGE_GITHUB_APP_ID != '' }}`). It writes the secret to `deploy/secrets/github-app.pem` at runtime, runs the lane, and never echoes it. The default `test` job is unchanged and stays network-free. CI logs are scrubbed (no `set -x` around secret writes; `add-mask`).

## 4. Public interfaces / contracts

**Frozen contracts (unchanged, conformed to):** `forge_contracts.GitHubClient` Protocol (`sync_repo`, `open_pr`, `request_reviews`, `parse_webhook`), and DTOs `RepositoryConnection`, `PullRequest`, `PullRequestRequest`, `RepoSyncResult`, `CIStatus`, `WebhookEvent`, `HealthResult`. No edits to `packages/contracts`.

**New public functions/classes (additive, in `forge_integrations`):**

```python
# forge_integrations/github_auth.py
def load_private_key(path: str) -> str
def build_app_jwt(app_id: str, private_key_pem: str, *, now: int | None = None, ttl_seconds: int = 540) -> str
class InstallationTokenProvider:
    def __init__(self, *, app_id, private_key_pem, installation_id, base_url=DEFAULT_BASE_URL, transport=None, clock=time.time)
    def token(self) -> str
    def invalidate(self) -> None

# forge_integrations/github.py (additions on GitHubClient)
@classmethod
def from_app(cls, *, app_id: str, private_key_pem: str, installation_id: str,
             base_url: str = DEFAULT_BASE_URL, transport: httpx.BaseTransport | None = None,
             retry: RetryPolicy | None = None, audit_sink: AuditSink | None = None) -> "GitHubClient"
def create_branch(self, repo: str, *, new_branch: str, from_ref: str = "main") -> str
def push_files(self, repo: str, *, branch: str, files: dict[str, str], message: str, base_ref: str = "main") -> str
def list_review_comments(self, pr: PullRequest) -> list[ReviewComment]
def list_reviews(self, pr: PullRequest) -> list[Review]
def close_pr(self, pr: PullRequest) -> PullRequest
def delete_branch(self, repo: str, branch: str) -> None

# forge_integrations/github.py (retry config)
@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 4
    base_delay_s: float = 0.5
    max_delay_s: float = 30.0
    jitter: bool = True
    retry_statuses: frozenset[int] = frozenset({500, 502, 503, 504})

# forge_integrations/audit.py
@dataclass(frozen=True)
class GitHubAuditEvent: ...
AuditSink = Callable[[GitHubAuditEvent], None]
```

`webhooks.py` is reused unchanged: `sign_github_payload`, `verify_github_signature`, `parse_github_webhook` (HARD-01 adds no new webhook parsing; it proves the existing verifier on real bytes).

**New `apps/api` settings (Pydantic `Settings`, `FORGE_` env prefix — authoritative resolved names):**

| Setting field | Env var | Default | Purpose |
|---|---|---|---|
| `github_app_id` | `FORGE_GITHUB_APP_ID` | `None` | GitHub App id (the JWT `iss`). |
| `github_app_private_key_path` | `FORGE_GITHUB_APP_PRIVATE_KEY_PATH` | `deploy/secrets/github-app.pem` | Path to the `.pem` (value loaded at call time, never stored). |
| `github_installation_id` | `FORGE_GITHUB_INSTALLATION_ID` | `None` | Installation to mint tokens for. |
| `github_test_repo` | `FORGE_GITHUB_TEST_REPO` | `None` | `owner/repo` used **only** by the integration lane. |
| `github_webhook_secret` (existing) | `FORGE_GITHUB_WEBHOOK_SECRET` | `None` | HMAC secret; route stays fail-closed when unset. |
| `github_api_url` (existing) | `FORGE_GITHUB_API_URL` | `https://api.github.com` | Override for GHE. |

> Reconciliation note: `SPEC-PRODUCTION-HARDENING.md` lists shorthand names `GITHUB_APP_ID` / `GITHUB_APP_PRIVATE_KEY_PATH` / `GITHUB_WEBHOOK_SECRET` / `GITHUB_TEST_REPO`. Because `apps/api` `Settings` uses `env_prefix="FORGE_"`, the **authoritative** vars are the `FORGE_`-prefixed names above; `.env.integration.example` documents them. The integration test harness reads the same `FORGE_`-prefixed vars (or the bare `.pem` path) so there is one source of truth.

**New dependency:** add `pyjwt[crypto]>=2.8` to `packages/integration-sdk/pyproject.toml` (pulls in `cryptography`, already an `apps/api` dep at `>=43`). Re-lock is owned by HARD-14; HARD-01 declares the dep.

**Vault binding (BYOK, optional alt path):** per-workspace App private keys may be stored encrypted under `APIKeyKind.INTEGRATION_TOKEN` in `forge_api.auth.vault.SecretVault` and resolved at call time; the env-only `.pem` path is the default for self-host single-tenant. Either way the key is resolved per request and discarded — never a module global, never logged.

## 5. Dependencies

- **`docs/implementation-slices/v1/F03-github-app.md` (DONE-against-fixtures)** — supplies `GitHubClient`, `webhooks.py`, the integration router, `RepositoryConnection`. HARD-01 extends it; it does not rebuild it.
- **Foundation `forge_contracts` (frozen)** — the GitHub DTOs/Protocol HARD-01 conforms to.
- **`apps/api/forge_api/observability/{audit,redaction}.py` (DONE)** — `AuditLog`, `redact_text`/`redact_mapping`/`REDACTED` reused for the live-path audit/redaction.
- **`apps/api/forge_api/auth/vault.py` (DONE; hardened by HARD-10)** — only for the optional per-workspace BYOK key path; the default `.pem`-from-env path needs nothing from HARD-10. If the BYOK path is used, HARD-10 (real `FernetCipher` + required `FORGE_SECRET_KEY`) should land first so keys are encrypted with the production cipher.
- **HARD-01 (real Postgres substrate, sibling)** — *soft.* The integration-lane audit assertions pass against the in-memory `AuditStore`; running them against real Postgres (so the audit immutability trigger covers GitHub rows too) is preferred but not required for G-GH.
- **Real creds + networked runner** — a GitHub App, its `.pem`, an installation on a disposable test repo, and the webhook secret. Hard prerequisite for the live half (see §9).

## 6. Acceptance criteria

Each criterion notes whether it **runs offline** (hermetic default suite, `MockTransport`/no network) or **requires real creds** (integration lane).

1. **[offline]** `build_app_jwt(app_id, pem)` produces an `RS256` JWT whose decoded claims have `iss == app_id`, `exp - iat <= 600`, and `iat <= now`; verifying with the matching public key succeeds and with a wrong key fails. (Uses a throwaway test keypair generated in-test — not the real App key.)
2. **[offline]** `InstallationTokenProvider.token()` with a `MockTransport` mints a JWT, calls `POST /app/installations/{id}/access_tokens` exactly once, caches the returned token, and does **not** re-call until within 60s of `expires_at`; after `invalidate()` it re-mints on the next `token()`.
3. **[offline]** `_request` retries on 500/502/503/504 up to `RetryPolicy.max_attempts` with backoff, retries once on `401` after `provider.invalidate()`, and respects a `403`/`429` `Retry-After` (asserted via a fake clock — no real sleep); a non-retryable `404` raises `GitHubError(status_code=404)` immediately.
4. **[offline]** `_paginate` follows `Link: rel="next"` across ≥2 pages and concatenates items; a single-page response yields exactly one page (no extra request).
5. **[offline]** `push_files` issues the Git Data API sequence (blob → tree → commit → update-ref) with correct payloads and returns the new commit SHA; `create_branch` resolves the base SHA and posts `refs/heads/<branch>`. (All via `MockTransport` + `RequestRecorder`.)
6. **[offline]** Redaction: a `GitHubAuditEvent` carrying a token-shaped string in `detail` is recorded with the token replaced by `REDACTED`; no audit entry, log line, or `GitHubError` message produced on any path contains the JWT, installation token, or `.pem` contents. (Asserted by scanning emitted records/log capture for the known test secret substring.)
7. **[offline]** `load_private_key` reads a temp `.pem` and never includes the key bytes in any raised exception (a missing-file error mentions only the path); `from_app` does not place the key in any attribute that `repr()`/`model_dump` would expose.
8. **[real creds]** Live installation-token mint succeeds against api.github.com using `deploy/secrets/github-app.pem` + `FORGE_GITHUB_APP_ID` + `FORGE_GITHUB_INSTALLATION_ID`; `GitHubClient.health()` returns `healthy:true` with a real `latency_ms`.
9. **[real creds]** On `FORGE_GITHUB_TEST_REPO`: `create_branch` + `push_files` + `open_pr` succeed; the returned `PullRequest.number` and `url` are non-null and the PR is visible via a follow-up `GET`. `request_reviews` (if a reviewer is configured) returns without error.
10. **[real creds]** `list_review_comments` / `list_reviews` read back the PR's review surface (empty list is a valid pass; the call must succeed + paginate).
11. **[real creds]** A **real** webhook delivery fetched from `GET /app/hook/deliveries` verifies: replaying its exact body + `X-Hub-Signature-256` at `POST /integration/github/webhooks` returns `200` with a parsed `CIStatus`; flipping one byte of the body returns `401`. (If the App has no recorded deliveries, the test triggers one via a push event on the test repo, then polls the delivery log.)
12. **[real creds]** Cleanup is idempotent: the test closes the PR and deletes the branch in teardown; rerunning the whole lane twice in a row leaves no orphan branches/PRs and both runs pass.
13. **[real creds]** Rate-limit realism: a forced burst hits a `403`/`429` secondary-limit path **or** the test asserts `X-RateLimit-Remaining` decreases across calls and the client honors `Retry-After` without erroring (whichever the live API surfaces; the handler is exercised, not just unit-mocked).
14. **[offline]** **Skip-clean gate:** with `FORGE_GITHUB_*` unset, every `[real creds]` test above skips with a clear reason and `uv run pytest -q` is fully green and makes **zero** network calls (asserted by a no-network guard fixture on the default lane).
15. **[offline]** **Whole-suite green gate:** `uv run pytest -q`, `uv run ruff check .`, `uv run ruff format --check .`, `make typecheck`, and `cd apps/web && pnpm test` are all green with HARD-01 merged (creds absent). No real secret appears in source, fixtures, snapshots, or the lockfile.

## 7. Test plan (TDD) — unit + integration (gated on env creds) + how to run

Write tests first; mirror the existing `packages/integration-sdk/tests/` style (`RequestRecorder`, `make_transport`, `load_fixture` from `tests/conftest.py`).

**Unit (offline, hermetic — `packages/integration-sdk/tests/`):**
- `test_github_auth.py`: `test_app_jwt_claims_and_signature` (AC1), `test_app_jwt_exp_capped_at_600s`, `test_installation_token_minted_and_cached` (AC2), `test_token_refresh_near_expiry`, `test_invalidate_forces_remint`.
- `test_github_retry.py`: `test_retries_on_5xx_with_backoff` (AC3, fake clock), `test_retry_once_on_401_after_invalidate`, `test_respects_retry_after_header`, `test_404_raises_immediately`.
- `test_github_pagination.py`: `test_paginate_follows_link_next` (AC4), `test_single_page_no_extra_request`.
- `test_github_gitdata.py`: `test_push_files_blob_tree_commit_ref_sequence` (AC5), `test_create_branch_resolves_base_sha`.
- `test_github_audit_redaction.py`: `test_audit_event_per_request`, `test_token_and_pem_never_in_audit_or_errors` (AC6, AC7).
- New fixtures under `tests/fixtures/`: `installation_token.json`, `git_ref.json`, `git_blob.json`, `git_tree.json`, `git_commit.json`, `pulls_page1.json`/`pulls_page2.json` (synthetic — **no real ids/secrets**).

**API unit (offline — `apps/api/tests/test_integration_router.py` additions):**
- `test_github_client_dep_uses_app_creds_when_configured` (DI builds `from_app`), `test_write_route_501_when_no_github_creds`, `test_health_route_returns_healthresult`, `test_audit_sink_redacts_detail` (AC6 at the API boundary).
- Reuse the existing `test_webhook_*` tests; add `test_webhook_real_delivery_bytes_verify` driven from a captured (sanitized) delivery fixture for the offline lane.

**Integration (gated — `@pytest.mark.integration`, runs only with creds):**
- `packages/integration-sdk/tests/integration/test_github_live.py`: a module-level `pytest.fixture` resolves creds from env and `pytest.skip(reason=...)` when absent (AC14). Tests: `test_live_token_mint_and_health` (AC8), `test_live_push_open_pr_and_read_reviews` (AC9, AC10) with a `try/finally` cleanup (AC12), `test_live_rate_limit_handling` (AC13).
- `apps/api/tests/integration/test_github_webhook_live.py`: `test_live_delivery_signature_verifies_and_tamper_rejected` (AC11).
- A shared `no_network` autouse fixture on the **default** lane (monkeypatches `httpx.HTTPTransport.handle_request` to raise) proves AC14's zero-network claim; the integration lane opts out.

**How to run:**
```bash
# Default hermetic suite (no creds, no network) — must be green:
uv run pytest -q
uv run pytest packages/integration-sdk -q

# Integration lane (requires real creds in env / .env.integration):
cp .env.integration.example .env.integration   # then fill in values
#   FORGE_GITHUB_APP_ID, FORGE_GITHUB_INSTALLATION_ID, FORGE_GITHUB_WEBHOOK_SECRET,
#   FORGE_GITHUB_TEST_REPO, FORGE_GITHUB_APP_PRIVATE_KEY_PATH=deploy/secrets/github-app.pem
set -a && source .env.integration && set +a
uv run pytest -m integration -k github -q

# CI: the `integration-github` job runs the lane only when the secrets exist.
```
Evidence to capture for G-GH: the integration-lane pytest output (token mint, PR number/url, webhook verify pass + tamper-reject), plus the default-lane green run with the skip lines.

## 8. Security & policy considerations

- **Private key handling (`.pem` is a path, not a value).** The key is read from `deploy/secrets/github-app.pem` (path from `FORGE_GITHUB_APP_PRIVATE_KEY_PATH`), used only to sign the App JWT in-memory, and never written to the DB, logs, audit rows, traces, or error messages (AC7). `.gitignore` covers `*.pem` and `.env*`; HARD-01 adds an explicit `deploy/secrets/` ignore line and a no-secret `deploy/secrets/README.md`.
- **Short-lived, least-privilege tokens.** Installation tokens are minted on demand, cached only in process memory, expire (~1h, GitHub-set), and are scoped to the single installation. The App JWT is capped at GitHub's 600s max. No long-lived PAT is used or stored.
- **Secret redaction is the single source of truth.** All audit/log emission on the live path flows through `forge_api.observability.redaction` (`redact_text`/`redact_mapping`) — the same filter F05 §8 mandates for MCP — so token-shaped strings, `Authorization` headers, and PEM blocks are scrubbed defensively even if something slips into `detail`/`metadata` (AC6).
- **Webhook trust boundary stays fail-closed.** `POST /integration/github/webhooks` remains outside the principal dependency and trusts **only** the `X-Hub-Signature-256` HMAC; a missing/invalid signature or unconfigured secret is rejected (`401`). HARD-01 proves this on real delivery bytes and on a tampered body (AC11) — forged CI/status events cannot drive workflow transitions.
- **Confused-deputy / cross-tenant.** The sync route already resolves `RepositoryConnection` **server-side** by `connection_id` scoped to the caller's workspace (never from the body). HARD-01 preserves this: the App token is only ever pointed at server-resolved repos, and the integration lane is hard-pinned to `FORGE_GITHUB_TEST_REPO`.
- **RBAC.** Write routes (sync, open-PR, push) require `Permission.WRITE`; `health` requires `Permission.READ`; the webhook route is HMAC-authenticated, not RBAC-gated — unchanged from F03 and re-asserted here.
- **SSRF surface.** `github_api_url` (GHE override) is operator-set config, not caller-supplied; the client never fetches caller-provided URLs. Noted for the HARD-09 pentest punch-list (attack surface = webhook verifier + App auth) but no caller-controlled URL is introduced here.
- **Audit completeness.** Every GitHub op (mint, sync, push, open-PR, review-read, webhook-verify) emits an `INTEGRATION` audit entry with `payload_hash` (never the body), `status`, `latency_ms`, and `connection_id` — satisfying the spec "Every agent action, tool call, MCP call, and approval — immutable, queryable" for the integration surface.

## 9. Effort & risk

**Effort: L.** App-auth (JWT + token exchange + caching) is M on its own; adding retry/secondary-rate-limit/pagination across the client, Git Data API push primitives, the audit/redaction seam, the creds-gated live lane, and CI wiring pushes it to L. Roughly: `github_auth.py` M · retry/pagination/push S–M · audit+redaction seam S · live integration tests + cleanup M · CI/secret-mount/docs S.

**Risks & cannot-do-in-sandbox:**
- **Cannot run the live half in the no-network sandbox (CANNOT, by design).** AC8–AC13 require real creds + outbound network to api.github.com; they run on a networked CI runner with secrets, not here. The hermetic AC1–AC7, AC14–AC15 run anywhere. This is the honest ceiling for G-GH and is stated as such.
- **Test-repo statefulness / flakiness (Med).** Live PR/branch/webhook tests mutate a real repo. Mitigation: unique run-scoped branch names (`forge/hardening-smoke-<runid>`), `try/finally` cleanup, idempotent reruns (AC12), and `xfail`/retry on transient `5xx` (the retry policy itself absorbs most).
- **Webhook delivery timing (Med).** A real delivery may not exist yet / may lag. Mitigation: trigger a push then poll `GET /app/hook/deliveries` with a bounded wait; fall back to a sanitized captured delivery for the offline assertion so the verifier logic is always covered.
- **GitHub rate limits in CI (Low–Med).** Repeated lane runs can hit primary limits. Mitigation: the lane is opt-in/low-frequency, the client honors `Retry-After`, and per-run resource use is tiny (one branch, one PR).
- **Secret leakage in CI logs (High impact, Low likelihood).** Mitigation: `add-mask`, no `set -x` around the `.pem` write, redaction filter on all app output, and AC6/AC7 scanning tests as a backstop.
- **`pyjwt[crypto]` adds `cryptography` to the SDK closure (Low).** Already present transitively via `apps/api`; HARD-14 re-locks. Mitigation: pin `>=2.8`, declare in `integration-sdk/pyproject.toml`.
- **CANNOT (human-only):** the 3rd-party human **pentest** of the App-auth + webhook attack surface — HARD-01 feeds the inventory item to the HARD-09 punch-list; the engagement itself stays an explicit external gap.

## 10. Key files / paths (real monorepo)

- `packages/integration-sdk/forge_integrations/github_auth.py` — **new**: `load_private_key`, `build_app_jwt`, `InstallationTokenProvider`.
- `packages/integration-sdk/forge_integrations/github.py` — **extend**: `from_app`, `_request` (retry/backoff/rate-limit/token-injection), `_paginate`, `create_branch`, `push_files`, `list_review_comments`, `list_reviews`, `close_pr`, `delete_branch`, `RetryPolicy`.
- `packages/integration-sdk/forge_integrations/audit.py` — **new**: `GitHubAuditEvent`, `AuditSink`.
- `packages/integration-sdk/forge_integrations/__init__.py` — export the new public names.
- `packages/integration-sdk/forge_integrations/webhooks.py` — **unchanged** (reused verifier).
- `packages/integration-sdk/pyproject.toml` — **extend** deps: `pyjwt[crypto]>=2.8`.
- `packages/integration-sdk/tests/test_github_auth.py`, `test_github_retry.py`, `test_github_pagination.py`, `test_github_gitdata.py`, `test_github_audit_redaction.py` — **new** unit tests.
- `packages/integration-sdk/tests/integration/test_github_live.py` — **new** gated live tests.
- `packages/integration-sdk/tests/fixtures/{installation_token,git_ref,git_blob,git_tree,git_commit,pulls_page1,pulls_page2}.json` — **new** synthetic fixtures.
- `apps/api/forge_api/routers/integration.py` — **extend**: App-aware `_github_client_singleton`, `audit_sink` adapter, `GET /github/health`.
- `apps/api/forge_api/settings.py` — **extend**: `github_app_id`, `github_app_private_key_path`, `github_installation_id`, `github_test_repo`.
- `apps/api/forge_api/observability/audit.py` — **extend**: `AuditCategory.INTEGRATION`.
- `apps/api/tests/test_integration_router.py` — **extend** API unit tests.
- `apps/api/tests/integration/test_github_webhook_live.py` — **new** gated webhook-delivery test.
- `apps/web/lib/api/integration.ts` — **extend**: `getGithubHealth()`.
- `deploy/secrets/` (+ `deploy/secrets/README.md`) — **new** env-only key location; `deploy/secrets/github-app.pem` is gitignored and never committed.
- `.gitignore` — **extend**: add `deploy/secrets/` line (defense-in-depth).
- `.env.integration.example` — **new** (key names only).
- `deploy/docker-compose.yml` — **extend**: mount `deploy/secrets/github-app.pem` ro + pass `FORGE_GITHUB_*`.
- `.github/workflows/ci.yml` — **extend**: opt-in `integration-github` job (creds-gated).

## 11. Research references

- GitHub Apps — generating a JWT (RS256, `iat`/`exp`≤10min/`iss`=app id): https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-json-web-token-jwt-for-a-github-app
- Authenticating as an installation / minting installation access tokens (`POST /app/installations/{id}/access_tokens`): https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/authenticating-as-a-github-app-installation
- Git Data API (blobs/trees/commits/refs) for branch push without a local clone: https://docs.github.com/en/rest/git
- Securing webhooks — validating `X-Hub-Signature-256` (HMAC-SHA256): https://docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries
- App webhook deliveries (list/get/redeliver) for verifying a real delivery: https://docs.github.com/en/rest/apps/webhooks
- Primary & secondary rate limits + `Retry-After` / `x-ratelimit-*` best practices: https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api and https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api
- PyJWT (RS256 signing with `cryptography`): https://pyjwt.readthedocs.io/
- smee.io (optional local webhook relay for true inbound delivery in dev): https://smee.io/
- Spec/report anchors: `docs/FORGE_SPEC.md` → "Integration Layer (GitHub App)" + Security table ("immutable audit log", "secrets stripped from logs"); `docs/implementation-slices/v1/F03-github-app.md` (the fixture-backed base this hardens); `docs/MORNING_REPORT.md` §1.13, §5(4), §6 (provider/transport realism); `SPEC-PRODUCTION-HARDENING.md` §"BETA bar" G-GH (HARD-05 mapping) + §"Credentials & secrets handling".

## 12. Out of scope / future

- **Real Model / embedder / reranker / MCP / Slack live exercise** — sibling hardening workstreams (HARD-02, HARD-03, HARD-06, HARD-07); HARD-01 covers GitHub only.
- **GitHub App install/onboarding flow** (public install URL → setup callback → per-repo `RepositoryConnection` rows) — already specified in F03 §2/§3; HARD-01 assumes the App is installed and an `installation_id` is known.
- **Branch push via authenticated git-over-HTTPS / local bare mirror** — HARD-01 uses the Git Data API for testability; the mirror-based push path (F03 §3) is a future optimization for large commits.
- **GitHub Enterprise Server live verification** — `github_api_url` override is supported; a live GHE run is future once a GHE test instance exists.
- **GitLab / Bitbucket providers** (`RepoProvider.GITLAB`/`BITBUCKET`) — out of scope; V1 is GitHub-only.
- **Image digest pinning + `docker compose build`** — HARD-08; HARD-01 only adds the secret mount + env passthrough.
- **Human pentest of the App-auth + webhook attack surface** — handed to the HARD-09 punch-list; cannot be performed by build agents.
</content>
</invoke>
