"""Each PC is told about their own documents that are stuck or failed (product owner 2026-11-15).

    "stuck documents. failed jobs should be intimated to each PC as notification"

Every few minutes the worker looks, per PC (the person who uploaded the file),
at their pages that are

- stuck: still being read more than 6 hours after the last change, or
- failed: refused or failed in the last 24 hours and not yet replaced,

and keeps ONE alert per PC (kind MY_DOCUMENTS_ATTENTION) saying how many and on
which bookings. It is shown only to that PC (``audienceActorIds``), refreshed
while anything is left, and withdrawn once nothing is. Same banner as the other
Project alerts; dismissing it hides it until it changes.
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import Connection, text

from audit_core.uc03_p2_journey_views import _IN_FLIGHT

ALERT_KIND = "MY_DOCUMENTS_ATTENTION"
STUCK_HOURS = 6
FAILED_HOURS = 24
_MAX_LISTED = 8

_PAGES_SQL = """
    SELECT b.uploaded_by_actor_id AS actor, j.journey_id, j.journey_reference AS reference,
           count(*) FILTER (WHERE q.queue_status = ANY(CAST(:in_flight AS text[]))) AS stuck,
           count(*) FILTER (WHERE q.queue_status IN ('FAILED','DEAD_LETTER')) AS failed
    FROM auditcore.p2_document_queue q
    JOIN auditcore.p2_upload_batches b ON b.tenant_id=q.tenant_id AND b.batch_id=q.batch_id
    JOIN auditcore.journeys j ON j.tenant_id=q.tenant_id AND j.journey_id=q.journey_id
    WHERE q.tenant_id=:t AND q.retired_at_utc IS NULL AND q.merged_into_queue_id IS NULL
      AND b.uploaded_by_actor_id IS NOT NULL
      AND ((q.queue_status = ANY(CAST(:in_flight AS text[]))
            AND q.updated_at_utc < now() - make_interval(hours => :stuck_hours))
        OR (q.queue_status IN ('FAILED','DEAD_LETTER')
            AND q.updated_at_utc > now() - make_interval(hours => :failed_hours)))
    GROUP BY 1, 2, 3
    ORDER BY 1, 3
"""


def _body(rows: list[dict[str, Any]]) -> tuple[str, str]:
    stuck = sum(int(r["stuck"]) for r in rows)
    failed = sum(int(r["failed"]) for r in rows)
    parts = []
    if stuck:
        parts.append(f"{stuck} page{'s' if stuck != 1 else ''} still being read after {STUCK_HOURS} hours")
    if failed:
        parts.append(f"{failed} page{'s' if failed != 1 else ''} failed or refused")
    title = "Your documents need attention: " + " and ".join(parts)
    listed = ", ".join(r["reference"] for r in rows[:_MAX_LISTED])
    more = f" and {len(rows) - _MAX_LISTED} more" if len(rows) > _MAX_LISTED else ""
    body = (f"On {listed}{more}. Open the booking's tasks: send a readable copy of a refused page; a stuck page "
            "is being looked at, re-upload it if it does not move.")
    return title, body


def publish_document_alerts(connection: Connection, *, tenant_id: str) -> dict[str, int]:
    rows = [dict(r) for r in connection.execute(
        text(_PAGES_SQL), {"t": tenant_id, "in_flight": list(_IN_FLIGHT), "stuck_hours": STUCK_HOURS,
                           "failed_hours": FAILED_HOURS}).mappings().all()]
    by_actor: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_actor.setdefault(str(row["actor"]), []).append(row)
    existing = {str(r["actor"]): r for r in connection.execute(
        text("SELECT alert_id, payload->'audienceActorIds'->>0 AS actor, title, body FROM auditcore.p2_tenant_alerts "
             "WHERE tenant_id=:t AND alert_kind=:k AND alert_status='PUBLISHED' AND expires_at_utc > now()"),
        {"t": tenant_id, "k": ALERT_KIND}).mappings().all()}
    counts = {"published": 0, "updated": 0, "withdrawn": 0}
    for actor, mine in by_actor.items():
        title, body = _body(mine)
        payload = json.dumps({"audienceActorIds": [actor],
                              "journeys": [{"journeyId": str(r["journey_id"]), "reference": r["reference"],
                                            "stuck": int(r["stuck"]), "failed": int(r["failed"])} for r in mine]})
        current = existing.get(actor)
        if current is None:
            connection.execute(text(
                "INSERT INTO auditcore.p2_tenant_alerts (tenant_id, alert_kind, alert_status, title, body, payload, "
                "published_at_utc, expires_at_utc) VALUES (:t, :k, 'PUBLISHED', :title, :body, CAST(:p AS jsonb), "
                "now(), now() + make_interval(hours => :h))"),
                {"t": tenant_id, "k": ALERT_KIND, "title": title, "body": body, "p": payload, "h": FAILED_HOURS})
            counts["published"] += 1
        elif (current["title"], current["body"]) != (title, body):
            connection.execute(text(
                "UPDATE auditcore.p2_tenant_alerts SET title=:title, body=:body, payload=CAST(:p AS jsonb), "
                "expires_at_utc=now() + make_interval(hours => :h), updated_at_utc=now() "
                "WHERE tenant_id=:t AND alert_id=:id"),
                {"t": tenant_id, "id": current["alert_id"], "title": title, "body": body, "p": payload,
                 "h": FAILED_HOURS})
            counts["updated"] += 1
    gone = [r["alert_id"] for actor, r in existing.items() if actor not in by_actor]
    if gone:
        connection.execute(text(
            "UPDATE auditcore.p2_tenant_alerts SET expires_at_utc=now(), "
            "updated_at_utc=now() WHERE tenant_id=:t AND alert_id = ANY(:ids)"), {"t": tenant_id, "ids": gone})
        counts["withdrawn"] = len(gone)
    return counts
