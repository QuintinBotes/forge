# Security hardening

Swarm Code Factory is built to run inside your own perimeter with bring-your-own-keys. This
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
- **Secrets are redacted everywhere.** SCF redacts secret values from logs,
  traces, and retrieval results by design. Do not defeat this by echoing secrets
  into application logs or by lowering `LOG_LEVEL` to debug in production.

### Envelope-vault key material (F37)

The F37 envelope vault (`scf_auth`) uses AES-256-GCM with a **versioned**
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

### Envelope encryption + KEK rotation (HARD-13)

The BYOK vault (the `api_key` table) is protected by **two-tier envelope
encryption**: every secret is encrypted under a fresh per-secret **data key
(DEK)**, and the DEK is wrapped under a versioned **key-encryption key (KEK)**.
The KEK is `FORGE_SECRET_KEY`; the wrapped DEK and its KEK version travel inside
the stored blob. This bounds blast radius (one leaked DEK ≠ all secrets) and
makes KEK rotation an O(rows) re-wrap of small DEKs — **no BYOK plaintext is ever
decrypted during rotation**.

Canonical config (12-factor; any key may instead be supplied as `<KEY>_FILE`):

| Env var | Meaning |
|---|---|
| `FORGE_ENVIRONMENT` | canonical env; `production` requires a real key (replaces `FORGE_ENV`). |
| `FORGE_SECRET_KEY` | current master KEK, ≥ 16 bytes (replaces `SECRET_KEY`). |
| `FORGE_SECRET_KEY_FILE` | path to a mounted Docker/K8s secret holding the KEK. |
| `FORGE_SECRET_KEY_VERSION` | current KEK version (int). |
| `FORGE_SECRET_KEY_V<n>` | previous KEK version, kept only during a rotation window. |
| `FORGE_ENVELOPE_ENCRYPTION` | `true` in prod — envelope vs single-tier cipher. |
| `FORGE_DEV_INSECURE` | dev-only explicit opt-in to an ephemeral key. **Never in prod.** |

Preflight your config before every deploy (blocking CI `secrets-config` gate):

```bash
python -m scf_api.cli_secrets check-config   # exits non-zero on any violation
```

**KEK rotation drill (zero-downtime).** Back up the database first.

1. Generate a new key: `python -c 'import secrets; print(secrets.token_urlsafe(32))'`.
2. Set `FORGE_SECRET_KEY` = the **new** key, `FORGE_SECRET_KEY_V1` = the **old**
   key, and `FORGE_SECRET_KEY_VERSION=2`.
3. Re-wrap every stored DEK under the new KEK:
   ```bash
   python -m scf_api.cli_secrets rotate-kek   # prints {"rewrapped": N, "skipped": M}
   ```
   Reads during the window transparently unwrap under whichever KEK version each
   row carries, so there is no downtime.
4. Verify a known BYOK secret still decrypts (`POST /auth/secrets/{id}/check`
   returns `{"ok": true}`), then **remove `FORGE_SECRET_KEY_V1`** from the env and
   recreate `api` + `worker`.

> **Downgrade note:** the additive migration `0023_envelope_key_version` is
> reversible, but rows written by the envelope cipher only remain decryptable
> while the code understands the envelope blob format — pair any schema downgrade
> with a matching code rollback.

**Value rotation vs KEK rotation.** Rotating a *credential value* (an operator
supplies a new secret) is `POST /auth/secrets/{id}/rotate`; rotating the *KEK*
(re-wrapping DEKs, no plaintext) is `secrets rotate-kek`. They are distinct and
separately audited (`secret.rotated` vs the `rotated_at`/`key_version` trail).

**Automatic expiry.** Stored BYOK secrets honour `expires_at` at read time (an
expired credential yields HTTP 409 "rotate this credential", not stale
plaintext), and minted agent-runner tokens auto-expire after
`FORGE_AGENT_TOKEN_TTL` (default 24h). A 15-minute sweep flags/purges expired
rows (`secrets sweep-expired`).

## GitHub App credentials per workspace

Agent runs reach GitHub with the GitHub App credentials of **their own
workspace**, stored encrypted in that workspace's vault. A run never borrows
another workspace's App, installation or token, so one host can serve several
workspaces without one workspace's run acting with another's access.

