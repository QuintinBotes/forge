# outputs.tf — cloudflare module.
#
# Surface the identifiers downstream compositions (and the Fly.io
# fly.toml templating step) need: bucket names/ids, the Tunnel id/token,
# and the DNS fqdns actually created.

# --------------------------------------------------------------------- #
# R2 buckets                                                            #
# --------------------------------------------------------------------- #

output "state_bucket_name" {
  description = "Name of the shared Tofu remote-state bucket (created only if create_state_bucket = true; otherwise this is just the configured name for reference against an out-of-band-created bucket)."
  value       = var.state_bucket_name
}

output "state_bucket_id" {
  description = "Id of the state R2 bucket, if this composition created it (create_state_bucket = true). null otherwise."
  value       = try(cloudflare_r2_bucket.state[0].id, null)
}

output "object_store_buckets" {
  description = "Map of purpose (audit_archive, marketplace, backups, + any extra_object_store_buckets keys) => bucket name."
  value       = local.object_store_buckets
}

output "object_store_bucket_ids" {
  description = "Map of purpose => R2 bucket id, keyed the same as object_store_buckets."
  value       = { for k, b in cloudflare_r2_bucket.object_store : k => b.id }
}

output "audit_archive_bucket_name" {
  description = "R2 bucket name used for the compliance audit-log archive (F39)."
  value       = local.object_store_buckets["audit_archive"]
}

output "marketplace_bucket_name" {
  description = "R2 bucket name used for marketplace package artifacts."
  value       = local.object_store_buckets["marketplace"]
}

output "backups_bucket_name" {
  description = "R2 bucket name used for control-plane backups."
  value       = local.object_store_buckets["backups"]
}

# --------------------------------------------------------------------- #
# Cloudflare Tunnel                                                      #
# --------------------------------------------------------------------- #

output "tunnel_id" {
  description = "Id of the Cloudflare Tunnel, if enable_tunnel = true. null otherwise."
  value       = try(cloudflare_zero_trust_tunnel_cloudflared.this[0].id, null)
}

output "tunnel_name" {
  description = "Name of the Cloudflare Tunnel (as created, or as it would be if enable_tunnel were true)."
  value       = local.tunnel_name
}

output "tunnel_cname" {
  description = "The `<tunnel-id>.cfargotunnel.com` CNAME target Cloudflare routes to this tunnel. null when enable_tunnel = false."
  value       = try(cloudflare_zero_trust_tunnel_cloudflared.this[0].cname, null)
}

output "tunnel_token" {
  description = "Connector token cloudflared authenticates with to run the tunnel (feed to the `cloudflared tunnel run --token ...` invocation, e.g. via a docker-compose env var / secret). null when enable_tunnel = false."
  value       = try(cloudflare_zero_trust_tunnel_cloudflared.this[0].tunnel_token, null)
  sensitive   = true
}

# --------------------------------------------------------------------- #
# DNS                                                                    #
# --------------------------------------------------------------------- #

output "dns_fqdns" {
  description = "Map of origin_routes key => fully-qualified hostname actually created (e.g. { \"\" = \"forge.example.com\", \"api\" = \"api.forge.example.com\" })."
  value       = local.record_fqdns
}

output "dns_record_ids" {
  description = "Map of origin_routes key => Cloudflare DNS record id, whichever record type (tunnel CNAME, A, or AAAA) was actually created for that key."
  value = {
    for k in keys(var.origin_routes) :
    k => try(cloudflare_record.tunnel[k].id, cloudflare_record.ipv4[k].id, cloudflare_record.ipv6[k].id, null)
  }
}

output "app_fqdn" {
  description = "Fully-qualified hostname for the apex/app domain (origin_routes key \"\")."
  value       = try(local.record_fqdns[""], var.domain)
}

output "api_fqdn" {
  description = "Fully-qualified hostname for the \"api\" origin_routes key, if present. null otherwise."
  value       = try(local.record_fqdns["api"], null)
}
