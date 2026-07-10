# Infrastructure as Code (OpenTofu) — apply runbook

Forge's cloud infrastructure lives under [`infra/`](../../infra/) as
**OpenTofu** (Apache-2.0/MPL — the open-source successor to Terraform after
its BSL relicense). The HCL is byte-for-byte Terraform-compatible.

> **HONEST CEILING — read this first.** Everything under `infra/` is
> **written, `fmt`-clean, `validate`-clean code, plus this runbook**. It has
> **NOT** been `tofu apply`'d anywhere. There are no cloud accounts or paid
> infrastructure attached to this repo, so no state exists and no cloud
> resources have been created. This document is the path a human, with real
> cloud accounts, follows from this blueprint to a live deployment — treat
> it as reviewed and ready-to-run, not as evidence anything is running.

For the day-1 single-machine path (no cloud accounts, no OpenTofu), see
[quickstart.md](quickstart.md) instead — this document is for standing up
the same stack on managed cloud infrastructure (Hetzner Cloud + Cloudflare +
Fly.io) rather than a single Docker host you administer by hand.

## What this provisions

| Concern | Platform | Module |
| --- | --- | --- |
| Control plane (servers, private network, firewall, volumes) | Hetzner Cloud | [`infra/modules/hetzner-control-plane`](../../infra/modules/hetzner-control-plane) |
| Object storage (R2), DNS, Zero Trust Tunnel | Cloudflare | [`infra/modules/cloudflare`](../../infra/modules/cloudflare) |
| Agent runtime (scale-to-zero task execution) | Fly.io | [`infra/modules/fly-agents`](../../infra/modules/fly-agents) (`fly.toml` + `flyctl`, not a Tofu resource — see that module's README for why) |
| Remote state | Cloudflare R2 | `s3` backend, per-env state key (see below) |

Three environment compositions call these modules with different sizing:

| Environment | Sizing | Composition |
| --- | --- | --- |
| `dev` | Minimal single node (`cpx21`, 20 GB volumes, delete protection off) | [`infra/envs/dev`](../../infra/envs/dev) |
| `staging` | Mid (`cpx41`, 50/100 GB volumes, delete protection on) | [`infra/envs/staging`](../../infra/envs/staging) |
| `prod` | HA-leaning (`cpx51` x2, 100/200 GB volumes, floating IP, delete protection enforced) | [`infra/envs/prod`](../../infra/envs/prod) |

Each `infra/envs/<env>/README.md` documents that environment's exact sizing
defaults and the honest caveats (prod's second node is reserved capacity,
not an active standby — see [`infra/envs/prod/README.md`](../../infra/envs/prod/README.md)).

## Prerequisites

Install locally:

- **OpenTofu** (`tofu`, `>= 1.6`) — used for `init`/`plan`/`apply`. Install
  from [opentofu.org](https://opentofu.org/docs/intro/install/).
- **`terraform`** (any recent 1.x) is also fine for `fmt`/`validate` only —
  the HCL is identical; this repo's local gate uses whichever is on `PATH`.
- **`flyctl`** — `curl -L https://fly.io/install.sh | sh` — for the agent
  runtime deploy step.

Accounts + tokens (none of these exist for this repo — you provision them):

- A **Hetzner Cloud** project + API token (Console → Security → API Tokens,
  read+write scope).
- A **Cloudflare** account with the target DNS zone, an R2 subscription
  enabled, and a scoped API token (Account → R2 Edit, Zone → DNS Edit,
  Account → Cloudflare Tunnel Edit for the target zone/account).
- A **Fly.io** account + `FLY_API_TOKEN` (`flyctl auth token`) — see
  [`infra/modules/fly-agents/README.md`](../../infra/modules/fly-agents/README.md).
- An SSH keypair to register on the Hetzner servers.

## 1. Bootstrap the shared state bucket (one time, ever)

The Tofu remote-state bucket is **shared** across every environment (only
the state object `key` differs per env). Create it exactly once, out of
band of any per-env apply — the `cloudflare` module's
`create_state_bucket` variable exists for this and defaults to `false`
everywhere in `infra/envs/*` on purpose, so a normal env apply never races
to (re)create it.

The simplest bootstrap is the Cloudflare dashboard (R2 → Create bucket,
name it `forge-tfstate`) plus an R2 API token (R2 → Manage API Tokens).
Alternatively, run a one-off Tofu apply with `create_state_bucket = true`
against the `cloudflare` module directly and never repeat it.

Record the account id, bucket name, and R2 access key pair — you'll need
them for every subsequent step.

## 2. Configure the shared backend + secrets

```bash
cp infra/backend.hcl.example infra/backend.hcl   # gitignored — fill in bucket/endpoint

export AWS_ACCESS_KEY_ID="..."            # R2 access key id (state backend)
export AWS_SECRET_ACCESS_KEY="..."        # R2 secret access key (state backend)
export TF_VAR_hcloud_token="..."          # Hetzner Cloud API token
export TF_VAR_cloudflare_api_token="..."  # Cloudflare scoped API token
```

`infra/backend.hcl` and every `TF_VAR_*` value are secrets or
account-specific — **never commit them**. `infra/.gitignore` enforces this
for `backend.hcl` and any `*.tfvars` that isn't a `*.example` template.

## 3. Configure per-environment (non-secret) variables

Each environment has its own placeholder template:

```bash
cp infra/envs/dev/terraform.tfvars.example     infra/envs/dev/dev.auto.tfvars
cp infra/envs/staging/terraform.tfvars.example infra/envs/staging/staging.auto.tfvars
cp infra/envs/prod/terraform.tfvars.example    infra/envs/prod/prod.auto.tfvars
```

Edit each `<env>.auto.tfvars` with your domain, Cloudflare account/zone ids,
SSH public key(s), and admin CIDRs. These files are gitignored — only the
`.example` templates are tracked. **Secrets never go in these files** —
see step 2.

## 4. `init` / `fmt` / `validate` / `plan` / `apply`, per environment

Every environment is its own Tofu root (it has its own provider config and
backend). Run the full cycle from inside `infra/envs/<env>/`:

```bash
cd infra/envs/dev   # or staging, or prod

tofu init \
  -backend-config=../../backend.hcl \
  -backend-config="key=forge/dev/terraform.tfstate"

tofu fmt -check -recursive ../..     # repo-wide formatting gate
tofu validate                        # offline — no cloud calls

tofu plan -out tfplan
# review the plan carefully, especially for prod
tofu apply tfplan
```

Repeat for `staging` (`key=forge/staging/terraform.tfstate`) and `prod`
(`key=forge/prod/terraform.tfstate`) — swap the directory and the
`-backend-config="key=..."` value; nothing else changes.

**What this repo has actually run:** `tofu fmt -check` and `tofu validate`
(via `terraform`, identical HCL) — offline, no credentials, no cloud calls.
`plan`/`apply` require the real accounts and tokens above and have not been
exercised here.

## 5. Deploy the Fly.io agent runtime

The Fly agent runtime is modeled as `fly.toml` templates + `flyctl`, not a
Tofu resource (see
[`infra/modules/fly-agents/README.md`](../../infra/modules/fly-agents/README.md)
for why). Once `FLY_API_TOKEN` is exported:

```bash
make -C infra/modules/fly-agents deploy ENV=dev     # or staging / prod
```

This creates the Fly app (idempotent-best-effort), deploys
`fly.<env>.toml`, and applies the min/max machine scaling for that
environment. See that module's README for the `scale` / `regions` /
`status` / `render` targets.

## Teardown

Per environment, from `infra/envs/<env>/`:

```bash
tofu plan -destroy -out tfplan.destroy
tofu apply tfplan.destroy
```

Notes:

- `staging` and `prod` have `enable_delete_protection = true` by default
  (prod hard-enforces it via a variable validation) — Hetzner will refuse
  to delete protected servers/volumes. Set
  `enable_delete_protection = false` in the relevant `*.auto.tfvars` (or,
  for prod, temporarily comment out the enforcing validation block) and
  re-`apply` before a destroy is possible. This is deliberate friction —
  don't remove it casually.
- Destroying an environment does **not** delete the shared R2 state
  bucket (`create_state_bucket` stays `false` in every env) or the other
  environments' state objects. To retire the bucket itself, do so
  out-of-band, after every environment's state has been destroyed and
  migrated/backed up as needed.
- Fly apps are torn down separately: `flyctl apps destroy <app-name>` (see
  `infra/modules/fly-agents/deploy.sh`).

## CI wiring (documented, not wired — follow-up)

This token cannot modify `.github/workflows/`, so CI for `infra/` is
**documented here as a follow-up**, not committed. A future workflow should,
on PRs touching `infra/**`, run:

- `tofu fmt -check -recursive infra`
- `tflint --recursive` (hcloud + cloudflare rulesets)
- `tofu validate` per environment (`-backend=false`, no credentials needed)
- `tofu plan` per environment on protected branches, using read-only
  credentials, with the plan posted as a PR comment (never auto-applied
  from CI)

Wire `TF_VAR_hcloud_token` / `TF_VAR_cloudflare_api_token` /
`AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` through the CI provider's
secret store — the same four values used locally in step 2.

## Local gate reference

The commands this repo's `make infra-validate` target runs (see
[`Makefile`](../../Makefile)):

```bash
terraform fmt -check -recursive infra
for d in infra infra/envs/dev infra/envs/staging infra/envs/prod; do
  (cd "$d" && terraform init -backend=false -input=false >/dev/null && terraform validate)
done
```

All of the above run offline: no Hetzner/Cloudflare/Fly credentials, no
network calls beyond the local provider plugin cache.
