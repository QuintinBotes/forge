#!/usr/bin/env bash
# Forge sandbox runtime installer (F34) — registers kernel-boundary OCI runtimes.
#
#   install-runtimes.sh --gvisor        install gVisor (runsc) + register `runsc`
#   install-runtimes.sh --firecracker   install Kata Containers + Firecracker +
#                                       register `kata-fc`
#   (both flags may be combined)
#
# Idempotent: the daemon.json `runtimes` merge preserves existing entries, the
# package installs are skipped when the binaries are already present, and the
# daemon is restarted only when daemon.json changed. Verifies registration via
# `docker info --format '{{json .Runtimes}}'`.
#
# Test/CI seams (all optional):
#   FORGE_DAEMON_JSON     daemon.json path        (default /etc/docker/daemon.json)
#   FORGE_SKIP_INSTALL=1  skip the binary installs (registration/merge only)
#   FORGE_SKIP_RESTART=1  skip `systemctl restart docker`
#   FORGE_SKIP_VERIFY=1   skip the `docker info` verification
set -euo pipefail

DAEMON_JSON="${FORGE_DAEMON_JSON:-/etc/docker/daemon.json}"
GVISOR_RUNTIME_NAME="${FORGE_SANDBOX_GVISOR_RUNTIME:-runsc}"
MICROVM_RUNTIME_NAME="${FORGE_SANDBOX_MICROVM_RUNTIME:-kata-fc}"

WANT_GVISOR=0
WANT_FIRECRACKER=0

err() { echo "install-runtimes: ERROR: $*" >&2; }
ok() { echo "install-runtimes: OK: $*"; }
info() { echo "install-runtimes: $*"; }

usage() {
  grep '^#' "$0" | head -n 12 | sed 's/^# \{0,1\}//'
  exit 64
}

for arg in "$@"; do
  case "${arg}" in
    --gvisor) WANT_GVISOR=1 ;;
    --firecracker) WANT_FIRECRACKER=1 ;;
    -h | --help) usage ;;
    *)
      err "unknown argument: ${arg}"
      usage
      ;;
  esac
done

if [ "${WANT_GVISOR}" -eq 0 ] && [ "${WANT_FIRECRACKER}" -eq 0 ]; then
  err "nothing to do: pass --gvisor and/or --firecracker"
  usage
fi

install_gvisor() {
  if [ "${FORGE_SKIP_INSTALL:-0}" = "1" ]; then
    info "FORGE_SKIP_INSTALL=1 — skipping runsc binary install"
    return 0
  fi
  if command -v runsc >/dev/null 2>&1; then
    ok "runsc already installed ($(runsc --version 2>/dev/null | head -n1 || true))"
    return 0
  fi
  # Official gVisor release channel (https://gvisor.dev/docs/user_guide/install/).
  local arch url tmp
  arch="$(uname -m)"
  url="https://storage.googleapis.com/gvisor/releases/release/latest/${arch}"
  tmp="$(mktemp -d)"
  info "downloading runsc + containerd-shim-runsc-v1 from ${url}"
  (
    cd "${tmp}"
    curl -fsSLO "${url}/runsc" -O "${url}/runsc.sha512" \
      -O "${url}/containerd-shim-runsc-v1" -O "${url}/containerd-shim-runsc-v1.sha512"
    sha512sum -c runsc.sha512 -c containerd-shim-runsc-v1.sha512
    chmod a+rx runsc containerd-shim-runsc-v1
    mv runsc containerd-shim-runsc-v1 /usr/local/bin/
  )
  rm -rf "${tmp}"
  ok "installed runsc to /usr/local/bin"
}

