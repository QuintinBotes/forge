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

### Envelope-vault key material (F37)

The F37 envelope vault (`forge_auth`) uses AES-256-GCM with a **versioned**
32-byte master key (KEK) and an HKDF-derived per-workspace data key; the
workspace id is bound into the GCM AAD, so a ciphertext copied across tenants
fails authentication. Generate KEKs and peppers with:

```bash
python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"
```

- `FORGE_VAULT_KEYS` — versioned KEK map, e.g. `1:<base64-32B>`. To rotate,
  append a new version (`1:<old>,2:<new>`), set
  `FORGE_VAULT_ACTIVE_KEY_VERSION=2`, and re-encrypt existing blobs
  (`SecretVault.rotate`); old versions keep decrypting until removed
  (crypto-shred by deleting a retired version).
- `API_KEY_PEPPER` — HMAC pepper for platform API-key hashing. Platform keys
  (`forge_pat_*` / `forge_svc_*` / `forge_agt_*`) are one-way hashed and shown
  once at mint; a database dump cannot recover a usable token.
- Startup is **fail-closed**: missing/malformed key material raises
  immediately rather than falling back to an insecure default.

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

### Rotation runbook — step by step

The order matters for `SECRET_KEY` (it keys the BYOK vault): rotate the key
*and* re-encrypt the vault in one maintenance window, or existing secrets become
undecryptable.

1. **`SECRET_KEY` (`FORGE_SECRET_KEY`)** — the vault master key.
   1. Generate: `python -c 'import secrets; print(secrets.token_urlsafe(32))'`.
   2. Prefer the **envelope** path (F37): add the new key as a new version in
      `FORGE_VAULT_KEYS` (e.g. `2:<new-b64>,1:<old-b64>`), set the new version as
      active, run `SecretVault.rotate` to re-wrap DEKs, then drop the old version
      once `rotate` completes. Old versions keep decrypting until removed, so
      there is no downtime.
   3. Recreate `api` + `worker`. Verify a known BYOK secret still decrypts
      through the API before removing the old key version.
   4. **Never** run production without `FORGE_SECRET_KEY` — the API refuses to
      boot outside `development` rather than fall back to an ephemeral key.
2. **`AUTH_SECRET`** — recreate `api` + `web`; all sessions are invalidated
   (users re-log-in). API keys are unaffected.
3. **API keys / BYOK provider keys** — rotate in the per-workspace vault via the
   UI/API; the old value stops being used immediately and the change is audited.
4. **`POSTGRES_PASSWORD` / `MINIO_ROOT_PASSWORD`** — change at the service, then
   `.env`, then recreate the dependent app containers.
5. **Webhook signing secrets** (GitHub/Slack/PM/alert providers) — rotate at the
   provider, update `.env`, recreate dependents. Until the new secret is set the
   webhook route fails **closed** (rejects deliveries) rather than trusting an
   unsigned payload.

After any rotation, confirm the health of the enforcement controls with
`uv run pytest -m security` (offline) against the deployed revision.

## HARD-09 edge controls (SSRF / rate limit / body limit / docs / headers)

These operator knobs default to the safe value; tune only with intent.

| Env var | Default | Meaning |
|---|---|---|
| `FORGE_RATELIMIT_ENABLED` | `true` | per-caller request rate limiting (429 + `Retry-After`) |
| `FORGE_RATELIMIT_RPM` | `120` | requests/minute per principal (or client IP) |
| `FORGE_RATELIMIT_BURST` | `60` | token-bucket burst allowance |
| `FORGE_MAX_BODY_BYTES` | `1048576` | max request body (1 MiB) before 413 |
| `FORGE_SSRF_ALLOW_PRIVATE` | `false` | allow private/RFC1918 outbound targets (self-hosted embedder/reranker on an internal network) — loopback and the cloud metadata endpoint stay blocked even when `true` |
| `FORGE_OUTBOUND_ALLOWLIST` | `[]` | JSON list of exact hostnames always permitted outbound |
| `FORGE_DOCS_ENABLED` | `true` | OpenAPI docs; forced **off** when `FORGE_ENVIRONMENT=production` unless explicitly set |

