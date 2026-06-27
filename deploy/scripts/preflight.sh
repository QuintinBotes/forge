#!/usr/bin/env bash
# Forge deploy preflight checks.
#
# F19 adds a Docker Engine >= 26.1 gate: per-task sandbox containers mount only the
# run's subpath of the worktree volume via `volume-subpath`, which requires Engine
# 26.1+. (Fallback for older engines: a per-run named volume — see F19 §9.)
set -euo pipefail

MIN_ENGINE_MAJOR=26
MIN_ENGINE_MINOR=1

err() { echo "preflight: ERROR: $*" >&2; }
ok() { echo "preflight: OK: $*"; }

check_docker_engine_version() {
  if ! command -v docker >/dev/null 2>&1; then
    err "docker not found on PATH"
    return 1
  fi
  # Server (engine) version, e.g. "26.1.4".
  local version
  version="$(docker version --format '{{.Server.Version}}' 2>/dev/null || true)"
  if [ -z "${version}" ]; then
    err "could not determine Docker Engine version (is the daemon running?)"
    return 1
  fi
  local major minor
  major="$(printf '%s' "${version}" | cut -d. -f1)"
  minor="$(printf '%s' "${version}" | cut -d. -f2)"
  if [ "${major}" -gt "${MIN_ENGINE_MAJOR}" ] || \
     { [ "${major}" -eq "${MIN_ENGINE_MAJOR}" ] && [ "${minor}" -ge "${MIN_ENGINE_MINOR}" ]; }; then
    ok "Docker Engine ${version} >= ${MIN_ENGINE_MAJOR}.${MIN_ENGINE_MINOR} (volume-subpath supported)"
    return 0
  fi
  err "Docker Engine ${version} < ${MIN_ENGINE_MAJOR}.${MIN_ENGINE_MINOR}; F19 sandbox volume-subpath requires >= ${MIN_ENGINE_MAJOR}.${MIN_ENGINE_MINOR}"
  return 1
}

main() {
  check_docker_engine_version
  ok "all preflight checks passed"
}

main "$@"
