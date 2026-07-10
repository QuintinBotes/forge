# variables.tf — inputs for the cloudflare module.
#
# Every variable carries a description, a sane default where one is safe,
# and a validation block where the input space is constrained. The
# Cloudflare API token itself is NOT a module input — the provider is
# configured by the calling environment root and passed in implicitly
# (same convention as the hetzner-control-plane module's hcloud token).

# --------------------------------------------------------------------- #
# Account / zone / naming                                               #
# --------------------------------------------------------------------- #

variable "account_id" {
  description = "Cloudflare account id that owns the R2 buckets and the Tunnel."
  type        = string

  validation {
    condition     = can(regex("^[0-9a-f]{32}$", var.account_id))
    error_message = "account_id must be a 32-character lowercase hex Cloudflare account id."
  }
}

variable "zone_id" {
  description = "Cloudflare zone id for var.domain, where the DNS records are created."
  type        = string

  validation {
    condition     = can(regex("^[0-9a-f]{32}$", var.zone_id))
    error_message = "zone_id must be a 32-character lowercase hex Cloudflare zone id."
  }
}

variable "domain" {
  description = "Apex domain managed in this Cloudflare zone (e.g. forge.example.com). DNS records are created at this apex and at subdomains keyed off var.origin_routes."
  type        = string

  validation {
    condition     = can(regex("^([a-z0-9]([a-z0-9-]*[a-z0-9])?\\.)+[a-z]{2,}$", var.domain))
    error_message = "domain must be a valid fully-qualified domain name, e.g. forge.example.com."
  }
}

variable "name_prefix" {
  description = "Resource name prefix, e.g. \"forge-prod\". Supplied by the env composition (local.name_prefix). Used to derive default bucket/tunnel names when the explicit *_bucket_name / tunnel_name variables are left empty."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,40}[a-z0-9]$", var.name_prefix))
    error_message = "name_prefix must be lowercase alphanumeric/hyphen, start with a letter, end alphanumeric."
  }
}

# --------------------------------------------------------------------- #
# R2 — remote-state bucket                                              #
# --------------------------------------------------------------------- #
#
# The Tofu state bucket is SHARED across every environment (only the
# state object `key` differs per env — see infra/backend.tf). Creating it
# is therefore NOT part of the normal per-env apply: enable
# create_state_bucket in exactly one bootstrap apply (see infra/README.md
# "Apply runbook" step 1), and leave it false in infra/envs/{dev,staging,
# prod} to avoid every environment racing to create the same bucket.

variable "create_state_bucket" {
  description = "Create the shared Tofu remote-state R2 bucket. Enable in exactly ONE bootstrap apply, never per-environment — the bucket is shared across all envs (see infra/README.md step 1). Defaults false so a normal env apply does not attempt to (re)create it."
  type        = bool
  default     = false
}

variable "state_bucket_name" {
  description = "Name of the shared Tofu remote-state R2 bucket. Must match the `bucket` value supplied via backend.hcl at `tofu init` time."
  type        = string
  default     = "forge-tfstate"

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$", var.state_bucket_name))
    error_message = "state_bucket_name must be 3-63 chars, lowercase alphanumeric/hyphen, starting and ending alphanumeric."
  }
}

# --------------------------------------------------------------------- #
# R2 — object-store buckets                                             #
# --------------------------------------------------------------------- #
#
# Three first-class purposes, each an explicit variable so the intent is
# self-documenting. Leave a *_bucket_name variable as "" (the default) to
# derive "${name_prefix}-<purpose>"; set it explicitly to pin a stable
# name across renames of name_prefix.

variable "audit_archive_bucket_name" {
  description = "R2 bucket name for the compliance audit-log archive (F39 quarterly NDJSON export + hash-chain verification artifacts). Empty string derives \"$${name_prefix}-audit-archive\"."
  type        = string
  default     = ""

  validation {
    condition     = var.audit_archive_bucket_name == "" || can(regex("^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$", var.audit_archive_bucket_name))
    error_message = "audit_archive_bucket_name must be empty (derive the default) or 3-63 chars, lowercase alphanumeric/hyphen, start/end alphanumeric."
  }
}

variable "marketplace_bucket_name" {
  description = "R2 bucket name for marketplace package artifacts (packages/marketplace-sdk manifests, signed bundles, published skill/tool packages). Empty string derives \"$${name_prefix}-marketplace\"."
  type        = string
  default     = ""

  validation {
    condition     = var.marketplace_bucket_name == "" || can(regex("^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$", var.marketplace_bucket_name))
    error_message = "marketplace_bucket_name must be empty (derive the default) or 3-63 chars, lowercase alphanumeric/hyphen, start/end alphanumeric."
  }
}

variable "backups_bucket_name" {
  description = "R2 bucket name for control-plane backups (Postgres/pgvector dumps, MinIO/object-store snapshots). Empty string derives \"$${name_prefix}-backups\"."
  type        = string
  default     = ""

  validation {
    condition     = var.backups_bucket_name == "" || can(regex("^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$", var.backups_bucket_name))
    error_message = "backups_bucket_name must be empty (derive the default) or 3-63 chars, lowercase alphanumeric/hyphen, start/end alphanumeric."
  }
}