Notes:

- The **rate limiter is per-process** (in-memory token bucket). In a
  multi-replica deployment the effective limit is `RPM × replicas`; treat it as a
  coarse per-instance guard and put a shared limiter / WAF at the edge for hard
  global limits. `/health` is always exempt so liveness probes are unaffected.
- The **SSRF guard** is defense-in-depth, **not** a replacement for the `data`/
  `mcp` network segmentation below. If your embedder/reranker/MCP endpoints live
  on a private network, add their hostnames to `FORGE_OUTBOUND_ALLOWLIST` (narrow)
  or set `FORGE_SSRF_ALLOW_PRIVATE=true` (broad) — do not disable the guard.
- **Security headers** (HSTS, `X-Content-Type-Options`, `X-Frame-Options: DENY`,
  `Referrer-Policy`, a deny-all CSP on JSON) are added at the API edge and
  mirrored for the web app in `apps/web/next.config`.

## Enforcement matrix (what is verified, continuously)

Every security control Forge claims is asserted on the **wired request path** by
a regression suite (`uv run pytest -m security`) and re-run on every CI push by
a blocking `security` job (SAST via bandit + custom semgrep rules, dependency
CVE audit via pip-audit/osv, secret scan via gitleaks, and a CycloneDX SBOM).
The full control list and the test that proves each is in
[`../security/evidence/enforcement-matrix.md`](../security/evidence/enforcement-matrix.md);
the threat model is in [`../security/threat-model.md`](../security/threat-model.md).
Run the whole local audit with `make security`.

Accepted-risk waivers are dated, owned, and **expire** (an expired waiver fails
the gate closed) in [`../../security/waivers.yaml`](../../security/waivers.yaml);
the triage of every automated finding is in
[`../../SECURITY_FINDINGS.md`](../../SECURITY_FINDINGS.md).

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

## Container sandboxing (F19)

By default Forge executes a task's verification/build commands as host
subprocesses inside a git **worktree** (`FORGE_SANDBOX_KIND=worktree`). Worktrees
are an isolation convenience, **not** a security boundary: untrusted repo code
(tests, build scripts, transitive deps) runs with the worker's privileges. For
multi-tenant deployments, opt in to **container** isolation
(`FORGE_SANDBOX_KIND=container`) to run those commands inside a per-task,
locked-down Docker container.

- **No raw Docker socket in the worker.** The worker reaches the daemon **only**
  through a `tecnativa/docker-socket-proxy` (`DOCKER_HOST=tcp://docker-proxy:2375`)
  restricted to the minimal verbs (`CONTAINERS/IMAGES/POST/EXEC/INFO`) — no
  network create, no volume delete, no swarm, no privileged. The socket-mounting
  proxy is the single privileged surface (read-only, on the `internal`
  `sandbox_ctl` network), a documented exception alongside `autoheal`.
- **Least privilege by default.** Each sandbox runs with `cap_drop: ALL`,
  `no-new-privileges`, a non-root uid/gid (10001), a read-only root fs + tmpfs
  scratch, and CPU/memory/PID limits.
- **No cross-task filesystem access.** A container mounts **only** its run's
  worktree subpath at `/workspace` (`volume-subpath`, Docker Engine >= 26.1 —
  `preflight.sh` gates this). No other task's worktree or host path is mounted.
- **No network by default.** `FORGE_SANDBOX_NETWORK=none`. `egress` mode routes
  the container's only path out through an allow-listing forward proxy
  (`sandbox-proxy`) on the `internal` `sandbox_egress` network, with a domain
  allowlist (`FORGE_SANDBOX_EGRESS_ALLOWLIST`).
