#!/usr/bin/env bash
# HARD-07 — generate a CycloneDX SBOM for each first-party image via syft.
#
# Usage:
#   deploy/scripts/sbom.sh                 # all 4 built images
#   deploy/scripts/sbom.sh api web         # a subset
#
# Output: ${SBOM_DIR:-deploy/sbom}/<image>.cdx.json — committed release copies
# feed the HARD-09 security-evidence pack (CVE scans, audits).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SBOM_DIR="${SBOM_DIR:-$REPO_ROOT/deploy/sbom}"
FORGE_VERSION="${FORGE_VERSION:-0.1.0}"

command -v syft >/dev/null || {
  echo "syft is required (https://github.com/anchore/syft): brew install syft" >&2
  exit 1
}

services=("$@")
if [[ "${#services[@]}" -eq 0 ]]; then
  services=(api worker mcp-gateway web)
fi

mkdir -p "$SBOM_DIR"
for svc in "${services[@]}"; do
  image="forge/${svc}:${FORGE_VERSION}"
  out="$SBOM_DIR/${svc}.cdx.json"
  echo "syft $image -> $out"
  syft "$image" -o "cyclonedx-json=$out" -q
done
echo "SBOMs written to $SBOM_DIR"
