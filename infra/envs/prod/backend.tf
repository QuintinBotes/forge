# backend.tf — remote state on Cloudflare R2, prod environment.
#
# Partial config (see infra/backend.tf for the full rationale): the shared
# bucket/endpoint come from infra/backend.hcl (gitignored, copied from
# infra/backend.hcl.example) supplied at init time; ONLY the per-env state
# `key` is fixed here so it can never be mixed up across environments.
#
#   cd infra/envs/prod
#   tofu init \
#     -backend-config=../../backend.hcl \
#     -backend-config="key=forge/prod/terraform.tfstate"

terraform {
  backend "s3" {
    key = "forge/prod/terraform.tfstate"

    region       = "auto"
    use_lockfile = true

    # --- R2 / non-AWS S3 compatibility shims ---
    skip_credentials_validation = true
    skip_region_validation      = true
    skip_metadata_api_check     = true
    skip_requesting_account_id  = true
    use_path_style              = true
  }
}
