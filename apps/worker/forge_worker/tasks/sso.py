"""F33 SSO worker tasks (queue ``auth``): metadata refresh, replay cleanup,
deprovision fan-out.

Deterministic cores (no Celery, injectable session factory + httpx transport)
are unit-tested directly; the ``@celery_app.task`` wrappers are thin seams,
mirroring the F32 marketplace task pattern. No live IdP is ever contacted in
tests — the metadata fetch goes through an injected ``httpx.MockTransport``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import httpx
from sqlalchemy import select

from forge_api.db import get_session_factory
from forge_api.sso.config_service import fetch_idp_metadata
from forge_api.sso.provisioning import emit_sso_audit
from forge_api.sso.replay import DbReplayGuard
from forge_api.sso.saml_metadata import parse_idp_metadata
from forge_db.models import SsoConfiguration
from forge_worker.celery_app import celery_app


def refresh_saml_metadata_core(
    session_factory,
    sso_configuration_id: uuid.UUID,
    *,
    transport: httpx.BaseTransport | None = None,
) -> dict:
    """Refresh one config from its ``metadata_url`` (idempotent).

    Newly-rotated IdP certs are **appended** (old certs kept) so responses
    signed by either cert validate during the rollover overlap window (AC20).
    """
    with session_factory() as session:
        config = session.get(SsoConfiguration, sso_configuration_id)
        if config is None:
            return {"status": "missing", "id": str(sso_configuration_id)}
        if not config.metadata_url:
            return {"status": "no_metadata_url", "id": str(sso_configuration_id)}
        xml = fetch_idp_metadata(config.metadata_url, transport=transport)
        idp = parse_idp_metadata(xml)
        merged_certs = list(config.idp_x509_certs)
        added = 0
        for cert in idp.x509_certs:
            if cert not in merged_certs:
                merged_certs.append(cert)
                added += 1
        config.idp_entity_id = idp.entity_id
        config.idp_sso_url = idp.sso_url
        config.idp_slo_url = idp.slo_url
        config.idp_x509_certs = merged_certs
        config.last_metadata_refresh_at = datetime.now(UTC)
        session.commit()
        return {
            "status": "refreshed",
            "id": str(sso_configuration_id),
            "certs_added": added,
            "certs_total": len(merged_certs),
        }


def refresh_all_saml_metadata_core(
    session_factory, *, transport: httpx.BaseTransport | None = None
) -> int:
    """Refresh every config that has a ``metadata_url``; returns the count."""
    with session_factory() as session:
        ids = [
            config_id
            for (config_id,) in session.execute(
                select(SsoConfiguration.id).where(
                    SsoConfiguration.metadata_url.is_not(None)
                )
            ).all()
        ]
    for config_id in ids:
        refresh_saml_metadata_core(session_factory, config_id, transport=transport)
    return len(ids)


def cleanup_saml_replay_core(session_factory) -> int:
    """Evict expired ``saml_replay`` rows (Postgres-fallback deployments)."""
    with session_factory() as session:
        removed = DbReplayGuard(session).cleanup_expired()
        session.commit()
        return removed


def propagate_deprovision_core(
    session_factory,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    revoke_sessions=None,
) -> dict:
    """Best-effort fan-out after the synchronous SCIM deprovision.

    Re-revokes credentials (idempotent belt-and-braces) and records the
    ``sso.deprovision_propagated`` audit event. PARKED: cancelling the user's
    in-flight ``agent_run`` rows — the foundation ``agent_run`` schema carries
    no per-user ownership column to cancel by; wired when F07's cancel
    transition grows an owner filter.
    """
    revoked = 0
    if revoke_sessions is not None:
        revoked = revoke_sessions(workspace_id, user_id)
    with session_factory() as session:
        emit_sso_audit(
            session,
            workspace_id=workspace_id,
            action="sso.deprovision_propagated",
            actor_type="system",
            target_type="user",
            target_id=user_id,
            details={"revoked_credentials": revoked},
        )
        session.commit()
    return {"status": "propagated", "revoked": revoked}


@celery_app.task(name="sso.refresh_saml_metadata", queue="auth")
def refresh_saml_metadata(sso_configuration_id: str) -> dict:
    return refresh_saml_metadata_core(
        get_session_factory(), uuid.UUID(sso_configuration_id)
    )


@celery_app.task(name="sso.refresh_all_saml_metadata", queue="auth")
def refresh_all_saml_metadata() -> int:
    return refresh_all_saml_metadata_core(get_session_factory())


@celery_app.task(name="sso.cleanup_saml_replay", queue="auth")
def cleanup_saml_replay() -> int:
    return cleanup_saml_replay_core(get_session_factory())


@celery_app.task(name="sso.propagate_deprovision", queue="auth")
def propagate_deprovision(user_id: str, workspace_id: str) -> dict:
    return propagate_deprovision_core(
        get_session_factory(), uuid.UUID(workspace_id), uuid.UUID(user_id)
    )


__all__ = [
    "cleanup_saml_replay",
    "cleanup_saml_replay_core",
    "propagate_deprovision",
    "propagate_deprovision_core",
    "refresh_all_saml_metadata",
    "refresh_all_saml_metadata_core",
    "refresh_saml_metadata",
    "refresh_saml_metadata_core",
]
