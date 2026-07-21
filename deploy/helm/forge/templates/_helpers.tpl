{{/*
Common helpers for the Forge chart: names, labels, image refs, env wiring and
the bundled-vs-external datastore guards.
*/}}

{{/* Base name (overridable). */}}
{{- define "forge.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* Fully-qualified release name. */}}
{{- define "forge.fullname" -}}
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

{{- define "forge.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* Common (recommended) labels applied to every object. */}}
{{- define "forge.labels" -}}
helm.sh/chart: {{ include "forge.chart" . }}
{{ include "forge.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/part-of: forge
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{/* Immutable selector labels (instance scope). */}}
{{- define "forge.selectorLabels" -}}
app.kubernetes.io/name: {{ include "forge.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
Per-workload labels. Call with (dict "ctx" . "component" "api").
Adds the immutable component label (part of the selector) plus the common set.
*/}}
{{- define "forge.componentLabels" -}}
{{- $ctx := .ctx -}}
{{ include "forge.labels" $ctx }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{/* Per-workload selector labels (immutable subset). */}}
{{- define "forge.componentSelectorLabels" -}}
{{- $ctx := .ctx -}}
{{ include "forge.selectorLabels" $ctx }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{/*
Build a workload image ref. Call with (dict "ctx" . "wl" .Values.api).
Prefers an immutable @sha256 digest; falls back to repository:tag (chart
appVersion when tag is empty). Honours a shared image.registry / global registry.
*/}}
{{- define "forge.image" -}}
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
{{- define "forge.secretName" -}}
{{- if .Values.secrets.create -}}
{{- printf "%s-secret" (include "forge.fullname" .) -}}
{{- else -}}
{{- required "secrets.create is false: secrets.existingSecret must name an existing Secret" .Values.secrets.existingSecret -}}
{{- end -}}
{{- end -}}

{{- define "forge.configMapName" -}}
{{- printf "%s-env" (include "forge.fullname" .) -}}
{{- end -}}

{{- define "forge.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "forge.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/* image pull secrets block (global). */}}
{{- define "forge.imagePullSecrets" -}}
{{- if and .Values.global .Values.global.imagePullSecrets -}}
imagePullSecrets:
{{ toYaml .Values.global.imagePullSecrets | indent 2 }}
{{- end -}}
{{- end -}}

{{/*
envFrom block shared by every workload + hook: non-secret ConfigMap then Secret.
*/}}
{{- define "forge.envFrom" -}}
- configMapRef:
    name: {{ include "forge.configMapName" . }}
- secretRef:
    name: {{ include "forge.secretName" . }}
{{- end -}}

{{/* ----------------------------------------------------------------------- */}}
{{/* Datastore bundled-XOR-external guards (fail fast at template time).      */}}
{{/* ----------------------------------------------------------------------- */}}

{{- define "forge.validateDatastores" -}}
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
{{- define "forge.validateSecrets" -}}
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
{{- define "forge.validateIngress" -}}
{{- if and .Values.ingress.enabled .Values.ingress.tls.enabled -}}
{{- $cm := .Values.ingress.tls.certManager -}}
{{- if and (not (and $cm $cm.enabled)) (not .Values.ingress.tls.secretName) -}}
{{- fail "ingress.tls.enabled=true requires either ingress.tls.certManager.enabled=true or ingress.tls.secretName." -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/* Run all validations once, from a single included template. */}}
{{- define "forge.validate" -}}
{{- include "forge.validateDatastores" . -}}
{{- include "forge.validateSecrets" . -}}
{{- include "forge.validateIngress" . -}}
{{- end -}}

{{/* Non-secret Postgres connection host (bundled service or external host). */}}
{{- define "forge.postgresHost" -}}
{{- if .Values.postgresql.enabled -}}
{{- printf "%s-postgresql" (include "forge.fullname" .) -}}
{{- else -}}
{{- .Values.externalDatabase.host -}}
{{- end -}}
{{- end -}}

{{- define "forge.redisHost" -}}
{{- if .Values.redis.enabled -}}
{{- printf "%s-redis-master" (include "forge.fullname" .) -}}
{{- else -}}
{{- .Values.externalRedis.host -}}
{{- end -}}
{{- end -}}

{{/* NOTE: no forge.minioEndpoint helper — object-store endpoint/bucket env is
     PARKED with the object-storage scope (no reader yet). Bundled/external
     object-store CREDS still flow via the Secret + the minio subchart /
     externalObjectStore config. */}}

{{- define "forge.publicUrl" -}}
{{- default (printf "https://%s" .Values.forge.domain) .Values.forge.publicUrl -}}
{{- end -}}

{{- define "forge.rerankerUrl" -}}
{{- if .Values.reranker.enabled -}}
{{- printf "http://%s-reranker:%d" (include "forge.fullname" .) (int .Values.reranker.port) -}}
{{- else if .Values.reranker.external.url -}}
{{- .Values.reranker.external.url -}}
{{- end -}}
{{- end -}}
