#!/usr/bin/env bash
# deploy.sh — flyctl wrapper for the Forge agent-runtime Fly.io app.
#
# Fly's own supported deploy path IS `fly.toml` + `flyctl` (see the Fly TF
# provider caveat in ../../README.md), so this script — not a Tofu
# resource — is how the fly-agents module actually gets an app onto Fly.
#
# Usage:
#   FLY_API_TOKEN=... ./deploy.sh deploy   --env prod [--app forge-agents-prod] [--org my-org]
#   FLY_API_TOKEN=... ./deploy.sh scale    --env prod
#   FLY_API_TOKEN=... ./deploy.sh regions  --env prod
#   FLY_API_TOKEN=... ./deploy.sh status   --env prod
#                      ./deploy.sh render  --env prod   # no token needed; requires `tofu`
#
# Env config (min/max machine counts) comes from scaling.env, and region
# config (primary + extra regions) comes from regions.env; both mirror
# variables.tf's `environments` map defaults — see each file's header for
# the sync contract. App name defaults to "forge-agents-<env>" and can be
# overridden with --app. Fly org defaults to scaling.env's FLY_ORG (which
# mirrors variables.tf's `fly_org` variable) and can be overridden with
# --org.
#
# No secrets live in this repo: FLY_API_TOKEN must be exported by the
# caller (a shell, a CI secret store, etc.) — never hardcoded here.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"

usage() {
  cat <<'EOF'
Usage: deploy.sh <command> [--env dev|staging|prod] [--app NAME] [--org ORG]

Commands:
  deploy   Ensure the app exists in --org (flyctl apps create, best-effort
           idempotent), run `flyctl deploy` using fly.<env>.toml, then apply
           regions.env's extra regions (same as the "regions" command).
  scale    Run `flyctl scale count` with the min/max from scaling.env.
  regions  Run `flyctl regions set` with the primary/extra regions from
           regions.env (no-op if the env has no extra_regions configured).
  status   Run `flyctl status` for the app.
  render   Regenerate fly.<env>.toml from variables.tf via `tofu output`
           (no FLY_API_TOKEN required; requires the `tofu` binary).

Options:
  --env ENV   Target environment: dev, staging, or prod. Default: dev.
  --app NAME  Override the Fly app name (default: forge-agents-<env>).
  --org ORG   Override the Fly org (default: scaling.env's FLY_ORG, which
              mirrors variables.tf's `fly_org`). Only used by `deploy`'s
              app-creation step — org is meaningless for scale/regions/status
              since Fly app names are globally unique once created.
  -h, --help  Show this help.

Environment:
  FLY_API_TOKEN  Required for deploy/scale/regions/status. Never sourced
                 from a file in this repo — export it from your shell/CI
                 secrets.
EOF
}

die() {
  echo "deploy.sh: error: $*" >&2
  exit 1
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "required command '$1' not found on PATH."
}

command_name=""
env_name="dev"
app_override=""
org_override=""

if [[ $# -eq 0 ]]; then
  usage
  exit 1
fi

command_name="$1"
shift

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env)
      env_name="${2:-}"
      shift 2
      ;;
    --app)
      app_override="${2:-}"
      shift 2
      ;;
    --org)
      org_override="${2:-}"
      shift 2
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      die "unknown argument: $1 (see --help)"
      ;;
  esac
done

case "$env_name" in
  dev | staging | prod) ;;
  *) die "--env must be one of: dev, staging, prod (got '${env_name}')." ;;
esac

toml_file="${SCRIPT_DIR}/fly.${env_name}.toml"
[[ -f "$toml_file" ]] || die "no such file: ${toml_file}"

app_name="${app_override:-forge-agents-${env_name}}"

# scaling.env defines DEV_MIN_MACHINES / DEV_MAX_MACHINES / STAGING_* / PROD_* / FLY_ORG.
# shellcheck disable=SC1091 # dynamic path (SCRIPT_DIR); file is co-located, see scaling.env
source "${SCRIPT_DIR}/scaling.env"
# regions.env defines DEV_PRIMARY_REGION / DEV_EXTRA_REGIONS / STAGING_* / PROD_*.
# shellcheck disable=SC1091 # dynamic path (SCRIPT_DIR); file is co-located, see regions.env
source "${SCRIPT_DIR}/regions.env"

