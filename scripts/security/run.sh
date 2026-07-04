#!/usr/bin/env bash
# HARD-09 — local security audit roll-up (single source of truth for `make
# security` and the CI `security` job). Runs SAST, dependency audit, secret
# scan, SBOM generation, waiver validation, and the enforcement-matrix suite,
# printing a green/red line per step and failing (non-zero) on any high/critical
# that is not waived. shellcheck-clean.
set -uo pipefail

cd "$(dirname "$0")/../.." || exit 2
ROOT="$(pwd)"
EVIDENCE_DIR="docs/security/evidence"
mkdir -p "${EVIDENCE_DIR}"

RC=0
declare -a RESULTS=()

record() {
  # record <name> <status 0|1> [note]
  local name="$1" status="$2" note="${3:-}"
  if [ "${status}" -eq 0 ]; then
    RESULTS+=("PASS  ${name} ${note}")
  else
    RESULTS+=("FAIL  ${name} ${note}")
    RC=1
  fi
}

step() { printf '\n=== %s ===\n' "$1"; }

have() { command -v "$1" >/dev/null 2>&1; }

# --- Waiver validation (fails closed on expiry) ----------------------------- #
step "waivers"
if uv run python scripts/security/check_waivers.py security/waivers.yaml; then
  record "waivers" 0
else
  record "waivers" 1 "(expired or malformed — see security/waivers.yaml)"
fi

# --- SAST: bandit (fail on high/critical) ----------------------------------- #
step "bandit (SAST, high/critical)"
if uv run bandit -c pyproject.toml -r packages apps \
    --severity-level high --format txt -o "${EVIDENCE_DIR}/bandit.txt" -q; then
  record "bandit" 0
else
  record "bandit" 1 "(high/critical findings — see ${EVIDENCE_DIR}/bandit.txt)"
fi

# --- SAST: custom semgrep rules --------------------------------------------- #
step "semgrep (custom Forge rules)"
if have semgrep || [ -x "${ROOT}/.venv/bin/semgrep" ]; then
  SEMGREP_BIN="semgrep"; [ -x "${ROOT}/.venv/bin/semgrep" ] && SEMGREP_BIN="${ROOT}/.venv/bin/semgrep"
  if "${SEMGREP_BIN}" --config .semgrep/forge.yml --error --metrics=off \
      --disable-version-check --sarif -o "${EVIDENCE_DIR}/semgrep.sarif" . >/dev/null 2>&1; then
    record "semgrep" 0
  else
    record "semgrep" 1 "(rule violations — see ${EVIDENCE_DIR}/semgrep.sarif)"
  fi
else
  record "semgrep" 0 "(SKIPPED: semgrep not installed)"
fi

# --- Dependency CVE audit --------------------------------------------------- #
step "pip-audit (dependency CVEs)"
if uv export --frozen --format requirements-txt --no-emit-workspace 2>/dev/null \
    | uv run pip-audit -r /dev/stdin --strict; then
  record "pip-audit" 0
else
  record "pip-audit" 1 "(known CVE — waive dated in security/waivers.yaml or upgrade)"
fi

# --- Secret scan ------------------------------------------------------------ #
step "gitleaks (secret scan)"
if have gitleaks; then
  if gitleaks detect --source . --config .gitleaks.toml --no-banner --redact \
      --report-format json --report-path "${EVIDENCE_DIR}/gitleaks.json"; then
    record "gitleaks" 0
  else
    record "gitleaks" 1 "(unallowlisted secret — see ${EVIDENCE_DIR}/gitleaks.json)"
  fi
else
  record "gitleaks" 0 "(SKIPPED: gitleaks not installed)"
fi

# --- SBOM ------------------------------------------------------------------- #
step "SBOM (CycloneDX)"
if uv run cyclonedx-py environment -o "${EVIDENCE_DIR}/sbom.cdx.json" >/dev/null 2>&1; then
  record "sbom" 0
else
  record "sbom" 1 "(generation failed)"
fi

# --- Evidence rendering ----------------------------------------------------- #
step "matrix evidence"
if uv run python scripts/security/gen_matrix_evidence.py >/dev/null; then
  record "matrix-evidence" 0
else
  record "matrix-evidence" 1
fi

# --- Enforcement-matrix regression suite ------------------------------------ #
step "enforcement matrix (pytest -m security)"
if uv run pytest -m security -q; then
  record "enforcement-matrix" 0
else
  record "enforcement-matrix" 1
fi

# --- Roll-up ---------------------------------------------------------------- #
printf '\n================ security roll-up ================\n'
for line in "${RESULTS[@]}"; do
  printf '  %s\n' "${line}"
done
printf '=================================================\n'
if [ "${RC}" -ne 0 ]; then
  printf 'SECURITY GATE: FAILED — triage above; waive (dated) in security/waivers.yaml only for accepted risk.\n' >&2
else
  printf 'SECURITY GATE: PASSED\n'
fi
exit "${RC}"
