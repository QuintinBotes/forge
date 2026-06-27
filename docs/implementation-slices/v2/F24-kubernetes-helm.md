# F24 — Kubernetes Helm Chart

> Phase: v2 · Spec module(s): Self-Hosting and Deployment (FORGE_SPEC.md §"Three Install Paths" → Kubernetes row; §"Required Self-Hosting Documentation" → `kubernetes.md`), Monorepo `deploy/helm/`, Phase 2 roadmap "Kubernetes Helm chart", Security (network segmentation, non-root, secrets isolation), Observability (probes/metrics) · Status target: **Done** = a single Helm chart at `deploy/helm/forge` installs a horizontally-scalable, highly-available Forge onto any conformant Kubernetes ≥ 1.28 cluster with one command (`helm install forge deploy/helm/forge -f my-values.yaml`); the four stateless workloads (`api`, `worker`, `web`, `mcp-gateway`) each render with multiple replicas, a `HorizontalPodAutoscaler`, a `PodDisruptionBudget`, liveness/readiness probes wired to the F14/C01 health-endpoint contract, a hardened `securityContext` (non-root, read-only rootfs, all caps dropped, `seccompProfile: RuntimeDefault`), and CPU/memory requests+limits; a pre-install/pre-upgrade Helm hook `Job` runs `forge-cli db migrate` to completion before any app pod serves traffic; datastores work in **both** bundled mode (pinned `pgvector`/`redis`/`minio` subcharts with PVCs) and **external/managed mode** (managed Postgres+pgvector / Redis / S3 via `existingSecret`, with no bundled stateful pods rendered); ingress + TLS are provided via an `Ingress` (cert-manager or existing TLS secret) that preserves the GitHub-webhook raw body and passes through WebSocket/SSE; `NetworkPolicy` resources reproduce the compose network segmentation (data tier reachable only from `api`/`worker`); and the whole chart is mechanically verified in CI: `helm lint`, `helm template` against three value profiles, `values.schema.json` validation, `kubeconform` schema conformance, a `helm-unittest` suite, and a `kind`-based smoke install that reaches `/readyz`. No secret value is ever rendered into a `ConfigMap`.

---

## 1. Intent — what & why

