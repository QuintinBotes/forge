# F03 — GitHub App Integration

> Phase: v1 · Spec module(s): Integration Layer (GitHub App: repo sync, PRs, CI, reviews, webhooks), Core Data Model (`RepositoryConnection`), Sandbox V1 (Git worktrees), Knowledge Sync (incremental), Workflow Engine (PR/CI/review transitions), Security (secrets, audit, policy) · Status target: **Done** = a workspace admin can install the Forge GitHub App, every accessible repo materializes as a `RepositoryConnection`, inbound webhooks are signature-verified + deduped + parsed into normalized domain events, the `open_pr` primitive + `render_pr_body` traceability renderer let the workflow FSM (F08) / agent runtime open a PR carrying a spec-traceability section, CI/review/merge/push events are persisted and emitted for downstream consumers, repo sync produces a bare mirror + triggers incremental knowledge indexing, and the entire surface is exercised against recorded fixtures with zero live network calls. Lint + types + `pytest` green on `packages/integration-sdk`, the `apps/api` integration router, and the `apps/worker` GitHub tasks.

---

## 1. Intent — what & why

Forge is repo-aware by design: every task knows its repo target before execution, the agent works in a git worktree, and human approval gates PR merges. GitHub is the V1 source of truth for code, PRs, CI results, and reviews. This slice builds the **GitHub App integration** that connects a Forge workspace to GitHub repositories and provides the four integration capabilities the spec lists for V1:

1. **Repo sync** — discover installed repos, persist them as `RepositoryConnection` rows, and maintain a local bare mirror that the agent runtime (`v1/F06-single-execution-agent`) worktrees from and the knowledge service (`v1/F05-hybrid-knowledge-retrieval`) indexes.
2. **PRs** — the mechanical primitives to open/update pull requests programmatically (`open_pr`/`update_pr`) plus a `render_pr_body` helper that renders the spec-traceability section (which acceptance criteria are claimed satisfied). The end-to-end `open_pr_with_spec_traceability` FSM transition that composes the *full* PR body (verification table, confidence, knowledge provenance, plus the traceability section) and persists the `pull_request` row is owned by `v1/F08-plan-execute-verify-pr-approval`, which calls this slice's `open_pr`. See the boundary note in §3.2.
3. **CI** — receive `check_suite` / `check_run` / commit-`status` events and expose an on-demand `get_ci_status`, producing the `ci_status_green` signal the merge gate requires.
4. **Reviews & webhooks** — verify and parse GitHub webhooks (`pull_request`, `pull_request_review`, `check_suite`, `check_run`, `status`, `push`, `installation*`) into normalized domain events consumed by the workflow FSM, board timeline, and incremental knowledge sync.

Why a GitHub **App** (not a PAT/OAuth token): per-installation least-privilege scoping, short-lived installation tokens, first-class webhook delivery, and it is the best PR/CI/review event source (Open SWE uses the same approach — see §11). Why fixtures-only in V1 build: the overnight/build constraints forbid live external calls; all clients are built against a transport interface with recorded HTTP fixtures so the integration is fully unit/integration tested offline and live-verified later.

---

## 2. User-facing behavior / journeys

**J1 — Connect a workspace to GitHub (admin).**
Admin opens Settings → Integrations → GitHub, clicks "Connect GitHub", and is sent to the GitHub App public install URL. After choosing repos on GitHub, GitHub redirects to Forge's setup callback (`?installation_id=...&setup_action=install`). Forge binds that installation to the current workspace, lists the installation's repositories, and creates one `RepositoryConnection` per repo (status `pending`). The settings page then shows each repo with sync status and a "Sync now" button.

**J2 — Repo sync.**
On connection (or "Sync now"), Forge clones a bare mirror of the repo using a fresh installation token, records `last_synced_sha` + `last_synced_at` + status `synced`, and enqueues a knowledge index job. Subsequent `push` webhooks trigger incremental sync (only changed files re-indexed).

**J3 — PR opened for a task (system, surfaced in UI).**
When a task run reaches `verifying -> pr_opened`, the workflow FSM transition (owned by `v1/F08-plan-execute-verify-pr-approval`'s `PRBuilderService`) composes the full PR body (verification results, confidence, knowledge provenance, plus the spec-traceability table) and calls this slice's `open_pr` with the task branch, base branch, title, and rendered body. The spec-traceability table is produced by this slice's `render_pr_body`. `v1/F06-single-execution-agent` may instead call `open_pr` directly only when a task's policy/skill explicitly authorizes the agent to open its own PR. The PR appears in the task's unified timeline; the connection's CI status is tracked.

**J4 — CI / review / merge reconciliation (system).**
GitHub sends `check_suite`/`status`, `pull_request_review`, and `pull_request` (merged) webhooks. Forge verifies the signature, dedupes by delivery id, parses each into a domain event, persists it, and emits it. The workflow engine's merge gate (`awaiting_review -> merged`) consumes `review_approved_by_human` + `ci_status_green` + `spec_validated`; the merged event closes the loop.

**J5 — Disconnect (admin).**
Admin clicks "Disconnect" (or GitHub sends `installation.deleted`); Forge marks the connection(s) inactive, stops processing their webhooks, and retains audit history.

---

## 3. Vertical slice

### 3.1 Data model (tables/columns/migrations touched)

The Phase-0 baseline (`packages/db`, package `forge_db`) already declares `RepositoryConnection`. This slice extends it and adds one idempotency table. Alembic migration: `packages/db/migrations/versions/<rev>_github_app_integration.py`, with `down_revision` chained off the foundation baseline migration (and the latest `repository_connections`-touching migration if one already exists at build time).

**`repository_connections`** (extend existing model `forge_db/models/repository_connection.py`):

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | existing |
| `workspace_id` | UUID FK → `workspaces.id` | existing; tenant scope; indexed |
| `installation_id` | BIGINT | GitHub App installation id; indexed |
| `account_login` | TEXT | org/user that installed the app |
| `account_type` | TEXT | `Organization` \| `User` |
| `repo_full_name` | TEXT | `owner/name`; unique with `workspace_id` |
| `repo_github_id` | BIGINT | GitHub numeric repo id |
| `default_branch` | TEXT | e.g. `main` |
| `clone_url` | TEXT | https clone url (token injected at runtime, never stored) |
| `private` | BOOLEAN | |
| `permissions` | JSONB | granted installation permissions snapshot |
| `mirror_path` | TEXT | absolute path of local bare mirror (nullable until first sync) |
| `sync_status` | ENUM `repo_sync_status` | `pending` \| `syncing` \| `synced` \| `error` |
| `sync_error` | TEXT | last error message, nullable |
| `last_synced_at` | TIMESTAMPTZ | nullable |
| `last_synced_sha` | TEXT | nullable; head SHA of default branch at last sync |
| `active` | BOOLEAN | false when installation suspended/removed; default true |
| `created_at` / `updated_at` | TIMESTAMPTZ | existing |

Constraints/indexes: `UNIQUE(workspace_id, repo_full_name)`, index on `installation_id`, index on `(workspace_id, active)`.

**`github_webhook_deliveries`** (new model `forge_db/models/github_webhook_delivery.py`) — idempotency + audit dedup:

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `delivery_id` | TEXT | GitHub `X-GitHub-Delivery` GUID; **UNIQUE** |
| `event_type` | TEXT | `X-GitHub-Event` |
| `action` | TEXT | payload `action`, nullable |
| `installation_id` | BIGINT | nullable |
| `repo_full_name` | TEXT | nullable |
| `payload_hash` | TEXT | sha256 hex of raw body |
| `received_at` | TIMESTAMPTZ | |
| `processed_at` | TIMESTAMPTZ | nullable; set when worker completes |
| `status` | ENUM `webhook_delivery_status` | `received` \| `processed` \| `skipped` \| `error` |

The GitHub App **private key** and **installation tokens are NOT stored in these tables.** The private key lives in the AES-256-GCM envelope-encrypted, per-workspace-isolated secrets vault provided by `cross-cutting/F37-auth-secrets-byok`; installation tokens are cached in Redis with TTL (see §3.3). Raw webhook payloads are not persisted in full beyond the worker's transient processing — only `payload_hash` + parsed/normalized fields persist (secret-redaction requirement via F37's `SecretRedactor`, §8).

