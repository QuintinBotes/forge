#!/usr/bin/env bash
# Local Postgres + Redis for the Python test suite, the same images as the CI
# python job (.github/workflows/ci.yml), published on 127.0.0.1 only.
#
#   scripts/test-services.sh up      Create the containers if absent, start them
#                                    if stopped, wait until both accept connections.
#   scripts/test-services.sh down    Stop and remove the two containers.
#   scripts/test-services.sh status  Print running / stopped / absent for each.
#   scripts/test-services.sh env     Print the export lines for the test URLs:
#                                      eval "$(scripts/test-services.sh env)"
#
# Ports: SCF_TEST_PG_PORT (default 5432), SCF_TEST_REDIS_PORT (default 6379).
# The test fixtures drop tables: point FORGE_TEST_DATABASE_URL only at this
# disposable database. The credentials are local test values, as in CI.
set -euo pipefail

PG_NAME="forge-test-pg"
REDIS_NAME="forge-test-redis"
PG_IMAGE="pgvector/pgvector:pg16"
REDIS_IMAGE="redis:7.4-alpine"
PG_PORT="${SCF_TEST_PG_PORT:-5432}"
REDIS_PORT="${SCF_TEST_REDIS_PORT:-6379}"
WAIT_SECONDS=60

# running | stopped | absent; any other inspect failure (no daemon, no permission) fails.
# Call it as an assignment (st="$(state ...)") so set -e sees the failure.
state() {
  local out
  if out="$(docker inspect -f '{{.State.Running}}' "$1" 2>&1)"; then
    [ "${out}" = true ] && echo running || echo stopped
  elif [[ "${out}" == *"No such"* ]]; then
    echo absent
  else
    echo "error: docker inspect $1 failed: ${out}" >&2
    return 1
  fi
}

port_free() {
  ! (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null
}

# ensure <name> <port> <port variable> <docker run args...>
ensure() {
  local name="$1" port="$2" var="$3" st bound
  shift 3
  st="$(state "${name}")"
  if [ "${st}" != absent ]; then
    # A container keeps the port it was created with, whatever ${var} says now.
    bound="$(docker inspect -f '{{range $p, $b := .HostConfig.PortBindings}}{{(index $b 0).HostPort}}{{end}}' "${name}")"
    if [ "${bound}" != "${port}" ]; then
      echo "error: ${name} is published on 127.0.0.1:${bound}, not ${port}; set ${var}=${bound}, or run '$0 down' first" >&2
      exit 1
    fi
  fi
  case "${st}" in
    running) echo "${name}: already running" ;;
    stopped)
      if ! port_free "${port}"; then
        echo "error: port ${port} on 127.0.0.1 is taken; run '$0 down', then set ${var} to a free port" >&2
        exit 1
      fi
      docker start "${name}" >/dev/null
      echo "${name}: started"
      ;;
    absent)
      if ! port_free "${port}"; then
        echo "error: port ${port} on 127.0.0.1 is taken; set ${var} to a free port" >&2
        exit 1
      fi
      docker run -d --name "${name}" "$@" >/dev/null
      echo "${name}: created on 127.0.0.1:${port}"
      ;;
  esac
}

# wait_for <name> <check command...>
wait_for() {
  local name="$1" waited=0
  shift
  until docker exec "${name}" "$@" >/dev/null 2>&1; do
    if [ "${waited}" -ge "${WAIT_SECONDS}" ]; then
      echo "error: ${name} did not accept connections within ${WAIT_SECONDS}s" >&2
      exit 1
    fi
    sleep 1
    waited=$((waited + 1))
  done
}

case "${1:-}" in
  up)
    ensure "${PG_NAME}" "${PG_PORT}" SCF_TEST_PG_PORT \
      -p "127.0.0.1:${PG_PORT}:5432" \
      -e POSTGRES_USER=forge -e POSTGRES_PASSWORD=forge -e POSTGRES_DB=forge_test \
      "${PG_IMAGE}"
    ensure "${REDIS_NAME}" "${REDIS_PORT}" SCF_TEST_REDIS_PORT \
      -p "127.0.0.1:${REDIS_PORT}:6379" "${REDIS_IMAGE}"
    # Over TCP: during first-boot init Postgres listens on its socket only.
    wait_for "${PG_NAME}" pg_isready -h 127.0.0.1 -U forge -d forge_test
    wait_for "${REDIS_NAME}" redis-cli ping
    echo "test services ready"
    ;;
  down)
    for name in "${PG_NAME}" "${REDIS_NAME}"; do
      st="$(state "${name}")"
      if [ "${st}" != absent ]; then
        docker rm -f -v "${name}" >/dev/null
        echo "${name}: removed"
      fi
    done
    ;;
  status)
    for name in "${PG_NAME}" "${REDIS_NAME}"; do
      st="$(state "${name}")"
      echo "${name}: ${st}"
    done
    ;;
  env)
    echo "export FORGE_TEST_DATABASE_URL=postgresql+psycopg://forge:forge@127.0.0.1:${PG_PORT}/forge_test"
    echo "export FORGE_REDIS_URL=redis://127.0.0.1:${REDIS_PORT}/0"
    ;;
  *)
    echo "usage: $0 {up|down|status|env}" >&2
    exit 2
    ;;
esac