FORGE_SPEC.md defines **three install paths** (§"Self-Hosting and Deployment"): Local dev (Docker Desktop), Docker Compose (1–50 engineers, single node), and **Kubernetes (50+ engineers, K8s cluster, ingress, managed DB, "High" complexity)**. The research report is explicit about *why* Kubernetes is a distinct path and not just "compose on a cluster": Docker Compose has no native auto-scaling, no high availability, and no cross-host container restart, so teams that need those properties must escalate to Kubernetes, and the standard production path for HA/scaling/cloud-native ops is a **Helm chart** (research report §"Self-Hosting and Deployment" [cite:120][cite:121]; Technology Recommendations table "Kubernetes deployment → Helm chart [cite:84]"). The monorepo layout reserves `deploy/helm/` for exactly this, the Phase 2 roadmap lists "Kubernetes Helm chart," and `docs/self-hosting/kubernetes.md` (shipped as a *preview pointer* by V1's F15) must be upgraded to document a real, supported chart.

This slice owns the **production Helm chart** (`deploy/helm/forge`) plus its values schema, its rendering/validation/unit/integration test harness, and the rewrite of `docs/self-hosting/kubernetes.md` from "preview" to "supported." It deliberately does **not** author application source code, Dockerfiles, the FastAPI health endpoints, or the `forge-cli` subcommands — those are owned by F14 (image build/hardening) and C01/auth (CLI + endpoints). The chart *consumes* the same container images F14 publishes (`ghcr.io/forge-platform/forge-{api,worker,web,mcp-gateway}`, digest-pinned) and the same env-var contract as the compose stack, then re-expresses the compose hardening invariants (digest pins, non-root, network segmentation, healthchecks, resource limits) as Kubernetes primitives while **adding the three things Compose cannot do**:

1. **Horizontal scaling** — `HorizontalPodAutoscaler` (CPU/memory) for the request-serving tiers and an optional **KEDA** `ScaledObject` (Redis queue-length) for the Celery `worker`.
2. **High availability** — multiple replicas, `PodDisruptionBudget`, pod anti-affinity, and rolling updates with surge control, so node drains and upgrades never take the service fully down.
3. **Cloud-native operations** — `Ingress` + cert-manager TLS (replacing Caddy auto-HTTPS), externalizable managed datastores, `ServiceMonitor` for Prometheus, and migrations run as a guaranteed pre-upgrade hook.

Why a single chart (not an umbrella of per-service charts): Forge ships as one product with one version; co-versioning the workloads, the migration hook, and the shared config/secret in one chart keeps `helm upgrade` atomic and the version-to-image mapping unambiguous. Bundled datastores are conditional **subchart dependencies** (Bitnami `postgresql`/`redis`/`minio`) so a self-contained "kick the tyres on a cluster" install works, while real production points at managed Postgres/Redis/S3.

---

## 2. User-facing behavior / journeys

The "user" is a **platform/SRE engineer** running Forge on Kubernetes for 50+ engineers — not an end user of the board.

**J1 — Self-contained evaluation install (bundled datastores, ~10 min).**
```bash
helm repo add forge https://charts.forge-platform.dev   # or: use the in-repo path deploy/helm/forge
helm dependency build deploy/helm/forge                  # pulls pinned postgresql/redis/minio subcharts
kubectl create namespace forge
helm install forge deploy/helm/forge -n forge \
  --set forge.domain=forge.example.com \
  --set ingress.tls.certManager.enabled=true \
  --set-string secrets.create=true \
  --set secrets.data.SECRET_KEY="$(openssl rand -hex 32)" \
  --set secrets.data.MODEL_PROVIDER_KEY="$MODEL_KEY"
# Helm runs the migration hook Job, then api/worker/web/mcp-gateway roll out.
kubectl -n forge rollout status deploy/forge-api
```
The pre-install hook `forge-db-migrate` completes, app pods become Ready (readiness gated on `/readyz`), the operator opens `https://forge.example.com`, cert-manager has issued a cert, and signs in. A post-install `createAdmin` hook (opt-in) seeds the first admin.

**J2 — Production install with managed datastores (recommended).** Operator provisions managed Postgres (with the `vector` extension enabled), managed Redis, and an S3 bucket, stores their credentials in a pre-created `Secret` (`forge-external`), then:
```bash
helm install forge deploy/helm/forge -n forge -f values-production.yaml
```
where `values-production.yaml` sets `postgresql.enabled=false`, `redis.enabled=false`, `minio.enabled=false`, points `externalDatabase`/`externalRedis`/`externalObjectStore` at the managed endpoints, and sets `secrets.existingSecret=forge-external`. **No bundled stateful pods or PVCs are created.** The chart asserts (via a fail-fast template guard) that exactly one of `enabled`/`external*` is configured for each datastore.

**J3 — Scale under load (automatic).** Traffic rises; the `api` HPA scales `forge-api` from `minReplicas` to `maxReplicas` on CPU; the agent queue backs up and (if KEDA is enabled) the `worker` `ScaledObject` scales `forge-worker` on Redis list length, scaling back to `minReplicas` (or zero, if `worker.keda.minReplicaCount=0`) when the queue drains. The operator observes this in `kubectl get hpa -n forge`.

**J4 — Zero-downtime upgrade.** `helm upgrade forge deploy/helm/forge -n forge -f values-production.yaml` (new image digests in `values`). Helm runs the **pre-upgrade** `forge-db-migrate` hook first; only after it succeeds do the workloads roll (RollingUpdate, `maxUnavailable: 0`), with the `PodDisruptionBudget` protecting in-flight requests. If the hook fails, the release does not roll and the operator runs `helm rollback`.

**J5 — Node drain / HA validation.** `kubectl drain <node>` evicts pods; the `PodDisruptionBudget` keeps `minAvailable` replicas of each tier serving; anti-affinity has already spread replicas across nodes/zones, so the board stays up.

**J6 — Operator reads the docs.** `docs/self-hosting/kubernetes.md` (rewritten here) walks J1–J5: prerequisites, `helm install`, the full `values` reference, external Postgres/Redis/S3, ingress & TLS, verification, and upgrade/rollback — matching the heading contract F15 froze.

---

## 3. Vertical slice

### 3.1 Data model (tables/columns/migrations touched)

**N/A — this is a deployment/packaging slice; it introduces no tables, columns, or migrations.** It *invokes* the existing migration chain via a Kubernetes `Job` running `forge-cli db migrate` (the Alembic chain at `packages/db/alembic.ini` is owned by C01 + each feature slice) and *operates on* datastores at the cluster/PVC/external-endpoint level. The only "schemas" this slice owns are YAML/JSON: the Helm `values` contract (`values.schema.json`, §4.1) and the rendered Kubernetes manifests.

One **requirement** placed on the database (not owned here): the `vector` (pgvector) extension must be available — the bundled subchart uses a `pgvector`-enabled Postgres image, and `kubernetes.md` instructs managed-Postgres operators to `CREATE EXTENSION vector` (the extension *creation* lives in F05's migration, the *availability* is a chart/runbook concern).

### 3.2 Backend (FastAPI routes + services/packages)

This slice implements **no** business routes. It **depends on and re-uses the health-endpoint contract frozen by F14 §3.2** to wire Kubernetes probes, and the `forge-cli` contract to run hooks:

| Workload | Probe | Endpoint | Semantics (owned by C01/app slices; consumed here) |
|---|---|---|---|
| `api` | readiness | `GET /readyz` | `200` only when Postgres+Redis+MinIO/S3 reachable; gates Service endpoints + rollout. |
| `api` | liveness | `GET /healthz` | process-up; restarts a wedged pod. |
| `api` | startup | `GET /healthz` | longer `failureThreshold` to cover cold start before liveness applies. |
| `mcp-gateway` | readiness/liveness | `GET /healthz` | process-up (gateway is mostly egress; readiness = serving). |
| `web` | readiness/liveness | `GET /healthz` | Next.js route handler `200`. |
| `worker` | liveness | exec `celery -A forge_worker.app inspect ping` | no HTTP port; broker-ping liveness (mirrors F14 §3.3). No readiness (not in a Service). |

`forge-cli` subcommands invoked by chart hooks (contract-frozen by F14/C01, used here): `forge-cli db migrate` (pre-install/pre-upgrade hook), `forge-cli db current` (verification), `forge-cli users create-admin --email … --password-stdin` (opt-in post-install hook). The chart does **not** implement these; it renders Jobs that call them and fails the release if they are missing.

### 3.3 Worker / agent runtime (Celery tasks, LangGraph, if any)

No Celery tasks or LangGraph graphs are authored here. The chart **packages and scales** the `worker` Deployment:
- **Scaling**: default CPU-based `HorizontalPodAutoscaler`; optional **KEDA** `ScaledObject` (`worker.keda.enabled`) that scales on Redis list length (`worker.keda.listName`, `lagThreshold`) — the correct signal for a Celery/Redis queue, since the worker has no Service and HPA cannot read queue depth without a metrics adapter. KEDA is the recommended production path; CPU-HPA is the no-extra-operator fallback.
- **Graceful drain**: `terminationGracePeriodSeconds` is set generously (default 600s) and a `preStop` hook (`celery -A forge_worker.app control shutdown` / warm shutdown) lets in-flight agent runs finish before SIGTERM; `PodDisruptionBudget` prevents mass eviction. (Long agent runs surviving eviction fully is a durable-execution concern owned by the Temporal slice; this slice provides best-effort warm shutdown only — see §12.)
- The worker mounts the same shared **repos volume** as `api` only if `worktree` sandboxing requires shared state; default V2 posture (per spec, container sandboxing arrives in V2) is **per-pod ephemeral `emptyDir`** for worktrees (no `ReadWriteMany` PVC required), documented as a values toggle `worker.repoStorage`.

### 3.4 Frontend / UI (Next.js routes/components, if any)

No product UI. The chart templates the `web` Deployment/Service/HPA/PDB and wires its `/healthz` probe (route handler `apps/web/app/healthz/route.ts`, owned by the `cross-cutting/C01-monorepo-and-api-foundations` "web foundation" scaffold). The only UI-adjacent contract is that the `Ingress` routes the SPA and API correctly (see §3.5). Note: `apps/web` does not yet exist in the repo (`apps/` currently holds `api`, `worker`, `mcp-gateway`); the chart's `web` workload is **gated by `web.enabled`** and ships enabled-by-spec but will no-op cleanly if the image is absent — documented so the chart can land before/after the web app.

### 3.5 Infra / deploy (compose, helm, caddy, if any)

**This is the entire slice.** Deliverables live under `deploy/helm/forge/` (a standard Helm v3 chart) plus the rewritten K8s doc and a CI workflow.

**Chart metadata & dependencies**
- `Chart.yaml` — `apiVersion: v2`, `type: application`, `version` (chart SemVer), `appVersion` (Forge release), and **conditional subchart `dependencies`**: `postgresql` (Bitnami, pinned, `condition: postgresql.enabled`), `redis` (Bitnami, pinned, `condition: redis.enabled`), `minio` (Bitnami, pinned, `condition: minio.enabled`). Optional `keda` is **not** a subchart (operator must be cluster-installed); the chart only renders a `ScaledObject` when `worker.keda.enabled`.
- `Chart.lock` — committed; locks subchart digests/versions for reproducible `helm dependency build`.
- The bundled Postgres subchart is configured to use a **pgvector-enabled image** (override `image.repository`/`tag`/`digest` to `pgvector/pgvector`), since stock Bitnami Postgres lacks the extension.

**values files**
- `values.yaml` — documented defaults (bundled datastores ON for a working out-of-box install; production overrides turn them off).
- `values-production.yaml` — managed-datastore profile (bundled OFF, `existingSecret`, HA replica counts, HPA on, NetworkPolicy on, cert-manager TLS).
- `values.example.yaml` — minimal install used by `helm template`/CI and referenced by `kubernetes.md`.
- `values.schema.json` — JSON Schema validating the values surface (§4.1); `helm install` enforces it automatically.

**templates/** (each stateless workload follows the same pattern)
```
deploy/helm/forge/
├── Chart.yaml
├── Chart.lock
├── values.yaml
├── values-production.yaml
├── values.example.yaml
├── values.schema.json
├── README.md                         # helm-docs generated reference
├── templates/
│   ├── _helpers.tpl                  # fullname, common labels, selector labels, image ref, env builders
│   ├── NOTES.txt                     # post-install URL + verification hints
│   ├── serviceaccount.yaml
│   ├── configmap-env.yaml            # NON-secret env only (ConfigMap): FORGE_PUBLIC_URL, LOG_LEVEL,
│   │                                 #   FORGE_VAULT_ACTIVE_KEY_VERSION, GITHUB_APP_ID/SLUG/CLIENT_ID,
│   │                                 #   JINA_RERANKER_URL, RERANK_ENABLED, MINIO_ENDPOINT, derived URLs…
│   ├── secret.yaml                   # rendered ONLY when secrets.create=true; else references existingSecret
│   ├── api/{deployment,service,hpa,pdb,networkpolicy}.yaml
│   ├── worker/{deployment,hpa,pdb,networkpolicy,scaledobject}.yaml
│   ├── web/{deployment,service,hpa,pdb,networkpolicy}.yaml
│   ├── mcp-gateway/{deployment,service,hpa,pdb,networkpolicy}.yaml
│   ├── reranker/{deployment,service}.yaml   # rendered only when reranker.enabled (self-hosted Jina); probe GET /health:8080, sets JINA_RERANKER_URL
│   ├── ingress.yaml
│   ├── job-migrate.yaml              # Helm hook: pre-install,pre-upgrade (forge-cli db migrate)
│   ├── job-create-admin.yaml         # Helm hook: post-install (opt-in)
│   ├── networkpolicy-datastores.yaml # data-tier ingress allowlist (bundled mode)
│   ├── servicemonitor.yaml           # rendered only when metrics.serviceMonitor.enabled
│   └── tests/
│       └── test-connection.yaml      # `helm test` pod: curl api /readyz
└── tests/                            # helm-unittest specs (see §7)
```

**Hardening expressed as K8s primitives (mirrors F14 compose invariants):**

| Compose invariant (F14) | Kubernetes expression (this slice) |
|---|---|
| Digest-pinned images | `image.repository@sha256:…` built by `_helpers.tpl` from `{repository,tag,digest}` values; tag-only fails a chart test. |
| Non-root, drop caps, no-new-priv | `podSecurityContext.runAsNonRoot: true`, `runAsUser`/`fsGroup` ≥ 10000; `containerSecurityContext`: `allowPrivilegeEscalation: false`, `capabilities.drop: [ALL]`, `readOnlyRootFilesystem: true`, `seccompProfile.type: RuntimeDefault`; writable scratch via `emptyDir`. |
| Healthcheck per service | liveness/readiness/startup probes (§3.2). |
| CPU/memory limits | `resources.requests`+`resources.limits` per container (required by schema). |
| Network segmentation (`data` internal) | `NetworkPolicy` per workload: default-deny ingress; datastore policy allows ingress only from `api`/`worker` pods; ingress controller → `web`/`api` only. |
| Log caps | N/A at chart level (node-level logging/`kubelet` owns rotation); documented in `kubernetes.md`. |
| autoheal sidecar | N/A — replaced by kubelet liveness-probe restarts + `restartPolicy: Always` (Kubernetes-native self-healing). |
| Caddy auto-HTTPS | `Ingress` + cert-manager (`cert-manager.io/cluster-issuer` annotation) or operator-supplied TLS secret. |
| `--remove-orphans` | N/A — `helm upgrade` prunes removed resources natively. |

**Ingress routing contract** (`ingress.yaml`): host `${forge.domain}`; path `/api`, `/healthz`, `/readyz`, `/integrations/github/webhook`, and WS/SSE upgrade paths → `forge-api:8000`; everything else → `forge-web:3000`. The GitHub webhook path must preserve the **raw request body** for HMAC (F03): for nginx-ingress, set `nginx.ingress.kubernetes.io/proxy-request-buffering: "off"` and **no** body rewrite; a chart test asserts no body-mutating annotation on the webhook path. WebSocket/SSE: nginx-ingress passes these through by default; the chart sets generous `proxy-read-timeout` for SSE run-trace streams. Gateway API (`HTTPRoute`) is offered behind `ingress.api: gateway` as an alternative renderer (out of scope to fully verify in V2 → see §12; default is `ingress.api: ingress`).

**Reverse proxy**: **N/A — Caddy/Nginx static configs are the compose path (F14).** On Kubernetes, the ingress controller (operator-installed; nginx-ingress assumed) replaces Caddy; the chart only emits `Ingress`/`HTTPRoute`, not a proxy config.

---

## 4. Public interfaces / contracts (exact signatures, Pydantic models, Protocols, YAML schemas)

The "interfaces" of a Helm chart are (a) the `values` schema, (b) the rendered-resource invariants, (c) the labeling/naming convention other tooling keys on, and (d) the hook contract. All are mechanically asserted (§7).

### 4.1 `values.schema.json` — top-level values contract (abridged; full schema is the deliverable)

```jsonc
{
  "$schema": "https://json-schema.org/draft-07/schema#",
  "type": "object",
  "required": ["forge", "image", "api", "worker", "web", "mcpGateway", "ingress"],
  "properties": {
    "global": {
      "type": "object",
      "properties": {
        "imageRegistry": {"type": "string"},
        "imagePullSecrets": {"type": "array", "items": {"type": "object"}},
        "storageClass": {"type": "string"}
      }
    },
    "forge": {
      "type": "object",
      "required": ["domain"],
      "properties": {
        "domain":     {"type": "string", "minLength": 3},
        "publicUrl":  {"type": "string"},          // default https://{domain}
        "logLevel":   {"enum": ["debug","info","warning","error"]},
        "modelProvider": {"type": "string"}
      }
    },
    "image": {
      "type": "object",
      "required": ["pullPolicy"],
      "properties": {"registry": {"type": "string"}, "pullPolicy": {"enum": ["IfNotPresent","Always"]}}
    },
    // ── per-workload block schema, applied to api/worker/web/mcpGateway ──
    "$defs": {
      "workload": {
        "type": "object",
        "required": ["image", "replicaCount", "resources"],
        "properties": {
          "enabled": {"type": "boolean"},
          "image": {
            "type": "object", "required": ["repository","tag"],
            "properties": {
              "repository": {"type": "string"},
              "tag":        {"type": "string"},
              "digest":     {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"}  // required in prod profile
            }
          },
          "replicaCount": {"type": "integer", "minimum": 1},
          "resources": {
            "type": "object", "required": ["requests","limits"],
            "properties": {
              "requests": {"type":"object","required":["cpu","memory"]},
              "limits":   {"type":"object","required":["cpu","memory"]}
            }
          },
          "autoscaling": {
            "type": "object",
            "properties": {
              "enabled": {"type":"boolean"},
              "minReplicas": {"type":"integer","minimum":1},
              "maxReplicas": {"type":"integer","minimum":1},
              "targetCPUUtilizationPercentage": {"type":"integer","minimum":1,"maximum":100}
            }
          },
          "pdb": {"type":"object","properties":{"enabled":{"type":"boolean"},"minAvailable":{}}},
          "nodeSelector": {"type":"object"},
          "tolerations": {"type":"array"},
          "affinity": {"type":"object"},
          "podAnnotations": {"type":"object"}
        }
      }
    },
    "api":        {"$ref": "#/$defs/workload"},
    "web":        {"$ref": "#/$defs/workload"},
    "mcpGateway": {"$ref": "#/$defs/workload"},
    "worker": {
      "allOf": [{"$ref": "#/$defs/workload"}],
      "properties": {
        "keda": {
          "type":"object",
          "properties": {
            "enabled": {"type":"boolean"},
            "listName": {"type":"string"},
            "lagThreshold": {"type":"integer","minimum":1},
            "minReplicaCount": {"type":"integer","minimum":0},
            "maxReplicaCount": {"type":"integer","minimum":1}
          }
        }
      }
    },
    // ── secrets ──
    "secrets": {
      "type": "object",
      "properties": {
        "create":         {"type": "boolean"},
        "existingSecret": {"type": "string"},
        "data": {
          "type": "object",
          // Boot-critical (api/worker fail closed without these — F37 §AC20): SECRET_KEY,
          // AUTH_SECRET, FORGE_VAULT_KEYS, API_KEY_PEPPER, INTERNAL_SERVICE_TOKEN, MODEL_PROVIDER_KEY.
          // FORGE_VAULT_ACTIVE_KEY_VERSION is non-secret and lives in configmap-env (not here).
          "properties": {
            "SECRET_KEY": {"type":"string"}, "AUTH_SECRET": {"type":"string"},
            "FORGE_VAULT_KEYS": {"type":"string"},          // versioned KEK map "1:<b64-32B>[,2:<b64-32B>]" (BYOK vault master key)
            "API_KEY_PEPPER": {"type":"string"}, "INTERNAL_SERVICE_TOKEN": {"type":"string"},
            "POSTGRES_PASSWORD": {"type":"string"}, "REDIS_PASSWORD": {"type":"string"},
            "MINIO_ROOT_PASSWORD": {"type":"string"}, "MODEL_PROVIDER_KEY": {"type":"string"},
            "GITHUB_APP_PRIVATE_KEY": {"type":"string"},    // PEM; chart mounts it as a file and sets GITHUB_APP_PRIVATE_KEY_PATH
            "GITHUB_APP_WEBHOOK_SECRET": {"type":"string"}, "GITHUB_APP_CLIENT_SECRET": {"type":"string"},
            "SLACK_SIGNING_SECRET": {"type":"string"}, "SLACK_BOT_TOKEN": {"type":"string"}
          }
        }
      }
    },
    // ── datastores: bundled XOR external (enforced by template guard, §4.3) ──
    "postgresql": {"type":"object","properties":{"enabled":{"type":"boolean"}}},
    "externalDatabase": {
      "type":"object",
      "properties":{"host":{"type":"string"},"port":{"type":"integer"},"user":{"type":"string"},
                    "database":{"type":"string"},"existingSecret":{"type":"string"},"passwordKey":{"type":"string"}}
    },
    "redis": {"type":"object","properties":{"enabled":{"type":"boolean"}}},
    "externalRedis": {"type":"object","properties":{"host":{"type":"string"},"port":{"type":"integer"},
                      "existingSecret":{"type":"string"},"passwordKey":{"type":"string"}}},
    "minio": {"type":"object","properties":{"enabled":{"type":"boolean"}}},
    "externalObjectStore": {"type":"object","properties":{"endpoint":{"type":"string"},"bucket":{"type":"string"},
                            "region":{"type":"string"},"existingSecret":{"type":"string"}}},
    "reranker": {"type":"object","properties":{"enabled":{"type":"boolean"},
                 "external":{"type":"object","properties":{"url":{"type":"string"}}}}},
    "ingress": {
      "type":"object","required":["enabled"],
      "properties": {
        "enabled": {"type":"boolean"},
        "api": {"enum":["ingress","gateway"]},
        "className": {"type":"string"},
        "annotations": {"type":"object"},
        "tls": {"type":"object","properties":{
          "enabled":{"type":"boolean"},"secretName":{"type":"string"},
          "certManager":{"type":"object","properties":{"enabled":{"type":"boolean"},"clusterIssuer":{"type":"string"}}}}}
      }
    },
    "networkPolicy": {"type":"object","properties":{"enabled":{"type":"boolean"}}},
    "migrations":   {"type":"object","properties":{"enabled":{"type":"boolean"},"backoffLimit":{"type":"integer"}}},
    "createAdmin":  {"type":"object","properties":{"enabled":{"type":"boolean"},"email":{"type":"string","format":"email"}}},
    "serviceAccount": {"type":"object","properties":{"create":{"type":"boolean"},"name":{"type":"string"},"annotations":{"type":"object"}}},
    "metrics": {"type":"object","properties":{"serviceMonitor":{"type":"object","properties":{"enabled":{"type":"boolean"}}}}}
  }
}
```

### 4.2 Rendered-resource invariants (asserted by helm-unittest + kubeconform + a Python contract test over `helm template` output)

For **every** stateless workload (`forge-api`, `forge-worker`, `forge-web`, `forge-mcp-gateway`):
1. `image` is `…@sha256:<64-hex>` when a `digest` value is set (required in `values-production.yaml`).
2. `securityContext` sets `runAsNonRoot: true`, `allowPrivilegeEscalation: false`, `readOnlyRootFilesystem: true`, `capabilities.drop:[ALL]`, `seccompProfile.type: RuntimeDefault`.
3. `resources.requests.{cpu,memory}` and `resources.limits.{cpu,memory}` are all present.
4. liveness + readiness probes exist (worker: liveness only; see §3.2).
5. A `HorizontalPodAutoscaler` (or worker `ScaledObject`) and a `PodDisruptionBudget` exist when scaling is enabled, and `minReplicas`/`replicaCount` ≥ 2 in the production profile.
6. Pod anti-affinity (`preferredDuringScheduling…` by hostname) is present.
7. Standard recommended labels on every object: `app.kubernetes.io/{name,instance,version,component,part-of=forge,managed-by=Helm}`; selector labels are the immutable subset (`name`,`instance`,`component`).
8. Each pod mounts env from the `ConfigMap` (non-secret) and the `Secret` (secret) via `envFrom`; **no secret key appears in the ConfigMap** (set-disjointness asserted).

### 4.3 Template guards (fail-fast `{{ fail }}` / `required`)

- Each datastore: exactly one of `<store>.enabled: true` **or** a populated `external<Store>` block — otherwise `fail "Configure either bundled or external <store>, not both/neither"`.
- `secrets.create: false` requires a non-empty `secrets.existingSecret` (`required`), else fail.
- `secrets.create: true` requires the **boot-critical** keys non-empty — `SECRET_KEY`, `AUTH_SECRET`, `FORGE_VAULT_KEYS`, `API_KEY_PEPPER`, `INTERNAL_SERVICE_TOKEN`, `MODEL_PROVIDER_KEY` — else `fail "secrets.data.<KEY> is required (api/worker fail closed without it — F37)"`. This mirrors F37's fail-closed startup so a missing KEK is caught at `helm template`/`install` time, not at a CrashLoopBackOff. (When `existingSecret` is used, the same keys are documented as required; presence is the operator's responsibility and is verified by the kind smoke install reaching `/readyz`, AC18.)
- `ingress.tls.enabled: true` requires either `certManager.enabled: true` **or** a non-empty `tls.secretName`.
- `forge.domain` is `required`.
- Production profile (`values-production.yaml`) sets `image.digest` for all four workloads; a chart test asserts the prod profile renders only digest-pinned images.

### 4.4 Helm hook contract (`job-migrate.yaml`, `job-create-admin.yaml`)

```yaml
# job-migrate.yaml
metadata:
  annotations:
    "helm.sh/hook": pre-install,pre-upgrade
    "helm.sh/hook-weight": "-5"
    "helm.sh/hook-delete-policy": before-hook-creation,hook-succeeded
spec:
  backoffLimit: {{ .Values.migrations.backoffLimit | default 3 }}
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: migrate
          image: {{ include "forge.image" (dict "ctx" . "wl" .Values.api) }}   # api image carries forge-cli
          command: ["forge-cli","db","migrate"]
          envFrom: [{configMapRef: {name: {{ include "forge.fullname" . }}-env}},
                    {secretRef:    {name: {{ include "forge.secretName" . }}}}]
          securityContext: { runAsNonRoot: true, readOnlyRootFilesystem: true, allowPrivilegeEscalation: false }
```
`job-create-admin.yaml` is `post-install` only (never on upgrade), gated by `createAdmin.enabled`, reads the admin password from the secret (`--password-stdin`), and is `hook-delete-policy: hook-succeeded`.

---

## 5. Dependencies — features/slices that must exist first

> Numbering note (same caveat the other slices carry): early slices were authored independently; reference by `<phase>/<id>-<slug>` and match by slug if numbers differ. **Slug reconciliation** (matching the convention F15 froze): the platform scaffold is a cross-cutting Phase-0 prerequisite with no dedicated numbered file yet — sibling slices name it variously as `cross-cutting/C01-monorepo-and-api-foundations`, `cross-cutting/F00-platform-foundation`, and `v1/F00-foundation-substrate`; all denote the same substrate (the `web` route handler lives in this same "web foundation" scaffold). The authoritative auth/secrets/RBAC slug is **`cross-cutting/F37-auth-secrets-byok`** (it supersedes the stale `v1/auth-secrets-rbac` / `cross-cutting/C02-auth-and-rbac` references seen in older siblings), the audit log is **`cross-cutting/F39-audit-log`**, observability is **`cross-cutting/F38-observability-cost-metrics`**, and Temporal is **`v2/F25-temporal-integration`**. All other refs match real files under `docs/implementation-slices/`.

**Hard prerequisites (the chart cannot install/verify without these):**
- **`cross-cutting/C01-monorepo-and-api-foundations`** (a.k.a. `v1/F00-foundation-substrate`; REQUIRED) — the `apps/{api,worker,mcp-gateway}` (and, when present, `web`) skeletons, the `forge-cli` entrypoint (`db migrate`, `db current`; the `db` group is owned here), the Celery app `forge_worker.app`, the api `GET /healthz`+`/readyz` and web `GET /healthz` handlers, and the Alembic chain (`packages/db/alembic.ini`). The migration hook and probes bind to these exact names.
- **`v1/F14-docker-compose-selfhost`** (REQUIRED) — owns the **published, hardened, digest-pinned container images** (`ghcr.io/forge-platform/forge-{api,worker,web,mcp-gateway}`, the exact refs in F14's service table §3.1) and the **env-var contract** the chart reuses (`SECRET_KEY`, `AUTH_SECRET`, `FORGE_VAULT_KEYS`, `FORGE_VAULT_ACTIVE_KEY_VERSION`, `API_KEY_PEPPER`, `INTERNAL_SERVICE_TOKEN`, `DATABASE_URL`/`POSTGRES_*`, `REDIS_URL`/`REDIS_PASSWORD`, `MINIO_*`, `MODEL_PROVIDER_KEY`, `GITHUB_APP_*` (private key delivered as a file via `GITHUB_APP_PRIVATE_KEY_PATH`), `SLACK_*`, `JINA_RERANKER_URL`, `FORGE_PUBLIC_URL`, `LOG_LEVEL`). The chart does not build images; it references F14's. The compose hardening invariants (F14 §4.1) are the source of the K8s hardening table (§3.5), and F14 §3.2 freezes the probe contract.
- **Health-endpoint providers** (REQUIRED for probes; contract frozen in F14 §3.2): `api` `/healthz`+`/readyz` (readyz = `200` only when Postgres+Redis+MinIO reachable, else `503 {"status":"not_ready","failed":[...]}`), `mcp-gateway` `/healthz`, `web` `/healthz`, worker broker-ping (`celery -A forge_worker.app inspect ping`). The chart wires probes against them.
- **`cross-cutting/F37-auth-secrets-byok`** (REQUIRED for boot + full runbook) — provides `forge-cli users create-admin` (post-install hook) and the **AES-256-GCM envelope vault** (BYOK) the workloads consume. The vault master key (KEK) is **env-resident and versioned** (`FORGE_VAULT_KEYS` + `FORGE_VAULT_ACTIVE_KEY_VERSION`), distinct from `SECRET_KEY`; `apps/api`/`apps/worker` **fail closed at startup** if `FORGE_VAULT_KEYS`/`API_KEY_PEPPER`/`AUTH_SECRET` are absent (F37 §AC20), so the chart's Secret MUST carry them (§4.1/§4.3) or the kind smoke install (AC18) never reaches `/readyz`. F37 also owns the four RBAC roles (`admin`/`member`/`viewer`/`agent-runner`) and `forge-cli secrets rotate-key` (KEK rotation, referenced by `upgrade.md`).

**Soft / integration dependencies (chart accommodates them; they need not be complete for the chart to build):**
- **`v1/F05-hybrid-knowledge-retrieval`** (SOFT) — requires pgvector on Postgres (bundled uses `pgvector/pgvector`; managed documented) and the self-hosted **Jina Reranker v2** served over HTTP, consumed via env **`JINA_RERANKER_URL`** (F05's frozen var name; the reranker exposes `GET /health` on port `8080`). The chart templates an optional `reranker` deployment/service (probe `/health:8080`, sets `JINA_RERANKER_URL=http://{fullname}-reranker:8080`) or accepts `reranker.external.url`. F05's `EMBEDDING_*`/`RERANK_ENABLED`/`RETRIEVAL_*`/`RRF_K` non-secret config flow through the image defaults / `configmap-env`.
- **`v1/F09-mcp-gateway-v1`** (SOFT) — runs as the `mcp-gateway` workload composed here; this slice provides the `mcp-egress` NetworkPolicy lane but never overrides F09's read-only-by-default MCP posture (§8 non-negotiables).
- **`cross-cutting/F39-audit-log`** (SOFT) — owns the immutable, queryable `audit_log` (Postgres). The chart neither writes nor truncates it; it lives in the (bundled or external) Postgres and is preserved by the K8s backup procedure documented in `backup.md` (owned by F15; cluster backup/restore is out of scope here, §12). Restated only to confirm the spec non-negotiable survives the K8s deployment surface.
- **`v1/F03-github-app`** (SOFT) — the ingress webhook raw-body rule and the `GITHUB_APP_*` config/secret keys are accommodated (private key mounted as a file from the Secret, `GITHUB_APP_PRIVATE_KEY_PATH` set to the mount); F03 only fills values.
- **`v1/F16-slack-notifications`** (SOFT) — `SLACK_*` secret keys are accommodated.
- **`v2/F25-temporal-integration`** (SOFT/FUTURE) — when Temporal is adopted (V2), a `temporal.enabled` toggle and worker pointing at the Temporal frontend service is added; this slice leaves a values stub but does **not** own Temporal deployment (§12).
- **`cross-cutting/C01-monorepo-and-api-foundations` "web foundation"** (SOFT) — `apps/web` does not yet exist in the repo (`apps/` holds `api`, `worker`, `mcp-gateway`); `apps/web/app/healthz/route.ts` is part of the same foundation scaffold. `web.enabled` gates the workload so the chart can land independently (§3.4).

**Consumers (NOT prerequisites — they consume this slice):**
- **`v1/F15-selfhosting-docs`** — F15 shipped `docs/self-hosting/kubernetes.md` as a *preview pointer* with a `helm-lint` job over whatever chart exists; this slice **provides the real chart** and **rewrites `kubernetes.md`** from "preview" to "supported" (keeping F15's frozen heading contract and `must_reference` strings: `deploy/helm`, `helm install`, `helm upgrade`, plus removing the `preview` banner once supported — coordinated with F15's manifest, see §8/§12).
- **`cross-cutting/F38-observability-cost-metrics`** (V2) — consumes `metrics.serviceMonitor.enabled` to scrape the workloads.

---

## 6. Acceptance criteria (numbered, testable)

Each criterion is checked by a named test in §7. Tiers: **static** (`helm lint`/`template`/schema/kubeconform/helm-unittest, no cluster) and **integration** (`kind`-gated).

1. **Chart lints clean.** `helm lint deploy/helm/forge` and `helm lint deploy/helm/forge -f values-production.yaml` both exit 0 with no errors. *(static)*
2. **Renders on three profiles.** `helm template forge deploy/helm/forge` with each of (defaults, `values.example.yaml`, `values-production.yaml`) exits 0 and produces parseable multi-doc YAML. *(static)*
3. **Values schema enforced.** `values.schema.json` rejects a values file missing `forge.domain` and one with a non-`sha256:` `image.digest`; accepts the three shipped profiles. *(static, helm-unittest + JSON-schema test)*
4. **Digest pinning in prod.** Under `values-production.yaml`, every rendered workload + hook `image:` matches `…@sha256:[0-9a-f]{64}`; a tag-only image fails the test. *(static)*
5. **Hardened securityContext.** Every workload Pod/container sets `runAsNonRoot: true`, `allowPrivilegeEscalation: false`, `readOnlyRootFilesystem: true`, `capabilities.drop:[ALL]`, `seccompProfile.type: RuntimeDefault`. *(static)*
6. **Resources present.** Every workload container declares `resources.requests.{cpu,memory}` and `resources.limits.{cpu,memory}`. *(static)*
7. **Probes wired.** `api`/`web`/`mcp-gateway` render readiness+liveness probes against the §3.2 endpoints; `worker` renders a liveness exec probe (`celery … inspect ping`). *(static)*
8. **HA primitives.** With autoscaling enabled, each stateless tier renders an `HPA` (or worker `ScaledObject` when `worker.keda.enabled`) and a `PodDisruptionBudget`; in the production profile `minReplicas`/`replicaCount` ≥ 2 and anti-affinity is present. *(static)*
9. **Secret/Config separation.** No key present in the rendered `Secret` (or referenced `existingSecret` key set) appears in the `ConfigMap`; with `secrets.create=false` and empty `existingSecret`, rendering **fails** with the guard message. *(static)*
10. **Datastore XOR guard.** Setting both `postgresql.enabled=true` and a populated `externalDatabase`, or neither, fails rendering with the §4.3 message; same for redis and object store. *(static)*
11. **External mode emits no bundled stateful objects.** Under `values-production.yaml` (bundled OFF), the rendered manifest contains **no** `StatefulSet`/`PVC`/bundled-`Service` for postgres/redis/minio, and workload env points at `externalDatabase`/`externalRedis`/`externalObjectStore`. *(static)*
12. **Migration hook ordering.** `job-migrate.yaml` carries `helm.sh/hook: pre-install,pre-upgrade` with a negative `hook-weight`; a render test confirms the hook Job exists and uses the api image + `forge-cli db migrate`. *(static)*
13. **Ingress routing + webhook raw body.** The `Ingress` routes `/api`,`/healthz`,`/readyz`,`/integrations/github/webhook` → `forge-api:8000` and `/`→`forge-web:3000`; the webhook path carries no body-mutating annotation and `proxy-request-buffering: "off"` is set for nginx. TLS renders a cert-manager annotation or `tls.secretName`. *(static)*
14. **NetworkPolicy segmentation.** With `networkPolicy.enabled`, datastore policies allow ingress only from `api`/`worker` selector labels; ingress-controller→`web`/`api` only; a default-deny baseline exists. *(static)*
15. **kubeconform conformance.** `helm template … | kubeconform -strict -kubernetes-version 1.28.0 -schema-location default` exits 0 for all three profiles (every resource is schema-valid). *(static)*
16. **helm-unittest suite green.** `helm unittest deploy/helm/forge` passes all specs in `deploy/helm/forge/tests/`. *(static)*
17. **Recommended labels.** Every rendered object carries the eight `app.kubernetes.io/*` + `part-of: forge` labels; selector labels are the immutable subset. *(static)*
18. **kind smoke install (bundled).** On a `kind` cluster, `helm dependency build` + `helm install` with bundled datastores brings the migration hook to `Completed` and `forge-api`/`forge-web` Deployments to `Available`; `helm test forge` (curl `/readyz`) passes within timeout. *(integration)*
19. **Upgrade runs migration first.** `helm upgrade` on the kind release re-runs the pre-upgrade hook to `Completed` before pods roll; `helm rollback` restores the prior revision. *(integration, may be slow)*
20. **Docs supported & in F15 parity.** `docs/self-hosting/kubernetes.md` satisfies F15's `kubernetes.md` heading + `must_reference` contract, no longer contains the `preview` banner (status: supported), and `helm lint`/`helm template` succeed on the values it documents. *(static; cross-checked by F15's `helm-lint` job)*
21. **Boot-critical secrets present & fail-closed.** The rendered `Secret` (or the documented `existingSecret` key set) carries `SECRET_KEY`, `AUTH_SECRET`, `FORGE_VAULT_KEYS`, `API_KEY_PEPPER`, `INTERNAL_SERVICE_TOKEN`, `MODEL_PROVIDER_KEY`; `FORGE_VAULT_ACTIVE_KEY_VERSION` is rendered in the `ConfigMap` (non-secret); and with `secrets.create=true` but any boot-critical key empty, rendering **fails** with the §4.3 message (mirrors F37 fail-closed). Every workload + the migrate hook reaches these via `envFrom`. *(static)*

---

## 7. Test plan (TDD) — concrete test cases (unit + integration), key fixtures

Write the failing tests first (helm-unittest specs + the Python render-contract test) against an empty `templates/`, then drive each template into existence criterion by criterion. Two tiers run in CI: **static** (no cluster — every PR) and **integration** (`kind`-gated job — required before tagging a chart release).

**Tooling (pinned in the CI workflow):** `helm` ≥ 3.14, the `helm-unittest` plugin, `kubeconform`, `yamllint`, `kind`, and `python` (`pytest`, `ruamel.yaml`) for the render-contract test.

**Static — helm-unittest specs (`deploy/helm/forge/tests/*_test.yaml`)** — one spec file per concern, each `helm unittest`-driven:
- `securitycontext_test.yaml` (AC5) — `asserts` that each Deployment's container `securityContext` matches the hardened set; parametrized over the four workloads + the migrate hook.
- `resources_test.yaml` (AC6) — requests+limits present on every container.
- `probes_test.yaml` (AC7) — readiness/liveness paths equal `/readyz`/`/healthz`; worker exec probe contains `inspect ping`.
- `scaling_test.yaml` (AC8) — HPA present at defaults; `ScaledObject` present when `set: {worker.keda.enabled: true}`; PDB present; prod profile replica ≥ 2; anti-affinity present.
- `secret_config_separation_test.yaml` (AC9, AC21) — Secret keys ∩ ConfigMap keys = ∅; the boot-critical keys (`SECRET_KEY`, `AUTH_SECRET`, `FORGE_VAULT_KEYS`, `API_KEY_PEPPER`, `INTERNAL_SERVICE_TOKEN`, `MODEL_PROVIDER_KEY`) appear in the Secret and `FORGE_VAULT_ACTIVE_KEY_VERSION` in the ConfigMap; `secrets.create=false` + empty `existingSecret` → `failedTemplate`; `secrets.create=true` with an empty `FORGE_VAULT_KEYS` → `failedTemplate` (fail-closed, F37).
- `datastore_guard_test.yaml` (AC10, AC11) — both-on and both-off → failedTemplate; prod profile (external) → `notFailedTemplate` and `hasDocuments: {kind: StatefulSet, count: 0}`.
- `migrate_hook_test.yaml` (AC12) — hook annotations + command `["forge-cli","db","migrate"]`.
- `ingress_test.yaml` (AC13) — path→backend map; webhook path has no `rewrite-target`/body annotation; `proxy-request-buffering: "off"`; cert-manager annotation rendered when enabled.
- `networkpolicy_test.yaml` (AC14) — datastore ingress `from` selectors == api/worker; default-deny present.
- `labels_test.yaml` (AC17) — recommended labels on a representative object of each kind.
- `digest_test.yaml` (AC4) — prod profile: every `image` matches the `@sha256:` regex (helm-unittest `matchRegex`).

**Static — JSON-schema test (`deploy/helm/tests/test_values_schema.py`, pytest)** (AC3):
- `test_schema_rejects_missing_domain`, `test_schema_rejects_bad_digest`, `test_schema_accepts_shipped_profiles` — validate each shipped values file against `values.schema.json` with `jsonschema`.

**Static — render-contract test (`deploy/helm/tests/test_render_contract.py`, pytest)** (AC2, AC4, AC15, AC17, cross-cutting) — shells `helm template` for each profile into `ruamel.yaml`, then asserts the §4.2 invariants programmatically (one place that enforces "every workload" rules even as workloads are added), and pipes the same output to `kubeconform -strict` (AC15). Skips with a clear message if `helm`/`kubeconform` are absent locally (always present in CI).

**Static — lint job** (AC1): `helm lint` on defaults + prod profile; `yamllint` over `templates/` rendered output.

**Integration — kind smoke (`deploy/helm/tests/e2e/test_kind_install.py`, `@pytest.mark.kind`)** (AC18, AC19):
- module-scoped fixture `kind_cluster` creates an ephemeral `kind` cluster (unique name), installs a CSI/`local-path` storage class and (if testing KEDA path) the KEDA operator, loads the locally-built F14 images via `kind load docker-image`, and tears the cluster down in a finalizer.
- `test_bundled_install_reaches_ready` — `helm dependency build`; `helm install` with bundled datastores + `--wait --timeout 15m`; assert migrate Job `Completed`, `forge-api`/`forge-web` `Available`; run `helm test forge` (the `tests/test-connection.yaml` pod curls `http://forge-api:8000/readyz` → 200).
- `test_upgrade_runs_migration_and_rollback` — `helm upgrade` (bump a benign value); assert the pre-upgrade hook Job for the new revision reaches `Completed` and pods roll with no full outage (poll Deployment `availableReplicas` ≥ `minAvailable` throughout); `helm rollback forge 1`; assert healthy.

**Docs check (AC20)** — reuses F15's harness: this slice's change makes F15's `helm-lint` job target the real chart and removes the preview banner; a `test_kubernetes_doc_supported` asserts `kubernetes.md` `## Status` no longer contains `preview` and still contains F15's `must_reference` strings.

**Key fixtures**
- `rendered(profile)` — cached `helm template` output per profile, parsed.
- `workloads` — parametrize list `["forge-api","forge-worker","forge-web","forge-mcp-gateway"]` so "every workload" assertions stay DRY as workloads are added.
- `prod_values` / `example_values` / `default_values` — values-file path fixtures.
- `kind_cluster` (integration) — ephemeral cluster + image loader + teardown.

**CI wiring** (`.github/workflows/helm-chart.yml`): job `helm-static` (lint + unittest + schema + render-contract + kubeconform) on every PR touching `deploy/helm/**`; job `helm-kind` (`@pytest.mark.kind`) on a Docker-enabled runner, required before a chart-version tag (it builds/loads the four F14 images so digests/refs resolve).

---

## 8. Security & policy considerations

- **Secrets never land in a ConfigMap or in `values` committed to git.** The chart renders a `Secret` only when `secrets.create=true` (dev/eval); production uses `secrets.existingSecret` (operator-managed, e.g. via External Secrets Operator / sealed-secrets — referenced, not owned). AC9 mechanically asserts Secret↔ConfigMap key-disjointness. `kubernetes.md` instructs `openssl rand -hex 32` for `SECRET_KEY`/`AUTH_SECRET` and never committing populated values.
- **The BYOK-vault master key (KEK) is `FORGE_VAULT_KEYS`, not `SECRET_KEY`** (corrected against the `cross-cutting/F37-auth-secrets-byok` contract). The vault is AES-256-GCM envelope encryption with a **versioned, env-resident** KEK map (`FORGE_VAULT_KEYS="1:<b64-32B>[,2:…]"` + `FORGE_VAULT_ACTIVE_KEY_VERSION`) and an HKDF-derived per-workspace DEK; `SECRET_KEY` is a separate app signing/session key. On K8s this means a Postgres restore (encrypted `api_key` rows) is useless without the same `FORGE_VAULT_KEYS` material — so `kubernetes.md` documents storing the KEK in a managed secret store (External Secrets Operator / sealed-secrets / Vault CSI as the `existingSecret`), and rotating it via `forge-cli secrets rotate-key` (re-encrypts all rows to the active version, zero plaintext exposure). This is the same posture F37/F14 established, restated for the cluster context. The boot-critical secret set the chart must carry (`SECRET_KEY`, `AUTH_SECRET`, `FORGE_VAULT_KEYS`, `API_KEY_PEPPER`, `INTERNAL_SERVICE_TOKEN`, `MODEL_PROVIDER_KEY`) is guarded at render time (§4.3) because F37 fails closed without it.
- **Network segmentation is enforced by `NetworkPolicy`, not by network topology** (Compose's `internal: true` has no direct K8s analogue). Default-deny ingress baseline + explicit allowlists: datastores accept ingress only from `api`/`worker` pod selectors; only the ingress controller reaches `web`/`api`; `mcp-gateway` is allowed egress (external MCP servers) but ingress only from `api`/`worker`. Requires a CNI that enforces NetworkPolicy (Calico/Cilium) — documented as a prerequisite; the policies render regardless so a conformant CNI applies them.
- **Pod hardening** mirrors F14: `runAsNonRoot`, `readOnlyRootFilesystem` (writable scratch via `emptyDir`), `drop:[ALL]` capabilities, `allowPrivilegeEscalation:false`, `seccompProfile: RuntimeDefault`, and a dedicated least-privilege `ServiceAccount` with **no** Role/RoleBinding by default (the app needs no Kubernetes API access). The chart is compatible with the `restricted` Pod Security Standard (documented; can be label-enforced at the namespace).
- **GitHub webhook raw-body integrity** (F03): the ingress webhook route must not buffer-transform/rewrite the body or HMAC verification breaks — `proxy-request-buffering: "off"`, no `rewrite-target`/body annotation on that path; asserted by `ingress_test.yaml`.
- **TLS by default**: cert-manager `ClusterIssuer` annotation (or operator-supplied TLS secret); HTTP→HTTPS redirect via ingress annotation; HSTS/security headers documented for the chosen controller. No plaintext service is exposed outside the cluster (only the ingress).
- **Supply chain**: images digest-pinned (AC4); `kubernetes.md`/`upgrade.md` document verifying digests against release notes and scanning (`trivy`) before bumping. Subchart versions/digests are locked in `Chart.lock`.
- **No agent-tool calls authored here**, so the runtime `PolicyEvaluator` (F04) is not invoked; this slice's "policy" is the **deployment hardening contract** mechanically enforced by §7 (the K8s analogue of F14's compose contract).
- **Spec non-negotiables — carried vs. delegated** (this slice must not *break* the runtime non-negotiables it does not own):
  - **Hybrid retrieval** (`v1/F05-hybrid-knowledge-retrieval`) — the chart guarantees the *substrate* it needs (pgvector available on bundled `pgvector/pgvector` + documented `CREATE EXTENSION vector` for managed Postgres; optional `reranker` workload or `reranker.external.url` wired via `JINA_RERANKER_URL`); it never alters retrieval logic.
  - **MCP read-only default + RFC 8707 token binding + per-call audit** (`v1/F09-mcp-gateway-v1`) — enforced inside the gateway; this slice only renders the `mcp-gateway` workload and the `mcp-egress` NetworkPolicy lane for its outbound calls, and never overrides the read-only-by-default env. `INTERNAL_SERVICE_TOKEN` (carried in the Secret) authenticates `api`↔`mcp-gateway`.
  - **Immutable, queryable audit log** (`cross-cutting/F39-audit-log`) — lives in Postgres (bundled or external); the chart neither writes nor truncates it. Cluster backup/restore that preserves it is documented in `backup.md`/`restore.md` (F15) and out of scope here (§12).
  - **BYOK** — realized by carrying `MODEL_PROVIDER_KEY` + the per-workspace encrypted vault whose env-resident KEK (`FORGE_VAULT_KEYS`) is delivered via the **Secret only**, never the ConfigMap (AC9).
  - **Spec-gated implementation / human-approval-before-merge** (`v1/F02-spec-engine`, `v1/F07-feature-workflow-fsm`, `v1/F08-plan-execute-verify-pr-approval`, `cross-cutting/F36-human-approval-system`) — workflow concerns with **no deployment surface**; the chart packages the same `api`/`worker` that enforce them and changes nothing about the gates.
- **Hook-Job least privilege**: migrate/create-admin Jobs run with the same hardened `securityContext`, read secrets via `envFrom`/`--password-stdin` (never argv), and are deleted on success (`hook-delete-policy`) so credentials don't linger in completed pods.

---

## 9. Effort estimate & risk (S/M/L + key risks)

**Effort: L.** No product code, but a wide, detail-dense surface: ~20 templates + `_helpers.tpl`, three values profiles + a JSON Schema, three pinned subchart integrations with a pgvector override, KEDA/HPA/PDB/anti-affinity HA wiring, NetworkPolicy segmentation, ingress+TLS+webhook-raw-body, the migration/create-admin hooks, a helm-unittest + JSON-schema + render-contract + kubeconform static suite, and a real `kind`-gated install/upgrade integration test (the most expensive piece). Plus the `kubernetes.md` rewrite coordinated with F15.

**Key risks & mitigations:**
- **Subchart drift / pgvector on bundled Postgres.** Stock Bitnami Postgres lacks `vector`; using a `pgvector` image inside the subchart can break Bitnami init assumptions. *Mitigation:* pin a known-good `pgvector` image + `Chart.lock`; the kind smoke test asserts `CREATE EXTENSION vector` succeeds (via F05's migration) on the bundled DB; document managed-Postgres as the production-preferred path.
- **Health-endpoint / image contract not yet final.** Probes and the migrate hook depend on F14/C01 endpoints and `forge-cli`. *Mitigation:* freeze against F14 §3.2, fail the kind test early with a clear message, and gate `web` behind `web.enabled` so the chart lands before `apps/web` exists.
- **KEDA / NetworkPolicy require cluster operators/CNI not present everywhere.** *Mitigation:* KEDA is opt-in (CPU-HPA fallback always renders); NetworkPolicy renders but is a no-op without an enforcing CNI — both documented as prerequisites, neither blocks a vanilla install.
- **kind integration flakiness** (image loads, slow rollouts, cert-manager in-cluster). *Mitigation:* `--wait --timeout`, `tls.secretName` self-signed for the e2e (skip real ACME), generous probe `startupProbe`, mark the job slow/required-only-at-release, retries on image pulls.
- **Gateway API vs Ingress fragmentation.** *Mitigation:* default to classic `Ingress` (broadest support); render `HTTPRoute` only behind `ingress.api: gateway` and mark it experimental (§12) — only the `Ingress` path is verified in V2.
- **Long-running agent jobs vs pod eviction.** True durability is Temporal's job (V2). *Mitigation:* best-effort warm shutdown (`preStop` + long grace) + PDB; documented limitation, not solved here.
- **Docs parity coupling with F15.** Removing the preview banner must stay in sync with F15's manifest. *Mitigation:* coordinate the `must_reference`/status change in the same PR; F15's `helm-lint` job now targets the real chart.

---

## 10. Key files / paths (exact)

```
deploy/helm/forge/Chart.yaml                          # apiVersion v2, app+chart version, conditional subchart deps
deploy/helm/forge/Chart.lock                          # locked subchart versions/digests
deploy/helm/forge/values.yaml                         # documented defaults (bundled datastores ON)
deploy/helm/forge/values-production.yaml              # managed-datastore HA profile (bundled OFF, digests required)
deploy/helm/forge/values.example.yaml                 # minimal install used by CI + kubernetes.md
deploy/helm/forge/values.schema.json                  # §4.1 values contract
deploy/helm/forge/README.md                           # helm-docs generated values reference
deploy/helm/forge/templates/_helpers.tpl              # fullname/labels/selector/image-ref/env builders
deploy/helm/forge/templates/NOTES.txt
deploy/helm/forge/templates/serviceaccount.yaml
deploy/helm/forge/templates/configmap-env.yaml        # NON-secret env only
deploy/helm/forge/templates/secret.yaml               # only when secrets.create=true
deploy/helm/forge/templates/api/{deployment,service,hpa,pdb,networkpolicy}.yaml
deploy/helm/forge/templates/worker/{deployment,hpa,pdb,networkpolicy,scaledobject}.yaml
deploy/helm/forge/templates/web/{deployment,service,hpa,pdb,networkpolicy}.yaml
deploy/helm/forge/templates/mcp-gateway/{deployment,service,hpa,pdb,networkpolicy}.yaml
deploy/helm/forge/templates/reranker/{deployment,service}.yaml   # when reranker.enabled
deploy/helm/forge/templates/ingress.yaml
deploy/helm/forge/templates/networkpolicy-datastores.yaml
deploy/helm/forge/templates/servicemonitor.yaml       # when metrics.serviceMonitor.enabled
deploy/helm/forge/templates/job-migrate.yaml          # pre-install,pre-upgrade hook
deploy/helm/forge/templates/job-create-admin.yaml     # post-install hook (opt-in)
deploy/helm/forge/templates/tests/test-connection.yaml # `helm test` pod (curl /readyz)

deploy/helm/forge/tests/securitycontext_test.yaml     # helm-unittest specs (AC5)
deploy/helm/forge/tests/resources_test.yaml           # AC6
deploy/helm/forge/tests/probes_test.yaml              # AC7
deploy/helm/forge/tests/scaling_test.yaml             # AC8
deploy/helm/forge/tests/secret_config_separation_test.yaml  # AC9
deploy/helm/forge/tests/datastore_guard_test.yaml     # AC10, AC11
deploy/helm/forge/tests/migrate_hook_test.yaml        # AC12
deploy/helm/forge/tests/ingress_test.yaml             # AC13
deploy/helm/forge/tests/networkpolicy_test.yaml       # AC14
deploy/helm/forge/tests/labels_test.yaml              # AC17
deploy/helm/forge/tests/digest_test.yaml              # AC4

deploy/helm/tests/test_values_schema.py               # AC3 (jsonschema)
deploy/helm/tests/test_render_contract.py             # AC2,4,15,17 (helm template + kubeconform)
deploy/helm/tests/e2e/test_kind_install.py            # AC18,19 (@pytest.mark.kind)
deploy/helm/tests/conftest.py                         # rendered()/workloads/values fixtures/kind_cluster

.github/workflows/helm-chart.yml                      # jobs: helm-static, helm-kind

docs/self-hosting/kubernetes.md                        # rewritten: preview -> supported (F15 heading/ref contract)
```

Contract-dependency artifacts owned by other slices but referenced here (not authored): the four `ghcr.io/forge-platform/forge-{api,worker,web,mcp-gateway}@sha256:…` images (F14 §3.1), `apps/api` `/healthz`+`/readyz` and `apps/mcp-gateway`/`apps/web` `/healthz` (contract frozen F14 §3.2; handlers in C01 "foundation" + F09), the `forge-cli db migrate`/`db current` entrypoint (C01), `forge-cli users create-admin`/`secrets rotate-key` + the `FORGE_VAULT_KEYS` BYOK vault (`cross-cutting/F37-auth-secrets-byok`), the Alembic chain (`packages/db/alembic.ini`), F05's `CREATE EXTENSION vector` migration + self-hosted Jina reranker (`JINA_RERANKER_URL`), and the immutable `audit_log` in Postgres (`cross-cutting/F39-audit-log`).

---

## 11. Research references (relevant links from the spec/research report)

- **Three install paths (Kubernetes = 50+ engineers, ingress, managed DB, "High" complexity)** and the **Required Self-Hosting Documentation** `kubernetes.md` line: FORGE_SPEC.md §"Self-Hosting and Deployment".
- **Monorepo `deploy/helm/` reservation** and **Phase 2 roadmap "Kubernetes Helm chart"**: FORGE_SPEC.md §"Monorepo Structure" + §"Phased Roadmap → Phase 2".
- **Compose→Kubernetes escalation rationale** (no native auto-scaling/HA/cross-host restart in Compose; Kubernetes is the correct escalation path) and **"Kubernetes deployment → Helm chart, standard production path for HA/scaling/cloud-native ops"**: forge-research-report.md §"Self-Hosting and Deployment" [cite:120][cite:121] and §"Technology Recommendations" table [cite:84].
- **Full-stack template ships Kubernetes manifests + Redis/rate-limiting/Prometheus production preset** (precedent for the chart's scope): forge-research-report.md §"Self-Hosting and Deployment" [cite:133]; https://github.com/vstorm-co/full-stack-ai-agent-template .
- **Production Docker Compose Requirements** (source of the hardening invariants re-expressed as K8s primitives — digest pins, non-root, network segmentation, healthchecks, resource limits): FORGE_SPEC.md §"Production Docker Compose Requirements"; the compose realization is F14 (`docs/implementation-slices/v1/F14-docker-compose-selfhost.md`).
- **Security posture inputs** (secrets at rest / BYOK vault keyed by `SECRET_KEY`, RBAC roles, MCP read-only default, secret redaction): FORGE_SPEC.md §Security + §"MCP Security Rules".
- **Datastores**: pgvector (must be available on bundled + managed Postgres) https://github.com/pgvector/pgvector ; MinIO / S3-compatible object storage https://min.io/ ; Jina Reranker v2 (optional self-hosted reranker workload) https://jina.ai/reranker/ .
- **Local Kubernetes guide** (used for `kubernetes.md` + the kind smoke test): https://www.plural.sh/blog/local-kubernetes-guide/ .
- **External canonical refs needed at implementation time** (not in spec, standard tooling): Helm v3 charts & hooks https://helm.sh/docs/ ; Bitnami subcharts (postgresql/redis/minio) https://github.com/bitnami/charts ; KEDA Redis-list scaler https://keda.sh/docs/latest/scalers/redis-lists/ ; cert-manager https://cert-manager.io/docs/ ; kubeconform https://github.com/yannh/kubeconform ; helm-unittest https://github.com/helm-unittest/helm-unittest ; kind https://kind.sigs.k8s.io/ ; Kubernetes recommended labels https://kubernetes.io/docs/concepts/overview/working-with-objects/common-labels/ .

---

## 12. Out of scope / future

- **Authoring the container images / Dockerfiles / `forge-cli` / health endpoints** — owned by F14 and C01/auth; this chart consumes them.
- **Temporal workflow engine on Kubernetes** — the `v2/F25-temporal-integration` slice owns Temporal deployment; this chart leaves a `temporal.enabled` values stub and a worker-target hook only (durable long-run survival across evictions is Temporal's job, not best-effort `preStop`).
- **Observability stack (Prometheus/Grafana/Loki) deployment** — this chart only emits an opt-in `ServiceMonitor`; deploying the monitoring stack is the observability slice / operator's responsibility.
- **Gateway API (`HTTPRoute`) as the verified default** — rendered behind `ingress.api: gateway` but experimental in V2; only classic `Ingress` is CI-verified.
- **GitOps packaging** (Argo CD `Application` / Flux `HelmRelease` manifests), an OCI/`helm repo` publish pipeline, and chart signing/provenance (`helm provenance`, cosign) — future infra slices.
- **External Secrets Operator / sealed-secrets / Vault CSI integration** — the chart supports `existingSecret` (the integration point) but does not own the secret-sync operator config.
- **Cloud-provider Terraform modules / managed-datastore provisioning** (RDS+pgvector, ElastiCache, S3 buckets) — the chart consumes managed endpoints via `external*`; provisioning them is out of scope.
- **Multi-cluster / multi-region / service-mesh (Istio/Linkerd) topologies** — beyond V2 Helm scope.
- **GPU scheduling for a bundled reranker** — `reranker.enabled` renders a CPU deployment; GPU node-selectors/tolerations and model-server tuning are future work (managed/external reranker URL is the default).