### 3.2 Backend (FastAPI routes + services/packages)

**Router:** `apps/api/forge_api/routers/integration.py` (Phase-0 stub → real handlers). All routes auth-required except the webhook intake (which is signature-verified instead). RBAC: connect/disconnect/sync require `admin` or `member`; reads allowed for `viewer`; `agent-runner` may call PR ops via the service layer, not these admin routes.

| Method & path | Purpose | Auth |
|---|---|---|
| `POST /integrations/github/webhook` | Webhook intake. Reads **raw body**, verifies `X-Hub-Signature-256`, dedupes by `X-GitHub-Delivery`, writes a `github_webhook_deliveries` row (status `received`), enqueues `process_github_webhook`, returns `202`. Returns `401` on bad/missing signature. | Signature (no bearer) |
| `GET /integrations/github/install-url` | Returns the GitHub App public install URL + `state` (workspace-scoped). | bearer |
| `GET /integrations/github/setup-callback` | Handles GitHub post-install redirect. The App is configured with **"Request user authorization (OAuth) during installation"** enabled so the redirect carries `installation_id`, `setup_action`, `code`, and the CSRF `state` issued by `install-url` (the bare public-install URL does **not** echo `state`). Validates `state` (CSRF + workspace binding), binds installation → workspace, lists repos, upserts `RepositoryConnection` rows, enqueues initial `sync_repository(full)`. | bearer |
| `GET /integrations/github/installations` | List installations + repos for the workspace with sync status + connection health. | bearer |
| `POST /integrations/github/installations/{installation_id}/sync` | Trigger `sync_repository` for all repos under the installation (mode query param `full`\|`incremental`, default `incremental`). | bearer (admin/member) |
| `GET /integrations/github/repos/{connection_id}` | Single connection detail + last CI status snapshot. | bearer |
| `DELETE /integrations/github/installations/{installation_id}` | Mark connection(s) inactive (soft disconnect). | bearer (admin) |

**PR operations are service-level, not REST.** The workflow FSM (F08) / agent runtime (F06) call `GitHubAppClient.open_pr(...)` directly through the injected `GitHubIntegration` (so they pass policy evaluation, §8). A debug-only `POST /integrations/github/prs` MAY be added behind an admin flag for manual testing; not required for V1.

**PR-body ownership boundary (F03 ↔ F08).** This slice owns (a) the GitHub *transport* (`open_pr`/`update_pr`/`merge_pr`/`get_ci_status`/`get_pr`) and (b) `render_pr_body(spec)`, which renders **only** the spec-traceability section (the acceptance-criterion table). `open_pr` sends `PullRequestSpec.body` and appends the `render_pr_body` traceability section **iff** `spec.traceability` is set — so it never double-renders a body that already contains one. `v1/F08-plan-execute-verify-pr-approval`'s `PRBuilderService` is the authoritative composer of the *full* PR body (it holds the verification report, confidence score, and knowledge provenance that this slice does not) and calls `open_pr` with the finished body (`traceability=None`). The default V1 path is the F08 FSM transition; the F06 agent-direct path is policy-gated and may reuse `render_pr_body` to keep one source of truth for the traceability table.

**Package:** `packages/integration-sdk/forge_integrations/github/`

| File | Responsibility |
|---|---|
| `auth.py` | `GitHubAppAuth` — RS256 app-JWT minting + installation-token exchange + Redis-backed cache w/ expiry. |
| `transport.py` | `GitHubTransport` Protocol; `HttpxGitHubTransport` (real, httpx async); `FixtureGitHubTransport` (replays recorded responses keyed by `(method, path)`). |
| `client.py` | `GitHubAppClient` implementing the `GitHubIntegration` Protocol (repos, PRs, reviews, CI, file/tree reads, branch ops). |
| `webhooks.py` | `verify_signature`, `parse_event`, `to_domain_events`, `ci_conclusion_to_state`. |
| `pr_body.py` | `render_pr_body(spec: PullRequestSpec) -> str` — renders the spec-traceability markdown section. |
| `sync.py` | `RepoSyncService` — clone/fetch bare mirror, compute changed files, update connection metadata. |
| `models.py` | Re-exports / thin local DTOs; canonical DTOs live in `packages/contracts` (§4). |
| `errors.py` | `GitHubAuthError`, `WebhookVerificationError`, `RepoSyncError`, `RateLimitError`. |

