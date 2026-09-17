#!/usr/bin/env bash
# Forge backup — Postgres dump + MinIO mirror + spec documents.
#
# Creates a timestamped backup directory containing a compressed Postgres dump,
# a mirror of the MinIO artifact bucket, and the spec engine's documents.
# Intended to run from the repo root against a running
# `docker compose -f deploy/docker-compose.yml` stack.
#
# The spec documents are a THIRD store, not a copy of the other two. The spec
# engine is filesystem backed: Postgres holds the version history
# (`spec_version`), but the live document — what `GET /spec/specs/{id}` returns
# and what the implementation gate reads — is a file on the `forge-specs`
# volume. A Postgres-and-MinIO-only backup restores a stack with no specs.
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
FORGE_SPEC_ROOT="${FORGE_SPEC_ROOT:-/srv/forge/specs}"

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

echo "==> Spec documents (forge-specs volume)"
docker compose -f "${COMPOSE_FILE}" cp "api:${FORGE_SPEC_ROOT}" "${DEST}/specs" 2>/dev/null ||
	echo "    (no spec documents to copy yet)"

echo "OK: backup complete at ${DEST}"
