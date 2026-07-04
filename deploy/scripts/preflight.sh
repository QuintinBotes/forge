#!/usr/bin/env bash
# Forge deploy preflight checks.
#
# F19 adds a Docker Engine >= 26.1 gate: per-task sandbox containers mount only the
# run's subpath of the worktree volume via `volume-subpath`, which requires Engine
# 26.1+. (Fallback for older engines: a per-run named volume — see F19 §9.)
#
# F34 adds the kernel-boundary sandbox gates: when FORGE_SANDBOX_KIND is gvisor or
# microvm, the matching OCI runtime (runsc / kata-fc) must be registered with the
# daemon, and microvm additionally requires /dev/kvm (+ best-effort nested-virt
# check). Forge NEVER silently downgrades to a weaker runtime — a missing
# capability fails here, before `up`.
set -euo pipefail

MIN_ENGINE_MAJOR=26
MIN_ENGINE_MINOR=1

# F34 kernel-boundary sandbox configuration (mirrors forge_agent SandboxSettings).
FORGE_SANDBOX_KIND="${FORGE_SANDBOX_KIND:-worktree}"
FORGE_SANDBOX_GVISOR_RUNTIME="${FORGE_SANDBOX_GVISOR_RUNTIME:-runsc}"
FORGE_SANDBOX_MICROVM_RUNTIME="${FORGE_SANDBOX_MICROVM_RUNTIME:-kata-fc}"
FORGE_SANDBOX_REQUIRE_KVM="${FORGE_SANDBOX_REQUIRE_KVM:-true}"
# Overridable for tests (stubbing the device node).
FORGE_KVM_DEVICE="${FORGE_KVM_DEVICE:-/dev/kvm}"

err() { echo "preflight: ERROR: $*" >&2; }
warn() { echo "preflight: WARN: $*" >&2; }
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

# F34: is the given OCI runtime registered with the daemon (`docker info` Runtimes)?
runtime_registered() {
  local runtime="$1"
  local runtimes
  runtimes="$(docker info --format '{{json .Runtimes}}' 2>/dev/null || true)"
  case "${runtimes}" in
    *"\"${runtime}\""*) return 0 ;;
    *) return 1 ;;
  esac
}

check_sandbox_runtime() {
  local runtime="$1" flag="$2"
  if runtime_registered "${runtime}"; then
    ok "OCI runtime '${runtime}' registered with the Docker daemon"
    return 0
  fi
  err "FORGE_SANDBOX_KIND=${FORGE_SANDBOX_KIND} requires the '${runtime}' OCI runtime registered with the Docker daemon"
  err "hint: run deploy/scripts/install-runtimes.sh ${flag} on the daemon host, then retry"
  return 1
}

check_kvm() {
  if [ "${FORGE_SANDBOX_REQUIRE_KVM}" != "true" ]; then
    warn "FORGE_SANDBOX_REQUIRE_KVM=${FORGE_SANDBOX_REQUIRE_KVM} — skipping the /dev/kvm gate"
    return 0
  fi
  if [ -e "${FORGE_KVM_DEVICE}" ]; then
    ok "KVM device ${FORGE_KVM_DEVICE} present"
  else
    err "FORGE_SANDBOX_KIND=microvm requires ${FORGE_KVM_DEVICE} on the daemon host"
    err "hint: use bare metal, or enable nested virtualization on your cloud VM; gVisor (FORGE_SANDBOX_KIND=gvisor) needs no KVM"
    return 1
  fi
  # Best-effort nested-virt visibility (warn-only: bare metal has vmx/svm; a
  # nested guest may not expose /proc/cpuinfo flags even when KVM works).
  if [ -r /proc/cpuinfo ] && ! grep -Eq '(vmx|svm)' /proc/cpuinfo; then
    warn "no vmx/svm flag in /proc/cpuinfo — verify nested virtualization is enabled"
  fi
  return 0
}

check_kernel_sandbox() {
  case "${FORGE_SANDBOX_KIND}" in
    gvisor)
      check_sandbox_runtime "${FORGE_SANDBOX_GVISOR_RUNTIME}" "--gvisor"
      ;;
    microvm)
      check_sandbox_runtime "${FORGE_SANDBOX_MICROVM_RUNTIME}" "--firecracker"
      check_kvm
      ;;
    *)
      ok "FORGE_SANDBOX_KIND=${FORGE_SANDBOX_KIND} — no kernel-boundary runtime required"
      ;;
  esac
}

main() {
  check_docker_engine_version
  check_kernel_sandbox
  ok "all preflight checks passed"
}

main "$@"
