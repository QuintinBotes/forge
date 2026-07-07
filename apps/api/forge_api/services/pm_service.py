"""Persistence + orchestration for the PM-adapter router (F18).

Owns ``pm_connection`` / ``pm_task_link`` / ``pm_webhook_delivery`` rows, stores
provider secrets in the F37 vault (never in a column/response), builds adapters
through an injectable transport factory (so tests stay socket-free), verifies +
dedupes inbound webhooks, and writes an immutable audit entry per accepted
webhook / health probe.

Board-write execution (the worker's ``process_webhook`` -> re-fetch ->
``sync_in`` and the outbound ``activity_events`` scan) is intentionally **not**
performed here — see module notes / the slice report: it depends on the F01
Postgres board substrate (``activity_events`` outbox + versioned task service)
which is not present in this foundation. The engine that performs it lives in
``forge_integrations.pm.sync_engine`` and is fully unit-tested against fakes.
"""

from __future__ import annotations

import builtins
import hashlib
import secrets
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from forge_api.auth.vault import SecretNotFoundError, SecretVault
from forge_api.observability.audit import AuditCategory, AuditLog
from forge_contracts.enums import APIKeyKind
from forge_contracts.pm import (
    AdapterContext,
    HealthResult,
    PMAdapter,
    PMConnectionConfig,
    WebhookEvent,
)
from forge_contracts.pm import PMProvider as ContractsProvider
from forge_db.models.enums import (
    PMAuthType,
    PMConflictPolicy,
    PMConnectionStatus,
    PMDeliveryStatus,
    PMProvider,
    PMSyncDirection,
    PMSyncState,
)
from forge_db.models.pm import PMConnection, PMTaskLink, PMWebhookDelivery
from forge_integrations.pm import PMError, build_adapter
from forge_integrations.pm.jira import auth as jira_auth
from forge_integrations.pm.jira import mapping as jira_mapping
from forge_integrations.pm.linear import auth as linear_auth
from forge_integrations.pm.linear import mapping as linear_mapping
from forge_integrations.pm.transport import (
    HttpxJiraTransport,
    HttpxLinearTransport,
    PMTransport,
)


class PMConnectionNotFound(LookupError):
    """Raised when a connection id is absent in the caller's workspace."""


class PMConflictExists(ValueError):
    """Raised when a (project_id, provider) connection already exists."""


def _default_transport_factory(connection: PMConnection) -> PMTransport:  # pragma: no cover
    if connection.provider == PMProvider.JIRA:
        base = (
            jira_auth.cloud_api_base(connection.jira_cloud_id or "")
            if (connection.jira_cloud_id)
            else (connection.external_base_url or "")
        )
        return HttpxJiraTransport(base_url=base)
    return HttpxLinearTransport()


def _now() -> datetime:
    return datetime.now(UTC)


