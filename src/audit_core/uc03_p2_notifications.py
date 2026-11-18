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

Status (product owner 2026-11-16: "Notifications is only for the files or journeys which doesnt get
uploaded.. no record get created.. for all other tasks are already getting generated.. if any bulk
upload or drive upload fails, that has to be notified"): the person's uploads of the last 14 days
that left nothing behind -- no pages, no booking -- and so no task either:

    UPLOAD   a file uploaded to a booking that was never split into pages (the upload was never
             confirmed, or it was not split in time) and was not uploaded or sent again since
    BULK     a file of a bulk upload that was not uploaded before the bulk was submitted, or that
             failed; a bulk upload left open (never submitted) for more than 2 hours
    DRIVE    a Drive file set aside (not taken: its reason is given); a Drive run that failed

Failed pages, stuck pages and documents to upload again are not here: each raises its task. The
bell's count is the unread messages plus these.

Product owner 2026-11-18: "notification tab is quite vague... it doesnt tell which customer/journey
is the reported issue.. can you provide it clickable so that it can directly open up that task or
journey 360". The message about a person's own stuck or failed pages (MY_DOCUMENTS_ATTENTION, one
alert per person over all their bookings) is therefore shown as one message per booking, named by
its customer and carrying the booking, so each opens that booking. And it is listed only while it
stands: withdrawn (the pages were sent again or removed) it says something no longer true -- on
18 Nov a PC's bell held three such messages of which two had been settled within a minute.
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
from audit_core.uc03_p2_document_alerts import ALERT_KIND as DOCUMENTS_KIND
from audit_core.uc03_p2_document_alerts import booking_messages
from audit_core.uc03_p2_tenant_alerts import (
    _for_audience,
    is_silent_merge,
    roles_by_outlet,
)

router = APIRouter(prefix="/p2/v1/tenants/{tenant_id}", tags=["uc03-phase2"])

_READ_PERMISSION = "audit.journey.read"
HISTORY_DAYS = 7
_MESSAGE_LIMIT = 50
_ITEM_LIMIT = 30

STATUS_DAYS = 14
OPEN_BULK_HOURS = 2

