# forge

A Helm chart that installs a horizontally-scalable, highly-available Forge
(api / worker / web / mcp-gateway) onto Kubernetes >= 1.28, with optional bundled
datastores (pgvector / redis / minio) or external/managed Postgres, Redis and S3.

See the full operator guide: [`docs/self-hosting/kubernetes.md`](../../../docs/self-hosting/kubernetes.md).

## TL;DR

```bash
helm dependency build deploy/helm/forge
helm install forge deploy/helm/forge -n forge --create-namespace \
  --set forge.domain=forge.example.com \
  --set-string secrets.data.SECRET_KEY="$(openssl rand -hex 32)" \
  --set-string secrets.data.AUTH_SECRET="$(openssl rand -hex 32)" \
  --set-string secrets.data.API_KEY_PEPPER="$(openssl rand -hex 32)" \
  --set-string secrets.data.INTERNAL_SERVICE_TOKEN="$(openssl rand -hex 32)" \
  --set-string secrets.data.MODEL_PROVIDER_KEY="$MODEL_KEY"
```

## Source / dependencies

| Repository | Name | Version |
|---|---|---|
| https://charts.bitnami.com/bitnami | postgresql (`condition: postgresql.enabled`) | 18.7.8 |
| https://charts.bitnami.com/bitnami | redis (`condition: redis.enabled`) | 27.0.12 |
| https://charts.bitnami.com/bitnami | minio (`condition: minio.enabled`) | 17.0.21 |

The bundled Postgres is configured to use a `pgvector/pgvector` image so hybrid
retrieval (F05) has the `vector` extension.

## Values

| Key | Default | Description |
|---|---|---|
| `forge.domain` | `forge.example.com` | Public DNS host (Ingress). **Required.** |
| `forge.publicUrl` | `https://{domain}` | Public base URL. |
| `forge.logLevel` | `info` | `debug`/`info`/`warning`/`error`. |
| `image.registry` | `ghcr.io/forge-platform` | Registry for the four images. |
| `image.pullPolicy` | `IfNotPresent` | Image pull policy. |
| `api`/`worker`/`web`/`mcpGateway`.`replicaCount` | `1` | Replicas (HPA overrides when `autoscaling.enabled`). |
| `*.image.{repository,tag,digest}` | per workload | Image ref; set `digest` (`sha256:…`) in production. |
| `*.resources.{requests,limits}.{cpu,memory}` | per workload | Required by the values schema. |
| `*.autoscaling` | enabled | CPU `HorizontalPodAutoscaler`. |
| `*.pdb` | enabled, `minAvailable: 1` | `PodDisruptionBudget`. |
| `worker.keda` | disabled | KEDA `ScaledObject` on Redis list length. |
| `secrets.create` | `true` | Render an in-chart Secret (dev). Set `false` + `secrets.existingSecret` in production. |
| `secrets.existingSecret` | `""` | Operator-managed Secret name. |
| `secrets.data.*` | dev placeholders | Boot-critical + integration secret keys. |
| `postgresql.enabled` / `externalDatabase.host` | bundled on | Bundled XOR external Postgres. |
| `redis.enabled` / `externalRedis.host` | bundled on | Bundled XOR external Redis. |
| `minio.enabled` / `externalObjectStore.endpoint` | bundled on | Bundled XOR external object store. |
| `reranker.enabled` / `reranker.external.url` | disabled | Self-hosted or external Jina reranker (F05). |
| `ingress.enabled` | `true` | Render an `Ingress` (or `HTTPRoute` when `ingress.api: gateway`). |
| `ingress.tls.certManager.enabled` | `true` | cert-manager-issued TLS. |
| `ingress.tls.secretName` | `""` | Existing TLS secret (alternative to cert-manager). |
| `networkPolicy.enabled` | `true` | Default-deny + data-tier allowlist. |
| `migrations.enabled` | `true` | Pre-install/pre-upgrade `alembic upgrade head` hook (override via `migrations.command`). |
| `createAdmin.enabled` | `false` | Opt-in post-install first-admin hook. |
| `metrics.serviceMonitor.enabled` | `false` | Prometheus Operator `ServiceMonitor`. |

`values.schema.json` validates this surface at `helm install` time. Profiles:
`values.yaml` (defaults, bundled), `values.example.yaml` (minimal eval),
`values-production.yaml` (managed + HA + digest-pinned).

## Testing

```bash
helm lint deploy/helm/forge
helm template forge deploy/helm/forge | kubeconform -strict -ignore-missing-schemas
helm unittest deploy/helm/forge
```
