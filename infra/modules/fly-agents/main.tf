# main.tf — fly-agents module.
#
# Models the Forge agent-execution layer (the Firecracker-microVM,
# scale-to-zero "one Fly Machine per task" tier) as data, NOT as Tofu-managed
# cloud resources.
#
# WHY NO fly PROVIDER: the community Fly Terraform provider is thin, lags
# the platform, and cannot express Fly Machines' per-second/scale-to-zero
# model well. Fly's own supported path is `fly.toml` + `flyctl`, so that is
# what this module produces:
#
#   - Static, human-editable fly.<env>.toml files (../fly.dev.toml etc.)
#     that flyctl reads directly — no Tofu involvement needed for a normal
#     deploy.
#   - variables.tf below as the single, validated, documented source of
#     truth for the values baked into those files (app name, region, VM
#     size, min/max machines) — the same "descriptive variable + sane
#     default + validation" convention every other module in infra/ uses.
#   - templates/fly.toml.tftpl + the locals/outputs below let an operator
#     *regenerate* the committed fly.<env>.toml from variables.tf via
#     `tofu output -json rendered_fly_toml | jq` so the static files and
#     the documented config never drift silently (see README "Keeping
#     fly.<env>.toml in sync").
#
# There is deliberately no `terraform { required_providers { fly = ... } }`
# block: this module calls no provider at all, only builtin functions
# (templatefile), so `tofu`/`terraform validate` never needs network
# access or cloud credentials to succeed here.

terraform {
  required_version = ">= 1.6"
}

locals {
  # Rendered fly.toml content per environment, keyed the same as
  # var.environments. Regenerate the committed fly.<env>.toml with:
  #   tofu output -json rendered_fly_toml | jq -j '."prod"' > ../fly.prod.toml
  rendered_fly_toml = {
    for env, cfg in var.environments : env => templatefile(
      "${path.module}/templates/fly.toml.tftpl",
      {
        app_name             = cfg.app_name
        image                = cfg.image
        primary_region       = cfg.primary_region
        extra_regions        = cfg.extra_regions
        vm_size              = cfg.vm_size
        vm_memory_mb         = cfg.vm_memory_mb
        internal_port        = cfg.internal_port
        min_machines_running = cfg.min_machines_running
        autostop_machines    = cfg.autostop_machines
        autostart_machines   = cfg.autostart_machines
        env                  = cfg.env
      }
    )
  }

  # `flyctl deploy` / `flyctl scale count` invocations implied by each
  # environment's config — surfaced as outputs purely for documentation /
  # copy-paste; deploy.sh hardcodes the equivalent logic so it works with
  # zero Tofu involvement (see deploy.sh + scaling.env).
  deploy_commands = {
    for env, cfg in var.environments : env =>
    "flyctl deploy --config fly.${env}.toml --app ${cfg.app_name} --strategy immediate"
  }

  scale_commands = {
    for env, cfg in var.environments : env =>
    "flyctl scale count ${cfg.max_machines_running} --min-per-region ${cfg.min_machines_running} --app ${cfg.app_name} --yes"
  }

  # var.fly_org actually drives app *creation*: a Fly app is created once,
  # under one org, and its name is globally unique thereafter — so org only
  # matters at `flyctl apps create` time, never for deploy/scale/status.
  # deploy.sh's `deploy` command runs the equivalent of this (best-effort,
  # idempotent if the app already exists) before every `flyctl deploy`.
  app_create_commands = {
    for env, cfg in var.environments : env =>
    "flyctl apps create ${cfg.app_name} --org ${var.fly_org}"
  }

  # fly.toml has no top-level "regions" list for Machines-based apps — Fly
  # tracks an app's region set out-of-band via `flyctl regions`, not via
  # manifest content. So extra_regions is realized as a `flyctl regions set`
  # call (primary + extras), not as template output. Empty string means "no
  # extra regions configured for this env" (app stays single-region).
  regions_commands = {
    for env, cfg in var.environments : env =>
    length(cfg.extra_regions) > 0
    ? "flyctl regions set ${cfg.primary_region} ${join(" ", cfg.extra_regions)} --app ${cfg.app_name} --yes"
    : ""
  }
}
