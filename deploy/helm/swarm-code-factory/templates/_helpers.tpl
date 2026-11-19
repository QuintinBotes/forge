{{/*
Common helpers for the SCF chart: names, labels, image refs, env wiring and
the bundled-vs-external datastore guards.
*/}}

{{/* Base name (overridable). */}}
{{- define "swarm-code-factory.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* Fully-qualified release name. */}}
{{- define "swarm-code-factory.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "swarm-code-factory.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* Common (recommended) labels applied to every object. */}}
{{- define "swarm-code-factory.labels" -}}
helm.sh/chart: {{ include "swarm-code-factory.chart" . }}
{{ include "swarm-code-factory.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/part-of: forge
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{/* Immutable selector labels (instance scope). */}}
{{- define "swarm-code-factory.selectorLabels" -}}
app.kubernetes.io/name: {{ include "swarm-code-factory.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
Per-workload labels. Call with (dict "ctx" . "component" "api").
Adds the immutable component label (part of the selector) plus the common set.
*/}}
{{- define "swarm-code-factory.componentLabels" -}}
{{- $ctx := .ctx -}}
{{ include "swarm-code-factory.labels" $ctx }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{/* Per-workload selector labels (immutable subset). */}}
{{- define "swarm-code-factory.componentSelectorLabels" -}}
{{- $ctx := .ctx -}}
{{ include "swarm-code-factory.selectorLabels" $ctx }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{/*
Build a workload image ref. Call with (dict "ctx" . "wl" .Values.api).
Prefers an immutable @sha256 digest; falls back to repository:tag (chart
appVersion when tag is empty). Honours a shared image.registry / global registry.
*/}}
{{- define "swarm-code-factory.image" -}}
{{- $ctx := .ctx -}}
{{- $wl := .wl -}}
{{- $registry := "" -}}
{{- if and $ctx.Values.global $ctx.Values.global.imageRegistry -}}
{{- $registry = $ctx.Values.global.imageRegistry -}}
{{- else if and $ctx.Values.image $ctx.Values.image.registry -}}
{{- $registry = $ctx.Values.image.registry -}}
{{- end -}}
{{- $repo := $wl.image.repository -}}
{{- $ref := "" -}}
{{- if $wl.image.digest -}}
{{- $ref = printf "%s@%s" $repo $wl.image.digest -}}
{{- else -}}
{{- $ref = printf "%s:%s" $repo (default $ctx.Chart.AppVersion $wl.image.tag) -}}
{{- end -}}
{{- if $registry -}}
{{- printf "%s/%s" $registry $ref -}}
{{- else -}}
{{- $ref -}}
{{- end -}}
{{- end -}}

{{/* Name of the Secret carrying secret env (created or external). */}}
{{- define "swarm-code-factory.secretName" -}}
{{- if .Values.secrets.create -}}
{{- printf "%s-secret" (include "swarm-code-factory.fullname" .) -}}
{{- else -}}
{{- required "secrets.create is false: secrets.existingSecret must name an existing Secret" .Values.secrets.existingSecret -}}
{{- end -}}
{{- end -}}

{{- define "swarm-code-factory.configMapName" -}}
{{- printf "%s-env" (include "swarm-code-factory.fullname" .) -}}
{{- end -}}

{{- define "swarm-code-factory.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "swarm-code-factory.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/* image pull secrets block (global). */}}
{{- define "swarm-code-factory.imagePullSecrets" -}}
{{- if and .Values.global .Values.global.imagePullSecrets -}}
imagePullSecrets:
{{ toYaml .Values.global.imagePullSecrets | indent 2 }}
{{- end -}}
{{- end -}}

{{/*
envFrom block shared by every workload + hook: non-secret ConfigMap then Secret.
*/}}
{{- define "swarm-code-factory.envFrom" -}}
- configMapRef:
    name: {{ include "swarm-code-factory.configMapName" . }}
- secretRef:
    name: {{ include "swarm-code-factory.secretName" . }}
{{- end -}}

{{/* ----------------------------------------------------------------------- */}}
{{/* Datastore bundled-XOR-external guards (fail fast at template time).      */}}
{{/* ----------------------------------------------------------------------- */}}

{{- define "swarm-code-factory.validateDatastores" -}}
{{- $pgBundled := .Values.postgresql.enabled -}}
{{- $pgExternal := and .Values.externalDatabase .Values.externalDatabase.host -}}
{{- if and $pgBundled $pgExternal -}}
{{- fail "Configure either bundled or external postgres, not both: set postgresql.enabled=false when externalDatabase.host is set." -}}
{{- end -}}
{{- if and (not $pgBundled) (not $pgExternal) -}}
{{- fail "Configure either bundled or external postgres, not neither: set postgresql.enabled=true or externalDatabase.host." -}}
{{- end -}}
{{- $redisBundled := .Values.redis.enabled -}}
{{- $redisExternal := and .Values.externalRedis .Values.externalRedis.host -}}
{{- if and $redisBundled $redisExternal -}}
{{- fail "Configure either bundled or external redis, not both: set redis.enabled=false when externalRedis.host is set." -}}
{{- end -}}
{{- if and (not $redisBundled) (not $redisExternal) -}}
{{- fail "Configure either bundled or external redis, not neither: set redis.enabled=true or externalRedis.host." -}}
{{- end -}}
{{- $minioBundled := .Values.minio.enabled -}}
{{- $minioExternal := and .Values.externalObjectStore .Values.externalObjectStore.endpoint -}}
{{- if and $minioBundled $minioExternal -}}
{{- fail "Configure either bundled or external object store, not both: set minio.enabled=false when externalObjectStore.endpoint is set." -}}
{{- end -}}
{{- if and (not $minioBundled) (not $minioExternal) -}}
{{- fail "Configure either bundled or external object store, not neither: set minio.enabled=true or externalObjectStore.endpoint." -}}
{{- end -}}
{{- end -}}

{{/*
Validate the secret surface. When the chart creates the Secret, the boot-critical
keys must be non-empty (mirrors F37 fail-closed). When using an existingSecret,
its name is required (and the same keys are the operator's responsibility).
*/}}
{{- define "swarm-code-factory.validateSecrets" -}}
{{- if .Values.secrets.create -}}
{{- $data := .Values.secrets.data | default dict -}}
{{- /* The BYOK model key (FORGE_MODEL_API_KEY) is intentionally NOT here: the
       worker/api boot fine without it (offline scripted fallback), so it is
       optional, not fail-closed. */ -}}
{{- range $key := (list "SECRET_KEY" "AUTH_SECRET" "FORGE_VAULT_KEYS" "API_KEY_PEPPER" "INTERNAL_SERVICE_TOKEN") -}}
{{- if not (get $data $key) -}}
{{- fail (printf "secrets.data.%s is required (api/worker fail closed without it — F37). Generate one (e.g. openssl rand -hex 32) and pass it via --set or a values file." $key) -}}
{{- end -}}
{{- end -}}
{{- else -}}
{{- $_ := required "secrets.create is false: secrets.existingSecret must name an existing Secret carrying the boot-critical keys (F37)." .Values.secrets.existingSecret -}}
{{- end -}}
{{- end -}}

{{/*
Validate ingress/TLS coherence: TLS on requires either cert-manager or a TLS
secret name.
*/}}
{{- define "swarm-code-factory.validateIngress" -}}
{{- if and .Values.ingress.enabled .Values.ingress.tls.enabled -}}
{{- $cm := .Values.ingress.tls.certManager -}}
{{- if and (not (and $cm $cm.enabled)) (not .Values.ingress.tls.secretName) -}}
{{- fail "ingress.tls.enabled=true requires either ingress.tls.certManager.enabled=true or ingress.tls.secretName." -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
Validate the durable engine: the chart bundles no Temporal server, so enabling it
needs an external frontend, and a chart-created Secret needs the codec key (an
existingSecret must carry it; that is the operator's responsibility).
*/}}
{{- define "swarm-code-factory.validateTemporal" -}}
{{- if .Values.temporal.enabled -}}
{{- $_ := required "temporal.enabled=true requires temporal.host: the host:port of an external Temporal frontend (this chart does not bundle a Temporal server)." .Values.temporal.host -}}
{{- if and .Values.secrets.create (not (get (.Values.secrets.data | default dict) "TEMPORAL_CODEC_KEY")) -}}
{{- fail "temporal.enabled=true requires secrets.data.TEMPORAL_CODEC_KEY (workflow payloads are encrypted with it). Generate one (e.g. openssl rand -hex 32) and pass it via --set or a values file." -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/* Run all validations once, from a single included template. */}}
{{- define "swarm-code-factory.validate" -}}
{{- include "swarm-code-factory.validateDatastores" . -}}
{{- include "swarm-code-factory.validateSecrets" . -}}
{{- include "swarm-code-factory.validateIngress" . -}}
{{- include "swarm-code-factory.validateTemporal" . -}}
{{- end -}}

{{/* Non-secret Postgres connection host (bundled service or external host). */}}
{{- define "swarm-code-factory.postgresHost" -}}
{{- if .Values.postgresql.enabled -}}
{{- printf "%s-postgresql" (include "swarm-code-factory.fullname" .) -}}
{{- else -}}
{{- .Values.externalDatabase.host -}}
{{- end -}}
{{- end -}}

{{- define "swarm-code-factory.redisHost" -}}
{{- if .Values.redis.enabled -}}
{{- printf "%s-redis-master" (include "swarm-code-factory.fullname" .) -}}
{{- else -}}
{{- .Values.externalRedis.host -}}
{{- end -}}
{{- end -}}

{{/* NOTE: no forge.minioEndpoint helper — object-store endpoint/bucket env is
     PARKED with the object-storage scope (no reader yet). Bundled/external
     object-store CREDS still flow via the Secret + the minio subchart /
     externalObjectStore config. */}}

{{- define "swarm-code-factory.publicUrl" -}}
{{- default (printf "https://%s" .Values.forge.domain) .Values.forge.publicUrl -}}
{{- end -}}

{{- define "swarm-code-factory.rerankerUrl" -}}
{{- if .Values.reranker.enabled -}}
{{- printf "http://%s-reranker:%d" (include "swarm-code-factory.fullname" .) (int .Values.reranker.port) -}}
{{- else if .Values.reranker.external.url -}}
{{- .Values.reranker.external.url -}}
{{- end -}}
{{- end -}}