**Set them** (a workspace administrator; the route needs the `manage_keys`
permission):

```bash
curl -X PUT "$SCF_API/integration/github/app-credentials" \
  -H "Authorization: Bearer $ADMIN_KEY" -H 'Content-Type: application/json' \
  --data "$(jq -n --arg key "$(cat github-app.pem)" \
    '{app_id: "123456", installation_id: "7890123", private_key: $key}')"
```

The answer repeats `app_id` and `installation_id` and never the key. Setting again
replaces the stored credentials; `DELETE` on the same path clears them, after
which the workspace's runs can neither fetch nor deliver. `GET /auth/secrets`
lists the entry as `github-app`, without its value.

- **Installation.** When the run's repository connection records an
  installation id, the run uses it; otherwise the stored `installation_id`.
  Either must be a number.
- **One App, many workspaces.** A deployment with one shared GitHub App stores the
  same App id and key in each workspace, each with that workspace's installation
  id.
- **Tokens.** Every step mints its own installation token: one repository, only
  the step's permissions (`contents: read` to fetch the mirror,
  `contents: write` and `pull_requests: write` to deliver, `checks: read` for the
  merge guard), GitHub's one-hour lifetime at most. No run or workspace reuses
  another's token.
- **Audit.** Each token issued writes a `github.token_issued` audit record with
  the workspace, run, repository, installation, permissions and expiry. The
  token, the App JWT and the private key appear in no audit record, log, run
  event or error.
