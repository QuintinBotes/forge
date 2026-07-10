# outputs.tf — fly-agents module.
#
# Nothing here reflects live cloud state (there is no provider, so nothing
# is ever "applied" to Fly). These outputs exist so the documented,
# validated config in variables.tf can be consumed programmatically —
# either to regenerate the committed fly.<env>.toml files, or by tooling
# (deploy.sh, CI) that wants the exact flyctl invocation for an env.

output "fly_app_names" {
  description = "Fly app name per environment, keyed by environment (dev/staging/prod)."
  value       = { for env, cfg in var.environments : env => cfg.app_name }
}

output "rendered_fly_toml" {
  description = "Rendered fly.toml content per environment. Regenerate the committed file with: tofu output -json rendered_fly_toml | jq -j '.\"<env>\"' > ../fly.<env>.toml"
  value       = local.rendered_fly_toml
}

output "deploy_commands" {
  description = "The flyctl deploy invocation for each environment, for copy-paste or scripting (deploy.sh implements the same logic without requiring Tofu)."
  value       = local.deploy_commands
}

output "scale_commands" {
  description = "The flyctl scale count invocation for each environment (sets min/max warm machines), for copy-paste or scripting."
  value       = local.scale_commands
}

output "app_create_commands" {
  description = "The flyctl apps create invocation for each environment, wiring var.fly_org to the org that owns the app. Idempotent-best-effort in deploy.sh (fails harmlessly if the app already exists)."
  value       = local.app_create_commands
}

output "regions_commands" {
  description = "The flyctl regions set invocation for each environment that realizes environments[*].extra_regions as actual Fly regions (empty string when extra_regions is empty — fly.toml itself carries no region list for Machines-based apps)."
  value       = local.regions_commands
}
