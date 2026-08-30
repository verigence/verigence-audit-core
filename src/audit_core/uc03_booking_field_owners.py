from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from audit_core.errors import AuditCoreError


def _validation_error(detail: str) -> AuditCoreError:
    return AuditCoreError(
        error_code="VAC-VAL-002",
        status_code=422,
        title="Business validation failed",
        detail=detail,
    )


def _text_value(value: Any, field_label: str, *, max_length: int = 240) -> str:
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        raise _validation_error(f"{field_label} requires a scalar text value.")
    normalized = " ".join(str(value).split())
    if not normalized:
        raise _validation_error(f"{field_label} cannot be blank.")
    if len(normalized) > max_length:
        raise _validation_error(f"{field_label} exceeds {max_length} characters.")
    return normalized


def _date_value(value: Any, field_label: str) -> date:
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise _validation_error(f"{field_label} requires an ISO date (YYYY-MM-DD).")
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise _validation_error(f"{field_label} requires an ISO date (YYYY-MM-DD).") from exc


def _booking_id(connection: Connection, *, tenant_id: str, journey_id: UUID) -> UUID:
    return connection.execute(
        text(
            """
            SELECT booking_id
            FROM auditcore.bookings
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).scalar_one()


def apply_booking_field_owner(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    attribute_key: str,
    value: Any,
    source_evidence_id: UUID | None,
) -> tuple[str, str, str] | None:
    """Apply only fields with a verified, unambiguous Audit Core typed owner.

    The function deliberately returns None for commercial amounts, identity
    relationship evidence, and other fields whose committed business semantics
    are not proven. Those values remain in DI and can still receive reference-only
    attribute-resolution provenance.
    """

    if attribute_key == "booking_registration_by":
        normalized = _text_value(value, "Registration By")
        connection.execute(
            text(
                """
                INSERT INTO auditcore.registration_records (
                    tenant_id, journey_id, registration_by,
                    source_kind, source_evidence_id
                ) VALUES (
                    :tenant_id, :journey_id, :registration_by,
                    'EVIDENCE', :source_evidence_id
                )
                ON CONFLICT (tenant_id, journey_id) DO UPDATE SET
                    registration_by=EXCLUDED.registration_by,
                    updated_at_utc=now(),
                    version_no=auditcore.registration_records.version_no+1
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "registration_by": normalized,
                "source_evidence_id": source_evidence_id,
            },
        )
        record_id = connection.execute(
            text(
                """
                SELECT registration_record_id
                FROM auditcore.registration_records
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id},
        ).scalar_one()
        return "REGISTRATION", str(record_id), "APPLIED"

    if attribute_key == "booking_insurance_by":
        normalized = _text_value(value, "Insurance By")
        connection.execute(
            text(
                """
                INSERT INTO auditcore.insurance_records (
                    tenant_id, journey_id, insurance_by,
                    source_kind, source_evidence_id
                ) VALUES (
                    :tenant_id, :journey_id, :insurance_by,
                    'EVIDENCE', :source_evidence_id
                )
                ON CONFLICT (tenant_id, journey_id) DO UPDATE SET
                    insurance_by=EXCLUDED.insurance_by,
                    updated_at_utc=now(),
                    version_no=auditcore.insurance_records.version_no+1
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "insurance_by": normalized,
                "source_evidence_id": source_evidence_id,
            },
        )
        record_id = connection.execute(
            text(
                """
                SELECT insurance_record_id
                FROM auditcore.insurance_records
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id},
        ).scalar_one()
        return "INSURANCE", str(record_id), "APPLIED"

    if attribute_key == "expected_delivery_text":
        normalized = _text_value(value, "Expected Delivery")
        connection.execute(
            text(
                """
                INSERT INTO auditcore.bookings (
                    tenant_id, journey_id, expected_delivery_text
                ) VALUES (
                    :tenant_id, :journey_id, :expected_delivery_text
                )
                ON CONFLICT (tenant_id, journey_id) DO UPDATE SET
                    expected_delivery_text=EXCLUDED.expected_delivery_text,
                    updated_at_utc=now(),
                    version_no=auditcore.bookings.version_no+1
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "expected_delivery_text": normalized,
            },
        )
        return "BOOKING", str(_booking_id(connection, tenant_id=tenant_id, journey_id=journey_id)), "APPLIED"

    if attribute_key == "expected_delivery_date":
        normalized = _date_value(value, "Expected Delivery Date")
        connection.execute(
            text(
                """
                INSERT INTO auditcore.bookings (
                    tenant_id, journey_id, expected_delivery_date
                ) VALUES (
                    :tenant_id, :journey_id, :expected_delivery_date
                )
                ON CONFLICT (tenant_id, journey_id) DO UPDATE SET
                    expected_delivery_date=EXCLUDED.expected_delivery_date,
                    updated_at_utc=now(),
                    version_no=auditcore.bookings.version_no+1
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "expected_delivery_date": normalized,
            },
        )
        return "BOOKING", str(_booking_id(connection, tenant_id=tenant_id, journey_id=journey_id)), "APPLIED"

    return None
