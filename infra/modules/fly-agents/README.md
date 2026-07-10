# Module: `fly-agents`

Models the Forge **agent-execution layer** — Firecracker microVMs,
scale-to-zero, "one Fly Machine per task" — on **Fly.io**.

> **CEILING (same as the rest of `infra/`):** this is written, `fmt`-clean,
> `validate`-clean configuration plus a deploy runbook. There is no Fly.io
> account or `FLY_API_TOKEN` attached to this repo, so nothing here has
> ever been deployed. `deploy.sh` and the `flyctl` commands below are the
> path from this blueprint to a real, running app.

## Why this module has no Tofu-managed Fly resources

The community `fly` Terraform provider is thin and lags the platform — it
cannot express Fly Machines' per-second billing / scale-to-zero model well.
Fly's own supported path is **`fly.toml` + `flyctl`**, so that is what this
module produces instead of provider resources:

| Concern | Where |
| --- | --- |
| The actual Fly app manifest `flyctl` reads | `fly.dev.toml` / `fly.staging.toml` / `fly.prod.toml` (static, committed, human-editable) |
| Single source of truth for app name / region / VM size / min·max machines | `variables.tf` (`environments` map — Tofu variable, validated, defaulted) |
| Regeneration of the `.toml` files from `variables.tf` | `templates/fly.toml.tftpl` + `main.tf` locals + `outputs.tf` |
| Deploy / scale / regions / status automation | `deploy.sh` (+ `Makefile` wrapper) |
| Non-secret min/max machine counts + Fly org consumed by `deploy.sh` | `scaling.env` |
| Non-secret primary/extra region config consumed by `deploy.sh` | `regions.env` |

There is **no `required_providers` block** for Fly in `main.tf` — this
module calls no provider at all, only the builtin `templatefile()`
function, so `init`/`validate` never need network access or a Fly token.
That also means this module, unlike `hetzner-control-plane` or
`cloudflare`, has nothing stopping you from actually running `tofu apply`
locally — there's just nothing for it to create (see "render" below).

## Layout

```
fly-agents/
├── README.md              # this file
├── main.tf                 # no provider; locals render fly.toml content via templatefile()
├── variables.tf             # `environments` map: app_name, region(s), vm_size, vm_memory_mb,
│                             #   min/max machines, autostop/autostart, env vars — per dev/staging/prod
├── outputs.tf               # fly_app_names, rendered_fly_toml, deploy_commands,
│                             #   scale_commands, app_create_commands, regions_commands
├── templates/
│   └── fly.toml.tftpl       # the fly.toml template rendered from variables.tf
├── fly.dev.toml              # committed, ready-to-use Fly manifests —
├── fly.staging.toml          #   flyctl reads these directly, no Tofu required
├── fly.prod.toml              #
├── scaling.env               # DEV_MIN_MACHINES/DEV_MAX_MACHINES/STAGING_*/PROD_*/FLY_ORG for deploy.sh
├── regions.env               # DEV_PRIMARY_REGION/DEV_EXTRA_REGIONS/STAGING_*/PROD_* for deploy.sh
├── deploy.sh                 # flyctl wrapper: deploy | scale | regions | status | render
└── Makefile                  # `make deploy ENV=prod` etc., thin wrapper around deploy.sh
```

## Usage — day to day (no Tofu needed)

```bash
export FLY_API_TOKEN=...          # from `flyctl auth token`, or a CI secret

cd infra/modules/fly-agents
./deploy.sh deploy   --env prod                  # apps create --org <org> (best-effort) +
                                                  #   flyctl deploy --config fly.prod.toml +
                                                  #   apply extra_regions (see regions.env)
./deploy.sh scale    --env prod                  # flyctl scale count <max> --min-per-region <min>
./deploy.sh regions  --env prod                  # flyctl regions set <primary> <extras...>
./deploy.sh status   --env prod                  # flyctl status

# or via the Makefile wrapper:
make deploy ENV=prod ORG=my-fly-org
make scale  ENV=prod
make regions ENV=prod
```

`--app` overrides the Fly app name if you don't want the
`forge-agents-<env>` default (e.g. a personal Fly org needs a globally
unique name). `--org` overrides the Fly org that owns the app (default:
`scaling.env`'s `FLY_ORG`, which mirrors `variables.tf`'s `fly_org`
variable) — see "Wiring `fly_org` and `extra_regions`" below.

## Wiring `fly_org` and `extra_regions`

Two of `variables.tf`'s inputs (`fly_org`, `environments[*].extra_regions`)
don't affect the rendered `fly.<env>.toml` content, because Fly itself
doesn't put them in the manifest:

