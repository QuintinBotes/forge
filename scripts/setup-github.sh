#!/usr/bin/env bash
#
# scripts/setup-github.sh — apply Forge's baseline GitHub repository hardening.
#
# Idempotent by construction: every call sets a *desired state* (a full-replace
# PUT or a PATCH), so re-running the script simply reconverges to the same
# configuration and never errors on "already configured". Safe to run repeatedly.
#
# What it configures on the target repo (default: service-hive/forge):
#
#   * Branch protection on `main`:
#       - require a pull request before merging
#       - require 1 approving review + dismiss stale approvals
#       - require conversation resolution before merging
#       - require the CI + security status checks to pass AND be up to date
#         (strict = "branch must be up to date")
#       - require linear history
#       - block force-pushes and branch deletion
#       - enforce all of the above for administrators
#   * Secret scanning + push protection
#   * Dependabot vulnerability alerts + automated security updates
#   * Squash-merge as the ONLY merge method; auto-delete head branches on merge
#
# Prerequisites: the GitHub CLI (`gh`) authenticated as a repository admin.
# See docs/contributing/github-setup.md. Run this ONCE, after the first PR has
# merged (the branch must exist and CI must have reported its check names).
#
# Usage:
#   scripts/setup-github.sh [<owner>/<repo>] [<branch>]
#   REPO=service-hive/forge BRANCH=main scripts/setup-github.sh
#
set -euo pipefail

REPO="${1:-${REPO:-service-hive/forge}}"
BRANCH="${2:-${BRANCH:-main}}"

# Required status checks. These MUST match the job `name:` values in
# .github/workflows/ci.yml — GitHub keys required checks on the job name, not the
# job id. Update this list if a CI job is renamed / added / removed.
#
# Intentionally excludes the jobs in .github/workflows/helm-chart.yml: those are
# path-filtered (deploy/helm/**), so requiring them would leave every non-Helm PR
# stuck on a check that never runs.
REQUIRED_CHECKS=(
  "python (lint + types + tests)"
  "web (lint + build)"
  "security (sast + deps + secrets + sbom + matrix)"
  "secrets-config (fail-closed preflight)"
  "compose (config validation)"
  "build (images + sbom + smoke)"
)

# --- Pretty logging (colour only on a TTY) -------------------------------- #
if [ -t 1 ]; then
  C_BLUE=$'\033[1;34m'; C_GREEN=$'\033[1;32m'; C_YELLOW=$'\033[1;33m'
  C_RED=$'\033[1;31m'; C_RESET=$'\033[0m'
else
  C_BLUE=''; C_GREEN=''; C_YELLOW=''; C_RED=''; C_RESET=''
fi
log()  { printf '%s==>%s %s\n' "${C_BLUE}" "${C_RESET}" "$*"; }
ok()   { printf '%s  ✓%s %s\n' "${C_GREEN}" "${C_RESET}" "$*"; }
warn() { printf '%s  ! %s%s\n' "${C_YELLOW}" "$*" "${C_RESET}" >&2; }
die()  { printf '%sERROR:%s %s\n' "${C_RED}" "${C_RESET}" "$*" >&2; exit 1; }

# --- Preflight ------------------------------------------------------------ #
preflight() {
  command -v gh >/dev/null 2>&1 \
    || die "GitHub CLI (gh) not found. Install it: https://cli.github.com"
  gh auth status >/dev/null 2>&1 \
    || die "Not authenticated. Run: gh auth login   (needs admin on ${REPO})"

  log "Target repository: ${REPO}  (protected branch: ${BRANCH})"

  local is_admin
  is_admin="$(gh api "repos/${REPO}" --jq '.permissions.admin // false' 2>/dev/null || echo false)"
  [ "${is_admin}" = "true" ] \
    || die "Your token lacks admin on ${REPO}. Re-auth as an admin: gh auth login --scopes 'repo'"
}

