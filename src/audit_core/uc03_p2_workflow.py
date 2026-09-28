"""Phase 2 on the existing journey workflow.

Phase 2 does not keep its own stage timeline. Milestones are recorded
where Phase 1 already records them, so every screen, report and the
legacy queue read one history:

* auditcore.journey_stage_states -- one row per stage: first_started_at_utc,
  capture_completed_at_utc (documents submitted), business_completed_at_utc
  and business_status / closure_disposition (Phase 1's own vocabulary:
  BOOKING_STARTED / BOOKING_IN_PROGRESS / BOOKING_CLOSED + PROCEED_TO_DELIVERY,
  BOOKING_CANCELLED, DUPLICATE_BOOKING, NO_DELIVERY), with version_no bumped
  on every change so Phase 1 screens holding the old version refresh.
* auditcore.journey_workflow_events -- the append-only who/when/role log.

Delivery is started with Phase 1's own idempotent ensure_delivery_started.
"""
from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Connection, text

_OPEN_BOOKING = ("BOOKING_STARTED", "BOOKING_IN_PROGRESS")
CANCELLED_BOOKING = ("BOOKING_CANCELLED", "DUPLICATE_BOOKING")


def append_workflow_event(
    connection: Connection, *, tenant_id: str, journey_id: UUID, stage: str, event_type: str,
    source_kind: str, actor_id: str | None, actor_role: str | None, payload: dict[str, Any],
    aggregate_version: int, correlation_id: str | None = None,
) -> None:
    connection.execute(
        text(
            """
            INSERT INTO auditcore.journey_workflow_events (
                tenant_id, journey_id, stage_code, event_type, source_kind, actor_id, actor_role_snapshot,
                idempotency_key, correlation_id, safe_payload, occurred_at_utc, aggregate_version
            ) VALUES (:t, :j, :stage, :type, :kind, :actor, :role, :key, :cid, CAST(:payload AS jsonb), now(), :version)
            """
        ),
        {"t": tenant_id, "j": journey_id, "stage": stage, "type": event_type, "kind": source_kind,
         "actor": actor_id, "role": actor_role, "key": f"p2:{event_type}:{journey_id}:{uuid4()}",
         "cid": correlation_id, "payload": json.dumps(payload, default=str), "version": max(1, aggregate_version)},
    )


def mark_booking_completed(connection: Connection, *, tenant_id: str, journey_id: UUID,
                           correlation_id: str | None = None) -> bool:
    """Phase 2 reached Booking Complete: close the Booking stage the way
    Phase 1 does (BOOKING_CLOSED, proceed to delivery), creating the stage
    row if nothing created it yet. Idempotent; never touches a Booking that
    is already closed, cancelled or a duplicate."""
    row = connection.execute(
        text(
            """
            INSERT INTO auditcore.journey_stage_states (
                tenant_id, journey_id, stage_code, business_status, closure_disposition, audit_state, audit_status,
                first_started_at_utc, capture_completed_at_utc, business_completed_at_utc, closed_at_utc,
                latest_activity_at_utc, version_no
            ) VALUES (:t, :j, 'BOOKING', 'BOOKING_CLOSED', 'PROCEED_TO_DELIVERY', 'IN_PROGRESS', 'NOT_EVALUATED',
                      now(), now(), now(), now(), now(), 1)
            ON CONFLICT (tenant_id, journey_id, stage_code) DO UPDATE
               SET business_status='BOOKING_CLOSED',
                   closure_disposition=COALESCE(journey_stage_states.closure_disposition, 'PROCEED_TO_DELIVERY'),
                   capture_completed_at_utc=COALESCE(journey_stage_states.capture_completed_at_utc, now()),
                   business_completed_at_utc=COALESCE(journey_stage_states.business_completed_at_utc, now()),
                   closed_at_utc=COALESCE(journey_stage_states.closed_at_utc, now()),
                   latest_activity_at_utc=now(), updated_at_utc=now(),
                   version_no=journey_stage_states.version_no + 1
             WHERE journey_stage_states.business_status = ANY(:open)
            RETURNING version_no
            """
        ),
        {"t": tenant_id, "j": journey_id, "open": list(_OPEN_BOOKING)},
    ).scalar_one_or_none()
    if row is None:
        return False
    append_workflow_event(
        connection, tenant_id=tenant_id, journey_id=journey_id, stage="BOOKING", event_type="P2_BOOKING_COMPLETED",
        source_kind="MACHINE", actor_id=None, actor_role=None,
        payload={"reason": "Every Booking gate passed", "closureDisposition": "PROCEED_TO_DELIVERY"},
        aggregate_version=int(row), correlation_id=correlation_id,
    )
    return True


