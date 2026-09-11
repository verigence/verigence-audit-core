from __future__ import annotations

"""Typed Audit Core materialization for reviewed UC03 Delivery DI values.

Delivery Review already preserves every DI field losslessly in
``journey_document_extracted_fields``.  That provenance table is not the business
owner used by Journey 360, so reviewed Delivery facts that have an explicit,
truthful mapping are also projected into the existing canonical Core tables.

This module deliberately does not infer fuzzy aliases. Unsupported DI fields
remain available losslessly until an explicit typed owner is added. The one
deliberate exception to "never overwrite workflow-owned Delivery timestamps"
is ``actual_delivered_at`` itself: the Gate Pass is the authoritative source
for the real delivery date, so it is allowed to replace whatever operational
default (e.g. the moment a PC clicked "Delivery Completed") is on record --
see ``materialize_delivery_date``.
"""

import json
import logging
from datetime import UTC, date, datetime, time
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from audit_core import uc03_booking_capture
from audit_core import uc03_v2_review_materialization as booking_materialization
from audit_core.uc03_attribute_mapping import spec_for_field
from audit_core.uc03_delivery_commands import _machine_flag, _set_stage_flag_status
from audit_core.uc03_document_registry import is_receipt_document_type
from audit_core.uc03_invoice_materialization import materialize_reviewed_invoices
from audit_core.uc03_payment_reconciliation import (
    materialize_reviewed_bank_statements,
    reconcile_payments,
)
from audit_core.uc03_scrappage_certificate_materialization import (
    materialize_reviewed_scrappage_certificates,
)

logger = logging.getLogger(__name__)

_DELIVERY_ORDER_DOCUMENT_TYPE = "delivery_order_cover"
_INSURANCE_DOCUMENT_TYPE = "insurance_cover"
_GATE_PASS_DOCUMENT_TYPE = "gate_pass"

# These are exact DI field keys, not fuzzy text matches.  ``chassis_no`` is the
# field emitted by verigence-di's delivery_order_cover schema.  The other keys are
# exact canonical-style fields used by tenant invoice schemas when present.
_VIN_FIELD_KEYS = ("vin", "vin_number")
_CHASSIS_FIELD_KEYS = ("chassis_number", "chassis_no")
_INVOICE_REFERENCE_FIELD_KEYS = ("invoice_reference", "invoice_number", "dms_invoice_number")
_DMS_REFERENCE_FIELD_KEYS = ("dms_reference",)
_REGISTRATION_FIELD_KEYS = ("registration_number", "insured_vehicle_reg")
_RTO_CHALLAN_DOCUMENT_TYPE = "rto_challan"
# registration_state/territory/district/type are only meaningful coming from
# an actual RTO Challan -- unlike registration_number, no other document type
# carries a truthful value for these.
_RTO_CHALLAN_FIELDS = {
    "registration_state": "registration_state",
    "registration_territory": "registration_territory",
    "registration_district": "registration_district",
    "registration_type": "registration_type_code",
}

# financed_by (bank/institution name) is a common invoice-schema field, not
# scoped to one document type -- any invoice can carry it. hp_charges_amount
# (hypothecation charges) is RTO-Challan-specific, same reasoning as the
# registration_state/district/territory/type fields above.
_FINANCED_BY_FIELD_KEYS = ("financed_by",)
_HP_CHARGES_FIELD_KEYS = ("hp_charges_amount",)

_INSURANCE_FIELDS = {
    "insurer_name": "insurer_name",
    "policy_number": "policy_reference",
    "premium_amount": "actual_premium_amount",
    "agent_intermediary_name": "agent_intermediary_name",
    "agent_intermediary_code": "agent_intermediary_code",
    "misp_code": "misp_code",
}
_INSURANCE_MERGE_COLUMNS = (
    "insurer_name", "policy_reference", "actual_premium_amount",
    "agent_intermediary_name", "agent_intermediary_code", "misp_code",
)

_VEHICLE_SOURCE_PRIORITY = (
    "customer_invoice_dms",
    "tax_invoice_dms",
    "tax_invoice_tally",
    "tax_invoice",
    _DELIVERY_ORDER_DOCUMENT_TYPE,
)


def _has_value(value: Any) -> bool:
    return value is not None and value != "" and not (
        isinstance(value, str) and not value.strip()
    )


