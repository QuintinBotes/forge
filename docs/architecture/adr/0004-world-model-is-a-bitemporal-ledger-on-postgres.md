# 0004. The world model is an append-only bitemporal ledger on forge's Postgres

**Status:** Accepted
**Date:** 2026-11-07

## Context

Utopia's core is a two-clock fact ledger. Its dedup is read-then-write without a unique index, so
parallel documents can double-insert (documented race). Lore spread its graph across Postgres,
Dgraph and utopia and lost memories for ~60 days to schema drift. Forge already runs Postgres 16 with
pgvector, has an immutability-trigger helper, and tests on SQLite for speed.

## Decision

- Facts live in `world_fact` on forge's Postgres. No second store.
- World time is `valid_from`/`valid_to` + per-bound precision; record time is
  `created_at`/`invalidated_at`. Two columns per axis, not `tstzrange`, so the same model runs on the
  SQLite unit lane; a GiST range index can be added later as an expression index without a model
  change.
- Rows are append-only with a **one-way tombstone**: a trigger allows only `invalidated_at` and
  `invalidated_reason` to move from NULL to a value once; DELETE raises.
- A partial unique index on `(workspace_id, dedup_key) WHERE invalidated_at IS NULL` makes duplicate
  live facts impossible.
- Exactly one implementation of `holds_at` and `held_at` exists in Python and one in SQL.

## Consequences

- Corrections are invalidate + insert in one transaction; history is never lost.
- Concurrency safety comes from the database, not from application locks.
- Range queries on very large ledgers may need the GiST index (H4).
