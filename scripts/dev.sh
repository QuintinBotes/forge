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

# The admin API key the `seed` one-shot minted, recovered from its log. The
# token is printed exactly once and is unrecoverable afterwards, so `up` has to
# read it back out of the container log rather than re-derive it.
seed_api_key() {
  compose logs --no-log-prefix seed 2>/dev/null \
    | grep -oE 'forge_[a-z_]+_[A-Za-z0-9_-]{20,}' \
    | tail -n 1
}

print_urls() {
  local key
  key="$(seed_api_key)"

  cat <<EOF

Forge dev stack is up. Open the edge — it serves the UI and /api/* on ONE
origin, so the browser needs no CORS and no separate API host:

  Forge    ->  http://localhost:${CADDY_PORT}

  (direct, bypasses the proxy: web http://localhost:${WEB_PORT} ·
   API http://localhost:${API_PORT} · docs http://localhost:${API_PORT}/docs)

Demo workspace seeded: slug=demo  admin=admin@forge.local
EOF

  if [ -n "${key}" ]; then
    cat <<EOF

Admin API key (dev only — shown once by the seed, full admin on the demo
workspace). Paste it into the UI's Connect dialog on first load:

  ${key}

  curl -H "Authorization: Bearer ${key}" http://localhost:${CADDY_PORT}/api/auth/me
EOF
  else
    cat <<EOF

No admin API key found in the seed log. Mint one with:

  scripts/dev.sh seed
EOF
  fi
  echo
}

cmd="${1:-up}"
case "${cmd}" in
  up)
    # Build and start everything, including the one-shot `migrate` and `seed`.
    compose up -d --build

    # Then wait for health — but only on the long-running services. `--wait`
    # treats a one-shot that has exited as a failed service even when it exited
    # 0, so waiting on the whole project makes a completely successful bring-up
    # return non-zero and `make dev` report failure. Naming the long-running
    # services keeps the health gate while letting migrate/seed finish and exit.
    compose up -d --wait \
      db redis minio api worker mcp-gateway web caddy

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
    # Re-run the idempotent demo seed against the running stack. This retires the
    # previous bootstrap key and mints a fresh one, so it is also how you recover
    # access after losing the token.
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
