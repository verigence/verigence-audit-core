from __future__ import annotations

import json
from datetime import date
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text


def create_daily_ops_run(
    connection: Connection,
    *,
    tenant_id: str,
    outlet_id: UUID,
    business_date: date,
    pc_actor_id: str,
    correlation_id: str | None = None,
) -> UUID:
    return connection.execute(
        text(
            """
            INSERT INTO auditcore.daily_ops_runs (
                tenant_id, outlet_id, business_date, pc_actor_id, correlation_id
            ) VALUES (
                :tenant_id, :outlet_id, :business_date, :pc_actor_id, :correlation_id
            )
            RETURNING daily_ops_run_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "outlet_id": outlet_id,
            "business_date": business_date,
            "pc_actor_id": pc_actor_id,
            "correlation_id": correlation_id,
        },
    ).scalar_one()


def add_daily_ops_item(
    connection: Connection,
    *,
    tenant_id: str,
    daily_ops_run_id: UUID,
    item_type: str,
    journey_id: UUID | None = None,
    evidence_id: UUID | None = None,
    details: dict[str, Any] | None = None,
) -> UUID:
    return connection.execute(
        text(
            """
            INSERT INTO auditcore.daily_ops_items (
                tenant_id, daily_ops_run_id, item_type,
                journey_id, evidence_id, details
            ) VALUES (
                :tenant_id, :daily_ops_run_id, :item_type,
                :journey_id, :evidence_id, CAST(:details AS jsonb)
            )
            RETURNING daily_ops_item_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "daily_ops_run_id": daily_ops_run_id,
            "item_type": item_type,
            "journey_id": journey_id,
            "evidence_id": evidence_id,
            "details": json.dumps(details or {}),
        },
    ).scalar_one()


def set_daily_ops_item_status(
    connection: Connection,
    *,
    tenant_id: str,
    daily_ops_item_id: UUID,
    item_status: str,
    details: dict[str, Any] | None = None,
) -> None:
    if item_status not in {"COMPLETED", "EXCEPTION", "NOT_APPLICABLE"}:
        raise ValueError("item_status must be COMPLETED, EXCEPTION or NOT_APPLICABLE")
    row = connection.execute(
        text(
            """
            UPDATE auditcore.daily_ops_items
            SET item_status = :item_status,
                details = COALESCE(CAST(:details AS jsonb), details),
                completed_at_utc = now(),
                updated_at_utc = now()
            WHERE tenant_id = :tenant_id
              AND daily_ops_item_id = :daily_ops_item_id
            RETURNING daily_ops_item_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "daily_ops_item_id": daily_ops_item_id,
            "item_status": item_status,
            "details": json.dumps(details) if details is not None else None,
        },
    ).scalar_one_or_none()
    if row is None:
        raise LookupError("daily operations item not found")


def record_activity(
    connection: Connection,
    *,
    tenant_id: str,
    actor_id: str,
    activity_type: str,
    actor_role_code: str | None = None,
    outlet_id: UUID | None = None,
    journey_id: UUID | None = None,
    details: dict[str, Any] | None = None,
) -> UUID:
    return connection.execute(
        text(
            """
            INSERT INTO auditcore.activity_records (
                tenant_id, outlet_id, journey_id, actor_id,
                actor_role_code, activity_type, details
            ) VALUES (
                :tenant_id, :outlet_id, :journey_id, :actor_id,
                :actor_role_code, :activity_type, CAST(:details AS jsonb)
            )
            RETURNING activity_record_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "outlet_id": outlet_id,
            "journey_id": journey_id,
            "actor_id": actor_id,
            "actor_role_code": actor_role_code,
            "activity_type": activity_type,
            "details": json.dumps(details or {}),
        },
    ).scalar_one()


def add_pc_daily_note(
    connection: Connection,
    *,
    tenant_id: str,
    pc_actor_id: str,
    note_date: date,
    note_text: str,
    outlet_id: UUID | None = None,
) -> UUID:
    if not note_text.strip():
        raise ValueError("note_text is required")
    return connection.execute(
        text(
            """
            INSERT INTO auditcore.pc_daily_notes (
                tenant_id, pc_actor_id, outlet_id, note_date, note_text
            ) VALUES (
                :tenant_id, :pc_actor_id, :outlet_id, :note_date, :note_text
            )
            RETURNING pc_daily_note_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "pc_actor_id": pc_actor_id,
            "outlet_id": outlet_id,
            "note_date": note_date,
            "note_text": note_text,
        },
    ).scalar_one()


def list_pc_daily_notes(
    connection: Connection,
    *,
    tenant_id: str,
    pc_actor_id: str,
    note_date: date | None = None,
):
    return connection.execute(
        text(
            """
            SELECT pc_daily_note_id, pc_actor_id, outlet_id,
                   note_date, note_text, created_at_utc, updated_at_utc
            FROM auditcore.pc_daily_notes
            WHERE tenant_id = :tenant_id
              AND pc_actor_id = :pc_actor_id
              AND (:note_date IS NULL OR note_date = :note_date)
            ORDER BY note_date, created_at_utc, pc_daily_note_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "pc_actor_id": pc_actor_id,
            "note_date": note_date,
        },
    ).mappings().all()


def complete_daily_ops_run(
    connection: Connection,
    *,
    tenant_id: str,
    daily_ops_run_id: UUID,
    run_status: str = "COMPLETED",
) -> None:
    if run_status not in {"COMPLETED", "EXCEPTION"}:
        raise ValueError("run_status must be COMPLETED or EXCEPTION")
    row = connection.execute(
        text(
            """
            UPDATE auditcore.daily_ops_runs
            SET run_status = :run_status,
                completed_at_utc = now(),
                updated_at_utc = now(),
                version_no = version_no + 1
            WHERE tenant_id = :tenant_id
              AND daily_ops_run_id = :daily_ops_run_id
              AND run_status = 'IN_PROGRESS'
            RETURNING daily_ops_run_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "daily_ops_run_id": daily_ops_run_id,
            "run_status": run_status,
        },
    ).scalar_one_or_none()
    if row is None:
        raise LookupError("daily operations run not found or already completed")


def get_daily_ops_run(
    connection: Connection,
    *,
    tenant_id: str,
    daily_ops_run_id: UUID,
):
    row = connection.execute(
        text(
            """
            SELECT daily_ops_run_id, outlet_id, business_date, pc_actor_id,
                   run_status, started_at_utc, completed_at_utc,
                   correlation_id, version_no
            FROM auditcore.daily_ops_runs
            WHERE tenant_id = :tenant_id
              AND daily_ops_run_id = :daily_ops_run_id
            """
        ),
        {"tenant_id": tenant_id, "daily_ops_run_id": daily_ops_run_id},
    ).mappings().one_or_none()
    if row is None:
        raise LookupError("daily operations run not found")
    return row
