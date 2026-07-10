# outputs.tf — hetzner-control-plane module.
#
# Surface the identifiers downstream compositions need: server IPs (public
# + private), the private network id, and the persistent volume ids.

output "server_ids" {
  description = "IDs of the control-plane servers, ordered (index 0 = primary)."
  value       = [for s in hcloud_server.this : s.id]
}

output "server_names" {
  description = "Names of the control-plane servers, ordered."
  value       = [for s in hcloud_server.this : s.name]
}

output "server_ipv4" {
  description = "Public IPv4 addresses of the control-plane servers, ordered."
  value       = [for s in hcloud_server.this : s.ipv4_address]
}

output "server_ipv6" {
  description = "Public IPv6 addresses of the control-plane servers, ordered."
  value       = [for s in hcloud_server.this : s.ipv6_address]
}

output "server_private_ips" {
  description = "Private-network IPv4 addresses of the control-plane servers, ordered (index 0 = primary)."
  value       = local.server_private_ips
}

output "primary_server_id" {
  description = "ID of the primary server (index 0) that hosts the stateful Postgres + MinIO containers and their volumes."
  value       = hcloud_server.this[0].id
}

output "primary_ipv4" {
  description = "Public IPv4 of the primary server — the DNS/tunnel origin for the control plane."
  value       = hcloud_server.this[0].ipv4_address
}

output "network_id" {
  description = "ID of the private network the control plane runs on. Feed to any peer resources that must join the same network."
  value       = hcloud_network.this.id
}

output "network_ip_range" {
  description = "CIDR of the private network."
  value       = hcloud_network.this.ip_range
}

output "subnet_ip_range" {
  description = "CIDR of the subnet servers are attached to."
  value       = hcloud_network_subnet.this.ip_range
}

output "firewall_id" {
  description = "ID of the control-plane firewall (attach additional servers to reuse the same ruleset)."
  value       = hcloud_firewall.this.id
}

output "ssh_key_ids" {
  description = "IDs of the SSH keys registered for the control plane, keyed by input name."
  value       = { for k, v in hcloud_ssh_key.this : k => v.id }
}

output "volume_ids" {
  description = "IDs of the persistent data volumes, keyed by role (postgres, minio)."
  value = {
    postgres = hcloud_volume.postgres.id
    minio    = hcloud_volume.minio.id
  }
}

output "postgres_volume_id" {
  description = "ID of the persistent Postgres/pgvector data volume."
  value       = hcloud_volume.postgres.id
}

output "minio_volume_id" {
  description = "ID of the persistent MinIO object-data volume."
  value       = hcloud_volume.minio.id
}

output "volume_devices" {
  description = "Stable /dev/disk/by-id device paths for the data volumes (as mounted by cloud-init), keyed by role."
  value = {
    postgres = hcloud_volume.postgres.linux_device
    minio    = hcloud_volume.minio.linux_device
  }
}

output "floating_ip" {
  description = "The floating IPv4 address if enable_floating_ip = true, else null. Point DNS/tunnel origin here for zero-downtime node replacement."
  value       = var.enable_floating_ip ? hcloud_floating_ip.this[0].ip_address : null
}
