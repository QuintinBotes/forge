# Kubernetes deployment

Forge ships a production Helm chart at [`../../deploy/helm/forge`](../../deploy/helm/forge)
that installs a horizontally-scalable, highly-available Forge onto any conformant
Kubernetes >= 1.28 cluster with one command. This is the recommended path for
50+ engineers who need auto-scaling, high availability and cloud-native
operations that Docker Compose cannot provide. For smaller single-node
deployments, use [docker-compose.md](docker-compose.md).

## Status

**Supported.** The chart is mechanically verified in CI (`helm lint`,
`helm template` against three value profiles, `values.schema.json` validation,
`kubeconform` schema conformance, a `helm-unittest` suite, and a `kind`-based
smoke install that reaches `/readyz`).

## What the chart deploys

Four stateless workloads, each with multiple replicas, a `HorizontalPodAutoscaler`,
a `PodDisruptionBudget`, liveness/readiness probes, a hardened `securityContext`
(non-root, read-only root filesystem, all capabilities dropped,
`seccompProfile: RuntimeDefault`) and CPU/memory requests + limits:

| Workload | Image | Probe |
|---|---|---|
| `api` | `ghcr.io/forge-platform/forge-api` | readiness `GET /readyz`, liveness `GET /healthz` |
| `worker` | `ghcr.io/forge-platform/forge-worker` | liveness `celery -A forge_worker inspect ping` |
| `web` | `ghcr.io/forge-platform/forge-web` | liveness/readiness `GET /healthz` |
| `mcp-gateway` | `ghcr.io/forge-platform/forge-mcp-gateway` | liveness/readiness `GET /health` |

Datastores run in **bundled mode** (pinned `pgvector`/`redis`/`minio` subcharts
with PVCs) for evaluation, or **external/managed mode** (managed Postgres with
the `vector` extension, managed Redis, S3) for production.

## Prerequisites

- Kubernetes >= 1.28 and `kubectl` configured for the cluster.
- `helm` >= 3.14.
- An ingress controller (nginx-ingress assumed) for `Ingress`/TLS.
- A NetworkPolicy-enforcing CNI (Calico/Cilium) for network segmentation.
- (Recommended) cert-manager for automatic TLS.
- (Optional) the KEDA operator for Redis-queue-length worker autoscaling.
- (Production) managed Postgres with `CREATE EXTENSION vector`, managed Redis,
  and an S3 bucket.

## Quick evaluation install (bundled datastores)

```bash
helm dependency build deploy/helm/forge          # pulls pinned subcharts
kubectl create namespace forge
helm install forge deploy/helm/forge -n forge \
  --set forge.domain=forge.example.com \
  --set ingress.tls.certManager.enabled=true \
  --set-string secrets.data.SECRET_KEY="$(openssl rand -hex 32)" \
  --set-string secrets.data.AUTH_SECRET="$(openssl rand -hex 32)" \
  --set-string secrets.data.API_KEY_PEPPER="$(openssl rand -hex 32)" \
  --set-string secrets.data.INTERNAL_SERVICE_TOKEN="$(openssl rand -hex 32)" \
  --set-string secrets.data.FORGE_MODEL_API_KEY="$MODEL_KEY"   # optional (see below)
kubectl -n forge rollout status deploy/forge-api
```

The BYOK model key is **optional**: `forge.modelProvider` (default `anthropic`)
sets the master switch `FORGE_MODEL_PROVIDER`, and with
`secrets.data.FORGE_MODEL_API_KEY` blank the worker runs the offline
deterministic scripted model (canned output) and logs a warning each run. Set the
key — and `forge.modelName` (required for `openai`) — to drive a real provider.
`ANTHROPIC_API_KEY` / `OPENAI_API_KEY` take precedence over `FORGE_MODEL_API_KEY`
if you set them instead.

The pre-install hook runs the Alembic chain (`alembic -c packages/db/alembic.ini
upgrade head`, the same command as `make migrate`) to completion before any app
pod serves traffic; readiness is gated on `/readyz`. Override `migrations.command`
to drop in a different entrypoint (e.g. a future `forge-cli db migrate`) without
re-templating.

