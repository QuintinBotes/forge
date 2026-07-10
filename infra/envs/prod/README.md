# Environment: prod

HA-leaning production tier. See
[`docs/self-hosting/iac.md`](../../../docs/self-hosting/iac.md) for the full
apply runbook (state bootstrap, secrets, per-env init/plan/apply). This file
only covers what's specific to `prod`.

## Sizing (defaults, see `variables.tf`)

| Knob                       | Default                | Rationale                                        |
| --------------------------- | ----------------------- | -------------------------------------------------- |
| `server_count`               | `2`                     | Primary (stateful) node + one reserved-capacity node |
| `server_type`                | `cpx51` (16 vCPU/32GB)  | Production headroom for the primary stateful node   |
| `postgres_volume_size`       | `100` GB                | Production data                                     |
| `minio_volume_size`          | `200` GB                | Production object data                              |
| `enable_delete_protection`   | `true` (enforced)       | A stray destroy must never wipe prod data           |
| `enable_floating_ip`         | `true`                  | Zero-downtime replacement of the primary node        |
| `admin_ssh_cidrs`            | required, no default    | Must be set explicitly; `0.0.0.0/0` is rejected      |
| `forge_repo_ref`             | `main` (override before apply) | Pin to a release tag/sha for a real deploy   |
| `enable_tunnel` (Cloudflare) | `true`                  | No public 80/443 listener                            |

## HONEST CEILING — what "HA-leaning" means here

The Forge compose stack is **single-node today**: Postgres/pgvector and
MinIO run on, and hold their data volumes on, the **primary** node
(`server_count` index `0`) only. `server_count = 2` provisions a **second**
node on the same private network so prod is topology-ready to grow into
real multi-node HA (e.g. a warm standby, or worker-only capacity) without a
network/firewall re-provision. It is **not** an active standby and there is
no automatic failover — treat it as reserved capacity until the compose
stack grows a replication/failover story. Full multi-node HA is out of
scope for this slice.

## Quickstart

```bash
cd infra/envs/prod
tofu init \
  -backend-config=../../backend.hcl \
  -backend-config="key=forge/prod/terraform.tfstate"
cp terraform.tfvars.example prod.auto.tfvars   # edit non-secret values, pin forge_repo_ref
export TF_VAR_hcloud_token=...
export TF_VAR_cloudflare_api_token=...
tofu plan -out tfplan
tofu apply tfplan
```
