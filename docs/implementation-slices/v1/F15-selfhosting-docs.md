# F15 — Self-Hosting Documentation (`docs/self-hosting/`)

> Phase: v1 · Spec module(s): Self-Hosting and Deployment (FORGE_SPEC.md §"Self-Hosting and Deployment" → "Required Self-Hosting Documentation"), OSS Strategy, Monorepo `docs/self-hosting/` + `deploy/` · Status target: **Done** = the nine required documents (`quickstart`, `docker-compose`, `kubernetes`, `reverse-proxy`, `backup`, `restore`, `upgrade`, `security`, `troubleshooting`) plus an index exist under `docs/self-hosting/`, each carries valid front-matter, references **only real artifacts that exist in `deploy/`** and **CLI commands that exist**, and is **mechanically verified in CI**: a docs-coverage test asserts the manifest, every external/internal link resolves, every fenced shell snippet passes shellcheck, the documented compose/helm/proxy configs validate, the quickstart is exercised end-to-end to a healthy `/readyz`, a backup→restore drill round-trips real data, an upgrade+rollback drill round-trips a schema migration, and `security.md`'s env-var table is at parity with `deploy/.env.production.example`. Documentation drift is a CI failure, not a review nit.

---

## 1. Intent — what & why

Core design principle #9 is *"Self-hosting is first-class. `docker compose up` delivers a working platform."* and the OSS strategy promises a system a serious team can *"adopt, self-host, extend, audit, and fully trust in production."* That promise is only real if the operator-facing documentation is **correct, complete, and continuously true**. F15 is the slice that makes the self-hosting docs a *tested deliverable* rather than prose that rots the first time an image digest or env var changes.

F15 owns three things:

1. **The nine required documents** (FORGE_SPEC.md §"Required Self-Hosting Documentation") under `docs/self-hosting/`, written to a fixed structure with machine-readable front-matter, each scoped to one install path / lifecycle operation.
2. **A documentation contract** — a small Pydantic/manifest layer (`tests/docs/selfhosting_manifest.py`) declaring which files must exist, which headings each must contain, which real artifact/CLI strings each must reference, and which CI job verifies it.
3. **An executable verification harness** — a pytest module plus a GitHub Actions workflow (`.github/workflows/selfhosting-docs.yml`) that lint, link-check, shellcheck, config-validate, and *actually run* the documented procedures (quickstart smoke, backup/restore drill, upgrade drill) so the docs cannot silently diverge from `deploy/`.

**Ownership boundary (important — F13/F14 overlap).** Two sibling slices seed drafts of some of these docs: `v1/F13-local-quickstart` ships the initial `docs/self-hosting/quickstart.md` (and the `make` targets + `scripts/quickstart_smoke.sh` it documents), and `v1/F14-docker-compose-selfhost` ships initial drafts of the deploy-oriented guides (and its own `deploy/tests/test_compose_contract.py` + `test_docs_present.py` presence check). F15 is the **single slice accountable for all nine docs + the index meeting the documentation contract and being CI-verified end-to-end**: it authors the docs not seeded elsewhere (`reverse-proxy`, `backup`, `restore`, `upgrade`, `security`, `troubleshooting`, `README`), brings the F13/F14 drafts up to the contract, and owns the consolidated `selfhosting-docs.yml` workflow. To avoid re-verifying what those slices already verify, F15 **reuses** their harnesses (it invokes F13's `scripts/quickstart_smoke.sh` and F14's `deploy/tests/test_compose_contract.py` / `test_caddyfile.py` / `backup.sh` / `restore.sh` / `healthcheck.sh`) and adds only the genuinely-new layers: the docs-as-tests contract, a doc↔invariant parity check, the two `verify-*.sh` probes, and the upgrade drill.

F15 **consumes** the deploy artifacts (the production `docker-compose.yml`, `caddy/Caddyfile`, `nginx/forge.conf`, the V2-preview `helm/`, `scripts/{install,preflight,backup,restore,healthcheck}.sh`, `.env.production.example`) and the platform CLI (`forge-cli db migrate`, `forge-cli users create-admin`, `forge-cli vault export-key`/`import-key`). It does **not author** those artifacts — that is `v1/F14-docker-compose-selfhost`'s job (the dev compose + `make` targets + `.env.example` are `v1/F13-local-quickstart`'s). F15 *documents and tests* them, and adds two new verification scripts (`deploy/scripts/verify-restore.sh`, `deploy/scripts/verify-upgrade.sh`) that the drills and the docs both use. Without F15, a self-hoster following the README hits a wall at the first command whose flag changed, and the project's flagship "self-hosting first" claim is unverifiable.

---

## 2. User-facing behavior / journeys

The "users" of this slice are operators reading docs and the CI system verifying them.

**Evaluator (10-minute path).** Lands on `docs/self-hosting/quickstart.md`, runs `git clone … && cp .env.example .env && make setup && make dev`, opens `http://localhost:3000`, logs in with the seeded admin, sees a demo workspace. Every command in the doc was executed by CI against `HEAD`, so it works.

**Operator (single-node production).** Follows `docker-compose.md`: sizes the VM (4 vCPU / 8 GB / 50 GB per spec), installs Docker, fills `.env.production` from `.env.production.example`, runs `docker compose -f deploy/docker-compose.yml --env-file .env.production up -d`, then `docker compose exec api forge-cli db migrate` and `forge-cli users create-admin`. Pairs it with `reverse-proxy.md` (Caddy auto-HTTPS or Nginx) and `security.md` (hardening checklist + credential rotation). Sets up a cron from `backup.md`; knows the exact `restore.md` steps before an incident; upgrades safely with `upgrade.md` including a verified rollback.

**Platform engineer (Kubernetes, preview).** Reads `kubernetes.md`, sees the explicit **preview / V2** status banner (the production Helm chart is a V2 deliverable; `deploy/helm/` is out of scope for `v1/F14-docker-compose-selfhost`), follows the documented preview install with required values pointing at managed Postgres/Redis/S3, and verifies pod health. If a preview chart is present under `deploy/helm/` it is lint-validated in CI; otherwise the doc is a forward-pointer the spec mandates ship at launch.

**On-call (something is broken).** Opens `troubleshooting.md`, finds a symptom→cause→fix table keyed by service (`db`, `redis`, `minio`, `api`, `worker`, `mcp-gateway`, `web`, `caddy`) with the exact `docker compose logs` / health-probe commands to run (`GET /healthz` liveness, `GET /readyz` readiness, `deploy/scripts/healthcheck.sh`).

**Contributor / CI (the enforcement loop).** Opens a PR that bumps an image digest or renames an env var. The `selfhosting-docs` workflow fails because `security.md`'s env table is now out of parity, or the compose `config` step changed, or a snippet no longer shellchecks — forcing the docs to be updated in the same PR. Green CI = docs are true as of that commit.

