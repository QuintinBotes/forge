# Restore

This guide restores a Forge instance from a backup produced by
[backup.md](backup.md) and verifies that the restore actually worked. Read it
end to end before you start: the Postgres restore is **destructive** and
overwrites the target database.

## Prerequisites

- A backup directory containing `postgres.dump` (and optionally `minio/`).
- The **matching `.env`** — specifically the `SECRET_KEY` that was in effect
  when the backup was taken. Without it the BYOK secrets vault cannot be
  decrypted and integrations will fail even though every row restores cleanly.
- A running stack: `docker compose -f deploy/docker-compose.yml up -d`.

## The restore script

Use [../../deploy/scripts/restore.sh](../../deploy/scripts/restore.sh). It asks
for explicit confirmation because it drops and recreates database objects.

```bash
deploy/scripts/restore.sh ./backups/<UTC-timestamp>
# This will OVERWRITE database 'forge'. Type 'yes' to continue: yes
```

It runs `pg_restore --clean --if-exists` against the `db` service, dropping
existing objects before recreating them from the dump.

## Restore order

1. **Put the matching `.env` in place** in the repo root, then start the stack
   so the database is healthy:

   ```bash
   docker compose -f deploy/docker-compose.yml up -d db
   docker compose -f deploy/docker-compose.yml ps
   ```

2. **Restore Postgres:**

   ```bash
   deploy/scripts/restore.sh ./backups/<UTC-timestamp>
   ```

3. **Restore MinIO artifacts.** The script restores Postgres only; re-upload the
   mirrored bucket from the backup:

   ```bash
   docker compose -f deploy/docker-compose.yml cp \
     ./backups/<UTC-timestamp>/minio minio:/tmp/minio-restore
   docker compose -f deploy/docker-compose.yml exec -T minio sh -c \
     'mc alias set local http://localhost:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" \
      && mc mirror --overwrite /tmp/minio-restore local/forge-artifacts'
   ```

4. **Bring the application tier up** (it was held back until data was in place):

   ```bash
   docker compose -f deploy/docker-compose.yml up -d
   ```

5. **Apply any pending migrations.** If the backup predates the running image,
   the schema may need to move forward (this is also the seam between restore and
   [upgrade.md](upgrade.md)):

   ```bash
   docker compose -f deploy/docker-compose.yml exec api \
     alembic -c packages/db/alembic.ini upgrade head
   ```

## Verify the restore

Do not declare success until every check below passes.

```bash
# 1. API is healthy
curl -fsS http://localhost:8000/health

# 2. Core tables have the expected row counts (compare to pre-backup notes)
docker compose -f deploy/docker-compose.yml exec -T db \
  psql -U forge -d forge -c \
  "select 'workspaces', count(*) from workspaces
   union all select 'projects', count(*) from projects
   union all select 'tasks', count(*) from tasks
   union all select 'retrieval_chunks', count(*) from retrieval_chunks;"

# 3. Artifacts are present
docker compose -f deploy/docker-compose.yml exec -T minio sh -c \
  'mc alias set local http://localhost:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" \
   && mc ls --recursive local/forge-artifacts | head'
```

Then verify at the application layer:

- Sign in through the web UI and confirm a known board renders.
- Open a run trace and confirm steps and audit entries are present.
- Confirm a BYOK-dependent feature works (e.g. `/knowledge/search`), which
  proves the vault decrypted with the restored `SECRET_KEY`.

## Troubleshooting

- **`pg_restore` errors about existing objects:** ensure you used the provided
  script (it passes `--clean --if-exists`); a manual `pg_restore` without those
  flags will collide with existing tables.
- **Auth/integrations fail after a clean data restore:** the `.env` `SECRET_KEY`
  does not match the backup. Restore the matching `.env` and restart `api` and
  `worker`. Vault rows cannot be salvaged without the original key.
- **Vector search returns nothing:** confirm the `pgvector` extension survived
  the restore (`\dx` in `psql`) and that re-indexing has run.

See [troubleshooting.md](troubleshooting.md) for the wider error catalogue.
