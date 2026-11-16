"""The notification bell: messages and the status of one's own documents (product owner 2026-11-16).

    "can we introduce a notification icon and they should click on it know the status or any
    notification messages"

    GET  /p2/v1/tenants/{tenant_id}/notifications        what the bell shows
    POST /p2/v1/tenants/{tenant_id}/notifications:seen   the person opened it

Messages: the Project alerts addressed to the person (the same audience rules as the banner,
uc03_p2_tenant_alerts._for_audience) published in the last 7 days -- still listed after the
banner closed them or they expired, so nothing is lost by dismissing it. A message is unread
when it was published after the person last opened the bell (p2_notification_seen; kept per
person and Project, so the web and the app agree).

Status: the documents the person uploaded that are not settled, as they stand now, with no
time window -- the gaps of the MY_DOCUMENTS_ATTENTION alert (16 Nov 2026: a file never split
into pages has no pages and was never in it; a failed page dropped out after 24 hours):

    beingRead       pages still being read (stuck: more than 6 hours since they last moved)
    failedPages     pages that failed or were refused and are not yet replaced or swept
    filesNotRead    files that were never split into pages and were not uploaded or sent again
    uploadTasks     open "upload again" tasks (category DOCUMENT_REUPLOAD) on bookings the person
                    uploaded to -- what is left to do after the day-end sweep took the pages off

Bookings that are deleted, cancelled, a duplicate, closed without delivery or delivered and
complete are left out: nothing is asked of them. The bell's count is the unread messages
plus the documents that need the person (failed pages, files not read, upload tasks).
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
from audit_core.uc03_p2_journey_views import _IN_FLIGHT
from audit_core.uc03_p2_task_producer import _OPEN
from audit_core.uc03_p2_tenant_alerts import _for_audience, roles_by_outlet

router = APIRouter(prefix="/p2/v1/tenants/{tenant_id}", tags=["uc03-phase2"])

_READ_PERMISSION = "audit.journey.read"
HISTORY_DAYS = 7
STUCK_HOURS = 6
_MESSAGE_LIMIT = 50
_JOURNEY_LIMIT = 30

# A booking nothing more is asked of (uc03_p2_same_file._CLOSED_SQL, with the journey as ``j``).
_SETTLED_SQL = """
    EXISTS (
      SELECT 1 FROM auditcore.journey_stage_states st
      WHERE st.tenant_id=j.tenant_id AND st.journey_id=j.journey_id
        AND ((st.stage_code='DELIVERY' AND st.business_completed_at_utc IS NOT NULL)
             OR (st.stage_code='BOOKING'
                 AND (st.business_status IN ('BOOKING_CANCELLED','DUPLICATE_BOOKING')
                      OR st.closure_disposition='NO_DELIVERY')))
    )
    OR EXISTS (SELECT 1 FROM auditcore.p2_journey_deletions d
               WHERE d.tenant_id=j.tenant_id AND d.journey_id=j.journey_id)