def _ready_documents(documents: list[Any]) -> list[Any]:
    return [
        document
        for document in documents
        if str(getattr(document, "extractionState", "")).upper() == "READY"
    ]


def _field_candidates(
    documents: list[Any],
    field_keys: tuple[str, ...],
    *,
    document_types: set[str] | None = None,
    source_priority: tuple[str, ...] = (),
) -> list[tuple[Any, Any]]:
    keys = {key.casefold() for key in field_keys}
    priorities = {key.casefold(): index for index, key in enumerate(source_priority)}
    candidates: list[tuple[Any, Any]] = []
    for document in _ready_documents(documents):
        document_type = str(getattr(document, "documentTypeKey", "") or "").strip().casefold()
        if document_types is not None and document_type not in document_types:
            continue
        for field in getattr(document, "fields", []):
            if str(getattr(field, "fieldKey", "")).strip().casefold() not in keys:
                continue
            if not _has_value(getattr(field, "value", None)):
                continue
            candidates.append((document, field))

    def sort_key(item: tuple[Any, Any]) -> tuple[int, float, str, str]:
        document, field = item
        document_type = str(getattr(document, "documentTypeKey", "") or "").strip().casefold()
        confidence = getattr(field, "confidenceScore", None)
        confidence_value = float(confidence) if confidence is not None else -1.0
        return (
            priorities.get(document_type, len(priorities)),
            -confidence_value,
            str(getattr(document, "documentId", "")),
            str(getattr(field, "fieldKey", "")),
        )

    return sorted(candidates, key=sort_key)


def _best_field(
    documents: list[Any],
    field_keys: tuple[str, ...],
    *,
    document_types: set[str] | None = None,
    source_priority: tuple[str, ...] = (),
) -> tuple[Any, Any] | None:
    candidates = _field_candidates(
        documents,
        field_keys,
        document_types=document_types,
        source_priority=source_priority,
    )
    return candidates[0] if candidates else None


def _text(value: Any) -> str | None:
    if not _has_value(value):
        return None
    result = " ".join(str(value).split())
    return result or None


def _source_evidence(document: Any) -> UUID | None:
    return getattr(document, "evidenceId", None)


def _to_delivery_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if not _has_value(value):
        return None
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        return None


def materialize_delivery_date(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    documents: list[Any],
) -> int:
    """Set the canonical delivery date from the Gate Pass.

    The Gate Pass date is the ground truth for when the vehicle actually left
    the dealership -- not the timestamp of whichever operational action
    happened to run first (a PC clicking "Delivery Completed" defaults
    ``actual_delivered_at`` to ``now()``, per ``_upsert_delivery_business_record``
    in uc03_delivery_commands.py). Once the Gate Pass is confirmed, its date
    is authoritative and replaces that default -- re-running this on every
    sync keeps it live if the field is later corrected in review, matching
    this pipeline's own "always-live, never-frozen" philosophy elsewhere.
    """

    pair = _best_field(
        documents, ("delivery_date",), document_types={_GATE_PASS_DOCUMENT_TYPE}
    )
    if pair is None:
        return 0
    parsed = _to_delivery_date(getattr(pair[1], "value", None))
    if parsed is None:
        return 0
    actual_delivered_at = datetime.combine(parsed, time.min, tzinfo=UTC)
    source_evidence_id = _source_evidence(pair[0])

    existing = connection.execute(
        text(
            """
            SELECT actual_delivered_at, status_source
            FROM auditcore.deliveries
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
            FOR UPDATE
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one_or_none()

    if (
        existing is not None
        and existing["status_source"] == "EVIDENCE"
        and existing["actual_delivered_at"] == actual_delivered_at
    ):
        return 0

    connection.execute(
        text(
            """
            INSERT INTO auditcore.deliveries (
                tenant_id, journey_id, actual_delivered_at,
                status_source, source_evidence_id
            ) VALUES (
                :tenant_id, :journey_id, :actual_delivered_at,
                'EVIDENCE', :source_evidence_id
            )
            ON CONFLICT (tenant_id, journey_id) DO UPDATE SET
                actual_delivered_at=EXCLUDED.actual_delivered_at,
                status_source='EVIDENCE',
                source_evidence_id=EXCLUDED.source_evidence_id,
                updated_at_utc=now(),
                version_no=auditcore.deliveries.version_no+1
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "actual_delivered_at": actual_delivered_at,
            "source_evidence_id": source_evidence_id,
        },
    )
    return 1


