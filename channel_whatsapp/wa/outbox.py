from __future__ import annotations
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID
import json
import structlog
from sqlalchemy import Connection, text

logger = structlog.get_logger(__name__)
_MAX_ATTEMPTS = 3
_WINDOW_HOURS = 23


def enqueue_reply(connection, *, tenant_id, contact_id, session_id, kind, payload,
                  dedup_key=None, send_after=None):
    if dedup_key and session_id:
        existing = connection.execute(
            text("SELECT id FROM wa.outbox WHERE session_id=:sid AND kind=:kind"
                 " AND state='pending' AND payload->>'_dedup'=:dedup"),
            {"sid": session_id, "kind": kind, "dedup": dedup_key}
        ).scalar_one_or_none()
        if existing is not None:
            return existing
    stamped = {**payload}
    if dedup_key:
        stamped["_dedup"] = dedup_key
    row = connection.execute(
        text("""INSERT INTO wa.outbox (tenant_id, contact_id, session_id, kind, payload, send_after)
               VALUES (:tenant_id, :contact_id, :session_id, :kind, CAST(:payload AS jsonb),
                       COALESCE(:send_after, now())) RETURNING id"""),
        {"tenant_id": tenant_id, "contact_id": contact_id, "session_id": session_id,
         "kind": kind, "payload": json.dumps(stamped), "send_after": send_after}
    ).scalar_one()
    logger.info("wa_outbox_enqueued", outbox_id=row, kind=kind)
    return row


def claim_pending(connection, *, limit=20):
    connection.execute(text("UPDATE wa.outbox SET state='skipped_window'"
                            " WHERE state='pending' AND send_after < now() - interval '23 hours'"))
    rows = connection.execute(
        text("""UPDATE wa.outbox SET state='pending', attempts=attempts+1
               WHERE id IN (SELECT id FROM wa.outbox WHERE state='pending'
                            AND send_after <= now() AND attempts < :max
                            ORDER BY send_after FOR UPDATE SKIP LOCKED LIMIT :limit)
               RETURNING id, contact_id, session_id, kind, payload, attempts"""),
        {"limit": limit, "max": _MAX_ATTEMPTS}
    ).mappings().all()
    return [dict(r) for r in rows]


def mark_sent(connection, *, outbox_id, wamid):
    connection.execute(
        text("UPDATE wa.outbox SET state='sent', sent_at=now(), wamid=:wamid WHERE id=:oid"),
        {"oid": outbox_id, "wamid": wamid}
    )


def mark_failed(connection, *, outbox_id, error, permanent=False):
    if permanent:
        connection.execute(text("UPDATE wa.outbox SET state='failed', last_error=:err WHERE id=:oid"),
                           {"oid": outbox_id, "err": error})
    else:
        connection.execute(
            text("UPDATE wa.outbox SET state='pending', last_error=:err,"
                 " send_after=now()+(interval '30 seconds' * power(2, attempts)) WHERE id=:oid"),
            {"oid": outbox_id, "err": error}
        )


def within_window(*, last_at: datetime | None) -> bool:
    if last_at is None:
        return False
    return (datetime.now(tz=timezone.utc) - last_at) < timedelta(hours=_WINDOW_HOURS)
