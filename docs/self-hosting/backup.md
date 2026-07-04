# Backups

Forge keeps durable state in three places, and a complete backup must capture
all three together:

| State | Where it lives | What you lose without it |
|---|---|---|
| Relational data | Postgres (`db` service, `db-data` volume) | Boards, specs, runs, audit log, users |
| Artifacts | MinIO (`minio` service, `minio-data` volume) | Run artifacts, logs, spec snapshots |
| Secrets | `.env` in the repo root + the BYOK vault rows in Postgres | The keys needed to decrypt the vault |

The BYOK secrets vault is encrypted at rest using `SECRET_KEY` from `.env`. A
Postgres dump therefore contains only ciphertext: **a database backup is
worthless without the matching `SECRET_KEY`.** Back up `.env` separately and
store it somewhere the database backup is not (see [security.md](security.md)).

## The backup script

A ready-made script lives at
[../../deploy/scripts/backup.sh](../../deploy/scripts/backup.sh). Run it from the
repo root against a running stack:

```bash
deploy/scripts/backup.sh ./backups
```

It writes a timestamped directory `./backups/<UTC-timestamp>/` containing:

- `postgres.dump` — a compressed custom-format dump (`pg_dump -Fc`).
- `minio/` — a mirror of the artifact bucket (`forge-artifacts` by default).

The script reads `POSTGRES_USER`, `POSTGRES_DB`, and `MINIO_BUCKET` from the
environment and falls back to the defaults in
[../../.env.example](../../.env.example).

## What the script does

```bash
# Postgres: logical dump in custom format (compressed, restorable selectively)
docker compose -f deploy/docker-compose.yml exec -T db \
  pg_dump -U forge -d forge -Fc > backups/<stamp>/postgres.dump

# MinIO: mirror the artifact bucket out of the container
docker compose -f deploy/docker-compose.yml exec -T minio sh -c \
  'mc alias set local http://localhost:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" \
   && mc mirror --overwrite local/forge-artifacts /tmp/minio-backup'
```

## Back up your secrets

The script intentionally does **not** copy `.env`, because secrets should not
land in the same archive as the data they protect. Copy it out of band:

```bash
cp .env /secure/offsite/forge.env.$(date -u +%Y%m%d)
```

Anything that can decrypt the vault must be treated as a crown-jewel secret:
`SECRET_KEY`, `AUTH_SECRET`, `POSTGRES_PASSWORD`, and `MINIO_ROOT_PASSWORD`.

## Scheduling

Run the script from `cron` on the Docker host. Example: a 02:00 UTC daily
backup with 14-day retention.

```cron
0 2 * * * cd /opt/forge && deploy/scripts/backup.sh /var/backups/forge >> /var/log/forge-backup.log 2>&1
15 2 * * * find /var/backups/forge -maxdepth 1 -type d -mtime +14 -exec rm -rf {} +
```

For off-host durability, sync the backup root to object storage or another
machine after each run (for example with `rclone` or `aws s3 sync`). A backup
that lives only on the server it protects is not a backup.

## Verify your backups

A backup you have never restored is a hypothesis, not a backup. Periodically
restore into a throwaway stack and confirm the row counts and a sign-in — the
procedure is in [restore.md](restore.md). Schedule a quarterly restore drill.

## Consistency notes

- `pg_dump` takes a consistent MVCC snapshot, so the Postgres dump is internally
  consistent without stopping the database.
- The Postgres dump and the MinIO mirror are taken seconds apart, not in a
  single transaction. For a strictly point-in-time-consistent pair, run the
  backup during a quiet window or briefly pause the `worker` service:
  `docker compose -f deploy/docker-compose.yml stop worker` before, and
  `... start worker` after.

## Related

- [restore.md](restore.md) — restoring and verifying a backup.
- [upgrade.md](upgrade.md) — always back up before upgrading.
- [security.md](security.md) — protecting backup credentials.
