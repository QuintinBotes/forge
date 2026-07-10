# variables.tf — inputs for the prod environment composition.
#
# Defaults in this file are the "HA-leaning" prod tier: a large Hetzner
# box for the primary (stateful) node plus a second node for future
# HA/worker capacity, a floating IP for zero-downtime primary
# replacement, large volumes, and delete protection ON everywhere.
#
# HONEST CEILING: the Forge compose stack itself is single-node today —
# Postgres/pgvector and MinIO run and hold their data volumes on the
# PRIMARY node only (index 0). `server_count = 2` here provisions a
# second node on the same private network (see
# infra/modules/hetzner-control-plane's server_count/README) so prod is
# ready to grow into true multi-node HA (e.g. a standby replica, or
# worker-only capacity) without a network/firewall re-provision — it is
# NOT full application-level HA out of the box. Treat the second node as
# reserved capacity, not an active standby, until the compose stack grows
# a replication/failover story.
#
# Secrets (hcloud_token, cloudflare_api_token) MUST be supplied via
# TF_VAR_* environment variables — never written to a tfvars file. See
# terraform.tfvars.example.

# --------------------------------------------------------------------- #
# Secrets — TF_VAR_* only, never a committed/gitignored tfvars file.    #
# --------------------------------------------------------------------- #

variable "hcloud_token" {
  description = "Hetzner Cloud API token (project-scoped). Supply via TF_VAR_hcloud_token — never in a tfvars file."
  type        = string
  sensitive   = true
}

variable "cloudflare_api_token" {
  description = "Cloudflare API token scoped to R2 + the target zone's DNS + Zero Trust Tunnel. Supply via TF_VAR_cloudflare_api_token — never in a tfvars file."
  type        = string
  sensitive   = true
}

# --------------------------------------------------------------------- #
# Naming / tagging                                                       #
# --------------------------------------------------------------------- #

variable "project" {
  description = "Project slug used as a name prefix and tag on every resource."
  type        = string
  default     = "forge"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,30}[a-z0-9]$", var.project))
    error_message = "project must be lowercase alphanumeric/hyphen, 3-32 chars, starting with a letter."
  }
}

variable "tags" {
  description = "Extra key/value labels merged onto the baseline tags (project/environment/managed_by) applied to every resource."
  type        = map(string)
  default     = {}
}

# --------------------------------------------------------------------- #
# Hetzner placement + sizing (prod tier: HA-leaning)                     #
# --------------------------------------------------------------------- #

variable "hcloud_location" {
  description = "Hetzner Cloud location for the prod control-plane servers and volumes."
  type        = string
  default     = "nbg1"

  validation {
    condition     = contains(["nbg1", "fsn1", "hel1", "ash", "hil", "sin"], var.hcloud_location)
    error_message = "hcloud_location must be one of: nbg1, fsn1, hel1, ash, hil, sin."
  }
}

variable "network_zone" {
  description = "Hetzner network zone matching hcloud_location (eu-central for nbg1/fsn1/hel1, us-east for ash, us-west for hil, ap-southeast for sin)."
  type        = string
  default     = "eu-central"

  validation {
    condition     = contains(["eu-central", "us-east", "us-west", "ap-southeast"], var.network_zone)
    error_message = "network_zone must be one of: eu-central, us-east, us-west, ap-southeast."
  }
}

variable "server_type" {
  description = "Hetzner server type for prod control-plane nodes. cpx51 (16 vCPU / 32 GB) gives the primary stateful node real production headroom."
  type        = string
  default     = "cpx51"

  validation {
    condition     = can(regex("^(cx|cpx|ccx|cax)[0-9]+$", var.server_type))
    error_message = "server_type must be a valid Hetzner type slug, e.g. cpx51, ccx43, cax41."
  }
}

variable "server_count" {
  description = "Number of control-plane servers. Defaults to 2 (HA-leaning: primary stateful node + one reserved-capacity node on the same private network) — see the module-level caveat at the top of this file about what is and is not HA today."
  type        = number
  default     = 2

  validation {
    condition     = var.server_count >= 1 && var.server_count <= 10
    error_message = "server_count must be between 1 and 10."
  }
}

variable "postgres_volume_size" {
  description = "Size in GB of the Postgres/pgvector data volume."
  type        = number
  default     = 100

  validation {
    condition     = var.postgres_volume_size >= 10 && var.postgres_volume_size <= 10240
    error_message = "postgres_volume_size must be between 10 and 10240 GB."
  }
}

