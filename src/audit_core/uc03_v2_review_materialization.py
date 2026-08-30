from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from audit_core import uc03_booking_capture
from audit_core.uc03_attribute_mapping import spec_for_field
from audit_core.uc03_booking_receipt_capture import (
    _RECEIPT_CAPTURE_MAP,
    _RECEIPT_DOCUMENT_TYPE,
)

logger = logging.getLogger(__name__)

_BOOKING_FORM_DOCUMENT_TYPE = "booking_form"
_PAN_DOCUMENT_TYPES = {"pan", "pan_card"}
_AADHAAR_DOCUMENT_TYPE = "aadhaar"

_BOOKING_FORM_FIELDS = (
    "dealer_name",
    "dealer_branch",
    "booking_reference_number",
    "booking_date",
    "customer_name",
    "customer_phone",
    "customer_email",
    "customer_address",
    "vehicle_model",
    "vehicle_variant",
    "vehicle_color",
    "sku_code",
    "sales_person",
    "registration_by",
    "registration_type",
    "insurance_by",
    "exchange_applicable",
    "exchange_value",
    "ex_showroom_price",
    "insurance_amount",
    "registration_charges",
    "road_tax_amount",
    "road_tax_registration",
    "tcs_amount",
    "rsa_amount",
    "additional_warranty_amount",
    "accessories_cost",
    "other_charges",
    "discount_amount",
    "bonus_amount",
    "total_price",
    "net_amount",
    "booking_amount_paid",
    "balance_amount",
    "mode_of_payment",
    "payment_reference_no",
    "expected_delivery",
    "expected_delivery_date",
)
_BOOKING_DATE_FIELDS = {"booking_date", "expected_delivery_date"}
_BOOKING_BOOLEAN_FIELDS = {"exchange_applicable"}
_BOOKING_DECIMAL_FIELDS = {
    "exchange_value",
    "ex_showroom_price",
    "insurance_amount",
    "registration_charges",
    "road_tax_amount",
    "road_tax_registration",
    "tcs_amount",
    "rsa_amount",
    "additional_warranty_amount",
    "accessories_cost",
    "other_charges",
    "discount_amount",
    "bonus_amount",
    "total_price",
    "net_amount",
    "booking_amount_paid",
    "balance_amount",
}
_COMMERCIAL_LINE_FIELDS = {
    "ex_showroom_price",
    "insurance_amount",
    "registration_charges",
    "road_tax_amount",
    "road_tax_registration",
    "tcs_amount",
    "rsa_amount",
    "additional_warranty_amount",
    "accessories_cost",
    "other_charges",
    "discount_amount",
    "bonus_amount",
    "total_price",
    "net_amount",
    "booking_amount_paid",
    "balance_amount",
}

_PAN_FIELDS = {
    "pan_number",
    "pan_name",
    "pan_father_name",
    "pan_relationship_type",
    "pan_relationship_name",
    "date_of_birth",
}
_AADHAAR_FIELDS = {
    "aadhaar_number",
    "aadhaar_name",
    "date_of_birth",
    "gender",
    "aadhaar_address",
    "aadhaar_relationship_type",
    "aadhaar_relationship_name",
}
_RECEIPT_FIELDS = tuple(_RECEIPT_CAPTURE_MAP)
_RECEIPT_DATE_FIELDS = {"receipt_date", "payment_reference_date"}
_RECEIPT_DECIMAL_FIELDS = {"amount_paid"}


def receipt_review_key(receipt_ordinal: int, field_key: str) -> str:
    """Return the readable/stable review key for one field on one receipt."""

    return f"raw:receipt_{receipt_ordinal}_{field_key.strip().lower()}"


def receipt_document_ordinals(document_ids: list[UUID]) -> dict[UUID, int]:
    """Assign stable ordinals after capture is closed, without storing another ID."""

    ordered = sorted(set(document_ids), key=str)
    return {document_id: index + 1 for index, document_id in enumerate(ordered)}