- **More than one workspace needs a container tier.** On the container tiers
  (`container`, `gvisor`, `microvm`) an agent run's commands start from an
  environment of their own: no token and no `FORGE_GITHUB_*` variable reaches
  them. The `worktree` tier is not a boundary. Its commands are host processes
  with the worker's privileges: they inherit the worker's environment, including
  `FORGE_SECRET_KEY` and `FORGE_DATABASE_URL`, which together open every
  workspace's stored `github-app` entry, and they can read the worker's own
  process environment. A production install that serves more than one workspace
  must run a container tier (#184 makes one the default). The `worktree` tier
  drops the `FORGE_GITHUB_*` variables from a command's environment; that is
  defence in depth, not isolation.

**The environment variables are a development fallback only.**
`FORGE_GITHUB_APP_ID`, `FORGE_GITHUB_INSTALLATION_ID` and
`FORGE_GITHUB_APP_PRIVATE_KEY_PATH` on the worker are used only when the worker
runs as development and the workspace stored no credentials. A worker runs as
development when `FORGE_ENVIRONMENT=development`, when `FORGE_ENVIRONMENT` is not
set at all (development is the default), and with the deprecated
`FORGE_ENV=development`. **Set `FORGE_ENVIRONMENT=production` on every worker**
(`deploy/docker-compose.yml` does). In any other environment a workspace without
stored credentials has none: delivery refuses (`GitHubAppNotConfigured`, or
`not_configured` on a development run), and a repository with no mirror yet is
unavailable, even when the variables are set. The API's own GitHub client for
webhooks and repository sync still reads the API container's variables.

**Upgrade in this order**, so delivery never fails in between:

1. Upgrade the API, which adds the route.
2. Store each workspace's credentials with `PUT /integration/github/app-credentials`.
3. Upgrade the workers. From now on they use only the stored credentials.
4. Remove the `FORGE_GITHUB_*` variables and the mounted key from the workers.

A worker upgraded before a workspace has stored its credentials refuses the
environment's App, and that workspace's deliveries fail without a retry.

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
| GitHub App key (agent runs) | Generate a new key at GitHub, `PUT /integration/github/app-credentials` for each workspace that uses the App, then revoke the old key at GitHub. |
| GitHub App (API webhooks and sync) / Slack secrets | Rotate at the provider, update `.env`, recreate dependent services. |

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

Every security control SCF claims is asserted on the **wired request path** by
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

## Browser storage on `localhost`

SCF holds the API key you paste into the Connect dialog in browser storage.
Browser storage is scoped to an **origin** — scheme, host *and* port — and
`http://localhost:8080` is not a private origin. It is the same origin as every
other local application that has ever been served from that port on this
machine, and those applications share one storage bucket.

We saw this concretely during a self-host evaluation: SCF's `localStorage`
already held `app-a-state`, `app-b-lock` and `app-c-store` keys
from unrelated local development. Nothing had gone wrong — that is simply what a
shared origin means. Anything SCF persists client-side is readable by those
apps, and theirs by SCF.

What SCF does about it:

- **The key is held per-tab by default.** The Connect dialog uses
  `sessionStorage`, so the credential dies when the tab closes. "Remember on
  this browser" opts into `localStorage`; the dialog says what that means.
- **Every key SCF writes is namespaced** under `forge.` (`forge.api.token`,
  `forge.board.savedViews`, …), so SCF cannot silently collide with another
  app's state on a shared origin. Namespacing prevents collisions; it does not
  and cannot prevent reads.

What you should do about it:

- **Prefer a real hostname over `localhost`, even locally.** Point a hostname at
  your machine (`forge.localhost`, an `/etc/hosts` entry, or a LAN name), set
  `DOMAIN`, and browse that. A distinct origin is a distinct storage bucket.
- **Do not tick "remember" for an admin key** on a machine that runs other local
  web apps. A tab-scoped token is a much smaller window.
- **Mint per-person, least-privilege keys** for anything beyond evaluation, and
  revoke the shared bootstrap key (`DELETE /auth/api-keys/{id}`).

This is a property of browsers, not of SCF's deployment posture: the same
reasoning applies to any app you run on `localhost`.

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

SCF ships safe-by-default controls. Operators should understand them rather
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

By default SCF executes a task's verification/build commands as host
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
- **Secrets never enter a container sandbox.** The BYOK model key and GitHub App
  credentials are never placed in a container sandbox's environment or any
  persisted `sandbox_instance` row. The `worktree` tier is no sandbox: its
  commands inherit the worker's environment and can reach the worker's secrets.
  It drops only the `FORGE_GITHUB_*` variables, as defence in depth.
- **No silent downgrade.** If `container` is requested but the daemon/proxy is
  unreachable, the run fails loudly — SCF never quietly executes untrusted code
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
  gates the same conditions before `up`. SCF never quietly executes untrusted
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

## First administrator: a sign-in link from the CLI

A fresh installation has no identity provider yet, so nobody can sign in. On the
API server, print a sign-in link for the first administrator:

```bash
scf-cli admin sign-in-link --workspace <slug> --email <address>
```

The command prints one link and the time it expires. Open the link in a browser:
it sets the same session cookie an SSO sign-in sets (12 hours) and redirects to
the app.

The link is built from `FORGE_PUBLIC_URL`: the address at which the browser
reaches the API. The Docker Compose stacks do not set it for the API, so pass it
to the command, or it prints a `http://localhost:8000` link that does not work.
Behind the edge proxy the value must include the `/api` prefix the proxy serves:

```bash
# Full stack: the edge proxy serves the API under /api, so the value ends in /api.
docker compose -f deploy/docker-compose.yml exec -e FORGE_PUBLIC_URL=https://<host>/api \
  api scf-cli admin sign-in-link --workspace <slug> --email <address>

# Single-user profile: no edge proxy, the API is at http://127.0.0.1:8100 (no /api).
docker compose -f deploy/docker-compose.single-user.yml exec -e FORGE_PUBLIC_URL=http://127.0.0.1:8100 \
  api scf-cli admin sign-in-link --workspace <slug> --email <address>
```

- **Who becomes an administrator.** When the workspace has no administrator, the
  command creates one with the given email. Otherwise the email must name an
  existing, active administrator of that workspace. Anything else is refused and
  nothing is created.
- **Lifetime.** `FORGE_SIGN_IN_LINK_TTL_SECONDS`, default `900` (15 minutes). A
  value above `3600` (one hour) is refused when the settings load.
- **Single use.** A link works once. A second use, an expired link and an unknown
  one get the same `401` and no cookie. Printing a new link for the same person
  makes their earlier unused link stop working.
- **Only without an identity provider.** The command refuses a workspace with an
  enabled OIDC or SAML configuration, and a link printed before one was enabled
  is refused when opened. Sign in through the identity provider instead.
- **What is stored.** Only a SHA-256 hash of the link's 32 random bytes. The link
  is not an API key: sent as `Authorization: Bearer` or `X-API-Key`, it gets
  `401`.
- **Audit.** Printing a link records `auth.sign_in_link_issued` (with
  `user_created` when it created the administrator) and opening it records
  `auth.sign_in_link_redeemed`, each with the workspace and the person. A link
  that is opened in time but refused (the person was deactivated or demoted, or
  an identity provider was enabled since) records `auth.sign_in_link_refused`
  with the reason, and stays used.
  The link's token is in no audit entry and in no log line SCF writes; the API
  server's access log keeps the address with the token removed. The redirect
  carries `Referrer-Policy: no-referrer` and `Cache-Control: no-store`.

> **Warning.** Anyone who can run this command on the server can become an
> administrator of any workspace that has no identity provider. Give shell
> access to the API server only to people you trust with that. Treat the
> printed link as a password until it is used or expires: do not paste it into
> chat or tickets. A reverse proxy in front of SCF may log the full link in its
> own access log; the link is useless once used or expired.

## Personal access tokens for desktop clients and the CLI

Give a desktop client (tervin), the ACP shim and your scripts a personal access
token, never an administrator's key. A person mints a token for themselves with
`POST /auth/personal-tokens`, from a browser session or their own unscoped key; a
token cannot mint a token. A token is bound to the person, names its surface
(`tervin` or `cli`), expires (`expires_in_days`, default 90, at most 365) and
carries one of three fixed scope sets:

| Scope set | Request body | What the token can do |
|---|---|---|
| `read` | `"decide": false` (the default) | Read what a workspace viewer reads. |
| `read`, `approvals:decide` | `"decide": true` | Also decide approvals, as its owner with the owner's current role. |
| `read`, `runs:execute` | `"start_runs": true` | Also what the agent-runner role may do, such as starting and forking agent runs (`POST /agent/runs`), simulating policy, calling the read tools of a connected MCP server, and proposing world-model facts (recorded as an agent's). It cannot record a world-model decision, which would be stored as the person's own. |

```bash
curl -fsS -X POST https://<host>/api/auth/personal-tokens \
  -H "Authorization: Bearer $YOUR_KEY" -H 'Content-Type: application/json' \
  -d '{"name": "laptop runner", "surface": "cli", "start_runs": true}'
```

- **No token both starts runs and decides approvals.** A request with both
  `decide` and `start_runs` is refused (`422`), and the key store refuses that
  scope set. A token that starts runs is refused on
  `POST /approvals/{id}/decision`, so one leaked token cannot approve its own
  work.
- **No token administers the workspace.** No token holds `write` or an
  administrator's permissions: none changes the board, opens an approval, or
  manages keys, secrets, members, teams or grants. Team and project access
  (F30) see every token as a workspace viewer.
- **A token is never more than its owner.** A deciding token decides with its
  owner's current role. A token that starts runs is minted only when both the
  credential that mints it and its owner's current role may run agents (an
  administrator or a member; a viewer may not). It is checked again on every
  call: once its owner is demoted to viewer, deactivated or removed, the token
  gets `401` at its next call.
- **Listing.** `GET /auth/api-keys` (administrators) shows each key's `scopes`
  (`null` for an unscoped key) and never the token. Revoke a token with
  `DELETE /auth/api-keys/{id}`.

**What a desktop client should hold.** A read token, with `approvals:decide` only
if the person answers approvals from that client. Give a process that runs agents
on the machine (the ACP shim) its own token with `runs:execute`, in its own
environment variable, apart from the deciding token: an agent that can read its
token can start runs with it but cannot approve them.

**Read the token from a named environment variable.** A client stores no secret:
it reads the token from an environment variable whose name it is given, never
from a command-line argument or a file it writes. `scf-cli board`, `specs`,
`approvals` and `runs` read `FORGE_TOKEN`, or the variable `--token-env <NAME>`
names (for example `TERVIN_FORGE_TOKEN`), and never print it.

### Device sign-in: a token for the CLI without pasting a key

A client with no browser and no stored secret gets a token by showing you a short
code to approve (the device authorization flow of RFC 8628):

```bash
export FORGE_URL=https://<host>/api
eval "$(scf-cli login --token-env FORGE_TOKEN)"
```

`scf-cli login` prints the code and where to approve it to standard error, waits,
and prints only `export FORGE_TOKEN=<token>` to standard output. The token lives in
that shell's environment; the command writes it nowhere. The approve address it
prints is on the server it reached (`FORGE_URL` or `--url`). It keeps polling
through a dropped connection, a `429` (after its `Retry-After`) or a `5xx`, until
the server gives a final answer or the code expires.

While signed in, first see who asked: `POST /auth/device/preview` with the same body
answers the client name, the address and user agent the request came from, and
when. Then approve the code with your browser session or your own unscoped key
(there is no web page for it yet), or deny it with `"decision": "deny"`:

```bash
curl -fsS -X POST https://<host>/api/auth/device/approve \
  -H "Authorization: Bearer $YOUR_KEY" -H 'Content-Type: application/json' \
  -d '{"user_code": "WDJB-MJHT"}'
```

- **What the client gets.** One personal access token with the run-start scope
  set (`read`, `runs:execute`), bound to the person who approved, named after the
  client (`--client-name`, default `scf-cli`), on surface `cli`. It is a
  personal token like any other: it cannot decide approvals, it is checked
  against its owner's current role on every call, and you revoke it with
  `DELETE /auth/api-keys/{id}`.
- **Who can approve.** Only a person: a session or their own unscoped key; no
  token approves. Approving needs `run_agent` in both that credential's role
  and the person's current role (an administrator or a member); any person with
  a current role may deny. A denial is final. The token is minted when the client collects
  it, and refused (`access_denied`) if the person may no longer run agents by
  then.
- **Lifetimes.** A request can be approved and its token collected for 10
  minutes. The client polls no more than once every 5 seconds; a faster poll is
  answered `slow_down`. The token expires after 24 hours: run `scf-cli login`
  again. There are no refresh tokens.
- **Once.** The token is returned to the first poll after approval; every later
  poll gets `invalid_grant`. A retry with an `Idempotency-Key` is not replayed.
- **Guessing.** The user code is 8 letters from 20 consonants (20^8, about
  2.6 × 10^10 codes), unique among pending requests. Its first half names the
  request; 5 wrong codes that name a request end it, whether sent to approve or to
  preview. An unknown code and a wrong one get the same answer, and only a
  signed-in person can try codes, at the per-caller rate limit (default 120 a
  minute per API process): in a request's 10 minutes one credential tries at most
  1,200 codes per process, which hit any of 100 pending requests with a chance of
  about 5 in a million. The device code is 32 random bytes.
- **Flooding.** The two routes that take no credential, `POST /auth/device/code`
  and `POST /auth/device/token`, are rate-limited by the client address the API
  sees, whatever headers a caller sends. One address may hold at most 20 pending
  requests; a further one gets `429` and is not stored. Behind a proxy that the
  API server does not trust to name the client (uvicorn trusts forwarded headers
  only from `127.0.0.1` by default), every client has the proxy's address and
  shares that budget and that cap.
- **What is stored.** Only SHA-256 hashes of the device code and the user code,
  the user code's first half, the client name, and the address and user agent
  the request came from; never the token. Neither code
  nor the token is in any audit entry or log line SCF writes; both travel only in
  request and response bodies, which are marked `Cache-Control: no-store`.
- **Audit.** When a person decides, the workspace records
  `auth.device_code_requested` and `auth.device_code_approved` or
  `auth.device_code_denied`; when the client collects its token,
  `auth.device_token_issued` (with the token's key id, or `result: denied`).
  Each names the client and the address and user agent it asked from, and has
  the deciding person as the actor. A request nobody decides belongs to no
  workspace yet, so it is only in the API log, by id and client name. Requests
  that ended more than a day ago are deleted. The token is marked taken before
  it is minted, so a failure part-way costs the client an `invalid_grant`, never
  a second token; a token minted just before such a failure exists without its
  `auth.device_token_issued` entry and shows in `GET /auth/api-keys`.
- **Approve only a code your own client printed.** Whoever started the request
  gets the token. Someone who sends you a code and gets you to approve it can
  start runs as you until you revoke the token. The preview's address and time
  help you check, but the client name is whatever the requester chose.
- **Other clients.** The routes follow RFC 8628 with JSON bodies:
  `POST /auth/device/code` with `{"client_name": ...}` returns `device_code`,
  `user_code`, `verification_uri` (built from `FORGE_PUBLIC_URL`, as for a
  [sign-in link](#first-administrator-a-sign-in-link-from-the-cli), so wrong
  where that is unset; `scf-cli login` does not use it),
  `expires_in`, `interval` and `scope`; `POST /auth/device/token` with
  `{"device_code": ...}` returns `access_token`, or `400` with `error`
  `authorization_pending`, `slow_down`, `access_denied`, `expired_token` or
  `invalid_grant`.

## Run tokens: a run process reaches only its own run record

A run process that executes one agent run in its own container or Job never
holds the database credential (spec 011, FR-011). It reaches its run record
through the API with a **run token**: a token that belongs to exactly one run of
one workspace and expires. The worker issues one with
`scf_api.auth.run_token.issue_run_token` when it launches the run's unit, hands it
to that unit alone as `FORGE_RUN_TOKEN`, and revokes it with `revoke_run_token` at
close-out (see [reliability.md](reliability.md#run-executor)). How a token reaches a
Kubernetes pod is still open (FR-013).

| Route (run token only) | What it does |
|---|---|
| `GET /agent/runs/{run_id}/record/inputs` | The run's stored objective. |
| `POST /agent/runs/{run_id}/record/events` | Appends one event (`type`, `summary`, `payload`). SCF numbers it, redacts and truncates its text as the worker does, and sets its provenance (`enforcement`, `observed_by`, `enforceable`); the caller's claims are ignored. A type outside the event vocabulary gets `422`. A retry with the same `Idempotency-Key` stores one event. |
| `POST /agent/runs/{run_id}/record/handoff` | Records the run's result for close-out, once: a second attempt gets `409` and the first is kept. |

- **Its own run, nothing else.** These routes accept only the live run token of
  the run in the path, read from `Authorization: Bearer`. Every other
  credential gets `401`: API keys of every role, agents' tokens, personal tokens
  of every scope set, sessions, a token in `X-API-Key` or the session cookie, and
  another run's token, whether or not that run exists. A run token is not an API
  key: every other route that reads a credential refuses it with `401`, a
  socket closes `1008`, and a public route answers it as it answers no
  credential.
- **Short-lived.** It lives as long as its run may: the wall-clock limit
  (`FORGE_RUN_TIME_LIMIT`) plus the reconciler's grace
  (`FORGE_RUN_RECONCILE_GRACE_SECONDS`), or `FORGE_RUN_TOKEN_TTL_SECONDS` when
  that is shorter. It is refused once it expires, is revoked, is replaced by a
  newer token of the same run, or once its run is closed out.
- **Never written down.** The plaintext is returned once. The run keeps only its
  SHA-256 and expiry (`agent_run.output["run_token"]`), which close-out removes.
  Text in events and logs is scrubbed of anything shaped like a run token
  (`scf_run_…`), such as one an agent read from its environment.
- **Audited.** Issuing a token (`run_token.issued`) and recording a handoff
  (`agent_run.handoff_recorded`) each write one audit entry naming the
  workspace, the run and the token's expiry.
- **What it is not.** The run process is SCF's own code, trusted like the
  worker. The token limits what a leaked credential can do (one run's record,
  for at most one run's lifetime); it is not a sandbox boundary, and it does not
  protect a run's record from the code running in that run.

## Enterprise SSO — SAML + SCIM (F33)

- **SAML 2.0 (SCF as SP).** Per-workspace federation with a corporate IdP
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
- [ ] The seed's bootstrap admin key is revoked once per-person keys exist.
- [ ] Desktop clients hold [personal access tokens](#personal-access-tokens-for-desktop-clients-and-the-cli), never an admin key, and a token that starts runs is kept apart from one that decides.
- [ ] Shell access to the API server is limited to people trusted to become an administrator (see [the sign-in link](#first-administrator-a-sign-in-link-from-the-cli)).
- [ ] Operators browse a real hostname, not `localhost` (see [browser storage](#browser-storage-on-localhost)).

## Related

- [docker-compose.md](docker-compose.md) — deployment-level hardening.
- [backup.md](backup.md) — protecting backup credentials.
- [troubleshooting.md](troubleshooting.md) — auth and certificate failures.
