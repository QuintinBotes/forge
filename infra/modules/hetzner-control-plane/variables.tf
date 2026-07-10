# variables.tf — inputs for the hetzner-control-plane module.
#
# Every variable carries a description, a sane default where one is safe,
# and a validation block where the input space is constrained. Secrets
# (the hcloud API token) are NOT module inputs — the provider is
# configured by the calling environment root and passed in implicitly.

# --------------------------------------------------------------------- #
# Naming & labelling                                                    #
# --------------------------------------------------------------------- #

variable "name_prefix" {
  description = "Resource name prefix, e.g. \"forge-prod\". Supplied by the env composition (local.name_prefix)."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,40}[a-z0-9]$", var.name_prefix))
    error_message = "name_prefix must be lowercase alphanumeric/hyphen, start with a letter, end alphanumeric."
  }
}

variable "labels" {
  description = "Labels merged onto every resource (Hetzner labels: keys/values <=63 chars). Pass local.baseline_tags."
  type        = map(string)
  default     = {}
}

# --------------------------------------------------------------------- #
# Placement                                                             #
# --------------------------------------------------------------------- #

variable "location" {
  description = "Hetzner Cloud location for servers, volumes and the floating IP (nbg1/fsn1/hel1 = eu-central, ash = us-east, hil = us-west, sin = ap-southeast)."
  type        = string
  default     = "nbg1"

  validation {
    condition     = contains(["nbg1", "fsn1", "hel1", "ash", "hil", "sin"], var.location)
    error_message = "location must be one of: nbg1, fsn1, hel1, ash, hil, sin."
  }
}

variable "network_zone" {
  description = "Hetzner network zone the private subnet lives in. Must match the location's zone (eu-central, us-east, us-west, ap-southeast)."
  type        = string
  default     = "eu-central"

  validation {
    condition     = contains(["eu-central", "us-east", "us-west", "ap-southeast"], var.network_zone)
    error_message = "network_zone must be one of: eu-central, us-east, us-west, ap-southeast."
  }
}

# --------------------------------------------------------------------- #
# Private network                                                       #
# --------------------------------------------------------------------- #

variable "network_ip_range" {
  description = "CIDR of the private network the control plane runs on. Must be a private (RFC1918) range."
  type        = string
  default     = "10.128.0.0/16"

  validation {
    condition     = can(cidrhost(var.network_ip_range, 0))
    error_message = "network_ip_range must be a valid CIDR block, e.g. 10.128.0.0/16."
  }
}

variable "subnet_ip_range" {
  description = "CIDR of the subnet carved out of network_ip_range that servers attach to."
  type        = string
  default     = "10.128.1.0/24"

  validation {
    condition     = can(cidrhost(var.subnet_ip_range, 0))
    error_message = "subnet_ip_range must be a valid CIDR block within network_ip_range, e.g. 10.128.1.0/24."
  }
}

variable "private_ip_offset" {
  description = "Host offset within subnet_ip_range for the first server's private IP; subsequent servers increment from here. Avoids the gateway (offset 1)."
  type        = number
  default     = 10

  validation {
    condition     = var.private_ip_offset >= 2 && var.private_ip_offset <= 250
    error_message = "private_ip_offset must be between 2 and 250 (leave room for the network gateway and server_count)."
  }
}

# --------------------------------------------------------------------- #
# Servers                                                               #
# --------------------------------------------------------------------- #

variable "server_count" {
  description = "Number of control-plane servers. Defaults to 1 (the single-node Forge compose stack); >1 stands up additional nodes on the same private network."
  type        = number
  default     = 1

  validation {
    condition     = var.server_count >= 1 && var.server_count <= 10
    error_message = "server_count must be between 1 and 10."
  }
}

variable "server_type" {
  description = "Hetzner server type / size (e.g. cpx31, cpx41, cx42, ccx23). cpx41 (8 vCPU / 16 GB) is a sane prod default for the full compose stack."
  type        = string
  default     = "cpx41"

  validation {
    condition     = can(regex("^(cx|cpx|ccx|cax)[0-9]+$", var.server_type))
    error_message = "server_type must be a valid Hetzner type slug, e.g. cx42, cpx41, ccx23, cax41."
  }
}

variable "server_image" {
  description = "Base OS image for the servers. cloud-init installs Docker on top, so a plain LTS image is expected."
  type        = string
  default     = "ubuntu-24.04"
}