---

## 3. Vertical slice

### 3.1 Data model (tables/columns/migrations touched)

**N/A — no schema changes.** F15 adds no tables or columns. The backup/restore and upgrade drills exercise the *existing* schema (the Phase-0 baseline + whatever migrations exist at `HEAD`); they seed and assert against existing models via `forge-cli` and SQL, and never define new ones. The only "data" F15 introduces is fixture/manifest data in the test tree (see §3.5).

### 3.2 Backend (FastAPI routes + services/packages)

**No new FastAPI routes or services.** F15 places two *requirements* on already-owned backend surfaces (it does not implement them; it asserts they exist and documents them):

- The quickstart/compose smoke tests depend on a liveness probe `GET /healthz` and a readiness probe `GET /readyz` on `apps/api` (liveness owned by the foundation slice `cross-cutting/C01-monorepo-and-api-foundations`; the readiness `/readyz` contract — `200` only when Postgres + Redis + MinIO are reachable, else `503 {"status":"not_ready","failed":[...]}` — is frozen in `v1/F14-docker-compose-selfhost` §3.2; documented and exercised here). `troubleshooting.md` and every healthcheck snippet reference these exact paths (`/healthz`, `/readyz`). `mcp-gateway` and `web` expose `GET /healthz` only.
- The docs reference the platform CLI `forge-cli`: subcommands `db migrate` / `db current` / `db check` are the foundation entrypoint (`cross-cutting/C01-monorepo-and-api-foundations`), while `users create-admin`, `vault export-key`, `vault import-key`, and `secrets rotate-key` come from `cross-cutting/F37-auth-secrets-byok`. F15's docs-coverage test asserts every `forge-cli` subcommand a doc references resolves (`forge-cli --help` lists it) so docs can never reference a command that does not exist.

The verification logic F15 *does* own lives outside the app runtime: a pytest module `tests/docs/test_selfhosting_docs.py` and two shell scripts under `deploy/scripts/` (§3.5). No request handlers, no DB sessions in app code.

### 3.3 Worker / agent runtime (Celery tasks, LangGraph, if any)

**N/A — no Celery tasks or LangGraph graphs.** F15 adds no worker code. The only worker-adjacent concern is operational and documented, not coded: `restore.md` and `upgrade.md` instruct the operator to **quiesce the worker first** (`docker compose stop worker mcp-gateway` before a restore so no in-flight agent run writes during the restore window), and the backup/restore drill in CI performs that quiesce step. These are documented procedures the drill asserts, not new tasks.

### 3.4 Frontend / UI (Next.js routes/components, if any)

**N/A — no Next.js routes or components.** The deliverables are plain Markdown rendered by the Git host (GitHub) and, optionally later, a docs site (out of scope, §12). The one UI touchpoint is documentary: `quickstart.md` and `docker-compose.md` tell the operator which URLs to open (`http://localhost:3000` web, `:8000` API) and what "working" looks like (login + demo workspace); the quickstart smoke test asserts the web container is reachable and `/readyz` is green, not any specific React component.

### 3.5 Infra / deploy (compose, helm, caddy, if any)

This is the core of the slice. F15 **consumes** (documents + validates) artifacts owned by `v1/F14-docker-compose-selfhost` (production) and `v1/F13-local-quickstart` (dev/quickstart) and **owns** the docs, the manifest, the verification scripts, and the CI workflow.

**Consumed (must exist; F15 references them by exact path and never duplicates their content):**

| Artifact | Owner slice | Documented by |
|---|---|---|
| `deploy/docker-compose.yml` (production single-node) | F14 | `docker-compose.md`, `upgrade.md` |
| `deploy/docker-compose.dev.yml` (used by `make dev`) | F13 | `quickstart.md` |
| `deploy/.env.example` (dev) | F13 | `quickstart.md` |
| `deploy/.env.production.example` (production) | F14 | `docker-compose.md`, `security.md` |
| `deploy/caddy/Caddyfile`, `deploy/nginx/forge.conf` | F14 | `reverse-proxy.md` |
| `deploy/helm/` (V2-preview chart, **may be absent at V1**) | F14 (V2) | `kubernetes.md` |
| `deploy/scripts/install.sh`, `preflight.sh`, `healthcheck.sh` | F14 | `docker-compose.md`, `troubleshooting.md` |
| `deploy/scripts/backup.sh`, `deploy/scripts/restore.sh` | F14 | `backup.md`, `restore.md` |
| `Makefile` (`setup`/`dev`/`migrate`/`seed`), `scripts/quickstart_smoke.sh` | F13 | `quickstart.md` |
| `deploy/tests/test_compose_contract.py`, `test_caddyfile.py` | F14 | reused by `compose-validate`/`proxy-validate` jobs |

**Owned (F15 creates these):**

- `docs/self-hosting/{README,quickstart,docker-compose,kubernetes,reverse-proxy,backup,restore,upgrade,security,troubleshooting}.md` — the nine required docs + an index. (`quickstart.md` originates as an F13 draft; F15 owns it meeting the contract.)
- `docs/self-hosting/_manifest.yaml` — machine-readable mirror of the doc contract (single source the test loads; see §4).
- `deploy/scripts/verify-restore.sh` — post-restore integrity probe (row counts vs the backup `manifest.json`, MinIO object count, admin login 200, `/readyz` green); used by both `restore.md` and the CI drill.
- `deploy/scripts/verify-upgrade.sh` — post-upgrade probe (migrations at head, `/readyz` green, sentinel row intact); used by `upgrade.md` and the CI drill.
- `tests/docs/selfhosting_manifest.py` + `tests/docs/test_selfhosting_docs.py` — the contract types + the docs-coverage/front-matter/env-parity/snippet tests.
- `tests/docs/check_doc_hardening_parity.py` — asserts every hardening bullet claimed in `docker-compose.md`/`security.md` maps to a named, passing test in F14's `deploy/tests/test_compose_contract.py` (so the docs cannot claim a posture the compose file does not enforce). F15 does **not** re-implement the structural compose checks — it depends on F14's suite as the source of truth.
- `.github/workflows/selfhosting-docs.yml` — the CI workflow with the lint/validate/drill jobs (invokes F14's contract suite and F13's smoke).
- Tooling config: `docs/self-hosting/.markdownlint.jsonc`, `.lychee.toml` (link-checker config with allow/ignore lists for rate-limited hosts), reused root `ruff`/`pytest` config.

