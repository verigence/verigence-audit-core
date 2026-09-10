"""uc03_scrappage_certificate_materialization.py — project reviewed Vehicle
Scrappage Certificate of Deposit documents into a typed Audit Core record.

DI classifies and extracts Vehicle Scrappage Certificates of Deposit
(verigence-di ``schemas/scrappage_certificate.py``) -- both the plain
"Certificate of Deposit" an RVSF issues when a vehicle is scrapped, and the
"Transfer Certificate of Deposit" recording a resale of one via the trading
portal. This module persists one typed row per reviewed certificate document
in ``scrappage_certificate_review_values`` (a journey can plausibly hold more
than one -- the original certificate plus a transfer recording its resale --
so this is keyed per source document, same precedent as
``uc03_invoice_materialization.py``'s ``invoice_review_values``).

This is deliberately a pure "capture the evidence" materializer: unlike
invoices, there is no commercial-line/discount derivation here -- the
scrappage discount amount itself is already captured off the Booking Form
(``uc03_booking_confirmation_rules.py``), and this module's job is only to
make the certificate + old-vehicle evidence queryable so that rule can verify
presence instead of always raising unconditionally.

Shares the same generic per-document upsert helper
(``uc03_v2_review_materialization._upsert_review_value_row``) and is called
from both the Booking (``uc03_v2_review_materialization.materialize_reviewed_
di_business_values``) and Delivery (``uc03_delivery_review_materialization.
materialize_reviewed_delivery_business_values``) orchestrators -- a
certificate can plausibly surface at either stage.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from sqlalchemy import Connection

from audit_core.uc03_v2_review_materialization import _upsert_review_value_row

_SCRAPPAGE_DOCUMENT_TYPE = "scrappage_certificate_of_deposit"

_TEXT_FIELDS = (
    "certificate_variant",
    "certificate_number",
    "old_vehicle_registration_number",
    "old_vehicle_make",
    "old_vehicle_model",
    "old_vehicle_category",
    "old_vehicle_type",
    "old_vehicle_fuel_type",
    "old_vehicle_year_of_manufacturing",
    "original_owner_name",
    "current_holder_name",
    "current_holder_mobile",
    "current_holder_pan",
    "trade_number",
    "scrapping_facility_name",
    "rvsf_registration_number",
    "state_of_scrapping",
)
_DATE_FIELDS = ("trade_date", "certificate_issue_date", "certificate_valid_until_date")
_DECIMAL_FIELDS = (
    "old_vehicle_cubic_capacity",
    "old_vehicle_unladen_weight_kg",
    "old_vehicle_gross_vehicle_weight_kg",
    "old_vehicle_wheelbase_mm",
)
_INT_FIELDS = ("old_vehicle_seating_capacity", "old_vehicle_number_of_cylinders")
_COLUMNS = (*_TEXT_FIELDS, *_DATE_FIELDS, *_DECIMAL_FIELDS, *_INT_FIELDS)


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    normalized = " ".join(str(value).split())
    return normalized or None


def _to_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if value is None:
        return None
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        return None


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


def _to_int(value: Any) -> int | None:
    dec = _to_decimal(value)
    return int(dec) if dec is not None else None


def _fields_by_key(document: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for field in getattr(document, "fields", []) or []:
        key = str(getattr(field, "fieldKey", "")).strip().lower()
        if not key:
            continue
        out[key] = getattr(field, "value", None)
    return out


def _certificate_values(raw: dict[str, Any]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for key in _TEXT_FIELDS:
        values[key] = _clean_text(raw.get(key))
    for key in _DATE_FIELDS:
        values[key] = _to_date(raw.get(key))
    for key in _DECIMAL_FIELDS:
        values[key] = _to_decimal(raw.get(key))
    for key in _INT_FIELDS:
        values[key] = _to_int(raw.get(key))
    return values


def materialize_reviewed_scrappage_certificates(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    documents: list[Any],
    actor_id: str,
) -> int:
    """Persist every reviewed Scrappage Certificate of Deposit document.

    Returns the number of certificate rows written/updated. Never raises --
    a document missing the fields needed to identify it (raw empty) is
    skipped rather than written as an empty row.
    """
    written = 0
    for document in documents:
        document_type = str(getattr(document, "documentTypeKey", "") or "").strip().lower()
        if document_type != _SCRAPPAGE_DOCUMENT_TYPE:
            continue
        if str(getattr(document, "extractionState", "")).upper() != "READY":
            continue

        raw = _fields_by_key(document)
        if not raw:
            continue

        values = _certificate_values(raw)
        _upsert_review_value_row(
            connection,
            table_name="scrappage_certificate_review_values",
            id_column="scrappage_certificate_review_value_id",
            tenant_id=tenant_id,
            journey_id=journey_id,
            document_id=document.documentId,
            evidence_id=getattr(document, "evidenceId", None),
            actor_id=actor_id,
            columns=_COLUMNS,
            values=values,
            extra_insert_columns={"document_type_key": document_type},
        )
        written += 1

    return written
