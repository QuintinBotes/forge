# variables.tf — shared input variables.
#
# These are the cross-cutting inputs every environment composition passes
# down into the provider modules. Environment-specific values live in the
# gitignored *.auto.tfvars (see terraform.tfvars.example); real secrets
# come from TF_VAR_* env vars, never a committed file.

variable "environment" {
  description = "Deployment environment. Drives resource naming, sizing, and the remote-state key."
  type        = string

  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be one of: dev, staging, prod."
  }
}

variable "project" {
  description = "Project slug used as a name prefix and tag on every resource."
  type        = string
  default     = "forge"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,30}[a-z0-9]$", var.project))
    error_message = "project must be lowercase alphanumeric/hyphen, 3-32 chars, starting with a letter."
  }
}

variable "region" {
  description = "Primary Hetzner Cloud location for control-plane resources (e.g. nbg1, fsn1, hel1, ash, hil)."
  type        = string
  default     = "nbg1"

  validation {
    condition     = contains(["nbg1", "fsn1", "hel1", "ash", "hil", "sin"], var.region)
    error_message = "region must be a valid Hetzner Cloud location: nbg1, fsn1, hel1, ash, hil, sin."
  }
}

variable "domain" {
  description = "Apex domain managed in Cloudflare for DNS records and tunnel hostnames (e.g. example.com)."
  type        = string

  validation {
    condition     = can(regex("^([a-z0-9]([a-z0-9-]*[a-z0-9])?\\.)+[a-z]{2,}$", var.domain))
    error_message = "domain must be a valid fully-qualified domain name, e.g. forge.example.com."
  }
}

variable "tags" {
  description = "Extra key/value labels merged onto the baseline tags applied to every resource."
  type        = map(string)
  default     = {}
}
