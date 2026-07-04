# Upgrades

Forge upgrades are pull-build-migrate-verify, with a documented rollback at
every step. The two things that make an upgrade safe are a **fresh backup taken
immediately before** and a **pinned version you can roll back to**.

## Before you start

1. Read the release notes for breaking changes and required manual steps.
2. Take a full backup and confirm it completed — see [backup.md](backup.md):

   ```bash
   deploy/scripts/backup.sh ./backups
   ```

3. Record the version you are on so you can roll back to it:

   ```bash
   git rev-parse HEAD > ./backups/PREVIOUS_VERSION
   docker compose -f deploy/docker-compose.yml images > ./backups/PREVIOUS_IMAGES.txt
   ```

4. Announce a short maintenance window. Migrations may briefly lock tables.

## Pin the version you deploy

Application images are tagged from `FORGE_VERSION` (see
[../../deploy/docker-compose.yml](../../deploy/docker-compose.yml)). Set it
explicitly in `.env` rather than relying on a floating default, so the version
running is the version you intend and rollback is a one-line change:

```bash
# in .env
FORGE_VERSION=0.2.0
```

> Base images (`db`, `redis`, `minio`, `caddy`) are pinned to version tags, not
> immutable `@sha256` digests. Pin them by digest before a production rollout;
> see the PARKED note at the top of
> [../../deploy/docker-compose.yml](../../deploy/docker-compose.yml).

## Upgrade procedure

```bash
# 1. Fetch the new code / compose definitions
git fetch --tags
git checkout v0.2.0          # or your target tag

# 2. Build (or pull) the application images
docker compose -f deploy/docker-compose.yml build

# 3. Run database migrations FIRST, while the old app is still serving
docker compose -f deploy/docker-compose.yml run --rm api \
  alembic -c packages/db/alembic.ini upgrade head

# 4. Recreate services with the new images
docker compose -f deploy/docker-compose.yml up -d --remove-orphans

# 5. Confirm health
docker compose -f deploy/docker-compose.yml ps
curl -fsS http://localhost:8000/health
```

Forge migrations are written to be backward compatible (additive) so the running
old containers tolerate the new schema during the brief window in step 3-4.
Avoid destructive column drops in the same release that deploys the code which
stops using them — split them across two releases.

## Verify after upgrading

- `GET /health` returns 200 on `api` and `mcp-gateway`.
- The web UI loads and a known board renders.
- `alembic -c packages/db/alembic.ini current` matches `... heads`.
- A `/knowledge/search` query returns attributed results (proves the worker,
  vault, and vector store all came back).
- Tail logs for new errors:
  `docker compose -f deploy/docker-compose.yml logs --since 5m`.

## Rollback

If verification fails, roll back to the version recorded above.

```bash
# 1. Stop the application tier (leave data services running)
docker compose -f deploy/docker-compose.yml stop api worker mcp-gateway web

# 2. Return to the previous code/images
git checkout "$(cat ./backups/PREVIOUS_VERSION)"
# set FORGE_VERSION in .env back to the previous tag

# 3. If migrations must be reversed, restore the pre-upgrade database
#    backup (the safe, supported path):
deploy/scripts/restore.sh ./backups/<pre-upgrade-timestamp>

# 4. Bring the previous version back up
docker compose -f deploy/docker-compose.yml up -d --remove-orphans
```

Prefer **restoring the pre-upgrade backup** over `alembic downgrade`: downgrade
scripts are best-effort and can lose data written after the upgrade. This is why
step 2 of "Before you start" is non-negotiable.

## Migration reversibility (verified on real Postgres) — HARD-11

Every revision in `packages/db/migrations/versions` is proven **individually
reversible on live pgvector** by the migration test-suite, which runs three gates
against a throwaway database on a real Postgres server (they *skip* cleanly where
no Postgres is reachable, so the hermetic suite stays green):

```bash
# Point at a pgvector-enabled Postgres and run the live migration gates.
export FORGE_TEST_DATABASE_URL=postgresql+psycopg://forge:forge@localhost:5433/forge
uv run pytest packages/db -m postgres -q
```

- **Full round-trip** (`test_full_roundtrip_on_postgres`) — `upgrade head →
  downgrade base → upgrade head` runs clean, including `CREATE EXTENSION vector`,
  the `VECTOR(1536)` embedding column, and `tsvector` keyword columns that SQLite
  cannot represent.
- **Per-revision walk** (`test_stepwise_revision_walk`) — every revision is
  upgraded to, single-step-downgraded to its parent, and re-upgraded, so *each*
  down script is exercised on Postgres, not only on the SQLite substrate.
- **Data preservation** (`test_rollback_is_data_preserving`) — a single-step
  rollback of the additive head revision preserves seeded `workspace` / `app_user`
  rows.

### Reversible vs backup-first

The current chain is **additive**: every revision either creates its own tables
(dropped on downgrade) or adds columns/indexes (removed on downgrade), and none
drops or rewrites a pre-existing table's data. A single-step `alembic downgrade
-1` is therefore data-preserving for the schema. **However**, two caveats make
the pre-upgrade backup the supported rollback path:

- **Rows written after the upgrade** into a table/column that the downgrade drops
  are lost by definition (e.g. rolling back `0023_envelope_key_version` discards
  `api_key.key_version`/`rotated_at` written since the upgrade).
- **Code/schema coupling.** Rows written by the envelope cipher stay decryptable
  after a downgrade only while the code still understands the envelope blob — a
  schema downgrade must be paired with a code rollback (see
  [security.md](security.md)).

If a future release ships an **intentionally destructive** revision (a column
drop that discards data, a type rewrite), it will be called out here as
**backup-first** rather than data-preserving, and the release notes will say so.

## Related

- [backup.md](backup.md) — take one before every upgrade.
- [restore.md](restore.md) — the rollback path of last resort.
- [troubleshooting.md](troubleshooting.md) — if health checks fail post-upgrade.
