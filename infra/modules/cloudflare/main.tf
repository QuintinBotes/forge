# main.tf — cloudflare module.
#
# Provisions the Cloudflare-side footprint for the Forge control plane:
#
#   - R2 object storage: the shared Tofu state bucket (gated, see
#     create_state_bucket) plus per-purpose buckets — audit-log archive
#     (F39), marketplace artifacts, and control-plane backups.
#   - Cloudflare Tunnel (cloudflared): lets the Hetzner control plane run
#     with NO public 80/443 listener at all — cloudflared dials OUT to
#     Cloudflare's edge, which proxies inbound traffic to it. Ingress
#     rules route by hostname to an origin service (Caddy) reachable from
#     wherever cloudflared runs.
#   - DNS: the app domain (apex) + api subdomain (or any other keys in
#     origin_routes), pointed at the tunnel (default) or directly at the
#     Hetzner control-plane IP.
#
# Provider config (an account-scoped Cloudflare API token) is supplied by
# the calling environment root and injected implicitly — same convention
# as the hetzner-control-plane module's hcloud token.

terraform {
  required_version = ">= 1.6"

  required_providers {
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 4.40"
    }

    # Generates the Tunnel secret when tunnel_secret is left null so no
    # credential ever needs to live in a tfvars file.
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

locals {
  tunnel_name = var.tunnel_name != "" ? var.tunnel_name : "${var.name_prefix}-tunnel"

  # Purpose -> bucket name. The three named purposes are explicit
  # variables (validated, self-documenting); extra_object_store_buckets
  # lets a caller graft on more without touching this module.
  object_store_buckets = merge(
    {
      audit_archive = var.audit_archive_bucket_name != "" ? var.audit_archive_bucket_name : "${var.name_prefix}-audit-archive"
      marketplace   = var.marketplace_bucket_name != "" ? var.marketplace_bucket_name : "${var.name_prefix}-marketplace"
      backups       = var.backups_bucket_name != "" ? var.backups_bucket_name : "${var.name_prefix}-backups"
    },
    var.extra_object_store_buckets,
  )

  # DNS record name for an origin_routes key: "" is the apex ("@"),
  # anything else is that subdomain of var.domain.
  record_names = { for k in keys(var.origin_routes) : k => k == "" ? "@" : k }
  record_fqdns = { for k in keys(var.origin_routes) : k => k == "" ? var.domain : "${k}.${var.domain}" }
}

# --------------------------------------------------------------------- #
# R2 — remote-state bucket (gated; see variables.tf for the sharing     #
# caveat — enable in exactly ONE bootstrap apply, not per environment). #
# --------------------------------------------------------------------- #

resource "cloudflare_r2_bucket" "state" {
  count = var.create_state_bucket ? 1 : 0

  account_id = var.account_id
  name       = var.state_bucket_name
  location   = var.r2_location
}

# --------------------------------------------------------------------- #
# R2 — object-store buckets (audit archive, marketplace, backups, +any  #
# extras).                                                               #
# --------------------------------------------------------------------- #

resource "cloudflare_r2_bucket" "object_store" {
  for_each = local.object_store_buckets

  account_id = var.account_id
  name       = each.value
  location   = var.r2_location
}

# --------------------------------------------------------------------- #
# Cloudflare Tunnel (cloudflared) — optional, default ON.                #
# --------------------------------------------------------------------- #

# Auto-generated credential secret, only when the caller didn't supply
# one. Referenced via try(...) below so this stays valid whichever branch
# of the count conditional is empty.
resource "random_id" "tunnel_secret" {
  count = var.enable_tunnel && var.tunnel_secret == null ? 1 : 0

  byte_length = 32
}

resource "cloudflare_zero_trust_tunnel_cloudflared" "this" {
  count = var.enable_tunnel ? 1 : 0

  account_id = var.account_id
  name       = local.tunnel_name
  secret     = coalesce(var.tunnel_secret, try(random_id.tunnel_secret[0].b64_std, null))

  # Ingress is managed remotely via the *_config resource below (not a
  # local config.yml on the origin box), so cloudflared only needs the
  # tunnel_token output to run.
  config_src = "cloudflare"
}

resource "cloudflare_zero_trust_tunnel_cloudflared_config" "this" {
  count = var.enable_tunnel ? 1 : 0

  account_id = var.account_id
  tunnel_id  = cloudflare_zero_trust_tunnel_cloudflared.this[0].id

  config {
    dynamic "ingress_rule" {
      for_each = var.origin_routes
      content {
        hostname = local.record_fqdns[ingress_rule.key]
        service  = ingress_rule.value
      }
    }

    # Mandatory catch-all: cloudflared requires the LAST ingress rule to
    # carry no hostname. Unmatched requests get a clean 404 from the edge
    # instead of the tunnel dropping the connection.
    ingress_rule {
      service = "http_status:404"
    }
  }
}

# --------------------------------------------------------------------- #
# DNS — app domain (apex) + api subdomain (or any other origin_routes   #
# key), pointed at the tunnel (default) or directly at the Hetzner IP.  #
# --------------------------------------------------------------------- #

resource "cloudflare_record" "tunnel" {
  for_each = var.enable_tunnel ? var.origin_routes : {}

  zone_id = var.zone_id
  name    = local.record_names[each.key]
  type    = "CNAME"
  content = cloudflare_zero_trust_tunnel_cloudflared.this[0].cname
  proxied = true # required — Tunnel routing only works through the Cloudflare proxy
  ttl     = 1    # forced "automatic" by Cloudflare whenever proxied = true
  tags    = var.dns_record_tags
  comment = "OpenTofu — ${local.record_fqdns[each.key]} -> Cloudflare Tunnel ${local.tunnel_name}"
}

resource "cloudflare_record" "ipv4" {
  for_each = !var.enable_tunnel && var.origin_ipv4 != null ? var.origin_routes : {}

  zone_id = var.zone_id
  name    = local.record_names[each.key]
  type    = "A"
  content = var.origin_ipv4
  proxied = var.dns_proxied
  ttl     = var.dns_proxied ? 1 : var.dns_ttl
  tags    = var.dns_record_tags
  comment = "OpenTofu — ${local.record_fqdns[each.key]} -> Hetzner origin ${var.origin_ipv4}"
}

resource "cloudflare_record" "ipv6" {
  for_each = !var.enable_tunnel && var.origin_ipv6 != null ? var.origin_routes : {}

  zone_id = var.zone_id
  name    = local.record_names[each.key]
  type    = "AAAA"
  content = var.origin_ipv6
  proxied = var.dns_proxied
  ttl     = var.dns_proxied ? 1 : var.dns_ttl
  tags    = var.dns_record_tags
  comment = "OpenTofu — ${local.record_fqdns[each.key]} -> Hetzner origin ${var.origin_ipv6}"
}

# --------------------------------------------------------------------- #
# Soft invariant: warn (do not fail the run) if the tunnel is disabled  #
# but no Hetzner origin IP was supplied — origin_routes would then get  #
# no DNS record at all.                                                 #
# --------------------------------------------------------------------- #

check "origin_reachable" {
  assert {
    condition     = var.enable_tunnel || var.origin_ipv4 != null || var.origin_ipv6 != null
    error_message = "enable_tunnel is false but neither origin_ipv4 nor origin_ipv6 is set — no DNS records will be created for origin_routes."
  }
}