def mark_delivery_completed(connection: Connection, *, tenant_id: str, journey_id: UUID,
                            gates: dict[str, Any] | None = None, correlation_id: str | None = None) -> bool:
    """Every Delivery rule passed: record DELIVERY_COMPLETED on the journey
    workflow (creating the Delivery stage row if nothing created it yet);
    the TL review follows. Idempotent."""
    version = connection.execute(
        text(
            """
            INSERT INTO auditcore.journey_stage_states (
                tenant_id, journey_id, stage_code, business_status, audit_state, audit_status,
                first_started_at_utc, capture_completed_at_utc, business_completed_at_utc,
                latest_activity_at_utc, version_no
            ) VALUES (:t, :j, 'DELIVERY', 'DELIVERY_COMPLETED', 'IN_PROGRESS', 'NOT_EVALUATED',
                      now(), now(), now(), now(), 1)
            ON CONFLICT (tenant_id, journey_id, stage_code) DO UPDATE
               SET business_status='DELIVERY_COMPLETED',
                   capture_completed_at_utc=COALESCE(journey_stage_states.capture_completed_at_utc, now()),
                   business_completed_at_utc=COALESCE(journey_stage_states.business_completed_at_utc, now()),
                   latest_activity_at_utc=now(), updated_at_utc=now(),
                   version_no=journey_stage_states.version_no + 1
             WHERE journey_stage_states.business_status IS DISTINCT FROM 'DELIVERY_COMPLETED'
            RETURNING version_no
            """
        ),
        {"t": tenant_id, "j": journey_id},
    ).scalar_one_or_none()
    if version is None:
        return False
    append_workflow_event(
        connection, tenant_id=tenant_id, journey_id=journey_id, stage="DELIVERY", event_type="P2_DELIVERY_COMPLETED",
        source_kind="MACHINE", actor_id=None, actor_role=None,
        payload={"rules": gates or {}, "next": "TL_REVIEW"}, aggregate_version=int(version),
        correlation_id=correlation_id,
    )
    return True


def mark_delivery_reviewed(connection: Connection, *, tenant_id: str, journey_id: UUID, actor_id: str | None,
                           actor_role: str | None, correlation_id: str | None = None) -> bool:
    """The TL reviewed the completed Delivery."""
    updated = connection.execute(
        text(
            """
            UPDATE auditcore.journeys SET review_completed_at_utc=COALESCE(review_completed_at_utc, now())
            WHERE tenant_id=:t AND journey_id=:j AND review_completed_at_utc IS NULL
            RETURNING version_no
            """
        ),
        {"t": tenant_id, "j": journey_id},
    ).scalar_one_or_none()
    if updated is None:
        return False
    stage_version = connection.execute(
        text("SELECT version_no FROM auditcore.journey_stage_states WHERE tenant_id=:t AND journey_id=:j "
             "AND stage_code='DELIVERY'"),
        {"t": tenant_id, "j": journey_id},
    ).scalar_one_or_none()
    append_workflow_event(
        connection, tenant_id=tenant_id, journey_id=journey_id, stage="DELIVERY", event_type="P2_DELIVERY_REVIEWED",
        source_kind="HUMAN" if actor_id else "MACHINE", actor_id=actor_id, actor_role=actor_role,
        payload={}, aggregate_version=int(stage_version or 1), correlation_id=correlation_id,
    )
    return True


def delivery_reviewed(connection: Connection, *, tenant_id: str, journey_id: UUID) -> bool:
    return connection.execute(
        text("SELECT review_completed_at_utc IS NOT NULL FROM auditcore.journeys WHERE tenant_id=:t AND journey_id=:j"),
        {"t": tenant_id, "j": journey_id},
    ).scalar_one()


def last_submission(connection: Connection, *, tenant_id: str, journey_id: UUID, stage: str) -> Any:
    return connection.execute(
        text(
            """
            SELECT MAX(occurred_at_utc) FROM auditcore.journey_workflow_events
            WHERE tenant_id=:t AND journey_id=:j AND stage_code=:stage
              AND event_type = ANY(:types)
            """
        ),
        {"t": tenant_id, "j": journey_id, "stage": stage,
         "types": [f"P2_{stage}_DOCUMENTS_SUBMITTED", "PC_BOOKING_CAPTURE_SUBMITTED"]},
    ).scalar_one_or_none()


