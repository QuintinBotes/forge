# HARD-08 — Kubernetes Helm Deploy (lint / template / install on local k8s + smoke)

> Phase: hardening · Blocker(s): **#3** (the deploy artifact — images & install — was never built/run for real; images not `@sha256`-pinned) and **#6** (maturity: no real deploy/install verification, no k8s install/upgrade test) · Status target: **verified** = a real Helm chart exists at `deploy/helm/forge`, `helm lint` + `helm template` + `values.schema.json` + `kubeconform` + a `helm-unittest` static suite are green on **every** PR (no cluster, network-free, part of the whole-suite gate), **and** a `kind`/`k3d`-gated integration lane installs the chart against locally-built (or digest-pinned) Forge images, brings the Alembic migration hook to `Completed`, rolls `api`/`worker`/`mcp-gateway`/`web` to `Available`, and a `helm test` pod proves the deployed stack answers `GET /health → 200` end-to-end. **No external/BYOK creds are required** — the smoke uses generated dev secrets; it does need Docker + a local k8s and the built images, so it runs on a networked CI runner, **not** in the no-network sandbox. The `@sha256` digest-pinning half (PRODUCTION gate `G-IMG-PINNED`) is asserted statically against the production values profile.

---

> **Numbering / consistency note.** The hardening spec (`scratchpad/hardening-docs/SPEC-PRODUCTION-HARDENING.md`) lists a workstream titled *"Container & web build, image digest pinning"* and maps it to gates **G-BUILD** / **G-IMG-PINNED** under blocker #3. This slice (`HARD-08 — kubernetes-helm-deploy`) is the **Kubernetes-deploy half of blocker #3** plus the **k8s install-verification half of blocker #6**: it *consumes* the built, digest-pinned images that the container-build workstream produces and proves they actually deploy and serve on a real cluster. Where this slice says "the container-build workstream", it means the spec's HARD-08 *"Container & web build"* scope (G-BUILD/G-IMG-PINNED). The two are siblings under one blocker; this file owns the chart, its tests, and the kind/k3d smoke. It **extends** the existing `deploy/` tree (no parallel deploy dir) and **implements the chart F24 designed** (`docs/implementation-slices/v2/F24-kubernetes-helm.md`) at the hardening scope (lint/template/install + smoke), not the full V2 HA/KEDA epic.

## 1. Intent — what & why

The ALPHA (`docs/MORNING_REPORT.md` §0, §5(8), §6) ships a hardened single-node `deploy/docker-compose.yml` whose `config` validates — but **no image was ever built, `next build` never ran, and no orchestrator deploy was ever exercised**. `deploy/helm/` does not exist on disk (verified: `deploy/` contains only `docker/`, `caddy/`, `scripts/`, and the two compose files). FORGE_SPEC's third install path — **Kubernetes for 50+ engineers** — is therefore entirely unproven: there is no chart, no install test, no upgrade test, no evidence a Forge pod ever reached Ready under kubelet probes.

This slice closes that gap with the **minimum real, mechanically-verified Helm chart** plus the test harness that exercises it:

1. **Build the chart for real.** A standard Helm v3 chart at `deploy/helm/forge` that re-expresses the compose hardening invariants (non-root, dropped caps, read-only rootfs, resource limits, network segmentation, digest-pinned images, healthchecks) as Kubernetes primitives, packaging the four real workloads (`api`, `worker`, `mcp-gateway`, `web`) and the three datastores (bundled `pgvector`/`redis`/`minio` **or** external/managed).
2. **Verify it without a cluster on every PR.** `helm lint`, `helm template` over three value profiles, `values.schema.json` enforcement, `kubeconform` schema conformance, and a `helm-unittest` suite — all network-free, all part of the whole-suite green gate so the tree never goes red.
3. **Install & smoke it on a real local cluster.** A `kind` (or `k3d`) integration lane that loads the built images, runs `helm install`, drives the **real Alembic migration chain** (the same one HARD-01 exercises on live pgvector) as a pre-install/pre-upgrade hook against the in-cluster pgvector, waits for every Deployment to be `Available`, and runs `helm test` to prove the deployed API answers `GET /health → 200` over the in-cluster Service — i.e. the stack genuinely runs on Kubernetes, not just renders.
4. **Pin images by digest for production.** The production values profile carries `@sha256:` digests for every image (the four app images from the container-build workstream + the three bundled subchart images); a render test fails if any floating tag survives — satisfying `G-IMG-PINNED`.

Why now / why this scope: blocker #3 says "images not pinned, builds never run"; blocker #6 says "no migration upgrade/rollback testing, low deploy maturity". A chart that lints but was never installed is the same class of unproven asterisk the whole hardening program exists to retire. This slice turns "compiles/validates" into "installs and serves on a cluster", with captured evidence. It deliberately scopes **down** from F24's full HA epic (KEDA, multi-profile NetworkPolicy matrix, Gateway API, ServiceMonitor) to the verifiable core (values, secret wiring, liveness/readiness, HPA, ingress, smoke), leaving the breadth as F24's V2 work (§12).

## 2. User-facing / operator behavior

The "user" is a **platform/SRE operator** self-hosting Forge on Kubernetes.

- **Journey A — Evaluate on a laptop cluster (bundled datastores, no external creds).**
  ```bash
  kind create cluster --name forge-eval            # or: k3d cluster create forge-eval
  helm dependency build deploy/helm/forge          # pulls pinned pgvector/redis/minio subcharts
  kubectl create namespace forge
  helm install forge deploy/helm/forge -n forge \
    --set forge.domain=forge.localtest.me \
    --set secrets.create=true \
    --set-string secrets.data.FORGE_SECRET_KEY="$(openssl rand -hex 32)" \
    --wait --timeout 15m
  kubectl -n forge get pods           # migrate Job Completed; api/worker/mcp-gateway/web Running+Ready
  helm test forge -n forge            # test-connection pod curls forge-api:8000/health -> 200
  ```
  The pre-install hook `forge-db-migrate` runs `alembic upgrade head` against the bundled pgvector to completion **before** any app pod serves traffic; pods become Ready only when their `/health` probe passes; the operator browses `http://forge.localtest.me`.

- **Journey B — Production install with managed datastores.** Operator points at managed Postgres+pgvector / Redis / S3 via `externalDatabase`/`externalRedis`/`externalObjectStore` + a pre-created `secrets.existingSecret`, sets `postgresql.enabled=false` etc.; **no bundled stateful pods/PVCs render**. `helm install forge deploy/helm/forge -n forge -f values-production.yaml` — every image is `@sha256`-pinned (the chart fails render if not, in the prod profile).

- **Journey C — Zero-downtime upgrade + rollback.** `helm upgrade` re-runs the **pre-upgrade** migration hook first; only on its success do workloads roll (`RollingUpdate`, `maxUnavailable: 0`) under a `PodDisruptionBudget`. If the hook fails, the release does not roll; `helm rollback forge <rev>` restores the prior revision. (The data-preserving upgrade→rollback→re-upgrade *proof on a populated DB* is owned by HARD-13 `G-MIGRATE`; this slice proves the **hook ordering and the rollback mechanics** on kind.)

