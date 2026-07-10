# variables.tf — inputs for the dev environment composition.
#
# Defaults in this file are the "minimal single node" dev tier: the
# smallest Hetzner box, small volumes, delete protection OFF (so
# `tofu destroy` cleanly tears dev down), no floating IP. Every default
# can still be overridden via dev.auto.tfvars for a one-off experiment.
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
# Hetzner placement + sizing (dev tier: minimal single node)             #
# --------------------------------------------------------------------- #

variable "hcloud_location" {
  description = "Hetzner Cloud location for the dev control-plane server and volumes."
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
  description = "Hetzner server type for the dev control plane. cpx21 (3 vCPU / 4 GB) comfortably runs the compose stack for a single developer/demo workload."
  type        = string
  default     = "cpx21"

  validation {
    condition     = can(regex("^(cx|cpx|ccx|cax)[0-9]+$", var.server_type))
    error_message = "server_type must be a valid Hetzner type slug, e.g. cpx21, cx22, cax11."
  }
}

variable "server_count" {
  description = "Number of control-plane servers. Dev is single-node by design; leave at 1."
  type        = number
  default     = 1

  validation {
    condition     = var.server_count >= 1 && var.server_count <= 10
    error_message = "server_count must be between 1 and 10."
  }
}

variable "postgres_volume_size" {
  description = "Size in GB of the Postgres/pgvector data volume. Small default — dev data is disposable."
  type        = number
  default     = 20

  validation {
    condition     = var.postgres_volume_size >= 10 && var.postgres_volume_size <= 10240
    error_message = "postgres_volume_size must be between 10 and 10240 GB."
  }
}

variable "minio_volume_size" {
  description = "Size in GB of the MinIO object-data volume. Small default — dev data is disposable."
  type        = number
  default     = 20

  validation {
    condition     = var.minio_volume_size >= 10 && var.minio_volume_size <= 10240
    error_message = "minio_volume_size must be between 10 and 10240 GB."
  }
}

variable "enable_delete_protection" {
  description = "Enable Hetzner delete protection on servers and volumes. Default OFF in dev so `tofu destroy` can cleanly tear the environment down."
  type        = bool
  default     = false
}

variable "enable_floating_ip" {
  description = "Provision a floating IPv4 for the dev control plane. Not needed for a disposable single-node dev box."
  type        = bool
  default     = false
}

variable "ssh_public_keys" {
  description = "Map of name => OpenSSH public key material to register and grant root login on the dev server."
  type        = map(string)

  validation {
    condition     = length(var.ssh_public_keys) >= 1
    error_message = "provide at least one SSH public key so the dev server is reachable."
  }
}

variable "admin_ssh_cidrs" {
  description = "CIDRs allowed to reach SSH (tcp/22) on the dev server's public interface. Restrict to your own IP(s) — do not leave empty/open in any shared environment."
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for c in var.admin_ssh_cidrs : can(cidrhost(c, 0))])
    error_message = "each admin_ssh_cidrs entry must be a valid CIDR, e.g. 203.0.113.4/32."
  }
}

variable "forge_repo_url" {
  description = "Git URL cloud-init clones on first boot to obtain deploy/docker-compose.yml. Empty string skips the clone (Docker is still installed)."
  type        = string
  default     = "https://github.com/QuintinBotes/forge.git"
}

variable "forge_repo_ref" {
  description = "Git ref (branch/tag/sha) cloud-init checks out. dev tracks main by default."
  type        = string
  default     = "main"
}

variable "compose_autostart" {
  description = "If true, cloud-init runs `docker compose up -d` on first boot. Default false everywhere (see hetzner-control-plane module) so secrets can be injected before the stack starts."
  type        = bool
  default     = false
}

# --------------------------------------------------------------------- #
# Cloudflare                                                             #
# --------------------------------------------------------------------- #

variable "cloudflare_account_id" {
  description = "Cloudflare account id that owns the dev R2 buckets and Tunnel."
  type        = string

  validation {
    condition     = can(regex("^[0-9a-f]{32}$", var.cloudflare_account_id))
    error_message = "cloudflare_account_id must be a 32-character lowercase hex Cloudflare account id."
  }
}

variable "cloudflare_zone_id" {
  description = "Cloudflare zone id for var.domain, where dev's DNS records are created."
  type        = string

  validation {
    condition     = can(regex("^[0-9a-f]{32}$", var.cloudflare_zone_id))
    error_message = "cloudflare_zone_id must be a 32-character lowercase hex Cloudflare zone id."
  }
}

variable "domain" {
  description = "Apex domain (or dev subdomain, e.g. dev.forge.example.com) managed in Cloudflare for this environment's DNS records and tunnel hostnames."
  type        = string

  validation {
    condition     = can(regex("^([a-z0-9]([a-z0-9-]*[a-z0-9])?\\.)+[a-z]{2,}$", var.domain))
    error_message = "domain must be a valid fully-qualified domain name, e.g. dev.forge.example.com."
  }
}

variable "enable_tunnel" {
  description = "Front the dev control plane with a Cloudflare Tunnel instead of a plain A/AAAA record. Default true — dev needs no public 80/443 listener either."
  type        = bool
  default     = true
}