- **`fly_org`** only matters at app-creation time — a Fly app belongs to
  one org for its whole (globally-unique-named) lifetime, so there's no
  "org" field in `fly.toml` or in `flyctl deploy`/`scale`/`status`. It's
  wired into `deploy.sh`'s `deploy` command as a `flyctl apps create
  <app> --org <org>` step (best-effort idempotent: fails harmlessly with
  `set -e` suppressed if the app already exists), and surfaced for
  scripting via `outputs.tf`'s `app_create_commands`.
- **`extra_regions`** can't be expressed as a `fly.toml` field either —
  Fly's Machines platform tracks an app's region set out-of-band via
  `flyctl regions add/set/remove`, not manifest content. `fly.<env>.toml`
  only gets an explanatory comment (see `templates/fly.toml.tftpl`); the
  actual region set is applied by `deploy.sh`'s `regions` command (also run
  automatically at the end of `deploy`), which reads `regions.env`'s
  `<ENV>_PRIMARY_REGION` / `<ENV>_EXTRA_REGIONS` and runs `flyctl regions
  set <primary> <extras...> --app <app>`. Surfaced for scripting via
  `outputs.tf`'s `regions_commands` (empty string for envs with no extra
  regions).

Both `regions.env` and `scaling.env`'s `FLY_ORG` line are plain-shell
mirrors of `variables.tf` (same manual sync contract already used for
min/max machine counts — see the CI follow-up below).

## Keeping `fly.<env>.toml` in sync with `variables.tf`

The committed `fly.<env>.toml` files are what `flyctl` actually reads day
to day — editing them directly works fine for a quick tweak. `variables.tf`
is the documented, validated version of the same values (app name, region,
VM size, memory, min/max machines). If you change `variables.tf`,
regenerate the `.toml` files so they don't drift:

```bash
./deploy.sh render --env prod   # no FLY_API_TOKEN needed; requires the `tofu` and `jq` binaries
# equivalent, by hand:
tofu -chdir=infra/modules/fly-agents init -backend=false
tofu -chdir=infra/modules/fly-agents apply -auto-approve   # nothing to create; only computes outputs
tofu -chdir=infra/modules/fly-agents output -json rendered_fly_toml | jq -j '."prod"' > fly.prod.toml
```

`terraform`/`tofu output` only accepts a plain output name — it cannot
evaluate an indexing expression like `rendered_fly_toml["prod"]` directly.
Fetch the whole map with `-json` and pull out the environment key with
`jq` instead (as above, and as `deploy.sh render` does internally).

**Follow-up (not wired here):** add a CI check on PRs touching
`infra/modules/fly-agents/**` that runs `render` for each env and fails the
diff if `fly.<env>.toml` doesn't match — this repo's token cannot add
`.github/workflows/`, so it's documented here instead (see the root
[`infra/README.md`](../../README.md) "CI wiring" section).

## `min`/`max` machines: two different mechanisms

Fly doesn't express "max machines" inside `fly.toml` itself — only the
*minimum* warm count (`http_service.min_machines_running`, used for
scale-to-zero / burst-from-zero). The *maximum* is a fleet size set via
`flyctl scale count`. So:

- `fly.<env>.toml`'s `min_machines_running` -> baked into the manifest,
  used by `flyctl deploy`.
- `max_machines_running` (from `variables.tf` / `scaling.env`) -> applied
  separately via `flyctl scale count <max> --min-per-region <min>`
  (`deploy.sh scale`).

Keep `scaling.env` and `variables.tf`'s `environments` map in agreement —
`scaling.env`'s header documents this as a manual sync contract for now.

## Defaults per environment

| Env | App name | Region(s) | VM size | Memory | min / max machines |
| --- | --- | --- | --- | --- | --- |
| dev | `forge-agents-dev` | `iad` | `shared-cpu-2x` | 1024 MB | 0 / 3 (full scale-to-zero) |
| staging | `forge-agents-staging` | `iad` | `performance-1x` | 2048 MB | 0 / 5 |
| prod | `forge-agents-prod` | `iad` + `ord` | `performance-1x` | 4096 MB | 1 / 10 (one warm machine per region) |

`prod` keeps `min_machines_running = 1` per region so a burst doesn't pay
a cold-start Firecracker boot on the first request; `dev`/`staging` are
full scale-to-zero to keep cost near $0 when idle. "1 per region" for prod
is realized by `flyctl regions set` (see "Wiring `fly_org` and
`extra_regions`" above) putting `iad` + `ord` in the app's region set, then
`http_service.min_machines_running = 1` in `fly.prod.toml` applying to
each region flyctl's `--min-per-region` targets.

## Inputs / outputs

See [`variables.tf`](./variables.tf) for the full `environments` object
schema (with validation on app-name format, Fly region-code format,
allowed VM sizes, memory granularity, and `min <= max`) and `fly_org`
(Fly org slug, wired via `deploy.sh`'s `flyctl apps create --org` step —
see above). See [`outputs.tf`](./outputs.tf) for `fly_app_names`,
`rendered_fly_toml`, `deploy_commands`, `scale_commands`,
`app_create_commands`, `regions_commands`.

## Local gate

```bash
terraform fmt -check -recursive infra
terraform -chdir=infra/modules/fly-agents init -backend=false
terraform -chdir=infra/modules/fly-agents validate
shellcheck infra/modules/fly-agents/deploy.sh
```

`validate` succeeds fully offline (no provider, no cloud calls) — this is
the one module in `infra/` where `init`/`validate`/`apply` all work with
zero external dependencies, since there's no cloud state to manage.
`deploy.sh render` additionally requires `jq` (to pull one environment's
string out of the `rendered_fly_toml` output map — `tofu output` cannot
evaluate an indexing expression itself).
