"""Application configuration (Pydantic Settings).

Settings are read from the environment with the ``FORGE_`` prefix so they align
with the rest of the workspace (``FORGE_DATABASE_URL`` is shared with
``forge_db``). ``get_settings`` is cached so the app and its dependencies share a
single resolved instance.
"""

from __future__ import annotations

import os
import warnings
from functools import lru_cache
from typing import Any

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from forge_api import __version__

# Matches ``forge_db.session.DEFAULT_DATABASE_URL`` so the API and the data-model
# layer resolve to the same database when ``FORGE_DATABASE_URL`` is unset.
DEFAULT_DATABASE_URL = "postgresql+psycopg://forge:forge@localhost:5432/forge"
DEFAULT_REDIS_URL = "redis://localhost:6379/0"


class Settings(BaseSettings):
    """Runtime configuration for the Forge API service."""

    model_config = SettingsConfigDict(
        env_prefix="FORGE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "Forge API"
    version: str = __version__
    environment: str = "development"
    debug: bool = False

    # --- HARD-13 secrets & config hardening ---------------------------------
    # The instance master KEK. Required in production (the auth service refuses
    # to boot without it); ``FORGE_SECRET_KEY_FILE`` may point at a mounted
    # Docker/K8s secret instead (the ``_FILE`` convention, honoured by the
    # secret provider). ``SECRET_KEY``/``FORGE_ENV`` remain as one-release
    # deprecated aliases (see the model validator below).
    secret_key: str | None = None
    secret_key_version: int | None = None
    # Secret-provider backend: ``env`` (default), ``file`` (/run/secrets), or
    # ``vault`` (HashiCorp Vault KV-v2, integration-only).
    secret_provider: str = "env"
    secret_file_root: str = "/run/secrets"
    # Two-tier envelope encryption for BYOK secrets. Unset -> on in production,
    # off elsewhere (single-tier); an explicit value always wins.
    envelope_encryption: bool | None = None
    # Explicit opt-in to the dev-only process-ephemeral master key. NEVER set in
    # production — a missing FORGE_SECRET_KEY otherwise fails closed.
    dev_insecure: bool = False
    # Default lifetime (seconds) for minted agent-runner tokens (auto-expiry).
    agent_token_ttl: int = 86_400
    # HashiCorp Vault KV-v2 knobs (integration-only; secret_provider=vault).
    vault_addr: str | None = None
    vault_token: str | None = None
    vault_mount: str = "secret"
    vault_path: str = "forge"

    @model_validator(mode="before")
    @classmethod
    def _apply_legacy_aliases(cls, data: Any) -> Any:
        """Map the deprecated ``SECRET_KEY``/``FORGE_ENV`` names (one release).

        These are the names the shipped compose files set. They are honoured for
        one release with a loud deprecation warning so an existing operator's
        deployment keeps booting while they migrate to ``FORGE_SECRET_KEY`` /
        ``FORGE_ENVIRONMENT``; the canonical name always wins when both are set.
        """
        if not isinstance(data, dict):
            return data
        provided = {k.lower() for k in data}

        legacy_env = os.environ.get("FORGE_ENV")
        if legacy_env and "environment" not in provided and "FORGE_ENVIRONMENT" not in os.environ:
            warnings.warn(
                "FORGE_ENV is deprecated; set FORGE_ENVIRONMENT instead.",
                DeprecationWarning,
                stacklevel=2,
            )
            data["environment"] = legacy_env

        legacy_secret = os.environ.get("SECRET_KEY")
        if legacy_secret and "secret_key" not in provided and "FORGE_SECRET_KEY" not in os.environ:
            warnings.warn(
                "SECRET_KEY is deprecated; set FORGE_SECRET_KEY instead.",
                DeprecationWarning,
                stacklevel=2,
            )
            data["secret_key"] = legacy_secret
        return data

    # Mounted prefix for every *feature* router. ``/health`` and ``/`` stay at
    # the root so liveness probes are stable regardless of this value.
    api_prefix: str = ""

    database_url: str = DEFAULT_DATABASE_URL
    redis_url: str = DEFAULT_REDIS_URL

    # F01 board backend selection. ``memory`` (default) keeps the hermetic,
    # process-memory ``InMemoryBoardService`` (unit-test default, no Postgres);
    # ``db`` wires the Postgres-backed ``SqlAlchemyBoardService`` behind the same
    # frozen ``BoardService`` protocol. Read via ``FORGE_BOARD_BACKEND``.
    board_backend: str = "memory"

    # Observability audit-store backend selection. ``memory`` (default) keeps the
    # hermetic, process-memory ``InMemoryAuditStore`` (unit-test default, no
    # Postgres); ``db`` wires the Postgres-backed ``DbAuditStore`` behind the same
    # frozen ``AuditStore`` protocol so the platform audit trail (and the MCP
    # db-path sink) is durably persisted. Read via ``FORGE_AUDIT_BACKEND``.
    audit_backend: str = "memory"

    # F36 approval-repository backend selection. ``memory`` (default) keeps the
    # hermetic, process-memory ``InMemoryApprovalRepository`` (unit-test default,
    # no Postgres); ``db`` wires the Postgres-backed ``SqlAlchemyApprovalRepository``
    # behind the same ``ApprovalRepository`` protocol so approval gates + decisions
    # are durably persisted. Read via ``FORGE_APPROVAL_BACKEND``.
    approval_backend: str = "memory"

    # Platform API-key backend selection. ``memory`` (default) keeps the hermetic,
    # process-memory ``InMemoryAPIKeyBackend`` (unit-test default, no Postgres);
    # ``db`` wires the Postgres-backed ``DbAPIKeyBackend`` behind the same
    # ``APIKeyBackend`` seam (``add`` / ``by_prefix`` / ``list`` / ``get``) onto the
    # ``platform_api_key`` table so minted keys, revocations, and last-used stamps
    # survive a restart. Read via ``FORGE_APIKEY_BACKEND``.
    apikey_backend: str = "memory"

    # F23 traceability-projection repository backend selection. ``memory``
    # (default) keeps the hermetic, process-memory ``InMemoryProjectionRepository``
    # (unit-test default, no Postgres); ``db`` wires the Postgres-backed
    # ``SqlAlchemyProjectionRepository`` behind the same ``ProjectionRepository``
    # protocol so the F23 dashboard's denormalised projection (criterion links +
    # spec rollups, with the monotonic ``projection_version``) is durably
    # persisted. Read via ``FORGE_PROJECTION_BACKEND``.
    projection_backend: str = "memory"

    # F29 policy-audit sink backend selection. ``memory`` (default) keeps the
    # hermetic, process-memory ``InMemoryPolicyAuditSink`` (unit-test default, no
    # Postgres); ``db`` wires the Postgres-backed ``DbPolicyAuditSink`` behind the
    # same ``PolicyAuditSink`` seam so each emitted ``policy.decision`` event lands
    # durably as an append-only ``policy_rule_evaluation`` row. Read via
    # ``FORGE_POLICY_AUDIT_BACKEND``.
    policy_audit_backend: str = "memory"

    # F36 policy-override grant-store backend selection (J5). ``memory`` (default)
    # keeps the hermetic, process-memory ``InMemoryGrantStore`` (unit-test default,
    # no Postgres); ``db`` wires the Postgres-backed ``DbGrantStore`` behind the
    # same ``mint`` / ``consume`` / ``all`` grant-store seam so single-use override
    # grants survive a restart and the single-active + atomic-consume invariants
    # are enforced by the database. Read via ``FORGE_OVERRIDE_GRANT_BACKEND``.
    override_grant_backend: str = "memory"

    # Filesystem root for the spec engine's SDD artifacts (manifests, plans).
    spec_root: str = "specs"

    # Integration credentials (BYOK). Unset by default so the overnight build
    # never makes live GitHub/Slack calls; configured per workspace in prod.
    github_token: str | None = None
    github_api_url: str = "https://api.github.com"
    # Shared secret GitHub signs webhook deliveries with (``X-Hub-Signature-256``).
    # Unset by default: the webhook ingest route rejects every delivery until a
    # secret is configured (fail-closed — an unsigned webhook is never trusted).
    github_webhook_secret: str | None = None
    # --- HARD-01 live GitHub App auth ---------------------------------------
    # When ``github_app_id`` + ``github_installation_id`` are set the integration
    # router mints its own short-lived installation tokens from the App private
    # key (loaded at call time from ``github_app_private_key_path``, never stored)
    # instead of using a static ``github_token``. The ``.pem`` is a file mount, a
    # PATH — its value is never an env var, never logged, never persisted.
    github_app_id: str | None = None
    github_app_private_key_path: str = "deploy/secrets/github-app.pem"
    github_installation_id: str | None = None
    # ``owner/repo`` used ONLY by the creds-gated live integration lane.
    github_test_repo: str | None = None
    slack_token: str | None = None
    slack_default_channel: str | None = None
    # --- HARD-06 live Slack integration -------------------------------------
    # Shared secret Slack signs inbound slash-command / interactivity requests
    # with (``X-Slack-Signature`` v0). Unset by default: the two inbound routes
    # return ``501 Not Configured`` until it is set (fail-closed — an unsigned
    # Slack callback is never trusted). Its value is never logged.
    slack_signing_secret: str | None = None
    # Bounded, rate-limit-aware outbound retries (honour Retry-After / 5xx backoff).
    slack_max_retries: int = 3
    slack_retry_base_delay_seconds: float = 0.5
    # Anti-replay window for inbound Slack signatures (Slack's guidance: 5 min).
    slack_signature_max_skew_seconds: int = 300

    # Per-provider alert webhook signing secrets (F17). Unset by default so the
    # corresponding webhook returns 501 Not Configured (fail-closed): an alert is
    # only trusted when its provider secret is configured and the signature over
    # the exact raw bytes verifies.
    pagerduty_webhook_secret: str | None = None
    datadog_webhook_secret: str | None = None
    sentry_webhook_secret: str | None = None
    grafana_webhook_secret: str | None = None

    # Recovery-monitoring knobs (F17).
    incident_recovery_window_seconds: int = 300
    incident_recovery_max_windows: int = 6

    # CORS — explicit list of allowed origins (JSON-encoded in the env var).
    # Locked down by default: no cross-origin access until a deployment names its
    # web origin(s). A wildcard ("*") is never combined with credentials (see
    # ``forge_api.main.create_app``) because that is a credential-leak vector.
    cors_origins: list[str] = []
    cors_allow_credentials: bool = True

    # OpenAPI docs (/docs, /redoc, /openapi.json). In production the pages are
    # forced OFF unless FORGE_DOCS_ENABLED is *explicitly* set (info-disclosure
    # reduction, HARD-09); see ``docs_effectively_enabled``.
    docs_enabled: bool = True

    # --- HARD-09 edge security controls -------------------------------------
    # Per-caller request rate limit (token bucket, per-process). Keyed by the
    # presented API credential, falling back to client IP; /health is exempt.
    ratelimit_enabled: bool = True
    ratelimit_rpm: int = 120
    ratelimit_burst: int = 60
    # HARD-11: per-route rate-limit overrides for the expensive hot paths, as a
    # JSON map ``{route: "N/window"}`` (e.g. {"/knowledge/search": "30/minute"}).
    # Empty by default -> every route uses the default budget above.
    ratelimit_overrides: dict[str, str] = {}
    # Maximum request body size before 413 (1 MiB default).
    max_body_bytes: int = 1_048_576

    # --- HARD-11 reliability primitives -------------------------------------
    # Request idempotency: a retry carrying the same ``Idempotency-Key`` returns
    # the first response and runs the side effect once. Default-on; no-ops when a
    # request carries no key.
    idempotency_enabled: bool = True
    idempotency_ttl_seconds: int = 86_400
    # Graceful-shutdown request drain grace: on SIGTERM readiness flips to 503 and
    # the app waits up to this long for in-flight requests before tearing down.
    shutdown_drain_seconds: int = 30
    # Promote readiness dependency checks (DB/Redis ping) to a hard gate. Off by
    # default so a dev/test instance stays ready without live backends; set true
    # in production so /health/ready reflects real dependency health.
    readiness_require_deps: bool = False
    # SSRF guard for admin-configured outbound URLs (embedder/reranker/MCP).
    # ``ssrf_allow_private`` opts self-hosted deployments into their own
    # private-network endpoints (loopback + metadata stay blocked);
    # ``outbound_allowlist`` names exact hostnames always permitted.
    ssrf_allow_private: bool = False
    outbound_allowlist: list[str] = []

    # --- HARD-03 live cross-encoder reranker --------------------------------
    # Process-level reranker selection (BYOK keys are NOT stored here — they are
    # read on demand from the vault/env so they never land in a logged Settings
    # field). ``fixture`` (default) keeps the offline, deterministic, network-free
    # reranker; ``jina``/``cohere``/``selfhosted`` build a budgeted, SSRF-guarded
    # live client. ``FORGE_RERANK_ENABLED=false`` -> weighted-RRF only, no client.
    rerank_enabled: bool = True
    rerank_provider: str = "fixture"
    rerank_model: str | None = None
    # SSRF-validated base_url override; self-hosted also honours JINA_RERANKER_URL.
    rerank_base_url: str | None = None
    # Per-call latency budget (ms); exceeding it degrades to weighted-RRF.
    rerank_timeout_ms: int = 800
    # Max documents sent to the reranker per call (DoS bound).
    rerank_candidates: int = 50
    # Required to point a self-hosted reranker at a non-private (public) host.
    rerank_allow_insecure_url: bool = False

    @property
    def docs_effectively_enabled(self) -> bool:
        """Whether the OpenAPI doc pages should be served.

        Production forces docs off unless the operator explicitly set
        ``FORGE_DOCS_ENABLED`` (the field appearing in ``model_fields_set``);
        every other environment honours the plain flag.
        """
        if self.environment == "production" and "docs_enabled" not in self.model_fields_set:
            return False
        return self.docs_enabled

    # F25 — workflow engine selection. ``postgres_fsm`` (default) keeps the V1
    # in-process FSM; ``temporal`` wires the V2 durable Temporal engine. The
    # detailed Temporal connection settings live in
    # ``forge_workflow.temporal.config.TemporalSettings`` (env vars without the
    # FORGE_ prefix: WORKFLOW_ENGINE_BACKEND, TEMPORAL_*), the single source of
    # truth shared with both workers + the CLI.
    workflow_engine_backend: str = "postgres_fsm"

    # F33 — enterprise SSO (SAML + SCIM). ``public_url`` must be the externally
    # reachable HTTPS URL in production: the SP entity id, ACS URL, SP metadata
    # URL, and SCIM base URL are all derived from it.
    public_url: str = "http://localhost:8000"
    saml_clock_skew_seconds: int = 120
    saml_authnrequest_ttl_seconds: int = 600
    scim_token_bytes: int = 32

    # F35 — benchmark suite & public leaderboard. The public, unauthenticated,
    # read-only ``/public/*`` router is DISABLED by default (self-hosted privacy:
    # a fresh instance never accidentally publishes internal eval data). Env vars
    # carry the workspace-wide FORGE_ prefix (deviation from the F35 draft's
    # bare names, conforming to every other setting here).
    public_leaderboard_enabled: bool = False
    # Filesystem root holding <slug>/<version>/manifest.yaml benchmark suites;
    # None -> the suites packaged inside forge_eval (benchmarks/).
    benchmark_dir: str | None = None
    benchmark_verify_epsilon: float = 0.005
    benchmark_submission_max_bytes: int = 52_428_800
    # Per-IP request budget per 60s window on the public leaderboard routes.
    leaderboard_public_rate_limit: int = 60
    leaderboard_cache_ttl_seconds: int = 60


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide cached :class:`Settings` instance."""
    return Settings()