`apps/mcp-gateway` is NOT touched. `forge_integrations/slack/*` is out of scope (separate V1 feature).

### 3.3 Worker / agent runtime (Celery tasks, LangGraph, if any)

**Celery tasks:** `apps/worker/forge_worker/tasks/github.py` (registered on the existing Celery app; queue `integrations`).

- `process_github_webhook(delivery_id: str)` — load the `github_webhook_deliveries` row, re-verify it has not been processed (idempotent), `parse_event` → `to_domain_events`, persist normalized events, update `RepositoryConnection` (e.g. `active`, CI snapshot), and dispatch downstream: enqueue `sync_repository(incremental)` on `push`, emit workflow/board domain events. Sets `processed_at` + status `processed` (or `skipped`/`error`).
- `sync_repository(connection_id: str, mode: Literal["full", "incremental"] = "incremental")` — invoke `RepoSyncService`; on success enqueue F05's task (`v1/F05-hybrid-knowledge-retrieval`, queue `knowledge`): `knowledge.incremental_sync(source_id=<repo source>, changed_paths=changed_files, to_commit=head_sha)` (incremental) or `knowledge.full_sync(source_id=<repo source>)` (full). These are F05-owned tasks (mocked in this slice's tests via `send_task`/signature assertion).
- `refresh_installation_token(installation_id: int)` — optional pre-warm of the token cache; idempotent.

**Installation-token cache:** Redis key `gh:itoken:{installation_id}` → encrypted token JSON, TTL = `expires_at - 60s`. `GitHubAppAuth.installation_token()` checks the cache first; on miss mints app-JWT → `POST /app/installations/{id}/access_tokens` → stores. A `401` from a GitHub call invalidates the cache and retries once.

**Agent runtime touchpoint (consumer, not built here):** `sync.py` writes the bare mirror to the deterministic, **frozen-contract** path `${FORGE_DATA_DIR}/repos/{workspace_id}/{owner}__{repo}.git`. With the compose defaults owned by `v1/F14-docker-compose-selfhost` (`FORGE_DATA_DIR=/var/lib/forge`; named volume `forge_repos` mounted at `/var/lib/forge/repos` into both `api` and `worker`), this resolves under the shared `forge_repos` volume. `v1/F06-single-execution-agent` adds *worktrees* off this mirror and MUST read it at `${FORGE_DATA_DIR}/repos/...`. **Contract note:** `FORGE_DATA_DIR` + the `forge_repos` volume are canonical (the compose owner F14 defines them); F06's draft `REPO_CACHE_ROOT`/`forge-repo-cache` env+volume names must be reconciled to this contract. This slice only guarantees the mirror-path contract and records the absolute path in `RepositoryConnection.mirror_path`. No LangGraph code in this slice.

### 3.4 Frontend / UI (Next.js routes/components, if any)

Minimal V1 settings surface under `apps/web`:

- Route: `apps/web/app/(settings)/integrations/github/page.tsx` — server component listing connections via `GET /integrations/github/installations`.
- Components (`apps/web/components/integrations/`): `GitHubConnectButton.tsx` (fetches install-url, redirects), `GitHubConnectionsTable.tsx` (TanStack Table: repo, branch, sync status badge, last synced, CI snapshot, "Sync now" / "Disconnect" actions), `SyncStatusBadge.tsx`.
- A thin setup-callback landing page `apps/web/app/(settings)/integrations/github/callback/page.tsx` that posts the `installation_id`/`state` to `GET /integrations/github/setup-callback` then redirects back to the list.
- PR / CI / review activity is surfaced in the **task timeline** (owned by the board UI slice); this slice only provides the connection-management screen.

### 3.5 Infra / deploy (compose, helm, caddy, if any)

