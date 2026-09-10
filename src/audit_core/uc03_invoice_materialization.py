"""uc03_invoice_materialization.py — project reviewed dealer invoices into the
canonical Audit Core reconciliation tables.

The DI generalized-invoice schema (verigence-di ``schemas/invoice.py``) extracts
every dealer invoice type against one lossless superset with deliberately neutral
commercial names — it never emits ``ex_showroom_price`` / ``insurance_amount`` /
``discount_amount``.  This module:

  1. Persists one typed row per reviewed invoice document in
     ``invoice_review_values`` (multiple invoice types coexist on one journey and
     are stored separately — the user's explicit requirement).
  2. Derives the per-component amounts deterministically from ``invoice_purpose``
     and each ``line_items[].line_category`` — no fuzzy label matching.
  3. Upserts those into ``commercial_lines`` / ``discount_applications`` /
     ``journey_addons`` with the invoice-first source priority already configured
     in ``uc03_attribute_mapping`` (an invoice value replaces an existing
     commercial only when its document type ranks ahead of the existing source).

Deterministic, idempotent, and safe to run inside the Review-Confirm transaction.
"""
from __future__ import annotations

import json
import logging
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from audit_core.uc03_attribute_mapping import spec_for_field
from audit_core.uc03_masters_alignment import canonical_discount_key
from audit_core.uc03_v2_review_materialization import (
    _INVOICE_DOCUMENT_TYPES as INVOICE_DOCUMENT_TYPES,
)
from audit_core.uc03_v2_review_materialization import _upsert_review_value_row

logger = logging.getLogger(__name__)

_ORIGIN = "INVOICE_MATERIALIZATION"

# invoice header scalar fields persisted verbatim in invoice_review_values.
_HEADER_TEXT_FIELDS = (
    "invoice_purpose",
    "invoice_nature",
    "invoice_heading_as_printed",
    "source_system",
    "issuer_role",
    "invoice_number",
    "seller_name",
    "seller_gstin",
    "seller_address",
    "buyer_name",
    "buyer_customer_id",
    "buyer_gstin",
    "buyer_gstin_status",
    "buyer_address",
    "financed_by",
    "amount_in_words",
    "narration",
    "vehicle_description_raw",
    "sku_code",
    "model_name_raw",
    "variant_raw",
    "vin_number",
    "chassis_number",
    "engine_number",
    "vehicle_color",
    "vehicle_registration_number",
    "plan_name",
)
_HEADER_DATE_FIELDS = ("invoice_date", "coverage_start_date", "coverage_end_date")
_HEADER_DECIMAL_FIELDS = (
    "gross_amount_before_discount",
    "invoice_discount_amount",
    "taxable_amount",
    "cgst_amount",
    "sgst_amount",
    "igst_amount",
    "cess_amount",
    "tcs_amount",
    "round_off_amount",
    "grand_total_amount",
)
_HEADER_INT_FIELDS = ("tenure_months",)
# line_items is jsonb — written separately (the shared upsert helper has no CAST).
_HEADER_COLUMNS = (
    *_HEADER_TEXT_FIELDS,
    *_HEADER_DATE_FIELDS,
    *_HEADER_DECIMAL_FIELDS,
    *_HEADER_INT_FIELDS,
)

# ── deterministic derivation maps ────────────────────────────────────────────
# A whole invoice whose purpose is one of these feeds a single commercial line
# from its grand total (fallback: taxable amount).
_PURPOSE_TO_COMPONENT: dict[str, str] = {
    "ACCESSORY": "accessories_cost",
    "EXTENDED_WARRANTY": "additional_warranty_amount",
    "RSA": "rsa_amount",
}
_VEHICLE_PURPOSES = frozenset({"VEHICLE_SALE", "VEHICLE_WHOLESALE"})

# line_items[].line_category -> commercial component (amounts for the same
# component sum across line items).
_LINE_CATEGORY_TO_COMPONENT: dict[str, str] = {
    "ACCESSORY_GENUINE": "accessories_cost",
    "ACCESSORY_NON_GENUINE": "accessories_cost",
    "EXTENDED_WARRANTY": "additional_warranty_amount",
    "RSA": "rsa_amount",
    "INSURANCE": "insurance_amount",
    "FASTAG": "fastag_amount",
    "TCS": "tcs_amount",
}
_DISCOUNT_LINE_CATEGORY = "DISCOUNT_LINE"
_VEHICLE_LINE_CATEGORY = "VEHICLE"

# journey_addons mirrors (kept in step with uc03_v2_review_materialization).
_COMPONENT_TO_ADDON: dict[str, str] = {
    "additional_warranty_amount": "ADDITIONAL_WARRANTY",
    "accessories_cost": "ACCESSORIES_TOTAL",
    "rsa_amount": "RSA",
    "fastag_amount": "FASTAG",
}