def _text_or_none(value: Any) -> str | None:
    if value is None:
        return None
    normalized = " ".join(str(value).split())
    return normalized or None


def _field_rejected(
    *,
    field_key: str,
    rejected_review_keys: set[str],
    receipt_ordinal: int | None = None,
) -> bool:
    normalized = field_key.strip().lower()
    spec = spec_for_field(normalized)
    if spec is not None and f"attribute:{spec.attribute_key}" in rejected_review_keys:
        return True
    if receipt_ordinal is not None:
        return receipt_review_key(receipt_ordinal, normalized) in rejected_review_keys
    return f"raw:{normalized}" in rejected_review_keys


def _normalized_document_values(
    document: Any,
    *,
    allowed_fields: set[str] | tuple[str, ...],
    rejected_review_keys: set[str],
    date_fields: set[str] | None = None,
    decimal_fields: set[str] | None = None,
    boolean_fields: set[str] | None = None,
    receipt_ordinal: int | None = None,
) -> dict[str, Any]:
    allowed = set(allowed_fields)
    dates = date_fields or set()
    decimals = decimal_fields or set()
    booleans = boolean_fields or set()
    values: dict[str, Any] = {}

    for field in document.fields:
        key = str(field.fieldKey).strip().lower()
        if key not in allowed:
            continue
        if _field_rejected(
            field_key=key,
            rejected_review_keys=rejected_review_keys,
            receipt_ordinal=receipt_ordinal,
        ):
            continue
        value = field.value
        if value is None or value == "":
            continue
        if key in dates:
            values[key] = uc03_booking_capture._as_date(value, key.upper())
        elif key in decimals:
            values[key] = uc03_booking_capture._as_decimal(value, key.upper())
        elif key in booleans:
            values[key] = uc03_booking_capture._as_bool(value, key.upper())
        else:
            values[key] = _text_or_none(value)
    return values


def _upsert_review_value_row(
    connection: Connection,
    *,
    table_name: str,
    id_column: str,
    tenant_id: str,
    journey_id: UUID,
    document_id: UUID,
    evidence_id: UUID | None,
    actor_id: str,
    columns: tuple[str, ...],
    values: dict[str, Any],
    extra_insert_columns: dict[str, Any] | None = None,
) -> str:
    extra = extra_insert_columns or {}
    params: dict[str, Any] = {
        "tenant_id": tenant_id,
        "journey_id": journey_id,
        "document_id": document_id,
        "evidence_id": evidence_id,
        "actor_id": actor_id,
    }
    params.update({column: values.get(column) for column in columns})
    params.update(extra)

    business_columns = list(columns) + list(extra)
    insert_columns = [
        "tenant_id",
        "journey_id",
        "source_di_document_id",
        "source_evidence_id",
        *business_columns,
        "reviewed_by_actor_id",
        "reviewed_at_utc",
    ]
    insert_values = [
        ":tenant_id",
        ":journey_id",
        ":document_id",
        ":evidence_id",
        *[f":{column}" for column in business_columns],
        ":actor_id",
        "now()",
    ]
    update_assignments = [
        f"{column}=EXCLUDED.{column}" for column in business_columns
    ] + [
        "source_evidence_id=EXCLUDED.source_evidence_id",
        "reviewed_by_actor_id=EXCLUDED.reviewed_by_actor_id",
        "reviewed_at_utc=now()",
        "updated_at_utc=now()",
        f"version_no=auditcore.{table_name}.version_no+1",
    ]

    row_id = connection.execute(
        text(
            f"""
            INSERT INTO auditcore.{table_name} ({', '.join(insert_columns)})
            VALUES ({', '.join(insert_values)})
            ON CONFLICT (tenant_id, journey_id, source_di_document_id)
            DO UPDATE SET {', '.join(update_assignments)}
            RETURNING {id_column}
            """
        ),
        params,
    ).scalar_one()
    return str(row_id)


