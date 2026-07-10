# main.tf — staging environment composition.
#
# Mid-tier: a bigger Hetzner box than dev with realistic volume sizes and
# delete protection on, fronted by a Cloudflare Tunnel. Single-node like
# dev (the compose stack itself is single-node) but sized for pre-prod
# load/perf rehearsal. See infra/README.md for the shared conventions
# this composition follows (naming, tagging, secrets).

locals {
  environment = "staging"

  baseline_tags = merge(
    {
      project     = var.project
      environment = local.environment
      managed_by  = "opentofu"
    },
    var.tags,
  )

  name_prefix = "${var.project}-${local.environment}"
}

module "control_plane" {
  source = "../../modules/hetzner-control-plane"

  name_prefix = local.name_prefix
  labels      = local.baseline_tags

  location     = var.hcloud_location
  network_zone = var.network_zone

  server_count = var.server_count
  server_type  = var.server_type

  ssh_public_keys          = var.ssh_public_keys
  enable_delete_protection = var.enable_delete_protection

  admin_ssh_cidrs = var.admin_ssh_cidrs

  postgres_volume_size = var.postgres_volume_size
  minio_volume_size    = var.minio_volume_size

  enable_floating_ip = var.enable_floating_ip

  forge_repo_url    = var.forge_repo_url
  forge_repo_ref    = var.forge_repo_ref
  compose_autostart = var.compose_autostart
}

module "cloudflare" {
  source = "../../modules/cloudflare"

  account_id  = var.cloudflare_account_id
  zone_id     = var.cloudflare_zone_id
  domain      = var.domain
  name_prefix = local.name_prefix

  # The shared Tofu state bucket is bootstrapped exactly once, out of
  # band — never from a per-env apply. See infra/README.md step 1.
  create_state_bucket = false

  enable_tunnel = var.enable_tunnel
  origin_ipv4   = var.enable_tunnel ? null : module.control_plane.primary_ipv4
}