variable "minio_volume_size" {
  description = "Size in GB of the MinIO object-data volume."
  type        = number
  default     = 200

  validation {
    condition     = var.minio_volume_size >= 10 && var.minio_volume_size <= 10240
    error_message = "minio_volume_size must be between 10 and 10240 GB."
  }
}

variable "enable_delete_protection" {
  description = "Enable Hetzner delete protection on servers and volumes. Always ON in prod — a stray destroy must not be able to wipe Postgres/MinIO data."
  type        = bool
  default     = true

  validation {
    condition     = var.enable_delete_protection == true
    error_message = "enable_delete_protection must stay true in prod."
  }
}

variable "enable_floating_ip" {
  description = "Provision a floating IPv4 for the prod control plane. ON by default — required for zero-downtime replacement of the primary node."
  type        = bool
  default     = true
}

variable "ssh_public_keys" {
  description = "Map of name => OpenSSH public key material to register and grant root login on the prod server(s)."
  type        = map(string)

  validation {
    condition     = length(var.ssh_public_keys) >= 1
    error_message = "provide at least one SSH public key so the prod servers are reachable."
  }
}

variable "admin_ssh_cidrs" {
  description = "CIDRs allowed to reach SSH (tcp/22) on prod's public interface. REQUIRED in prod — restrict to admin/bastion IPs, never 0.0.0.0/0."
  type        = list(string)

  validation {
    condition     = length(var.admin_ssh_cidrs) >= 1
    error_message = "admin_ssh_cidrs must not be empty in prod — restrict SSH to specific admin/bastion CIDRs."
  }

  validation {
    condition     = alltrue([for c in var.admin_ssh_cidrs : can(cidrhost(c, 0))])
    error_message = "each admin_ssh_cidrs entry must be a valid CIDR, e.g. 203.0.113.4/32."
  }

  validation {
    condition     = !contains(var.admin_ssh_cidrs, "0.0.0.0/0")
    error_message = "admin_ssh_cidrs must not include 0.0.0.0/0 in prod."
  }
}

variable "forge_repo_url" {
  description = "Git URL cloud-init clones on first boot to obtain deploy/docker-compose.yml. Empty string skips the clone (Docker is still installed)."
  type        = string
  default     = "https://github.com/QuintinBotes/forge.git"
}

variable "forge_repo_ref" {
  description = "Git ref (branch/tag/sha) cloud-init checks out. RECOMMENDED: override this in prod.auto.tfvars to a pinned release tag/sha before applying — the \"main\" default here is a moving branch, fine for a first plan/validate pass but not for a real prod apply."
  type        = string
  default     = "main"

  validation {
    condition     = length(var.forge_repo_ref) > 0
    error_message = "forge_repo_ref must not be empty."
  }
}

variable "compose_autostart" {
  description = "If true, cloud-init runs `docker compose up -d` on first boot. Default false everywhere so secrets can be injected before the stack starts — especially important in prod."
  type        = bool
  default     = false
}

# --------------------------------------------------------------------- #
# Cloudflare                                                             #
# --------------------------------------------------------------------- #

variable "cloudflare_account_id" {
  description = "Cloudflare account id that owns the prod R2 buckets and Tunnel."
  type        = string

  validation {
    condition     = can(regex("^[0-9a-f]{32}$", var.cloudflare_account_id))
    error_message = "cloudflare_account_id must be a 32-character lowercase hex Cloudflare account id."
  }
}

variable "cloudflare_zone_id" {
  description = "Cloudflare zone id for var.domain, where prod's DNS records are created."
  type        = string

  validation {
    condition     = can(regex("^[0-9a-f]{32}$", var.cloudflare_zone_id))
    error_message = "cloudflare_zone_id must be a 32-character lowercase hex Cloudflare zone id."
  }
}

variable "domain" {
  description = "Apex domain managed in Cloudflare for prod's DNS records and tunnel hostnames (e.g. forge.example.com)."
  type        = string

  validation {
    condition     = can(regex("^([a-z0-9]([a-z0-9-]*[a-z0-9])?\\.)+[a-z]{2,}$", var.domain))
    error_message = "domain must be a valid fully-qualified domain name, e.g. forge.example.com."
  }
}

variable "enable_tunnel" {
  description = "Front the prod control plane with a Cloudflare Tunnel instead of a plain A/AAAA record. Default true — prod runs with NO public 80/443 listener."
  type        = bool
  default     = true
}
