# main.tf — hetzner-control-plane module.
#
# Stands up the always-on Forge control plane on Hetzner Cloud: a private
# network + subnet, one (or more) servers running the Forge compose stack
# (API / worker / Postgres+pgvector / Redis / MinIO), persistent data
# volumes for Postgres and MinIO, a locked-down firewall, an optional
# floating IP, and a cloud-init bootstrap that installs Docker and can
# bring the stack up (see deploy/docker-compose.yml).
#
# The hcloud provider itself is configured by the calling environment
# root and injected implicitly; this module only declares the constraint.

terraform {
  required_version = ">= 1.6"

  required_providers {
    hcloud = {
      source  = "hetznercloud/hcloud"
      version = "~> 1.48"
    }
  }
}

locals {
  # Deterministic private IPs: primary at offset, then increment.
  server_private_ips = [
    for i in range(var.server_count) :
    cidrhost(var.subnet_ip_range, var.private_ip_offset + i)
  ]

  # cloud-init user_data. Rendered once; every server shares it (the
  # compose stack is single-node, so autostart is only meaningful on the
  # primary — see compose_autostart guidance in variables.tf/README).
  user_data = var.enable_bootstrap ? templatefile("${path.module}/templates/cloud-init.yaml.tftpl", {
    repo_url          = var.forge_repo_url
    repo_ref          = var.forge_repo_ref
    compose_file      = var.compose_file
    compose_autostart = var.compose_autostart
    volume_format     = var.volume_format
    volume_mounts = {
      postgres = hcloud_volume.postgres.linux_device
      minio    = hcloud_volume.minio.linux_device
    }
    extra_commands = var.extra_user_data
  }) : null
}

# --------------------------------------------------------------------- #
# SSH keys                                                              #
# --------------------------------------------------------------------- #

resource "hcloud_ssh_key" "this" {
  for_each = var.ssh_public_keys

  name       = "${var.name_prefix}-${each.key}"
  public_key = each.value
  labels     = var.labels
}

# --------------------------------------------------------------------- #
# Private network + subnet                                              #
# --------------------------------------------------------------------- #

resource "hcloud_network" "this" {
  name     = "${var.name_prefix}-net"
  ip_range = var.network_ip_range
  labels   = var.labels
}

resource "hcloud_network_subnet" "this" {
  network_id   = hcloud_network.this.id
  type         = "cloud"
  network_zone = var.network_zone
  ip_range     = var.subnet_ip_range
}

# --------------------------------------------------------------------- #
# Firewall (applies to the PUBLIC interface only — Hetzner firewalls do #
# not filter private-network traffic, so intra-cluster traffic on the   #
# private subnet is unrestricted by design).                            #
# --------------------------------------------------------------------- #

resource "hcloud_firewall" "this" {
  name   = "${var.name_prefix}-fw"
  labels = var.labels

  # SSH — admin CIDRs only.
  dynamic "rule" {
    for_each = length(var.admin_ssh_cidrs) > 0 ? [1] : []
    content {
      description = "SSH from admin CIDRs"
      direction   = "in"
      protocol    = "tcp"
      port        = "22"
      source_ips  = var.admin_ssh_cidrs
    }
  }

  # HTTP.
  rule {
    description = "HTTP ingress"
    direction   = "in"
    protocol    = "tcp"
    port        = "80"
    source_ips  = var.http_ingress_cidrs
  }

  # HTTPS.
  rule {
    description = "HTTPS ingress"
    direction   = "in"
    protocol    = "tcp"
    port        = "443"
    source_ips  = var.http_ingress_cidrs
  }

  # ICMP (optional).
  dynamic "rule" {
    for_each = var.allow_icmp ? [1] : []
    content {
      description = "ICMP (ping)"
      direction   = "in"
      protocol    = "icmp"
      source_ips  = ["0.0.0.0/0", "::/0"]
    }
  }

  # Everything else inbound is implicitly denied by Hetzner. Outbound is
  # left at the Hetzner default (allow-all) so the node can pull images.
}

# --------------------------------------------------------------------- #
# Persistent data volumes (Postgres + MinIO)                            #
# --------------------------------------------------------------------- #

resource "hcloud_volume" "postgres" {
  name              = "${var.name_prefix}-postgres-data"
  size              = var.postgres_volume_size
  location          = var.location
  format            = var.volume_format
  delete_protection = var.enable_delete_protection
  labels            = merge(var.labels, { role = "postgres-data" })

  lifecycle {
    # Persistent state — never silently recreate the data volume.
    prevent_destroy = false # set true in prod overrides; kept false so `validate`/dev teardown works
  }
}

resource "hcloud_volume" "minio" {
  name              = "${var.name_prefix}-minio-data"
  size              = var.minio_volume_size
  location          = var.location
  format            = var.volume_format
  delete_protection = var.enable_delete_protection
  labels            = merge(var.labels, { role = "minio-data" })
}

# --------------------------------------------------------------------- #
# Servers                                                               #
# --------------------------------------------------------------------- #

resource "hcloud_server" "this" {
  count = var.server_count

  name        = "${var.name_prefix}-cp-${count.index + 1}"
  server_type = var.server_type
  image       = var.server_image
  location    = var.location

  ssh_keys           = [for k in hcloud_ssh_key.this : k.id]
  firewall_ids       = [hcloud_firewall.this.id]
  user_data          = local.user_data
  delete_protection  = var.enable_delete_protection
  rebuild_protection = var.enable_delete_protection

  labels = merge(var.labels, {
    role  = "control-plane"
    index = tostring(count.index + 1)
  })

  # Attach to the private subnet with a deterministic IP.
  network {
    network_id = hcloud_network.this.id
    ip         = local.server_private_ips[count.index]
  }

  # The subnet must exist before the server joins the network.
  depends_on = [hcloud_network_subnet.this]

  lifecycle {
    # user_data only takes effect on (re)create; don't churn running
    # nodes when the bootstrap template changes — re-provision on purpose.
    ignore_changes = [user_data]
  }
}

# --------------------------------------------------------------------- #
# Volume attachments — data volumes live on the PRIMARY node (index 0), #
# which runs the stateful Postgres + MinIO containers.                  #
# --------------------------------------------------------------------- #

resource "hcloud_volume_attachment" "postgres" {
  volume_id = hcloud_volume.postgres.id
  server_id = hcloud_server.this[0].id
  automount = true
}

resource "hcloud_volume_attachment" "minio" {
  volume_id = hcloud_volume.minio.id
  server_id = hcloud_server.this[0].id
  automount = true
}

# --------------------------------------------------------------------- #
# Floating IP (optional) — stable public IP re-pointable across nodes.  #
# --------------------------------------------------------------------- #

resource "hcloud_floating_ip" "this" {
  count = var.enable_floating_ip ? 1 : 0

  type          = "ipv4"
  home_location = var.location
  name          = "${var.name_prefix}-fip"
  description   = "${var.name_prefix} control-plane floating IP"
  labels        = var.labels
}

resource "hcloud_floating_ip_assignment" "this" {
  count = var.enable_floating_ip ? 1 : 0

  floating_ip_id = hcloud_floating_ip.this[0].id
  server_id      = hcloud_server.this[0].id
}
