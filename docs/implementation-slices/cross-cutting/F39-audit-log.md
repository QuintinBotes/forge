# F39 — Immutable Audit Log

> Phase: cross-cutting · Alternate slugs seen in sibling slices that resolve to this feature: `v1/F14-observability-audit`, `F: observability-audit`, `cross-cutting/observability-audit` (authoritative slug: `cross-cutting/F39-audit-log`) · Spec module(s): Security (§"Audit log … Every agent action, tool call, MCP call, and approval — immutable, queryable"; §"Secret redaction"), Observability Layer, MCP Integration (§"MCP Security Rules" rule 4: "Full audit log: tool name, payload hash, result status, latency"), Core packages `packages/contracts` + `packages/db`, `apps/api`, `apps/worker`, `apps/web` · Status target: **Done** = there is ONE canonical, frozen `AuditEvent` contract + `AuditSink` Protocol in `packages/contracts` (`forge_contracts.audit`) that **promotes and supersedes the interim audit contract `cross-cutting/F37-auth-secrets-byok` shipped in `forge_contracts.auth`**; the central, **append-only, tamper-evident** `audit_log` table — **whose base table + base immutability trigger originate in `cross-cutting/F37-auth-secrets-byok`'s `0002_auth_secrets` migration and which F39 extends in place** with a per-workspace hash chain — records every security-relevant action (agent actions, tool calls, MCP calls, approval decisions, policy decisions, auth/credential access, connection & key mutations), written through a single `SqlAuditWriter` that **redacts secrets before persistence by deep-walking metadata through the canonical `SecretRedactor` owned by `cross-cutting/F37-auth-secrets-byok`**; a DB-level immutability trigger on Postgres (a reusable `attach_immutability_trigger(table)` helper that **supersedes F37's local `audit_log_immutable` trigger, behavior-identical**, and that the per-domain append-only tables opt into) and repository-level append-only enforcement on every dialect; an authenticated, workspace-isolated, keyset-paginated **query API** with filters; a **chain-integrity verifier** (API endpoint + scheduled worker check) that detects any modification/deletion; an immutable **archival export** to MinIO; and an admin-only **audit viewer** in the web UI (extending F37's base `GET /audit` + `settings/audit` page). `ruff` + `mypy` + `pytest` green on the new `forge_contracts.audit` module, the `forge_db` audit model/writer/repository, the `apps/api` audit router, and the worker verifier; new-code coverage ≥ 80%.

---

## 1. Intent — what & why

The Forge Security table makes one non-negotiable promise (`docs/FORGE_SPEC.md` → Security): **"Audit log — Every agent action, tool call, MCP call, and approval — immutable, queryable."** It is also Build Prompt constraint #9 ("An audit log exists for every agent action, tool call, and MCP call"). Forge's value proposition is an autonomous, policy-gated agent platform a serious team can "adopt, self-host, extend, **audit**, and fully trust in production" (Build Prompt closing line). Without a trustworthy audit log, none of the human-in-the-loop, policy, or approval guarantees are verifiable after the fact.

**The relationship to `cross-cutting/F37-auth-secrets-byok` is foundational (hard dependency).** F37 — the security spine that lands first — creates the baseline `audit_log` table (`packages/db/forge_db/models/audit_log.py`, migration `0002_auth_secrets`, with its own `audit_log_immutable` trigger), the canonical string `SecretRedactor` (`forge_auth.redaction`), an interim `forge_contracts.auth.AuditEvent`/`AuditSink`, an `audit_service.py` sink, an admin `GET /audit` route, and a `settings/audit` web page — so that auth/secret/key actions are audited from day one. **F39 does not re-create any of those; it finalizes and hardens them**: it promotes the canonical contract to `forge_contracts.audit`, extends the `audit_log` table in place with the tamper-evident hash chain, supplies `SqlAuditWriter` + the reusable `attach_immutability_trigger` + the chain verifier + export, and re-homes F37's audit emission, query route, and viewer onto the canonical path. Downstream slices (`v3/F30-multi-team-rbac`, `v1/F09-mcp-gateway-v1`) already assume exactly this split: table origin = F37, canonical `forge_contracts.audit.AuditEvent`/`AuditSink`/`SqlAuditWriter` = F39.

Several other sibling slices already write **per-domain** append-only detail tables, each rich for its own purpose:

- F07 `workflow_transition` — FSM transitions (append-only; F07 explicitly **defers** its "DB-level immutability trigger … to `cross-cutting/F39-audit-log`" and notes its rows "feed the immutable, queryable audit log the Security section requires").
- F06 `agent_steps` — agent step trace (append-only).
- F09 `mcp_audit_log` — MCP operational audit (latency, bytes, payload hash); F09 names "**Platform audit log (cross-cutting, soft)** — if a central audit slice exists, the gateway's `AuditSink` also fans out to it" and adopts `attach_immutability_trigger("mcp_audit_log")`.
- F04 `RepoPolicySnapshot` — the governing policy per commit (append-only); F04 emits an allow/deny `Decision` on every tool call.
- F08 emits approval/check/merge events into F07's event log.

**F39 is the central audit slice those handoffs point at.** It does NOT replace the per-domain tables — they remain the high-fidelity detail. F39 adds three cross-cutting things the spec requires but no single feature owns:

1. **One canonical security-event contract + sink.** A frozen `AuditEvent` DTO and `AuditSink` Protocol in `packages/contracts` → `forge_contracts.audit` (the package whose stated job is "Frozen Pydantic DTOs and Protocol interfaces shared across all Forge packages"), **which supersedes the interim `forge_contracts.auth.AuditEvent`/`AuditSink` F37 shipped to bootstrap auth auditing** (see §4 for the field mapping; F37's `audit_service` is re-pointed at the canonical contract). Every producer — agent runtime (F06), policy evaluator (F04), MCP gateway (F09), approval service (F08), workflow engine (F07), auth/secrets (F37), multi-team RBAC (F30) — emits the **same** event shape. This makes "who did what, when, to what, and was it allowed?" answerable in **one** query surface instead of many.
2. **Tamper-evidence + true immutability.** A per-workspace **hash chain** (each row links to the previous via `prev_hash`/`entry_hash`) plus a DB-level UPDATE/DELETE-blocking trigger on Postgres and append-only repository enforcement everywhere. The chain means even a privileged operator who bypasses the trigger and edits/deletes a row is **detectable** by re-walking the chain. F39 also exports the reusable `attach_immutability_trigger(table)` helper — which **supersedes F37's local `audit_log_immutable` trigger (behavior-identical) and is the hardening F07 deferred** — so `audit_log`, `workflow_transition`, `agent_steps`, `mcp_audit_log`, and `RepoPolicySnapshot` all adopt the same DB-level guarantee.
3. **Query, verify, export, view.** An authenticated, workspace-isolated query API with the filters an auditor needs; a chain verifier (endpoint + scheduled worker job that alarms on a break); an immutable NDJSON archival export to MinIO; and an admin-only viewer in the web UI.

F39 is deliberately the **security/compliance** stream, not the **observability** stream. It captures the redacted *summary* of each security-relevant action (actor, action, resource, outcome, payload hash, link to detail). F10 (run-trace viewer) owns the full step-level I/O for debugging; F39 links to it via `detail_ref`. Keeping them separate keeps the audit log low-volume, permanent, and verifiable while the trace stays high-volume and expirable.

## 2. User-facing behavior / journeys

**Journey A — Auditor answers "who approved this merge?" (admin):** An admin opens Settings → Audit Log, filters `action = approval.decided`, `resource_type = pull_request`, time range last 7 days. Each row shows actor (`alice@acme`), action, resource (`PR #214 / TASK-123`), outcome (`approved`), and timestamp. Clicking a row opens a drawer with the redacted metadata (CI status, spec-validation result), the chain position (`seq 4,102`), a green "chain verified" badge, and a deep link to the F10 run trace and the F08 approval record.

**Journey B — Security review of agent actions:** An admin filters `actor_type = agent_runner`, `outcome = denied`. They see every policy denial and every blocked MCP write: `policy.tool_denied` (write to `infra/prod/**`), `mcp.write_blocked` (delete_page on confluence). They confirm the agent never expanded its own scope (Build Prompt constraint #2) — every denied action is logged with the matched policy rule.

**Journey C — Tamper detection:** A daily worker job re-walks each workspace's hash chain. Someone with raw DB access deleted an audit row to hide an action. The verifier finds `prev_hash` of row N+1 no longer matches `entry_hash` of the surviving predecessor, writes a `system` audit event `audit.chain_broken{broken_at_seq}`, raises a Prometheus alert, and (if F16 present) posts a Slack alert to the admin channel. An admin can also run the check on demand via "Verify integrity" in the viewer.

**Journey D — Credential & connection accountability:** When an admin enables an MCP connection, rotates a BYOK key, or grants a role, F39 records `connection.updated` / `apikey.rotated` / `rbac.role_changed` with the actor and a redacted before/after diff (secrets never appear — only `key_prefix`/`***`). A compliance reviewer can prove every credential touch.

**Journey E — Export for compliance / retention:** An admin exports the last quarter as NDJSON; the file streams from the API and a copy is archived to the `forge-audit` MinIO bucket by the worker. The export includes per-row hashes so an external auditor can independently re-verify the chain offline. Rows are never deleted by export.

**Journey F — Permissions & isolation:** A `member`/`viewer` has no access to the audit log surface (admin-only in V1) and any direct API call returns 403; an admin from another workspace requesting an `audit_log` id gets 404 (no existence leak). No UI or API path can edit or delete an audit row.

## 3. Vertical slice

### 3.1 Data model (tables/columns/migrations touched)

The `audit_log` ORM model **originates in F37** at `packages/db/forge_db/models/audit_log.py`; F39 **edits that model in place** to add the chain/observability columns below and the canonical enums, and adds the **new** `audit_chain_head` model (co-located in `audit_log.py`, or a sibling `audit_chain.py`). Enums move/extend in `packages/db/forge_db/models/enums.py`, registered via `packages/db/forge_db/models/__init__.py`. Because the repo's SQLite unit tests build the schema with `Base.metadata.create_all`, the updated model metadata makes both new tables and all new columns appear automatically in tests. The Postgres path needs a real migration: F37's `0002_auth_secrets` already created `audit_log` + its `audit_log_immutable` trigger, so **F39 ships an incremental, reversible migration `packages/db/migrations/versions/0003_audit_chain.py`** (set `down_revision` to F37's `0002_auth_secrets`, or to the actual Alembic head at integration time) that:
> - `op.add_column`s the new `audit_log` columns (`seq`, `occurred_at`, `severity`, `payload_hash`, `prev_hash`, `entry_hash`, `detail_ref`, `request_id`), renames `actor_display`→`actor_label` and the `result` enum column→`outcome` (with a data backfill mapping `ok→success`, `denied→denied`, `error→error`; `blocked` is new), and widens `action`/`resource_type` to the canonical `AuditAction`/`AuditResourceType` enums (string columns on SQLite; PG enum or checked text);
> - `op.create_table`s `audit_chain_head` and backfills one head row per existing workspace from the current MAX(`seq`)/last `entry_hash` (or genesis when empty — see §4 backfill note);
> - drops F37's local `audit_log_immutable` trigger and re-attaches the **identical** block via `attach_immutability_trigger(audit_log.__table__)` (so the trigger name/behavior is owned by F39 going forward) and adds the new indexes (incl. the Postgres GIN index);
> - `downgrade` reverses every step (drop `audit_chain_head`, drop new columns/indexes, restore F37's column names + local trigger).

> **Required test update:** `packages/db/tests/test_models.py::test_table_set_is_exactly_the_spec_model` and `test_migration.py::EXPECTED_TABLES` assert the table set **exactly** equals the integrated model set. `audit_log` is **already** in those sets (added by F37); F39 adds only **`audit_chain_head`** to `EXPECTED_MODELS`/`EXPECTED_TABLES` and adds the new `audit_log` columns + the new enums (`ActorType`, `AuditAction`, `AuditResourceType`, `AuditOutcome`, `AuditSeverity`) to the column/enum assertions and the `__all__` lists in `models/__init__.py` and `enums.py` (AC1).

**Table `audit_log`** (origin: F37; F39 extends — the central immutable security audit; uses `WorkspaceScopedModel`: UUID PK `id`, `workspace_id` FK→`workspace.id` ON DELETE CASCADE, `created_at`/`updated_at`; `updated_at` is set once at insert and never changes under the immutability trigger). Columns F39 **adds** are marked **(+)**; columns it **renames from F37** are marked **(→)**.

| column | type (`forge_db.base` helper) | notes |
|---|---|---|
| `id` | `Uuid` PK | (F37) from `UUIDPrimaryKeyMixin` |
| `workspace_id` | `Uuid` FK→`workspace.id` ON DELETE CASCADE, indexed | (F37) tenant key (`WorkspaceScopedMixin`); chain is per-workspace |
| `seq` | `BigInteger` not null | **(+)** monotonic per `workspace_id`; the chain ordering key |
| `occurred_at` | `DateTime(timezone=True)` not null | **(+)** event time (caller-supplied; defaults to `created_at`) |
| `actor_type` | `enum_type(ActorType)` not null | **(→)** widens F37's `actor_type: PrincipalType`(`user\|api_key\|service`) to the audit vocabulary `user \| agent_runner \| system \| integration` (mapping in §4) |
| `actor_id` | `Uuid` FK→`app_user.id` ON DELETE SET NULL, nullable | (F37) the `app_user` id for `user` actors; null for agent/system |
| `actor_label` | `String(255)` not null | **(→)** renamed from F37's `actor_display`; durable snapshot: `user:alice@acme`, `agent_run:<uuid>`, `system:scheduler`, `integration:github` — survives user deletion |
| `action` | `enum_type(AuditAction)` not null | **(→)** widens F37's `action: String(128)` to the canonical enum; see §4 (`agent.action`, `tool.call`, `mcp.tool_call`, `mcp.write_blocked`, `policy.tool_allowed/denied`, `approval.requested/decided`, `workflow.transition`, `apikey.created/rotated/revoked`, `connection.created/updated/deleted`, `rbac.role_changed`, `auth.login/logout/failed`, `secret.accessed`, `audit.exported`, `audit.chain_broken`, …) — absorbs F37's auth/secret action catalog |
| `resource_type` | `enum_type(AuditResourceType)` not null | **(→)** widens F37's `resource_type: String(64) NULL` to the canonical enum (now not-null, defaulting to `system`): `task \| workflow_run \| agent_run \| pull_request \| mcp_connection \| repository \| api_key \| user \| policy \| spec \| approval \| audit \| system` |
| `resource_id` | `String(255)` nullable | **(→)** widens F37's `resource_id: UUID NULL` to `String` so external refs (e.g. `PR#214`) fit alongside uuids |
| `outcome` | `enum_type(AuditOutcome)` not null | **(→)** renamed/extended from F37's `result: AuditResult`(`ok\|denied\|error`) to `success \| denied \| error \| blocked` (backfill `ok→success`) |
| `severity` | `enum_type(AuditSeverity)` not null default `info` | **(+)** `info \| notice \| warning \| critical` (drives alerting + fail-closed) |
| `reason` | `Text` nullable | **(+)** short, redacted human reason (e.g. matched policy rule, error class) |
| `metadata_json` | `json_type()` not null default `{}` | **(→)** renamed from F37's `metadata` (the SQLAlchemy attribute cannot be `metadata`); **redacted** structured detail (diff, params summary, cost, latency); never raw secrets |
| `payload_hash` | `String(64)` not null | **(+)** sha256 hex of the canonical redacted `metadata_json` |
| `prev_hash` | `String(64)` not null | **(+)** `entry_hash` of the previous row in this workspace's chain (`"0"*64` for genesis) |
| `entry_hash` | `String(64)` not null | **(+)** sha256 hex over the canonical tuple (§4 `compute_entry_hash`) |
| `detail_ref` | `json_type()` nullable | **(+)** `{table, id}` pointer into the per-domain detail row (`agent_steps`, `mcp_audit_log`, `workflow_transition`, `repo_policy_snapshot`) for drill-down |
| `request_id` | `String(64)` nullable | **(+)** correlation id (HTTP request / Celery task id) for cross-event grouping (F37 carried this inside `metadata`; F39 promotes it to a column) |

Constraints / indexes (names follow `forge_db.base.NAMING_CONVENTION`):
- `UNIQUE (workspace_id, seq)` — chain ordering integrity & idempotent inserts.
- `INDEX (workspace_id, occurred_at)` — primary time-range read path.
- `INDEX (workspace_id, action, occurred_at)` and `(workspace_id, resource_type, resource_id)` — filter support.
- `INDEX (workspace_id, actor_id, occurred_at)` — actor accountability.
- Postgres-only GIN index on `metadata_json` (added in the dialect-guarded path) for free-text/jsonb filtering; skipped on SQLite.

**New table `audit_chain_head`** (per-workspace chain cursor — the **only** mutable audit table; serializes appends and supplies `seq`/`prev_hash`). UUID PK is unnecessary; key is `workspace_id`.

| column | type | notes |
|---|---|---|
| `workspace_id` | `Uuid` PK + FK→`workspace.id` ON DELETE CASCADE | one head row per workspace |
| `last_seq` | `BigInteger` not null default 0 | last assigned `seq` |
| `last_hash` | `String(64)` not null default `"0"*64` | last `entry_hash` (= next row's `prev_hash`) |
| `updated_at` | `DateTime(timezone=True)` | bumped each append |

**Immutability** (Postgres): `attach_immutability_trigger(table, *, allow_cascade_delete=True)` in `packages/db/forge_db/audit/immutability.py` attaches, via `sqlalchemy.event.listen(table, "after_create", DDL(...).execute_if(dialect="postgresql"))`, a `BEFORE UPDATE OR DELETE` trigger function `forge_audit_block_mutation()` that `RAISE EXCEPTION`s — except a `DELETE` originating from the `workspace` ON DELETE CASCADE during tenant teardown (detected via a session-local GUC `forge.allow_audit_cascade` the teardown path sets). On `audit_log` it **supersedes F37's local `audit_log_immutable` trigger** (F39's `0003_audit_chain` migration drops the F37 trigger and re-attaches this behavior-identical one), and is exported for `workflow_transition` / `agent_steps` / `mcp_audit_log` / `repo_policy_snapshot` to opt in (closing F07's deferred hardening; already consumed by F09). `audit_chain_head` is **not** immutable. On SQLite (unit tests) the trigger is skipped; append-only is enforced by the repository (which exposes only insert + select) and asserted by tests.

### 3.2 Backend (FastAPI routes + services/packages)

**`packages/contracts/forge_contracts/audit.py`** — the frozen contract every producer and the writer share (DTOs + enums + `AuditSink` Protocol + pure hashing helpers; no I/O, no SQLAlchemy). See §4.

**`packages/db/forge_db/audit/`** (writer + chain + repository + redaction; depends on `forge_contracts` + the SQLAlchemy session):
```
packages/db/forge_db/audit/
├── __init__.py
├── chain.py          # canonical_json(), compute_payload_hash(), compute_entry_hash(), verify_chain()
├── immutability.py   # attach_immutability_trigger(table), the trigger DDL/function
├── redaction.py      # redact_metadata(value, redactor) — deep-walks dict/list/str, applying F37's canonical SecretRedactor to every string leaf (F37's redactor is string-only; F39 does NOT define its own pattern set — see §5)
├── writer.py         # SqlAuditWriter(AuditSink): emit() / emit_async-enqueue
└── repository.py     # AuditQueryRepository: keyset-paginated, workspace-scoped reads + verify
```

`SqlAuditWriter.emit(session, event)` algorithm (synchronous, transactional):
1. `redacted = redaction.redact_metadata(event.metadata, self._redactor)` (the injected F37 `SecretRedactor`, deep-walked over nested dict/list/str); build the durable `actor_label`.
2. `SELECT … FROM audit_chain_head WHERE workspace_id = :ws FOR UPDATE` (insert genesis head if absent) — this **serializes** appends per workspace.
3. `seq = head.last_seq + 1`; `prev_hash = head.last_hash`.
4. `payload_hash = compute_payload_hash(redacted)`; `entry_hash = compute_entry_hash(prev_hash, ws, seq, occurred_at, actor_type, actor_label, action, resource_type, resource_id, outcome, payload_hash)`.
5. INSERT the `audit_log` row; UPDATE the head (`last_seq=seq, last_hash=entry_hash`).
6. The caller controls the transaction boundary. **Critical events** (`severity=critical`: approval decisions, policy overrides, write/deploy actions, key/role changes) are emitted on the **caller's** session so they commit atomically with the action — **fail-closed**: if the audit write fails, the action rolls back. **Non-critical** events use `emit_async` (enqueues `audit.record` Celery task with the prebuilt event) — **fail-open**: a transient audit failure never aborts a routine agent step (it is retried by Celery and a drop is logged + metered).

**Audit router** (the `audit.py` router mounted at `/api/v1/audit`; all routes auth-required; **admin-only** read in V1). F37 already ships this router + an `audit_service.py` with the basic `GET /audit` list. **F39 extends F37's existing router/service in place** (same module location F37 chose under `apps/api`; use F37's `require_role(UserRole.ADMIN)` dependency) rather than creating a parallel router — it re-points the list query at `AuditQueryRepository` (keyset pagination + the full filter set), and adds the `{id}`, `verify`, `export`, and `actions` routes:

| Method | Path | RBAC | Purpose |
|---|---|---|---|
| `GET` | `/audit` | admin | (F37 base, F39 extends) List entries; filters `actor_id, actor_type, action[], resource_type, resource_id, outcome, severity, from, to, q`; keyset `cursor`+`limit`. |
| `GET` | `/audit/{id}` | admin | Single entry (redacted) + chain neighbors + `detail_ref`. |
| `POST` | `/audit/verify` | admin | Verify the workspace chain (optional `from_seq`/`to_seq`); returns `ChainVerifyResult`. |
| `GET` | `/audit/export` | admin | Stream NDJSON export for a range (also archived to MinIO by the worker). |
| `GET` | `/audit/actions` | admin | Enum/vocabulary for filter UI (action/resource/outcome lists). |

Thin controllers delegate to the `AuditService` (F37's `audit_service.py`, extended): wraps `AuditQueryRepository`, enforces workspace isolation → 404 on cross-workspace id, RBAC, and signed-URL minting for archived exports. F37's `audit_service` `AuditSink.record(event)` is re-pointed to construct a `forge_contracts.audit.AuditEvent` and persist via `SqlAuditWriter` (so every existing F37 auth/secret/key event lands in the hash chain). The API schemas adapt the `forge_contracts.audit` models for the OpenAPI surface. **There is no write endpoint** — audit rows are produced only by trusted in-process producers via `AuditSink`, never over HTTP.

### 3.3 Worker / agent runtime (Celery tasks, LangGraph, if any)

`apps/worker/forge_worker/tasks/audit.py` (queue `audit`):
- `audit.record(event_dict)` — the async sink: rebuilds the `AuditEvent`, opens its own session, calls `SqlAuditWriter.emit`. Retries with backoff; on terminal failure logs + increments `forge_audit_dropped_total`. Used by `emit_async` for non-critical events.
- `audit.verify_chain_all()` — Celery beat (daily). For each workspace, `verify_chain(...)`; on a break, emit a `system`/`critical` `audit.chain_broken{broken_at_seq}` event, increment `forge_audit_chain_broken_total`, and (if F16 present) dispatch a Slack alert.
- `audit.archive(period)` — Celery beat (monthly). Stream the prior period's rows to NDJSON, upload to MinIO `forge-audit/{workspace}/{period}.ndjson` (with chain hashes for offline re-verification), and emit `audit.exported`. **Never deletes** source rows.

No LangGraph here. The agent runtime (F06) and other producers call the `AuditSink` directly; F39 supplies the sink, not the agent loop. LangGraph is N/A — F39 is a cross-cutting write/query/verify surface, not an agent workflow.

### 3.4 Frontend / UI (Next.js routes/components, if any)

- `apps/web/app/(app)/settings/audit/page.tsx` — **F37 ships a base version of this admin-only page (a plain `AuditTable` over `GET /audit`); F39 extends it** with: filter bar (actor, action, resource type, outcome, severity, date range, free-text), TanStack Table (virtualized) of `AuditEntry` rows with actor/action/resource/outcome/time columns and a severity chip, plus the detail drawer / verify / export affordances below. Members/viewers do not see the nav entry and the route 403s.
- `apps/web/components/audit/audit-detail-drawer.tsx` — redacted metadata viewer, chain position (`seq`, truncated `prev_hash`/`entry_hash`), a "chain verified" badge, and deep links to the F10 run trace + the domain detail (`detail_ref`).
- `apps/web/components/audit/verify-integrity-button.tsx` — calls `POST /audit/verify`, renders `ok` / `broken_at_seq`.
- `apps/web/components/audit/export-dialog.tsx` — date-range export → NDJSON download.
- `apps/web/lib/api/audit.ts` — typed client matching §4; hooks `useAuditLog(filters)` (keyset-paginated), `useAuditEntry(id)`, `useVerifyChain()`.

Keyboard-first (board UX standard): `j`/`k` move row selection, `o`/`Enter` open the detail drawer, `Esc` closes, `f` focuses the filter. No mouse required.

### 3.5 Infra / deploy (compose, helm, caddy, if any)

- **No new compose service.** F39 reuses Postgres (`audit_log`/`audit_chain_head`), Redis+Celery (async sink, beat verifier/archiver), and MinIO (export archive).
- **MinIO bucket `forge-audit`** with a long retention/lifecycle (default: never expire; compliance hold) and object-lock/versioning recommended — add to the bucket bootstrap in `deploy/scripts/install.sh` alongside F08's `forge-checks` and F10's `forge-traces`.
- New env (add to `deploy/.env.example` + `.env.production.example`): `AUDIT_ARCHIVE_BUCKET=forge-audit`, `AUDIT_VERIFY_CRON=0 3 * * *`, `AUDIT_ARCHIVE_CRON=0 4 1 * *`, `AUDIT_ASYNC_QUEUE=audit`, `AUDIT_EXPORT_SIGNED_URL_TTL_SECONDS=300`, `AUDIT_FAIL_CLOSED_SEVERITY=critical`.
- **Caddy:** the export endpoint may stream large NDJSON — document a long read timeout / buffering-off for `/api/v1/audit/export` in `deploy/caddy/Caddyfile` (and the Nginx alternative). No public route beyond the standard authenticated API.
- Helm: N/A — V1 ships Docker Compose only (Helm is V2, F24). The schema/trigger and worker tasks are deployment-agnostic.

## 4. Public interfaces / contracts (exact signatures, Pydantic models, Protocols, YAML schemas)

`packages/contracts/forge_contracts/audit.py`:

```python
from __future__ import annotations
import enum, hashlib, json
from datetime import datetime
from typing import Any, Protocol, runtime_checkable
from uuid import UUID
from pydantic import BaseModel, Field

class ActorType(enum.StrEnum):
    USER = "user"; AGENT_RUNNER = "agent_runner"
    SYSTEM = "system"; INTEGRATION = "integration"

class AuditOutcome(enum.StrEnum):
    SUCCESS = "success"; DENIED = "denied"; ERROR = "error"; BLOCKED = "blocked"

class AuditSeverity(enum.StrEnum):
    INFO = "info"; NOTICE = "notice"; WARNING = "warning"; CRITICAL = "critical"

class AuditResourceType(enum.StrEnum):
    TASK = "task"; WORKFLOW_RUN = "workflow_run"; AGENT_RUN = "agent_run"
    PULL_REQUEST = "pull_request"; MCP_CONNECTION = "mcp_connection"
    REPOSITORY = "repository"; API_KEY = "api_key"; USER = "user"
    POLICY = "policy"; SPEC = "spec"; APPROVAL = "approval"
    AUDIT = "audit"; SYSTEM = "system"

class AuditAction(enum.StrEnum):
    # agent / tools (Security: "every agent action, tool call")
    AGENT_ACTION = "agent.action"; TOOL_CALL = "tool.call"
    # MCP (Security: "MCP call"; MCP rule 4)
    MCP_TOOL_CALL = "mcp.tool_call"; MCP_RESOURCE_READ = "mcp.resource_read"
    MCP_WRITE_BLOCKED = "mcp.write_blocked"
    # policy (Security: "Policy evaluation")
    POLICY_TOOL_ALLOWED = "policy.tool_allowed"; POLICY_TOOL_DENIED = "policy.tool_denied"
    POLICY_OVERRIDE = "policy.override"
    # approvals (Security: "approval")
    APPROVAL_REQUESTED = "approval.requested"; APPROVAL_DECIDED = "approval.decided"
    # workflow / lifecycle
    WORKFLOW_TRANSITION = "workflow.transition"
    # secrets / connections / rbac / auth
    APIKEY_CREATED = "apikey.created"; APIKEY_ROTATED = "apikey.rotated"; APIKEY_REVOKED = "apikey.revoked"
    SECRET_ACCESSED = "secret.accessed"
    CONNECTION_CREATED = "connection.created"; CONNECTION_UPDATED = "connection.updated"
    CONNECTION_DELETED = "connection.deleted"
    RBAC_ROLE_CHANGED = "rbac.role_changed"
    AUTH_LOGIN = "auth.login"; AUTH_LOGOUT = "auth.logout"; AUTH_FAILED = "auth.failed"
    # audit self-events
    AUDIT_EXPORTED = "audit.exported"; AUDIT_CHAIN_BROKEN = "audit.chain_broken"

class AuditEvent(BaseModel):
    """The single event every producer emits. `metadata` is redacted by the writer."""
    workspace_id: UUID
    actor_type: ActorType
    actor_label: str                      # durable snapshot, e.g. "user:alice@acme"
    action: AuditAction
    resource_type: AuditResourceType
    outcome: AuditOutcome
    actor_id: UUID | None = None          # app_user id for user actors
    resource_id: str | None = None
    severity: AuditSeverity = AuditSeverity.INFO
    reason: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    detail_ref: dict[str, str] | None = None   # {"table": "...", "id": "..."}
    request_id: str | None = None
    occurred_at: datetime | None = None        # defaults to insert time

class AuditEntry(AuditEvent):
    """Read model: a persisted, redacted row including chain fields."""
    id: UUID
    seq: int
    payload_hash: str
    prev_hash: str
    entry_hash: str
    created_at: datetime

class ChainVerifyResult(BaseModel):
    workspace_id: UUID
    ok: bool
    entries_checked: int
    broken_at_seq: int | None = None      # first seq whose linkage/hash failed
    detail: str | None = None

@runtime_checkable
class AuditSink(Protocol):
    """Implemented by F39 (SqlAuditWriter); called by every producer."""
    def emit(self, session: Any, event: AuditEvent) -> AuditEntry: ...          # transactional (critical)
    def emit_async(self, event: AuditEvent) -> None: ...                         # enqueue (non-critical)

# --- pure, deterministic hashing (shared by writer + verifier + offline auditors) ---
def canonical_json(value: Any) -> str:
    """Stable JSON: sorted keys, no whitespace, UTC ISO timestamps."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)

def compute_payload_hash(redacted_metadata: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(redacted_metadata).encode()).hexdigest()

def compute_entry_hash(*, prev_hash: str, workspace_id: UUID, seq: int,
                       occurred_at: datetime, actor_type: ActorType, actor_label: str,
                       action: AuditAction, resource_type: AuditResourceType,
                       resource_id: str | None, outcome: AuditOutcome,
                       payload_hash: str) -> str:
    tuple_ = [prev_hash, str(workspace_id), seq, occurred_at.isoformat(),
              actor_type.value, actor_label, action.value, resource_type.value,
              resource_id or "", outcome.value, payload_hash]
    return hashlib.sha256(canonical_json(tuple_).encode()).hexdigest()

GENESIS_HASH = "0" * 64
```

**Contract reconciliation with `cross-cutting/F37-auth-secrets-byok` (supersession + field mapping).** F37 shipped an interim audit contract in `forge_contracts.auth` to bootstrap auth/secret/key auditing before this slice existed. `forge_contracts.audit` above is the **canonical** contract (as `v3/F30-multi-team-rbac` and `v1/F09-mcp-gateway-v1` already reference); F39 deletes F37's `AuditEvent`/`AuditSink` from `forge_contracts.auth` (or makes them thin re-exports for one release) and re-points F37's `audit_service`. The field mapping the migration/data-backfill applies:

| F37 (`forge_contracts.auth`) | F39 canonical (`forge_contracts.audit`) | mapping |
|---|---|---|
| `actor_type: PrincipalType` (`user\|api_key\|service`) | `actor_type: ActorType` (`user\|agent_runner\|system\|integration`) | `user→user`; `service→system`; `api_key→` the issuing identity's type (`agent_runner` for agent-runner keys, else `system`/`integration`) |
| `actor_display: str` | `actor_label: str` | rename (verbatim value) |
| `action: str` | `action: AuditAction` | F37's dotted strings are members of the `AuditAction` enum (auth/secret/apikey/connection/rbac actions are already enumerated above) |
| `result: AuditResult` (`ok\|denied\|error`) | `outcome: AuditOutcome` (`success\|denied\|error\|blocked`) | `ok→success`; `denied→denied`; `error→error` |
| `resource_type: str\|None`, `resource_id: UUID\|None` | `resource_type: AuditResourceType`, `resource_id: str\|None` | null `resource_type` → `system`; uuid → str |
| `metadata: dict` | `metadata: dict` | unchanged (still redacted before persistence) |
| (n/a — interim sink was `async record(event)`) | `AuditSink.emit` / `emit_async` | F37's `AuditSink.record` becomes an adapter that builds an `AuditEvent` and calls `emit`/`emit_async` |

> **Chain backfill (one-time, in `0003_audit_chain`).** F37's pre-F39 `audit_log` rows have no hash chain. The migration walks existing rows per workspace ordered by `created_at, id`, assigns `seq` 1..N, sets `occurred_at = created_at`, computes `prev_hash`/`payload_hash`/`entry_hash` over the (mapped) values, and writes one `audit_chain_head` row per workspace at the final `seq`/`entry_hash`. From that point the chain is continuous; the backfilled prefix is verifiable by `verify_chain` exactly like live rows.

`packages/db/forge_db/audit/writer.py`:

```python
class SqlAuditWriter:                       # implements forge_contracts.audit.AuditSink
    def __init__(self, *, redactor: "SecretRedactor",
                 async_dispatch: "Callable[[dict], None]") -> None: ...
    def emit(self, session: Session, event: AuditEvent) -> AuditEntry:
        """Redact -> lock head (FOR UPDATE) -> assign seq/prev_hash ->
        compute hashes -> INSERT audit_log + UPDATE audit_chain_head.
        Uses the caller's `session` so critical events commit atomically
        with the action (fail-closed)."""
    def emit_async(self, event: AuditEvent) -> None:
        """Serialize the event and hand to the `audit.record` Celery task
        (fail-open path for non-critical, high-volume events)."""
```

`packages/db/forge_db/audit/chain.py`:

```python
def verify_chain(session: Session, workspace_id: UUID, *,
                 from_seq: int | None = None, to_seq: int | None = None
                 ) -> ChainVerifyResult:
    """Re-walk audit_log rows ordered by seq; for each row recompute
    payload_hash and entry_hash and assert row.prev_hash == previous.entry_hash
    (GENESIS_HASH for the first). Returns ok / broken_at_seq."""
```

`packages/db/forge_db/audit/immutability.py`:

```python
def attach_immutability_trigger(table: "Table", *, allow_cascade_delete: bool = True) -> None:
    """Register an after_create DDL (Postgres only) installing a BEFORE
    UPDATE OR DELETE trigger that RAISEs, except DELETEs during a guarded
    workspace cascade. No-op on non-Postgres dialects."""
```

`apps/api/app/schemas/audit.py` (request/response):

```python
class AuditListResponse(BaseModel):
    items: list[AuditEntry]
    next_cursor: str | None              # opaque base64 of (occurred_at_iso, seq)

class AuditListQuery(BaseModel):
    actor_id: UUID | None = None
    actor_type: ActorType | None = None
    action: list[AuditAction] | None = None
    resource_type: AuditResourceType | None = None
    resource_id: str | None = None
    outcome: AuditOutcome | None = None
    severity: AuditSeverity | None = None
    from_: datetime | None = Field(default=None, alias="from")
    to: datetime | None = None
    q: str | None = None                 # free-text over redacted metadata (jsonb)
    cursor: str | None = None
    limit: int = Field(100, ge=1, le=500)
```

**Producer call convention** (documented contract every emitting slice follows):

```python
# Critical (atomic with the action, fail-closed) — F08 approval, F04 override, key/role change:
audit_sink.emit(session, AuditEvent(
    workspace_id=ws, actor_type=ActorType.USER, actor_id=user.id,
    actor_label=f"user:{user.email}", action=AuditAction.APPROVAL_DECIDED,
    resource_type=AuditResourceType.PULL_REQUEST, resource_id="PR#214",
    outcome=AuditOutcome.SUCCESS, severity=AuditSeverity.CRITICAL,
    metadata={"decision": "approved", "ci": "green", "spec_validation": "pass"},
    detail_ref={"table": "approval_request", "id": str(approval.id)}))

# Non-critical (fail-open, async) — F06 tool call, F09 MCP read:
audit_sink.emit_async(AuditEvent(
    workspace_id=ws, actor_type=ActorType.AGENT_RUNNER,
    actor_label=f"agent_run:{agent_run_id}", action=AuditAction.TOOL_CALL,
    resource_type=AuditResourceType.AGENT_RUN, resource_id=str(agent_run_id),
    outcome=AuditOutcome.SUCCESS, metadata={"tool": "read_file", "path": "app/main.py"},
    detail_ref={"table": "agent_steps", "id": str(step_id)}))
```

## 5. Dependencies — features/slices that must exist first (reference by <phase>/<id>-<slug>)

- **`v1/F00-foundation-substrate`** (REQUIRED; alternate slugs seen across slices: `v1/F01-foundation-scaffolding`, `v1/F00-platform-foundation`). Supplies `packages/contracts` (the home for the canonical frozen `AuditEvent`/`AuditSink`), `packages/db` (`Base`, `WorkspaceScopedModel`, `enum_type`, `json_type`, the `workspace` + `app_user` tables, the `0001_baseline` migration, the session factory), Redis+Celery wiring, and the MinIO `ArtifactStore`.
- **`cross-cutting/F37-auth-secrets-byok`** (REQUIRED — the audit foundation; alternate slugs seen across slices: `v1/F14-observability-audit`, `v1/F15-auth-secrets-rbac`). F37 is the security spine that lands first and **originates everything F39 builds on**: the baseline `audit_log` table (`packages/db/forge_db/models/audit_log.py`, migration `0002_auth_secrets`, with its `audit_log_immutable` trigger) that F39 extends in place; the **canonical `SecretRedactor`** (`forge_auth.redaction`, a `redact(text: str) -> str` + `register_known_secret(value)` Protocol+impl) that F39's `redact_metadata` deep-walker consumes (F39 does **not** define its own pattern set); the interim `forge_contracts.auth.AuditEvent`/`AuditSink` that F39 **supersedes** with `forge_contracts.audit` (§4 mapping); the `audit_service.py` sink, the admin `GET /audit` route, and the `settings/audit` web page that F39 extends; the `Principal` + `get_principal` auth dependency and the flat `require_role(UserRole.ADMIN)` RBAC gate used on every `/audit*` route; and the four roles (`admin/member/viewer/agent-runner`). **Build order: F37 → F39.** (If F30 has also landed, `require_role` is its back-compat shim — unchanged for F39.)

**Producers (consumers of F39's contract — soft, bidirectional; F39 ships standalone with a fake producer and is fully testable without them):**
- `cross-cutting/F37-auth-secrets-byok` — once F39 lands, F37's own auth/secret/key/role events (`auth.login/logout/failed`, `secret.accessed`, `apikey.created/rotated/revoked`, `connection.*`, `rbac.role_changed`) flow through the canonical sink into the hash chain.
- `v3/F30-multi-team-rbac` — emits `role_grant.*` / `team.*` / `project_access.*` through F39's canonical `AuditSink`/`SqlAuditWriter` (F30 §4 already references this); extends the action vocabulary (see §12).
- `v1/F04-repo-policy` — emits `policy.tool_allowed/denied` and `policy.override` (`Decision` on every tool call); also a candidate to adopt `attach_immutability_trigger` for `repo_policy_snapshot`.
- `v1/F06-single-execution-agent` — emits `agent.action` / `tool.call` (`detail_ref` → `agent_steps`); adopts the trigger for `agent_steps`.
- `v1/F07-feature-workflow-fsm` — emits `workflow.transition`; F39 **closes F07's explicitly deferred** "DB-level immutability trigger for `workflow_transition` … deferred to `cross-cutting/F39-audit-log`" by providing `attach_immutability_trigger`.
- `v1/F08-plan-execute-verify-pr-approval` — emits `approval.requested/decided`, `workflow.transition` effects, merge events (critical, fail-closed).
- `v1/F09-mcp-gateway-v1` — emits `mcp.tool_call` / `mcp.resource_read` / `mcp.write_blocked`; F39 is the "Platform audit log (cross-cutting)" F09 names; `mcp_audit_log` stays as the MCP operational detail (`detail_ref`).
- `v1/F16-slack-notifications` — soft; consumed for chain-break / critical alerts (degrades to log+metric if absent).
- `v1/F10-run-trace-viewer` — sibling observability surface; F39 links to it via `detail_ref`; neither blocks the other.

## 6. Acceptance criteria (numbered, testable)

1. **Schema extends F37 without breaking the substrate.** On SQLite, `Base.metadata.create_all` builds the full `audit_log` (F37 columns + F39's added/renamed columns) and the new `audit_chain_head`; `audit_log` is already in `EXPECTED_TABLES`/`EXPECTED_MODELS` (added by F37) and F39 adds only `audit_chain_head` plus the new columns/enums to those assertions and the `__all__` lists. On Postgres, F39's reversible `0003_audit_chain` migration (down_revision = F37's `0002_auth_secrets`) applies cleanly on top of F37, and `alembic upgrade head` then `downgrade` back to `0002_auth_secrets` round-trips (rows preserved, F37 column names + local trigger restored).
2. **Append assigns a monotonic per-workspace chain.** Sequential `emit` calls for one workspace produce rows with `seq` 1,2,3…, each `prev_hash == previous.entry_hash`, the first with `prev_hash == GENESIS_HASH`; two different workspaces keep independent chains.
3. **Hashes are correct and deterministic.** `payload_hash == compute_payload_hash(redacted_metadata)` and `entry_hash == compute_entry_hash(...)` for the stored field values; recomputation by an independent caller yields identical hashes (offline re-verifiable).
4. **Secret redaction before persistence.** An event whose `metadata` contains secret-shaped values (AWS key, PEM block, `*_API_KEY=…`, high-entropy token) is stored with those replaced by the redactor's marker in `metadata_json`, in `payload_hash`'s input, in API responses, and in the NDJSON export — verified for nested dict/list structures; no raw secret appears anywhere.
5. **Chain verification detects tampering.** `verify_chain` returns `ok=True` for an untouched chain; after a row's `metadata_json`/`entry_hash` is mutated or a row is deleted directly in the DB, it returns `ok=False` with the correct `broken_at_seq`.
6. **DB-level immutability (Postgres).** `UPDATE` or `DELETE` on `audit_log` raises (trigger); a `workspace` delete that cascades audit rows succeeds via the guarded cascade path. (Asserted on Postgres; PARKED-style note where Postgres is unreachable, mirroring `test_migration.py`, with the trigger DDL verified to compile for the Postgres dialect.)
7. **Repository-level append-only on every dialect.** `AuditQueryRepository` exposes only insert (via writer) and select; there is no update/delete code path; an attempted update is rejected by a typed guard on SQLite (where no trigger exists).
8. **Concurrency keeps the chain linear.** Concurrent `emit` calls for the same workspace (serialized by `audit_chain_head … FOR UPDATE`) produce a gap-free, duplicate-free `seq` sequence and a chain that `verify_chain` accepts.
9. **Fail-closed for critical events.** When `SqlAuditWriter.emit` is called on the caller's session for a `severity=critical` event and the surrounding transaction rolls back (or the audit insert fails), neither the audit row **nor** the action is committed (atomicity); a forced audit-insert failure propagates so the caller can abort.
10. **Fail-open for non-critical events.** `emit_async` enqueues the `audit.record` task; a transient task failure is retried and, on terminal failure, logged + `forge_audit_dropped_total` incremented — never raised into the producing operation.
11. **Query filters + keyset pagination.** `GET /audit` honors every filter (`actor_id`, `actor_type`, `action[]`, `resource_type`, `resource_id`, `outcome`, `severity`, `from`/`to`, `q`); `limit=N` returns ≤ N items + `next_cursor`; following the cursor yields contiguous, gapless, duplicate-free pages; the final page returns `next_cursor=null`.
12. **RBAC + tenant isolation.** All `/audit*` endpoints require `admin`; `member`/`viewer`/`agent-runner` get 403; an admin from another workspace requesting an `audit_log` id gets **404** (no existence leak); every query is filtered by `workspace_id`.
13. **No mutation surface.** There is no HTTP route that creates, updates, or deletes an audit row; OpenAPI exposes only the read/verify/export endpoints.
14. **`POST /audit/verify` endpoint.** Returns `ChainVerifyResult` matching `verify_chain`; supports `from_seq`/`to_seq` range checks; admin-only.
15. **Export is immutable + re-verifiable.** `GET /audit/export` streams NDJSON including `seq`/`prev_hash`/`entry_hash`; the `audit.archive` worker writes the same to MinIO `forge-audit/...`; no source row is modified or deleted; an `audit.exported` event is recorded.
16. **Scheduled verifier alarms on break.** `audit.verify_chain_all` over a tampered workspace emits a `system`/`critical` `audit.chain_broken{broken_at_seq}` event and increments `forge_audit_chain_broken_total` (and dispatches the F16 alert when present).
17. **Reusable immutability helper.** `attach_immutability_trigger(table)` is idempotent, dialect-guarded (no-op on SQLite), and applying it to a second table (e.g. a fake `workflow_transition`-shaped table) installs the same UPDATE/DELETE block — proving the F07 hand-off works.
18. **Producer contract is honored end-to-end.** A fake producer emitting one of each `AuditAction` (user-critical via `emit`, agent-non-critical via `emit_async` eager) results in correctly typed, redacted, chained rows queryable via the API with working `detail_ref` drill-down fields.
19. **F37 contract supersession.** F37's `audit_service` `AuditSink.record(F37_AuditEvent)` adapter maps an F37-shaped event (`actor_display`/`result`/string `action`) onto `forge_contracts.audit.AuditEvent` per the §4 table (`ok→success`, `actor_display→actor_label`, `service→system`) and persists it via `SqlAuditWriter` into the chained `audit_log`; the resulting row is chain-valid and queryable. `forge_contracts.auth.AuditEvent`/`AuditSink` are gone (or re-export the canonical types).
20. **F37 row backfill is chain-valid.** Given pre-F39 `audit_log` rows (no chain columns), the `0003_audit_chain` backfill assigns gap-free `seq`, sets `occurred_at = created_at`, computes the chain, and writes one `audit_chain_head` per workspace; `verify_chain` returns `ok=True` over the backfilled prefix and stays `ok` when new live events are appended after it.

## 7. Test plan (TDD) — concrete test cases (unit + integration), key fixtures

Write tests first; `backend-tdd` discipline (≥ 80% new-code coverage). Layout: `packages/contracts/tests/` (pure contract/hash), `packages/db/tests/audit/` (writer/chain/repository/immutability), `apps/api/tests/audit/` (API), `apps/worker/tests/` (verifier/archiver/async sink), `apps/web/__tests__/` (viewer).

**Key fixtures:**
- `sqlite_engine` — in-memory SQLite with `Base.metadata.create_all` (matches the existing `test_models.py` fixture); audit tables present, triggers skipped.
- `pg` — Postgres (testcontainer) with the migration applied, for the immutability-trigger and concurrency tests (PARKED with a clear note where Postgres is unreachable, mirroring `test_migration.py`; the trigger DDL is still asserted to compile for the Postgres dialect).
- `writer` — `SqlAuditWriter` wired to a `FakeRedactor` (deterministic markers) + a recording async dispatcher.
- `seed_events` — factory emitting N varied `AuditEvent`s (mixed actors/actions/outcomes) into a workspace.
- `secretful_metadata` — nested dict/list with API-key/PEM/high-entropy strings.
- `api_app` — httpx `AsyncClient` over the FastAPI app with DI overrides + an `admin` and a `member` principal and a second workspace.

**Unit — contract & hashing (`packages/contracts/tests/test_audit_contract.py`):**
- `test_canonical_json_is_stable` — key order / whitespace invariant; AC3.
- `test_entry_hash_links_and_is_deterministic` — same inputs → same hash; changing any field changes it; AC2/AC3.
- `test_genesis_prev_hash` — first row uses `GENESIS_HASH`; AC2.

**Unit — writer (`packages/db/tests/audit/test_writer.py`):**
- `test_emit_assigns_monotonic_seq_and_links_chain` — AC2.
- `test_emit_redacts_metadata_before_hash_and_store` — secrets gone from `metadata_json` and from `payload_hash` input, using F37's `SecretRedactor` via the `redact_metadata` deep-walker over nested dict/list; AC4.
- `test_emit_async_enqueues_and_never_raises` — dispatcher called; a raising dispatcher is swallowed (fail-open); AC10.
- `test_critical_emit_uses_caller_session` — rolling back the caller's transaction discards both action + audit row (atomicity); a forced insert error propagates; AC9.
- `test_f37_record_adapter_maps_and_persists` — F37's `AuditSink.record` adapter maps an F37-shaped event onto the canonical `AuditEvent` (`ok→success`, `actor_display→actor_label`, `service→system`) and persists a chain-valid row; AC19.

**Unit — chain & immutability (`packages/db/tests/audit/test_chain.py`, `test_immutability.py`):**
- `test_verify_ok_for_clean_chain` and `test_verify_detects_mutation_and_deletion` — returns `broken_at_seq`; AC5.
- `test_repository_has_no_update_delete_path` / `test_update_rejected_on_sqlite_guard` — AC7.
- `test_trigger_ddl_compiles_for_postgres_and_noops_on_sqlite` — AC6/AC17.
- `test_attach_trigger_idempotent_and_reusable_on_second_table` — AC17.

**Integration — Postgres (`packages/db/tests/audit/test_pg_immutability.py`, gated/PARKED):**
- `test_update_delete_raises` — trigger blocks mutation; workspace cascade delete succeeds; AC6.
- `test_concurrent_emit_keeps_linear_chain` — N concurrent appends → gap-free `seq`, `verify_chain` ok; AC8.

**API tests (`apps/api/tests/audit/test_audit_api.py`):**
- `test_list_filters_and_keyset_pagination` — every filter + cursor round-trip, gapless pages, final `next_cursor=null`; AC11.
- `test_admin_only_and_cross_workspace_404` — member/viewer 403; foreign id 404; AC12.
- `test_no_write_endpoints_in_openapi` — only read/verify/export present; AC13.
- `test_verify_endpoint` — clean → ok; tampered → `broken_at_seq`; AC14.
- `test_export_streams_ndjson_with_hashes` — rows include chain fields; re-verifiable offline; emits `audit.exported`; AC15.
- `test_get_entry_includes_detail_ref` — drill-down pointer present; AC18.

**Worker tests (`apps/worker/tests/test_audit_tasks.py`, Celery eager):**
- `test_record_task_persists_event` — async sink writes the row; AC10/AC18.
- `test_verify_chain_all_emits_chain_broken_on_tamper` — `audit.chain_broken` recorded + metric incremented + Slack alert dispatched (F16 stub); AC16.
- `test_archive_writes_minio_and_keeps_rows` — NDJSON in MinIO, source rows intact; AC15.

**DB schema tests (extend `packages/db/tests/test_models.py` / `test_migration.py`):**
- `test_table_set_includes_audit_chain_head` — `audit_chain_head` is added to `EXPECTED_TABLES`/`EXPECTED_MODELS` (`audit_log` already present from F37); the new `audit_log` columns + enums are asserted; AC1.
- `test_0003_audit_chain_upgrade_downgrade_roundtrip` — applies `0003_audit_chain` on top of F37's `0002_auth_secrets` and reverses it (column renames + `audit_chain_head` drop + trigger restore); AC1 (PARKED where Postgres is unreachable, mirroring existing `test_migration.py`).
- `test_0003_backfill_makes_existing_rows_chain_valid` — seed pre-F39 `audit_log` rows, run the backfill, assert gap-free `seq`/`audit_chain_head` and `verify_chain` ok over the prefix and after new appends; AC20.

**Frontend (`apps/web/__tests__/audit.test.tsx`, RTL):**
- `test_admin_sees_log_member_blocked` — nav + route gating; AC12.
- `test_filters_and_detail_drawer` — filtering + drawer with redacted metadata + chain badge; AC4/AC18.
- `test_verify_button_shows_result` — ok / broken_at_seq; AC14.

## 8. Security & policy considerations

- **This slice is the audit guarantee.** It directly satisfies `docs/FORGE_SPEC.md` → Security "Audit log — Every agent action, tool call, MCP call, and approval — immutable, queryable" and Build Prompt constraint #9, and centralizes the per-domain append-only logs into one verifiable stream.
- **Immutability is defense-in-depth, not a single control.** (1) Append-only repository on every dialect; (2) a Postgres `BEFORE UPDATE OR DELETE` trigger; (3) a per-workspace hash chain that makes any out-of-band mutation/deletion **detectable** even if (1) and (2) are bypassed; (4) immutable MinIO archival (object-lock recommended) for off-box durability. The reusable `attach_immutability_trigger` extends control (2) to the sibling append-only tables (closing F07's deferred hardening).
- **Secret redaction at write time** (Security: "Secrets stripped from logs, traces, and retrieval results"). F39 reuses the **single canonical `SecretRedactor` owned by `cross-cutting/F37-auth-secrets-byok`** (`forge_auth.redaction`, regex + entropy + per-workspace known-secret registry) via a deep-walker (`redact_metadata`) over the nested `metadata` dict/list — F39 defines no pattern set of its own. Redaction runs **before** hashing and persistence, so neither Postgres, the hash inputs, the API, nor the export ever contains a secret. Credentials never travel in `metadata`; connection/key events carry only `key_prefix`/`***`/redacted diffs.
- **Fail-closed where it matters.** Security-critical events (approval decisions, policy overrides, write/deploy actions, key/role changes) are written atomically with the action on the same transaction — if the audit cannot be recorded, the action does not happen. Routine high-volume events are fail-open (async, retried) so observability never becomes a liveness risk for the agent loop (the same trade-off F10 makes for traces, but inverted for the security-critical subset).
- **Least-privilege read.** The audit log exposes cross-user, cross-agent actions and credential touches, so read is **admin-only** in V1 (a dedicated future `auditor` role can be added without schema change). There is **no** write/edit/delete surface over HTTP — only trusted in-process producers emit. All routes are authenticated (no anonymous access) and workspace-isolated; cross-workspace ids return 404 to avoid existence leakage.
- **Tamper alerting.** The scheduled verifier turns "immutable" from a claim into a monitored property: a break raises a metric/alert and is itself audited (`audit.chain_broken`).
- **Non-repudiation / accountability.** Every row carries a durable `actor_label` snapshot (survives user deletion via `ON DELETE SET NULL` on `actor_id`), a `request_id` correlation key, and a `detail_ref` to the full domain record.

## 9. Effort estimate & risk (S/M/L + key risks)

**Effort: L** — a cross-cutting contract + a tamper-evident store + a query/verify/export surface, plus the F37 reconciliation (contract supersession, in-place table migration + chain backfill, re-pointing F37's sink/route/page) and wiring guidance for the producers. Split: contract + hashing (S), extend F37 model + supersede F37 contract + `0003_audit_chain` migration with column renames/backfill + test updates (M), writer + chain + concurrency serialization (M), repository + extend F37 API + keyset pagination (M), worker verifier/archiver/async-sink (S/M), extend F37 web viewer (M), producer-integration docs/fixtures (S).

**Key risks:**
- **Hash-chain correctness under concurrency.** A race in `seq`/`prev_hash` assignment forks the chain and produces false tamper alarms. *Mitigation:* serialize appends per workspace via `audit_chain_head … FOR UPDATE`; pure, unit-tested hashing; explicit concurrency test (AC8). (Medium)
- **Fail-closed vs. fail-open boundary.** Mis-classifying a critical event as async loses non-repudiation; mis-classifying a hot-path event as critical can stall the agent loop. *Mitigation:* the `severity=critical` set is explicit and small (approvals, overrides, write/deploy, key/role); documented producer convention + tests (AC9/AC10). (Medium)
- **Immutability portability.** SQLite (unit tests) has no comparable trigger, risking a false sense of safety. *Mitigation:* repository-level append-only on all dialects + dialect-guarded trigger + the hash chain as the dialect-independent backstop; Postgres trigger behavior covered by a gated/PARKED test (AC6), DDL compile-checked everywhere. (Medium)
- **Redaction completeness.** A missed pattern writes a secret into a permanent, immutable record. *Mitigation:* reuse the single shared `SecretRedactor` (not a local copy), redact before hashing, and test deep/nested structures + the export path (AC4). (Medium → mitigated)
- **Volume / growth.** A high-volume agent platform produces many events; an unbounded immutable table grows. *Mitigation:* keyset pagination + covering indexes; monthly MinIO archival; table partitioning by month is a documented forward step (§12). (Low/Medium)
- **Double-counting vs. detail tables.** Risk of confusion with `workflow_transition`/`agent_steps`/`mcp_audit_log`. *Mitigation:* explicit boundary (security summary + chain here; full detail there) and `detail_ref` linkage. (Low)
- **In-place migration of F37's populated `audit_log`.** Renaming `actor_display`/`result`/`metadata` columns, widening enums, and back-filling a hash chain over existing rows must not lose or corrupt F37's auth audit history. *Mitigation:* a single reversible `0003_audit_chain` migration with an explicit data backfill (ordered by `created_at, id`), an upgrade/downgrade round-trip test, and a `verify_chain`-over-backfill test (AC1/AC20); because F39 lands in the same V1 cycle shortly after F37, the populated history is small. (Medium)
- **Two `AuditEvent`/`AuditSink` definitions during the transition.** If `forge_contracts.auth`'s interim types linger, producers may import the wrong one. *Mitigation:* delete them (or re-export the canonical types) in F39's migration release and re-point F37's `audit_service`; test AC19 asserts the adapter path. (Low)

## 10. Key files / paths (exact)

**packages/contracts/forge_contracts/**
- `audit.py` (new) — `AuditEvent`, `AuditEntry`, `ChainVerifyResult`, enums, `AuditSink` Protocol, `canonical_json`/`compute_payload_hash`/`compute_entry_hash`/`GENESIS_HASH`.
- `auth.py` (F37, edited) — remove the interim `AuditEvent`/`AuditSink` (or make them thin re-exports of `forge_contracts.audit` for one deprecation release).
- `tests/test_audit_contract.py`.

**packages/db/forge_db/**
- `models/audit_log.py` (F37, edited) — extend `AuditLog` with the chain/observability columns + the canonical enums and the renames (`actor_display→actor_label`, `result→outcome`, `metadata→metadata_json`); add the **new** `AuditChainHead` ORM (here or a sibling `audit_chain.py`); call `attach_immutability_trigger(AuditLog.__table__)`.
- `models/enums.py` — add `ActorType`, `AuditAction`, `AuditResourceType`, `AuditOutcome`, `AuditSeverity` (mirroring `forge_contracts.audit`); reconcile with F37's `PrincipalType`/`AuditResult`; extend `__all__`.
- `models/__init__.py` — export the new model/enums.
- `audit/__init__.py`, `audit/chain.py`, `audit/immutability.py`, `audit/redaction.py` (`redact_metadata` deep-walker over F37's `SecretRedactor` — no own pattern set), `audit/writer.py`, `audit/repository.py`.
- `migrations/versions/0003_audit_chain.py` (new; down_revision = F37's `0002_auth_secrets`) — add columns + renames + data backfill (incl. chain backfill), create `audit_chain_head`, drop F37's local trigger and re-attach via the helper, add indexes; reversible `downgrade`.
- `tests/audit/{test_writer.py,test_chain.py,test_immutability.py,test_pg_immutability.py}`; updated `tests/test_models.py` + `tests/test_migration.py`.

**apps/api/** (extend F37's existing audit module — same `apps/api` package layout F37 used; do not fork a parallel router/service)
- audit router — add `{id}`, `verify`, `export`, `actions`; re-point `GET /audit` at `AuditQueryRepository`.
- `audit_service.py` (F37, extended) — `AuditService` query/isolation/export + the `AuditSink.record` → `SqlAuditWriter` adapter.
- audit API schemas — `AuditListResponse`/`AuditListQuery` over `forge_contracts.audit`.
- `tests/audit/test_audit_api.py`.

**apps/worker/forge_worker/**
- `tasks/audit.py` — `audit.record`, `audit.verify_chain_all`, `audit.archive`.
- `tests/test_audit_tasks.py`.

**apps/web/**
- `app/(app)/settings/audit/page.tsx` (F37 base, F39 extends with filters/drawer/verify/export)
- `components/audit/{audit-detail-drawer,verify-integrity-button,export-dialog}.tsx`
- `lib/api/audit.ts`
- `__tests__/audit.test.tsx`

**deploy/**
- `scripts/install.sh` — `forge-audit` MinIO bucket bootstrap (retention/object-lock).
- `.env.example`, `.env.production.example` — `AUDIT_*` vars.
- `caddy/Caddyfile` + `nginx/forge.conf` — export streaming timeout/buffering.

## 11. Research references (relevant links from the spec/research report)

- Security — Audit log requirement ("Every agent action, tool call, MCP call, and approval — immutable, queryable") and Secret redaction ("Secrets stripped from logs, traces, and retrieval results"): `docs/FORGE_SPEC.md` §Security.
- Build Prompt constraint #9 ("An audit log exists for every agent action, tool call, and MCP call") and closing quality bar ("adopt, self-host, extend, **audit**, and fully trust this system in production"): `docs/FORGE_SPEC.md` §Build Prompt for Claude Code.
- MCP Security Rules rule 4 ("Full audit log: tool name, payload hash, result status, latency") — the per-MCP detail F39 summarizes/links: `docs/FORGE_SPEC.md` §MCP Integration → MCP Security Rules; research report §"Model Context Protocol → Security considerations" (DoD 2026 advisory: least-privilege, input validation, audit on all MCP calls): https://media.defense.gov/2026/Jun/02/2003943289/-1/-1/0/CSI_MCP_SECURITY.PDF
- Human Approval System (approval decisions are first-class audited events): `docs/FORGE_SPEC.md` §Human Approval System.
- Policy evaluation ("Every tool invocation checked against repo policy before execution") — produces the allow/deny events F39 records: `docs/FORGE_SPEC.md` §Security + §Repo Policy System.
- Observability Layer (run traces, token/cost, lineage) — F39's sibling stream; `detail_ref` links to it: `docs/FORGE_SPEC.md` §Product Scope + §Observability and Evaluation.
- Self-hosting production guidance (immutable storage, backups) — MinIO archival + object-lock posture: `docs/FORGE_SPEC.md` §Self-Hosting; https://distr.sh/blog/running-docker-in-production/
- **Audit foundation F39 extends:** `docs/implementation-slices/cross-cutting/F37-auth-secrets-byok.md` — originates the `audit_log` table + `audit_log_immutable` trigger (migration `0002_auth_secrets`), the canonical `SecretRedactor` (`forge_auth.redaction`), the interim `forge_contracts.auth.AuditEvent`/`AuditSink` (superseded here), `audit_service.py`, `GET /audit`, and the `settings/audit` page.
- **Downstream consumers that already assume F39's canonical contract:** `docs/implementation-slices/v3/F30-multi-team-rbac.md` (emits authz events through `forge_contracts.audit.AuditEvent`/`AuditSink`/`SqlAuditWriter`; "table origin: F37") and `.../v1/F09-mcp-gateway-v1.md` (fans MCP `AuditEntry` out to F39's central sink; adopts `attach_immutability_trigger("mcp_audit_log")`).
- Sibling slice hand-offs F39 fulfills: `docs/implementation-slices/v1/F07-feature-workflow-fsm.md` (deferred immutability trigger to `cross-cutting/F39-audit-log` + central store), `.../F09-mcp-gateway-v1.md` ("Platform audit log (cross-cutting, soft)"), `.../F08-plan-execute-verify-pr-approval.md` (audit-on-every-transition/approval), `.../F04-repo-policy.md` (policy `Decision` events), `.../F06-single-execution-agent.md` (append-only agent steps), `.../F10-run-trace-viewer.md` (redaction + immutability conventions).

## 12. Out of scope / future

- **Per-domain detail tables** (`workflow_transition`, `agent_steps`, `mcp_audit_log`, `repo_policy_snapshot`) — owned by F07/F06/F09/F04. F39 records the cross-cutting security summary + chain and links via `detail_ref`; it does not replace or migrate those tables (it only offers them the `attach_immutability_trigger` helper).
- **A dedicated `auditor` RBAC role / scoped self-audit reads** — V1 is admin-only; adding a role needs no schema change.
- **Per-domain action-vocabulary extensions** — F39 enumerates the core `AuditAction` set (incl. F37's auth/secret/key/connection/rbac actions). Slices that add new action kinds (e.g. `v3/F30-multi-team-rbac`'s `role_grant.*`/`team.*`/`project_access.*`) extend the `AuditAction` enum in their own slice; F39 owns the contract mechanism, not every future verb.
- **Table partitioning / tiered storage** (monthly partitions, cold storage) for very large deployments — V1 uses one indexed table + MinIO archival; partitioning is a documented forward step.
- **External SIEM / syslog / OpenTelemetry export** of audit events (push to Splunk/Datadog) — V2; the NDJSON export + stable hashes make this straightforward later.
- **Merkle-tree / signed checkpoints / external timestamping** (cryptographic anchoring beyond the per-workspace SHA-256 chain) — a V2 hardening; the chain + immutable archive are sufficient for V1.
- **WORM/object-lock enforcement configuration** of the MinIO bucket beyond a documented recommendation — folded into the self-hosting hardening slice.
- **Cross-workspace / org-level audit aggregation dashboards and analytics** — V3 multi-team RBAC (`v3/F30`) territory; V1 is per-workspace.
- **Rate limiting / quotas on the export endpoint** beyond auth + signed-URL TTL — folded into the platform-wide rate-limiting slice.