variable "extra_object_store_buckets" {
  description = "Additional purpose => bucket-name pairs merged alongside audit_archive/marketplace/backups. Room for future migrations (e.g. the self-hosted MinIO forge-checks / forge-traces / forge-postmortems buckets from F08/F10/F39 moving to R2) without changing this module's interface."
  type        = map(string)
  default     = {}

  validation {
    condition     = alltrue([for n in values(var.extra_object_store_buckets) : can(regex("^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$", n))])
    error_message = "each extra_object_store_buckets value must be 3-63 chars, lowercase alphanumeric/hyphen, start/end alphanumeric."
  }
}

variable "r2_location" {
  description = "R2 location hint applied to every bucket created by this module. null lets Cloudflare place the bucket automatically. Set \"WEUR\" or \"EEUR\" to keep data close to a Hetzner EU control plane (nbg1/fsn1/hel1)."
  type        = string
  default     = null

  validation {
    condition     = var.r2_location == null || contains(["WNAM", "ENAM", "WEUR", "EEUR", "APAC", "OC"], var.r2_location)
    error_message = "r2_location must be null or one of: WNAM, ENAM, WEUR, EEUR, APAC, OC."
  }
}

# --------------------------------------------------------------------- #
# DNS + Cloudflare Tunnel                                                #
# --------------------------------------------------------------------- #

variable "enable_tunnel" {
  description = "Front the control plane with a Cloudflare Tunnel (cloudflared) instead of exposing it via a plain A/AAAA record. Recommended: cloudflared dials OUT to Cloudflare's edge, so the Hetzner box needs no public 80/443 listener at all. When false, origin_ipv4/origin_ipv6 are used instead."
  type        = bool
  default     = true
}

variable "origin_routes" {
  description = "Map of DNS-record key => origin service address. Key \"\" is the apex/app domain (var.domain); any other key (e.g. \"api\") is that subdomain (\"<key>.$${domain}\"). The value is only used as the Tunnel ingress `service` target when enable_tunnel = true (point it at wherever cloudflared can reach Caddy, e.g. \"http://localhost:80\" if cloudflared runs on the same box, or a Docker-network service name); it is ignored when enable_tunnel = false. Every key present here gets a DNS record either way."
  type        = map(string)
  default = {
    ""    = "http://localhost:80"
    "api" = "http://localhost:80"
  }

  validation {
    condition     = alltrue([for k in keys(var.origin_routes) : k == "" || can(regex("^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$", k))])
    error_message = "each origin_routes key must be \"\" (apex) or a valid DNS label (lowercase alphanumeric/hyphen)."
  }
}

variable "origin_ipv4" {
  description = "Hetzner control-plane public IPv4 (e.g. module.control_plane.primary_ipv4 or .floating_ip). Used to create A records when enable_tunnel = false. null skips A records."
  type        = string
  default     = null

  validation {
    condition     = var.origin_ipv4 == null || can(regex("^([0-9]{1,3}\\.){3}[0-9]{1,3}$", var.origin_ipv4))
    error_message = "origin_ipv4 must be null or a dotted-quad IPv4 address."
  }
}

variable "origin_ipv6" {
  description = "Hetzner control-plane public IPv6. Used to create AAAA records when enable_tunnel = false. null skips AAAA records."
  type        = string
  default     = null

  validation {
    condition     = var.origin_ipv6 == null || can(regex("^[0-9a-fA-F:]+$", var.origin_ipv6))
    error_message = "origin_ipv6 must be null or a valid-looking IPv6 address."
  }
}

variable "dns_proxied" {
  description = "Proxy A/AAAA records through Cloudflare (orange-cloud) when enable_tunnel = false. Tunnel CNAME records are always proxied regardless of this setting (required for Tunnel routing to work)."
  type        = bool
  default     = true
}

variable "dns_ttl" {
  description = "TTL (seconds) for A/AAAA records when dns_proxied = false. Ignored (Cloudflare forces \"automatic\") whenever a record is proxied."
  type        = number
  default     = 300

  validation {
    condition     = var.dns_ttl >= 60 && var.dns_ttl <= 86400
    error_message = "dns_ttl must be between 60 and 86400 seconds."
  }
}

variable "dns_record_tags" {
  description = "Cloudflare DNS record tags (cost/ownership metadata visible in the dashboard) applied to every record this module creates."
  type        = list(string)
  default     = []
}

variable "tunnel_name" {
  description = "Cloudflare Tunnel name. Empty string derives \"$${name_prefix}-tunnel\"."
  type        = string
  default     = ""

  validation {
    condition     = var.tunnel_name == "" || (length(var.tunnel_name) >= 1 && length(var.tunnel_name) <= 64)
    error_message = "tunnel_name must be empty (derive the default) or 1-64 characters."
  }
}

variable "tunnel_secret" {
  description = "32+ bytes, base64-encoded, used as the Tunnel's credential secret. null generates one via `random_id` (recommended — avoids committing a secret to any tfvars file). Provide explicitly only if you need to reuse an existing tunnel's credentials file."
  type        = string
  default     = null
  sensitive   = true

  validation {
    condition     = var.tunnel_secret == null || length(var.tunnel_secret) >= 44
    error_message = "tunnel_secret must be null (auto-generate) or a base64 string encoding at least 32 bytes (>= 44 characters)."
  }
}
