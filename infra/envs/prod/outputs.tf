# outputs.tf — prod environment composition.

output "primary_ipv4" {
  description = "Public IPv4 of the prod primary (stateful) control-plane server."
  value       = module.control_plane.primary_ipv4
}

output "floating_ip" {
  description = "Floating IPv4 for zero-downtime primary replacement. Always set in prod (enable_floating_ip defaults true)."
  value       = module.control_plane.floating_ip
}

output "server_ids" {
  description = "Hetzner server ids for all prod control-plane nodes (index 0 = primary/stateful)."
  value       = module.control_plane.server_ids
}

output "app_fqdn" {
  description = "Fully-qualified hostname for the prod app domain (origin_routes key \"\")."
  value       = module.cloudflare.app_fqdn
}

output "api_fqdn" {
  description = "Fully-qualified hostname for the prod api subdomain."
  value       = module.cloudflare.api_fqdn
}

output "tunnel_token" {
  description = "cloudflared connector token for the prod Tunnel (feed to `cloudflared tunnel run --token ...`). null if enable_tunnel = false."
  value       = module.cloudflare.tunnel_token
  sensitive   = true
}

output "object_store_buckets" {
  description = "Map of purpose => R2 bucket name provisioned for prod (audit_archive, marketplace, backups)."
  value       = module.cloudflare.object_store_buckets
}
