from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

logger = logging.getLogger(__name__)

_REVIEWED_CUSTOMER_FIELDS = (
    "customer_name",
    "customer_phone",
    "customer_email",
    "customer_address",
    "relation_type",
    "relation_name",
    "customer_relation_type",
    "customer_relation_name",
)


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    result = str(value).strip()
    return result or None


def _normalize_mobile(value: Any) -> str | None:
    text_value = _clean_text(value)
    if text_value is None:
        return None
    digits = "".join(ch for ch in text_value if ch.isdigit())
    return digits or None


def _normalize_relation_type(value: Any) -> str | None:
    text_value = _clean_text(value)
    if text_value is None:
        return None
    normalized = text_value.upper().replace(" ", "")
    aliases = {
        "SO": "S/O",
        "S/O": "S/O",
        "SONOF": "S/O",
        "DO": "D/O",
        "D/O": "D/O",
        "DAUGHTEROF": "D/O",
        "WO": "W/O",
        "W/O": "W/O",
        "WIFEOF": "W/O",
    }
    return aliases.get(normalized, text_value.upper())


def apply_reviewed_customer_values(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    actor_id: str,
) -> list[str]:
    """Apply final reviewed Booking customer facts to the existing Customer once.

    The Journey/Customer shell already exists before Review. This function does not
    create a second Customer and does not call DI. It reads the accepted/corrected
    Booking proposals once, compares them with the current Customer row in memory,
    and performs at most one Customer UPDATE when Review is submitted.
    """

    row = connection.execute(
        text(
            """
            WITH ranked AS (
                SELECT
                    p.field_key,
                    CASE
                        WHEN jsonb_typeof(COALESCE(p.accepted_value, p.proposed_value)) = 'object'
                            THEN COALESCE(p.accepted_value, p.proposed_value)->>'value'
                        ELSE trim(both '"' from COALESCE(p.accepted_value, p.proposed_value)::text)
                    END AS reviewed_value,
                    row_number() OVER (
                        PARTITION BY p.field_key
                        ORDER BY
                            CASE lower(COALESCE(p.source_document_type_key, ''))
                                WHEN 'booking_form' THEN 0
                                WHEN 'booking_docket' THEN 1
                                ELSE 2
                            END,
                            p.accepted_at_utc DESC NULLS LAST,
                            p.updated_at_utc DESC,
                            p.capture_proposal_id DESC
                    ) AS rn
                FROM auditcore.journey_capture_proposals p
                WHERE p.tenant_id = :tenant_id
                  AND p.journey_id = :journey_id
                  AND p.stage_code = 'BOOKING'
                  AND p.proposal_status IN ('ACCEPTED','CORRECTED')
                  AND lower(COALESCE(p.source_document_type_key, '')) IN ('booking_form','booking_docket')
                  AND p.field_key = ANY(:field_keys)
            ), reviewed AS (
                SELECT COALESCE(
                    jsonb_object_agg(field_key, reviewed_value) FILTER (WHERE rn = 1),
                    '{}'::jsonb
                ) AS values
                FROM ranked
            )
            SELECT
                c.customer_id,
                c.display_name,
                c.mobile_number,
                c.mobile_last4,
                c.email_reference,
                c.address_text,
                c.relation_type_code,
                c.relation_name,
                reviewed.values AS reviewed_values
            FROM auditcore.journeys j
            JOIN auditcore.customers c
              ON c.tenant_id = j.tenant_id
             AND c.customer_id = j.customer_id
            CROSS JOIN reviewed
            WHERE j.tenant_id = :tenant_id
              AND j.journey_id = :journey_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "field_keys": list(_REVIEWED_CUSTOMER_FIELDS),
        },
    ).mappings().one_or_none()
    if row is None:
        raise RuntimeError("UC03 Review could not resolve the Journey Customer")

    reviewed = dict(row["reviewed_values"] or {})
    candidate = {
        "display_name": _clean_text(reviewed.get("customer_name")) or row["display_name"],
        "mobile_number": _normalize_mobile(reviewed.get("customer_phone")) or row["mobile_number"],
        "email_reference": _clean_text(reviewed.get("customer_email")) or row["email_reference"],
        "address_text": _clean_text(reviewed.get("customer_address")) or row["address_text"],
        "relation_type_code": _normalize_relation_type(
            reviewed.get("customer_relation_type") or reviewed.get("relation_type")
        )
        or row["relation_type_code"],
        "relation_name": _clean_text(
            reviewed.get("customer_relation_name") or reviewed.get("relation_name")
        )
        or row["relation_name"],
    }
    candidate["mobile_last4"] = (
        candidate["mobile_number"][-4:]
        if candidate["mobile_number"] and len(candidate["mobile_number"]) >= 4
        else row["mobile_last4"]
    )

    field_map = {
        "display_name": "CUSTOMER_NAME",
        "mobile_number": "CUSTOMER_NUMBER",
        "mobile_last4": "CUSTOMER_NUMBER",
        "email_reference": "CUSTOMER_EMAIL",
        "address_text": "CUSTOMER_ADDRESS",
        "relation_type_code": "CUSTOMER_RELATION_TYPE",
        "relation_name": "CUSTOMER_RELATION_NAME",
    }
    changed = []
    for column, field_key in field_map.items():
        if candidate[column] != row[column] and field_key not in changed:
            changed.append(field_key)

    if not changed:
        logger.info(
            "uc03_customer_review_unchanged tenant_id=%s journey_id=%s customer_id=%s",
            tenant_id,
            journey_id,
            row["customer_id"],
        )
        return []

    connection.execute(
        text(
            """
            UPDATE auditcore.customers
            SET display_name = :display_name,
                mobile_number = :mobile_number,
                mobile_last4 = :mobile_last4,
                email_reference = :email_reference,
                address_text = :address_text,
                relation_type_code = :relation_type_code,
                relation_name = :relation_name,
                updated_by_actor_id = :actor_id,
                updated_at_utc = now(),
                version_no = version_no + 1
            WHERE tenant_id = :tenant_id
              AND customer_id = :customer_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "customer_id": row["customer_id"],
            "actor_id": actor_id,
            **candidate,
        },
    )
    logger.info(
        "uc03_customer_review_updated tenant_id=%s journey_id=%s customer_id=%s fields=%s",
        tenant_id,
        journey_id,
        row["customer_id"],
        ",".join(changed),
    )
    return changed
