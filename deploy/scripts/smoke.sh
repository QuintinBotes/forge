#!/usr/bin/env bash
# HARD-07 — production-compose smoke: build artifacts must actually RUN.
#
#   up -d (distinct project) -> alembic upgrade head -> wait for every
#   healthchecked service to report healthy -> hit /health on api +
#   mcp-gateway (+ / on web) -> down -v.
#
# The production compose publishes NO app ports (only Caddy is on the edge),
# so the /health probes exec inside the containers with the same stdlib
# probes the healthchecks use. Non-zero exit on any failure; always tears
# the stack down (trap).
#
# Env knobs: SMOKE_PROJECT (forge-prod-smoke), SMOKE_TIMEOUT_SECONDS (300),
# COMPOSE_FILE (deploy/docker-compose.yml), FORGE_VERSION (0.1.0).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
COMPOSE_FILE="${COMPOSE_FILE:-deploy/docker-compose.yml}"
PROJECT="${SMOKE_PROJECT:-forge-prod-smoke}"
TIMEOUT="${SMOKE_TIMEOUT_SECONDS:-300}"

DC=(docker compose -p "$PROJECT" -f "$REPO_ROOT/$COMPOSE_FILE")
# Every service with a healthcheck (autoheal has none — it is the watcher).
WAIT_SERVICES=(db redis minio api mcp-gateway web worker caddy docker-proxy sandbox-proxy)

RESULT="FAIL"
cleanup() {
  if [[ "$RESULT" != "PASS" ]]; then
    echo "--- smoke failed; last container logs ---" >&2
    "${DC[@]}" ps >&2 || true
    "${DC[@]}" logs --tail 40 >&2 || true
  fi
  "${DC[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
  echo "SMOKE ${RESULT}"
  [[ "$RESULT" == "PASS" ]]
}
trap cleanup EXIT

wait_healthy() {
  local deadline remaining
  deadline=$(( $(date +%s) + TIMEOUT ))
  while :; do
    remaining="$("${DC[@]}" ps --format json | python3 -c '
import json, sys

want = set(sys.argv[1:])
state = {}
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    obj = json.loads(line)
    state[obj.get("Service")] = obj.get("Health") or obj.get("State") or ""
missing = [svc + "=" + (state.get(svc) or "absent")
           for svc in sorted(want) if state.get(svc) != "healthy"]
print(" ".join(missing))
' "${WAIT_SERVICES[@]}")"
    if [[ -z "$remaining" ]]; then
      echo "all services healthy: ${WAIT_SERVICES[*]}"
      return 0
    fi
    if (( $(date +%s) > deadline )); then
      echo "TIMEOUT (${TIMEOUT}s) waiting for healthy: $remaining" >&2
      return 1
    fi
    echo "waiting for: $remaining"
    sleep 5
  done
}

echo "== 1/5 data tier up (db redis minio) =="
"${DC[@]}" up -d --wait db redis minio

echo "== 2/5 migrations (alembic upgrade head) =="
"${DC[@]}" run --rm --no-deps api alembic -c packages/db/alembic.ini upgrade head

echo "== 3/5 full stack up =="
"${DC[@]}" up -d

echo "== 4/5 waiting for healthy =="
wait_healthy

echo "== 5/5 /health probes =="
"${DC[@]}" exec -T api python -c '
import sys
import urllib.request

r = urllib.request.urlopen("http://localhost:8000/health")
body = r.read().decode()
print("api /health", r.status, body)
sys.exit(0 if r.status == 200 and "\"status\":\"ok\"" in body else 1)
'
"${DC[@]}" exec -T mcp-gateway python -c '
import sys
import urllib.request

r = urllib.request.urlopen("http://localhost:8001/health")
body = r.read().decode()
print("mcp-gateway /health", r.status, body)
sys.exit(0 if r.status == 200 and "\"status\":\"ok\"" in body else 1)
'
"${DC[@]}" exec -T web node -e '
fetch("http://localhost:3000/")
  .then((r) => { console.log("web /", r.status); process.exit(r.ok ? 0 : 1); })
  .catch((e) => { console.error(e); process.exit(1); });
'

RESULT="PASS"