variable "ssh_public_keys" {
  description = "Map of name => OpenSSH public key material to register in the project and grant root login. At least one is required so the servers are reachable."
  type        = map(string)

  validation {
    condition     = length(var.ssh_public_keys) >= 1
    error_message = "provide at least one SSH public key so the control plane is reachable."
  }

  validation {
    condition     = alltrue([for k in values(var.ssh_public_keys) : can(regex("^(ssh-ed25519|ssh-rsa|ecdsa-sha2-) ", k))])
    error_message = "each ssh_public_keys value must be OpenSSH public-key material (ssh-ed25519 / ssh-rsa / ecdsa-sha2-*)."
  }
}

variable "enable_delete_protection" {
  description = "Enable Hetzner delete protection on servers and data volumes. Recommended true for prod so a stray destroy cannot wipe Postgres/MinIO data."
  type        = bool
  default     = true
}

# --------------------------------------------------------------------- #
# Firewall                                                              #
# --------------------------------------------------------------------- #

variable "admin_ssh_cidrs" {
  description = "CIDRs allowed to reach SSH (tcp/22) on the public interface. Restrict to admin/bastion IPs — do NOT leave 0.0.0.0/0 in prod."
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for c in var.admin_ssh_cidrs : can(cidrhost(c, 0))])
    error_message = "each admin_ssh_cidrs entry must be a valid CIDR, e.g. 203.0.113.4/32."
  }
}

variable "http_ingress_cidrs" {
  description = "CIDRs allowed to reach HTTP/HTTPS (tcp/80,443). Default is the internet; set to the Cloudflare tunnel/edge ranges to force all ingress through the tunnel."
  type        = list(string)
  default     = ["0.0.0.0/0", "::/0"]

  validation {
    condition     = alltrue([for c in var.http_ingress_cidrs : can(cidrhost(c, 0))])
    error_message = "each http_ingress_cidrs entry must be a valid CIDR."
  }
}

variable "allow_icmp" {
  description = "Allow inbound ICMP (ping) on the public interface. Useful for reachability checks; harmless to leave on."
  type        = bool
  default     = true
}

# --------------------------------------------------------------------- #
# Volumes (persistent data)                                             #
# --------------------------------------------------------------------- #

variable "postgres_volume_size" {
  description = "Size in GB of the persistent volume for Postgres/pgvector data. Hetzner volumes are min 10 GB and grow-only."
  type        = number
  default     = 50

  validation {
    condition     = var.postgres_volume_size >= 10 && var.postgres_volume_size <= 10240
    error_message = "postgres_volume_size must be between 10 and 10240 GB."
  }
}

variable "minio_volume_size" {
  description = "Size in GB of the persistent volume for MinIO object data."
  type        = number
  default     = 100

  validation {
    condition     = var.minio_volume_size >= 10 && var.minio_volume_size <= 10240
    error_message = "minio_volume_size must be between 10 and 10240 GB."
  }
}

variable "volume_format" {
  description = "Filesystem Hetzner formats the data volumes with on first attach."
  type        = string
  default     = "ext4"

  validation {
    condition     = contains(["ext4", "xfs"], var.volume_format)
    error_message = "volume_format must be ext4 or xfs."
  }
}

# --------------------------------------------------------------------- #
# Floating IP (optional)                                                #
# --------------------------------------------------------------------- #

variable "enable_floating_ip" {
  description = "Provision a floating IPv4 and assign it to the primary server — a stable public IP that can be re-pointed across servers for zero-downtime replacement."
  type        = bool
  default     = false
}

# --------------------------------------------------------------------- #
# cloud-init / bootstrap                                                #
# --------------------------------------------------------------------- #

variable "enable_bootstrap" {
  description = "Render the cloud-init user_data that installs Docker and (optionally) brings up the Forge compose stack. Disable to provision bare servers you configure out-of-band."
  type        = bool
  default     = true
}

variable "forge_repo_url" {
  description = "Git URL cloud-init clones to obtain deploy/docker-compose.yml. Empty string skips the clone/up step (Docker is still installed)."
  type        = string
  default     = ""
}

variable "forge_repo_ref" {
  description = "Git ref (branch/tag/sha) cloud-init checks out from forge_repo_url."
  type        = string
  default     = "main"

  validation {
    condition     = length(var.forge_repo_ref) > 0
    error_message = "forge_repo_ref must not be empty."
  }
}

variable "compose_file" {
  description = "Path (within the cloned repo) to the compose file cloud-init brings up."
  type        = string
  default     = "deploy/docker-compose.yml"
}

variable "compose_autostart" {
  description = "If true and forge_repo_url is set, cloud-init runs `docker compose up -d` on first boot. If false it only clones + installs, leaving `up` to the operator (safer for first-run secret injection)."
  type        = bool
  default     = false
}

variable "extra_user_data" {
  description = "Optional raw cloud-init runcmd lines appended verbatim to the generated bootstrap (e.g. node_exporter install). One shell command per list element."
  type        = list(string)
  default     = []
}
