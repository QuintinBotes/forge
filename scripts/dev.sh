#!/usr/bin/env bash
# Forge local dev stack helper.
#
#   scripts/dev.sh up     Build + start the whole stack, run migrations + seed,
#                         wait until every service is healthy, print URLs.
#   scripts/dev.sh down   Stop and remove the stack (keeps named volumes).
#   scripts/dev.sh logs   Follow logs for all services (or: logs <service>).
#   scripts/dev.sh seed    Re-run the idempotent demo-workspace seed.
#
# The underlying one command is:
#   docker compose -f deploy/docker-compose.dev.yml up -d --build --wait
set -euo pipefail

# Resolve repo root from this script's location so it works from any CWD.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${ROOT_DIR}"

COMPOSE_FILE="deploy/docker-compose.dev.yml"
ENV_FILE="deploy/.env.dev"
PROJECT="forge-dev"

WEB_PORT="${WEB_PORT:-3000}"
API_PORT="${API_PORT:-8000}"
CADDY_PORT="${CADDY_PORT:-8080}"

compose() {
  local args=(compose -p "${PROJECT}" -f "${COMPOSE_FILE}")
  [ -f "${ENV_FILE}" ] && args+=(--env-file "${ENV_FILE}")
  docker "${args[@]}" "$@"
}

print_urls() {
  cat <<EOF

Forge dev stack is up:
  Web UI   ->  http://localhost:${WEB_PORT}
  API      ->  http://localhost:${API_PORT}      (docs: http://localhost:${API_PORT}/docs)
  Edge     ->  http://localhost:${CADDY_PORT}      (Caddy: /api/*, /mcp/*, /*)

Demo workspace seeded: slug=demo  admin=admin@forge.local
EOF
}

cmd="${1:-up}"
case "${cmd}" in
  up)
    # The single command: build images, start detached, wait for healthy.
    compose up -d --build --wait
    compose ps
    print_urls
    ;;
  down)
    compose down "${@:2}"
    ;;
  logs)
    compose logs -f "${@:2}"
    ;;
  seed)
    # Re-run the idempotent demo seed against the running stack.
    compose run --rm seed
    ;;
  ps)
    compose ps
    ;;
  *)
    echo "usage: scripts/dev.sh {up|down|logs|seed|ps}" >&2
    exit 2
    ;;
esac