def _to_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    text_value = str(value).strip().replace(",", "")
    if not text_value or text_value.lower() in {"n/a", "na", "none", "null", "-"}:
        return None
    try:
        return Decimal(text_value)
    except (InvalidOperation, ValueError):
        return None


def _to_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if value is None:
        return None
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        return None


def _to_int(value: Any) -> int | None:
    dec = _to_decimal(value)
    return int(dec) if dec is not None else None


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    normalized = " ".join(str(value).split())
    return normalized or None


def _upper(value: Any) -> str:
    return _clean_text(value).upper() if _clean_text(value) is not None else ""


def _line_item_rows(value: Any) -> list[dict[str, Any]]:
    """Normalise a DI array field into a list of dict rows."""
    raw = value
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return []
    if not isinstance(raw, list):
        return []
    return [row for row in raw if isinstance(row, dict)]


def _line_amount(item: dict[str, Any]) -> Decimal | None:
    for key in ("net_amount", "taxable_amount", "gross_amount", "unit_rate"):
        amount = _to_decimal(item.get(key))
        if amount is not None:
            return amount
    return None


def _fields_by_key(document: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for field in getattr(document, "fields", []) or []:
        key = str(getattr(field, "fieldKey", "")).strip().lower()
        if not key:
            continue
        out[key] = getattr(field, "value", None)
    return out


def _header_values(raw: dict[str, Any]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for key in _HEADER_TEXT_FIELDS:
        values[key] = _clean_text(raw.get(key))
    for key in _HEADER_DATE_FIELDS:
        values[key] = _to_date(raw.get(key))
    for key in _HEADER_DECIMAL_FIELDS:
        values[key] = _to_decimal(raw.get(key))
    for key in _HEADER_INT_FIELDS:
        values[key] = _to_int(raw.get(key))
    return values


# ── derivation ──────────────────────────────────────────────────────────────
def derive_commercials(raw: dict[str, Any]) -> dict[str, Decimal]:
    """OEM-neutral invoice fields -> Audit Core commercial component amounts."""
    # A credit note reduces an earlier invoice; it is never a fresh sale, so
    # it contributes nothing here -- derive_discounts below is where its
    # value lands instead.
    if _upper(raw.get("invoice_nature")) == "CREDIT_NOTE":
        return {}
    purpose = _upper(raw.get("invoice_purpose"))
    lines = _line_item_rows(raw.get("line_items"))
    out: dict[str, Decimal] = {}

    def add(component: str, amount: Decimal | None) -> None:
        if amount is None:
            return
        out[component] = out.get(component, Decimal(0)) + amount

    # 1) line items classified by DI carry the most specific signal.
    vehicle_line_total = Decimal(0)
    saw_vehicle_line = False
    for item in lines:
        category = _upper(item.get("line_category"))
        amount = _line_amount(item)
        if amount is None:
            continue
        if category == _VEHICLE_LINE_CATEGORY:
            vehicle_line_total += amount
            saw_vehicle_line = True
            continue
        component = _LINE_CATEGORY_TO_COMPONENT.get(category)
        if component is not None:
            add(component, amount)

    # 2) a single-purpose invoice (accessory / EW / RSA) feeds one line from its
    #    grand total when line items did not already account for it.
    single_component = _PURPOSE_TO_COMPONENT.get(purpose)
    if single_component is not None and single_component not in out:
        add(
            single_component,
            _to_decimal(raw.get("grand_total_amount"))
            or _to_decimal(raw.get("taxable_amount")),
        )

    # 3) a vehicle-sale invoice: taxable value is the ex-showroom price, plus the
    #    printed TCS. Never map grand_total (that is the invoice total, not on-road).
    if purpose in _VEHICLE_PURPOSES:
        ex_showroom = _to_decimal(raw.get("taxable_amount"))
        if ex_showroom is None and saw_vehicle_line:
            ex_showroom = vehicle_line_total
        add("ex_showroom_price", ex_showroom)
        if "tcs_amount" not in out:
            add("tcs_amount", _to_decimal(raw.get("tcs_amount")))

    return out


def derive_discounts(raw: dict[str, Any]) -> dict[str, Decimal]:
    """Invoice-level + DISCOUNT_LINE amounts -> canonical discount keys."""
    # A credit note's whole value is a reduction against an earlier invoice --
    # not tied to a specific OEM scheme entitlement, so it lands under the
    # same discretionary bucket a dealer's own over-grant discount does.
    if _upper(raw.get("invoice_nature")) == "CREDIT_NOTE":
        amount = _to_decimal(raw.get("grand_total_amount")) or _to_decimal(raw.get("taxable_amount"))
        if amount is None or amount == 0:
            return {}
        return {canonical_discount_key("ADDITIONAL_DISCOUNT"): abs(amount)}

    total = Decimal(0)
    invoice_level = _to_decimal(raw.get("invoice_discount_amount"))
    if invoice_level is not None:
        total += invoice_level
    for item in _line_item_rows(raw.get("line_items")):
        if _upper(item.get("line_category")) != _DISCOUNT_LINE_CATEGORY:
            continue
        amount = _line_amount(item) or _to_decimal(item.get("discount_amount"))
        if amount is not None:
            total += abs(amount)
    if total == 0:
        return {}
    return {canonical_discount_key("CASH_DISCOUNT"): total}


# ── canonical upserts (invoice-first precedence) ─────────────────────────────
def _reference_document_type(source_reference: Any) -> str | None:
    if source_reference is None:
        return None
    candidate = str(source_reference)
    return candidate.split(":", 1)[0].strip().casefold() if ":" in candidate else None


def _source_rank(priority: tuple[str, ...], document_type: str | None) -> int:
    normalized = str(document_type or "").strip().casefold()
    for index, item in enumerate(priority):
        if item.casefold() == normalized:
            return index
    return len(priority) + 1


def _upsert_commercial_line(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    component_key: str,
    amount: Decimal,
    document_type: str,
    document_id: UUID,
    evidence_id: UUID | None,
) -> bool:
    spec = spec_for_field(component_key)
    priority = spec.source_priority if spec is not None else (document_type,)
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
        {"tenant_id": tenant_id, "journey_id": journey_id, "component_key": component_key},
    ).mappings().one_or_none()
    if existing is not None:
        existing_source = _reference_document_type(existing["source_reference"])
        if existing_source is not None and _source_rank(priority, existing_source) <= _source_rank(
            priority, document_type
        ):
            # existing source is the same or stronger — do not overwrite it
            return False
        if existing["actual_amount"] == amount and existing_source == document_type.casefold():
            return False
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
            "evidence_id": evidence_id,
            "source_reference": f"{document_type.casefold()}:{document_id}",
        },
    )
    return True