class PMConnectionService:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        vault: SecretVault,
        audit: AuditLog,
        transport_factory: Callable[[PMConnection], PMTransport] = _default_transport_factory,
    ) -> None:
        self._sf = session_factory
        self._vault = vault
        self._audit = audit
        self._transport_factory = transport_factory

    # ------------------------------------------------------------------ #
    # connection CRUD                                                     #
    # ------------------------------------------------------------------ #

    def create(self, workspace_id: uuid.UUID, cfg: PMConnectionConfig) -> PMConnection:
        provider = PMProvider(cfg.provider.value)
        with self._sf() as session:
            existing = session.execute(
                select(PMConnection).where(
                    PMConnection.project_id == cfg.project_id,
                    PMConnection.provider == provider,
                )
            ).scalar_one_or_none()
            if existing is not None:
                raise PMConflictExists(
                    f"connection for project {cfg.project_id} + {provider.value} exists"
                )

            credential_ref = None
            account_label = None
            if cfg.api_token:
                info = self._vault.put_secret(
                    workspace_id=workspace_id,
                    name=f"pm:{cfg.provider.value}:{cfg.external_project_key}",
                    secret=cfg.api_token,
                    kind=APIKeyKind.INTEGRATION_TOKEN,
                    provider=cfg.provider.value,
                )
                credential_ref = str(info.id)
                account_label = cfg.api_token_email

            # Per-connection webhook signing secret -> vault.
            webhook_secret = secrets.token_hex(32)
            wh_info = self._vault.put_secret(
                workspace_id=workspace_id,
                name=f"pm-webhook:{cfg.provider.value}:{cfg.external_project_key}",
                secret=webhook_secret,
                kind=APIKeyKind.INTEGRATION_TOKEN,
                provider=cfg.provider.value,
            )

            config: dict = {}
            if cfg.api_token_email:
                config["api_token_email"] = cfg.api_token_email
            config["on_external_delete"] = cfg.on_external_delete

            connection = PMConnection(
                workspace_id=workspace_id,
                provider=provider,
                name=cfg.name,
                project_id=cfg.project_id,
                external_base_url=cfg.external_base_url,
                external_project_key=cfg.external_project_key,
                external_project_id=cfg.external_project_key,  # resolved live post-merge
                auth_type=PMAuthType(cfg.auth_type),
                credential_ref=credential_ref,
                account_label=account_label,
                granted_scopes=[],
                sync_direction=PMSyncDirection(cfg.sync_direction.value),
                conflict_policy=PMConflictPolicy(cfg.conflict_policy.value),
                status_map=cfg.status_map or self._default_status_map(provider),
                priority_map=cfg.priority_map or self._default_priority_map(provider),
                field_map=cfg.field_map,
                webhook_secret_ref=str(wh_info.id),
                status=PMConnectionStatus.PENDING,
                config=config,
            )
            session.add(connection)
            session.commit()
            session.refresh(connection)
            session.expunge(connection)
            return connection

    def list(self, workspace_id: uuid.UUID) -> list[PMConnection]:
        with self._sf() as session:
            rows = (
                session.execute(
                    select(PMConnection)
                    .where(PMConnection.workspace_id == workspace_id)
                    .order_by(PMConnection.created_at)
                )
                .scalars()
                .all()
            )
            for r in rows:
                session.expunge(r)
            return list(rows)

    def get(self, workspace_id: uuid.UUID, connection_id: uuid.UUID) -> PMConnection:
        with self._sf() as session:
            conn = self._load(session, workspace_id, connection_id)
            session.expunge(conn)
            return conn

    def link_counts(self, connection_id: uuid.UUID) -> dict[str, int]:
        with self._sf() as session:
            links = (
                session.execute(select(PMTaskLink).where(PMTaskLink.connection_id == connection_id))
                .scalars()
                .all()
            )
        counts: dict[str, int] = {}
        for link in links:
            counts[link.sync_state.value] = counts.get(link.sync_state.value, 0) + 1
        return counts

    def patch(
        self,
        workspace_id: uuid.UUID,
        connection_id: uuid.UUID,
        *,
        name: str | None = None,
        status_map: dict | None = None,
        priority_map: dict | None = None,
        field_map: dict | None = None,
        sync_direction: PMSyncDirection | None = None,
        conflict_policy: PMConflictPolicy | None = None,
        enabled: bool | None = None,
    ) -> PMConnection:
        with self._sf() as session:
            conn = self._load(session, workspace_id, connection_id)
            if name is not None:
                conn.name = name
            if status_map is not None:
                conn.status_map = status_map
            if priority_map is not None:
                conn.priority_map = priority_map
            if field_map is not None:
                conn.field_map = field_map
            if sync_direction is not None:
                conn.sync_direction = sync_direction
            if conflict_policy is not None:
                conn.conflict_policy = conflict_policy
            if enabled is not None:
                conn.status = (
                    PMConnectionStatus.CONNECTED if enabled else PMConnectionStatus.DISABLED
                )
            session.commit()
            session.refresh(conn)
            session.expunge(conn)
            return conn

    def disconnect(self, workspace_id: uuid.UUID, connection_id: uuid.UUID) -> PMConnection:
        """Best-effort unregister webhook + mark disabled; retain links + audit."""
        with self._sf() as session:
            conn = self._load(session, workspace_id, connection_id)
            conn.status = PMConnectionStatus.DISABLED
            session.commit()
            session.refresh(conn)
            session.expunge(conn)
        self._audit.record(
            category=AuditCategory.SYSTEM,
            action="pm_disconnect",
            workspace_id=workspace_id,
            connection_id=str(connection_id),
        )
        return conn

    # ------------------------------------------------------------------ #
    # health                                                             #
    # ------------------------------------------------------------------ #

    async def test_connection(
        self, workspace_id: uuid.UUID, connection_id: uuid.UUID
    ) -> HealthResult:
        with self._sf() as session:
            conn = self._load(session, workspace_id, connection_id)
            session.expunge(conn)
        adapter = self._build_adapter(conn)
        start = time.perf_counter()
        health = await adapter.get_connection_health()
        latency = (time.perf_counter() - start) * 1000
        with self._sf() as session:
            live = self._load(session, workspace_id, connection_id)
            live.last_health_at = _now()
            if health.status == "connected":
                live.status = PMConnectionStatus.CONNECTED
                live.account_label = health.account or live.account_label
                live.granted_scopes = health.granted_scopes or live.granted_scopes
                live.last_health_error = None
            else:
                live.status = PMConnectionStatus.ERROR
                live.last_health_error = health.error
            session.commit()
        self._audit.record(
            category=AuditCategory.TOOL_CALL,
            action="pm_health",
            workspace_id=workspace_id,
            connection_id=str(connection_id),
            status="ok" if health.status == "connected" else "error",
            latency_ms=int(latency),
        )
        return health

    # ------------------------------------------------------------------ #
    # webhook intake                                                     #
    # ------------------------------------------------------------------ #

    def get_connection_any_workspace(self, connection_id: uuid.UUID) -> PMConnection | None:
        with self._sf() as session:
            conn = session.get(PMConnection, connection_id)
            if conn is not None:
                session.expunge(conn)
            return conn

    def receive_webhook(
        self, connection: PMConnection, body: bytes, headers: dict[str, str]
    ) -> tuple[int, WebhookEvent | None]:
        """Verify -> dedupe -> persist a delivery. Returns ``(status_code, event)``.

        The payload is a *hint*; the worker re-fetches authoritative state before
        any board write (parked — see module docstring). 401 on bad signature.
        """
        adapter = self._build_adapter(connection)
        secret = self._webhook_secret(connection)
        if not adapter.verify_webhook(body, headers, secret or ""):
            return 401, None

        event = adapter.parse_webhook(body, headers)
        disabled = connection.status == PMConnectionStatus.DISABLED
        outbound_only = connection.sync_direction == PMSyncDirection.OUTBOUND_ONLY
        status = PMDeliveryStatus.RECEIVED
        if disabled or outbound_only:
            status = PMDeliveryStatus.SKIPPED

        payload_hash = hashlib.sha256(body).hexdigest()
        with self._sf() as session:
            dupe = session.execute(
                select(PMWebhookDelivery).where(PMWebhookDelivery.delivery_id == event.delivery_id)
            ).scalar_one_or_none()
            if dupe is not None:
                return 202, event
            delivery = PMWebhookDelivery(
                provider=connection.provider,
                connection_id=connection.id,
                delivery_id=event.delivery_id,
                event_type=event.event_type,
                external_id=event.external_id,
                payload_hash=payload_hash,
                signature_valid=True,
                received_at=_now(),
                status=status,
            )
            session.add(delivery)
            session.commit()

        self._audit.record(
            category=AuditCategory.SYSTEM,
            action="pm_webhook",
            connection_id=str(connection.id),
            target=event.event_type,
            status=status.value,
            payload_hash=payload_hash,
        )
        # NOTE: enqueue of pm.process_webhook is parked (worker board-write path).
        return 202, event

    # ------------------------------------------------------------------ #
    # links                                                              #
    # ------------------------------------------------------------------ #

    def list_links(
        self,
        workspace_id: uuid.UUID,
        connection_id: uuid.UUID,
        *,
        state: PMSyncState | None = None,
    ) -> builtins.list[PMTaskLink]:
        with self._sf() as session:
            self._load(session, workspace_id, connection_id)  # tenant check / 404
            stmt = select(PMTaskLink).where(PMTaskLink.connection_id == connection_id)
            if state is not None:
                stmt = stmt.where(PMTaskLink.sync_state == state)
            rows = session.execute(stmt.order_by(PMTaskLink.created_at)).scalars().all()
            for r in rows:
                session.expunge(r)
            return list(rows)

    # ------------------------------------------------------------------ #
    # internals                                                          #
    # ------------------------------------------------------------------ #

    def _load(
        self, session: Session, workspace_id: uuid.UUID, connection_id: uuid.UUID
    ) -> PMConnection:
        conn = session.get(PMConnection, connection_id)
        if conn is None or conn.workspace_id != workspace_id:
            raise PMConnectionNotFound(str(connection_id))
        return conn

    def _build_adapter(self, connection: PMConnection) -> PMAdapter:
        ctx = AdapterContext(
            connection_id=connection.id,
            workspace_id=connection.workspace_id,
            provider=ContractsProvider(connection.provider.value),
            external_project_key=connection.external_project_key,
            external_project_id=connection.external_project_id,
            external_base_url=connection.external_base_url,
            status_map=connection.status_map or {},
            priority_map=connection.priority_map or {},
            field_map=connection.field_map or {},
            config={
                "granted_scopes": connection.granted_scopes or [],
                **(connection.config or {}),
            },
        )
        transport = self._transport_factory(connection)
        return build_adapter(
            connection.provider.value,
            transport,
            ctx,
            auth_header=self._auth_header(connection),
        )

    def _auth_header(self, connection: PMConnection) -> str | None:
        if not connection.credential_ref:
            return None
        try:
            secret = self._vault.get_secret(
                connection.workspace_id, uuid.UUID(connection.credential_ref)
            )
        except (SecretNotFoundError, ValueError):
            return None
        if connection.provider == PMProvider.JIRA:
            if connection.auth_type == PMAuthType.API_TOKEN:
                email = (connection.config or {}).get("api_token_email") or ""
                return jira_auth.basic_auth_header(email, secret)
            return jira_auth.bearer_header(secret)
        if connection.auth_type == PMAuthType.API_TOKEN:
            return linear_auth.api_key_header(secret)
        return linear_auth.bearer_header(secret)

    def _webhook_secret(self, connection: PMConnection) -> str | None:
        if not connection.webhook_secret_ref:
            return None
        try:
            return self._vault.get_secret(
                connection.workspace_id, uuid.UUID(connection.webhook_secret_ref)
            )
        except (SecretNotFoundError, ValueError):
            return None

    @staticmethod
    def _default_status_map(provider: PMProvider) -> dict:
        table = (
            jira_mapping.STATUS_OUT if provider == PMProvider.JIRA else linear_mapping.STATUS_OUT
        )
        return dict(table)

    @staticmethod
    def _default_priority_map(provider: PMProvider) -> dict:
        table = (
            jira_mapping.PRIORITY_OUT
            if provider == PMProvider.JIRA
            else linear_mapping.PRIORITY_OUT
        )
        return dict(table)


__all__ = [
    "PMConflictExists",
    "PMConnectionNotFound",
    "PMConnectionService",
    "PMError",
]
