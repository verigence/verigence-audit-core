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


def active_alerts(connection: Connection, *, tenant_id: str, since: datetime | None = None) -> list[TenantAlert]:
    rows = connection.execute(
        text(
            """
            SELECT alert_id, alert_kind, outcome, title, body, subject_journey_id, subject_journey_reference,
                   payload, published_at_utc, expires_at_utc
            FROM auditcore.p2_tenant_alerts
            WHERE tenant_id=:t AND alert_status='PUBLISHED' AND expires_at_utc > now()
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
    return TenantAlertsResponse(items=active_alerts(connection, tenant_id=tenant_id, since=since), serverTimeUtc=now)