def _upsert_addon(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    addon_type_code: str,
    amount: Decimal,
    document_type: str,
    evidence_id: UUID | None,
) -> None:
    details = json.dumps({"origin": _ORIGIN, "sourceDocumentType": document_type.casefold()})
    connection.execute(
        text(
            """
            WITH upd AS (
                UPDATE auditcore.journey_addons
                SET actual_amount=:amount,
                    source_kind='EVIDENCE',
                    source_evidence_id=:evidence_id,
                    details=CAST(:details AS jsonb),
                    updated_at_utc=now()
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                  AND addon_type_code=:addon_type_code
                  AND (details->>'origin' IS DISTINCT FROM 'BOOKING_FORM_MATERIALIZATION'
                       OR source_kind='EVIDENCE')
                RETURNING journey_addon_id
            )
            INSERT INTO auditcore.journey_addons (
                tenant_id, journey_id, addon_type_code, actual_amount,
                source_kind, source_evidence_id, details
            )
            SELECT :tenant_id, :journey_id, :addon_type_code, :amount,
                   'EVIDENCE', :evidence_id, CAST(:details AS jsonb)
            WHERE NOT EXISTS (SELECT 1 FROM upd)
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "addon_type_code": addon_type_code,
            "amount": amount,
            "evidence_id": evidence_id,
            "details": details,
        },
    )


def _upsert_invoice_discount(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    discount_key: str,
    amount: Decimal,
    document_type: str,
    document_id: UUID,
    evidence_id: UUID | None,
) -> None:
    """Write the canonical invoice discount, beating a booking-form actual.

    An existing invoice-origin row is only replaced when the new invoice type
    ranks ahead in the discount source priority; a booking-form / calculated
    row's actual is always superseded by an invoice.
    """
    priority = ("tax_invoice_tally", "customer_invoice_dms", "wholesale_invoice",
                "invoice_generic", "accessory_invoice_dms", "accessory_invoice_tally",
                "ew_invoice", "rsa_invoice")
    existing = connection.execute(
        text(
            """
            SELECT discount_application_id, details, standard_eligible_amount,
                   eligibility_result
            FROM auditcore.discount_applications
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND discount_key=:discount_key
            ORDER BY created_at_utc DESC
            LIMIT 1
            FOR UPDATE
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id, "discount_key": discount_key},
    ).mappings().one_or_none()

    details_payload: dict[str, Any] = {
        "origin": _ORIGIN,
        "sourceDocumentType": document_type.casefold(),
        "sourceDocumentId": str(document_id),
    }

    if existing is not None:
        current_details = existing["details"] or {}
        if isinstance(current_details, str):
            try:
                current_details = json.loads(current_details)
            except ValueError:
                current_details = {}
        if current_details.get("origin") == _ORIGIN:
            incumbent = str(current_details.get("sourceDocumentType") or "")
            if _source_rank(priority, incumbent) <= _source_rank(priority, document_type):
                return
        # preserve a calculated standard already reconciled onto this row
        merged_details = {**current_details, **details_payload}
        connection.execute(
            text(
                """
                UPDATE auditcore.discount_applications
                SET actual_discount_amount=:amount,
                    actual_source_kind='EVIDENCE',
                    source_evidence_id=:evidence_id,
                    details=CAST(:details AS jsonb),
                    updated_at_utc=now()
                WHERE tenant_id=:tenant_id AND discount_application_id=:application_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "application_id": existing["discount_application_id"],
                "amount": amount,
                "evidence_id": evidence_id,
                "details": json.dumps(merged_details),
            },
        )
        return

    connection.execute(
        text(
            """
            INSERT INTO auditcore.discount_applications (
                tenant_id, journey_id, discount_key,
                actual_discount_amount, actual_source_kind,
                source_evidence_id, details
            ) VALUES (
                :tenant_id, :journey_id, :discount_key,
                :amount, 'EVIDENCE', :evidence_id, CAST(:details AS jsonb)
            )
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "discount_key": discount_key,
            "amount": amount,
            "evidence_id": evidence_id,
            "details": json.dumps(details_payload),
        },
    )