- **Journey D — Scale under load.** `api`/`web` `HorizontalPodAutoscaler` scale on CPU between `minReplicas` and `maxReplicas`; operator watches `kubectl get hpa -n forge`. (KEDA queue-length scaling for the Celery worker is F24/V2; this slice ships CPU-HPA only.)

- **Journey E — Operator reads the docs.** `docs/self-hosting/kubernetes.md` is upgraded from "preview pointer" to a supported runbook walking A–D: prerequisites (k8s ≥ 1.28, an ingress controller, a CNI for NetworkPolicy), `helm install`, the full values reference, external datastores, ingress+TLS, verification (`helm test`), and upgrade/rollback.

Failure-mode behavior the operator sees: a missing boot-critical secret fails **at `helm template`/`install` time** with a clear `{{ fail }}` message (not a later CrashLoopBackOff); configuring both bundled and external for one datastore fails render; a tag-only image in the production profile fails the digest test.

## 3. Vertical slice

### 3.1 Data model

**No new tables, columns, or migrations.** This is a deploy/packaging slice. It **invokes** the existing Alembic chain (`packages/db` — the `forge_db` baseline + per-feature revisions, the singular/`Enum(native_enum=False)` schema the whole program conforms to) via a Kubernetes `Job` running `alembic upgrade head` (the same command HARD-01 runs against live pgvector and `make migrate` runs in dev). It **operates on** datastores at the cluster/PVC/external-endpoint level only.

One *requirement* it places on the DB (does not own): the `vector` (pgvector) extension must be available. The bundled subchart uses a `pgvector/pgvector`-based image; managed-Postgres operators are instructed in `kubernetes.md` to ensure `CREATE EXTENSION vector` is permitted (the extension's `CREATE` lives in the knowledge-retrieval migration; HARD-01 verifies it executes). The kind smoke (AC18) confirms the migration chain — including the `vector` extension create — succeeds on the bundled pgvector pod.

### 3.2 Backend

This slice implements **no** FastAPI routes. It **consumes** the real health surface of `apps/api` / `apps/mcp-gateway` / `apps/web` to wire kubelet probes, and the Alembic entrypoint for the hook.

Health/probe contract **as it really exists in the ALPHA** (read from `deploy/docker-compose.yml` healthchecks + MORNING_REPORT §1 0.4 "`/health` 200"):

| Workload | Probe(s) | Real endpoint / command | Port |
|---|---|---|---|
| `api` | readiness + liveness + startup | `GET /health` | 8000 |
| `mcp-gateway` | readiness + liveness | `GET /health` | 8001 |
| `web` | readiness + liveness | `GET /` (Next.js root `200`) | 3000 |
| `worker` | liveness only (no Service) | exec `celery -A forge_worker inspect ping` | — |

> **Honest reconciliation with F24.** F24's design assumed a split `/healthz` (liveness) + `/readyz` (readiness, gated on Postgres+Redis+S3 reachability). **That split does not exist in the ALPHA** — the real apps expose a single `/health` (compose probes `curl /health`). This slice wires probes to the **real** `/health` so the chart installs against the code that exists today. Adding a dependency-aware `/readyz` (so readiness gates Service endpoints only when datastores are reachable) is a small backend follow-up tracked against the foundation (`cross-cutting/C01`) and noted in §12; when it lands, the chart flips readiness to `/readyz` behind a values toggle `probes.readinessPath` (default `/health`).

Migration hook command (real): the chart's `job-migrate.yaml` runs the **api image** (it carries `packages/db` + `alembic.ini`) with `["sh","-c","alembic -c packages/db/alembic.ini upgrade head"]` (exact `-c` path resolved against the image's working dir; the chart exposes `migrations.command` so the path is overridable without re-templating). It reads DB connection env via `envFrom` the same `ConfigMap`+`Secret` the app pods use, so it points at the in-cluster (or external) Postgres. There is **no `forge-cli`** in the ALPHA (F24 assumed one); the hook uses `alembic` directly, matching `make migrate`. `migrations.command` lets a future `forge-cli db migrate` drop in without a chart change.

### 3.3 Worker / agent runtime

No Celery tasks or graphs are authored here. The chart **packages and runs** the existing worker:

- **Command** mirrors compose exactly: `["celery","-A","forge_worker","worker","--loglevel=info","-Q","forge,incident"]` (overridable via `worker.command`).
- **Liveness** = exec `celery -A forge_worker inspect ping` (the real compose healthcheck); **no readiness** (the worker is not behind a Service).
- **Scaling**: a CPU-based `HorizontalPodAutoscaler` (the no-extra-operator path). KEDA Redis-queue-length scaling is explicitly deferred to F24/V2 (§12); a `worker.keda.enabled` values stub is reserved but renders nothing in this slice.
- **Graceful drain**: `terminationGracePeriodSeconds` default 120s + a `preStop` `celery -A forge_worker control shutdown` so in-flight tasks attempt a warm finish; a `PodDisruptionBudget` (`minAvailable: 1`) prevents mass eviction. (Durable survival of long agent runs across eviction is a Temporal/V2 concern — out of scope, §12.)
- **Worktree storage**: per-pod ephemeral `emptyDir` (no `ReadWriteMany` PVC), matching the agent-runtime git-worktree sandbox model; values toggle `worker.repoStorage` reserved.

### 3.4 Frontend

