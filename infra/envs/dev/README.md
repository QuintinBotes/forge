# Environment: dev

Minimal single-node dev tier. See
[`docs/self-hosting/iac.md`](../../../docs/self-hosting/iac.md) for the full
apply runbook (state bootstrap, secrets, per-env init/plan/apply). This file
only covers what's specific to `dev`.

## Sizing (defaults, see `variables.tf`)

| Knob                       | Default              | Rationale                              |
| --------------------------- | -------------------- | --------------------------------------- |
| `server_count`               | `1`                  | Single node — no HA in dev              |
| `server_type`                | `cpx21` (3 vCPU/4GB) | Smallest box that runs the full compose stack |
| `postgres_volume_size`       | `20` GB              | Disposable dev data                    |
| `minio_volume_size`          | `20` GB              | Disposable dev data                    |
| `enable_delete_protection`   | `false`              | Lets `tofu destroy` clean up freely    |
| `enable_floating_ip`         | `false`              | Not needed for a single disposable box |
| `enable_tunnel` (Cloudflare) | `true`               | No public 80/443 listener even in dev  |

## Quickstart

```bash
cd infra/envs/dev
tofu init \
  -backend-config=../../backend.hcl \
  -backend-config="key=forge/dev/terraform.tfstate"
cp terraform.tfvars.example dev.auto.tfvars   # edit non-secret values
export TF_VAR_hcloud_token=...
export TF_VAR_cloudflare_api_token=...
tofu plan -out tfplan
tofu apply tfplan
```