# ── producer ────────────────────────────────────────────────────────────────
def materialize_reviewed_invoices(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    documents: list[Any],
    actor_id: str,
) -> dict[str, int]:
    """Persist every reviewed invoice and project its derived commercial +
    discount amounts into the canonical reconciliation tables."""

    invoices_written = 0
    commercial_lines_written = 0
    discount_rows_written = 0
    addons_written = 0

    for document in documents:
        document_type = str(getattr(document, "documentTypeKey", "") or "").strip().lower()
        if document_type not in INVOICE_DOCUMENT_TYPES:
            continue
        if str(getattr(document, "extractionState", "")).upper() != "READY":
            continue

        raw = _fields_by_key(document)
        if not raw:
            continue

        header = _header_values(raw)
        row_id = _upsert_review_value_row(
            connection,
            table_name="invoice_review_values",
            id_column="invoice_review_value_id",
            tenant_id=tenant_id,
            journey_id=journey_id,
            document_id=document.documentId,
            evidence_id=getattr(document, "evidenceId", None),
            actor_id=actor_id,
            columns=_HEADER_COLUMNS,
            values=header,
            extra_insert_columns={"document_type_key": document_type},
        )
        connection.execute(
            text(
                """
                UPDATE auditcore.invoice_review_values
                SET line_items = CAST(:line_items AS jsonb)
                WHERE tenant_id = :tenant_id
                  AND invoice_review_value_id = CAST(:row_id AS uuid)
                """
            ),
            {
                "tenant_id": tenant_id,
                "row_id": row_id,
                "line_items": json.dumps(_line_item_rows(raw.get("line_items"))),
            },
        )
        invoices_written += 1

        evidence_id = getattr(document, "evidenceId", None)
        for component_key, amount in derive_commercials(raw).items():
            if _upsert_commercial_line(
                connection,
                tenant_id=tenant_id,
                journey_id=journey_id,
                component_key=component_key,
                amount=amount,
                document_type=document_type,
                document_id=document.documentId,
                evidence_id=evidence_id,
            ):
                commercial_lines_written += 1
            addon_code = _COMPONENT_TO_ADDON.get(component_key)
            if addon_code is not None:
                _upsert_addon(
                    connection,
                    tenant_id=tenant_id,
                    journey_id=journey_id,
                    addon_type_code=addon_code,
                    amount=amount,
                    document_type=document_type,
                    evidence_id=evidence_id,
                )
                addons_written += 1

        for discount_key, amount in derive_discounts(raw).items():
            _upsert_invoice_discount(
                connection,
                tenant_id=tenant_id,
                journey_id=journey_id,
                discount_key=discount_key,
                amount=amount,
                document_type=document_type,
                document_id=document.documentId,
                evidence_id=evidence_id,
            )
            discount_rows_written += 1

    result = {
        "invoices": invoices_written,
        "commercialLines": commercial_lines_written,
        "discountApplications": discount_rows_written,
        "journeyAddons": addons_written,
    }
    if invoices_written:
        logger.info(
            "UC03 reviewed invoices materialized",
            extra={"tenant_id": tenant_id, "journey_id": str(journey_id), **result},
        )
    return result


__all__ = [
    "INVOICE_DOCUMENT_TYPES",
    "derive_commercials",
    "derive_discounts",
    "materialize_reviewed_invoices",
]
