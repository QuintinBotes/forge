# Persistence Progress — In-Memory Stores → Real Postgres

Goal: give in-memory service stores **real Postgres persistence without changing
behaviour**. For each target a Postgres-backed repository was implemented that
satisfies the **same frozen Protocol** the InMemory store already implements. The
InMemory store **stays** and remains the **default** so every existing unit test
runs untouched (hermetic, no Postgres). The backend is chosen at the composition
root by an env flag, `FORGE_<AREA>_BACKEND=db|memory`, **default `memory`**.

Method per target (TDD, PARK-DON'T-FAKE): stand up the DB repo against the
canonical `forge_db` ORM (extend, never fork), add a chained Alembic migration
where a new column/table was needed, wire the seam behind the env flag, and add a
DB-backed integration test that exercises the Protocol against real Postgres
(round-trip, filtering, ordering, constraints). Nothing was weakened or skipped
to go green.

## Results

| id | refuted | repaired | decision |
|----|---------|----------|----------|
| board-service | 0 | no | committed |
| audit-store | 0 | no | committed |
| approval-repository | 0 | no | committed |
| policy-override-grant-store | 0 | no | committed |
| api-key-backend | 0 | no | committed |
| policy-audit-sink | 0 | no | committed |
| spec-projection-repository | 0 | no | committed |
| pm-link-repository | 0 | no | committed |
| secret-vault-store | 0 | no | committed |
| idempotency-store | 0 | no | committed |

`refuted` = integration assertions that failed and forced a code change during
TDD; `repaired` = whether the InMemory store itself had to be touched (it never
did — the Protocol was the only shared surface). All ten are committed as
individual `feat(db/<target>): Postgres persistence` commits.

## Target → repo → migration → test map

| target | Protocol (frozen) | Postgres repo | Alembic migration | table(s) | env flag | DB integration test |
|--------|-------------------|---------------|-------------------|----------|----------|---------------------|
| board-service | `forge_contracts.BoardService` | `packages/board-core/forge_board/sql_service.py` (`SqlAlchemyBoardService`) | `0024_board_persistence` (add columns) | `task`, `epic`, `sprint`, `spec`, `task_dependency` (existing F-series tables extended) | `FORGE_BOARD_BACKEND` | `packages/board-core/tests/test_sql_board_service.py` |
| audit-store | `forge_contracts.AuditSink` | `apps/api/forge_api/observability/audit_db.py` | `0025_observability_audit_store` (new) | `observability_audit_entry`, `observability_audit_chain_head` | `FORGE_AUDIT_BACKEND` (MCP bridge: `FORGE_MCP_AUDIT_BACKEND`) | `apps/api/tests/test_audit_store_db.py` |
| approval-repository | `ApprovalRepository` (F36) | `apps/api/forge_api/services/approval_repository_db.py` | `0026_approval_repository_columns` (add columns) | `approval_request` (existing, extended) | `FORGE_APPROVAL_BACKEND` | `apps/api/tests/test_approval_repository_db.py` |
| policy-override-grant-store | `GrantStore` (F36 J5) | `apps/api/forge_api/services/policy_override_grant_store_db.py` | none — reuses existing table | `policy_override_grant` (existing) | `FORGE_OVERRIDE_GRANT_BACKEND` | `apps/api/tests/test_policy_override_grant_store_db.py` |
| api-key-backend | API-key backend Protocol | `apps/api/forge_api/auth/apikeys_db.py` | none — reuses existing table | `platform_api_key` (existing) | `FORGE_APIKEY_BACKEND` | `apps/api/tests/test_apikeys_db.py` |
| policy-audit-sink | Policy audit sink Protocol (F29) | `apps/api/forge_api/services/policy_audit_sink_db.py` | none — reuses existing table | `policy_rule_evaluation` (existing) | `FORGE_POLICY_AUDIT_BACKEND` | `apps/api/tests/test_policy_audit_sink_db.py` |
| spec-projection-repository | Projection repository Protocol (F23) | `apps/api/forge_api/services/projection_repository_db.py` | none — reuses existing tables | `traceability_spec_rollup`, `traceability_criterion_link` (existing) | `FORGE_PROJECTION_BACKEND` | `apps/api/tests/test_projection_repository_db.py` |
| pm-link-repository | Link repository Protocol (F18) | `apps/api/forge_api/services/pm_link_repository_db.py` | none — reuses existing table | `pm_task_link` (existing) | `FORGE_PM_LINK_BACKEND` | `apps/api/tests/test_pm_link_repository_db.py` |
| secret-vault-store | `forge_contracts.Vault` / secret store | `apps/api/forge_api/auth/vault_db.py` | `0027_secret_vault_store` (new) | `secret` | `FORGE_SECRET_BACKEND` | `apps/api/tests/test_secret_vault_store_db.py` |
| idempotency-store | Idempotency store Protocol | `apps/api/forge_api/middleware/idempotency_db.py` | `0028_idempotency_store` (new) | `idempotency_key` | `FORGE_IDEMPOTENCY_BACKEND` | `apps/api/tests/test_idempotency_store_db.py` |

## Which services are now Postgres-backed vs still memory-only

All ten targets are now **Postgres-capable**: each has a real Postgres-backed
repository selectable at the composition root. They remain **memory-default** —
`db` is opt-in per area — so unit tests stay hermetic. Concretely:

- **Postgres-backed available (opt-in via env flag), memory default:** board
  service, observability audit store, approval repository, policy-override grant
  store, platform API-key backend, policy-audit sink, spec/traceability
  projection repository, PM-sync link repository, encrypted secret-vault store,
  HTTP idempotency store.
- **Still memory-only (out of scope for this pass):** every other in-memory seam
  not listed above keeps its InMemory implementation with no DB alternative yet
  (e.g. worker task-dedup runs behind its own separate `FORGE_TASK_DEDUP_BACKEND`
  flag and was not part of this batch).

The InMemory implementation of each target is retained and is the default, so no
existing test needed modification.

## Alembic migrations added

Chained sequentially onto the prior head `0023_envelope_key_version`; each has
both `upgrade()` and `downgrade()` and applies cleanly on the `:5433` pgvector
test DB. Five of the ten targets required schema; the other five persist against
tables already created by earlier F-series migrations.

| revision | down_revision | change |
|----------|---------------|--------|
| `0024_board_persistence` | `0023_envelope_key_version` | adds board columns to `task`/`epic`/`sprint`/`spec`/`task_dependency` |
| `0025_observability_audit_store` | `0024_board_persistence` | new `observability_audit_entry` + `observability_audit_chain_head` (hash-chained audit) |
| `0026_approval_repository_columns` | `0025_observability_audit_store` | adds columns to `approval_request` |
| `0027_secret_vault_store` | `0026_approval_repository_columns` | new `secret` table (envelope-encrypted vault) |
| `0028_idempotency_store` | `0027_secret_vault_store` | new `idempotency_key` table |

Current Alembic head: **`0028_idempotency_store`**.

## Env flags added (composition root)

All read in `apps/api/forge_api/settings.py` (each defaults to `memory`):

| flag | selects |
|------|---------|
| `FORGE_BOARD_BACKEND` | board service (`memory` → `InMemoryBoardService`, `db` → `SqlAlchemyBoardService`) |
| `FORGE_AUDIT_BACKEND` | observability audit store |
| `FORGE_MCP_AUDIT_BACKEND` | MCP audit bridge (forwards to the audit store) |
| `FORGE_APPROVAL_BACKEND` | approval repository |
| `FORGE_OVERRIDE_GRANT_BACKEND` | policy-override grant store |
| `FORGE_APIKEY_BACKEND` | platform API-key backend |
| `FORGE_POLICY_AUDIT_BACKEND` | policy-audit sink |
| `FORGE_PROJECTION_BACKEND` | spec/traceability projection repository |
| `FORGE_PM_LINK_BACKEND` | PM-sync link repository |
| `FORGE_SECRET_BACKEND` | encrypted secret-vault store |
| `FORGE_IDEMPOTENCY_BACKEND` | HTTP idempotency store |

## Parked items

- None functional. The only prior parked note — that the whole-repo
  `uv run pytest -q` had not been confirmed green because the earlier report was
  forced while pytest was still buffering — is **closed**: the full suite was
  re-run under `FORGE_TEST_DATABASE_URL` (pgvector `:5433`) and is green (see
  Green Gate below).

## Green gate

- `uv run ruff check .` → **All checks passed!**
- `FORGE_TEST_DATABASE_URL=postgresql+psycopg://forge:forge@localhost:5433/forge uv run pytest -q`
  → **3688 passed, 53 skipped, 23 warnings in 667.85s** (exit 0).

The 53 skips are pre-existing live-lane tests that skip cleanly when their
external creds/binaries are absent (live MCP transport, live GitHub webhook
secret, live model provider, `promtool`/`amtool` on PATH) — none were introduced
or weakened by this work.
