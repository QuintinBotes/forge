# outputs.tf — staging environment composition.

output "primary_ipv4" {
  description = "Public IPv4 of the staging control-plane server (primary node)."
  value       = module.control_plane.primary_ipv4
}

output "server_ids" {
  description = "Hetzner server id(s) for the staging control plane."
  value       = module.control_plane.server_ids
}

output "app_fqdn" {
  description = "Fully-qualified hostname for the staging app domain (origin_routes key \"\")."
  value       = module.cloudflare.app_fqdn
}

output "api_fqdn" {
  description = "Fully-qualified hostname for the staging api subdomain."
  value       = module.cloudflare.api_fqdn
}

output "tunnel_token" {
  description = "cloudflared connector token for the staging Tunnel (feed to `cloudflared tunnel run --token ...`). null if enable_tunnel = false."
  value       = module.cloudflare.tunnel_token
  sensitive   = true
}

output "object_store_buckets" {
  description = "Map of purpose => R2 bucket name provisioned for staging (audit_archive, marketplace, backups)."
  value       = module.cloudflare.object_store_buckets
}
