# versions.tf — provider + OpenTofu/Terraform version pinning.
#
# This file is the single source of truth for the toolchain and provider
# versions used across every environment composition under infra/envs/*.
# Modules under infra/modules/* declare only the *constraints* they need;
# this root pins the concrete versions so plans are reproducible.
#
# Tooling note: authored for OpenTofu (>= 1.6). The HCL is byte-for-byte
# compatible with Terraform, so `terraform fmt`/`validate` is used for the
# local gate. Run `tofu` in CI/apply.

terraform {
  # OpenTofu 1.6 is the first stable OSS release after the Terraform BSL
  # relicense; 1.6 also introduced native state encryption + the S3
  # backend `use_lockfile` option we rely on for R2 (see backend.tf).
  required_version = ">= 1.6"

  required_providers {
    # Hetzner Cloud — the control-plane servers, networks, firewalls,
    # load balancers and volumes.
    hcloud = {
      source  = "hetznercloud/hcloud"
      version = "~> 1.48"
    }

    # Cloudflare — R2 object storage (incl. the Tofu state bucket), DNS
    # records, and Zero Trust tunnels fronting the Hetzner control plane.
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 4.40"
    }
  }
}
