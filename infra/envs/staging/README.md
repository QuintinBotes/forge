# Environment: staging

Mid-tier pre-prod environment. See
[`docs/self-hosting/iac.md`](../../../docs/self-hosting/iac.md) for the full
apply runbook (state bootstrap, secrets, per-env init/plan/apply). This file
only covers what's specific to `staging`.

## Sizing (defaults, see `variables.tf`)

| Knob                       | Default               | Rationale                                   |
| --------------------------- | ---------------------- | -------------------------------------------- |
| `server_count`               | `1`                    | Single node — mirrors dev's topology, bigger box |
| `server_type`                | `cpx41` (8 vCPU/16GB)  | Realistic headroom for pre-prod load/perf testing |
| `postgres_volume_size`       | `50` GB                | Room for representative data volumes         |
| `minio_volume_size`          | `100` GB               | Room for representative object data          |
| `enable_delete_protection`   | `true`                 | Staging data is worth protecting              |
| `enable_floating_ip`         | `false`                | Set `true` to rehearse zero-downtime node replacement |
| `enable_tunnel` (Cloudflare) | `true`                 | No public 80/443 listener                     |

## Quickstart

```bash
cd infra/envs/staging
tofu init \
  -backend-config=../../backend.hcl \
  -backend-config="key=forge/staging/terraform.tfstate"
cp terraform.tfvars.example staging.auto.tfvars   # edit non-secret values
export TF_VAR_hcloud_token=...
export TF_VAR_cloudflare_api_token=...
tofu plan -out tfplan
tofu apply tfplan
```