def materialize_delivery_vehicle(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    documents: list[Any],
) -> int:
    """Fill missing canonical vehicle identifiers from reviewed Delivery evidence."""

    selected = {
        "vin": _best_field(
            documents,
            _VIN_FIELD_KEYS,
            source_priority=_VEHICLE_SOURCE_PRIORITY,
        ),
        "chassis_number": _best_field(
            documents,
            _CHASSIS_FIELD_KEYS,
            source_priority=_VEHICLE_SOURCE_PRIORITY,
        ),
        "invoice_reference": _best_field(
            documents,
            _INVOICE_REFERENCE_FIELD_KEYS,
            source_priority=_VEHICLE_SOURCE_PRIORITY,
        ),
        "dms_reference": _best_field(documents, _DMS_REFERENCE_FIELD_KEYS),
    }
    values = {
        key: _text(getattr(pair[1], "value", None)) if pair is not None else None
        for key, pair in selected.items()
    }
    if not any(values.values()):
        return 0

    existing = connection.execute(
        text(
            """
            SELECT vin, chassis_number, dms_reference, invoice_reference,
                   source_kind, source_evidence_id
            FROM auditcore.vehicle_records
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
            FOR UPDATE
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one_or_none()

    merged = dict(values)
    source_evidence_id: UUID | None = None
    if existing is not None:
        for key in ("vin", "chassis_number", "dms_reference", "invoice_reference"):
            merged[key] = existing[key] or values[key]
        source_evidence_id = existing["source_evidence_id"]
    if source_evidence_id is None:
        for pair in selected.values():
            if pair is not None:
                source_evidence_id = _source_evidence(pair[0])
                if source_evidence_id is not None:
                    break

    if existing is not None and all(
        existing[key] == merged[key]
        for key in ("vin", "chassis_number", "dms_reference", "invoice_reference")
    ):
        return 0

    connection.execute(
        text(
            """
            INSERT INTO auditcore.vehicle_records (
                tenant_id, journey_id, vin, chassis_number, dms_reference,
                invoice_reference, source_kind, source_evidence_id
            ) VALUES (
                :tenant_id, :journey_id, :vin, :chassis_number, :dms_reference,
                :invoice_reference, 'EVIDENCE', :source_evidence_id
            )
            ON CONFLICT (tenant_id, journey_id) DO UPDATE SET
                vin=EXCLUDED.vin,
                chassis_number=EXCLUDED.chassis_number,
                dms_reference=EXCLUDED.dms_reference,
                invoice_reference=EXCLUDED.invoice_reference,
                source_kind=COALESCE(auditcore.vehicle_records.source_kind, 'EVIDENCE'),
                source_evidence_id=COALESCE(
                    auditcore.vehicle_records.source_evidence_id,
                    EXCLUDED.source_evidence_id
                ),
                updated_at_utc=now(),
                version_no=auditcore.vehicle_records.version_no+1
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            **merged,
            "source_evidence_id": source_evidence_id,
        },
    )
    return sum(1 for key, value in values.items() if value is not None and (existing is None or existing[key] is None))