No product UI is authored. The chart templates the `web` Deployment/Service/HPA/PDB and wires its probe to the real Next.js root (`GET / → 200`, the compose `wget http://localhost:3000` check). `apps/web` **does** exist in the ALPHA (MORNING_REPORT §1 0.5; 28 Vitest tests). The chart consumes the `web` image produced by the container-build workstream (which runs the real `next build` — the `G-BUILD` half of blocker #3 this slice depends on). `web.enabled` gates the workload so the chart still installs cleanly if a web image is temporarily unavailable. Env: `NODE_ENV=production`, `NEXT_PUBLIC_API_URL` (default `/api`, routed by the ingress).

### 3.5 Infra / deploy / CI

**This is the whole slice.** Deliverables under `deploy/helm/forge/` (a standard Helm v3 chart), a kind/k3d test harness, the `kubernetes.md` rewrite, and a CI workflow.

**Chart layout (hardening scope — the verifiable core of F24):**
```
deploy/helm/forge/
├── Chart.yaml                 # apiVersion v2; chart+appVersion; conditional subchart deps
├── Chart.lock                 # locked subchart versions/digests (committed)
├── values.yaml                # documented defaults (bundled datastores ON for eval)
├── values-production.yaml     # managed-datastore HA profile (bundled OFF; @sha256 digests required)
├── values.example.yaml        # minimal install used by CI + kubernetes.md
├── values.schema.json         # values contract (§4)
├── README.md                  # helm-docs values reference
├── templates/
│   ├── _helpers.tpl           # fullname, common+selector labels, image-ref builder, env builders
│   ├── NOTES.txt
│   ├── serviceaccount.yaml
│   ├── configmap-env.yaml     # NON-secret env ONLY (set-disjoint from secret.yaml)
│   ├── secret.yaml            # rendered only when secrets.create=true; else references existingSecret
│   ├── api/{deployment,service,hpa,pdb}.yaml
│   ├── worker/{deployment,hpa,pdb}.yaml
│   ├── mcp-gateway/{deployment,service}.yaml
│   ├── web/{deployment,service,hpa,pdb}.yaml
│   ├── ingress.yaml
│   ├── networkpolicy.yaml     # default-deny + datastore allowlist (api/worker only)
│   ├── job-migrate.yaml       # Helm hook: pre-install,pre-upgrade -> alembic upgrade head
│   └── tests/
│       └── test-connection.yaml   # `helm test` pod: curl forge-api:8000/health
└── tests/                     # helm-unittest specs (see §7)
```

**Subcharts** (`Chart.yaml` `dependencies`, conditional, pinned in `Chart.lock`): `postgresql` (Bitnami, `condition: postgresql.enabled`, image overridden to a **pgvector-enabled** repo/tag/digest), `redis` (Bitnami, `condition: redis.enabled`), `minio` (Bitnami, `condition: minio.enabled`). When the corresponding `external*` block is set, the subchart does not render.

**Compose invariant → Kubernetes primitive (mirrors `deploy/docker-compose.yml`):**

| Compose invariant (real, today) | Kubernetes expression (this slice) |
|---|---|
| `image:` pinned to version tag; `@sha256` PARKED | `_helpers.tpl` image builder emits `repo@sha256:…` when `image.digest` set; the prod profile requires it; a render test fails on a floating tag (G-IMG-PINNED). |
| `user: "1000:1000"` (non-root) | `podSecurityContext.runAsNonRoot: true`, `runAsUser/fsGroup: 1000`; container `allowPrivilegeEscalation: false`, `capabilities.drop:[ALL]`, `readOnlyRootFilesystem: true`, `seccompProfile.type: RuntimeDefault`; writable scratch via `emptyDir`. |
| `healthcheck:` per service | liveness + readiness (+ startup on api) probes to the real endpoints (§3.2). |
| `deploy.resources.limits` (cpu/mem) | `resources.requests` + `resources.limits` per container (schema-required). |
| networks `data: {internal: true}`, `edge`/`backend`/`mcp` | `NetworkPolicy`: default-deny ingress; datastore policy allows ingress only from `api`/`worker` pod selectors; ingress-controller → `web`/`api`. (Requires a NetworkPolicy-enforcing CNI — documented.) |
| `willfarrell/autoheal` sidecar | N/A — replaced by kubelet liveness-probe restarts (`restartPolicy: Always`). |
| `caddy` auto-HTTPS | `Ingress` + cert-manager annotation **or** operator-supplied `tls.secretName`. |
| `--remove-orphans` | native `helm upgrade` prune. |
| `FORGE_VERSION` tag interpolation | `appVersion` + per-workload `image.tag`/`image.digest`. |

**Ingress routing contract** (`ingress.yaml`, nginx-ingress assumed): host `${forge.domain}`; paths `/api`, `/health`, and the GitHub webhook path (the F03 integration webhook route, e.g. `/api/integrations/github/webhook`) → `forge-api:8000`; everything else → `forge-web:3000`. The webhook path must **preserve the raw request body** for HMAC verification: set `nginx.ingress.kubernetes.io/proxy-request-buffering: "off"` and carry **no** `rewrite-target`/body-mutating annotation on that path (asserted by `ingress_test.yaml`). Generous `proxy-read-timeout` for SSE run-trace streams.

**CI** — new workflow `.github/workflows/helm-chart.yml`:
- Job `helm-static` (every PR touching `deploy/helm/**`, **no cluster**): `helm lint` (defaults + prod), `helm template` × 3 profiles, `values.schema.json` validation, `kubeconform -strict`, `helm unittest`, `yamllint`. These also run under the repo's whole-suite gate via the Python render-contract test (`@pytest.mark.helm`, skips cleanly if `helm` absent).
- Job `helm-kind` (Docker-enabled runner, required before tagging a chart release / for the BETA `G-BUILD`+deploy evidence): builds/loads the four Forge images (or pulls digest-pinned ones from the container-build workstream), `kind create cluster`, `helm dependency build`, `helm install --wait`, asserts rollout + `helm test`, then `helm upgrade`/`helm rollback`, then tears down.

Also: the existing `.github/workflows/ci.yml` `compose` job (today only `docker compose config`) is extended by the container-build sibling workstream to actually `build`; this slice's `helm-kind` job **consumes** those images, so the two workflows share the image-build step (documented, not duplicated).

## 4. Public interfaces / contracts (exact signatures, env vars, config keys)

The "interfaces" of this slice are (a) the Helm `values` contract, (b) the rendered-resource invariants, (c) the env-var contract the workloads consume, and (d) the hook contract. All are mechanically asserted (§7).

### 4.1 Env-var contract (REAL — reused verbatim from `deploy/docker-compose.yml`, never re-invented)

**Non-secret env → `configmap-env.yaml` (`ConfigMap`):**
| Key | Value (rendered) | Source |
|---|---|---|
| `FORGE_ENV` | `production` | compose |
| `FORGE_DATABASE_URL` | `postgresql+psycopg://<user>:<pw-from-secret>@<host>:5432/<db>` (host = `{fullname}-postgresql` bundled, or `externalDatabase.host`) | compose |
| `FORGE_REDIS_URL` | `redis://<host>:6379/0` (host = `{fullname}-redis-master` or `externalRedis.host`) | compose |
| `MINIO_ENDPOINT` | `http://{fullname}-minio:9000` or `externalObjectStore.endpoint` | compose |
| `MCP_GATEWAY_URL` | `http://{fullname}-mcp-gateway:8001` | compose |
| `NEXT_PUBLIC_API_URL` | `/api` (web) | compose |
| `NODE_ENV` | `production` (web) | compose |

> Password material referenced by `FORGE_DATABASE_URL`/`FORGE_REDIS_URL` is **not** rendered into the ConfigMap; the URL is assembled in `_helpers.tpl` with the password sourced from the Secret at pod start (env-substitution via an init pattern or a `*_PASSWORD` + URL-template the app composes). The ConfigMap holds only the non-secret host/port/db.

**Secret env → `secret.yaml` (`Secret`, only when `secrets.create=true`) / operator `existingSecret`:**
| Key | Required? | Notes |
|---|---|---|
| `FORGE_SECRET_KEY` | **boot-critical** | app signing key + at-rest cipher key for the BYOK vault. **Per HARD-10, required (no ephemeral fallback) in prod.** The ALPHA app currently reads `SECRET_KEY` (compose) — the chart renders **both** `FORGE_SECRET_KEY` and `SECRET_KEY` from the same value until HARD-10 unifies the name, so the chart works pre- and post-HARD-10. |
| `POSTGRES_PASSWORD` | required (bundled) | also consumed by the postgresql subchart (`auth.existingSecret`). |
| `REDIS_PASSWORD` | optional | if redis auth enabled. |
| `MINIO_ROOT_PASSWORD` | required (bundled minio) | |
| `FORGE_PAGERDUTY_WEBHOOK_SECRET`, `FORGE_DATADOG_WEBHOOK_SECRET`, `FORGE_SENTRY_WEBHOOK_SECRET`, `FORGE_GRAFANA_WEBHOOK_SECRET` | optional | F17 incident webhook signing; absent → app fails closed (501) per compose comment. |
| `GITHUB_APP_PRIVATE_KEY` (PEM) | optional | mounted **as a file**; chart sets `GITHUB_APP_PRIVATE_KEY_PATH` to the mount (HARD-05 / `deploy/secrets/github-app.pem` posture: env-only, never logged). |
| `GITHUB_WEBHOOK_SECRET`, `SLACK_SIGNING_SECRET`, `SLACK_BOT_TOKEN` | optional | HARD-05/HARD-07 integration secrets. |

> **BYOK model-provider keys are NOT in the chart Secret.** Per the program's BYOK posture, per-workspace model/embedder/reranker keys live in the **encrypted DB vault** (`forge_api.auth.vault`), resolved at call time and decrypted with `FORGE_SECRET_KEY`. The chart only carries platform-level secrets. This keeps the chart compatible with the redaction/vault rules in the spec's "Credentials & secrets handling".

### 4.2 `values.schema.json` (abridged; full file is the deliverable)
```jsonc
{
  "$schema": "https://json-schema.org/draft-07/schema#",
  "type": "object",
  "required": ["forge", "api", "worker", "mcpGateway", "web", "ingress", "secrets"],
  "$defs": {
    "workload": {
      "type": "object",
      "required": ["image", "replicaCount", "resources"],
      "properties": {
        "enabled": {"type": "boolean"},
        "image": {
          "type": "object", "required": ["repository", "tag"],
          "properties": {
            "repository": {"type": "string"},
            "tag": {"type": "string"},
            "digest": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"}
          }
        },
        "replicaCount": {"type": "integer", "minimum": 1},
        "resources": {
          "type": "object", "required": ["requests", "limits"],
          "properties": {
            "requests": {"type": "object", "required": ["cpu", "memory"]},
            "limits":   {"type": "object", "required": ["cpu", "memory"]}
          }
        },
        "autoscaling": {"type": "object", "properties": {
          "enabled": {"type": "boolean"},
          "minReplicas": {"type": "integer", "minimum": 1},
          "maxReplicas": {"type": "integer", "minimum": 1},
          "targetCPUUtilizationPercentage": {"type": "integer", "minimum": 1, "maximum": 100}}},
        "pdb": {"type": "object", "properties": {"enabled": {"type": "boolean"}, "minAvailable": {}}}
      }
    }
  },
  "properties": {
    "forge": {"type": "object", "required": ["domain"],
      "properties": {"domain": {"type": "string", "minLength": 3},
                     "publicUrl": {"type": "string"},
                     "logLevel": {"enum": ["debug","info","warning","error"]}}},
    "api":        {"$ref": "#/$defs/workload"},
    "worker":     {"$ref": "#/$defs/workload"},
    "mcpGateway": {"$ref": "#/$defs/workload"},
    "web":        {"$ref": "#/$defs/workload"},
    "secrets": {"type": "object", "properties": {
      "create": {"type": "boolean"},
      "existingSecret": {"type": "string"},
      "data": {"type": "object", "properties": {
        "FORGE_SECRET_KEY": {"type": "string"},
        "POSTGRES_PASSWORD": {"type": "string"},
        "REDIS_PASSWORD": {"type": "string"},
        "MINIO_ROOT_PASSWORD": {"type": "string"},
        "GITHUB_APP_PRIVATE_KEY": {"type": "string"},
        "GITHUB_WEBHOOK_SECRET": {"type": "string"},
        "SLACK_SIGNING_SECRET": {"type": "string"},
        "SLACK_BOT_TOKEN": {"type": "string"}}}}},
    "postgresql": {"type": "object", "properties": {"enabled": {"type": "boolean"}}},
    "externalDatabase": {"type": "object", "properties": {
      "host": {"type": "string"}, "port": {"type": "integer"}, "user": {"type": "string"},
      "database": {"type": "string"}, "existingSecret": {"type": "string"}, "passwordKey": {"type": "string"}}},
    "redis": {"type": "object", "properties": {"enabled": {"type": "boolean"}}},
    "externalRedis": {"type": "object", "properties": {"host": {"type": "string"}, "port": {"type": "integer"}}},
    "minio": {"type": "object", "properties": {"enabled": {"type": "boolean"}}},
    "externalObjectStore": {"type": "object", "properties": {"endpoint": {"type": "string"}, "bucket": {"type": "string"}}},
    "ingress": {"type": "object", "required": ["enabled"], "properties": {
      "enabled": {"type": "boolean"}, "className": {"type": "string"},
      "annotations": {"type": "object"},
      "tls": {"type": "object", "properties": {"enabled": {"type": "boolean"}, "secretName": {"type": "string"},
        "certManager": {"type": "object", "properties": {"enabled": {"type": "boolean"}, "clusterIssuer": {"type": "string"}}}}}}},
    "networkPolicy": {"type": "object", "properties": {"enabled": {"type": "boolean"}}},
    "migrations": {"type": "object", "properties": {"enabled": {"type": "boolean"}, "backoffLimit": {"type": "integer"}, "command": {"type": "array"}}},
    "probes": {"type": "object", "properties": {"readinessPath": {"type": "string"}, "livenessPath": {"type": "string"}}}
  }
}
```

### 4.3 Rendered-resource invariants (asserted by helm-unittest + kubeconform + a Python render-contract test)
For **every** stateless workload (`forge-api`, `forge-worker`, `forge-mcp-gateway`, `forge-web`):
1. `image:` is `repo@sha256:<64-hex>` when `image.digest` is set (required in `values-production.yaml`).
2. `securityContext`: `runAsNonRoot: true`, `allowPrivilegeEscalation: false`, `readOnlyRootFilesystem: true`, `capabilities.drop:[ALL]`, `seccompProfile.type: RuntimeDefault`.
3. `resources.requests.{cpu,memory}` and `resources.limits.{cpu,memory}` all present.
4. liveness + readiness probes present (worker: liveness exec only).
5. With autoscaling enabled, an `HPA` + a `PodDisruptionBudget` render for `api`/`web` (and `worker` HPA); prod profile `replicaCount`/`minReplicas` ≥ 2.
6. Recommended labels on every object: `app.kubernetes.io/{name,instance,version,component,part-of=forge,managed-by=Helm}`; selector labels are the immutable subset (`name,instance,component`).
7. Every pod consumes env via `envFrom` the `ConfigMap` (non-secret) **and** the `Secret` (secret); **Secret keys ∩ ConfigMap keys = ∅** (set-disjointness).

### 4.4 Template guards (fail-fast `{{ fail }}` / `required`)
- `forge.domain` is `required`.
- Each datastore: exactly one of `<store>.enabled: true` **or** a populated `external<Store>` — else `fail "Configure either bundled or external <store>, not both/neither"`.
- `secrets.create: false` requires non-empty `secrets.existingSecret`.
- `secrets.create: true` requires non-empty `secrets.data.FORGE_SECRET_KEY` (boot-critical) — else `fail "secrets.data.FORGE_SECRET_KEY is required (app fails closed without it — HARD-10)"`.
- `ingress.tls.enabled: true` requires `certManager.enabled: true` **or** a non-empty `tls.secretName`.
- Production profile sets `image.digest` for all four workloads; a render test asserts the prod profile renders only digest-pinned images.

### 4.5 Hook contract (`job-migrate.yaml`)
```yaml
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
          image: {{ include "forge.image" (dict "ctx" . "wl" .Values.api) }}   # api image carries packages/db + alembic
          command: {{ .Values.migrations.command | default (list "sh" "-c" "alembic -c packages/db/alembic.ini upgrade head") | toJson }}
          envFrom:
            - configMapRef: { name: {{ include "forge.fullname" . }}-env }
            - secretRef:    { name: {{ include "forge.secretName" . }} }
          securityContext: { runAsNonRoot: true, readOnlyRootFilesystem: true, allowPrivilegeEscalation: false, capabilities: { drop: [ALL] } }
```

## 5. Dependencies (other slices/foundation that must exist first)

**Foundation / real-monorepo prerequisites (exist today, this slice binds to them):**
- **`deploy/docker-compose.yml` + `deploy/docker/{api,worker,mcp-gateway,web}.Dockerfile`** (exist) — the source of the **env-var contract** (§4.1), the **healthcheck contract** (§3.2), the **hardening invariants** (§3.5 table), and the four workloads. The chart re-expresses these; it does not redefine them.
- **`packages/db` Alembic chain** (exists; `forge_db` schema) — invoked by the migration hook (`alembic upgrade head`). The chart runs the *same* chain HARD-01 exercises on live pgvector and `make migrate` runs in dev.
- **`apps/{api,worker,mcp-gateway,web}`** (exist) — the workloads packaged; `/health` (api/mcp-gateway), Next.js root (web), and `celery -A forge_worker inspect ping` (worker) are the probe targets.

**Sibling hardening workstreams (must land first or co-land):**
- **Container & web build + image digest pinning** (SPEC §HARD-08 *"Container & web build"*, gates `G-BUILD`/`G-IMG-PINNED`) — **REQUIRED**. This chart **consumes** the built images (`forge/{api,worker,mcp-gateway,web}` or `ghcr.io/...`) and the digests it pins in `values-production.yaml`. The kind smoke loads these images. Without real, buildable images there is nothing to deploy. (This is the "other half" of blocker #3; see the numbering note up top.)
- **HARD-01 — Real Postgres + pgvector substrate** — **REQUIRED** (logically). The migration hook + smoke prove the Alembic chain applies on the in-cluster pgvector; HARD-01 first proves that chain applies on live pgvector at all (upgrade head / downgrade base / `pytest -m postgres`). The kind smoke is the k8s-shaped re-exercise of the same path.
- **HARD-10 — Production crypto + `FORGE_SECRET_KEY` required** — **SOFT/co-land**. The chart's fail-closed secret guard (§4.4) mirrors HARD-10's "no ephemeral fallback" requirement; the chart renders both `FORGE_SECRET_KEY` and the legacy `SECRET_KEY` so it works before and after HARD-10 renames the key.

**Design reference (not a runtime dep):**
- **`docs/implementation-slices/v2/F24-kubernetes-helm.md`** — this slice **implements the hardening-scoped subset** of F24's chart design (values schema, hardening table, hook contract, helm-unittest harness). F24's full HA breadth (KEDA, multi-profile NetworkPolicy matrix, Gateway API renderer, ServiceMonitor, create-admin hook, reranker workload) remains F24/V2 (§12). When F24 lands in full it **extends this same `deploy/helm/forge` chart** — no parallel chart.

**Soft / accommodated (need not be complete):**
- **HARD-05 (GitHub App)** — ingress webhook raw-body rule + `GITHUB_APP_*` secret keys + PEM-as-file mount accommodated.
- **HARD-07 (Slack)** — `SLACK_*` secret keys accommodated.
- **`docs/self-hosting/` (F15)** — `kubernetes.md` rewritten here from preview → supported; backup/restore of cluster datastores stays in F15/`backup.md` (out of scope, §12).

## 6. Acceptance criteria (numbered, testable; creds/offline marked)

Tiers: **[offline-static]** = no cluster, no network, no creds (runs in the default suite / `helm-static` CI job); **[kind-int]** = needs Docker + a local k8s + the built images (networked CI runner; **no external/BYOK creds**); **[needs-real-creds]** = none in this slice.

1. **Chart lints clean.** `helm lint deploy/helm/forge` and `helm lint deploy/helm/forge -f deploy/helm/forge/values-production.yaml` both exit 0, no errors. **[offline-static]**
2. **Renders on three profiles.** `helm template forge deploy/helm/forge` for each of {defaults, `values.example.yaml`, `values-production.yaml`} exits 0 and yields parseable multi-doc YAML. **[offline-static]**
3. **Values schema enforced.** `values.schema.json` rejects a values file missing `forge.domain` and one with a non-`sha256:` `image.digest`; accepts all three shipped profiles. **[offline-static]**
4. **Digest pinning in prod (`G-IMG-PINNED`).** Under `values-production.yaml`, every rendered workload + the migrate hook `image:` matches `…@sha256:[0-9a-f]{64}`; a tag-only image fails the test. **[offline-static]**
5. **Hardened securityContext.** Every workload pod/container sets `runAsNonRoot`, `allowPrivilegeEscalation:false`, `readOnlyRootFilesystem:true`, `capabilities.drop:[ALL]`, `seccompProfile.type:RuntimeDefault`. **[offline-static]**
6. **Resources present.** Every workload container declares `resources.requests.{cpu,memory}` and `.limits.{cpu,memory}`. **[offline-static]**
7. **Probes wired to the REAL endpoints.** `api`/`mcp-gateway` render readiness+liveness HTTP `GET /health`; `web` renders `GET /`; `api` also renders a startup probe; `worker` renders a liveness exec probe containing `inspect ping`. **[offline-static]**
8. **HA primitives.** With autoscaling enabled, `api`/`web`/`worker` render an `HPA` and `api`/`web`/`worker` a `PodDisruptionBudget`; in the prod profile `replicaCount`/`minReplicas` ≥ 2. **[offline-static]**
9. **Secret/Config separation.** Rendered `Secret` keys ∩ `ConfigMap` keys = ∅; with `secrets.create=false` and empty `existingSecret`, render **fails** with the guard message; with `secrets.create=true` and empty `FORGE_SECRET_KEY`, render **fails** (fail-closed, HARD-10). **[offline-static]**
10. **Datastore XOR guard.** Both `postgresql.enabled=true` and a populated `externalDatabase` (or neither) fails render with the §4.4 message; same for redis and object store. **[offline-static]**
11. **External mode emits no bundled stateful objects.** Under `values-production.yaml` (bundled OFF), the manifest contains **no** subchart `StatefulSet`/`PVC`/`Service` for postgres/redis/minio, and `FORGE_DATABASE_URL`/`FORGE_REDIS_URL`/`MINIO_ENDPOINT` point at the `external*` endpoints. **[offline-static]**
12. **Migration hook ordering.** `job-migrate.yaml` carries `helm.sh/hook: pre-install,pre-upgrade` with a negative `hook-weight`, uses the api image, and runs `alembic … upgrade head`. **[offline-static]**
13. **Ingress routing + webhook raw body.** The `Ingress` routes `/api`,`/health`,`<github webhook path>` → `forge-api:8000` and `/` → `forge-web:3000`; the webhook path carries `proxy-request-buffering:"off"` and **no** `rewrite-target`/body-mutating annotation; TLS renders a cert-manager annotation or `tls.secretName`. **[offline-static]**
14. **NetworkPolicy segmentation.** With `networkPolicy.enabled`, datastore policies allow ingress only from `api`/`worker` selector labels and a default-deny baseline exists. **[offline-static]**
15. **kubeconform conformance.** `helm template … | kubeconform -strict -kubernetes-version 1.28.0 -schema-location default` exits 0 for all three profiles. **[offline-static]**
16. **helm-unittest suite green.** `helm unittest deploy/helm/forge` passes all specs. **[offline-static]**
17. **Recommended labels.** Every rendered object carries the `app.kubernetes.io/*` + `part-of: forge` labels; selectors are the immutable subset. **[offline-static]**
18. **kind/k3d smoke install (bundled) — the headline deploy evidence.** On a fresh `kind` (or `k3d`) cluster, `helm dependency build` + `helm install --wait --timeout 15m` with bundled datastores brings the migrate Job to `Completed` (alembic head applied to the in-cluster pgvector, incl. the `vector` extension), and `forge-api`/`forge-web`/`forge-mcp-gateway`/`forge-worker` Deployments to `Available`; `helm test forge` (test-connection pod curls `http://forge-api:8000/health`) passes within timeout. **[kind-int]**
19. **Upgrade runs migration first + rollback.** `helm upgrade` (bump a benign value) re-runs the pre-upgrade hook to `Completed` before pods roll (poll `availableReplicas` ≥ `minAvailable` throughout); `helm rollback forge 1` restores the prior revision healthy. (Data-preserving populated-DB proof is HARD-13 `G-MIGRATE`; this AC proves hook-ordering + rollback mechanics.) **[kind-int]**
20. **Whole-suite green gate holds.** `uv run pytest -q` (the helm Python tests skip cleanly when `helm`/`kubeconform`/kind absent), `uv run ruff check .`, `make typecheck`, and `cd apps/web && pnpm test` stay green; the `helm-static` CI job is green and blocking. **[offline-static]**
21. **Docs supported.** `docs/self-hosting/kubernetes.md` documents `helm install`/`helm upgrade` against `deploy/helm/forge`, no longer carries a "preview" banner, and `helm lint`/`template` succeed on the values it shows. **[offline-static]**

## 7. Test plan (TDD) — unit + integration (gated) + how to run

Write the failing tests first (helm-unittest specs + the Python render-contract test) against an empty `templates/`, then drive each template into existence criterion-by-criterion. Two tiers: **static** (every PR, network-free, part of the whole-suite gate) and **kind-int** (`@pytest.mark.kind`, Docker + cluster).

**Tooling (pinned in CI):** `helm` ≥ 3.14 + `helm-unittest` plugin, `kubeconform`, `yamllint`, `kind` (or `k3d`), and `python` (`pytest`, `ruamel.yaml`, `jsonschema`).

**Static — helm-unittest specs (`deploy/helm/forge/tests/*_test.yaml`), one per concern:**
- `securitycontext_test.yaml` (AC5) — hardened set on every workload + migrate hook (parametrized).
- `resources_test.yaml` (AC6) — requests+limits on every container.
- `probes_test.yaml` (AC7) — api/mcp-gateway readiness+liveness path `/health`; web `/`; worker exec contains `inspect ping`; api startup probe present.
- `scaling_test.yaml` (AC8) — HPA + PDB present; prod replica ≥ 2.
- `secret_config_separation_test.yaml` (AC9) — Secret∩ConfigMap = ∅; `secrets.create=false`+empty `existingSecret` → `failedTemplate`; `secrets.create=true`+empty `FORGE_SECRET_KEY` → `failedTemplate`.
- `datastore_guard_test.yaml` (AC10, AC11) — both-on/both-off → `failedTemplate`; prod (external) → `hasDocuments: {kind: StatefulSet, count: 0}` for each subchart.
- `migrate_hook_test.yaml` (AC12) — hook annotations + `alembic … upgrade head` + api image.
- `ingress_test.yaml` (AC13) — path→backend map; webhook path has no `rewrite-target`/body annotation; `proxy-request-buffering:"off"`; cert-manager annotation when enabled.
- `networkpolicy_test.yaml` (AC14) — datastore ingress `from` == api/worker; default-deny present.
- `labels_test.yaml` (AC17) — recommended labels on one object per kind.
- `digest_test.yaml` (AC4) — prod profile: every `image` `matchRegex` `@sha256:`.

**Static — Python tests (run under `uv run pytest`, `@pytest.mark.helm`, skip if `helm` absent):**
- `deploy/helm/tests/test_values_schema.py` (AC3) — `test_schema_rejects_missing_domain`, `test_schema_rejects_bad_digest`, `test_schema_accepts_shipped_profiles` via `jsonschema` against each values file.
- `deploy/helm/tests/test_render_contract.py` (AC2, AC4, AC11, AC15, AC17) — shells `helm template` per profile into `ruamel.yaml`, asserts the §4.3 "every workload" invariants in one DRY place, and pipes output to `kubeconform -strict` (AC15). Skips with a clear message if `helm`/`kubeconform` are absent locally; always present in CI.

**Static — lint (AC1):** `helm lint` on defaults + prod; `yamllint` over rendered output.

**Integration — kind/k3d smoke (`deploy/helm/tests/e2e/test_kind_install.py`, `@pytest.mark.kind`):**
- module-scoped fixture `kind_cluster`: create an ephemeral `kind` cluster (unique name), install a `local-path` storage class, `kind load docker-image` the four Forge images (built by the container-build workstream), teardown in a finalizer. (k3d variant behind an env switch `FORGE_LOCAL_K8S=k3d`.)
- `test_bundled_install_reaches_ready` (AC18) — `helm dependency build`; `helm install --wait --timeout 15m` bundled; assert migrate Job `Completed`, the four Deployments `Available`; `helm test forge` (test-connection pod curls `http://forge-api:8000/health` → 200).
- `test_upgrade_runs_migration_and_rollback` (AC19) — `helm upgrade` (benign bump); assert the new-revision pre-upgrade hook `Completed` and `availableReplicas ≥ minAvailable` throughout the roll; `helm rollback forge 1`; assert healthy.

**Docs check (AC21):** `deploy/helm/tests/test_kubernetes_doc_supported.py` asserts `docs/self-hosting/kubernetes.md` references `deploy/helm`, `helm install`, `helm upgrade`, and its `## Status` no longer contains `preview`.

**How to run.**
```bash
# Static (no cluster, no creds) — part of the whole-suite gate:
helm lint deploy/helm/forge
helm lint deploy/helm/forge -f deploy/helm/forge/values-production.yaml
helm template forge deploy/helm/forge | kubeconform -strict -kubernetes-version 1.28.0 -schema-location default
helm unittest deploy/helm/forge
uv run pytest deploy/helm/tests -m helm          # schema + render-contract (skips if helm absent)
uv run pytest -q                                  # whole suite stays green (helm/kind tests skip cleanly)

# Integration (Docker + local k8s; NO external creds):
kind create cluster --name forge-ci
uv run pytest deploy/helm/tests/e2e -m kind       # install + smoke + upgrade/rollback
kind delete cluster --name forge-ci
```

**CI wiring** (`.github/workflows/helm-chart.yml`): `helm-static` (lint + unittest + schema + render-contract + kubeconform + yamllint) on every PR touching `deploy/helm/**` — **blocking**; `helm-kind` (`@pytest.mark.kind`, Docker-enabled runner; builds/loads the four images) — required before a chart-version tag and for the BETA deploy evidence.

## 8. Security & policy considerations

- **Secrets never land in a ConfigMap or in committed `values`.** The chart renders a `Secret` only when `secrets.create=true` (dev/eval); production uses `secrets.existingSecret` (operator-managed — External Secrets Operator / sealed-secrets / Vault CSI, referenced not owned). AC9 mechanically asserts Secret↔ConfigMap key-disjointness. `kubernetes.md` instructs `openssl rand -hex 32` for `FORGE_SECRET_KEY` and never committing populated values. This matches the program's "env-only ingress, never committed/logged/in fixtures" rule; `.gitignore` already ignores `.env*`/`*.pem` — the chart's example values carry **key names only**, never values.
- **`FORGE_SECRET_KEY` is fail-closed (HARD-10 alignment).** The render guard (§4.4) refuses to template without it, so a missing at-rest cipher key is caught at `helm install` time, not at a later CrashLoopBackOff. BYOK model keys are **not** in the chart — they live in the encrypted DB vault unlocked by `FORGE_SECRET_KEY`; a Postgres restore is useless without the same key material (documented in `kubernetes.md`).
- **GitHub App private key is a file, not a value.** When provided, `GITHUB_APP_PRIVATE_KEY` (PEM) is mounted as a file and `GITHUB_APP_PRIVATE_KEY_PATH` points at the mount — the HARD-05 "`.pem` is a path, never logged/committed" posture, carried onto k8s.
- **Webhook raw-body integrity (HARD-05).** The ingress webhook path must not buffer-transform/rewrite the body or HMAC verification breaks — `proxy-request-buffering:"off"`, no `rewrite-target`; asserted by `ingress_test.yaml`.
- **Pod hardening** mirrors compose `user:"1000:1000"` + the spec's "Production Docker Compose Requirements": `runAsNonRoot`, `readOnlyRootFilesystem` (writable scratch via `emptyDir`), `drop:[ALL]`, `allowPrivilegeEscalation:false`, `seccompProfile:RuntimeDefault`, a least-privilege `ServiceAccount` with **no** Role/RoleBinding (the app needs no Kubernetes API access). Compatible with the `restricted` Pod Security Standard (documented).
- **Network segmentation via `NetworkPolicy`** (compose `data:{internal:true}` has no direct k8s analogue): default-deny ingress baseline + datastore policies admitting only `api`/`worker` pod selectors. Requires a NetworkPolicy-enforcing CNI (Calico/Cilium) — documented prerequisite; policies render regardless.
- **MCP read-only default is not weakened.** The chart only packages the `mcp-gateway` workload and its `MCP_GATEWAY_URL` wiring + egress lane; it never overrides the read-only-by-default / RFC 8707 / audit posture HARD-06 owns inside the gateway.
- **Hook least privilege.** The migrate Job runs the hardened `securityContext`, reads DB creds via `envFrom` (never argv), and is deleted on success (`hook-delete-policy`) so credentials don't linger in completed pods.
- **No agent-tool calls authored here**, so the runtime `forge_policy` evaluator is not invoked; this slice's "policy" is the **deployment hardening contract** mechanically enforced by §7 — the k8s analogue of the compose hardening contract.
- **Supply chain.** Images are `@sha256`-pinned in the prod profile (AC4); subchart versions/digests locked in `Chart.lock`; `kubernetes.md` documents verifying digests against release notes and scanning (`trivy`) before bumping. (The SBOM/SAST/secret-scan evidence pack is HARD-09; this slice feeds it the chart's pinned-digest manifest.)

## 9. Effort & risk (S/M/L + risks; what cannot be done in-sandbox)

**Effort: L.** No product code, but a wide, detail-dense surface: ~16 templates + `_helpers.tpl`, three values profiles + a JSON Schema, three pinned subchart integrations with a pgvector override, HPA/PDB/anti-affinity wiring, NetworkPolicy, ingress+TLS+webhook-raw-body, the migration hook, a helm-unittest + schema + render-contract + kubeconform static suite, and a real `kind`/`k3d` install/upgrade/rollback integration test (the most expensive piece). Plus the `kubernetes.md` rewrite.

**Risks & mitigations:**
- **Probe-contract mismatch with reality.** The real app exposes `/health`, not the `/readyz` split F24 assumed; readiness on `/health` means a pod is marked Ready before datastores are confirmed reachable. *Mitigation:* probe the real `/health`, gate Service traffic via the migrate-hook ordering + `--wait`, and track a dependency-aware `/readyz` as a small foundation follow-up (§12) flippable via `probes.readinessPath`.
- **No `forge-cli` in the ALPHA.** F24 assumed `forge-cli db migrate`; the chart uses `alembic … upgrade head` directly. *Mitigation:* `migrations.command` is a values array, so a future `forge-cli` drops in without a chart change.
- **pgvector on a Bitnami subchart.** Stock Bitnami Postgres lacks `vector`; overriding the image can break init assumptions. *Mitigation:* pin a known-good `pgvector` image + `Chart.lock`; the kind smoke asserts the `vector` extension create (via the real migration) succeeds; document managed Postgres as the production-preferred path.
- **kind/k3d integration flakiness** (image loads, slow rollouts). *Mitigation:* `--wait --timeout`, generous startup probe, self-signed `tls.secretName` for e2e (skip ACME), image-pull retries, mark slow/required-only-at-release.
- **NetworkPolicy is a no-op without an enforcing CNI** (kind's default CNI does not enforce). *Mitigation:* the policy *renders* and is unit-asserted statically; enforcement is documented as a CNI prerequisite, optionally validated on a Cilium-enabled lane (out of scope here).
- **Image availability.** This slice depends on the container-build workstream producing real images. *Mitigation:* `helm-kind` shares the build step; `web.enabled` gates the web workload so the rest installs if one image is missing.

**Cannot be done in the no-network sandbox (explicit, not hidden):**
- **The kind/k3d install + smoke + upgrade/rollback (AC18, AC19)** needs Docker, a local Kubernetes, and the built images — a **networked CI runner**, not the hermetic sandbox. The static tier (AC1–17, 20, 21) runs anywhere with `helm` installed and stays in the default suite.
- **A real cloud/managed-datastore production install** and a **multi-node HA drain test** are beyond a single-node kind cluster (kind can be multi-node but is not a real fleet) — the production-grade fleet behaviour overlaps the spec's "real multi-tenant fleet soak" honest-ceiling item (HARD-13) and is **not** claimable from kind.
- **Human pentest of the deployed surface** (ingress, secret handling, NetworkPolicy efficacy) stays a HARD-09 punch-list item — not performable by agents.

## 10. Key files / paths (exact, in the real monorepo)

```
deploy/helm/forge/Chart.yaml
deploy/helm/forge/Chart.lock
deploy/helm/forge/values.yaml
deploy/helm/forge/values-production.yaml
deploy/helm/forge/values.example.yaml
deploy/helm/forge/values.schema.json
deploy/helm/forge/README.md
deploy/helm/forge/templates/_helpers.tpl
deploy/helm/forge/templates/NOTES.txt
deploy/helm/forge/templates/serviceaccount.yaml
deploy/helm/forge/templates/configmap-env.yaml
deploy/helm/forge/templates/secret.yaml
deploy/helm/forge/templates/api/{deployment,service,hpa,pdb}.yaml
deploy/helm/forge/templates/worker/{deployment,hpa,pdb}.yaml
deploy/helm/forge/templates/mcp-gateway/{deployment,service}.yaml
deploy/helm/forge/templates/web/{deployment,service,hpa,pdb}.yaml
deploy/helm/forge/templates/ingress.yaml
deploy/helm/forge/templates/networkpolicy.yaml
deploy/helm/forge/templates/job-migrate.yaml
deploy/helm/forge/templates/tests/test-connection.yaml
deploy/helm/forge/tests/{securitycontext,resources,probes,scaling,secret_config_separation,datastore_guard,migrate_hook,ingress,networkpolicy,labels,digest}_test.yaml
deploy/helm/tests/test_values_schema.py          # @pytest.mark.helm
deploy/helm/tests/test_render_contract.py        # @pytest.mark.helm
deploy/helm/tests/test_kubernetes_doc_supported.py
deploy/helm/tests/e2e/test_kind_install.py       # @pytest.mark.kind
deploy/helm/tests/conftest.py                    # rendered()/workloads/values fixtures/kind_cluster
.github/workflows/helm-chart.yml                 # jobs: helm-static (blocking), helm-kind (release-gate)
docs/self-hosting/kubernetes.md                  # rewritten: preview -> supported
pyproject.toml / pytest.ini                      # register markers: helm, kind
```

**Referenced (owned elsewhere, not authored here):** the four Forge images from the container-build workstream (SPEC §HARD-08 *"Container & web build"*); `deploy/docker-compose.yml` + `deploy/docker/*.Dockerfile` (env/healthcheck/hardening source); `packages/db` Alembic chain + `alembic.ini` (migration hook target; HARD-01); the app `/health` handlers (`apps/api`, `apps/mcp-gateway`) and Next.js root (`apps/web`); `FORGE_SECRET_KEY`/vault (HARD-10); GitHub App PEM + webhook secret (HARD-05); `SLACK_*` (HARD-07); `docs/self-hosting/` (F15).

## 11. Research references

- **FORGE_SPEC.md** — "Self-Hosting and Deployment" (Kubernetes = 50+ engineers install path; required `kubernetes.md` doc); "Production Docker Compose Requirements" (the hardening invariants re-expressed as k8s primitives); "Monorepo Structure" (`deploy/helm/` reservation); "Phased Roadmap → Phase 2: Kubernetes Helm chart".
- **`docs/MORNING_REPORT.md`** — §0/§6 (`docker compose build` + `next build` never run; only `config` validated), §5(8) (image build + `@sha256` pinning PARKED), §1 0.4 (`/health` 200), §1 0.6 (deploy infra), §7(3) (build + pin + shellcheck as a ranked next step).
- **`scratchpad/hardening-docs/SPEC-PRODUCTION-HARDENING.md`** — blocker #3 → G-BUILD/G-IMG-PINNED; blocker #6 → migration/deploy maturity; the whole-suite green gate; credentials & secrets handling rules; the honest beta/production ceiling (no real fleet soak).
- **`docs/implementation-slices/v2/F24-kubernetes-helm.md`** — the full chart design this slice implements at hardening scope (values schema, hardening table, hook contract, helm-unittest harness, F15 docs coupling).
- **`deploy/docker-compose.yml`** — the authoritative env-var, healthcheck, network-segmentation, resource-limit, and non-root source the chart re-expresses.
- **Standard tooling (implementation-time):** Helm v3 charts & hooks https://helm.sh/docs/ ; Bitnami subcharts (postgresql/redis/minio) https://github.com/bitnami/charts ; pgvector https://github.com/pgvector/pgvector ; cert-manager https://cert-manager.io/docs/ ; kubeconform https://github.com/yannh/kubeconform ; helm-unittest https://github.com/helm-unittest/helm-unittest ; kind https://kind.sigs.k8s.io/ ; k3d https://k3d.io/ ; Kubernetes recommended labels https://kubernetes.io/docs/concepts/overview/working-with-objects/common-labels/ ; Pod Security Standards https://kubernetes.io/docs/concepts/security/pod-security-standards/ .

## 12. Out of scope / future

- **The container/image build itself + `next build` + image `@sha256` resolution** — the sibling container-build workstream (SPEC §HARD-08 *"Container & web build"*, G-BUILD); this slice **consumes** its images and pins them in prod values.
- **F24's full HA/cloud-native breadth** — **KEDA** Redis-queue worker autoscaling, the multi-profile **NetworkPolicy** matrix, **Gateway API** (`HTTPRoute`) renderer, **ServiceMonitor**/Prometheus, the **create-admin** post-install hook, a bundled **reranker** workload, pod anti-affinity across zones — remain F24/V2, extending this same chart (no parallel chart).
- **Dependency-aware `/readyz`** (readiness gated on Postgres+Redis+S3 reachability) — a small foundation (`cross-cutting/C01`) follow-up; the chart flips to it via `probes.readinessPath` when it lands.
- **`forge-cli` migration entrypoint** — the hook uses `alembic` directly today; `migrations.command` accepts a `forge-cli db migrate` drop-in later.
- **Data-preserving populated-DB upgrade→rollback→re-upgrade proof** and a **multi-tenant soak** — owned by HARD-13 (`G-MIGRATE`/`G-SOAK`); this slice proves only hook-ordering + rollback mechanics on kind.
- **Cluster backup/restore** that preserves the audit log + vault — F15 `backup.md`/`restore.md`; not a chart concern here.
- **GitOps packaging** (Argo CD `Application` / Flux `HelmRelease`), an OCI/`helm repo` publish pipeline, chart signing/provenance (cosign) — future infra slices.
- **External Secrets Operator / sealed-secrets / Vault CSI** wiring — the chart supports `existingSecret` (the integration point) but does not own the secret-sync operator config.
- **Cloud Terraform modules / managed-datastore provisioning** (RDS+pgvector, ElastiCache, S3) — the chart consumes managed endpoints via `external*`; provisioning is out of scope.
- **A real multi-node HA drain / multi-region / service-mesh topology** and a **human pentest of the deployed surface** — beyond a single-node kind cluster / not agent-performable (HARD-09 punch-list; the program's honest ceiling).