- **Curated image allowlist.** The sandbox image must be on
  `FORGE_SANDBOX_ALLOWED_IMAGES`; a repo policy cannot point the sandbox at an
  arbitrary image.
- **Secrets never enter the sandbox.** The BYOK model key and GitHub App
  credentials are never placed in the sandbox environment or any persisted
  `sandbox_instance` row.
- **No silent downgrade.** If `container` is requested but the daemon/proxy is
  unreachable, the run fails loudly — Forge never quietly executes untrusted code
  on the host when the operator asked for containment.
- **Orphan reaping.** A `sandbox.reap_orphans` beat task and a worker-boot pass
  remove any `forge.sandbox=true` container whose run is terminal or older than
  `FORGE_SANDBOX_MAX_TTL_SECONDS`.

> Future hardening (V3): Sysbox/gVisor runtimes and Firecracker microVMs plug in
> behind the same `SandboxProvider` seam — shipped as F34, below.

## Kernel-boundary sandboxing (F34)

A Docker container (`runc`) still **shares the host kernel**: its isolation
rests on namespaces, cgroups and seccomp, so one kernel-level exploit escapes
every sandbox on the node. For genuinely untrusted, multi-tenant workloads F34
adds two **kernel-boundary** tiers behind the same `SandboxProvider` seam:

| `FORGE_SANDBOX_KIND` | Runtime | Isolation class | Boundary |
|---|---|---|---|
| `worktree` | — | `host_process` | none (V1 default) |
| `container` | `runc` | `namespace` | shared host kernel |
| `gvisor` | `runsc` | `userspace_kernel` | gVisor application kernel services the guest's syscalls in userspace |
| `microvm` | `kata-fc` | `microvm` | Firecracker microVM with its **own guest kernel** (Kata Containers) |

- **Monotonic isolation lattice.** `worktree < container < gvisor < microvm`.
  `FORGE_SANDBOX_KIND` is the workspace **minimum**; a repo's
  `.forge/policy.yaml` `sandbox.isolation` may *strengthen* it but never weaken
  it. An operator mandate of `microvm` cannot be undercut by a repo policy.
- **No silent downgrade — ever.** If the resolved kind's runtime is not
  registered (or `/dev/kvm` is missing for `microvm`), worker boot and the run
  fail loudly with `SandboxRuntimeUnavailable`; `deploy/scripts/preflight.sh`
  gates the same conditions before `up`. Forge never quietly executes untrusted
  code under a weaker runtime than the operator asked for.
- **Enabling gVisor (no special hardware).** Run
  `deploy/scripts/install-runtimes.sh --gvisor` on the daemon host (installs
  `runsc`, merges `runtimes.runsc` into `/etc/docker/daemon.json`, restarts the
  daemon), then set `FORGE_SANDBOX_KIND=gvisor`. The gVisor platform is
  selectable via `FORGE_SANDBOX_GVISOR_PLATFORM` (`systrap` default; `kvm`
  fastest but needs `/dev/kvm`; `ptrace` most compatible).
- **Enabling Firecracker microVMs (KVM required).** On a bare-metal or
  nested-virt-enabled host, run `install-runtimes.sh --firecracker` (Kata
  Containers + Firecracker, registers `kata-fc`), verify `/dev/kvm`, then set
  `FORGE_SANDBOX_KIND=microvm`. Guest sizing defaults derive from the sandbox
  CPU/memory limits and can be overridden per repo (`vm_vcpus`, `vm_memory`) or
  workspace-wide (`FORGE_SANDBOX_MICROVM_VCPUS` / `_MEMORY_MB`).
- **All F19 controls still apply inside the stronger boundary** — single
  worktree mount, `cap_drop: ALL`, non-root uid, read-only rootfs, limits,
  no-network default, egress allowlist proxy, curated images, output capping,
  orphan reaping. The kernel boundary is defense-in-depth on top, not a
  replacement. Runtime selection is part of `POST /containers/create`, so the
  socket-proxy verb set is **unchanged** and the worker still never touches the
  raw socket.