def materialize_delivery_registration(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    documents: list[Any],
) -> int:
    """Fill missing registration facts from explicit reviewed Delivery fields.

    registration_number can come from any document that carries a truthful
    vehicle registration (RTO Challan, Delivery Order Cover). State/
    Territory/District/Type are only ever truthful on an actual RTO Challan
    -- confirmed live: DI extracts all four from the Challan (rto_challan.py),
    but only registration_number was ever wired into this table, leaving the
    PC to type State/District in by hand even when the document already
    states them.
    """

    values: dict[str, Any] = {}
    document_for_field: dict[str, Any] = {}

    number_selected = _best_field(documents, _REGISTRATION_FIELD_KEYS)
    if number_selected is not None:
        document, field = number_selected
        registration_number = _text(getattr(field, "value", None))
        if registration_number is not None:
            values["registration_number"] = registration_number
            document_for_field["registration_number"] = document

    for source, destination in _RTO_CHALLAN_FIELDS.items():
        selected = _best_field(documents, (source,), document_types={_RTO_CHALLAN_DOCUMENT_TYPE})
        if selected is None:
            continue
        document, field = selected
        text_value = _text(getattr(field, "value", None))
        if text_value is not None:
            values[destination] = text_value
            document_for_field[destination] = document

    if not values:
        return 0

    existing = connection.execute(
        text(
            """
            SELECT registration_number, registration_state, registration_territory,
                   registration_district, registration_type_code,
                   source_kind, source_evidence_id
            FROM auditcore.registration_records
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
            FOR UPDATE
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one_or_none()

    columns = (
        "registration_number", "registration_state", "registration_territory",
        "registration_district", "registration_type_code",
    )
    merged: dict[str, Any] = {column: values.get(column) for column in columns}
    source_evidence_id: UUID | None = None
    if existing is not None:
        for column in columns:
            merged[column] = existing[column] if existing[column] is not None else merged[column]
        source_evidence_id = existing["source_evidence_id"]
    if source_evidence_id is None:
        for column in columns:
            document = document_for_field.get(column)
            if document is not None:
                source_evidence_id = _source_evidence(document)
                if source_evidence_id is not None:
                    break

    if existing is not None and all(existing[column] == merged[column] for column in columns):
        return 0

    connection.execute(
        text(
            """
            INSERT INTO auditcore.registration_records (
                tenant_id, journey_id, registration_number, registration_state,
                registration_territory, registration_district, registration_type_code,
                source_kind, source_evidence_id
            ) VALUES (
                :tenant_id, :journey_id, :registration_number, :registration_state,
                :registration_territory, :registration_district, :registration_type_code,
                'EVIDENCE', :source_evidence_id
            )
            ON CONFLICT (tenant_id, journey_id) DO UPDATE SET
                registration_number=EXCLUDED.registration_number,
                registration_state=EXCLUDED.registration_state,
                registration_territory=EXCLUDED.registration_territory,
                registration_district=EXCLUDED.registration_district,
                registration_type_code=EXCLUDED.registration_type_code,
                source_kind=COALESCE(
                    auditcore.registration_records.source_kind,
                    'EVIDENCE'
                ),
                source_evidence_id=COALESCE(
                    auditcore.registration_records.source_evidence_id,
                    EXCLUDED.source_evidence_id
                ),
                updated_at_utc=now(),
                version_no=auditcore.registration_records.version_no+1
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            **merged,
            "source_evidence_id": source_evidence_id,
        },
    )
    return sum(
        1 for column in columns
        if values.get(column) is not None and (existing is None or existing[column] is None)
    )


