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

Product owner 2026-11-18: "notification tab is quite vague... it doesnt tell which
customer/journey is the reported issue.. can you provide it clickable so that it can
directly open up that task or journey 360". So the alert names each booking's customer,
and the bell shows it as one line per booking (``booking_messages``), each opening that
booking. Each booking keeps the time it came up (``sinceUtc``), so a booking added to an
alert already open is new in the bell.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, text

from audit_core.uc03_p2_journey_views import _IN_FLIGHT

ALERT_KIND = "MY_DOCUMENTS_ATTENTION"
STUCK_HOURS = 6
FAILED_HOURS = 24
_MAX_LISTED = 8

_PAGES_SQL = """
    SELECT b.uploaded_by_actor_id AS actor, j.journey_id, j.journey_reference AS reference,
           auditcore.shown_name(c.legal_name, c.display_name) AS customer_name,
           count(*) FILTER (WHERE q.queue_status = ANY(CAST(:in_flight AS text[]))) AS stuck,
           count(*) FILTER (WHERE q.queue_status IN ('FAILED','DEAD_LETTER')) AS failed
    FROM auditcore.p2_document_queue q
    JOIN auditcore.p2_upload_batches b ON b.tenant_id=q.tenant_id AND b.batch_id=q.batch_id
    JOIN auditcore.journeys j ON j.tenant_id=q.tenant_id AND j.journey_id=q.journey_id
    LEFT JOIN auditcore.customers c ON c.tenant_id=j.tenant_id AND c.customer_id=j.customer_id
    WHERE q.tenant_id=:t AND q.retired_at_utc IS NULL AND q.merged_into_queue_id IS NULL
      AND b.uploaded_by_actor_id IS NOT NULL
      AND ((q.queue_status = ANY(CAST(:in_flight AS text[]))
            AND q.updated_at_utc < now() - make_interval(hours => :stuck_hours))
        OR (q.queue_status IN ('FAILED','DEAD_LETTER')
            AND q.updated_at_utc > now() - make_interval(hours => :failed_hours)))
    GROUP BY 1, 2, 3, 4
    ORDER BY 1, 3
"""

# auditcore.shown_name's answer when no name was read: not a name to show.
_NO_NAME = "Name not read"
_TO_DO = ("Open the booking's tasks: send a readable copy of a refused page; a stuck page "
          "is being looked at, re-upload it if it does not move.")


def _counts_text(stuck: int, failed: int) -> str:
    parts = []
    if stuck:
        parts.append(f"{stuck} page{'s' if stuck != 1 else ''} still being read after {STUCK_HOURS} hours")
    if failed:
        parts.append(f"{failed} page{'s' if failed != 1 else ''} failed or refused")
    return " and ".join(parts)


def _customer(name: Any) -> str | None:
    name = str(name or "").strip()
    return name if name and name != _NO_NAME else None


def _named(customer: Any, reference: Any) -> str:
    """"Rakesh Sahoo (UTK-SA-261111-001)", or the reference alone when no name was read."""
    name, reference = _customer(customer), str(reference or "").strip()
    return f"{name} ({reference})" if name and reference else name or reference or "A booking"


def booking_messages(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """The alert as the bell shows it: one line per booking, named by its customer."""
    out = []
    for entry in payload.get("journeys") or []:
        if not isinstance(entry, dict) or not entry.get("journeyId"):
            continue
        stuck, failed = int(entry.get("stuck") or 0), int(entry.get("failed") or 0)
        name = _customer(entry.get("customerName"))
        out.append({
            "journeyId": str(entry["journeyId"]), "reference": entry.get("reference"), "customerName": name,
            "stuck": stuck, "failed": failed, "sinceUtc": entry.get("sinceUtc"),
            "title": f"{name or entry.get('reference') or 'A booking'}: {_counts_text(stuck, failed) or 'pages need attention'}",
            "body": _TO_DO,
        })
    return out


def _body(rows: list[dict[str, Any]]) -> tuple[str, str]:
    stuck = sum(int(r["stuck"]) for r in rows)
    failed = sum(int(r["failed"]) for r in rows)
    title = "Your documents need attention: " + _counts_text(stuck, failed)
    listed = ", ".join(_named(r.get("customer_name"), r["reference"]) for r in rows[:_MAX_LISTED])
    more = f" and {len(rows) - _MAX_LISTED} more" if len(rows) > _MAX_LISTED else ""
    return title, f"On {listed}{more}. {_TO_DO}"


def _journeys(rows: list[dict[str, Any]], before: list[dict[str, Any]], *, now: datetime,
              fallback: datetime) -> list[dict[str, Any]]:
    """The alert's bookings. Each keeps the time it came up, or the time more of its pages went
    wrong; a booking of an alert written before 2026-11-18 takes the alert's own time."""
    known = {str(e.get("journeyId")): e for e in before if isinstance(e, dict)}
    out = []
    for row in rows:
        stuck, failed = int(row["stuck"]), int(row["failed"])
        old = known.get(str(row["journey_id"]))
        grew = old is None or stuck > int(old.get("stuck") or 0) or failed > int(old.get("failed") or 0)
        since = now.isoformat() if grew else old.get("sinceUtc") or fallback.isoformat()
        out.append({"journeyId": str(row["journey_id"]), "reference": row["reference"],
                    "customerName": _customer(row.get("customer_name")), "stuck": stuck, "failed": failed,
                    "sinceUtc": since})
    return out


def publish_document_alerts(connection: Connection, *, tenant_id: str) -> dict[str, int]:
    rows = [dict(r) for r in connection.execute(
        text(_PAGES_SQL), {"t": tenant_id, "in_flight": list(_IN_FLIGHT), "stuck_hours": STUCK_HOURS,
                           "failed_hours": FAILED_HOURS}).mappings().all()]
    by_actor: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_actor.setdefault(str(row["actor"]), []).append(row)
    existing = {str(r["actor"]): r for r in connection.execute(
        text("SELECT alert_id, payload->'audienceActorIds'->>0 AS actor, title, body, payload, published_at_utc "
             "FROM auditcore.p2_tenant_alerts "
             "WHERE tenant_id=:t AND alert_kind=:k AND alert_status='PUBLISHED' AND expires_at_utc > now()"),
        {"t": tenant_id, "k": ALERT_KIND}).mappings().all()}
    now = connection.execute(text("SELECT now()")).scalar_one()
    counts = {"published": 0, "updated": 0, "withdrawn": 0}
    for actor, mine in by_actor.items():
        title, body = _body(mine)
        current = existing.get(actor)
        before = list((current["payload"] or {}).get("journeys") or []) if current else []
        journeys = _journeys(mine, before, now=now, fallback=current["published_at_utc"] if current else now)
        payload = json.dumps({"audienceActorIds": [actor], "journeys": journeys})
        if current is None:
            connection.execute(text(
                "INSERT INTO auditcore.p2_tenant_alerts (tenant_id, alert_kind, alert_status, title, body, payload, "
                "published_at_utc, expires_at_utc) VALUES (:t, :k, 'PUBLISHED', :title, :body, CAST(:p AS jsonb), "
                "now(), now() + make_interval(hours => :h))"),
                {"t": tenant_id, "k": ALERT_KIND, "title": title, "body": body, "p": payload, "h": FAILED_HOURS})
            counts["published"] += 1
        elif (current["title"], current["body"], before) != (title, body, journeys):
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