install_firecracker() {
  if [ "${FORGE_SKIP_INSTALL:-0}" = "1" ]; then
    info "FORGE_SKIP_INSTALL=1 — skipping Kata/Firecracker install"
    return 0
  fi
  if command -v kata-runtime >/dev/null 2>&1; then
    ok "kata-runtime already installed ($(kata-runtime --version 2>/dev/null | head -n1 || true))"
    return 0
  fi
  # Kata ships Firecracker + virtiofsd in kata-static (https://katacontainers.io/).
  err "automated Kata Containers install is distribution-specific; install kata-static"
  err "(https://github.com/kata-containers/kata-containers/releases) so that"
  err "'containerd-shim-kata-fc-v2' + a firecracker-configured kata runtime exist,"
  err "then re-run this script to register '${MICROVM_RUNTIME_NAME}'"
  return 1
}

# Merge a runtimes.<name> entry into daemon.json (create the file if absent).
merge_runtime() {
  local name="$1" path="$2" extra_json="${3:-}"
  python3 - "$DAEMON_JSON" "$name" "$path" "$extra_json" <<'PY'
import json
import pathlib
import sys

daemon_json, name, path, extra = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
target = pathlib.Path(daemon_json)
config = {}
if target.is_file() and target.read_text().strip():
    config = json.loads(target.read_text())
runtimes = config.setdefault("runtimes", {})
entry = {"path": path}
if extra:
    entry.update(json.loads(extra))
changed = runtimes.get(name) != entry
runtimes[name] = entry
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n")
print("changed" if changed else "unchanged")
PY
}

restart_docker_if_needed() {
  local changed="$1"
  if [ "${changed}" = "unchanged" ]; then
    info "daemon.json unchanged — no restart needed"
    return 0
  fi
  if [ "${FORGE_SKIP_RESTART:-0}" = "1" ]; then
    info "FORGE_SKIP_RESTART=1 — restart docker manually to pick up ${DAEMON_JSON}"
    return 0
  fi
  info "restarting docker to register the new runtimes"
  systemctl restart docker
}

verify_runtime() {
  local name="$1"
  if [ "${FORGE_SKIP_VERIFY:-0}" = "1" ]; then
    info "FORGE_SKIP_VERIFY=1 — skipping docker info verification for ${name}"
    return 0
  fi
  local runtimes
  runtimes="$(docker info --format '{{json .Runtimes}}' 2>/dev/null || true)"
  case "${runtimes}" in
    *"\"${name}\""*)
      ok "runtime '${name}' registered with the Docker daemon"
      ;;
    *)
      err "runtime '${name}' NOT visible in 'docker info' Runtimes after registration"
      return 1
      ;;
  esac
}

CHANGED="unchanged"

if [ "${WANT_GVISOR}" -eq 1 ]; then
  install_gvisor
  result="$(merge_runtime "${GVISOR_RUNTIME_NAME}" "/usr/local/bin/runsc")"
  if [ "${result}" = "changed" ]; then CHANGED="changed"; fi
  ok "merged runtimes.${GVISOR_RUNTIME_NAME} into ${DAEMON_JSON} (${result})"
fi

if [ "${WANT_FIRECRACKER}" -eq 1 ]; then
  install_firecracker
  result="$(merge_runtime "${MICROVM_RUNTIME_NAME}" "/usr/bin/containerd-shim-kata-fc-v2" \
    '{"runtimeArgs": []}')"
  if [ "${result}" = "changed" ]; then CHANGED="changed"; fi
  ok "merged runtimes.${MICROVM_RUNTIME_NAME} into ${DAEMON_JSON} (${result})"
  if [ ! -e /dev/kvm ]; then
    err "warning: /dev/kvm absent — microvm sandboxes will fail preflight on this host"
  fi
fi

restart_docker_if_needed "${CHANGED}"

if [ "${WANT_GVISOR}" -eq 1 ]; then verify_runtime "${GVISOR_RUNTIME_NAME}"; fi
if [ "${WANT_FIRECRACKER}" -eq 1 ]; then verify_runtime "${MICROVM_RUNTIME_NAME}"; fi

ok "done"