"""

_STATUS_SQL = f"""
    WITH mine AS (
        SELECT DISTINCT b.journey_id FROM auditcore.p2_upload_batches b
        WHERE b.tenant_id=:t AND b.uploaded_by_actor_id=:a
    ),
    open_journeys AS (
        SELECT j.journey_id, j.journey_reference FROM auditcore.journeys j
        JOIN mine m ON m.journey_id=j.journey_id
        WHERE j.tenant_id=:t AND NOT ({_SETTLED_SQL})
    ),
    pages AS (
        SELECT q.journey_id,
               count(*) FILTER (WHERE q.queue_status = ANY(CAST(:in_flight AS text[]))) AS being_read,
               count(*) FILTER (WHERE q.queue_status = ANY(CAST(:in_flight AS text[]))
                                  AND q.updated_at_utc < now() - make_interval(hours => :stuck)) AS stuck,
               count(*) FILTER (WHERE q.queue_status IN ('FAILED','DEAD_LETTER')) AS failed
        FROM auditcore.p2_document_queue q
        JOIN auditcore.p2_upload_batches b ON b.tenant_id=q.tenant_id AND b.batch_id=q.batch_id
        WHERE q.tenant_id=:t AND b.uploaded_by_actor_id=:a
          AND q.retired_at_utc IS NULL AND q.merged_into_queue_id IS NULL
        GROUP BY q.journey_id
    ),
    files AS (
        -- A file never split into pages that nothing replaced: not uploaded again (read or being
        -- read) and not sent again by Verigence (that closes it as CANCELLED).
        SELECT b.journey_id, count(*) AS not_read, array_agg(b.original_filename ORDER BY b.created_at_utc) AS names
        FROM auditcore.p2_upload_batches b
        WHERE b.tenant_id=:t AND b.uploaded_by_actor_id=:a
          AND b.batch_status='FAILED' AND COALESCE(b.page_count, 0)=0
          AND NOT EXISTS (
            SELECT 1 FROM auditcore.p2_upload_batches o
            WHERE o.tenant_id=b.tenant_id AND o.journey_id=b.journey_id AND o.created_at_utc > b.created_at_utc
              AND o.batch_status NOT IN ('FAILED','CANCELLED')
              AND (o.original_filename = b.original_filename OR o.size_bytes = b.size_bytes))
        GROUP BY b.journey_id
    ),
    tasks AS (
        SELECT t.journey_id, count(*) AS upload_tasks
        FROM auditcore.p2_tasks t
        WHERE t.tenant_id=:t AND t.category='DOCUMENT_REUPLOAD' AND t.task_status = ANY(CAST(:open AS text[]))
        GROUP BY t.journey_id
    )
    SELECT o.journey_id, o.journey_reference AS reference,
           COALESCE(p.being_read, 0) AS being_read, COALESCE(p.stuck, 0) AS stuck,
           COALESCE(p.failed, 0) AS failed, COALESCE(f.not_read, 0) AS files_not_read,
           COALESCE(f.names, ARRAY[]::text[]) AS files, COALESCE(k.upload_tasks, 0) AS upload_tasks
    FROM open_journeys o
    LEFT JOIN pages p ON p.journey_id=o.journey_id
    LEFT JOIN files f ON f.journey_id=o.journey_id
    LEFT JOIN tasks k ON k.journey_id=o.journey_id
    WHERE COALESCE(p.being_read, 0) + COALESCE(p.failed, 0) + COALESCE(f.not_read, 0)
          + COALESCE(k.upload_tasks, 0) > 0
    ORDER BY COALESCE(f.not_read, 0) + COALESCE(p.failed, 0) + COALESCE(k.upload_tasks, 0) DESC,
             o.journey_reference