_NOT_DONE_SQL = """
    -- UPLOAD: a file never split into pages, not uploaded again (read or being read) nor sent
    -- again by Verigence (that closes it as CANCELLED).
    SELECT 'UPLOAD' AS source, b.batch_id::text AS item_id, b.original_filename AS name,
           COALESCE(b.failure_reason, 'The file was not read.') AS reason,
           j.journey_id, j.journey_reference AS reference, b.created_at_utc AS at_utc
    FROM auditcore.p2_upload_batches b
    JOIN auditcore.journeys j ON j.tenant_id=b.tenant_id AND j.journey_id=b.journey_id
    WHERE b.tenant_id=:t AND b.uploaded_by_actor_id=:a AND b.batch_status='FAILED'
      AND COALESCE(b.page_count, 0)=0 AND b.created_at_utc > now() - make_interval(days => :days)
      AND NOT EXISTS (SELECT 1 FROM auditcore.p2_journey_deletions d
                      WHERE d.tenant_id=b.tenant_id AND d.journey_id=b.journey_id)
      AND NOT EXISTS (
        SELECT 1 FROM auditcore.p2_upload_batches o
        WHERE o.tenant_id=b.tenant_id AND o.journey_id=b.journey_id AND o.created_at_utc > b.created_at_utc
          AND o.batch_status NOT IN ('FAILED','CANCELLED')
          AND (o.original_filename = b.original_filename OR o.size_bytes = b.size_bytes))
    UNION ALL
    -- BULK: a file that was not uploaded before submit, or failed, and was not uploaded since.
    SELECT CASE WHEN u.source = 'DRIVE' THEN 'DRIVE' ELSE 'BULK' END, f.file_id::text, f.original_filename,
           COALESCE(NULLIF(f.detail, ''), 'The file was not uploaded.'),
           COALESCE(f.placed_journey_id, f.journey_id), f.placed_reference, f.created_at_utc
    FROM auditcore.p2_bulk_files f
    JOIN auditcore.p2_bulk_uploads u ON u.tenant_id=f.tenant_id AND u.bulk_id=f.bulk_id
    WHERE f.tenant_id=:t AND u.uploaded_by_actor_id=:a AND f.outcome='FAILED'
      AND f.created_at_utc > now() - make_interval(days => :days)
      AND NOT EXISTS (
        SELECT 1 FROM auditcore.p2_bulk_files g
        WHERE g.tenant_id=f.tenant_id AND g.created_at_utc > f.created_at_utc
          AND g.upload_status='UPLOADED' AND g.outcome <> 'FAILED'
          AND (g.original_filename = f.original_filename OR (f.sha256 IS NOT NULL AND g.sha256 = f.sha256)))
    UNION ALL
    -- BULK: an upload left open, never submitted, with files still waiting.
    SELECT 'BULK', u.bulk_id::text, COALESCE(NULLIF(u.folder, ''), 'Bulk upload'),
           count(f.file_id)::text || ' file(s) waiting: the bulk upload was never submitted.',
           NULL::uuid, NULL, u.created_at_utc
    FROM auditcore.p2_bulk_uploads u
    JOIN auditcore.p2_bulk_files f ON f.tenant_id=u.tenant_id AND f.bulk_id=u.bulk_id AND f.outcome='PENDING'
    WHERE u.tenant_id=:t AND u.uploaded_by_actor_id=:a AND u.status='OPEN'
      AND u.updated_at_utc < now() - make_interval(hours => :open_hours)
      AND u.created_at_utc > now() - make_interval(days => :days)
    GROUP BY u.bulk_id, u.folder, u.created_at_utc
    UNION ALL
    -- DRIVE: a file met in Drive and set aside (not taken).
    SELECT 'DRIVE', f.file_row_id::text, f.file_name, COALESCE(NULLIF(f.detail, ''), 'Set aside: not taken.'),
           f.journey_id, NULL, COALESCE(f.updated_at_utc, f.seen_at_utc)
    FROM auditcore.p2_drive_files f
    WHERE f.tenant_id=:t AND f.pc_actor_id=:a AND f.file_status='SET_ASIDE'
      AND COALESCE(f.updated_at_utc, f.seen_at_utc) > now() - make_interval(days => :days)
    UNION ALL
    -- DRIVE: a run that failed.
    SELECT 'DRIVE', r.run_id::text, 'Google Drive run', COALESCE(NULLIF(r.detail::text, ''), 'The Drive run failed.'),
           NULL::uuid, NULL, r.requested_at_utc
    FROM auditcore.p2_drive_runs r
    WHERE r.tenant_id=:t AND r.requested_by_actor_id=:a AND r.run_status='FAILED'
      AND r.requested_at_utc > now() - make_interval(days => :days)
    ORDER BY at_utc DESC
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


class NotUploaded(BaseModel):
    source: str
    itemId: str
    name: str
    reason: str
    journeyId: str | None
    reference: str | None
    atUtc: datetime


class UploadStatus(BaseModel):
    upload: int
    bulk: int
    drive: int
    total: int
    items: list[NotUploaded]
    more: int


class NotificationsResponse(BaseModel):
    messages: list[NotificationMessage]
    unreadCount: int
    status: UploadStatus
    badgeCount: int
    seenAtUtc: datetime | None
    serverTimeUtc: datetime


def seen_at(connection: Connection, *, tenant_id: str, actor_id: str) -> datetime | None:
    return connection.execute(
        text("SELECT seen_at_utc FROM auditcore.p2_notification_seen WHERE tenant_id=:t AND actor_id=:a"),
        {"t": tenant_id, "a": actor_id},
    ).scalar_one_or_none()


def _at(value: Any, fallback: datetime) -> datetime:
    try:
        at = datetime.fromisoformat(str(value)) if value else fallback
    except ValueError:
        return fallback
    return at if at.tzinfo else fallback


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
              -- one's own stuck or failed pages: only while it stands (2026-11-18)
              AND (alert_kind <> :documents OR expires_at_utc > now())
            ORDER BY published_at_utc DESC, alert_id
            LIMIT :limit
            """
        ),
        {"t": tenant_id, "days": HISTORY_DAYS, "limit": _MESSAGE_LIMIT * 4, "documents": DOCUMENTS_KIND},
    ).mappings().all()
    out: list[NotificationMessage] = []
    for row in rows:
        payload = dict(row["payload"] or {})
        if not _for_audience(payload, held, actor_id) or is_silent_merge(row["alert_kind"], payload):
            continue
        bookings = booking_messages(payload) if row["alert_kind"] == DOCUMENTS_KIND else []
        for booking in bookings:
            # One message per booking, named by its customer; new since the booking came up.
            since = _at(booking["sinceUtc"], row["published_at_utc"])
            out.append(NotificationMessage(
                alertId=f"{row['alert_id']}:{booking['journeyId']}", kind=row["alert_kind"],
                title=booking["title"], body=booking["body"], journeyId=booking["journeyId"],
                journeyReference=booking["reference"],
                payload={k: booking[k] for k in ("customerName", "stuck", "failed")},
                publishedAtUtc=since, expiresAtUtc=row["expires_at_utc"],
                unread=seen is None or since > seen,
            ))
        if bookings:
            continue
        out.append(NotificationMessage(
            alertId=str(row["alert_id"]), kind=row["alert_kind"], title=row["title"], body=row["body"],
            journeyId=str(row["subject_journey_id"]) if row["subject_journey_id"] else None,
            journeyReference=row["subject_journey_reference"], payload=payload,
            publishedAtUtc=row["published_at_utc"], expiresAtUtc=row["expires_at_utc"],
            unread=seen is None or row["published_at_utc"] > seen,
        ))
    out.sort(key=lambda m: m.publishedAtUtc, reverse=True)
    return out[:_MESSAGE_LIMIT]


def upload_status(connection: Connection, *, tenant_id: str, actor_id: str) -> UploadStatus:
    rows = [dict(r) for r in connection.execute(
        text(_NOT_DONE_SQL),
        {"t": tenant_id, "a": actor_id, "days": STATUS_DAYS, "open_hours": OPEN_BULK_HOURS},
    ).mappings().all()]
    by_source = {k: sum(1 for r in rows if r["source"] == k) for k in ("UPLOAD", "BULK", "DRIVE")}
    return UploadStatus(
        upload=by_source["UPLOAD"], bulk=by_source["BULK"], drive=by_source["DRIVE"], total=len(rows),
        items=[NotUploaded(
            source=r["source"], itemId=r["item_id"], name=str(r["name"] or ""), reason=str(r["reason"] or ""),
            journeyId=str(r["journey_id"]) if r["journey_id"] else None, reference=r["reference"], atUtc=r["at_utc"],
        ) for r in rows[:_ITEM_LIMIT]],
        more=max(0, len(rows) - _ITEM_LIMIT),
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
    status = upload_status(connection, tenant_id=tenant_id, actor_id=actor)
    unread = sum(1 for m in messages if m.unread)
    return NotificationsResponse(
        messages=messages, unreadCount=unread, status=status, badgeCount=unread + status.total,
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