# --- Branch protection ---------------------------------------------------- #
apply_branch_protection() {
  log "Applying branch protection on '${BRANCH}'…"

  # Build the JSON array of {"context": "..."} objects from REQUIRED_CHECKS.
  local checks_json="" esc ctx
  for ctx in "${REQUIRED_CHECKS[@]}"; do
    esc=${ctx//\\/\\\\}   # escape backslashes
    esc=${esc//\"/\\\"}   # escape double quotes
    [ -n "${checks_json}" ] && checks_json+=","
    checks_json+="{\"context\":\"${esc}\"}"
  done

  local body
  read -r -d '' body <<JSON || true
{
  "required_status_checks": {
    "strict": true,
    "checks": [${checks_json}]
  },
  "enforce_admins": true,
  "required_pull_request_reviews": {
    "dismiss_stale_reviews": true,
    "require_code_owner_reviews": false,
    "required_approving_review_count": 1
  },
  "restrictions": null,
  "required_linear_history": true,
  "allow_force_pushes": false,
  "allow_deletions": false,
  "required_conversation_resolution": true,
  "block_creations": false,
  "lock_branch": false,
  "allow_fork_syncing": true
}
JSON

  printf '%s' "${body}" \
    | gh api --method PUT "repos/${REPO}/branches/${BRANCH}/protection" \
        -H "Accept: application/vnd.github+json" --input - >/dev/null
  ok "Branch protection applied (${#REQUIRED_CHECKS[@]} required checks, strict; 1 review + dismiss-stale; conversation resolution; linear history; no force-push/deletion; admins enforced)."
}

# --- Merge settings ------------------------------------------------------- #
apply_merge_settings() {
  log "Setting squash-only merge + auto-delete head branches…"
  gh api --method PATCH "repos/${REPO}" \
    -H "Accept: application/vnd.github+json" \
    -F allow_squash_merge=true \
    -F allow_merge_commit=false \
    -F allow_rebase_merge=false \
    -F delete_branch_on_merge=true >/dev/null
  ok "Squash is the only merge method; head branches auto-delete on merge."
}

# --- Secret scanning + push protection ------------------------------------ #
# Soft-fail: free on public repos, but private repos need GitHub Advanced
# Security. We warn (rather than abort) so the rest of the hardening still lands.
apply_secret_scanning() {
  log "Enabling secret scanning + push protection…"
  local body err
  read -r -d '' body <<'JSON' || true
{
  "security_and_analysis": {
    "secret_scanning": { "status": "enabled" },
    "secret_scanning_push_protection": { "status": "enabled" }
  }
}
JSON
  if err="$(printf '%s' "${body}" \
      | gh api --method PATCH "repos/${REPO}" \
          -H "Accept: application/vnd.github+json" --input - 2>&1 >/dev/null)"; then
    ok "Secret scanning + push protection enabled."
    return 0
  fi
  warn "Could not enable secret scanning: ${err}"
  warn "Public repos get this for free; private repos require GitHub Advanced Security."
  return 1
}

# --- Dependabot ----------------------------------------------------------- #
apply_dependabot() {
  log "Enabling Dependabot alerts + automated security updates…"
  gh api --method PUT "repos/${REPO}/vulnerability-alerts" \
    -H "Accept: application/vnd.github+json" >/dev/null
  ok "Dependabot vulnerability alerts enabled."
  gh api --method PUT "repos/${REPO}/automated-security-fixes" \
    -H "Accept: application/vnd.github+json" >/dev/null
  ok "Dependabot automated security updates enabled."
}

# --- Verify (read-back; also proves idempotency) -------------------------- #
verify() {
  log "Verifying applied configuration…"
  gh api "repos/${REPO}" --jq '
    "  merge:      squash=\(.allow_squash_merge)  merge_commit=\(.allow_merge_commit)  rebase=\(.allow_rebase_merge)  delete_branch_on_merge=\(.delete_branch_on_merge)"' \
    || warn "Could not read repo settings for verification."
  gh api "repos/${REPO}/branches/${BRANCH}/protection" --jq '
    "  protection: strict=\(.required_status_checks.strict)  checks=\(.required_status_checks.checks | length)  reviews=\(.required_pull_request_reviews.required_approving_review_count)  linear=\(.required_linear_history.enabled)  force_push=\(.allow_force_pushes.enabled)  deletions=\(.allow_deletions.enabled)  admins=\(.enforce_admins.enabled)  conv_resolution=\(.required_conversation_resolution.enabled)"' \
    || warn "Branch protection not readable yet — does '${BRANCH}' exist with at least one commit?"
}

main() {
  preflight
  apply_branch_protection
  apply_merge_settings
  apply_dependabot
  local rc=0
  apply_secret_scanning || rc=1
  verify
  if [ "${rc}" -ne 0 ]; then
    warn "Completed with warnings (see above)."
  else
    ok "GitHub hardening applied to ${REPO}."
  fi
  return "${rc}"
}

main "$@"
