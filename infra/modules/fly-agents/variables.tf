# variables.tf — inputs for the fly-agents module.
#
# Every variable carries a description, a sane default, and a validation
# block, per infra/README.md conventions. There is no `fly_api_token`
# variable here on purpose: Fly auth is never a Tofu input — deploy.sh
# reads FLY_API_TOKEN directly from the environment (see README), the same
# "secrets never in tfvars" rule as the hcloud/cloudflare tokens.

variable "environments" {
  description = <<-EOT
    Per-environment Fly.io agent-runtime config. Keys must be a subset of
    "dev", "staging", "prod". Each value drives the rendered fly.<env>.toml
    (via templates/fly.toml.tftpl) AND documents the values baked into the
    committed fly.<env>.toml files, so this map is the single source of
    truth even though no Fly resources are actually managed by Tofu here.
  EOT
  type = map(object({
    app_name             = string
    image                = string
    primary_region       = string
    extra_regions        = list(string)
    vm_size              = string
    vm_memory_mb         = number
    internal_port        = number
    min_machines_running = number
    max_machines_running = number
    autostop_machines    = bool
    autostart_machines   = bool
    env                  = map(string)
  }))

  default = {
    dev = {
      app_name             = "forge-agents-dev"
      image                = "registry.fly.io/forge-agents-dev:latest"
      primary_region       = "iad"
      extra_regions        = []
      vm_size              = "shared-cpu-2x"
      vm_memory_mb         = 1024
      internal_port        = 8080
      min_machines_running = 0 # full scale-to-zero in dev
      max_machines_running = 3
      autostop_machines    = true
      autostart_machines   = true
      env = {
        FORGE_ENV = "dev"
      }
    }
    staging = {
      app_name             = "forge-agents-staging"
      image                = "registry.fly.io/forge-agents-staging:latest"
      primary_region       = "iad"
      extra_regions        = []
      vm_size              = "performance-1x"
      vm_memory_mb         = 2048
      internal_port        = 8080
      min_machines_running = 0
      max_machines_running = 5
      autostop_machines    = true
      autostart_machines   = true
      env = {
        FORGE_ENV = "staging"
      }
    }
    prod = {
      app_name             = "forge-agents-prod"
      image                = "registry.fly.io/forge-agents-prod:latest"
      primary_region       = "iad" # NA/EU per docs/hosting/cloud-cost-analysis.md region discipline
      extra_regions        = ["ord"]
      vm_size              = "performance-1x" # one Fly Machine per task; bump to performance-2x/4x if tasks are CPU-heavier
      vm_memory_mb         = 4096
      internal_port        = 8080
      min_machines_running = 1 # keep one warm per region to absorb bursts without cold start
      max_machines_running = 10
      autostop_machines    = true
      autostart_machines   = true
      env = {
        FORGE_ENV = "prod"
      }
    }
  }

  validation {
    condition     = length(setsubtract(keys(var.environments), ["dev", "staging", "prod"])) == 0
    error_message = "environments keys must be a subset of: dev, staging, prod."
  }

  validation {
    condition = alltrue([
      for cfg in values(var.environments) :
      can(regex("^[a-z][a-z0-9-]{1,61}[a-z0-9]$", cfg.app_name))
    ])
    error_message = "each environments[*].app_name must be a valid Fly app slug: lowercase alphanumeric/hyphen, 3-63 chars, start/end alphanumeric."
  }

  validation {
    condition = alltrue([
      for cfg in values(var.environments) :
      can(regex("^[a-z]{3}$", cfg.primary_region))
    ])
    error_message = "each environments[*].primary_region must be a 3-letter lowercase Fly region code (e.g. iad, ord, fra, syd)."
  }

  validation {
    condition = alltrue([
      for cfg in values(var.environments) :
      alltrue([for r in cfg.extra_regions : can(regex("^[a-z]{3}$", r))])
    ])
    error_message = "each environments[*].extra_regions entry must be a 3-letter lowercase Fly region code."
  }

  validation {
    condition = alltrue([
      for cfg in values(var.environments) :
      contains([
        "shared-cpu-1x", "shared-cpu-2x", "shared-cpu-4x", "shared-cpu-8x",
        "performance-1x", "performance-2x", "performance-4x", "performance-8x", "performance-16x",
      ], cfg.vm_size)
    ])
    error_message = "each environments[*].vm_size must be a valid Fly Machine size (shared-cpu-{1,2,4,8}x or performance-{1,2,4,8,16}x)."
  }

  validation {
    condition = alltrue([
      for cfg in values(var.environments) :
      cfg.vm_memory_mb >= 256 && cfg.vm_memory_mb <= 65536 && cfg.vm_memory_mb % 256 == 0
    ])
    error_message = "each environments[*].vm_memory_mb must be between 256 and 65536, and a multiple of 256 (Fly's memory granularity)."
  }

  validation {
    condition = alltrue([
      for cfg in values(var.environments) :
      cfg.internal_port >= 1 && cfg.internal_port <= 65535
    ])
    error_message = "each environments[*].internal_port must be a valid TCP port (1-65535)."
  }

  validation {
    condition = alltrue([
      for cfg in values(var.environments) :
      cfg.min_machines_running >= 0 && cfg.max_machines_running >= 1
    ])
    error_message = "each environments[*].min_machines_running must be >= 0 and max_machines_running must be >= 1."
  }

  validation {
    condition = alltrue([
      for cfg in values(var.environments) :
      cfg.max_machines_running >= cfg.min_machines_running
    ])
    error_message = "each environments[*].max_machines_running must be >= min_machines_running."
  }
}

variable "fly_org" {
  description = "Fly.io organization slug the agent-runtime apps belong to. \"personal\" for a single-user account; use the shared org slug for team accounts."
  type        = string
  default     = "personal"

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{0,61}[a-z0-9]$", var.fly_org))
    error_message = "fly_org must be a valid Fly organization slug: lowercase alphanumeric/hyphen."
  }
}
