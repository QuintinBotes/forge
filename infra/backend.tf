# backend.tf — remote state on Cloudflare R2 (S3-compatible).
#
# R2 speaks the S3 API, so Tofu's built-in "s3" backend works with a few
# validation shims (R2 is not real AWS, so the AWS-specific preflight
# checks must be skipped).
#
# THIS FILE IS A PARTIAL BACKEND CONFIG.
# The bucket is shared; the *key* (state path) and any per-env overrides
# are supplied at init time so each environment gets an isolated state
# object. Do NOT hard-code the key here — the env compositions under
# infra/envs/{dev,staging,prod}/ each `terraform init` with their own
# backend config, e.g.:
#
#   tofu init \
#     -backend-config=../../backend.hcl \
#     -backend-config="key=forge/prod/terraform.tfstate"
#
# ...where backend.hcl (gitignored, from backend.hcl.example) carries the
# endpoint + bucket. Credentials come from the environment, never a file:
#
#   AWS_ACCESS_KEY_ID     = <R2 access key id>
#   AWS_SECRET_ACCESS_KEY = <R2 secret access key>
#   AWS_ENDPOINT_URL_S3   = https://<accountid>.r2.cloudflarestorage.com
#
# LOCKING: R2 has no DynamoDB. We use OpenTofu's native S3 lockfile
# (`use_lockfile = true`, OpenTofu >= 1.6) which writes a
# `<key>.tflock` object for advisory locking — no separate lock table.

terraform {
  backend "s3" {
    # --- Supplied via -backend-config at init time (see header) ---
    # bucket   = "forge-tfstate"
    # key      = "forge/<env>/terraform.tfstate"
    # endpoints = { s3 = "https://<accountid>.r2.cloudflarestorage.com" }

    # R2 is region-agnostic; "auto" is the S3 API convention.
    region = "auto"

    # Native lockfile locking — writes <key>.tflock to the bucket.
    # No DynamoDB table required.
    use_lockfile = true

    # --- R2 / non-AWS S3 compatibility shims ---
    # R2 access keys are not AWS STS creds — skip AWS-only preflight.
    skip_credentials_validation = true
    skip_region_validation      = true
    skip_metadata_api_check     = true
    skip_requesting_account_id  = true

    # R2 requires path-style addressing (no <bucket>.<host> vhosts).
    use_path_style = true
  }
}