> **Bundled subchart images.** The bundled `postgresql`/`redis`/`minio`
> subcharts are Bitnami charts. Bitnami withdrew its versioned public Docker Hub
> images in its 2025 catalog deprecation, so a bundled install may need the image
> overridden to the retained `docker.io/bitnamilegacy/*` repo (e.g.
> `--set postgresql.image.repository=bitnamilegacy/postgresql`), or — recommended
> — use managed/external datastores (below). The local kind smoke
> (`deploy/helm/forge/tests/e2e/`) runs the **external** path against in-cluster
> official `pgvector`/`redis` images for exactly this reason.

## Production install (managed datastores)

Provision managed Postgres (with `vector`), Redis and an S3 bucket, store every
secret in an operator-managed `Secret` (e.g. via External Secrets Operator /
sealed-secrets / Vault CSI), then:

```bash
helm install forge deploy/helm/forge -n forge -f values-production.yaml
```

[`../../deploy/helm/forge/values-production.yaml`](../../deploy/helm/forge/values-production.yaml)
sets `postgresql.enabled=false`, `redis.enabled=false`, `minio.enabled=false`,
points `externalDatabase`/`externalRedis`/`externalObjectStore` at the managed
endpoints, sets `secrets.existingSecret`, and pins every image by `@sha256`
digest. No bundled stateful pods or PVCs are rendered.

## Values reference

The full, documented values surface lives in
[`../../deploy/helm/forge/values.yaml`](../../deploy/helm/forge/values.yaml) and is
validated by [`../../deploy/helm/forge/values.schema.json`](../../deploy/helm/forge/values.schema.json).
Key blocks:

| Block | Purpose |
|---|---|
| `forge.domain` / `forge.publicUrl` | Public DNS name + base URL. |
| `image.registry` / per-workload `image.{repository,tag,digest}` | Image refs (digest-pin in production). |
| `api`/`worker`/`web`/`mcpGateway` | Replicas, resources, probes, `autoscaling`, `pdb`. |
| `worker.keda` | Redis-list autoscaling for the Celery worker. |
| `secrets.create` / `secrets.existingSecret` / `secrets.data` | In-chart Secret (dev) vs operator-managed Secret (prod). |
| `postgresql` / `externalDatabase` | Bundled XOR external Postgres. |
| `redis` / `externalRedis` | Bundled XOR external Redis. |
| `minio` / `externalObjectStore` | Bundled XOR external object store. |
| `ingress` | Host, class, TLS (cert-manager or `secretName`). |
| `networkPolicy.enabled` | Data-tier segmentation. |
| `migrations` / `createAdmin` | Migration hook + opt-in first-admin hook. |
| `metrics.serviceMonitor.enabled` | Prometheus Operator scraping. |
| `sandbox` | F34 kernel-boundary isolation: workspace minimum `kind`, runtime names, `RuntimeClass` renderings (below). |

## Stronger sandbox isolation (gVisor / Kata-Firecracker) — F34

The chart can render Kubernetes `RuntimeClass` objects — the K8s analogue of a
Docker host's `daemon.json` `runtimes` block — for the two kernel-boundary
sandbox tiers:

```yaml
sandbox:
  kind: gvisor            # workspace MINIMUM: worktree | container | gvisor | microvm
  runtimeClasses:
    gvisor:
      enabled: true       # renders RuntimeClass forge-gvisor (handler: runsc)
    kataFc:
      enabled: true       # renders RuntimeClass forge-kata-fc (handler: kata-fc)
```

- The node pools referenced by the `RuntimeClass` `scheduling` block
  (`forge.dev/sandbox-runtime: gvisor|kata-fc` by default) must have the runtime
  installed on their container runtime (runsc; Kata + Firecracker with
  `/dev/kvm` exposed for `kata-fc`). Keep them tainted and separate from
  control-plane workloads.