**Networking/segmentation notes the docs must capture (from FORGE_SPEC.md §"Production Docker Compose Requirements"):** images pinned by `@sha256`, `willfarrell/autoheal` sidecar (the spec's `willfarrell/docker-autoheal`) with `autoheal=true` labels on every service except `autoheal` itself, named volumes (never bind mounts) for `db`/`redis`/`minio`, per-container limits via `deploy.resources.limits.{cpus,memory}`, healthchecks on every service, separate networks for API / database / MCP gateway / observability (`data` + `observability` networks `internal: true`; only `caddy` publishes `ports:`), non-root containers (`autoheal` the sole documented root exception), `daemon.json` log caps (`max-size: 100m`, `max-file: 5`), `--remove-orphans` on every `up`. `docker-compose.md` and `security.md` each carry these as a checklist; the `compose-validate` CI job runs F14's `deploy/tests/test_compose_contract.py` (the authoritative enforcer of pins/healthchecks/limits/networks/autoheal) plus `check_doc_hardening_parity.py` (doc↔invariant parity).

---

## 4. Public interfaces / contracts (exact signatures, Pydantic models, Protocols, YAML schemas)

The "interfaces" of a docs slice are (a) the doc contract the tests enforce, (b) the front-matter schema, (c) the env-parity contract, and (d) the CLI contracts of the two new verify scripts.

**(a) Doc contract — `tests/docs/selfhosting_manifest.py`**

```python
from __future__ import annotations
from datetime import date
from enum import Enum
from pydantic import BaseModel, Field

DOCS_ROOT = "docs/self-hosting"
DEPLOY_ROOT = "deploy"

class Audience(str, Enum):
    evaluator = "evaluator"
    operator = "operator"
    admin = "admin"
    contributor = "contributor"

class SelfHostingDocFrontMatter(BaseModel):
    """Parsed from the YAML front-matter block at the top of every doc."""
    model_config = {"extra": "forbid"}
    title: str = Field(min_length=3, max_length=80)
    summary: str = Field(min_length=10, max_length=200)
    audience: list[Audience] = Field(min_length=1)
    est_time_minutes: int | None = Field(default=None, ge=1, le=240)
    tested_against_version: str        # semver of the release the doc was last exercised against
                                       # (from deploy/VERSION if present, else the git tag); validated as a
                                       # well-formed version string only — not parity-checked against a file
    last_verified: date                # must be >= the file's last contract change
    verified_by_ci: str                # a job id in selfhosting-docs.yml, OR "none:<reason>"

class RequiredDoc(BaseModel):
    model_config = {"extra": "forbid"}
    filename: str                      # e.g. "quickstart.md"
    required_headings: list[str]       # exact "## ..." headings that must be present, in order
    must_reference: list[str] = []     # literal substrings that MUST appear (real paths / commands)
    forbidden: list[str] = []          # literal substrings that must NOT appear (e.g. "TBD", "TODO")
    verified_by_ci: str

# The single source of truth the test asserts against; _manifest.yaml mirrors this 1:1.
REQUIRED_DOCS: dict[str, RequiredDoc] = {
    "quickstart.md": RequiredDoc(
        filename="quickstart.md",
        required_headings=["## Prerequisites", "## Setup (one command)",
                           "## Verify it works", "## First login", "## Next steps"],
        must_reference=["make setup", "make dev", "cp .env.example .env",
                        "http://localhost:3000", "http://localhost:8000", "/readyz"],
        verified_by_ci="quickstart-smoke",
    ),
    "docker-compose.md": RequiredDoc(
        filename="docker-compose.md",
        required_headings=["## Sizing & prerequisites", "## Install Docker",
                           "## Configure .env.production", "## Bring up the stack",
                           "## Initialize the database", "## Verify health",
                           "## Service reference", "## Production hardening checklist",
                           "## Day-2 operations"],
        must_reference=["deploy/docker-compose.yml", ".env.production",
                        "forge-cli db migrate", "forge-cli users create-admin",
                        "--remove-orphans", "@sha256", "autoheal"],
        verified_by_ci="compose-validate",
    ),
    "kubernetes.md": RequiredDoc(
        filename="kubernetes.md",
        # Helm chart is a V2 roadmap item; deploy/helm/ may be absent at V1. This doc
        # ships at launch (spec-mandated) as a preview/forward-pointer; the "## Status"
        # banner MUST contain "preview" and "V2" (asserted by test_kubernetes_status_preview).
        required_headings=["## Status", "## Prerequisites", "## Install the chart (preview)",
                           "## Required values", "## External Postgres / Redis / S3",
                           "## Ingress & TLS", "## Verify", "## Upgrade"],
        must_reference=["deploy/helm", "helm install", "helm upgrade", "preview", "V2"],
        verified_by_ci="helm-lint",  # job lints deploy/helm IF a chart exists, else skips (see AC11)
    ),
    "reverse-proxy.md": RequiredDoc(
        filename="reverse-proxy.md",
        required_headings=["## Caddy (recommended, auto-HTTPS)", "## Nginx (alternative)",
                           "## WebSocket & SSE passthrough", "## Security headers",
                           "## Validate the config"],
        must_reference=["deploy/caddy/Caddyfile", "deploy/nginx/forge.conf",
                        "caddy validate", "nginx -t"],
        verified_by_ci="proxy-validate",
    ),
    "backup.md": RequiredDoc(
        filename="backup.md",
        required_headings=["## What to back up", "## Schedule", "## Run a backup",
                           "## Encryption", "## Offsite & retention", "## Verify a backup"],
        must_reference=["deploy/scripts/backup.sh", "pg_dump", "mc mirror",
                        "forge-cli vault export-key", "SECRET_KEY",
                        "deploy/scripts/verify-restore.sh"],
        verified_by_ci="backup-restore-drill",
    ),
    "restore.md": RequiredDoc(
        filename="restore.md",
        required_headings=["## Preconditions", "## Quiesce services", "## Restore Postgres",
                           "## Restore object storage", "## Restore secrets key",
                           "## Run migrations", "## Verify integrity", "## Resume"],
        must_reference=["deploy/scripts/restore.sh", "docker compose stop worker",
                        "forge-cli vault import-key",
                        "deploy/scripts/verify-restore.sh", "forge-cli db migrate"],
        verified_by_ci="backup-restore-drill",
    ),
    "upgrade.md": RequiredDoc(
        filename="upgrade.md",
        required_headings=["## Before you upgrade", "## Take a backup", "## Pull new images",
                           "## Apply migrations", "## Verify", "## Rollback"],
        must_reference=["deploy/scripts/backup.sh", "@sha256", "forge-cli db migrate",
                        "deploy/scripts/verify-upgrade.sh", "alembic downgrade"],
        verified_by_ci="upgrade-drill",
    ),
    "security.md": RequiredDoc(
        filename="security.md",
        required_headings=["## Secrets & BYOK", "## Credential rotation",
                           "## Network segmentation", "## Container hardening",
                           "## TLS", "## RBAC roles", "## Audit log",
                           "## MCP read-only defaults", "## Secret redaction",
                           "## Hardening checklist", "## Environment variable reference"],
        must_reference=["admin", "member", "viewer", "agent-runner",
                        "allow_write: false", "RFC 8707", "non-root"],
        verified_by_ci="env-parity",
    ),
    "troubleshooting.md": RequiredDoc(
        filename="troubleshooting.md",
        required_headings=["## How to read this", "## db", "## redis", "## minio",
                           "## api", "## worker", "## mcp-gateway", "## web", "## caddy",
                           "## Getting help"],
        must_reference=["docker compose logs", "/healthz", "/readyz",
                        "deploy/scripts/healthcheck.sh"],
        verified_by_ci="none:manual table reviewed each release; links are link-checked",
    ),
    "README.md": RequiredDoc(
        filename="README.md",
        required_headings=["## Choose your install path", "## Documents"],
        must_reference=["quickstart.md", "docker-compose.md", "kubernetes.md"],
        verified_by_ci="docs-coverage",
    ),
}

GLOBAL_FORBIDDEN = ["TBD", "TODO", "FIXME", "XXX", "lorem ipsum", "<placeholder>"]
```

**(b) Front-matter block** — every doc begins with:

```yaml
---
title: Local Quickstart
summary: Stand up a working Forge on a laptop in under 10 minutes.
audience: [evaluator, contributor]
est_time_minutes: 10
tested_against_version: 0.1.0
last_verified: 2026-06-26
verified_by_ci: quickstart-smoke
---
```

**(c) Env-parity contract** — `security.md` must contain a table whose first column is the env-var name. The test parses every `KEY=` from `deploy/.env.production.example` and the var names from that table and asserts set-equality, modulo an explicit allowlist of non-secret tuning vars maintained in the manifest:

```python
# Non-secret tuning vars documented in docker-compose.md, not security.md's secret table.
# Mirror deploy/.env.production.example (F14 §4.2) — keep in sync when F14 adds tuning knobs.
ENV_DOC_ALLOWLIST: set[str] = {"LOG_LEVEL", "FORGE_ENV", "AUTOHEAL_INTERVAL", "AUTOHEAL_START_PERIOD"}
# parity rule, asserted in test (every *secret* var must appear in security.md):
#   keys(.env.production.example) - ENV_DOC_ALLOWLIST  ==  vars(security.md table)
```

**(d) Verify-script CLIs** (POSIX `sh`, set `-euo pipefail`):

```text
deploy/scripts/verify-restore.sh --manifest <archive>/manifest.json \
                                 [--api-url http://localhost:8000] [--admin-email <e>]
  exit 0  restore verified: Postgres row counts == manifest, MinIO object count == manifest,
          admin login returns 200, /readyz green
  exit 2  mismatch (prints which check failed: rows|objects|login|ready)
  exit 3  inputs missing/corrupt (manifest unreadable, services unreachable)

deploy/scripts/verify-upgrade.sh [--api-url http://localhost:8000] [--sentinel-id <uuid>]
  exit 0  alembic at head, /readyz green, sentinel row present & unchanged
  exit 2  not-at-head | unhealthy | sentinel changed/missing
  exit 3  inputs missing/corrupt
```

`v1/F14-docker-compose-selfhost` already specifies that `backup.sh` writes a `manifest.json` into its checksummed archive (today it carries `created_at`, archive checksums, and sha256 hashes of secret material — never plaintext). **F15 extends that manifest contract** to additionally require `version`, `pg_row_counts: {table: int}`, and `minio_object_count: int`, so `verify-restore.sh` has a ground truth to assert against. F14 implements the emission; F15 owns the `verify-restore.sh` consumer and the field requirement (asserted by `test_backup_manifest_schema`).

---

## 5. Dependencies — features/slices that must exist first

> Slug reconciliation: sibling slices reference the un-numbered platform foundation variously (`v1/F00-foundation-substrate`, `cross-cutting/C01-monorepo-and-api-foundations`); both denote the same Phase-0 substrate and no dedicated file exists yet. The authoritative auth/secrets/RBAC slug is **`cross-cutting/F37-auth-secrets-byok`** (it explicitly supersedes the stale `v1/F15-auth-secrets-rbac` reference seen in F14/F30), the audit log is **`cross-cutting/F39-audit-log`**, and observability is **`cross-cutting/F38-observability-cost-metrics`**. All other refs below match real files under `docs/implementation-slices/`.

**Hard prerequisites (F15's tests cannot pass without these):**

- `cross-cutting/C01-monorepo-and-api-foundations` (a.k.a. `v1/F00-foundation-substrate`) — **REQUIRED**. Provides the monorepo, `apps/api` skeleton with `/healthz` (liveness) + `/readyz` (readiness), the `forge-cli` base entrypoint (`db migrate`/`db current`/`db check`), and `packages/db` Alembic baseline. Docs-coverage CLI checks and the readiness/liveness snippets bind to these exact names.
- `v1/F13-local-quickstart` — **REQUIRED**. Owns the `Makefile` (`setup`/`dev`/`migrate`/`seed`/`reset`/`doctor`), `deploy/docker-compose.dev.yml`, `deploy/.env.example`, the deterministic demo-seed orchestrator (`make seed` → `python -m forge_api.scripts.seed`, no model-provider key required), `scripts/quickstart_smoke.sh` (the <10-min timed smoke), and the initial `docs/self-hosting/quickstart.md`. F15's `quickstart-smoke` job **invokes F13's `scripts/quickstart_smoke.sh`** and adds a doc↔script parity assertion; F15 does not re-implement the smoke.
- `v1/F14-docker-compose-selfhost` — **REQUIRED**. Owns `deploy/docker-compose.yml` (sha256 pins, `willfarrell/autoheal`, healthchecks, `deploy.resources.limits`, segmented networks, non-root), `deploy/caddy/Caddyfile`, `deploy/nginx/forge.conf`, `deploy/.env.production.example`, `deploy/scripts/{install,preflight,backup,restore,healthcheck,pin-digests}.sh`, `deploy/docker/daemon.json`, the compose contract suite (`deploy/tests/test_compose_contract.py`, `test_caddyfile.py`, `test_docs_present.py`), and the archive `manifest.json` (extended per §4d). F15 documents and validates these and **reuses** F14's contract/caddy tests + `backup.sh`/`restore.sh`/`healthcheck.sh`; if a path here changes, F15's `must_reference` and validate jobs fail until docs follow.

**Soft / integration dependencies (F15 documents them; they need not be complete for F15 to build, but the relevant doc section is marked preview/from-spec until they are):**

- `cross-cutting/F37-auth-secrets-byok` — **SOFT** (authoritative auth/secrets/RBAC slug). `security.md` documents the AES-256-GCM per-workspace encrypted vault, BYOK key storage, the four RBAC roles (admin/member/viewer/agent-runner), the `SecretRedactor`, and credential rotation; `forge-cli users create-admin`, `vault export-key`/`import-key`, and `secrets rotate-key` come from here. If absent, the vault/RBAC subsections are written against the spec and flagged.
- `cross-cutting/F39-audit-log` — **SOFT**. `security.md`'s `## Audit log` heading documents the immutable, tamper-evident per-workspace hash-chained `audit_log` (tool name, payload hash, result status, latency); `troubleshooting.md` references where audit/log data lives. Documented from spec if not yet wired.
- `cross-cutting/F38-observability-cost-metrics` — **SOFT**. The optional V2 observability profile (`prometheus`/`grafana`/`loki`, `temporal`) is referenced by `security.md`/`troubleshooting.md` as `profiles:`-gated, off by default.
- `cross-cutting/F36-human-approval-system` + `v1/F08-plan-execute-verify-pr-approval` — **SOFT**. `security.md`'s trust-model note documents that human-approval-before-merge and spec-gated implementation are platform-enforced guarantees an operator inherits and cannot configure away (cross-link only).
- Helm chart (production) — **SOFT/PREVIEW, V2**. No dedicated slice file exists; `deploy/helm/` is explicitly out of scope for F14 (V2 per roadmap). `kubernetes.md` must ship at V1 (spec mandates it) as a **preview/forward-pointer**; the `## Status` heading carries the `preview`/`V2` banner. `helm-lint` lint-validates a chart only if `deploy/helm/Chart.yaml` is present, otherwise it skips (AC11).
- `v1/F03-github-app` — **SOFT**. Cross-link only: `quickstart.md`/`security.md` point to GitHub App setup, and `GITHUB_APP_*` env vars appear in the env-parity table. Not a build dependency.

**Downstream consumers (not prerequisites):** the top-level project README (authored alongside F13/F14) links this self-hosting set; the release process (out of scope) gates a tag on the `selfhosting-docs` workflow being green. (There is no separate `examples-docs` slice in the v1 set — slices run F01–F16.)

---

## 6. Acceptance criteria (numbered, testable)

1. All ten files in `REQUIRED_DOCS` exist under `docs/self-hosting/`; a missing file fails `test_all_required_docs_present`.
2. Every doc parses a valid `SelfHostingDocFrontMatter` block (extra keys forbidden); a malformed/missing block fails `test_front_matter_valid`.
3. Every doc contains all of its `required_headings`, in the declared order; a missing/out-of-order heading fails `test_required_headings`.
4. Every doc contains all of its `must_reference` substrings; e.g. `docker-compose.md` literally contains `deploy/docker-compose.yml`, `forge-cli db migrate`, `@sha256`, `--remove-orphans`. A renamed artifact that breaks a reference fails `test_must_reference`.
5. No doc contains any `GLOBAL_FORBIDDEN` marker (`TBD`/`TODO`/`FIXME`/placeholder); fails `test_no_filler`.
6. Every relative link/path a doc references that points into the repo (`deploy/...`, `docs/...`, sibling `*.md`) resolves to an existing file; fails `test_internal_links_resolve`.
7. Every external URL in every doc returns a non-4xx/5xx status (via `lychee`, retries + allowlist for known rate-limited hosts); broken links fail the `link-check` job.
8. Every fenced ```bash/```sh block passes `shellcheck` (extracted by the snippet harness; non-runnable illustrative blocks must be tagged ```text); a syntax error fails `snippet-shellcheck`.
9. `forge-cli --help` (and nested `--help` for groups) lists every `forge-cli` subcommand any doc references — `db migrate`, `users create-admin`, `vault export-key`, `vault import-key`, `secrets rotate-key`; if a doc references a subcommand not present, `test_documented_cli_exists` fails.
10. `docker compose -f deploy/docker-compose.yml --env-file deploy/.env.production.example config -q` exits 0 (doc-command validity), **and** F14's `deploy/tests/test_compose_contract.py` is green (the authoritative enforcer of `@sha256` pins, per-service healthchecks, `deploy.resources.limits.{cpus,memory}`, segmented internal networks, non-root, and `autoheal=true` labelling), **and** `check_doc_hardening_parity.py` confirms every hardening bullet in `docker-compose.md`/`security.md` maps to a named test in that suite. Any of the three failing fails `compose-validate`. (F15 does not duplicate the structural checks; it depends on F14's suite.)
11. **Helm (conditional, V2-preview).** If `deploy/helm/Chart.yaml` exists, `helm lint deploy/helm` and `helm template deploy/helm -f deploy/helm/values.example.yaml` exit 0 (`helm-lint`); if it is absent (the V1 default — Helm is V2), `helm-lint` skips the lint step and passes. Independently, `test_kubernetes_status_preview` asserts `kubernetes.md` `## Status` contains both `preview` and `V2`.
12. `caddy validate --config deploy/caddy/Caddyfile` and `nginx -t -c <(envsubst < deploy/nginx/forge.conf)` exit 0; fails `proxy-validate`.
13. **Quickstart drill:** the `quickstart-smoke` job invokes F13's `scripts/quickstart_smoke.sh` on a clean checkout (which runs the documented `cp .env.example .env && make setup` path, brings infra up detached with `--wait`, and polls `GET /readyz` until 200), and additionally asserts that the literal command sequence in `quickstart.md` matches the steps the script runs (`test_quickstart_doc_matches_script`). The script enforces F13's <10-min budget (warning at >600s, hard-fail at its ceiling). Fails `quickstart-smoke`.
14. **Backup/restore drill:** seed deterministic demo data via `make seed` (1 workspace, 1 admin, N tasks; then put K known MinIO objects) → F14's `deploy/scripts/backup.sh` (writes the archive `manifest.json` with `pg_row_counts` + `minio_object_count` per §4d) → `docker compose down -v` (drop volumes) → recreate → F14's `deploy/scripts/restore.sh` → `forge-cli db migrate` → `deploy/scripts/verify-restore.sh --manifest <archive>/manifest.json` exits 0 (row counts, object count, admin login, `/readyz` all match). Fails `backup-restore-drill`.
15. **Upgrade drill:** bring up the stack at the previous tagged image set, seed a sentinel row, run the documented upgrade (pull new `@sha256` set, `forge-cli db migrate`), `deploy/scripts/verify-upgrade.sh` exits 0; then run the documented rollback (F14 `restore.sh` of the pre-upgrade backup + `alembic downgrade`) and assert `/readyz` green and sentinel intact. Fails `upgrade-drill`.
16. **Env parity:** `keys(deploy/.env.production.example) − ENV_DOC_ALLOWLIST == vars(security.md table)`; an undocumented secret or a documented-but-removed var fails `test_env_parity`.
17. **No leaked secrets:** `gitleaks detect` over `docs/self-hosting/` and `deploy/*.example` finds zero findings; example files contain only placeholders (`change-me`, empty, or obvious dummies). Fails `secret-scan`.
18. Each doc's `verified_by_ci` value is either a real job id in `.github/workflows/selfhosting-docs.yml` or starts with `none:` plus a justification; fails `test_verified_by_ci_mapping`.
19. The committed `docs/self-hosting/_manifest.yaml` deserializes 1:1 into `REQUIRED_DOCS` (drift guard between the human-readable manifest and the test source); fails `test_manifest_matches_code`.
20. The `selfhosting-docs` workflow is required on PRs touching `docs/self-hosting/**`, `deploy/**`, or `.env*.example` (path filter present in the workflow); a PR changing those without the workflow running is a misconfiguration caught by `test_workflow_path_filters` (parses the YAML).

### Traceability requirement → criteria

| Spec requirement | Criteria |
|---|---|
| All 9 required docs exist | 1, 3 |
| quickstart under 10 minutes | 13 |
| docker-compose production guide (hardening) | 4, 10 |
| kubernetes (Helm install & values) | 11 (conditional lint + V2-preview banner) |
| reverse-proxy (Caddy + Nginx) | 12 |
| backup (Postgres + MinIO + secrets) | 14, 16 |
| restore (full restore with verification) | 14 |
| upgrade (safe upgrade + rollback) | 15 |
| security (hardening, rotation, network) | 16, 17 |
| troubleshooting (common errors) | 3 (headings), 6/7 (links) |
| Docs reference only real artifacts/CLI | 4, 6, 9, 19 |
| Docs stay true over time (anti-drift) | 5, 7, 8, 10–18, 20 |

---

## 7. Test plan (TDD) — concrete test cases (unit + integration), key fixtures

Write the manifest + tests first; author each doc until its assertions pass (docs-as-tests). Layout: contract/tests under `tests/docs/`, CI under `.github/workflows/`, drills reuse `deploy/scripts/`.

**Shared fixtures (`tests/docs/conftest.py`):**
- `docs_dir` → `Path("docs/self-hosting")`; `deploy_dir` → `Path("deploy")`.
- `parsed_docs` → `dict[str, tuple[SelfHostingDocFrontMatter, str]]` (front-matter + body) for all required docs, parsed once.
- `bad_fixtures` → `Path("tests/docs/fixtures")` holding the negative cases the unit tests assert against (a doc with an extra front-matter key, a dangling internal link, an un-excluded broken shell snippet, an env-table with a missing/stale var).
- Drill seeding uses F13's `make seed` (deterministic, no model keys); the **ground truth** for `verify-restore.sh` is the backup archive's `manifest.json` (§4d), not a pre-authored fixture. The K MinIO objects are put with a small documented loop the drill captures.
- `compose_file` / `helm_dir` / `caddyfile` / `nginx_conf` path fixtures.

**Unit — manifest & front-matter (`tests/docs/test_selfhosting_docs.py`, pure, no Docker):**
- `test_all_required_docs_present` (AC1) — every key in `REQUIRED_DOCS` exists on disk.
- `test_front_matter_valid` (AC2) — parametrized over docs; assert `SelfHostingDocFrontMatter` parses; inject a doc fixture with an extra key → `ValidationError`.
- `test_required_headings` (AC3) — body contains each heading; order preserved via index monotonicity.
- `test_must_reference` (AC4) — each literal substring present.
- `test_no_filler` (AC5) — no `GLOBAL_FORBIDDEN` token (case-insensitive, word-boundary for `XXX`).
- `test_verified_by_ci_mapping` (AC18) — value ∈ workflow job ids ∪ `none:*`.
- `test_manifest_matches_code` (AC19) — `yaml.safe_load(_manifest.yaml)` round-trips into `REQUIRED_DOCS`.
- `test_workflow_path_filters` (AC20) — parse `selfhosting-docs.yml`, assert `on.pull_request.paths` includes `docs/self-hosting/**`, `deploy/**`, `.env*.example`.
- `test_kubernetes_status_preview` (AC11) — `kubernetes.md` `## Status` body contains both `preview` and `V2`.
- `test_quickstart_doc_matches_script` (AC13) — the ordered command lines in `quickstart.md` are a superset-consistent match for the steps F13's `scripts/quickstart_smoke.sh` executes (no doc step the script omits; no script step the doc hides).
- `test_backup_manifest_schema` (AC14, §4d) — a sample `manifest.json` from a drill run deserializes with `version`, `pg_row_counts`, `minio_object_count` present and typed.

**Unit — references & parity (same module):**
- `test_internal_links_resolve` (AC6) — regex-extract `(...)` link targets + bare `deploy/…`/`docs/…` paths; assert each repo-relative target exists. Fixture with a dangling link → fail.
- `test_documented_cli_exists` (AC9) — run `forge-cli --help`, extract subcommands; assert every CLI string referenced in docs (matched against a `KNOWN_CLI_PREFIXES` allowlist like `forge-cli `, `make `, `docker compose `) that is a `forge-cli` subcommand is present.
- `test_env_parity` (AC16) — parse `.env.production.example` keys + `security.md` table; assert equality modulo `ENV_DOC_ALLOWLIST`. Two negative fixtures: add an env var (missing-from-docs → fail), add a doc-only var (stale → fail).

**Unit — snippet harness (`tests/docs/test_snippets.py`):**
- `test_bash_snippets_shellcheck` (AC8) — extract all ` ```bash `/` ```sh ` blocks, write each to a temp file, run `shellcheck -S warning`; assert exit 0. A block tagged ` ```text ` is skipped. Negative fixture: an intentionally broken snippet flagged via a magic comment is excluded; an un-excluded broken block fails.

**Integration — config validation (CI jobs; can run locally with the tools installed):**
- `compose-validate` (AC10) — `docker compose -f deploy/docker-compose.yml --env-file deploy/.env.production.example config -q` (doc-command validity) → `pytest deploy/tests/test_compose_contract.py` (F14's authoritative structural enforcer; reused, not duplicated) → `python tests/docs/check_doc_hardening_parity.py` (every hardening bullet in the docs maps to a named contract test).
- `helm-lint` (AC11) — **conditional**: if `deploy/helm/Chart.yaml` exists, `helm lint deploy/helm` + `helm template deploy/helm -f deploy/helm/values.example.yaml`; else skip the lint step (Helm is V2). The `test_kubernetes_status_preview` unit assertion runs regardless.
- `proxy-validate` (AC12) — reuse F14's `deploy/tests/test_caddyfile.py` (`caddy validate`) + `nginx -t` against an env-substituted temp copy of `deploy/nginx/forge.conf`.
- `link-check` (AC7) — `lychee --config .lychee.toml docs/self-hosting/**`.
- `secret-scan` (AC17) — `gitleaks detect --no-git --source docs/self-hosting deploy` with a config allowing placeholder values.

**Integration — drills (CI, Docker-in-Docker; the load-bearing tests). Every drill runs in its own ephemeral compose project; see drill isolation below):**
- `quickstart-smoke` (AC13) — clean checkout → run F13's `scripts/quickstart_smoke.sh` (it performs `cp .env.example .env && make setup`, `docker compose -f deploy/docker-compose.dev.yml up -d --wait`, and polls `:8000/readyz`); F15 additionally runs `test_quickstart_doc_matches_script`. Tear down with `docker compose down -v --remove-orphans`. (F15 does not re-author the wait loop; `make dev` is foreground and is documented, not invoked, in CI.)
- `backup-restore-drill` (AC14) — `up -d` → `forge-cli db migrate` → `make seed` + put K MinIO objects → `bash deploy/scripts/backup.sh --env-file .env.ci --out $TMP` (writes `$TMP/<archive>/manifest.json` with row/object counts) → `docker compose down -v` (destroys volumes) → recreate `up -d` → `bash deploy/scripts/restore.sh --env-file .env.ci --archive $TMP/<archive>` → `forge-cli db migrate` → `bash deploy/scripts/verify-restore.sh --manifest $TMP/<archive>/manifest.json` (assert exit 0).
- `upgrade-drill` (AC15) — `up` at `PREV_TAG` image set → seed sentinel → switch to HEAD's pinned `@sha256` set (`.env.production`/compose) → `docker compose pull && up -d --remove-orphans` → `forge-cli db migrate` → `verify-upgrade.sh` (exit 0) → rollback path: F14 `restore.sh` of the pre-upgrade backup + `alembic downgrade` → assert `/readyz` 200 + sentinel intact.

**Drill isolation:** every drill runs in its own ephemeral compose project (`-p forge_ci_<job>_<sha>`) with a dedicated `.env.ci` (random `SECRET_KEY`/passwords, ports offset) so destructive `down -v` never touches anything outside the job. No drill talks to external model providers (BYOK keys empty; agent runtime not exercised — these tests verify *infra*, not agents).

---

## 8. Security & policy considerations

- **No real secrets in docs or examples (AC17).** Every credential in `docs/self-hosting/*` and `deploy/*.example` is a placeholder; `gitleaks` runs in CI over both. `security.md` instructs operators to generate `SECRET_KEY`/`AUTH_SECRET` with `openssl rand -hex 32` and never commit a populated `.env*` (reinforced by the existing `.gitignore` rules `!.env.example`, `!.env.production.example`).
- **Backup of the encryption key is a documented, scary, explicit step.** Because BYOK secrets are AES-256-GCM envelope-encrypted at rest (per-workspace key isolation, `cross-cutting/F37-auth-secrets-byok`) under `SECRET_KEY`, a database restore is useless without the same key. `backup.md`/`restore.md` make this a first-class item using the real CLI: `forge-cli vault export-key` writes the master key material *separately* from the DB dump (its own encrypted artifact, e.g. GPG-to-an-offline-recipient), and `restore.md` runs `forge-cli vault import-key` in its "restore secrets key" step **before** "run migrations". F14's `backup.sh`/`restore.sh` already invoke these. The drill seeds an encrypted secret and `verify-restore.sh` confirms it decrypts post-restore (admin login exercises the vault).
- **Least-privilege drill credentials.** Drills use throwaway random passwords and never the real provider keys; the worker/MCP gateway are quiesced during restore so no agent action runs against half-restored state.
- **Hardening documented matches what's enforced (AC10).** The compose-validate job prevents `docker-compose.md` from claiming "non-root, pinned, healthchecked, segmented" while the real compose file drifts — the security posture is asserted, not asserted-in-prose.
- **MCP & RBAC posture is documented from the spec's security rules.** `security.md` states MCP `allow_write: false` default (enabling write requires explicit admin action), RFC 8707 `resource`-parameter token binding, per-connection namespace scoping, secret redaction from logs/traces/retrieval, and the four RBAC roles — pulled verbatim from FORGE_SPEC.md §Security / §"MCP Security Rules" so operators inherit the platform's guarantees.
- **Platform trust model (non-negotiables) is stated so operators know what is not configurable away.** `security.md` notes that human-approval-before-merge and spec-gated implementation are platform-enforced (`cross-cutting/F36-human-approval-system`, `v1/F08-plan-execute-verify-pr-approval`), that every agent action / tool call / MCP call / approval is written to the immutable, tamper-evident audit log (`cross-cutting/F39-audit-log`), and that BYOK keys never leave the operator's vault. These are documented as guarantees, not toggles, so a self-hoster understands the trust posture they inherit.
- **Credential rotation runbook.** `security.md` includes step-by-step rotation for `SECRET_KEY` (re-encrypting all secrets to the new key via `forge-cli secrets rotate-key`), DB password, GitHub App private key + webhook secret, and provider/BYOK keys, including which services to restart and the order that avoids downtime.
- **Supply-chain note.** `docker-compose.md`/`upgrade.md` document verifying image digests against the release notes (the `@sha256` pins) so an operator can confirm they ran the published artifact, not a mutated tag.

---

## 9. Effort estimate & risk (S/M/L + key risks)

**Effort: M.** The prose is bounded (ten focused docs from a clear spec list), and the heaviest infra pieces are **reused, not rebuilt**: `quickstart-smoke` invokes F13's `scripts/quickstart_smoke.sh` and `compose-validate`/`proxy-validate`/`backup-restore-drill` reuse F14's contract suite + `backup.sh`/`restore.sh`/`healthcheck.sh`. The genuinely-new work is the docs-as-tests contract + manifest, the snippet extractor + shellcheck, the env-parity parser, `check_doc_hardening_parity.py`, the two `verify-*.sh` probes, and the one net-new Docker-in-Docker **upgrade drill**. Estimate ≈ 4–6 engineer-days: ~1.5 for docs, ~2 for the contract/harness + CI wiring, ~1 for `verify-*.sh` + the upgrade drill + flake-hardening.

**Key risks & mitigations:**
- **Docs drift from `deploy/` (the whole reason this slice exists).** *Mitigation:* the `must_reference`/compose-validate/env-parity assertions make drift a red build; the path-filtered workflow (AC20) guarantees it runs whenever `deploy/**` or `.env*.example` change.
- **Drill flakiness (image pulls, slow CI runners, port races).** *Mitigation:* per-job compose projects + offset ports, generous `timeout`s with functional success as the pass condition (timing is a soft warning, not a hard fail except the 20-min ceiling), retries on pulls, and `down -v` in a `finally`/`always()` step.
- **Destructive `down -v` blast radius.** *Mitigation:* dedicated `-p` project names and `.env.ci`; never run drills against the default project.
- **Kubernetes/Helm is V2 (no chart at V1) but the spec mandates `kubernetes.md` at launch.** *Mitigation:* ship the doc as a preview/forward-pointer with an explicit `## Status` banner containing `preview`+`V2` (asserted by `test_kubernetes_status_preview`); `helm-lint` validates a chart only **if** `deploy/helm/Chart.yaml` exists and skips otherwise, so no AC depends on an artifact that does not yet exist. The production chart and full support land in V2 (§12).
- **External link rot (AC7).** *Mitigation:* `lychee` with retry/backoff, a maintained ignore-list for rate-limited hosts, and link-check as a *non-blocking-on-network-flake but blocking-on-404* job (distinguish 4xx from transient via retries).
- **Quickstart 10-minute target is machine-dependent.** *Mitigation:* assert functional success always; treat the 10-min budget as a warning annotation on the reference runner, not a hard gate.

---

## 10. Key files / paths (exact)

**docs/self-hosting/ (owned, new):**
- `README.md` (index / choose-your-path)
- `quickstart.md`, `docker-compose.md`, `kubernetes.md`, `reverse-proxy.md`
- `backup.md`, `restore.md`, `upgrade.md`, `security.md`, `troubleshooting.md`
- `_manifest.yaml` (mirrors `REQUIRED_DOCS`)
- `.markdownlint.jsonc`, `.lychee.toml`

**tests/docs/ (owned, new):**
- `selfhosting_manifest.py` (`SelfHostingDocFrontMatter`, `RequiredDoc`, `REQUIRED_DOCS`, `ENV_DOC_ALLOWLIST`, `GLOBAL_FORBIDDEN`)
- `test_selfhosting_docs.py`, `test_snippets.py`, `check_doc_hardening_parity.py`
- `conftest.py`
- `fixtures/` (negative cases for the unit tests: bad front-matter, dangling link, broken snippet, env-parity mismatch)

**deploy/scripts/ (owned, new — verification helpers used by docs + drills):**
- `verify-restore.sh`, `verify-upgrade.sh`

**deploy/ (consumed — must exist; documented & validated, not authored here; owners: F13=dev/quickstart, F14=production):**
- `docker-compose.yml` (F14), `docker-compose.dev.yml` (F13), `.env.example` (F13), `.env.production.example` (F14)
- `caddy/Caddyfile` (F14), `nginx/forge.conf` (F14), `helm/` (V2-preview, may be absent at V1)
- `scripts/install.sh`, `scripts/preflight.sh`, `scripts/healthcheck.sh`, `scripts/backup.sh`, `scripts/restore.sh` (all F14)
- `deploy/tests/test_compose_contract.py`, `deploy/tests/test_caddyfile.py` (F14 — reused by F15's validate jobs)
- `Makefile` (`setup`/`dev`/`migrate`/`seed`), `scripts/quickstart_smoke.sh` (F13 — reused by `quickstart-smoke`)

**.github/workflows/ (owned, new):**
- `selfhosting-docs.yml` (jobs: `docs-coverage`, `link-check`, `snippet-shellcheck`, `env-parity`, `secret-scan`, `compose-validate`, `helm-lint`, `proxy-validate`, `quickstart-smoke`, `backup-restore-drill`, `upgrade-drill`)

**root (consumed):** `Makefile` (`setup`/`dev`/`migrate`/`seed`), `.env.example`, `.gitignore` (already excludes populated env files).

---

## 11. Research references (relevant links from the spec/research report)

- FORGE_SPEC.md §"Self-Hosting and Deployment" — three install paths, local quickstart, docker-compose production sequence, service list, "Production Docker Compose Requirements", and the authoritative "Required Self-Hosting Documentation (must ship at launch)" nine-file list.
- FORGE_SPEC.md §"Monorepo Structure" — `docs/self-hosting/` and `deploy/` layout (compose files, `helm/`, `caddy/Caddyfile`, `nginx/forge.conf`, `scripts/{install,backup,restore}.sh`).
- FORGE_SPEC.md §Security + §"MCP Security Rules" — content source for `security.md` (secrets at rest, RBAC roles, audit log, MCP read-only default, RFC 8707 token binding, secret redaction).
- forge-research-report.md §"Self-Hosting and Deployment" — Compose-in-production caveats (autoheal, pinned digests, named volumes, network segmentation, Watchtower-style updates) and the Compose→Kubernetes escalation rationale.
- Docker Compose in production 2026: https://distr.sh/blog/running-docker-in-production/
- Docker Compose best practices: https://nickjanetakis.com/blog/best-practices-around-production-ready-web-apps-with-docker-compose
- Self-hosting doc structure references — Langfuse: https://langfuse.com/self-hosting/deployment/docker-compose · Trigger.dev: https://trigger.dev/docs/self-hosting/docker · Plane.so: https://developers.plane.so/self-hosting/methods/docker-compose
- Local Kubernetes guide (for `kubernetes.md` preview): https://www.plural.sh/blog/local-kubernetes-guide/
- Comprehensive self-hosting guide: https://github.com/mikeroyal/self-hosting-guide
- Caddy (auto-HTTPS reverse proxy): https://caddyserver.com/ · MinIO (object storage / `mc mirror` in backup): https://min.io/

---

## 12. Out of scope / future

- **Authoring the deploy artifacts themselves** — `deploy/docker-compose.yml`, `caddy/Caddyfile`, `nginx/forge.conf`, and `scripts/{install,preflight,backup,restore,healthcheck}.sh` are owned by `v1/F14-docker-compose-selfhost`; the dev compose + `Makefile` targets + `.env.example` + `scripts/quickstart_smoke.sh` by `v1/F13-local-quickstart`. F15 documents, references, and tests them and adds only the `verify-*.sh` probes, the docs contract/harness, and the upgrade drill.
- **A hosted/versioned documentation website** (Docusaurus/MkDocs portal, per-version docs, search, i18n) — V2+. F15 ships Markdown rendered by the Git host.
- **Production-grade Kubernetes Helm chart** — Phase 2 per the roadmap. V1 `kubernetes.md` documents the preview reference chart with a status banner; full HA/scaling guidance follows in V2.
- **Observability stack runbooks** (Prometheus/Grafana/Loki/Temporal operations) — these services are optional/V2 in the compose file; `security.md`/`troubleshooting.md` mention them but dedicated operator runbooks are future work.
- **Air-gapped / offline-mirror install, multi-region HA, managed-cloud Terraform modules** — beyond V1 self-hosting scope.
- **Video walkthroughs / screencast assets** — non-goal.
- **Backup/restore for external managed datastores** (when operators bring their own RDS/ElastiCache/S3) — `backup.md` notes "use your provider's snapshot tooling"; first-class docs are future.
- **Localization of docs** — English only at V1.
