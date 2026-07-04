#!/usr/bin/env bash
# Forge backup — Postgres dump + MinIO mirror (plan Task 0.6 substrate).
#
# Creates a timestamped backup directory containing a compressed Postgres dump
# and a mirror of the MinIO artifact bucket. Intended to run from the repo root
# against a running `docker compose -f deploy/docker-compose.yml` stack.
#
# Usage: deploy/scripts/backup.sh [output_dir]
set -euo pipefail

COMPOSE_FILE="${COMPOSE_FILE:-deploy/docker-compose.yml}"
OUT_ROOT="${1:-./backups}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
DEST="${OUT_ROOT}/${STAMP}"

POSTGRES_USER="${POSTGRES_USER:-forge}"
POSTGRES_DB="${POSTGRES_DB:-forge}"
MINIO_BUCKET="${MINIO_BUCKET:-forge-artifacts}"

mkdir -p "${DEST}"
echo "Backing up to ${DEST}"

echo "==> Postgres dump"
docker compose -f "${COMPOSE_FILE}" exec -T db \
	pg_dump -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" -Fc \
	>"${DEST}/postgres.dump"

echo "==> MinIO mirror (bucket: ${MINIO_BUCKET})"
docker compose -f "${COMPOSE_FILE}" exec -T minio sh -c "\
	mc alias set local http://localhost:9000 \"\$MINIO_ROOT_USER\" \"\$MINIO_ROOT_PASSWORD\" >/dev/null && \
	mc mirror --overwrite \"local/${MINIO_BUCKET}\" /tmp/minio-backup >/dev/null 2>&1 || true"
docker compose -f "${COMPOSE_FILE}" cp "minio:/tmp/minio-backup" "${DEST}/minio" 2>/dev/null || \
	echo "    (no MinIO data to copy yet)"

echo "OK: backup complete at ${DEST}"
