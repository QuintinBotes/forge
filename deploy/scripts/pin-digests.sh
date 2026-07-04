#!/usr/bin/env bash
# HARD-07 — resolve every pulled/base image's immutable digest, rewrite the
# compose files + Dockerfiles to `name:tag@sha256:<digest>`, and (re)write
# deploy/build-manifest.json.
#
# Usage:
#   deploy/scripts/pin-digests.sh            # resolve + rewrite + manifest (needs docker + network)
#   deploy/scripts/pin-digests.sh --check    # offline lint: fail if any pulled/base ref is unpinned
#
# Idempotent: re-running deliberately rolls digests forward to whatever the
# tags currently resolve to (a conscious supply-chain decision, never implicit).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
COMPOSE_FILES=("$REPO_ROOT/deploy/docker-compose.yml" "$REPO_ROOT/deploy/docker-compose.dev.yml")
MANIFEST="$REPO_ROOT/deploy/build-manifest.json"
FORGE_VERSION="${FORGE_VERSION:-0.1.0}"
DIGEST_RE='@sha256:[0-9a-f]{64}$'

dockerfiles() {
  find "$REPO_ROOT/deploy/docker" -maxdepth 1 -name '*.Dockerfile' | sort
}

# Every pulled `image:` ref in the compose files (first-party forge/* excluded).
compose_refs() {
  grep -hE '^[[:space:]]+image:[[:space:]]' "${COMPOSE_FILES[@]}" \
    | awk '{print $2}' | grep -v '^forge/' | sort -u
}

# Every `FROM <ref>` in the Dockerfiles (multi-stage: ref is the 2nd token).
dockerfile_refs() {
  # shellcheck disable=SC2046 # dockerfiles() emits one safe path per line
  grep -hE '^FROM ' $(dockerfiles) | awk '{print $2}' | sort -u
}

check() {
  local ok=0 ref
  while IFS= read -r ref; do
    if ! [[ "$ref" =~ $DIGEST_RE ]]; then
      echo "UNPINNED compose image: $ref" >&2
      ok=1
    fi
  done < <(compose_refs)
  while IFS= read -r ref; do
    if ! [[ "$ref" =~ $DIGEST_RE ]]; then
      echo "UNPINNED Dockerfile FROM: $ref" >&2
      ok=1
    fi
  done < <(dockerfile_refs)
  if [[ "$ok" -ne 0 ]]; then
    echo "FAIL: unpinned image references found (run deploy/scripts/pin-digests.sh)" >&2
    return 1
  fi
  echo "OK: every pulled compose image and Dockerfile base is @sha256-pinned"
}

resolve_digest() {
  local ref="$1"
  docker buildx imagetools inspect "$ref" --format '{{json .Manifest.Digest}}' | tr -d '"'
}

pin() {
  command -v docker >/dev/null || { echo "docker is required to resolve digests" >&2; exit 1; }

  local bare digest ref
  declare -A resolved=()

  # Collect unique bare (digest-stripped) refs from compose files + Dockerfiles.
  while IFS= read -r ref; do
    bare="${ref%%@sha256:*}"
    if [[ -z "${resolved[$bare]:-}" ]]; then
      digest="$(resolve_digest "$bare")"
      [[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]] || {
        echo "could not resolve digest for $bare (got: $digest)" >&2
        exit 1
      }
      resolved[$bare]="$digest"
      echo "pinned  $bare@$digest"
    fi
  done < <({ compose_refs; dockerfile_refs; } | sort -u)

  # Rewrite refs in place (append or replace the digest suffix).
  local file
  for bare in "${!resolved[@]}"; do
    digest="${resolved[$bare]}"
    for file in "${COMPOSE_FILES[@]}"; do
      REF_BARE="$bare" REF_DIGEST="$digest" perl -pi -e \
        's/\Q$ENV{REF_BARE}\E(\@sha256:[0-9a-f]{64})?/$ENV{REF_BARE}\@$ENV{REF_DIGEST}/g if /^\s+image:/' \
        "$file"
    done
    while IFS= read -r file; do
      REF_BARE="$bare" REF_DIGEST="$digest" perl -pi -e \
        's/\Q$ENV{REF_BARE}\E(\@sha256:[0-9a-f]{64})?/$ENV{REF_BARE}\@$ENV{REF_DIGEST}/g if /^FROM /' \
        "$file"
    done < <(dockerfiles)
  done

  write_manifest
  echo "wrote $MANIFEST"
}

write_manifest() {
  local compose_list dockerfile_list
  compose_list="$(compose_refs)"
  dockerfile_list="$(dockerfile_refs)"

  COMPOSE_REFS="$compose_list" \
  DOCKERFILE_REFS="$dockerfile_list" \
  FORGE_VERSION="$FORGE_VERSION" \
  MANIFEST_PATH="$MANIFEST" \
  REPO_ROOT="$REPO_ROOT" \
  python3 - <<'PYEOF'
import json
import os
import subprocess
from datetime import UTC, datetime

repo_root = os.environ["REPO_ROOT"]
version = os.environ["FORGE_VERSION"]
images: dict[str, dict[str, str]] = {}


def split_ref(ref: str) -> tuple[str, str]:
    bare, _, digest = ref.partition("@")
    return bare, digest


for ref in os.environ["COMPOSE_REFS"].split():
    bare, digest = split_ref(ref)
    images[bare] = {"digest": digest, "kind": "pulled"}
for ref in os.environ["DOCKERFILE_REFS"].split():
    bare, digest = split_ref(ref)
    images[bare] = {"digest": digest, "kind": "base"}

# Locally built first-party images: record the content-addressed image ID
# (local builds have no registry digest until pushed) + the SBOM path.
for svc in ("api", "worker", "mcp-gateway", "web"):
    tag = f"forge/{svc}:{version}"
    proc = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", tag],
        capture_output=True,
        text=True,
    )
    if proc.returncode == 0:
        images[tag] = {
            "digest": proc.stdout.strip(),
            "kind": "built",
            "sbom": f"deploy/sbom/{svc}.cdx.json",
        }

manifest = {
    "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "forge_version": version,
    "images": dict(sorted(images.items())),
}
path = os.environ["MANIFEST_PATH"]
with open(path, "w", encoding="utf-8") as fh:
    json.dump(manifest, fh, indent=2)
    fh.write("\n")
PYEOF
}

case "${1:-}" in
  --check) check ;;
  "") pin ;;
  *) echo "usage: $0 [--check]" >&2; exit 2 ;;
esac