- **Compose / env / Caddyfile are owned by `v1/F14-docker-compose-selfhost`**, which already reserves the `forge_repos` volume (`/var/lib/forge/repos`), `FORGE_DATA_DIR`, and the Caddy raw-body webhook route for this slice — F03 only *fills in values* (coordinated with F14). F03 appends: `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY` (PEM; multiline via base64 env or file mount path `GITHUB_APP_PRIVATE_KEY_PATH`), `GITHUB_APP_WEBHOOK_SECRET`, `GITHUB_APP_CLIENT_ID`, `GITHUB_APP_CLIENT_SECRET`, `GITHUB_APP_SLUG`, `FORGE_PUBLIC_URL` (webhook + setup-callback URLs) to `deploy/.env.example` and the `api`/`worker` env. The private key is **never committed**; it is resolved from the F37 vault / env-mount at boot.
- `deploy/caddy/Caddyfile` (F14-owned): `POST /integrations/github/webhook` is reverse-proxied to `api` **without** body buffering/transformation (signature is over the exact raw bytes). Caddy passes bodies untouched by default; the Caddyfile carries a comment asserting no `request_body` rewrite on this path (regression-locked by F14's `test_caddyfile.py`).
- Named volume `forge_repos` for `${FORGE_DATA_DIR}/repos` shared **read-write** between `api` and `worker` (both need the mirror).
- Helm (V2) out of scope.

---

## 4. Public interfaces / contracts (exact signatures, Pydantic models, Protocols, YAML schemas)

Canonical DTOs + Protocols added to `packages/contracts/forge_contracts/integration.py` (the Phase-0 contracts module names `IntegrationClient` Protocol; this slice freezes the GitHub-specific surface).

```python
# packages/contracts/forge_contracts/integration.py
from __future__ import annotations
from datetime import datetime
from typing import Literal, Protocol, runtime_checkable
from pydantic import BaseModel, Field

# ---------- DTOs ----------

class RepoRef(BaseModel):
    full_name: str                      # "owner/name"
    github_id: int
    default_branch: str
    private: bool
    clone_url: str                      # https; token injected at call time, never stored

class ACResult(BaseModel):
    id: str                             # e.g. "A1"
    text: str
    satisfied: bool
    evidence: str | None = None         # link/path/test ref

class SpecTraceability(BaseModel):
    spec_id: str
    task_id: str
    acceptance_criteria: list[ACResult]

class PullRequestSpec(BaseModel):
    repo_full_name: str
    head: str                           # branch to merge from (e.g. "forge/TASK-123")
    base: str                           # base branch (e.g. "main")
    title: str
    body: str                           # narrative; traceability appended by render_pr_body
    draft: bool = False
    labels: list[str] = Field(default_factory=list)
    reviewers: list[str] = Field(default_factory=list)
    traceability: SpecTraceability | None = None

class PullRequest(BaseModel):
    number: int
    url: str
    head_sha: str
    state: Literal["open", "closed", "merged"]
    draft: bool
    merge_commit_sha: str | None = None   # set by merge_pr; F08's MergeResult.merged_sha reads this

class CheckRunResult(BaseModel):
    name: str
    status: Literal["queued", "in_progress", "completed"]
    conclusion: str | None              # raw GitHub conclusion
    url: str | None = None

class CIStatus(BaseModel):
    sha: str
    state: Literal["pending", "success", "failure", "error"]
    checks: list[CheckRunResult] = Field(default_factory=list)

class ReviewResult(BaseModel):
    pr_number: int
    reviewer: str
    state: Literal["approved", "changes_requested", "commented", "dismissed"]
    submitted_at: datetime

class InstallationToken(BaseModel):
    token: str
    expires_at: datetime
    permissions: dict[str, str] = Field(default_factory=dict)

# ---------- Normalized webhook + domain events ----------

class GitHubEvent(BaseModel):
    delivery_id: str
    event_type: str                     # X-GitHub-Event
    action: str | None
    installation_id: int | None
    repo_full_name: str | None
    payload: dict                       # validated, secret-free subset

class DomainEvent(BaseModel):
    kind: Literal[
        "ci_status_changed", "review_submitted", "pull_request_merged",
        "pull_request_opened", "push_received",
        "installation_created", "installation_deleted",
    ]
    repo_full_name: str | None
    installation_id: int | None
    data: dict                          # event-specific normalized fields

# ---------- Protocols ----------

@runtime_checkable
class GitHubTransport(Protocol):
    async def request(
        self, method: str, path: str, *,
        token: str | None = None,
        json: dict | None = None,
        params: dict | None = None,
    ) -> "HttpResponse": ...

class HttpResponse(BaseModel):
    status_code: int
    json_body: dict | list | None = None
    headers: dict[str, str] = Field(default_factory=dict)

@runtime_checkable
class GitHubIntegration(Protocol):
    async def list_installation_repos(self, installation_id: int) -> list[RepoRef]: ...
    async def open_pr(self, spec: PullRequestSpec, *, installation_id: int) -> PullRequest: ...
    async def update_pr(self, repo_full_name: str, number: int, *,
                        installation_id: int, title: str | None = None,
                        body: str | None = None, state: str | None = None) -> PullRequest: ...
    async def get_pr(self, repo_full_name: str, number: int, *, installation_id: int) -> PullRequest: ...
    async def merge_pr(self, repo_full_name: str, number: int, *,
                       installation_id: int, sha: str, method: Literal["merge", "squash", "rebase"] = "squash") -> PullRequest: ...
    async def get_ci_status(self, repo_full_name: str, sha: str, *, installation_id: int) -> CIStatus: ...
    async def list_reviews(self, repo_full_name: str, number: int, *, installation_id: int) -> list[ReviewResult]: ...
    async def request_reviewers(self, repo_full_name: str, number: int, *,
                                installation_id: int, reviewers: list[str]) -> None: ...
    async def add_labels(self, repo_full_name: str, number: int, *,
                         installation_id: int, labels: list[str]) -> None: ...
    async def get_file(self, repo_full_name: str, path: str, ref: str, *, installation_id: int) -> bytes: ...
```

**Consumer-side aliases (authoritative surface = `GitHubIntegration`).** `v1/F08-plan-execute-verify-pr-approval` refers to a narrower view of this Protocol as `GitHubAdapter`; `v1/F06-single-execution-agent` reaches it through the generic `IntegrationClient` slot frozen in `packages/contracts`. They are the same object. Mappings consumers rely on: F08's `repo_id` == `repo_full_name`; F08's `get_combined_status(...) -> str` == `get_ci_status(...).state`; F08's `merge_pr(...) -> MergeResult.merged_sha` == `PullRequest.merge_commit_sha`. The concrete `GitHubAppClient` resolves `installation_id` from the `RepositoryConnection` for a given `repo_full_name` (via an injected connection resolver), so the F08/F06 views need not pass `installation_id` explicitly.

Auth + webhook free functions / classes (in `forge_integrations.github`):

```python
class GitHubAppAuth:
    def __init__(self, app_id: str, private_key_pem: str,
                 transport: GitHubTransport, token_cache: "TokenCache",
                 clock: Callable[[], datetime] = ...) -> None: ...
    def app_jwt(self) -> str:                                   # RS256; iss=app_id; iat-60s; exp=+9min
        ...
    async def installation_token(self, installation_id: int) -> InstallationToken:  # cache-first
        ...
    async def invalidate(self, installation_id: int) -> None: ...

def verify_signature(secret: str, body: bytes, signature_header: str | None) -> bool:
    """HMAC-SHA256 over raw body, compared constant-time to 'sha256=<hex>'. False if header missing."""

def parse_event(event_type: str, delivery_id: str, payload: dict) -> GitHubEvent: ...
def to_domain_events(event: GitHubEvent) -> list[DomainEvent]: ...
def ci_conclusion_to_state(conclusion: str | None, status: str) -> Literal["pending","success","failure","error"]: ...
def render_pr_body(spec: PullRequestSpec) -> str: ...
```

`RepoSyncService`:

```python
class RepoSyncService:
    def __init__(self, auth: GitHubAppAuth, data_dir: str, git: "GitRunner") -> None: ...
    async def sync(self, conn: RepositoryConnection, mode: Literal["full","incremental"]) -> "SyncResult": ...

class SyncResult(BaseModel):
    connection_id: str
    head_sha: str
    changed_files: list[str]            # empty on first full sync (treated as "all")
    mirror_path: str
```

**CI conclusion → state mapping (frozen table, implemented by `ci_conclusion_to_state`):**

| GitHub `status` | GitHub `conclusion` | Forge state |
|---|---|---|
| `queued`/`in_progress` | — | `pending` |
| `completed` | `success` | `success` |
| `completed` | `neutral`, `skipped` | `success` |
| `completed` | `failure`, `timed_out`, `startup_failure` | `failure` |
| `completed` | `action_required`, `cancelled`, `stale` | `error` |
| `completed` | `null`/unknown | `error` |

Aggregation across multiple checks for `get_ci_status`: `failure` if any failure, else `error` if any error, else `pending` if any pending, else `success`.

---

## 5. Dependencies — features/slices that must exist first

**Hard prerequisites (must exist before this slice builds):**
- `v1/F00-foundation-substrate` — Phase-0 scaffold: `packages/db` (`forge_db`) data model (incl. the `RepositoryConnection` model + Alembic baseline), `packages/contracts` (`forge_contracts`; the `IntegrationClient` Protocol slot + DTO module this slice freezes), `apps/api` skeleton with the `routers/integration.py` stub pre-registered, `apps/worker` Celery app. (No dedicated `F00` slice file exists yet; this is the agreed Phase-0 scaffold every V1 slice builds on.)
- `cross-cutting/F37-auth-secrets-byok` — the AES-256-GCM envelope-encrypted, per-workspace-isolated secrets vault that stores the GitHub App private key; the `Principal` + flat `require_role(...)` RBAC dependency (admin/member/viewer/agent-runner) gating the integration routes; and the canonical `SecretRedactor`. The webhook secret and app id are read from settings/env (vault-eligible).
- `cross-cutting/F39-audit-log` — the frozen `AuditEvent` contract + `AuditSink` Protocol and `SqlAuditWriter` (redact-before-persist, per-workspace hash chain) that every outbound GitHub API call and accepted inbound webhook writes through.

**Coordination (owns shared infra this slice fills in):**
- `v1/F14-docker-compose-selfhost` — owns `deploy/docker-compose.yml`, `deploy/.env.example`, the `deploy/caddy/Caddyfile`, the `forge_repos` named volume, and `FORGE_DATA_DIR`; it already reserves the `GITHUB_APP_*` env, the volume, and the raw-body webhook route for this slice (F03 only fills in values).

**Integration points (consumers — need NOT exist first; this slice emits/produces against frozen contracts):**
- `v1/F05-hybrid-knowledge-retrieval` — consumes `RepoSyncService` output + `push_received` events for incremental indexing (this slice enqueues the indexer task; the indexer itself belongs to F05).
- `v1/F07-feature-workflow-fsm` — consumes `ci_status_changed` / `review_submitted` / `pull_request_merged` / `pull_request_opened` domain events to drive `verifying -> pr_opened` and `awaiting_review -> merged`.
- `v1/F08-plan-execute-verify-pr-approval` — **primary consumer of `open_pr`/`get_ci_status`/`merge_pr`.** Owns the `verifying -> pr_opened` FSM transition (its `PRBuilderService` composes the full PR body and calls this slice's `open_pr`) and the merge gate (`review_approved AND ci_green AND spec_validated`). See the PR-body boundary in §3.2.
- `cross-cutting/F36-human-approval-system` — owns the `pr`/`deploy` approval gates; `merge_pr` here is approval-gated and fires only once F36's gate resolves (default V1 flow: human merges on GitHub, reconciled via the merged webhook).
- `v1/F04-repo-policy` — `RepoSyncService` fetches `.forge/policy.yaml` + `AGENTS.md` during sync (`resolve_effective_policy`); `GitHubAppClient` write calls are gated by the policy evaluator.
- `v1/F06-single-execution-agent` — worktrees from the bare mirror produced here (mirror-path contract, §3.3) and may call `open_pr` when policy authorizes.
- `v1/F01-project-board` — renders PR/CI/review domain events in the task unified timeline.
- `cross-cutting/F38-observability-cost-metrics` — consumes the structured logs/traces/metrics this slice emits for GitHub calls and webhook processing.

---

## 6. Acceptance criteria (numbered, testable)

1. **Installation binding.** Calling the setup-callback with a valid `installation_id` + workspace `state` upserts exactly one `RepositoryConnection` per repo returned by `list_installation_repos`, each with `installation_id`, `repo_full_name`, `repo_github_id`, `default_branch`, `private`, and `sync_status="pending"`; re-running the callback is idempotent (no duplicate rows; uniqueness on `(workspace_id, repo_full_name)`).
2. **App JWT.** `GitHubAppAuth.app_jwt()` returns an RS256-signed JWT whose claims are `iss == app_id`, `iat == now-60s`, `exp <= now+600s`; signature verifies against the public key.
3. **Token cache.** `installation_token(id)` exchanges the app-JWT once, caches the token with TTL `expires_at-60s`; a second call within TTL returns the cached token and makes **zero** additional transport calls; after expiry (clock advanced) it re-exchanges; a simulated `401` on a downstream call invalidates the cache and triggers exactly one re-mint+retry.
4. **Webhook signature.** `POST /integrations/github/webhook` returns `401` when `X-Hub-Signature-256` is missing, malformed, or computed over a tampered body; returns `202` when the HMAC-SHA256 over the exact raw body matches; comparison is constant-time.
5. **Webhook idempotency.** Two POSTs with the same `X-GitHub-Delivery` both return `202`, create exactly one `github_webhook_deliveries` row, and enqueue `process_github_webhook` exactly once.
6. **Event parsing.** For each fixture: `pull_request.opened` → `pull_request_opened`; `pull_request.closed` with `merged=true` → `pull_request_merged` (with merge commit SHA); `pull_request_review.submitted` state `approved` → `review_submitted` (reviewer + `approved`); `check_suite.completed` / `check_run.completed` / `status` → `ci_status_changed` with the mapped state; `push` → `push_received` with before/after SHAs; `installation.created`/`deleted` → corresponding events and `active` flag updates.
7. **CI mapping.** `ci_conclusion_to_state` returns the exact values in the §4 table for every (status, conclusion) pair, and `get_ci_status` aggregation follows the documented precedence (any failure→failure, then error, then pending, else success).
8. **Open PR (mechanics + traceability).** `open_pr(spec)` issues `POST /repos/{owner}/{repo}/pulls` with the caller-supplied `spec.body`, then `add_labels` and `request_reviewers` when present, and returns a `PullRequest` with `number`, `url`, `head_sha`. When `spec.traceability` is set, the submitted body has the `render_pr_body` "Spec traceability" section (each acceptance-criterion id + text + satisfied flag) appended; when `spec.traceability` is `None` the body is sent verbatim (no double-render — the F08 path that pre-composes the full body relies on this). `render_pr_body` is independently unit-tested (§7).
9. **Repo sync (full).** `sync_repository(conn, "full")` clones a bare mirror into `${FORGE_DATA_DIR}/repos/{workspace_id}/{owner}__{repo}.git` using a freshly minted installation token in the clone URL, sets `mirror_path`, `last_synced_sha`, `last_synced_at`, `sync_status="synced"`, and enqueues a knowledge index job.
10. **Repo sync (incremental).** A second `sync_repository(conn, "incremental")` after new commits fetches and returns only the files changed since `last_synced_sha` (via `git diff --name-only <old>..<new>`), and the enqueued index job is scoped to those files.
11. **Secret redaction.** Installation tokens, the app private key, and the webhook secret never appear in any audit-log entry, structured log line, or API response (asserted by serializing a connection and capturing logs during a full PR-open + webhook flow).
12. **Audit completeness.** Every outbound GitHub API call and every accepted inbound webhook produces exactly one audit entry with method/path or event type, `payload_hash`, result status, and latency.
13. **Offline guarantee.** The full test suite passes with `HttpxGitHubTransport` unimported at runtime in tests; all GitHub interactions go through `FixtureGitHubTransport`; CI asserts no socket connections are opened during the integration-sdk tests.
14. **Disconnect.** `DELETE /integrations/github/installations/{id}` sets `active=false` on all of that installation's connections; subsequent webhooks for those repos are recorded but `skipped` (no downstream dispatch).
15. **Merge is gated, never bypassable.** No REST endpoint merges a PR; `merge_pr` is reachable only through F08's gated path, and its `ToolCall` policy evaluation (F04) requires an `approval` grant — a caller lacking a resolved `cross-cutting/F36-human-approval-system` approval is refused before any GitHub call. The human-approval-before-merge guarantee (`review_approved AND ci_green AND spec_validated`) is owned by F08's `MergeGateEvaluator`. On success `merge_pr` returns `PullRequest.merge_commit_sha`. The default V1 flow leaves merging to the human on GitHub, reconciled via the `pull_request.closed merged=true` webhook (criterion 6).

### 3.x note
All of §3 sub-sections are present; none are N/A.

---

## 7. Test plan (TDD) — concrete test cases (unit + integration), key fixtures

Write tests first; implement to green. Test root: `packages/integration-sdk/tests/` and `apps/api/tests/integration/`, `apps/worker/tests/`.

**Unit — `auth.py`**
- `test_app_jwt_claims` — decode (with public key) and assert iss/iat/exp bounds; assert RS256 alg.
- `test_installation_token_cache_hit` — first call hits transport once, second within TTL hits zero (spy on `FixtureGitHubTransport`).
- `test_installation_token_expiry_refresh` — advance injected clock past `expires_at-60s`; assert re-exchange.
- `test_401_invalidates_and_retries_once` — transport returns 401 then 200; assert one re-mint, one retry, no infinite loop.

**Unit — `webhooks.py`**
- `test_verify_signature_valid/invalid/missing/tampered` — parametrized; tampered body flips one byte.
- `test_verify_signature_constant_time` — uses `hmac.compare_digest` (assert via implementation, not timing).
- `test_parse_event_*` — one test per fixture in `tests/fixtures/github/webhooks/` (includes `check_suite` **and** `check_run`).
- `test_to_domain_events_*` — assert normalized fields per event type (merged commit SHA, reviewer+state, CI mapped state for both `check_suite` and `check_run`, push before/after).
- `test_ci_conclusion_to_state_table` — parametrized over the full §4 table.

**Unit — `pr_body.py`**
- `test_render_pr_body_includes_traceability` — snapshot; assert each AC id/text/✓-✗ present; assert no secrets.
- `test_render_pr_body_without_traceability` — narrative-only body unchanged.

**Unit — `client.py` (FixtureGitHubTransport)**
- `test_open_pr_posts_and_returns_pr` — assert request `(POST, /repos/o/r/pulls)` issued with mapped JSON; returns recorded PR.
- `test_open_pr_applies_labels_and_reviewers` — assert follow-up label + reviewer requests when present; skipped when empty.
- `test_get_ci_status_aggregation` — multiple check runs → aggregated state.
- `test_list_reviews_maps_states`, `test_get_file_returns_bytes`, `test_merge_pr_uses_squash_default`.
- `test_merge_pr_returns_merge_commit_sha` — recorded merge response → `PullRequest.merge_commit_sha` populated.
- `test_merge_pr_refused_without_approval_grant` — `PolicyEvaluator` denies the `merge_pr` `ToolCall` lacking an `approval` grant → raises, **zero** transport calls.
- `test_open_pr_appends_traceability_only_when_set` — body with `traceability=None` sent verbatim; with `traceability` set, the section is appended exactly once (no double-render).
- `test_redaction_no_token_in_serialized_output`.

**Unit — `sync.py`** (uses a local fixture git repo, no remote)
- `test_full_sync_creates_mirror` — clone from a `file://` bare fixture; assert `mirror_path`, `last_synced_sha`, status synced.
- `test_incremental_sync_changed_files_only` — add a commit to the fixture, fetch, assert `changed_files` matches `git diff --name-only`.
- `test_sync_error_sets_error_status` — point at a bad path; assert `sync_status="error"` + `sync_error` set, exception surfaced (no swallow).

**Integration — `apps/api` (Postgres test container + ASGI client)**
- `test_setup_callback_creates_connections` — `FixtureGitHubTransport` returns 2 repos; assert 2 rows; re-call → still 2.
- `test_webhook_endpoint_signed_202_and_enqueues` — signed POST (Celery eager) → 202 → connection/event updated.
- `test_webhook_endpoint_unsigned_401`.
- `test_webhook_dedup` — same delivery id twice → one row, one task.
- `test_disconnect_marks_inactive_and_skips`.
- `test_rbac_viewer_cannot_sync` — viewer principal → 403 on sync/connect/disconnect.

**Integration — `apps/worker`**
- `test_process_github_webhook_push_enqueues_incremental_sync`.
- `test_process_github_webhook_merged_emits_domain_event_persisted`.
- `test_sync_repository_full_then_index_enqueued` — assert knowledge index task enqueued (mock the F05 task).

**Key fixtures**
- `tests/fixtures/github/webhooks/*.json`: `pull_request.opened.json`, `pull_request.closed.merged.json`, `pull_request_review.approved.json`, `pull_request_review.changes_requested.json`, `check_suite.completed.success.json`, `check_suite.completed.failure.json`, `check_run.completed.success.json`, `check_run.completed.failure.json`, `status.success.json`, `push.json`, `installation.created.json`, `installation.deleted.json`, `installation_repositories.added.json`.
- `tests/fixtures/github/api/*.json`: `installation_token.json`, `installation_repos.json`, `create_pull.json`, `list_reviews.json`, `check_runs.json`, `get_pull.json`.
- `FixtureGitHubTransport(records: dict[tuple[str, str], HttpResponse], call_log: list)` — deterministic, asserts unexpected calls fail loudly.
- `rsa_test_keypair` fixture — generate an ephemeral RSA-2048 keypair (cryptography) for JWT sign/verify; never a real key.
- `bare_git_repo` fixture — `git init --bare` + a working clone with seed commits, exposed via `file://` for sync tests.
- `signed_webhook(body: bytes, secret: str)` helper — produces the `sha256=` header.
- `freeze/clock` fixture — injectable `now()` for token-expiry tests (no wall-clock dependence).

---

## 8. Security & policy considerations

- **Webhook authenticity:** signature verification is mandatory and constant-time (`hmac.compare_digest`); unsigned/invalid → `401`; the endpoint reads the raw body bytes **before** any JSON parsing (FastAPI `Request.body()`), since the HMAC is over exact bytes. Caddy must not rewrite/buffer-transform this path. Enforce a max body size (e.g. 25 MB) and per-installation rate limit.
- **Setup-callback CSRF:** the `install-url` route issues a single-use, workspace-scoped `state`; the setup-callback rejects a missing/mismatched/expired `state` (CSRF protection), and binds the installation only to the `state`-resolved workspace.
- **Secret storage & redaction:** the GitHub App private key lives in the AES-256-GCM envelope-encrypted, per-workspace-isolated secrets vault (`cross-cutting/F37-auth-secrets-byok`), loaded via env/secret mount, never written to a column or log. Installation tokens cached only in Redis with TTL and never returned by any API or written to audit (only `payload_hash` of webhook bodies persists). The canonical `SecretRedactor` (F37) strips `token`, `Authorization`, `private_key`, `client_secret`, `webhook_secret` from all structured logs/traces/audit; this slice registers no second redactor.
- **Least privilege / token binding:** installation tokens carry only the granted permissions; each token is used **only** against its own installation's repos (analogous to RFC 8707 resource binding the spec mandates for MCP — MCP's read-only default is N/A here since this slice exposes no MCP surface, but the least-privilege/token-binding principle is applied to installation tokens). The app should request the minimal permission set: `contents:read` (sync), `pull_requests:write` (PRs/reviews), `checks:read`/`statuses:read` (CI), `metadata:read`; webhook events limited to `pull_request`, `pull_request_review`, `check_suite`, `check_run`, `status`, `push`, `installation`, `installation_repositories`.
- **Policy gating of write actions:** `open_pr`, `update_pr`, `merge_pr`, `request_reviewers`, `add_labels` are agent-tool-equivalent calls and MUST pass `PolicyEvaluator.evaluate(ToolCall)` (F04) before execution — `open_pr` only when the task policy allows `open_pr`; the client refuses to push to / open PRs targeting a protected base/default branch unless explicitly allowed, and refuses any branch not matching the task `branch_prefix`. `merge_pr` carries **no REST endpoint** and is reachable only through F08's gated path; the human-approval-before-merge guarantee (`review_approved AND ci_green AND spec_validated`) is owned by F08's `MergeGateEvaluator` + the `cross-cutting/F36-human-approval-system` `pr` gate. As defense-in-depth, `merge_pr`'s `ToolCall` policy evaluation (F04) requires an `approval` grant, so a caller without a resolved F36 approval is refused before any GitHub call; the default V1 flow leaves merging to the human on GitHub, reconciled via the merged webhook.
- **RBAC:** connect/disconnect/sync require `admin`/`member`; `viewer` is read-only; `agent-runner` may invoke PR service calls but not installation management. Enforced via F37's `require_role(...)` dependency.
- **Audit immutability:** every outbound call + inbound accepted webhook is written through `cross-cutting/F39-audit-log`'s `AuditSink`/`SqlAuditWriter` (append-only, redact-before-persist, per-workspace hash chain) with redacted payload hash.
- **No outward calls in build/CI:** all clients run against `FixtureGitHubTransport`; CI asserts no real sockets (e.g. `pytest-socket` `disable_socket`).
- **Confused-deputy / SSRF:** repo clone URLs are constructed only from GitHub-returned `clone_url` + injected token; never from user-supplied URLs.

---

## 9. Effort estimate & risk (S/M/L + key risks)

**Effort: L** — spans a package (auth + transport + client + webhooks + PR rendering + sync), an API router (intake + admin routes), Celery tasks, a settings UI, fixtures, and a migration.

Key risks:
- **GitHub App auth correctness** (JWT clock skew, token refresh, 401 handling) — mitigated by injected-clock tests and an explicit invalidate-and-retry-once path.
- **Raw-body signature integrity** — easy to break if any middleware/proxy reparses or re-encodes the body; mitigated by reading `Request.body()` first and a Caddyfile assertion + a tampered-body test.
- **Webhook ordering / at-least-once delivery** — GitHub may deliver out of order or twice; mitigated by delivery-id idempotency plus on-demand reconciliation (`get_ci_status`, `list_reviews`, `get_pr`) so state never depends solely on a single webhook.
- **Fixture drift from the live GitHub API** — fixtures are recorded, not live; mitigated by capturing real payloads into the fixtures dir and flagging a post-merge live-verification task (out of overnight scope).
- **Merge semantics** — whether Forge merges via API or only reconciles human merges; resolved by defaulting to detect-via-webhook with an approval-gated `merge_pr` available but off by default.

---

## 10. Key files / paths (exact)

```
packages/contracts/forge_contracts/integration.py          # DTOs + GitHubIntegration/GitHubTransport Protocols (extend)
packages/db/forge_db/models/repository_connection.py       # extend columns
packages/db/forge_db/models/github_webhook_delivery.py     # new
packages/db/migrations/versions/<rev>_github_app_integration.py  # new migration (down_revision chains off foundation baseline)

packages/integration-sdk/forge_integrations/github/__init__.py
packages/integration-sdk/forge_integrations/github/auth.py
packages/integration-sdk/forge_integrations/github/transport.py
packages/integration-sdk/forge_integrations/github/client.py
packages/integration-sdk/forge_integrations/github/webhooks.py
packages/integration-sdk/forge_integrations/github/pr_body.py
packages/integration-sdk/forge_integrations/github/sync.py
packages/integration-sdk/forge_integrations/github/models.py
packages/integration-sdk/forge_integrations/github/errors.py
packages/integration-sdk/tests/test_auth.py
packages/integration-sdk/tests/test_webhooks.py
packages/integration-sdk/tests/test_client.py
packages/integration-sdk/tests/test_pr_body.py
packages/integration-sdk/tests/test_sync.py
packages/integration-sdk/tests/fixtures/github/webhooks/*.json
packages/integration-sdk/tests/fixtures/github/api/*.json

apps/api/forge_api/routers/integration.py                  # real handlers (Phase-0 stub)
apps/api/forge_api/settings.py                             # add GITHUB_APP_* settings
apps/api/tests/integration/test_github_webhook.py
apps/api/tests/integration/test_github_installations.py

apps/worker/forge_worker/tasks/github.py                   # process_github_webhook, sync_repository, refresh_installation_token
apps/worker/tests/test_github_tasks.py

apps/web/app/(settings)/integrations/github/page.tsx
apps/web/app/(settings)/integrations/github/callback/page.tsx
apps/web/components/integrations/GitHubConnectButton.tsx
apps/web/components/integrations/GitHubConnectionsTable.tsx
apps/web/components/integrations/SyncStatusBadge.tsx

deploy/docker-compose.yml                                  # GITHUB_APP_* env, forge_repos volume (F14-owned; F03 fills values)
deploy/caddy/Caddyfile                                     # webhook raw-body passthrough note (F14-owned)
deploy/.env.example                                        # GITHUB_APP_* + FORGE_PUBLIC_URL (FORGE_DATA_DIR owned by F14)
```

---

## 11. Research references (relevant links from the spec/research report)

- **Git integration = GitHub App** (best PR/CI/review event source; Open SWE uses the same approach): FORGE_SPEC.md Technology Stack row "Git integration → GitHub App"; research report Technology Recommendations table, "Git integration → GitHub App … Open SWE uses same approach.[cite:35]".
- **Open SWE** (isolated sandboxes per task, AGENTS.md repo-context loading, PR-opening workflows): https://www.langchain.com/blog/open-swe-an-open-source-framework-for-internal-coding-agents · https://github.com/langchain-ai/open-swe · deep dive https://byteiota.com/open-swe-langchain-autonomous-coding-agent/ (research report "Open SWE: What to Borrow").
- **Symphony — task-as-control-plane** (issue tracker as the control plane; workflow files move work through statuses; agents work in dedicated workspaces): https://openai.com/index/open-source-codex-orchestration-symphony/ · https://www.infoq.com/news/2026/05/openai-symphony-agents/.
- **Spec gating / traceability** (PR must cite acceptance criteria; requirement-to-diff/test traceability): FORGE_SPEC.md "Spec Gating Rules" + "Approval UI Must Show".
- **Workflow transitions** consuming GitHub signals (`open_pr_with_spec_traceability`, `ci_status_green`, `review_approved_by_human`): FORGE_SPEC.md Workflow DSL + Default Feature Workflow States.
- **Repo policy** for write/branch/review rules the client enforces: FORGE_SPEC.md "Repo Policy System" (`policy.yaml` `write_rules`, `review_rules`, `deploy_rules`).
- **Incremental knowledge sync via git diff / webhook**: FORGE_SPEC.md "Knowledge Sync Modes" (Incremental).
- **Security** (secrets at rest, audit every action, token binding, least privilege): FORGE_SPEC.md Security table + MCP Security Rules (token binding RFC 8707, read-only default — applied by analogy to installation tokens).
- **Docker Compose production practices** (pinned digests, healthchecks, named volumes for the repo-mirror volume): https://distr.sh/blog/running-docker-in-production/ (FORGE_SPEC.md self-hosting section).
- External canonical reference for the live-verification phase (not in spec, needed to implement against the real API): GitHub Apps authentication & webhooks docs — https://docs.github.com/apps · https://docs.github.com/webhooks.

---

## 12. Out of scope / future

- **Slack notifications & `/forge` slash commands, Email approvals/digests** — separate V1 integration features (`forge_integrations/slack`, email).
- **GitHub OAuth user sign-in** — belongs to `cross-cutting/F37-auth-secrets-byok` (this slice is App *installation* + machine auth, not human login). Note: this slice does enable the App's "request user authorization during installation" option purely to carry `state`/`code` on the setup-callback (§3.2); it does not own sign-in sessions.
- **External PM sync (Jira, Linear, Asana, Monday.com), GitLab** — V2; the `PMAdapter` Protocol surface lives elsewhere.
- **MCP `sync_and_index` mode, Datadog/Sentry/PagerDuty/Grafana** — V2.
- **Multi-repo task execution** across several `RepositoryConnection`s in one run — V2.
- **Deployment gates / environment promotion via GitHub Actions dispatch** — V3.
- **Container/Firecracker sandboxes** — V2/V3 (V1 uses git worktrees off the mirror produced here).
- **Self-service GitHub App registration via App Manifest flow** — V1 assumes a pre-registered App configured via env; manifest-driven onboarding is future.
- **Auto-merge by Forge as the default** — V1 default is human-merge-on-GitHub reconciled via webhook; `merge_pr` exists but is approval-gated and off by default.