- `sandbox.kind` (plus `gvisorRuntime`/`gvisorPlatform`/`microvmRuntime`/
  `requireKvm`/`jailerRoot`) sets the worker's `FORGE_SANDBOX_*` env — the
  workspace minimum a repo policy may strengthen but never weaken. A missing
  runtime fails loudly (never a silent downgrade); see
  [security.md](security.md#kernel-boundary-sandboxing-f34).
- The per-task **launch path** on Kubernetes still goes through the Docker
  sandbox substrate; a native per-task Pod launcher with `runtimeClassName` is
  tracked with the Kubernetes sandbox work (F34 §12).

## Secrets and the BYOK vault

No secret value is ever rendered into a `ConfigMap`. The boot-critical keys
(`SECRET_KEY`, `AUTH_SECRET`, `FORGE_VAULT_KEYS`, `API_KEY_PEPPER`,
`INTERNAL_SERVICE_TOKEN`) are required — the chart fails fast at template time if
any is missing, mirroring the application's fail-closed startup. The BYOK model
key (`FORGE_MODEL_API_KEY`) is **not** boot-critical: the worker/api boot without
it and fall back to the offline scripted model, so it is optional. `FORGE_VAULT_KEYS`
is the AES-256-GCM envelope-vault master key (KEK)
and is distinct from `SECRET_KEY`; store it in a managed secret store and rotate
it with `forge-cli secrets rotate-key`. A Postgres restore of encrypted rows is
useless without the same `FORGE_VAULT_KEYS` material. See
[security.md](security.md) for the full posture.

## Ingress, TLS and the GitHub webhook

The `Ingress` routes `/api`, `/healthz`, `/readyz`, `/integrations/github/webhook`
and WebSocket/SSE paths to `forge-api:8000`, and everything else to
`forge-web:3000`. The webhook route preserves the **raw request body** for HMAC
verification (`proxy-request-buffering: "off"`, no body rewrite). TLS is provided
by cert-manager (`ingress.tls.certManager.enabled=true`) or an existing TLS
secret (`ingress.tls.secretName`).

## Scaling and high availability

Each request-serving tier has a CPU `HorizontalPodAutoscaler`; the worker can use
a KEDA `ScaledObject` on Redis list length (`worker.keda.enabled=true`).
`PodDisruptionBudget`s plus pod anti-affinity keep the service up during node
drains and rolling upgrades (`maxUnavailable: 0`).

```bash
kubectl get hpa -n forge
```

## Upgrade and rollback

```bash
helm upgrade forge deploy/helm/forge -n forge -f values-production.yaml
```

`helm upgrade` runs the **pre-upgrade** Alembic migration hook first; only
after it succeeds do the workloads roll. If the hook fails, the release does not
roll — run `helm rollback forge <rev>`. Verify and bump image digests against the
release notes before upgrading; see [upgrade.md](upgrade.md).

> **Values rename (pre-1.0):** the dead `MODEL_PROVIDER` / `secrets.data.MODEL_PROVIDER_KEY`
> values were replaced by `forge.modelProvider` (→ `FORGE_MODEL_PROVIDER`) and
> `secrets.data.FORGE_MODEL_API_KEY`. Rename any `--set secrets.data.MODEL_PROVIDER_KEY=…`
> to `FORGE_MODEL_API_KEY` before upgrading; a blank key falls back to the offline
> scripted model (see the BYOK note above).

## Verification

```bash
helm test forge -n forge        # curls forge-api:8000/readyz from in-cluster
kubectl get pods -n forge
```

## Backup and restore

Cluster backup/restore that preserves the (bundled or external) Postgres —
including the immutable `audit_log` and the encrypted `api_key` rows — is covered
in [backup.md](backup.md) and [restore.md](restore.md). The KEK
(`FORGE_VAULT_KEYS`) must be backed up alongside the database.

## Troubleshooting

See [troubleshooting.md](troubleshooting.md). Common Kubernetes-specific issues:

- **Pods `CrashLoopBackOff` immediately**: a boot-critical secret is missing from
  `secrets.existingSecret`. Confirm all keys listed above are present.
- **`/readyz` never passes**: Postgres/Redis/S3 unreachable — check
  `externalDatabase`/`externalRedis`/`externalObjectStore` and NetworkPolicy.
- **NetworkPolicies have no effect**: the cluster CNI does not enforce them
  (install Calico/Cilium).