def materialize_delivery_finance(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    documents: list[Any],
) -> int:
    """Fill Finance tab facts from the explicit invoice financier field and
    the RTO Challan's hypothecation charges.

    A finance_records row is only created when a financier is actually
    stated -- no invoice ever declares "this is a cash purchase", so absence
    of financed_by is the correct signal for "presumed cash/outright
    purchase" (see the Finance tab's own empty-state copy), not something to
    default a row for.
    """

    provider_pair = _best_field(documents, _FINANCED_BY_FIELD_KEYS)
    provider_name = _text(getattr(provider_pair[1], "value", None)) if provider_pair is not None else None
    if provider_name is None:
        return 0

    hp_pair = _best_field(documents, _HP_CHARGES_FIELD_KEYS, document_types={_RTO_CHALLAN_DOCUMENT_TYPE})
    financed_amount = None
    if hp_pair is not None:
        raw_amount = getattr(hp_pair[1], "value", None)
        if _has_value(raw_amount):
            financed_amount = uc03_booking_capture._as_decimal(raw_amount, "HP_CHARGES_AMOUNT")

    values = {
        "finance_type_code": "LOAN",
        "provider_name": provider_name,
        "financed_amount": financed_amount,
    }
    columns = ("finance_type_code", "provider_name", "financed_amount")

    # finance_records has no (tenant_id, journey_id) uniqueness -- its own PC
    # write path (payments_finance.put_finance) treats the latest row as "the"
    # finance record for a journey rather than upserting on a key. Match that
    # exact select-then-branch pattern instead of ON CONFLICT.
    existing = connection.execute(
        text(
            """
            SELECT finance_record_id, finance_type_code, provider_name, financed_amount,
                   source_kind, source_evidence_id
            FROM auditcore.finance_records
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
            ORDER BY created_at_utc DESC, finance_record_id DESC
            LIMIT 1
            FOR UPDATE
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one_or_none()

    merged = dict(values)
    source_evidence_id: UUID | None = None
    if existing is not None:
        for column in columns:
            merged[column] = existing[column] if existing[column] is not None else values[column]
        source_evidence_id = existing["source_evidence_id"]
    if source_evidence_id is None:
        for pair in (provider_pair, hp_pair):
            if pair is not None:
                source_evidence_id = _source_evidence(pair[0])
                if source_evidence_id is not None:
                    break

    if existing is not None and all(existing[column] == merged[column] for column in columns):
        return 0

    if existing is None:
        connection.execute(
            text(
                """
                INSERT INTO auditcore.finance_records (
                    tenant_id, journey_id, finance_type_code, provider_name,
                    financed_amount, source_kind, source_evidence_id
                ) VALUES (
                    :tenant_id, :journey_id, :finance_type_code, :provider_name,
                    :financed_amount, 'EVIDENCE', :source_evidence_id
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                **merged,
                "source_evidence_id": source_evidence_id,
            },
        )
    else:
        connection.execute(
            text(
                """
                UPDATE auditcore.finance_records
                SET finance_type_code=:finance_type_code,
                    provider_name=:provider_name,
                    financed_amount=:financed_amount,
                    source_kind=COALESCE(source_kind, 'EVIDENCE'),
                    source_evidence_id=COALESCE(source_evidence_id, :source_evidence_id),
                    updated_at_utc=now()
                WHERE tenant_id=:tenant_id AND finance_record_id=:finance_record_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "finance_record_id": existing["finance_record_id"],
                **merged,
                "source_evidence_id": source_evidence_id,
            },
        )
    return sum(
        1 for column in columns
        if values.get(column) is not None and (existing is None or existing[column] is None)
    )


_FINANCE_HYPOTHECATION_FINDING_TYPE = "FINANCE_HYPOTHECATION_MISSING"
_FINANCE_HYPOTHECATION_RULE_KEY = "FINANCE_HYPOTHECATION_MISSING"


def sync_finance_hypothecation_findings(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    stage_code: str = "DELIVERY",
    correlation_id: str = "",
) -> dict[str, int]:
    """Raise a finding when a financed deal has no hypothecation (HP) charges
    on record; resolve it once one is captured or the deal turns out to be
    cash after all.

    Reads the same finance_records row materialize_delivery_finance keeps
    current -- best-effort and idempotent, safe to call after every Delivery
    review sync. Never raises.
    """
    try:
        finance = connection.execute(
            text(
                """
                SELECT finance_type_code, financed_amount
                FROM auditcore.finance_records
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                ORDER BY created_at_utc DESC, finance_record_id DESC
                LIMIT 1
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id},
        ).mappings().one_or_none()
    except Exception:  # noqa: BLE001 - producer must never break the caller
        return {"raised": 0, "resolved": 0, "error": 1}

    is_financed = finance is not None and bool(str(finance["finance_type_code"] or "").strip())
    has_hp_charges = finance is not None and finance["financed_amount"] is not None and finance["financed_amount"] != 0
    is_gap = is_financed and not has_hp_charges

    if is_gap:
        _machine_flag(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            stage_code=stage_code,
            rule_key=_FINANCE_HYPOTHECATION_RULE_KEY,
            finding_type=_FINANCE_HYPOTHECATION_FINDING_TYPE,
            severity="MEDIUM",
            title="Financed deal has no hypothecation charges on record",
            description=(
                "This deal is financed but no hypothecation (HP) charges amount "
                "has been captured. HP charges are stated on the RTO Challan -- "
                "upload it or confirm the amount if it is genuinely zero."
            ),
            correlation_id=correlation_id,
            safe_payload={"financeTypeCode": finance["finance_type_code"]},
        )
        _set_stage_flag_status(connection, tenant_id=tenant_id, journey_id=journey_id, stage_code=stage_code)
        return {"raised": 1, "resolved": 0}

    updated = connection.execute(
        text(
            """
            UPDATE auditcore.audit_findings
            SET finding_status = 'RESOLVED',
                disposition = 'FIXED',
                resolved_at_utc = now(),
                updated_at_utc = now()
            WHERE tenant_id = :tenant_id AND journey_id = :journey_id
              AND stage_code = :stage_code
              AND finding_type_code = :finding_type
              AND rule_key = :rule_key
              AND finding_status IN ('OPEN', 'ACKNOWLEDGED')
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "stage_code": stage_code,
            "finding_type": _FINANCE_HYPOTHECATION_FINDING_TYPE,
            "rule_key": _FINANCE_HYPOTHECATION_RULE_KEY,
        },
    )
    if updated.rowcount:
        _set_stage_flag_status(connection, tenant_id=tenant_id, journey_id=journey_id, stage_code=stage_code)
    return {"raised": 0, "resolved": updated.rowcount}


def materialize_delivery_insurance(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    documents: list[Any],
) -> int:
    """Fill canonical insurance facts from the explicit DI insurance_cover schema."""

    insurance_documents = {
        _INSURANCE_DOCUMENT_TYPE,
        # Legacy/tenant aliases already used by the UC03 mapping registry.
        "insurance_cover_note",
        "insurance_policy",
    }
    selected: dict[str, tuple[Any, Any] | None] = {
        destination: _best_field(
            documents,
            (source,),
            document_types=insurance_documents,
        )
        for source, destination in _INSURANCE_FIELDS.items()
    }
    values: dict[str, Any] = {}
    for destination, pair in selected.items():
        if pair is None:
            values[destination] = None
            continue
        raw = getattr(pair[1], "value", None)
        if destination == "actual_premium_amount":
            values[destination] = uc03_booking_capture._as_decimal(raw, "PREMIUM_AMOUNT")
        else:
            values[destination] = _text(raw)

    # add_ons is a DI array field (zero dep, engine protect, etc.), not a
    # single text value like the others -- kept out of _INSURANCE_FIELDS'
    # per-column text merge and handled as its own JSON column.
    add_ons_pair = _best_field(documents, ("add_ons",), document_types=insurance_documents)
    add_ons_json = json.dumps(add_ons_pair[1].value) if add_ons_pair is not None and _has_value(add_ons_pair[1].value) else None

    if not any(value is not None for value in values.values()) and add_ons_json is None:
        return 0

    existing = connection.execute(
        text(
            f"""
            SELECT {', '.join(_INSURANCE_MERGE_COLUMNS)}, add_ons,
                   source_kind, source_evidence_id
            FROM auditcore.insurance_records
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
            FOR UPDATE
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one_or_none()

    merged = dict(values)
    merged["add_ons"] = add_ons_json
    source_evidence_id: UUID | None = None
    if existing is not None:
        for key in _INSURANCE_MERGE_COLUMNS:
            merged[key] = existing[key] if existing[key] is not None else values.get(key)
        merged["add_ons"] = existing["add_ons"] if existing["add_ons"] is not None else add_ons_json
        source_evidence_id = existing["source_evidence_id"]
    if source_evidence_id is None:
        candidates = list(selected.values()) + [add_ons_pair]
        for pair in candidates:
            if pair is not None:
                source_evidence_id = _source_evidence(pair[0])
                if source_evidence_id is not None:
                    break

    if existing is not None and all(
        existing[key] == merged[key] for key in (*_INSURANCE_MERGE_COLUMNS, "add_ons")
    ):
        return 0

    connection.execute(
        text(
            f"""
            INSERT INTO auditcore.insurance_records (
                tenant_id, journey_id, {', '.join(_INSURANCE_MERGE_COLUMNS)},
                add_ons, source_kind, source_evidence_id
            ) VALUES (
                :tenant_id, :journey_id, {', '.join(f':{c}' for c in _INSURANCE_MERGE_COLUMNS)},
                CAST(:add_ons AS jsonb), 'EVIDENCE', :source_evidence_id
            )
            ON CONFLICT (tenant_id, journey_id) DO UPDATE SET
                {', '.join(f'{c}=EXCLUDED.{c}' for c in _INSURANCE_MERGE_COLUMNS)},
                add_ons=EXCLUDED.add_ons,
                source_kind=COALESCE(auditcore.insurance_records.source_kind, 'EVIDENCE'),
                source_evidence_id=COALESCE(
                    auditcore.insurance_records.source_evidence_id,
                    EXCLUDED.source_evidence_id
                ),
                updated_at_utc=now(),
                version_no=auditcore.insurance_records.version_no+1
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            **merged,
            "source_evidence_id": source_evidence_id,
        },
    )
    written = sum(
        1
        for key, value in values.items()
        if value is not None and (existing is None or existing[key] is None)
    )
    if add_ons_json is not None and (existing is None or existing["add_ons"] is None):
        written += 1
    return written


def _source_rank(source_priority: tuple[str, ...], source_type: str | None) -> int:
    normalized = str(source_type or "").strip().casefold()
    try:
        return tuple(item.casefold() for item in source_priority).index(normalized)
    except ValueError:
        return len(source_priority) + 1


def _reference_document_type(source_reference: Any) -> str | None:
    if source_reference is None:
        return None
    candidate = str(source_reference)
    return candidate.split(":", 1)[0].strip().casefold() if ":" in candidate else None


def materialize_delivery_commercial_lines(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    documents: list[Any],
) -> int:
    """Project reviewed Delivery commercial values using the existing source registry.

    A Delivery source may replace an existing commercial only when the configured
    UC03 source priority ranks it ahead of the existing source.  This prevents a
    random Delivery upload from overwriting a stronger reviewed Booking source.
    """

    written = 0
    ready = _ready_documents(documents)
    for component_key in sorted(booking_materialization._COMMERCIAL_LINE_FIELDS):
        spec = spec_for_field(component_key)
        if spec is None or "DELIVERY" not in spec.stages:
            continue
        allowed_sources = {item.casefold() for item in spec.source_priority}
        candidates = _field_candidates(ready, (component_key,))
        candidates = [
            pair
            for pair in candidates
            if str(getattr(pair[0], "documentTypeKey", "") or "").strip().casefold()
            in allowed_sources
        ]
        if not candidates:
            continue
        candidates.sort(
            key=lambda pair: (
                _source_rank(
                    spec.source_priority,
                    str(getattr(pair[0], "documentTypeKey", "") or ""),
                ),
                -float(getattr(pair[1], "confidenceScore", None) or -1.0),
                str(getattr(pair[0], "documentId", "")),
            )
        )
        document, field = candidates[0]
        amount = uc03_booking_capture._as_decimal(
            getattr(field, "value", None),
            component_key.upper(),
        )
        document_type = str(getattr(document, "documentTypeKey", "") or "").strip().casefold()
        existing = connection.execute(
            text(
                """
                SELECT source_reference, actual_amount
                FROM auditcore.commercial_lines
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                  AND component_key=:component_key
                FOR UPDATE
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "component_key": component_key,
            },
        ).mappings().one_or_none()
        if existing is not None:
            existing_source = _reference_document_type(existing["source_reference"])
            if existing_source is not None and _source_rank(
                spec.source_priority, existing_source
            ) <= _source_rank(spec.source_priority, document_type):
                continue
            if existing["actual_amount"] == amount and existing_source == document_type:
                continue

        connection.execute(
            text(
                """
                INSERT INTO auditcore.commercial_lines (
                    tenant_id, journey_id, component_key, actual_amount,
                    actual_source_kind, source_evidence_id, source_reference
                ) VALUES (
                    :tenant_id, :journey_id, :component_key, :actual_amount,
                    'EVIDENCE', :source_evidence_id, :source_reference
                )
                ON CONFLICT (tenant_id, journey_id, component_key) DO UPDATE SET
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
                "source_evidence_id": _source_evidence(document),
                "source_reference": f"{document_type}:{document.documentId}",
            },
        )
        written += 1
    return written


def _same_delivery_payment_state(
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
        and row["payment_stage"] == "DELIVERY"
        and row["status_source"] == "EVIDENCE"
    )


def materialize_delivery_receipts(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    documents: list[Any],
    actor_id: str,
) -> dict[str, int]:
    """Persist reviewed Delivery receipts and additive canonical Payment rows."""

    receipt_documents = [
        document
        for document in _ready_documents(documents)
        if is_receipt_document_type(getattr(document, "documentTypeKey", None))
        and booking_materialization._has_reviewable_receipt_value(document)
    ]
    ordinals = booking_materialization.receipt_document_ordinals(
        [document.documentId for document in receipt_documents]
    )
    created = updated = unchanged = skipped = review_rows = 0

    for document in receipt_documents:
        receipt_values = booking_materialization._reviewed_receipt_values(
            document,
            receipt_ordinal=ordinals[document.documentId],
            rejected_review_keys=set(),
        )
        if not receipt_values:
            continue
        booking_materialization._upsert_review_value_row(
            connection,
            table_name="dealer_receipt_review_values",
            id_column="dealer_receipt_review_value_id",
            tenant_id=tenant_id,
            journey_id=journey_id,
            document_id=document.documentId,
            evidence_id=document.evidenceId,
            actor_id=actor_id,
            columns=booking_materialization._RECEIPT_FIELDS,
            values=receipt_values,
        )
        review_rows += 1
        values = booking_materialization._payment_values(receipt_values)
        if values["amount"] is None:
            skipped += 1
            continue

        existing = booking_materialization._existing_payment(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            document_id=document.documentId,
            evidence_id=document.evidenceId,
        )
        params = {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "document_id": document.documentId,
            "evidence_id": document.evidenceId,
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
                        'EVIDENCE', :evidence_id, :document_id, 'DELIVERY'
                    )
                    """
                ),
                params,
            )
            created += 1
            continue
        if _same_delivery_payment_state(
            existing,
            document_id=document.documentId,
            evidence_id=document.evidenceId,
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
                    payment_stage='DELIVERY',
                    updated_at_utc=now(),
                    version_no=version_no+1
                WHERE tenant_id=:tenant_id AND payment_id=:payment_id
                """
            ),
            {**params, "payment_id": existing["payment_id"]},
        )
        updated += 1

    return {
        "reviewRowsWritten": review_rows,
        "created": created,
        "updated": updated,
        "unchanged": unchanged,
        "skippedWithoutAmount": skipped,
    }


def materialize_reviewed_delivery_business_values(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    documents: list[Any],
    actor_id: str,
) -> dict[str, int]:
    """Project reviewed Delivery DI into the canonical Core tables Journey 360 reads."""

    vehicle_fields = materialize_delivery_vehicle(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        documents=documents,
    )
    registration_fields = materialize_delivery_registration(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        documents=documents,
    )
    finance_fields = materialize_delivery_finance(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        documents=documents,
    )
    finance_hypothecation = sync_finance_hypothecation_findings(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
    )
    insurance_fields = materialize_delivery_insurance(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        documents=documents,
    )
    delivery_date_set = materialize_delivery_date(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        documents=documents,
    )
    invoices = materialize_reviewed_invoices(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        documents=documents,
        actor_id=actor_id,
    )
    scrappage_certificates = materialize_reviewed_scrappage_certificates(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        documents=documents,
        actor_id=actor_id,
    )
    commercial_lines = materialize_delivery_commercial_lines(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        documents=documents,
    )
    receipts = materialize_delivery_receipts(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        documents=documents,
        actor_id=actor_id,
    )
    bank_lines = materialize_reviewed_bank_statements(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        documents=documents,
        actor_id=actor_id,
    )
    reconciliation = reconcile_payments(
        connection, tenant_id=tenant_id, journey_id=journey_id, correlation_id="",
    )
    result = {
        "vehicleFields": vehicle_fields,
        "registrationFields": registration_fields,
        "financeFields": finance_fields,
        "financeHypothecationRaised": finance_hypothecation["raised"],
        "financeHypothecationResolved": finance_hypothecation["resolved"],
        "insuranceFields": insurance_fields,
        "deliveryDateSet": delivery_date_set,
        "invoicesMaterialized": invoices["invoices"],
        "invoiceCommercialLines": invoices["commercialLines"],
        "invoiceDiscountApplications": invoices["discountApplications"],
        "scrappageCertificatesMaterialized": scrappage_certificates,
        "commercialLines": commercial_lines,
        "bankStatementLines": bank_lines,
        "paymentReconciliation": reconciliation,
        "receiptDocuments": receipts["reviewRowsWritten"],
        "receiptPaymentsCreated": receipts["created"],
        "receiptPaymentsUpdated": receipts["updated"],
        "receiptPaymentsUnchanged": receipts["unchanged"],
        "receiptPaymentsSkippedWithoutAmount": receipts["skippedWithoutAmount"],
    }
    logger.info(
        "UC03 reviewed Delivery DI materialized into canonical Core",
        extra={"tenant_id": tenant_id, "journey_id": str(journey_id), **result},
    )
    return result