"""


class NotificationMessage(BaseModel):
    alertId: str
    kind: str
    title: str
    body: str
    journeyId: str | None
    journeyReference: str | None
    payload: dict[str, Any]
    publishedAtUtc: datetime
    expiresAtUtc: datetime
    unread: bool


class DocumentStatusJourney(BaseModel):
    journeyId: str
    reference: str | None
    beingRead: int
    stuck: int
    failedPages: int
    filesNotRead: int
    files: list[str]
    uploadTasks: int


class DocumentStatus(BaseModel):
    beingRead: int
    stuck: int
    failedPages: int
    filesNotRead: int
    uploadTasks: int
    needsAction: int
    journeys: list[DocumentStatusJourney]
    moreJourneys: int


class NotificationsResponse(BaseModel):
    messages: list[NotificationMessage]
    unreadCount: int
    status: DocumentStatus
    badgeCount: int
    seenAtUtc: datetime | None
    serverTimeUtc: datetime


def seen_at(connection: Connection, *, tenant_id: str, actor_id: str) -> datetime | None:
    return connection.execute(
        text("SELECT seen_at_utc FROM auditcore.p2_notification_seen WHERE tenant_id=:t AND actor_id=:a"),
        {"t": tenant_id, "a": actor_id},
    ).scalar_one_or_none()


def messages_for(connection: Connection, *, tenant_id: str, actor_id: str,
                 held: dict[str, set[str]] | None, seen: datetime | None) -> list[NotificationMessage]:
    rows = connection.execute(
        text(
            """
            SELECT alert_id, alert_kind, title, body, subject_journey_id, subject_journey_reference,
                   payload, published_at_utc, expires_at_utc
            FROM auditcore.p2_tenant_alerts
            WHERE tenant_id=:t AND alert_status='PUBLISHED'
              AND published_at_utc > now() - make_interval(days => :days)
            ORDER BY published_at_utc DESC, alert_id
            LIMIT :limit
            """
        ),
        {"t": tenant_id, "days": HISTORY_DAYS, "limit": _MESSAGE_LIMIT * 4},
    ).mappings().all()
    out: list[NotificationMessage] = []
    for row in rows:
        payload = dict(row["payload"] or {})
        if not _for_audience(payload, held, actor_id):
            continue
        out.append(NotificationMessage(
            alertId=str(row["alert_id"]), kind=row["alert_kind"], title=row["title"], body=row["body"],
            journeyId=str(row["subject_journey_id"]) if row["subject_journey_id"] else None,
            journeyReference=row["subject_journey_reference"], payload=payload,
            publishedAtUtc=row["published_at_utc"], expiresAtUtc=row["expires_at_utc"],
            unread=seen is None or row["published_at_utc"] > seen,
        ))
        if len(out) >= _MESSAGE_LIMIT:
            break
    return out


def document_status(connection: Connection, *, tenant_id: str, actor_id: str) -> DocumentStatus:
    rows = [dict(r) for r in connection.execute(
        text(_STATUS_SQL),
        {"t": tenant_id, "a": actor_id, "in_flight": list(_IN_FLIGHT), "stuck": STUCK_HOURS, "open": list(_OPEN)},
    ).mappings().all()]
    total = {k: sum(int(r[k]) for r in rows) for k in ("being_read", "stuck", "failed", "files_not_read",
                                                       "upload_tasks")}
    return DocumentStatus(
        beingRead=total["being_read"], stuck=total["stuck"], failedPages=total["failed"],
        filesNotRead=total["files_not_read"], uploadTasks=total["upload_tasks"],
        needsAction=total["failed"] + total["files_not_read"] + total["upload_tasks"],
        journeys=[DocumentStatusJourney(
            journeyId=str(r["journey_id"]), reference=r["reference"], beingRead=int(r["being_read"]),
            stuck=int(r["stuck"]), failedPages=int(r["failed"]), filesNotRead=int(r["files_not_read"]),
            files=[str(f) for f in (r["files"] or [])], uploadTasks=int(r["upload_tasks"]),
        ) for r in rows[:_JOURNEY_LIMIT]],
        moreJourneys=max(0, len(rows) - _JOURNEY_LIMIT),
    )


def _authorize(connection: Connection, tenant_id: str, principal: HumanPrincipal,
               client: SecurityAuthorizationClient) -> None:
    check_p2_permission(tenant_id=tenant_id, human_principal=principal, authorization_client=client,
                        permission_key=_READ_PERMISSION)
    set_tenant_context(connection, tenant_id)


@router.get("/notifications", response_model=NotificationsResponse)
def get_notifications(
    tenant_id: str,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> NotificationsResponse:
    _authorize(connection, tenant_id, human_principal, authorization_client)
    actor = human_principal.subject
    seen = seen_at(connection, tenant_id=tenant_id, actor_id=actor)
    held = roles_by_outlet(connection, tenant_id=tenant_id, actor_id=actor)
    messages = messages_for(connection, tenant_id=tenant_id, actor_id=actor, held=held, seen=seen)
    status = document_status(connection, tenant_id=tenant_id, actor_id=actor)
    unread = sum(1 for m in messages if m.unread)
    return NotificationsResponse(
        messages=messages, unreadCount=unread, status=status, badgeCount=unread + status.needsAction,
        seenAtUtc=seen, serverTimeUtc=connection.execute(text("SELECT now()")).scalar_one(),
    )


@router.post("/notifications:seen", status_code=204)
def mark_notifications_seen(
    tenant_id: str,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> None:
    _authorize(connection, tenant_id, human_principal, authorization_client)
    connection.execute(
        text("INSERT INTO auditcore.p2_notification_seen (tenant_id, actor_id, seen_at_utc) VALUES (:t, :a, now()) "
             "ON CONFLICT (tenant_id, actor_id) DO UPDATE SET seen_at_utc = now()"),
        {"t": tenant_id, "a": human_principal.subject},
    )
