"""Alerts every signed-in member of a Project sees (product owner, 2026-10-31).

    GET /p2/v1/tenants/{tenant_id}/alerts?since=<ISO time>

"This alert should be seen by all team on their respective screens, if they
are active": the web app polls this while a screen is open and shows what is
new. Any member of the Project may read it (the journey-read permission every
operating role holds -- PC, TL, PM, Reviewer, CRM, Executive); nothing here is
scoped to a dealership, since the same file may be uploaded at another one.

Only PUBLISHED alerts that have not expired (24 hours) are returned, newest
first; ``since`` narrows to those published after it. The first kind is
SAME_FILE_UPLOADED (uc03_p2_same_file).

DUPLICATE_ACROSS_OUTLETS: from 2026-11-04 (another dealership) for every
screen of the Project. DUPLICATE_SAME_DEALERSHIP (2026-11-04) is "to be seen
by the PC of both the affected outlets, Reviewer, TL", and stays up while both
bookings are open. Its payload names the outlets
(``audienceOutletIds``) and roles (``audienceRoles``); it is returned to a
person who holds one of those roles at one of those outlets -- a dealer-wide
or Project-wide assignment covers the outlet (uc03_p2_gate_pass.scope_outlets).
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import Connection, text

from audit_core.db import set_tenant_context
from audit_core.dependencies import get_connection, get_human_principal
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.uc03_p2_access import check_p2_permission

router = APIRouter(prefix="/p2/v1/tenants/{tenant_id}", tags=["uc03-phase2"])

_READ_PERMISSION = "audit.journey.read"
_LIMIT = 50


class TenantAlert(BaseModel):
    alertId: str
    kind: str
    outcome: str | None
    title: str
    body: str
    journeyId: str | None
    journeyReference: str | None
    payload: dict[str, Any]
    publishedAtUtc: datetime
    expiresAtUtc: datetime


class TenantAlertsResponse(BaseModel):
    items: list[TenantAlert]
    serverTimeUtc: datetime


def _for_audience(payload: dict[str, Any], held: dict[str, set[str]] | None) -> bool:
    """An alert with an audience is for those who hold one of its roles at one of its outlets."""
    outlets = payload.get("audienceOutletIds")
    if not outlets:
        return True
    if held is None:
        return False
    roles = {str(r).upper() for r in payload.get("audienceRoles") or []}
    return any(roles & held.get(str(o), set()) for o in outlets)


def roles_by_outlet(connection: Connection, *, tenant_id: str, actor_id: str) -> dict[str, set[str]]:
    from audit_core.uc03_p2_gate_pass import scope_outlets

    return {str(o["outlet_id"]): {str(r).upper() for r in (o.get("roles") or [])}
            for o in scope_outlets(connection, tenant_id, actor_id)}


def active_alerts(connection: Connection, *, tenant_id: str, since: datetime | None = None,
                  held: dict[str, set[str]] | None = None) -> list[TenantAlert]:
    from audit_core.uc03_p2_same_file import _CLOSED_SQL

    rows = connection.execute(
        text(
            """
            SELECT alert_id, alert_kind, outcome, title, body, subject_journey_id, subject_journey_reference,
                   payload, published_at_utc, expires_at_utc
            FROM auditcore.p2_tenant_alerts
            WHERE tenant_id=:t AND alert_status='PUBLISHED'
              AND (expires_at_utc > now()
                   -- a duplicate booking at two outlets of one dealership stays up while both
                   -- bookings are open: it is what pauses the PCs' uploads (2026-11-04)
                   OR (alert_kind = 'DUPLICATE_SAME_DEALERSHIP'
                       AND (SELECT count(*) FROM auditcore.journeys j
                            WHERE j.tenant_id = p2_tenant_alerts.tenant_id
                              AND j.journey_id IN (subject_journey_id, related_journey_id)
                              AND NOT """ + _CLOSED_SQL + """) = 2))
              AND (CAST(:since AS timestamptz) IS NULL OR published_at_utc > CAST(:since AS timestamptz))
            ORDER BY published_at_utc DESC, alert_id
            LIMIT :limit
            """
        ),
        {"t": tenant_id, "since": since, "limit": _LIMIT},
    ).mappings().all()
    return [
        TenantAlert(
            alertId=str(row["alert_id"]),
            kind=row["alert_kind"],
            outcome=row["outcome"],
            title=row["title"],
            body=row["body"],
            journeyId=str(row["subject_journey_id"]) if row["subject_journey_id"] else None,
            journeyReference=row["subject_journey_reference"],
            payload=dict(row["payload"] or {}),
            publishedAtUtc=row["published_at_utc"],
            expiresAtUtc=row["expires_at_utc"],
        )
        for row in rows
        if _for_audience(dict(row["payload"] or {}), held)
    ]


@router.get("/alerts", response_model=TenantAlertsResponse)
def list_tenant_alerts(
    tenant_id: str,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
    since: datetime | None = None,
) -> TenantAlertsResponse:
    check_p2_permission(
        tenant_id=tenant_id, human_principal=human_principal,
        authorization_client=authorization_client, permission_key=_READ_PERMISSION,
    )
    set_tenant_context(connection, tenant_id)
    now = connection.execute(text("SELECT now()")).scalar_one()
    held = roles_by_outlet(connection, tenant_id=tenant_id, actor_id=human_principal.subject)
    return TenantAlertsResponse(items=active_alerts(connection, tenant_id=tenant_id, since=since, held=held),
                                serverTimeUtc=now)
