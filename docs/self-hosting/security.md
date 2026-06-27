# Security hardening

Forge is built to run inside your own perimeter with bring-your-own-keys. This
guide covers the operator's responsibilities: secrets, credential rotation,
network policy, and the platform's built-in guardrails. It complements the
deployment hardening described in [docker-compose.md](docker-compose.md).

## Secrets

All secrets are supplied through `.env` (see
[../../.env.example](../../.env.example)) and, for per-workspace provider keys,
the encrypted BYOK vault in Postgres.

- **Generate strong values.** `SECRET_KEY`, `AUTH_SECRET`, `POSTGRES_PASSWORD`,
  and `MINIO_ROOT_PASSWORD` must be long and random:

  ```bash
  openssl rand -hex 32
  ```

- **`SECRET_KEY` is the master key.** The BYOK vault is encrypted at rest with
  it. If it leaks, rotate it and re-encrypt the vault. If it is lost, the vault
  is unrecoverable — keep an off-host copy with your backups' twin (never in the
  same archive; see [backup.md](backup.md)).
- **Never commit `.env`.** It is git-ignored by default. Restrict it on disk:
  `chmod 600 .env`.
- **Secrets are redacted everywhere.** Forge redacts secret values from logs,
  traces, and retrieval results by design. Do not defeat this by echoing secrets
  into application logs or by lowering `LOG_LEVEL` to debug in production.

## Credential rotation

Rotate on a schedule and immediately after any suspected exposure or staff
departure.

| Credential | How to rotate |
|---|---|
| `POSTGRES_PASSWORD` | `ALTER ROLE forge WITH PASSWORD '...'`, update `.env`, recreate `api`/`worker`/`mcp-gateway`. |
| `MINIO_ROOT_PASSWORD` | Update `.env`, recreate `minio`, re-point clients. |
| `AUTH_SECRET` | Update `.env`, recreate `api`/`web`; existing sessions are invalidated. |
| `SECRET_KEY` | Rotate, then re-encrypt the BYOK vault with the new key; recreate `api`/`worker`. |
| Provider / BYOK keys | Replace in the per-workspace vault through the UI/API; old key stops being used immediately. |
| GitHub App / Slack secrets | Rotate at the provider, update `.env`, recreate dependent services. |

After any rotation, recreate the affected services so they pick up the new
values:

```bash
docker compose -f deploy/docker-compose.yml up -d --force-recreate api worker
```

## Network policy

The production compose file segments traffic into `edge`, `backend`, `data`, and
`mcp` networks, with `data` marked `internal` so Postgres, Redis, and MinIO are
unreachable from the edge. Preserve this posture:

- Only `caddy` should publish ports to the host (`80`/`443`). Do not add host
  port mappings to `db`, `redis`, or `minio`.
- Terminate TLS at Caddy (automatic HTTPS via [../../deploy/caddy/Caddyfile](../../deploy/caddy/Caddyfile)).
  Set a real `DOMAIN` so certificates are issued.
- Put the host behind a firewall; expose only 80/443 (and your SSH port) to the
  internet.
- Keep app containers non-root (`user: "1000:1000"`, already set).

## Built-in guardrails

Forge ships safe-by-default controls. Operators should understand them rather
than weaken them:

- **MCP is read-only by default.** Connections set `allow_write: false`; enabling
  writes is an explicit, audited per-connection decision. The example connectors
  in [../../examples/mcp-connectors](../../examples/mcp-connectors) all keep
  writes disabled.
- **MCP token binding (RFC 8707).** Tokens are bound to the specific MCP resource
  they were issued for, so a token cannot be replayed against another server.
- **Repo policy is deny-by-default.** `.forge/policy.yaml` governs what an agent
  may touch; a `deny` glob beats an `allow` glob and unknown actions are denied.
  See the examples in [../../examples/policies](../../examples/policies).
- **RBAC.** Roles are `admin`, `member`, `viewer`, and `agent-runner`; viewers
  cannot perform writes and agent-runners are scoped to automated execution.
- **Per-workspace isolation.** Tenant data is workspace-scoped; one workspace
  cannot read another's data, secrets, or retrieval index.
- **Immutable audit log.** Every agent action, tool call, MCP call, and approval
  is appended to an append-only audit log for after-the-fact review.

## Operational checklist

- [ ] All four core secrets are unique 32-byte random values.
- [ ] `.env` is `chmod 600` and excluded from version control and backups.
- [ ] An off-host copy of `SECRET_KEY` exists, stored apart from data backups.
- [ ] Only `caddy` publishes host ports; `data` network is `internal`.
- [ ] `DOMAIN` is set and Caddy has issued a valid certificate.
- [ ] A credential rotation schedule is documented and owned.
- [ ] MCP connections that need writes are explicitly reviewed and audited.
- [ ] Backups are encrypted in transit and at rest off-host.

## Related

- [docker-compose.md](docker-compose.md) — deployment-level hardening.
- [backup.md](backup.md) — protecting backup credentials.
- [troubleshooting.md](troubleshooting.md) — auth and certificate failures.