env_upper="$(printf '%s' "$env_name" | tr '[:lower:]' '[:upper:]')"
min_var="${env_upper}_MIN_MACHINES"
max_var="${env_upper}_MAX_MACHINES"
primary_region_var="${env_upper}_PRIMARY_REGION"
extra_regions_var="${env_upper}_EXTRA_REGIONS"
min_machines="${!min_var:?scaling.env is missing ${min_var}}"
max_machines="${!max_var:?scaling.env is missing ${max_var}}"
primary_region="${!primary_region_var:?regions.env is missing ${primary_region_var}}"
extra_regions="${!extra_regions_var:-}"
fly_org="${org_override:-${FLY_ORG:?scaling.env is missing FLY_ORG}}"

require_fly_token() {
  [[ -n "${FLY_API_TOKEN:-}" ]] || die "FLY_API_TOKEN is not set. Export it before running '${command_name}' (never commit it)."
}

# Applies environments[*].extra_regions (via regions.env) as actual Fly
# regions. fly.toml carries no region list for Machines-based apps, so this
# is the only mechanism that makes extra_regions do anything real — see
# regions.env and templates/fly.toml.tftpl's comment. No-op when this env
# has no extra regions configured.
apply_regions() {
  if [[ -z "$extra_regions" ]]; then
    echo "==> no extra_regions configured for '${env_name}' (regions.env); app stays single-region (${primary_region})"
    return 0
  fi
  # Intentionally unquoted: extra_regions is a space-separated list of
  # region codes from regions.env, meant to expand to multiple arguments.
  # shellcheck disable=SC2086
  echo "==> flyctl regions set ${primary_region} ${extra_regions} --app ${app_name}"
  # shellcheck disable=SC2086
  flyctl regions set "$primary_region" $extra_regions --app "$app_name" --yes
}

case "$command_name" in
  deploy)
    require_cmd flyctl
    require_fly_token
    # Best-effort idempotent: `flyctl apps create` fails if the app already
    # exists, which is expected on every deploy after the first, so a
    # nonzero exit here is not treated as fatal. This is what actually wires
    # --org / FLY_ORG (var.fly_org) to a real flyctl invocation — org only
    # matters at app-creation time.
    echo "==> flyctl apps create ${app_name} --org ${fly_org} (no-op if it already exists)"
    flyctl apps create "$app_name" --org "$fly_org" || echo "==> apps create failed/skipped (app likely already exists) — continuing"
    echo "==> flyctl deploy --config ${toml_file} --app ${app_name}"
    flyctl deploy --config "$toml_file" --app "$app_name" --strategy immediate
    apply_regions
    ;;

  scale)
    require_cmd flyctl
    require_fly_token
    echo "==> flyctl scale count ${max_machines} --min-per-region ${min_machines} --app ${app_name}"
    flyctl scale count "$max_machines" --min-per-region "$min_machines" --app "$app_name" --yes
    ;;

  regions)
    require_cmd flyctl
    require_fly_token
    apply_regions
    ;;

  status)
    require_cmd flyctl
    require_fly_token
    flyctl status --app "$app_name"
    ;;

  render)
    require_cmd tofu
    require_cmd jq
    module_dir="${SCRIPT_DIR}"
    echo "==> tofu -chdir=${module_dir} init -backend=false"
    tofu -chdir="$module_dir" init -backend=false -input=false >/dev/null
    echo "==> regenerating ${toml_file} from variables.tf via tofu output"
    # `tofu output` only accepts a plain output name (no indexing
    # expressions), so pull the whole rendered_fly_toml map as JSON and
    # extract this environment's string with jq. `-j` (not `-r`) so jq
    # doesn't append its own trailing newline on top of the templatefile
    # output's own trailing newline.
    tofu -chdir="$module_dir" apply -auto-approve -input=false >/dev/null
    tofu -chdir="$module_dir" output -json rendered_fly_toml | jq -j --arg env "$env_name" '.[$env]' >"$toml_file"
    echo "==> wrote ${toml_file}"
    ;;

  -h | --help)
    usage
    ;;

  *)
    die "unknown command: ${command_name} (see --help)"
    ;;
esac
