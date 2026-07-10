# Forge — Infrastructure as Code (OpenTofu)

Production infrastructure for Forge, authored as **OpenTofu** (Apache-2.0 /
MPL — the open-source successor to Terraform after its BSL relicense). The
HCL is byte-for-byte Terraform-compatible; `terraform` v1.15.5 is used for
the local `fmt`/`validate` gate, and `tofu` is used for `init`/`plan`/`apply`.

> **⚠️ CEILING — read this first.** Everything under `infra/` is
> **written, `fmt`-clean, and `validate`-clean code plus a documented
> apply-runbook**. It has **NOT** been `tofu apply`'d. There are no cloud
> accounts or paid infrastructure attached to this repo, so no state exists
> and no resources have been created. Treat this as a reviewed, ready-to-run
> blueprint — not a live deployment. The "Apply runbook" section below is the
> path from here to real infrastructure.

## Providers & platforms

| Concern                     | Platform        | How it's modeled                                    |
| --------------------------- | --------------- | --------------------------------------------------- |
| Control plane (servers, LB) | Hetzner Cloud   | `hetznercloud/hcloud` provider                      |
| Object storage (R2), DNS, tunnel | Cloudflare | `cloudflare/cloudflare` provider                    |
| App runtime (agents)        | Fly.io          | `fly.toml` templates + `flyctl` via a make target   |
| Remote state                | Cloudflare R2   | Tofu `s3` backend (S3-compatible) + native lockfile |

**Why Fly is not a Tofu resource:** the community Fly Terraform provider is
thin and lags the platform. Fly apps are instead modeled as versioned
`fly.toml` templates deployed by `flyctl` from a `make`/script target — the
supported, first-class path. See `infra/modules/fly/` (separate slice).

## Layout

```
infra/
├── README.md                  # this file
├── versions.tf                # required_version + pinned provider versions
├── backend.tf                 # R2 (S3-compatible) remote-state backend — partial config
├── variables.tf               # shared inputs: environment, project, region, domain, tags
├── main.tf                    # root scaffold: baseline_tags + name_prefix locals (no resources)
├── .gitignore                 # state, tfvars, .terraform/, plans, crash logs
├── terraform.tfvars.example   # non-secret var template (copy → <env>.auto.tfvars)
├── backend.hcl.example        # partial backend config template (copy → backend.hcl)
│
├── modules/                   # one reusable module per provider (separate slices)
│   ├── hcloud/                #   main.tf · variables.tf · outputs.tf · README.md
│   ├── cloudflare/
│   └── fly/
│
└── envs/                      # environment compositions that call the modules
    ├── dev/
    ├── staging/
    └── prod/
```

## How it composes

- **`infra/` root** is intentionally resource-free. It fixes the toolchain
  (`versions.tf`), the state-backend contract (`backend.tf`), and the shared
  input surface (`variables.tf`). The `main.tf` locals (`baseline_tags`,
  `name_prefix`) are the naming/labelling conventions every module reuses.
- **`modules/<provider>/`** — one module per provider, each with the standard
  `main.tf` / `variables.tf` / `outputs.tf` / `README.md` quartet. Modules
  declare only the version *constraints* they need; the root pins concrete
  versions.
- **`envs/{dev,staging,prod}/`** — the actual Tofu roots you `init`/`plan`/
  `apply`. Each calls the modules with environment-appropriate sizing and
  supplies its own remote-state `key`.

## Conventions

- **Provider versions** are pinned in `versions.tf` (`~>` on
  `hetznercloud/hcloud` and `cloudflare/cloudflare`); `required_version >= 1.6`.
- **Naming:** `${project}-${environment}-<resource>` via `local.name_prefix`.
- **Tagging:** every resource merges `local.baseline_tags`
  (`project`, `environment`, `managed_by=opentofu`) with per-resource labels.
- **Variables** carry descriptive `description`s, sane `default`s where safe,
  and `validation` blocks (see `variables.tf`).
- **Secrets** are supplied only via `TF_VAR_*` env vars or a **gitignored**
  `*.auto.tfvars`. Only `*.example` templates are committed — **never** real
  tokens. `.gitignore` enforces this.

## Remote state (Cloudflare R2)

R2 is S3-compatible, so the built-in `s3` backend is used with non-AWS shims
(`skip_credentials_validation`, `skip_region_validation`,
`skip_metadata_api_check`, `use_path_style`). There is no DynamoDB on R2, so
locking uses OpenTofu's **native lockfile** (`use_lockfile = true`, requires
OpenTofu >= 1.6), which writes a `<key>.tflock` object to the bucket.

`backend.tf` is a **partial** config: the shared `bucket`/`endpoints` come
from `backend.hcl` (copied from `backend.hcl.example`) and the per-env `key`
is passed on the `init` line, so each environment gets an isolated state
object:

| Environment | State key                          |
| ----------- | ---------------------------------- |
| dev         | `forge/dev/terraform.tfstate`      |
| staging     | `forge/staging/terraform.tfstate`  |
| prod        | `forge/prod/terraform.tfstate`     |

R2 backend credentials (`AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`) are
R2 access keys, exported into the environment — never committed.

## Local gate (what runs here)

```bash
# Formatting — must be clean.
terraform fmt -check -recursive infra

# Validation (no cloud calls; backend disabled).
cd infra && terraform init -backend=false && terraform validate
```

`tofu` and `terraform` accept identical HCL; either binary works for the
gate. `validate` runs offline — it does **not** contact Hetzner/Cloudflare/R2.

## Apply runbook (from blueprint → live)

> Prerequisites: a Hetzner Cloud project + API token, a Cloudflare account
> with the target zone + a scoped API token, and an R2 bucket + access keys
> for state. None of these exist in this repo — this is the manual bootstrap.

1. **Create the state bucket** (one-time, out-of-band): create the
   `forge-tfstate` R2 bucket and an R2 access key pair.
2. **Configure backend + secrets:**
   ```bash
   cp infra/backend.hcl.example infra/backend.hcl        # fill in bucket/endpoint
   export AWS_ACCESS_KEY_ID=...  AWS_SECRET_ACCESS_KEY=...  # R2 state creds
   export TF_VAR_hcloud_token=...                          # Hetzner token
   export TF_VAR_cloudflare_api_token=...                  # Cloudflare token
   ```
3. **Per-env vars:** `cp infra/terraform.tfvars.example infra/envs/prod/prod.auto.tfvars` and edit (non-secret values).
4. **Init with per-env state key:**
   ```bash
   cd infra/envs/prod
   tofu init -backend-config=../../backend.hcl \
             -backend-config="key=forge/prod/terraform.tfstate"
   ```
5. **Plan / apply:** `tofu plan -out tfplan` → review → `tofu apply tfplan`.
6. **Fly apps:** `make fly-deploy ENV=prod` (renders `fly.toml`, runs
   `flyctl deploy`) — see `infra/modules/fly/`.

## CI wiring (follow-up — not committed here)

CI for `infra/` is **documented, not yet wired**: this token cannot modify
`.github/workflows/`. Add a workflow that, on PRs touching `infra/**`, runs:

- `tofu fmt -check -recursive infra`
- `tflint --recursive` (with the hcloud + cloudflare rulesets)
- `tofu validate` per env (`-backend=false`)
- `tofu plan` per env on protected branches (read-only creds, plan posted as
  a PR comment)

Wire state/provider secrets via the CI provider's secret store, mirroring the
`TF_VAR_*` / `AWS_*` env vars above.
