# providers.tf — provider configuration for the dev environment root.
#
# Tokens are never module inputs (see infra/modules/*/variables.tf); they
# are read here from TF_VAR_hcloud_token / TF_VAR_cloudflare_api_token
# (see variables.tf + terraform.tfvars.example) and injected implicitly
# into every resource the hcloud/cloudflare modules declare.

provider "hcloud" {
  token = var.hcloud_token
}

provider "cloudflare" {
  api_token = var.cloudflare_api_token
}
