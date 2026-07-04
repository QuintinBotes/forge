"""Alert ingest router (F17).

``POST /integrations/alerts/{provider}/webhook`` is **signature-verified, not
authenticated** (untrusted intake): the provider signs the exact raw bytes, so
the handler reads the raw body, verifies the per-provider secret, and fails
closed (501) when no secret is configured and (401) on a bad/missing signature
with no state change. Duplicate deliveries are skipped idempotently.

``POST /integrations/alerts/manual`` is the authenticated, member+ manual-declare
path (an alert without an external provider).

Workspace routing for the unauthenticated webhook is via explicit
``workspace_id`` + ``project_id`` query params (the in-memory registry has no
provider-connection table yet — deviation noted), so the dedup/idempotency/
signature behavior is fully exercised.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status

from forge_api.auth.rbac import Permission
from forge_api.deps import Principal, SettingsDep
from forge_api.routers._rbac import require_permission
from forge_api.routers.incidents import (
    IncidentServiceRegistry,
    _view,
    get_incident_registry,
)
from forge_api.schemas.incidents import (
    AlertAccepted,
    IncidentView,
    ManualAlertRequest,
)
from forge_board.incidents.alert import derive_dedup_key
from forge_contracts.incident import AlertProvider, IncidentAlert
from forge_integrations.alerts import get_alert_adapter

router = APIRouter(prefix="/integrations/alerts", tags=["alerts"])

WriterDep = Annotated[Principal, Depends(require_permission(Permission.WRITE))]
RegistryDep = Annotated[IncidentServiceRegistry, Depends(get_incident_registry)]

_SECRET_ATTR = {
    AlertProvider.PAGERDUTY: "pagerduty_webhook_secret",
    AlertProvider.DATADOG: "datadog_webhook_secret",
    AlertProvider.SENTRY: "sentry_webhook_secret",
    AlertProvider.GRAFANA: "grafana_webhook_secret",
}


@router.post("/manual", response_model=IncidentView, status_code=status.HTTP_201_CREATED)
def ingest_manual_alert(
    principal: WriterDep,
    registry: RegistryDep,
    body: ManualAlertRequest,
) -> IncidentView:
    """Declare an incident from a manual alert (authenticated, member+)."""
    alert = IncidentAlert(
        provider=AlertProvider.MANUAL,
        dedup_key=body.dedup_key or "",
        title=body.title,
        severity=body.severity,
        service=body.service,
        description=body.description,
    )
    alert = alert.model_copy(update={"dedup_key": derive_dedup_key(alert)})
    service = registry.for_workspace(principal.workspace_id)
    record, _status = service.ingest_alert(
        alert=alert, project_id=body.project_id, actor=f"user:{principal.user_id}"
    )
    return _view(service, record)


@router.post("/{provider}/webhook", status_code=status.HTTP_202_ACCEPTED)
async def ingest_alert(
    provider: str,
    request: Request,
    response: Response,
    settings: SettingsDep,
    registry: RegistryDep,
    workspace_id: Annotated[uuid.UUID, Query()],
    project_id: Annotated[uuid.UUID, Query()],
) -> AlertAccepted:
    """Ingest a provider alert webhook (signature-verified, fail-closed)."""
    try:
        alert_provider = AlertProvider(provider)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"unknown provider {provider!r}"
        ) from exc
    if alert_provider not in _SECRET_ATTR:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"provider {provider!r} has no webhook adapter",
        )

    secret = getattr(settings, _SECRET_ATTR[alert_provider], None)
    if not secret:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=f"{provider} webhook secret not configured",
        )

    body = await request.body()
    headers = dict(request.headers)
    adapter = get_alert_adapter(alert_provider)
    if not adapter.verify(secret=secret, body=body, headers=headers):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid alert signature"
        )

    alert = adapter.normalize(body=body, headers=headers)
    service = registry.for_workspace(workspace_id)

    # Idempotency: a duplicate delivery is skipped without re-processing.
    if alert.delivery_id and not service.register_delivery(provider, alert.delivery_id):
        return AlertAccepted(status="skipped")

    # The raw payload is not persisted beyond a redacted hash.
    _payload_hash = hashlib.sha256(body).hexdigest()
    record, ingest_status = service.ingest_alert(
        alert=alert, project_id=project_id, actor="system"
    )
    response.status_code = status.HTTP_202_ACCEPTED
    return AlertAccepted(
        status=ingest_status, incident_id=record.id, incident_key=record.key
    )


__all__ = ["router"]
