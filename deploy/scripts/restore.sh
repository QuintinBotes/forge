#!/usr/bin/env bash
# Forge restore — restore Postgres + MinIO from a backup directory (Task 0.6).
#
# Usage: deploy/scripts/restore.sh <backup_dir>
# The backup_dir is one produced by backup.sh (contains postgres.dump [+ minio/]).
#
# This is destructive (it drops and recreates database objects); it requires an
# explicit confirmation before proceeding.
set -euo pipefail

COMPOSE_FILE="${COMPOSE_FILE:-deploy/docker-compose.yml}"
SRC="${1:-}"
POSTGRES_USER="${POSTGRES_USER:-forge}"
POSTGRES_DB="${POSTGRES_DB:-forge}"

if [[ -z "${SRC}" || ! -d "${SRC}" ]]; then
	echo "usage: $0 <backup_dir>" >&2
	exit 2
fi
if [[ ! -f "${SRC}/postgres.dump" ]]; then
	echo "error: ${SRC}/postgres.dump not found" >&2
	exit 2
fi

read -r -p "This will OVERWRITE database '${POSTGRES_DB}'. Type 'yes' to continue: " confirm
if [[ "${confirm}" != "yes" ]]; then
	echo "aborted"
	exit 1
fi

echo "==> Restoring Postgres from ${SRC}/postgres.dump"
docker compose -f "${COMPOSE_FILE}" exec -T db \
	pg_restore -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" --clean --if-exists \
	<"${SRC}/postgres.dump"

echo "OK: restore complete (MinIO artifacts in ${SRC}/minio must be re-uploaded with 'mc mirror')"