def _materialize_commercial_lines(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    document_id: UUID,
    evidence_id: UUID | None,
    values: dict[str, Any],
) -> int:
    written = 0
    for component_key in sorted(_COMMERCIAL_LINE_FIELDS):
        amount = values.get(component_key)
        if amount is None:
            continue
        connection.execute(
            text(
                """
                INSERT INTO auditcore.commercial_lines (
                    tenant_id, journey_id, component_key, actual_amount,
                    actual_source_kind, source_evidence_id, source_reference
                ) VALUES (
                    :tenant_id, :journey_id, :component_key, :actual_amount,
                    'EVIDENCE', :evidence_id, :source_reference
                )
                ON CONFLICT (tenant_id, journey_id, component_key)
                DO UPDATE SET
                    actual_amount=EXCLUDED.actual_amount,
                    actual_source_kind='EVIDENCE',
                    source_evidence_id=EXCLUDED.source_evidence_id,
                    source_reference=EXCLUDED.source_reference,
                    updated_at_utc=now()
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "component_key": component_key,
                "actual_amount": amount,
                "evidence_id": evidence_id,
                "source_reference": f"booking_form:{document_id}",
            },
        )
        written += 1
    return written


def materialize_reviewed_booking_form_values(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    documents: list[Any],
    rejected_review_keys: set[str],
    actor_id: str,
) -> dict[str, int]:
    documents_written = 0
    commercial_lines_written = 0
    for document in documents:
        if (
            str(document.documentTypeKey or "").strip().lower()
            != _BOOKING_FORM_DOCUMENT_TYPE
            or str(document.extractionState).upper() != "READY"
        ):
            continue
        values = _normalized_document_values(
            document,
            allowed_fields=_BOOKING_FORM_FIELDS,
            rejected_review_keys=rejected_review_keys,
            date_fields=_BOOKING_DATE_FIELDS,
            decimal_fields=_BOOKING_DECIMAL_FIELDS,
            boolean_fields=_BOOKING_BOOLEAN_FIELDS,
        )
        if not values:
            continue
        _upsert_review_value_row(
            connection,
            table_name="booking_form_review_values",
            id_column="booking_form_review_value_id",
            tenant_id=tenant_id,
            journey_id=journey_id,
            document_id=document.documentId,
            evidence_id=document.evidenceId,
            actor_id=actor_id,
            columns=_BOOKING_FORM_FIELDS,
            values=values,
        )
        documents_written += 1
        commercial_lines_written += _materialize_commercial_lines(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            document_id=document.documentId,
            evidence_id=document.evidenceId,
            values=values,
        )
    return {
        "documents": documents_written,
        "commercialLines": commercial_lines_written,
    }


def _journey_customer_id(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
) -> UUID:
    return connection.execute(
        text(
            """
            SELECT customer_id
            FROM auditcore.journeys
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).scalar_one()


def materialize_reviewed_identity_values(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    documents: list[Any],
    rejected_review_keys: set[str],
    actor_id: str,
) -> int:
    customer_id = _journey_customer_id(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
    )
    written = 0
    columns = (
        "pan_number",
        "pan_name",
        "pan_father_name",
        "pan_relationship_type",
        "pan_relationship_name",
        "pan_date_of_birth",
        "aadhaar_number",
        "aadhaar_name",
        "aadhaar_date_of_birth",
        "aadhaar_gender",
        "aadhaar_address",
        "aadhaar_relationship_type",
        "aadhaar_relationship_name",
    )
    for document in documents:
        document_type = str(document.documentTypeKey or "").strip().lower()
        if str(document.extractionState).upper() != "READY":
            continue

        row_values: dict[str, Any] = {}
        core_type: str
        if document_type in _PAN_DOCUMENT_TYPES:
            source_values = _normalized_document_values(
                document,
                allowed_fields=_PAN_FIELDS,
                rejected_review_keys=rejected_review_keys,
                date_fields={"date_of_birth"},
            )
            if not source_values:
                continue
            core_type = "PAN"
            row_values = {
                "pan_number": source_values.get("pan_number"),
                "pan_name": source_values.get("pan_name"),
                "pan_father_name": source_values.get("pan_father_name"),
                "pan_relationship_type": source_values.get("pan_relationship_type"),
                "pan_relationship_name": source_values.get("pan_relationship_name"),
                "pan_date_of_birth": source_values.get("date_of_birth"),
            }
        elif document_type == _AADHAAR_DOCUMENT_TYPE:
            source_values = _normalized_document_values(
                document,
                allowed_fields=_AADHAAR_FIELDS,
                rejected_review_keys=rejected_review_keys,
                date_fields={"date_of_birth"},
            )
            if not source_values:
                continue
            core_type = "AADHAAR"
            row_values = {
                "aadhaar_number": source_values.get("aadhaar_number"),
                "aadhaar_name": source_values.get("aadhaar_name"),
                "aadhaar_date_of_birth": source_values.get("date_of_birth"),
                "aadhaar_gender": source_values.get("gender"),
                "aadhaar_address": source_values.get("aadhaar_address"),
                "aadhaar_relationship_type": source_values.get(
                    "aadhaar_relationship_type"
                ),
                "aadhaar_relationship_name": source_values.get(
                    "aadhaar_relationship_name"
                ),
            }
        else:
            continue

        _upsert_review_value_row(
            connection,
            table_name="customer_identity_review_values",
            id_column="customer_identity_review_value_id",
            tenant_id=tenant_id,
            journey_id=journey_id,
            document_id=document.documentId,
            evidence_id=document.evidenceId,
            actor_id=actor_id,
            columns=columns,
            values=row_values,
            extra_insert_columns={
                "customer_id": customer_id,
                "document_type_key": core_type,
            },
        )
        written += 1
    return written


def _has_reviewable_receipt_value(document: Any) -> bool:
    return any(
        str(field.fieldKey).strip().lower() in _RECEIPT_CAPTURE_MAP
        and field.value is not None
        and field.value != ""
        for field in document.fields
    )


def _reviewed_receipt_values(
    document: Any,
    *,
    receipt_ordinal: int,
    rejected_review_keys: set[str],
) -> dict[str, Any]:
    return _normalized_document_values(
        document,
        allowed_fields=_RECEIPT_FIELDS,
        rejected_review_keys=rejected_review_keys,
        date_fields=_RECEIPT_DATE_FIELDS,
        decimal_fields=_RECEIPT_DECIMAL_FIELDS,
        receipt_ordinal=receipt_ordinal,
    )


def _payment_values(receipt_values: dict[str, Any]) -> dict[str, Any]:
    return {
        "receipt_number": receipt_values.get("receipt_number"),
        "receipt_date": receipt_values.get("receipt_date"),
        "amount": receipt_values.get("amount_paid"),
        "payment_method_code": receipt_values.get("payment_mode"),
        "payment_reference": receipt_values.get("payment_reference_no"),
        "receipt_dealer_name": receipt_values.get("dealer_name"),
        "receipt_dealer_gstin": receipt_values.get("dealer_gstin"),
        "receipt_customer_name": receipt_values.get("customer_name"),
        "receipt_customer_phone": receipt_values.get("customer_phone"),
        "payment_reference_date": receipt_values.get("payment_reference_date"),
        "receipt_bank_name": receipt_values.get("bank_name"),
        "receipt_bank_location": receipt_values.get("bank_location"),
        "receipt_booking_reference": receipt_values.get("booking_reference_number"),
        "receipt_remarks": receipt_values.get("remarks"),
        "receipt_amount_in_words": receipt_values.get("amount_in_words"),
    }


def _existing_payment(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    document_id: UUID,
    evidence_id: UUID | None,
):
    select_sql = """
        SELECT payment_id, source_evidence_id, source_di_document_id,
               receipt_number, receipt_date, amount, payment_method_code,
               payment_reference, receipt_dealer_name, receipt_dealer_gstin,
               receipt_customer_name, receipt_customer_phone,
               payment_reference_date, receipt_bank_name, receipt_bank_location,
               receipt_booking_reference, receipt_remarks, receipt_amount_in_words,
               payment_stage, status_source
        FROM auditcore.payments
    """
    row = connection.execute(
        text(
            select_sql
            + """
            WHERE tenant_id=:tenant_id
              AND journey_id=:journey_id
              AND source_di_document_id=:document_id
            ORDER BY created_at_utc, payment_id
            LIMIT 1
            FOR UPDATE
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "document_id": document_id,
        },
    ).mappings().one_or_none()
    if row is not None or evidence_id is None:
        return row

    # Backward-compatible reuse when a legacy Evidence-backed Payment already
    # exists for the same receipt. This prevents a V2 review from duplicating it.
    return connection.execute(
        text(
            select_sql
            + """
            WHERE tenant_id=:tenant_id
              AND journey_id=:journey_id
              AND source_evidence_id=:evidence_id
            ORDER BY created_at_utc, payment_id
            LIMIT 1
            FOR UPDATE
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "evidence_id": evidence_id,
        },
    ).mappings().one_or_none()


def _same_payment_state(
    row: Any,
    *,
    document_id: UUID,
    evidence_id: UUID | None,
    values: dict[str, Any],
) -> bool:
    columns = (
        "receipt_number",
        "receipt_date",
        "amount",
        "payment_method_code",
        "payment_reference",
        "receipt_dealer_name",
        "receipt_dealer_gstin",
        "receipt_customer_name",
        "receipt_customer_phone",
        "payment_reference_date",
        "receipt_bank_name",
        "receipt_bank_location",
        "receipt_booking_reference",
        "receipt_remarks",
        "receipt_amount_in_words",
    )
    return (
        row["source_di_document_id"] == document_id
        and row["source_evidence_id"] == evidence_id
        and all(row[column] == values.get(column) for column in columns)
        and row["payment_stage"] == "BOOKING"
        and row["status_source"] == "EVIDENCE"
    )


def materialize_reviewed_booking_receipts(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    documents: list[Any],
    rejected_review_keys: set[str],
    actor_id: str | None = None,
) -> dict[str, int]:
    """Persist every reviewed Dealer Receipt and its valid Payment projection.

    The dependent receipt row preserves all accepted DI fields. A receipt without an
    accepted amount is deliberately not projected into payments because Payment.amount
    is NOT NULL and fabricating zero would corrupt cumulative-payment business logic.
    """

    receipt_documents = [
        document
        for document in documents
        if str(document.documentTypeKey or "").strip().lower() == _RECEIPT_DOCUMENT_TYPE
        and str(document.extractionState).upper() == "READY"
        and _has_reviewable_receipt_value(document)
    ]
    ordinals = receipt_document_ordinals(
        [document.documentId for document in receipt_documents]
    )

    created = 0
    updated = 0
    unchanged = 0
    skipped_without_amount = 0
    review_rows_written = 0

    for document in receipt_documents:
        receipt_values = _reviewed_receipt_values(
            document,
            receipt_ordinal=ordinals[document.documentId],
            rejected_review_keys=rejected_review_keys,
        )
        if not receipt_values:
            continue

        if actor_id is not None:
            _upsert_review_value_row(
                connection,
                table_name="dealer_receipt_review_values",
                id_column="dealer_receipt_review_value_id",
                tenant_id=tenant_id,
                journey_id=journey_id,
                document_id=document.documentId,
                evidence_id=document.evidenceId,
                actor_id=actor_id,
                columns=_RECEIPT_FIELDS,
                values=receipt_values,
            )
            review_rows_written += 1

        values = _payment_values(receipt_values)
        if values["amount"] is None:
            skipped_without_amount += 1
            continue

        evidence_id = document.evidenceId
        existing = _existing_payment(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            document_id=document.documentId,
            evidence_id=evidence_id,
        )

        params = {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "document_id": document.documentId,
            "evidence_id": evidence_id,
            **values,
        }

        payment_columns = (
            "amount",
            "payment_method_code",
            "payment_reference",
            "receipt_number",
            "receipt_date",
            "receipt_dealer_name",
            "receipt_dealer_gstin",
            "receipt_customer_name",
            "receipt_customer_phone",
            "payment_reference_date",
            "receipt_bank_name",
            "receipt_bank_location",
            "receipt_booking_reference",
            "receipt_remarks",
            "receipt_amount_in_words",
        )
        if existing is None:
            connection.execute(
                text(
                    f"""
                    INSERT INTO auditcore.payments (
                        tenant_id, journey_id, {', '.join(payment_columns)},
                        status_source, source_evidence_id, source_di_document_id,
                        payment_stage
                    ) VALUES (
                        :tenant_id, :journey_id,
                        {', '.join(f':{column}' for column in payment_columns)},
                        'EVIDENCE', :evidence_id, :document_id, 'BOOKING'
                    )
                    """
                ),
                params,
            )
            created += 1
            continue

        if _same_payment_state(
            existing,
            document_id=document.documentId,
            evidence_id=evidence_id,
            values=values,
        ):
            unchanged += 1
            continue

        assignments = [f"{column}=:{column}" for column in payment_columns]
        connection.execute(
            text(
                f"""
                UPDATE auditcore.payments
                SET {', '.join(assignments)},
                    status_source='EVIDENCE',
                    source_evidence_id=:evidence_id,
                    source_di_document_id=:document_id,
                    payment_stage='BOOKING',
                    updated_at_utc=now(),
                    version_no=version_no+1
                WHERE tenant_id=:tenant_id
                  AND payment_id=:payment_id
                """
            ),
            {**params, "payment_id": existing["payment_id"]},
        )
        updated += 1

    result = {
        "created": created,
        "updated": updated,
        "unchanged": unchanged,
        "skippedWithoutAmount": skipped_without_amount,
        "reviewRowsWritten": review_rows_written,
    }
    logger.info(
        "UC03 V2 reviewed receipts materialized",
        extra={
            "tenant_id": tenant_id,
            "journey_id": str(journey_id),
            "created_count": created,
            "updated_count": updated,
            "unchanged_count": unchanged,
            "skipped_without_amount_count": skipped_without_amount,
            "review_rows_written": review_rows_written,
        },
    )
    return result


def materialize_reviewed_di_business_values(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    documents: list[Any],
    rejected_review_keys: set[str],
    actor_id: str,
) -> dict[str, int]:
    """Materialize accepted DI values into typed Audit Core business storage."""

    booking = materialize_reviewed_booking_form_values(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        documents=documents,
        rejected_review_keys=rejected_review_keys,
        actor_id=actor_id,
    )
    identities = materialize_reviewed_identity_values(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        documents=documents,
        rejected_review_keys=rejected_review_keys,
        actor_id=actor_id,
    )
    receipts = materialize_reviewed_booking_receipts(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        documents=documents,
        rejected_review_keys=rejected_review_keys,
        actor_id=actor_id,
    )
    return {
        "bookingFormDocuments": booking["documents"],
        "commercialLines": booking["commercialLines"],
        "identityDocuments": identities,
        "receiptDocuments": receipts["reviewRowsWritten"],
        "receiptPaymentsCreated": receipts["created"],
        "receiptPaymentsUpdated": receipts["updated"],
        "receiptPaymentsUnchanged": receipts["unchanged"],
        "receiptPaymentsSkippedWithoutAmount": receipts["skippedWithoutAmount"],
    }
