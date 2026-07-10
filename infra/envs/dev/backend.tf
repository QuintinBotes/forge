# backend.tf — remote state on Cloudflare R2, dev environment.
#
# Partial config (see infra/backend.tf for the full rationale): the shared
# bucket/endpoint come from infra/backend.hcl (gitignored, copied from
# infra/backend.hcl.example) supplied at init time; ONLY the per-env state
# `key` is fixed here so it can never be mixed up across environments.
#
#   cd infra/envs/dev
#   tofu init \
#     -backend-config=../../backend.hcl \
#     -backend-config="key=forge/dev/terraform.tfstate"

terraform {
  backend "s3" {
    key = "forge/dev/terraform.tfstate"

    # R2 is region-agnostic; "auto" is the S3 API convention.
    region = "auto"

    # Native lockfile locking — writes <key>.tflock to the bucket. No
    # DynamoDB table required (R2 has none).
    use_lockfile = true

    # --- R2 / non-AWS S3 compatibility shims ---
    skip_credentials_validation = true
    skip_region_validation      = true
    skip_metadata_api_check     = true
    skip_requesting_account_id  = true
    use_path_style              = true
  }
}
