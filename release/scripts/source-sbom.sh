#!/usr/bin/env bash
# HARD-12 — source-tree SBOM.
#
# Produces release/sbom/forge-source.cdx.json: the CycloneDX SBOM of the SOURCE
# dependency closure (uv.lock + pnpm-lock.yaml + package manifests), distinct from
# HARD-07's per-image runtime SBOMs under deploy/sbom/. This is the auditor's
# first artifact (feeds the G-SEC-EVIDENCE gate).
#
# Requires Syft (https://github.com/anchore/syft); `brew install syft`. If Syft is
# absent the script exits non-zero with a clear reason — it never fakes an SBOM.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
out="${1:-${repo_root}/release/sbom/forge-source.cdx.json}"

if ! command -v syft >/dev/null 2>&1; then
  echo "error: syft not installed — install with 'brew install syft' (see" \
       "https://github.com/anchore/syft). The source SBOM is not generated." >&2
  exit 1
fi

mkdir -p "$(dirname "$out")"

echo "Generating source SBOM with $(syft version 2>/dev/null | head -1) ..." >&2

# Catalog from lockfiles/manifests; exclude vendored/cache trees so the SBOM is
# the dependency closure, not a scan of installed node_modules / the venv.
syft scan "dir:${repo_root}" \
  --source-name forge-source \
  --exclude './.git/**' \
  --exclude './.venv/**' \
  --exclude './node_modules/**' \
  --exclude './apps/web/node_modules/**' \
  --exclude './**/node_modules/**' \
  --exclude './.mypy_cache/**' \
  --exclude './.ruff_cache/**' \
  --exclude './.pytest_cache/**' \
  --exclude './dist/**' \
  --exclude './build/**' \
  -o "cyclonedx-json=${out}"

echo "Wrote ${out}" >&2