- **Auditable trust tier.** Every run's `sandbox_instance` row records the
  `runtime`, `isolation_class`, and (for microVM) the `guest_kernel_version`,
  vCPU/RAM sizing and boot latency — auditors can query exactly which boundary
  contained each piece of untrusted execution.
- **Residual trust.** `microvm` shifts trust to KVM + the Firecracker VMM; on
  cloud VMs nested virtualization must be enabled. Prefer dedicated bare-metal
  sandbox nodes for the highest-untrust tenants, and gVisor as the no-KVM tier.
  On Kubernetes, isolate runtime-capable node pools via the chart's
  `RuntimeClass` scheduling (see [kubernetes.md](kubernetes.md)).
- **VM artifact hygiene.** The reaper sweeps any orphaned jailer chroot under
  `FORGE_SANDBOX_JAILER_ROOT` once a run's sandbox container is gone.

## Enterprise SSO — SAML + SCIM (F33)

- **SAML 2.0 (Forge as SP).** Per-workspace federation with a corporate IdP
  (Okta / Entra ID / Google Workspace / generic). Responses are validated with
  mandatory XML-DSig signature checks (toolkit-delegated to `signxml` — pure
  Python, no native `xmlsec1` needed), audience restriction, bounded clock
  skew (`FORGE_SAML_CLOCK_SKEW_SECONDS`), one-time `InResponseTo` consumption,
  and assertion-id replay caching. XML parsing is XXE-hardened (entities, DTD
  loading, and network access disabled; any DOCTYPE is rejected).
- **SCIM 2.0 provisioning.** `/scim/v2/*` authenticates with a per-workspace
  bearer token: CSPRNG-generated, stored only as a SHA-256 hash, constant-time
  compared, revocable, expirable, and revealed exactly once at mint time.
  `active=false`/`DELETE` deprovisions the user and revokes their sessions and
  agent tokens immediately.
- **HTTPS is mandatory in production.** `FORGE_PUBLIC_URL` must be the
  externally reachable HTTPS URL — the SP entity id, ACS URL, and SCIM base
  URL derive from it, and SAML requires TLS on the ACS. Caddy must proxy
  `POST /auth/saml/*/acs` and `/scim/v2/*` with bodies untouched.
- **Break-glass.** Disabling SSO (or deprovisioning the last active local
  admin) is refused while no non-SSO admin remains; keep at least one local
  admin so a misconfigured IdP can never lock the workspace out.
- **No privilege escalation via IdP.** Roles are honored only through the
  admin-configured group→role map; arbitrary asserted `role=admin` attributes
  are ignored. Every SSO/SCIM action lands in the immutable audit log with
  secrets redacted.

## Public benchmark leaderboard (F35)

The public evaluation leaderboard (`/public/leaderboard/...`) is **disabled by
default**: every `/public/*` route returns `404` until you explicitly set
`FORGE_PUBLIC_LEADERBOARD_ENABLED=true`, so a fresh self-hosted instance never
accidentally publishes internal benchmark scores. Before opting in:

- Only submissions an **admin explicitly published** appear (moderation gate);
  flagged entries are removed from the board immediately.
- Responses are structurally payload-free: score breakdowns, model *labels*,
  and submitter display names only. `submitter_contact`, raw run
  configuration, and BYOK keys are not representable in the public response
  models, and submission configs are secret-redacted at ingest before they
  are ever stored.
- The router is read-only (GET only), per-IP rate-limited
  (`FORGE_LEADERBOARD_PUBLIC_RATE_LIMIT`, default 60/min), and cache-fronted
  (`FORGE_LEADERBOARD_CACHE_TTL_SECONDS`).
- Only **verified** entries carry the badge: verification deterministically
  replays each submission's content-hashed bundles offline and rejects any
  claimed score that does not reproduce within
  `FORGE_BENCHMARK_VERIFY_EPSILON`.

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
