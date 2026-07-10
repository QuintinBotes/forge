# Module: `cloudflare`

Provisions the Cloudflare-side footprint for the Forge control plane: **R2
object storage**, **DNS**, and a **Cloudflare Tunnel** so the Hetzner
control plane (see [`../hetzner-control-plane`](../hetzner-control-plane))
doesn't need to expose any public port at all.

| Resource | Purpose |
| --- | --- |
| `cloudflare_r2_bucket.state` (gated) | Shared Tofu remote-state bucket. Gated behind `create_state_bucket` — see [State bucket bootstrap](#state-bucket-bootstrap-caveat). |
| `cloudflare_r2_bucket.object_store` × 3(+) | `audit_archive`, `marketplace`, `backups` — plus anything in `extra_object_store_buckets`. |
| `cloudflare_zero_trust_tunnel_cloudflared.this` (optional) | The Tunnel itself; `secret` auto-generated via `random_id` if not supplied. |
| `cloudflare_zero_trust_tunnel_cloudflared_config.this` (optional) | Remotely-managed ingress rules (hostname → origin service), one per `origin_routes` entry + a mandatory catch-all. |
| `cloudflare_record.tunnel` / `.ipv4` / `.ipv6` | DNS for the app domain (apex) + `api` subdomain (or any other `origin_routes` key), pointed at the Tunnel CNAME (default) or directly at the Hetzner IP. |

> **Provider config is NOT in this module.** The `cloudflare` provider (and
> its account-scoped API token) is configured by the calling environment
> root and passed in implicitly — same convention as
> `hetzner-control-plane`'s `hcloud` token.

## Usage

```hcl
module "cloudflare" {
  source = "../../modules/cloudflare"

  account_id  = var.cloudflare_account_id
  zone_id     = var.cloudflare_zone_id
  domain      = var.domain               # "forge.example.com"
  name_prefix = local.name_prefix         # "forge-prod"

  # --- R2 --------------------------------------------------------------
  # Leave *_bucket_name empty to derive "${name_prefix}-<purpose>".
  r2_location = "WEUR"                    # keep data close to a Hetzner EU control plane

  # --- Tunnel + DNS ------------------------------------------------------
  enable_tunnel = true                    # no public 80/443 on the Hetzner box at all
  origin_routes = {
    ""    = "http://localhost:80"         # apex -> Caddy on the control-plane box
    "api" = "http://localhost:80"         # api.<domain> -> same Caddy origin (see caveat below)
  }
}

# On the Hetzner box: run `cloudflared tunnel run --token <module.cloudflare.tunnel_token>`
# (e.g. as an extra compose service) — no inbound firewall rule required.
```

To instead expose the control plane directly (no Tunnel), point DNS at the
Hetzner IP:

```hcl
module "cloudflare" {
  source = "../../modules/cloudflare"
  # ...
  enable_tunnel = false
  origin_ipv4   = module.control_plane.primary_ipv4 # or .floating_ip
}
```

## R2 buckets

Four buckets in play, three of them purpose-named and validated as
first-class variables:

| Purpose | Variable | Default name |
| --- | --- | --- |
| Tofu remote state (shared, gated) | `state_bucket_name` | `forge-tfstate` |
| Audit-log archive (F39 compliance export) | `audit_archive_bucket_name` | `${name_prefix}-audit-archive` |
| Marketplace package artifacts | `marketplace_bucket_name` | `${name_prefix}-marketplace` |
| Control-plane backups (Postgres/MinIO dumps) | `backups_bucket_name` | `${name_prefix}-backups` |

`extra_object_store_buckets` (a `map(string)`) grafts on more without
changing this module — a natural landing spot if the self-hosted MinIO
`forge-checks` / `forge-traces` / `forge-postmortems` buckets (F08/F10/F39)
are ever migrated to R2.

### State bucket bootstrap caveat

The state bucket is **shared across every environment** — only the state
object `key` differs per env (`forge/{dev,staging,prod}/terraform.tfstate`,
see [`../../backend.tf`](../../backend.tf)). That makes it structurally
different from every other resource here: if each of `envs/dev`,
`envs/staging`, `envs/prod` tried to create it, they'd race on the same
bucket name. `create_state_bucket` therefore **defaults to `false`** —
enable it in exactly **one** bootstrap apply (see the repo
[`infra/README.md`](../../README.md) "Apply runbook" step 1), then leave it
`false` in every environment composition. This also sidesteps the
chicken-and-egg problem of needing the state bucket to exist before you can
`init` a backend that stores state *in* that bucket — the bootstrap apply
runs with a local (or no) backend, one time, out-of-band.

## Cloudflare Tunnel — no public ports

With `enable_tunnel = true` (the default), `cloudflared` runs **on** the
Hetzner control plane and dials **out** to Cloudflare's edge; the edge then
proxies matching hostnames in to it. The Hetzner box needs no inbound
80/443 rule at all — pair this with the `hetzner-control-plane` module's
`http_ingress_cidrs = []` to close those ports completely and rely
entirely on the tunnel for ingress.

- **Ingress rules** come from `origin_routes` (`map(string)`): each key is a
  DNS-record key (`""` = apex, `"api"` = the `api` subdomain, or any other
  label) and each value is the origin `service` URL cloudflared forwards
  matching requests to. A mandatory catch-all (`http_status:404`) is always
  appended last, as cloudflared requires.
- **Secret handling:** `tunnel_secret` defaults to `null`, which
  auto-generates a 32-byte secret via `random_id` — no credential ever
  needs to live in a tfvars file. `tunnel_token` (an output, marked
  `sensitive`) is what you actually hand to the `cloudflared` process
  (`cloudflared tunnel run --token <token>`); it's derived from the tunnel
  + secret server-side, so it's safe to pass through `TF_VAR_*`/CI secret
  stores same as any other credential.
- **Caveat — subdomain routing vs. today's Caddyfile:** the deployed
  [`deploy/caddy/Caddyfile`](../../../deploy/caddy/Caddyfile) is a single
  vhost (`{$DOMAIN}`) that splits `/api/*` vs. everything-else **by path**,
  not by subdomain. Provisioning an `api.<domain>` DNS/tunnel-ingress entry
  here is forward-looking (a distinct, brandable API hostname); today it
  will reach the same Caddy origin as the apex. Routing it to the `api`
  container directly requires either a second Caddy site block for
  `api.<domain>` or SNI-based origin routing added later — out of scope
  for this IaC slice.

## DNS records

Exactly one record per `origin_routes` key, whichever type applies:

- `enable_tunnel = true` → `CNAME` to `<tunnel-id>.cfargotunnel.com`,
  always `proxied = true` (required for Tunnel routing — Cloudflare's edge
  is the only thing that can reach the tunnel).
- `enable_tunnel = false` → `A` (from `origin_ipv4`) and/or `AAAA` (from
  `origin_ipv6`), proxied per `dns_proxied` (default `true`).

## Inputs

See [`variables.tf`](./variables.tf) — every variable is documented with a
description, default, and validation. Highlights:

| Variable | Default | Notes |
| --- | --- | --- |
| `account_id` / `zone_id` | — (required) | 32-char hex Cloudflare ids. |
| `domain` | — (required) | Apex domain; also `infra/variables.tf`'s shared `domain`. |
| `name_prefix` | — (required) | `${project}-${environment}`; derives default bucket/tunnel names. |
| `create_state_bucket` | `false` | See [bootstrap caveat](#state-bucket-bootstrap-caveat) — enable once, out-of-band. |
| `r2_location` | `null` (auto) | `WNAM`/`ENAM`/`WEUR`/`EEUR`/`APAC`/`OC`. |
| `enable_tunnel` | `true` | `false` requires `origin_ipv4`/`origin_ipv6`. |
| `origin_routes` | `{"" = ..., "api" = ...}` | Apex + api subdomain by default. |
| `tunnel_secret` | `null` (auto-generate) | Never commit a real value. |

## Outputs

See [`outputs.tf`](./outputs.tf): `state_bucket_name`, `state_bucket_id`,
`object_store_buckets`, `object_store_bucket_ids`,
`audit_archive_bucket_name`, `marketplace_bucket_name`,
`backups_bucket_name`, `tunnel_id`, `tunnel_name`, `tunnel_cname`,
`tunnel_token` (sensitive), `dns_fqdns`, `dns_record_ids`, `app_fqdn`,
`api_fqdn`.

## Local gate

```bash
terraform fmt -check -recursive infra
terraform -chdir=infra/modules/cloudflare init -backend=false
terraform -chdir=infra/modules/cloudflare validate
```

`validate` runs offline — it does not contact Cloudflare. This module has
**not** been applied (no Cloudflare account/zone attached to this repo);
see the repo [`infra/README.md`](../../README.md) "Apply runbook".
