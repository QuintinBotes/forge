# main.tf — root scaffold.
#
# The infra/ root is intentionally resource-free: it fixes the toolchain
# (versions.tf), the state backend contract (backend.tf), and the shared
# input surface (variables.tf) that every environment composition reuses.
#
# Real infrastructure is COMPOSED in the environment roots:
#
#   infra/envs/dev      -> calls the provider modules with dev sizing
#   infra/envs/staging  -> ...staging sizing
#   infra/envs/prod     -> ...prod sizing
#
# Reusable building blocks live one per provider under:
#
#   infra/modules/hcloud       -> Hetzner control plane
#   infra/modules/cloudflare   -> R2 + DNS + Zero Trust tunnel
#   infra/modules/fly          -> fly.toml templating + flyctl deploy target
#
# The `baseline_tags` local below is the canonical label set every module
# merges with its own tags; envs pass it through via var.tags.

locals {
  # Labels stamped on every resource for cost attribution + cleanup.
  baseline_tags = merge(
    {
      project     = var.project
      environment = var.environment
      managed_by  = "opentofu"
    },
    var.tags,
  )

  # Consistent name prefix, e.g. "forge-prod-<resource>".
  name_prefix = "${var.project}-${var.environment}"
}
