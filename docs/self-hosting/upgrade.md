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

## Related

- [backup.md](backup.md) — take one before every upgrade.
- [restore.md](restore.md) — the rollback path of last resort.
- [troubleshooting.md](troubleshooting.md) — if health checks fail post-upgrade.