def timeline(connection: Connection, *, tenant_id: str, journey_id: UUID) -> dict[str, Any]:
    """Milestones per stage and the time each role spent, from the existing
    workflow tables and every task (Phase 2 and Phase 1) on the Journey."""
    stages = {
        str(r["stage_code"]): dict(r)
        for r in connection.execute(
            text(
                """
                SELECT stage_code, business_status, closure_disposition, first_started_at_utc,
                       capture_completed_at_utc, business_completed_at_utc, pc_verification_status,
                       booking_confirm_date, booking_confirmed_at_utc
                FROM auditcore.journey_stage_states WHERE tenant_id=:t AND journey_id=:j
                """
            ),
            {"t": tenant_id, "j": journey_id},
        ).mappings().all()
    }
    created = connection.execute(
        text("SELECT created_at_utc FROM auditcore.journeys WHERE tenant_id=:t AND journey_id=:j"),
        {"t": tenant_id, "j": journey_id},
    ).scalar_one()
    delivered = connection.execute(
        text("SELECT actual_delivered_at FROM auditcore.deliveries WHERE tenant_id=:t AND journey_id=:j"),
        {"t": tenant_id, "j": journey_id},
    ).scalar_one_or_none()

    def hours(start: Any, end: Any) -> float | None:
        return round((end - start).total_seconds() / 3600, 1) if start and end else None

    out_stages = {}
    for code in ("BOOKING", "DELIVERY"):
        row = stages.get(code) or {}
        started = row.get("first_started_at_utc") or (created if code == "BOOKING" else None)
        completed = row.get("business_completed_at_utc") or (delivered if code == "DELIVERY" else None)
        cancelled = code == "BOOKING" and (
            row.get("business_status") in CANCELLED_BOOKING or row.get("closure_disposition") == "NO_DELIVERY"
        )
        out_stages[code] = {
            "status": row.get("business_status"),
            "startedAtUtc": started,
            "submittedAtUtc": last_submission(connection, tenant_id=tenant_id, journey_id=journey_id, stage=code)
            or row.get("capture_completed_at_utc"),
            "completedAtUtc": None if cancelled else completed,
            "cancelled": cancelled,
            "hoursToSubmit": hours(started, row.get("capture_completed_at_utc")),
            "hoursToComplete": None if cancelled else hours(started, completed),
            "bookingConfirmDate": row.get("booking_confirm_date") if code == "BOOKING" else None,
        }
    roles = connection.execute(
        text(
            """
            SELECT role, COUNT(*) AS tasks, COUNT(*) FILTER (WHERE closed_at IS NULL) AS open,
                   AVG(EXTRACT(EPOCH FROM closed_at - created_at) / 3600.0) FILTER (WHERE closed_at IS NOT NULL)
                     AS avg_hours,
                   SUM(EXTRACT(EPOCH FROM COALESCE(closed_at, now()) - created_at) / 3600.0) AS total_hours
            FROM (
              SELECT assigned_role_code AS role, created_at_utc AS created_at,
                     CASE WHEN task_status IN ('VERIFIED_COMPLETE','CANCELLED','FAILED','DEAD_LETTER')
                          THEN COALESCE(verified_at_utc, updated_at_utc) END AS closed_at
              FROM auditcore.p2_tasks WHERE tenant_id=:t AND journey_id=:j
              UNION ALL
              SELECT assigned_role_code, created_at_utc,
                     CASE WHEN task_status IN ('COMPLETED','CANCELLED','FAILED','DEAD_LETTER')
                          THEN COALESCE(completed_at_utc, cancelled_at_utc, updated_at_utc) END
              FROM auditcore.workflow_tasks
              WHERE tenant_id=:t AND journey_id=:j AND assigned_role_code IN ('PC','TL','PM','EXECUTIVE')
            ) t
            GROUP BY role ORDER BY role
            """
        ),
        {"t": tenant_id, "j": journey_id},
    ).mappings().all()
    events = connection.execute(
        text(
            """
            SELECT stage_code, event_type, source_kind, actor_role_snapshot, occurred_at_utc
            FROM auditcore.journey_workflow_events WHERE tenant_id=:t AND journey_id=:j
            ORDER BY occurred_at_utc DESC LIMIT 40
            """
        ),
        {"t": tenant_id, "j": journey_id},
    ).mappings().all()
    return {
        "stages": out_stages,
        "roles": [
            {"role": r["role"], "tasks": int(r["tasks"]), "open": int(r["open"]),
             "avgHoursToClose": round(float(r["avg_hours"]), 1) if r["avg_hours"] is not None else None,
             "totalHours": round(float(r["total_hours"] or 0), 1)}
            for r in roles
        ],
        "workflowEvents": [dict(e) for e in events],
    }
