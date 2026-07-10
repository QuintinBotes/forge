# Module: `hetzner-control-plane`

Stands up the **always-on Forge control plane** on Hetzner Cloud — the node(s)
that run the Forge Docker Compose stack (API · worker · Postgres+pgvector ·
Redis · MinIO) referenced by [`deploy/docker-compose.yml`](../../../deploy/docker-compose.yml).

What it creates:

| Resource | Purpose |
| --- | --- |
| `hcloud_network` + `hcloud_network_subnet` | Private network the control plane runs on; intra-cluster traffic stays off the public interface. |
| `hcloud_server` × `server_count` | Control-plane node(s). Primary (index 0) hosts the stateful containers. |
| `hcloud_volume` (postgres, minio) | **Persistent** data volumes, attached to the primary node. Survive server rebuild. |
| `hcloud_volume_attachment` × 2 | Attach + auto-mount the data volumes on the primary. |
| `hcloud_firewall` | Allow SSH from admin CIDRs, HTTP/HTTPS from the internet (or the CF tunnel), optional ICMP; deny the rest. |
| `hcloud_ssh_key` × N | Registers the supplied public keys for root login. |
| `hcloud_floating_ip` (optional) | Stable public IPv4, re-pointable across nodes for zero-downtime replacement. |
| cloud-init `user_data` | Installs Docker + compose plugin, mounts the data volumes, optionally clones the repo and `docker compose up -d`. |

> **Provider config is NOT in this module.** The `hcloud` provider (and its
> API token) is configured by the calling environment root and passed in
> implicitly. This module only declares the `~> 1.48` version constraint.

## Usage

```hcl
module "control_plane" {
  source = "../../modules/hetzner-control-plane"

  name_prefix = local.name_prefix          # e.g. "forge-prod"
  labels      = local.baseline_tags
  location    = var.region                  # "nbg1"
  network_zone = "eu-central"

  server_count = 1
  server_type  = "cpx41"                     # 8 vCPU / 16 GB
  ssh_public_keys = {
    ops = file("~/.ssh/forge_ops.pub")
  }

  admin_ssh_cidrs = ["203.0.113.10/32"]      # bastion / admin only
  # Force all web ingress through the Cloudflare tunnel by narrowing this:
  # http_ingress_cidrs = local.cloudflare_ingress_cidrs

  postgres_volume_size = 100
  minio_volume_size    = 250

  enable_floating_ip       = true
  enable_delete_protection = true            # prod: guard Postgres/MinIO data

  # Bootstrap: install Docker + clone the stack, but DON'T auto-start
  # (inject secrets first, then `docker compose up -d`).
  forge_repo_url    = "https://github.com/your-org/forge.git"
  forge_repo_ref    = "main"
  compose_autostart = false
}
```

## Networking & the firewall

Hetzner Cloud firewalls filter the **public** interface only — traffic on the
attached private network is **not** filtered. That is intentional: the compose
services talk to each other over the private subnet (or the Docker bridge on a
single node) with no firewall in the path, while the public interface exposes
only 80/443 (and SSH to admin CIDRs).

- **SSH (22):** allowed only from `admin_ssh_cidrs`. Leave it empty and *no*
  public SSH rule is created (reach the box via the private net / a bastion).
- **HTTP/HTTPS (80/443):** allowed from `http_ingress_cidrs` (default: the
  internet). To force ingress through Cloudflare, set this to the Cloudflare
  edge/tunnel ranges (or run a `cloudflared` tunnel and close 80/443 entirely).
- **Everything else inbound:** implicitly denied by Hetzner. Outbound is left
  at Hetzner's allow-all default so the node can pull images and clone the repo.

## Persistent data & delete protection

The Postgres and MinIO volumes are the durable state of the platform. They are
separate from the server lifecycle, so rebuilding/replacing a node does not
touch the data. Set `enable_delete_protection = true` in prod so a stray
`tofu destroy` cannot remove the servers *or* the volumes without first
clearing protection.

cloud-init mounts them by their stable `/dev/disk/by-id/scsi-0HC_Volume_<id>`
path under `/mnt/forge/postgres` and `/mnt/forge/minio` (see
`volume_devices` output). Point the compose named-volume bind targets at these
mount paths in production.

## cloud-init bootstrap

`enable_bootstrap = true` (default) renders `templates/cloud-init.yaml.tftpl`:

1. Installs Docker Engine + the compose plugin from Docker's apt repo.
2. Mounts the attached data volumes (`nofail`, added to `/etc/fstab`).
3. If `forge_repo_url` is set, clones it at `forge_repo_ref` to `/opt/forge`.
4. If `compose_autostart = true`, runs `docker compose -f <compose_file> up -d`.

**Secrets are never baked into user_data.** Keep `compose_autostart = false`
for production first-boot: the stack is cloned but not started, so you inject
the env file / secrets (via `TF_VAR_*`-driven out-of-band provisioning or a
config-management step) *before* bringing it up. `user_data` is in
`ignore_changes` — editing the template will not churn a running node; a
re-provision is a deliberate act.

## Inputs

See [`variables.tf`](./variables.tf) — every variable is documented with a
description, default, and validation. Highlights:

| Variable | Default | Notes |
| --- | --- | --- |
| `name_prefix` | — (required) | `${project}-${environment}`. |
| `location` | `nbg1` | Hetzner location. |
| `server_count` | `1` | Single-node stack by default. |
| `server_type` | `cpx41` | 8 vCPU / 16 GB. |
| `ssh_public_keys` | — (required) | map(name => OpenSSH pubkey). |
| `admin_ssh_cidrs` | `[]` | Empty ⇒ no public SSH rule. |
| `http_ingress_cidrs` | `0.0.0.0/0, ::/0` | Narrow to the CF tunnel to lock down. |
| `postgres_volume_size` | `50` GB | Grow-only. |
| `minio_volume_size` | `100` GB | Grow-only. |
| `enable_floating_ip` | `false` | Stable re-pointable IPv4. |
| `enable_delete_protection` | `true` | Guards servers + data volumes. |
| `compose_autostart` | `false` | Keep false in prod (inject secrets first). |

## Outputs

See [`outputs.tf`](./outputs.tf): `server_ids`, `server_ipv4`, `server_ipv6`,
`server_private_ips`, `primary_ipv4`, `network_id`, `network_ip_range`,
`subnet_ip_range`, `firewall_id`, `ssh_key_ids`, `volume_ids`,
`postgres_volume_id`, `minio_volume_id`, `volume_devices`, `floating_ip`.

## Local gate

```bash
terraform fmt -check -recursive infra
terraform -chdir=infra/modules/hetzner-control-plane init -backend=false
terraform -chdir=infra/modules/hetzner-control-plane validate
```

`validate` runs offline — it does not contact Hetzner. This module has **not**
been applied (no Hetzner project/token attached to this repo); see the repo
[`infra/README.md`](../../README.md) "Apply runbook".
